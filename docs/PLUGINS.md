# Future Plugin Architecture

Community-developed monitoring modules are a plausible long-term direction.

> **Status: not being built.** There is no plugin framework, no extension API,
> and no stable interface. This document records the *intent* and the
> extension points being kept clean, so that a future plugin system is a
> tractable change rather than a rewrite. It is not a commitment to ship one.

- [1. Why not now](#1-why-not-now)
- [2. What is being kept clean](#2-what-is-being-kept-clean)
- [3. Plausible mechanisms](#3-plausible-mechanisms)
- [4. Questions that must be answered first](#4-questions-that-must-be-answered-first)
- [5. What would not change](#5-what-would-not-change)

---

## 1. Why not now

A plugin system is a **published contract**. Publishing one before the first
six internal modules exist would freeze an abstraction derived from guesses.

Concretely, the module interface cannot be known to be right yet. The modules
already designed in [`MONITORING_MODULES.md`](MONITORING_MODULES.md) differ in
ways that will reshape it:

- `cpu` reads a file and returns numbers.
- `docker` holds a long-lived event stream *and* needs periodic
  reconciliation.
- `smart_selftest` triggers a hardware operation that takes hours and must
  report progress.
- `temperature` must enumerate hardware that may not exist and explain its
  absence.
- `system_logs` produces log records, not metrics, on a different pipeline.

An interface that accommodates all five honestly is one discovered by writing
all five. Freezing it after the first would mean either breaking plugin
authors later, or carrying a bad abstraction permanently — and third-party code
is precisely what makes an interface expensive to change.

The second reason is security. A plugin system is an arbitrary-code-execution
feature by definition. This project's entire security posture rests on the
agent executing only enumerated operations
([`PRODUCT_VISION.md §4`](PRODUCT_VISION.md#4-invariants-are-structural-capabilities-are-configurable)),
with external binaries restricted to a compiled-in allow-list
([ADR-021](ARCHITECTURE.md#adr-021-external-collector-binaries-on-a-fixed-allow-list)).
Letting operators install third-party collectors onto hosts is a direct
tension with that, and resolving it needs §4 answered first — not hand-waved
at design time.

Recorded as
[ADR-024](ARCHITECTURE.md#adr-024-no-plugin-system-but-keep-the-module-boundary-clean).

---

## 2. What is being kept clean

These are design habits being followed now, at no cost, that keep a future
plugin system tractable:

| Extension point | Current state | Why it helps later |
| --- | --- | --- |
| **Module interface** | Internal Go interface; every module implements the same contract (declare, detect availability, schedule, collect, report) | A plugin host would implement the same contract. No parallel code path. |
| **Module descriptor** | Declarative: name, permitted scheduling kinds, minimum interval, options schema, metric categories | A plugin ships a descriptor; the controller validates it the same way it validates a built-in. |
| **Scheduling** | One shared model with no module-specific logic ([`SCHEDULING.md`](SCHEDULING.md)) | Plugins get scheduling for free, and cannot invent their own timing behaviour. |
| **Configuration** | Modules appear as entries in the config document under a name key; options validated against a schema | A plugin's configuration is structurally identical to a built-in's. |
| **Metric ingestion** | Series identified by `(host, module, metric, labels)` with no built-in-only assumptions | Plugin metrics need no schema change. |
| **Capability reporting** | Modules report availability with a reason ([`AGENT_ARCHITECTURE.md §7`](AGENT_ARCHITECTURE.md#7-capability-reporting)) | A plugin that cannot run explains itself through the existing path. |
| **Retention categories** | Per-category, declared by the module | Plugin data ages out under the existing rules and storage budget. |
| **Failure isolation** | Each module fails independently; one broken collector never stalls the scheduler | A misbehaving plugin degrades one module, not the agent. |

The constraint being honoured: **no code path exists that only built-in modules
can use.** If a built-in module ever needs a privileged shortcut, that is the
signal the abstraction is wrong — and it is cheap to fix now and expensive to
fix after publishing.

The interface itself remains **explicitly unstable** and may change in any
release. That freedom is what makes it worth keeping clean.

---

## 3. Plausible mechanisms

Recorded for completeness. No preference is being committed to.

| Mechanism | Pros | Cons |
| --- | --- | --- |
| **Out-of-process, stdio protocol** | Strong isolation; any language; a crash cannot take the agent down; sandboxable with the same OS primitives the agent already documents | A process per module; serialisation cost; packaging and distribution burden |
| **WebAssembly** | Memory-safe sandbox, capability-based by construction, portable, no subprocess | No direct filesystem or socket access — which is what collectors need, so every host interaction needs a brokered API; immature Go tooling for this shape |
| **Go plugins (`plugin` package)** | Native performance; shares the existing interface directly | Linux-only in practice, requires exact toolchain and dependency matching, **no isolation whatsoever** — a plugin bug crashes the agent and a malicious plugin owns it. Almost certainly disqualifying. |
| **Controller-side only** | Plugins never touch monitored hosts; much smaller risk | Cannot collect host-local data, which is most of the value |
| **Declarative, no code** | A manifest describing a file to read and how to parse it; no execution at all | Covers only the simplest collectors — but covers them safely, and may be the right first step |

The declarative option deserves emphasis: a large share of plausible community
modules are "read this `/sys` file, parse this number, label it this way". That
can be expressed as data rather than code, needs no sandbox, and would deliver
much of the benefit with none of the arbitrary-execution problem. If a plugin
system ever ships, this is the most likely first form.

---

## 4. Questions that must be answered first

No plugin system ships until each of these has a concrete answer.

**Security**

1. What is the trust model? Does installing a plugin grant it everything the
   agent user can do, or is it capability-restricted?
2. How is a plugin sandboxed on the host — and if it is not, is that stated as
   plainly as the `docker` group caveat is today?
3. Are plugins signed? By whom? What does the agent do with an unsigned one?
4. Can a plugin read the agent's credential? (It must not, which rules out
   anything sharing the agent's address space.)
5. How does a plugin interact with the enumerated-command-set invariant? A
   plugin that can run code has effectively broken it unless sandboxed.
6. What is the revocation story for a plugin found to be malicious after
   deployment?

**Operations**

7. Who distributes plugins? A registry is infrastructure this project has
   deliberately avoided.
8. How are plugins updated, and does a bad update brick monitoring?
9. What happens when a plugin is incompatible with the agent version, and who
   notices?
10. Does the operator install on each host, or does the controller distribute
    to agents? (Distribution means the controller can push code to hosts —
    a significant change to the threat model.)

**Product**

11. Does a plugin's data participate in notification rules and retention like
    a built-in? (It should, or plugins are second-class and nobody uses them.)
12. How are plugin configuration forms rendered without the controller
    executing plugin-supplied UI code?

Question 10 is the pivotal one. Controller-side distribution is the feature
operators would want, and it is also the one that turns the controller into a
code-distribution channel for every host — contradicting the property that a
compromised controller cannot reach into hosts
([`AGENT_ARCHITECTURE.md §1`](AGENT_ARCHITECTURE.md#1-topology)).

---

## 5. What would not change

Whatever mechanism is eventually chosen, these are not negotiable:

- **Plugins are disabled by default**, like every module
  ([`MONITORING_MODULES.md §2`](MONITORING_MODULES.md#2-everything-is-off-by-default)).
- **No inbound ports.** A plugin cannot make the agent listen.
- **No credential access.** A plugin never sees the agent's credential or the
  controller's address in a usable form.
- **The enumerated command set stays closed.** A plugin cannot add a management
  command, and cannot be a route to arbitrary remote execution.
- **Scheduling, retention and storage limits apply unchanged.** A plugin cannot
  opt out of the storage budget.
- **Failure isolation.** A broken plugin degrades one module.
- **Honest capability reporting.** A plugin that cannot run says why.
- **The operator decides.** No plugin is installed, enabled or updated without
  an explicit action.
