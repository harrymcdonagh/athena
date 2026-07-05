from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from apps.api.edgar.client import CompanyRef, TickerNotFoundError
from apps.api.main import app
from apps.api.research.embeddings import EmbeddedChunk
from apps.api.research.repository import Repository
from apps.api.research.router import get_embedder, get_read_engine, get_research_service
from apps.api.research.service import ResearchService
from apps.api.tests.test_embeddings import (
    QueryOnlyEmbedder,
    axis_vector,
    seed_filing,
    seed_second_company_filing,
)
from apps.api.tests.test_service import FakeEdgar, FakeSummarizer


class UnknownTickerEdgar(FakeEdgar):
    def resolve_ticker(self, ticker: str) -> CompanyRef:
        raise TickerNotFoundError(f"ticker {ticker!r} not found")


@pytest.fixture
def client(db: Engine) -> Iterator[TestClient]:
    service = ResearchService(edgar=FakeEdgar(), summarizer=FakeSummarizer(), engine=db)
    app.dependency_overrides[get_research_service] = lambda: service
    app.dependency_overrides[get_read_engine] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_post_research_returns_summaries_and_ids(client: TestClient) -> None:
    response = client.post("/research/AAPL")
    assert response.status_code == 200
    body = response.json()
    assert body["ticker"] == "AAPL"
    assert body["accession_number"] == "0000320193-25-000123"
    assert set(body["summaries"]) == {"business", "risk_factors", "mdna"}
    assert body["filing_url"].startswith("https://www.sec.gov/")


def test_post_research_twice_returns_409(client: TestClient) -> None:
    assert client.post("/research/AAPL").status_code == 200
    response = client.post("/research/AAPL")
    assert response.status_code == 409
    assert "already ingested" in response.json()["detail"]


def test_post_research_unknown_ticker_returns_404(client: TestClient, db: Engine) -> None:
    service = ResearchService(edgar=UnknownTickerEdgar(), summarizer=FakeSummarizer(), engine=db)
    app.dependency_overrides[get_research_service] = lambda: service
    assert client.post("/research/ZZZZ").status_code == 404


def test_get_summary_after_research(client: TestClient) -> None:
    client.post("/research/AAPL")
    response = client.get("/companies/AAPL/summary")
    assert response.status_code == 200
    body = response.json()
    assert body["company_name"] == "Apple Inc."
    assert "thesis" in body
    assert set(body["summaries"]) == {"business", "risk_factors", "mdna"}


def test_get_summary_unknown_ticker_returns_404(client: TestClient) -> None:
    assert client.get("/companies/ZZZZ/summary").status_code == 404


# --- GET /research/search ---

RESULT_FIELDS = {
    "content",
    "source_url",
    "ticker",
    "section",
    "filing_id",
    "chunk_index",
    "distance",
}


@pytest.fixture
def search_client(db: Engine) -> Iterator[TestClient]:
    app.dependency_overrides[get_embedder] = lambda: QueryOnlyEmbedder(axis_vector(0))
    app.dependency_overrides[get_read_engine] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


def seed_search_corpus(db: Engine) -> None:
    """Two companies, two sections, distinguishable axis embeddings.

    The search_client's fake embedder maps every query to axis 0, so
    'apple revenue grew' (axis 0) is always the nearest chunk.
    """
    aapl_filing = seed_filing(db)
    msft_filing = seed_second_company_filing(db)
    with db.begin() as conn:
        repo = Repository(conn)
        repo.replace_chunks(
            aapl_filing,
            "mdna",
            "https://www.sec.gov/aapl-10k.htm#mdna",
            [EmbeddedChunk(text="apple revenue grew", embedding=axis_vector(0))],
            model="voyage-context-4",
            dimension=1024,
        )
        repo.replace_chunks(
            aapl_filing,
            "risk_factors",
            "https://www.sec.gov/aapl-10k.htm#risk",
            [EmbeddedChunk(text="apple supply chain risk", embedding=axis_vector(1))],
            model="voyage-context-4",
            dimension=1024,
        )
        repo.replace_chunks(
            msft_filing,
            "mdna",
            "https://www.sec.gov/msft-10k.htm#mdna",
            [EmbeddedChunk(text="microsoft cloud growth", embedding=axis_vector(2))],
            model="voyage-context-4",
            dimension=1024,
        )


def test_search_returns_results_each_with_source_url(search_client: TestClient, db: Engine) -> None:
    seed_search_corpus(db)
    response = search_client.get("/research/search", params={"q": "revenue growth"})
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 3
    for result in body:
        assert set(result) == RESULT_FIELDS
        assert result["source_url"].startswith("https://www.sec.gov/")
    assert body[0]["content"] == "apple revenue grew"
    assert body[0]["distance"] == pytest.approx(0.0, abs=1e-6)


def test_search_ticker_filter_constrains_results(search_client: TestClient, db: Engine) -> None:
    seed_search_corpus(db)
    response = search_client.get("/research/search", params={"q": "growth", "ticker": "MSFT"})
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["ticker"] == "MSFT"
    assert body[0]["content"] == "microsoft cloud growth"


def test_search_section_filter_constrains_results(search_client: TestClient, db: Engine) -> None:
    seed_search_corpus(db)
    response = search_client.get(
        "/research/search", params={"q": "risks", "section": "risk_factors"}
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["section"] == "risk_factors"
    assert body[0]["content"] == "apple supply chain risk"


def test_search_blank_query_returns_422(search_client: TestClient) -> None:
    response = search_client.get("/research/search", params={"q": "   "})
    assert response.status_code == 422
    assert "blank" in response.json()["detail"]


def test_search_invalid_section_returns_422_listing_allowed_values(
    search_client: TestClient,
) -> None:
    response = search_client.get(
        "/research/search", params={"q": "growth", "section": "financials"}
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    for allowed in ("business", "risk_factors", "mdna"):
        assert allowed in detail


def test_search_unknown_ticker_returns_200_with_empty_list(
    search_client: TestClient, db: Engine
) -> None:
    seed_search_corpus(db)
    response = search_client.get("/research/search", params={"q": "growth", "ticker": "ZZZZ"})
    assert response.status_code == 200
    assert response.json() == []


def test_search_clamps_limit_to_max(search_client: TestClient, db: Engine) -> None:
    filing_id = seed_filing(db)
    chunks = [EmbeddedChunk(text=f"chunk {i}", embedding=axis_vector(i)) for i in range(30)]
    with db.begin() as conn:
        Repository(conn).replace_chunks(
            filing_id,
            "mdna",
            "https://www.sec.gov/aapl-10k.htm#mdna",
            chunks,
            model="voyage-context-4",
            dimension=1024,
        )
    response = search_client.get("/research/search", params={"q": "everything", "limit": 100})
    assert response.status_code == 200
    assert len(response.json()) == 25
