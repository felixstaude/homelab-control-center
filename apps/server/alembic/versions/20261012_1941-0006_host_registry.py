"""add the host registry, enrollment tokens and agent credentials

Phase 1, milestone 1.8. Three tables, described in ``hcc/models/host.py``:

* ``hosts`` — the controller's durable record of a machine.
* ``host_enrollments`` — short-lived, single-use tokens.
* ``agent_credentials`` — long-lived per-host secrets.

Plus one column on an existing table: ``audit_events.target_host_id``. The
nine new audit actions needed no schema change, which is what ADR-038 designed
the ``varchar`` action column for — but a host event's *subject* is a host, and
an audit trail that cannot be filtered by its subject is a list rather than a
trail. Nullable and unconstrained, so it is additive: every existing row keeps
a null and nothing has to be backfilled.

Started from ``alembic revision --autogenerate``, which produced all three
tables, every CHECK, both foreign keys and all seven indexes correctly —
including the **partial** unique index, which it rendered with its
``postgresql_where`` intact.

**One thing was added by hand**, and it is the one autogenerate structurally
cannot see: the ``updated_at`` trigger on ``hosts``. Triggers are invisible to
autogenerate (ADR-015), so without this the column would be set on INSERT by
its server default and then never change again — and ``updated_at`` would
silently mean "created_at" on every host row. The ``set_updated_at()``
function already exists from ``0002_users``; only the trigger is new, and the
downgrade drops only the trigger and not the shared function.

``if_exists`` on the index drops was also added, matching ``0003_sessions``, so
a downgrade is idempotent against a half-applied upgrade.

Two details that look like boilerplate and are not:

* ``uq_agent_credentials_one_live_per_host`` is a **partial** unique index on
  ``host_id WHERE revoked_at IS NULL``. It makes "at most one live credential
  per host" a database guarantee rather than application care. Without it, a
  re-enrollment race could leave two valid credentials for one host, and
  revoking "the" credential would silently leave the other one working — a
  revocation that reports success and does not revoke.
* ``agent_credentials.enrolled_from_enrollment_id`` has **no** foreign key.
  Enrollment rows are prunable once consumed and expired; a credential must
  outlive the token that produced it, and a cascade would delete a live
  credential along with a tidied-up token row.

Reversible with no loss beyond what removing the feature inherently costs: the
host records, their tokens and their credentials. A downgrade therefore
un-enrolls every agent, which is correct for returning the schema to a state
that predates hosts, and is worth stating because the agents themselves keep
running and retrying.

Revision ID: 0006_host_registry
Revises: 0005_user_management
Created: 2026-10-12 19:41:38.752921

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_host_registry"
down_revision: str | None = "0005_user_management"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Mirrors ``TOKEN_HASH_LENGTH`` in ``hcc/security/tokens.py``. Spelled out
#: rather than imported: a migration must describe the schema as it was at this
#: revision, so changing the constant later cannot retroactively change what
#: this migration did.
TOKEN_HASH_LENGTH = 64

# BEFORE UPDATE with the same WHEN clause the users trigger carries, so a
# no-op UPDATE does not bump the timestamp and `updated_at` means "when this
# row's data last actually changed". Assigning unconditionally also means a
# client cannot backdate a row: any value an UPDATE supplies is overwritten.
CREATE_HOSTS_TRIGGER = """
CREATE TRIGGER trg_hosts_set_updated_at
BEFORE UPDATE ON hosts
FOR EACH ROW
WHEN (OLD.* IS DISTINCT FROM NEW.*)
EXECUTE FUNCTION set_updated_at();
"""

DROP_HOSTS_TRIGGER = "DROP TRIGGER IF EXISTS trg_hosts_set_updated_at ON hosts;"


def upgrade() -> None:
    op.create_table(
        "hosts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("name_canonical", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=512), nullable=True),
        sa.Column("environment", sa.String(length=32), nullable=True),
        sa.Column(
            "lifecycle",
            sa.String(length=16),
            server_default=sa.text("'registered'"),
            nullable=False,
        ),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        # Transient connection state. There is deliberately no `is_connected`
        # boolean: a controller crash would leave it true forever, and the
        # dashboard would report healthy connectivity for a fleet connected to
        # nothing. Connectivity is derived from these two columns (ADR-043).
        sa.Column("current_connection_id", sa.Uuid(), nullable=True),
        sa.Column("connection_epoch", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_disconnected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        # Advisory facts the agent reports about itself. Untrusted input from a
        # possibly-compromised host; never used for an authorization decision.
        sa.Column("agent_version", sa.String(length=64), nullable=True),
        sa.Column("protocol_version", sa.Integer(), nullable=True),
        sa.Column("platform", sa.String(length=64), nullable=True),
        sa.Column("agent_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "lifecycle IN ('registered', 'enrolled', 'revoked')",
            name=op.f("ck_hosts_lifecycle_is_known"),
        ),
        sa.CheckConstraint("char_length(name) BETWEEN 1 AND 64", name=op.f("ck_hosts_name_length")),
        sa.CheckConstraint(
            "char_length(name_canonical) BETWEEN 1 AND 64",
            name=op.f("ck_hosts_name_canonical_length"),
        ),
        sa.CheckConstraint(
            "connection_epoch >= 0", name=op.f("ck_hosts_connection_epoch_not_negative")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_hosts")),
        sa.UniqueConstraint("name_canonical", name="uq_hosts_name_canonical"),
    )
    # Connectivity derivation reads this on every list.
    op.create_index("ix_hosts_last_heartbeat_at", "hosts", ["last_heartbeat_at"], unique=False)
    # The lifecycle filter on the host list.
    op.create_index("ix_hosts_lifecycle", "hosts", ["lifecycle"], unique=False)

    # Added by hand: autogenerate cannot see triggers (ADR-015). The shared
    # `set_updated_at()` function is created by 0002_users.
    op.execute(CREATE_HOSTS_TRIGGER)

    op.create_table(
        "agent_credentials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("host_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=TOKEN_HASH_LENGTH), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        # No foreign key, deliberately: an enrollment row is prunable once
        # consumed, and a credential must outlive the token that produced it.
        sa.Column("enrolled_from_enrollment_id", sa.Uuid(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"char_length(token_hash) = {TOKEN_HASH_LENGTH}",
            name=op.f("ck_agent_credentials_token_hash_is_sha256_hex"),
        ),
        sa.ForeignKeyConstraint(
            ["host_id"],
            ["hosts.id"],
            name=op.f("fk_agent_credentials_host_id_hosts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_credentials")),
    )
    op.create_index("ix_agent_credentials_host_id", "agent_credentials", ["host_id"], unique=False)
    # "At most one live credential per host", as a database guarantee. See the
    # module docstring for what goes wrong without it.
    op.create_index(
        "uq_agent_credentials_one_live_per_host",
        "agent_credentials",
        ["host_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    # The hot path: resolve a presented credential on every reconnection.
    op.create_index(
        "uq_agent_credentials_token_hash", "agent_credentials", ["token_hash"], unique=True
    )

    op.create_table(
        "host_enrollments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("host_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=TOKEN_HASH_LENGTH), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        # No foreign key to `users`, for the reason `audit_events` has none
        # (ADR-038): deleting a user must not erase the record of what they did.
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            f"char_length(token_hash) = {TOKEN_HASH_LENGTH}",
            name=op.f("ck_host_enrollments_token_hash_is_sha256_hex"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_host_enrollments_expires_after_created")
        ),
        sa.ForeignKeyConstraint(
            ["host_id"],
            ["hosts.id"],
            name=op.f("fk_host_enrollments_host_id_hosts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_host_enrollments")),
    )
    # The pruning job scans by expiry.
    op.create_index(
        "ix_host_enrollments_expires_at", "host_enrollments", ["expires_at"], unique=False
    )
    op.create_index("ix_host_enrollments_host_id", "host_enrollments", ["host_id"], unique=False)
    # Resolve a presented enrollment token in one indexed lookup (ADR-042).
    op.create_index(
        "uq_host_enrollments_token_hash", "host_enrollments", ["token_hash"], unique=True
    )

    # The audit trail's host dimension. Nullable with no backfill: every event
    # written before this revision concerned an account, so a null is the
    # correct value rather than a missing one.
    op.add_column("audit_events", sa.Column("target_host_id", sa.Uuid(), nullable=True))
    op.create_index(
        "ix_audit_events_target_host_id", "audit_events", ["target_host_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_audit_events_target_host_id", table_name="audit_events", if_exists=True)
    # Discards the host attribution on any event that carried one. Inherent:
    # the column is being removed. The events themselves survive, with their
    # action and their `details`, so nothing becomes unanswerable — only
    # harder to filter.
    op.drop_column("audit_events", "target_host_id")

    op.drop_index("uq_host_enrollments_token_hash", table_name="host_enrollments", if_exists=True)
    op.drop_index("ix_host_enrollments_host_id", table_name="host_enrollments", if_exists=True)
    op.drop_index("ix_host_enrollments_expires_at", table_name="host_enrollments", if_exists=True)
    op.drop_table("host_enrollments")

    op.drop_index("uq_agent_credentials_token_hash", table_name="agent_credentials", if_exists=True)
    op.drop_index(
        "uq_agent_credentials_one_live_per_host",
        table_name="agent_credentials",
        postgresql_where=sa.text("revoked_at IS NULL"),
        if_exists=True,
    )
    op.drop_index("ix_agent_credentials_host_id", table_name="agent_credentials", if_exists=True)
    op.drop_table("agent_credentials")

    # The trigger, but not the shared `set_updated_at()` function — `users`
    # still needs it. Dropping the function here would break that trigger.
    op.execute(DROP_HOSTS_TRIGGER)

    op.drop_index("ix_hosts_lifecycle", table_name="hosts", if_exists=True)
    op.drop_index("ix_hosts_last_heartbeat_at", table_name="hosts", if_exists=True)
    # Removes every host record, token and credential. Inherent to removing
    # the feature: a downgrade un-enrolls the fleet, and the agents keep
    # running and retrying.
    op.drop_table("hosts")
