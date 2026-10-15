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

### 3.2 What is implemented today

The table above is the designed catalogue across the whole product. **Ten
modules exist**, and this is them — generated from
`contracts/monitoring-modules.json`, which is produced from the server's
registry and drift-checked by `make check-contract`, so this table cannot
quietly stop matching the code.

| Id | Name | Metrics | Kinds | Default | Floor | Needs a grant |
| --- | --- | --- | --- | --- | --- | --- |
| `cpu` | CPU utilisation | `utilisation`, `core_utilisation` (by core), `logical_cpus` | interval | 10 s | 1 s | no |
| `memory` | Memory | `utilisation`, `total`, `available`, `used` | interval | 10 s | 1 s | no |
| `swap` | Swap | `utilisation`, `total`, `used` | interval | 10 s | 1 s | no |
| `load` | Load average | `average` (by window) | interval | 30 s | 5 s | no |
| `uptime` | System uptime | `seconds` | interval | 300 s | 60 s | no |
| `filesystem` | Filesystem usage | `total`, `used`, `available`, `utilisation`, `inodes_utilisation`, `readonly` (all by mount) | interval | 60 s | 10 s | no |
| `diskio` | Disk throughput | `read_bytes`, `write_bytes`, `read_ops`, `write_ops`, `utilisation` (all by device) | interval | 10 s | 1 s | no |
| `network` | Network traffic | `receive_bytes`, `transmit_bytes`, `receive_packets`, `transmit_packets`, `receive_errors`, `transmit_errors`, `receive_drops`, `transmit_drops`, `up`, `speed` (all by interface) | interval | 10 s | 1 s | no |
| `sensors` | Temperatures and fans | `temperature`, `fan_speed` (by sensor) | interval | 30 s | 5 s | no |
| `docker` | Containers | `containers_total`, `containers_running`, and by container: `running`, `cpu_utilisation`, `memory_bytes`, `memory_limit`, `memory_utilisation`, `receive_bytes`, `transmit_bytes`, `restart_count`, `uptime_seconds`, `healthy` | interval | 15 s | 5 s | **yes** |
| `systemd` | Services | `units_failed`, and by unit: `active`, `failed`, `restarts`, `enabled`, `uptime_seconds` | interval | 60 s | 15 s | no |

### What each of the newer ones deliberately does not report

Every exclusion is a decision, and each is the difference between a useful
list and one nobody reads.

**`filesystem` is capacity, not disk size.** `statfs` reports what a
filesystem holds, which is smaller than its block device (metadata, the
reserved-blocks percentage) and on LVM or btrfs can span several. Excluded:
pseudo-filesystems, `tmpfs` and `ramfs` (they are memory — reporting `/run`
as a disk invents one), `overlay` (container roots, dozens of them, all
showing the host's free space), `squashfs` (read-only, therefore permanently
100% full and permanently alarming), and **network filesystems** — that last
one not for noise but because `statfs` on a mount whose server has gone away
blocks in uninterruptible sleep and no context deadline cancels a syscall in
D state.

Duplicates are removed twice over: by device number, which catches bind
mounts, and by source device, which catches btrfs subvolumes — those get
their own anonymous device numbers, so a host with `/` and `/home` on one
pool would otherwise report its free space twice. **ZFS datasets are not
caught** and are reported once per dataset; their source is `pool/dataset`
rather than a device path.

**`diskio` is whole devices only.** A device appears when `/sys/block/<name>`
exists, which excludes partitions — their I/O is already counted on the disk
containing them. Loop, ram and zram devices are excluded. Device-mapper and
md devices are **included**, because LVM and RAID are what a homelab's
storage usually is.

**`network` excludes what is already counted elsewhere**: loopback, anything
enslaved to a bridge or bond (its traffic appears on the master, which is
also what makes a bridged VM host come out right — the slave is dropped and
`br0` is kept), and container plumbing by name (`veth*`, `docker0`, `br-*`,
`cni*`, `virbr*`, `vnet*`). `HCC_AGENT_NETWORK_INTERFACES` names an exact set
and replaces the rule entirely.

**`sensors` reads `/sys/class/hwmon` and not `/sys/class/thermal`.** The
thermal zone interface looks like extra coverage and is mostly a second view
of the same silicon — on the machine this was developed against,
`thermal_zone0` and `hwmon5` resolve to the same device path, and
`thermal_zone1` is `x86_pkg_temp`, which is the sensor `coretemp` already
reports as "Package id 0" through a different device. A zone's `type` is also
a vendor string with no reliable mapping to a component, so calling one "CPU"
would be a guess. The cost: a host with thermal zones and no hwmon reports no
temperatures, and says so.

Sensor identity is the chip's `name` plus the sensor's `label`, never the
hwmon number — those are assigned in probe order and change between boots,
so a series keyed on one would silently continue a chart with another chip's
readings. The device basename disambiguates two chips with the same name,
which is not hypothetical: a machine with two NVMe drives has two chips
called `nvme`, each reporting `Composite`.

### Nothing is summed across entries, anywhere

Stacked storage reports the same bytes at every layer: a write to a
filesystem on LVM on LUKS on a partition appears in `dm-1`, `dm-0` and
`sda`. A packet leaving a container is counted on its veth, its bridge and
the physical NIC. So there is **no aggregate metric** in any of these
modules and the interface charts per entry — a "total disk throughput" would
be inflated by however many layers each host happens to have, which is a
number nobody can see from the interface.

The one place network counters *are* added together is a container's own
interfaces, in `docker`, because a container's interfaces are its own and do
not overlap.

### `systemd` is the one module that runs a command

systemd has no pseudo-file interface. The alternatives are its D-Bus API,
which means a D-Bus implementation inside an agent whose whole point is being
one static binary, or `systemctl` — which is a command, and
[ARCHITECTURE §10.5](ARCHITECTURE.md#105-explicitly-excluded-permanently)
excludes arbitrary command execution on hosts.

[§4.3](#43-external-binaries--the-one-genuinely-new-risk) is where the eight
rules that make that a difference were written down, before any module needed
them. This module satisfies all of them: `/usr/bin/systemctl` or
`/bin/systemctl`, compiled in and resolved absolutely rather than through
`PATH`; fixed argument vectors with only validated unit names substituted; no
shell; `list-units` and `show` only; a timeout, an output cap and one
invocation at a time; no root; and `LC_ALL=C`, so the bytes are the same on
every machine.

It also uses `--output=json`, which is a *structured* interface. That is the
difference between this and parsing `df`: systemd's JSON does not move
between releases, and a systemd too old to produce it (before 246) is a clear
refusal naming the version rather than a column-counting guess.

`systemctl show` is asked for **six named properties**. Without
`--property`, it prints around two hundred fields per unit including
`Environment`, which can hold secrets — asking for six is both cheaper and
the difference between a monitoring agent and an exfiltration tool.

**An explicit list is required for per-unit series**, from
`HCC_AGENT_SYSTEMD_UNITS`. A host has hundreds of units and collecting all of
them would be a cardinality decision made on the operator's behalf. Without a
list the module reports only `units_failed` — cardinality one, no
configuration, and the first question systemd can answer: is anything broken
on this machine.

A unit the operator named and systemd does not have is **absent**, not
inactive. `systemctl show` answers for a unit it has never heard of, with
`LoadState=not-found`, so a typo would otherwise become a series permanently
reporting a stopped service that does not exist — with a restart count of
zero to make it look measured.

### `docker` is the one module that needs a privilege grant

**Access to the Docker socket is equivalent to root on the host.** Anything
that can talk to it can start a privileged container with the host
filesystem mounted. So the capability is granted *locally and explicitly*,
and the controller can only decide whether to use one the host has already
given. Two switches, both of which must be on:

1. **The host administrator** sets `HCC_AGENT_DOCKER_SOCKET=/var/run/docker.sock`
   in `/etc/hcc-agent/agent.env` and makes the agent account able to read
   that socket. There is no default path; a compromised controller cannot do
   this.
2. **An operator** enables the `docker` module for the host, like any other.

Unset, the module reports itself unavailable with a reason naming the
variable and saying what granting it means. The agent issues only
`GET /containers/json`, `GET /containers/{id}/json` and
`GET /containers/{id}/stats` — there is no start, no stop, no exec, and a
test asserts that the only HTTP method the daemon ever sees is `GET`.

The **controller** never touches a Docker socket. It stores numbers an agent
sent it, exactly as it does for CPU.

Three differences from §3 worth naming rather than leaving to be noticed:

* **Load average is its own module, not part of `cpu`.** §3 lists
  `/proc/loadavg` under `cpu`. It is separate because its floor is different —
  five seconds, not one — and the kernel's load average is already a smoothed
  value over one, five and fifteen minutes, so sampling it faster measures the
  smoothing rather than the machine. One module cannot have two floors.
* **`swap` is separate from `memory`**, where §3 folds it in. A host with no
  swap has no swap usage, and that is not zero: the module reports **no
  samples** on such a host and the interface renders an absence. Folded into
  `memory`, that distinction would have had nowhere to live —
  [ADR-048](ARCHITECTURE.md#adr-048-five-collection-modules-in-the-first-delivery-not-two).
* **`uptime` is new**, not in §3 at all. It is the only counter rather than
  gauge in this set and the only one whose natural interval is minutes, which
  is what proves the schedule is per-module rather than per-host.

§3's `disk_usage`, `disk_io` and `temperature` are implemented under the
names `filesystem`, `diskio` and `sensors`. The names changed for the reasons
above: "disk usage" conflates filesystem capacity with disk size, and
"temperature" would not cover the fan readings that come from the same
sysfs tree.

Everything else in §3 — the log modules, SMART, `raid`, `sessions` — **does
not exist yet**. The interface lists exactly the ten above, so a module that
is absent is absent rather than appearing with no data.

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
