"""Update policy and maintenance windows.

Two things are tested hardest here, because they are the two that fail quietly.

**The policy table.** "Automatically install patch releases" has to mean
exactly one set of steps, and a policy that admitted a minor release by
accident would upgrade a fleet across a boundary nobody authorised. The table
is asserted exhaustively — every policy against every step kind — rather than
by example.

**Midnight and time zones.** A window of 22:00 to 02:00 on Sunday is ordinary,
and the obvious implementation stops admitting work at midnight. A window in
`Europe/Berlin` evaluated against a UTC instant has to land in the right local
hour, including across a DST transition. Both are arithmetic, so both are
tested rather than reasoned about.
"""

from __future__ import annotations

import itertools
import zoneinfo
from datetime import UTC, datetime, time
from typing import Any

import pytest

from hcc.updates.compat import Blocker, BlockerCode, Outcome, UpdatePlan
from hcc.updates.policy import (
    ALWAYS,
    AUTOMATIC_STEPS,
    DEFAULT_POLICY,
    Action,
    MaintenanceWindow,
    Policy,
    WindowError,
    decide,
)
from hcc.updates.version import StepKind, parse

BERLIN = zoneinfo.ZoneInfo("Europe/Berlin")

# A Sunday, chosen so the weekday arithmetic is exercised rather than assumed.
SUNDAY_0300_UTC = datetime(2026, 10, 11, 3, 0, tzinfo=UTC)
assert SUNDAY_0300_UTC.isoweekday() == 7, "2026-10-11 must be a Sunday"


def available(step: str = StepKind.PATCH, target: str = "0.1.1") -> UpdatePlan:
    return UpdatePlan(
        outcome=Outcome.UPDATE_AVAILABLE,
        installed=parse("0.1.0"),
        target=parse(target),
        step=step,
        target_channel="stable",
    )


class TestAutomaticUpdatesAreOffByDefault:
    def test_the_default_policy_is_manual(self) -> None:
        # A value rather than a claim in a document.
        assert DEFAULT_POLICY is Policy.MANUAL
        assert not DEFAULT_POLICY.is_automatic

    def test_only_patch_and_minor_are_automatic(self) -> None:
        automatic = [policy for policy in Policy if policy.is_automatic]

        assert set(automatic) == {Policy.PATCH, Policy.MINOR}

    def test_a_manual_host_does_nothing(self) -> None:
        decision = decide(available(), policy=Policy.MANUAL, now=SUNDAY_0300_UTC)

        assert decision.action is Action.NOTHING
        assert "policy is manual" in decision.reason

    def test_there_is_no_major_policy(self) -> None:
        # The omission is the point: "automatically install breaking changes"
        # as a checkbox is a way to take a fleet down while asleep.
        assert "major" not in {policy.value for policy in Policy}
        for permitted in AUTOMATIC_STEPS.values():
            assert StepKind.MAJOR not in permitted


class TestThePolicyTable:
    @pytest.mark.parametrize(
        ("policy", "step"), list(itertools.product(list(Policy), StepKind.ALL))
    )
    def test_every_policy_against_every_step(self, policy: Policy, step: str) -> None:
        # Exhaustive rather than by example. A policy that admitted a minor
        # release by accident would upgrade a fleet across a boundary nobody
        # authorised, and that is not a bug an example-based test finds.
        plan = available(step=step)
        decision = decide(plan, policy=policy, now=SUNDAY_0300_UTC)

        should_install = step in AUTOMATIC_STEPS[policy]
        assert decision.action.starts_work is should_install, (
            f"{policy.value} + {step} produced {decision.action.value}: {decision.reason}"
        )

    def test_patch_takes_patch_and_prerelease_only(self) -> None:
        # Prerelease is a step between two versions with the same core triple,
        # so it is at most as large as a patch.
        assert AUTOMATIC_STEPS[Policy.PATCH] == frozenset({StepKind.PATCH, StepKind.PRERELEASE})

    def test_minor_also_takes_minor(self) -> None:
        assert StepKind.MINOR in AUTOMATIC_STEPS[Policy.MINOR]
        assert StepKind.PATCH in AUTOMATIC_STEPS[Policy.MINOR]

    def test_a_step_a_policy_will_not_take_is_surfaced_not_silent(self) -> None:
        # It must not be Action.NOTHING: an administrator whose host is two
        # minor versions behind on a patch-only policy needs to be told.
        decision = decide(
            available(step=StepKind.MINOR, target="0.2.0"),
            policy=Policy.PATCH,
            now=SUNDAY_0300_UTC,
        )

        assert decision.action is Action.NOTIFY
        assert "minor update" in decision.reason
        assert "by hand" in decision.reason

    def test_a_major_step_is_never_installed(self) -> None:
        for policy in Policy:
            decision = decide(
                available(step=StepKind.MAJOR, target="1.0.0"),
                policy=policy,
                now=SUNDAY_0300_UTC,
            )
            assert decision.action is not Action.INSTALL


class TestBlockersBeatPolicy:
    def test_an_incompatible_update_is_never_installed(self) -> None:
        # A policy expresses how large a step is acceptable, not permission to
        # ignore a blocker.
        plan = UpdatePlan(
            outcome=Outcome.UPDATE_AVAILABLE_INCOMPATIBLE,
            installed=parse("0.1.0"),
            target=parse("0.1.1"),
            step=StepKind.PATCH,
            blockers=(Blocker(code=BlockerCode.PROTOCOL_MISMATCH, detail="speaks protocol 99"),),
        )

        for policy in Policy:
            decision = decide(plan, policy=policy, now=SUNDAY_0300_UTC)
            assert decision.action is Action.NOTIFY
            assert "speaks protocol 99" in decision.reason

    def test_every_blocker_appears_in_the_reason(self) -> None:
        plan = UpdatePlan(
            outcome=Outcome.UPDATE_AVAILABLE_INCOMPATIBLE,
            target=parse("0.1.1"),
            blockers=(
                Blocker(code=BlockerCode.PROTOCOL_MISMATCH, detail="first problem"),
                Blocker(code=BlockerCode.SCHEMA_TOO_OLD, detail="second problem"),
            ),
        )

        decision = decide(plan, policy=Policy.PATCH, now=SUNDAY_0300_UTC)

        assert "first problem" in decision.reason
        assert "second problem" in decision.reason


class TestOutcomesWithNothingToInstall:
    def test_up_to_date_is_nothing(self) -> None:
        plan = UpdatePlan(outcome=Outcome.UP_TO_DATE, installed=parse("0.1.0"))

        decision = decide(plan, policy=Policy.PATCH, now=SUNDAY_0300_UTC)

        assert decision.action is Action.NOTHING

    @pytest.mark.parametrize("outcome", [Outcome.UNSUPPORTED, Outcome.UNKNOWN_VERSION])
    def test_a_host_needing_attention_is_notified_not_ignored(self, outcome: Outcome) -> None:
        # These two are not "no update" — they are "this host is in a state
        # somebody has to look at", and reporting them as nothing is how a
        # broken host disappears from a dashboard.
        plan = UpdatePlan(outcome=outcome, detail="this host needs looking at")

        decision = decide(plan, policy=Policy.MANUAL, now=SUNDAY_0300_UTC)

        assert decision.action is Action.NOTIFY
        assert decision.reason == "this host needs looking at"

    def test_metadata_unavailable_is_nothing_to_do(self) -> None:
        # Nothing the scheduler can do about it, and it is reported on the
        # controller's own panel rather than per host.
        plan = UpdatePlan(outcome=Outcome.METADATA_UNAVAILABLE)

        decision = decide(plan, policy=Policy.PATCH, now=SUNDAY_0300_UTC)

        assert decision.action is Action.NOTHING

    def test_notify_only_surfaces_an_available_update(self) -> None:
        decision = decide(available(), policy=Policy.NOTIFY, now=SUNDAY_0300_UTC)

        assert decision.action is Action.NOTIFY
        assert "notify only" in decision.reason


class TestMaintenanceWindows:
    def test_the_default_window_admits_everything(self) -> None:
        # A window is an extra restriction an administrator adds, not something
        # they have to configure before updates work at all.
        assert ALWAYS.is_always_open
        assert ALWAYS.admits(SUNDAY_0300_UTC)
        assert ALWAYS.describe() == "any time"

    def test_a_simple_window_admits_inside_and_refuses_outside(self) -> None:
        window = MaintenanceWindow(timezone="UTC", start=time(2, 0), end=time(4, 0))

        assert window.admits(datetime(2026, 10, 11, 2, 0, tzinfo=UTC))
        assert window.admits(datetime(2026, 10, 11, 3, 59, tzinfo=UTC))
        assert not window.admits(datetime(2026, 10, 11, 1, 59, tzinfo=UTC))
        # The end is exclusive, so two adjacent windows do not overlap.
        assert not window.admits(datetime(2026, 10, 11, 4, 0, tzinfo=UTC))

    def test_a_window_crossing_midnight_does_not_stop_at_midnight(self) -> None:
        # The obvious implementation gets this wrong, and "22:00 to 02:00" is
        # the most natural thing somebody would type.
        window = MaintenanceWindow(timezone="UTC", start=time(22, 0), end=time(2, 0))

        assert window.admits(datetime(2026, 10, 11, 22, 30, tzinfo=UTC))
        assert window.admits(datetime(2026, 10, 11, 23, 59, tzinfo=UTC))
        assert window.admits(datetime(2026, 10, 12, 0, 30, tzinfo=UTC))
        assert window.admits(datetime(2026, 10, 12, 1, 59, tzinfo=UTC))
        assert not window.admits(datetime(2026, 10, 12, 2, 0, tzinfo=UTC))
        assert not window.admits(datetime(2026, 10, 11, 21, 59, tzinfo=UTC))

    def test_a_midnight_crossing_window_is_matched_on_the_day_it_started(self) -> None:
        # A Sunday 22:00 to 02:00 window still admits Monday 00:30. Matching
        # the current day instead would make it stop at midnight, which is not
        # what anybody means by "Sunday night".
        sunday = MaintenanceWindow(timezone="UTC", weekdays=(0,), start=time(22, 0), end=time(2, 0))

        sunday_night = datetime(2026, 10, 11, 23, 0, tzinfo=UTC)
        monday_small_hours = datetime(2026, 10, 12, 0, 30, tzinfo=UTC)
        assert sunday_night.isoweekday() == 7
        assert monday_small_hours.isoweekday() == 1

        assert sunday.admits(sunday_night)
        assert sunday.admits(monday_small_hours)
        # And Monday *night* is not Sunday's window.
        assert not sunday.admits(datetime(2026, 10, 12, 23, 0, tzinfo=UTC))

    def test_weekdays_use_the_projects_convention(self) -> None:
        # 0 = Sunday, matching CalendarSchedule.days_of_week and cron, and not
        # Python's weekday() where 0 = Monday. Two numbering schemes in one
        # codebase is a defect waiting for a Sunday.
        sunday_only = MaintenanceWindow(timezone="UTC", weekdays=(0,))

        assert sunday_only.admits(datetime(2026, 10, 11, 12, 0, tzinfo=UTC))
        assert not sunday_only.admits(datetime(2026, 10, 12, 12, 0, tzinfo=UTC))

        saturday_only = MaintenanceWindow(timezone="UTC", weekdays=(6,))
        assert saturday_only.admits(datetime(2026, 10, 10, 12, 0, tzinfo=UTC))

    def test_equal_bounds_mean_all_day_not_nothing(self) -> None:
        # A zero-length interval admits nothing, and a window that admits
        # nothing is never configured on purpose — it is what an unset form
        # produces.
        all_day_sunday = MaintenanceWindow(timezone="UTC", weekdays=(0,))

        assert all_day_sunday.admits(datetime(2026, 10, 11, 0, 0, tzinfo=UTC))
        assert all_day_sunday.admits(datetime(2026, 10, 11, 23, 59, tzinfo=UTC))
        assert not all_day_sunday.admits(datetime(2026, 10, 12, 0, 0, tzinfo=UTC))
        assert not all_day_sunday.is_always_open  # the weekday still restricts

    def test_a_window_is_evaluated_in_its_own_zone(self) -> None:
        # 02:00 Berlin is 00:00 UTC in October (CEST, +02:00). An
        # implementation that compared UTC clock time would admit the wrong
        # hour entirely.
        window = MaintenanceWindow(timezone="Europe/Berlin", start=time(2, 0), end=time(4, 0))

        assert window.admits(datetime(2026, 10, 11, 0, 30, tzinfo=UTC))
        assert not window.admits(datetime(2026, 10, 11, 2, 30, tzinfo=UTC))
        # And the same instant expressed in Berlin local time agrees.
        assert window.admits(datetime(2026, 10, 11, 2, 30, tzinfo=BERLIN))

    def test_a_window_survives_a_dst_transition(self) -> None:
        # Europe/Berlin leaves summer time on 2026-10-25 at 03:00 local.
        # A 02:00-04:00 local window must still mean 02:00-04:00 local on both
        # sides of it, which is the whole reason the zone is stored rather than
        # an offset.
        window = MaintenanceWindow(timezone="Europe/Berlin", start=time(2, 0), end=time(4, 0))

        before = datetime(2026, 10, 18, 2, 30, tzinfo=BERLIN)
        after = datetime(2026, 11, 1, 2, 30, tzinfo=BERLIN)

        assert window.admits(before)
        assert window.admits(after)
        # The UTC offsets genuinely differ, so this is not a vacuous pair.
        assert before.utcoffset() != after.utcoffset()

    def test_a_naive_instant_is_refused(self) -> None:
        # The point of this type is that "02:00" means 02:00 somewhere
        # specific. Guessing the zone is how a window fires at the wrong time.
        window = MaintenanceWindow(timezone="UTC", start=time(2, 0), end=time(4, 0))

        with pytest.raises(WindowError, match="timezone-aware"):
            window.admits(datetime(2026, 10, 11, 3, 0))

    def test_an_unknown_zone_is_refused_at_construction(self) -> None:
        # At construction rather than at evaluation, so a bad stored value is
        # an error where it can be reported rather than at 02:00 on a Sunday.
        with pytest.raises(WindowError, match="not a known IANA time zone"):
            MaintenanceWindow(timezone="Mars/Olympus_Mons")

    @pytest.mark.parametrize("day", [-1, 7, 99])
    def test_an_out_of_range_weekday_is_refused(self, day: int) -> None:
        with pytest.raises(WindowError, match="0 = Sunday"):
            MaintenanceWindow(weekdays=(day,))

    def test_describe_always_names_the_zone(self) -> None:
        # A window rendered as "02:00-04:00" with the reader's own offset
        # assumed is the most reliable way to make somebody believe an update
        # ran at the wrong time.
        window = MaintenanceWindow(
            timezone="Europe/Berlin", weekdays=(0, 6), start=time(2, 0), end=time(4, 0)
        )

        described = window.describe()

        assert "Europe/Berlin" in described
        assert "02:00-04:00" in described
        assert "Sunday" in described
        assert "Saturday" in described

    def test_describe_for_an_all_day_window(self) -> None:
        assert "all day" in MaintenanceWindow(timezone="UTC", weekdays=(1,)).describe()


class TestWindowsAndPolicyTogether:
    def test_an_update_outside_the_window_is_deferred_not_failed(self) -> None:
        # Deferred is not a failure. Conflating them would make a healthy
        # fleet waiting for Sunday look broken.
        window = MaintenanceWindow(timezone="UTC", start=time(2, 0), end=time(4, 0))

        decision = decide(
            available(),
            policy=Policy.PATCH,
            window=window,
            now=datetime(2026, 10, 11, 12, 0, tzinfo=UTC),
        )

        assert decision.action is Action.DEFER
        assert "maintenance window is" in decision.reason
        assert "02:00-04:00 UTC" in decision.reason

    def test_an_update_inside_the_window_installs(self) -> None:
        window = MaintenanceWindow(timezone="UTC", start=time(2, 0), end=time(4, 0))

        decision = decide(available(), policy=Policy.PATCH, window=window, now=SUNDAY_0300_UTC)

        assert decision.action is Action.INSTALL
        assert "0.1.0 → 0.1.1" in decision.reason
        assert "patch update" in decision.reason

    def test_the_window_is_not_consulted_when_there_is_nothing_to_install(self) -> None:
        # A window that would refuse must not turn "nothing to do" into
        # "deferred", or every up-to-date host outside its window would look
        # like it is waiting for something.
        window = MaintenanceWindow(timezone="UTC", start=time(2, 0), end=time(2, 1))
        plan = UpdatePlan(outcome=Outcome.UP_TO_DATE, installed=parse("0.1.0"))

        decision = decide(
            plan,
            policy=Policy.PATCH,
            window=window,
            now=datetime(2026, 10, 11, 12, 0, tzinfo=UTC),
        )

        assert decision.action is Action.NOTHING

    def test_a_manual_host_is_not_deferred_either(self) -> None:
        # Manual means nothing is pending, so there is nothing for a window to
        # delay. Reporting DEFER would imply it will happen later.
        window = MaintenanceWindow(timezone="UTC", start=time(2, 0), end=time(2, 1))

        decision = decide(
            available(),
            policy=Policy.MANUAL,
            window=window,
            now=datetime(2026, 10, 11, 12, 0, tzinfo=UTC),
        )

        assert decision.action is Action.NOTHING


class TestEveryDecisionExplainsItself:
    @pytest.mark.parametrize("policy", list(Policy))
    def test_no_decision_has_an_empty_reason(self, policy: Policy) -> None:
        # "Why did my fleet not update" is the question this system exists to
        # answer. A decision that cannot explain itself makes the answer a
        # code-reading exercise.
        plans: list[Any] = [
            available(),
            UpdatePlan(outcome=Outcome.UP_TO_DATE, installed=parse("0.1.0")),
            UpdatePlan(outcome=Outcome.METADATA_UNAVAILABLE),
            UpdatePlan(outcome=Outcome.UNSUPPORTED, detail="too old"),
            UpdatePlan(
                outcome=Outcome.UPDATE_AVAILABLE_INCOMPATIBLE,
                target=parse("0.2.0"),
                blockers=(Blocker(code=BlockerCode.SCHEMA_TOO_OLD, detail="migrate"),),
            ),
        ]
        for plan in plans:
            decision = decide(plan, policy=policy, now=SUNDAY_0300_UTC)
            assert decision.reason.strip()
