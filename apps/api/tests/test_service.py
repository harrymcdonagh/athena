from collections.abc import Sequence
from datetime import date

import anthropic

# anthropic SDK exception types are bound to real httpx; do not use httpx2 here.
import httpx
import pytest
from sqlalchemy import Engine, text

from apps.api.edgar.client import CompanyRef, FilingNotFoundError, FilingRef
from apps.api.research.service import (
    ResearchService,
    UnsupportedFilingTypeError,
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
PRIOR_FILING = FilingRef(
    accession_number="0000320193-24-000100",
    form_type="10-K",
    filing_date=date(2024, 11, 1),
    period_end_date=date(2024, 9, 28),
    filing_url="https://www.sec.gov/Archives/edgar/data/320193/x/old-10k.htm",
)
EIGHT_K = FilingRef(
    accession_number="0000320193-26-000001",
    form_type="8-K",
    filing_date=date(2026, 1, 5),
    period_end_date=date(2026, 1, 5),
    filing_url="https://www.sec.gov/Archives/edgar/data/320193/x/a8k.htm",
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
    def __init__(self, filings: Sequence[FilingRef] = (FILING,)) -> None:
        self._filings = list(filings)  # newest first, like EDGAR's recent window

    def resolve_ticker(self, ticker: str) -> CompanyRef:
        return COMPANY

    def latest_10k(self, company: CompanyRef) -> FilingRef:
        for filing in self._filings:
            if filing.form_type == "10-K":
                return filing
        raise FilingNotFoundError(f"no 10-K filing found for CIK {company.cik}")

    def get_filing(self, company: CompanyRef, accession_number: str) -> FilingRef:
        # Exact match only: dash-insensitive normalization is EdgarClient's job
        # and is pinned by test_edgar_client; callers here pass canonical form.
        for filing in self._filings:
            if filing.accession_number == accession_number:
                return filing
        raise FilingNotFoundError(f"accession {accession_number!r} not found")

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

    assert outcome.status == "ingested"
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


def test_rerun_same_filing_is_skipped_noop(db: Engine) -> None:
    service = ResearchService(edgar=FakeEdgar(), summarizer=FakeSummarizer(), engine=db)
    first = service.run("AAPL")

    second = service.run("AAPL")

    assert second.status == "skipped"
    assert second.filing_id == first.filing_id
    assert second.company_id == first.company_id
    assert second.accession_number == first.accession_number
    assert second.summaries == {}
    assert second.thesis_snapshot_id is None
    assert count(db, "filings") == 1
    assert count(db, "filing_summaries") == 3
    assert count(db, "thesis_snapshots") == 1


def test_run_with_accession_ingests_prior_year_filing(db: Engine) -> None:
    edgar = FakeEdgar(filings=[FILING, PRIOR_FILING])
    service = ResearchService(edgar=edgar, summarizer=FakeSummarizer(), engine=db)
    service.run("AAPL")

    outcome = service.run("AAPL", accession_number=PRIOR_FILING.accession_number)

    assert outcome.status == "ingested"
    assert outcome.accession_number == PRIOR_FILING.accession_number
    assert count(db, "companies") == 1
    assert count(db, "filings") == 2
    assert count(db, "filing_summaries") == 6
    assert count(db, "thesis_snapshots") == 2
    with db.connect() as conn:
        source_urls = (
            conn.execute(
                text(
                    "SELECT DISTINCT s.source_url FROM filing_summaries s"
                    " JOIN filings f ON f.id = s.filing_id"
                    " WHERE f.accession_number = :acc"
                ),
                {"acc": PRIOR_FILING.accession_number},
            )
            .scalars()
            .all()
        )
        period = conn.execute(
            text("SELECT period_end_date FROM filings WHERE accession_number = :acc"),
            {"acc": PRIOR_FILING.accession_number},
        ).scalar_one()
    assert source_urls == [PRIOR_FILING.filing_url]
    assert period == PRIOR_FILING.period_end_date


def test_rerun_prior_year_accession_is_skipped_noop(db: Engine) -> None:
    edgar = FakeEdgar(filings=[FILING, PRIOR_FILING])
    service = ResearchService(edgar=edgar, summarizer=FakeSummarizer(), engine=db)
    service.run("AAPL")
    first = service.run("AAPL", accession_number=PRIOR_FILING.accession_number)

    second = service.run("AAPL", accession_number=PRIOR_FILING.accession_number)

    assert second.status == "skipped"
    assert second.filing_id == first.filing_id
    assert second.thesis_snapshot_id is None
    assert count(db, "filings") == 2
    assert count(db, "filing_summaries") == 6
    assert count(db, "thesis_snapshots") == 2


def test_run_with_unknown_accession_raises_filing_not_found(db: Engine) -> None:
    service = ResearchService(edgar=FakeEdgar(), summarizer=FakeSummarizer(), engine=db)
    with pytest.raises(FilingNotFoundError):
        service.run("AAPL", accession_number="0000320193-99-999999")
    assert count(db, "filings") == 0


def test_run_rejects_non_10k_target(db: Engine) -> None:
    edgar = FakeEdgar(filings=[EIGHT_K, FILING])
    service = ResearchService(edgar=edgar, summarizer=FakeSummarizer(), engine=db)
    with pytest.raises(UnsupportedFilingTypeError, match="8-K"):
        service.run("AAPL", accession_number=EIGHT_K.accession_number)
    assert count(db, "filings") == 0
    assert count(db, "filing_summaries") == 0


def test_run_rejects_missing_period_end_date(db: Engine) -> None:
    undated = FilingRef(
        accession_number="0000320193-24-000999",
        form_type="10-K",
        filing_date=date(2024, 11, 1),
        period_end_date=None,
        filing_url="https://www.sec.gov/Archives/edgar/data/320193/x/undated.htm",
    )
    service = ResearchService(
        edgar=FakeEdgar(filings=[undated]), summarizer=FakeSummarizer(), engine=db
    )
    with pytest.raises(UpstreamError) as exc:
        service.run("AAPL")
    assert exc.value.source == "sec-edgar"
    assert count(db, "filings") == 0


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
