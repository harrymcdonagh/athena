# 0016 — Single-Name Valuation Snapshot: Decision-Support Behind the Wall

**Status:** Accepted (2026-07-08)

## Context

Every module Athena has shipped lives on the **evidence** side of the
evidence/judgment wall (ADR-0007 §3): cited QA, change detection, FIND, and
COMPARE all state what a filing *says*, cited, and order by nothing a human
would read as a verdict. The wall was drawn deliberately, and every prior ADR
has held to it — FIND orders by `match_strength` (a retrieval fact, ADR-0011
§1), COMPARE makes cross-company ranking structurally unrepresentable
(ADR-0012). ADR-0007 §3 and ADR-0011 §5 both name a future **judgment layer**
that would require its own ADR, must be labelled as judgment, must build only
on evidence-layer outputs or read-only market data (FMP/Finnhub/Polygon/FRED —
**never** Trading 212), and must stay behind the wall.

**This ADR is that first crossing.** It introduces one small, walled module: a
single-name **decision-SUPPORT** valuation snapshot. Given a ticker, it pulls
that company's valuation picture from Financial Modeling Prep (FMP — the
Premium key is now active) and returns a cited, judgment-labelled read of where
the stock is priced **always against a named, cited comparator** — versus its
own multi-year history and versus two independent fair-value reference points —
with the peer set FMP names shown alongside (identity only, for the reader to
judge the grouping; decision #4). "Cheap" and "rich" never stand alone here: the
snapshot says "cheap **vs its own 10-year median**," never a bare "cheap"
(decision #8 makes this a register invariant). Decision-SUPPORT, not
decision-MAKING: it lays out the cited picture and surfaces divergences; **the
user decides.** No buy/sell/hold, no ranking of companies, no "therefore act."

**Cost is a design driver, and it is favourable here.** The answer model
NEVER sees a filing in this module. It reasons over a small *structured* block
of FMP numbers — a few hundred tokens of ratios and fair-value points — not
documents. Structured data in, compact cited snapshot out: this is the cheap
path, cheaper per call than QA or COMPARE, which pay to read filing text.

**This ADR is written against a live schema-capture probe, not FMP's docs**
(which publish no response shapes). A throwaway probe (`scratch/fmp_schema_probe.py`)
called each candidate `/stable/` endpoint once for AAPL on 2026-07-08 and saved
the raw JSON to `tests/fixtures/fmp/<endpoint>_AAPL.json`. All ten endpoints
returned HTTP 200 on this Premium key; **no endpoint 402/403'd** and none came
back empty. Every field name and history depth cited below is from those
fixtures. The probe surfaced one finding that overturns the naïve doc-guess and
is load-bearing for decision #3 — recorded there.

## Decision

1. **This is the judgment layer's first module, and it is labelled as such.**
   The snapshot is JUDGMENT, not evidence — that label is carried in the
   **return type**, not merely in prose (decision #6), so no caller can render
   a valuation read as if it were a cited filing fact. The module lives in its
   own walled package, separate from the evidence layer (`research/qa.py`,
   `find.py`, `compare.py` are untouched and do not import it). It builds only
   on read-only market data (FMP), consistent with ADR-0007 §3 and ADR-0011 §5.
   Scope is deliberately minimal: **one company, one snapshot.** The rejected
   alternative is folding valuation into the existing evidence endpoints — that
   would breach the wall by putting a judgment output on an evidence surface.

2. **The endpoint set, with the real field names from the fixtures.** All under
   base `https://financialmodelingprep.com/stable`, queried `?symbol=<TICKER>`:

   - `profile` — object-in-array; fields include `price`, `marketCap`,
     `beta`, `currency`, `range` (52-week), `lastDividend`, `companyName`,
     `sector`, `industry`, `cik`. The identity + current-price anchor.
   - `quote-short` — `symbol`, `price`, `change`, `volume`. A second, minimal
     price read (cross-checks `profile.price`).
   - `key-metrics-ttm` — trailing-twelve-month valuation metrics: e.g.
     `enterpriseValueTTM`, `evToEBITDATTM`, `evToSalesTTM`, `earningsYieldTTM`,
     `freeCashFlowYieldTTM`, `returnOnInvestedCapitalTTM`, `marketCap` (43 fields).
   - `ratios-ttm` — trailing multiples: `priceToEarningsRatioTTM`,
     `priceToSalesRatioTTM`, `priceToBookRatioTTM`,
     `priceToFreeCashFlowRatioTTM`, `enterpriseValueMultipleTTM`,
     `dividendYieldTTM`, `netProfitMarginTTM`, `grossProfitMarginTTM` (60 fields).
   - `key-metrics` — **historical** annual series (decision #3); each row
     self-dates with `date`, `fiscalYear`, `period`, `reportedCurrency` plus the
     same valuation metrics as the TTM variant without the `TTM` suffix
     (`evToEBITDA`, `evToSales`, `earningsYield`, `freeCashFlowYield`, …).
   - `ratios` — **historical** annual series; `date`/`fiscalYear`/`period`/
     `reportedCurrency` plus `priceToEarningsRatio`, `priceToSalesRatio`,
     `priceToBookRatio`, `priceToFreeCashFlowRatio`, `enterpriseValueMultiple`,
     `dividendYield`, `netProfitMargin`, `grossProfitMargin`.
   - `stock-peers` — array of peers, each `symbol`, `companyName`, `mktCap`,
     `price` (decision #4).
   - `discounted-cash-flow` — `symbol`, `date`, `dcf`, `Stock Price`. **The
     `"Stock Price"` key contains a space** and must be read by dict-key (or a
     Pydantic `Field(alias="Stock Price")`), never as a clean attribute — the
     future model must not assume tidy field names for this endpoint. Fair-value
     reference point #1 (decision #5).
   - `price-target-consensus` — `symbol`, `targetConsensus`, `targetHigh`,
     `targetLow`, `targetMedian`. Fair-value reference point #2 (decision #5).
   - `financial-scores` — `altmanZScore`, `piotroskiScore`, plus the inputs
     (`ebit`, `revenue`, `totalAssets`, …). **Captured but out of scope for the
     snapshot** — recorded here as a future increment (see Out of scope). AAPL
     returned `altmanZScore` 12.89, `piotroskiScore` 9.

   That is **~9–10 GET calls per snapshot** (financial-scores excluded from the
   live module: ~9). Rate-limit headroom on Premium is 750/min, so one snapshot
   is ~1.2% of the per-minute budget — non-binding at single-name scope.

3. **History depth is deep and cycle-aware — 20 annual periods, not the
   5-row default.** This is the finding the probe was run to get, and it
   overturns the doc-guess. `key-metrics` and `ratios` with **no** `limit`
   return only **5** annual periods — which would make "vs own history" a
   shallow, non-cycle-aware comparison. But that 5 is only the default `limit`:
   on this Premium key, `?limit=20` returns **20 annual periods (FY2006–FY2025)**
   and `?limit=40` returns **40 (FY1986–FY2025)** — verified live, fixtures
   captured at `limit=20`. **Consequence: "cheap vs own history" is genuinely
   cycle-aware** — a 20-year window spans the 2008–09 financial crisis, the 2020
   COVID shock, and the 2022 rate repricing, so a current multiple can be placed
   against a full-cycle distribution of the company's own history rather than a
   3–5-year recent slice. The module pulls `limit=20` (period=`annual`) as its
   default depth; `limit` is the explicit, tunable knob (raising it deepens the
   window and the token/rows cost together — tuned, never bypassed). The
   rejected alternative — accept the 5-row default — is rejected precisely
   because it would silently make the historical comparison shallow while
   looking complete. Which *current* multiple is placed against this history —
   and how it is cited — is decision #9.

4. **Peers come from FMP `stock-peers`, SHOWN and CITED as IDENTITY + market
   cap only — no per-peer multiples, no peer-median, in increment 1.** The
   snapshot never compares against an invisible peer set: the peer list
   (`symbol` + `companyName` + `mktCap`) is part of the return payload, tagged
   `[FMP, fetched <timestamp>]` (decision #7), so the user can see and judge the
   grouping. This matters because the grouping is **noisy and unreliable**:
   AAPL's 9 peers came back as GOOGL, META, MSFT, NVDA, **NXT, RIME, TBCH**,
   SONY, TSM — several are not credible large-cap comparables.

   Given that unreliability, the peer decision is deliberately minimal:
   **surface WHO FMP considers the peers (name + market cap) and let the reader
   judge the grouping — do NOT compute any peer-relative multiple in increment
   1.** That means neither a single peer-**median** over the set NOR each peer's
   **individual** multiples:
   - A peer-**median** P/E reduces a noisy, heterogeneous group to one number
     that reads as precise — manufacturing false precision even though the peers
     are cited, because the citation does not undo the fact that the median
     silently includes NXT/RIME/TBCH.
   - Showing each peer's **individual** multiples would need a `ratios-ttm` call
     *per peer* (~9 extra calls, taking a snapshot from ~9 to ~18+ GETs). That
     both breaks the "one ticker in, ~9 calls out" cost story (decision #2,
     Consequences) and is unnecessary to the increment-1 point: the peer set's
     job here is to let the reader see and filter the grouping, which
     name + market cap already does.

   Increment 1 therefore ships peer **identity** only. Per-peer individual
   multiples are **deferred to a later increment (its own decision)**, and a
   peer-**median** — even flagged for dispersion — is **rejected** (a caveat
   bolted onto a number that should not be computed over this set; structural
   avoidance beats an advisory warning, CLAUDE.md wall posture). A **user-named
   peer set** (user supplies the comparables) is likewise **rejected-for-now**:
   cleaner input, but it defers the "one ticker in, snapshot out" simplicity
   this increment is scoped to, and it can be added later without reopening this
   decision. Peer *ranking* by any desirability measure is out of scope and
   stays behind the wall (ADR-0011 §5).

5. **Two INDEPENDENT fair-value reference points, shown beside the current
   price — never blended into one number, never a verdict.** The snapshot
   surfaces three prices side by side and lets them disagree:
   - **Current price** — `profile.price` / `quote-short.price` (AAPL: **$310.66**).
   - **Fair-value point #1 — discounted cash flow:** `discounted-cash-flow.dcf`
     (AAPL: **$152.05**, `date` 2026-07-07), a model-intrinsic estimate.
   - **Fair-value point #2 — analyst consensus:** `price-target-consensus`
     (AAPL: consensus **$327**, median **$325**, range **$253–$400**), a
     sell-side aggregate.

   These are two *different kinds* of estimate — intrinsic-model vs
   crowd-of-analysts — and on AAPL they **disagree sharply** (DCF ~$152 well
   below price; consensus ~$327 slightly above). **This divergence is confirmed
   from the fixtures and is expected, not an anomaly** — DCF $152.05, consensus
   $327, current price $310.66, three numbers that do not reconcile. That is
   precisely what validates this decision: **the divergence IS the product.**
   The snapshot shows all three side by side and reconciles none — **both fair-
   value points are shown, never averaged, never collapsed into a single "fair
   value," never turned into "under/over-valued therefore act."** Blending them
   would destroy the one thing a decision-support tool should hand the user: the
   fact that two independent methods point in opposite directions, which the
   user must weigh.

6. **Output contract: a MANDATORY judgment label, and both sides shown on
   disagreement.** The return type carries:
   - a non-optional `layer: "judgment"` (or equivalent required discriminant)
     — every consumer sees this is a judgment output, structurally, not by
     convention;
   - **every figure paired with a provenance tag** — same provenance rigor as
     filing citations; the tag's form is governed by the provenance rule
     (decision #7);
   - **when two sources disagree, BOTH are shown**, never silently picked. If
     `profile.price` and `quote-short.price` diverge, both appear; and the DCF,
     the consensus target, and the current price are shown side by side and
     reconciled into nothing (decision #5).

7. **Provenance rule: self-dated figures cite their own date; live-snapshot
   figures cite the FETCH timestamp — never a false data-date.** The probe
   found FMP's payloads split cleanly into two provenance classes, and the
   contract treats them differently *by rule*, not case-by-case:
   - **Self-dated (3 endpoints)** — `key-metrics` and `ratios` (the *historical*
     series; each row carries `date` / `fiscalYear` / `period`, e.g. FY2025 with
     `date` 2025-09-27) and `discounted-cash-flow` (`date` 2026-07-07). Their
     as-of **is the datum's own date**, cited `[FMP, as-of FY2025]` or
     `[FMP, as-of 2026-07-07]`.
   - **Fetch-stamped (7 endpoints)** — `profile`, `quote-short`,
     `key-metrics-ttm`, `ratios-ttm`, `price-target-consensus`, `stock-peers`,
     and `financial-scores` carry **NO date field whatsoever** in the payload.
     This explicitly includes the two **TTM** endpoints — `key-metrics-ttm` and
     `ratios-ttm` — which are the source of the *secondary "most-recent"
     multiple* (decision #9 makes it secondary, not the leading number); a TTM
     figure is as-of the moment it was pulled, not any fiscal date, so it must be
     fetch-stamped even though it reads like a "latest" number. Their as-of is
     **the timestamp at which Athena pulled them**, cited
     `[FMP, fetched 2026-07-08T14:03]` (ISO, minute precision) — explicitly
     *fetched*, NOT dated as if FMP had timestamped it.

   All 10 probed endpoints are classified: 3 self-dated + 7 fetch-stamped, none
   unassigned. (`financial-scores` is fetch-stamped but out of scope for the
   snapshot, decision #2.)

   Why this is a first-class decision and not a formatting detail: stamping a
   live, undated quote with a *data-date* (e.g. presenting today's calendar
   date as though it were the source's own as-of) silently claims precision the
   payload does not carry — it tells the user "FMP states this was true as of
   DATE" when FMP stated no such thing. Conflating the two is exactly the
   silent-wrongness the project refuses (CLAUDE.md #5): a fetch-stamped figure
   is labelled *fetched*, a self-dated figure is labelled *as-of its date*. The
   distinction is carried structurally in the provenance type — a discriminated
   union (`Dated(date)` vs `Fetched(timestamp)`) rather than a free string — so
   mislabelling a live figure with a false data-date is unrepresentable, not
   merely discouraged.

8. **The plain-language read flags tension; it never prescribes action.** The
   snapshot may carry a short prose read at the register of *surfacing
   divergence*, never *recommending*. Target sentences (written here so the
   register is a fixed target, not left to the implementer):

   > "AAPL's ~34× FY2025 earnings [FMP, as-of FY2025] sits toward the upper end
   > of its own 20-year range [FMP, as-of FY2006–FY2025], while net margin holds
   > near 27% [FMP, as-of FY2025] — richer than its own history on the multiple,
   > even as profitability stays intact."

   > "Two independent fair-value points diverge: a discounted-cash-flow estimate
   > of ~$152 [FMP, as-of 2026-07-07] sits well below the current ~$311 [FMP,
   > fetched 2026-07-08T14:03], while the analyst consensus target of ~$327
   > [FMP, fetched 2026-07-08T14:03] sits slightly above it. The intrinsic-model
   > and sell-side views point in opposite directions; both are shown, neither
   > is reconciled into the other."

   > "This is a judgment-layer snapshot: it lays out the cited valuation picture
   > and flags where the sources pull in different directions. It does not tell
   > you whether to buy, hold, or sell — that is yours."

   Forbidden in this voice: "buy," "sell," "attractive," "overvalued/undervalued
   → act," or any single computed verdict. **"Cheap" / "rich" must ALWAYS carry
   a named, cited comparator** ("cheap vs its own 10-year median," "rich vs the
   DCF") — a bare "the stock is cheap," with no reference, is itself a drift
   toward verdict and is out. "Cheap vs its own history" is a *cited relative
   observation*; "cheap" alone, or "cheap, so buy," is not.

9. **The primary "current" multiple is the latest FISCAL-YEAR figure
   (self-dated); TTM is a SECONDARY, fetch-stamped "most-recent" datapoint.**
   There are two sources for a current multiple and they disagree in **both
   value and provenance**: the latest period of `key-metrics` / `ratios`
   (FY2025, self-dated, AAPL P/E **34.1×**, `[FMP, as-of FY2025]`) and
   `key-metrics-ttm` / `ratios-ttm` (trailing-twelve-month, fetch-stamped, AAPL
   P/E **37.5×**, `[FMP, fetched <ts>]`). This decision fixes which one leads,
   so the build cannot pick arbitrarily and cannot cite two different "current"
   numbers with two different provenance kinds and no ordering.

   The snapshot **leads with the latest fiscal-year figure** as the primary
   "vs own history" anchor, cited `[FMP, as-of FY2025]`, and shows the **TTM**
   figure as a **secondary "most-recent" datapoint**, clearly cited
   `[FMP, fetched <ts>]`. Rationale: the historical series this is compared
   against (decision #3 — 20 annual *fiscal-year* periods) is fiscal-year-based,
   so leading with the FY figure makes current-vs-history **apples-to-apples** —
   FY2025's 34.1× sits in the *same basis* as the FY2006–FY2024 distribution, so
   "toward the upper end of its own range" is a clean like-for-like statement.
   Leading with **TTM** against an FY series would be a **basis mismatch** that
   quietly wrong-foots the range comparison (comparing a trailing figure to
   fiscal-year points). TTM is **not discarded** — it is the freshest read and a
   genuine signal when the trailing quarters have moved off the last fiscal
   year-end — but it is secondary and is **never the number placed against the
   historical range**.

   This is why the flagship example (decision #8) leads "~34× FY2025 earnings
   [FMP, as-of FY2025]": that is the primary multiple *by this decision*, not an
   arbitrary pick — and it fully resolves the two-provenance-tags question of
   decision #7 (primary = self-dated `Dated`, secondary = `Fetched`; neither
   number is ambiguous about which it is). **Rejected: TTM-as-primary.** TTM is
   fresher, but the increment's headline comparison is explicitly "vs own
   *fiscal-year* history," and matching that basis matters more than freshness
   here; an off-cycle or data-poor case still surfaces TTM as the secondary read
   (and, per decision #7 / the M2 absence contract, the primary is marked absent
   if the latest FY period is itself missing).

**Out of scope** (future work, each its own decision, so the silence is chosen):

- **Increment 2 — the filing-bridge:** connecting this valuation read back to
  the evidence layer (e.g. "the multiple compressed the quarter the 10-K flagged
  demand softening"), which crosses back over the wall in the *controlled*
  direction and needs its own design.
- **`financial-scores` integration** (Altman Z / Piotroski): captured and
  working, but scoring is a distinct judgment surface and waits for its own
  increment.
- **Screen-and-surface** (run the snapshot across many companies) and
  **bounded multi-company valuation compare** — both re-open the ranking /
  cross-company-desirability question the wall forbids (ADR-0011 §5) and are
  explicitly not decided here.
- **Per-peer individual multiples** (increment 1b): a `ratios-ttm` fetch per
  peer to show each peer's P/E / EV/EBITDA / P/S beside the subject's — deferred
  from increment 1 to hold the ~9-call budget (decision #4). A peer-**median**
  stays rejected, not merely deferred.
- **User-named peer sets** (decision #4's rejected-for-now alternative).

## Compliance and Validation

What must be TRUE, and how it is checked (structural assertions preferred over
output-scanning):

- **The label is unforgeable.** `layer: "judgment"` is a required, non-default
  field on the return type — a snapshot cannot be constructed without it. Check:
  the type makes it non-optional (a schema/type assertion, not a test that scans
  strings).
- **No figure without provenance, and the right KIND of provenance.** Every
  numeric field in the snapshot is paired with a provenance; the type pairs
  value and provenance so an unprovenanced number is unrepresentable. Per the
  provenance rule (decision #7), that provenance is a discriminated union —
  `Dated(date)` for self-dated figures, `Fetched(timestamp)` for live-snapshot
  figures — so a live figure structurally cannot be stamped with a false
  data-date. Check: a type/schema assertion that the live-snapshot fields carry
  `Fetched`, not `Dated`.
- **Primary vs secondary current multiple, correctly typed.** The return type
  names the primary current multiple as the latest-fiscal-year figure carrying
  `Dated`, and the TTM figure as a distinct secondary field carrying `Fetched`
  (decision #9). Check: the two are separate fields with the two provenance
  kinds, so the "which current number, and how cited" choice is structural, not
  a build-time toss-up.
- **No blended fair value.** The return type exposes current price, DCF, and
  consensus as **separate** fields — there is no `fairValue` field to blend them
  into. Structural, per decision #5.
- **Peers are identity-only; no peer-relative multiple.** The peer entries on
  the return type carry `symbol` / `companyName` / `mktCap` and **no multiple
  field at all** — neither a per-peer P/E nor an aggregate/median-over-peers.
  The false precision of decision #4 is unrepresentable, and the ~9-call budget
  is structural (no per-peer fetch exists to make). Check: peer entries expose
  no multiple field, and there is no peer-aggregate field on the type.
- **The evidence layer does not import the judgment module** (mirrors ADR-0011
  §1's import-boundary contract): `qa.py`/`find.py`/`compare.py` gain no import
  of the new package. Check: import-direction assertion.
- **No verdict vocabulary in Athena's own voice** (wall-guard): the prose read
  is validated against the ADR-0007 §3 register — cited relative observations
  only, no buy/sell/attractiveness. Live-validate on ≥2 tickers, including one
  where DCF and consensus disagree (AAPL is the named specimen — DCF $152 vs
  consensus $327) and one where they roughly agree, confirming the snapshot
  *shows the divergence* in the first and *does not manufacture* one in the
  second (live-validate-twice, precedent 7973c0a).

## Consequences

- **Changes:** Athena gains its first judgment-layer surface — a single-name
  valuation snapshot — behind a new walled package, and a new external
  dependency (**FMP**, Premium key) alongside SEC EDGAR, Voyage, and Anthropic.
  A new config field (`fmp_api_key`) and a narrow FMP client are added (their
  own reviewed change; no financial-API code is written before this ADR is
  accepted, per house rules). The snapshot is a cited, judgment-labelled read
  over ~9 FMP calls of structured numbers.
- **Unchanged:** every evidence-layer guarantee — cited QA (ADR-0007), change
  detection (ADR-0009), FIND's zero-answer-model contract (ADR-0011 §1),
  COMPARE's no-ranking seam (ADR-0012) — is untouched; the evidence layer does
  not import this module; the wall itself is not weakened but *crossed under its
  own stated conditions* (labelled judgment, read-only market data, its own ADR).
- **Risk accepted:**
  - *The wall is now crossed.* Mitigation is the whole structure of decisions
    #1/#5/#6/#7: the output is structurally labelled judgment, provenance is
    mandatory and correctly typed, fair-value points are unblendable, and no
    action language is permitted — the crossing is bounded to *decision-support*,
    and any move toward decision-*making* requires a new ADR.
  - *Provenance/as-of burden.* Seven of the ten probed endpoints don't self-date
    (six of them used in the snapshot; `financial-scores` is out of scope), so
    the module must fetch-stamp and label them honestly (decision #7). The remedy
    is structural: value and provenance are paired in the type as a `Dated` /
    `Fetched` discriminated union.
  - *Peer-list noise* (decision #4): FMP peers can be off (NXT/RIME/TBCH for
    AAPL). Remedy: peers are shown as identity + market cap and cited so the user
    filters them; the snapshot never silently trusts the set, computes no
    peer-relative number over it, and so cannot launder the noise into a figure.
  - *FMP as source of truth for fair value.* DCF and consensus are FMP's
    computations, not Athena's; they are cited **as FMP's**, never restated as
    Athena's own estimate.
  - *Only AAPL (data-rich) was probed (M2).* All ten endpoints returned full
    payloads for AAPL; a thinner ticker may return an empty `stock-peers`, no
    `discounted-cash-flow`, or no `price-target-consensus`. The contract is
    **honest absence / graceful degradation**: each snapshot section is
    independently nullable with a reason (`no_dcf` / `no_peers` / `no_consensus`),
    the snapshot shows what is available and marks the rest explicitly absent,
    and it **never fabricates or zero-fills** a missing figure (CLAUDE.md #5).
    Full sparse-data behaviour is a build-round live-validation item — validate
    the absence path on a chosen data-poor ticker before behavioural sign-off,
    not just the AAPL-rich path.
- **Token cost:** materially *lower* than QA/COMPARE — the answer model reads a
  few hundred tokens of structured numbers, never a filing. This is the cheap
  path by construction (Context, decision #1).
- **Build sequencing:** first increment is this single-name snapshot with a
  mocked FMP client reviewed before any live spend (mocked-build-then-review-
  then-apply), with `get_...()`-style single live-swap point. `financial-scores`
  integration and the increment-2 filing-bridge are named future increments and
  wait for their own ADRs.
