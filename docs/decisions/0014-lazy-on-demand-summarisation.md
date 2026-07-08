# 0014 — Lazy, On-Demand, Cached Filing Summarisation

**Status:** Draft

## Context

Every ingested filing is summarised eagerly at ingest. `ResearchService.run()`
(`apps/api/research/service.py`) loops over the three extracted sections —
`business`, `risk_factors`, `mdna` — and calls `self._summarizer.summarize()`
once per section (service.py:213–221). That is **three answer-model calls per
filing**, made unconditionally the moment a filing is stored, before anyone has
asked to read anything. The summarizer runs `claude-sonnet-5`
(`summarizer.py:5`) at `max_tokens=16000` over the full section text; a 10-K's
Business, Risk Factors, and MD&A sections are each many thousands of tokens of
input. The three summaries are then concatenated (no further model call) by
`compose_thesis()` into a `thesis_snapshots` row, also written eagerly at
ingest.

**This eager summarisation is the binding cost constraint on scale.** At the
current 85-filing corpus it is ~255 `claude-sonnet-5` calls already spent; an
S&P 500 breadth run (deferred by choice under ADR-0010/0011) would multiply it
by roughly six with no change in design. The cost is paid per company at ingest
whether or not the summary is ever read.

**And most of it is never read.** The expensive `summary` output is consumed by
exactly three surfaces, all of them thin:

- `Repository.latest_research()` → `GET /companies/{ticker}/summary`
  (`router.py:406`), the only endpoint that returns summary/thesis prose;
- `ResearchOutcome.summaries` → the `POST /research/{ticker}` response body;
- `repair.stored_summaries()`, which reuses unaffected sections' summaries when
  recomposing a thesis during extraction repair.

The committed frontend (`apps/web`) calls **none** of these — it uses
`/research/find`, `/research/qa`, `/research/search`, and `/research/companies`
only. The QA layer (ADR-0007), change detection (ADR-0009), FIND (ADR-0011),
and COMPARE (ADR-0012) **never read `filing_summaries.summary` or
`thesis_snapshots`** — they answer over `filing_chunks`, the embedded
`source_text`. So the answer-model summary is produced for every filing and
read for almost none.

**The principle to encode is the system-wide one already applied elsewhere:
cheap by default, expensive on explicit demand** (ADR-0011 §3's FIND-first
funnel; ADR-0012's caps-as-refusals; ADR-0013 §5's opt-in rerank). Filing
summarisation — answer-model prose over full-section text — is the expensive
work, and it should happen on demand and be cached, not eagerly for every
company at ingest.

**The load-bearing fact that makes this safe: retrieval does not depend on the
summary.** The embeddings backfill reads `source_text`, not `summary`
(`Repository.sections_pending_embedding` selects `s.source_text`;
`embeddings.run_backfill` embeds `item.source_text` — repository.py:198–219,
embeddings.py:100–114). `source_text` is the raw extracted section text, written
at ingest with no model call. Embedding it (Voyage `voyage-context-4`, ADR-0006)
is the cheap step that keeps FIND, COMPARE, QA, and change detection working,
and **it stays eager**. Only the answer-model summarisation moves.

This ADR governs only the summarisation pass. It changes no retrieval behaviour,
adds no answerer to any path that lacks one, and does not touch FMP or the
judgment layer.

## Decision

1. **Summarise a filing's sections on first explicit demand, then cache;
   ingest stops summarising.** At ingest, `ResearchService.run()` extracts each
   section and stores its `source_text` (the retrieval substrate) exactly as it
   does today, but makes **zero** `summarize()` calls. The three
   `claude-sonnet-5` calls per filing are removed from the ingest path. A
   section's `summary` is computed the first time it is explicitly demanded
   (decision #3) and cached in place; every subsequent read is a cache hit with
   no model call. The rejected alternative — keep eager summarisation but add a
   cache — saves nothing, because the eager pass is exactly the spend we are
   trying not to make.

2. **The cache unit is the section; the cache key is `(filing_id, section)` —
   the identity that already exists.** `filing_summaries` already carries
   `UNIQUE (filing_id, section)` (migration 0001). Per-section, not per-filing,
   is the right grain: sections are summarised independently today, repair
   already operates per damaged section, and a demand for one section should not
   force the other two. No new identity is invented — `filing_id` already
   encodes the filing (a unique `accession_number` with its `content_sha256`),
   and `period_end_date` already encodes the period, so the sha/period identity
   the codebase relies on carries over unchanged.

3. **"Demand" is an explicit request for a filing's summary — never inferred
   from a QA/FIND/COMPARE call.** Concretely, demand is a request to the
   summary surface (`GET /companies/{ticker}/summary`, and the
   `POST /research/{ticker}` response if a caller asks it to return prose):
   someone has explicitly asked to read the human-readable summary/thesis of a
   filing. Demand is **not** a QA question, a FIND query, a COMPARE column, or a
   change-detection run — those read `filing_chunks` and must continue to make
   **zero** summarisation calls. This mirrors the explicit-not-inferred posture
   of the compare flag (ADR-0009 §7: change detection is requested, never
   inferred from question text) and the opt-in rerank flag (ADR-0013 §5): the
   expensive path is entered by an explicit act, not guessed from a cheap one.
   The rejected alternative — letting a QA call lazily trigger summarisation of
   whatever filings it retrieved from — is refused: it reintroduces
   answer-model spend into the cheap, frequent path and couples QA cost to
   corpus size, the exact coupling this ADR removes.

4. **A not-yet-computed summary is a first-class honest-absence state, not an
   error and not a fabricated blank.** Between ingest and first demand, a
   section has `source_text` (embedded, searchable) but no `summary`. This is
   the same posture as the 16 known-absent filers (CLAUDE.md; honest absence
   over silent wrongness) and COMPARE's `no_finding` / `no_evidence`
   (ADR-0012 #5): the state is *reported as what it is* — "not yet summarised" —
   never a 500, never an empty string presented as if the model returned it. The
   summary surface distinguishes "computed" from "not yet computed" explicitly.
   On the storage side this state is `summary` absent for that `(filing_id,
   section)`; the migration that expresses it (dropping the `summary NOT NULL`
   constraint so the section row can exist without a summary, or an equivalent
   split of the section row from the summary row) is deferred to implementation
   under this ADR, per the ADR-before-migration rule — but the *contract* is
   decided here: a section row may exist with its `source_text` and no
   `summary`, and that is valid.

5. **A cached summary is valid for the exact `source_text` (and thus
   `content_sha256`) it was computed from; when that changes, it is
   invalidated, not silently served.** A new filing is a new `accession_number`
   → a new `filing_id` → no cached summary, so new filings are summarised on
   demand by construction, reusing existing filing identity. For the *same*
   `filing_id`, the only way `source_text` changes is the extraction-repair path
   (`repair.py`), which is already `content_sha256`-guarded: a section whose
   fresh extract differs from the stored `source_text` is "damaged," and repair
   already re-summarises exactly those sections. Under lazy summarisation the
   rule is unchanged in spirit — a summary computed from stale `source_text` is
   invalid — but repair need only *invalidate* (clear) a damaged section's
   cached summary so it recomputes on next demand, rather than eagerly
   re-summarising during the repair run. Either is compatible with this ADR;
   the decided invariant is that a served summary always matches the
   `source_text`/sha it was derived from.

6. **Retrieval and embeddings stay eager at ingest.** Section extraction,
   `source_text` storage, and the Voyage embedding backfill are unchanged and
   continue to run for every ingested filing. They are the cheap substrate that
   keeps FIND, COMPARE, QA, and change detection working (ADR-0006, ADR-0007,
   ADR-0011, ADR-0012), and none of them is an answer-model call. Making
   retrieval lazy would break cross-company search for un-demanded companies —
   the opposite of the goal — so it is explicitly out of scope.

**Out of scope** (each its own future decision, named so the silence is
chosen):

- **The FMP judgment module** — a later ADR; this increment only stops the
  ingest-summarisation bleed and touches no market data.
- **A cheaper model tier for summarisation.** A model split already exists in
  the codebase (summarisation runs `claude-sonnet-5`; QA/COMPARE run
  `claude-opus-4-8`). Dropping summarisation further (e.g. to Haiku) is
  orthogonal to *when* summarisation runs and is flagged for its own follow-on
  ADR — it is not folded in here.
- **Any change-detection / COMPARE / QA redesign.** Their answer-model spend is
  *unaffected* by this ADR because they read `filing_chunks`, never summaries
  (see Consequences); this ADR does not reopen them.

## Compliance and Validation

For the guarantees to hold, each checked structurally where possible:

- **Ingest makes zero summarisation calls.** After this change, no code path
  reachable from `ResearchService.run()` calls `Summarizer.summarize()`.
  *Check:* a structural test that the ingest path holds no `summarize()` call
  (a fake summarizer that raises if invoked, driven through `run()`), analogous
  to how the suite proves FIND imports no answerer.
- **The cheap path stays cheap and unchanged.** QA, FIND, COMPARE, and change
  detection make the same number of answer-model calls as before (they never
  read summaries). *Check:* existing QA/FIND/COMPARE tests continue to pass with
  a summarizer that raises on use, proving none of them summarise.
- **`source_text` is written eagerly for every ingested section**, so the
  embeddings backfill and all retrieval are unaffected. *Check:* the ingest
  test asserts a section row with `source_text` exists post-ingest even though
  `summary` does not.
- **Honest absence is observable, not silent.** The summary surface reports
  not-yet-computed distinctly from computed. *Check:* an endpoint test that a
  freshly ingested, never-demanded filing returns the explicit
  not-yet-computed state, not a 500 and not an empty summary.
- **The wall is untouched.** No verdict field, no ranking, no evaluative Athena
  voice is added; summaries remain the same cited-prose artifact, only computed
  later. *Check:* wall-guard review of the diff; the summarizer prompt and
  schema are unchanged.

## Consequences

- **Changes:** ingest stops making the three `claude-sonnet-5` calls per filing;
  summarisation moves behind an explicit demand and is cached per `(filing_id,
  section)`. The eager `thesis_snapshots` write at ingest also goes: the thesis
  is a pure concatenation of the three section summaries (`compose_thesis`), so
  it can only be composed once those summaries exist — it becomes a
  compute-on-demand artifact assembled when its sections have been summarised.
- **Cost saving (order of magnitude):** eager summarisation is ~3
  answer-model calls × N filings, paid at ingest regardless of readership. At
  N=85 that is ~255 `claude-sonnet-5` calls already spent; an S&P 500 run would
  be ~1,500. Because the committed frontend reads *no* summary surface and the
  evidence layer never reads summaries, real demand is a small fraction of the
  corpus — so deferring to demand removes essentially the entire ingest-time
  summarisation spend and, with it, the term that made breadth unaffordable.
  The saving is proportional to how much of the corpus is never read as prose,
  which today is almost all of it.
- **Latency shift (accepted, bounded, and paid by the asker):** the first
  explicit demand for a filing now pays its summarisation cost inline — up to
  three sequential `claude-sonnet-5` calls at `max_tokens=16000` over long
  sections, i.e. seconds to tens of seconds — where today that request is a
  cheap DB read. This is the same trade ADR-0013 §5 accepted for opt-in rerank:
  the cost lands only on the caller who explicitly asked for the expensive
  artifact, exactly once per section, then never again (cache hit). It does
  **not** touch QA/FIND/COMPARE latency, which never summarise. *Remedy/knobs
  if first-demand latency bites:* summarise a filing's three sections
  concurrently rather than sequentially; the cheaper-model split (out of scope,
  flagged above) would also cut it.
- **Consumers that must tolerate a not-yet-computed summary** (every reader of
  `summary`/thesis today):
  1. `Repository.latest_research()` / `GET /companies/{ticker}/summary` — the
     demand path; must either compute-then-return or report honest absence,
     never assume a row's `summary` is present.
  2. `ResearchOutcome.summaries` and the `POST /research/{ticker}`
     `ResearchResponse.summaries` field — at ingest this is now empty (nothing
     computed yet); the response schema already tolerates `summaries={}` on the
     `skipped` path, so the shape holds, but the `ingested` case now also
     returns empty summaries.
  3. `compose_thesis()` / `thesis_snapshots` — no longer written at ingest;
     `thesis_snapshots` remains append-only, so a lazily-composed thesis is a
     later append, consistent with the existing repair-appends-a-snapshot
     pattern.
  4. `repair.stored_summaries()` — reuses unaffected sections' summaries when
     recomposing a thesis; must tolerate a section whose summary was never
     computed (nothing to reuse — the section stays lazy) rather than assuming
     every section has a stored summary.
- **Migration implication:** the `filing_summaries.summary NOT NULL` constraint
  (migration 0001) encodes the eager-existence assumption and must be relaxed so
  a section row can exist with `source_text` and no `summary` (equivalently, the
  section/`source_text` row and the summary row could be split into two tables —
  a cleaner separation of the eager substrate from the lazy derivative, at the
  cost of a larger migration and updates to `sections_pending_embedding`,
  `stored_summaries`, and repair's diff). The concrete migration is **not**
  written here; per the ADR-before-migration rule it follows acceptance of this
  ADR, which fixes the contract (a section may exist un-summarised) that the
  migration must express.
- **Unchanged:** all retrieval (embeddings, pgvector, FIND, COMPARE, QA, change
  detection) and its cost; the wall and every ADR-0007 grounding/citation
  guarantee; `filing_chunks` and its HNSW index; the summarizer prompt, model,
  and output schema (only *when* it runs changes); `content_sha256` and
  `period_end_date` identity; the append-only `thesis_snapshots` trigger.
- **Model-tier split is a separate follow-on ADR, not decided here.** The
  existing sonnet-vs-opus split shows the tier lever is already partly pulled;
  whether summarisation should drop further is orthogonal to lazy-vs-eager and
  is flagged, not folded in.
- **Build sequencing:** this ADR is draft-for-review only. On acceptance, the
  first increment is the contract-relaxing migration (drop `summary NOT NULL`)
  plus the ingest change (stop summarising) and the on-demand compute-and-cache
  path behind the summary surface, built mocked and reviewed before any live
  run, with the ingest-makes-zero-summarisation-calls structural test as the
  gate. FMP, the model-tier split, and any COMPARE/change-detection change wait
  for their own ADRs.
