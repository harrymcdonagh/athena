import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError

import apps.api.research.find as find_module


@pytest.fixture(autouse=True)
def _rerank_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-0013: the reranker ships default ON, but the test suite must never
    import torch (the ~hundreds-of-MB sentence-transformers/torch dependency).
    Force FIND's rerank flag OFF for every test so no test path triggers the
    lazy model load; the existing find/router tests then exercise the exact
    pre-rerank cosine behavior, which is also the runtime fallback when the
    dependency is absent. The dedicated rerank tests opt back IN by passing
    rerank_enabled=True with an injected STUB scorer, so the ON path is
    exercised deterministically — never against real torch."""
    monkeypatch.setattr(find_module, "RERANK_ENABLED", False)


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
                "TRUNCATE companies, filings, filing_summaries, filing_chunks,"
                " thesis_snapshots, sec_ticker_reference RESTART IDENTITY CASCADE"
            )
        )
    yield db_engine
