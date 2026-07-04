# 0005 — First Slice: SEC EDGAR Ingestion, LLM Summarization, Postgres Research Schema

**Status:** Accepted

## Context

CLAUDE.md forbids database code and financial-API integrations without a written
plan in `docs/decisions/`. Athena's first feature — fetch a company's latest 10-K,
summarize its thesis-relevant sections, persist the result with full source
traceability — requires both. This ADR is that plan; the detailed design is in
`docs/superpowers/specs/2026-07-04-first-research-slice-design.md`.

## Decision

1. **Financial data source:** SEC EDGAR's free, unauthenticated REST APIs
   (`company_tickers.json` for ticker→CIK, the submissions API for filing metadata,
   the archives for documents), called directly from the backend via httpx with a
   declared `User-Agent`, per SEC fair-access policy. No MCP dependency: MCP servers
   are Claude-session tools and cannot be invoked by the running FastAPI app.

2. **Summarization:** the backend calls the Anthropic API behind a narrow
   `Summarizer` protocol (production: claude-sonnet-5; tests: deterministic fake).
   The prompt requires exact figures with inline source URLs and forbids buy/sell
   or recommendation language. Athena summarizes and cites; it never advises.

3. **Database:** PostgreSQL 16 + pgvector (extension enabled now, embeddings later),
   run locally via Docker Compose. Schema migrations are Alembic revisions written
   as explicit raw SQL so every schema change is a readable diff (ADR-0003). Access
   from Python uses SQLAlchemy 2.0 with the psycopg3 driver.

4. **Schema (four tables):** `companies`, `filings`, `filing_summaries`, and an
   append-only `thesis_snapshots` (immutability enforced by a database trigger,
   per ADR-0004). Every summary row stores its source filing URL and the exact
   extracted section text it was derived from; every filing row stores a SHA-256
   of the fetched document. Auditability is a hard requirement: any stored claim
   is verifiable with `psql` alone.

## Consequences

- Athena gains its first runtime dependencies on external services (SEC EDGAR,
  Anthropic API) and on local infrastructure (Postgres via Docker).
- `ANTHROPIC_API_KEY` and `DATABASE_URL` live in the environment (`.env`,
  git-ignored); `.env.example` documents names only. No secrets are committed.
- Thesis history becomes a queryable, immutable audit log from day one.
- Later slices (embeddings, more filing types, watchlists) extend this schema
  rather than replace it; pgvector is already enabled.
