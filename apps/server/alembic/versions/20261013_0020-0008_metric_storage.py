"""add time-partitioned metric storage

Phase 3, implementing ``docs/DATA_RETENTION.md`` §4. Five tables, described in
``hcc/models/metrics.py``:

* ``metric_series`` — one row per measurable stream, ``(host, module, metric,
  labels)``, referenced by every sample through a narrow ``BIGINT``.
* ``metric_samples_raw`` — full-resolution samples, **partitioned by range on
  ``recorded_at``**, daily.
* ``metric_samples_5m`` and ``metric_samples_1h`` — rollups carrying min, max,
  avg, first, last and count, partitioned weekly and monthly.
* ``metric_partitions`` — the catalogue the maintenance job keeps.

Started from ``alembic revision --autogenerate``, which produced every column,
every CHECK, the foreign key and both indexes correctly.

**Three things were added by hand**, and the first is the one the whole design
rests on:

1. ``PARTITION BY RANGE`` on the three sample tables. SQLAlchemy's DDL cannot
   express it and autogenerate therefore cannot see it, so the generated
   ``CREATE TABLE`` statements were replaced with raw SQL. Without the
   partitioning, retention would be a ``DELETE`` over hundreds of millions of
   rows — holding locks, generating enormous WAL, leaving dead tuples, and
   then needing a ``VACUUM`` that competes with ingestion. With it, expiring a
   day is ``DROP TABLE``.

   Note what *cannot* be hand-written here: the partitions themselves. They
   are named after the window they cover, so they are created at runtime by
   the maintenance job rather than by a migration — and ``alembic/env.py``'s
   ``include_object`` hook now excludes them from autogenerate, which would
   otherwise reflect them, find no model, and propose dropping every stored
   measurement.

2. An **initial partition per tier**, covering the day, week and month this
   migration runs in. Without one, the very first ingestion would fail with
   "no partition of relation … found for row" — a migration that leaves the
   database unable to accept the data it just made room for.

3. A ``DEFAULT`` partition per tier. A row whose timestamp falls outside every
   partition lands there instead of being rejected, which turns the one
   failure mode of range partitioning — a maintenance job that fell behind —
   from lost data into data in a table the job can redistribute. It is a
   safety net and not a plan: a default partition that is being written to
   means the job is not keeping up, and the catalogue is what makes that
   visible.

Two details that look like boilerplate and are not:

* ``metric_samples_raw`` has **no foreign key** to ``metric_series``. On a
  partitioned table the constraint is enforced per partition, so deleting a
  series would have to touch every partition that references it — and the cost
  sits on the hot insert path. Series are removed only by a host cascade,
  which removes the samples with them, so the constraint's value is small.
* The primary keys are ``(series_id, recorded_at)`` and ``(series_id,
  bucket_start)`` — series first. Every read is "this series over this time
  range", so clustering by series makes the common query a single index range
  scan. It is also the deduplication mechanism: delivery is at-least-once, and
  a re-sent batch is an ``ON CONFLICT DO NOTHING`` that changes nothing.

Reversible. The downgrade drops the partitioned parents, which takes their
partitions with them — so it destroys every stored measurement, which is what
removing metric storage inherently costs.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0008_metric_storage"
down_revision: str | None = "0007_monitoring_config"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The three partitioned tables, as raw DDL.
#
# One statement per entry, and that is not a style choice: asyncpg sends
# statements through PostgreSQL's *prepared* protocol, which refuses more than
# one command per statement. A single string containing a CREATE TABLE and its
# COMMENT fails with "cannot insert multiple commands into a prepared
# statement" — which is how the first version of this migration failed.
#
# Written out rather than built from a loop, so that the actual schema of each
# table is readable in the migration that creates it.
PARTITIONED_TABLES: Final[tuple[str, ...]] = (
    """
    CREATE TABLE metric_samples_raw (
        series_id   bigint NOT NULL,
        recorded_at timestamptz NOT NULL,
        value       double precision NOT NULL,
        CONSTRAINT pk_metric_samples_raw PRIMARY KEY (series_id, recorded_at)
    ) PARTITION BY RANGE (recorded_at)
    """,
    """
    COMMENT ON TABLE metric_samples_raw IS
        'Raw metric samples. Partitioned by range on recorded_at, daily.'
    """,
    """
    CREATE TABLE metric_samples_5m (
        series_id    bigint NOT NULL,
        bucket_start timestamptz NOT NULL,
        min_value    double precision NOT NULL,
        max_value    double precision NOT NULL,
        avg_value    double precision NOT NULL,
        first_value  double precision NOT NULL,
        last_value   double precision NOT NULL,
        sample_count integer NOT NULL,
        CONSTRAINT pk_metric_samples_5m PRIMARY KEY (series_id, bucket_start)
    ) PARTITION BY RANGE (bucket_start)
    """,
    """
    COMMENT ON TABLE metric_samples_5m IS
        'Five-minute rollups. Partitioned by range on bucket_start, weekly.'
    """,
    """
    CREATE TABLE metric_samples_1h (
        series_id    bigint NOT NULL,
        bucket_start timestamptz NOT NULL,
        min_value    double precision NOT NULL,
        max_value    double precision NOT NULL,
        avg_value    double precision NOT NULL,
        first_value  double precision NOT NULL,
        last_value   double precision NOT NULL,
        sample_count integer NOT NULL,
        CONSTRAINT pk_metric_samples_1h PRIMARY KEY (series_id, bucket_start)
    ) PARTITION BY RANGE (bucket_start)
    """,
    """
    COMMENT ON TABLE metric_samples_1h IS
        'Hourly rollups. Partitioned by range on bucket_start, monthly.'
    """,
    # A DEFAULT partition per tier. See the docstring: a safety net for a
    # maintenance job that fell behind, not a plan.
    "CREATE TABLE metric_samples_raw_default PARTITION OF metric_samples_raw DEFAULT",
    "CREATE TABLE metric_samples_5m_default PARTITION OF metric_samples_5m DEFAULT",
    "CREATE TABLE metric_samples_1h_default PARTITION OF metric_samples_1h DEFAULT",
)

# The first real partition of each tier, covering the period this migration
# runs in. date_trunc rather than a literal, so the bounds are correct whenever
# the migration is applied.
#
# **Truncated in UTC explicitly.** `date_trunc('day', now())` on a timestamptz
# truncates in the *session* time zone, so on a database whose TimeZone is
# Europe/Berlin the boundary would be local midnight — 22:00 or 23:00 UTC —
# and every partition would be offset from every other installation's. The
# maintenance job computes its boundaries in UTC, so the two would disagree
# about where one day ends and the next begins, and the job would try to create
# a partition overlapping one that already existed.
#
# The catalogue rows are inserted in the same statement, so a partition and its
# record cannot exist without each other — which is the property the
# maintenance job's gap check depends on.
CREATE_INITIAL_PARTITIONS = """
DO $$
DECLARE
    today       timestamptz := date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC';
    this_week   timestamptz := date_trunc('week', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC';
    this_month  timestamptz := date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC';
    raw_start   timestamptz := today;
    raw_end     timestamptz := today + interval '1 day';
    five_start  timestamptz := this_week;
    five_end    timestamptz := this_week + interval '7 days';
    hour_start  timestamptz := this_month;
    hour_end    timestamptz := this_month + interval '1 month';
    raw_name    text := 'metric_samples_raw_' || to_char(today AT TIME ZONE 'UTC', 'YYYYMMDD');
    five_name   text := 'metric_samples_5m_' || to_char(this_week AT TIME ZONE 'UTC', 'YYYYMMDD');
    hour_name   text := 'metric_samples_1h_' || to_char(this_month AT TIME ZONE 'UTC', 'YYYYMM');
BEGIN
    EXECUTE format(
        'CREATE TABLE %I PARTITION OF metric_samples_raw FOR VALUES FROM (%L) TO (%L)',
        raw_name, raw_start, raw_end);
    EXECUTE format(
        'CREATE TABLE %I PARTITION OF metric_samples_5m FOR VALUES FROM (%L) TO (%L)',
        five_name, five_start, five_end);
    EXECUTE format(
        'CREATE TABLE %I PARTITION OF metric_samples_1h FOR VALUES FROM (%L) TO (%L)',
        hour_name, hour_start, hour_end);

    INSERT INTO metric_partitions (id, tier, table_name, range_start, range_end)
    VALUES
        (gen_random_uuid(), 'raw', raw_name, raw_start, raw_end),
        (gen_random_uuid(), '5m', five_name, five_start, five_end),
        (gen_random_uuid(), '1h', hour_name, hour_start, hour_end);
END $$
"""


def upgrade() -> None:
    op.create_table(
        "metric_partitions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tier", sa.String(length=8), nullable=False),
        sa.Column("table_name", sa.Text(), nullable=False),
        sa.Column("range_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("range_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("dropped_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "tier IN ('raw', '5m', '1h')", name=op.f("ck_metric_partitions_tier_is_known")
        ),
        sa.CheckConstraint(
            "range_end > range_start", name=op.f("ck_metric_partitions_range_is_forward")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_metric_partitions")),
        sa.UniqueConstraint("table_name", name="uq_metric_partitions_table_name"),
    )
    op.create_index(
        "ix_metric_partitions_tier_range",
        "metric_partitions",
        ["tier", "range_start"],
        unique=False,
    )

    op.create_table(
        "metric_series",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("host_id", sa.Uuid(), nullable=False),
        sa.Column("module", sa.String(length=32), nullable=False),
        sa.Column("metric", sa.String(length=32), nullable=False),
        sa.Column(
            "labels",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("labels_key", sa.String(length=400), server_default="", nullable=False),
        sa.Column("unit", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_sample_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "module IN ('cpu', 'memory', 'swap', 'load', 'uptime')",
            name=op.f("ck_metric_series_module_is_known"),
        ),
        sa.CheckConstraint(
            "char_length(metric) > 0", name=op.f("ck_metric_series_metric_is_not_empty")
        ),
        sa.ForeignKeyConstraint(
            ["host_id"],
            ["hosts.id"],
            name=op.f("fk_metric_series_host_id_hosts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_metric_series")),
        sa.UniqueConstraint(
            "host_id", "module", "metric", "labels_key", name="uq_metric_series_identity"
        ),
    )
    op.create_index(
        "ix_metric_series_host_module", "metric_series", ["host_id", "module"], unique=False
    )

    # Added by hand: SQLAlchemy's DDL cannot express PARTITION BY.
    for statement in PARTITIONED_TABLES:
        op.execute(statement)
    op.execute(CREATE_INITIAL_PARTITIONS)


def downgrade() -> None:
    # The partitioned parents, which take every partition with them.
    op.execute("DROP TABLE IF EXISTS metric_samples_1h")
    op.execute("DROP TABLE IF EXISTS metric_samples_5m")
    op.execute("DROP TABLE IF EXISTS metric_samples_raw")

    op.drop_index("ix_metric_series_host_module", table_name="metric_series", if_exists=True)
    op.drop_table("metric_series")
    op.drop_index("ix_metric_partitions_tier_range", table_name="metric_partitions", if_exists=True)
    op.drop_table("metric_partitions")
