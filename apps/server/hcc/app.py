"""Application factory.

``create_app`` is a factory rather than a module-level singleton so tests can
build an app with injected settings, and so importing this module has no side
effects.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from hcc import __version__
from hcc.api import health
from hcc.api.v1 import router as v1_router
from hcc.config import Settings, get_settings
from hcc.db import Database
from hcc.errors import ErrorResponse, register_exception_handlers
from hcc.logging import configure_logging, get_logger
from hcc.middleware import RequestContextMiddleware

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = get_logger(__name__)

DESCRIPTION = """
Control plane API for Homelab Control Center — a self-hosted platform for
monitoring and managing Linux hosts, Docker containers and network devices.

This is a Phase 0 foundation release: the API surface is intentionally limited
to operational endpoints and version metadata.
"""

# Paths excluded from per-request access logging (polled continuously).
_UNLOGGED_PATHS = frozenset({"/healthz", "/readyz"})


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, log_format=settings.log_format)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Own the lifecycle of process-wide resources."""
        logger.info(
            "startup",
            version=__version__,
            environment=settings.environment,
            api_prefix=settings.api_prefix,
        )
        database = Database(settings)
        database.connect()
        app.state.database = database
        try:
            yield
        finally:
            # Runs even if startup of a later resource fails, so connections
            # are never leaked.
            await database.disconnect()
            logger.info("shutdown_complete")

    app = FastAPI(
        title="Homelab Control Center",
        description=DESCRIPTION.strip(),
        version=__version__,
        lifespan=lifespan,
        # Interactive docs are a development convenience. In production they
        # widen the attack surface for no benefit to an operator, so they are
        # served only outside production.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
        # Documents the uniform error envelope on every route in OpenAPI.
        responses={
            "4XX": {"model": ErrorResponse, "description": "Client error"},
            "5XX": {"model": ErrorResponse, "description": "Server error"},
        },
    )

    # Settings are available immediately, not only after lifespan startup, so
    # that dependencies resolve the same configuration the app was built with
    # even in tests that skip the lifespan.
    app.state.settings = settings

    register_exception_handlers(app)

    # Middleware is applied bottom-up: CORS must wrap the request-context
    # middleware so that preflight responses also carry CORS headers.
    app.add_middleware(RequestContextMiddleware, exclude_paths=_UNLOGGED_PATHS)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
            expose_headers=["X-Request-ID"],
            max_age=600,
        )

    # Operational probes stay unversioned at the root.
    app.include_router(health.router)
    # Product API is versioned.
    app.include_router(v1_router, prefix=f"{settings.api_prefix}/v1")

    return app
