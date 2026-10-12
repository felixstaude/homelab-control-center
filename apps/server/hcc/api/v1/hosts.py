"""Host registry endpoints.

Phase 1, milestone 1.8.

## Paths

    GET    /api/v1/hosts
    POST   /api/v1/hosts
    GET    /api/v1/hosts/{host_id}
    PATCH  /api/v1/hosts/{host_id}
    POST   /api/v1/hosts/{host_id}/disable
    POST   /api/v1/hosts/{host_id}/enable
    POST   /api/v1/hosts/{host_id}/enrollment
    POST   /api/v1/hosts/{host_id}/enrollment/revoke
    POST   /api/v1/hosts/{host_id}/credential/revoke

## The reads are not administrator-only, and the writes are

This is the first router in the application whose routes are **split across
two gates**, and the split is the product decision rather than an
implementation detail: every role can see the fleet, and only an administrator
can change it.

A Viewer who cannot list hosts cannot use a monitoring product. An Operator who
could mint enrollment credentials could enroll a machine of their own choosing
and start feeding the installation fabricated metrics, which is a privilege
escalation dressed as a convenience. So the two read endpoints are mounted on
the protected router and the seven mutations on the administrator router —
placement, not decorators, for the reason ADR-031 gives.

The reads are therefore in a module whose other routes are privileged, which
is the one arrangement that could go wrong by accident. ``hcc/api/v1/router.py``
mounts ``read_router`` and ``router`` separately and
``tests/test_api_authorization.py`` walks every published path without
credentials, so a mutation that drifted into the read router would be caught by
a test rather than by a reader.

## What these responses never contain

``HostResponse`` is an explicit allow-list, so the enrollment credential, the
agent credential and both of their digests are not "excluded" — they are
absent from a model that was never given them. ``name_canonical`` is absent
too: it is an internal uniqueness key, and exposing it would invite clients to
treat it as an identifier.

The enrollment credential appears in exactly one response body, that of
``POST /hosts/{host_id}/enrollment``, and never again. There is deliberately no
``GET`` that returns it: only its digest is stored (ADR-042), so no later
request *could* produce it, and the interface says so rather than offering a
"show again" control that would have to lie.

It is also never in a URL. The credential travels in a response body on the
way out and in an ``Authorization`` header on the way back in
(:mod:`hcc.api.v1.agents`). A path or query parameter would put it in the
browser's history, in the server's access log, and in the ``Referer`` header of
whatever the operator clicked next.

## Why there is no delete endpoint

Not an omission, and not deferred. Deleting a host cascades its enrollment
rows and credentials away, and the audit trail then points at an id that
resolves to nothing — so "who enrolled this machine, and when was its
credential revoked" would stop being answerable about precisely the host
somebody is asking about. Disabling ends the agent's access immediately and
keeps the history, which is what the operation is actually for. A host that
genuinely needs to disappear is a later, deliberate feature with a
confirmation and a retention story, not a ``DELETE`` added for symmetry.
"""

from __future__ import annotations

import uuid

# Imported at runtime: FastAPI resolves endpoint annotations with
# `get_type_hints`, and `_iso` is annotated with it.
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Final

from fastapi import APIRouter, Path, Query, status
from pydantic import BaseModel, Field

from hcc.api.deps import (
    AuditContextDep,
    AuditedAdminDep,
    HostServiceDep,
    SessionDep,
    ViewerDep,
)
from hcc.errors import ConflictError, NotFoundError, ValidationFailedError
from hcc.logging import get_logger
from hcc.models.host import (
    HOST_DESCRIPTION_MAX_LENGTH,
    HOST_ENVIRONMENT_MAX_LENGTH,
    HOST_NAME_MAX_LENGTH,
    HOST_NAME_MIN_LENGTH,
    HostConnectivity,
    HostLifecycle,
)
from hcc.services.hosts import (
    DEFAULT_PAGE_SIZE,
    MAX_OFFSET,
    MAX_PAGE_SIZE,
    MAX_SEARCH_LENGTH,
    UNSET,
    HostAlreadyEnrolledError,
    HostNameTakenError,
    HostNotFoundError,
    HostUpdate,
    NoLiveCredentialError,
)

if TYPE_CHECKING:
    from hcc.services.hosts import HostPage, HostRecord, IssuedEnrollment

logger = get_logger(__name__)

#: Administrator-only mutations. Mounted by :mod:`hcc.api.v1.router` beneath
#: the administrator gate, so nothing here can be published to every
#: authenticated user by forgetting a decorator.
router = APIRouter(prefix="/hosts", tags=["hosts"])

#: Reads, available to any authenticated role. A **separate** router object,
#: mounted under a different gate. The separation is the whole mechanism: a
#: route's privilege follows from which of these two it is attached to.
read_router = APIRouter(prefix="/hosts", tags=["hosts"])

#: Upper bound on the submitted name, before normalization. Generous relative
#: to :data:`HOST_NAME_MAX_LENGTH`, because NFKC folding and whitespace
#: collapse can shorten a string — so a name that is legal after normalization
#: may arrive longer than the column allows, and rejecting it at the raw length
#: would refuse a valid name. The real check is `normalize_host_name`.
MAX_RAW_NAME_LENGTH: Final = 256

#: Same reasoning for the two optional labels.
MAX_RAW_DESCRIPTION_LENGTH: Final = 2048
MAX_RAW_ENVIRONMENT_LENGTH: Final = 128

#: Error codes this module returns beyond the shared envelope's defaults.
#: Spelled out because the client branches on them.
CODE_HOST_NAME_TAKEN: Final = "host_name_taken"
CODE_HOST_ALREADY_ENROLLED: Final = "host_already_enrolled"
CODE_NO_LIVE_CREDENTIAL: Final = "no_live_credential"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class HostResponse(BaseModel):
    """One host, as the interface sees it.

    An allow-list. See the module docstring for what is deliberately absent
    and why "absent" is a stronger statement than "excluded".
    """

    host_id: str
    name: str = Field(description="Display form of the name, as it was entered.")
    description: str | None = Field(
        description=(
            "Operator's note, or null when there is none. Null means 'no note', not 'unknown'."
        )
    )
    environment: str | None = Field(
        description="Free-text grouping label for the operator's own use, or null."
    )
    # The enums rather than `str`, so the closed sets reach the OpenAPI schema
    # and a client is typed against the states that exist.
    lifecycle: HostLifecycle = Field(
        description="How far through enrollment this host is. Independent of connectivity."
    )
    is_enabled: bool = Field(
        description=(
            "Whether the operator currently wants this host connected. A "
            "disabled host keeps its enrollment and its history; its agent is "
            "refused at the door."
        )
    )
    connectivity: HostConnectivity = Field(
        description=(
            "Derived from whether a connection is recorded and how recent the "
            "last heartbeat is — never read from a stored flag. 'stale' means "
            "a connection is recorded but the heartbeats stopped, which is the "
            "honest answer when the controller cannot tell whether the agent "
            "died or the network did."
        )
    )
    last_heartbeat_at: str | None = Field(
        description=(
            "ISO-8601, or null when no agent has ever reported. Null must not "
            "be rendered as a date."
        )
    )
    last_connected_at: str | None = Field(description="ISO-8601, or null.")
    last_disconnected_at: str | None = Field(description="ISO-8601, or null.")
    agent_version: str | None = Field(
        description=(
            "As the agent reports it. Advisory and untrusted — it comes from a "
            "possibly-compromised host and is never used for a decision."
        )
    )
    platform: str | None = Field(description="Reported os/arch, e.g. 'linux/amd64'. Advisory.")
    protocol_version: int | None = Field(
        description="Protocol major version negotiated on the last connection, or null."
    )
    agent_started_at: str | None = Field(
        description="When the connected agent process started, as it reports. Advisory."
    )
    has_pending_enrollment: bool = Field(
        description=(
            "Whether an unused, unexpired enrollment credential exists. A "
            "boolean and nothing more: the credential itself is unrecoverable "
            "once issued."
        )
    )
    credential_issued_at: str | None = Field(
        description=(
            "When the live agent credential was issued, or null when there is "
            "none. Distinguishes 'never enrolled' from 'credential revoked' "
            "without disclosing anything about the credential."
        )
    )
    created_at: str = Field(description="ISO-8601 registration timestamp.")

    @classmethod
    def of(cls, record: HostRecord) -> HostResponse:
        """Build a response from a service record."""
        host = record.host
        return cls(
            host_id=str(host.id),
            name=host.name,
            description=host.description,
            environment=host.environment,
            lifecycle=HostLifecycle(host.lifecycle),
            is_enabled=host.is_enabled,
            connectivity=record.connectivity,
            last_heartbeat_at=_iso(host.last_heartbeat_at),
            last_connected_at=_iso(host.last_connected_at),
            last_disconnected_at=_iso(host.last_disconnected_at),
            agent_version=host.agent_version,
            platform=host.platform,
            protocol_version=host.protocol_version,
            agent_started_at=_iso(host.agent_started_at),
            has_pending_enrollment=record.has_pending_enrollment,
            credential_issued_at=_iso(record.credential_issued_at),
            created_at=host.created_at.isoformat(),
        )


class HostListResponse(BaseModel):
    """A page of hosts, and what a pager needs."""

    items: list[HostResponse]
    total: int = Field(
        description=(
            "Hosts matching the name, lifecycle and status filters, ignoring "
            "pagination. The `connectivity` filter is applied to the page "
            "after it is fetched, so when one is supplied this total counts "
            "the hosts the other filters matched and `items` may be shorter "
            "than `limit`. Deliberate — see hcc/services/hosts.py."
        )
    )
    limit: int
    offset: int


class HostCreateRequest(BaseModel):
    """Register a host.

    ``extra="forbid"``: a request naming a field this model does not declare
    is rejected. That is the mass-assignment guard at the boundary — a
    forwarded ``lifecycle``, ``connection_epoch`` or ``id`` fails validation
    rather than being silently dropped, and a client that believed it set one
    finds out.
    """

    model_config = {"extra": "forbid"}

    name: str = Field(
        min_length=1,
        max_length=MAX_RAW_NAME_LENGTH,
        description=(
            f"{HOST_NAME_MIN_LENGTH} to {HOST_NAME_MAX_LENGTH} characters after "
            "normalization. Case-insensitively unique. Interior spaces are "
            "allowed; runs of whitespace are collapsed."
        ),
    )
    description: str | None = Field(
        default=None,
        max_length=MAX_RAW_DESCRIPTION_LENGTH,
        description=(
            f"Optional note, up to {HOST_DESCRIPTION_MAX_LENGTH} characters "
            "after normalization. Blank or whitespace-only is stored as null."
        ),
    )
    environment: str | None = Field(
        default=None,
        max_length=MAX_RAW_ENVIRONMENT_LENGTH,
        description=(
            f"Optional grouping label, up to {HOST_ENVIRONMENT_MAX_LENGTH} "
            "characters. For the operator's own use; nothing resolves "
            "configuration against it."
        ),
    )


class HostUpdateRequest(BaseModel):
    """Change a host's labels.

    Every field is optional, and "absent" is distinguished from "null":
    omitting ``description`` leaves it alone, sending ``null`` clears it.
    Pydantic cannot express that with a plain default, so
    :meth:`to_service_update` consults ``model_fields_set`` — the set of keys
    the client actually sent.

    Labels only. ``lifecycle``, ``is_enabled`` and every connectivity column
    are deliberately absent: enrollment state is changed by enrolling or
    revoking, administrative state by the enable and disable endpoints, and
    connectivity by an agent connecting. A field here that could set any of
    them would let an administrator type a host into a state the system had
    not reached.
    """

    model_config = {"extra": "forbid"}

    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_RAW_NAME_LENGTH,
        description="Omit to leave unchanged. Null is treated as omitted.",
    )
    description: str | None = Field(
        default=None,
        max_length=MAX_RAW_DESCRIPTION_LENGTH,
        description="Send a string to set it, null to clear it, or omit the key.",
    )
    environment: str | None = Field(
        default=None,
        max_length=MAX_RAW_ENVIRONMENT_LENGTH,
        description="Send a string to set it, null to clear it, or omit the key.",
    )

    def to_service_update(self) -> HostUpdate:
        """Translate into the service's explicit field set.

        Field by field, never by unpacking a dict. That is what makes mass
        assignment impossible rather than merely discouraged: adding a column
        to ``hosts`` does not make it writable here, because there is no code
        that would carry it.
        """
        submitted = self.model_fields_set
        return HostUpdate(
            # A name has no meaningful null — a host must be called something —
            # so an explicit null is treated as "unchanged" rather than
            # rejected. There is nothing a client could intend by it.
            name=self.name if "name" in submitted and self.name is not None else UNSET,
            description=self.description if "description" in submitted else UNSET,
            environment=self.environment if "environment" in submitted else UNSET,
        )


class HostEnrollmentRequest(BaseModel):
    """Issue a one-time enrollment credential."""

    model_config = {"extra": "forbid"}

    allow_reenrollment: bool = Field(
        default=False,
        description=(
            "Required to issue a credential for a host that already has an "
            "enrolled agent. Consuming the new credential revokes the existing "
            "one and disconnects that agent, so the confirmation is the "
            "difference between a deliberate re-enrollment and a misclick that "
            "takes a working host offline."
        ),
    )


class EnrollmentResponse(BaseModel):
    """A freshly issued enrollment credential.

    **The only response in the API that contains a secret.** The credential is
    not stored — only its digest is — so this body cannot be reproduced by any
    later request, including a retry of this one. The interface has to show it
    once and say so.
    """

    enrollment_id: str
    credential: str = Field(
        description=(
            "The one-time enrollment credential, in plaintext. Shown once and "
            "never retrievable again. Single use, scoped to this host, and "
            "revocable until used."
        )
    )
    expires_at: str = Field(description="ISO-8601. Absolute, and never extended.")
    expires_in_seconds: int = Field(
        description=(
            "Convenience for a countdown, computed server-side so the display "
            "does not depend on the browser's clock being right."
        )
    )
    superseded: int = Field(
        description=(
            "How many previously-pending credentials this issuance revoked. "
            "Non-zero means an earlier credential for this host has just "
            "stopped working, which an administrator needs to be told rather "
            "than discover by watching an agent fail."
        )
    )

    def __repr__(self) -> str:
        """Representation with the credential redacted.

        Pydantic models are rendered in validation errors and in tracebacks,
        so the default would print the plaintext. Same guard as
        ``LoginPayload`` and ``UserCreateRequest``.
        """
        return (
            f"EnrollmentResponse(enrollment_id={self.enrollment_id!r}, "
            f"credential=<redacted>, expires_at={self.expires_at!r})"
        )

    __str__ = __repr__


class RevocationResponse(BaseModel):
    """The outcome of a revocation."""

    revoked: int = Field(
        description=(
            "How many credentials were revoked. Zero means there were none to "
            "revoke — the request succeeded and changed nothing."
        )
    )


# ---------------------------------------------------------------------------
# Shared mapping
# ---------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    """Render an optional timestamp, preserving the difference from null.

    A missing timestamp stays ``None`` all the way to the client rather than
    becoming an empty string or an epoch date. "We have never heard from this
    host" and "we heard from it at midnight on 1 January 1970" are different
    claims, and only one of them is ever true.
    """
    if value is None:
        return None
    return value.isoformat()


def _conflict(
    error: HostNameTakenError | HostAlreadyEnrolledError | NoLiveCredentialError,
) -> ConflictError:
    """Map a service refusal onto the error envelope.

    All three are 409 with **distinct codes**, because the client's response to
    each differs: a taken name is fixed by typing another one, an
    already-enrolled host needs a confirmation dialog, and a missing credential
    means the button should not have been offered. One shared ``conflict`` code
    would force the interface to parse the message, which is not a contract.
    """
    if isinstance(error, HostNameTakenError):
        return ConflictError(str(error), code=CODE_HOST_NAME_TAKEN)
    if isinstance(error, HostAlreadyEnrolledError):
        return ConflictError(str(error), code=CODE_HOST_ALREADY_ENROLLED)
    return ConflictError(str(error), code=CODE_NO_LIVE_CREDENTIAL)


HostIdPath = Annotated[uuid.UUID, Path(description="The host's id.")]


# ---------------------------------------------------------------------------
# Reads — any authenticated role
# ---------------------------------------------------------------------------


@read_router.get(
    "",
    summary="List hosts",
    response_model=HostListResponse,
    responses={401: {"description": "No valid session."}},
)
async def list_hosts(
    db: SessionDep,
    hosts: HostServiceDep,
    _current: ViewerDep,
    search: Annotated[
        str | None,
        Query(
            max_length=MAX_SEARCH_LENGTH,
            description=(
                "Substring match against the normalized name. Folded the same "
                "way host names are, so 'HOME' matches 'homeserver'."
            ),
        ),
    ] = None,
    lifecycle: Annotated[
        HostLifecycle | None, Query(description="Exact enrollment-state match.")
    ] = None,
    is_enabled: Annotated[
        bool | None, Query(description="Filter by administrative state. Omit for both.")
    ] = None,
    connectivity: Annotated[
        HostConnectivity | None,
        Query(
            description=(
                "Filter by derived connectivity. Applied to the page after it "
                "is fetched, so `items` may be shorter than `limit` — see "
                "`total`."
            )
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> HostListResponse:
    """Return a page of hosts.

    Available to every authenticated role, because a Viewer who cannot see the
    fleet cannot use a monitoring product. Nothing in the response is
    privileged: it is the same host metadata the dashboard renders, with no
    credential material and no digests.

    ``limit`` is bounded by the query validation *and* clamped again in the
    service. Two layers on purpose: this is the bound that stops an
    authenticated caller asking the server to materialise every host, and a
    bound enforced in only one place is one a future caller can route around.
    """
    page: HostPage = await hosts.list_hosts(
        db,
        search=search,
        lifecycle=lifecycle,
        is_enabled=is_enabled,
        connectivity=connectivity,
        limit=limit,
        offset=offset,
    )
    return HostListResponse(
        items=[HostResponse.of(record) for record in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@read_router.get(
    "/{host_id}",
    summary="Fetch one host",
    response_model=HostResponse,
    responses={
        401: {"description": "No valid session."},
        404: {"description": "No such host."},
    },
)
async def get_host(
    db: SessionDep,
    hosts: HostServiceDep,
    _current: ViewerDep,
    host_id: HostIdPath,
) -> HostResponse:
    """Return one host.

    ``host_id`` is typed as a UUID, so a malformed id is a 422 from the
    framework and never reaches a query.
    """
    try:
        record = await hosts.get_host(db, host_id)
    except HostNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    return HostResponse.of(record)


# ---------------------------------------------------------------------------
# Mutations — administrator only
# ---------------------------------------------------------------------------


@router.post(
    "",
    summary="Register a host",
    response_model=HostResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        401: {"description": "No valid session."},
        403: {"description": "Not an administrator."},
        409: {"description": "The host name is already in use."},
        422: {"description": "The name, description or environment is invalid."},
    },
)
async def create_host(
    payload: HostCreateRequest,
    db: SessionDep,
    hosts: HostServiceDep,
    context: AuditContextDep,
    _current: AuditedAdminDep,
) -> HostResponse:
    """Register a host and return it.

    The new host is ``registered``, enabled, and has **no** enrollment
    credential. Registering a record and authorising an agent to claim it are
    two decisions, and bundling them would mean every mistyped host also
    minted a live secret with a fifteen-minute life.
    """
    try:
        host = await hosts.create_host(
            db,
            name=payload.name,
            description=payload.description,
            environment=payload.environment,
            context=context,
        )
    except HostNameTakenError as exc:
        raise _conflict(exc) from exc
    except ValueError as exc:
        raise ValidationFailedError(str(exc)) from exc

    # The unit of work: the host row and its audit event commit together.
    await db.commit()

    record = await hosts.get_host(db, host.id)
    return HostResponse.of(record)


@router.patch(
    "/{host_id}",
    summary="Change a host's name, description or environment",
    response_model=HostResponse,
    responses={
        401: {"description": "No valid session."},
        403: {"description": "Not an administrator."},
        404: {"description": "No such host."},
        409: {"description": "The new name is already in use."},
        422: {"description": "The name is invalid."},
    },
)
async def update_host(
    payload: HostUpdateRequest,
    db: SessionDep,
    hosts: HostServiceDep,
    context: AuditContextDep,
    _current: AuditedAdminDep,
    host_id: HostIdPath,
) -> HostResponse:
    """Change a host's labels.

    Partial by design: a request naming only ``description`` leaves the name
    alone. An empty body is accepted and changes nothing, which is the honest
    answer — refusing it would mean the client had to decide whether its own
    diff was empty, and a no-op is not an error.

    Renaming is permitted, and the agent's access is unaffected: a credential
    is bound to the host's id, not to its name. A host name is a label the
    operator reads, which is why it is mutable where a username is not
    (ADR-041).
    """
    try:
        host = await hosts.update_host(
            db, host_id=host_id, update=payload.to_service_update(), context=context
        )
    except HostNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    except HostNameTakenError as exc:
        raise _conflict(exc) from exc
    except ValueError as exc:
        raise ValidationFailedError(str(exc)) from exc

    await db.commit()
    record = await hosts.get_host(db, host.id)
    return HostResponse.of(record)


async def _set_enabled(
    *,
    db: SessionDep,
    hosts: HostServiceDep,
    context: AuditContextDep,
    host_id: uuid.UUID,
    is_enabled: bool,
) -> HostResponse:
    """Shared body for the enable and disable endpoints.

    Two routes, one implementation. The endpoints exist because disabling a
    host is a distinct confirmable action rather than a field edit, but a
    second code path would be a second place for the audit record to be
    forgotten.
    """
    try:
        host = await hosts.set_enabled(db, host_id=host_id, is_enabled=is_enabled, context=context)
    except HostNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc

    await db.commit()
    record = await hosts.get_host(db, host.id)
    return HostResponse.of(record)


@router.post(
    "/{host_id}/disable",
    summary="Disable a host",
    response_model=HostResponse,
    responses={
        401: {"description": "No valid session."},
        403: {"description": "Not an administrator."},
        404: {"description": "No such host."},
    },
)
async def disable_host(
    db: SessionDep,
    hosts: HostServiceDep,
    context: AuditContextDep,
    _current: AuditedAdminDep,
    host_id: HostIdPath,
) -> HostResponse:
    """Refuse this host's agent at the door, keeping its enrollment.

    Takes effect immediately: a connected agent's connection is closed, and
    further authentication attempts are refused while the host is disabled.

    Distinct from revoking the credential. Disabling is "not right now" and is
    undone by enabling; revoking is "that secret is no longer trusted" and is
    undone only by re-enrolling the agent. An interface that offered one
    control for both would force the operator to choose a consequence they had
    not been told about.

    Idempotent — disabling an already-disabled host succeeds and records
    nothing.
    """
    return await _set_enabled(
        db=db, hosts=hosts, context=context, host_id=host_id, is_enabled=False
    )


@router.post(
    "/{host_id}/enable",
    summary="Enable a host",
    response_model=HostResponse,
    responses={
        401: {"description": "No valid session."},
        403: {"description": "Not an administrator."},
        404: {"description": "No such host."},
    },
)
async def enable_host(
    db: SessionDep,
    hosts: HostServiceDep,
    context: AuditContextDep,
    _current: AuditedAdminDep,
    host_id: HostIdPath,
) -> HostResponse:
    """Allow this host's agent to connect again.

    Does not reconnect anything by itself, and must not: the controller never
    dials out to a host (ADR-018). The agent is retrying on its own backoff
    schedule and will succeed on its next attempt.
    """
    return await _set_enabled(db=db, hosts=hosts, context=context, host_id=host_id, is_enabled=True)


@router.post(
    "/{host_id}/enrollment",
    summary="Issue a one-time enrollment credential",
    response_model=EnrollmentResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        401: {"description": "No valid session."},
        403: {"description": "Not an administrator."},
        404: {"description": "No such host."},
        409: {"description": "The host already has an enrolled agent."},
    },
)
async def issue_enrollment(
    db: SessionDep,
    hosts: HostServiceDep,
    context: AuditContextDep,
    _current: AuditedAdminDep,
    host_id: HostIdPath,
    # Optional, so the common case — "issue one for a host with no agent" —
    # is a POST with no body rather than a mandatory `{}`. An absent body
    # means `allow_reenrollment` is false, which is the safe reading: the
    # destructive variant has to be asked for.
    payload: HostEnrollmentRequest | None = None,
) -> EnrollmentResponse:
    """Mint the credential an agent uses to claim this host, once.

    **The returned credential is the only copy.** Only its SHA-256 digest is
    stored (ADR-042), so no later request can produce it — there is no "show
    again", and the interface says so instead of offering a control that would
    have to lie.

    Issuing revokes any still-pending credential for this host, so the one on
    screen is the only one that works. ``superseded`` reports how many stopped
    working, because an administrator who issued a credential ten minutes ago
    needs to know the old one is dead rather than find out when an agent fails.

    A host that already has a live agent credential is refused unless
    ``allow_reenrollment`` is true: consuming this credential revokes that one
    and disconnects the running agent, which is a consequence the caller has to
    accept explicitly.

    Issuing for a **disabled** host is permitted. Enrollment and connection are
    separate steps, and preparing a host before enabling it is a reasonable
    order of operations — the agent will enroll and then be refused at the door
    until the host is enabled, which the host detail view states.
    """
    try:
        issued: IssuedEnrollment = await hosts.issue_enrollment(
            db,
            host_id=host_id,
            context=context,
            allow_reenrollment=bool(payload and payload.allow_reenrollment),
        )
    except HostNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    except HostAlreadyEnrolledError as exc:
        raise _conflict(exc) from exc

    await db.commit()

    # Computed from the row's own expiry rather than from the configured TTL,
    # so the countdown cannot disagree with the deadline the database will
    # enforce.
    remaining = int((issued.expires_at - issued.enrollment.created_at).total_seconds())
    return EnrollmentResponse(
        enrollment_id=str(issued.enrollment.id),
        credential=issued.credential,
        expires_at=issued.expires_at.isoformat(),
        expires_in_seconds=max(0, remaining),
        superseded=issued.superseded,
    )


@router.post(
    "/{host_id}/enrollment/revoke",
    summary="Revoke a host's pending enrollment credentials",
    response_model=RevocationResponse,
    responses={
        401: {"description": "No valid session."},
        403: {"description": "Not an administrator."},
        404: {"description": "No such host."},
    },
)
async def revoke_enrollment(
    db: SessionDep,
    hosts: HostServiceDep,
    context: AuditContextDep,
    _current: AuditedAdminDep,
    host_id: HostIdPath,
) -> RevocationResponse:
    """Kill an enrollment credential that has not been used yet.

    The "I pasted that into the wrong window" action. Takes effect
    immediately, and cannot be undone — issue a new credential instead.

    Returns the count rather than asserting success, so "there was nothing
    pending" is distinguishable from "one was revoked". An unknown id is a 404
    rather than a cheerful zero, because a revocation that reports success
    without having revoked anything is the one thing this must not do.
    """
    try:
        count = await hosts.revoke_enrollments(db, host_id=host_id, context=context)
    except HostNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc

    await db.commit()
    return RevocationResponse(revoked=count)


@router.post(
    "/{host_id}/credential/revoke",
    summary="Revoke a host's agent credential",
    response_model=HostResponse,
    responses={
        401: {"description": "No valid session."},
        403: {"description": "Not an administrator."},
        404: {"description": "No such host."},
        409: {"description": "The host has no active agent credential."},
    },
)
async def revoke_credential(
    db: SessionDep,
    hosts: HostServiceDep,
    context: AuditContextDep,
    _current: AuditedAdminDep,
    host_id: HostIdPath,
) -> HostResponse:
    """Withdraw trust from this host's agent.

    The agent is cut off at its next authentication attempt, which for a
    connected agent is immediately: the controller closes a connection whose
    credential has stopped being live.

    The host moves to ``revoked``, which is deliberately not ``registered``.
    The record has been used, and re-enrolling a host that once had an agent is
    a different decision from enrolling one for the first time — collapsing the
    two would hide the fact that a credential was ever issued.

    A host with no live credential is a 409, not a success. "Revoked" and
    "there was nothing to revoke" are different answers, and an interface that
    conflated them would tell an operator their host is secured when nothing
    happened.
    """
    try:
        host = await hosts.revoke_credential(db, host_id=host_id, context=context)
    except HostNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    except NoLiveCredentialError as exc:
        raise _conflict(exc) from exc

    await db.commit()
    record = await hosts.get_host(db, host.id)
    return HostResponse.of(record)
