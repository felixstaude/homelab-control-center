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

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture
async def error_client() -> AsyncIterator[AsyncClient]:
    """A minimal app whose routes raise, to exercise the handlers directly."""
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
