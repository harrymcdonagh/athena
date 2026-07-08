"""lazy summary: filing_summaries.summary nullable

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-08

"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

# ADR-0014 #5: summarisation moves from eager-at-ingest to lazy-on-demand.
# `source_text` (the retrieval substrate the embeddings backfill reads) stays
# eager and NOT NULL; only the `summary` answer-model output goes lazy. A
# pending summary is `summary IS NULL` — unambiguous, since a real summary is
# always 300-500 words of markdown and never NULL. DROP NOT NULL relaxes the
# constraint and rewrites no rows, so the already-summarised corpus keeps its
# text and stays non-pending; only newly-ingested filings arrive pending.
_UPGRADE_SQL = """
ALTER TABLE filing_summaries ALTER COLUMN summary DROP NOT NULL;
"""

# SET NOT NULL fails loudly if any pending (NULL) summary exists rather than
# silently discarding the lazy contract — honest absence over silent wrongness,
# mirroring migration 0003's period_end_date NOT NULL pattern.
_DOWNGRADE_SQL = """
ALTER TABLE filing_summaries ALTER COLUMN summary SET NOT NULL;
"""


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
