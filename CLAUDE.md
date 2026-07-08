# Athena — Claude Guide

## What Athena is

Athena is a personal Investment Research Operating System. It retrieves cited
evidence from SEC filings and explains analytical reasoning to support the
user's own research. **It NEVER makes buy/sell recommendations, never ranks
companies by attractiveness, and never places trades.** All conclusions are
the user's sole responsibility (ADR-0007).

## Directory Map

```
apps/api/            FastAPI backend (Python 3.12+)
apps/api/research/   Evidence layer: qa, find, compare, batch, embeddings, …
apps/api/edgar/      SEC EDGAR client + section extraction
apps/api/migrations/ Alembic revisions (explicit raw SQL, per ADR-0003/0005)
apps/web/            React + Vite + TS research terminal (committed 12bc1a3)
packages/            Shared Python packages (future)
docs/decisions/      ADRs 0001–0012 — read before touching affected code
docs/domain/         Domain knowledge (curated ticker list)
docs/superpowers/    Plans and specs from past build slices
.claude/skills/      Project skills: adr-workflow, wall-guard,
                     commit-discipline, live-validation, filing-analysis
```

## Commands

```bash
source .venv/bin/activate            # activate venv (python3.12)
uvicorn apps.api.main:app --reload   # start API → http://localhost:8000
pytest                               # run all tests
ruff check . && ruff format .        # lint + format
mypy apps/                           # type check
python -m apps.api.research.batch    # batch ingestion (then embeddings backfill)
python -m apps.api.research.embeddings   # embedding backfill
```

## The evidence/judgment wall (load-bearing — never compromise)

- The evidence layer (cited QA, change detection, FIND, COMPARE) is
  structurally separate from any future judgment layer.
- **No verdict fields anywhere in evidence-layer schemas.** No buy/sell/hold,
  no price targets, no attractiveness ranking, no evaluative language in
  Athena's own voice (attributed source language, cited, is fine — ADR-0007 §3).
- FIND results are ordered by `match_strength` — a retrieval fact about how
  well filing TEXT matched the query — never by company judgment.
  `apps/api/research/find.py` must never import an answerer module; the
  zero-answer-model path is its contract (ADR-0011 §1).
- COMPARE synthesizes per-column behind a `(filing_id, query)` seam with no
  passage parameter, so cross-company ranking is unrepresentable; no
  superlatives (most/best/least/worst), no computed ordering, caps enforced
  as REFUSALS, never silent truncation (ADR-0012).
- A future judgment layer requires its own ADR, must be labeled as judgment,
  builds only on evidence-layer outputs, and uses read-only market data
  (FMP/Finnhub/Polygon/FRED — never Trading 212).
- **Prefer structural enforcement over test enforcement:** make violations
  impossible by construction — the draft/resolved citation split in change
  detection (model drafts chunk labels; URLs stamped from the database),
  import boundaries in FIND, the no-passage-parameter seam in COMPARE — not
  merely caught by tests.

## Working disciplines (non-negotiable)

1. **ADR before migration.** Any schema, behavioral, or architectural change
   starts with a draft ADR in Nygard format in `docs/decisions/`, gets a
   review pass, is accepted, then implemented. Never write a migration — or
   any database/financial-API code — without an accepted ADR.
2. **One concern per commit.** Do not entangle unrelated changes.
3. **Mocked-build-then-review-then-apply.** External API calls are mocked in
   the initial build; the build is reviewed before any live spend.
4. **Live-validate-twice for behavioral work.** Confirm the defect cleared
   AND that adjacent legitimate behavior still emits (precedent: 7973c0a).
5. **Honest absence over silent wrongness.** Failures are categorized and
   reported (`unresolved` ≠ `not_found` — a bad list entry is not "EDGAR had
   nothing"); missing data is clearly absent, never partially/silently wrong.
6. **No-change-on-a-dimension is a first-class outcome** in change detection
   (ADR-0009 §5); likewise `no_finding` in COMPARE (ADR-0012 #5).
7. **Review findings can be wrong.** The maintainer overrides incorrect
   findings; flag disagreement with reasoning rather than silently complying.
8. **Never commit secrets.** No .env files, API keys, or tokens in tracked
   files; `.env.example` documents names only.
9. **Verify before claiming done.** Run `ruff check .` and `pytest`; show
   output. Edit existing files over creating new ones.
10. **Keep the record current.** When a commit changes state this file
    documents (Current state, Pending queue, test count, corpus counts, stack,
    directory map) or that a README asserts (stack, run story, status), update
    the relevant doc *in the same commit* — reverify numbers (pytest count, DB
    corpus counts) rather than editing them from memory. A `Stop` hook
    (`.claude/hooks/check-docs-drift.sh`) backstops this: after a commit that
    changed code but not the docs it reminds once to check for drift. The hook
    only reminds; the judgment and the edit are yours — if nothing documented
    changed, say so and move on.

## Current state (2026-07-08)

- **Evidence layer complete and live through ADR-0012.** Ingestion →
  embeddings (per-filing summaries now lazy, ADR-0014 below) → cited QA
  (ADR-0007) → temporal corpus (ADR-0008)
  → change detection with balanced per-period retrieval and mandatory
  both-period citation (ADR-0009) → `sec_ticker_reference` as an external SEC
  cache, distinct from `companies` (ADR-0010) → FIND cross-company mode with
  zero answer-model calls (ADR-0011) → COMPARE live (df8f047, answerer swapped
  from the f7c221a mock): CIK-deduped named set ≤5, refusal at cap,
  filing-pinned per-column synthesis, coverage counts, live-validated
  2026-07-07 (see Pending queue).
- **Lazy on-demand summarisation landed** (ADR-0014, 7a20069): ingest no longer
  summarises — it writes each section's `source_text` eagerly (the retrieval
  substrate the embeddings backfill reads) and leaves `summary` NULL (pending).
  `GET /companies/{ticker}/summary` is the SOLE compute surface: it summarises
  pending sections inline via `ResearchService.summarize_on_demand`, caches them
  in place (UPDATE guarded `WHERE summary IS NULL`), and composes the thesis
  lazily on first demand; a second read is a cache hit. `POST /research/{ticker}`
  NEVER computes — the "ingest never spends" invariant holds under every call
  path. The eager ~$12–18/full-corpus summary spend is now conditional, paid
  only on an explicit GET. Migration 0005 makes `filing_summaries.summary`
  nullable; `source_text` stays NOT NULL. Live-validated against Postgres:
  migration round-trips clean (0004↔0005), the 255 existing summary rows stay
  non-pending, no duplicate-revision/multiple-heads error.
- **Summariser tier decided — keep Sonnet 5** (ADR-0015, Accepted 2026-07-08,
  no code change): `summarizer.py` stays `claude-sonnet-5`; QA/COMPARE stay
  `claude-opus-4-8` (`qa.py`). Haiku 4.5 was A/B'd (COST/NVDA/LLY, all 3
  sections, both models — evidence in `0015-summariser-model-tier-AB.md`): it
  holds every material figure but breaks the 300–500-word / thesis-selective
  contract (1.6–2× long, exhaustive extractions), and post-0014 the ~$0.15/
  company-summary delta is trivial at lazy per-demand volumes. One-line
  revertible; REOPEN on a bulk-resummarise event (deferred S&P 500 breadth run,
  hundreds × $0.15) → re-run the A/B then.
- **Judgment layer opened — ADR-0016 Accepted (2026-07-08), build pending.**
  First deliberate crossing of the evidence/judgment wall (ADR-0007 §3): a
  single-name, decision-SUPPORT valuation snapshot over FMP (Premium key) —
  structured numbers in, a cited judgment-labelled snapshot out; the answer
  model NEVER sees a filing (the cheap path). 9 decisions, incl. a mandatory
  `layer:"judgment"` label carried in the return type; a `Dated`/`Fetched`
  provenance split (self-dated key-metrics/ratios/dcf vs fetch-stamped live
  snapshots incl. the -ttm endpoints); DCF-vs-consensus-vs-price shown side by
  side, never blended; identity-only peers (no peer-median over FMP's noisy
  set); and an FY-primary / TTM-secondary current multiple. Schema-captured
  live against AAPL fixtures (`tests/fixtures/fmp/`; history depth 20yr
  FY2006–2025 at `limit=20`). Docs-only so far — NO module yet; review evidence
  in `0016-…-REVIEW.md` (3 rounds). Build is a later session.
- **Frontend landed** (12bc1a3, 2026-07-07): `apps/web` is a Vite + React + TS
  research terminal (find / research+compare / passages) over the local API;
  backend has narrow CORS for the Vite dev origin (e4acb4f). ADR-0002 amended
  to record Vite-not-Next (f21bc30).
- **Corpus:** 84 companies / 85 filings / 8,277 chunks (verified against the
  DB 2026-07-07) from a 101-symbol S&P 100 snapshot
  (`docs/domain/sp100-tickers.txt`); 16 filers known-absent (10-Ks that
  incorporate sections by reference defeat section extraction): BNY, C, COP,
  CVX, DE, GE, HON, IBM, INTC, JPM, MCD, MS, NOW, USB, WFC, XOM. A raw
  ticker diff also shows GOOGL — not absent: it shares Alphabet's CIK with
  GOOG, which is ingested (companies are keyed by CIK).
  Extraction repair f5f39ee re-embedded 13 corrupted filings; corpus clean.
- **Tests:** 287 collected and passing (`pytest` with Postgres up, verified
  2026-07-08); the DB-backed suite skips when Postgres is down.
- **Batch ingestion** reports categorized failures (`unresolved`, `not_found`,
  `parse_error`, `rate_limited`, `other`) with the accounting invariant
  ingested + skipped + failed == attempted; section-plausibility warnings are
  a distinct channel from failure.
- **FIND recall knobs already widened and committed** (3687d8d):
  `WIDE_SEARCH_LIMIT` 40→80, `CANDIDATE_N` 10→15, `PASSAGES_PER_COMPANY`
  stays 3; live validation confirmed HD (tariff) and GOOG (AI-risk) surface.

## Pending queue

- **COMPARE is LIVE and validated** (2026-07-07): `get_column_answerer()`
  swapped to the Claude-backed answerer; live-validated on three specimen
  runs (GOOG/MSFT/META on AI risks — the known-rich specimen surfaced its
  genuine disclosure; V/KO/CAT on crypto — honest below-floor absences with
  zero model calls; HD on tariffs — weak-but-real match cleared the floor)
  plus a live refusal check (6 names, zero tokens). Evidence recorded at the
  constants in compare.py. Open knob: `PASSAGES_PER_COMPANY_COMPARE` 2→3 if
  column omissions bite (k=8 diff found on-topic above-floor passages at
  ranks 3+).
- **ADR-0014 lazy summarisation landed** on branch
  `claude/lazy-filing-summarisation-413bgp` (7a20069 implementation + test
  updates + this doc-sync), PR-to-main pending. Open follow-up (separate
  commit): `repair.py` still re-summarises damaged sections inline — permitted
  by ADR-0014 §6 (invalidate-or-re-summarise both allowed), but switching it to
  invalidate-to-NULL would leave `GET summary` as the single compute surface.
- **ADR-0016 valuation-snapshot build pending** (Accepted 2026-07-08, docs-only
  on main): the increment-1 module is unbuilt. Next session builds it
  mocked-build-then-review-then-apply — new `fmp_api_key` config + a narrow FMP
  client with one named live-swap point, the `Dated`/`Fetched` provenance types,
  and the identity-only / FY-primary contracts of ADR-0016. `scratch/fmp_schema_probe.py`
  (throwaway) + `tests/fixtures/fmp/*_AAPL.json` are the schema-capture evidence;
  live-validate the sparse-data absence path on a data-poor ticker (ADR-0016 M2).
- **Push discipline:** commits may accumulate locally; check
  `git log origin/main..HEAD` before assuming origin is current.
- **Frontend committed and pushed** (f21bc30 / e4acb4f / 12bc1a3): builds clean
  under `tsc` strict (`cd apps/web && npm run build`); no eslint config, the
  strict-TS compile is the gate. Follow-ups if pursued: env-configurable API
  base is present (`VITE_API_BASE_URL`) but there is no production build/deploy
  story yet, and no frontend tests.
- **FIND cross-encoder rerank landed** (ADR-0013, opt-in): local
  `cross-encoder/ms-marco-MiniLM-L-6-v2` reranks FIND candidates by
  query-relevance, fixing bi-encoder off-topic-best-chunk noise (validated:
  SCHW/cyber, ORCL/AI demoted). OFF BY DEFAULT / opt-in per request
  (`?rerank=true`) — it costs ~3 s/query on CPU, so FIND's cheap path stays
  ~270 ms; the `[rerank]` extra (`sentence-transformers`) is optional and the
  suite runs torch-free without it (opting in without it raises a clear error).
- **Backlog:** S&P 500 breadth run (deferred by choice, ADR-0010/0011);
  incorporation-by-reference extractor for the 16 missing filers; content
  dedupe for FIND; semantic-support check (ADR-0007's deferred enforcement);
  daily briefing (ADR-0011 is its substrate); judgment layer (own ADR, behind
  the wall).

## Stack

- Python 3.12 monorepo; FastAPI; Pydantic; SQLAlchemy 2.0 + psycopg3;
  Postgres 16 + pgvector with HNSW index; Alembic migrations written as
  explicit raw SQL; mypy strict; ruff.
- Embeddings: Voyage `voyage-context-4` (contextualized chunks, 1024-dim);
  documents embed with input type `document`, queries with `query` — same
  space, asymmetric input types (ADR-0006). Model + dimension recorded per
  chunk row.
- SEC EDGAR is the primary source (fair-access: 10 req/s, declared
  User-Agent). `sec_ticker_reference` caches the SEC ticker→CIK→name mapping;
  identity is never hand-typed (ADR-0010).
- Summarization/QA via the Anthropic API behind narrow protocols
  (`Summarizer`, answerers); tests use deterministic fakes.
- HTTP: `httpx2` everywhere EXCEPT anthropic-SDK exception handling, which
  needs real `httpx` (see `import httpx2 as httpx` in batch.py).
- Frontend: Vite + React 19 + TypeScript SPA in `apps/web` (committed),
  `tsc` strict, no UI kit. ADR-0002 originally chose Next.js and was amended
  2026-07-07 to record the Vite reality (the SSR rationale never materialised).

## Deeper knowledge

- `docs/decisions/` — ADRs: *why* choices were made. Read before touching
  affected code; ADR-0012 includes a Compliance and Validation section that
  governs the COMPARE build.
- `docs/domain/` — investment terminology and the curated ticker list. Don't
  guess domain concepts.
- `.claude/skills/` — adr-workflow, wall-guard, commit-discipline,
  live-validation, filing-analysis.

## Compact instructions

When compacting, preserve:

1. **API/schema changes** — what changed and why.
2. **Modified files** — full paths of every file touched this session.
3. **Error → solution pairs** — errors hit and how they were resolved.
4. **In-progress work** — anything uncommitted or unfinished.
