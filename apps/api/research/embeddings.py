import logging
from dataclasses import dataclass
from typing import Protocol

import voyageai
from sqlalchemy import Engine, create_engine

from apps.api.config import get_settings
from apps.api.research.repository import Repository

_MODEL = "voyage-context-4"
_DIMENSION = 1024

_logger = logging.getLogger(__name__)


class EmbeddingError(Exception):
    pass


@dataclass(frozen=True)
class EmbeddedChunk:
    text: str
    embedding: list[float]


class Embedder(Protocol):
    @property
    def model(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    def embed_document(self, text: str) -> list[EmbeddedChunk]: ...

    def embed_query(self, text: str) -> list[float]: ...


class VoyageEmbedder:
    model = _MODEL
    dimension = _DIMENSION

    def __init__(self, api_key: str) -> None:
        # voyageai re-exports Client without __all__, tripping strict no_implicit_reexport
        self._client = voyageai.Client(api_key=api_key)  # type: ignore[attr-defined]

    def embed_document(self, text: str) -> list[EmbeddedChunk]:
        # Auto-chunking requires a FLAT list of full-document strings; nested
        # [[text]] is invalid with enable_auto_chunking and risks silently
        # embedding the whole section as a single pre-chunked chunk.
        response = self._client.contextualized_embed(
            inputs=[text],
            model=self.model,
            input_type="document",
            output_dimension=self.dimension,
            enable_auto_chunking=True,
        )
        result = response.results[0]
        _logger.info(
            "voyage chunked document into %d chunk(s) (model=%s, chunker_version=%s)",
            len(result.embeddings),
            self.model,
            response.chunker_version,
        )
        embeddings = [self._checked(list(e)) for e in result.embeddings]
        if not embeddings:
            raise EmbeddingError("Voyage returned no chunks for document")
        chunk_texts = result.chunk_texts
        if chunk_texts is None or len(chunk_texts) != len(embeddings):
            raise EmbeddingError(
                "Voyage did not return chunk texts matching the embeddings; refusing to"
                " store vectors whose exact source text is unknown (auditability)"
            )
        return [
            EmbeddedChunk(text=chunk_text, embedding=embedding)
            for chunk_text, embedding in zip(chunk_texts, embeddings, strict=True)
        ]

    def embed_query(self, text: str) -> list[float]:
        result = self._client.contextualized_embed(
            inputs=[[text]],
            model=self.model,
            input_type="query",
            output_dimension=self.dimension,
        ).results[0]
        if not result.embeddings:
            raise EmbeddingError("Voyage returned no embedding for query")
        return self._checked(list(result.embeddings[0]))

    def _checked(self, embedding: list[float]) -> list[float]:
        if len(embedding) != self.dimension:
            raise EmbeddingError(
                f"expected embedding dimension {self.dimension}, got {len(embedding)};"
                " refusing to store a vector that does not match the schema"
            )
        return embedding


def run_backfill(engine: Engine, embedder: Embedder) -> int:
    with engine.connect() as conn:
        pending = Repository(conn).sections_pending_embedding(embedder.model)
    for item in pending:
        chunks = embedder.embed_document(item.source_text)
        with engine.begin() as conn:
            Repository(conn).replace_chunks(
                item.filing_id,
                item.section,
                item.source_url,
                chunks,
                model=embedder.model,
                dimension=embedder.dimension,
            )
    return len(pending)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    if not settings.voyage_api_key:
        raise SystemExit("VOYAGE_API_KEY is not set; aborting without embedding anything.")
    engine = create_engine(settings.database_url)
    embedder = VoyageEmbedder(api_key=settings.voyage_api_key)
    embedded = run_backfill(engine, embedder)
    print(f"embedded {embedded} section(s) with {embedder.model}")


if __name__ == "__main__":
    main()
