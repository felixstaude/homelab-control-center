"""Monitoring configuration service tests against real PostgreSQL.

These cover what only a database can show:

* the ``NULL``-means-inherit columns behaving as three distinct states through
  a real round trip, not just in the resolver
* revision **idempotence** — the property the reconnect path depends on, and
  the one that fails if ``generated_at`` creeps into the comparison
* version monotonicity under the per-host advisory lock, with real concurrent
  connections
* the ``updated_at`` triggers that autogenerate cannot see
* acknowledgement and rejection matching on version *and* checksum
* the epoch guard on agent-reported configuration state

Schema comes from running the real Alembic migrations (the ``migrated_database``
fixture), so these also prove migration ``0007_monitoring_config`` and the
models agree.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select, text

from hcc.models import AuditAction, AuditEvent, Host
from hcc.models.monitoring import (
    MAX_PROBLEM_LENGTH,
    MAX_PROBLEMS,
    ConfigSyncStatus,
    GlobalModuleDefault,
    HostConfigRevision,
    HostModuleOverride,
    bound_problems,
)
from hcc.monitoring.canonical import CHECKSUM_PREFIX, checksum_of_bytes
from hcc.monitoring.modules import MODULE_IDS, UnknownModuleError
from hcc.monitoring.resolve import ValueSource
from hcc.monitoring.schedule import ScheduleError
from hcc.security.audit import AuditContext, AuditService
from hcc.services.monitoring import (
    ModuleSettingUpdate,
    MonitoringConfigService,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration

#: A module identifier that will never be real.
#:
#: These tests used ``"docker"`` as "a module the registry does not have",
#: until Phase G implemented it and three of them started asserting that a
#: module in the registry is not in the registry — in the integration suite,
#: which does not run without a database, so it was CI that noticed. A
#: placeholder has to be a name nobody would ever ship.
UNKNOWN_MODULE = "not_a_real_module"


CONTEXT = AuditContext(actor_user_id=uuid.uuid4(), request_id="test-request")


@pytest.fixture
def service() -> MonitoringConfigService:
    return MonitoringConfigService(audit=AuditService())


async def insert_host(db: AsyncSession, name: str = "homeserver") -> Host:
    host = Host(name=name)
    db.add(host)
    await db.flush()
    return host


async def connect_host(db: AsyncSession, host: Host) -> tuple[uuid.UUID, int]:
    """Give a host a current connection, so the epoch guard has something to match."""
    connection_id = uuid.uuid4()
    host.current_connection_id = connection_id
    host.connection_epoch = host.connection_epoch + 1
    await db.flush()
    return connection_id, host.connection_epoch


# ---------------------------------------------------------------------------
# Global defaults
# ---------------------------------------------------------------------------


async def test_a_fresh_installation_has_no_global_defaults(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    """Nothing is configured until an administrator configures it."""
    assert await service.global_defaults(db_session) == {}


async def test_a_fresh_host_resolves_every_module_off(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    """The product's central promise, through the real storage path."""
    host = await insert_host(db_session)
    effective = await service.effective(db_session, host.id)
    assert effective.enabled_module_ids == ()


async def test_a_global_default_round_trips(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    await service.set_global_default(
        db_session,
        module_id="cpu",
        update=ModuleSettingUpdate(
            enabled=True, schedule={"kind": "interval", "interval_seconds": 15}
        ),
        context=CONTEXT,
    )
    await db_session.commit()

    defaults = await service.global_defaults(db_session)
    assert defaults["cpu"].enabled is True
    assert defaults["cpu"].schedule is not None
    assert defaults["cpu"].schedule.interval_seconds == 15  # type: ignore[union-attr]


async def test_setting_only_the_interval_leaves_enabled_alone(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    """A partial update names only what it changes, and cannot reset the rest."""
    await service.set_global_default(
        db_session,
        module_id="cpu",
        update=ModuleSettingUpdate(enabled=True),
        context=CONTEXT,
    )
    await service.set_global_default(
        db_session,
        module_id="cpu",
        update=ModuleSettingUpdate(schedule={"kind": "interval", "interval_seconds": 20}),
        context=CONTEXT,
    )
    await db_session.commit()

    defaults = await service.global_defaults(db_session)
    assert defaults["cpu"].enabled is True
    assert defaults["cpu"].schedule.interval_seconds == 20  # type: ignore[union-attr]


async def test_an_explicit_null_clears_a_global_default_back_to_inherit(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    """``None`` is a value here, distinct from "not submitted"."""
    await service.set_global_default(
        db_session,
        module_id="cpu",
        update=ModuleSettingUpdate(enabled=True),
        context=CONTEXT,
    )
    await service.set_global_default(
        db_session, module_id="cpu", update=ModuleSettingUpdate(enabled=None), context=CONTEXT
    )
    await db_session.commit()

    row = (
        await db_session.execute(
            select(GlobalModuleDefault).where(GlobalModuleDefault.module_id == "cpu")
        )
    ).scalar_one()
    assert row.enabled is None


async def test_an_unknown_module_is_refused_before_anything_is_written(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    with pytest.raises(UnknownModuleError):
        await service.set_global_default(
            db_session,
            module_id=UNKNOWN_MODULE,
            update=ModuleSettingUpdate(enabled=True),
            context=CONTEXT,
        )
    assert (await db_session.execute(select(GlobalModuleDefault))).scalars().all() == []


async def test_an_invalid_schedule_leaves_no_row_behind(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    """Validated before anything is written, so a refused save is a no-op."""
    with pytest.raises(ScheduleError):
        await service.set_global_default(
            db_session,
            module_id="load",
            update=ModuleSettingUpdate(
                enabled=True, schedule={"kind": "interval", "interval_seconds": 1}
            ),
            context=CONTEXT,
        )
    await db_session.rollback()
    assert (await db_session.execute(select(GlobalModuleDefault))).scalars().all() == []


async def test_the_global_defaults_updated_at_trigger_fires(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    """Invisible to autogenerate (ADR-015), so it has to be tested explicitly.

    Through raw SQL, so the ORM cannot be what sets the column.
    """
    await service.set_global_default(
        db_session, module_id="cpu", update=ModuleSettingUpdate(enabled=True), context=CONTEXT
    )
    await db_session.commit()
    before: datetime = (
        await db_session.execute(
            text("SELECT updated_at FROM global_module_defaults WHERE module_id = 'cpu'")
        )
    ).scalar_one()

    await db_session.execute(
        text("UPDATE global_module_defaults SET enabled = false WHERE module_id = 'cpu'")
    )
    await db_session.commit()
    after: datetime = (
        await db_session.execute(
            text("SELECT updated_at FROM global_module_defaults WHERE module_id = 'cpu'")
        )
    ).scalar_one()
    assert after > before


async def test_a_no_op_update_does_not_move_updated_at(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    """``WHEN (OLD.* IS DISTINCT FROM NEW.*)``, so the column keeps meaning
    "when this row's data last actually changed".
    """
    await service.set_global_default(
        db_session, module_id="cpu", update=ModuleSettingUpdate(enabled=True), context=CONTEXT
    )
    await db_session.commit()
    before: datetime = (
        await db_session.execute(
            text("SELECT updated_at FROM global_module_defaults WHERE module_id = 'cpu'")
        )
    ).scalar_one()

    await db_session.execute(
        text("UPDATE global_module_defaults SET enabled = true WHERE module_id = 'cpu'")
    )
    await db_session.commit()
    after: datetime = (
        await db_session.execute(
            text("SELECT updated_at FROM global_module_defaults WHERE module_id = 'cpu'")
        )
    ).scalar_one()
    assert after == before


async def test_an_unknown_module_id_is_refused_by_the_database_too(
    db_session: AsyncSession,
) -> None:
    """Through raw SQL, so the ORM validator cannot absorb it first.

    The constraint is what makes the invariant true of the *data* rather than
    of one code path — a bulk UPDATE or a migration does not go through the ORM.
    """
    with pytest.raises(Exception, match="module_id_is_known"):
        await db_session.execute(
            text(
                "INSERT INTO global_module_defaults (module_id, enabled) "
                f"VALUES ('{UNKNOWN_MODULE}', true)"
            )
        )
    await db_session.rollback()


# ---------------------------------------------------------------------------
# Host overrides
# ---------------------------------------------------------------------------


async def test_a_host_override_beats_the_global_default_through_storage(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    host = await insert_host(db_session)
    await service.set_global_default(
        db_session, module_id="cpu", update=ModuleSettingUpdate(enabled=True), context=CONTEXT
    )
    await service.set_host_override(
        db_session,
        host_id=host.id,
        module_id="cpu",
        update=ModuleSettingUpdate(enabled=False),
        context=CONTEXT,
    )
    await db_session.commit()

    effective = await service.effective(db_session, host.id)
    cpu = effective.module("cpu")
    assert cpu is not None
    assert cpu.enabled.value is False
    assert cpu.enabled.source is ValueSource.HOST


async def test_a_reset_deletes_the_override_row(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    host = await insert_host(db_session)
    await service.set_host_override(
        db_session,
        host_id=host.id,
        module_id="cpu",
        update=ModuleSettingUpdate(enabled=True),
        context=CONTEXT,
    )
    await db_session.commit()
    assert (await db_session.execute(select(HostModuleOverride))).scalars().all()

    await service.reset_host_override(db_session, host_id=host.id, module_id="cpu", context=CONTEXT)
    await db_session.commit()
    assert (await db_session.execute(select(HostModuleOverride))).scalars().all() == []


async def test_a_reset_is_recorded_even_when_there_was_nothing_to_reset(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    """ "Reset a setting that was already inherited" is a thing an administrator
    did, and a trail that only records effective changes cannot answer "did
    anyone try?".
    """
    host = await insert_host(db_session)
    await service.reset_host_override(db_session, host_id=host.id, module_id="cpu", context=CONTEXT)
    await db_session.commit()

    event = (
        await db_session.execute(
            select(AuditEvent).where(
                AuditEvent.action == AuditAction.MONITORING_HOST_OVERRIDE_RESET.value
            )
        )
    ).scalar_one()
    assert event.details is not None
    assert event.details["had_override"] is False
    assert event.target_host_id == host.id


async def test_an_override_for_a_missing_host_is_refused(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    from hcc.services.hosts import HostNotFoundError

    with pytest.raises(HostNotFoundError):
        await service.set_host_override(
            db_session,
            host_id=uuid.uuid4(),
            module_id="cpu",
            update=ModuleSettingUpdate(enabled=True),
            context=CONTEXT,
        )


async def test_one_override_per_host_and_module_is_enforced_by_the_database(
    db_session: AsyncSession,
) -> None:
    """Two rows would make resolution depend on row order, which is not a thing
    that exists.
    """
    host = await insert_host(db_session)
    await db_session.commit()
    with pytest.raises(Exception, match="uq_host_module_overrides_host_module"):
        await db_session.execute(
            text(
                "INSERT INTO host_module_overrides (id, host_id, module_id, enabled) VALUES "
                "(gen_random_uuid(), :h, 'cpu', true), (gen_random_uuid(), :h, 'cpu', false)"
            ).bindparams(h=host.id)
        )
    await db_session.rollback()


async def test_deleting_a_host_removes_its_overrides_and_revisions(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    host = await insert_host(db_session)
    await service.set_host_override(
        db_session,
        host_id=host.id,
        module_id="cpu",
        update=ModuleSettingUpdate(enabled=True),
        context=CONTEXT,
    )
    await db_session.commit()
    assert (await db_session.execute(select(HostConfigRevision))).scalars().all()

    await db_session.execute(text("DELETE FROM hosts WHERE id = :h").bindparams(h=host.id))
    await db_session.commit()

    assert (await db_session.execute(select(HostModuleOverride))).scalars().all() == []
    assert (await db_session.execute(select(HostConfigRevision))).scalars().all() == []


# ---------------------------------------------------------------------------
# Revisions
# ---------------------------------------------------------------------------


async def test_the_first_revision_is_version_one(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    host = await insert_host(db_session)
    outcome = await service.ensure_revision(db_session, host.id, reason="initial configuration")
    await db_session.commit()
    assert outcome.created is True
    assert outcome.revision.version == 1


async def test_ensure_revision_is_idempotent(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    """The property the reconnect path depends on.

    Called twice with nothing changed in between, the second call must not mint
    a revision — otherwise a flapping link leaves a host permanently "pending"
    on a configuration identical to the one it is already running.
    """
    host = await insert_host(db_session)
    first = await service.ensure_revision(db_session, host.id, reason="initial configuration")
    await db_session.commit()
    second = await service.ensure_revision(db_session, host.id, reason="initial configuration")
    await db_session.commit()

    assert first.created is True
    assert second.created is False
    assert second.revision.version == first.revision.version
    assert second.revision.checksum == first.revision.checksum
    total = (await db_session.execute(select(HostConfigRevision))).scalars().all()
    assert len(total) == 1


async def test_idempotence_holds_across_a_later_wall_clock(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    """``generated_at`` differs, so a whole-document comparison would mint a
    revision. The content checksum is what makes this pass.
    """
    host = await insert_host(db_session)
    await service.ensure_revision(
        db_session, host.id, reason="initial configuration", now=datetime(2026, 1, 1, tzinfo=UTC)
    )
    await db_session.commit()
    later = await service.ensure_revision(
        db_session, host.id, reason="initial configuration", now=datetime(2026, 6, 1, tzinfo=UTC)
    )
    await db_session.commit()
    assert later.created is False


async def test_the_returned_revision_is_the_stored_one_when_nothing_changed(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    """Not a candidate with a bumped version that was never persisted — a
    caller would otherwise push a revision that does not exist.
    """
    host = await insert_host(db_session)
    first = await service.ensure_revision(db_session, host.id, reason="initial configuration")
    await db_session.commit()
    again = await service.ensure_revision(db_session, host.id, reason="initial configuration")

    stored = await service.latest_revision(db_session, host.id)
    assert stored is not None
    assert again.revision.version == stored.version
    assert again.revision.checksum == stored.checksum
    assert again.revision.document == stored.document.encode("utf-8")
    assert first.revision.checksum == stored.checksum


async def test_a_configuration_change_mints_the_next_version(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    host = await insert_host(db_session)
    await service.ensure_revision(db_session, host.id, reason="initial configuration")
    await db_session.commit()

    _, outcome = await service.set_host_override(
        db_session,
        host_id=host.id,
        module_id="cpu",
        update=ModuleSettingUpdate(enabled=True),
        context=CONTEXT,
    )
    await db_session.commit()
    assert outcome.created is True
    assert outcome.revision.version == 2


async def test_the_stored_document_reproduces_its_checksum(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    """ADR-050's whole point: ``text``, not ``jsonb``, so the bytes survive.

    A ``jsonb`` column would reorder keys and normalise numbers, and the digest
    would no longer verify — which would make "is this agent running what we
    think?" unanswerable.
    """
    host = await insert_host(db_session)
    await service.set_host_override(
        db_session,
        host_id=host.id,
        module_id="cpu",
        update=ModuleSettingUpdate(
            enabled=True, schedule={"kind": "interval", "interval_seconds": 7}
        ),
        context=CONTEXT,
    )
    await db_session.commit()

    stored = await service.latest_revision(db_session, host.id)
    assert stored is not None
    assert checksum_of_bytes(stored.document.encode("utf-8")) == stored.checksum
    assert stored.checksum.startswith(CHECKSUM_PREFIX)


async def test_the_stored_document_is_the_canonical_form(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    """Sorted keys and no whitespace, as stored — not merely as computed."""
    host = await insert_host(db_session)
    await service.ensure_revision(db_session, host.id, reason="initial configuration")
    await db_session.commit()
    stored = await service.latest_revision(db_session, host.id)
    assert stored is not None
    assert " " not in stored.document.replace('"generated_at"', "")
    document = json.loads(stored.document)
    assert list(document) == sorted(document)


async def test_a_revision_records_who_caused_it(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    host = await insert_host(db_session)
    await service.set_host_override(
        db_session,
        host_id=host.id,
        module_id="cpu",
        update=ModuleSettingUpdate(enabled=True),
        context=CONTEXT,
    )
    await db_session.commit()
    stored = await service.latest_revision(db_session, host.id)
    assert stored is not None
    assert stored.created_by_user_id == CONTEXT.actor_user_id
    assert stored.reason == "host override changed"


async def test_a_controller_generated_revision_has_no_actor(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    """The first revision a host gets on connecting: no operator asked for it
    individually, so attributing it to one would be a claim about a person who
    was not present.
    """
    host = await insert_host(db_session)
    await service.ensure_revision(db_session, host.id, reason="initial configuration")
    await db_session.commit()
    stored = await service.latest_revision(db_session, host.id)
    assert stored is not None
    assert stored.created_by_user_id is None


async def test_version_monotonicity_is_enforced_by_the_database(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    host = await insert_host(db_session)
    await service.ensure_revision(db_session, host.id, reason="initial configuration")
    await db_session.commit()

    with pytest.raises(Exception, match="uq_host_config_revisions_host_version"):
        await db_session.execute(
            text(
                "INSERT INTO host_config_revisions "
                "(id, host_id, version, checksum, content_checksum, document, generated_at) "
                "VALUES (gen_random_uuid(), :h, 1, :c, :c, '{}', now())"
            ).bindparams(h=host.id, c=CHECKSUM_PREFIX + "a" * 64)
        )
    await db_session.rollback()


async def test_a_bare_digest_is_refused_as_a_checksum(db_session: AsyncSession) -> None:
    """``char_length = 71``, so a future algorithm change is visible in stored
    data rather than implied.
    """
    host = await insert_host(db_session)
    await db_session.commit()
    with pytest.raises(Exception, match="checksum_is_prefixed_sha256"):
        await db_session.execute(
            text(
                "INSERT INTO host_config_revisions "
                "(id, host_id, version, checksum, content_checksum, document, generated_at) "
                "VALUES (gen_random_uuid(), :h, 1, :bare, :bare, '{}', now())"
            ).bindparams(h=host.id, bare="a" * 64)
        )
    await db_session.rollback()


async def test_a_revision_cannot_be_both_acknowledged_and_rejected(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    """They are the two outcomes of one decision, and a row with both would
    make "what is this agent running?" ambiguous.
    """
    host = await insert_host(db_session)
    await service.ensure_revision(db_session, host.id, reason="initial configuration")
    await db_session.commit()

    with pytest.raises(Exception, match="not_both_acknowledged_and_rejected"):
        await db_session.execute(
            text(
                "UPDATE host_config_revisions SET acknowledged_at = now(), rejected_at = now() "
                "WHERE host_id = :h"
            ).bindparams(h=host.id)
        )
    await db_session.rollback()


# ---------------------------------------------------------------------------
# Global changes fan out
# ---------------------------------------------------------------------------


async def test_a_global_change_regenerates_every_affected_host(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    hosts = [await insert_host(db_session, name=f"host-{index}") for index in range(3)]
    for host in hosts:
        await service.ensure_revision(db_session, host.id, reason="initial configuration")
    await db_session.commit()

    _, changed = await service.set_global_default(
        db_session, module_id="cpu", update=ModuleSettingUpdate(enabled=True), context=CONTEXT
    )
    await db_session.commit()

    # `(host_id, new_version)` pairs: the version is returned so the caller
    # can notify the agents without a query per host.
    assert sorted(host_id for host_id, _ in changed) == sorted(host.id for host in hosts)
    assert {version for _, version in changed} == {2}
    for host in hosts:
        latest = await service.latest_revision(db_session, host.id)
        assert latest is not None
        assert latest.version == 2
        assert json.loads(latest.document)["modules"]["cpu"]["enabled"] is True


async def test_a_host_that_overrides_the_module_is_not_reported_as_changed(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    """So the interface can say "12 of 30 hosts changed" rather than implying
    the whole fleet moved.
    """
    plain = await insert_host(db_session, name="plain")
    overridden = await insert_host(db_session, name="overridden")
    await service.set_host_override(
        db_session,
        host_id=overridden.id,
        module_id="cpu",
        update=ModuleSettingUpdate(enabled=False),
        context=CONTEXT,
    )
    await service.ensure_revision(db_session, plain.id, reason="initial configuration")
    await db_session.commit()

    _, changed = await service.set_global_default(
        db_session, module_id="cpu", update=ModuleSettingUpdate(enabled=True), context=CONTEXT
    )
    await db_session.commit()

    assert [host_id for host_id, _ in changed] == [plain.id]


async def test_a_global_save_that_changes_nothing_mints_no_revision(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    host = await insert_host(db_session)
    await service.set_global_default(
        db_session, module_id="cpu", update=ModuleSettingUpdate(enabled=True), context=CONTEXT
    )
    await db_session.commit()
    before = await service.latest_revision(db_session, host.id)
    assert before is not None

    _, changed = await service.set_global_default(
        db_session, module_id="cpu", update=ModuleSettingUpdate(enabled=True), context=CONTEXT
    )
    await db_session.commit()
    after = await service.latest_revision(db_session, host.id)
    assert after is not None
    assert changed == []
    assert after.version == before.version


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


async def test_concurrent_overrides_on_one_host_produce_sequential_versions(
    db_session_factory: async_sessionmaker[AsyncSession],
    service: MonitoringConfigService,
) -> None:
    """Real connections, real advisory lock.

    Two administrators enabling different modules on the same host at the same
    time must produce versions N and N+1. Without the per-host lock both read
    "latest is 1", both compute 2, and one request fails on the unique
    constraint — the right failure, but still a failure the operator sees.
    """
    async with db_session_factory() as setup:
        host = await insert_host(setup)
        await setup.commit()
        host_id = host.id

    async def enable(module_id: str) -> None:
        async with db_session_factory() as db:
            await service.set_host_override(
                db,
                host_id=host_id,
                module_id=module_id,
                update=ModuleSettingUpdate(enabled=True),
                context=CONTEXT,
            )
            await db.commit()

    await asyncio.gather(*(enable(module_id) for module_id in ("cpu", "memory", "swap", "load")))

    async with db_session_factory() as check:
        versions = (
            (
                await check.execute(
                    select(HostConfigRevision.version)
                    .where(HostConfigRevision.host_id == host_id)
                    .order_by(HostConfigRevision.version)
                )
            )
            .scalars()
            .all()
        )
        # Four saves, four distinct sequential versions, none lost.
        assert list(versions) == [1, 2, 3, 4]

        effective = await service.effective(check, host_id)
        assert set(effective.enabled_module_ids) == {"cpu", "memory", "swap", "load"}


async def test_concurrent_global_saves_do_not_deadlock(
    db_session_factory: async_sessionmaker[AsyncSession],
    service: MonitoringConfigService,
) -> None:
    """Hosts are locked in id order, so every transaction walks the same path.

    Locking in query order would deadlock here and PostgreSQL would kill one
    transaction, turning a settings save into an error nobody can explain.
    """
    async with db_session_factory() as setup:
        for index in range(5):
            await insert_host(setup, name=f"host-{index}")
        await setup.commit()

    async def save(module_id: str) -> None:
        async with db_session_factory() as db:
            await service.set_global_default(
                db,
                module_id=module_id,
                update=ModuleSettingUpdate(enabled=True),
                context=CONTEXT,
            )
            await db.commit()

    results = await asyncio.gather(
        *(save(module_id) for module_id in ("cpu", "memory", "swap")),
        return_exceptions=True,
    )
    failures = [result for result in results if isinstance(result, BaseException)]
    assert failures == []

    async with db_session_factory() as check:
        defaults = await service.global_defaults(check)
        assert {module_id for module_id, setting in defaults.items() if setting.enabled} == {
            "cpu",
            "memory",
            "swap",
        }


# ---------------------------------------------------------------------------
# Acknowledgement and rejection
# ---------------------------------------------------------------------------


async def test_an_acknowledgement_matching_version_and_checksum_is_recorded(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    host = await insert_host(db_session)
    outcome = await service.ensure_revision(db_session, host.id, reason="initial configuration")
    await db_session.commit()

    applied = await service.acknowledge(
        db_session,
        host_id=host.id,
        version=outcome.revision.version,
        checksum=outcome.revision.checksum,
    )
    await db_session.commit()
    assert applied is True
    stored = await service.latest_revision(db_session, host.id)
    assert stored is not None
    assert stored.acknowledged_at is not None


async def test_an_acknowledgement_with_the_wrong_checksum_is_refused(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    """A version alone would let an agent acknowledge revision 42 while running
    something else — a truncated document, a tampered one, or one from a
    different installation — and the controller would report it up to date.
    """
    host = await insert_host(db_session)
    outcome = await service.ensure_revision(db_session, host.id, reason="initial configuration")
    await db_session.commit()

    applied = await service.acknowledge(
        db_session,
        host_id=host.id,
        version=outcome.revision.version,
        checksum=CHECKSUM_PREFIX + "b" * 64,
    )
    await db_session.commit()
    assert applied is False
    stored = await service.latest_revision(db_session, host.id)
    assert stored is not None
    assert stored.acknowledged_at is None


async def test_an_acknowledgement_for_another_hosts_revision_does_nothing(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    """Cross-host isolation at the service layer, on top of the transport's."""
    mine = await insert_host(db_session, name="mine")
    theirs = await insert_host(db_session, name="theirs")
    outcome = await service.ensure_revision(db_session, theirs.id, reason="initial configuration")
    await db_session.commit()

    applied = await service.acknowledge(
        db_session,
        host_id=mine.id,
        version=outcome.revision.version,
        checksum=outcome.revision.checksum,
    )
    await db_session.commit()
    assert applied is False


@pytest.mark.parametrize("bad", ["", "a" * 64, "sha256:zz", CHECKSUM_PREFIX + "A" * 64])
async def test_a_malformed_checksum_is_refused_without_a_query(
    db_session: AsyncSession, service: MonitoringConfigService, bad: str
) -> None:
    host = await insert_host(db_session)
    await service.ensure_revision(db_session, host.id, reason="initial configuration")
    await db_session.commit()
    assert await service.acknowledge(db_session, host_id=host.id, version=1, checksum=bad) is False


async def test_a_rejection_records_every_problem(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    host = await insert_host(db_session)
    outcome = await service.ensure_revision(db_session, host.id, reason="initial configuration")
    await db_session.commit()

    recorded = await service.reject(
        db_session,
        host_id=host.id,
        version=outcome.revision.version,
        checksum=outcome.revision.checksum,
        problems=["cpu: interval below the host's floor", "swap: no swap configured"],
        context=CONTEXT,
    )
    await db_session.commit()
    assert recorded is True

    stored = await service.latest_revision(db_session, host.id)
    assert stored is not None
    assert stored.rejected_at is not None
    assert stored.rejection_problems == [
        "cpu: interval below the host's floor",
        "swap: no swap configured",
    ]

    event = (
        await db_session.execute(
            select(AuditEvent).where(
                AuditEvent.action == AuditAction.MONITORING_CONFIG_REJECTED.value
            )
        )
    ).scalar_one()
    assert event.target_host_id == host.id
    assert event.details is not None
    assert event.details["version"] == outcome.revision.version


async def test_an_acknowledgement_after_a_rejection_clears_it(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    """An agent that has now applied a revision it previously refused has
    superseded the refusal — commonly an upgraded agent, or a host where the
    operator fixed what the agent complained about.
    """
    host = await insert_host(db_session)
    outcome = await service.ensure_revision(db_session, host.id, reason="initial configuration")
    await db_session.commit()
    await service.reject(
        db_session,
        host_id=host.id,
        version=outcome.revision.version,
        checksum=outcome.revision.checksum,
        problems=["transient"],
        context=CONTEXT,
    )
    await db_session.commit()

    await service.acknowledge(
        db_session,
        host_id=host.id,
        version=outcome.revision.version,
        checksum=outcome.revision.checksum,
    )
    await db_session.commit()

    stored = await service.latest_revision(db_session, host.id)
    assert stored is not None
    assert stored.acknowledged_at is not None
    assert stored.rejected_at is None
    assert stored.rejection_problems is None


def test_problem_lists_are_bounded_in_count_and_length() -> None:
    """Text from a host whose security the controller does not own. An
    unbounded list of unbounded strings written on every reconnection is a
    table-growth attack that needs no exploit.
    """
    bounded = bound_problems(["x" * 5_000] * (MAX_PROBLEMS + 10))
    assert len(bounded) == MAX_PROBLEMS + 1
    assert all(len(problem) <= MAX_PROBLEM_LENGTH for problem in bounded[:MAX_PROBLEMS])
    # Truncation is marked rather than silent, so an operator is not sent
    # looking for a cause that was in the part discarded.
    assert "further problem(s), not recorded" in bounded[-1]


def test_a_short_problem_list_is_returned_unchanged() -> None:
    assert bound_problems(["one", "two"]) == ["one", "two"]


async def test_oversized_agent_problems_are_bounded_before_storage(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    host = await insert_host(db_session)
    outcome = await service.ensure_revision(db_session, host.id, reason="initial configuration")
    await db_session.commit()

    await service.reject(
        db_session,
        host_id=host.id,
        version=outcome.revision.version,
        checksum=outcome.revision.checksum,
        problems=["y" * 10_000] * 100,
        context=CONTEXT,
    )
    await db_session.commit()

    stored = await service.latest_revision(db_session, host.id)
    assert stored is not None
    assert stored.rejection_problems is not None
    assert len(stored.rejection_problems) == MAX_PROBLEMS + 1


# ---------------------------------------------------------------------------
# Agent-reported state and the epoch guard
# ---------------------------------------------------------------------------


async def test_reported_configuration_is_recorded(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    host = await insert_host(db_session)
    connection_id, epoch = await connect_host(db_session, host)
    outcome = await service.ensure_revision(db_session, host.id, reason="initial configuration")
    await db_session.commit()

    applied = await service.record_reported_config(
        db_session,
        host_id=host.id,
        connection_id=connection_id,
        epoch=epoch,
        version=outcome.revision.version,
        checksum=outcome.revision.checksum,
    )
    await db_session.commit()
    assert applied is True

    await db_session.refresh(host)
    assert host.reported_config_version == outcome.revision.version
    assert host.reported_config_checksum == outcome.revision.checksum
    assert host.reported_config_at is not None


async def test_a_superseded_connection_cannot_overwrite_reported_state(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    """The same epoch guard the heartbeat uses, for the same reason: a slow
    write from a socket that has been replaced must not clobber the newer
    connection's state.
    """
    host = await insert_host(db_session)
    stale_connection, stale_epoch = await connect_host(db_session, host)
    await connect_host(db_session, host)
    await db_session.commit()

    applied = await service.record_reported_config(
        db_session,
        host_id=host.id,
        connection_id=stale_connection,
        epoch=stale_epoch,
        version=1,
        checksum=CHECKSUM_PREFIX + "a" * 64,
    )
    await db_session.commit()
    assert applied is False
    await db_session.refresh(host)
    assert host.reported_config_version is None


async def test_a_malformed_reported_checksum_is_dropped_rather_than_stored(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    """An agent on a compromised host controls this string. A value that cannot
    be a checksum would otherwise sit in the column and make every later
    comparison fail for a reason nobody could see.
    """
    host = await insert_host(db_session)
    connection_id, epoch = await connect_host(db_session, host)
    await db_session.commit()

    applied = await service.record_reported_config(
        db_session,
        host_id=host.id,
        connection_id=connection_id,
        epoch=epoch,
        version=1,
        checksum="not-a-checksum",
    )
    await db_session.commit()
    assert applied is True
    await db_session.refresh(host)
    assert host.reported_config_version == 1
    assert host.reported_config_checksum is None


# ---------------------------------------------------------------------------
# Sync status
# ---------------------------------------------------------------------------


async def test_a_host_with_no_revision_reports_none(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    host = await insert_host(db_session)
    await db_session.commit()
    state = await service.sync_state(db_session, host)
    assert state.status is ConfigSyncStatus.NONE
    assert state.desired_version is None


async def test_an_unacknowledged_revision_is_pending_then_stale(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    host = await insert_host(db_session)
    generated = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    await service.ensure_revision(
        db_session, host.id, reason="initial configuration", now=generated
    )
    await db_session.commit()

    pending = await service.sync_state(db_session, host, now=generated + timedelta(seconds=30))
    assert pending.status is ConfigSyncStatus.PENDING

    stale = await service.sync_state(db_session, host, now=generated + timedelta(minutes=10))
    assert stale.status is ConfigSyncStatus.STALE


async def test_an_acknowledged_and_reported_revision_is_up_to_date(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    host = await insert_host(db_session)
    connection_id, epoch = await connect_host(db_session, host)
    outcome = await service.ensure_revision(db_session, host.id, reason="initial configuration")
    await service.acknowledge(
        db_session,
        host_id=host.id,
        version=outcome.revision.version,
        checksum=outcome.revision.checksum,
    )
    await service.record_reported_config(
        db_session,
        host_id=host.id,
        connection_id=connection_id,
        epoch=epoch,
        version=outcome.revision.version,
        checksum=outcome.revision.checksum,
    )
    await db_session.commit()
    await db_session.refresh(host)

    state = await service.sync_state(db_session, host)
    assert state.status is ConfigSyncStatus.UP_TO_DATE
    assert state.reported_version == outcome.revision.version


async def test_a_rejected_revision_reports_rejected_with_its_problems(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    host = await insert_host(db_session)
    outcome = await service.ensure_revision(db_session, host.id, reason="initial configuration")
    await service.reject(
        db_session,
        host_id=host.id,
        version=outcome.revision.version,
        checksum=outcome.revision.checksum,
        problems=["swap: this host has no swap"],
        context=CONTEXT,
    )
    await db_session.commit()

    state = await service.sync_state(db_session, host)
    assert state.status is ConfigSyncStatus.REJECTED
    assert state.rejection_problems == ("swap: this host has no swap",)


async def test_the_revision_history_is_newest_first_and_counted(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    host = await insert_host(db_session)
    for module_id in ("cpu", "memory", "swap"):
        await service.set_host_override(
            db_session,
            host_id=host.id,
            module_id=module_id,
            update=ModuleSettingUpdate(enabled=True),
            context=CONTEXT,
        )
    await db_session.commit()

    page, total = await service.revisions(db_session, host.id, limit=2)
    assert total == 3
    assert [revision.version for revision in page] == [3, 2]


async def test_every_module_can_be_enabled_and_lands_in_the_document(
    db_session: AsyncSession, service: MonitoringConfigService
) -> None:
    """All five, so a module that cannot actually be configured is a failure
    here rather than a surprise in the interface.
    """
    host = await insert_host(db_session)
    for module_id in MODULE_IDS:
        await service.set_host_override(
            db_session,
            host_id=host.id,
            module_id=module_id,
            update=ModuleSettingUpdate(enabled=True),
            context=CONTEXT,
        )
    await db_session.commit()

    latest = await service.latest_revision(db_session, host.id)
    assert latest is not None
    modules: dict[str, Any] = json.loads(latest.document)["modules"]
    assert all(modules[module_id]["enabled"] is True for module_id in MODULE_IDS)
