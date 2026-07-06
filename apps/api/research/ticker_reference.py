"""Download/refresh for the sec_ticker_reference external cache (ADR-0010).

The table is a CACHE of the SEC's ticker→identity mapping — evidence-layer
reference data, not evidence (ADR-0010 #2). A refresh writes ONLY
sec_ticker_reference, never companies/filings (ADR-0010 #4), and is an
explicit on-demand operation, not a background job.

Run with: python -m apps.api.research.ticker_reference
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import Engine, bindparam, create_engine, text

from apps.api.config import get_settings
from apps.api.edgar.client import EdgarClient, TickerReferenceRow

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RefreshReport:
    snapshot_rows: int
    added: int
    updated: int
    removed: int
    unchanged: int


def refresh(engine: Engine, fetch: Callable[[], Sequence[TickerReferenceRow]]) -> RefreshReport:
    """Reconcile sec_ticker_reference to the fetched SEC snapshot, all-or-nothing.

    The SEC file is the entire universe each time, so the table must converge
    on it in both directions: rows are upserted, and tickers no longer in the
    file (delistings/deregistrations) are deleted — otherwise the table stops
    being authoritative for resolving NOW (ADR-0010 #4).

    Safety: fetch (network + shape validation) completes BEFORE the write
    transaction opens, so a download/parse failure writes nothing; the
    reconcile itself is one transaction, so a mid-refresh failure rolls back
    to the last-good table. At ~10k rows a delete+upsert diff inside a single
    transaction is simpler than stage-and-swap and never exposes a half-empty
    table.

    refreshed_at is bumped for every row present in the snapshot on every
    successful refresh — it means "last confirmed against the SEC file", the
    per-row staleness signal — while added/updated/unchanged count identity
    DATA changes only, so a rerun on an unchanged file reports 0/0/0.
    """
    snapshot = list(fetch())
    by_ticker = {row.ticker: row for row in snapshot}
    with engine.begin() as conn:
        existing = {
            row.ticker: (row.cik, row.company_name, row.exchange)
            for row in conn.execute(
                text("SELECT ticker, cik, company_name, exchange FROM sec_ticker_reference")
            )
        }
        departed = sorted(set(existing) - set(by_ticker))
        if departed:
            conn.execute(
                text("DELETE FROM sec_ticker_reference WHERE ticker IN :tickers").bindparams(
                    bindparam("tickers", expanding=True)
                ),
                {"tickers": departed},
            )
        conn.execute(
            text(
                "INSERT INTO sec_ticker_reference (ticker, cik, company_name, exchange)"
                " VALUES (:ticker, :cik, :company_name, :exchange)"
                " ON CONFLICT (ticker) DO UPDATE SET cik = EXCLUDED.cik,"
                " company_name = EXCLUDED.company_name, exchange = EXCLUDED.exchange,"
                " refreshed_at = now()"
            ),
            [vars(row) for row in snapshot],
        )
    added = sum(1 for ticker in by_ticker if ticker not in existing)
    unchanged = sum(
        1
        for ticker, row in by_ticker.items()
        if existing.get(ticker) == (row.cik, row.company_name, row.exchange)
    )
    report = RefreshReport(
        snapshot_rows=len(snapshot),
        added=added,
        updated=len(snapshot) - added - unchanged,
        removed=len(departed),
        unchanged=unchanged,
    )
    _logger.info(
        "sec_ticker_reference refreshed: %d in snapshot, %d added, %d updated,"
        " %d removed, %d unchanged",
        report.snapshot_rows,
        report.added,
        report.updated,
        report.removed,
        report.unchanged,
    )
    return report


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    if not settings.sec_edgar_user_agent:
        raise SystemExit("SEC_EDGAR_USER_AGENT is not set; aborting without contacting EDGAR.")
    engine = create_engine(settings.database_url)
    # One file download per refresh; the client's User-Agent / fair-access
    # handling is the only rate limiting a single request needs.
    client = EdgarClient(user_agent=settings.sec_edgar_user_agent)
    report = refresh(engine, client.fetch_ticker_reference)
    print(
        f"sec_ticker_reference: {report.snapshot_rows} in snapshot —"
        f" {report.added} added, {report.updated} updated,"
        f" {report.removed} removed, {report.unchanged} unchanged"
    )


if __name__ == "__main__":
    main()
