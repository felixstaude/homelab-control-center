"""Why a chart is empty when collection is on and the agent is connected.

Phase M of Operation Horizon. The failure-injection work asked "what does an
operator see when a disk is pulled out?" and the answer was: an enabled
module, a connected agent, no data, and no explanation, for ever.

The agent had always said why. ``TelemetryEntry.error`` carries it, and its
own description in ``hcc/agents/protocol.py`` reads *"a module that has been
failing for an hour is information an operator needs"*. ``ingest`` read
``entry.samples`` and nothing else, so the field was parsed, length-checked,
and discarded.

These tests are what stops that happening again. They are integration tests
because the behaviour is in an UPSERT — ``consecutive_failures`` increments
against the row as it exists, and a fake would be asserting the test's own
arithmetic rather than PostgreSQL's.

The distinctions each group defends:

* **Ran and produced nothing is not failed.** A rate collector's first
  interval legitimately reports no samples. Calling that a failure would make
  the first minute after every agent restart look broken.
* **Recovered is not "never failed".** The error text survives a success, and
  only the counter resets, so a module that fails and recovers every few
  minutes is visible rather than self-erasing.
* **Never reported is not healthy.** A module with no row is absent from the
  health map, and the API reports nulls rather than a zero that would read as
  "working".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from hcc.agents.protocol import MetricSample, TelemetryEntry
from hcc.models import Host
from hcc.models.monitoring import MAX_COLLECTOR_ERROR_LENGTH, HostModuleHealth
from hcc.services.metrics import MetricsService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

ENABLED = frozenset({"cpu", "memory", "diskio", "docker", "systemd"})


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
    error: str | None = None,
    samples: list[MetricSample] | None = None,
) -> TelemetryEntry:
    """One module's collection.

    ``samples`` defaults to one valid CPU sample for the ``cpu`` module and
    to none for anything else, so a caller asking about ``diskio`` does not
    have to know which metrics that module declares in order to make a point
    about its health.
    """
    if samples is None:
        samples = [MetricSample(metric="utilisation", value=11.5)] if module == "cpu" else []
    return TelemetryEntry(
        module=module,
        schema_version=1,
        collected_at=at or datetime.now(UTC),
        duration_ms=3,
        error=error,
        samples=samples,
    )


async def health_of(db: AsyncSession, host: Host, module: str) -> HostModuleHealth | None:
    return (
        await db.execute(
            select(HostModuleHealth).where(
                HostModuleHealth.host_id == host.id,
                HostModuleHealth.module_id == module,
            )
        )
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# The failure is recorded at all
# ---------------------------------------------------------------------------


async def test_a_failing_collector_is_recorded_with_its_reason(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """The defect this whole table exists for.

    Before Phase M this assertion had nothing to assert against: the error
    arrived, was validated, and went nowhere.
    """
    host = await insert_host(db_session)
    await metrics.ingest(
        db_session,
        host_id=host.id,
        entries=[entry("diskio", error="/proc/diskstats is not readable")],
        enabled_modules=ENABLED,
    )
    await db_session.commit()

    row = await health_of(db_session, host, "diskio")
    assert row is not None
    assert row.last_error == "/proc/diskstats is not readable"
    assert row.last_error_at is not None
    assert row.consecutive_failures == 1
    assert row.last_success_at is None


async def test_the_failure_count_climbs_across_batches(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """One per batch, not one per call to anything.

    The increment is in the UPSERT's ``DO UPDATE`` clause rather than read
    then written, so two overlapping connections for one host cannot lose a
    count. That it climbs at all is what this asserts.
    """
    host = await insert_host(db_session)
    for attempt in range(3):
        await metrics.ingest(
            db_session,
            host_id=host.id,
            entries=[entry("docker", error=f"the Docker daemon did not answer ({attempt})")],
            enabled_modules=ENABLED,
        )
        await db_session.commit()

    row = await health_of(db_session, host, "docker")
    assert row is not None
    assert row.consecutive_failures == 3
    # The newest reason, not the first: an operator reading this wants the
    # error the collector is raising now.
    assert row.last_error == "the Docker daemon did not answer (2)"


async def test_a_success_resets_the_count_and_keeps_the_last_error(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """Recovered is not "never failed".

    A module that fails and recovers every few minutes is a real condition —
    a flapping Docker daemon, a disk that spins down — and a schema that
    cleared its own evidence on every success could not express it.
    """
    host = await insert_host(db_session)
    await metrics.ingest(
        db_session,
        host_id=host.id,
        entries=[entry("docker", error="the Docker daemon did not answer")],
        enabled_modules=ENABLED,
    )
    await db_session.commit()
    await metrics.ingest(
        db_session, host_id=host.id, entries=[entry("docker")], enabled_modules=ENABLED
    )
    await db_session.commit()

    row = await health_of(db_session, host, "docker")
    assert row is not None
    assert row.consecutive_failures == 0
    assert row.last_success_at is not None
    assert row.last_error == "the Docker daemon did not answer"
    assert row.last_error_at is not None


async def test_a_second_failure_after_a_recovery_starts_from_one(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """Consecutive means consecutive.

    Were the reset a no-op — the obvious bug in a ``+ 1`` written without a
    CASE — this would read 2, and "failing for two intervals" would be the
    report for a module that failed once.
    """
    host = await insert_host(db_session)
    for outcome in ("the daemon went away", None, "the daemon went away"):
        await metrics.ingest(
            db_session,
            host_id=host.id,
            entries=[entry("docker", error=outcome)],
            enabled_modules=ENABLED,
        )
        await db_session.commit()

    row = await health_of(db_session, host, "docker")
    assert row is not None
    assert row.consecutive_failures == 1


# ---------------------------------------------------------------------------
# What is not a failure
# ---------------------------------------------------------------------------


async def test_a_collection_that_produced_no_samples_is_a_success(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """The first interval of every rate collector.

    ``diskio`` reports nothing on its first collection — one snapshot of a
    monotonic counter is not a rate — and on any interval where a counter
    went backwards. Calling those failures would make the first minute after
    every agent restart look broken, and would bury the real failures in
    noise nobody could filter.
    """
    host = await insert_host(db_session)
    await metrics.ingest(
        db_session, host_id=host.id, entries=[entry("diskio")], enabled_modules=ENABLED
    )
    await db_session.commit()

    row = await health_of(db_session, host, "diskio")
    assert row is not None
    assert row.consecutive_failures == 0
    assert row.last_success_at is not None
    assert row.last_error is None
    assert row.last_error_at is None


async def test_a_module_that_is_not_enabled_gets_no_health_row(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """Switched off is not unhealthy.

    Also the bound on what an agent can create here: an agent sending entries
    for every module in the registry cannot populate rows for modules the
    operator never turned on.
    """
    host = await insert_host(db_session)
    await metrics.ingest(
        db_session,
        host_id=host.id,
        entries=[entry("sensors", error="no hwmon devices")],
        enabled_modules=ENABLED,
    )
    await db_session.commit()

    assert await health_of(db_session, host, "sensors") is None


async def test_a_module_that_never_reported_has_no_row(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """Absent, rather than a synthetic row that reads as healthy.

    "Never heard from" and "working" are different answers and the second is
    the one a zeroed row would give.
    """
    host = await insert_host(db_session)
    await metrics.ingest(
        db_session, host_id=host.id, entries=[entry("cpu")], enabled_modules=ENABLED
    )
    await db_session.commit()

    assert await health_of(db_session, host, "memory") is None
    reported = await metrics.health_for_host(db_session, host.id)
    assert set(reported) == {"cpu"}


# ---------------------------------------------------------------------------
# The controller's own refusals
# ---------------------------------------------------------------------------


async def test_a_clock_that_is_wrong_is_recorded_as_the_failure_it_is(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """The one failure the agent cannot report.

    From inside a host whose clock is four hours fast, nothing is wrong: the
    collector runs, produces samples, and hands them over. The controller
    refuses every one of them, and without this the host shows an enabled
    module, a connected agent and no data — the exact symptom, with the cause
    visible only in a server log.
    """
    host = await insert_host(db_session)
    outcome = await metrics.ingest(
        db_session,
        host_id=host.id,
        entries=[entry("cpu", at=datetime.now(UTC) + timedelta(hours=4))],
        enabled_modules=ENABLED,
    )
    await db_session.commit()

    assert outcome.accepted == 0
    assert outcome.rejected == 1

    row = await health_of(db_session, host, "cpu")
    assert row is not None
    assert row.consecutive_failures == 1
    assert row.last_error is not None
    assert "clock" in row.last_error or "future" in row.last_error
    assert row.last_success_at is None


# ---------------------------------------------------------------------------
# Bounds and hostile input
# ---------------------------------------------------------------------------


async def test_the_longest_error_the_protocol_allows_still_fits(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """Two bounds for one value, and they have to agree.

    The protocol refuses a longer frame; the column is what physically holds
    it. If the column were the narrower of the two, a valid frame would raise
    ``StringDataRightTruncationError`` inside the WebSocket handler — which
    the agent sees only as ``protocol_error``, and retries for ever. That is
    not hypothetical: it is defect 5 of this run, on ``metric_series.unit``.
    """
    host = await insert_host(db_session)
    longest = "x" * MAX_COLLECTOR_ERROR_LENGTH
    await metrics.ingest(
        db_session,
        host_id=host.id,
        entries=[entry("diskio", error=longest)],
        enabled_modules=ENABLED,
    )
    await db_session.commit()

    row = await health_of(db_session, host, "diskio")
    assert row is not None
    assert row.last_error == longest


async def test_an_over_long_controller_refusal_is_truncated_rather_than_refused(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """The second bound, for the strings the protocol does not check.

    An agent's error is bounded by the protocol before it ever reaches here.
    The controller's own refusals are not: they are f-strings over values
    from the frame, and `_timestamp_problem` interpolates a skew count that
    nothing bounds. One character too long raises
    ``StringDataRightTruncationError`` inside the WebSocket handler, which
    the agent sees only as ``protocol_error`` and retries for ever.

    Built with ``model_construct``, which skips validation — the protocol
    bound would refuse this frame, and skipping it is how "suppose that
    bound were relaxed, or suppose the controller writes a longer refusal of
    its own" is expressed. Through ``ingest`` rather than the private
    recorder, so what is asserted is the behaviour of the path that runs.
    """
    host = await insert_host(db_session)
    overlong = TelemetryEntry.model_construct(
        module="diskio",
        schema_version=1,
        collected_at=datetime.now(UTC),
        duration_ms=3,
        error="y" * (MAX_COLLECTOR_ERROR_LENGTH * 3),
        samples=[],
    )
    await metrics.ingest(db_session, host_id=host.id, entries=[overlong], enabled_modules=ENABLED)
    await db_session.commit()

    row = await health_of(db_session, host, "diskio")
    assert row is not None
    assert row.last_error == "y" * MAX_COLLECTOR_ERROR_LENGTH


async def test_an_error_containing_markup_is_stored_verbatim(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """Untrusted text from a host that may be compromised.

    Stored as it arrived rather than sanitised here: escaping belongs at the
    point of rendering, and a column holding pre-escaped text would be one
    where every later reader has to guess which encoding it is in. What this
    asserts is that nothing *interprets* it on the way in.
    """
    host = await insert_host(db_session)
    hostile = "<script>alert(1)</script> '; DROP TABLE hosts; --"
    await metrics.ingest(
        db_session,
        host_id=host.id,
        entries=[entry("diskio", error=hostile)],
        enabled_modules=ENABLED,
    )
    await db_session.commit()

    row = await health_of(db_session, host, "diskio")
    assert row is not None
    assert row.last_error == hostile
    # The table the string asked to drop is still there.
    assert (await db_session.execute(select(Host).where(Host.id == host.id))).scalar_one()


# ---------------------------------------------------------------------------
# One row per host per module
# ---------------------------------------------------------------------------


async def test_two_hosts_failing_the_same_module_do_not_share_a_row(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    host_one = await insert_host(db_session, "one")
    host_two = await insert_host(db_session, "two")

    await metrics.ingest(
        db_session,
        host_id=host_one.id,
        entries=[entry("docker", error="daemon gone on one")],
        enabled_modules=ENABLED,
    )
    await metrics.ingest(
        db_session, host_id=host_two.id, entries=[entry("docker")], enabled_modules=ENABLED
    )
    await db_session.commit()

    first = await health_of(db_session, host_one, "docker")
    second = await health_of(db_session, host_two, "docker")
    assert first is not None
    assert second is not None
    assert first.consecutive_failures == 1
    assert second.consecutive_failures == 0


async def test_one_batch_records_every_module_in_it(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """One statement for the whole batch, which is the point of the array form.

    Eleven modules reporting on one tick is eleven rows; eleven round trips
    per batch per host would be the most expensive thing a fleet did.
    """
    host = await insert_host(db_session)
    await metrics.ingest(
        db_session,
        host_id=host.id,
        entries=[
            entry("cpu"),
            entry("diskio", error="the device went away"),
            entry("docker", error="the Docker daemon did not answer"),
        ],
        enabled_modules=ENABLED,
    )
    await db_session.commit()

    reported = await metrics.health_for_host(db_session, host.id)
    assert set(reported) == {"cpu", "diskio", "docker"}
    assert reported["cpu"].failing is False
    assert reported["diskio"].failing is True
    assert reported["docker"].failing is True


async def test_deleting_a_host_takes_its_health_with_it(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """A collector's state on a host that no longer exists is not a fact."""
    host = await insert_host(db_session)
    await metrics.ingest(
        db_session,
        host_id=host.id,
        entries=[entry("diskio", error="gone")],
        enabled_modules=ENABLED,
    )
    await db_session.commit()

    await db_session.delete(host)
    await db_session.commit()

    assert (await db_session.execute(select(HostModuleHealth))).scalars().all() == []


# ---------------------------------------------------------------------------
# The API surface
# ---------------------------------------------------------------------------


async def test_the_status_endpoint_reports_the_failure(
    db_session: AsyncSession, metrics: MetricsService
) -> None:
    """The fourth reason a chart is empty, visible through the API.

    Asserted through the service rather than over HTTP here; the HTTP shape
    is covered by ``test_metrics_api_integration.py``. What matters is that
    the module's own row is reachable from the host id alone, which is all
    the endpoint has.
    """
    host = await insert_host(db_session)
    await metrics.ingest(
        db_session,
        host_id=host.id,
        entries=[entry("diskio", error="/proc/diskstats is not readable")],
        enabled_modules=ENABLED,
    )
    await db_session.commit()

    reported = await metrics.health_for_host(db_session, host.id)
    assert reported["diskio"].last_error == "/proc/diskstats is not readable"
    assert reported["diskio"].failing is True
    assert reported["diskio"].last_reported_at is not None
