# ADR-0013 FIND Rerank — Before/After Validation Plan

**Status:** ready to execute (build landed mocked; NOT yet live-run, NOT committed).
**Discipline:** live-validate-twice (CLAUDE.md #4, precedent 7973c0a) — confirm the
defect clears AND that adjacent legitimate behavior still emits. Nothing commits
until both specimens pass the two-part criterion below on two runs.

## 0. Prerequisites (one-time, before any run)

1. **Install the reranker extra** (torch is not in the base/dev env by design —
   `sentence_transformers`/`torch` are confirmed absent, so the suite is
   torch-free):
   ```bash
   source .venv/bin/activate
   pip install -e '.[rerank]'
   ```
2. **Warm the model once** (first use downloads ~80 MB; do it outside the timed
   runs so the download doesn't pollute the latency numbers):
   ```bash
   python -c "from apps.api.research.rerank import _default_scorer; _default_scorer()('warm', ['warm up the model'])"
   ```
3. Reranking ships **default on** (`RERANK_ENABLED = True`), so the live API and
   the ad-hoc harness below rerank without any flag flip. The "before" arm is
   produced by passing `rerank_enabled=False`.

## 1. What reranking can and cannot fix (read before interpreting results)

Reranking operates on the **clamped** candidate set, not the full corpus:

- Stage-1 wide search returns up to `WIDE_SEARCH_LIMIT = 80` chunks corpus-wide.
- Grouping keeps the top `CANDIDATE_N = 15` companies by best cosine.
- The reranker scores **every chunk those 15 companies have in the 80-chunk wide
  set** (`RERANK_CANDIDATE_LIMIT = 80` ≥ the max possible pool, so nothing is
  clipped in the default config), then reorders passages within and across them.

Consequences to hold in mind when judging pass/fail:

- **Rerank fixes ORDERING among candidates, not RECALL of non-candidates.** A
  company that never made the top-15 cut cannot be rescued here — that is the
  stage-1 recall knob (`CANDIDATE_N` / `WIDE_SEARCH_LIMIT`), unchanged by ADR-0013.
- SCHW/EMR are fixable **because they already surfaced** — i.e. their off-topic
  high-cosine chunk got them into the top-15. The reranker sees their chunks and
  can demote them. If a specimen's off-topic company is NOT in the pre-rerank
  top-15, that is a recall issue, out of scope for this validation.
- Rerank can only re-rank passages that were **retrieved**. If a company's only
  on-topic passage sits deeper than wide-rank 80, rerank cannot surface it. If a
  specimen fails this way, note it as a recall boundary, not a rerank regression.

## 2. Harness (produces the before/after diff per query)

Run from the repo root with the venv active and the live DB reachable (the same
engine/embedder the API uses). This calls `find_companies` twice per query —
`rerank_enabled=False` then `True` (default scorer) — and prints the company
order plus each company's representative passage.

```python
# scratch/rerank_validate.py  (scratch only — do NOT commit)
from apps.api.db import get_engine            # use the project's real engine factory
from apps.api.research.embeddings import make_embedder  # the real Voyage embedder
from apps.api.research.find import find_companies

QUERIES = [
    "cybersecurity risk",            # SCHW specimen (off-topic-best on cyber)
    "artificial intelligence risk",  # EMR specimen (off-topic-best on AI)
    "supply chain disruption",       # broad control
    "climate and environmental risk",# broad control
    "foreign currency exchange risk south",  # broad control
]

def show(query, enabled):
    r = find_companies(engine, embedder, query, rerank_enabled=enabled)
    print(f"\n[{'RERANK' if enabled else 'COSINE '}] {query!r}")
    for i, m in enumerate(r.matches, 1):
        p = m.passages[0]
        rs = "—" if p.rerank_score is None else f"{p.rerank_score:+.3f}"
        print(f"  {i:>2}. {m.ticker:<6} match_strength={m.match_strength:.3f} "
              f"rerank={rs}  “{p.snippet[:90].strip()}…”")

engine = get_engine()
embedder = make_embedder()
for q in QUERIES:
    show(q, False)   # BEFORE
    show(q, True)    # AFTER
```

(Adjust the two import lines to the project's actual engine/embedder factories —
the harness itself is the only thing that changes, not `find_companies`.)

## 3. Two-part pass criterion (BOTH must hold for BOTH specimens)

For **SCHW (cyber)** and **EMR (AI)**, "fixed" means both effects fire — not one:

- **(a) Cross-set effect — the off-topic-best company drops in COMPANY order.**
  In the COSINE arm, SCHW (resp. EMR) ranks high because its top-cosine chunk is
  off-topic. In the RERANK arm, it moves **down** the company list, overtaken by
  companies with genuinely on-topic passages. Record its rank before vs after.
- **(b) Within-company effect — an on-topic company surfaces the CORRECT
  passage.** For at least one genuinely on-topic company in the same result, its
  representative passage (`passages[0]`) in the RERANK arm is **more on-topic**
  than in the COSINE arm — the reranker picked a better-answering chunk to
  represent it and survive the cap.

Both specimens should demonstrate **both** (a) and (b): SCHW/EMR drop, and a
real cyber/AI discloser rises with the right passage shown. A specimen that
shows only (a) or only (b) is a partial result — investigate before committing.

**Auditability check (wall):** in the RERANK arm, confirm each demoted company's
`match_strength` is **unchanged** from the COSINE arm (it is preserved as the max
cosine, ADR-0013 §4). The demotion must be visible as "rerank_score low, cosine
still high," never as a rewritten cosine. If `match_strength` moved, that is a
bug — the retrieval fact was overwritten.

## 4. Broad-query regression check (adjacent behavior still emits)

For the three broad controls (`supply chain`, `climate`, `foreign currency`):

- The RERANK arm still returns a **non-empty, sensible** company list — reranking
  must not empty out or scramble a healthy broad query.
- Spot-check that on-topic companies that were already correctly ranked in the
  COSINE arm are **still present** and not displaced by the reorder. Reranking
  should refine the top, not churn a working result.
- Every returned passage carries a non-null `rerank_score` (proves the reranker
  actually ran on the whole pool, no silent fallback to cosine).

This is the "legitimate behavior still emits" half of live-validate-twice: the
fix must clear SCHW/EMR **without** regressing queries that were already fine.

## 5. Latency delta

Measure the per-query wall-clock added by reranking, model already warm:

```python
import time
def timed(query, enabled, n=5):
    ts = []
    for _ in range(n):
        t = time.perf_counter()
        find_companies(engine, embedder, query, rerank_enabled=enabled)
        ts.append(time.perf_counter() - t)
    ts.sort()
    return ts[len(ts)//2]  # median
```

- Report **median COSINE vs median RERANK** per query and the delta.
- **Expectation:** the delta is the cross-encoder scoring the candidate pool
  (≤ 80 (query, passage) pairs) on CPU — tens of ms, proportionate to a
  retrieval step, not seconds. Exclude the first call (model load / download).
- **Red flag:** a per-query delta in the hundreds of ms or more suggests the
  model is reloading per call (singleton not holding) or the pool is far larger
  than expected — investigate before accepting.

## 6. Record & decide

- Paste the before/after blocks for all five queries and the latency table into
  the ADR-0013 evidence trail (mirror how COMPARE recorded its specimens at the
  constants in `compare.py`).
- **Run the whole thing twice** (live-validate-twice). Only if both specimens
  pass (a)+(b) on both runs, the broad controls don't regress, `match_strength`
  is preserved, and latency is tens-of-ms, do the changes get committed — as one
  concern (the reranker), with the ADR already Accepted.
- If a specimen fails, classify it: ordering not moving (rerank/tuning), an
  on-topic company absent (recall — out of scope, note it), or `match_strength`
  drift (bug — fix before proceeding).

## Open knob (from the ADR)

`RERANK_CANDIDATE_LIMIT = 80` covers the full default pool. It only needs raising
if `WIDE_SEARCH_LIMIT` is later raised above 80; otherwise the whole candidate
pool is already scored and there is nothing to widen.
