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

Usage::

    make db-up
    ./scripts/measure-metrics.py [--hosts 20] [--hours 6] [--interval 10]

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

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = REPO_ROOT / "apps" / "server"
sys.path.insert(0, str(SERVER_DIR))

# Local development credentials, matching deploy/compose.yaml. Not a secret.
SUPERUSER_URL = "postgresql://hcc:hcc_local_dev@127.0.0.1:5432/hcc"


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
        help="Collection interval in seconds. Default 10, the registry's default.",
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
    #
    # cpu and memory, which is what the Debian guide tells an operator to
    # enable first — so the shape measured is the shape most installations
    # will have.
    enabled = frozenset({"cpu", "memory"})
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
        # The real shape: cpu reports utilisation and a logical-cpu count,
        # memory reports four values. Nine samples per collection for the two
        # modules the guide enables.
        return [
            TelemetryEntry(
                module="cpu",
                schema_version=1,
                collected_at=at,
                duration_ms=2,
                samples=[
                    MetricSample(metric="utilisation", value=10.0 + (tick % 70)),
                    MetricSample(metric="logical_cpus", value=8),
                ],
            ),
            TelemetryEntry(
                module="memory",
                schema_version=1,
                collected_at=at,
                duration_ms=1,
                samples=[
                    MetricSample(metric="utilisation", value=40.0 + (tick % 30)),
                    MetricSample(metric="total", value=33_394_307_072),
                    MetricSample(metric="available", value=20_000_000_000 - tick),
                    MetricSample(metric="used", value=13_000_000_000 + tick),
                ],
            ),
        ]

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
            # Extrapolation, labelled. A hundred hosts collecting cpu and
            # memory every ten seconds is 9 samples per 10 s per host.
            daily = 100 * 9 * (86_400 / 10)
            result(
                "100 hosts, 7 days raw (extrapolated)",
                f"{daily * 7 * per_row / 1024**3:.1f} GiB",
            )
    finally:
        await connection.close()
    result("live partitions", str(partitions))

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

    await timed(
        "latest values for one host",
        lambda db: metrics.latest_for_host(db, probe_host, within=timedelta(hours=hours + 1)),
    )
    await timed("series list for one host", lambda db: metrics.series_for_host(db, probe_host))
    await timed("sample count for one host", lambda db: metrics.sample_count(db, probe_host))


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
