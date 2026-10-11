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
from contextlib import asynccontextmanager
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
    from collections.abc import AsyncIterator, Callable, Iterator
    from contextlib import AbstractAsyncContextManager

    from fastapi import FastAPI

# Credentials for a throwaway local test database. Not a secret.
_UNREACHABLE_TEST_DSN = "postgresql://hcc:hcc_test@127.0.0.1:1/hcc_test"

# apps/server — the directory holding alembic.ini.
_SERVER_DIR = Path(__file__).resolve().parent.parent

# Tables emptied between integration tests. Listed explicitly rather than
# discovered from metadata, so adding a table is a deliberate decision about
# test isolation instead of a silent change in behaviour. Order is irrelevant
# because the truncation uses CASCADE.
_TRUNCATED_TABLES = ("sessions", "users")

# Cheapest Argon2 parameters the module permits. Integration tests hash and
# verify real passwords, so the shipped 64 MiB defaults would make the suite
# slow for no added coverage — the defaults are exercised in
# tests/test_security_password.py.
_FAST_HASH_SETTINGS = {
    "password_hash_memory_cost": 19456,
    "password_hash_time_cost": 2,
    "password_hash_parallelism": 1,
}


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


# ---------------------------------------------------------------------------
# HTTP integration against the real database
#
# The `app`/`client` fixtures above point at a deliberately unreachable
# database, which is right for unit tests. Authentication tests need the real
# thing, so these build a second app wired to the migrated test database.
# ---------------------------------------------------------------------------


@pytest.fixture
def api_settings_factory(
    migrated_database: str,
) -> Callable[..., Settings]:
    """Build integration settings, overriding individual fields.

    A factory rather than a fixture value because tests need to vary the
    configuration under test — a low rate limit, a short session lifetime,
    production cookie flags — and each variation needs its own app.
    """

    def build(**overrides: object) -> Settings:
        values: dict[str, object] = {
            "environment": "test",
            "database_url": migrated_database,
            "log_level": "WARNING",
            # Generous by default so that tests which are not about
            # throttling never trip it. The throttling tests lower it.
            "login_rate_limit_per_address": 1000,
            "login_rate_limit_per_username": 1000,
            # NullPool, so every connection is closed when its session closes
            # — inside the test, while the event loop is still alive. With a
            # QueuePool, idle connections are closed by `engine.dispose()`
            # asynchronously at teardown, and pytest-asyncio tears the loop
            # down first; the sockets were then finalised by the garbage
            # collector, producing an intermittent "unclosed transport"
            # unraisable warning that `filterwarnings = ["error"]` turned into
            # a failure in an unrelated test.
            "database_pool_class": "null",
            **_FAST_HASH_SETTINGS,
        }
        values.update(overrides)
        return Settings(**values)  # type: ignore[arg-type]

    return build


@pytest.fixture
async def api_factory(
    api_settings_factory: Callable[..., Settings],
) -> AsyncIterator[Callable[..., AbstractAsyncContextManager[AsyncClient]]]:
    """Yield a factory producing HTTP clients against the real database.

    Each client runs the app's full stack — middleware, exception handlers,
    lifespan — so cookie attributes and security headers are the real ones.
    Tables are truncated after the fixture finishes, once per test.
    """
    truncate_url: str | None = None

    @asynccontextmanager
    async def build(**overrides: object) -> AsyncIterator[AsyncClient]:
        nonlocal truncate_url
        settings = api_settings_factory(**overrides)
        truncate_url = settings.sqlalchemy_url()
        app = create_app(settings)
        transport = ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=transport, base_url="http://testserver") as client,
        ):
            # Handy for tests that need to reach past HTTP, e.g. to seed a
            # user or to age a session row.
            client.app_state = app.state  # type: ignore[attr-defined]
            yield client

    try:
        yield build
    finally:
        if truncate_url is not None:
            engine = create_async_engine(truncate_url, poolclass=NullPool)
            try:
                async with engine.begin() as connection:
                    for table in _TRUNCATED_TABLES:
                        await connection.execute(text(f"TRUNCATE TABLE {table} CASCADE"))
            finally:
                await engine.dispose()
