"""sec_ticker_reference external cache

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-06

"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

# ADR-0010 #2: an external CACHE of the SEC's company_tickers_exchange.json —
# a new kind of table (external-source cache), not evidence. No foreign keys
# to companies/filings; it stands alone. ADR-0010 #6: SEC-authoritative
# identity fields ONLY (ticker, CIK, conformed name, exchange) — never
# sector/industry/price/market-cap.
#
# cik is TEXT zero-padded to 10 digits, matching companies.cik and
# CompanyRef.cik — one CIK representation in the system (padding happens once,
# at parse time in the EDGAR client, same as resolve_ticker).
#
# ticker is the primary key: unique in the SEC file (verified against the real
# 2026-07-06 download — 10,415 rows, zero duplicate tickers). cik is indexed
# but NOT unique: one CIK carries multiple tickers for share classes
# (GOOG/GOOGL both map to Alphabet's CIK; 1,476 multi-ticker CIKs in the same
# download). exchange is nullable — the SEC file itself carries nulls (~189).
#
# refreshed_at = when this row was last confirmed against the SEC file, so
# staleness is inspectable per row (ADR-0010 #4's on-demand refresh bumps it).
_UPGRADE_SQL = """
CREATE TABLE sec_ticker_reference (
    ticker        TEXT PRIMARY KEY,
    cik           TEXT NOT NULL,
    company_name  TEXT NOT NULL,
    exchange      TEXT,
    refreshed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX sec_ticker_reference_cik_idx ON sec_ticker_reference (cik);
"""

_DOWNGRADE_SQL = """
DROP TABLE sec_ticker_reference;
"""


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
