# Build review — COMPARE mocked build (f7c221a) vs accepted ADR-0012

Reviewer: Claude (session 2026-07-07). Findings only; no code changed, no
live call made. Confidence flagged per finding — Harry overrides freely.

Verified against: `apps/api/research/compare.py`, `repository.py`,
`router.py`, `test_compare.py`, the COMPARE endpoint tests in
`test_router.py`, ADR-0012's Compliance section, and the wall-guard /
live-validation skill checklists.

---

## Structural properties: verified as structure

- **The wall seam holds by type.** `synthesize_column(engine, embedder,
  answerer, *, filing_id, query, passages_per_company)` — read directly, not
  from the test: there is no parameter that admits a passage, a chunk list,
  or a second company's anything. Its only retrieval path is
  `Repository.search_chunks_in_filing`, whose SQL is `WHERE fc.model = :model
  AND fc.filing_id = :filing_id` — one filing per call, so a cross-company or
  cross-period blend inside a synthesis call is unrepresentable, the same
  class of foreclosure as FIND's no-answerer-import. The module imports only
  `Embedder`, `ChunkMatch`/`PinnedFiling`/`Repository`, dataclasses, and
  pydantic — nothing anthropic-shaped. The signature test is a regression
  pin on structure, not the structure itself. Confidence: high.
- **No re-entry, model-free assembly.** `ColumnDraft` appears in the module
  only as the answerer's RETURN type and inside the plain-code resolve loop;
  no function in `compare.py` or `router.py` accepts one as input, so a
  "summarize the five columns" call has no representable input. Assembly in
  `compare_companies` is pure Python; the call-count test (3 companies → 3
  calls exactly) pins it. Caveat, for honesty about what kind of guarantee
  this is: nothing type-level prevents a future dev *adding* a summarize
  seam — the invariant is module shape + docstring + test, same as FIND's
  posture. Acceptable; it is the house norm. Confidence: high.
- **Refusal order matches the ADR.** Code order in `compare_companies`:
  input validation → symbol normalize/dedup → reference-cache reads
  (`resolve_ticker_from_reference`, sec_ticker_reference only) → CIK dedup →
  cap check raising `CompareRefusal` → THEN `latest_annual_filing` pinning →
  then synthesis. Nothing embeds or touches `filing_chunks` before the cap
  check; the exploding-embedder/answerer fakes corroborate what the code
  order shows. The cap counts unresolved names (they enter `named` with
  cik=None before the check). One note: refusal-precedes-retrieval is held
  by *code order* pinned by tests, not by type — there is no cheap way to do
  better, and the ADR's Compliance section anticipated exactly this.
  Confidence: high.
- **Entries invariant and prominence.** One `entries.append` per `named`
  element, loop in caller order, plus `assert len(entries) == len(named)`
  and the test invariant. Unresolved/no_evidence are typed entries in the
  same list; the response model has no side failure list to render around.
  (Trivial note: the runtime `assert` is stripped under `python -O`; the
  invariant also holds by construction, so this is cosmetic.) Confidence:
  high.
- **Pinning.** `latest_annual_filing` filters `form_type IN ('10-K',
  '10-K/A')`, orders by the ADR-0008 §1 triple, `LIMIT 1`: the two-10-K AAPL
  test proves single-period columns; a 10-K/A sharing the period wins on the
  filing_date tie-break; `None` (→ `no_evidence`) means no 10-K held — the
  10-Q-only test pins "no 10-K", not "no filings". I checked the
  NULLS-FIRST-on-DESC ordering hazard for `period_end_date`: void — migration
  `0003_period_end_date_not_null.py` makes the column NOT NULL (ADR-0008's
  promised guarantee, delivered). Confidence: high.
- **Response models carry no rank/score/ordering field.** Entry fields are
  kind, symbol, company_name, cik, filing, statements, coverage,
  no_finding_cause, consulted_passages, warnings. `similarity` on a
  consulted passage is a labeled retrieval fact (FIND precedent) attached
  for audit, not an ordering. Entries order is caller order. Gap: FIND has
  `test_find_response_cannot_carry_a_ranking_or_synthesis` asserting a
  forbidden-field set against its response models; COMPARE has no mirror
  test. Cheap to add, worth having as the regression tripwire the wall-guard
  checklist expects. Confidence: high (gap is real, severity low).

---

## Named target 1 — the 0.20 coverage floor

**Verdict: NOT benign. The floor sits inside the displayed honesty signal —
in the denominator — and it also gates the model call. It cannot inflate N,
but it deflates M, and a deflated M inflates the derived fraction: a filing
with eight relevant-but-weak passages and two above-floor ones displays
"2 of 2", which reads as exhaustive coverage. That is the legible thinness
the signal exists to expose, re-hidden by an unreviewed constant.**
Confidence: high on the mechanism; medium on how often the corpus will
actually produce it.

Specifics:

1. **What 0.20 clamps.** `qualifying = [c for c in scanned if 1.0 -
   c.distance >= QUALIFYING_SIMILARITY_FLOOR]`; `coverage.qualifying =
   len(qualifying)`; consulted is the top-2 *of qualifying*. So the floor
   defines the denominator of "N of M" AND the eligibility of passages to be
   consulted at all. It is not an internal-only threshold.
2. **The false-completeness direction.** "2 of 9" self-reports starvation;
   "2 of 2" self-reports completeness. If genuinely relevant passages score
   0.15–0.19, the display flips from the first to the second with no other
   observable change. The ADR added this signal precisely because a starved
   column is otherwise fluent and complete-looking; a too-high floor
   restores that failure while the mechanism appears to be working.
3. **The false-silence direction (worse).** If ALL of a filing's genuine
   passages score just under 0.20, `retrieval_empty` fires: a first-class
   "the filing does not address this" — manufactured by a threshold, with
   the model never consulted. HD's genuine tariff match scored 0.2397 in
   FIND's validation, i.e. real matches live within 0.04 of this floor.
4. **The calibration evidence is transplanted.** 0.2397 came from FIND's
   stage-1 *global* ranking. Per-filing scoped similarity distributions have
   never been observed (stage 2 has never run live) — the same
   evidence-transplant class the ADR review flagged for the passage cap. The
   floor is a guess wearing a citation.
5. Minor, same family: `COVERAGE_SCAN_LIMIT = 24` saturation makes
   `qualifying` a floor-of-a-count; the code comment says so but the
   response doesn't. Direction of error is mostly honest (M understated on
   rich filings ⇒ column looks *more* starved), so low severity.

What to do about it (Harry's call; options, not edits): (a) put
`QUALIFYING_SIMILARITY_FLOOR` explicitly on the live-validate agenda at the
same rank as the passage cap — the k=6–8 omission diff should specifically
check whether omitted-material passages score below 0.20, and the specimen
set should include a weak-but-real match (the HD-tariff class) to catch
false `retrieval_empty`; (b) consider making the display floor-independent
or floor-legible — e.g. also report the scanned count, or the floor value
itself, in the coverage object — so "2 of 2" cannot silently mean "2 of 2
above an uncalibrated threshold"; (c) at minimum the constant's comment
should say it is unvalidated for stage-2 distributions, which is the fact.

---

## Named target 2 — the draft/resolved drop path

**Verdict: (a) the path exists and is correct for what it enforces; (b) a
column that loses claims degrades honestly — partially-dropped columns emit
with warnings, fully-dropped columns become `model_declined` with the
consulted passages attached; (c) there is no path for an uncited statement
to reach a column. BUT the live-validate expectation as phrased in the
review brief over-promises: this mechanism enforces citation *validity*
(labels present and resolvable), not semantic *support*. A live model that
asserts something a passage does not say, while citing that passage's
label, sails through the drop path by design.** Confidence: high.

Read of the code: each claim with empty `chunk_ids` → warning + drop; each
claim citing any label outside the consulted set → warning + drop; surviving
claims get `source_url` stamped from the retrieved chunk (database fact,
never model-copied). Statements are constructible ONLY from `labeled` —
there is no other constructor call site — so uncited synthesis cannot reach
a column. If every claim drops, the outcome is `model_declined`, auditable
via consulted passages + warnings.

Three honest caveats:

1. **`model_declined` slightly mislabels the all-dropped case.** "Declined"
   per ADR-0012 #5 means the model found nothing on the dimension; a model
   that asserted three unciteable claims did not decline — it asserted
   unauditable content that was suppressed. The response is still honest
   (warnings name every drop, consulted passages attached), but the cause
   tag is the only place the two cases share a label. A reader who skips
   warnings reads "the model found nothing here." Low-to-medium severity;
   an option is a distinct cause value or simply accepting warnings as the
   disambiguator (ADR-0009's warnings-are-first-class posture). Flagged for
   Harry's call.
2. **Partial drops don't change the column's face.** A column that emitted 4
   claims and kept 1 renders as a 1-statement column + warnings. That is the
   house posture (flag, never rewrite), and coverage counts are unaffected
   (they describe retrieval, not claims). Fine — noting it only so the live
   pass knows warnings are where this failure surfaces.
3. **What live validation can and cannot check here.** CAN: that label
   hygiene violations from the real model actually fire the drop path
   (watch the warnings channel across the validation queries — with a real
   model, unknown-label and uncited-claim events are probabilistic, so their
   *absence* over a small run proves little; the mocked tests remain the
   proof the path works). CANNOT: "feed it a case where the model asserts
   something the passages don't support and confirm the drop fires" — the
   drop will NOT fire for a labeled-but-unsupported claim, and no code in
   this build claims otherwise. Semantic-support checking is ADR-0007's
   explicitly deferred enforcement (and ADR-0009 accepted the same risk for
   change entries). The live pass's check for this failure class is human:
   read each column's statements against their cited passages in the k=6–8
   diff and confirm support — which the ADR's validation section already
   requires for omissions; extend the same read to fabrications.

---

## Empty-column split

Real in code and auditable: `retrieval_empty` returns before the answerer is
touched (spy test pins zero calls), `model_declined` attaches consulted
passages with snippet, source_url, and similarity. Coverage rides on both.
Confidence: high.

**One genuine gap: zero-chunks vs. below-floor are conflated.** `scanned ==
[]` (the filing has NO embedded chunks — e.g. embeddings backfill pending,
or a section-extraction gap) and "chunks exist but all score under the
floor" both produce `retrieval_empty`, which the user reads as "this filing
is silent on the dimension." A filing with no embeddings is not silent — the
corpus simply hasn't been embedded — and presenting corpus state as filing
content is a small honest-absence violation of exactly the kind decision #2
vs #5 exists to prevent. The distinguishing fact (`len(scanned) == 0`) is
already in hand and costs nothing. Severity: low today (the corpus is fully
embedded, 85/85 clean), but the failure is silent when it happens.
Confidence: high that the conflation exists; medium that it warrants a
category rather than, say, a warning string.

---

## Mock swap point

Confirmed: `get_column_answerer()` in router.py is the single point; the
shipped answerer is `MockColumnAnswerer`;
`test_compare_answerer_is_the_mock_in_this_build` does an isinstance check
that the live swap must consciously flip, making the swap visible and
deliberate. Two things the "one edit" framing undersells — both swap-time
work, neither a defect now:

1. **It is not key-gated yet, deliberately** — the mock needs no key. The
   live version must add the `get_qa_answerer`-style 503 gate, and the
   isinstance test must be rewritten (not deleted) to pin the gate.
2. **The live build includes writing a column system prompt that does not
   exist yet.** ADR-0007's Consequences call prompt wording load-bearing
   policy; the column prompt (labels-only citation, attributed language
   rules, no superlatives — even though per-column calls can't rank
   companies, they can editorialize within a column) must get its own
   wall-guard review at swap time. The swap is one *point* but not one
   *line*. Confidence: high.

## Comment-vs-structure audit

Checked every place the ADR wanted structure: the seam (type — structural),
period pinning (SQL ordering — structural), citation binding (constructor
reachable only from labeled chunks — structural), refusal (code order +
exploding-fake tests — as structural as order can be), no-re-entry (module
shape + call-count test — FIND-class, acceptable), caps (min()-clamped —
structural). Nothing load-bearing is held by comment or prompt text in the
mocked build — with the standing note that the live prompt, when written,
becomes the one prompt-discipline surface COMPARE has (within-column
editorializing), exactly as the ADR's Compliance section predicted
(verdict-language detection remains the backstop there).

## Wall-guard checklist (pre-commit list, run against f7c221a)

- No verdict/ranking/score/attractiveness field in any schema — PASS.
- find.py untouched by the commit; no answerer import — PASS.
- Seam takes (filing_id, query), assembly model-free, no model output into a
  model call — PASS.
- No superlative/ordering language in outputs; mock text is verbatim chunk
  content behind a "[MOCK]" prefix — PASS (live prompt: swap-time item).
- Caps min()-clamped; over-cap REFUSED naming the cap — PASS.
- Every emitted claim's source_url stamped from the database — PASS.

## Summary of findings for Harry's decision

1. (High) The 0.20 floor is on the honesty signal's denominator and gates
   the model call: false-completeness ("2 of 2") and false-silence
   (threshold-manufactured retrieval_empty) are both live risks; the
   constant is calibrated on transplanted stage-1 evidence. Promote it to a
   first-rank live-validate item; consider floor-legible coverage display.
2. (High) Live validation cannot exercise semantic support via the drop
   path — that path enforces label validity only. The fabrication check in
   the live pass is the human read of statements vs cited passages.
3. (Medium) `retrieval_empty` conflates "no embedded chunks" with "nothing
   relevant" — corpus state can masquerade as filing silence.
4. (Low-medium) All-claims-dropped is labeled `model_declined`; warnings are
   the only disambiguator from a true decline.
5. (Low) No forbidden-fields test on CompareResponse mirroring FIND's.
6. (Low) Swap-time work is more than one line: key gate, test rewrite, and a
   yet-unwritten column prompt needing wall-guard review.
