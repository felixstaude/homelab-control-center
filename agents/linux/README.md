# Linux Agent (`agents/linux`)

The agent runs on each monitored host, dials out to the control plane, and
reports what you configure it to report.

**Current scope: connectivity and core metrics.** The agent enrols against a
real controller, holds an outbound authenticated WebSocket, heartbeats,
reconnects with exponential backoff, and is cut off within milliseconds when
its credential is revoked. Since milestone 1.9 it also receives a
configuration document over that socket, applies or refuses it, and collects
and reports five modules — `cpu`, `memory`, `swap`, `load` and `uptime` —
from `/proc`, buffering across a controller outage.

Every module is **off until the controller enables it**, so a freshly
enrolled agent collects nothing. It executes no commands and does not touch
Docker; the disk, network and sensor modules do not exist yet.

```bash
hcc-agent enroll     # spend a one-time credential for a durable one
hcc-agent run        # connect and stay connected (the service command)
hcc-agent status     # is this host enrolled? (never prints the credential)
hcc-agent version
```

Installation, the hardened systemd unit, and an explicit account of what has
and has not been verified: [`deploy/README.md`](deploy/README.md).

The designed architecture — transport, enrollment, credential rotation,
configuration synchronisation and offline behaviour — is in
[`docs/AGENT_ARCHITECTURE.md`](../../docs/AGENT_ARCHITECTURE.md). Delivery
order is in [`docs/ROADMAP.md`](../../docs/ROADMAP.md).

## Security properties

These are structural, not configuration options:

- **No listening socket.** The agent is always the client. It needs no inbound
  firewall rule and works behind NAT.
- **The Docker socket is never exposed on the network.** In Phase 6 the agent
  reads its own host's `/var/run/docker.sock` locally and sends results
  outward. The socket is not proxied, forwarded or bound to a port.
- **No general-purpose command execution.** The protocol has no field that
  carries a command, a script, a path or a module name — asserted against the
  encoded frames in `internal/protocol/protocol_test.go`, not merely intended.
  An agent that runs what the controller tells it has turned a control-plane
  compromise into root on every monitored machine.
- **Default deny.** Every management capability is off until explicitly
  enabled for this host, and the agent enforces that from its own applied
  configuration rather than trusting the controller to have checked.
- **Never root.** The agent runs unprivileged and is not in `sudoers`.
- **No secret is a flag.** Command lines are readable by every local user
  through `/proc`, so neither credential is settable that way and
  `TestNoSecretIsSettableViaAFlag` fails if one becomes so. The enrolment
  credential comes from a file (preferred) or the environment; the durable one
  comes from a file and only a file.
- **No TLS verification bypass exists.** Removed in 1.8 rather than carried
  forward: the credential and every heartbeat cross that connection, and the
  warning the old flag logged at every start was never a mitigation. Plain
  HTTP is permitted for loopback only, and a hostname that merely *resolves*
  to loopback is refused — DNS is not the agent's to trust.
- **The host's identity comes from its credential.** The agent reports no
  hostname and the protocol has no field for one, so a compromised agent
  cannot report as a machine it does not hold a credential for.

Two designed capabilities deliberately sit close to these lines, and are
documented rather than glossed over:

- Collectors with no kernel interface (SMART, some RAID controllers) invoke
  external binaries from a **compiled-in allow-list with fixed argument
  vectors and no shell** — there is no input path by which a caller chooses
  what runs. See
  [ADR-021](../../docs/ARCHITECTURE.md#adr-021-external-collector-binaries-on-a-fixed-allow-list).
- An **interactive terminal** is a planned opt-in capability: off by default,
  per host, Administrator-only, re-authenticated, unprivileged, time-bounded
  and fully recorded. See
  [`docs/REMOTE_MANAGEMENT.md`](../../docs/REMOTE_MANAGEMENT.md#5-remote-terminal)
  and
  [ADR-022](../../docs/ARCHITECTURE.md#adr-022-remote-terminal-is-an-opt-in-capability-superseding-the-blanket-shell-exclusion).

Full model: [`docs/SECURITY.md`](../../docs/SECURITY.md).

## Layout

```
cmd/hcc-agent/       entrypoint: subcommands, signals, exit codes
deploy/              systemd unit, environment example, install guide
vendor/              the one third-party dependency, in-tree (ADR-047)
internal/
├── agent/           the supervision loop: connect, heartbeat, reconnect
├── buildinfo/       version metadata, injected at link time
├── collect/         the five collectors, and the /proc parsing under them
├── config/          environment loading and validation (and no secrets)
├── controller/      the HTTP and WebSocket client, and the TLS policy
├── credentials/     the credential file, and what it refuses
├── logging/         log/slog setup
├── monitoring/      the applied configuration: validate, store, apply
├── protocol/        the wire format, mirroring the controller's
├── scheduler/       per-module timing, anchored so drift does not accumulate
└── telemetry/       the bounded outbound queue, oldest dropped first
```

**One third-party dependency, vendored.** `github.com/coder/websocket`, which
has no transitive dependencies of its own, lives in `vendor/`. ADR-011
originally required none at all;
[ADR-047](../../docs/ARCHITECTURE.md#adr-047-the-agent-takes-one-vendored-websocket-dependency-amending-adr-011)
amends it and gives the reasoning. Everything else is the standard library,
and `go mod vendor` must leave no diff — CI checks.

## Build and run

From the repository root:

```bash
make agent-build     # -> agents/linux/bin/hcc-agent, with version stamping
make test-agent      # go test -race
make check-agent     # gofmt + go vet + go test -race
```

Directly:

```bash
cd agents/linux
go build -o bin/hcc-agent ./cmd/hcc-agent
./bin/hcc-agent version
```

A binary built that way reports `hcc-agent dev (commit unknown, built
unknown, …)`, because the version metadata is injected at link time. That is
fine for development and not what you want in `/usr/local/bin` on a real
host — `make agent-build` stamps it.

### Cross-compiling

No cgo, so this needs nothing installed beyond Go:

```bash
GOOS=linux GOARCH=arm64 go build -o bin/hcc-agent-arm64 ./cmd/hcc-agent
```

`linux/amd64`, `linux/arm64` and `linux/arm` are built on every CI run. There
are **no published binaries yet**: building from source and copying the
result to the host is currently the only way to install an agent. The design
for prebuilt, checksummed releases is
[`docs/plans/simple-installation-and-agent-distribution.md`](../../docs/plans/simple-installation-and-agent-distribution.md).

## Configuration

All configuration comes from `HCC_AGENT_*` environment variables. Invalid
configuration makes the agent exit with code 2 and print every problem at
once, so a broken unit file can be fixed in one pass.

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `HCC_AGENT_CONTROLLER_URL` | yes | — | Control plane base URL. Must be `https`, except for a loopback host. |
| `HCC_AGENT_CREDENTIAL_FILE` | no | `/etc/hcc-agent/credential` | The durable credential written by `enroll`. Refused if group- or world-readable, a symlink, owned by another user, or in a directory others can write to. |
| `HCC_AGENT_CONFIG_FILE` | no | `/etc/hcc-agent/config.json` | Where the configuration the controller sends is persisted. Controller-owned: edits are overwritten on the next connection. |
| `HCC_AGENT_CA_FILE` | no | — | An extra CA bundle to trust in addition to the system roots, for an internal CA. |
| `HCC_AGENT_LOG_LEVEL` | no | `info` | `debug`, `info`, `warn` or `error`. |
| `HCC_AGENT_LOG_FORMAT` | no | `text` | `text` or `json`. |
| `HCC_AGENT_SHUTDOWN_TIMEOUT` | no | `10s` | Upper bound on graceful shutdown. |
| `HCC_AGENT_RECONNECT_MIN_WAIT` | no | `2s` | Backoff floor. Cannot be zero: a fleet reconnecting instantly is a denial of service against its own controller. |
| `HCC_AGENT_RECONNECT_MAX_WAIT` | no | `60s` | Backoff ceiling, so a host does not stay invisible long after an outage ends. |

Read by `hcc-agent enroll` and by nothing else:

| Variable | Description |
| --- | --- |
| `HCC_AGENT_ENROLLMENT_TOKEN_FILE` | File holding the one-time enrolment credential. **Preferred.** |
| `HCC_AGENT_ENROLLMENT_TOKEN` | The credential itself. Readable through `/proc/<pid>/environ` and inherited by child processes — use the file form. |

Exactly one of the two is required to enrol. Neither belongs in a unit file
or an `EnvironmentFile`: they are live secrets for fifteen minutes, and that
file is readable by every local user.

**There is no `HCC_AGENT_TLS_SKIP_VERIFY`, and no other way to skip
certificate verification.** It was removed in milestone 1.8 rather than
carried forward. If the agent cannot verify your controller, the fix is a
certificate it can verify — a public CA, or your own trusted through
`HCC_AGENT_CA_FILE`.

There is also no `HCC_AGENT_HOSTNAME`. The host's identity comes from its
credential, and the protocol has no field in which an agent could claim to be
a different machine. Collection intervals come from the controller, not from
the environment, so there is no reporting-interval variable either.

### Flags

No secret is accepted as a flag, and `TestNoSecretIsSettableViaAFlag` fails
if one becomes so. The only flag is:

```
-version, --version    print version information and exit
```

equivalent to the `version` subcommand. Logging is configured through the
environment, which is what a unit file sets.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Clean shutdown after `SIGINT` or `SIGTERM`. |
| 1 | Runtime failure. |
| 2 | Invalid configuration or flags. |

## Signals

`SIGINT` and `SIGTERM` both begin graceful shutdown; the agent drains, logs
`shutdown complete`, and exits 0. This is covered end to end by
`cmd/hcc-agent/signal_test.go`, which re-executes the test binary and sends it
a real `SIGTERM`.

## Example: systemd unit

Not installed by the project — packaging is Phase 10. This is the short
reference; the **authoritative** unit, with the full hardening block and an
explicit account of which directives have and have not been exercised on a
live system, is in [`deploy/README.md`](deploy/README.md). Use that one.

Note that no enrolment token appears here. Enrolment happens once, by hand,
before the service is started; the unit only needs to know where the
controller is, because the durable credential is read from its file at the
point of use.

```ini
[Unit]
Description=Homelab Control Center agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# Dedicated unprivileged user. Group membership for Docker access is added
# in Phase 6, when the agent first needs the socket.
User=hcc-agent
Group=hcc-agent
Environment=HCC_AGENT_CONTROLLER_URL=https://hcc.internal:8000
Environment=HCC_AGENT_LOG_FORMAT=json
ExecStart=/usr/local/bin/hcc-agent run
Restart=always
RestartSec=5s

# Hardening: the agent reads system state and makes outbound connections.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictNamespaces=true
LockPersonality=true
MemoryDenyWriteExecute=true

[Install]
WantedBy=multi-user.target
```
