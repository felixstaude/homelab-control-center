"""add display names and the audit event table

Phase 1, milestone 1.7. Two additions, both required by production user
management:

* ``users.display_name`` — a mutable human-readable label, separate from the
  immutable login name (ADR-041).
* ``audit_events`` — the append-only trail for privileged operations
  (ADR-038).

Started from ``alembic revision --autogenerate``, which produced the table,
its four indexes and the new column correctly. **Two things were added by
hand**, and the first is the kind of omission that is easy to miss in review:

1. The ``display_name_length`` CHECK on ``users``. Alembic does not detect a
   CHECK constraint added to an *existing* table — it compares them only when
   emitting ``create_table`` — so autogenerate produced the column with no
   constraint at all. Without it the database would accept an empty-string
   display name, which the application maps to NULL and would therefore never
   write but also never reject on any other write path. ``alembic check``
   does not catch this either, for the same reason, which is why it is called
   out here rather than left as a silent difference between the model and the
   schema.
2. ``if_exists`` on the index drops, matching ``0003_sessions``, so a
   downgrade is idempotent rather than failing on a half-applied state.

The descending composite index is deliberate and is the one index here that
does real work: the audit list is "most recent first, paginated", and
``(occurred_at DESC, id DESC)`` serves it as an index scan with no sort while
making the ordering total. See ``hcc/models/audit.py``.

Reversible with no data loss beyond what is inherent: the downgrade drops
``audit_events``, which discards the trail, and drops ``display_name``, which
discards labels. Both are inherent to removing a feature, and neither touches
a credential or a session.

Revision ID: 0005_user_management
Revises: 0004_installation
Created: 2026-10-12 17:11:16.745625

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005_user_management"
down_revision: str | None = "0004_installation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Mirrors ``DISPLAY_NAME_MAX_LENGTH`` in ``hcc/models/user.py``. Spelled out
#: rather than imported: a migration must describe the schema as it was at
#: this revision, so that changing the constant later does not retroactively
#: change what this migration did.
DISPLAY_NAME_MAX_LENGTH = 64


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        # No foreign key to `users`, deliberately: a cascade would delete the
        # record of what was done to an account along with the account, which
        # is precisely backwards. Same reasoning as
        # `installation.initialized_by_user_id` (ADR-033).
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("target_user_id", sa.Uuid(), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        # The enforcement a native enum would provide for the one genuinely
        # closed column, without imposing `ALTER TYPE` on the `action`
        # vocabulary, which is expected to grow every milestone (ADR-038).
        sa.CheckConstraint(
            "outcome IN ('success', 'denied', 'failure')",
            name=op.f("ck_audit_events_outcome_is_known"),
        ),
        sa.CheckConstraint(
            "char_length(action) BETWEEN 1 AND 64",
            name=op.f("ck_audit_events_action_length"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    # Action filtering on the list endpoint.
    op.create_index("ix_audit_events_action", "audit_events", ["action"], unique=False)
    # "What did this administrator do."
    op.create_index(
        "ix_audit_events_actor_user_id", "audit_events", ["actor_user_id"], unique=False
    )
    # The default read: newest first, paginated, with a total ordering so a
    # paginated reader cannot see one event twice and miss another. Ascending
    # even though the query descends — PostgreSQL scans a btree backwards at
    # the same cost, and declaring DESC operators made `alembic check` report
    # permanent false drift. See `hcc/models/audit.py`.
    op.create_index(
        "ix_audit_events_occurred_at_id", "audit_events", ["occurred_at", "id"], unique=False
    )
    # "What was done to this account."
    op.create_index(
        "ix_audit_events_target_user_id", "audit_events", ["target_user_id"], unique=False
    )

    # Nullable with no server default: every existing row means "no display
    # name", which is the correct reading and needs no backfill. A NOT NULL
    # column would have required inventing a value for accounts that have
    # never had one — the unsafe nullable-to-non-nullable transition this
    # deliberately avoids.
    op.add_column(
        "users", sa.Column("display_name", sa.String(length=DISPLAY_NAME_MAX_LENGTH), nullable=True)
    )
    # Added by hand: autogenerate does not detect a CHECK added to an existing
    # table. NULL satisfies a CHECK, so this bounds the length and forbids the
    # empty string without forbidding "no display name" — two states the
    # application keeps distinct.
    op.create_check_constraint(
        "display_name_length",
        "users",
        "display_name IS NULL OR char_length(display_name) BETWEEN 1 "
        f"AND {DISPLAY_NAME_MAX_LENGTH}",
    )


def downgrade() -> None:
    # The *unprefixed* name, as `create_check_constraint` above was given:
    # both ends apply the `ck_%(table_name)s_%(constraint_name)s` convention
    # from `hcc/db.py`, so passing the already-resolved
    # `ck_users_display_name_length` produced
    # `ck_users_ck_users_display_name_length` and the downgrade failed. Found
    # by running the downgrade rather than by reading it.
    op.drop_constraint("display_name_length", "users", type_="check")
    op.drop_column("users", "display_name")

    # Dropped explicitly rather than relying on `drop_table`, so the downgrade
    # reads as the exact inverse of the upgrade. `if_exists` makes it
    # idempotent against a half-applied upgrade.
    op.drop_index("ix_audit_events_target_user_id", table_name="audit_events", if_exists=True)
    op.drop_index("ix_audit_events_occurred_at_id", table_name="audit_events", if_exists=True)
    op.drop_index("ix_audit_events_actor_user_id", table_name="audit_events", if_exists=True)
    op.drop_index("ix_audit_events_action", table_name="audit_events", if_exists=True)
    # Discards the audit trail. Inherent to removing the feature, and stated
    # rather than left to be discovered.
    op.drop_table("audit_events")
