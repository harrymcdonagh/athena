"""Batch ingestion over a checked-in ticker list.

Operational wrapper around the existing single-ticker path: each ticker runs
ResearchService.run(), which already skips filings that are stored
(ADR-0008), so reruns are idempotent and only re-attempt what is missing or
previously failed. One ticker's failure never aborts the batch — every ticker
ends in a known, reported state (ingested / skipped / failed-with-reason).

Run with: python -m apps.api.research.batch
Embeddings are not produced here; run the existing backfill afterwards
(python -m apps.api.research.embeddings), matching the single-ticker flow.
"""

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx2 as httpx
from sqlalchemy import Engine, create_engine

from apps.api.config import get_settings
from apps.api.edgar.client import (
    CompanyRef,
    EdgarClient,
    EdgarError,
    FilingNotFoundError,
    TickerNotFoundError,
)
from apps.api.edgar.sections import SectionExtractionError
from apps.api.research.repository import Repository
from apps.api.research.service import ResearchService, SectionPlausibilityWarning
from apps.api.research.summarizer import ClaudeSummarizer

TICKER_LIST_PATH = Path(__file__).resolve().parents[3] / "docs/domain/sp100-tickers.txt"

_logger = logging.getLogger(__name__)

# `unresolved` ("not in the SEC-resolvable universe — a bad curated-list entry")
# is deliberately distinct from `not_found` ("EDGAR had no 10-K for a real
# company"): the first is fixed by editing the list, the second by accepting
# the company has nothing to ingest.
FailureCategory = Literal["unresolved", "not_found", "parse_error", "rate_limited", "other"]


class ReferenceNotPopulatedError(Exception):
    def __init__(self) -> None:
        super().__init__(
            "sec_ticker_reference is empty — run"
            " `python -m apps.api.research.ticker_reference` to download the SEC"
            " mapping before batch ingestion. An empty reference is an operator"
            " error, not a list of bad symbols."
        )


@dataclass(frozen=True)
class TickerResult:
    ticker: str
    status: Literal["ingested", "skipped", "failed"]
    accession_number: str | None = None
    category: FailureCategory | None = None
    reason: str | None = None
    # Section-plausibility warnings are a channel DISTINCT from failure: the
    # filing ingested and is stored; its sections just look suspicious.
    section_warnings: tuple[SectionPlausibilityWarning, ...] = ()


@dataclass(frozen=True)
class BatchReport:
    results: tuple[TickerResult, ...]

    @property
    def attempted(self) -> int:
        return len(self.results)

    @property
    def ingested(self) -> int:
        return sum(1 for r in self.results if r.status == "ingested")

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.status == "skipped")

    @property
    def failures(self) -> tuple[TickerResult, ...]:
        return tuple(r for r in self.results if r.status == "failed")

    @property
    def warned(self) -> tuple[TickerResult, ...]:
        """Results carrying section-plausibility warnings. Orthogonal to
        status: ingested + skipped + failed == attempted always holds."""
        return tuple(r for r in self.results if r.section_warnings)


def parse_ticker_list(text: str) -> list[str]:
    """Tickers from the checked-in list: `#` comments (full-line or trailing)
    and blank lines are ignored; duplicates collapse to the first occurrence."""
    tickers: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        ticker = entry.upper()
        if ticker not in seen:
            seen.add(ticker)
            tickers.append(ticker)
    return tickers


def _is_rate_limited(exc: BaseException | None) -> bool:
    # ResearchService wraps httpx errors in UpstreamError; walk the cause chain
    # so an HTTP 429 is still recognizable after wrapping.
    while exc is not None:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
            return True
        exc = exc.__cause__
    return False


def _categorize(exc: Exception) -> FailureCategory:
    if isinstance(exc, TickerNotFoundError | FilingNotFoundError):
        return "not_found"
    if _is_rate_limited(exc):
        return "rate_limited"
    # EdgarError here means an unexpected EDGAR response shape — the response
    # could not be parsed, same bucket as a filing whose sections won't extract.
    if isinstance(exc, SectionExtractionError | EdgarError):
        return "parse_error"
    return "other"


def run_batch(
    service: ResearchService,
    tickers: Sequence[str],
    *,
    engine: Engine,
    delay_seconds: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
) -> BatchReport:
    """Run the existing single-ticker ingestion for each ticker, skip-and-continue
    on failure, and account for every ticker in the returned report.

    The whole list is validated against sec_ticker_reference UP FRONT
    (ADR-0010 #5): a symbol not in the reference is reported `unresolved` and
    never attempted against EDGAR, so the list's validity is known before any
    EDGAR/summarization time is spent. Resolved symbols are ingested under the
    reference's identity (ADR-0010 #3) — resolution alone still creates no
    companies row; only successful ingestion does.

    A skipped ticker still made its EDGAR lookup before the stored check, so
    the politeness delay applies between every pair of EDGAR-contacting
    tickers regardless of outcome (SEC fair-access; there is no other rate
    limiting in the EDGAR client). Unresolved symbols contact nothing and get
    no delay.
    """
    with engine.connect() as conn:
        repo = Repository(conn)
        if repo.ticker_reference_count() == 0:
            raise ReferenceNotPopulatedError()
        references = {ticker: repo.resolve_ticker_from_reference(ticker) for ticker in tickers}
    results: list[TickerResult] = []
    contacted_edgar = False
    for ticker in tickers:
        reference = references[ticker]
        if reference is None:
            reason = (
                "not in sec_ticker_reference (the SEC-resolvable universe);"
                " fix the curated list entry or refresh the reference"
            )
            _logger.warning("%s failed (unresolved): %s", ticker, reason)
            results.append(
                TickerResult(ticker=ticker, status="failed", category="unresolved", reason=reason)
            )
            continue
        if contacted_edgar and delay_seconds > 0:
            sleep(delay_seconds)
        contacted_edgar = True
        # exchange stays in the reference table for now: companies has no
        # exchange column, and adding one is schema work outside this increment.
        company = CompanyRef(
            ticker=reference.ticker, cik=reference.cik, name=reference.company_name
        )
        try:
            outcome = service.run(ticker, company=company)
        except Exception as exc:
            category = _categorize(exc)
            reason = f"{type(exc).__name__}: {exc}"
            _logger.warning("%s failed (%s): %s", ticker, category, reason)
            results.append(
                TickerResult(ticker=ticker, status="failed", category=category, reason=reason)
            )
            continue
        _logger.info("%s %s (%s)", ticker, outcome.status, outcome.accession_number)
        results.append(
            TickerResult(
                ticker=ticker,
                status=outcome.status,
                accession_number=outcome.accession_number,
                section_warnings=outcome.section_warnings,
            )
        )
    return BatchReport(results=tuple(results))


def format_report(report: BatchReport) -> str:
    lines = [
        f"batch ingest: {report.attempted} attempted — {report.ingested} ingested,"
        f" {report.skipped} skipped (already stored), {len(report.failures)} failed"
    ]
    if report.failures:
        lines.append("failed tickers:")
        lines += [f"  {r.ticker}: {r.category} — {r.reason}" for r in report.failures]
    else:
        lines.append("no failures; every ticker ingested or already stored")
    if report.warned:
        lines.append("section-plausibility warnings (ingested and stored; review advised):")
        lines += [
            f"  {r.ticker} ({r.accession_number}): {w.describe()}"
            for r in report.warned
            for w in r.section_warnings
        ]
    return "\n".join(lines)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    if not settings.sec_edgar_user_agent:
        raise SystemExit("SEC_EDGAR_USER_AGENT is not set; aborting without contacting EDGAR.")
    if not settings.anthropic_api_key:
        raise SystemExit("ANTHROPIC_API_KEY is not set; aborting without ingesting anything.")
    tickers = parse_ticker_list(TICKER_LIST_PATH.read_text(encoding="utf-8"))
    engine = create_engine(settings.database_url)
    service = ResearchService(
        edgar=EdgarClient(user_agent=settings.sec_edgar_user_agent),
        summarizer=ClaudeSummarizer(api_key=settings.anthropic_api_key),
        engine=engine,
    )
    try:
        report = run_batch(
            service, tickers, engine=engine, delay_seconds=settings.edgar_batch_delay_seconds
        )
    except ReferenceNotPopulatedError as exc:
        raise SystemExit(str(exc)) from exc
    print(format_report(report))
    print("reminder: run `python -m apps.api.research.embeddings` to embed new sections")
    if report.failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
