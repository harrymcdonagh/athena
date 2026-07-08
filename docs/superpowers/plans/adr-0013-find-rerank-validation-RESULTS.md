# ADR-0013 FIND Rerank — Live Validation RESULTS

**Run:** live corpus (84 companies / 85 filings / 8,277 chunks), Voyage live embedder,
`cross-encoder/ms-marco-MiniLM-L-6-v2` installed via `.[rerank]` (torch 2.12.1,
sentence-transformers 5.6.0), model warmed before timing.
**Verdict: PARTIAL — NOT a clean two-part pass. DO NOT COMMIT.** One specimen (EMR/AI)
fails part 1, plus a material latency finding. Details below. No app code was changed;
no commit made.

## Methodology note (important — the first pass was confounded)

Stage-1 retrieval (Voyage embed + pgvector **HNSW, an approximate index**) is **not
guaranteed bit-identical across independent calls**. A naive harness that calls
`find_companies(rerank=False)` then `find_companies(rerank=True)` compares an OFF ordering
from one retrieval against an ON ordering from a *different* retrieval — the candidate
pools can differ at the margin (observed: EMR `match_strength` 0.4740 vs 0.4762; ORCL
0.4878 vs 0.4931; GS present in one pool, absent the other). That confounds the OFF↔ON
comparison and is what produced spurious "determinism: False" readings — **the variance
is in retrieval, not in the reranker.**

The authoritative results below **retrieve once per query and apply both orderings to the
identical candidate pool**, isolating the rerank effect. Under a fixed pool:

- **Reranker determinism: PASS (5/5)** — ON == ON on the same pool, every query.
- **`match_strength` preserved OFF→ON: PASS (5/5)** — the retrieval fact is untouched.

## Specimen results (pool-isolated, authoritative)

### SCHW — query "cybersecurity risk" — ORDERING case — **PASS**

- Present in stage-1 top-15 pool: **yes** (OFF rank #3) → a real ordering case, in scope.
- **Part 1 (drops in company order): PASS.** SCHW **#3 → #11** under rerank.
  Its best-reranked passage (`rr=+2.351`) is *"When these outflows outpace excess cash on
  hand…"* — a liquidity passage, not cyber: SCHW has no strong cyber chunk in its pool, so
  demotion is correct.
- **Part 2 (on-topic companies surface the correct passage): PASS.** The rerank winners are
  genuinely on-topic cyber passages: MMM `rr=+4.113` *"Network disruptions, security and
  data breaches, cyberattacks…"*, UNP `rr=+3.911` *"…cyber incidents in the normal
  course…"*, CRM `rr=+3.475` *"…target of malicious cyberattacks…"*.
- Minor caveat: PYPL stays #1 but its rerank-winning passage (`rr=+4.698`) is a generic
  *"ITEM 1A. RISK FACTORS…"* header rather than a cyber-specific line — the cross-encoder
  over-scores boilerplate slightly. Not a wall/correctness issue; noted.

Full: OFF `[PYPL, BRK-B, SCHW, MSFT, MMM, GD, COST, TMUS, UPS, UNP, CRM, AMT, VZ, PLTR, LMT]`
→ ON `[PYPL, MMM, UNP, CRM, GD, COST, AMT, LMT, UPS, BRK-B, SCHW, TMUS, PLTR, MSFT, VZ]`.

### EMR — query "artificial intelligence risk" — ORDERING case — **FAIL (part 1)**

- Present in stage-1 top-15 pool: **yes** (OFF rank #4) → a real ordering case, in scope
  (not a recall miss).
- **Part 1 (drops in company order): FAIL.** EMR does **not** drop — it moves **#4 → #3**.
  The reranker scores EMR's AI passage `rr=+4.304` (the **3rd-highest** of the pool),
  judging it genuinely relevant. EMR's representative passage is unchanged OFF→ON:
  *"Many of our products and services, including measurement and analytical instrumentation,
  industrial valves an[d]…"*.
- **Interpretation (needs maintainer judgment — I did not decide, per scope):** the
  reranker's broader behavior on this query is *sensible* — it elevates companies with
  explicit generative-AI risk language (PM `rr=+5.657` *"We increasingly use artificial
  intelligence-based solutions…"*, AIG `rr=+4.760` *"…generative artificial intelligence,
  may present risks…"*) and demotes off-topic-best companies (ORCL #1→#11 — its top chunk
  is a cyber/IT-security passage, off-topic for AI; MSFT #6→#13; ADBE, DHR fall). So either
  **(a)** EMR's AI disclosure is genuinely on-topic and EMR was never a valid "off-topic"
  specimen (the reranker correctly keeps it), or **(b)** the cross-encoder over-values
  EMR's product-portfolio passage. The truncated passage ("measurement and analytical
  instrumentation") is consistent with (a). This is the one result that blocks a clean pass
  and is the reason to STOP rather than commit.

Full: OFF `[ORCL, PLTR, ACN, EMR, AXP, MSFT, ADBE, PM, CRM, MMM, DHR, ISRG, AIG, COF, META]`
→ ON `[PM, AIG, EMR, MMM, PLTR, ISRG, META, CRM, ACN, COF, ORCL, AXP, MSFT, ADBE, DHR]`.

## Broad controls (regression check — adjacent behavior still emits)

All three return non-empty, sensible orderings; reranker deterministic and `match_strength`
preserved on all. Rerank refines rather than scrambles — on-topic companies rise:

- **supply chain disruption:** LLY, HD, MMM, CAT to the top (all supply-chain passages);
  CL (a divestiture-passage false-lead that was #1 by cosine) drops to #14.
- **climate and environmental risk:** UNP (a rail-competition passage that was #1 by cosine)
  drops #1→#11; SO/NEE/DUK/CAT (genuine environmental-regulation disclosers) reorder around
  it. Mixed but not regressive.
- **foreign currency exchange risk:** AAPL, ACN, RTX rise (explicit FX-rate passages);
  ORCL (impairment passage) drops #1→#4.

No broad query emptied out or lost a previously-correct top company to the reorder.

## Latency — **MATERIAL FINDING: ~100× the ADR estimate**

Real end-to-end, post-warm, median of 5:

| query | OFF | ON | delta |
|---|---|---|---|
| cybersecurity risk | 266 ms | 3146 ms | **+2880 ms** |
| artificial intelligence risk | 251 ms | 3618 ms | **+3367 ms** |
| supply chain disruption | 260 ms | 2820 ms | **+2560 ms** |
| climate and environmental risk | 281 ms | 4032 ms | **+3751 ms** |
| foreign currency exchange risk | 296 ms | 3029 ms | **+2733 ms** |

The ADR-0013 Consequences claim "steady-state per-query rerank latency is tens of ms on
CPU." **Measured: +2.6 to +3.8 seconds per query — roughly 100× the estimate.** Cause is
the cross-encoder scoring up to ~80 (query, filing-chunk) pairs on CPU with unoptimized
torch; filing chunks are long (many tokens), so each pair is not cheap. FIND is ADR-0011's
"cheap, frequent mode"; adding ~3 s to every FIND call is a real cost the ADR did not
budget for. This is a cost/UX finding, not a correctness bug — but it is significant enough
that it should factor into the commit decision, and the ADR's cost line needs correcting.

## Determinism (pre-existing, for the record)

- **Reranker: deterministic** given a fixed pool (proven above and in unit tests).
- **Stage-1 retrieval: NOT guaranteed deterministic** across independent calls (HNSW ANN
  and/or Voyage). This is a **pre-existing** property of FIND, independent of ADR-0013 —
  but it means two identical FIND requests can already return slightly different orderings
  at the margin. Worth knowing so run-to-run FIND variance is not misattributed to rerank.

## Overall verdict

**PARTIAL — not safe to commit as a clean two-part pass.**

- SCHW/cyber specimen: **PASS** (both parts).
- EMR/AI specimen: **FAIL on part 1** — does not drop; the reranker judges EMR's AI passage
  genuinely relevant (`rr=+4.304`, rank 3). Needs a maintainer call on whether EMR is in
  fact on-topic (spec premise wrong) or the reranker over-scores it.
- Reranker correctness (determinism, `match_strength` preservation, no scramble of broad
  queries): **clean.**
- Latency: **+~3 s/query, ~100× the ADR estimate** — material, flag before committing.

**STOPPING here per scope — no fix, no commit.** Awaiting your read of the EMR case and the
latency cost.

## Out-of-round items surfaced by the pre-run code review (for a later round, not now)

1. The claimed "falls back to cosine ordering when the dependency is absent" is **not
   implemented** — with `RERANK_ENABLED=True` and the extra uninstalled, `_default_scorer()`
   raises `ImportError` on the first FIND call (500s), rather than auto-disabling. The ADR
   §5 and pyproject comment assert a fallback that requires a manual `RERANK_ENABLED=False`.
2. No NaN guard after scoring (`model.predict` NaN would silently corrupt the sort).
3. Minor: double-clamp of `RERANK_CANDIDATE_LIMIT` at the find.py call site; stale docstring
   on `test_find_rejects_non_positive_caps`.

These are code changes, deliberately **not** made this round.
