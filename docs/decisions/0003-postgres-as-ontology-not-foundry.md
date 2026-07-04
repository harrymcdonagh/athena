# 0003 — Model the Investment Domain in Postgres, Not Palantir Foundry

**Status:** Accepted

## Context

Athena needs a structured representation of the investment domain: companies,
filings, theses, evidence, relationships. Palantir Foundry is a purpose-built
ontology and data platform used by institutional investors. It offers rich entity
modelling and lineage tracking, but it is a managed, opaque, expensive platform
designed for large teams.

Athena's core requirement is **transparent, auditable, evidence-linked reasoning**.
Every claim must be traceable to a source. That traceability must be inspectable
with standard tools — SQL, git log, plain text — not locked behind a proprietary
query layer.

## Decision

Model the investment domain as a well-designed PostgreSQL schema with pgvector.
Entities, relationships, and evidence links are plain tables. Schema migrations are
version-controlled. Foundry is not adopted.

## Consequences

- Schema is fully auditable and portable — no vendor lock-in.
- Evidence chains are SQL joins, readable by anyone with `psql`.
- The schema must be designed thoughtfully upfront; Foundry's drag-and-drop
  ontology tooling is not available as a crutch.
- If Athena ever scales beyond a solo tool and Foundry's institutional integrations
  become valuable, the decision can be revisited; the Postgres schema provides a
  clean migration target.
