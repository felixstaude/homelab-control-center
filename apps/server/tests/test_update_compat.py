"""Release discovery and compatibility: the whole decision table.

This module is pure — no clock, no database, no I/O — which is the reason it
can be tested exhaustively, and the reason it is worth doing so. Everything
downstream (the policy engine, the rollout scheduler, the interface) reads an
`UpdatePlan` and trusts it.

The cases that earn their place here are the ones where a plausible
implementation gets it wrong: a blocked update reported as "no update", a beta
offered to a stable host, a pin that silently becomes advisory, and an absent
compatibility field read as a prohibition rather than as "no constraint".
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from hcc.agents.releases import ReleaseManifest, parse_release_manifest
from hcc.updates.compat import (
    PROTOCOL_UNKNOWN_BLOCKS_AUTOMATIC,
    BlockerCode,
    Outcome,
    evaluate_compatibility,
    permitted_channels,
    plan_update,
    version_distribution,
)
from hcc.updates.version import StepKind, parse

DIGEST = "a" * 64
AMD64 = "linux/amd64"
ARM64 = "linux/arm64"

CONTROLLER = "0.2.0"
PROTOCOLS = (1, 2)
SCHEMA = frozenset({"0009_agent_queue_health", "0010_update_management"})


def artifact(name: str = "a.tar.gz") -> dict[str, Any]:
    return {
        "archive": name,
        "url": f"https://example.com/{name}",
        "sha256": DIGEST,
        "size_bytes": 3_000_000,
        "tier": "supported",
    }


def manifest(
    releases: dict[str, Any],
    *,
    recommended: str | None = None,
    minimum_supported: str = "0.1.0",
    channels: dict[str, str] | None = None,
) -> ReleaseManifest:
    document = {
        "schema_version": 2,
        "recommended": recommended or next(iter(releases)),
        "minimum_supported": minimum_supported,
        "channels": channels if channels is not None else {},
        "releases": releases,
    }
    return parse_release_manifest(json.dumps(document).encode())


def release(
    *,
    channel: str = "stable",
    protocols: list[int] | None = None,
    min_controller: str | None = None,
    schema: str | None = None,
    platforms: list[str] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "channel": channel,
        "platforms": {name: artifact() for name in (platforms or [AMD64])},
    }
    if protocols is not None:
        entry["protocol_versions"] = protocols
    if min_controller is not None:
        entry["min_controller"] = min_controller
    if schema is not None:
        entry["schema_requirement"] = schema
    return entry


def plan(installed: str | None, releases: dict[str, Any], **kwargs: Any) -> Any:
    defaults: dict[str, Any] = {
        "host_platform": AMD64,
        "controller_version": CONTROLLER,
        "supported_protocol_versions": PROTOCOLS,
        "applied_schema_revisions": SCHEMA,
        # The ordinary case. Authenticity has its own tests below; stating it
        # here keeps every other test about the thing it is named after.
        "metadata_authentic": True,
    }
    manifest_kwargs = {
        key: kwargs.pop(key)
        for key in ("recommended", "minimum_supported", "channels")
        if key in kwargs
    }
    defaults.update(kwargs)
    return plan_update(
        manifest(releases, **manifest_kwargs), installed_version=installed, **defaults
    )


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


class TestChannelsMeanTheMostInstabilityAccepted:
    def test_stable_accepts_only_stable(self) -> None:
        assert permitted_channels("stable") == ("stable",)

    def test_beta_also_accepts_stable(self) -> None:
        # Somebody who opted into pre-releases should still receive an
        # ordinary release that happens to be newer. A beta subscriber who
        # stops getting stable updates has been punished for helping.
        assert permitted_channels("beta") == ("stable", "beta")

    def test_alpha_accepts_everything(self) -> None:
        assert permitted_channels("alpha") == ("stable", "beta", "alpha")

    def test_an_unknown_channel_fails_closed(self) -> None:
        # A typo in a stored policy must not widen what a host accepts.
        assert permitted_channels("nightly") == ("stable",)
        assert permitted_channels("") == ("stable",)

    def test_a_stable_host_is_not_offered_a_beta(self) -> None:
        # The thing channels exist to prevent.
        result = plan(
            "0.1.0",
            {
                "0.1.0": release(),
                "0.3.0-beta.1": release(channel="beta"),
            },
        )

        assert result.outcome is Outcome.UP_TO_DATE
        assert result.target == parse("0.1.0")

    def test_a_beta_host_is_offered_the_beta(self) -> None:
        result = plan(
            "0.1.0",
            {"0.1.0": release(), "0.3.0-beta.1": release(channel="beta")},
            channel="beta",
        )

        assert result.outcome is Outcome.UPDATE_AVAILABLE
        assert result.target == parse("0.3.0-beta.1")
        assert result.target_channel == "beta"

    def test_a_beta_host_prefers_a_newer_stable_over_an_older_beta(self) -> None:
        # The subset rule doing its job: newest wins across the permitted set,
        # rather than "beta if there is one".
        result = plan(
            "0.1.0",
            {"0.4.0": release(), "0.3.0-beta.1": release(channel="beta")},
            channel="beta",
        )

        assert result.target == parse("0.4.0")
        assert result.target_channel == "stable"


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


class TestOutcomes:
    def test_up_to_date(self) -> None:
        result = plan("0.2.0", {"0.2.0": release()})

        assert result.outcome is Outcome.UP_TO_DATE
        assert result.step == StepKind.NONE
        assert not result.has_update

    def test_a_newer_installed_version_is_still_up_to_date(self) -> None:
        # A host running something newer than the controller knows about is
        # not "behind", and reporting an update to an older release would be
        # offering a downgrade as an upgrade.
        result = plan("0.9.0", {"0.2.0": release()})

        assert result.outcome is Outcome.UP_TO_DATE

    def test_update_available(self) -> None:
        result = plan("0.1.0", {"0.1.0": release(), "0.1.1": release()})

        assert result.outcome is Outcome.UPDATE_AVAILABLE
        assert result.target == parse("0.1.1")
        assert result.step == StepKind.PATCH
        assert result.has_update
        assert result.outcome.is_actionable
        assert result.artifact is not None

    def test_it_picks_the_newest_not_the_recommended(self) -> None:
        # `recommended` is what a *fresh install* should take. The newest in
        # the channel is what an existing host should move to, and a manifest
        # whose recommended lags behind must not pin the fleet.
        result = plan(
            "0.1.0",
            {"0.1.0": release(), "0.1.1": release(), "0.10.0": release()},
            recommended="0.1.1",
        )

        assert result.target == parse("0.10.0")

    def test_it_does_not_order_releases_lexicographically(self) -> None:
        # 0.9.0 vs 0.10.0 through the whole planner, not just the comparator.
        result = plan("0.9.0", {"0.9.0": release(), "0.10.0": release()})

        assert result.outcome is Outcome.UPDATE_AVAILABLE
        assert result.target == parse("0.10.0")

    def test_an_unparseable_installed_version_is_unknown_not_an_error(self) -> None:
        # The string is advisory and arrives from outside. It has to be a
        # renderable state, not a 500 in the planner.
        result = plan("hand-built-tuesday", {"0.1.0": release()})

        assert result.outcome is Outcome.UNKNOWN_VERSION
        assert result.installed is None
        # The newest is still reported, so the interface has something to show.
        assert result.target == parse("0.1.0")
        assert result.detail is not None
        assert "hand-built-tuesday" in result.detail

    def test_a_host_that_has_never_reported_is_unknown(self) -> None:
        result = plan(None, {"0.1.0": release()})

        assert result.outcome is Outcome.UNKNOWN_VERSION

    def test_a_version_below_minimum_supported_is_unsupported(self) -> None:
        # Distinguished from "an update is available": this may need a
        # reinstall rather than an upgrade, which is a different conversation.
        result = plan("0.0.1", {"0.1.0": release(), "0.2.0": release()}, minimum_supported="0.1.0")

        assert result.outcome is Outcome.UNSUPPORTED
        assert result.detail is not None
        assert "reinstalling the agent" in result.detail

    def test_metadata_unavailable_is_not_no_update(self) -> None:
        result = plan_update(
            None,
            installed_version="0.1.0",
            host_platform=AMD64,
            controller_version=CONTROLLER,
            supported_protocol_versions=PROTOCOLS,
        )

        assert result.outcome is Outcome.METADATA_UNAVAILABLE
        assert not result.has_update
        assert result.detail is not None
        assert "does not know what versions exist" in result.detail

    def test_a_manifest_with_nothing_in_the_permitted_channel(self) -> None:
        # Not "up to date": the controller has metadata and it offers this host
        # nothing, which is a different thing and worth saying.
        result = plan("0.1.0", {"0.3.0-beta.1": release(channel="beta")})

        assert result.outcome is Outcome.METADATA_UNAVAILABLE
        assert result.detail is not None
        assert "no release in stable" in result.detail


# ---------------------------------------------------------------------------
# Compatibility
# ---------------------------------------------------------------------------


class TestCompatibilityBlocksWithoutHiding:
    def test_a_blocked_update_is_still_reported_as_an_update(self) -> None:
        # The failure this prevents: a dashboard showing "no update available"
        # for a host that is stuck, which makes a stuck fleet look current.
        result = plan("0.1.0", {"0.1.0": release(), "0.2.0": release(protocols=[99])})

        assert result.outcome is Outcome.UPDATE_AVAILABLE_INCOMPATIBLE
        assert result.has_update
        assert not result.outcome.is_actionable
        assert BlockerCode.PROTOCOL_MISMATCH.value in [b.code.value for b in result.blockers]

    def test_a_protocol_mismatch_names_both_sides(self) -> None:
        result = plan("0.1.0", {"0.2.0": release(protocols=[7, 8])})

        (blocker,) = result.blockers
        assert "7, 8" in blocker.detail
        assert "1, 2" in blocker.detail

    def test_a_controller_too_old_says_to_upgrade_it_first(self) -> None:
        result = plan("0.1.0", {"0.2.0": release(protocols=[1], min_controller="9.0.0")})

        (blocker,) = result.blockers
        assert blocker.code is BlockerCode.CONTROLLER_TOO_OLD
        assert "Upgrade the controller first" in blocker.detail

    def test_a_missing_migration_says_to_migrate_first(self) -> None:
        result = plan("0.1.0", {"0.2.0": release(protocols=[1], schema="0099_not_applied")})

        (blocker,) = result.blockers
        assert blocker.code is BlockerCode.SCHEMA_TOO_OLD
        assert "0099_not_applied" in blocker.detail

    def test_schema_is_a_membership_test_not_an_ordering(self) -> None:
        # Alembic revisions are a linked list with opaque identifiers.
        # "Is this revision applied" is exactly the question and trivially
        # correct; "is it at or before the head" would need the migration graph.
        result = plan(
            "0.1.0",
            {"0.2.0": release(protocols=[1], schema="0009_agent_queue_health")},
        )

        assert result.outcome is Outcome.UPDATE_AVAILABLE

    def test_a_missing_architecture_lists_what_is_available(self) -> None:
        result = plan(
            "0.1.0",
            {"0.2.0": release(protocols=[1], platforms=[ARM64])},
            host_platform=AMD64,
        )

        (blocker,) = result.blockers
        assert blocker.code is BlockerCode.PLATFORM_UNAVAILABLE
        assert ARM64 in blocker.detail

    def test_every_blocker_is_reported_not_just_the_first(self) -> None:
        # An administrator fixing one wants to know about the rest now.
        result = plan(
            "0.1.0",
            {
                "0.2.0": release(
                    protocols=[99],
                    min_controller="9.0.0",
                    schema="0099_nope",
                    platforms=[ARM64],
                )
            },
        )

        assert set(result.blockers and [b.code for b in result.blockers]) == {
            BlockerCode.PROTOCOL_MISMATCH,
            BlockerCode.CONTROLLER_TOO_OLD,
            BlockerCode.SCHEMA_TOO_OLD,
            BlockerCode.PLATFORM_UNAVAILABLE,
        }

    def test_absent_constraints_do_not_block(self) -> None:
        # A schema-v1 release declared nothing. Inventing requirements it never
        # stated would withhold releases that were installable before the
        # manifest upgraded.
        compatibility = evaluate_compatibility(
            manifest({"0.2.0": release()}).releases["0.2.0"],
            controller_version=CONTROLLER,
            supported_protocol_versions=PROTOCOLS,
            applied_schema_revisions=frozenset(),
            host_platform=AMD64,
            automatic=False,
            metadata_authentic=True,
        )

        assert compatibility.ok
        assert compatibility.codes == ()

    def test_an_undeclared_protocol_blocks_an_automatic_update_only(self) -> None:
        # The one place absence is treated as a blocker, and only for an
        # automatic update: "we do not know" is not permission, and an
        # automatic update is exactly where there is no human to notice.
        assert PROTOCOL_UNKNOWN_BLOCKS_AUTOMATIC

        entry = manifest({"0.2.0": release()}).releases["0.2.0"]
        shared: dict[str, Any] = {
            "controller_version": CONTROLLER,
            "supported_protocol_versions": PROTOCOLS,
            "applied_schema_revisions": frozenset[str](),
            "host_platform": AMD64,
        }

        assert evaluate_compatibility(entry, automatic=False, metadata_authentic=True, **shared).ok
        automatic = evaluate_compatibility(entry, automatic=True, metadata_authentic=True, **shared)
        assert not automatic.ok
        assert automatic.codes == (BlockerCode.PROTOCOL_UNDECLARED.value,)

    def test_an_unparseable_controller_version_blocks_a_constrained_release(self) -> None:
        # A development build reports "dev". A release that constrains the
        # controller version cannot be evaluated against that, and the honest
        # answer is a blocker rather than silently allowing it.
        result = plan(
            "0.1.0",
            {"0.2.0": release(protocols=[1], min_controller="0.1.0")},
            controller_version="dev",
        )

        (blocker,) = result.blockers
        assert blocker.code is BlockerCode.CONTROLLER_TOO_OLD
        assert "not a version that can be compared" in blocker.detail


# ---------------------------------------------------------------------------
# Pins
# ---------------------------------------------------------------------------


class TestPins:
    def test_a_host_at_its_pin_is_pinned(self) -> None:
        result = plan("0.1.0", {"0.1.0": release(), "0.2.0": release()}, pinned_version="0.1.0")

        assert result.outcome is Outcome.PINNED
        assert not result.has_update
        assert result.detail is not None
        assert "pinned to 0.1.0" in result.detail

    def test_a_pin_ahead_of_the_host_is_an_update(self) -> None:
        result = plan(
            "0.1.0",
            {"0.1.0": release(), "0.2.0": release(protocols=[1]), "0.9.0": release()},
            pinned_version="0.2.0",
        )

        assert result.outcome is Outcome.UPDATE_AVAILABLE
        # The pin wins over "newest available": 0.9.0 is not offered.
        assert result.target == parse("0.2.0")

    def test_a_pin_behind_the_host_is_a_downgrade_and_is_refused(self) -> None:
        result = plan(
            "0.2.0", {"0.1.0": release(protocols=[1]), "0.2.0": release()}, pinned_version="0.1.0"
        )

        assert result.outcome is Outcome.DOWNGRADE
        assert BlockerCode.DOWNGRADE_NOT_AUTHORIZED in [b.code for b in result.blockers]
        assert not result.outcome.is_actionable or result.blockers

    def test_an_authorized_downgrade_has_no_authorization_blocker(self) -> None:
        result = plan(
            "0.2.0",
            {"0.1.0": release(protocols=[1]), "0.2.0": release()},
            pinned_version="0.1.0",
            allow_downgrade=True,
        )

        assert result.outcome is Outcome.DOWNGRADE
        assert result.blockers == ()

    def test_a_pin_overrides_the_channel(self) -> None:
        # Pinning a beta on a stable host is a deliberate act. Refusing it
        # would make the pin advisory, and a pin that is sometimes ignored is
        # worse than no pin.
        result = plan(
            "0.1.0",
            {"0.1.0": release(), "0.3.0-beta.1": release(channel="beta", protocols=[1])},
            pinned_version="0.3.0-beta.1",
            channel="stable",
        )

        assert result.outcome is Outcome.UPDATE_AVAILABLE
        assert result.target == parse("0.3.0-beta.1")

    def test_a_pin_to_an_unknown_version_says_to_fix_the_pin(self) -> None:
        result = plan("0.1.0", {"0.1.0": release()}, pinned_version="9.9.9")

        assert result.outcome is Outcome.UNKNOWN_VERSION
        assert result.detail is not None
        assert "Change or clear the pin" in result.detail

    def test_a_pin_to_a_non_version_is_rejected_not_crashed(self) -> None:
        result = plan("0.1.0", {"0.1.0": release()}, pinned_version="latest")

        assert result.outcome is Outcome.UNKNOWN_VERSION

    def test_a_pinned_update_still_checks_compatibility(self) -> None:
        # A pin authorizes a version, not an incompatibility.
        result = plan(
            "0.1.0",
            {"0.1.0": release(), "0.2.0": release(protocols=[99])},
            pinned_version="0.2.0",
        )

        assert result.outcome is Outcome.UPDATE_AVAILABLE_INCOMPATIBLE
        assert BlockerCode.PROTOCOL_MISMATCH in [b.code for b in result.blockers]


# ---------------------------------------------------------------------------
# The fleet view
# ---------------------------------------------------------------------------


class TestVersionDistribution:
    def test_it_sorts_by_precedence_newest_first(self) -> None:
        # By version rather than by count, because the useful reading is "how
        # far behind is the tail".
        assert version_distribution({"0.9.0": 3, "0.10.0": 1, "0.2.0": 7}) == [
            ("0.10.0", 1),
            ("0.9.0", 3),
            ("0.2.0", 7),
        ]

    def test_unparseable_versions_sort_last_and_stay_visible(self) -> None:
        # Visible rather than hidden or crashing the sort: a host reporting
        # nonsense is something an operator needs to see.
        assert version_distribution({"dev": 1, "0.1.0": 2, "???": 1}) == [
            ("0.1.0", 2),
            ("???", 1),
            ("dev", 1),
        ]

    def test_an_empty_fleet_is_an_empty_list(self) -> None:
        assert version_distribution({}) == []


class TestTheOutcomeEnumIsClosed:
    def test_every_outcome_has_a_distinct_value(self) -> None:
        values = [outcome.value for outcome in Outcome]

        assert len(values) == len(set(values))

    @pytest.mark.parametrize("outcome", list(Outcome))
    def test_only_two_outcomes_are_actionable(self, outcome: Outcome) -> None:
        # An administrator can start an update from exactly these. Asserted
        # per-member so adding one cannot quietly become actionable.
        expected = outcome in {Outcome.UPDATE_AVAILABLE, Outcome.DOWNGRADE}

        assert outcome.is_actionable is expected


def codes_of(result: Any) -> tuple[str, ...]:
    """The blocker codes on a plan, in order.

    `Compatibility` has this as a property and `UpdatePlan` deliberately does
    not — a plan is read by the API, which renders each blocker's detail
    alongside its code, so a codes-only view would be a shortcut that exists
    for tests. Here it is, in the tests.
    """
    return tuple(blocker.code.value for blocker in result.blockers)


class TestMetadataNobodyCanVouchForIsNotOffered:
    """The clause the end-to-end scenario added.

    Before it, a controller holding a manifest signed by a key it did not
    trust reported ``untrusted_key`` on its overview *and* offered the update
    anyway. The agent then declined it, because the agent's signature gate is
    unconditional — so the administrator pressed "install", waited, and got a
    failure about signatures on a page about versions.
    """

    def test_an_unauthenticated_manifest_blocks_the_update(self) -> None:
        result = plan(
            "0.1.0",
            {"0.1.0": release(), "0.2.0": release()},
            metadata_authentic=False,
        )

        assert result.outcome is Outcome.UPDATE_AVAILABLE_INCOMPATIBLE
        assert BlockerCode.METADATA_UNAUTHENTICATED.value in codes_of(result)

    def test_the_update_is_still_reported_as_existing(self) -> None:
        # Blocked, not hidden. A host held back by a missing signing key is a
        # host somebody has to do something about, and an interface that
        # showed it as up to date would hide the thing that needs fixing.
        result = plan(
            "0.1.0",
            {"0.1.0": release(), "0.2.0": release()},
            metadata_authentic=False,
        )

        assert result.has_update
        assert result.target is not None
        assert str(result.target) == "0.2.0"

    def test_it_is_not_actionable_even_for_a_manual_start(self) -> None:
        # The important half. `automatic=False` means a human is in the loop,
        # which relaxes the undeclared-protocol rule — and must not relax this
        # one, because the agent will refuse whatever the human intended.
        result = plan(
            "0.1.0",
            {"0.1.0": release(), "0.2.0": release()},
            metadata_authentic=False,
            automatic=False,
        )

        assert not result.outcome.is_actionable

    def test_the_blocker_names_the_setting_that_fixes_it(self) -> None:
        result = plan(
            "0.1.0",
            {"0.1.0": release(), "0.2.0": release()},
            metadata_authentic=False,
        )

        detail = next(
            blocker.detail
            for blocker in result.blockers
            if blocker.code is BlockerCode.METADATA_UNAUTHENTICATED
        )
        assert "HCC_RELEASE_SIGNING_KEYS" in detail

    def test_a_pinned_host_is_blocked_too(self) -> None:
        # A pin is the strongest statement an administrator can make and it
        # still does not establish who built the bytes.
        result = plan(
            "0.1.0",
            {"0.1.0": release(), "0.2.0": release()},
            pinned_version="0.2.0",
            metadata_authentic=False,
        )

        assert BlockerCode.METADATA_UNAUTHENTICATED.value in codes_of(result)
        assert not result.outcome.is_actionable

    def test_authenticity_is_not_assumed_by_default(self) -> None:
        # `plan_update`'s default. A caller that has not verified a signature
        # has not verified one, and the other default would mean every caller
        # who forgot the argument silently offered unverifiable updates.
        result = plan_update(
            manifest({"0.1.0": release(), "0.2.0": release()}),
            installed_version="0.1.0",
            host_platform=AMD64,
            controller_version=CONTROLLER,
            supported_protocol_versions=PROTOCOLS,
            applied_schema_revisions=SCHEMA,
        )

        assert BlockerCode.METADATA_UNAUTHENTICATED.value in codes_of(result)

    def test_a_host_already_on_the_newest_release_is_still_up_to_date(self) -> None:
        # Nothing to install, so nothing to authenticate. Reporting a host
        # that needs no update as "blocked" would be alarming and wrong.
        result = plan("0.2.0", {"0.1.0": release(), "0.2.0": release()}, metadata_authentic=False)

        assert result.outcome is Outcome.UP_TO_DATE
        assert codes_of(result) == ()
