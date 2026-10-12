#!/usr/bin/env python
"""Measure what the agent connection path actually costs.

Milestone 1.8 §22: the roadmap's "1,000 agents" figure is a target, and this
exists so that nothing in the documentation claims it without a number behind
it. Run it, read the output, write down what it says — including where it
falls short.

What it measures, against a **disposable database** and a **real controller
process**:

1. The host-list query plan at fleet scale, from ``EXPLAIN ANALYZE``. This is
   the query every browser poll runs, so its plan is the one that decides
   whether the console stays usable as the fleet grows.
2. Heartbeat write latency. The most frequent write in the system: a thousand
   hosts at thirty seconds is 2.88 million a day.
3. Real concurrent WebSocket connections, with the controller's resident
   memory before and after — so "cost per connection" is a measurement rather
   than an estimate.

What it does **not** measure, and the output says so:

* A thousand *processes*. The connections here are opened by one Python
  client, so this measures the controller's cost per connection and not the
  network's, the kernel's at a thousand distinct peers, or a thousand real
  agents' own overhead.
* Anything over a real network. Everything is loopback.
* Sustained load over hours. This is minutes.

Usage::

    make db-up
    ./scripts/measure-agents.py [--hosts 1000] [--connections 200]

Safety: creates its own database, drops it at the end, and refuses to touch
one that already exists. It never connects to the developer's database.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import resource
import subprocess
import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = REPO_ROOT / "apps" / "server"
VENV_PYTHON = SERVER_DIR / ".venv" / "bin" / "python"

# Local development credentials, matching deploy/compose.yaml. Not a secret.
SUPERUSER_URL = "postgresql://hcc:hcc_local_dev@127.0.0.1:5432/hcc"
PORT = 8207


def log(message: str) -> None:
    print(message, flush=True)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hosts",
        type=int,
        default=1000,
        help="how many host rows to seed (default: the roadmap's target fleet)",
    )
    parser.add_argument(
        "--connections",
        type=int,
        default=200,
        help="how many concurrent WebSocket connections to open",
    )
    arguments = parser.parse_args()

    # Imported from the server's venv, which is where asyncpg and websockets
    # live. Running under that interpreter is the documented invocation.
    try:
        import asyncpg
        import websockets
    except ModuleNotFoundError:
        log(
            "Run this with the server's interpreter:\n"
            f"  {VENV_PYTHON} {Path(__file__).relative_to(REPO_ROOT)}"
        )
        return 2

    database = f"hcc_perf_{int(time.time())}_{os.getpid()}"
    url = f"postgresql://hcc:hcc_local_dev@127.0.0.1:5432/{database}"

    admin = await asyncpg.connect(SUPERUSER_URL)
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
    finally:
        await admin.close()
    log(f"created {database}")

    server: subprocess.Popen[bytes] | None = None
    try:
        # In a thread, because `subprocess.run` blocks the event loop and
        # this script's own linter is right to say so — even in a one-shot
        # measurement, a blocked loop is how a measurement ends up measuring
        # the measurer.
        await asyncio.to_thread(
            subprocess.run,
            [str(VENV_PYTHON), "-m", "alembic", "upgrade", "head"],
            cwd=SERVER_DIR,
            env={**os.environ, "HCC_DATABASE_URL": url},
            check=True,
            capture_output=True,
        )
        log("migrations applied")

        credentials = await seed(url, arguments.hosts)
        log(f"seeded {arguments.hosts} hosts with live credentials")

        await measure_list_query(url, arguments.hosts)
        await measure_heartbeat_write(url, credentials[0][0])

        server = start_controller(url)
        await wait_for_controller()
        await measure_connections(
            websockets, credentials[: arguments.connections], server.pid
        )
        return 0
    finally:
        if server is not None:
            server.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                server.wait(timeout=10)
        admin = await asyncpg.connect(SUPERUSER_URL)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        finally:
            await admin.close()
        log(f"dropped {database}")


async def seed(url: str, count: int) -> list[tuple[str, str]]:
    """Insert `count` enrolled hosts and return their `(host_id, credential)`.

    Inserted directly rather than through the enrollment endpoint: a thousand
    real enrollments would measure the enrollment path, which is a
    once-per-host operation and not what is under test here.
    """
    import asyncpg

    sys.path.insert(0, str(SERVER_DIR))
    from hcc.security.tokens import generate_token, hash_token

    connection = await asyncpg.connect(url)
    try:
        rows: list[tuple[str, str]] = []
        hosts: list[tuple[uuid.UUID, str, str]] = []
        creds: list[tuple[uuid.UUID, uuid.UUID, str]] = []
        for index in range(count):
            host_id = uuid.uuid4()
            name = f"perf-host-{index:05d}"
            credential = generate_token()
            hosts.append((host_id, name, name))
            creds.append((uuid.uuid4(), host_id, hash_token(credential)))
            rows.append((str(host_id), credential))

        await connection.executemany(
            "INSERT INTO hosts (id, name, name_canonical, lifecycle, is_enabled) "
            "VALUES ($1, $2, $3, 'enrolled', true)",
            hosts,
        )
        await connection.executemany(
            "INSERT INTO agent_credentials (id, host_id, token_hash) VALUES ($1, $2, $3)",
            creds,
        )
        await connection.execute("ANALYZE hosts, agent_credentials")
        return rows
    finally:
        await connection.close()


async def measure_list_query(url: str, host_count: int) -> None:
    """Report the plan for the query every browser poll runs."""
    import asyncpg

    connection = await asyncpg.connect(url)
    try:
        # The real shape: the page, with the two correlated subqueries the
        # list endpoint uses.
        plan = await connection.fetchval(
            """
            EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
            SELECT hosts.*,
                   (SELECT count(*) FROM host_enrollments
                     WHERE host_enrollments.host_id = hosts.id
                       AND consumed_at IS NULL AND revoked_at IS NULL
                       AND expires_at > now()),
                   (SELECT max(created_at) FROM agent_credentials
                     WHERE agent_credentials.host_id = hosts.id
                       AND revoked_at IS NULL)
            FROM hosts
            ORDER BY name_canonical ASC, id ASC
            LIMIT 25 OFFSET 0
            """
        )
        parsed = json.loads(plan)[0]
        log("")
        log(f"== Host list, page 1 of {host_count} hosts ==")
        log(f"  execution time : {parsed['Execution Time']:.2f} ms")
        log(f"  planning time  : {parsed['Planning Time']:.2f} ms")
        log(f"  top node       : {parsed['Plan']['Node Type']}")
        log(f"  rows returned  : {parsed['Plan']['Actual Rows']}")

        # And the deepest page, which is where offset pagination degrades.
        deep = await connection.fetchval(
            """
            EXPLAIN (ANALYZE, FORMAT JSON)
            SELECT id FROM hosts ORDER BY name_canonical ASC, id ASC
            LIMIT 25 OFFSET $1
            """,
            max(0, host_count - 25),
        )
        deep_parsed = json.loads(deep)[0]
        log(f"  last page      : {deep_parsed['Execution Time']:.2f} ms")

        count = await connection.fetchval(
            "EXPLAIN (ANALYZE, FORMAT JSON) SELECT count(*) FROM hosts"
        )
        log(f"  total count    : {json.loads(count)[0]['Execution Time']:.2f} ms")
    finally:
        await connection.close()


async def measure_heartbeat_write(url: str, host_id: str) -> None:
    """Time the single most frequent write in the system."""
    import asyncpg

    connection = await asyncpg.connect(url)
    try:
        # Arrange the connection state a heartbeat writes against.
        connection_id = uuid.uuid4()
        await connection.execute(
            "UPDATE hosts SET current_connection_id = $1, connection_epoch = 1, "
            "last_heartbeat_at = now() WHERE id = $2",
            connection_id,
            uuid.UUID(host_id),
        )

        statement = (
            "UPDATE hosts SET last_heartbeat_at = now() "
            "WHERE id = $1 AND current_connection_id = $2 AND connection_epoch = 1"
        )
        iterations = 2000
        started = time.perf_counter()
        for _ in range(iterations):
            await connection.execute(statement, uuid.UUID(host_id), connection_id)
        elapsed = time.perf_counter() - started

        per_write_ms = elapsed / iterations * 1000
        log("")
        log("== Heartbeat write ==")
        log(f"  {iterations} guarded UPDATEs in {elapsed:.2f} s")
        log(f"  per write      : {per_write_ms:.3f} ms")
        # The figure that matters: a thousand hosts at the 30-second interval.
        daily = 1000 * (86400 / 30)
        log(
            f"  1,000 hosts/30s: {daily:,.0f} writes/day, "
            f"{daily * per_write_ms / 1000 / 86400 * 100:.2f}% of one core-second per second"
        )
    finally:
        await connection.close()


def start_controller(url: str) -> subprocess.Popen[bytes]:
    """Start a real controller process on a dedicated port."""
    return subprocess.Popen(
        [str(VENV_PYTHON), "-m", "hcc.main"],
        cwd=SERVER_DIR,
        env={
            **os.environ,
            "HCC_DATABASE_URL": url,
            "HCC_HOST": "127.0.0.1",
            "HCC_PORT": str(PORT),
            "HCC_ENVIRONMENT": "development",
            "HCC_LOG_LEVEL": "WARNING",
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def wait_for_controller() -> None:
    """Poll the health endpoint until the controller answers.

    The probe runs in a thread for the same reason the migration does: a
    blocking read on the event loop would stall every other coroutine, and
    the point of this script is to measure the controller rather than its
    client.
    """
    for _ in range(100):
        if await asyncio.to_thread(_healthy):
            return
        await asyncio.sleep(0.2)
    raise RuntimeError("the controller did not come up")


def _healthy() -> bool:
    """Whether the controller is answering. A fixed loopback URL."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{PORT}/healthz", timeout=1
        ) as response:
            return bool(response.status == 200)
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def resident_mib(pid: int) -> float:
    """The process's resident set size, in MiB, from /proc."""
    with Path(f"/proc/{pid}/statm").open() as handle:
        pages = int(handle.read().split()[1])
    return pages * resource.getpagesize() / 1024 / 1024


async def measure_connections(
    websockets_module: object, credentials: list[tuple[str, str]], pid: int
) -> None:
    """Open real WebSocket connections and report what they cost."""
    connect = websockets_module.connect  # type: ignore[attr-defined]

    baseline = resident_mib(pid)
    log("")
    log(f"== {len(credentials)} concurrent agent connections ==")
    log(f"  controller RSS before : {baseline:.1f} MiB")

    opened: list[object] = []
    started = time.perf_counter()
    try:
        for _, credential in credentials:
            socket = await connect(
                f"ws://127.0.0.1:{PORT}/api/v1/agents/connect",
                additional_headers={
                    "Authorization": f"Bearer {credential}",
                    "X-HCC-Protocol-Version": "1",
                },
            )
            opened.append(socket)
        connect_elapsed = time.perf_counter() - started

        after_connect = resident_mib(pid)
        log(
            f"  all accepted in       : {connect_elapsed:.2f} s "
            f"({connect_elapsed / len(credentials) * 1000:.1f} ms each)"
        )
        log(f"  controller RSS after  : {after_connect:.1f} MiB")
        log(
            f"  per connection        : "
            f"{(after_connect - baseline) / len(credentials) * 1024:.0f} KiB"
        )

        # One heartbeat round trip on every connection, serially, which is the
        # honest way to measure the controller's per-heartbeat cost without
        # the client becoming the bottleneck.
        frame = json.dumps({"v": 1, "type": "heartbeat", "platform": "linux/amd64"})
        started = time.perf_counter()
        for socket in opened:
            await socket.send(frame)  # type: ignore[attr-defined]
            await socket.recv()  # type: ignore[attr-defined]
        beat_elapsed = time.perf_counter() - started

        log(
            f"  heartbeat round trip  : "
            f"{beat_elapsed / len(opened) * 1000:.2f} ms each "
            f"({len(opened) / beat_elapsed:.0f}/s serially)"
        )
        log(f"  controller RSS at rest: {resident_mib(pid):.1f} MiB")
    finally:
        for socket in opened:
            with contextlib.suppress(Exception):
                await socket.close()  # type: ignore[attr-defined]

    log("")
    log("Not measured: a thousand distinct peers, a real network, or sustained")
    log("load over hours. These figures are one client on loopback, for minutes.")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
