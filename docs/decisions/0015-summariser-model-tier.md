# 0015 — Summariser Model Tier: Keep Sonnet 5, Do Not Drop to Haiku

**Status:** Accepted (2026-07-08)

## Context

ADR-0014 made per-filing summarisation **lazy** (7a20069): ingest no longer
summarises — it writes each section's `source_text` eagerly and leaves
`summary` NULL. `GET /companies/{ticker}/summary` is now the SOLE compute
surface (`ResearchService.summarize_on_demand`), summarising pending sections
inline and caching them `WHERE summary IS NULL`; `POST /research/{ticker}`
never computes. The eager ~$12–18/full-corpus summary spend became
**conditional**, paid only on an explicit GET.

With cost removed from the ingest path, the one remaining cost lever is the
**summariser model tier**. The summariser runs on Sonnet 5:

- `apps/api/research/summarizer.py:5` — `_MODEL = "claude-sonnet-5"`, used by
  `ClaudeSummarizer` (`summarizer.py:53`), the only class behind the
  `Summarizer` protocol that `summarize_on_demand` calls.

QA and COMPARE run on a **different, higher tier** and are unaffected by this
decision:

- `apps/api/research/qa.py:30` — `_MODEL = "claude-opus-4-8"`, used by both
  `ClaudeQaAnswerer` (`qa.py:186`) and `ClaudeColumnAnswerer` (`qa.py:272`);
  the latter is wired into COMPARE via `get_column_answerer()` in `router.py`.

This ADR asks a narrow cost-vs-quality question: now that summarisation is
lazy, is dropping the summariser from Sonnet 5 to Haiku 4.5 worth the quality
risk? The driving constraint is **whether the remaining spend is large enough
to justify changing the deliverable's character** — not raw savings.

### Deliverable A — the remaining spend, quantified

Per company-summary is three model calls (business, risk_factors, mdna),
input ≈ the full section `source_text`, output ≈ 300–500 words/section. Real
measured cost, standard pricing (Sonnet 5 $3/$15, Haiku 4.5 $1/$5 per MTok;
Sonnet is on introductory $2/$10 through 2026-08-31, which narrows the gap
further):

| Company | Input tok (3 sec) | Sonnet $ | Haiku $ | Δ / summary |
|---|--:|--:|--:|--:|
| COST | 28,661 | $0.150 | $0.058 | $0.092 |
| NVDA | 59,418 | $0.244 | $0.098 | $0.147 |
| LLY  | 64,887 | $0.265 | $0.103 | $0.162 |
| avg  | ~51k | **~$0.22** | **~$0.086** | **~$0.13** |

Corpus-average input runs higher than this moderate-sized test set (mean mdna
is ~92k chars vs. these three's smaller mdna), so a corpus-average
company-summary is ~$0.25–0.28 on Sonnet, ~$0.10 on Haiku — a **~$0.15
delta per summary**, Haiku ≈ 38% of Sonnet's cost.

**The critical framing:** post-0014 this is paid **per demand**, not
per-corpus. A one-time full-corpus resummarisation of all 84 companies would
cost ~$20–24 on Sonnet vs. ~$8–9 on Haiku (a ~$12–15 delta) — but that event
does not occur under normal use. At realistic lazy volumes (a handful of
`GET summary` calls per research session), the absolute spend is a few dollars
and the Sonnet→Haiku delta is **cents to low single-digit dollars per month**.
0014 already fixed the cost problem; the residual Sonnet spend is small and
conditional.

### Deliverable B — quality A/B (evidence, not vibes)

Both models were run through the exact production `ClaudeSummarizer` path
(same `_SYSTEM_PROMPT`, same `build_prompt`, `max_tokens=16000`, no thinking
param) on three real corpus filings spanning three domains — **COST** (retail),
**NVDA** (tech), **LLY** (pharma) — all nine sections. Full side-by-side outputs
and the per-company cost table are in the companion file
[`0015-summariser-model-tier-AB.md`](0015-summariser-model-tier-AB.md).

Honest prose read of the outputs:

- **Haiku holds the substance.** Across all nine sections and three domains,
  Haiku preserved every material figure exactly — COST's 914 warehouses / $65
  fee / 341k employees / 92.3% renewal; NVDA's $4.5B H20 charge / $60M H20
  revenue / 22%+14% customer concentration / 71.1% gross margin / $4.90 EPS;
  LLY's $65,179M revenue (+45%) / $22,965M Mounjaro (+99%) / 56%-of-revenue
  concentration / IRA Jardiance 66% discount. **No figure corruption, no
  hallucination, no dropped material fact was found** — including on the pharma
  specimen, where drug names, trial phases, and patent-cliff dates are easy to
  garble.
- **If anything, Haiku retains _more_.** It surfaced extra granular facts
  Sonnet (correctly) compressed away — LLY's Mounjaro U.S./OUS revenue split, a
  full drug-by-drug pipeline-phase section, NVDA's full export-control timeline
  and executive-officer roster. Substance loss is **not** the risk here.
- **The real difference is instruction adherence, not accuracy.** Haiku
  systematically **ignores the 300–500-word cap** and produces 1.6–2× the
  output length (e.g. LLY/business: Sonnet 1,833 tok vs. Haiku 3,610;
  COST/risk_factors: 1,520 vs. 2,933). Its output reads as a *near-exhaustive
  restructured extraction*, not a *thesis-selective summary*. Sonnet honours
  both the length ceiling and the "focus on thesis-relevant facts" instruction,
  leading with what matters and compressing boilerplate.

So B does not block a Haiku swap on substance — it flags a **behavioural
regression against the summariser's own contract** (`summarizer.py` asks for
"300-500 words … thesis-relevant facts"). For a *personal research file*
thesis substrate, Sonnet's disciplined distillation is the better-fit product;
Haiku's verbosity shifts the deliverable's character and would inflate its own
output-token cost (eroding part of the very saving that motivates the swap).

## Decision

1. **Keep the summariser on Sonnet 5; do not drop to Haiku 4.5.**
   `summarizer.py:5` stays `_MODEL = "claude-sonnet-5"`. The decision rests on
   both deliverables jointly:
   - **A (primary):** ADR-0014 already reduced summary spend to a small,
     conditional, per-demand cost. The residual Sonnet→Haiku delta (~$0.15 per
     company-summary; cents-to-low-dollars per month at realistic lazy volumes)
     is not material enough to justify revalidating output, running a second
     live-validation cycle, and accepting a change in the deliverable's
     character. **0014 was the real cost fix; the model tier is not worth
     touching.**
   - **B (supporting):** Haiku preserves every material fact but breaks the
     summariser's 300–500-word / thesis-selection contract, producing 1.6–2×
     longer near-exhaustive output. The saving is not worth the behaviour
     change.

2. **Rejected — (b) drop to Haiku 4.5.** Lost because the spend it saves is now
   trivial post-0014 while it degrades adherence to the summary contract; the
   juice (a few dollars/month) does not justify the squeeze (re-validation +
   output-character drift). Substance quality was *not* the disqualifier — the
   A/B shows Haiku holds the facts — but with the cost case gone, there is no
   positive reason to accept even a small behavioural risk.

3. **Rejected — (c) per-section split (e.g. Haiku for business, Sonnet for
   risk/mdna).** The A/B shows Haiku's verbosity is **uniform** across all three
   sections; there is no section where Haiku clearly wins or where its output is
   materially cheaper-and-equivalent. A split would add per-section model
   plumbing and a mixed-provenance `filing_summaries.model` column for no
   evidenced benefit. The evidence does not support a middle option.

**Out of scope** (each its own decision if ever pursued):

- **QA / COMPARE model tier** (`qa.py:30`, `claude-opus-4-8`). Untouched; a
  different tier for the answer layer is a separate cost-vs-quality question
  behind its own ADR.
- **FMP / market-data tiering** — unrelated to summarisation.
- **`repair.py` re-summarisation cleanup** (the ADR-0014 §6 follow-up to switch
  repair to invalidate-to-NULL). Independent of model tier.

## Compliance and Validation

What must hold for this decision to be correct, and how it is checked:

- **Blast radius is exactly one call site.** The summariser model is read only
  at `summarizer.py:53` (`model = _MODEL`). QA/FIND/COMPARE/change-detection
  never read `filing_summaries.summary` — they retrieve from `filing_chunks`
  (the embeddings substrate), so no evidence-layer surface depends on the
  summary text or its model. Verifiable structurally: `grep` for
  `filing_summaries` / `.summary` usage shows the summary is consumed only by
  `summarize_on_demand` → `GET /companies/{ticker}/summary`.
- **Reversibility.** This is a one-line change (`_MODEL` string). If the spend
  calculus ever changes (see Consequences → Risk accepted), flipping to Haiku
  and re-running the A/B is trivial and non-migrating — no schema change, no
  backfill (`filing_summaries.model` already records per-row provenance from
  ADR-0014, so a mixed-model corpus is already representable).
- **Wall compliance unchanged.** The summariser emits attributed, cited
  research-file prose ending in `Source: <url>` (ADR-0007 §3); it produces no
  verdict, ranking, or superlative regardless of tier. Keeping Sonnet does not
  reopen any evidence/judgment-wall guarantee.

## Consequences

- **Changes:** none to code. This ADR records that the summariser tier was
  evaluated post-0014 and deliberately left on Sonnet 5 — the silence is
  chosen, not accidental.
- **Unchanged:** the ADR-0014 lazy/on-demand contract (ingest never spends;
  `GET summary` is the sole compute surface; UPDATE-guarded caching); the
  ADR-0007 citation discipline; QA/COMPARE on `claude-opus-4-8`; all
  retrieval (FIND/COMPARE/change-detection reading `filing_chunks`, not
  `summary`).
- **Cost outcome:** the summary spend that matters was already removed by 0014
  (eager → lazy). The additional Sonnet→Haiku saving (~$0.15/summary; ~$12–15
  one-time if the entire corpus were ever resummarised at once) is **declined**
  as not worth pursuing at current lazy volumes.
- **Quality tradeoff shown by B:** Haiku would not have lost facts, but it
  would have replaced 300–500-word thesis-selective summaries with 1.6–2×
  longer exhaustive extractions — a change the research-file use case does not
  want. Keeping Sonnet preserves the intended deliverable.
- **Risk accepted / reopen condition:** if a **bulk-summarisation event** is
  ever scheduled — most plausibly the deferred **S&P 500 breadth run**
  (ADR-0010/0011 backlog), which would summarise hundreds of new filings in one
  pass — the per-corpus delta becomes material (hundreds of companies × ~$0.15)
  and this decision should be **re-opened**: re-run the A/B, and consider Haiku
  (or a tightened prompt that curbs its verbosity) for that specific bulk pass
  while leaving interactive `GET summary` on Sonnet. The reversibility above
  makes that a cheap future move.
- **Build sequencing:** no build. Draft → review → accept records the "keep
  Sonnet" decision; no implementation follows.
