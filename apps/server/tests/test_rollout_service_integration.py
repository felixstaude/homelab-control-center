"""Staged rollouts, against a real database.

The behaviours here are the reason the feature exists, and most of them are
about **not** doing something: not starting the second batch until the canary
is healthy, not counting a powered-off host as a failure, not resuming past a
failure that an administrator has not looked at.

## Why these need a database

The whole engine is "read rows, decide, write rows". Its correctness is in
the queries — the partial unique index that makes two jobs for one host an
error, the advisory lock, the aggregate over members — and a fake session
would be asserting the behaviour of the fake.

## Time is a parameter

Every method takes ``now``, so a 120-second health settle and a 3600-second
offline deadline are arithmetic rather than a wait. Nothing here sleeps.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select

from hcc.models.host import Host
from hcc.models.monitoring import HostConfigRevision
from hcc.models.updates import (
    RolloutState,
    UpdateJob,
    UpdateRolloutMember,
    UpdateState,
    UpdateTrigger,
)
from hcc.security.audit import AuditContext, AuditService
from hcc.services.rollouts import (
    MAX_ROLLOUT_HOSTS,
    RolloutInvalidError,
    RolloutNotResumableError,
    RolloutService,
)
from hcc.services.updates import UpdateService
from hcc.updates.policy import MaintenanceWindow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

CONTROLLER = "0.2.0"
PROTOCOLS = (1, 2)
AMD64 = "linux/amd64"
DIGEST = "a" * 64
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


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
                        "platforms": {
                            AMD64: {
                                "archive": "hcc-agent.tar.gz",
                                "url": "https://example.com/hcc-agent.tar.gz",
                                "sha256": DIGEST,
                                "size_bytes": 3_000_000,
                                "tier": "supported",
                            }
                        },
                    }
                    for version in versions
                },
            }
        ).encode()
    )


MANIFEST = manifest_with("0.1.0", "0.2.0")


def rollouts() -> RolloutService:
    return RolloutService(
        UpdateService(
            audit=AuditService(),
            controller_version=CONTROLLER,
            supported_protocol_versions=PROTOCOLS,
        )
    )


def context() -> AuditContext:
    return AuditContext(actor_user_id=None, request_id="test-request")


async def make_host(
    db: AsyncSession,
    *,
    name: str,
    version: str | None = "0.1.0",
    connected: bool = True,
) -> Host:
    """A host the rollout engine will consider.

    ``connected`` is the only interesting variable: a rollout starts a member
    only when its host has a live connection, and a disconnected one is the
    case the offline deadline exists for.
    """
    host = Host(
        name=name,
        name_canonical=name.lower(),
        agent_version=version,
        platform=AMD64,
        protocol_version=2,
        current_connection_id=uuid.uuid4() if connected else None,
    )
    db.add(host)
    await db.flush()
    return host


async def run(db: AsyncSession, service: RolloutService, *, now: datetime = NOW) -> Any:
    return await service.run_pass(db, manifest=MANIFEST, metadata_authentic=True, now=now)


async def succeed(
    db: AsyncSession,
    host: Host,
    *,
    version: str = "0.2.0",
    at: datetime = NOW,
) -> None:
    """Drive a host's open job to ``succeeded``, as a real update would.

    Written out rather than reaching into the service, because the rollout
    engine reads the job's *state* and the host's *reported version*, and a
    test that set only one of those would be testing a state the system
    cannot reach.
    """
    job = (
        await db.execute(
            select(UpdateJob)
            .where(UpdateJob.host_id == host.id)
            .order_by(UpdateJob.created_at.desc())
            .limit(1)
        )
    ).scalar_one()
    job.state = UpdateState.SUCCEEDED.value
    job.resulting_version = version
    job.finished_at = at
    host.agent_version = version
    await db.flush()


async def fail(db: AsyncSession, host: Host, *, at: datetime = NOW) -> None:
    job = (
        await db.execute(
            select(UpdateJob)
            .where(UpdateJob.host_id == host.id)
            .order_by(UpdateJob.created_at.desc())
            .limit(1)
        )
    ).scalar_one()
    job.state = UpdateState.FAILED.value
    job.error = "the service would not restart"
    job.finished_at = at
    await db.flush()


async def member_of(
    db: AsyncSession, rollout_id: uuid.UUID, host_id: uuid.UUID
) -> UpdateRolloutMember:
    return (
        await db.execute(
            select(UpdateRolloutMember).where(
                UpdateRolloutMember.rollout_id == rollout_id,
                UpdateRolloutMember.host_id == host_id,
            )
        )
    ).scalar_one()


# ---------------------------------------------------------------------------


class TestCreating:
    async def test_nothing_starts_when_a_rollout_is_created(self, db_session: AsyncSession) -> None:
        """A created rollout is pending.

        This is what makes "create it now, let it run inside its window
        tonight" work, and what keeps the request that creates a forty-host
        rollout from performing forty installs synchronously.
        """
        hosts = [await make_host(db_session, name=f"host-{index}") for index in range(4)]

        rollout = await rollouts().create(
            db_session,
            name="to 0.2.0",
            to_version="0.2.0",
            host_ids=[host.id for host in hosts],
            context=context(),
        )

        assert rollout.state == RolloutState.PENDING
        jobs = (await db_session.execute(select(UpdateJob))).scalars().all()
        assert jobs == []

    async def test_the_canary_is_batch_zero_and_everyone_else_is_chunked(
        self, db_session: AsyncSession
    ) -> None:
        hosts = [await make_host(db_session, name=f"host-{index}") for index in range(7)]

        rollout = await rollouts().create(
            db_session,
            name="staged",
            to_version="0.2.0",
            host_ids=[host.id for host in hosts],
            batch_size=3,
            context=context(),
        )

        members = (
            (
                await db_session.execute(
                    select(UpdateRolloutMember).where(UpdateRolloutMember.rollout_id == rollout.id)
                )
            )
            .scalars()
            .all()
        )
        by_batch: dict[int, int] = {}
        for member in members:
            by_batch[member.batch] = by_batch.get(member.batch, 0) + 1
        # One canary, then 3 + 3 for the remaining six.
        assert by_batch == {0: 1, 1: 3, 2: 3}

    async def test_a_connected_host_is_preferred_for_the_canary(
        self, db_session: AsyncSession
    ) -> None:
        """A powered-off canary tells nobody anything.

        It would also hold the whole rollout at the offline deadline before a
        single real host was touched, which is the worst possible use of an
        hour.
        """
        offline = await make_host(db_session, name="aaa-offline", connected=False)
        online = await make_host(db_session, name="zzz-online", connected=True)

        rollout = await rollouts().create(
            db_session,
            name="staged",
            to_version="0.2.0",
            host_ids=[offline.id, online.id],
            context=context(),
        )

        canary = await member_of(db_session, rollout.id, online.id)
        assert canary.batch == 0
        assert (await member_of(db_session, rollout.id, offline.id)).batch == 1

    async def test_a_named_canary_wins_over_connectivity(self, db_session: AsyncSession) -> None:
        # An administrator who names a canary has a reason — it is the host
        # with the workload they care about — and the engine should not
        # second-guess it.
        first = await make_host(db_session, name="a", connected=True)
        second = await make_host(db_session, name="b", connected=True)

        rollout = await rollouts().create(
            db_session,
            name="staged",
            to_version="0.2.0",
            host_ids=[first.id, second.id],
            canary_host_ids=[second.id],
            context=context(),
        )

        assert (await member_of(db_session, rollout.id, second.id)).batch == 0

    @pytest.mark.parametrize(
        ("kwargs", "fragment"),
        [
            pytest.param({"host_ids": []}, "at least one host", id="no hosts"),
            pytest.param({"to_version": "latest"}, "semantic version", id="unparseable version"),
            pytest.param({"canary_count": 5}, "no batch after it", id="canary is everything"),
        ],
    )
    async def test_refusals(
        self, db_session: AsyncSession, kwargs: dict[str, Any], fragment: str
    ) -> None:
        hosts = [await make_host(db_session, name=f"host-{index}") for index in range(2)]
        arguments: dict[str, Any] = {
            "name": "staged",
            "to_version": "0.2.0",
            "host_ids": [host.id for host in hosts],
            "context": context(),
            **kwargs,
        }

        with pytest.raises(RolloutInvalidError, match=fragment):
            await rollouts().create(db_session, **arguments)

    async def test_a_canary_that_is_not_a_member_is_refused(self, db_session: AsyncSession) -> None:
        host = await make_host(db_session, name="member")
        stranger = await make_host(db_session, name="stranger")

        with pytest.raises(RolloutInvalidError, match="also be a member"):
            await rollouts().create(
                db_session,
                name="staged",
                to_version="0.2.0",
                host_ids=[host.id],
                canary_host_ids=[stranger.id],
                context=context(),
            )

    async def test_a_rollout_larger_than_the_cap_is_refused(self, db_session: AsyncSession) -> None:
        # Not a limit on a fleet but on one request: the members are written
        # in a single transaction.
        host = await make_host(db_session, name="only")

        with pytest.raises(RolloutInvalidError, match="at most"):
            await rollouts().create(
                db_session,
                name="staged",
                to_version="0.2.0",
                host_ids=[host.id] + [uuid.uuid4() for _ in range(MAX_ROLLOUT_HOSTS)],
                context=context(),
            )


class TestTheCanaryGate:
    """The feature. Everything else is a refinement of it."""

    async def test_only_the_canary_starts_first(self, db_session: AsyncSession) -> None:
        hosts = [await make_host(db_session, name=f"host-{index}") for index in range(4)]
        service = rollouts()
        rollout = await service.create(
            db_session,
            name="staged",
            to_version="0.2.0",
            host_ids=[host.id for host in hosts],
            context=context(),
        )

        await run(db_session, service)

        jobs = (await db_session.execute(select(UpdateJob))).scalars().all()
        assert len(jobs) == 1
        assert (await service.get(db_session, rollout.id)).state == RolloutState.CANARY

    async def test_the_next_batch_waits_for_the_canary_to_be_healthy(
        self, db_session: AsyncSession
    ) -> None:
        """The install succeeding is not enough.

        An agent that comes up, reports its version and then crashes a minute
        later has satisfied the job completely. The batch waits for the host
        to still be there after the settle.
        """
        hosts = [await make_host(db_session, name=f"host-{index}") for index in range(3)]
        service = rollouts()
        await service.create(
            db_session,
            name="staged",
            to_version="0.2.0",
            host_ids=[host.id for host in hosts],
            health_check_seconds=120,
            context=context(),
        )
        await run(db_session, service)

        # Which host got the canary is the engine's decision, so it is read
        # back rather than assumed.
        job = (await db_session.execute(select(UpdateJob))).scalars().one()
        canary = next(host for host in hosts if host.id == job.host_id)
        await succeed(db_session, canary, at=NOW)

        # One second later: the install is done and the settle is not.
        await run(db_session, service, now=NOW + timedelta(seconds=1))
        assert len((await db_session.execute(select(UpdateJob))).scalars().all()) == 1

        # Past the settle, and the host is still connected and reporting the
        # new version.
        await run(db_session, service, now=NOW + timedelta(seconds=200))
        assert len((await db_session.execute(select(UpdateJob))).scalars().all()) == 3

    async def test_a_canary_that_went_away_never_opens_the_gate(
        self, db_session: AsyncSession
    ) -> None:
        """The case the settle exists for.

        The job says succeeded — the agent did reconnect and report the new
        version — and then the host disconnected. Nothing further starts.
        """
        hosts = [await make_host(db_session, name=f"host-{index}") for index in range(3)]
        service = rollouts()
        await service.create(
            db_session,
            name="staged",
            to_version="0.2.0",
            host_ids=[host.id for host in hosts],
            health_check_seconds=120,
            context=context(),
        )
        await run(db_session, service)
        job = (await db_session.execute(select(UpdateJob))).scalars().one()
        canary = next(host for host in hosts if host.id == job.host_id)
        await succeed(db_session, canary, at=NOW)
        canary.current_connection_id = None
        await db_session.flush()

        await run(db_session, service, now=NOW + timedelta(seconds=600))

        assert len((await db_session.execute(select(UpdateJob))).scalars().all()) == 1

    async def test_a_canary_still_on_the_old_version_never_opens_the_gate(
        self, db_session: AsyncSession
    ) -> None:
        # A host that reconnected on the old binary has rolled back, whatever
        # its job says.
        hosts = [await make_host(db_session, name=f"host-{index}") for index in range(3)]
        service = rollouts()
        await service.create(
            db_session,
            name="staged",
            to_version="0.2.0",
            host_ids=[host.id for host in hosts],
            health_check_seconds=60,
            context=context(),
        )
        await run(db_session, service)
        job = (await db_session.execute(select(UpdateJob))).scalars().one()
        canary = next(host for host in hosts if host.id == job.host_id)
        await succeed(db_session, canary, version="0.1.0", at=NOW)

        await run(db_session, service, now=NOW + timedelta(seconds=600))

        assert len((await db_session.execute(select(UpdateJob))).scalars().all()) == 1

    async def test_an_unacknowledged_configuration_holds_the_gate(
        self, db_session: AsyncSession
    ) -> None:
        """The check that catches a half-working binary.

        An agent can start, connect and report a version while being unable
        to apply anything it is sent. Nothing above notices; this does.
        """
        hosts = [await make_host(db_session, name=f"host-{index}") for index in range(3)]
        service = rollouts()
        await service.create(
            db_session,
            name="staged",
            to_version="0.2.0",
            host_ids=[host.id for host in hosts],
            health_check_seconds=60,
            context=context(),
        )
        await run(db_session, service)
        job = (await db_session.execute(select(UpdateJob))).scalars().one()
        canary = next(host for host in hosts if host.id == job.host_id)
        await succeed(db_session, canary, at=NOW)
        db_session.add(
            HostConfigRevision(
                host_id=canary.id,
                version=7,
                document='{"version":1,"modules":{}}',
                checksum="sha256:" + "0" * 64,
                content_checksum="sha256:" + "1" * 64,
                generated_at=NOW,
            )
        )
        canary.reported_config_version = 6
        await db_session.flush()

        await run(db_session, service, now=NOW + timedelta(seconds=600))

        assert len((await db_session.execute(select(UpdateJob))).scalars().all()) == 1

        # And once it acknowledges, the gate opens.
        canary.reported_config_version = 7
        await db_session.flush()
        await run(db_session, service, now=NOW + timedelta(seconds=700))
        assert len((await db_session.execute(select(UpdateJob))).scalars().all()) == 3


class TestFailure:
    async def test_a_failed_canary_pauses_the_rollout(self, db_session: AsyncSession) -> None:
        hosts = [await make_host(db_session, name=f"host-{index}") for index in range(3)]
        service = rollouts()
        rollout = await service.create(
            db_session,
            name="staged",
            to_version="0.2.0",
            host_ids=[host.id for host in hosts],
            context=context(),
        )
        await run(db_session, service)
        job = (await db_session.execute(select(UpdateJob))).scalars().one()
        canary = next(host for host in hosts if host.id == job.host_id)
        await fail(db_session, canary)

        await run(db_session, service, now=NOW + timedelta(seconds=10))

        refreshed = await service.get(db_session, rollout.id)
        assert refreshed.state == RolloutState.PAUSED
        assert "tolerates 0" in (refreshed.paused_reason or "")
        assert len((await db_session.execute(select(UpdateJob))).scalars().all()) == 1

    async def test_resuming_without_an_override_pauses_again(
        self, db_session: AsyncSession
    ) -> None:
        """Otherwise "resume" would look broken.

        A rollout paused by the failure gate has a failure that is still
        there. Resuming without acknowledging it re-reaches the same
        conclusion on the next pass, which is correct — and would be baffling
        without this test saying so.
        """
        hosts = [await make_host(db_session, name=f"host-{index}") for index in range(3)]
        service = rollouts()
        rollout = await service.create(
            db_session,
            name="staged",
            to_version="0.2.0",
            host_ids=[host.id for host in hosts],
            context=context(),
        )
        await run(db_session, service)
        job = (await db_session.execute(select(UpdateJob))).scalars().one()
        await fail(db_session, next(h for h in hosts if h.id == job.host_id))
        await run(db_session, service, now=NOW + timedelta(seconds=10))

        await service.resume(db_session, rollout.id)
        await run(db_session, service, now=NOW + timedelta(seconds=20))

        assert (await service.get(db_session, rollout.id)).state == RolloutState.PAUSED

    async def test_an_explicit_override_lets_it_continue(self, db_session: AsyncSession) -> None:
        hosts = [await make_host(db_session, name=f"host-{index}") for index in range(3)]
        service = rollouts()
        rollout = await service.create(
            db_session,
            name="staged",
            to_version="0.2.0",
            host_ids=[host.id for host in hosts],
            health_check_seconds=0,
            context=context(),
        )
        await run(db_session, service)
        job = (await db_session.execute(select(UpdateJob))).scalars().one()
        await fail(db_session, next(h for h in hosts if h.id == job.host_id))
        await run(db_session, service, now=NOW + timedelta(seconds=10))

        await service.resume(db_session, rollout.id, override_failures=True)
        await run(db_session, service, now=NOW + timedelta(seconds=20))

        refreshed = await service.get(db_session, rollout.id)
        assert refreshed.failures_overridden is True
        assert refreshed.state == RolloutState.RUNNING
        assert len((await db_session.execute(select(UpdateJob))).scalars().all()) == 3

    async def test_resume_is_refused_on_a_rollout_that_is_not_paused(
        self, db_session: AsyncSession
    ) -> None:
        host = await make_host(db_session, name="only")
        service = rollouts()
        rollout = await service.create(
            db_session,
            name="staged",
            to_version="0.2.0",
            host_ids=[host.id],
            context=context(),
        )

        with pytest.raises(RolloutNotResumableError, match="not paused"):
            await service.resume(db_session, rollout.id)


class TestOfflineHosts:
    async def test_an_offline_host_is_not_started_and_not_failed(
        self, db_session: AsyncSession
    ) -> None:
        """It may come back. The deadline is what decides, not the first pass."""
        online = await make_host(db_session, name="online", connected=True)
        offline = await make_host(db_session, name="offline", connected=False)
        service = rollouts()
        rollout = await service.create(
            db_session,
            name="staged",
            to_version="0.2.0",
            host_ids=[online.id, offline.id],
            canary_host_ids=[online.id],
            health_check_seconds=0,
            context=context(),
        )
        await run(db_session, service)
        await succeed(db_session, online, at=NOW)

        await run(db_session, service, now=NOW + timedelta(seconds=30))

        member = await member_of(db_session, rollout.id, offline.id)
        assert member.job_id is None
        assert member.skipped_at is None
        assert (await service.get(db_session, rollout.id)).state != RolloutState.PAUSED

    async def test_past_the_deadline_it_is_skipped_rather_than_failed(
        self, db_session: AsyncSession
    ) -> None:
        """A powered-off NAS says nothing about the release.

        Counting it as a failure would stop a fleet rollout for a reason
        unrelated to the thing the gate is guarding.
        """
        online = await make_host(db_session, name="online", connected=True)
        offline = await make_host(db_session, name="offline", connected=False)
        service = rollouts()
        rollout = await service.create(
            db_session,
            name="staged",
            to_version="0.2.0",
            host_ids=[online.id, offline.id],
            canary_host_ids=[online.id],
            health_check_seconds=0,
            offline_deadline_seconds=600,
            context=context(),
        )
        await run(db_session, service)
        await succeed(db_session, online, at=NOW)
        await run(db_session, service, now=NOW + timedelta(seconds=30))

        await run(db_session, service, now=NOW + timedelta(seconds=1200))

        member = await member_of(db_session, rollout.id, offline.id)
        assert member.skipped_at is not None
        assert "did not come online" in (member.skipped_reason or "")
        refreshed = await service.get(db_session, rollout.id)
        # Skipped is not failed: the rollout finished rather than pausing.
        assert refreshed.state == RolloutState.COMPLETED

    async def test_a_host_already_on_the_target_version_is_skipped(
        self, db_session: AsyncSession
    ) -> None:
        # Not a failure of the release. Counting it as one would pause a
        # rollout for doing exactly what was asked.
        canary = await make_host(db_session, name="canary", version="0.1.0")
        already = await make_host(db_session, name="already", version="0.2.0")
        service = rollouts()
        rollout = await service.create(
            db_session,
            name="staged",
            to_version="0.2.0",
            host_ids=[canary.id, already.id],
            canary_host_ids=[canary.id],
            health_check_seconds=0,
            context=context(),
        )
        await run(db_session, service)
        await succeed(db_session, canary, at=NOW)

        await run(db_session, service, now=NOW + timedelta(seconds=30))

        member = await member_of(db_session, rollout.id, already.id)
        assert member.skipped_at is not None
        assert "already running 0.2.0" in (member.skipped_reason or "")
        # The batch finished inside the pass that started it, so the rollout
        # completes immediately rather than sitting on "running" for an
        # interval with nothing left to do.
        assert (await service.get(db_session, rollout.id)).state == RolloutState.COMPLETED


class TestMaintenanceWindows:
    async def test_nothing_starts_outside_the_window(self, db_session: AsyncSession) -> None:
        host = await make_host(db_session, name="only")
        service = rollouts()
        await service.create(
            db_session,
            name="nightly",
            to_version="0.2.0",
            host_ids=[host.id],
            window=MaintenanceWindow(
                timezone="UTC",
                start=datetime(2026, 1, 1, 2, 0).time(),
                end=datetime(2026, 1, 1, 4, 0).time(),
            ),
            context=context(),
        )

        # NOW is 12:00 UTC, which is outside 02:00-04:00.
        await run(db_session, service)

        assert (await db_session.execute(select(UpdateJob))).scalars().all() == []

    async def test_it_starts_inside_the_window(self, db_session: AsyncSession) -> None:
        host = await make_host(db_session, name="only")
        service = rollouts()
        await service.create(
            db_session,
            name="nightly",
            to_version="0.2.0",
            host_ids=[host.id],
            window=MaintenanceWindow(
                timezone="UTC",
                start=datetime(2026, 1, 1, 2, 0).time(),
                end=datetime(2026, 1, 1, 4, 0).time(),
            ),
            context=context(),
        )

        inside = NOW.replace(hour=3)
        await run(db_session, service, now=inside)

        assert len((await db_session.execute(select(UpdateJob))).scalars().all()) == 1

    async def test_waiting_for_a_window_is_not_a_skip(self, db_session: AsyncSession) -> None:
        # A host waiting for its window is not a host that failed, and must
        # not be written off by the offline deadline while it waits.
        host = await make_host(db_session, name="only")
        service = rollouts()
        rollout = await service.create(
            db_session,
            name="nightly",
            to_version="0.2.0",
            host_ids=[host.id],
            offline_deadline_seconds=60,
            window=MaintenanceWindow(
                timezone="UTC",
                start=datetime(2026, 1, 1, 2, 0).time(),
                end=datetime(2026, 1, 1, 4, 0).time(),
            ),
            context=context(),
        )

        await run(db_session, service, now=NOW + timedelta(hours=2))

        member = await member_of(db_session, rollout.id, host.id)
        assert member.skipped_at is None


class TestControl:
    async def test_pausing_stops_new_starts(self, db_session: AsyncSession) -> None:
        hosts = [await make_host(db_session, name=f"host-{index}") for index in range(3)]
        service = rollouts()
        rollout = await service.create(
            db_session,
            name="staged",
            to_version="0.2.0",
            host_ids=[host.id for host in hosts],
            health_check_seconds=0,
            context=context(),
        )
        await run(db_session, service)
        job = (await db_session.execute(select(UpdateJob))).scalars().one()
        await succeed(db_session, next(h for h in hosts if h.id == job.host_id), at=NOW)
        await service.pause(db_session, rollout.id, reason="looking at something")

        await run(db_session, service, now=NOW + timedelta(seconds=30))

        assert len((await db_session.execute(select(UpdateJob))).scalars().all()) == 1

    async def test_cancelling_leaves_unstarted_members_alone(
        self, db_session: AsyncSession
    ) -> None:
        hosts = [await make_host(db_session, name=f"host-{index}") for index in range(3)]
        service = rollouts()
        rollout = await service.create(
            db_session,
            name="staged",
            to_version="0.2.0",
            host_ids=[host.id for host in hosts],
            context=context(),
        )
        await run(db_session, service)

        await service.cancel(db_session, rollout.id, reason="changed my mind")
        await run(db_session, service, now=NOW + timedelta(seconds=30))

        refreshed = await service.get(db_session, rollout.id)
        assert refreshed.state == RolloutState.CANCELLED
        assert refreshed.finished_at is not None
        assert len((await db_session.execute(select(UpdateJob))).scalars().all()) == 1


class TestDurability:
    async def test_a_pass_reaches_the_same_conclusion_from_the_rows_alone(
        self, db_session: AsyncSession
    ) -> None:
        """What makes a controller restart a non-event.

        There is no in-memory rollout state to lose. A fresh service object —
        which is what a restarted controller has — reads the same rows and
        decides the same thing.
        """
        hosts = [await make_host(db_session, name=f"host-{index}") for index in range(3)]
        rollout = await rollouts().create(
            db_session,
            name="staged",
            to_version="0.2.0",
            host_ids=[host.id for host in hosts],
            health_check_seconds=0,
            context=context(),
        )
        await run(db_session, rollouts())
        job = (await db_session.execute(select(UpdateJob))).scalars().one()
        await succeed(db_session, next(h for h in hosts if h.id == job.host_id), at=NOW)

        # A different service instance entirely, as after a restart.
        await run(db_session, rollouts(), now=NOW + timedelta(seconds=30))

        assert len((await db_session.execute(select(UpdateJob))).scalars().all()) == 3
        assert (await rollouts().get(db_session, rollout.id)).state == RolloutState.RUNNING

    async def test_a_second_pass_does_not_start_a_host_twice(
        self, db_session: AsyncSession
    ) -> None:
        # The partial unique index makes a second job for one host an error
        # rather than a decision. The engine must not reach it.
        hosts = [await make_host(db_session, name=f"host-{index}") for index in range(2)]
        service = rollouts()
        await service.create(
            db_session,
            name="staged",
            to_version="0.2.0",
            host_ids=[host.id for host in hosts],
            context=context(),
        )

        await run(db_session, service)
        await run(db_session, service, now=NOW + timedelta(seconds=5))

        assert len((await db_session.execute(select(UpdateJob))).scalars().all()) == 1

    async def test_every_member_finishing_completes_the_rollout(
        self, db_session: AsyncSession
    ) -> None:
        hosts = [await make_host(db_session, name=f"host-{index}") for index in range(2)]
        service = rollouts()
        rollout = await service.create(
            db_session,
            name="staged",
            to_version="0.2.0",
            host_ids=[host.id for host in hosts],
            health_check_seconds=0,
            context=context(),
        )
        await run(db_session, service)
        job = (await db_session.execute(select(UpdateJob))).scalars().one()
        await succeed(db_session, next(h for h in hosts if h.id == job.host_id), at=NOW)
        await run(db_session, service, now=NOW + timedelta(seconds=10))
        second = (
            (await db_session.execute(select(UpdateJob).where(UpdateJob.id != job.id)))
            .scalars()
            .one()
        )
        await succeed(db_session, next(h for h in hosts if h.id == second.host_id), at=NOW)

        await run(db_session, service, now=NOW + timedelta(seconds=20))

        refreshed = await service.get(db_session, rollout.id)
        assert refreshed.state == RolloutState.COMPLETED
        assert refreshed.finished_at is not None

    async def test_jobs_are_attributed_to_the_rollout(self, db_session: AsyncSession) -> None:
        # Without this a rollout's history is unreadable: the jobs exist and
        # nothing connects them to the thing that asked for them.
        hosts = [await make_host(db_session, name=f"host-{index}") for index in range(2)]
        service = rollouts()
        rollout = await service.create(
            db_session,
            name="staged",
            to_version="0.2.0",
            host_ids=[host.id for host in hosts],
            context=context(),
        )

        await run(db_session, service)

        job = (await db_session.execute(select(UpdateJob))).scalars().one()
        assert job.rollout_id == rollout.id
        assert job.trigger == UpdateTrigger.ROLLOUT
