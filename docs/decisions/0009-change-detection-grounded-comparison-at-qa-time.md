# 0009 — Change Detection: Grounded Comparison at QA Time

**Status:** Accepted

## Context

The corpus is temporal (ADR-0008): the evidence layer holds multiple 10-Ks per
company over time — AAPL now carries FY2024 and FY2025 — and live temporal QA
already produces grounded year-over-year comparisons citing both filings. The
ADR-0007 §5 gate opened by data, exactly as designed.

Two concrete problems surfaced from real temporal-QA runs, not from
speculation:

1. **Retrieval is period-skewed.** At the default limit of 8, a "what changed"
   query pulled 6 chunks from FY2024 and 2 from FY2025. Raw top-k ranks by
   similarity alone; it does not guarantee both periods are represented —
   which is exactly what a comparison needs. The answer layer coped honestly
   (it scoped its claims to the overlap), but the comparison was starved of
   newer-period evidence by the retrieval step itself.

2. **Comparisons have no structured home.** The model packs year-over-year
   findings into direct-mode prose claims because there is no dedicated slot
   for a both-period diff. The comparison content was correct and cited, but
   unstructured — hard for downstream consumers (a future briefing, a UI diff
   view) to read without re-parsing prose.

This ADR records the decision to add a **change-detection capability** to the
evidence layer: make "what changed between period A and period B" produce the
best possible grounded, both-cited answer. It is a pure analysis-quality
enhancement. It is ADDITIVE to ADR-0007 — no guarantee is reopened or
weakened — and **alerting is explicitly out of scope**.

## Decision

1. **QA-time capability, no persistence.** Change detection is a retrieval +
   prompting mode on the existing QA path, not a stored artifact. No new
   table, no migration. Rationale: alerting is out of scope, so there is
   nothing to fire on; persisting a change record now would be speculative
   forward work. It is built as ONE change-detection function so that if a
   future ADR introduces persistence (for alerting/briefing), the store is a
   cache of this function's output, not a second implementation — one
   definition of a change in the system.

2. **Balanced per-period retrieval, scoped to change-detection queries only.**
   For a temporal/comparison query across P periods at limit L, retrieve
   ⌊L/P⌋ chunks per period (4/4 at limit 8 for the pairwise case), with any
   remainder from uneven division going to the most recent period(s). This
   applies ONLY when change detection is requested; ADR-0007's existing QA
   retrieval path is untouched.

3. **Structured `period_comparison` response field.** Temporal queries
   populate a NEW dedicated structured field — distinct from ADR-0007's
   existing `what_changed` (list[Claim]), which it neither renames nor
   modifies — carrying a list of change entries, each with: *dimension*;
   *period_a* {state, period_end_date, source_url}; *period_b* {state,
   period_end_date, source_url}; *change_description* (factual — what
   differs). Comparisons are ROUTED into this field rather than left in
   direct-mode prose. `period_comparison` is null/absent for non-temporal
   queries.

4. **Both-period citation is mandatory (ADR-0008 §5).** Every change entry
   must carry both periods' `source_url`s. A change claim that cannot cite
   both periods is not emitted.

5. **"No change on the queried dimension" is a first-class outcome.** When the
   filings state the same thing on the queried dimension across periods, the
   layer says so explicitly with both periods cited — it must never
   manufacture a change to appear useful. This is the temporal analog of the
   `no_prior_period` guard.

6. **Factual/structural materiality only — no significance threshold, no
   ranking.** The layer surfaces WHAT changed and labels magnitude factually
   (for numeric changes: absolute + percent). It does NOT decide whether a
   change matters enough to care, does NOT rank changes by importance, and
   does NOT frame anything as an opportunity or as attractive.
   Ranking-by-attractiveness and any "worth looking at" judgment remain
   walled off in a future, separately-governed judgment layer and are
   explicitly out of scope here.

7. **Explicit request, not inferred.** Change detection is triggered by an
   explicit parameter/flag on the QA request, not by inferring intent from
   question text. Rationale: inference is a classifier that can misfire and
   silently degrade the working ADR-0007 QA path; an explicit flag keeps the
   behavior deterministic and reviewable, and a temporal question asked
   without the flag simply gets today's proven ADR-0007 answer shape.

**Out of scope** (future work, each its own decision): alerting and the daily
briefing (and with them any persistence of change records, per decision #1),
significance thresholds and importance ranking (the judgment layer's wall,
per decision #6), and 10-Q ingestion — when quarterly filings land, comparison
pairing must follow ADR-0008 §3's *previous comparable filing* definition and
surface unlike-period comparisons explicitly, never silently.

## Consequences

- **Changes:** the QA request grows an explicit change-detection flag; a
  balanced per-period retrieval mode is added alongside (not inside) the
  existing top-k path; a NEW `period_comparison` field is ADDED to the answer
  schema for change-detection queries per decision #3 — it modifies no
  existing field; verification extends to enforce decision #4 — a change
  entry missing either period's citation is a defect the layer must not emit,
  with the existing warnings channel as the backstop.
- **Unchanged:** every ADR-0007 guarantee (chunks-only grounding, no verdicts,
  attribution rules, warnings-are-first-class), the default QA retrieval
  path, the schema (no table, no migration — per house rules none may be
  written before this ADR is accepted), and the whole ingestion/embedding
  pipeline. ADR-0007's `what_changed` field (list[Claim]) and the two-sided
  case's what-changed component are unchanged and still populated per
  ADR-0007 regardless of the change-detection flag.
- **Risk accepted:** at a fixed limit, balancing halves per-period retrieval
  depth (4 chunks per period instead of up to 8), so a comparison may see
  less of each filing than a single-period question does; the remedy is
  raising the limit for comparison queries, not unbalancing retrieval.
  Dimension alignment across periods (that period_a's "state" and period_b's
  "state" describe the same thing) is model-judged and only
  citation-checkable — semantic support checking remains future enforcement
  work, the same posture ADR-0007 took for claims.
- **Build sequencing:** the first increment is pairwise 10-K-vs-prior-10-K on
  the existing annual-only corpus (ADR-0008's sequencing), where both-period
  citation and no-change behavior can be validated without the
  unlike-period hazard. Multi-period (P > 2) and quarterly comparisons build
  on the same decisions later; nothing here presumes them.
