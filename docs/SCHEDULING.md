# Scheduling Model

One reusable scheduling model, shared by every monitoring module, retention
job and periodic task. Defined once so that an operator learns it once.

> **Status: design.** Not implemented. Tracked in [`ROADMAP.md`](ROADMAP.md).

- [1. Why one model](#1-why-one-model)
- [2. Representation](#2-representation)
- [3. Interval schedules](#3-interval-schedules)
- [4. Calendar schedules](#4-calendar-schedules)
- [5. Daylight saving time](#5-daylight-saving-time)
- [6. Missed executions](#6-missed-executions)
- [7. Drift and overrun](#7-drift-and-overrun)
- [8. Jitter](#8-jitter)
- [9. Which modes each module allows](#9-which-modes-each-module-allows)
- [10. Validation](#10-validation)

---

## 1. Why one model

Four scheduling kinds cover everything the product needs:

| Kind | Meaning | Example use |
| --- | --- | --- |
| `interval` | Every N seconds, continuously | CPU, memory, temperature |
| `calendar` | At wall-clock times on chosen days | SMART self-tests, retention jobs |
| `event` | Driven by an external event stream, not a clock | Docker lifecycle, Docker logs |
| `manual` | Only when a user asks | On-demand SMART attribute read |

Not every module accepts every kind (§9). The model is shared, the *permitted
subset* is per module, and the controller and the agent both enforce it.

---

## 2. Representation

### 2.1 Durations are normalised to integer seconds

Every duration in the system — interval, timeout, retention, cooldown — is
stored as **integer seconds**. There is no `"5m"`, no `"2h30m"`, no float.

```jsonc
{ "kind": "interval", "interval_seconds": 9000 }   // every 2h30m
{ "kind": "interval", "interval_seconds": 17 }     // every 17 seconds
```

Why seconds, and why integers:

- **Comparable and validatable.** Enforcing "at least 5 seconds" is an integer
  comparison, not a parse-then-compare against an ambiguous unit.
- **Arbitrary intervals come free.** 17 seconds, 45 minutes and 2 h 30 min are
  not special cases; they are 17, 2700 and 9000.
- **No parser, no dependency.** The agent has no third-party dependencies
  (ADR-011) and now needs no duration-string parser either.
- **One representation end to end.** The same integer is stored in PostgreSQL,
  sent on the wire, and passed to a Go `time.Ticker`. No conversion means no
  conversion bug.

Presentation is the UI's job: the interface offers presets and a
hours/minutes/seconds builder, and renders `9000` as "every 2h 30m". The
stored value is always seconds. See
[ADR-019](ARCHITECTURE.md#adr-019-durations-are-normalised-to-integer-seconds).

### 2.2 Presets are UI sugar, not a separate type

The interface offers: 5 s, 10 s, 30 s, 1 min, 5 min, 15 min, 1 h, 6 h, daily.
Each is just an `interval_seconds` value. A preset and a custom interval are
indistinguishable once stored, so no code branches on "is this a preset".

### 2.3 Calendar schedules are structured, not cron strings

```jsonc
{
  "kind": "calendar",
  "timezone": "Europe/Berlin",       // IANA name, required
  "months": null,                     // null = every month
  "days_of_month": null,              // null = every day of month
  "days_of_week": [0, 3],             // 0 = Sunday … 6 = Saturday
  "times": ["03:30", "15:30"],        // local wall clock, HH:MM
  "on_missed": "run_once",
  "missed_grace_seconds": 7200,
  "dst_nonexistent": "shift_forward",
  "dst_ambiguous": "first"
}
```

**Deliberately not cron syntax.** Reasons, in order of importance:

1. **Validatable field by field.** A UI can present weekday checkboxes and a
   time picker and reject bad input per field. `"*/7 2-5 * * 1-5"` can only be
   validated as a whole and explained to the user as prose.
2. **No ambiguity.** Cron's day-of-month and day-of-week interaction is an
   OR, not an AND, which surprises almost everyone and silently runs jobs on
   unintended days.
3. **DST policy has somewhere to live.** Cron has no field for "what happens
   when 02:30 does not exist tonight". Here it is explicit (§5).
4. **No parser dependency** in the agent (ADR-011).

The cost is that operators who know cron cannot paste an expression. Accepted:
the audience is homelab operators configuring a handful of schedules in a web
form, not writing crontabs. See
[ADR-020](ARCHITECTURE.md#adr-020-structured-calendar-schedules-not-cron-expressions).

---

## 3. Interval schedules

Semantics: run, then run again `interval_seconds` later, forever.

- **Measured on a monotonic clock.** Intervals are immune to DST transitions,
  NTP steps and manual clock changes. An interval of 300 s is 300 s of elapsed
  time, never "until the wall clock reads a multiple of 5 minutes".
- **Phase is anchored to a fixed epoch per (host, module)**, not to the last
  completed run. See §7.
- **No alignment to wall-clock boundaries.** A 5-minute interval does not
  guarantee runs at :00, :05, :10. Operators who need wall-clock alignment
  want a calendar schedule, and the UI says so.

Each module declares a floor (`min_interval_seconds`) and the controller
enforces it. Floors exist because some collectors are genuinely expensive:
spinning up `smartctl` every second would hammer the device and tell you
nothing new. Per-module values are in
[`MONITORING_MODULES.md`](MONITORING_MODULES.md).

---

## 4. Calendar schedules

Semantics: run at each listed time, on each matching day, in the configured
time zone.

- `days_of_week` and `days_of_month` are **ANDed** when both are set, unlike
  cron. Both `null` means every day.
- `times` may list several entries; each produces an execution.
- `timezone` is a required IANA name (`Europe/Berlin`), never a fixed offset
  like `UTC+2`. A fixed offset cannot express "03:30 local time, year round",
  which is what an operator scheduling a nightly self-test actually means.
- Evaluation happens **on the agent**, using the host's configured time zone
  from the config document. The agent must be able to schedule while the
  controller is unreachable ([`AGENT_ARCHITECTURE.md §8`](AGENT_ARCHITECTURE.md#8-offline-behaviour)),
  so the controller cannot be the scheduler.

The agent embeds the IANA time zone database (Go's standard
`time/tzdata`), so schedules are correct on minimal hosts with no system
tzdata installed, and do not silently fall back to UTC.

Time zone data ages: a government changing DST rules requires an agent update
to schedule correctly. This is noted in
[§5.4](#54-time-zone-database-updates).

---

## 5. Daylight saving time

Wall-clock scheduling across a DST transition is genuinely ambiguous. Policy is
explicit rather than emergent.

Worked examples use `Europe/Berlin`, where clocks jump 02:00 → 03:00 in spring
and 03:00 → 02:00 in autumn.

### 5.1 Spring forward — the time does not exist

A schedule at `02:30` on the transition night: 02:30 never occurs.

| `dst_nonexistent` | Behaviour |
| --- | --- |
| `shift_forward` *(default)* | Runs at 03:00, the first instant that exists. |
| `skip` | Does not run that day. Recorded as skipped with the reason. |

Default is `shift_forward` because a nightly maintenance task that silently
does not happen once a year is worse than one that happens 30 minutes late.

### 5.2 Fall back — the time occurs twice

A schedule at `02:30` on the transition night: 02:30 happens twice, an hour
apart.

| `dst_ambiguous` | Behaviour |
| --- | --- |
| `first` *(default)* | Runs at the first 02:30 (still in summer time). |
| `second` | Runs at the second 02:30 (already in standard time). |
| `both` | Runs twice. Only permitted for modules where a duplicate run is harmless. |

Default is `first` — run once, as early as the operator asked. `both` is
explicitly **not** permitted for SMART self-tests: running a long self-test
twice is a real cost to the device, and nobody asking for a 02:30 test means
"twice, once a year".

### 5.3 Intervals are unaffected

Interval schedules use a monotonic clock (§3) and have no DST behaviour at all.
A 1-hour interval produces 24 runs on a 23-hour day and 24 on a 25-hour day —
24 hours of elapsed time either way. This is correct for sampling, and it is
why temperature and CPU collection use intervals rather than hourly calendar
entries.

### 5.4 Time zone database updates

The embedded tzdata is fixed at agent build time. If a jurisdiction changes
its DST rules, calendar schedules on that host are evaluated against stale
rules until the agent is updated.

Mitigation: the agent reports its tzdata version in the capability report, and
the controller surfaces "agent tzdata is older than the controller's" as an
advisory. Not an error — a stale rule shifts a maintenance window by an hour,
it does not break monitoring.

---

## 6. Missed executions

An execution is missed when the agent was not running, or was overloaded, at
the scheduled moment.

### 6.1 Interval schedules never backfill

A missed sample is a **gap**, and the gap is the truth. Collecting CPU
utilisation "for 03:00" at 04:00 would record a measurement that was never
taken, which
[principle 2](PRODUCT_VISION.md#2-never-invent-information) forbids.

On restart an interval schedule simply resumes. The gap is visible in the UI as
absent data, never interpolated and never zero-filled.

### 6.2 Calendar schedules may catch up, within a grace window

Calendar schedules are *tasks*, not samples, so a late run can still be
useful.

| `on_missed` | Behaviour |
| --- | --- |
| `skip` *(default)* | Do not run. Record that it was missed. |
| `run_once` | Run once on recovery, if within `missed_grace_seconds`; never run N times for N missed occurrences. |

`run_once` never fires more than once no matter how many occurrences were
missed. An agent offline for two weeks with a daily task does not come back and
run it fourteen times — that is how a maintenance window becomes an outage.

Default is `skip`, because the safe assumption for an unknown task is "do not
start work the operator is no longer expecting".

### 6.3 Missed runs are recorded

Every miss is recorded with its scheduled time, the reason, and the action
taken, and is visible per host. Silent misses are the failure mode that makes
operators distrust a scheduler.

---

## 7. Drift and overrun

### 7.1 Phase is anchored, so drift does not accumulate

Naive scheduling — `sleep(interval)` after each run — makes every run's
duration push the next one later. At 5-second intervals with a 200 ms
collector, that is ~4 minutes of drift per day.

Each schedule therefore has a **fixed phase anchor** derived from a stable
seed (host id + module name), and the next execution is computed as the next
multiple of the interval after that anchor. Collection duration shifts
*nothing*. Go's `time.Ticker` already behaves this way; the rule is written
down so a future refactor does not replace it with a sleep loop.

### 7.2 Overrun: skip, never queue

If a collection is still running when its next execution is due, the due
execution is **skipped** and counted.

Queueing instead would let a slow collector build an unbounded backlog, and the
backlog would be of samples whose timestamps are already wrong. Skipping keeps
the schedule honest: the data has a gap, and the gap has a counter explaining
it.

Each module has an execution timeout. A collector exceeding it is cancelled,
the failure is recorded, and the next execution proceeds normally. One wedged
collector must not stall the others — modules are scheduled independently, not
in a shared loop.

### 7.3 Counters

Per host and module, exposed in the UI:

| Counter | Meaning |
| --- | --- |
| `executions_total` | Completed executions |
| `skipped_overrun_total` | Skipped because the previous run was still going |
| `missed_total` | Scheduled while the agent was down |
| `timeout_total` | Cancelled for exceeding the module timeout |
| `error_total` | Ran and failed |

These make "why is this graph sparse?" answerable without reading logs.

---

## 8. Jitter

```jsonc
{ "kind": "interval", "interval_seconds": 30, "jitter_seconds": 5 }
```

Each execution is offset by a random amount in `[0, jitter_seconds)`.

Jitter matters at fleet scale and is not cosmetic. Twenty agents on a 30-second
interval, all enrolled the same afternoon, will otherwise align and deliver
twenty simultaneous batches every 30 seconds — turning smooth load into
spikes, and making rate limits fire on honest traffic.

- Default: `jitter_seconds = min(interval_seconds / 10, 30)`, so it scales with
  the interval and never dominates it.
- Jitter perturbs each execution independently; it does not shift the phase
  anchor, so it cannot accumulate into drift (§7.1).
- Set to 0 for schedules where exact timing matters more than smooth load.

---

## 9. Which modes each module allows

Permitted kinds are a property of the module, enforced by both the controller
(at save time) and the agent (at apply time). Double validation is intentional:
the controller gives a good error message in the UI, and the agent is the
component that must never act on a configuration it cannot honour.

| Module | `interval` | `calendar` | `event` | `manual` | Note |
| --- | :-: | :-: | :-: | :-: | --- |
| CPU | ✅ | ❌ | ❌ | ❌ | Sampling; a calendar makes no sense |
| Memory | ✅ | ❌ | ❌ | ❌ | |
| Disk usage | ✅ | ✅ | ❌ | ✅ | Slow-moving; hourly or nightly is reasonable |
| Disk I/O | ✅ | ❌ | ❌ | ❌ | Rate counters need regular sampling |
| Temperature | ✅ | ❌ | ❌ | ❌ | |
| Network interfaces | ✅ | ❌ | ❌ | ❌ | Rate counters |
| SMART attributes | ✅ | ✅ | ❌ | ✅ | Poll periodically; may also run nightly |
| **SMART self-test** | ❌ | ✅ | ❌ | ✅ | **Calendar or manual only** — see below |
| RAID status | ✅ | ❌ | ✅ | ✅ | Polling plus controller events where available |
| Systemd services | ✅ | ❌ | ✅ | ✅ | D-Bus signals plus reconciliation poll |
| Docker lifecycle | ❌ | ❌ | ✅ | ❌ | Event stream plus reconciliation (below) |
| Docker logs | ❌ | ❌ | ✅ | ❌ | Streaming only |
| System logs | ❌ | ❌ | ✅ | ❌ | journald cursor following |
| Screen/Tmux sessions | ✅ | ❌ | ❌ | ✅ | No event source; polling only |

### 9.1 SMART self-tests are never interval-scheduled

A self-test is a **write operation against the device** that takes minutes to
hours and competes with real I/O. Offering an interval for it would invite
"every 5 minutes", which would keep a disk permanently self-testing.

Self-tests are therefore calendar or manual only, are never scheduled
automatically, and ship **disabled with no default schedule**. An operator who
wants weekly short tests says so explicitly. `dst_ambiguous: "both"` is
rejected for this module (§5.2).

### 9.2 Event-driven modules still reconcile

An event stream can be missed: the Docker daemon restarts, a connection drops,
a buffer overflows. Event-driven modules therefore also run a **reconciliation
poll** on a slow interval (default 5 min) that re-reads full current state and
corrects anything the event stream got wrong.

This is not configurable as "off". An event stream without reconciliation
drifts out of sync and the UI then confidently shows a container as running
after it stopped — which is worse than showing nothing. The reconciliation
interval is configurable within bounds; its existence is not.

---

## 10. Validation

A schedule is rejected unless all of the following hold. Validation runs on the
controller when configuration is saved, and again on the agent before it is
applied ([`AGENT_ARCHITECTURE.md §6.3`](AGENT_ARCHITECTURE.md#63-delivery-and-acknowledgement)).

**Structural**

- `kind` is one of the four, and is permitted for this module (§9).
- Exactly the fields for that kind are present. Unknown fields are rejected
  rather than ignored, so a typo surfaces instead of silently doing nothing.

**Interval**

- `interval_seconds` is a positive integer.
- Not below the module's floor, nor the global floor of 1 s.
- Not above the ceiling (default 30 days).
- `jitter_seconds` is non-negative and less than `interval_seconds`.

**Calendar**

- `timezone` is a known IANA name.
- Every entry in `times` is `HH:MM`, 00:00–23:59, and the list is non-empty
  and free of duplicates.
- `days_of_week` ⊆ 0–6, `days_of_month` ⊆ 1–31, `months` ⊆ 1–12, each without
  duplicates.
- A schedule that can never fire — 30 February, or 31st of a 30-day-only month
  set — is rejected at save time, not discovered by nothing ever happening.
- `dst_ambiguous: "both"` only where the module permits it.

**Cross-cutting**

- The combined configuration must satisfy the host's storage budget
  ([`DATA_RETENTION.md §6`](DATA_RETENTION.md#6-storage-limits)). A schedule
  that is individually legal but collectively unaffordable is rejected with
  the projected volume shown.
- Errors are returned **all at once**, each naming the field and the limit,
  matching the agent's existing startup-validation behaviour.
