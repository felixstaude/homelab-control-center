"""Shared FastAPI dependencies.

Dependencies resolve collaborators from ``app.state`` rather than from module
globals or a cached singleton. That matters for correctness, not just style:
``create_app(settings)`` accepts injected settings, so a dependency that read
the environment-backed ``get_settings()`` singleton instead would hand handlers
*different* configuration than the one the app was built with. Reading
``app.state`` makes the app the single source of truth and keeps handlers
testable without monkeypatching imports.

Note on imports: the types used inside ``Annotated`` aliases below are imported
at runtime, not under ``TYPE_CHECKING``. FastAPI resolves endpoint annotations
with ``get_type_hints`` against the *endpoint's* module namespace, so a string
forward reference here would fail to resolve in whichever router module uses
the alias.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Final

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from hcc.config import Settings
from hcc.db import Database
from hcc.errors import (
    CsrfValidationError,
    InsufficientPermissionsError,
    NotAuthenticatedError,
)
from hcc.logging import get_logger
from hcc.models.user import UserRole
from hcc.security.authentication import AuthenticationService
from hcc.security.authorization import (
    AuthorizationRequest,
    Authorizer,
    CapabilityNotAvailableError,
    CapabilityNotEnabledError,
    InsufficientRoleError,
)
from hcc.security.password import PasswordHasher
from hcc.security.sessions import AuthenticatedSession, SessionService
from hcc.security.tokens import tokens_equal

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

logger = get_logger(__name__)


def get_app_settings(request: Request) -> Settings:
    """Return the settings the application was built with."""
    settings: Settings = request.app.state.settings
    return settings


def get_database(request: Request) -> Database:
    """Return the process-wide :class:`~hcc.db.Database`."""
    database: Database = request.app.state.database
    return database


async def get_session(
    database: Annotated[Database, Depends(get_database)],
) -> AsyncIterator[AsyncSession]:
    """Yield a request-scoped database session."""
    async for session in database.session():
        yield session


def get_password_hasher(request: Request) -> PasswordHasher:
    """Return the process-wide password hasher."""
    hasher: PasswordHasher = request.app.state.password_hasher
    return hasher


def get_session_service(request: Request) -> SessionService:
    """Return the process-wide session service."""
    service: SessionService = request.app.state.session_service
    return service


def get_authentication_service(request: Request) -> AuthenticationService:
    """Return the process-wide authentication service."""
    service: AuthenticationService = request.app.state.authentication_service
    return service


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

#: HTTP methods treated as state-changing, and therefore CSRF-protected.
#: GET, HEAD and OPTIONS are excluded because they are defined to be safe —
#: and because requiring a CSRF header on GET would break ordinary navigation.
#: An endpoint that mutates state on GET is a bug, not a case to support.
_STATE_CHANGING_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _validate_origin(request: Request, settings: Settings) -> None:
    """Reject a state-changing request from an unexpected origin.

    A present ``Origin`` must match an allowed one. An absent ``Origin`` is
    allowed through on the strength of the CSRF token alone: non-browser
    clients do not send one, and browsers always do for the cross-site
    requests this guards against. Documented as a deliberate gap in
    ``hcc/api/v1/auth.py``.

    Allowed origins are the configured CORS origins plus the request's own
    host, so a same-origin deployment needs no extra configuration.
    """
    origin = request.headers.get("origin")
    if not origin:
        return

    allowed = set(settings.cors_origins)
    # Same-origin requests carry an Origin matching the request's own host,
    # which is always legitimate and must not require configuration.
    allowed.add(f"{request.url.scheme}://{request.url.netloc}")

    if origin not in allowed:
        logger.warning(
            "csrf_origin_rejected",
            # The rejected origin is attacker-controlled, so it is recorded
            # for the operator but never echoed back to the client.
            origin=origin,
            path=request.url.path,
        )
        raise CsrfValidationError


def _validate_csrf_token(
    request: Request, authenticated: AuthenticatedSession, settings: Settings
) -> None:
    """Require the session's CSRF token in the configured header.

    Compared against the value stored on the session row — a synchronizer
    token, not a bare double-submit cookie — so an attacker who can write
    cookies for a sibling subdomain still cannot forge a request. Compared in
    constant time via :func:`hcc.security.tokens.tokens_equal`.
    """
    presented = request.headers.get(settings.csrf_header_name)
    if not presented or not tokens_equal(presented, authenticated.csrf_token):
        logger.warning(
            "csrf_token_rejected",
            session_id=str(authenticated.session.id),
            # Whether a token was presented at all distinguishes a
            # misconfigured client from a forged request. The value itself is
            # never logged.
            presented=bool(presented),
            path=request.url.path,
        )
        raise CsrfValidationError


async def get_current_session(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    sessions: Annotated[SessionService, Depends(get_session_service)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> AuthenticatedSession:
    """Resolve the caller's session, enforcing CSRF on state-changing requests.

    CSRF validation lives in the same dependency as session resolution on
    purpose. Were it separate, an endpoint author could add an authenticated
    mutating route and forget it — and the omission would be invisible until
    someone exploited it. Here, asking for the current session necessarily
    gets the protection.

    Raises:
        NotAuthenticatedError: no usable session. Identical for an absent,
            malformed, expired, revoked or unknown token, so a caller cannot
            probe whether a token is live.
        CsrfValidationError: the request changes state and failed its Origin
            or CSRF token check.
    """
    authenticated = await sessions.resolve(db, request.cookies.get(settings.session_cookie_name))
    if authenticated is None:
        raise NotAuthenticatedError

    if request.method in _STATE_CHANGING_METHODS:
        _validate_origin(request, settings)
        _validate_csrf_token(request, authenticated, settings)

    return authenticated


# ---------------------------------------------------------------------------
# Authorization
#
# Separate from authentication above, deliberately: a missing session is a 401
# and an insufficient role is a 403, and a client must be able to tell "log in
# again" from "you are not allowed".
# ---------------------------------------------------------------------------


def get_authorizer(request: Request) -> Authorizer:
    """Return the process-wide authorizer."""
    authorizer: Authorizer = request.app.state.authorizer
    return authorizer


def require_min_role(
    minimum: UserRole,
) -> Callable[[AuthenticatedSession, Authorizer], Awaitable[AuthenticatedSession]]:
    """Build a dependency admitting callers at or above ``minimum``.

    Returns the authenticated session, so an endpoint needs one dependency
    rather than two:

        @router.get("/things")
        async def list_things(current: OperatorDep) -> ...:

    Roles are hierarchical (``viewer < operator < administrator``), so a
    minimum is sufficient; see :mod:`hcc.security.authorization` for why that
    is the design rather than a coincidence.

    The capability axis is not wired in here. An endpoint that needs one will
    call the authorizer directly with the target host, because the host comes
    from the request path and a dependency factory cannot know it.
    """

    async def dependency(
        current: Annotated[AuthenticatedSession, Depends(get_current_session)],
        authorizer: Annotated[Authorizer, Depends(get_authorizer)],
    ) -> AuthenticatedSession:
        try:
            await authorizer.authorize(AuthorizationRequest(session=current, minimum_role=minimum))
        except InsufficientRoleError as exc:
            # Logged with the specifics; the client is told only that it is
            # not permitted.
            logger.warning(
                "authorization_denied",
                reason="insufficient_role",
                required_role=exc.required.value,
                actual_role=exc.actual.value,
                user_id=str(current.user.id),
                # No path: the dependency has no Request, and the request id
                # already correlates this line with the access log, which
                # carries the path.
            )
            raise InsufficientPermissionsError from exc
        except (CapabilityNotEnabledError, CapabilityNotAvailableError) as exc:
            # Unreachable from this dependency, which never requests a
            # capability — but mapped so that a future caller cannot turn a
            # denial into a 500, which some clients treat as retryable.
            raise InsufficientPermissionsError from exc
        return current

    return dependency


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
DatabaseDep = Annotated[Database, Depends(get_database)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
PasswordHasherDep = Annotated[PasswordHasher, Depends(get_password_hasher)]
SessionServiceDep = Annotated[SessionService, Depends(get_session_service)]
AuthenticationServiceDep = Annotated[AuthenticationService, Depends(get_authentication_service)]
CurrentSessionDep = Annotated[AuthenticatedSession, Depends(get_current_session)]
AuthorizerDep = Annotated[Authorizer, Depends(get_authorizer)]

#: Any authenticated user. Authentication only — no role requirement beyond
#: being logged in. Named for the lowest role so that call sites read as a
#: permission level rather than as "merely authenticated".
ViewerDep = Annotated[AuthenticatedSession, Depends(require_min_role(UserRole.VIEWER))]

#: Operator or above: read access plus permitted operational actions.
OperatorDep = Annotated[AuthenticatedSession, Depends(require_min_role(UserRole.OPERATOR))]

#: Administrator only: configuration and user management.
AdminDep = Annotated[AuthenticatedSession, Depends(require_min_role(UserRole.ADMIN))]
