from datetime import date

# EdgarClient and ResearchService are built on httpx2 (see service.py); the
# rate-limit categorization test must raise the same exception types they catch.
import httpx2 as httpx
import pytest
from sqlalchemy import Engine, text

from apps.api.edgar.client import (
    CompanyRef,
    FilingNotFoundError,
    FilingRef,
    TickerReferenceRow,
)
from apps.api.research.batch import (
    BatchReport,
    ReferenceNotPopulatedError,
    TickerResult,
    format_report,
    parse_ticker_list,
    run_batch,
)
from apps.api.research.service import ResearchService
from apps.api.research.ticker_reference import refresh
from apps.api.tests.test_service import HTML, FakeSummarizer, count

UNPARSEABLE_HTML = "<html><body><p>No items here at all.</p></body></html>"


def filing(seq: int) -> FilingRef:
    return FilingRef(
        accession_number=f"{seq:010d}-25-{seq:06d}",
        form_type="10-K",
        filing_date=date(2025, 11, 1),
        period_end_date=date(2025, 9, 30),
        filing_url=f"https://www.sec.gov/Archives/edgar/data/{seq}/x/f{seq}-10k.htm",
    )


def seed_reference(db: Engine, tickers: list[str]) -> None:
    """Populate sec_ticker_reference for the given tickers via the real
    refresh path. The conformed name deliberately differs from what
    MultiTickerEdgar.resolve_ticker would return ("X Inc."), so any companies
    row carrying it proves identity came from the reference (ADR-0010 #3)."""
    rows = [
        TickerReferenceRow(
            ticker=t,
            cik=f"{seq:010d}",
            company_name=f"{t} Conformed Inc.",
            exchange="NYSE",
        )
        for seq, t in enumerate(tickers, start=1)
    ]
    refresh(db, lambda: rows)


class MultiTickerEdgar:
    """FakeEdgar over several companies, keyed by ticker.

    Tickers listed in `unparseable` resolve and fetch fine but return HTML the
    section extractor cannot parse; tickers in `rate_limited` fail their EDGAR
    filing lookup with an HTTP 429, the way a throttled EdgarClient call
    would. A company the fake does not know raises FilingNotFoundError from
    latest_10k — a real ticker with nothing on EDGAR. resolve_calls counts
    resolve_ticker uses: the batch path resolves from sec_ticker_reference and
    must never call it.
    """

    def __init__(
        self,
        tickers: list[str],
        unparseable: set[str] | None = None,
        rate_limited: set[str] | None = None,
    ) -> None:
        self._companies = {
            t: CompanyRef(ticker=t, cik=f"{seq:010d}", name=f"{t} Inc.")
            for seq, t in enumerate(tickers, start=1)
        }
        self._filings = {t: filing(seq) for seq, t in enumerate(tickers, start=1)}
        self._unparseable = {self._filings[t].accession_number for t in (unparseable or set())}
        self._rate_limited = rate_limited or set()
        self.resolve_calls = 0

    def resolve_ticker(self, ticker: str) -> CompanyRef:
        self.resolve_calls += 1
        return self._companies[ticker.upper()]

    def latest_10k(self, company: CompanyRef) -> FilingRef:
        if company.ticker in self._rate_limited:
            request = httpx.Request("GET", "https://data.sec.gov/submissions/CIK.json")
            raise httpx.HTTPStatusError(
                "429 Too Many Requests",
                request=request,
                response=httpx.Response(429, request=request),
            )
        found = self._filings.get(company.ticker)
        if found is None:
            raise FilingNotFoundError(f"no 10-K filing found for CIK {company.cik}")
        return found

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
    seed_reference(db, tickers)
    service = make_service(MultiTickerEdgar(tickers), db)

    report = run_batch(service, tickers, engine=db)

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
    seed_reference(db, tickers)
    edgar = MultiTickerEdgar(tickers)
    service = make_service(edgar, db)
    # AAA is stored through the existing single-ticker path, not batch code.
    assert service.run("AAA").status == "ingested"

    report = run_batch(service, tickers, engine=db)

    by_ticker = {r.ticker: r for r in report.results}
    assert by_ticker["AAA"].status == "skipped"
    assert by_ticker["BBB"].status == "ingested"
    assert count(db, "filings") == 2

    rerun = run_batch(service, tickers, engine=db)
    assert all(r.status == "skipped" for r in rerun.results)
    assert count(db, "filings") == 2  # nothing re-ingested
    assert count(db, "thesis_snapshots") == 2


def test_failure_mid_list_does_not_abort_and_is_categorized(db: Engine) -> None:
    tickers = ["AAA", "BAD", "CCC"]
    seed_reference(db, tickers)
    edgar = MultiTickerEdgar(tickers, unparseable={"BAD"})
    service = make_service(edgar, db)

    report = run_batch(service, tickers, engine=db)

    by_ticker = {r.ticker: r for r in report.results}
    assert by_ticker["AAA"].status == "ingested"
    assert by_ticker["CCC"].status == "ingested"  # the ticker after the failure still ran
    failed = by_ticker["BAD"]
    assert failed.status == "failed"
    assert failed.category == "parse_error"
    assert failed.reason is not None and "SectionExtractionError" in failed.reason
    assert report.failures == (failed,)
    assert count(db, "filings") == 2  # nothing persisted for the failed ticker


def test_unresolved_not_found_and_rate_limited_stay_distinct(db: Engine) -> None:
    # BADSYM: not in the reference (bad curated-list entry) → unresolved,
    # never attempted against EDGAR. NOFILING: resolves, but EDGAR has no
    # 10-K → not_found. Different failures, different remedies.
    tickers = ["AAA", "BADSYM", "NOFILING", "THROTTLED"]
    seed_reference(db, ["AAA", "NOFILING", "THROTTLED"])
    edgar = MultiTickerEdgar(["AAA", "THROTTLED"], rate_limited={"THROTTLED"})
    service = make_service(edgar, db)

    report = run_batch(service, tickers, engine=db)

    by_ticker = {r.ticker: r for r in report.results}
    assert by_ticker["AAA"].status == "ingested"
    assert by_ticker["BADSYM"].category == "unresolved"
    assert (
        by_ticker["BADSYM"].reason is not None
        and "sec_ticker_reference" in by_ticker["BADSYM"].reason
    )
    assert by_ticker["NOFILING"].category == "not_found"
    assert by_ticker["THROTTLED"].category == "rate_limited"
    # Resolution alone created no companies rows: only AAA was ingested.
    assert count(db, "companies") == 1


def test_ingested_identity_comes_from_the_reference(db: Engine) -> None:
    seed_reference(db, ["AAA"])
    edgar = MultiTickerEdgar(["AAA"])
    service = make_service(edgar, db)

    report = run_batch(service, ["AAA"], engine=db)

    assert report.ingested == 1
    with db.connect() as conn:
        row = conn.execute(text("SELECT ticker, cik, name FROM companies")).one()
    # The reference's conformed name and CIK, not resolve_ticker's "AAA Inc.".
    assert (row.ticker, row.cik, row.name) == ("AAA", "0000000001", "AAA Conformed Inc.")
    assert edgar.resolve_calls == 0  # identity never came from EDGAR resolution


def test_resolving_without_ingesting_creates_no_companies_row(db: Engine) -> None:
    # XYZ resolves in the reference but its EDGAR ingest fails — the resolved
    # identity must not have landed in companies (ADR-0010 #2 corollary).
    seed_reference(db, ["XYZ"])
    service = make_service(MultiTickerEdgar([]), db)

    report = run_batch(service, ["XYZ"], engine=db)

    assert report.failures[0].category == "not_found"
    assert count(db, "companies") == 0


def test_report_accounts_for_every_ticker(db: Engine) -> None:
    tickers = ["AAA", "BAD", "BADSYM", "CCC"]
    seed_reference(db, ["AAA", "BAD", "CCC"])
    edgar = MultiTickerEdgar(["AAA", "BAD", "CCC"], unparseable={"BAD"})
    service = make_service(edgar, db)
    assert service.run("CCC").status == "ingested"  # pre-stored → skipped in the batch

    report = run_batch(service, tickers, engine=db)

    assert {r.ticker for r in report.results} == set(tickers)
    assert report.ingested + report.skipped + len(report.failures) == report.attempted
    assert report.attempted == len(tickers)
    assert all(r.status in ("ingested", "skipped", "failed") for r in report.results)


def test_empty_reference_fails_loudly_not_as_unresolved_symbols(db: Engine) -> None:
    service = make_service(MultiTickerEdgar(["AAA"]), db)

    with pytest.raises(ReferenceNotPopulatedError, match="ticker_reference"):
        run_batch(service, ["AAA"], engine=db)
    assert count(db, "companies") == 0  # nothing was attempted


def test_delay_is_applied_between_companies_not_before_the_first(db: Engine) -> None:
    tickers = ["AAA", "BBB", "CCC"]
    seed_reference(db, tickers)
    service = make_service(MultiTickerEdgar(tickers), db)
    naps: list[float] = []

    run_batch(service, tickers, engine=db, delay_seconds=0.25, sleep=naps.append)

    assert naps == [0.25, 0.25]  # between companies only


def test_unresolved_symbols_get_no_politeness_delay(db: Engine) -> None:
    # BADSYM contacts nothing, so the only delay is between the two tickers
    # that actually hit EDGAR.
    tickers = ["AAA", "BADSYM", "BBB"]
    seed_reference(db, ["AAA", "BBB"])
    service = make_service(MultiTickerEdgar(["AAA", "BBB"]), db)
    naps: list[float] = []

    run_batch(service, tickers, engine=db, delay_seconds=0.25, sleep=naps.append)

    assert naps == [0.25]


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
            TickerResult(
                ticker="BADSYM",
                status="failed",
                category="unresolved",
                reason="not in sec_ticker_reference (the SEC-resolvable universe)",
            ),
        )
    )
    text = format_report(report)
    assert "4 attempted" in text
    assert "1 ingested" in text
    assert "1 skipped" in text
    assert "2 failed" in text
    assert "BAD" in text and "parse_error" in text and "SectionExtractionError" in text
    assert "BADSYM" in text and "unresolved" in text
