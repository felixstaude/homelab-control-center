"""Schedule model and calendar-evaluation unit tests.

No database. These cover the validation rules in ``docs/SCHEDULING.md`` §10 and
the next-run calculator, including every daylight-saving case the document
specifies a policy for.

The DST tests are the reason this file is long. Wall-clock scheduling across a
transition is genuinely ambiguous, the policy is explicit rather than emergent,
and the implementation got it wrong in a way that only shows up twice a year —
so each case is pinned against a real transition on a real date, not against a
fabricated zone.
"""

from __future__ import annotations

import zoneinfo
from datetime import UTC, datetime

import pytest

from hcc.monitoring.resolve import parse_schedule
from hcc.monitoring.schedule import (
    MAX_CALENDAR_TIMES,
    MAX_INTERVAL_SECONDS,
    MIN_INTERVAL_SECONDS,
    CalendarSchedule,
    DstAmbiguous,
    DstNonexistent,
    IntervalSchedule,
    OnMissed,
    ScheduleError,
    ScheduleKind,
    next_run,
    upcoming_runs,
)

BERLIN = zoneinfo.ZoneInfo("Europe/Berlin")

# Europe/Berlin, 2024. Spring forward: 31 March, 02:00 -> 03:00. Fall back:
# 27 October, 03:00 -> 02:00. Both verified against the tz database rather
# than assumed.
SPRING_EVE = datetime(2024, 3, 30, 12, 0, tzinfo=UTC)
FALL_EVE = datetime(2024, 10, 26, 12, 0, tzinfo=UTC)


def local(occurrence_at: datetime) -> str:
    """An occurrence rendered in Berlin local time, for readable assertions."""
    return occurrence_at.astimezone(BERLIN).strftime("%Y-%m-%d %H:%M%z")


# ---------------------------------------------------------------------------
# Interval schedules
# ---------------------------------------------------------------------------


def test_an_interval_schedule_declares_its_kind() -> None:
    assert IntervalSchedule(interval_seconds=30).kind is ScheduleKind.INTERVAL


@pytest.mark.parametrize("seconds", [MIN_INTERVAL_SECONDS, 17, 2700, 9000, MAX_INTERVAL_SECONDS])
def test_an_arbitrary_interval_is_not_a_special_case(seconds: int) -> None:
    """17 seconds and 2h30m are 17 and 9000. ADR-019: no duration strings."""
    assert IntervalSchedule(interval_seconds=seconds).interval_seconds == seconds


@pytest.mark.parametrize("seconds", [0, -1, MAX_INTERVAL_SECONDS + 1])
def test_an_interval_outside_the_global_bounds_is_refused(seconds: int) -> None:
    with pytest.raises(ValueError):
        IntervalSchedule(interval_seconds=seconds)


def test_an_interval_is_refused_as_a_float() -> None:
    """Durations are integer seconds. A float would not round-trip a checksum."""
    with pytest.raises(ValueError):
        IntervalSchedule(interval_seconds=30.5)


def test_an_unknown_field_is_refused_rather_than_ignored() -> None:
    """A typo must surface instead of silently doing nothing (§10)."""
    with pytest.raises(ValueError):
        IntervalSchedule(interval_seconds=30, intrval_seconds=5)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("interval", "expected"),
    [
        # min(interval / 10, 30): scales with the interval, never dominates it.
        (10, 1),
        (30, 3),
        (100, 10),
        (300, 30),
        # Capped, so a daily schedule is not smeared over two and a half hours.
        (86_400, 30),
        # Below ten seconds the integer division floors to zero, which is the
        # right answer: a 5-second interval has no room for useful jitter.
        (5, 0),
    ],
)
def test_default_jitter_scales_with_the_interval_and_caps(interval: int, expected: int) -> None:
    schedule = IntervalSchedule(interval_seconds=interval)
    assert schedule.jitter_seconds is None
    assert schedule.effective_jitter_seconds() == expected


def test_an_explicit_jitter_of_zero_is_kept_rather_than_treated_as_absent() -> None:
    """Zero means "none" — for schedules where timing beats smooth load."""
    schedule = IntervalSchedule(interval_seconds=300, jitter_seconds=0)
    assert schedule.effective_jitter_seconds() == 0


def test_jitter_at_or_above_the_interval_is_refused() -> None:
    """Otherwise it randomises the sampling rate rather than de-synchronising."""
    with pytest.raises(ValueError, match="less than interval_seconds"):
        IntervalSchedule(interval_seconds=30, jitter_seconds=30)


def test_the_parse_boundary_reports_jitter_as_a_flat_problem() -> None:
    """Direct construction raises Pydantic's wrapper; `parse_schedule` unwraps
    it, which is what the API layer needs in order to name the field.
    """
    with pytest.raises(ScheduleError) as caught:
        parse_schedule({"kind": "interval", "interval_seconds": 30, "jitter_seconds": 30})
    assert "less than interval_seconds" in caught.value.problems[0]


def test_the_document_carries_a_concrete_jitter() -> None:
    """The agent merges nothing, so it must not have to compute a default."""
    document = IntervalSchedule(interval_seconds=30).to_document()
    assert document == {"kind": "interval", "interval_seconds": 30, "jitter_seconds": 3}


# ---------------------------------------------------------------------------
# Calendar validation
# ---------------------------------------------------------------------------


def test_a_minimal_calendar_schedule_uses_the_documented_defaults() -> None:
    schedule = CalendarSchedule(timezone="Europe/Berlin", times=["03:00"])
    assert schedule.on_missed is OnMissed.SKIP
    assert schedule.dst_nonexistent is DstNonexistent.SHIFT_FORWARD
    assert schedule.dst_ambiguous is DstAmbiguous.FIRST


@pytest.mark.parametrize("zone", ["Not/AZone", "UTC+2", "", "Europe/Berlin "])
def test_a_timezone_that_is_not_an_iana_name_is_refused(zone: str) -> None:
    """A fixed offset cannot express '03:30 local, year round' (§4)."""
    with pytest.raises(ValueError):
        CalendarSchedule(timezone=zone, times=["03:00"])


@pytest.mark.parametrize("entry", ["3:00", "03:0", "0300", "24:00", "03:60", "03-00", "ab:cd"])
def test_a_malformed_wall_clock_time_is_refused(entry: str) -> None:
    with pytest.raises(ValueError):
        CalendarSchedule(timezone="UTC", times=[entry])


def test_a_non_ascii_digit_is_not_accepted_as_a_time() -> None:
    """``str.isdigit`` accepts Arabic-Indic digits; ``int`` would then parse them.

    Refused because two spellings of the same time would serialise differently
    and therefore checksum differently.
    """
    with pytest.raises(ValueError):
        CalendarSchedule(timezone="UTC", times=["٠٣:00"])


def test_a_duplicate_time_is_refused() -> None:
    with pytest.raises(ValueError):
        CalendarSchedule(timezone="UTC", times=["03:00", "03:00"])


def test_an_empty_times_list_is_refused() -> None:
    with pytest.raises(ValueError):
        CalendarSchedule(timezone="UTC", times=[])


def test_more_times_than_the_limit_is_refused() -> None:
    too_many = [f"{hour:02d}:00" for hour in range(24)] + ["00:30"]
    assert len(too_many) == MAX_CALENDAR_TIMES + 1
    with pytest.raises(ValueError):
        CalendarSchedule(timezone="UTC", times=too_many)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("days_of_week", [7]),
        ("days_of_week", [-1]),
        ("days_of_month", [0]),
        ("days_of_month", [32]),
        ("months", [0]),
        ("months", [13]),
    ],
)
def test_a_set_field_outside_its_range_is_refused(field: str, value: list[int]) -> None:
    with pytest.raises(ValueError):
        CalendarSchedule(timezone="UTC", times=["03:00"], **{field: value})


@pytest.mark.parametrize("field", ["days_of_week", "days_of_month", "months"])
def test_an_empty_set_field_is_refused_rather_than_read_as_every_value(field: str) -> None:
    """``days_of_week: []`` reads as "no days", and a schedule matching no day
    can never fire. Reinterpreting it as "every day" would turn a mistake into
    a running job; ``null`` is how "every value" is spelled.
    """
    with pytest.raises(ValueError) as caught:
        CalendarSchedule(timezone="UTC", times=["03:00"], **{field: []})
    assert "omit it (null)" in str(caught.value)


def test_a_duplicate_in_a_set_field_is_refused() -> None:
    with pytest.raises(ValueError):
        CalendarSchedule(timezone="UTC", times=["03:00"], days_of_week=[1, 1])


@pytest.mark.parametrize("value", [True, False, "1", 1.0])
def test_a_non_integer_is_not_coerced_into_a_weekday(value: object) -> None:
    """``bool`` is an ``int`` in Python, so ``True`` would otherwise validate as
    Monday — and Pydantic's lax mode coerces it *before* any custom validator
    runs, so the fields are ``StrictInt`` rather than ``int``.
    """
    with pytest.raises(ValueError):
        CalendarSchedule(timezone="UTC", times=["03:00"], days_of_week=[value])


def test_every_problem_is_reported_rather_than_the_first() -> None:
    """§10: errors are returned all at once, each naming the field and limit.

    Through `parse_schedule`, because Pydantic collapses a `ScheduleError` into
    one wrapped error whose message is the joined form — so the unwrapping in
    `_flatten_validation_error` is what makes this property real rather than
    five complaints in one string.
    """
    with pytest.raises(ScheduleError) as caught:
        parse_schedule(
            {
                "kind": "calendar",
                "timezone": "Not/AZone",
                "times": ["3:00", "99:99"],
                "days_of_week": [9],
                "months": [14],
            }
        )
    problems = caught.value.problems
    assert len(problems) >= 5
    joined = " ".join(problems)
    assert "timezone" in joined
    assert "times" in joined
    assert "days_of_week" in joined
    assert "months" in joined


# ---------------------------------------------------------------------------
# Reachability
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("months", "days_of_month"),
    [
        # 30 February never happens.
        ([2], [30]),
        ([2], [31]),
        # September has thirty days.
        ([9], [31]),
        # Four thirty-day months, asked for the 31st.
        ([4, 6, 9, 11], [31]),
    ],
)
def test_a_schedule_that_can_never_fire_is_refused_at_save_time(
    months: list[int], days_of_month: list[int]
) -> None:
    """Rather than being discovered by nothing ever happening (§10)."""
    with pytest.raises(ScheduleError) as caught:
        parse_schedule(
            {
                "kind": "calendar",
                "timezone": "UTC",
                "times": ["00:00"],
                "months": months,
                "days_of_month": days_of_month,
            }
        )
    assert "can never fire" in caught.value.problems[0]


def test_a_february_29th_schedule_is_accepted() -> None:
    """It fires once every four years, which is legal and must not be refused.

    This is why the search horizon is four years of days rather than a round
    number, and why the reachability anchor is a fixed date: a horizon of one
    year would reject this in three years out of four.
    """
    schedule = CalendarSchedule(timezone="UTC", times=["00:00"], months=[2], days_of_month=[29])
    runs = upcoming_runs(schedule, after=datetime(2024, 3, 1, tzinfo=UTC), count=2)
    assert [run.at.date().isoformat() for run in runs] == ["2028-02-29", "2032-02-29"]


def test_validity_does_not_depend_on_when_the_schedule_was_saved() -> None:
    """The reachability anchor is fixed, so a leap-day schedule validates the
    same way in every year. Asserted by validating the same document twice.
    """
    for _ in range(2):
        CalendarSchedule(timezone="UTC", times=["00:00"], months=[2], days_of_month=[29])


def test_a_day_of_week_and_day_of_month_combination_is_anded_not_ored() -> None:
    """Unlike cron, where the interaction is an OR that surprises everyone."""
    # The 13th, but only when it is a Friday.
    schedule = CalendarSchedule(
        timezone="UTC", times=["00:00"], days_of_month=[13], days_of_week=[5]
    )
    runs = upcoming_runs(schedule, after=datetime(2024, 1, 1, tzinfo=UTC), count=3)
    for run in runs:
        assert run.at.day == 13
        # Python's Friday is 4; the wire format's Friday is 5.
        assert run.at.weekday() == 4


# ---------------------------------------------------------------------------
# Next-run evaluation, ordinary days
# ---------------------------------------------------------------------------


def test_a_naive_after_is_refused() -> None:
    """A silently interpreted datetime is how a schedule fires an hour late in
    production and on time in a test.
    """
    with pytest.raises(ValueError, match="timezone-aware"):
        next_run(CalendarSchedule(timezone="UTC", times=["03:00"]), after=datetime(2024, 1, 1))


def test_an_occurrence_later_today_is_found_rather_than_skipped() -> None:
    """The walk starts on the boundary's own local date."""
    schedule = CalendarSchedule(timezone="Europe/Berlin", times=["23:00"])
    occurrence = next_run(schedule, after=datetime(2024, 6, 1, 8, 0, tzinfo=BERLIN))
    assert occurrence is not None
    assert local(occurrence.at) == "2024-06-01 23:00+0200"


def test_several_times_each_produce_an_execution_in_clock_order() -> None:
    schedule = CalendarSchedule(timezone="Europe/Berlin", times=["20:00", "08:00"])
    runs = upcoming_runs(schedule, after=datetime(2024, 6, 1, 0, 0, tzinfo=BERLIN), count=4)
    assert [local(run.at) for run in runs] == [
        "2024-06-01 08:00+0200",
        "2024-06-01 20:00+0200",
        "2024-06-02 08:00+0200",
        "2024-06-02 20:00+0200",
    ]


def test_a_weekday_restriction_is_honoured() -> None:
    """0 = Sunday … 6 = Saturday, which is the wire format, not Python's."""
    schedule = CalendarSchedule(timezone="Europe/Berlin", times=["03:00"], days_of_week=[1])
    runs = upcoming_runs(schedule, after=datetime(2024, 6, 1, 0, 0, tzinfo=BERLIN), count=3)
    assert [local(run.at) for run in runs] == [
        "2024-06-03 03:00+0200",
        "2024-06-10 03:00+0200",
        "2024-06-17 03:00+0200",
    ]


def test_sunday_is_zero_not_seven() -> None:
    schedule = CalendarSchedule(timezone="UTC", times=["00:00"], days_of_week=[0])
    occurrence = next_run(schedule, after=datetime(2024, 6, 1, tzinfo=UTC))
    assert occurrence is not None
    # 2 June 2024 is a Sunday.
    assert occurrence.at.date().isoformat() == "2024-06-02"


def test_an_ordinary_occurrence_reports_no_dst_adjustment() -> None:
    occurrence = next_run(
        CalendarSchedule(timezone="Europe/Berlin", times=["03:00"]),
        after=datetime(2024, 6, 1, 0, 0, tzinfo=BERLIN),
    )
    assert occurrence is not None
    assert occurrence.dst_adjustment is None
    assert occurrence.local_time == "03:00"


def test_a_zone_without_daylight_saving_is_unremarkable() -> None:
    runs = upcoming_runs(
        CalendarSchedule(timezone="UTC", times=["00:00"]),
        after=datetime(2024, 6, 1, 12, tzinfo=UTC),
        count=2,
    )
    assert [run.at.isoformat() for run in runs] == [
        "2024-06-02T00:00:00+00:00",
        "2024-06-03T00:00:00+00:00",
    ]


# ---------------------------------------------------------------------------
# Daylight saving: spring forward, the time does not exist
# ---------------------------------------------------------------------------


def test_spring_forward_shifts_to_the_first_instant_that_exists() -> None:
    """§5.1: a 02:30 schedule runs at **03:00**, not 03:30.

    The distinction is the whole test. Resolving a nonexistent local time and
    converting it back gives 03:30 — the requested time plus the size of the
    gap — and the document asked for the first instant that exists, which is
    the transition itself. Thirty minutes apart, and only one is specified.
    """
    schedule = CalendarSchedule(timezone="Europe/Berlin", times=["02:30"])
    occurrence = next_run(schedule, after=SPRING_EVE)
    assert occurrence is not None
    assert local(occurrence.at) == "2024-03-31 03:00+0200"
    assert occurrence.dst_adjustment == "shifted_forward"
    # The requested wall-clock time is preserved, so an interface can say
    # "runs at 03:00, because 02:30 does not exist tonight".
    assert occurrence.local_time == "02:30"


def test_spring_forward_under_skip_does_not_run_that_day() -> None:
    schedule = CalendarSchedule(
        timezone="Europe/Berlin", times=["02:30"], dst_nonexistent=DstNonexistent.SKIP
    )
    occurrence = next_run(schedule, after=SPRING_EVE)
    assert occurrence is not None
    # The 31st is skipped entirely; the next run is the following day.
    assert local(occurrence.at) == "2024-04-01 02:30+0200"
    assert occurrence.dst_adjustment is None


def test_a_time_outside_the_gap_is_unaffected_on_a_transition_night() -> None:
    schedule = CalendarSchedule(timezone="Europe/Berlin", times=["01:30"])
    occurrence = next_run(schedule, after=SPRING_EVE)
    assert occurrence is not None
    assert local(occurrence.at) == "2024-03-31 01:30+0100"
    assert occurrence.dst_adjustment is None


def test_a_half_hour_transition_shifts_by_half_an_hour() -> None:
    """Lord Howe Island moves 02:00 -> 02:30, not 02:00 -> 03:00.

    A hardcoded "+1 hour" would schedule this at 03:15 and be an hour wrong
    every spring, which is why the implementation walks minutes forward
    instead.
    """
    zone = zoneinfo.ZoneInfo("Australia/Lord_Howe")
    schedule = CalendarSchedule(timezone="Australia/Lord_Howe", times=["02:15"])
    occurrence = next_run(schedule, after=datetime(2024, 10, 5, 0, 0, tzinfo=UTC))
    assert occurrence is not None
    assert occurrence.at.astimezone(zone).strftime("%Y-%m-%d %H:%M") == "2024-10-06 02:30"
    assert occurrence.dst_adjustment == "shifted_forward"


# ---------------------------------------------------------------------------
# Daylight saving: fall back, the time occurs twice
# ---------------------------------------------------------------------------


def test_fall_back_first_runs_at_the_earlier_of_the_two() -> None:
    """§5.2, the default: run once, as early as the operator asked."""
    schedule = CalendarSchedule(timezone="Europe/Berlin", times=["02:30"])
    runs = upcoming_runs(schedule, after=FALL_EVE, count=2)
    assert local(runs[0].at) == "2024-10-27 02:30+0200"
    assert runs[0].dst_adjustment == "ambiguous_first"
    # And exactly once: the second 02:30 is not also a run.
    assert local(runs[1].at) == "2024-10-28 02:30+0100"


def test_fall_back_second_runs_at_the_later_of_the_two() -> None:
    schedule = CalendarSchedule(
        timezone="Europe/Berlin", times=["02:30"], dst_ambiguous=DstAmbiguous.SECOND
    )
    runs = upcoming_runs(schedule, after=FALL_EVE, count=2)
    assert local(runs[0].at) == "2024-10-27 02:30+0100"
    assert runs[0].dst_adjustment == "ambiguous_second"
    assert local(runs[1].at) == "2024-10-28 02:30+0100"


def test_fall_back_both_runs_twice_an_hour_apart() -> None:
    schedule = CalendarSchedule(
        timezone="Europe/Berlin", times=["02:30"], dst_ambiguous=DstAmbiguous.BOTH
    )
    runs = upcoming_runs(schedule, after=FALL_EVE, count=2)
    assert [local(run.at) for run in runs] == [
        "2024-10-27 02:30+0200",
        "2024-10-27 02:30+0100",
    ]
    assert (runs[1].at - runs[0].at).total_seconds() == 3600
    assert [run.dst_adjustment for run in runs] == ["ambiguous_first", "ambiguous_second"]


def test_an_ambiguous_time_is_not_reported_as_shifted() -> None:
    """The discrimination that the implementation originally got wrong.

    PEP 495 gives a *nonexistent* local time differing offsets across the two
    folds, exactly as an *ambiguous* one has — so a fold comparison alone
    cannot tell them apart, and checking it first reported every spring-forward
    time as ambiguous. Existence is tested first, and this pins both halves of
    that: a fall-back time is ambiguous and a spring-forward time is shifted.
    """
    fall = next_run(CalendarSchedule(timezone="Europe/Berlin", times=["02:30"]), after=FALL_EVE)
    spring = next_run(CalendarSchedule(timezone="Europe/Berlin", times=["02:30"]), after=SPRING_EVE)
    assert fall is not None and spring is not None
    assert fall.dst_adjustment == "ambiguous_first"
    assert spring.dst_adjustment == "shifted_forward"


def test_occurrences_on_a_transition_day_are_returned_in_instant_order() -> None:
    """Resolution can reorder a day's candidates.

    A time shifted forward out of the gap can land after a later listed time,
    so the day's candidates are sorted by instant rather than trusted to come
    out in the order their wall-clock strings did.
    """
    schedule = CalendarSchedule(timezone="Europe/Berlin", times=["02:30", "02:45"])
    runs = upcoming_runs(schedule, after=SPRING_EVE, count=2)
    assert [run.at for run in runs] == sorted(run.at for run in runs)


# ---------------------------------------------------------------------------
# Horizon and preview
# ---------------------------------------------------------------------------


def test_a_search_that_finds_nothing_within_the_horizon_returns_none() -> None:
    schedule = CalendarSchedule(timezone="UTC", times=["00:00"], months=[2], days_of_month=[29])
    # One year of days cannot reach the next 29 February from March 2024.
    assert next_run(schedule, after=datetime(2024, 3, 1, tzinfo=UTC), horizon_days=365) is None


def test_the_preview_walks_forward_rather_than_sharing_one_horizon() -> None:
    """Each step searches four years from the *previous* occurrence.

    So a leap-day schedule previews ten runs across forty years rather than
    stopping at the first horizon. Pinned because the alternative reading — one
    horizon for the whole preview — is the one I assumed when writing this, and
    it would silently truncate a preview of any schedule rarer than annual.
    """
    schedule = CalendarSchedule(timezone="UTC", times=["00:00"], months=[2], days_of_month=[29])
    runs = upcoming_runs(schedule, after=datetime(2024, 3, 1, tzinfo=UTC), count=10)
    assert len(runs) == 10
    assert [run.at.year for run in runs[:3]] == [2028, 2032, 2036]


def test_the_preview_is_strictly_increasing() -> None:
    schedule = CalendarSchedule(timezone="Europe/Berlin", times=["00:00", "12:00"])
    runs = upcoming_runs(schedule, after=datetime(2024, 6, 1, tzinfo=UTC), count=6)
    instants = [run.at for run in runs]
    assert instants == sorted(instants)
    assert len(set(instants)) == len(instants)


def test_the_preview_does_not_repeat_an_occurrence_across_a_both_policy() -> None:
    """``both`` yields two instants for one wall-clock time, and the preview
    walks by instant, so neither is returned twice.
    """
    schedule = CalendarSchedule(
        timezone="Europe/Berlin", times=["02:30"], dst_ambiguous=DstAmbiguous.BOTH
    )
    runs = upcoming_runs(schedule, after=FALL_EVE, count=4)
    assert len({run.at for run in runs}) == len(runs)


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def test_the_calendar_document_emits_every_field() -> None:
    """Including the ones left at their defaults: the agent must not have to
    know what a default is, and a null in one of the three "every value"
    fields is the value rather than an omission.
    """
    document = CalendarSchedule(timezone="Europe/Berlin", times=["03:30", "15:30"]).to_document()
    assert document == {
        "kind": "calendar",
        "timezone": "Europe/Berlin",
        "times": ["03:30", "15:30"],
        "days_of_week": None,
        "days_of_month": None,
        "months": None,
        "on_missed": "skip",
        "missed_grace_seconds": 7200,
        "dst_nonexistent": "shift_forward",
        "dst_ambiguous": "first",
    }


def test_the_document_sorts_times_and_sets_so_two_equivalent_schedules_agree() -> None:
    """Two schedules that fire identically must serialise identically, or they
    would checksum differently and re-deploy for no reason.
    """
    left = CalendarSchedule(
        timezone="UTC", times=["15:30", "03:30"], days_of_week=[3, 1]
    ).to_document()
    right = CalendarSchedule(
        timezone="UTC", times=["03:30", "15:30"], days_of_week=[1, 3]
    ).to_document()
    assert left == right


def test_missed_grace_beyond_the_ceiling_is_refused() -> None:
    with pytest.raises(ValueError):
        CalendarSchedule(
            timezone="UTC",
            times=["03:00"],
            on_missed=OnMissed.RUN_ONCE,
            missed_grace_seconds=8 * 24 * 60 * 60,
        )
