"""add installation table

Phase 1, milestone 1.5. The durable first-run invariant: one row meaning
"this installation has been initialized".

Started from ``alembic revision --autogenerate``, which produced a correct and
reversible migration — the table introduces no PostgreSQL enum type, no
trigger and no foreign key, so there is nothing autogenerate is blind to here.
Only the revision id and this docstring were changed.

Two details are load-bearing and are easy to mistake for boilerplate:

* ``CHECK (id)`` on a boolean primary key means ``true`` is the only storable
  value, so the primary key permits exactly one row. A second insert is
  refused by the database, which is what makes concurrent bootstrap safe
  regardless of application logic.
* ``initialized_by_user_id`` has **no foreign key**. A cascade from ``users``
  would delete this row with the administrator and silently reopen bootstrap
  — the precise failure the table exists to prevent.

Full rationale in ``hcc/models/installation.py`` and ``hcc/security/bootstrap.py``.

Revision ID: 0004_installation
Revises: 0003_sessions
Created: 2026-10-11 23:00:26.277426

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_installation"
down_revision: str | None = "0003_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "installation",
        sa.Column("id", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "initialized_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("initialized_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("bootstrap_method", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "char_length(bootstrap_method) BETWEEN 1 AND 32",
            name=op.f("ck_installation_bootstrap_method_length"),
        ),
        sa.CheckConstraint("id", name=op.f("ck_installation_singleton")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_installation")),
    )


def downgrade() -> None:
    # Dropping this table discards the record that the installation was
    # initialized, which reopens bootstrap. That is correct for a downgrade —
    # the schema is being returned to a state that predates the invariant —
    # but it is worth stating, because the row is otherwise immutable.
    op.drop_table("installation")
