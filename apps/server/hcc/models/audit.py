"""Append-only audit trail for privileged operations.

Phase 1, milestone 1.7. A foundation rather than the finished audit system the
roadmap reserves for 1.14: it records user and session administration, which
is what milestone 1.7 creates, and its shape is chosen so that later
privileged operations — agent enrolment, container control, terminal sessions
— can be added as new action values without a schema change. The scope
decision is ADR-038.

## Append-only means append-only

The application only ever ``INSERT``s. There is no update path and no delete
path, and :mod:`hcc.security.audit` exposes none. That is a property of the
code rather than of the database: ``REVOKE UPDATE, DELETE`` would be the
stronger statement, but the control plane owns the schema and runs migrations
with the same role it serves requests with (ADR-003), so a grant it can revoke
is a grant it can restore. Stating the real boundary is more useful than
implying one that is not there.

**No tamper evidence is claimed.** There is no hash chain and no signature, so
an attacker with database credentials can rewrite history. That attacker is
already explicitly out of scope in ``docs/SECURITY.md``, and a hash chain that
the same process both writes and verifies would be security theatre — the
verifier's key would sit next to the data it protects.

## Why the action vocabulary is a string, not a PostgreSQL enum

ADR-013 made ``user_role`` a native enum precisely so that adding a member is
a reviewed migration, because a new role changes the authorization surface.
The reasoning does **not** transfer to audit actions, and applying it anyway
would be cargo-culting:

* A new audit action changes nothing about what anybody may do. It records
  something the system already did.
* The vocabulary is *expected* to grow with almost every future milestone,
  and each addition would otherwise need ``ALTER TYPE ... ADD VALUE`` written
  by hand, because Alembic cannot autogenerate enum-member changes.
* An unrecognised value read back is harmless here. The reader renders it
  verbatim; there is no decision to fail closed.

So the column is ``varchar`` and :class:`AuditAction` is the vocabulary the
code writes. ``outcome`` is a different case — it is a closed three-member set
with no growth pressure — but it is kept as a constrained ``varchar`` for
consistency within one table, with a CHECK constraint doing the enforcing a
native enum would.

## Why the actor and target carry no foreign key

``actor_user_id`` and ``target_user_id`` are plain ``uuid`` columns. A foreign
key with ``ON DELETE CASCADE`` would delete the audit trail along with the
account, which is exactly backwards: the record of what was done to an account
is most valuable once the account is gone. ``ON DELETE SET NULL`` would keep
the row but discard the attribution, which is nearly as bad.

This is the same reasoning that keeps ``installation.initialized_by_user_id``
unconstrained (ADR-033): a cascade from ``users`` must not be able to erase an
invariant or a record.

The cost is that an id can refer to a row that no longer exists, so the read
API resolves names with a ``LEFT JOIN`` and reports an absent user honestly
rather than inventing a label. Deactivation rather than deletion is the normal
path, so in practice the join resolves.

## What is never stored here

No password, no password hash, no session token, no CSRF token, no cookie, and
no request body. ``details`` exists for small, safe, structured context — a
role transition, how many sessions a revocation affected — and
:mod:`hcc.security.audit` enforces that by constructing it from named fields
rather than by accepting arbitrary caller input.
"""

from __future__ import annotations

import enum
import uuid

# Imported at runtime: SQLAlchemy resolves `Mapped[...]` annotations at
# class-definition time. See hcc/models/user.py.
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    String,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, validates

from hcc.db import Base

#: Upper bound on an action identifier. Dotted and lower-case by convention,
#: e.g. ``user.role_changed``; the bound exists so the column stays narrow
#: enough to index.
ACTION_MAX_LENGTH: Final = 64

#: Upper bound on an outcome. Three values today; the column is sized for a
#: word rather than for the current longest one.
OUTCOME_MAX_LENGTH: Final = 16

#: Upper bound on the correlation id, matching ``REQUEST_ID_MAX_LENGTH`` in
#: :mod:`hcc.middleware`. The request id can arrive from a client header, so it
#: is attacker-influenced — the middleware validates and bounds it, and this
#: column refuses anything longer regardless.
REQUEST_ID_MAX_LENGTH: Final = 128


class AuditAction(enum.StrEnum):
    """The actions this milestone records.

    Dotted namespaces group by subject, so a later filter can match a prefix
    and a reader can tell at a glance whether an event concerns an account or
    a session.

    Not a closed set at the database level — see the module docstring. New
    members are added here as the operations that need them arrive.
    """

    USER_CREATED = "user.created"
    USER_UPDATED = "user.updated"
    USER_ROLE_CHANGED = "user.role_changed"
    USER_DEACTIVATED = "user.deactivated"
    USER_REACTIVATED = "user.reactivated"
    USER_SESSIONS_REVOKED = "user.sessions_revoked"
    #: Suppression justified: an action identifier written into the audit
    #: trail, not a credential. Bandit matches on the member name alone.
    ACCOUNT_PASSWORD_CHANGED = "account.password_changed"  # noqa: S105
    ACCOUNT_SESSIONS_REVOKED = "account.sessions_revoked"
    ADMIN_ACCESS_DENIED = "admin.access_denied"


class AuditOutcome(enum.StrEnum):
    """Whether the recorded attempt took effect.

    Three states, and the middle one is the reason this column exists at all:
    a trail that only records successes cannot answer "did anyone try", which
    is the question a security review actually asks.
    """

    #: The operation completed and its effects are committed.
    SUCCESS = "success"
    #: Authorization refused it. Nothing changed.
    DENIED = "denied"
    #: A business rule refused it — the last-administrator guard, a duplicate
    #: username. Nothing changed, and the refusal was deliberate.
    FAILURE = "failure"


class AuditEvent(Base):
    """One recorded privileged operation.

    Rows are inserted and never modified. ``occurred_at`` is assigned by the
    database rather than by the application, so an event cannot be backdated
    by a caller — the same reasoning as the ``updated_at`` trigger (ADR-015),
    applied to a column that is written exactly once.
    """

    __tablename__ = "audit_events"

    __table_args__ = (
        # The default read is "most recent first, paginated". Including `id`
        # makes the ordering total — two events sharing a transaction
        # timestamp would otherwise be returned in an arbitrary order, so a
        # paginated reader could see one twice and miss the other.
        #
        # Plain ascending, although the query orders descending. PostgreSQL
        # scans a btree backwards at the same cost, so `DESC` index operators
        # buy nothing when every key sorts the same way — they earn their
        # keep only for a *mixed* ordering such as `occurred_at DESC, id ASC`.
        # Declaring them here was actively harmful: SQLAlchemy reflects a
        # descending index as an expression rather than a column, so
        # `alembic check` reported permanent, unfixable drift between this
        # model and a schema that in fact matched it. Caught by running the
        # migration gate rather than by reading the code.
        Index("ix_audit_events_occurred_at_id", "occurred_at", "id"),
        # "What was done to this account" — the question asked when reviewing
        # one user, and the only other access pattern this milestone creates.
        Index("ix_audit_events_target_user_id", "target_user_id"),
        # "What did this administrator do", for the actor filter.
        Index("ix_audit_events_actor_user_id", "actor_user_id"),
        # Action filtering is offered on the list endpoint, and an equality
        # match on a low-cardinality column is worth an index here only
        # because it combines with the ordering above.
        Index("ix_audit_events_action", "action"),
        CheckConstraint(
            f"char_length(action) BETWEEN 1 AND {ACTION_MAX_LENGTH}",
            name="action_length",
        ),
        # The enforcement a native enum would give, without the migration cost
        # a native enum imposes on a vocabulary that is expected to grow.
        # `outcome` is the half that genuinely is closed, so it is constrained.
        CheckConstraint(
            "outcome IN ('success', 'denied', 'failure')",
            name="outcome_is_known",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        # Generated in Python, as elsewhere (ADR-016).
        default=uuid.uuid4,
    )

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        doc=(
            "When the operation happened, assigned by the database. Because "
            "`now()` is the transaction timestamp, an event and the mutation "
            "it records share it — which is correct, since they are one unit "
            "of work."
        ),
    )

    action: Mapped[str] = mapped_column(
        String(ACTION_MAX_LENGTH),
        nullable=False,
        doc="An `AuditAction` value. A plain string by design — see the module docstring.",
    )

    outcome: Mapped[str] = mapped_column(
        String(OUTCOME_MAX_LENGTH),
        nullable=False,
        doc="An `AuditOutcome` value, constrained by a CHECK.",
    )

    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        nullable=True,
        doc=(
            "Who performed it. Null for an operation with no authenticated "
            "actor — nothing writes one today, and the column is nullable so "
            "that a future system-initiated event is representable rather "
            "than attributed to whoever happens to be convenient. No foreign "
            "key: see the module docstring."
        ),
    )

    target_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        nullable=True,
        doc="Which account it acted on, where the action has one. No foreign key.",
    )

    request_id: Mapped[str | None] = mapped_column(
        String(REQUEST_ID_MAX_LENGTH),
        nullable=True,
        doc=(
            "Correlates the event with the server log for the same request. "
            "Null when the operation did not arrive over HTTP."
        ),
    )

    details: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        doc=(
            "Small, safe, structured context. Never a request body and never "
            "credential material; `hcc.security.audit` builds it from named "
            "fields rather than from caller input."
        ),
    )

    @validates("action", "outcome")
    def _reject_unbounded_vocabulary(self, key: str, value: str) -> str:
        """Refuse an empty or over-long action or outcome at assignment.

        The CHECK constraints are the guarantee; this makes the failure happen
        where the value was set, with a readable message, rather than as a
        driver error at flush time.
        """
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a non-empty string")
        limit = ACTION_MAX_LENGTH if key == "action" else OUTCOME_MAX_LENGTH
        if len(value) > limit:
            raise ValueError(f"{key} must be at most {limit} characters, got {len(value)}")
        if key == "outcome" and value not in tuple(AuditOutcome):
            permitted = ", ".join(member.value for member in AuditOutcome)
            raise ValueError(f"invalid outcome {value!r}; permitted outcomes are: {permitted}")
        return value

    def __repr__(self) -> str:
        """Debug representation without ``details``.

        ``details`` is constructed to be safe, but it is the one open-ended
        field on the row, and a ``repr`` is exactly what ends up in a log line
        or a traceback. Omitting it means a future caller who puts something
        careless in there cannot leak it by accident through this path.
        """
        return (
            f"AuditEvent(id={self.id!r}, action={self.action!r}, "
            f"outcome={self.outcome!r}, actor_user_id={self.actor_user_id!r}, "
            f"target_user_id={self.target_user_id!r})"
        )
