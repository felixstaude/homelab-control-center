# Product Vision

## What this is

Homelab Control Center is a self-hosted, open-source platform for monitoring
and managing Linux servers, Docker containers and network devices from one
place.

It is built for the person who runs a handful of machines at home or in a small
office, has outgrown SSHing into each one to run `docker ps`, and does not want
to stand up Prometheus, Grafana, Loki, Alertmanager and an exporter per host to
answer "is everything fine, and if not, what broke".

## Who it is for

The **homelab operator**: technical, runs their own hardware, values their own
time, and is the only person on call.

Design consequences of that single assumption:

- **One person maintains the deployment.** Every added service is something
  they must back up, upgrade and debug alone. Hence: one control plane, one
  database, no broker (ADR-001, ADR-003).
- **They already have opinions about TLS and reverse proxies.** The platform
  does not impose a certificate mechanism; it sits behind whatever they run.
- **They are interrupted by notifications at 3am.** Notification rules must be
  precise and testable, not best-effort. An alerting system that cries wolf
  gets muted, and a muted alerting system is worse than none.
- **Their network may be air-gapped or segmented.** No external requests, no
  CDN, no telemetry (ADR-009).

## What it does

### Server monitoring

CPU, memory, swap, disk usage and I/O, network interfaces, temperature
sensors, SMART attributes, RAID health and systemd service state — each a
separate module the operator enables per host, with its own collection
schedule and retention. Host availability and uptime, hardware and OS
inventory, and historical usage across many Linux hosts, all through one
lightweight agent.

Every module is off until it is switched on. A fresh agent reports only that
it is alive. The full catalogue, with collection methods and hardware caveats,
is in [`MONITORING_MODULES.md`](MONITORING_MODULES.md).

### Docker monitoring and management

Automatic container discovery on connected hosts. Container state and resource
usage. Log viewing with search. Start, stop and restart — user-initiated,
authorized and audited. Compose project grouping in a later release.

Reading Docker state and controlling containers are separate capabilities, and
both are off by default. The same applies to systemd units and to
screen/tmux sessions. See
[`REMOTE_MANAGEMENT.md`](REMOTE_MANAGEMENT.md).

### Log monitoring

Collection from configured sources, with original messages and timestamps
**preserved exactly as emitted**. Searchable, filterable, live views. Error
severity surfaced where a structured level genuinely exists. Configurable
parsing rules for unstructured output, because real applications do not agree
on log formats.

### Notification rules

Complete user control. Conditions on log level, message content, container
name, host and event source. Exact, substring and regular-expression matching.
AND/OR logic with nested groups. Per-rule cooldowns. Multiple destinations.
Dry-run testing against sample or historical events.

> Notify through Ntfy whenever a log message contains "network" and its
> severity is ERROR.

> Notify whenever any Docker container stops unexpectedly.

### Network monitoring

Manually registered devices, checked over ICMP and configurable TCP probes,
displayed in a topology the operator defines. Containers on connected Docker
hosts appear automatically.

## Principles

These are the commitments that shape the product. They are not aspirations;
they are constraints that have already been applied — see `docs/ARCHITECTURE.md`
for where each one shows up in the design.

### 1. The operator decides what matters

The platform surfaces facts and notifies according to rules the user wrote. It
does not decide on their behalf which events are important. A `MetricTile` in
Phase 0 deliberately has no notion of thresholds, because "82% memory is bad"
is a rule the alerting engine owns, not a presentational component.

This extends to the whole product: **anything that is reasonably a matter of
preference is configurable from the web interface, not compiled in.** The
operator chooses which monitoring modules run, what each one collects, how
often, how long the history is kept, which management capabilities are
permitted, and which events notify them.

Configurable does not mean unconfigured. Every setting ships with a safe
default, and the defaults are chosen so that a fresh install is useful and
cheap rather than maximal. Where a value has a technically necessary limit — a
polling interval a device cannot sustain, a retention period the disk cannot
hold — the limit is enforced and the reason is shown, instead of letting the
operator discover it as a failure.

The division is set out in [principle 4](#4-invariants-are-structural-capabilities-are-configurable).

### 2. Never invent information

Logs are shown as they were emitted. Parsed fields are stored alongside the raw
message, never instead of it. When a log has no structured severity, that is
recorded as "no level present" — not guessed, and not defaulted to INFO.

Unknown is a first-class state. A host that has not reported is shown as
**No data**, never as 0% and healthy. A monitoring tool that renders missing
data as nominal is actively dangerous.

### 3. Management actions are explicit, bounded, opt-in and auditable

Management is a closed, enumerated set of operations. Every one is
authenticated, authorized, audited before dispatch, and scoped to a single
host. There is no generic passthrough.

Capabilities are **modular and disabled by default**. Reading Docker state,
controlling Docker containers, reading systemd units, controlling systemd
units, attaching to a terminal — each is a separate switch, each off until the
operator turns it on for a specific host. Enabling one never implicitly
enables another: an operator who wants container restarts does not thereby
grant service control.

An interactive remote terminal is a planned capability, and it is the most
dangerous thing this product will ever do. It is off by default, requires
explicit per-host authorization, is restricted to the Administrator role, and
is fully recorded. See
[`REMOTE_MANAGEMENT.md`](REMOTE_MANAGEMENT.md#5-remote-terminal) for why that
is not the same thing as the unrestricted shell execution this project still
refuses to build.

### 4. Invariants are structural; capabilities are configurable

Principle 1 makes the product highly configurable. That has a hard boundary:
**configuration decides what the system does, never whether its security
properties hold.**

These are invariants. They are enforced by architecture, have no setting, and
no amount of configuration can switch them off:

- The agent has no listening socket and accepts no inbound connections.
- The Docker socket is never exposed on the network, proxied or forwarded.
- Agents authenticate per host; one agent's credential cannot read the fleet.
- Agent-submitted data is untrusted input and is attributed server-side.
- Management capabilities are default-deny; an unconfigured host permits
  nothing.
- Audit logging of management actions is always on. There is no switch.
- The agent executes only enumerated operations. There is no setting that
  turns it into a general-purpose command runner.

These are capabilities. They are configurable, and all of them start off:

- Which monitoring modules run, and what each collects.
- Collection intervals and schedules.
- Retention periods per metric category.
- Which management capabilities are permitted, per host.
- Which events notify whom, and how.

The practical test: a setting may widen what the platform *does*, and may
never weaken how it *protects*. A proposed setting that would relax an
invariant is a design error, not a feature request.

### 5. Honest about what is not built

An unbuilt feature says so, naming the milestone that delivers it. Sample data
is labelled as sample data. No placeholder pretends to work.

## What this is not

Excluded by design, not deferred:

| Not included | Why |
| --- | --- |
| AI-generated error explanations | Plausible-sounding guesses about production failures are worse than no answer. The operator's own judgement, given accurate data, is the product. |
| Automatic root-cause analysis | Same reason. Correlation presented as causation misleads during exactly the incidents that matter. |
| Autonomous self-healing | Restarting a service to clear a symptom destroys the evidence needed to find the cause, and can make an outage worse. |
| Automatic remediation | Actions are user-initiated. The operator knows context the platform does not. |
| Unrestricted remote shell execution | See the clarification below. An always-available, unauthenticated-by-default, root-capable command channel is still refused outright. |

This is a monitoring, notification and user-controlled management platform.

### Clarification: "unrestricted remote shell execution"

Earlier revisions of this document listed remote shell access as permanently
excluded, full stop. That wording conflated two different features, and it is
now stated precisely.

**Still excluded, permanently:**

- A general-purpose "run this command" API. There is no endpoint, and no agent
  command type, that accepts a caller-supplied command line.
- Root or `sudo`-capable execution. The agent does not run as root and does not
  escalate.
- Any execution path that is available without the operator having explicitly
  turned it on for that specific host.
- Execution that is not recorded.

**Now a planned, opt-in capability:** an interactive terminal session to a
host, which is

- disabled by default and enabled per host, never fleet-wide by default;
- restricted to the Administrator role;
- run as the agent's unprivileged user, not root;
- fully session-recorded, with the recording retained under its own policy;
- bounded by an idle and absolute timeout;
- revocable mid-session.

**Why the change.** The original blanket exclusion was aimed at the real
hazard: a monitoring agent that doubles as an unauthenticated root backdoor on
every host it touches. That hazard is addressed by default-deny, per-host
opt-in, role restriction, unprivileged execution and mandatory recording — not
by refusing to build a terminal at all. Refusing outright pushed operators
back to maintaining separate SSH access, which is strictly worse: it is
unaudited by this platform and often root-capable.

This is an honest expansion of scope and of the threat surface, not a
reinterpretation. It is recorded as
[ADR-022](ARCHITECTURE.md#adr-022-remote-terminal-is-an-opt-in-capability-superseding-the-blanket-shell-exclusion),
which supersedes the earlier blanket wording. Operators who want the old
guarantee get it by leaving the capability off, which is the default.

## Non-goals for the foreseeable future

Not excluded on principle, just not what this is for: multi-tenancy,
Kubernetes orchestration, APM and distributed tracing, cloud-provider
integrations, and log retention at compliance scale.

A **public plugin system** belongs here too. Community-developed modules are a
plausible long-term direction, and the internal module boundaries are being
kept clean with that in mind, but no plugin framework is being built now and
no extension API is being published. Committing to a plugin contract before
the first six modules exist would freeze the wrong abstraction. See
[`PLUGINS.md`](PLUGINS.md).

## How success is measured

1. An operator installs it and sees real data from one host in under ten
   minutes.
2. They write a notification rule, test it against past events, and trust it.
3. When something breaks at 3am, the notification tells them what and where
   accurately enough to act.
4. They stop SSHing into hosts to check whether things are running.
5. They can read the architecture and understand the security model without
   reading the source.
6. They can tune what is collected, how often, and how long it is kept, from
   the web interface — without editing a file on the server or restarting
   anything.
