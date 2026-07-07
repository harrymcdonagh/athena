# Athena Web

Terminal-style research frontend for the Athena API. React + Vite + TypeScript,
no UI kit. It presents cited evidence from stored SEC filings — it never ranks
or judges companies; every "match" number is a retrieval fact about how well
filing text matched a query.

## Run

```bash
cd apps/web
npm install
npm run dev        # → http://localhost:5173
```

The backend must be running (`uvicorn apps.api.main:app --reload`) and must
allow the dev origin via CORS (see below).

## API base URL

Defaults to `http://localhost:8000`. Override with an env var:

```bash
# apps/web/.env.local
VITE_API_BASE_URL=http://localhost:8000
```

## Views

- `[1] find` — `GET /research/find?q=…` — which companies' filings discuss a
  topic, with cited passages. Ordered by query relevance, never by company merit.
- `[2] research` — `POST /research/qa` — grounded Q&A over stored filings, every
  claim cited. The "compare across years" toggle sends `compare=true` and renders
  the period comparison side by side.
- `[3] passages` — `GET /research/search?q=…&ticker=…` — raw semantic search
  over filing chunks.

Keyboard: `1` / `2` / `3` switch tabs.

## CORS (backend prerequisite, separate change)

The browser calls the API cross-origin, so FastAPI needs CORSMiddleware allowing
the Vite dev origin (`http://localhost:5173`). This is a small backend change,
kept out of this app — see `apps/api/main.py`.
