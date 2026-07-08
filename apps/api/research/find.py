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
from apps.api.research.rerank import (
    RERANK_CANDIDATE_LIMIT,
    RERANK_ENABLED,
    Scorer,
    rerank,
)

# ADR-0011 §3 structural caps. Each is a ceiling: find_companies clamps its
# parameters to these, so a caller can narrow a cap but never widen it past
# the named constant — raising recall means editing the constant on purpose
# (the explicit knob in ADR-0011's risk-accepted trade-off), never a runtime
# argument drifting upward.
#
# How many chunks the stage-1 wide search retrieves. This bounds the one
# global pgvector query — cheap indexed math, but bounded by design.
# 40 -> 80 after live validation (2026-07-07): text-heavy companies take 3+
# chunks each, so a 40-chunk pool held only ~12-15 distinct companies — the
# candidate cap couldn't see companies the wide search never retrieved.
# Raising CANDIDATE_N without this would be half a fix. Pure pgvector/HNSW
# math, near-free.
WIDE_SEARCH_LIMIT = 80
# How many candidate companies FIND returns (ADR-0011 §3 stage-1 bound).
# 10 -> 15 after live validation: the cap was hit on 6/6 broad thematic
# queries (every broad query was trimming), and confirmed misses HD (tariffs,
# -0.003 below rank 10) and GOOG (AI-risk, -0.016) sat just under the cut.
# Marginal cost per extra company is zero answer-model tokens (response size
# only), so this is a free recall win.
CANDIDATE_N = 15
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
    # Cross-encoder relevance of this passage to the query (ADR-0013): how well
    # the passage TEXT answers the query, a better estimator of the same
    # retrieval quantity `similarity` measures — never a company judgment. None
    # when reranking is disabled; `similarity` is never overwritten by it.
    rerank_score: float | None = None


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
    # best-first, capped at passages_per_company. "Best" is rerank relevance
    # when reranking is enabled (ADR-0013), else cosine similarity.
    passages: list[CitedPassage]


@dataclass(frozen=True)
class FindResult:
    query: str
    # ordered by the best passage's rerank relevance when reranking is enabled
    # (ADR-0013 §4), else by match_strength — strongest first either way.
    matches: list[CompanyMatch]


@dataclass(frozen=True)
class _PooledPassage:
    """Internal: one candidate passage carrying its owning ticker through the
    rerank step, so grouping the reranked pool back by company is exact. It is
    Rerankable (`snippet` + `rerank_score`); the reranker stamps `rerank_score`
    and preserves every other field, so `ticker` and `similarity` survive."""

    ticker: str
    snippet: str
    source_url: str
    similarity: float
    rerank_score: float | None = None


def find_companies(
    engine: Engine,
    embedder: Embedder,
    query: str,
    *,
    wide_search_limit: int = WIDE_SEARCH_LIMIT,
    candidate_n: int = CANDIDATE_N,
    passages_per_company: int = PASSAGES_PER_COMPANY,
    rerank_enabled: bool | None = None,
    rerank_candidate_limit: int = RERANK_CANDIDATE_LIMIT,
    scorer: Scorer | None = None,
) -> FindResult:
    """ADR-0011 §2 stage 1: one global vector search over the whole corpus
    (the existing semantic_search path with no ticker filter — the
    already-paid-for pgvector work), grouped by company. A company matches
    if it has at least one chunk in the wide-search results. No stage-2
    re-retrieval and no answer-model — a query matching nothing returns an
    empty match list, never an error and never a generated explanation.

    Reranking is OPT-IN and OFF BY DEFAULT (ADR-0013 §5, amended after live
    validation: it costs ~3 s/query, so FIND's cheap path stays cheap unless a
    caller asks). `rerank_enabled=None` resolves to the module default
    `RERANK_ENABLED` (False); pass `rerank_enabled=True` to opt in. When on, the
    candidate pool is reordered by a local cross-encoder's query-relevance
    before the per-company cap: passages within a company are ordered by rerank
    relevance (the on-topic passage represents the company and survives the cap)
    and companies are ordered by their best passage's rerank relevance (an
    off-topic-best company falls). The candidate SET is unchanged — reranking
    reorders passages, it never re-runs retrieval — and reranking makes no
    answer-model call (`scorer` is a local model, injected in tests). Opting in
    without the `[rerank]` extra installed raises a clear RerankUnavailableError
    (ADR-0013 §5), never a silent cosine swap.
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
    # similarity — the stage-1 candidate ordering — and each group's chunks
    # are cosine-best-first.
    by_ticker: dict[str, list[ChunkMatch]] = {}
    for chunk in sorted(chunks, key=lambda c: c.distance):
        by_ticker.setdefault(chunk.ticker, []).append(chunk)
    candidates = list(by_ticker.items())[: min(candidate_n, CANDIDATE_N)]
    if not candidates:
        return FindResult(query=query, matches=[])
    # Flatten the candidate companies' chunks into one pool. The candidate SET
    # is fixed above (stage-1 cosine selection, ADR-0011 §2–3); the reranker
    # only reorders these passages — it never re-runs retrieval or widens the
    # list, so a company that never made the candidate cut cannot be rescued
    # here (the recall knob stays CANDIDATE_N / WIDE_SEARCH_LIMIT, ADR-0013 §3).
    pool = [
        _PooledPassage(
            ticker=ticker,
            snippet=chunk.content,
            source_url=chunk.source_url,
            # pgvector cosine distance; unit-normalized embeddings keep it in
            # [0, 1], so similarity lands in [0, 1] too. Preserved as the
            # retrieval fact even after reranking (ADR-0013 §4).
            similarity=1.0 - chunk.distance,
        )
        for ticker, company_chunks in candidates
        for chunk in company_chunks
    ]
    # ADR-0013 §4/§5: rerank the whole candidate pool by query-relevance when
    # enabled. Grouping the globally rerank-sorted pool by first appearance
    # then orders companies by their best passage's rerank_score (cross-set
    # effect) and each company's passages by rerank_score (within-company
    # effect). Disabled → the pool keeps cosine order and FIND behaves exactly
    # as it did before rerank landed, every rerank_score None.
    enabled = RERANK_ENABLED if rerank_enabled is None else rerank_enabled
    if enabled:
        pool = rerank(
            query,
            pool,
            scorer=scorer,
            limit=min(rerank_candidate_limit, RERANK_CANDIDATE_LIMIT),
        )
    regrouped: dict[str, list[_PooledPassage]] = {}
    for passage in pool:
        regrouped.setdefault(passage.ticker, []).append(passage)
    # Second connection on purpose: semantic_search owns its own, and the
    # name lookup is a separate read, not part of the vector query.
    with engine.connect() as conn:
        names = Repository(conn).company_names([ticker for ticker, _ in candidates])
    cap = min(passages_per_company, PASSAGES_PER_COMPANY)
    matches = []
    for ticker, company_passages in regrouped.items():
        # match_strength stays the MAX COSINE over the company's candidate
        # chunks — the stage-1 retrieval fact (ADR-0011 §5), preserved untouched
        # by reranking (ADR-0013 §4). Company order follows rerank relevance
        # (the insertion order above); match_strength keeps the original
        # retrieval fact auditable regardless of that order.
        match_strength = max(p.similarity for p in company_passages)
        passages = [
            CitedPassage(
                snippet=p.snippet,
                source_url=p.source_url,
                similarity=p.similarity,
                rerank_score=p.rerank_score,
            )
            for p in company_passages[:cap]
        ]
        matches.append(
            CompanyMatch(
                ticker=ticker,
                # Every ticker here came through the chunks→filings→companies
                # join, so a miss is structurally impossible; the fallback
                # keeps a hypothetical inconsistency from 500ing a read path.
                company_name=names.get(ticker, ticker),
                match_strength=match_strength,
                passages=passages,
            )
        )
    return FindResult(query=query, matches=matches)
