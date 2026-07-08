import hashlib
import logging
from dataclasses import dataclass
from typing import Literal, Protocol

import anthropic
import httpx2 as httpx
from sqlalchemy import Engine

from apps.api.edgar.client import CompanyRef, FilingRef

# _html_to_text is deliberately the extractor's own converter: the guard's
# denominator must be the same document text the sections were sliced from.
# Known cost: extract_sections also parses the HTML internally, so each ingest
# parses twice; a single parse would need the extractor to expose its text.
from apps.api.edgar.sections import _html_to_text, extract_sections
from apps.api.research.repository import Repository, ResearchView
from apps.api.research.summarizer import Summarizer

_logger = logging.getLogger(__name__)

_SECTION_TITLES = {
    "business": "Business (Item 1)",
    "risk_factors": "Risk Factors (Item 1A)",
    "mdna": "Management's Discussion and Analysis (Item 7)",
}

# Section-plausibility thresholds, calibrated against the 2026-07-07 extraction
# repair (85-filing corpus + 24-document fraction measurement). Hijacked-sliver
# corruption sat at ≤1.11% of document text (SPG risk_factors 0.32%, ACN
# ≤1.00%, BA 1.11%); the smallest healthy non-stub section was 2.97% (NFLX
# business). 2% splits the gap conservatively. The floor must exceed the
# extractor's own 500-char hard minimum to ever fire; 1,000 is still ~9x below
# the smallest healthy real section observed (9,172 chars).
PLAUSIBLE_SECTION_MIN_FRACTION = 0.02
PLAUSIBLE_SECTION_MIN_CHARS = 1_000

PlausibilityCheck = Literal["fraction_of_document", "absolute_floor"]


@dataclass(frozen=True)
class SectionPlausibilityWarning:
    """One suspiciously-short extracted section, flagged at ingest.

    Flag-not-block: the section is still stored and summarized normally; a
    false positive costs one report line, never data. Mirrors the QA layer's
    flag-not-strip posture for reasoning artifacts."""

    section: str
    section_chars: int
    document_chars: int  # length of the document's extracted text
    fraction: float
    checks: tuple[PlausibilityCheck, ...]

    def describe(self) -> str:
        return (
            f"section {self.section!r} is {self.section_chars:,} chars of a"
            f" {self.document_chars:,}-char document ({self.fraction:.2%});"
            f" checks: {', '.join(self.checks)}"
        )


def check_section_plausibility(
    html: str, sections: dict[str, str]
) -> tuple[SectionPlausibilityWarning, ...]:
    """Flag extracted sections that are implausibly short — the signature of
    silent extraction corruption (hijacked slivers, hard truncation). Purely
    observational: storage and summarization proceed regardless."""
    document_chars = max(len(_html_to_text(html)), 1)
    warnings: list[SectionPlausibilityWarning] = []
    for section, section_text in sections.items():
        section_chars = len(section_text)
        fraction = section_chars / document_chars
        checks: list[PlausibilityCheck] = []
        if fraction < PLAUSIBLE_SECTION_MIN_FRACTION:
            checks.append("fraction_of_document")
        if section_chars < PLAUSIBLE_SECTION_MIN_CHARS:
            checks.append("absolute_floor")
        if checks:
            warnings.append(
                SectionPlausibilityWarning(
                    section=section,
                    section_chars=section_chars,
                    document_chars=document_chars,
                    fraction=fraction,
                    checks=tuple(checks),
                )
            )
    return tuple(warnings)


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
    section_warnings: tuple[SectionPlausibilityWarning, ...] = ()


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

    def run(
        self,
        ticker: str,
        accession_number: str | None = None,
        *,
        company: CompanyRef | None = None,
    ) -> ResearchOutcome:
        # `company` carries identity already resolved from sec_ticker_reference
        # (ADR-0010 #3: reference informs ingestion) — batch passes it so
        # companies is populated with the reference's CIK/conformed name.
        # Without it, resolve_ticker is the existing single-ticker fallback.
        try:
            if company is None:
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

        # Flag-not-block: warnings are logged and carried on the outcome, but
        # summarization and storage proceed unchanged.
        section_warnings = check_section_plausibility(html, sections)
        for warning in section_warnings:
            _logger.warning(
                "%s %s: %s", company.ticker, filing.accession_number, warning.describe()
            )

        # ADR-0014 §1/§2/§6: ingest makes ZERO answer-model calls. Each section's
        # source_text lands EAGERLY (retrieval reads it via
        # sections_pending_embedding — load-bearing), with the summary left
        # PENDING (NULL). No thesis is composed here; it is composed lazily at
        # first demand (summarize_on_demand) from the summaries computed then, so
        # the eager answer-model spend is not re-introduced through the thesis.
        with self._engine.begin() as conn:
            repo = Repository(conn)
            company_id = repo.upsert_company(company.ticker, company.cik, company.name)
            filing_id = repo.insert_filing(company_id, filing, content_sha256)
            for section, section_text in sections.items():
                repo.insert_pending_summary(
                    filing_id,
                    section,
                    source_text=section_text,
                    source_url=filing.filing_url,
                    model=self._summarizer.model,
                )

        return ResearchOutcome(
            status="ingested",
            company_id=company_id,
            filing_id=filing_id,
            accession_number=filing.accession_number,
            filing_url=filing.filing_url,
            summaries={},  # nothing computed at ingest (§1); sections are pending
            thesis_snapshot_id=None,  # thesis composed lazily at first demand (§6)
            section_warnings=section_warnings,
        )

    def summarize_on_demand(self, ticker: str) -> ResearchView | None:
        """ADR-0014 §3: the ONE place a summary is computed. On read of the
        summary surface, compute each PENDING section inline via the summarizer,
        cache it in place (UPDATE the existing row — never INSERT, the row already
        exists from the eager source_text write), compose the thesis on first
        demand (append-only), and return the view. A second read is a cache hit
        with zero summarizer calls. Demand is this explicit surface only — never
        QA/FIND/COMPARE/change-detection, which read filing_chunks."""
        with self._engine.connect() as conn:
            latest = Repository(conn).latest_filing(ticker)
        if latest is None:
            return None
        with self._engine.connect() as conn:
            stored = Repository(conn).stored_summaries(latest.filing_id)

        # Compute pending sections BEFORE opening the write transaction, mirroring
        # the ingest/repair posture: a summarizer failure writes nothing.
        computed: dict[str, str] = {}
        for section, entry in stored.items():
            if entry.summary is None:
                try:
                    computed[section] = self._summarizer.summarize(
                        section, entry.source_text, entry.source_url
                    )
                except anthropic.APIError as exc:
                    raise UpstreamError("anthropic", str(exc)) from exc
        if computed:
            with self._engine.begin() as conn:
                repo = Repository(conn)
                for section, summary in computed.items():
                    repo.fill_summary(
                        latest.filing_id, section, summary=summary, model=self._summarizer.model
                    )

        summaries: dict[str, str] = {}
        for section, entry in stored.items():
            value = computed.get(section, entry.summary)
            assert value is not None  # every pending section was just computed above
            summaries[section] = value

        with self._engine.connect() as conn:
            thesis = Repository(conn).latest_thesis_for_filing(latest.company_id, latest.filing_id)
        if thesis is None:
            company = CompanyRef(ticker=latest.ticker, cik=latest.cik, name=latest.company_name)
            filing = FilingRef(
                accession_number=latest.accession_number,
                form_type=latest.form_type,
                filing_date=latest.filing_date,
                period_end_date=latest.period_end_date,
                filing_url=latest.filing_url,
            )
            thesis_content = compose_thesis(company, filing, summaries)
            with self._engine.begin() as conn:
                Repository(conn).insert_thesis_snapshot(
                    latest.company_id, latest.filing_id, thesis_content
                )
            with self._engine.connect() as conn:
                thesis = Repository(conn).latest_thesis_for_filing(
                    latest.company_id, latest.filing_id
                )
            assert thesis is not None  # just inserted

        return ResearchView(
            ticker=latest.ticker,
            company_name=latest.company_name,
            accession_number=latest.accession_number,
            filing_date=latest.filing_date,
            filing_url=latest.filing_url,
            summaries=summaries,
            thesis=thesis[0],
            thesis_created_at=thesis[1],
        )
