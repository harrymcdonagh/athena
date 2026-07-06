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
from sqlalchemy import create_engine

from apps.api.config import get_settings
from apps.api.edgar.client import (
    EdgarClient,
    EdgarError,
    FilingNotFoundError,
    TickerNotFoundError,
)
from apps.api.edgar.sections import SectionExtractionError
from apps.api.research.service import ResearchService
from apps.api.research.summarizer import ClaudeSummarizer

TICKER_LIST_PATH = Path(__file__).resolve().parents[3] / "docs/domain/sp100-tickers.txt"

_logger = logging.getLogger(__name__)

FailureCategory = Literal["not_found", "parse_error", "rate_limited", "other"]


@dataclass(frozen=True)
class TickerResult:
    ticker: str
    status: Literal["ingested", "skipped", "failed"]
    accession_number: str | None = None
    category: FailureCategory | None = None
    reason: str | None = None


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
    delay_seconds: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
) -> BatchReport:
    """Run the existing single-ticker ingestion for each ticker, skip-and-continue
    on failure, and account for every ticker in the returned report.

    A skipped ticker still made its EDGAR lookups before the stored check, so
    the politeness delay applies between every pair of companies regardless of
    the previous outcome (SEC fair-access; there is no other rate limiting in
    the EDGAR client).
    """
    results: list[TickerResult] = []
    for i, ticker in enumerate(tickers):
        if i and delay_seconds > 0:
            sleep(delay_seconds)
        try:
            outcome = service.run(ticker)
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
    return "\n".join(lines)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    if not settings.sec_edgar_user_agent:
        raise SystemExit("SEC_EDGAR_USER_AGENT is not set; aborting without contacting EDGAR.")
    if not settings.anthropic_api_key:
        raise SystemExit("ANTHROPIC_API_KEY is not set; aborting without ingesting anything.")
    tickers = parse_ticker_list(TICKER_LIST_PATH.read_text(encoding="utf-8"))
    service = ResearchService(
        edgar=EdgarClient(user_agent=settings.sec_edgar_user_agent),
        summarizer=ClaudeSummarizer(api_key=settings.anthropic_api_key),
        engine=create_engine(settings.database_url),
    )
    report = run_batch(service, tickers, delay_seconds=settings.edgar_batch_delay_seconds)
    print(format_report(report))
    print("reminder: run `python -m apps.api.research.embeddings` to embed new sections")
    if report.failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
