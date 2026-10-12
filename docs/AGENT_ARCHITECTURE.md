# Agent Architecture

How agents connect, authenticate, receive configuration, and behave when the
controller is unreachable.

> **Status: design.** None of this is implemented. The agent today
> ([`agents/linux`](../agents/linux)) runs its lifecycle only: it loads and
> validates configuration, logs, ticks, and shuts down cleanly. The controller
> protocol exists as a Go interface with no implementation, deliberately — see
> [`internal/controller/client.go`](../agents/linux/internal/controller/client.go).
> Delivery is tracked in [`ROADMAP.md`](ROADMAP.md).

- [1. Topology](#1-topology)
- [2. Transport](#2-transport)
- [3. Agent identity](#3-agent-identity)
- [4. Enrollment](#4-enrollment)
- [5. Credential rotation and revocation](#5-credential-rotation-and-revocation)
- [6. Configuration synchronisation](#6-configuration-synchronisation)
- [7. Capability reporting](#7-capability-reporting)
- [8. Offline behaviour](#8-offline-behaviour)
- [9. Heartbeat](#9-heartbeat)

---

## 1. Topology

One controller, many agents. Every agent is a client; the controller is never
a client of an agent.

```
                    ┌───────────────────────────┐
                    │        Controller         │
                    │   (apps/server, one DB)   │
                    └─────────────▲─────────────┘
                                  │
          all connections outbound from the agent, TLS
                                  │
      ┌───────────────┬───────────┴──────────┬───────────────┐
      │               │                      │               │
┌─────┴──────┐ ┌──────┴───────┐      ┌───────┴──────┐ ┌──────┴──────┐
│ LAN host   │ │ LAN host     │      │ VPS, public  │ │ host behind │
│ 10.0.0.10  │ │ 10.0.0.11    │      │ internet     │ │ CGNAT       │
└────────────┘ └──────────────┘      └──────────────┘ └─────────────┘
```

**One protocol for every location.** A LAN host, a rented VPS and a machine
behind carrier-grade NAT all speak the same protocol over the same transport.
There is no "remote mode". This matters because the alternative — a LAN
protocol plus a tunnelled remote protocol — doubles the security surface and
means the less-used path is the less-tested one.

The property that makes this work is that **agents dial out**. Consequences:

- No inbound firewall rule, port forward or reverse tunnel on any monitored
  host.
- NAT, CGNAT and dynamic addresses are non-issues: the agent's address never
  needs to be reachable or stable.
- No listening socket on the agent means no agent port to scan or exploit.
- A compromised controller cannot reach into hosts. It can only answer
  connections that agents choose to open, and what it may say on them is
  bounded by the enumerated command set
  ([`REMOTE_MANAGEMENT.md`](REMOTE_MANAGEMENT.md#3-command-set)).

This is an invariant, not a setting. See
[`PRODUCT_VISION.md`](PRODUCT_VISION.md#4-invariants-are-structural-capabilities-are-configurable).

---

## 2. Transport

Two channels, both opened by the agent:

| Channel | Protocol | Carries |
| --- | --- | --- |
| Request | HTTPS, JSON | Enrollment, heartbeat, config fetch and ACK, metric and log batches, command results |
| Stream | WebSocket over HTTPS, agent-initiated | Pending-command delivery, event-driven module output, terminal session frames |

### 2.1 Why a stream channel, and what it does not change

Phase 0's design said commands are **polled**, not pushed
([ARCHITECTURE §10.2](ARCHITECTURE.md#102-invariants)). Polling cannot carry an
interactive terminal, and makes event-driven Docker log streaming either
laggy or wasteful.

The resolution is an agent-initiated WebSocket. The security property that
mattered was never "polling specifically" — it was **the agent opens every
connection and listens on nothing**. A WebSocket the agent dials out and holds
open preserves that exactly. See
[ADR-018](ARCHITECTURE.md#adr-018-agent-initiated-websocket-replaces-command-polling).

Guarantees kept:

- The agent still has no listening socket.
- The controller still cannot initiate a connection.
- The agent still only acts on enumerated command types and rejects anything
  else.
- Commands still carry `expires_at`, so one delivered after a partition cannot
  execute against changed state.

If the WebSocket cannot be established — a proxy that blocks upgrades, for
example — the agent falls back to long-polling the request channel. Latency
degrades; nothing else does. Capabilities that need a live stream (terminal,
log streaming) are reported unavailable rather than silently approximated.

### 2.2 Reconnection

Exponential backoff with full jitter, capped (target: 1 s initial, 5 min
ceiling). Jitter is not cosmetic: a controller restart would otherwise be
followed by every agent in the fleet reconnecting in lockstep.

---

## 3. Agent identity

| Concept | Meaning |
| --- | --- |
| Host | The controller's record of a machine. Created by an administrator. Holds configuration, capabilities and history. |
| Agent instance | A running agent process bound to one host record. |
| Agent credential | The long-lived secret proving "I am the agent for host X". |

An agent credential authorizes exactly one thing: **reporting as that host and
receiving that host's commands.** It is not a user credential with a different
shape. It cannot read another host's data, cannot enumerate the fleet, cannot
read configuration for other hosts, and cannot perform management actions —
agents *receive* commands, they never *issue* them.

This is what contains a compromised host: an attacker with root on one
monitored machine gets that machine's credential, and that credential is worth
one machine.

### 3.1 Identity continuity

The host record is the stable identity. A reinstalled machine that re-enrolls
against the same host record keeps its history; one enrolled fresh gets a new
record. Which happens is the administrator's explicit choice, not an inference
from a hostname or a MAC address — both of which change for innocent reasons
and are trivially forgeable.

The agent reports a `machine_id` and `boot_id` as **advisory** fields. They are
used to surface "this host record is now being used by what looks like a
different machine" in the UI, never to authenticate. Authentication is the
credential alone.

---

## 4. Enrollment

Agents are enrolled deliberately. There is no auto-discovery and no
self-service enrollment: a monitoring system that automatically trusts anything
claiming to be a host is one that anyone on the network can poison with
fabricated metrics, and fabricated metrics can fire or suppress notification
rules.

```
1. Administrator creates a host record in the UI.
       │
       ▼
2. Controller issues a ONE-TIME enrollment credential.
   - short-lived (default 15 minutes, HCC_AGENT_ENROLLMENT_TTL_SECONDS)
   - single-use
   - scoped to that one host record
   - revocable before use
   - stored as a SHA-256 digest (ADR-042); the plaintext is shown once
     and never again
       │
       ▼
3. Administrator runs `hcc-agent enroll`, with the credential in a file
   (mode 0600) rather than on the command line or in the environment.
       │
       ▼
4. The agent POSTs /api/v1/agents/enroll, with the credential in an
   Authorization: Bearer header — never in the URL.
       │
       ▼
5. Controller consumes the credential and returns
   - host_id and host_name
   - a long-lived agent credential
   - the protocol version and heartbeat interval
   (Configuration distribution is a later milestone; §6 is the design.)
       │
       ▼
6. Agent writes the credential to /etc/hcc-agent/credential atomically,
   at mode 0600, refusing a symlink or a world-writable directory.
       │
       ▼
7. Steady state: an outbound authenticated WebSocket, heartbeating.
```

> **Implemented in milestone 1.8**, with three differences from what this
> section originally described, each recorded where it was decided:
>
> - **SHA-256, not Argon2id** — [ADR-042](ARCHITECTURE.md#adr-042-agent-credentials-are-hashed-with-sha-256-not-argon2id).
>   The rule for passwords does not transfer to 256-bit CSPRNG values, and a
>   salted hash cannot be looked up by index.
> - **Fifteen minutes, not an hour.** This is the secret that gets pasted into
>   a terminal; a shorter window is what makes that survivable.
> - **No `host_id` anywhere in the request.** The host is decided by the
>   credential, so there is no field for an agent to claim to be a machine it
>   does not hold a credential for.

### 4.1 Why two different secrets

The one-time token and the steady-state credential are deliberately separate.
The token is the one that gets pasted into a terminal, lands in shell history,
and is read over someone's shoulder. Making it single-use and short-lived
means that exposure is survivable. The long-lived credential never passes
through a human's clipboard.

### 4.2 Credential handling rules

All implemented and tested as of milestone 1.8.

- **Never a command-line flag.** Command lines are readable by every local user
  through `/proc`. Neither secret is a flag, and
  `TestNoSecretIsSettableViaAFlag` fails if one is added.
- **Neither file may be group- or world-readable.** The agent refuses to use a
  credential that is not a secret, rather than fixing the mode itself —
  silently chmod-ing a file it did not create would hide a misconfiguration
  the operator needs to see. Measured: `0640`, `0644` and `0660` are refused,
  `0600` and `0400` accepted.
- **Neither file may be a symlink, nor owned by another user, nor sit in a
  directory other users can write to.** Each is the same local attack in a
  different shape: point the path at a file you own, and the agent either
  writes the secret where you can read it or reads a secret you chose.
- **Writes are atomic.** A temporary file in the same directory, fsynced, then
  renamed. A plain write interrupted mid-enrollment leaves a truncated
  credential that fails authentication with no indication the file is the
  problem.
- **Both secrets are stored hashed** (ADR-042). A stolen database dump yields
  digests that cannot be replayed.
- **Neither secret is ever in a URL.** Both travel in an `Authorization`
  header, including on the WebSocket handshake. A query parameter would be
  recorded by every proxy on the path and by the controller's own access log.

---

## 5. Credential rotation and revocation

### 5.1 Rotation

Agent credentials rotate on a schedule (default 30 days, configurable) and on
demand.

The agent initiates rotation, because only the agent can confirm it has
successfully stored the new secret:

```
agent → POST /api/v1/agents/credential/rotate   (authenticated with current credential)
       ← new credential, plus an overlap window

agent  writes new credential atomically (temp file + fsync + rename)
agent → POST /api/v1/agents/credential/confirm  (authenticated with NEW credential)
       ← old credential invalidated immediately
```

The overlap window exists so that a crash between "received new" and "stored
new" is recoverable: until the agent confirms, both credentials are accepted.
Without it, a power cut at the wrong microsecond would strand a host and
require a physical visit to re-enroll it. The window is short (default 24 h)
and a credential that is never confirmed is reported in the UI as a stalled
rotation rather than left to expire quietly.

Rotation is a capability of the agent, not something the controller can force
mid-session — the controller cannot initiate anything (§1). To force one, an
administrator revokes instead.

### 5.2 Revocation

Revocation is immediate and server-side. There is no waiting for an expiry,
and no dependency on the agent cooperating.

| Action | Effect |
| --- | --- |
| Revoke credential | Next request from that agent is rejected with `unauthenticated`. Open WebSockets are closed. The host is marked **Unknown**, never healthy. |
| Disable host | As above, and the controller stops accepting data for it. Configuration and history are retained. |
| Delete host | Record and history removed, subject to a confirmation that states what will be lost. |

A revoked agent keeps running and keeps retrying with backoff. It does **not**
wipe its local buffer, in case the revocation was a mistake and is reversed.
It does not fall back to any other credential, and it does not re-enroll
itself — re-enrollment always requires a fresh administrator-issued token.

### 5.3 What revocation does not do

It does not reach into the host. The platform cannot uninstall the agent,
because that would require exactly the inbound control path §1 forbids.
Revoking cuts the host off from the controller; removing the software is a
local administrative act. This is stated plainly because an operator might
otherwise assume "revoke" means "remove".

---

## 6. Configuration synchronisation

Configuration is authored in the web interface, stored by the controller,
versioned, and pulled by agents. Agents never hold authoritative
configuration; they hold a cached copy of a controller-owned document.

### 6.1 Layering

Three layers, resolved by the controller into one flat document per host:

```
   global defaults           set once, apply to every host
          │
          ▼  overridden by
   host group settings       (planned; not in the first delivery)
          │
          ▼  overridden by
   per-host overrides        set on one host
          │
          ▼
   EFFECTIVE CONFIGURATION for host X, version N
```

**Resolution happens on the controller, not the agent.** The agent receives a
fully resolved document and never merges anything. Two reasons: the UI can
show exactly what a given host will run before it is applied, and layer
precedence bugs cannot differ between agent versions.

The UI must show, for any setting, whether the value is inherited or
overridden, and what the inherited value was. A configuration system that
cannot answer "why is this host collecting every 5 seconds?" is one operators
stop trusting.

### 6.2 The configuration document

```jsonc
{
  "host_id": "…",
  "version": 42,              // monotonic per host, assigned by controller
  "checksum": "sha256:…",     // over the canonical serialisation
  "generated_at": "2026-…",
  "modules": {
    "cpu":         { "enabled": true,  "schedule": { … }, "options": { … } },
    "temperature": { "enabled": true,  "schedule": { … }, "options": { … } },
    "smart":       { "enabled": false }
  },
  "capabilities": {           // management; see REMOTE_MANAGEMENT.md
    "docker.read": true,
    "docker.control": false,
    "terminal": false
  },
  "limits": { … }             // controller-enforced floors and ceilings
}
```

Disabled modules are present with `"enabled": false` rather than omitted, so
that "this module is off" and "this agent is too old to know about this
module" are distinguishable.

### 6.3 Delivery and acknowledgement

```
  heartbeat carries the agent's current config version
       │
       ├── versions match ───────────────► nothing to do
       │
       └── controller has a newer version
                │
                ▼
       agent GETs /api/v1/agents/config
                │
                ▼
       agent VALIDATES locally
                │
        ┌───────┴────────┐
        │                │
      valid          invalid
        │                │
        ▼                ▼
   write atomically   KEEP running the last known good config
   to disk as the          │
   new last-known-good     │
        │                  │
        ▼                  ▼
   apply              POST …/config/nack  { version, checksum, problems[] }
        │                  │
        ▼                  ▼
   POST …/config/ack   controller marks version REJECTED for this host
   { version, checksum,    and surfaces every problem in the UI
     applied_at }
```

Rules, each with a reason:

- **The agent validates before applying.** Controller-side validation already
  happened when the configuration was saved, so a NACK means either a bug or a
  host-specific mismatch (SMART enabled on a host with no `smartctl`) — and
  that is itself information worth surfacing.
- **An invalid configuration never replaces the last known good one.** The
  agent continues on its previous configuration. A monitoring agent that stops
  monitoring because someone typed a bad interval has failed at its job.
- **A NACK is explicit and structured**, listing every problem rather than the
  first. Mirrors the agent's existing startup config validation, which already
  reports all problems at once so a broken unit file can be fixed in one pass.
- **Rejections are not retried blindly.** A rejected version stays rejected
  until the operator changes it. Re-fetching the same bad document on a timer
  would turn one mistake into a log flood.
- **ACK carries the checksum, not just the version**, so a truncated or
  tampered document cannot be acknowledged as applied.
- **Applying is atomic**: temp file, `fsync`, `rename`. A crash mid-write
  leaves the previous configuration intact, never a half-written one.
- **No fleet-wide atomic apply.** Configuration is per host and rolls out as
  each agent checks in. A monolith has no need of distributed consensus
  (ADR-001), and an operator changing a global default expects it to land
  progressively, not to block on an offline host.

### 6.4 Drift detection

The heartbeat always reports the agent's running config version and checksum.
The controller compares against what it believes is current and surfaces, per
host: up to date, pending (newer version not yet acknowledged), rejected (with
reasons), or stale (not acknowledged within a grace period). "Stale" is shown
as a problem, not hidden — an agent silently running month-old configuration
is exactly the failure a dashboard exists to reveal.

---

## 7. Capability reporting

On enrollment and on every config apply, the agent reports what it actually
found on the host:

```jsonc
{
  "modules": {
    "temperature": {
      "available": true,
      "detail": "4 hwmon zones: coretemp, nvme, acpitz, amdgpu",
      "min_interval_seconds": 1
    },
    "smart": {
      "available": false,
      "reason": "smartctl not found in PATH"
    },
    "raid": {
      "available": false,
      "reason": "no mdadm arrays and no supported controller detected"
    }
  }
}
```

This exists because the alternative is a UI that lets an operator enable SMART
monitoring on a host that cannot do it and then shows an empty graph forever.
With a capability report the UI can state the reason up front, and
[§5 of `MONITORING_MODULES.md`](MONITORING_MODULES.md#5-hardware-reality)
covers the hardware cases that make this necessary.

Capability reports are **advisory input to the UI**, not authorization. An
agent claiming a capability does not thereby gain permission to use it;
permission comes from configuration the operator wrote.

---

## 8. Offline behaviour

The agent must keep working when the controller is down — during a controller
upgrade, a network partition, or a reboot of the machine hosting it.

| Concern | Behaviour |
| --- | --- |
| Collection | Continues on the last known good configuration. A controller outage must not create a monitoring gap. |
| Buffering | Samples and events are buffered on disk, bounded by a configured size and age. |
| Overflow | **Oldest data is dropped first**, and the drop is counted and reported on reconnect. Never silent. |
| Commands | None arrive, by definition. Queued commands expire; the agent refuses expired ones on delivery. |
| Terminal | Unavailable. There is no local fallback path, deliberately. |
| Configuration | Never expires locally. An agent unreachable for a month keeps running its last good configuration rather than reverting to defaults, which would change behaviour at the worst possible moment. |
| Credential | Rotation is skipped while offline; the existing credential is not self-expired. An agent must not lock itself out because the controller was unreachable. |

**Bounded buffering, not unbounded.** A disk filled by a monitoring agent is a
self-inflicted outage on a machine the operator cares about. The limit is
configurable with an enforced ceiling, and dropping oldest-first keeps the
most recent — and most diagnostically useful — data.

**Gaps are reported as gaps.** Backfilled data is submitted with its original
timestamps, and the UI distinguishes "no data was collected" from "data was
collected and is zero". Interpolating across a gap would manufacture
information, which [principle 2](PRODUCT_VISION.md#2-never-invent-information)
forbids.

---

## 9. Heartbeat

The one thing that is **always on and not configurable**. Every other module
can be disabled; the heartbeat cannot, because an agent that can be configured
into silence is indistinguishable from a dead one.

It is deliberately minimal — liveness and synchronisation state, no
measurements:

```jsonc
{
  "host_id": "…",
  "sent_at": "2026-…",
  "agent_version": "1.2.0",
  "uptime_seconds": 86400,
  "config_version": 42,
  "config_checksum": "sha256:…",
  "buffered_items": 0,
  "dropped_items_since_last": 0
}
```

Default interval 30 s, configurable within enforced bounds. Metric payloads
are a separate request on a separate schedule, so that heartbeating and
ingestion fail, retry and rate-limit independently: a host rejected for
sending too many metrics must still be able to report that it is alive.

A host that misses its heartbeat window becomes **Unknown**, never
"healthy" and never "0%". See
[principle 2](PRODUCT_VISION.md#2-never-invent-information).
