import os
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from hermes.schema import metadata

# Resolve the DSN from a migration-safe source: prefer alembic.ini's
# `sqlalchemy.url`, fall back to the same env var the app reads. Avoid
# importing `hermes.config.settings` here — that triggers pydantic-settings
# validation (platform_admin_token / _email required), which is irrelevant
# to running migrations and would break `alembic upgrade` in CI / images
# that only have DB credentials.
config = context.config

_ini_url = config.get_main_option("sqlalchemy.url")
_env_url = os.getenv("HERMES_DATABASE_URL")
_db_url = _env_url or _ini_url
if not _db_url:
    raise RuntimeError(
        "alembic: DB URL is required — set sqlalchemy.url in alembic.ini "
        "or HERMES_DATABASE_URL in the environment"
    )
config.set_main_option("sqlalchemy.url", _db_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    """Configure + run migrations on a sync connection — both must share the
    same transaction (Alembic owns it via begin_transaction).

    `compare_type` + `compare_server_default` let autogenerate spot column-type
    swaps (e.g. INTEGER→BOOLEAN in Task 5's schema port) and default flips that
    the default diff would miss.
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    try:
        async with connectable.connect() as conn:
            await conn.run_sync(_do_run_migrations)
    finally:
        await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    import asyncio
    asyncio.run(run_migrations_online())
