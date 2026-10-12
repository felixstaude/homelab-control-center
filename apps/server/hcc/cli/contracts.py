"""Emit the cross-language contract documents.

Run by ``scripts/contracts.sh``. Writes one JSON object to stdout containing
every fact the Go agent and the TypeScript client have to agree with the
controller about, so that three hand-maintained copies of one table cannot
drift silently.

## Why a module rather than a function in the script

The script is bash and the registry is Python, so something has to cross that
boundary. Doing it here rather than with an inline heredoc means the
generation is importable, type-checked by mypy, and lintable — and a change to
the registry's shape fails in the same gate as the rest of the codebase
instead of only when somebody runs the script.

## The calendar cases are generated, not hand-written

Each case is ``(schedule, after) → occurrence``, computed by the controller's
own evaluator. Both test suites then assert their implementation reproduces
it. The alternative — a hand-written table — would be a third implementation
of the rule, written by whoever was least sure of it.

Generating them from one side does mean the Go suite is asserting agreement
with Python rather than agreement with the specification. That is the weaker
of the two claims, and it is the one worth having: the specification is prose
(``docs/SCHEDULING.md`` §5), it is tested directly on the Python side against
real transition dates, and what the agent actually needs is to not disagree
with the controller.
"""

from __future__ import annotations

import json
import sys
import zoneinfo
from datetime import datetime
from typing import Any

from hcc.monitoring import modules as registry
from hcc.monitoring.document import DOCUMENT_VERSION
from hcc.monitoring.schedule import (
    MAX_INTERVAL_SECONDS,
    MIN_INTERVAL_SECONDS,
    CalendarSchedule,
    next_run,
)

UTC = zoneinfo.ZoneInfo("UTC")

#: The calendar cases both implementations must reproduce.
#:
#: Chosen to cover every branch of the resolution rule rather than to be
#: numerous: an ordinary day, a weekday restriction, a day-of-month and
#: day-of-week conjunction, 29 February, and all four daylight-saving
#: policies against real transition dates in two zones with different
#: transition *sizes* — Berlin's hour and Lord Howe Island's half hour, which
#: is what catches an implementation that shifts by a hardcoded hour.
_CALENDAR_CASES: list[dict[str, Any]] = [
    {
        "name": "every day at 03:00",
        "schedule": {"timezone": "Europe/Berlin", "times": ["03:00"]},
        "after": "2024-06-01T12:00:00+00:00",
    },
    {
        "name": "two times a day, earliest first",
        "schedule": {"timezone": "Europe/Berlin", "times": ["20:00", "08:00"]},
        "after": "2024-06-01T00:00:00+00:00",
    },
    {
        "name": "mondays only, 0 is sunday",
        "schedule": {"timezone": "Europe/Berlin", "times": ["03:00"], "days_of_week": [1]},
        "after": "2024-06-01T00:00:00+00:00",
    },
    {
        "name": "sunday is zero",
        "schedule": {"timezone": "UTC", "times": ["00:00"], "days_of_week": [0]},
        "after": "2024-06-01T00:00:00+00:00",
    },
    {
        "name": "day of month and day of week are ANDed, not ORed",
        "schedule": {
            "timezone": "UTC",
            "times": ["00:00"],
            "days_of_month": [13],
            "days_of_week": [5],
        },
        "after": "2024-01-01T00:00:00+00:00",
    },
    {
        "name": "29 february, four years away",
        "schedule": {
            "timezone": "UTC",
            "times": ["00:00"],
            "months": [2],
            "days_of_month": [29],
        },
        "after": "2024-03-01T00:00:00+00:00",
    },
    {
        "name": "spring forward, shift to the first instant that exists",
        "schedule": {"timezone": "Europe/Berlin", "times": ["02:30"]},
        "after": "2024-03-30T12:00:00+00:00",
    },
    {
        "name": "spring forward, skip",
        "schedule": {
            "timezone": "Europe/Berlin",
            "times": ["02:30"],
            "dst_nonexistent": "skip",
        },
        "after": "2024-03-30T12:00:00+00:00",
    },
    {
        "name": "spring forward, a 30-minute gap shifts 30 minutes",
        "schedule": {"timezone": "Australia/Lord_Howe", "times": ["02:15"]},
        "after": "2024-10-05T00:00:00+00:00",
    },
    {
        "name": "fall back, first occurrence",
        "schedule": {"timezone": "Europe/Berlin", "times": ["02:30"]},
        "after": "2024-10-26T12:00:00+00:00",
    },
    {
        "name": "fall back, second occurrence",
        "schedule": {
            "timezone": "Europe/Berlin",
            "times": ["02:30"],
            "dst_ambiguous": "second",
        },
        "after": "2024-10-26T12:00:00+00:00",
    },
    {
        "name": "fall back, both occurrences, the first of the two",
        "schedule": {
            "timezone": "Europe/Berlin",
            "times": ["02:30"],
            "dst_ambiguous": "both",
        },
        "after": "2024-10-26T12:00:00+00:00",
    },
    {
        "name": "fall back, both occurrences, the second of the two",
        "schedule": {
            "timezone": "Europe/Berlin",
            "times": ["02:30"],
            "dst_ambiguous": "both",
        },
        "after": "2024-10-27T00:45:00+00:00",
    },
    {
        "name": "a time outside the gap on a transition night is unaffected",
        "schedule": {"timezone": "Europe/Berlin", "times": ["01:30"]},
        "after": "2024-03-30T12:00:00+00:00",
    },
]


def build() -> dict[str, Any]:
    """The whole contract document."""
    return {"modules": _modules(), "calendar": _calendar()}


def _modules() -> dict[str, Any]:
    return {
        "registry_version": registry.REGISTRY_VERSION,
        "document_version": DOCUMENT_VERSION,
        "min_interval_seconds": MIN_INTERVAL_SECONDS,
        "max_interval_seconds": MAX_INTERVAL_SECONDS,
        "modules": [
            {
                "id": module.id,
                "name": module.name,
                "platform": module.platform.value,
                "schedule_kinds": [kind.value for kind in module.schedule_kinds],
                "min_interval_seconds": module.min_interval_seconds,
                "default_interval_seconds": module.default_interval_seconds,
                "data_schema_version": module.data_schema_version,
                "requires_privileges": module.requires_privileges,
                "allows_duplicate_runs": module.allows_duplicate_runs,
                "metrics": [
                    {
                        "id": metric.id,
                        "name": metric.name,
                        "unit": metric.unit.value,
                        "kind": metric.kind.value,
                        "label_keys": list(metric.label_keys),
                    }
                    for metric in module.metrics
                ],
            }
            for module in registry.MODULE_LIST
        ],
    }


def _calendar() -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    for case in _CALENDAR_CASES:
        schedule = CalendarSchedule.model_validate(case["schedule"])
        after = datetime.fromisoformat(case["after"])
        occurrence = next_run(schedule, after=after)
        cases.append(
            {
                "name": case["name"],
                # The *resolved* schedule, with every default made explicit, so
                # the other implementations are not also asserting agreement
                # about what a default is.
                "schedule": schedule.to_document(),
                "after": after.isoformat(),
                "expected": (
                    None
                    if occurrence is None
                    else {
                        "at": occurrence.at.isoformat(),
                        "local_time": occurrence.local_time,
                        "dst_adjustment": occurrence.dst_adjustment,
                    }
                ),
            }
        )
    return {"cases": cases}


def main() -> None:
    """Write the contract document to stdout."""
    json.dump(build(), sys.stdout)


if __name__ == "__main__":  # pragma: no cover - invoked by scripts/contracts.sh
    main()
