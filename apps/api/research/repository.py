from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import Connection, text

from apps.api.edgar.client import FilingRef


@dataclass(frozen=True)
class ResearchView:
    ticker: str
    company_name: str
    accession_number: str
    filing_date: date
    filing_url: str
    summaries: dict[str, str]
    thesis: str
    thesis_created_at: datetime


class Repository:
    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def upsert_company(self, ticker: str, cik: str, name: str) -> int:
        result: int = self._conn.execute(
            text(
                "INSERT INTO companies (ticker, cik, name) VALUES (:ticker, :cik, :name)"
                " ON CONFLICT (cik) DO UPDATE"
                " SET ticker = EXCLUDED.ticker, name = EXCLUDED.name"
                " RETURNING id"
            ),
            {"ticker": ticker, "cik": cik, "name": name},
        ).scalar_one()
        return result

    def find_filing_id(self, accession_number: str) -> int | None:
        result: int | None = self._conn.execute(
            text("SELECT id FROM filings WHERE accession_number = :acc"),
            {"acc": accession_number},
        ).scalar()
        return result

    def insert_filing(self, company_id: int, filing: FilingRef, content_sha256: str) -> int:
        result: int = self._conn.execute(
            text(
                "INSERT INTO filings (company_id, accession_number, form_type, filing_date,"
                " period_end_date, filing_url, content_sha256)"
                " VALUES (:company_id, :acc, :form, :filed, :period, :url, :sha)"
                " RETURNING id"
            ),
            {
                "company_id": company_id,
                "acc": filing.accession_number,
                "form": filing.form_type,
                "filed": filing.filing_date,
                "period": filing.period_end_date,
                "url": filing.filing_url,
                "sha": content_sha256,
            },
        ).scalar_one()
        return result

    def insert_summary(
        self,
        filing_id: int,
        section: str,
        summary: str,
        source_text: str,
        source_url: str,
        model: str,
    ) -> int:
        result: int = self._conn.execute(
            text(
                "INSERT INTO filing_summaries"
                " (filing_id, section, summary, source_text, source_url, model)"
                " VALUES (:filing_id, :section, :summary, :source_text, :source_url, :model)"
                " RETURNING id"
            ),
            {
                "filing_id": filing_id,
                "section": section,
                "summary": summary,
                "source_text": source_text,
                "source_url": source_url,
                "model": model,
            },
        ).scalar_one()
        return result

    def insert_thesis_snapshot(self, company_id: int, filing_id: int, content: str) -> int:
        result: int = self._conn.execute(
            text(
                "INSERT INTO thesis_snapshots (company_id, content, source_filing_id)"
                " VALUES (:company_id, :content, :filing_id) RETURNING id"
            ),
            {"company_id": company_id, "content": content, "filing_id": filing_id},
        ).scalar_one()
        return result

    def latest_research(self, ticker: str) -> ResearchView | None:
        row = self._conn.execute(
            text(
                "SELECT c.ticker, c.name, f.id AS filing_id, f.accession_number,"
                " f.filing_date, f.filing_url, t.content, t.created_at"
                " FROM companies c"
                " JOIN thesis_snapshots t ON t.company_id = c.id"
                " JOIN filings f ON f.id = t.source_filing_id"
                " WHERE upper(c.ticker) = upper(:ticker)"
                " ORDER BY t.created_at DESC, t.id DESC LIMIT 1"
            ),
            {"ticker": ticker},
        ).one_or_none()
        if row is None:
            return None
        summaries = {
            section: summary
            for section, summary in self._conn.execute(
                text(
                    "SELECT section, summary FROM filing_summaries"
                    " WHERE filing_id = :filing_id ORDER BY section"
                ),
                {"filing_id": row.filing_id},
            )
        }
        return ResearchView(
            ticker=row.ticker,
            company_name=row.name,
            accession_number=row.accession_number,
            filing_date=row.filing_date,
            filing_url=row.filing_url,
            summaries=summaries,
            thesis=row.content,
            thesis_created_at=row.created_at,
        )
