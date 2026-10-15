"""Controller upgrade readiness, against a real database.

The endpoint reports and never acts, which is the whole design: replacing
the controller means replacing the container it is served from, and for the
application to do that to itself it would need the Docker socket — root on
the host. `scripts/upgrade-controller.sh` performs the upgrade; this tells an
administrator what it would involve.

These need a database because every field comes from one: the applied
revision, the connected hosts, the open updates.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

from hcc.models.host import Host
from hcc.models.updates import UpdateJob, UpdateState, UpdateTrigger
from hcc.updates import schema

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable
    from contextlib import AbstractAsyncContextManager

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from hcc.models.user import User

    ApiFactory = Callable[..., AbstractAsyncContextManager[AsyncClient]]
    SeedUser = Callable[..., Awaitable[User]]
    SignIn = Callable[..., Awaitable[str]]

pytestmark = pytest.mark.integration

PATH = "/api/v1/controller/upgrade-readiness"


@pytest.fixture
async def restored_revision(db_session: AsyncSession) -> AsyncGenerator[None]:
    """Put ``alembic_version`` back, whatever the test did to it.

    ``alembic_version`` is deliberately **not** in ``_TRUNCATED_TABLES``:
    emptying it between tests would throw away the marker that says which
    schema the database has. The consequence is that a test writing to it
    writes for the rest of the session — and, where the database outlives
    the session, for everything that runs afterwards.

    That is not hypothetical. Without this, CI's "verify migrations apply to
    an empty database" step failed with *Can't locate revision identified by
    '9999_from_the_future'*, because pytest and that step share one
    ``hcc_test`` database and the test below had left a revision in it that
    Alembic has never heard of. It passed locally the whole time:
    ``scripts/test-integration.sh`` creates a disposable database per run and
    drops it, so the damage never outlived the process.

    A fixture rather than a ``try``/``finally`` in the one test that needs
    it, so the next test to reach for this cannot forget.
    """
    original: str = (
        await db_session.execute(text("SELECT version_num FROM alembic_version"))
    ).scalar_one()
    try:
        yield
    finally:
        await db_session.execute(
            text("UPDATE alembic_version SET version_num = :revision"),
            {"revision": original},
        )
        await db_session.commit()


async def as_viewer(client: AsyncClient, seed_user: SeedUser, sign_in: SignIn) -> None:
    """Sign in as an ordinary viewer.

    Viewer rather than administrator throughout, because "is the controller
    current" is a question anybody looking after an installation may ask and
    the answer holds no secret.
    """
    await seed_user(client, username="viewer")
    await sign_in(client, "viewer")


class TestWhatItReports:
    async def test_a_current_schema_says_so_and_nothing_else(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """The ordinary case, which has to be unremarkable.

        A readiness check that always had something to say would be one
        nobody reads.
        """
        async with api_factory() as client:
            await as_viewer(client, seed_user, sign_in)

            body = (await client.get(PATH)).json()

            assert body["schema_is_current"] is True
            assert body["schema_is_ahead"] is False
            assert body["pending_revisions"] == []
            assert body["advice"] == ["The schema is current and nothing is in flight."]

    async def test_it_names_the_build_and_the_revision(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            await as_viewer(client, seed_user, sign_in)

            body = (await client.get(PATH)).json()

            assert body["controller_version"]
            # The fixture database is migrated to head, so these agree —
            # which is the pair the whole endpoint is about.
            assert body["schema_revision"] == body["head_revision"]

    async def test_a_database_ahead_of_this_build_is_the_loudest_advice(
        self,
        api_factory: ApiFactory,
        db_session: AsyncSession,
        seed_user: SeedUser,
        sign_in: SignIn,
        restored_revision: None,
    ) -> None:
        """An older image against a newer schema.

        The dangerous direction, and the one that looks fine from a count of
        pending migrations: there are none, because this build has never
        heard of the revision the database reports. A backup taken now would
        not recover it either — it would restore the *newer* schema under the
        older image.
        """
        await db_session.execute(
            text("UPDATE alembic_version SET version_num = '9999_from_the_future'")
        )
        await db_session.commit()

        async with api_factory() as client:
            await as_viewer(client, seed_user, sign_in)

            body = (await client.get(PATH)).json()

            assert body["schema_is_ahead"] is True
            assert body["schema_is_current"] is False
            assert "does not contain" in body["advice"][0]
            assert "deploy the newer image again" in body["advice"][0]

    async def test_a_build_that_cannot_read_its_history_says_so(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ "Cannot tell" is not "current", and this is where they diverged.

        With the history unreadable every signal points at "current":
        ``knows_revision`` fails open, ``pending_revisions`` is empty, and
        nothing is in flight. A production image really did answer "the
        schema is current and nothing is in flight" to a database reporting
        a revision from the future, because ``site-packages/alembic`` is a
        directory and the resolution only checked that it was one.

        The resolution is fixed; this is the second line, so a future way of
        failing to read the history cannot produce a reassuring answer.
        """
        monkeypatch.setattr(schema, "CANDIDATE_DIRECTORIES", (Path("/nonexistent-for-this-test"),))
        schema.reset_history_cache()
        try:
            async with api_factory() as client:
                await as_viewer(client, seed_user, sign_in)

                body = (await client.get(PATH)).json()

                assert body["head_revision"] is None
                assert body["schema_is_current"] is False
                assert "cannot read its own migration history" in body["advice"][0]
                assert "packaging fault" in body["advice"][0]
        finally:
            # The caches are process-wide, so leaving them poisoned would
            # make every later test in this session see no history.
            schema.reset_history_cache()

    async def test_it_counts_connected_hosts_and_open_updates(
        self,
        api_factory: ApiFactory,
        db_session: AsyncSession,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        """Both are "choose your moment" information, not blockers.

        Restarting the controller during an update does not break it — the
        agent reports the result on its next connection — and saying so is
        more useful than refusing.
        """
        host = Host(
            name="homeserver",
            name_canonical="homeserver",
            agent_version="0.1.0",
            platform="linux/amd64",
            current_connection_id=uuid.uuid4(),
        )
        db_session.add(host)
        await db_session.flush()
        db_session.add(
            UpdateJob(
                host_id=host.id,
                state=UpdateState.DOWNLOADING.value,
                trigger=UpdateTrigger.MANUAL.value,
                to_version="0.2.0",
                platform="linux/amd64",
                expected_sha256="a" * 64,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        await db_session.commit()

        async with api_factory() as client:
            await as_viewer(client, seed_user, sign_in)

            body = (await client.get(PATH)).json()

            assert body["connected_hosts"] == 1
            assert body["hosts_with_open_updates"] == 1
            assert any("in flight" in line for line in body["advice"])

    async def test_a_finished_update_is_not_in_flight(
        self,
        api_factory: ApiFactory,
        db_session: AsyncSession,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # The count uses `is_terminal`, which is the one place that knows
        # which states are terminal. A second list in SQL would be a copy
        # that drifts, and the drift would read as "an update is stuck".
        host = Host(name="done", name_canonical="done", platform="linux/amd64")
        db_session.add(host)
        await db_session.flush()
        db_session.add(
            UpdateJob(
                host_id=host.id,
                state=UpdateState.SUCCEEDED.value,
                trigger=UpdateTrigger.MANUAL.value,
                to_version="0.2.0",
                platform="linux/amd64",
                expected_sha256="a" * 64,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        await db_session.commit()

        async with api_factory() as client:
            await as_viewer(client, seed_user, sign_in)

            assert (await client.get(PATH)).json()["hosts_with_open_updates"] == 0


class TestItNeverActs:
    async def test_there_is_no_write_method_on_it(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """The property that makes this safe to expose at all.

        Asserted against the running application rather than by reading the
        source: a POST added later would make this fail, which is the point.
        """
        async with api_factory() as client:
            await as_viewer(client, seed_user, sign_in)

            for method in ("post", "put", "patch", "delete"):
                response = await getattr(client, method)(PATH)
                assert response.status_code == 405, (
                    f"{method.upper()} {PATH} answered {response.status_code}; this "
                    "endpoint reports and must never act"
                )

    async def test_an_anonymous_caller_is_refused(self, api_factory: ApiFactory) -> None:
        # The schema revision is not a secret and is not public either: it
        # tells an unauthenticated caller exactly which migrations a
        # deployment has, which is a version fingerprint.
        async with api_factory() as client:
            assert (await client.get(PATH)).status_code == 401
