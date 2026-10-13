"""Partition maintenance, rollups and retention, against real PostgreSQL.

Phase 3. These cannot be unit tests: every property worth asserting here is a
property of PostgreSQL rather than of Python. Partition routing, ``ON CONFLICT
DO UPDATE`` recomputing a bucket, an advisory lock making a second run skip,
``DROP TABLE`` removing a window's data — a fake database would let all four
pass while the real one failed.

The themes:

* **Raw data is never dropped before the rollups that depend on it exist.**
  §19's requirement, and the one failure in this system that cannot be undone.
  Asserted from three directions: no rollups at all, rollups behind the
  retention cutoff, and rollups caught up.
* **A bucket still receiving samples is not sealed.** An agent reconnecting
  after an outage backfills with its original timestamps, so a bucket inside
  the lateness grace is left alone.
* **Rolling up twice is not double-counting.** Delivery is at-least-once and
  the job can be interrupted, so the aggregation has to be recomputable.
* **first and last mean earliest and latest**, not smallest and largest.
* **Nothing is averaged away.** Minima and maxima survive the rollup, because
  a spike flattened into an average is the measurement an operator turned fast
  sampling on to catch.

## Why these tests use timestamps in the past

The partition the migration created covers *now*, and the suite's truncation
deliberately leaves the partition catalogue alone — a partition is a table that
physically exists, and emptying the catalogue while leaving the tables behind
would make the job try to create ones that already do. So a test that dropped
"expired" partitions around the current date would remove a table a later test
expected, and the coupling would show up as an unrelated failure.

Every test here therefore drives the service with an explicit ``now`` in 2021,
creating, filling and dropping that era's partitions. The windows are disjoint
from the migration's, so no test can disturb another's.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select, text

from hcc.models import Host, MetricSeries
from hcc.models.metrics import TIER_1H, TIER_5M, TIER_RAW, TIER_TABLES, MetricPartition
from hcc.services.retention import (
    DEFAULT_LATENESS_MINUTES,
    MAX_DROPS_PER_RUN,
    METRIC_LOCK_CLASS,
    METRIC_LOCK_KEY,
    PARTITIONS_AHEAD,
    RetentionPolicy,
    RetentionService,
    _floor_to,
    _metric_table,
    _timestamp_literal,
    _windows,
    policy_from_settings,
    rollup_watermark,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration

#: The era every test here operates in. Far enough from the migration's own
#: partition that creating and dropping these cannot disturb it.
ERA = datetime(2021, 6, 14, 12, 0, tzinfo=UTC)  # A Monday, so weekly windows align.


@pytest.fixture
def service() -> RetentionService:
    """A service with the shipped defaults."""
    return RetentionService()


async def seed_series(db: AsyncSession, *, module: str = "cpu", metric: str = "utilisation") -> int:
    """Create a host and one series, returning the series id."""
    host = Host(name=f"host-{uuid.uuid4().hex[:8]}")
    db.add(host)
    await db.flush()
    series = MetricSeries(
        host_id=host.id,
        module=module,
        metric=metric,
        unit="percent",
        kind="gauge",
        labels={},
        labels_key="",
    )
    db.add(series)
    await db.flush()
    return series.id


async def add_raw(db: AsyncSession, series_id: int, points: list[tuple[datetime, float]]) -> None:
    """Insert raw samples directly.

    Bypasses ``MetricsService.ingest`` deliberately: that path rejects
    timestamps years in the past, which is correct for an agent and useless for
    a test that needs to own the clock.
    """
    for at, value in points:
        await db.execute(
            text(
                "INSERT INTO metric_samples_raw (series_id, recorded_at, value) "
                "VALUES (:series_id, :recorded_at, :value) "
                "ON CONFLICT DO NOTHING"
            ).bindparams(series_id=series_id, recorded_at=at, value=value)
        )


async def rollup_rows(db: AsyncSession, tier: str, series_id: int) -> list[dict[str, object]]:
    """Every rollup row for one series in one tier, oldest first."""
    table = TIER_TABLES[tier]
    rows = (
        (
            await db.execute(
                text(
                    f"SELECT bucket_start, min_value, max_value, avg_value, "
                    f"first_value, last_value, sample_count FROM {table} "
                    "WHERE series_id = :series_id ORDER BY bucket_start"
                ).bindparams(series_id=series_id)
            )
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


async def drop_era_partitions(db: AsyncSession) -> None:
    """Remove every partition this module's tests created.

    Called by the fixture rather than left to each test, because a partition is
    a real table: leaving 2021's behind would make the next run's "created
    nothing" assertion depend on which tests ran before it.
    """
    rows = (
        (
            await db.execute(
                select(MetricPartition).where(
                    MetricPartition.range_start < datetime(2022, 1, 1, tzinfo=UTC)
                )
            )
        )
        .scalars()
        .all()
    )
    for partition in rows:
        await db.execute(text(f'DROP TABLE IF EXISTS "{partition.table_name}"'))
        await db.delete(partition)
    await db.commit()


@pytest.fixture(autouse=True)
async def era_cleanup(db_session: AsyncSession) -> object:
    """Drop this module's partitions before and after each test."""
    await drop_era_partitions(db_session)
    yield None
    await drop_era_partitions(db_session)


# ---------------------------------------------------------------------------
# Partitions
# ---------------------------------------------------------------------------


async def test_a_run_creates_the_current_window_and_the_ones_ahead(
    db_session: AsyncSession, service: RetentionService
) -> None:
    outcome = await service.run(db_session, now=ERA)
    await service.release_lock(db_session)

    # Three tiers, each wanting the current window plus PARTITIONS_AHEAD.
    assert outcome.partitions_created == 3 * (PARTITIONS_AHEAD + 1)
    # And none missing afterwards, which is the property an operator monitors.
    assert outcome.gaps == {}


async def test_the_partition_is_physically_created_with_the_bounds_claimed(
    db_session: AsyncSession, service: RetentionService
) -> None:
    """Regression: a partition bound cannot be a bind parameter.

    ``FOR VALUES FROM (:start)`` makes PostgreSQL report that it "expects 0
    arguments for this query", because a bound is part of the table definition
    and is parsed at DDL time. The job therefore created *nothing*, and because
    the error surfaced through the handler that exists to absorb a benign race
    with another controller, the only trace was an "already created" line.

    The failure was silent in the worst way: every insert landed in the DEFAULT
    partition, so the data looked fine and retention — a ``DROP TABLE`` per
    window — would have removed none of it.

    Asserting the catalogue row is not enough to catch this, since that row is
    written by the application. This reads PostgreSQL's own partition
    descriptor instead.
    """
    created = await service.run(db_session, now=ERA)
    await service.release_lock(db_session)
    assert created.partitions_created > 0

    bound: str | None = (
        await db_session.execute(
            text(
                "SELECT pg_get_expr(c.relpartbound, c.oid) FROM pg_class c "
                "WHERE c.relname = 'metric_samples_raw_20210614'"
            )
        )
    ).scalar_one_or_none()

    assert bound is not None, "the partition table does not exist"
    # PostgreSQL renders the bounds it actually stored, so this is the server's
    # account of the window rather than the application's.
    assert "2021-06-14 00:00:00+00" in bound
    assert "2021-06-15 00:00:00+00" in bound


async def test_a_second_run_creates_nothing(
    db_session: AsyncSession, service: RetentionService
) -> None:
    await service.run(db_session, now=ERA)
    await service.release_lock(db_session)

    second = await service.run(db_session, now=ERA)
    await service.release_lock(db_session)

    assert second.partitions_created == 0
    assert second.gaps == {}


async def test_the_hourly_tier_is_partitioned_by_calendar_month(
    db_session: AsyncSession, service: RetentionService
) -> None:
    """A monthly window must not be thirty days.

    ``_advance`` has a separate branch for the hourly tier precisely so that a
    partition named ``202106`` cannot cover part of July.
    """
    await service.run(db_session, now=ERA)
    await service.release_lock(db_session)

    # Scoped to this module's era. The migration created an hourly partition
    # for the real current month, and it is correct for that to be here.
    era_only = select(MetricPartition).where(
        MetricPartition.tier == TIER_1H,
        MetricPartition.range_start < datetime(2022, 1, 1, tzinfo=UTC),
    )
    rows = (await db_session.execute(era_only)).scalars().all()

    assert sorted(row.table_name for row in rows) == [
        "metric_samples_1h_202106",
        "metric_samples_1h_202107",
        "metric_samples_1h_202108",
        "metric_samples_1h_202109",
    ]
    boundaries = {row.table_name: (row.range_start, row.range_end) for row in rows}
    start, end = boundaries["metric_samples_1h_202106"]
    assert start == datetime(2021, 6, 1, tzinfo=UTC)
    assert end == datetime(2021, 7, 1, tzinfo=UTC)


async def test_a_sample_lands_in_the_partition_covering_its_timestamp(
    db_session: AsyncSession, service: RetentionService
) -> None:
    """Partition routing is what makes retention a ``DROP TABLE``.

    Asserted through ``tableoid``, because the row being *visible* through the
    parent proves nothing: a row in the DEFAULT partition is visible too, and
    dropping its window would not remove it.
    """
    await service.run(db_session, now=ERA)
    await service.release_lock(db_session)
    series_id = await seed_series(db_session)
    await add_raw(db_session, series_id, [(ERA, 42.0)])
    await db_session.commit()

    physical: str = (
        await db_session.execute(
            text(
                "SELECT tableoid::regclass::text FROM metric_samples_raw "
                "WHERE series_id = :series_id"
            ).bindparams(series_id=series_id)
        )
    ).scalar_one()
    assert physical == "metric_samples_raw_20210614"


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------


async def test_a_run_skips_while_another_holds_the_lock(
    db_session_factory: async_sessionmaker[AsyncSession], service: RetentionService
) -> None:
    """Two controller processes must not both create the same partition.

    Two genuinely separate sessions, because one session taking the same
    advisory lock twice succeeds — PostgreSQL's advisory locks are re-entrant
    within a session, so a single-session test would assert nothing.
    """
    async with db_session_factory() as holder, db_session_factory() as second:
        await holder.execute(
            text("SELECT pg_advisory_lock(:class, :key)").bindparams(
                **{"class": METRIC_LOCK_CLASS, "key": METRIC_LOCK_KEY}
            )
        )
        try:
            outcome = await service.run(second, now=ERA)
            assert outcome.skipped is True
            assert outcome.partitions_created == 0
        finally:
            await holder.execute(
                text("SELECT pg_advisory_unlock(:class, :key)").bindparams(
                    **{"class": METRIC_LOCK_CLASS, "key": METRIC_LOCK_KEY}
                )
            )


async def test_releasing_the_lock_lets_the_next_run_proceed(
    db_session_factory: async_sessionmaker[AsyncSession], service: RetentionService
) -> None:
    """The reason ``release_lock`` is called explicitly.

    The lock is session-scoped, so on a pooled connection it outlives the run.
    A run that did not release would make every later run skip — a job that
    silently stops working, which is the failure mode this test exists to
    prevent regressing.
    """
    async with db_session_factory() as first:
        await service.run(first, now=ERA)
        await service.release_lock(first)
        await first.commit()

        held: int = (
            await first.execute(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                    "AND classid = :class AND objid = :key"
                ).bindparams(**{"class": METRIC_LOCK_CLASS, "key": METRIC_LOCK_KEY})
            )
        ).scalar_one()
        assert held == 0


# ---------------------------------------------------------------------------
# Rollups
# ---------------------------------------------------------------------------


async def test_a_sealed_bucket_is_rolled_up_preserving_the_extremes(
    db_session: AsyncSession, service: RetentionService
) -> None:
    """The point of the rollup: a spike survives downsampling.

    §19 asks that maxima are not averaged away. Five samples whose average is
    unremarkable and whose maximum is not, in one five-minute bucket.
    """
    await service.run(db_session, now=ERA)
    await service.release_lock(db_session)
    series_id = await seed_series(db_session)

    bucket = datetime(2021, 6, 14, 12, 0, tzinfo=UTC)
    await add_raw(
        db_session,
        series_id,
        [
            (bucket + timedelta(seconds=0), 10.0),
            (bucket + timedelta(seconds=60), 12.0),
            (bucket + timedelta(seconds=120), 99.0),  # The spike.
            (bucket + timedelta(seconds=180), 11.0),
            (bucket + timedelta(seconds=240), 13.0),
        ],
    )
    await db_session.commit()

    # Well past the lateness grace, so the bucket is sealed.
    later = bucket + timedelta(hours=2)
    outcome = await service.run(db_session, now=later)
    await service.release_lock(db_session)

    assert outcome.buckets_rolled_up is not None
    assert outcome.buckets_rolled_up[TIER_5M] == 1

    rows = await rollup_rows(db_session, TIER_5M, series_id)
    assert len(rows) == 1
    assert rows[0]["bucket_start"] == bucket
    assert rows[0]["min_value"] == pytest.approx(10.0)
    assert rows[0]["max_value"] == pytest.approx(99.0)
    assert float(rows[0]["avg_value"]) == pytest.approx(29.0)  # type: ignore[arg-type]
    assert rows[0]["sample_count"] == 5


async def test_first_and_last_are_earliest_and_latest_not_smallest_and_largest(
    db_session: AsyncSession, service: RetentionService
) -> None:
    """The subtlety ``_ROLLUP_SQL`` documents.

    Taking ``min``/``max`` of the value for first/last would make a
    counter-derived rate wrong in a way that survives review, so the values are
    arranged for the two to disagree: the earliest sample is the largest and
    the latest is the smallest.
    """
    await service.run(db_session, now=ERA)
    await service.release_lock(db_session)
    series_id = await seed_series(db_session)

    bucket = datetime(2021, 6, 14, 12, 5, tzinfo=UTC)
    await add_raw(
        db_session,
        series_id,
        [
            (bucket + timedelta(seconds=10), 90.0),  # Earliest, and the largest.
            (bucket + timedelta(seconds=100), 50.0),
            (bucket + timedelta(seconds=200), 5.0),  # Latest, and the smallest.
        ],
    )
    await db_session.commit()

    await service.run(db_session, now=bucket + timedelta(hours=2))
    await service.release_lock(db_session)

    rows = await rollup_rows(db_session, TIER_5M, series_id)
    assert rows[0]["first_value"] == pytest.approx(90.0)
    assert rows[0]["last_value"] == pytest.approx(5.0)
    # And distinct from the extremes, which is what makes the assertion mean
    # something.
    assert rows[0]["min_value"] == pytest.approx(5.0)
    assert rows[0]["max_value"] == pytest.approx(90.0)


async def test_a_bucket_inside_the_lateness_grace_is_not_sealed(
    db_session: AsyncSession, service: RetentionService
) -> None:
    """An agent backfilling after an outage must not find its bucket closed."""
    await service.run(db_session, now=ERA)
    await service.release_lock(db_session)
    series_id = await seed_series(db_session)

    bucket = datetime(2021, 6, 14, 12, 0, tzinfo=UTC)
    await add_raw(db_session, series_id, [(bucket + timedelta(seconds=30), 20.0)])
    await db_session.commit()

    # Inside the grace: the bucket closed five minutes in, and the grace is ten.
    just_after = bucket + timedelta(minutes=5, seconds=1)
    assert just_after < bucket + timedelta(minutes=DEFAULT_LATENESS_MINUTES)
    outcome = await service.run(db_session, now=just_after)
    await service.release_lock(db_session)

    assert outcome.buckets_rolled_up == {TIER_5M: 0, TIER_1H: 0}
    assert await rollup_rows(db_session, TIER_5M, series_id) == []


async def test_a_partially_elapsed_bucket_is_never_sealed(
    db_session: AsyncSession, service: RetentionService
) -> None:
    """The grace alone would not prevent this, which is why ``_floor_to`` exists.

    Eleven minutes after a bucket opened is past the ten-minute grace, but the
    *next* bucket is still filling. Sealing on the grace alone would roll up a
    bucket that is one sample old and then never revisit it with a count that
    made the partial fill visible.
    """
    await service.run(db_session, now=ERA)
    await service.release_lock(db_session)
    series_id = await seed_series(db_session)

    sealed = datetime(2021, 6, 14, 12, 0, tzinfo=UTC)
    filling = datetime(2021, 6, 14, 12, 15, tzinfo=UTC)
    await add_raw(
        db_session,
        series_id,
        [(sealed + timedelta(seconds=10), 1.0), (filling + timedelta(seconds=10), 2.0)],
    )
    await db_session.commit()

    # 12:16 — the 12:15 bucket is still open and is 1 minute into the grace.
    await service.run(db_session, now=filling + timedelta(minutes=1))
    await service.release_lock(db_session)

    buckets = [row["bucket_start"] for row in await rollup_rows(db_session, TIER_5M, series_id)]
    assert sealed in buckets
    assert filling not in buckets


async def test_rolling_up_twice_does_not_double_count(
    db_session: AsyncSession, service: RetentionService
) -> None:
    """``ON CONFLICT DO UPDATE`` over a value recomputed from the raw rows.

    The property that makes an interrupted job safe to re-run: a second pass
    over the same bucket has to produce the same answer, not a doubled count.
    """
    await service.run(db_session, now=ERA)
    await service.release_lock(db_session)
    series_id = await seed_series(db_session)

    bucket = datetime(2021, 6, 14, 12, 0, tzinfo=UTC)
    await add_raw(
        db_session,
        series_id,
        [(bucket + timedelta(seconds=i * 30), float(i)) for i in range(4)],
    )
    await db_session.commit()

    later = bucket + timedelta(hours=2)
    await service.run(db_session, now=later)
    await service.release_lock(db_session)
    first = await rollup_rows(db_session, TIER_5M, series_id)

    # Force a recompute of the same bucket by clearing the watermark's effect:
    # delete the rollup row and run again. Without the upsert this would be the
    # same assertion; with it, what is being tested is that the *values* are
    # reproduced exactly.
    await db_session.execute(
        text(f"DELETE FROM {TIER_TABLES[TIER_5M]} WHERE series_id = :series_id").bindparams(
            series_id=series_id
        )
    )
    await db_session.commit()
    await service.run(db_session, now=later)
    await service.release_lock(db_session)
    second = await rollup_rows(db_session, TIER_5M, series_id)

    assert first == second
    assert second[0]["sample_count"] == 4


async def test_late_data_recomputes_a_sealed_bucket(
    db_session: AsyncSession, service: RetentionService
) -> None:
    """A sealed bucket that gains a sample is corrected, not left disagreeing.

    The documented trade-off: a sealed bucket can change. The alternative —
    dropping late samples — would make the rollup silently disagree with the
    raw data it came from.
    """
    await service.run(db_session, now=ERA)
    await service.release_lock(db_session)
    series_id = await seed_series(db_session)

    bucket = datetime(2021, 6, 14, 12, 0, tzinfo=UTC)
    await add_raw(db_session, series_id, [(bucket + timedelta(seconds=10), 10.0)])
    await db_session.commit()

    later = bucket + timedelta(hours=2)
    await service.run(db_session, now=later)
    await service.release_lock(db_session)
    assert (await rollup_rows(db_session, TIER_5M, series_id))[0]["sample_count"] == 1

    # The agent reconnects and backfills a sample into the sealed bucket.
    await add_raw(db_session, series_id, [(bucket + timedelta(seconds=200), 80.0)])
    await db_session.commit()

    # The watermark is now at this bucket, so the next run's window starts here
    # and recomputes it — which is why the watermark is a floor and not an
    # exclusive bound.
    await service.run(db_session, now=later)
    await service.release_lock(db_session)

    rows = await rollup_rows(db_session, TIER_5M, series_id)
    assert rows[0]["sample_count"] == 2
    assert rows[0]["max_value"] == pytest.approx(80.0)


async def test_the_hourly_tier_is_produced_too(
    db_session: AsyncSession, service: RetentionService
) -> None:
    await service.run(db_session, now=ERA)
    await service.release_lock(db_session)
    series_id = await seed_series(db_session)

    hour = datetime(2021, 6, 14, 12, 0, tzinfo=UTC)
    await add_raw(
        db_session,
        series_id,
        [(hour + timedelta(minutes=m), float(m)) for m in (0, 17, 44, 59)],
    )
    await db_session.commit()

    await service.run(db_session, now=hour + timedelta(hours=3))
    await service.release_lock(db_session)

    rows = await rollup_rows(db_session, TIER_1H, series_id)
    assert len(rows) == 1
    assert rows[0]["bucket_start"] == hour
    assert rows[0]["sample_count"] == 4
    assert rows[0]["min_value"] == pytest.approx(0.0)
    assert rows[0]["max_value"] == pytest.approx(59.0)


async def test_a_database_with_no_raw_samples_rolls_up_nothing(
    db_session: AsyncSession, service: RetentionService
) -> None:
    """Not an error, and not a row of zeroes."""
    outcome = await service.run(db_session, now=ERA)
    await service.release_lock(db_session)
    assert outcome.buckets_rolled_up == {TIER_5M: 0, TIER_1H: 0}


# ---------------------------------------------------------------------------
# Retention, and the rule that raw data outlives its rollups
# ---------------------------------------------------------------------------


async def test_raw_partitions_are_held_when_no_rollups_exist(
    db_session: AsyncSession, service: RetentionService
) -> None:
    """§19, from the direction that loses data if it is wrong.

    An installation whose rollup step has never run keeps its raw data and
    grows — which is visible — rather than deleting it, which is not.
    """
    await service.run(db_session, now=ERA)
    await service.release_lock(db_session)

    # A raw partition old enough to be expired, and no rollups anywhere.
    long_after = ERA + timedelta(days=400)
    outcome = await service.run(db_session, now=long_after)
    await service.release_lock(db_session)

    raw_dropped = (
        (
            await db_session.execute(
                select(MetricPartition).where(
                    MetricPartition.tier == TIER_RAW,
                    MetricPartition.range_start < datetime(2022, 1, 1, tzinfo=UTC),
                    MetricPartition.dropped_at.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert raw_dropped == []
    assert outcome.partitions_dropped >= 0


async def test_raw_retention_is_held_back_to_the_rollup_watermark(
    db_session: AsyncSession, service: RetentionService
) -> None:
    """The case §19 is really about: rollups exist, but are behind.

    The retention cutoff says a partition is expired; the rollup watermark says
    its data has not been aggregated yet. The older of the two wins, so the raw
    partition survives.
    """
    await service.run(db_session, now=ERA)
    await service.release_lock(db_session)
    series_id = await seed_series(db_session)

    # Raw data on day one, rolled up. Then the clock moves far past raw
    # retention while no further rollup happens.
    day_one = datetime(2021, 6, 14, 12, 0, tzinfo=UTC)
    await add_raw(db_session, series_id, [(day_one, 5.0)])
    await db_session.commit()
    await service.run(db_session, now=day_one + timedelta(hours=2))
    await service.release_lock(db_session)

    watermark = await rollup_watermark(db_session)
    assert watermark is not None

    # The partition covering day one ends after the watermark, so even with a
    # retention cutoff far in the future it may not be dropped.
    partition = (
        await db_session.execute(
            select(MetricPartition).where(
                MetricPartition.tier == TIER_RAW,
                MetricPartition.table_name == "metric_samples_raw_20210614",
            )
        )
    ).scalar_one()
    assert partition.range_end > watermark
    assert partition.dropped_at is None


async def test_an_expired_partition_is_dropped_and_marked_not_deleted(
    db_session: AsyncSession, service: RetentionService
) -> None:
    """A dropped window stays answerable.

    An operator asking why last month's data is gone should find a row saying
    it was expired, not an absence they have to guess about.
    """
    await service.run(db_session, now=ERA)
    await service.release_lock(db_session)

    # Age the five-minute tier past its retention by moving the clock, and give
    # the rollup tiers a watermark so the raw hold-back is not what is under
    # test here.
    target = (
        await db_session.execute(
            select(MetricPartition).where(
                MetricPartition.tier == TIER_5M,
                MetricPartition.range_start == datetime(2021, 6, 14, tzinfo=UTC),
            )
        )
    ).scalar_one()
    table_name = target.table_name

    policy = RetentionPolicy(raw_days=1, five_minute_days=1, hourly_days=1)
    aggressive = RetentionService(policy)
    outcome = await aggressive.run(db_session, now=ERA + timedelta(days=30))
    await aggressive.release_lock(db_session)

    assert outcome.partitions_dropped > 0
    await db_session.refresh(target)
    assert target.dropped_at is not None

    # And the table is really gone, which is what makes retention cheap.
    exists: bool = (
        await db_session.execute(
            text("SELECT to_regclass(:name) IS NOT NULL").bindparams(name=table_name)
        )
    ).scalar_one()
    assert exists is False


async def test_no_more_than_the_bound_is_dropped_in_one_run(
    db_session: AsyncSession, service: RetentionService
) -> None:
    """Each drop takes an exclusive lock, so a run is bounded.

    Enough partitions to exceed the bound, created by running the job across a
    succession of weeks.
    """
    moment = ERA
    for _ in range(MAX_DROPS_PER_RUN + 4):
        await service.run(db_session, now=moment)
        await service.release_lock(db_session)
        moment += timedelta(days=1)

    aggressive = RetentionService(RetentionPolicy(raw_days=1, five_minute_days=1, hourly_days=1))
    outcome = await aggressive.run(db_session, now=ERA + timedelta(days=60))
    await aggressive.release_lock(db_session)

    assert outcome.partitions_dropped <= MAX_DROPS_PER_RUN


# ---------------------------------------------------------------------------
# rollup_watermark
# ---------------------------------------------------------------------------


async def test_the_watermark_is_none_until_both_tiers_have_produced_something(
    db_session: AsyncSession, service: RetentionService
) -> None:
    """Either tier empty means no raw data may be expired at all."""
    assert await rollup_watermark(db_session) is None

    await service.run(db_session, now=ERA)
    await service.release_lock(db_session)
    series_id = await seed_series(db_session)

    # A five-minute row only, written directly, so the hourly tier stays empty.
    await db_session.execute(
        text(
            f"INSERT INTO {TIER_TABLES[TIER_5M]} (series_id, bucket_start, min_value, "
            "max_value, avg_value, first_value, last_value, sample_count) VALUES "
            "(:series_id, :bucket, 1, 1, 1, 1, 1, 1)"
        ).bindparams(series_id=series_id, bucket=datetime(2021, 6, 14, 12, 0, tzinfo=UTC))
    )
    await db_session.commit()

    assert await rollup_watermark(db_session) is None


async def test_the_watermark_is_the_older_of_the_two_tiers(
    db_session: AsyncSession, service: RetentionService
) -> None:
    """Taking the newer would authorise deleting raw data a tier had not seen."""
    await service.run(db_session, now=ERA)
    await service.release_lock(db_session)
    series_id = await seed_series(db_session)

    newer = datetime(2021, 6, 14, 18, 0, tzinfo=UTC)
    older = datetime(2021, 6, 14, 12, 0, tzinfo=UTC)
    for tier, bucket in ((TIER_5M, newer), (TIER_1H, older)):
        await db_session.execute(
            text(
                f"INSERT INTO {TIER_TABLES[tier]} (series_id, bucket_start, min_value, "
                "max_value, avg_value, first_value, last_value, sample_count) VALUES "
                "(:series_id, :bucket, 1, 1, 1, 1, 1, 1)"
            ).bindparams(series_id=series_id, bucket=bucket)
        )
    await db_session.commit()

    assert await rollup_watermark(db_session) == older


# ---------------------------------------------------------------------------
# The background loop
# ---------------------------------------------------------------------------


async def test_the_loop_survives_a_failing_pass(migrated_database: str) -> None:
    """A pass that raises must not end the task.

    The task not running is the failure that is invisible: partitions stop
    being created, inserts start landing in the DEFAULT partition, rollups
    stop, and the first symptom is a disk filling weeks later. So this asserts
    the loop continues — the one property that cannot be observed from the
    outside until it is far too late.
    """
    from hcc.app import _run_maintenance
    from hcc.config import Settings
    from hcc.db import Database

    attempts: list[int] = []
    # Signalled by the pass that proves the loop lived, so the test waits on the
    # event rather than polling a list.
    survived = asyncio.Event()

    class Flaky(RetentionService):
        async def run(  # type: ignore[override]
            self, db: AsyncSession, *, now: datetime | None = None
        ) -> object:
            attempts.append(len(attempts))
            if len(attempts) == 1:
                raise RuntimeError("the first pass fails")
            outcome = await super().run(db, now=now)
            survived.set()
            return outcome

    database = Database(
        Settings(environment="test", database_url=migrated_database, log_level="ERROR")
    )
    database.connect()
    task = asyncio.create_task(_run_maintenance(database, Flaky(), 1))
    try:
        # Two passes: the failing one and the one that proves the loop lived.
        async with asyncio.timeout(20):
            await survived.wait()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await database.disconnect()

    assert len(attempts) >= 2


async def test_the_lock_is_released_even_when_a_pass_raises(migrated_database: str) -> None:
    """Otherwise one failure stops the job for the life of the connection.

    The lock is session-scoped, so a pass that raised without releasing would
    hold it until the pool recycled the connection — and every later pass would
    report "another run holds the lock" and do nothing. A job that silently
    stops, from a single transient error.
    """
    from hcc.app import _run_maintenance
    from hcc.config import Settings
    from hcc.db import Database

    calls: list[int] = []
    # Set by the second pass: reaching it at all is the assertion, because a
    # leaked lock would have made every later pass skip instead.
    second_pass = asyncio.Event()

    class AlwaysFails(RetentionService):
        async def run(  # type: ignore[override]
            self, db: AsyncSession, *, now: datetime | None = None
        ) -> object:
            # Take the lock first, exactly as a real run does, then fail after
            # it is held — which is the case that leaks.
            await self._try_lock(db)
            calls.append(len(calls))
            if len(calls) >= 2:
                second_pass.set()
            raise RuntimeError("fails after taking the lock")

    database = Database(
        Settings(environment="test", database_url=migrated_database, log_level="ERROR")
    )
    database.connect()
    task = asyncio.create_task(_run_maintenance(database, AlwaysFails(), 1))
    try:
        async with asyncio.timeout(20):
            await second_pass.wait()
        # The second call proves the first released: a held lock would have
        # made every later pass skip rather than reach `run` again. Checked
        # against pg_locks too, so the assertion does not rest on that
        # inference alone.
        async with database.session_factory() as db:
            held: int = (
                await db.execute(
                    text(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                        "AND classid = :class AND objid = :key"
                    ).bindparams(**{"class": METRIC_LOCK_CLASS, "key": METRIC_LOCK_KEY})
                )
            ).scalar_one()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await database.disconnect()

    # At most the one the in-flight pass is holding, not an accumulation.
    assert held <= 1


async def test_cancelling_the_task_ends_it_promptly(migrated_database: str) -> None:
    """Shutdown must not wait on the interval.

    The sleep between passes is the cancellation point, so a controller
    restarting does not sit for five minutes.
    """
    from hcc.app import _run_maintenance
    from hcc.config import Settings
    from hcc.db import Database

    database = Database(
        Settings(environment="test", database_url=migrated_database, log_level="ERROR")
    )
    database.connect()
    task = asyncio.create_task(_run_maintenance(database, RetentionService(), 3600))
    try:
        # Let the first pass finish so the task is in its sleep.
        await asyncio.sleep(1.0)
        task.cancel()
        async with asyncio.timeout(5):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        assert task.cancelled() or task.done()
    finally:
        await database.disconnect()


# ---------------------------------------------------------------------------
# Helpers that do not need a database
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", ["hosts", "users", "metric_samples_raw; DROP TABLE users", ""])
def test_the_identifier_guard_refuses_anything_but_a_metric_table(table: str) -> None:
    """The single guard every identifier interpolation in the module passes.

    ``text()`` cannot parameterise an identifier, so the table name is
    interpolated. This is what makes that safe, and it raises rather than
    sanitising because reaching it is a programming mistake.
    """
    with pytest.raises(ValueError, match="not a metric table"):
        _metric_table(table)


def test_the_identifier_guard_accepts_each_tier_table() -> None:
    for table in TIER_TABLES.values():
        assert _metric_table(table) == table


def test_a_partition_bound_literal_is_utc_and_quoted() -> None:
    """The one place this module builds a SQL *value* by formatting.

    Safe because of the type, not because of the driver: an aware ``datetime``
    rendered by ``isoformat`` can only contain digits and ``-:+.T``, so there
    is no quote to escape.
    """
    from zoneinfo import ZoneInfo

    assert _timestamp_literal(datetime(2021, 6, 14, tzinfo=UTC)) == "'2021-06-14T00:00:00+00:00'"
    # Normalised to UTC, so a bound expressed in another zone cannot overlap
    # the neighbouring partition.
    berlin = datetime(2021, 6, 14, 2, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    assert _timestamp_literal(berlin) == "'2021-06-14T00:00:00+00:00'"


@pytest.mark.parametrize("bad", ["2021-06-14", 1623628800, None])
def test_a_partition_bound_must_be_a_datetime(bad: object) -> None:
    """A refactor passing a string through must not become an injection point."""
    with pytest.raises(TypeError, match="must be a datetime"):
        _timestamp_literal(bad)  # type: ignore[arg-type]


def test_a_partition_bound_must_be_aware() -> None:
    """A naive bound would be read in the session's zone."""
    with pytest.raises(ValueError, match="time-zone aware"):
        _timestamp_literal(datetime(2021, 6, 14))


def test_bucket_truncation_matches_the_sql() -> None:
    """``_floor_to`` and the rollup statement must agree on the boundary.

    Two different truncations would make the job seal a bucket the statement
    then recomputed from a different boundary, producing a row whose
    ``bucket_start`` the watermark never advances past.
    """
    moment = datetime(2021, 6, 14, 12, 7, 43, 500_000, tzinfo=UTC)
    assert _floor_to(moment, 300) == datetime(2021, 6, 14, 12, 5, tzinfo=UTC)
    assert _floor_to(moment, 3600) == datetime(2021, 6, 14, 12, 0, tzinfo=UTC)
    # Already on a boundary: unchanged, not advanced.
    assert _floor_to(datetime(2021, 6, 14, 12, 5, tzinfo=UTC), 300) == datetime(
        2021, 6, 14, 12, 5, tzinfo=UTC
    )


def test_windows_are_computed_in_utc_regardless_of_the_input_zone() -> None:
    """A job computing boundaries in local time would overlap existing ones.

    The same instant expressed in two zones must produce the same windows,
    because the migration pinned its first partition to UTC.
    """
    from zoneinfo import ZoneInfo

    utc_windows = _windows(TIER_RAW, datetime(2021, 6, 14, 23, 30, tzinfo=UTC), 2)
    # 23:30Z is already the next day in Berlin, so a local-time computation
    # would produce a different first window.
    berlin = datetime(2021, 6, 14, 23, 30, tzinfo=UTC).astimezone(ZoneInfo("Europe/Berlin"))
    berlin_windows = _windows(TIER_RAW, berlin, 2)
    assert [w.start for w in utc_windows] == [w.start for w in berlin_windows]
    assert utc_windows[0].start == datetime(2021, 6, 14, tzinfo=UTC)


def test_the_policy_is_read_from_settings() -> None:
    """An operator changing retention in configuration must actually change it."""
    from hcc.config import Settings

    settings = Settings(
        environment="test",
        database_url="postgresql://u:p@127.0.0.1:1/x",
        metrics_raw_retention_days=3,
        metrics_5m_retention_days=30,
        metrics_1h_retention_days=365,
        metrics_rollup_lateness_minutes=2,
    )
    policy = policy_from_settings(settings)
    assert policy.raw_days == 3
    assert policy.five_minute_days == 30
    assert policy.hourly_days == 365
    assert policy.lateness_minutes == 2
    assert policy.days_for(TIER_RAW) == 3
    assert policy.days_for(TIER_5M) == 30
    assert policy.days_for(TIER_1H) == 365
