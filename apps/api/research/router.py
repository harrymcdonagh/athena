from functools import lru_cache
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import Engine

from apps.api.config import get_settings
from apps.api.db import get_engine
from apps.api.edgar.client import EdgarClient, FilingNotFoundError, TickerNotFoundError
from apps.api.edgar.sections import SECTIONS, SectionExtractionError
from apps.api.research.embeddings import (
    Embedder,
    EmbeddingError,
    VoyageEmbedder,
    semantic_search,
)
from apps.api.research.qa import (
    ClaudeQaAnswerer,
    QaAnswer,
    QaAnswerer,
    QaError,
    answer_question,
)
from apps.api.research.repository import Repository
from apps.api.research.service import (
    FilingAlreadyIngestedError,
    ResearchService,
    UpstreamError,
)
from apps.api.research.summarizer import ClaudeSummarizer, SummarizationError

router = APIRouter()

MAX_SEARCH_LIMIT = 25


@lru_cache
def get_research_service() -> ResearchService:
    settings = get_settings()
    return ResearchService(
        edgar=EdgarClient(user_agent=settings.sec_edgar_user_agent),
        summarizer=ClaudeSummarizer(api_key=settings.anthropic_api_key),
        engine=get_engine(),
    )


def get_read_engine() -> Engine:
    return get_engine()


@lru_cache
def get_embedder() -> Embedder:
    settings = get_settings()
    if not settings.voyage_api_key:
        raise HTTPException(
            status_code=503, detail="semantic search unavailable: VOYAGE_API_KEY is not set"
        )
    return VoyageEmbedder(api_key=settings.voyage_api_key)


@lru_cache
def get_qa_answerer() -> QaAnswerer:
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise HTTPException(status_code=503, detail="QA unavailable: ANTHROPIC_API_KEY is not set")
    return ClaudeQaAnswerer(api_key=settings.anthropic_api_key)


class ResearchResponse(BaseModel):
    ticker: str
    company_id: int
    filing_id: int
    accession_number: str
    filing_url: str
    summaries: dict[str, str]
    thesis_snapshot_id: int


class SummaryResponse(BaseModel):
    ticker: str
    company_name: str
    accession_number: str
    filing_date: str
    filing_url: str
    summaries: dict[str, str]
    thesis: str


class SearchResult(BaseModel):
    content: str
    source_url: str
    ticker: str
    section: str
    filing_id: int
    chunk_index: int
    distance: float


class QaRequest(BaseModel):
    question: str
    ticker: str | None = None
    section: str | None = None
    limit: int = Field(default=8, ge=1)


class QaWarningResponse(BaseModel):
    kind: Literal["uncited_claim", "unknown_citation"]
    message: str


class QaResponse(BaseModel):
    """HTTP mirror of QaResult. `answer` reuses QaAnswer verbatim, so a verdict or
    recommendation stays structurally unrepresentable (ADR-0007 §3); `citations`
    carry each chunk's source_url so the audit chain crosses the HTTP boundary."""

    answer: QaAnswer
    citations: dict[str, SearchResult]
    warnings: list[QaWarningResponse]


# Route matching is sequential: POST /research/qa must be registered before
# POST /research/{ticker}, or the path-param route captures "qa" as a ticker.
@router.post("/research/qa", response_model=QaResponse)
def qa(
    request: QaRequest,
    engine: Annotated[Engine, Depends(get_read_engine)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
    answerer: Annotated[QaAnswerer, Depends(get_qa_answerer)],
) -> QaResponse:
    if request.section is not None and request.section not in SECTIONS:
        raise HTTPException(
            status_code=422, detail=f"section must be one of: {', '.join(SECTIONS)}"
        )
    try:
        result = answer_question(
            engine,
            embedder,
            answerer,
            request.question,
            limit=min(request.limit, MAX_SEARCH_LIMIT),
            ticker=request.ticker,
            section=request.section,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (EmbeddingError, QaError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return QaResponse(
        answer=result.answer,
        citations={label: SearchResult(**vars(match)) for label, match in result.citations.items()},
        warnings=[QaWarningResponse(kind=w.kind, message=w.message) for w in result.warnings],
    )


@router.post("/research/{ticker}", response_model=ResearchResponse)
def run_research(
    ticker: str, service: Annotated[ResearchService, Depends(get_research_service)]
) -> ResearchResponse:
    try:
        outcome = service.run(ticker)
    except (TickerNotFoundError, FilingNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FilingAlreadyIngestedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SectionExtractionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (UpstreamError, SummarizationError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ResearchResponse(ticker=ticker.upper(), **vars(outcome))


@router.get("/companies/{ticker}/summary", response_model=SummaryResponse)
def latest_summary(
    ticker: str, engine: Annotated[Engine, Depends(get_read_engine)]
) -> SummaryResponse:
    with engine.connect() as conn:
        view = Repository(conn).latest_research(ticker)
    if view is None:
        raise HTTPException(status_code=404, detail=f"no research stored for {ticker!r}")
    return SummaryResponse(
        ticker=view.ticker,
        company_name=view.company_name,
        accession_number=view.accession_number,
        filing_date=view.filing_date.isoformat(),
        filing_url=view.filing_url,
        summaries=view.summaries,
        thesis=view.thesis,
    )


@router.get("/research/search", response_model=list[SearchResult])
def search(
    q: str,
    engine: Annotated[Engine, Depends(get_read_engine)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
    ticker: str | None = None,
    section: str | None = None,
    limit: Annotated[int, Query(ge=1)] = 8,
) -> list[SearchResult]:
    if section is not None and section not in SECTIONS:
        raise HTTPException(
            status_code=422, detail=f"section must be one of: {', '.join(SECTIONS)}"
        )
    try:
        matches = semantic_search(
            engine, embedder, q, limit=min(limit, MAX_SEARCH_LIMIT), ticker=ticker, section=section
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except EmbeddingError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return [SearchResult(**vars(match)) for match in matches]
