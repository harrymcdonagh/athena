"""FIND mode: cross-company retrieval (ADR-0011 §1, first increment).

Answers "which of my companies match X" as a RETRIEVAL result — matching
companies with their citing passages — with ZERO answer-model calls. The
zero-token path is the contract, not an optimization: this module has no
answerer import and no answerer parameter, so a generated answer is
structurally unrepresentable here. Synthesis lives in the future COMPARE
increment, not in FIND.
"""

from dataclasses import dataclass

from sqlalchemy import Engine

from apps.api.research.embeddings import Embedder, semantic_search
from apps.api.research.repository import ChunkMatch, Repository

# ADR-0011 §3 structural caps. Each is a ceiling: find_companies clamps its
# parameters to these, so a caller can narrow a cap but never widen it past
# the named constant — raising recall means editing the constant on purpose
# (the explicit knob in ADR-0011's risk-accepted trade-off), never a runtime
# argument drifting upward.
#
# How many chunks the stage-1 wide search retrieves. This bounds the one
# global pgvector query — cheap indexed math, but bounded by design.
WIDE_SEARCH_LIMIT = 40
# How many candidate companies FIND returns (ADR-0011 §3 stage-1 bound).
CANDIDATE_N = 10
# How many citing passages each returned company carries (§3 per-company
# evidence bound).
PASSAGES_PER_COMPANY = 3


@dataclass(frozen=True)
class CitedPassage:
    """One citing passage: the chunk text and its filing source_url
    (ADR-0011 §4 — every match cited)."""

    snippet: str
    source_url: str
    similarity: float  # cosine similarity of this passage to the query


@dataclass(frozen=True)
class CompanyMatch:
    """One matched company. match_strength is the cosine similarity of the
    company's best-matching passage — a retrieval fact (how well the filing
    TEXT matched the query), explicitly NOT a judgment about the company
    (ADR-0011 §5): it ranks text matches, never exposure, quality, or
    desirability."""

    ticker: str
    company_name: str
    match_strength: float
    passages: list[CitedPassage]  # best-first, capped at passages_per_company


@dataclass(frozen=True)
class FindResult:
    query: str
    matches: list[CompanyMatch]  # ordered by match_strength, strongest first


def find_companies(
    engine: Engine,
    embedder: Embedder,
    query: str,
    *,
    wide_search_limit: int = WIDE_SEARCH_LIMIT,
    candidate_n: int = CANDIDATE_N,
    passages_per_company: int = PASSAGES_PER_COMPANY,
) -> FindResult:
    """ADR-0011 §2 stage 1 only: one global vector search over the whole
    corpus (the existing semantic_search path with no ticker filter — the
    already-paid-for pgvector work), grouped by company. A company matches
    if it has at least one chunk in the wide-search results. No stage-2
    re-retrieval and no answer-model — a query matching nothing returns an
    empty match list, never an error and never a generated explanation.
    """
    if min(wide_search_limit, candidate_n, passages_per_company) < 1:
        raise ValueError(
            "wide_search_limit, candidate_n, and passages_per_company must be positive"
        )
    chunks = semantic_search(
        engine, embedder, query, limit=min(wide_search_limit, WIDE_SEARCH_LIMIT)
    )
    # Sorted locally so correctness does not silently couple to the search's
    # ORDER BY (a stable no-op today). Dicts preserve insertion order, so
    # grouping by first appearance orders companies by their best chunk's
    # similarity — the match-strength ordering — and each group's passages
    # are best-first.
    by_ticker: dict[str, list[ChunkMatch]] = {}
    for chunk in sorted(chunks, key=lambda c: c.distance):
        by_ticker.setdefault(chunk.ticker, []).append(chunk)
    candidates = list(by_ticker.items())[: min(candidate_n, CANDIDATE_N)]
    if not candidates:
        return FindResult(query=query, matches=[])
    # Second connection on purpose: semantic_search owns its own, and the
    # name lookup is a separate read, not part of the vector query.
    with engine.connect() as conn:
        names = Repository(conn).company_names([ticker for ticker, _ in candidates])
    matches = []
    for ticker, company_chunks in candidates:
        passages = [
            CitedPassage(
                snippet=chunk.content,
                source_url=chunk.source_url,
                # pgvector cosine distance; unit-normalized embeddings keep
                # it in [0, 1], so similarity lands in [0, 1] too.
                similarity=1.0 - chunk.distance,
            )
            for chunk in company_chunks[: min(passages_per_company, PASSAGES_PER_COMPANY)]
        ]
        matches.append(
            CompanyMatch(
                ticker=ticker,
                # Every ticker here came through the chunks→filings→companies
                # join, so a miss is structurally impossible; the fallback
                # keeps a hypothetical inconsistency from 500ing a read path.
                company_name=names.get(ticker, ticker),
                match_strength=passages[0].similarity,
                passages=passages,
            )
        )
    return FindResult(query=query, matches=matches)
