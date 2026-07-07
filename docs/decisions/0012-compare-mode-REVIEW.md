# Review notes — ADR-0012 draft (COMPARE mode)

Reviewer: Claude (session 2026-07-07). Findings only; the ADR is untouched.
Each finding carries a confidence flag — Harry overrides freely.

Verified against: ADR-0007/0008/0009/0010/0011; `apps/api/research/find.py`,
`qa.py`, `embeddings.py`, `repository.py`; commits 3687d8d and 7973c0a.

---

## Named target 1 — passages-per-company = 2

**Verdict: the failure-mode asymmetry is over-claimed, and as drafted the
live-validation step would not catch the failure that matters. 2 is
acceptable as a starting constant only if the validation design (and ideally
the response shape) is amended as below.** Confidence: high on the analysis,
medium on whether it should move the constant itself.

Three sub-findings:

**(a) "A starved column fails visibly" is only true at the zero/near-zero
end.** The draft's asymmetry argument (decision #3, Risk accepted) assumes
starved ⇒ thin-or-empty ⇒ visible. That holds when scoped retrieval finds
almost nothing. It does not hold in the case that matters: a company with
*substantial* disclosure on the queried dimension. A 10-K that discusses the
topic across risk factors, MD&A, and legal proceedings might have 8–10
relevant chunks; a 2-passage column built from the top 2 is not thin — it is
fluent, fully cited, and complete-*looking*. Nothing in a cited column
advertises what it omitted. So at cap=2 the real trade is not
visible-thinness vs. invisible-off-topic; it is one invisible failure
(off-topic passage inside synthesis) traded for another (a coverage gap
wearing the costume of a complete answer). The draft's own principle —
honest absence over silent wrongness — cuts against a column that silently
under-reports held evidence just as much as against one that over-reports.
Confidence: high.

**(b) The drift evidence is being transplanted across retrieval shapes.**
Not relitigating 3-vs-2 — the drift finding (commit 3687d8d:
"PASSAGES_PER_COMPANY stays 3 (third passages drift off-topic)") is settled
*for what it measured*. But what it measured is FIND stage 1: a company's
passages there are its chunks that survived the *global* top-80 on broad
thematic queries, grouped per company. A company's 3rd-ranked chunk in that
pool is its 3rd-best globally-competitive chunk. COMPARE stage 2 is a
different retrieval: a scoped search inside one filing, where rank 3 is
simply that filing's 3rd-most-similar chunk — for a disclosure-rich filing
on the queried dimension, rank 3 is plausibly still squarely on-topic. The
drift observation motivates *caution*; it does not measure stage-2 rank-3
quality, because stage-2 has never run (see cross-reference finding #3
below: FIND built no stage 2). The draft presents 2 as grounded in evidence
that is actually about a different distribution. Confidence: high that the
distinction is real; medium on its practical size.

**(c) Live-validate-2× as specified will not catch under-reporting.** The
validate-twice step (Compliance) is aimed at defect-clears-AND-adjacent-
behavior-still-emits — the ADR-0009 pattern. Under-reporting is precisely
the defect that *looks like success*: a good column and an under-reporting
column are indistinguishable unless validation compares against something
wider. What validation should specifically do:

1. Include at least one known-rich specimen — GOOG-on-AI is already the
   named specimen in the draft's own Context #2, and its 10-K is known to
   hold a genuine AI-risk passage plus presumably more; validate COMPARE on
   the exact dimension where disclosure is known to be deep.
2. For each validation query, pull a wider filing-scoped retrieval (k=6–8)
   as ground truth and check the 2-passage column against it for material
   omissions — i.e., validate *coverage*, not just column quality.
3. Consider a mechanical coverage signal in the response: count of chunks in
   the pinned filing above the similarity floor vs. count consulted (e.g.
   "2 of 9 qualifying passages consulted"). This is a retrieval fact — zero
   answer-model tokens, same epistemic class as FIND's match_strength, safe
   under the ADR-0011 §5 test — and it converts the invisible coverage gap
   into a reported one. With this signal, 2 genuinely becomes the safer
   failure mode, because starvation self-reports instead of relying on the
   column "looking thin."

One thing the draft gets right and should keep: the constant-not-runtime-arg
posture, and the honesty that cost is not the binding argument (3 vs 2
passages × 5 companies is a trivial token delta — this is a quality knob).

---

## Named target 2 — partial failure, proceed-for-the-rest

**Verdict: the proceed/refuse distinction is sound and correctly classified
as reported absence — but the prominence attack lands. The draft asserts
field-existence, not impossible-to-miss, and its own caller-order promise is
what a side-list breaks. This is the gap.** Confidence: high.

**The distinction holds.** The >5 refusal guards a bound the caller chose to
exceed and can trivially fix; truncating would silently answer a different
question than the one asked (and *which* 5 would be arbitrary). An
unresolved or no-evidence name is a fact about the corpus the caller should
learn — refusing the whole query would *hide* that fact behind a retry loop
and punish the four resolvable names for the fifth's absence. Proceeding
with a categorized per-name report is the same posture ADR-0010 §5 already
takes for batch ingestion (unresolvable symbol = reported error, work
continues). Reported absence, not silent truncation. Endorsed as drafted.

**But prominence is a response field, not a compliance assertion.** The
compliance bullet ("Failures are categorized and loud… first-class in the
response") and its test ("columns + a complete failure list, with nothing
dropped") assert *completeness* — nothing dropped from the payload. They do
not assert that a consumer cannot miss it. The concrete failure path exists
today: `apps/web` is being built right now; a renderer that maps
`response.columns` to a table and never reads `response.failures` shows a
clean 3-column comparison for a 4-name request. The API was honest; the
*rendered* comparison is the silent truncation the ADR refuses — reincarnated
one layer up, and no test in the Compliance section would notice.

The draft even contains the seed of the fix and doesn't use it: decision #4
promises **column order follows the caller's named order**. With failures in
a side list, that promise silently degrades — the caller named A, B, C, D
and sees columns A, C, D; positional correspondence with the request is
broken, which is itself a small silent wrongness.

**Recommended shape (making prominence structural):** one ordered `entries`
list, one entry per deduplicated asked-for name, *in caller order*, each
entry typed: `column` | `no_finding` (decision #5's honest empty) |
`unresolved` | `no_evidence`. Failures occupy their position in the same
sequence the columns live in — a renderer cannot draw the successes without
iterating past the failures, because they are the same array. Add the
compliance invariant as a *tested assertion*: `len(entries) ==
len(deduplicated_names)`, always, plus a top-level `partial: true` status
when any entry is a failure. This also makes decision #2's "unconfusable
from #5" requirement structural: the four entry types are one discriminated
union, distinguishable by type tag, not by which corner of the response they
sit in. (Note this does *not* conflict with the draft's "never empty
columns" language — a typed failure entry in the sequence is not an empty
column masquerading as no-finding; it is a differently-typed entry.)

Minor adjacent points, lower severity:

- The cap applies to the deduplicated *named* set before resolution, so a
  7-name request with only 4 resolvable names is refused. That is consistent
  (refusal is request-layer, pre-resolution) and defensible — but it means
  the cap binds on names asked, not columns produced; worth one sentence so
  it reads as chosen, not accidental. Confidence: high that this is the
  draft's behavior as written.
- Name-level dedup and company-level dedup differ: GOOG and GOOGL are two
  `sec_ticker_reference` rows sharing one CIK and (at most) one `companies`
  row. A request naming both passes dedup as 2 names and yields two
  identical columns. Small, real, unaddressed — post-resolution CIK dedup
  (reported, per the house style) closes it. Confidence: medium-high
  (depends on resolution keying, which the draft leaves unspecified — it
  also never says whether "named" means tickers or free-text names; ADR-0010
  resolution is ticker→identity, so presumably tickers, but say so).

---

## The structural seam for the wall (decision #6a / Compliance)

**Finding: (a) is the right choice, but as *specified* it is weaker than the
FIND precedent it invokes, and the compliance assertion overstates what a
signature can assert.** Confidence: medium-high.

FIND's guarantee is import-shaped: the module has no answerer import, so the
forbidden output is unrepresentable *anywhere in the module*. The draft
claims the same class of assertion ("on the seam's signature and imports…
there is no parameter through which another company's evidence can enter a
call") — but a synthesis function whose signature accepts a list of passages
cannot know they belong to one company. The one-company property lives in
the *caller's loop*, and a caller bug that passes a merged list type-checks
fine. That is orchestration discipline — nearer to prompt discipline's
cousin than to no-answerer-import.

Two ways to make it genuinely structural, either sufficient:

1. **Evidence enters only via retrieval, inside the seam.** The synthesis
   function takes `(filing_id, query)` — not passages — and performs its own
   filing-scoped retrieval internally. Then there is literally no parameter
   through which foreign evidence can enter, which is the claim the
   Compliance section already wants to make.
2. **A single-company evidence type.** The seam accepts a
   `CompanyEvidence`-style object constructed only by the filing-scoped
   retrieval path, carrying one ticker/filing identity; construction from
   arbitrary passage lists is not exported.

Also missing: an explicit invariant that **assembly is model-free and model
outputs never re-enter a model**. "≤5 synthesis calls" bounds count, but
nothing in the draft forecloses a sixth call that summarizes the five
columns — which would see all companies' *content* and is exactly where a
ranking would leak back in. One sentence closes it: no answer-model call
takes more than one company's evidence, and no answer-model call takes
another call's output, as input. Confidence: high that the draft doesn't
state this; the risk itself is a future-drift guard, not a current defect.

The within-column backstop (verdict-language detection per ADR-0007) is
correctly identified as the residue structure can't cover. Agreed.

---

## Latest-10-K column pinning (decision #3)

**Finding: the pinning intent is right and correctly keeps cross-period out
of scope, but the named mechanism contradicts it, and two edge cases go
unaddressed.**

**(a) Mechanism contradiction — verified in code, high confidence.** The
draft says the column "draws from its most recent ingested 10-K" via "the
existing per-company `search_chunks` path." Those two clauses conflict.
`search_chunks` / `semantic_search` scopes by **ticker** (and optionally
section) — not by filing. The corpus already holds multiple 10-Ks per
company (AAPL FY2024 + FY2025, ADR-0009 Context), so a ticker-scoped search
returns a cross-period mix, which is precisely what pinning exists to
prevent. The mechanism that *can* pin exists — the ADR-0009 §2 machinery
retrieves by `filing_ids` (`balanced_semantic_search` /
`search_chunks_for_filings`) — but it is not the path the draft names. The
fix is one sentence: resolve latest-10-K per company first, then retrieve
scoped to that filing id. As written, an implementer following the ADR
literally builds a column that silently blends FY2024 and FY2025 evidence —
inside a synthesized column, the exact invisible-failure class this draft is
otherwise vigilant about.

**(b) Amended filings (10-K/A).** ADR-0008 §3's *previous comparable
filing* is same-`form_type`; a 10-K/A is a different form_type string with
the same period_end_date and a later filing_date. When one is ingested, is
"most recent ingested 10-K" the original or the amendment? Under strict
form_type matching the amendment is invisible; under period ordering with
filing_date tie-break it wins. Either answer is defensible; the draft (and
arguably ADR-0008) is silent. Today's corpus likely holds no 10-K/A, so an
out-of-scope note naming the hazard would suffice — the ADR-0008 §2 house
pattern (unlike-period comparisons: handled explicitly or restricted, never
papered over) is the template. Confidence: high that it's unaddressed;
low urgency.

**(c) A company whose latest filing is not a 10-K, or that has filings but
no 10-K.** Decision #2's `no_evidence` category is defined as "no
`companies` row / no ingested filings." When 10-Qs land (ADR-0008's stated
direction), two gaps open: (i) a company with filings but no 10-K falls
between the categories — it resolves, has evidence, but cannot produce a
pinned column; define the category boundary as "no ingested **10-K**" now
and it's future-proof for free. (ii) A company whose latest 10-K is stale
relative to a newer ingested 10-Q: the column silently speaks from older
evidence than the corpus holds. Cross-period *synthesis* is rightly
ADR-0009's job, but "a newer filing exists and was not consulted" is a
disclosure fact, not a synthesis — at minimum name it in Out of scope so
the silence is chosen. Confidence: high; unreachable on today's annual-only
corpus, so severity low now, but the category definition fix costs one word.

---

## Citation binding (decision #4 / #5 / Compliance)

**Finding: the draft/resolved binding is real and correctly invoked — but
the honest empty column is the one output with no citation to audit, and
the draft conflates its two different causes.** Confidence: medium-high.

The draft/resolved mechanism the draft cites exists and works as described
(`qa.py`: the model-facing draft carries chunk labels only; `source_url` and
`period_end_date` are stamped from the retrieved chunks and the filings
table; unresolvable entries are dropped with warnings). Extending it
per-column leaves no path to uncited *claims*. Good.

The gap is decision #5. It treats as one outcome two different facts:

- *Retrieval-empty*: the filing-scoped search returned nothing relevant — a
  mechanical retrieval fact, assertable with zero model involvement.
- *Model-declined*: passages were retrieved, but the model judged them
  unresponsive and emitted no claims — a model judgment.

The second is the only output in COMPARE that is model-produced yet carries
no citation — "the filing does not address this" backed by nothing the user
can check. ADR-0009 §5's no-change analog is *stronger* than this: no-change
must cite both periods. The cheap fix keeping the same auditability: the
empty column reports which it is, and in the model-declined case the
response lists the consulted passages (retrieval facts, already in hand) as
consulted-but-not-cited — so the user can audit the decline exactly the way
they audit a claim. This also gives live validation something concrete to
check the empty-column shape against.

---

## Cross-reference and framing errors in the draft's own reasoning

1. **Wrong ADR-0008 section cite — high confidence.** Decision #3 says
   "most recent ingested 10-K (ADR-0008 §4 ordering)." ADR-0008 §4 is
   `thesis_snapshots`. The ordering is §1 (period_end_date, filing_date,
   accession_number) and *latest filing* is defined in §3.

2. **Decisions #4 and #6 justify each other circularly — high confidence.**
   #4 bans cross-referential prose "— see decision #6 for why this falls out
   of the enforcement choice." #6's second reason for (a): the foreclosure
   "costs nothing now" because cross-referential prose "is already excluded
   by decision #4." Each cites the other as the ground. The underlying
   position is sound and there are real independent grounds (comparative
   prose is the on-ramp to ranking language; deferring it to its own ADR is
   the house pattern) — but one of the two needs to state a reason that
   doesn't point at the other, or a reader pulling either thread finds the
   other end in their hand.

3. **"Reuses the FIND funnel" overstates — high confidence, verified.**
   FIND is stage-1 only; its own docstring says "No stage-2 re-retrieval."
   There is no FIND per-company stage to reuse (Build sequencing: "reusing
   the FIND funnel's per-company stage"). What COMPARE reuses is ADR-0011
   §2's stage-2 *design* and the general per-company/per-filing search
   paths — which is fine, but the draft implies validated shared machinery
   where the stage-2 retrieval (with filing pinning, per above) is new
   work being run for the first time. This also feeds finding 1(b): stage-2
   passage quality has no live evidence yet, from FIND or anywhere.

4. **Untraceable precedent labels — low severity.** "The ADR-0009 Q3
   precedent" appears nowhere in `docs/`; it lives only in commit 7973c0a's
   message ("Re-validated live twice per question: the Q3-vs-Q1 tariff
   contradiction cleared in both passes and the legitimate fiscal-year
   changed=false still emits"). Same class: "ADR-0009's draft/resolved
   precedent" — the mechanism is real but is recorded in `qa.py`, not in
   ADR-0009's text. A reader of the decisions directory cannot follow either
   cite. Spell the pattern out in a clause or cite the commit; ADRs are the
   knowledge-in-git surface (ADR-0004), so precedents load-bearing enough to
   cite should be legible from within it.

---

## What survives review unqualified

For balance — pressure was applied to these and they held: the >5 refusal
as a request-layer rejection with no result ever produced; caller-order
columns with *no* computed ordering (the match_strength-is-fine-THERE /
position-reads-as-meaning-HERE distinction is exactly right); per-column
synthesis as the recommended shape (the critique above is about how its
structuralness is asserted, not the choice); no-table-no-migration; the
FIND-quality increments as non-blocking follow-ups; and the Consequences
section's honesty that COMPARE breaks the zero-token line deliberately and
boundedly rather than pretending otherwise.
