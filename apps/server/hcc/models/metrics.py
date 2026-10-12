"""Time-series storage: series identity, raw samples, and rollups.

Phase 3, implementing ``docs/DATA_RETENTION.md``. Plain PostgreSQL,
time-partitioned, no time-series database (ADR-003).

## A series is the unit of identity, and a sample references it by integer

A **series** is one measurable stream: ``(host, module, metric, labels)`` — for
example ``homeserver / cpu / core_utilisation / core=3``. A **sample** is one
``(series, timestamp, value)``.

Series metadata lives in its own table so a sample row carries a small integer
rather than repeating label strings. At five samples a second per host,
repeating ``"core=3"`` on every row is the difference between a manageable
table and an unmanageable one — and the integer key is what makes the
primary-key index narrow enough to stay in cache.

``BIGINT`` rather than a UUID for :attr:`MetricSeries.id`, and this is the one
place in this schema that departs from the project's UUID-everywhere habit
(ADR-005). A UUID is sixteen bytes and unordered; it is the right key for a
row a client references by URL, and the wrong one for a column that appears
in hundreds of millions of index entries and is never shown to anybody. The
series table is also not something an API exposes by id — it is addressed by
``(host, module, metric, labels)`` — so there is no identifier-guessing
surface to protect.

## Narrow rows, not a column per metric

One row per series per timestamp, rather than a wide row with a column for
each metric. The module set is configurable, so a wide table would need a
schema migration whenever somebody enabled a module, and would store nulls for
every module a host does not run.

## Partitioning carries the retention mechanism

Expiring data is ``DROP TABLE`` on a partition: near-instant, reclaims disk
immediately, creates no bloat. A ``DELETE FROM … WHERE recorded_at < …`` over
hundreds of millions of rows holds locks, generates enormous WAL, leaves dead
tuples, and then needs a ``VACUUM`` that competes with ingestion. Retention is
the most predictable workload in the system and it must not be the one that
degrades the database.

The partitioned parents are declared here as **plain tables with no
partitioning in the model**, because SQLAlchemy's DDL does not express
``PARTITION BY`` and Alembic's autogenerate cannot see it. The migration
creates the real partitioned tables; this module describes their shape so the
ORM can query them, and ``test_migrations_integration.py`` asserts the
partitioning that the models cannot express actually exists. Same arrangement,
and the same reason, as the ``updated_at`` triggers (ADR-015).

## Rollups keep the maximum, not just the average

A 5-second CPU series rolled up to a 5-minute *average* loses a 30-second
spike completely — the exact event somebody enabled 5-second sampling to
catch. So every rollup row carries min, max, avg, count, first and last, and
the interface renders a downsampled series as a min-max band with the average
inside it rather than as a single line. ``count`` is what makes a
partially-filled bucket visible rather than indistinguishable from a full one.
"""

from __future__ import annotations

import uuid

# Imported at runtime: SQLAlchemy resolves `Mapped[...]` annotations at
# class-definition time. See hcc/models/user.py.
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hcc.db import Base
from hcc.monitoring.modules import MAX_MODULE_ID_LENGTH, MODULE_IDS

#: SQL fragment listing every known module, for the CHECK constraint.
_MODULE_ID_SQL: Final = ", ".join(f"'{module_id}'" for module_id in MODULE_IDS)

#: Longest a metric identifier may be. Same bound as a module's, because both
#: are registry identifiers with the same shape.
MAX_METRIC_ID_LENGTH: Final = MAX_MODULE_ID_LENGTH

#: Longest the canonical label string may be.
#:
#: Four labels of a 32-character key and a 64-character value, plus
#: separators, is under 400. The column carries the *canonical* rendering and
#: not the JSON, because that is what the unique constraint is on — see
#: :attr:`MetricSeries.labels_key`.
MAX_LABELS_KEY_LENGTH: Final = 400


class MetricSeries(Base):
    """One measurable stream.

    Created on first sight of a sample and then never changed. A series is
    append-only in the same sense a host's configuration revisions are: the
    identity it describes is what historical rows are attributed to, so
    rewriting one would silently re-label data already stored.
    """

    __tablename__ = "metric_series"

    __table_args__ = (
        # The identity. `labels_key` rather than the JSONB, because two
        # JSONB values that differ only in key order are *equal* in
        # PostgreSQL but a unique index on a jsonb column is on the stored
        # form — and relying on that equality for identity would make the
        # constraint depend on normalisation nobody controls.
        UniqueConstraint(
            "host_id", "module", "metric", "labels_key", name="uq_metric_series_identity"
        ),
        # "Every series for this host", which the host detail page asks for,
        # and "every series for this host and module", which a chart asks for.
        Index("ix_metric_series_host_module", "host_id", "module"),
        CheckConstraint(f"module IN ({_MODULE_ID_SQL})", name="module_is_known"),
        CheckConstraint("char_length(metric) > 0", name="metric_is_not_empty"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
        doc=(
            "Narrow integer key, referenced by every sample row. Not a UUID: "
            "see the module docstring."
        ),
    )

    host_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        # Deleting a host removes its series and, through them, its samples.
        # An operator deleting a host is told what will be lost
        # (`AGENT_ARCHITECTURE.md` §5.2); keeping orphaned measurements
        # attributed to nothing would be worse than losing them.
        ForeignKey("hosts.id", ondelete="CASCADE"),
        nullable=False,
    )

    module: Mapped[str] = mapped_column(String(MAX_MODULE_ID_LENGTH), nullable=False)
    metric: Mapped[str] = mapped_column(String(MAX_METRIC_ID_LENGTH), nullable=False)

    labels: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        # Raw SQL rather than `func.cast("{}", JSONB)`: SQLAlchemy cannot
        # render a JSONB bind parameter as a literal, which `--autogenerate`
        # needs in order to write the default into a migration.
        server_default=text("'{}'::jsonb"),
        doc=(
            "The label dimensions, for reading. An empty object means one "
            "series per host, which is most of them."
        ),
    )

    labels_key: Mapped[str] = mapped_column(
        String(MAX_LABELS_KEY_LENGTH),
        nullable=False,
        server_default="",
        doc=(
            "Canonical rendering of `labels` — sorted `key=value` pairs joined "
            "by commas — carrying the unique constraint. Derived; never "
            "assign it directly."
        ),
    )

    unit: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        doc=(
            "percent | bytes | seconds | count | load. Stored on the series "
            "rather than looked up from the registry at read time, so a "
            "historical series keeps the unit it was recorded in even if a "
            "later release changes the module's."
        ),
    )

    kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        doc=(
            "gauge | counter. A counter is never averaged: an averaged "
            "power-on-hours is meaningless. Stored for the same reason as "
            "`unit`."
        ),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    last_sample_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc=(
            "When this series last received a sample. Denormalised from the "
            "sample tables on purpose: 'which series does this host have, and "
            "is each still reporting?' is the host page's question, and "
            "answering it with a MAX() over a partitioned sample table would "
            "scan every partition for every series on every page load."
        ),
    )

    def __repr__(self) -> str:
        return (
            f"MetricSeries(id={self.id!r}, host_id={self.host_id!r}, "
            f"module={self.module!r}, metric={self.metric!r}, labels={self.labels_key!r})"
        )


def canonical_labels_key(labels: dict[str, str] | None) -> str:
    """Render labels into the form the unique constraint is on.

    Sorted by key, ``key=value`` pairs joined by commas. Sorted because the
    identity of a series must not depend on the order an agent happened to
    serialise its labels in — two frames with the same labels in a different
    order describe the same series, and a key that preserved order would
    create two.

    An empty mapping renders as the empty string rather than ``"{}"``, so the
    common case (a series with no labels) carries no bytes.
    """
    if not labels:
        return ""
    return ",".join(f"{key}={labels[key]}" for key in sorted(labels))


class MetricSampleRaw(Base):
    """One measurement, at full resolution.

    The real table is ``PARTITION BY RANGE (recorded_at)`` with daily
    partitions, created by the migration; see the module docstring for why the
    model cannot express that.

    ## The primary key is the deduplication mechanism

    ``(series_id, recorded_at)``, which is both the query pattern — "this
    series over this time range" — and what makes a repeated submission
    harmless. Delivery is at-least-once: an agent that sent a batch and lost
    the acknowledgement re-sends it on reconnection, and the re-send is an
    ``ON CONFLICT DO NOTHING`` that changes nothing. That is the honest
    delivery model and it is not exactly-once; what it guarantees is that
    duplicates cannot produce duplicate *rows*.

    Clustering by ``series_id`` first is deliberate: every read is for one
    series over a range, so the common query becomes a single index range scan
    rather than a scan over a time-ordered index filtered by series.
    """

    __tablename__ = "metric_samples_raw"

    __table_args__ = (
        # Declared on the model so the ORM knows the key; the migration
        # creates it on each partition, because PostgreSQL requires a
        # partitioned table's primary key to include the partition column and
        # creates the index per partition.
        {"comment": "Raw metric samples. Partitioned by range on recorded_at, daily."},
    )

    series_id: Mapped[int] = mapped_column(
        BigInteger,
        # No foreign key. Deliberate, and the one real trade in this schema:
        # a foreign key from a partitioned table to `metric_series` would be
        # enforced per partition and would make dropping a *series* require
        # touching every partition that references it. Series are removed only
        # by a host cascade, which removes the samples too — so the
        # constraint's value is small and its cost is on the hot insert path.
        primary_key=True,
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
        doc="The agent's collection timestamp, validated against the controller's clock.",
    )
    value: Mapped[float] = mapped_column(
        Double,
        nullable=False,
        doc=(
            "The measurement. `double precision` rather than `numeric`: a "
            "utilisation percentage needs range and speed, not exact decimal "
            "arithmetic, and `numeric` is several times slower to aggregate."
        ),
    )

    def __repr__(self) -> str:
        return f"MetricSampleRaw(series_id={self.series_id!r}, recorded_at={self.recorded_at!r})"


class _RollupMixin:
    """Columns every rollup tier shares.

    Six aggregates rather than one average, because an average alone is a
    graph that lies about what happened — see the module docstring.
    """

    series_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bucket_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
        doc="Start of the bucket, aligned to the tier's width.",
    )

    min_value: Mapped[float] = mapped_column(Double, nullable=False, doc="Preserves the dip.")
    max_value: Mapped[float] = mapped_column(Double, nullable=False, doc="Preserves the spike.")
    avg_value: Mapped[float] = mapped_column(Double, nullable=False, doc="The typical value.")
    first_value: Mapped[float] = mapped_column(
        Double,
        nullable=False,
        doc="Needed for counter-derived series and for step rendering.",
    )
    last_value: Mapped[float] = mapped_column(Double, nullable=False)
    sample_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        doc=(
            "How many raw samples went into this bucket. What makes a "
            "partially-filled bucket visible rather than indistinguishable "
            "from a full one — an agent offline for four of five minutes must "
            "not produce a bucket that looks authoritative."
        ),
    )


class MetricSample5m(_RollupMixin, Base):
    """Five-minute aggregates. Weekly partitions."""

    __tablename__ = "metric_samples_5m"
    __table_args__ = (
        {"comment": "Five-minute rollups. Partitioned by range on bucket_start, weekly."},
    )


class MetricSample1h(_RollupMixin, Base):
    """Hourly aggregates. Monthly partitions."""

    __tablename__ = "metric_samples_1h"
    __table_args__ = (
        {"comment": "Hourly rollups. Partitioned by range on bucket_start, monthly."},
    )


class MetricPartition(Base):
    """Which partitions exist, and what each covers.

    A catalogue the maintenance job keeps, rather than querying
    ``pg_inherits`` every time. Two reasons, and the second is the one that
    decided it: a job that reads the catalogue can tell "this partition was
    created by us for this tier" from "somebody attached a table", and the
    *gap* between the newest partition and now is the thing worth monitoring —
    a missing future partition rejects inserts, so the job creates several
    periods ahead and the gap is what says whether it is keeping up.
    """

    __tablename__ = "metric_partitions"

    __table_args__ = (
        UniqueConstraint("table_name", name="uq_metric_partitions_table_name"),
        # The maintenance job asks "what is the newest partition for this
        # tier", on every run.
        Index("ix_metric_partitions_tier_range", "tier", "range_start"),
        CheckConstraint("range_end > range_start", name="range_is_forward"),
        CheckConstraint("tier IN ('raw', '5m', '1h')", name="tier_is_known"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    tier: Mapped[str] = mapped_column(String(8), nullable=False, doc="raw | 5m | 1h.")
    table_name: Mapped[str] = mapped_column(
        Text, nullable=False, doc="The partition's own table name."
    )
    range_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    range_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        doc="Exclusive upper bound, matching PostgreSQL's FOR VALUES FROM … TO ….",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    dropped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc=(
            "When retention removed it. The row is kept so that 'this window "
            "existed and was expired at this time' stays answerable — "
            "otherwise an operator asking why last month's data is gone has "
            "only an absence to look at."
        ),
    )

    def __repr__(self) -> str:
        return (
            f"MetricPartition(tier={self.tier!r}, table_name={self.table_name!r}, "
            f"dropped={self.dropped_at is not None})"
        )


#: The three storage tiers, coarsest last. Ordered, because the query planner
#: in :mod:`hcc.services.metrics` walks them from finest to coarsest.
TIER_RAW: Final = "raw"
TIER_5M: Final = "5m"
TIER_1H: Final = "1h"
TIERS: Final = (TIER_RAW, TIER_5M, TIER_1H)

#: Each tier's bucket width in seconds. ``raw`` has none — a raw sample is an
#: instant, not a bucket — which is why it is absent rather than zero.
TIER_BUCKET_SECONDS: Final = {TIER_5M: 300, TIER_1H: 3600}

#: The table each tier lives in.
TIER_TABLES: Final = {
    TIER_RAW: "metric_samples_raw",
    TIER_5M: "metric_samples_5m",
    TIER_1H: "metric_samples_1h",
}

#: How wide one partition is, per tier.
#:
#: Different per tier so each has a useful *number* of partitions: daily for
#: raw gives 7-30 live ones at the default retention, weekly for five-minute
#: and monthly for hourly keep those in the tens rather than the thousands.
#: A thousand partitions is where planning time starts to dominate a query.
TIER_PARTITION_DAYS: Final = {TIER_RAW: 1, TIER_5M: 7, TIER_1H: 30}
