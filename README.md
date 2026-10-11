<div align="center">

# Homelab Control Center

**Self-hosted monitoring and management for Linux hosts, Docker containers and
network devices.**

[![CI](https://github.com/felixstaude/homelab-control-center/actions/workflows/ci.yml/badge.svg)](https://github.com/felixstaude/homelab-control-center/actions/workflows/ci.yml)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE)

</div>

---

> ### ⚠️ Phase 0 — Foundation release
>
> **This is not a working monitoring tool yet.** Phase 0 establishes the
> architecture, build system, design system and CI. It collects no metrics,
> ships no authentication, and does not talk to Docker.
>
> The Overview page shows **clearly labelled sample data** to demonstrate the
> intended design. Nothing in it is real.
>
> What is actually implemented, and what comes next, is in
> [`docs/ROADMAP.md`](docs/ROADMAP.md).

---

## What it will do

Monitoring for people who run a handful of machines at home, have outgrown
SSHing into each one to run `docker ps`, and do not want to operate Prometheus,
Grafana, Loki, Alertmanager and an exporter per host to answer *"is everything
fine, and if not, what broke?"*

| Area | Capability |
| --- | --- |
| **Hosts** | CPU, memory, disk usage and I/O, network, temperature, SMART, RAID, systemd — each a module you enable per host |
| **Docker** | Automatic container discovery; state and resource usage; logs; start/stop/restart |
| **Logs** | Collection from journald, files and containers; full-text search; live tailing |
| **Alerts** | Nested AND/OR rules with regex matching, cooldowns, and dry-run testing |
| **Network** | Manually registered devices; ICMP and TCP checks; user-defined topology |
| **Management** | Independent, opt-in capabilities for Docker, systemd and sessions — all off by default, all audited |

Everything is configurable from the web interface: which modules run, what they
collect, how often, how long history is kept, which management capabilities are
permitted, and what notifies you. Every setting has a safe default and
technically necessary limits are enforced.

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

Full detail, including all twenty-four architecture decision records:
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Stack

| Layer | Technology |
| --- | --- |
| Frontend | React 19, TypeScript, Vite 8, Tailwind CSS 4, TanStack Query, Recharts |
| Backend | Python 3.12+, FastAPI, Pydantic 2, SQLAlchemy 2 (async), Alembic |
| Database | PostgreSQL 17 |
| Agent | Go 1.24+, **zero third-party dependencies** |
| Dev | Docker **or** Podman, GNU Make, GitHub Actions |

## Quick start

Requires Python 3.12+, Node 20.19+/22.12+, Go 1.24+, GNU Make, and Docker or
Podman. On Fedora: `sudo dnf install python3 nodejs npm golang podman podman-compose make`

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

| URL | What |
| --- | --- |
| http://localhost:5173 | Web client |
| http://localhost:8000/docs | API documentation |
| http://localhost:8000/readyz | Readiness, including the database |

Run `make help` for every available target.

## Development

```bash
make check            # every quality gate — exactly what CI runs
make check-server     # ruff format + ruff check + mypy + pytest
make check-web        # tsc + eslint + vitest
make check-agent      # gofmt + go vet + go test -race
```

Conventions, troubleshooting and the full setup guide:
[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md).

## Documentation

| Document | Contents |
| --- | --- |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | The hub: system design, trust boundaries, 24 ADRs, open questions |
| [PRODUCT_VISION.md](docs/PRODUCT_VISION.md) | What this is, who it is for, what it will never do |
| [ROADMAP.md](docs/ROADMAP.md) | Phases 0–11 as small, independently testable milestones |
| [SECURITY.md](docs/SECURITY.md) | Threat model, capability risks, secrets handling, known gaps |
| [DEVELOPMENT.md](docs/DEVELOPMENT.md) | Setup, conventions, troubleshooting |
| [AGENT_ARCHITECTURE.md](docs/AGENT_ARCHITECTURE.md) | Transport, enrollment, credential rotation, config sync, offline behaviour |
| [MONITORING_MODULES.md](docs/MONITORING_MODULES.md) | Module catalogue, collection methods, hardware caveats |
| [SCHEDULING.md](docs/SCHEDULING.md) | Shared scheduling model, DST, missed runs, drift |
| [DATA_RETENTION.md](docs/DATA_RETENTION.md) | Retention, downsampling, storage limits, volume figures |
| [REMOTE_MANAGEMENT.md](docs/REMOTE_MANAGEMENT.md) | Capability model, authorization chain, remote terminal |
| [PLUGINS.md](docs/PLUGINS.md) | Future extension points; why no plugin system now |
| [PHASE_0_PLAN.md](docs/PHASE_0_PLAN.md) | What Phase 0 set out to do, and its accepted deviations |

## Repository layout

```
apps/
├── server/        FastAPI control plane (owns all state)
└── web/           React client
agents/
└── linux/         Go agent, one binary per monitored host
deploy/            Compose file for local PostgreSQL
docs/              Architecture, vision, roadmap, security
.github/           CI and Dependabot
Makefile           Task runner — start here
```

There is deliberately no `packages/` directory yet; shared TypeScript types
will be generated from the OpenAPI schema rather than hand-maintained
(ADR-007).

## Contributing

Early days — the architecture is still settling, so open an issue to discuss a
change before writing it.

Before opening a pull request:

1. `make check` passes.
2. New behaviour has tests.
3. Architectural decisions are documented as an ADR in
   [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#13-architecture-decision-records).
   A decision that contradicts an existing ADR must say so and supersede it
   explicitly, rather than quietly disagreeing.
4. No secrets are committed. CI enforces this.

## Security

Please do not report vulnerabilities in public issues. Use GitHub's private
vulnerability reporting. See [`docs/SECURITY.md`](docs/SECURITY.md), which also
lists the known gaps in this Phase 0 release.

## License

[GNU Affero General Public License v3.0](LICENSE).

AGPL-3.0 was chosen deliberately: if you run a modified version of this as a
network service, the licence requires you to publish those modifications. For
a self-hosted infrastructure tool, that keeps improvements available to the
people self-hosting it.
