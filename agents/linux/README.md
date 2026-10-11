# Linux Agent (`agents/linux`)

The agent runs on each monitored host, dials out to the control plane, and
reports what you configure it to report.

**Current scope:** lifecycle only. The agent loads and validates its
configuration, logs its identity, runs its reporting loop, and shuts down
cleanly. It collects no metrics, runs no monitoring modules, executes no
commands and does not touch Docker.

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
- **No general-purpose command execution.** Management commands are drawn from
  an enumerated set (`internal/controller.CommandType`). No command type
  accepts a caller-supplied command line.
- **Default deny.** Every management capability is off until explicitly
  enabled for this host, and the agent enforces that from its own applied
  configuration rather than trusting the controller to have checked.
- **Never root.** The agent runs unprivileged and is not in `sudoers`.
- **The enrolment token is not a flag.** Command lines are world-readable via
  `/proc`, so the token comes from the environment or, preferably, a file.

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
cmd/hcc-agent/       entrypoint: flags, signals, exit codes
internal/
├── agent/           runtime loop and graceful shutdown
├── config/          environment loading and validation
├── logging/         log/slog setup
├── buildinfo/       version metadata, injected at link time
└── controller/      control plane interface (contract only; no implementation)
```

No third-party dependencies, deliberately — the standard library covers
everything the agent needs. See ADR-011.

## Build and run

From the repository root:

```bash
make agent-build     # -> agents/linux/bin/hcc-agent
make agent-test
make agent-check     # gofmt + go vet + go test
```

Directly:

```bash
cd agents/linux
go build -o bin/hcc-agent ./cmd/hcc-agent
./bin/hcc-agent -version
```

## Configuration

All configuration comes from `HCC_AGENT_*` environment variables. Invalid
configuration makes the agent exit with code 2 and print every problem at
once, so a broken unit file can be fixed in one pass.

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `HCC_AGENT_CONTROLLER_URL` | yes | — | Control plane base URL. Must be `http` or `https`. |
| `HCC_AGENT_TOKEN_FILE` | yes¹ | — | Path to a file containing the enrolment token. **Preferred.** Must not be group- or world-readable. |
| `HCC_AGENT_TOKEN` | yes¹ | — | Enrolment token inline. Readable via `/proc/<pid>/environ`; prefer the file form. |
| `HCC_AGENT_HOSTNAME` | no | OS hostname | Identity reported to the controller. |
| `HCC_AGENT_REPORT_INTERVAL` | no | `30s` | Reporting cadence. Must be between `5s` and `15m`. |
| `HCC_AGENT_SHUTDOWN_TIMEOUT` | no | `10s` | Upper bound on graceful shutdown. |
| `HCC_AGENT_LOG_LEVEL` | no | `info` | `debug`, `info`, `warn` or `error`. |
| `HCC_AGENT_LOG_FORMAT` | no | `text` | `text` or `json`. |
| `HCC_AGENT_TLS_SKIP_VERIFY` | no | `false` | Disables certificate verification. Logs a warning on every start. Use only while installing an internal CA. |

¹ Exactly one of `HCC_AGENT_TOKEN_FILE` or `HCC_AGENT_TOKEN` is required. The
file wins if both are set.

### Flags

```
-version               print version information and exit
-log-level <level>     override HCC_AGENT_LOG_LEVEL
-log-format <format>   override HCC_AGENT_LOG_FORMAT
```

Flags override the environment, so verbosity can be raised for one invocation
without editing a unit file.

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

Not yet installed by the project — provided as a reference. Packaging is
Phase 10.

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
Environment=HCC_AGENT_TOKEN_FILE=/etc/hcc-agent/token
Environment=HCC_AGENT_LOG_FORMAT=json
ExecStart=/usr/local/bin/hcc-agent
Restart=on-failure
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
