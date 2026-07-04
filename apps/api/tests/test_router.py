from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from apps.api.edgar.client import CompanyRef, TickerNotFoundError
from apps.api.main import app
from apps.api.research.router import get_read_engine, get_research_service
from apps.api.research.service import ResearchService
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
