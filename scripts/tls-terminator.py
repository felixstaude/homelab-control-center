#!/usr/bin/env python3
"""A minimal TLS-terminating TCP proxy, for tests that need real HTTPS.

The control plane speaks plain HTTP and expects a TLS-terminating reverse proxy
in front of it (``docs/SECURITY.md``). Several things are therefore impossible
to test without one:

* The agent **refuses** plain HTTP to anything but loopback, so an agent in a
  container cannot reach a controller on the host's bridge address at all.
* ``--ca-file`` and the whole internal-CA path — the single most common real
  enrolment failure — have no code path without a certificate to verify.
* WebSocket-over-TLS is a different code path from WebSocket-over-HTTP in
  every client library, including the agent's.

Nginx or Caddy would do this. Neither is on a CI runner by default, and adding
a package installation to a test script is a dependency on somebody's mirror.
This is forty lines of standard library that does the one thing needed: accept
TLS, forward bytes.

A **byte** proxy, not an HTTP one, deliberately. It knows nothing about
requests, so WebSocket upgrades, long-lived connections and chunked bodies all
work without special cases — and it cannot corrupt a header it does not parse.

Usage:
    tls-terminator.py --listen 172.17.0.1:8443 \\
        --target 127.0.0.1:8000 \\
        --certificate server.pem --key server.key

Not for production. It does no client authentication, no rate limiting and no
logging, and it is in `scripts/` rather than in the application for that
reason.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import ssl
import sys


async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Copy one direction until it closes."""
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError, ssl.SSLError):
        # An abrupt close is ordinary here: an agent being killed, a test
        # ending, a controller restarting. Nothing to report.
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


def build_handler(target_host: str, target_port: int):
    async def handle(
        client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                target_host, target_port
            )
        except OSError as error:
            # The controller is not up, or has gone away. Close the client
            # rather than hanging: an agent gets a connection reset and
            # retries on its own backoff, which is the behaviour under test in
            # the reliability scenarios.
            print(f"tls-terminator: cannot reach {target_host}:{target_port}: {error}",
                  file=sys.stderr, flush=True)
            with contextlib.suppress(Exception):
                client_writer.close()
            return

        await asyncio.gather(
            relay(client_reader, upstream_writer),
            relay(upstream_reader, client_writer),
        )

    return handle


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", required=True, help="address:port to accept TLS on")
    parser.add_argument("--target", required=True, help="address:port to forward to")
    parser.add_argument("--certificate", required=True, help="PEM certificate chain")
    parser.add_argument("--key", required=True, help="PEM private key")
    arguments = parser.parse_args()

    listen_host, _, listen_port = arguments.listen.rpartition(":")
    target_host, _, target_port = arguments.target.rpartition(":")
    if not listen_host or not target_host:
        parser.error("--listen and --target must be address:port")

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    # The same floor the agent enforces on its side, stated rather than left
    # to the default so a test cannot accidentally prove something about TLS
    # 1.1.
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(arguments.certificate, arguments.key)

    server = await asyncio.start_server(
        build_handler(target_host, int(target_port)),
        host=listen_host,
        port=int(listen_port),
        ssl=context,
    )
    for socket_ in server.sockets or ():
        print(f"tls-terminator: listening on {socket_.getsockname()} → {arguments.target}",
              flush=True)

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for received in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(received, stopping.set)

    await stopping.wait()

    # Closed, and deliberately **not** awaited with `wait_closed()`.
    #
    # `wait_closed` waits for every handler task to finish, and a byte proxy's
    # handler tasks live exactly as long as the connections they relay. An
    # agent holds a WebSocket open for hours, so waiting here means never
    # exiting — which is not a theoretical concern: it hung the installation
    # matrix for twelve minutes on the step that stops the controller to test
    # upgrade rollback, because the script's `wait` never returned.
    #
    # Returning instead lets asyncio.run cancel the outstanding relays and
    # close the loop, which is the right behaviour for a test fixture being
    # shut down: the connections it was carrying are supposed to break.
    server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
