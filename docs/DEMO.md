# The demo environment

An isolated Homelab Control Center you can sign in to, in one command, with
real telemetry from the machine you run it on.

```bash
make demo
```

It prints a URL, a username and a generated password. Open the URL.

## Contents

- [What it starts](#what-it-starts)
- [What it does not touch](#what-it-does-not-touch)
- [Commands](#commands)
- [What to look at](#what-to-look-at)
- [What is real and what is labelled sample data](#what-is-real-and-what-is-labelled-sample-data)
- [Troubleshooting](#troubleshooting)
- [Why not Compose](#why-not-compose)

## What it starts

Four real things, none of them simulated:

| | |
| --- | --- |
| **PostgreSQL** | its own container, `hcc-demo-postgres`, on `127.0.0.1:55433`, with its own named volume |
| **The controller** | the real FastAPI application from this working tree, on `127.0.0.1:8900`, serving the real built web client from the same origin |
| **An administrator** | `demo-admin`, with a password generated at first start and written to `.demo/credentials` (mode 0600) |
| **A real agent** | built from source, enrolled with a one-time credential, running as your user and reporting this machine's own `/proc` and `/sys` |

Seven monitoring modules are enabled on it: `cpu`, `memory`, `load`,
`uptime`, `filesystem`, `diskio` and `network`. Every number you see came
from this computer's kernel in the last few seconds.

`swap` is deliberately left off. A host with no swap configured reports the
module *unavailable*, which is correct behaviour and looks like a failure to
somebody seeing the product for the first time. Turn it on from
**Hosts → demo-localhost → Monitoring** if your machine has swap.

First start takes a couple of minutes — it pulls a PostgreSQL image, builds
the web bundle and compiles the agent. After that it is seconds.

## What it does not touch

The separation is the point rather than a precaution.

- **Not your development database.** The demo has its own container, its own
  port (55433, not 5432) and its own volume. The development database is
  never opened, migrated or read.
- **Not your development server.** Port 8900, not 8000, and not any port the
  test scenarios use.
- **Not this machine's installation.** Nothing is written to `/etc` or
  `/var/lib`, no systemd unit is created, nothing asks for `sudo`, and this
  machine is **not enrolled** into anything. The agent runs as your user from
  `.demo/agent/`. `hcc-agent install` is a different command and the demo is
  not it.
- **Not the network.** Both ports bind to `127.0.0.1`. There is no flag to
  change that: a demo that could be exposed by accident is one that will be.
- **No default password.** It is generated at first start. There is no
  password in any file in this repository and no way to pass one on a command
  line.

## Commands

```bash
make demo            # start it (safe to re-run; resumes what is already up)
make demo-status     # what is running, and whether telemetry is arriving
make demo-logs       # follow the controller and agent logs
make demo-down       # stop it, keeping the database and the administrator
make demo-clean      # stop it and delete everything
```

Or `./scripts/demo.sh up|status|logs|down [--clean]` directly.
`./scripts/demo.sh up --rebuild` forces a fresh web build and agent build,
which is what you want after changing either.

**`make demo-down` keeps your data.** The container is stopped rather than
removed and its volume survives, so the next `make demo` resumes with the
same administrator, the same host and the history already collected.
**`make demo-clean` discards all of it** — the volume, the credentials, the
agent's enrolment and the logs.

`make demo-status` is the first thing to run if something looks wrong:

```
  database               running on 127.0.0.1:55433
  controller             running on http://127.0.0.1:8900 (pid 880871)
  agent                  running (pid 881034)
  samples                986 across 65 series
  most recent            2026-10-15 05:29:34 UTC

  module           series  most recent
  cpu                   2  05:29:34
  diskio               20  05:29:34
  filesystem           16  05:29:09
  load                  3  05:29:29
  memory                4  05:29:34
  network              19  05:29:34
  uptime                1  05:28:40
```

## What to look at

| Where | What is there |
| --- | --- |
| **Hosts → demo-localhost** | live tiles and history charts for this machine |
| **Hosts → demo-localhost → Monitoring** | turn modules on and off, change intervals, see which are unavailable and why |
| **Settings → Monitoring** | fleet-wide defaults, which a host override takes precedence over |
| **Settings → Audit** | every change the demo just made, recorded |
| **Settings → Users / Sessions** | role-based access, session administration |

Things worth trying, because they are the behaviours that are hard to believe
without seeing:

- **Stop the agent** (`kill $(cat .demo/agent.pid)`) and watch the host move
  from connected to stale to offline. Start it again with
  `make demo` and watch the buffered telemetry arrive.
- **Disable a module** and watch collection stop within a second — the
  controller pushes the change, it does not wait for a poll.
- **Set an interval below a module's floor.** It is refused with a reason,
  not silently clamped.
- **Fill a filesystem** (or just watch `/boot`, which is usually the fullest
  thing on a Linux host) and see the usage series move.

## What is real and what is labelled sample data

Everything in a chart or a tile on the **host detail** page is real,
collected from this machine:

| Module | Source |
| --- | --- |
| `cpu`, `load`, `uptime` | `/proc/stat`, `/proc/loadavg`, `/proc/uptime` |
| `memory`, `swap` | `/proc/meminfo` |
| `filesystem` | `statfs` per mount, from `/proc/self/mountinfo` |
| `diskio` | `/proc/diskstats`, with whole disks selected via `/sys/block` |
| `network` | `/proc/net/dev`, with link state and speed from `/sys/class/net` |

The **Overview** page's container and log cards are **labelled sample data**
and say so on screen. Docker and log collection are not built yet, and the
placeholder names the roadmap phase that delivers each. Nothing anywhere
presents invented numbers as measurements.

Two things you will see that are correct and look odd at first:

- **Stacked storage reports the same bytes at every layer.** A write to a
  filesystem on LVM on NVMe appears in `dm-1` *and* `nvme0n1`. That is the
  kernel telling the truth about two devices. Nothing in the product sums
  them, deliberately — see [`MONITORING_MODULES.md`](MONITORING_MODULES.md).
- **btrfs subvolumes of one pool are reported once.** If `/` and `/home` are
  subvolumes sharing a pool, only `/` appears: they have the same free space,
  and listing both would be the same number twice.

## Troubleshooting

### `no container runtime found`

Install Podman or Docker. The demo uses one plain `run` invocation, which
both accept identically:

```bash
sudo dnf install podman      # Fedora, RHEL
sudo apt install podman      # Debian, Ubuntu
```

### `the backend is not installed` / `frontend dependencies are missing`

```bash
make setup
```

### `port 8900 is already in use by something that is not this demo`

Find it and decide:

```bash
ss -ltnp 'sport = :8900'
```

### The host shows as connected with no data

```bash
make demo-status      # which modules have series, and how recent
make demo-logs        # the agent says what it is collecting and why not
```

The two usual causes are a module reporting itself *unavailable* on this host
— visible on the host's Monitoring tab with the reason — and an agent that
enrolled but cannot reach the controller, which the agent log says plainly.

### `this demo database was created before <modules> existed`

An applied Alembic migration is never re-run, so a demo database created
before a module was added keeps the narrower `CHECK` constraint that refuses
it. Without the check that prints this, the mismatch surfaces much later as a
500 from the Monitoring tab. Start over:

```bash
make demo-clean && make demo
```

Only possible while developing on a branch that adds modules; a released
installation gets each module in its own migration.

### Containers

Off by default, and deliberately not something `make demo` turns on for you.
Reading the Docker socket is equivalent to root on the host, so it is granted
on the machine:

```bash
HCC_AGENT_DOCKER_SOCKET=/var/run/docker.sock ./scripts/demo.sh up --rebuild
```

Then enable **Containers** on the host's Monitoring tab. On a Podman host the
socket is `/run/user/$UID/podman/podman.sock` after
`systemctl --user start podman.socket` — Podman's Docker-compatible API is
what this was developed against.

The view is read-only: container state, CPU, memory, network, restarts,
uptime and health. There are no start/stop controls and the agent issues no
request other than `GET`.

### Starting over

```bash
make demo-clean && make demo
```

This is always safe and never touches anything outside `.demo/`, the
`hcc-demo-postgres` container and the `hcc-demo-pgdata` volume.

### Where everything is

```
.demo/credentials      the administrator's username and password (mode 600)
.demo/agent/           the agent binary, its credential and its configuration
.demo/controller.log   the controller's structured log
.demo/agent.log        the agent's structured log
.demo/*.pid            process ids, used by status and down
```

`.demo/` is gitignored. The password in it is real for as long as the demo is
up, and the agent credential authenticates a host.

## Why not Compose

[`deploy/production/compose.yaml`](../deploy/production/compose.yaml) is the
real deployment, and it is verified against **Docker Compose only**:
podman-compose 1.6.0 hangs on its `depends_on:
service_completed_successfully`, which is recorded in
[`DEPLOYMENT.md`](DEPLOYMENT.md) and in `scripts/deployment-e2e.sh`. Plenty of
people run Podman.

So the demo avoids Compose entirely. One `run` invocation for PostgreSQL — a
command both CLIs accept identically — and the controller and agent as host
processes from this working tree. That also makes it fast (no image build)
and makes a code change visible on a restart, which is what a demo is for.

To exercise the containerised deployment itself, use `make deployment-e2e`,
which builds the image, brings the production stack up, bootstraps, signs in,
restarts and tears down. It needs Docker.

## See also

- [`DEVELOPMENT.md`](DEVELOPMENT.md) — the development loop, which is a
  different thing: two servers, hot reload, and the database on 5432
- [`DEPLOYMENT.md`](DEPLOYMENT.md) — the real deployment
- [`MONITORING_MODULES.md`](MONITORING_MODULES.md) — what each module
  collects and what it deliberately does not
