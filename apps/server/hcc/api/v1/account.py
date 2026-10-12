"""Self-service account endpoints.

Phase 1, milestone 1.7. What a user may do to their own account, at any role.

## Paths

    GET    /api/v1/account
    GET    /api/v1/account/sessions
    POST   /api/v1/account/sessions/revoke-others
    POST   /api/v1/account/sessions/{session_id}/revoke
    POST   /api/v1/account/password

No role gate, because every one of these is scoped to the caller by
construction: the user id comes from the resolved session and there is no
request field that names an account. That is a stronger guarantee than an
authorization check would be — there is no object-level check to forget,
because there is no object to choose.

## Why session ids are safe to expose

The session list returns ``sessions.id``, and that needs justifying rather
than assuming.

A session has two identifiers and they are not interchangeable. The
**token** is the credential: 256 bits from a CSPRNG, sent in the ``HttpOnly``
cookie, and stored only as a SHA-256 digest so a database dump yields nothing
replayable (ADR-027). The **id** is the row's ``uuid4`` primary key. It is not
derived from the token and the token cannot be derived from it; presenting an
id authenticates nothing, and the only operation it names — revocation — is
filtered by owner, so holding somebody else's id grants nothing
(:meth:`hcc.security.sessions.SessionService.revoke_for_user`).

So the id is a handle, not a secret, and no separate opaque mapping is
needed. ``token_hash`` and ``csrf_token`` are of course never returned; the
response model does not have fields for them.

## Why the password change rotates the session

Every other session is revoked and the current one is **replaced** with fresh
material, so the response carries new cookies. The reasoning is in
:mod:`hcc.services.account` and ADR-040. The HTTP consequence worth stating
here: the cookie the client sent with this very request stops working the
moment it commits, so the new ``Set-Cookie`` pair is not a convenience — it is
what keeps the user signed in.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Annotated, Final

from fastapi import APIRouter, Path, Request, Response
from pydantic import BaseModel, Field

from hcc.api.deps import (
    AccountServiceDep,
    AuditContextDep,
    CurrentSessionDep,
    SessionDep,
    SettingsDep,
)
from hcc.api.v1.auth import set_session_cookies
from hcc.errors import NotFoundError, RateLimitedError, ValidationFailedError
from hcc.logging import get_logger
from hcc.models.user import UserRole
from hcc.security.forwarded import FORWARDED_FOR_HEADER, client_address
from hcc.security.password import MAX_PASSWORD_BYTES, PasswordPolicyError
from hcc.services.account import (
    InvalidCurrentPasswordError,
    PasswordChangeRateLimitedError,
    SessionNotFoundError,
)

if TYPE_CHECKING:
    from hcc.models.session import Session

logger = get_logger(__name__)

#: Mounted on the protected router — authenticated, no role requirement.
router = APIRouter(prefix="/account", tags=["account"])

#: Error code for a current password that did not verify. A distinct code so
#: the interface can attach the message to the right field instead of showing
#: a generic validation failure next to a form with three inputs.
#: Suppression justified: this is a stable error *code* in the response
#: envelope, not a credential. Bandit matches on the variable name alone.
CODE_INVALID_CURRENT_PASSWORD: Final = "invalid_current_password"  # noqa: S105


class AccountResponse(BaseModel):
    """The caller's own account.

    Overlaps ``CurrentUserResponse`` from ``/auth/me`` and is deliberately a
    separate model. ``/auth/me`` answers "who am I, and what CSRF token should
    I send" — it is part of the authentication handshake and carries the CSRF
    token. This answers "what does my account look like", carries no token,
    and is free to grow fields that the authentication path should not.
    """

    user_id: str
    username: str
    display_name: str | None = Field(
        description="Null when the account has no separate label; render the username."
    )
    role: UserRole
    is_active: bool
    created_at: str = Field(description="ISO-8601.")
    last_login_at: str | None = Field(
        description=(
            "ISO-8601, or null if this account has never signed in. On the "
            "caller's own account this is the *previous* sign-in's timestamp "
            "only until the current one records it, which happens at login — "
            "so it reflects the current session."
        )
    )


class SessionResponse(BaseModel):
    """One of the caller's live sessions.

    Carries no token and no CSRF value — see the module docstring on why the
    id is nonetheless safe to return.
    """

    session_id: str
    is_current: bool = Field(description="True for the session this request was made with.")
    created_at: str = Field(description="ISO-8601 sign-in time.")
    last_seen_at: str = Field(
        description=(
            "ISO-8601 last activity. Advanced at most once per the configured "
            "touch interval, so it is accurate to within that interval rather "
            "than to the second."
        )
    )
    expires_at: str = Field(description="ISO-8601 absolute expiry. Never extended.")
    created_ip: str | None = Field(
        description=(
            "The client address recorded at sign-in, or null if none was "
            "available. Returned on the caller's own sessions only."
        )
    )
    user_agent: str | None = Field(
        description=(
            "The raw, truncated User-Agent header recorded at sign-in, or "
            "null when none was sent. Deliberately unparsed: a browser or "
            "operating-system label inferred from this string would be a "
            "guess presented as a fact, and would be wrong exactly for the "
            "unusual clients worth noticing."
        )
    )


class SessionListResponse(BaseModel):
    """The caller's live sessions, newest first."""

    items: list[SessionResponse]
    current_session_id: str = Field(
        description=(
            "Always present and always one of `items`, so a client never has "
            "to infer which session it is using."
        )
    )


class SessionRevocationResponse(BaseModel):
    """The outcome of a revocation."""

    sessions_revoked: int


class PasswordChangeRequest(BaseModel):
    """Change the caller's own password.

    Confirmation of the new password is deliberately **not** a field. It is a
    client-side guard against a typo in a masked input, and the server has
    nothing to compare it against that the client does not already have — so
    sending it would mean transmitting the new password twice for no gain. The
    interface enforces the match before submitting.
    """

    model_config = {"extra": "forbid"}

    current_password: str = Field(
        min_length=1,
        max_length=MAX_PASSWORD_BYTES * 4,
        description="Proof that the caller is the account's owner, not merely its session.",
    )
    new_password: str = Field(
        min_length=1,
        max_length=MAX_PASSWORD_BYTES * 4,
        description="Subject to the standard policy.",
    )

    def __repr__(self) -> str:
        """Both passwords redacted.

        This model holds two plaintext secrets and appears in validation
        errors and handler locals, which a traceback renders.
        """
        return "PasswordChangeRequest(current_password=<redacted>, new_password=<redacted>)"

    __str__ = __repr__


class PasswordChangeResponse(BaseModel):
    """What the password change did.

    No password material, and no token: the replacement session travels in
    ``Set-Cookie``, where the client cannot read it (``HttpOnly``) and does not
    need to.
    """

    status: str = Field(default="password_changed", description="Fixed marker.")
    other_sessions_revoked: int = Field(
        description=(
            "How many *other* sessions were ended. The current one was "
            "replaced rather than counted here, so zero means this was the "
            "only place the account was signed in."
        )
    )
    session_expires_at: str = Field(description="ISO-8601 expiry of the replacement session.")


def _session_response(session: Session, *, current_session_id: uuid.UUID) -> SessionResponse:
    return SessionResponse(
        session_id=str(session.id),
        is_current=session.id == current_session_id,
        created_at=session.created_at.isoformat(),
        last_seen_at=session.last_seen_at.isoformat(),
        expires_at=session.expires_at.isoformat(),
        created_ip=session.created_ip,
        user_agent=session.created_user_agent,
    )


@router.get(
    "",
    summary="The caller's own account",
    response_model=AccountResponse,
    responses={401: {"description": "No valid session."}},
)
async def get_account(current: CurrentSessionDep) -> AccountResponse:
    """Return the caller's account details.

    Reads from the user already loaded with the session, so this costs no
    additional query.
    """
    user = current.user
    return AccountResponse(
        user_id=str(user.id),
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        is_active=user.is_active,
        created_at=user.created_at.isoformat(),
        last_login_at=user.last_login_at.isoformat() if user.last_login_at else None,
    )


@router.get(
    "/sessions",
    summary="The caller's active sessions",
    response_model=SessionListResponse,
    responses={401: {"description": "No valid session."}},
)
async def list_sessions(
    db: SessionDep,
    account: AccountServiceDep,
    current: CurrentSessionDep,
) -> SessionListResponse:
    """List every session that would currently authenticate the caller.

    Only live ones. Expired and revoked rows are kept in the table so that
    "when did this session end" stays answerable, but they are not what
    "where am I signed in" means and listing them would bury the answer.
    """
    sessions = await account.list_sessions(db, user_id=current.user.id)
    return SessionListResponse(
        items=[
            _session_response(session, current_session_id=current.session.id)
            for session in sessions
        ],
        current_session_id=str(current.session.id),
    )


@router.post(
    "/sessions/revoke-others",
    summary="Sign out of every other session",
    response_model=SessionRevocationResponse,
    responses={
        401: {"description": "No valid session."},
        403: {"description": "CSRF validation failed."},
    },
)
async def revoke_other_sessions(
    db: SessionDep,
    account: AccountServiceDep,
    context: AuditContextDep,
    current: CurrentSessionDep,
) -> SessionRevocationResponse:
    """End every session except this one.

    Declared before the ``{session_id}`` route below so that ``revoke-others``
    is not captured as a session id. FastAPI matches in declaration order, and
    the parameter is typed as a UUID so the literal would in fact fall through
    with a 422 rather than being mistaken for one — but relying on a
    validation failure for correct routing is the kind of thing that breaks
    the day someone widens the type.
    """
    count = await account.revoke_other_sessions(
        db,
        user_id=current.user.id,
        current_session_id=current.session.id,
        context=context,
    )
    await db.commit()
    return SessionRevocationResponse(sessions_revoked=count)


@router.post(
    "/sessions/{session_id}/revoke",
    summary="Revoke one of the caller's sessions",
    response_model=SessionRevocationResponse,
    responses={
        401: {"description": "No valid session."},
        403: {"description": "CSRF validation failed."},
        404: {"description": "No live session of the caller's has that id."},
    },
)
async def revoke_session(
    db: SessionDep,
    account: AccountServiceDep,
    context: AuditContextDep,
    current: CurrentSessionDep,
    session_id: Annotated[uuid.UUID, Path(description="The session to revoke.")],
) -> SessionRevocationResponse:
    """End one session.

    Revoking the current session is allowed — that is a sign-out, and the
    client learns on its next request, which answers 401. Refusing it would
    mean one row in the list behaved differently from the others for no reason
    the user could see.

    A session id belonging to another account returns 404, identically to one
    that does not exist, so this cannot be used to discover whose sessions are
    live.
    """
    try:
        await account.revoke_session(
            db, user_id=current.user.id, session_id=session_id, context=context
        )
    except SessionNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc

    await db.commit()
    return SessionRevocationResponse(sessions_revoked=1)


@router.post(
    "/password",
    summary="Change the caller's own password",
    response_model=PasswordChangeResponse,
    responses={
        401: {"description": "No valid session."},
        403: {"description": "CSRF validation failed."},
        422: {
            "description": (
                "The current password did not verify, or the new one violates "
                "the policy. The two are distinguished by `error.code`."
            )
        },
        429: {"description": "Too many attempts for this account. Carries Retry-After."},
    },
)
async def change_password(
    payload: PasswordChangeRequest,
    request: Request,
    response: Response,
    db: SessionDep,
    account: AccountServiceDep,
    context: AuditContextDep,
    current: CurrentSessionDep,
    settings: SettingsDep,
) -> PasswordChangeResponse:
    """Replace the caller's password, ending every session and issuing a new one.

    Requires the current password even though the caller already holds a valid
    session. A session proves possession of a cookie; changing a credential
    should require possession of the credential, so that a stolen session
    cannot be used to lock the real owner out of their own account.

    A wrong current password is **422, never 401**. The client treats 401 as
    "your session has ended" and signs the user out, so returning it here
    would eject someone for a typo — see
    :class:`~hcc.services.account.InvalidCurrentPasswordError`.
    """
    try:
        result = await account.change_password(
            db,
            user_id=current.user.id,
            current_password=payload.current_password,
            new_password=payload.new_password,
            context=context,
            client_ip=client_address(
                peer=request.client.host if request.client else None,
                forwarded_for=request.headers.get(FORWARDED_FOR_HEADER),
                networks=settings.trusted_proxy_networks,
            ),
            user_agent=request.headers.get("user-agent"),
        )
    except PasswordChangeRateLimitedError as exc:
        raise RateLimitedError(exc.retry_after_seconds) from exc
    except InvalidCurrentPasswordError as exc:
        raise ValidationFailedError(str(exc), code=CODE_INVALID_CURRENT_PASSWORD) from exc
    except PasswordPolicyError as exc:
        raise ValidationFailedError(
            "The new password does not meet the policy.",
            details={"problems": exc.problems},
        ) from exc

    # One commit for the new hash, the revocations, the replacement session and
    # the audit event. Cookies are set only after it succeeds: handing the
    # client a token for a session that was rolled back would sign them out
    # with no explanation.
    await db.commit()
    set_session_cookies(response, result.issued, settings)

    return PasswordChangeResponse(
        other_sessions_revoked=result.other_sessions_revoked,
        session_expires_at=result.issued.expires_at.isoformat(),
    )
