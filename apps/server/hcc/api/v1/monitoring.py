"""Monitoring configuration endpoints: the catalogue, defaults and overrides.

Phase 2.

## Paths

    GET    /api/v1/monitoring/modules                         every role
    GET    /api/v1/monitoring/defaults                        every role
    PUT    /api/v1/monitoring/defaults/{module_id}            administrator
    GET    /api/v1/hosts/{host_id}/monitoring                 every role
    PUT    /api/v1/hosts/{host_id}/monitoring/modules/{id}    administrator
    DELETE /api/v1/hosts/{host_id}/monitoring/modules/{id}    administrator
    GET    /api/v1/hosts/{host_id}/monitoring/revisions       every role

## Split across two gates, like the host router

Every role may **read** what is configured; only an administrator may change
it (§2.6). A viewer who cannot see which modules are collecting cannot
interpret a chart — "flat line" and "not collecting" are different facts and
the interface has to be able to tell them apart. An operator who could enable
collection could start gathering temperature, uptime and load data from
somebody's machine without an administrator's involvement, and enabling
collection is the act this product treats as requiring consent
(``docs/MONITORING_MODULES.md`` §2).

Read routes therefore go on ``read_router``, included into ``_protected``, and
writes on ``router``, included into ``_admin`` — privileged by *placement*, so
a route cannot be unprotected by omission (ADR-031).

## Absent and null are different, and the API has to carry both

``PUT`` bodies use Pydantic's ``model_fields_set`` rather than a ``None``
default, because three states have to survive the round trip:

* **absent** — leave this field alone. A request that changes the interval
  must not silently reset ``enabled``.
* **``null``** — inherit. An administrator clearing a host override back to
  the global default.
* **``false``** — off, on purpose.

A model where "not sent" and "sent as null" both arrive as ``None`` makes one
of those three impossible to express, which is the mission's §5 warning. The
reset endpoint is a separate ``DELETE`` rather than a ``PUT`` of all-nulls,
because "stop overriding this module entirely" is one intention and should be
one request.

## Schedules are validated by the service, not by the request model

The request field is a plain object. That is deliberate: a Pydantic union of
the two schedule models reports the failures of **every** member on a
malformed document, so a bad ``interval_seconds`` arrives alongside a
complaint about a missing ``timezone`` and the operator reads two problems
where there is one.
:func:`hcc.monitoring.resolve.parse_schedule` dispatches on ``kind`` first and
returns every real problem and no invented ones
(``docs/SCHEDULING.md`` §10), which is worth more than a tighter OpenAPI
shape. Responses *are* concretely typed, so the browser client still has a
schema for what it reads.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, Path, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hcc.agents.registry import ConnectionRegistry
from hcc.api.deps import (
    AdminDep,
    AuditContextDep,
    MonitoringConfigServiceDep,
    SessionDep,
    ViewerDep,
)
from hcc.api.v1.agents import ConnectionRegistryDep, config_available_frame
from hcc.errors import NotFoundError, ValidationFailedError
from hcc.logging import get_logger
from hcc.models.host import Host
from hcc.models.monitoring import REVISION_REASONS, ConfigSyncStatus
from hcc.monitoring import modules as registry
from hcc.monitoring.document import DOCUMENT_VERSION
from hcc.monitoring.resolve import EffectiveConfiguration, ModuleSetting
from hcc.monitoring.schedule import (
    MAX_INTERVAL_SECONDS,
    MIN_INTERVAL_SECONDS,
    ScheduleError,
    upcoming_runs,
)
from hcc.services.hosts import HostNotFoundError
from hcc.services.monitoring import UNSET, ConfigSyncState, ModuleSettingUpdate

logger = get_logger(__name__)

#: Read routes. Included into ``_protected``: every authenticated role.
read_router = APIRouter(tags=["monitoring"])

#: Write routes. Included into ``_admin``.
router = APIRouter(tags=["monitoring"])

#: Largest page of revision history one request may ask for. The history is
#: the audit-adjacent record of every configuration a host has run, so it grows
#: without bound over a deployment's life and a page has to be finite.
MAX_REVISION_PAGE: Final = 50
DEFAULT_REVISION_PAGE: Final = 20

#: How many future runs a calendar schedule preview returns. Three is enough
#: to see a weekly pattern and cheap enough to compute on every read.
SCHEDULE_PREVIEW_COUNT: Final = 3

#: A module identifier in a path segment, bounded before it is looked up.
ModuleIdPath = Annotated[
    str,
    Path(
        max_length=registry.MAX_MODULE_ID_LENGTH,
        description="A module identifier from GET /api/v1/monitoring/modules.",
    ),
]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class MetricResponse(BaseModel):
    """One series a module produces, declared before any sample arrives.

    The interface needs units and labels up front, so that an enabled module
    with no data yet renders as "no data" rather than as nothing at all.
    """

    id: str
    name: str
    unit: str = Field(description="percent | bytes | seconds | count | load.")
    kind: str = Field(description="gauge | counter. A counter is never averaged.")
    label_keys: list[str] = Field(
        description=(
            "Dimensions this metric is broken down by — 'core' for per-core "
            "CPU, 'window' for the three load averages. Empty means one "
            "series per host."
        )
    )


class ModuleResponse(BaseModel):
    """A module as the interface needs to present it."""

    id: str
    name: str
    description: str
    platform: str
    schedule_kinds: list[str] = Field(
        description="Which scheduling kinds this module accepts. Enforced on save."
    )
    min_interval_seconds: int = Field(description="The floor. Collecting faster is refused.")
    default_interval_seconds: int = Field(
        description=(
            "The interval offered when an operator enables this module. NOT a "
            "default to run: every module ships disabled."
        )
    )
    data_schema_version: int
    requires_privileges: bool
    allows_duplicate_runs: bool
    metrics: list[MetricResponse]


class ModuleCatalogueResponse(BaseModel):
    """Everything that can be configured, and the limits that apply to all of it."""

    registry_version: int = Field(
        description=(
            "Version of the catalogue as a whole. Distinct from the protocol "
            "version: adding a module must not force a fleet upgrade."
        )
    )
    document_version: int = Field(description="Version of the configuration document shape.")
    min_interval_seconds: int = Field(description="Global interval floor, across every module.")
    max_interval_seconds: int = Field(description="Global interval ceiling.")
    modules: list[ModuleResponse]


class StoredSettingResponse(BaseModel):
    """One layer's stored opinion. ``null`` means that layer inherits."""

    enabled: bool | None = Field(
        description="True, false, or null meaning this layer does not say."
    )
    schedule: dict[str, Any] | None = Field(
        description="The stored schedule document, or null meaning this layer does not say."
    )


class GlobalDefaultResponse(BaseModel):
    """A global default, keyed by module."""

    module_id: str
    setting: StoredSettingResponse


class GlobalDefaultsResponse(BaseModel):
    """Every global default.

    Modules with nothing configured are present with both fields null, so the
    interface renders the full catalogue from one response and "nothing has
    been said about this module" is a value rather than a missing key.
    """

    defaults: list[GlobalDefaultResponse]


class ResolvedEnabledResponse(BaseModel):
    """Whether a module collects on this host, and which layer decided."""

    value: bool
    source: str = Field(
        description=(
            "default | global | host. 'default' means nothing is configured, "
            "which resolves to off. The source is why an interface can answer "
            "'why is this host collecting every five seconds?'."
        )
    )


class ResolvedScheduleResponse(BaseModel):
    """The schedule a module runs on, and which layer decided."""

    value: dict[str, Any] = Field(
        description=(
            "The fully resolved schedule, exactly as the agent receives it. "
            "Every value is concrete — `jitter_seconds` is resolved, never "
            "omitted — because the agent merges nothing."
        )
    )
    source: str = Field(description="default | global | host.")
    next_runs: list[str] | None = Field(
        default=None,
        description=(
            "For a calendar schedule, the next few executions as ISO-8601 "
            "instants. Null for an interval schedule, where the next run is "
            "arithmetic rather than a calculation worth previewing."
        ),
    )


class EffectiveModuleResponse(BaseModel):
    """One module's effective configuration on one host, with its provenance.

    Both stored layers are reported alongside the resolved value, because
    "inherited from the global default of 10 s" and "overridden on this host to
    10 s" are identical in the value and are different facts.
    """

    module_id: str
    name: str
    enabled: ResolvedEnabledResponse
    schedule: ResolvedScheduleResponse
    global_default: StoredSettingResponse
    host_override: StoredSettingResponse | None = Field(
        description="The host's own override, or null when this host has none at all."
    )


class ConfigSyncResponse(BaseModel):
    """Desired versus applied.

    The distinction §23 insists on: a configuration the controller has saved is
    not thereby active. ``status`` describes the gap.
    """

    status: str = Field(
        description=(
            "none | up_to_date | pending | rejected | stale. 'pending' is not a "
            "problem on its own — an agent has up to one heartbeat interval to "
            "notice. 'stale' is."
        )
    )
    desired_version: int | None = Field(description="Newest revision the controller has generated.")
    desired_checksum: str | None
    reported_version: int | None = Field(
        description="What the agent says it is running. Null means it has not said."
    )
    reported_checksum: str | None
    acknowledged_at: str | None = Field(
        description="When the agent confirmed applying the newest revision."
    )
    rejected_at: str | None
    rejection_problems: list[str] = Field(
        description="Every problem the agent reported, bounded. Empty unless rejected."
    )
    generated_at: str | None


class HostMonitoringResponse(BaseModel):
    """A host's full monitoring configuration."""

    host_id: str
    modules: list[EffectiveModuleResponse]
    sync: ConfigSyncResponse
    problems: list[str] = Field(
        description=(
            "Adjustments the controller made on the operator's behalf — an "
            "interval raised to a module's current floor, configuration for a "
            "module this release no longer has. Not errors: the configuration "
            "is valid and in use. Shown so a setting that does something other "
            "than what it says cannot do so silently."
        )
    )


class ModuleSettingRequest(BaseModel):
    """A change to one module at one layer.

    ``model_fields_set`` distinguishes absent from null, which is the whole
    reason this is not two optional fields with ``None`` defaults — see the
    module docstring.
    """

    model_config = {"extra": "forbid"}

    enabled: bool | None = Field(
        default=None,
        description=(
            "True to collect, false not to, null to inherit the layer above. "
            "Omit the field entirely to leave it unchanged."
        ),
    )
    schedule: dict[str, Any] | None = Field(
        default=None,
        description=(
            "A schedule document — {'kind': 'interval', 'interval_seconds': N} "
            "or a calendar schedule — or null to inherit. Omit to leave "
            "unchanged. Validated against the module's permitted kinds and its "
            "interval floor; every problem is returned at once."
        ),
    )

    def to_update(self) -> ModuleSettingUpdate:
        """Translate into the service's update, preserving all three states."""
        return ModuleSettingUpdate(
            enabled=self.enabled if "enabled" in self.model_fields_set else UNSET,
            schedule=self.schedule if "schedule" in self.model_fields_set else UNSET,
        )


class GlobalDefaultSavedResponse(BaseModel):
    """What a global-default save did, including how far it reached."""

    module_id: str
    setting: StoredSettingResponse
    hosts_changed: int = Field(
        description=(
            "How many hosts' effective configuration this changed. Not every "
            "host: one that overrides this module is unaffected, and saying so "
            "lets the interface report '12 of 30 changed' rather than implying "
            "the whole fleet moved."
        )
    )


class HostSettingSavedResponse(BaseModel):
    """What a host-override save did."""

    module_id: str
    setting: StoredSettingResponse
    revision_version: int = Field(description="The revision this produced, or the existing one.")
    revision_created: bool = Field(
        description=(
            "False when the save changed nothing the agent would notice — a "
            "setting written back identical to what it already was. The "
            "configuration is correct either way; this says whether a new "
            "revision was minted."
        )
    )
    sync: ConfigSyncResponse


class RevisionResponse(BaseModel):
    """One historical configuration, without its document.

    The document is several kilobytes and is not included in a list: a page of
    twenty would be most of a megabyte, and the interface needs the versions
    and their outcomes rather than the bytes.
    """

    version: int
    checksum: str
    generated_at: str
    reason: str | None
    created_by_user_id: str | None
    acknowledged_at: str | None
    rejected_at: str | None
    rejection_problems: list[str]


class RevisionListResponse(BaseModel):
    """A page of a host's configuration history, newest first."""

    revisions: list[RevisionResponse]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    """ISO-8601, or null. Never a formatted placeholder for a missing date."""
    return value.isoformat() if value is not None else None


def _module_response(module: registry.ModuleDescriptor) -> ModuleResponse:
    return ModuleResponse(
        id=module.id,
        name=module.name,
        description=module.description,
        platform=module.platform.value,
        schedule_kinds=[kind.value for kind in module.schedule_kinds],
        min_interval_seconds=module.min_interval_seconds,
        default_interval_seconds=module.default_interval_seconds,
        data_schema_version=module.data_schema_version,
        requires_privileges=module.requires_privileges,
        allows_duplicate_runs=module.allows_duplicate_runs,
        metrics=[
            MetricResponse(
                id=metric.id,
                name=metric.name,
                unit=metric.unit.value,
                kind=metric.kind.value,
                label_keys=list(metric.label_keys),
            )
            for metric in module.metrics
        ],
    )


def _stored(setting: ModuleSetting | None) -> StoredSettingResponse:
    """A stored layer, with ``None`` rendered as "this layer does not say"."""
    if setting is None:
        return StoredSettingResponse(enabled=None, schedule=None)
    return StoredSettingResponse(
        enabled=setting.enabled,
        schedule=setting.schedule.to_document() if setting.schedule is not None else None,
    )


def _sync_response(state: ConfigSyncState) -> ConfigSyncResponse:
    return ConfigSyncResponse(
        status=state.status.value,
        desired_version=state.desired_version,
        desired_checksum=state.desired_checksum,
        reported_version=state.reported_version,
        reported_checksum=state.reported_checksum,
        acknowledged_at=_iso(state.acknowledged_at),
        rejected_at=_iso(state.rejected_at),
        rejection_problems=list(state.rejection_problems),
        generated_at=_iso(state.generated_at),
    )


def _effective_response(
    effective: EffectiveConfiguration,
    *,
    global_defaults: dict[str, ModuleSetting],
    host_overrides: dict[str, ModuleSetting],
    now: datetime,
) -> list[EffectiveModuleResponse]:
    modules: list[EffectiveModuleResponse] = []
    for entry in effective.modules:
        schedule = entry.schedule.value
        preview: list[str] | None = None
        if schedule.kind.value == "calendar":
            preview = [
                occurrence.at.isoformat()
                for occurrence in upcoming_runs(
                    schedule,  # type: ignore[arg-type]
                    after=now,
                    count=SCHEDULE_PREVIEW_COUNT,
                )
            ]
        modules.append(
            EffectiveModuleResponse(
                module_id=entry.module_id,
                name=registry.descriptor(entry.module_id).name,
                enabled=ResolvedEnabledResponse(
                    value=entry.enabled.value, source=entry.enabled.source.value
                ),
                schedule=ResolvedScheduleResponse(
                    value=schedule.to_document(),
                    source=entry.schedule.source.value,
                    next_runs=preview,
                ),
                global_default=_stored(global_defaults.get(entry.module_id)),
                host_override=(
                    _stored(host_overrides[entry.module_id])
                    if entry.module_id in host_overrides
                    else None
                ),
            )
        )
    return modules


def _require_known_module(module_id: str) -> registry.ModuleDescriptor:
    """Resolve a module or raise a 404.

    404 rather than 422: the module identifier is a **path segment**, so an
    unknown one is a resource that does not exist rather than a malformed
    field. The message names the catalogue endpoint, because "unknown module"
    without a way to discover the real ones is an error nobody can act on.
    """
    try:
        return registry.descriptor(module_id)
    except registry.UnknownModuleError as exc:
        raise NotFoundError(
            f"No monitoring module {module_id!r}. "
            "GET /api/v1/monitoring/modules lists every module this controller has."
        ) from exc


def _notify(registry: ConnectionRegistry, changed: Iterable[tuple[uuid.UUID, int]]) -> int:
    """Tell any connected agent that a newer configuration exists.

    **Best effort, and deliberately so.** Returns how many agents this process
    was able to notify, which may be fewer than the hosts that changed — a host
    may be offline, or its socket may be held by another worker.

    Nothing depends on the notification arriving. Every heartbeat
    acknowledgement states the version the controller believes is current, so
    an agent that missed this asks within one heartbeat interval. The push
    exists to turn "up to thirty seconds" into "milliseconds" for the common
    case, which is an operator watching the interface after clicking save.

    Called **after** the commit, never before. A notification sent inside the
    transaction would race its own commit: the agent could ask for the
    configuration and be served the previous revision, then never ask again
    because the notification had already been spent.
    """
    return sum(
        1
        for host_id, version in changed
        if registry.notify(host_id, config_available_frame(version))
    )


def _validation_failed(exc: ScheduleError) -> ValidationFailedError:
    """Turn a schedule refusal into a 422 that lists every problem.

    Not the first problem. An operator fixing a schedule in a form should need
    one pass, not one pass per mistake (``docs/SCHEDULING.md`` §10).
    """
    return ValidationFailedError(
        "The schedule was refused.",
        details={"problems": exc.problems},
    )


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------


@read_router.get(
    "/monitoring/modules",
    summary="Every monitoring module this controller supports",
    response_model=ModuleCatalogueResponse,
)
async def list_modules(_session: ViewerDep) -> ModuleCatalogueResponse:
    """The closed catalogue, with the limits that bound every schedule.

    Static for a given release — it is the registry, which is code — so it
    takes no database session and does no query. Served from an authenticated
    route rather than a public one anyway: the module list describes what this
    deployment is capable of collecting, which is of no use to an anonymous
    caller and of some use to someone profiling the deployment.
    """
    return ModuleCatalogueResponse(
        registry_version=registry.REGISTRY_VERSION,
        document_version=DOCUMENT_VERSION,
        min_interval_seconds=MIN_INTERVAL_SECONDS,
        max_interval_seconds=MAX_INTERVAL_SECONDS,
        modules=[_module_response(module) for module in registry.MODULE_LIST],
    )


# ---------------------------------------------------------------------------
# Global defaults
# ---------------------------------------------------------------------------


@read_router.get(
    "/monitoring/defaults",
    summary="The global module defaults",
    response_model=GlobalDefaultsResponse,
)
async def get_global_defaults(
    db: SessionDep,
    monitoring: MonitoringConfigServiceDep,
    _session: ViewerDep,
) -> GlobalDefaultsResponse:
    """What every host does unless it overrides it.

    Every module in the catalogue appears, including those with nothing
    configured — both fields null — so the interface renders the whole list
    from one response and does not have to treat a missing key as a value.
    """
    stored = await monitoring.global_defaults(db)
    return GlobalDefaultsResponse(
        defaults=[
            GlobalDefaultResponse(module_id=module.id, setting=_stored(stored.get(module.id)))
            for module in registry.MODULE_LIST
        ]
    )


@router.put(
    "/monitoring/defaults/{module_id}",
    summary="Set or clear a global module default",
    response_model=GlobalDefaultSavedResponse,
    status_code=status.HTTP_200_OK,
    responses={
        404: {"description": "No such module."},
        422: {"description": "The schedule was refused; every problem is listed."},
    },
)
async def set_global_default(
    db: SessionDep,
    monitoring: MonitoringConfigServiceDep,
    context: AuditContextDep,
    payload: ModuleSettingRequest,
    registry: ConnectionRegistryDep,
    _admin: AdminDep,
    module_id: ModuleIdPath,
) -> GlobalDefaultSavedResponse:
    """Change the fleet-wide default for one module.

    **200, not 201.** Nothing addressable is created: the resource is the
    module's default, which exists conceptually whether or not a row backs it,
    and this endpoint is idempotent — sending the same body twice leaves the
    same state and mints no second revision.

    Regenerates the affected hosts' configuration revisions in the same
    transaction, so a host's desired configuration cannot lag the default it
    is supposed to inherit. Bounded: past
    :data:`hcc.services.monitoring.MAX_EAGER_REGENERATION` hosts the remainder
    pick the change up on their next connection, which regenerates anyway.
    """
    _require_known_module(module_id)
    try:
        setting, changed = await monitoring.set_global_default(
            db, module_id=module_id, update=payload.to_update(), context=context
        )
    except ScheduleError as exc:
        await db.rollback()
        raise _validation_failed(exc) from exc

    # One unit of work: the default, every regenerated revision, and the audit
    # event. A partial commit would leave hosts inheriting a default that was
    # never saved.
    await db.commit()

    notified = _notify(registry, changed)
    logger.info(
        "monitoring_global_default_saved",
        module=module_id,
        enabled=setting.enabled,
        hosts_changed=len(changed),
        agents_notified=notified,
    )
    return GlobalDefaultSavedResponse(
        module_id=module_id,
        setting=_stored(setting),
        hosts_changed=len(changed),
    )


# ---------------------------------------------------------------------------
# Per-host configuration
# ---------------------------------------------------------------------------


@read_router.get(
    "/hosts/{host_id}/monitoring",
    summary="A host's effective monitoring configuration",
    response_model=HostMonitoringResponse,
    responses={404: {"description": "No such host."}},
)
async def get_host_monitoring(
    db: SessionDep,
    monitoring: MonitoringConfigServiceDep,
    _session: ViewerDep,
    host_id: uuid.UUID,
) -> HostMonitoringResponse:
    """Every module's resolved settings for one host, with provenance.

    Reports the resolved value *and* both stored layers, because the question
    an operator asks is not "what is the interval" but "why is it that".
    """
    host = await _load_host(db, host_id)
    now = datetime.now(UTC)

    global_defaults = await monitoring.global_defaults(db)
    host_overrides = await monitoring.host_overrides(db, host_id)
    effective = await monitoring.effective(db, host_id)
    sync = await monitoring.sync_state(db, host, now=now)

    return HostMonitoringResponse(
        host_id=str(host_id),
        modules=_effective_response(
            effective,
            global_defaults=global_defaults,
            host_overrides=host_overrides,
            now=now,
        ),
        sync=_sync_response(sync),
        problems=list(effective.problems),
    )


@router.put(
    "/hosts/{host_id}/monitoring/modules/{module_id}",
    summary="Set or clear one module's override on one host",
    response_model=HostSettingSavedResponse,
    status_code=status.HTTP_200_OK,
    responses={
        404: {"description": "No such host, or no such module."},
        422: {"description": "The schedule was refused; every problem is listed."},
    },
)
async def set_host_override(
    db: SessionDep,
    monitoring: MonitoringConfigServiceDep,
    context: AuditContextDep,
    payload: ModuleSettingRequest,
    registry: ConnectionRegistryDep,
    _admin: AdminDep,
    host_id: uuid.UUID,
    module_id: ModuleIdPath,
) -> HostSettingSavedResponse:
    """Make this host differ from the global default for one module.

    The response reports ``revision_created`` and the sync status rather than
    just success, because §23 draws a line this endpoint sits exactly on: a
    configuration the controller has saved is **not** thereby active. The
    agent has to receive it, validate it and apply it, and until it does the
    status is ``pending``.
    """
    _require_known_module(module_id)
    try:
        setting, outcome = await monitoring.set_host_override(
            db,
            host_id=host_id,
            module_id=module_id,
            update=payload.to_update(),
            context=context,
        )
    except HostNotFoundError as exc:
        await db.rollback()
        raise NotFoundError("No such host.") from exc
    except ScheduleError as exc:
        await db.rollback()
        raise _validation_failed(exc) from exc

    await db.commit()

    if outcome.created:
        _notify(registry, [(host_id, outcome.revision.version)])

    host = await _load_host(db, host_id)
    sync = await monitoring.sync_state(db, host)
    logger.info(
        "monitoring_host_override_saved",
        host_id=str(host_id),
        module=module_id,
        enabled=setting.enabled,
        revision_created=outcome.created,
    )
    return HostSettingSavedResponse(
        module_id=module_id,
        setting=_stored(setting),
        revision_version=outcome.revision.version,
        revision_created=outcome.created,
        sync=_sync_response(sync),
    )


@router.delete(
    "/hosts/{host_id}/monitoring/modules/{module_id}",
    summary="Make a host inherit the global default for one module",
    response_model=HostSettingSavedResponse,
    status_code=status.HTTP_200_OK,
    responses={404: {"description": "No such host, or no such module."}},
)
async def reset_host_override(
    db: SessionDep,
    monitoring: MonitoringConfigServiceDep,
    context: AuditContextDep,
    registry: ConnectionRegistryDep,
    _admin: AdminDep,
    host_id: uuid.UUID,
    module_id: ModuleIdPath,
) -> HostSettingSavedResponse:
    """Remove this host's override, so it inherits again.

    A ``DELETE`` rather than a ``PUT`` of all-nulls: "stop overriding this
    module entirely" is one intention and should be one request. Idempotent —
    resetting a module that was already inherited succeeds, and is recorded,
    because "did anyone try to change this?" is a question the audit trail
    should be able to answer.

    **200 with a body**, not 204. The caller needs the resulting revision and
    sync status in order to show what the reset actually did, and a 204 would
    make that a second request against state that may already have moved.
    """
    _require_known_module(module_id)
    try:
        outcome = await monitoring.reset_host_override(
            db, host_id=host_id, module_id=module_id, context=context
        )
    except HostNotFoundError as exc:
        await db.rollback()
        raise NotFoundError("No such host.") from exc

    await db.commit()

    if outcome.created:
        _notify(registry, [(host_id, outcome.revision.version)])

    host = await _load_host(db, host_id)
    stored = await monitoring.host_overrides(db, host_id)
    sync = await monitoring.sync_state(db, host)
    logger.info(
        "monitoring_host_override_reset",
        host_id=str(host_id),
        module=module_id,
        revision_created=outcome.created,
    )
    return HostSettingSavedResponse(
        module_id=module_id,
        setting=_stored(stored.get(module_id)),
        revision_version=outcome.revision.version,
        revision_created=outcome.created,
        sync=_sync_response(sync),
    )


@read_router.get(
    "/hosts/{host_id}/monitoring/revisions",
    summary="A host's configuration history",
    response_model=RevisionListResponse,
    responses={404: {"description": "No such host."}},
)
async def list_revisions(
    db: SessionDep,
    monitoring: MonitoringConfigServiceDep,
    _session: ViewerDep,
    host_id: uuid.UUID,
    limit: int = Query(default=DEFAULT_REVISION_PAGE, ge=1, le=MAX_REVISION_PAGE),
    offset: int = Query(default=0, ge=0),
) -> RevisionListResponse:
    """Every configuration this host has been given, newest first.

    Offset pagination, unlike the audit log's keyset. The justification is the
    scale: a host accumulates a revision per configuration change, which is
    operator-paced rather than event-paced, so the deep-offset degradation
    keyset pagination exists to avoid is not reachable here. The audit log,
    which grows per request, genuinely needs keyset.

    The documents themselves are not included — see
    :class:`RevisionResponse`.
    """
    await _load_host(db, host_id)
    page, total = await monitoring.revisions(db, host_id, limit=limit, offset=offset)
    return RevisionListResponse(
        revisions=[
            RevisionResponse(
                version=revision.version,
                checksum=revision.checksum,
                generated_at=revision.generated_at.isoformat(),
                reason=revision.reason,
                created_by_user_id=(
                    str(revision.created_by_user_id)
                    if revision.created_by_user_id is not None
                    else None
                ),
                acknowledged_at=_iso(revision.acknowledged_at),
                rejected_at=_iso(revision.rejected_at),
                rejection_problems=list(revision.rejection_problems or ()),
            )
            for revision in page
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _load_host(db: AsyncSession, host_id: uuid.UUID) -> Host:
    """Load a host or raise a 404.

    Reads the row directly rather than through :class:`HostService`. The host
    service's reader returns a `HostRecord` with connectivity derived and
    enrollment counts joined, none of which this module needs — and taking the
    dependency would mean every monitoring read ran two correlated subqueries
    to answer "does this host exist".
    """
    host = (await db.execute(select(Host).where(Host.id == host_id))).scalar_one_or_none()
    if host is None:
        raise NotFoundError("No such host.")
    return host


#: Re-exported so the API tests can assert the vocabulary has not quietly
#: grown, and so the interface's filter list has one source.
SYNC_STATUSES: Final = tuple(member.value for member in ConfigSyncStatus)
REVISION_REASON_VALUES: Final = REVISION_REASONS
