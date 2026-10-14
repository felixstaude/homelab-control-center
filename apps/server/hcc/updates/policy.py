"""Update policy: what to do about an available update, and when.

Pure, like :mod:`hcc.updates.compat`, with one concession — it takes the
current time as an argument. It still performs no I/O and reads no clock of its
own, which is what makes "does a Sunday 02:00 window admit 01:59" a test rather
than a thing somebody waits for.

## The policies

Five, and automatic updates are **off by default**. The default is
:attr:`Policy.MANUAL`, on every host, and moving a host off it is an
administrator action that is audited.

| Policy | What happens when an update is available |
| --- | --- |
| `manual` | Nothing. The interface shows it. |
| `notify` | Nothing is installed; it is surfaced as needing attention. |
| `patch` | A compatible patch release installs itself. |
| `minor` | A compatible patch *or* minor release installs itself. |
| `pinned` | The host is held at a version. |

There is no `major` policy, and the omission is the point: a major version is
where a project says "this one breaks something". An administrator who wants
one installs it deliberately. Offering "automatically install breaking changes"
as a checkbox would be offering somebody a way to take their fleet down while
asleep.

## Pre-releases are never automatic from a stable channel

A policy says *how large a step* may be taken automatically; the channel says
*what kind of release* is eligible. Both have to agree. A host on `patch` whose
channel is `beta` can automatically take `0.1.1-beta.2`, because it opted into
pre-releases and that is a patch step. A host on `stable` cannot, because no
pre-release is in its channel at all — which :func:`~hcc.updates.compat.plan_update`
has already decided before this module sees the plan.

## Maintenance windows

A window is an *interval* — "between 02:00 and 04:00 on Sunday" — which is a
different question from the point-in-time occurrences
:mod:`hcc.monitoring.schedule` answers, so this does not reuse ``next_run``.
It does reuse the conventions: IANA zone names via ``zoneinfo``, and
``0 = Sunday … 6 = Saturday`` for weekdays, matching ``days_of_week`` there.
Two weekday numbering schemes in one codebase is a defect waiting for a
Sunday.

A window that crosses midnight (``22:00`` to ``02:00``) is ordinary rather than an
error, and the weekday is matched against the day the window **started**. The
alternative — matching the day it is currently — means a Sunday 22:00 to 02:00
window stops admitting work at midnight, which is not what anybody means by
"Sunday night".
"""

from __future__ import annotations

import enum
import zoneinfo
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Final

from hcc.updates.compat import Outcome
from hcc.updates.version import StepKind

if TYPE_CHECKING:
    from hcc.updates.compat import UpdatePlan

#: Weekday numbering, stated once. 0 = Sunday, matching
#: ``CalendarSchedule.days_of_week`` and cron, and *not* matching Python's
#: ``datetime.weekday()`` where 0 = Monday. The conversion happens in exactly
#: one place: :func:`_weekday_of`.
SUNDAY: Final = 0
SATURDAY: Final = 6


class Policy(enum.Enum):
    """How much a host may update without being asked."""

    #: Nothing automatic. The default, on every host.
    MANUAL = "manual"
    #: Nothing automatic, and surfaced as needing attention.
    NOTIFY = "notify"
    #: Compatible patch releases install themselves.
    PATCH = "patch"
    #: Compatible patch and minor releases install themselves.
    MINOR = "minor"
    #: Held at a specific version.
    PINNED = "pinned"

    @property
    def is_automatic(self) -> bool:
        """Whether this policy can install anything without being asked."""
        return self in {Policy.PATCH, Policy.MINOR}


#: The default, spelled out so that "off by default" is a value rather than a
#: claim in a document.
DEFAULT_POLICY: Final = Policy.MANUAL

#: Which step kinds each policy will take automatically.
#:
#: A table rather than a chain of comparisons, because the question "can this
#: policy take this step" is asked from three places and a table can be
#: asserted exhaustively. `PRERELEASE` is a step between two versions with the
#: same core triple — `0.2.0-beta.1` → `0.2.0` — and it is permitted wherever
#: `PATCH` is, because it is at most as large.
#:
#: `MAJOR` appears nowhere. See the module docstring.
AUTOMATIC_STEPS: Final[dict[Policy, frozenset[str]]] = {
    Policy.MANUAL: frozenset(),
    Policy.NOTIFY: frozenset(),
    Policy.PATCH: frozenset({StepKind.PATCH, StepKind.PRERELEASE}),
    Policy.MINOR: frozenset({StepKind.PATCH, StepKind.PRERELEASE, StepKind.MINOR}),
    Policy.PINNED: frozenset(),
}


class Action(enum.Enum):
    """What the scheduler should do about one host right now."""

    #: Do nothing, and there is nothing to say.
    NOTHING = "nothing"
    #: Do nothing, and surface it to the administrator.
    NOTIFY = "notify"
    #: Start an update.
    INSTALL = "install"
    #: An update is due and this is not the time for it.
    DEFER = "defer"

    @property
    def starts_work(self) -> bool:
        return self is Action.INSTALL


class WindowError(ValueError):
    """A maintenance window that cannot be used."""


@dataclass(frozen=True, slots=True)
class MaintenanceWindow:
    """When a host is willing to be interrupted.

    ``None`` as a whole means "any time", which is the default: a window is an
    extra restriction an administrator adds, not something they have to
    configure before updates work at all.
    """

    timezone: str = "UTC"
    #: 0 = Sunday … 6 = Saturday. Empty means every day.
    weekdays: tuple[int, ...] = ()
    start: time = time(0, 0)
    end: time = time(0, 0)

    def __post_init__(self) -> None:
        try:
            zoneinfo.ZoneInfo(self.timezone)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError, KeyError) as error:
            raise WindowError(
                f"{self.timezone!r} is not a known IANA time zone name, so this "
                "window could not be evaluated. Use something like 'Europe/Berlin'."
            ) from error
        for day in self.weekdays:
            if not SUNDAY <= day <= SATURDAY:
                raise WindowError(f"weekday {day} is out of range; 0 = Sunday … 6 = Saturday")

    @property
    def is_always_open(self) -> bool:
        """Whether this window admits every moment.

        ``start == end`` means a zero-length interval, which would admit
        nothing — and a window that admits nothing is never what somebody
        configured on purpose, it is what an unset form produces. So equal
        bounds mean "all day", and combined with no weekday restriction that
        is "always".
        """
        return self.start == self.end and not self.weekdays

    def admits(self, moment: datetime) -> bool:
        """Whether *moment* falls inside this window.

        Args:
            moment: a timezone-aware instant. A naive one is refused rather
                than assumed to be UTC — the whole point of this type is that
                "02:00" means 02:00 somewhere specific, and silently guessing
                the zone is how a window fires at the wrong time of day.
        """
        if moment.tzinfo is None:
            raise WindowError(
                "a maintenance window must be evaluated against a timezone-aware "
                "instant; a naive one cannot be placed in the window's zone"
            )
        local = moment.astimezone(zoneinfo.ZoneInfo(self.timezone))

        if self.start == self.end:
            # All day. Only the weekday restriction applies.
            return not self.weekdays or _weekday_of(local) in self.weekdays

        current = local.time()
        if self.start < self.end:
            inside = self.start <= current < self.end
            started_on = local
        else:
            # Crosses midnight: 22:00 to 02:00. Two disjoint pieces of the day.
            inside = current >= self.start or current < self.end
            # The weekday is the day the window *started*, so a Sunday
            # 22:00 to 02:00 window still admits Monday 00:30. Matching the
            # current day instead would make it stop at midnight, which is not
            # what anybody means by "Sunday night".
            started_on = local if current >= self.start else local - _ONE_DAY
        if not inside:
            return False
        return not self.weekdays or _weekday_of(started_on) in self.weekdays

    def describe(self) -> str:
        """One line for an operator, naming the zone.

        The zone is always named. A window rendered as "02:00-04:00" with the
        reader's own offset assumed is the single most reliable way to make
        somebody believe an update ran at the wrong time.
        """
        if self.is_always_open:
            return "any time"
        when = (
            "all day"
            if self.start == self.end
            else (f"{self.start.strftime('%H:%M')}-{self.end.strftime('%H:%M')}")
        )
        if not self.weekdays:
            return f"{when} {self.timezone}"
        names = ", ".join(_WEEKDAY_NAMES[day] for day in sorted(self.weekdays))
        return f"{names} {when} {self.timezone}"


_ONE_DAY: Final = timedelta(days=1)

_WEEKDAY_NAMES: Final = (
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
)

#: A window that admits everything. The default for a host with none set.
ALWAYS: Final = MaintenanceWindow()


def _weekday_of(moment: datetime) -> int:
    """The weekday of *moment* as 0 = Sunday … 6 = Saturday.

    Python's ``weekday()`` is 0 = Monday and ``isoweekday()`` is 1 = Monday,
    and this project's stored convention is neither. The conversion lives here
    and nowhere else: two weekday numbering schemes in one codebase is a defect
    waiting for a Sunday.
    """
    return moment.isoweekday() % 7


@dataclass(frozen=True, slots=True)
class Decision:
    """What to do about one host, and the sentence explaining it.

    The reason is not decoration. "Why did my fleet not update" is the question
    this system exists to answer, and a decision that cannot explain itself
    makes the answer a code-reading exercise.
    """

    action: Action
    reason: str


def decide(
    plan: UpdatePlan,
    *,
    policy: Policy,
    window: MaintenanceWindow = ALWAYS,
    now: datetime,
) -> Decision:
    """Decide what to do about one host's available update.

    Order matters, and it is: is there anything to do, may this policy do it,
    is this the time. Each gate produces a different :class:`Action`, so the
    interface can distinguish "nothing to do" from "waiting for Sunday" — which
    look identical from the outside and are not the same at all.
    """
    if plan.outcome is Outcome.UPDATE_AVAILABLE_INCOMPATIBLE:
        # Never automatic, whatever the policy says. A policy expresses how
        # large a step is acceptable, not permission to ignore a blocker.
        reasons = "; ".join(blocker.detail for blocker in plan.blockers)
        return Decision(
            action=Action.NOTIFY,
            reason=f"{plan.target} is available and cannot be installed: {reasons}",
        )

    if not plan.has_update:
        if plan.outcome in {Outcome.UNSUPPORTED, Outcome.UNKNOWN_VERSION}:
            return Decision(
                action=Action.NOTIFY,
                reason=plan.detail or f"this host needs attention: {plan.outcome.value}",
            )
        return Decision(action=Action.NOTHING, reason=f"no update: {plan.outcome.value}")

    if policy is Policy.NOTIFY:
        return Decision(
            action=Action.NOTIFY,
            reason=f"{plan.target} is available; this host is set to notify only",
        )

    if not policy.is_automatic:
        return Decision(
            action=Action.NOTHING,
            reason=(
                f"{plan.target} is available; this host's policy is "
                f"{policy.value}, so nothing happens automatically"
            ),
        )

    permitted = AUTOMATIC_STEPS[policy]
    if plan.step not in permitted:
        return Decision(
            action=Action.NOTIFY,
            reason=(
                f"{plan.target} is a {plan.step} update and this host installs "
                f"{' and '.join(sorted(permitted))} automatically. Install it by hand "
                "if you want it."
            ),
        )

    if not window.admits(now):
        return Decision(
            action=Action.DEFER,
            reason=(f"{plan.target} is due and the maintenance window is {window.describe()}"),
        )

    return Decision(
        action=Action.INSTALL,
        reason=(
            f"{plan.installed} → {plan.target} is a {plan.step} update, permitted by "
            f"the {policy.value} policy, inside the maintenance window"
        ),
    )
