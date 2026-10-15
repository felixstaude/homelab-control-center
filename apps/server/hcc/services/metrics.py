"""Metric ingestion, series identity and historical queries.

Phase 3. The write path an agent's telemetry reaches, and the read path a
chart comes from.

## Every sample is validated against the registry, not just against its shape

The protocol bounds a frame's *size and types* (:mod:`hcc.agents.protocol`).
This module answers the different question: is this a measurement the
controller asked for? Four checks, and each exists because the alternative is
a series nobody can explain:

* **The module must be enabled for this host.** An agent that submits
  telemetry for a module nobody turned on is either running stale
  configuration or has been tampered with, and storing it would make the
  interface show data for a module it displays as off (§17).
* **The metric must be one the module declares.** A typo would otherwise
  create a new series, permanently, and nothing would ever remove it.
* **The labels must match the declared label keys.** This is the cardinality
  bound: labels are what turn one series into a thousand, and an unconstrained
  label set is the one cost of a metric system an operator cannot see coming.
* **The timestamp must be plausible.** Too far ahead and every chart gets a
  point in the future; too far behind and the write creates partitions for a
  window retention has already passed.

A refused sample is **counted and reported back**, never silently dropped. The
acknowledgement carries ``accepted`` and ``rejected``, so an agent whose
configuration has drifted can be seen to be submitting things the controller
does not want.

## Series identity is cached, because the alternative is a query per sample

A series is ``(host, module, metric, labels)``. Resolving that to an id with a
``SELECT`` per sample would be thirty queries per collection per host; at the
default intervals across a fleet that dominates everything else the controller
does. So resolved ids are cached in the process, bounded, and the cache is
only ever *added* to — a series is created once and never changes, so a stale
entry is impossible rather than merely unlikely.

The cache is per process and is not a correctness dependency: a miss costs one
upsert, and the upsert is ``ON CONFLICT … DO UPDATE`` returning the id, so two
workers racing to create the same series both get the same one.

## Delivery is at-least-once, with idempotent writes

Stated plainly because the alternative claim would be wrong. An agent that
sends a batch and loses the acknowledgement re-sends it on reconnection. The
sample primary key is ``(series_id, recorded_at)`` and the insert is
``ON CONFLICT DO NOTHING``, so the re-send changes nothing. That is not
exactly-once delivery — the frame genuinely arrives twice — but it does mean
duplicates cannot produce duplicate rows, which is the property that matters
for stored data.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from hcc.agents.protocol import (
    MAX_BACKFILL_SECONDS,
    MAX_CLOCK_SKEW_SECONDS,
)
from hcc.logging import get_logger
from hcc.models.metrics import (
    TIER_1H,
    TIER_5M,
    TIER_BUCKET_SECONDS,
    TIER_RAW,
    TIERS,
    MetricSample1h,
    MetricSample5m,
    MetricSampleRaw,
    MetricSeries,
    canonical_labels_key,
)
from hcc.models.monitoring import HostModuleHealth
from hcc.monitoring import modules as registry

if TYPE_CHECKING:
    import uuid
    from collections.abc import Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from hcc.agents.protocol import TelemetryEntry

logger = get_logger(__name__)

#: Most series identities one process caches.
#:
#: A reference host produces about thirty series; five hundred hosts is fifteen
#: thousand. Twenty thousand entries of a tuple key and an integer is a few
#: megabytes, which is cheap against a query per sample. Evicted
#: least-recently-used, so a fleet larger than the cache degrades to more
#: upserts rather than to unbounded memory.
MAX_SERIES_CACHE: Final = 20_000

#: Most points one range query will return.
#:
#: Fifty thousand, from ``DATA_RETENTION.md`` §6.4. A request for a year of raw
#: data is not served by sending six million points to a browser; it is served
#: from a coarser tier, or refused with a structured error naming the limit.
MAX_QUERY_POINTS: Final = 50_000

#: Default points a range query targets when the caller does not say.
#:
#: Sized for a chart rather than for the data: a 1,200-pixel chart cannot show
#: more than about 1,200 points, and sending ten times that is bytes the
#: browser spends and discards.
DEFAULT_QUERY_POINTS: Final = 1_000


class MetricsError(Exception):
    """Base class for refusals this service raises."""


class QueryRangeError(MetricsError):
    """A time range that cannot be served.

    Names the limit, because "range too large" without a number is an error a
    caller cannot correct.
    """


@dataclass(frozen=True, slots=True)
class SeriesKey:
    """The identity of one series, as the cache keys it."""

    host_id: uuid.UUID
    module: str
    metric: str
    labels_key: str


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    """What an ingestion did.

    ``rejected`` and ``reasons`` are reported back to the agent. A sample the
    controller refused is something the operator should be able to see, and an
    agent told only "accepted: 11" of twelve would have no way to say which.
    """

    accepted: int
    rejected: int
    reasons: tuple[str, ...] = field(default=())

    @property
    def detail(self) -> str | None:
        """A short operator-facing summary, or ``None`` when nothing failed.

        Bounded by taking only the first few distinct reasons: the string goes
        into a frame with a length limit, and twelve repetitions of "unknown
        metric" say no more than one.
        """
        if not self.reasons:
            return None
        unique = list(dict.fromkeys(self.reasons))[:3]
        summary = "; ".join(unique)
        if len(unique) < len(set(self.reasons)):
            summary += "; …"
        return summary[:120]


@dataclass(frozen=True, slots=True)
class _ModuleOutcome:
    """What happened to one module in one batch.

    One field, and it is deliberately not a boolean: ``error is None`` is a
    success, and the string is what makes an unsuccessful one useful. A
    ``succeeded: bool`` alongside the text would admit the pair
    ``(True, "disk gone")``, which nothing should have to interpret.
    """

    error: str | None


@dataclass(frozen=True, slots=True)
class ModuleHealth:
    """Whether one module's collector is working, as a reader sees it.

    ``failing`` is derived rather than stored, because the stored form —
    "how many failures since the last success" — is the fact, and "is it
    failing" is one reading of it that would go stale in a column.
    """

    module: str
    last_reported_at: datetime | None
    last_success_at: datetime | None
    last_error_at: datetime | None
    last_error: str | None
    consecutive_failures: int

    @property
    def failing(self) -> bool:
        """Whether the most recent collection failed."""
        return self.consecutive_failures > 0


@dataclass(frozen=True, slots=True)
class SeriesDescriptor:
    """A series as a reader sees it."""

    id: int
    module: str
    metric: str
    labels: dict[str, str]
    unit: str
    kind: str
    last_sample_at: datetime | None


@dataclass(frozen=True, slots=True)
class LatestSample:
    """The most recent value of one series."""

    series: SeriesDescriptor
    value: float
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class RangePoint:
    """One point of a historical series.

    ``minimum`` and ``maximum`` are present only for a rolled-up tier, where
    they are the whole reason rollups are usable: a downsampled series rendered
    as a single line of averages is a graph that lies about what happened, so
    the interface draws a min-max band and the API has to supply one.

    ``sample_count`` is what makes a partially-filled bucket visible. An agent
    offline for four of five minutes must not produce a bucket that looks
    authoritative.
    """

    at: datetime
    value: float
    minimum: float | None = None
    maximum: float | None = None
    sample_count: int | None = None


@dataclass(frozen=True, slots=True)
class RangeResult:
    """A historical series, and which tier answered.

    ``tier`` is reported to the caller and shown in the interface. "This is
    hourly data" is information an operator needs in order to interpret a
    flat-looking graph; silently serving a different resolution than expected
    is how people draw wrong conclusions (``DATA_RETENTION.md`` §5.1).
    """

    series: SeriesDescriptor
    tier: str
    points: tuple[RangePoint, ...]
    truncated: bool = False


class MetricsService:
    """Ingests telemetry and answers historical queries.

    Holds a bounded series-identity cache, so one instance is shared across
    requests — and the cache is the only state, which is why it is safe to.
    """

    __slots__ = ("_cache",)

    def __init__(self) -> None:
        # An OrderedDict used as an LRU: moved to the end on a hit, popped
        # from the front when full. A plain dict with no eviction would grow
        # with the number of series ever seen, which on a fleet with churning
        # per-core labels is unbounded.
        self._cache: OrderedDict[SeriesKey, int] = OrderedDict()

    # --- Ingestion --------------------------------------------------------

    async def ingest(
        self,
        db: AsyncSession,
        *,
        host_id: uuid.UUID,
        entries: Sequence[TelemetryEntry],
        enabled_modules: frozenset[str],
        now: datetime | None = None,
    ) -> IngestOutcome:
        """Validate and store a batch of telemetry.

        ``enabled_modules`` is passed in rather than read here, because the
        caller already has the host's effective configuration — and because
        making this method query for it would mean a configuration read per
        batch on the hot path.

        **Does not commit.** The caller does, so that a batch is one unit of
        work: a partially stored batch would leave the agent's acknowledgement
        describing something other than what is in the database.
        """
        moment = now or datetime.now(UTC)
        accepted = 0
        rejected = 0
        reasons: list[str] = []

        # Accumulated across the whole batch and inserted once. A per-sample
        # INSERT at thirty samples a collection is thirty round trips; one
        # multi-row INSERT is one, and the difference is the whole of the
        # ingestion budget at fleet scale.
        rows: list[dict[str, Any]] = []
        touched: dict[int, datetime] = {}
        # One health row per module in this batch, keyed by module so a batch
        # that somehow carries two entries for one module records the later
        # of them rather than racing itself inside a single UPSERT.
        health: dict[str, _ModuleOutcome] = {}

        for entry in entries:
            if entry.module not in enabled_modules:
                # Counted per sample rather than per entry, so `rejected` is
                # comparable with `accepted` — both count samples.
                rejected += len(entry.samples)
                reasons.append(f"module {entry.module!r} is not enabled for this host")
                continue

            try:
                descriptor = registry.descriptor(entry.module)
            except registry.UnknownModuleError:
                rejected += len(entry.samples)
                reasons.append(f"module {entry.module!r} is not in this controller's registry")
                continue

            if (problem := _timestamp_problem(entry.collected_at, moment)) is not None:
                rejected += len(entry.samples)
                reasons.append(problem)
                # Recorded as a failure, with the controller's words rather
                # than the agent's. A host whose clock is four hours out
                # produces an enabled module, a connected agent and no data,
                # which is precisely the symptom this table exists to explain
                # — and the agent cannot report it, because from inside that
                # host nothing is wrong.
                health[entry.module] = _ModuleOutcome(error=problem)
                continue

            # The agent's own account of the collection, recorded whether or
            # not it carried samples. An entry with an error and no samples is
            # the normal shape of a failing collector; an entry with no error
            # and no samples is a rate collector's first interval, which is
            # success. See `HostModuleHealth`.
            health[entry.module] = _ModuleOutcome(error=entry.error)

            for sample in entry.samples:
                metric = descriptor.metric(sample.metric)
                if metric is None:
                    rejected += 1
                    reasons.append(
                        f"{entry.module}/{sample.metric} is not a metric this module declares"
                    )
                    continue
                if (problem := _label_problem(metric, sample.labels)) is not None:
                    rejected += 1
                    reasons.append(problem)
                    continue

                series_id = await self._resolve_series(
                    db,
                    host_id=host_id,
                    module=entry.module,
                    metric=sample.metric,
                    labels=sample.labels,
                    unit=metric.unit.value,
                    kind=metric.kind.value,
                )
                rows.append(
                    {
                        "series_id": series_id,
                        "recorded_at": entry.collected_at,
                        "value": sample.value,
                    }
                )
                previous = touched.get(series_id)
                if previous is None or entry.collected_at > previous:
                    touched[series_id] = entry.collected_at
                accepted += 1

        if rows:
            # ON CONFLICT DO NOTHING is the deduplication: delivery is
            # at-least-once, so a re-sent batch arrives again and must change
            # nothing. Not DO UPDATE — a second write of the same
            # (series, timestamp) with a *different* value would mean two
            # agents claiming the same host, and overwriting would hide it.
            await db.execute(
                pg_insert(MetricSampleRaw)
                .values(rows)
                .on_conflict_do_nothing(index_elements=["series_id", "recorded_at"])
            )
            await self._touch_series(db, touched)

        # After the samples and inside the same transaction. A batch that
        # fails to store must not leave a health row claiming a collection
        # the database never accepted.
        await self._record_health(db, host_id=host_id, outcomes=health, moment=moment)

        if rejected:
            logger.warning(
                "telemetry_partially_rejected",
                host_id=str(host_id),
                accepted=accepted,
                rejected=rejected,
                # The reasons, deduplicated. The raw list can hold one entry
                # per sample, and a log line with five hundred copies of the
                # same sentence is one nobody reads.
                reasons=sorted(set(reasons))[:5],
            )

        return IngestOutcome(accepted=accepted, rejected=rejected, reasons=tuple(reasons))

    async def _resolve_series(
        self,
        db: AsyncSession,
        *,
        host_id: uuid.UUID,
        module: str,
        metric: str,
        labels: Mapping[str, str],
        unit: str,
        kind: str,
    ) -> int:
        """Return the series id, creating the series if it is new.

        The upsert is ``ON CONFLICT … DO UPDATE`` on a no-op column rather than
        ``DO NOTHING``, and that is not a stylistic choice: ``DO NOTHING``
        returns no row when the conflict fires, so a second worker racing to
        create the same series would get nothing back and have to re-select.
        Updating ``host_id`` to its own value makes the statement always
        return the id, in one round trip, whichever worker won.
        """
        key = SeriesKey(
            host_id=host_id,
            module=module,
            metric=metric,
            labels_key=canonical_labels_key(dict(labels)),
        )
        if (cached := self._cache.get(key)) is not None:
            self._cache.move_to_end(key)
            return cached

        statement = (
            pg_insert(MetricSeries)
            .values(
                host_id=host_id,
                module=module,
                metric=metric,
                labels=dict(labels),
                labels_key=key.labels_key,
                unit=unit,
                kind=kind,
            )
            .on_conflict_do_update(
                constraint="uq_metric_series_identity",
                set_={"host_id": host_id},
            )
            .returning(MetricSeries.id)
        )
        series_id = (await db.execute(statement)).scalar_one()

        self._cache[key] = series_id
        if len(self._cache) > MAX_SERIES_CACHE:
            self._cache.popitem(last=False)
        return series_id

    async def _touch_series(self, db: AsyncSession, latest: Mapping[int, datetime]) -> None:
        """Record when each series last received a sample.

        Denormalised deliberately: "which series does this host have, and is
        each still reporting?" is the host page's question, and answering it
        with a ``MAX()`` over a partitioned sample table would scan every
        partition for every series on every page load.

        ``GREATEST`` rather than a plain assignment, so a backfilled batch —
        which arrives with older timestamps — cannot move the column
        backwards and make a live series look stale.
        """
        if not latest:
            return
        await db.execute(
            text(
                """
                UPDATE metric_series AS s
                SET last_sample_at = GREATEST(
                    COALESCE(s.last_sample_at, to_timestamp(0)), v.recorded_at
                )
                FROM (
                    SELECT unnest(cast(:ids AS bigint[])) AS id,
                           unnest(cast(:times AS timestamptz[])) AS recorded_at
                ) AS v
                WHERE s.id = v.id
                """
            ).bindparams(
                ids=list(latest.keys()),
                times=list(latest.values()),
            )
        )

    async def _record_health(
        self,
        db: AsyncSession,
        *,
        host_id: uuid.UUID,
        outcomes: Mapping[str, _ModuleOutcome],
        moment: datetime,
    ) -> None:
        """Record whether each module in this batch collected successfully.

        One statement for the whole batch, because this runs on the ingestion
        hot path: eleven modules reporting on one tick is eleven rows, and
        eleven round trips per batch per host would be the most expensive
        thing a fleet did.

        ## The UPSERT has to read the existing row

        ``consecutive_failures`` is a counter, and "one more than whatever is
        there" cannot be computed before the statement runs — a read followed
        by a write would also be a lost update whenever two batches from the
        same host overlapped. So the increment is expressed in the
        ``DO UPDATE`` clause against ``host_module_health.consecutive_failures``,
        which is the row as it exists, and PostgreSQL serialises the
        conflicting writers for us.

        ## A success does not erase the last error

        ``last_error`` and ``last_error_at`` are carried forward with
        ``COALESCE`` on the excluded values, so only ``consecutive_failures``
        resets. A module that fails and recovers every few minutes is a real
        condition and one that cleared its own evidence would be invisible.
        """
        if not outcomes:
            return

        modules = list(outcomes)
        errors = [outcomes[module].error for module in modules]
        failed = [outcomes[module].error is not None for module in modules]

        await db.execute(
            text(
                """
                INSERT INTO host_module_health AS h (
                    id, host_id, module_id,
                    last_reported_at, last_success_at, last_error_at, last_error,
                    consecutive_failures
                )
                SELECT
                    gen_random_uuid(), :host_id, v.module_id,
                    :moment,
                    CASE WHEN v.failed THEN NULL ELSE :moment END,
                    CASE WHEN v.failed THEN :moment ELSE NULL END,
                    v.error,
                    CASE WHEN v.failed THEN 1 ELSE 0 END
                FROM (
                    SELECT unnest(cast(:modules AS text[])) AS module_id,
                           unnest(cast(:errors AS text[])) AS error,
                           unnest(cast(:failed AS boolean[])) AS failed
                ) AS v
                ON CONFLICT ON CONSTRAINT uq_host_module_health_host_module
                DO UPDATE SET
                    last_reported_at = EXCLUDED.last_reported_at,
                    -- GREATEST, not assignment: two connections for one host
                    -- can interleave, and an older batch landing second must
                    -- not move a success backwards into the past.
                    last_success_at = GREATEST(h.last_success_at, EXCLUDED.last_success_at),
                    last_error_at = COALESCE(EXCLUDED.last_error_at, h.last_error_at),
                    last_error = COALESCE(EXCLUDED.last_error, h.last_error),
                    consecutive_failures = CASE
                        WHEN EXCLUDED.consecutive_failures = 0 THEN 0
                        ELSE h.consecutive_failures + 1
                    END
                """
            ).bindparams(
                host_id=host_id,
                moment=moment,
                modules=modules,
                errors=errors,
                failed=failed,
            )
        )

    # --- Reading ----------------------------------------------------------

    async def health_for_host(
        self, db: AsyncSession, host_id: uuid.UUID
    ) -> dict[str, ModuleHealth]:
        """Every module this host has ever reported on, keyed by module.

        A module absent from the result has never reported — which is not the
        same as healthy and not the same as failing, and the caller has to be
        able to tell. Returning a synthetic "unknown" row for every module in
        the registry would make that distinction the caller's problem to undo.
        """
        rows = (
            (
                await db.execute(
                    select(HostModuleHealth)
                    .where(HostModuleHealth.host_id == host_id)
                    .order_by(HostModuleHealth.module_id)
                )
            )
            .scalars()
            .all()
        )
        return {
            row.module_id: ModuleHealth(
                module=row.module_id,
                last_reported_at=row.last_reported_at,
                last_success_at=row.last_success_at,
                last_error_at=row.last_error_at,
                last_error=row.last_error,
                consecutive_failures=row.consecutive_failures,
            )
            for row in rows
        }

    async def series_for_host(
        self, db: AsyncSession, host_id: uuid.UUID
    ) -> tuple[SeriesDescriptor, ...]:
        """Every series this host has ever produced, in a stable order.

        Includes series that have stopped reporting. Deliberately: a chart for
        a module that was disabled last week should still be offerable, and
        filtering them out here would make "this host has no CPU data" and
        "this host had CPU data until Tuesday" the same answer.
        """
        rows = (
            (
                await db.execute(
                    select(MetricSeries)
                    .where(MetricSeries.host_id == host_id)
                    .order_by(MetricSeries.module, MetricSeries.metric, MetricSeries.labels_key)
                )
            )
            .scalars()
            .all()
        )
        return tuple(_descriptor(row) for row in rows)

    async def latest_for_host(
        self, db: AsyncSession, host_id: uuid.UUID, *, within: timedelta | None = None
    ) -> tuple[LatestSample, ...]:
        """The most recent value of each of a host's series.

        ``within`` bounds how old a sample may be and still be returned. The
        host page passes one, because a value from three days ago is not "the
        current CPU usage" — and returning it without the age would let the
        interface present a stale number as live, which is the failure
        ``PRODUCT_VISION.md`` principle 2 forbids. A series whose newest
        sample is older than the window is simply absent, so the interface
        renders "no data" rather than a number.

        One query with a lateral join rather than a query per series: a host
        with thirty series would otherwise be thirty round trips on every page
        load, and the join lets PostgreSQL use the
        ``(series_id, recorded_at)`` index once per series.
        """
        cutoff = datetime.now(UTC) - within if within is not None else None
        statement = text(
            """
            SELECT s.id, s.module, s.metric, s.labels, s.unit, s.kind, s.last_sample_at,
                   latest.recorded_at, latest.value
            FROM metric_series AS s
            JOIN LATERAL (
                SELECT r.recorded_at, r.value
                FROM metric_samples_raw AS r
                WHERE r.series_id = s.id
                  -- Cast explicitly: a bare `:cutoff IS NULL` leaves asyncpg
                  -- unable to infer the parameter's type and it refuses the
                  -- statement with "could not determine data type of
                  -- parameter $1". The cast is on the comparison rather than
                  -- the column, so the index on recorded_at is still used.
                  AND (
                      cast(:cutoff AS timestamptz) IS NULL
                      OR r.recorded_at >= cast(:cutoff AS timestamptz)
                  )
                ORDER BY r.recorded_at DESC
                LIMIT 1
            ) AS latest ON true
            WHERE s.host_id = :host_id
            ORDER BY s.module, s.metric, s.labels_key
            """
        ).bindparams(host_id=host_id, cutoff=cutoff)

        samples: list[LatestSample] = []
        for row in (await db.execute(statement)).mappings():
            samples.append(
                LatestSample(
                    series=SeriesDescriptor(
                        id=row["id"],
                        module=row["module"],
                        metric=row["metric"],
                        labels=dict(row["labels"] or {}),
                        unit=row["unit"],
                        kind=row["kind"],
                        last_sample_at=row["last_sample_at"],
                    ),
                    value=row["value"],
                    recorded_at=row["recorded_at"],
                )
            )
        return tuple(samples)

    async def range_for_series(
        self,
        db: AsyncSession,
        *,
        series_id: int,
        host_id: uuid.UUID,
        start: datetime,
        end: datetime,
        max_points: int = DEFAULT_QUERY_POINTS,
        tier: str | None = None,
    ) -> RangeResult | None:
        """One series over a time range, at a resolution the range allows.

        Returns ``None`` when the series does not exist **or belongs to another
        host**. The ``host_id`` parameter is not redundant: a series id is a
        sequential integer, so without it an operator who can read one host's
        charts could read every host's by incrementing a number. The check
        lives here rather than only in the API because this is the method that
        would otherwise be the hole.

        ``tier`` overrides the automatic choice. Offered because §20 asks for a
        resolution parameter and because an operator comparing two charts
        needs them at the same resolution — but the default is to choose, and
        the choice is the one a caller should normally take. A caller that
        names a tier can ask for more points than the budget allows, which is
        the only way :attr:`RangeResult.truncated` becomes true: the automatic
        choice guarantees the tier fits.

        Raises:
            QueryRangeError: the range is backwards, the point budget is
                outside what this controller will serve, or the named tier is
                not one that exists.
        """
        if end <= start:
            raise QueryRangeError("the end of the range must be after its start")
        if max_points < 1 or max_points > MAX_QUERY_POINTS:
            raise QueryRangeError(
                f"max_points must be between 1 and {MAX_QUERY_POINTS}, got {max_points}"
            )
        if tier is not None and tier not in TIERS:
            raise QueryRangeError(
                f"unknown resolution {tier!r}; expected one of: {', '.join(TIERS)}"
            )

        series = (
            await db.execute(
                select(MetricSeries).where(
                    MetricSeries.id == series_id, MetricSeries.host_id == host_id
                )
            )
        ).scalar_one_or_none()
        if series is None:
            return None

        chosen = tier or choose_tier(start, end, max_points)
        descriptor = _descriptor(series)

        # One more than the budget, so a full page is distinguishable from an
        # overflowing one without a second COUNT query.
        if chosen == TIER_RAW:
            points = await self._raw_points(db, series_id, start, end, max_points + 1)
        else:
            points = await self._rollup_points(db, chosen, series_id, start, end, max_points + 1)

        truncated = len(points) > max_points
        return RangeResult(
            series=descriptor,
            tier=chosen,
            points=tuple(points[:max_points]),
            truncated=truncated,
        )

    async def _raw_points(
        self, db: AsyncSession, series_id: int, start: datetime, end: datetime, limit: int
    ) -> list[RangePoint]:
        rows = (
            await db.execute(
                select(MetricSampleRaw.recorded_at, MetricSampleRaw.value)
                .where(
                    MetricSampleRaw.series_id == series_id,
                    MetricSampleRaw.recorded_at >= start,
                    MetricSampleRaw.recorded_at < end,
                )
                .order_by(MetricSampleRaw.recorded_at)
                .limit(limit)
            )
        ).all()
        return [RangePoint(at=row[0], value=row[1]) for row in rows]

    async def _rollup_points(
        self,
        db: AsyncSession,
        tier: str,
        series_id: int,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[RangePoint]:
        model = MetricSample5m if tier == TIER_5M else MetricSample1h
        rows = (
            await db.execute(
                select(
                    model.bucket_start,
                    model.avg_value,
                    model.min_value,
                    model.max_value,
                    model.sample_count,
                )
                .where(
                    model.series_id == series_id,
                    model.bucket_start >= start,
                    model.bucket_start < end,
                )
                .order_by(model.bucket_start)
                .limit(limit)
            )
        ).all()
        return [
            RangePoint(
                at=row[0],
                value=row[1],
                minimum=row[2],
                maximum=row[3],
                sample_count=row[4],
            )
            for row in rows
        ]

    async def sample_count(self, db: AsyncSession, host_id: uuid.UUID) -> int:
        """How many raw samples a host has stored.

        For the host page's "is anything actually arriving?" line.

        ## Two queries, deliberately

        The obvious single query joins ``metric_series`` and filters on
        ``host_id``, and it is **O(every sample in the installation)**: the
        filter is on the joined table, so the planner has no way to prune
        partitions or use the ``(series_id, recorded_at)`` index, and it
        sequentially scans every partition whatever host is asked about.
        Measured, it cost 37 ms for a host with a hundred samples — the same
        as for a host with half a million, because the work is proportional to
        everybody else's data.

        Resolving the series ids first and counting by them is 0.044 ms for
        that same host, and scales with the host's own data rather than the
        installation's. ``scripts/measure-metrics.py`` is where the numbers
        come from.

        The extra round trip is worth it twice over: a host with no series at
        all returns zero without touching the sample tables, and that is the
        newly-enrolled host — previously the most expensive case and the one
        most likely to be looked at.
        """
        series_ids = (
            (await db.execute(select(MetricSeries.id).where(MetricSeries.host_id == host_id)))
            .scalars()
            .all()
        )
        if not series_ids:
            return 0
        return (
            await db.execute(
                select(func.count())
                .select_from(MetricSampleRaw)
                .where(MetricSampleRaw.series_id.in_(series_ids))
            )
        ).scalar_one()


def choose_tier(start: datetime, end: datetime, max_points: int) -> str:
    """Pick the finest tier that can answer a range within the point budget.

    The resolution is chosen by the *query*, not by the caller
    (``DATA_RETENTION.md`` §5.1). A caller asking for a year of raw data is
    asking for six million points, and the right answer is hourly data rather
    than either a refusal or six million points — so the tier is derived from
    the window and the budget rather than being a parameter.

    Walks finest to coarsest and takes the first that fits, assuming the
    fastest plausible sampling rate for the raw tier. One second is
    deliberately pessimistic: at the ten-second default the raw tier would fit
    a ten-times-larger window, but choosing a tier on an assumption about how
    often a module *happens* to be configured would mean the same request
    returned a different resolution on two hosts.
    """
    window = (end - start).total_seconds()

    # Raw, assuming the floor of one sample a second.
    if window <= max_points:
        return TIER_RAW
    if window / TIER_BUCKET_SECONDS[TIER_5M] <= max_points:
        return TIER_5M
    return TIER_1H


def _timestamp_problem(collected_at: datetime, now: datetime) -> str | None:
    """Why a collection timestamp cannot be stored, or ``None``.

    A timestamp is refused rather than rewritten to the controller's clock. An
    agent whose clock is an hour fast is reporting measurements that have not
    happened; replacing its timestamp with the controller's would store a
    plausible-looking row whose time is the controller's invention, and the
    operator would have no way to discover that the host's clock is wrong.
    """
    if collected_at.tzinfo is None:
        # Should not arise: the protocol model parses an aware datetime.
        # Refused rather than assumed to be UTC, because assuming is how a
        # host two zones away gets its data filed two hours out.
        return "a collection timestamp must carry a time zone"
    skew = (collected_at - now).total_seconds()
    if skew > MAX_CLOCK_SKEW_SECONDS:
        return (
            f"a collection timestamp is {int(skew)}s in the future; "
            f"this host's clock is wrong by more than {MAX_CLOCK_SKEW_SECONDS}s"
        )
    if -skew > MAX_BACKFILL_SECONDS:
        return (
            f"a collection timestamp is {int(-skew)}s old, beyond the "
            f"{MAX_BACKFILL_SECONDS}s backfill window"
        )
    return None


def _label_problem(metric: registry.MetricDescriptor, labels: Mapping[str, str]) -> str | None:
    """Why a sample's labels cannot be stored, or ``None``.

    Exactly the declared keys, no more and no fewer. An extra key would create
    a series the registry does not describe — so nothing could render its
    unit — and a missing one would collapse several series into one: three
    load-average windows arriving without their ``window`` label would all
    become the same series and overwrite each other.
    """
    declared = set(metric.label_keys)
    present = set(labels)
    if present == declared:
        return None
    if extra := sorted(present - declared):
        return f"{metric.id}: unexpected label(s) {', '.join(extra)}"
    missing = sorted(declared - present)
    return f"{metric.id}: missing label(s) {', '.join(missing)}"


def _descriptor(series: MetricSeries) -> SeriesDescriptor:
    return SeriesDescriptor(
        id=series.id,
        module=series.module,
        metric=series.metric,
        labels=dict(series.labels or {}),
        unit=series.unit,
        kind=series.kind,
        last_sample_at=series.last_sample_at,
    )
