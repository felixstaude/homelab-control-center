<div align="center">

# Homelab Control Center

**Self-hosted monitoring and management for Linux hosts, Docker containers and
network devices.**

[![CI](https://github.com/felixstaude/homelab-control-center/actions/workflows/ci.yml/badge.svg)](https://github.com/felixstaude/homelab-control-center/actions/workflows/ci.yml)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE)

</div>

---

> ### ⚠️ Pre-release. Useful for host metrics, and not much else yet.
>
> **What works end to end today:** a multi-user installation with roles and an
> audit trail; a host registry with one-time enrolment; a Go agent that
> enrols, connects and survives restarts; and **real CPU, memory, swap, load
> and uptime** collected from real `/proc`, stored with rollups and retention,
> and charted per host.
>
> **What does not exist yet:** Docker, logs, alerting, network devices, remote
> management, and the disk, network and temperature modules. The Overview's
> container and log cards are **labelled sample data** and say so on screen.
>
> There is **no packaged installation** — no published image, no Compose
> deployment, no prebuilt agent binaries. Running this today means the
> development setup below and building the agent yourself. See
> [current limitations](#current-limitations).
>
> Exactly what is built, partially built and not started, milestone by
> milestone: [`docs/ROADMAP.md`](docs/ROADMAP.md).

---

## What it is

Monitoring for people who run a handful of machines at home, have outgrown
SSHing into each one to run `docker ps`, and do not want to operate Prometheus,
Grafana, Loki, Alertmanager and an exporter per host to answer *"is everything
fine, and if not, what broke?"*

### Working today

| Area | What is actually implemented |
| --- | --- |
| **Identity** | Argon2id passwords, server-side sessions, CSRF, login throttling, Viewer / Operator / Administrator roles, user and session administration, append-only audit trail |
| **Hosts** | Administrator-registered host records, one-time enrolment credentials, derived connectivity (connected / stale / offline), revocation that disconnects within milliseconds |
| **Agent** | A single Go binary that dials out, heartbeats, reconnects with backoff, applies configuration it is sent, and buffers telemetry across a controller outage |
| **Metrics** | Five modules — `cpu`, `memory`, `swap`, `load`, `uptime` — enabled per host or globally, with enforced interval floors |
| **Storage** | Time-partitioned samples, raw → 5-minute → hourly rollups, retention by partition drop |
| **Interface** | Host list and host detail with live tiles and history charts, per-host and global monitoring configuration, settings for users, sessions and audit |

### Planned

Each is a roadmap phase, not a backlog wish. None of it is present in the
interface except as a labelled placeholder naming the phase that delivers it.

All phase numbers below refer to [`docs/ROADMAP.md`](docs/ROADMAP.md), which
states the exit criterion for every milestone.

| Area | Phase |
| --- | --- |
| Disk usage and I/O, network throughput, hardware inventory | 3 (the rest of it) |
| Temperature, SMART, RAID | 4 |
| Log collection, search and live tail | 5 |
| Docker container discovery and metrics | 6 |
| Remote management — Docker, systemd, sessions | 7 |
| Notification rules | 8 |
| Network device checks and topology | 9 |
| Packaging, backup and upgrade tooling | 10 |
| Interactive remote terminal | 11 |

### What it will never do

AI-generated error explanations · automatic root-cause analysis · autonomous
self-healing · automatic remediation · a general-purpose command-execution
API · root or `sudo`-capable execution · capabilities on by default ·
unrecorded interactive access.

These are product boundaries, not backlog items — see
[`docs/PRODUCT_VISION.md`](docs/PRODUCT_VISION.md#what-this-is-not). This is a
monitoring, notification and **user-controlled** management platform.

An interactive remote terminal *is* planned, as an opt-in, per-host,
Administrator-only, unprivileged, fully recorded capability that ships
disabled. That is deliberately not the same thing as the unrestricted shell
execution listed above — the distinction, and the honest assessment of the
added risk, is in
[`docs/REMOTE_MANAGEMENT.md`](docs/REMOTE_MANAGEMENT.md#5-remote-terminal).

## Design principles

1. **The operator decides what matters.** The platform surfaces facts and
   notifies according to rules you wrote. It does not decide for you.
2. **Never invent information.** Logs are preserved exactly as emitted. A host
   that has not reported shows **No data** — never 0% and healthy.
3. **Security is structural.** The agent has no listening socket. The Docker
   socket is never exposed on the network. Management commands come from a
   closed set, authorized and audited.
4. **Honest about what is not built.** Unbuilt features say so and name the
   milestone. Sample data is labelled as sample data.

## Architecture

```
Browser (React SPA)
      │ HTTPS — REST + WebSocket, same origin
      ▼
Control plane (FastAPI) ──── PostgreSQL
      ▲
      │ HTTPS — always agent-initiated, outbound from the host
      │
Agent (Go) on each monitored host
  reads /proc, /sys, journald and the LOCAL Docker socket
```

The agent always dials out; the control plane never connects to an agent. The
Docker socket is therefore never exposed on the network — which is the reason
the agent exists at all. LAN hosts, rented VPSes and machines behind CGNAT all
use the same protocol, with no inbound ports anywhere.

Full detail, including all fifty-two architecture decision records:
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Stack

| Layer | Technology |
| --- | --- |
| Frontend | React 19, TypeScript, Vite 8, Tailwind CSS 4, TanStack Query, Recharts |
| Backend | Python 3.12+, FastAPI, Pydantic 2, SQLAlchemy 2 (async), Alembic |
| Database | PostgreSQL 17 |
| Agent | Go 1.24+, one vendored dependency ([ADR-047](docs/ARCHITECTURE.md#adr-047-the-agent-takes-one-vendored-websocket-dependency-amending-adr-011)) |
| Dev | Docker **or** Podman, GNU Make, GitHub Actions |

## Supported platforms

| Component | Supported | Notes |
| --- | --- | --- |
| **Control plane** | Linux, Python 3.12–3.14 | Developed on Fedora; CI runs the 3.12 floor and a 3.14 job. Needs PostgreSQL 17. |
| **Agent** | Linux with `/proc` | `linux/amd64`, `linux/arm64` and `linux/arm` are cross-compiled in CI. Verified in use on Debian 13 and on CI's Ubuntu runners. |
| **Database** | PostgreSQL 17 | Tested against 17 only. Partitioning and the migration chain assume it. |
| **Browser** | Chromium-based | The end-to-end suite runs Chromium only; there is no engine-specific code, but Firefox and Safari are untested. |

The agent needs no capabilities and no root for the modules that exist today —
it reads `/proc` and writes two files in its own state directory. macOS,
Windows and BSD agents do not exist and are not planned for the current
phases.

**Measured limits** ([`docs/PERFORMANCE.md`](docs/PERFORMANCE.md)): 500 agent
connections per controller process at 224 KiB each. 500 is the supported fleet
size today — a thousand extrapolates comfortably and has **not** been
measured.

## Quick start

This is the **development** setup, and currently the only one. Requires Python
3.12+, Node 20.19+/22.12+, Go 1.24+, GNU Make, and Docker or Podman. On
Fedora: `sudo dnf install python3 nodejs npm golang podman podman-compose make`

```bash
git clone https://github.com/felixstaude/homelab-control-center.git
cd homelab-control-center

make setup       # Python venv + npm install
make env         # create .env files from the committed examples
make db-up       # start PostgreSQL (Docker or Podman, auto-detected)
make migrate     # apply migrations
make bootstrap   # create the initial administrator (prompts for a password)
```

`make bootstrap` runs once per installation. There is deliberately no HTTP
endpoint that creates the first administrator — an endpoint that works while
no user exists hands ownership of the deployment to whoever reaches the port
first. See
[ADR-033](docs/ARCHITECTURE.md#adr-033-the-initial-administrator-is-created-by-a-local-command-not-an-endpoint).

Then, in separate terminals:

```bash
make dev-server  # control plane → http://localhost:8000
make dev-web     # web client    → http://localhost:5173
```

Open http://localhost:5173 and sign in with the administrator you just
created. If you skipped `make bootstrap`, the browser shows a first-run screen
with the command to run — it explains the command rather than performing it,
for the reason above.

Every account after the first is created in the interface, under **Settings →
Users**: an administrator can create accounts, assign Viewer / Operator /
Administrator, change a display name or role, deactivate and reactivate, and
revoke another account's sessions. Each user manages their own password and
sessions under **My account** and **Sessions**, and privileged actions are
recorded in an append-only audit trail. The installation can never be left
without an active administrator — that operation is refused, because
bootstrap does not reopen. See
[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md#managing-accounts-milestone-17).

| URL | What |
| --- | --- |
| http://localhost:5173 | Web client |
| http://localhost:8000/docs | API documentation |
| http://localhost:8000/readyz | Readiness, including the database |

Run `make help` for every available target.

## Current limitations

Stated here rather than discovered after an hour of trying:

* **No packaged installation.** There is no published container image, no
  Compose deployment for the application itself, and no installation script.
  `deploy/compose.yaml` starts **PostgreSQL only**, for development. The
  design for a real one is
  [`docs/plans/simple-installation-and-agent-distribution.md`](docs/plans/simple-installation-and-agent-distribution.md);
  none of it is implemented.
* **No prebuilt agent binaries.** There are no GitHub Releases and no
  checksums. Running an agent means building it from this repository with
  `make agent-build` and copying it to the host yourself.
* **One controller process.** Login throttling counters live in process
  memory, so several uvicorn workers each get their own and multiply the
  effective limit. Deploy with **one worker** until that is addressed
  ([ADR-046](docs/ARCHITECTURE.md#adr-046-one-controller-process-stated-rather-than-enforced),
  [ADR-029](docs/ARCHITECTURE.md#adr-029-in-process-rate-limiting-plus-a-semaphore-on-argon2)).
* **No TLS of its own.** The control plane speaks plain HTTP and expects a
  TLS-terminating reverse proxy in front of it. Agents refuse plain HTTP to
  anything but loopback, so a proxy is not optional for a remote host. Read
  [`docs/SECURITY.md`](docs/SECURITY.md#transport-security) before exposing it.
* **No password reset.** A user can change their own password; there is no
  recovery for a forgotten one, deliberately — see
  [known gaps](docs/SECURITY.md#known-gaps).
* **No backup or restore tooling.** PostgreSQL's own `pg_dump` is the answer
  today.
* **No screenshots in this README.** The interface changes faster than
  screenshots can be kept honest, and a stale screenshot is a claim about
  behaviour. Run the development setup to see it.

## Connecting a host

```bash
# In the interface: Hosts → Register host, then "Issue enrolment credential".
# The credential is shown once, expires in fifteen minutes, and is single-use.

# On the machine you want to monitor:
make agent-build                                    # or cross-compile
sudo install -m 0755 agents/linux/bin/hcc-agent /usr/local/bin/

sudo install -m 0600 /dev/null /root/hcc-token
sudo editor /root/hcc-token                         # paste the credential

sudo HCC_AGENT_CONTROLLER_URL=https://your-controller \
     HCC_AGENT_ENROLLMENT_TOKEN_FILE=/root/hcc-token \
     hcc-agent enroll
```

The host appears as **Connected** within a second or two. Enrolling does not
start collecting anything: every module is off until an administrator turns it
on.

* Full installation procedure, including the hardened systemd unit and what
  has and has not been verified about it:
  [`agents/linux/deploy/README.md`](agents/linux/deploy/README.md).
* A complete worked example from bare host to a chart with real measurements:
  [`docs/development/ENABLING_MONITORING_DEBIAN.md`](docs/development/ENABLING_MONITORING_DEBIAN.md).

## Authentication and enrolment security

The two credential paths, summarised. The full model, including the threat
model and the known gaps, is [`docs/SECURITY.md`](docs/SECURITY.md).

**People** sign in with a password hashed using Argon2id at RFC 9106
parameters, against a server-side session whose cookie carries an opaque
random token — only its SHA-256 hash is stored, so the session table is not a
list of valid cookies. Cookies are `HttpOnly`, `SameSite=Lax` and `Secure` in
production (setting that false in production is refused at startup).
State-changing requests need a session-bound CSRF token *and* a matching
`Origin`. Login is throttled on three independent budgets before the password
is ever verified, proportioned so that no single source can lock a known
administrator out of their own installation.

**Agents** never receive a password and never listen on a port. An
administrator registers a host record first; registering does not mint a
secret. Issuing an enrolment credential is a separate, deliberate action — the
credential is displayed **once**, expires in fifteen minutes, is single-use,
and only its digest is stored, so no request can return it again. It
authorises an agent to claim *that one host record* and nothing else. Enrolling
exchanges it for a long-lived credential written to a file at mode `0600`; the
agent refuses to read that file if it is group- or world-readable, is a
symlink, is owned by another user, or sits in a directory other users can
write to.

Neither credential is ever accepted as a command-line argument, because
`/proc` makes every process's arguments readable by every local user on the
machine. There is no setting that disables TLS verification —
`HCC_AGENT_TLS_SKIP_VERIFY` was removed rather than carried forward. Revoking
an agent's credential disconnects it within milliseconds and it exits rather
than retrying.

Nothing about enrolment is automatic. A monitoring system that trusts anything
claiming to be a host is one anyone on the network can poison with fabricated
metrics, and fabricated metrics fire and suppress notification rules.

## Development

```bash
make check            # every local quality gate
make check-server     # ruff format + ruff check + mypy + pytest
make check-web        # tsc + eslint + vitest
make check-agent      # gofmt + go vet + go test -race
make test-integration # backend integration tests against real PostgreSQL
make e2e              # Playwright, real browser against a real backend
make monitoring-e2e   # a real agent reading real /proc, end to end
```

CI additionally runs the integration suite, the migration apply/reverse/check
cycle, the production build, Go cross-compilation and the browser suite — see
[`.github/workflows/ci.yml`](.github/workflows/ci.yml).

Conventions, troubleshooting and the full setup guide:
[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md).

## Documentation

| Document | Contents |
| --- | --- |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | The hub: system design, trust boundaries, 52 ADRs, open questions |
| [PRODUCT_VISION.md](docs/PRODUCT_VISION.md) | What this is, who it is for, what it will never do |
| [ROADMAP.md](docs/ROADMAP.md) | Phases 0–11 as small, independently testable milestones |
| [SECURITY.md](docs/SECURITY.md) | Threat model, capability risks, secrets handling, known gaps |
| [PERFORMANCE.md](docs/PERFORMANCE.md) | Measured figures, the measurement, and what has *not* been measured |
| [DEVELOPMENT.md](docs/DEVELOPMENT.md) | Setup, conventions, troubleshooting |
| [AGENT_ARCHITECTURE.md](docs/AGENT_ARCHITECTURE.md) | Transport, enrollment, credential rotation, config sync, offline behaviour |
| [MONITORING_MODULES.md](docs/MONITORING_MODULES.md) | Module catalogue, collection methods, hardware caveats |
| [SCHEDULING.md](docs/SCHEDULING.md) | Shared scheduling model, DST, missed runs, drift |
| [DATA_RETENTION.md](docs/DATA_RETENTION.md) | Retention, downsampling, storage limits, volume figures |
| [REMOTE_MANAGEMENT.md](docs/REMOTE_MANAGEMENT.md) | Capability model, authorization chain, remote terminal |
| [PLUGINS.md](docs/PLUGINS.md) | Future extension points; why no plugin system now |
| [RELEASING.md](docs/RELEASING.md) | How artifacts are built, what is verified, what a user can check |
| [PHASE_0_PLAN.md](docs/PHASE_0_PLAN.md) | What Phase 0 set out to do, and its accepted deviations |

## Repository layout

```
apps/
├── server/        FastAPI control plane (owns all state)
└── web/           React client
agents/
└── linux/         Go agent, one binary per monitored host
contracts/         Generated cross-language fixtures (Python → Go, TypeScript)
deploy/            Compose file for local PostgreSQL
docs/              Architecture, vision, roadmap, security, plans
scripts/           Quality gates that need more than one process
.github/           CI and Dependabot
Makefile           Task runner — start here
```

There is deliberately no `packages/` directory yet; shared TypeScript types
will be generated from the OpenAPI schema rather than hand-maintained
(ADR-007). Until that lands in milestone 1.15, the client's types are
hand-written but **validated against the backend's real schema**, so drift
fails the build rather than reaching runtime (ADR-037).

## Contributing

Early days — the architecture is still settling, so **open an issue to discuss
a change before writing it**. Conventions, the commit style, the ADR rule and
what a review looks for are in [`CONTRIBUTING.md`](CONTRIBUTING.md).

The short version:

1. `make check` passes.
2. New behaviour has tests.
3. Architectural decisions are documented as an ADR in
   [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#13-architecture-decision-records).
   A decision that contradicts an existing ADR must say so and supersede it
   explicitly, rather than quietly disagreeing.
4. No secrets are committed. CI enforces this.

## Security

Please do not report vulnerabilities in public issues. Use GitHub's private
vulnerability reporting — see [`SECURITY.md`](SECURITY.md) for the policy, and
[`docs/SECURITY.md`](docs/SECURITY.md) for the threat model and the known gaps
in this pre-release.

## License

Copyright © 2026 Felix Staude.

[GNU Affero General Public License v3.0](LICENSE), or (at your option) any
later version.

AGPL-3.0 was chosen deliberately: if you run a modified version of this as a
network service, the licence requires you to publish those modifications. For
a self-hosted infrastructure tool, that keeps improvements available to the
people self-hosting it.

In practice that means **§13**: if you modify this and let other people use it
over a network, you must offer those users the source of your version. If you
deploy it unmodified, or only for yourself, that obligation does not arise.
