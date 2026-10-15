"""Module registry, canonicalisation, resolution and document unit tests.

No database. Four concerns in one file because they are one contract: the
registry says what may be configured, resolution says what a host ends up
with, canonicalisation says what bytes that is, and the document is the result.
A bug in any of them shows up as the agent running something other than what
the interface displayed.

The resolution tests are the ones worth reading. The mission's §5 names the
failure they exist to prevent — ambiguous combinations of ``enabled=false``,
missing values and inherited values — and each of the three states is pinned
separately here rather than being assumed distinguishable.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest

from hcc.monitoring import modules as registry
from hcc.monitoring.canonical import (
    CHECKSUM_LENGTH,
    CHECKSUM_PREFIX,
    NonCanonicalValueError,
    canonicalize,
    checksum,
    checksum_of_bytes,
    is_checksum,
)
from hcc.monitoring.document import DOCUMENT_VERSION, build_revision
from hcc.monitoring.modules import (
    MODULE_IDS,
    MetricKind,
    MetricUnit,
    Platform,
    UnknownModuleError,
    descriptor,
)
from hcc.monitoring.resolve import (
    DEFAULT_ENABLED,
    ModuleSetting,
    ValueSource,
    parse_schedule,
    resolve_host_configuration,
    validate_setting,
)
from hcc.monitoring.schedule import (
    MAX_INTERVAL_SECONDS,
    MIN_INTERVAL_SECONDS,
    CalendarSchedule,
    IntervalSchedule,
    ScheduleError,
    ScheduleKind,
)

HOST_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def test_the_registry_holds_the_modules_this_release_implements() -> None:
    """Pinned so that adding a module is a deliberate act with a migration.

    The list itself matters less than the reason it is written down: the
    ``module_id`` CHECK constraints enumerate it, and
    :func:`test_every_module_is_in_the_database_allow_list` is what actually
    checks they agree. This assertion is the human-readable half — it makes
    "what does this release collect" a line somebody reviews.
    """
    assert MODULE_IDS == (
        "cpu",
        "memory",
        "swap",
        "load",
        "uptime",
        "filesystem",
        "diskio",
        "network",
    )


def test_every_module_is_in_the_database_allow_list() -> None:
    """The guard that cannot be satisfied by editing one tuple.

    A module in the registry and not in the newest CHECK constraint is a row
    the database refuses — and it refuses it at *runtime*, on a real host,
    the first time somebody enables that module. That is the worst place to
    find out, so it is found here instead.

    Read out of the migrations rather than out of the models, deliberately.
    ``hcc/models/monitoring.py`` renders its constraint from ``MODULE_IDS``,
    so comparing the registry against the model compares a list with itself.
    The migrations are the only statement of what is in a database that has
    already been migrated.
    """
    import pathlib
    import re

    versions = pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions"
    pattern = re.compile(r"(?:module_id|module) IN \(([^)]*)\)")

    # Sorted by file name, which is the date-prefixed revision order this
    # project uses, so the last match is the newest statement about the
    # constraint. A migration that *narrows* it would be found by this too.
    latest: set[str] | None = None
    for path in sorted(versions.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            latest = {value.strip().strip("'\"") for value in match.group(1).split(",")}
        # A migration may also build the list from a tuple, as 0011 does.
        if "_AFTER" in text:
            after = re.search(r"_AFTER = \(([^)]*)\)", text)
            if after:
                names = re.findall(r"\"([a-z_]+)\"", after.group(1))
                latest = (latest or set()) | set(names)

    assert latest is not None, "no migration defines a module allow-list"
    missing = sorted(set(MODULE_IDS) - latest)
    assert not missing, (
        f"{missing} are in the registry and not in the newest CHECK constraint. "
        f"Enabling one on a real host would fail with a constraint violation. "
        f"Add a migration that widens the allow-list."
    )


def test_no_module_can_express_being_enabled_by_default() -> None:
    """§2.1, enforced by absence: there is no descriptor field to set.

    The strongest form of the default-deny promise available — a registry entry
    *cannot* say "on", so no migration and no one-line edit can turn collection
    on for an existing host.
    """
    assert not hasattr(registry.CPU, "enabled")
    assert not hasattr(registry.CPU, "default_enabled")
    assert DEFAULT_ENABLED is False


@pytest.mark.parametrize("module_id", MODULE_IDS)
def test_every_module_is_consistent(module_id: str) -> None:
    module = descriptor(module_id)
    assert module.id == module_id
    assert module.platform is Platform.LINUX
    assert module.schedule_kinds, "a module with no permitted kind cannot be scheduled"
    assert MIN_INTERVAL_SECONDS <= module.min_interval_seconds <= MAX_INTERVAL_SECONDS
    # The offered default must itself be legal, or enabling a module would
    # produce a configuration the controller would refuse to save.
    assert module.default_interval_seconds >= module.min_interval_seconds
    assert module.default_interval_seconds <= MAX_INTERVAL_SECONDS
    assert module.data_schema_version >= 1
    assert module.metrics, "a module that produces no series collects nothing"


@pytest.mark.parametrize("module_id", MODULE_IDS)
def test_no_module_in_this_phase_needs_privileges(module_id: str) -> None:
    """All five read world-readable pseudo-files.

    Pinned so that a module which *does* need a grant cannot be added without
    the declaration — and so "the agent never needs root to collect" stays a
    checkable claim rather than a sentence in a document.
    """
    assert descriptor(module_id).requires_privileges is False


@pytest.mark.parametrize("module_id", MODULE_IDS)
def test_every_metric_id_is_unique_within_its_module(module_id: str) -> None:
    ids = [metric.id for metric in descriptor(module_id).metrics]
    assert len(ids) == len(set(ids))


def test_load_average_is_not_a_percentage() -> None:
    """§14: load average must never be labelled as CPU utilisation.

    Enforced in the data model rather than by UI convention, which is the whole
    reason ``load`` is a separate module from ``cpu`` (ADR-048).
    """
    average = descriptor("load").metric("average")
    assert average is not None
    assert average.unit is MetricUnit.LOAD
    # Compared by value, not identity: mypy rejects an identity check between
    # two distinct literal members as non-overlapping, which is true and also
    # exactly the property worth asserting. The value comparison keeps the
    # assertion real — if someone changes `LOAD` to `PERCENT`, this fails.
    assert average.unit.value != MetricUnit.PERCENT.value


def test_uptime_is_a_gauge_not_a_counter() -> None:
    """It only increases, but a reboot resets it and that reset is the signal.

    A counter's reset handling would record the reboot as a gap and hide it.
    """
    seconds = descriptor("uptime").metric("seconds")
    assert seconds is not None
    assert seconds.kind is MetricKind.GAUGE


def test_cpu_declares_a_per_core_label_so_cardinality_is_reviewable() -> None:
    core = descriptor("cpu").metric("core_utilisation")
    assert core is not None
    assert core.label_keys == ("core",)


def test_every_module_in_this_phase_is_interval_only() -> None:
    """All five are samplers, and a sampler on a calendar is a measurement
    taken at a time chosen for human convenience (``SCHEDULING.md`` §9).
    """
    for module_id in MODULE_IDS:
        assert descriptor(module_id).schedule_kinds == (ScheduleKind.INTERVAL,)


def test_an_unknown_module_is_refused_and_the_refusal_names_the_alternatives() -> None:
    with pytest.raises(UnknownModuleError) as caught:
        descriptor("docker")
    message = str(caught.value)
    assert "docker" in message
    for module_id in MODULE_IDS:
        assert module_id in message


def test_a_descriptor_is_immutable() -> None:
    """A mutable one would let a request handler change the floor for every
    later request in the process.
    """
    with pytest.raises(ValueError):
        registry.CPU.min_interval_seconds = 60  # type: ignore[misc]


def test_load_has_a_higher_floor_than_cpu_because_the_kernel_recomputes_slowly() -> None:
    """``/proc/loadavg`` advances on a 5-second timer, so sampling faster
    returns the same number and stores it twice.
    """
    assert descriptor("load").min_interval_seconds == 5
    assert descriptor("cpu").min_interval_seconds == 1


# ---------------------------------------------------------------------------
# Canonicalisation
# ---------------------------------------------------------------------------


def test_keys_are_sorted_and_whitespace_is_absent() -> None:
    assert canonicalize({"b": 1, "a": 2}) == b'{"a":2,"b":1}'


def test_two_documents_with_the_same_content_have_the_same_bytes() -> None:
    left = canonicalize({"a": 1, "b": {"d": 4, "c": 3}})
    right = canonicalize({"b": {"c": 3, "d": 4}, "a": 1})
    assert left == right


def test_non_ascii_is_literal_rather_than_escaped() -> None:
    """``\\uXXXX`` escaping is optional in JSON and therefore not canonical."""
    assert canonicalize({"name": "Köln"}) == '{"name":"Köln"}'.encode()


def test_a_float_is_refused_rather_than_rendered() -> None:
    """Two languages' shortest round-trip formatting need not agree, so a float
    in a checksummed document is a checksum that can differ between the
    controller and the agent.
    """
    with pytest.raises(NonCanonicalValueError, match="float"):
        canonicalize({"interval": 0.1})


def test_a_float_nested_anywhere_is_refused_and_the_path_is_named() -> None:
    with pytest.raises(NonCanonicalValueError, match=r"\$.modules.cpu.rate"):
        canonicalize({"modules": {"cpu": {"rate": 1.5}}})


def test_a_float_inside_a_list_is_refused_and_the_index_is_named() -> None:
    with pytest.raises(NonCanonicalValueError, match=r"\$.values\[1\]"):
        canonicalize({"values": [1, 2.5]})


def test_a_non_string_key_is_refused() -> None:
    with pytest.raises(NonCanonicalValueError, match="keys must be strings"):
        canonicalize({1: "one"})


def test_a_type_json_cannot_represent_is_refused() -> None:
    with pytest.raises(NonCanonicalValueError, match="not JSON-representable"):
        canonicalize({"when": NOW})


def test_booleans_and_null_survive() -> None:
    assert canonicalize({"a": True, "b": False, "c": None}) == b'{"a":true,"b":false,"c":null}'


def test_a_bool_is_not_mistaken_for_an_int_and_refused() -> None:
    """``isinstance(True, int)`` is true, so the bool check has to come first."""
    assert canonicalize({"enabled": True}) == b'{"enabled":true}'


def test_a_checksum_is_prefixed_and_the_right_length() -> None:
    value = checksum({"a": 1})
    assert value.startswith(CHECKSUM_PREFIX)
    assert len(value) == CHECKSUM_LENGTH
    assert is_checksum(value)


def test_the_checksum_is_over_the_canonical_bytes() -> None:
    """The property ADR-050 rests on: a digest of the bytes, so the agent can
    verify exactly what it received without a second JSON encoder.
    """
    document = {"b": 2, "a": 1}
    assert checksum(document) == checksum_of_bytes(canonicalize(document))


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "sha256:",
        # A bare digest, no prefix.
        "a" * 64,
        # Right length, wrong prefix.
        "sha512:" + "a" * 64,
        # Uppercase hex: refused, so one digest has one spelling.
        CHECKSUM_PREFIX + "A" * 64,
        # Non-hex.
        CHECKSUM_PREFIX + "z" * 64,
        # Too short, too long.
        CHECKSUM_PREFIX + "a" * 63,
        CHECKSUM_PREFIX + "a" * 65,
    ],
)
def test_a_malformed_checksum_is_not_mistaken_for_one(value: object) -> None:
    assert is_checksum(value) is False


# ---------------------------------------------------------------------------
# Resolution: the three states of "enabled"
# ---------------------------------------------------------------------------


def test_nothing_configured_resolves_to_every_module_off() -> None:
    """A freshly enrolled agent collects nothing (§2.1)."""
    effective = resolve_host_configuration(global_defaults={}, host_overrides={})
    assert effective.enabled_module_ids == ()
    assert [module.module_id for module in effective.modules] == list(MODULE_IDS)
    for module in effective.modules:
        assert module.enabled.value is False
        assert module.enabled.source is ValueSource.DEFAULT


def test_an_unconfigured_module_offers_its_registry_interval() -> None:
    effective = resolve_host_configuration(global_defaults={}, host_overrides={})
    for module in effective.modules:
        assert isinstance(module.schedule.value, IntervalSchedule)
        assert (
            module.schedule.value.interval_seconds
            == descriptor(module.module_id).default_interval_seconds
        )
        assert module.schedule.source is ValueSource.DEFAULT


def test_a_global_default_applies_and_reports_its_source() -> None:
    effective = resolve_host_configuration(
        global_defaults={"cpu": ModuleSetting(enabled=True)}, host_overrides={}
    )
    cpu = effective.module("cpu")
    assert cpu is not None
    assert cpu.enabled.value is True
    assert cpu.enabled.source is ValueSource.GLOBAL
    assert effective.enabled_module_ids == ("cpu",)


def test_a_host_override_beats_a_global_default() -> None:
    effective = resolve_host_configuration(
        global_defaults={"cpu": ModuleSetting(enabled=True)},
        host_overrides={"cpu": ModuleSetting(enabled=False)},
    )
    cpu = effective.module("cpu")
    assert cpu is not None
    assert cpu.enabled.value is False
    assert cpu.enabled.source is ValueSource.HOST


def test_inherited_false_and_overridden_false_are_distinguishable() -> None:
    """The mission's §5 warning, as the one test that proves it is not an issue.

    Both hosts collect nothing from ``cpu``. They differ in *why*, and the
    difference is what lets an interface answer "is this host customised?".
    """
    inherited = resolve_host_configuration(
        global_defaults={"cpu": ModuleSetting(enabled=False)}, host_overrides={}
    )
    overridden = resolve_host_configuration(
        global_defaults={}, host_overrides={"cpu": ModuleSetting(enabled=False)}
    )
    for effective in (inherited, overridden):
        cpu = effective.module("cpu")
        assert cpu is not None
        assert cpu.enabled.value is False

    assert inherited.module("cpu").enabled.source is ValueSource.GLOBAL  # type: ignore[union-attr]
    assert overridden.module("cpu").enabled.source is ValueSource.HOST  # type: ignore[union-attr]


def test_unset_is_a_third_state_distinct_from_both_false_values() -> None:
    nothing_said = resolve_host_configuration(global_defaults={}, host_overrides={})
    cpu = nothing_said.module("cpu")
    assert cpu is not None
    assert cpu.enabled.value is False
    assert cpu.enabled.source is ValueSource.DEFAULT


def test_a_row_that_overrides_nothing_resolves_as_if_it_were_absent() -> None:
    """Legal in the schema, and the service does not create one — but it must
    not resolve differently from no row if it ever exists.
    """
    with_empty = resolve_host_configuration(
        global_defaults={"cpu": ModuleSetting(enabled=True)},
        host_overrides={"cpu": ModuleSetting()},
    )
    without = resolve_host_configuration(
        global_defaults={"cpu": ModuleSetting(enabled=True)}, host_overrides={}
    )
    assert with_empty.to_document() == without.to_document()


def test_a_host_may_override_the_interval_while_inheriting_enabled() -> None:
    """The common case, and the reason nullability is per column, not per row."""
    effective = resolve_host_configuration(
        global_defaults={
            "cpu": ModuleSetting(enabled=True, schedule=IntervalSchedule(interval_seconds=30))
        },
        host_overrides={"cpu": ModuleSetting(schedule=IntervalSchedule(interval_seconds=5))},
    )
    cpu = effective.module("cpu")
    assert cpu is not None
    assert (cpu.enabled.value, cpu.enabled.source) == (True, ValueSource.GLOBAL)
    assert cpu.schedule.value.interval_seconds == 5  # type: ignore[union-attr]
    assert cpu.schedule.source is ValueSource.HOST


def test_every_module_appears_in_the_document_including_the_disabled_ones() -> None:
    """So "this module is off" and "this agent is too old to know about this
    module" stay distinguishable (``AGENT_ARCHITECTURE.md`` §6.2).
    """
    document = resolve_host_configuration(
        global_defaults={"cpu": ModuleSetting(enabled=True)}, host_overrides={}
    ).to_document()
    assert set(document) == set(MODULE_IDS)
    assert document["cpu"]["enabled"] is True
    assert document["memory"]["enabled"] is False


def test_the_document_omits_provenance() -> None:
    """The agent cannot act on it, and including it would change the checksum
    when an operator moved a setting between layers without changing its effect.
    """
    document = resolve_host_configuration(
        global_defaults={"cpu": ModuleSetting(enabled=True)}, host_overrides={}
    ).to_document()
    assert set(document["cpu"]) == {"enabled", "schedule"}
    assert "source" not in json.dumps(document)


def test_moving_a_setting_between_layers_without_changing_it_does_not_change_the_document() -> None:
    globally = resolve_host_configuration(
        global_defaults={
            "cpu": ModuleSetting(enabled=True, schedule=IntervalSchedule(interval_seconds=30))
        },
        host_overrides={},
    )
    per_host = resolve_host_configuration(
        global_defaults={},
        host_overrides={
            "cpu": ModuleSetting(enabled=True, schedule=IntervalSchedule(interval_seconds=30))
        },
    )
    assert globally.to_document() == per_host.to_document()


# ---------------------------------------------------------------------------
# Resolution: conforming to a changed registry
# ---------------------------------------------------------------------------


def test_an_interval_below_a_raised_floor_is_clamped_and_reported() -> None:
    """A floor can be raised by an upgrade, which makes a stored override
    invalid through nobody's fault. Collection keeps working and the adjustment
    is surfaced, rather than the host becoming unconfigurable or the setting
    silently doing something else.
    """
    effective = resolve_host_configuration(
        global_defaults={},
        # `load`'s floor is 5s; 1s was legal for a module with a 1s floor.
        host_overrides={
            "load": ModuleSetting(enabled=True, schedule=IntervalSchedule(interval_seconds=1))
        },
    )
    load = effective.module("load")
    assert load is not None
    assert load.schedule.value.interval_seconds == 5  # type: ignore[union-attr]
    assert any("below this module's minimum of 5s" in problem for problem in effective.problems)


def test_a_clamped_interval_keeps_its_source() -> None:
    """It is still the host's setting, raised — not a value the registry chose."""
    effective = resolve_host_configuration(
        global_defaults={},
        host_overrides={"load": ModuleSetting(schedule=IntervalSchedule(interval_seconds=1))},
    )
    load = effective.module("load")
    assert load is not None
    assert load.schedule.source is ValueSource.HOST


def test_a_schedule_kind_the_module_no_longer_permits_falls_back_and_is_reported() -> None:
    """There is no nearby legal value to clamp to, so the module's default
    interval is used — a behaviour change, reported in the strongest terms the
    problem list allows.
    """
    effective = resolve_host_configuration(
        global_defaults={},
        host_overrides={
            "cpu": ModuleSetting(
                enabled=True,
                schedule=CalendarSchedule(timezone="UTC", times=["03:00"]),
            )
        },
    )
    cpu = effective.module("cpu")
    assert cpu is not None
    assert isinstance(cpu.schedule.value, IntervalSchedule)
    assert cpu.schedule.value.interval_seconds == descriptor("cpu").default_interval_seconds
    assert any("no longer permitted" in problem for problem in effective.problems)


def test_configuration_for_a_module_the_registry_dropped_is_ignored_and_reported() -> None:
    """Refusing to produce a configuration would strand the host."""
    effective = resolve_host_configuration(
        global_defaults={"docker": ModuleSetting(enabled=True)}, host_overrides={}
    )
    assert set(effective.to_document()) == set(MODULE_IDS)
    assert effective.problems == (
        "configuration exists for unknown module 'docker' and was ignored",
    )


def test_problems_are_not_errors() -> None:
    """The configuration is valid and in use; the problems are adjustments."""
    effective = resolve_host_configuration(
        global_defaults={},
        host_overrides={
            "load": ModuleSetting(enabled=True, schedule=IntervalSchedule(interval_seconds=1))
        },
    )
    assert effective.problems
    assert effective.enabled_module_ids == ("load",)


# ---------------------------------------------------------------------------
# Save-time validation
# ---------------------------------------------------------------------------


def test_a_valid_setting_is_accepted_and_parsed() -> None:
    setting = validate_setting(
        "cpu", enabled=True, schedule={"kind": "interval", "interval_seconds": 15}
    )
    assert setting.enabled is True
    assert isinstance(setting.schedule, IntervalSchedule)
    assert setting.schedule.interval_seconds == 15


def test_a_null_schedule_means_inherit_and_is_not_an_error() -> None:
    setting = validate_setting("cpu", enabled=True, schedule=None)
    assert setting.schedule is None
    assert setting.enabled is True


def test_a_null_enabled_means_inherit() -> None:
    setting = validate_setting("cpu", enabled=None, schedule=None)
    assert setting.enabled is None


def test_an_interval_below_the_module_floor_is_refused_at_save_time() -> None:
    """Refused here rather than clamped: the operator is present, so they get
    an error they can act on instead of a value they did not choose.
    """
    with pytest.raises(ScheduleError) as caught:
        validate_setting("load", enabled=True, schedule={"kind": "interval", "interval_seconds": 1})
    assert "at least 5" in caught.value.problems[0]


def test_a_kind_the_module_does_not_accept_is_refused_at_save_time() -> None:
    with pytest.raises(ScheduleError) as caught:
        validate_setting(
            "cpu",
            enabled=True,
            schedule={"kind": "calendar", "timezone": "UTC", "times": ["03:00"]},
        )
    assert "does not accept a calendar schedule" in caught.value.problems[0]
    assert "permitted: interval" in caught.value.problems[0]


def test_an_unknown_module_is_refused_at_save_time() -> None:
    with pytest.raises(UnknownModuleError):
        validate_setting("docker", enabled=True, schedule=None)


@pytest.mark.parametrize("kind", ["event", "manual"])
def test_a_reserved_schedule_kind_is_refused_rather_than_half_implemented(kind: str) -> None:
    """They are in the enum so a descriptor can name them. Nothing in this
    phase can be configured with one, and there is deliberately no half-built
    path for a later module to inherit.
    """
    with pytest.raises(ScheduleError, match="not supported in this release"):
        parse_schedule({"kind": kind})


# ---------------------------------------------------------------------------
# The configuration document
# ---------------------------------------------------------------------------


def test_a_revision_document_carries_the_shape_and_registry_versions() -> None:
    revision = build_revision(
        host_id=HOST_ID,
        version=1,
        effective=resolve_host_configuration(global_defaults={}, host_overrides={}),
        generated_at=NOW,
    )
    document = json.loads(revision.document)
    assert document["document_version"] == DOCUMENT_VERSION
    assert document["registry_version"] == registry.REGISTRY_VERSION
    assert document["host_id"] == str(HOST_ID)
    assert document["version"] == 1
    assert document["generated_at"] == NOW.isoformat()


def test_the_document_carries_the_limits_the_agent_revalidates_against() -> None:
    """So the agent's validation agrees with the controller's by construction
    rather than by both being upgraded together.
    """
    revision = build_revision(
        host_id=HOST_ID,
        version=1,
        effective=resolve_host_configuration(global_defaults={}, host_overrides={}),
        generated_at=NOW,
    )
    limits = json.loads(revision.document)["limits"]
    assert limits["min_interval_seconds"] == MIN_INTERVAL_SECONDS
    assert limits["max_interval_seconds"] == MAX_INTERVAL_SECONDS
    assert limits["module_min_interval_seconds"] == {
        module_id: descriptor(module_id).min_interval_seconds for module_id in MODULE_IDS
    }


def test_the_document_has_no_capabilities_key() -> None:
    """Absent rather than present with every value false. An all-false map
    would assert that a capability system exists, which it does not — and an
    agent finding one could reasonably infer there is a mechanism to grant.
    """
    revision = build_revision(
        host_id=HOST_ID,
        version=1,
        effective=resolve_host_configuration(global_defaults={}, host_overrides={}),
        generated_at=NOW,
    )
    assert "capabilities" not in json.loads(revision.document)


def test_the_checksum_is_not_inside_the_document() -> None:
    """It could not be: a digest over a document containing itself has no
    fixed point.
    """
    revision = build_revision(
        host_id=HOST_ID,
        version=1,
        effective=resolve_host_configuration(global_defaults={}, host_overrides={}),
        generated_at=NOW,
    )
    assert "checksum" not in json.loads(revision.document)
    assert revision.checksum == checksum_of_bytes(revision.document)


def test_the_same_inputs_produce_the_same_checksum() -> None:
    """Which is why ``generated_at`` is a parameter rather than a clock read."""
    effective = resolve_host_configuration(global_defaults={}, host_overrides={})
    left = build_revision(host_id=HOST_ID, version=1, effective=effective, generated_at=NOW)
    right = build_revision(host_id=HOST_ID, version=1, effective=effective, generated_at=NOW)
    assert left.checksum == right.checksum
    assert left.document == right.document


def test_a_different_generated_at_changes_the_checksum_but_not_the_content_checksum() -> None:
    """The reason there are two digests. Without the content one, every
    reconnection would mint a revision and a host nobody had touched would
    report itself permanently out of date.
    """
    effective = resolve_host_configuration(global_defaults={}, host_overrides={})
    first = build_revision(host_id=HOST_ID, version=1, effective=effective, generated_at=NOW)
    later = build_revision(
        host_id=HOST_ID,
        version=1,
        effective=effective,
        generated_at=datetime(2026, 10, 12, 13, 0, 0, tzinfo=UTC),
    )
    assert first.checksum != later.checksum
    assert first.content_checksum == later.content_checksum


def test_a_different_version_changes_the_checksum_but_not_the_content_checksum() -> None:
    effective = resolve_host_configuration(global_defaults={}, host_overrides={})
    first = build_revision(host_id=HOST_ID, version=1, effective=effective, generated_at=NOW)
    second = build_revision(host_id=HOST_ID, version=2, effective=effective, generated_at=NOW)
    assert first.checksum != second.checksum
    assert first.content_checksum == second.content_checksum


def test_a_configuration_change_changes_the_content_checksum() -> None:
    off = build_revision(
        host_id=HOST_ID,
        version=1,
        effective=resolve_host_configuration(global_defaults={}, host_overrides={}),
        generated_at=NOW,
    )
    on = build_revision(
        host_id=HOST_ID,
        version=1,
        effective=resolve_host_configuration(
            global_defaults={"cpu": ModuleSetting(enabled=True)}, host_overrides={}
        ),
        generated_at=NOW,
    )
    assert off.content_checksum != on.content_checksum


def test_the_content_checksum_does_not_depend_on_the_host() -> None:
    """Two hosts with identical effective configuration have the same content
    digest. Harmless — the comparison is always within one host — and pinned
    because the alternative would make the digest useless for answering
    "which hosts differ from the global defaults?".
    """
    effective = resolve_host_configuration(
        global_defaults={"cpu": ModuleSetting(enabled=True)}, host_overrides={}
    )
    left = build_revision(host_id=HOST_ID, version=1, effective=effective, generated_at=NOW)
    right = build_revision(host_id=uuid.uuid4(), version=1, effective=effective, generated_at=NOW)
    assert left.content_checksum == right.content_checksum
    assert left.checksum != right.checksum


def test_the_document_text_round_trips_the_bytes_exactly() -> None:
    """Stored as ``text``, so the digest must survive the decode."""
    revision = build_revision(
        host_id=HOST_ID,
        version=1,
        effective=resolve_host_configuration(global_defaults={}, host_overrides={}),
        generated_at=NOW,
    )
    assert revision.text.encode("utf-8") == revision.document
    assert checksum_of_bytes(revision.text.encode("utf-8")) == revision.checksum


def test_a_document_contains_no_float_anywhere() -> None:
    """Enforced by :func:`canonicalize`, asserted here against the real
    document so that a future field carrying a percentage as a float fails.
    """
    revision = build_revision(
        host_id=HOST_ID,
        version=1,
        effective=resolve_host_configuration(
            global_defaults={"cpu": ModuleSetting(enabled=True)}, host_overrides={}
        ),
        generated_at=NOW,
    )

    def assert_no_floats(value: object, path: str = "$") -> None:
        if isinstance(value, float):
            # AssertionError, not TypeError: this is a test assertion about the
            # document's shape, not a function rejecting a caller's argument.
            raise AssertionError(f"{path} is a float")  # noqa: TRY004
        if isinstance(value, dict):
            for key, item in value.items():
                assert_no_floats(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                assert_no_floats(item, f"{path}[{index}]")

    assert_no_floats(json.loads(revision.document))
