from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from sqlalchemy import Connection, text

from apps.api.edgar.client import FilingRef


class ChunkLike(Protocol):
    """Structural type for embedded chunks (see apps.api.research.embeddings)."""

    @property
    def text(self) -> str: ...

    @property
    def embedding(self) -> list[float]: ...


@dataclass(frozen=True)
class PendingSection:
    filing_id: int
    section: str
    source_text: str
    source_url: str


@dataclass(frozen=True)
class ChunkMatch:
    ticker: str
    filing_id: int
    section: str
    chunk_index: int
    content: str
    source_url: str
    distance: float


def _vector_literal(embedding: Sequence[float]) -> str:
    return "[" + ",".join(map(str, embedding)) + "]"


@dataclass(frozen=True)
class StoredFiling:
    id: int
    company_id: int


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

    def find_filing(self, accession_number: str) -> StoredFiling | None:
        row = self._conn.execute(
            text("SELECT id, company_id FROM filings WHERE accession_number = :acc"),
            {"acc": accession_number},
        ).one_or_none()
        if row is None:
            return None
        return StoredFiling(id=row.id, company_id=row.company_id)

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

    def sections_pending_embedding(self, model: str) -> list[PendingSection]:
        rows = self._conn.execute(
            text(
                "SELECT s.filing_id, s.section, s.source_text, s.source_url"
                " FROM filing_summaries s"
                " WHERE NOT EXISTS ("
                "   SELECT 1 FROM filing_chunks c"
                "   WHERE c.filing_id = s.filing_id AND c.section = s.section"
                "     AND c.model = :model)"
                " ORDER BY s.filing_id, s.section"
            ),
            {"model": model},
        ).all()
        return [
            PendingSection(
                filing_id=row.filing_id,
                section=row.section,
                source_text=row.source_text,
                source_url=row.source_url,
            )
            for row in rows
        ]

    def replace_chunks(
        self,
        filing_id: int,
        section: str,
        source_url: str,
        chunks: Sequence[ChunkLike],
        *,
        model: str,
        dimension: int,
    ) -> int:
        self._conn.execute(
            text("DELETE FROM filing_chunks WHERE filing_id = :filing_id AND section = :section"),
            {"filing_id": filing_id, "section": section},
        )
        for index, chunk in enumerate(chunks):
            self._conn.execute(
                text(
                    "INSERT INTO filing_chunks (filing_id, section, source_url, chunk_index,"
                    " content, embedding, model, dimension)"
                    " VALUES (:filing_id, :section, :source_url, :chunk_index, :content,"
                    " CAST(:embedding AS vector), :model, :dimension)"
                ),
                {
                    "filing_id": filing_id,
                    "section": section,
                    "source_url": source_url,
                    "chunk_index": index,
                    "content": chunk.text,
                    "embedding": _vector_literal(chunk.embedding),
                    "model": model,
                    "dimension": dimension,
                },
            )
        return len(chunks)

    def search_chunks(
        self,
        query_embedding: Sequence[float],
        *,
        model: str,
        limit: int = 8,
        ticker: str | None = None,
        section: str | None = None,
    ) -> list[ChunkMatch]:
        filters = ""
        params: dict[str, object] = {
            "query": _vector_literal(query_embedding),
            "model": model,
            "limit": limit,
        }
        if ticker is not None:
            filters += " AND upper(co.ticker) = upper(:ticker)"
            params["ticker"] = ticker
        if section is not None:
            filters += " AND fc.section = :section"
            params["section"] = section
        rows = self._conn.execute(
            text(
                "SELECT co.ticker, fc.filing_id, fc.section, fc.chunk_index, fc.content,"
                " fc.source_url, fc.embedding <=> CAST(:query AS vector) AS distance"
                " FROM filing_chunks fc"
                " JOIN filings f ON f.id = fc.filing_id"
                " JOIN companies co ON co.id = f.company_id"
                " WHERE fc.model = :model"
                + filters
                + " ORDER BY fc.embedding <=> CAST(:query AS vector)"
                " LIMIT :limit"
            ),
            params,
        ).all()
        return [
            ChunkMatch(
                ticker=row.ticker,
                filing_id=row.filing_id,
                section=row.section,
                chunk_index=row.chunk_index,
                content=row.content,
                source_url=row.source_url,
                distance=row.distance,
            )
            for row in rows
        ]

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
                # ADR-0008 §1/§4: "latest" follows the filing-period ordering, not
                # ingestion order, so backfilling an older filing can't hijack it.
                " ORDER BY f.period_end_date DESC, f.filing_date DESC,"
                " f.accession_number DESC, t.created_at DESC, t.id DESC LIMIT 1"
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
