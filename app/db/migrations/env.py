"""Alembic migration environment (async).

This wires Alembic to the same configuration and metadata the application uses:

* The database URL comes from ``app.config.get_settings`` — never hardcoded in
  ``alembic.ini`` — so migrations and the running app can never diverge on which
  database they target.
* ``target_metadata`` is ``Base.metadata`` with every model imported, which is
  what ``--autogenerate`` diffs against the live schema.

Because the app uses the asyncpg driver, offline mode renders SQL from the DSN
directly while online mode drives migrations through an ``AsyncEngine``.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from app.config import get_settings

# Import models for their side effect: registering tables on Base.metadata so
# autogenerate can see them. (noqa: models are used implicitly via metadata.)
from app.db import models  # noqa: F401
from app.db.database import Base

# Alembic Config object providing access to values in alembic.ini.
config = context.config

# Inject the application's DSN so there is a single source of truth.
config.set_main_option("sqlalchemy.url", str(get_settings().database_url))

# Configure Python logging from the ini file, if present.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations without a live DB connection (emits SQL to a script)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    """Run migrations against an established (sync-facing) connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Detect column type changes on autogenerate, not just add/drop.
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Create an async engine and run migrations within a connection."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
