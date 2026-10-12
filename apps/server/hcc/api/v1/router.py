"""v1 router aggregation and the default-deny authentication boundary.

Feature routers are included here as they are built.

## Authentication is default-deny by construction

Two routers are assembled:

* ``_public`` — reachable without a session. Membership is deliberate, small,
  and listed in :data:`PUBLIC_V1_PATHS`.
* ``_protected`` — carries a **router-level** authentication dependency, so
  every route mounted beneath it requires a valid session whether or not its
  handler asks for one.

A new feature router is included into ``_protected``, which means forgetting
to protect it requires actively moving it to ``_public`` rather than merely
omitting a decorator. That is the point: the failure mode of a per-endpoint
dependency is silence, and an unauthenticated endpoint is not something that
should be able to appear by accident.

FastAPI caches dependency results per request, so an endpoint that also takes
``CurrentSessionDep`` or a role dependency does not resolve the session twice.

The structural arrangement is backed by a behavioural regression test:
``tests/test_api_authorization.py`` walks every path in the OpenAPI schema,
calls it without credentials, and requires a 401 unless the path is in the
allow-list. That catches a route added outside this module entirely, which no
amount of router nesting could.
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from hcc import __version__
from hcc.api.deps import SettingsDep, get_current_session
from hcc.api.v1 import auth, setup

#: Paths under ``/api/v1`` reachable without a session, with the reason.
#: Mirrored by an explicit expected set in the authorization tests, so
#: widening this list cannot pass review unnoticed.
PUBLIC_V1_PATHS: Final[dict[str, str]] = {
    "/api/v1/auth/login": (
        "Authentication has to be reachable before a session exists. Protected "
        "instead by credentials, by three throttling budgets applied before "
        "any password verification, and by a bounded request body."
    ),
    "/api/v1/setup/status": (
        "The web client must distinguish 'needs initializing' from 'log in' "
        "before any credential exists. Returns one boolean and nothing else, "
        "and is safe to disclose because there is no public path to claim an "
        "uninitialized installation — see hcc/api/v1/setup.py and ADR-036."
    ),
}

router = APIRouter()

_public = APIRouter()
_protected = APIRouter(dependencies=[Depends(get_current_session)])


class ApiInfo(BaseModel):
    """Metadata describing this API version."""

    api_version: str = Field(description="API major version served under this prefix.")
    server_version: str = Field(description="Control plane release version.")
    environment: str = Field(description="Deployment environment name.")


@_protected.get("/info", summary="API version metadata", tags=["meta"])
async def api_info(settings: SettingsDep) -> ApiInfo:
    """Return metadata about the running control plane.

    Authenticated, not public. The release version and environment name are of
    no use to a legitimate anonymous caller and of obvious use to someone
    matching the deployment against known vulnerabilities.
    """
    return ApiInfo(
        api_version="v1",
        server_version=__version__,
        environment=settings.environment,
    )


_public.include_router(auth.public_router)
_public.include_router(setup.public_router)
_protected.include_router(auth.router)

router.include_router(_public)
router.include_router(_protected)
