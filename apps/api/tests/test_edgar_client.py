import json
from collections.abc import Callable

import httpx2 as httpx
import pytest

from apps.api.edgar.client import (
    CompanyRef,
    EdgarClient,
    EdgarError,
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


def test_list_filings_returns_10ks_newest_first() -> None:
    client = make_client(edgar_handler)
    company = CompanyRef(ticker="AAPL", cik="0000320193", name="Apple Inc.")
    filings = client.list_filings(company, "10-K")
    assert [f.accession_number for f in filings] == [
        "0000320193-25-000123",
        "0000320193-24-000100",
    ]
    assert all(f.form_type == "10-K" for f in filings)
    assert filings[1].filing_url == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019324000100/old-10k.htm"
    )


def test_list_filings_filters_by_form_type() -> None:
    client = make_client(edgar_handler)
    company = CompanyRef(ticker="AAPL", cik="0000320193", name="Apple Inc.")
    filings = client.list_filings(company, "8-K")
    assert [f.accession_number for f in filings] == ["0000320193-26-000001"]
    assert filings[0].form_type == "8-K"


def test_list_filings_no_matches_returns_empty_list() -> None:
    client = make_client(edgar_handler)
    company = CompanyRef(ticker="AAPL", cik="0000320193", name="Apple Inc.")
    assert client.list_filings(company, "10-Q") == []


def test_get_filing_returns_specific_prior_year_10k() -> None:
    client = make_client(edgar_handler)
    company = CompanyRef(ticker="AAPL", cik="0000320193", name="Apple Inc.")
    filing = client.get_filing(company, "0000320193-24-000100")
    assert filing.accession_number == "0000320193-24-000100"
    assert filing.form_type == "10-K"
    assert filing.filing_date.isoformat() == "2024-11-01"
    assert filing.period_end_date is not None
    assert filing.period_end_date.isoformat() == "2024-09-28"
    assert filing.filing_url == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019324000100/old-10k.htm"
    )


def test_get_filing_matches_accession_without_dashes() -> None:
    """Dash-insensitive lookup returns the canonical dashed accession number,
    so the DB dedup key cannot be defeated by caller formatting."""
    client = make_client(edgar_handler)
    company = CompanyRef(ticker="AAPL", cik="0000320193", name="Apple Inc.")
    filing = client.get_filing(company, "000032019324000100")
    assert filing.accession_number == "0000320193-24-000100"


def test_get_filing_unknown_accession_raises_filing_not_found() -> None:
    client = make_client(edgar_handler)
    company = CompanyRef(ticker="AAPL", cik="0000320193", name="Apple Inc.")
    with pytest.raises(FilingNotFoundError):
        client.get_filing(company, "0000320193-99-999999")


def test_get_filing_empty_report_date_maps_to_none_period() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=json.dumps(
                {
                    "filings": {
                        "recent": {
                            "form": ["10-K"],
                            "accessionNumber": ["0000000009-24-000001"],
                            "filingDate": ["2024-11-01"],
                            "reportDate": [""],
                            "primaryDocument": ["x.htm"],
                        }
                    }
                }
            ),
        )

    client = make_client(handler)
    filing = client.get_filing(
        CompanyRef(ticker="X", cik="0000000009", name="X"), "0000000009-24-000001"
    )
    assert filing.period_end_date is None


def test_list_filings_unequal_parallel_arrays_raise_edgar_error() -> None:
    """EDGAR's recent arrays are parallel; a short array must surface as
    EdgarError (502), not an unhandled IndexError (500)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=json.dumps(
                {
                    "filings": {
                        "recent": {
                            "form": ["10-K", "10-K"],
                            "accessionNumber": ["0000000009-25-000001"],
                            "filingDate": ["2025-11-01"],
                            "reportDate": ["2025-09-27"],
                            "primaryDocument": ["x.htm"],
                        }
                    }
                }
            ),
        )

    client = make_client(handler)
    with pytest.raises(EdgarError):
        client.list_filings(CompanyRef(ticker="X", cik="0000000009", name="X"), "10-K")


def test_get_filing_invalid_filing_date_raises_edgar_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=json.dumps(
                {
                    "filings": {
                        "recent": {
                            "form": ["10-K"],
                            "accessionNumber": ["0000000009-25-000001"],
                            "filingDate": ["not-a-date"],
                            "reportDate": ["2025-09-27"],
                            "primaryDocument": ["x.htm"],
                        }
                    }
                }
            ),
        )

    client = make_client(handler)
    with pytest.raises(EdgarError):
        client.get_filing(
            CompanyRef(ticker="X", cik="0000000009", name="X"), "0000000009-25-000001"
        )


def test_latest_10k_malformed_response_raises_edgar_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=json.dumps({"filings": {}}))

    client = make_client(handler)
    with pytest.raises(EdgarError):
        client.latest_10k(CompanyRef(ticker="X", cik="0000000009", name="X"))


def test_fetch_document_returns_html() -> None:
    client = make_client(edgar_handler)
    company = CompanyRef(ticker="AAPL", cik="0000320193", name="Apple Inc.")
    filing = client.latest_10k(company)
    assert client.fetch_document(filing) == "<html>10-K body</html>"


def test_default_client_sets_user_agent() -> None:
    """Verify User-Agent header is set on default httpx client.

    Guards the SEC fair-access requirement that every EDGAR request
    must carry a User-Agent header. Accessing _http is acceptable here
    to verify this invariant is maintained.
    """
    user_agent_str = "Athena test@example.com"
    client = EdgarClient(user_agent=user_agent_str)
    assert client._http.headers["User-Agent"] == user_agent_str


def test_blank_user_agent_without_injected_client_raises() -> None:
    """SEC fair-access policy: refuse to build a default client with no User-Agent."""
    with pytest.raises(ValueError, match="SEC_EDGAR_USER_AGENT"):
        EdgarClient(user_agent="  ")


def test_blank_user_agent_with_injected_client_is_unaffected() -> None:
    """An injected client bypasses the default-client construction, so it is not validated."""
    http = httpx.Client(transport=httpx.MockTransport(edgar_handler))
    client = EdgarClient(user_agent="  ", client=http)
    assert client.resolve_ticker("AAPL") == CompanyRef(
        ticker="AAPL", cik="0000320193", name="Apple Inc."
    )


def test_requests_carry_user_agent_header() -> None:
    """Verify User-Agent header is transmitted in actual requests."""
    captured_headers: list[str | None] = []

    def header_capturing_handler(request: httpx.Request) -> httpx.Response:
        captured_headers.append(request.headers.get("User-Agent"))
        # Return a valid response for company_tickers.json request
        return httpx.Response(200, text=json.dumps(TICKERS))

    client = make_client(header_capturing_handler)
    client.resolve_ticker("AAPL")

    assert len(captured_headers) == 1
    assert captured_headers[0] == "t t@e.c"
