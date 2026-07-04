from fastapi import FastAPI

app = FastAPI(title="Athena API")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
