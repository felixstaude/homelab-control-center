"""add sessions table

Phase 1, milestone 1.3. Server-side sessions for cookie authentication.

Started from ``alembic revision --autogenerate``, which produced a correct and
reversible migration here — unlike ``0002_users``, this table introduces no
PostgreSQL enum type and no trigger, so there is nothing autogenerate is blind
to. Only the revision id and this docstring were changed, plus explicit
``if_exists`` on the index drops.

Design rationale for the columns and indexes lives in ``hcc/models/session.py``
and ADR-028.

Revision ID: 0003_sessions
Revises: 0002_users
Created: 2026-10-11 20:27:32.082621

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_sessions"
down_revision: str | None = "0002_users"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_token", sa.String(length=43), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_ip", sa.String(length=45), nullable=True),
        sa.Column("created_user_agent", sa.String(length=256), nullable=True),
        sa.CheckConstraint(
            "char_length(csrf_token) = 43", name=op.f("ck_sessions_csrf_token_length")
        ),
        sa.CheckConstraint(
            "char_length(token_hash) = 64",
            name=op.f("ck_sessions_token_hash_is_sha256_hex"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_sessions_expires_after_created")
        ),
        # CASCADE: a session pointing at a deleted user could otherwise
        # authenticate a principal that no longer exists. Deactivation rather
        # than deletion is the normal path.
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
    )
    # The pruning job scans by expiry.
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"], unique=False)
    # Listing or revoking a user's sessions filters by user.
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"], unique=False)
    # The hot path: every authenticated request resolves a token hash. Unique,
    # so a collision or double-insert is a database error rather than an
    # ambiguous query returning two sessions.
    op.create_index("uq_sessions_token_hash", "sessions", ["token_hash"], unique=True)


def downgrade() -> None:
    # Dropping the table would remove its indexes anyway; they are dropped
    # explicitly so the downgrade reads as the exact inverse of the upgrade.
    op.drop_index("uq_sessions_token_hash", table_name="sessions", if_exists=True)
    op.drop_index("ix_sessions_user_id", table_name="sessions", if_exists=True)
    op.drop_index("ix_sessions_expires_at", table_name="sessions", if_exists=True)
    op.drop_table("sessions")
