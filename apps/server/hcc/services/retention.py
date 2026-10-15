"""Partition maintenance, rollups and retention.

Phase 3, implementing ``docs/DATA_RETENTION.md`` §3 and §4. One periodic job
with three responsibilities, in this order and for this reason:

1. **Create the partitions the data needs**, behind as well as ahead. A row
   whose timestamp falls outside every partition lands in the DEFAULT one,
   which is a safety net and not a plan: it is never catalogued and therefore
   never dropped, so whatever reaches it is kept for ever. Staying ahead
   handles the clock moving forward; staying *behind* handles backfill, which
   is the case that was missed and measured — see :data:`PARTITIONS_BEHIND`.
2. **Roll up sealed buckets.** Aggregate raw samples into the five-minute and
   hourly tiers, preserving min, max, first, last and count.
3. **Drop expired partitions**, and only then.

The order is the part that matters: **raw data is never dropped before the
rollups that depend on it have been produced.** §19 asks for that explicitly,
and it is enforced rather than hoped for — see
:func:`rollup_watermark`.

## Idempotent, bounded, restart-safe, and safe to run twice at once

Each of those is a specific mechanism rather than an aspiration:

* **Idempotent.** A rollup is an ``INSERT … ON CONFLICT DO UPDATE`` computed
  entirely from the raw rows in the bucket, so recomputing it produces the
  same answer. A job interrupted halfway and re-run cannot double-count.
* **Bounded.** Every step has a limit on how much it will do in one run. An
  unbounded rollup over a database that has been collecting for a month while
  the job was broken is a transaction that holds locks for an hour.
* **Restart-safe.** There is no in-memory progress. What has been rolled up is
  derivable from the rollup tables themselves, so a process that dies
  mid-run resumes by re-deriving rather than by remembering.
* **Safe under concurrency.** One installation-wide advisory lock, so two
  controller processes — or a manual run alongside the scheduled one — do not
  both create the same partition and fail.

## A bucket is not rolled up while samples could still arrive for it

An agent reconnecting after an outage backfills with its original timestamps,
so the newest buckets are still changing. The job therefore waits a
**lateness grace** before sealing a bucket (``DATA_RETENTION.md`` §3.2).

Late data arriving after a bucket is sealed is not dropped: the rollup is an
upsert, so the next run that touches that bucket recomputes it. The cost is
that a sealed bucket can change; the alternative — dropping it — would make
the rollup silently disagree with the raw data it came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, cast

from sqlalchemy import select, text

from hcc.logging import get_logger
from hcc.models.metrics import (
    TIER_1H,
    TIER_5M,
    TIER_BUCKET_SECONDS,
    TIER_PARTITION_DAYS,
    TIER_RAW,
    TIER_TABLES,
    TIERS,
    MetricPartition,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import TextClause

logger = get_logger(__name__)

#: Advisory lock class for metric maintenance.
#:
#: Distinct from the per-host class (``hcc.services.hosts.HOST_LOCK_CLASS``),
#: because this is installation-wide: two processes must not both try to
#: create the same partition, and serialising that against a host's
#: configuration writes would be pointless contention.
#:
#: 0x4D455452 is "METR" in ASCII, chosen so a ``pg_locks`` row is
#: recognisable rather than being an arbitrary number somebody has to look up.
METRIC_LOCK_CLASS: Final = 0x4D455452
METRIC_LOCK_KEY: Final = 1

#: How many periods ahead of now the job keeps partitions.
#:
#: Three. One would mean a job that missed a single run left the next day's
#: inserts landing in the DEFAULT partition; three survives a job that has been
#: broken for two days, which is longer than anyone takes to notice.
PARTITIONS_AHEAD: Final = 3

#: How many periods *behind* now each tier keeps partitions.
#:
#: Not symmetry with :data:`PARTITIONS_AHEAD` — this exists because of
#: backfill, and the numbers come from how far back an agent is allowed to
#: reach. ``protocol.MAX_BACKFILL_SECONDS`` is seven days, so an agent
#: reconnecting after a week-long outage delivers samples timestamped up to
#: seven days ago, and a partition has to exist for each of them.
#:
#: Without this, those samples land in the DEFAULT partition — which is a
#: safety net, is never catalogued in ``metric_partitions``, and is therefore
#: **never dropped by retention**. Backfilled data would accumulate there for
#: ever, invisibly, which is §19's "the first symptom is a disk filling weeks
#: later" arriving by a different route. Measured on a benchmark that wrote six
#: hours of history backwards from now: 189,120 of 259,200 rows landed in
#: DEFAULT, because only the current day's partition existed.
#:
#: The counts differ because the partition widths do. Raw is daily, so seven
#: days needs seven. The five-minute tier is weekly and the hourly tier
#: monthly, so one each already spans more than the backfill window.
PARTITIONS_BEHIND: Final[dict[str, int]] = {
    TIER_RAW: 7,
    TIER_5M: 1,
    TIER_1H: 1,
}

#: How long a bucket must be closed before it is rolled up.
#:
#: Ten minutes, from ``DATA_RETENTION.md`` §3.2. Long enough for an agent that
#: reconnected after a brief outage to have delivered its backlog, short
#: enough that the rollup tiers are usable for "the last few hours".
DEFAULT_LATENESS_MINUTES: Final = 10

#: Most buckets one rollup run will compute, per tier.
#:
#: Sized so a job that has not run for a day catches up in one pass at the
#: five-minute tier (288 buckets a day per series) without holding a
#: transaction open indefinitely. A job further behind than this catches up
#: over several runs, which is the right shape: progress every run rather than
#: one enormous transaction.
MAX_BUCKETS_PER_RUN: Final = 5_000

#: How many bucket widths a tier may be behind before it is worth a warning.
#:
#: Three, so the five-minute tier tolerates fifteen minutes. Steady state is
#: zero or one — the pass runs every five minutes and seals one bucket — and
#: three leaves room for a slow pass and a restart without producing a line
#: an operator learns to ignore.
MAX_ACCEPTABLE_LAG_BUCKETS: Final = 3

#: Most partitions one retention run will drop.
#:
#: Each drop is a ``DROP TABLE``, which is fast but takes an exclusive lock.
#: Ten a run means a database that has been accumulating for a month clears in
#: three runs rather than in one long lock.
MAX_DROPS_PER_RUN: Final = 10

#: Default retention per tier, in days.
#:
#: From ``DATA_RETENTION.md`` §2's "CPU, memory, load" row, which is every
#: category this phase collects. Per-category retention is deferred — see the
#: module docstring in ``docs/DATA_RETENTION.md`` and the deferral note in
#: ``docs/PERFORMANCE.md`` — because it needs a configuration surface of its
#: own and the categories in this phase all want the same numbers.
DEFAULT_RETENTION_DAYS: Final[dict[str, int]] = {
    TIER_RAW: 7,
    TIER_5M: 90,
    TIER_1H: 730,
}


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """How long each tier is kept.

    A value object rather than settings read inside the service, so a test can
    drive a one-day retention without rebuilding the application — and so the
    numbers appear in one place rather than being looked up three times.
    """

    raw_days: int = DEFAULT_RETENTION_DAYS[TIER_RAW]
    five_minute_days: int = DEFAULT_RETENTION_DAYS[TIER_5M]
    hourly_days: int = DEFAULT_RETENTION_DAYS[TIER_1H]
    lateness_minutes: int = DEFAULT_LATENESS_MINUTES

    def days_for(self, tier: str) -> int:
        """Retention for one tier, in days."""
        return {
            TIER_RAW: self.raw_days,
            TIER_5M: self.five_minute_days,
            TIER_1H: self.hourly_days,
        }[tier]


@dataclass(frozen=True, slots=True)
class MaintenanceOutcome:
    """What one maintenance run did.

    Returned rather than only logged, so a test can assert it and an operator
    endpoint could report it. Each count is a different kind of work, because
    "the job ran" is not the useful answer — "it created no partitions and
    dropped none and rolled up nothing" and "it rolled up four thousand
    buckets" are the same sentence otherwise.
    """

    partitions_created: int = 0
    partitions_dropped: int = 0
    buckets_rolled_up: dict[str, int] | None = None
    #: Partitions that *should* exist and do not, by tier. Non-empty means the
    #: job is not keeping up and inserts are landing in a DEFAULT partition.
    gaps: dict[str, int] | None = None
    #: Rows sitting in each tier's DEFAULT partition, by tier.
    #:
    #: Non-empty is a problem that does not resolve itself: the DEFAULT
    #: partition is never catalogued and never dropped, so whatever reaches it
    #: is kept for ever. Reported rather than only logged, so an operator
    #: endpoint could surface it.
    default_rows: dict[str, int] | None = None
    #: Tiers whose rollup wrote exactly :data:`MAX_BUCKETS_PER_RUN` rows.
    #:
    #: The run did not finish that tier. Harmless once — a job catching up
    #: after an outage takes several passes by design — and a standing
    #: condition if it happens on consecutive passes: the tier is then
    #: falling further behind every run and will never catch up on its own.
    #:
    #: Reported as well as logged, because "the rollups are permanently
    #: behind" is invisible from the data. The tiers still answer queries;
    #: they just answer them about last week.
    capped_tiers: tuple[str, ...] = ()
    #: How far behind each rollup tier is, in seconds, after this pass.
    #:
    #: The newest bucket the tier holds against the newest it should hold.
    #: Derived from the database rather than remembered, which is what keeps
    #: this service stateless and therefore restart-safe.
    #:
    #: Zero in steady state. Large and *shrinking* after an outage, which is
    #: the job working. Large and not shrinking means the installation has
    #: more series than :data:`MAX_BUCKETS_PER_RUN` sustains, and nothing in
    #: the data would show it — the tiers keep answering queries, about last
    #: week.
    rollup_lag_seconds: dict[str, float] = field(default_factory=dict)
    #: Set when the lock was held by another process, so the run did nothing.
    #: Not an error: the other run is doing the work.
    skipped: bool = False


class RetentionService:
    """Creates partitions, produces rollups and expires old data.

    Stateless. Everything it needs to know about progress is derivable from
    the database, which is what makes it restart-safe.
    """

    __slots__ = ("_policy",)

    def __init__(self, policy: RetentionPolicy | None = None) -> None:
        self._policy = policy or RetentionPolicy()

    @property
    def policy(self) -> RetentionPolicy:
        """The retention this service enforces."""
        return self._policy

    async def run(self, db: AsyncSession, *, now: datetime | None = None) -> MaintenanceOutcome:
        """Do one maintenance pass.

        **Commits.** Unlike most services here, which leave the transaction to
        the caller: this one is driven by a background task with no request to
        belong to, and the three steps are deliberately separate units of work
        — a rollup that succeeded should not be rolled back because a later
        partition drop failed.

        Returns a :class:`MaintenanceOutcome` with ``skipped`` set when another
        process holds the lock.
        """
        moment = now or datetime.now(UTC)

        if not await self._try_lock(db):
            logger.info("metric_maintenance_skipped", reason="another run holds the lock")
            return MaintenanceOutcome(skipped=True)

        created = await self._ensure_partitions(db, moment)
        await db.commit()

        rolled = await self._rollup_all(db, moment)
        await db.commit()

        dropped = await self._enforce_retention(db, moment)
        await db.commit()

        gaps = await self._partition_gaps(db, moment)
        default_rows = await self._default_partition_rows(db)

        # A tier that wrote exactly the bound did not finish. One pass of
        # that is how catching up works; *every* pass of it is a job that
        # will never catch up, and the distinction is the difference between
        # a slope and a cliff.
        #
        # The cliff is reachable: the five-minute tier writes one row per
        # series per five-minute bucket, and the job runs every five
        # minutes, so a steady-state pass has one bucket per series to
        # write. The bound is therefore a bound on **series in the
        # installation** — 5,000 of them — and the label-keyed modules make
        # that far fewer hosts than it sounds: `scripts/measure-metrics.py
        # --profile full` measures 164 series on a well-populated host,
        # which is thirty hosts. Measured, and recorded in
        # `docs/PERFORMANCE.md` §4.
        capped = tuple(
            sorted(tier for tier, count in rolled.items() if count >= MAX_BUCKETS_PER_RUN)
        )
        lag = await self._rollup_lag(db, moment)
        lagging = {
            tier: seconds
            for tier, seconds in lag.items()
            if seconds > MAX_ACCEPTABLE_LAG_BUCKETS * TIER_BUCKET_SECONDS[tier]
        }
        outcome = MaintenanceOutcome(
            partitions_created=created,
            partitions_dropped=dropped,
            buckets_rolled_up=rolled,
            gaps=gaps,
            default_rows=default_rows,
            capped_tiers=capped,
            rollup_lag_seconds=lag,
        )
        if lagging:
            # Warned on the *lag* rather than on the bound being hit, because
            # the bound being hit once is how catching up works and says
            # nothing on its own. Lag is derived from the database — the
            # newest bucket each tier holds, against the newest it should —
            # which keeps this service stateless and therefore restart-safe,
            # the property its own docstring claims.
            logger.warning(
                "metric_rollup_behind",
                lag_seconds={tier: round(seconds) for tier, seconds in lagging.items()},
                capped_tiers=list(capped),
                bound=MAX_BUCKETS_PER_RUN,
                detail=(
                    "A rollup tier is further behind than one pass should leave "
                    "it. After an outage this shrinks every run and clears "
                    "itself. If it does not, this installation has more series "
                    "than the per-run bound sustains — the five-minute tier "
                    "writes one row per series per pass, so the bound is a bound "
                    "on series, and docs/PERFORMANCE.md section 4 has the "
                    "measured ceiling."
                ),
            )
        if default_rows:
            # Warned rather than counted quietly: nothing drops these, so the
            # number only grows, and the remedy is a decision an operator has
            # to make.
            logger.warning(
                "metric_default_partition_in_use",
                rows=default_rows,
                detail=(
                    "Samples landed outside every partition window and are in a "
                    "DEFAULT partition, which retention never drops. They were "
                    "timestamped outside the range the job keeps partitions for."
                ),
            )
        logger.info(
            "metric_maintenance_complete",
            partitions_created=created,
            partitions_dropped=dropped,
            buckets_rolled_up=rolled,
            gaps=gaps,
            default_rows=default_rows,
        )
        return outcome

    async def _try_lock(self, db: AsyncSession) -> bool:
        """Take the installation-wide maintenance lock, or report it held.

        ``pg_try_advisory_lock`` rather than the blocking form, and *session*
        scoped rather than transaction scoped. The blocking form would queue a
        second run behind the first and then do the same work again; trying
        means the second run returns at once. Session scope is needed because
        this method commits between steps, and a transaction-scoped lock would
        be released by the first commit.
        """
        held: bool = (
            await db.execute(
                text("SELECT pg_try_advisory_lock(:class, :key)").bindparams(
                    **{"class": METRIC_LOCK_CLASS, "key": METRIC_LOCK_KEY}
                )
            )
        ).scalar_one()
        return held

    async def release_lock(self, db: AsyncSession) -> None:
        """Release the maintenance lock.

        Explicit because the lock is session-scoped: it is released when the
        connection closes, which for a pooled connection is not when the run
        ends. A run that did not release would hold it until the pool recycled
        the connection, and every later run would skip.
        """
        await db.execute(
            text("SELECT pg_advisory_unlock(:class, :key)").bindparams(
                **{"class": METRIC_LOCK_CLASS, "key": METRIC_LOCK_KEY}
            )
        )

    # --- Partitions -------------------------------------------------------

    async def _ensure_partitions(self, db: AsyncSession, now: datetime) -> int:
        """Create the partitions each tier needs: behind, now, and ahead.

        Creates nothing that exists. The catalogue is consulted first, and the
        ``CREATE TABLE`` is guarded by its own uniqueness — so a race with
        another process is a caught error rather than a failed run.

        ## A window already past its retention is not created

        Otherwise the job would recreate the partition retention dropped on the
        previous run, drop it again on the next, and churn for ever. Worse than
        pointless: a window whose raw data has already been rolled up and
        expired would start accepting late samples the rollup pass will not
        revisit — ``_rollup`` starts from the tier watermark — so the data
        would sit there unaggregated until retention removed it again, and the
        chart would disagree with the raw table in the meantime.

        So the backward reach stops at the retention cutoff. Samples older than
        that are refused by the ingestion path's backfill bound or land in
        DEFAULT, and :meth:`_default_partition_rows` is what makes the latter
        visible.
        """
        created = 0
        for tier in TIERS:
            cutoff = now - timedelta(days=self._policy.days_for(tier))
            for window in _windows(tier, now, PARTITIONS_AHEAD, PARTITIONS_BEHIND[tier]):
                if window.end <= cutoff:
                    continue
                if await self._partition_exists(db, tier, window.start):
                    continue
                if await self._create_partition(db, tier, window):
                    created += 1
        return created

    async def _default_partition_rows(self, db: AsyncSession) -> dict[str, int]:
        """How many rows are sitting in each tier's DEFAULT partition.

        The DEFAULT partition is a safety net rather than a plan: a row whose
        timestamp falls outside every window lands there instead of failing the
        insert, which is the right trade — losing a measurement is worse than
        storing it awkwardly.

        But it is **never catalogued and never dropped**, so anything that
        accumulates there is kept for ever. Reporting the count is what turns
        that from an invisible leak into a number an operator can act on, and
        it is cheap: the count is per-partition and these should be empty.
        """
        counts: dict[str, int] = {}
        for tier, table in TIER_TABLES.items():
            # The identifier is derived from TIER_TABLES, through the same
            # guard every other interpolation in this module uses.
            name = f"{_metric_table(table)}_default"
            rows: int = (
                await db.execute(text(f'SELECT count(*) FROM "{name}"'))  # noqa: S608
            ).scalar_one()
            if rows:
                counts[tier] = rows
        return counts

    async def _partition_exists(self, db: AsyncSession, tier: str, start: datetime) -> bool:
        found = (
            await db.execute(
                select(MetricPartition.id).where(
                    MetricPartition.tier == tier,
                    MetricPartition.range_start == start,
                    MetricPartition.dropped_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        return found is not None

    async def _create_partition(self, db: AsyncSession, tier: str, window: _Window) -> bool:
        """Create one partition and catalogue it. Returns whether it was new."""
        parent = TIER_TABLES[tier]
        name = f"{parent}_{window.suffix}"
        try:
            # Quoted identifiers built from a derived name, not from input:
            # `parent` comes from TIER_TABLES and `suffix` from a formatted
            # timestamp, so there is no caller-supplied string in this DDL.
            #
            # The bounds are literals rather than bind parameters, which is not
            # a style choice: a partition bound is part of the table
            # definition, so PostgreSQL parses it at DDL time and accepts no
            # parameters there. Binding them makes the server report that it
            # "expects 0 arguments for this query" — and the partition is never
            # created. `_timestamp_literal` is what keeps that safe.
            await db.execute(
                text(
                    f'CREATE TABLE IF NOT EXISTS "{name}" PARTITION OF "{parent}" '
                    f"FOR VALUES FROM ({_timestamp_literal(window.start)}) "
                    f"TO ({_timestamp_literal(window.end)})"
                )
            )
            db.add(
                MetricPartition(
                    tier=tier,
                    table_name=name,
                    range_start=window.start,
                    range_end=window.end,
                )
            )
            await db.flush()
        except Exception as exc:  # noqa: BLE001 - see below
            # Deliberately broad. Another process creating the same partition
            # between the check and the create surfaces as any of several
            # driver errors — a unique violation on the catalogue, a duplicate
            # table, a serialisation failure — and enumerating them would be a
            # list that goes stale with the driver. The outcome wanted is the
            # same for all of them: the partition exists, which is what this
            # method was for.
            #
            # Not an error, then, but the transaction is unusable, so it is
            # rolled back and the run continues with the next window.
            await db.rollback()
            # The cause is logged, not just the conclusion. The handler is
            # broad enough to swallow a genuine programming error — a missing
            # column, a bad constraint — and "already created" would then be
            # the only trace of it, on a job that silently created nothing for
            # ever. With the cause recorded, the benign race and the bug look
            # different in the log.
            logger.info(
                "metric_partition_already_created",
                tier=tier,
                table_name=name,
                cause=type(exc).__name__,
                detail=str(exc).splitlines()[0] if str(exc) else "",
            )
            return False

        logger.info(
            "metric_partition_created",
            tier=tier,
            table_name=name,
            range_start=window.start.isoformat(),
            range_end=window.end.isoformat(),
        )
        return True

    async def _partition_gaps(self, db: AsyncSession, now: datetime) -> dict[str, int]:
        """How many partitions each tier is missing.

        The thing worth monitoring: a missing *future* partition means inserts
        land in the DEFAULT one, which the schema allows precisely so that a
        job falling behind is recoverable rather than a data-loss event. A
        non-zero gap is how an operator finds out.
        """
        gaps: dict[str, int] = {}
        for tier in TIERS:
            cutoff = now - timedelta(days=self._policy.days_for(tier))
            missing = 0
            # The same windows `_ensure_partitions` creates, including the
            # retention skip — otherwise this reports a gap for a window
            # nothing is ever going to create, and a permanent non-zero gap is
            # a signal nobody looks at twice.
            for window in _windows(tier, now, PARTITIONS_AHEAD, PARTITIONS_BEHIND[tier]):
                if window.end <= cutoff:
                    continue
                if not await self._partition_exists(db, tier, window.start):
                    missing += 1
            if missing:
                gaps[tier] = missing
        return gaps

    # --- Rollups ----------------------------------------------------------

    async def _rollup_all(self, db: AsyncSession, now: datetime) -> dict[str, int]:
        """Produce both rollup tiers from the raw samples."""
        return {
            TIER_5M: await self._rollup(db, TIER_5M, now),
            TIER_1H: await self._rollup(db, TIER_1H, now),
        }

    async def _rollup(self, db: AsyncSession, tier: str, now: datetime) -> int:
        """Aggregate sealed raw buckets into one tier. Returns rows written.

        One statement, deliberately. Pulling the raw samples into Python to
        aggregate them would move megabytes across the wire to compute six
        numbers PostgreSQL can compute in place — and would make the
        idempotence depend on application code rather than on an upsert.

        The statement itself is :data:`_ROLLUP_SQL`, which explains why the
        table name is substituted into a template rather than formatted into an
        f-string, and why ``first_value`` is taken by time order.
        """
        width = TIER_BUCKET_SECONDS[tier]
        table = TIER_TABLES[tier]
        sealed_before = now - timedelta(minutes=self._policy.lateness_minutes)
        # Truncated to the bucket width, so a partially-elapsed bucket is never
        # sealed — the grace period alone would not prevent that.
        sealed_before = _floor_to(sealed_before, width)

        # Only buckets newer than what has already been rolled up, so a run
        # does not recompute the whole history every time. Derived from the
        # rollup table rather than remembered, which is what makes this
        # restart-safe.
        watermark = await self._tier_watermark(db, tier)
        start_from = watermark or await self._oldest_raw(db)
        if start_from is None:
            return 0
        start_from = _floor_to(start_from, width)
        if start_from >= sealed_before:
            return 0

        result = await db.execute(
            _rollup_statement(table).bindparams(
                width=width,
                start_from=start_from,
                sealed_before=sealed_before,
                limit=MAX_BUCKETS_PER_RUN,
            )
        )
        # `rowcount` is a CursorResult attribute; AsyncSession.execute is
        # typed as returning Result. Same cast as hcc/services/hosts.py.
        written = cast("CursorResult[Any]", result).rowcount or 0
        if written:
            logger.info(
                "metric_rollup_written",
                tier=tier,
                rows=written,
                start_from=start_from.isoformat(),
                sealed_before=sealed_before.isoformat(),
            )
        return written

    async def _rollup_lag(self, db: AsyncSession, now: datetime) -> dict[str, float]:
        """How far behind each rollup tier is, in seconds.

        The gap between the newest bucket a tier holds and the newest it
        should: everything older than the lateness grace, floored to the
        bucket width. Zero when caught up, and never negative — a tier with
        no rows at all has nothing to be behind, and reporting the age of the
        installation would make a fresh controller look broken.
        """
        newest_raw: datetime | None = (
            await db.execute(text("SELECT max(recorded_at) FROM metric_samples_raw"))
        ).scalar_one_or_none()

        lag: dict[str, float] = {}
        for tier, width in TIER_BUCKET_SECONDS.items():
            if tier == TIER_RAW:
                continue
            if newest_raw is None:
                # Nothing to roll up. A fresh controller is not infinitely
                # behind, and reporting the age of the epoch would make
                # every new installation look broken.
                lag[tier] = 0.0
                continue
            sealed_before = _floor_to(now - timedelta(minutes=self._policy.lateness_minutes), width)
            # The newest bucket that *should* exist: the newest raw sample's
            # bucket, unless the lateness grace has not released it yet.
            #
            # Measured against the data rather than against the clock. A
            # series with gaps has no bucket for a window nothing sampled,
            # so comparing the watermark with `sealed_before` would report a
            # caught-up tier as five minutes behind for ever — which is what
            # the first version of this did.
            expected = min(_floor_to(newest_raw, width), sealed_before - timedelta(seconds=width))
            watermark = await self._tier_watermark(db, tier)
            if watermark is None:
                oldest = await self._oldest_raw(db)
                lag[tier] = (
                    max(0.0, (expected - _floor_to(oldest, width)).total_seconds())
                    if oldest
                    else 0.0
                )
                continue
            lag[tier] = max(0.0, (expected - watermark).total_seconds())
        return lag

    async def _tier_watermark(self, db: AsyncSession, tier: str) -> datetime | None:
        """The newest bucket already rolled up for a tier, or ``None``."""
        table = TIER_TABLES[tier]
        return (await db.execute(_max_bucket_statement(table))).scalar_one_or_none()

    async def _oldest_raw(self, db: AsyncSession) -> datetime | None:
        """The oldest raw sample, or ``None`` when there are none."""
        oldest: datetime | None = (
            await db.execute(text("SELECT min(recorded_at) FROM metric_samples_raw"))
        ).scalar_one_or_none()
        return oldest

    # --- Retention --------------------------------------------------------

    async def _enforce_retention(self, db: AsyncSession, now: datetime) -> int:
        """Drop partitions past their tier's retention. Returns how many.

        **Raw partitions are held back until their data has been rolled up.**
        §19 asks for exactly this, and without it a job whose rollup step was
        broken would quietly delete the raw data the rollups were going to be
        made from — the one failure in this system that cannot be undone.
        """
        dropped = 0
        watermark = await rollup_watermark(db)

        for tier in TIERS:
            cutoff = now - timedelta(days=self._policy.days_for(tier))
            if tier == TIER_RAW and watermark is not None:
                # Whichever is older. A rollup watermark behind the retention
                # cutoff means the rollups have not caught up, and the raw
                # data has to wait.
                cutoff = min(cutoff, watermark)
            elif tier == TIER_RAW:
                # No rollups at all. Nothing is dropped, which is the safe
                # direction: an installation whose rollup step has never run
                # keeps its raw data and grows, which is visible, rather than
                # losing it, which is not.
                logger.warning(
                    "metric_retention_held",
                    tier=tier,
                    reason="no rollups exist yet, so raw data is not expired",
                )
                continue

            expired = (
                (
                    await db.execute(
                        select(MetricPartition)
                        .where(
                            MetricPartition.tier == tier,
                            MetricPartition.dropped_at.is_(None),
                            MetricPartition.range_end <= cutoff,
                        )
                        .order_by(MetricPartition.range_start)
                        .limit(MAX_DROPS_PER_RUN - dropped)
                    )
                )
                .scalars()
                .all()
            )
            for partition in expired:
                await db.execute(text(f'DROP TABLE IF EXISTS "{partition.table_name}"'))
                # Marked rather than deleted, so "this window existed and was
                # expired at this time" stays answerable — otherwise an
                # operator asking why last month's data is gone has only an
                # absence to look at.
                partition.dropped_at = now
                dropped += 1
                logger.info(
                    "metric_partition_dropped",
                    tier=tier,
                    table_name=partition.table_name,
                    range_end=partition.range_end.isoformat(),
                )
                if dropped >= MAX_DROPS_PER_RUN:
                    return dropped
        return dropped


async def rollup_watermark(db: AsyncSession) -> datetime | None:
    """How far the rollups have caught up, across both tiers.

    The **older** of the two tiers' newest buckets, because raw data is needed
    until *both* have consumed it. Taking the newer would let the hourly tier's
    progress authorise deleting raw data the five-minute tier had not seen.

    ``None`` when either tier has produced nothing, which means no raw data may
    be expired at all.
    """
    newest: list[datetime] = []
    for tier in (TIER_5M, TIER_1H):
        table = TIER_TABLES[tier]
        value: datetime | None = (
            await db.execute(_max_bucket_statement(table))
        ).scalar_one_or_none()
        if value is None:
            return None
        newest.append(value)
    return min(newest)


#: The aggregation that fills one rollup tier.
#:
#: A template with a ``{table}`` placeholder rather than an f-string, and the
#: substitution is a plain ``str.replace`` in :func:`_rollup_statement`. Not
#: stylistic: ``text()`` cannot parameterise an *identifier*, so the table name
#: has to be interpolated — and a multi-line f-string is a construct the
#: security linter flags and cannot be given a suppression on, because the
#: diagnostic attaches to the line the literal opens on and that line has no
#: room for a comment. A template keeps the one interpolation in one guarded
#: function, which is the property that actually matters.
#:
#: ``first_value`` and ``last_value`` are taken by **time order**, not by
#: magnitude: "first" means earliest, and using ``min``/``max`` of the value
#: would silently make a counter-derived rate wrong in a way that survives
#: review. ``array_agg … ORDER BY`` is how that is expressed in one pass.
_ROLLUP_SQL: Final = """
INSERT INTO "{table}" (
    series_id, bucket_start, min_value, max_value, avg_value,
    first_value, last_value, sample_count
)
SELECT
    bucketed.series_id,
    bucketed.bucket_start,
    min(bucketed.value),
    max(bucketed.value),
    avg(bucketed.value),
    (array_agg(bucketed.value ORDER BY bucketed.recorded_at ASC))[1],
    (array_agg(bucketed.value ORDER BY bucketed.recorded_at DESC))[1],
    count(*)
FROM (
    SELECT
        series_id,
        recorded_at,
        value,
        to_timestamp(
            floor(extract(epoch FROM recorded_at) / :width) * :width
        ) AS bucket_start
    FROM metric_samples_raw
    WHERE recorded_at >= :start_from
      AND recorded_at < :sealed_before
) AS bucketed
GROUP BY bucketed.series_id, bucketed.bucket_start
ORDER BY bucketed.bucket_start
LIMIT :limit
ON CONFLICT (series_id, bucket_start) DO UPDATE SET
    min_value = EXCLUDED.min_value,
    max_value = EXCLUDED.max_value,
    avg_value = EXCLUDED.avg_value,
    first_value = EXCLUDED.first_value,
    last_value = EXCLUDED.last_value,
    sample_count = EXCLUDED.sample_count
"""

#: ``SELECT max(bucket_start)`` for one rollup table. Same arrangement.
_MAX_BUCKET_SQL: Final = 'SELECT max(bucket_start) FROM "{table}"'


def _metric_table(table: str) -> str:
    """Return ``table`` if it is one of this schema's metric tables.

    The single guard every identifier interpolation in this module goes
    through. The only possible values are
    :data:`hcc.models.metrics.TIER_TABLES`' own constants, so this cannot
    fail in practice — which is why it raises rather than sanitising: a
    failure here is a programming mistake and should stop, not be corrected.
    """
    if table not in TIER_TABLES.values():
        raise ValueError(f"not a metric table: {table!r}")
    return table


def _timestamp_literal(moment: datetime) -> str:
    """A partition bound as a quoted SQL literal.

    Needed because a partition bound cannot be a bind parameter — see
    :meth:`RetentionService._create_partition`. The safety therefore has to
    come from the type rather than from the driver:

    * The argument must be a ``datetime``, checked rather than assumed, so a
      refactor that passed a string through could not turn this into an
      injection point.
    * It must be time-zone aware, because a naive bound would be interpreted in
      the session's zone and could overlap the neighbouring partition.
    * The output is produced by ``isoformat``, which for an aware ``datetime``
      can only contain digits and ``-:+.T``. There is no quote to escape.
    """
    if not isinstance(moment, datetime):
        raise TypeError(f"partition bound must be a datetime, got {type(moment).__name__}")
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("partition bound must be time-zone aware")
    return f"'{moment.astimezone(UTC).isoformat()}'"


def _rollup_statement(table: str) -> TextClause:
    """The aggregation that fills one rollup tier. See :data:`_ROLLUP_SQL`."""
    return text(_ROLLUP_SQL.replace("{table}", _metric_table(table)))


def _max_bucket_statement(table: str) -> TextClause:
    """``SELECT max(bucket_start)`` for one rollup table."""
    return text(_MAX_BUCKET_SQL.replace("{table}", _metric_table(table)))


@dataclass(frozen=True, slots=True)
class _Window:
    """One partition's range, and the suffix its table is named with."""

    start: datetime
    end: datetime
    suffix: str


def _windows(tier: str, now: datetime, ahead: int, behind: int = 0) -> list[_Window]:
    """The partition windows a tier should have, from ``behind`` to ``ahead``.

    Boundaries are computed **in UTC**, which is not cosmetic: the migration
    that created the first partition truncates in UTC for the same reason, and
    a job computing them in the session's time zone would try to create a
    partition overlapping one that already existed.

    ``behind`` covers backfill. An agent reconnecting after an outage delivers
    samples with their original timestamps, and a window has to exist for each
    or they land in the DEFAULT partition — which nothing ever drops. See
    :data:`PARTITIONS_BEHIND`.

    Returned oldest first, so a caller creating them produces the past before
    the future and a partial run leaves the useful half done.
    """
    days = TIER_PARTITION_DAYS[tier]
    current = _floor_to_days(now.astimezone(UTC), days, tier)
    windows: list[_Window] = []
    for index in range(-behind, ahead + 1):
        start = _advance(current, days, index, tier)
        end = _advance(current, days, index + 1, tier)
        windows.append(_Window(start=start, end=end, suffix=_suffix(tier, start)))
    return windows


def _floor_to_days(moment: datetime, days: int, tier: str) -> datetime:
    """Truncate to the start of this tier's current partition window.

    The hourly tier is partitioned by *calendar month* rather than by thirty
    days, so its boundary is the first of the month. Mixing the two would make
    a partition named ``202610`` cover part of November.
    """
    if tier == TIER_1H:
        return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if days == 1:
        return midnight
    # Weekly: back to Monday, matching PostgreSQL's `date_trunc('week', …)`
    # which the migration used for the first partition.
    return midnight - timedelta(days=midnight.weekday())


def _advance(start: datetime, days: int, periods: int, tier: str) -> datetime:
    """Move ``periods`` partition windows forward from ``start``."""
    if tier == TIER_1H:
        month = start.month - 1 + periods
        year = start.year + month // 12
        return start.replace(year=year, month=month % 12 + 1, day=1)
    return start + timedelta(days=days * periods)


def _suffix(tier: str, start: datetime) -> str:
    """The table-name suffix for a window, matching the migration's format."""
    if tier == TIER_1H:
        return start.strftime("%Y%m")
    return start.strftime("%Y%m%d")


def _floor_to(moment: datetime, width_seconds: int) -> datetime:
    """Truncate an instant down to a multiple of ``width_seconds``.

    Computed on the epoch rather than on calendar fields, so it matches the
    ``floor(extract(epoch …) / width) * width`` the rollup statement uses. Two
    different truncations would make the job seal a bucket the statement then
    recomputed from a different boundary.
    """
    epoch = moment.timestamp()
    return datetime.fromtimestamp((epoch // width_seconds) * width_seconds, tz=UTC)


def policy_from_settings(settings: Any) -> RetentionPolicy:
    """Build a policy from application settings.

    Takes ``Any`` rather than ``Settings`` to avoid importing the configuration
    module into a service — the same arrangement every other service here uses,
    and it keeps this module importable by a maintenance script that has no
    application.
    """
    return RetentionPolicy(
        raw_days=getattr(settings, "metrics_raw_retention_days", DEFAULT_RETENTION_DAYS[TIER_RAW]),
        five_minute_days=getattr(
            settings, "metrics_5m_retention_days", DEFAULT_RETENTION_DAYS[TIER_5M]
        ),
        hourly_days=getattr(settings, "metrics_1h_retention_days", DEFAULT_RETENTION_DAYS[TIER_1H]),
        lateness_minutes=getattr(
            settings, "metrics_rollup_lateness_minutes", DEFAULT_LATENESS_MINUTES
        ),
    )
