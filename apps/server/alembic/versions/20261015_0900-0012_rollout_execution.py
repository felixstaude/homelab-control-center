"""What a staged rollout needs in order to actually execute.

Phase B of Operation Horizon. Evergreen created ``update_rollouts`` and
``update_rollout_members`` with the shape of a staged rollout — a canary
batch, a batch size, a failure tolerance, an offline deadline — and nothing
that drove one. This migration adds the columns the engine needs and
nothing else.

## What each column exists for

``update_rollouts``:

* ``canary_count`` — how many hosts are in batch 0. One by default, which is
  what "canary" means; more is for a fleet where one host is not
  representative.
* ``health_check_seconds`` — how long a host must stay healthy *after* its
  install before the next batch may start. This is the column that makes
  "the process restarted" stop meaning "the upgrade succeeded": the job
  concludes at reconnection, and the rollout waits to see whether the host
  is still there afterwards.
* ``started_at`` — when the first batch began. The offline deadline is
  measured per member, from when its batch started, so this is for display.
* ``window_*`` — a maintenance window for the rollout as a whole, in the
  same shape ``host_update_policies`` already uses. A rollout and a host
  policy can each have one and they are different statements: "this fleet
  change happens at night" and "this host is only interruptible at night".
* ``failures_overridden`` — an administrator said "go on anyway" after the
  gate stopped it. Recorded rather than implied by the state, because
  "resumed after a failure" and "resumed after a pause" are different facts
  and the second must not look like the first in an audit trail.

``update_rollout_members``:

* ``started_at`` — when this member's job was created. The offline deadline
  runs from here.
* ``skipped_at`` / ``skipped_reason`` — a host that was offline past the
  deadline. Skipped is a third outcome beside succeeded and failed, and it
  must not count against the failure tolerance: a powered-off NAS is not a
  bad release.
* ``healthy_at`` — when the post-install health check passed. Null while a
  member is in flight *and* null for a member that installed and then went
  away, which is the distinction the canary gate turns on.

## Why this is safe on a running installation

Every column is nullable or has a server default, so there is no backfill
and no lock held beyond the catalogue update. Both tables are empty on every
existing installation — nothing has ever created a rollout, because nothing
could.

## The downgrade drops the columns

Not the tables: those predate this migration. Dropping a column loses the
data in it, which here is the progress of any in-flight rollout — stated
rather than silently done, and the reason a downgrade should be taken while
nothing is rolling out.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0012_rollout_execution"
down_revision: str | None = "0011_storage_modules"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "update_rollouts",
        sa.Column("canary_count", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )
    op.add_column(
        "update_rollouts",
        sa.Column(
            "health_check_seconds", sa.Integer(), nullable=False, server_default=sa.text("120")
        ),
    )
    op.add_column(
        "update_rollouts",
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "update_rollouts",
        sa.Column("window_timezone", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "update_rollouts",
        sa.Column("window_weekdays", postgresql.ARRAY(sa.SmallInteger()), nullable=True),
    )
    op.add_column(
        "update_rollouts",
        sa.Column("window_start_minute", sa.Integer(), nullable=True),
    )
    op.add_column(
        "update_rollouts",
        sa.Column("window_end_minute", sa.Integer(), nullable=True),
    )
    op.add_column(
        "update_rollouts",
        sa.Column(
            "failures_overridden",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    op.create_check_constraint("canary_count_is_positive", "update_rollouts", "canary_count >= 1")
    op.create_check_constraint(
        "health_check_is_sane", "update_rollouts", "health_check_seconds >= 0"
    )
    op.create_check_constraint(
        "window_start_in_range",
        "update_rollouts",
        "window_start_minute IS NULL OR window_start_minute BETWEEN 0 AND 1439",
    )
    op.create_check_constraint(
        "window_end_in_range",
        "update_rollouts",
        "window_end_minute IS NULL OR window_end_minute BETWEEN 0 AND 1439",
    )

    op.add_column(
        "update_rollout_members",
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "update_rollout_members",
        sa.Column("skipped_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "update_rollout_members",
        sa.Column("skipped_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "update_rollout_members",
        sa.Column("healthy_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    for column in ("healthy_at", "skipped_reason", "skipped_at", "started_at"):
        op.drop_column("update_rollout_members", column)
    for name in (
        "window_end_in_range",
        "window_start_in_range",
        "health_check_is_sane",
        "canary_count_is_positive",
    ):
        op.drop_constraint(op.f(f"ck_update_rollouts_{name}"), "update_rollouts", type_="check")
    for column in (
        "failures_overridden",
        "window_end_minute",
        "window_start_minute",
        "window_weekdays",
        "window_timezone",
        "started_at",
        "health_check_seconds",
        "canary_count",
    ):
        op.drop_column("update_rollouts", column)
