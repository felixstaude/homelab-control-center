"""Alembic migration environment.

Reads the database URL from application settings (and therefore from the
environment) so migrations and the running application can never disagree
about which database they target, and so no credentials live in alembic.ini.

Uses the async engine, matching the application (ADR-004).
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

# Importing the models package registers every table on Base.metadata.
# Without it, autogenerate would see an empty schema and emit drop statements.
import hcc.models  # noqa: F401  # pylint: disable=unused-import
from alembic import context
from hcc.config import load_settings_or_exit
from hcc.db import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = load_settings_or_exit()
config.set_main_option("sqlalchemy.url", settings.sqlalchemy_url())

target_metadata = Base.metadata


#: Prefixes of table names Alembic must not try to manage.
#:
#: The metric sample tables are ``PARTITION BY RANGE``, and their partitions
#: are real tables in the public schema created at runtime by the maintenance
#: job (``hcc/services/metrics.py``) rather than by a migration. Autogenerate
#: reflects them, finds no model, and proposes dropping them — which would
#: delete every stored measurement, and would make `alembic check` fail on any
#: database that had ingested anything.
#:
#: Matched by prefix rather than by an exact list because the set changes
#: daily: a partition is named after the window it covers.
_PARTITION_PREFIXES = (
    "metric_samples_raw_",
    "metric_samples_5m_",
    "metric_samples_1h_",
)


def _include_object(obj, name, type_, reflected, compare_to) -> bool:  # type: ignore[no-untyped-def]
    """Exclude objects Alembic should not manage.

    Two kinds, and the second is the one that matters today:

    * TimescaleDB-created artefacts, if the retention design ever adopts them
      (ADR-008). This hook was added in phase 0 for that possibility.
    * **Runtime-created partitions** of the metric sample tables. Excluded by
      name prefix; see :data:`_PARTITION_PREFIXES`.

    Only *reflected* objects are filtered. A model whose name happened to
    match a prefix should still be managed — the filter exists to stop
    autogenerate acting on tables it found in the database and has no model
    for, not to hide anything the codebase declares.
    """
    if reflected and type_ == "table" and name.startswith(_PARTITION_PREFIXES):
        return False
    # An index on an excluded table is excluded with it. Reflected separately
    # by Alembic, so without this the partitions' primary-key indexes would be
    # proposed for dropping even though their tables were not.
    if reflected and type_ == "index":
        table = getattr(obj, "table", None)
        if table is not None and str(table.name).startswith(_PARTITION_PREFIXES):
            return False
    return True


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of applying it (``alembic upgrade --sql``)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Detect column type and server-default changes, which Alembic
        # otherwise silently ignores during autogenerate.
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Apply migrations against a live database."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        # Migrations are a short-lived one-shot process; pooling adds nothing
        # and keeps the event loop alive at exit.
        poolclass=NullPool,
    )

    try:
        async with connectable.connect() as connection:
            await connection.run_sync(_do_run_migrations)
    finally:
        await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
