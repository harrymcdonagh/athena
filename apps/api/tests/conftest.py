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
