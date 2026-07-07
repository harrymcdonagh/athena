# 0002 — Technology Stack

**Status:** Accepted (frontend framework amended 2026-07-07 — see Amendment)

## Context

Athena needs a backend API, a frontend UI, and a data store that handles both
structured relational data (companies, filings, theses) and semantic/vector search
(embedding-based similarity, evidence retrieval).

## Decision

- **Backend:** Python 3.12 + FastAPI. Strong data-science ecosystem, type-safe with
  mypy, async-capable for future streaming endpoints.
- **Frontend:** Next.js + React + TypeScript. Industry-standard, strong component
  ecosystem, server-side rendering for future data-heavy pages.
  **Frontend framework amended 2026-07-07 — the build is Vite + React + TS,
  not Next.js; see Amendment below.**
- **Database:** PostgreSQL + pgvector. One database handles relational integrity and
  vector search. Avoids running a separate vector store (Pinecone, Weaviate, etc.)
  alongside Postgres.

## Consequences

- Single database simplifies ops and keeps evidence joins trivially cheap.
- pgvector is mature enough for personal-scale workloads; dedicated vector DBs can
  be evaluated if retrieval quality demands it.
- Python's typing discipline (mypy strict) catches data-shape errors before runtime.

## Amendment (2026-07-07) — Frontend is Vite + React + TypeScript, not Next.js

The frontend actually built in `apps/web` is a **Vite + React + TypeScript**
single-page app, not Next.js. React and TypeScript are unchanged from the
original decision; only the framework/build tooling differs.

**Why:** Athena's frontend is a single-user, client-side research terminal that
talks to the separate FastAPI backend over HTTP (cross-origin, hence the
CORS middleware in `apps/api/main.py`). The original rationale for Next.js —
"server-side rendering for future data-heavy pages" — did not materialise: the
UI renders entirely from cited API responses, so a static SPA with a fast dev
server is the simpler fit. No SSR, routing, or server-component needs emerged.

This records a decision already taken in the build; it does not reopen the
choice. Database (Postgres + pgvector) and backend (FastAPI) are unaffected.
