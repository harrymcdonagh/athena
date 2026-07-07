# 0012 — COMPARE Mode: Cited Side-by-Side Over a Named Set, No Ranking

**Status:** Accepted (review findings folded in — see
`0012-compare-mode-REVIEW.md`)

## Context

ADR-0011 designed cross-company query as two modes and sequenced FIND first.
FIND is now built and live-validated: `GET /research/find` runs the stage-1
wide search, returns matching companies with cited passages, and spends zero
answer-model tokens — the caps are min()-clamped ceilings in code
(`WIDE_SEARCH_LIMIT=80`, `CANDIDATE_N=15`, `PASSAGES_PER_COMPANY=3` after the
recall-knob widening), and the zero-token contract is held structurally: the
FIND module imports no answerer, so a generated answer is unrepresentable
there. COMPARE is ADR-0011's second increment — bounded synthesis over a
small, explicitly named set — and this ADR decides its build shape inside the
bounds ADR-0011 already drew (≤5 companies, refusal over truncation, funnel
retrieval, grounding and the judgment wall intact).

Three facts from FIND's build and live validation bear directly on COMPARE:

1. **Third passages drift off-topic.** `PASSAGES_PER_COMPANY` stayed at 3
   during the knob widening precisely because third passages were observed
   drifting off the query topic (commit 3687d8d). In FIND that drift is
   visible and cheap: an off-topic passage is one extra row the user reads
   and discards. In COMPARE the same passage becomes *input to synthesis* —
   the drift lands inside a generated column where the user cannot see it
   arrived off-topic. One caveat the sizing below must respect: that drift
   was measured in FIND's stage-1 shape — a company's passages there are its
   chunks that survived the *global* wide pool on broad thematic queries.
   COMPARE's stage 2 is a different retrieval (scoped inside one filing),
   whose rank-3 quality has never been measured live. The drift finding
   motivates caution; it does not by itself size the stage-2 constant.

2. **Duplicate-chunk and boilerplate noise are real.** The GOOG-on-AI miss
   (generic technology-risk boilerplate from other filers outscoring GOOG's
   genuine AI-risk passage) is the motivating specimen for a future
   FIND-quality increment (passage dedupe, relevance rerank / section-aware
   scoring). The same noise that pollutes a FIND shortlist pollutes a COMPARE
   column.

3. **FIND shipped stage 1 only.** ADR-0011 §2's stage-2 focused per-company
   retrieval was designed but never built — FIND returns straight from the
   wide search (no stage-2 re-retrieval, per its module contract). COMPARE is
   therefore the *first live use* of stage 2, not a reuse of validated FIND
   machinery. What does exist and is validated is the filing-scoped retrieval
   machinery ADR-0009 §2 built for change detection (retrieval bounded to an
   explicit set of filing ids); decision #3 builds stage 2 on that.

COMPARE is the first cross-company mode that spends answer-model tokens at
all — FIND's contract is zero. ADR-0011 §3's posture therefore applies with
full force: the spend must be bounded by design, structurally, and the
expensive path must require naming a small set on purpose.

## Decision

1. **COMPARE operates on an explicitly named set of tickers, deduplicated to
   companies, capped at 5 — and an over-cap request is REFUSED, never
   truncated.** Callers name ticker symbols (the resolution key ADR-0010
   established; free-text company names are not an input). COMPARE performs
   no FIND-style discovery. The named symbols are normalized and
   deduplicated, then resolved against `sec_ticker_reference` — a cheap
   Postgres cache read (ADR-0010 §2), not retrieval work — and the resolved
   names are deduplicated again **by CIK**: two tickers on one CIK (GOOG and
   GOOGL both resolve to Alphabet) are one company and get one entry, labeled
   with the first-named symbol, never two duplicate columns. The cap applies
   to this deduplicated set — CIK-level for resolved names, symbol-level for
   unresolved ones — and it counts every deduplicated asked-for name,
   resolvable or not: a 7-name request is refused even if only 4 names would
   resolve, because the bound is on the request the caller made, not on the
   workload triage happens to leave. More than 5 returns a clear refusal that
   states the cap and asks for a named set of at most 5, before ANY retrieval
   or answer-model work runs (reference-cache resolution is the one step that
   precedes the check, because CIK dedup requires it; it spends no
   answer-model tokens and touches no evidence tables). This restates
   ADR-0011 §3 as a build commitment: a comparison quietly cut to the first 5
   that the user believes is complete is exactly the silently-wrong result
   this project refuses everywhere (ADR-0009 §5's no-manufactured-change,
   ADR-0010 §5's reported-never-silent symbol errors). Refusal is honest
   absence; truncation is silent wrongness.

2. **The response is ONE ordered list of typed entries — one entry per
   deduplicated asked-for name, in caller order — so a failure cannot be
   rendered around.** Each name resolves in two steps with two distinct
   failure categories, mirroring the two-table meaning of ADR-0010 §2:
   - *unresolved* — the symbol does not resolve against
     `sec_ticker_reference` (the same category batch ingestion already
     reports);
   - *no_evidence* — it resolves, but Athena holds **no ingested 10-K** for
     it. (Defined on the 10-K, not on "no ingested filings": a company can
     have a reference row and no `companies` row, a `companies` row and no
     filings, or — once 10-Qs land per ADR-0008 — filings but no 10-K to pin
     a column to. All three are the same fact for COMPARE: no column can
     speak.)

   Every entry in the list carries a type tag — `column` (decision #4),
   `no_finding` (decision #5), `unresolved`, or `no_evidence` — and the
   response carries a top-level partial-status flag when any entry is a
   failure. The invariant, asserted in tests:
   `len(entries) == len(deduplicated asked-for names)`, always. COMPARE
   proceeds for the resolvable companies; proceeding is acceptable only
   because the absence is loud, categorized, and **structurally impossible to
   miss**: failures occupy their position in the same sequence the columns
   live in, so a consumer cannot draw the successful columns without
   iterating past the failed names — there is no side list a renderer can
   forget to read. (A columns array plus a separate failure list was
   considered and rejected: it lets a renderer show a clean 3-column
   comparison for a 4-name request — the refused silent truncation
   reincarnated one layer up — and it breaks the caller-order positional
   correspondence decision #4 promises.) The in-sequence shape also makes
   decision #2-vs-#5 unconfusability a *type tag*, not a location in the
   payload: "we hold this filing and it is silent here" and "we never had the
   filing" are different tags on the same list, unconfusable by construction.

3. **Retrieval is filing-pinned: resolve each company's latest 10-K first,
   then retrieve scoped to that filing id.** The caller's naming replaces
   ADR-0011 §2's stage-1 discovery; what remains is stage 2, built here for
   the first time (Context #3). The mechanism, in order:
   - *Pin the filing:* per resolved company, select the most recent ingested
     10-K — the greatest filing in ADR-0008 §1's ordering
     (`period_end_date`, then `filing_date`, then `accession_number`; the
     *latest filing* definition of ADR-0008 §3), restricted to annual-report
     form types. When the corpus holds both a 10-K and a 10-K/A for the same
     period, **the amendment speaks for the period**: a 10-K/A is the filer's
     corrected statement, and synthesizing from the superseded original while
     holding the correction would be silent wrongness; the citation carries
     the actual form type and period, so a column speaking from an amendment
     says so.
   - *Retrieve inside the pinned filing:* scoped retrieval bounded to that
     single filing id, via the filing-scoped retrieval machinery ADR-0009 §2
     established for change detection (retrieval over an explicit
     `filing_ids` set). Explicitly NOT the ticker-scoped `search_chunks`
     path: the corpus holds multiple 10-Ks per company (AAPL carries FY2024
     and FY2025), so a ticker-scoped search returns a cross-period blend —
     inside a synthesized column, exactly the invisible failure pinning
     exists to prevent. A column speaks for one filing, cited with its
     period; cross-period change stays ADR-0009's job, and unlike filing
     vintages across companies are visible on their citations, never blended
     silently.

   **Per-company evidence budget:** a fixed, min()-clamped stage-2 passage
   cap, the same ceiling mechanism as FIND's constants — a caller can narrow
   it, never widen it past the named constant. Total answer-model input is
   therefore bounded by design: ≤ 5 companies × the per-company cap.

   **Coverage is reported mechanically on every column.** Each column entry
   carries a retrieval-fact coverage signal: how many passages in the pinned
   filing qualified under the stage-2 relevance floor versus how many were
   consulted — "2 of 9 qualifying passages consulted." This costs zero
   answer-model tokens and passes the ADR-0011 §5 test the same way FIND's
   match_strength does: it is a labeled fact about text retrieval, not a
   judgment about a company. It exists because a starved column does NOT
   reliably fail visibly on its own: against a filing with rich disclosure on
   the queried dimension, a 2-passage column is fluent, fully cited, and
   complete-*looking* — under-coverage wearing the costume of a complete
   answer. The coverage signal converts that invisible failure into a
   self-reporting one: thinness becomes legible as "2 of 9" whether or not
   the prose looks thin.

   **Recommended value: 2 passages per company — flagged as an open question
   for live validation.** Grounded on two legs, and honest about what each
   carries: (i) FIND's validation found third passages drifting off-topic
   (commit 3687d8d), and COMPARE cannot tolerate drift the way FIND does,
   because the drift lands inside a synthesized column (Context #1) — though
   that measurement is of stage-1's global-pool shape, and stage-2
   filing-scoped rank-3 quality has no live evidence yet; (ii) the coverage
   signal is what makes the narrow budget an honestly-bounded choice rather
   than a guess — with starvation self-reporting, the visible-failure side of
   honest-absence-over-silent-wrongness is actually secured, not assumed. The
   constant is *earned* by the validation regime below (including the
   known-rich specimen and the wider-retrieval diff), tuned at the constant
   after live validation, never via a runtime argument drifting upward. Cost
   is not the binding argument — 2 vs 3 passages across ≤5 companies is a
   trivial token delta; this is a quality knob.

4. **Output is a cited side-by-side of what each filing says — not a ranked
   verdict.** Per company, a `column` entry carries factual, attributed
   statements of what that company's pinned filing says on the queried topic,
   every stated claim citing its `source_url`. ADR-0007's per-company rules
   apply inside each column (attributed evaluative language fine, Athena's
   own voice banned). Constraints that hold the ADR-0011 §5 wall:
   - No superlatives or ranking across companies: no most/least/best/worst/
     stronger/weaker, no ordering-by-attractiveness. The §5 test governs:
     stating what each filing says, cited, is evidence work; the system
     picking "most" requires judging magnitude or desirability ACROSS
     companies and is out.
   - No cross-referential prose ("unlike A, B…") in this increment, on its
     own merits: comparative prose is the on-ramp to ranking language — 
     "unlike A, B faces…" sits one adverb from "B is worse positioned," and
     holding that line inside generated prose is exactly the
     prompt-discipline burden this ADR refuses to take on (decision #6
     forecloses it structurally, at no cost since it is excluded here
     anyway). If a future increment wants comparative prose, that is a
     deliberate reopening of decision #6's enforcement shape, in its own ADR.
   - **Entry order follows the caller's named order** — automatic under
     decision #2's shape, since entries are per-asked-name in caller order.
     No computed ordering of any kind — not even retrieval similarity. FIND
     orders by match_strength because there it is a labeled retrieval fact
     about text; in a side-by-side synthesis, position reads as meaning, so
     any computed order would leak a strength signal the system must not
     emit.
   - Citations are stamped mechanically, per the draft/resolved split that
     ADR-0009's change-detection implementation established (in
     `apps/api/research/qa.py`; spelled out here so the precedent is legible
     from this directory, per ADR-0004): the answer model's draft carries
     chunk LABELS only (C1, C2, …) — no URLs, no dates — and provenance
     (`source_url`, period) is resolved from the database at stamp time; a
     claim whose labels do not resolve is not emitted, and the drop is
     reported on the warnings channel. Never model-copied URLs.

5. **No-finding-on-a-dimension is a first-class outcome — and both of its
   causes are auditable.** A `no_finding` entry states explicitly that the
   pinned filing does not address the queried dimension, rather than
   manufacturing content to appear useful — the cross-company analog of
   ADR-0009 §5's no-change outcome and ADR-0007 §4's insufficient-evidence
   answer. The entry distinguishes its two causes, riding on decision #3's
   coverage machinery rather than any new mechanism:
   - *retrieval-empty* — zero passages in the pinned filing qualified under
     the relevance floor. A mechanical retrieval fact, already legible from
     the coverage signal ("0 qualifying"); no model was consulted and no
     model judgment is involved.
   - *model-declined* — passages qualified and were consulted, but the model
     emitted no claims on the dimension. This is a model judgment, and it
     would otherwise be COMPARE's one uncited, unauditable output; so the
     entry attaches the consulted passages (already in hand from the
     coverage machinery) as consulted-but-uncited, letting the user audit the
     decline exactly the way they audit a claim — read what the model saw and
     judge whether "silent here" was right.

   Both causes are honest; they are different facts, and both are distinct
   from decision #2's failure categories: "we hold this filing and it is
   silent here" is evidence; "we never had the filing" is a failure. The type
   tags keep all of them unconfusable.

6. **How the wall is held — the load-bearing choice.** Two candidate
   enforcement shapes; both are presented so the choice can be
   pressure-tested at review:

   - **(a) Per-column synthesis behind a filing-scoped seam.** One
     answer-model call per company. The seam's signature is
     `(filing_id, query)` — the synthesis function takes a single pinned
     filing id plus the query and performs its own single-filing scoped
     retrieval internally. Evidence enters a call ONLY via that retrieval:
     there is no passage parameter at all, so there is structurally no path
     through which another company's evidence — or another period's — can
     enter, and no caller loop whose discipline the guarantee depends on. (A
     seam accepting a passage list was considered and rejected as weaker: a
     list parameter cannot know its passages are one company's; the
     one-company property would live in the caller's loop, and a caller bug
     passing a merged list type-checks fine. The `(filing_id, query)`
     signature makes the forbidden input unrepresentable at the seam — the
     same class of assertion as FIND's no-answerer-import.) Cross-company
     ranking becomes structurally impossible: the model cannot rank companies
     it never sees together. Two invariants complete the wall: **column
     assembly is model-free** — entries are assembled by code, mechanically —
     and **no answer-model output re-enters an answer-model call**. The ≤5
     call bound alone would not foreclose a sixth call summarizing the five
     columns, which is precisely where a ranking would leak back in; the
     no-re-entry invariant does. Cost: up to 5 calls instead of 1 (the
     evidence tokens are the same either way; the extra spend is the per-call
     instruction overhead, ×5), and cross-referential comparative prose is
     foreclosed — including its benign form, and with it any model-side
     alignment of dimensions across columns (company A's column may organize
     around "supply chain" while B's says "logistics"; rows do not
     necessarily line up).
   - **(b) Single combined call with prompt guardrails.** One call over all
     companies' passages, instructed not to rank. Cost: the wall is held by
     prompt discipline, not structure. ADR-0007's own Consequences flagged
     this fragility — prompt wording becomes load-bearing policy — and
     multi-company evidence in one context is precisely the input that
     tempts a model toward "most exposed" phrasing. Benefit: one call, and
     the model can align dimensions across columns so rows correspond.

   **Recommendation: (a).** Three reasons. First, structural-over-test
   enforcement is the house norm — FIND's no-answerer-import, ADR-0009 §7's
   explicit flag over inference, ADR-0011 §3's caps as design invariants;
   (a) is that same move at COMPARE's one dangerous seam, making the
   forbidden output unrepresentable instead of prohibited. Second, the thing
   (a) forecloses — cross-referential prose — is excluded from this
   increment on decision #4's own on-ramp-to-ranking grounds, so the
   foreclosure costs nothing now; reopening it is a future ADR's deliberate
   choice, not a prompt tweak. Third, the call-count cost is bounded and
   small (≤5 calls over ≤2 passages each). The real price of (a) is
   unaligned dimensions across columns, accepted as a risk below.

**Out of scope** (future work, each its own decision): cross-referential
comparative prose and model-side dimension alignment across columns (both
require reopening decision #6's enforcement shape); any ranking or scoring by
desirability (judgment layer, per ADR-0011); the FIND-quality increments —
content dedupe, relevance rerank / section-aware scoring — which improve
COMPARE columns for free but are NOT prerequisites (decision #3's budget is
sized for today's retrieval quality); a staleness disclosure for when a
company's pinned 10-K is older than other evidence the corpus holds (once
10-Qs land per ADR-0008, "a newer filing exists and was not consulted" is a
disclosure fact a column may need to carry — named here so the current
silence is chosen, not accidental); persistence of comparison results (the
ADR-0009 §1 posture: QA-time capability, no table, no migration); and the
daily briefing.

## Compliance and Validation

What must be TRUE for this ADR's guarantees to hold, and how each is checked:

- **The wall is structural.** Under the recommended (a), the synthesis seam's
  signature is `(filing_id, query)` and it performs its own single-filing
  retrieval — there is no parameter through which any passage, let alone
  another company's, can be injected into a call. Asserted the way FIND's
  zero-model contract is asserted: on the seam's signature and imports, not
  by output-scanning tests alone. Additionally asserted: entry assembly is
  model-free, and no answer-model output is input to any answer-model call.
  Verdict-language detection (ADR-0007 Consequences) remains the backstop for
  within-column editorializing, which structure cannot prevent.
- **The refusal is a request-layer rejection.** >5 deduplicated names
  (CIK-deduplicated after reference-cache resolution, per decision #1) is
  rejected with a message naming the cap before any retrieval or answer-model
  work runs. Tested: an over-cap request must never produce a result at all —
  the failure shape to guard against is a truncated comparison.
- **Failures are categorized, loud, and impossible to render around.** The
  response is one ordered entries list; unresolved and no-evidence names each
  land as their own typed entry, in caller order, in the same sequence as the
  columns — structurally distinguishable by type tag from decision #5's
  honest no-finding. Tested invariant:
  `len(entries) == len(deduplicated asked-for names)`, with the partial flag
  set whenever any entry is a failure. A set mixing resolvable, unresolved,
  and no-evidence names yields one list with nothing dropped and nothing
  relegated to a side field.
- **Evidence is bounded by ceilings.** The per-company passage cap is a
  min()-clamped constant like FIND's; a runtime argument can narrow, never
  widen. Total answer-model input per query is bounded by 5 × the cap.
- **Coverage is reported on every column.** Each column entry carries
  qualifying-vs-consulted counts from the pinned filing; a no_finding entry
  carries the retrieval-empty / model-declined distinction, with consulted
  passages attached in the model-declined case. Tested: the counts are
  retrieval facts computed with zero answer-model involvement.
- **Every stated claim is cited**, provenance stamped from the database via
  the draft/resolved split (decision #4: model drafts labels; the resolver
  stamps `source_url` and period from the database) — a claim that cannot
  cite its passage is not emitted, with the existing warnings channel as
  backstop (ADR-0009's mandatory-citation posture and its Consequences'
  warnings backstop).
- **Live-validate-twice, with under-reporting explicitly in scope.** The
  behavioral work — column quality, the passages-per-company open question,
  the no-finding shapes — is validated against the real corpus, and any
  behavioral fix is re-validated twice: confirm the defect cleared AND that
  the adjacent legitimate behavior still emits. (Precedent: ADR-0009's
  change-detection validation — a false no-change fix was re-validated live
  twice per question, the contradiction clearing in both passes while the
  legitimate no-change case still emitted; recorded in commit 7973c0a, spelled
  out here so the pattern is legible from this directory.) Validation MUST
  additionally: (i) include a specimen with known-rich disclosure on the
  queried dimension — GOOG-on-AI is the named specimen (Context #2) — and
  (ii) diff each 2-passage column against a wider filing-scoped retrieval
  (k=6–8) from the same pinned filing, checking for material omissions.
  Under-reporting is the defect that *looks like success*: a good column and
  an under-coverage column are indistinguishable without the wider-retrieval
  diff, and it would otherwise pass both validation passes.

## Consequences

- **Changes:** Athena spends answer-model tokens on a cross-company query
  for the first time — bounded, not zero. FIND's contract (zero) is
  untouched; COMPARE's contract is *bounded by structure*: ≤5 companies ×
  ≤2 passages, ≤5 synthesis calls each seeing exactly one pinned filing,
  refusal beyond. A COMPARE endpoint and response shape are added alongside
  the existing paths: one ordered list of typed entries in caller order
  (cited columns with mechanical coverage counts, honest no-finding entries,
  categorized failures), plus a top-level partial flag. ADR-0011 §2's stage 2
  is built for the first time, on the filing-scoped retrieval machinery
  ADR-0009 §2 established. No table, no migration — per house rules none may
  be written before this ADR is accepted, and none is needed after.
- **Unchanged:** FIND in its entirety (knobs, zero-token contract,
  match-strength ordering — a retrieval fact remains fine THERE); every
  ADR-0007 guarantee; per-company QA and change detection (ADR-0009); the
  judgment-layer wall (ADR-0011 §5); ingestion, embeddings, and the schema.
- **Risk accepted:** *Unaligned columns* — per-column synthesis means rows
  may not correspond across companies; the user aligns dimensions by
  reading, which is the pedagogical posture anyway (ADR-0007's rationale:
  teach the reasoning, don't pre-digest it). *A starved column* — 2 passages
  may miss a filing's relevant statement; the coverage signal makes that
  starvation self-reporting ("2 of 9") rather than dependent on the column
  looking thin, and the residual risk shifts to the relevance floor itself
  (a floor that mis-scores a passage under-counts the qualifying total); the
  remedy is tuning the constant and the floor after validation, never
  unbounding them. *Column noise* — duplicate-chunk and wrong-topic-passage
  noise (the GOOG-on-AI class) degrades COMPARE columns exactly as it
  degrades FIND shortlists; the content-dedupe and
  reranker/section-aware-scoring increments apply to both and are
  non-blocking follow-ups, not prerequisites. *Amendment coverage* — the
  10-K/A-speaks-for-the-period rule (decision #3) is only as good as
  amendment ingestion; a held original plus an unheld amendment silently
  speaks from the superseded text, which is the general
  corpus-completeness risk, not a COMPARE-specific one.
- **Build sequencing:** COMPARE is built only after this ADR is accepted. It
  is the first build of ADR-0011 §2's stage 2 (FIND shipped stage 1 only),
  constructed on ADR-0009 §2's filing-scoped retrieval. First increment:
  ticker resolution with CIK dedup and categorized failures, refusal at the
  cap, filing-pinned per-column synthesis at 2 passages per company with the
  coverage signal, live-validated per the section above — resolving the
  passages-per-company open question with corpus evidence (including the
  known-rich specimen and the wider-retrieval diff) before any knob moves.
  Nothing here presumes comparative prose, the briefing, or the judgment
  layer.
