# Historical Data and Retention

How collected metrics are stored, aggregated, aged out and queried.

> **Status: design.** No metric storage exists. The only table today is
> `users`. Delivery is tracked in [`ROADMAP.md`](ROADMAP.md).

- [1. Shape of the data](#1-shape-of-the-data)
- [2. Per-category retention](#2-per-category-retention)
- [3. Downsampling](#3-downsampling)
- [4. Storage layout](#4-storage-layout)
- [5. Query path](#5-query-path)
- [6. Storage limits](#6-storage-limits)
- [7. Expected volume](#7-expected-volume)
- [8. Benchmarks required before metric storage ships](#8-benchmarks-required-before-metric-storage-ships)
- [9. When to revisit the database choice](#9-when-to-revisit-the-database-choice)

---

## 1. Shape of the data

A **series** is one measurable stream: `(host, module, metric, labels)` — for
example `nas-01 / temperature / celsius / sensor=coretemp,zone=package`.

A **sample** is one `(series, timestamp, value)`.

Series metadata lives in its own table, so a sample row references a small
integer series id rather than repeating label strings. At 5 samples per second
per host, repeating `"sensor=coretemp"` on every row is the difference between
a manageable table and an unmanageable one.

Samples are narrow — one row per series per timestamp — rather than wide rows
with a column per metric. Narrow rows are the right fit because the module set
is configurable: a wide table would need a schema migration whenever someone
enables a module, and would store nulls for every module a host does not run.

---

## 2. Per-category retention

Retention is configured **per metric category**, not globally, because the
categories have genuinely different value over time.

| Category | Default raw | Default 5-min | Default 1-hour | Why |
| --- | --- | --- | --- | --- |
| CPU, memory, load | 7 days | 90 days | 2 years | Spikes matter recently; trends matter long-term. |
| Disk I/O, network | 7 days | 90 days | 2 years | As above. |
| Temperature | 7 days | 1 year | 5 years | Long history is how you spot a fan degrading or dust accumulating over seasons. |
| Disk usage | 30 days | 1 year | 5 years | Low volume, and capacity forecasting needs a long baseline. |
| SMART attributes | 2 years | — | — | Already low-frequency; the raw values *are* the trend. Downsampling a reallocated-sector count would destroy its meaning. |
| RAID state | 90 days | — | — | State, not a measurement. |
| Systemd unit state | 30 days | — | — | State; the event log carries the history that matters. |
| Session inventory | 7 days | — | — | Operational curiosity, not history. |

Every value is operator-configurable within enforced bounds. Each tier may
also be disabled: an operator who wants raw data for 30 days and no rollups at
all may have that, and will be shown what it costs.

**Counter-style attributes are never averaged.** SMART attributes, and any
monotonic counter, keep their raw values. An averaged "power-on hours" is
meaningless, and an averaged reallocated-sector count hides the step changes
that are the entire diagnostic signal.

---

## 3. Downsampling

A rollup row covers one bucket of one series and stores:

| Field | Why it exists |
| --- | --- |
| `min` | Preserves the dip. |
| `max` | **Preserves the spike.** |
| `avg` | The typical value. |
| `count` | Distinguishes a full bucket from one with two samples in it. |
| `first`, `last` | Needed for counter-derived series and for step rendering. |

### 3.1 Why min and max are not optional

A 5-second CPU temperature series rolled up to a 5-minute average loses a
30-second thermal spike completely — the exact event the operator enabled
5-second sampling to catch. Storing only an average would mean the data
answers questions for a week and then quietly stops answering them.

So the UI must render downsampled series as a **min–max band with the average
inside it**, not as a single line. A single line drawn from averages is a
graph that lies about what happened, and a monitoring tool that lies about the
past is worse than one with a shorter history.

`count` is what makes a partially-filled bucket visible rather than
indistinguishable from a full one — an agent that was offline for four of five
minutes must not produce a bucket that looks authoritative.

### 3.2 How rollups are produced

A scheduled job on the controller, using the shared scheduling model
([`SCHEDULING.md`](SCHEDULING.md)), aggregates completed buckets only. A
bucket is never rolled up while samples could still arrive for it — an agent
reconnecting after an outage backfills with original timestamps
([`AGENT_ARCHITECTURE.md §8`](AGENT_ARCHITECTURE.md#8-offline-behaviour)), so
the job waits a configurable lateness grace period (default 10 minutes) before
sealing a bucket.

Late data arriving after a bucket is sealed marks that bucket for
recomputation rather than being dropped. Dropping it would make the rollup
silently disagree with the raw data it came from.

Rollups are **idempotent**: recomputing a bucket produces the same result, so
a job that is interrupted and re-run cannot double-count.

---

## 4. Storage layout

Plain PostgreSQL, time-partitioned. No time-series database — see ADR-003 and
§9.

```
metric_series                      small, one row per series
  id, host_id, module, metric, labels (jsonb), unit, created_at

metric_samples_raw                 PARTITION BY RANGE (recorded_at)
  series_id, recorded_at, value        → daily partitions

metric_samples_5m                  PARTITION BY RANGE (bucket_start)
  series_id, bucket_start, min, max, avg, count, first, last
                                       → weekly partitions

metric_samples_1h                  PARTITION BY RANGE (bucket_start)
  series_id, bucket_start, min, max, avg, count, first, last
                                       → monthly partitions
```

**Why partitioning carries the retention mechanism.** Expiring data is
`DROP TABLE` on a partition: near-instant, reclaims disk immediately, and
creates no bloat. A `DELETE FROM … WHERE recorded_at < …` on hundreds of
millions of rows holds locks, generates enormous WAL, leaves dead tuples
behind, and then needs a `VACUUM` that competes with ingestion. Retention is
the single most predictable workload in the system, and it must not be the one
that degrades the database.

Partition granularity differs per tier so that each has a useful number of
partitions: daily for raw (7–30 live partitions), weekly for 5-minute,
monthly for hourly. Partitions are created ahead of time by the maintenance
job; a missing future partition would reject inserts, so the job creates
several periods in advance and the gap is monitored.

Primary key `(series_id, recorded_at)` — the query pattern is always "this
series over this time range", so clustering by series first makes the common
query a single index range scan.

---

## 5. Query path

### 5.1 Resolution is chosen by the query, not the caller

A range query names a time window and a target point count. The API selects
the coarsest tier that satisfies the request:

```
window ≤ 6 hours          → raw
window ≤ 7 days           → raw if still retained, else 5-minute
window ≤ 90 days          → 5-minute
window > 90 days          → 1-hour
```

The response states **which tier answered it**, and the UI shows that. "This
is hourly data" is information the operator needs in order to interpret a
flat-looking graph; silently serving a different resolution than expected is
how people draw wrong conclusions.

### 5.2 Bounded results

Every query has a maximum point count. A request for a year of raw data is not
served by returning 6 million points to a browser; it is served from the
hourly tier, or rejected with a structured error naming the limit. This is the
same bounding principle applied to log ingestion
([ARCHITECTURE §7.4](ARCHITECTURE.md#74-bounds)).

### 5.3 Gaps stay gaps

A query response distinguishes "no sample in this bucket" from "a sample whose
value is zero". The API returns nulls for empty buckets and never interpolates
([principle 2](PRODUCT_VISION.md#2-never-invent-information)).

---

## 6. Storage limits

Retention is configurable, so the configuration can be made unaffordable. The
platform therefore makes cost visible before it is incurred and enforces a
ceiling.

### 6.1 Projection at configuration time

When a module's schedule or retention changes, the controller computes the
projected steady-state size from series count, interval and retention, and
shows it in the UI next to the setting. An operator moving CPU sampling from
10 s to 1 s sees the tenfold increase at the moment of the decision rather
than six weeks later.

### 6.2 A storage budget

The deployment has a configurable budget (default: 10 GB, or 20% of the
filesystem, whichever is smaller). The controller:

- rejects a configuration change whose **projection exceeds the budget**,
  naming the projected figure and the budget
  ([`SCHEDULING.md §10`](SCHEDULING.md#10-validation));
- warns at 80% of **actual** usage;
- at 100%, stops accepting new samples for the categories over budget and
  raises a notifiable event.

### 6.3 Over-budget behaviour is explicit, not silent

At the hard limit the platform **stops ingesting and says so loudly**. It does
not:

- silently drop samples — that produces gaps nobody can explain;
- silently shorten retention — that destroys data the operator asked to keep;
- grow until the filesystem fills — a monitoring tool must never be the cause
  of the outage it is meant to detect.

Refusing to ingest is a visible, recoverable failure. The other three are
invisible ones.

### 6.4 Enforced floors and ceilings

| Limit | Value | Reason |
| --- | --- | --- |
| Minimum interval, global | 1 s | Below this, collection cost dominates the measurement. |
| Minimum interval, per module | see [`MONITORING_MODULES.md`](MONITORING_MODULES.md#3-the-catalogue) | Some collectors are genuinely expensive. |
| Maximum interval | 30 days | Beyond this it is not monitoring. |
| Minimum retention | 1 hour | Shorter makes the data useless for investigation. |
| Maximum raw retention | 1 year | Hard ceiling regardless of budget; use rollups. |
| Maximum points per query | 50,000 | Protects the browser and the database. |

---

## 7. Expected volume

Figures below assume ~100 bytes per raw row (tuple header, columns, index
overhead) and ~140 bytes per rollup row. Rounded, and intended to be checked
against the benchmarks in §8 rather than trusted.

Reference host: 8 cores, 2 drives, 2 network interfaces, 3 mounted
filesystems, 20 monitored systemd units — **103 series** at the recommended
defaults from [`MONITORING_MODULES.md §6`](MONITORING_MODULES.md#6-recommended-defaults).

### 7.1 At the default presets

| Measure | Per host |
| --- | --- |
| Raw rows/second | ~5 |
| Raw rows/day | ~434,000 |
| Raw bytes/day | ~43 MB |
| Raw at 7-day retention | ~300 MB |
| 5-minute tier at 90 days | ~375 MB |
| 1-hour tier at 2 years | ~250 MB |
| **Steady-state total** | **~0.9 GB** |

Ten hosts: **~9 GB** at steady state. Entirely comfortable for PostgreSQL on a
homelab machine.

### 7.2 Driven to the floor

If an operator sets ~80 of those series to 1-second sampling:

| Measure | Per host |
| --- | --- |
| Raw rows/second | ~81 |
| Raw bytes/day | ~700 MB |
| Raw at 7-day retention | ~4.9 GB |

Ten hosts: **~49 GB** of raw data alone. Legal, sustainable on adequate
hardware, and exactly why §6 exists.

### 7.3 Where it stops being reasonable

Twenty hosts at the floor is ~1,600 rows/second and ~14 GB/day.

The honest observation: **1,600 rows/second is not an ingestion problem.**
Batched inserts at that rate are unremarkable for PostgreSQL. The problems at
that scale are *storage growth* and *query performance over billions of rows*,
which partition pruning and the rollup tiers address directly. This is why
ADR-003 holds, and §9 names the condition that would overturn it.

---

## 8. Benchmarks required before metric storage ships

The figures in §7 are arithmetic, not measurements. The core metrics phase
(Phase 3) does not ship without these numbers, measured on representative hardware (a mid-range
homelab machine with a SATA SSD, not a cloud instance with provisioned IOPS):

1. **Ingestion throughput.** Batched insert rate at which commit latency
   exceeds 100 ms, with and without indexes, at 10M and 100M existing rows.
2. **Query latency** for the four windows in §5.1, at 10M, 100M and 500M rows,
   with partition pruning verified by `EXPLAIN` — a query plan that scans every
   partition is the failure mode to catch early.
3. **Rollup job duration** for one day of raw data at the §7.2 rate, and its
   measured impact on concurrent ingestion.
4. **Partition drop time** and reclaimed space, confirming retention is O(1)
   rather than proportional to row count.
5. **On-disk bytes per row**, measured, to replace the 100-byte estimate.
6. **Behaviour at the budget ceiling** (§6.3): verify ingestion stops cleanly,
   the event fires, and the database stays healthy.

Each benchmark is a committed, repeatable script, so that the numbers can be
re-measured after a schema change rather than quoted from memory.

---

## 9. When to revisit the database choice

ADR-003 chose one PostgreSQL and no dedicated time-series database, because
every additional stateful service is one the operator must back up, upgrade
and debug alone. That reasoning is unchanged.

The decision should be reopened if, **measured, not assumed**:

- partition pruning stops keeping §5.1 queries under ~1 s at realistic volume;
- the rollup job cannot keep up with ingestion on reference hardware;
- on-disk size exceeds roughly twice the §7 projection, making the storage
  budget unworkable;
- compression becomes necessary to fit a normal fleet on a normal disk.

The first step would be **TimescaleDB**, which is a PostgreSQL *extension* —
hypertables and continuous aggregates replace most of §3 and §4 with less
code, and `alembic/env.py` already carries the `include_object` hook needed to
keep Timescale-managed objects out of autogenerate. That is a change of
extension, not a migration to a separate system, and it preserves the
one-stateful-service property.

Adopting a separate TSDB (Prometheus, VictoriaMetrics, InfluxDB) would mean a
second datastore to operate and would re-raise the question the product exists
to answer. It is the last resort, not the first.
