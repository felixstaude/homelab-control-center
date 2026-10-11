"""CLI entry point: ``hcc``.

Dispatches subcommands. Uses :mod:`argparse` from the standard library rather
than a CLI framework — one command with three options does not justify a
dependency on the authentication path's package set.

Settings are loaded through the same :func:`~hcc.config.load_settings_or_exit`
the server uses, so the command and the running controller can never disagree
about which database they target.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import TYPE_CHECKING, Final

from hcc import __version__
from hcc.cli import bootstrap
from hcc.config import load_settings_or_exit
from hcc.logging import configure_logging

if TYPE_CHECKING:
    from collections.abc import Sequence

EXIT_USAGE: Final = 2

DESCRIPTION: Final = """
Operator commands for the Homelab Control Center control plane.

These run on the controller host, or inside its container, against the
database named by HCC_DATABASE_URL.
"""


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser.

    Separate from :func:`main` so tests can inspect the interface — in
    particular, that no option accepts a password.
    """
    parser = argparse.ArgumentParser(
        prog="hcc",
        description=DESCRIPTION.strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"hcc {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    bootstrap.add_parser(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run the selected command.

    Returns an exit code rather than calling :func:`sys.exit`, so the whole
    path is testable. The console-script wrapper below does the exiting.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "handler", None) is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE

    # Loaded after parsing, so `--help` and `--version` work without a
    # configured database. Exits 2 with a readable diagnostic when the
    # environment is invalid.
    settings = load_settings_or_exit()
    # Commands write their own output to stdout; the log stream carries the
    # structured events (`bootstrap_completed`) for an operator collecting
    # them. WARNING keeps routine engine chatter out of a terminal session.
    configure_logging(level="WARNING", log_format=settings.log_format)

    # `args.handler` comes off an argparse Namespace and is therefore Any;
    # the cast states the contract every `set_defaults(handler=...)` honours.
    exit_code: int = asyncio.run(args.handler(args, settings))
    return exit_code


def run() -> None:
    """Console-script entry point."""
    sys.exit(main())


if __name__ == "__main__":
    run()
