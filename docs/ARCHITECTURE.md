# Architecture

Technical architecture for Homelab Control Center. Describes the system as
designed, distinguishing what exists today from what the design anticipates.

This document is the hub. Five subsystems are large enough to have their own
documents, linked from the relevant sections below:

| Document | Covers |
| --- | --- |
| [`AGENT_ARCHITECTURE.md`](AGENT_ARCHITECTURE.md) | Transport, enrollment, credential rotation and revocation, configuration synchronisation, offline behaviour |
| [`MONITORING_MODULES.md`](MONITORING_MODULES.md) | The module catalogue, collection methods, hardware caveats |
| [`SCHEDULING.md`](SCHEDULING.md) | The shared scheduling model, DST, missed runs, drift |
| [`UPDATES.md`](UPDATES.md) | Agent updates: what establishes a release is genuine, policies, channels, maintenance windows, troubleshooting |
| [`DATA_RETENTION.md`](DATA_RETENTION.md) | Per-category retention, downsampling, storage limits, volume figures |
| [`REMOTE_MANAGEMENT.md`](REMOTE_MANAGEMENT.md) | Capability model, authorization chain, remote terminal |
| [`PLUGINS.md`](PLUGINS.md) | Future extension points; why no plugin system now |

- [1. System overview](#1-system-overview)
- [2. Components](#2-components)
- [3. Communication](#3-communication)
- [4. Authentication and authorization](#4-authentication-and-authorization)
- [5. Agent registration](#5-agent-registration)
- [6. Database strategy](#6-database-strategy)
- [7. Event and log ingestion](#7-event-and-log-ingestion)
- [8. Metrics retention](#8-metrics-retention)
- [9. Notification rule engine](#9-notification-rule-engine)
- [10. Host management security model](#10-host-management-security-model)
- [11. Network monitoring](#11-network-monitoring)
- [12. Deployment model](#12-deployment-model)
- [13. Architecture decision records](#13-architecture-decision-records)
- [14. Open design questions](#14-open-design-questions)

---

## 1. System overview

Three components and one database. No message broker, no service mesh, no
microservices.

```
┌──────────────────┐
│     Browser      │
│  React SPA       │
└────────┬─────────┘
         │ HTTPS  (REST + WebSocket, same origin)
         ▼
┌──────────────────────────────────────────┐
│          Control plane (FastAPI)         │
│                                          │
│  • Owns ALL persistent state             │
│  • Only component that touches the DB    │
│  • Authenticates users and agents        │
│  • Owns module + schedule configuration  │
│  • Evaluates notification rules          │
│  • Issues management commands            │
└───────┬───────────────────────┬──────────┘
        │ asyncpg               │ outbound only
        ▼                       │ (notifications)
┌────────────────┐              ▼
│   PostgreSQL   │      ┌────────────────┐
│                │      │ Ntfy / webhook │
└────────────────┘      │ / SMTP         │
                        └────────────────┘
        ▲
        │ HTTPS, agent-initiated (outbound from host)
        │
┌───────┴────────┐  ┌────────────────┐  ┌────────────────┐
│  Agent (Go)    │  │  Agent (Go)    │  │  Agent (Go)    │
│  host: nas     │  │  host: compute │  │  host: edge    │
│                │  │                │  │                │
│  local only:   │  │                │  │                │
│  /proc, /sys   │  │                │  │                │
│  docker.sock   │  │                │  │                │
│  journald      │  │                │  │                │
│  hwmon, SMART  │  │                │  │                │
└────────────────┘  └────────────────┘  └────────────────┘
```

The single most important structural property: **agents always dial out. The
control plane never connects to an agent.** Everything in
[§10](#10-host-management-security-model) follows from it.

LAN hosts, rented VPSes and machines behind CGNAT all use the same protocol
over the same transport. There is no separate "remote mode" — see
[`AGENT_ARCHITECTURE.md §1`](AGENT_ARCHITECTURE.md#1-topology).

The second structural property, added by the configurability requirements:
**the controller owns configuration and agents hold a cached copy.** What a
host collects, how often, and which management capabilities it permits are all
controller-owned, versioned documents that agents validate and acknowledge.
See [`AGENT_ARCHITECTURE.md §6`](AGENT_ARCHITECTURE.md#6-configuration-synchronisation).

### What exists today

| Element | Status |
| --- | --- |
| Monorepo, build system, CI | Implemented |
| FastAPI app, settings, logging, error envelope | Implemented |
| `/healthz`, `/readyz` (public), `/api/v1/info` (authenticated) | Implemented |
| PostgreSQL connectivity, Alembic chain | Implemented |
| `users`, `sessions`, `installation`, `audit_events` tables | Implemented |
| Argon2id password hashing (1.2) | Implemented |
| Login, logout, server-side sessions, CSRF, throttling (1.3) | Implemented |
| Default-deny authentication, role authorization (1.4) | Implemented |
| First-run bootstrap (`hcc bootstrap-admin`), reverse-proxy origin handling (1.5) | Implemented |
| User management, role enforcement, session administration, password change, audit foundation (1.7) | Implemented |
| `GET /api/v1/setup/status` (public, one bit) | Implemented |
| Per-host capability authorization | **Interface only**, fails closed |
| React shell, design system, 7 nav sections | Implemented |
| Typed API client, login, first-run screen, protected routing, account menu (1.6) | Implemented — authenticated against the real API |
| Settings: users, my account, sessions, audit (1.7) | Implemented — real API; General is read-only because configuration is environment-only |
| Overview page data | **Static fixtures** behind a banner, until 3.17 |
| Generated OpenAPI client | **Hand-written types with schema drift detection**, generation deferred to 1.15 |
| Go agent lifecycle, config, logging, shutdown | Implemented |
| Controller protocol | **Interface only**, no implementation |
| Monitoring modules, scheduling, retention | **Design, not code** |
| Remote management and terminal | **Design, not code** |
| Everything else below | **Design, not code** |

---

## 2. Components

### 2.1 Web client (`apps/web`)

React SPA. Presentation only.

**Responsibilities:** render state from the API; collect user intent; client-side
routing; display formatting.

**Explicitly not responsible for:** business rules, threshold decisions, rule
evaluation, or holding authoritative state. A browser is an untrusted client;
anything it decides can be forged. Notification rules are evaluated server-side
even though they are authored in the UI.

**Not an authorization boundary.** The route guards and the role-to-permission
map decide what the interface *offers*; the control plane decides what happens
and refuses anything else regardless of what was rendered
([ADR-031](#adr-031-authentication-is-default-deny-at-the-router-boundary)).
An unrecognised role therefore fails closed to the smallest interface rather
than to an error, because the client's guess only affects presentation.

**Authentication state** is one explicit union — `checking`,
`setup_required`, `unauthenticated`, `authenticated`, `unavailable` — derived
from `GET /api/v1/auth/me` and `GET /api/v1/setup/status`. The last two states
are separate deliberately: an unreachable control plane reported as a sign-out
would be a false statement about the user's session. Nothing about the session
is written to `localStorage` or `sessionStorage`; the session cookie is
`HttpOnly` and the client never needs to read it.

Built to static assets and served by the control plane in production, which
makes the deployment one container and removes CORS from the production path
entirely. In development Vite proxies `/api` with `changeOrigin`, which
rewrites `Host` — so the dev setup does need a declared CORS origin, and that
asymmetry is covered by a test in both directions.

Screens shown outside the console shell — login, first-run setup and the
backend-unavailable state — share one `FullPagePanel` layout rather than each
carrying its own copy of the chrome. The three copies had already drifted
apart in a way no test could catch, because none of them assert on
decoration.

### 2.2 Control plane (`apps/server`)

FastAPI. The authority for all state.

**Responsibilities:**
- Sole owner of the database. No other component connects to PostgreSQL.
- Authenticates and authorizes users and agents.
- **Owns configuration.** Resolves global defaults and per-host overrides into
  one versioned document per host, validates it, and serves it to agents.
- Ingests metrics, logs and events from agents.
- Computes rollups and enforces retention.
- Evaluates notification rules and dispatches notifications.
- Queues management commands for agents and records the audit trail.
- Performs network checks that do not require an agent.

**Deliberately stateless between requests.** All state is in PostgreSQL, so the
control plane can be restarted at any time and — once a leader-election
mechanism exists for the scheduler — scaled horizontally. Verified rather than
assumed: a browser session established before a restart still works after one,
because the session lives in `sessions` and not in process memory.

**`hcc.main` is the only supported way to start it**, in development and
production alike. Pointing uvicorn at the factory directly works but silently
loses what that module configures — the suppressed `Server` banner, the
disabled duplicate access log, and the single logging chain — so two
invocations would harden differently. They had already drifted once.

### 2.3 Agent (`agents/linux`)

A single static Go binary per monitored host.

**Responsibilities:**
- Run the monitoring modules its configuration enables — and **only** those.
  Every module is disabled by default
  ([`MONITORING_MODULES.md §2`](MONITORING_MODULES.md#2-everything-is-off-by-default)).
- Evaluate its own schedules locally, so collection survives a controller
  outage ([`SCHEDULING.md`](SCHEDULING.md)).
- Validate and acknowledge configuration, keeping the last known good document
  if a new one is invalid.
- Report which modules are actually available on this host, with reasons.
- Buffer on disk when the controller is unreachable, bounded, dropping oldest
  first and reporting the loss.
- Execute the enumerated set of management commands it is sent, subject to the
  capabilities enabled for its host.
- Report everything outward to the control plane.

**Explicitly not responsible for:** storing history (it is a forwarder with a
bounded buffer), resolving configuration layers (the controller sends a
resolved document), evaluating notification rules, computing rollups, or
deciding what matters. An agent that evaluated rules locally would mean
fleet-wide rule changes requiring a fleet-wide agent rollout.

**Structural constraints:** no listening socket; no inbound connections; never
proxies the Docker socket; runs only enumerated operations and a compiled-in
allow-list of collector binaries (ADR-021); never root.

---

## 3. Communication

| Link | Protocol | Direction | Auth |
| --- | --- | --- | --- |
| Browser → control plane | HTTPS REST, JSON | Inbound | Session cookie |
| Browser → control plane | WebSocket | Inbound | Session cookie |
| Agent → control plane | HTTPS REST, JSON | **Outbound from host** | Bearer credential |
| Agent → control plane | WebSocket | **Outbound from host** | Bearer credential |
| Control plane → PostgreSQL | PostgreSQL wire | Outbound | Password / TLS |
| Control plane → notifiers | HTTPS / SMTP | Outbound | Per-destination |

Every agent connection is **initiated by the agent**, including the WebSocket.
The control plane has no means of connecting to a host. Details in
[`AGENT_ARCHITECTURE.md §2`](AGENT_ARCHITECTURE.md#2-transport).

### 3.1 API versioning

Product endpoints live under `/api/v1`. Additive changes (new endpoint, new
optional field) go into the existing version. A breaking change introduces
`/api/v2` as a sibling package, and both are served during a deprecation
window.

Operational endpoints (`/healthz`, `/readyz`) are deliberately **unversioned**:
they are consumed by orchestrators, not product clients, and their contract
must survive API version changes. See ADR-006.

### 3.2 Error envelope

Every non-2xx response has one shape, so clients parse one contract:

```json
{
  "error": {
    "code": "not_found",
    "message": "Host 'nas-01' not found.",
    "details": { "host": "nas-01" },
    "request_id": "0f3c1a…"
  }
}
```

`code` is stable and machine-readable; `message` is human-readable;
`request_id` correlates with server logs. Internal details — exception types,
stack traces, connection strings — never cross this boundary. Implemented in
`apps/server/hcc/errors.py`.

### 3.3 WebSockets

Two distinct uses, not to be conflated:

**Browser ↔ control plane.** Live log tailing and dashboard updates.
Read-only: ordinary mutations go through REST, so that authorization and audit
logging live on one path instead of two. Authenticated by the same session
cookie as REST — and, unlike REST, with a **mandatory** `Origin` check,
because a handshake is not subject to CORS and the double-submit CSRF header
cannot be sent on one. See
[ADR-035](#adr-035-browser-websockets-are-session-authenticated-with-a-mandatory-origin-check).

**Agent → control plane.** Opened outbound by the agent and held open. Carries
pending-command delivery, event-driven module output (Docker lifecycle and
logs), and terminal session frames.

These two are **separate authentication domains** and must never be
interchangeable: the browser socket authenticates a *user* by session cookie,
the agent socket authenticates a *host* by bearer credential. Accepting an
agent credential where a user session is expected would let a compromised host
act as a user; the reverse would let a browser impersonate a host. The
boundary is tabulated in
[`SECURITY.md`](SECURITY.md#authentication-boundaries-for-websockets).

#### The one exception to "WebSockets are read-only"

A remote terminal session is inherently a bidirectional, mutating stream: it
cannot be expressed as REST calls. It is therefore an explicit, named
exception rather than a quiet erosion of the rule.

What keeps the exception contained:

- It is the **only** mutating WebSocket path, and it carries nothing but
  terminal frames for one authorized session.
- Opening it requires the full authorization chain plus re-authentication,
  evaluated over REST **before** the stream is established
  ([`REMOTE_MANAGEMENT.md §5.2`](REMOTE_MANAGEMENT.md#52-gates)).
- Every session is recorded with no off switch, so the audit guarantee is met
  by recording rather than by per-message audit entries.

All other mutations continue to go through REST. A proposal to add a second
mutating WebSocket path should be treated as a design smell.

---

## 4. Authentication and authorization

**Substantially implemented as of milestone 1.7.** User authentication,
server-side sessions, CSRF, default-deny routing and the role axis are in the
code and tested, and since 1.7 the role axis has production call sites: user
administration, session administration and the audit trail are
Administrator-only and enforced server-side on every request. The
**capability axis** is an interface that fails closed, and **agent**
authentication is still design. `docs/SECURITY.md` marks each section as
implemented or design.

### 4.1 Two distinct identity types

The system has two authentication domains that must not be conflated:

| | Users | Agents |
| --- | --- | --- |
| Credential | Password + session | Long-lived enrolment token |
| Transport | `Secure`, `HttpOnly`, `SameSite=Lax` cookie | `Authorization: Bearer` |
| Scope | Fleet-wide, role-limited | **One host only** |
| Can issue commands | Yes, if authorized | No |
| Can read others' data | Per role | No |

An agent token authenticates *one host reporting its own data*. A compromised
agent token must not be usable to read the fleet or issue commands — it is not
a user credential with a different shape.

### 4.2 Trust boundaries

```
  Untrusted ───────────────────────────────────────────
  Browser JavaScript, agent-submitted payloads,
  log content, container names, hostnames
       │
       │  validated and bounded at the boundary
       ▼
  ─────────────────────────────────────────────────────
  Control plane process
       │
       ▼
  Trusted: PostgreSQL (reachable only by the control plane)
```

Agent-submitted data is **untrusted input**, even from an authenticated agent:
a compromised host must not be able to escalate via a crafted payload. This has
concrete consequences — see [§7](#7-event-and-log-ingestion).

### 4.3 Authorization model

Authorization is evaluated along **two independent axes**, and both must
permit an action:

```
  WHO: the user's role                AND    WHERE: the target host's
  (Viewer / Operator / Administrator)        enabled capabilities
```

Role-based, checked server-side on every request:

| Role | Read monitoring | Manage containers | Manage units | Edit rules | Terminal | Manage agents/users |
| --- | :-: | :-: | :-: | :-: | :-: | :-: |
| Viewer | ✓ | | | | | |
| Operator | ✓ | ✓ | ✓ | ✓ | | |
| Administrator | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

A role grants nothing on a host whose corresponding capability is disabled —
and every capability is disabled by default. An Operator on a host with
`docker.control` off cannot restart a container there, and the refusal is
explicit rather than a silent no-op.

The role set stays the closed three from ADR-013. Granularity comes from the
capability axis, not from inventing more roles: the alternative — a role per
capability combination — multiplies out and is unmanageable. The capability
matrix and the full ordered chain are in
[`REMOTE_MANAGEMENT.md §4`](REMOTE_MANAGEMENT.md#4-authorization-chain).

Management actions additionally require re-authentication for destructive
operations and for terminal access, and are written to an append-only audit
log before dispatch. Audit logging has no off switch.

---

## 5. Agent registration

> Full specification, including credential rotation, revocation and
> configuration synchronisation:
> [`AGENT_ARCHITECTURE.md`](AGENT_ARCHITECTURE.md). This section is the
> summary.

Agents are enrolled deliberately by an administrator. There is no
auto-discovery and no self-service enrolment — a monitoring system that
automatically trusts anything claiming to be a host is a monitoring system
anyone on the network can poison.

**Implemented as of milestone 1.8.** What follows is what the code does, not
a plan.

```
1. Admin registers a host in the UI. Nothing else happens — no credential is
   minted, so a mistyped name does not leave a live secret anywhere.
      ↓
2. Admin issues a one-time enrolment credential. Shown exactly once; only its
   SHA-256 digest is stored, so no later request can produce it.
      ↓
3. Admin runs `hcc-agent enroll` on the host, with the credential in a file
   (mode 0600) rather than on a command line or in the environment.
      ↓
4. Agent POSTs /api/v1/agents/enroll, credential in an Authorization header.
      ↓
5. Control plane consumes the credential in one conditional UPDATE, revokes
   any previous agent credential, issues a new one, and returns:
     - host_id and host_name
     - the long-lived agent credential
     - the protocol version and heartbeat interval
      ↓
6. Agent writes the credential atomically at mode 0600, refusing a symlink or
   a world-writable directory.
      ↓
7. Steady state: one outbound authenticated WebSocket, heartbeating. The
   controller never dials in (ADR-018).
```

Design points:

- **One-time enrolment credential, separate from the steady-state one.** The
  short-lived one is what gets pasted into terminals and shell history; it is
  single-use, expires in fifteen minutes, is scoped to one host, and is
  revocable until spent. The long-lived credential never passes through a
  human's clipboard.
- **Both are stored as SHA-256 digests**
  ([ADR-042](#adr-042-agent-credentials-are-hashed-with-sha-256-not-argon2id)),
  never in plaintext. A database dump yields nothing replayable. Not Argon2id:
  there is no candidate list to slow down, and a salted hash cannot be looked
  up by index — which on the reconnection path a whole fleet takes at once is
  not a performance note but an outage.
- **Single use is decided by one conditional `UPDATE`**, not a read followed by
  a write. Two agents presenting the same credential at the same moment — two
  machines started by one Ansible run — would otherwise both see it as unused
  and both enroll.
- **Revocation is immediate.** Measured at **23 ms** from the administrative
  request to the agent's disconnection when one process holds the socket, and
  bounded by one heartbeat interval otherwise
  ([ADR-046](#adr-046-one-controller-process-stated-rather-than-enforced)).
  The agent then exits non-zero, so `systemctl status` says something true.
- **Neither secret is ever a command-line flag or in a URL.** Command lines
  are readable by every local user through `/proc`; a URL is in the browser's
  history, in the next request's `Referer`, and in every proxy's access log.
  Both travel in an `Authorization` header, including on the WebSocket
  handshake.
- **Neither file may be group- or world-readable, a symlink, owned by another
  user, or in a directory others can write to.** The agent refuses rather than
  fixing the mode itself: silently chmod-ing a file it did not create would
  hide a misconfiguration the operator needs to see.
- **There is no option to skip TLS verification.** The previous agent had one,
  with a warning logged at every start, and it was removed rather than kept —
  the warning was never a mitigation, and a flag that exists "just for
  testing" ends up in a unit file on a real server. Plain HTTP survives for
  loopback only.
- **The host's identity comes from the credential.** There is no `host_id` on
  either endpoint and no such field in the protocol, so an agent has nothing
  to claim to be. §7.3's server-side attribution rule, expressed as an
  absence.
- **Connectivity is derived, never stored**
  ([ADR-043](#adr-043-connectivity-is-derived-never-stored-as-a-boolean)).
  Three states, and a controller crash reports `stale` rather than leaving
  every host claiming to be connected.

---

## 6. Database strategy

PostgreSQL 17, accessed exclusively by the control plane.

**Why one PostgreSQL and not a dedicated time-series database:** a homelab
monitoring tool is deployed by one person on their own hardware. Every
additional stateful service is another thing to back up, upgrade and debug at
2am. PostgreSQL handles the expected volume — tens of hosts, thousands of
containers, and on the order of half a million raw metric rows per host per
day at the default schedules — comfortably. Note that ingestion rate is not
the constraint; storage growth and long-range query performance are, which is
what partitioning and the rollup tiers address. If volume later demands it,
TimescaleDB is a PostgreSQL extension, not a migration to a different system.
See ADR-003 and
[`DATA_RETENTION.md §7`](DATA_RETENTION.md#7-expected-volume).

### 6.1 Access rules

- Async SQLAlchemy 2.x with `asyncpg` (ADR-004).
- Sessions are request-scoped, rolled back on error, always closed. Commits are
  the handler's responsibility — only the handler knows whether its unit of
  work succeeded.
- Schema arrives with the features that need it; speculative tables are
  harder to remove than to add. Phase 0 therefore shipped no ORM models at
  all, and the three tables that exist today each arrived with the milestone
  that uses them.

### 6.2 Migrations

Alembic, async, sharing the application's settings so the two can never
disagree about which database they target. No credentials in `alembic.ini`.

An explicit constraint naming convention is configured on the metadata
(`hcc/db.py`). Without it, a migration dropping a constraint cannot reliably
name it, because the name was assigned by the database.

CI verifies three things on every push: migrations apply to an empty database,
they are reversible, and `alembic check` finds no model change lacking a
migration.

### 6.3 Schema shape

`users`, `sessions`, `installation` and `audit_events` are implemented; the
rest is indicative:

```
users                       IMPLEMENTED (1.1, display_name 1.7)
sessions                    IMPLEMENTED (1.3)
installation                IMPLEMENTED (1.5) — one row; first-run invariant
audit_events                IMPLEMENTED (1.7) — append-only; ADR-038
hosts ── agent_credentials
      ├─ host_metrics       (time-series, partitioned by time)
      ├─ containers ── container_metrics
      └─ log_entries        (time-series, partitioned by time)
network_devices ── device_checks ── device_relationships
notification_rules ── rule_conditions (nested)
notification_destinations
events ── notifications_sent
```

The `audit_log` this section previously anticipated shipped early, as
`audit_events`, with the operations that made it worth having — see ADR-038 for
why that was a deliberate scope change and what milestone 1.14 still owns.

High-volume tables (`host_metrics`, `container_metrics`, `log_entries`) are
declaratively partitioned by time, which makes retention a matter of dropping
a partition rather than a `DELETE` that bloats the table.

---

## 7. Event and log ingestion

### 7.1 Path

```
Source (journald / file / docker logs)
    ↓  agent tails, batches, applies backpressure
POST /api/v1/ingest/logs      (batched, gzip, agent-authenticated)
    ↓  validated, bounded, attributed server-side
log_entries (partitioned by time)
    ↓
rule evaluation ──► notification dispatch
```

### 7.2 Design rules

These follow directly from the product requirement that logs be preserved
faithfully.

**The original message and timestamp are preserved verbatim.** Parsed fields
are stored *alongside* the raw message, never instead of it. A parser that
silently rewrote log content would make the tool untrustworthy for the exact
debugging task it exists to support.

**Log level is nullable and honest.** It is populated only when a structured
level genuinely exists in the source. Applications do not agree on level
formatting — `ERROR`, `error`, `E`, `[err]`, `severity=3`, or nothing at all —
so the schema distinguishes "no level was present" from "level is INFO".
Guessing would silently miscategorise, and a rule matching `level = ERROR`
would quietly miss events.

**Parsing is user-configurable and non-destructive.** Operators define rules
for their unstructured sources. A failed parse stores the raw line with no
parsed fields and is counted in a visible metric — it is never dropped and
never silently discarded.

**No interpretation.** The platform surfaces, searches and filters logs. It
does not explain them, infer causes, or generate summaries. This is a product
boundary, not a limitation to be worked around later.

### 7.3 Server-side attribution

Ingested records are attributed to the host by the *server*, from the
authenticated agent credential — never from a `hostname` field in the payload.
Trusting a client-supplied hostname would let one compromised agent inject logs
attributed to any other host, which could in turn fire or suppress another
host's notification rules.

### 7.4 Bounds

Untrusted input must be bounded, or ingestion becomes a denial-of-service
vector:

- Maximum batch size and maximum message length, enforced before parsing.
- Per-agent rate limits, with a counter the operator can see.
- Rejected batches return a structured error naming the limit hit — the agent
  learns to back off rather than retrying blindly.

---

## 8. Metrics retention

> Full specification, including per-category retention, aggregation rules,
> storage limits, measured volume projections and the required benchmarks:
> [`DATA_RETENTION.md`](DATA_RETENTION.md). This section is the summary.

Not implemented. Design for Phase 3 (core metrics).

Three tiers, time-partitioned in plain PostgreSQL, with rollups computed by a
scheduled job and expiry implemented as partition drops:

| Tier | Default resolution | Default retention | Purpose |
| --- | --- | --- | --- |
| Raw | module-specific (1 s–6 h) | 7 days | Live view, recent incident investigation |
| 5-minute | 5 min | 90 days | Trends, weekly patterns |
| 1-hour | 1 h | 2 years | Capacity planning, long-term growth |

**These are defaults, not the design.** Retention is configured **per metric
category**, because the categories differ in how they age: a SMART
reallocated-sector count is valuable for years and must never be averaged,
while a session inventory is uninteresting after a week. Each tier can also be
disabled. ADR-008 is amended accordingly.

Two rules that follow from the configurability requirements:

- **Rollups store min, max, avg, count, first and last** — not just an
  average. Averaging a 5-second temperature series into 5-minute means erases
  the thermal spike the operator enabled 5-second sampling to catch. The UI
  renders downsampled series as a min–max band, not a single line.
- **Storage is budgeted and the cost is shown before it is incurred.** The
  controller projects steady-state size when a schedule or retention changes,
  rejects configurations exceeding the budget, and at the hard limit stops
  ingesting **loudly** rather than silently dropping samples, silently
  shortening retention, or filling the filesystem.

Expected volume at the recommended defaults is roughly **0.9 GB per host** at
steady state (~5 raw rows/second, 103 series); driven to the per-module
interval floors it reaches ~4.9 GB of raw data per host per week. The
arithmetic is in
[`DATA_RETENTION.md §7`](DATA_RETENTION.md#7-expected-volume) and must be
replaced by measurements before it ships
([§8 there](DATA_RETENTION.md#8-benchmarks-required-before-metric-storage-ships)).

TimescaleDB's continuous aggregates would do this with less code and are the
first escalation if benchmarks demand it — it is a PostgreSQL *extension*, so
adopting it preserves the one-stateful-service property. `alembic/env.py`
already carries the `include_object` hook needed to exclude Timescale-managed
artefacts. See ADR-003, ADR-008, and
[`DATA_RETENTION.md §9`](DATA_RETENTION.md#9-when-to-revisit-the-database-choice)
for the conditions that would reopen the decision.

---

## 9. Notification rule engine

Not implemented. Design for Phase 8. This is the feature with the most
specific product requirements, so the data model is specified early — it is
the hardest thing to retrofit.

### 9.1 Rules are a persisted tree, not code

A rule's conditions form a tree of AND/OR groups with arbitrary nesting,
stored relationally:

```
notification_rules
  id, name, enabled, cooldown_seconds, destination_id

rule_condition_groups
  id, rule_id, parent_group_id (nullable → root), operator ('AND' | 'OR')

rule_conditions
  id, group_id, field, match_type, value, negate
```

`parent_group_id` gives unlimited nesting without a recursive text format that
would need its own parser and validator.

**Why relational rather than a stored expression string or embedded script:**
rules must be editable in a UI, validated before saving, explained back to the
user, and testable against historical events. A stored script is none of those
things, and an engine that evaluates user-supplied code is a remote code
execution surface. There is no scripting in the rule engine, by design.

### 9.2 Matchable fields and match types

| Field | Source |
| --- | --- |
| `log.message` | Raw log text |
| `log.level` | Structured level, when present |
| `log.source` | Source identifier |
| `container.name`, `container.state`, `container.image` | Docker events |
| `host.name`, `host.status` | Host events |
| `event.type` | Event classification |

Match types: `equals`, `not_equals`, `contains`, `not_contains`,
`starts_with`, `ends_with`, `regex`, `in`, `greater_than`, `less_than`.
Each condition also carries a `negate` flag.

**Regex safety.** User-supplied patterns are a denial-of-service risk
(catastrophic backtracking). Mitigations: patterns are validated and
length-capped at save time; evaluation is bounded by a timeout; a pattern that
times out disables its rule and raises a visible error rather than silently
dropping events or wedging ingestion.

### 9.3 Level matching must not assume consistency

A rule matching "severity is ERROR" cannot rely on every application writing
`ERROR`. The engine therefore:

- Matches the normalised level when one was genuinely parsed.
- Lets a rule match the raw message text instead, for sources with no
  structured level.
- Treats "no level present" as a distinct, matchable state — never as INFO.

### 9.4 Cooldowns and testing

**Cooldown** is per rule, per distinct event key, so a flapping container
produces one notification rather than two hundred. Suppressed events are
counted and reported in the next notification ("and 47 similar events"), not
discarded silently.

**Dry-run testing** is a first-class requirement: a rule can be evaluated
against a pasted sample event or a window of historical events, showing exactly
what would have fired and why. Authoring a notification rule that cannot be
tested until 3am is the main failure mode of alerting systems.

---

## 10. Host management security model

This section is the most security-sensitive part of the design.

> Scope note. This section was written when management meant three Docker
> commands. Management is now a set of independent, default-off capabilities
> covering Docker, systemd, screen/tmux and an interactive terminal. The
> invariants below still hold; the capability model, the authorization matrix
> and the terminal's specific boundaries are specified in
> [`REMOTE_MANAGEMENT.md`](REMOTE_MANAGEMENT.md).

### 10.1 The Docker socket is root

Access to `/var/run/docker.sock` is equivalent to root on the host: it permits
starting a privileged container that mounts the host filesystem. Every rule
below follows from that fact.

### 10.2 Invariants

**The socket is never exposed on the network.** Not bound to a port, not
proxied, not forwarded, not tunnelled. The agent opens it as a local Unix
socket on its own host and nothing else can reach it. This is why the agent
exists at all: without it, the control plane would need remote Docker access,
which means either exposing the socket or holding root-equivalent credentials
for every host.

**The command set is closed and enumerated.**
`internal/controller.CommandType` enumerates every action an agent will ever
perform. The agent rejects any type it does not recognise. There is no
`exec`, no `run`, no generic passthrough, and no command that accepts a
caller-supplied command line.

The set has grown beyond the original three Docker commands to cover systemd
and session control; the current membership is in
[`REMOTE_MANAGEMENT.md §3`](REMOTE_MANAGEMENT.md#3-command-set). Growing an
enumerated set is not the same as opening it: each member maps to one
capability, carries only an enumerated target the agent itself discovered, and
is added deliberately with its authorization rule, audit entry and tests.

**Every capability is default-deny.** A freshly enrolled agent refuses every
command, including from a legitimate administrator, until a capability is
explicitly enabled for that host. The agent enforces this from its own applied
configuration rather than trusting the controller to have checked — so a
compromised controller cannot exceed what each host was already granted.

**The agent initiates every connection.** The original wording of this
invariant was "commands are polled, not pushed". Polling cannot carry an
interactive terminal, and makes event-driven log streaming either laggy or
wasteful, so command delivery now uses a WebSocket the **agent dials out** and
holds open.

The security property was never polling as a mechanism; it was that the agent
opens every connection and listens on nothing. That is unchanged: there is
still no listening socket, the controller still cannot initiate anything, and
commands still carry `expires_at` so one delivered after a partition cannot
execute against changed state. Recorded as
[ADR-018](#adr-018-agent-initiated-websocket-replaces-command-polling).

### 10.3 Authorization and audit chain

Every management action passes through, in order:

1. **Authenticated** — a valid user session.
2. **Authorized** — the user's role permits this action class, **and** the
   target host has the corresponding capability enabled. Both axes, checked
   independently; see [§4.3](#43-authorization-model).
3. **Re-authenticated** — for destructive operations, protected systemd units
   and all terminal access, the user proves possession of their password
   again. A valid session alone is not sufficient.
4. **Audited** — written to the append-only audit log *before* dispatch, so an
   action that crashes mid-flight is still recorded. Refused attempts are
   audited too: "a Viewer tried to restart a container" is a security signal
   worth keeping.
5. **Validated** — the target container, unit or session exists on the target
   host, and is permitted by that capability's target allow-list where one
   applies.
6. **Scoped** — the command is queued for exactly one agent.
7. **Expiring** — commands carry `expires_at`; the agent refuses stale
   commands, so one queued during a network partition cannot execute hours
   later against changed state.
8. **Re-checked at the agent** — the agent verifies the capability is enabled
   in its own applied configuration before acting.
9. **Reported** — the agent returns a result, which completes the audit record.
   A failed command always carries a reason; a failure with no reason is not
   auditable.

### 10.4 Agent privileges on the host

The agent runs as a dedicated unprivileged user, **not root**, and does not
escalate: it is not in `sudoers`.

Each capability needs its own narrow host grant, and the UI states what it is
asking for before the operator enables it. The per-capability table is in
[`REMOTE_MANAGEMENT.md §6`](REMOTE_MANAGEMENT.md#6-host-privileges). Two
caveats are repeated here because they undercut the "never root" property:

- **Docker group membership is root-equivalent.** Socket access permits
  launching a privileged container that mounts the host filesystem. Stated
  plainly, not glossed over.
- **Enabling `docker.control` and `terminal` on the same host is effectively
  root access**, because the terminal session can reach the Docker socket.
  The UI says so at the point of enabling.

Collector modules that have no kernel interface — SMART, some RAID
controllers, screen/tmux — invoke external binaries from a compiled-in
allow-list with fixed argument vectors, no shell, read-only subcommands, and
absolute paths. This sits deliberately close to the exclusion in §10.5; the
rules that make it a different thing are in
[`MONITORING_MODULES.md §4.3`](MONITORING_MODULES.md#43-external-binaries--the-one-genuinely-new-risk)
and [ADR-021](#adr-021-external-collector-binaries-on-a-fixed-allow-list).

The reference systemd unit in `agents/linux/README.md` applies
`NoNewPrivileges`, `ProtectSystem=strict` and a restricted syscall surface.

### 10.5 Explicitly excluded, permanently

Per the product vision, and not deferred:

- **A general-purpose command-execution API.** No endpoint and no agent
  command type accepts a caller-supplied command line.
- **Root or `sudo`-capable execution.** The agent is unprivileged and does not
  escalate.
- **Arbitrary collector binaries.** Collection uses a compiled-in allow-list
  with fixed argv (ADR-021); an operator cannot point a module at an
  executable of their choosing.
- **Capabilities enabled by default.** Default-deny is an invariant.
- **Unrecorded interactive access.** Session recording has no off switch.
- Autonomous self-healing or automatic remediation.
- Automatic root-cause analysis or AI-generated error explanations.

The platform monitors, notifies, and performs **user-initiated** management
actions from an enumerated set.

#### Supersession notice

Earlier revisions listed "arbitrary command or shell execution on hosts" as
excluded without qualification, which was read as excluding an interactive
terminal entirely. That blanket wording is superseded by
[ADR-022](#adr-022-remote-terminal-is-an-opt-in-capability-superseding-the-blanket-shell-exclusion).

An interactive terminal is now a planned capability: off by default, per host,
Administrator-only, re-authenticated, unprivileged, time-bounded, revocable
and fully recorded. The hazard the original exclusion targeted — a monitoring
agent that doubles as an always-available root backdoor — remains excluded by
the bullets above. [`REMOTE_MANAGEMENT.md §5`](REMOTE_MANAGEMENT.md#5-remote-terminal)
gives the boundaries and an honest assessment of the residual risk.

---

## 11. Network monitoring

Not implemented. Design for Phase 9.

**Devices are registered manually**, by IP or hostname. The platform does not
scan or sweep the network. Unsolicited scanning is hostile behaviour on
someone's own network, it trips IDS, and it is how a monitoring tool ends up
looking like an attacker.

**Checks:**

| Check | Mechanism | Notes |
| --- | --- | --- |
| ICMP | Echo request | Needs `CAP_NET_RAW`, not full root |
| TCP | Connect to a configured port | No payload sent, connection closed immediately |

Checks run from the control plane for directly reachable devices, or are
delegated to an agent for devices only reachable from that agent's network
segment — which is what makes a segmented homelab monitorable at all.

**Topology** is user-defined. Relationships between devices are declared by the
operator, not inferred: inferred topology is wrong in exactly the interesting
cases, and a diagram the operator does not recognise is worse than no diagram.
Docker containers on connected hosts are surfaced automatically, because that
relationship *is* known reliably — the agent observes it directly.

---

## 12. Deployment model

### 12.1 Target: single-host Compose

The design target is one person running this on their own hardware.

```
┌───────────────────────────────────────┐
│  Docker / Podman host                 │
│                                       │
│  ┌─────────────────────────────────┐  │
│  │ hcc-server                      │  │
│  │  FastAPI + built static assets  │  │
│  │  :8000                          │  │
│  └─────────────────────────────────┘  │
│  ┌─────────────────────────────────┐  │
│  │ postgres:17                     │  │
│  │  volume: hcc-data               │  │
│  └─────────────────────────────────┘  │
│  ┌─────────────────────────────────┐  │
│  │ reverse proxy (user-supplied)   │  │
│  │  TLS termination                │  │
│  └─────────────────────────────────┘  │
└───────────────────────────────────────┘

Agents installed directly on each monitored host (systemd unit, not a
container — they read host state).
```

**The web client is served by the control plane**, not by a separate web
server. One container, one origin, no CORS in production, no reverse-proxy
routing rules for the user to get wrong.

**TLS is terminated by a reverse proxy the user already runs** (Caddy, Traefik,
nginx). Homelab users have an existing TLS story — an internal CA, Let's
Encrypt via DNS-01, or Tailscale. Imposing a certificate mechanism would fight
their setup.

### 12.2 Development

Dependencies in containers, application processes on the host with hot reload:

```
PostgreSQL          container  (make db-up)
Control plane       host       (make dev-server, :8000)
Web client          host       (make dev-web,    :5173, proxies /api)
Agent               host       (make agent-run)
```

The Vite dev server proxies `/api` to the backend, so development is
same-origin like production. Without the proxy, development would depend on
CORS that production does not use — and the difference would surface as a
production-only bug.

### 12.3 Configuration

Environment variables only, validated at startup. No configuration files to
mount, no credentials in the image, no defaults that silently work in
development and fail in production.

`HCC_DATABASE_URL` has **no default**: a missing database URL is a
configuration error, not something to paper over with a localhost guess that
fails confusingly later. The process exits with a readable diagnostic naming
the variable.

### 12.4 One controller process

**Run one.** The registry of live agent connections is per process
([ADR-046](#adr-046-one-controller-process-stated-rather-than-enforced)), so
a second worker does not share it.

Nothing refuses to start with more, and the degradation is graceful rather
than silent: connectivity still comes from the database, so every worker
answers the same, and an agent whose host reconnected onto another worker is
closed by the epoch guard within one heartbeat interval instead of
immediately. What is genuinely lost is only *immediacy* — revoking a
credential becomes "within thirty seconds" rather than "within milliseconds"
when the revoking request lands on a different worker than the socket.

Measured capacity for that one process:
**500 concurrent agents at 224 KiB each**, 188 heartbeat round trips a second
against the 17 that 500 hosts at the 30-second interval need
([`PERFORMANCE.md`](PERFORMANCE.md)). The 501st connection is refused with a
503 the agent retries on.

This becomes a correctness question rather than a tidiness one the moment the
controller needs to *push* to a specific agent — configuration distribution —
and the answer then is a real channel, most likely PostgreSQL
`LISTEN`/`NOTIFY`, rather than a shared in-memory registry that cannot exist.

---

## 13. Architecture decision records

Each record states the decision, the reasoning, and what it costs.

### ADR-001: Monolithic control plane, not microservices

**Decision.** One FastAPI service owning all state and logic.

**Why.** The target deployment is one person's hardware. Microservices buy
independent scaling and deployment, neither of which is a homelab problem;
they cost network hops, distributed failure modes, and an operational burden
the user did not sign up for. A monolith with clear internal module boundaries
can be split later if it ever needs to be.

**Cost.** Horizontal scaling will eventually need leader election for the
scheduler. Accepted — that is a far smaller problem than premature
distribution.

### ADR-002: Ruff instead of Black + isort + Flake8

**Decision.** Ruff for both linting and formatting.

**Why.** One tool and one config section instead of three, materially faster,
and Ruff's formatter is Black-compatible so there is no style change to argue
about.

**Cost.** A few Flake8 plugins have no Ruff equivalent. None are in use.

### ADR-003: One PostgreSQL, no dedicated time-series database

**Decision.** PostgreSQL 17 for everything: relational data, metrics, logs.

**Why.** Every extra stateful service is another thing to back up, upgrade and
debug. PostgreSQL handles the expected volume with time-partitioned tables.
TimescaleDB remains available later as an *extension*, not a migration.

**Cost.** Less efficient than a purpose-built TSDB at high cardinality.
Revisit if real deployments exceed what partitioning handles — not before.

### ADR-004: Async SQLAlchemy with asyncpg

**Decision.** Async throughout, not sync.

**Why.** The workload is I/O-bound and concurrent: many agents ingesting
concurrently, plus WebSocket log streaming from Phase 5. Retrofitting async
later would touch every repository function and every handler.

**Cost.** Async code is harder to debug, and some libraries lack async
support. The ingestion path is the core of the product, so this is the right
place to pay.

### ADR-005: Separate liveness and readiness probes

**Decision.** `/healthz` checks nothing external; `/readyz` verifies the
database and returns 503 when degraded.

**Why.** A single endpoint that checks the database makes a brief PostgreSQL
blip restart the control plane, turning a recoverable dependency outage into a
full outage. Liveness answers "is this process alive", readiness answers "can
it serve traffic" — the orchestrator responses are different (restart vs.
remove from rotation).

**Cost.** Two endpoints to document. Covered by tests asserting that liveness
stays 200 while the database is unreachable.

### ADR-006: Versioned product API, unversioned operational endpoints

**Decision.** Product routes under `/api/v1`; probes at the root.

**Why.** Breaking changes need a migration path for API clients. Probes are
consumed by orchestrators and must not move when the product API version
changes — a Kubernetes manifest should not need editing because the API went
to v2.

**Cost.** Two conventions in one service. Tested explicitly.

### ADR-007: Generated API client, no shared types package

**Decision.** No `packages/` directory in Phase 0. TypeScript types will be
generated from the OpenAPI schema when the frontend first consumes the API.

**Why.** The brief suggests a shared package, but hand-maintained shared types
drift from the server that actually validates requests. FastAPI already
produces an OpenAPI schema from the Pydantic models that enforce the contract,
so generating from it makes drift impossible. An empty `packages/` directory
today would be speculative structure.

**Cost.** A generation step in the build. Added in Phase 1 with the first real
endpoint.

### ADR-008: Tiered downsampling for metrics retention

**Decision.** Three tiers — raw, 5-minute, hourly — implemented with
partitioned tables and a rollup job. Expiry is a partition drop.

**Why.** Unbounded raw retention is unaffordable, and nobody needs
second-level resolution from last year. Dropping partitions is cheap;
`DELETE` bloats tables.

**Cost.** A scheduled job to maintain, and queries must select the right tier.

**Amended (configurability requirements).** Two changes:

1. **Retention is per metric category, not global.** The original fixed
   figures (7 / 90 days / 2 years) are now *defaults*. Categories age
   differently: SMART attributes stay raw for years and are never averaged,
   because an averaged reallocated-sector count destroys the diagnostic
   signal; session inventory is worthless after a week. Tiers may be
   individually disabled.
2. **Rollups store min, max, avg, count, first and last** — not an average
   alone. The original wording did not specify the aggregate, and an average
   would erase exactly the spikes that justify 5-second sampling.

Also superseded: the original volume reasoning assumed a uniform 30-second
interval. With 5-second temperature presets and ~103 series per host the
figure is ~5 raw rows/second/host at defaults. See
[`DATA_RETENTION.md`](DATA_RETENTION.md), which also adds the storage budget
and the benchmarks required before metric storage ships.

### ADR-009: System fonts, no external assets

**Decision.** No web fonts, no CDN, no analytics, no external requests of any
kind from the web client.

**Why.** A self-hosted tool must work on an air-gapped network and must not
phone out to a third party on every page load. Loading a font from a CDN also
leaks the user's IP and usage pattern to that CDN.

**Cost.** Typography varies slightly across platforms. Mitigated with a
carefully ordered stack, and tabular numerals for metric alignment.

### ADR-010: Compose file targets both Docker and Podman

**Decision.** `deploy/compose.yaml` uses only OCI-portable keys and works with
`docker compose` and `podman-compose`. The Makefile detects which is present.

**Why.** The project's primary development platform is Fedora, where Podman is
the default and Docker is often absent. Requiring Docker for local development
of a tool that *monitors* Docker is an avoidable barrier. The agent still
targets the Docker Engine API, which is what it must monitor.

**Cost.** Runtime-specific features are unavailable. None are needed for a
single Postgres container.

### ADR-011: Zero third-party dependencies in the agent

**Decision.** The agent uses only the Go standard library.

**Why.** It is a long-lived process running on other people's servers, often
with elevated group membership. Its entire supply chain is the Go standard
library, so a compromised transitive dependency cannot reach those hosts. The
stdlib covers structured logging (`log/slog`), flags, signals and HTTP.

**Cost.** Some functionality must be written by hand. Phase 3 may need a
`/proc` parsing helper; the decision will be revisited then, explicitly,
rather than drifting.

**Reaffirmed (configurability requirements).** The new requirements pushed on
this decision in three places, and it holds in all three:

| Pressure | Resolution |
| --- | --- |
| Duration parsing (`"2h30m"`) | Avoided entirely: durations are integer seconds (ADR-019). No parser needed. |
| Cron expression parsing | Avoided entirely: calendar schedules are structured fields (ADR-020). No parser needed. |
| Time zone data for DST | Go's standard `time/tzdata`, embedded. Still stdlib, and it means schedules are correct on minimal hosts with no system tzdata. |

Those three avoidances were a deliberate consequence of this ADR rather than a
coincidence — the constraint shaped the design toward representations that are
also easier to validate.

**Scope clarification.** This ADR is about *Go module dependencies*. It does
not cover invoking external **binaries** such as `smartctl` or `mdadm`, which
some collectors genuinely require. That is a separate decision with a
different risk profile, recorded as
[ADR-021](#adr-021-external-collector-binaries-on-a-fixed-allow-list).

### ADR-012: Mock data is isolated and loudly labelled

**Decision.** Phase 0 sample data lives only in `src/mocks/`, is static (never
randomly generated), is rendered only beneath a non-dismissible banner, and is
never served through a fake API client.

**Why.** Engineering rules 8 and 9. A foundation that looks like a working
product invites the belief that monitoring exists. Randomly generated values
would animate on each render and read as live telemetry; a fake API client
would be a placeholder that pretends to work. Deleted in Phase 3.

**Cost.** The Overview page is rebuilt when real data arrives. Intended — the
fixtures exist to validate the design, not to be the implementation. A test
asserts the banner is present.

### ADR-013: `role` is a native PostgreSQL enum, not a string or a lookup table

**Decision.** `UserRole` is a Python `StrEnum` mapped to a native PostgreSQL
`user_role` enum type storing the member *values* (`viewer`), not their names.

**Why.** The role set is deliberately closed (§4.3), so the database should
enforce it: an unknown role becomes impossible rather than merely unexpected.
A `VARCHAR` + `CHECK` would be easier to extend, which is exactly the wrong
incentive — adding a role changes the authorization surface and should require
a reviewed migration, the same reasoning as the closed command set in §10.2.
A separate `roles` table was rejected as unnecessary indirection for three
fixed values with no attributes of their own.

`values_callable` is required because SQLAlchemy persists enum *names* by
default, which would put `VIEWER` in the database while the API says `viewer`.

**Cost.** Adding a role needs `ALTER TYPE user_role ADD VALUE` in a migration,
and Alembic autogenerate does not detect changes to enum members — so a new
role must be written by hand. Accepted, and the reason
`test_enum_type_has_exactly_the_expected_labels` pins the label list.

### ADR-014: Username uniqueness via a derived canonical column

**Decision.** Two columns: `username` holds exactly what the user typed;
`username_canonical` holds the normalized form and carries the unique
constraint. Normalization is NFKC → strip → casefold → NFKC, and rejects
interior whitespace and control characters. A SQLAlchemy `@validates` hook on
`username` recomputes the canonical value on every assignment.

**Why.** Users expect `Admin` and `admin` to be the same account, but also
expect their chosen capitalization to be displayed. Storing only the
normalized form loses the display case; a unique functional index on
`lower(username)` keeps it but can only apply `lower()`.

Full Unicode handling needs more than `lower()`: casefolding expands `ß` to
`ss`, and NFKC folds full-width `ｆｅｌｉｘ` onto `felix`, so a near-duplicate
cannot be registered alongside an existing name. Neither operation exists in
SQL, which rules out a PostgreSQL generated column — those may only call
immutable SQL functions. The control plane is the only writer (ADR-003), so
the application is the right place to enforce it.

Interior whitespace is rejected because NFKC maps U+00A0 onto a plain space:
without that check, `ad min` would normalize to `ad min` and become
confusable with a deliberately registered account.

**Cost.** The invariant is application-enforced, so a write issued outside the
ORM could store an inconsistent canonical value. Mitigated by the `@validates`
hook, by CHECK constraints on both columns' lengths, and by tests asserting the
hook fires on reassignment. Accents are deliberately *not* folded — `félix`
and `felix` are different accounts, which is a documented boundary rather than
an oversight.

### ADR-015: `updated_at` is maintained by a database trigger

**Decision.** A `BEFORE UPDATE` trigger (`trg_users_set_updated_at`) calling a
shared `set_updated_at()` function sets `updated_at = now()`. The ORM does not
use `onupdate`; the column is declared `server_onupdate=FetchedValue()` so
SQLAlchemy knows to refetch it.

**Why.** SQLAlchemy's `onupdate` only fires for ORM-mediated updates. A bulk
`UPDATE`, a migration, or a maintenance statement would leave `updated_at`
stale, which makes the column untrustworthy — and it is the column an audit
trail will lean on. The trigger holds for every write path.

Assigning unconditionally also means a client cannot backdate a row to conceal
a change: any `updated_at` supplied by an `UPDATE` is overwritten.

The trigger carries `WHEN (OLD.* IS DISTINCT FROM NEW.*)`, so a no-op update
does not bump the timestamp. `updated_at` therefore means "when this row's
data last actually changed", which is what callers assume.

**Cost.** Triggers are invisible to Alembic autogenerate, so they must be
written by hand in migrations and dropped explicitly on downgrade. The upside
of that invisibility is that they cannot cause false schema drift — `alembic
check` stays clean. Because `now()` is the transaction timestamp, two updates
in one transaction share an `updated_at`; that is correct, not a defect.

### ADR-016: UUID primary keys generated in Python

**Decision.** `id` is a `uuid4` generated by the application
(`default=uuid.uuid4`), stored in a native PostgreSQL `uuid` column.

**Why.** Application-side generation means code holds the id before the row is
flushed, so a unit of work can insert a parent and its children together
without a round trip. UUIDs also avoid leaking row counts through sequential
ids, which matters for a `users` table.

**Cost.** Random v4 values scatter across the primary-key index, so inserts
have worse locality than a sequential key. Irrelevant at the scale this
product targets (tens of users, tens of hosts). High-volume tables arriving in
Phase 3 are a different decision and will be made separately — a time-ordered
key such as UUIDv7 is the likely choice there, once the minimum Python version
makes `uuid.uuid7` available.

### ADR-017: Configuration is controller-owned, versioned and acknowledged

**Decision.** All module, schedule, retention and capability configuration is
authored in the web interface and owned by the controller. Layers (global
defaults → per-host overrides) are **resolved by the controller** into one
flat, versioned, checksummed document per host. Agents fetch it, validate it
locally, apply it atomically, and acknowledge it by version and checksum. An
invalid document is rejected with structured reasons and **never replaces the
last known good configuration**.

**Why.**

- *Controller-side resolution* means the UI can show exactly what a host will
  run before it is applied, and layer-precedence bugs cannot differ between
  agent versions. An agent that merged layers itself would make "why is this
  host collecting every 5 seconds?" unanswerable without SSH.
- *Versioning plus checksum* makes drift detectable. The heartbeat carries the
  running version, so an agent silently running month-old configuration is
  surfaced rather than hidden.
- *Agent-side validation* catches host-specific mismatches the controller
  cannot know about — SMART enabled on a host with no `smartctl` — and that
  mismatch is itself information worth showing.
- *Keeping the last known good* is the critical rule. A monitoring agent that
  stops monitoring because someone typed a bad interval has failed at its one
  job.

**Cost.** Two validation implementations, controller and agent, which can
disagree. Mitigated by sharing the schema definition and by treating a NACK of
a controller-validated document as a reportable defect rather than routine.
Rejected versions also require UI surface area, because a rejection nobody
sees is a silent failure.

**Alternatives rejected.** Config files on each host (defeats the point of a
web interface, and drifts); agent-side layer merging (above); fleet-wide
atomic apply (needs distributed consensus, which ADR-001 exists to avoid, and
an offline host would block every rollout).

### ADR-018: Agent-initiated WebSocket replaces command polling

**Decision.** Pending commands, event-driven module output and terminal frames
are delivered over a WebSocket that the **agent** opens outbound and holds
open. Long-polling remains the fallback where upgrades are blocked. This
supersedes the "commands are polled, not pushed" wording in
[§10.2](#102-invariants).

**Why.** Polling cannot carry an interactive terminal at usable latency, and
makes event-driven Docker log streaming either laggy or wasteful. More
importantly, the security property that mattered was never polling as a
mechanism — it was **the agent opens every connection and listens on
nothing**. A WebSocket dialled out by the agent preserves that exactly.

Unchanged: no listening socket on the agent, no inbound firewall rule, the
controller cannot initiate a connection, NAT and CGNAT remain non-issues, the
command set stays enumerated, and commands still expire.

**Cost.** A long-lived connection is more state to manage than a request loop:
reconnection with jittered backoff, and a fallback path that is by definition
less exercised. Capabilities needing a live stream report unavailable on the
fallback rather than silently degrading.

### ADR-019: Durations are normalised to integer seconds

**Decision.** Every duration in the system — intervals, timeouts, retention,
cooldowns, grace periods — is stored, transmitted and compared as **integer
seconds**. No duration strings (`"2h30m"`), no floats, no per-field units.

**Why.**

- *Validation is comparison.* Enforcing a 5-second floor is an integer test,
  not parse-then-compare against an ambiguous unit.
- *Arbitrary intervals come free.* "Every 17 seconds" and "every 2 h 30 min"
  are 17 and 9000; neither is a special case, which is what the requirement
  for arbitrary intervals asks for.
- *One representation end to end.* The same integer goes into PostgreSQL, onto
  the wire, and into a Go `time.Ticker`. No conversion means no conversion
  bug.
- *No parser in the agent*, upholding ADR-011.

Presets (5 s, 30 s, 1 min, …) are UI sugar over the same field, so no code
branches on "is this a preset".

**Cost.** Raw stored values are less readable (`9000` rather than `2h30m`),
and the UI must do all formatting and parsing. Sub-second intervals become
unrepresentable — accepted deliberately, since the 1-second floor is a
documented limit and sub-second collection is not a homelab requirement.

### ADR-020: Structured calendar schedules, not cron expressions

**Decision.** Calendar schedules are structured fields — IANA time zone,
months, days of month, days of week, a list of `HH:MM` times, plus explicit
DST and missed-execution policies. Cron expressions are not accepted.

**Why.**

- *Field-level validation.* A UI can present weekday checkboxes and a time
  picker, and reject bad input per field with a specific message.
  `"*/7 2-5 * * 1-5"` can only be validated as a whole.
- *No ambiguity.* Cron ORs day-of-month with day-of-week, which surprises
  nearly everyone and silently runs jobs on unintended days. Here they are
  ANDed, and that is written down.
- *DST policy has somewhere to live.* Cron has no field for "what happens when
  02:30 does not exist tonight". Here it is explicit, with defaults.
- *No parser dependency* in the agent (ADR-011).

**Cost.** Operators fluent in cron cannot paste an expression, and some
expressible patterns ("every 7th day of the month") are not representable.
Accepted: the audience configures a handful of schedules in a web form, and
the patterns actually needed — nightly, weekly on chosen days, several times a
day — are all covered.

### ADR-021: External collector binaries on a fixed allow-list

**Decision.** Modules with no kernel interface (SMART, some RAID controllers,
screen/tmux) may invoke external binaries, subject to all of: a **compiled-in
allow-list** of executables; **fixed argument vectors** with only
agent-discovered device paths substituted; **no shell**, ever; **read-only
subcommands** except the separately-gated `smart_selftest` module; resolution
by **absolute path**, never `PATH`; timeouts, output caps and per-device
concurrency limits; **never root**; and module-level audit of the resolved
`argv`.

**Why.** There is no kernel interface for SMART attributes or most hardware
RAID controllers. The alternatives were to drop those modules — losing the
disk-failure warning that is among the most valuable things a homelab monitor
can provide — or to reimplement vendor protocols, which is a large amount of
hardware-specific code with worse correctness than the vendor's own tool.

This sits deliberately close to the exclusion of "arbitrary command execution"
in [§10.5](#105-explicitly-excluded-permanently). The distinction is not a
technicality: with a compiled-in allow-list and fixed argv there is **no input
path by which a caller chooses what runs**. An operator cannot point a module
at a different executable, and no user-supplied string reaches an argument.
Arbitrary execution means the caller picks the command; this does not.

**Cost.** The agent now depends on host tooling it does not ship, so module
availability becomes host-dependent — which is why capability reporting
exists. Subprocess execution is a larger attack surface than reading
`/proc`, and the mitigations above are load-bearing rather than belt-and-braces.
A module that cannot be implemented within these constraints does not ship.

### ADR-022: Remote terminal is an opt-in capability, superseding the blanket shell exclusion

**Decision.** An interactive terminal becomes a planned, **opt-in** capability:
disabled by default, enabled per host, Administrator-only, requiring
re-authentication, running as the agent's unprivileged user with no `sudo`,
idle- and absolute-timeout bounded, revocable mid-session, and **recorded with
no off switch**.

This supersedes the unqualified "unrestricted remote shell execution is
permanently excluded" in earlier revisions of
[`PRODUCT_VISION.md`](PRODUCT_VISION.md#clarification-unrestricted-remote-shell-execution),
[`ROADMAP.md`](ROADMAP.md) and [§10.5](#105-explicitly-excluded-permanently).

**Why.** The original wording conflated two features. The hazard it targeted —
a monitoring agent that doubles as an always-available, root-capable backdoor
on every host — is addressed by default-deny, per-host opt-in, role
restriction, re-authentication, unprivileged execution and mandatory
recording. Refusing to build a terminal at all did not remove that hazard; it
pushed operators back to maintaining separate SSH access, which this platform
cannot audit and which is frequently root-capable. The honest comparison is
not "terminal versus nothing" but "audited unprivileged terminal versus
unaudited root SSH".

**What stays excluded.** A general-purpose command-execution API; root or
`sudo` execution; any path available without explicit per-host opt-in; and
unrecorded access. See
[`REMOTE_MANAGEMENT.md §8`](REMOTE_MANAGEMENT.md#8-still-excluded).

**Cost, stated plainly.** This materially expands the threat surface. An
attacker holding an Administrator session who can re-authenticate gains
interactive unprivileged shell access to hosts where the capability is
enabled. No design removes that; the design bounds it. It also creates a
second mutating WebSocket path, an explicit exception to
[§3.3](#33-websockets). And on a host where `docker.control` is also enabled,
the terminal can reach the Docker socket and is therefore effectively root —
documented at the point of enabling rather than buried here.

Operators who judge the trade-off unacceptable get the original guarantee by
leaving the capability off, which is the default and requires no action.

### ADR-023: Capability granularity on hosts, not more roles

**Decision.** Authorization is two-dimensional: a coarse **user role**
(Viewer / Operator / Administrator, unchanged from ADR-013) ANDed with a
**per-host capability** flag (`docker.read`, `docker.control`,
`systemd.control`, `terminal`, …). Read and control are always separate
capabilities, and enabling one never implies another.

**Why.** The requirement is that permissions be evaluable for both the user
and the target agent. The alternative — encoding capabilities into roles —
multiplies out: three roles times seven capabilities is a combinatorial mess
that nobody can reason about, and it would force the `user_role` enum open
every time a capability is added, contradicting ADR-013's reasoning that
widening an authorization surface should be a reviewed migration.

Keeping capabilities on the host also matches how operators think: "this
machine may be restarted from the dashboard; that one is read-only" is a
property of the machine, not of a person.

**Cost.** Two things to configure before an action works, and a corresponding
need for good error messages distinguishing "your role does not permit this"
from "this host does not permit this". A per-host *role assignment* (user X is
an Operator only on these hosts) is a plausible future third dimension; it
composes with this model rather than replacing it, and is listed in
[§14](#14-open-design-questions).

### ADR-024: No plugin system, but keep the module boundary clean

**Decision.** No plugin framework, no published extension API, and no stable
module interface. Internal modules are compiled-in Go packages behind a
common interface which remains **explicitly unstable**. Extension points are
nonetheless kept clean, and the intent is recorded in
[`PLUGINS.md`](PLUGINS.md).

**Why.** A plugin system is a published contract, and the module interface
cannot be known to be right yet. The designed modules differ in shape in ways
that will reshape it: `cpu` reads a file and returns numbers, `docker` holds
an event stream and needs reconciliation, `smart_selftest` triggers a
multi-hour hardware operation with progress, `system_logs` produces records on
a different pipeline. An interface that honestly accommodates all of them is
one discovered by writing all of them. Publishing after the first would mean
either breaking third-party authors later or carrying a bad abstraction
permanently.

Second, a plugin system is an arbitrary-code-execution feature. The whole
security posture rests on the agent running only enumerated operations, with
external binaries confined to ADR-021's allow-list. Letting operators install
third-party collectors onto monitored hosts is in direct tension with that,
and the open questions in
[`PLUGINS.md §4`](PLUGINS.md#4-questions-that-must-be-answered-first) — above
all whether the controller distributes plugin code to agents — must be
answered before any mechanism is chosen.

**Cost.** Community contributions must go through pull requests for the
foreseeable future, which is slower and limits the catalogue to what
maintainers will merge. Accepted: a wrong plugin contract is far more
expensive than a slow one, and the declarative "read this file, parse this
number" form sketched in `PLUGINS.md §3` would cover most plausible community
modules without arbitrary execution at all.

---

### ADR-025: Argon2id via argon2-cffi, with RFC 9106 low-memory parameters

**Decision.** Passwords are hashed with **Argon2id** through
[`argon2-cffi`](https://argon2-cffi.readthedocs.io/). Default cost parameters
are RFC 9106's *second recommended* ("low memory") profile — 64 MiB memory,
3 passes, 4 lanes, 32-byte digest, 16-byte salt — configurable within enforced
floors. The full PHC-encoded string is stored, which makes parameter upgrades
detectable without a schema change.

**Why Argon2id.** RFC 9106 §4 recommends the hybrid variant for password
hashing, as does OWASP. Argon2i alone is weaker against time-memory trade-off
attacks; Argon2d alone leaks information through data-dependent memory access.

**Why `argon2-cffi`.** It is the reference Python binding to the official
Argon2 C implementation, actively maintained, and ships the RFC profiles as
named constants. The obvious alternative, `passlib`, last released in 2020 and
has known friction with current `bcrypt` releases — an unmaintained dependency
on the authentication path is not a trade worth making. No cryptography is
implemented in this project.

**Why these parameters.** Measured on a 20-core desktop: ~50 ms per hash at
the defaults, ~115 ms at `parallelism=1`. Both are acceptable login latencies
and expensive enough to make offline cracking costly.

RFC 9106's *first* recommended profile (2 GiB, `t=1`) is deliberately not the
default: it is stronger on a dedicated machine, but a single login would
exceed the total memory of much of the hardware this product targets.

**Memory is charged per concurrent hash**, which is the operationally
surprising part and is documented wherever the setting appears: ten
simultaneous logins at 64 MiB each transiently need ~640 MiB.

**Why configurable, and why floored.** A Raspberry Pi serving a household of
three should be able to tune the cost down; an operator with memory to spare
should be able to tune it up. Floors (19456 KiB, 2 passes, 1 lane — OWASP's
minimum) mean configuration can trade cost for hardware but cannot configure
the hashing into uselessness. This is the invariants-versus-capabilities rule
from [`PRODUCT_VISION.md`](PRODUCT_VISION.md#4-invariants-are-structural-capabilities-are-configurable)
applied to a concrete setting. The bounds are enforced twice, in Pydantic
settings and again in `Argon2Parameters`, with a test asserting the two have
not drifted apart.

**Cost.** Login is inherently slow — tens of milliseconds of CPU and tens of
megabytes of RAM per attempt. That is the point of a password hash, but it has
two consequences the later milestones must handle: hashing must not run on the
event loop (hence the `*_async` methods offloading to a worker thread), and
the login endpoint needs rate limiting, because an unauthenticated caller can
otherwise make the server spend 64 MiB and 50 ms per request.

### ADR-026: Password policy — permissive, NFC-normalized, never truncated

**Decision.** Minimum 8 characters, maximum 1024 UTF-8 bytes, **no composition
rules**, all Unicode accepted, **NFC** normalization, and over-long input
**rejected rather than truncated**. Passwords are not stripped of whitespace.

**Why no composition rules.** NIST SP 800-63B removed them, because requiring
character classes pushes users toward predictable patterns (`Password1!`) and
measurably reduces real-world entropy. The minimum length is the one rule that
survives that reasoning.

**Why a maximum at all.** Argon2 has no inherent input limit — unlike bcrypt,
which silently ignores everything past 72 bytes. The cap exists purely because
hashing cost grows with input length, so an unbounded field lets an
unauthenticated caller make the server do arbitrary work. It is measured in
**bytes, not characters**, because bytes are what the algorithm processes: a
character-based cap would let a four-times-larger payload through as emoji.

**Why never truncate.** Silently shortening a password breaks the user's
expectation and, worse, makes two distinct passwords sharing a long prefix
into the same credential. A test demonstrates that collision to document what
rejection prevents.

**Why NFC and not NFKC — the opposite of usernames.** This is the interesting
part, because milestone 1.1 chose NFKC for usernames (ADR-014) and this
chooses NFC for passwords. Both are right:

* Usernames need *aggressive* folding so a full-width or ligature variant
  cannot be registered alongside an existing name. Confusables are an
  impersonation risk, and collapsing them is the goal.
* Passwords need *minimal* folding. NFKC maps compatibility variants onto
  canonical ones, which would collapse genuinely distinct passwords and
  discard entropy the user intended to have.

NFC fixes only encoding differences representing *identical* text — `é` as one
code point versus `e` plus a combining accent. Without it, a user who sets a
password on one platform and types it on another can be locked out by an
invisible difference; this was verified experimentally before choosing. The
profile is PRECIS `OpaqueString` (RFC 8265 §4.2), the standard for passwords.

**Why whitespace is preserved.** Leading and trailing spaces are legitimate
password characters and a password manager may generate them. Trimming would
silently alter the secret — the same class of mistake as truncation.

**Cost.** The minimum length is enforced when *setting* a password but
deliberately **not** when verifying one, so that raising the minimum later
cannot lock out existing users. The byte cap is enforced on both paths,
because that bound exists to limit work rather than to express policy.

### ADR-027: Session tokens are hashed with SHA-256, not Argon2

**Decision.** Session and CSRF tokens are 256-bit values from
`secrets.token_bytes`, stored as an **unsalted hex SHA-256 digest**.

**Why not Argon2, when passwords use it (ADR-025).** Argon2's cost exists to
defeat *guessing* of low-entropy secrets. A password has perhaps 30–60 bits of
entropy, so an attacker holding the hash can enumerate candidates, and making
each attempt cost 64 MiB makes that uneconomic.

A session token has 256 bits from a CSPRNG. There is no candidate list, no
dictionary and no structure to exploit, so a slow KDF has nothing to slow
down. Using Argon2 here would be actively harmful: **every authenticated
request** would cost ~50 ms of CPU and 64 MiB of RAM, making the session
lookup more expensive than the login that created it and turning every
authenticated endpoint into a denial-of-service amplifier.

**Why unsalted.** The hash must be deterministic so a presented token resolves
in one indexed lookup; a per-row salt would force a table scan with one hash
per row. Salts defend against precomputation across a shared keyspace, and
256-bit random tokens have no shared keyspace.

**What the hashing buys.** A database dump, backup or leaked log yields hashes
that cannot be replayed. That is the entire threat, and SHA-256 covers it.

**Cost.** Two hashing strategies in one codebase, which invites "why is this
one different?" — answered in the module docstring and here.

### ADR-028: Server-side sessions, not JWT

**Decision.** A `sessions` table. The cookie carries an opaque random token;
every authorization decision reads the row.

**Why.** **Revocation.** Logging out, deactivating a user or revoking a
credential must take effect immediately — the same promise
`AGENT_ARCHITECTURE.md` makes for agents. A self-contained JWT is valid until
it expires, so immediate revocation needs a server-side deny-list; at that
point every request hits the database anyway and the statelessness has bought
nothing, while adding signature verification, key rotation and a much larger
footgun surface (algorithm confusion, `alg: none`, unverified claims).

Stateless tokens pay off at a scale this product explicitly does not target:
many services verifying tokens without a shared datastore. Here there is one
control plane and one database (ADR-001), so a row is simpler, smaller and
revocable.

It also makes `is_active` live: the user is loaded with the session on every
request, so disabling an account takes effect on its next request with no
session sweep.

**Cost.** A database read per authenticated request — one indexed lookup on a
small table, with the user eagerly joined. A session table to prune, for which
`SessionService.prune_expired` exists. And sessions do not survive a database
loss, which is correct: they are state, and the database is where state lives.

### ADR-029: In-process rate limiting, plus a semaphore on Argon2

**Decision.** Two independent bounds on the login path. A rate limiter
(`hcc/security/ratelimit.py`) with separate per-address and per-username
budgets, consulted **before** password verification. And a
`threading.BoundedSemaphore` inside `PasswordHasher` capping simultaneous
Argon2 operations (default 4, so ~256 MiB peak).

**Why both.** They bound different things and neither subsumes the other: the
limiter bounds attempts over time, the semaphore bounds peak memory at an
instant. Ten attempts per second is survivable spread out and is not
survivable all at once.

**Why the ordering matters.** A limiter consulted *after* verification still
lets an attacker spend 64 MiB and 50 ms per request. Checking first is the
whole point, and it is asserted by a test.

**Why both keys.** Per-address alone lets a botnet spray one account from many
addresses, each under the limit. Per-username alone lets one host enumerate
usernames freely. Usernames are keyed canonically, so casing variants share a
budget.

**Amended (milestone 1.5): three budgets, not two.** The two-budget form had a
defect found by audit. With a per-username budget *lower* than the per-address
budget — the shipped defaults were 10 and 20 — one attacker could exhaust a
known administrator's budget without exhausting their own, and sustain it
indefinitely: a remote, unauthenticated, permanent denial of service against
a named account, reachable with ten requests every five minutes from a single
host. The original wording above even noted that risk and shipped it anyway.

The budgets are now per source, per source-and-account, and per account
(defaults 20 / 10 / 100), consulted in that order with a refusal returning
immediately. A source cut off by its per-account budget therefore stops
contributing to the global one, so one source can spend at most
`per_address_username` of `per_username` and cannot exhaust it. Configuration
enforces `per_username > per_address_username` at startup, so the unsafe
relationship cannot return. A successful login clears that source's budgets
but not the global one, which would otherwise be a free reset for an attacker
holding one working password.

What remains, stated in
[`SECURITY.md`](SECURITY.md#login-throttling-implemented): a sufficiently
distributed attacker can still degrade availability for one account. That is
inherent to any per-account limit, and the alternative — no global budget —
permits unlimited distributed guessing.

**Why in-process, and not Redis.** Redis would be a second stateful service
for one feature, which ADR-001 and ADR-003 exist to avoid.

**Cost, stated plainly.** State is per process. Several uvicorn workers each
get their own counters, multiplying the effective limit by the worker count,
and a restart clears them. Acceptable for the single-container deployment
targeted here; if multi-worker support ever lands, the limiter moves to
PostgreSQL or the deployment pins one worker. Recorded in
[`SECURITY.md`](SECURITY.md#known-gaps).

**Why a threading semaphore** rather than an asyncio one: the work runs in a
worker thread for async callers and inline for sync ones, so one threading
primitive bounds both paths with no event-loop affinity.

### ADR-030: CSRF via a session-bound double-submit token

**Decision.** Every state-changing authenticated request must carry the
session's CSRF token in `X-CSRF-Token`. The token is generated per session,
stored **in the session row**, and compared against that row in constant time.
A present `Origin` must additionally match an allowed origin.

**Why not rely on `SameSite=Lax` alone.** It is one browser-side control on an
API that hands out credentials, and it depends on browser behaviour the server
cannot verify. Defence in depth is cheap here.

**Why session-bound rather than a bare double-submit cookie.** A plain
double-submit trusts that only the real origin can set the cookie. That fails
if an attacker can write cookies for the parent domain — a realistic risk on a
homelab where several services share one domain. Comparing against the session
row removes the assumption, making this a synchronizer token with no extra
state, since the session row already exists. A test asserts that a forged
cookie-and-header pair is still refused.

**Why the CSRF token is stored in plaintext** while the session token is
hashed: it must be handed back to a legitimate client on `/auth/me`, which is
how double-submit works at all. It is not a credential on its own — an
attacker holding it can do nothing without the session cookie, which *is*
hashed.

**Why the check lives in the session dependency.** Were it separate, an author
could add an authenticated mutating route and forget it, and the omission
would be invisible until exploited. Asking for the current session necessarily
gets the protection.

**Cost.** Clients must read a cookie and set a header — ordinary SPA work.
A missing `Origin` header falls back to the token check alone, because
non-browser clients send none; that is a deliberate, documented gap rather
than an oversight.

### ADR-031: Authentication is default-deny at the router boundary

**Decision.** Product routes are mounted beneath a **router-level**
authentication dependency in `hcc/api/v1/router.py`. A route becomes public
only by being deliberately placed on the separate public router, and the
public set is recorded with a reason in `hcc.app.PUBLIC_PATHS`. A
**behavioural** regression test walks every path in the running application's
OpenAPI schema, calls it unauthenticated, and requires a 401 unless the path
is allow-listed.

**Why not per-endpoint dependencies.** They were the Phase 1.3 arrangement and
they work, but their failure mode is silence: forgetting one produces an
unauthenticated endpoint and no signal at all. Under a router-level
dependency, forgetting is impossible — a developer has to actively move a
route out to expose it, which is a visible diff.

**Why not middleware.** A middleware checking a path allow-list would catch
routes mounted anywhere, but it needs pattern matching against paths with
parameters, duplicates the dependency's work, and sits awkwardly relative to
the database session. The router dependency is the idiomatic FastAPI
mechanism, and the behavioural test closes the gap middleware would have
covered.

**Why the test is behavioural rather than introspective.** Walking
`app.routes` to look for the dependency would be the obvious check, but this
FastAPI version nests included routers behind private internals, and a test
coupled to those would break on upgrade while proving less. Issuing a real
unauthenticated request proves the property that actually matters, and catches
a route mounted outside `hcc.api.v1.router` entirely — which no amount of
router nesting could prevent. The test includes a self-check that mounts a
deliberately unprotected route and asserts the scan detects it, so the scan
cannot pass by doing nothing.

**Cost.** Two routers to keep straight in one module, and an allow-list
duplicated between the application and the test. The duplication is
deliberate: widening the public surface then requires editing both, which
makes it a reviewed decision.

### ADR-032: Public endpoints disclose as little as possible

**Decision.** `/healthz` returns `{"status": "ok"}`. `/readyz` returns
`{"status": "ready"}` or a 503 with `{"status": "not_ready"}`. Neither
includes the release version, the environment name, or the per-dependency
breakdown they previously carried. The version and environment moved to
`GET /api/v1/info`, which requires a session; the dependency detail moved to
the structured log.

**Amends ADR-005**, which specified the readiness probe without considering
that it is necessarily unauthenticated.

**Why.** These two endpoints have to be public — orchestrators, reverse-proxy
health checks and container runtimes probe them before any session exists and
frequently cannot present credentials. Being public, everything they return is
returned to anyone who can reach the port. A release version lets an attacker
match the deployment against known vulnerabilities, and an enumerated
dependency list tells them what the deployment is built from and when a
component is down — a useful signal for timing an attack.

**Why nothing is lost.** The orchestrator consumes the HTTP status code, which
is unchanged. The operator debugging a failed readiness check reads the log,
which now records the dependency and the exception type — and which already
had to be the source of truth, since the probe only ever reported the first
failure it found.

**Cost.** An operator can no longer see the version without logging in, and
`curl /readyz` no longer says which dependency is down. Accepted: both are one
authenticated request or one log line away, and neither is worth handing to an
anonymous caller.

### ADR-033: The initial administrator is created by a local command, not an endpoint

**Decision.** A fresh installation is initialized by running
`hcc bootstrap-admin` on the controller host or inside its container. There is
**no** HTTP endpoint that creates the first administrator, not even one gated
on "no users exist". Initialization is recorded as a single row in an
`installation` table, and bootstrap is refused while that row exists.

**Why not an endpoint.** The obvious design — expose `POST /api/v1/setup`
while `users` is empty — gives ownership of the deployment to whoever reaches
the port first. On a homelab that race is real: the control plane is commonly
reachable on the LAN before the operator has finished configuring it, and the
window is the gap between "container started" and "operator opened the
browser". Losing it is also silent, because the installation then simply looks
already set up.

An empty table is a statement about the current user list, not proof of
anything. So bootstrap is authorized by something a network attacker does not
have — the database credentials, demonstrated by running a local command.
That is the same authority needed to read every password hash and session
token anyway, so requiring it grants nothing new.

**Why the invariant is a row and not a count.** Bootstrap must not reopen when
the administrator is later deactivated or deleted; deriving the condition from
`count(*)` would hand ownership to whoever noticed an emptied table. The row
is written once, never updated or deleted by the application, and carries
**no foreign key** to `users` — a cascade would delete it with the
administrator and silently reopen the door.

**Why it is atomic.** The administrator row and the installation row are
written in one transaction, so no half-initialized state exists. A
transaction-scoped advisory lock serialises concurrent attempts so the loser
gets a readable error, and the **singleton primary key** on `installation`
(`id boolean` with `CHECK (id)`) means a second insert is refused by the
database whether or not the lock was taken. Correctness does not depend on
cooperation; the lock only improves the error message.

**Cost.** Initialization cannot be completed from a browser, so the first-run
experience is a terminal command — documented in `docs/DEVELOPMENT.md` and in
the server README. A future HTTP flow for the setup wizard would additionally
require a one-time bootstrap secret printed to the server's log or console,
which is deliberately **not** built now: the CLI is sufficient and has no
equivalent race. Recovering a lost administrator password also requires
database access, which is recorded as a known gap rather than solved with a
second privileged path.

### ADR-034: Request origin and client address come from declared configuration, not from headers

**Decision.** Two settings govern how the application interprets a request
that arrived through a proxy:

* `HCC_PUBLIC_ORIGIN` — the origin browsers use. When set it is
  **authoritative** for the CSRF `Origin` check; nothing the client sends can
  override it.
* `HCC_TRUSTED_PROXIES` — addresses or CIDR networks whose
  `X-Forwarded-For` and `X-Forwarded-Proto` headers may be believed. Empty by
  default. The `X-Forwarded-For` chain is walked from the **right**,
  discarding trusted hops.

A production deployment must set one of them, or startup fails. The previous
`HCC_TRUSTED_PROXY_HEADERS`, which trusted a named header unconditionally, is
removed.

**Why.** Forwarding headers are request headers: any client can set them.
Trusting them unconditionally lets a caller forge its address and bypass
per-address throttling; ignoring them behind a real proxy makes every client
look like the proxy, so all users share one budget and one attacker can deny
login to everyone. Neither failure is acceptable, and the application cannot
tell which situation it is in — but the operator can. So it is configuration,
and the default is the safe-but-degraded one rather than the unsafe one.

The same information decides the CSRF `Origin` comparison. Deriving the
expected origin from the raw ASGI scheme — what the code did before milestone
1.5 — meant that behind a TLS-terminating proxy the application compared a
browser's `https://host` against its own internal `http://host` and **rejected
every state-changing request** with a CSRF error naming the wrong cause. That
was a verified defect in the one topology [§12.1](#121-target-single-host-compose)
recommends.

**Why startup fails rather than warns.** The runtime symptom is a 403 on every
mutation, attributed to CSRF. An operator debugging that looks at cookies and
tokens, not at scheme derivation. A startup failure naming the two variables
is strictly more useful than a deployment where login works and logout does
not.

**Why a wildcard CORS origin is refused.** `allow_credentials` is on because
this API authenticates with cookies, and Starlette answers a wildcard in that
configuration by echoing whichever origin asked — so any website could read
authenticated responses. An operator who writes `*` wants something the
application must not provide, so it is refused rather than silently narrowed.

**Cost.** One more required setting for a production deployment, and an
operator who changes their proxy topology must remember to update it. Accepted:
the alternative is a security property that depends on a guess.

### ADR-035: Browser WebSockets are session-authenticated with a mandatory Origin check

**Decision.** The shared session dependency accepts both HTTP requests and
WebSocket handshakes. On a handshake, a valid session is required **and**
`Origin` must be present and match — absent is refused, unlike on HTTP. Agent
WebSockets (Phase 7) are a separate authentication domain using a bearer host
credential, and the two must never be interchangeable.

**Why `Origin` is mandatory here but optional on HTTP.** A WebSocket handshake
is not subject to CORS, so a browser will open a cross-origin socket and
attach cookies — this is cross-site WebSocket hijacking, and `Origin` is the
only signal that distinguishes it from a legitimate handshake. The
double-submit CSRF header **cannot** be sent on a handshake at all, so there is
no token to fall back on. Browsers always send `Origin` on a handshake, so
requiring it costs a legitimate client nothing, and a non-browser client has
no business on a session-authenticated socket.

**Why this is decided now, with no WebSocket in the product.** Two are coming
and they are not the same: a browser log tail and terminal authenticate a
*user*; the agent channel authenticates a *host*. Accepting an agent
credential where a user session is expected would let a compromised host act
as a user, and the reverse would let a browser impersonate a host. Deciding
the boundary after the first socket exists means deciding it under pressure
from whatever that socket happened to need.

It also required a concrete fix. The shared dependencies were annotated
`Request`, which FastAPI injects only on HTTP routes, so a WebSocket mounted
on the protected router failed with a `TypeError` — refused, but by an
internal error rather than an authentication decision. They now take
`HTTPConnection`, the common base of `Request` and `WebSocket`, so one
dependency is correct on both.

**Cost.** How a browser socket presents a CSRF token *after* the handshake is
left to Phase 5. The `ws`/`wss` schemes also have to be mapped onto `http`/
`https` when reconstructing an origin, because a browser opening
`wss://host/x` sends `Origin: https://host` — a small translation, and one
whose absence made every authenticated handshake fail until a test caught it.

---

### ADR-036: The setup-status endpoint discloses one bit, publicly

**Decision.** `GET /api/v1/setup/status` is unauthenticated and returns
exactly `{"initialized": true|false}`. It reads the installation row from
[ADR-033](#adr-033-the-initial-administrator-is-created-by-a-local-command-not-an-endpoint),
not a user count. It is the fourth and currently last public path in
production.

**Why anything is public here.** A fresh installation has no credentials, so
the first screen a browser can reach must be reachable with none. The
alternative is to infer the state from the login endpoint — a distinct error
when no administrator exists — which is strictly worse: it reopens the
username-enumeration oracle [ADR-032](#adr-032-public-endpoints-disclose-as-little-as-possible)
closes, because the same mechanism that distinguishes "no users yet" can be
made to distinguish "not this user". The other alternative, showing setup
instructions to every anonymous visitor, is a worse interface and discloses
the same bit.

**Why the bit is acceptable.** Its only value to an attacker is knowing
whether an installation can still be claimed. Under ADR-033 it cannot be
claimed over HTTP at all, at any time — creating the first administrator
requires shell access to the host. So the disclosure is "this deployment has
not finished being set up", which an unconfigured deployment announces anyway
by having nothing behind the login page. If a future milestone ever adds a
network-reachable bootstrap path, this endpoint becomes a reconnaissance
signal and must be reconsidered in the same change.

**Why it reads the installation row.** A user count would answer a different
question. An installation whose only administrator was later deleted has zero
users but is still initialized; reporting `false` there would show setup
instructions for a path that correctly refuses to run, which is a confusing
dead end rather than a vulnerability — the durable invariant exists precisely
so the two cannot diverge.

**Cost.** One public endpoint to keep in the default-deny audit
([ADR-031](#adr-031-authentication-is-default-deny-at-the-router-boundary)),
pinned by a test asserting the full production public surface, so adding a
fifth is a deliberate act.

---

### ADR-037: The client's API types are hand-written and validated against the real schema

**Decision.** `apps/web/src/api/types.ts` is written by hand. A committed
snapshot of the backend's OpenAPI document (`src/api/openapi.snapshot.json`,
regenerated by `make openapi`) is asserted against those types by
`src/api/openapi.test.ts`, which fails when the contract moves. Full
client generation from the schema remains milestone 1.15.

**Why not generate now.** The roadmap reserves generation for 1.15, and taking
it over early would mean choosing a generator, a build step and a vendoring
policy as a side effect of a frontend-integration milestone — scope that
belongs to the milestone that owns it. The frontend needed four endpoints, not
a code generator.

**Why not just hand-write and move on.** Hand-written types fail silently and
invisibly: the compiler validates the client against *its own belief* about
the server, so a renamed field or a widened enum typechecks perfectly and
breaks at runtime. Drift detection is the half of generation that carries the
safety, and it is independent of generating anything — so it ships now. The
test caught a real instance immediately: the login response typed `role` as a
bare `str` on the server, so the closed role set never reached the schema at
all.

**Why a committed snapshot rather than a live backend.** The frontend test
suite must run without PostgreSQL, a running control plane or a network. A
committed artefact also makes contract changes visible in review as a diff,
which a live fetch would hide.

**Cost.** The snapshot is a second place to update, and a stale one would
validate against yesterday's contract — so CI regenerates it from the backend
and fails if it differs from the committed copy. The types still have to be
edited by hand until 1.15; the test narrows that to a mechanical, detected
task rather than a silent one.

---

### ADR-038: The audit trail is a database table with an open action vocabulary

**Decision.** Privileged operations are recorded in an append-only
`audit_events` table from milestone **1.7**, not 1.14. `action` is a
`varchar` carrying dotted identifiers (`user.role_changed`), with the
vocabulary defined in Python (`AuditAction`) and **not** constrained by the
database. `outcome` is a `varchar` constrained by a CHECK to `success`,
`denied` or `failure`. Neither `actor_user_id` nor `target_user_id` carries a
foreign key. **No cryptographic tamper evidence is claimed or implemented.**

**Why now, rather than in 1.14.** The roadmap reserved an append-only
`audit_log` for 1.14, and 1.7 is the milestone that creates the operations
worth auditing: an administrator can now change somebody's role, end their
access, and invalidate their sessions. Shipping those with only a structured
log line would mean the first privileged operations in the product are also
the only ones never recorded in a queryable trail — and retrofitting the
record later cannot recover what happened in between.

So this is a deliberate scope change rather than drift, and it is bounded: a
table, a write path, and one read endpoint. What 1.14 still owns is everything
that makes an audit *system* — retention, export, the login and enrolment
events, and whatever integrity story is decided on. The shape here is chosen so
those are additive.

**Why the action vocabulary is not a native enum, when `role` is.** ADR-013
made `user_role` a PostgreSQL enum precisely so that adding a member is a
reviewed migration, because a new role changes the authorization surface.
Applying that reasoning here would be cargo-culting it:

* A new audit action changes nothing about what anybody may do. It records
  something the system already did.
* The vocabulary is *expected* to grow with almost every future milestone, and
  Alembic cannot autogenerate enum-member changes — so each addition would be
  a hand-written `ALTER TYPE` for no review benefit.
* An unrecognised value read back is harmless. The reader renders it verbatim;
  there is no decision to fail closed. The client's types say `string` for the
  same reason, and a test asserts the schema does not narrow it to an enum.

`outcome` is the half that genuinely is closed — three states with no growth
pressure — so a CHECK does the work a native enum would, without a second
custom type in the schema.

**Why no foreign key.** `ON DELETE CASCADE` would delete the record of what
was done to an account along with the account, which is exactly backwards: the
record is most valuable once the account is gone. `ON DELETE SET NULL` keeps
the row and discards the attribution, which is nearly as bad. This is the same
reasoning that keeps `installation.initialized_by_user_id` unconstrained
(ADR-033). The read API resolves names with a `LEFT JOIN` and reports an absent
account as absent rather than inventing a label.

**Why no tamper evidence.** An attacker with database credentials can rewrite
history, and that attacker is already out of scope in `SECURITY.md`. A hash
chain the same process both writes and verifies would put the verifier's key
next to the data it protects — security theatre with a maintenance cost. Said
plainly here so nobody reads "append-only" as more than it is: the application
has no update and no delete path, which is a property of the code, not a grant
the database enforces.

**Cost.** A table that grows and has no retention policy yet, and whose denial
records can be produced at an authenticated caller's request rate. Both are
recorded in `SECURITY.md` alongside the session table's equivalent, and
retention arrives with the subsystem that owns retention.

### ADR-039: The last-active-administrator invariant is held by a transaction-scoped advisory lock

**Decision.** Every operation that can reduce the set of active administrators
takes `pg_advisory_xact_lock` on one fixed key **before** reading, and holds it
until the caller commits. All such operations funnel through
`hcc/services/users.py`, which takes the lock on every mutating path rather
than only the dangerous ones.

**Why a count followed by an update is not enough.** The obvious
implementation is wrong in a way that one connection cannot demonstrate:

```
SELECT count(*) FROM users WHERE role = 'admin' AND is_active;   -- 2, fine
UPDATE users SET role = 'viewer' WHERE id = :target;             -- go ahead
```

Two administrators demoting each other simultaneously both read 2, both decide
they are safe, and both commit. Under READ COMMITTED neither sees the other's
uncommitted write, so there is nothing for either to notice. The installation
ends with zero administrators and every check passed.

**Why this matters more than an ordinary availability bug.** An installation
with no administrator cannot create one: bootstrap is a one-way door that does
not reopen (ADR-033), deliberately, so that nobody can claim an installation by
emptying its user table. Locking yourself out is therefore permanent short of
database access.

**Why the lock works.** Because the count runs *after* the lock is held, and
READ COMMITTED gives each statement a fresh snapshot, the second transaction's
count sees the first's committed demotion, finds zero other active
administrators, and refuses. Transaction-scoped means it is released by commit,
rollback or crash, so an interrupted attempt leaves nothing to clean up — the
same mechanism and the same lifetime as the bootstrap lock, with a distinct
key so a bootstrap attempt does not block on a role change.

The lock is taken by every mutating operation, not only those that could break
the invariant. Two reasons: a display-name change and a role change against the
same row would otherwise be a lost-update race, and "which operations need the
lock?" is a question whose wrong answer is silent. Over-locking a path that
sees a handful of operations a month costs nothing measurable.

**Why not the alternatives.** A unique index cannot express "at least one row
exists". An exclusion constraint cannot either. A statement-level trigger that
counts has the exact race above unless it, too, serialises — at which point it
is this mechanism, written somewhere harder to find and harder to test.

**Cost, stated plainly.** The lock protects writers that take it. A raw
`UPDATE users` issued directly against the database bypasses it. That is
accepted rather than solved: it requires the database credentials, and an
adversary holding those is already out of scope. Note the contrast with
ADR-033, where the singleton primary key means correctness does not depend on
cooperation — here it does, and the honest statement is that every application
write path cooperates and nothing else is claimed.

Proved by PostgreSQL concurrency tests rather than by argument: ten
simultaneous demotions across ten connections leave exactly one administrator,
and nine succeed.

### ADR-040: A password change rotates the current session and revokes every other one

**Decision.** `POST /api/v1/account/password` requires the current password,
revokes **every** session for the account including the caller's own, and
issues a replacement — so the browser stays signed in on new material while
the token it held before the change stops working.

**Why every other session.** A password is changed either as hygiene or because
it may be known to somebody else. In the second case, leaving existing sessions
alive means the change accomplished nothing: the attacker's session outlives
the credential it came from, for up to the absolute session lifetime.

**Why the current session is replaced rather than preserved.** This follows the
project's existing position instead of inventing one. Login already revokes any
presented session before issuing a new one, because a token that survives a
credential event is a token an attacker may have planted. A password change is
exactly such an event, and a session token that may have leaked — into a proxy
log, a shared machine, an XSS payload — is retired by the same mechanism.

Preserving it was the alternative and is cheaper to implement. It also means
the one session an attacker is most likely to already hold is the one session
the change does not touch.

**Why the current password is required**, when the caller already holds a valid
session: a session proves possession of a cookie, and changing a credential
should require possession of the credential. Otherwise a stolen session can
lock the real owner out of their own account.

**Why a wrong current password is 422 and never 401.** The browser client
treats 401 as "your session has ended" and signs the user out
(`isSessionExpired` is 401-only, by design — ADR-030's neighbour in
`SECURITY.md`). Returning 401 here would eject somebody from the application
for a typo. It is not 403 either: the caller *is* permitted to change their own
password, so "you do not have permission" would be a false statement about
authorization. What happened is that a submitted field failed verification,
which is what 422 means everywhere else in this API.

**Concurrency.** The user row is locked `FOR UPDATE` for the duration, so two
simultaneous changes serialise and the second verifies against the *new* hash
and is refused. The locked read uses `populate_existing`, which is
load-bearing and was missing in the first implementation: session resolution
has already loaded that row, so SQLAlchemy returned the identity-map copy and
the lock was acquired around a **stale** hash. Both changes then verified
against the same old value and both succeeded — the one combination that looks
correct in every single-request test.

**Cost.** Two Argon2 operations per request, so roughly twice a login, on an
authenticated endpoint. Bounded by a rate limiter keyed on the account and
kept deliberately separate from the login budgets: sharing them would mean
changing your password a few times spends the budget you need in order to sign
in, which is a self-inflicted lockout created by a limiter meant to prevent
one.

### ADR-041: Usernames are immutable; the display name is the mutable label

**Decision.** `users.username` and `users.username_canonical` cannot be changed
through the API. A new nullable `users.display_name` carries the
human-readable label, and it is administrator-managed rather than
self-service.

**Why the two were separated.** Before this, the only human-readable label an
account had was the thing it logs in with, so correcting a typo in somebody's
name meant changing their credential. Splitting them is what makes "rename a
user" a safe operation at all.

**Why renaming the username is not offered.** A username is an identity, and
it is load-bearing in three places that a rename would silently re-point:

* the login throttle's budget keys, which are canonical usernames (ADR-029);
* every structured log line about that account;
* the audit trail, which records `username_canonical` on account creation.

Worse, a rename frees the old canonical name, so somebody else could register
it and inherit the previous holder's history by eye. Supporting it properly
means a rename history, and a reviewer able to tell which `felix` a year-old
log line meant. That is a feature, not a field.

**Why `display_name` is nullable, and why there is no empty string.** Null
means "this account has no separate label", never "unknown" — callers render
the username. A blank submission maps onto null, and a CHECK constraint keeps
the empty string unstorable, so the two states cannot be confused. An empty
cell in an administrator's user list is otherwise indistinguishable from a bug.

**Why display-name editing is administrator-managed.** A judgement call rather
than a security boundary, and it goes the conservative way: the display name is
how an administrator reviewing the user list identifies an account, so letting
every user rewrite their own label means the list an administrator audits is
populated with names those users chose. The My Account screen says where the
change is made rather than offering a field that does nothing.

**Cost.** Correcting a username requires creating a new account and
deactivating the old one, which loses that account's `created_at` and its
session history. Accepted: it is rare, it is explicit, and the alternative
makes an identity mutable.

---

### ADR-042: Agent credentials are hashed with SHA-256, not Argon2id

**Decision.** Both the one-time enrollment credential and the long-lived agent
credential are stored as an **unsalted SHA-256 digest**, through the existing
`hcc.security.tokens` helpers. This supersedes
[`AGENT_ARCHITECTURE.md §4.2`](AGENT_ARCHITECTURE.md#42-credential-handling-rules),
which said Argon2id.

**Why the earlier document was wrong.** It applied the password rule to a
different kind of secret. [ADR-027](#adr-027-session-tokens-are-hashed-with-sha-256-not-argon2)
already settled this for session tokens and the reasoning transfers exactly:

- Argon2's cost exists to defeat *guessing* of low-entropy secrets. These are
  256 bits from a CSPRNG. There is no candidate list to enumerate, no
  dictionary and no structure, so there is nothing for a slow KDF to slow
  down.
- A **salted** hash cannot be looked up by index. Verifying a presented
  credential would mean scanning every pending enrollment and every agent
  credential, hashing each at 64 MiB and 50 ms. On the agent-reconnection
  path — which a thousand hosts take at once after a controller restart —
  that is not a performance note, it is an outage.

**What the hashing does buy, and it is the whole threat.** A database dump,
backup or log leak yields digests that cannot be replayed. SHA-256 addresses
that completely.

**Why unsalted is safe here.** Salts defend against precomputation across a
*shared* keyspace. With 256-bit random values there is no shared keyspace to
precompute, and determinism is what makes the single indexed lookup possible.

**Cost.** The two documents disagreed for a while, which is how a
security-relevant decision gets implemented twice in different ways. Resolved
by changing the one that was wrong rather than the one that was right.

---

### ADR-043: Connectivity is derived, never stored as a boolean

**Decision.** There is no `hosts.is_connected` column. A host's connectivity
is computed from two stored facts — `current_connection_id` and
`last_heartbeat_at` — into one of three states: `offline`, `connected`,
`stale`.

**Why not a boolean.** A flag set on connect and cleared on disconnect is
wrong in the one case that matters. If the controller process dies, nothing
runs the clearing code, and **every host stays "connected" forever** — so the
dashboard reports healthy connectivity for a fleet connected to nothing. That
is precisely the failure [`PRODUCT_VISION.md`](PRODUCT_VISION.md) principle 2
forbids, and it is the failure mode that is hardest to notice because the
interface looks fine.

**Why three states and not two.** A crash leaves `current_connection_id` set
and `last_heartbeat_at` frozen, which derives as **stale** — the honest
answer, and visibly a problem. Collapsing that into either `connected` or
`offline` would require the controller to guess which of two different
incidents happened: the agent died, or the network did. It genuinely cannot
tell, and only the host's operator can.

**Why `offline` is asserted only on a cleared connection.** Clearing
`current_connection_id` is something the controller did deliberately, on a
clean disconnect, so it is more trustworthy than a timestamp — which is merely
the last thing that arrived. A recorded connection with no recent heartbeat is
therefore never reported as offline.

**Connection epoch.** Each accepted connection increments
`hosts.connection_epoch` and gets a fresh `current_connection_id`, and every
later write by that connection carries both. Without the guard, an agent that
reconnects while its previous socket is still draining produces a disconnect
from the *old* connection that clears the *new* one's state — so a host that
is in fact connected reads as offline, and a flapping link makes the dashboard
oscillate.

**Cost.** Connectivity cannot be filtered in SQL without duplicating the
derivation as a `WHERE` clause — two implementations of the one rule that
decides whether the dashboard claims a host is up, free to disagree. So the
list endpoint applies that filter in Python, after the page is fetched, and
says so: a connectivity-filtered page can return fewer rows than its limit.
Acceptable at tens of hosts; the fix when it stops being acceptable is a
stored projection refreshed on heartbeat, not a second copy of the rule.

---

### ADR-044: Host invariants are serialised per host, not installation-wide

**Decision.** Every host mutation takes a **per-host** transaction-scoped
advisory lock, `pg_advisory_xact_lock(HOST_LOCK_CLASS, hashtext(host_id))`.
[ADR-039](#adr-039-the-last-active-administrator-invariant-is-held-by-a-transaction-scoped-advisory-lock)
serialises every *user* mutation on one global key; this deliberately does not
follow it.

**Why the global pattern does not transfer.** Two differences, and both point
the same way:

- The invariant ADR-039 protects is installation-wide — "at least one active
  administrator" — so any two writers can interfere. The host invariants are
  per host: at most one live credential, at most one pending enrollment.
  Nothing about host A's credential can affect host B's.
- User administration is a handful of operations a month. An agent fleet
  reconnecting after a controller restart is a burst of concurrent writes, and
  a global lock would serialise the whole fleet behind one mutex at exactly
  the moment it is busiest.

**What the lock is and is not for.** Correctness comes from the partial unique
index (`uq_agent_credentials_one_live_per_host`) and from conditional
`UPDATE`s whose predicates the database evaluates against committed state. The
lock makes the *outcome deterministic* rather than a constraint violation
somebody has to retry. The connection-accept path takes no lock at all for
this reason: `connection_epoch = connection_epoch + 1` in one statement is
correct under PostgreSQL's row lock without one.

**Why the object id is computed by PostgreSQL.** `hashtext(host_id::text)`,
not a hash chosen in Python. A lock key derived differently by two callers is a
lock that does nothing, and computing it in SQL means every participant
derives it the same way by construction. A collision between two host ids
costs a little unnecessary serialisation and breaks no invariant.

---

### ADR-045: The audit trail gains a host dimension rather than a second trail

**Decision.** `audit_events` gains one nullable column, `target_host_id`, a
sibling of `target_user_id`. The nine host actions are new `AuditAction`
members with no schema change.

**Why not a polymorphic `(target_type, target_id)` pair.** It looks tidier and
is worse. The type would have to be checked at every read, "every event about
this host" would be a query no index can serve without a composite on a
low-cardinality discriminator, and a reader could not tell a host id from a
user id without consulting another column. Two nullable, independently indexed
columns are filterable, joinable and unambiguous.

**Why not a separate host audit table.** Because then "what happened on this
installation" is two queries whose results have to be merged and ordered in
the application, and the merge is where events start going missing. One
append-only trail, one ordering, one place to look.

**What is deliberately not recorded.** There is no `host.agent_connected` and
no `host.agent_disconnected`. A flapping link would write two rows a second
and bury every administrative event in the trail, and the information already
lives where it belongs — `last_connected_at` on the host row and the
structured log. An audit trail answers "who did what to this system", and an
agent reattaching after a network blip is not somebody doing something.

**Failed authentications are recorded, with suppression.** A refused agent is
recorded **only when the presented credential resolves to a known host** —
otherwise there is nothing to attribute it to, and a row per unrecognised
digest would make an endpoint reachable without a session into a table-growth
amplifier. And at most once per host per minute, because a revoked agent
retries on its own backoff forever, so one machine nobody has fixed yet would
otherwise fill the trail.

---

### ADR-046: One controller process, stated rather than enforced

**Decision.** The registry of live agent connections
(`hcc.agents.registry.ConnectionRegistry`) is **per process**. The deployment
guidance is one controller process. Nothing in the code refuses to start with
more.

**What a second worker does and does not break.** Worth being precise, because
the tempting mistake is to treat the registry as the source of truth:

- **Connectivity is unaffected.** It is derived from the `hosts` row, which
  every worker reads and writes in the same database (ADR-043). A browser
  asking "is homeserver connected?" gets the same answer from either.
- **Superseding within one worker works.** Both connections are in the same
  registry.
- **Superseding across workers is delayed, not lost.** Worker A still holds a
  socket whose host has reconnected onto worker B. A's next heartbeat fails
  the epoch guard, so its write is a no-op, and it establishes the real reason
  and closes. The state stays correct; the stale socket lingers for up to one
  heartbeat interval.
- **Immediate revocation becomes eventual.** An administrative request that
  revokes a credential asks *its own* process to close the socket, which is
  instant when that process holds it. Across workers it is bounded by the
  heartbeat interval instead — the durable check is what makes it a guarantee
  either way.
- **Pushing to an agent across workers does not work at all.** Nothing in this
  milestone pushes, so nothing depends on it.

**Why it is not enforced in code.** A check that refused to start with
`--workers 2` would be enforcing a constraint on an operator who may have
reasons, and whose hosts would still report correctly. Refusing to start is a
strong action to take over a property that degrades gracefully.

**When this has to change.** Configuration distribution. The moment the
controller needs to *push* to a specific agent, a second worker becomes a
correctness question rather than a tidiness one, and the answer is a real
channel — PostgreSQL `LISTEN`/`NOTIFY` is the obvious one, since the
dependency is already there.

---

### ADR-047: The agent takes one vendored WebSocket dependency, amending ADR-011

**Decision.** `agents/linux` depends on `github.com/coder/websocket`, vendored
into the repository. [ADR-011](#adr-011-zero-third-party-dependencies-in-the-agent)'s
"standard library only" rule is amended to "the standard library, plus
dependency-free libraries that are vendored and reviewed", with this as the
only entry.

**Why the rule had to give.** The standard library has no WebSocket client, and
the agent needs a persistent connection the controller can push a close frame
down. The three options were:

1. **Hand-roll RFC 6455.** Framing, masking, continuation frames, close
   handshakes and ping/pong, written once and maintained by nobody, on the one
   code path that parses untrusted input from the network. The worst of the
   three by a wide margin — "no dependencies" is not a security property if
   the thing replacing them is a protocol parser nobody reviews.
2. **Long-poll over `net/http`.** No new dependency, and a permanently worse
   protocol: no server-initiated frames, so a revocation could not cut an
   agent off until its next poll.
3. **One audited, dependency-free library.**

**Why this library.** `go mod graph` shows it requiring nothing but a Go
version — **zero transitive dependencies** — so the supply chain grows by one
module rather than by a tree.

**Why vendored.** The source that compiles is in this repository, reviewable in
a diff, and does not depend on a module proxy being reachable or honest at
build time. CI runs `go mod vendor` and fails on a diff, so the vendored tree
cannot drift from `go.mod` or be edited in place.

**Cost.** 200 KiB of third-party source in the repository, and a dependency to
track for advisories. Accepted, and much the smaller cost of the three.

---

### ADR-048: Five collection modules in the first delivery, not two

**Decision.** The Phase 3 module registry declares five modules — `cpu`,
`memory`, `swap`, `load` and `uptime` — rather than the two (CPU and memory)
that the minimum useful product needs.

**Why more than the minimum.** Each of the extra three exists to force a design
question that two modules would have let us answer wrongly and discover later:

* **`swap` can be legitimately absent.** A host with no swap has no swap
  usage, and that is not zero. With only CPU and memory, every module always
  produces a value, and the "a module that collects nothing is not a module
  reporting zero" rule would have been a sentence in a document rather than a
  path with a test. The agent returns no samples for a swapless host and the
  interface renders an absence — a distinction that is very hard to retrofit
  once something downstream assumes every enabled module yields a number.
* **`load` has a different interval floor.** Five seconds rather than one,
  because the kernel's load average is itself a smoothed value and sampling it
  faster measures the smoothing. One floor shared by every module would have
  become a constant, and the per-module floor is what makes the refusal in
  `PUT …/monitoring/modules/load` a real rule rather than a formality.
* **`uptime` is the only module whose value is a counter rather than a
  gauge**, and the only one whose natural interval is minutes. It is what
  proves the schedule is per-module rather than per-host.

**What it cost.** Three more collectors, three more sets of `/proc` fixtures,
and a contract file that has to stay in step across two languages. All of it
mechanical.

**What it bought.** The registry is a closed set with five genuinely different
entries, so the generic paths — resolution, validation, scheduling, ingestion,
rendering — were written against variety from the start instead of against two
near-identical cases.

---

### ADR-049: Configuration travels on the agent's existing connection

**Decision.** An agent receives its monitoring configuration as messages on the
WebSocket it already holds (`config_available`, `config_request`,
`config_snapshot`, `config_ack`, `config_nack`), not by polling an HTTP
endpoint.

**Why not an endpoint.** A `GET /agents/configuration` would have been simpler
to write and worse in three specific ways:

1. **Latency would be the poll interval.** Enabling a module is an action an
   administrator takes while watching the screen, and "it will start within
   five minutes" is a different product from "it starts now". The controller
   cannot dial out
   ([ADR-018](#adr-018-agent-initiated-websocket-replaces-command-polling)), so
   the only way to push is down a connection the agent opened.
2. **It would be a second authentication surface** for the same credential, on
   a path that returns the agent's own configuration. The socket is already
   authenticated, already epoch-guarded, and already closes on revocation; a
   polling endpoint would need all of that again, correctly, separately.
3. **Acknowledgement would have nowhere to go.** The thing that makes desired
   state distinguishable from applied state (§23) is the agent reporting what
   it is running. On a connection that is a message; over polling it would be
   a second endpoint that writes — and the monitoring protocol deliberately
   has no general write surface for agents beyond telemetry and configuration
   acknowledgement ([`SECURITY.md`](SECURITY.md)).

**Cost.** The message set grew by five, and the socket loop has to handle
configuration and telemetry on one connection — which it does, and which is
asserted by a test that exercises both in one session.

---

### ADR-050: The configuration checksum is computed over the transmitted bytes

**Decision.** The controller stores a configuration revision as the exact
UTF-8 byte string it will send, and the checksum is SHA-256 over **those
bytes**. The agent verifies the checksum over the bytes it received, before
parsing them. Both sides canonicalise identically: sorted keys, no whitespace,
no floating-point values.

**Why over bytes and not over the document.** Hashing a parsed structure means
each side hashes its own idea of that structure, and the two ideas are written
in different languages. Python's `json.dumps` and Go's `encoding/json` do not
agree on key order, on whitespace, or on how a number is spelled — so a
document-level hash would match until the day a value serialised differently,
and then it would report corruption that had not happened. Hashing the wire
form removes the question: there is one byte string, and either it arrived
intact or it did not.

**Why no floats.** `0.1` has no exact binary representation, and the two
languages print it differently in some ranges. Every numeric value in a
configuration document is therefore an integer — intervals in whole seconds,
jitter in whole seconds. That is a real constraint on the schema, accepted
deliberately: sub-second collection is not a homelab requirement, and a
checksum that silently disagrees across languages is a debugging session
nobody should have.

**How it is kept honest.** `contracts/monitoring-modules.json` and
`contracts/calendar-cases.json` are generated from the Python side, committed,
and compared against the Go implementation by `make check-contract` — so the
two canonicalisations cannot drift without CI failing. A revision also carries
a second checksum over the *content* only, excluding `generated_at`, which is
what makes regeneration idempotent: saving a setting that resolves to the same
configuration produces no new revision and pushes nothing.

---

### ADR-051: The agent's telemetry queue is in memory, with no disk spool

**Decision.** Samples the agent has collected but not yet delivered are held in
a bounded in-memory queue. When it is full the **oldest** are dropped and
counted. There is no disk spool, and the agent does not persist telemetry
across a restart.

**Why bounded.** §16: an unbounded queue is a memory leak with a delay. An
agent on a host that cannot reach its controller for a week must not consume
the memory of the machine it is monitoring — a monitoring agent that causes the
outage it would have reported is the worst possible failure.

**Why oldest-first.** When a choice has to be made, recent data is worth more:
an operator looking at a chart after an outage wants to know what is happening
now, and the gap is information too. Dropping the newest would keep a record of
the start of the incident and lose the present.

**Why no disk spool.** It was considered and rejected for this phase:

* It needs its own bounded size, its own corruption handling, its own
  fsync policy and its own upgrade story for the on-disk format — four new
  failure modes on the agent, which is the component that runs unattended on
  machines nobody logs into.
* The agent writes to `/etc/hcc-agent` and nothing else. A spool means it
  writes data, which means disk-full becomes an agent failure mode, on a host
  whose disk the controller is supposed to be *watching*.
* The data it would save is the data least worth saving. A restart is usually
  seconds; a week-long outage overflows any plausible spool anyway.

**What is claimed.** At-least-once delivery for what the queue holds, with
idempotent writes making a re-send a no-op. **Exactly-once is not claimed and
is not provided**: SIGKILL loses the queue. `scripts/reliability-e2e.sh`
asserts exactly that distinction — that stored data survives and collection
resumes, not that nothing was ever lost.

**What is reported.** Every heartbeat carries `buffered_items` and
`dropped_items`, so loss is a number the controller records rather than
something the operator infers from a gap in a chart.

---

### ADR-052: PostgreSQL native partitioning, not TimescaleDB

**Decision.** Metric storage uses PostgreSQL's own declarative range
partitioning on `recorded_at`, with a controller-side rollup job. TimescaleDB
is not a dependency.

**Why this is an ADR at all.** TimescaleDB is the obvious answer for time-series
in PostgreSQL, and continuous aggregates would replace the rollup job entirely.
Not adopting it needs a reason, and "we did not get round to it" is not one.

**Why not.** The deciding factor is deployment, not capability:

1. **It is an extension**, so it has to be installed in the database the
   operator already runs. A homelab controller is often pointed at an existing
   PostgreSQL — a container, a NAS package, a managed instance — and requiring
   an extension turns "give me a connection string" into "rebuild your
   database server". That is a large ask for a self-hosted tool.
2. **The rollup job is testable in process.** Continuous aggregates are
   configured in SQL and run on the database's schedule, so verifying them
   means asserting against a background worker somebody else owns. The job
   here is a function with a `now` parameter, which is why
   `tests/test_metrics_maintenance.py` can drive a year of retention in
   milliseconds.
3. **Measured, PostgreSQL holds.** 4,497 samples a second through the full
   validation path, 88 bytes a sample including indexes, range queries at
   0.6–0.8 ms touching one partition of twelve. See
   [`PERFORMANCE.md`](PERFORMANCE.md). Those are not fleet-scale numbers, and
   they are enough to say the plain implementation is not the bottleneck yet.

**What it costs.** Partition creation, rollups and retention are code this
project maintains — roughly 700 lines with its own test file — rather than
features somebody else maintains. Two defects in it have already been found
and fixed (see the Phase 3 history), which is the honest price of the choice.

**When to revisit.** If a fleet makes the rollup job the bottleneck, or if
compression becomes necessary before the hourly tier's two-year retention is
affordable. The schema is deliberately compatible: `metric_samples_raw` is a
plain partitioned table, so converting it to a hypertable later is a migration
rather than a redesign.

### ADR-053: The agent binary is its own installer; there is no installation script

**Decision.** `hcc-agent` grows `install`, `upgrade`, `uninstall` and `doctor`
subcommands. No shell script installs the agent, and none will be added. The
artifact a person verifies with `sha256sum -c` is the artifact that performs
the installation and then *is* the installed program.

**Why.** The alternative everybody reaches for, `curl … | sudo bash`, fails
three ways at once:

1. Unaudited code runs as root without ever being seen.
2. Whatever authenticates the install usually arrives as `-s -- --token XYZ`,
   which is argv, which `/proc/<pid>/cmdline` makes readable by every local
   user on the machine.
3. The whole command lands in `~/.bash_history`, credential included.

A script that is downloaded, verified and read first fixes all three and
introduces a second problem: you have verified the **script**, which then
downloads the **binary** — two artifacts to trust where one would do.

Making the binary the installer leaves one artifact and one digest. It also
moves the work — create a user, place a binary, write a unit, set modes — into
a language with types and tests rather than forty lines of shell every
operator retypes slightly differently.

**`--dry-run` is the half that makes this an improvement rather than a
preference.** It prints every path, every mode, the service account and the
complete unit file, and changes nothing. That is the audit step a pipe into a
shell structurally cannot offer, and it costs one flag.

**Cost.** The agent carries installation logic it only runs once, and
`internal/install` is about 1,400 lines of the agent's source. Bounded by
being behind a [System](../agents/linux/internal/install/system.go) interface
with every path rooted, so the ordering, the refusals and the rollback are
unit-tested without root — and by the installation matrix, which starts the
real service in a real container because a mocked systemd proves nothing about
systemd.

### ADR-054: The systemd unit has exactly one copy, embedded in the binary

**Decision.** `agents/linux/internal/install/hcc-agent.service` is the unit,
embedded with `go:embed` and written verbatim by `install`.
`agents/linux/deploy/hcc-agent.service` was **deleted** rather than kept as a
documented copy.

**Why.** Once an installer owns the unit, a second copy exists: one embedded
in the binary and one for humans to read. Two copies drift, and the one people
read would be the stale one — so the documentation would describe hardening
the installer does not apply, or vice versa.

The alternatives, and why not:

* *A generated copy with a drift check*, like the OpenAPI snapshot and the
  monitoring contracts. Workable, and a third mechanism for a file that has
  exactly one consumer.
* *Keep the deploy copy as the canonical one and read it at runtime.*
  `go:embed` cannot reach outside its package, and a unit read from disk is a
  unit that can be missing.

`install --dry-run` prints the embedded bytes, so the way to read the unit
without installing it is to ask the installer — which is also the only way to
be sure you are reading what would be installed.

**Cost.** The deployment README can no longer show the unit inline; it links
to the embedded file and names the dry run. A reader browsing the repository
has to open a file under `internal/`.

**What it enabled.** The move is how `ProcSubset=pid` was found. Reading the
unit as *the thing about to be installed on every host* rather than as
documentation prompted the question "what does this directive hide?", and the
answer was every file the agent reads.

### ADR-055: The controller serves the web client; one origin, one port

**Decision.** The control plane serves the built client from the same port as
the API. `/api` is the API, `/healthz` and `/readyz` are the probes, and
everything else is the client. `HCC_WEB_ROOT` is unset in development, where
Vite serves the client and `cors_origins` names it.

**Why.** The security model already assumed this and had no implementation of
it. Session cookies are `HttpOnly`, `SameSite=Lax` and `Secure`;
state-changing requests need a session-bound CSRF token *and* a matching
`Origin` ([ADR-030](#adr-030-csrf-via-a-session-bound-double-submit-token)); the
production CORS list is empty because a same-origin client needs no exception.

A second origin for the client would need either a credentialed CORS
allow-list — the arrangement `_validate_cors_origins` refuses a wildcard for —
or a second name and a second certificate for one deployment.

**The refusals are the design.** A catch-all that returned `index.html` for
anything unmatched would answer `/readyz` with a 200 HTML page, so a broken
deployment would report itself healthy to a monitoring check; and
`/api/v1/typo` would become a 200 page, so a client bug would look like a
rendering bug. Paths under the API prefix and the operational probes get the
application's own JSON 404 instead.

The fallback serves one fixed file and never a path taken from the request,
which is what makes traversal structurally impossible rather than checked.

**A missing build is a startup failure**, not a mount that is quietly skipped.
The alternative is a container that answers `/readyz` with "ready" and every
page with a 404 — healthy, and useless.

**Cost.** The controller is now also a static file server, with two opposite
cache policies to keep straight: hashed assets immutable for a year, and
`index.html` `no-store` because it is the file that names them. One process
serving both means the client cannot be scaled or cached separately, which is
not a homelab problem.

### ADR-056: The controller is the trust anchor for agent downloads

**Decision.** The controller ships a manifest of the agent releases it was
built alongside, and the interface renders an install command carrying the
expected SHA-256 of the archive it tells somebody to download.
`GET /api/v1/agent-releases` serves it, Administrator-only. The controller
does **not** fetch checksums at runtime.

**Why.** The administrator is already authenticated to the controller over
TLS — it is the thing holding their fleet's credentials. So the controller,
not the download origin, is what should vouch for the binary. Three
consequences, all of them the point:

1. **No runtime dependency on the download origin for trust.** The controller
   does not go and look; it already has the digests.
2. **The digest travels inside the session the administrator is already in.**
   An attacker who controls the mirror, or the network between the host and
   it, cannot make a substituted tarball match.
3. **Air-gapped and mirrored installs work.** Point the manifest's `url` at an
   internal mirror; the digest still pins the bytes.

`cosign verify-blob` and `gh attestation verify` are documented and are
deliberately **not** on the critical path: requiring a verification tool on a
NAS to install a monitoring agent means nobody verifies anything. The
checksum path needs coreutils.

**Absence is a first-class answer.** No release of this project has been
published, so the endpoint reports `available: false` with a reason and the
interface shows the build-from-source path. It does not render a plausible
download command for a URL that would 404 — a digest beside an invented URL is
a claim nobody can check, which is worse than no claim.

**What it forced.** Both manifest fields are interpolated into a command
somebody pastes into a root shell, so both are refused if they contain a
character that means something to a shell. See
[the installation-platform review](security/2026-10-14-installation-platform-review.md).

**Cost.** A manifest to generate and ship with every release, and a schema
version to maintain. `scripts/build-release-manifest.py` derives it from the
build's own `artifacts.json`, so the digests are never transcribed.

### ADR-057: Migrations run on container start, before the port is bound

**Decision.** The container entrypoint runs `alembic upgrade head` and then
execs uvicorn. A container that cannot migrate exits non-zero and never serves
traffic. `HCC_AUTO_MIGRATE=false` exists for an operator who would rather run
`docker compose run --rm hcc migrate` and watch it.

**Why.** It is what makes `docker compose up -d` an installation rather than
the first of two steps. An `up -d` that left an empty database and a confusing
error is not a deployment path.

**Why this is safe here specifically.** One controller process is the rule
([ADR-046](#adr-046-one-controller-process-stated-rather-than-enforced)), so
there is no second replica racing the same migration. A deployment that ever
becomes multi-replica has to move this to a one-shot service — and that is the
point at which ADR-046 gets superseded, not quietly violated.

**Why before binding and not after.** Migrating in a background task after
binding produces a deployment that answers requests against a schema it is
halfway through changing. The port is the signal that the schema is ready.

**Cost.** Startup is as slow as the slowest migration, and a failed migration
is a container that will not start — which is the intended behaviour and is
also the failure an operator has to read `docker compose logs` to understand.
The health check's `start-period` covers it.

### ADR-058: The deployment generates its own database credential, into a file

**Decision.** A one-shot service writes 32 random bytes to
`./secrets/db_password` on first start and leaves it alone afterwards. Both
PostgreSQL and the control plane read the **file**. The operator never chooses
or sees a database password, and `.env` holds no secret.

**Why not ask.** An operator told to invent a password will reuse one or pick
`postgres`, and a documented default ends up baked into every copy of the
documentation.

**Why a file and not an environment variable.** A container's configured
environment appears in `docker inspect`, and `docker inspect` is what people
paste into issue reports. A file does not.

**Why the application assembles the URL, not the entrypoint.** It was the
entrypoint, which read the file and exported `HCC_DATABASE_URL` before exec-ing
the server. That worked for the server and broke the one step
[ADR-033](#adr-033-the-initial-administrator-is-created-by-a-local-command-not-an-endpoint)
makes mandatory: `docker compose exec hcc hcc bootstrap-admin` does not run an
entrypoint, so it saw no URL and failed with "Field required" on a working
deployment. The assembly is a `mode="before"` validator in `hcc/config.py`, so
every process in the container gets it — including alembic.

**Three details that are not incidental.**

* The password is base64**url**. It goes into a URL's userinfo, where a `/`
  changes which host is addressed and the resulting failure is a connection
  error about DNS. Startup refuses a password outside `[A-Za-z0-9_-]` rather
  than trusting the generator stayed correct.
* The file is mode `0644`, not `0600`. Three containers with three different
  uids read it, so a `0600` file owned by one of them is unreadable by the
  other two — `0600` there would not be stricter, it would be broken. The
  protection is the deployment directory and the mount being read-only in both
  consumers, which is the same guarantee Docker's own `/run/secrets` gives.
* The mount carries `:ro,z`. Without the SELinux relabel the deployment fails
  on Fedora, RHEL, CentOS Stream, Rocky and Alma with "Permission denied" on a
  file that is plainly `0644`.

**There is no application signing secret to manage**, and that is worth
protecting in review. Sessions are server-side and the cookie carries an
opaque random token of which only the SHA-256 digest is stored
([ADR-027](#adr-027-session-tokens-are-hashed-with-sha-256-not-argon2),
[ADR-028](#adr-028-server-side-sessions-not-jwt)) — nothing is signed, so
nothing needs a key, so there is no key to rotate or leak.

**Cost.** `./secrets/db_password` is not in the database dump and a restore
into an existing volume needs it, so it is one more thing to back up — stated
in the backup's own manifest rather than only in the documentation.

---

### ADR-059: Compression is an allow-list, because BREACH is a length attack

**Decision.** Two things are compressed and nothing else is: `/assets/*`, at
the static mount, and the four metric read endpoints, by an explicit pattern in
`hcc/compression.py`. There is no application-wide `GZipMiddleware`.

**Why compress at all.** Measured — the numbers are in
[`PERFORMANCE.md §5`](PERFORMANCE.md#5-what-the-control-plane-sends-a-browser-606-kb-a-first-load-now-181-kb).
A first load of the sign-in page was 606 kB and is now 181 kB, and a host
page's four charts are 237 kB uncompressed against 24 kB gzipped. The second
figure is the one that recurs: assets are cached for a year, chart data is
re-fetched every time somebody opens a page.

**Why not everything.** Compression and secrecy interact. If a response holds
both a secret and text the attacker chose, the compressed *length* reveals how
well the two matched, so an attacker who can trigger the request repeatedly and
watch the sizes recovers the secret a character at a time. That is BREACH, and
it makes `Content-Encoding` a security decision rather than a tuning knob.

This application has exactly one response body containing a secret — the
enrollment credential, which `hcc/api/v1/hosts.py` already documents as "the
only response in the API that contains a secret". An allow-list keeps it that
way by construction. A deny-list would be one forgotten entry away from
compressing the next secret somebody adds, and the failure would be silent;
`test_compression.py` asserts the pattern cannot be widened by a crafted path
and that the list has exactly one entry, so growing it is a deliberate change
to a test rather than an edit nobody reviews.

**Why this is not a new exposure.** `compose.tls.yaml` runs Caddy with
`encode zstd gzip`, which already compressed every proxied response including
these. The recommended deployment has had compressed API responses all along;
this brings the default one — which has no proxy — in line with it. Session
cookies are `SameSite=Lax` and state-changing requests need a session-bound
CSRF token with a matching `Origin`
([ADR-030](#adr-030-csrf-via-a-session-bound-double-submit-token), and
`hcc/api/v1/auth.py` for the cookie attributes), so an attacker's page cannot
drive authenticated requests from its own origin — which is the delivery
mechanism the attack needs.

**Why the asset compression is at the mount, not in the stack.** Same reason,
expressed structurally: a `GZipMiddleware` added to the application would
compress the API too, and the next person to read `app.py` would have no way to
see that this was a decision. Wrapping the mount puts the scope in the same
place as the thing it applies to.

**Cost.** CPU per cache miss, argued rather than measured: the responses are
`immutable` for a year, so a browser fetches each one about once per release.
`PERFORMANCE.md §6` lists it as unmeasured rather than implying otherwise.


### ADR-060: The publisher signs releases; the controller only authorizes them

**Decision.** A release is established by a detached Ed25519 signature over the
release manifest's exact bytes, made by a key the publisher holds and the
controller does not. The agent carries the public key, stamped in at link time
by `scripts/build-agent-artifacts.sh --trust-key`, and takes the expected
SHA-256 for its architecture **out of** the verified metadata. The controller
decides *whether* a host is offered an update and *when*; it does not get to
say what the bytes are.

**Why not have the controller vouch for the binary.** It already does, for the
*fresh install* command — the digest an administrator pastes travels inside
their own authenticated session, and `hcc/agents/releases.py` argues at length
that this is the right trust anchor for a human running a command. That
argument does not survive automation. An unattended agent installing as root
from a digest the controller supplied would mean a compromised control plane
could install arbitrary code on every host in a fleet — and the control plane
is the most likely thing to be compromised, because it holds the fleet's
credentials and is reachable from a browser.

So the controller is deliberately demoted for this one purpose. An attacker who
owns it can withhold updates and offer an older *signed* release. They cannot
choose the bytes.

**Why detached, and over exact bytes.** Two JSON libraries do not agree on key
order, integer rendering or non-ASCII escaping, so a signature over a
re-serialised document would turn a formatting difference into something
indistinguishable from tampering. The manifest therefore travels as a *string*
through the protocol and is read from disk verbatim, never rebuilt from the
parsed model.

**Why a stamped key rather than a configured one.** A trust anchor that lives
in configuration is a trust anchor the configuration can be made to change. An
operator who signs their own agents adds a key with `HCC_AGENT_RELEASE_KEYS`,
which is *added* to the stamped one and needs root on the host to set — and
the privileged half of an update deliberately ignores it, trusting only what
the binary was built to trust.

**Consequence accepted.** A build with no stamped key trusts nothing and
declines every update, with that as the reason. That is every build of this
repository that is not an official release, and it is correct: such a binary
genuinely cannot tell a real release from a forged one. Four tests hold that
line.

**Cost.** A publisher key that leaks lets its holder install arbitrary code as
root on every host that trusts it. The same property every signed distribution
has, with the same mitigations — the key lives off the build machine, `keygen`
writes it mode 0600, and the trust store is a list so rotation can overlap.
There is no revocation. See
[the update management security review](security/2026-10-14-update-management-review.md).

### ADR-061: A path-triggered root unit installs updates, because the agent is unprivileged

**Decision.** `hcc-agent.service` stays unprivileged. When an update is
authorized, the agent fetches, bounds, hashes and verifies the archive itself,
then writes a typed request to `/var/lib/hcc-agent/update-request.json`.
`hcc-agent-update.path` notices it and `hcc-agent-update.service` — root,
one-shot, `ExecStart=/usr/local/bin/hcc-agent apply-update` — performs the
install.

**Why anything was needed.** An unprivileged process cannot replace a
root-owned binary in `/usr/local/bin` or restart its own unit. The first real
run of `scripts/update-e2e.sh` ended with "upgrading needs root and this
process is uid 996" after every other part of the update had worked. Nothing
in 1200 unit tests could have found that: they all inject the installer.

**What was rejected.**

| Option | Why not |
| --- | --- |
| Run the agent as root | a long-lived network-facing root process, for a monitoring agent |
| A setuid helper | a new attack surface, famously hard to get right |
| Polkit rules | per-distribution configuration, a surprising amount of it |
| A D-Bus service | a daemon to write and harden |

systemd is already pid 1 and already watches paths. The trigger is a file an
administrator can read with `cat`.

**Why this is not "the agent account can run anything as root".** The request
carries the publisher's signed metadata and its detached signature **and no
digest**. `apply-update` verifies that signature itself and derives the
expected hash from the verified metadata, so there is nothing in the request
whose author chooses what the bytes should be. The privilege granted is exactly
"cause root to install a genuinely published, publisher-signed release".
`TestThereIsNoDigestFieldToSupply` asserts the field is absent from the type
rather than merely rejected.

Defence in depth on top, each with its own test: the archive must be a regular
file (`lstat`, so a symlink is refused rather than followed) inside the state
directory; the request must not be world-writable; unknown JSON fields are
refused because ignoring one is how a check gets skipped; the schema version is
checked because an update that replaces the agent mid-flight may be read by a
different build than wrote it.

**Why the request is removed before the install, not after.** `PathExists`
re-triggers while the file is there, so a crash mid-install with the request
present would be an install loop — on a service that replaces its own binary,
which is the worst available failure. Removing first means a crash costs one
failed update.

**Why the staging directory is `/var/lib`, not `/tmp`.** The unit sets
`PrivateTmp=yes`, so an archive the agent stages under `/tmp` is in a mount
namespace root cannot see. `StateDirectory=hcc-agent` is the shared ground, and
the handoff refuses to proceed if the archive is anywhere else rather than
letting root fail with "cannot stat".

### ADR-062: No process reports its own successful replacement

**Decision.** The states an agent can report during an update stop at
`restarting`. The controller concludes the job when the replacement connects:
the target version means `succeeded`, the previous version means
`rolled_back`, and a third version means `failed` with "something changed this
host outside the update system". `restarting → succeeded` is therefore an
allowed transition and `installing → succeeded` deliberately is not.

**Why.** The install ends with `systemctl restart`, which terminates the
process performing it. There is no moment at which the old agent can observe
that the new one works, and a report claiming otherwise would be a claim
nothing checked. The reconnection *is* the health evidence.

**Why `installing → succeeded` is excluded.** An agent that never said it was
restarting might have died for an unrelated reason — a crash, an operator
stopping the service, the host rebooting. Concluding `succeeded` from a
reconnection in that state would turn an unrelated restart into a successful
update.

This had two consequences that were found by running it rather than by
reasoning. `install.Upgrader` reported `restarting` *after* the systemctl call,
so the report never left the dying process and the job held `installing` — a
state nothing could conclude. And the handoff's reports were merely *queued*
onto a buffered channel, so they died in the buffer for the same reason; frames
whose delivery matters now carry an acknowledgement and the reporter waits for
the write.

**Consequence for the interface.** Progress is derived from the last reported
state, never from elapsed time. A stuck download shows a bar that does not
move, which is the true and useful answer; a bar that advanced on a timer would
be inventing progress and somebody would believe it.

### ADR-063: A root-owned version floor, in a sibling directory

**Decision.** Each host stores a monotonic anti-rollback floor — the oldest
version an update may target — in `/var/lib/hcc-agent-update/`, a directory
owned by root and **not** inside the agent's own state directory. The floor
rises only when an install reaches a health-checked conclusion. No field in an
update instruction lowers it; `allow_downgrade` is bounded by it. The only way
down is `hcc-agent upgrade --allow-downgrade --version <v>`, which needs root
on the host.

**Why a floor at all.** [ADR-060](#adr-060-the-publisher-signs-releases-the-controller-only-authorizes-them)
stops a compromised controller installing a binary the publisher did not
build. It does not stop one installing an *old* binary the publisher did
build, and a signed release from before a fix is exactly what an attacker
holding the control plane would choose. A signature is not a freshness
statement: the one on 0.1.0 is as valid today as the day it was made.

**Why a sibling and not a subdirectory.** Permission to rename or delete a
directory entry comes from the **parent** directory. `/var/lib/hcc-agent` is
owned by the unprivileged service account by design — the agent stages
downloads there — so a root-owned `trust/` inside it could be renamed away by
that account and replaced with one it wrote. The only fix within that layout
is a sticky bit, which is a subtle property one `chmod` undoes. A sibling
needs write access to `/var/lib`, which the agent does not have.

**Why it rises on success and not on attempt.** Raising it when an install is
*attempted* would mean a host that failed to reach 0.2.0 could no longer be
sent 0.1.5 either, and the only way out would be root on the machine. So the
floor follows what has actually run.

**What it does not claim.** Nothing here resists a compromised root account,
which can rewrite the floor, replace the binary or remove the unit. The
property defended is narrower and worth stating exactly: a compromised
*controller*, or a compromised *unprivileged agent account*, cannot move a
host to an older release.

### ADR-064: The privileged installer works from its own copy of the archive

**Decision.** Before verifying or extracting anything, the privileged update
unit copies the staged archive out of `/var/lib/hcc-agent` into its own
root-owned directory, and installs from that copy. The upgrader additionally
reads the archive through a single file descriptor rather than opening the
path twice.

**Why.** An upgrade reads the archive twice — once to hash it against the
digest in the publisher's signed metadata, once to extract the binary. The
directory it was staged in belongs to the unprivileged agent account, so
those two reads can see different bytes: the digest is verified against an
archive that is then not the one installed. That turns "the agent account is
compromised" into "root executes the attacker's binary", which is precisely
what the handoff exists to prevent.

**Why both mechanisms.** A single descriptor closes the *rename* form of the
substitution, because a descriptor is pinned to an inode. It does nothing
about `open(O_TRUNC)` and a rewrite, which modifies that same inode — a fact
established by writing the test first and watching a descriptor-only
implementation fail it. The copy into a directory no unprivileged account can
write closes both, and a modification made *during* the copy produces a digest
mismatch, which is a refusal.

**Cost.** One extra copy of a ~3 MB archive per update, deleted when the
install finishes.

### ADR-065: The agent divides by the interval, not the controller

**Decision.** Disk throughput, disk IOPS and network throughput are stored as
**gauges already expressed per second**, computed by the agent from the
difference between two of its own readings. The kernel's monotonic counters
are never sent. Four new units carry them: `bytes_per_second`,
`operations_per_second`, `packets_per_second`, `bits_per_second`.

**Why not store the counters and derive rates at query time.** The agent is
the only component that can tell a counter which *reset* from one that
advanced. Given two readings from either side of a reboot, a device
re-enumeration or a 32-bit wrap, the controller can only subtract them —
producing a negative rate, or a spike of several gigabytes per second at the
exact moment somebody is trying to work out what happened. The agent holds
the previous reading and knows it came from the same boot, so it can do what
the CPU collector already does: produce **nothing** for that interval.

**Why not have the agent divide by its configured interval.** Because the
scheduler is not a clock. A host under load fires a 10-second job at 10.4
seconds, and a rate computed against 10 would read 4% high — consistently,
and invisibly. The agent measures the elapsed time between its own two
readings and divides by that.

**Consequence.** A rate series is averageable, so the existing rollups are
correct for it without a special case, and a gap in the raw series means
"not measured" exactly as it does everywhere else. The cost is that the
underlying totals are not recoverable: "how many bytes has this disk written
since Tuesday" is not a question this schema can answer. That is the right
trade for a monitoring product — nobody asks it — and it is stated here so
the absence is a decision rather than a surprise.

### ADR-066: Collector failure is stored against the module, not inferred from absence

**Decision.** A new table, `host_module_health`, holds one row per host per
module: when the controller last heard about it, when it last collected
without an error, when it last failed, the agent's own words for that
failure, and how many collections have failed since the last success.
Upserted on the ingestion path in the same transaction as the samples.

**Why a table and not an inference.** "This module has no recent samples"
has four causes with four different remedies — nobody enabled it, the agent
is offline, it was enabled a second ago, or the collector is raising an
error every interval — and only the first three were answerable. The fourth
is the interesting one and was entirely invisible: a removed disk, a stopped
Docker daemon or a `systemctl` that started refusing produced an enabled
module, a connected agent, no data and no explanation, indefinitely.

**The protocol already carried it.** `TelemetryEntry.error` has existed
since phase 3 and its own description says "a module that has been failing
for an hour is information an operator needs". `MetricsService.ingest` read
`entry.samples` and discarded the rest. This ADR is mostly a record that the
field is now read.

**Why not a column on `host_module_overrides`.** That row is what the
operator *wants*; this one is what is *happening*. A module inheriting the
global default has no override row at all and still has health, and
resetting an override deletes a row that must not take the failure history
with it.

**Why every timestamp is the controller's clock.** Unlike
`metric_series.last_sample_at`, which records when the agent says it took a
measurement. A host whose clock is four hours out is one of the conditions
this table reports — the controller refuses every sample it sends — and a
diagnostic timestamped by the thing being diagnosed is not a diagnostic.
That case is recorded too, with the controller's own words, because from
inside that host nothing is wrong and the agent cannot report it.

**Consequence.** An error string from a possibly-compromised host is stored
and rendered in a browser. It is bounded twice (the protocol refuses a
longer frame; the service truncates before the UPSERT), stored verbatim
rather than pre-escaped, and escaped by React at the point of rendering. A
collection that ran and produced *no samples* is a **success** — a rate
collector's first interval legitimately produces nothing — and the error
survives a later success so that a module which fails and recovers every few
minutes is visible rather than self-erasing.

### ADR-067: The rollup bound is a bound on series, and the lag is reported

**Decision.** `MAX_BUCKETS_PER_RUN` stays at 5,000. The maintenance pass now
reports `rollup_lag_seconds` per tier and logs `metric_rollup_behind` when a
tier is more than three bucket widths behind.

**The measurement.** The five-minute tier writes one row per series per
five-minute bucket, and the job runs every five minutes, so a steady-state
pass has exactly one bucket per series to write. The bound is therefore a
bound on **series in the installation** — and since five of the eleven
modules are label-keyed, that is far fewer hosts than it sounds:
`scripts/measure-metrics.py --profile full` measures 164 series on a
well-populated host, so the ceiling is **thirty hosts** with everything
enabled, against 357 on the two-module profile the earlier figures used.

Past the ceiling the job does not degrade, it never catches up: each pass
writes 5,000 of the buckets that accrued and falls behind by the remainder,
every five minutes, for ever. The tiers keep answering queries, about last
week, and nothing in the data shows it.

**Why not raise the bound.** 5,000 buckets took about half a second in the
benchmark, so the 82,000 that 500 full-profile hosts would need is an
eight-second transaction every five minutes. That is a different trade from
the one the constant currently makes — progress every run rather than one
enormous transaction — and the contention it would cause with live ingestion
is exactly what has not been measured. The likely eventual answer is a
**time-bounded** pass rather than a larger count.

**Why the lag is derived rather than remembered.** `RetentionService`
declares itself stateless and `__slots__` enforces it, which is what makes
it restart-safe. A consecutive-capped-runs counter was tried first and
failed immediately and correctly. The lag is read from the database: the
newest bucket each tier holds against the newest the *data* says it should.

**Measured against the data, not the clock.** A series with gaps has no
bucket for a window nothing sampled, so comparing the watermark with "now
minus the grace period" reports a caught-up tier as permanently five minutes
behind. The first version did exactly that.

### ADR-068: The browser's module fixture is generated, not transcribed

**Decision.** `apps/web/src/test/module-catalogue.generated.ts` is written by
`scripts/contracts.sh` from the controller's registry, and
`make check-contract` fails when the committed file is stale.
`FAKE_MODULES` re-exports it.

**Why.** It was a hand-written copy of `contracts/monitoring-modules.json`,
kept in step by a comment asking the next reader to keep it in step. Six
modules landed in the registry during Operation Horizon and the fixture
claimed five throughout — so every browser test ran against a five-module
world, a page that renders eleven rows was never rendered with eleven, and a
test seeding one of the new modules asserted against something the fixture
had never heard of. **Nothing failed**, because the only thing that could
have noticed the drift was the copy itself.

**Why not import `contracts/monitoring-modules.json` directly.** The client
image is built from `apps/web` alone, so a file importing
`../../../../contracts/` type-checks locally and breaks `docker compose
build` with "Cannot find module". The generated file lives inside the build
context; a comparison test that read the contract with `node:fs` would also
have needed `@types/node`, a dependency added for a fixture.

**Consequence.** `description` was added to the contract document so the
fixture could be generated whole. The Go agent ignores it — `encoding/json`
reads named fields and skips the rest — and the contract test's struct is
unchanged.


---

## 14. Open design questions

Recorded rather than silently deferred. Each needs an answer before the
milestone that depends on it, not before the next commit.

| # | Question | Blocks | Current leaning |
| --- | --- | --- | --- |
| 1 | ~~Does storage volume at realistic fleet size hold up?~~ **Answered in Phase 3.** Measured: 88 bytes per raw sample including indexes, 30.5:1 compression into the five-minute tier, 4,497 samples/s ingestion. See [`PERFORMANCE.md`](PERFORMANCE.md) and [ADR-052](#adr-052-postgresql-native-partitioning-not-timescaledb). Not fleet-scale numbers, and enough to freeze the schema. | — | Resolved: PostgreSQL native partitioning |
| 2 | Per-host role assignment — should a user be an Operator on some hosts and a Viewer on others? | Multi-user milestones | Likely yes, as a third authorization dimension composing with ADR-023; not in the first delivery |
| 3 | Host groups as a configuration layer. [`AGENT_ARCHITECTURE.md §6.1`](AGENT_ARCHITECTURE.md#61-layering) reserves the slot but defers it. | Fleet-scale configuration | Defer until someone runs enough hosts to need it; two layers cover the homelab case |
| 4 | ~~Metric identity across config changes.~~ **Answered in Phase 3.** A series is identified by `(host, module, metric, labels_key)`, so a different label set is a different series. The read API returns every series a host has ever produced, including ones that have stopped, so "no CPU data" and "CPU data until Tuesday" stay different answers. Stitching is deferred to the interface and not yet done. | — | Resolved: a new series |
| 5 | Whether `sessions.control` is worth shipping at all. Attaching to an existing session inherits that session's privileges, including root. | Session management milestone | Possibly read-only only; the control half may be more risk than value |
| 6 | ~~Agent self-update distribution.~~ **Answered.** Built, opt-in and signature-verified, and the concern is resolved by *not* making the controller a code-distribution channel: it authorizes which release and when, and a publisher signature establishes what the bytes are ([ADR-060](#adr-060-the-publisher-signs-releases-the-controller-only-authorizes-them)). Off by default on every host. See [`UPDATES.md`](UPDATES.md). | — | Resolved: the controller authorizes, the publisher signs |
| 7 | ~~Rollup computation location.~~ **Answered in Phase 3:** controller-side, in a single `INSERT … SELECT … ON CONFLICT DO UPDATE` per tier. See [ADR-052](#adr-052-postgresql-native-partitioning-not-timescaledb) for why not continuous aggregates. | — | Resolved: controller-side |
| 8 | Whether the terminal needs a break-glass path when the controller is down. Today it does not work offline, by design. | Terminal milestone | Leave it unavailable; a break-glass path is an unaudited backdoor by another name |
