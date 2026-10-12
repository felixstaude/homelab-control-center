"""Administrative user management endpoints.

Phase 1, milestone 1.7. The **first production call site for ``AdminDep``** —
until now the role dependencies were implemented and tested but nothing in the
product needed more than "logged in".

## Paths

    GET    /api/v1/users
    POST   /api/v1/users
    GET    /api/v1/users/{user_id}
    PATCH  /api/v1/users/{user_id}
    POST   /api/v1/users/{user_id}/deactivate
    POST   /api/v1/users/{user_id}/reactivate
    POST   /api/v1/users/{user_id}/sessions/revoke

Every one is Administrator-only, and the gate is on the **router** rather than
on each handler, for the same reason authentication is (ADR-031): a route
added to this module is privileged by placement, so forgetting the gate takes
an explicit move rather than an omission. Denials here are audited —
see :data:`~hcc.api.deps.AuditedAdminDep`.

## What these responses never contain

``UserResponse`` is an explicit allow-list of fields, not a serialisation of
the ORM row. So ``password_hash`` is not "excluded"; it is absent from a model
that was never given it. Also absent: ``username_canonical`` (an internal
uniqueness key — exposing it would invite clients to treat it as an
identifier), session tokens, and CSRF tokens.

The counterpart matters as much: ``UserCreateRequest`` and
``UserUpdateRequest`` both set ``extra="forbid"``, so a request naming
``password_hash``, ``id`` or ``created_at`` is **rejected** rather than having
the field quietly ignored. A silently-dropped field is how a client comes to
believe it changed something it did not.

## Why there is no delete endpoint

Deactivation is the removal mechanism (``User.is_active``), and it is not a
euphemism for a feature that is missing. A deleted row cascades its sessions
away and leaves the audit trail pointing at an id that resolves to nothing,
so "who deactivated this account" would stop being answerable about the one
account anybody asks it about. Deactivation ends access immediately — the flag
is re-checked on every request (ADR-028) — while keeping the history intact.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Annotated, Final

from fastapi import APIRouter, Path, Query, status
from pydantic import BaseModel, Field

from hcc.api.deps import (
    AuditContextDep,
    AuditedAdminDep,
    SessionDep,
    UserServiceDep,
)
from hcc.errors import ConflictError, NotFoundError, ValidationFailedError
from hcc.logging import get_logger
from hcc.models.user import (
    DISPLAY_NAME_MAX_LENGTH,
    USERNAME_MAX_LENGTH,
    USERNAME_MIN_LENGTH,
    UserRole,
)
from hcc.security.password import MAX_PASSWORD_BYTES, PasswordPolicyError
from hcc.services.users import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    MAX_SEARCH_LENGTH,
    UNSET,
    LastAdministratorError,
    UsernameTakenError,
    UserNotFoundError,
    UserUpdate,
)

if TYPE_CHECKING:
    from hcc.models.user import User
    from hcc.services.users import UserPage, UserRecord

logger = get_logger(__name__)

#: Mounted by :mod:`hcc.api.v1.router` beneath the administrator gate. The
#: router carries the dependency, so nothing here can be published by
#: forgetting a decorator.
router = APIRouter(prefix="/users", tags=["users"])

#: Upper bound on the submitted username, before normalization. Generous
#: relative to :data:`USERNAME_MAX_LENGTH`, because NFKC folding can shorten a
#: string — so a name that is legal after normalization may arrive longer than
#: the column allows, and rejecting it at the raw length would refuse a valid
#: name. The real check is `normalize_username`.
MAX_RAW_USERNAME_LENGTH: Final = 256

#: Same reasoning for the display name.
MAX_RAW_DISPLAY_NAME_LENGTH: Final = 256

#: Error codes this module returns, beyond the shared envelope's defaults.
#: Spelled out because the client branches on them.
CODE_USERNAME_TAKEN: Final = "username_taken"
CODE_LAST_ADMINISTRATOR: Final = "last_administrator"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class UserResponse(BaseModel):
    """One account, as an administrator sees it.

    An allow-list. See the module docstring for what is deliberately absent
    and why "absent" is a stronger statement than "excluded".
    """

    user_id: str
    username: str = Field(description="Display form of the login name, as it was created.")
    display_name: str | None = Field(
        description=(
            "Human-readable label, or null when the account has none. Null "
            "means 'no separate label', not 'unknown' — render the username."
        )
    )
    # The enum rather than `str`, so the closed role set reaches the OpenAPI
    # schema and a client is typed against the three roles that exist. Same
    # reasoning as `CurrentUserResponse` in auth.py.
    role: UserRole
    is_active: bool
    created_at: str = Field(description="ISO-8601 creation timestamp.")
    last_login_at: str | None = Field(
        description=(
            "ISO-8601, or null when the account has never signed in. Null "
            "must not be rendered as a date."
        )
    )
    active_session_count: int = Field(
        description=(
            "Currently usable sessions. Counts the same thing session "
            "resolution does, including the idle timeout, so a session listed "
            "here will actually work."
        )
    )

    @classmethod
    def of(cls, record: UserRecord) -> UserResponse:
        """Build a response from a service record."""
        return cls.from_user(record.user, active_session_count=record.active_session_count)

    @classmethod
    def from_user(cls, user: User, *, active_session_count: int) -> UserResponse:
        """Build a response from a row and a separately-computed count."""
        return cls(
            user_id=str(user.id),
            username=user.username,
            display_name=user.display_name,
            role=user.role,
            is_active=user.is_active,
            created_at=user.created_at.isoformat(),
            last_login_at=user.last_login_at.isoformat() if user.last_login_at else None,
            active_session_count=active_session_count,
        )


class UserListResponse(BaseModel):
    """A page of accounts, and what a pager needs."""

    items: list[UserResponse]
    total: int = Field(description="Accounts matching the filters, ignoring pagination.")
    limit: int
    offset: int


class UserCreateRequest(BaseModel):
    """Create an account.

    ``extra="forbid"``: a request naming a field this model does not declare
    is rejected. That is the mass-assignment guard at the boundary — a
    forwarded ``password_hash`` or ``id`` fails validation rather than being
    silently dropped, and a client that believed it set one finds out.
    """

    model_config = {"extra": "forbid"}

    username: str = Field(
        min_length=1,
        max_length=MAX_RAW_USERNAME_LENGTH,
        description=(
            f"{USERNAME_MIN_LENGTH} to {USERNAME_MAX_LENGTH} characters after "
            "normalization. Case-insensitively unique."
        ),
    )
    password: str = Field(
        min_length=1,
        max_length=MAX_PASSWORD_BYTES * 4,
        description="Initial password. Subject to the standard policy.",
    )
    display_name: str | None = Field(
        default=None,
        max_length=MAX_RAW_DISPLAY_NAME_LENGTH,
        description=(
            f"Optional label, up to {DISPLAY_NAME_MAX_LENGTH} characters. "
            "Blank or whitespace-only is stored as null."
        ),
    )
    role: UserRole = Field(
        default=UserRole.VIEWER,
        description=(
            "Defaults to viewer — the least privilege. An account created "
            "without an explicit role is never an administrator."
        ),
    )
    is_active: bool = Field(
        default=True, description="Whether the account may sign in immediately."
    )

    def __repr__(self) -> str:
        """Representation with the password redacted.

        Pydantic models are rendered in validation errors and in tracebacks,
        so the default would print the plaintext. Same guard as
        ``LoginPayload``.
        """
        return (
            f"UserCreateRequest(username={self.username!r}, password=<redacted>, "
            f"role={self.role!r})"
        )

    __str__ = __repr__


class UserUpdateRequest(BaseModel):
    """Change an account.

    Every field is optional, and "absent" is distinguished from "null":
    omitting ``display_name`` leaves it alone, sending ``null`` clears it.
    Pydantic cannot express that with a plain default, so
    :meth:`to_service_update` consults ``model_fields_set`` — the set of keys
    the client actually sent.

    ``username`` is deliberately **not** a field. Usernames are immutable; see
    ADR-041.
    """

    model_config = {"extra": "forbid"}

    display_name: str | None = Field(
        default=None,
        max_length=MAX_RAW_DISPLAY_NAME_LENGTH,
        description=(
            "Send a string to set it, null to clear it, or omit the key to leave it unchanged."
        ),
    )
    role: UserRole | None = Field(default=None, description="Omit to leave unchanged.")
    is_active: bool | None = Field(default=None, description="Omit to leave unchanged.")

    def to_service_update(self) -> UserUpdate:
        """Translate into the service's explicit field set.

        Field by field, never by unpacking a dict. That is what makes mass
        assignment impossible rather than merely discouraged: adding a column
        to ``users`` does not make it writable here, because there is no code
        that would carry it.
        """
        submitted = self.model_fields_set
        return UserUpdate(
            display_name=self.display_name if "display_name" in submitted else UNSET,
            # `role` and `is_active` have no meaningful null, so an explicit
            # null is treated as "unchanged" rather than rejected — there is
            # nothing a client could intend by it.
            role=self.role if "role" in submitted and self.role is not None else UNSET,
            is_active=(
                self.is_active if "is_active" in submitted and self.is_active is not None else UNSET
            ),
        )


class SessionRevocationResponse(BaseModel):
    """The outcome of revoking an account's sessions."""

    sessions_revoked: int = Field(
        description=(
            "How many live sessions were ended. Zero means the account had "
            "none — the request succeeded and changed nothing."
        )
    )


# ---------------------------------------------------------------------------
# Shared failure mapping
# ---------------------------------------------------------------------------


def _conflict(error: UsernameTakenError | LastAdministratorError) -> ConflictError:
    """Map a service refusal onto the error envelope.

    Both are 409, with **distinct codes**, because the client's response to
    each is different: a taken username is fixed by typing another one, while
    the last-administrator refusal needs an explanation of what to do instead.
    One shared ``conflict`` code would force the interface to parse the
    message, which is not a contract.
    """
    if isinstance(error, UsernameTakenError):
        return ConflictError(str(error), code=CODE_USERNAME_TAKEN)
    return ConflictError(str(error), code=CODE_LAST_ADMINISTRATOR)


def _invalid_input(error: ValueError) -> ValidationFailedError:
    """Map a normalization or policy failure onto a 422.

    ``PasswordPolicyError`` carries every problem at once, so the response
    does too — an operator fixing three issues one round trip at a time is a
    worse experience than the validation being thorough. The message and the
    ``problems`` list describe rules and limits only; neither ever contains
    the password (:mod:`hcc.security.password`).
    """
    if isinstance(error, PasswordPolicyError):
        return ValidationFailedError(
            "The password does not meet the policy.",
            details={"problems": error.problems},
        )
    return ValidationFailedError(str(error))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "",
    summary="List user accounts",
    response_model=UserListResponse,
    responses={
        401: {"description": "No valid session."},
        403: {"description": "Not an administrator."},
    },
)
async def list_users(
    db: SessionDep,
    users: UserServiceDep,
    _current: AuditedAdminDep,
    search: Annotated[
        str | None,
        Query(
            max_length=MAX_SEARCH_LENGTH,
            description=(
                "Substring match against the normalized username. Folded the "
                "same way usernames are, so 'AD' matches 'admin'."
            ),
        ),
    ] = None,
    role: Annotated[UserRole | None, Query(description="Exact role match.")] = None,
    is_active: Annotated[
        bool | None, Query(description="Filter by account status. Omit for both.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> UserListResponse:
    """Return a page of accounts.

    ``limit`` is bounded by the query validation *and* clamped again in the
    service. Two layers on purpose: this is the bound that stops an
    authenticated caller asking the server to materialise the whole table, and
    a bound enforced in only one place is one a future caller can route
    around.
    """
    page: UserPage = await users.list_users(
        db, search=search, role=role, is_active=is_active, limit=limit, offset=offset
    )
    return UserListResponse(
        items=[UserResponse.of(record) for record in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.post(
    "",
    summary="Create a user account",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        401: {"description": "No valid session."},
        403: {"description": "Not an administrator."},
        409: {"description": "The username is already taken."},
        422: {"description": "The username, display name or password is invalid."},
    },
)
async def create_user(
    payload: UserCreateRequest,
    db: SessionDep,
    users: UserServiceDep,
    context: AuditContextDep,
    _current: AuditedAdminDep,
) -> UserResponse:
    """Create an account and return it.

    The response carries **no password material** — not the plaintext that was
    submitted, not the hash, not a token. An administrator who needs to give
    the password to its owner already has it: they chose it.
    """
    try:
        user = await users.create_user(
            db,
            username=payload.username,
            password=payload.password,
            display_name=payload.display_name,
            role=payload.role,
            is_active=payload.is_active,
            context=context,
        )
    except UsernameTakenError as exc:
        raise _conflict(exc) from exc
    except ValueError as exc:
        # Covers PasswordPolicyError (a ValueError subclass) and the username
        # and display-name normalization failures.
        raise _invalid_input(exc) from exc

    # The unit of work: the user row and its audit event commit together.
    await db.commit()

    # A brand-new account has no sessions, so the count is known without a
    # query rather than being re-read for the sake of uniformity.
    return UserResponse.from_user(user, active_session_count=0)


@router.get(
    "/{user_id}",
    summary="Fetch one user account",
    response_model=UserResponse,
    responses={
        401: {"description": "No valid session."},
        403: {"description": "Not an administrator."},
        404: {"description": "No such account."},
    },
)
async def get_user(
    db: SessionDep,
    users: UserServiceDep,
    _current: AuditedAdminDep,
    user_id: Annotated[uuid.UUID, Path(description="The account's id.")],
) -> UserResponse:
    """Return one account.

    ``user_id`` is typed as a UUID, so a malformed id is a 422 from the
    framework and never reaches a query.
    """
    try:
        record = await users.get_user(db, user_id)
    except UserNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    return UserResponse.of(record)


@router.patch(
    "/{user_id}",
    summary="Change a user's display name, role or status",
    response_model=UserResponse,
    responses={
        401: {"description": "No valid session."},
        403: {"description": "Not an administrator."},
        404: {"description": "No such account."},
        409: {"description": "The change would leave no active administrator."},
        422: {"description": "The display name is invalid."},
    },
)
async def update_user(
    payload: UserUpdateRequest,
    db: SessionDep,
    users: UserServiceDep,
    context: AuditContextDep,
    _current: AuditedAdminDep,
    user_id: Annotated[uuid.UUID, Path(description="The account's id.")],
) -> UserResponse:
    """Apply an administrative change.

    Partial by design: a request naming only ``role`` leaves the display name
    and status alone. An empty body is accepted and changes nothing, which is
    the honest answer — refusing it would mean the client had to decide
    whether its own diff was empty, and a no-op is not an error.
    """
    try:
        user = await users.update_user(
            db, user_id=user_id, update=payload.to_service_update(), context=context
        )
    except UserNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    except LastAdministratorError as exc:
        raise _conflict(exc) from exc
    except ValueError as exc:
        raise _invalid_input(exc) from exc

    await db.commit()

    # Re-read the session count: deactivation revoked sessions in the same
    # transaction, so the count held before the change is stale. Cheap, and
    # the alternative is a response that contradicts the action just taken.
    record = await users.get_user(db, user.id)
    return UserResponse.of(record)


async def _set_active(
    *,
    db: SessionDep,
    users: UserServiceDep,
    context: AuditContextDep,
    user_id: uuid.UUID,
    is_active: bool,
) -> UserResponse:
    """Shared body for the deactivate and reactivate endpoints.

    Two routes, one implementation. The endpoints exist because deactivating
    an account is a distinct confirmable action rather than a field edit, but
    a second code path would be a second place for the last-administrator
    guard to be forgotten.
    """
    try:
        user = await users.set_active(db, user_id=user_id, is_active=is_active, context=context)
    except UserNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    except LastAdministratorError as exc:
        raise _conflict(exc) from exc

    await db.commit()
    record = await users.get_user(db, user.id)
    return UserResponse.of(record)


@router.post(
    "/{user_id}/deactivate",
    summary="Deactivate a user account",
    response_model=UserResponse,
    responses={
        401: {"description": "No valid session."},
        403: {"description": "Not an administrator."},
        404: {"description": "No such account."},
        409: {"description": "This is the only active administrator."},
    },
)
async def deactivate_user(
    db: SessionDep,
    users: UserServiceDep,
    context: AuditContextDep,
    _current: AuditedAdminDep,
    user_id: Annotated[uuid.UUID, Path(description="The account's id.")],
) -> UserResponse:
    """End an account's access.

    Takes effect immediately: ``is_active`` is re-checked on every request, so
    the account's next call fails whether or not its session has expired. Its
    live sessions are also revoked, so the session counts an administrator
    sees afterwards are accurate.

    Idempotent — deactivating an already-inactive account succeeds and records
    nothing.
    """
    return await _set_active(db=db, users=users, context=context, user_id=user_id, is_active=False)


@router.post(
    "/{user_id}/reactivate",
    summary="Reactivate a user account",
    response_model=UserResponse,
    responses={
        401: {"description": "No valid session."},
        403: {"description": "Not an administrator."},
        404: {"description": "No such account."},
    },
)
async def reactivate_user(
    db: SessionDep,
    users: UserServiceDep,
    context: AuditContextDep,
    _current: AuditedAdminDep,
    user_id: Annotated[uuid.UUID, Path(description="The account's id.")],
) -> UserResponse:
    """Restore an account's access.

    Does **not** restore the sessions deactivation revoked, and must not: a
    revoked token is revoked. The account signs in again, which is also the
    only way the server learns the password is still known to its owner.
    """
    return await _set_active(db=db, users=users, context=context, user_id=user_id, is_active=True)


@router.post(
    "/{user_id}/sessions/revoke",
    summary="Revoke all of a user's sessions",
    response_model=SessionRevocationResponse,
    responses={
        401: {"description": "No valid session."},
        403: {"description": "Not an administrator."},
        404: {"description": "No such account."},
    },
)
async def revoke_user_sessions(
    db: SessionDep,
    users: UserServiceDep,
    context: AuditContextDep,
    _current: AuditedAdminDep,
    user_id: Annotated[uuid.UUID, Path(description="The account's id.")],
) -> SessionRevocationResponse:
    """End every live session for an account, without changing its status.

    Distinct from deactivation: the account may sign in again straight away.
    This is the "I think that session was taken over" action, not the "this
    person has left" action.

    Returns the count, so the interface can report what happened rather than
    asserting success. A revocation that reports success without having
    revoked anything is the one thing this must not do, which is why an
    unknown id is a 404 rather than a cheerful zero.
    """
    try:
        count = await users.revoke_sessions(db, user_id=user_id, context=context)
    except UserNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc

    await db.commit()
    return SessionRevocationResponse(sessions_revoked=count)
