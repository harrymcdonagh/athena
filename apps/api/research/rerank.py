"""Cross-encoder rerank of FIND candidate passages (ADR-0013).

Reorders retrieved passages by joint (query, passage) relevance using a local
cross-encoder, correcting the bi-encoder failure mode where a chunk scores a
high cosine similarity while being off-topic for the ACTUAL query (validated on
SCHW/cyber and ORCL/AI — a cybersecurity passage that only mentions AI in
passing; ADR-0013 Consequences). Off by default, opt-in per request.

This is a RETRIEVAL operation, on the evidence side of the wall (ADR-0007 §3,
ADR-0013 §4). `rerank_score` measures how well a passage's filing TEXT answers
the query — the same category of fact as cosine similarity, a better estimator
of the same quantity — never a judgment about a company. Reordering passages by
query-relevance, and letting a company fall because its text is off-topic for
THIS query, is not ranking companies by attractiveness: the original retrieval
fact (`match_strength`, `similarity`) is preserved untouched and stays
auditable.

Zero-answer-model contract (ADR-0011 §1) is structurally intact: this module
imports no answerer, takes no answerer parameter, and makes NO network call at
inference — the cross-encoder runs locally on CPU. The heavy dependency
(`sentence-transformers` → `torch`) is imported lazily inside `_default_scorer`
ONLY, never at module import, so importing this module (and the test suite that
does) never pays the torch tax.
"""

from __future__ import annotations

import dataclasses
import math
from typing import Any, ClassVar, Protocol


class RerankUnavailableError(RuntimeError):
    """Raised when reranking is requested but its optional dependency is not
    installed. Carries an actionable message (install the `[rerank]` extra)
    instead of surfacing a bare ImportError — ADR-0013 §5: opting in without the
    extra fails clearly, never a silent cosine swap."""


# ADR-0013 §5 config. Reranking is behind a flag, OFF BY DEFAULT and opt-in per
# request (amended after live validation: it costs ~3 s/query, so FIND's cheap,
# frequent path stays cheap unless a caller explicitly asks). When off, FIND
# keeps its raw cosine ordering and every rerank_score stays None; no
# cross-encoder is loaded. Opting in requires the [rerank] extra — there is no
# silent cosine fallback when it is absent (see _default_scorer).
RERANK_ENABLED = False
# ADR-0013 §2: a small, local, CPU-viable cross-encoder — Apache-2.0, trained on
# MS MARCO passage ranking, ~80 MB. Named here so swapping the model is a
# deliberate one-line edit (the ADR-0011 §3 knob posture), never a literal
# buried at the call site.
RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
# ADR-0013 §5 ceiling on how many passages are scored per query, min()-clamped
# against the actual candidate pool like every other FIND cap — scoring more
# passages than the candidate set holds is meaningless, so it is bounded by
# construction. Set to cover the whole candidate pool in the default config:
# that pool is a subset of the stage-1 wide-search results, so it can never
# exceed WIDE_SEARCH_LIMIT (80). Keeping the ceiling at that size means the
# full pool is always scored — no on-topic passage at a deep wide-rank goes
# unscored — while still bounding model work at tens of (query, passage) pairs.
RERANK_CANDIDATE_LIMIT = 80


class Rerankable(Protocol):
    """A passage the reranker can score and stamp. Any frozen dataclass with a
    text `snippet` and a `rerank_score` field qualifies; the reranker sets
    `rerank_score` via `dataclasses.replace` and touches no other field, so a
    passage's `similarity` / cosine retrieval fact is preserved.

    `snippet` / `rerank_score` are read-only properties on purpose: FIND's
    passages are FROZEN dataclasses (read-only attributes), which a settable
    Protocol member would reject."""

    @property
    def snippet(self) -> str: ...
    @property
    def rerank_score(self) -> float | None: ...

    # Present on every dataclass instance; declared with the same shape the
    # stdlib synthesizes so a concrete dataclass satisfies this Protocol AND a
    # `T: Rerankable` type parameter satisfies dataclasses.replace's
    # DataclassInstance requirement.
    __dataclass_fields__: ClassVar[dict[str, dataclasses.Field[Any]]]


class Scorer(Protocol):
    """Scores each (query, passage_text) pair jointly, returning one float per
    text in order. The production implementation is the local cross-encoder
    (`_default_scorer`); tests pass a deterministic stub so the suite never
    imports torch (ADR-0013 Consequences)."""

    def __call__(self, query: str, texts: list[str]) -> list[float]: ...


# Process-level singleton: the cross-encoder is loaded once (downloaded ~80 MB
# on first use) and reused across queries.
_scorer_singleton: Scorer | None = None


def rerank[T: Rerankable](
    query: str,
    candidates: list[T],
    *,
    scorer: Scorer | None = None,
    limit: int = RERANK_CANDIDATE_LIMIT,
) -> list[T]:
    """Reorder `candidates` by cross-encoder relevance to `query`, descending.

    Pure and deterministic given (query, candidates, scorer): it calls the
    scorer once, stamps each scored candidate's `rerank_score` via
    `dataclasses.replace` (never mutating an input, never touching any other
    field — `similarity` / `match_strength` are preserved), and returns a NEW
    list sorted by `rerank_score`. Ties keep their incoming order (stable sort),
    so the result is deterministic.

    At most `min(limit, RERANK_CANDIDATE_LIMIT, len(candidates))` passages are
    scored (the ADR-0013 §5 bound). In the normal FIND path the candidate pool
    is well under the bound, so every passage is scored; if the bound ever
    clips, the unscored remainder trails the scored passages in its incoming
    order with `rerank_score` left None — an explicit, visible degradation, not
    a silent drop.

    A non-finite score (NaN/inf) RAISES rather than silently corrupting the
    order: NaN comparisons break the sort's total order and would yield an
    undefined, non-deterministic ordering — honest failure over silent
    wrongness.
    """
    if not candidates:
        return []
    if scorer is None:
        scorer = _default_scorer()
    bound = min(limit, RERANK_CANDIDATE_LIMIT, len(candidates))
    to_score = candidates[:bound]
    remainder = candidates[bound:]
    scores = scorer(query, [c.snippet for c in to_score])
    if len(scores) != len(to_score):
        raise ValueError(f"scorer returned {len(scores)} scores for {len(to_score)} passages")
    if any(not math.isfinite(s) for s in scores):
        raise ValueError(f"scorer returned a non-finite (NaN/inf) score for query {query!r}")
    scored = [
        dataclasses.replace(c, rerank_score=float(s)) for c, s in zip(to_score, scores, strict=True)
    ]
    scored.sort(key=_rerank_key, reverse=True)
    return scored + list(remainder)


def _rerank_key(candidate: Rerankable) -> float:
    # Every candidate in the sorted slice has just been stamped, so this is
    # never None; the guard keeps mypy honest and the sort total.
    return candidate.rerank_score if candidate.rerank_score is not None else float("-inf")


def _default_scorer() -> Scorer:
    """Build (once) the local cross-encoder scorer.

    Imports `sentence-transformers` (and `torch`) ONLY here, on first real use —
    never at module import — so FIND import and the test suite never pay the
    torch tax (ADR-0013 Consequences). This path is reached only on an explicit
    opt-in (`rerank_enabled=True`) with no injected scorer; reranking is off by
    default and tests inject a stub, so the suite never lands here. If the
    `[rerank]` extra is not installed, raise a clear RerankUnavailableError
    naming the extra — never a bare ImportError, never a silent cosine swap
    (ADR-0013 §5).
    """
    global _scorer_singleton
    if _scorer_singleton is None:
        try:
            from sentence_transformers import CrossEncoder  # lazy: heavy dep (torch)
        except ImportError as exc:
            raise RerankUnavailableError(
                "reranking was requested but the optional cross-encoder dependency "
                "is not installed. Install it with: pip install -e '.[rerank]' "
                "(or disable reranking / omit rerank=true)."
            ) from exc

        model = CrossEncoder(RERANK_MODEL)

        def score(query: str, texts: list[str]) -> list[float]:
            if not texts:
                return []
            return [float(s) for s in model.predict([(query, t) for t in texts])]

        _scorer_singleton = score
    return _scorer_singleton
