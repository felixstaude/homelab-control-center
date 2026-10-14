"""Update management: the service that owns the state machine.

Everything that changes an update's state goes through here, and that is the
point of the module existing. The decision logic is pure and lives in
:mod:`hcc.updates`; this is the part that touches the database, takes locks,
writes audit events and refuses things — and keeping those two apart is what
makes the first testable and the second reviewable.

## Three invariants, each enforced in one place

1. **At most one open job per host.** The partial unique index
   ``ix_update_jobs_one_open_per_host`` is the authority; this service catches
   the integrity error and turns it into :class:`UpdateAlreadyRunningError`.
   It also checks first, because a clear refusal beats a caught exception —
   but the check is an optimisation and the index is the guarantee. Two
   administrators clicking "update" in the same second is not a rare case in a
   fleet tool.

2. **Every state change is a transition the table permits.** `_transition`
   consults :func:`~hcc.models.updates.can_transition` and writes an
   :class:`~hcc.models.updates.UpdateEvent`. There is no other way to change
   ``UpdateJob.state``, so the event log cannot disagree with the job.

3. **An agent only ever addresses its own job.** Every agent-facing method
   takes the ``host_id`` the connection authenticated as *and* the instruction
   id, and matches on both. An agent cannot report progress for another host's
   update even if it knows the identifier, because the query will not find it.

## Why the agent reports state rather than the controller inferring it

The controller cannot see the inside of an install. It can see that an agent
reconnected and what version it claims, which is enough to conclude "it
worked" — but not enough to distinguish "downloading" from "verifying", and
those are what somebody watching a stuck update needs.

So the agent reports, and the controller validates the report against the
transition table. A compromised agent can lie about its own progress, which
costs it an inaccurate row about itself and nothing else: the states it claims
do not authorise anything, and the final word on what it is running is the
version it reports on the socket, which is also self-reported but is checked
against what was asked for.

## Expiry is the mechanism that makes offline hosts safe

Every job carries ``expires_at``. A host that was powered off for a day must
not wake up and install last week's decision — the release may have been
withdrawn, the controller may have been downgraded, the administrator may have
changed their mind. The agent enforces it too, which is the half that matters,
because the controller cannot reach a disconnected host to withdraw anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Final

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from hcc.logging import get_logger
from hcc.models.audit import AuditAction
from hcc.models.host import Host
from hcc.models.updates import (
    ERROR_MAX_LENGTH,
    IN_FLIGHT_STATES,
    TERMINAL_STATES,
    HostUpdatePolicy,
    UpdateEvent,
    UpdateJob,
    UpdateState,
    UpdateTrigger,
    can_transition,
    is_terminal,
)
from hcc.services.hosts import HOST_LOCK_CLASS, HostNotFoundError
from hcc.updates.compat import Outcome, plan_update, version_distribution
from hcc.updates.policy import (
    ALWAYS,
    DEFAULT_POLICY,
    Action,
    MaintenanceWindow,
    Policy,
    WindowError,
    decide,
)
from hcc.updates.version import try_parse

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from hcc.agents.releases import ReleaseManifest
    from hcc.security.audit import AuditContext, AuditService
    from hcc.updates.compat import UpdatePlan

logger = get_logger(__name__)

#: How long an instruction stays valid by default.
#:
#: Two hours. Long enough that a host rebooting, or a slow download on a
#: domestic connection, does not lose its instruction; short enough that a
#: machine switched on the next morning re-plans against current metadata
#: instead of acting on yesterday's decision.
DEFAULT_INSTRUCTION_TTL: Final = timedelta(hours=2)

#: The most history one API page will return.
#:
#: The history endpoint paginates; this is the ceiling a caller may ask for, so
#: a client cannot turn one request into a table scan of every update the
#: fleet has ever performed.
MAX_HISTORY_LIMIT: Final = 200

#: How many attempts a policy-driven update makes before it stops trying.
#:
#: Three. An update that has failed three times is failing for a reason that
#: retrying will not fix, and a scheduler that kept trying would turn one bad
#: release into a host that restarts its agent every few minutes. A manual
#: update is not limited: an administrator retrying is a human who has decided
#: to, and the limit exists to bound *automation*, not people.
MAX_POLICY_ATTEMPTS: Final = 3


@dataclass(frozen=True, slots=True)
class PolicyPassResult:
    """What one pass of the automatic-policy evaluator did.

    Returned rather than only logged, so a test can assert on the decisions
    and so the loop in `hcc/app.py` has something to put in one log line
    instead of one per host. A fleet of ninety hosts where nothing is due
    should produce one quiet line, not ninety.
    """

    #: Hosts with an automatic policy that were evaluated.
    considered: int
    #: The jobs this pass created.
    started: list[UpdateJob]
    #: Hosts with an update due and outside their maintenance window.
    deferred: int
    #: Hosts with something an administrator should see and no action to take.
    notified: int
    #: Hosts skipped because an update was already in flight.
    skipped_open: int
    #: Hosts skipped because this version has already failed too often.
    exhausted: int
    #: Hosts where starting raised. Counted rather than fatal: one host must
    #: not stop the pass.
    failed_to_start: int
    #: Jobs whose instruction had expired, failed by this pass.
    expired: list[UpdateJob]

    @property
    def did_something(self) -> bool:
        """Whether this pass is worth a log line above debug."""
        return bool(self.started or self.expired or self.failed_to_start or self.exhausted)


class UpdateServiceError(Exception):
    """Base class for refusals this service makes."""


class UpdateAlreadyRunningError(UpdateServiceError):
    """A host already has an open update.

    Carries the open job so the caller can say which, because "an update is
    already running" without saying what it is forces the administrator to go
    and look.
    """

    def __init__(self, job: UpdateJob) -> None:
        self.job = job
        super().__init__(
            f"an update to {job.to_version} is already in progress for this host "
            f"(state: {job.state}). Cancel it before starting another."
        )


class UpdateNotFoundError(UpdateServiceError):
    """No such job, or not one this caller may address."""


class UpdateNotCancellableError(UpdateServiceError):
    """The job has got too far to cancel.

    From ``installing`` onwards the binary is being replaced on a machine the
    controller cannot reach into, so "cancel" would be a promise it cannot
    keep. The agent's own rollback is what recovers from there.
    """


class InvalidTransitionError(UpdateServiceError):
    """A state change the machine does not permit."""


class UpdateNotPossibleError(UpdateServiceError):
    """There is no update to start, or something blocks it.

    Carries the plan, so the caller can report *why* — the blockers are the
    useful part and an error string would lose them.
    """

    def __init__(self, plan: UpdatePlan, message: str) -> None:
        self.plan = plan
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class HostUpdateStatus:
    """Everything the interface shows for one host's updates."""

    host_id: uuid.UUID
    host_name: str
    installed_version: str | None
    plan: UpdatePlan
    policy: Policy
    channel: str
    pinned_version: str | None
    window: MaintenanceWindow | None
    open_job: UpdateJob | None
    last_job: UpdateJob | None


@dataclass(frozen=True, slots=True)
class FleetUpdateOverview:
    """The dashboard's numbers, computed once rather than per host."""

    total_hosts: int
    up_to_date: int
    update_available: int
    blocked: int
    needs_attention: int
    #: version → count, newest first. Unparseable versions last and visible.
    version_distribution: list[tuple[str, int]]
    hosts_with_automatic_updates: int
    open_jobs: int
    failed_recently: int
    metadata_available: bool
    #: Why there is no metadata, when there is none.
    metadata_detail: str | None


class UpdateService:
    """Owns update policies, jobs and their transitions.

    Takes its collaborators rather than reaching for them, so a test can run
    the whole state machine against a real database with a fake clock and no
    manifest — which is most of what the interesting tests need.
    """

    def __init__(
        self,
        *,
        audit: AuditService,
        controller_version: str,
        supported_protocol_versions: Sequence[int],
        instruction_ttl: timedelta = DEFAULT_INSTRUCTION_TTL,
    ) -> None:
        self._audit = audit
        self._controller_version = controller_version
        self._supported_protocol_versions = tuple(supported_protocol_versions)
        self._instruction_ttl = instruction_ttl

    # --- Reading ----------------------------------------------------------

    async def policy_for(self, db: AsyncSession, host_id: uuid.UUID) -> HostUpdatePolicy | None:
        """The stored policy row, or ``None`` when the host has never been set.

        ``None`` is not missing data: it is the default, and the default is
        manual. See :meth:`resolve_policy`.
        """
        return (
            await db.execute(select(HostUpdatePolicy).where(HostUpdatePolicy.host_id == host_id))
        ).scalar_one_or_none()

    @staticmethod
    def resolve_policy(
        row: HostUpdatePolicy | None,
    ) -> tuple[Policy, str, str | None, MaintenanceWindow | None]:
        """Turn a stored row — or its absence — into the four effective values.

        One function, so "no row means manual on stable with no window" is
        stated once. A caller that defaulted these itself would be a caller
        that could default them differently.
        """
        if row is None:
            return (DEFAULT_POLICY, "stable", None, None)
        try:
            policy = Policy(row.policy)
        except ValueError:
            # A value the enum does not know. Fail closed to manual rather
            # than crash: a row somebody edited by hand must not make a host
            # unreadable, and manual is the safe reading of "unrecognised".
            logger.warning(
                "update_policy_unrecognised", host_id=str(row.host_id), policy=row.policy
            )
            policy = DEFAULT_POLICY
        window = None
        if (
            row.window_timezone is not None
            and row.window_start_minute is not None
            and row.window_end_minute is not None
        ):
            try:
                window = MaintenanceWindow(
                    timezone=row.window_timezone,
                    weekdays=tuple(row.window_weekdays or ()),
                    start=_minute_to_time(row.window_start_minute),
                    end=_minute_to_time(row.window_end_minute),
                )
            except WindowError:
                # Same reasoning: a stored window that cannot be evaluated is
                # reported as no window rather than making the host's status a
                # 500. The interface shows the policy; the operator can fix it.
                logger.warning(
                    "update_window_unusable",
                    host_id=str(row.host_id),
                    timezone=row.window_timezone,
                )
        return (policy, row.channel, row.pinned_version, window)

    async def open_job(self, db: AsyncSession, host_id: uuid.UUID) -> UpdateJob | None:
        """The host's in-progress job, if it has one."""
        return (
            await db.execute(
                select(UpdateJob)
                .where(
                    UpdateJob.host_id == host_id,
                    UpdateJob.state.notin_([state.value for state in TERMINAL_STATES]),
                )
                .order_by(UpdateJob.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def last_job(self, db: AsyncSession, host_id: uuid.UUID) -> UpdateJob | None:
        """The host's most recent job in any state."""
        return (
            await db.execute(
                select(UpdateJob)
                .where(UpdateJob.host_id == host_id)
                .order_by(UpdateJob.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def host_status(
        self,
        db: AsyncSession,
        host_id: uuid.UUID,
        *,
        manifest: ReleaseManifest | None,
        applied_schema_revisions: frozenset[str] = frozenset(),
        metadata_authentic: bool = False,
    ) -> HostUpdateStatus:
        """Everything the interface shows for one host.

        ``metadata_authentic`` defaults to False and so fails closed: a caller
        that has not verified the manifest's signature has not verified it,
        and an unverifiable release is reported as blocked rather than as
        available. See :func:`hcc.updates.compat.evaluate_compatibility`.
        """
        host = (await db.execute(select(Host).where(Host.id == host_id))).scalar_one_or_none()
        if host is None:
            raise HostNotFoundError(host_id)

        row = await self.policy_for(db, host_id)
        policy, channel, pin, window = self.resolve_policy(row)
        plan = plan_update(
            manifest,
            installed_version=host.agent_version,
            host_platform=host.platform or "",
            channel=channel,
            pinned_version=pin,
            controller_version=self._controller_version,
            supported_protocol_versions=self._supported_protocol_versions,
            applied_schema_revisions=applied_schema_revisions,
            automatic=policy.is_automatic,
            metadata_authentic=metadata_authentic,
        )
        return HostUpdateStatus(
            host_id=host.id,
            host_name=host.name,
            installed_version=host.agent_version,
            plan=plan,
            policy=policy,
            channel=channel,
            pinned_version=pin,
            window=window,
            open_job=await self.open_job(db, host_id),
            last_job=await self.last_job(db, host_id),
        )

    async def fleet_overview(
        self,
        db: AsyncSession,
        *,
        manifest: ReleaseManifest | None,
        applied_schema_revisions: frozenset[str] = frozenset(),
        metadata_authentic: bool = False,
        failure_window: timedelta = timedelta(days=7),
        now: datetime | None = None,
    ) -> FleetUpdateOverview:
        """The dashboard's numbers.

        One pass over the hosts and their policies, rather than calling
        :meth:`host_status` per host: that would be two queries per host plus a
        plan, and the overview is the page people leave open.
        """
        moment = now or datetime.now(UTC)
        rows = (
            await db.execute(
                select(Host, HostUpdatePolicy)
                .outerjoin(HostUpdatePolicy, HostUpdatePolicy.host_id == Host.id)
                .where(Host.is_enabled.is_(True))
            )
        ).all()

        counts = {
            "up_to_date": 0,
            "update_available": 0,
            "blocked": 0,
            "needs_attention": 0,
        }
        automatic = 0
        installed: dict[str, int] = {}
        metadata_detail: str | None = None

        for host, policy_row in rows:
            policy, channel, pin, _ = self.resolve_policy(policy_row)
            if policy.is_automatic:
                automatic += 1
            reported = host.agent_version or "unknown"
            installed[reported] = installed.get(reported, 0) + 1

            plan = plan_update(
                manifest,
                installed_version=host.agent_version,
                host_platform=host.platform or "",
                channel=channel,
                pinned_version=pin,
                controller_version=self._controller_version,
                supported_protocol_versions=self._supported_protocol_versions,
                applied_schema_revisions=applied_schema_revisions,
                automatic=policy.is_automatic,
                metadata_authentic=metadata_authentic,
            )
            if plan.outcome is Outcome.UPDATE_AVAILABLE:
                counts["update_available"] += 1
            elif plan.outcome is Outcome.UPDATE_AVAILABLE_INCOMPATIBLE:
                counts["blocked"] += 1
            elif plan.outcome in {Outcome.UNSUPPORTED, Outcome.UNKNOWN_VERSION}:
                counts["needs_attention"] += 1
            elif plan.outcome in {Outcome.UP_TO_DATE, Outcome.PINNED}:
                counts["up_to_date"] += 1
            elif plan.outcome is Outcome.METADATA_UNAVAILABLE:
                metadata_detail = plan.detail

        open_jobs = (
            await db.execute(
                select(func.count())
                .select_from(UpdateJob)
                .where(UpdateJob.state.in_([state.value for state in IN_FLIGHT_STATES]))
            )
        ).scalar_one()
        failed = (
            await db.execute(
                select(func.count())
                .select_from(UpdateJob)
                .where(
                    UpdateJob.state.in_([UpdateState.FAILED.value, UpdateState.ROLLED_BACK.value]),
                    UpdateJob.created_at >= moment - failure_window,
                )
            )
        ).scalar_one()

        return FleetUpdateOverview(
            total_hosts=len(rows),
            up_to_date=counts["up_to_date"],
            update_available=counts["update_available"],
            blocked=counts["blocked"],
            needs_attention=counts["needs_attention"],
            version_distribution=version_distribution(installed),
            hosts_with_automatic_updates=automatic,
            open_jobs=open_jobs,
            failed_recently=failed,
            metadata_available=manifest is not None,
            metadata_detail=metadata_detail,
        )

    async def history(
        self,
        db: AsyncSession,
        *,
        host_id: uuid.UUID | None = None,
        limit: int = 50,
        before: datetime | None = None,
    ) -> list[UpdateJob]:
        """Finished and in-flight jobs, newest first.

        Keyset pagination on ``created_at`` rather than an offset: the history
        of a fleet grows without bound and ``OFFSET 10000`` makes PostgreSQL
        walk and discard ten thousand rows. The audit log already uses keyset
        for the same reason.
        """
        bounded = max(1, min(limit, MAX_HISTORY_LIMIT))
        statement = select(UpdateJob).order_by(UpdateJob.created_at.desc()).limit(bounded)
        if host_id is not None:
            statement = statement.where(UpdateJob.host_id == host_id)
        if before is not None:
            statement = statement.where(UpdateJob.created_at < before)
        return list((await db.execute(statement)).scalars().all())

    async def events_for(self, db: AsyncSession, job_id: uuid.UUID) -> list[UpdateEvent]:
        """One job's transitions, oldest first — the order they happened.

        Ordered by the event's own sequence, not by ``created_at``:
        PostgreSQL's ``now()`` is the transaction timestamp, so several
        transitions written in one transaction share it. See
        :class:`~hcc.models.updates.UpdateEvent`.
        """
        return list(
            (
                await db.execute(
                    select(UpdateEvent).where(UpdateEvent.job_id == job_id).order_by(UpdateEvent.id)
                )
            )
            .scalars()
            .all()
        )

    # --- Policy -----------------------------------------------------------

    async def set_policy(
        self,
        db: AsyncSession,
        host_id: uuid.UUID,
        *,
        context: AuditContext,
        policy: Policy,
        channel: str,
        pinned_version: str | None = None,
        window: MaintenanceWindow | None = None,
    ) -> HostUpdatePolicy:
        """Set a host's update policy. Administrator only, and audited.

        Writes a row even when the values equal the defaults. That is
        deliberate: "this host was deliberately set to manual" and "this host
        has never been configured" are different facts, and an interface that
        cannot distinguish them cannot show which hosts somebody has reviewed.
        """
        await self._lock_host(db, host_id)
        host = (await db.execute(select(Host).where(Host.id == host_id))).scalar_one_or_none()
        if host is None:
            raise HostNotFoundError(host_id)

        if (policy is Policy.PINNED) != (pinned_version is not None):
            raise UpdateServiceError(
                "a pinned policy needs a version, and a version needs the pinned "
                "policy. Set both or neither."
            )

        row = await self.policy_for(db, host_id)
        if row is None:
            row = HostUpdatePolicy(host_id=host_id)
            db.add(row)

        row.policy = policy.value
        row.channel = channel
        row.pinned_version = pinned_version
        if window is None:
            row.window_timezone = None
            row.window_weekdays = None
            row.window_start_minute = None
            row.window_end_minute = None
        else:
            row.window_timezone = window.timezone
            row.window_weekdays = list(window.weekdays)
            row.window_start_minute = _time_to_minute(window.start)
            row.window_end_minute = _time_to_minute(window.end)

        self._audit.record(
            db,
            action=AuditAction.UPDATE_POLICY_CHANGED,
            context=context,
            target_host_id=host_id,
            details={
                "policy": policy.value,
                "channel": channel,
                "pinned_version": pinned_version,
                "window": window.describe() if window else None,
                # The fact somebody will want to find later: this is the moment
                # a host started updating itself.
                "automatic": policy.is_automatic,
            },
        )
        return row

    # --- Starting and stopping -------------------------------------------

    async def start_update(
        self,
        db: AsyncSession,
        host_id: uuid.UUID,
        *,
        manifest: ReleaseManifest | None,
        applied_schema_revisions: frozenset[str] = frozenset(),
        metadata_authentic: bool = False,
        trigger: UpdateTrigger,
        context: AuditContext | None = None,
        target_version: str | None = None,
        allow_downgrade: bool = False,
        rollout_id: uuid.UUID | None = None,
        now: datetime | None = None,
    ) -> UpdateJob:
        """Create a job and queue it.

        Args:
            target_version: what to install. ``None`` means "whatever the plan
                says", which is the normal case; naming a version is how an
                administrator installs something other than the newest, and it
                is checked against the plan rather than trusted.
            trigger: who decided. Manual decisions are audited; a policy's are
                recorded as events on the job, because an audit row per
                scheduler pass would bury the trail.

        Raises:
            UpdateAlreadyRunningError: the host has an open job.
            UpdateNotPossibleError: there is nothing to install, or something
                blocks it. Carries the plan, so the caller can say what.
        """
        moment = now or datetime.now(UTC)
        await self._lock_host(db, host_id)

        host = (await db.execute(select(Host).where(Host.id == host_id))).scalar_one_or_none()
        if host is None:
            raise HostNotFoundError(host_id)

        existing = await self.open_job(db, host_id)
        if existing is not None:
            raise UpdateAlreadyRunningError(existing)

        row = await self.policy_for(db, host_id)
        # The policy is not consulted here: `automatic` below derives from the
        # *trigger*, because a manual start on a host whose policy is `patch`
        # still has a human in the loop and should not be held to the
        # stricter automatic rules.
        _, channel, pin, _ = self.resolve_policy(row)
        plan = plan_update(
            manifest,
            installed_version=host.agent_version,
            host_platform=host.platform or "",
            channel=channel,
            pinned_version=target_version or pin,
            controller_version=self._controller_version,
            supported_protocol_versions=self._supported_protocol_versions,
            applied_schema_revisions=applied_schema_revisions,
            allow_downgrade=allow_downgrade,
            automatic=trigger is not UpdateTrigger.MANUAL,
            metadata_authentic=metadata_authentic,
        )

        if plan.blockers:
            raise UpdateNotPossibleError(
                plan,
                "this update cannot be installed: "
                + "; ".join(blocker.detail for blocker in plan.blockers),
            )
        if plan.target is None or plan.artifact is None:
            raise UpdateNotPossibleError(
                plan,
                plan.detail or f"there is nothing to install for this host ({plan.outcome.value})",
            )
        if not plan.has_update and plan.outcome is not Outcome.DOWNGRADE:
            raise UpdateNotPossibleError(
                plan, f"this host is already {plan.outcome.value}; there is nothing to do"
            )

        job = UpdateJob(
            host_id=host_id,
            state=UpdateState.AVAILABLE.value,
            trigger=trigger.value,
            requested_by_user_id=context.actor_user_id if context else None,
            rollout_id=rollout_id,
            from_version=host.agent_version,
            to_version=str(plan.target),
            platform=host.platform or "",
            expected_sha256=plan.artifact.sha256,
            expires_at=moment + self._instruction_ttl,
        )
        db.add(job)
        # Flushed so the partial unique index speaks now rather than at commit:
        # a caller that got a job object back and an integrity error later
        # would have to unwind work it had already reported as done.
        try:
            await db.flush()
        except IntegrityError as error:
            raise UpdateAlreadyRunningError(await self.open_job(db, host_id) or job) from error

        self._record_event(
            db,
            job,
            UpdateState.AVAILABLE,
            source="controller",
            detail=f"{plan.installed or 'unknown'} → {plan.target}, {plan.step} step",
        )
        await self._transition(
            db,
            job,
            UpdateState.QUEUED,
            source="controller",
            detail=f"queued by {trigger.value}",
        )

        if context is not None and trigger is UpdateTrigger.MANUAL:
            self._audit.record(
                db,
                action=AuditAction.UPDATE_STARTED,
                context=context,
                target_host_id=host_id,
                details={
                    "job_id": str(job.id),
                    "from_version": job.from_version,
                    "to_version": job.to_version,
                    "step": plan.step,
                },
            )
            if plan.outcome is Outcome.DOWNGRADE:
                # Separate, because "when did we last roll a host back" is a
                # question somebody will ask and an ordinary upgrade row
                # cannot answer it.
                self._audit.record(
                    db,
                    action=AuditAction.UPDATE_DOWNGRADE_AUTHORIZED,
                    context=context,
                    target_host_id=host_id,
                    details={
                        "job_id": str(job.id),
                        "from_version": job.from_version,
                        "to_version": job.to_version,
                    },
                )
        logger.info(
            "update_job_created",
            job_id=str(job.id),
            host_id=str(host_id),
            to_version=job.to_version,
            trigger=trigger.value,
        )
        return job

    async def cancel(
        self,
        db: AsyncSession,
        job_id: uuid.UUID,
        *,
        context: AuditContext,
        reason: str | None = None,
    ) -> UpdateJob:
        """Cancel an open job.

        Raises:
            UpdateNotCancellableError: it has reached a state where cancelling
                would be a promise the controller cannot keep.
        """
        job = (
            await db.execute(select(UpdateJob).where(UpdateJob.id == job_id))
        ).scalar_one_or_none()
        if job is None:
            raise UpdateNotFoundError(f"no update job {job_id}")
        await self._lock_host(db, job.host_id)

        if is_terminal(job.state):
            raise UpdateNotCancellableError(
                f"this update already finished as {job.state}; there is nothing to cancel"
            )
        if not can_transition(job.state, UpdateState.CANCELLED):
            raise UpdateNotCancellableError(
                f"this update is {job.state} — the binary is already being replaced on "
                "the host, so the controller cannot stop it. The agent rolls back by "
                "itself if the new version does not come up."
            )

        await self._transition(
            db,
            job,
            UpdateState.CANCELLED,
            source="controller",
            detail=reason or "cancelled by an administrator",
        )
        self._audit.record(
            db,
            action=AuditAction.UPDATE_CANCELLED,
            context=context,
            target_host_id=job.host_id,
            details={"job_id": str(job.id), "state_when_cancelled": job.state},
        )
        return job

    # --- Agent reports ----------------------------------------------------

    async def job_for_instruction(
        self, db: AsyncSession, host_id: uuid.UUID, instruction_id: uuid.UUID
    ) -> UpdateJob:
        """The job an agent is reporting about.

        Matched on **both** the host and the instruction. An agent cannot
        address another host's job even knowing its identifier, because the
        query will not find it — which makes impersonation a structural
        impossibility here rather than a check that could be forgotten.
        """
        job = (
            await db.execute(
                select(UpdateJob).where(
                    UpdateJob.instruction_id == instruction_id,
                    UpdateJob.host_id == host_id,
                )
            )
        ).scalar_one_or_none()
        if job is None:
            raise UpdateNotFoundError(f"no open update instruction {instruction_id} for this host")
        return job

    async def record_progress(
        self,
        db: AsyncSession,
        *,
        host_id: uuid.UUID,
        instruction_id: uuid.UUID,
        state: UpdateState,
        detail: str | None = None,
    ) -> UpdateJob:
        """Record a transition the agent reports.

        Validated against the transition table, so an agent cannot put a job
        into a state the machine does not allow — including skipping straight
        to ``succeeded``, which would be the useful lie if one were available.
        """
        job = await self.job_for_instruction(db, host_id, instruction_id)
        await self._lock_host(db, host_id)
        if not can_transition(job.state, state):
            raise InvalidTransitionError(
                f"an agent reported {state.value} for a job that is {job.state}, which "
                "the update state machine does not allow"
            )
        await self._transition(db, job, state, source="agent", detail=detail)
        return job

    async def record_result(
        self,
        db: AsyncSession,
        *,
        host_id: uuid.UUID,
        instruction_id: uuid.UUID,
        state: UpdateState,
        resulting_version: str | None = None,
        error: str | None = None,
    ) -> UpdateJob:
        """Record a terminal outcome the agent reports."""
        if state not in TERMINAL_STATES:
            raise InvalidTransitionError(
                f"{state.value} is not a terminal state; use record_progress"
            )
        job = await self.job_for_instruction(db, host_id, instruction_id)
        await self._lock_host(db, host_id)
        if not can_transition(job.state, state):
            raise InvalidTransitionError(
                f"an agent reported {state.value} for a job that is {job.state}, which "
                "the update state machine does not allow"
            )
        job.resulting_version = resulting_version
        if error is not None:
            job.error = error[:ERROR_MAX_LENGTH]
        await self._transition(db, job, state, source="agent", detail=error)
        return job

    async def reconcile_on_connect(
        self,
        db: AsyncSession,
        host_id: uuid.UUID,
        *,
        reported_version: str | None,
    ) -> UpdateJob | None:
        """Finish a job whose agent has come back.

        ## Why the controller has to do this

        **No process can report its own successful replacement.** The install
        ends with ``systemctl restart``, which terminates the agent that was
        performing it — so the states an agent can report stop at
        ``restarting``. Everything after that is the controller observing a
        reconnection.

        So: a job in ``restarting`` or ``verifying_health`` whose host
        reconnects reporting the target version **succeeded**. One reporting
        the old version has rolled back — the agent's own recovery put the
        previous binary in service, which is the designed outcome of a failed
        health check and is why ``rolled_back`` exists as a distinct state.

        Returns the job it concluded, or ``None`` when there was nothing to
        conclude — which is the common case, since most connections are not
        the far side of an update.
        """
        job = await self.open_job(db, host_id)
        if job is None or job.state not in {
            UpdateState.RESTARTING.value,
            UpdateState.VERIFYING_HEALTH.value,
        }:
            return None

        await self._lock_host(db, host_id)
        installed = try_parse(reported_version)
        target = try_parse(job.to_version)

        if installed is not None and target is not None and installed == target:
            job.resulting_version = reported_version
            await self._transition(
                db,
                job,
                UpdateState.SUCCEEDED,
                source="controller",
                detail=f"the agent reconnected reporting {reported_version}",
            )
            logger.info(
                "update_reconciled_succeeded",
                job_id=str(job.id),
                host_id=str(host_id),
                version=reported_version,
            )
            return job

        # It came back as something else. Rolled back if that something is
        # what it was running before, and failed if it is neither — a third
        # version means something happened that this system did not plan, and
        # saying "rolled back" would be a guess.
        job.resulting_version = reported_version
        if reported_version is not None and reported_version == job.from_version:
            job.error = (
                f"the agent reconnected still running {reported_version} rather than "
                f"{job.to_version}, so the update was rolled back. The agent's own "
                "health check is what restores the previous binary; its journal says "
                "why the new one did not come up."
            )
            state = UpdateState.ROLLED_BACK
        else:
            job.error = (
                f"the agent reconnected reporting {reported_version!r}, which is "
                f"neither the target {job.to_version} nor the version it was running "
                f"({job.from_version}). Something changed this host outside the "
                "update system."
            )
            state = UpdateState.FAILED
        await self._transition(
            db, job, state, source="controller", detail="reconciled on reconnection"
        )
        logger.warning(
            "update_reconciled_unsuccessful",
            job_id=str(job.id),
            host_id=str(host_id),
            state=state.value,
            reported=reported_version,
        )
        return job

    # --- Housekeeping -----------------------------------------------------

    async def run_policy_pass(
        self,
        db: AsyncSession,
        *,
        manifest: ReleaseManifest | None,
        applied_schema_revisions: frozenset[str] = frozenset(),
        metadata_authentic: bool = False,
        now: datetime | None = None,
    ) -> PolicyPassResult:
        """Evaluate every host's automatic policy once, and act on it.

        This is what makes an automatic policy automatic. Without it the
        policy table and :func:`hcc.updates.policy.decide` are a decision
        nothing asks for — which is exactly what they were until this existed,
        and an administrator who set a host to "automatic patch" would have
        watched nothing happen.

        ## What it does, in order

        1. **Expires stale instructions.** First, because an expired job holds
           the host's one open slot and nothing else can start until it is
           cleared. A pass that evaluated policies before expiring would skip
           every host whose previous attempt had timed out.
        2. **Walks enabled hosts** and their policies, one query.
        3. **Skips a host with an open job.** The schema enforces one open job
           per host, so starting another would be an `IntegrityError` rather
           than a decision — and a host already installing something does not
           need a second opinion.
        4. **Skips a host that has exhausted its attempts.** See
           :data:`MAX_POLICY_ATTEMPTS`.
        5. **Asks `decide`**, which is where the policy's step limit and the
           maintenance window live, and starts an update when it says to.

        ## Why failures do not stop the pass

        One host whose update cannot be started must not prevent the other
        ninety-nine from being considered. Each start is attempted
        independently and its failure is counted and logged; the pass's result
        says how many.

        ## Why there is no clock of its own

        `now` is a parameter, so a test can put a host inside and outside its
        maintenance window without waiting for Sunday. The caller in
        `hcc/app.py` passes nothing and gets the real clock.
        """
        moment = now or datetime.now(UTC)
        expired = await self.expire_stale(db, now=moment)
        if expired:
            # Committed before the policy walk, so the slots those jobs held
            # are actually free for the decisions below. Without this the
            # expiry and the start would be in one transaction and the
            # `open_job` query would still see the expired job as open.
            await db.commit()

        rows = (
            await db.execute(
                select(Host, HostUpdatePolicy)
                .outerjoin(HostUpdatePolicy, HostUpdatePolicy.host_id == Host.id)
                .where(Host.is_enabled.is_(True))
            )
        ).all()

        considered = 0
        started: list[UpdateJob] = []
        deferred = 0
        notified = 0
        skipped_open = 0
        exhausted = 0
        failed_to_start = 0

        for host, policy_row in rows:
            policy, channel, pin, window = self.resolve_policy(policy_row)
            if not policy.is_automatic:
                # Manual, notify-only and pinned hosts are not this pass's
                # business. `decide` would tell us the same thing; not asking
                # saves a plan per host on a fleet that is mostly manual,
                # which is the common configuration.
                continue
            considered += 1

            plan = plan_update(
                manifest,
                installed_version=host.agent_version,
                host_platform=host.platform or "",
                channel=channel,
                pinned_version=pin,
                controller_version=self._controller_version,
                supported_protocol_versions=self._supported_protocol_versions,
                applied_schema_revisions=applied_schema_revisions,
                automatic=True,
                metadata_authentic=metadata_authentic,
            )
            decision = decide(plan, policy=policy, window=window or ALWAYS, now=moment)

            if decision.action is Action.NOTHING:
                continue
            if decision.action is Action.NOTIFY:
                notified += 1
                continue
            if decision.action is Action.DEFER:
                # Counted, not recorded. A row per deferred host per pass would
                # be a table that grows with the product of the fleet size and
                # the interval, to say "still waiting for Sunday" — and the
                # interface already derives that from the policy and the
                # window.
                deferred += 1
                continue

            if await self.open_job(db, host.id) is not None:
                skipped_open += 1
                continue

            attempts = await self._policy_attempts(db, host.id, plan.target)
            if attempts >= MAX_POLICY_ATTEMPTS:
                exhausted += 1
                logger.info(
                    "update_policy_attempts_exhausted",
                    host_id=str(host.id),
                    to_version=str(plan.target),
                    attempts=attempts,
                    limit=MAX_POLICY_ATTEMPTS,
                )
                continue

            try:
                job = await self.start_update(
                    db,
                    host.id,
                    manifest=manifest,
                    applied_schema_revisions=applied_schema_revisions,
                    metadata_authentic=metadata_authentic,
                    trigger=UpdateTrigger.POLICY,
                    now=moment,
                )
            except (UpdateServiceError, IntegrityError):
                # Logged with its traceback and counted. A host whose update
                # cannot be started must not stop the other ninety-nine from
                # being considered, and the two realistic causes — a race with
                # an administrator starting one by hand, and a plan that
                # became unactionable between the decision and the start — are
                # both transient.
                await db.rollback()
                failed_to_start += 1
                logger.exception("update_policy_start_failed", host_id=str(host.id))
                continue

            await db.commit()
            started.append(job)
            logger.info(
                "update_started_by_policy",
                host_id=str(host.id),
                job_id=str(job.id),
                policy=policy.value,
                from_version=job.from_version,
                to_version=job.to_version,
                reason=decision.reason,
            )

        return PolicyPassResult(
            considered=considered,
            started=started,
            deferred=deferred,
            notified=notified,
            skipped_open=skipped_open,
            exhausted=exhausted,
            failed_to_start=failed_to_start,
            expired=expired,
        )

    async def _policy_attempts(
        self, db: AsyncSession, host_id: uuid.UUID, target: object | None
    ) -> int:
        """How many times a policy has already tried this version and failed.

        Counted per *target version*, not per host: a new release is a new
        chance, and a host that gave up on 0.2.0 should still take 0.2.1. The
        alternative — a per-host counter — would mean one bad release poisoned
        a host until somebody reset something.

        Manual attempts are not counted. An administrator retrying is a human
        who has decided to, and :data:`MAX_POLICY_ATTEMPTS` exists to bound
        automation rather than people.
        """
        if target is None:
            return 0
        return (
            await db.execute(
                select(func.count())
                .select_from(UpdateJob)
                .where(
                    UpdateJob.host_id == host_id,
                    UpdateJob.to_version == str(target),
                    UpdateJob.trigger == UpdateTrigger.POLICY.value,
                    UpdateJob.state.in_(
                        [
                            UpdateState.FAILED.value,
                            UpdateState.ROLLED_BACK.value,
                        ]
                    ),
                )
            )
        ).scalar_one()

    async def expire_stale(
        self, db: AsyncSession, *, now: datetime | None = None
    ) -> list[UpdateJob]:
        """Fail jobs whose instruction has expired without finishing.

        The mechanism that keeps an offline host from holding a slot forever.
        A job past its expiry that the agent never picked up is marked failed
        with a reason that says so — not deferred, because deferred means "the
        policy decided not now" and this is "nobody answered".
        """
        moment = now or datetime.now(UTC)
        stale = list(
            (
                await db.execute(
                    select(UpdateJob).where(
                        UpdateJob.state.in_([state.value for state in IN_FLIGHT_STATES]),
                        UpdateJob.expires_at < moment,
                    )
                )
            )
            .scalars()
            .all()
        )
        for job in stale:
            # Through the same transition path as everything else, so the event
            # log records the expiry rather than a state that changed silently.
            if can_transition(job.state, UpdateState.FAILED):
                job.error = (
                    f"the update instruction expired at {job.expires_at.isoformat()} "
                    f"while the job was {job.state}. The host may have been offline. "
                    "Starting a new update will re-plan against current metadata."
                )
                await self._transition(
                    db,
                    job,
                    UpdateState.FAILED,
                    source="controller",
                    detail="instruction expired",
                )
        if stale:
            logger.info("update_jobs_expired", count=len(stale))
        return stale

    # --- Internals --------------------------------------------------------

    async def _transition(
        self,
        db: AsyncSession,
        job: UpdateJob,
        state: UpdateState,
        *,
        source: str,
        detail: str | None = None,
    ) -> None:
        """Move a job and record the event. The only writer of ``state``."""
        if not can_transition(job.state, state):
            raise InvalidTransitionError(f"a job cannot go from {job.state} to {state.value}")
        job.state = state.value
        now = datetime.now(UTC)
        if state is UpdateState.DOWNLOADING and job.started_at is None:
            job.started_at = now
        if state in TERMINAL_STATES:
            job.finished_at = now
        self._record_event(db, job, state, source=source, detail=detail)

    def _record_event(
        self,
        db: AsyncSession,
        job: UpdateJob,
        state: UpdateState,
        *,
        source: str,
        detail: str | None = None,
    ) -> UpdateEvent:
        event = UpdateEvent(
            job_id=job.id,
            host_id=job.host_id,
            state=state.value,
            detail=detail[:ERROR_MAX_LENGTH] if detail else None,
            source=source,
        )
        db.add(event)
        return event

    async def _lock_host(self, db: AsyncSession, host_id: uuid.UUID) -> None:
        """Serialise against other writes to the same host.

        The same lock class the host and monitoring services use, deliberately:
        an update and a credential revocation on the same host must serialise
        against each other, and a second lock class would let them run
        concurrently — which is how a host gets an update instruction a moment
        after its credential was revoked.
        """
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:class, hashtext(:host_id))").bindparams(
                **{"class": HOST_LOCK_CLASS, "host_id": str(host_id)}
            )
        )


def _minute_to_time(minute: int) -> time:
    """Minutes after local midnight to a wall-clock time.

    The column stores minutes rather than a TIME, because the value is
    wall-clock in another zone and a TIME invites a driver or a query to attach
    an offset to it. These two functions are the only place the conversion
    happens.
    """
    return time(hour=minute // 60, minute=minute % 60)


def _time_to_minute(value: time) -> int:
    return value.hour * 60 + value.minute
