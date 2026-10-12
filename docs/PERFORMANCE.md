# Performance

Measured figures, with the measurement recorded so it can be repeated and
argued with. Nothing in this document is an estimate unless it says so.

The reason this file exists is narrow: it is easy to write "supports a fleet of
a thousand hosts" in a roadmap and much harder to be able to answer "measured
how?". Where a number here is small enough to be uninteresting, that is the
useful answer; where the honest answer is "less than you might assume", it says
so.

Re-run everything here with:

```bash
make db-up
apps/server/.venv/bin/python scripts/measure-agents.py --hosts 1000 --connections 500
```

The script creates its own disposable database, starts a real controller
process, and drops the database afterwards. It never touches the developer's
own database.

---

## 1. How many agents one controller holds: **500, and that is the limit**

**Measured, and it is lower than the roadmap's eventual target.** Said plainly
because the alternative is a number nobody checked.

`hcc.agents.registry.MAX_CONNECTIONS` caps one controller **process** at 500
concurrent agent connections. The documented deployment is one process
([ADR-046](ARCHITECTURE.md#adr-046-one-controller-process-stated-rather-than-enforced)),
so **500 is the supported fleet size today**, not a thousand. The 501st
connection is refused with a `503` and a reason the agent retries on, which is
a bounded and visible failure rather than an unbounded one discovered when the
process is killed.

What that costs, at the cap:

| | 200 connections | 500 connections |
| --- | --- | --- |
| Controller RSS, idle | 93.6 MiB | 93.7 MiB |
| Controller RSS, connected | 139.6 MiB | 203.0 MiB |
| **Per connection** | **235 KiB** | **224 KiB** |
| Accept latency | 8.6 ms each | 10.4 ms each |
| Heartbeat round trip | 3.82 ms | 5.33 ms |
| Heartbeat throughput, serial | 262/s | 188/s |

Per-connection cost is flat between the two runs, which is the property worth
having: it means the cost is per socket rather than something superlinear in
the number of them.

### Is 188 heartbeats a second enough for 500 hosts?

Yes, with room. 500 hosts at the 30-second interval is **17 heartbeats a
second**, so the measured serial capacity is about eleven times what the cap
requires — and that figure is *serial*, one round trip at a time from a single
client. The controller handles them concurrently.

Extrapolating to a thousand hosts: 33 heartbeats a second against 188/s
serial. The arithmetic is comfortable. **It has not been measured**, because
the registry refuses the connections, and a figure obtained by raising the cap
for a benchmark would be a measurement of something nobody runs.

### What raising the cap would need

Not a bigger number. The limits that would actually bind are the ones this run
did not exercise: a thousand distinct peers rather than one client, real
network latency and TCP buffers per peer, and file-descriptor limits. The unit
file sets `LimitNOFILE=1024`, which is the agent's own bound; the controller's
is whatever its service manager grants.

---

## 2. The heartbeat write: **1.4–1.8 ms, 2.88 million a day at a thousand hosts**

The most frequent write in the system, and the one worth knowing the cost of
before the fleet grows.

| | Measured |
| --- | --- |
| Guarded `UPDATE`, per write | 1.397 ms (2,000 iterations), 1.790 ms on a second run |
| 1,000 hosts at 30 s | 2,880,000 writes/day |
| Share of one core | **4.7–6.0%** |

"Guarded" means the real statement, with the epoch and connection-id
predicates and the `updated_at` trigger firing — not a bare `UPDATE`. The
variance between runs is this machine's, not the query's.

Six percent of a core for a thousand hosts' liveness is the answer to "is a
row per heartbeat affordable", and it is why there is **no row per heartbeat**
in a history table: the same thousand hosts would write 2.88 million *inserts*
a day to hold information whose only consumer is "when did we last hear from
this host". The host row is updated in place instead
([`hcc/models/host.py`](../apps/server/hcc/models/host.py)).

---

## 3. The host list: **0.3 ms at a thousand hosts**

The query every browser poll runs, with both correlated subqueries the list
endpoint uses — the pending-enrollment count and the live-credential
timestamp.

| | Measured, 1,000 hosts |
| --- | --- |
| Page 1, execution | **0.28 ms** |
| Page 1, planning | 1.21 ms |
| Last page (`OFFSET 975`) | 0.24 ms |
| `count(*)` | 0.09 ms |

Two things worth reading off that table:

- **Planning costs more than execution.** At this scale the query is
  effectively free and the plan is the whole cost, which is what a
  `LIMIT`-bounded index scan over a thousand rows should look like. There is
  nothing here to optimise.
- **The last page is no slower than the first.** Offset pagination degrades at
  depth — `OFFSET 10000` makes PostgreSQL walk and discard ten thousand rows —
  and at a thousand hosts it has not begun to. The audit log uses keyset
  pagination because it genuinely needs it
  ([`hcc/services/audit.py`](../apps/server/hcc/services/audit.py)); the host
  list does not, and the measurement is why that is a decision rather than an
  oversight.

### The connectivity filter is the one that does not scale

Connectivity is derived rather than stored
([ADR-043](ARCHITECTURE.md#adr-043-connectivity-is-derived-never-stored-as-a-boolean)),
so the list endpoint applies that filter **in Python, to the page it already
fetched**. A connectivity-filtered page can therefore return fewer rows than
its limit, and the interface says so rather than pretending otherwise.

Not measured, because it is arithmetic rather than a query: filtering 25 rows
in Python is free, and the behaviour is not slow — it is *incomplete*, which
is a correctness trade and not a performance one. The fix when tens of hosts
becomes thousands is a stored projection refreshed on heartbeat, not a second
copy of the derivation rule.

---

## 4. What has not been measured

Listed because a performance document that only contains good news is one
nobody should trust.

| Not measured | Why it matters | When it will |
| --- | --- | --- |
| A thousand **distinct peers** | Everything above is one client on loopback. Real peers cost kernel buffers, file descriptors and TCP state per connection. | Before any claim above 500 hosts |
| A real network | No latency, no loss, no proxy. A reverse proxy's own connection limits are a likely first bottleneck. | Before a deployment guide claims a fleet size |
| Sustained load | Minutes, not days. A slow leak and a connection that is never reclaimed both look fine for minutes. | Before Phase 3's metric ingestion |
| Metric ingestion | Nothing is collected yet, so there is nothing to measure. The figures in [`DATA_RETENTION.md §7`](DATA_RETENTION.md#7-expected-volume) remain arithmetic. | Phase 3, and [§8 there](DATA_RETENTION.md#8-benchmarks-required-before-metric-storage-ships) says so |
| Two controller processes | ADR-046 states the degradation rather than measuring it. | When configuration distribution needs a real channel |

---

## 5. The machine these figures came from

Figures are comparative, not absolute, and they are worth nothing without
this:

- Fedora 44, Linux 7.2.5
- PostgreSQL 17 in a container on the same host, over loopback
- Python 3.14, one Uvicorn worker
- Everything on one machine, including the measuring client — so the client's
  own CPU use is competing with the controller's

A slower disk would move the heartbeat figure; a faster one would not move the
query figures, which are already dominated by planning.
