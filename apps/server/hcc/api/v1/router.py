"""v1 router aggregation.

Feature routers are included here as they are built. Phase 0 exposes only a
metadata endpoint — enough to prove versioning, routing, response models and
OpenAPI generation work end to end, without shipping product behaviour.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from hcc import __version__
from hcc.api.deps import SettingsDep

router = APIRouter()


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
