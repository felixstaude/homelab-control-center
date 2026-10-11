"""``hcc bootstrap-admin`` — create the initial administrator.

Phase 1, milestone 1.5.

## Why this is a command and not an endpoint

Creating the first administrator is authorized by **local access to the
controller's database**, which is what running this command proves. The
alternative — an HTTP endpoint that works while no user exists — gives
ownership of the deployment to whoever reaches the port first, and on a
homelab that race is real: the control plane is frequently reachable on the
LAN before the operator has opened the browser. Losing it is also invisible,
because the installation then simply looks already set up.

The reasoning is in :mod:`hcc.security.bootstrap`; this module is the terminal
interface to it.

## Handling of the password

* **Never a command-line argument.** Command lines are world-readable through
  ``/proc`` and land in shell history.
* **Never an environment variable.** ``/proc/<pid>/environ`` is readable by
  the same user, and the environment is inherited by child processes. The
  agent's token handling already treats the environment as the inferior option
  (``docs/AGENT_ARCHITECTURE.md §4.2``); the same reasoning applies here, and
  since a better option exists this one is simply not offered.
* **Interactively**, read twice with echo off via :func:`getpass.getpass`.
* **Non-interactively**, read from standard input with ``--password-stdin``,
  so automation can supply it from a file or a secret store without it
  appearing in any process listing:

      hcc bootstrap-admin --username admin --password-stdin < /run/secrets/admin-password

Nothing in this module prints, logs or returns the password, and the only
value derived from it that leaves is the Argon2 hash, which is written to the
database and never displayed.

## Exit codes

Distinct codes so a provisioning script can branch without parsing output,
following the agent's convention that a misconfiguration is 2:

| Code | Meaning |
| --- | --- |
| 0 | The administrator was created. |
| 1 | Runtime failure — database unreachable, unexpected error. |
| 2 | Usage or input error — bad arguments, invalid username, weak password. |
| 3 | Refused: already initialized. Idempotent callers treat this as success. |
"""

from __future__ import annotations

import getpass
import sys
from typing import TYPE_CHECKING, Final, TextIO

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from hcc.models.user import normalize_username
from hcc.security.bootstrap import (
    AlreadyInitializedError,
    BootstrapError,
    BootstrapMethod,
    BootstrapResult,
    bootstrap_administrator,
    count_users,
    is_initialized,
)
from hcc.security.password import PasswordPolicyError, build_password_hasher

if TYPE_CHECKING:
    import argparse

    from hcc.config import Settings

EXIT_OK: Final = 0
EXIT_RUNTIME_ERROR: Final = 1
EXIT_USAGE_ERROR: Final = 2
EXIT_ALREADY_INITIALIZED: Final = 3

#: Tables the bootstrap needs. Their absence means migrations have not been
#: applied, which is the common first-run mistake and deserves a precise
#: message rather than a driver error about a missing relation.
REQUIRED_TABLES: Final = ("users", "installation")

#: How many times to re-prompt after a rejected username or password. Finite,
#: so a non-interactive invocation that somehow reaches the prompt cannot spin.
MAX_PROMPT_ATTEMPTS: Final = 3


class CliError(Exception):
    """A failure to report to the operator, with an exit code.

    Carries the exit code alongside the message so the caller does not have
    to map message text back onto a code.
    """

    def __init__(self, message: str, exit_code: int = EXIT_USAGE_ERROR) -> None:
        self.exit_code = exit_code
        super().__init__(message)


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``bootstrap-admin`` subcommand."""
    parser = subparsers.add_parser(
        "bootstrap-admin",
        help="Create the initial administrator for a fresh installation.",
        description=(
            "Create the initial administrator. Runs on the controller host or "
            "inside its container, against the database named by "
            "HCC_DATABASE_URL. Refused once the installation has been "
            "initialized, even if every account is later removed."
        ),
        epilog=(
            "The password is never accepted as an argument or an environment "
            "variable. Interactively it is prompted for twice with echo off; "
            "for automation use --password-stdin, e.g.\n"
            "  hcc bootstrap-admin --username admin --password-stdin "
            "< /run/secrets/admin-password"
        ),
    )
    parser.add_argument(
        "--username",
        help="Administrator username. Prompted for when omitted.",
    )
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help=(
            "Read the password from the first line of standard input instead "
            "of prompting. Required when standard input is not a terminal."
        ),
    )
    parser.set_defaults(handler=run)


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------


async def _check_connectivity(session_factory: async_sessionmaker) -> None:  # type: ignore[type-arg]
    """Confirm the database answers, with an actionable error if not."""
    try:
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError) as exc:
        # OSError as well as SQLAlchemyError: a refused TCP connection — the
        # most likely failure on a first run — surfaces as ConnectionRefusedError
        # from asyncpg's connect and is not wrapped by SQLAlchemy.
        #
        # The driver message can contain the connection string, and therefore
        # the password, so only the exception type is shown.
        raise CliError(
            "Could not connect to the database "
            f"({type(exc).__name__}). Check HCC_DATABASE_URL and that "
            "PostgreSQL is running.",
            EXIT_RUNTIME_ERROR,
        ) from exc


async def _check_schema(session_factory: async_sessionmaker) -> str | None:  # type: ignore[type-arg]
    """Confirm the schema is present. Returns the applied Alembic revision.

    Checks for the tables bootstrap writes rather than comparing against a
    hardcoded head revision: the tables are what this command actually needs,
    the check works regardless of how the package was installed, and it cannot
    drift from the migration chain the way a duplicated revision id would.
    """
    async with session_factory() as session:
        present = set(
            (
                await session.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = current_schema() "
                        "AND table_name = ANY(:names)"
                    ).bindparams(names=list(REQUIRED_TABLES))
                )
            )
            .scalars()
            .all()
        )
        missing = [name for name in REQUIRED_TABLES if name not in present]
        if missing:
            raise CliError(
                "The database schema is not up to date — missing "
                f"{', '.join(missing)}. Apply migrations first:\n"
                "    cd apps/server && alembic upgrade head\n"
                "  (or `make migrate` from the repository root)",
                EXIT_USAGE_ERROR,
            )

        revision = (
            await session.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
        ).scalar_one_or_none()
        return str(revision) if revision is not None else None


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


def _read_username(supplied: str | None, *, interactive: bool, out: TextIO) -> str:
    """Return a validated username, prompting when permitted.

    Normalisation runs here as well as in the service, so a typo is reported
    before the password is asked for rather than after.
    """
    if supplied is not None:
        try:
            normalize_username(supplied)
        except ValueError as exc:
            raise CliError(f"Invalid username: {exc}") from exc
        return supplied

    if not interactive:
        raise CliError(
            "--username is required when standard input is not a terminal.",
            EXIT_USAGE_ERROR,
        )

    for remaining in range(MAX_PROMPT_ATTEMPTS, 0, -1):
        candidate = input("Administrator username: ").strip()
        try:
            normalize_username(candidate)
        except ValueError as exc:
            print(f"  {exc}", file=out)
            if remaining > 1:
                continue
            raise CliError("Too many invalid usernames.", EXIT_USAGE_ERROR) from exc
        return candidate

    raise CliError("Too many invalid usernames.", EXIT_USAGE_ERROR)  # pragma: no cover


def _read_password_from_stdin(stdin: TextIO) -> str:
    """Read the password from the first line of standard input.

    Only the first line is taken, and a single trailing newline is stripped —
    ``printf 'secret' > file`` and ``echo secret > file`` must behave the
    same. Interior and leading whitespace is preserved, because a password may
    legitimately contain it (ADR-026) and silently trimming would alter the
    secret.
    """
    raw = stdin.readline()
    if not raw:
        raise CliError("No password was provided on standard input.", EXIT_USAGE_ERROR)
    return raw.removesuffix("\n").removesuffix("\r")


def _prompt_password(out: TextIO) -> str:
    """Prompt twice with echo off, requiring the two to match."""
    for remaining in range(MAX_PROMPT_ATTEMPTS, 0, -1):
        first = getpass.getpass("Administrator password: ")
        second = getpass.getpass("Repeat password: ")
        if first != second:
            print("  The passwords do not match.", file=out)
            if remaining > 1:
                continue
            raise CliError("Passwords did not match.", EXIT_USAGE_ERROR)
        return first

    raise CliError("Passwords did not match.", EXIT_USAGE_ERROR)  # pragma: no cover


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


async def run(
    args: argparse.Namespace,
    settings: Settings,
    *,
    stdin: TextIO | None = None,
    out: TextIO | None = None,
) -> int:
    """Execute ``bootstrap-admin``. Returns the process exit code.

    ``stdin`` and ``out`` are injectable so the whole command — including the
    non-interactive path and every failure message — is testable without
    driving a terminal.
    """
    stdin = stdin if stdin is not None else sys.stdin
    out = out if out is not None else sys.stdout

    # NullPool: a one-shot process gains nothing from pooling, and it means
    # every connection is closed before the event loop ends.
    engine = create_async_engine(settings.sqlalchemy_url(), poolclass=NullPool)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    try:
        await _check_connectivity(session_factory)
        revision = await _check_schema(session_factory)

        # An advisory pre-check, for a clear message and to avoid prompting
        # for a password that cannot be used. The authoritative check happens
        # inside the bootstrap transaction, under a lock — see
        # hcc.security.bootstrap.
        async with session_factory() as session:
            initialized_at = await is_initialized(session)
            existing_users = await count_users(session)

        if initialized_at is not None:
            # command is raised as a CliError inside the body so that the one
            # `except CliError` handler below prints it and maps it to an exit
            # code. That is the structure, not an oversight.
            raise CliError(  # noqa: TRY301
                "This installation was already initialized at "
                f"{initialized_at.isoformat()}, so creating an initial "
                "administrator is refused.\n"
                "  This holds even if no accounts currently exist: "
                "initialization is a one-way door, by design, so that an "
                "emptied user table cannot be used to claim ownership.",
                EXIT_ALREADY_INITIALIZED,
            )

        print(f"Database       : reachable (schema revision {revision or 'unknown'})", file=out)
        print(f"Existing users : {existing_users}", file=out)
        if existing_users:
            # Users without an installation row means a database that predates
            # this milestone, or a restore from such a backup. Worth saying,
            # because the command is about to succeed on a non-empty database.
            print(
                "  Note: accounts exist but this installation was never "
                "marked initialized. Bootstrap will proceed and mark it.",
                file=out,
            )
        print("", file=out)

        interactive = stdin.isatty() and not args.password_stdin
        username = _read_username(args.username, interactive=stdin.isatty(), out=out)

        if args.password_stdin:
            password = _read_password_from_stdin(stdin)
        elif interactive:
            password = _prompt_password(out)
        else:
            raise CliError(  # noqa: TRY301
                "Standard input is not a terminal, so the password cannot be "
                "prompted for. Pass --password-stdin and supply it on "
                "standard input.",
                EXIT_USAGE_ERROR,
            )

        try:
            async with session_factory() as session:
                result = await bootstrap_administrator(
                    session,
                    username=username,
                    password=password,
                    hasher=build_password_hasher(settings),
                    method=BootstrapMethod.CLI,
                )
        except PasswordPolicyError as exc:
            raise CliError(
                "The password does not meet the policy:\n"
                + "\n".join(f"  - {problem}" for problem in exc.problems),
                EXIT_USAGE_ERROR,
            ) from exc
        except ValueError as exc:
            # normalize_username rejected it inside the service. Reachable
            # when --username bypassed the earlier check in some edge case.
            raise CliError(f"Invalid username: {exc}") from exc
        except AlreadyInitializedError as exc:
            raise CliError(str(exc), EXIT_ALREADY_INITIALIZED) from exc
        except BootstrapError as exc:
            raise CliError(str(exc), EXIT_ALREADY_INITIALIZED) from exc
        except SQLAlchemyError as exc:
            raise CliError(
                f"The database refused the operation ({type(exc).__name__}). "
                "No administrator was created.",
                EXIT_RUNTIME_ERROR,
            ) from exc

        _report_success(result, out)
        # Inside the `try` on purpose, so a failure while reporting is handled
        # by the same path as every other failure rather than escaping.
        return EXIT_OK  # noqa: TRY300

    except CliError as exc:
        print(f"hcc bootstrap-admin: {exc}", file=sys.stderr)
        return exc.exit_code
    finally:
        await engine.dispose()


def _report_success(result: BootstrapResult, out: TextIO) -> None:
    """Confirm what was created. No credential material appears here."""
    print("Administrator created.", file=out)
    print("", file=out)
    print(f"  Username   : {result.username}", file=out)
    if result.username_canonical != result.username:
        # The canonical form is what uniqueness and login are keyed on
        # (ADR-014), so when it differs from what was typed, say so rather
        # than letting the operator discover it at the login prompt.
        print(f"  Login as   : {result.username_canonical}", file=out)
    print(f"  Role       : {result.role.value}", file=out)
    print(f"  Created at : {result.initialized_at.isoformat()}", file=out)
    print("", file=out)
    print(
        "This installation is now initialized. Bootstrap is closed and will not reopen.",
        file=out,
    )
    print("Log in at POST /api/v1/auth/login.", file=out)
