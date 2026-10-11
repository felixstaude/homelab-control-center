"""add users table

Phase 1, milestone 1.1. Creates the ``users`` table, the ``user_role``
enum type, and the trigger that maintains ``updated_at``.

Started from ``alembic revision --autogenerate`` and then hand-corrected,
because autogenerate cannot produce a reversible migration here on its own:

* It emits ``CREATE TYPE user_role`` implicitly via ``create_table`` but never
  a matching ``DROP TYPE`` in ``downgrade``. The downgrade would appear to
  succeed and the next ``upgrade`` would fail with "type user_role already
  exists". The enum is therefore created and dropped explicitly below, with
  ``create_type=False`` on the column so the type is not created twice.
* Triggers are not part of SQLAlchemy metadata, so autogenerate neither
  creates nor drops the ``updated_at`` trigger. Because triggers are invisible
  to autogenerate, they also cannot cause false schema drift — ``alembic
  check`` stays clean.

Revision ID: 0002_users
Revises: 0001_baseline
Created: 2026-10-11 18:06:03.403694

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002_users"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Values must match hcc.models.user.UserRole. `create_type=False` stops
# `create_table` from emitting a second CREATE TYPE; this migration creates and
# drops the type itself so that upgrade and downgrade are symmetric.
USER_ROLE = postgresql.ENUM(
    "viewer",
    "operator",
    "admin",
    name="user_role",
    create_type=False,
)

# Shared trigger function, maintained here because `users` is the first table
# to need it. Later tables with an `updated_at` column attach their own trigger
# to this same function.
#
# The function is deliberately generic and uses CREATE OR REPLACE so that a
# later migration adding a second table can call it without conflict.
CREATE_UPDATED_AT_FUNCTION = """
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;
"""

# BEFORE UPDATE, so the new value is written as part of the same row version
# rather than requiring a second write.
#
# The WHEN clause means a no-op UPDATE (one that sets every column to the value
# it already held) does not touch `updated_at`. That makes the column mean
# "when this row's data last actually changed", which is what callers assume.
#
# Assigning NEW.updated_at unconditionally also means a client cannot set it:
# any value supplied by an UPDATE statement is overwritten.
CREATE_USERS_TRIGGER = """
CREATE TRIGGER trg_users_set_updated_at
BEFORE UPDATE ON users
FOR EACH ROW
WHEN (OLD.* IS DISTINCT FROM NEW.*)
EXECUTE FUNCTION set_updated_at();
"""


def upgrade() -> None:
    USER_ROLE.create(op.get_bind(), checkfirst=False)

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.String(length=32), nullable=False),
        sa.Column("username_canonical", sa.String(length=32), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "role",
            USER_ROLE,
            server_default=sa.text("'viewer'::user_role"),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "char_length(password_hash) > 0",
            name=op.f("ck_users_password_hash_not_empty"),
        ),
        sa.CheckConstraint(
            "char_length(username) BETWEEN 3 AND 32",
            name=op.f("ck_users_username_length"),
        ),
        sa.CheckConstraint(
            "char_length(username_canonical) BETWEEN 3 AND 32",
            name=op.f("ck_users_username_canonical_length"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("username_canonical", name="uq_users_username_canonical"),
    )

    op.execute(CREATE_UPDATED_AT_FUNCTION)
    op.execute(CREATE_USERS_TRIGGER)


def downgrade() -> None:
    # Reverse order of upgrade. The trigger goes before the table only for
    # clarity — dropping the table would remove it anyway — but the function
    # and the enum type are independent objects that `DROP TABLE` does not
    # touch, so they must be dropped explicitly or a later upgrade fails.
    op.execute("DROP TRIGGER IF EXISTS trg_users_set_updated_at ON users;")
    op.drop_table("users")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at();")
    USER_ROLE.drop(op.get_bind(), checkfirst=False)
