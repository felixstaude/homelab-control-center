"""Host and enrollment invariants under genuine concurrency.

Milestone 1.8. A separate file because these need **independent connections**
— separate transactions, separate snapshots — in order to observe PostgreSQL's
own serialisation. One shared session could not demonstrate the properties at
all, and a mock could not demonstrate them even in principle: what is being
tested is that two transactions racing produce one outcome, which is a fact
about the database and the statements, not about the Python.

Each of these races fails in a specific, silent way if the code is written the
obvious way instead:

* **Two enrollment credentials for one host.** The administrator sees the new
  one on screen while an older one still enrolls — so "the enrollment
  credential" is ambiguous, and revoking what is displayed leaves the other
  live.
* **Two agent credentials for one host.** Revoking "the" credential reports
  success and leaves the other working: a revocation that does not revoke.
* **Two enrolled identities from one credential.** The duplicate-identity
  failure ``docs/AGENT_ARCHITECTURE.md`` §4 forbids, which is reachable with
  no attacker at all — just two agents started by the same Ansible run.
* **Two hosts with the same name.** Two rows that render identically, so an
  operator cannot tell which one their agent enrolled against.

The mechanisms being exercised are a per-host advisory lock (ADR-044), a
partial unique index, and a conditional ``UPDATE``. The division of labour
matters: the index and the conditional updates are what make the outcome
*correct*, and the lock is what makes it *deterministic* rather than a
constraint violation somebody has to retry.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

from hcc.models.host import Host, HostLifecycle
from hcc.security.audit import AuditContext, AuditService
from hcc.services.enrollment import AgentEnrollmentService, EnrollmentRefusedError
from hcc.services.hosts import HostNameTakenError, HostService, NoLiveCredentialError

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration

CONTEXT = AuditContext(actor_user_id=None, request_id="concurrency-test")


def host_service() -> HostService:
    return HostService(audit=AuditService())


def enrollment_service() -> AgentEnrollmentService:
    return AgentEnrollmentService(audit=AuditService())


async def seed_host(
    factory: async_sessionmaker[AsyncSession], name: str = "homeserver"
) -> uuid.UUID:
    async with factory() as db:
        host = Host(name=name)
        db.add(host)
        await db.commit()
        return host.id


async def count(factory: async_sessionmaker[AsyncSession], statement: str) -> int:
    async with factory() as db:
        value: int = (await db.execute(text(statement))).scalar_one()
        return value


# ---------------------------------------------------------------------------
# Issuing enrollment credentials
# ---------------------------------------------------------------------------


async def test_concurrent_issuance_leaves_one_pending_credential(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Five administrators clicking at once produce one usable credential.

    Not five, and not a constraint violation: the per-host lock serialises
    them, so each issuance supersedes the last and the final state is
    unambiguous.
    """
    host_id = await seed_host(db_session_factory)
    hosts = host_service()

    async def issue() -> None:
        async with db_session_factory() as db:
            await hosts.issue_enrollment(db, host_id=host_id, context=CONTEXT)
            await db.commit()

    await asyncio.gather(*(issue() for _ in range(5)))

    pending = await count(
        db_session_factory,
        "SELECT count(*) FROM host_enrollments WHERE consumed_at IS NULL AND revoked_at IS NULL",
    )
    total = await count(db_session_factory, "SELECT count(*) FROM host_enrollments")

    assert pending == 1
    # The superseded rows are kept, so "issued, then revoked unused" stays
    # answerable rather than the evidence being deleted.
    assert total == 5


async def test_two_hosts_do_not_wait_on_each_other(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The lock is per host, which is the point of ADR-044.

    A global lock would be correct and would serialise the whole fleet behind
    one mutex at exactly the moment it is busiest — an agent fleet reconnecting
    after a controller restart. Asserted by holding one host's lock for a
    measurable time and requiring another host's issuance to complete anyway.
    """
    first = await seed_host(db_session_factory, "alpha")
    second = await seed_host(db_session_factory, "beta")
    hosts = host_service()
    released = asyncio.Event()

    async def hold_first() -> None:
        async with db_session_factory() as db:
            await hosts.issue_enrollment(db, host_id=first, context=CONTEXT)
            # The lock is transaction-scoped, so it is held until this commits.
            await asyncio.sleep(0.5)
            await db.commit()
            released.set()

    async def issue_second() -> float:
        # Start after the first has taken its lock.
        await asyncio.sleep(0.1)
        started = time.monotonic()
        async with db_session_factory() as db:
            await hosts.issue_enrollment(db, host_id=second, context=CONTEXT)
            await db.commit()
        return time.monotonic() - started

    _, elapsed = await asyncio.gather(hold_first(), issue_second())

    assert released.is_set()
    # Comfortably less than the 0.4s still remaining on the other host's lock.
    assert elapsed < 0.3, f"issuance waited {elapsed:.3f}s on an unrelated host's lock"


# ---------------------------------------------------------------------------
# Spending an enrollment credential
# ---------------------------------------------------------------------------


async def test_one_credential_concurrently_spent_enrolls_once(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Single use, decided by the conditional UPDATE.

    The race a ``SELECT`` followed by an ``UPDATE`` loses: both readers see
    the credential as unused, both proceed, and the host ends with two
    identities and every check passed.
    """
    host_id = await seed_host(db_session_factory)
    hosts = host_service()
    enrollment = enrollment_service()

    async with db_session_factory() as db:
        issued = await hosts.issue_enrollment(db, host_id=host_id, context=CONTEXT)
        credential = issued.credential
        await db.commit()

    async def enroll() -> str | None:
        async with db_session_factory() as db:
            try:
                enrolled = await enrollment.enroll(db, credential=credential)
            except EnrollmentRefusedError:
                return None
            await db.commit()
            return enrolled.credential

    results = await asyncio.gather(*(enroll() for _ in range(5)))
    succeeded = [result for result in results if result is not None]

    assert len(succeeded) == 1, results
    assert (
        await count(
            db_session_factory,
            "SELECT count(*) FROM agent_credentials WHERE revoked_at IS NULL",
        )
        == 1
    )


async def test_two_credentials_for_one_host_still_leave_one_identity(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Belt and braces for the case issuance is supposed to prevent.

    ``issue_enrollment`` revokes the previous pending credential, so two live
    credentials should not exist — but if one ever did, the outcome must still
    be one identity rather than a partial unique index violation surfacing as
    a 500. Arranged by inserting the second credential past the service.
    """
    host_id = await seed_host(db_session_factory)
    hosts = host_service()
    enrollment = enrollment_service()

    async with db_session_factory() as db:
        first = await hosts.issue_enrollment(db, host_id=host_id, context=CONTEXT)
        await db.commit()
    async with db_session_factory() as db:
        # Un-revoke the first, so both are live at once — the state the
        # service refuses to create.
        second = await hosts.issue_enrollment(db, host_id=host_id, context=CONTEXT)
        await db.execute(text("UPDATE host_enrollments SET revoked_at = NULL"))
        await db.commit()

    async def enroll(credential: str) -> str | None:
        async with db_session_factory() as db:
            try:
                enrolled = await enrollment.enroll(db, credential=credential)
            except EnrollmentRefusedError:
                return None
            await db.commit()
            return enrolled.credential

    results = await asyncio.gather(enroll(first.credential), enroll(second.credential))

    # Both credentials were legitimately live, so both may enroll — but the
    # second revokes the first's identity, leaving exactly one usable.
    assert any(result is not None for result in results)
    assert (
        await count(
            db_session_factory,
            "SELECT count(*) FROM agent_credentials WHERE revoked_at IS NULL",
        )
        == 1
    )


async def test_concurrent_authentication_does_not_lose_the_connection_epoch(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Every accepted connection must advance the epoch exactly once.

    A lost update here would give two connections the same epoch, and the
    guard that stops a superseded socket clearing a newer connection's state
    would silently stop working.
    """
    host_id = await seed_host(db_session_factory)
    hosts = host_service()
    enrollment = enrollment_service()

    async with db_session_factory() as db:
        issued = await hosts.issue_enrollment(db, host_id=host_id, context=CONTEXT)
        await db.commit()
    async with db_session_factory() as db:
        enrolled = await enrollment.enroll(db, credential=issued.credential)
        agent_credential = enrolled.credential
        await db.commit()

    async def authenticate() -> int:
        async with db_session_factory() as db:
            agent = await enrollment.authenticate(db, credential=agent_credential)
            await db.commit()
            return agent.epoch

    epochs = await asyncio.gather(*(authenticate() for _ in range(4)))

    assert sorted(epochs) == [1, 2, 3, 4], epochs
    final = await count(
        db_session_factory, f"SELECT connection_epoch FROM hosts WHERE id = '{host_id}'"
    )
    assert final == 4


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


async def test_concurrent_revocation_succeeds_once_and_refuses_the_rest(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ "Revoked" and "there was nothing to revoke" stay distinguishable.

    If both callers reported success, two operators would each believe they
    had been the one to secure the host — and neither would look again.
    """
    host_id = await seed_host(db_session_factory)
    hosts = host_service()
    enrollment = enrollment_service()

    async with db_session_factory() as db:
        issued = await hosts.issue_enrollment(db, host_id=host_id, context=CONTEXT)
        await db.commit()
    async with db_session_factory() as db:
        await enrollment.enroll(db, credential=issued.credential)
        await db.commit()

    async def revoke() -> bool:
        async with db_session_factory() as db:
            try:
                await hosts.revoke_credential(db, host_id=host_id, context=CONTEXT)
            except NoLiveCredentialError:
                return False
            await db.commit()
            return True

    outcomes = await asyncio.gather(*(revoke() for _ in range(4)))

    assert outcomes.count(True) == 1, outcomes
    async with db_session_factory() as db:
        lifecycle: str = (
            await db.execute(text(f"SELECT lifecycle FROM hosts WHERE id = '{host_id}'"))
        ).scalar_one()
    assert lifecycle == HostLifecycle.REVOKED.value


async def test_revoking_while_enrolling_leaves_a_consistent_state(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Whichever order they resolve in, the host must not end up both
    ``revoked`` and holding a live credential, or ``enrolled`` with none.

    The per-host lock is what makes this true: both operations take it, so one
    completes before the other reads.
    """
    host_id = await seed_host(db_session_factory)
    hosts = host_service()
    enrollment = enrollment_service()

    async with db_session_factory() as db:
        first = await hosts.issue_enrollment(db, host_id=host_id, context=CONTEXT)
        await db.commit()
    async with db_session_factory() as db:
        await enrollment.enroll(db, credential=first.credential)
        await db.commit()
    async with db_session_factory() as db:
        second = await hosts.issue_enrollment(
            db, host_id=host_id, context=CONTEXT, allow_reenrollment=True
        )
        await db.commit()

    async def re_enroll() -> bool:
        async with db_session_factory() as db:
            try:
                await enrollment.enroll(db, credential=second.credential)
            except EnrollmentRefusedError:
                return False
            await db.commit()
            return True

    async def revoke() -> bool:
        async with db_session_factory() as db:
            try:
                await hosts.revoke_credential(db, host_id=host_id, context=CONTEXT)
            except NoLiveCredentialError:
                return False
            await db.commit()
            return True

    await asyncio.gather(re_enroll(), revoke())

    async with db_session_factory() as db:
        lifecycle: str = (
            await db.execute(text(f"SELECT lifecycle FROM hosts WHERE id = '{host_id}'"))
        ).scalar_one()
    live = await count(
        db_session_factory,
        "SELECT count(*) FROM agent_credentials WHERE revoked_at IS NULL",
    )

    if lifecycle == HostLifecycle.ENROLLED.value:
        assert live == 1, "enrolled with no live credential"
    else:
        assert lifecycle == HostLifecycle.REVOKED.value
        assert live == 0, "revoked with a live credential"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_concurrent_registration_of_one_name_creates_one_host(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Decided by the unique constraint, not by the check in the service.

    The check only improves the message in the common case; under concurrency
    both callers pass it and the constraint is what refuses the loser.
    """
    hosts = host_service()

    async def create(name: str) -> bool:
        async with db_session_factory() as db:
            try:
                await hosts.create_host(db, name=name, context=CONTEXT)
            except HostNameTakenError:
                return False
            await db.commit()
            return True

    outcomes = await asyncio.gather(*(create("HomeServer") for _ in range(5)))

    assert outcomes.count(True) == 1, outcomes
    assert await count(db_session_factory, "SELECT count(*) FROM hosts") == 1


async def test_concurrent_registration_of_case_variants_creates_one_host(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two rows that render identically are the failure, not two rows.

    An operator could not tell which record their agent enrolled against.
    """
    hosts = host_service()
    names = ["homeserver", "HomeServer", "HOMESERVER", "  homeserver  "]

    async def create(name: str) -> bool:
        async with db_session_factory() as db:
            try:
                await hosts.create_host(db, name=name, context=CONTEXT)
            except HostNameTakenError:
                return False
            await db.commit()
            return True

    outcomes = await asyncio.gather(*(create(name) for name in names))

    assert outcomes.count(True) == 1, outcomes
    assert await count(db_session_factory, "SELECT count(*) FROM hosts") == 1
