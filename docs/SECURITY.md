# Security

The security model for Homelab Control Center.

> **Current status (Phase 1, milestone 1.1).** No authentication,
> authorization, agent communication, monitoring or management is implemented
> yet. The `users` table exists as schema only — see "Known gaps" below.
> Sections marked **(design)** describe what later milestones will build; they
> are documented now because they constrain the architecture that *is* built.
> Sections marked **(implemented)** are in the code today and covered by
> tests.
>
> **This revision expands the designed threat surface.** Remote management is
> now a set of opt-in capabilities including an interactive terminal
> ([the capability section](#remote-management-capabilities)), and some collectors invoke
> external binaries ([the external-binaries section](#external-collector-binaries)). Both are new
> risks relative to read-only monitoring, and both are assessed rather than
> asserted away.

## Threat model

### What is being protected

| Asset | Why it matters |
| --- | --- |
| Docker socket access | Equivalent to **root** on the host |
| Agent credentials | Allow reporting as a host, and receiving its commands |
| User sessions | Allow management actions, and terminal access for an Administrator |
| Log content | Routinely contains tokens, IPs, paths, usernames |
| Notification destination credentials | Ntfy tokens, SMTP passwords, webhook secrets |
| Terminal session recordings | Contain whatever the operator typed, including secrets |
| Host configuration | Reveals which capabilities are enabled, i.e. where the soft targets are |

### Assumed adversaries

1. **An attacker on the local network** who can reach the control plane's
   HTTP port.
2. **A compromised monitored host** — one agent, fully controlled. It must not
   escalate to the fleet.
3. **A malicious or careless authenticated user** with a low-privilege role.
   Now also: an Administrator whose session is stolen, which is the adversary
   the terminal capability's gates exist for.
4. **A compromised dependency** in any of the three components' supply chains.
5. **An attacker with a stolen database dump**, offline.

### Explicitly out of scope

- An attacker with root on the host running the control plane. They own the
  database and the process; no application-level control helps.
- Physical access to hardware.
- Nation-state-grade targeting of a homelab.

---

## Structural properties

These are the load-bearing security decisions. They are enforced by
architecture rather than by configuration, so an operator cannot accidentally
turn them off.

### 1. The agent has no listening socket (implemented)

The agent is always the client. It opens outbound HTTPS connections to the
control plane; the control plane never connects to an agent.

Consequences:

- No inbound firewall rule on monitored hosts.
- No listening port to attack, fingerprint or exploit.
- Works behind NAT, which is the normal homelab case.
- **A compromised control plane cannot reach into hosts.** It can only wait to
  be polled, and what it can answer is bounded by the closed command set
  below.

### 2. The Docker socket is never exposed on the network (design)

Not bound to a port. Not proxied. Not forwarded. Not tunnelled.

The agent opens `/var/run/docker.sock` as a **local Unix socket on its own
host**, and sends results outward. This is the primary reason the agent exists:
without it, the control plane would need remote Docker access, which means
either exposing the socket (catastrophic) or holding root-equivalent
credentials for every host (nearly as bad).

### 3. Management commands come from an enumerated set (implemented as a contract)

`agents/linux/internal/controller.CommandType` enumerates every action an
agent will ever perform. The agent **rejects any command type it does not
recognise**. There is no `exec`, no `run`, no generic passthrough, and no
command type that accepts a caller-supplied command line.

The set has grown beyond the original three Docker commands to cover systemd
and session control; current membership is in
[`REMOTE_MANAGEMENT.md §3`](REMOTE_MANAGEMENT.md#3-command-set). Growing an
enumerated set is not opening it — each member maps to one capability, carries
only an agent-discovered target, and arrives with its authorization rule,
audit entry and tests.

A general-purpose command-execution API remains permanently excluded. An
interactive terminal is now a separate, opt-in capability — see
[the capability section](#remote-management-capabilities) and
[ADR-022](ARCHITECTURE.md#adr-022-remote-terminal-is-an-opt-in-capability-superseding-the-blanket-shell-exclusion).

### 4. The agent initiates every connection (design)

Previously stated as "commands are polled, not pushed". Command delivery now
uses a WebSocket the **agent dials out** and holds open, because polling
cannot carry an interactive terminal.

The security property is unchanged and is the one that matters: the agent
opens every connection and listens on nothing, so property 1 holds exactly.
See [ADR-018](ARCHITECTURE.md#adr-018-agent-initiated-websocket-replaces-command-polling).

### 5. Management capabilities are default-deny (design)

Every capability on every host starts **off**. A freshly enrolled agent
refuses every command, including from a legitimate Administrator, until a
capability is explicitly enabled for that host.

There is no "enable all" switch and no global capability default that applies
to future hosts, so **adding a host can never silently grant control over
it**.

The agent enforces capabilities from its own applied configuration rather than
trusting the controller to have checked. A compromised controller therefore
cannot exceed the capabilities each host was already granted — which is what
keeps the blast radius of a controller compromise bounded.

### 6. Agent-submitted data is untrusted input (design)

Even from an authenticated agent. A compromised host must not escalate through
a crafted payload.

Concretely:

- **Records are attributed server-side**, from the authenticated credential —
  never from a `hostname` field in the payload. Otherwise one compromised agent
  could inject logs attributed to any other host, and thereby fire or suppress
  another host's notification rules.
- Batch sizes, message lengths and request rates are bounded before parsing.
- Rejected batches return a structured error naming the limit, so the agent
  backs off instead of retrying blindly.

---

## Authentication (design)

Two distinct identity types, deliberately not conflated:

| | Users | Agents |
| --- | --- | --- |
| Credential | Password → session | Long-lived enrolment credential |
| Hashing | Argon2id | Argon2id (stored hashed) |
| Transport | `Secure`, `HttpOnly`, `SameSite=Lax` cookie | `Authorization: Bearer` |
| Scope | Fleet-wide, role-limited | **One host only** |
| May issue commands | Yes, if authorized | No |
| May read others' data | Per role | No |

An agent credential authenticates *one host reporting its own data*. It is not
a user credential with a different shape, and cannot be used to read the fleet
or issue commands. This is what contains adversary #2.

### Agent enrolment

1. An admin creates a host and receives a **one-time** enrolment token.
2. The token goes to `/etc/hcc-agent/token`, mode `0600`.
3. The agent exchanges it once for a long-lived credential; the token is marked
   consumed.

Design points:

- **The one-time token is separate from the steady-state credential.** The
  short-lived one is what gets pasted into terminals and lands in shell
  history.
- **Both are stored hashed.** A stolen database dump (adversary #5) yields no
  working credentials.
- **Revocation is immediate and server-side.** No waiting for expiry.
- **The token is never a command-line flag** — command lines are world-readable
  via `/proc`. It comes from a file (preferred) or the environment. Enforced by
  `TestNoFlagAcceptsTheEnrolmentToken`.
- **The token file must not be group- or world-readable.** The agent refuses to
  start otherwise (`config.readTokenFile`), rather than accepting a secret that
  is not a secret. Tested across modes `0644`, `0640`, `0604`, `0666`.

---

## Authorization (design)

Role-based, enforced **server-side on every request**. The web client is an
untrusted client; anything it decides can be forged.

Two axes, both of which must permit an action: the user's **role**, and the
target host's enabled **capability** ([the capability section](#remote-management-capabilities)).
A role grants nothing on a host whose capability is off.

| Role | Read monitoring | Manage containers | Manage units | Edit rules | Terminal | Manage agents/users |
| --- | :-: | :-: | :-: | :-: | :-: | :-: |
| Viewer | ✓ | | | | | |
| Operator | ✓ | ✓ | ✓ | ✓ | | |
| Administrator | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

Every management action passes through, in order:

1. **Authenticated** — valid session.
2. **Authorized** — role permits this action on this host.
3. **Audited** — written to the append-only audit log **before** dispatch, so
   an action that crashes mid-flight is still recorded.
4. **Validated** — target exists on the target host.
5. **Scoped** — queued for exactly one agent.
6. **Expiring** — carries `expires_at`; the agent refuses stale commands, so
   one queued during a network partition cannot execute hours later against
   changed state.
7. **Reported** — the result completes the audit record. A failed command
   always carries a reason; a failure with no reason is not auditable.

---

## Secrets handling

### Never committed (implemented)

- `.gitignore` excludes `.env`, `.env.*` (except `.env.example`), `*.pem`,
  `*.key`, `secrets/`.
- Committed `.env.example` files contain only local-development placeholders
  and an explicit warning.
- `alembic.ini` contains **no** database URL; it comes from the environment.
- CI fails the build if a `.env` file or a private key is committed, or if an
  `.env.example` lacks its warning (`hygiene` job).

The credentials in `deploy/compose.yaml` and `apps/server/.env.example`
(`hcc:hcc_local_dev`) are for a throwaway local container bound to loopback.
They are committed so a fresh checkout works with no setup, and are documented
as never to be reused.

### Never logged (implemented)

- `config.Config.String()` **and** `LogValue()` both redact the agent token.
  Both paths are tested — overriding only `String()` would leave the token
  exposed when logged as a structured attribute.
- `/readyz` reports a dependency failure by **exception type**, not message:
  driver messages can contain the connection string, including the password.
  Tested by `test_readiness_does_not_leak_credentials_in_failure_detail`.
- The catch-all exception handler logs the traceback server-side and returns an
  **opaque** 500. Internal details never reach the client. Tested by
  `test_unexpected_exception_does_not_leak_internals`.

### Browser-exposed configuration (implemented)

Only `VITE_`-prefixed variables reach the client, and they are embedded
verbatim in the bundle. `apps/web/.env.example` states this and warns against
putting any secret there.

---

## Transport security

### Control plane

TLS is terminated by a reverse proxy the operator already runs (Caddy,
Traefik, nginx). The application speaks plain HTTP behind it.

**Why not terminate TLS in the application:** homelab users already have a TLS
story — an internal CA, Let's Encrypt via DNS-01, or Tailscale. Imposing a
certificate mechanism would fight their existing setup and produce a worse
result than the proxy they already operate.

Deploying without a reverse proxy means session cookies travel in cleartext.
Do not do it.

### Agent

HTTPS with full certificate verification by default.

`HCC_AGENT_TLS_SKIP_VERIFY=true` disables verification. It exists for the real
case of a homelab running an internal CA not yet installed on a host. When
enabled, the agent logs a `WARN` on **every start** stating that traffic can be
intercepted and naming the variable to unset — not a one-time notice at
configuration time. Tested by `TestRunWarnsWhenTLSVerificationIsDisabled`.

---

## Application hardening (implemented)

| Control | Where | Rationale |
| --- | --- | --- |
| API docs disabled in production | `hcc/app.py` | Widens attack surface for no operator benefit |
| CORS default-deny | `hcc/config.py` | Production is same-origin; empty origin list adds no middleware |
| Strict settings validation | `hcc/config.py` | `extra="forbid"` turns a typo'd variable into a startup failure, not a silently ignored setting |
| Immutable settings | `frozen=True` | No runtime mutation of security-relevant configuration |
| Fail-fast startup | `load_settings_or_exit` | Invalid configuration exits with code 2 and a readable diagnostic |
| Uniform error envelope | `hcc/errors.py` | No endpoint leaks a differently shaped error with more detail |
| Request correlation | `hcc/middleware.py` | Every log line carries a request ID; opaque 500s remain traceable |
| `pool_pre_ping` | `hcc/db.py` | Recycles connections a restarted database dropped |
| Bandit lint rules (`S`) | `pyproject.toml` | Flags common insecure patterns in CI |

### Agent process hardening

The agent runs as a dedicated unprivileged user, **not root**. The reference
systemd unit in [`agents/linux/README.md`](../agents/linux/README.md) applies
`NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`,
`RestrictAddressFamilies`, `RestrictNamespaces` and `MemoryDenyWriteExecute`.

**Docker group membership is root-equivalent.** In Phase 6 the agent needs it
to reach the socket. This is documented to the operator plainly rather than
glossed over: adding the agent user to `docker` grants root-equivalent power on
that host.

Each capability requires its own narrow grant, listed per capability in
[`REMOTE_MANAGEMENT.md §6`](REMOTE_MANAGEMENT.md#6-host-privileges). Two rules:

- Grants are **per capability, never blanket**. `systemd.control` uses a polkit
  rule for specific units, not `manage-units`. Raw device access for SMART
  prefers a device ACL over adding the agent user to `disk`.
- Some hardening directives in the reference unit must be relaxed for specific
  capabilities. Each relaxation is documented with what it costs, rather than
  the unit quietly shipping with fewer protections.

Network checks (Phase 9) use `CAP_NET_RAW` for ICMP, not full root.

---

## Remote management capabilities

> Full model: [`REMOTE_MANAGEMENT.md`](REMOTE_MANAGEMENT.md). This is the
> security summary.

Capabilities are independent, per host, and all default to off. Read and
control are always separate; enabling one never implies another.

| Capability | Risk if enabled | Mitigation |
| --- | --- | --- |
| `docker.read` | Container names and log contents exposed | Role-gated; logs follow the log pipeline's rules |
| `docker.control` | Service outage; **root-equivalent via the Docker socket** | Role-gated, audited, per-host; the root-equivalence is stated at the point of enabling |
| `systemd.read` | Unit names reveal what runs on the host | Role-gated |
| `systemd.control` | Can stop `sshd` or the agent itself | Per-unit allow-list, empty by default; compiled-in protective list for `sshd`, `systemd-*`, `dbus`, `network*`; re-authentication |
| `sessions.read` | Session inventory exposed | Role-gated |
| `sessions.control` | Attaching inherits the session's privileges, **including root** | Administrator-adjacent; see open question 5 in [ARCHITECTURE §14](ARCHITECTURE.md#14-open-design-questions) — the control half may not ship |
| `terminal` | Interactive shell access to the host | Four independent gates; see below |

### The terminal capability, assessed honestly

This is the most dangerous thing the product will do. Gates, all required:

1. `terminal` enabled on that specific host.
2. Administrator role — it cannot be granted to an Operator.
3. Re-authentication immediately before the session opens.
4. An active session cookie over TLS.

Boundaries: runs as the agent's **unprivileged** user; no `sudo` or `su` (the
agent user is not in `sudoers`); fixed shell path, not caller-supplied; a
minimal constructed environment that does **not** inherit the agent's own
environment, so the agent credential is not exposed to the session; idle and
absolute timeouts; bounded concurrency; revocable mid-session; multiplexed
over the agent's existing outbound connection with **no new port and no SSH
daemon**.

**Recording has no off switch.** Every session records who, which host, when,
and the full input/output stream. An unrecorded interactive session on a
production host is the accountability gap this platform exists to close.
Recordings are themselves sensitive — they contain whatever was typed — so
they are Administrator-only, have their own retention with a non-zero minimum,
and their deletion is audited.

**Residual risk.** An attacker with a stolen Administrator session who can
re-authenticate gains interactive unprivileged shell access to hosts where the
capability is on. Nothing in the design removes that; the design bounds it.
And where `docker.control` is also enabled, the session can reach the Docker
socket and is therefore effectively root — the UI says so at the point of
enabling, and this combination is the one to think hardest about.

The trade-off being made is not "terminal versus nothing". It is "audited,
unprivileged, opt-in terminal" versus "operators keep separate SSH access this
platform cannot see and which is often root-capable".

## External collector binaries

Some modules — SMART, several RAID controllers, screen/tmux — have no kernel
interface and must invoke `smartctl`, `nvme`, `mdadm`, a vendor CLI, `screen`
or `tmux`.

This sits close to the permanently-excluded "arbitrary command execution", so
the distinction is load-bearing rather than rhetorical. **There is no input
path by which a caller chooses what runs.** Specifically:

| Control | Why it matters |
| --- | --- |
| Compiled-in allow-list of executables | An operator cannot point a module at a different binary |
| Fixed argument vectors | Only an agent-discovered device path is substituted; no user string reaches an argument |
| No shell, ever — `exec` with an argv slice | Removes the quoting and injection surface entirely |
| Read-only subcommands only | The one exception, `smart_selftest`, is a separate module with its own capability |
| Absolute path resolution, never `PATH` | A writable directory earlier in `PATH` cannot hijack the call |
| Timeout, output cap, one invocation per device | Bounds a hung or chatty tool |
| Never root | Raw device access comes from a narrow grant or device ACL, not `sudo` |
| Module-level audit of the resolved `argv` | What ran is recoverable after the fact |

Arbitrary execution means the caller picks the command. This does not. See
[ADR-021](ARCHITECTURE.md#adr-021-external-collector-binaries-on-a-fixed-allow-list)
and
[`MONITORING_MODULES.md §4.3`](MONITORING_MODULES.md#43-external-binaries--the-one-genuinely-new-risk).

**Residual risk, stated.** Subprocess execution is a larger attack surface
than reading `/proc`. A vulnerability in `smartctl` itself becomes reachable
on hosts where SMART monitoring is enabled — which it would also be for anyone
running `smartctl` from cron, but the platform is now the thing invoking it.
A module that cannot meet every control above does not ship.

## Configuration as an attack surface

Configuration is operator-authored and controller-delivered, which makes it
worth treating as a surface in its own right.

- **Configuration cannot weaken an invariant.** There is no setting that opens
  an inbound port, exposes the Docker socket, disables audit logging, or turns
  the agent into a general command runner. See
  [`PRODUCT_VISION.md §4`](PRODUCT_VISION.md#4-invariants-are-structural-capabilities-are-configurable).
- **Agents validate before applying**, and an invalid document never replaces
  the last known good one — so a bad or tampered configuration cannot silently
  stop monitoring.
- **ACK carries a checksum**, not just a version, so a truncated or modified
  document cannot be acknowledged as applied.
- **Configuration is per host and scoped by credential.** One agent cannot
  fetch another host's configuration, which would otherwise leak the fleet's
  capability map to a single compromised machine.
- **Resource limits are enforced server-side** (interval floors, retention
  ceilings, storage budget), so a configuration cannot be used to exhaust the
  controller's disk. See
  [`DATA_RETENTION.md §6`](DATA_RETENTION.md#6-storage-limits).

## Supply chain

| Component | Dependencies | Mitigation |
| --- | --- | --- |
| Agent | **Zero** third-party (ADR-011) | Entire supply chain is the Go standard library — the strongest guarantee in the system, and it is the component running on monitored hosts |
| Backend | 8 direct | Pinned ranges; grouped Dependabot PRs; `pip-audit` planned |
| Frontend | ~16 direct | Exact pins + lockfile; `npm ci` in CI; `npm audit` reports 0 vulnerabilities |
| CI actions | Pinned by major | Dependabot-updated |

The agent's zero-dependency rule is a security decision, not minimalism for its
own sake: it is the component with elevated privileges on hosts the operator
cares about, so a compromised transitive dependency must not be able to reach
them.

---

## Known gaps

Stated plainly rather than implied:

1. **There is no authentication.** The API is still unauthenticated. It exposes
   only health probes and version metadata, but it must not be deployed on an
   untrusted network. Milestone 1.1 added the `users` table — schema only. No
   login, session, registration or password-reset path exists yet, and nothing
   hashes or verifies a password; `password_hash` is simply a NOT NULL column
   with a non-empty CHECK constraint awaiting milestone 1.2.
2. **There is no rate limiting.** Added with the ingestion endpoints in
   Phase 3, where it matters.
3. **There is no audit log.** Phase 1.
4. **There is no CSRF protection.** Not yet needed (no cookies, no mutating
   endpoints); required in Phase 1 alongside session cookies.
5. **No security headers** (CSP, HSTS, `X-Frame-Options`). Added in Phase 10
   with the production image, where the app serves the client itself.
6. **`pip-audit` and `npm audit` are not yet CI-blocking.** Phase 1.
7. **Nothing in the capability, external-binary and configuration sections exists yet.** No capability is implemented,
   no command is executed, no binary is invoked, no configuration is
   distributed. They are specified now because they constrain milestones that
   are about to be built — not because any of it is live.
8. **The terminal capability has no implementation and no security review.**
   Before it ships it needs a dedicated review of PTY handling, session
   recording integrity, and the interaction with `docker.control` noted in
   the terminal assessment above. It is deliberately last in the roadmap for this reason.

## Reporting a vulnerability

Do **not** open a public issue.

Use GitHub's private vulnerability reporting ("Report a vulnerability" under
the Security tab), or email the maintainer. Please include affected version,
reproduction steps, and impact.

This is a volunteer-maintained project; expect an acknowledgement within a week.
Fixes are released as soon as practical, and credit is given unless you prefer
otherwise.
