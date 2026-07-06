from datetime import date

# EdgarClient and ResearchService are built on httpx2 (see service.py); the
# rate-limit categorization test must raise the same exception types they catch.
import httpx2 as httpx
from sqlalchemy import Engine

from apps.api.edgar.client import (
    CompanyRef,
    FilingNotFoundError,
    FilingRef,
    TickerNotFoundError,
)
from apps.api.research.batch import (
    BatchReport,
    TickerResult,
    format_report,
    parse_ticker_list,
    run_batch,
)
from apps.api.research.service import ResearchService
from apps.api.tests.test_service import HTML, FakeSummarizer, count

UNPARSEABLE_HTML = "<html><body><p>No items here at all.</p></body></html>"


def company(ticker: str, seq: int) -> CompanyRef:
    return CompanyRef(ticker=ticker, cik=f"{seq:010d}", name=f"{ticker} Inc.")


def filing(seq: int) -> FilingRef:
    return FilingRef(
        accession_number=f"{seq:010d}-25-{seq:06d}",
        form_type="10-K",
        filing_date=date(2025, 11, 1),
        period_end_date=date(2025, 9, 30),
        filing_url=f"https://www.sec.gov/Archives/edgar/data/{seq}/x/f{seq}-10k.htm",
    )


class MultiTickerEdgar:
    """FakeEdgar over several companies, keyed by ticker.

    Tickers listed in `unparseable` resolve and fetch fine but return HTML the
    section extractor cannot parse; tickers in `rate_limited` fail their EDGAR
    lookup with an HTTP 429, the way a throttled EdgarClient call would.
    """

    def __init__(
        self,
        tickers: list[str],
        unparseable: set[str] | None = None,
        rate_limited: set[str] | None = None,
    ) -> None:
        self._companies = {t: company(t, seq) for seq, t in enumerate(tickers, start=1)}
        self._filings = {t: filing(seq) for seq, t in enumerate(tickers, start=1)}
        self._unparseable = {self._filings[t].accession_number for t in (unparseable or set())}
        self._rate_limited = rate_limited or set()

    def resolve_ticker(self, ticker: str) -> CompanyRef:
        if ticker.upper() in self._rate_limited:
            request = httpx.Request("GET", "https://www.sec.gov/files/company_tickers.json")
            raise httpx.HTTPStatusError(
                "429 Too Many Requests",
                request=request,
                response=httpx.Response(429, request=request),
            )
        try:
            return self._companies[ticker.upper()]
        except KeyError:
            raise TickerNotFoundError(f"ticker {ticker!r} not found on SEC EDGAR") from None

    def latest_10k(self, company: CompanyRef) -> FilingRef:
        return self._filings[company.ticker]

    def get_filing(self, company: CompanyRef, accession_number: str) -> FilingRef:
        target = self._filings[company.ticker]
        if target.accession_number != accession_number:
            raise FilingNotFoundError(f"accession {accession_number!r} not found")
        return target

    def fetch_document(self, filing: FilingRef) -> str:
        if filing.accession_number in self._unparseable:
            return UNPARSEABLE_HTML
        return HTML


def make_service(edgar: MultiTickerEdgar, db: Engine) -> ResearchService:
    return ResearchService(edgar=edgar, summarizer=FakeSummarizer(), engine=db)


def test_batch_ingests_every_ticker_and_reports_each(db: Engine) -> None:
    tickers = ["AAA", "BBB", "CCC"]
    service = make_service(MultiTickerEdgar(tickers), db)

    report = run_batch(service, tickers)

    assert [r.ticker for r in report.results] == tickers
    assert all(r.status == "ingested" for r in report.results)
    assert report.attempted == 3
    assert report.ingested == 3
    assert report.skipped == 0
    assert report.failures == ()
    assert count(db, "companies") == 3
    assert count(db, "filings") == 3


def test_batch_rerun_skips_already_stored_via_existing_path(db: Engine) -> None:
    tickers = ["AAA", "BBB"]
    edgar = MultiTickerEdgar(tickers)
    service = make_service(edgar, db)
    # AAA is stored through the existing single-ticker path, not batch code.
    assert service.run("AAA").status == "ingested"

    report = run_batch(service, tickers)

    by_ticker = {r.ticker: r for r in report.results}
    assert by_ticker["AAA"].status == "skipped"
    assert by_ticker["BBB"].status == "ingested"
    assert count(db, "filings") == 2

    rerun = run_batch(service, tickers)
    assert all(r.status == "skipped" for r in rerun.results)
    assert count(db, "filings") == 2  # nothing re-ingested
    assert count(db, "thesis_snapshots") == 2


def test_failure_mid_list_does_not_abort_and_is_categorized(db: Engine) -> None:
    tickers = ["AAA", "BAD", "CCC"]
    edgar = MultiTickerEdgar(tickers, unparseable={"BAD"})
    service = make_service(edgar, db)

    report = run_batch(service, tickers)

    by_ticker = {r.ticker: r for r in report.results}
    assert by_ticker["AAA"].status == "ingested"
    assert by_ticker["CCC"].status == "ingested"  # the ticker after the failure still ran
    failed = by_ticker["BAD"]
    assert failed.status == "failed"
    assert failed.category == "parse_error"
    assert failed.reason is not None and "SectionExtractionError" in failed.reason
    assert report.failures == (failed,)
    assert count(db, "filings") == 2  # nothing persisted for the failed ticker


def test_not_found_and_rate_limited_failures_are_categorized(db: Engine) -> None:
    tickers = ["AAA", "NOPE", "THROTTLED"]
    edgar = MultiTickerEdgar(["AAA", "THROTTLED"], rate_limited={"THROTTLED"})
    service = make_service(edgar, db)

    report = run_batch(service, tickers)

    by_ticker = {r.ticker: r for r in report.results}
    assert by_ticker["NOPE"].status == "failed"
    assert by_ticker["NOPE"].category == "not_found"
    assert by_ticker["THROTTLED"].status == "failed"
    assert by_ticker["THROTTLED"].category == "rate_limited"


def test_report_accounts_for_every_ticker(db: Engine) -> None:
    tickers = ["AAA", "BAD", "NOPE", "CCC"]
    edgar = MultiTickerEdgar(["AAA", "BAD", "CCC"], unparseable={"BAD"})
    service = make_service(edgar, db)
    assert service.run("CCC").status == "ingested"  # pre-stored → skipped in the batch

    report = run_batch(service, tickers)

    assert {r.ticker for r in report.results} == set(tickers)
    assert report.ingested + report.skipped + len(report.failures) == report.attempted
    assert report.attempted == len(tickers)
    assert all(r.status in ("ingested", "skipped", "failed") for r in report.results)


def test_delay_is_applied_between_companies_not_before_the_first(db: Engine) -> None:
    tickers = ["AAA", "BBB", "CCC"]
    service = make_service(MultiTickerEdgar(tickers), db)
    naps: list[float] = []

    run_batch(service, tickers, delay_seconds=0.25, sleep=naps.append)

    assert naps == [0.25, 0.25]  # between companies only


def test_parse_ticker_list_ignores_comments_and_blank_lines() -> None:
    text = (
        "# S&P 100 snapshot — 2026-07-06\n"
        "\n"
        "AAPL\n"
        "  msft  # trailing comment\n"
        "\n"
        "# full-line comment\n"
        "GOOG\n"
        "AAPL\n"  # duplicate is collapsed
    )
    assert parse_ticker_list(text) == ["AAPL", "MSFT", "GOOG"]


def test_format_report_lists_every_failure_with_reason() -> None:
    report = BatchReport(
        results=(
            TickerResult(ticker="AAA", status="ingested", accession_number="1-25-1"),
            TickerResult(ticker="BBB", status="skipped", accession_number="2-25-2"),
            TickerResult(
                ticker="BAD",
                status="failed",
                category="parse_error",
                reason="SectionExtractionError: could not locate the start of section 'business'",
            ),
        )
    )
    text = format_report(report)
    assert "3 attempted" in text
    assert "1 ingested" in text
    assert "1 skipped" in text
    assert "1 failed" in text
    assert "BAD" in text
    assert "parse_error" in text
    assert "SectionExtractionError" in text
