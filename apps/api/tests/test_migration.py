import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError


def test_all_tables_exist(db: Engine) -> None:
    with db.connect() as conn:
        rows = (
            conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
            .scalars()
            .all()
        )
    for table in ("companies", "filings", "filing_summaries", "thesis_snapshots"):
        assert table in rows


def test_pgvector_extension_enabled(db: Engine) -> None:
    with db.connect() as conn:
        found = conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")).scalar()
    assert found == 1


def _seed_snapshot(conn: Connection) -> None:
    company_id = conn.execute(
        text(
            "INSERT INTO companies (ticker, cik, name)"
            " VALUES ('T', '0000000001', 'T Inc') RETURNING id"
        )
    ).scalar_one()
    filing_id = conn.execute(
        text(
            "INSERT INTO filings (company_id, accession_number, form_type, filing_date,"
            " period_end_date, filing_url, content_sha256)"
            " VALUES (:c, 'acc-1', '10-K', '2026-01-01', '2025-12-31', 'https://example.com',"
            " 'sha') RETURNING id"
        ),
        {"c": company_id},
    ).scalar_one()
    conn.execute(
        text(
            "INSERT INTO thesis_snapshots (company_id, content, source_filing_id)"
            " VALUES (:c, 'thesis', :f)"
        ),
        {"c": company_id, "f": filing_id},
    )


def test_thesis_snapshots_reject_update(db: Engine) -> None:
    with db.begin() as conn:
        _seed_snapshot(conn)
    # separate transaction: the raised exception aborts it, and pytest.raises
    # must wrap the begin() block so rollback (not commit) runs on exit
    with pytest.raises(DBAPIError, match="append-only"), db.begin() as conn:
        conn.execute(text("UPDATE thesis_snapshots SET content = 'edited'"))


def test_thesis_snapshots_reject_delete(db: Engine) -> None:
    with db.begin() as conn:
        _seed_snapshot(conn)
    with pytest.raises(DBAPIError, match="append-only"), db.begin() as conn:
        conn.execute(text("DELETE FROM thesis_snapshots"))
