"""COMPARE mode: cited side-by-side over a named set, no ranking (ADR-0012).

The first cross-company mode that spends answer-model tokens — bounded by
structure, not policy. The load-bearing properties, held as structure:

- **The wall seam** (ADR-0012 #6a): per-column synthesis is one call behind
  `synthesize_column(engine, embedder, answerer, filing_id=…, query=…)`. The
  seam performs its own single-filing scoped retrieval (the ADR-0009 §2
  machinery); it has NO passage parameter, so another company's evidence —
  or another period's — has no path into a call. Cross-company ranking is
  unrepresentable the way a generated answer is unrepresentable in FIND.
- **Model-free assembly, no re-entry**: `compare_companies` assembles entries
  mechanically. The answerer's only input type is retrieved evidence
  (`dict[str, ChunkMatch]`); a `ColumnDraft` is accepted nowhere, so model
  output cannot re-enter a model call — there is no "summarize the columns"
  sixth call.
- **Bounded spend**: ≤ MAX_COMPARE_COMPANIES columns × ≤
  PASSAGES_PER_COMPANY_COMPARE passages, min()-clamped ceilings like FIND's.

MOCKED BUILD (ADR-0012 build sequencing): the shipped answerer is
`MockColumnAnswerer` — deterministic, zero live spend. THE single live-swap
point is `get_column_answerer()` in router.py; nothing else changes for the
live build, which happens only after this build is reviewed and then
live-validated twice.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel, Field
from sqlalchemy import Engine

from apps.api.research.embeddings import Embedder
from apps.api.research.repository import ChunkMatch, PinnedFiling, Repository

# ADR-0012 #1: the company cap, applied to the CIK-deduplicated named set —
# over-cap is REFUSED before any retrieval or answer-model work, never
# truncated (restating ADR-0011 §3 as a build commitment).
MAX_COMPARE_COMPANIES = 5
# ADR-0012 #3: stage-2 passages fed to synthesis per company. 2 is the
# starting constant, earned by live validation (known-rich specimen +
# wider-retrieval diff), tuned at the constant only — never a runtime
# argument drifting upward. min()-clamped like FIND's caps.
PASSAGES_PER_COMPANY_COMPARE = 2
# How many chunks of the pinned filing the coverage scan reads to count
# qualifying passages. Bounds the one scoped vector query; when the scan
# saturates, `qualifying` is a floor, not an exact count.
COVERAGE_SCAN_LIMIT = 24
# Similarity floor a passage must clear to count as qualifying (and to be
# consulted). UNVALIDATED for stage-2 per-filing distributions (build review
# finding #1): the only calibration datum — HD's genuine tariff match at
# 0.2397 — came from FIND's stage-1 GLOBAL ranking, a different distribution.
# The floor sits in the coverage display's denominator and gates the model
# call, so it is a first-rank live-validate item alongside the passage cap:
# the k=6-8 omission diff must check whether omitted-material passages score
# below it, and the specimen set must include a weak-but-real match (the
# HD-tariff class) to catch a false below-floor "silence". Tuned at the
# constant only, with before/after evidence, per the live-validation
# discipline.
QUALIFYING_SIMILARITY_FLOOR = 0.20


class CompareRefusal(Exception):
    """Request-layer refusal (ADR-0012 #1): raised before any retrieval or
    answer-model work runs. An over-cap request never produces a result."""


class ColumnClaim(BaseModel):
    """One drafted statement, citing the chunk label(s) it rests on."""

    text: str
    chunk_ids: list[str] = Field(default_factory=list)


class ColumnDraft(BaseModel):
    """Model-facing draft: claims cite chunk LABELS only (C1, C2, …) — no
    URLs, no dates. Provenance is stamped mechanically at resolve time (the
    draft/resolved split, ADR-0012 #4). Deliberately has no cross-company or
    ranking field: one draft speaks for one filing."""

    claims: list[ColumnClaim] = Field(default_factory=list)


class ColumnAnswerer(Protocol):
    """The one seam that touches an answer model. Input is retrieved evidence
    only; ColumnDraft is not a legal input anywhere in this module (no
    re-entry, ADR-0012 #6a)."""

    def draft_column(self, query: str, chunks: dict[str, ChunkMatch]) -> ColumnDraft: ...


class MockColumnAnswerer:
    """MOCKED answer model (ADR-0012 mocked build) — deterministic and free:
    one claim per supplied passage, citing exactly that passage's label, so
    the full path (dedup, refusal, pinning, filing-scoped retrieval, entries
    assembly, coverage, citation binding) runs and is reviewable with zero
    live spend. THE single swap point for the live model is
    get_column_answerer() in router.py — replace the mock there and nothing
    else changes."""

    def draft_column(self, query: str, chunks: dict[str, ChunkMatch]) -> ColumnDraft:
        return ColumnDraft(
            claims=[
                ColumnClaim(text=f"[MOCK] The filing states: {match.content}", chunk_ids=[label])
                for label, match in chunks.items()
            ]
        )


@dataclass(frozen=True)
class CitedStatement:
    """One stated claim in a column, provenance stamped from the database —
    never model-copied (ADR-0012 #4)."""

    text: str
    source_url: str


@dataclass(frozen=True)
class ConsultedPassage:
    """One passage the answer model saw — attached to a model_declined or
    claims_uncited no_finding so the decline (or the suppression) is
    auditable like any claim (ADR-0012 #5)."""

    label: str
    snippet: str
    source_url: str
    similarity: float


@dataclass(frozen=True)
class Coverage:
    """Mechanical coverage signal (ADR-0012 #3): a retrieval fact, zero
    answer-model tokens, §5-safe like FIND's match_strength. Converts a
    starved-but-fluent column from invisible under-coverage into
    self-reporting ("2 of 9 qualifying passages consulted").

    scanned and floor make the display floor-legible (build review finding
    #1): "2 of 2 qualifying" cannot silently mean "above an uncalibrated
    threshold" when the response also says 24 chunks were scanned against a
    0.20 floor. When scanned == COVERAGE_SCAN_LIMIT the qualifying count is
    a lower bound, not an exact count."""

    qualifying: int  # passages in the pinned filing at/above the floor
    consulted: int  # of those, how many the answer model saw
    scanned: int  # chunks the coverage scan read from the pinned filing
    floor: float  # the similarity floor in force (QUALIFYING_SIMILARITY_FLOOR)


@dataclass(frozen=True)
class ColumnSynthesis:
    """Outcome of one seam call. `column` carries statements; the empty
    outcomes are ADR-0012 #5's split, three-way per the build review:
    no_embedded_evidence and below_floor are mechanical facts (the model is
    never consulted — corpus state and a low-confidence retrieval state,
    respectively, neither of which is filing silence), while the last two
    are audited model outcomes (consulted passages attached): model_declined
    means the model emitted no claims at all, claims_uncited means it
    asserted claims that all failed citation binding and were suppressed —
    a model that hallucinated is not a model that declined."""

    outcome: Literal[
        "column", "no_embedded_evidence", "below_floor", "model_declined", "claims_uncited"
    ]
    coverage: Coverage
    statements: list[CitedStatement]
    consulted: list[ConsultedPassage]
    warnings: list[str]


@dataclass(frozen=True)
class CompareEntry:
    """One typed entry in the ordered response list (ADR-0012 #2). The kind
    tag is what keeps failures, honest no-findings, and columns unconfusable
    — by construction, not by payload location."""

    kind: Literal["column", "no_finding", "unresolved", "no_evidence"]
    symbol: str  # the caller's (first-named) symbol
    company_name: str | None = None
    cik: str | None = None
    filing: PinnedFiling | None = None  # column / no_finding: what the column speaks for
    statements: list[CitedStatement] = field(default_factory=list)
    coverage: Coverage | None = None
    no_finding_cause: (
        Literal["no_embedded_evidence", "below_floor", "model_declined", "claims_uncited"] | None
    ) = None
    consulted_passages: list[ConsultedPassage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CompareResult:
    query: str
    # Exactly one entry per deduplicated asked-for name, in caller order —
    # a renderer cannot draw the columns without iterating past the failures
    # (ADR-0012 #2: prominence is structural, there is no side list).
    entries: list[CompareEntry]
    partial: bool  # True when any entry is unresolved / no_evidence


def synthesize_column(
    engine: Engine,
    embedder: Embedder,
    answerer: ColumnAnswerer,
    *,
    filing_id: int,
    query: str,
    passages_per_company: int = PASSAGES_PER_COMPANY_COMPARE,
) -> ColumnSynthesis:
    """The wall seam (ADR-0012 #6a): one filing, one call, own retrieval.

    Takes (filing_id, query) and performs its own single-filing scoped
    retrieval via the ADR-0009 §2 machinery — deliberately NOT the
    ticker-scoped search path, which blends periods, and deliberately with no
    passage parameter, so foreign evidence has no entry path. The signature
    is the structural assertion; tests pin it.
    """
    if not query.strip():
        raise ValueError("query must not be blank")
    if passages_per_company < 1:
        raise ValueError("passages_per_company must be positive")
    query_embedding = embedder.embed_query(query)
    with engine.connect() as conn:
        scanned = Repository(conn).search_chunks_in_filing(
            query_embedding,
            model=embedder.model,
            filing_id=filing_id,
            limit=COVERAGE_SCAN_LIMIT,
        )
    if not scanned:
        # Corpus state, not filing content: the pinned filing has no embedded
        # chunks (embeddings backfill pending, or an extraction gap). Calling
        # this "the filing is silent here" would present corpus state as
        # filing content — silent wrongness. The model is never consulted.
        return ColumnSynthesis(
            outcome="no_embedded_evidence",
            coverage=Coverage(
                qualifying=0, consulted=0, scanned=0, floor=QUALIFYING_SIMILARITY_FLOOR
            ),
            statements=[],
            consulted=[],
            warnings=[],
        )
    # scanned is best-first by cosine distance, so slicing keeps the best.
    qualifying = [c for c in scanned if 1.0 - c.distance >= QUALIFYING_SIMILARITY_FLOOR]
    if not qualifying:
        # Chunks exist but none cleared the floor: a low-confidence retrieval
        # state — not filing silence, and not a model judgment (the model is
        # never consulted). A mechanical fact either way (ADR-0012 #5).
        return ColumnSynthesis(
            outcome="below_floor",
            coverage=Coverage(
                qualifying=0,
                consulted=0,
                scanned=len(scanned),
                floor=QUALIFYING_SIMILARITY_FLOOR,
            ),
            statements=[],
            consulted=[],
            warnings=[],
        )
    labeled = {
        f"C{i + 1}": chunk
        for i, chunk in enumerate(
            qualifying[: min(passages_per_company, PASSAGES_PER_COMPANY_COMPARE)]
        )
    }
    coverage = Coverage(
        qualifying=len(qualifying),
        consulted=len(labeled),
        scanned=len(scanned),
        floor=QUALIFYING_SIMILARITY_FLOOR,
    )
    consulted = [
        ConsultedPassage(
            label=label,
            snippet=chunk.content,
            source_url=chunk.source_url,
            similarity=1.0 - chunk.distance,
        )
        for label, chunk in labeled.items()
    ]
    draft = answerer.draft_column(query, labeled)
    # Resolve (the draft/resolved split, ADR-0012 #4): the draft cited labels;
    # provenance is stamped here from the retrieved chunks — database facts,
    # never model-copied. A claim that cannot cite its passage is not emitted;
    # the drop is reported, never swallowed.
    statements: list[CitedStatement] = []
    warnings: list[str] = []
    for claim in draft.claims:
        if not claim.chunk_ids:
            warnings.append(f"dropped uncited claim: {claim.text!r}")
            continue
        unknown = [chunk_id for chunk_id in claim.chunk_ids if chunk_id not in labeled]
        if unknown:
            warnings.append(f"dropped claim citing unknown chunk(s) {unknown}: {claim.text!r}")
            continue
        statements.append(
            CitedStatement(text=claim.text, source_url=labeled[claim.chunk_ids[0]].source_url)
        )
    if statements:
        return ColumnSynthesis(
            outcome="column",
            coverage=coverage,
            statements=statements,
            consulted=consulted,
            warnings=warnings,
        )
    return ColumnSynthesis(
        # An empty draft is a decline; a draft whose every claim failed
        # citation binding is suppressed assertion — different facts, and the
        # warnings above name each drop (build review finding #4).
        outcome="claims_uncited" if draft.claims else "model_declined",
        coverage=coverage,
        statements=[],
        consulted=consulted,
        warnings=warnings,
    )


def compare_companies(
    engine: Engine,
    embedder: Embedder,
    answerer: ColumnAnswerer,
    tickers: Sequence[str],
    query: str,
    *,
    passages_per_company: int = PASSAGES_PER_COMPANY_COMPARE,
) -> CompareResult:
    """COMPARE over an explicitly named ticker set (ADR-0012 #1-#5).

    Order of operations is the contract: normalize and dedup symbols, resolve
    against the sec_ticker_reference cache (the ONE step permitted before the
    cap check, because CIK dedup needs it — a cheap reference read, no
    evidence touched), dedup by CIK, REFUSE over-cap, and only then pin
    filings and synthesize per column. Assembly below the seam is mechanical:
    no model call sees more than one filing, and no model output re-enters a
    model call.
    """
    if not query.strip():
        raise ValueError("query must not be blank")
    if passages_per_company < 1:
        raise ValueError("passages_per_company must be positive")
    symbols = list(dict.fromkeys(t.strip().upper() for t in tickers if t.strip()))
    if not symbols:
        raise ValueError("tickers must name at least one company")

    with engine.connect() as conn:
        repo = Repository(conn)
        # Resolution + CIK dedup (ADR-0012 #1): two tickers on one CIK are one
        # company — one entry, labeled with the first-named symbol.
        named: list[tuple[str, str | None, str | None]] = []  # (symbol, cik, company_name)
        seen_ciks: set[str] = set()
        for symbol in symbols:
            reference = repo.resolve_ticker_from_reference(symbol)
            if reference is None:
                named.append((symbol, None, None))
            elif reference.cik not in seen_ciks:
                seen_ciks.add(reference.cik)
                named.append((symbol, reference.cik, reference.company_name))
        if len(named) > MAX_COMPARE_COMPANIES:
            raise CompareRefusal(
                f"COMPARE accepts at most {MAX_COMPARE_COMPANIES} companies;"
                f" got {len(named)} distinct names."
                f" Name a set of at most {MAX_COMPARE_COMPANIES} tickers."
            )
        # Pin AFTER the cap check: metadata reads only, still zero retrieval.
        pinned = {
            symbol: repo.latest_annual_filing(cik) if cik is not None else None
            for symbol, cik, _ in named
        }

    entries: list[CompareEntry] = []
    for symbol, cik, company_name in named:
        if cik is None:
            entries.append(CompareEntry(kind="unresolved", symbol=symbol))
            continue
        filing = pinned[symbol]
        if filing is None:
            entries.append(
                CompareEntry(kind="no_evidence", symbol=symbol, company_name=company_name, cik=cik)
            )
            continue
        synthesis = synthesize_column(
            engine,
            embedder,
            answerer,
            filing_id=filing.filing_id,
            query=query,
            passages_per_company=passages_per_company,
        )
        entries.append(
            CompareEntry(
                kind="column" if synthesis.outcome == "column" else "no_finding",
                symbol=symbol,
                company_name=filing.company_name,
                cik=cik,
                filing=filing,
                statements=synthesis.statements,
                coverage=synthesis.coverage,
                no_finding_cause=None if synthesis.outcome == "column" else synthesis.outcome,
                # Attached on the audited model outcomes so the decline (or
                # the suppression) is checkable (ADR-0012 #5); a cited column
                # audits through its citations.
                consulted_passages=(
                    synthesis.consulted
                    if synthesis.outcome in ("model_declined", "claims_uncited")
                    else []
                ),
                warnings=synthesis.warnings,
            )
        )
    # The ADR-0012 #2 invariant, also asserted in tests: nothing dropped,
    # nothing relegated to a side list.
    assert len(entries) == len(named)
    return CompareResult(
        query=query,
        entries=entries,
        partial=any(entry.kind in ("unresolved", "no_evidence") for entry in entries),
    )
