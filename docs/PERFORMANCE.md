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

## 4. Metric storage and querying: **measured in Phase 3**

From `make measure-metrics` (`scripts/measure-metrics.py`), against a
disposable database: 20 hosts, six hours of history each, a ten-second
interval, `cpu` and `memory` enabled — the shape the
[Debian guide](development/ENABLING_MONITORING_DEBIAN.md) tells an operator to
start with. 259,200 samples.

| What | Measured |
| --- | --- |
| Ingestion throughput | **4,497 samples/s** through the real validation path |
| Per batch | **1.25 ms** median, **2.00 ms** p95 |
| Raw storage | **88 bytes per sample**, including indexes |
| Five-minute tier | **30.5:1** smaller than the raw it replaces |
| Current values for one host | **0.5 ms** |
| Series list for one host | **0.4 ms** |
| Range query, any window | **0.6–0.8 ms**, index scan, one partition touched of twelve |
| Rollup | **5,000 buckets in 0.46 s** — and it hit the per-run bound |

Deliberately through `MetricsService.ingest` rather than `COPY`: the four
validation layers, the series cache and the partition router are what this
system costs, and a `COPY` benchmark would measure PostgreSQL instead.

### Extrapolated, and labelled as such

A hundred hosts collecting CPU and memory every ten seconds is nine samples
per host per interval, so **4.4 GiB of raw data for seven days** at 88 bytes
each. That is arithmetic on a measured per-sample cost, not a measurement —
it assumes the per-row cost holds at thirty times the volume, which is likely
and unverified.

### The rollup run hit its bound, and that is reported

`MAX_BUCKETS_PER_RUN` is 5,000, and the run above reached it. So 0.46 s is the
cost of a *capped* pass, not of catching up: an installation whose job has
been broken for a day needs several passes. Quoting the duration without the
cap would have understated it.

### `sample_count` costs more than its SQL, and it is not clear why

The count query plans in 0.46 ms and executes in 0.56 ms. The service method
measured in the latency table costs roughly ten times that on a database with
twenty-one partitions. It is **not** the ORM — `in_()` and `ANY()` were
measured as identical — and the cause is not established. It grows with
partition count, so it is recorded here as a characteristic to watch rather
than a number to rely on.

It matters less than it would have: the endpoint that uses it is fetched once
per page visit rather than polled. It was briefly polled every thirty
seconds, which was a defect, found by reading this benchmark's output.

### Two defects this benchmark found

Worth recording, because both were invisible to the test suite:

1. **Backfilled samples went to a partition nothing ever drops.** Partitions
   were created only *ahead* of now, so any sample timestamped before the
   current window — legitimate backfill, up to the protocol's seven-day bound
   — landed in the DEFAULT partition, which is never catalogued and therefore
   never expired. The benchmark writes history backwards and made it obvious:
   189,120 of 259,200 rows. Fixed by `PARTITIONS_BEHIND`; the job now also
   reports rows in DEFAULT as a warning, because it cannot fix that condition
   itself.
2. **`sample_count` was O(every sample in the installation).** 37 ms for a
   host with a hundred samples — the same as for a host with half a million,
   because the filter was on a joined table and the planner could not use the
   index. 0.044 ms after resolving the series ids first.

---

## 5. What the control plane sends a browser: **606 kB a first load, now 181 kB**

Measured in Phase 10, because until Phase 10 nothing had counted it. Serving
the web client from the control plane was new — development ran Vite on a
second port and the production deployment served no client at all — so the one
transfer every user of the installation makes had never been looked at.

```bash
cd apps/web && npm run build
make measure-transfer
```

Byte counts come from the ASGI `http.response.body` events rather than from an
HTTP client, and that is not pedantry: httpx decompresses transparently and
Starlette omits `Content-Length` when it streams a compressed response, so
`len(response.content)` is the *decoded* size for exactly the large responses
whose compression matters most. Measured through an HTTP client, the two
biggest chunks in this bundle appeared to compress to precisely their original
size.

### A first load

What a browser fetches before it can show the sign-in screen. The chart library
is lazy-loaded, so it is not in this set — `apps/web` already splits it.

| Response | Verbatim | gzip | Saved |
| --- | --- | --- | --- |
| `index.html` | 1,217 | 1,217 | — |
| the stylesheet | 34,863 | 7,624 | 4.57x |
| the entry chunk | 570,289 | 172,144 | 3.31x |
| **total** | **606,369** | **180,985** | **3.35x** |

Every asset cold — a whole session with an empty cache — is 969,806 bytes
verbatim and 286,820 gzipped, 3.38x.

`index.html` is deliberately not compressed. It is about a kilobyte, and it is
the one file served `no-store`, so it is fetched on every navigation; the saving
would be around 500 bytes and the response already fits in one segment.

### Metric payloads, which are the recurring cost

A bundle is cached for a year and fetched once per release. A host page
re-fetches its charts every time somebody opens it, and that turns out to be
the larger number.

| Response | Verbatim | gzip | Saved |
| --- | --- | --- | --- |
| one chart, 600 points, rollup | 59,526 | 6,762 | 8.80x |
| one chart, 600 points, raw | 59,663 | 3,731 | 15.99x |
| a host page, 4 charts | 237,134 | 24,355 | 9.74x |
| the server's cap, 50,000 points | 4,933,874 | 482,446 | 10.23x |

600 points is what `metric-history.tsx` actually requests. These compress an
order of magnitude — far better than the JavaScript — because an array of
ISO-8601 timestamps a fixed interval apart is about the most repetitive thing a
JSON encoder can emit.

### What was changed, and what was deliberately not

`/assets/*` is gzipped at the mount, and the four metric read endpoints are
gzipped by an allow-list. Nothing else is, and that boundary is a security
decision rather than a tuning one — see
[ADR-059](ARCHITECTURE.md#adr-059-compression-is-an-allow-list-because-breach-is-a-length-attack).

No optimization was made to the bundle itself. 570 kB for an entry chunk
containing React, the router, the query client and the icon set is unremarkable,
the charts are already split out, and 181 kB over a LAN is not a number worth
restructuring an application for. Measuring it and leaving it alone is the
result, not a gap in the work.

### What this does not show

Time. These are byte counts. Load time depends on the link, the round trips and
the parse, none of which this touches — and no browser-side timing has been
measured, so this document does not quote any.

---

## 6. One command to a working controller: **51 seconds, and a 354 MB image**

Measured by `deployment-e2e.sh` on a GitHub-hosted runner, which reports the
figures on every run. They are **reported and never asserted**: a runner's
speed varies by a factor of several between runs, so a threshold would fail for
the one reason that says nothing about the deployment. The ceiling that *is*
enforced is `wait_for`'s deadline — the scenario fails if the controller never
answers at all.

| Step | Measured |
| --- | --- |
| Build the image and start the stack | 48 s |
| From `compose up` returning to `/healthz` answering | 3 s |
| **One command to a ready controller, total** | **51 s** |
| Restart the controller and answer again | 4 s |
| The controller image | 354 MB |

The 3 seconds is the part that is the deployment's own: PostgreSQL starting, the
migrations applying, and the application coming up. The 48 seconds is the image
build, which an operator pays only on the build-from-source path — and that is
currently the documented default, because no image has been published.

The 4-second restart is the upgrade wait: what somebody sits through after
`up -d` on a new image, with the database already running and the migrations
already applied.

354 MB is a Python runtime, the dependency set, and a built SPA. Recorded
because "a Python image with a web client in it" is not a size, and a number
nobody checks is how an image quietly reaches two gigabytes.

### What this does not show

A cold registry pull, because there is nothing to pull. A slower machine: these
are a CI runner's figures and the build in particular is dominated by its CPU
and its network. And nothing about a second version — the restart figure is a
restart of the same image, not an upgrade across one.

---

## 7. What has not been measured

Listed because a performance document that only contains good news is one
nobody should trust.

| Not measured | Why it matters | When it will |
| --- | --- | --- |
| A thousand **distinct peers** | Everything above is one client on loopback. Real peers cost kernel buffers, file descriptors and TCP state per connection. | Before any claim above 500 hosts |
| A real network | No latency, no loss, no proxy. A reverse proxy's own connection limits are a likely first bottleneck. | Before a deployment guide claims a fleet size |
| Sustained load | Minutes, not days. A slow leak and a connection that is never reclaimed both look fine for minutes. | Before Phase 3's metric ingestion |
| Concurrent ingestion and maintenance | §4 is one writer and one reader. Contention between the rollup job and live ingestion is not exercised. | Before a fleet large enough for the two to overlap continuously |
| A cold cache | Everything in §4 is in PostgreSQL's shared buffers. A real ninety-day query reads from disk. | Before quoting a query figure for a long window |
| Ninety days of real data | §4 writes hours. Retention totals are extrapolated per sample. | When an installation has been running that long |
| Fleet-scale ingestion | §4's samples are written by one process in a loop, so it measures the storage path and not the socket, the scheduler, or a hundred real agents. | Before claiming a host count for collection |
| Two controller processes | ADR-046 states the degradation rather than measuring it. | When configuration distribution needs a real channel |
| Browser-side timing | §5 counts bytes. Time to first contentful paint, parse cost and render cost are all unmeasured, so no load-time claim appears anywhere. | Before any claim about how fast the interface feels |
| Compression CPU | §5 measures the bytes saved, not the microseconds spent. Assets are cached for a year, so the cost is paid about once per browser per release — argued rather than measured. | If a controller ever serves enough cache misses for it to show |

---

## 8. The machine these figures came from

Figures are comparative, not absolute, and they are worth nothing without
this:

- Fedora 44, Linux 7.2.5
- PostgreSQL 17 in a container on the same host, over loopback
- Python 3.14, one Uvicorn worker
- Everything on one machine, including the measuring client — so the client's
  own CPU use is competing with the controller's

A slower disk would move the heartbeat figure; a faster one would not move the
query figures, which are already dominated by planning.
