# 0004 — Durable Knowledge Lives in Git and Postgres, Not a Memory MCP Server

**Status:** Accepted

## Context

LLM-native tooling increasingly offers "memory" MCP servers that persist facts
across sessions in an opaque store (e.g. key-value DBs, embeddings in a managed
service). For Athena, durable project knowledge and thesis evolution are core
artefacts, not ephemeral context.

Two risks make memory MCP servers unsuitable here:

1. **Prompt injection / memory poisoning:** an adversarial document retrieved
   during research could write false beliefs into a persistent memory store,
   silently corrupting future reasoning sessions.
2. **Auditability:** opaque memory stores have no git history, no diff, and no
   way to verify what was written or when.

## Decision

- **Project knowledge** (architecture, domain concepts, decisions) lives as
  git-versioned markdown in `docs/`.
- **Thesis evolution** is recorded in an append-only Postgres table (each entry
  timestamped and immutable; no in-place updates).
- Memory-store MCP servers are deliberately excluded from the Athena toolchain.

## Consequences

- Every knowledge update is a git commit — reviewable, diffable, revertable.
- Thesis history is a queryable audit log; nothing is silently overwritten.
- Claude's in-session context must be populated explicitly (via `@docs/` mentions);
  there is no ambient background memory. This is a feature, not a limitation.
- If a well-audited memory tool emerges that solves the poisoning and provenance
  problems, the decision can be revisited.
