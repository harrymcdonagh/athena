"""filing chunks for semantic search

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-04

"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_UPGRADE_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE filing_chunks (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    filing_id    BIGINT NOT NULL REFERENCES filings(id),
    section      TEXT NOT NULL CHECK (section IN ('business', 'risk_factors', 'mdna')),
    source_url   TEXT NOT NULL,
    chunk_index  INTEGER NOT NULL,
    content      TEXT NOT NULL,
    embedding    VECTOR(1024) NOT NULL,
    model        TEXT NOT NULL,
    dimension    INTEGER NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (filing_id, section, chunk_index)
);

CREATE INDEX filing_chunks_embedding_hnsw
    ON filing_chunks USING hnsw (embedding vector_cosine_ops);
"""

_DOWNGRADE_SQL = """
DROP TABLE filing_chunks;
"""


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
