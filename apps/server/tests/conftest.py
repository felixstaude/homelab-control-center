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
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

from hcc.app import create_app
from hcc.config import Settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from fastapi import FastAPI

# Credentials for a throwaway local test database. Not a secret.
_UNREACHABLE_TEST_DSN = "postgresql://hcc:hcc_test@127.0.0.1:1/hcc_test"


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


@pytest.fixture
def test_database_url() -> Iterator[str]:
    """The integration-test database URL, or skip the test.

    Skips with a message naming the variable to set, so a skipped integration
    test is self-explanatory.
    """
    url = os.environ.get("HCC_TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "HCC_TEST_DATABASE_URL is not set; start Postgres "
            "(`make db-up`) and export it to run integration tests"
        )
    yield url
