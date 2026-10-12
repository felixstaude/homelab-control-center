"""Global defaults plus per-host overrides into one effective document.

Phase 2, implementing ``docs/AGENT_ARCHITECTURE.md`` §6.1.

## Resolution happens here, never on the agent

The agent receives a **fully resolved** document and merges nothing. Two
reasons, and the second is the one that makes it non-negotiable: the interface
can show exactly what a host will run before it is applied, and layer
precedence bugs cannot differ between agent versions. A fleet where three agent
builds disagree about whether a host override beats a global default is a fleet
nobody can reason about.

## Three layers, and "inherit" is a value rather than an absence

```
    registry default        enabled=False, the module's own default interval
           │
           ▼  overridden by
    global default          set once, applies to every host
           │
           ▼  overridden by
    host override           set on one host
           │
           ▼
    EFFECTIVE CONFIGURATION for host X
```

Each layer stores ``None`` for a field it does not speak to, and ``None``
means **inherit** — explicitly, in the column, rather than being inferred from
a missing row. This is the mission's §5 warning taken seriously: ``enabled =
false``, "no value configured" and "inherited false" are three different
states, and a design where they share a representation produces a UI that
cannot answer "why is this host collecting every five seconds?".

So every resolved value carries its :class:`ValueSource`, and the API reports
it. "Inherited from the global default of 10 s" and "overridden on this host to
10 s" look identical in the value and are different facts.

## A raised floor clamps and says so

A module's ``min_interval_seconds`` can be raised in a later release. Stored
overrides below the new floor are then invalid, and there are three things to
do about it: refuse to generate a configuration, silently clamp, or clamp and
report. The first makes a host unconfigurable because of an upgrade. The second
is the behaviour this project forbids — a setting that does not do what it says
([`PRODUCT_VISION.md`](../../../../docs/PRODUCT_VISION.md) principle 2 applied
to configuration). So the interval is raised to the floor, collection keeps
working, and the adjustment is recorded in
:attr:`EffectiveConfiguration.problems` for the interface to surface.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from hcc.monitoring import modules as registry
from hcc.monitoring.schedule import (
    CalendarSchedule,
    IntervalSchedule,
    Schedule,
    ScheduleError,
    ScheduleKind,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


class ValueSource(enum.StrEnum):
    """Which layer decided a resolved value.

    Reported for every field, because a configuration system that cannot say
    where a value came from is one operators stop trusting.
    """

    #: The module registry's own default. Nothing has been configured.
    DEFAULT = "default"
    #: A global default an administrator set.
    GLOBAL = "global"
    #: An override set on this host.
    HOST = "host"


@dataclass(frozen=True, slots=True)
class Resolved[T]:
    """A value and the layer it came from."""

    value: T
    source: ValueSource


@dataclass(frozen=True, slots=True)
class ModuleSetting:
    """One layer's opinion about one module. ``None`` means "inherit".

    Used for both the global default and the host override, because they are
    the same shape — which is what makes resolution a fold over layers rather
    than a pair of special cases.
    """

    enabled: bool | None = None
    #: The schedule as stored: a parsed model, or ``None`` to inherit. Parsed
    #: rather than raw JSON so that an unparseable stored schedule is a loud
    #: failure at the service boundary rather than a quiet one here.
    schedule: Schedule | None = None


@dataclass(frozen=True, slots=True)
class EffectiveModule:
    """What one module will actually do on one host, and why."""

    module_id: str
    enabled: Resolved[bool]
    schedule: Resolved[Schedule]

    def to_document(self) -> dict[str, Any]:
        """The agent's view: the decision, without the provenance.

        The agent is deliberately not told which layer a value came from. It
        has no use for it — it cannot act differently — and sending it would
        make the checksum change when an operator moved a setting between
        layers without changing its effect, which would re-deploy a
        configuration that is identical in every way the agent cares about.
        """
        return {
            "enabled": self.enabled.value,
            "schedule": self.schedule.value.to_document(),
        }


@dataclass(frozen=True, slots=True)
class EffectiveConfiguration:
    """Every module's effective settings for one host.

    :attr:`problems` is not an error list — the configuration is valid and
    usable. It records adjustments the controller made on the operator's
    behalf, each of which the interface must surface, because a setting that
    silently does something other than what it says is the failure this
    project's second principle forbids.
    """

    modules: tuple[EffectiveModule, ...]
    problems: tuple[str, ...] = field(default=())

    def module(self, module_id: str) -> EffectiveModule | None:
        """One module's effective settings, if it is in the registry."""
        for candidate in self.modules:
            if candidate.module_id == module_id:
                return candidate
        return None

    @property
    def enabled_module_ids(self) -> tuple[str, ...]:
        """Just the modules that will collect. For logging and for the UI."""
        return tuple(entry.module_id for entry in self.modules if entry.enabled.value)

    def to_document(self) -> dict[str, Any]:
        """The ``modules`` map of the agent configuration document.

        **Every** module appears, including the disabled ones, with
        ``"enabled": false`` rather than being omitted. That is what makes
        "this module is off" and "this agent is too old to know about this
        module" distinguishable (``AGENT_ARCHITECTURE.md`` §6.2) — an absent
        key means the controller did not mention it, which is a different
        thing from mentioning it as off.
        """
        return {entry.module_id: entry.to_document() for entry in self.modules}


#: ``enabled`` resolves to this when no layer has an opinion.
#:
#: A named constant rather than a literal ``False`` at the call site, because
#: this is the product's central default-deny promise (§2.1) and a grep for it
#: should land somewhere that explains itself: enrolling an agent must not
#: start collecting anything.
DEFAULT_ENABLED: Final = False


def resolve_host_configuration(
    *,
    global_defaults: Mapping[str, ModuleSetting],
    host_overrides: Mapping[str, ModuleSetting],
) -> EffectiveConfiguration:
    """Fold the layers into one effective configuration.

    Iterates the **registry**, not the stored rows, which is what makes the
    result complete and deterministic: every known module appears exactly once
    and in registry order, whether or not anything has been configured for it.
    A stored row for a module the registry does not know is ignored here and
    reported — it can only arise from a module being removed in an upgrade, and
    refusing to produce a configuration because of it would strand the host.
    """
    resolved: list[EffectiveModule] = []
    problems: list[str] = []

    for descriptor in registry.MODULE_LIST:
        host = host_overrides.get(descriptor.id, ModuleSetting())
        globally = global_defaults.get(descriptor.id, ModuleSetting())

        enabled = _first_opinion(
            (host.enabled, ValueSource.HOST),
            (globally.enabled, ValueSource.GLOBAL),
            fallback=Resolved(DEFAULT_ENABLED, ValueSource.DEFAULT),
        )

        # Annotated explicitly: the type variable would otherwise be inferred
        # from the union of the layer types and the fallback's concrete type,
        # which generalises to `BaseModel` and loses the `.to_document()` the
        # document builder needs.
        schedule: Resolved[Schedule] = _first_opinion(
            (host.schedule, ValueSource.HOST),
            (globally.schedule, ValueSource.GLOBAL),
            fallback=Resolved(
                IntervalSchedule(interval_seconds=descriptor.default_interval_seconds),
                ValueSource.DEFAULT,
            ),
        )

        schedule, adjustments = _conform_to_descriptor(descriptor, schedule)
        problems.extend(adjustments)

        resolved.append(
            EffectiveModule(module_id=descriptor.id, enabled=enabled, schedule=schedule)
        )

    problems.extend(
        f"configuration exists for unknown module {module_id!r} and was ignored"
        for module_id in sorted(set(global_defaults) | set(host_overrides))
        if module_id not in registry.MODULES
    )

    return EffectiveConfiguration(modules=tuple(resolved), problems=tuple(problems))


def _first_opinion[T](
    *layers: tuple[T | None, ValueSource],
    fallback: Resolved[T],
) -> Resolved[T]:
    """The first layer with a non-``None`` opinion, or the fallback.

    Layers are passed most-specific first, so this is "host beats global beats
    registry" expressed once rather than as a chain of conditionals per field.
    """
    for value, source in layers:
        if value is not None:
            return Resolved(value, source)
    return fallback


def _conform_to_descriptor(
    descriptor: registry.ModuleDescriptor, schedule: Resolved[Schedule]
) -> tuple[Resolved[Schedule], list[str]]:
    """Bring a resolved schedule within the module's current limits.

    Two things can be wrong with a stored schedule that was legal when saved:
    the module's floor may have been raised, or a permitted kind may have been
    withdrawn. Both are upgrade consequences rather than operator mistakes, so
    both are corrected and reported rather than refused.

    A withdrawn kind cannot be clamped — there is no nearby legal value — so it
    falls back to the module's default interval. That changes behaviour, which
    is why it is reported in the strongest terms the problem list allows.
    """
    problems: list[str] = []
    value = schedule.value

    if value.kind not in descriptor.schedule_kinds:
        permitted = ", ".join(kind.value for kind in descriptor.schedule_kinds)
        problems.append(
            f"{descriptor.id}: a {value.kind.value} schedule is no longer permitted "
            f"for this module (permitted: {permitted}); reset to the default "
            f"interval of {descriptor.default_interval_seconds}s"
        )
        return (
            Resolved(
                IntervalSchedule(interval_seconds=descriptor.default_interval_seconds),
                schedule.source,
            ),
            problems,
        )

    if isinstance(value, IntervalSchedule) and value.interval_seconds < (
        floor := descriptor.min_interval_seconds
    ):
        problems.append(
            f"{descriptor.id}: the configured interval of {value.interval_seconds}s is "
            f"below this module's minimum of {floor}s and was raised to it"
        )
        # Jitter is carried over and re-validated by the model: it had to be
        # less than the old interval, and the new one is larger, so it cannot
        # become invalid in this direction.
        value = IntervalSchedule(
            interval_seconds=floor,
            jitter_seconds=value.jitter_seconds,
        )
        return Resolved(value, schedule.source), problems

    return schedule, problems


def validate_setting(
    module_id: str,
    *,
    enabled: bool | None,
    schedule: Any | None,
) -> ModuleSetting:
    """Validate one layer's opinion about one module, reporting every problem.

    The controller-side half of the double validation
    (``docs/SCHEDULING.md`` §9): this one exists to give a good error message
    in the interface, and the agent validates again because it is the component
    that must never act on a configuration it cannot honour.

    ``schedule`` is raw — a mapping straight from a request body or a database
    column — because this is the boundary where it stops being raw.

    Raises:
        UnknownModuleError: the module is not in the registry.
        ScheduleError: the schedule is malformed, or outside this module's
            limits. Carries every problem, not the first.
    """
    descriptor = registry.descriptor(module_id)

    if schedule is None:
        return ModuleSetting(enabled=enabled, schedule=None)

    parsed = parse_schedule(schedule)

    problems: list[str] = []
    if parsed.kind not in descriptor.schedule_kinds:
        permitted = ", ".join(kind.value for kind in descriptor.schedule_kinds)
        problems.append(
            f"module {module_id!r} does not accept a {parsed.kind.value} schedule; "
            f"permitted: {permitted}"
        )
    if isinstance(parsed, IntervalSchedule):
        if parsed.interval_seconds < descriptor.min_interval_seconds:
            problems.append(
                f"interval_seconds must be at least {descriptor.min_interval_seconds} "
                f"for module {module_id!r}, got {parsed.interval_seconds}"
            )
    elif (
        isinstance(parsed, CalendarSchedule)
        and parsed.dst_ambiguous.value == "both"
        and not descriptor.allows_duplicate_runs
    ):
        problems.append(
            f"module {module_id!r} does not permit dst_ambiguous='both', because a "
            "duplicate run is not harmless for it"
        )

    if problems:
        raise ScheduleError(problems)
    return ModuleSetting(enabled=enabled, schedule=parsed)


def parse_schedule(raw: Any) -> Schedule:
    """Parse a raw schedule mapping into the model for its kind.

    Dispatches on ``kind`` explicitly rather than letting Pydantic try a union,
    because a union's error message on a malformed document enumerates the
    failures of *every* member — so a bad ``interval_seconds`` is reported
    alongside a complaint about a missing ``timezone``, and the operator reads
    two problems where there is one.

    Raises:
        ScheduleError: the document is not a mapping, has no recognised
            ``kind``, or fails its model's validation.
    """
    if not isinstance(raw, dict):
        raise ScheduleError(["a schedule must be an object"])

    kind: object = raw.get("kind")
    try:
        # `kind` arrives from parsed JSON, so it may be any type. ScheduleKind
        # raises ValueError for a non-member, including for a non-string —
        # which is the behaviour wanted here, so the annotation is widened
        # rather than the value pre-checked.
        parsed_kind = ScheduleKind(kind)  # type: ignore[arg-type]
    except ValueError:
        permitted = ", ".join(k.value for k in (ScheduleKind.INTERVAL, ScheduleKind.CALENDAR))
        raise ScheduleError([f"schedule kind {kind!r} is not one of: {permitted}"]) from None

    model: type[IntervalSchedule] | type[CalendarSchedule]
    if parsed_kind is ScheduleKind.INTERVAL:
        model = IntervalSchedule
    elif parsed_kind is ScheduleKind.CALENDAR:
        model = CalendarSchedule
    else:
        # `event` and `manual` are in the enum so a descriptor can name them,
        # but nothing in this phase can be configured with one and there is
        # deliberately no half-built path here for a later module to inherit.
        raise ScheduleError(
            [f"schedule kind {parsed_kind.value!r} is not supported in this release"]
        )

    try:
        return model.model_validate(raw)
    except ScheduleError:
        # Defensive. A `ScheduleError` raised inside a Pydantic validator is
        # caught and wrapped by Pydantic — see `_flatten_validation_error` —
        # so this branch is for a future validator that raises outside one.
        raise
    except ValueError as exc:
        raise ScheduleError(_flatten_validation_error(exc)) from exc


def _flatten_validation_error(exc: ValueError) -> list[str]:
    """Turn a Pydantic validation error into one message per problem.

    Pydantic's own string form is multi-line and names the model class, which
    is an implementation detail an operator should not be shown. The field path
    and the message are what they need.

    ## Wrapped ``ScheduleError`` problems are expanded

    Pydantic catches every ``ValueError`` a validator raises — including
    :class:`ScheduleError` — and wraps it as one validation error whose message
    is ``str(exc)``. For a :class:`ScheduleError` that is the joined form, so
    without the expansion below, a calendar schedule with five problems would
    arrive here as **one** string containing five semicolon-separated
    complaints. The product rule is that each problem names its own field
    (``docs/SCHEDULING.md`` §10), so the original exception is recovered from
    the error's ``ctx`` — where Pydantic preserves it — and its problems are
    spliced in individually.
    """
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return [str(exc)]
    problems: list[str] = []
    for detail in errors():
        original = detail.get("ctx", {}).get("error")
        if isinstance(original, ScheduleError):
            problems.extend(original.problems)
            continue
        location = ".".join(str(part) for part in detail.get("loc", ())) or "schedule"
        problems.append(f"{location}: {detail.get('msg', 'is invalid')}")
    return problems or [str(exc)]
