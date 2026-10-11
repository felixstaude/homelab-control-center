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
| `/healthz`, `/readyz`, `/api/v1/info` | Implemented |
| PostgreSQL connectivity, Alembic chain | Implemented |
| `users` table (milestone 1.1) | Implemented — schema only, no auth |
| React shell, design system, 7 nav sections | Implemented |
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

Built to static assets and served by the control plane in production, which
makes the deployment one container and removes CORS from the production path
entirely.

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
mechanism exists for the scheduler — scaled horizontally.

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
cookie as REST.

**Agent → control plane.** Opened outbound by the agent and held open. Carries
pending-command delivery, event-driven module output (Docker lifecycle and
logs), and terminal session frames.

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

**None of this is implemented in Phase 0.** It is the design that Phase 1
builds, documented now because it constrains everything else.

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

```
1. Admin creates a host entry in the UI and gets a one-time enrolment token.
      ↓
2. Admin installs the agent on the host and writes the token to
   /etc/hcc-agent/token (mode 0600).
      ↓
3. Agent starts, POSTs /api/v1/agents/enroll with the one-time token.
      ↓
4. Control plane validates it, marks it consumed, and returns:
     - host_id
     - a long-lived agent credential
      ↓
5. Agent persists the credential and uses it for all later calls.
      ↓
6. Steady state: heartbeat, report, poll for commands — all outbound.
```

Design points:

- **One-time enrolment token, separate from the steady-state credential.** The
  short-lived token is the one that gets pasted into terminals and shell
  history; it is single-use and expires.
- **Tokens are stored hashed** (Argon2id), never in plaintext. A database dump
  does not yield working agent credentials.
- **Revocation is immediate**, server-side. Revoking a host rejects its
  credential on the next call; there is no waiting for an expiry.
- **The token is never a command-line flag.** Command lines are world-readable
  via `/proc`. It comes from a file (preferred) or the environment. Enforced
  by a test in `cmd/hcc-agent/main_test.go`.
- **The token file must not be group- or world-readable.** The agent refuses to
  start otherwise, rather than accepting a secret that is not secret.

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
- No ORM models in Phase 0. Schema arrives with the features that need it;
  speculative tables are harder to remove than to add.

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

`users` is implemented (milestone 1.1); the rest is indicative:

```
users                       IMPLEMENTED
sessions, roles
hosts ── agent_credentials
      ├─ host_metrics       (time-series, partitioned by time)
      ├─ containers ── container_metrics
      └─ log_entries        (time-series, partitioned by time)
network_devices ── device_checks ── device_relationships
notification_rules ── rule_conditions (nested)
notification_destinations
events ── notifications_sent
audit_log                   (append-only)
```

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

## 14. Open design questions

Recorded rather than silently deferred. Each needs an answer before the
milestone that depends on it, not before the next commit.

| # | Question | Blocks | Current leaning |
| --- | --- | --- | --- |
| 1 | Does storage volume at realistic fleet size hold up? The figures in [`DATA_RETENTION.md §7`](DATA_RETENTION.md#7-expected-volume) are arithmetic, not measurements. | Phase 3 schema freeze | Benchmark first ([§8 there](DATA_RETENTION.md#8-benchmarks-required-before-metric-storage-ships)); PostgreSQL expected to hold, TimescaleDB as first escalation |
| 2 | Per-host role assignment — should a user be an Operator on some hosts and a Viewer on others? | Multi-user milestones | Likely yes, as a third authorization dimension composing with ADR-023; not in the first delivery |
| 3 | Host groups as a configuration layer. [`AGENT_ARCHITECTURE.md §6.1`](AGENT_ARCHITECTURE.md#61-layering) reserves the slot but defers it. | Fleet-scale configuration | Defer until someone runs enough hosts to need it; two layers cover the homelab case |
| 4 | Metric identity across config changes: if an operator changes a module's labels, is that the same series or a new one? | Phase 3 schema | Probably a new series, with the UI stitching them for display — but it affects the schema and must be settled before it lands |
| 5 | Whether `sessions.control` is worth shipping at all. Attaching to an existing session inherits that session's privileges, including root. | Session management milestone | Possibly read-only only; the control half may be more risk than value |
| 6 | Agent self-update distribution. Phase 10 lists it as opt-in and signature-verified, but it makes the controller a code-distribution channel — the same concern as plugin question 10. | Phase 10 | Keep opt-in and signed; prefer OS packages over self-update |
| 7 | Rollup computation location: controller-side job, or PostgreSQL-side with Timescale continuous aggregates. | Phase 3 | Controller-side first (portable, testable); revisit with benchmarks |
| 8 | Whether the terminal needs a break-glass path when the controller is down. Today it does not work offline, by design. | Terminal milestone | Leave it unavailable; a break-glass path is an unaudited backdoor by another name |

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
