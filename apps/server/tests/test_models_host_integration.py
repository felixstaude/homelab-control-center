"""Host model integration tests against real PostgreSQL.

These cover what only the database can enforce, and what an in-memory
substitute would get wrong:

* case-insensitive name uniqueness through the unique constraint
* the CHECK constraints, exercised through **raw SQL** so the ORM validators
  cannot absorb the bad value before it reaches the database
* server-side defaults (``lifecycle``, ``is_enabled``, ``connection_epoch``)
* the ``updated_at`` trigger, including on a write that bypasses the ORM
* the partial unique index that allows one live credential per host
* ``ON DELETE CASCADE`` reaching both child tables
* ``TIMESTAMPTZ`` round-tripping with a real offset

The CHECK constraints are the point of several of these. ``hcc/models/host.py``
validates lifecycle at assignment, which is better ergonomics — but a bulk
``UPDATE``, a migration, or a future service written in a hurry does not go
through the ORM, and the constraint is what makes the invariant true of the
*data* rather than of one code path.

Schema comes from running the real Alembic migrations (the ``migrated_database``
fixture), so these also prove migration ``0006_host_registry`` and the models
agree.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from hcc.models import AgentCredential, Host, HostConnectivity, HostEnrollment, HostLifecycle
from hcc.security.tokens import TOKEN_HASH_LENGTH

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


def digest(seed: str) -> str:
    """A digest-shaped value of the right width. Not derived from a real token."""
    return seed[0] * TOKEN_HASH_LENGTH if len(seed) == 1 else seed.ljust(TOKEN_HASH_LENGTH, "0")


async def count_rows(session: AsyncSession, table: str, where: str = "true") -> int:
    """Count rows straight from a table.

    Past the ORM so an assertion about persistence is made against the
    database rather than against the objects the test just created. ``table``
    and ``where`` are literals written in this module, never request data.
    """
    result = await session.execute(text(f"SELECT count(*) FROM {table} WHERE {where}"))
    total: int = result.scalar_one()
    return total


async def read_updated_at(session: AsyncSession, host_id: uuid.UUID) -> datetime:
    """Read ``updated_at`` straight from the row.

    Past the ORM deliberately: the trigger changed a value the identity map
    still holds, and these tests are asking what the database contains.
    """
    result = await session.execute(
        text("SELECT updated_at FROM hosts WHERE id = :id"), {"id": host_id}
    )
    stamp: datetime = result.scalar_one()
    return stamp


async def insert_host(session: AsyncSession, **overrides: object) -> Host:
    """Persist a host and reload it, so assertions see database values."""
    values: dict[str, object] = {"name": "homeserver"}
    values.update(overrides)
    host = Host(**values)
    session.add(host)
    await session.commit()
    await session.refresh(host)
    return host


# ---------------------------------------------------------------------------
# Creation and defaults
# ---------------------------------------------------------------------------


async def test_a_host_can_be_created_and_read_back(db_session: AsyncSession) -> None:
    host = await insert_host(db_session, name="HomeServer", description="the one in the cupboard")

    found = (
        await db_session.execute(select(Host).where(Host.name_canonical == "homeserver"))
    ).scalar_one()

    assert found.id == host.id
    assert found.name == "HomeServer"
    assert found.name_canonical == "homeserver"
    assert found.description == "the one in the cupboard"


async def test_the_server_defaults_land_in_the_row(db_session: AsyncSession) -> None:
    """The values a host has before anything happens to it.

    Checked against the database rather than the model, because the migration
    is what production runs and a default present in one and absent from the
    other would only show up as a NOT NULL violation under load.
    """
    host = await insert_host(db_session)

    assert host.lifecycle == HostLifecycle.REGISTERED
    assert host.is_enabled is True
    assert host.connection_epoch == 0
    assert host.created_at is not None
    assert host.updated_at is not None


async def test_a_new_host_reports_no_agent_facts(db_session: AsyncSession) -> None:
    """Null, never a placeholder or a zero.

    Product principle 2: an unreported value must stay distinguishable from a
    measured one. A zero here would be rendered as a measurement.
    """
    host = await insert_host(db_session)

    assert host.current_connection_id is None
    assert host.last_connected_at is None
    assert host.last_disconnected_at is None
    assert host.last_heartbeat_at is None
    assert host.agent_version is None
    assert host.protocol_version is None
    assert host.platform is None
    assert host.agent_started_at is None


async def test_a_fresh_host_derives_as_offline(db_session: AsyncSession) -> None:
    host = await insert_host(db_session)

    assert host.connectivity(datetime.now(UTC)) is HostConnectivity.OFFLINE


# ---------------------------------------------------------------------------
# Name uniqueness
# ---------------------------------------------------------------------------


async def test_two_hosts_differing_only_in_case_cannot_coexist(
    db_session: AsyncSession,
) -> None:
    """The constraint, not the application, decides this.

    Two rows rendering identically in a list is the failure: an operator could
    not tell which record their agent enrolled against.
    """
    await insert_host(db_session, name="homeserver")

    db_session.add(Host(name="HomeServer"))
    with pytest.raises(IntegrityError, match="uq_hosts_name_canonical"):
        await db_session.commit()


async def test_whitespace_variants_cannot_coexist_either(db_session: AsyncSession) -> None:
    await insert_host(db_session, name="backup nas")

    db_session.add(Host(name="Backup   NAS"))
    with pytest.raises(IntegrityError, match="uq_hosts_name_canonical"):
        await db_session.commit()


async def test_distinct_names_coexist_freely(db_session: AsyncSession) -> None:
    await insert_host(db_session, name="homeserver")
    await insert_host(db_session, name="backup-nas")

    count = await count_rows(db_session, "hosts")
    assert count == 2


# ---------------------------------------------------------------------------
# The updated_at trigger
# ---------------------------------------------------------------------------


async def test_the_trigger_moves_updated_at_on_an_orm_write(db_session: AsyncSession) -> None:
    host = await insert_host(db_session)
    created = host.updated_at

    host.description = "now described"
    await db_session.commit()
    # Refreshed, not read from the instance: the trigger wrote `updated_at` on
    # the server, so the in-memory value is whatever it was before the flush.
    await db_session.refresh(host)

    assert host.updated_at > created


async def test_the_trigger_fires_on_a_write_that_bypasses_the_orm(
    db_session: AsyncSession,
) -> None:
    """The reason this is a trigger and not an ORM hook.

    A heartbeat is a bulk ``UPDATE``, not an ORM flush. An ``onupdate=`` in
    Python would leave ``updated_at`` frozen for exactly the writes that happen
    most often.
    """
    host = await insert_host(db_session)
    created = host.updated_at

    await db_session.execute(
        text("UPDATE hosts SET last_heartbeat_at = now() WHERE id = :id"), {"id": host.id}
    )
    await db_session.commit()
    after = await read_updated_at(db_session, host.id)

    assert after > created


async def test_a_no_op_update_does_not_move_updated_at(db_session: AsyncSession) -> None:
    """So ``updated_at`` means "when the data last changed".

    Without the ``WHEN (OLD.* IS DISTINCT FROM NEW.*)`` clause, any write that
    happened to set a column to the value it already held would make the host
    look freshly modified.
    """
    host = await insert_host(db_session, description="unchanged")
    created = host.updated_at

    await db_session.execute(
        text("UPDATE hosts SET description = 'unchanged' WHERE id = :id"), {"id": host.id}
    )
    await db_session.commit()
    after = await read_updated_at(db_session, host.id)

    assert after == created


async def test_a_client_cannot_backdate_updated_at(db_session: AsyncSession) -> None:
    """The trigger assigns unconditionally, so a supplied value is overwritten.

    An audit question — "when did this host last change" — must not be
    answerable with a value the caller chose.
    """
    host = await insert_host(db_session)

    await db_session.execute(
        text("UPDATE hosts SET description = 'x', updated_at = :stale WHERE id = :id"),
        {"id": host.id, "stale": datetime(2001, 1, 1, tzinfo=UTC)},
    )
    await db_session.commit()
    after = await read_updated_at(db_session, host.id)

    assert after.year != 2001


# ---------------------------------------------------------------------------
# CHECK constraints
#
# Driven through raw SQL on purpose: the ORM validators would reject these
# before they reached the database, which is the ergonomics, not the
# guarantee. A bulk UPDATE or a hand-run statement has no validator.
# ---------------------------------------------------------------------------


async def test_an_unknown_lifecycle_is_refused_by_the_database(
    db_session: AsyncSession,
) -> None:
    host = await insert_host(db_session)

    with pytest.raises(DBAPIError, match="ck_hosts_lifecycle_is_known"):
        await db_session.execute(
            text("UPDATE hosts SET lifecycle = 'decommissioned' WHERE id = :id"), {"id": host.id}
        )


async def test_a_negative_connection_epoch_is_refused(db_session: AsyncSession) -> None:
    """An epoch only ever increases, so negative means an arithmetic bug.

    Worth catching at the database: the epoch is what stops a stale socket
    overwriting a newer connection's state, and a wrapped or negative value
    would silently disable that protection.
    """
    host = await insert_host(db_session)

    with pytest.raises(DBAPIError, match="ck_hosts_connection_epoch_not_negative"):
        await db_session.execute(
            text("UPDATE hosts SET connection_epoch = -1 WHERE id = :id"), {"id": host.id}
        )


async def test_an_empty_name_is_refused_by_the_database(db_session: AsyncSession) -> None:
    host = await insert_host(db_session)

    with pytest.raises(DBAPIError, match="ck_hosts_name_length"):
        await db_session.execute(text("UPDATE hosts SET name = '' WHERE id = :id"), {"id": host.id})


async def test_a_wrong_width_credential_digest_is_refused(db_session: AsyncSession) -> None:
    """The width check is the guard against storing the wrong thing.

    A 64-character column cannot hold a plaintext token, and the equality check
    means a truncated or empty digest is a database error rather than a
    credential nothing can ever match.
    """
    host = await insert_host(db_session)

    with pytest.raises(DBAPIError, match="ck_agent_credentials_token_hash_is_sha256_hex"):
        await db_session.execute(
            text(
                "INSERT INTO agent_credentials (id, host_id, token_hash) "
                "VALUES (gen_random_uuid(), :host_id, 'too-short')"
            ),
            {"host_id": host.id},
        )


async def test_an_enrollment_that_expires_before_it_was_created_is_refused(
    db_session: AsyncSession,
) -> None:
    """An already-expired token is a bug, not a policy.

    It would be issued, displayed to the operator exactly once, and then
    refuse the agent — with nothing in the interface explaining why.
    """
    host = await insert_host(db_session)

    with pytest.raises(DBAPIError, match="ck_host_enrollments_expires_after_created"):
        await db_session.execute(
            text(
                "INSERT INTO host_enrollments (id, host_id, token_hash, expires_at) "
                "VALUES (gen_random_uuid(), :host_id, :digest, now() - interval '1 second')"
            ),
            {"host_id": host.id, "digest": digest("e")},
        )


# ---------------------------------------------------------------------------
# Enrollment tokens
# ---------------------------------------------------------------------------


async def test_an_enrollment_can_be_issued_and_resolved_by_digest(
    db_session: AsyncSession,
) -> None:
    host = await insert_host(db_session)
    token_hash = digest("a")
    db_session.add(
        HostEnrollment(
            host_id=host.id,
            token_hash=token_hash,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
    )
    await db_session.commit()

    found = (
        await db_session.execute(
            select(HostEnrollment).where(HostEnrollment.token_hash == token_hash)
        )
    ).scalar_one()

    assert found.host_id == host.id
    assert found.consumed_at is None
    assert found.revoked_at is None
    # Eagerly joined, so this does not emit a second query — and under async
    # SQLAlchemy a lazy load here would raise instead of loading.
    assert found.host.name == "homeserver"


async def test_two_enrollments_cannot_share_a_digest(db_session: AsyncSession) -> None:
    """A collision or a double-insert must be an error, not an ambiguous row.

    If two rows could carry one digest, resolving a presented token would
    return an arbitrary one of them, and the token would enroll against
    whichever host the query happened to pick.
    """
    host = await insert_host(db_session)
    expires = datetime.now(UTC) + timedelta(minutes=15)
    db_session.add(HostEnrollment(host_id=host.id, token_hash=digest("a"), expires_at=expires))
    await db_session.commit()

    db_session.add(HostEnrollment(host_id=host.id, token_hash=digest("a"), expires_at=expires))
    with pytest.raises(IntegrityError, match="uq_host_enrollments_token_hash"):
        await db_session.commit()


async def test_a_host_may_hold_several_enrollment_rows(db_session: AsyncSession) -> None:
    """Unlike credentials, which are capped at one live per host.

    Issuing a second token must not require destroying the record of the
    first: "this token was issued, and then revoked unused" is the audit
    trail, and deleting the row would erase it.
    """
    host = await insert_host(db_session)
    expires = datetime.now(UTC) + timedelta(minutes=15)
    for seed in "abc":
        db_session.add(HostEnrollment(host_id=host.id, token_hash=digest(seed), expires_at=expires))
    await db_session.commit()

    count = await count_rows(db_session, "host_enrollments")
    assert count == 3


# ---------------------------------------------------------------------------
# Agent credentials and the partial unique index
#
# This index is the reason a revocation can be trusted.
# ---------------------------------------------------------------------------


async def test_a_host_cannot_hold_two_live_credentials(db_session: AsyncSession) -> None:
    """Enforced by the database, not by application care.

    Without it, a re-enrollment race leaves two valid credentials for one
    host — and revoking "the" credential then reports success while the other
    one keeps working. A revocation that does not revoke is the worst possible
    outcome of this screen.
    """
    host = await insert_host(db_session)
    db_session.add(AgentCredential(host_id=host.id, token_hash=digest("a")))
    await db_session.commit()

    db_session.add(AgentCredential(host_id=host.id, token_hash=digest("b")))
    with pytest.raises(IntegrityError, match="uq_agent_credentials_one_live_per_host"):
        await db_session.commit()


async def test_revoking_frees_the_slot_for_a_re_enrollment(db_session: AsyncSession) -> None:
    """The index is partial for this reason: a revoked row still exists.

    Re-enrollment after a reinstall has to work, and the old row has to stay
    so "this credential was revoked, at this time" remains answerable.
    """
    host = await insert_host(db_session)
    old = AgentCredential(host_id=host.id, token_hash=digest("a"))
    db_session.add(old)
    await db_session.commit()

    old.revoked_at = datetime.now(UTC)
    db_session.add(AgentCredential(host_id=host.id, token_hash=digest("b")))
    await db_session.commit()

    live = await count_rows(db_session, "agent_credentials", "revoked_at IS NULL")
    total = await count_rows(db_session, "agent_credentials")

    assert live == 1
    assert total == 2


async def test_a_host_may_accumulate_many_revoked_credentials(
    db_session: AsyncSession,
) -> None:
    """The partial index excludes them, so the history is unbounded by design."""
    host = await insert_host(db_session)
    revoked = datetime.now(UTC)
    for seed in "abc":
        db_session.add(
            AgentCredential(host_id=host.id, token_hash=digest(seed), revoked_at=revoked)
        )
    await db_session.commit()

    db_session.add(AgentCredential(host_id=host.id, token_hash=digest("d")))
    await db_session.commit()

    total = await count_rows(db_session, "agent_credentials")
    assert total == 4


async def test_two_hosts_cannot_share_a_credential_digest(db_session: AsyncSession) -> None:
    """Resolving a presented credential must identify exactly one host.

    If a digest could belong to two hosts, an agent would authenticate as
    whichever row the query returned — which is a cross-host impersonation
    with no attacker required.
    """
    first = await insert_host(db_session, name="alpha")
    second = await insert_host(db_session, name="beta")
    db_session.add(AgentCredential(host_id=first.id, token_hash=digest("a")))
    await db_session.commit()

    db_session.add(AgentCredential(host_id=second.id, token_hash=digest("a")))
    with pytest.raises(IntegrityError, match="uq_agent_credentials_token_hash"):
        await db_session.commit()


async def test_each_host_gets_its_own_live_credential(db_session: AsyncSession) -> None:
    """The index is per host, not global — the obvious way to get this wrong."""
    first = await insert_host(db_session, name="alpha")
    second = await insert_host(db_session, name="beta")

    db_session.add(AgentCredential(host_id=first.id, token_hash=digest("a")))
    db_session.add(AgentCredential(host_id=second.id, token_hash=digest("b")))
    await db_session.commit()

    live = await count_rows(db_session, "agent_credentials", "revoked_at IS NULL")
    assert live == 2


async def test_a_credential_cannot_point_at_a_host_that_does_not_exist(
    db_session: AsyncSession,
) -> None:
    db_session.add(AgentCredential(host_id=uuid.uuid4(), token_hash=digest("a")))

    with pytest.raises(IntegrityError, match="fk_agent_credentials_host_id_hosts"):
        await db_session.commit()


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------


async def test_deleting_a_host_removes_its_tokens_and_credentials(
    db_session: AsyncSession,
) -> None:
    """A token or credential outliving its host would be a live secret
    authorizing a connection to nothing.
    """
    host = await insert_host(db_session)
    db_session.add(
        HostEnrollment(
            host_id=host.id,
            token_hash=digest("a"),
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
    )
    db_session.add(AgentCredential(host_id=host.id, token_hash=digest("b")))
    await db_session.commit()

    await db_session.execute(text("DELETE FROM hosts WHERE id = :id"), {"id": host.id})
    await db_session.commit()

    enrollments = await count_rows(db_session, "host_enrollments")
    credentials = await count_rows(db_session, "agent_credentials")

    assert enrollments == 0
    assert credentials == 0


async def test_deleting_a_host_does_not_touch_another_hosts_credential(
    db_session: AsyncSession,
) -> None:
    doomed = await insert_host(db_session, name="doomed")
    keeper = await insert_host(db_session, name="keeper")
    db_session.add(AgentCredential(host_id=doomed.id, token_hash=digest("a")))
    db_session.add(AgentCredential(host_id=keeper.id, token_hash=digest("b")))
    await db_session.commit()

    await db_session.execute(text("DELETE FROM hosts WHERE id = :id"), {"id": doomed.id})
    await db_session.commit()

    remaining = (await db_session.execute(select(AgentCredential))).unique().scalars().all()

    assert [credential.host_id for credential in remaining] == [keeper.id]


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


async def test_timestamps_round_trip_with_a_real_offset(db_session: AsyncSession) -> None:
    """Stored as an instant, not as wall-clock plus a label.

    A naive column would make "when did we last hear from this host"
    unanswerable across a DST boundary, and an expiry comparison wrong by the
    offset — which is a token that outlives its own expiry.
    """
    berlin = timezone(timedelta(hours=2))
    heartbeat = datetime(2026, 7, 1, 14, 30, tzinfo=berlin)
    host = await insert_host(db_session)

    host.last_heartbeat_at = heartbeat
    await db_session.commit()
    await db_session.refresh(host)

    assert host.last_heartbeat_at is not None
    assert host.last_heartbeat_at.tzinfo is not None
    assert host.last_heartbeat_at == heartbeat
    assert host.last_heartbeat_at.astimezone(UTC).hour == 12


async def test_connectivity_is_derived_from_values_that_went_through_the_database(
    db_session: AsyncSession,
) -> None:
    """The derivation has to hold for timestamps Postgres produced.

    A unit test can only prove the arithmetic; this proves the arithmetic
    still works on a timezone-aware value read back from the column, which is
    where a naive/aware mix-up would surface as a ``TypeError`` in production.
    """
    host = await insert_host(db_session)

    await db_session.execute(
        text(
            "UPDATE hosts SET current_connection_id = gen_random_uuid(), "
            "last_heartbeat_at = now() WHERE id = :id"
        ),
        {"id": host.id},
    )
    await db_session.commit()
    await db_session.refresh(host)

    assert host.connectivity(datetime.now(UTC)) is HostConnectivity.CONNECTED

    await db_session.execute(
        text("UPDATE hosts SET last_heartbeat_at = now() - interval '1 hour' WHERE id = :id"),
        {"id": host.id},
    )
    await db_session.commit()
    await db_session.refresh(host)

    assert host.connectivity(datetime.now(UTC)) is HostConnectivity.STALE
