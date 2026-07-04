# First Research Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `POST /research/{ticker}` fetches the company's latest 10-K from SEC EDGAR, summarizes Item 1 / 1A / 7 via the Claude API with exact figures and source URLs, and persists company + filing + summaries + an append-only thesis snapshot in Postgres.

**Architecture:** FastAPI sync endpoint → `ResearchService` orchestrates `EdgarClient` (httpx2 against SEC's free JSON APIs) → `extract_sections` (BeautifulSoup + regex) → `Summarizer` protocol (`ClaudeSummarizer` in prod, fake in tests) → `Repository` (raw SQL via SQLAlchemy Core `text()`) writing all rows in one transaction. Schema lives in one raw-SQL Alembic migration. Spec: `docs/superpowers/specs/2026-07-04-first-research-slice-design.md`; plan gate: ADR-0005.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 + psycopg3, Alembic, httpx2, anthropic SDK, BeautifulSoup4, pgvector/pg16 via Docker Compose, pytest.

## Global Constraints

- Python via `.venv/bin/python3.12` (system Python is 3.9); activate with `source .venv/bin/activate`.
- ruff line-length 100, `select = ["E","F","I","UP","B","SIM"]`; mypy `strict = true`; run `ruff check .`, `mypy apps/`, `pytest` before claiming done.
- Conventional commits (`feat:` `fix:` `chore:` `docs:` `test:`).
- **No secrets in tracked files.** `ANTHROPIC_API_KEY` only via `.env` (git-ignored); `.env.example` carries names only.
- Anthropic model ID is exactly `claude-sonnet-5` (no date suffix). Do not pass `temperature`. Filter response content to `type == "text"` blocks (adaptive thinking is on by default and emits `thinking` blocks). `max_tokens=16000`.
- Every SEC request must send a `User-Agent` header (SEC fair-access policy) — value from `SEC_EDGAR_USER_AGENT` env var.
- Local Postgres: Docker Compose, host port **5433** (avoids clashing with any native Postgres on 5432).
- Athena NEVER outputs buy/sell/hold recommendations — enforced in the summarizer system prompt.
- Tests requiring Postgres must `pytest.skip` cleanly when the database is unreachable.

---

### Task 1: Dependencies, Docker Compose, Settings

**Files:**
- Modify: `pyproject.toml`
- Create: `docker-compose.yml`
- Create: `.env.example`
- Create: `apps/api/config.py`
- Create: `apps/api/db.py`
- Test: `apps/api/tests/test_config.py`

**Interfaces:**
- Consumes: nothing (first task)
- Produces: `apps.api.config.Settings` (fields `database_url: str`, `anthropic_api_key: str`, `sec_edgar_user_agent: str`), `apps.api.config.get_settings() -> Settings` (lru_cached), `apps.api.db.get_engine() -> sqlalchemy.Engine` (lru_cached)

- [ ] **Step 1: Add runtime dependencies to `pyproject.toml`**

Replace the `dependencies` list with:

```toml
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "httpx2>=0.28",
    "sqlalchemy>=2.0",
    "psycopg[binary]>=3.2",
    "alembic>=1.13",
    "pydantic-settings>=2.4",
    "anthropic>=0.40",
    "beautifulsoup4>=4.12",
]
```

- [ ] **Step 2: Install and verify the httpx2 import name**

Run: `.venv/bin/pip install -e ".[dev]"`
Then: `.venv/bin/python -c "import httpx2; print(httpx2.__name__)"`
If that import fails, try `.venv/bin/python -c "import httpx; print(httpx.__version__)"` — whichever name works is the import to use in `apps/api/edgar/client.py` (Task 3) and tests. Record the working name; the plan below assumes `import httpx2 as httpx`. If the module is importable as plain `httpx`, drop the alias.

- [ ] **Step 3: Create `docker-compose.yml`**

```yaml
services:
  db:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_USER: athena
      POSTGRES_PASSWORD: athena
      POSTGRES_DB: athena
    ports:
      - "5433:5432"
    volumes:
      - athena_pgdata:/var/lib/postgresql/data

volumes:
  athena_pgdata:
```

Run: `docker compose up -d` and verify with `docker compose ps` (state should be `running`).

- [ ] **Step 4: Create `.env.example`** (names only — no secrets)

```
# Copy to .env and fill in. NEVER commit .env.
DATABASE_URL=postgresql+psycopg://athena:athena@localhost:5433/athena
ANTHROPIC_API_KEY=
# SEC fair-access policy requires a descriptive User-Agent with contact email
SEC_EDGAR_USER_AGENT="Athena Research your-email@example.com"
```

- [ ] **Step 5: Write the failing test** — `apps/api/tests/test_config.py`

```python
import pytest

from apps.api.config import Settings


def test_settings_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h:5/db")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "Test test@example.com")
    settings = Settings()
    assert settings.database_url == "postgresql+psycopg://u:p@h:5/db"
    assert settings.anthropic_api_key == "test-key"
    assert settings.sec_edgar_user_agent == "Test test@example.com"


def test_settings_have_local_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = Settings(_env_file=None)
    assert settings.database_url.endswith("localhost:5433/athena")
```

Run: `pytest apps/api/tests/test_config.py -v` — Expected: FAIL (`ModuleNotFoundError: apps.api.config`)

- [ ] **Step 6: Implement `apps/api/config.py`**

```python
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://athena:athena@localhost:5433/athena"
    anthropic_api_key: str = ""
    sec_edgar_user_agent: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 7: Implement `apps/api/db.py`**

```python
from functools import lru_cache

from sqlalchemy import Engine, create_engine

from apps.api.config import get_settings


@lru_cache
def get_engine() -> Engine:
    return create_engine(get_settings().database_url)
```

- [ ] **Step 8: Run tests, lint, type check**

Run: `pytest apps/api/tests/test_config.py -v` — Expected: PASS
Run: `ruff check . && mypy apps/` — Expected: clean

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml docker-compose.yml .env.example apps/api/config.py apps/api/db.py apps/api/tests/test_config.py
git commit -m "feat: add settings, db engine, and dockerized pgvector Postgres"
```

---

### Task 2: Alembic + migration 0001 + DB test fixture

**Files:**
- Create: `alembic.ini`
- Create: `apps/api/migrations/env.py`
- Create: `apps/api/migrations/script.py.mako`
- Create: `apps/api/migrations/versions/0001_initial_research_schema.py`
- Create: `apps/api/tests/conftest.py`
- Test: `apps/api/tests/test_migration.py`

**Interfaces:**
- Consumes: `get_settings()` from Task 1
- Produces: tables `companies`, `filings`, `filing_summaries`, `thesis_snapshots`; pytest fixtures `db_engine` (session-scoped, migrated `athena_test` database, skips if Postgres down) and `db` (function-scoped, truncates all four tables, yields `Engine`)

- [ ] **Step 1: Create `alembic.ini`**

```ini
[alembic]
script_location = apps/api/migrations
prepend_sys_path = .

[loggers]
keys = root

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
```

- [ ] **Step 2: Create `apps/api/migrations/env.py`**

```python
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from apps.api.config import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", get_settings().database_url)


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

- [ ] **Step 3: Create `apps/api/migrations/script.py.mako`** (standard Alembic template)

```mako
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

- [ ] **Step 4: Write the failing test** — `apps/api/tests/test_migration.py`

```python
import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError


def test_all_tables_exist(db: Engine) -> None:
    with db.connect() as conn:
        rows = conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        ).scalars().all()
    for table in ("companies", "filings", "filing_summaries", "thesis_snapshots"):
        assert table in rows


def test_pgvector_extension_enabled(db: Engine) -> None:
    with db.connect() as conn:
        found = conn.execute(
            text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        ).scalar()
    assert found == 1


def _seed_snapshot(conn: Connection) -> None:
    company_id = conn.execute(
        text("INSERT INTO companies (ticker, cik, name) VALUES ('T', '0000000001', 'T Inc') RETURNING id")
    ).scalar_one()
    filing_id = conn.execute(
        text(
            "INSERT INTO filings (company_id, accession_number, form_type, filing_date,"
            " filing_url, content_sha256)"
            " VALUES (:c, 'acc-1', '10-K', '2026-01-01', 'https://example.com', 'sha') RETURNING id"
        ),
        {"c": company_id},
    ).scalar_one()
    conn.execute(
        text(
            "INSERT INTO thesis_snapshots (company_id, content, source_filing_id)"
            " VALUES (:c, 'thesis', :f)"
        ),
        {"c": company_id, "f": filing_id},
    )


def test_thesis_snapshots_reject_update(db: Engine) -> None:
    with db.begin() as conn:
        _seed_snapshot(conn)
    # separate transaction: the raised exception aborts it, and pytest.raises
    # must wrap the begin() block so rollback (not commit) runs on exit
    with pytest.raises(DBAPIError, match="append-only"), db.begin() as conn:
        conn.execute(text("UPDATE thesis_snapshots SET content = 'edited'"))


def test_thesis_snapshots_reject_delete(db: Engine) -> None:
    with db.begin() as conn:
        _seed_snapshot(conn)
    with pytest.raises(DBAPIError, match="append-only"), db.begin() as conn:
        conn.execute(text("DELETE FROM thesis_snapshots"))
```

- [ ] **Step 5: Create `apps/api/tests/conftest.py`**

Note: `TRUNCATE` does not fire the row-level append-only trigger, which is what lets the fixture reset `thesis_snapshots` between tests. That is expected — the trigger guards row mutation, not table administration.

```python
import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError

TEST_DATABASE_URL = os.environ.get(
    "ATHENA_TEST_DATABASE_URL",
    "postgresql+psycopg://athena:athena@localhost:5433/athena_test",
)
ADMIN_DATABASE_URL = os.environ.get(
    "ATHENA_ADMIN_DATABASE_URL",
    "postgresql+psycopg://athena:athena@localhost:5433/athena",
)


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    try:
        admin = create_engine(ADMIN_DATABASE_URL, isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = 'athena_test'")
            ).scalar()
            if not exists:
                conn.execute(text("CREATE DATABASE athena_test"))
        admin.dispose()
    except OperationalError:
        pytest.skip("Postgres is not running — start it with `docker compose up -d`")

    cfg = AlembicConfig("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(cfg, "head")

    engine = create_engine(TEST_DATABASE_URL)
    yield engine
    engine.dispose()


@pytest.fixture
def db(db_engine: Engine) -> Iterator[Engine]:
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE companies, filings, filing_summaries, thesis_snapshots"
                " RESTART IDENTITY CASCADE"
            )
        )
    yield db_engine
```

Run: `pytest apps/api/tests/test_migration.py -v` — Expected: FAIL (alembic has no revisions yet / tables missing)

- [ ] **Step 6: Create `apps/api/migrations/versions/0001_initial_research_schema.py`**

```python
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
```

- [ ] **Step 7: Apply to the dev database and run tests**

Run: `alembic upgrade head` — Expected: `Running upgrade  -> 0001`
Run: `pytest apps/api/tests/test_migration.py -v` — Expected: 4 PASS
Run: `ruff check . && mypy apps/` — Expected: clean

- [ ] **Step 8: Commit**

```bash
git add alembic.ini apps/api/migrations apps/api/tests/conftest.py apps/api/tests/test_migration.py
git commit -m "feat: initial research schema migration with append-only thesis_snapshots"
```

---

### Task 3: EDGAR client

**Files:**
- Create: `apps/api/edgar/__init__.py` (empty)
- Create: `apps/api/edgar/client.py`
- Test: `apps/api/tests/test_edgar_client.py`

**Interfaces:**
- Consumes: nothing from prior tasks (user agent string passed in)
- Produces:
  - `CompanyRef` frozen dataclass: `ticker: str`, `cik: str` (10-digit zero-padded), `name: str`
  - `FilingRef` frozen dataclass: `accession_number: str`, `form_type: str`, `filing_date: date`, `period_end_date: date | None`, `filing_url: str`
  - `EdgarClient(user_agent: str, client: httpx.Client | None = None)` with `resolve_ticker(ticker: str) -> CompanyRef`, `latest_10k(company: CompanyRef) -> FilingRef`, `fetch_document(filing: FilingRef) -> str`
  - Exceptions: `EdgarError(Exception)`, `TickerNotFoundError(EdgarError)`, `FilingNotFoundError(EdgarError)`

- [ ] **Step 1: Write the failing tests** — `apps/api/tests/test_edgar_client.py`

Uses `httpx.MockTransport` so no live network. (Import name per Task 1 Step 2 — assumed `import httpx2 as httpx`.)

```python
import json
from collections.abc import Callable

import httpx2 as httpx
import pytest

from apps.api.edgar.client import (
    CompanyRef,
    EdgarClient,
    FilingNotFoundError,
    TickerNotFoundError,
)

TICKERS = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
}

SUBMISSIONS = {
    "filings": {
        "recent": {
            "form": ["8-K", "10-K", "10-K"],
            "accessionNumber": ["0000320193-26-000001", "0000320193-25-000123", "0000320193-24-000100"],
            "filingDate": ["2026-01-05", "2025-11-01", "2024-11-01"],
            "reportDate": ["2026-01-05", "2025-09-27", "2024-09-28"],
            "primaryDocument": ["a8k.htm", "aapl-10k.htm", "old-10k.htm"],
        }
    }
}


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> EdgarClient:
    http = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": "t t@e.c"})
    return EdgarClient(user_agent="t t@e.c", client=http)


def edgar_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url.endswith("company_tickers.json"):
        return httpx.Response(200, text=json.dumps(TICKERS))
    if "submissions/CIK0000320193" in url:
        return httpx.Response(200, text=json.dumps(SUBMISSIONS))
    if url.endswith("aapl-10k.htm"):
        return httpx.Response(200, text="<html>10-K body</html>")
    return httpx.Response(404)


def test_resolve_ticker_pads_cik_and_is_case_insensitive() -> None:
    client = make_client(edgar_handler)
    company = client.resolve_ticker("aapl")
    assert company == CompanyRef(ticker="AAPL", cik="0000320193", name="Apple Inc.")


def test_resolve_ticker_unknown_raises() -> None:
    client = make_client(edgar_handler)
    with pytest.raises(TickerNotFoundError):
        client.resolve_ticker("ZZZZ")


def test_latest_10k_picks_first_10k_and_builds_url() -> None:
    client = make_client(edgar_handler)
    company = CompanyRef(ticker="AAPL", cik="0000320193", name="Apple Inc.")
    filing = client.latest_10k(company)
    assert filing.accession_number == "0000320193-25-000123"
    assert filing.form_type == "10-K"
    assert filing.filing_date.isoformat() == "2025-11-01"
    assert filing.period_end_date is not None
    assert filing.filing_url == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019325000123/aapl-10k.htm"
    )


def test_latest_10k_none_found_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=json.dumps({"filings": {"recent": {
                "form": ["8-K"], "accessionNumber": ["a-1"], "filingDate": ["2026-01-01"],
                "reportDate": ["2026-01-01"], "primaryDocument": ["a.htm"],
            }}}),
        )

    client = make_client(handler)
    with pytest.raises(FilingNotFoundError):
        client.latest_10k(CompanyRef(ticker="X", cik="0000000009", name="X"))


def test_fetch_document_returns_html() -> None:
    client = make_client(edgar_handler)
    company = CompanyRef(ticker="AAPL", cik="0000320193", name="Apple Inc.")
    filing = client.latest_10k(company)
    assert client.fetch_document(filing) == "<html>10-K body</html>"
```

Run: `pytest apps/api/tests/test_edgar_client.py -v` — Expected: FAIL (module missing)

- [ ] **Step 2: Implement `apps/api/edgar/client.py`**

```python
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx2 as httpx

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodash}/{document}"


class EdgarError(Exception):
    pass


class TickerNotFoundError(EdgarError):
    pass


class FilingNotFoundError(EdgarError):
    pass


@dataclass(frozen=True)
class CompanyRef:
    ticker: str
    cik: str
    name: str


@dataclass(frozen=True)
class FilingRef:
    accession_number: str
    form_type: str
    filing_date: date
    period_end_date: date | None
    filing_url: str


class EdgarClient:
    def __init__(self, user_agent: str, client: httpx.Client | None = None) -> None:
        self._http = client or httpx.Client(
            headers={"User-Agent": user_agent}, timeout=30.0, follow_redirects=True
        )

    def resolve_ticker(self, ticker: str) -> CompanyRef:
        data = self._get_json(COMPANY_TICKERS_URL)
        wanted = ticker.upper()
        for entry in data.values():
            if entry["ticker"].upper() == wanted:
                return CompanyRef(
                    ticker=entry["ticker"], cik=f"{entry['cik_str']:010d}", name=entry["title"]
                )
        raise TickerNotFoundError(f"ticker {ticker!r} not found on SEC EDGAR")

    def latest_10k(self, company: CompanyRef) -> FilingRef:
        data = self._get_json(SUBMISSIONS_URL.format(cik=company.cik))
        recent = data["filings"]["recent"]
        for i, form in enumerate(recent["form"]):
            if form != "10-K":
                continue
            accession = recent["accessionNumber"][i]
            report_date = recent["reportDate"][i]
            return FilingRef(
                accession_number=accession,
                form_type=form,
                filing_date=date.fromisoformat(recent["filingDate"][i]),
                period_end_date=date.fromisoformat(report_date) if report_date else None,
                filing_url=ARCHIVES_URL.format(
                    cik_int=int(company.cik),
                    accession_nodash=accession.replace("-", ""),
                    document=recent["primaryDocument"][i],
                ),
            )
        raise FilingNotFoundError(f"no 10-K filing found for CIK {company.cik}")

    def fetch_document(self, filing: FilingRef) -> str:
        response = self._http.get(filing.filing_url)
        response.raise_for_status()
        return response.text

    def _get_json(self, url: str) -> dict[str, Any]:
        response = self._http.get(url)
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result
```

- [ ] **Step 3: Run tests, lint, type check**

Run: `pytest apps/api/tests/test_edgar_client.py -v` — Expected: 5 PASS
Run: `ruff check . && mypy apps/` — Expected: clean

- [ ] **Step 4: Commit**

```bash
git add apps/api/edgar apps/api/tests/test_edgar_client.py
git commit -m "feat: SEC EDGAR client (ticker resolution, latest 10-K, document fetch)"
```

---

### Task 4: Section extraction

**Files:**
- Create: `apps/api/edgar/sections.py`
- Test: `apps/api/tests/test_sections.py`

**Interfaces:**
- Consumes: nothing from prior tasks
- Produces: `SECTIONS: tuple[str, ...] = ("business", "risk_factors", "mdna")`, `extract_sections(html: str) -> dict[str, str]`, `SectionExtractionError(Exception)`

Heuristic: 10-K headings appear twice — once in the table of contents, once in the body. We take the **last** occurrence of each start heading and slice to the nearest following end heading. A section under 500 chars fails loudly.

- [ ] **Step 1: Write the failing tests** — `apps/api/tests/test_sections.py`

```python
import pytest

from apps.api.edgar.sections import SectionExtractionError, extract_sections

BUSINESS = "We design and sell widgets. Revenue was $391,035 million in fiscal 2025. " * 20
RISKS = "Competition may harm margins. Supply chain concentration in one region. " * 20
MDNA = "Net sales increased 2% to $391 billion driven by services growth of 13%. " * 20


def build_10k_html() -> str:
    toc = (
        "<p>Item 1. Business ... 3</p><p>Item 1A. Risk Factors ... 20</p>"
        "<p>Item 1B. Unresolved Staff Comments ... 45</p>"
        "<p>Item 7. Management's Discussion and Analysis ... 50</p>"
        "<p>Item 7A. Quantitative and Qualitative Disclosures ... 80</p>"
    )
    body = (
        f"<h2>Item 1. Business</h2><p>{BUSINESS}</p>"
        f"<h2>Item 1A. Risk Factors</h2><p>{RISKS}</p>"
        "<h2>Item 1B. Unresolved Staff Comments</h2><p>None.</p>"
        "<h2>Item 5. Market</h2><p>Common stock is listed on Nasdaq.</p>"
        f"<h2>Item 7. Management's Discussion and Analysis of Financial Condition</h2><p>{MDNA}</p>"
        "<h2>Item 7A. Quantitative and Qualitative Disclosures About Market Risk</h2><p>Rates.</p>"
    )
    return f"<html><body>{toc}{body}</body></html>"


def test_extracts_all_three_sections_past_the_toc() -> None:
    sections = extract_sections(build_10k_html())
    assert set(sections) == {"business", "risk_factors", "mdna"}
    assert "Revenue was $391,035 million" in sections["business"]
    assert "Supply chain concentration" in sections["risk_factors"]
    assert "services growth of 13%" in sections["mdna"]
    # slices must not bleed into the next item
    assert "Unresolved Staff Comments" not in sections["risk_factors"].rstrip(" .")


def test_missing_section_raises() -> None:
    with pytest.raises(SectionExtractionError):
        extract_sections("<html><body><p>Item 1. Business</p><p>short</p></body></html>")


def test_too_short_section_raises() -> None:
    html = (
        "<html><body><h2>Item 1. Business</h2><p>tiny</p>"
        "<h2>Item 1A. Risk Factors</h2><p>tiny</p>"
        "<h2>Item 1B. Unresolved</h2>"
        "<h2>Item 7. Management's Discussion</h2><p>tiny</p>"
        "<h2>Item 7A. Quantitative</h2></body></html>"
    )
    with pytest.raises(SectionExtractionError, match="too short"):
        extract_sections(html)
```

Run: `pytest apps/api/tests/test_sections.py -v` — Expected: FAIL (module missing)

- [ ] **Step 2: Implement `apps/api/edgar/sections.py`**

```python
import re

from bs4 import BeautifulSoup

SECTIONS: tuple[str, ...] = ("business", "risk_factors", "mdna")

_MIN_SECTION_CHARS = 500

_BOUNDS: dict[str, tuple[str, str]] = {
    "business": (
        r"item\s*1\s*[.:–—-]?\s*business",
        r"item\s*1a\s*[.:–—-]?\s*risk\s*factors",
    ),
    "risk_factors": (
        r"item\s*1a\s*[.:–—-]?\s*risk\s*factors",
        r"item\s*1b\s*[.:–—-]?\s*unresolved",
    ),
    "mdna": (
        r"item\s*7\s*[.:–—-]?\s*management",
        r"item\s*7a\s*[.:–—-]?\s*quantitative",
    ),
}


class SectionExtractionError(Exception):
    pass


def extract_sections(html: str) -> dict[str, str]:
    text = _html_to_text(html)
    lowered = text.lower()
    sections: dict[str, str] = {}
    for section, (start_pattern, end_pattern) in _BOUNDS.items():
        starts = [m.start() for m in re.finditer(start_pattern, lowered)]
        if not starts:
            raise SectionExtractionError(f"could not locate the start of section {section!r}")
        start = max(starts)  # last occurrence: TOC entries come first, the body last
        ends = [m.start() for m in re.finditer(end_pattern, lowered) if m.start() > start]
        if not ends:
            raise SectionExtractionError(f"could not locate the end of section {section!r}")
        content = text[start : min(ends)].strip()
        if len(content) < _MIN_SECTION_CHARS:
            raise SectionExtractionError(
                f"section {section!r} too short ({len(content)} chars) — extraction likely failed"
            )
        sections[section] = content
    return sections


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return re.sub(r"\s+", " ", soup.get_text(" "))
```

- [ ] **Step 3: Run tests, lint, type check**

Run: `pytest apps/api/tests/test_sections.py -v` — Expected: 3 PASS
Run: `ruff check . && mypy apps/` — Expected: clean

- [ ] **Step 4: Commit**

```bash
git add apps/api/edgar/sections.py apps/api/tests/test_sections.py
git commit -m "feat: extract Item 1 / 1A / 7 sections from 10-K HTML"
```

---

### Task 5: Summarizer protocol + ClaudeSummarizer

**Files:**
- Create: `apps/api/research/__init__.py` (empty)
- Create: `apps/api/research/summarizer.py`
- Test: `apps/api/tests/test_summarizer.py`

**Interfaces:**
- Consumes: nothing from prior tasks
- Produces:
  - `Summarizer` Protocol: attribute `model: str`; method `summarize(section: str, text: str, source_url: str) -> str`
  - `ClaudeSummarizer(api_key: str)` implementing it (`model = "claude-sonnet-5"`)
  - `build_prompt(section: str, text: str, source_url: str) -> str` (pure, unit-testable)
  - `SummarizationError(Exception)`
- Later tasks define their own `FakeSummarizer` test double conforming to the Protocol.

- [ ] **Step 1: Write the failing tests** — `apps/api/tests/test_summarizer.py`

Glue is tested lightly: prompt construction only, no network.

```python
from apps.api.research.summarizer import ClaudeSummarizer, Summarizer, build_prompt


def test_prompt_contains_section_title_source_url_and_text() -> None:
    prompt = build_prompt("risk_factors", "Competition may reduce margins.", "https://sec.gov/x")
    assert "Risk Factors" in prompt
    assert "https://sec.gov/x" in prompt
    assert "Competition may reduce margins." in prompt


def test_prompt_forbids_recommendations() -> None:
    prompt = build_prompt("business", "text", "https://sec.gov/x")
    assert "recommendation" in prompt.lower()


def test_claude_summarizer_satisfies_protocol() -> None:
    summarizer: Summarizer = ClaudeSummarizer(api_key="test")
    assert summarizer.model == "claude-sonnet-5"
```

Run: `pytest apps/api/tests/test_summarizer.py -v` — Expected: FAIL (module missing)

- [ ] **Step 2: Implement `apps/api/research/summarizer.py`**

```python
from typing import Protocol

import anthropic

_MODEL = "claude-sonnet-5"

_SYSTEM_PROMPT = (
    "You are Athena, an investment research assistant. You summarize SEC filings for a "
    "personal research file.\n"
    "Rules:\n"
    "- Summarize only what the filing states. Preserve exact figures (revenue, margins, "
    "unit counts, dates, percentages) exactly as written.\n"
    "- NEVER give buy/sell/hold recommendations, price targets, or investment advice of "
    "any kind. You summarize and cite; conclusions are the reader's responsibility.\n"
    "- Write clear markdown. End with a line `Source: <filing URL>`."
)

_SECTION_TITLES = {
    "business": "Item 1 — Business",
    "risk_factors": "Item 1A — Risk Factors",
    "mdna": "Item 7 — Management's Discussion and Analysis",
}


class SummarizationError(Exception):
    pass


class Summarizer(Protocol):
    @property
    def model(self) -> str: ...

    def summarize(self, section: str, text: str, source_url: str) -> str: ...


def build_prompt(section: str, text: str, source_url: str) -> str:
    title = _SECTION_TITLES[section]
    return (
        f"Summarize the {title} section of this 10-K filing.\n"
        f"Source filing URL: {source_url}\n\n"
        "Requirements:\n"
        "- 300-500 words of markdown, organised with short headings or bullets.\n"
        "- Preserve exact figures as stated in the filing.\n"
        "- Focus on thesis-relevant facts: what the business does, how it makes money, "
        "material risks, and drivers of results.\n"
        "- No investment recommendation or opinion of any kind.\n"
        f"- End with the line: Source: {source_url}\n\n"
        f"<section>\n{text}\n</section>"
    )


class ClaudeSummarizer:
    model = _MODEL

    def __init__(self, api_key: str) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)

    def summarize(self, section: str, text: str, source_url: str) -> str:
        response = self._client.messages.create(
            model=self.model,
            max_tokens=16000,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_prompt(section, text, source_url)}],
        )
        parts = [block.text for block in response.content if block.type == "text"]
        if not parts:
            raise SummarizationError(
                f"model returned no text for section {section!r} (stop_reason="
                f"{response.stop_reason!r})"
            )
        return "\n".join(parts).strip()
```

- [ ] **Step 3: Run tests, lint, type check**

Run: `pytest apps/api/tests/test_summarizer.py -v` — Expected: 3 PASS
Run: `ruff check . && mypy apps/` — Expected: clean

- [ ] **Step 4: Commit**

```bash
git add apps/api/research apps/api/tests/test_summarizer.py
git commit -m "feat: Summarizer protocol and Claude-backed filing summarizer"
```

---

### Task 6: Repository

**Files:**
- Create: `apps/api/research/repository.py`
- Test: `apps/api/tests/test_repository.py`

**Interfaces:**
- Consumes: `FilingRef` from Task 3, `db`/`db_engine` fixtures from Task 2
- Produces (all methods on `Repository(conn: sqlalchemy.Connection)`):
  - `upsert_company(ticker: str, cik: str, name: str) -> int`
  - `find_filing_id(accession_number: str) -> int | None`
  - `insert_filing(company_id: int, filing: FilingRef, content_sha256: str) -> int`
  - `insert_summary(filing_id: int, section: str, summary: str, source_text: str, source_url: str, model: str) -> int`
  - `insert_thesis_snapshot(company_id: int, filing_id: int, content: str) -> int`
  - `latest_research(ticker: str) -> ResearchView | None` where `ResearchView` is a frozen dataclass: `ticker: str`, `company_name: str`, `accession_number: str`, `filing_date: date`, `filing_url: str`, `summaries: dict[str, str]`, `thesis: str`, `thesis_created_at: datetime`

- [ ] **Step 1: Write the failing tests** — `apps/api/tests/test_repository.py`

```python
from datetime import date

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from apps.api.edgar.client import FilingRef
from apps.api.research.repository import Repository

FILING = FilingRef(
    accession_number="0000320193-25-000123",
    form_type="10-K",
    filing_date=date(2025, 11, 1),
    period_end_date=date(2025, 9, 27),
    filing_url="https://www.sec.gov/Archives/edgar/data/320193/000032019325000123/aapl-10k.htm",
)


def test_upsert_company_is_idempotent(db: Engine) -> None:
    with db.begin() as conn:
        repo = Repository(conn)
        first = repo.upsert_company("AAPL", "0000320193", "Apple Inc.")
        second = repo.upsert_company("AAPL", "0000320193", "Apple Inc. (renamed)")
    assert first == second


def test_full_round_trip(db: Engine) -> None:
    with db.begin() as conn:
        repo = Repository(conn)
        company_id = repo.upsert_company("AAPL", "0000320193", "Apple Inc.")
        filing_id = repo.insert_filing(company_id, FILING, content_sha256="abc123")
        repo.insert_summary(
            filing_id, "business", "It sells widgets. Source: url", "raw text",
            FILING.filing_url, "claude-sonnet-5",
        )
        repo.insert_thesis_snapshot(company_id, filing_id, "# Thesis\ncontent")

    with db.connect() as conn:
        view = Repository(conn).latest_research("AAPL")
    assert view is not None
    assert view.company_name == "Apple Inc."
    assert view.accession_number == FILING.accession_number
    assert view.summaries == {"business": "It sells widgets. Source: url"}
    assert view.thesis.startswith("# Thesis")
    assert view.filing_url == FILING.filing_url


def test_find_filing_id(db: Engine) -> None:
    with db.begin() as conn:
        repo = Repository(conn)
        company_id = repo.upsert_company("AAPL", "0000320193", "Apple Inc.")
        filing_id = repo.insert_filing(company_id, FILING, content_sha256="abc123")
        assert repo.find_filing_id(FILING.accession_number) == filing_id
        assert repo.find_filing_id("nope") is None


def test_duplicate_section_summary_rejected(db: Engine) -> None:
    with db.begin() as conn:
        repo = Repository(conn)
        company_id = repo.upsert_company("AAPL", "0000320193", "Apple Inc.")
        filing_id = repo.insert_filing(company_id, FILING, content_sha256="abc123")
        repo.insert_summary(filing_id, "business", "s1", "t", "u", "m")
        with pytest.raises(IntegrityError):
            repo.insert_summary(filing_id, "business", "s2", "t", "u", "m")


def test_latest_research_returns_none_for_unknown_ticker(db: Engine) -> None:
    with db.connect() as conn:
        assert Repository(conn).latest_research("ZZZZ") is None
```

Run: `pytest apps/api/tests/test_repository.py -v` — Expected: FAIL (module missing)

- [ ] **Step 2: Implement `apps/api/research/repository.py`**

```python
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
```

- [ ] **Step 3: Run tests, lint, type check**

Run: `pytest apps/api/tests/test_repository.py -v` — Expected: 5 PASS
Run: `ruff check . && mypy apps/` — Expected: clean

- [ ] **Step 4: Commit**

```bash
git add apps/api/research/repository.py apps/api/tests/test_repository.py
git commit -m "feat: research repository with upsert, inserts, and latest-research view"
```

---

### Task 7: Research service

**Files:**
- Create: `apps/api/research/service.py`
- Test: `apps/api/tests/test_service.py`

**Interfaces:**
- Consumes: `EdgarClient`/`CompanyRef`/`FilingRef`/exceptions (Task 3), `extract_sections` (Task 4), `Summarizer` (Task 5), `Repository` (Task 6), `db` fixture (Task 2)
- Produces:
  - `ResearchOutcome` frozen dataclass: `company_id: int`, `filing_id: int`, `accession_number: str`, `filing_url: str`, `summaries: dict[str, str]`, `thesis_snapshot_id: int`
  - `FilingAlreadyIngestedError(Exception)` with attrs `filing_id: int`, `accession_number: str`
  - `UpstreamError(Exception)` with attrs `source: str` (`"sec-edgar"` or `"anthropic"`), `detail: str`
  - `compose_thesis(company: CompanyRef, filing: FilingRef, summaries: dict[str, str]) -> str`
  - `ResearchService(edgar, summarizer, engine)` with `run(ticker: str) -> ResearchOutcome`
- Service duck-types `edgar` (any object with the three EdgarClient methods) so tests can pass a stub.

- [ ] **Step 1: Write the failing tests** — `apps/api/tests/test_service.py`

```python
from datetime import date

import pytest
from sqlalchemy import Engine, text

from apps.api.edgar.client import CompanyRef, FilingRef
from apps.api.research.service import (
    FilingAlreadyIngestedError,
    ResearchService,
    UpstreamError,
    compose_thesis,
)

COMPANY = CompanyRef(ticker="AAPL", cik="0000320193", name="Apple Inc.")
FILING = FilingRef(
    accession_number="0000320193-25-000123",
    form_type="10-K",
    filing_date=date(2025, 11, 1),
    period_end_date=date(2025, 9, 27),
    filing_url="https://www.sec.gov/Archives/edgar/data/320193/x/aapl-10k.htm",
)

BUSINESS = "We design and sell widgets. Revenue was $391,035 million in fiscal 2025. " * 20
RISKS = "Competition may harm margins. Supply chain concentration in one region. " * 20
MDNA = "Net sales increased 2% to $391 billion driven by services growth of 13%. " * 20

HTML = (
    "<html><body>"
    "<p>Item 1. Business ... 3</p><p>Item 1A. Risk Factors ... 20</p>"
    "<p>Item 1B. Unresolved ... 45</p><p>Item 7. Management's Discussion ... 50</p>"
    "<p>Item 7A. Quantitative ... 80</p>"
    f"<h2>Item 1. Business</h2><p>{BUSINESS}</p>"
    f"<h2>Item 1A. Risk Factors</h2><p>{RISKS}</p>"
    "<h2>Item 1B. Unresolved Staff Comments</h2><p>None.</p>"
    f"<h2>Item 7. Management's Discussion and Analysis</h2><p>{MDNA}</p>"
    "<h2>Item 7A. Quantitative and Qualitative Disclosures</h2><p>Rates.</p>"
    "</body></html>"
)


class FakeEdgar:
    def resolve_ticker(self, ticker: str) -> CompanyRef:
        return COMPANY

    def latest_10k(self, company: CompanyRef) -> FilingRef:
        return FILING

    def fetch_document(self, filing: FilingRef) -> str:
        return HTML


class FakeSummarizer:
    model = "fake-model"

    def summarize(self, section: str, text: str, source_url: str) -> str:
        return f"[{section}] summary. Source: {source_url}"


class ExplodingSummarizer(FakeSummarizer):
    def summarize(self, section: str, text: str, source_url: str) -> str:
        raise UpstreamError("anthropic", "boom")


def count(db: Engine, table: str) -> int:
    with db.connect() as conn:
        result: int = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
    return result


def test_run_persists_everything(db: Engine) -> None:
    service = ResearchService(edgar=FakeEdgar(), summarizer=FakeSummarizer(), engine=db)
    outcome = service.run("AAPL")

    assert set(outcome.summaries) == {"business", "risk_factors", "mdna"}
    assert count(db, "companies") == 1
    assert count(db, "filings") == 1
    assert count(db, "filing_summaries") == 3
    assert count(db, "thesis_snapshots") == 1
    with db.connect() as conn:
        stored_model = conn.execute(
            text("SELECT DISTINCT model FROM filing_summaries")
        ).scalar_one()
        source_text = conn.execute(
            text("SELECT source_text FROM filing_summaries WHERE section = 'business'")
        ).scalar_one()
    assert stored_model == "fake-model"
    assert "Revenue was $391,035 million" in source_text


def test_rerun_same_filing_raises_409_error(db: Engine) -> None:
    service = ResearchService(edgar=FakeEdgar(), summarizer=FakeSummarizer(), engine=db)
    outcome = service.run("AAPL")
    with pytest.raises(FilingAlreadyIngestedError) as exc:
        service.run("AAPL")
    assert exc.value.filing_id == outcome.filing_id


def test_summarizer_failure_persists_nothing(db: Engine) -> None:
    service = ResearchService(edgar=FakeEdgar(), summarizer=ExplodingSummarizer(), engine=db)
    with pytest.raises(UpstreamError):
        service.run("AAPL")
    assert count(db, "companies") == 0
    assert count(db, "filings") == 0
    assert count(db, "filing_summaries") == 0
    assert count(db, "thesis_snapshots") == 0


def test_compose_thesis_cites_filing() -> None:
    thesis = compose_thesis(COMPANY, FILING, {"business": "b", "risk_factors": "r", "mdna": "m"})
    assert "Apple Inc." in thesis
    assert FILING.filing_url in thesis
    assert FILING.accession_number in thesis
    assert "recommendation" in thesis.lower()  # the no-advice disclaimer is embedded
```

Run: `pytest apps/api/tests/test_service.py -v` — Expected: FAIL (module missing)

- [ ] **Step 2: Implement `apps/api/research/service.py`**

```python
import hashlib
from dataclasses import dataclass
from typing import Protocol

import anthropic
import httpx2 as httpx
from sqlalchemy import Engine

from apps.api.edgar.client import CompanyRef, FilingRef
from apps.api.edgar.sections import extract_sections
from apps.api.research.repository import Repository
from apps.api.research.summarizer import Summarizer

_SECTION_TITLES = {
    "business": "Business (Item 1)",
    "risk_factors": "Risk Factors (Item 1A)",
    "mdna": "Management's Discussion and Analysis (Item 7)",
}


class EdgarGateway(Protocol):
    def resolve_ticker(self, ticker: str) -> CompanyRef: ...

    def latest_10k(self, company: CompanyRef) -> FilingRef: ...

    def fetch_document(self, filing: FilingRef) -> str: ...


class FilingAlreadyIngestedError(Exception):
    def __init__(self, filing_id: int, accession_number: str) -> None:
        super().__init__(f"filing {accession_number} already ingested (id={filing_id})")
        self.filing_id = filing_id
        self.accession_number = accession_number


class UpstreamError(Exception):
    def __init__(self, source: str, detail: str) -> None:
        super().__init__(f"{source}: {detail}")
        self.source = source
        self.detail = detail


@dataclass(frozen=True)
class ResearchOutcome:
    company_id: int
    filing_id: int
    accession_number: str
    filing_url: str
    summaries: dict[str, str]
    thesis_snapshot_id: int


def compose_thesis(company: CompanyRef, filing: FilingRef, summaries: dict[str, str]) -> str:
    lines = [
        f"# Initial thesis snapshot: {company.name} ({company.ticker})",
        "",
        f"Derived from Form {filing.form_type} filed {filing.filing_date.isoformat()}"
        f" (accession {filing.accession_number}).",
        f"Source: {filing.filing_url}",
        "",
        "_This snapshot summarizes and cites the filing. It contains no investment"
        " recommendation; conclusions are the reader's responsibility._",
    ]
    for section, title in _SECTION_TITLES.items():
        lines += ["", f"## {title}", "", summaries[section]]
    return "\n".join(lines)


class ResearchService:
    def __init__(self, edgar: EdgarGateway, summarizer: Summarizer, engine: Engine) -> None:
        self._edgar = edgar
        self._summarizer = summarizer
        self._engine = engine

    def run(self, ticker: str) -> ResearchOutcome:
        try:
            company = self._edgar.resolve_ticker(ticker)
            filing = self._edgar.latest_10k(company)
        except httpx.HTTPError as exc:
            raise UpstreamError("sec-edgar", str(exc)) from exc

        with self._engine.connect() as conn:
            existing = Repository(conn).find_filing_id(filing.accession_number)
        if existing is not None:
            raise FilingAlreadyIngestedError(existing, filing.accession_number)

        try:
            html = self._edgar.fetch_document(filing)
        except httpx.HTTPError as exc:
            raise UpstreamError("sec-edgar", str(exc)) from exc
        content_sha256 = hashlib.sha256(html.encode("utf-8")).hexdigest()
        sections = extract_sections(html)

        summaries: dict[str, str] = {}
        for section, section_text in sections.items():
            try:
                summaries[section] = self._summarizer.summarize(
                    section, section_text, filing.filing_url
                )
            except anthropic.APIError as exc:
                raise UpstreamError("anthropic", str(exc)) from exc

        thesis = compose_thesis(company, filing, summaries)

        with self._engine.begin() as conn:
            repo = Repository(conn)
            company_id = repo.upsert_company(company.ticker, company.cik, company.name)
            filing_id = repo.insert_filing(company_id, filing, content_sha256)
            for section, summary in summaries.items():
                repo.insert_summary(
                    filing_id,
                    section,
                    summary,
                    source_text=sections[section],
                    source_url=filing.filing_url,
                    model=self._summarizer.model,
                )
            snapshot_id = repo.insert_thesis_snapshot(company_id, filing_id, thesis)

        return ResearchOutcome(
            company_id=company_id,
            filing_id=filing_id,
            accession_number=filing.accession_number,
            filing_url=filing.filing_url,
            summaries=summaries,
            thesis_snapshot_id=snapshot_id,
        )
```

- [ ] **Step 3: Run tests, lint, type check**

Run: `pytest apps/api/tests/test_service.py -v` — Expected: 4 PASS
Run: `ruff check . && mypy apps/` — Expected: clean

- [ ] **Step 4: Commit**

```bash
git add apps/api/research/service.py apps/api/tests/test_service.py
git commit -m "feat: research service orchestrating fetch, summarize, and transactional persist"
```

---

### Task 8: API router + wiring

**Files:**
- Create: `apps/api/research/router.py`
- Modify: `apps/api/main.py`
- Test: `apps/api/tests/test_router.py`

**Interfaces:**
- Consumes: everything from Tasks 1-7
- Produces:
  - `POST /research/{ticker}` → 200 `ResearchResponse` | 404 | 409 | 422 | 502
  - `GET /companies/{ticker}/summary` → 200 `SummaryResponse` | 404
  - FastAPI dependencies `get_research_service()` and `get_read_engine()` (overridable in tests)

- [ ] **Step 1: Write the failing tests** — `apps/api/tests/test_router.py`

Reuses the fakes from `test_service.py` by importing them.

```python
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from apps.api.edgar.client import CompanyRef, TickerNotFoundError
from apps.api.main import app
from apps.api.research.router import get_read_engine, get_research_service
from apps.api.research.service import ResearchService
from apps.api.tests.test_service import FakeEdgar, FakeSummarizer


class UnknownTickerEdgar(FakeEdgar):
    def resolve_ticker(self, ticker: str) -> CompanyRef:
        raise TickerNotFoundError(f"ticker {ticker!r} not found")


@pytest.fixture
def client(db: Engine) -> Iterator[TestClient]:
    service = ResearchService(edgar=FakeEdgar(), summarizer=FakeSummarizer(), engine=db)
    app.dependency_overrides[get_research_service] = lambda: service
    app.dependency_overrides[get_read_engine] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_post_research_returns_summaries_and_ids(client: TestClient) -> None:
    response = client.post("/research/AAPL")
    assert response.status_code == 200
    body = response.json()
    assert body["ticker"] == "AAPL"
    assert body["accession_number"] == "0000320193-25-000123"
    assert set(body["summaries"]) == {"business", "risk_factors", "mdna"}
    assert body["filing_url"].startswith("https://www.sec.gov/")


def test_post_research_twice_returns_409(client: TestClient) -> None:
    assert client.post("/research/AAPL").status_code == 200
    response = client.post("/research/AAPL")
    assert response.status_code == 409
    assert "already ingested" in response.json()["detail"]


def test_post_research_unknown_ticker_returns_404(client: TestClient, db: Engine) -> None:
    service = ResearchService(edgar=UnknownTickerEdgar(), summarizer=FakeSummarizer(), engine=db)
    app.dependency_overrides[get_research_service] = lambda: service
    assert client.post("/research/ZZZZ").status_code == 404


def test_get_summary_after_research(client: TestClient) -> None:
    client.post("/research/AAPL")
    response = client.get("/companies/AAPL/summary")
    assert response.status_code == 200
    body = response.json()
    assert body["company_name"] == "Apple Inc."
    assert "thesis" in body
    assert set(body["summaries"]) == {"business", "risk_factors", "mdna"}


def test_get_summary_unknown_ticker_returns_404(client: TestClient) -> None:
    assert client.get("/companies/ZZZZ/summary").status_code == 404
```

Run: `pytest apps/api/tests/test_router.py -v` — Expected: FAIL (module missing)

- [ ] **Step 2: Implement `apps/api/research/router.py`**

```python
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import Engine

from apps.api.config import get_settings
from apps.api.db import get_engine
from apps.api.edgar.client import EdgarClient, FilingNotFoundError, TickerNotFoundError
from apps.api.edgar.sections import SectionExtractionError
from apps.api.research.repository import Repository
from apps.api.research.service import (
    FilingAlreadyIngestedError,
    ResearchService,
    UpstreamError,
)
from apps.api.research.summarizer import ClaudeSummarizer, SummarizationError

router = APIRouter()


def get_research_service() -> ResearchService:
    settings = get_settings()
    return ResearchService(
        edgar=EdgarClient(user_agent=settings.sec_edgar_user_agent),
        summarizer=ClaudeSummarizer(api_key=settings.anthropic_api_key),
        engine=get_engine(),
    )


def get_read_engine() -> Engine:
    return get_engine()


class ResearchResponse(BaseModel):
    ticker: str
    company_id: int
    filing_id: int
    accession_number: str
    filing_url: str
    summaries: dict[str, str]
    thesis_snapshot_id: int


class SummaryResponse(BaseModel):
    ticker: str
    company_name: str
    accession_number: str
    filing_date: str
    filing_url: str
    summaries: dict[str, str]
    thesis: str


@router.post("/research/{ticker}", response_model=ResearchResponse)
def run_research(
    ticker: str, service: Annotated[ResearchService, Depends(get_research_service)]
) -> ResearchResponse:
    try:
        outcome = service.run(ticker)
    except (TickerNotFoundError, FilingNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FilingAlreadyIngestedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SectionExtractionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (UpstreamError, SummarizationError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ResearchResponse(ticker=ticker.upper(), **vars(outcome))


@router.get("/companies/{ticker}/summary", response_model=SummaryResponse)
def latest_summary(
    ticker: str, engine: Annotated[Engine, Depends(get_read_engine)]
) -> SummaryResponse:
    with engine.connect() as conn:
        view = Repository(conn).latest_research(ticker)
    if view is None:
        raise HTTPException(status_code=404, detail=f"no research stored for {ticker!r}")
    return SummaryResponse(
        ticker=view.ticker,
        company_name=view.company_name,
        accession_number=view.accession_number,
        filing_date=view.filing_date.isoformat(),
        filing_url=view.filing_url,
        summaries=view.summaries,
        thesis=view.thesis,
    )
```

Note: `vars(outcome)` on a frozen dataclass returns its fields; `ResearchResponse` has the same names plus `ticker`.

- [ ] **Step 3: Wire the router into `apps/api/main.py`**

```python
from fastapi import FastAPI

from apps.api.research.router import router as research_router

app = FastAPI(title="Athena API")
app.include_router(research_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
```

- [ ] **Step 4: Run tests, lint, type check**

Run: `pytest apps/api/tests/test_router.py apps/api/tests/test_health.py -v` — Expected: PASS (health endpoint untouched)
Run: `ruff check . && mypy apps/` — Expected: clean

- [ ] **Step 5: Commit**

```bash
git add apps/api/research/router.py apps/api/main.py apps/api/tests/test_router.py
git commit -m "feat: research endpoints POST /research/{ticker} and GET /companies/{ticker}/summary"
```

---

### Task 9: Verification and review

**Files:**
- No new files (fixes only, if review/verification finds issues)

- [ ] **Step 1: Full local verification**

Run: `ruff check . && ruff format --check . && mypy apps/ && pytest -v`
Expected: all clean, all tests pass (DB tests require `docker compose up -d`).

- [ ] **Step 2: Run the code-reviewer subagent**

Dispatch the project's `code-reviewer` agent (`.claude/agents/code-reviewer.md`) over the full diff of this feature (`git diff <first-feature-commit>^..HEAD`). Apply fixes for confirmed findings; re-run Step 1 after any fix; commit fixes as `fix:` commits.

- [ ] **Step 3: Live smoke test (requires user's ANTHROPIC_API_KEY in `.env`)**

1. Ensure `.env` exists with `ANTHROPIC_API_KEY` and a real `SEC_EDGAR_USER_AGENT`.
2. `alembic upgrade head` (dev database).
3. `uvicorn apps.api.main:app --reload` in background.
4. `curl -s -X POST http://localhost:8000/research/AAPL | head -c 2000` — expect 200 with three summaries containing exact figures and `Source:` URLs (takes 30-90s).
5. `curl -s http://localhost:8000/companies/AAPL/summary | head -c 1000` — expect the stored thesis.
6. Verify auditability in psql: `docker compose exec db psql -U athena -c "SELECT section, source_url, model, length(source_text) FROM filing_summaries JOIN filings ON filings.id = filing_summaries.filing_id;"`

If no API key is available, skip 3-6 and report that the live path is unverified.

- [ ] **Step 4: Report**

Report to the user: what worked, test/lint output, and **what surprised you** during implementation (the user wants these as seeds for future skills).
