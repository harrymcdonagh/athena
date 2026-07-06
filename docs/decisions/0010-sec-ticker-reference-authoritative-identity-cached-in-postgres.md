# 0010 — SEC Ticker Reference: Authoritative Identity, Cached in Postgres

**Status:** Accepted

## Context

Ticker→identity resolution currently rides on two things. First, the EDGAR
client's `resolve_ticker` downloads the SEC's `company_tickers.json` on every
lookup — the right source, used ad hoc and never cached. Second, the batch
ingestion universe is `docs/domain/sp100-tickers.txt`, a model-sourced,
hand-maintained snapshot whose header admits it needs verifying. That file
conflates two jobs: resolving a ticker to ground-truth identity (CIK, EDGAR
conformed name) and curating which companies Athena covers. Identity facts
should never be hand-typed or model-remembered — some tickers in a hand-kept
list may simply be wrong, and a wrong identity poisons everything downstream.

The SEC publishes the authoritative mapping itself: `company_tickers.json`
(and `company_tickers_exchange.json`, which adds exchange) at `sec.gov/files/`
— free, no API key, ~13k companies, ticker → CIK → EDGAR conformed company
name, from the same authority the filings come from. Operational facts: CIKs
arrive without leading zeros and EDGAR endpoints need them zero-padded to 10
digits (the client already normalizes this); the SEC's fair-access policy
(10 req/s, required User-Agent) is already honored by the EDGAR client.

The goal is breadth well beyond 100 companies — but NOT the whole exchange
tail. Micro-caps, 20-F filers, shells, and SPACs carry messy-tail ingestion
failures and real embedding cost for filings that will never be queried. The
resolver should know the whole universe; the ingestion set stays curated.

## Decision

1. **SEC company_tickers.json is the authoritative ticker→identity resolver.**
   Ticker → CIK, EDGAR conformed company name, and exchange are sourced from
   the SEC file, never hand-typed. This is evidence-layer REFERENCE data from
   the same authority as the filings themselves; the hand-maintained list is
   superseded as a source of CIK/name truth.

2. **Cached in Postgres as a dedicated reference table, separate from
   `companies`.** A new table, named plainly as external SEC mapping data
   (e.g. `sec_ticker_reference`), holds the resolvable universe. It is
   explicitly a CACHE of an external SEC file — it does not replace or merge
   into `companies`. Rationale (Postgres-as-ontology, ADR-0003): `companies`
   means "companies Athena holds ingested evidence for"; the reference table
   means "the universe of SEC-resolvable companies." Folding ~13k universe
   rows into `companies` would break that meaning and every query and join
   that assumes a `companies` row implies ingested filings. Two tables, two
   distinct meanings. Corollary: resolution alone never creates a `companies`
   row — a ticker that resolves in the reference table but has no ingested
   filings has a reference row and NO `companies` row.

3. **Direction of flow: reference informs ingestion; `companies` keeps its
   meaning.** `companies` continues to be populated during ingestion exactly
   as today (`upsert_company` at ingest time, keyed on CIK), now using
   resolved facts — CIK, conformed name, exchange — from the reference table
   instead of unverified input. The reference table feeds resolution; it is
   not itself a record of what has been ingested. One direction:
   reference → informs → `companies`. No dual source of truth for "what have
   we ingested."

4. **Refresh freely; ingested evidence stays pinned.** The reference table is
   a cache and may be re-downloaded to track SEC truth; a refresh writes ONLY
   the reference table, never the evidence tables. Already-ingested
   companies and filings stay pinned to the identity they were ingested under
   — a filing was ingested as the entity it was at ingest time, and that
   record does not silently mutate when the cache refreshes. Reference and
   pinned evidence MAY therefore disagree after a refresh; each is
   authoritative for its own purpose — the reference table for resolving a
   ticker NOW, the pinned evidence for what was ingested THEN. Refresh is an
   explicit, on-demand operation, not an implicit background job —
   deterministic and reviewable, the same explicit-over-inferred posture as
   ADR-0009 §7. Scheduled refresh is future work if wanted.

5. **The curated ingestion set stays git-versioned knowledge (ADR-0004),
   defined as a selection method, not an identity list.** The set is a
   checked-in, dated, hand-editable list of SYMBOLS to ingest — no hand-typed
   CIKs or names, since those come from the resolver. What is versioned is
   the selection (e.g. an S&P 500 snapshot or an explicit symbol list), not
   today's particular 100. Every symbol is validated against the SEC
   reference at ingest time, so an unresolvable symbol is a reported error,
   never a silent gap. The sp100 file is superseded as an identity source;
   the git-versioned-list PATTERN continues.

6. **Scope boundary: SEC-authoritative provenance is the wall — explicitly
   not market data.** The durable boundary is provenance, not a field list: a
   field belongs in the reference table only if the SEC itself publishes it
   as company identity reference data — today ticker, CIK, EDGAR conformed
   name, and exchange. Anything requiring a market-data or other non-SEC
   third-party source — sector, industry, price, market cap, fundamentals —
   is out, and belongs to the future, separately-walled judgment layer and
   its market-data-API ADR. This
   reference source stays on the evidence side of the wall; "we're already
   pulling company data, let's grab price too" is exactly the drift this
   boundary exists to stop.

**Out of scope** (future work, each its own decision): scheduled or automatic
cache refresh; ingesting the full exchange tail (all NASDAQ/NYSE including
micro-caps, 20-F filers, SPACs); any market data (price, market cap) or
non-SEC enrichment (sector, industry, fundamentals) — judgment-layer /
market-data-API territory with its own ADR; and any change to how
already-ingested evidence is stored.

## Consequences

- **Changes:** the ontology gains a new KIND of table — an external-source
  cache, distinct from ingested-evidence tables and derived-from-evidence
  tables — and must stay legible about that distinction; ticker resolution
  reads the cache instead of re-downloading `company_tickers.json` per
  lookup; batch ingestion validates its symbol list against the reference,
  turning bad symbols into reported failures; a larger curated symbol set
  replaces the sp100 file as the ingestion universe. A new table means a
  migration, which per house rules is written only AFTER this ADR is
  accepted — nothing migrates before acceptance.
- **Unchanged:** `companies`' meaning and its ingest-time population path;
  the filings/summaries/chunks evidence chain and every ADR-0005/0007/0008/
  0009 guarantee; the EDGAR fair-access posture; and the judgment-layer wall
  — no market data enters the system.
- **Risk accepted:** reference vs. pinned-evidence divergence after a refresh
  is a real, accepted state, with decision #4 defining which is authoritative
  for which purpose; a curated set (deliberately) means Athena cannot answer
  about companies outside it, and the remedy is editing the checked-in list,
  not widening the default; the SEC file's shape is an external contract that
  can change, and the existing unexpected-response-shape error handling is
  the guard.
- **Build sequencing:** the first increment after acceptance is the reference
  table, its download/refresh operation, and ticker resolution wired into the
  existing batch ingestion path — validating the resolver on the current
  scale before widening. The larger curated symbol set and the full breadth
  run follow on the validated resolver. Nothing here presumes the judgment
  layer.
