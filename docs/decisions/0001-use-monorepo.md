# 0001 — Use a Monorepo

**Status:** Accepted

## Context

Athena has two primary components: a Python/FastAPI backend and a Next.js frontend.
As a solo project, the overhead of coordinating changes across multiple repositories
(separate git histories, separate CI, separate dependency management) adds friction
with no meaningful benefit.

## Decision

All Athena code lives in a single monorepo managed with one `pyproject.toml` at the
root for Python tooling. Frontend and backend share one git history and one pull
request workflow.

## Consequences

- Cross-cutting changes (e.g. API contract + UI update) ship in a single commit.
- A single `.claude/` config governs the whole project.
- If the project ever grows to a team, repo split can be revisited; for a solo tool
  it is unnecessary complexity.
