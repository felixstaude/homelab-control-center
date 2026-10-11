"""Database integration tests.

Require a live PostgreSQL instance. Run them with:

    make db-up
    export HCC_TEST_DATABASE_URL=postgresql://hcc:hcc_local_dev@localhost:5432/hcc
    make test-integration

CI always provides a Postgres service container, so these never skip there.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

from hcc.config import Settings
from hcc.db import Database

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.integration


@pytest.fixture
async def database(test_database_url: str) -> AsyncIterator[Database]:
    """A connected Database against the real Postgres instance."""
    settings = Settings(
        environment="test",
        database_url=test_database_url,
        log_level="WARNING",
    )
    db = Database(settings)
    db.connect()
    try:
        yield db
    finally:
        await db.disconnect()


async def test_check_connection_succeeds_against_live_database(database: Database) -> None:
    # Must not raise.
    await database.check_connection()


async def test_session_executes_a_query(database: Database) -> None:
    async for session in database.session():
        result = await session.execute(text("SELECT 42 AS answer"))
        assert result.scalar_one() == 42
        break


async def test_session_rolls_back_on_error(database: Database) -> None:
    # A failed unit of work must not leave a usable-but-dirty session behind.
    with pytest.raises(RuntimeError):
        async for session in database.session():
            await session.execute(text("SELECT 1"))
            raise RuntimeError("simulated handler failure")

    # The pool is still healthy afterwards.
    await database.check_connection()


async def test_readiness_reports_ready_against_live_database(test_database_url: str) -> None:
    from httpx import ASGITransport, AsyncClient

    from hcc.app import create_app

    app = create_app(
        Settings(
            environment="test",
            database_url=test_database_url,
            log_level="WARNING",
        )
    )

    transport = ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        response = await client.get("/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert all(dep["status"] == "ok" for dep in body["dependencies"])


async def test_disconnect_is_idempotent(database: Database) -> None:
    await database.disconnect()
    await database.disconnect()  # must not raise
