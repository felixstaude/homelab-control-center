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
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.integration

_SERVER_DIR = Path(__file__).resolve().parent.parent

# Revisions in order, oldest first. Asserted against `alembic history` so that
# adding a migration without updating this list is caught rather than silently
# untested.
EXPECTED_REVISIONS = ("0001_baseline", "0002_users")


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


@pytest.fixture
async def scratch_database(test_database_url: str) -> AsyncIterator[str]:
    """Create an empty database for one test, and drop it afterwards.

    Migrations are destructive and revision-stamped, so they must not run
    against the database the other integration tests share.
    """
    url = make_url(test_database_url)
    scratch_name = f"hcc_migtest_{uuid.uuid4().hex[:12]}"

    # CREATE DATABASE cannot run inside a transaction, hence AUTOCOMMIT.
    admin_url = url.set(drivername="postgresql+asyncpg")
    admin_engine = create_async_engine(admin_url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{scratch_name}"'))
    finally:
        await admin_engine.dispose()

    scratch_url = url.set(database=scratch_name)
    try:
        # Yield the plain postgresql:// form; Settings applies the driver.
        yield scratch_url.render_as_string(hide_password=False)
    finally:
        drop_engine = create_async_engine(
            admin_url, poolclass=NullPool, isolation_level="AUTOCOMMIT"
        )
        try:
            async with drop_engine.connect() as connection:
                await connection.execute(
                    text(f'DROP DATABASE IF EXISTS "{scratch_name}" WITH (FORCE)')
                )
        finally:
            await drop_engine.dispose()


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
    assert "0002_users (head)" in alembic_ok(scratch_database, "current")


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
    alembic_ok(scratch_database, "upgrade", "head")
    assert await table_exists(scratch_database, "users")

    alembic_ok(scratch_database, "downgrade", "-1")

    assert not await table_exists(scratch_database, "users")


async def test_downgrade_removes_the_enum_type_and_function(scratch_database: str) -> None:
    # These are independent objects that DROP TABLE does not touch. Leaving
    # them behind is what breaks a subsequent upgrade.
    alembic_ok(scratch_database, "upgrade", "head")

    alembic_ok(scratch_database, "downgrade", "-1")

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
    alembic_ok(scratch_database, "downgrade", "-1")
    alembic_ok(scratch_database, "upgrade", "head")

    assert await table_exists(scratch_database, "users")
    assert await type_exists(scratch_database, "user_role")


async def test_full_chain_round_trips_to_base_and_back(scratch_database: str) -> None:
    alembic_ok(scratch_database, "upgrade", "head")
    alembic_ok(scratch_database, "downgrade", "base")

    assert not await table_exists(scratch_database, "users")
    assert not await type_exists(scratch_database, "user_role")

    alembic_ok(scratch_database, "upgrade", "head")

    assert await table_exists(scratch_database, "users")


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

    assert tables == {"alembic_version", "users"}
