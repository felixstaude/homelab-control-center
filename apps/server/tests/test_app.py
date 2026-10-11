"""Application wiring tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi import APIRouter
from httpx import ASGITransport, AsyncClient

# Runtime import: annotates a FastAPI route handler below.
from hcc.api.deps import SettingsDep  # noqa: TC001
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
    # Regression test for a real Phase 0 defect: dependencies resolved the
    # environment-backed get_settings() singleton, so handlers saw different
    # configuration than create_app() was given.
    #
    # Asserted through a test-only route rather than /api/v1/info, which has
    # required authentication since milestone 1.4. The property under test is
    # the dependency, not the endpoint. The environment is not configured in
    # this process at all, so a fallback to get_settings() would raise a
    # ValidationError and produce a 500 instead of the injected value.
    app = create_app(
        Settings(
            environment="production",
            database_url="postgresql://u:p@db:5432/hcc",
            log_level="WARNING",
            # Production requires a declared public origin (or a trusted
            # proxy) since milestone 1.5, so that the CSRF Origin check knows
            # what browsers actually send. See hcc/config.py.
            public_origin="https://hcc.example.test",
        )
    )

    probe = APIRouter()

    @probe.get("/_settings-probe")
    async def settings_probe(settings: SettingsDep) -> dict[str, str]:
        return {"environment": settings.environment}

    app.include_router(probe)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/_settings-probe")

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
            public_origin="https://hcc.example.test",
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


# ---------------------------------------------------------------------------
# Request-id handling (milestone 1.5)
#
# A client-supplied `X-Request-ID` is echoed into a response header and into
# every log line for that request, so it is attacker-controlled data crossing
# into two places other tools parse. The HTTP parser already rejects CR and LF
# in a request header (verified against a real server), so log-line forgery is
# not reachable; an allow-list bounds the rest.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "supplied",
    [
        "simple-id",
        "0123456789abcdef",
        "trace.id_with-separators",
        "a" * 128,
    ],
)
async def test_a_well_formed_request_id_is_propagated(client: AsyncClient, supplied: str) -> None:
    # Correlation across a proxy and the API depends on propagation, so a
    # legitimate value must survive unchanged.
    response = await client.get("/healthz", headers={"X-Request-ID": supplied})

    assert response.headers["X-Request-ID"] == supplied


@pytest.mark.parametrize(
    "supplied",
    [
        "",
        "a" * 129,
        "has spaces",
        "<script>alert(1)</script>",
        "tab\there",
        "semi;colon",
        "slash/es",
    ],
)
async def test_an_invalid_request_id_is_replaced(client: AsyncClient, supplied: str) -> None:
    # Replaced, not rejected: correlation is a diagnostic aid, and failing a
    # request because its trace header was odd would turn an observability
    # nicety into an outage.
    response = await client.get("/healthz", headers={"X-Request-ID": supplied})

    echoed = response.headers["X-Request-ID"]
    assert echoed != supplied
    # A generated id: 32 hex characters.
    assert len(echoed) == 32
    assert all(character in "0123456789abcdef" for character in echoed)


async def test_a_request_id_is_generated_when_absent(client: AsyncClient) -> None:
    response = await client.get("/healthz")

    assert len(response.headers["X-Request-ID"]) == 32


@pytest.mark.parametrize(
    "supplied",
    ["unicode-é", "null\x00byte", "newline\ninjected", "carriage\rreturn"],
)
def test_the_resolver_replaces_values_a_client_could_never_send(supplied: str) -> None:
    # These cannot reach the middleware over HTTP — httpx refuses to encode a
    # non-ASCII header value, and a compliant server rejects CR or LF in a
    # request header with 400 (verified against uvicorn). The resolver is
    # tested directly so the allow-list is pinned regardless of which client
    # or server sits in front of it.
    from hcc.middleware import _resolve_request_id

    resolved = _resolve_request_id(supplied)

    assert resolved != supplied
    assert len(resolved) == 32


def test_the_resolver_keeps_a_valid_value() -> None:
    from hcc.middleware import _resolve_request_id

    assert _resolve_request_id("valid-trace-id.1_2") == "valid-trace-id.1_2"
    assert _resolve_request_id(None) != _resolve_request_id(None)
