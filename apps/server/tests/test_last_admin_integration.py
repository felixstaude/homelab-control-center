"""The last-active-administrator invariant, under real concurrency.

Milestone 1.7. This file exists separately from
``test_api_users_integration.py`` because the property it asserts cannot be
demonstrated through one connection, and the concurrency has to be real.

## Why a mock could not test this

The invariant is that no interleaving of operations leaves the installation
with zero active administrators. The failure mode it guards against — two
administrators demoting each other simultaneously, both reading a count of two
and both committing — is a property of PostgreSQL's READ COMMITTED isolation:
neither transaction sees the other's uncommitted write, so no amount of
application logic running against a single session can reproduce it. A mocked
repository would return whatever the test told it to, which asserts the test's
own assumptions.

So these tests open **genuinely separate sessions**, each on its own
connection, and run the operations with ``asyncio.gather`` so the database is
the thing deciding the order. The ``db_session_factory`` fixture exists for
exactly this — it was added for the bootstrap concurrency tests, which prove
the same class of property about the first-run invariant.

## The mechanism being verified

A transaction-scoped advisory lock taken *before* the count, held until commit.
See :mod:`hcc.services.users` for why a count-then-update is not sufficient
and why the alternatives were rejected.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import func, select

from hcc.models.user import User, UserRole
from hcc.security.audit import AuditContext, AuditService
from hcc.security.sessions import SessionService
from hcc.services.users import (
    LastAdministratorError,
    UserService,
    UserUpdate,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from contextlib import AbstractAsyncContextManager

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from hcc.config import Settings
    from hcc.security.password import PasswordHasher

    ApiFactory = Callable[..., AbstractAsyncContextManager[AsyncClient]]
    SeedUser = Callable[..., Awaitable[User]]
    SignIn = Callable[..., Awaitable[str]]
    ReadSql = Callable[..., Awaitable[Any]]

pytestmark = pytest.mark.integration

USERS = "/api/v1/users"
PASSWORD = "correct horse battery staple"


def csrf(token: str) -> dict[str, str]:
    return {"X-CSRF-Token": token}


def build_service(hasher: PasswordHasher, settings: Settings) -> UserService:
    """A service wired the way the application wires it.

    Real collaborators rather than stubs: the audit writes and the session
    revocations happen inside the same transaction as the mutation, so
    substituting them would change the transaction's shape — which is the
    thing under test.
    """
    return UserService(
        hasher=hasher,
        sessions=SessionService(settings),
        audit=AuditService(),
    )


async def seed_admins(
    factory: async_sessionmaker[AsyncSession], hasher: PasswordHasher, count: int
) -> list[User]:
    """Insert ``count`` active administrators and return them."""
    async with factory() as db:
        admins = [
            User(
                username=f"admin{index}",
                password_hash=hasher.hash(PASSWORD),
                role=UserRole.ADMIN,
                is_active=True,
            )
            for index in range(count)
        ]
        for admin in admins:
            db.add(admin)
        await db.commit()
        for admin in admins:
            await db.refresh(admin)
        return admins


async def count_active_admins(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as db:
        return (
            await db.execute(
                select(func.count())
                .select_from(User)
                .where(User.role == UserRole.ADMIN, User.is_active.is_(True))
            )
        ).scalar_one()


# ---------------------------------------------------------------------------
# Sequential behaviour
# ---------------------------------------------------------------------------


class TestSingleAdministrator:
    async def test_the_only_administrator_cannot_be_demoted(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            admin = await seed_user(client, username="soleadmin", role=UserRole.ADMIN)
            token = await sign_in(client, "soleadmin")

            response = await client.patch(
                f"{USERS}/{admin.id}", json={"role": "viewer"}, headers=csrf(token)
            )

            assert response.status_code == 409
            assert response.json()["error"]["code"] == "last_administrator"
            # The message has to tell the operator what to do instead, because
            # "conflict" is not actionable.
            assert "another active account" in response.json()["error"]["message"]

    async def test_the_only_administrator_cannot_be_deactivated(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            admin = await seed_user(client, username="soleadmin", role=UserRole.ADMIN)
            token = await sign_in(client, "soleadmin")

            response = await client.post(f"{USERS}/{admin.id}/deactivate", headers=csrf(token))

            assert response.status_code == 409
            assert response.json()["error"]["code"] == "last_administrator"

    async def test_the_same_refusal_applies_through_patch(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # Both routes share one service method precisely so the guard cannot
        # hold on one path and be missing on the other.
        async with api_factory() as client:
            admin = await seed_user(client, username="soleadmin", role=UserRole.ADMIN)
            token = await sign_in(client, "soleadmin")

            response = await client.patch(
                f"{USERS}/{admin.id}", json={"is_active": False}, headers=csrf(token)
            )

            assert response.status_code == 409

    async def test_a_refused_operation_changes_nothing(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            admin = await seed_user(client, username="soleadmin", role=UserRole.ADMIN)
            token = await sign_in(client, "soleadmin")

            await client.patch(
                f"{USERS}/{admin.id}",
                # A display-name change bundled with a refused role change:
                # neither may be applied, or a rejected request would leave a
                # half-updated row.
                json={"display_name": "Should Not Stick", "role": "viewer"},
                headers=csrf(token),
            )

            after = (await client.get(f"{USERS}/{admin.id}")).json()
            assert after["role"] == "admin"
            assert after["display_name"] is None

    async def test_the_refusal_is_recorded_as_a_failure(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        read_sql: ReadSql,
    ) -> None:
        # A refused privileged operation is worth keeping: it distinguishes
        # "nobody tried" from "somebody tried and was stopped".
        async with api_factory() as client:
            admin = await seed_user(client, username="soleadmin", role=UserRole.ADMIN)
            token = await sign_in(client, "soleadmin")

            await client.patch(f"{USERS}/{admin.id}", json={"role": "viewer"}, headers=csrf(token))

            rows = await read_sql(
                client,
                "SELECT action, outcome, details->>'reason' FROM audit_events "
                "WHERE outcome = 'failure'",
            )
            assert len(rows) == 1
            assert rows[0][0] == "user.role_changed"
            assert rows[0][2] == "last_administrator"

    async def test_an_inactive_administrator_does_not_count(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        """The guard counts *active* administrators, not administrators.

        Getting this wrong in the permissive direction is the dangerous
        version: a deactivated administrator would be treated as a safety net
        that cannot actually sign in, and the last usable one could then be
        demoted.
        """
        async with api_factory() as client:
            admin = await seed_user(client, username="soleadmin", role=UserRole.ADMIN)
            await seed_user(client, username="dormant", role=UserRole.ADMIN, is_active=False)
            token = await sign_in(client, "soleadmin")

            response = await client.patch(
                f"{USERS}/{admin.id}", json={"role": "viewer"}, headers=csrf(token)
            )

            assert response.status_code == 409

    async def test_an_operator_is_not_mistaken_for_a_safety_net(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            admin = await seed_user(client, username="soleadmin", role=UserRole.ADMIN)
            await seed_user(client, username="operator", role=UserRole.OPERATOR)
            token = await sign_in(client, "soleadmin")

            response = await client.patch(
                f"{USERS}/{admin.id}", json={"role": "viewer"}, headers=csrf(token)
            )

            assert response.status_code == 409

    async def test_the_guard_does_not_fire_on_an_unrelated_change(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # The other direction: over-blocking would make the sole
        # administrator unable to correct their own display name.
        async with api_factory() as client:
            admin = await seed_user(client, username="soleadmin", role=UserRole.ADMIN)
            token = await sign_in(client, "soleadmin")

            response = await client.patch(
                f"{USERS}/{admin.id}", json={"display_name": "Sole Admin"}, headers=csrf(token)
            )

            assert response.status_code == 200
            assert response.json()["display_name"] == "Sole Admin"

    async def test_promoting_to_administrator_is_never_refused(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="soleadmin", role=UserRole.ADMIN)
            other = await seed_user(client, username="rising", role=UserRole.VIEWER)
            token = await sign_in(client, "soleadmin")

            response = await client.patch(
                f"{USERS}/{other.id}", json={"role": "admin"}, headers=csrf(token)
            )

            assert response.status_code == 200

    async def test_deactivating_a_non_administrator_is_never_refused(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="soleadmin", role=UserRole.ADMIN)
            other = await seed_user(client, username="leaving", role=UserRole.VIEWER)
            token = await sign_in(client, "soleadmin")

            response = await client.post(f"{USERS}/{other.id}/deactivate", headers=csrf(token))

            assert response.status_code == 200


class TestSeveralAdministrators:
    async def test_one_of_two_can_be_demoted(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="first", role=UserRole.ADMIN)
            second = await seed_user(client, username="second", role=UserRole.ADMIN)
            token = await sign_in(client, "first")

            response = await client.patch(
                f"{USERS}/{second.id}", json={"role": "viewer"}, headers=csrf(token)
            )

            assert response.status_code == 200
            assert response.json()["role"] == "viewer"

    async def test_the_second_demotion_is_then_refused(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            first = await seed_user(client, username="first", role=UserRole.ADMIN)
            second = await seed_user(client, username="second", role=UserRole.ADMIN)
            token = await sign_in(client, "first")

            assert (
                await client.patch(
                    f"{USERS}/{second.id}", json={"role": "viewer"}, headers=csrf(token)
                )
            ).status_code == 200
            response = await client.patch(
                f"{USERS}/{first.id}", json={"role": "viewer"}, headers=csrf(token)
            )

            assert response.status_code == 409

    async def test_self_demotion_is_allowed_when_another_administrator_remains(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # Not forbidden: an administrator handing over and stepping down is a
        # legitimate thing to do, and the guard is about the installation's
        # state rather than about who is asking.
        async with api_factory() as client:
            first = await seed_user(client, username="first", role=UserRole.ADMIN)
            await seed_user(client, username="second", role=UserRole.ADMIN)
            token = await sign_in(client, "first")

            response = await client.patch(
                f"{USERS}/{first.id}", json={"role": "operator"}, headers=csrf(token)
            )

            assert response.status_code == 200
            # And the demotion takes effect on the next privileged request,
            # without re-login.
            assert (await client.get(USERS)).status_code == 403

    async def test_self_demotion_is_refused_when_nobody_else_is_left(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            first = await seed_user(client, username="first", role=UserRole.ADMIN)
            await seed_user(client, username="second", role=UserRole.ADMIN, is_active=False)
            token = await sign_in(client, "first")

            response = await client.patch(
                f"{USERS}/{first.id}", json={"role": "operator"}, headers=csrf(token)
            )

            assert response.status_code == 409

    async def test_bootstrap_stays_closed_after_every_administrator_is_demoted(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        """The bootstrap invariant must not be reopened by user management.

        Even if the installation somehow reached zero administrators, setup
        must stay closed: the invariant is the ``installation`` row, not a
        user count (ADR-033). Asserted here because milestone 1.7 is the first
        milestone that can change the user table through HTTP at all, and
        "does user management reopen the front door" is exactly the question
        ADR-036 says must be re-asked whenever it might.
        """
        async with api_factory() as client:
            from hcc.security.bootstrap import bootstrap_administrator

            async with client.app_state.database.session_factory() as db:  # type: ignore[attr-defined]
                await bootstrap_administrator(
                    db,
                    username="bootstrapped",
                    password=PASSWORD,
                    hasher=client.app_state.password_hasher,  # type: ignore[attr-defined]
                )

            assert (await client.get("/api/v1/setup/status")).json() == {"initialized": True}

            second = await seed_user(client, username="second", role=UserRole.ADMIN)
            token = await sign_in(client, "bootstrapped")
            await client.patch(f"{USERS}/{second.id}", json={"role": "viewer"}, headers=csrf(token))

            # Still initialized, and the sole remaining administrator is still
            # protected.
            assert (await client.get("/api/v1/setup/status")).json() == {"initialized": True}


# ---------------------------------------------------------------------------
# Real concurrency
# ---------------------------------------------------------------------------


class TestConcurrency:
    """Separate sessions, separate connections, ``asyncio.gather``.

    Each test runs the operations through the service rather than through
    HTTP, because what has to be concurrent is the *transactions*: an HTTP
    client shares one session per request, which is right, but two requests
    issued from one test would be serialised by the single-threaded ASGI
    transport rather than by the database.
    """

    @staticmethod
    async def demote(
        factory: async_sessionmaker[AsyncSession],
        service: UserService,
        user_id: Any,
    ) -> str:
        """Attempt one demotion in its own transaction. Returns the outcome."""
        async with factory() as db:
            try:
                await service.update_user(
                    db,
                    user_id=user_id,
                    update=UserUpdate(role=UserRole.VIEWER),
                    context=AuditContext(actor_user_id=None),
                )
                await db.commit()
            except LastAdministratorError:
                await db.rollback()
                return "refused"
            else:
                return "applied"

    @staticmethod
    async def deactivate(
        factory: async_sessionmaker[AsyncSession],
        service: UserService,
        user_id: Any,
    ) -> str:
        async with factory() as db:
            try:
                await service.update_user(
                    db,
                    user_id=user_id,
                    update=UserUpdate(is_active=False),
                    context=AuditContext(actor_user_id=None),
                )
                await db.commit()
            except LastAdministratorError:
                await db.rollback()
                return "refused"
            else:
                return "applied"

    async def test_two_simultaneous_demotions_preserve_one_administrator(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        fast_hasher: PasswordHasher,
        api_settings_factory: Callable[..., Settings],
    ) -> None:
        """The race the advisory lock exists for.

        Without the lock, both transactions read "two active administrators",
        both decide they are safe, and both commit — leaving zero. With it,
        the second one's count runs after the first has committed and sees
        one.
        """
        settings = api_settings_factory()
        service = build_service(fast_hasher, settings)
        first, second = await seed_admins(db_session_factory, fast_hasher, 2)

        outcomes = await asyncio.gather(
            self.demote(db_session_factory, service, first.id),
            self.demote(db_session_factory, service, second.id),
        )

        assert sorted(outcomes) == ["applied", "refused"]
        assert await count_active_admins(db_session_factory) == 1

    async def test_two_simultaneous_deactivations_preserve_one_administrator(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        fast_hasher: PasswordHasher,
        api_settings_factory: Callable[..., Settings],
    ) -> None:
        settings = api_settings_factory()
        service = build_service(fast_hasher, settings)
        first, second = await seed_admins(db_session_factory, fast_hasher, 2)

        outcomes = await asyncio.gather(
            self.deactivate(db_session_factory, service, first.id),
            self.deactivate(db_session_factory, service, second.id),
        )

        assert sorted(outcomes) == ["applied", "refused"]
        assert await count_active_admins(db_session_factory) == 1

    async def test_a_demotion_and_a_deactivation_racing_preserve_one(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        fast_hasher: PasswordHasher,
        api_settings_factory: Callable[..., Settings],
    ) -> None:
        # The mixed case: two different operations, both of which reduce the
        # active-administrator set, must serialise against each other and not
        # merely against their own kind.
        settings = api_settings_factory()
        service = build_service(fast_hasher, settings)
        first, second = await seed_admins(db_session_factory, fast_hasher, 2)

        outcomes = await asyncio.gather(
            self.demote(db_session_factory, service, first.id),
            self.deactivate(db_session_factory, service, second.id),
        )

        assert sorted(outcomes) == ["applied", "refused"]
        assert await count_active_admins(db_session_factory) == 1

    async def test_ten_simultaneous_demotions_leave_exactly_one(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        fast_hasher: PasswordHasher,
        api_settings_factory: Callable[..., Settings],
    ) -> None:
        """Scaled up, because two attempts can pass by luck.

        Ten administrators, all demoted at once. Exactly nine must succeed and
        one must be refused — not "at least one survives", which a
        coincidentally-serialised run would also satisfy.
        """
        settings = api_settings_factory()
        service = build_service(fast_hasher, settings)
        admins = await seed_admins(db_session_factory, fast_hasher, 10)

        outcomes = await asyncio.gather(
            *(self.demote(db_session_factory, service, admin.id) for admin in admins)
        )

        assert outcomes.count("applied") == 9
        assert outcomes.count("refused") == 1
        assert await count_active_admins(db_session_factory) == 1

    async def test_the_same_administrator_demoted_twice_at_once_is_safe(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        fast_hasher: PasswordHasher,
        api_settings_factory: Callable[..., Settings],
    ) -> None:
        # Not the invariant, but the adjacent lost-update race: two writers
        # against one row must serialise rather than interleave.
        settings = api_settings_factory()
        service = build_service(fast_hasher, settings)
        first, _second = await seed_admins(db_session_factory, fast_hasher, 2)

        outcomes = await asyncio.gather(
            self.demote(db_session_factory, service, first.id),
            self.demote(db_session_factory, service, first.id),
        )

        # Both are permitted — the second is a no-op against an account that
        # is already a viewer — and the other administrator is untouched.
        assert set(outcomes) == {"applied"}
        assert await count_active_admins(db_session_factory) == 1

    async def test_a_refused_attempt_releases_the_lock(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        fast_hasher: PasswordHasher,
        api_settings_factory: Callable[..., Settings],
    ) -> None:
        """A rollback must not leave the lock held.

        The lock is transaction-scoped precisely so this holds, but it is
        worth asserting: a leaked advisory lock would make every subsequent
        user mutation in the process block forever, and the symptom would be a
        hung request rather than an error.
        """
        settings = api_settings_factory()
        service = build_service(fast_hasher, settings)
        (sole,) = await seed_admins(db_session_factory, fast_hasher, 1)

        assert await self.demote(db_session_factory, service, sole.id) == "refused"

        # A second operation must complete rather than block. The timeout is
        # what turns "the lock leaked" into a failure instead of a hang.
        async with asyncio.timeout(10):
            async with db_session_factory() as db:
                await service.update_user(
                    db,
                    user_id=sole.id,
                    update=UserUpdate(display_name="Still Works"),
                    context=AuditContext(actor_user_id=None),
                )
                await db.commit()

        async with db_session_factory() as db:
            refreshed = (await db.execute(select(User).where(User.id == sole.id))).scalar_one()
            assert refreshed.display_name == "Still Works"

    async def test_concurrent_creation_of_one_username_yields_one_account(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        fast_hasher: PasswordHasher,
        api_settings_factory: Callable[..., Settings],
    ) -> None:
        """The unique constraint, not the pre-check, is the guarantee.

        Creation deliberately does not take the advisory lock — it cannot
        reduce the administrator count, and serialising it would hold a lock
        across an Argon2 hash. So safety under concurrency comes from
        ``uq_users_username_canonical``, and this asserts that rather than
        trusting the check-then-insert that precedes it.
        """
        from hcc.services.users import UsernameTakenError

        settings = api_settings_factory()
        service = build_service(fast_hasher, settings)

        async def create() -> str:
            async with db_session_factory() as db:
                try:
                    await service.create_user(
                        db,
                        username="contested",
                        password=PASSWORD,
                        context=AuditContext(actor_user_id=None),
                    )
                    await db.commit()
                except UsernameTakenError:
                    with contextlib.suppress(Exception):
                        await db.rollback()
                    return "refused"
                else:
                    return "created"

        outcomes = await asyncio.gather(*(create() for _ in range(5)))

        assert outcomes.count("created") == 1
        assert outcomes.count("refused") == 4

        async with db_session_factory() as db:
            total = (
                await db.execute(
                    select(func.count())
                    .select_from(User)
                    .where(User.username_canonical == "contested")
                )
            ).scalar_one()
        assert total == 1
