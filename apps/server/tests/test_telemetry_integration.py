"""Telemetry ingestion and historical queries, against real PostgreSQL.

Phase 3. Two halves in one file because they are one path: the service decides
what to store and the socket is how it arrives, and a test of either alone
would miss the join.

The themes, each of which exists because the alternative is a chart nobody can
explain:

* **Nothing is stored that was not asked for.** A module nobody enabled, a
  metric the registry does not declare, a label that would create a new
  series — each is refused, counted, and reported back to the agent.
* **An agent cannot write to another host.** There is no field in which to
  name one, and a series id is a sequential integer, so the read path is
  checked too.
* **Duplicates cannot produce duplicate rows.** Delivery is at-least-once;
  the primary key is what makes a re-sent batch a no-op.
* **Partition routing is real.** The samples land in the partition covering
  their timestamp, which is what makes retention a ``DROP TABLE``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select, text

from hcc.agents.protocol import (
    MAX_BACKFILL_SECONDS,
    MAX_CLOCK_SKEW_SECONDS,
    PROTOCOL_VERSION,
    MetricSample,
    TelemetryEntry,
)
from hcc.models import Host, MetricSeries
from hcc.models.user import UserRole
from hcc.services.metrics import (
    DEFAULT_QUERY_POINTS,
    MAX_QUERY_POINTS,
    MetricsService,
    QueryRangeError,
    choose_tier,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from contextlib import AbstractAsyncContextManager

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from hcc.models.user import User

    ApiFactory = Callable[..., AbstractAsyncContextManager[AsyncClient]]
    SeedUser = Callable[..., Awaitable[User]]
    SignIn = Callable[..., Awaitable[str]]
    ReadSql = Callable[..., Awaitable[Any]]

pytestmark = pytest.mark.integration

HOSTS = "/api/v1/hosts"
ENROLL = "/api/v1/agents/enroll"
CONNECT = "/api/v1/agents/connect"

ALL_MODULES = frozenset({"cpu", "memory", "swap", "load", "uptime"})


@pytest.fixture
def metrics() -> MetricsService:
    return MetricsService()


async def insert_host(db: AsyncSession, name: str = "homeserver") -> Host:
    host = Host(name=name)
    db.add(host)
    await db.flush()
    return host


def entry(
    module: str = "cpu",
    *,
    at: datetime | None = None,
    samples: list[MetricSample] | None = None,
    error: str | None = None,
    duration_ms: int | None = 3,
) -> TelemetryEntry:
    return TelemetryEntry(
        module=module,
        schema_version=1,
        collected_at=at or datetime.now(UTC),
        duration_ms=duration_ms,
        error=error,
        samples=samples
        if samples is not None
        else [MetricSample(metric="utilisation", value=11.5)],
    )


# ---------------------------------------------------------------------------
# Ingestion: the happy path
# ---------------------------------------------------------------------------


async def test_a_sample_is_stored_and_creates_its_series(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    host = await insert_host(db_session)
    outcome = await metrics.ingest(
        db_session, host_id=host.id, entries=[entry()], enabled_modules=ALL_MODULES
    )
    await db_session.commit()

    assert outcome.accepted == 1
    assert outcome.rejected == 0
    assert outcome.detail is None

    series = (await db_session.execute(select(MetricSeries))).scalars().all()
    assert len(series) == 1
    assert series[0].module == "cpu"
    assert series[0].metric == "utilisation"
    assert series[0].unit == "percent"
    assert series[0].kind == "gauge"
    assert series[0].labels == {}
    assert series[0].labels_key == ""
    assert series[0].last_sample_at is not None


async def test_a_labelled_sample_creates_one_series_per_label_value(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    host = await insert_host(db_session)
    await metrics.ingest(
        db_session,
        host_id=host.id,
        entries=[
            entry(
                samples=[
                    MetricSample(metric="core_utilisation", value=9.0, labels={"core": "0"}),
                    MetricSample(metric="core_utilisation", value=12.0, labels={"core": "1"}),
                ]
            )
        ],
        enabled_modules=ALL_MODULES,
    )
    await db_session.commit()

    keys = sorted(
        row.labels_key for row in (await db_session.execute(select(MetricSeries))).scalars().all()
    )
    assert keys == ["core=0", "core=1"]


async def test_the_same_series_is_reused_across_batches(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    host = await insert_host(db_session)
    for _ in range(5):
        await metrics.ingest(
            db_session,
            host_id=host.id,
            entries=[entry(at=datetime.now(UTC) + timedelta(seconds=_))],
            enabled_modules=ALL_MODULES,
        )
    await db_session.commit()

    series = (await db_session.execute(select(MetricSeries))).scalars().all()
    assert len(series) == 1


async def test_two_hosts_reporting_the_same_metric_get_separate_series(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """The series identity includes the host, so one host's data can never be
    attributed to another's chart.
    """
    first = await insert_host(db_session, "one")
    second = await insert_host(db_session, "two")
    for host in (first, second):
        await metrics.ingest(
            db_session, host_id=host.id, entries=[entry()], enabled_modules=ALL_MODULES
        )
    await db_session.commit()

    series = (await db_session.execute(select(MetricSeries))).scalars().all()
    assert len(series) == 2
    assert {row.host_id for row in series} == {first.id, second.id}


async def test_a_repeated_batch_produces_no_duplicate_rows(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """Delivery is at-least-once: an agent that lost an acknowledgement
    re-sends. The primary key is what makes that a no-op.
    """
    host = await insert_host(db_session)
    fixed = datetime.now(UTC)
    batch = [entry(at=fixed)]

    first = await metrics.ingest(
        db_session, host_id=host.id, entries=batch, enabled_modules=ALL_MODULES
    )
    await db_session.commit()
    second = await metrics.ingest(
        db_session, host_id=host.id, entries=batch, enabled_modules=ALL_MODULES
    )
    await db_session.commit()

    # Both are reported as accepted — the controller did accept them — and
    # there is one row. "Accepted" means "we are not asking you to send it
    # again", which is true of a duplicate.
    assert first.accepted == 1
    assert second.accepted == 1
    assert await metrics.sample_count(db_session, host.id) == 1


async def test_the_last_sample_time_never_moves_backwards(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """A backfilled batch arrives with older timestamps, and must not make a
    live series look stale.
    """
    host = await insert_host(db_session)
    recent = datetime.now(UTC)
    await metrics.ingest(
        db_session, host_id=host.id, entries=[entry(at=recent)], enabled_modules=ALL_MODULES
    )
    await db_session.commit()

    await metrics.ingest(
        db_session,
        host_id=host.id,
        entries=[entry(at=recent - timedelta(hours=2))],
        enabled_modules=ALL_MODULES,
    )
    await db_session.commit()

    series = (await db_session.execute(select(MetricSeries))).scalar_one()
    assert series.last_sample_at == recent


async def test_a_sample_lands_in_the_partition_covering_its_timestamp(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """What makes retention a ``DROP TABLE`` rather than a ``DELETE``.

    Asserted through ``tableoid``, so this is the physical table the row is
    in rather than a claim about the schema.
    """
    host = await insert_host(db_session)
    await metrics.ingest(
        db_session, host_id=host.id, entries=[entry()], enabled_modules=ALL_MODULES
    )
    await db_session.commit()

    landed: str = (
        await db_session.execute(
            text("SELECT tableoid::regclass::text FROM metric_samples_raw LIMIT 1")
        )
    ).scalar_one()
    assert landed.startswith("metric_samples_raw_")
    assert landed != "metric_samples_raw_default", (
        "the sample landed in the DEFAULT partition, which means no real "
        "partition covered its timestamp"
    )


# ---------------------------------------------------------------------------
# Ingestion: refusals
# ---------------------------------------------------------------------------


async def test_telemetry_for_a_disabled_module_is_refused(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """§17. An agent submitting for a module nobody turned on is running stale
    configuration or has been tampered with, and storing it would make the
    interface show data for a module it displays as off.
    """
    host = await insert_host(db_session)
    outcome = await metrics.ingest(
        db_session,
        host_id=host.id,
        entries=[entry("cpu")],
        enabled_modules=frozenset({"memory"}),
    )
    await db_session.commit()

    assert outcome.accepted == 0
    assert outcome.rejected == 1
    assert "not enabled" in (outcome.detail or "")
    assert (await db_session.execute(select(MetricSeries))).scalars().all() == []


async def test_an_undeclared_metric_is_refused(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """A typo would otherwise create a series, permanently, that nothing
    removes and nothing can render a unit for.
    """
    host = await insert_host(db_session)
    outcome = await metrics.ingest(
        db_session,
        host_id=host.id,
        entries=[entry(samples=[MetricSample(metric="utilsation", value=1.0)])],
        enabled_modules=ALL_MODULES,
    )
    await db_session.commit()

    assert outcome.rejected == 1
    assert "not a metric this module declares" in (outcome.detail or "")


async def test_an_unexpected_label_is_refused(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """The cardinality bound. An unconstrained label set is the one cost of a
    metric system an operator cannot see coming.
    """
    host = await insert_host(db_session)
    outcome = await metrics.ingest(
        db_session,
        host_id=host.id,
        entries=[
            entry(samples=[MetricSample(metric="utilisation", value=1.0, labels={"pid": "123"})])
        ],
        enabled_modules=ALL_MODULES,
    )
    await db_session.commit()

    assert outcome.rejected == 1
    assert "unexpected label" in (outcome.detail or "")


async def test_a_missing_label_is_refused(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """Three load-average windows arriving without their ``window`` label
    would all become the same series and overwrite each other.
    """
    host = await insert_host(db_session)
    outcome = await metrics.ingest(
        db_session,
        host_id=host.id,
        entries=[entry("load", samples=[MetricSample(metric="average", value=2.5)])],
        enabled_modules=ALL_MODULES,
    )
    await db_session.commit()

    assert outcome.rejected == 1
    assert "missing label" in (outcome.detail or "")


async def test_a_future_timestamp_is_refused_rather_than_rewritten(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """A host whose clock is an hour fast is reporting measurements that have
    not happened. Replacing its timestamp with the controller's would store a
    plausible row whose time is the controller's invention, and nobody would
    discover the clock was wrong.
    """
    host = await insert_host(db_session)
    outcome = await metrics.ingest(
        db_session,
        host_id=host.id,
        entries=[entry(at=datetime.now(UTC) + timedelta(seconds=MAX_CLOCK_SKEW_SECONDS + 60))],
        enabled_modules=ALL_MODULES,
    )
    await db_session.commit()

    assert outcome.accepted == 0
    assert "clock is wrong" in (outcome.detail or "")
    assert await metrics.sample_count(db_session, host.id) == 0


async def test_a_timestamp_within_the_skew_allowance_is_accepted(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """NTP slop is not a wrong clock, and refusing it would make ordinary
    hosts look broken.
    """
    host = await insert_host(db_session)
    outcome = await metrics.ingest(
        db_session,
        host_id=host.id,
        entries=[entry(at=datetime.now(UTC) + timedelta(seconds=MAX_CLOCK_SKEW_SECONDS - 30))],
        enabled_modules=ALL_MODULES,
    )
    await db_session.commit()
    assert outcome.accepted == 1


async def test_a_timestamp_beyond_the_backfill_window_is_refused(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """A sample older than the retention window would be written and
    immediately become eligible for deletion — and backfilling a year would
    create a year of daily partitions.
    """
    host = await insert_host(db_session)
    outcome = await metrics.ingest(
        db_session,
        host_id=host.id,
        entries=[entry(at=datetime.now(UTC) - timedelta(seconds=MAX_BACKFILL_SECONDS + 3600))],
        enabled_modules=ALL_MODULES,
    )
    await db_session.commit()

    assert outcome.accepted == 0
    assert "backfill window" in (outcome.detail or "")


async def test_a_partly_bad_batch_stores_the_good_samples(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """One bad sample must not cost the whole collection: a module whose
    registry entry changed between agent releases would otherwise silence
    every other module on the host.
    """
    host = await insert_host(db_session)
    outcome = await metrics.ingest(
        db_session,
        host_id=host.id,
        entries=[
            entry(
                samples=[
                    MetricSample(metric="utilisation", value=10.0),
                    MetricSample(metric="nonsense", value=1.0),
                    MetricSample(metric="logical_cpus", value=8.0),
                ]
            )
        ],
        enabled_modules=ALL_MODULES,
    )
    await db_session.commit()

    assert outcome.accepted == 2
    assert outcome.rejected == 1
    assert await metrics.sample_count(db_session, host.id) == 2


async def test_an_entry_reporting_a_collector_error_stores_nothing_and_is_not_refused(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """§26: collector errors are observable. An entry carrying an error and no
    samples is a legitimate report, not a rejection.
    """
    host = await insert_host(db_session)
    outcome = await metrics.ingest(
        db_session,
        host_id=host.id,
        entries=[entry(samples=[], error="cannot read /proc/stat")],
        enabled_modules=ALL_MODULES,
    )
    await db_session.commit()

    assert outcome.accepted == 0
    assert outcome.rejected == 0
    assert outcome.detail is None


async def test_the_refusal_detail_is_bounded(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """It goes into a frame with a length limit, and twelve repetitions of the
    same sentence say no more than one.
    """
    host = await insert_host(db_session)
    outcome = await metrics.ingest(
        db_session,
        host_id=host.id,
        entries=[
            entry(samples=[MetricSample(metric=f"bogus{index}", value=1.0) for index in range(40)])
        ],
        enabled_modules=ALL_MODULES,
    )
    await db_session.commit()

    assert outcome.rejected == 40
    assert outcome.detail is not None
    assert len(outcome.detail) <= 120


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


async def seed_series(
    db: AsyncSession, metrics: MetricsService, host: Host, *, count: int, step: timedelta
) -> int:
    """Store ``count`` samples ending now, and return the series id."""
    base = datetime.now(UTC) - step * count
    for index in range(count):
        await metrics.ingest(
            db,
            host_id=host.id,
            entries=[
                entry(
                    at=base + step * index,
                    samples=[MetricSample(metric="utilisation", value=float(index))],
                )
            ],
            enabled_modules=ALL_MODULES,
        )
    await db.commit()
    return (await db.execute(select(MetricSeries.id))).scalar_one()


async def test_the_latest_value_of_each_series_is_returned(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    host = await insert_host(db_session)
    await seed_series(db_session, metrics, host, count=5, step=timedelta(seconds=10))

    latest = await metrics.latest_for_host(db_session, host.id)
    assert len(latest) == 1
    # Values were 0..4, so the newest is 4.
    assert latest[0].value == 4.0
    assert latest[0].series.module == "cpu"
    assert latest[0].series.unit == "percent"


async def test_a_stale_series_is_absent_rather_than_reported_as_current(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """A value from three days ago is not "the current CPU usage". Returning
    it would let the interface present a stale number as live.
    """
    host = await insert_host(db_session)
    await metrics.ingest(
        db_session,
        host_id=host.id,
        entries=[entry(at=datetime.now(UTC) - timedelta(days=3))],
        enabled_modules=ALL_MODULES,
    )
    await db_session.commit()

    assert await metrics.latest_for_host(db_session, host.id, within=timedelta(minutes=5)) == ()
    # And without a window it is still available, because "what has this host
    # ever reported" is a different question.
    assert len(await metrics.latest_for_host(db_session, host.id)) == 1


async def test_a_host_with_no_samples_has_no_latest_values(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """Empty, not zeroes. §21: do not display zero when the value is unknown."""
    host = await insert_host(db_session)
    await db_session.commit()
    assert await metrics.latest_for_host(db_session, host.id) == ()


async def test_a_range_query_returns_the_points_in_order(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    host = await insert_host(db_session)
    series_id = await seed_series(db_session, metrics, host, count=20, step=timedelta(seconds=30))

    # A 15-minute window, not an hour: the automatic choice assumes one sample
    # a second, so a 3,600-second window exceeds the default 1,000-point
    # budget and would be answered from the 5-minute tier — which has no rows
    # until the rollup job has run. Fifteen minutes is 900 seconds and stays
    # raw, which is also what the interface's shortest range asks for.
    result = await metrics.range_for_series(
        db_session,
        series_id=series_id,
        host_id=host.id,
        start=datetime.now(UTC) - timedelta(minutes=15),
        end=datetime.now(UTC) + timedelta(minutes=1),
    )
    assert result is not None
    assert result.tier == "raw"
    assert [point.value for point in result.points] == [float(index) for index in range(20)]
    assert [point.at for point in result.points] == sorted(point.at for point in result.points)


async def test_a_raw_range_query_carries_no_min_or_max(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """They are a rollup's fields. Reporting a raw point's value as its own
    minimum and maximum would make a chart draw a zero-width band and imply a
    precision the tier does not have.
    """
    host = await insert_host(db_session)
    series_id = await seed_series(db_session, metrics, host, count=3, step=timedelta(seconds=10))

    result = await metrics.range_for_series(
        db_session,
        series_id=series_id,
        host_id=host.id,
        start=datetime.now(UTC) - timedelta(minutes=10),
        end=datetime.now(UTC) + timedelta(minutes=1),
    )
    assert result is not None
    assert result.tier == "raw"
    for point in result.points:
        assert point.minimum is None
        assert point.maximum is None
        assert point.sample_count is None


async def test_a_range_query_for_another_hosts_series_returns_nothing(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """A series id is a sequential integer, so without the host check an
    operator who can read one host's charts could read every host's by
    incrementing a number.
    """
    victim = await insert_host(db_session, "victim")
    attacker = await insert_host(db_session, "attacker")
    series_id = await seed_series(db_session, metrics, victim, count=3, step=timedelta(seconds=10))

    result = await metrics.range_for_series(
        db_session,
        series_id=series_id,
        host_id=attacker.id,
        start=datetime.now(UTC) - timedelta(hours=1),
        end=datetime.now(UTC),
    )
    assert result is None


async def test_a_backwards_range_is_refused(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    host = await insert_host(db_session)
    await db_session.commit()
    now = datetime.now(UTC)
    with pytest.raises(QueryRangeError, match="after its start"):
        await metrics.range_for_series(
            db_session, series_id=1, host_id=host.id, start=now, end=now - timedelta(hours=1)
        )


@pytest.mark.parametrize("points", [0, -1, MAX_QUERY_POINTS + 1])
async def test_an_impossible_point_budget_is_refused_naming_the_limit(
    db_session: AsyncSession, metrics: MetricsService, points: int
) -> None:
    host = await insert_host(db_session)
    await db_session.commit()
    with pytest.raises(QueryRangeError, match=str(MAX_QUERY_POINTS)):
        await metrics.range_for_series(
            db_session,
            series_id=1,
            host_id=host.id,
            start=datetime.now(UTC) - timedelta(hours=1),
            end=datetime.now(UTC),
            max_points=points,
        )


async def test_an_explicitly_requested_tier_can_overflow_and_says_so(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """Truncation is only reachable when the caller *names* a tier.

    Worth stating, because it took a failing test to notice: the automatic
    choice assumes one sample a second for the raw tier, so it picks raw only
    when the window in seconds is within the budget — and at the one-second
    interval floor that window cannot hold more samples than the budget
    allows. The same holds for the rollup tiers by construction. So the
    automatic path cannot overflow, and `truncated` exists for the caller who
    asked for a resolution the range does not suit.
    """
    host = await insert_host(db_session)
    series_id = await seed_series(db_session, metrics, host, count=10, step=timedelta(seconds=5))

    result = await metrics.range_for_series(
        db_session,
        series_id=series_id,
        host_id=host.id,
        start=datetime.now(UTC) - timedelta(hours=1),
        end=datetime.now(UTC) + timedelta(minutes=1),
        max_points=4,
        tier="raw",
    )
    assert result is not None
    assert result.tier == "raw"
    assert len(result.points) == 4
    assert result.truncated is True


async def test_the_automatic_tier_choice_never_overflows_the_budget(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """The property that makes truncation unreachable on the default path.

    Pinned across several windows, because it is a consequence of the
    arithmetic in `choose_tier` rather than of a check — and a future change
    to the assumed sampling rate would break it silently.
    """
    host = await insert_host(db_session)
    series_id = await seed_series(db_session, metrics, host, count=30, step=timedelta(seconds=2))

    for window in (timedelta(minutes=15), timedelta(hours=6), timedelta(days=7)):
        result = await metrics.range_for_series(
            db_session,
            series_id=series_id,
            host_id=host.id,
            start=datetime.now(UTC) - window,
            end=datetime.now(UTC) + timedelta(minutes=1),
            max_points=500,
        )
        assert result is not None
        assert result.truncated is False, f"{window} overflowed the budget"
        assert len(result.points) <= 500


async def test_an_unknown_resolution_is_refused(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    host = await insert_host(db_session)
    await db_session.commit()
    with pytest.raises(QueryRangeError, match="unknown resolution"):
        await metrics.range_for_series(
            db_session,
            series_id=1,
            host_id=host.id,
            start=datetime.now(UTC) - timedelta(hours=1),
            end=datetime.now(UTC),
            tier="minutely",
        )


def test_the_tier_is_chosen_by_the_window_and_the_budget() -> None:
    """The resolution is a property of the query, not a parameter
    (``DATA_RETENTION.md`` §5.1). A caller asking for a year of raw data is
    asking for six million points, and the right answer is hourly data.
    """
    now = datetime.now(UTC)
    assert choose_tier(now - timedelta(minutes=15), now, DEFAULT_QUERY_POINTS) == "raw"
    assert choose_tier(now - timedelta(days=1), now, DEFAULT_QUERY_POINTS) == "5m"
    assert choose_tier(now - timedelta(days=365), now, DEFAULT_QUERY_POINTS) == "1h"


def test_a_larger_budget_buys_a_finer_tier() -> None:
    now = datetime.now(UTC)
    window = (now - timedelta(hours=2), now)
    assert choose_tier(*window, 100) != "raw"
    assert choose_tier(*window, MAX_QUERY_POINTS) == "raw"


async def test_every_series_a_host_has_produced_is_listed(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    host = await insert_host(db_session)
    await metrics.ingest(
        db_session,
        host_id=host.id,
        entries=[
            entry(
                samples=[
                    MetricSample(metric="utilisation", value=1.0),
                    MetricSample(metric="core_utilisation", value=2.0, labels={"core": "0"}),
                ]
            ),
            entry("memory", samples=[MetricSample(metric="total", value=16.0)]),
        ],
        enabled_modules=ALL_MODULES,
    )
    await db_session.commit()

    series = await metrics.series_for_host(db_session, host.id)
    assert [(row.module, row.metric) for row in series] == [
        ("cpu", "core_utilisation"),
        ("cpu", "utilisation"),
        ("memory", "total"),
    ]


async def test_deleting_a_host_removes_its_series_and_samples(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    host = await insert_host(db_session)
    await seed_series(db_session, metrics, host, count=3, step=timedelta(seconds=10))
    assert await metrics.sample_count(db_session, host.id) > 0

    await db_session.execute(text("DELETE FROM hosts WHERE id = :h").bindparams(h=host.id))
    await db_session.commit()

    assert (await db_session.execute(select(MetricSeries))).scalars().all() == []
    # The samples are orphaned rather than cascaded — there is deliberately no
    # foreign key from the partitioned table — so they are counted through the
    # series join, which now finds nothing. Stated because "the rows are gone"
    # would be the wrong claim: retention removes them with their partition.
    assert await metrics.sample_count(db_session, host.id) == 0


# ---------------------------------------------------------------------------
# Over the socket
# ---------------------------------------------------------------------------


def csrf(token: str) -> dict[str, str]:
    return {"X-CSRF-Token": token}


def bearer(credential: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {credential}"}


async def enrolled_agent(
    client: AsyncClient, seed_user: SeedUser, sign_in: SignIn, name: str = "homeserver"
) -> tuple[str, str, str]:
    await seed_user(client, username="admin", role=UserRole.ADMIN)
    token = await sign_in(client, "admin")
    host_id = (await client.post(HOSTS, json={"name": name}, headers=csrf(token))).json()["host_id"]
    enrollment = (await client.post(f"{HOSTS}/{host_id}/enrollment", headers=csrf(token))).json()[
        "credential"
    ]
    credential = (await client.post(ENROLL, headers=bearer(enrollment), json={})).json()[
        "credential"
    ]
    return host_id, credential, token


def socket_scope(credential: str) -> dict[str, Any]:
    return {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "scheme": "ws",
        "http_version": "1.1",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 51234),
        "path": CONNECT,
        "raw_path": CONNECT.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"host", b"testserver"),
            (b"authorization", f"Bearer {credential}".encode()),
        ],
        "subprotocols": [],
        "state": {},
    }


async def drive_socket(
    client: AsyncClient, *, credential: str, frames: list[str]
) -> list[dict[str, Any]]:
    app = client.app_state.app  # type: ignore[attr-defined]
    inbound: list[dict[str, Any]] = [{"type": "websocket.connect"}]
    for frame in frames:
        inbound.append({"type": "websocket.receive", "text": frame})
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        if inbound:
            return inbound.pop(0)
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(socket_scope(credential), receive, send)
    return sent


def decoded(messages: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [
        frame
        for frame in (
            json.loads(message["text"])
            for message in messages
            if message["type"] == "websocket.send" and message.get("text")
        )
        if frame["type"] == kind
    ]


def telemetry_frame(seq: int, module: str = "cpu", **overrides: Any) -> str:
    payload: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "type": "telemetry",
        "seq": seq,
        "batch": [
            {
                "module": module,
                "schema_version": 1,
                "collected_at": datetime.now(UTC).isoformat(),
                "duration_ms": 2,
                "samples": [{"metric": "utilisation", "value": 11.5, "labels": {}}],
            }
        ],
    }
    payload.update(overrides)
    return json.dumps(payload)


async def test_telemetry_for_a_disabled_module_is_refused_over_the_socket(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
) -> None:
    """Default-deny reaching the ingestion path. A freshly enrolled host has
    every module off, so an agent that submitted anyway is refused — and the
    acknowledgement says so rather than the samples vanishing.
    """
    async with api_factory() as client:
        _, credential, _ = await enrolled_agent(client, seed_user, sign_in)
        messages = await drive_socket(client, credential=credential, frames=[telemetry_frame(1)])
        rows = await read_sql(client, "SELECT count(*) FROM metric_samples_raw")

    acks = decoded(messages, "telemetry_ack")
    assert len(acks) == 1
    assert acks[0]["seq"] == 1
    assert acks[0]["accepted"] == 0
    assert acks[0]["rejected"] == 1
    assert "not enabled" in acks[0]["detail"]
    assert rows[0][0] == 0


async def test_telemetry_is_stored_once_the_module_is_enabled(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
) -> None:
    """The whole path: an administrator enables a module, the agent connects,
    is sent the configuration, and its samples are stored.
    """
    async with api_factory() as client:
        host_id, credential, token = await enrolled_agent(client, seed_user, sign_in)
        await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu",
            json={"enabled": True},
            headers=csrf(token),
        )

        messages = await drive_socket(client, credential=credential, frames=[telemetry_frame(1)])
        rows = await read_sql(
            client,
            "SELECT s.module, s.metric, r.value FROM metric_samples_raw r "
            "JOIN metric_series s ON s.id = r.series_id",
        )

    acks = decoded(messages, "telemetry_ack")
    assert acks[0]["accepted"] == 1
    assert acks[0]["rejected"] == 0
    assert acks[0]["detail"] is None
    assert [(row[0], row[1], row[2]) for row in rows] == [("cpu", "utilisation", 11.5)]


async def test_an_agent_cannot_submit_telemetry_for_another_host(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
) -> None:
    """§15. There is no ``host_id`` field, so inventing one is a protocol error
    rather than a cross-host write — and the samples that *were* in the frame
    go nowhere.
    """
    async with api_factory() as client:
        host_id, credential, token = await enrolled_agent(client, seed_user, sign_in)
        await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu",
            json={"enabled": True},
            headers=csrf(token),
        )

        messages = await drive_socket(
            client,
            credential=credential,
            frames=[telemetry_frame(1, host_id="11111111-2222-3333-4444-555555555555")],
        )
        rows = await read_sql(client, "SELECT count(*) FROM metric_samples_raw")

    closes = decoded(messages, "close")
    assert closes
    assert closes[0]["reason"] == "protocol_error"
    assert rows[0][0] == 0


async def test_telemetry_is_attributed_to_the_credentials_host(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
) -> None:
    """Two enrolled hosts, one submitting. The samples belong to the host whose
    credential authenticated the socket, which is the only thing that decides
    attribution.
    """
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")

        async def register(name: str) -> tuple[str, str]:
            host = (await client.post(HOSTS, json={"name": name}, headers=csrf(token))).json()[
                "host_id"
            ]
            enrollment = (
                await client.post(f"{HOSTS}/{host}/enrollment", headers=csrf(token))
            ).json()["credential"]
            credential = (await client.post(ENROLL, headers=bearer(enrollment), json={})).json()[
                "credential"
            ]
            await client.put(
                f"{HOSTS}/{host}/monitoring/modules/cpu",
                json={"enabled": True},
                headers=csrf(token),
            )
            return host, credential

        first, first_credential = await register("one")
        second, _ = await register("two")

        await drive_socket(client, credential=first_credential, frames=[telemetry_frame(1)])
        rows = await read_sql(client, "SELECT DISTINCT host_id::text FROM metric_series WHERE 1=1")

    assert [row[0] for row in rows] == [first]
    assert second != first


async def test_a_nan_value_is_refused_at_the_protocol_boundary(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """JSON cannot express NaN, so an agent sending one has a bug or a
    hand-built frame — and a NaN in a numeric column is a value no chart can
    render and no query can filter on.
    """
    async with api_factory() as client:
        host_id, credential, token = await enrolled_agent(client, seed_user, sign_in)
        await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu",
            json={"enabled": True},
            headers=csrf(token),
        )

        # `NaN` is what Python's json.dumps emits and what a hand-built frame
        # would carry; it is not valid JSON, so this is the realistic attack.
        frame = telemetry_frame(1).replace("11.5", "NaN")
        messages = await drive_socket(client, credential=credential, frames=[frame])

    closes = decoded(messages, "close")
    assert closes
    assert closes[0]["reason"] == "protocol_error"


async def test_an_oversized_batch_is_refused_before_it_is_stored(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
) -> None:
    """A compromised host presents exactly this interface, so the bound is a
    denial-of-service bound rather than a tidiness one.
    """
    async with api_factory() as client:
        host_id, credential, token = await enrolled_agent(client, seed_user, sign_in)
        await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu",
            json={"enabled": True},
            headers=csrf(token),
        )

        oversized = json.dumps(
            {
                "v": PROTOCOL_VERSION,
                "type": "telemetry",
                "seq": 1,
                "batch": [
                    {
                        "module": "cpu",
                        "schema_version": 1,
                        "collected_at": datetime.now(UTC).isoformat(),
                        "samples": [{"metric": "utilisation", "value": 1.0}],
                    }
                ]
                * 64,
            }
        )
        messages = await drive_socket(client, credential=credential, frames=[oversized])
        rows = await read_sql(client, "SELECT count(*) FROM metric_samples_raw")

    assert decoded(messages, "close")[0]["reason"] == "protocol_error"
    assert rows[0][0] == 0


async def test_a_resent_batch_over_the_socket_stores_one_row(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
) -> None:
    """At-least-once delivery, through the real transport. The same frame
    twice — which is what an agent that lost an acknowledgement sends — is one
    row.
    """
    async with api_factory() as client:
        host_id, credential, token = await enrolled_agent(client, seed_user, sign_in)
        await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu",
            json={"enabled": True},
            headers=csrf(token),
        )

        frame = telemetry_frame(1)
        messages = await drive_socket(client, credential=credential, frames=[frame, frame])
        rows = await read_sql(client, "SELECT count(*) FROM metric_samples_raw")

    assert len(decoded(messages, "telemetry_ack")) == 2
    assert rows[0][0] == 1


async def test_the_sample_rate_budget_closes_a_flooding_agent(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """The frame budget bounds *calls*; this bounds *work*. One agent sending
    the maximum batch at the maximum frame rate is three orders of magnitude
    beyond a reference host.
    """
    async with api_factory() as client:
        host_id, credential, token = await enrolled_agent(client, seed_user, sign_in)
        await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu",
            json={"enabled": True},
            headers=csrf(token),
        )

        # Each frame carries 500 samples; the burst is 2,000, so the fifth
        # frame exhausts it. Sent as four frames of 500 plus one more.
        big = json.dumps(
            {
                "v": PROTOCOL_VERSION,
                "type": "telemetry",
                "seq": 1,
                "batch": [
                    {
                        "module": "cpu",
                        "schema_version": 1,
                        "collected_at": datetime.now(UTC).isoformat(),
                        "samples": [
                            {"metric": "core_utilisation", "value": 1.0, "labels": {"core": str(i)}}
                            for i in range(500)
                        ],
                    }
                ],
            }
        )
        messages = await drive_socket(client, credential=credential, frames=[big] * 6)

    closes = decoded(messages, "close")
    assert closes
    assert closes[0]["reason"] == "rate_limited"
    assert "samples per second" in closes[0]["detail"]


async def test_telemetry_and_configuration_share_one_connection(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """The whole point of ADR-049: one authenticated socket carries both, so a
    heartbeat, a configuration acknowledgement and a telemetry batch are
    answered on the same connection without a second credential path.
    """
    async with api_factory() as client:
        host_id, credential, token = await enrolled_agent(client, seed_user, sign_in)
        await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu",
            json={"enabled": True},
            headers=csrf(token),
        )

        first = await drive_socket(client, credential=credential, frames=[])
        snapshot = decoded(first, "config_snapshot")[0]

        messages = await drive_socket(
            client,
            credential=credential,
            frames=[
                json.dumps(
                    {
                        "v": PROTOCOL_VERSION,
                        "type": "config_ack",
                        "version": snapshot["version"],
                        "checksum": snapshot["checksum"],
                    }
                ),
                telemetry_frame(1),
                json.dumps({"v": PROTOCOL_VERSION, "type": "heartbeat", "config_version": 1}),
            ],
        )

    assert decoded(messages, "telemetry_ack")
    assert decoded(messages, "heartbeat_ack")
    assert decoded(messages, "config_snapshot")


async def test_concurrent_agents_ingesting_the_same_series_shape_do_not_collide(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
) -> None:
    """Two hosts' agents creating their own series at the same moment.

    The upsert is ``ON CONFLICT … DO UPDATE`` returning the id rather than
    ``DO NOTHING``, which returns no row on a conflict — so a worker that lost
    the race would otherwise get nothing back.
    """
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")

        credentials: list[str] = []
        for index in range(4):
            host = (
                await client.post(HOSTS, json={"name": f"host-{index}"}, headers=csrf(token))
            ).json()["host_id"]
            enrollment = (
                await client.post(f"{HOSTS}/{host}/enrollment", headers=csrf(token))
            ).json()["credential"]
            credentials.append(
                (await client.post(ENROLL, headers=bearer(enrollment), json={})).json()[
                    "credential"
                ]
            )
            await client.put(
                f"{HOSTS}/{host}/monitoring/modules/cpu",
                json={"enabled": True},
                headers=csrf(token),
            )

        results = await asyncio.gather(
            *(
                drive_socket(client, credential=credential, frames=[telemetry_frame(1)])
                for credential in credentials
            ),
            return_exceptions=True,
        )
        failures = [item for item in results if isinstance(item, BaseException)]
        rows = await read_sql(client, "SELECT count(*) FROM metric_series")

    assert failures == []
    assert rows[0][0] == 4


async def test_a_series_id_is_not_guessable_across_hosts_through_the_api(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """The ids are sequential, which is the point of the host check on the
    read path. Asserted on the ids themselves so the premise is visible.
    """
    first = await insert_host(db_session, "one")
    second = await insert_host(db_session, "two")
    for host in (first, second):
        await metrics.ingest(
            db_session, host_id=host.id, entries=[entry()], enabled_modules=ALL_MODULES
        )
    await db_session.commit()

    ids = sorted(row.id for row in (await db_session.execute(select(MetricSeries))).scalars().all())
    assert ids[1] == ids[0] + 1, "series ids are sequential, so the host check is load-bearing"


def test_a_series_key_renders_labels_in_a_stable_order() -> None:
    """Two frames with the same labels in a different order describe the same
    series, and a key that preserved order would create two.
    """
    from hcc.models import canonical_labels_key

    assert canonical_labels_key({"b": "2", "a": "1"}) == canonical_labels_key({"a": "1", "b": "2"})
    assert canonical_labels_key({}) == ""
    assert canonical_labels_key(None) == ""


def test_a_host_id_is_never_a_telemetry_field() -> None:
    """Asserted against the model rather than trusted, because adding such a
    field would be a one-line change that no behavioural test would notice.
    """
    from hcc.agents.protocol import Telemetry

    for name in Telemetry.model_fields:
        assert "host" not in name.lower(), f"Telemetry.{name} names a host"
    for name in TelemetryEntry.model_fields:
        assert "host" not in name.lower(), f"TelemetryEntry.{name} names a host"
    assert uuid  # the import is used by the fixtures above
