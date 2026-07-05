import hashlib
from dataclasses import dataclass
from typing import Literal, Protocol

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

    def get_filing(self, company: CompanyRef, accession_number: str) -> FilingRef: ...

    def fetch_document(self, filing: FilingRef) -> str: ...


class UnsupportedFilingTypeError(Exception):
    def __init__(self, accession_number: str, form_type: str) -> None:
        super().__init__(
            f"filing {accession_number} is a {form_type}; only 10-K ingestion is"
            " supported in this step (ADR-0008 build sequencing: annual-only)"
        )
        self.accession_number = accession_number
        self.form_type = form_type


class UpstreamError(Exception):
    def __init__(self, source: str, detail: str) -> None:
        super().__init__(f"{source}: {detail}")
        self.source = source
        self.detail = detail


@dataclass(frozen=True)
class ResearchOutcome:
    status: Literal["ingested", "skipped"]
    company_id: int
    filing_id: int
    accession_number: str
    filing_url: str
    summaries: dict[str, str]
    thesis_snapshot_id: int | None


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

    def run(self, ticker: str, accession_number: str | None = None) -> ResearchOutcome:
        try:
            company = self._edgar.resolve_ticker(ticker)
            if accession_number is None:
                filing = self._edgar.latest_10k(company)
            else:
                filing = self._edgar.get_filing(company, accession_number)
        except httpx.HTTPError as exc:
            raise UpstreamError("sec-edgar", str(exc)) from exc

        if filing.form_type != "10-K":
            raise UnsupportedFilingTypeError(filing.accession_number, filing.form_type)
        if filing.period_end_date is None:
            # filings.period_end_date is NOT NULL (ADR-0008 §1); a missing EDGAR
            # reportDate is upstream data we refuse to ingest, not a schema gap.
            raise UpstreamError(
                "sec-edgar",
                f"filing {filing.accession_number} has no reportDate;"
                " period_end_date is required (ADR-0008 §1)",
            )

        with self._engine.connect() as conn:
            stored = Repository(conn).find_filing(filing.accession_number)
        if stored is not None:
            return ResearchOutcome(
                status="skipped",
                company_id=stored.company_id,
                filing_id=stored.id,
                accession_number=filing.accession_number,
                filing_url=filing.filing_url,
                summaries={},
                thesis_snapshot_id=None,
            )

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
            status="ingested",
            company_id=company_id,
            filing_id=filing_id,
            accession_number=filing.accession_number,
            filing_url=filing.filing_url,
            summaries=summaries,
            thesis_snapshot_id=snapshot_id,
        )
