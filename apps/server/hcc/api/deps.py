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

Note on ``HTTPConnection``: the dependencies below take
:class:`starlette.requests.HTTPConnection`, the common base of ``Request`` and
``WebSocket``, rather than ``Request``. That is deliberate and load-bearing.
FastAPI injects ``Request`` only on HTTP routes, so a dependency annotated
``Request`` raises ``TypeError`` when the same router is used for a WebSocket
— which meant the router-level authentication dependency could not actually
authenticate a WebSocket, and a protected WebSocket route would have been
refused by an internal error rather than by an authentication decision.
Depending on the base class makes one dependency correct on both.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Final

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import HTTPConnection

from hcc.config import Settings
from hcc.db import Database
from hcc.errors import (
    CsrfValidationError,
    InsufficientPermissionsError,
    NotAuthenticatedError,
)
from hcc.logging import get_logger
from hcc.models.user import UserRole
from hcc.security.audit import AuditContext, AuditService
from hcc.security.authentication import AuthenticationService
from hcc.security.authorization import (
    AuthorizationRequest,
    Authorizer,
    CapabilityNotAvailableError,
    CapabilityNotEnabledError,
    InsufficientRoleError,
)
from hcc.security.forwarded import forwarded_scheme
from hcc.security.origins import InvalidOriginError, normalize_origin, origin_from_parts
from hcc.security.password import PasswordHasher
from hcc.security.sessions import AuthenticatedSession, SessionService
from hcc.security.tokens import tokens_equal
from hcc.services.account import AccountService
from hcc.services.audit import AuditQueryService
from hcc.services.users import UserService

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

logger = get_logger(__name__)


def get_app_settings(connection: HTTPConnection) -> Settings:
    """Return the settings the application was built with."""
    settings: Settings = connection.app.state.settings
    return settings


def get_database(connection: HTTPConnection) -> Database:
    """Return the process-wide :class:`~hcc.db.Database`."""
    database: Database = connection.app.state.database
    return database


async def get_session(
    database: Annotated[Database, Depends(get_database)],
) -> AsyncIterator[AsyncSession]:
    """Yield a request-scoped database session."""
    async for session in database.session():
        yield session


def get_password_hasher(connection: HTTPConnection) -> PasswordHasher:
    """Return the process-wide password hasher."""
    hasher: PasswordHasher = connection.app.state.password_hasher
    return hasher


def get_session_service(connection: HTTPConnection) -> SessionService:
    """Return the process-wide session service."""
    service: SessionService = connection.app.state.session_service
    return service


def get_authentication_service(connection: HTTPConnection) -> AuthenticationService:
    """Return the process-wide authentication service."""
    service: AuthenticationService = connection.app.state.authentication_service
    return service


def get_audit_service(connection: HTTPConnection) -> AuditService:
    """Return the process-wide audit service."""
    service: AuditService = connection.app.state.audit_service
    return service


def get_user_service(connection: HTTPConnection) -> UserService:
    """Return the process-wide user management service."""
    service: UserService = connection.app.state.user_service
    return service


def get_account_service(connection: HTTPConnection) -> AccountService:
    """Return the process-wide self-service account service."""
    service: AccountService = connection.app.state.account_service
    return service


def get_audit_query_service(connection: HTTPConnection) -> AuditQueryService:
    """Return the process-wide audit query service."""
    service: AuditQueryService = connection.app.state.audit_query_service
    return service


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

#: HTTP methods treated as state-changing, and therefore CSRF-protected.
#: GET, HEAD and OPTIONS are excluded because they are defined to be safe —
#: and because requiring a CSRF header on GET would break ordinary navigation.
#: An endpoint that mutates state on GET is a bug, not a case to support.
_STATE_CHANGING_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def expected_origins(connection: HTTPConnection, settings: Settings) -> set[str]:
    """The canonical origins a legitimate browser request may carry.

    Three sources, in order of authority:

    1. ``settings.public_origin`` — the origin an operator has declared this
       deployment is reached on. When set it is **authoritative**, and the
       request's own view is not consulted at all. This is the recommended
       production setting, because it depends on nothing the client sends.

    2. ``settings.cors_origins`` — explicitly allow-listed origins, for a
       cross-origin development client. Already canonicalised and
       wildcard-free by configuration validation.

    3. The request's own origin, reconstructed from its scheme and host. This
       is what makes a direct same-origin deployment work with no
       configuration. The **scheme** comes from ``X-Forwarded-Proto`` when,
       and only when, the peer is a configured trusted proxy; otherwise from
       the transport.

    Point 3 is the one that was wrong before this milestone. The old code took
    the scheme straight from the ASGI scope, so behind a TLS-terminating proxy
    it compared the browser's ``https://host`` against its own internal
    ``http://host`` and rejected every state-changing request with a CSRF
    error — naming the wrong cause and breaking the documented deployment.
    """
    allowed = set(settings.cors_origins)

    if settings.public_origin is not None:
        # Authoritative. Deliberately does not add the derived origin: an
        # operator who has stated the public origin has also stated that
        # anything else is not it, and silently accepting the internal origin
        # too would widen the check without being asked.
        allowed.add(settings.public_origin)
        return allowed

    scheme = (
        forwarded_scheme(
            peer=connection.client.host if connection.client else None,
            forwarded_proto=connection.headers.get("x-forwarded-proto"),
            networks=settings.trusted_proxy_networks,
        )
        or connection.url.scheme
    )
    derived = origin_from_parts(scheme, connection.url.netloc)
    if derived is not None:
        allowed.add(derived)
    return allowed


def _validate_origin(
    connection: HTTPConnection, settings: Settings, *, require_origin: bool
) -> None:
    """Reject a request whose ``Origin`` is absent or unexpected.

    ``require_origin`` differs by protocol, and the difference is not
    arbitrary:

    * **HTTP: not required.** An absent ``Origin`` falls back to the CSRF
      token alone. Non-browser clients — ``curl``, the test suite, a future
      CLI — send none, and browsers always send one for the cross-site
      requests this guards against. A documented, narrow gap.

    * **WebSocket: required.** A WebSocket handshake is not subject to CORS,
      so the browser will happily open a cross-origin connection and attach
      cookies; ``Origin`` is the only signal distinguishing a legitimate
      handshake from cross-site WebSocket hijacking. Browsers always send it
      on a WebSocket handshake, so requiring it costs a legitimate browser
      client nothing, and a non-browser client has no business on a
      session-authenticated socket (see the module note on authentication
      boundaries in ``hcc/api/v1/router.py``).
    """
    origin = connection.headers.get("origin")
    if not origin:
        if require_origin:
            logger.warning("csrf_origin_missing", path=connection.url.path)
            raise CsrfValidationError
        return

    try:
        presented = normalize_origin(origin)
    except InvalidOriginError:
        # A malformed Origin is never produced by a browser, so there is
        # nothing to be permissive about.
        logger.warning("csrf_origin_malformed", origin=origin, path=connection.url.path)
        raise CsrfValidationError from None

    if presented not in expected_origins(connection, settings):
        logger.warning(
            "csrf_origin_rejected",
            # The rejected origin is attacker-controlled, so it is recorded
            # for the operator but never echoed back to the client.
            origin=presented,
            path=connection.url.path,
        )
        raise CsrfValidationError


def _validate_csrf_token(
    connection: HTTPConnection, authenticated: AuthenticatedSession, settings: Settings
) -> None:
    """Require the session's CSRF token in the configured header.

    Compared against the value stored on the session row — a synchronizer
    token, not a bare double-submit cookie — so an attacker who can write
    cookies for a sibling subdomain still cannot forge a request. Compared in
    constant time via :func:`hcc.security.tokens.tokens_equal`.
    """
    presented = connection.headers.get(settings.csrf_header_name)
    if not presented or not tokens_equal(presented, authenticated.csrf_token):
        logger.warning(
            "csrf_token_rejected",
            session_id=str(authenticated.session.id),
            # Whether a token was presented at all distinguishes a
            # misconfigured client from a forged request. The value itself is
            # never logged.
            presented=bool(presented),
            path=connection.url.path,
        )
        raise CsrfValidationError


async def get_current_session(
    connection: HTTPConnection,
    db: Annotated[AsyncSession, Depends(get_session)],
    sessions: Annotated[SessionService, Depends(get_session_service)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> AuthenticatedSession:
    """Resolve the caller's session, enforcing CSRF where it applies.

    CSRF validation lives in the same dependency as session resolution on
    purpose. Were it separate, an endpoint author could add an authenticated
    mutating route and forget it — and the omission would be invisible until
    someone exploited it. Here, asking for the current session necessarily
    gets the protection.

    Three protocol cases, deliberately distinct:

    * **Safe HTTP methods** (GET, HEAD, OPTIONS) — session only. Requiring a
      CSRF header on GET would break ordinary navigation, and a GET that
      mutates state is a bug rather than a case to support.
    * **State-changing HTTP methods** — Origin (when present) and the
      session-bound CSRF token.
    * **WebSocket handshakes** — Origin, **mandatory**. A handshake carries no
      custom headers a browser can set, so the double-submit token cannot be
      presented here; the CORS-exempt nature of WebSockets makes ``Origin``
      the load-bearing check instead. How a browser socket presents a CSRF
      token after the handshake is a Phase 5 decision, recorded in
      ``hcc/api/v1/router.py``.

    Raises:
        NotAuthenticatedError: no usable session. Identical for an absent,
            malformed, expired, revoked or unknown token, so a caller cannot
            probe whether a token is live.
        CsrfValidationError: the request changes state, or is a WebSocket
            handshake, and failed its Origin or CSRF token check.
    """
    authenticated = await sessions.resolve(db, connection.cookies.get(settings.session_cookie_name))
    if authenticated is None:
        raise NotAuthenticatedError

    if connection.scope["type"] == "websocket":
        _validate_origin(connection, settings, require_origin=True)
        return authenticated

    # `method` exists on an HTTP connection only, which the branch above has
    # already excluded.
    if connection.scope["method"] in _STATE_CHANGING_METHODS:
        _validate_origin(connection, settings, require_origin=False)
        _validate_csrf_token(connection, authenticated, settings)

    return authenticated


# ---------------------------------------------------------------------------
# Authorization
#
# Separate from authentication above, deliberately: a missing session is a 401
# and an insufficient role is a 403, and a client must be able to tell "log in
# again" from "you are not allowed".
# ---------------------------------------------------------------------------


def get_authorizer(connection: HTTPConnection) -> Authorizer:
    """Return the process-wide authorizer."""
    authorizer: Authorizer = connection.app.state.authorizer
    return authorizer


def request_id_of(connection: HTTPConnection) -> str | None:
    """The correlation id :class:`~hcc.middleware.RequestContextMiddleware` set.

    ``getattr`` rather than a direct read: a request that bypassed the
    middleware — which only happens in a test that mounts a bare app — must
    produce an unattributed audit event rather than an ``AttributeError`` from
    inside an authorization check.
    """
    value = getattr(connection.state, "request_id", None)
    return str(value) if value else None


def get_audit_context(
    connection: HTTPConnection,
    current: Annotated[AuthenticatedSession, Depends(get_current_session)],
) -> AuditContext:
    """Build the audit context for an authenticated request.

    The actor comes from the **resolved session**, never from the request
    body. That is what makes an audit event's attribution trustworthy: there
    is no field a caller could set to record their action against somebody
    else, which is the audit-log equivalent of the server-side attribution
    rule for ingested data (``docs/ARCHITECTURE.md`` §7.3).
    """
    return AuditContext(
        actor_user_id=current.user.id,
        request_id=request_id_of(connection),
    )


def require_min_role(
    minimum: UserRole,
    *,
    audit_denials: bool = False,
) -> Callable[..., Awaitable[AuthenticatedSession]]:
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

    ``audit_denials`` adds a persisted record of each refusal, for the
    administrative surfaces where "a Viewer tried to list every account" is a
    security signal worth keeping rather than only a log line. It is off by
    default because the denial write has a cost and most role-gated routes do
    not warrant one; see
    :meth:`hcc.security.audit.AuditService.record_denial` for why the write
    can never convert a refusal into a success.
    """

    async def dependency(
        connection: HTTPConnection,
        current: Annotated[AuthenticatedSession, Depends(get_current_session)],
        authorizer: Annotated[Authorizer, Depends(get_authorizer)],
        audit: Annotated[AuditService, Depends(get_audit_service)],
        database: Annotated[Database, Depends(get_database)],
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
                path=connection.url.path,
            )
            if audit_denials:
                # Before raising, and in its own transaction: the request's
                # transaction is about to be rolled back, so an event added to
                # it would be the one event class that never survived.
                await audit.record_denial(
                    # A session of its own, not the request's: that one is
                    # about to be rolled back by `Database.session`.
                    session_factory=database.session_factory,
                    context=AuditContext(
                        actor_user_id=current.user.id,
                        request_id=request_id_of(connection),
                    ),
                    path=connection.url.path,
                    method=connection.scope.get("method", "WEBSOCKET"),
                    required_role=exc.required,
                    actual_role=exc.actual,
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
AuditServiceDep = Annotated[AuditService, Depends(get_audit_service)]
AuditContextDep = Annotated[AuditContext, Depends(get_audit_context)]
UserServiceDep = Annotated[UserService, Depends(get_user_service)]
AccountServiceDep = Annotated[AccountService, Depends(get_account_service)]
AuditQueryServiceDep = Annotated[AuditQueryService, Depends(get_audit_query_service)]

#: Any authenticated user. Authentication only — no role requirement beyond
#: being logged in. Named for the lowest role so that call sites read as a
#: permission level rather than as "merely authenticated".
ViewerDep = Annotated[AuthenticatedSession, Depends(require_min_role(UserRole.VIEWER))]

#: Operator or above: read access plus permitted operational actions.
OperatorDep = Annotated[AuthenticatedSession, Depends(require_min_role(UserRole.OPERATOR))]

#: Administrator only: configuration and user management.
AdminDep = Annotated[AuthenticatedSession, Depends(require_min_role(UserRole.ADMIN))]

#: Administrator only, **and** a refusal is written to the audit trail.
#:
#: Used by the user-administration and audit routers. The distinction from
#: :data:`AdminDep` is deliberate rather than an optimisation: an attempt to
#: reach user management from a Viewer or Operator session is a security
#: event — it means either a confused operator or a session being probed —
#: whereas a denial on an ordinary role-gated route is routine and would only
#: add noise. Adding the record where it carries signal keeps the trail worth
#: reading.
AuditedAdminDep = Annotated[
    AuthenticatedSession,
    Depends(require_min_role(UserRole.ADMIN, audit_denials=True)),
]
