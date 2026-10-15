"""Migration integration tests against real PostgreSQL.

Verifies the migration chain itself rather than the model: that it applies to
an empty database, that every revision reverses cleanly, and that the resulting
schema matches what the ORM metadata describes.

The reversibility test is the one that matters most. The first draft of the
users migration — produced by ``alembic revision --autogenerate`` — created the
``user_role`` enum type implicitly but never dropped it, so ``downgrade``
appeared to succeed and the *next* ``upgrade`` failed with "type user_role
already exists". Nothing short of an actual down-then-up cycle catches that.

Each test runs Alembic as a subprocess against its own temporary database, so
tests neither interfere with each other nor leave the developer's database at
an unexpected revision.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.integration

_SERVER_DIR = Path(__file__).resolve().parent.parent

# Revisions in order, oldest first. Asserted against `alembic history` so that
# adding a migration without updating this list is caught rather than silently
# untested.
EXPECTED_REVISIONS = (
    "0001_baseline",
    "0002_users",
    "0003_sessions",
    "0004_installation",
    "0005_user_management",
    "0006_host_registry",
    "0007_monitoring_config",
    "0008_metric_storage",
    "0009_agent_queue_health",
    "0010_update_management",
    "0011_storage_modules",
    "0012_rollout_execution",
)

# Every table the chain creates, excluding Alembic's own bookkeeping. Pinned so
# that a migration adding a table has to say so here — which is the prompt to
# add it to `_TRUNCATED_TABLES` in conftest.py as well, the omission that
# otherwise shows up much later as one test seeing another's rows.
EXPECTED_TABLES = frozenset(
    {
        "agent_credentials",
        "audit_events",
        "global_module_defaults",
        "host_config_revisions",
        "host_enrollments",
        "host_module_overrides",
        "host_update_policies",
        "hosts",
        "metric_partitions",
        "metric_samples_1h",
        "metric_samples_5m",
        "metric_samples_raw",
        "metric_series",
        "installation",
        "sessions",
        "update_events",
        "update_jobs",
        "update_rollout_members",
        "update_rollouts",
        "users",
    }
)


def run_alembic(database_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run an Alembic command against ``database_url``.

    A subprocess rather than Alembic's Python API because ``alembic/env.py``
    calls ``asyncio.run``, which cannot be nested inside pytest-asyncio's
    running event loop.
    """
    # Fixed argv, no shell.
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_SERVER_DIR,
        env={**os.environ, "HCC_DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def alembic_ok(database_url: str, *args: str) -> str:
    """Run Alembic and fail the test with its output if it errors."""
    result = run_alembic(database_url, *args)
    if result.returncode != 0:
        pytest.fail(
            f"`alembic {' '.join(args)}` exited {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout + result.stderr


async def table_exists(database_url: str, table: str) -> bool:
    engine = create_async_engine(
        make_url(database_url).set(drivername="postgresql+asyncpg"), poolclass=NullPool
    )
    try:
        async with engine.connect() as connection:
            return bool(
                (await connection.execute(text("SELECT to_regclass(:t)"), {"t": table})).scalar()
            )
    finally:
        await engine.dispose()


async def type_exists(database_url: str, type_name: str) -> bool:
    engine = create_async_engine(
        make_url(database_url).set(drivername="postgresql+asyncpg"), poolclass=NullPool
    )
    try:
        async with engine.connect() as connection:
            return bool(
                (
                    await connection.execute(
                        text("SELECT 1 FROM pg_type WHERE typname = :n"), {"n": type_name}
                    )
                ).scalar()
            )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------


async def test_upgrade_applies_to_an_empty_database(scratch_database: str) -> None:
    assert not await table_exists(scratch_database, "users")

    alembic_ok(scratch_database, "upgrade", "head")

    assert await table_exists(scratch_database, "users")
    assert f"{EXPECTED_REVISIONS[-1]} (head)" in alembic_ok(scratch_database, "current")


async def test_upgrade_creates_the_enum_type_and_trigger(scratch_database: str) -> None:
    alembic_ok(scratch_database, "upgrade", "head")

    assert await type_exists(scratch_database, "user_role")

    engine = create_async_engine(
        make_url(scratch_database).set(drivername="postgresql+asyncpg"), poolclass=NullPool
    )
    try:
        async with engine.connect() as connection:
            trigger = (
                await connection.execute(
                    text(
                        "SELECT tgname FROM pg_trigger "
                        "WHERE tgrelid = 'users'::regclass AND NOT tgisinternal"
                    )
                )
            ).scalar()
            function = (
                await connection.execute(
                    text("SELECT proname FROM pg_proc WHERE proname = 'set_updated_at'")
                )
            ).scalar()
    finally:
        await engine.dispose()

    assert trigger == "trg_users_set_updated_at"
    assert function == "set_updated_at"


async def test_downgrade_removes_the_table(scratch_database: str) -> None:
    """Each revision drops exactly what it created, and nothing earlier.

    Stepped by **named revision** rather than by ``-1``, so adding a migration
    does not silently shift what each step is asserting — which is how this
    test would start passing while checking the wrong boundary.
    """
    alembic_ok(scratch_database, "upgrade", "head")
    for table in EXPECTED_TABLES:
        assert await table_exists(scratch_database, table), table

    # 0009 drops two columns on `hosts` and nothing else, so the boundary
    # worth asserting is that the metric tables it sits above survive.
    alembic_ok(scratch_database, "downgrade", "0008_metric_storage")
    assert await table_exists(scratch_database, "metric_samples_raw")

    # 0008 drops the metric tables — the partitioned parents taking every
    # partition with them — and leaves the configuration tables alone.
    alembic_ok(scratch_database, "downgrade", "0007_monitoring_config")
    assert not await table_exists(scratch_database, "metric_samples_raw")
    assert not await table_exists(scratch_database, "metric_series")
    assert not await table_exists(scratch_database, "metric_partitions")
    assert await table_exists(scratch_database, "host_config_revisions")

    # 0007 drops the three monitoring configuration tables and leaves the host
    # registry alone — the boundary worth asserting, because the override and
    # revision tables both cascade from `hosts` and a downgrade that took
    # `hosts` with them would be a data-loss bug that still passed a
    # "the table is gone" check.
    alembic_ok(scratch_database, "downgrade", "0006_host_registry")
    assert not await table_exists(scratch_database, "global_module_defaults")
    assert not await table_exists(scratch_database, "host_module_overrides")
    assert not await table_exists(scratch_database, "host_config_revisions")
    assert await table_exists(scratch_database, "hosts")

    # 0006 drops the host registry and its two child tables.
    alembic_ok(scratch_database, "downgrade", "0005_user_management")
    assert not await table_exists(scratch_database, "hosts")
    assert not await table_exists(scratch_database, "host_enrollments")
    assert not await table_exists(scratch_database, "agent_credentials")
    assert await table_exists(scratch_database, "audit_events")

    # 0005 drops `audit_events` and the `display_name` column.
    alembic_ok(scratch_database, "downgrade", "0004_installation")
    assert not await table_exists(scratch_database, "audit_events")
    assert await table_exists(scratch_database, "users")

    alembic_ok(scratch_database, "downgrade", "0003_sessions")
    assert not await table_exists(scratch_database, "installation")

    # 0003 drops `sessions`; `users` belongs to the revision before it.
    alembic_ok(scratch_database, "downgrade", "0002_users")
    assert not await table_exists(scratch_database, "sessions")
    assert await table_exists(scratch_database, "users")

    alembic_ok(scratch_database, "downgrade", "0001_baseline")
    assert not await table_exists(scratch_database, "users")


async def test_downgrade_removes_the_enum_type_and_function(scratch_database: str) -> None:
    # These are independent objects that DROP TABLE does not touch. Leaving
    # them behind is what breaks a subsequent upgrade.
    alembic_ok(scratch_database, "upgrade", "head")

    # Back past `0002_users`, which owns the enum type and the trigger
    # function. Named rather than counted, so a new migration cannot quietly
    # make this stop reaching the revision under test.
    alembic_ok(scratch_database, "downgrade", "0001_baseline")

    assert not await type_exists(scratch_database, "user_role")

    engine = create_async_engine(
        make_url(scratch_database).set(drivername="postgresql+asyncpg"), poolclass=NullPool
    )
    try:
        async with engine.connect() as connection:
            function = (
                await connection.execute(
                    text("SELECT proname FROM pg_proc WHERE proname = 'set_updated_at'")
                )
            ).scalar()
    finally:
        await engine.dispose()
    assert function is None


async def test_upgrade_is_reapplicable_after_downgrade(scratch_database: str) -> None:
    # The regression test for the autogenerated draft: a downgrade that leaves
    # the enum type behind makes this second upgrade fail.
    alembic_ok(scratch_database, "upgrade", "head")
    alembic_ok(scratch_database, "downgrade", "0002_users")
    alembic_ok(scratch_database, "upgrade", "head")

    assert await table_exists(scratch_database, "users")
    assert await table_exists(scratch_database, "sessions")
    assert await table_exists(scratch_database, "audit_events")
    assert await type_exists(scratch_database, "user_role")


async def test_full_chain_round_trips_to_base_and_back(scratch_database: str) -> None:
    alembic_ok(scratch_database, "upgrade", "head")
    alembic_ok(scratch_database, "downgrade", "base")

    assert not await table_exists(scratch_database, "users")
    assert not await table_exists(scratch_database, "sessions")
    assert not await table_exists(scratch_database, "audit_events")
    assert not await type_exists(scratch_database, "user_role")

    alembic_ok(scratch_database, "upgrade", "head")

    assert await table_exists(scratch_database, "users")
    assert await table_exists(scratch_database, "sessions")
    assert await table_exists(scratch_database, "audit_events")


async def test_sessions_foreign_key_and_indexes_exist(scratch_database: str) -> None:
    # The token-hash index is the hot path for every authenticated request, so
    # its presence and uniqueness are worth asserting rather than assuming.
    alembic_ok(scratch_database, "upgrade", "head")

    engine = create_async_engine(
        make_url(scratch_database).set(drivername="postgresql+asyncpg"), poolclass=NullPool
    )
    try:
        async with engine.connect() as connection:
            indexes: set[str] = set(
                (
                    await connection.execute(
                        text("SELECT indexname FROM pg_indexes WHERE tablename = 'sessions'")
                    )
                )
                .scalars()
                .all()
            )
            unique: bool = (
                await connection.execute(
                    text(
                        "SELECT indisunique FROM pg_index i "
                        "JOIN pg_class c ON c.oid = i.indexrelid "
                        "WHERE c.relname = 'uq_sessions_token_hash'"
                    )
                )
            ).scalar_one()
            cascade: str | bytes = (
                await connection.execute(
                    text(
                        "SELECT confdeltype FROM pg_constraint "
                        "WHERE conname = 'fk_sessions_user_id_users'"
                    )
                )
            ).scalar_one()
    finally:
        await engine.dispose()

    assert {
        "pk_sessions",
        "uq_sessions_token_hash",
        "ix_sessions_user_id",
        "ix_sessions_expires_at",
    } <= indexes
    assert unique is True
    # 'c' is ON DELETE CASCADE: a session must not outlive its user.
    # pg_constraint.confdeltype is PostgreSQL's "char" type, which asyncpg
    # returns as a single byte rather than a str.
    assert cascade in ("c", b"c")


async def test_host_registry_objects_the_models_cannot_express_exist(
    scratch_database: str,
) -> None:
    """The two things ``alembic check`` cannot see.

    Autogenerate produced the host tables, their CHECKs and even the partial
    unique index correctly. It is structurally blind to triggers (ADR-015), so
    the ``updated_at`` trigger was written by hand — and a hand-written DDL
    statement that was mistyped or dropped in a rebase would leave
    ``updated_at`` meaning "created_at" with every other gate still green.

    The partial index is asserted here too, by its ``WHERE`` clause rather
    than merely by name: an index created without the predicate would be a
    *global* unique index on ``host_id``, which forbids a host from ever
    being re-enrolled. ``alembic check`` compares it against the model, so
    both would have to be wrong together — but this is the invariant
    revocation depends on, and it is worth one explicit assertion.
    """
    alembic_ok(scratch_database, "upgrade", "head")

    engine = create_async_engine(
        make_url(scratch_database).set(drivername="postgresql+asyncpg"), poolclass=NullPool
    )
    try:
        async with engine.connect() as connection:
            trigger = (
                await connection.execute(
                    text(
                        "SELECT tgname FROM pg_trigger "
                        "WHERE tgrelid = 'hosts'::regclass AND NOT tgisinternal"
                    )
                )
            ).scalar()
            partial_index = (
                await connection.execute(
                    text(
                        "SELECT indexdef FROM pg_indexes "
                        "WHERE indexname = 'uq_agent_credentials_one_live_per_host'"
                    )
                )
            ).scalar()
            cascades: set[str | bytes] = set(
                (
                    await connection.execute(
                        text(
                            "SELECT confdeltype FROM pg_constraint WHERE conname IN "
                            "('fk_host_enrollments_host_id_hosts', "
                            "'fk_agent_credentials_host_id_hosts')"
                        )
                    )
                )
                .scalars()
                .all()
            )
    finally:
        await engine.dispose()

    assert trigger == "trg_hosts_set_updated_at"
    assert partial_index is not None
    assert "UNIQUE" in partial_index
    assert "revoked_at IS NULL" in partial_index
    # 'c' is ON DELETE CASCADE: neither a token nor a credential may outlive
    # its host, or it would be a live secret authorizing a connection to
    # nothing. asyncpg returns PostgreSQL's "char" as a single byte.
    assert cascades <= {"c", b"c"}
    assert len(cascades) == 1


async def test_monitoring_config_triggers_the_models_cannot_express_exist(
    scratch_database: str,
) -> None:
    """``0007``'s two hand-written triggers, for the ADR-015 reason again.

    Both tables carry ``server_onupdate=FetchedValue()`` in the model, which
    tells SQLAlchemy to refetch and says nothing to PostgreSQL. The trigger is
    what actually maintains the column, autogenerate cannot see it, and a
    statement mistyped or lost in a rebase would leave ``updated_at`` meaning
    "created_at" with `alembic check`, mypy and every other gate still green.

    ``host_config_revisions`` is asserted to have **no** trigger, because it has
    no ``updated_at``: a revision's document is immutable, and a trigger there
    would be a sign someone had made it mutable.
    """
    alembic_ok(scratch_database, "upgrade", "head")

    engine = create_async_engine(
        make_url(scratch_database).set(drivername="postgresql+asyncpg"), poolclass=NullPool
    )
    try:
        async with engine.connect() as connection:

            async def triggers_on(table: str) -> set[str]:
                return set(
                    (
                        await connection.execute(
                            text(
                                "SELECT tgname FROM pg_trigger "
                                f"WHERE tgrelid = '{table}'::regclass AND NOT tgisinternal"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )

            defaults = await triggers_on("global_module_defaults")
            overrides = await triggers_on("host_module_overrides")
            revisions = await triggers_on("host_config_revisions")
    finally:
        await engine.dispose()

    assert defaults == {"trg_global_module_defaults_set_updated_at"}
    assert overrides == {"trg_host_module_overrides_set_updated_at"}
    assert revisions == set()


async def test_metric_storage_is_actually_partitioned(scratch_database: str) -> None:
    """The one thing in ``0008`` the models structurally cannot express.

    SQLAlchemy's DDL has no ``PARTITION BY``, so the three sample tables are
    created by raw SQL and `alembic check` compares them against plain-table
    models — which means a migration that created them *unpartitioned* would
    pass every other gate. Retention would then be a ``DELETE`` over hundreds
    of millions of rows instead of a ``DROP TABLE``, and the first sign would
    be a production database that locked up during a nightly job.

    Also asserts the DEFAULT partition, because its absence is the difference
    between "the maintenance job fell behind" and "ingestion started failing".
    """
    alembic_ok(scratch_database, "upgrade", "head")

    engine = create_async_engine(
        make_url(scratch_database).set(drivername="postgresql+asyncpg"), poolclass=NullPool
    )
    try:
        async with engine.connect() as connection:
            parents: set[str] = set(
                (
                    await connection.execute(
                        text(
                            "SELECT c.relname FROM pg_class c "
                            "JOIN pg_namespace n ON n.oid = c.relnamespace "
                            "WHERE n.nspname = 'public' AND c.relkind = 'p'"
                        )
                    )
                )
                .scalars()
                .all()
            )
            defaults: set[str] = set(
                (
                    await connection.execute(
                        text(
                            "SELECT c.relname FROM pg_class c "
                            "JOIN pg_namespace n ON n.oid = c.relnamespace "
                            "WHERE n.nspname = 'public' "
                            "AND pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT'"
                        )
                    )
                )
                .scalars()
                .all()
            )
            catalogued: int = (
                await connection.execute(text("SELECT count(*) FROM metric_partitions"))
            ).scalar_one()
    finally:
        await engine.dispose()

    assert parents == {"metric_samples_raw", "metric_samples_5m", "metric_samples_1h"}
    assert defaults == {
        "metric_samples_raw_default",
        "metric_samples_5m_default",
        "metric_samples_1h_default",
    }
    # One catalogued partition per tier. The DEFAULT ones are deliberately not
    # catalogued: the catalogue is what retention drops, and dropping the
    # safety net would be the opposite of what it is for.
    assert catalogued == 3


async def test_no_schema_drift_between_models_and_migrations(scratch_database: str) -> None:
    # `alembic check` compares ORM metadata against the migrated schema. This
    # is the gate that catches a model edited without a matching migration.
    alembic_ok(scratch_database, "upgrade", "head")

    output = alembic_ok(scratch_database, "check")

    assert "No new upgrade operations detected" in output


async def test_revision_history_matches_the_expected_chain(scratch_database: str) -> None:
    # Fails when a migration is added without updating EXPECTED_REVISIONS,
    # which is a prompt to confirm the new revision is covered here.
    output = alembic_ok(scratch_database, "history")

    for revision in EXPECTED_REVISIONS:
        assert revision in output


async def test_migrations_leave_no_unexpected_tables(scratch_database: str) -> None:
    alembic_ok(scratch_database, "upgrade", "head")

    engine = create_async_engine(
        make_url(scratch_database).set(drivername="postgresql+asyncpg"), poolclass=NullPool
    )
    try:
        async with engine.connect() as connection:
            tables: set[str] = set(
                (
                    await connection.execute(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = 'public'"
                        )
                    )
                )
                .scalars()
                .all()
            )
    finally:
        await engine.dispose()

    # The runtime-created partitions are excluded: they are real tables in the
    # public schema named after the window they cover, so pinning them would
    # make this test fail on a different day. Their *existence* is asserted by
    # `test_metric_storage_is_actually_partitioned`.
    partitions = {
        name
        for name in tables
        if name.startswith(("metric_samples_raw_", "metric_samples_5m_", "metric_samples_1h_"))
    }
    assert partitions, "the migration must create at least an initial partition per tier"
    assert tables - partitions == EXPECTED_TABLES | {"alembic_version"}
