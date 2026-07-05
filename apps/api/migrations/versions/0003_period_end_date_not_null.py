"""period_end_date not null

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-05

"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# ADR-0008 decision #1: period_end_date is authoritative for what period a
# filing covers, so temporal ordering requires it to be reliably present.
_UPGRADE_SQL = """
ALTER TABLE filings ALTER COLUMN period_end_date SET NOT NULL;
"""

_DOWNGRADE_SQL = """
ALTER TABLE filings ALTER COLUMN period_end_date DROP NOT NULL;
"""


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
