# Remote Management

The capability model for acting on a host, rather than merely observing it.

> **Status: design.** Nothing here is implemented. The agent today executes no
> commands; `internal/controller.CommandType` exists as a contract with three
> members and no implementation. Delivery is tracked in
> [`ROADMAP.md`](ROADMAP.md).

- [1. Capabilities](#1-capabilities)
- [2. Default deny](#2-default-deny)
- [3. Command set](#3-command-set)
- [4. Authorization chain](#4-authorization-chain)
- [5. Remote terminal](#5-remote-terminal)
- [6. Host privileges](#6-host-privileges)
- [7. Audit](#7-audit)
- [8. Still excluded](#8-still-excluded)

---

## 1. Capabilities

A capability is a single switch, set **per host**, that permits one class of
action. Read and control are always separate.

| Capability | Permits | Risk |
| --- | --- | --- |
| `docker.read` | List containers, inspect state, read container logs | Low. Container names and logs can be sensitive. |
| `docker.control` | Start, stop, restart a container | Medium. Can cause a service outage. |
| `systemd.read` | List units, read active/sub state and recent failures | Low. Unit names reveal what runs on the host. |
| `systemd.control` | Start, stop, restart a unit | **High.** Can stop `sshd` or the agent itself. |
| `sessions.read` | List screen and tmux sessions | Low. |
| `sessions.control` | Attach to an existing screen or tmux session | **High.** An attached session inherits whatever shell it already runs. |
| `terminal` | Open a new interactive shell session | **Highest.** See §5. |

### 1.1 Read does not imply control, and control does not imply read

Each capability is independent. Enabling `docker.control` does not enable
`docker.read`; enabling both is the normal case but it is two deliberate acts.

This is more granular than strictly necessary, and that is the point: the
common mistake in permission systems is bundling, and the bundle is always
discovered after someone used the half they were not supposed to have. An
operator who wants a dashboard that can restart a container but not read its
logs can have exactly that.

### 1.2 Capabilities are not roles

A capability says *what may be done on this host*. A role says *which users may
do it*. Both must allow an action — see §4.

---

## 2. Default deny

Every capability on every host starts **off**.

A freshly enrolled agent will refuse every command, including ones from a
legitimate administrator, until a capability is explicitly enabled for that
host. The refusal is explicit (`capability_not_enabled`), not a silent
no-op, so the operator learns what to turn on.

This is an invariant, not a default that configuration can invert
([`PRODUCT_VISION.md §4`](PRODUCT_VISION.md#4-invariants-are-structural-capabilities-are-configurable)):

- There is no "enable all" switch, and no global capability setting that
  applies to future hosts. A new host is inert until someone decides
  otherwise, so adding a host can never silently grant control over it.
- The agent enforces the capability locally, from its applied configuration —
  it does not rely on the controller having checked. A compromised controller
  therefore cannot exceed the capabilities each host was already granted.
- Disabling a capability takes effect on the next config apply and closes any
  session it was permitting.

Double enforcement (controller *and* agent) is deliberate. The controller's
check produces good error messages; the agent's check is the one that holds
when the controller is the thing that has gone wrong.

---

## 3. Command set

The command set remains **closed and enumerated**. The agent rejects any type
it does not recognise, and there is no generic passthrough.

Phase 0 enumerated three members. The set now grows, organised by capability:

```go
// docker.control
CommandContainerStart   = "container.start"
CommandContainerStop    = "container.stop"
CommandContainerRestart = "container.restart"

// systemd.control
CommandUnitStart        = "unit.start"
CommandUnitStop         = "unit.stop"
CommandUnitRestart      = "unit.restart"
CommandUnitReload       = "unit.reload"

// sessions.control
CommandSessionAttach    = "session.attach"

// terminal  (stream-based, not a one-shot command — see §5)
CommandTerminalOpen     = "terminal.open"
```

Growing an enumerated set is not the same as opening it. Each member:

- maps to exactly one capability, and is refused unless that capability is
  enabled on the target host;
- carries only an **enumerated target** — a container id or unit name the agent
  itself discovered and reported — never a free-form string the agent passes
  to a shell;
- is added deliberately, with its authorization rule, audit entry and tests,
  in the same change. This mirrors ADR-013's reasoning for the role enum:
  widening an authorization surface should require a reviewed change.

### 3.1 Unit control needs a target allow-list

`systemd.control` with an arbitrary unit name is effectively root: stopping
`sshd` locks the operator out, and stopping the agent's own unit makes the host
unmanageable and unmonitored at once.

Therefore, when `systemd.control` is enabled, the operator also configures
**which units** it applies to. The default is an empty list — the capability
alone grants nothing. The agent additionally refuses, unconditionally:

- its own unit;
- units matching a compiled-in protective list (`sshd`, `systemd-*`,
  `dbus`, `network*`), overridable only by an explicit per-unit entry that the
  UI marks as dangerous.

Refusing to stop `sshd` by default is not paternalism — recovering from it may
require physical access, which is exactly the situation remote management
exists to avoid.

---

## 4. Authorization chain

Every management action is evaluated along **two independent axes**, as
required for multi-user support. Both must allow it.

```
   ┌─────────────────────────────┐        ┌──────────────────────────────┐
   │  WHO                        │        │  WHERE                       │
   │  user role                  │  AND   │  target host capability      │
   │  (Viewer/Operator/Admin)    │        │  (docker.control = true?)    │
   └─────────────────────────────┘        └──────────────────────────────┘
                    │                                    │
                    └────────────────┬───────────────────┘
                                     ▼
                         permitted, then audited, then dispatched
```

| Role | Read monitoring | `*.read` | `docker.control` | `systemd.control` | `sessions.control` | `terminal` |
| --- | :-: | :-: | :-: | :-: | :-: | :-: |
| Viewer | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |
| Operator | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |
| Administrator | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

`terminal` is Administrator-only and cannot be granted to an Operator. The
role enum stays the closed set of three from ADR-013 — granularity comes from
the capability axis, not from inventing more roles. A future per-host role
assignment (user X is an Operator on these hosts only) fits this model without
changing it, and is noted as an open question in
[ARCHITECTURE §14](ARCHITECTURE.md#14-open-design-questions).

The full ordered chain for a single action is unchanged from Phase 0 —
authenticate, authorize, audit *before* dispatch, validate target, scope to one
agent, expire, report — and is specified in
[ARCHITECTURE §10.3](ARCHITECTURE.md#103-authorization-and-audit-chain). Two
steps are added:

- **Capability check**, on the controller and again on the agent (§2).
- **Re-authentication** for `terminal` and for any action on a protected unit:
  a valid session is not sufficient, the user proves possession of their
  password again.

---

## 5. Remote terminal

This is the most dangerous capability in the product. It is specified in more
detail than anything else here for that reason.

### 5.1 What it is, and what it is not

It is an interactive shell session on a host, opened from the web interface,
multiplexed over the agent's existing outbound connection.

It is **not** a general command-execution API. There is no endpoint that takes
a command string and returns output. The only thing the controller can ask for
is "open a PTY"; everything after that is a byte stream the user drives
interactively. That distinction matters because an API that executes a
caller-supplied command is trivially scriptable by anything that steals a
token, whereas an interactive session requires a live, authenticated,
re-authenticated, recorded human session.

[`PRODUCT_VISION.md`](PRODUCT_VISION.md#clarification-unrestricted-remote-shell-execution)
sets out why this is a scope expansion rather than a reinterpretation, and
[ADR-022](ARCHITECTURE.md#adr-022-remote-terminal-is-an-opt-in-capability-superseding-the-blanket-shell-exclusion)
records the supersession.

### 5.2 Gates

A terminal session requires **all** of:

1. `terminal` capability enabled on that specific host.
2. Administrator role.
3. Re-authentication immediately before opening the session.
4. An active session cookie over TLS.

Each gate is independent. None has a bypass, and none is skipped for
convenience.

### 5.3 Boundaries

| Boundary | Rule |
| --- | --- |
| Identity | Runs as the agent's unprivileged user. **Never root.** |
| Escalation | No `sudo`, no `su`. The agent user is not in `sudoers`; if it were, the capability would be a root backdoor regardless of the other gates. |
| Shell | A fixed, configured shell path. Not caller-supplied. |
| Environment | Minimal, explicitly constructed. The agent's own environment — which holds its credential — is **not** inherited. |
| Idle timeout | Default 15 minutes, configurable within bounds. |
| Absolute timeout | Default 4 hours. No session outlives it. |
| Concurrency | Bounded per host and per user. |
| Revocation | An administrator can terminate any live session; disabling the capability or revoking the host's credential closes all of them immediately. |
| Transport | Multiplexed over the agent's outbound WebSocket. **No new port, no inbound listener, no SSH daemon.** The §1 invariant holds unchanged. |

The "not root, no sudo" rule is the one that makes the rest meaningful. With
it, a terminal session is bounded by what the agent user can already do; the
attack value of compromising the controller stays bounded by the same thing.

### 5.4 Recording is mandatory

Every session is recorded: who, which host, when, duration, and the full
input/output stream. Recording is **not optional and has no off switch** —
an unrecorded interactive session on a production host is precisely the
accountability gap this platform exists to close.

- Recordings are stored with their own configurable retention, with a
  non-zero minimum.
- Recordings may contain secrets the user typed. They are therefore treated as
  sensitive data: Administrator-only access, and their existence is noted in
  the UI before the session starts, so nobody is recorded unknowingly.
- Deleting a recording is itself an audited action.

### 5.5 Honest assessment of the risk

With this capability enabled on a host, an attacker who obtains an
Administrator session and can re-authenticate gets interactive, unprivileged
shell access to that host. That is a materially larger blast radius than
read-only monitoring, and no amount of design makes it otherwise.

What the design does provide: it is off by default, it is per host, it needs
the highest role plus re-authentication, it is not root, it cannot escalate,
it is time-bounded, it is revocable, and every keystroke is recorded. An
operator who judges the trade-off unacceptable leaves it off — which costs
them nothing, because off is the default.

---

## 6. Host privileges

The agent runs as a dedicated unprivileged user. Each capability needs a
specific, narrow grant, and the UI must state what it is asking for before the
operator enables it.

| Capability | Host grant needed | Honest note |
| --- | --- | --- |
| `docker.read`, `docker.control` | membership of the `docker` group | **Root-equivalent.** Docker socket access permits launching a privileged container that mounts the host filesystem. Stated plainly, not glossed over. |
| `systemd.read` | none beyond D-Bus introspection | |
| `systemd.control` | a polkit rule for the specific units | Grant per unit, not blanket `manage-units`. |
| `sessions.read` | read access to session sockets | Usually same-user only. |
| `sessions.control` | access to the session owner's socket | Attaching inherits that shell's privileges, including root if the session is root's. |
| `terminal` | none beyond the agent user | The absence of a grant here is the point. |
| `smart_*` modules | raw device access | Prefer a narrow capability or a device ACL over adding the user to `disk`. |

The `docker` group caveat deserves repeating because it undercuts the
"never root" property of §5.3: on a host where `docker.control` is enabled, the
agent user can obtain root through Docker. Operators enabling both
`docker.control` and `terminal` on the same host should understand that the
combination is effectively root access, and the UI says so at the point of
enabling.

The reference systemd unit in
[`agents/linux/README.md`](../agents/linux/README.md) applies
`NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`,
`RestrictAddressFamilies`, `RestrictNamespaces` and
`MemoryDenyWriteExecute`. Some of these must be relaxed for specific
capabilities; each relaxation is documented with what it costs.

---

## 7. Audit

Audit logging is an invariant with no off switch.

Recorded for every management action, before dispatch:

| Field | Note |
| --- | --- |
| Actor | User id, role, session id |
| Source | Client IP, user agent |
| Target | Host id, object (container id, unit name) |
| Action | Command type |
| Authorization | Which capability and role permitted it |
| Re-authentication | Whether required, and whether satisfied |
| Timestamp | Dispatch time |
| Outcome | Success or failure **with a reason** — a failure with no reason is not auditable |
| Correlation | Request id, linking to the structured logs |

The log is append-only. Entries are written **before** dispatch, so an action
that crashes mid-flight is still recorded; the outcome is a later update to the
same entry. Writing afterwards would lose exactly the events most worth
knowing about.

Refused attempts are audited too. "Viewer tried to restart a container" is a
security-relevant signal, and discarding it would hide the reconnaissance phase
of an attack.

---

## 8. Still excluded

Unchanged from [`PRODUCT_VISION.md`](PRODUCT_VISION.md#what-this-is-not), and
not softened by the terminal capability:

| Excluded | Why |
| --- | --- |
| A "run this command" API | An endpoint accepting a caller-supplied command line is scriptable by anything that steals a token. The enumerated command set exists to prevent it. |
| Root or `sudo`-capable execution | The agent is unprivileged and does not escalate. |
| Capabilities on by default | Default deny is an invariant (§2). |
| Unrecorded interactive access | Recording has no off switch (§5.4). |
| Autonomous self-healing or automatic remediation | Restarting a service to clear a symptom destroys the evidence and can worsen an outage. Actions are user-initiated. |
| Automatic root-cause analysis, AI-generated explanations | A plausible-sounding guess about a production failure is worse than no answer. |
| Arbitrary binaries for collection | Collectors use a fixed allow-list with fixed argv ([ADR-021](ARCHITECTURE.md#adr-021-external-collector-binaries-on-a-fixed-allow-list)). |
