"""Authentication endpoints.

Phase 1, milestone 1.3. Login, logout, and the current-identity endpoint. The
HTTP concerns live here — cookies, the CSRF header, Origin validation — while
the decisions live in :mod:`hcc.security.authentication` and
:mod:`hcc.security.sessions`.

## Paths

``/api/v1/auth/login``, ``/api/v1/auth/logout``, ``/api/v1/auth/me``.

The milestone brief asked for ``/api/auth/*``. These sit under the version
prefix because ADR-006 makes every product route versioned, with only the
operational probes unversioned. An unversioned authentication surface would be
one we could never change without breaking clients.

``login`` is the only public route here and lives on its own
``public_router``; ``logout`` and ``me`` are on ``router``, which
:mod:`hcc.api.v1.router` mounts beneath a router-level authentication
dependency. Public access is therefore something a route has to be moved to
obtain, not something it gets by omission.

## Cookies

``hcc_session`` — the credential. ``HttpOnly``, ``Secure``*,
``SameSite=Lax``, ``Path=/``. ``HttpOnly`` keeps it out of reach of injected
JavaScript, which is the difference between an XSS bug and a stolen session.

``hcc_csrf`` — the double-submit token. ``Secure``*, ``SameSite=Lax``,
``Path=/``, and deliberately **not** ``HttpOnly``: the client has to read it
in order to echo it in a header. It is not a credential on its own.

\\* `Secure` is on in production and off elsewhere, so plain-HTTP local
development works without anyone disabling a security flag by hand. Disabling
it in production is refused at startup.

`SameSite=Lax` rather than `Strict`: `Strict` would drop the session cookie
when a user follows a link to the dashboard from another site — including from
a notification this product itself sent — which reads as a random logout.
`Lax` still withholds the cookie on cross-site POST, which is the CSRF-relevant
case.

No `Domain` attribute, so the cookie is host-only and is not shared with
sibling subdomains. On a homelab where other services often share a parent
domain, a host-only cookie is the safer default.

## CSRF: double-submit, bound to the session

`SameSite=Lax` already blocks the classic cross-site form POST, but it is one
browser-side control and this endpoint hands out credentials, so there is a
second, server-side check.

Every state-changing authenticated request must carry the session's CSRF token
in the `X-CSRF-Token` header. The token is generated per session, stored in the
session row, and returned by login and by `/auth/me`.

Binding it to the server-side session is what makes this a synchronizer token
rather than a plain double-submit cookie. A plain double-submit trusts that
only the real origin can set the cookie, which fails if an attacker can write
cookies for the parent domain — a realistic risk on a homelab with several
services under one domain. Comparing against the session row removes that
assumption. See ADR-030.

## Origin validation

For state-changing requests, a present `Origin` must match an allowed origin.
If `Origin` is absent the request is allowed through on the strength of the
CSRF token alone, because non-browser clients — `curl`, the test suite, a
future CLI — do not send one, and browsers always do for the requests that
matter. Stated plainly because it is a deliberate gap rather than an oversight.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from hcc.api.deps import (
    AuthenticationServiceDep,
    CurrentSessionDep,
    SessionDep,
    SessionServiceDep,
    SettingsDep,
)
from hcc.errors import InvalidCredentialsError, RateLimitedError
from hcc.logging import get_logger
from hcc.security.authentication import (
    AuthenticationFailedError,
    LoginRateLimitedError,
    LoginRequest,
)
from hcc.security.forwarded import FORWARDED_FOR_HEADER, client_address

if TYPE_CHECKING:
    from hcc.config import Settings
    from hcc.security.sessions import IssuedSession

logger = get_logger(__name__)

# Two routers, because login must be reachable without a session while
# logout and `/me` must not. The protected one is mounted by
# `hcc.api.v1.router` beneath a router-level authentication dependency, so
# nothing here can forget it. See that module for the default-deny design.
public_router = APIRouter(prefix="/auth", tags=["authentication"])
router = APIRouter(prefix="/auth", tags=["authentication"])

#: Upper bound on the username and password a client may submit. Generous
#: relative to the policy in :mod:`hcc.security.password`, and present so that
#: an unauthenticated caller cannot post a megabyte and make the server
#: normalize it.
MAX_CREDENTIAL_FIELD_LENGTH: Final = 4096


class LoginPayload(BaseModel):
    """Login request body.

    Bounded by Pydantic so oversized input is refused before any of our code
    sees it. ``model_config`` forbids extra fields: a typo'd key should be an
    error, not something silently dropped.
    """

    model_config = {"extra": "forbid"}

    username: str = Field(min_length=1, max_length=MAX_CREDENTIAL_FIELD_LENGTH)
    password: str = Field(min_length=1, max_length=MAX_CREDENTIAL_FIELD_LENGTH)

    def __repr__(self) -> str:
        """Representation with the password redacted.

        Pydantic models are rendered in validation errors and in tracebacks,
        so the default repr would print the plaintext password.
        """
        return f"LoginPayload(username={self.username!r}, password=<redacted>)"

    __str__ = __repr__


class CurrentUserResponse(BaseModel):
    """The authenticated identity.

    Carries no password material and no session token. ``csrf_token`` is
    included because the client needs it to make state-changing requests, and
    it is useless without the session cookie.
    """

    user_id: str
    username: str = Field(description="Display form, as the user entered it.")
    role: str
    is_active: bool
    session_expires_at: str = Field(description="ISO-8601 absolute session expiry.")
    csrf_token: str = Field(description="Send as the X-CSRF-Token header.")


class LogoutResponse(BaseModel):
    status: Literal["logged_out"] = "logged_out"


def _client_ip(request: Request, settings: Settings) -> str:
    """Determine the client address for throttling.

    Delegates to :func:`hcc.security.forwarded.client_address`, which trusts
    ``X-Forwarded-For`` only when the immediate peer is a configured trusted
    proxy, and then walks the chain from the right.

    Both failure modes matter here, and only one of them was handled before
    this milestone:

    * Trusting the header unconditionally lets any caller rotate its apparent
      address per request and bypass the per-address budget entirely.
    * *Ignoring* it behind a real proxy makes every client look like the
      proxy, so all users share one budget — which means one attacker can
      exhaust it and deny login to everyone.

    Neither is acceptable, so the operator declares where the proxy is.
    """
    return client_address(
        peer=request.client.host if request.client else None,
        forwarded_for=request.headers.get(FORWARDED_FOR_HEADER),
        networks=settings.trusted_proxy_networks,
    )


def _set_session_cookies(response: Response, issued: IssuedSession, settings: Settings) -> None:
    """Attach the session and CSRF cookies.

    ``max_age`` matches the session's absolute lifetime, so the browser
    discards the cookie at roughly the same moment the server stops honouring
    it. The server remains the authority either way: an expired session is
    rejected on lookup regardless of what the browser kept.
    """
    secure = settings.effective_cookie_secure
    max_age = settings.session_lifetime_seconds

    response.set_cookie(
        settings.session_cookie_name,
        issued.token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite=settings.session_cookie_samesite,
        path="/",
    )
    response.set_cookie(
        settings.csrf_cookie_name,
        issued.csrf_token,
        max_age=max_age,
        # Readable by the client on purpose: it has to echo this in a header.
        httponly=False,
        secure=secure,
        samesite=settings.session_cookie_samesite,
        path="/",
    )


def _clear_session_cookies(response: Response, settings: Settings) -> None:
    """Remove both cookies.

    Attributes must match those used when setting them, or the browser treats
    it as a different cookie and leaves the original in place.

    Clearing the browser's copy is a courtesy, not the security boundary: the
    session is already revoked server-side, so a client that ignores this and
    replays the cookie is still refused.
    """
    for name in (settings.session_cookie_name, settings.csrf_cookie_name):
        response.delete_cookie(
            name,
            path="/",
            secure=settings.effective_cookie_secure,
            samesite=settings.session_cookie_samesite,
            httponly=name == settings.session_cookie_name,
        )


@public_router.post(
    "/login",
    summary="Authenticate and start a session",
    response_model=CurrentUserResponse,
    responses={
        401: {"description": "Invalid credentials. Identical for every cause."},
        429: {"description": "Too many attempts. Carries Retry-After."},
    },
)
async def login(
    payload: LoginPayload,
    request: Request,
    response: Response,
    db: SessionDep,
    auth: AuthenticationServiceDep,
    sessions: SessionServiceDep,
    settings: SettingsDep,
) -> CurrentUserResponse:
    """Verify credentials and issue a session.

    Session fixation is impossible by construction: the token is generated
    server-side from a CSPRNG and nothing the client sends influences it. Any
    session already presented is revoked before the new one is issued, so a
    token planted in the browser before login cannot survive it.
    """
    # Revoke an existing session before issuing a new one. Without this, a
    # token an attacker had planted in the victim's browser would remain
    # valid after the victim logged in — and would then be a session
    # belonging to the victim.
    existing = await sessions.resolve(db, request.cookies.get(settings.session_cookie_name))
    if existing is not None:
        await sessions.revoke(db, existing.session.id)
        logger.info(
            "session_replaced_on_login",
            previous_session_id=str(existing.session.id),
        )

    try:
        issued = await auth.login(
            db,
            LoginRequest(
                username=payload.username,
                password=payload.password,
                client_ip=_client_ip(request, settings),
                user_agent=request.headers.get("user-agent"),
            ),
        )
    except LoginRateLimitedError as exc:
        raise RateLimitedError(exc.retry_after_seconds) from exc
    except AuthenticationFailedError as exc:
        # Every cause collapses to one response here. The specific reason was
        # already logged server-side.
        raise InvalidCredentialsError from exc

    # The session row and the revocation above are one unit of work.
    await db.commit()

    authenticated = await sessions.resolve(db, issued.token)
    if authenticated is None:  # pragma: no cover - would mean a write failed
        raise InvalidCredentialsError

    _set_session_cookies(response, issued, settings)

    return CurrentUserResponse(
        user_id=str(authenticated.user.id),
        username=authenticated.user.username,
        role=authenticated.user.role.value,
        is_active=authenticated.user.is_active,
        session_expires_at=issued.expires_at.isoformat(),
        csrf_token=issued.csrf_token,
    )


@router.post(
    "/logout",
    summary="End the current session",
    response_model=LogoutResponse,
    responses={
        401: {"description": "No valid session."},
        403: {"description": "CSRF validation failed."},
    },
)
async def logout(
    response: Response,
    db: SessionDep,
    current: CurrentSessionDep,
    sessions: SessionServiceDep,
    settings: SettingsDep,
) -> LogoutResponse:
    """Revoke the current session server-side and clear its cookies.

    Requires a valid session and a valid CSRF token: without the CSRF check, a
    cross-site request could log a user out, which is a minor but real
    nuisance attack.

    Revocation is server-side and immediate. The cookie clearing is a
    courtesy — the session is dead whether or not the client complies.
    """
    await sessions.revoke(db, current.session.id)
    await db.commit()

    _clear_session_cookies(response, settings)
    return LogoutResponse()


@router.get(
    "/me",
    summary="The authenticated identity",
    response_model=CurrentUserResponse,
    responses={401: {"description": "No valid session."}},
)
async def me(current: CurrentSessionDep) -> CurrentUserResponse:
    """Return the current user, and the CSRF token for subsequent requests.

    This is how a client that has a session cookie but no in-memory state —
    after a page reload, say — recovers both its identity and its CSRF token.
    """
    return CurrentUserResponse(
        user_id=str(current.user.id),
        username=current.user.username,
        role=current.user.role.value,
        is_active=current.user.is_active,
        session_expires_at=current.session.expires_at.isoformat(),
        csrf_token=current.csrf_token,
    )
