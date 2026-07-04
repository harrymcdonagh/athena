# 0002 — Technology Stack

**Status:** Accepted

## Context

Athena needs a backend API, a frontend UI, and a data store that handles both
structured relational data (companies, filings, theses) and semantic/vector search
(embedding-based similarity, evidence retrieval).

## Decision

- **Backend:** Python 3.12 + FastAPI. Strong data-science ecosystem, type-safe with
  mypy, async-capable for future streaming endpoints.
- **Frontend:** Next.js + React + TypeScript. Industry-standard, strong component
  ecosystem, server-side rendering for future data-heavy pages.
- **Database:** PostgreSQL + pgvector. One database handles relational integrity and
  vector search. Avoids running a separate vector store (Pinecone, Weaviate, etc.)
  alongside Postgres.

## Consequences

- Single database simplifies ops and keeps evidence joins trivially cheap.
- pgvector is mature enough for personal-scale workloads; dedicated vector DBs can
  be evaluated if retrieval quality demands it.
- Python's typing discipline (mypy strict) catches data-shape errors before runtime.
