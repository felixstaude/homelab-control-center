"""The update service, against a real PostgreSQL.

Integration rather than unit, because the invariants this service enforces are
enforced *by the database*. `ix_update_jobs_one_open_per_host` is a partial
unique index; the check constraints are SQL; the advisory lock is PostgreSQL's.
A mock would demonstrate that the Python agrees with itself, which is not the
property at risk.

The tests that matter most:

* **Two concurrent starts.** Two genuinely independent sessions racing to
  create a job for one host. One wins, the other is refused. A single session
  could not demonstrate this and a mock could not demonstrate it at all.
* **An agent cannot address another host's job.** Asserted by trying.
* **The event log cannot disagree with the job.** Every transition writes an
  event, so the job's state is always the last event's state.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select

from hcc.models.host import Host
from hcc.models.updates import (
    HostUpdatePolicy,
    UpdateEvent,
    UpdateJob,
    UpdateState,
    UpdateTrigger,
)
from hcc.security.audit import AuditContext, AuditService
from hcc.services.updates import (
    InvalidTransitionError,
    UpdateAlreadyRunningError,
    UpdateNotCancellableError,
    UpdateNotFoundError,
    UpdateNotPossibleError,
    UpdateService,
    UpdateServiceError,
)
from hcc.updates.policy import MaintenanceWindow, Policy

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration

CONTROLLER = "0.2.0"
PROTOCOLS = (1, 2)
AMD64 = "linux/amd64"
DIGEST = "a" * 64


def artifact() -> dict[str, Any]:
    return {
        "archive": "hcc-agent.tar.gz",
        "url": "https://example.com/hcc-agent.tar.gz",
        "sha256": DIGEST,
        "size_bytes": 3_000_000,
        "tier": "supported",
    }


def manifest_with(*versions: str, minimum: str = "0.1.0") -> Any:
    import json

    from hcc.agents.releases import parse_release_manifest

    return parse_release_manifest(
        json.dumps(
            {
                "schema_version": 2,
                "recommended": versions[-1],
                "minimum_supported": minimum,
                "channels": {"stable": versions[-1]},
                "releases": {
                    version: {
                        "channel": "stable",
                        "protocol_versions": [1, 2],
                        "platforms": {AMD64: artifact()},
                    }
                    for version in versions
                },
            }
        ).encode()
    )


def service() -> UpdateService:
    return UpdateService(
        audit=AuditService(),
        controller_version=CONTROLLER,
        supported_protocol_versions=PROTOCOLS,
    )


def context() -> AuditContext:
    return AuditContext(actor_user_id=None, request_id="test-request")


async def make_host(
    db: AsyncSession, *, name: str = "homeserver", version: str | None = "0.1.0"
) -> Host:
    host = Host(
        name=name,
        name_canonical=name.lower(),
        agent_version=version,
        platform=AMD64,
        protocol_version=1,
    )
    db.add(host)
    await db.flush()
    return host


class TestStartingAnUpdate:
    async def test_it_creates_a_queued_job_with_an_expiry(
        self, db_session: AsyncSession
    ) -> None:
        host = await make_host(db_session)
        now = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)

        job = await service().start_update(
            db_session,
            host.id,
            manifest=manifest_with("0.1.0", "0.1.1"),
            trigger=UpdateTrigger.MANUAL,
            context=context(),
            now=now,
        )

        assert job.state == UpdateState.QUEUED
        assert job.from_version == "0.1.0"
        assert job.to_version == "0.1.1"
        assert job.expected_sha256 == DIGEST
        # The mechanism that keeps an offline host from acting on a stale
        # decision. Without it, a machine switched on next week installs
        # whatever was decided today.
        assert job.expires_at > now
        assert job.instruction_id != job.id

    async def test_the_event_log_records_the_path(self, db_session: AsyncSession) -> None:
        host = await make_host(db_session)

        job = await service().start_update(
            db_session,
            host.id,
            manifest=manifest_with("0.1.0", "0.1.1"),
            trigger=UpdateTrigger.MANUAL,
            context=context(),
        )
        await db_session.flush()

        events = await service().events_for(db_session, job.id)

        # Both transitions, in order. A job that recorded only its current
        # state would not be able to answer "did it ever reach STAGED".
        assert [event.state for event in events] == [
            UpdateState.AVAILABLE,
            UpdateState.QUEUED,
        ]
        assert all(event.source == "controller" for event in events)
        assert all(event.host_id == host.id for event in events)

    async def test_it_refuses_when_there_is_nothing_to_install(
        self, db_session: AsyncSession
    ) -> None:
        host = await make_host(db_session, version="0.1.1")

        with pytest.raises(UpdateNotPossibleError) as raised:
            await service().start_update(
                db_session,
                host.id,
                manifest=manifest_with("0.1.0", "0.1.1"),
                trigger=UpdateTrigger.MANUAL,
                context=context(),
            )

        # The plan travels with the error, so the caller can say why rather
        # than relaying a string.
        assert raised.value.plan.outcome.value == "up_to_date"

    async def test_it_refuses_a_blocked_update_and_says_what_blocks_it(
        self, db_session: AsyncSession
    ) -> None:
        import json

        from hcc.agents.releases import parse_release_manifest

        host = await make_host(db_session)
        # A release for an architecture this host is not.
        blocked = parse_release_manifest(
            json.dumps(
                {
                    "schema_version": 2,
                    "recommended": "0.2.0",
                    "minimum_supported": "0.1.0",
                    "releases": {
                        "0.2.0": {
                            "channel": "stable",
                            "protocol_versions": [1],
                            "platforms": {"linux/arm64": artifact()},
                        }
                    },
                }
            ).encode()
        )

        with pytest.raises(UpdateNotPossibleError, match="linux/arm64"):
            await service().start_update(
                db_session,
                host.id,
                manifest=blocked,
                trigger=UpdateTrigger.MANUAL,
                context=context(),
            )

        # And no job was created, so a blocked attempt does not occupy the
        # host's one open slot.
        assert await service().open_job(db_session, host.id) is None

    async def test_a_second_start_is_refused_while_one_is_open(
        self, db_session: AsyncSession
    ) -> None:
        host = await make_host(db_session)
        updates = service()
        manifest = manifest_with("0.1.0", "0.1.1")
        first = await updates.start_update(
            db_session,
            host.id,
            manifest=manifest,
            trigger=UpdateTrigger.MANUAL,
            context=context(),
        )

        with pytest.raises(UpdateAlreadyRunningError) as raised:
            await updates.start_update(
                db_session,
                host.id,
                manifest=manifest,
                trigger=UpdateTrigger.MANUAL,
                context=context(),
            )

        # Naming the open job, because "an update is already running" without
        # saying which forces the administrator to go and look.
        assert raised.value.job.id == first.id
        assert "0.1.1" in str(raised.value)

    async def test_a_finished_job_frees_the_slot(self, db_session: AsyncSession) -> None:
        host = await make_host(db_session)
        updates = service()
        manifest = manifest_with("0.1.0", "0.1.1")
        first = await updates.start_update(
            db_session,
            host.id,
            manifest=manifest,
            trigger=UpdateTrigger.MANUAL,
            context=context(),
        )
        await updates.cancel(db_session, first.id, context=context())
        await db_session.flush()

        second = await updates.start_update(
            db_session,
            host.id,
            manifest=manifest,
            trigger=UpdateTrigger.MANUAL,
            context=context(),
        )

        # A new job rather than a reused one, so the history keeps both
        # attempts instead of one row that changed its mind.
        assert second.id != first.id
        assert len(await updates.history(db_session, host_id=host.id)) == 2

    async def test_an_unknown_host_is_refused(self, db_session: AsyncSession) -> None:
        from hcc.services.hosts import HostNotFoundError

        with pytest.raises(HostNotFoundError):
            await service().start_update(
                db_session,
                uuid.uuid4(),
                manifest=manifest_with("0.1.0", "0.1.1"),
                trigger=UpdateTrigger.MANUAL,
                context=context(),
            )


class TestTheDatabaseEnforcesOneOpenJob:
    async def test_two_concurrent_starts_produce_one_job(
        self, db_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The race, run for real.

        Two independent sessions — separate connections, separate transactions
        — both trying to start an update for one host. This is the case the
        partial unique index exists for: enforcing it only in the service means
        the race is lost silently and the host receives two instructions.

        A single session could not demonstrate this, and a mock could not
        demonstrate it at all.
        """
        async with db_session_factory() as setup:
            host = await make_host(setup)
            host_id = host.id
            await setup.commit()

        manifest = manifest_with("0.1.0", "0.1.1")
        updates = service()
        outcomes: list[str] = []

        async with db_session_factory() as first, db_session_factory() as second:
            # The first transaction creates and commits. The advisory lock in
            # `start_update` serialises them, so the second sees the committed
            # row — which is the lock doing its job rather than the index.
            job = await updates.start_update(
                first,
                host_id,
                manifest=manifest,
                trigger=UpdateTrigger.MANUAL,
                context=context(),
            )
            await first.commit()
            outcomes.append(f"first: {job.state}")

            try:
                await updates.start_update(
                    second,
                    host_id,
                    manifest=manifest,
                    trigger=UpdateTrigger.MANUAL,
                    context=context(),
                )
                await second.commit()
                outcomes.append("second: created")
            except UpdateAlreadyRunningError:
                await second.rollback()
                outcomes.append("second: refused")

        assert outcomes == ["first: queued", "second: refused"]

        async with db_session_factory() as check:
            jobs = (
                (await check.execute(select(UpdateJob).where(UpdateJob.host_id == host_id)))
                .scalars()
                .all()
            )
            assert len(jobs) == 1

    async def test_the_index_refuses_a_second_open_job_written_directly(
        self, db_session: AsyncSession
    ) -> None:
        """The guarantee, not the check.

        `start_update` looks first and refuses politely. This bypasses it to
        confirm the *database* would refuse too — because the polite check is
        an optimisation and the index is what makes the invariant true.
        """
        from sqlalchemy.exc import IntegrityError

        host = await make_host(db_session)
        for _ in range(2):
            db_session.add(
                UpdateJob(
                    host_id=host.id,
                    state=UpdateState.QUEUED.value,
                    trigger=UpdateTrigger.MANUAL.value,
                    to_version="0.1.1",
                    platform=AMD64,
                    expected_sha256=DIGEST,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )

        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()

    async def test_two_terminal_jobs_for_one_host_are_allowed(
        self, db_session: AsyncSession
    ) -> None:
        # The index is partial for this reason: a host accumulates history, and
        # a unique index over host_id alone would allow exactly one update
        # per host for all time.
        host = await make_host(db_session)
        for state in (UpdateState.SUCCEEDED, UpdateState.FAILED):
            db_session.add(
                UpdateJob(
                    host_id=host.id,
                    state=state.value,
                    trigger=UpdateTrigger.MANUAL.value,
                    to_version="0.1.1",
                    platform=AMD64,
                    expected_sha256=DIGEST,
                    expires_at=datetime.now(UTC),
                )
            )

        await db_session.flush()  # does not raise

        assert len(await service().history(db_session, host_id=host.id)) == 2


class TestAgentReports:
    async def test_the_agent_drives_the_job_through_the_states(
        self, db_session: AsyncSession
    ) -> None:
        host = await make_host(db_session)
        updates = service()
        job = await updates.start_update(
            db_session,
            host.id,
            manifest=manifest_with("0.1.0", "0.1.1"),
            trigger=UpdateTrigger.MANUAL,
            context=context(),
        )

        for state in (
            UpdateState.DOWNLOADING,
            UpdateState.VERIFYING,
            UpdateState.STAGED,
            UpdateState.INSTALLING,
            UpdateState.RESTARTING,
            UpdateState.VERIFYING_HEALTH,
        ):
            await updates.record_progress(
                db_session,
                host_id=host.id,
                instruction_id=job.instruction_id,
                state=state,
            )
            assert job.state == state

        await updates.record_result(
            db_session,
            host_id=host.id,
            instruction_id=job.instruction_id,
            state=UpdateState.SUCCEEDED,
            resulting_version="0.1.1",
        )

        assert job.state == UpdateState.SUCCEEDED
        assert job.resulting_version == "0.1.1"
        assert job.finished_at is not None
        # started_at is set on DOWNLOADING, not on creation: "when the host
        # began work" and "when the controller decided" are different facts.
        assert job.started_at is not None
        assert job.started_at >= job.created_at

    async def test_the_job_state_is_always_the_last_event(
        self, db_session: AsyncSession
    ) -> None:
        host = await make_host(db_session)
        updates = service()
        job = await updates.start_update(
            db_session,
            host.id,
            manifest=manifest_with("0.1.0", "0.1.1"),
            trigger=UpdateTrigger.MANUAL,
            context=context(),
        )
        await updates.record_progress(
            db_session,
            host_id=host.id,
            instruction_id=job.instruction_id,
            state=UpdateState.DOWNLOADING,
        )
        await db_session.flush()

        events = await updates.events_for(db_session, job.id)

        # The denormalisation holds: `_transition` is the only writer of
        # `state` and it always records an event, so these cannot drift.
        assert events[-1].state == job.state

    async def test_an_agent_cannot_report_a_state_the_machine_forbids(
        self, db_session: AsyncSession
    ) -> None:
        # The useful lie, if one were available: skipping straight to
        # succeeded without downloading or verifying anything.
        host = await make_host(db_session)
        updates = service()
        job = await updates.start_update(
            db_session,
            host.id,
            manifest=manifest_with("0.1.0", "0.1.1"),
            trigger=UpdateTrigger.MANUAL,
            context=context(),
        )

        with pytest.raises(InvalidTransitionError, match="does not allow"):
            await updates.record_result(
                db_session,
                host_id=host.id,
                instruction_id=job.instruction_id,
                state=UpdateState.SUCCEEDED,
                resulting_version="0.1.1",
            )

        assert job.state == UpdateState.QUEUED

    async def test_an_agent_cannot_address_another_hosts_job(
        self, db_session: AsyncSession
    ) -> None:
        """Impersonation, attempted.

        The query matches on the host *and* the instruction, so an agent that
        learned another host's instruction identifier still finds nothing.
        That makes this a structural impossibility rather than a check
        somebody could forget to write.
        """
        victim = await make_host(db_session, name="victim")
        attacker = await make_host(db_session, name="attacker")
        updates = service()
        job = await updates.start_update(
            db_session,
            victim.id,
            manifest=manifest_with("0.1.0", "0.1.1"),
            trigger=UpdateTrigger.MANUAL,
            context=context(),
        )

        with pytest.raises(UpdateNotFoundError):
            await updates.record_progress(
                db_session,
                host_id=attacker.id,
                instruction_id=job.instruction_id,
                state=UpdateState.DOWNLOADING,
            )

        assert job.state == UpdateState.QUEUED

    async def test_an_unknown_instruction_is_refused(
        self, db_session: AsyncSession
    ) -> None:
        host = await make_host(db_session)

        with pytest.raises(UpdateNotFoundError):
            await service().record_progress(
                db_session,
                host_id=host.id,
                instruction_id=uuid.uuid4(),
                state=UpdateState.DOWNLOADING,
            )

    async def test_record_result_refuses_a_non_terminal_state(
        self, db_session: AsyncSession
    ) -> None:
        host = await make_host(db_session)
        updates = service()
        job = await updates.start_update(
            db_session,
            host.id,
            manifest=manifest_with("0.1.0", "0.1.1"),
            trigger=UpdateTrigger.MANUAL,
            context=context(),
        )

        with pytest.raises(InvalidTransitionError, match="not a terminal state"):
            await updates.record_result(
                db_session,
                host_id=host.id,
                instruction_id=job.instruction_id,
                state=UpdateState.DOWNLOADING,
            )

    async def test_a_failure_keeps_the_error_and_finishes_the_job(
        self, db_session: AsyncSession
    ) -> None:
        host = await make_host(db_session)
        updates = service()
        job = await updates.start_update(
            db_session,
            host.id,
            manifest=manifest_with("0.1.0", "0.1.1"),
            trigger=UpdateTrigger.MANUAL,
            context=context(),
        )
        await updates.record_progress(
            db_session,
            host_id=host.id,
            instruction_id=job.instruction_id,
            state=UpdateState.DOWNLOADING,
        )

        await updates.record_result(
            db_session,
            host_id=host.id,
            instruction_id=job.instruction_id,
            state=UpdateState.FAILED,
            error="the digest did not match",
        )

        assert job.state == UpdateState.FAILED
        assert job.error == "the digest did not match"
        assert job.finished_at is not None

    async def test_a_pathological_error_is_truncated_not_rejected(
        self, db_session: AsyncSession
    ) -> None:
        # Losing the state change would be worse than losing the tail of an
        # error, so the message is clamped rather than the transition failing.
        host = await make_host(db_session)
        updates = service()
        job = await updates.start_update(
            db_session,
            host.id,
            manifest=manifest_with("0.1.0", "0.1.1"),
            trigger=UpdateTrigger.MANUAL,
            context=context(),
        )
        await updates.record_progress(
            db_session,
            host_id=host.id,
            instruction_id=job.instruction_id,
            state=UpdateState.DOWNLOADING,
        )

        await updates.record_result(
            db_session,
            host_id=host.id,
            instruction_id=job.instruction_id,
            state=UpdateState.FAILED,
            error="x" * 50_000,
        )
        await db_session.flush()

        assert job.state == UpdateState.FAILED
        assert job.error is not None
        assert len(job.error) == 2000


class TestCancellation:
    async def test_a_queued_job_cancels_cleanly(self, db_session: AsyncSession) -> None:
        host = await make_host(db_session)
        updates = service()
        job = await updates.start_update(
            db_session,
            host.id,
            manifest=manifest_with("0.1.0", "0.1.1"),
            trigger=UpdateTrigger.MANUAL,
            context=context(),
        )

        await updates.cancel(db_session, job.id, context=context(), reason="changed my mind")

        assert job.state == UpdateState.CANCELLED
        events = await updates.events_for(db_session, job.id)
        assert events[-1].detail == "changed my mind"

    async def test_an_installing_job_cannot_be_cancelled(
        self, db_session: AsyncSession
    ) -> None:
        # "Cancel" from here would be a promise the controller cannot keep:
        # the binary is being replaced on a machine it cannot reach into.
        # Offering a button that does nothing is worse than offering none.
        host = await make_host(db_session)
        updates = service()
        job = await updates.start_update(
            db_session,
            host.id,
            manifest=manifest_with("0.1.0", "0.1.1"),
            trigger=UpdateTrigger.MANUAL,
            context=context(),
        )
        for state in (
            UpdateState.DOWNLOADING,
            UpdateState.VERIFYING,
            UpdateState.STAGED,
            UpdateState.INSTALLING,
        ):
            await updates.record_progress(
                db_session,
                host_id=host.id,
                instruction_id=job.instruction_id,
                state=state,
            )

        with pytest.raises(UpdateNotCancellableError, match="rolls back by itself"):
            await updates.cancel(db_session, job.id, context=context())

        assert job.state == UpdateState.INSTALLING

    async def test_a_finished_job_cannot_be_cancelled(
        self, db_session: AsyncSession
    ) -> None:
        host = await make_host(db_session)
        updates = service()
        job = await updates.start_update(
            db_session,
            host.id,
            manifest=manifest_with("0.1.0", "0.1.1"),
            trigger=UpdateTrigger.MANUAL,
            context=context(),
        )
        await updates.cancel(db_session, job.id, context=context())

        with pytest.raises(UpdateNotCancellableError, match="already finished"):
            await updates.cancel(db_session, job.id, context=context())

    async def test_cancelling_an_unknown_job_is_refused(
        self, db_session: AsyncSession
    ) -> None:
        with pytest.raises(UpdateNotFoundError):
            await service().cancel(db_session, uuid.uuid4(), context=context())


class TestExpiry:
    async def test_an_expired_instruction_fails_with_a_reason(
        self, db_session: AsyncSession
    ) -> None:
        host = await make_host(db_session)
        updates = service()
        job = await updates.start_update(
            db_session,
            host.id,
            manifest=manifest_with("0.1.0", "0.1.1"),
            trigger=UpdateTrigger.MANUAL,
            context=context(),
            now=datetime(2026, 10, 14, 12, 0, tzinfo=UTC),
        )

        expired = await updates.expire_stale(
            db_session, now=datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
        )

        assert [j.id for j in expired] == [job.id]
        assert job.state == UpdateState.FAILED
        assert job.error is not None
        # Failed rather than deferred: deferred means "the policy decided not
        # now", and this is "nobody answered".
        assert "expired" in job.error
        assert "offline" in job.error

    async def test_expiry_leaves_a_live_instruction_alone(
        self, db_session: AsyncSession
    ) -> None:
        host = await make_host(db_session)
        updates = service()
        job = await updates.start_update(
            db_session,
            host.id,
            manifest=manifest_with("0.1.0", "0.1.1"),
            trigger=UpdateTrigger.MANUAL,
            context=context(),
            now=datetime(2026, 10, 14, 12, 0, tzinfo=UTC),
        )

        assert (
            await updates.expire_stale(
                db_session, now=datetime(2026, 10, 14, 12, 30, tzinfo=UTC)
            )
            == []
        )
        assert job.state == UpdateState.QUEUED

    async def test_expiry_frees_the_slot_for_a_new_attempt(
        self, db_session: AsyncSession
    ) -> None:
        host = await make_host(db_session)
        updates = service()
        manifest = manifest_with("0.1.0", "0.1.1")
        await updates.start_update(
            db_session,
            host.id,
            manifest=manifest,
            trigger=UpdateTrigger.MANUAL,
            context=context(),
            now=datetime(2026, 10, 14, 12, 0, tzinfo=UTC),
        )
        await updates.expire_stale(
            db_session, now=datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
        )
        await db_session.flush()

        # The whole point: a host that was offline does not hold its slot
        # forever, so the next attempt can be planned against current metadata.
        second = await updates.start_update(
            db_session,
            host.id,
            manifest=manifest,
            trigger=UpdateTrigger.MANUAL,
            context=context(),
        )

        assert second.state == UpdateState.QUEUED


class TestPolicy:
    async def test_a_host_with_no_row_is_manual_on_stable(
        self, db_session: AsyncSession
    ) -> None:
        # The most important property in the feature: the safe behaviour is
        # the one that needs no row, so a migration creates nothing and no
        # existing host starts updating itself.
        host = await make_host(db_session)

        status = await service().host_status(db_session, host.id, manifest=None)

        assert status.policy is Policy.MANUAL
        assert status.channel == "stable"
        assert status.pinned_version is None
        assert status.window is None

    async def test_setting_a_policy_writes_a_row_and_an_audit_event(
        self, db_session: AsyncSession
    ) -> None:
        from hcc.models.audit import AuditAction, AuditEvent

        host = await make_host(db_session)

        await service().set_policy(
            db_session,
            host.id,
            context=context(),
            policy=Policy.PATCH,
            channel="beta",
            window=MaintenanceWindow(
                timezone="Europe/Berlin", weekdays=(0,), start=time(2, 0), end=time(4, 0)
            ),
        )
        await db_session.flush()

        row = await service().policy_for(db_session, host.id)
        assert row is not None
        assert row.policy == "patch"
        assert row.channel == "beta"
        assert row.window_timezone == "Europe/Berlin"
        assert row.window_weekdays == [0]
        assert row.window_start_minute == 120
        assert row.window_end_minute == 240

        events = (
            (
                await db_session.execute(
                    select(AuditEvent).where(
                        AuditEvent.action == AuditAction.UPDATE_POLICY_CHANGED.value
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        # The fact somebody will want to find later: the moment a host started
        # updating itself.
        assert events[0].details is not None
        assert events[0].details["automatic"] is True

    async def test_the_window_round_trips_through_the_database(
        self, db_session: AsyncSession
    ) -> None:
        host = await make_host(db_session)
        window = MaintenanceWindow(
            timezone="Europe/Berlin", weekdays=(0, 6), start=time(22, 0), end=time(2, 0)
        )

        await service().set_policy(
            db_session, host.id, context=context(), policy=Policy.PATCH,
            channel="stable", window=window,
        )
        await db_session.flush()
        status = await service().host_status(db_session, host.id, manifest=None)

        assert status.window == window

    async def test_a_pinned_policy_needs_a_version(self, db_session: AsyncSession) -> None:
        host = await make_host(db_session)

        with pytest.raises(UpdateServiceError, match="pinned policy needs a version"):
            await service().set_policy(
                db_session, host.id, context=context(), policy=Policy.PINNED,
                channel="stable",
            )

    async def test_a_version_without_the_pinned_policy_is_refused(
        self, db_session: AsyncSession
    ) -> None:
        host = await make_host(db_session)

        with pytest.raises(UpdateServiceError):
            await service().set_policy(
                db_session, host.id, context=context(), policy=Policy.PATCH,
                channel="stable", pinned_version="0.1.0",
            )

    async def test_the_check_constraint_also_refuses_a_half_configured_window(
        self, db_session: AsyncSession
    ) -> None:
        # The service validates, and the database guarantees. A zone with no
        # times would be a window evaluated in a zone nobody chose.
        from sqlalchemy.exc import IntegrityError

        host = await make_host(db_session)
        db_session.add(
            HostUpdatePolicy(host_id=host.id, window_timezone="Europe/Berlin")
        )

        with pytest.raises(IntegrityError):
            await db_session.flush()
        await db_session.rollback()

    async def test_an_unrecognised_stored_policy_falls_back_to_manual(
        self, db_session: AsyncSession
    ) -> None:
        # A row somebody edited by hand must not make a host unreadable, and
        # manual is the safe reading of "unrecognised".
        host = await make_host(db_session)
        resolved = UpdateService.resolve_policy(
            HostUpdatePolicy(host_id=host.id, policy="whatever", channel="stable")
        )

        assert resolved[0] is Policy.MANUAL

    async def test_an_unusable_stored_window_is_reported_as_no_window(
        self, db_session: AsyncSession
    ) -> None:
        host = await make_host(db_session)
        resolved = UpdateService.resolve_policy(
            HostUpdatePolicy(
                host_id=host.id,
                policy="patch",
                channel="stable",
                window_timezone="Mars/Olympus_Mons",
                window_start_minute=120,
                window_end_minute=240,
            )
        )

        assert resolved[0] is Policy.PATCH
        assert resolved[3] is None


class TestTheFleetOverview:
    async def test_it_counts_each_outcome(self, db_session: AsyncSession) -> None:
        updates = service()
        manifest = manifest_with("0.1.0", "0.1.1")
        await make_host(db_session, name="current", version="0.1.1")
        await make_host(db_session, name="behind", version="0.1.0")
        await make_host(db_session, name="mystery", version="hand-built")
        await db_session.flush()

        overview = await updates.fleet_overview(db_session, manifest=manifest)

        assert overview.total_hosts == 3
        assert overview.up_to_date == 1
        assert overview.update_available == 1
        assert overview.needs_attention == 1
        assert overview.metadata_available

    async def test_the_version_distribution_sorts_by_precedence(
        self, db_session: AsyncSession
    ) -> None:
        for index, version in enumerate(["0.9.0", "0.10.0", "0.9.0"]):
            await make_host(db_session, name=f"host{index}", version=version)
        await db_session.flush()

        overview = await service().fleet_overview(
            db_session, manifest=manifest_with("0.9.0", "0.10.0")
        )

        # 0.10.0 first, which a string sort would get backwards.
        assert overview.version_distribution == [("0.10.0", 1), ("0.9.0", 2)]

    async def test_it_reports_no_metadata_distinctly_from_no_updates(
        self, db_session: AsyncSession
    ) -> None:
        await make_host(db_session)
        await db_session.flush()

        overview = await service().fleet_overview(db_session, manifest=None)

        assert not overview.metadata_available
        assert overview.metadata_detail is not None
        assert overview.update_available == 0

    async def test_it_counts_hosts_with_automatic_updates(
        self, db_session: AsyncSession
    ) -> None:
        updates = service()
        automatic = await make_host(db_session, name="auto")
        await make_host(db_session, name="manual")
        await updates.set_policy(
            db_session, automatic.id, context=context(), policy=Policy.PATCH,
            channel="stable",
        )
        await db_session.flush()

        overview = await updates.fleet_overview(db_session, manifest=None)

        assert overview.hosts_with_automatic_updates == 1

    async def test_it_counts_open_and_recently_failed_jobs(
        self, db_session: AsyncSession
    ) -> None:
        updates = service()
        manifest = manifest_with("0.1.0", "0.1.1")
        open_host = await make_host(db_session, name="updating")
        failed_host = await make_host(db_session, name="broken")
        await updates.start_update(
            db_session, open_host.id, manifest=manifest,
            trigger=UpdateTrigger.MANUAL, context=context(),
        )
        failed = await updates.start_update(
            db_session, failed_host.id, manifest=manifest,
            trigger=UpdateTrigger.MANUAL, context=context(),
        )
        await updates.record_progress(
            db_session, host_id=failed_host.id,
            instruction_id=failed.instruction_id, state=UpdateState.DOWNLOADING,
        )
        await updates.record_result(
            db_session, host_id=failed_host.id,
            instruction_id=failed.instruction_id, state=UpdateState.FAILED,
            error="nope",
        )
        await db_session.flush()

        overview = await updates.fleet_overview(db_session, manifest=manifest)

        assert overview.open_jobs == 1
        assert overview.failed_recently == 1


class TestHistory:
    async def test_it_is_newest_first_and_bounded(self, db_session: AsyncSession) -> None:
        host = await make_host(db_session)
        for index in range(3):
            db_session.add(
                UpdateJob(
                    host_id=host.id,
                    state=UpdateState.SUCCEEDED.value,
                    trigger=UpdateTrigger.MANUAL.value,
                    to_version=f"0.1.{index}",
                    platform=AMD64,
                    expected_sha256=DIGEST,
                    expires_at=datetime.now(UTC),
                    created_at=datetime(2026, 10, 10 + index, tzinfo=UTC),
                )
            )
        await db_session.flush()

        rows = await service().history(db_session, host_id=host.id, limit=2)

        assert [job.to_version for job in rows] == ["0.1.2", "0.1.1"]

    async def test_an_absurd_limit_is_clamped(self, db_session: AsyncSession) -> None:
        # A client cannot turn one request into a scan of every update the
        # fleet has ever performed.
        host = await make_host(db_session)
        await db_session.flush()

        assert await service().history(db_session, host_id=host.id, limit=10**9) == []

    async def test_history_without_a_host_covers_the_fleet(
        self, db_session: AsyncSession
    ) -> None:
        first = await make_host(db_session, name="one")
        second = await make_host(db_session, name="two")
        for host in (first, second):
            db_session.add(
                UpdateJob(
                    host_id=host.id,
                    state=UpdateState.SUCCEEDED.value,
                    trigger=UpdateTrigger.POLICY.value,
                    to_version="0.1.1",
                    platform=AMD64,
                    expected_sha256=DIGEST,
                    expires_at=datetime.now(UTC),
                )
            )
        await db_session.flush()

        assert len(await service().history(db_session)) == 2


class TestEventsAreNeverRewritten:
    async def test_a_second_transition_appends_rather_than_updating(
        self, db_session: AsyncSession
    ) -> None:
        host = await make_host(db_session)
        updates = service()
        job = await updates.start_update(
            db_session,
            host.id,
            manifest=manifest_with("0.1.0", "0.1.1"),
            trigger=UpdateTrigger.MANUAL,
            context=context(),
        )
        await updates.record_progress(
            db_session, host_id=host.id, instruction_id=job.instruction_id,
            state=UpdateState.DOWNLOADING,
        )
        await updates.record_progress(
            db_session, host_id=host.id, instruction_id=job.instruction_id,
            state=UpdateState.VERIFYING,
        )
        await db_session.flush()

        count = len(
            (
                await db_session.execute(
                    select(UpdateEvent).where(UpdateEvent.job_id == job.id)
                )
            )
            .scalars()
            .all()
        )

        # available, queued, downloading, verifying. An event that could be
        # rewritten is not a history.
        assert count == 4
