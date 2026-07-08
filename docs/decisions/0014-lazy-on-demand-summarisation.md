# 0014 — Lazy, On-Demand, Cached Filing Summarisation

**Status:** Accepted

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

1. **`source_text` stays EAGER; only the `summary` answer-model call goes
   lazy — the two split.** The `filing_summaries` row bundles two things of
   opposite cost: `source_text` (the raw extracted section text, written with no
   model call) and `summary` (the `claude-sonnet-5` output). `source_text` is
   the **retrieval substrate** — `Repository.sections_pending_embedding` selects
   it and `embeddings.run_backfill` embeds it (repository.py:198–219,
   embeddings.py:100–114), so every FIND, COMPARE, QA, and change-detection
   answer ultimately rests on it. **Deferring the whole row is therefore WRONG:
   it would starve the embeddings backfill and corrupt retrieval for every
   un-demanded filing — the exact opposite of the goal.** Ingest continues to
   write `source_text` for all three sections eagerly and to embed it; what
   defers is the per-section `summary` call alone. The lazy unit is the
   `summary` field, never the row. This is the load-bearing distinction the rest
   of the ADR builds on.

2. **Summarise a section's `summary` on first explicit demand, then cache;
   ingest stops summarising.** At ingest, `ResearchService.run()` extracts each
   section and stores its `source_text` (per #1) exactly as today, but makes
   **zero** `summarize()` calls — the three `claude-sonnet-5` calls per filing
   leave the ingest path. A section's `summary` is computed the first time it is
   explicitly demanded (decision #4) and cached in place; every subsequent read
   is a cache hit with no model call. The rejected alternative — keep eager
   summarisation but add a cache — saves nothing, because the eager pass is
   exactly the spend we are trying not to make.

3. **The cache unit is the section; the cache key is `(filing_id, section)` —
   the identity that already exists.** `filing_summaries` already carries
   `UNIQUE (filing_id, section)` (migration 0001). Per-section, not per-filing,
   is the right grain: sections are summarised independently today, repair
   already operates per damaged section, and a demand for one section should not
   force the other two. No new identity is invented — `filing_id` already
   encodes the filing (a unique `accession_number` with its `content_sha256`),
   and `period_end_date` already encodes the period, so the sha/period identity
   the codebase relies on carries over unchanged.

4. **Demand is a synchronous compute-on-read at the summary surface: compute
   inline, cache, return — and it is never inferred from a QA/FIND/COMPARE
   call.** On a request to the summary surface
   (`GET /companies/{ticker}/summary`, and the `POST /research/{ticker}`
   response if a caller asks it to return prose) for a section whose `summary`
   is still pending, Athena computes that section's summary **inline within the
   request**, writes it to the cache, and returns it in the same response: the
   caller waits once, and every later read is a cache hit. Background/async
   precomputation (a worker queue, a warm-the-cache job) is deliberately **out
   of scope** — a breadth-era follow-on; at the current corpus size synchronous
   compute-on-read is sufficient and keeps the control flow legible. Demand is
   **not** a QA question, a FIND query, a COMPARE column, or a change-detection
   run — those read `filing_chunks` and must continue to make **zero**
   summarisation calls. This mirrors the explicit-not-inferred posture of the
   compare flag (ADR-0009 §7: change detection is requested, never inferred from
   question text) and the opt-in rerank flag (ADR-0013 §5): the expensive path
   is entered by an explicit act, not guessed from a cheap one. The rejected
   alternative — letting a QA call lazily trigger summarisation of whatever
   filings it retrieved from — is refused: it reintroduces answer-model spend
   into the cheap, frequent path and couples QA cost to corpus size, the exact
   coupling this ADR removes.

5. **A pending summary is `summary IS NULL`; the schema change is a nullable
   `summary` column, not a split table.** A not-yet-computed summary is a
   first-class honest-absence state, not an error and not a fabricated blank —
   the same posture as the 16 known-absent filers (CLAUDE.md; honest absence
   over silent wrongness) and COMPARE's `no_finding` / `no_evidence`
   (ADR-0012 #5): the state is *reported as what it is* — "not yet summarised" —
   never a 500, never an empty string presented as if the model returned it. The
   storage expression is: **drop the `summary NOT NULL` constraint** so a
   `filing_summaries` row can carry its eager `source_text` with `summary` still
   `NULL`, meaning "pending, not yet summarised." This is unambiguous **because a
   real summary is always 300–500 words of markdown** (the summarizer's own
   contract, `summarizer.py` `build_prompt`) and never NULL — so `summary IS
   NULL` can only mean pending, never "the model produced nothing." The rejected
   alternative — splitting the `source_text` row and the summary into two
   tables — is cleaner in the abstract but buys nothing here: it duplicates the
   `(filing_id, section)` identity, forces changes to
   `sections_pending_embedding`, `stored_summaries`, and repair's diff, and a
   larger migration, all to distinguish a state a single nullable column already
   expresses without ambiguity.

6. **A cached summary is valid for the exact `source_text` (and thus
   `content_sha256`) it was computed from; when that changes, it is
   invalidated, not silently served.** A new filing is a new `accession_number`
   → a new `filing_id` → no cached summary, so new filings are summarised on
   demand by construction, reusing existing filing identity. For the *same*
   `filing_id`, the only way `source_text` changes is the extraction-repair path
   (`repair.py`), which is already `content_sha256`-guarded: a section whose
   fresh extract differs from the stored `source_text` is "damaged," and repair
   already re-summarises exactly those sections. Under lazy summarisation the
   rule is unchanged in spirit — a summary computed from stale `source_text` is
   invalid — but repair need only *invalidate* (set `summary` back to `NULL`) a
   damaged section's cached summary so it recomputes on next demand, rather than
   eagerly re-summarising during the repair run. Either is compatible with this
   ADR; the decided invariant is that a served summary always matches the
   `source_text`/sha it was derived from.

7. **Retrieval and embeddings stay eager at ingest (the #1 corollary at the
   pipeline level).** Section extraction, `source_text` storage, and the Voyage
   embedding backfill are unchanged and continue to run for every ingested
   filing. They are the cheap substrate that keeps FIND, COMPARE, QA, and change
   detection working (ADR-0006, ADR-0007, ADR-0011, ADR-0012), and none of them
   is an answer-model call. Making retrieval lazy would break cross-company
   search for un-demanded companies — the opposite of the goal — so it is
   explicitly out of scope.

**Out of scope** (each its own future decision, named so the silence is
chosen):

- **Async/background precomputation of summaries** — a worker queue or
  cache-warming job; a breadth-era follow-on. This ADR does synchronous
  compute-on-read only (decision #4).

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
  test asserts a section row with a non-null `source_text` exists post-ingest
  even though its `summary IS NULL` (pending). `source_text` remains `NOT NULL`
  — the load-bearing retrieval invariant — while only `summary` becomes
  nullable.
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
- **Migration:** migration 0005 drops the `filing_summaries.summary NOT NULL`
  constraint (decision #5) so a section row can carry its eager `source_text`
  with `summary IS NULL` = pending; `source_text` stays `NOT NULL`. `DROP NOT
  NULL` relaxes a constraint and **rewrites no rows**, so the 85 filings already
  summarised keep their `summary` text and stay non-pending — zero recompute, no
  re-spend — and only newly-ingested filings arrive pending. The `down` restores
  `SET NOT NULL`, which fails loudly if any pending (NULL) summary exists rather
  than silently dropping the lazy contract — the honest outcome, mirroring
  migration 0003's `period_end_date NOT NULL` pattern.
- **Unchanged:** all retrieval (embeddings, pgvector, FIND, COMPARE, QA, change
  detection) and its cost; the wall and every ADR-0007 grounding/citation
  guarantee; `filing_chunks` and its HNSW index; the summarizer prompt, model,
  and output schema (only *when* it runs changes); `content_sha256` and
  `period_end_date` identity; the append-only `thesis_snapshots` trigger.
- **Model-tier split is a separate follow-on ADR, not decided here.** The
  existing sonnet-vs-opus split shows the tier lever is already partly pulled;
  whether summarisation should drop further is orthogonal to lazy-vs-eager and
  is flagged, not folded in.
- **Build sequencing:** this round lays acceptance + the migration only; the
  migration is written but **not applied**, and no ingest/caching code lands
  yet. The next increment (a later session) is the ingest change (stop
  summarising) and the synchronous on-demand compute-and-cache path behind the
  summary surface, built mocked and reviewed before any live run, with the
  ingest-makes-zero-summarisation-calls structural test as the gate. FMP, the
  model-tier split, async precomputation, and any COMPARE/change-detection
  change wait for their own ADRs.
