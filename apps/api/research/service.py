import hashlib
from dataclasses import dataclass
from typing import Protocol

import anthropic
import httpx2 as httpx
from sqlalchemy import Engine

from apps.api.edgar.client import CompanyRef, FilingRef
from apps.api.edgar.sections import extract_sections
from apps.api.research.repository import Repository
from apps.api.research.summarizer import Summarizer

_SECTION_TITLES = {
    "business": "Business (Item 1)",
    "risk_factors": "Risk Factors (Item 1A)",
    "mdna": "Management's Discussion and Analysis (Item 7)",
}


class EdgarGateway(Protocol):
    def resolve_ticker(self, ticker: str) -> CompanyRef: ...

    def latest_10k(self, company: CompanyRef) -> FilingRef: ...

    def fetch_document(self, filing: FilingRef) -> str: ...


class FilingAlreadyIngestedError(Exception):
    def __init__(self, filing_id: int, accession_number: str) -> None:
        super().__init__(f"filing {accession_number} already ingested (id={filing_id})")
        self.filing_id = filing_id
        self.accession_number = accession_number


class UpstreamError(Exception):
    def __init__(self, source: str, detail: str) -> None:
        super().__init__(f"{source}: {detail}")
        self.source = source
        self.detail = detail


@dataclass(frozen=True)
class ResearchOutcome:
    company_id: int
    filing_id: int
    accession_number: str
    filing_url: str
    summaries: dict[str, str]
    thesis_snapshot_id: int


def compose_thesis(company: CompanyRef, filing: FilingRef, summaries: dict[str, str]) -> str:
    lines = [
        f"# Initial thesis snapshot: {company.name} ({company.ticker})",
        "",
        f"Derived from Form {filing.form_type} filed {filing.filing_date.isoformat()}"
        f" (accession {filing.accession_number}).",
        f"Source: {filing.filing_url}",
        "",
        "_This snapshot summarizes and cites the filing. It contains no investment"
        " recommendation; conclusions are the reader's responsibility._",
    ]
    for section, title in _SECTION_TITLES.items():
        lines += ["", f"## {title}", "", summaries[section]]
    return "\n".join(lines)


class ResearchService:
    def __init__(self, edgar: EdgarGateway, summarizer: Summarizer, engine: Engine) -> None:
        self._edgar = edgar
        self._summarizer = summarizer
        self._engine = engine

    def run(self, ticker: str) -> ResearchOutcome:
        try:
            company = self._edgar.resolve_ticker(ticker)
            filing = self._edgar.latest_10k(company)
        except httpx.HTTPError as exc:
            raise UpstreamError("sec-edgar", str(exc)) from exc

        with self._engine.connect() as conn:
            existing = Repository(conn).find_filing_id(filing.accession_number)
        if existing is not None:
            raise FilingAlreadyIngestedError(existing, filing.accession_number)

        try:
            html = self._edgar.fetch_document(filing)
        except httpx.HTTPError as exc:
            raise UpstreamError("sec-edgar", str(exc)) from exc
        content_sha256 = hashlib.sha256(html.encode("utf-8")).hexdigest()
        sections = extract_sections(html)

        summaries: dict[str, str] = {}
        for section, section_text in sections.items():
            try:
                summaries[section] = self._summarizer.summarize(
                    section, section_text, filing.filing_url
                )
            except anthropic.APIError as exc:
                raise UpstreamError("anthropic", str(exc)) from exc

        thesis = compose_thesis(company, filing, summaries)

        with self._engine.begin() as conn:
            repo = Repository(conn)
            company_id = repo.upsert_company(company.ticker, company.cik, company.name)
            filing_id = repo.insert_filing(company_id, filing, content_sha256)
            for section, summary in summaries.items():
                repo.insert_summary(
                    filing_id,
                    section,
                    summary,
                    source_text=sections[section],
                    source_url=filing.filing_url,
                    model=self._summarizer.model,
                )
            snapshot_id = repo.insert_thesis_snapshot(company_id, filing_id, thesis)

        return ResearchOutcome(
            company_id=company_id,
            filing_id=filing_id,
            accession_number=filing.accession_number,
            filing_url=filing.filing_url,
            summaries=summaries,
            thesis_snapshot_id=snapshot_id,
        )
