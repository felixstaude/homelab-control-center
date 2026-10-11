"""Health and readiness endpoint tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hcc import __version__

if TYPE_CHECKING:
    from httpx import AsyncClient


async def test_liveness_returns_ok_without_touching_dependencies(client: AsyncClient) -> None:
    # The test settings point at an unreachable database on purpose. Liveness
    # must still answer 200 — that is the whole point of separating it from
    # readiness (ADR-005).
    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


async def test_liveness_echoes_request_id_header(client: AsyncClient) -> None:
    response = await client.get("/healthz", headers={"X-Request-ID": "trace-abc-123"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "trace-abc-123"


async def test_liveness_assigns_request_id_when_absent(client: AsyncClient) -> None:
    response = await client.get("/healthz")

    request_id = response.headers.get("X-Request-ID")
    assert request_id
    assert len(request_id) == 32  # uuid4().hex


async def test_readiness_reports_503_when_database_is_unreachable(client: AsyncClient) -> None:
    # Degraded dependency => not ready, but the error is reported explicitly
    # rather than swallowed into a 200.
    response = await client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["environment"] == "test"

    postgres = next(dep for dep in body["dependencies"] if dep["name"] == "postgresql")
    assert postgres["status"] == "error"
    assert postgres["detail"]


async def test_readiness_does_not_leak_credentials_in_failure_detail(client: AsyncClient) -> None:
    response = await client.get("/readyz")

    # The DSN password must never appear in a client-facing response.
    assert "hcc_test" not in response.text
