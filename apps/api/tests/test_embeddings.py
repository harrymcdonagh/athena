import logging
from types import SimpleNamespace

import pytest
import voyageai
from sqlalchemy import Engine, text

from apps.api.research.embeddings import (
    EmbeddedChunk,
    Embedder,
    EmbeddingError,
    VoyageEmbedder,
    run_backfill,
    semantic_search,
)
from apps.api.research.repository import Repository

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


def seed_filing(db: Engine, accession: str = "0000320193-25-000001") -> int:
    with db.begin() as conn:
        company_id = conn.execute(
            text(
                "INSERT INTO companies (ticker, cik, name)"
                " VALUES ('AAPL', '0000320193', 'Apple Inc.')"
                " ON CONFLICT (cik) DO UPDATE SET name = EXCLUDED.name RETURNING id"
            )
        ).scalar_one()
        filing_id: int = conn.execute(
            text(
                "INSERT INTO filings (company_id, accession_number, form_type, filing_date,"
                " filing_url, content_sha256)"
                " VALUES (:company_id, :acc, '10-K', '2025-10-31',"
                " 'https://sec.gov/filing.htm', 'abc123') RETURNING id"
            ),
            {"company_id": company_id, "acc": accession},
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


def seed_summaries(db: Engine, filing_id: int, sections: list[str]) -> None:
    with db.begin() as conn:
        for section in sections:
            conn.execute(
                text(
                    "INSERT INTO filing_summaries"
                    " (filing_id, section, summary, source_text, source_url, model)"
                    " VALUES (:f, :s, 'summary', :src, 'https://sec.gov/filing.htm',"
                    " 'claude-sonnet-5')"
                ),
                {"f": filing_id, "s": section, "src": f"full {section} section text"},
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
    assert [m.content for m in matches] == ["topic 1", "topic 0", "topic 2"]
    assert matches[0].distance == pytest.approx(0.0, abs=1e-6)
    assert matches[1].distance == pytest.approx(1.0, abs=1e-6)


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
