import json
from typing import cast

import httpx2 as httpx
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from apps.api.edgar.client import EdgarClient, EdgarError, TickerReferenceRow
from apps.api.research.ticker_reference import refresh
from apps.api.tests.conftest import TEST_DATABASE_URL
from apps.api.tests.test_edgar_client import make_client
from apps.api.tests.test_service import count

# Real shape of company_tickers_exchange.json (verified 2026-07-06): columnar
# fields/data, integer CIKs, nullable exchange, share classes as separate rows
# on one CIK (GOOG/GOOGL). "brk-b" is lowercase here only to pin uppercasing;
# the real file is already uppercase.
EXCHANGE_PAYLOAD = {
    "fields": ["cik", "name", "ticker", "exchange"],
    "data": [
        [320193, "Apple Inc.", "AAPL", "Nasdaq"],
        [1652044, "Alphabet Inc.", "GOOGL", "Nasdaq"],
        [1652044, "Alphabet Inc.", "GOOG", "Nasdaq"],
        [1067983, "BERKSHIRE HATHAWAY INC", "brk-b", None],
    ],
}

AAPL = TickerReferenceRow(
    ticker="AAPL", cik="0000320193", company_name="Apple Inc.", exchange="Nasdaq"
)
GOOGL = TickerReferenceRow(
    ticker="GOOGL", cik="0001652044", company_name="Alphabet Inc.", exchange="Nasdaq"
)
GOOG = TickerReferenceRow(
    ticker="GOOG", cik="0001652044", company_name="Alphabet Inc.", exchange="Nasdaq"
)
BRK_B = TickerReferenceRow(
    ticker="BRK-B", cik="0001067983", company_name="BERKSHIRE HATHAWAY INC", exchange=None
)


def exchange_client(payload: object) -> EdgarClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("company_tickers_exchange.json"):
            return httpx.Response(200, text=json.dumps(payload))
        return httpx.Response(404)

    return make_client(handler)


def all_rows(db: Engine) -> dict[str, tuple[str, str, str | None]]:
    with db.connect() as conn:
        return {
            row.ticker: (row.cik, row.company_name, row.exchange)
            for row in conn.execute(
                text("SELECT ticker, cik, company_name, exchange FROM sec_ticker_reference")
            )
        }


# --- migration ---


def test_migration_downgrade_and_upgrade_cycle(db: Engine) -> None:
    cfg = AlembicConfig("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)

    command.downgrade(cfg, "0003")
    with db.connect() as conn:
        gone = conn.execute(
            text("SELECT 1 FROM pg_tables WHERE tablename = 'sec_ticker_reference'")
        ).scalar()
    assert gone is None

    command.upgrade(cfg, "head")
    with db.connect() as conn:
        back = conn.execute(
            text("SELECT 1 FROM pg_tables WHERE tablename = 'sec_ticker_reference'")
        ).scalar()
    assert back == 1


def test_table_has_only_adr_0010_fields(db: Engine) -> None:
    with db.connect() as conn:
        columns = set(
            conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'sec_ticker_reference'"
                )
            ).scalars()
        )
    # ADR-0010 #6: SEC-authoritative identity fields only, plus the staleness
    # timestamp — no sector/industry/price/market-cap columns may appear here.
    assert columns == {"ticker", "cik", "company_name", "exchange", "refreshed_at"}


def test_ticker_is_primary_key_and_cik_is_indexed_not_unique(db: Engine) -> None:
    with db.connect() as conn:
        indexes = {
            row.indexname: row.indexdef
            for row in conn.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes"
                    " WHERE tablename = 'sec_ticker_reference'"
                )
            )
        }
    assert any("(ticker)" in d and "UNIQUE" in d for d in indexes.values())
    cik_indexes = [d for d in indexes.values() if "(cik)" in d]
    assert cik_indexes and all("UNIQUE" not in d for d in cik_indexes)


# --- refresh ---


def test_refresh_populates_from_sec_payload(db: Engine) -> None:
    client = exchange_client(EXCHANGE_PAYLOAD)

    report = refresh(db, client.fetch_ticker_reference)

    assert report.snapshot_rows == 4
    assert report.added == 4
    assert (report.updated, report.removed, report.unchanged) == (0, 0, 0)
    rows = all_rows(db)
    # CIK stored zero-padded (canonical form shared with companies.cik);
    # tickers uppercased; nullable exchange preserved.
    assert rows["AAPL"] == ("0000320193", "Apple Inc.", "Nasdaq")
    assert rows["BRK-B"] == ("0001067983", "BERKSHIRE HATHAWAY INC", None)
    # Share classes: two rows, one CIK.
    assert rows["GOOG"][0] == rows["GOOGL"][0] == "0001652044"


def test_refresh_is_idempotent_on_unchanged_snapshot(db: Engine) -> None:
    fetch = lambda: [AAPL, GOOGL, GOOG]  # noqa: E731
    refresh(db, fetch)

    second = refresh(db, fetch)

    assert (second.added, second.updated, second.removed) == (0, 0, 0)
    assert second.unchanged == 3
    assert set(all_rows(db)) == {"AAPL", "GOOGL", "GOOG"}


def test_refresh_removes_delisted_tickers(db: Engine) -> None:
    refresh(db, lambda: [AAPL, BRK_B])

    report = refresh(db, lambda: [AAPL])

    assert report.removed == 1
    assert set(all_rows(db)) == {"AAPL"}  # BRK-B delisted → stops resolving


def test_refresh_reports_updated_identity_data(db: Engine) -> None:
    refresh(db, lambda: [AAPL])
    renamed = TickerReferenceRow(
        ticker="AAPL", cik="0000320193", company_name="Apple Computer Inc.", exchange="Nasdaq"
    )

    report = refresh(db, lambda: [renamed])

    assert (report.added, report.updated, report.removed, report.unchanged) == (0, 1, 0, 0)
    assert all_rows(db)["AAPL"][1] == "Apple Computer Inc."


def test_failed_fetch_leaves_last_good_table_intact(db: Engine) -> None:
    refresh(db, lambda: [AAPL, GOOG])
    before = all_rows(db)

    def failing_fetch() -> list[TickerReferenceRow]:
        raise EdgarError("unexpected EDGAR response shape: boom")

    with pytest.raises(EdgarError):
        refresh(db, failing_fetch)
    assert all_rows(db) == before


def test_mid_transaction_failure_rolls_back_to_last_good_table(db: Engine) -> None:
    refresh(db, lambda: [AAPL, GOOG])
    before = all_rows(db)
    # A NULL cik violates NOT NULL after the transaction has already deleted
    # GOOG — the rollback must restore it (all-or-nothing reconcile).
    poisoned = TickerReferenceRow(
        ticker="BAD", cik=cast(str, None), company_name="Bad Co", exchange=None
    )

    with pytest.raises(DBAPIError):
        refresh(db, lambda: [AAPL, poisoned])
    assert all_rows(db) == before


@pytest.mark.parametrize(
    "payload",
    [
        {"unexpected": "shape"},
        {"fields": ["cik", "name"], "data": [[1, "X"]]},  # required columns missing
        {"fields": ["cik", "name", "ticker", "exchange"], "data": [[320193, "Apple Inc."]]},
        {
            "fields": ["cik", "name", "ticker", "exchange"],
            "data": [["320193", "Apple Inc.", "AAPL", "Nasdaq"]],  # cik as string
        },
        {
            "fields": ["cik", "name", "ticker", "exchange"],
            "data": [[1, "A", "AAPL", None], [2, "B", "AAPL", None]],  # duplicate ticker
        },
        {"fields": ["cik", "name", "ticker", "exchange"], "data": []},  # empty universe
    ],
)
def test_malformed_payload_is_a_hard_error_and_writes_nothing(db: Engine, payload: object) -> None:
    client = exchange_client(payload)
    with pytest.raises(EdgarError, match="unexpected EDGAR response shape"):
        refresh(db, client.fetch_ticker_reference)
    assert all_rows(db) == {}  # no silent partial load


def test_refresh_writes_only_the_reference_table(db: Engine) -> None:
    # ADR-0010 #4: refresh never touches evidence tables. The code path only
    # ever names sec_ticker_reference; this pins it against regression.
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO companies (ticker, cik, name)"
                " VALUES ('AAPL', '0000320193', 'Apple Inc. (as ingested)')"
            )
        )
    renamed = TickerReferenceRow(
        ticker="AAPL", cik="0000320193", company_name="Apple Inc. (renamed)", exchange="Nasdaq"
    )

    refresh(db, lambda: [renamed])

    assert count(db, "companies") == 1
    assert count(db, "filings") == 0
    with db.connect() as conn:
        pinned = conn.execute(text("SELECT name FROM companies WHERE ticker = 'AAPL'")).scalar_one()
    # Reference and pinned evidence now disagree — the accepted ADR-0010 #4
    # state: the cache resolves NOW, companies records what was ingested THEN.
    assert pinned == "Apple Inc. (as ingested)"
    assert all_rows(db)["AAPL"][1] == "Apple Inc. (renamed)"
