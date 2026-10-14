#!/usr/bin/env python
"""Measure how many bytes the control plane sends to a browser.

Phase 10 §I. Serving the web client from the control plane is new — before it,
Vite served the client in development and the production deployment served no
client at all — so nothing had ever counted the bytes. This counts them, and it
is where the compression decisions in ``hcc/web.py`` and ``hcc/compression.py``
get their numbers.

What it measures:

1. **Every built asset**, with and without ``Accept-Encoding: gzip``, plus the
   subset a browser fetches for a **first load** of the sign-in page. That
   subset is the number that matters: it is the one transfer every user of the
   installation makes before seeing anything.
2. **Metric payloads** at the point budget ``apps/web`` actually requests, and
   at the server's own cap. This is the *recurring* cost — a bundle is cached
   for a year and fetched once per release, while a host page re-fetches its
   charts every time somebody opens it.

It reads the real responses from the real application, assembled by
``create_app`` and ``mount_web_client``, so the figures include the middleware
as shipped.

## Why the byte counts come from ASGI rather than from an HTTP client

An HTTP client cannot see them. httpx decompresses transparently, and Starlette
omits ``Content-Length`` when it streams a compressed response — so
``len(response.content)`` is the *decoded* size for exactly the large responses
whose compression matters most. Measured through httpx, the two biggest chunks
in this bundle appeared to compress to precisely their original size. The
``http.response.body`` events are the only place the real figure appears.

What it does **not** measure, and the output says so:

* Time. These are byte counts. A browser's load time depends on the link, the
  round trips, and the parse — none of which this touches.
* TLS or HTTP/2 framing. The figures are response bodies, not packets.
* Caching. Every request here is a cold one. In practice ``/assets/*`` is
  immutable for a year, so a returning browser fetches none of it.
* The agent protocol. This is the browser's side only.

Usage::

    cd apps/web && npm run build
    ./scripts/measure-transfer.py

Safety: opens no network listener, reads no database — the ``Settings`` object
below names a database that is never connected to, because no lifespan runs.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import gzip
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = REPO_ROOT / "apps" / "server"
DIST = REPO_ROOT / "apps" / "web" / "dist"
sys.path.insert(0, str(SERVER_DIR))

#: Never connected to: no lifespan runs, so nothing opens a connection. The
#: field is required, so a value has to exist.
UNUSED_DATABASE_URL = "postgresql://hcc:unused@127.0.0.1:5432/hcc"

#: What apps/web/src/routes/hosts/metric-history.tsx requests per chart.
CLIENT_POINT_BUDGET = 600

#: Charts on a host page, from the overview and host views.
CHARTS_PER_PAGE = 4

GREEN = "\033[0;32m"
CYAN = "\033[0;36m"
DIM = "\033[2m"
OFF = "\033[0m"


def heading(message: str) -> None:
    print(f"\n{CYAN}{message}{OFF}")


def note(message: str) -> None:
    print(f"{DIM}{message}{OFF}")


async def wire_bytes(app: object, path: str, *, accept_encoding: str | None) -> tuple[int, str]:
    """Body bytes *app* sends for *path*, and the content encoding it used."""
    headers = [] if accept_encoding is None else [(b"accept-encoding", accept_encoding.encode())]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    sent = 0
    encoding = "identity"
    delivered = False

    async def receive() -> dict[str, object]:
        # The request once, then an await that never completes.
        #
        # `FileResponse` runs a disconnect listener alongside the send, and it
        # is a `while True: await receive()` loop. A stub returning a message
        # every time never yields to the event loop, so that loop starves the
        # task sending the response and the request hangs instead of failing.
        # The task group cancels this when the response completes.
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()
        raise AssertionError("unreachable: the task group cancels this")

    async def send(message: dict[str, object]) -> None:
        nonlocal sent, encoding
        if message["type"] == "http.response.start":
            for key, value in message["headers"]:  # type: ignore[union-attr]
                if key.lower() == b"content-encoding":
                    encoding = value.decode()
        elif message["type"] == "http.response.body":
            sent += len(message.get("body", b""))  # type: ignore[arg-type]

    await app(scope, receive, send)  # type: ignore[operator]
    return sent, encoding


def row(label: str, plain: int, compressed: int, encoding: str) -> None:
    ratio = f"{plain / compressed:.2f}x" if compressed else "--"
    shown = label if len(label) <= 38 else "…" + label[-37:]
    print(f"  {shown:<38} {plain:>10,} {compressed:>10,} {encoding:>9} {ratio:>7}")


def header() -> None:
    print(f"  {'':<38} {'verbatim':>10} {'gzip':>10} {'encoding':>9} {'saved':>7}")


async def measure_assets() -> None:
    from hcc.app import create_app
    from hcc.config import Settings
    from hcc.web import mount_web_client

    app = create_app(Settings(database_url=UNUSED_DATABASE_URL, environment="test"))
    mount_web_client(app, DIST, api_prefix="/api")

    assets = sorted(p for p in (DIST / "assets").glob("*") if p.suffix in {".js", ".css"})
    entry = next((p for p in assets if p.name.startswith("index-") and p.suffix == ".js"), None)
    stylesheet = next((p for p in assets if p.suffix == ".css"), None)
    if entry is None or stylesheet is None:
        print("  the build has no entry chunk or no stylesheet; is apps/web/dist current?")
        return

    heading("== A first load: what a browser fetches before it shows anything ==")
    note("  index.html, the stylesheet and the entry chunk. The chart library is")
    note("  lazy-loaded, so it is not in this set.")
    header()
    first_load = ["/", f"/assets/{stylesheet.name}", f"/assets/{entry.name}"]
    fl_plain = fl_gzip = 0
    for path in first_load:
        plain, _ = await wire_bytes(app, path, accept_encoding=None)
        gz, encoding = await wire_bytes(app, path, accept_encoding="gzip, deflate, br")
        row(path, plain, gz, encoding)
        fl_plain += plain
        fl_gzip += gz
    print(f"  {'-' * 76}")
    row("FIRST LOAD", fl_plain, fl_gzip, "")

    heading("== Every asset, cold ==")
    note("  What a browser fetches across a whole session with an empty cache.")
    header()
    total_plain = total_gzip = 0
    for asset in assets:
        path = f"/assets/{asset.name}"
        plain, _ = await wire_bytes(app, path, accept_encoding=None)
        gz, encoding = await wire_bytes(app, path, accept_encoding="gzip, deflate, br")
        row(path, plain, gz, encoding)
        total_plain += plain
        total_gzip += gz
    print(f"  {'-' * 76}")
    row("TOTAL", total_plain, total_gzip, "")


def metric_payload(points: int, *, rollup: bool) -> bytes:
    """One range response, serialised exactly as the endpoint would serialise it."""
    from hcc.api.v1.metrics import RangePointResponse, RangeResponse, SeriesResponse

    now = dt.datetime(2026, 10, 14, tzinfo=dt.UTC)
    step = dt.timedelta(minutes=5) if rollup else dt.timedelta(seconds=1)
    body = RangeResponse(
        series=SeriesResponse(
            series_id=1,
            module="cpu",
            metric="utilisation",
            labels={},
            unit="percent",
            kind="gauge",
            module_name="Processor",
            metric_name="Total usage",
            last_sample_at=now.isoformat(),
        ),
        resolution="5m" if rollup else "raw",
        start=now.isoformat(),
        end=(now + step * points).isoformat(),
        points=[
            RangePointResponse(
                at=(now + step * n).isoformat(),
                value=round(17.0 + (n % 97) * 0.37, 4),
                minimum=round(12.0 + (n % 53) * 0.21, 4) if rollup else None,
                maximum=round(41.0 + (n % 71) * 0.19, 4) if rollup else None,
                sample_count=12 if rollup else None,
            )
            for n in range(points)
        ],
        truncated=False,
    )
    return body.model_dump_json().encode()


def measure_payloads() -> None:
    from hcc.compression import COMPRESSION_LEVEL
    from hcc.services.metrics import MAX_QUERY_POINTS

    heading("== Metric payloads: the recurring cost ==")
    note("  Serialised by the real response models. Compression applied at the")
    note("  level hcc/compression.py uses, so these are the shipped figures.")
    header()
    scenarios = [
        (f"one chart, {CLIENT_POINT_BUDGET} points, rollup", CLIENT_POINT_BUDGET, True),
        (f"one chart, {CLIENT_POINT_BUDGET} points, raw", CLIENT_POINT_BUDGET, False),
        (
            f"a host page, {CHARTS_PER_PAGE} charts",
            CLIENT_POINT_BUDGET * CHARTS_PER_PAGE,
            True,
        ),
        (f"the server's cap, {MAX_QUERY_POINTS:,} points", MAX_QUERY_POINTS, True),
    ]
    for label, points, rollup in scenarios:
        raw = metric_payload(points, rollup=rollup)
        row(label, len(raw), len(gzip.compress(raw, COMPRESSION_LEVEL)), "gzip")
    note("")
    note("  An array of ISO-8601 timestamps a fixed interval apart is about the")
    note("  most repetitive thing a JSON encoder can emit, which is why these")
    note("  compress several times better than the JavaScript does.")


def what_this_does_not_show() -> None:
    heading("== What this does not show ==")
    for line in [
        "Time. These are byte counts. Load time depends on the link, the",
        "  round trips and the parse.",
        "TLS or HTTP/2 framing. Response bodies, not packets.",
        "Caching. Every request here is cold; /assets/* is immutable for a",
        "  year, so a returning browser fetches none of it.",
        "The agent protocol. This is the browser's side only.",
    ]:
        print(f"  {line}" if line.startswith("  ") else f"  * {line}")


async def main() -> int:
    if not (DIST / "index.html").is_file():
        print(f"{DIST} has no index.html.")
        print("Build the client first:  cd apps/web && npm run build")
        return 1
    await measure_assets()
    measure_payloads()
    what_this_does_not_show()
    print(f"\n{GREEN}Measured.{OFF} Numbers above, method in this file's docstring.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
