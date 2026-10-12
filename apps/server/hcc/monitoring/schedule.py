"""When a module runs. One model, shared by every module and periodic job.

Phase 2, implementing ``docs/SCHEDULING.md``. The shapes here are the wire
format, the database format and the validation surface at once, deliberately:
three representations of a schedule would be three places for the floor to be
enforced differently.

## Durations are integer seconds, everywhere

No ``"5m"``, no ``"2h30m"``, no float (ADR-019). Enforcing "at least five
seconds" is then an integer comparison rather than a parse followed by a
comparison against an ambiguous unit, and 17 seconds is not a special case —
it is 17.

The trap this closes is specific and was worth naming in the mission brief:
Go's ``time.Duration`` is nanoseconds, so a ``Duration`` serialised with
``encoding/json`` emits ``30000000000`` for thirty seconds. Every field here is
named ``*_seconds`` and typed ``int``, and the agent converts at the boundary
rather than carrying a ``Duration`` on a wire struct.

## Calendar schedules are structured, not cron

Field-by-field validatable, no day-of-month/day-of-week OR surprise, and a
place for the daylight-saving policy to live (ADR-020). The cost is that an
operator who knows cron cannot paste an expression; the audience is people
filling in a web form for a handful of schedules.

## The next-run calculator exists twice, on purpose

The agent evaluates its own calendar schedules, because it must keep working
while the controller is unreachable (``docs/AGENT_ARCHITECTURE.md`` §8). So the
authoritative evaluator is the Go one. :func:`next_run` here is the
controller's copy, and it has two jobs the agent's does not:

1. **Reachability.** "Can this schedule ever fire?" is answered by trying to
   find a run, which is how 30 February is refused at save time rather than
   discovered by nothing ever happening.
2. **Preview.** The interface shows the next three runs when an operator picks
   a schedule, because a calendar expression nobody can evaluate by eye is one
   they will get wrong.

Two implementations of one rule is a drift risk, and it is managed rather than
accepted: ``contracts/calendar-cases.json`` holds the cases, both test suites
read it, and ``scripts/contracts.sh --check`` fails if the committed file does
not match what Python generates.
"""

from __future__ import annotations

import enum
import zoneinfo
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal, Self

from pydantic import BaseModel, Field, StrictInt, model_validator

#: The global interval floor. Below one second the cost of collecting dominates
#: the measurement, and no kernel interface this project reads changes faster.
MIN_INTERVAL_SECONDS: Final = 1

#: The global interval ceiling: thirty days. Beyond this it is not monitoring,
#: and an operator who wants a monthly job wants a calendar schedule.
MAX_INTERVAL_SECONDS: Final = 30 * 24 * 60 * 60

#: Longest a missed calendar run may be deferred on recovery. Seven days, so a
#: weekly task that was missed can still run before its next occurrence.
MAX_MISSED_GRACE_SECONDS: Final = 7 * 24 * 60 * 60

#: Most wall-clock times one calendar schedule may list. Twenty-four is enough
#: for hourly-on-the-hour without a second mechanism, and bounds the work
#: :func:`next_run` does per day.
MAX_CALENDAR_TIMES: Final = 24

#: How far :func:`next_run` searches before declaring a schedule unreachable.
#:
#: Four years, in days. Not an arbitrary round number: a schedule restricted to
#: 29 February fires once every four years, and the search must find it rather
#: than report a legal schedule as impossible. February 29th is also the reason
#: this is a day-by-day walk and not arithmetic.
SEARCH_HORIZON_DAYS: Final = 366 * 4


class ScheduleKind(enum.StrEnum):
    """The four kinds of schedule, covering everything the product needs.

    Not every module accepts every kind. The model is shared; the permitted
    subset is a property of the module
    (:attr:`hcc.monitoring.modules.ModuleDescriptor.schedule_kinds`), and both
    the controller and the agent enforce it.
    """

    #: Every N seconds, continuously. Sampling.
    INTERVAL = "interval"
    #: At wall-clock times on chosen days, in a named time zone. Tasks.
    CALENDAR = "calendar"
    #: Driven by an external event stream rather than a clock. Reserved: no
    #: module in this phase uses it, and it is present so that a module
    #: descriptor can declare it without the enum moving later.
    EVENT = "event"
    #: Only when a user asks.
    MANUAL = "manual"


class OnMissed(enum.StrEnum):
    """What to do about a calendar run that was missed.

    Only meaningful for calendar schedules. An interval schedule never
    backfills — a missed sample is a gap, and the gap is the truth
    (``docs/SCHEDULING.md`` §6.1).
    """

    #: Do not run. Record that it was missed. The default, because the safe
    #: assumption for an unknown task is "do not start work the operator is no
    #: longer expecting".
    SKIP = "skip"
    #: Run once on recovery, if still within the grace window. **Never N times
    #: for N missed occurrences** — an agent offline for a fortnight with a
    #: daily task does not come back and run it fourteen times.
    RUN_ONCE = "run_once"


class DstNonexistent(enum.StrEnum):
    """Policy for a local time that does not exist on a spring-forward night."""

    #: Run at the first instant that does exist. The default: a nightly task
    #: that silently does not happen once a year is worse than one that happens
    #: half an hour late.
    SHIFT_FORWARD = "shift_forward"
    #: Do not run that day. Recorded as skipped, with the reason.
    SKIP = "skip"


class DstAmbiguous(enum.StrEnum):
    """Policy for a local time that occurs twice on a fall-back night."""

    #: The first occurrence, still in summer time. The default: run once, as
    #: early as the operator asked.
    FIRST = "first"
    #: The second occurrence, already in standard time.
    SECOND = "second"
    #: Both. Permitted only for modules that declare a duplicate run harmless —
    #: see :attr:`hcc.monitoring.modules.ModuleDescriptor.allows_duplicate_runs`.
    BOTH = "both"


class ScheduleError(ValueError):
    """A schedule that is refused, with every problem rather than the first.

    ``ValueError`` so a caller that does not care about the distinction still
    catches it, and a distinct type so the API layer maps it to a 422 rather
    than to a 500.

    :attr:`problems` is a list because of a specific product rule: errors are
    returned all at once, each naming the field and the limit
    (``docs/SCHEDULING.md`` §10). An operator fixing a unit file or a form
    should need one pass, not one pass per mistake.

    ## Raised by the validators, but not what a caller catches

    Constructing a model directly — ``CalendarSchedule(...)`` — raises
    Pydantic's ``ValidationError``, not this, because Pydantic catches every
    ``ValueError`` a validator raises and wraps it. Both are ``ValueError``
    subclasses, so a caller that only cares about "invalid" is unaffected.

    A caller that wants the flat problem list must go through
    :func:`hcc.monitoring.resolve.parse_schedule`, which is the boundary that
    unwraps the wrapping and re-raises this type. That is the function every
    request path and every stored-document read already uses; direct
    construction is for tests and for code that has already validated.
    """

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


class IntervalSchedule(BaseModel):
    """Run, then run again ``interval_seconds`` later, forever.

    Measured on a **monotonic** clock by the agent, so an interval is immune to
    daylight saving, an NTP step and a manual clock change. Three hundred
    seconds is three hundred seconds of elapsed time, never "until the wall
    clock reads a multiple of five minutes" — an operator who wants wall-clock
    alignment wants a calendar schedule, and the interface says so.
    """

    model_config = {"extra": "forbid"}

    kind: Literal[ScheduleKind.INTERVAL] = ScheduleKind.INTERVAL

    interval_seconds: int = Field(
        ge=MIN_INTERVAL_SECONDS,
        le=MAX_INTERVAL_SECONDS,
        description="Seconds between executions. Integer seconds, never a duration string.",
    )

    jitter_seconds: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Random offset in [0, jitter_seconds) applied to each execution. "
            "Null means use the module default, which scales with the "
            "interval. Zero means none — for schedules where exact timing "
            "matters more than smooth fleet load."
        ),
    )

    @model_validator(mode="after")
    def _jitter_fits_inside_the_interval(self) -> Self:
        """Reject jitter that could exceed the interval it perturbs.

        Jitter at or above the interval does not de-synchronise a fleet, it
        randomises the sampling rate — and a 30-second series whose samples
        arrive 5 to 55 seconds apart is one nobody can read a rate off.
        """
        if self.jitter_seconds is not None and self.jitter_seconds >= self.interval_seconds:
            raise ScheduleError(
                [
                    f"jitter_seconds must be less than interval_seconds "
                    f"({self.jitter_seconds} is not less than {self.interval_seconds})"
                ]
            )
        return self

    def effective_jitter_seconds(self) -> int:
        """Jitter to apply, resolving ``None`` to the scaled default.

        ``min(interval / 10, 30)``: it scales with the interval so it never
        dominates it, and it caps so that a daily schedule is not smeared over
        two and a half hours.

        Jitter is not cosmetic. Twenty agents on a 30-second interval, all
        enrolled the same afternoon, otherwise align and deliver twenty
        simultaneous batches every thirty seconds — turning smooth load into
        spikes and making rate limits fire on honest traffic.
        """
        if self.jitter_seconds is not None:
            return self.jitter_seconds
        return min(self.interval_seconds // 10, 30)

    def to_document(self) -> dict[str, Any]:
        """The agent's view of this schedule: every value concrete.

        ``jitter_seconds`` is **resolved**, never omitted. The agent merges
        nothing (``AGENT_ARCHITECTURE.md`` §6.1), so leaving it out would
        require the agent to compute the same scaled default the controller
        just computed — two implementations of one rule, in two languages,
        differing silently the first time one of them changes.
        """
        return {
            "kind": self.kind.value,
            "interval_seconds": self.interval_seconds,
            "jitter_seconds": self.effective_jitter_seconds(),
        }


class CalendarSchedule(BaseModel):
    """Run at each listed wall-clock time, on each matching day, in one zone.

    ``days_of_week`` and ``days_of_month`` are **ANDed** when both are set,
    unlike cron — where their interaction is an OR that surprises nearly
    everyone and silently runs jobs on unintended days. Both null means every
    day.
    """

    model_config = {"extra": "forbid"}

    kind: Literal[ScheduleKind.CALENDAR] = ScheduleKind.CALENDAR

    timezone: str = Field(
        min_length=1,
        max_length=64,
        description=(
            "IANA zone name, e.g. 'Europe/Berlin'. Never a fixed offset: "
            "'UTC+2' cannot express '03:30 local time, year round', which is "
            "what an operator scheduling a nightly task means."
        ),
    )

    times: list[str] = Field(
        min_length=1,
        max_length=MAX_CALENDAR_TIMES,
        description="Local wall-clock times as 'HH:MM'. Each produces an execution.",
    )

    days_of_week: list[StrictInt] | None = Field(
        default=None,
        description="0 = Sunday … 6 = Saturday. Null means every day of the week.",
    )

    days_of_month: list[StrictInt] | None = Field(
        default=None,
        description="1-31. Null means every day of the month.",
    )

    months: list[StrictInt] | None = Field(
        default=None,
        description="1-12. Null means every month.",
    )

    on_missed: OnMissed = Field(
        default=OnMissed.SKIP,
        description="What to do about an occurrence missed while the agent was down.",
    )

    missed_grace_seconds: int = Field(
        default=2 * 60 * 60,
        ge=0,
        le=MAX_MISSED_GRACE_SECONDS,
        description="How late a missed run may still be executed under 'run_once'.",
    )

    dst_nonexistent: DstNonexistent = Field(
        default=DstNonexistent.SHIFT_FORWARD,
        description="Policy for a local time that does not exist on a spring-forward night.",
    )

    dst_ambiguous: DstAmbiguous = Field(
        default=DstAmbiguous.FIRST,
        description="Policy for a local time that occurs twice on a fall-back night.",
    )

    @model_validator(mode="after")
    def _validate_fields_together(self) -> Self:
        """Check every field, collecting all problems rather than raising on the first.

        Structural per-field bounds that Pydantic cannot express — a known time
        zone, ``HH:MM`` shape, set membership, no duplicates — plus the one
        cross-field property that matters: that the schedule can fire at all.
        """
        problems: list[str] = []

        try:
            zoneinfo.ZoneInfo(self.timezone)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError, KeyError):
            # The name is not echoed in full: it is operator input that ends up
            # in an error body, and a 64-character string reflected verbatim is
            # a small reflection surface for no benefit. The length bound above
            # already keeps it short, so naming it is safe — but naming it
            # *quoted* is what makes a trailing space visible.
            problems.append(f"timezone {self.timezone!r} is not a known IANA zone name")

        seen_times: set[str] = set()
        for entry in self.times:
            if not _is_wall_clock_time(entry):
                problems.append(f"times entry {entry!r} is not a valid 'HH:MM' local time")
            elif entry in seen_times:
                problems.append(f"times contains {entry!r} more than once")
            else:
                seen_times.add(entry)

        problems.extend(_validate_int_set("days_of_week", self.days_of_week, 0, 6))
        problems.extend(_validate_int_set("days_of_month", self.days_of_month, 1, 31))
        problems.extend(_validate_int_set("months", self.months, 1, 12))

        if problems:
            raise ScheduleError(problems)

        # Reachability last, because it needs every field above to be valid in
        # order to mean anything. A schedule that can never fire — 30 February,
        # or the 31st of a September-only month set — is refused here rather
        # than discovered by nothing ever happening.
        #
        # The anchor is a fixed date rather than `now`: validity must not
        # depend on when the schedule was saved, or a February-29th schedule
        # would be accepted in some years and refused in others.
        if next_run(self, after=datetime(2024, 1, 1, tzinfo=zoneinfo.ZoneInfo("UTC"))) is None:
            raise ScheduleError(
                [
                    "this schedule can never fire: no date in the next four years "
                    "matches the configured months, days of month and days of week"
                ]
            )
        return self

    def to_document(self) -> dict[str, Any]:
        """The agent's view of this schedule: every value concrete.

        Every field is emitted, including the ones left at their defaults, for
        the reason :meth:`IntervalSchedule.to_document` gives — the agent must
        not have to know what a default is. ``None`` is kept for the three
        "every value" fields, because there it is the *value*: null months
        means every month, and omitting the key would make it indistinguishable
        from a controller too old to send it.
        """
        return {
            "kind": self.kind.value,
            "timezone": self.timezone,
            "times": sorted(self.times),
            "days_of_week": sorted(self.days_of_week) if self.days_of_week else None,
            "days_of_month": sorted(self.days_of_month) if self.days_of_month else None,
            "months": sorted(self.months) if self.months else None,
            "on_missed": self.on_missed.value,
            "missed_grace_seconds": self.missed_grace_seconds,
            "dst_nonexistent": self.dst_nonexistent.value,
            "dst_ambiguous": self.dst_ambiguous.value,
        }


#: Either kind a module may be configured with in this phase.
#:
#: ``event`` and ``manual`` have no payload — they are a kind and nothing else —
#: so they are represented by their :class:`ScheduleKind` rather than by a model
#: with one field. No module in this phase declares them, and
#: :func:`parse_schedule` refuses them, so there is no half-built path for a
#: later module to inherit.
Schedule = IntervalSchedule | CalendarSchedule


def _is_wall_clock_time(raw: object) -> bool:
    """Whether ``raw`` is ``HH:MM`` with a real hour and minute.

    Deliberately strict about shape as well as range: ``"3:00"`` and ``"03:0"``
    are refused, so the stored form is unambiguous and two schedules that fire
    at the same moment cannot have different serialisations — which would give
    them different checksums.
    """
    if not isinstance(raw, str) or len(raw) != 5 or raw[2] != ":":
        return False
    hours, minutes = raw[:2], raw[3:]
    if not (hours.isdigit() and minutes.isdigit()):
        return False
    # isdigit() accepts Unicode decimal digits — "٠٣" is two digits and would
    # pass — so the value is bounded by int() on an ASCII-checked string.
    if not (hours.isascii() and minutes.isascii()):
        return False
    return 0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59


def _validate_int_set(field: str, values: list[int] | None, low: int, high: int) -> list[str]:
    """Problems with an optional integer set field, all of them.

    Null means "every one", which is why an *empty list* is refused rather than
    treated as the same thing: ``days_of_week: []`` reads as "no days", and a
    schedule that matches no day can never fire. Silently reinterpreting it as
    "every day" would turn a mistake into a running job.
    """
    if values is None:
        return []
    problems: list[str] = []
    if not values:
        problems.append(f"{field} must not be an empty list; omit it (null) to mean 'every value'")
        return problems
    seen: set[int] = set()
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int):
            # bool is an int in Python, so `True` would otherwise validate as 1.
            problems.append(f"{field} entry {value!r} is not an integer")
        elif not low <= value <= high:
            problems.append(f"{field} entry {value} is outside {low}-{high}")
        elif value in seen:
            problems.append(f"{field} contains {value} more than once")
        else:
            seen.add(value)
    return problems


# ---------------------------------------------------------------------------
# Next-run evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CalendarOccurrence:
    """One resolved execution instant, and what daylight saving did to it.

    The second field is why this is not just a ``datetime``. An occurrence that
    was shifted forward out of a nonexistent hour, or chosen from two ambiguous
    candidates, is a thing the interface should be able to say — "runs at 03:00
    tonight, because 02:30 does not exist" is the difference between a
    scheduler an operator trusts and one they suspect.
    """

    #: The instant, as an aware UTC datetime. UTC rather than the schedule's
    #: zone because this is what gets compared, stored and sorted, and a
    #: local-time datetime invites a comparison across two zones.
    at: datetime
    #: The local wall-clock time that produced it, ``HH:MM``.
    local_time: str
    #: Set when the requested local time did not exist and the policy shifted
    #: it. Null when nothing unusual happened.
    dst_adjustment: Literal["shifted_forward", "ambiguous_first", "ambiguous_second"] | None


def next_run(
    schedule: CalendarSchedule,
    *,
    after: datetime,
    horizon_days: int = SEARCH_HORIZON_DAYS,
) -> CalendarOccurrence | None:
    """The first occurrence strictly after ``after``, or ``None`` if there is none.

    A day-by-day walk, not arithmetic, and that is a deliberate choice rather
    than laziness. The cases that break arithmetic are the ones that matter:
    29 February exists in one year of four, "the 31st" exists in seven months
    of twelve, and a daylight-saving transition moves a local time without
    moving the date. Walking days and asking the time zone database what each
    local time means on that day gets all three right for the price of at most
    ``horizon_days`` iterations — bounded, and fast enough that the controller
    computes three of them to preview a schedule in a form.

    ``after`` must be timezone-aware; a naive datetime here would be silently
    interpreted, and "silently interpreted" is how a schedule fires an hour
    late in production and on time in a test.

    Raises:
        ValueError: if ``after`` is naive.
    """
    if after.tzinfo is None or after.tzinfo.utcoffset(after) is None:
        raise ValueError("`after` must be timezone-aware; a naive datetime cannot be ordered")

    zone = zoneinfo.ZoneInfo(schedule.timezone)
    boundary = after.astimezone(zone)
    # Start on the boundary's own local date: an occurrence later today is
    # still in the future, and starting tomorrow would skip it.
    cursor = boundary.date()

    # Sorted so the first match found on a matching day is the earliest one,
    # which is what makes a single pass correct.
    wall_times = sorted(schedule.times)

    for _ in range(horizon_days):
        if _day_matches(schedule, cursor.month, cursor.day, cursor.isoweekday() % 7):
            candidates: list[CalendarOccurrence] = []
            for wall in wall_times:
                candidates.extend(_resolve_local_time(zone, cursor, wall, schedule))
            # Resolution can reorder: a time shifted forward out of a
            # nonexistent hour may land after a later listed time, and an
            # ambiguous time resolved to its second occurrence certainly can.
            # So the day's candidates are sorted by instant rather than trusted
            # to come out in the order their wall-clock strings did.
            for occurrence in sorted(candidates, key=lambda item: item.at):
                if occurrence.at > after:
                    return occurrence
        cursor += timedelta(days=1)

    return None


def _day_matches(schedule: CalendarSchedule, month: int, day: int, weekday: int) -> bool:
    """Whether a date satisfies every day restriction.

    ANDed, unlike cron (§4). ``weekday`` is 0 = Sunday, matching the wire
    format rather than Python's 0 = Monday — converted by the caller, because
    doing it here would hide the conversion inside the one function whose
    contract depends on it.
    """
    if schedule.months is not None and month not in schedule.months:
        return False
    if schedule.days_of_month is not None and day not in schedule.days_of_month:
        return False
    return not (schedule.days_of_week is not None and weekday not in schedule.days_of_week)


def _resolve_local_time(
    zone: zoneinfo.ZoneInfo,
    date: Any,
    wall: str,
    schedule: CalendarSchedule,
) -> list[CalendarOccurrence]:
    """Turn one local wall-clock time on one date into zero, one or two instants.

    Where the daylight-saving policy is actually applied. Three cases, and the
    detection of each is the interesting part:

    * **Nonexistent** (spring forward), detected by a **UTC round trip**. The
      obvious test does not work and getting this wrong is silent:
      ``datetime.replace(tzinfo=…)`` attaches a zone without normalising the
      fields, so a nonexistent ``02:30`` keeps ``.hour == 2`` and comparing
      ``.hour`` against the requested hour can never detect anything. What PEP
      495 actually specifies is that a nonexistent local time resolves using
      the *pre-transition* offset, so converting to UTC and back yields a
      *different* local time — ``02:30`` comes back as ``03:30``. That
      inequality is the test.

      Under ``shift_forward`` the instant used is **the first local time that
      exists**, found by walking minutes forward and round-tripping each — so
      a ``02:30`` request in a 02:00→03:00 gap runs at 03:00, which is what
      ``docs/SCHEDULING.md`` §5.1 specifies. Note that this is *not* the same
      as the round trip's own answer of 03:30: shifting by the size of the gap
      and running at the first existing instant differ by thirty minutes, and
      the document asked for the latter. Walking also keeps the half-hour and
      45-minute transitions some zones have used correct, which a hardcoded
      ``+1 hour`` would not.
    * **Ambiguous** (fall back). Detected with ``fold``: the same local time
      with ``fold=0`` and ``fold=1`` yields two different UTC offsets. The
      policy picks one, or both.
    * **Neither**, which is every other day of the year.

    **The order of those two checks is load-bearing**, and getting it wrong is
    the kind of bug that only shows up twice a year. Differing offsets across
    the two folds does *not* mean "ambiguous": PEP 495 gives a **nonexistent**
    time the pre-transition offset at ``fold=0`` and the post-transition offset
    at ``fold=1``, so the folds disagree for a spring-forward gap exactly as
    they do for a fall-back repeat. Testing offsets first therefore reports
    every nonexistent time as ambiguous, applies the wrong policy, and runs at
    the wrong instant — measured here as a ``02:30`` schedule firing at 03:30
    tagged ``ambiguous_first`` instead of at 03:00 tagged ``shifted_forward``,
    and a ``skip`` policy running anyway. Existence is checked first, because
    "does this local time occur at all" is the question the fold comparison
    cannot answer.
    """
    hour, minute = int(wall[:2]), int(wall[3:])
    naive = datetime(date.year, date.month, date.day, hour, minute)

    if not _local_time_exists(zone, naive):
        if schedule.dst_nonexistent is DstNonexistent.SKIP:
            return []
        shifted = _first_existing_at_or_after(zone, naive)
        if shifted is None:  # pragma: no cover - a >2h gap has never existed
            return []
        return [CalendarOccurrence(_utc(shifted), wall, "shifted_forward")]

    first = naive.replace(tzinfo=zone)
    second = naive.replace(tzinfo=zone, fold=1)

    # Ambiguous: this local time exists, and the two folds disagree about the
    # offset — so two distinct instants share it.
    if first.utcoffset() != second.utcoffset():
        if schedule.dst_ambiguous is DstAmbiguous.FIRST:
            return [CalendarOccurrence(_utc(first), wall, "ambiguous_first")]
        if schedule.dst_ambiguous is DstAmbiguous.SECOND:
            return [CalendarOccurrence(_utc(second), wall, "ambiguous_second")]
        return [
            CalendarOccurrence(_utc(first), wall, "ambiguous_first"),
            CalendarOccurrence(_utc(second), wall, "ambiguous_second"),
        ]

    return [CalendarOccurrence(_utc(first), wall, None)]


def _local_time_exists(zone: zoneinfo.ZoneInfo, naive: datetime) -> bool:
    """Whether this local time occurs in this zone.

    The UTC round trip described in :func:`_resolve_local_time`. A local time
    that exists survives it unchanged; one inside a spring-forward gap comes
    back as a different local time, because it was resolved with the offset
    that applied before the transition.
    """
    aware = naive.replace(tzinfo=zone)
    return _utc(aware).astimezone(zone).replace(tzinfo=None) == naive


def _first_existing_at_or_after(zone: zoneinfo.ZoneInfo, naive: datetime) -> datetime | None:
    """Walk minutes forward to the first local time that exists.

    Minute granularity because every transition in the tz database is a whole
    number of minutes, and 120 iterations bounds the search to more than the
    largest gap ever recorded — the common case is 60 minutes, Lord Howe
    Island's is 30.

    Returns ``None`` if nothing in the next two hours exists, which would mean
    a transition larger than any in the database. Handled rather than
    asserted: a tz database update must not be able to raise here.
    """
    for offset in range(1, 121):
        probe = naive + timedelta(minutes=offset)
        if _local_time_exists(zone, probe):
            return probe.replace(tzinfo=zone)
    return None


def _utc(moment: datetime) -> datetime:
    """Convert an aware local datetime to UTC."""
    return moment.astimezone(zoneinfo.ZoneInfo("UTC"))


def upcoming_runs(
    schedule: CalendarSchedule, *, after: datetime, count: int
) -> list[CalendarOccurrence]:
    """The next ``count`` occurrences, for the interface's schedule preview.

    A calendar expression nobody can evaluate by eye is one operators get
    wrong, so the form shows what it will actually do. Returns fewer than
    ``count`` — possibly none — if the schedule runs out within the search
    horizon, which a February-29th schedule legitimately does.
    """
    occurrences: list[CalendarOccurrence] = []
    cursor = after
    for _ in range(count):
        occurrence = next_run(schedule, after=cursor)
        if occurrence is None:
            break
        occurrences.append(occurrence)
        cursor = occurrence.at
    return occurrences
