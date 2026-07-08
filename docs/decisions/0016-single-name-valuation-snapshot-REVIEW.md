# 0016 — Review Pass (pre-acceptance)

Review of the sharpened ADR-0016 draft. Pressure-tests the decisions (8 in
rounds 1–2; 9 after the round-3 L2 promotion), the
wall-labelling, and the register of the 3 example sentences. Findings are
ordered by severity. Review findings can be wrong — the maintainer overrides
with reasoning (CLAUDE.md #7). Nothing is accepted; this pass precedes the
maintainer's own review-prompt pass.

> **Round 2 (this pass' resolution) is recorded at the bottom of this file** —
> Round 1's High/Medium findings below are the *original* pass; the Round 2
> section confirms each is closed and reports the two new contradictions the
> fixes introduced (both fixed).

## High

### H1 — Peer multiples require a fetch PER PEER; the ~9-call budget is wrong.
Decision #4 commits to showing **each peer's individual multiples (P/E,
EV/EBITDA, P/S)**. But the `stock-peers` fixture returns only `symbol` /
`companyName` / `mktCap` / `price` — **no multiples**. To show a peer's P/E the
module must call `ratios-ttm` (or `key-metrics-ttm`) *for that peer*. AAPL
returned **9 peers**, so decision #4 implies **~9 extra GET calls**, taking a
snapshot from the "~9–10 calls" stated in decision #2 / Consequences to **~18+**.
The call-count claim and the peer decision contradict each other.
**Resolve one way:** either (a) cap peers shown (e.g. top N by `mktCap`) and
restate the budget as ~9 + N; or (b) for increment 1 show peers as identities +
`mktCap` + `price` only (no per-peer multiples), deferring peer multiples to a
later increment. (b) keeps the "one ticker in, ~9 calls out" cost story intact
and still lets the reader see the peer set to filter it. Recommend (b) unless
per-peer multiples are essential now — in which case say ~9 + N explicitly.

### H2 — The provenance rule (decision #7) omits the two TTM endpoints.
Decision #7 classifies self-dated = {key-metrics, ratios, dcf} and fetch-stamped
= {profile, quote-short, price-target-consensus, stock-peers, financial-scores}.
**`key-metrics-ttm` and `ratios-ttm` appear in neither list.** Both carry **no
date field** (their fixtures have only `…TTM` fields + `symbol`), so they are
fetch-stamped — and they are the source of the *current* headline multiple the
snapshot leads with. The enumeration must add them to the fetch-stamped class,
or the rule has a hole exactly where the most-used number lives.

### H3 — The flagship example sentence mis-tags a TTM figure with a fiscal-year as-of.
Decision #8's first example: *"AAPL trades at ~34× trailing earnings [FMP,
as-of FY2025] …"*. The fixtures make the mismatch exact: **TTM P/E is 37.5×**
(`ratios-ttm`, fetch-stamped per H2), while **FY2025 historical P/E is 34.1×**
(`ratios`, self-dated). So the *number* (34) and the *tag* (`as-of FY2025`) are
mutually consistent — they are the FY2025 historical figure — but the *word*
**"trailing"** describes a TTM figure, which is 37.5× and would need a
`[FMP, fetched …]` tag. The sentence therefore labels a fiscal-year datum as
TTM, blurring the very self-dated/fetch-stamped line decision #7 draws, inside
the ADR's own target sentence. Fix: either write "~34× FY2025 earnings [FMP,
as-of FY2025]" (drop "trailing"), or "~37× trailing earnings [FMP, fetched …]"
(use the real TTM number and a fetched tag). Do not keep "trailing" + `as-of
FY2025` + 34.

## Medium

### M1 — "Whether the stock looks cheaply or richly priced" is stated absolutely in Context/decision #1.
Decision #8 carefully defends "cheap **vs its own history**" as a *cited
relative* observation and forbids bare verdicts. But Context (line ~22) and the
decision-#1 framing say the snapshot reads "**whether the stock looks cheaply or
richly priced**" with no reference anchor. An *absolute* "this is cheap/rich" is
materially closer to a verdict than "cheap vs its own 20-yr range / vs peers / vs
DCF." Tighten the top-level framing so "cheap/rich" is **always** bound to a
cited reference (own history, peers, or a fair-value point) — never a bare
characterization. This is the highest-leverage register fix; the rest of the
register holds.

### M2 — Behavior on data-poor tickers is undefined (only AAPL was probed).
Every guarantee rests on a single data-rich name. A small/foreign ticker may
return an empty `stock-peers`, no `discounted-cash-flow`, or no
`price-target-consensus`. The contract does not say how a missing sub-result is
represented. Per CLAUDE.md #5 (honest absence over silent wrongness), specify
that each snapshot section is independently nullable with a reason
(`no_dcf` / `no_peers` / `no_consensus`), never silently dropped or zero-filled —
and note the fixtures validate only the data-rich path, so the absence path needs
its own live-validation on a chosen data-poor ticker before acceptance-in-fact.

## Low

### L1 — "vs own history" mixes a live-price current multiple against fiscal-year-END historical multiples.
The historical `ratios` multiples are computed at each fiscal year-end price;
the "current" multiple is against today's live price. The comparison is
directionally sound but not an identical basis. Worth a one-line note in
decision #3 so the build (and any prose) treats "vs own history" as directional
positioning, not a precise like-for-like.

### L2 — `key-metrics-ttm` vs the latest `key-metrics` row are near-redundant.
Both can supply a "current-ish" metric. State which is authoritative for the
headline multiple (recommend the TTM endpoints for "current," the historical
series for the distribution) so the build doesn't cite two slightly different
"current" numbers.

## Register verdict

The 3 example sentences are at the right register **except H3's tag error** and
the **M1 absolute-framing drift**. Sentence 3 ("It does not tell you whether to
buy, hold, or sell — that is yours") is exactly right and should stay. No
sentence prescribes action; the forbidden-vocabulary list is good. Fixing H3 and
M1 makes the register airtight.

## Not blocking / confirmed good

- Wall-labelling (decision #1) is structural (`layer:"judgment"` non-optional),
  not prose-only — correct.
- No-blended-fair-value (decision #5) and no-peer-median (decision #4) are both
  enforced as *absent fields on the type*, not tests — correct posture.
- The `"Stock Price"` space-key note (decision #2) is captured for the future
  model.
- The DCF/consensus/price divergence is real from the fixtures ($152 / $327 /
  $311) and correctly framed as the product, reconciled by nothing.

---

## Round 2 — resolution pass (after H1/H2/H3/M1 fixes, M2 recorded)

Re-reviewed the corrected ADR. Each Round-1 finding is closed; two *new*
contradictions were introduced by the fixes and both were corrected in the same
pass. No open contradiction remains.

### Round-1 findings — status

- **H1 (peer call-budget) — RESOLVED.** Decision #4 now ships peer **identity +
  market cap only** in increment 1; per-peer individual multiples are deferred
  to increment 1b (added to Out of scope), and a peer-median stays *rejected*
  (not deferred). The `~9`-call budget is now internally consistent across
  decision #2, decision #4, Consequences, and the Compliance check (which
  asserts peer entries carry *no* multiple field — the ~18-call path is
  structurally absent, not merely unused).
- **H2 (provenance hole) — RESOLVED.** Decision #7 now enumerates **3 self-dated
  + 7 fetch-stamped = all 10 probed endpoints**, with `key-metrics-ttm` and
  `ratios-ttm` explicitly named in the fetch-stamped class and the reason given
  (TTM is as-of pull time, not a fiscal date). The "none unassigned" line makes
  the completeness claim checkable.
- **H3 (flagship mis-tag) — RESOLVED and re-verified against fixtures.** The
  example now reads "**~34× FY2025 earnings [FMP, as-of FY2025]**". Fixture
  check: `ratios_AAPL.json` FY2025 `priceToEarningsRatio` = 34.11 → 34 is the
  FY2025 figure, tag `as-of FY2025` is correct, and "trailing" (which would mean
  the 37.5× TTM figure) is gone. The number, the word, and the tag now agree,
  and the sentence models decision #7 instead of violating it. Bonus: FY2025
  P/E vs the FY2006–FY2025 *range* is now a like-basis comparison (FY vs FY),
  tightening L1 as a side effect.
- **M1 (bare cheap/rich) — RESOLVED.** Context now states "cheap/rich never
  stand alone" and always carry a cited comparator; decision #8 adds it as an
  explicit **register invariant** ("'Cheap'/'rich' must ALWAYS carry a named,
  cited comparator … a bare 'the stock is cheap' … is out"). Scan of all three
  example sentences: none uses a bare cheap/rich; the only remaining "cheap"
  occurrences are the *cost*-path meaning ("the cheap path") and the invariant
  statements themselves.
- **M2 (only AAPL probed) — RECORDED, not solved (as instructed).** Consequences
  now carries an "Only AAPL (data-rich) was probed" risk: each snapshot section
  is independently nullable with a reason (`no_dcf`/`no_peers`/`no_consensus`),
  the snapshot degrades gracefully and never fabricates/zero-fills, and the
  sparse-data absence path is named a build-round live-validation item.

### New contradictions introduced by the fixes — both FIXED

- **N1 — Context over-promised a peer comparison after H1 descoped peers.** The
  M1 Context rewrite briefly said the read is priced "versus the peers FMP
  names," but H1 reduced peers to identity-only (no price-vs-peer comparison in
  increment 1). Fixed: Context now anchors "cheap/rich" to *own history* and the
  *two fair-value points* only, and lists the peer set as "shown alongside
  (identity only, for the reader to judge the grouping)" — consistent with
  decision #4.
- **N2 — Decision #7 called TTM "the current headline multiple the snapshot
  leads with," but the flagship example leads with the FY2025 figure.** Fixed:
  decision #7 now calls TTM "the current trailing-twelve-month multiple … reads
  like a 'latest' number" without claiming it is what the prose leads with, so
  it no longer conflicts with example #1's FY2025-vs-history framing.

### Still-open (unchanged from Round 1, non-blocking)

- **L1** (own-history basis) — materially reduced by the H3 fix (the example is
  now FY-vs-FY). A one-line note in decision #3 that "vs own history" uses
  fiscal-year multiples (not the live-price current multiple) would fully close
  it; optional, build-time.
- **L2** (`key-metrics-ttm` vs latest `key-metrics` row near-redundant) —
  unchanged; a build-time "which endpoint is authoritative for the current
  number" note, not an ADR blocker.

### Register verdict (Round 2)

Airtight. All three example sentences are comparator-anchored, none prescribes
action, and sentence 3 ("does not tell you whether to buy, hold, or sell — that
is yours") remains the correct closing register. The H3 tag fix removes the last
place the ADR contradicted its own provenance rule.

**Recommendation:** the four blocking findings (H1/H2/H3/M1) are closed and the
two induced contradictions (N1/N2) are fixed; only optional build-time notes
(L1/L2) remain. Ready for the maintainer's own review-prompt pass. Not flipped
to Accepted.

---

## Round 3 — final check (L2 promoted to a decision)

L2 was promoted from a build-note to **decision #9**; L1 stays a build-note as
directed. Re-checked #9 against the flagship example, decision #7's provenance
classes, decision #3, and Context. No new contradiction.

### L2 → decision #9 — the choice, checked for consistency

- **The decision, stated (not left to the build):** the **primary** current
  multiple is the **latest fiscal-year** figure (self-dated, AAPL P/E 34.1×,
  `[FMP, as-of FY2025]`); the **TTM** figure is a **secondary** "most-recent"
  datapoint (fetch-stamped, AAPL P/E 37.5×, `[FMP, fetched <ts>]`). Each carries
  its own provenance tag; the leading number and its citation are now fixed.
- **Reason recorded:** the historical series (decision #3) is fiscal-year-based,
  so FY-vs-FY is a like-for-like "vs own history" comparison; leading with TTM
  against an FY range is a basis mismatch. TTM is kept as the fresher secondary
  read, never the number placed against the range. (TTM-as-primary explicitly
  rejected, with justification — the increment's headline comparison is "vs own
  *fiscal-year* history.")
- **Consistent with the flagship (decision #8):** the example leads "~34× FY2025
  earnings [FMP, as-of FY2025]" — which is now the *primary multiple by decision
  #9*, not an arbitrary pick. Flagship and decision agree.
- **Consistent with decision #7:** primary = `Dated`, secondary = `Fetched`;
  decision #7's TTM bullet now points forward to #9 ("secondary, not the leading
  number"). This **fully resolves N2** (the earlier "which number leads" tension)
  structurally, not by wording alone — the two are distinct typed fields, and a
  new Compliance check asserts primary-`Dated` / secondary-`Fetched`.
- **Consistent with Context:** Context anchors "cheap/rich" to "its own 10-year
  median" (a fiscal-year basis) and lists TTM nowhere as the lead — no conflict.
- **Consistent with decision #3:** a forward-pointer was added at the end of #3
  ("Which current multiple is placed against this history … is decision #9").

### New contradiction scan — none

- Numbering is sequential 1–9; every `decision #N` cross-reference resolves to an
  existing decision (#1–#9). No stale count remains in the ADR.
- No section still implies TTM is the lead or that peers carry multiples.
- The M2 absence contract composes with #9: if the latest FY period is missing,
  the primary is marked absent (honest absence), with TTM available as secondary.

### L1 — kept as a build-note (unchanged, as directed)

"'vs own history' uses fiscal-year multiples" remains a one-line build-round
note. The #9 FY-primary choice already makes the *basis* explicit; L1 is now
just the build reminder that the comparison is FY-based, not live-price-based.

### Verdict

The four Round-1 blockers (H1/H2/H3/M1) were closed in round 2; the two induced
contradictions (N1/N2) were fixed; L2 is now stated as decision #9, consistent
across the flagship, #7, #3, and Context, with N2 resolved structurally. Only
L1/L2-adjacent build-time notes remain, and L2 is no longer one of them.

**ADR-0016 is acceptance-ready.** No open contradiction, one concern, no new
endpoint, no module code. Not flipped to Accepted — awaiting the maintainer's
word after their read of the final state.
