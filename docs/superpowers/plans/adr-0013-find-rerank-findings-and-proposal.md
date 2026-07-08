# ADR-0013 FIND Rerank — Findings & Proposal (investigate-only; no code changed)

Investigation round following the PARTIAL live validation. **No app code, no ADR edits,
no commit** — this is a finding + proposal for review. HEAD still `39b5ca4`.

---

## PART A — The EMR fork, resolved by passage text: **SUBSTANTIVE → mis-specified specimen**

I pulled the exact chunk the reranker scored `+4.304` for EMR on "artificial intelligence
risk", plus the ORCL and MSFT chunks it demoted, verbatim from the corpus.

### EMR — the chunk the reranker KEPT (rank #3, rr=+4.304)
Source: `sec.gov/Archives/edgar/data/32604/…/emr-20250930.htm`. The chunk contains a
**dedicated, headed AI risk factor** (excerpt):

> "**We May Use Artificial Intelligence in Our Businesses and in Our Products and Services,
> and Challenges With Managing its Use Could Result in Reputational Harm, Competitive Harm,
> and Legal Liability…** Our businesses increasingly rely on artificial intelligence
> solutions to optimize our operations… Our artificial intelligence efforts subject us to
> risks related to accuracy, intellectual property infringement or misappropriation, data
> privacy, and cybersecurity…"

Corroboration — a keyword scan of EMR's whole filing returns **5 AI/ML chunks**: the headed
risk factor above, a rapidly-evolving-AI-**regulation** passage, an AI-driven-cyberattack
passage, and a technological-change AI passage. Emerson's 10-K discusses AI risk
**substantively and by name**, with its own risk-factor heading.

### For contrast — the chunks the reranker DEMOTED
- **ORCL (#1 → #11, rr=−4.881)** — fundamentally a **cybersecurity** passage; AI is an
  incidental sub-clause: *"…techniques used to obtain unauthorized access to, or sabotage
  IT systems… The increasing use of AI technologies may also introduce or accelerate
  existing cybersecurity… risks."* The subject is cyber, not AI. High cosine (0.4878),
  off-topic for AI — **exactly the bi-encoder false positive the reranker exists to fix.
  Correct demotion.**
- **MSFT (#6 → #13, rr=−5.645)** — its chunk **is** substantive AI-risk content: *"AI
  presents risks and challenges… AI algorithms or training methodologies may be flawed…
  agentic AI systems that can take actions autonomously… could result in legal liability,
  regulatory action, brand… harm."* This is genuine AI-risk disclosure, yet the reranker
  scored it very low and demoted it — see the honest caveat below.

### Verdict (no hedge): EMR is **SUBSTANTIVE / legitimately on-topic.**
The reranker keeping EMR at #3 is **CORRECT behavior**, not a failure. The validation's
"FAIL (part 1)" is a **MIS-SPECIFIED SPECIMEN** — EMR was assumed off-topic, but Emerson's
filing carries a dedicated AI risk factor. EMR should not have been an "off-topic" specimen.

### Honest caveat that is NOT the EMR fork (but the maintainer should see it)
The **MSFT demotion is a genuine reranker precision miss.** MSFT's representative chunk is
real, substantive AI-risk disclosure, yet the MS-MARCO cross-encoder scored it −5.645 and
sank it below EMR. Likely cause: the chunk is long and opens with forward-looking *vision*
prose ("We envision a future in which AI…") and closes on datacenter operational risk — the
cross-encoder, trained on short QA-style relevance, appears to penalize the mixed/long
framing. So on the AI query the reranker is **net-mixed**: it correctly demotes ORCL
(cyber-with-AI-mention) but wrongly demotes MSFT (substantive AI). It is a precision
*improver*, not a precision *oracle*. This strengthens the case for Part B (make it opt-in,
not the default path).

---

## PART B — Latency blocker + off-by-default proposal

### B1. Current default (from the code): rerank is ON, and NOT per-request controllable

- `apps/api/research/rerank.py:33` — `RERANK_ENABLED = True`.
- `apps/api/research/find.py:111` — `rerank_enabled: bool | None = None`.
- `apps/api/research/find.py:173` — `enabled = RERANK_ENABLED if rerank_enabled is None else
  rerank_enabled` → with the param defaulting to `None`, this resolves to `True`.
- `apps/api/research/router.py:439-443` — the `find(q, engine, embedder)` endpoint takes
  **no rerank parameter**, and calls `find_companies(engine, embedder, q)` (line ~448) with
  no `rerank_enabled` → uses the ON default.

**Net: every FIND HTTP call reranks today → the measured ~3 s/query is paid on the cheap,
frequent, daily-briefing path. This directly fights ADR-0011's reason for FIND to exist.**

### B1 proposal (recommended): rerank OFF by default, opt-in per request

Principle: FIND stays cheap/fast by default (~270 ms); the ~3 s precision cost is paid only
when the caller explicitly asks — matching the system's cheap-by-default /
expensive-on-explicit-demand posture (ADR-0011 §3, ADR-0012 caps). The change (to make next
round, **not now**):

1. `rerank.py:33` — `RERANK_ENABLED = True` → `False`. The module constant becomes the
   library-wide default; `find_companies(...)` with no override then does cosine order.
2. `router.py` `find()` — add a request knob `rerank: bool = False` and pass it:
   `find_companies(engine, embedder, q, rerank_enabled=rerank)`. Default false = cheap; a
   caller sends `?rerank=true` to pay for precision on that one query. (This is a deliberate
   exception to the endpoint's "caps are not request knobs" comment at `router.py:444-446` —
   rerank is a cost/precision toggle, not a structural cap; note that in the comment.)
3. Test touch-ups that this default flip forces: `test_rerank.py:150`
   (`assert RERANK_ENABLED is True` → `is False`); the conftest autouse fixture
   (`conftest.py:23`) becomes redundant belt-and-suspenders — keep it (harmless, explicit)
   or drop it. No other test asserts the default.

Fit check: **good.** `find_companies` already threads `rerank_enabled` and `scorer`, and the
call-time resolution at `find.py:173` already reads the module global, so the flip needs no
plumbing — only the constant, one endpoint param, and one test assertion.

### B2. The two ADR claims the validation proved FALSE

**(i) Latency — FALSE by ~100×.** `docs/decisions/0013-…md:184` states *"steady-state
per-query rerank latency is tens of ms on CPU."* Measured: **+2.6 to +3.8 s/query**
(OFF ~270 ms → ON ~3 s), five queries. The entire "Risk accepted — first-run latency"
bullet and the implicit "cheap mode stays cheap" framing are wrong and must be rewritten to
state the real cost and to justify off-by-default. The cause is the cross-encoder scoring up
to ~80 (query, long-filing-chunk) pairs on CPU; filing chunks are long, so pairs are not
cheap.

**(ii) Cosine fallback — FALSE / not implemented.** `0013-…md:179-180` claims
`RERANK_ENABLED` *"provides a clean off-switch so an environment without the dependency
falls back to cosine ordering rather than failing"*, and `:199` repeats *"cosine ordering,
not failure."* `pyproject.toml:36` echoes *"without it, FIND falls back to cosine
ordering."* **Confirmed against the code: there is no such fallback.** `rerank.py` contains
no `try`/`except`; `_default_scorer` (`rerank.py:132-150`) does a bare
`from sentence_transformers import CrossEncoder`. With `RERANK_ENABLED=True` and the extra
uninstalled, the first FIND call raises `ImportError` → propagates through `rerank()` →
`find_companies()` → **500**, not a cosine fallback. The `_default_scorer` docstring
(`rerank.py:137-139`) also misleads: that path is reached whenever rerank is enabled and no
scorer is injected — **regardless** of whether the extra is installed.
  - Note: making rerank **off-by-default (B1)** *mostly* neutralizes this — the default path
    never calls `_default_scorer`, so an install without the extra only 500s if a caller
    explicitly opts into `rerank=true` without the extra. That is a defensible "you asked
    for it, install `.[rerank]`" contract — but the ADR/pyproject wording still overclaims
    an *automatic* fallback that does not exist and must be fixed regardless.

### B3. Ordered path to a committable state (no code this round)

1. **Correct ADR-0013 wording** (its own edit): latency line `:184` → real ~3 s with the
   measured table; fallback lines `:179-180`, `:199` → drop the automatic-fallback claim (or
   commit to implementing one — see step 4); §5 `:102` "behind a flag, default on" →
   "default **off**, opt-in per request." Fix `pyproject.toml:36` comment likewise.
2. **Set rerank off-by-default with per-request opt-in** (B1 steps 1–3).
3. **EMR:** no fix needed — mis-specified specimen; reranker behavior correct. Update the
   validation RESULTS doc's specimen list to note EMR was mis-specified, and (optional) add a
   genuinely off-topic AI specimen if a second clean ordering-case is wanted. SCHW/cyber
   remains the clean pass.
4. **Address correctness code-review items; defer cosmetic ones:**
   - *Fallback (correctness/claim-match):* either implement a real guard (wrap the import in
     `try/except ImportError` in `_default_scorer` and fall back to cosine / raise a clear
     "install `.[rerank]`" message) **or** delete the fallback claim in step 1. Pick one so
     code and docs agree.
   - *NaN guard (correctness):* add a NaN check after scoring in `rerank()` so a pathological
     `model.predict` NaN raises instead of silently corrupting the sort.
   - *Defer (cosmetic):* the `RERANK_CANDIDATE_LIMIT` double-clamp at `find.py:178` (harmless
     — `rerank()` re-clamps authoritatively); the stale docstring on
     `test_find.py:208` (`test_find_rejects_non_positive_caps`).
5. **Re-validate the corrected design:** confirm default FIND is back to ~270 ms (cheap path
   restored); confirm `?rerank=true` still demotes the true off-topic cases (SCHW on cyber,
   ORCL on AI); re-run `pytest` (update the one `RERANK_ENABLED` assertion) → expect green.
6. **Commit (one concern) + push.**

---

## Summary for your decision
- **EMR:** substantive → the reranker was right; the specimen was wrong. No reranker fix.
- **Latency:** real ~3 s/query — recommend **rerank off-by-default, opt-in per request**
  (`rerank.py:33` + a `rerank=false` endpoint param); the code already supports it.
- **Two ADR claims to correct before commit:** latency (~100× off) and the non-existent
  cosine fallback.
- **Honest extra:** the reranker is net-mixed on the AI query (correctly demotes ORCL,
  wrongly demotes MSFT) — a precision improver, not an oracle, which is itself an argument
  for opt-in.

Awaiting your call on EMR (agree it's mis-specified?) and on the off-by-default proposal
before any implementation round.
