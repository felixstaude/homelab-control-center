# Monitoring Modules

The catalogue of collection modules, what each reads, how it is scheduled, and
what it needs from the host.

> **Status: design.** No module is implemented. The agent currently collects
> nothing. Delivery order is in [`ROADMAP.md`](ROADMAP.md).

- [1. Module model](#1-module-model)
- [2. Everything is off by default](#2-everything-is-off-by-default)
- [3. The catalogue](#3-the-catalogue)
- [4. Collection methods](#4-collection-methods)
- [5. Hardware reality](#5-hardware-reality)
- [6. Recommended defaults](#6-recommended-defaults)
- [7. Adding a module](#7-adding-a-module)

---

## 1. Module model

A module is the unit of configuration, scheduling, and enablement. Each one:

- is **independently enabled**, per host;
- has its own **schedule**, from the shared model in
  [`SCHEDULING.md`](SCHEDULING.md), restricted to the kinds it supports;
- has its own **options** — which mounts, which interfaces, which units;
- has its own **retention**, per [`DATA_RETENTION.md`](DATA_RETENTION.md);
- declares a **minimum interval** the controller enforces;
- reports its **availability** on the host, so the UI can explain why a module
  cannot run here
  ([`AGENT_ARCHITECTURE.md §7`](AGENT_ARCHITECTURE.md#7-capability-reporting));
- fails **independently**. One broken collector must not stop the others, and
  must not stall the agent.

Modules are internal, compiled-in Go packages behind a common interface. They
are *not* plugins, and there is no public extension API — see
[`PLUGINS.md`](PLUGINS.md) for why that line is where it is.

---

## 2. Everything is off by default

A freshly enrolled agent collects **nothing**. It heartbeats, reports its
capabilities, and waits.

This is deliberate and it is the opposite of what most monitoring agents do:

- **Consent.** Temperature, SMART data, systemd unit names and terminal
  session lists are information about someone's machine. The platform should
  not start collecting them because it technically can.
- **Cost is visible.** An operator who enables six modules at 5-second
  intervals sees the projected storage cost at the moment they do it, rather
  than discovering it as a full disk
  ([`DATA_RETENTION.md §6`](DATA_RETENTION.md#6-storage-limits)).
- **Nothing surprising runs.** Nothing shells out to `smartctl` on a host
  until someone asked for SMART monitoring on that host.

The heartbeat is the single exception and is not configurable
([`AGENT_ARCHITECTURE.md §9`](AGENT_ARCHITECTURE.md#9-heartbeat)): an agent
that can be configured into total silence is indistinguishable from a dead
one.

---

## 3. The catalogue

Scheduling kinds are defined in
[`SCHEDULING.md §9`](SCHEDULING.md#9-which-modes-each-module-allows).

| Module | Collects | Source | Kinds | Min interval | Needs |
| --- | --- | --- | --- | --- | --- |
| `cpu` | Per-core and aggregate utilisation, load average | `/proc/stat`, `/proc/loadavg` | interval | 1 s | — |
| `memory` | Used, free, cached, buffers, swap | `/proc/meminfo` | interval | 1 s | — |
| `disk_usage` | Per-filesystem capacity and inodes | `statfs` | interval, calendar, manual | 10 s | — |
| `disk_io` | Per-device reads, writes, queue, latency | `/proc/diskstats` | interval | 1 s | — |
| `temperature` | CPU, GPU, NVMe, drive and board sensors | `/sys/class/hwmon`, `/sys/class/thermal` | interval | 1 s | hwmon drivers |
| `network` | Per-interface bytes, packets, errors, drops | `/proc/net/dev` | interval | 1 s | — |
| `docker` | Container inventory, state, resource usage | Docker Engine API, local socket | event (+reconcile) | — | `docker.read` capability, socket access |
| `docker_logs` | Container stdout/stderr | Docker Engine API log stream | event | — | `docker.read` capability |
| `system_logs` | journald entries | journald, cursor-based | event | — | journal read access |
| `smart_attributes` | Health, reallocated sectors, hours, drive temperature | `smartctl --json` | interval, calendar, manual | 60 s | `smartctl`, device access |
| `smart_selftest` | Short and extended self-test control and results | `smartctl` | **calendar, manual only** | — | as above; **no default schedule** |
| `raid` | Array state, sync progress, failed members | `/proc/mdstat`, controller CLIs | interval, event, manual | 10 s | mdadm or a supported controller |
| `systemd` | Unit active/sub state, restarts, failures | D-Bus | interval, event, manual | 5 s | D-Bus access |
| `sessions` | screen and tmux session inventory | socket directories, `screen -ls` / `tmux ls` | interval, manual | 10 s | matching tooling |

### 3.1 Notes on specific modules

**`disk_io` and `network` are rate counters.** The kernel exposes monotonic
totals; a rate needs two samples. Each sample stores the raw counter *and* the
computed rate, and the agent detects counter resets (reboot, interface
re-creation) and records a gap rather than a spike of several terabytes per
second.

**`docker` is read-only.** It observes. Starting and stopping containers is a
*management capability*, configured separately and off by default — see
[`REMOTE_MANAGEMENT.md`](REMOTE_MANAGEMENT.md). Enabling the monitoring module
does not grant control.

**`docker_logs` and `system_logs` feed the log pipeline**, not the metric
pipeline, and inherit its rules: the original message and timestamp are
preserved verbatim, and a missing severity is stored as absent rather than
guessed ([ARCHITECTURE §7.2](ARCHITECTURE.md#72-design-rules)).

**`sessions` is inventory only** at this layer. Attaching to a session is a
management capability.

**`smart_attributes` and `smart_selftest` are separate modules**, not one
module with a flag. §5.2 explains why.

---

## 4. Collection methods

### 4.1 Pseudo-filesystems: the default, and the preferred one

`cpu`, `memory`, `disk_io`, `network`, `temperature` and `raid` (mdadm) read
`/proc` and `/sys` directly. No subprocess, no parsing of human-readable CLI
output, no dependency. This is why those modules can safely support 1-second
intervals.

### 4.2 Sockets and buses

`docker` uses the Docker Engine API over the **local** Unix socket. The socket
is never exposed on the network, proxied or forwarded — an invariant, not a
setting ([ARCHITECTURE §10.2](ARCHITECTURE.md#102-invariants)).

`systemd` uses D-Bus. Read-only introspection needs no privilege beyond the
agent user; unit *control* is a separate capability.

`system_logs` reads the journal with a persistent cursor, so a restart resumes
without gaps or duplicates.

### 4.3 External binaries — the one genuinely new risk

`smart_attributes`, `smart_selftest`, some `raid` controllers and `sessions`
have no kernel interface. They require invoking a vendor or system tool:
`smartctl`, `nvme`, `mdadm`, `storcli`, `screen`, `tmux`.

This needs stating clearly because it sits very close to something the project
refuses to do. [ARCHITECTURE §10.5](ARCHITECTURE.md#105-explicitly-excluded-permanently)
excludes "arbitrary command or shell execution on hosts". Invoking `smartctl`
is execution of a command. The difference is not a technicality, and the rules
below are what make it a difference:

1. **A fixed allow-list of binaries**, compiled into the agent. Not
   configurable. An operator cannot point a module at a different executable.
2. **Fixed argument vectors.** Each invocation is a constant `argv` template.
   Only an enumerated device path — chosen from devices the agent itself
   discovered — may be substituted.
3. **No shell, ever.** `exec` with an argument slice. No `sh -c`, no string
   concatenation, so there is no quoting or injection surface.
4. **Read-only subcommands only**, except the explicitly separate
   `smart_selftest` module.
5. **Resolved from an absolute path** at a known location, never via `PATH`
   lookup, so a writable directory earlier in `PATH` cannot hijack the call.
6. **Bounded**: timeout, output size cap, and a concurrency limit of one
   invocation per device.
7. **Never root.** The agent runs unprivileged. Where raw device access is
   needed, it comes from a narrow grant (§5.1), not from `sudo`.
8. **Audited at the module level**, with the resolved `argv` recorded.

Recorded as
[ADR-021](ARCHITECTURE.md#adr-021-external-collector-binaries-on-a-fixed-allow-list).
If a module cannot be implemented within these constraints, it does not ship.

---

## 5. Hardware reality

The modules in §4.3 are the ones that behave differently on real hardware.
Designing as though every host has a well-behaved SATA SSD produces a product
that appears broken on half of them.

### 5.1 Drive types

| Type | Tool | Caveats |
| --- | --- | --- |
| SATA | `smartctl -d sat` | Attribute *numbers* are standard; their meanings are vendor-specific. Normalised values are not comparable across vendors. |
| NVMe | `smartctl -d nvme` or `nvme smart-log` | Different model entirely: a health-log structure, not numbered attributes. Temperature may report several sensors. |
| SAS | `smartctl -d scsi` | Log pages, not SMART attributes. Much of what a SATA UI shows does not exist. |
| USB-attached | `smartctl -d sat` with a bridge hint | Many bridges do not pass SMART through at all. Must degrade to "unavailable", never to "healthy". |
| Behind hardware RAID | controller-specific | Often invisible to `smartctl`; needs the vendor CLI, if one exists for that controller. |

Consequence for the data model: SMART data is stored as **typed attributes
with their source and device class**, not as a fixed set of columns modelled on
SATA. A UI that shows "Reallocated Sector Count: n/a" for every NVMe drive is
one operators stop believing.

### 5.2 Attribute polling and self-tests are different things

| | `smart_attributes` | `smart_selftest` |
| --- | --- | --- |
| Operation | Read | **Write** — instructs the device to test itself |
| Duration | Milliseconds | Minutes (short) to many hours (extended) |
| Cost | Negligible | Competes with real I/O; measurable latency impact |
| Scheduling | interval, calendar, manual | **calendar or manual only** |
| Default | Off, 6-hour preset available | Off, **no default schedule at all** |
| Risk if over-scheduled | None | A device permanently self-testing |

They are separate modules precisely so that no path exists by which enabling
SMART *monitoring* starts SMART *testing*. The requirement is explicit: disk
self-tests are never scheduled automatically.

The agent also refuses to start a self-test while one is already running on
that device, and surfaces the in-progress test instead of queueing another.

### 5.3 Temperature sensors

- Sensor naming is inconsistent across platforms. `coretemp`, `k10temp`,
  `acpitz`, `nvme`, `amdgpu` and vendor-specific names all appear.
- Labels may be absent, duplicated, or wrong. The agent reports the hwmon path
  alongside any label so an operator can disambiguate "temp1" from "temp1".
- Many virtualised hosts expose **no** thermal sensors. A VPS is the common
  case here, and the module must report unavailable with that reason.
- Some sensors return implausible values (0 °C, 128 °C) when a device is
  asleep. Readings outside a plausible range are recorded as **invalid**, not
  clamped and not silently dropped — a graph that quietly hides bad readings
  hides a failing sensor.
- Spinning disks: reading temperature may wake a sleeping drive. The module
  therefore defaults to 30 s for drives rather than 5 s, and documents that
  frequent polling prevents spin-down.

### 5.4 RAID

- **Linux mdraid** reads from `/proc/mdstat` plus `sysfs`. No subprocess
  needed.
- **Hardware controllers** (LSI/Broadcom, Adaptec, HP Smart Array) each need a
  vendor CLI with its own output format and licensing. Support is added per
  controller family, as separate work, and is reported unavailable until then.
- **ZFS and btrfs** are not "RAID" in this module's sense and get their own
  future modules. Treating `zpool status` as RAID status would misrepresent
  both.
- A **degraded array is an event, not a metric.** Sync progress is a metric;
  "member failed" must raise an event the notification engine can match, not
  wait for someone to look at a graph.

### 5.5 Missing and unsupported

Any module may report, per host: available; unavailable with a reason;
partially available (three of four drives support SMART). The UI shows the
reason at the point of configuration.

An unavailable module is never silently enabled-but-empty. "No data because
this host has no thermal sensors" and "no data because collection is broken"
must be distinguishable — the same principle as a host that has not reported
showing **Unknown** rather than 0%.

---

## 6. Recommended defaults

Presets offered when a module is enabled. Every one is adjustable within the
module's floor and the host's storage budget.

| Module | Suggested preset | Reasoning |
| --- | --- | --- |
| CPU temperature | 5 s | Thermal events are fast; a throttling spike is invisible at 60 s. |
| GPU temperature | 5 s | As above. |
| Drive temperature | 30 s | Changes slowly, and polling can prevent spin-down (§5.3). |
| CPU utilisation | 10 s | Balance between seeing spikes and storage cost. |
| Memory | 10 s | |
| Disk I/O | 10 s | |
| Network | 10 s | |
| Disk usage | 5 min | Capacity moves slowly. |
| SMART attributes | 6 h | Attributes change over weeks. Polling faster is cost without information. |
| **SMART self-test** | **none** | Never scheduled automatically (§5.2). |
| RAID status | 60 s | Fast enough to notice a degraded array promptly; the event path covers the rest. |
| Systemd services | 30 s poll + events | Events give latency; the poll reconciles. |
| Sessions | 60 s | Inventory, not telemetry. |
| Docker | event + 5 min reconcile | [`SCHEDULING.md §9.2`](SCHEDULING.md#92-event-driven-modules-still-reconcile) |

Shorter intervals are permitted where the module's floor allows and the host
can sustain them. The 1-second floor on `/proc`-based modules is real and
usable; it is not the default because few operators need per-second CPU
history and all of them pay for it in storage.

---

## 7. Adding a module

Internal modules are added by implementing a Go interface in the agent and a
matching controller-side descriptor. The checklist exists so that new modules
inherit the guarantees rather than reinventing them:

1. **Declare the descriptor**: name, permitted scheduling kinds, minimum
   interval, options schema, metric categories produced.
2. **Default to disabled**, with no schedule.
3. **Implement availability detection** with a human-readable reason on
   failure.
4. **Honour context cancellation**, so shutdown and the execution timeout both
   work.
5. **Fail in isolation** — return an error, never panic, never block the
   scheduler.
6. **Add retention defaults** for every metric category produced.
7. **Register the schedule-kind restriction** so the controller rejects
   invalid configurations at save time.
8. **Document hardware caveats here**, especially the unavailable cases.
9. If an external binary is needed, satisfy every rule in §4.3 or do not ship
   the module.

The interface is internal and may change freely. That is the point of not
publishing it yet — see [`PLUGINS.md`](PLUGINS.md).
