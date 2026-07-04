from datetime import date

import anthropic
import httpx
import pytest
from sqlalchemy import Engine, text

from apps.api.edgar.client import CompanyRef, FilingRef
from apps.api.research.service import (
    FilingAlreadyIngestedError,
    ResearchService,
    UpstreamError,
    compose_thesis,
)

COMPANY = CompanyRef(ticker="AAPL", cik="0000320193", name="Apple Inc.")
FILING = FilingRef(
    accession_number="0000320193-25-000123",
    form_type="10-K",
    filing_date=date(2025, 11, 1),
    period_end_date=date(2025, 9, 27),
    filing_url="https://www.sec.gov/Archives/edgar/data/320193/x/aapl-10k.htm",
)

BUSINESS = "We design and sell widgets. Revenue was $391,035 million in fiscal 2025. " * 20
RISKS = "Competition may harm margins. Supply chain concentration in one region. " * 20
MDNA = "Net sales increased 2% to $391 billion driven by services growth of 13%. " * 20

HTML = (
    "<html><body>"
    "<p>Item 1. Business ... 3</p><p>Item 1A. Risk Factors ... 20</p>"
    "<p>Item 1B. Unresolved ... 45</p><p>Item 7. Management's Discussion ... 50</p>"
    "<p>Item 7A. Quantitative ... 80</p>"
    f"<h2>Item 1. Business</h2><p>{BUSINESS}</p>"
    f"<h2>Item 1A. Risk Factors</h2><p>{RISKS}</p>"
    "<h2>Item 1B. Unresolved Staff Comments</h2><p>None.</p>"
    f"<h2>Item 7. Management's Discussion and Analysis</h2><p>{MDNA}</p>"
    "<h2>Item 7A. Quantitative and Qualitative Disclosures</h2><p>Rates.</p>"
    "</body></html>"
)


class FakeEdgar:
    def resolve_ticker(self, ticker: str) -> CompanyRef:
        return COMPANY

    def latest_10k(self, company: CompanyRef) -> FilingRef:
        return FILING

    def fetch_document(self, filing: FilingRef) -> str:
        return HTML


class FakeSummarizer:
    model = "fake-model"

    def summarize(self, section: str, text: str, source_url: str) -> str:
        return f"[{section}] summary. Source: {source_url}"


class ExplodingSummarizer(FakeSummarizer):
    def summarize(self, section: str, text: str, source_url: str) -> str:
        raise UpstreamError("anthropic", "boom")


class AnthropicErrorSummarizer(FakeSummarizer):
    def summarize(self, section: str, text: str, source_url: str) -> str:
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        raise anthropic.APIConnectionError(request=request)


def count(db: Engine, table: str) -> int:
    with db.connect() as conn:
        result: int = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
    return result


def test_run_persists_everything(db: Engine) -> None:
    service = ResearchService(edgar=FakeEdgar(), summarizer=FakeSummarizer(), engine=db)
    outcome = service.run("AAPL")

    assert set(outcome.summaries) == {"business", "risk_factors", "mdna"}
    assert count(db, "companies") == 1
    assert count(db, "filings") == 1
    assert count(db, "filing_summaries") == 3
    assert count(db, "thesis_snapshots") == 1
    with db.connect() as conn:
        stored_model = conn.execute(
            text("SELECT DISTINCT model FROM filing_summaries")
        ).scalar_one()
        source_text = conn.execute(
            text("SELECT source_text FROM filing_summaries WHERE section = 'business'")
        ).scalar_one()
    assert stored_model == "fake-model"
    assert "Revenue was $391,035 million" in source_text


def test_rerun_same_filing_raises_409_error(db: Engine) -> None:
    service = ResearchService(edgar=FakeEdgar(), summarizer=FakeSummarizer(), engine=db)
    outcome = service.run("AAPL")
    with pytest.raises(FilingAlreadyIngestedError) as exc:
        service.run("AAPL")
    assert exc.value.filing_id == outcome.filing_id


def test_summarizer_failure_persists_nothing(db: Engine) -> None:
    service = ResearchService(edgar=FakeEdgar(), summarizer=ExplodingSummarizer(), engine=db)
    with pytest.raises(UpstreamError):
        service.run("AAPL")
    assert count(db, "companies") == 0
    assert count(db, "filings") == 0
    assert count(db, "filing_summaries") == 0
    assert count(db, "thesis_snapshots") == 0


def test_anthropic_error_is_wrapped_as_upstream_error(db: Engine) -> None:
    service = ResearchService(edgar=FakeEdgar(), summarizer=AnthropicErrorSummarizer(), engine=db)
    with pytest.raises(UpstreamError) as exc:
        service.run("AAPL")
    assert exc.value.source == "anthropic"
    assert count(db, "companies") == 0
    assert count(db, "filings") == 0
    assert count(db, "filing_summaries") == 0
    assert count(db, "thesis_snapshots") == 0


def test_compose_thesis_cites_filing() -> None:
    thesis = compose_thesis(COMPANY, FILING, {"business": "b", "risk_factors": "r", "mdna": "m"})
    assert "Apple Inc." in thesis
    assert FILING.filing_url in thesis
    assert FILING.accession_number in thesis
    assert "recommendation" in thesis.lower()  # the no-advice disclaimer is embedded
