"""v1 router aggregation.

Feature routers are included here as they are built.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from hcc import __version__
from hcc.api.deps import SettingsDep
from hcc.api.v1 import auth

router = APIRouter()

router.include_router(auth.router)


class ApiInfo(BaseModel):
    """Metadata describing this API version."""

    api_version: str = Field(description="API major version served under this prefix.")
    server_version: str = Field(description="Control plane release version.")
    environment: str = Field(description="Deployment environment name.")


@router.get("/info", summary="API version metadata", tags=["meta"])
async def api_info(settings: SettingsDep) -> ApiInfo:
    """Return metadata about the running control plane."""
    return ApiInfo(
        api_version="v1",
        server_version=__version__,
        environment=settings.environment,
    )
