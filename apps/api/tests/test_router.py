import inspect
from collections.abc import Iterator

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

import apps.api.research.find as find_module
from apps.api.config import Settings
from apps.api.edgar.client import CompanyRef, TickerNotFoundError
from apps.api.main import app
from apps.api.research.compare import QUALIFYING_SIMILARITY_FLOOR, MockColumnAnswerer
from apps.api.research.embeddings import EmbeddedChunk, EmbeddingError
from apps.api.research.qa import (
    Claim,
    ClaudeColumnAnswerer,
    ComparisonDraft,
    ComparisonResult,
    QaAnswer,
    QaAnswerer,
)
from apps.api.research.repository import Repository
from apps.api.research.router import (
    CompaniesResponse,
    CompanyListItem,
    CompareCoverageResponse,
    CompareEntryResponse,
    ComparePassageResponse,
    ComparePinnedFilingResponse,
    CompareResponse,
    CompareStatementResponse,
    ComparisonResponse,
    FindCompanyMatchResponse,
    FindPassageResponse,
    FindResponse,
    QaResponse,
    ResearchResponse,
    get_column_answerer,
    get_comparison_answerer,
    get_embedder,
    get_qa_answerer,
    get_read_engine,
    get_research_service,
)
from apps.api.research.router import find as find_endpoint
from apps.api.research.service import ResearchService
from apps.api.tests.test_compare import (
    seed_ready_company,
)
from apps.api.tests.test_compare import (
    seed_reference as seed_compare_reference,
)
from apps.api.tests.test_embeddings import (
    QueryOnlyEmbedder,
    axis_vector,
    seed_filing,
    seed_prior_period_filing,
    seed_second_company_filing,
)
from apps.api.tests.test_qa import (
    TWO_SIDED,
    AnthropicErrorAnswerer,
    FakeComparisonAnswerer,
    FakeQaAnswerer,
)
from apps.api.tests.test_service import (
    EIGHT_K,
    FILING,
    FRACTION_TRIP_HTML,
    PRIOR_FILING,
    CountingSummarizer,
    FakeEdgar,
    FakeSummarizer,
    FixedDocumentEdgar,
)


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


def test_post_research_ingests_with_pending_summaries(client: TestClient) -> None:
    # ADR-0014: ingest computes no summaries, so the ingested response carries an
    # empty summaries map — sections are pending until the summary surface is hit.
    response = client.post("/research/AAPL")
    assert response.status_code == 200
    body = response.json()
    assert body["ticker"] == "AAPL"
    assert body["status"] == "ingested"
    assert body["accession_number"] == "0000320193-25-000123"
    assert body["summaries"] == {}
    assert body["thesis_snapshot_id"] is None
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
    assert body["section_warnings"] == []  # the skipped path never audits sections


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


def test_post_research_surfaces_section_plausibility_warnings(
    client: TestClient, db: Engine
) -> None:
    service = ResearchService(
        edgar=FixedDocumentEdgar(FRACTION_TRIP_HTML), summarizer=FakeSummarizer(), engine=db
    )
    app.dependency_overrides[get_research_service] = lambda: service

    body = client.post("/research/AAPL").json()

    assert body["status"] == "ingested"  # flag-not-block: the warning is not an error
    (warning,) = body["section_warnings"]
    assert warning["section"] == "mdna"
    assert warning["checks"] == ["fraction_of_document"]
    assert warning["section_chars"] > 0 and warning["document_chars"] > warning["section_chars"]
    assert 0 < warning["fraction"] < 1


def test_healthy_ingest_has_empty_section_warnings(client: TestClient) -> None:
    body = client.post("/research/AAPL").json()
    assert body["section_warnings"] == []


def test_post_research_unknown_ticker_returns_404(client: TestClient, db: Engine) -> None:
    service = ResearchService(edgar=UnknownTickerEdgar(), summarizer=FakeSummarizer(), engine=db)
    app.dependency_overrides[get_research_service] = lambda: service
    assert client.post("/research/ZZZZ").status_code == 404


def test_get_summary_computes_on_read_then_serves_cached(db: Engine) -> None:
    # ADR-0014 §3: the summary endpoint is the ONLY place summaries compute.
    # First GET computes+caches (one call/section); the second is a cache hit.
    summarizer = CountingSummarizer()
    service = ResearchService(edgar=FakeEdgar(), summarizer=summarizer, engine=db)
    app.dependency_overrides[get_research_service] = lambda: service
    client = TestClient(app)

    client.post("/research/AAPL")  # ingest: pending, zero summarizer calls
    assert summarizer.calls == []

    first = client.get("/companies/AAPL/summary")
    assert first.status_code == 200
    assert set(first.json()["summaries"]) == {"business", "risk_factors", "mdna"}
    assert sorted(summarizer.calls) == ["business", "mdna", "risk_factors"]

    second = client.get("/companies/AAPL/summary")
    assert second.status_code == 200
    assert second.json()["summaries"] == first.json()["summaries"]
    assert len(summarizer.calls) == 3  # cache hit — no recompute


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


# --- GET /research/companies ---


def test_get_companies_lists_ingested_alphabetically_with_aggregates(
    client: TestClient, db: Engine
) -> None:
    # MSFT seeded first to prove ordering is by ticker, not insertion.
    seed_second_company_filing(db)  # MSFT, one filing (period 2025-09-27)
    seed_filing(db)  # AAPL, latest period 2025-09-27
    seed_prior_period_filing(db)  # AAPL, prior period 2024-09-28

    response = client.get("/research/companies")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == set(CompaniesResponse.model_fields)
    aapl, msft = body["companies"]
    for item in (aapl, msft):
        assert set(item) == set(CompanyListItem.model_fields)
    assert aapl == {
        "ticker": "AAPL",
        "company_name": "Apple Inc.",
        "filing_count": 2,
        "latest_period_end_date": "2025-09-27",
        "has_multiple_filings": True,
    }
    assert msft == {
        "ticker": "MSFT",
        "company_name": "Microsoft Corporation",
        "filing_count": 1,
        "latest_period_end_date": "2025-09-27",
        "has_multiple_filings": False,
    }


def test_get_companies_empty_corpus_returns_200_with_empty_list(client: TestClient) -> None:
    response = client.get("/research/companies")
    assert response.status_code == 200
    assert response.json() == {"companies": []}


def test_get_companies_excludes_reference_only_tickers(client: TestClient, db: Engine) -> None:
    """ADR-0010 #2: the picker reflects evidence held (`companies`), never the
    resolvable universe (sec_ticker_reference). A ticker with a reference row
    but no ingested filings must not appear."""
    seed_filing(db)  # AAPL, ingested
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO sec_ticker_reference (ticker, cik, company_name, exchange)"
                " VALUES ('NVDA', '0001045810', 'NVIDIA CORP', 'Nasdaq')"
            )
        )

    response = client.get("/research/companies")

    assert response.status_code == 200
    assert [c["ticker"] for c in response.json()["companies"]] == ["AAPL"]


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


# --- POST /research/qa with compare=true (ADR-0009 §7) ---


def use_comparison_answerer(answerer: FakeComparisonAnswerer) -> None:
    app.dependency_overrides[get_comparison_answerer] = lambda: answerer


def test_qa_compare_off_and_omitted_return_exact_qa_response(
    qa_client: TestClient, db: Engine
) -> None:
    seed_search_corpus(db)
    use_answerer(FakeQaAnswerer(DIRECT))
    for payload in (
        {"question": "what drives revenue?"},
        {"question": "what drives revenue?", "compare": False},
    ):
        response = qa_client.post("/research/qa", json=payload)
        assert response.status_code == 200
        body = response.json()
        assert set(body) == set(QaResponse.model_fields)  # today's ADR-0007 shape exactly
        assert "period_comparison" not in body


def test_qa_compare_resolves_previous_comparable_pair(
    qa_client: TestClient, db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    newer = seed_filing(db)
    older = seed_prior_period_filing(db)
    use_answerer(FakeQaAnswerer(DIRECT))
    use_comparison_answerer(FakeComparisonAnswerer(ComparisonDraft()))
    captured: dict[str, object] = {}

    def fake_detect_changes(
        engine: Engine,
        embedder: object,
        answerer: object,
        question: str,
        filing_ids: list[int],
        limit: int = 8,
    ) -> ComparisonResult:
        captured.update(question=question, filing_ids=list(filing_ids), limit=limit)
        return ComparisonResult(period_comparison=[], citations={}, warnings=[], explanation="stub")

    monkeypatch.setattr("apps.api.research.router.detect_changes", fake_detect_changes)

    response = qa_client.post(
        "/research/qa",
        json={"question": "what changed year over year?", "ticker": "AAPL", "compare": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == set(ComparisonResponse.model_fields)
    # Latest 10-K first, previous COMPARABLE 10-K second (ADR-0008 §3).
    assert captured["filing_ids"] == [newer, older]
    assert captured["question"] == "what changed year over year?"


def test_qa_compare_single_filing_returns_200_no_prior_comparable(
    qa_client: TestClient, db: Engine
) -> None:
    seed_filing(db)  # one 10-K only
    use_answerer(FakeQaAnswerer(DIRECT))
    use_comparison_answerer(FakeComparisonAnswerer(ComparisonDraft()))

    response = qa_client.post(
        "/research/qa", json={"question": "what changed?", "ticker": "AAPL", "compare": True}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["period_comparison"] == []
    assert body["citations"] == {}
    assert "no prior comparable" in body["explanation"].lower()


def test_qa_compare_unknown_ticker_returns_404(qa_client: TestClient) -> None:
    use_answerer(FakeQaAnswerer(DIRECT))
    use_comparison_answerer(FakeComparisonAnswerer(ComparisonDraft()))
    response = qa_client.post(
        "/research/qa", json={"question": "what changed?", "ticker": "ZZZZ", "compare": True}
    )
    assert response.status_code == 404
    assert "no research stored" in response.json()["detail"]


def test_qa_compare_without_ticker_returns_422(qa_client: TestClient) -> None:
    use_answerer(FakeQaAnswerer(DIRECT))
    use_comparison_answerer(FakeComparisonAnswerer(ComparisonDraft()))
    response = qa_client.post("/research/qa", json={"question": "what changed?", "compare": True})
    assert response.status_code == 422
    assert "ticker" in response.json()["detail"]


def test_qa_compare_rejects_section_filter(qa_client: TestClient, db: Engine) -> None:
    seed_filing(db)
    seed_prior_period_filing(db)
    use_answerer(FakeQaAnswerer(DIRECT))
    use_comparison_answerer(FakeComparisonAnswerer(ComparisonDraft()))
    response = qa_client.post(
        "/research/qa",
        json={
            "question": "what changed?",
            "ticker": "AAPL",
            "section": "risk_factors",
            "compare": True,
        },
    )
    assert response.status_code == 422
    assert "section" in response.json()["detail"]


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


# --- GET /research/find (ADR-0011 §1: FIND mode, zero answer-model) ---


def test_find_returns_companies_grouped_with_cited_passages(
    search_client: TestClient, db: Engine
) -> None:
    seed_search_corpus(db)
    response = search_client.get("/research/find", params={"q": "revenue growth"})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == set(FindResponse.model_fields)
    assert body["query"] == "revenue growth"
    # The fake embedder maps every query to axis 0, so AAPL's mdna chunk is the
    # exact match; both companies have >=1 chunk in the wide results.
    assert [m["ticker"] for m in body["matches"]] == ["AAPL", "MSFT"]
    assert body["matches"][0]["company_name"] == "Apple Inc."
    assert body["matches"][0]["match_strength"] == pytest.approx(1.0, abs=1e-6)
    for match in body["matches"]:
        assert set(match) == set(FindCompanyMatchResponse.model_fields)
        assert match["passages"]
        for passage in match["passages"]:
            assert set(passage) == set(FindPassageResponse.model_fields)
            assert passage["source_url"].startswith("https://www.sec.gov/")


def test_find_path_is_structurally_zero_answer_model(search_client: TestClient, db: Engine) -> None:
    """ADR-0011 §1: the zero-token path is the contract, enforced structurally.
    The endpoint declares NO answerer dependency (so FastAPI never resolves
    one — note search_client wires only the engine and embedder, and the
    request still succeeds) and the find module has nothing answerer-shaped
    in its namespace to call. A DI spy override cannot test this — an
    undeclared dependency is never resolved — so the assertion inspects the
    dependency surface itself."""
    seed_search_corpus(db)

    response = search_client.get("/research/find", params={"q": "revenue growth"})

    assert response.status_code == 200
    assert response.json()["matches"]
    # `rerank` (ADR-0013 §5) is a cost/precision toggle, off by default; still
    # no answerer dependency in the endpoint surface.
    assert set(inspect.signature(find_endpoint).parameters) == {"q", "engine", "embedder", "rerank"}
    assert not any("answerer" in name.lower() for name in vars(find_module))
    assert not any("anthropic" in name.lower() for name in vars(find_module))
    # ADR-0013: FIND now imports the reranker; it too must be answerer-free, so
    # the zero-answer-model contract survives the new import edge.
    import apps.api.research.rerank as rerank_module

    assert not any("answerer" in name.lower() for name in vars(rerank_module))
    assert not any("anthropic" in name.lower() for name in vars(rerank_module))


def test_find_default_path_does_not_rerank(search_client: TestClient, db: Engine) -> None:
    """ADR-0013 §5: rerank is OFF by default. A plain FIND request must leave
    every rerank_score null (the cheap cosine path), proving the feature is
    inert unless explicitly opted into."""
    seed_search_corpus(db)
    body = search_client.get("/research/find", params={"q": "revenue growth"}).json()
    assert body["matches"]
    for match in body["matches"]:
        for passage in match["passages"]:
            assert passage["rerank_score"] is None


def test_find_rerank_opt_in_without_extra_returns_clear_503(
    search_client: TestClient, db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0013 §5: opting in (?rerank=true) without the [rerank] extra returns a
    clear 503 naming the extra — never a raw 500, never a silent cosine swap.
    Torch-free: the missing dependency is simulated by forcing the lazy import
    to fail."""
    import sys

    import apps.api.research.rerank as rerank_module

    seed_search_corpus(db)
    monkeypatch.setattr(rerank_module, "_scorer_singleton", None)
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)  # -> ImportError

    response = search_client.get("/research/find", params={"q": "growth", "rerank": "true"})

    assert response.status_code == 503
    assert "[rerank]" in response.json()["detail"]


def test_find_response_cannot_carry_a_ranking_or_synthesis(
    search_client: TestClient, db: Engine
) -> None:
    """ADR-0011 §5: FIND surfaces cited passages ordered by text-match
    strength. A company ranking or generated characterization is structurally
    unrepresentable — no response field could carry one."""
    seed_search_corpus(db)
    body = search_client.get("/research/find", params={"q": "growth"}).json()
    forbidden = {"rank", "ranking", "score", "rating", "exposure", "answer", "explanation"}
    assert forbidden.isdisjoint(FindResponse.model_fields)
    assert forbidden.isdisjoint(FindCompanyMatchResponse.model_fields)
    assert forbidden.isdisjoint(FindPassageResponse.model_fields)
    assert set(body) == set(FindResponse.model_fields)


def test_find_empty_corpus_returns_200_with_empty_matches(
    search_client: TestClient, db: Engine
) -> None:
    response = search_client.get("/research/find", params={"q": "tariffs"})
    assert response.status_code == 200
    assert response.json() == {"query": "tariffs", "matches": []}


def test_find_blank_query_returns_422(search_client: TestClient) -> None:
    response = search_client.get("/research/find", params={"q": "   "})
    assert response.status_code == 422
    assert "blank" in response.json()["detail"]


def test_find_embedding_failure_returns_502(search_client: TestClient, db: Engine) -> None:
    seed_search_corpus(db)
    app.dependency_overrides[get_embedder] = lambda: ExplodingEmbedder()
    response = search_client.get("/research/find", params={"q": "growth"})
    assert response.status_code == 502
    assert "voyage" in response.json()["detail"]


# --- GET /research/compare (ADR-0012: live build — tests still use the mock) ---


@pytest.fixture
def compare_client(db: Engine) -> Iterator[TestClient]:
    app.dependency_overrides[get_embedder] = lambda: QueryOnlyEmbedder(axis_vector(0))
    app.dependency_overrides[get_read_engine] = lambda: db
    # The live build's answerer is Claude-backed; the suite overrides it with
    # the deterministic mock so tests keep spending zero live tokens.
    app.dependency_overrides[get_column_answerer] = MockColumnAnswerer
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_compare_returns_one_ordered_typed_entries_list(
    compare_client: TestClient, db: Engine
) -> None:
    """ADR-0012 #2: columns and failures share ONE ordered list in caller
    order — there is no side failure list a renderer could skip."""
    seed_ready_company(db)
    response = compare_client.get(
        "/research/compare", params={"q": "tariffs", "tickers": ["AAPL", "MISSING"]}
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == set(CompareResponse.model_fields)
    assert body["partial"] is True
    assert [entry["kind"] for entry in body["entries"]] == ["column", "unresolved"]
    assert [entry["symbol"] for entry in body["entries"]] == ["AAPL", "MISSING"]
    column = body["entries"][0]
    assert column["coverage"] == {
        "qualifying": 2,
        "consulted": 2,
        "scanned": 2,
        "floor": QUALIFYING_SIMILARITY_FLOOR,
    }
    assert column["filing"]["form_type"] == "10-K"
    for statement in column["statements"]:
        assert statement["source_url"].startswith("https://sec.gov/")


def test_compare_over_cap_is_a_400_refusal_naming_the_cap(
    compare_client: TestClient, db: Engine
) -> None:
    for i, ticker in enumerate(["AAPL", "MSFT", "GOOG", "AMZN", "NVDA", "META"]):
        seed_compare_reference(db, ticker, f"000000000{i}", f"Company {ticker}")
    response = compare_client.get(
        "/research/compare",
        params={"q": "tariffs", "tickers": ["AAPL", "MSFT", "GOOG", "AMZN", "NVDA", "META"]},
    )
    assert response.status_code == 400
    assert "at most 5" in response.json()["detail"]


def test_compare_answerer_is_live_and_key_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-0012 live build: get_column_answerer() was THE single swap point,
    and it swapped — the shipped answerer is Claude-backed, gated on the API
    key exactly like get_qa_answerer (503, never a silent mock fallback)."""
    monkeypatch.setattr(
        "apps.api.research.router.get_settings",
        lambda: Settings(anthropic_api_key="", voyage_api_key="present"),
    )
    get_column_answerer.cache_clear()
    try:
        with pytest.raises(HTTPException) as excinfo:
            get_column_answerer()
        assert excinfo.value.status_code == 503
        assert "COMPARE unavailable" in excinfo.value.detail
        monkeypatch.setattr(
            "apps.api.research.router.get_settings",
            lambda: Settings(anthropic_api_key="test-key", voyage_api_key="present"),
        )
        get_column_answerer.cache_clear()
        assert isinstance(get_column_answerer(), ClaudeColumnAnswerer)
    finally:
        get_column_answerer.cache_clear()


def test_compare_response_cannot_carry_a_ranking_or_ordering() -> None:
    """ADR-0012 #4 / wall-guard tripwire: a cross-company ranking must be
    structurally unrepresentable in the COMPARE payload — no response model
    has a field that could carry one. match_strength is a retrieval fact
    that is fine in FIND (ADR-0011 §5) and deliberately absent HERE: in a
    side-by-side, strength and position read as judgment. This lands BEFORE
    the live swap so a rank/score field leaking in with the real answerer
    trips immediately."""
    forbidden = {
        "rank",
        "ranking",
        "score",
        "rating",
        "exposure",
        "answer",
        "explanation",
        "match_strength",
        "verdict",
    }
    for model in (
        CompareResponse,
        CompareEntryResponse,
        ComparePinnedFilingResponse,
        CompareStatementResponse,
        CompareCoverageResponse,
        ComparePassageResponse,
    ):
        assert forbidden.isdisjoint(model.model_fields), model.__name__
