"""Shared pytest fixtures.

Test strategy (two tiers):

* **Unit tests** — always run, no external services. The database engine is
  created but never connected (asyncpg connects lazily), so routing, settings,
  error handling and serialisation are all testable without Postgres.
* **Integration tests** — marked ``@pytest.mark.integration`` and run only when
  ``HCC_TEST_DATABASE_URL`` is set. They skip with an explicit reason
  otherwise; CI always sets it via a Postgres service container, so they are
  never quietly skipped where it matters.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from hcc.app import create_app
from hcc.config import Settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from fastapi import FastAPI

# Credentials for a throwaway local test database. Not a secret.
_UNREACHABLE_TEST_DSN = "postgresql://hcc:hcc_test@127.0.0.1:1/hcc_test"

# apps/server — the directory holding alembic.ini.
_SERVER_DIR = Path(__file__).resolve().parent.parent

# Tables emptied between integration tests. Listed explicitly rather than
# discovered from metadata, so adding a table is a deliberate decision about
# test isolation instead of a silent change in behaviour.
_TRUNCATED_TABLES = ("users",)


@pytest.fixture
def settings() -> Settings:
    """Settings for unit tests.

    Points at a deliberately unreachable database (port 1): unit tests must
    never depend on Postgres, and a connection attempt should fail loudly
    rather than silently reach a developer's real database.
    """
    return Settings(
        environment="test",
        database_url=_UNREACHABLE_TEST_DSN,
        log_level="WARNING",
        log_format="console",
        cors_origins=("http://localhost:5173",),
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    """A FastAPI app built with test settings."""
    return create_app(settings)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """An HTTP client bound to the app via ASGI, exercising the real stack.

    Using ``ASGITransport`` with the lifespan manager means middleware,
    exception handlers and startup/shutdown all run, unlike a bare
    ``TestClient`` on the router.
    """
    transport = ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=transport, base_url="http://test") as async_client,
    ):
        yield async_client


@pytest.fixture(scope="session")
def test_database_url() -> Iterator[str]:
    """The integration-test database URL, or skip the test.

    Skips with a message naming the variable to set, so a skipped integration
    test is self-explanatory.

    Session-scoped so that :func:`migrated_database`, which is expensive, can
    depend on it and run once per test session.
    """
    url = os.environ.get("HCC_TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "HCC_TEST_DATABASE_URL is not set; start Postgres "
            "(`make db-up`) and export it to run integration tests"
        )
    yield url


@pytest.fixture(scope="session")
def migrated_database(test_database_url: str) -> str:
    """Bring the test database up to ``head`` and return its URL.

    Schema is built by running the real Alembic migrations, not by
    ``Base.metadata.create_all``. That matters: the ``updated_at`` trigger
    exists only in the migration, so ``create_all`` would produce a schema
    that differs from the one actually deployed, and the tests would pass
    against something nobody runs.

    Run as a subprocess rather than through Alembic's Python API because
    ``alembic/env.py`` calls ``asyncio.run``, which cannot be nested inside the
    event loop pytest-asyncio has already started.
    """
    # Fixed argv, no shell, no user input.
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_SERVER_DIR,
        env={**os.environ, "HCC_DATABASE_URL": test_database_url},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        # Fail loudly with the migration output; a silent skip here would hide
        # a broken migration behind "no integration tests ran".
        pytest.fail(
            "alembic upgrade head failed, so the schema under test is unknown.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return test_database_url


@pytest.fixture
async def db_session(migrated_database: str) -> AsyncIterator[AsyncSession]:
    """A session against the migrated test database, isolated per test.

    Tests using this fixture commit for real, which is the point: server
    defaults and the ``updated_at`` trigger only take effect on an actual
    write. Isolation therefore comes from truncating afterwards rather than
    from rolling back a wrapping transaction.

    A fresh engine per test with ``NullPool`` avoids binding a pooled
    connection to one test's event loop and then reusing it from another's.
    """
    settings = Settings(
        environment="test",
        database_url=migrated_database,
        log_level="WARNING",
    )
    engine = create_async_engine(settings.sqlalchemy_url(), poolclass=NullPool)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async def truncate() -> None:
        async with engine.begin() as connection:
            for table in _TRUNCATED_TABLES:
                await connection.execute(text(f"TRUNCATE TABLE {table} CASCADE"))

    try:
        # Truncate first as well: a previous crashed run must not leak rows
        # into this one.
        await truncate()
        async with factory() as session:
            yield session
        await truncate()
    finally:
        await engine.dispose()
