# Roadmap

Milestones are small and independently testable. Each one lists an explicit
exit criterion — a demonstrable behaviour, not "work on X". A milestone is not
done until its criterion can be shown working and `make check` passes.

Ordering follows one rule: **each phase must be useful on its own.** Phase 2
alone is already a working multi-host resource monitor. Nothing depends on a
feature three phases away.

No dates. This is a side project; sequence is a commitment, schedule is not.

**Legend:** ✅ done · ⬜ not started. A phase heading without ✅ or ⬜ is in
progress.

**Restructured** for the configurability requirements. The configuration and
scheduling subsystem is now its own phase, inserted as Phase 2 *before* any
collection work, because every module needs it: a module that cannot be
enabled, scheduled and retained from the UI is not shippable under
[`PRODUCT_VISION.md §1`](PRODUCT_VISION.md#1-the-operator-decides-what-matters).
Later phases shifted and new module phases were added. Remote management is
split by risk, with the terminal last.

> The frontend placeholder pages were updated to the new numbering in a
> separate housekeeping commit; the `milestone` prop on `NotImplemented` in
> `apps/web/src/routes/*.tsx` now matches the phases below.

---

## Phase 0 — Foundation ✅

Monorepo, build system, CI, design system, and the skeletons of all three
components. No product features.

| # | Milestone | Exit criterion | Status |
| --- | --- | --- | --- |
| 0.1 | Monorepo and tooling | `make check` runs all three gates and passes | ✅ |
| 0.2 | Backend foundation | `/healthz`, `/readyz`, `/api/v1/info` respond; structured logs; uniform error envelope | ✅ |
| 0.3 | Database connectivity | `make db-up && make migrate` succeeds; `/readyz` reports `ready` | ✅ |
| 0.4 | Frontend foundation | Seven nav sections render; design tokens in place; labelled sample Overview | ✅ |
| 0.5 | Agent foundation | Agent starts, validates config, exits 0 on `SIGTERM` | ✅ |
| 0.6 | CI | Four parallel jobs green on push | ✅ |
| 0.7 | Documentation | Architecture, vision, roadmap, development, security | ✅ |

---

## Phase 1 — Identity and enrolment (in progress)

**Goal:** a real user can log in, and a real agent can enrol. No telemetry yet.

Deliberately before monitoring: ingestion endpoints need an authenticated
identity to attribute data to. Building ingestion first would mean either an
unauthenticated endpoint or reworking attribution later.

| # | Milestone | Exit criterion |
| --- | --- | --- |
| 1.1 | User schema and migration | ✅ `users` table with UUID key, canonical-username uniqueness, role enum, timestamptz timestamps and an `updated_at` trigger; reversible migration `0002_users`; `alembic check` clean. `sessions` moves to 1.4, where it is actually used. |
| 1.2 | Password hashing | ✅ Argon2id via `argon2-cffi` at RFC 9106 low-memory parameters; configurable with enforced floors; rehash detection; fails closed on malformed hashes; NFC-normalized policy that never truncates. Hashing only — the login endpoint moved to 1.3, since it needs sessions and rate limiting. |
| 1.3 | Login endpoint and rate limiting | Login returns a `Secure`/`HttpOnly`/`SameSite` session cookie; attempts are rate-limited, because each costs ~64 MiB and ~50 ms |
| 1.4 | Session middleware | Protected endpoint returns 401 without a session, 200 with one |
| 1.5 | First-run admin setup | Fresh install prompts for an admin account; no default credentials exist |
| 1.6 | Login UI | Login page, session persistence, logout; protected routes redirect |
| 1.7 | Role enforcement | Viewer is refused a write endpoint with a 403 in the standard envelope |
| 1.8 | Host registration | Create a host in the UI; `hosts` table populated |
| 1.9 | Enrolment tokens | One-time token issued, hashed at rest, single-use, expiring |
| 1.10 | Agent enrolment endpoint | `POST /api/v1/agents/enroll` exchanges a one-time token for a credential |
| 1.11 | Agent-side enrolment | Agent enrols on first start, persists its credential, survives restart |
| 1.12 | Heartbeat | Agent heartbeats; UI shows last-seen and marks a silent host **Unknown** |
| 1.13 | Credential revocation | Revoking a host rejects its next call immediately |
| 1.14 | Audit log | Append-only `audit_log`; login, enrolment and revocation recorded |
| 1.15 | Generated API client | TypeScript types generated from OpenAPI (ADR-007); drift fails CI |

**Phase 1 delivers:** a secure multi-host inventory with live up/down status.

---

## Phase 2 — Configuration and scheduling ⬜

**Goal:** an operator can enable a module, schedule it, and see the agent
acknowledge it. No module actually collects anything yet — the plumbing every
later phase depends on.

Deliberately before collection. Building CPU collection first would mean
hardcoding an interval, then retrofitting configuration through it; and the
first module to be made configurable would pay for the whole subsystem.

Specifications: [`SCHEDULING.md`](SCHEDULING.md),
[`AGENT_ARCHITECTURE.md §6`](AGENT_ARCHITECTURE.md#6-configuration-synchronisation).

| # | Milestone | Exit criterion |
| --- | --- | --- |
| 2.1 | Schedule model | `interval`/`calendar`/`event`/`manual` persisted; durations in integer seconds (ADR-019); unit tests for every validation rule |
| 2.2 | Calendar evaluation | Next-run computation with IANA time zones; tests covering spring-forward, fall-back and both DST policies (ADR-020) |
| 2.3 | Config schema | `agent_configs` with per-host monotonic version and checksum; module and capability entries |
| 2.4 | Layer resolution | Global defaults plus per-host overrides resolved server-side; API reports whether each value is inherited or overridden |
| 2.5 | Config fetch endpoint | Agent retrieves its resolved document; scoped by credential so no host can read another's |
| 2.6 | Agent apply and ACK | Atomic write, apply, acknowledge by version **and** checksum |
| 2.7 | Agent reject and NACK | Invalid config keeps the last known good one; NACK lists every problem; rejection surfaced in the UI |
| 2.8 | Drift detection | Heartbeat reports running version; up-to-date / pending / rejected / stale shown per host |
| 2.9 | Capability reporting | Agent reports module availability with reasons; UI explains why a module cannot run here |
| 2.10 | Agent scheduler | Modules scheduled independently; anchored phase so drift does not accumulate; overrun skips rather than queues; counters exposed |
| 2.11 | Jitter | Fleet-wide de-synchronisation; verified with simulated agents |
| 2.12 | Module registry | Internal module interface plus descriptor; one trivial reference module end to end |
| 2.13 | Configuration UI | Enable/disable a module, pick a preset or custom schedule, see projected storage cost |
| 2.14 | Limit enforcement | Interval floors and ceilings enforced on controller and agent; all errors returned at once |

**Phase 2 delivers:** a working configuration and scheduling plane. Useful on
its own only as groundwork, which is why it is small and tightly scoped.

---

## Phase 3 — Core metrics ⬜

**Goal:** real resource data from real hosts, with history. The first phase
that is genuinely useful standalone.

Specifications: [`MONITORING_MODULES.md`](MONITORING_MODULES.md),
[`DATA_RETENTION.md`](DATA_RETENTION.md).

| # | Milestone | Exit criterion |
| --- | --- | --- |
| 3.1 | Metric schema | `metric_series` plus time-partitioned sample tables; partition creation automated and monitored |
| 3.2 | **Storage benchmarks** | All six measurements in [`DATA_RETENTION.md §8`](DATA_RETENTION.md#8-benchmarks-required-before-metric-storage-ships) recorded as repeatable scripts; schema not frozen until they pass |
| 3.3 | Ingestion endpoint | Batched, gzipped, size- and rate-limited; server-side attribution |
| 3.4 | Agent buffering | Survives a controller outage; bounded, oldest-dropped-first, loss counted and reported |
| 3.5 | `cpu` module | `/proc/stat` and `/proc/loadavg`; tests against recorded fixtures |
| 3.6 | `memory` module | `/proc/meminfo`; values match `free -b` in a test |
| 3.7 | `disk_usage` module | Per-filesystem capacity and inodes; pseudo-filesystems excluded |
| 3.8 | `disk_io` module | `/proc/diskstats` as rates; counter resets detected and recorded as gaps, never spikes |
| 3.9 | `network` module | `/proc/net/dev` as rates; same reset handling |
| 3.10 | Hardware inventory | CPU model, core count, total memory, OS release |
| 3.11 | Rollup job | min/max/avg/count/first/last; idempotent; late data recomputes a sealed bucket |
| 3.12 | Retention enforcement | Per-category retention; expiry by partition drop, verified O(1) |
| 3.13 | Storage budget | Projection shown at configuration time; over-budget rejected; hard limit stops ingestion loudly |
| 3.14 | Metrics query API | Resolution selection; response states which tier answered; empty buckets null, never interpolated |
| 3.15 | Hosts page | Real host list with live metrics, replacing the placeholder |
| 3.16 | Host detail page | Per-host charts; downsampled series render as a min–max band, not a single line |
| 3.17 | **Overview page rewrite** | `src/mocks/` **deleted**; Overview reads the real API (ADR-012) |

**Phase 3 delivers:** a configurable multi-host resource monitor with history.

---

## Phase 4 — Sensors and storage health ⬜

**Goal:** the modules that warn about hardware before it fails. Separated from
Phase 3 because they are the ones with real hardware variability and the
external-binary risk.

| # | Milestone | Exit criterion |
| --- | --- | --- |
| 4.1 | External binary invocation | Allow-list, fixed argv, no shell, absolute paths, timeouts, per-device concurrency; adversarial tests confirming no argument injection (ADR-021) |
| 4.2 | `temperature` module | hwmon and thermal zones enumerated with paths and labels; implausible readings marked invalid, not clamped; absent sensors reported as unavailable |
| 4.3 | `smart_attributes` module | SATA, NVMe and SAS handled distinctly; typed attributes with source and device class; USB bridges that pass nothing degrade to unavailable |
| 4.4 | `smart_selftest` module | **Separate capability**, calendar or manual only, no default schedule; refuses to start when a test is already running |
| 4.5 | `raid` module | mdraid from `/proc/mdstat`; degraded member raises an **event**, not just a metric |
| 4.6 | Hardware RAID controllers | At least one vendor family; others report unavailable with a reason |
| 4.7 | Sensor configuration UI | Per-sensor enable, per-category intervals, hardware caveats shown inline |

**Phase 4 delivers:** disk and thermal failure warning — arguably the highest
practical value in the product for a homelab.

---

## Phase 5 — Logs ⬜

**Goal:** searchable logs with faithful preservation of the original.

| # | Milestone | Exit criterion |
| --- | --- | --- |
| 5.1 | Log schema | Time-partitioned `log_entries`; raw message column is never null |
| 5.2 | `system_logs` module | journald with a persistent cursor; no gaps or duplicates after restart |
| 5.3 | File tailing | Follows rotation and truncation correctly |
| 5.4 | Log ingestion endpoint | Batched; rejects oversized payloads with a structured error |
| 5.5 | Level detection | Structured levels normalised; absent level stored as null, never guessed |
| 5.6 | Custom parsing rules | Operator-defined patterns; failed parses retain the raw line and are counted |
| 5.7 | Full-text search | Indexed search returning results in under a second at realistic volume |
| 5.8 | Structured filters | Filter by host, source, level and time range |
| 5.9 | Logs page | Virtualised list, search and filters; messages rendered verbatim |
| 5.10 | WebSocket live tail | Live follow with backpressure; survives reconnect |
| 5.11 | Log retention | Configurable per source; expired partitions dropped |

**Phase 5 delivers:** centralised, searchable, live logs.

---

## Phase 6 — Docker monitoring ⬜

**Goal:** see containers. Controlling them is Phase 7 — reading and acting are
separate capabilities and separate milestones.

| # | Milestone | Exit criterion |
| --- | --- | --- |
| 6.1 | Container schema | `containers`, `container_metrics` |
| 6.2 | Docker API client | Agent talks to the local socket; integration-tested against a real daemon |
| 6.3 | `docker.read` capability | Discovery refused until the capability is enabled for that host |
| 6.4 | Container discovery | Containers appear and disappear in the UI as they start and stop |
| 6.5 | Event stream plus reconciliation | Daemon restart or dropped stream self-corrects within one reconciliation interval |
| 6.6 | Container metrics | Per-container CPU, memory, network and I/O |
| 6.7 | Lifecycle events | Unexpected stops and restart loops recorded as events |
| 6.8 | `docker_logs` module | Container stdout/stderr through the Phase 5 pipeline |
| 6.9 | Docker page | Real container list with state, grouped by host |
| 6.10 | Compose project grouping | Containers grouped by compose project labels |

**Phase 6 delivers:** full Docker visibility, read-only.

---

## Phase 7 — Remote management ⬜

**Goal:** act on hosts — safely, opt-in, audited. The first phase with write
access, so the authorization and audit chain is load-bearing throughout.

The interactive terminal is deliberately **not** here; it is Phase 11.

Specification: [`REMOTE_MANAGEMENT.md`](REMOTE_MANAGEMENT.md).

| # | Milestone | Exit criterion |
| --- | --- | --- |
| 7.1 | Capability schema | Per-host capability flags; default-deny verified by a test asserting a fresh host permits nothing |
| 7.2 | Audit log | Append-only; entry written **before** dispatch; refused attempts audited too |
| 7.3 | Command queue | Persisted, scoped to one host, carrying `expires_at` |
| 7.4 | Command delivery | Agent-initiated WebSocket with long-poll fallback (ADR-018); unknown types rejected; expired commands refused |
| 7.5 | Role enforcement | Viewer refused with 403 in the standard envelope; the attempt is audited |
| 7.6 | Double capability enforcement | Agent refuses a command whose capability is off in its own applied config, even if the controller allowed it |
| 7.7 | `docker.control` | Start / stop / restart end to end from the UI, fully audited |
| 7.8 | Re-authentication | Destructive actions require the password again; a valid session alone is refused |
| 7.9 | `systemd.read` | Unit state via D-Bus introspection |
| 7.10 | `systemd.control` | Per-unit allow-list, empty by default; protective list for `sshd`, `systemd-*`, `dbus`, `network*`; refuses the agent's own unit |
| 7.11 | `sessions.read` | screen and tmux inventory |
| 7.12 | Audit UI | Every action and outcome visible; failures always carry a reason |

**Phase 7 delivers:** user-initiated container and service control, with a
complete audit trail.

> `sessions.control` is intentionally absent. Attaching to an existing session
> inherits that session's privileges, including root, and it may not be worth
> shipping at all — see open question 5 in
> [`ARCHITECTURE.md §14`](ARCHITECTURE.md#14-open-design-questions).

---

## Phase 8 — Notification rules ⬜

**Goal:** precise, testable notifications. The product's differentiator.

| # | Milestone | Exit criterion |
| --- | --- | --- |
| 8.1 | Rule schema | Rules, nested condition groups, conditions (per [ARCHITECTURE §9.1](ARCHITECTURE.md#91-rules-are-a-persisted-tree-not-code)) |
| 8.2 | Condition evaluator | Unit-tested across all match types, including negation |
| 8.3 | Nested AND/OR | Deeply nested groups evaluate correctly; property-based tests |
| 8.4 | Regex safety | Patterns validated and length-capped; evaluation timeout disables the rule with a visible error |
| 8.5 | Event bus | Internal events from logs, containers, hosts, RAID and sensors reach the evaluator |
| 8.6 | Threshold rules | Metric thresholds as rule conditions, so "CPU above 90% for 5 minutes" is user-defined, not hardcoded |
| 8.7 | Ntfy destination | A real notification arrives at a real Ntfy server |
| 8.8 | Webhook destination | Signed payload; delivery retried with backoff |
| 8.9 | SMTP destination | Email delivery with credentials stored encrypted |
| 8.10 | Cooldowns | Flapping container produces one notification; suppressed count reported |
| 8.11 | Rule builder UI | Nested conditions authored visually; invalid rules cannot be saved |
| 8.12 | Dry-run testing | A rule evaluated against pasted or historical events shows what would fire, and why |
| 8.13 | Notification history | Delivery attempts, successes and failures visible |
| 8.14 | Alerts page | Rule management replacing the placeholder |

**Phase 8 delivers:** the full rule engine from the product vision, including
both worked examples.

---

## Phase 9 — Network ⬜

| # | Milestone | Exit criterion |
| --- | --- | --- |
| 9.1 | Device schema | `network_devices`, `device_checks`, `device_relationships` |
| 9.2 | Manual registration | Add a device by IP or hostname; no scanning occurs |
| 9.3 | ICMP checks | Reachability via `CAP_NET_RAW`, not root |
| 9.4 | TCP port checks | Configurable ports; connection closed immediately, no payload |
| 9.5 | Agent-delegated checks | Devices reachable only from an agent's segment are checked by that agent |
| 9.6 | Check scheduling | Uses the shared scheduling model; no bespoke timing logic |
| 9.7 | Availability history | Uptime percentage over a selected range |
| 9.8 | User-defined relationships | Operator declares links between devices |
| 9.9 | Topology view | Visual graph; container relationships surfaced automatically |
| 9.10 | Network page | Device management replacing the placeholder |

---

## Phase 10 — Operability ⬜

**Goal:** make it pleasant to run for a long time.

| # | Milestone | Exit criterion |
| --- | --- | --- |
| 10.1 | Settings UI | Agents, users, retention, capabilities and destinations manageable in the UI |
| 10.2 | Credential rotation | Agent-initiated with a confirm step and an overlap window; stalled rotations surfaced |
| 10.3 | Production container image | Multi-stage build; non-root; health check; published |
| 10.4 | Deployment Compose file | `docker compose up` yields a working install |
| 10.5 | Backup and restore | Documented, tested procedure for database and configuration |
| 10.6 | Agent packaging | `.deb` and `.rpm` with a hardened systemd unit |
| 10.7 | Agent self-update | Opt-in, signature-verified; never automatic by default |
| 10.8 | Upgrade path | Documented, tested version-to-version upgrade |
| 10.9 | Security headers | CSP, HSTS, `X-Frame-Options` on the production image |
| 10.10 | Observability of itself | Control plane exposes its own metrics |

---

## Phase 11 — Remote terminal ⬜

**Goal:** interactive shell access to a host from the web interface.

**Last on purpose.** It is the highest-risk capability in the product and it
depends on everything before it being solid: capabilities (Phase 7), audit
(Phase 7), re-authentication (Phase 7), roles (Phase 1) and the stream
transport (Phase 7). Shipping it earlier would mean building its gates twice.

Specification:
[`REMOTE_MANAGEMENT.md §5`](REMOTE_MANAGEMENT.md#5-remote-terminal) ·
[ADR-022](ARCHITECTURE.md#adr-022-remote-terminal-is-an-opt-in-capability-superseding-the-blanket-shell-exclusion).

| # | Milestone | Exit criterion |
| --- | --- | --- |
| 11.1 | **Security review** | Written review of PTY handling, recording integrity and the `docker.control` interaction, completed **before** implementation starts |
| 11.2 | `terminal` capability | Off by default; Administrator-only; cannot be granted to an Operator |
| 11.3 | Session establishment | Re-authentication required immediately before opening; refused without it |
| 11.4 | PTY handling | Correct resize, signal and encoding behaviour; tested |
| 11.5 | Unprivileged execution | Runs as the agent user; test asserts no `sudo` path and that the agent's own environment is not inherited |
| 11.6 | Timeouts | Idle and absolute timeouts enforced agent-side, not just in the UI |
| 11.7 | Session recording | Full input/output recorded; **no off switch**; integrity verifiable |
| 11.8 | Recording access control | Administrator-only; own retention with a non-zero minimum; deletion audited |
| 11.9 | Live revocation | An administrator terminates a live session; disabling the capability or revoking the credential closes all sessions |
| 11.10 | Terminal UI | Session view that states, before connecting, that the session will be recorded |
| 11.11 | Combination warning | UI states that `terminal` plus `docker.control` on one host is effectively root access |

**Phase 11 delivers:** audited, unprivileged, opt-in interactive access —
replacing the unaudited SSH operators would otherwise keep.

---

## Later, unscheduled

Candidates, not commitments: ZFS and btrfs health modules, GPU utilisation
beyond temperature, `sessions.control` if open question 5 resolves in its
favour, host groups as a configuration layer, per-host role assignment,
Compose file editing, `.rpm`/`.deb` repository hosting, SSO/OIDC, read-only
public status page, log frequency anomaly detection (statistical and
explainable — never AI-generated explanations), mobile-optimised views, agent
support for BSD and macOS.

A **public plugin system** sits here too, and deliberately has no phase. The
module boundary is being kept clean for it, but the questions in
[`PLUGINS.md §4`](PLUGINS.md#4-questions-that-must-be-answered-first) must be
answered before any mechanism is chosen. See
[ADR-024](ARCHITECTURE.md#adr-024-no-plugin-system-but-keep-the-module-boundary-clean).

## Permanently excluded

AI-generated error explanations, automatic root-cause analysis, autonomous
self-healing, automatic remediation, a general-purpose command-execution API,
root or `sudo`-capable execution, capabilities enabled by default, and
unrecorded interactive access.

These are product boundaries, not backlog items. See
[`PRODUCT_VISION.md`](PRODUCT_VISION.md#what-this-is-not).

> **Changed in this revision.** "Unrestricted remote shell execution" was
> previously listed here without qualification. It is now stated precisely: a
> general-purpose command-execution API and root-capable execution remain
> permanently excluded, while an opt-in, unprivileged, recorded interactive
> terminal became Phase 11. The supersession is recorded as
> [ADR-022](ARCHITECTURE.md#adr-022-remote-terminal-is-an-opt-in-capability-superseding-the-blanket-shell-exclusion)
> and explained in
> [`PRODUCT_VISION.md`](PRODUCT_VISION.md#clarification-unrestricted-remote-shell-execution).
