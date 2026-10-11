# Architecture

Technical architecture for Homelab Control Center. Describes the system as
designed, distinguishing what exists today (Phase 0) from what the design
anticipates.

- [1. System overview](#1-system-overview)
- [2. Components](#2-components)
- [3. Communication](#3-communication)
- [4. Authentication and authorization](#4-authentication-and-authorization)
- [5. Agent registration](#5-agent-registration)
- [6. Database strategy](#6-database-strategy)
- [7. Event and log ingestion](#7-event-and-log-ingestion)
- [8. Metrics retention](#8-metrics-retention)
- [9. Notification rule engine](#9-notification-rule-engine)
- [10. Docker management security model](#10-docker-management-security-model)
- [11. Network monitoring](#11-network-monitoring)
- [12. Deployment model](#12-deployment-model)
- [13. Architecture decision records](#13-architecture-decision-records)

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
└────────────────┘  └────────────────┘  └────────────────┘
```

The single most important structural property: **agents always dial out. The
control plane never connects to an agent.** Everything in [§10](#10-docker-management-security-model)
follows from it.

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
- Ingests metrics, logs and events from agents.
- Evaluates notification rules and dispatches notifications.
- Queues management commands for agents and records the audit trail.
- Performs network checks that do not require an agent.

**Deliberately stateless between requests.** All state is in PostgreSQL, so the
control plane can be restarted at any time and — once a leader-election
mechanism exists for the scheduler — scaled horizontally.

### 2.3 Agent (`agents/linux`)

A single static Go binary per monitored host.

**Responsibilities:**
- Collect host metrics from `/proc` and `/sys`.
- Discover and monitor local Docker containers via the local socket.
- Tail configured log sources.
- Execute the closed set of management commands it is sent.
- Report everything outward to the control plane.

**Explicitly not responsible for:** storing history (it is a forwarder with a
small buffer), evaluating notification rules, or deciding what matters. An
agent that evaluated rules locally would mean fleet-wide rule changes requiring
a fleet-wide agent rollout.

**Structural constraints:** no listening socket; no inbound connections; never
proxies the Docker socket; executes no arbitrary commands.

---

## 3. Communication

| Link | Protocol | Direction | Auth |
| --- | --- | --- | --- |
| Browser → control plane | HTTPS REST, JSON | Inbound | Session cookie |
| Browser → control plane | WebSocket | Inbound | Session cookie |
| Agent → control plane | HTTPS REST, JSON | **Outbound from host** | Bearer token |
| Control plane → PostgreSQL | PostgreSQL wire | Outbound | Password / TLS |
| Control plane → notifiers | HTTPS / SMTP | Outbound | Per-destination |

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

### 3.3 WebSockets (Phase 3+)

Used for live log tailing and dashboard updates. Read-only: all mutations go
through REST, so that authorization and audit logging live on one path instead
of two. Authenticated by the same session cookie as REST.

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

Role-based, checked server-side on every request:

| Role | Read monitoring | Manage containers | Edit rules | Manage agents/users |
| --- | --- | --- | --- | --- |
| Viewer | ✓ | | | |
| Operator | ✓ | ✓ | ✓ | |
| Admin | ✓ | ✓ | ✓ | ✓ |

Management actions additionally require re-authentication for destructive
operations and are written to an append-only audit log before dispatch.

---

## 5. Agent registration

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
containers, single-digit-million metric rows per day — comfortably. If volume
later demands it, TimescaleDB is a PostgreSQL extension, not a migration to a
different system. See ADR-003.

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

Not implemented. Design for Phase 2.

Raw samples are not kept forever: at a 30-second interval, one host produces
~2.9M samples a year per metric, and nobody inspects 30-second granularity
from eight months ago.

**Tiered downsampling, with aggregates computed by a scheduled job:**

| Tier | Resolution | Retention | Purpose |
| --- | --- | --- | --- |
| Raw | 30 s | 7 days | Live view, recent incident investigation |
| 5-minute | 5 min | 90 days | Trends, weekly patterns |
| 1-hour | 1 h | 2 years | Capacity planning, long-term growth |

Mechanism: time-partitioned tables, aggregates written by a scheduled
rollup job, expired partitions dropped rather than deleted. Retention is
operator-configurable per tier — their disk, their decision.

TimescaleDB's continuous aggregates would do this with less code and are a
plausible later optimisation. Phase 2 starts with plain PostgreSQL to keep the
deployment to one standard image; `alembic/env.py` already carries the
`include_object` hook needed to exclude Timescale-managed artefacts if that
changes. See ADR-008.

---

## 9. Notification rule engine

Not implemented. Design for Phase 5. This is the feature with the most
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

## 10. Docker management security model

This section is the most security-sensitive part of the design.

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

**The command set is closed.** `internal/controller.CommandType` enumerates
every action an agent will ever perform:

```go
CommandContainerStart   = "container.start"
CommandContainerStop    = "container.stop"
CommandContainerRestart = "container.restart"
```

The agent rejects any type it does not recognise. There is no `exec`, no
`run`, no shell, and no generic passthrough. Adding a member is a deliberate
decision requiring a matching authorization rule and audit entry.

**Commands are polled, not pushed.** The agent asks for pending work. This
preserves the no-inbound-connections property, which in turn means a
compromised control plane cannot connect to hosts — it can only wait to be
asked, and what it can answer is constrained by the closed command set.

### 10.3 Authorization and audit chain

Every management action passes through, in order:

1. **Authenticated** — a valid user session.
2. **Authorized** — the user's role permits this action on this host.
3. **Audited** — written to the append-only audit log *before* dispatch, so an
   action that crashes mid-flight is still recorded.
4. **Validated** — the target container exists on the target host.
5. **Scoped** — the command is queued for exactly one agent.
6. **Expiring** — commands carry `expires_at`; the agent refuses stale
   commands, so one queued during a network partition cannot execute hours
   later against changed state.
7. **Reported** — the agent returns a result, which completes the audit record.
   A failed command always carries a reason; a failure with no reason is not
   auditable.

### 10.4 Agent privileges on the host

The agent runs as a dedicated unprivileged user, **not root**. Docker access
in Phase 4 comes from `docker` group membership — which is root-equivalent and
must be documented as such to the operator, not glossed over. The reference
systemd unit in `agents/linux/README.md` applies `NoNewPrivileges`,
`ProtectSystem=strict` and a restricted syscall surface.

### 10.5 Explicitly excluded, permanently

Per the product vision, and not deferred:

- Arbitrary command or shell execution on hosts.
- Autonomous self-healing or automatic remediation.
- Automatic root-cause analysis or AI-generated error explanations.

The platform monitors, notifies, and performs **user-initiated** management
actions from a closed set.

---

## 11. Network monitoring

Not implemented. Design for Phase 6.

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
concurrently, plus WebSocket log streaming from Phase 3. Retrofitting async
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

**Decision.** Raw 7 days, 5-minute 90 days, hourly 2 years, operator-tunable.
Implemented with partitioned tables and a rollup job.

**Why.** Unbounded raw retention is unaffordable, and nobody needs
30-second resolution from last year. Dropping partitions is cheap; `DELETE`
bloats tables.

**Cost.** A scheduled job to maintain, and queries must select the right tier.

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

**Cost.** Some functionality must be written by hand. Phase 2 may need a
`/proc` parsing helper; the decision will be revisited then, explicitly,
rather than drifting.

### ADR-012: Mock data is isolated and loudly labelled

**Decision.** Phase 0 sample data lives only in `src/mocks/`, is static (never
randomly generated), is rendered only beneath a non-dismissible banner, and is
never served through a fake API client.

**Why.** Engineering rules 8 and 9. A foundation that looks like a working
product invites the belief that monitoring exists. Randomly generated values
would animate on each render and read as live telemetry; a fake API client
would be a placeholder that pretends to work. Deleted in Phase 2.

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
Phase 2 are a different decision and will be made separately — a time-ordered
key such as UUIDv7 is the likely choice there, once the minimum Python version
makes `uuid.uuid7` available.
