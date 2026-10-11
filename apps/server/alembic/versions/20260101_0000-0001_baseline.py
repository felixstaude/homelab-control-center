"""baseline

Establishes the migration chain for a fresh database. Intentionally empty:
Phase 0 defined no tables; the first real schema arrives in 0002_users.

Having a baseline revision means a new deployment runs `alembic upgrade head`
and ends up with a stamped, tracked database, rather than an untracked one
whose first migration is also its schema.

Revision ID: 0001_baseline
Revises:
Created: 2026-01-01 00:00:00

"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op: the baseline exists to anchor the revision chain."""


def downgrade() -> None:
    """No-op: nothing to undo."""
