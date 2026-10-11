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

CPU, memory, swap, disk and network metrics; host availability and uptime;
hardware and OS inventory; historical usage. Multiple Linux hosts managed
centrally through a lightweight agent.

### Docker monitoring and management

Automatic container discovery on connected hosts. Container state and resource
usage. Log viewing with search. Start, stop and restart — user-initiated,
authorized and audited. Compose project grouping in a later release.

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

### 2. Never invent information

Logs are shown as they were emitted. Parsed fields are stored alongside the raw
message, never instead of it. When a log has no structured severity, that is
recorded as "no level present" — not guessed, and not defaulted to INFO.

Unknown is a first-class state. A host that has not reported is shown as
**No data**, never as 0% and healthy. A monitoring tool that renders missing
data as nominal is actively dangerous.

### 3. Management actions are explicit, bounded and auditable

Management is a closed set of operations — start, stop, restart a container —
each authenticated, authorized, audited before dispatch, and scoped to one
host. There is no generic passthrough and no shell.

### 4. Security is structural, not configurable

The properties that matter are enforced by architecture rather than left to a
settings page:

- The agent has no listening socket and accepts no inbound connections.
- The Docker socket is never exposed on the network, proxied or forwarded.
- Agents authenticate per host; one agent's credential cannot read the fleet.
- Agent-submitted data is untrusted input and attributed server-side.

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
| Unrestricted remote shell execution | Root-equivalent remote execution is a far larger security liability than the convenience is worth. The closed command set exists precisely to avoid it. |

This is a monitoring, notification and user-controlled management platform.

## Non-goals for the foreseeable future

Not excluded on principle, just not what this is for: multi-tenancy,
Kubernetes orchestration, APM and distributed tracing, cloud-provider
integrations, and log retention at compliance scale.

## How success is measured

1. An operator installs it and sees real data from one host in under ten
   minutes.
2. They write a notification rule, test it against past events, and trust it.
3. When something breaks at 3am, the notification tells them what and where
   accurately enough to act.
4. They stop SSHing into hosts to check whether things are running.
5. They can read the architecture and understand the security model without
   reading the source.
