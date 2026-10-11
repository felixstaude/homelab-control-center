"""First-run bootstrap, against real PostgreSQL.

Milestone 1.5. These run against a live database on purpose: the properties
under test — transaction atomicity, the singleton primary key, advisory-lock
serialisation, and behaviour under genuine concurrency — are properties *of
the database*. A mock would assert that the code calls the functions it calls,
which is not the same claim and would pass with a broken schema.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
import structlog
from sqlalchemy import select, text

from hcc.models.installation import Installation
from hcc.models.user import User, UserRole
from hcc.security.bootstrap import (
    BOOTSTRAP_LOCK_KEY,
    AlreadyInitializedError,
    BootstrapMethod,
    ConcurrentBootstrapError,
    UsernameTakenError,
    bootstrap_administrator,
    count_users,
    is_initialized,
)
from hcc.security.password import PasswordPolicyError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from hcc.security.password import PasswordHasher

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"


async def _installation_rows(db: AsyncSession) -> int:
    return (await db.execute(text("SELECT count(*) FROM installation"))).scalar_one()


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


class TestSuccessfulBootstrap:
    async def test_creates_an_active_administrator(
        self, db_session: AsyncSession, fast_hasher: PasswordHasher
    ) -> None:
        result = await bootstrap_administrator(
            db_session, username="FelixAdmin", password=PASSWORD, hasher=fast_hasher
        )

        assert result.role is UserRole.ADMIN
        # The display form is preserved; the canonical form is what login and
        # uniqueness are keyed on (ADR-014).
        assert result.username == "FelixAdmin"
        assert result.username_canonical == "felixadmin"

        user = (await db_session.execute(select(User))).scalar_one()
        assert user.id == result.user_id
        assert user.role is UserRole.ADMIN
        assert user.is_active is True
        assert user.username == "FelixAdmin"
        assert user.username_canonical == "felixadmin"

    async def test_the_password_verifies(
        self, db_session: AsyncSession, fast_hasher: PasswordHasher
    ) -> None:
        # The whole point of the command: the credential it creates must
        # actually authenticate.
        await bootstrap_administrator(
            db_session, username="admin", password=PASSWORD, hasher=fast_hasher
        )

        user = (await db_session.execute(select(User))).scalar_one()
        assert fast_hasher.verify(user.password_hash, PASSWORD) is True
        assert fast_hasher.verify(user.password_hash, "not the password") is False

    async def test_no_plaintext_is_stored(
        self, db_session: AsyncSession, fast_hasher: PasswordHasher
    ) -> None:
        await bootstrap_administrator(
            db_session, username="admin", password=PASSWORD, hasher=fast_hasher
        )

        user = (await db_session.execute(select(User))).scalar_one()
        assert PASSWORD not in user.password_hash
        assert user.password_hash.startswith("$argon2id$")

    async def test_marks_the_installation_initialized(
        self, db_session: AsyncSession, fast_hasher: PasswordHasher
    ) -> None:
        assert await is_initialized(db_session) is None

        result = await bootstrap_administrator(
            db_session, username="admin", password=PASSWORD, hasher=fast_hasher
        )

        initialized_at = await is_initialized(db_session)
        assert initialized_at is not None
        assert initialized_at == result.initialized_at

        row = (await db_session.execute(select(Installation))).scalar_one()
        assert row.id is True
        assert row.bootstrap_method == BootstrapMethod.CLI.value
        assert row.initialized_by_user_id == result.user_id

    async def test_both_rows_are_committed(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        fast_hasher: PasswordHasher,
    ) -> None:
        # Read back through a *different* session, so the assertion cannot be
        # satisfied by uncommitted state in the writer's transaction.
        async with db_session_factory() as writer:
            await bootstrap_administrator(
                writer, username="admin", password=PASSWORD, hasher=fast_hasher
            )

        async with db_session_factory() as reader:
            assert await count_users(reader) == 1
            assert await is_initialized(reader) is not None

    async def test_survives_a_restart(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        fast_hasher: PasswordHasher,
    ) -> None:
        # "Restart" is a fresh session against the same database; the state
        # lives in PostgreSQL, so nothing in the process should matter.
        async with db_session_factory() as first:
            await bootstrap_administrator(
                first, username="admin", password=PASSWORD, hasher=fast_hasher
            )

        async with db_session_factory() as second:
            assert await is_initialized(second) is not None
            with pytest.raises(AlreadyInitializedError):
                await bootstrap_administrator(
                    second, username="second", password=PASSWORD, hasher=fast_hasher
                )


# ---------------------------------------------------------------------------
# The one-way door
# ---------------------------------------------------------------------------


class TestInitializationIsPermanent:
    async def test_a_second_attempt_is_refused(
        self, db_session: AsyncSession, fast_hasher: PasswordHasher
    ) -> None:
        await bootstrap_administrator(
            db_session, username="first", password=PASSWORD, hasher=fast_hasher
        )

        with pytest.raises(AlreadyInitializedError) as caught:
            await bootstrap_administrator(
                db_session, username="second", password=PASSWORD, hasher=fast_hasher
            )

        # The error states when, so an operator can tell a stale deployment
        # from a hijacked one.
        assert caught.value.initialized_at is not None
        assert await count_users(db_session) == 1

    async def test_deleting_every_user_does_not_reopen_bootstrap(
        self, db_session: AsyncSession, fast_hasher: PasswordHasher
    ) -> None:
        """The durable invariant, and the reason it is a row and not a count.

        Deriving "may bootstrap" from an empty ``users`` table would hand
        ownership of a deployment to whoever noticed that the last account had
        been removed. This asserts the opposite.
        """
        await bootstrap_administrator(
            db_session, username="first", password=PASSWORD, hasher=fast_hasher
        )

        await db_session.execute(text("DELETE FROM users"))
        await db_session.commit()
        assert await count_users(db_session) == 0

        with pytest.raises(AlreadyInitializedError):
            await bootstrap_administrator(
                db_session, username="attacker", password=PASSWORD, hasher=fast_hasher
            )

        assert await count_users(db_session) == 0

    async def test_deactivating_every_user_does_not_reopen_bootstrap(
        self, db_session: AsyncSession, fast_hasher: PasswordHasher
    ) -> None:
        await bootstrap_administrator(
            db_session, username="first", password=PASSWORD, hasher=fast_hasher
        )
        await db_session.execute(text("UPDATE users SET is_active = false"))
        await db_session.commit()

        with pytest.raises(AlreadyInitializedError):
            await bootstrap_administrator(
                db_session, username="attacker", password=PASSWORD, hasher=fast_hasher
            )

    async def test_the_database_refuses_a_second_installation_row(
        self, db_session: AsyncSession, fast_hasher: PasswordHasher
    ) -> None:
        # The guarantee does not depend on the application's pre-check. The
        # singleton primary key means a second row is impossible even by
        # direct SQL, which is what makes the concurrency behaviour sound.
        await bootstrap_administrator(
            db_session, username="first", password=PASSWORD, hasher=fast_hasher
        )

        with pytest.raises(Exception, match=r"duplicate key|unique"):
            await db_session.execute(
                text(
                    "INSERT INTO installation (id, initialized_at, bootstrap_method) "
                    "VALUES (true, now(), 'cli')"
                )
            )
        await db_session.rollback()

    async def test_the_singleton_check_rejects_a_false_row(self, db_session: AsyncSession) -> None:
        # `CHECK (id)` is what makes the primary key a singleton rather than a
        # two-row table.
        with pytest.raises(Exception, match=r"singleton|violates check"):
            await db_session.execute(
                text(
                    "INSERT INTO installation (id, initialized_at, bootstrap_method) "
                    "VALUES (false, now(), 'cli')"
                )
            )
        await db_session.rollback()


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


class TestConcurrency:
    async def test_concurrent_attempts_create_exactly_one_administrator(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        fast_hasher: PasswordHasher,
    ) -> None:
        """Ten simultaneous attempts, one administrator.

        Each uses its own session and therefore its own connection and
        transaction, so this exercises PostgreSQL's serialisation rather than
        anything in the process. No sleeps: the attempts are started together
        with ``asyncio.gather`` and the database decides the order.
        """

        async def attempt(index: int) -> str:
            async with db_session_factory() as session:
                try:
                    await bootstrap_administrator(
                        session,
                        username=f"admin{index}",
                        password=PASSWORD,
                        hasher=fast_hasher,
                    )
                except (AlreadyInitializedError, ConcurrentBootstrapError) as exc:
                    return type(exc).__name__
                return "created"

        outcomes = await asyncio.gather(*(attempt(index) for index in range(10)))

        assert outcomes.count("created") == 1, outcomes
        # Every loser was refused for a reason that means "already
        # initialized", never an unexpected error.
        assert set(outcomes) <= {"created", "AlreadyInitializedError", "ConcurrentBootstrapError"}

        async with db_session_factory() as reader:
            assert await count_users(reader) == 1
            assert await _installation_rows(reader) == 1

    async def test_the_advisory_lock_serialises_attempts(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        fast_hasher: PasswordHasher,
    ) -> None:
        """A bootstrap blocks on the advisory lock while another holds it.

        Deterministic, not timing-based: the test waits for PostgreSQL to
        report an *ungranted* advisory lock request in ``pg_locks``, which is
        direct evidence that the attempt is blocked on exactly this lock. An
        earlier version of this test merely asserted the task had not finished
        yet, which passed even with the lock removed — because the task simply
        had not got that far. Observing the lock state removes the guesswork.
        """
        async with db_session_factory() as holder:
            # Transaction-scoped lock, held until this transaction ends.
            await holder.execute(
                text("SELECT pg_advisory_xact_lock(:key)").bindparams(key=BOOTSTRAP_LOCK_KEY)
            )

            async def attempt() -> str:
                async with db_session_factory() as session:
                    await bootstrap_administrator(
                        session, username="admin", password=PASSWORD, hasher=fast_hasher
                    )
                    return "created"

            task = asyncio.create_task(attempt())

            async def waiting_on_the_lock() -> bool:
                return bool(
                    (
                        await holder.execute(
                            text(
                                "SELECT count(*) FROM pg_locks "
                                "WHERE locktype = 'advisory' AND NOT granted "
                                "AND (classid::bigint << 32) | objid::bigint = :key"
                            ).bindparams(key=BOOTSTRAP_LOCK_KEY)
                        )
                    ).scalar_one()
                )

            blocked = False
            for _ in range(200):
                if await waiting_on_the_lock():
                    blocked = True
                    break
                if task.done():
                    break
                # Yield to let the attempt progress to the lock. The loop
                # exits on an observed state, not after a fixed duration.
                await asyncio.sleep(0.01)

            assert blocked, (
                "no attempt was ever seen waiting for the bootstrap advisory "
                "lock, so attempts are not serialised by it"
            )
            assert not task.done(), "bootstrap completed while the lock was held"

            # Ending the holder's transaction releases the lock.
            await holder.rollback()

            assert await task == "created"

        async with db_session_factory() as reader:
            assert await count_users(reader) == 1

    async def test_an_interrupted_attempt_leaves_no_state(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        fast_hasher: PasswordHasher,
    ) -> None:
        # Cancellation mid-flight must not leave a user without an
        # installation row, or an installation row without a user. The
        # advisory lock is transaction-scoped, so it is released too — a
        # later attempt must not block forever.
        async def attempt() -> None:
            async with db_session_factory() as session:
                await bootstrap_administrator(
                    session, username="admin", password=PASSWORD, hasher=fast_hasher
                )

        task = asyncio.create_task(attempt())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        async with db_session_factory() as reader:
            users = await count_users(reader)
            rows = await _installation_rows(reader)
        # Either it completed before cancellation took effect, or nothing
        # happened. Never half.
        assert (users, rows) in {(0, 0), (1, 1)}, (users, rows)

        # And bootstrap still works afterwards, which proves no lock leaked.
        if users == 0:
            async with db_session_factory() as session:
                await bootstrap_administrator(
                    session, username="admin", password=PASSWORD, hasher=fast_hasher
                )
            async with db_session_factory() as reader:
                assert await count_users(reader) == 1


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


class TestFailureHandling:
    @pytest.mark.parametrize(
        "username",
        ["", "   ", "ab", "a" * 33, "ad min", "admin​", "admin\x00"],
    )
    async def test_invalid_usernames_are_refused(
        self, db_session: AsyncSession, fast_hasher: PasswordHasher, username: str
    ) -> None:
        with pytest.raises(ValueError):
            await bootstrap_administrator(
                db_session, username=username, password=PASSWORD, hasher=fast_hasher
            )

        # Refused before anything was written, and before the installation was
        # marked — so a typo does not consume the one-shot bootstrap.
        assert await count_users(db_session) == 0
        assert await is_initialized(db_session) is None

    @pytest.mark.parametrize("password", ["", "short", "1234567"])
    async def test_weak_passwords_are_refused(
        self, db_session: AsyncSession, fast_hasher: PasswordHasher, password: str
    ) -> None:
        with pytest.raises(PasswordPolicyError):
            await bootstrap_administrator(
                db_session, username="admin", password=password, hasher=fast_hasher
            )

        assert await count_users(db_session) == 0
        assert await is_initialized(db_session) is None

    async def test_an_over_long_password_is_refused_not_truncated(
        self, db_session: AsyncSession, fast_hasher: PasswordHasher
    ) -> None:
        with pytest.raises(PasswordPolicyError):
            await bootstrap_administrator(
                db_session, username="admin", password="x" * 2000, hasher=fast_hasher
            )
        assert await count_users(db_session) == 0

    async def test_the_policy_error_does_not_contain_the_password(
        self, db_session: AsyncSession, fast_hasher: PasswordHasher
    ) -> None:
        secret = "sh0rt"
        with pytest.raises(PasswordPolicyError) as caught:
            await bootstrap_administrator(
                db_session, username="admin", password=secret, hasher=fast_hasher
            )
        assert secret not in str(caught.value)
        assert all(secret not in problem for problem in caught.value.problems)

    async def test_a_taken_username_is_reported_distinctly(
        self, db_session: AsyncSession, fast_hasher: PasswordHasher
    ) -> None:
        # Users exist but the installation was never marked — a database
        # restored from a backup predating this milestone. "Pick another name"
        # is a different instruction from "already initialized".
        db_session.add(
            User(
                username="admin",
                password_hash=fast_hasher.hash(PASSWORD),
                role=UserRole.VIEWER,
            )
        )
        await db_session.commit()

        with pytest.raises(UsernameTakenError):
            await bootstrap_administrator(
                db_session, username="Admin", password=PASSWORD, hasher=fast_hasher
            )

        assert await is_initialized(db_session) is None
        assert await count_users(db_session) == 1

    async def test_bootstrap_proceeds_when_unrelated_users_exist(
        self, db_session: AsyncSession, fast_hasher: PasswordHasher
    ) -> None:
        # Same restored-backup case, but with a free username: the command
        # should work and mark the installation.
        db_session.add(
            User(
                username="someone",
                password_hash=fast_hasher.hash(PASSWORD),
                role=UserRole.VIEWER,
            )
        )
        await db_session.commit()

        result = await bootstrap_administrator(
            db_session, username="admin", password=PASSWORD, hasher=fast_hasher
        )

        assert result.role is UserRole.ADMIN
        assert await count_users(db_session) == 2
        assert await is_initialized(db_session) is not None


class TestLogging:
    async def test_no_secret_reaches_a_log_event(
        self, db_session: AsyncSession, fast_hasher: PasswordHasher
    ) -> None:
        with structlog.testing.capture_logs() as events:
            await bootstrap_administrator(
                db_session, username="admin", password=PASSWORD, hasher=fast_hasher
            )

        assert events, "bootstrap logged nothing at all"
        for event in events:
            for key, value in event.items():
                rendered = f"{key}={value!r}"
                assert PASSWORD not in rendered, rendered
                assert "$argon2" not in rendered, rendered

    async def test_completion_is_logged_with_the_canonical_username(
        self, db_session: AsyncSession, fast_hasher: PasswordHasher
    ) -> None:
        # The operator needs to know what to log in as; it is not a secret.
        with structlog.testing.capture_logs() as events:
            await bootstrap_administrator(
                db_session, username="FelixAdmin", password=PASSWORD, hasher=fast_hasher
            )

        completed = [event for event in events if event["event"] == "bootstrap_completed"]
        assert len(completed) == 1
        assert completed[0]["username_canonical"] == "felixadmin"
        assert completed[0]["role"] == "admin"
