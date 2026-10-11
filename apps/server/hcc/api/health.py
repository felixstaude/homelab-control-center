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

## These are the only unauthenticated endpoints outside login

They have to be. An orchestrator, a reverse proxy health check or a container
runtime probes them before any session could exist, and many cannot present
credentials at all.

Because they are public, **they disclose as little as possible** — amended in
milestone 1.4 (see ADR-005):

* ``/healthz`` returns ``{"status": "ok"}`` and nothing else. It previously
  returned the release version, which is of no use to a legitimate anonymous
  caller and of obvious use to someone matching the deployment against known
  vulnerabilities.
* ``/readyz`` returns an aggregate status and the HTTP code. It previously
  enumerated each dependency by name and state, which told an anonymous caller
  what the deployment is built from and when a component was down — a useful
  signal for timing an attack.

The detail did not disappear: a failed readiness check still logs the
dependency and the exception type server-side, which is where an operator
debugging a deployment is looking anyway. The version and environment moved to
``GET /api/v1/info``, which requires a session.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from hcc.api.deps import DatabaseDep
from hcc.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["operations"])


class LivenessResponse(BaseModel):
    """Deliberately minimal: this endpoint is public."""

    status: Literal["ok"] = "ok"


class ReadinessResponse(BaseModel):
    """Aggregate only: this endpoint is public.

    Which dependency failed, and why, is logged server-side rather than
    returned — see the module docstring.
    """

    status: Literal["ready", "not_ready"]


@router.get(
    "/healthz",
    summary="Liveness probe",
    response_model=LivenessResponse,
    status_code=status.HTTP_200_OK,
)
async def liveness() -> LivenessResponse:
    """Return 200 whenever the process is able to serve requests.

    Public and intentionally uninformative. The version is available to an
    authenticated caller at ``GET /api/v1/info``.
    """
    return LivenessResponse()


@router.get(
    "/readyz",
    summary="Readiness probe",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse, "description": "A dependency is unavailable."}},
)
async def readiness(
    database: DatabaseDep,
    response: Response,
) -> ReadinessResponse:
    """Verify every dependency required to serve traffic.

    Public, so the response is an aggregate. Which dependency failed and why
    goes to the log, not to an anonymous caller.
    """
    ready = True

    try:
        await database.check_connection()
    except Exception as exc:
        # Caught, not swallowed: logged with the traceback and reflected in a
        # 503. The exception *type* is logged rather than its message, because
        # a driver message can contain the connection string and therefore the
        # password.
        logger.exception(
            "readiness_check_failed",
            dependency="postgresql",
            error_type=type(exc).__name__,
        )
        ready = False

    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(status="ready" if ready else "not_ready")
