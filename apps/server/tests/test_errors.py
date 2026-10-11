"""Error handling tests.

These verify the uniform error envelope, which every API client depends on, and
that unexpected exceptions neither leak internals nor get swallowed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from hcc.errors import ConflictError, NotFoundError, register_exception_handlers
from hcc.logging import configure_logging

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture
async def error_client() -> AsyncIterator[AsyncClient]:
    """A minimal app whose routes raise, to exercise the handlers directly.

    Logging is configured explicitly. Without it, ``structlog`` is
    unconfigured in a fresh process and its default logger writes directly
    rather than through the stdlib bridge — so no ``LogRecord`` is produced
    and ``caplog`` sees nothing. That made
    ``test_unexpected_exception_is_logged_with_traceback`` pass only when some
    earlier test had already called ``create_app``: it passed in a full-suite
    run and failed when this file was run alone. Configuring here removes the
    ordering dependency.
    """
    configure_logging(level="INFO", log_format="console")

    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/not-found")
    async def _not_found() -> None:
        raise NotFoundError("Host 'nas-01' not found.", details={"host": "nas-01"})

    @app.get("/conflict")
    async def _conflict() -> None:
        raise ConflictError

    @app.get("/boom")
    async def _boom() -> None:
        # Simulates a genuine bug: an exception nobody anticipated.
        raise RuntimeError("database credentials: super-secret-value")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_app_error_uses_uniform_envelope(error_client: AsyncClient) -> None:
    response = await error_client.get("/not-found")

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "not_found",
            "message": "Host 'nas-01' not found.",
            "details": {"host": "nas-01"},
        }
    }


async def test_app_error_falls_back_to_class_default_message(error_client: AsyncClient) -> None:
    response = await error_client.get("/conflict")

    assert response.status_code == 409
    body = response.json()["error"]
    assert body["code"] == "conflict"
    assert body["message"]
    # exclude_none keeps absent optional members out of the payload.
    assert "details" not in body


async def test_unexpected_exception_returns_opaque_500(error_client: AsyncClient) -> None:
    response = await error_client.get("/boom")

    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": "An internal error occurred."}
    }


async def test_unexpected_exception_does_not_leak_internals(error_client: AsyncClient) -> None:
    response = await error_client.get("/boom")

    assert "super-secret-value" not in response.text
    assert "RuntimeError" not in response.text


async def test_unexpected_exception_is_logged_with_traceback(
    error_client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    # Engineering rule 6: the error is logged, never silently swallowed.
    with caplog.at_level("ERROR"):
        await error_client.get("/boom")

    assert any(record.exc_info for record in caplog.records), (
        "the catch-all handler must log the exception with its traceback"
    )


async def test_validation_error_reports_offending_fields(error_client: AsyncClient) -> None:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/items")
    async def _items(count: int) -> dict[str, int]:
        return {"count": count}

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/items", params={"count": "not-a-number"})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_failed"
    assert error["details"]["fields"][0]["location"] == "query.count"


async def test_unmatched_route_uses_the_same_envelope(error_client: AsyncClient) -> None:
    # Starlette's own 404 must be translated, so clients never see a bare
    # {"detail": ...} body from some paths and our envelope from others.
    response = await error_client.get("/no-such-route")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# Middleware-provided headers on responses that bypass the middleware
#
# Milestone 1.5. Starlette binds the `Exception` handler to
# `ServerErrorMiddleware`, the outermost layer, so a 500 produced there never
# travels back through the middleware stack. Before this milestone an
# unhandled 500 was the one response class with no security headers and no
# `X-Request-ID`, while docs/SECURITY.md claimed every response carried them.
#
# These exercise the real application through the real ASGI stack rather than
# the minimal app above, because the property under test *is* the middleware
# arrangement.
# ---------------------------------------------------------------------------

SECURITY_HEADERS = (
    "X-Content-Type-Options",
    "X-Frame-Options",
    "Referrer-Policy",
    "Permissions-Policy",
    "Cross-Origin-Opener-Policy",
)


@pytest.fixture
async def real_app_client() -> AsyncIterator[AsyncClient]:
    """The real application, plus one route that raises."""
    from fastapi import APIRouter

    from hcc.app import create_app
    from hcc.config import Settings

    app = create_app(
        Settings(
            environment="production",
            database_url="postgresql://u:p@127.0.0.1:5432/hcc",
            log_level="WARNING",
            public_origin="https://hcc.example.test",
        )
    )

    boom = APIRouter()

    @boom.get("/_boom")
    async def _boom() -> None:
        raise RuntimeError("database credentials: super-secret-value")

    app.include_router(boom)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    # The lifespan runs so that `app.state.database` exists; asyncpg connects
    # lazily, so no database is actually contacted.
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        yield client


async def test_unhandled_500_carries_the_security_headers(
    real_app_client: AsyncClient,
) -> None:
    response = await real_app_client.get("/_boom")

    assert response.status_code == 500
    missing = [header for header in SECURITY_HEADERS if header not in response.headers]
    assert not missing, f"an unhandled 500 lacks {missing}"
    # Production, so the Content-Security-Policy applies too.
    assert "content-security-policy" in response.headers


async def test_unhandled_500_carries_the_request_id_header(
    real_app_client: AsyncClient,
) -> None:
    # The response body already carries `request_id`; the header is what a
    # client correlates on, and a 500 is exactly when that matters.
    response = await real_app_client.get("/_boom", headers={"X-Request-ID": "trace-abc-123"})

    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "trace-abc-123"
    assert response.json()["error"]["request_id"] == "trace-abc-123"


async def test_handled_errors_still_carry_them(real_app_client: AsyncClient) -> None:
    # The comparison case: these travel through the middleware normally, and
    # must not have regressed.
    response = await real_app_client.get("/api/v1/info")

    assert response.status_code == 401
    for header in SECURITY_HEADERS:
        assert header in response.headers
    assert "X-Request-ID" in response.headers


async def test_unhandled_500_still_leaks_nothing(real_app_client: AsyncClient) -> None:
    response = await real_app_client.get("/_boom")

    assert "super-secret-value" not in response.text
    assert "RuntimeError" not in response.text
