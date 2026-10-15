"""Staged rollouts: a canary, then batches, with a gate between them.

Evergreen created the tables — ``update_rollouts`` and
``update_rollout_members``, with a canary batch, a batch size, a failure
tolerance and an offline deadline — and nothing that drove one. This is the
engine.

## The whole point is that a bad release stops after one host

Everything else here is in service of that. An update that goes to forty
machines at once is not a rollout, it is a deployment with extra steps; the
canary's health gating the first batch is the feature, and the batches after
it are the refinement.

## "The process restarted" is not "the upgrade succeeded"

The job concludes when the agent reconnects reporting the target version.
That is a real check and a narrow one: an agent that comes up, reports its
version and then crashes a minute later has satisfied it completely.

So a rollout does not advance on the job. It advances on a **member health
check** taken after the install, over a settling period the rollout
configures, which asks four separate questions:

1. **Is the host connected now?** Not "did it connect once".
2. **Is it reporting the target version?** A host that reconnected on the
   old binary has rolled back, whatever its job says.
3. **Has it acknowledged its monitoring configuration at the current
   revision?** This is the one that catches an agent which starts, connects
   and then fails to apply anything — a half-working binary looks identical
   to a working one until something asks it to do work.
4. **Has it delivered telemetry since the install?** Only asked when the
   host has a module enabled, because a host collecting nothing cannot
   answer and holding it to that would stall every rollout to a
   monitoring-free fleet.

A member that passes all four, continuously, for ``health_check_seconds``,
is healthy. One that passed and then stopped is not, and the gate notices on
the next pass.

## Three outcomes, not two

Succeeded, failed, and **skipped**. A host that is powered off past the
offline deadline is skipped, and a skip does not count against the failure
tolerance — a NAS somebody unplugged says nothing about the release, and
stopping a fleet rollout over one would be a gate that fires for the wrong
reason. The skip is recorded with its reason and appears in the rollout's
summary, because a rollout that quietly left four hosts behind is worse than
one that says it did.

## Everything is derived from the database on every pass

There is no in-memory rollout state, no scheduler object and no timer per
rollout. A pass reads the rows, works out what should happen, and writes
what it did. That is what makes a controller restart a non-event: the next
pass reaches the same conclusion from the same rows.

It is also what makes the engine testable without waiting: ``now`` is a
parameter everywhere.

## Concurrency

One rollout advances at a time per pass, and a pass holds a transaction-level
advisory lock. Two controllers — or a request handler and the background
task — cannot both start the same batch, which would create two jobs for one
host and hit the partial unique index as an error rather than a decision.
"""

from __future__ import annotations

import uuid  # noqa: TC003  -- a runtime annotation: dataclass fields are resolved
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from hcc.models.host import Host
from hcc.models.metrics import MetricSeries
from hcc.models.monitoring import HostConfigRevision
from hcc.models.updates import (
    TERMINAL_ROLLOUT_STATES,
    RolloutState,
    UpdateJob,
    UpdateRollout,
    UpdateRolloutMember,
    UpdateState,
    UpdateTrigger,
    is_terminal,
)
from hcc.updates.policy import MaintenanceWindow
from hcc.updates.version import try_parse

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from hcc.agents.releases import ReleaseManifest
    from hcc.security.audit import AuditContext
    from hcc.services.updates import UpdateService

logger = structlog.get_logger(__name__)

#: The advisory lock the engine takes while advancing rollouts.
#:
#: One number for the whole engine rather than one per rollout: a pass
#: advances every rollout, and two passes overlapping is the thing to
#: prevent. Transaction-scoped, so it is released by the commit or the
#: rollback and never leaks on an exception.
ROLLOUT_LOCK_KEY: Final = 0x48_43_43_52  # "HCCR"

#: Most hosts one rollout may contain.
#:
#: Not a limit on a fleet — a rollout of two hundred hosts is a reasonable
#: thing to want — but on one *request*: the members are created in a single
#: transaction, and a request that writes ten thousand rows is a request that
#: holds a connection long enough to matter.
MAX_ROLLOUT_HOSTS: Final = 500


class RolloutError(Exception):
    """A rollout operation that cannot be performed."""


class RolloutNotFoundError(RolloutError):
    def __init__(self, rollout_id: uuid.UUID) -> None:
        self.rollout_id = rollout_id
        super().__init__(f"no rollout {rollout_id}")


class RolloutNotResumableError(RolloutError):
    """Resume was asked for on a rollout that is not paused."""


class RolloutInvalidError(RolloutError):
    """The rollout as described cannot be created."""


@dataclass(frozen=True, slots=True)
class MemberOutcome:
    """Where one member has got to, derived rather than stored.

    Stored would mean two sources of truth — this and the job's own state —
    which is exactly the pair that drifts when a controller restarts
    mid-transition.
    """

    host_id: uuid.UUID
    batch: int
    #: pending | running | succeeded | failed | skipped
    state: str
    #: Set once the post-install health check has passed.
    healthy: bool = False
    detail: str | None = None

    @property
    def is_finished(self) -> bool:
        """Whether nothing further will happen to this member."""
        return self.state in {"succeeded", "failed", "skipped"}

    @property
    def counts_as_failure(self) -> bool:
        """Whether this member should count against the failure tolerance.

        A skip does not. A powered-off host says nothing about the release,
        and stopping a fleet rollout over one would be a gate firing for a
        reason unrelated to the thing it is guarding.
        """
        return self.state == "failed"


@dataclass(slots=True)
class RolloutProgress:
    """What a pass found and did, for a log line and for the API."""

    rollout_id: uuid.UUID
    state: str
    current_batch: int | None = None
    started: list[uuid.UUID] = field(default_factory=list)
    skipped: list[uuid.UUID] = field(default_factory=list)
    became_healthy: list[uuid.UUID] = field(default_factory=list)
    paused_reason: str | None = None
    completed: bool = False

    @property
    def did_something(self) -> bool:
        return bool(
            self.started
            or self.skipped
            or self.became_healthy
            or self.paused_reason
            or self.completed
        )


@dataclass(slots=True)
class RolloutPassResult:
    """Every rollout a pass touched."""

    progressed: list[RolloutProgress] = field(default_factory=list)

    @property
    def did_something(self) -> bool:
        return any(progress.did_something for progress in self.progressed)


class RolloutService:
    """Creates rollouts and advances them.

    Holds no state. Every method takes the session it works in, and every
    decision is re-derived from rows — see the module docstring.
    """

    def __init__(self, updates: UpdateService) -> None:
        self._updates = updates

    # --- Creation ---------------------------------------------------------

    async def create(
        self,
        db: AsyncSession,
        *,
        name: str,
        to_version: str,
        host_ids: Sequence[uuid.UUID],
        canary_host_ids: Sequence[uuid.UUID] = (),
        canary_count: int = 1,
        batch_size: int = 5,
        failure_tolerance: int = 0,
        health_check_seconds: int = 120,
        offline_deadline_seconds: int = 3600,
        window: MaintenanceWindow | None = None,
        context: AuditContext | None = None,
        now: datetime | None = None,
    ) -> UpdateRollout:
        """Create a rollout and assign its hosts to batches.

        Nothing starts here. A created rollout is ``pending`` until a pass
        picks it up, which is what makes "create it now, let it run tonight"
        work with a maintenance window and what keeps the request that
        creates it from doing the work of forty installs.

        ## Batch assignment

        The canary is batch 0. Named hosts go in it; if fewer than
        ``canary_count`` are named, it is filled from the remaining hosts —
        **connected ones first**, because a canary that is powered off tells
        nobody anything and would hold the whole rollout at the offline
        deadline before a single real host was touched.

        The rest are chunked by ``batch_size`` in a stable order, so the
        assignment is reproducible and an administrator reading it can see
        why a host is where it is.

        Raises:
            RolloutInvalidError: no hosts, too many, an unparseable version,
                or a named canary that is not in the host list.
        """
        moment = now or datetime.now(UTC)

        if try_parse(to_version) is None:
            raise RolloutInvalidError(
                f"{to_version!r} is not a semantic version, so it cannot be compared "
                "with what hosts are running"
            )
        unique_hosts = list(dict.fromkeys(host_ids))
        if not unique_hosts:
            raise RolloutInvalidError("a rollout needs at least one host")
        if len(unique_hosts) > MAX_ROLLOUT_HOSTS:
            raise RolloutInvalidError(
                f"a rollout may contain at most {MAX_ROLLOUT_HOSTS} hosts, "
                f"and this one names {len(unique_hosts)}"
            )
        if canary_count > len(unique_hosts):
            raise RolloutInvalidError(
                f"the canary is {canary_count} hosts and the rollout has "
                f"{len(unique_hosts)}; there would be no batch after it"
            )
        named_canaries = list(dict.fromkeys(canary_host_ids))
        strays = [host for host in named_canaries if host not in unique_hosts]
        if strays:
            raise RolloutInvalidError(
                "every canary must also be a member of the rollout; "
                f"{len(strays)} named host(s) are not"
            )

        # Every host that exists, enabled, in a stable order. Connectivity
        # decides which fill the canary, and the name decides the rest — so
        # two runs of the same request produce the same assignment.
        rows = (
            (
                await db.execute(
                    select(Host)
                    .where(Host.id.in_(unique_hosts))
                    .order_by(Host.current_connection_id.is_(None), Host.name, Host.id)
                )
            )
            .scalars()
            .all()
        )
        found = {host.id for host in rows}
        missing = [host for host in unique_hosts if host not in found]
        if missing:
            raise RolloutInvalidError(f"{len(missing)} host(s) in this rollout do not exist")

        ordered = [host.id for host in rows]
        canary = list(named_canaries)
        for host_id in ordered:
            if len(canary) >= canary_count:
                break
            if host_id not in canary:
                canary.append(host_id)
        rest = [host_id for host_id in ordered if host_id not in canary]

        rollout = UpdateRollout(
            name=name,
            to_version=to_version,
            state=RolloutState.PENDING.value,
            canary_count=len(canary),
            batch_size=batch_size,
            failure_tolerance=failure_tolerance,
            health_check_seconds=health_check_seconds,
            offline_deadline_seconds=offline_deadline_seconds,
            created_by_user_id=context.actor_user_id if context else None,
        )
        if window is not None and not window.is_always_open:
            rollout.window_timezone = window.timezone
            rollout.window_weekdays = list(window.weekdays) or None
            rollout.window_start_minute = window.start.hour * 60 + window.start.minute
            rollout.window_end_minute = window.end.hour * 60 + window.end.minute
        db.add(rollout)
        await db.flush()

        for host_id in canary:
            db.add(UpdateRolloutMember(rollout_id=rollout.id, host_id=host_id, batch=0))
        for index, host_id in enumerate(rest):
            db.add(
                UpdateRolloutMember(
                    rollout_id=rollout.id,
                    host_id=host_id,
                    batch=1 + index // max(1, batch_size),
                )
            )
        await db.flush()

        logger.info(
            "rollout_created",
            rollout_id=str(rollout.id),
            to_version=to_version,
            hosts=len(ordered),
            canary=len(canary),
            batches=1 + (len(rest) + batch_size - 1) // max(1, batch_size),
            created_at=moment.isoformat(),
        )
        return rollout

    # --- Control ----------------------------------------------------------

    async def get(self, db: AsyncSession, rollout_id: uuid.UUID) -> UpdateRollout:
        rollout = (
            await db.execute(select(UpdateRollout).where(UpdateRollout.id == rollout_id))
        ).scalar_one_or_none()
        if rollout is None:
            raise RolloutNotFoundError(rollout_id)
        return rollout

    async def pause(self, db: AsyncSession, rollout_id: uuid.UUID, *, reason: str) -> UpdateRollout:
        """Stop starting new members. In-flight ones finish.

        Deliberately not a cancellation of what is running: an install that
        is halfway through replacing a binary is the worst possible moment to
        abandon, and the agent's own rollback is what handles a bad one.
        """
        rollout = await self.get(db, rollout_id)
        if rollout.state in TERMINAL_ROLLOUT_STATES:
            raise RolloutNotResumableError(f"this rollout is {rollout.state} and cannot be paused")
        rollout.state = RolloutState.PAUSED.value
        rollout.paused_reason = reason
        logger.info("rollout_paused", rollout_id=str(rollout_id), reason=reason)
        return rollout

    async def resume(
        self,
        db: AsyncSession,
        rollout_id: uuid.UUID,
        *,
        override_failures: bool = False,
    ) -> UpdateRollout:
        """Start it again.

        ``override_failures`` is the administrator saying "I have looked at
        the canary that failed and I want the rest anyway". Without it, a
        rollout paused by the failure gate pauses again on the next pass —
        which is the correct behaviour and would otherwise look like a bug.

        The override is **recorded on the rollout**, not implied by the fact
        that it resumed: "resumed after a failure" and "resumed after an
        administrator paused it" are different facts, and the second must not
        be able to look like the first afterwards.
        """
        rollout = await self.get(db, rollout_id)
        if rollout.state != RolloutState.PAUSED.value:
            raise RolloutNotResumableError(
                f"this rollout is {rollout.state}, not paused, so there is nothing to resume"
            )
        rollout.state = RolloutState.RUNNING.value
        rollout.paused_reason = None
        if override_failures:
            rollout.failures_overridden = True
        logger.info("rollout_resumed", rollout_id=str(rollout_id), overridden=override_failures)
        return rollout

    async def cancel(
        self, db: AsyncSession, rollout_id: uuid.UUID, *, reason: str
    ) -> UpdateRollout:
        """Abandon it. Members that have not started never will.

        In-flight jobs are **not** cancelled here. The job has its own cancel,
        which an administrator can use per host; cancelling an install that is
        replacing a binary from a fleet-level button is a bigger hammer than
        anybody wants, and the agent would ignore it anyway once the
        privileged installer has the request.
        """
        rollout = await self.get(db, rollout_id)
        if rollout.state in TERMINAL_ROLLOUT_STATES:
            return rollout
        rollout.state = RolloutState.CANCELLED.value
        rollout.paused_reason = reason
        rollout.finished_at = datetime.now(UTC)
        logger.info("rollout_cancelled", rollout_id=str(rollout_id), reason=reason)
        return rollout

    # --- Inspection -------------------------------------------------------

    async def outcomes(
        self, db: AsyncSession, rollout: UpdateRollout, *, now: datetime | None = None
    ) -> list[MemberOutcome]:
        """Where every member of *rollout* has got to.

        Derived from the members and their jobs on every call. See the module
        docstring for why nothing is stored.
        """
        moment = now or datetime.now(UTC)
        # Two queries and a dictionary rather than an outer join. The join is
        # one statement and makes "this member has no job yet" a `None` that
        # the type checker cannot see — it reads the join as producing an
        # `UpdateJob` and calls the `is None` branch unreachable. A dictionary
        # miss is the same fact, visibly.
        members = (
            (
                await db.execute(
                    select(UpdateRolloutMember)
                    .where(UpdateRolloutMember.rollout_id == rollout.id)
                    .order_by(UpdateRolloutMember.batch, UpdateRolloutMember.host_id)
                )
            )
            .scalars()
            .all()
        )
        job_ids = [member.job_id for member in members if member.job_id is not None]
        jobs = (
            {
                job.id: job
                for job in (
                    await db.execute(select(UpdateJob).where(UpdateJob.id.in_(job_ids)))
                ).scalars()
            }
            if job_ids
            else {}
        )

        outcomes: list[MemberOutcome] = []
        for member in members:
            job = jobs.get(member.job_id) if member.job_id is not None else None
            if member.skipped_at is not None:
                outcomes.append(
                    MemberOutcome(
                        host_id=member.host_id,
                        batch=member.batch,
                        state="skipped",
                        detail=member.skipped_reason,
                    )
                )
                continue
            if job is None:
                outcomes.append(
                    MemberOutcome(host_id=member.host_id, batch=member.batch, state="pending")
                )
                continue
            if not is_terminal(job.state):
                outcomes.append(
                    MemberOutcome(
                        host_id=member.host_id,
                        batch=member.batch,
                        state="running",
                        detail=job.state,
                    )
                )
                continue
            if job.state == UpdateState.SUCCEEDED.value:
                outcomes.append(
                    MemberOutcome(
                        host_id=member.host_id,
                        batch=member.batch,
                        state="succeeded",
                        healthy=member.healthy_at is not None and member.healthy_at <= moment,
                        detail=job.resulting_version,
                    )
                )
                continue
            outcomes.append(
                MemberOutcome(
                    host_id=member.host_id,
                    batch=member.batch,
                    state="failed",
                    detail=job.error or job.state,
                )
            )
        return outcomes

    def window_of(self, rollout: UpdateRollout) -> MaintenanceWindow | None:
        """The rollout's own maintenance window, or None."""
        if rollout.window_timezone is None:
            return None
        from datetime import time as clock_time

        def _to_time(minute: int | None) -> clock_time:
            value = minute or 0
            return clock_time(value // 60, value % 60)

        return MaintenanceWindow(
            timezone=rollout.window_timezone,
            weekdays=tuple(rollout.window_weekdays or ()),
            start=_to_time(rollout.window_start_minute),
            end=_to_time(rollout.window_end_minute),
        )

    # --- The pass ---------------------------------------------------------

    async def run_pass(
        self,
        db: AsyncSession,
        *,
        manifest: ReleaseManifest | None,
        applied_schema_revisions: frozenset[str] = frozenset(),
        metadata_authentic: bool = False,
        now: datetime | None = None,
    ) -> RolloutPassResult:
        """Advance every rollout that is not finished.

        Takes a transaction-scoped advisory lock first: two passes, or a pass
        and a request handler, both deciding to start the same batch would
        create two jobs for one host — which the partial unique index turns
        into an error rather than a decision.
        """
        moment = now or datetime.now(UTC)
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ROLLOUT_LOCK_KEY})

        rollouts = (
            (
                await db.execute(
                    select(UpdateRollout)
                    .where(UpdateRollout.state.notin_([s.value for s in TERMINAL_ROLLOUT_STATES]))
                    .order_by(UpdateRollout.created_at)
                )
            )
            .scalars()
            .all()
        )

        result = RolloutPassResult()
        for rollout in rollouts:
            progress = await self._advance(
                db,
                rollout,
                manifest=manifest,
                applied_schema_revisions=applied_schema_revisions,
                metadata_authentic=metadata_authentic,
                now=moment,
            )
            result.progressed.append(progress)
        return result

    async def _advance(
        self,
        db: AsyncSession,
        rollout: UpdateRollout,
        *,
        manifest: ReleaseManifest | None,
        applied_schema_revisions: frozenset[str],
        metadata_authentic: bool,
        now: datetime,
    ) -> RolloutProgress:
        progress = RolloutProgress(rollout_id=rollout.id, state=rollout.state)

        # Health first, so a member that became healthy since the last pass is
        # counted before the gate below looks at the batch.
        progress.became_healthy = await self._refresh_health(db, rollout, now=now)

        outcomes = await self.outcomes(db, rollout, now=now)
        if not outcomes:
            rollout.state = RolloutState.COMPLETED.value
            rollout.finished_at = now
            progress.state = rollout.state
            progress.completed = True
            return progress

        failures = sum(1 for outcome in outcomes if outcome.counts_as_failure)
        if failures > rollout.failure_tolerance and not rollout.failures_overridden:
            if rollout.state != RolloutState.PAUSED.value:
                reason = (
                    f"{failures} host(s) failed and this rollout tolerates "
                    f"{rollout.failure_tolerance}. Nothing further will start until an "
                    "administrator resumes it, and resuming needs the failures to be "
                    "overridden explicitly."
                )
                rollout.state = RolloutState.PAUSED.value
                rollout.paused_reason = reason
                progress.paused_reason = reason
                logger.warning(
                    "rollout_paused_by_failures",
                    rollout_id=str(rollout.id),
                    failures=failures,
                    tolerance=rollout.failure_tolerance,
                )
            progress.state = rollout.state
            return progress

        if rollout.state == RolloutState.PAUSED.value:
            # An administrator's pause. In-flight members keep going — their
            # health is refreshed above — and nothing new starts.
            progress.state = rollout.state
            return progress

        if all(outcome.is_finished for outcome in outcomes):
            rollout.state = RolloutState.COMPLETED.value
            rollout.finished_at = now
            progress.state = rollout.state
            progress.completed = True
            logger.info(
                "rollout_completed",
                rollout_id=str(rollout.id),
                succeeded=sum(1 for o in outcomes if o.state == "succeeded"),
                failed=failures,
                skipped=sum(1 for o in outcomes if o.state == "skipped"),
            )
            return progress

        batch = min(outcome.batch for outcome in outcomes if not outcome.is_finished)
        progress.current_batch = batch
        in_batch = [outcome for outcome in outcomes if outcome.batch == batch]

        # Anything earlier still unfinished means a previous batch has an
        # in-flight member; `batch` already accounts for that by taking the
        # minimum, so this is the batch to work on.
        running = [outcome for outcome in in_batch if outcome.state == "running"]
        pending = [outcome for outcome in in_batch if outcome.state == "pending"]

        # The gate. Every finished member of *every earlier* batch must be
        # healthy before this batch starts — which for batch 1 is the canary,
        # and is the entire feature.
        if batch > 0:
            earlier = [outcome for outcome in outcomes if outcome.batch < batch]
            unhealthy = [
                outcome
                for outcome in earlier
                if outcome.state == "succeeded" and not outcome.healthy
            ]
            if unhealthy:
                # Not a pause: the health check is still settling, and this
                # is the ordinary state of a rollout thirty seconds after its
                # canary finished installing.
                progress.state = rollout.state
                return progress

        if running:
            # Members in flight. Only thing to do is notice ones that have
            # been pending too long, which the skip below handles for the
            # *pending* ones.
            await self._skip_stragglers(db, rollout, pending, progress, now=now)
            progress.state = rollout.state
            return progress

        if not pending:
            # The batch is done; the next pass picks up the next batch, after
            # the health gate above has had a chance to pass.
            progress.state = rollout.state
            return progress

        window = self.window_of(rollout)
        if window is not None and not window.admits(now):
            # Outside the window. Nothing starts, and nothing is skipped: a
            # host waiting for its window is not a host that failed.
            progress.state = rollout.state
            return progress

        # Start the batch.
        if rollout.state == RolloutState.PENDING.value:
            rollout.started_at = now
        rollout.state = RolloutState.CANARY.value if batch == 0 else RolloutState.RUNNING.value
        progress.state = rollout.state

        members = {
            member.host_id: member
            for member in (
                await db.execute(
                    select(UpdateRolloutMember).where(
                        UpdateRolloutMember.rollout_id == rollout.id,
                        UpdateRolloutMember.batch == batch,
                    )
                )
            ).scalars()
        }

        for outcome in pending:
            member = members.get(outcome.host_id)
            if member is None:
                continue
            if member.started_at is None:
                member.started_at = now
            started = await self._start_member(
                db,
                rollout,
                member,
                manifest=manifest,
                applied_schema_revisions=applied_schema_revisions,
                metadata_authentic=metadata_authentic,
                now=now,
            )
            if started:
                progress.started.append(member.host_id)
            elif member.skipped_at is not None:
                progress.skipped.append(member.host_id)

        # A batch can finish inside the pass that started it: every member
        # skipped, because they were offline past the deadline or already
        # running the target version. Re-deriving here rather than waiting
        # for the next pass is the difference between a rollout that reports
        # "completed" immediately and one that sits on "running" for an
        # interval with nothing left to do — which looks stuck.
        if progress.skipped and not progress.started:
            remaining = await self.outcomes(db, rollout, now=now)
            if all(outcome.is_finished for outcome in remaining):
                rollout.state = RolloutState.COMPLETED.value
                rollout.finished_at = now
                progress.state = rollout.state
                progress.completed = True
                logger.info(
                    "rollout_completed",
                    rollout_id=str(rollout.id),
                    succeeded=sum(1 for o in remaining if o.state == "succeeded"),
                    failed=sum(1 for o in remaining if o.counts_as_failure),
                    skipped=sum(1 for o in remaining if o.state == "skipped"),
                )

        return progress

    async def _skip_stragglers(
        self,
        db: AsyncSession,
        rollout: UpdateRollout,
        pending: Sequence[MemberOutcome],
        progress: RolloutProgress,
        *,
        now: datetime,
    ) -> None:
        """Skip members whose offline deadline has passed."""
        if not pending:
            return
        deadline = timedelta(seconds=rollout.offline_deadline_seconds)
        members = {
            member.host_id: member
            for member in (
                await db.execute(
                    select(UpdateRolloutMember).where(
                        UpdateRolloutMember.rollout_id == rollout.id,
                        UpdateRolloutMember.host_id.in_([o.host_id for o in pending]),
                    )
                )
            ).scalars()
        }
        for outcome in pending:
            member = members.get(outcome.host_id)
            if member is None or member.started_at is None:
                continue
            if now - member.started_at < deadline:
                continue
            member.skipped_at = now
            member.skipped_reason = (
                f"this host did not come online within "
                f"{rollout.offline_deadline_seconds}s of its batch starting, so the "
                "rollout moved on. It was not updated and this is not counted as a "
                "failure."
            )
            progress.skipped.append(member.host_id)
            logger.info(
                "rollout_member_skipped",
                rollout_id=str(rollout.id),
                host_id=str(member.host_id),
            )

    async def _start_member(
        self,
        db: AsyncSession,
        rollout: UpdateRollout,
        member: UpdateRolloutMember,
        *,
        manifest: ReleaseManifest | None,
        applied_schema_revisions: frozenset[str],
        metadata_authentic: bool,
        now: datetime,
    ) -> bool:
        """Create one member's update job, or decide not to.

        Returns whether a job was created. A host that is offline is left
        pending — it may come back — until the offline deadline, which
        [_skip_stragglers] enforces on a later pass.
        """
        host = (
            await db.execute(select(Host).where(Host.id == member.host_id))
        ).scalar_one_or_none()
        if host is None:
            member.skipped_at = now
            member.skipped_reason = "this host no longer exists"
            return False
        if not host.is_enabled:
            member.skipped_at = now
            member.skipped_reason = "this host is disabled, so nothing was sent to it"
            return False
        if host.current_connection_id is None:
            # Offline. Not skipped yet: the deadline is what decides, and a
            # host that is rebooting should not be written off in the second
            # its batch happens to start.
            if member.started_at is not None and now - member.started_at >= timedelta(
                seconds=rollout.offline_deadline_seconds
            ):
                member.skipped_at = now
                member.skipped_reason = (
                    f"this host did not come online within "
                    f"{rollout.offline_deadline_seconds}s of its batch starting"
                )
            return False

        from hcc.services.updates import UpdateNotPossibleError, UpdateServiceError

        try:
            job = await self._updates.start_update(
                db,
                member.host_id,
                manifest=manifest,
                applied_schema_revisions=applied_schema_revisions,
                metadata_authentic=metadata_authentic,
                trigger=UpdateTrigger.ROLLOUT,
                target_version=rollout.to_version,
                rollout_id=rollout.id,
                now=now,
            )
        except UpdateNotPossibleError as error:
            # Nothing to install, or something blocks it. Skipped rather than
            # failed: a host already running the target version is not a
            # failure of the release, and counting it as one would pause a
            # rollout for doing exactly what was asked.
            #
            # The reason is rewritten for the one case that reads badly. The
            # planner is asked for `to_version` as a *pin*, so a host already
            # on it comes back as "this host is already pinned", which is true
            # of the question asked and nonsense as an explanation of why a
            # rollout skipped a host.
            member.skipped_at = now
            if "already pinned" in str(error):
                member.skipped_reason = (
                    f"this host is already running {rollout.to_version}, so there was "
                    "nothing to install"
                )
            else:
                member.skipped_reason = str(error)
            logger.info(
                "rollout_member_not_possible",
                rollout_id=str(rollout.id),
                host_id=str(member.host_id),
                reason=str(error),
            )
            return False
        except (UpdateServiceError, IntegrityError) as error:
            # An open job from somewhere else, or a race. Left pending; the
            # next pass tries again, and the offline deadline is the backstop
            # that stops it trying for ever.
            logger.warning(
                "rollout_member_start_failed",
                rollout_id=str(rollout.id),
                host_id=str(member.host_id),
                error=str(error),
            )
            return False

        member.job_id = job.id
        return True

    # --- Health -----------------------------------------------------------

    async def _refresh_health(
        self, db: AsyncSession, rollout: UpdateRollout, *, now: datetime
    ) -> list[uuid.UUID]:
        """Mark members healthy once they have settled.

        See the module docstring for the four questions. This is where "the
        process restarted" stops being enough.
        """
        rows = (
            await db.execute(
                select(UpdateRolloutMember, UpdateJob, Host)
                .join(UpdateJob, UpdateJob.id == UpdateRolloutMember.job_id)
                .join(Host, Host.id == UpdateRolloutMember.host_id)
                .where(
                    UpdateRolloutMember.rollout_id == rollout.id,
                    UpdateRolloutMember.healthy_at.is_(None),
                    UpdateJob.state == UpdateState.SUCCEEDED.value,
                )
            )
        ).all()

        became: list[uuid.UUID] = []
        settle = timedelta(seconds=rollout.health_check_seconds)
        for member, job, host in rows:
            # `finished_at` is set by the terminal transition; `updated_at` is
            # the trigger's, and is the fallback for a row written before the
            # column existed. One of them is always present on a succeeded job.
            finished = job.finished_at or job.updated_at
            if now - finished < settle:
                # Still settling. Nothing is wrong; it is simply too early to
                # say the host survived.
                continue
            problem = await self._health_problem(db, host, rollout, since=finished)
            if problem is not None:
                # Not marked healthy. The gate holds, and the next pass asks
                # again — a host that is briefly busy recovers on its own,
                # and one that is genuinely gone keeps the rollout stopped,
                # which is the point.
                logger.info(
                    "rollout_member_not_healthy",
                    rollout_id=str(rollout.id),
                    host_id=str(host.id),
                    problem=problem,
                )
                continue
            member.healthy_at = now
            became.append(host.id)
        return became

    async def _health_problem(
        self,
        db: AsyncSession,
        host: Host,
        rollout: UpdateRollout,
        *,
        since: datetime,
    ) -> str | None:
        """Why this host is not yet healthy, or None.

        Four questions, in the order that makes the first failure the most
        informative.
        """
        if host.current_connection_id is None:
            return "the host is not connected"

        installed = try_parse(host.agent_version)
        target = try_parse(rollout.to_version)
        if installed is None or target is None or installed != target:
            return f"the host reports {host.agent_version!r} rather than {rollout.to_version}"

        # Has it acknowledged the configuration it was sent? This is the
        # question that catches a half-working binary: an agent can start,
        # connect and report a version while being unable to apply anything,
        # and nothing above would notice.
        desired = (
            await db.execute(
                select(func.max(HostConfigRevision.version)).where(
                    HostConfigRevision.host_id == host.id
                )
            )
        ).scalar_one_or_none()
        if desired is not None and host.reported_config_version != desired:
            return (
                f"the host has not acknowledged configuration revision {desired} "
                f"(it reports {host.reported_config_version})"
            )

        # Has it delivered telemetry since the install? Only asked of a host
        # that has a series at all — a host collecting nothing cannot answer,
        # and holding it to this would stall every rollout to a
        # monitoring-free fleet.
        latest = (
            await db.execute(
                select(func.max(MetricSeries.last_sample_at)).where(MetricSeries.host_id == host.id)
            )
        ).scalar_one_or_none()
        if latest is not None and latest < since:
            return "the host has not delivered any telemetry since the update"
        return None
