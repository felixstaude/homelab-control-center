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
    assert response.json() == {"status": "ok"}


async def test_liveness_does_not_disclose_the_version(client: AsyncClient) -> None:
    # Public endpoint, so it discloses as little as possible (milestone 1.4).
    # The version is available to an authenticated caller at /api/v1/info.
    response = await client.get("/healthz")

    assert "version" not in response.json()
    assert __version__ not in response.text


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
    # Degraded dependency => not ready. The failure is reported through the
    # status code rather than swallowed into a 200.
    response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


async def test_readiness_does_not_disclose_which_dependency_failed(
    client: AsyncClient,
) -> None:
    # Public endpoint, so the aggregate is the whole signal (milestone 1.4).
    # Enumerating dependencies by name and state tells an anonymous caller
    # what the deployment is built from, and when a component is down.
    response = await client.get("/readyz")
    body = response.text

    assert "postgres" not in body.lower()
    assert "dependenc" not in body.lower()
    assert "environment" not in body.lower()
    assert __version__ not in body


async def test_readiness_failure_is_logged_with_the_detail(
    client: AsyncClient,
) -> None:
    from structlog.testing import capture_logs

    # The detail did not disappear — it moved to the log, which is where an
    # operator debugging a deployment is looking anyway.
    with capture_logs() as events:
        await client.get("/readyz")

    failures = [event for event in events if event["event"] == "readiness_check_failed"]
    assert len(failures) == 1
    assert failures[0]["dependency"] == "postgresql"
    assert failures[0]["error_type"]
    # The exception *type*, never its message: a driver message can contain
    # the connection string and therefore the password.
    assert "hcc_test" not in repr(events)


async def test_readiness_does_not_leak_credentials_in_failure_detail(client: AsyncClient) -> None:
    response = await client.get("/readyz")

    # The DSN password must never appear in a client-facing response.
    assert "hcc_test" not in response.text
