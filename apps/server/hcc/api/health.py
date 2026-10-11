"""Health and readiness endpoints.

Two separate probes, deliberately (ADR-005):

``/healthz`` — liveness. Answers "is this process alive and serving?" It checks
nothing external. An orchestrator restarting the container because Postgres
briefly blipped would turn a recoverable dependency outage into an outage of
the whole control plane.

``/readyz`` — readiness. Answers "can this process serve traffic right now?"
It verifies the database. A failing readiness probe pulls the instance out of
the load balancer without killing it.

Both live outside the versioned API prefix: they are operational endpoints, not
product API, and their contract must stay stable across API versions.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, Field

from hcc import __version__
from hcc.api.deps import DatabaseDep, SettingsDep
from hcc.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["operations"])


class LivenessResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str = Field(description="Control plane version.")


class DependencyStatus(BaseModel):
    name: str
    status: Literal["ok", "error"]
    detail: str | None = Field(
        default=None, description="Failure reason. Present only when status is 'error'."
    )


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    version: str
    environment: str
    dependencies: list[DependencyStatus]


@router.get(
    "/healthz",
    summary="Liveness probe",
    response_model=LivenessResponse,
    status_code=status.HTTP_200_OK,
)
async def liveness() -> LivenessResponse:
    """Return 200 whenever the process is able to serve requests."""
    return LivenessResponse(version=__version__)


@router.get(
    "/readyz",
    summary="Readiness probe",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse, "description": "A dependency is unavailable."}},
)
async def readiness(
    database: DatabaseDep,
    settings: SettingsDep,
    response: Response,
) -> ReadinessResponse:
    """Verify every dependency required to serve traffic."""
    dependencies: list[DependencyStatus] = []

    try:
        await database.check_connection()
    except Exception as exc:
        # Caught, not swallowed: logged with the traceback, reported in the
        # payload, and reflected in a 503 status.
        logger.exception("readiness_database_check_failed")
        dependencies.append(
            DependencyStatus(
                name="postgresql",
                status="error",
                # Report the exception type rather than its message: the
                # message can contain the connection string, including
                # credentials.
                detail=type(exc).__name__,
            )
        )
    else:
        dependencies.append(DependencyStatus(name="postgresql", status="ok"))

    ready = all(dependency.status == "ok" for dependency in dependencies)
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(
        status="ready" if ready else "not_ready",
        version=__version__,
        environment=settings.environment,
        dependencies=dependencies,
    )
