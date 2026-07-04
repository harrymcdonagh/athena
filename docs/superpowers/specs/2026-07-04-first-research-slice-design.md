# Design: First Research Slice — 10-K Ingestion, Summarization, Persistence

**Date:** 2026-07-04
**Status:** Approved (brainstormed and validated section-by-section)

## Goal

The thinnest end-to-end slice that delivers research value for one company: given a
ticker, fetch the latest 10-K from SEC EDGAR, summarize the thesis-relevant sections
(business, risk factors, MD&A) with exact figures, and persist filing, summaries, and
an initial thesis snapshot — every stored claim traceable to its source filing URL.

Athena never makes buy/sell recommendations. It summarizes and cites.

## Decisions Made

| Question | Decision |
|---|---|
| EDGAR access | Backend calls SEC EDGAR's free REST APIs directly via httpx (declared User-Agent). No MCP dependency — MCP servers are Claude-session tools and cannot be called by the running app. |
| Summarization | Backend calls the Anthropic API (claude-sonnet-5) behind a narrow `Summarizer` protocol; tests use a deterministic fake. `ANTHROPIC_API_KEY` via env, never committed. |
| DB tooling | Alembic migrations written as explicit raw SQL (auditable diffs per ADR-0003); SQLAlchemy 2.0 + psycopg3 for access. |
| Local DB | Docker Compose, `pgvector/pgvector:pg16` image. |
| Entry point | Synchronous `POST /research/{ticker}` (30–90 s is acceptable for a single-user tool). |

## Schema (migration 0001)

```sql
CREATE EXTENSION IF NOT EXISTS vector;   -- pgvector enabled; no embedding columns yet

CREATE TABLE companies (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ticker      TEXT NOT NULL UNIQUE,
    cik         TEXT NOT NULL UNIQUE,      -- 10-digit zero-padded; TEXT because leading zeros matter
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE filings (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id        BIGINT NOT NULL REFERENCES companies(id),
    accession_number  TEXT NOT NULL UNIQUE,    -- EDGAR's natural key
    form_type         TEXT NOT NULL,           -- '10-K' only in this slice
    filing_date       DATE NOT NULL,
    period_end_date   DATE,
    filing_url        TEXT NOT NULL,           -- exact EDGAR document URL (audit anchor)
    content_sha256    TEXT NOT NULL,           -- hash of fetched document: proves what was summarized
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE filing_summaries (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    filing_id    BIGINT NOT NULL REFERENCES filings(id),
    section      TEXT NOT NULL CHECK (section IN ('business', 'risk_factors', 'mdna')),
    summary      TEXT NOT NULL,     -- markdown, exact figures preserved
    source_text  TEXT NOT NULL,     -- extracted section text that was summarized (full audit trail)
    source_url   TEXT NOT NULL,     -- filing document URL this summary cites
    model        TEXT NOT NULL,     -- which Claude model produced it
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (filing_id, section)
);

CREATE TABLE thesis_snapshots (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id        BIGINT NOT NULL REFERENCES companies(id),
    content           TEXT NOT NULL,   -- synthesis of the three summaries, citations inline
    source_filing_id  BIGINT NOT NULL REFERENCES filings(id),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ADR-0004: thesis history is immutable
CREATE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS
$$ BEGIN RAISE EXCEPTION 'thesis_snapshots is append-only'; END $$;

CREATE TRIGGER thesis_snapshots_append_only
    BEFORE UPDATE OR DELETE ON thesis_snapshots
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
```

Schema notes:

- Every summary row carries both `source_url` and the exact `source_text` it was
  derived from; the filing row carries `content_sha256` of the fetched document.
  Any claim is verifiable with `psql` alone (ADR-0003).
- Append-only is enforced in the database via trigger, not convention (ADR-0004).
- `source_filing_id` is a plain FK; when theses later draw on multiple sources it
  becomes a join table. Not needed in slice one.
- `source_text` can be hundreds of KB for risk factors/MD&A; TOAST handles it at
  personal scale.

## Components

```
apps/api/
  main.py                  app factory; mounts research router (existing /health untouched)
  config.py                pydantic-settings: DATABASE_URL, ANTHROPIC_API_KEY, SEC_EDGAR_USER_AGENT
  db.py                    SQLAlchemy engine + session dependency
  edgar/
    client.py              EdgarClient (httpx): ticker→CIK via company_tickers.json,
                           latest 10-K via submissions API, fetch primary document
    sections.py            extract Item 1 / 1A / 7 text from 10-K HTML
  research/
    summarizer.py          Summarizer protocol + ClaudeSummarizer
    service.py             pipeline orchestration (domain logic under test)
    repository.py          persistence: upsert company, insert filing/summaries/snapshot
    router.py              POST /research/{ticker}; GET /companies/{ticker}/summary
  migrations/              Alembic; migration 0001 is the raw SQL above
docker-compose.yml         pgvector/pgvector:pg16
.env.example               variable names only, no values
docs/decisions/0005-first-slice-edgar-ingestion-and-postgres-schema.md
                           ADR covering DB + financial-API plan (CLAUDE.md gate)
```

## Data Flow

`POST /research/{ticker}`:

1. Resolve ticker → CIK against EDGAR `company_tickers.json`.
2. Latest 10-K metadata from the EDGAR submissions API (form type exactly
   `10-K`; amendments (`10-K/A`) are out of scope for this slice).
3. Fetch the primary filing document; compute `content_sha256`.
4. Extract Item 1 (business), Item 1A (risk factors), Item 7 (MD&A).
5. `ClaudeSummarizer` produces one summary per section. Prompt hard-requires exact
   figures, inline source URL, and forbids buy/sell or recommendation language.
6. Service composes an initial thesis snapshot from the three summaries.
7. Repository persists company, filing, summaries, snapshot in **one transaction**;
   nothing partial on failure.
8. Response returns stored IDs and summaries.

`GET /companies/{ticker}/summary` reads back the latest stored result.

## Error Handling

- Unknown ticker → 404.
- Company has no 10-K → 404 with explanation.
- EDGAR or Anthropic HTTP failure → 502 with upstream detail.
- Section extraction failure → the run fails loudly; no filing stored with silent
  gaps. Partial data is worse than a clear error in a one-company slice.
- Re-run with no new filing → 409 with the existing filing's ID (accession-number
  uniqueness makes this natural).

## Testing

- **Domain logic (thorough):** repository tests against dockerized Postgres —
  storage round-trips, `UNIQUE(filing_id, section)`, append-only trigger raising.
  Section extraction against fixture 10-K HTML. Service tests with fake
  EdgarClient + fake Summarizer verifying orchestration and transactionality.
- **Glue (light):** EdgarClient URL construction and JSON parsing against canned
  fixtures; ClaudeSummarizer request shape. No live network in tests.

## Out of Scope

Frontend, watchlist, scoring, news, embeddings, multiple filing types, multiple
companies per run, async jobs.
