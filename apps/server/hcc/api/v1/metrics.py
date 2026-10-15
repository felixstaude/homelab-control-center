"""Metric query endpoints: the latest values, the series, and history.

Phase 3, implementing §20.

## Paths

    GET /api/v1/hosts/{host_id}/metrics/latest    every role
    GET /api/v1/hosts/{host_id}/metrics/series    every role
    GET /api/v1/hosts/{host_id}/metrics/range     every role
    GET /api/v1/hosts/{host_id}/metrics/status    every role

All reads, all on ``read_router``, so all open to every authenticated role.
There is nothing to write here: metrics arrive through the agent connection,
and an endpoint that accepted a measurement from a browser session would be a
way to fabricate history — which is the thing
``docs/AGENT_ARCHITECTURE.md`` §4 says a monitoring system must not allow.

## Nothing here invents a value

The rule the whole module is shaped around, and it shows up as four specific
absences:

* **A host with no samples returns an empty list**, not zeroes. §21: do not
  display zero when the actual value is unknown.
* **A stale value is absent from ``latest``** rather than returned without
  comment. A reading from three days ago is not "the current CPU usage", and
  an interface handed one has no way to know it should not show it. The
  freshness window is a parameter with a default, and the response states it.
* **Empty buckets are absent, never interpolated.** A gap in a series is a gap
  (``DATA_RETENTION.md`` §5.3); the interface draws a break rather than a
  line through it.
* **The tier that answered is reported.** "This is hourly data" is information
  an operator needs in order to read a flat-looking graph, and silently
  serving a different resolution than expected is how people draw wrong
  conclusions.

## The range endpoint takes a series id, and checks the host

A series id is a sequential ``BIGINT``, so an operator who can read one host's
charts could read every host's by incrementing a number. The service checks
the host on every range query and the path carries it, so the check cannot be
bypassed by calling the service directly either.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Final

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hcc.api.deps import MetricsServiceDep, SessionDep, ViewerDep
from hcc.errors import NotFoundError, ValidationFailedError
from hcc.models.host import Host
from hcc.models.metrics import TIERS
from hcc.monitoring import modules as registry
from hcc.services.metrics import (
    DEFAULT_QUERY_POINTS,
    MAX_QUERY_POINTS,
    LatestSample,
    QueryRangeError,
    RangeResult,
    SeriesDescriptor,
)

#: Read routes. Included into ``_protected``: every authenticated role.
read_router = APIRouter(tags=["metrics"])

#: How recent a sample must be to count as "current", by default.
#:
#: Five minutes. Longer than the slowest default interval (``uptime``, five
#: minutes) so a host collecting at its defaults always has current values,
#: and short enough that a host whose agent died half an hour ago shows no
#: numbers rather than stale ones.
DEFAULT_FRESHNESS_SECONDS: Final = 300

#: Bounds on the freshness window a caller may ask for.
#:
#: The lower bound stops a caller asking for a one-second window and getting
#: an empty response they then interpret as "the host is not collecting". The
#: upper bound is a day, past which "latest" has stopped meaning anything an
#: operator would call current.
MIN_FRESHNESS_SECONDS: Final = 10
MAX_FRESHNESS_SECONDS: Final = 24 * 60 * 60

#: Longest range a single query may cover.
#:
#: Ninety days, matching the five-minute tier's default retention. A longer
#: window would be served from the hourly tier, which is legitimate — but the
#: bound exists because the *planning* cost grows with the number of
#: partitions a query touches, and a request for ten years would touch every
#: one that has ever existed.
MAX_RANGE_DAYS: Final = 90


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SeriesResponse(BaseModel):
    """One measurable stream this host has produced."""

    series_id: int = Field(
        description=(
            "Stable identifier for this series, used by the range endpoint. "
            "Sequential, which is why that endpoint also takes the host."
        )
    )
    module: str
    metric: str
    labels: dict[str, str] = Field(
        description="The declared dimensions, e.g. {'core': '3'}. Empty for most series."
    )
    unit: str = Field(description="percent | bytes | seconds | count | load.")
    kind: str = Field(description="gauge | counter.")
    module_name: str = Field(description="Human-readable module name, for the interface.")
    metric_name: str = Field(description="Human-readable metric name.")
    last_sample_at: str | None = Field(
        description=(
            "When this series last received a sample, or null if it never has. "
            "Null must not be rendered as a date."
        )
    )


class SeriesListResponse(BaseModel):
    """Every series a host has ever produced.

    Includes series that have stopped reporting, so that "this host has no CPU
    data" and "this host had CPU data until Tuesday" are different answers.
    """

    host_id: str
    series: list[SeriesResponse]


class LatestValueResponse(BaseModel):
    """The most recent value of one series."""

    series: SeriesResponse
    value: float
    recorded_at: str


class LatestResponse(BaseModel):
    """A host's current values.

    Empty when the host has produced nothing recent — not a list of zeroes.
    The interface renders "no data", which is the honest answer and is
    distinguishable from a measurement of zero.
    """

    host_id: str
    freshness_seconds: int = Field(
        description=(
            "How recent a sample had to be to appear here. Reported so the "
            "interface can say 'no data in the last 5 minutes' rather than "
            "'no data', which are different claims."
        )
    )
    values: list[LatestValueResponse]


class RangePointResponse(BaseModel):
    """One point of a historical series.

    ``minimum`` and ``maximum`` are present only for a rolled-up tier. They
    are not decoration: a downsampled series drawn as a single line of
    averages loses the spikes the operator enabled fast sampling to catch, so
    the interface draws a min-max band and needs the bounds to do it.
    """

    at: str
    value: float
    minimum: float | None = None
    maximum: float | None = None
    sample_count: int | None = Field(
        default=None,
        description=(
            "How many raw samples this bucket aggregates. Present only for a "
            "rolled-up tier, and what makes a partially-filled bucket visible "
            "rather than indistinguishable from a full one."
        ),
    )


class RangeResponse(BaseModel):
    """A historical series over a time range."""

    series: SeriesResponse
    resolution: str = Field(
        description=(
            "raw | 5m | 1h — which tier answered. Reported because 'this is "
            "hourly data' is information needed to interpret a flat-looking "
            "graph."
        )
    )
    start: str
    end: str
    points: list[RangePointResponse] = Field(
        description=(
            "Ordered by time, with **no points for empty buckets**. A gap is a "
            "gap: nothing here is interpolated, so the interface draws a break "
            "rather than a line through a period when nothing was collected."
        )
    )
    truncated: bool = Field(
        description=(
            "Whether the point budget cut the series short. Only reachable "
            "when the caller named a resolution the range does not suit; the "
            "automatic choice always fits."
        )
    )


class ModuleStatusResponse(BaseModel):
    """What one module is doing on one host.

    The last five fields answer a question the first four could not: **is the
    collector failing?** A module with ``series_count`` of zero and a
    ``last_sample_at`` of null could be one nobody enabled, one enabled a
    second ago, or one that has raised the same error every interval for a
    week. Those need different actions and used to look identical.
    """

    module: str
    module_name: str
    series_count: int = Field(description="How many series this module has produced.")
    last_sample_at: str | None = Field(
        description=(
            "When this module last produced a sample, on the *agent's* clock, "
            "or null if it never has."
        )
    )

    # Below here: the controller's clock, deliberately. A host whose clock is
    # wrong is one of the conditions these fields report, and a diagnostic
    # timestamped by the thing being diagnosed is not a diagnostic.
    last_reported_at: str | None = Field(
        default=None,
        description=(
            "When the controller last heard anything about this module — a "
            "successful collection or a failed one. Null means this host has "
            "never reported it, which is not the same as healthy."
        ),
    )
    last_success_at: str | None = Field(
        default=None,
        description=(
            "When a collection last arrived without an error. A collection "
            "that ran and produced no samples counts: a rate collector's "
            "first interval legitimately produces nothing."
        ),
    )
    last_error_at: str | None = Field(
        default=None, description="When the last failure arrived, or null if none ever has."
    )
    last_error: str | None = Field(
        default=None,
        description=(
            "Why the last collection produced nothing, usually in the agent's "
            "own words. Kept after a later success — only the failure count "
            "resets — so a module that fails and recovers every few minutes "
            "is visible rather than self-erasing. Untrusted text from the "
            "host: display it, do not interpret it."
        ),
    )
    consecutive_failures: int = Field(
        default=0,
        description=(
            "Failures since the last success. Zero both for a module that is "
            "working and for one that has never reported; `last_reported_at` "
            "tells those apart."
        ),
    )


class MetricsStatusResponse(BaseModel):
    """Whether anything is actually arriving.

    The answer to the question an operator asks before looking at a chart, and
    the one a chart cannot answer. An empty chart has four possible causes and
    only one of them is visible from the data:

    1. **Nothing has been collected** — this endpoint.
    2. **Collection is switched off** — the monitoring endpoint.
    3. **The agent is offline** — the host endpoint.
    4. **Collection is on, the agent is connected, and the collector is
       failing every interval** — this endpoint too, through
       :class:`ModuleStatusResponse`'s error fields.

    The fourth was unreported until Phase M. The agent had always sent the
    reason; the controller parsed it and threw it away.
    """

    host_id: str
    sample_count: int = Field(
        description="Raw samples stored for this host, across every series and partition."
    )
    series_count: int
    last_sample_at: str | None = Field(
        description="The newest sample across every series, or null if there are none."
    )
    modules: list[ModuleStatusResponse] = Field(
        description=(
            "Every module in the registry, including those that have produced "
            "nothing — so the interface can distinguish 'this module has no "
            "data' from 'this module does not exist'."
        )
    )


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _series_response(series: SeriesDescriptor) -> SeriesResponse:
    """Decorate a stored series with its registry names.

    The unit and kind come from the *stored* series rather than the registry,
    so a historical series keeps the unit it was recorded in even if a later
    release changes the module's. The human-readable names come from the
    registry, because they are presentation and a renamed module should show
    its new name everywhere.
    """
    module_name = series.module
    metric_name = series.metric
    try:
        descriptor = registry.descriptor(series.module)
    except registry.UnknownModuleError:
        # A series for a module this release no longer has. Returned with its
        # identifiers as names rather than hidden: the data exists and an
        # operator should be able to see it, and inventing a friendly name for
        # something the registry cannot describe would be worse than the
        # identifier.
        pass
    else:
        module_name = descriptor.name
        if (metric := descriptor.metric(series.metric)) is not None:
            metric_name = metric.name

    return SeriesResponse(
        series_id=series.id,
        module=series.module,
        metric=series.metric,
        labels=series.labels,
        unit=series.unit,
        kind=series.kind,
        module_name=module_name,
        metric_name=metric_name,
        last_sample_at=_iso(series.last_sample_at),
    )


def _latest_response(sample: LatestSample) -> LatestValueResponse:
    return LatestValueResponse(
        series=_series_response(sample.series),
        value=sample.value,
        recorded_at=sample.recorded_at.isoformat(),
    )


def _range_response(result: RangeResult, start: datetime, end: datetime) -> RangeResponse:
    return RangeResponse(
        series=_series_response(result.series),
        resolution=result.tier,
        start=start.isoformat(),
        end=end.isoformat(),
        points=[
            RangePointResponse(
                at=point.at.isoformat(),
                value=point.value,
                minimum=point.minimum,
                maximum=point.maximum,
                sample_count=point.sample_count,
            )
            for point in result.points
        ],
        truncated=result.truncated,
    )


async def _require_host(db: AsyncSession, host_id: uuid.UUID) -> Host:
    """Load a host or raise a 404.

    Every endpoint here starts with this, so a metric query for a host that
    does not exist is a 404 rather than an empty series — which would look
    like a host that exists and has collected nothing.
    """
    host = (await db.execute(select(Host).where(Host.id == host_id))).scalar_one_or_none()
    if host is None:
        raise NotFoundError("No such host.")
    return host


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@read_router.get(
    "/hosts/{host_id}/metrics/latest",
    summary="A host's current metric values",
    response_model=LatestResponse,
    responses={404: {"description": "No such host."}},
)
async def latest_metrics(
    db: SessionDep,
    metrics: MetricsServiceDep,
    _session: ViewerDep,
    host_id: uuid.UUID,
    freshness_seconds: Annotated[
        int,
        Query(
            ge=MIN_FRESHNESS_SECONDS,
            le=MAX_FRESHNESS_SECONDS,
            description=(
                "How recent a sample must be to count as current. A series "
                "whose newest sample is older is absent rather than returned "
                "stale."
            ),
        ),
    ] = DEFAULT_FRESHNESS_SECONDS,
) -> LatestResponse:
    """The newest value of each series, if it is recent enough to be current.

    The freshness window is the whole design of this endpoint. Returning a
    host's newest sample regardless of age would let the interface present a
    three-day-old CPU reading as the current one, and nothing in the response
    would say otherwise. An absent series renders as "no data", which is
    honest and distinguishable from a measurement of zero.
    """
    await _require_host(db, host_id)
    samples = await metrics.latest_for_host(
        db, host_id, within=timedelta(seconds=freshness_seconds)
    )
    return LatestResponse(
        host_id=str(host_id),
        freshness_seconds=freshness_seconds,
        values=[_latest_response(sample) for sample in samples],
    )


@read_router.get(
    "/hosts/{host_id}/metrics/series",
    summary="Every series a host has produced",
    response_model=SeriesListResponse,
    responses={404: {"description": "No such host."}},
)
async def list_series(
    db: SessionDep,
    metrics: MetricsServiceDep,
    _session: ViewerDep,
    host_id: uuid.UUID,
) -> SeriesListResponse:
    """What this host has data for, which is what a chart picker needs."""
    await _require_host(db, host_id)
    series = await metrics.series_for_host(db, host_id)
    return SeriesListResponse(
        host_id=str(host_id),
        series=[_series_response(entry) for entry in series],
    )


@read_router.get(
    "/hosts/{host_id}/metrics/range",
    summary="One series over a time range",
    response_model=RangeResponse,
    responses={
        404: {"description": "No such host, or no such series on this host."},
        422: {"description": "The range or the point budget was refused."},
    },
)
async def metric_range(
    db: SessionDep,
    metrics: MetricsServiceDep,
    _session: ViewerDep,
    host_id: uuid.UUID,
    series_id: Annotated[int, Query(ge=1, description="From the series endpoint.")],
    start: Annotated[datetime, Query(description="Inclusive start, ISO-8601 with a zone.")],
    end: Annotated[datetime, Query(description="Exclusive end, ISO-8601 with a zone.")],
    max_points: Annotated[
        int,
        Query(
            ge=1,
            le=MAX_QUERY_POINTS,
            description=(
                "Target point count. Decides which tier answers, unless `resolution` names one."
            ),
        ),
    ] = DEFAULT_QUERY_POINTS,
    resolution: Annotated[
        str | None,
        Query(
            description=(
                "raw | 5m | 1h. Omit to let the range and the point budget "
                "choose, which is what a caller should normally do — naming "
                "one can return a truncated series."
            )
        ),
    ] = None,
) -> RangeResponse:
    """A series' history, at a resolution the range allows.

    **A series id alone is not authorization.** The ids are sequential, so the
    host is checked too: a series belonging to another host answers 404, the
    same as one that does not exist. Told apart by nothing, deliberately — a
    distinct error would confirm that the id is real.
    """
    await _require_host(db, host_id)

    start, end = _aware(start, "start"), _aware(end, "end")
    if (end - start) > timedelta(days=MAX_RANGE_DAYS):
        raise ValidationFailedError(
            f"A range may cover at most {MAX_RANGE_DAYS} days.",
            details={"max_range_days": MAX_RANGE_DAYS},
        )
    if resolution is not None and resolution not in TIERS:
        raise ValidationFailedError(
            f"Unknown resolution {resolution!r}.",
            details={"resolutions": list(TIERS)},
        )

    try:
        result = await metrics.range_for_series(
            db,
            series_id=series_id,
            host_id=host_id,
            start=start,
            end=end,
            max_points=max_points,
            tier=resolution,
        )
    except QueryRangeError as exc:
        raise ValidationFailedError(str(exc)) from exc

    if result is None:
        raise NotFoundError("No such series on this host.")
    return _range_response(result, start, end)


@read_router.get(
    "/hosts/{host_id}/metrics/status",
    summary="Whether metrics are actually arriving for a host",
    response_model=MetricsStatusResponse,
    responses={404: {"description": "No such host."}},
)
async def metrics_status(
    db: SessionDep,
    metrics: MetricsServiceDep,
    _session: ViewerDep,
    host_id: uuid.UUID,
) -> MetricsStatusResponse:
    """The question a chart cannot answer.

    An empty chart could mean "nothing collected", "collection is off", "the
    agent is offline" or "the collector is failing", and only the first is
    visible from the data. This endpoint answers the first and the fourth; the
    monitoring endpoint answers the second and the host endpoint the third.

    Every module in the registry appears, including those that have produced
    nothing, so the interface can tell "this module has no data" from "this
    module does not exist on this controller".
    """
    await _require_host(db, host_id)
    series = await metrics.series_for_host(db, host_id)
    sample_count = await metrics.sample_count(db, host_id)
    health = await metrics.health_for_host(db, host_id)

    newest: datetime | None = None
    per_module: dict[str, list[SeriesDescriptor]] = {}
    for entry in series:
        per_module.setdefault(entry.module, []).append(entry)
        if entry.last_sample_at is not None and (newest is None or entry.last_sample_at > newest):
            newest = entry.last_sample_at

    modules: list[ModuleStatusResponse] = []
    for module in registry.MODULE_LIST:
        owned = per_module.get(module.id, [])
        latest = max(
            (entry.last_sample_at for entry in owned if entry.last_sample_at is not None),
            default=None,
        )
        reported = health.get(module.id)
        modules.append(
            ModuleStatusResponse(
                module=module.id,
                module_name=module.name,
                series_count=len(owned),
                last_sample_at=_iso(latest),
                # Absent rather than zeroed when a module has never reported.
                # A `consecutive_failures` of 0 and a `last_reported_at` of
                # null is "never heard from"; 0 with a timestamp is "working".
                last_reported_at=_iso(reported.last_reported_at) if reported else None,
                last_success_at=_iso(reported.last_success_at) if reported else None,
                last_error_at=_iso(reported.last_error_at) if reported else None,
                last_error=reported.last_error if reported else None,
                consecutive_failures=reported.consecutive_failures if reported else 0,
            )
        )

    return MetricsStatusResponse(
        host_id=str(host_id),
        sample_count=sample_count,
        series_count=len(series),
        last_sample_at=_iso(newest),
        modules=modules,
    )


def _aware(value: datetime, field: str) -> datetime:
    """Require a time zone on a query parameter.

    A naive datetime would be silently interpreted as the controller's local
    time, so a browser in a different zone would ask for a window hours away
    from the one it meant — and the response would be plausible and wrong.
    Refused rather than assumed to be UTC, because assuming is how that bug
    ships.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValidationFailedError(
            f"`{field}` must carry a time zone, e.g. 2026-10-13T12:00:00Z.",
            details={"parameter": field},
        )
    return value.astimezone(UTC)
