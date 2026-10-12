"""Reads and writes monitoring configuration, and mints revisions.

Phase 2. The one place that turns "an administrator enabled CPU monitoring on
this host" into a numbered, checksummed document an agent can verify.

## A revision is minted only when the content actually changes

:meth:`MonitoringConfigService.ensure_revision` is **idempotent**: it resolves
the effective configuration, compares its content checksum to the newest stored
revision's, and inserts only on a difference. Everything else in this module
calls it, which is what makes the following all safe to do unconditionally:

* saving a global default that happens to match what was already there;
* an agent reconnecting (which calls it, so a host whose configuration changed
  while it was offline is brought up to date without anyone touching it);
* an upgrade that changes the module registry.

Without that property, the reconnect path alone would mint a revision per
reconnection, and a flapping link would leave a host permanently "pending" on a
configuration identical to the one it is already running.

## Writes take the per-host advisory lock

Version monotonicity is a per-host invariant, so it uses the per-host lock
(ADR-044) rather than the installation-wide one. Two administrators saving
different modules on the same host at the same time must produce versions N
and N+1, not two attempts at N — and the unique constraint on
``(host_id, version)`` means getting that wrong is a failed request rather
than a silent overwrite, which is the right failure but still a failure.

A global-default change touches every host, so it takes every host's lock, in
a deterministic order. **Ordering is the whole point**: two concurrent global
saves that locked hosts in the order a query happened to return them would
deadlock, and PostgreSQL would resolve it by killing one — turning a settings
save into an error an operator cannot explain. Sorted by id, every transaction
walks the same path.

## Nothing here enables anything by itself

There is no method that turns a module on across the fleet as a side effect,
and no default that resolves to enabled. The only way ``enabled`` becomes true
is an explicit administrator call naming a module (§2.1).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, cast

from sqlalchemy import delete, func, select, text, update

from hcc.logging import get_logger
from hcc.models.audit import AuditAction, AuditOutcome
from hcc.models.host import Host
from hcc.models.monitoring import (
    REVISION_REASON_GLOBAL,
    REVISION_REASON_HOST,
    REVISION_REASON_INITIAL,
    REVISION_REASON_RESET,
    ConfigSyncStatus,
    GlobalModuleDefault,
    HostConfigRevision,
    HostModuleOverride,
    bound_problems,
    derive_config_status,
)
from hcc.monitoring import modules as registry
from hcc.monitoring.canonical import is_checksum
from hcc.monitoring.document import ConfigurationRevision, build_revision
from hcc.monitoring.resolve import (
    EffectiveConfiguration,
    ModuleSetting,
    resolve_host_configuration,
    validate_setting,
)
from hcc.services.hosts import HOST_LOCK_CLASS, HostNotFoundError

if TYPE_CHECKING:
    import uuid
    from collections.abc import Mapping, Sequence

    from sqlalchemy.engine import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession

    from hcc.monitoring.schedule import Schedule
    from hcc.security.audit import AuditContext, AuditService

logger = get_logger(__name__)


class Unset(enum.Enum):
    """Sentinel for "this field was not submitted".

    An enum singleton rather than ``object()`` because mypy narrows it, which
    forces the three cases of ``bool | None | Unset`` to be handled
    explicitly. Here the distinction is not cosmetic and is the mission's §5
    warning in one type: ``False`` means *off*, ``None`` means *inherit*, and
    absent means *leave this field alone*. Collapsing any two of them would
    make one of the three impossible to express.
    """

    UNSET = "unset"


UNSET: Final = Unset.UNSET

#: Most hosts one global-default change will regenerate revisions for in the
#: request that made it.
#:
#: Beyond this the work is left to each agent's next connection, which calls
#: :meth:`MonitoringConfigService.ensure_revision` anyway — so the
#: configuration still reaches every host, just not all within one HTTP
#: request. The bound exists because a settings save must not become a request
#: whose duration grows with the fleet; the measured cost per host is in
#: ``docs/PERFORMANCE.md``.
MAX_EAGER_REGENERATION: Final = 200


@dataclass(frozen=True, slots=True)
class ModuleSettingUpdate:
    """What one call changes about one module at one layer.

    Every field defaults to :data:`UNSET`, so a request that only changes the
    interval does not have to restate whether the module is enabled — and
    cannot accidentally reset it.
    """

    enabled: bool | Unset | None = UNSET
    #: A raw schedule mapping, ``None`` to inherit, or absent to leave alone.
    #: Raw because this is the boundary where it stops being raw: it is
    #: validated against the module's limits by :func:`validate_setting`.
    schedule: dict[str, Any] | Unset | None = UNSET

    @property
    def is_empty(self) -> bool:
        """Whether this update would change nothing."""
        return self.enabled is UNSET and self.schedule is UNSET


@dataclass(frozen=True, slots=True)
class RevisionOutcome:
    """A revision, and whether this call is what created it.

    The flag matters to callers that push: there is nothing to tell a
    connected agent about when the configuration did not change, and sending
    it anyway would make every reconnection look like a configuration event
    in the agent's log.
    """

    revision: ConfigurationRevision
    created: bool
    #: Adjustments the resolver made — a clamped interval, an unknown stored
    #: module. Not errors; the configuration is valid and in use.
    problems: tuple[str, ...]
    #: Which modules this revision enables.
    #:
    #: Returned rather than left to be derived from the document, because the
    #: agent connection needs it on the **ingestion** path — a telemetry batch
    #: is refused for a module nobody enabled — and re-reading the host's
    #: configuration per batch would be two queries per batch per host. The
    #: connection holds this set and replaces it whenever it sends a new
    #: revision, which is exactly when it can change.
    enabled_modules: frozenset[str]


@dataclass(frozen=True, slots=True)
class ConfigSyncState:
    """Everything the interface needs to describe one host's sync state."""

    status: ConfigSyncStatus
    desired_version: int | None
    desired_checksum: str | None
    reported_version: int | None
    reported_checksum: str | None
    acknowledged_at: datetime | None
    rejected_at: datetime | None
    rejection_problems: tuple[str, ...]
    generated_at: datetime | None


class MonitoringConfigService:
    """Resolves, stores and versions monitoring configuration.

    Stateless apart from its collaborators, so one instance is shared across
    requests.
    """

    __slots__ = ("_audit",)

    def __init__(self, *, audit: AuditService) -> None:
        self._audit = audit

    # --- Reading ----------------------------------------------------------

    async def global_defaults(self, db: AsyncSession) -> dict[str, ModuleSetting]:
        """Every configured global default, keyed by module.

        Modules with no row are simply absent; the resolver treats absent and
        all-null identically, which is correct — both mean "nothing has been
        said about this module globally".
        """
        rows = (await db.execute(select(GlobalModuleDefault))).scalars().all()
        return {row.module_id: _to_setting(row.enabled, row.schedule) for row in rows}

    async def host_overrides(
        self, db: AsyncSession, host_id: uuid.UUID
    ) -> dict[str, ModuleSetting]:
        """Every override on one host, keyed by module."""
        rows = (
            (
                await db.execute(
                    select(HostModuleOverride).where(HostModuleOverride.host_id == host_id)
                )
            )
            .scalars()
            .all()
        )
        return {row.module_id: _to_setting(row.enabled, row.schedule) for row in rows}

    async def effective(self, db: AsyncSession, host_id: uuid.UUID) -> EffectiveConfiguration:
        """The resolved configuration for one host.

        Two queries regardless of how many modules exist, because the
        resolution itself is pure
        (:func:`hcc.monitoring.resolve.resolve_host_configuration`) and takes
        both layers as mappings.
        """
        return resolve_host_configuration(
            global_defaults=await self.global_defaults(db),
            host_overrides=await self.host_overrides(db, host_id),
        )

    async def latest_revision(
        self, db: AsyncSession, host_id: uuid.UUID
    ) -> HostConfigRevision | None:
        """The newest revision for a host, or ``None`` if it has none.

        Ordered by ``version`` rather than by ``created_at``: the version is
        the thing that is monotonic by construction, and two revisions created
        inside the same transaction can share a timestamp.
        """
        return (
            await db.execute(
                select(HostConfigRevision)
                .where(HostConfigRevision.host_id == host_id)
                .order_by(HostConfigRevision.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def revisions(
        self, db: AsyncSession, host_id: uuid.UUID, *, limit: int, offset: int = 0
    ) -> tuple[Sequence[HostConfigRevision], int]:
        """A page of a host's revision history, newest first, and the total."""
        page = (
            (
                await db.execute(
                    select(HostConfigRevision)
                    .where(HostConfigRevision.host_id == host_id)
                    .order_by(HostConfigRevision.version.desc())
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        total = (
            await db.execute(
                select(func.count())
                .select_from(HostConfigRevision)
                .where(HostConfigRevision.host_id == host_id)
            )
        ).scalar_one()
        return page, total

    async def sync_state(
        self, db: AsyncSession, host: Host, *, now: datetime | None = None
    ) -> ConfigSyncState:
        """Describe where a host is between desired and applied configuration.

        ``now`` is a parameter so that a list of hosts is all judged against
        one instant — otherwise two hosts in the same response could be
        measured against timestamps milliseconds apart and a status could
        differ between two renderings of the same data. Same reason
        :meth:`hcc.models.host.Host.connectivity` takes one.
        """
        moment = now or datetime.now(UTC)
        latest = await self.latest_revision(db, host.id)
        return ConfigSyncState(
            status=derive_config_status(
                latest_version=latest.version if latest else None,
                latest_checksum=latest.checksum if latest else None,
                latest_generated_at=latest.generated_at if latest else None,
                latest_acknowledged_at=latest.acknowledged_at if latest else None,
                latest_rejected_at=latest.rejected_at if latest else None,
                reported_version=host.reported_config_version,
                reported_checksum=host.reported_config_checksum,
                now=moment,
            ),
            desired_version=latest.version if latest else None,
            desired_checksum=latest.checksum if latest else None,
            reported_version=host.reported_config_version,
            reported_checksum=host.reported_config_checksum,
            acknowledged_at=latest.acknowledged_at if latest else None,
            rejected_at=latest.rejected_at if latest else None,
            rejection_problems=tuple(latest.rejection_problems or ()) if latest else (),
            generated_at=latest.generated_at if latest else None,
        )

    # --- Minting revisions ------------------------------------------------

    async def ensure_revision(
        self,
        db: AsyncSession,
        host_id: uuid.UUID,
        *,
        reason: str,
        actor_user_id: uuid.UUID | None = None,
        now: datetime | None = None,
    ) -> RevisionOutcome:
        """Make sure the newest revision matches the current effective configuration.

        Idempotent: returns the existing revision with ``created=False`` when
        nothing has changed. See the module docstring for why that property is
        load-bearing rather than merely tidy.

        **Does not commit.** The caller does, together with whatever change
        prompted this — a revision committed separately from the override that
        caused it is a window in which the two disagree.

        Takes the host's advisory lock itself. ``pg_advisory_xact_lock`` is
        counted per transaction, so a caller that already holds it — every
        write path in this module does — pays one cheap no-op rather than
        deadlocking. That is worth the redundancy: the alternative was a
        documented precondition, and the agent-connection path is a caller
        outside this module which would have had to know about it.
        """
        moment = now or datetime.now(UTC)
        await self._lock_host(db, host_id)
        effective = await self.effective(db, host_id)
        latest = await self.latest_revision(db, host_id)

        candidate = build_revision(
            host_id=host_id,
            version=(latest.version + 1) if latest else 1,
            effective=effective,
            generated_at=moment,
        )

        if latest is not None and latest.content_checksum == candidate.content_checksum:
            # Nothing about what this host collects has changed. Return the
            # stored revision rather than the candidate: the stored one is
            # what the agent has, and handing back a document with a bumped
            # version that was never persisted would make a caller push a
            # revision that does not exist.
            return RevisionOutcome(
                revision=ConfigurationRevision(
                    host_id=host_id,
                    version=latest.version,
                    document=latest.document.encode("utf-8"),
                    checksum=latest.checksum,
                    content_checksum=latest.content_checksum,
                    generated_at=latest.generated_at,
                ),
                created=False,
                problems=effective.problems,
                enabled_modules=frozenset(effective.enabled_module_ids),
            )

        db.add(
            HostConfigRevision(
                host_id=host_id,
                version=candidate.version,
                checksum=candidate.checksum,
                content_checksum=candidate.content_checksum,
                document=candidate.text,
                generated_at=candidate.generated_at,
                created_by_user_id=actor_user_id,
                reason=reason,
            )
        )
        # Flushed so the unique (host_id, version) violation surfaces here,
        # attributable to this call, rather than at the caller's commit where
        # it would look like whatever else that transaction did.
        await db.flush()

        logger.info(
            "monitoring_revision_created",
            host_id=str(host_id),
            version=candidate.version,
            reason=reason,
            enabled_modules=list(effective.enabled_module_ids),
        )
        return RevisionOutcome(
            revision=candidate,
            created=True,
            problems=effective.problems,
            enabled_modules=frozenset(effective.enabled_module_ids),
        )

    # --- Writing ----------------------------------------------------------

    async def set_global_default(
        self,
        db: AsyncSession,
        *,
        module_id: str,
        update: ModuleSettingUpdate,
        context: AuditContext,
        now: datetime | None = None,
    ) -> tuple[ModuleSetting, list[tuple[uuid.UUID, int]]]:
        """Set or clear a global default, and regenerate affected hosts' revisions.

        Returns the stored setting and ``(host_id, new_version)`` for each host
        whose configuration changed — which is not every host: a host that
        overrides the module is unaffected, and saying so lets the interface
        report "12 of 30 hosts changed" rather than implying the whole fleet
        moved.

        The versions are returned rather than left to be looked up, because
        the caller's next act is to notify those agents and the notification
        carries a version. Re-reading each host's newest revision to find out
        what was just written would be a query per host for a fact this method
        already had.

        Raises:
            UnknownModuleError: the module is not in the registry.
            ScheduleError: the schedule is malformed or outside the module's
                limits, with every problem rather than the first.
        """
        moment = now or datetime.now(UTC)
        descriptor = registry.descriptor(module_id)

        row = (
            await db.execute(
                select(GlobalModuleDefault)
                .where(GlobalModuleDefault.module_id == module_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

        enabled = row.enabled if row else None
        schedule_raw = row.schedule if row else None
        if update.enabled is not UNSET:
            enabled = update.enabled
        if update.schedule is not UNSET:
            schedule_raw = update.schedule

        # Validated *before* anything is written, so a refused save leaves no
        # row behind — and validated against the module's own limits, which is
        # what makes "this module does not accept a calendar schedule" an
        # error the operator sees at save time rather than a configuration the
        # agent later rejects.
        setting = validate_setting(module_id, enabled=enabled, schedule=schedule_raw)

        if row is None:
            row = GlobalModuleDefault(module_id=module_id)
            db.add(row)
        row.enabled = enabled
        row.schedule = schedule_raw
        row.updated_by_user_id = context.actor_user_id
        await db.flush()

        self._audit.record(
            db,
            action=AuditAction.MONITORING_GLOBAL_DEFAULT_CHANGED,
            context=context,
            outcome=AuditOutcome.SUCCESS,
            details={
                "module": module_id,
                "module_name": descriptor.name,
                # The resolved values, so the trail says what the setting now
                # *is* rather than which fields the request happened to send.
                "enabled": enabled,
                "inherits_enabled": enabled is None,
                "schedule": schedule_raw,
                "inherits_schedule": schedule_raw is None,
            },
        )

        changed = await self._regenerate_all(
            db, reason=REVISION_REASON_GLOBAL, context=context, now=moment
        )
        return setting, changed

    async def set_host_override(
        self,
        db: AsyncSession,
        *,
        host_id: uuid.UUID,
        module_id: str,
        update: ModuleSettingUpdate,
        context: AuditContext,
        now: datetime | None = None,
    ) -> tuple[ModuleSetting, RevisionOutcome]:
        """Set or clear one module's override on one host.

        Raises:
            HostNotFoundError: no such host.
            UnknownModuleError: the module is not in the registry.
            ScheduleError: the schedule is malformed or outside the limits.
        """
        moment = now or datetime.now(UTC)
        descriptor = registry.descriptor(module_id)
        await self._lock_host(db, host_id)
        await self._require_host(db, host_id)

        row = (
            await db.execute(
                select(HostModuleOverride)
                .where(
                    HostModuleOverride.host_id == host_id,
                    HostModuleOverride.module_id == module_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

        enabled = row.enabled if row else None
        schedule_raw = row.schedule if row else None
        if update.enabled is not UNSET:
            enabled = update.enabled
        if update.schedule is not UNSET:
            schedule_raw = update.schedule

        setting = validate_setting(module_id, enabled=enabled, schedule=schedule_raw)

        if row is None:
            row = HostModuleOverride(host_id=host_id, module_id=module_id)
            db.add(row)
        row.enabled = enabled
        row.schedule = schedule_raw
        row.updated_by_user_id = context.actor_user_id
        await db.flush()

        self._audit.record(
            db,
            action=AuditAction.MONITORING_HOST_OVERRIDE_CHANGED,
            context=context,
            outcome=AuditOutcome.SUCCESS,
            target_host_id=host_id,
            details={
                "module": module_id,
                "module_name": descriptor.name,
                "enabled": enabled,
                "inherits_enabled": enabled is None,
                "schedule": schedule_raw,
                "inherits_schedule": schedule_raw is None,
            },
        )

        outcome = await self.ensure_revision(
            db,
            host_id,
            reason=REVISION_REASON_HOST,
            actor_user_id=context.actor_user_id,
            now=moment,
        )
        return setting, outcome

    async def reset_host_override(
        self,
        db: AsyncSession,
        *,
        host_id: uuid.UUID,
        module_id: str,
        context: AuditContext,
        now: datetime | None = None,
    ) -> RevisionOutcome:
        """Make a host inherit the global default for one module again.

        The override row is **deleted** rather than nulled. Both represent
        "inherit", and the earlier design kept the row so ``updated_at`` could
        answer "when was this reset?" — but the audit trail answers that
        better, with who did it as well, and a table of all-null rows is one
        where "has this host been customised?" needs a column-by-column check
        rather than a row count.

        Raises:
            HostNotFoundError: no such host.
            UnknownModuleError: the module is not in the registry.
        """
        moment = now or datetime.now(UTC)
        descriptor = registry.descriptor(module_id)
        await self._lock_host(db, host_id)
        await self._require_host(db, host_id)

        result = await db.execute(
            delete(HostModuleOverride).where(
                HostModuleOverride.host_id == host_id,
                HostModuleOverride.module_id == module_id,
            )
        )
        # `rowcount` is a CursorResult attribute; AsyncSession.execute is
        # typed as returning Result. Same cast as hcc/services/hosts.py.
        deleted = cast("CursorResult[Any]", result).rowcount or 0

        # Recorded even when there was nothing to delete. "Reset a setting
        # that was already inherited" is a thing an administrator did, and a
        # trail that only records effective changes cannot answer "did anyone
        # try to change this?" — the same reason `AuditOutcome` has three
        # members rather than two.
        self._audit.record(
            db,
            action=AuditAction.MONITORING_HOST_OVERRIDE_RESET,
            context=context,
            outcome=AuditOutcome.SUCCESS,
            target_host_id=host_id,
            details={
                "module": module_id,
                "module_name": descriptor.name,
                "had_override": bool(deleted),
            },
        )

        return await self.ensure_revision(
            db,
            host_id,
            reason=REVISION_REASON_RESET,
            actor_user_id=context.actor_user_id,
            now=moment,
        )

    async def _regenerate_all(
        self,
        db: AsyncSession,
        *,
        reason: str,
        context: AuditContext,
        now: datetime,
    ) -> list[tuple[uuid.UUID, int]]:
        """Regenerate revisions for every host, returning those that changed.

        Hosts are visited **in id order**, and :meth:`ensure_revision` takes
        each one's lock on entry — so every transaction walks the same path.
        That is the deadlock argument in the module docstring: two concurrent
        global saves locking in whatever order a query returned would
        deadlock, and PostgreSQL would resolve it by killing one of them.

        Bounded by :data:`MAX_EAGER_REGENERATION`. Past that the remaining
        hosts are left to pick the change up on their next connection, which
        calls :meth:`ensure_revision` anyway — so the configuration still
        reaches every host, and a settings save does not become a request
        whose duration grows with the fleet.
        """
        host_ids = list(
            (await db.execute(select(Host.id).order_by(Host.id).limit(MAX_EAGER_REGENERATION + 1)))
            .scalars()
            .all()
        )
        deferred = max(0, len(host_ids) - MAX_EAGER_REGENERATION)
        if deferred:
            host_ids = host_ids[:MAX_EAGER_REGENERATION]
            logger.info(
                "monitoring_regeneration_deferred",
                eager=len(host_ids),
                deferred=deferred,
                reason=reason,
            )

        changed: list[tuple[uuid.UUID, int]] = []
        for host_id in host_ids:
            outcome = await self.ensure_revision(
                db,
                host_id,
                reason=reason,
                actor_user_id=context.actor_user_id,
                now=now,
            )
            if outcome.created:
                changed.append((host_id, outcome.revision.version))
        return changed

    # --- Agent-reported state ---------------------------------------------

    async def record_reported_config(
        self,
        db: AsyncSession,
        *,
        host_id: uuid.UUID,
        connection_id: uuid.UUID,
        epoch: int,
        version: int | None,
        checksum: str | None,
        now: datetime | None = None,
    ) -> bool:
        """Record what an agent says it is running. Returns whether it applied.

        Guarded by the same connection id and epoch as the heartbeat, for the
        same reason: a slow write from a superseded socket must not overwrite
        a newer connection's state. A ``False`` return means this connection
        is no longer the host's current one.

        ``checksum`` is shape-checked before storage. An agent on a
        compromised host controls this string, and the column is bounded, so a
        malformed value is dropped rather than stored — a checksum that cannot
        be a checksum would otherwise sit in the column and make every later
        comparison fail for a reason nobody could see.
        """
        moment = now or datetime.now(UTC)
        safe_checksum = checksum if (checksum is None or is_checksum(checksum)) else None
        if checksum is not None and safe_checksum is None:
            logger.warning(
                "monitoring_reported_checksum_malformed",
                host_id=str(host_id),
                length=len(checksum),
            )

        result = await db.execute(
            update(Host)
            .where(
                Host.id == host_id,
                Host.current_connection_id == connection_id,
                Host.connection_epoch == epoch,
            )
            .values(
                reported_config_version=version,
                reported_config_checksum=safe_checksum,
                reported_config_at=moment,
            )
        )
        return bool(cast("CursorResult[Any]", result).rowcount)

    async def acknowledge(
        self,
        db: AsyncSession,
        *,
        host_id: uuid.UUID,
        version: int,
        checksum: str,
        now: datetime | None = None,
    ) -> bool:
        """Mark a revision applied. Returns whether a revision matched.

        Matched on **version and checksum together**. A version alone would let
        an agent acknowledge revision 42 while running something else — a
        truncated document, a tampered one, or one from a different
        installation — and the controller would report it up to date
        (``AGENT_ARCHITECTURE.md`` §6.3). The checksum is what makes the
        acknowledgement mean "I applied these bytes".

        ``rejected_at`` is cleared, because an agent that has now applied a
        revision it previously refused has superseded the refusal — commonly a
        host where the operator fixed the thing the agent complained about,
        or an agent that was upgraded. The CHECK constraint forbids both being
        set, so this is not optional.
        """
        moment = now or datetime.now(UTC)
        if not is_checksum(checksum):
            logger.warning(
                "monitoring_ack_checksum_malformed", host_id=str(host_id), version=version
            )
            return False
        result = await db.execute(
            update(HostConfigRevision)
            .where(
                HostConfigRevision.host_id == host_id,
                HostConfigRevision.version == version,
                HostConfigRevision.checksum == checksum,
            )
            .values(acknowledged_at=moment, rejected_at=None, rejection_problems=None)
        )
        if cast("CursorResult[Any]", result).rowcount:
            logger.info("monitoring_config_acknowledged", host_id=str(host_id), version=version)
            return True
        logger.warning(
            "monitoring_ack_unmatched",
            host_id=str(host_id),
            version=version,
            # The checksum is not logged: it is the lookup key for a revision,
            # and while not secret, a log that confirms which digests exist is
            # a log that answers questions nobody should need to ask it.
        )
        return False

    async def reject(
        self,
        db: AsyncSession,
        *,
        host_id: uuid.UUID,
        version: int,
        checksum: str,
        problems: Sequence[str],
        context: AuditContext,
        now: datetime | None = None,
    ) -> bool:
        """Record that an agent refused a revision, and why.

        Returns whether a revision matched. Matched on version and checksum
        together, for the reason :meth:`acknowledge` gives.

        The problems are bounded before storage
        (:func:`hcc.models.monitoring.bound_problems`): they are text from a
        host whose security the controller does not own, and an unbounded list
        of unbounded strings written on every reconnection is a table-growth
        attack that needs no exploit.
        """
        moment = now or datetime.now(UTC)
        if not is_checksum(checksum):
            logger.warning(
                "monitoring_nack_checksum_malformed", host_id=str(host_id), version=version
            )
            return False

        bounded = bound_problems(list(problems))
        result = await db.execute(
            update(HostConfigRevision)
            .where(
                HostConfigRevision.host_id == host_id,
                HostConfigRevision.version == version,
                HostConfigRevision.checksum == checksum,
            )
            .values(rejected_at=moment, rejection_problems=bounded, acknowledged_at=None)
        )
        if not cast("CursorResult[Any]", result).rowcount:
            logger.warning("monitoring_nack_unmatched", host_id=str(host_id), version=version)
            return False

        self._audit.record(
            db,
            action=AuditAction.MONITORING_CONFIG_REJECTED,
            context=context,
            outcome=AuditOutcome.FAILURE,
            target_host_id=host_id,
            details={"version": version, "problems": bounded},
        )
        logger.warning(
            "monitoring_config_rejected",
            host_id=str(host_id),
            version=version,
            problem_count=len(bounded),
        )
        return True

    # --- Internals --------------------------------------------------------

    async def _lock_host(self, db: AsyncSession, host_id: uuid.UUID) -> None:
        """Serialise this transaction against other writes to the same host.

        The same lock class and key derivation as
        :meth:`hcc.services.hosts.HostService._lock_host`, deliberately: a
        configuration write and a credential revocation on the same host must
        serialise against each other, and two lock classes would let them run
        concurrently.
        """
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:class, hashtext(:host_id))").bindparams(
                **{"class": HOST_LOCK_CLASS, "host_id": str(host_id)}
            )
        )

    async def _require_host(self, db: AsyncSession, host_id: uuid.UUID) -> Host:
        """Load a host or raise, after the lock has been taken."""
        host = (
            await db.execute(
                select(Host).where(Host.id == host_id).execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if host is None:
            raise HostNotFoundError(host_id)
        return host


def _to_setting(enabled: bool | None, schedule: Mapping[str, Any] | None) -> ModuleSetting:
    """Turn stored columns into a resolver input.

    The stored schedule is re-parsed rather than trusted, because a column can
    hold a document that was valid under an older release. A parse failure is
    *not* swallowed: it raises, so a stored configuration that cannot be
    understood is a loud failure at this boundary rather than a host silently
    resolving to defaults — which would look like an operator's settings
    having been forgotten.
    """
    from hcc.monitoring.resolve import parse_schedule

    parsed: Schedule | None = None
    if schedule is not None:
        parsed = parse_schedule(dict(schedule))
    return ModuleSetting(enabled=enabled, schedule=parsed)


#: Exported for the API layer, which reports the reason on a revision.
REVISION_REASON_FOR_CONNECT: Final = REVISION_REASON_INITIAL
