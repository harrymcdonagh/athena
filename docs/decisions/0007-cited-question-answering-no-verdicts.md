# 0007 — Cited Question-Answering: Analytical, Evidence-Grounded, No Buy/Sell Verdicts

**Status:** Accepted

## Context

Phase 2 (ADR-0006) returns cited chunks for a search query. The QA layer sits
an LLM between that evidence and the answer the user reads — the first point
in the pipeline where Athena's auditability can break, because generated prose
can silently drift from what the retrieved chunks actually say.

Athena's purpose is not only to retrieve evidence but to help the user *learn*
to analyze companies. An answer layer that hands down conclusions would
undercut that goal even if every citation checked out.

## Decision

The answer-writer is **analytical but grounded**: it may reason *about* the
evidence, never *beyond* it, and it never renders a verdict.

1. **It MAY:**
   - group evidence by theme;
   - note agreement or contradiction across filings;
   - connect a stated figure to a cause **the filing itself states**;
   - frame what different investor lenses (e.g. growth, value, credit) would
     weigh in the evidence;
   - report the filing's own evaluative language, **attributed** — this is
     encouraged, not merely tolerated. "Apple characterizes its liquidity as
     strong" (cited) shows the user how management frames things, which is
     itself pedagogically useful. Don't scrub the documents' actual voice;
     attribute it.

2. **Buy/sell-shaped questions** get a two-sided cited case — bull case, bear
   case, what changed since the prior filing — and an explicit statement that
   the verdict is the user's. The question is answered analytically, not
   refused, but no side is endorsed.

3. **It MAY NOT:**
   - issue a buy/sell/hold recommendation or price target;
   - rank companies by "attractiveness";
   - adopt an evaluative stance **in Athena's own voice** ("strong,"
     "concerning," "impressive"). The line is attribution: reporting the
     source's framing, cited, is fine (see the MAY list); Athena
     editorializing is banned. The same word can be either — what matters is
     whose judgment it carries.

4. **Grounding (hard constraint):** answers use ONLY retrieved chunks — never
   the model's background knowledge of the company. Every claim must trace to
   a chunk's `source_url`, extending the ADR-0005/0006 audit chain
   (answer → chunk → document → EDGAR URL) through the generated text. When
   the retrieved evidence is insufficient, the correct answer is "the
   retrieved filings don't address this" — not a fallback to model knowledge.

5. **Temporal comparison is data-gated.** "What changed since last
   quarter/year" is a grounded claim only once the corpus holds multiple
   filings for the same company across time. With today's
   single-10-K-per-company corpus, the honest answer to a "what changed"
   question is that the retrieved filings don't include a prior period to
   compare — NOT a comparison synthesized from the model's background
   knowledge. This closes the most likely outside-knowledge leak: the model
   often *knows* the prior-period numbers, and must not use them.

**Rationale.** The no-verdict boundary serves both core commitments at once.
Auditability: a verdict is exactly the kind of claim that cannot trace to a
chunk. Pedagogy: showing the cited two-sided case teaches the reasoning;
substituting a judgment would teach the user to consume conclusions instead of
forming them.

## Consequences

- **Enforcement and testing become future work** (described here, not built):
  - *Grounding checks* — verifying each answer claim is supported by a cited
    chunk, so drift beyond evidence is detectable, not just prohibited.
  - *Verdict-language detection* — tests that recommendation-shaped and
    evaluative phrasing does not appear unattributed in answers.
  - *Refusal-shape tests* — out-of-scope asks ("should I buy?", "which is
    more attractive?") must produce the two-sided-case or
    insufficient-evidence shape, not a verdict and not a bare refusal.
- **Tradeoff accepted:** Athena is deliberately less "decisive" than a
  recommendation engine. In exchange, every answer is auditable to EDGAR and
  the tool builds the user's analytical skill rather than replacing it.
- Prompt wording that implements these rules becomes load-bearing policy;
  changes to it should be reviewed against this ADR, not treated as copy
  tweaks.
