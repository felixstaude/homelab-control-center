"""Process entrypoint.

Run in development with hot reload:

    make dev-server

Run directly:

    python -m hcc.main
    python -m hcc.main --reload

**This module is the only supported way to start the server**, in development
and in production alike. Pointing uvicorn at the factory directly works, but
silently loses what is configured below — the absent ``Server`` banner, the
disabled duplicate access log, and the single logging chain — so the two
invocations would harden differently. The container image and the `make`
targets therefore both go through here.
"""

from __future__ import annotations

import argparse

import uvicorn

from hcc.app import create_app
from hcc.config import load_settings_or_exit
from hcc.logging import configure_logging

#: Import string for the application factory.
#:
#: Reload needs a string rather than an instance: the reloader re-imports the
#: application in a subprocess on every change, which it cannot do with an
#: object that was already constructed.
APP_FACTORY = "hcc.app:create_app"


def main(argv: list[str] | None = None) -> None:
    """Start the HTTP server using validated settings."""
    parser = argparse.ArgumentParser(
        prog="python -m hcc.main",
        description="Run the Homelab Control Center control plane.",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Restart on source changes. Development only.",
    )
    arguments = parser.parse_args(argv)

    # Validated in this process even when reloading, so a configuration
    # mistake is reported once, immediately, rather than by a subprocess that
    # the reloader then restarts in a loop.
    settings = load_settings_or_exit()

    # Configure logging in *this* process as well, not only inside
    # `create_app`. When reloading, this process is the reloader and never
    # imports the application, so without this its own messages — which
    # directories it is watching, that it is reloading now, and the traceback
    # when a reload fails — have no handler and vanish. Idempotent:
    # `create_app` calls it too.
    configure_logging(level=settings.log_level, log_format=settings.log_format)

    uvicorn.run(
        APP_FACTORY if arguments.reload else create_app(settings),
        factory=arguments.reload,
        reload=arguments.reload,
        host=settings.host,
        port=settings.port,
        # Logging is configured by the application itself (hcc.logging), so
        # uvicorn's own dictConfig is disabled to avoid two handler chains.
        log_config=None,
        access_log=False,
        # No `Server: uvicorn` banner. It names the software to anyone who
        # asks, which ADR-032's "disclose as little as possible" rules out and
        # which a client has no use for. A version-less banner is only a small
        # help to an attacker, but it is a free one to withhold.
        server_header=False,
    )


if __name__ == "__main__":
    main()
