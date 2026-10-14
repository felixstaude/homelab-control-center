"""Audit trail endpoint.

Phase 1, milestone 1.7. Read-only and Administrator-only.

## Paths

    GET /api/v1/audit/events

There is deliberately **no** write endpoint, no update and no delete. The
trail is written by the operations it describes
(:mod:`hcc.security.audit`); an HTTP path that appended to it would let a
client fabricate history, and one that removed from it would let a client
erase their own. Append-only is not a convention here — there is no route.

## Why administrators only

The trail names who did what to whom. For a Viewer that is an inventory of
every account and every administrative action, which is reconnaissance;
specifically, it would reveal the full user list to a role the user-listing
endpoint refuses. Denials on this router are themselves audited, so an attempt
to read it from a low-privilege session is recorded.

## What the response discloses

Ids, action names, outcomes, timestamps, the resolved usernames, and the small
``details`` object the writer constructed. Never a password, a hash, a token or
a request body — those are not in the table to begin with.

``details`` is returned verbatim rather than being reformatted server-side.
The interface renders it as text and performs no interpolation, which is what
keeps an audit entry from being a vector into the page — the usual audit-log
injection shape is a stored value that the viewer renders as markup. React
escapes by default and the only strings in there are role names, counts, paths
and reason codes this codebase writes, so there is no untrusted text in the
field today; the honest statement is that the protection is the renderer's
escaping plus a writer that never takes caller input, not a sanitiser.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from hcc.api.deps import AuditedAdminDep, AuditQueryServiceDep, SessionDep
from hcc.errors import ValidationFailedError
from hcc.models.audit import ACTION_MAX_LENGTH, AuditAction
from hcc.services.audit import (
    DEFAULT_PAGE_SIZE,
    MAX_CURSOR_LENGTH,
    MAX_PAGE_SIZE,
    InvalidCursorError,
)

#: Mounted beneath the administrator gate by :mod:`hcc.api.v1.router`.
router = APIRouter(prefix="/audit", tags=["audit"])


class AuditActorResponse(BaseModel):
    """An account referenced by an event.

    ``username`` is null when the account no longer exists. The id is always
    present, because that is what the trail actually recorded — see
    :mod:`hcc.services.audit` on why names are joined rather than
    snapshotted.
    """

    user_id: str
    username: str | None = Field(
        description="Null when the referenced account no longer exists. Render the id."
    )
    display_name: str | None = None


class AuditEventResponse(BaseModel):
    """One recorded operation."""

    event_id: str
    occurred_at: str = Field(description="ISO-8601, assigned by the database.")
    action: str = Field(
        description=(
            "A dotted action identifier such as `user.role_changed`. A string "
            "rather than a closed enum, because the vocabulary grows with "
            "every milestone that records something new (ADR-038) — a client "
            "must render an unrecognised value rather than failing on it."
        )
    )
    outcome: str = Field(description="One of `success`, `denied`, `failure`.")
    actor: AuditActorResponse | None = Field(
        description="Who performed it, or null for an operation with no authenticated actor."
    )
    target: AuditActorResponse | None = Field(
        description="Which account it acted on, or null where the action has no target."
    )
    target_host_id: str | None = Field(
        default=None,
        description=(
            "Which host it acted on, or null. Separate from `target`, which "
            "is an account: an event can have one, the other or neither, and "
            "collapsing them would mean rendering a host id in a field "
            "labelled as a username."
        ),
    )
    request_id: str | None = Field(
        description="Correlates with the server log for the same request."
    )
    details: dict[str, Any] | None = Field(
        description=(
            "Small structured context the writer constructed. Never credential "
            "material and never a request body."
        )
    )


class AuditListResponse(BaseModel):
    """A page of events, newest first."""

    items: list[AuditEventResponse]
    next_cursor: str | None = Field(
        description=(
            "Pass as `cursor` for the next page. Null on the last page. "
            "Keyset rather than an offset, so events recorded while an "
            "operator is reading cannot duplicate or hide rows."
        )
    )
    total: int = Field(description="Events matching the filters, ignoring pagination.")
    limit: int


@router.get(
    "/events",
    summary="List audit events",
    response_model=AuditListResponse,
    responses={
        401: {"description": "No valid session."},
        403: {"description": "Not an administrator."},
        422: {"description": "The cursor is not valid."},
    },
)
async def list_audit_events(
    db: SessionDep,
    audit: AuditQueryServiceDep,
    _current: AuditedAdminDep,
    action: Annotated[
        str | None,
        Query(
            max_length=ACTION_MAX_LENGTH,
            description=(
                "Exact action match. Not validated against the known "
                "vocabulary: an unknown value simply matches nothing, which "
                "is the right answer and avoids a 422 for an action a newer "
                "server recorded."
            ),
        ),
    ] = None,
    target_user_id: Annotated[
        uuid.UUID | None, Query(description="Events acting on this account.")
    ] = None,
    actor_user_id: Annotated[
        uuid.UUID | None, Query(description="Events performed by this account.")
    ] = None,
    cursor: Annotated[
        str | None,
        Query(
            max_length=MAX_CURSOR_LENGTH,
            description="The `next_cursor` from a previous response, unmodified.",
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> AuditListResponse:
    """Return a page of audit events."""
    try:
        page = await audit.list_events(
            db,
            action=action,
            target_user_id=target_user_id,
            actor_user_id=actor_user_id,
            cursor=cursor,
            limit=limit,
        )
    except InvalidCursorError as exc:
        # Refused rather than ignored: falling back to the first page would
        # make a corrupted cursor look like "no more results", which is the
        # failure hardest to notice in a paginated list.
        raise ValidationFailedError(str(exc)) from exc

    return AuditListResponse(
        items=[
            AuditEventResponse(
                event_id=str(record.event.id),
                occurred_at=record.event.occurred_at.isoformat(),
                action=record.event.action,
                outcome=record.event.outcome,
                actor=(
                    AuditActorResponse(
                        user_id=str(record.event.actor_user_id),
                        username=record.actor_username,
                        display_name=record.actor_display_name,
                    )
                    if record.event.actor_user_id is not None
                    else None
                ),
                target=(
                    AuditActorResponse(
                        user_id=str(record.event.target_user_id),
                        username=record.target_username,
                        display_name=record.target_display_name,
                    )
                    if record.event.target_user_id is not None
                    else None
                ),
                target_host_id=(
                    str(record.event.target_host_id)
                    if record.event.target_host_id is not None
                    else None
                ),
                request_id=record.event.request_id,
                details=record.event.details,
            )
            for record in page.items
        ],
        next_cursor=page.next_cursor,
        total=page.total,
        limit=page.limit,
    )


@router.get(
    "/actions",
    summary="The action vocabulary this server writes",
    response_model=list[str],
    responses={
        401: {"description": "No valid session."},
        403: {"description": "Not an administrator."},
    },
)
async def list_audit_actions(_current: AuditedAdminDep) -> list[str]:
    """Return the action identifiers this server can record.

    Served so the interface can offer a filter without hard-coding a list
    that drifts from the server's. It describes what this build *writes*, not
    what the table *contains* — a stored event from an older or newer version
    may carry an action absent from here, which is exactly why the filter
    accepts any string and the renderer tolerates an unknown one.

    A constant, so there is no query and nothing to paginate.
    """
    return [member.value for member in AuditAction]
