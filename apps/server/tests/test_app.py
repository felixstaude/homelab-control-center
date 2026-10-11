"""Application wiring tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

from hcc.app import create_app
from hcc.config import Settings

if TYPE_CHECKING:
    from fastapi import FastAPI


def test_injected_settings_are_exposed_on_app_state(settings: Settings) -> None:
    # Regression test: dependencies previously resolved settings from the
    # environment-backed get_settings() singleton, which meant handlers saw
    # different configuration than create_app() was given.
    app = create_app(settings)
    assert app.state.settings is settings


async def test_handlers_observe_injected_settings_not_the_environment() -> None:
    # The environment is not configured in this test process at all; if any
    # handler fell back to get_settings() it would raise a ValidationError and
    # return 500 instead of the injected value.
    app = create_app(
        Settings(
            environment="production",
            database_url="postgresql://u:p@db:5432/hcc",
            log_level="WARNING",
        )
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/info")

    assert response.status_code == 200
    assert response.json()["environment"] == "production"


def test_api_prefix_setting_moves_the_versioned_routes() -> None:
    app: FastAPI = create_app(
        Settings(
            environment="test",
            database_url="postgresql://u:p@db:5432/hcc",
            api_prefix="/backend",
        )
    )
    # Asserted against the OpenAPI schema rather than `app.routes`: this
    # FastAPI version keeps included routers nested instead of flattening
    # them, and the schema is the contract clients actually consume.
    paths = app.openapi()["paths"]

    assert "/backend/v1/info" in paths
    assert "/api/v1/info" not in paths
    # Probes are unversioned and unaffected by the prefix.
    assert "/healthz" in paths


def test_cors_middleware_is_absent_when_no_origins_configured() -> None:
    # Production serves the client same-origin; adding CORS with an empty
    # origin list would be dead middleware on every request.
    app = create_app(
        Settings(
            environment="production",
            database_url="postgresql://u:p@db:5432/hcc",
            cors_origins=(),
        )
    )
    middleware_names = [
        getattr(middleware.cls, "__name__", "") for middleware in app.user_middleware
    ]

    assert "CORSMiddleware" not in middleware_names
    assert "RequestContextMiddleware" in middleware_names


async def test_cors_preflight_is_honoured_for_configured_origin(settings: Settings) -> None:
    app = create_app(settings)  # fixture configures http://localhost:5173
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.options(
            "/api/v1/info",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


async def test_cors_rejects_an_unconfigured_origin(settings: Settings) -> None:
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.options(
            "/api/v1/info",
            headers={
                "Origin": "https://evil.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert "access-control-allow-origin" not in response.headers


async def test_lifespan_creates_and_disposes_the_database(settings: Settings) -> None:
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        database = app.state.database
        assert database.engine is not None

    # After shutdown the engine is released, so a later access fails loudly
    # instead of handing out a disposed engine.
    with pytest.raises(RuntimeError, match="connect"):
        _ = database.engine
