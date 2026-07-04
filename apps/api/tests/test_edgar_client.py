import json
from collections.abc import Callable

import httpx2 as httpx
import pytest

from apps.api.edgar.client import (
    CompanyRef,
    EdgarClient,
    FilingNotFoundError,
    TickerNotFoundError,
)

TICKERS = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
}

SUBMISSIONS = {
    "filings": {
        "recent": {
            "form": ["8-K", "10-K", "10-K"],
            "accessionNumber": [
                "0000320193-26-000001",
                "0000320193-25-000123",
                "0000320193-24-000100",
            ],
            "filingDate": ["2026-01-05", "2025-11-01", "2024-11-01"],
            "reportDate": ["2026-01-05", "2025-09-27", "2024-09-28"],
            "primaryDocument": ["a8k.htm", "aapl-10k.htm", "old-10k.htm"],
        }
    }
}


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> EdgarClient:
    http = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": "t t@e.c"})
    return EdgarClient(user_agent="t t@e.c", client=http)


def edgar_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url.endswith("company_tickers.json"):
        return httpx.Response(200, text=json.dumps(TICKERS))
    if "submissions/CIK0000320193" in url:
        return httpx.Response(200, text=json.dumps(SUBMISSIONS))
    if url.endswith("aapl-10k.htm"):
        return httpx.Response(200, text="<html>10-K body</html>")
    return httpx.Response(404)


def test_resolve_ticker_pads_cik_and_is_case_insensitive() -> None:
    client = make_client(edgar_handler)
    company = client.resolve_ticker("aapl")
    assert company == CompanyRef(ticker="AAPL", cik="0000320193", name="Apple Inc.")


def test_resolve_ticker_unknown_raises() -> None:
    client = make_client(edgar_handler)
    with pytest.raises(TickerNotFoundError):
        client.resolve_ticker("ZZZZ")


def test_latest_10k_picks_first_10k_and_builds_url() -> None:
    client = make_client(edgar_handler)
    company = CompanyRef(ticker="AAPL", cik="0000320193", name="Apple Inc.")
    filing = client.latest_10k(company)
    assert filing.accession_number == "0000320193-25-000123"
    assert filing.form_type == "10-K"
    assert filing.filing_date.isoformat() == "2025-11-01"
    assert filing.period_end_date is not None
    assert filing.filing_url == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019325000123/aapl-10k.htm"
    )


def test_latest_10k_none_found_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=json.dumps(
                {
                    "filings": {
                        "recent": {
                            "form": ["8-K"],
                            "accessionNumber": ["a-1"],
                            "filingDate": ["2026-01-01"],
                            "reportDate": ["2026-01-01"],
                            "primaryDocument": ["a.htm"],
                        }
                    }
                }
            ),
        )

    client = make_client(handler)
    with pytest.raises(FilingNotFoundError):
        client.latest_10k(CompanyRef(ticker="X", cik="0000000009", name="X"))


def test_fetch_document_returns_html() -> None:
    client = make_client(edgar_handler)
    company = CompanyRef(ticker="AAPL", cik="0000320193", name="Apple Inc.")
    filing = client.latest_10k(company)
    assert client.fetch_document(filing) == "<html>10-K body</html>"
