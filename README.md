# Athena

A personal Investment Research Operating System — a research and analysis tool for organising investment ideas, tracking thesis development, and running custom screens. **It never places trades.**

## Stack

- **API:** FastAPI (Python 3.12+)
- **Web:** Next.js (planned)

## Run the API

```bash
# Install dependencies (from repo root)
pip install -e ".[dev]"

# Start the development server
uvicorn apps.api.main:app --reload
# → http://localhost:8000
# → http://localhost:8000/docs
```

## Run tests

```bash
pytest
```

## Lint & format

```bash
ruff check .
ruff format .
```
