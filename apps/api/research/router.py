from functools import lru_cache
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import Engine

from apps.api.config import get_settings
from apps.api.db import get_engine
from apps.api.edgar.client import EdgarClient, FilingNotFoundError, TickerNotFoundError
from apps.api.edgar.sections import SECTIONS, SectionExtractionError
from apps.api.research.compare import (
    ColumnAnswerer,
    CompareRefusal,
    MockColumnAnswerer,
    compare_companies,
)
from apps.api.research.embeddings import (
    Embedder,
    EmbeddingError,
    VoyageEmbedder,
    semantic_search,
)
from apps.api.research.find import find_companies
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


@lru_cache
def get_column_answerer() -> ColumnAnswerer:
    # ADR-0012 mocked build: COMPARE ships with the deterministic mock, so the
    # whole path (dedup, refusal, pinning, filing-scoped retrieval, entries,
    # coverage, citation binding) runs with zero live answer-model spend.
    # THE single live-swap point: after this build is reviewed, return a
    # Claude-backed ColumnAnswerer here (API-key-gated like get_qa_answerer);
    # nothing else changes for the live build.
    return MockColumnAnswerer()


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


class FindPassageResponse(BaseModel):
    snippet: str
    source_url: str
    similarity: float


class FindCompanyMatchResponse(BaseModel):
    """One matched company. match_strength is how well the filing TEXT matched
    the query (best passage's cosine similarity) — a retrieval fact, never a
    judgment about the company (ADR-0011 §5)."""

    ticker: str
    company_name: str
    match_strength: float
    passages: list[FindPassageResponse]


class FindResponse(BaseModel):
    """HTTP mirror of FindResult (ADR-0011 §1): a retrieval result — matching
    companies with citing passages — not a generated answer. There is no text
    field a synthesis or ranking could ride in, and the endpoint takes no
    answerer dependency: the zero-answer-model contract is structural."""

    query: str
    matches: list[FindCompanyMatchResponse]


class CompareStatementResponse(BaseModel):
    text: str
    source_url: str


class ComparePassageResponse(BaseModel):
    label: str
    snippet: str
    source_url: str
    similarity: float


class CompareCoverageResponse(BaseModel):
    """Mechanical coverage signal (ADR-0012 #3): a retrieval fact, so a
    starved column self-reports ('2 of 9') instead of reading as complete.
    scanned + floor keep the display floor-legible: "2 of 2" always says how
    many chunks were read and what threshold defined "qualifying"."""

    qualifying: int
    consulted: int
    scanned: int
    floor: float


class ComparePinnedFilingResponse(BaseModel):
    form_type: str
    period_end_date: str
    filing_url: str


class CompareEntryResponse(BaseModel):
    """One typed entry in the single ordered list (ADR-0012 #2). Deliberately
    has no rank/score field and no cross-company text field: a ranking is
    structurally unrepresentable, like FIND's response."""

    kind: Literal["column", "no_finding", "unresolved", "no_evidence"]
    symbol: str
    company_name: str | None = None
    cik: str | None = None
    filing: ComparePinnedFilingResponse | None = None
    statements: list[CompareStatementResponse] = []
    coverage: CompareCoverageResponse | None = None
    no_finding_cause: Literal["no_embedded_evidence", "below_floor", "model_declined"] | None = None
    consulted_passages: list[ComparePassageResponse] = []
    warnings: list[str] = []


class CompareResponse(BaseModel):
    query: str
    partial: bool  # True when any asked-for name failed (unresolved/no_evidence)
    entries: list[CompareEntryResponse]  # one per deduplicated name, caller order


class CompanyListItem(BaseModel):
    ticker: str
    company_name: str
    filing_count: int
    latest_period_end_date: str
    # Compare needs two filings (ADR-0009); this tells the picker whether the
    # compare-across-years toggle is meaningful for the company.
    has_multiple_filings: bool


class CompaniesResponse(BaseModel):
    """The companies Athena holds ingested evidence for (`companies` table,
    never sec_ticker_reference — ADR-0010 #2): exactly the set the frontend
    picker may offer, since a reference-only ticker has nothing to research.
    Ticker-alphabetical — a plain roster, no judgment-flavored ordering."""

    companies: list[CompanyListItem]


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


@router.get("/research/companies", response_model=CompaniesResponse)
def list_companies(engine: Annotated[Engine, Depends(get_read_engine)]) -> CompaniesResponse:
    with engine.connect() as conn:
        listings = Repository(conn).list_companies()
    return CompaniesResponse(
        companies=[
            CompanyListItem(
                ticker=listing.ticker,
                company_name=listing.company_name,
                filing_count=listing.filing_count,
                latest_period_end_date=listing.latest_period_end_date.isoformat(),
                has_multiple_filings=listing.filing_count > 1,
            )
            for listing in listings
        ]
    )


@router.get("/research/find", response_model=FindResponse)
def find(
    q: str,
    engine: Annotated[Engine, Depends(get_read_engine)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
) -> FindResponse:
    # No answerer dependency on purpose (ADR-0011 §1): FIND composes its
    # result purely from retrieval output. Caps are the find module's
    # structural constants (ADR-0011 §3) — deliberately not request knobs.
    try:
        result = find_companies(engine, embedder, q)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except EmbeddingError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return FindResponse(
        query=result.query,
        matches=[
            FindCompanyMatchResponse(
                ticker=match.ticker,
                company_name=match.company_name,
                match_strength=match.match_strength,
                passages=[FindPassageResponse(**vars(p)) for p in match.passages],
            )
            for match in result.matches
        ],
    )


@router.get("/research/compare", response_model=CompareResponse)
def compare(
    q: str,
    tickers: Annotated[list[str], Query()],
    engine: Annotated[Engine, Depends(get_read_engine)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
    answerer: Annotated[ColumnAnswerer, Depends(get_column_answerer)],
) -> CompareResponse:
    # Order of operations is the compare module's contract (ADR-0012 #1):
    # resolve + CIK-dedup (cache reads only), refuse over 5, then pin and
    # synthesize per column. Caps are structural constants, not request knobs.
    try:
        result = compare_companies(engine, embedder, answerer, tickers, q)
    except CompareRefusal as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except EmbeddingError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return CompareResponse(
        query=result.query,
        partial=result.partial,
        entries=[
            CompareEntryResponse(
                kind=entry.kind,
                symbol=entry.symbol,
                company_name=entry.company_name,
                cik=entry.cik,
                filing=(
                    ComparePinnedFilingResponse(
                        form_type=entry.filing.form_type,
                        period_end_date=entry.filing.period_end_date.isoformat(),
                        filing_url=entry.filing.filing_url,
                    )
                    if entry.filing is not None
                    else None
                ),
                statements=[
                    CompareStatementResponse(**vars(statement)) for statement in entry.statements
                ],
                coverage=(
                    CompareCoverageResponse(**vars(entry.coverage))
                    if entry.coverage is not None
                    else None
                ),
                no_finding_cause=entry.no_finding_cause,
                consulted_passages=[
                    ComparePassageResponse(**vars(passage)) for passage in entry.consulted_passages
                ],
                warnings=entry.warnings,
            )
            for entry in result.entries
        ],
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
