# Athena

A personal Investment Research Operating System — a research and analysis tool for organising investment ideas, tracking thesis development, and running custom screens. **It never places trades.**

## Stack

- **API:** FastAPI (Python 3.12+)
- **Web:** Vite + React + TypeScript (`apps/web`; ADR-0002 amended — see `docs/decisions/0002-technology-stack.md`)

## Run the API

```bash
# Install dependencies (from repo root)
pip install -e ".[dev]"

# Start the development server
uvicorn apps.api.main:app --reload
# → http://localhost:8000
# → http://localhost:8000/docs
```

## Run the web frontend

```bash
cd apps/web
npm install
npm run dev        # → http://localhost:5173 (needs the API running + its CORS)
```

See `apps/web/README.md` for the views and API base URL config.

## Run tests

```bash
pytest
```

## Lint & format

```bash
ruff check .
ruff format .
```
