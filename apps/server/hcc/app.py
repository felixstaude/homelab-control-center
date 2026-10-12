"""Application factory.

``create_app`` is a factory rather than a module-level singleton so tests can
build an app with injected settings, and so importing this module has no side
effects.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Final

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from hcc import __version__
from hcc.agents.registry import ConnectionRegistry
from hcc.api import health
from hcc.api.v1 import router as v1_router
from hcc.api.v1.router import PUBLIC_V1_PATHS
from hcc.config import Settings, get_settings
from hcc.db import Database
from hcc.errors import ErrorResponse, register_exception_handlers
from hcc.logging import configure_logging, get_logger
from hcc.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from hcc.security.audit import AuditService
from hcc.security.authentication import AuthenticationService
from hcc.security.authorization import Authorizer
from hcc.security.forwarded import describe
from hcc.security.password import build_password_hasher
from hcc.security.ratelimit import LoginThrottle, RateLimiter
from hcc.security.sessions import SessionService
from hcc.services.account import AccountService
from hcc.services.audit import AuditQueryService
from hcc.services.enrollment import AgentEnrollmentService
from hcc.services.hosts import HostService
from hcc.services.monitoring import MonitoringConfigService
from hcc.services.users import UserService

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

#: Interactive API documentation, served by FastAPI itself.
#:
#: Public, and **disabled entirely in production**: `create_app` passes
#: `docs_url=None` and `openapi_url=None` when the environment is production,
#: so these routes do not exist there at all.
#:
#: Listed because this mapping is meant to be the complete answer to "what can
#: an anonymous caller reach", and until milestone 1.5 the route scan read the
#: OpenAPI schema — which does not describe the documentation endpoints
#: themselves, so they were unlisted and untested while still being public
#: outside production.
DOCUMENTATION_PATHS: Final[dict[str, str]] = {
    "/openapi.json": (
        "The OpenAPI schema, needed by the interactive documentation and by "
        "client generation. Absent in production."
    ),
    "/docs": (
        "Swagger UI. A development convenience that widens the attack surface "
        "for no operator benefit, so it is absent in production."
    ),
    "/docs/oauth2-redirect": (
        "Swagger UI's OAuth redirect helper, mounted by FastAPI alongside "
        "/docs. Absent in production."
    ),
}

#: Every path reachable without a session, with the reason it has to be.
#:
#: Authentication is default-deny: product routes are mounted beneath a
#: router-level dependency in `hcc.api.v1.router`, so a route becomes public
#: only by being deliberately placed outside it. This mapping is the
#: human-readable record of that set, and
#: `tests/test_api_authorization.py` asserts the running application's
#: behaviour matches it exactly — so the list cannot drift from reality in
#: either direction.
PUBLIC_PATHS: Final[dict[str, str]] = {
    "/healthz": (
        "Liveness. Probed by orchestrators and container runtimes that have no "
        "session and often cannot present credentials. Returns a status and "
        "nothing else."
    ),
    "/readyz": (
        "Readiness. Same reason. Returns an aggregate status; which dependency "
        "failed goes to the log, not to an anonymous caller."
    ),
    **PUBLIC_V1_PATHS,
    **DOCUMENTATION_PATHS,
}


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
        logger.info(
            "password_hashing_configured",
            memory_cost_mib=app.state.password_hasher.parameters.memory_cost_mib,
            max_concurrency=app.state.password_hasher.max_concurrency,
            peak_memory_mib=app.state.password_hasher.peak_memory_mib,
        )
        # The request-attribution configuration decides whether per-address
        # throttling works at all, and it is silent when wrong: behind an
        # undeclared proxy every client shares one budget. Logged at startup
        # so the state is visible without reading the environment.
        logger.info(
            "request_attribution_configured",
            trusted_proxies=describe(settings.trusted_proxy_networks),
            public_origin=settings.public_origin or "derived from request",
        )
        if settings.is_production and not settings.trusted_proxies:
            logger.warning(
                "client_addresses_not_distinguished",
                detail=(
                    "HCC_TRUSTED_PROXIES is empty, so X-Forwarded-For is "
                    "ignored. Behind a reverse proxy every client appears as "
                    "the proxy and shares one login throttle budget. Set it "
                    "to the proxy's address or network."
                ),
            )
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
    # A self-reference, so anything holding the state can reach the
    # application — used by the authorization tests to enumerate routes
    # without reaching into httpx's internals.
    app.state.app = app

    # Security services. Built here rather than in the lifespan because they
    # hold no connections and tests that skip the lifespan still need them.
    # They are stateless apart from configuration, so one instance each is
    # shared across requests — which is also what makes the password hasher's
    # concurrency semaphore and the login throttle's counters process-wide
    # rather than per-request.
    app.state.password_hasher = build_password_hasher(settings)
    app.state.session_service = SessionService(settings)
    app.state.login_throttle = LoginThrottle(
        per_address=RateLimiter(
            limit=settings.login_rate_limit_per_address,
            window_seconds=settings.login_rate_limit_window_seconds,
        ),
        per_address_username=RateLimiter(
            limit=settings.login_rate_limit_per_address_username,
            window_seconds=settings.login_rate_limit_window_seconds,
        ),
        per_username=RateLimiter(
            limit=settings.login_rate_limit_per_username,
            window_seconds=settings.login_rate_limit_window_seconds,
        ),
    )
    app.state.authentication_service = AuthenticationService(
        hasher=app.state.password_hasher,
        sessions=app.state.session_service,
        throttle=app.state.login_throttle,
    )
    # Milestone 1.7. Separate from the login budgets on purpose: sharing them
    # would let a user spend the budget they need in order to sign in by
    # changing their password a few times. See hcc/config.py.
    app.state.password_change_throttle = RateLimiter(
        limit=settings.password_change_rate_limit_per_user,
        window_seconds=settings.password_change_rate_limit_window_seconds,
    )
    app.state.audit_service = AuditService()
    app.state.audit_query_service = AuditQueryService()
    app.state.user_service = UserService(
        hasher=app.state.password_hasher,
        sessions=app.state.session_service,
        audit=app.state.audit_service,
    )
    app.state.account_service = AccountService(
        hasher=app.state.password_hasher,
        sessions=app.state.session_service,
        audit=app.state.audit_service,
        throttle=app.state.password_change_throttle,
    )
    # Milestone 1.8. Both the enrollment lifetime and the heartbeat grace are
    # passed in rather than read from a module constant, so the app the tests
    # build with a two-second grace really behaves that way — a service that
    # reached for the global default would ignore the injected settings, which
    # is the bug this module's whole arrangement exists to prevent.
    app.state.host_service = HostService(
        audit=app.state.audit_service,
        enrollment_ttl_seconds=settings.agent_enrollment_ttl_seconds,
        heartbeat_grace_seconds=settings.agent_heartbeat_grace_seconds,
    )
    app.state.agent_enrollment_service = AgentEnrollmentService(audit=app.state.audit_service)
    # Phase 2. Shares the audit service rather than holding its own, so a
    # configuration change and the host mutation beside it land in one
    # transaction and one trail.
    app.state.monitoring_config_service = MonitoringConfigService(audit=app.state.audit_service)
    # Per *process*, not per application and not shared across workers. This
    # is the honest statement of a real limitation rather than a cache: the
    # connections a worker holds are its own, and connectivity is derived from
    # the database so every worker still answers the same. See
    # hcc/agents/registry.py and ADR-046.
    app.state.agent_connections = ConnectionRegistry()
    # No capability resolver: per-host capability configuration does not exist
    # yet, so the second authorization axis is unavailable and any request for
    # it is denied rather than approximated. See hcc/security/authorization.py.
    app.state.authorizer = Authorizer()

    register_exception_handlers(app)

    # Middleware is applied bottom-up, so the last one added is the outermost.
    # Security headers are added first here and therefore run innermost of the
    # three, which is what lets them apply to every response including those
    # produced by CORS preflight and by the exception handlers.
    if settings.security_headers_enabled:
        app.add_middleware(SecurityHeadersMiddleware, enable_csp=settings.effective_csp_enabled)
    app.add_middleware(RequestContextMiddleware, exclude_paths=_UNLOGGED_PATHS)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
            # The CSRF header must be allowed, or a cross-origin development
            # client could never send it and every mutation would 403.
            allow_headers=[
                "Authorization",
                "Content-Type",
                "X-Request-ID",
                settings.csrf_header_name,
            ],
            expose_headers=["X-Request-ID", "Retry-After"],
            max_age=600,
        )

    # Operational probes stay unversioned at the root.
    app.include_router(health.router)
    # Product API is versioned.
    app.include_router(v1_router, prefix=f"{settings.api_prefix}/v1")

    return app
