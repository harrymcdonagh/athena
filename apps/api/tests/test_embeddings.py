import logging
from collections import Counter
from types import SimpleNamespace

import pytest
import voyageai
from sqlalchemy import Engine, text

from apps.api.research.embeddings import (
    EmbeddedChunk,
    Embedder,
    EmbeddingError,
    VoyageEmbedder,
    balanced_semantic_search,
    run_backfill,
    semantic_search,
)
from apps.api.research.repository import ChunkMatch, Repository

# --- VoyageEmbedder unit tests (fake Voyage client, no network) ---


class FakeVoyageClient:
    """Mimics voyageai.Client.contextualized_embed's response shape:
    results[0].embeddings and results[0].chunk_texts are parallel lists."""

    def __init__(
        self,
        embeddings: list[list[float]],
        chunk_texts: list[str] | None,
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self._embeddings = embeddings
        self._chunk_texts = chunk_texts

    def contextualized_embed(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(
            results=[SimpleNamespace(embeddings=self._embeddings, chunk_texts=self._chunk_texts)],
            chunker_version="fake-chunker-1",
        )


def make_embedder(
    monkeypatch: pytest.MonkeyPatch,
    embeddings: list[list[float]],
    chunk_texts: list[str] | None,
) -> tuple[VoyageEmbedder, FakeVoyageClient]:
    fake = FakeVoyageClient(embeddings, chunk_texts)
    monkeypatch.setattr(voyageai, "Client", lambda api_key: fake)
    return VoyageEmbedder(api_key="test"), fake


def test_embed_document_requests_server_side_contextualized_chunking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedder, fake = make_embedder(monkeypatch, [[0.1] * 1024], ["chunk a"])
    embedder.embed_document("full section text")
    call = fake.calls[0]
    # Auto-chunking requires a FLAT list of full-document strings. The nested
    # [[text]] form is an invalid combination with enable_auto_chunking per
    # Voyage's docs, and can silently embed the whole section as ONE chunk.
    assert call["inputs"] == ["full section text"]
    assert call["model"] == "voyage-context-4"
    assert call["input_type"] == "document"
    assert call["output_dimension"] == 1024
    assert call["enable_auto_chunking"] is True


def test_embed_document_logs_voyage_chunker_version(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    embedder, _ = make_embedder(monkeypatch, [[0.1] * 1024], ["chunk a"])
    with caplog.at_level(logging.INFO, logger="apps.api.research.embeddings"):
        embedder.embed_document("section")
    assert "fake-chunker-1" in caplog.text


def test_embed_document_pairs_chunk_texts_with_embeddings_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedder, _ = make_embedder(monkeypatch, [[0.1] * 1024, [0.2] * 1024], ["chunk a", "chunk b"])
    chunks = embedder.embed_document("section")
    assert [c.text for c in chunks] == ["chunk a", "chunk b"]
    assert chunks[0].embedding == [0.1] * 1024
    assert chunks[1].embedding == [0.2] * 1024


def test_embed_document_rejects_wrong_dimension(monkeypatch: pytest.MonkeyPatch) -> None:
    embedder, _ = make_embedder(monkeypatch, [[0.1] * 512], ["chunk a"])
    with pytest.raises(EmbeddingError, match="dimension"):
        embedder.embed_document("section")


def test_embed_document_rejects_empty_chunk_list(monkeypatch: pytest.MonkeyPatch) -> None:
    embedder, _ = make_embedder(monkeypatch, [], [])
    with pytest.raises(EmbeddingError, match="no chunks"):
        embedder.embed_document("section")


def test_embed_document_rejects_missing_chunk_texts(monkeypatch: pytest.MonkeyPatch) -> None:
    embedder, _ = make_embedder(monkeypatch, [[0.1] * 1024], None)
    with pytest.raises(EmbeddingError, match="chunk texts"):
        embedder.embed_document("section")


def test_embed_document_rejects_mismatched_chunk_texts(monkeypatch: pytest.MonkeyPatch) -> None:
    embedder, _ = make_embedder(monkeypatch, [[0.1] * 1024, [0.2] * 1024], ["only one"])
    with pytest.raises(EmbeddingError, match="chunk texts"):
        embedder.embed_document("section")


def test_embed_query_uses_query_input_type_without_auto_chunking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedder, fake = make_embedder(monkeypatch, [[0.3] * 1024], None)
    vector = embedder.embed_query("what are the risks")
    call = fake.calls[0]
    assert call["inputs"] == [["what are the risks"]]
    assert call["input_type"] == "query"
    assert call["output_dimension"] == 1024
    assert "enable_auto_chunking" not in call
    assert vector == [0.3] * 1024


def test_embed_query_rejects_wrong_dimension(monkeypatch: pytest.MonkeyPatch) -> None:
    embedder, _ = make_embedder(monkeypatch, [[0.3] * 2048], None)
    with pytest.raises(EmbeddingError, match="dimension"):
        embedder.embed_query("q")


def test_voyage_embedder_satisfies_protocol() -> None:
    embedder: Embedder = VoyageEmbedder(api_key="test")
    assert embedder.model == "voyage-context-4"
    assert embedder.dimension == 1024


# --- Repository.replace_chunks (real test database) ---


def seed_filing(
    db: Engine,
    accession: str = "0000320193-25-000001",
    ticker: str = "AAPL",
    cik: str = "0000320193",
    name: str = "Apple Inc.",
    filing_date: str = "2025-10-31",
    period_end_date: str = "2025-09-27",
    filing_url: str = "https://sec.gov/filing.htm",
) -> int:
    with db.begin() as conn:
        company_id = conn.execute(
            text(
                "INSERT INTO companies (ticker, cik, name)"
                " VALUES (:ticker, :cik, :name)"
                " ON CONFLICT (cik) DO UPDATE SET name = EXCLUDED.name RETURNING id"
            ),
            {"ticker": ticker, "cik": cik, "name": name},
        ).scalar_one()
        filing_id: int = conn.execute(
            text(
                "INSERT INTO filings (company_id, accession_number, form_type, filing_date,"
                " period_end_date, filing_url, content_sha256)"
                " VALUES (:company_id, :acc, '10-K', :filed, :period, :url, 'abc123')"
                " RETURNING id"
            ),
            {
                "company_id": company_id,
                "acc": accession,
                "filed": filing_date,
                "period": period_end_date,
                "url": filing_url,
            },
        ).scalar_one()
    return filing_id


def fake_chunks(count: int) -> list[EmbeddedChunk]:
    return [EmbeddedChunk(text=f"chunk {i}", embedding=[float(i)] * 1024) for i in range(count)]


def test_replace_chunks_persists_provenance_in_voyage_order(db: Engine) -> None:
    filing_id = seed_filing(db)
    with db.begin() as conn:
        Repository(conn).replace_chunks(
            filing_id,
            "mdna",
            "https://sec.gov/filing.htm",
            fake_chunks(3),
            model="voyage-context-4",
            dimension=1024,
        )
    with db.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT chunk_index, content, source_url, model, dimension,"
                " vector_dims(embedding) AS dims"
                " FROM filing_chunks WHERE filing_id = :f AND section = 'mdna'"
                " ORDER BY chunk_index"
            ),
            {"f": filing_id},
        ).all()
    assert [(r.chunk_index, r.content) for r in rows] == [
        (0, "chunk 0"),
        (1, "chunk 1"),
        (2, "chunk 2"),
    ]
    assert all(r.source_url == "https://sec.gov/filing.htm" for r in rows)
    assert all(r.model == "voyage-context-4" for r in rows)
    assert all(r.dimension == 1024 for r in rows)
    assert all(r.dims == 1024 for r in rows)


def test_replace_chunks_removes_stale_rows_on_reembed(db: Engine) -> None:
    filing_id = seed_filing(db)
    with db.begin() as conn:
        Repository(conn).replace_chunks(
            filing_id,
            "mdna",
            "https://sec.gov/filing.htm",
            fake_chunks(3),
            model="voyage-context-4",
            dimension=1024,
        )
    with db.begin() as conn:
        Repository(conn).replace_chunks(
            filing_id,
            "mdna",
            "https://sec.gov/filing.htm",
            fake_chunks(2),
            model="voyage-context-4",
            dimension=1024,
        )
    with db.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT chunk_index FROM filing_chunks"
                    " WHERE filing_id = :f AND section = 'mdna' ORDER BY chunk_index"
                ),
                {"f": filing_id},
            )
            .scalars()
            .all()
        )
    assert rows == [0, 1]


# --- run_backfill (real test database, fake embedder) ---


class FakeEmbedder:
    def __init__(self, model: str = "voyage-context-4", chunks_per_section: int = 2) -> None:
        self.model = model
        self.dimension = 1024
        self.calls: list[str] = []
        self._chunks_per_section = chunks_per_section

    def embed_document(self, text: str) -> list[EmbeddedChunk]:
        self.calls.append(text)
        return [
            EmbeddedChunk(text=f"{text[:20]} [chunk {i}]", embedding=[0.5] * 1024)
            for i in range(self._chunks_per_section)
        ]

    def embed_query(self, text: str) -> list[float]:
        return [0.5] * 1024


def seed_summaries(
    db: Engine,
    filing_id: int,
    sections: list[str],
    source_url: str = "https://sec.gov/filing.htm",
) -> None:
    with db.begin() as conn:
        for section in sections:
            conn.execute(
                text(
                    "INSERT INTO filing_summaries"
                    " (filing_id, section, summary, source_text, source_url, model)"
                    " VALUES (:f, :s, 'summary', :src, :url, 'claude-sonnet-5')"
                ),
                {
                    "f": filing_id,
                    "s": section,
                    "src": f"full {section} section text",
                    "url": source_url,
                },
            )


def chunk_count(db: Engine) -> int:
    with db.connect() as conn:
        count: int = conn.execute(text("SELECT count(*) FROM filing_chunks")).scalar_one()
    return count


def test_run_backfill_embeds_every_pending_section(db: Engine) -> None:
    filing_id = seed_filing(db)
    seed_summaries(db, filing_id, ["business", "mdna"])
    embedder = FakeEmbedder()

    embedded = run_backfill(db, embedder)

    assert embedded == 2
    assert sorted(embedder.calls) == ["full business section text", "full mdna section text"]
    assert chunk_count(db) == 4


def test_run_backfill_skips_sections_already_embedded_with_same_model(db: Engine) -> None:
    filing_id = seed_filing(db)
    seed_summaries(db, filing_id, ["business", "mdna"])
    run_backfill(db, FakeEmbedder())

    second = FakeEmbedder()
    embedded = run_backfill(db, second)

    assert embedded == 0
    assert second.calls == []
    assert chunk_count(db) == 4


def test_run_backfill_reembeds_sections_tagged_with_a_different_model(db: Engine) -> None:
    filing_id = seed_filing(db)
    seed_summaries(db, filing_id, ["business"])
    run_backfill(db, FakeEmbedder(model="voyage-context-4"))

    upgraded = FakeEmbedder(model="voyage-context-5", chunks_per_section=3)
    embedded = run_backfill(db, upgraded)

    assert embedded == 1
    with db.connect() as conn:
        models = (
            conn.execute(
                text("SELECT DISTINCT model FROM filing_chunks WHERE filing_id = :f"),
                {"f": filing_id},
            )
            .scalars()
            .all()
        )
    assert models == ["voyage-context-5"]
    assert chunk_count(db) == 3


def test_run_backfill_embeds_only_new_filing_sections(db: Engine) -> None:
    """ADR-0008: adding a prior-year filing re-uses the existing backfill;
    only the new filing's sections are pending, and its chunks carry its own
    source_url. The already-embedded filing is untouched."""
    newer = seed_filing(db)
    seed_summaries(db, newer, ["business", "mdna"])
    run_backfill(db, FakeEmbedder())

    prior = seed_filing(
        db,
        accession="0000320193-24-000100",
        filing_date="2024-11-01",
        period_end_date="2024-09-28",
        filing_url="https://sec.gov/old-10k.htm",
    )
    seed_summaries(db, prior, ["business", "mdna"], source_url="https://sec.gov/old-10k.htm")
    second = FakeEmbedder()
    embedded = run_backfill(db, second)

    assert embedded == 2
    assert len(second.calls) == 2
    with db.connect() as conn:
        per_filing = {
            row.filing_id: row.n
            for row in conn.execute(
                text("SELECT filing_id, count(*) AS n FROM filing_chunks GROUP BY filing_id")
            )
        }
        prior_urls = (
            conn.execute(
                text("SELECT DISTINCT source_url FROM filing_chunks WHERE filing_id = :f"),
                {"f": prior},
            )
            .scalars()
            .all()
        )
    assert per_filing == {newer: 4, prior: 4}
    assert prior_urls == ["https://sec.gov/old-10k.htm"]


def test_chunks_from_two_filings_same_company_distinguishable_by_period(db: Engine) -> None:
    """Temporal retrieval linkage (ADR-0008): every chunk resolves to its
    filing's period_end_date through filing_chunks.filing_id -> filings, so
    an AAPL FY2025 chunk and an AAPL FY2024 chunk are distinct evidence."""
    newer = seed_filing(db)
    prior = seed_filing(
        db,
        accession="0000320193-24-000100",
        filing_date="2024-11-01",
        period_end_date="2024-09-28",
        filing_url="https://sec.gov/old-10k.htm",
    )
    seed_axis_chunks(db, newer)
    seed_axis_chunks(db, prior)

    with db.connect() as conn:
        periods = {
            row.filing_id: row.period_end_date
            for row in conn.execute(
                text(
                    "SELECT DISTINCT fc.filing_id, f.period_end_date"
                    " FROM filing_chunks fc JOIN filings f ON f.id = fc.filing_id"
                )
            )
        }
        matches = Repository(conn).search_chunks(
            axis_vector(1), model="voyage-context-4", limit=10, ticker="AAPL"
        )
    assert periods[newer].isoformat() == "2025-09-27"
    assert periods[prior].isoformat() == "2024-09-28"
    # retrieval spans both periods, and each match's filing_id keys its period
    assert {m.filing_id for m in matches} == {newer, prior}


# --- semantic search (real test database) ---


def axis_vector(axis: int) -> list[float]:
    vector = [0.0] * 1024
    vector[axis] = 1.0
    return vector


def seed_axis_chunks(db: Engine, filing_id: int, section: str = "mdna") -> None:
    chunks = [EmbeddedChunk(text=f"topic {axis}", embedding=axis_vector(axis)) for axis in range(3)]
    with db.begin() as conn:
        Repository(conn).replace_chunks(
            filing_id,
            section,
            "https://sec.gov/filing.htm",
            chunks,
            model="voyage-context-4",
            dimension=1024,
        )


def test_search_chunks_orders_by_cosine_distance(db: Engine) -> None:
    filing_id = seed_filing(db)
    seed_axis_chunks(db, filing_id)
    with db.connect() as conn:
        matches = Repository(conn).search_chunks(axis_vector(1), model="voyage-context-4", limit=3)
    # topic 0 and topic 2 are both orthogonal to the query (distance exactly 1.0),
    # so their relative order is a tie the SQL does not promise to break stably.
    assert matches[0].content == "topic 1"
    assert {m.content for m in matches[1:]} == {"topic 0", "topic 2"}
    assert matches[0].distance == pytest.approx(0.0, abs=1e-6)
    assert matches[1].distance == pytest.approx(1.0, abs=1e-6)
    assert matches[2].distance == pytest.approx(1.0, abs=1e-6)


def test_search_chunks_returns_full_provenance(db: Engine) -> None:
    filing_id = seed_filing(db)
    seed_axis_chunks(db, filing_id)
    with db.connect() as conn:
        match = Repository(conn).search_chunks(axis_vector(2), model="voyage-context-4", limit=1)[0]
    assert match.ticker == "AAPL"
    assert match.filing_id == filing_id
    assert match.section == "mdna"
    assert match.chunk_index == 2
    assert match.content == "topic 2"
    assert match.source_url == "https://sec.gov/filing.htm"


def test_search_chunks_only_matches_chunks_from_the_given_model(db: Engine) -> None:
    filing_id = seed_filing(db)
    seed_axis_chunks(db, filing_id, section="mdna")
    with db.begin() as conn:
        Repository(conn).replace_chunks(
            filing_id,
            "business",
            "https://sec.gov/filing.htm",
            [EmbeddedChunk(text="stale model chunk", embedding=axis_vector(1))],
            model="voyage-context-3",
            dimension=1024,
        )
    with db.connect() as conn:
        matches = Repository(conn).search_chunks(axis_vector(1), model="voyage-context-4", limit=10)
    assert all(m.content != "stale model chunk" for m in matches)
    assert len(matches) == 3


def test_search_chunks_respects_limit(db: Engine) -> None:
    filing_id = seed_filing(db)
    seed_axis_chunks(db, filing_id)
    with db.connect() as conn:
        matches = Repository(conn).search_chunks(axis_vector(0), model="voyage-context-4", limit=2)
    assert len(matches) == 2


class QueryOnlyEmbedder(FakeEmbedder):
    def __init__(self, query_vector: list[float]) -> None:
        super().__init__()
        self._query_vector = query_vector
        self.queries: list[str] = []

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return self._query_vector


def test_semantic_search_embeds_query_and_returns_nearest_chunks(db: Engine) -> None:
    filing_id = seed_filing(db)
    seed_axis_chunks(db, filing_id)
    embedder = QueryOnlyEmbedder(axis_vector(2))

    matches = semantic_search(db, embedder, "what about topic two?", limit=2)

    assert embedder.queries == ["what about topic two?"]
    assert [m.content for m in matches] == ["topic 2", "topic 0"] or [
        m.content for m in matches
    ] == ["topic 2", "topic 1"]
    assert matches[0].distance == pytest.approx(0.0, abs=1e-6)


def test_semantic_search_rejects_blank_query(db: Engine) -> None:
    with pytest.raises(ValueError, match="query"):
        semantic_search(db, FakeEmbedder(), "   ")


# --- balanced per-period retrieval (ADR-0009 §2, real test database) ---


def seed_prior_period_filing(db: Engine) -> int:
    """Same company as seed_filing, one fiscal year earlier."""
    return seed_filing(
        db,
        accession="0000320193-24-000100",
        filing_date="2024-11-01",
        period_end_date="2024-09-28",
        filing_url="https://sec.gov/old-10k.htm",
    )


def seed_vector_chunks(
    db: Engine,
    filing_id: int,
    vectors: list[list[float]],
    source_url: str = "https://sec.gov/filing.htm",
) -> None:
    chunks = [
        EmbeddedChunk(text=f"filing {filing_id} chunk {i}", embedding=vector)
        for i, vector in enumerate(vectors)
    ]
    with db.begin() as conn:
        Repository(conn).replace_chunks(
            filing_id,
            "mdna",
            source_url,
            chunks,
            model="voyage-context-4",
            dimension=1024,
        )


def spy_per_filing_searches(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record the filing_id of every per-filing search without changing behavior."""
    searched: list[int] = []
    original = Repository.search_chunks_in_filing

    def spy(
        self: Repository,
        query_embedding: list[float],
        *,
        model: str,
        filing_id: int,
        limit: int,
    ) -> list[ChunkMatch]:
        searched.append(filing_id)
        return original(self, query_embedding, model=model, filing_id=filing_id, limit=limit)

    monkeypatch.setattr(Repository, "search_chunks_in_filing", spy)
    return searched


def test_balanced_search_splits_evenly_between_two_filings(
    db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    newer = seed_filing(db)
    older = seed_prior_period_filing(db)
    seed_vector_chunks(db, newer, [axis_vector(i) for i in range(6)])
    seed_vector_chunks(db, older, [axis_vector(i) for i in range(6)])
    embedder = QueryOnlyEmbedder(axis_vector(0))
    searched = spy_per_filing_searches(monkeypatch)

    matches = balanced_semantic_search(db, embedder, "growth", [older, newer], limit=8)

    counts = Counter(m.filing_id for m in matches)
    assert counts == {newer: 4, older: 4}
    assert embedder.queries == ["growth"]  # the query is embedded exactly once
    assert sorted(searched) == sorted([newer, older])  # one separate search per filing


def test_balanced_search_gives_remainder_to_most_recent_period(db: Engine) -> None:
    newer = seed_filing(db)
    older = seed_prior_period_filing(db)
    seed_vector_chunks(db, newer, [axis_vector(i) for i in range(6)])
    seed_vector_chunks(db, older, [axis_vector(i) for i in range(6)])

    # Input order is oldest-first on purpose: allocation must key on
    # period_end_date, not on the order the caller happened to pass.
    matches = balanced_semantic_search(
        db, QueryOnlyEmbedder(axis_vector(0)), "growth", [older, newer], limit=7
    )

    counts = Counter(m.filing_id for m in matches)
    assert counts == {newer: 4, older: 3}


def test_balanced_search_fixes_top_k_period_skew(db: Engine) -> None:
    """The bug from the live 2026-07-05 run: raw top-k lets one period starve
    the other. Here the older filing's chunks all outscore the newer's, so raw
    top-8 returns 6/2; balanced retrieval must return 4/4."""
    newer = seed_filing(db)
    older = seed_prior_period_filing(db)
    seed_vector_chunks(db, newer, [axis_vector(1)] * 8)  # orthogonal to the query
    seed_vector_chunks(db, older, [axis_vector(0)] * 6)  # exact match to the query

    with db.connect() as conn:
        raw = Repository(conn).search_chunks(
            axis_vector(0), model="voyage-context-4", limit=8, ticker="AAPL"
        )
    assert Counter(m.filing_id for m in raw) == {older: 6, newer: 2}  # the skew

    matches = balanced_semantic_search(
        db, QueryOnlyEmbedder(axis_vector(0)), "growth", [newer, older], limit=8
    )
    assert Counter(m.filing_id for m in matches) == {newer: 4, older: 4}


def test_balanced_search_does_not_backfill_a_short_filing(db: Engine) -> None:
    newer = seed_filing(db)
    older = seed_prior_period_filing(db)
    seed_vector_chunks(db, newer, [axis_vector(i) for i in range(6)])
    seed_vector_chunks(db, older, [axis_vector(0), axis_vector(1)])  # only 2 available

    matches = balanced_semantic_search(
        db, QueryOnlyEmbedder(axis_vector(0)), "growth", [older, newer], limit=8
    )

    counts = Counter(m.filing_id for m in matches)
    assert counts == {newer: 4, older: 2}  # older returns what it has; no backfill


def test_balanced_search_dedupes_filing_ids(db: Engine) -> None:
    newer = seed_filing(db)
    older = seed_prior_period_filing(db)
    seed_vector_chunks(db, newer, [axis_vector(i) for i in range(6)])
    seed_vector_chunks(db, older, [axis_vector(i) for i in range(6)])

    matches = balanced_semantic_search(
        db, QueryOnlyEmbedder(axis_vector(0)), "growth", [newer, newer, older], limit=8
    )

    counts = Counter(m.filing_id for m in matches)
    assert counts == {newer: 4, older: 4}  # P=2 after dedupe, not P=3


def test_balanced_search_degenerate_limit_serves_most_recent_filings_only(db: Engine) -> None:
    newer = seed_filing(db)
    older = seed_prior_period_filing(db)
    seed_vector_chunks(db, newer, [axis_vector(i) for i in range(3)])
    seed_vector_chunks(db, older, [axis_vector(i) for i in range(3)])

    matches = balanced_semantic_search(
        db, QueryOnlyEmbedder(axis_vector(0)), "growth", [older, newer], limit=1
    )

    counts = Counter(m.filing_id for m in matches)
    assert counts == {newer: 1}  # L < P: at least 1 each to the most-recent L filings


def test_balanced_search_unknown_filing_id_raises(db: Engine) -> None:
    newer = seed_filing(db)
    with pytest.raises(ValueError, match="unknown filing_id"):
        balanced_semantic_search(
            db, QueryOnlyEmbedder(axis_vector(0)), "growth", [newer, 999999], limit=8
        )


def test_balanced_search_rejects_blank_query(db: Engine) -> None:
    with pytest.raises(ValueError, match="query"):
        balanced_semantic_search(db, FakeEmbedder(), "   ", [1], limit=8)


def test_balanced_search_rejects_empty_filing_ids(db: Engine) -> None:
    with pytest.raises(ValueError, match="filing_ids"):
        balanced_semantic_search(db, FakeEmbedder(), "growth", [], limit=8)


# --- ticker/section filters (real test database) ---


def seed_second_company_filing(db: Engine) -> int:
    return seed_filing(
        db,
        accession="0000789019-25-000001",
        ticker="MSFT",
        cik="0000789019",
        name="Microsoft Corporation",
    )


def test_search_chunks_filters_by_ticker_case_insensitively(db: Engine) -> None:
    aapl_filing = seed_filing(db)
    msft_filing = seed_second_company_filing(db)
    seed_axis_chunks(db, aapl_filing)
    seed_axis_chunks(db, msft_filing)
    with db.connect() as conn:
        matches = Repository(conn).search_chunks(
            axis_vector(1), model="voyage-context-4", limit=10, ticker="msft"
        )
    assert len(matches) == 3
    assert all(m.ticker == "MSFT" for m in matches)


def test_search_chunks_filters_by_section(db: Engine) -> None:
    filing_id = seed_filing(db)
    seed_axis_chunks(db, filing_id, section="mdna")
    seed_axis_chunks(db, filing_id, section="business")
    with db.connect() as conn:
        matches = Repository(conn).search_chunks(
            axis_vector(1), model="voyage-context-4", limit=10, section="business"
        )
    assert len(matches) == 3
    assert all(m.section == "business" for m in matches)


def test_semantic_search_passes_filters_through(db: Engine) -> None:
    aapl_filing = seed_filing(db)
    msft_filing = seed_second_company_filing(db)
    seed_axis_chunks(db, aapl_filing, section="mdna")
    seed_axis_chunks(db, aapl_filing, section="business")
    seed_axis_chunks(db, msft_filing, section="mdna")

    matches = semantic_search(
        db, QueryOnlyEmbedder(axis_vector(0)), "growth", ticker="AAPL", section="mdna"
    )

    assert len(matches) == 3
    assert all(m.ticker == "AAPL" and m.section == "mdna" for m in matches)
