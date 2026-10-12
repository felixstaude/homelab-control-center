"""Reading the audit trail.

Phase 1, milestone 1.7. The write side is :mod:`hcc.security.audit`; this is
the query side, separated because they have genuinely different shapes — one
appends a row inside somebody else's transaction, the other paginates and
resolves names.

## Pagination is keyset, not offset

The user list uses ``LIMIT``/``OFFSET`` and says why
(:mod:`hcc.services.users`). The audit log must not, and the reason is
specific to it rather than a general preference.

An audit log is append-only and read newest-first, so rows are inserted at
exactly the end the reader starts from. With offset pagination, every event
recorded while an operator is reading pushes the whole list down by one: page
2 then re-shows the last row of page 1, and the row that was there has moved
to page 2 as well — so a reader paging through a busy log sees duplicates and
misses events, which in a security screen is worse than useless. Offset also
degrades: ``OFFSET 10000`` makes PostgreSQL walk and discard ten thousand
rows.

Keyset pagination asks instead for "the events before this exact point". The
cursor is the ``(occurred_at, id)`` of the last row returned, compared as a
tuple, which is why ``id`` is in the index and in the ordering: ``occurred_at``
alone is not unique — it is the transaction timestamp, so a request that
records two events gives them the same value — and a non-unique sort key makes
the boundary between pages ambiguous in exactly the way this is meant to fix.

The cursor is opaque to the client by convention rather than by encryption: it
is base64url of ``<iso timestamp>|<uuid>``, and it is validated strictly on
the way back in. There is nothing secret in it — both components appear in the
page it came from — so the encoding exists to stop callers constructing
cursors by hand and depending on a format that is free to change, not to hide
anything.

## Names are resolved by join, never duplicated

``audit_events`` stores ids and no names, and carries no foreign key, so an
actor or target may no longer exist (:mod:`hcc.models.audit`). The query
``LEFT JOIN``s ``users`` twice and reports ``None`` for an account that is
gone. The alternative — snapshotting usernames into the event — would make
the trail a second, unconstrained copy of the user table that drifts the
moment anyone is renamed, and would answer "who was this" with whatever the
name was at write time while the interface shows current names everywhere
else.

An absent name is rendered as the bare id. That is honest: the id *is* what
the trail recorded.
"""

from __future__ import annotations

import base64
import binascii
import uuid as uuid_module
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import aliased

from hcc.models.audit import AuditEvent
from hcc.models.user import User

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement

#: Default and maximum page size. The maximum is what stops an authenticated
#: administrator asking for the entire trail in one response.
DEFAULT_PAGE_SIZE: Final = 50
MAX_PAGE_SIZE: Final = 200

#: Longest cursor accepted before decoding is attempted. A cursor is a fixed
#: shape, so anything much longer is either corrupt or an attempt to make the
#: decoder do work.
MAX_CURSOR_LENGTH: Final = 256


class InvalidCursorError(ValueError):
    """The supplied pagination cursor could not be decoded.

    Mapped to 422. Refused rather than ignored: silently falling back to the
    first page would make a corrupted cursor look like "no more results",
    which is the failure mode hardest to notice in a paginated list.
    """

    def __init__(self) -> None:
        super().__init__(
            "The pagination cursor is not valid. It should be the "
            "`next_cursor` from a previous response, unmodified."
        )


@dataclass(frozen=True, slots=True)
class AuditCursor:
    """A position in the newest-first ordering.

    The ``(occurred_at, id)`` of the last row a client has already seen.
    """

    occurred_at: datetime
    event_id: uuid_module.UUID

    def encode(self) -> str:
        """Return the opaque token form.

        base64url without padding, so the value is safe in a query string
        without escaping — a ``+`` in standard base64 becomes a space when a
        query parameter is decoded, which is a bug that only appears for some
        cursors and is therefore found late.
        """
        raw = f"{self.occurred_at.isoformat()}|{self.event_id}".encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @classmethod
    def decode(cls, token: str) -> AuditCursor:
        """Parse a token, or raise :class:`InvalidCursorError`.

        Every failure mode collapses to one exception: wrong base64, wrong
        separator count, an unparseable timestamp, a bad UUID. The client can
        do nothing differently for any of them, and enumerating them would
        describe the internal format the encoding exists to keep private.
        """
        if not token or len(token) > MAX_CURSOR_LENGTH:
            raise InvalidCursorError

        # Three stages rather than one `try`, so that the "wrong shape"
        # rejection in the middle is a plain `raise` rather than an exception
        # raised inside a handler that also catches it.
        try:
            # Restore the padding base64url stripped.
            padded = token + "=" * (-len(token) % 4)
            raw = base64.urlsafe_b64decode(padded.encode()).decode("utf-8")
        except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
            raise InvalidCursorError from exc

        timestamp, separator, identifier = raw.partition("|")
        if not separator:
            raise InvalidCursorError

        try:
            return cls(
                occurred_at=datetime.fromisoformat(timestamp),
                event_id=uuid_module.UUID(identifier),
            )
        except ValueError as exc:
            raise InvalidCursorError from exc


@dataclass(frozen=True, slots=True)
class AuditEventRecord:
    """One event, with the names its ids resolve to.

    ``actor_username`` and ``target_username`` are ``None`` when the account
    no longer exists — not "unknown", and not invented. See the module
    docstring.
    """

    event: AuditEvent
    actor_username: str | None
    actor_display_name: str | None
    target_username: str | None
    target_display_name: str | None


@dataclass(frozen=True, slots=True)
class AuditPage:
    """One page of events, newest first."""

    items: Sequence[AuditEventRecord]
    #: Pass as ``cursor`` to fetch the next page. ``None`` means this is the
    #: last page — distinct from an empty string, which would be a cursor.
    next_cursor: str | None
    #: Total matching the filters. Reported because an operator wants to know
    #: the size of what they are paging through; computed by a second query,
    #: so it is two queries per page regardless of page size.
    total: int
    limit: int


class AuditQueryService:
    """Reads the audit trail. Stateless."""

    __slots__ = ()

    @staticmethod
    def _filter_clauses(
        *,
        action: str | None,
        target_user_id: uuid_module.UUID | None,
        actor_user_id: uuid_module.UUID | None,
    ) -> list[ColumnElement[bool]]:
        """Build the list endpoint's WHERE clauses.

        Returns clauses rather than taking and returning a statement, so the
        page query keeps its row types — a helper annotated to accept any
        ``Select`` erases them, and every column read downstream then becomes
        an unchecked ``object``. Sharing them is still the point: a total
        computed under different filters than the rows is a pager that lies.
        """
        clauses: list[ColumnElement[bool]] = []
        if action:
            clauses.append(AuditEvent.action == action)
        if target_user_id is not None:
            clauses.append(AuditEvent.target_user_id == target_user_id)
        if actor_user_id is not None:
            clauses.append(AuditEvent.actor_user_id == actor_user_id)
        return clauses

    async def list_events(
        self,
        db: AsyncSession,
        *,
        action: str | None = None,
        target_user_id: uuid_module.UUID | None = None,
        actor_user_id: uuid_module.UUID | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> AuditPage:
        """Return one page of events, newest first.

        Raises:
            InvalidCursorError: ``cursor`` is not a token this service issued.
        """
        bounded_limit = max(1, min(limit, MAX_PAGE_SIZE))

        actor = aliased(User, name="actor")
        target = aliased(User, name="target")

        clauses = self._filter_clauses(
            action=action, target_user_id=target_user_id, actor_user_id=actor_user_id
        )

        if cursor is not None:
            position = AuditCursor.decode(cursor)
            # Row-value comparison, which PostgreSQL evaluates
            # lexicographically — the correct keyset predicate for a
            # two-column ordering. Comparing the columns separately
            # (`occurred_at <= ts AND id < id`) is the common mistake and
            # drops every event that shares the timestamp but sorts before
            # the cursor's id.
            clauses.append(
                tuple_(AuditEvent.occurred_at, AuditEvent.id)
                < tuple_(position.occurred_at, position.event_id)
            )

        rows = (
            await db.execute(
                select(
                    AuditEvent,
                    actor.username,
                    actor.display_name,
                    target.username,
                    target.display_name,
                )
                # Outer joins, because an id may refer to an account that is
                # gone and an inner join would silently drop exactly the
                # events most worth keeping.
                .outerjoin(actor, actor.id == AuditEvent.actor_user_id)
                .outerjoin(target, target.id == AuditEvent.target_user_id)
                .where(*clauses)
                .order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
                # One more than asked for, so "is there a next page" is
                # answered by the same query rather than by a second count the
                # client would have to trust.
                .limit(bounded_limit + 1)
            )
        ).all()

        total = (
            await db.execute(
                select(func.count())
                .select_from(AuditEvent)
                # The cursor clause is deliberately *not* excluded here, and
                # that is a decision: `total` is "events matching the
                # filters", so a page deep into the list must not report a
                # shrinking total as the reader advances. The cursor is
                # appended to `clauses` after this list is built for the count
                # — see below.
                .where(
                    *self._filter_clauses(
                        action=action,
                        target_user_id=target_user_id,
                        actor_user_id=actor_user_id,
                    )
                )
            )
        ).scalar_one()

        has_more = len(rows) > bounded_limit
        page = rows[:bounded_limit]

        items = [
            AuditEventRecord(
                event=event,
                actor_username=actor_username,
                actor_display_name=actor_label,
                target_username=target_username,
                target_display_name=target_label,
            )
            for event, actor_username, actor_label, target_username, target_label in page
        ]

        next_cursor = (
            AuditCursor(
                occurred_at=items[-1].event.occurred_at, event_id=items[-1].event.id
            ).encode()
            if has_more and items
            else None
        )

        return AuditPage(items=items, next_cursor=next_cursor, total=total, limit=bounded_limit)
