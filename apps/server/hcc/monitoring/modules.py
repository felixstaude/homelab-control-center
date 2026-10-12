"""The closed, versioned registry of monitoring modules.

Phase 2, implementing ``docs/MONITORING_MODULES.md``. A module is the unit of
**configuration, scheduling and consent**: independently enabled per host,
independently scheduled, independently failing.

## Closed, and that is the security property

An identifier not in :data:`MODULES` is **rejected**, not stored. The tempting
alternative — accept any string and let the agent decide whether it knows the
module — fails in both directions: a typo becomes a module that silently never
collects, and a compromised controller could name a module a future agent
version will one day implement. So the controller refuses what it does not
know, and an agent too old to know a module reports it unavailable rather than
guessing.

## Three different "defaults", kept apart by the type system

The confusion this is shaped to avoid is real and is the mission's §5 warning:

* :attr:`ModuleDescriptor.default_interval_seconds` — the interval to *offer*
  when an operator enables the module. It is not an instruction to run.
* :attr:`ModuleDescriptor.min_interval_seconds` — the floor, enforced by the
  controller at save time and by the agent at apply time.
* **Enabled** — which has no descriptor field at all, because the only value it
  may take as a default is ``False``. A registry entry cannot express
  "enabled by default", so no migration and no code path can accidentally
  activate collection on an existing host (§2.1).

## Why five modules and not two

``MONITORING_MODULES.md`` §3 originally folded load average into ``cpu`` and
swap into ``memory``. They are separate here, and the reasons are product
reasons rather than tidiness:

* **Consent is per module.** A swapless host must be able to report ``swap``
  unavailable without that saying anything about ``memory``, and an operator
  who wants load average without per-core CPU sampling should not have to buy
  both.
* **Load average is not a utilisation percentage** and must never be rendered
  as one. Separate modules put that distinction in the data model, where a UI
  convention cannot erode it.
* **The cheap ones are genuinely cheap.** ``uptime`` and ``load`` are one
  short read of one pseudo-file each; making them separate costs a schedule
  and buys an operator the ability to keep a 1-second load series without a
  1-second CPU series.

Recorded as ADR-048.

## Schema versions exist because agents are not upgraded with the controller

:attr:`ModuleDescriptor.data_schema_version` is the version of the *metric
payload* a module produces. An agent installed on a dedicated server across
the internet is not upgraded when the controller is, so the controller will
one day receive a payload from an older schema. Versioning it from the first
release means that is a decision rather than a surprise.
"""

from __future__ import annotations

import enum
from typing import Final

from pydantic import BaseModel, Field

from hcc.monitoring.schedule import (
    MAX_INTERVAL_SECONDS,
    MIN_INTERVAL_SECONDS,
    ScheduleKind,
)

#: Version of the registry *as a whole*. Bumped when a module is added,
#: removed, or has its identifier, permitted kinds or floor changed — the
#: things an agent's own copy must agree about.
#:
#: Distinct from the protocol version: the protocol is the frame format, this
#: is the catalogue. They move for different reasons, and tying them together
#: would mean adding a module forced every agent in the fleet to be upgraded
#: before it could connect.
REGISTRY_VERSION: Final = 1

#: Longest a module identifier may be, and the shape it must take. Identifiers
#: appear in URLs, JSON keys, database rows and Go constants, so they are
#: restricted to the intersection that is safe in all four.
MAX_MODULE_ID_LENGTH: Final = 32


class Platform(enum.StrEnum):
    """Which hosts a module can run on at all.

    Not a capability report — that is per host and comes from the agent
    (``docs/AGENT_ARCHITECTURE.md`` §7). This is the coarser statement that
    ``/proc/stat`` does not exist on Windows, so the interface need not offer
    a Linux-only module on a host that is not Linux.
    """

    LINUX = "linux"


class ModuleDescriptor(BaseModel):
    """Everything the controller, the agent and the interface must agree about.

    Frozen: a descriptor is a constant, and a mutable one would let a request
    handler change the floor for every later request in the process.
    """

    model_config = {"extra": "forbid", "frozen": True}

    id: str = Field(
        max_length=MAX_MODULE_ID_LENGTH,
        pattern=r"^[a-z][a-z0-9_]*$",
        description=(
            "Stable identifier. Lower-case, underscore-separated, and never "
            "renamed: it is a database value, a JSON key and an agent "
            "constant, so a rename is a migration in three languages."
        ),
    )

    name: str = Field(max_length=64, description="Human-readable name for the interface.")

    description: str = Field(
        max_length=256,
        description="One sentence an operator can decide from, shown at the point of enabling.",
    )

    platform: Platform = Field(description="The platform this module can run on.")

    schedule_kinds: tuple[ScheduleKind, ...] = Field(
        description=(
            "Which scheduling kinds are permitted. Enforced by the controller "
            "at save time, for the error message, and by the agent at apply "
            "time, because the agent is the component that must never act on "
            "a configuration it cannot honour."
        )
    )

    min_interval_seconds: int = Field(
        ge=MIN_INTERVAL_SECONDS,
        le=MAX_INTERVAL_SECONDS,
        description="The floor. Collecting faster than this costs more than it tells you.",
    )

    default_interval_seconds: int = Field(
        ge=MIN_INTERVAL_SECONDS,
        le=MAX_INTERVAL_SECONDS,
        description=(
            "Interval offered when an operator enables this module. NOT a "
            "default to run: every module ships disabled."
        ),
    )

    data_schema_version: int = Field(
        ge=1, description="Version of the metric payload this module produces."
    )

    requires_privileges: bool = Field(
        description=(
            "Whether collection needs more than the unprivileged agent user. "
            "False for every module in this phase — all of them read "
            "world-readable pseudo-files — and present so that a module which "
            "does need a grant cannot be added without declaring it."
        )
    )

    allows_duplicate_runs: bool = Field(
        description=(
            "Whether running twice for one scheduled occurrence is harmless. "
            "Gates `dst_ambiguous: both`. True for samplers, where a duplicate "
            "is one extra data point; false for anything that acts on the "
            "host, which is why a SMART self-test will not be able to set it."
        )
    )

    metrics: tuple[MetricDescriptor, ...] = Field(
        description=(
            "The series this module produces, declared rather than discovered. "
            "The interface needs units and labels before any sample has "
            "arrived, so that an enabled module with no data yet renders as "
            "'no data' rather than as nothing at all."
        )
    )

    def metric(self, metric_id: str) -> MetricDescriptor | None:
        """Look up one of this module's metrics by identifier."""
        for candidate in self.metrics:
            if candidate.id == metric_id:
                return candidate
        return None


class MetricUnit(enum.StrEnum):
    """The unit a metric is reported in.

    A closed set, because the interface formats by unit and an unrecognised
    one would render as a bare number. Percentages are 0-100 rather than 0-1
    throughout — stated here because mixing the two conventions in one system
    produces charts that are wrong by a factor of a hundred.
    """

    #: 0-100.
    PERCENT = "percent"
    BYTES = "bytes"
    SECONDS = "seconds"
    #: A dimensionless count, e.g. logical CPUs.
    COUNT = "count"
    #: Load average: a dimensionless number of runnable processes, which is
    #: *not* a percentage and must never be formatted as one.
    LOAD = "load"


class MetricKind(enum.StrEnum):
    """How a metric behaves over time, which decides how it may be aggregated."""

    #: A value measured at an instant. Averaging a window of them is
    #: meaningful, as long as the minimum and maximum survive too.
    GAUGE = "gauge"
    #: A monotonically increasing total. **Never averaged**: an averaged
    #: power-on-hours is meaningless. Rollups keep first and last so a rate can
    #: be derived, and a reset is recorded as a gap rather than a negative
    #: spike.
    COUNTER = "counter"


class MetricDescriptor(BaseModel):
    """One series a module produces."""

    model_config = {"extra": "forbid", "frozen": True}

    id: str = Field(
        max_length=MAX_MODULE_ID_LENGTH,
        pattern=r"^[a-z][a-z0-9_]*$",
        description="Stable identifier, unique within its module.",
    )
    name: str = Field(max_length=64)
    unit: MetricUnit
    kind: MetricKind
    label_keys: tuple[str, ...] = Field(
        default=(),
        description=(
            "Label dimensions this metric is broken down by — 'core' for "
            "per-core CPU, 'window' for the three load averages. Empty means "
            "one series per host. Declared, so cardinality is a reviewable "
            "property of the registry rather than whatever an agent sends."
        ),
    )


# Resolve the forward reference: `ModuleDescriptor.metrics` names
# `MetricDescriptor`, which is defined after it so that the reading order is
# module-then-metric rather than bottom-up.
ModuleDescriptor.model_rebuild()


#: Only ``interval`` in this phase. The five modules are all samplers, and a
#: sampler on a calendar is a measurement taken at a time chosen for human
#: convenience rather than for the signal — which is why
#: ``docs/SCHEDULING.md`` §9 marks CPU and memory interval-only.
_SAMPLER_KINDS: Final = (ScheduleKind.INTERVAL,)


CPU: Final = ModuleDescriptor(
    id="cpu",
    name="CPU utilisation",
    description=(
        "Aggregate and optional per-core CPU utilisation, from /proc/stat. "
        "Computed from the difference between two samples."
    ),
    platform=Platform.LINUX,
    schedule_kinds=_SAMPLER_KINDS,
    # One second: /proc/stat is a cheap read and the kernel's own counters
    # advance at the scheduler tick, so a 1-second delta is a real measurement.
    min_interval_seconds=1,
    # Ten seconds, from MONITORING_MODULES.md §6: a balance between seeing a
    # spike and paying for it in storage.
    default_interval_seconds=10,
    data_schema_version=1,
    requires_privileges=False,
    allows_duplicate_runs=True,
    metrics=(
        MetricDescriptor(
            id="utilisation", name="CPU utilisation", unit=MetricUnit.PERCENT, kind=MetricKind.GAUGE
        ),
        MetricDescriptor(
            id="core_utilisation",
            name="Per-core utilisation",
            unit=MetricUnit.PERCENT,
            kind=MetricKind.GAUGE,
            label_keys=("core",),
        ),
        MetricDescriptor(
            id="logical_cpus",
            name="Logical CPUs",
            unit=MetricUnit.COUNT,
            kind=MetricKind.GAUGE,
        ),
    ),
)

MEMORY: Final = ModuleDescriptor(
    id="memory",
    name="Memory",
    description=(
        "Total, available and used memory from /proc/meminfo. Uses MemAvailable, "
        "which is the kernel's own estimate of what a new workload could claim."
    ),
    platform=Platform.LINUX,
    schedule_kinds=_SAMPLER_KINDS,
    min_interval_seconds=1,
    default_interval_seconds=10,
    data_schema_version=1,
    requires_privileges=False,
    allows_duplicate_runs=True,
    metrics=(
        MetricDescriptor(
            id="utilisation", name="Memory used", unit=MetricUnit.PERCENT, kind=MetricKind.GAUGE
        ),
        MetricDescriptor(
            id="total", name="Total memory", unit=MetricUnit.BYTES, kind=MetricKind.GAUGE
        ),
        MetricDescriptor(
            id="available", name="Available memory", unit=MetricUnit.BYTES, kind=MetricKind.GAUGE
        ),
        MetricDescriptor(
            id="used", name="Used memory", unit=MetricUnit.BYTES, kind=MetricKind.GAUGE
        ),
    ),
)

SWAP: Final = ModuleDescriptor(
    id="swap",
    name="Swap",
    description=(
        "Swap total and usage from /proc/meminfo. Reports unavailable on a host "
        "with no swap configured, rather than reporting zero usage of nothing."
    ),
    platform=Platform.LINUX,
    schedule_kinds=_SAMPLER_KINDS,
    min_interval_seconds=1,
    default_interval_seconds=10,
    data_schema_version=1,
    requires_privileges=False,
    allows_duplicate_runs=True,
    metrics=(
        MetricDescriptor(
            id="utilisation", name="Swap used", unit=MetricUnit.PERCENT, kind=MetricKind.GAUGE
        ),
        MetricDescriptor(
            id="total", name="Total swap", unit=MetricUnit.BYTES, kind=MetricKind.GAUGE
        ),
        MetricDescriptor(id="used", name="Used swap", unit=MetricUnit.BYTES, kind=MetricKind.GAUGE),
    ),
)

LOAD: Final = ModuleDescriptor(
    id="load",
    name="Load average",
    description=(
        "The 1, 5 and 15-minute load averages from /proc/loadavg. A count of "
        "runnable and uninterruptible processes — not a CPU percentage."
    ),
    platform=Platform.LINUX,
    schedule_kinds=_SAMPLER_KINDS,
    # Five seconds, not one: the kernel recomputes these on a 5-second timer,
    # so sampling faster returns the same number and stores it twice.
    min_interval_seconds=5,
    default_interval_seconds=30,
    data_schema_version=1,
    requires_privileges=False,
    allows_duplicate_runs=True,
    metrics=(
        MetricDescriptor(
            id="average",
            name="Load average",
            unit=MetricUnit.LOAD,
            kind=MetricKind.GAUGE,
            # One series per window rather than three metrics, because the
            # interface draws them on one chart and the three are the same
            # measurement over different windows.
            label_keys=("window",),
        ),
    ),
)

UPTIME: Final = ModuleDescriptor(
    id="uptime",
    name="System uptime",
    description=(
        "How long the host has been running, from /proc/uptime. The host's "
        "uptime, which is not the agent process's uptime."
    ),
    platform=Platform.LINUX,
    schedule_kinds=_SAMPLER_KINDS,
    # A minute: uptime is a clock, so sampling it often tells you only what
    # arithmetic already would.
    min_interval_seconds=60,
    default_interval_seconds=300,
    data_schema_version=1,
    requires_privileges=False,
    allows_duplicate_runs=True,
    metrics=(
        MetricDescriptor(
            id="seconds",
            name="System uptime",
            unit=MetricUnit.SECONDS,
            # A gauge rather than a counter, even though it only increases:
            # a reboot resets it, and that reset is the signal — a counter's
            # reset handling would record it as a gap and hide the reboot.
            kind=MetricKind.GAUGE,
        ),
    ),
)


#: Every module that exists, in display order.
#:
#: A tuple rather than a dict literal so the order is the interface's order and
#: is reviewable; :data:`MODULES` is the lookup.
MODULE_LIST: Final[tuple[ModuleDescriptor, ...]] = (CPU, MEMORY, SWAP, LOAD, UPTIME)

#: Lookup by identifier. The closed set §1 of the docstring describes.
MODULES: Final[dict[str, ModuleDescriptor]] = {module.id: module for module in MODULE_LIST}

#: Identifiers, for a CHECK constraint and for error messages that name what
#: *would* have been accepted.
MODULE_IDS: Final[tuple[str, ...]] = tuple(module.id for module in MODULE_LIST)


class UnknownModuleError(ValueError):
    """A module identifier that is not in the registry.

    Names the permitted set, because "unknown module" without a list is an
    error an operator cannot act on.
    """

    def __init__(self, module_id: object) -> None:
        self.module_id = module_id
        super().__init__(
            f"unknown module {module_id!r}; this controller knows: {', '.join(MODULE_IDS)}"
        )


def descriptor(module_id: str) -> ModuleDescriptor:
    """The descriptor for ``module_id``.

    Raises:
        UnknownModuleError: if the identifier is not in the registry.
    """
    try:
        return MODULES[module_id]
    except KeyError as exc:
        raise UnknownModuleError(module_id) from exc
