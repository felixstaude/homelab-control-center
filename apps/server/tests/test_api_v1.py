"""API versioning and metadata tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hcc import __version__

if TYPE_CHECKING:
    from fastapi import FastAPI
    from httpx import AsyncClient


async def test_info_endpoint_is_served_under_the_v1_prefix(client: AsyncClient) -> None:
    response = await client.get("/api/v1/info")

    assert response.status_code == 200
    assert response.json() == {
        "api_version": "v1",
        "server_version": __version__,
        "environment": "test",
    }


async def test_unversioned_product_route_is_not_served(client: AsyncClient) -> None:
    # Guards against a router accidentally being mounted at the root, which
    # would create an unversioned API surface we'd have to support forever.
    response = await client.get("/api/info")

    assert response.status_code == 404


async def test_operational_probes_stay_outside_the_version_prefix(client: AsyncClient) -> None:
    # Probes must not move when the API version changes.
    assert (await client.get("/healthz")).status_code == 200
    assert (await client.get("/api/v1/healthz")).status_code == 404


async def test_openapi_schema_is_generated(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == "Homelab Control Center"
    assert "/api/v1/info" in schema["paths"]


def test_openapi_docs_are_disabled_in_production() -> None:
    from hcc.app import create_app
    from hcc.config import Settings

    production_app: FastAPI = create_app(
        Settings(
            environment="production",
            database_url="postgresql://u:p@db:5432/hcc",
        )
    )

    assert production_app.docs_url is None
    assert production_app.openapi_url is None
