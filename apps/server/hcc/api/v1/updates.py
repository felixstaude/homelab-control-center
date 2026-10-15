"""Update management endpoints.

## The read/write split, and why it is where it is

Two routers, the same way :mod:`hcc.api.v1.monitoring` splits its own:

* ``read_router`` goes on the protected router, so **every authenticated role**
  can see update status. A viewer who cannot see that the fleet is two versions
  behind cannot do their job, and hiding it would make the interface's most
  useful page an administrator-only one.
* ``router`` goes on the administrator router. Enabling automatic updates,
  changing a channel, starting an update, cancelling one, changing a pin — each
  of those is a decision about running different code on somebody's machine,
  which makes them the highest-consequence actions in the product.

An operator can see everything and change nothing. That is deliberate: an
operator who could start an update could install a version of their choosing
on every host they can see.

## What is not here

**No endpoint updates the controller.** The controller does not replace its own
container — that needs a Docker socket, and a Docker socket in a web
application is root on the host reachable through whatever the next
authentication bug is. The assisted workflow is a *plan* this API can render
and a script the operator runs outside the container. See
``docs/plans/evergreen-update-architecture.md`` §8.

**No endpoint returns a credential or a signing key.** The update instruction
carries release metadata and a publisher signature, both of which are public
by construction: the signature is verifiable precisely because the public key
is not a secret.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, time
from typing import TYPE_CHECKING, Annotated, Final, Literal

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from starlette.requests import HTTPConnection

from hcc import __version__
from hcc.agents.releases import CHANNELS, ManifestError, load_release_manifest
from hcc.api.deps import AuditContextDep, SettingsDep, get_database
from hcc.db import Database
from hcc.errors import AppError, NotFoundError, ValidationFailedError
from hcc.logging import get_logger
from hcc.models.updates import (
    ROLLOUT_NAME_MAX_LENGTH,
    VERSION_MAX_LENGTH,
    UpdateRollout,
    UpdateState,
    UpdateTrigger,
)
from hcc.services.hosts import HostNotFoundError
from hcc.services.rollouts import (
    MAX_ROLLOUT_HOSTS,
    MemberOutcome,
    RolloutInvalidError,
    RolloutNotFoundError,
    RolloutNotResumableError,
    RolloutService,
)
from hcc.services.updates import (
    MAX_HISTORY_LIMIT,
    UpdateAlreadyRunningError,
    UpdateNotCancellableError,
    UpdateNotFoundError,
    UpdateNotPossibleError,
    UpdateService,
    UpdateServiceError,
)
from hcc.updates.compat import UpdatePlan
from hcc.updates.policy import MaintenanceWindow, Policy, WindowError
from hcc.updates.schema import applied_revisions, current_revision

if TYPE_CHECKING:
    from hcc.agents.releases import ReleaseManifest
    from hcc.models.updates import UpdateEvent, UpdateJob
    from hcc.services.updates import FleetUpdateOverview, HostUpdateStatus

logger = get_logger(__name__)

read_router = APIRouter(tags=["updates"])
router = APIRouter(tags=["updates"])


def get_update_service(connection: HTTPConnection) -> UpdateService:
    """Return the process-wide update service."""
    service: UpdateService = connection.app.state.update_service
    return service


UpdateServiceDep = Annotated[UpdateService, Depends(get_update_service)]
# Both of these name runtime types rather than forward references, and the
# comment is here because the mistake is easy and the symptom is confusing:
# FastAPI resolves a dependency's annotation at runtime, so `HTTPConnection`
# or `Database` behind `TYPE_CHECKING` makes schema generation fail with
# "is not fully defined" a long way from the cause. hcc/api/v1/agents.py
# carries the same note.
DatabaseDep = Annotated[Database, Depends(get_database)]


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class BlockerResponse(BaseModel):
    """One reason a release cannot be installed on a host."""

    code: str = Field(
        description=(
            "protocol_mismatch | protocol_undeclared | controller_too_old | "
            "schema_too_old | platform_unavailable | channel_not_permitted | "
            "downgrade_not_authorized"
        )
    )
    detail: str = Field(description="A sentence naming what is wrong and often what to do.")


class UpdatePlanResponse(BaseModel):
    """What should happen to one host, and why not."""

    outcome: str = Field(
        description=(
            "up_to_date | update_available | update_available_incompatible | "
            "downgrade | pinned | unknown_version | unsupported | "
            "metadata_unavailable"
        )
    )
    installed_version: str | None = Field(
        default=None, description="What the host reports running, when it is parseable."
    )
    target_version: str | None = Field(
        default=None, description="What it would move to. Null when there is nothing."
    )
    target_channel: str | None = Field(default=None, description="The channel the target is in.")
    step: str = Field(description="major | minor | patch | prerelease | none")
    #: Every blocker, not the first. An administrator fixing one wants to know
    #: about the second now.
    blockers: list[BlockerResponse] = Field(default_factory=list)
    detail: str | None = Field(default=None, description="A sentence, when the outcome needs one.")
    download_url: str | None = Field(
        default=None,
        description=(
            "Where the target's archive for this host's architecture is "
            "published. Public release metadata, not a credentialled URL."
        ),
    )
    expected_sha256: str | None = Field(
        default=None, description="The target archive's digest, from the release metadata."
    )

    @classmethod
    def of(cls, plan: UpdatePlan) -> UpdatePlanResponse:
        return cls(
            outcome=plan.outcome.value,
            installed_version=str(plan.installed) if plan.installed else None,
            target_version=str(plan.target) if plan.target else None,
            target_channel=plan.target_channel,
            step=plan.step,
            blockers=[
                BlockerResponse(code=blocker.code.value, detail=blocker.detail)
                for blocker in plan.blockers
            ],
            detail=plan.detail,
            download_url=plan.artifact.url if plan.artifact else None,
            expected_sha256=plan.artifact.sha256 if plan.artifact else None,
        )


class MaintenanceWindowResponse(BaseModel):
    """When a host is willing to be interrupted."""

    timezone: str = Field(description="IANA zone name. Always present, never implied.")
    weekdays: list[int] = Field(description="0 = Sunday … 6 = Saturday. Empty means every day.")
    start: str = Field(description="Local wall-clock HH:MM.")
    end: str = Field(description="Local wall-clock HH:MM. Equal to start means all day.")
    description: str = Field(description="One line for display, naming the zone.")


class UpdateJobResponse(BaseModel):
    """One update attempt."""

    job_id: uuid.UUID
    host_id: uuid.UUID
    state: str = Field(description="See the update state machine.")
    trigger: str = Field(description="manual | policy | rollout")
    from_version: str | None
    to_version: str
    resulting_version: str | None = Field(
        default=None,
        description=(
            "What the host reported after the attempt. Can differ from "
            "`to_version`, and the difference is the interesting case."
        ),
    )
    error: str | None = Field(default=None, description="Why it failed. Never a stack trace.")
    attempt: int
    created_at: str
    started_at: str | None
    finished_at: str | None
    requested_by_user_id: uuid.UUID | None = Field(
        default=None, description="Null for a policy or rollout decision."
    )

    @classmethod
    def of(cls, job: UpdateJob) -> UpdateJobResponse:
        return cls(
            job_id=job.id,
            host_id=job.host_id,
            state=job.state,
            trigger=job.trigger,
            from_version=job.from_version,
            to_version=job.to_version,
            resulting_version=job.resulting_version,
            error=job.error,
            attempt=job.attempt,
            created_at=job.created_at.isoformat(),
            started_at=job.started_at.isoformat() if job.started_at else None,
            finished_at=job.finished_at.isoformat() if job.finished_at else None,
            requested_by_user_id=job.requested_by_user_id,
        )


class UpdateEventResponse(BaseModel):
    """One state transition."""

    sequence: int = Field(
        description=(
            "The log's own ordering. Not a timestamp: several transitions can "
            "share one, because PostgreSQL's now() is the transaction time."
        )
    )
    state: str
    detail: str | None
    source: str = Field(description="controller | agent")
    created_at: str


class HostUpdateStatusResponse(BaseModel):
    """Everything the interface shows for one host."""

    host_id: uuid.UUID
    host_name: str
    installed_version: str | None
    plan: UpdatePlanResponse
    policy: str = Field(description="manual | notify | patch | minor | pinned")
    automatic: bool = Field(description="Whether this host installs anything by itself.")
    channel: str
    pinned_version: str | None
    maintenance_window: MaintenanceWindowResponse | None
    open_job: UpdateJobResponse | None
    last_job: UpdateJobResponse | None

    @classmethod
    def of(cls, status_: HostUpdateStatus) -> HostUpdateStatusResponse:
        return cls(
            host_id=status_.host_id,
            host_name=status_.host_name,
            installed_version=status_.installed_version,
            plan=UpdatePlanResponse.of(status_.plan),
            policy=status_.policy.value,
            automatic=status_.policy.is_automatic,
            channel=status_.channel,
            pinned_version=status_.pinned_version,
            maintenance_window=_window_response(status_.window),
            open_job=UpdateJobResponse.of(status_.open_job) if status_.open_job else None,
            last_job=UpdateJobResponse.of(status_.last_job) if status_.last_job else None,
        )


class VersionCountResponse(BaseModel):
    """How many hosts report one version."""

    version: str
    hosts: int


class FleetOverviewResponse(BaseModel):
    """The dashboard's numbers."""

    total_hosts: int
    up_to_date: int
    update_available: int
    blocked: int = Field(
        description="A newer release exists and something stops it. Still an update."
    )
    needs_attention: int = Field(
        description="Unsupported or unreadable versions — hosts somebody has to look at."
    )
    version_distribution: list[VersionCountResponse] = Field(
        description="Newest first. Unparseable versions last and visible rather than hidden."
    )
    hosts_with_automatic_updates: int
    open_jobs: int
    failed_recently: int
    controller_version: str
    controller_schema_revision: str | None
    metadata_available: bool = Field(
        description="Whether release metadata is configured. Not the same as 'no updates'."
    )
    metadata_detail: str | None
    metadata_signature: str = Field(
        description=(
            "valid | absent | untrusted_key | invalid | malformed | "
            "unsupported_algorithm. An automatic update needs 'valid'."
        )
    )
    metadata_signature_detail: str | None
    #: The channels this build understands, so the interface does not hardcode
    #: them and cannot offer one the controller would refuse.
    channels: list[str]


class UpdateHistoryResponse(BaseModel):
    """A page of update attempts, newest first."""

    jobs: list[UpdateJobResponse]
    next_before: str | None = Field(
        default=None,
        description=(
            "Pass as `before` for the next page, or null at the end. Keyset "
            "rather than an offset: a fleet's history grows without bound."
        ),
    )


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class MaintenanceWindowRequest(BaseModel):
    """When this host may be interrupted."""

    model_config = {"extra": "forbid"}

    timezone: str = Field(
        max_length=64,
        description="IANA zone name, e.g. Europe/Berlin. Required: 02:00 means 02:00 somewhere.",
    )
    weekdays: list[int] = Field(
        default_factory=list,
        max_length=7,
        description="0 = Sunday … 6 = Saturday. Empty means every day.",
    )
    start: str = Field(description="Local wall-clock HH:MM.")
    end: str = Field(description="Local wall-clock HH:MM. Equal to start means all day.")

    @field_validator("start", "end")
    @classmethod
    def _validate_time(cls, value: str) -> str:
        try:
            hours, minutes = value.split(":")
            parsed = time(hour=int(hours), minute=int(minutes))
        except (ValueError, TypeError) as error:
            raise ValueError(f"{value!r} is not a wall-clock time like '02:00'") from error
        return parsed.strftime("%H:%M")

    @field_validator("weekdays")
    @classmethod
    def _validate_weekdays(cls, value: list[int]) -> list[int]:
        for day in value:
            if not 0 <= day <= 6:
                raise ValueError(f"weekday {day} is out of range; 0 = Sunday … 6 = Saturday")
        # Sorted and deduplicated, so two requests that mean the same thing
        # store the same thing — and the interface does not have to care about
        # the order a form submitted them in.
        return sorted(set(value))

    def to_window(self) -> MaintenanceWindow:
        hours, minutes = self.start.split(":")
        end_hours, end_minutes = self.end.split(":")
        return MaintenanceWindow(
            timezone=self.timezone,
            weekdays=tuple(self.weekdays),
            start=time(hour=int(hours), minute=int(minutes)),
            end=time(hour=int(end_hours), minute=int(end_minutes)),
        )


class UpdatePolicyRequest(BaseModel):
    """What a host is allowed to do by itself."""

    model_config = {"extra": "forbid"}

    policy: Literal["manual", "notify", "patch", "minor", "pinned"] = Field(
        description=(
            "manual: nothing automatic. notify: surfaced, not installed. "
            "patch/minor: install compatible releases of that size. pinned: "
            "hold at `pinned_version`. There is deliberately no 'major'."
        )
    )
    channel: Literal["stable", "beta", "alpha"] = Field(
        default="stable", description="The most instability this host accepts."
    )
    pinned_version: str | None = Field(
        default=None,
        max_length=VERSION_MAX_LENGTH,
        description="Required with the `pinned` policy, and forbidden without it.",
    )
    maintenance_window: MaintenanceWindowRequest | None = Field(
        default=None, description="Null means any time."
    )

    @model_validator(mode="after")
    def _validate_pairing(self) -> UpdatePolicyRequest:
        # Refused here as well as by the check constraint, so the caller gets
        # a 422 naming the field rather than a 500 from the database.
        if (self.policy == "pinned") != (self.pinned_version is not None):
            raise ValueError(
                "a pinned policy needs a pinned_version, and a pinned_version "
                "needs the pinned policy. Set both or neither."
            )
        return self


class StartUpdateRequest(BaseModel):
    """Install a release on one host."""

    model_config = {"extra": "forbid"}

    target_version: str | None = Field(
        default=None,
        max_length=VERSION_MAX_LENGTH,
        description=(
            "What to install. Null means whatever the plan recommends, which "
            "is the normal case; naming a version installs something other "
            "than the newest and is still checked against compatibility."
        ),
    )
    allow_downgrade: bool = Field(
        default=False,
        description=(
            "Authorize going backwards. Audited separately from an ordinary "
            "upgrade, because 'when did we last roll a host back' is a "
            "question somebody asks."
        ),
    )


class CancelUpdateRequest(BaseModel):
    """Stop an update that has not got too far."""

    model_config = {"extra": "forbid"}

    reason: str | None = Field(
        default=None, max_length=ROLLOUT_NAME_MAX_LENGTH, description="For the history."
    )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class UpdateAlreadyRunningConflictError(AppError):
    """409: this host already has an open update."""

    status_code = status.HTTP_409_CONFLICT
    code = "update_already_running"


class UpdateNotPossibleValidationError(AppError):
    """422: there is nothing to install, or something blocks it."""

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "update_not_possible"


class UpdateNotCancellableConflictError(AppError):
    """409: it has got too far to cancel."""

    status_code = status.HTTP_409_CONFLICT
    code = "update_not_cancellable"


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class _ManifestState:
    """The manifest, and what is known about who vouched for it.

    One object rather than a tuple because ``authentic`` is the fourth value
    and the thing most easily dropped — it was a tuple for exactly one
    revision, four of the five call sites unpacked it as ``manifest, _, _``,
    and the result was a controller that offered updates it had not verified.
    """

    manifest: ReleaseManifest | None
    signature_status: str
    signature_detail: str | None
    #: Whether the signature verified against a key this controller trusts.
    #: **Only** a valid signature. Absence, an untrusted key and a broken
    #: signature are all "not authentic", because none of them establishes
    #: who made the release.
    authentic: bool


async def _manifest_and_signature(settings: SettingsDep) -> _ManifestState:
    """Load the manifest and report its signature status.

    A manifest that will not load is reported rather than raised: the overview
    is the page somebody opens *because* something is wrong, and a 500 there
    would hide the reason behind a stack trace in a log.
    """
    from hcc.updates.signing import (
        SignatureStatus,
        SigningError,
        TrustStore,
        read_signature,
        verify_manifest,
    )

    path = settings.agent_release_manifest
    if path is None:
        return _ManifestState(
            manifest=None,
            signature_status=SignatureStatus.ABSENT.value,
            signature_detail="no release metadata is configured",
            authentic=False,
        )
    try:
        manifest = load_release_manifest(path)
    except ManifestError as error:
        logger.warning("update_manifest_unusable", detail=str(error))
        return _ManifestState(
            manifest=None,
            signature_status=SignatureStatus.MALFORMED.value,
            signature_detail=str(error),
            authentic=False,
        )

    try:
        signature = read_signature(path)
    except SigningError as error:
        return _ManifestState(
            manifest=manifest,
            signature_status=SignatureStatus.MALFORMED.value,
            signature_detail=str(error),
            authentic=False,
        )

    store = TrustStore(settings.release_signing_keys)
    verification = verify_manifest(path.read_bytes(), signature, store)
    return _ManifestState(
        manifest=manifest,
        signature_status=verification.status.value,
        signature_detail=verification.detail,
        authentic=verification.is_authentic,
    )


@read_router.get(
    "/updates/overview",
    summary="Fleet update status",
    response_model=FleetOverviewResponse,
)
async def fleet_overview(
    settings: SettingsDep,
    updates: UpdateServiceDep,
    database: DatabaseDep,
) -> FleetOverviewResponse:
    """What the fleet is running, and what is available.

    Available to every authenticated role: a viewer who cannot see that the
    fleet is two versions behind cannot do their job.
    """
    metadata = await _manifest_and_signature(settings)
    async with database.session_factory() as db:
        revisions = await applied_revisions(db)
        revision = await current_revision(db)
        overview = await updates.fleet_overview(
            db,
            manifest=metadata.manifest,
            metadata_authentic=metadata.authentic,
            applied_schema_revisions=revisions,
        )
    return _overview_response(
        overview, revision, metadata.signature_status, metadata.signature_detail
    )


def _overview_response(
    overview: FleetUpdateOverview,
    revision: str | None,
    signature_status: str,
    signature_detail: str | None,
) -> FleetOverviewResponse:
    return FleetOverviewResponse(
        total_hosts=overview.total_hosts,
        up_to_date=overview.up_to_date,
        update_available=overview.update_available,
        blocked=overview.blocked,
        needs_attention=overview.needs_attention,
        version_distribution=[
            VersionCountResponse(version=version, hosts=count)
            for version, count in overview.version_distribution
        ],
        hosts_with_automatic_updates=overview.hosts_with_automatic_updates,
        open_jobs=overview.open_jobs,
        failed_recently=overview.failed_recently,
        controller_version=__version__,
        controller_schema_revision=revision,
        metadata_available=overview.metadata_available,
        metadata_detail=overview.metadata_detail,
        metadata_signature=signature_status,
        metadata_signature_detail=signature_detail,
        channels=list(CHANNELS),
    )


@read_router.get(
    "/updates/hosts",
    summary="Per-host update status",
    response_model=list[HostUpdateStatusResponse],
)
async def host_update_statuses(
    settings: SettingsDep,
    updates: UpdateServiceDep,
    database: DatabaseDep,
) -> list[HostUpdateStatusResponse]:
    """Every host's update situation, for the table.

    Not paginated, deliberately: the fleet is bounded at 500 connections by
    ``MAX_CONNECTIONS`` and this is one row per host with no joins per row.
    Paginating it would add a cursor to a page that wants to be sorted and
    filtered in the browser.
    """
    from sqlalchemy import select

    from hcc.models.host import Host

    metadata = await _manifest_and_signature(settings)
    statuses: list[HostUpdateStatusResponse] = []
    async with database.session_factory() as db:
        revisions = await applied_revisions(db)
        host_ids = (
            (await db.execute(select(Host.id).where(Host.is_enabled.is_(True)).order_by(Host.name)))
            .scalars()
            .all()
        )
        for host_id in host_ids:
            status_ = await updates.host_status(
                db,
                host_id,
                manifest=metadata.manifest,
                metadata_authentic=metadata.authentic,
                applied_schema_revisions=revisions,
            )
            statuses.append(HostUpdateStatusResponse.of(status_))
    return statuses


@read_router.get(
    "/hosts/{host_id}/updates",
    summary="One host's update status",
    response_model=HostUpdateStatusResponse,
)
async def host_update_status(
    host_id: uuid.UUID,
    settings: SettingsDep,
    updates: UpdateServiceDep,
    database: DatabaseDep,
) -> HostUpdateStatusResponse:
    metadata = await _manifest_and_signature(settings)
    async with database.session_factory() as db:
        revisions = await applied_revisions(db)
        try:
            status_ = await updates.host_status(
                db,
                host_id,
                manifest=metadata.manifest,
                metadata_authentic=metadata.authentic,
                applied_schema_revisions=revisions,
            )
        except HostNotFoundError as error:
            raise NotFoundError("No such host.") from error
    return HostUpdateStatusResponse.of(status_)


@read_router.get(
    "/updates/history",
    summary="Update history",
    response_model=UpdateHistoryResponse,
)
async def update_history(
    updates: UpdateServiceDep,
    database: DatabaseDep,
    host_id: Annotated[uuid.UUID | None, Query(description="One host, or the fleet.")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_HISTORY_LIMIT)] = 50,
    before: Annotated[
        str | None, Query(description="An ISO-8601 instant from a previous page.")
    ] = None,
) -> UpdateHistoryResponse:
    """Finished and in-flight attempts, newest first."""
    cursor: datetime | None = None
    if before is not None:
        try:
            cursor = datetime.fromisoformat(before)
        except ValueError as error:
            raise ValidationFailedError(
                "`before` must be an ISO-8601 instant, e.g. 2026-10-14T12:00:00Z."
            ) from error
        if cursor.tzinfo is None:
            raise ValidationFailedError("`before` must carry a time zone.")

    async with database.session_factory() as db:
        jobs = await updates.history(db, host_id=host_id, limit=limit, before=cursor)
    # The cursor is the oldest row's timestamp, and only when the page was
    # full — otherwise there is nothing after it and offering a cursor would
    # mean one more request to learn that.
    next_before = jobs[-1].created_at.isoformat() if len(jobs) == limit else None
    return UpdateHistoryResponse(
        jobs=[UpdateJobResponse.of(job) for job in jobs], next_before=next_before
    )


@read_router.get(
    "/updates/jobs/{job_id}/events",
    summary="One update's transitions",
    response_model=list[UpdateEventResponse],
)
async def update_events(
    job_id: uuid.UUID,
    updates: UpdateServiceDep,
    database: DatabaseDep,
) -> list[UpdateEventResponse]:
    """How an update got where it did, oldest first.

    "It reached staged and then failed" and "it never got past queued" are
    different problems with the same final state, and this is where that
    difference lives.
    """
    async with database.session_factory() as db:
        events = await updates.events_for(db, job_id)
    if not events:
        raise NotFoundError("No such update job, or it has no recorded transitions.")
    return [_event_response(event) for event in events]


def _event_response(event: UpdateEvent) -> UpdateEventResponse:
    return UpdateEventResponse(
        sequence=event.id,
        state=event.state,
        detail=event.detail,
        source=event.source,
        created_at=event.created_at.isoformat(),
    )


def _window_response(window: MaintenanceWindow | None) -> MaintenanceWindowResponse | None:
    if window is None:
        return None
    return MaintenanceWindowResponse(
        timezone=window.timezone,
        weekdays=list(window.weekdays),
        start=window.start.strftime("%H:%M"),
        end=window.end.strftime("%H:%M"),
        description=window.describe(),
    )


# ---------------------------------------------------------------------------
# Writing. Administrator only.
# ---------------------------------------------------------------------------


@router.put(
    "/hosts/{host_id}/update-policy",
    summary="Set a host's update policy",
    response_model=HostUpdateStatusResponse,
)
async def set_update_policy(
    host_id: uuid.UUID,
    body: UpdatePolicyRequest,
    settings: SettingsDep,
    updates: UpdateServiceDep,
    database: DatabaseDep,
    audit: AuditContextDep,
) -> HostUpdateStatusResponse:
    """Change what this host may do by itself. Audited.

    The audit row records whether the result is automatic, because the moment
    a host starts updating itself is the fact somebody will want to find.
    """
    window: MaintenanceWindow | None = None
    if body.maintenance_window is not None:
        try:
            window = body.maintenance_window.to_window()
        except WindowError as error:
            raise ValidationFailedError(str(error)) from error

    metadata = await _manifest_and_signature(settings)
    async with database.session_factory() as db:
        try:
            await updates.set_policy(
                db,
                host_id,
                context=audit,
                policy=Policy(body.policy),
                channel=body.channel,
                pinned_version=body.pinned_version,
                window=window,
            )
        except HostNotFoundError as error:
            await db.rollback()
            raise NotFoundError("No such host.") from error
        except UpdateServiceError as error:
            await db.rollback()
            raise ValidationFailedError(str(error)) from error
        await db.commit()

        revisions = await applied_revisions(db)
        status_ = await updates.host_status(
            db,
            host_id,
            manifest=metadata.manifest,
            metadata_authentic=metadata.authentic,
            applied_schema_revisions=revisions,
        )
    return HostUpdateStatusResponse.of(status_)


@router.post(
    "/hosts/{host_id}/updates",
    summary="Start an update on one host",
    status_code=status.HTTP_201_CREATED,
    response_model=UpdateJobResponse,
)
async def start_update(
    host_id: uuid.UUID,
    body: StartUpdateRequest,
    settings: SettingsDep,
    updates: UpdateServiceDep,
    database: DatabaseDep,
    audit: AuditContextDep,
) -> UpdateJobResponse:
    """Queue an update. Administrator only, and audited.

    The host is notified on its next heartbeat and asks for the instruction;
    nothing is pushed to a host that cannot receive it.
    """
    metadata = await _manifest_and_signature(settings)
    async with database.session_factory() as db:
        revisions = await applied_revisions(db)
        try:
            job = await updates.start_update(
                db,
                host_id,
                manifest=metadata.manifest,
                metadata_authentic=metadata.authentic,
                applied_schema_revisions=revisions,
                trigger=UpdateTrigger.MANUAL,
                context=audit,
                target_version=body.target_version,
                allow_downgrade=body.allow_downgrade,
            )
        except HostNotFoundError as error:
            await db.rollback()
            raise NotFoundError("No such host.") from error
        except UpdateAlreadyRunningError as error:
            await db.rollback()
            raise UpdateAlreadyRunningConflictError(str(error)) from error
        except UpdateNotPossibleError as error:
            await db.rollback()
            # The blockers are the useful part, so they are in the message
            # rather than flattened away — the interface shows this sentence.
            raise UpdateNotPossibleValidationError(str(error)) from error
        await db.commit()
        return UpdateJobResponse.of(job)


@router.post(
    "/updates/jobs/{job_id}/cancel",
    summary="Cancel an update",
    response_model=UpdateJobResponse,
)
async def cancel_update(
    job_id: uuid.UUID,
    body: CancelUpdateRequest,
    updates: UpdateServiceDep,
    database: DatabaseDep,
    audit: AuditContextDep,
) -> UpdateJobResponse:
    """Stop an update that has not got too far.

    Refused from ``installing`` onwards: the binary is being replaced on a
    machine the controller cannot reach into, so cancelling would be a promise
    it cannot keep. The agent's own rollback is what recovers from there, and
    the refusal says so.
    """
    async with database.session_factory() as db:
        try:
            job = await updates.cancel(
                db,
                job_id,
                context=audit,
                reason=body.reason,
            )
        except UpdateNotFoundError as error:
            await db.rollback()
            raise NotFoundError("No such update job.") from error
        except UpdateNotCancellableError as error:
            await db.rollback()
            raise UpdateNotCancellableConflictError(str(error)) from error
        await db.commit()
        return UpdateJobResponse.of(job)


@router.post(
    "/updates/expire",
    summary="Expire stale update instructions",
    response_model=list[UpdateJobResponse],
)
async def expire_stale_updates(
    updates: UpdateServiceDep,
    database: DatabaseDep,
) -> list[UpdateJobResponse]:
    """Fail updates whose instruction expired without finishing.

    Exposed because the scheduler is not implemented yet and an administrator
    needs a way to clear a host whose agent never came back — stated rather
    than implied, so nobody mistakes this endpoint for the mechanism. When the
    reconciliation loop lands this becomes a manual nudge rather than the only
    path.
    """
    async with database.session_factory() as db:
        expired = await updates.expire_stale(db, now=datetime.now(UTC))
        await db.commit()
    return [UpdateJobResponse.of(job) for job in expired]


# ---------------------------------------------------------------------------
# Staged rollouts
#
# Evergreen created the tables and nothing drove one. These are the endpoints
# for hcc/services/rollouts.py — create, inspect, pause, resume, cancel.
#
# Creating a rollout starts nothing. A created rollout is `pending` until a
# background pass picks it up, which is what makes "create it now, let it run
# inside its window tonight" work and what keeps the request that creates it
# from performing forty installs synchronously.
# ---------------------------------------------------------------------------


class RolloutMemberResponse(BaseModel):
    """One host's place in a rollout, and where it has got to."""

    host_id: uuid.UUID
    batch: int = Field(description="0 is the canary; 1 and up are the batches, in order.")
    state: str = Field(description="pending | running | succeeded | failed | skipped")
    healthy: bool = Field(
        description=(
            "Whether the post-install health check has passed. False while a host "
            "is still settling, and false for one that installed and then went "
            "away — which is the distinction the next batch waits on."
        )
    )
    detail: str | None = None


class RolloutResponse(BaseModel):
    """A rollout and its current progress."""

    rollout_id: uuid.UUID
    name: str
    to_version: str
    state: str = Field(description="pending | canary | running | paused | completed | cancelled")
    canary_count: int
    batch_size: int
    failure_tolerance: int
    health_check_seconds: int
    offline_deadline_seconds: int
    failures_overridden: bool
    window: MaintenanceWindowResponse | None = None
    paused_reason: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    total: int
    succeeded: int
    failed: int
    skipped: int
    running: int
    pending: int
    current_batch: int | None = None
    members: list[RolloutMemberResponse] = Field(default_factory=list)

    @classmethod
    def of(
        cls,
        rollout: UpdateRollout,
        outcomes: list[MemberOutcome],
        window: MaintenanceWindow | None,
    ) -> RolloutResponse:
        unfinished = [outcome for outcome in outcomes if not outcome.is_finished]
        return cls(
            rollout_id=rollout.id,
            name=rollout.name,
            to_version=rollout.to_version,
            state=rollout.state,
            canary_count=rollout.canary_count,
            batch_size=rollout.batch_size,
            failure_tolerance=rollout.failure_tolerance,
            health_check_seconds=rollout.health_check_seconds,
            offline_deadline_seconds=rollout.offline_deadline_seconds,
            failures_overridden=rollout.failures_overridden,
            window=(
                None
                if window is None
                else MaintenanceWindowResponse(
                    timezone=window.timezone,
                    weekdays=list(window.weekdays),
                    start=window.start.strftime("%H:%M"),
                    end=window.end.strftime("%H:%M"),
                    description=window.describe(),
                )
            ),
            paused_reason=rollout.paused_reason,
            created_at=rollout.created_at,
            started_at=rollout.started_at,
            finished_at=rollout.finished_at,
            total=len(outcomes),
            succeeded=sum(1 for o in outcomes if o.state == "succeeded"),
            failed=sum(1 for o in outcomes if o.state == "failed"),
            skipped=sum(1 for o in outcomes if o.state == "skipped"),
            running=sum(1 for o in outcomes if o.state == "running"),
            pending=sum(1 for o in outcomes if o.state == "pending"),
            current_batch=min((o.batch for o in unfinished), default=None),
            members=[
                RolloutMemberResponse(
                    host_id=outcome.host_id,
                    batch=outcome.batch,
                    state=outcome.state,
                    healthy=outcome.healthy,
                    detail=outcome.detail,
                )
                for outcome in outcomes
            ],
        )


class CreateRolloutRequest(BaseModel):
    """Everything a rollout needs, with defaults that are the cautious ones."""

    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=120)
    to_version: str = Field(min_length=1, max_length=64)
    host_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_ROLLOUT_HOSTS)
    canary_host_ids: list[uuid.UUID] = Field(
        default_factory=list,
        max_length=MAX_ROLLOUT_HOSTS,
        description=(
            "Hosts to put in batch 0. Fewer than canary_count fills the rest "
            "from the connected members, because a powered-off canary tells "
            "nobody anything and would hold the rollout at the offline deadline."
        ),
    )
    canary_count: int = Field(default=1, ge=1, le=50)
    batch_size: int = Field(
        default=5,
        ge=1,
        le=100,
        description="How many hosts update at once after the canary, and so the concurrency limit.",
    )
    failure_tolerance: int = Field(
        default=0,
        ge=0,
        le=100,
        description=(
            "Failures accepted before the rollout pauses itself. Zero by default: "
            "the first failure stops it, which is what somebody rolling out to a "
            "fleet they care about wants. Skipped hosts do not count."
        ),
    )
    health_check_seconds: int = Field(
        default=120,
        ge=0,
        le=3600,
        description=(
            "How long a host must stay healthy after its install before the next "
            "batch starts. Zero skips the wait, which makes 'the process "
            "restarted' the only evidence there is."
        ),
    )
    offline_deadline_seconds: int = Field(default=3600, ge=60, le=7 * 24 * 3600)
    window: MaintenanceWindowRequest | None = None


class RolloutControlRequest(BaseModel):
    """Pausing, resuming or cancelling, with a reason for the audit trail."""

    model_config = {"extra": "forbid"}

    reason: str = Field(default="", max_length=500)
    override_failures: bool = Field(
        default=False,
        description=(
            "On resume: go on despite the failures that paused it. Without "
            "this, a rollout stopped by the failure gate pauses again on the "
            "next pass — which is correct and would otherwise look like a bug."
        ),
    )


def get_rollout_service(connection: HTTPConnection) -> RolloutService:
    """Return the process-wide rollout service."""
    service: RolloutService = connection.app.state.rollout_service
    return service


RolloutServiceDep = Annotated[RolloutService, Depends(get_rollout_service)]


@read_router.get(
    "/updates/rollouts",
    summary="Every rollout, newest first",
    response_model=list[RolloutResponse],
)
async def list_rollouts(
    rollouts: RolloutServiceDep,
    database: DatabaseDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> list[RolloutResponse]:
    """List rollouts with their progress. Any authenticated role may read."""
    async with database.session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(UpdateRollout).order_by(UpdateRollout.created_at.desc()).limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [
            RolloutResponse.of(
                rollout,
                await rollouts.outcomes(db, rollout),
                rollouts.window_of(rollout),
            )
            for rollout in rows
        ]


@read_router.get(
    "/updates/rollouts/{rollout_id}",
    summary="One rollout and every member's state",
    response_model=RolloutResponse,
)
async def get_rollout(
    rollout_id: uuid.UUID,
    rollouts: RolloutServiceDep,
    database: DatabaseDep,
) -> RolloutResponse:
    async with database.session_factory() as db:
        try:
            rollout = await rollouts.get(db, rollout_id)
        except RolloutNotFoundError as error:
            raise NotFoundError("No such rollout.") from error
        return RolloutResponse.of(
            rollout, await rollouts.outcomes(db, rollout), rollouts.window_of(rollout)
        )


@router.post(
    "/updates/rollouts",
    summary="Create a staged rollout",
    status_code=status.HTTP_201_CREATED,
    response_model=RolloutResponse,
)
async def create_rollout(
    body: CreateRolloutRequest,
    rollouts: RolloutServiceDep,
    database: DatabaseDep,
    audit: AuditContextDep,
) -> RolloutResponse:
    """Create a rollout. Administrator only, and audited.

    **Nothing starts here.** The rollout is `pending` and a background pass
    starts its canary — within the maintenance window, if one was given.
    """
    async with database.session_factory() as db:
        try:
            window = None if body.window is None else body.window.to_window()
        except WindowError as error:
            raise ValidationFailedError(str(error)) from error
        try:
            rollout = await rollouts.create(
                db,
                name=body.name,
                to_version=body.to_version,
                host_ids=body.host_ids,
                canary_host_ids=body.canary_host_ids,
                canary_count=body.canary_count,
                batch_size=body.batch_size,
                failure_tolerance=body.failure_tolerance,
                health_check_seconds=body.health_check_seconds,
                offline_deadline_seconds=body.offline_deadline_seconds,
                window=window,
                context=audit,
            )
        except RolloutInvalidError as error:
            await db.rollback()
            raise ValidationFailedError(str(error)) from error
        outcomes = await rollouts.outcomes(db, rollout)
        response = RolloutResponse.of(rollout, outcomes, rollouts.window_of(rollout))
        await db.commit()
        return response


@router.post(
    "/updates/rollouts/{rollout_id}/pause",
    summary="Stop starting new hosts",
    response_model=RolloutResponse,
)
async def pause_rollout(
    rollout_id: uuid.UUID,
    body: RolloutControlRequest,
    rollouts: RolloutServiceDep,
    database: DatabaseDep,
    audit: AuditContextDep,
) -> RolloutResponse:
    """Pause. In-flight installs finish; nothing new starts.

    Deliberately not a cancellation of what is running: an install halfway
    through replacing a binary is the worst moment to abandon, and the
    agent's own rollback is what handles a bad one.
    """
    async with database.session_factory() as db:
        try:
            rollout = await rollouts.pause(
                db, rollout_id, reason=body.reason or "paused by an administrator"
            )
        except RolloutNotFoundError as error:
            raise NotFoundError("No such rollout.") from error
        except RolloutNotResumableError as error:
            await db.rollback()
            raise ValidationFailedError(str(error)) from error
        response = RolloutResponse.of(
            rollout, await rollouts.outcomes(db, rollout), rollouts.window_of(rollout)
        )
        await db.commit()
        return response


@router.post(
    "/updates/rollouts/{rollout_id}/resume",
    summary="Start it again",
    response_model=RolloutResponse,
)
async def resume_rollout(
    rollout_id: uuid.UUID,
    body: RolloutControlRequest,
    rollouts: RolloutServiceDep,
    database: DatabaseDep,
    audit: AuditContextDep,
) -> RolloutResponse:
    """Resume a paused rollout.

    `override_failures` is the administrator saying "I have looked at the
    canary that failed and I want the rest anyway". It is recorded on the
    rollout rather than implied by the resume, because "resumed after a
    failure" and "resumed after an administrator paused it" are different
    facts and the second must not be able to look like the first.
    """
    async with database.session_factory() as db:
        try:
            rollout = await rollouts.resume(
                db, rollout_id, override_failures=body.override_failures
            )
        except RolloutNotFoundError as error:
            raise NotFoundError("No such rollout.") from error
        except RolloutNotResumableError as error:
            await db.rollback()
            raise ValidationFailedError(str(error)) from error
        response = RolloutResponse.of(
            rollout, await rollouts.outcomes(db, rollout), rollouts.window_of(rollout)
        )
        await db.commit()
        return response


@router.post(
    "/updates/rollouts/{rollout_id}/cancel",
    summary="Abandon a rollout",
    response_model=RolloutResponse,
)
async def cancel_rollout(
    rollout_id: uuid.UUID,
    body: RolloutControlRequest,
    rollouts: RolloutServiceDep,
    database: DatabaseDep,
    audit: AuditContextDep,
) -> RolloutResponse:
    """Cancel. Members that have not started never will.

    In-flight jobs are **not** cancelled: the job has its own cancel, per
    host, and reaching for a fleet-level button to abandon an install that is
    replacing a binary is a bigger hammer than anybody wants.
    """
    async with database.session_factory() as db:
        try:
            rollout = await rollouts.cancel(
                db, rollout_id, reason=body.reason or "cancelled by an administrator"
            )
        except RolloutNotFoundError as error:
            raise NotFoundError("No such rollout.") from error
        response = RolloutResponse.of(
            rollout, await rollouts.outcomes(db, rollout), rollouts.window_of(rollout)
        )
        await db.commit()
        return response


# ---------------------------------------------------------------------------
# Controller upgrade readiness
#
# Read-only, and that is the design rather than a limitation. Replacing the
# controller means replacing the container this endpoint is served from, and
# for the application to do that to itself it would need the Docker socket —
# which is root on the host. A web application with root on its host is a
# worse problem than any upgrade it could perform.
#
# So the controller reports what an upgrade would involve and
# `scripts/upgrade-controller.sh` performs it, run by an administrator on the
# machine with the privileges they already have. There is no remote-command
# API here for the same reason: an endpoint that runs what it is told is what
# an attacker is looking for.
# ---------------------------------------------------------------------------

#: Every update state, for the "is anything in flight" count.
#:
#: Enumerated from the enum rather than written out in SQL, because
#: `is_terminal` is the one place that knows which states are terminal and a
#: second list would be a copy that drifts.
_ALL_UPDATE_STATES: Final = tuple(state.value for state in UpdateState)


class UpgradeReadinessResponse(BaseModel):
    """What an administrator needs before upgrading the controller."""

    controller_version: str
    schema_revision: str | None = Field(
        description="What the database reports. Null on an unmigrated database."
    )
    head_revision: str | None = Field(
        description=(
            "The newest revision this build's migration files contain. Null "
            "means the history could not be read, which is 'cannot tell' "
            "rather than 'nothing pending'."
        )
    )
    pending_revisions: list[str] = Field(
        description=(
            "Migrations this build has and the database has not, in the order they would run."
        )
    )
    schema_is_current: bool = Field(
        description=(
            "Nothing pending, nothing ahead, and the history was readable. "
            "False when this build cannot read its own migrations: 'cannot "
            "tell' is nearer to 'not current' than to 'current', and this "
            "is the field a script would check before upgrading."
        )
    )
    schema_is_ahead: bool = Field(
        description=(
            "The database reports a revision this build does not contain: an "
            "older image against a newer schema. The dangerous direction, and "
            "the one no backup taken now would recover."
        )
    )
    connected_hosts: int = Field(
        description="Agents connected right now. They reconnect by themselves after a restart."
    )
    hosts_with_open_updates: int = Field(
        description=(
            "Hosts with an update in flight. Restarting the controller during "
            "one does not break it — the agent reports the result on its next "
            "connection — but it is worth knowing before choosing the moment."
        )
    )
    advice: list[str] = Field(
        description="Sentences for an operator, strongest first. Never empty."
    )


@read_router.get(
    "/controller/upgrade-readiness",
    summary="What a controller upgrade would involve",
    response_model=UpgradeReadinessResponse,
)
async def upgrade_readiness(database: DatabaseDep) -> UpgradeReadinessResponse:
    """Report, never act. See the note above this handler.

    Available to every authenticated role: "is the controller current" is a
    question anybody looking after this installation may ask, and the answer
    holds no secret — a schema revision and two counts.
    """
    from sqlalchemy import func as sql_func

    from hcc.models.host import Host
    from hcc.models.updates import UpdateJob, is_terminal
    from hcc.updates.schema import (
        current_revision,
        head_revision,
        knows_revision,
        pending_revisions,
    )

    unfinished = [state for state in _ALL_UPDATE_STATES if not is_terminal(state)]

    async with database.session_factory() as db:
        revision = await current_revision(db)
        applied = await applied_revisions(db)
        head = head_revision()
        pending = list(pending_revisions(applied))

        connected = (
            await db.execute(
                select(sql_func.count())
                .select_from(Host)
                .where(Host.current_connection_id.is_not(None))
            )
        ).scalar_one()
        open_updates = (
            await db.execute(
                select(sql_func.count())
                .select_from(UpdateJob)
                .where(UpdateJob.state.in_(unfinished))
            )
        ).scalar_one()

    # The database is ahead when it reports a revision this build has never
    # heard of. Checked before `schema_is_current`, because it is the case
    # that looks current from a count of pending migrations and is not.
    #
    # Asked of the history directly rather than of `applied`: that reports an
    # unknown head as the head alone — deliberately, so a blocker can name it
    # — which would make an unknown revision look applied.
    ahead = revision is not None and not knows_revision(revision)
    # `head_revision()` is None when this build cannot read its own migration
    # history. That is not "nothing is pending" and must not be reported as
    # it: with the history unreadable, `knows_revision` fails open and
    # `pending_revisions` is empty, so every signal below points at "current"
    # — which is how a production image once reported "the schema is current"
    # against a database from the future. See hcc/updates/schema.py.
    unreadable = head is None
    advice: list[str] = []
    if unreadable:
        advice.append(
            "This build cannot read its own migration history, so it cannot "
            "tell whether the schema is current. That is a packaging fault "
            "rather than a database one: check the image contains its "
            "`alembic/` directory. Do not read the absence of pending "
            "migrations below as a reason to upgrade."
        )
    if ahead:
        advice.append(
            f"This database reports schema revision {revision}, which this build "
            f"({__version__}) does not contain. It was migrated by a newer "
            "controller. Running an older image against a newer schema is the one "
            "direction a backup taken now cannot recover; deploy the newer image "
            "again."
        )
    elif pending:
        advice.append(
            f"{len(pending)} migration(s) would run: {', '.join(pending)}. "
            "Migrations are applied once and are not reversed automatically — take "
            "a backup first, and verify that it restores."
        )
    if open_updates:
        advice.append(
            f"{open_updates} host update(s) are in flight. Restarting the controller "
            "does not break them — the agent reports the result on its next "
            "connection — but waiting makes the history easier to read."
        )
    if not advice:
        advice.append("The schema is current and nothing is in flight.")

    return UpgradeReadinessResponse(
        controller_version=__version__,
        schema_revision=revision,
        head_revision=head,
        pending_revisions=pending,
        # False when the history is unreadable: "cannot tell" is nearer to
        # "not current" than to "current", and this field is what an
        # operator — or a script — reads to decide it is safe to proceed.
        schema_is_current=not pending and not ahead and not unreadable,
        schema_is_ahead=ahead,
        connected_hosts=int(connected),
        hosts_with_open_updates=int(open_updates),
        advice=advice,
    )
