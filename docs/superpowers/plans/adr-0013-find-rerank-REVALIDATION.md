# ADR-0013 FIND Rerank — Re-validation (corrected design)

Re-validation after implementing: rerank **off by default / opt-in per request**, ADR
latency + fallback corrections, clear-error-on-opt-in-without-extra, and a NaN guard. No
commit (HEAD `39b5ca4`). Pool-isolated harness (retrieve once per query, apply OFF and ON
to the identical pool) — the confound-free method from the prior round.

## Gate (green, torch-free without the extra)

- `ruff check` clean · `ruff format --check` clean · `mypy apps/` strict clean.
- `pytest`: **282 passed** — verified **twice**: once with the `[rerank]` extra installed,
  and again after uninstalling `sentence-transformers` (the extra), confirming the suite
  runs green **without** the extra. The reranker's torch-laziness is now asserted by a
  subprocess test (importing only `rerank` loads neither torch nor sentence-transformers),
  robust to the fact that `voyageai` opportunistically imports torch when present.

## Criterion 1 — DEFAULT PATH is inert (rerank off by default): **PASS**

For each query, `find_companies(...)` with no rerank arg (module default now `False`):

| query | ordered by match_strength | all rerank_score None | verdict |
|---|---|---|---|
| cybersecurity risk | yes | yes | INERT |
| artificial intelligence risk | yes | yes | INERT |
| supply chain disruption | yes | yes | INERT |

Default-path latency (median of 5): **250–280 ms** — the cheap FIND path is fully restored
(vs ~3 s with rerank on). The feature is provably inert when off: same ordering as the
pre-0013 match_strength order, every `rerank_score` null, and no cross-encoder loaded.

## Criterion 2 — OPT-IN PATH demotes the TRUE false positives: **PASS**

Pool-isolated OFF vs ON on the identical candidate pool:

| query | false positive | OFF rank | ON rank | dropped | determinism | match_strength preserved |
|---|---|---|---|---|---|---|
| cybersecurity risk | **SCHW** (bi-encoder FP) | #3 | **#11** | PASS | PASS (ON==ON) | PASS |
| artificial intelligence risk | **ORCL** (cyber passage w/ incidental AI) | #1 | **#11** | PASS | PASS (ON==ON) | PASS |

- SCHW/cyber: `['PYPL','BRK-B','SCHW',…]` → `['PYPL','MMM','UNP','CRM',…,'SCHW'(#11),…]` —
  on-topic MMM/UNP/CRM rise, SCHW falls.
- ORCL/AI: `['ORCL','PLTR','ACN','EMR',…]` → `['PM','AIG','EMR','MMM',…,'ORCL'(#11),…]` —
  PM/AIG (explicit generative-AI risk) rise, ORCL (a cybersecurity passage that only
  mentions AI in passing) falls from #1 to #11.

EMR and MSFT are **not** criteria (per locked decisions: EMR legitimately on-topic —
dedicated AI risk factor; MSFT a recorded known limitation). Both behave as expected: EMR
stays high (correctly), MSFT is demoted (known cross-encoder miss, accepted).

## Correctness fixes verified

- **Opt-in without the extra → clear error.** With `sentence-transformers` uninstalled, a
  real (non-stubbed) `rerank()` opt-in raises `RerankUnavailableError` naming the `[rerank]`
  extra — not a bare ImportError, not a silent cosine swap. The endpoint maps it to a 503
  (`test_find_rerank_opt_in_without_extra_returns_clear_503`).
- **NaN guard.** A non-finite score raises `ValueError` rather than silently corrupting the
  sort (`test_rerank_raises_on_non_finite_scores`).

## Overall verdict: **PASS on both corrected criteria — committable.**

Default path inert and cheap (~270 ms); opt-in demotes the true false positives (SCHW, ORCL)
deterministically with `match_strength` preserved; clear error and NaN guard in place; ADR
corrected (latency, fallback removed, off-by-default, known-limitations); suite green and
torch-free without the extra. Deferred (separate cosmetic commit, NOT done): the
`RERANK_CANDIDATE_LIMIT` double-clamp and the stale `test_find_rejects_non_positive_caps`
docstring.

**Stopped before commit per scope.**
