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
    ChangeEntry,
    ClaudeQaAnswerer,
    ComparisonAnswerer,
    QaAnswer,
    QaAnswerer,
    QaError,
    answer_question,
    detect_changes,
)
from apps.api.research.repository import Repository
from apps.api.research.service import (
    ResearchService,
    UnsupportedFilingTypeError,
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


@lru_cache
def get_comparison_answerer() -> ComparisonAnswerer:
    # Separate provider so the change-detection path is typed against its own
    # Protocol and independently overridable in tests; same client underneath.
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise HTTPException(status_code=503, detail="QA unavailable: ANTHROPIC_API_KEY is not set")
    return ClaudeQaAnswerer(api_key=settings.anthropic_api_key)


class SectionWarningResponse(BaseModel):
    """HTTP mirror of SectionPlausibilityWarning — flag-not-block, so it rides
    on a successful (200, ingested) response like QaResponse.warnings does."""

    section: str
    section_chars: int
    document_chars: int
    fraction: float
    checks: list[str]


class ResearchResponse(BaseModel):
    ticker: str
    status: Literal["ingested", "skipped"]
    company_id: int
    filing_id: int
    accession_number: str
    filing_url: str
    summaries: dict[str, str]
    thesis_snapshot_id: int | None
    section_warnings: list[SectionWarningResponse] = []


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
    # ADR-0009 §7: change detection is explicitly requested, never inferred
    # from question text. Default off = today's ADR-0007 path, unchanged.
    compare: bool = False


class QaWarningResponse(BaseModel):
    # Mirrors QaWarning.kind. missing_period_citation is emitted only by the
    # (not yet endpoint-wired) change-detection path; listing it here keeps the
    # HTTP mirror in lockstep with the shared warning type.
    kind: Literal[
        "uncited_claim", "unknown_citation", "reasoning_artifact", "missing_period_citation"
    ]
    message: str


class QaResponse(BaseModel):
    """HTTP mirror of QaResult. `answer` reuses QaAnswer verbatim, so a verdict or
    recommendation stays structurally unrepresentable (ADR-0007 §3); `citations`
    carry each chunk's source_url so the audit chain crosses the HTTP boundary."""

    answer: QaAnswer
    citations: dict[str, SearchResult]
    warnings: list[QaWarningResponse]


class ComparisonResponse(BaseModel):
    """HTTP mirror of ComparisonResult (ADR-0009). Its own response shape behind
    the explicit compare flag — period_comparison is never a field on QaResponse,
    so flag-off callers receive today's ADR-0007 shape unchanged."""

    period_comparison: list[ChangeEntry]
    citations: dict[str, SearchResult]
    warnings: list[QaWarningResponse]
    explanation: str


def _run_comparison(
    request: QaRequest, engine: Engine, embedder: Embedder, answerer: ComparisonAnswerer
) -> ComparisonResponse:
    if request.ticker is None or not request.ticker.strip():
        raise HTTPException(status_code=422, detail="ticker is required when compare is true")
    if request.section is not None:
        raise HTTPException(
            status_code=422, detail="section filter is not supported when compare is true"
        )
    with engine.connect() as conn:
        # ADR-0008 §3: the previous COMPARABLE filing is the previous filing OF
        # THE SAME form_type. The form_type filter is applied BEFORE indexing —
        # load-bearing: indexing an unfiltered list would pair a 10-K with a
        # 10-Q once quarterlies are ingested.
        comparable = Repository(conn).filings_for_company(request.ticker, form_type="10-K")
    if not comparable:
        raise HTTPException(status_code=404, detail=f"no research stored for {request.ticker!r}")
    if len(comparable) == 1:
        # Endpoint-level analog of the no_prior_period guard: an honest 200,
        # never a fabricated comparison and never a 500.
        return ComparisonResponse(
            period_comparison=[],
            citations={},
            warnings=[],
            explanation=(
                f"No prior comparable filing: only one 10-K is stored for"
                f" {request.ticker.upper()}, so there is no prior period to compare."
            ),
        )
    latest, previous_comparable = comparable[0], comparable[1]
    try:
        result = detect_changes(
            engine,
            embedder,
            answerer,
            request.question,
            [latest.filing_id, previous_comparable.filing_id],
            limit=min(request.limit, MAX_SEARCH_LIMIT),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (EmbeddingError, QaError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ComparisonResponse(
        period_comparison=result.period_comparison,
        citations={label: SearchResult(**vars(match)) for label, match in result.citations.items()},
        warnings=[QaWarningResponse(kind=w.kind, message=w.message) for w in result.warnings],
        explanation=result.explanation,
    )


# Route matching is sequential: POST /research/qa must be registered before
# POST /research/{ticker}, or the path-param route captures "qa" as a ticker.
@router.post("/research/qa", response_model=QaResponse | ComparisonResponse)
def qa(
    request: QaRequest,
    engine: Annotated[Engine, Depends(get_read_engine)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
    answerer: Annotated[QaAnswerer, Depends(get_qa_answerer)],
    comparison_answerer: Annotated[ComparisonAnswerer, Depends(get_comparison_answerer)],
) -> QaResponse | ComparisonResponse:
    if request.compare:
        return _run_comparison(request, engine, embedder, comparison_answerer)
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
    ticker: str,
    service: Annotated[ResearchService, Depends(get_research_service)],
    accession_number: Annotated[str | None, Query()] = None,
) -> ResearchResponse:
    # blank or whitespace-only accession_number means "no explicit target"
    target = accession_number.strip() or None if accession_number else None
    try:
        outcome = service.run(ticker, target)
    except (TickerNotFoundError, FilingNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (UnsupportedFilingTypeError, SectionExtractionError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (UpstreamError, SummarizationError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    fields = {k: v for k, v in vars(outcome).items() if k != "section_warnings"}
    return ResearchResponse(
        ticker=ticker.upper(),
        **fields,
        section_warnings=[SectionWarningResponse(**vars(w)) for w in outcome.section_warnings],
    )


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
