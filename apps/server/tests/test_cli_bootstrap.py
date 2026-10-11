"""The ``hcc bootstrap-admin`` command.

Milestone 1.5. Split in two: the interface contract (no option may carry a
password) is a unit test, and the behaviour is exercised end to end against
real PostgreSQL, because the command's whole purpose is to write a row that
survives.
"""

from __future__ import annotations

import argparse
import io
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select, text

from hcc.cli.bootstrap import (
    EXIT_ALREADY_INITIALIZED,
    EXIT_OK,
    EXIT_RUNTIME_ERROR,
    EXIT_USAGE_ERROR,
)
from hcc.cli.bootstrap import run as bootstrap_command
from hcc.cli.main import build_parser, main
from hcc.config import Settings
from hcc.models.user import User, UserRole

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

PASSWORD = "correct horse battery staple"


class FakeStdin(io.StringIO):
    """Standard input that reports whether it is a terminal.

    The command branches on ``isatty`` to decide between prompting and
    reading, so the tests have to be able to present both.
    """

    def __init__(self, content: str = "", *, tty: bool = False) -> None:
        super().__init__(content)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


# ---------------------------------------------------------------------------
# Interface contract — no database needed
# ---------------------------------------------------------------------------


class TestCommandInterface:
    @pytest.mark.parametrize("option", ["--password", "--pass", "--admin-password"])
    def test_no_option_accepts_a_password(self, option: str) -> None:
        """The contract that keeps the secret out of ``/proc`` and history.

        Asserted behaviourally rather than by introspecting argparse: an
        option that does not exist makes the parser exit, while one that took
        a password would parse happily. So adding ``--password`` in future
        fails here.
        """
        parser = build_parser()

        with pytest.raises(SystemExit):
            parser.parse_args(["bootstrap-admin", "--username", "admin", option, "a-password"])

    def test_password_stdin_is_a_flag_carrying_no_value(self) -> None:
        parser = build_parser()

        args = parser.parse_args(["bootstrap-admin", "--username", "admin", "--password-stdin"])
        assert args.password_stdin is True

        # A following token is not consumed as a value, so a password cannot
        # be smuggled in after the flag either.
        with pytest.raises(SystemExit):
            parser.parse_args(["bootstrap-admin", "--password-stdin", "a-password"])

    def test_the_subcommand_is_registered(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["bootstrap-admin", "--username", "admin"])
        assert args.username == "admin"
        assert args.password_stdin is False

    def test_no_command_prints_help_and_fails(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main([]) == 2
        assert "bootstrap-admin" in capsys.readouterr().err

    def test_version_is_available_without_configuration(self) -> None:
        # `--version` and `--help` must not require a database URL, so an
        # operator can discover the interface before configuring anything.
        with pytest.raises(SystemExit) as caught:
            main(["--version"])
        assert caught.value.code == 0


# ---------------------------------------------------------------------------
# Behaviour, against the real database
# ---------------------------------------------------------------------------


def command_args(**overrides: Any) -> argparse.Namespace:
    """Build the parsed arguments the command expects.

    Module-level so both test classes use the same shape; the real parser is
    exercised separately in :class:`TestCommandInterface`.
    """
    return argparse.Namespace(**{"username": None, "password_stdin": False, **overrides})


@pytest.mark.integration
class TestBootstrapCommand:
    @staticmethod
    def _settings(database_url: str) -> Settings:
        return Settings(
            environment="test",
            database_url=database_url,
            log_level="WARNING",
            database_pool_class="null",
            # Cheapest permitted Argon2 parameters: these tests hash a real
            # password and the defaults would make them slow for no coverage.
            password_hash_memory_cost=19456,
            password_hash_time_cost=2,
            password_hash_parallelism=1,
        )

    async def test_creates_an_administrator_non_interactively(
        self, db_session: AsyncSession, migrated_database: str
    ) -> None:
        out = io.StringIO()

        code = await bootstrap_command(
            command_args(username="FelixAdmin", password_stdin=True),
            self._settings(migrated_database),
            stdin=FakeStdin(f"{PASSWORD}\n"),
            out=out,
        )

        assert code == EXIT_OK
        rendered = out.getvalue()
        assert "Administrator created." in rendered
        # The canonical form is shown when it differs, because that is what
        # the operator must type at the login prompt.
        assert "felixadmin" in rendered

        user = (await db_session.execute(select(User))).scalar_one()
        assert user.username == "FelixAdmin"
        assert user.role is UserRole.ADMIN
        assert user.is_active is True

    async def test_the_created_credential_authenticates(
        self, db_session: AsyncSession, migrated_database: str, fast_hasher: Any
    ) -> None:
        await bootstrap_command(
            command_args(username="admin", password_stdin=True),
            self._settings(migrated_database),
            stdin=FakeStdin(f"{PASSWORD}\n"),
            out=io.StringIO(),
        )

        user = (await db_session.execute(select(User))).scalar_one()
        assert fast_hasher.verify(user.password_hash, PASSWORD) is True

    async def test_output_contains_no_secret(
        self, db_session: AsyncSession, migrated_database: str
    ) -> None:
        out = io.StringIO()
        await bootstrap_command(
            command_args(username="admin", password_stdin=True),
            self._settings(migrated_database),
            stdin=FakeStdin(f"{PASSWORD}\n"),
            out=out,
        )

        rendered = out.getvalue()
        assert PASSWORD not in rendered
        assert "$argon2" not in rendered

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            (f"{PASSWORD}\n", PASSWORD),
            # No trailing newline: `printf` rather than `echo`.
            (PASSWORD, PASSWORD),
            # CRLF, from a file written on Windows.
            (f"{PASSWORD}\r\n", PASSWORD),
        ],
    )
    async def test_stdin_line_endings_are_handled(
        self,
        db_session: AsyncSession,
        migrated_database: str,
        line: str,
        expected: str,
        fast_hasher: Any,
    ) -> None:
        await bootstrap_command(
            command_args(username="admin", password_stdin=True),
            self._settings(migrated_database),
            stdin=FakeStdin(line),
            out=io.StringIO(),
        )

        user = (await db_session.execute(select(User))).scalar_one()
        assert fast_hasher.verify(user.password_hash, expected) is True

    async def test_leading_whitespace_in_the_password_is_preserved(
        self, db_session: AsyncSession, migrated_database: str, fast_hasher: Any
    ) -> None:
        # A password manager may generate one; trimming would silently alter
        # the secret (ADR-026).
        spaced = f"  {PASSWORD}"
        await bootstrap_command(
            command_args(username="admin", password_stdin=True),
            self._settings(migrated_database),
            stdin=FakeStdin(f"{spaced}\n"),
            out=io.StringIO(),
        )

        user = (await db_session.execute(select(User))).scalar_one()
        assert fast_hasher.verify(user.password_hash, spaced) is True

    async def test_a_second_run_is_refused(
        self, db_session: AsyncSession, migrated_database: str
    ) -> None:
        settings = self._settings(migrated_database)
        await bootstrap_command(
            command_args(username="first", password_stdin=True),
            settings,
            stdin=FakeStdin(f"{PASSWORD}\n"),
            out=io.StringIO(),
        )

        code = await bootstrap_command(
            command_args(username="second", password_stdin=True),
            settings,
            stdin=FakeStdin(f"{PASSWORD}\n"),
            out=io.StringIO(),
        )

        # A distinct exit code, so an idempotent provisioning script can treat
        # "already done" as success without parsing output.
        assert code == EXIT_ALREADY_INITIALIZED
        assert (await db_session.execute(text("SELECT count(*) FROM users"))).scalar_one() == 1

    async def test_refusal_survives_deleting_every_user(
        self, db_session: AsyncSession, migrated_database: str
    ) -> None:
        settings = self._settings(migrated_database)
        await bootstrap_command(
            command_args(username="first", password_stdin=True),
            settings,
            stdin=FakeStdin(f"{PASSWORD}\n"),
            out=io.StringIO(),
        )
        await db_session.execute(text("DELETE FROM users"))
        await db_session.commit()

        code = await bootstrap_command(
            command_args(username="attacker", password_stdin=True),
            settings,
            stdin=FakeStdin(f"{PASSWORD}\n"),
            out=io.StringIO(),
        )

        assert code == EXIT_ALREADY_INITIALIZED
        assert (await db_session.execute(text("SELECT count(*) FROM users"))).scalar_one() == 0

    async def test_a_weak_password_is_refused_with_the_reason(
        self, db_session: AsyncSession, migrated_database: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = await bootstrap_command(
            command_args(username="admin", password_stdin=True),
            self._settings(migrated_database),
            stdin=FakeStdin("short\n"),
            out=io.StringIO(),
        )

        assert code == EXIT_USAGE_ERROR
        error = capsys.readouterr().err
        assert "at least 8 characters" in error
        # The rejected password must not be echoed back.
        assert "short" not in error.replace("too short", "")
        assert (await db_session.execute(text("SELECT count(*) FROM users"))).scalar_one() == 0

    async def test_an_invalid_username_is_refused(
        self, db_session: AsyncSession, migrated_database: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = await bootstrap_command(
            command_args(username="ab", password_stdin=True),
            self._settings(migrated_database),
            stdin=FakeStdin(f"{PASSWORD}\n"),
            out=io.StringIO(),
        )

        assert code == EXIT_USAGE_ERROR
        assert "Invalid username" in capsys.readouterr().err
        # Refused before the installation was marked, so a typo does not
        # consume the one-shot bootstrap.
        assert (
            await db_session.execute(text("SELECT count(*) FROM installation"))
        ).scalar_one() == 0

    async def test_empty_stdin_is_refused(
        self, migrated_database: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = await bootstrap_command(
            command_args(username="admin", password_stdin=True),
            self._settings(migrated_database),
            stdin=FakeStdin(""),
            out=io.StringIO(),
        )

        assert code == EXIT_USAGE_ERROR
        assert "No password" in capsys.readouterr().err

    async def test_a_missing_username_without_a_terminal_is_refused(
        self, migrated_database: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Rather than hanging on a prompt nobody can answer.
        code = await bootstrap_command(
            command_args(password_stdin=True),
            self._settings(migrated_database),
            stdin=FakeStdin(f"{PASSWORD}\n"),
            out=io.StringIO(),
        )

        assert code == EXIT_USAGE_ERROR
        assert "--username is required" in capsys.readouterr().err

    async def test_no_password_source_without_a_terminal_is_refused(
        self, migrated_database: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = await bootstrap_command(
            command_args(username="admin"),
            self._settings(migrated_database),
            stdin=FakeStdin(f"{PASSWORD}\n", tty=False),
            out=io.StringIO(),
        )

        assert code == EXIT_USAGE_ERROR
        assert "--password-stdin" in capsys.readouterr().err

    async def test_reports_the_schema_revision_and_user_count(self, migrated_database: str) -> None:
        out = io.StringIO()
        await bootstrap_command(
            command_args(username="admin", password_stdin=True),
            self._settings(migrated_database),
            stdin=FakeStdin(f"{PASSWORD}\n"),
            out=out,
        )

        rendered = out.getvalue()
        assert "reachable" in rendered
        assert "0004_installation" in rendered
        assert "Existing users : 0" in rendered


@pytest.mark.integration
class TestPreflightFailures:
    async def test_an_unreachable_database_is_reported_without_credentials(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Port 1, so the connection fails fast. The driver's message can
        # contain the connection string and therefore the password, so only
        # the exception type may be shown.
        settings = Settings(
            environment="test",
            database_url="postgresql://hcc:sup3rsecret@127.0.0.1:1/hcc",
            log_level="WARNING",
            database_pool_class="null",
        )

        code = await bootstrap_command(
            command_args(username="admin", password_stdin=True),
            settings,
            stdin=FakeStdin(f"{PASSWORD}\n"),
            out=io.StringIO(),
        )

        assert code == EXIT_RUNTIME_ERROR
        error = capsys.readouterr().err
        assert "Could not connect" in error
        assert "sup3rsecret" not in error

    async def test_a_missing_schema_names_the_fix(
        self, scratch_database: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The common first-run mistake: database reachable, migrations not
        # applied. A real empty database rather than a simulated one, so the
        # check is exercised against the state an operator would actually hit.
        settings = Settings(
            environment="test",
            database_url=scratch_database,
            log_level="WARNING",
            database_pool_class="null",
        )

        code = await bootstrap_command(
            command_args(username="admin", password_stdin=True),
            settings,
            stdin=FakeStdin(f"{PASSWORD}\n"),
            out=io.StringIO(),
        )

        assert code == EXIT_USAGE_ERROR
        error = capsys.readouterr().err
        assert "alembic upgrade head" in error
        assert "users" in error and "installation" in error
