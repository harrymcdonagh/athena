from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from apps.api.config import Settings
from apps.api.edgar.client import CompanyRef, TickerNotFoundError
from apps.api.main import app
from apps.api.research.embeddings import EmbeddedChunk, EmbeddingError
from apps.api.research.qa import Claim, QaAnswer, QaAnswerer
from apps.api.research.repository import Repository
from apps.api.research.router import (
    QaResponse,
    ResearchResponse,
    get_embedder,
    get_qa_answerer,
    get_read_engine,
    get_research_service,
)
from apps.api.research.service import ResearchService
from apps.api.tests.test_embeddings import (
    QueryOnlyEmbedder,
    axis_vector,
    seed_filing,
    seed_second_company_filing,
)
from apps.api.tests.test_qa import TWO_SIDED, AnthropicErrorAnswerer, FakeQaAnswerer
from apps.api.tests.test_service import EIGHT_K, FILING, PRIOR_FILING, FakeEdgar, FakeSummarizer


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
    assert body["status"] == "ingested"
    assert body["accession_number"] == "0000320193-25-000123"
    assert set(body["summaries"]) == {"business", "risk_factors", "mdna"}
    assert body["filing_url"].startswith("https://www.sec.gov/")


def test_post_research_twice_returns_200_skipped(client: TestClient) -> None:
    first = client.post("/research/AAPL")
    assert first.status_code == 200

    second = client.post("/research/AAPL")

    assert second.status_code == 200
    body = second.json()
    assert body["status"] == "skipped"
    assert body["filing_id"] == first.json()["filing_id"]
    assert body["summaries"] == {}
    assert body["thesis_snapshot_id"] is None


def test_post_research_with_accession_param_ingests_that_filing(
    client: TestClient, db: Engine
) -> None:
    edgar = FakeEdgar(filings=[FILING, PRIOR_FILING])
    service = ResearchService(edgar=edgar, summarizer=FakeSummarizer(), engine=db)
    app.dependency_overrides[get_research_service] = lambda: service

    assert client.post("/research/AAPL").status_code == 200
    response = client.post(
        "/research/AAPL", params={"accession_number": PRIOR_FILING.accession_number}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ingested"
    assert body["accession_number"] == PRIOR_FILING.accession_number

    # ADR-0008 §4: the backfilled prior year must not hijack "latest research".
    summary = client.get("/companies/AAPL/summary")
    assert summary.status_code == 200
    assert summary.json()["accession_number"] == FILING.accession_number


def test_post_research_unknown_accession_returns_404(client: TestClient) -> None:
    response = client.post("/research/AAPL", params={"accession_number": "0000320193-99-999999"})
    assert response.status_code == 404


def test_post_research_non_10k_accession_returns_422(client: TestClient, db: Engine) -> None:
    edgar = FakeEdgar(filings=[EIGHT_K, FILING])
    service = ResearchService(edgar=edgar, summarizer=FakeSummarizer(), engine=db)
    app.dependency_overrides[get_research_service] = lambda: service
    response = client.post("/research/AAPL", params={"accession_number": EIGHT_K.accession_number})
    assert response.status_code == 422
    assert "8-K" in response.json()["detail"]


def test_research_response_shape(client: TestClient) -> None:
    body = client.post("/research/AAPL").json()
    assert set(body) == set(ResearchResponse.model_fields)


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


# --- POST /research/qa ---

DIRECT = QaAnswer(
    mode="direct",
    claims=[Claim(text="Revenue grew, led by services.", chunk_ids=["C1"])],
)


class ExplodingEmbedder:
    """Embedder whose query embedding fails, as when Voyage is unreachable."""

    model = "voyage-context-4"
    dimension = 1024

    def embed_document(self, text: str) -> list[EmbeddedChunk]:
        raise NotImplementedError

    def embed_query(self, text: str) -> list[float]:
        raise EmbeddingError("voyage: connection failed")


@pytest.fixture
def qa_client(db: Engine) -> Iterator[TestClient]:
    app.dependency_overrides[get_read_engine] = lambda: db
    app.dependency_overrides[get_embedder] = lambda: QueryOnlyEmbedder(axis_vector(0))
    yield TestClient(app)
    app.dependency_overrides.clear()


def use_answerer(answerer: QaAnswerer) -> None:
    app.dependency_overrides[get_qa_answerer] = lambda: answerer


def test_qa_direct_question_returns_cited_answer_with_source_urls(
    qa_client: TestClient, db: Engine
) -> None:
    seed_search_corpus(db)
    use_answerer(FakeQaAnswerer(DIRECT))

    response = qa_client.post("/research/qa", json={"question": "what drives revenue growth?"})

    assert response.status_code == 200
    body = response.json()
    assert body["answer"]["mode"] == "direct"
    assert body["answer"]["claims"][0]["chunk_ids"] == ["C1"]
    assert body["citations"]
    for citation in body["citations"].values():
        assert set(citation) == RESULT_FIELDS
        assert citation["source_url"].startswith("https://www.sec.gov/")
    assert body["warnings"] == []


def test_qa_buy_sell_question_is_two_sided_and_cannot_carry_a_verdict(
    qa_client: TestClient, db: Engine
) -> None:
    seed_search_corpus(db)
    use_answerer(FakeQaAnswerer(TWO_SIDED))

    response = qa_client.post(
        "/research/qa", json={"question": "Should I buy AAPL?", "ticker": "AAPL"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["answer"]["mode"] == "two_sided"
    assert body["answer"]["bull"] and body["answer"]["bear"]
    assert "verdict is yours" in body["answer"]["verdict_note"]
    # A recommendation is structurally unrepresentable across the HTTP boundary:
    # the response models have no field that could carry one.
    forbidden = {"recommendation", "rating", "action", "price_target", "verdict"}
    assert set(body["answer"]) == set(QaAnswer.model_fields)
    assert forbidden.isdisjoint(QaAnswer.model_fields)
    assert set(body) == set(QaResponse.model_fields) == {"answer", "citations", "warnings"}
    assert forbidden.isdisjoint(QaResponse.model_fields)


def test_qa_verification_warnings_survive_to_http_response(
    qa_client: TestClient, db: Engine
) -> None:
    seed_search_corpus(db)
    fabricated = QaAnswer(
        mode="direct",
        claims=[Claim(text="Claim resting on a fabricated citation.", chunk_ids=["C99"])],
    )
    use_answerer(FakeQaAnswerer(fabricated))

    response = qa_client.post("/research/qa", json={"question": "what drives revenue?"})

    assert response.status_code == 200
    body = response.json()
    assert [w["kind"] for w in body["warnings"]] == ["unknown_citation"]
    assert "C99" in body["warnings"][0]["message"]


def test_qa_reasoning_artifact_warning_survives_to_http_response(
    qa_client: TestClient, db: Engine
) -> None:
    seed_search_corpus(db)
    leaky = QaAnswer(
        mode="direct",
        claims=[Claim(text="Revenue grew, led by services.", chunk_ids=["C1"])],
        explanation="Scope is limited.<br>No change needed.",
    )
    use_answerer(FakeQaAnswerer(leaky))

    response = qa_client.post("/research/qa", json={"question": "what drives revenue?"})

    assert response.status_code == 200
    body = response.json()
    assert {w["kind"] for w in body["warnings"]} == {"reasoning_artifact"}
    # Flagged, not stripped: the leaky text crosses the boundary intact.
    assert body["answer"]["explanation"] == "Scope is limited.<br>No change needed."


def test_qa_blank_question_returns_422(qa_client: TestClient) -> None:
    use_answerer(FakeQaAnswerer(DIRECT))
    response = qa_client.post("/research/qa", json={"question": "   "})
    assert response.status_code == 422
    assert "blank" in response.json()["detail"]


def test_qa_invalid_section_returns_422_listing_allowed_values(qa_client: TestClient) -> None:
    use_answerer(FakeQaAnswerer(DIRECT))
    response = qa_client.post(
        "/research/qa", json={"question": "what are the risks?", "section": "financials"}
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    for allowed in ("business", "risk_factors", "mdna"):
        assert allowed in detail


def test_qa_unknown_ticker_returns_200_insufficient_evidence(
    qa_client: TestClient, db: Engine
) -> None:
    seed_search_corpus(db)
    answerer = FakeQaAnswerer(DIRECT)
    use_answerer(answerer)

    response = qa_client.post("/research/qa", json={"question": "growth?", "ticker": "ZZZZ"})

    assert response.status_code == 200
    body = response.json()
    assert body["answer"]["mode"] == "insufficient_evidence"
    assert body["citations"] == {}
    assert body["warnings"] == []
    assert answerer.calls == []  # empty retrieval short-circuits before any Claude call


def test_qa_anthropic_failure_returns_502(qa_client: TestClient, db: Engine) -> None:
    seed_search_corpus(db)
    use_answerer(AnthropicErrorAnswerer())
    response = qa_client.post("/research/qa", json={"question": "what drives revenue?"})
    assert response.status_code == 502
    assert "anthropic" in response.json()["detail"]


def test_qa_embedding_failure_returns_502(qa_client: TestClient, db: Engine) -> None:
    seed_search_corpus(db)
    use_answerer(FakeQaAnswerer(DIRECT))
    app.dependency_overrides[get_embedder] = lambda: ExplodingEmbedder()
    response = qa_client.post("/research/qa", json={"question": "what drives revenue?"})
    assert response.status_code == 502
    assert "voyage" in response.json()["detail"]


def test_qa_missing_anthropic_key_returns_503(
    qa_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "apps.api.research.router.get_settings",
        lambda: Settings(anthropic_api_key="", voyage_api_key="present"),
    )
    get_qa_answerer.cache_clear()
    try:
        response = qa_client.post("/research/qa", json={"question": "what drives revenue?"})
    finally:
        get_qa_answerer.cache_clear()
    assert response.status_code == 503
    assert "QA unavailable" in response.json()["detail"]


def test_qa_missing_voyage_key_returns_503(db: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    app.dependency_overrides[get_read_engine] = lambda: db
    monkeypatch.setattr(
        "apps.api.research.router.get_settings",
        lambda: Settings(anthropic_api_key="present", voyage_api_key=""),
    )
    get_embedder.cache_clear()
    get_qa_answerer.cache_clear()
    try:
        response = TestClient(app).post("/research/qa", json={"question": "what drives revenue?"})
    finally:
        get_embedder.cache_clear()
        get_qa_answerer.cache_clear()
        app.dependency_overrides.clear()
    assert response.status_code == 503


def test_qa_clamps_limit_to_max(qa_client: TestClient, db: Engine) -> None:
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
    use_answerer(FakeQaAnswerer(DIRECT))
    response = qa_client.post("/research/qa", json={"question": "everything", "limit": 100})
    assert response.status_code == 200
    assert len(response.json()["citations"]) == 25
