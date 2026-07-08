"""Unit tests for the FIND cross-encoder reranker (ADR-0013).

Every test injects a deterministic STUB scorer — never the real cross-encoder —
so the suite never builds the model (ADR-0013 Consequences: the suite must not
pay the torch tax).

Note on the torch-laziness guard: `voyageai` (a base dependency, imported via
the embeddings module elsewhere in the app) opportunistically imports torch +
sentence-transformers WHEN THEY ARE INSTALLED. So a global `"torch" not in
sys.modules` check is confounded once the `[rerank]` extra is present — it would
fail on torch that voyageai, not the reranker, imported. The meaningful,
env-independent invariant is checked instead: importing ONLY the rerank module
stays lazy (subprocess), and the stub path never fires the model loader. In CI
(extra absent) nothing imports torch at all.
"""

import dataclasses
import os
import subprocess
import sys
from pathlib import Path

import pytest

import apps.api.research.rerank as rerank_module
from apps.api.research.rerank import (
    RERANK_CANDIDATE_LIMIT,
    RERANK_ENABLED,
    RERANK_MODEL,
    Scorer,
    rerank,
)


@dataclasses.dataclass(frozen=True)
class StubPassage:
    """A minimal Rerankable: `snippet` to score, `rerank_score` to stamp, and a
    `similarity` standing in for the preserved retrieval fact."""

    snippet: str
    similarity: float
    rerank_score: float | None = None


def keyword_scorer(keyword: str) -> Scorer:
    """Deterministic stub cross-encoder: 1.0 if the keyword is in the passage
    text, else 0.0. No network, no torch — a pure function of the inputs."""

    def score(query: str, texts: list[str]) -> list[float]:
        return [1.0 if keyword in text else 0.0 for text in texts]

    return score


def test_rerank_reorders_by_rerank_score_descending() -> None:
    passages = [
        StubPassage("off-topic filler", similarity=0.99),
        StubPassage("mentions cyber risk", similarity=0.40),
        StubPassage("more filler", similarity=0.80),
    ]

    result = rerank("cyber", passages, scorer=keyword_scorer("cyber"))

    # The only on-topic passage floats to the top despite the lowest cosine.
    assert [p.snippet for p in result] == [
        "mentions cyber risk",
        "off-topic filler",
        "more filler",
    ]
    assert result[0].rerank_score == 1.0
    assert result[1].rerank_score == 0.0


def test_rerank_preserves_other_fields_and_never_overwrites_similarity() -> None:
    passages = [
        StubPassage("high cosine but off-topic", similarity=0.99),
        StubPassage("on cyber topic", similarity=0.30),
    ]

    result = rerank("cyber", passages, scorer=keyword_scorer("cyber"))

    top = result[0]
    assert top.snippet == "on cyber topic"
    assert top.similarity == 0.30  # untouched retrieval fact — NOT overwritten
    assert top.rerank_score == 1.0
    # Pure function: inputs are not mutated (frozen + dataclasses.replace).
    assert [p.rerank_score for p in passages] == [None, None]


def test_rerank_is_deterministic_and_stable_on_ties() -> None:
    passages = [StubPassage(f"tie {i}", similarity=0.5) for i in range(5)]

    def flat(query: str, texts: list[str]) -> list[float]:
        return [0.5] * len(texts)

    first = rerank("q", passages, scorer=flat)
    second = rerank("q", passages, scorer=flat)

    assert [p.snippet for p in first] == [p.snippet for p in second]
    # Ties keep incoming order (stable sort), so the result is deterministic.
    assert [p.snippet for p in first] == [f"tie {i}" for i in range(5)]


def test_rerank_bounds_passages_scored_and_trails_the_unscored_remainder() -> None:
    passages = [StubPassage(f"p{i}", similarity=0.5) for i in range(10)]
    sent: list[int] = []

    def counting(query: str, texts: list[str]) -> list[float]:
        sent.append(len(texts))
        return [float(i) for i in range(len(texts))]

    result = rerank("q", passages, scorer=counting, limit=3)

    assert sent == [3]  # only the bound number of passages reach the scorer
    assert len(result) == 10  # remainder trails, never silently dropped
    assert [p.rerank_score for p in result[:3]] == [2.0, 1.0, 0.0]
    assert all(p.rerank_score is None for p in result[3:])
    assert [p.snippet for p in result[3:]] == [f"p{i}" for i in range(3, 10)]


def test_rerank_limit_is_clamped_to_the_module_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0013 §5: the bound is min()-clamped against the module ceiling, so a
    caller can narrow it but never widen it past the named constant."""
    monkeypatch.setattr(rerank_module, "RERANK_CANDIDATE_LIMIT", 2)
    passages = [StubPassage(f"p{i}", similarity=0.5) for i in range(6)]
    sent: list[int] = []

    def counting(query: str, texts: list[str]) -> list[float]:
        sent.append(len(texts))
        return [float(i) for i in range(len(texts))]

    rerank("q", passages, scorer=counting, limit=999)

    assert sent == [2]  # 999 clamped down to the module ceiling


def test_rerank_empty_candidates_returns_empty() -> None:
    assert rerank("q", [], scorer=keyword_scorer("x")) == []


def test_rerank_rejects_a_scorer_that_returns_the_wrong_count() -> None:
    passages = [StubPassage("a", 0.5), StubPassage("b", 0.5)]

    def short(query: str, texts: list[str]) -> list[float]:
        return [1.0]  # one score for two passages

    with pytest.raises(ValueError, match="scores"):
        rerank("q", passages, scorer=short)


def test_rerank_raises_on_non_finite_scores() -> None:
    """ADR-0013 §5: a NaN/inf score would break the sort's total order and yield
    a non-deterministic ordering — raise rather than silently corrupt."""
    passages = [StubPassage("a", 0.5), StubPassage("b", 0.5)]

    def nan_scorer(query: str, texts: list[str]) -> list[float]:
        return [1.0, float("nan")]

    with pytest.raises(ValueError, match="non-finite"):
        rerank("q", passages, scorer=nan_scorer)


def test_opt_in_without_the_extra_raises_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-0013 §5: opting into rerank without the [rerank] extra installed must
    raise a clear, actionable RerankUnavailableError naming the extra — never a
    bare ImportError, never a silent cosine swap. Simulated by forcing the lazy
    import to fail, so this test needs no torch."""
    monkeypatch.setattr(rerank_module, "_scorer_singleton", None)
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)  # -> ImportError on import

    with pytest.raises(rerank_module.RerankUnavailableError, match=r"\[rerank\]"):
        rerank_module._default_scorer()


def test_stub_rerank_never_fires_the_model_loader() -> None:
    """ADR-0013: reranking with an injected scorer must never trigger the lazy
    loader (_default_scorer) that builds the real cross-encoder. Robust to
    whether the extra is installed — asserts the loader singleton is untouched
    rather than scanning global sys.modules (which voyageai can pollute)."""
    before = rerank_module._scorer_singleton
    rerank("cyber", [StubPassage("cyber", 0.5)], scorer=keyword_scorer("cyber"))
    assert rerank_module._scorer_singleton is before  # loader never fired


def test_importing_the_rerank_module_is_torch_lazy() -> None:
    """ADR-0013: importing the reranker must not import its heavy dependency —
    the cross-encoder loads lazily inside _default_scorer only. Checked in a
    clean subprocess importing ONLY rerank, so the result holds even when an
    unrelated dependency (voyageai) pulls torch elsewhere in the app, and even
    with the [rerank] extra installed."""
    repo_root = Path(__file__).resolve().parents[3]
    code = (
        "import sys, apps.api.research.rerank; "
        "print('torch' in sys.modules or 'sentence_transformers' in sys.modules)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(repo_root)},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False", proc.stdout


def test_rerank_config_defaults_are_sane() -> None:
    # OFF by default (ADR-0013 §5, amended): FIND stays cheap unless a caller
    # opts in; the ~3 s cross-encoder pass is never paid on the default path.
    assert RERANK_ENABLED is False
    assert isinstance(RERANK_MODEL, str) and RERANK_MODEL
    assert 1 <= RERANK_CANDIDATE_LIMIT <= 200
