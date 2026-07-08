# 0013 — FIND: Cross-Encoder Rerank of Candidate Passages

**Status:** Accepted (amended 2026-07-07 after live validation)

> **Amendment (2026-07-07, post-live-validation).** Live validation on the real
> corpus corrected two claims in this ADR and changed one decision:
> 1. **Latency** is ~2.6–3.8 s/query on CPU, not the "tens of ms" originally
>    estimated (~100× off) — see the amended latency consequence.
> 2. **Reranking is now OFF BY DEFAULT, opt-in per request** (decision #5),
>    precisely because of (1): FIND is ADR-0011's cheap, frequent mode and must
>    stay ~270 ms by default.
> 3. The originally claimed **automatic "cosine fallback" when the dependency is
>    absent was never implemented and is removed** — opting in without the
>    `[rerank]` extra now raises a clear, explicit error, not a silent method
>    swap and not a raw ImportError.
> A **Known limitations** note is added: the cross-encoder is a precision
> improver, not an oracle (it can sink a substantive passage — observed MSFT on
> the AI query). The wall guarantees and structural invariants are unchanged.

## Context

FIND (ADR-0011 §1, implemented in `apps/api/research/find.py`) answers "which
of my companies match X" as a pure retrieval result with zero answer-model
calls. It funnels a single stage-1 global pgvector search into candidate
companies, groups each company's chunks best-first, and reports each company's
best chunk's cosine similarity as `match_strength`. Company ordering and the
representative passage are both driven by raw bi-encoder cosine distance.

**The observed defect is precision noise at the top of a company's chunk
list.** A bi-encoder (Voyage `voyage-context-4`, ADR-0006) embeds query and
chunk independently, so a chunk can score a high cosine while being off-topic
for the *actual* question — lexically or thematically adjacent but not
responsive. Two concrete specimens from the live corpus:

- **SCHW surfaced on a cyber-risk query** — its top-cosine chunk was not about
  cybersecurity.
- **EMR surfaced on an AI query** — its top-cosine chunk was not about AI.

In both, retrieval score is high but semantic relevance to the query is low.
Because `match_strength` and the shown passage are the *max-cosine* chunk, the
wrong chunk both represents the company and sets its rank. A cross-encoder,
which scores the (query, passage) pair *jointly*, is the standard, cheap fix
for exactly this bi-encoder failure mode — and it can run locally, so it does
not reopen the cost asymmetry ADR-0011 §2 is built on.

**The constraint that drives this design is the wall and the zero-answer-model
contract (ADR-0011 §1, ADR-0007 §3), not cost.** The reranker must be a local
model, must not be an answer-model, and must not turn a retrieval ordering into
a judgment about companies. This ADR governs only that reranking is added to
FIND; it changes no schema and adds no table.

## Decision

1. **Add a local cross-encoder reranker as a separate module,
   `apps/api/research/rerank.py`, that FIND imports.** The reranker scores each
   `(query, passage_text)` pair jointly and returns candidates reordered by
   rerank score descending. It is a pure, deterministic function of its inputs
   with **no network call at inference** — the model runs locally on CPU. FIND
   imports the reranker; the reranker imports no answerer and takes no answerer
   parameter, so FIND's zero-answer-model guarantee (ADR-0011 §1) is structurally
   unchanged: reranking is retrieval math, not generation. The alternative —
   folding rerank logic into `find.py` — is rejected to keep `find.py`'s import
   surface auditable and the reranker independently testable.

2. **Model: `cross-encoder/ms-marco-MiniLM-L-6-v2` (sentence-transformers),
   pinned as a named constant.** Rationale over alternatives:
   - *Size / latency:* ~80 MB, 6-layer MiniLM, CPU-viable. Scoring a FIND
     candidate pool (tens of passages) is tens of milliseconds on CPU, not
     seconds — proportionate to a retrieval step.
   - *License:* Apache-2.0 — compatible, redistributable.
   - *Fitness:* trained on MS MARCO passage ranking — precisely the
     query↔passage relevance task FIND needs.
   - *Rejected:* `ms-marco-MiniLM-L-12-v2` (2× layers, marginal quality gain
     for the added latency); `ms-marco-TinyBERT-L-2-v2` (faster but weaker on
     the subtle off-topic cases this exists to catch); `BAAI/bge-reranker-base`
     (stronger but ~1.1 GB XLM-RoBERTa — dependency and latency out of
     proportion to a FIND-precision tweak). The MiniLM-L-6 sits at the quality
     knee for this size class.
   The model name is a constant, not a hardcoded literal at the call site, so
   changing it is a deliberate one-line edit (the ADR-0011 §3 knob posture).

3. **Where it sits in the funnel: after stage-1 candidate selection and
   per-company grouping, before the `PASSAGES_PER_COMPANY` cap.** Stage-1 wide
   search and candidate selection (`WIDE_SEARCH_LIMIT`, `CANDIDATE_N`) are
   untouched — the reranker never re-runs retrieval and never widens the
   candidate set. For the selected candidate companies, their pooled passages
   are reranked, and **the cap is applied to the reranked order**, so the
   reranker changes *which* passages survive the cap, not how many. The
   min()-clamped caps stay exactly as they are.

4. **Reranking reorders passages both within and across the candidate set;
   `match_strength` is preserved as an untouched retrieval fact and a new
   `rerank_score` field is added — never overwritten.** Concretely:
   - Within a company, passages are reordered by `rerank_score` descending
     before the cap, so the passage that best answers the query (not merely the
     highest-cosine one) represents the company and survives the cap.
   - Across companies, candidates are ordered by their best passage's
     `rerank_score`, so a company whose only high-cosine chunk is off-topic
     (SCHW, EMR) falls — this is what actually fixes the stated symptom.
   - `match_strength` retains its definition — the max cosine over the
     company's chunks, a stage-1 retrieval fact (ADR-0011 §5) — and is carried
     through unchanged on every result. `rerank_score` is added alongside it,
     per passage and surfaced at the company level. Neither field overwrites
     the other.

   **Rejected alternative — within-company reorder only, company ordering left
   on `match_strength`.** This matches the most literal reading of the wiring,
   but it does *not* remove SCHW/EMR from the result: they were selected as
   candidates by max cosine *before* rerank, so leaving company order on
   `match_strength` leaves them ranked where the off-topic chunk put them. The
   observed defect is "SCHW *surfaced*," i.e. it appears and ranks high — that
   is a company-ordering symptom, so the fix must reach company ordering.
   *(Maintainer review call: if you prefer the minimal within-company-only
   variant and accept that SCHW/EMR keep their rank, override this decision
   here and the build follows the narrower scope.)*

5. **Reranking is behind a flag, OFF BY DEFAULT and opt-in per request; the
   flag and the max-candidates bound are named, min()-clamped constants.**
   `RERANK_ENABLED` (default `False`, amended from `True` after live
   validation) — with reranking off, FIND behaves exactly as before this ADR
   (cosine / `match_strength` order, every `rerank_score` `None`), at ~270 ms,
   and no cross-encoder is loaded. A caller opts in per request (the FIND
   endpoint takes `rerank=false` by default; `rerank=true` pays the ~3 s
   precision cost on that one query). Off-by-default is deliberate: FIND is
   ADR-0011's cheap, frequent mode, so the expensive precision pass is
   opt-in, matching the system-wide cheap-by-default / expensive-on-explicit-
   demand posture (ADR-0011 §3, ADR-0012 caps). Opting in **requires** the
   `[rerank]` extra; opting in without it raises a clear, explicit error naming
   the missing extra (never a silent cosine swap — a quiet scoring-method
   change would return different results with no signal, worse than a clear
   error — and never a raw ImportError/500).
   `RERANK_CANDIDATE_LIMIT` bounds how many passages are scored per query and
   is min()-clamped against the actual candidate pool size — reranking more
   passages than the candidate set holds is meaningless, so it is bounded by
   construction like every other FIND cap.

**Out of scope** (future work, each its own decision): reranking in COMPARE
(ADR-0012) — this ADR touches FIND only; changing stage-1 recall knobs
(`WIDE_SEARCH_LIMIT`, `CANDIDATE_N`) — orthogonal, already tuned in 3687d8d;
a GPU/batched serving path for the reranker — unnecessary at this corpus size;
any cross-encoder that emits a company verdict, score-of-desirability, or
ordering by anything other than query-text relevance — that is the judgment
wall (ADR-0007 §3), permanently out of scope.

## Compliance and Validation

For the guarantees to hold, all of the following must be TRUE, each checked
structurally where possible rather than by output-scanning:

- **Zero-answer-model preserved.** `find.py` imports no answerer and
  `rerank.py` imports no answerer; the reranker makes no network call at
  inference. *Check:* extend the existing FIND structural guard test to assert
  neither module references an answerer; the reranker's only heavy import is
  the local cross-encoder.
- **Wall held — `rerank_score` is a retrieval fact, not a judgment, and
  cross-set reordering is not company-ranking.** `rerank_score` measures how
  well a passage's *filing text* answers the query — the same category as
  `match_strength` (ADR-0011 §5), a better relevance estimator of the identical
  quantity. It ranks TEXT↔query relevance, never company exposure, quality, or
  desirability. The cross-set reordering of decision #4 is the variant most
  easily *mistaken* for company-ranking, so it is stated plainly here: when
  SCHW (cyber query) or ORCL (AI query) falls in the ordering, it falls because
  its filing TEXT is off-topic for THIS query — a retrieval fact about the
  passages — **not**
  because Athena judged the company less exposed, less attractive, or worse
  positioned. The reranker reorders PASSAGES by query-relevance; companies
  inherit position from their best passage's relevance, exactly as they already
  inherited position from their best chunk's cosine under ADR-0011 §5. Swapping
  a better relevance estimator for a worse one does not cross the wall. And the
  original retrieval fact stays visible: `match_strength` is preserved as an
  untouched field on every match, so the pre-rerank cosine ordering remains
  auditable next to the reranked order — the demotion is inspectable, never
  laundered. No superlative, no verdict field, no company scored for
  attractiveness enters any schema. *Check:* wall-guard review of the FIND
  schema diff; `rerank_score` documented as query-relevance on the field;
  `match_strength` asserted preserved (below).
- **`match_strength` not overwritten.** It keeps its max-cosine definition and
  value through reranking. *Check:* unit test asserts `match_strength` equals
  the pre-rerank max cosine while `rerank_score` reflects the cross-encoder.
- **Determinism.** The cross-encoder in eval mode, no dropout, fixed weights,
  is deterministic given inputs. *Check:* unit test with a stub scorer proves
  a fixed input yields a fixed order; the real model is loaded once and reused.
- **Caps still bind post-rerank.** `PASSAGES_PER_COMPANY` and
  `RERANK_CANDIDATE_LIMIT` are min()-clamped. *Check:* unit test asserts the
  surviving passage count after rerank never exceeds the cap.

## Consequences

- **Changes:** FIND gains an OPT-IN local cross-encoder rerank stage between
  candidate grouping and the per-company cap. Off by default; when a request
  opts in, each returned passage carries a `rerank_score` alongside its cosine
  `similarity`, and company ordering and the representative passage follow
  rerank relevance instead of raw cosine. The off-topic-best-chunk failure is
  corrected on the true false positives (validated: SCHW on a cyber query;
  ORCL — a cybersecurity passage with an incidental AI mention — on an AI
  query): off-topic high-cosine chunks lose to on-topic ones, and companies
  represented only by an off-topic chunk fall in the ordering. (The original
  EMR-on-AI example was found to be MIS-SPECIFIED — Emerson's filing carries a
  dedicated AI risk factor, so it is legitimately on-topic and the reranker
  correctly keeps it; see Known limitations.)
- **Unchanged:** FIND's zero-answer-model contract (ADR-0011 §1); the
  two-stage funnel and its stage-1 caps (`WIDE_SEARCH_LIMIT`, `CANDIDATE_N`,
  ADR-0011 §2–3); every ADR-0007 grounding/citation guarantee; `match_strength`
  as a retrieval fact; the schema, tables, and `filing_chunks` index (no
  migration); COMPARE (ADR-0012) and per-company QA entirely; the
  judgment-layer wall.
- **Risk accepted — dependency weight.** `sentence-transformers` pulls in
  `torch`, a heavy dependency the project does not carry by default. *Remedy:*
  the reranker lazily imports the model inside `rerank.py` (not at FIND import
  time), the model is downloaded once and cached locally, and reranking is off
  by default so the dependency is needed only when a caller opts in. There is
  **no silent cosine fallback** when the extra is absent (an earlier draft
  claimed one; it was never built and would be worse than a clear signal):
  opting in without the `[rerank]` extra installed raises a clear, explicit
  error naming the extra, not a raw ImportError/500. The dependency lives in
  the optional `[rerank]` group, installed deliberately.
- **Risk accepted — per-query latency (measured, ~100× the original
  estimate).** The model (~80 MB) downloads on first use and loads once per
  process. Live validation measured steady-state rerank latency at **~2.6–3.8 s
  per query on CPU** (FIND without rerank is ~270 ms) — the cross-encoder scores
  up to ~80 (query, long-filing-chunk) pairs, and filing chunks are long. This
  is far above the "tens of ms" this ADR originally estimated, and is the direct
  reason reranking is **off by default and opt-in** (decision #5): FIND's cheap
  path stays cheap, and the ~3 s cost is paid only on an explicit `rerank=true`.
  *Remedy / knobs:* the model is a lazily-loaded, reused singleton; if the
  opt-in cost ever needs cutting, the levers are a smaller cross-encoder, a
  lower `RERANK_CANDIDATE_LIMIT`, or the GPU/batched path (out of scope here).
- **Risk accepted — install size and CI build time.** `torch` (pulled by
  `sentence-transformers`) is hundreds of MB installed; adding it to the base
  image would inflate install size and CI build time on every run — for a
  dependency only the live reranker needs. *Remedy:* `sentence-transformers` is
  an OPTIONAL dependency group (`.[rerank]`), absent from the base and dev/test
  installs, so CI never installs torch and never pays that build cost. The hard
  requirement this enforces: **the reranker's tests inject a stub scorer and
  never import torch**, and the suite runs with reranking disabled by default
  (a test fixture forces the flag off; the dedicated rerank tests opt back in
  with the stub), so the full test suite pays zero torch tax on every run.
  Production installs the extra deliberately; with the extra absent, the
  default path is unaffected (rerank is off), and an explicit opt-in raises a
  clear "install `.[rerank]`" error rather than a silent swap or a raw 500.
- **Known limitations — the cross-encoder is a precision improver, not an
  oracle.** It reliably demotes clear off-topic-best chunks (SCHW/cyber,
  ORCL/AI), but it can also sink a *substantive* on-topic passage: live
  validation observed MSFT's genuine, detailed AI-risk disclosure scored low
  and demoted on the AI query (likely because the MS-MARCO cross-encoder
  penalizes long, mixed, vision-framed passages). This is an accepted
  limitation and part of why the feature is opt-in rather than the default —
  the user asks for the precision pass knowing it trades some recall of
  substantive-but-awkwardly-framed passages for sharper top-of-list relevance.
  `match_strength` stays visible on every match, so a demoted company's
  original retrieval standing remains inspectable.
- **Build sequencing:** this ADR is draft-for-review only. On acceptance, the
  first and only increment is `rerank.py` + the FIND wiring + the constants +
  the unit test, built mocked (stub scorer) and reviewed, with **no live model
  run and no commit** until the before/after validation plan is executed and
  confirmed twice (live-validation discipline; precedent 7973c0a).
