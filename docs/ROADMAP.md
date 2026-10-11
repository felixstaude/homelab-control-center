# Roadmap

Milestones are small and independently testable. Each one lists an explicit
exit criterion — a demonstrable behaviour, not "work on X". A milestone is not
done until its criterion can be shown working and `make check` passes.

Ordering follows one rule: **each phase must be useful on its own.** Phase 2
alone is already a working multi-host resource monitor. Nothing depends on a
feature three phases away.

No dates. This is a side project; sequence is a commitment, schedule is not.

**Legend:** ✅ done · ⬜ not started

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

## Phase 1 — Identity and enrolment ⬜

**Goal:** a real user can log in, and a real agent can enrol. No telemetry yet.

Deliberately before monitoring: ingestion endpoints need an authenticated
identity to attribute data to. Building ingestion first would mean either an
unauthenticated endpoint or reworking attribution later.

| # | Milestone | Exit criterion |
| --- | --- | --- |
| 1.1 | User schema and migration | `users`, `roles`, `sessions` tables; `alembic check` clean |
| 1.2 | Password authentication | Argon2id hashing; login returns a `Secure`/`HttpOnly`/`SameSite` session cookie |
| 1.3 | Session middleware | Protected endpoint returns 401 without a session, 200 with one |
| 1.4 | First-run admin setup | Fresh install prompts for an admin account; no default credentials exist |
| 1.5 | Login UI | Login page, session persistence, logout; protected routes redirect |
| 1.6 | Role enforcement | Viewer is refused a write endpoint with a 403 in the standard envelope |
| 1.7 | Host registration | Create a host in the UI; `hosts` table populated |
| 1.8 | Enrolment tokens | One-time token issued, hashed at rest, single-use, expiring |
| 1.9 | Agent enrolment endpoint | `POST /api/v1/agents/enroll` exchanges a one-time token for a credential |
| 1.10 | Agent-side enrolment | Agent enrols on first start, persists its credential, survives restart |
| 1.11 | Heartbeat | Agent heartbeats; UI shows last-seen and marks a silent host **Unknown** |
| 1.12 | Credential revocation | Revoking a host rejects its next call immediately |
| 1.13 | Audit log | Append-only `audit_log`; login, enrolment and revocation recorded |
| 1.14 | Generated API client | TypeScript types generated from OpenAPI (ADR-007); drift fails CI |

**Phase 1 delivers:** a secure multi-host inventory with live up/down status.

---

## Phase 2 — Host metrics ⬜

**Goal:** real resource data from real hosts, with history.

| # | Milestone | Exit criterion |
| --- | --- | --- |
| 2.1 | Metric schema | Time-partitioned `host_metrics`; partition creation automated |
| 2.2 | CPU collection | Agent reads `/proc/stat`; unit tests against recorded fixtures |
| 2.3 | Memory and swap | `/proc/meminfo`; values match `free -b` in a test |
| 2.4 | Disk | Per-mount usage and I/O; excludes pseudo-filesystems |
| 2.5 | Network | Per-interface counters from `/proc/net/dev`, as rates |
| 2.6 | Load and uptime | `/proc/loadavg`, `/proc/uptime` |
| 2.7 | Hardware inventory | CPU model, core count, total memory, OS release |
| 2.8 | Ingestion endpoint | Batched, gzipped, size- and rate-limited; server-side attribution |
| 2.9 | Agent batching and buffering | Agent buffers through a controller outage and resumes without data loss |
| 2.10 | Metrics query API | Range queries with resolution selection |
| 2.11 | Hosts page | Real host list with live metrics, replacing the placeholder |
| 2.12 | Host detail page | Per-host charts over selectable ranges |
| 2.13 | **Overview page rewrite** | `src/mocks/` **deleted**; Overview reads the real API (ADR-012) |
| 2.14 | Downsampling job | 5-minute and hourly rollups produced on schedule |
| 2.15 | Retention enforcement | Expired partitions dropped; retention configurable per tier |

**Phase 2 delivers:** a working multi-host resource monitor with history. This
is the first phase that is genuinely useful standalone.

---

## Phase 3 — Logs ⬜

**Goal:** searchable logs with faithful preservation of the original.

| # | Milestone | Exit criterion |
| --- | --- | --- |
| 3.1 | Log schema | Time-partitioned `log_entries`; raw message column is never null |
| 3.2 | journald collection | Agent reads journald with a persistent cursor; no gaps or duplicates after restart |
| 3.3 | File tailing | Follows rotation and truncation correctly |
| 3.4 | Log ingestion endpoint | Batched; rejects oversized payloads with a structured error |
| 3.5 | Level detection | Structured levels normalised; absent level stored as null, never guessed |
| 3.6 | Custom parsing rules | Operator-defined patterns; failed parses retain the raw line and are counted |
| 3.7 | Full-text search | Indexed search returning results in under a second at realistic volume |
| 3.8 | Structured filters | Filter by host, source, level and time range |
| 3.9 | Logs page | Virtualised list, search and filters; messages rendered verbatim |
| 3.10 | WebSocket live tail | Live follow with backpressure; survives reconnect |
| 3.11 | Log retention | Configurable; expired partitions dropped |

**Phase 3 delivers:** centralised, searchable, live logs.

---

## Phase 4 — Docker ⬜

**Goal:** see and control containers. The first phase with write access to
hosts, so the authorization and audit chain is load-bearing here.

| # | Milestone | Exit criterion |
| --- | --- | --- |
| 4.1 | Container schema | `containers`, `container_metrics` |
| 4.2 | Docker API client | Agent talks to the local socket; integration-tested against a real daemon |
| 4.3 | Container discovery | Containers appear and disappear in the UI as they start and stop |
| 4.4 | Container metrics | Per-container CPU, memory, network and I/O |
| 4.5 | Lifecycle events | Unexpected stops and restart loops recorded as events |
| 4.6 | Container logs | Container stdout/stderr through the Phase 3 pipeline |
| 4.7 | Docker page | Real container list with state, grouped by host |
| 4.8 | Command queue | Commands persisted, scoped to one host, with `expires_at` |
| 4.9 | Command polling | Agent polls, rejects unknown types, refuses expired commands |
| 4.10 | Start / stop / restart | End-to-end from the UI, fully audited |
| 4.11 | Authorization enforcement | Viewer is refused a container action with 403; the attempt is audited |
| 4.12 | Command audit trail | Every action and outcome visible in the UI; failures always carry a reason |
| 4.13 | Compose project grouping | Containers grouped by compose project labels |

**Phase 4 delivers:** full Docker visibility and user-initiated control.

---

## Phase 5 — Notification rules ⬜

**Goal:** precise, testable notifications. The product's differentiator.

| # | Milestone | Exit criterion |
| --- | --- | --- |
| 5.1 | Rule schema | Rules, nested condition groups, conditions (per ARCHITECTURE §9.1) |
| 5.2 | Condition evaluator | Unit-tested across all match types, including negation |
| 5.3 | Nested AND/OR | Deeply nested groups evaluate correctly; property-based tests |
| 5.4 | Regex safety | Patterns validated and length-capped; evaluation timeout disables the rule with a visible error |
| 5.5 | Event bus | Internal events from logs, containers and hosts reach the evaluator |
| 5.6 | Ntfy destination | A real notification arrives at a real Ntfy server |
| 5.7 | Webhook destination | Signed payload; delivery retried with backoff |
| 5.8 | SMTP destination | Email delivery with credentials stored encrypted |
| 5.9 | Cooldowns | Flapping container produces one notification; suppressed count reported |
| 5.10 | Rule builder UI | Nested conditions authored visually; invalid rules cannot be saved |
| 5.11 | Dry-run testing | A rule evaluated against pasted or historical events shows what would fire, and why |
| 5.12 | Notification history | Delivery attempts, successes and failures visible |
| 5.13 | Alerts page | Rule management replacing the placeholder |

**Phase 5 delivers:** the full rule engine from the product vision, including
both worked examples.

---

## Phase 6 — Network ⬜

| # | Milestone | Exit criterion |
| --- | --- | --- |
| 6.1 | Device schema | `network_devices`, `device_checks`, `device_relationships` |
| 6.2 | Manual registration | Add a device by IP or hostname; no scanning occurs |
| 6.3 | ICMP checks | Reachability via `CAP_NET_RAW`, not root |
| 6.4 | TCP port checks | Configurable ports; connection closed immediately, no payload |
| 6.5 | Agent-delegated checks | Devices reachable only from an agent's segment are checked by that agent |
| 6.6 | Availability history | Uptime percentage over a selected range |
| 6.7 | User-defined relationships | Operator declares links between devices |
| 6.8 | Topology view | Visual graph; container relationships surfaced automatically |
| 6.9 | Network page | Device management replacing the placeholder |

---

## Phase 7 — Operability ⬜

**Goal:** make it pleasant to run for a long time.

| # | Milestone | Exit criterion |
| --- | --- | --- |
| 7.1 | Settings UI | Agents, users, retention and destinations manageable in the UI |
| 7.2 | Production container image | Multi-stage build; non-root; health check; published |
| 7.3 | Deployment Compose file | `docker compose up` yields a working install |
| 7.4 | Backup and restore | Documented, tested procedure for database and configuration |
| 7.5 | Agent packaging | `.deb` and `.rpm` with a systemd unit |
| 7.6 | Agent self-update | Opt-in, signature-verified; never automatic by default |
| 7.7 | Upgrade path | Documented, tested version-to-version upgrade |
| 7.8 | Observability of itself | Control plane exposes its own metrics |

---

## Later, unscheduled

Candidates, not commitments: Compose file editing, `.rpm`/`.deb` repository
hosting, SSO/OIDC, read-only public status page, log frequency anomaly
detection (statistical and explainable — never AI-generated explanations),
mobile-optimised views, agent support for BSD and macOS.

## Permanently excluded

AI-generated error explanations, automatic root-cause analysis, autonomous
self-healing, automatic remediation, unrestricted remote shell execution.

These are product boundaries, not backlog items. See
[`PRODUCT_VISION.md`](PRODUCT_VISION.md#what-this-is-not).
