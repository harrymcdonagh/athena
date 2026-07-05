from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx2 as httpx

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodash}/{document}"


class EdgarError(Exception):
    pass


class TickerNotFoundError(EdgarError):
    pass


class FilingNotFoundError(EdgarError):
    pass


@dataclass(frozen=True)
class CompanyRef:
    ticker: str
    cik: str
    name: str


@dataclass(frozen=True)
class FilingRef:
    accession_number: str
    form_type: str
    filing_date: date
    period_end_date: date | None
    filing_url: str


class EdgarClient:
    def __init__(self, user_agent: str, client: httpx.Client | None = None) -> None:
        if client is None and not user_agent.strip():
            raise ValueError("SEC_EDGAR_USER_AGENT must be set (SEC fair-access policy)")
        self._http = client or httpx.Client(
            headers={"User-Agent": user_agent}, timeout=30.0, follow_redirects=True
        )

    def resolve_ticker(self, ticker: str) -> CompanyRef:
        data = self._get_json(COMPANY_TICKERS_URL)
        wanted = ticker.upper()
        try:
            for entry in data.values():
                if entry["ticker"].upper() == wanted:
                    return CompanyRef(
                        ticker=entry["ticker"],
                        cik=f"{entry['cik_str']:010d}",
                        name=entry["title"],
                    )
        except KeyError as exc:
            raise EdgarError(f"unexpected EDGAR response shape: missing {exc}") from exc
        raise TickerNotFoundError(f"ticker {ticker!r} not found on SEC EDGAR")

    def latest_10k(self, company: CompanyRef) -> FilingRef:
        filings = self.list_filings(company, "10-K")
        if not filings:
            raise FilingNotFoundError(f"no 10-K filing found for CIK {company.cik}")
        return filings[0]

    def list_filings(self, company: CompanyRef, form_type: str) -> list[FilingRef]:
        """All of a company's filings of the given form type, most recent first.

        Only EDGAR's "recent" window (~1000 most recent filings) is scanned;
        older filings live in paginated archive files and are out of scope for
        now. Exact form match, so amendments (e.g. 10-K/A) are excluded.
        """
        recent = self._recent_filings(company)
        try:
            return [
                self._filing_ref(company, recent, i)
                for i, form in enumerate(recent["form"])
                if form == form_type
            ]
        except (KeyError, IndexError, ValueError) as exc:
            raise EdgarError(f"unexpected EDGAR response shape: {exc!r}") from exc

    def get_filing(self, company: CompanyRef, accession_number: str) -> FilingRef:
        """A specific filing by accession number, matched dash-insensitively.

        Returns the canonical dashed accession number regardless of caller
        formatting, so the database dedup key stays uniform. Same "recent"
        window limitation as list_filings.
        """
        wanted = accession_number.strip().replace("-", "")
        recent = self._recent_filings(company)
        try:
            for i, accession in enumerate(recent["accessionNumber"]):
                if accession.replace("-", "") == wanted:
                    return self._filing_ref(company, recent, i)
        except (KeyError, IndexError, ValueError) as exc:
            raise EdgarError(f"unexpected EDGAR response shape: {exc!r}") from exc
        raise FilingNotFoundError(
            f"accession {accession_number!r} not found in recent filings for CIK {company.cik}"
        )

    def _recent_filings(self, company: CompanyRef) -> dict[str, Any]:
        data = self._get_json(SUBMISSIONS_URL.format(cik=company.cik))
        try:
            recent: dict[str, Any] = data["filings"]["recent"]
        except KeyError as exc:
            raise EdgarError(f"unexpected EDGAR response shape: missing {exc}") from exc
        return recent

    def _filing_ref(self, company: CompanyRef, recent: dict[str, Any], i: int) -> FilingRef:
        accession: str = recent["accessionNumber"][i]
        report_date = recent["reportDate"][i]
        return FilingRef(
            accession_number=accession,
            form_type=recent["form"][i],
            filing_date=date.fromisoformat(recent["filingDate"][i]),
            period_end_date=date.fromisoformat(report_date) if report_date else None,
            filing_url=ARCHIVES_URL.format(
                cik_int=int(company.cik),
                accession_nodash=accession.replace("-", ""),
                document=recent["primaryDocument"][i],
            ),
        )

    def fetch_document(self, filing: FilingRef) -> str:
        response = self._http.get(filing.filing_url)
        response.raise_for_status()
        return response.text

    def _get_json(self, url: str) -> dict[str, Any]:
        response = self._http.get(url)
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result
