# 0008 — Temporal Corpus: Multiple Filings per Company over Time

**Status:** Accepted

## Context

The corpus is single-period: one 10-K per company, ingested once. The schema
(ADR-0005) never required this — `filings` FKs to `companies`, carries
`form_type`, `filing_date`, `period_end_date`, and dedupes by unique
`accession_number` — but the pipeline around it assumes it: the EDGAR gateway
exposes only `latest_10k`, re-running research on a ticker 409s as "already
ingested," and `latest_research` picks a thesis by `created_at`, which happens
to be fiscal order only because each company was ingested exactly once.

ADR-0007 §5 gated temporal claims on the corpus: "what changed since last
quarter/year" is answerable only when the chunks span multiple filing periods
for the same company, and today they never do, so QA honestly answers
`no_prior_period`. This ADR is the foundation for opening that gate — making
"what changed between two filings" a grounded, queryable fact — and, further
out, for daily briefings and a (separately walled) valuation layer.

## Decision

1. **`companies` is the temporal anchor.** A company's history is the set of
   `filings` rows sharing its `company_id`; no new entity is introduced. A
   company's filings are ordered in time by **`period_end_date`** — which is
   authoritative for *what period a filing covers* — with `filing_date` as
   tie-breaker (then `accession_number`, for determinism). The two dates
   differ: `period_end_date` is the fiscal boundary the document reports on;
   `filing_date` is when it reached EDGAR, weeks later. Disclosure-timeline
   questions may consult `filing_date`, but period ordering keys on
   `period_end_date`. Consequence: `period_end_date`, today nullable, must be
   reliably populated for every ingested filing.

2. **The corpus mixes filing types.** Ingestion extends to 10-Qs now, 8-Ks
   later; annual and quarterly filings interleave on one timeline. Therefore
   "the filing before this one" may pair a 10-Q with a 10-Q *or* with a 10-K.
   Comparing unlike periods (a quarter against a full year) is a real
   correctness concern: it must be handled explicitly by the future
   change-detection layer — surfaced as unlike-period, or restricted to
   comparable filings — never papered over silently.

3. **Definitions the temporal layer depends on.** For a company:
   - *Latest filing*: the filing greatest in the §1 ordering.
   - *Previous filing*: the filing immediately preceding a given one in the
     §1 ordering, regardless of `form_type` — the disclosure-sequence
     neighbor.
   - *Previous comparable filing*: the nearest preceding filing **of the same
     `form_type`** — the like-for-like comparison target ("this 10-Q vs the
     prior 10-Q"). Year-ago-quarter selection and other comparison policies
     belong to the change-detection ADR, built on these definitions.

4. **`thesis_snapshots` stands as designed.** Its append-only, one-row-per-
   `(company, point in time)` design — `company_id` FK, `source_filing_id`,
   trigger-enforced immutability — already represents multiple points in time;
   nothing structural changes. One honest caveat: `latest_research` orders
   snapshots by `created_at`, i.e. ingestion order. Once older filings can be
   backfilled after newer ones, "latest thesis" should follow the source
   filing's §1 ordering, not insertion order. That is a query fix for the
   implementation step, not a schema change.

5. **Auditability carries forward unweakened.** Every filing, summary, and
   chunk row already carries its own `source_url`; additional filings add
   rows with their own URLs, so the ADR-0005/0006/0007 audit chain
   (claim → chunk → document → EDGAR URL) is untouched by going temporal.
   New rule: a cross-filing comparison is grounded only if it cites evidence
   from **both** filings being compared — a "what changed" claim must trace to
   the two `source_url`s, not just the newer one.

**Out of scope** (future work, each its own decision): the change-detection
algorithm itself, the daily briefing, and the entire valuation/judgment layer
— which gets its own ADR with its own wall, separate from the evidence layer.

## Consequences

- **Changes:** the EDGAR gateway grows beyond `latest_10k` (list a company's
  filings, fetch a specific one, including 10-Qs); section extraction must
  handle 10-Q structure (different items; risk factors often update-only);
  re-running research on a ticker means "ingest what's new" rather than 409
  (dedup by `accession_number` already supports this); `period_end_date`
  gains a NOT NULL guarantee via a later migration — written only after this
  ADR is accepted, per house rules.
- **Unchanged:** the schema's shape, the chunking/embedding pipeline, the
  whole evidence/QA layer, and every ADR-0007 guarantee. The §5 temporal gate
  opens by data, not by code: once multi-period chunks exist, retrieval spans
  periods and `no_prior_period` recedes naturally.
- **Risk accepted:** unlike-period comparisons become *possible* before the
  change-detection layer makes them *safe*; until then QA's grounding rules
  (cite both filings or say the corpus doesn't support the comparison) are
  the only guard, which is exactly the ADR-0007 posture.
- **Build sequencing:** the schema and the latest/previous/previous-comparable
  definitions support mixed filing types (10-K, 10-Q) from day one, but the
  first temporal ingestion increment is deliberately **annual-only** — a
  second (prior-year) 10-K per company — so 10-K-vs-prior-10-K change
  detection can be validated in isolation, without simultaneously introducing
  the unlike-period (quarter-vs-year) hazard decision #2 flags. 10-Qs are a
  deliberate follow-on once same-type temporal comparison is proven. This is
  a build-order decision, not a change to what the schema permits.
