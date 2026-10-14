"""Shared pytest fixtures.

Test strategy (two tiers):

* **Unit tests** — always run, no external services. The database engine is
  created but never connected (asyncpg connects lazily), so routing, settings,
  error handling and serialisation are all testable without Postgres.
* **Integration tests** — marked ``@pytest.mark.integration`` and run only when
  ``HCC_TEST_DATABASE_URL`` is set. They skip with an explicit reason
  otherwise; CI always sets it via a Postgres service container, so they are
  never quietly skipped where it matters.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from hcc.app import create_app
from hcc.config import Settings
from hcc.models.user import User, UserRole
from hcc.security.password import Argon2Parameters, PasswordHasher

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
    from contextlib import AbstractAsyncContextManager
    from typing import Any

    from fastapi import APIRouter, FastAPI

# Credentials for a throwaway local test database. Not a secret.
_UNREACHABLE_TEST_DSN = "postgresql://hcc:hcc_test@127.0.0.1:1/hcc_test"

# apps/server — the directory holding alembic.ini.
_SERVER_DIR = Path(__file__).resolve().parent.parent

# Tables emptied between integration tests. Listed explicitly rather than
# discovered from metadata, so adding a table is a deliberate decision about
# test isolation instead of a silent change in behaviour. Order is irrelevant
# because the truncation uses CASCADE.
#
# `installation` is included because it is the bootstrap invariant: a leftover
# row would make every later bootstrap test see an already-initialized
# installation, and the suite would pass by refusing to do anything.
#
# `audit_events` is included for the mirror-image reason: the audit assertions
# count and inspect events, so a leftover row from a previous test would make
# one test's writes look like another's. The table has no foreign keys, so
# nothing else removes its rows on CASCADE — it has to be listed explicitly.
#
# `hosts` is listed and its two children are not needed for correctness —
# `host_enrollments` and `agent_credentials` both cascade from it — but they
# are listed anyway, so that a future change which drops the cascade cannot
# silently start leaking credentials between tests.
#
# `global_module_defaults` has no parent at all — it is keyed by module id, not
# by host — so a test that enables a module globally would otherwise leak that
# into every later test in the session, and "modules are off by default" would
# pass or fail depending on test order.
_TRUNCATED_TABLES = (
    "agent_credentials",
    "audit_events",
    "global_module_defaults",
    "host_config_revisions",
    "host_enrollments",
    "host_module_overrides",
    "hosts",
    # The partitioned sample tables are truncated through their parents, which
    # cascades to every partition. `metric_partitions` is *not* truncated: it
    # is the catalogue of tables that physically exist, and emptying it while
    # leaving the partitions in place would make the maintenance job try to
    # create ones that already do.
    "metric_samples_1h",
    "metric_samples_5m",
    "metric_samples_raw",
    "metric_series",
    "installation",
    "sessions",
    # Update management. `update_jobs` cascades to `update_events` and
    # `update_rollout_members` through their foreign keys, and `hosts` above
    # would cascade to all of them — but they are named explicitly because a
    # table whose emptying depends on another table's CASCADE is a table that
    # stops being emptied the moment that relationship changes.
    "host_update_policies",
    "update_events",
    "update_jobs",
    "update_rollout_members",
    "update_rollouts",
    "users",
)

# Cheapest Argon2 parameters the module permits. Integration tests hash and
# verify real passwords, so the shipped 64 MiB defaults would make the suite
# slow for no added coverage — the defaults are exercised in
# tests/test_security_password.py.
_FAST_HASH_SETTINGS = {
    "password_hash_memory_cost": 19456,
    "password_hash_time_cost": 2,
    "password_hash_parallelism": 1,
}


@pytest.fixture
def settings() -> Settings:
    """Settings for unit tests.

    Points at a deliberately unreachable database (port 1): unit tests must
    never depend on Postgres, and a connection attempt should fail loudly
    rather than silently reach a developer's real database.
    """
    return Settings(
        environment="test",
        database_url=_UNREACHABLE_TEST_DSN,
        log_level="WARNING",
        log_format="console",
        cors_origins=("http://localhost:5173",),
        # No background metric maintenance in unit tests. It would open
        # connections no unit test asked for, and its task would outlive the
        # event loop pytest-asyncio tears down — see the NullPool note in
        # `api_settings_factory` for the same class of problem. The job is
        # tested directly in tests/test_metrics_maintenance.py.
        metrics_maintenance_interval_seconds=0,
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    """A FastAPI app built with test settings."""
    return create_app(settings)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """An HTTP client bound to the app via ASGI, exercising the real stack.

    Using ``ASGITransport`` with the lifespan manager means middleware,
    exception handlers and startup/shutdown all run, unlike a bare
    ``TestClient`` on the router.
    """
    transport = ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=transport, base_url="http://test") as async_client,
    ):
        yield async_client


@pytest.fixture(scope="session")
def test_database_url() -> Iterator[str]:
    """The integration-test database URL, or skip the test.

    Skips with a message naming the variable to set, so a skipped integration
    test is self-explanatory.

    Session-scoped so that :func:`migrated_database`, which is expensive, can
    depend on it and run once per test session.
    """
    url = os.environ.get("HCC_TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "HCC_TEST_DATABASE_URL is not set; start Postgres "
            "(`make db-up`) and export it to run integration tests"
        )
    yield url


@pytest.fixture(scope="session")
def migrated_database(test_database_url: str) -> str:
    """Bring the test database up to ``head`` and return its URL.

    Schema is built by running the real Alembic migrations, not by
    ``Base.metadata.create_all``. That matters: the ``updated_at`` trigger
    exists only in the migration, so ``create_all`` would produce a schema
    that differs from the one actually deployed, and the tests would pass
    against something nobody runs.

    Run as a subprocess rather than through Alembic's Python API because
    ``alembic/env.py`` calls ``asyncio.run``, which cannot be nested inside the
    event loop pytest-asyncio has already started.
    """
    # Fixed argv, no shell, no user input.
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_SERVER_DIR,
        env={**os.environ, "HCC_DATABASE_URL": test_database_url},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        # Fail loudly with the migration output; a silent skip here would hide
        # a broken migration behind "no integration tests ran".
        pytest.fail(
            "alembic upgrade head failed, so the schema under test is unknown.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return test_database_url


@pytest.fixture
async def db_session(migrated_database: str) -> AsyncIterator[AsyncSession]:
    """A session against the migrated test database, isolated per test.

    Tests using this fixture commit for real, which is the point: server
    defaults and the ``updated_at`` trigger only take effect on an actual
    write. Isolation therefore comes from truncating afterwards rather than
    from rolling back a wrapping transaction.

    A fresh engine per test with ``NullPool`` avoids binding a pooled
    connection to one test's event loop and then reusing it from another's.
    """
    settings = Settings(
        environment="test",
        database_url=migrated_database,
        log_level="WARNING",
    )
    engine = create_async_engine(settings.sqlalchemy_url(), poolclass=NullPool)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async def truncate() -> None:
        async with engine.begin() as connection:
            for table in _TRUNCATED_TABLES:
                await connection.execute(text(f"TRUNCATE TABLE {table} CASCADE"))

    try:
        # Truncate first as well: a previous crashed run must not leak rows
        # into this one.
        await truncate()
        async with factory() as session:
            yield session
        await truncate()
    finally:
        await engine.dispose()


@pytest.fixture
async def scratch_database(test_database_url: str) -> AsyncIterator[str]:
    """Create an empty database for one test, and drop it afterwards.

    Shared by the migration tests, which are destructive and
    revision-stamped and so must not touch the database the other
    integration tests use, and by the bootstrap CLI tests, which need a
    database with no schema in order to exercise the "migrations not applied"
    path.
    """
    url = make_url(test_database_url)
    scratch_name = f"hcc_migtest_{uuid.uuid4().hex[:12]}"

    # CREATE DATABASE cannot run inside a transaction, hence AUTOCOMMIT.
    admin_url = url.set(drivername="postgresql+asyncpg")
    admin_engine = create_async_engine(admin_url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{scratch_name}"'))
    finally:
        await admin_engine.dispose()

    scratch_url = url.set(database=scratch_name)
    try:
        # Yield the plain postgresql:// form; Settings applies the driver.
        yield scratch_url.render_as_string(hide_password=False)
    finally:
        drop_engine = create_async_engine(
            admin_url, poolclass=NullPool, isolation_level="AUTOCOMMIT"
        )
        try:
            async with drop_engine.connect() as connection:
                await connection.execute(
                    text(f'DROP DATABASE IF EXISTS "{scratch_name}" WITH (FORCE)')
                )
        finally:
            await drop_engine.dispose()


@pytest.fixture
async def db_session_factory(
    migrated_database: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A session *factory* against the migrated test database.

    Distinct from :func:`db_session` because the bootstrap concurrency tests
    need several genuinely independent sessions — separate connections,
    separate transactions — in order to observe the database's own
    serialisation. One shared session could not demonstrate that property, and
    a mock could not demonstrate it at all.

    Truncates before and after, like :func:`db_session`.
    """
    engine = create_async_engine(
        Settings(
            environment="test", database_url=migrated_database, log_level="WARNING"
        ).sqlalchemy_url(),
        poolclass=NullPool,
    )
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async def truncate() -> None:
        async with engine.begin() as connection:
            for table in _TRUNCATED_TABLES:
                await connection.execute(text(f"TRUNCATE TABLE {table} CASCADE"))

    try:
        await truncate()
        yield factory
        await truncate()
    finally:
        await engine.dispose()


@pytest.fixture
def fast_hasher() -> PasswordHasher:
    """A hasher at the cheapest permitted parameters.

    Bootstrap tests hash a real password; the shipped 64 MiB defaults would
    make them slow for no added coverage, and the defaults are exercised in
    tests/test_security_password.py.
    """
    return PasswordHasher(
        Argon2Parameters(
            memory_cost=_FAST_HASH_SETTINGS["password_hash_memory_cost"],
            time_cost=_FAST_HASH_SETTINGS["password_hash_time_cost"],
            parallelism=_FAST_HASH_SETTINGS["password_hash_parallelism"],
        )
    )


# ---------------------------------------------------------------------------
# HTTP integration against the real database
#
# The `app`/`client` fixtures above point at a deliberately unreachable
# database, which is right for unit tests. Authentication tests need the real
# thing, so these build a second app wired to the migrated test database.
# ---------------------------------------------------------------------------


@pytest.fixture
def api_settings_factory(
    migrated_database: str,
) -> Callable[..., Settings]:
    """Build integration settings, overriding individual fields.

    A factory rather than a fixture value because tests need to vary the
    configuration under test — a low rate limit, a short session lifetime,
    production cookie flags — and each variation needs its own app.
    """

    def build(**overrides: object) -> Settings:
        values: dict[str, object] = {
            "environment": "test",
            "database_url": migrated_database,
            "log_level": "WARNING",
            # Generous by default so that tests which are not about
            # throttling never trip it. The throttling tests lower it.
            # `per_username` stays above `per_address_username`, which
            # configuration validation requires — see
            # hcc/security/ratelimit.py for why that ordering is load-bearing.
            "login_rate_limit_per_address": 1000,
            "login_rate_limit_per_address_username": 1000,
            "login_rate_limit_per_username": 2000,
            # NullPool, so every connection is closed when its session closes
            # — inside the test, while the event loop is still alive. With a
            # QueuePool, idle connections are closed by `engine.dispose()`
            # asynchronously at teardown, and pytest-asyncio tears the loop
            # down first; the sockets were then finalised by the garbage
            # collector, producing an intermittent "unclosed transport"
            # unraisable warning that `filterwarnings = ["error"]` turned into
            # a failure in an unrelated test.
            "database_pool_class": "null",
            # Off by default for the same reason as the unit-test `settings`
            # fixture: a periodic task on every integration app is a second
            # user of the connection pool that no test accounted for, and its
            # cancellation races the loop teardown. Tests that want the job
            # pass the interval explicitly.
            "metrics_maintenance_interval_seconds": 0,
            **_FAST_HASH_SETTINGS,
        }
        values.update(overrides)
        return Settings(**values)  # type: ignore[arg-type]

    return build


@pytest.fixture
async def api_factory(
    api_settings_factory: Callable[..., Settings],
) -> AsyncIterator[Callable[..., AbstractAsyncContextManager[AsyncClient]]]:
    """Yield a factory producing HTTP clients against the real database.

    Each client runs the app's full stack — middleware, exception handlers,
    lifespan — so cookie attributes and security headers are the real ones.
    Tables are truncated after the fixture finishes, once per test.
    """
    truncate_url: str | None = None

    @asynccontextmanager
    async def build(
        *,
        routers: Sequence[APIRouter] = (),
        client_address: str = "127.0.0.1",
        **overrides: object,
    ) -> AsyncIterator[AsyncClient]:
        nonlocal truncate_url
        settings = api_settings_factory(**overrides)
        truncate_url = settings.sqlalchemy_url()
        app = create_app(settings)
        # `routers` mounts test-only endpoints. Role-gated routes have no
        # production call site yet — user management is a later milestone — so
        # the authorization dependencies are exercised against synthetic
        # routes rather than by inventing product endpoints for the tests to
        # lean on.
        for extra in routers:
            app.include_router(extra, prefix=f"{settings.api_prefix}/v1")
        # `client_address` is the transport-level peer — the one value a
        # caller cannot forge. Varying it is how the proxy-trust tests
        # distinguish "request arrived from the declared proxy" from "request
        # arrived from somewhere else claiming to be forwarded".
        transport = ASGITransport(app=app, client=(client_address, 51234))
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=transport, base_url="http://testserver") as client,
        ):
            # Handy for tests that need to reach past HTTP, e.g. to seed a
            # user or to age a session row.
            client.app_state = app.state  # type: ignore[attr-defined]
            yield client

    try:
        yield build
    finally:
        if truncate_url is not None:
            engine = create_async_engine(truncate_url, poolclass=NullPool)
            try:
                async with engine.begin() as connection:
                    for table in _TRUNCATED_TABLES:
                        await connection.execute(text(f"TRUNCATE TABLE {table} CASCADE"))
            finally:
                await engine.dispose()


# ---------------------------------------------------------------------------
# Shared helpers for the API integration suites (milestone 1.7)
#
# Exposed as fixtures returning callables rather than as module-level
# functions, so the new suites share one implementation without importing
# across test modules — which `tests/` cannot do reliably, having no
# `__init__.py`.
#
# `tests/test_api_authorization.py` predates these and keeps its own copies.
# Left alone deliberately: it is the default-deny regression suite, and
# rewriting its scaffolding to save a few lines would risk the one file whose
# job is to notice when something stops being protected.
# ---------------------------------------------------------------------------

#: Password for seeded accounts. Satisfies the policy and is not a credential
#: for anything — the database it reaches exists for one test.
TEST_PASSWORD = "correct horse battery staple"


@pytest.fixture
def seed_user() -> Callable[..., Awaitable[User]]:
    """Insert a user directly, bypassing the API.

    Deliberately not "create a user through the endpoint": a test of the
    create endpoint cannot use the create endpoint to arrange its fixtures,
    and a test of the *list* endpoint should not fail because creation is
    broken. Hashing uses the app's own hasher, so the account can really sign
    in.
    """

    async def insert(
        client: AsyncClient,
        *,
        username: str,
        role: UserRole = UserRole.VIEWER,
        is_active: bool = True,
        display_name: str | None = None,
        password: str = TEST_PASSWORD,
    ) -> User:
        hasher = client.app_state.password_hasher  # type: ignore[attr-defined]
        async with client.app_state.database.session_factory() as db:  # type: ignore[attr-defined]
            user = User(
                username=username,
                display_name=display_name,
                password_hash=hasher.hash(password),
                role=role,
                is_active=is_active,
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)
            return user

    return insert


@pytest.fixture
def sign_in() -> Callable[..., Awaitable[str]]:
    """Log in through the real endpoint and return the CSRF token.

    The real endpoint, not a forged cookie: the tests that follow exercise
    CSRF-protected mutations, and a hand-made session row would not carry a
    CSRF token the client could present.
    """

    async def login(client: AsyncClient, username: str, password: str = TEST_PASSWORD) -> str:
        response = await client.post(
            "/api/v1/auth/login", json={"username": username, "password": password}
        )
        assert response.status_code == 200, response.text
        token: str = response.json()["csrf_token"]
        return token

    return login


@pytest.fixture
def read_sql() -> Callable[..., Awaitable[Sequence[Any]]]:
    """Run one read statement outside the API and return the rows.

    For the states an HTTP client cannot reach or should not be trusted to
    report — counting audit rows, checking a revocation really marked the
    row — so an assertion about persistence is made against the database
    rather than against the response that claimed it.
    """

    async def query(client: AsyncClient, statement: str) -> Sequence[Any]:
        async with client.app_state.database.session_factory() as db:  # type: ignore[attr-defined]
            rows: Sequence[Any] = (await db.execute(text(statement))).all()
            return rows

    return query
