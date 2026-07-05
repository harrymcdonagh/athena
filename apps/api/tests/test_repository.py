from datetime import date

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from apps.api.edgar.client import FilingRef
from apps.api.research.repository import Repository, StoredFiling

FILING = FilingRef(
    accession_number="0000320193-25-000123",
    form_type="10-K",
    filing_date=date(2025, 11, 1),
    period_end_date=date(2025, 9, 27),
    filing_url="https://www.sec.gov/Archives/edgar/data/320193/000032019325000123/aapl-10k.htm",
)

PRIOR_FILING = FilingRef(
    accession_number="0000320193-24-000100",
    form_type="10-K",
    filing_date=date(2024, 11, 1),
    period_end_date=date(2024, 9, 28),
    filing_url="https://www.sec.gov/Archives/edgar/data/320193/000032019324000100/old-10k.htm",
)


def test_upsert_company_is_idempotent(db: Engine) -> None:
    with db.begin() as conn:
        repo = Repository(conn)
        first = repo.upsert_company("AAPL", "0000320193", "Apple Inc.")
        second = repo.upsert_company("AAPL", "0000320193", "Apple Inc. (renamed)")
    assert first == second


def test_full_round_trip(db: Engine) -> None:
    with db.begin() as conn:
        repo = Repository(conn)
        company_id = repo.upsert_company("AAPL", "0000320193", "Apple Inc.")
        filing_id = repo.insert_filing(company_id, FILING, content_sha256="abc123")
        repo.insert_summary(
            filing_id,
            "business",
            "It sells widgets. Source: url",
            "raw text",
            FILING.filing_url,
            "claude-sonnet-5",
        )
        repo.insert_thesis_snapshot(company_id, filing_id, "# Thesis\ncontent")

    with db.connect() as conn:
        view = Repository(conn).latest_research("AAPL")
    assert view is not None
    assert view.company_name == "Apple Inc."
    assert view.accession_number == FILING.accession_number
    assert view.summaries == {"business": "It sells widgets. Source: url"}
    assert view.thesis.startswith("# Thesis")
    assert view.filing_url == FILING.filing_url


def test_find_filing(db: Engine) -> None:
    with db.begin() as conn:
        repo = Repository(conn)
        company_id = repo.upsert_company("AAPL", "0000320193", "Apple Inc.")
        filing_id = repo.insert_filing(company_id, FILING, content_sha256="abc123")
        assert repo.find_filing(FILING.accession_number) == StoredFiling(
            id=filing_id, company_id=company_id
        )
        assert repo.find_filing("nope") is None


def test_latest_research_orders_by_period_not_insertion(db: Engine) -> None:
    """ADR-0008 §4: backfilling an older filing later must not hijack "latest".

    The newer-period thesis stays latest even though the older-period filing
    and its snapshot were inserted afterwards (greater created_at).
    """
    with db.begin() as conn:
        repo = Repository(conn)
        company_id = repo.upsert_company("AAPL", "0000320193", "Apple Inc.")
        newer_id = repo.insert_filing(company_id, FILING, content_sha256="abc123")
        repo.insert_thesis_snapshot(company_id, newer_id, "# Thesis FY2025")
    with db.begin() as conn:
        repo = Repository(conn)
        older_id = repo.insert_filing(company_id, PRIOR_FILING, content_sha256="def456")
        repo.insert_thesis_snapshot(company_id, older_id, "# Thesis FY2024")

    with db.connect() as conn:
        view = Repository(conn).latest_research("AAPL")
    assert view is not None
    assert view.accession_number == FILING.accession_number
    assert view.thesis == "# Thesis FY2025"


def test_duplicate_section_summary_rejected(db: Engine) -> None:
    with db.begin() as conn:
        repo = Repository(conn)
        company_id = repo.upsert_company("AAPL", "0000320193", "Apple Inc.")
        filing_id = repo.insert_filing(company_id, FILING, content_sha256="abc123")
        repo.insert_summary(filing_id, "business", "s1", "t", "u", "m")
        with pytest.raises(IntegrityError):
            repo.insert_summary(filing_id, "business", "s2", "t", "u", "m")


def test_latest_research_returns_none_for_unknown_ticker(db: Engine) -> None:
    with db.connect() as conn:
        assert Repository(conn).latest_research("ZZZZ") is None
