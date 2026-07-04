"""initial research schema

Revision ID: 0001
Revises:
Create Date: 2026-07-04

"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_UPGRADE_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE companies (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ticker      TEXT NOT NULL UNIQUE,
    cik         TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE filings (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id        BIGINT NOT NULL REFERENCES companies(id),
    accession_number  TEXT NOT NULL UNIQUE,
    form_type         TEXT NOT NULL,
    filing_date       DATE NOT NULL,
    period_end_date   DATE,
    filing_url        TEXT NOT NULL,
    content_sha256    TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE filing_summaries (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    filing_id    BIGINT NOT NULL REFERENCES filings(id),
    section      TEXT NOT NULL CHECK (section IN ('business', 'risk_factors', 'mdna')),
    summary      TEXT NOT NULL,
    source_text  TEXT NOT NULL,
    source_url   TEXT NOT NULL,
    model        TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (filing_id, section)
);

CREATE TABLE thesis_snapshots (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id        BIGINT NOT NULL REFERENCES companies(id),
    content           TEXT NOT NULL,
    source_filing_id  BIGINT NOT NULL REFERENCES filings(id),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS
$$ BEGIN RAISE EXCEPTION 'thesis_snapshots is append-only'; END $$;

CREATE TRIGGER thesis_snapshots_append_only
    BEFORE UPDATE OR DELETE ON thesis_snapshots
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
"""

_DOWNGRADE_SQL = """
DROP TRIGGER thesis_snapshots_append_only ON thesis_snapshots;
DROP FUNCTION forbid_mutation();
DROP TABLE thesis_snapshots;
DROP TABLE filing_summaries;
DROP TABLE filings;
DROP TABLE companies;
"""


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
