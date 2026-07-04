# Athena — Claude Guide

## What is Athena?

Athena is a personal Investment Research Operating System. It retrieves evidence,
organises investment ideas, and explains analytical reasoning to support research.
**It never makes investment decisions or places trades.** All conclusions are the
user's sole responsibility.

## Directory Map

```
apps/api/           FastAPI backend (Python 3.12+)
apps/web/           Next.js frontend (placeholder)
packages/           Shared Python packages (future)
docs/decisions/     Architecture Decision Records (ADRs)
docs/domain/        Domain knowledge and glossaries
docs/architecture/  High-level design notes
.claude/            Claude Code config and hooks
```

## Commands

```bash
source .venv/bin/activate            # activate venv (python3.12)
uvicorn apps.api.main:app --reload   # start API → http://localhost:8000
pytest                                # run all tests
ruff check .                          # lint
ruff format .                         # format
mypy apps/                            # type check
```

## Conventions (Non-Negotiable)

- **Never commit secrets.** No .env files, API keys, or tokens in any tracked file.
- **Verify before claiming done.** Run `ruff check .` and `pytest`; show output.
- **Conventional commits.** Prefixes: `feat:` `fix:` `chore:` `docs:` `refactor:` `test:`
- **Edit existing files over creating new ones.** New file only when strictly necessary.
- **No database or financial API code** without a written plan in `docs/decisions/`.

## Deeper Knowledge

Read relevant docs before making decisions:

- `docs/decisions/` — ADRs: *why* choices were made. Read before touching affected code.
- `docs/domain/` — Investment terminology. Don't guess domain concepts.
- `docs/architecture/` — System design.

## Compact Instructions

When compacting, preserve:

1. **API/schema changes** — what changed and why.
2. **Modified files** — full paths of every file touched this session.
3. **Error → solution pairs** — errors hit and how they were resolved.
4. **In-progress work** — anything uncommitted or unfinished.
