# 0011 — Cross-Company Query: FIND-First, Funnel Retrieval, Bounded Compare

**Status:** Proposed

## Context

QA (ADR-0007) and change detection (ADR-0009) are per-company: every question
runs against one company's filings. With ~100 companies now ingested
(ADR-0010's curated set), cross-company questions become possible and useful —
"which of my companies mention tariff exposure in their risk factors,"
"compare supply-chain risk across these five." Today the only way to answer
them is to run per-company QA ~100 times, which nobody will do.

**Cost is the primary design driver.** The developer is token-budget
constrained, and a naive cross-company query is exactly how a corpus this size
gets expensive: retrieve chunks from all 100 companies, stuff them into one
prompt, run the answer-model. The design must make the cheap path cheap and
the expensive path bounded and rare.

The key asymmetry this ADR is built on: the expensive part is the
ANSWER-MODEL; the cheap part is VECTOR SEARCH. Every chunk in `filing_chunks`
is already embedded and indexed (pgvector HNSW, ADR-0006) — querying it is
cheap math that is already paid for, and `search_chunks` runs global (no
ticker filter) or per-company today. So the design does as much work as
possible in retrieval and invokes the answer-model as narrowly as possible.

## Decision

1. **Two distinct modes, FIND-first.** FIND answers "which companies match X":
   retrieval-led, returning the matching companies with their citing passages
   and `source_url`s. The answer-model is largely or fully SKIPPED — a match
   list with citations is a retrieval result, not a generated answer. A FIND
   result is returnable as cited matches with NO answer-model call at all:
   the zero-token answer path is the mode's contract, not an optimization.
   FIND is the cheap, frequent mode and the first build increment. COMPARE
   synthesizes across a small, explicitly named set of companies; it costs
   answer-model tokens, so it is bounded (decision #3) and built second. Rationale: the
   frequent query should be the cheap one, and COMPARE is mechanically FIND
   plus synthesis over the narrowed set — FIND-first makes COMPARE a smaller
   later add, not a separate build.

2. **Two-stage funnel retrieval (the core cost decision).** Cross-company
   retrieval is a funnel, never a single flat search over all chunks:
   - *Stage 1 (wide, cheap):* one global vector search across the whole corpus
     to surface CANDIDATE COMPANIES that match the query — pgvector math on
     the existing HNSW index, no answer-model.
   - *Stage 2 (narrow, focused):* focused per-company retrieval ONLY on the
     handful of companies stage 1 surfaced. Any answer-model work happens
     here and only here, only on the narrowed set.

   Flat global top-k-across-all-chunks is explicitly rejected as the primary
   shape: it lets one text-heavy company dominate the result and starve the
   others — the cross-company analog of the period skew ADR-0009 §2 fixed —
   and it does not bound cost. The funnel keeps cost proportional to the
   number of MATCHES, not the size of the corpus.

3. **Hard cost caps by design — structural, not advisory.** These are design
   invariants, the same posture as ADR-0009 §7's explicit flag and ADR-0010
   §4's explicit refresh: the system PREVENTS the expensive query rather than
   warning about it.
   - COMPARE caps the companies per query (default: 5) and REFUSES over-cap
     and unbounded scope alike — "compare all 100," or a named set larger
     than the cap, is rejected with a clear message asking for a named set of
     at most 5. Rejection, never silent truncation: a comparison quietly cut
     to the first 5 that the user believes is complete is exactly the
     silently-wrong result this project refuses everywhere.
   - Evidence per company is bounded: stage 2 caps chunks (and therefore
     tokens) per company.
   - Stage 1 is bounded: only the top-N candidate companies pass to stage 2.

   The developer should not be able to ACCIDENTALLY issue a corpus-wide
   answer-model query; the expensive path requires naming a small set on
   purpose.

4. **Grounding and citation guarantees carry over — ADR-0007 unchanged.**
   Every cross-company result — FIND matches and COMPARE synthesis alike — is
   grounded in retrieved chunks and cited (company + filing `source_url`),
   the same contract as per-company QA. No cross-company claim without a
   citation. This ADR is ADDITIVE to ADR-0007: no guarantee is reopened or
   weakened, and per-company QA is untouched.

5. **The evidence/judgment wall holds (ADR-0009 §6, ADR-0007 §3).**
   Cross-company FILTERING ("which companies mention X") and COMPARISON ("how
   does their language on X differ," cited) are evidence-layer work and in
   scope. Cross-company RANKING BY DESIRABILITY — "which is most exposed,"
   "which is best positioned," "which is the better opportunity" — is
   judgment-layer and explicitly OUT of scope. Multi-company queries naturally
   tempt "rank them for me"; the answer is the cited facts side by side, with
   the ranking left to the user. The principle, not the example list, is the
   wall — the ADR-0007 §3 test of whose judgment the answer carries: stating
   what each company's filing says, both cited, is evidence work; the SYSTEM
   picking "most," "best," or "least" is not, because a superlative requires
   Athena to judge magnitude or desirability ACROSS companies rather than
   surface their statements. "Which is most exposed" is therefore out of
   scope even though every underlying fact is citable; the in-scope answer is
   each company's cited exposure language, ordered by nothing.

**Out of scope** (future work, each its own decision): the COMPARE build
itself (designed here, built after FIND validates the funnel); any
cross-company ranking or scoring by desirability (judgment layer, its own
ADR); a daily briefing built on cross-company query — a briefing is
mechanically a scheduled cross-company query, so this ADR is its substrate,
but the briefing's "what is worth surfacing" decision is deferred; and any
widening of the corpus beyond the current ~100 — breadth is deliberately
deferred (ADR-0010), and this feature exists to test value on the existing
corpus first.

## Consequences

- **Changes:** Athena gains corpus interrogation on top of per-filing lookup —
  the first query surface that reads across companies. A cross-company
  retrieval path is added alongside (not inside) the existing per-company
  paths: a stage-1 global candidate search plus stage-2 focused per-company
  retrieval, with the caps of decision #3 enforced in the request/query layer
  so an over-cap or unbounded request is a rejected request, not a warning.
  FIND returns matching companies with cited passages and no generated prose;
  COMPARE, when built, returns cited synthesis over a named set of at most 5.
- **Unchanged:** every ADR-0007 guarantee (chunks-only grounding, no verdicts,
  attribution, citation to `source_url`); per-company QA and change detection
  (ADR-0009) in their entirety; the ingestion/embedding pipeline, the schema,
  and the `filing_chunks` index (no table, no migration — per house rules
  none may be written before this ADR is accepted); and the judgment-layer
  wall.
- **Risk accepted:** the funnel trades recall for cost — FIND quality depends
  on stage-1 candidate recall, and a company whose match is subtle may score
  below the candidate cutoff and never reach stage 2. This is accepted, and
  the knob is explicit: raising the stage-1 candidate cap raises recall AND
  cost together — the trade-off is tuned, never bypassed. Likewise COMPARE's
  company cap means "compare across many" is deliberately unsupported; the
  remedy is naming a smaller set, not lifting the cap.
- **Build sequencing:** FIND is the first increment — stage-1 wide search
  funneling to matching companies with cited passages, answer-model skipped —
  validating the funnel's recall and the caps on the cheap mode. COMPARE
  (bounded synthesis over a named set) is the second increment, reusing the
  funnel. Nothing here presumes the briefing or corpus breadth.
