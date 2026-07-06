from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from apps.api.config import get_settings

config = context.config

if config.config_file_name is not None:
    # Keep already-imported app loggers alive: fileConfig's default
    # disable_existing_loggers=True would silently disable them when
    # migrations run in-process (e.g. the test-session upgrade in conftest).
    fileConfig(config.config_file_name, disable_existing_loggers=False)

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
