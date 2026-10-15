#!/usr/bin/env python
"""Measure what metric storage and querying actually cost.

Phase 3 §28. The roadmap and the architecture documents will want to say things
like "a hundred hosts collecting five modules" and "ninety days of history",
and this exists so that nothing claims a number without one behind it. Run it,
read the output, write down what it says — including where it falls short.

What it measures, against a **disposable database**:

1. **Ingestion throughput**, through ``MetricsService.ingest`` rather than raw
   ``COPY``: the four validation layers, the series cache and the partition
   router are the cost, and a ``COPY`` benchmark would measure PostgreSQL
   rather than this system.
2. **Storage per sample**, from ``pg_total_relation_size`` — including the
   indexes, because a row that costs 24 bytes and 60 bytes of index costs 84.
3. **Query latency** for the three reads a browser makes: current values, one
   series over a range at each tier, and the status summary.
4. **The query plans**, from ``EXPLAIN ANALYZE``, because latency on a small
   database says nothing about whether the planner is using the partition key
   — and a sequential scan that is fast on a day of data is a scan that stops
   being fast on ninety.
5. **Rollup throughput**: how long aggregating a day of raw samples takes, and
   how much the five-minute tier costs compared with the raw it replaces.

What it does **not** measure, and the output says so:

* A real fleet. The samples are written by one process in a loop, so this is
  the storage path's cost and not the socket's, the scheduler's, or a hundred
  agents' own.
* Concurrency. One writer, one reader. Contention between the maintenance job
  and live ingestion is not exercised here.
* Ninety days. The default run writes hours, extrapolated per sample — and an
  extrapolation is labelled as one.
* A cold cache. PostgreSQL will have everything in shared buffers; a real
  ninety-day query reads from disk.

Two profiles, because the fleet shape changed under this system and the old
one is no longer representative:

``--profile guide``
    ``cpu`` and ``memory``, nine samples a collection, **9 series a host**.
    What `development/ENABLING_MONITORING_DEBIAN.md` tells an operator to
    enable first, and what every figure before Operation Horizon was measured
    against.

``--profile full``
    All eleven modules at the label cardinality of a well-populated homelab
    host — three filesystems, four block devices, two interfaces, six
    temperature sensors, five containers, four watched units, eight cores.
    **164 series a host**, eighteen times the guide profile.

    Each module runs at **its own registry default interval**, not at one
    flat rate. That matters by a factor of six on `filesystem` alone, whose
    default is 60 s against `diskio`'s 10 s, and a benchmark that collected
    everything every ten seconds would overstate a full installation's
    storage several times over.

The difference is the point. Five of the six modules Operation Horizon added
are *label-keyed*, so a host's series count stopped being a property of the
module list and became a property of the machine: a NAS with twelve disks
and thirty containers produces several times what a laptop does. Everything
downstream of series count — the latest-values query, the series list, the
rollup's bucket count — scales with it, and nothing had measured that.

Usage::

    make db-up
    ./scripts/measure-metrics.py [--hosts 20] [--hours 6] [--interval 10]
    ./scripts/measure-metrics.py --profile full

Safety: creates its own database, drops it on every exit path, and never
connects to the developer's own.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import statistics
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = REPO_ROOT / "apps" / "server"
sys.path.insert(0, str(SERVER_DIR))

if TYPE_CHECKING:
    from hcc.agents.protocol import MetricSample, TelemetryEntry

# Local development credentials, matching deploy/compose.yaml. Not a secret.
SUPERUSER_URL = "postgresql://hcc:hcc_local_dev@127.0.0.1:5432/hcc"

#: Label values a reference host produces, per module and label key.
#:
#: Taken from the machine the demo runs on rather than invented: a laptop with
#: three real filesystems (btrfs subvolumes deduplicated), four block devices,
#: two reportable interfaces after the bridge rule, two hwmon chips with six
#: temperature sensors between them, five containers and four watched units.
#: That host produced 65 series, which is what `--profile full` reproduces.
#:
#: Written as *label values* rather than as counts so the series names in the
#: output are readable, and so a reader can see what "four devices" means on
#: a machine they might recognise.
_REFERENCE_LABELS: Final = {
    "cpu": {"core": [str(index) for index in range(8)]},
    "load": {"window": ["1m", "5m", "15m"]},
    "filesystem": {"mount": ["/", "/home", "/boot"]},
    "diskio": {"device": ["nvme0n1", "sda", "dm-0", "md0"]},
    "network": {"interface": ["br0", "wg0"]},
    # Keyed by sensor alone, with the chip in the name — which is what the
    # collector does, because two chips can both have a `temp1`.
    "sensors": {
        "sensor": [
            "coretemp package",
            "coretemp core0",
            "coretemp core1",
            "coretemp core2",
            "nvme composite",
            "nvme sensor1",
        ]
    },
    "docker": {"container": ["hcc", "postgres", "caddy", "jellyfin", "syncthing"]},
    "systemd": {"unit": ["sshd.service", "docker.service", "cron.service", "nginx.service"]},
}

#: What each profile collects.
#:
#: `guide` is pinned to the pair every figure before Operation Horizon was
#: measured against, so re-running it reproduces the old numbers and the
#: comparison in `docs/PERFORMANCE.md` §4 means something. `full` is read
#: from the registry rather than listed, so a module added later is measured
#: without anybody remembering to add it here.
PROFILES: Final = {
    "guide": ("cpu", "memory"),
    "full": None,
}


def _modules_for(profile: str) -> tuple[str, ...]:
    """The module ids a profile enables."""
    from hcc.monitoring import modules as registry

    listed = PROFILES[profile]
    return listed if listed is not None else tuple(module.id for module in registry.MODULE_LIST)


def _plausible(metric_id: str, unit: str, tick: int) -> float:
    """A value that is in range for its unit and changes between collections.

    In range because the ingestion path does not check ranges and a benchmark
    storing nonsense would still be storing rows of the right size — but the
    reliability scenario's "every value is within its unit's range" assertion
    runs against databases this script's shape informs, and a benchmark that
    could not satisfy it would be describing something other than the system.
    Changing because a constant column compresses differently.
    """
    if unit == "percent":
        return float(10 + (tick % 80))
    if unit == "celsius":
        return float(35 + (tick % 25))
    if unit == "bytes":
        return float(1_000_000_000 + tick * 4096)
    if unit in {"bytes_per_second", "operations_per_second", "packets_per_second"}:
        return float(tick % 5000)
    if unit == "bits_per_second":
        return 1_000_000_000.0
    if unit == "rpm":
        return float(800 + (tick % 1200))
    if unit == "seconds":
        return float(86_400 + tick)
    if unit == "count":
        # `readonly` and `up` are booleans in a count column, and `restarts`
        # is a small integer. One expression for all of them: a count that
        # stays small is true to every one.
        return float(tick % 3)
    return float(tick % 100)


def _entries_for(
    modules: tuple[str, ...], at: datetime, tick: int, interval: int
) -> list[TelemetryEntry]:
    """The modules due on this tick, built from the registry's declarations.

    Driven by the registry rather than hand-written on both axes:

    * **Which metrics and labels**, so the sample count is whatever the
      modules actually declare. A hand-written generator would measure the
      shape somebody remembered rather than the shape that ships.
    * **How often**, because each module has its own default interval and a
      real agent honours it. Collecting `filesystem` every ten seconds
      rather than every sixty overstates its storage sixfold, and the
      storage figure is the one that ends up in documentation.
    """
    from hcc.agents.protocol import MetricSample, TelemetryEntry
    from hcc.monitoring import modules as registry

    entries: list[TelemetryEntry] = []
    for module_id in modules:
        descriptor = registry.descriptor(module_id)
        # The slower of the tick and the module's own default, rounded to a
        # whole number of ticks so a 60 s module lands on every sixth tick
        # of a 10 s run rather than drifting.
        every = max(1, round(max(interval, descriptor.default_interval_seconds) / interval))
        if tick % every:
            continue
        samples: list[MetricSample] = []
        for metric in descriptor.metrics:
            labels = _REFERENCE_LABELS.get(module_id, {})
            missing = [key for key in metric.label_keys if key not in labels]
            if missing:
                # Loud, because the quiet version of this is what the first
                # run did: `load.average` is keyed by `window`, the table had
                # no entry for it, and every one of those samples was
                # *rejected* by the ingestion path — so the benchmark was
                # reporting throughput for a batch a third of which the
                # controller threw away.
                raise SystemExit(
                    f"_REFERENCE_LABELS has no values for {module_id}.{metric.id} "
                    f"label(s) {missing}. Add them: a module whose labels are "
                    f"missing has its samples refused, and the run would measure "
                    f"a batch the controller discards."
                )
            keys = list(metric.label_keys)
            if not keys:
                samples.append(
                    MetricSample(
                        metric=metric.id, value=_plausible(metric.id, metric.unit.value, tick)
                    )
                )
                continue
            # One sample per value of the *first* key, with the remaining
            # keys held at their first value. The registry's metrics are
            # keyed by one label each except `sensors`, which is keyed by
            # chip and sensor together — and enumerating that product would
            # claim twelve sensors on a host that has six.
            primary = keys[0]
            fixed = {key: labels[key][0] for key in keys[1:]}
            for index, value in enumerate(labels[primary]):
                samples.append(
                    MetricSample(
                        metric=metric.id,
                        labels={primary: value, **fixed},
                        value=_plausible(metric.id, metric.unit.value, tick + index),
                    )
                )
        entries.append(
            TelemetryEntry(
                module=module_id,
                schema_version=descriptor.data_schema_version,
                collected_at=at,
                duration_ms=2,
                samples=samples,
            )
        )
    return entries


def log(message: str) -> None:
    print(message, flush=True)


def heading(message: str) -> None:
    print(f"\n\033[0;36m{message}\033[0m", flush=True)


def result(label: str, value: str) -> None:
    print(f"  {label:<46} {value}", flush=True)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hosts",
        type=int,
        default=20,
        help="How many hosts to simulate. Default 20, a plausible homelab fleet.",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=6,
        help="How many hours of history to write per host. Default 6.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=10,
        help=(
            "The tick, in seconds. Each module collects at the slower of this "
            "and its own registry default, which is what a real agent does. "
            "Default 10."
        ),
    )
    parser.add_argument(
        "--profile",
        choices=sorted(PROFILES),
        default="guide",
        help=(
            "Which modules each host collects. 'guide' is cpu and memory, the "
            "pair the Debian guide enables and every pre-Horizon figure was "
            "measured against. 'full' is all eleven at a reference host's "
            "label cardinality. Default guide."
        ),
    )
    arguments = parser.parse_args()

    import asyncpg

    database = f"hcc_perf_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    url = SUPERUSER_URL.rsplit("/", 1)[0] + f"/{database}"

    admin = await asyncpg.connect(SUPERUSER_URL)
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
    finally:
        await admin.close()
    log(f"created {database}")

    try:
        return await run(url, arguments)
    finally:
        admin = await asyncpg.connect(SUPERUSER_URL)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
            log(f"dropped {database}")
        finally:
            await admin.close()


async def run(url: str, arguments: argparse.Namespace) -> int:
    import asyncpg

    from hcc.config import Settings
    from hcc.db import Database

    # Migrations, through alembic, so the schema is the one that ships.
    process = await asyncio.create_subprocess_exec(
        str(SERVER_DIR / ".venv" / "bin" / "alembic"),
        "upgrade",
        "head",
        cwd=str(SERVER_DIR),
        env={"HCC_DATABASE_URL": url, "PATH": "/usr/bin:/bin"},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    if process.returncode != 0:
        log(output.decode())
        return 1
    log("migrations applied")

    from hcc.logging import configure_logging

    configure_logging(level="ERROR", log_format="console")
    settings = Settings(environment="test", database_url=url, log_level="ERROR")
    database = Database(settings)
    database.connect()

    try:
        await measure_everything(database, url, arguments)
    finally:
        await database.disconnect()

    heading("== What this does not show ==")
    for line in (
        "One writer and one reader. Contention between the maintenance job and",
        "live ingestion is not measured.",
        "Samples written by one process in a loop, so this is the storage",
        "path's cost — not the socket's, the scheduler's, or a real fleet's.",
        "Everything is in PostgreSQL's shared buffers. A real ninety-day query",
        "reads from disk and will be slower.",
        "Totals for longer retention are extrapolated per sample and labelled",
        "as extrapolation. They are arithmetic, not measurements.",
    ):
        log(f"  {line}")
    return 0


async def measure_everything(
    database: object, url: str, arguments: argparse.Namespace
) -> None:
    import asyncpg

    from hcc.agents.protocol import MetricSample, TelemetryEntry
    from hcc.models.host import Host
    from hcc.services.metrics import MetricsService

    session_factory = database.session_factory  # type: ignore[attr-defined]

    host_count = arguments.hosts
    hours = arguments.hours
    interval = arguments.interval
    per_host_ticks = (hours * 3600) // interval

    heading("== Environment ==")
    connection = await asyncpg.connect(url)
    try:
        version = await connection.fetchval("SHOW server_version")
        result("PostgreSQL", str(version))
    finally:
        await connection.close()
    result("hosts simulated", str(host_count))
    result("history per host", f"{hours} h at every {interval} s")
    result("collections per host", str(per_host_ticks))

    # --- Hosts and the enabled modules ------------------------------------
    modules = _modules_for(arguments.profile)
    enabled = frozenset(modules)
    result("profile", f"{arguments.profile} ({len(modules)} modules)")
    host_ids: list[uuid.UUID] = []
    async with session_factory() as db:
        for index in range(host_count):
            host = Host(name=f"perf-{index:04d}")
            db.add(host)
            await db.flush()
            host_ids.append(host.id)
        await db.commit()

    metrics = MetricsService()
    now = datetime.now(UTC).replace(microsecond=0)
    start = now - timedelta(hours=hours)

    # Maintenance first, as a real deployment does: the job runs at startup
    # and every five minutes, so the partitions covering the backfill window
    # exist before any agent delivers into them.
    #
    # The first version of this script ingested first, so only the migration's
    # single partition existed and three quarters of the rows landed in
    # DEFAULT — which is how the missing backward windows were found. Keeping
    # the order wrong would have kept measuring that bug instead of the
    # system.
    from hcc.services.retention import RetentionService

    bootstrap = RetentionService()
    async with session_factory() as db:
        try:
            await bootstrap.run(db, now=now)
        finally:
            await bootstrap.release_lock(db)
            await db.commit()

    # --- Ingestion --------------------------------------------------------
    heading("== Ingestion, through the real validation path ==")

    def batch_for(host_index: int, tick: int) -> list[TelemetryEntry]:
        at = start + timedelta(seconds=tick * interval)
        return _entries_for(modules, at, tick, interval)

    # Per *hour*, not per collection: with each module on its own interval
    # the per-collection figure varies, and an hour is the unit every
    # extrapolation below multiplies. Counted by running a whole hour of
    # ticks rather than by arithmetic, so a module's interval rounding is
    # included rather than assumed away.
    ticks_per_hour = max(1, 3600 // interval)
    per_hour = sum(
        len(entry.samples) for tick in range(ticks_per_hour) for entry in batch_for(0, tick)
    )
    result("samples per host per hour", f"{per_hour:,}")

    samples_written = 0
    durations: list[float] = []
    began = time.perf_counter()
    async with session_factory() as db:
        for host_index, host_id in enumerate(host_ids):
            for tick in range(per_host_ticks):
                entry_start = time.perf_counter()
                outcome = await metrics.ingest(
                    db,
                    host_id=host_id,
                    entries=batch_for(host_index, tick),
                    enabled_modules=enabled,
                    now=now,
                )
                durations.append(time.perf_counter() - entry_start)
                samples_written += outcome.accepted
                if outcome.rejected:
                    log(f"  ! {outcome.rejected} rejected: {outcome.detail}")
            # One commit per host rather than per batch: a real agent's
            # batches arrive on separate requests, but committing 2,000 times
            # per host would measure fsync and nothing else.
            await db.commit()
    elapsed = time.perf_counter() - began

    result("samples stored", f"{samples_written:,}")
    result("wall clock", f"{elapsed:.2f} s")
    result("throughput", f"{samples_written / elapsed:,.0f} samples/s")
    result("per batch (median)", f"{statistics.median(durations) * 1000:.2f} ms")
    result(
        "per batch (p95)",
        f"{sorted(durations)[int(len(durations) * 0.95)] * 1000:.2f} ms",
    )

    # --- Storage ----------------------------------------------------------
    heading("== Storage, including indexes ==")
    # `pg_total_relation_size` on a *partitioned parent* returns zero: the
    # parent holds no rows, so it has no storage of its own. Summing the
    # partition tree is what gives a real number — the first version of this
    # script reported 0 bytes per sample, which is the kind of figure that ends
    # up in documentation.
    PARTITIONED_SIZE = (
        "SELECT coalesce(sum(pg_total_relation_size(relid)), 0) "
        "FROM pg_partition_tree($1)"
    )

    connection = await asyncpg.connect(url)
    try:
        raw_bytes = float(await connection.fetchval(PARTITIONED_SIZE, "metric_samples_raw"))
        series_rows = await connection.fetchval("SELECT count(*) FROM metric_series")
        raw_rows = await connection.fetchval("SELECT count(*) FROM metric_samples_raw")
        partitions = await connection.fetchval(
            "SELECT count(*) FROM metric_partitions WHERE dropped_at IS NULL"
        )
        result("series created", f"{series_rows:,}")
        result("raw rows", f"{raw_rows:,}")
        result("raw table + indexes", f"{raw_bytes / 1024 / 1024:.1f} MiB")
        if raw_rows:
            per_row = raw_bytes / raw_rows
            result("per sample", f"{per_row:.0f} bytes")
            # Extrapolation, labelled, and now derived from the profile
            # rather than from a hardcoded 9. The full profile is about
            # eight times the guide's, which is the figure that matters:
            # the old number was the only one in the documentation and it
            # described a fleet nobody will have after Operation Horizon.
            daily = 100 * per_hour * 24
            result(
                f"100 hosts, 7 days raw, {arguments.profile} (extrapolated)",
                f"{daily * 7 * per_row / 1024**3:.1f} GiB",
            )
            result(
                f"100 hosts, 90 days at the 1h tier, {arguments.profile} (extrapolated)",
                # The tier retention actually keeps for three months. Uses
                # the measured 1h-row cost further down where there is one;
                # here it is the raw cost, which overstates, and that is
                # said rather than quietly corrected.
                f"≤{100 * per_hour / 60 * 24 * 90 * per_row / 1024**3:.1f} GiB"
                " (raw per-row cost; the 1h row is wider but there are 60x fewer)",
            )
    finally:
        await connection.close()
    result("live partitions", str(partitions))

    # --- Series cardinality -----------------------------------------------
    #
    # The measurement Operation Horizon made necessary. Five of the six
    # modules it added are label-keyed, so a host's series count stopped
    # being a property of the module list and became a property of the
    # machine. Everything that iterates series — the latest-values query,
    # the series list, the rollup's bucket count — scales with this number,
    # and before this section nothing reported it.
    heading("== Series cardinality, which is now a property of the host ==")
    connection = await asyncpg.connect(url)
    try:
        per_host = await connection.fetch(
            "SELECT module, count(*) AS series FROM metric_series "
            "WHERE host_id = $1 GROUP BY module ORDER BY count(*) DESC, module",
            host_ids[0],
        )
        for row in per_host:
            result(f"  series for {row['module']}", str(row["series"]))
        per_host_series = sum(row["series"] for row in per_host)
        result("series per host", str(per_host_series))
        # The number a fleet multiplies. Quoted because the rollup job walks
        # every series in the installation on every pass.
        result("series in the installation", f"{series_rows:,}")
    finally:
        await connection.close()

    # --- Query latency ----------------------------------------------------
    heading("== Query latency, warm cache ==")
    probe_host = host_ids[0]

    async def timed(label: str, call: object, runs: int = 10) -> None:
        samples: list[float] = []
        async with session_factory() as db:
            for _ in range(runs):
                began_at = time.perf_counter()
                await call(db)  # type: ignore[operator]
                samples.append(time.perf_counter() - began_at)
        result(label, f"median {statistics.median(samples) * 1000:.1f} ms")

    # These three are the ones that scale with series count rather than with
    # sample count, which is why the profile matters to them and not to the
    # range queries below.
    await timed(
        "latest values for one host",
        lambda db: metrics.latest_for_host(db, probe_host, within=timedelta(hours=hours + 1)),
    )
    await timed("series list for one host", lambda db: metrics.series_for_host(db, probe_host))
    await timed("sample count for one host", lambda db: metrics.sample_count(db, probe_host))
    await timed("module health for one host", lambda db: metrics.health_for_host(db, probe_host))


    async with session_factory() as db:
        series = await metrics.series_for_host(db, probe_host)
    cpu_series = next(
        entry for entry in series if entry.module == "cpu" and entry.metric == "utilisation"
    )

    for label, window in (
        ("range, 1 hour", timedelta(hours=1)),
        ("range, 24 hours", timedelta(hours=24)),
        ("range, 30 days", timedelta(days=30)),
    ):
        await timed(
            label,
            lambda db, window=window: metrics.range_for_series(  # type: ignore[misc]
                db,
                series_id=cpu_series.id,
                host_id=probe_host,
                start=now - window,
                end=now,
                max_points=1000,
            ),
        )

    # --- Query plans ------------------------------------------------------
    #
    # Latency on a small database says nothing about scale. The plan does: a
    # sequential scan that is fast on six hours is a scan that is not fast on
    # ninety days, and partition pruning is the property that decides which
    # one a query gets.
    heading("== Query plans ==")
    connection = await asyncpg.connect(url)
    try:
        plan = await connection.fetch(
            "EXPLAIN (ANALYZE, BUFFERS) SELECT recorded_at, value "
            "FROM metric_samples_raw WHERE series_id = $1 "
            "AND recorded_at >= $2 AND recorded_at < $3 ORDER BY recorded_at",
            cpu_series.id,
            now - timedelta(hours=1),
            now,
        )
        text = "\n".join(row[0] for row in plan)
        uses_index = "Index Scan" in text or "Bitmap" in text
        result("range query uses an index", "yes" if uses_index else "NO — sequential scan")
        # Distinct partition names, not occurrences: the plan names each one
        # twice (the scan node and its index condition), so counting
        # occurrences doubled the figure. Pruning is the property being
        # measured, and over-reporting it would hide a planner that stopped
        # pruning.
        touched = sorted(
            {
                word.strip(",()")
                for word in text.replace("\n", " ").split()
                if word.strip(",()").startswith("metric_samples_raw_")
                and not word.strip(",()").endswith("_pkey")
            }
        )
        result("partitions scanned", f"{len(touched)} ({', '.join(touched) or 'none named'})")
        total_partitions = await connection.fetchval(
            "SELECT count(*) - 1 FROM pg_partition_tree('metric_samples_raw')"
        )
        result("partitions that exist", str(total_partitions))

        # How much landed in the DEFAULT partition. A non-zero number here is
        # the maintenance job not having run ahead of the data — which is what
        # a benchmark that writes history *backwards* from now produces, and
        # is worth reporting because every query over that range then reads a
        # partition holding every host's data.
        in_default = await connection.fetchval(
            "SELECT count(*) FROM metric_samples_raw "
            "WHERE tableoid::regclass::text LIKE '%_default'"
        )
        result(
            "rows in the DEFAULT partition",
            f"{in_default:,} of {raw_rows:,}"
            + ("" if in_default == 0 else "  <- no partition covered their timestamps"),
        )

        count_plan = "\n".join(
            row[0]
            for row in await connection.fetch(
                "EXPLAIN (ANALYZE) SELECT count(*) FROM metric_samples_raw "
                "WHERE series_id = ANY($1)",
                [cpu_series.id],
            )
        )
        result(
            "sample count uses an index",
            "yes" if "Index" in count_plan or "Bitmap" in count_plan else "NO",
        )
        # Execution is fast and *planning* is not: `series_id` is not the
        # partition key, so the planner considers every partition on every
        # call. Reported separately because the two scale differently —
        # execution with the host's own data, planning with how many
        # partitions the retention policy keeps — and only the second explains
        # a wall-clock figure that looks wrong next to "actual time".
        for key in ("Planning Time", "Execution Time"):
            line = next(
                (l.strip() for l in count_plan.splitlines() if l.strip().startswith(key)),
                "",
            )
            if line:
                result(f"  count {key.lower()}", line.split(":", 1)[1].strip())
        log(
            "    note: the SQL above costs about a millisecond, and the service\n"
            "    method measured in the latency section costs roughly ten times\n"
            "    that on a database with this many partitions. The gap is not the\n"
            "    ORM — in_() and ANY() were measured as identical — and is not\n"
            "    explained here. It grows with partition count, so it is a\n"
            "    characteristic to watch rather than a number to rely on."
        )
        for line in count_plan.splitlines()[:3]:
            log(f"    {line}")
        for line in text.splitlines()[:6]:
            log(f"    {line}")
    finally:
        await connection.close()

    # --- Rollups ----------------------------------------------------------
    heading("== Rollups ==")
    from hcc.services.retention import RetentionPolicy, RetentionService

    service = RetentionService(RetentionPolicy(lateness_minutes=0))
    began = time.perf_counter()
    async with session_factory() as db:
        try:
            outcome = await service.run(db, now=now + timedelta(minutes=10))
        finally:
            await service.release_lock(db)
            await db.commit()
    rollup_elapsed = time.perf_counter() - began

    from hcc.services.retention import MAX_BUCKETS_PER_RUN

    result("maintenance pass", f"{rollup_elapsed:.2f} s")
    result("buckets written", str(outcome.buckets_rolled_up))
    # A run that hit the cap did not finish, and reporting its duration as
    # "the rollup cost" would understate what catching up actually takes.
    capped = [
        tier
        for tier, count in (outcome.buckets_rolled_up or {}).items()
        if count >= MAX_BUCKETS_PER_RUN
    ]
    if capped:
        result(
            "hit the per-run bound",
            f"{', '.join(capped)} at {MAX_BUCKETS_PER_RUN:,} — more runs needed to catch up",
        )
    else:
        result("hit the per-run bound", f"no (bound is {MAX_BUCKETS_PER_RUN:,})")

    # The ceiling the bound implies, which is the number this whole section
    # exists to produce.
    #
    # The five-minute tier writes one row per series per bucket, the
    # maintenance job runs every `metrics_maintenance_interval_seconds` (300
    # by default) and a bucket is five minutes wide — so a steady-state pass
    # has exactly one bucket per series to write. The bound is therefore a
    # bound on **series in the installation**, not on hosts, and with
    # label-keyed modules those are very different numbers.
    #
    # Over the ceiling the job never catches up: each pass writes 5,000 of
    # the N buckets that accrued and falls further behind every five
    # minutes. That is a cliff rather than a slope, and it is why this is
    # computed and printed rather than left as arithmetic in a document.
    from hcc.config import Settings

    maintenance_seconds = Settings.model_fields[
        "metrics_maintenance_interval_seconds"
    ].default
    buckets_per_pass = max(1, round(maintenance_seconds / 300))
    ceiling = MAX_BUCKETS_PER_RUN // buckets_per_pass
    result("series the 5m tier can sustain", f"{ceiling:,}")
    if per_host_series:
        result(
            f"  which is, at {per_host_series} series a host",
            f"{ceiling // per_host_series:,} hosts on the {arguments.profile} profile",
        )
    if series_rows > ceiling:
        result(
            "  this installation",
            f"{series_rows:,} series — OVER the ceiling; the rollup job would never catch up",
        )

    connection = await asyncpg.connect(url)
    try:
        five_bytes = float(await connection.fetchval(PARTITIONED_SIZE, "metric_samples_5m"))
        five_rows = await connection.fetchval("SELECT count(*) FROM metric_samples_5m")
        result("5m rows", f"{five_rows:,}")
        result("5m table + indexes", f"{five_bytes / 1024 / 1024:.1f} MiB")
        if five_rows and raw_rows and five_bytes:
            # The compression ratio is what makes ninety days affordable, and
            # it is a measurement here rather than the 30x the interval
            # arithmetic would predict — the rollup row is wider.
            result("rows per rollup row", f"{raw_rows / five_rows:.1f}")
            result(
                "bytes per rollup row",
                f"{five_bytes / five_rows:.0f} (raw: {raw_bytes / raw_rows:.0f})",
            )
            result(
                "storage ratio raw:5m",
                f"{raw_bytes / five_bytes:.1f}:1",
            )
    finally:
        await connection.close()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        raise SystemExit(asyncio.run(main()))
