from fastapi import FastAPI

from apps.api.research.router import router as research_router

app = FastAPI(title="Athena API")
app.include_router(research_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
