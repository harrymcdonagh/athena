from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from apps.api.research.router import router as research_router

app = FastAPI(title="Athena API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],  # Vite dev frontend (apps/web)
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)
app.include_router(research_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
