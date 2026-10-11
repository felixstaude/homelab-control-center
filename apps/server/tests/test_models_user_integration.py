"""User model integration tests against real PostgreSQL.

These cover behaviour that only the database can enforce, and that a SQLite or
in-memory substitute would get wrong:

* case-insensitive uniqueness through the unique constraint
* NOT NULL and CHECK constraints
* server-side defaults (``role``, ``is_active``, ``created_at``)
* the native ``user_role`` enum rejecting unknown values
* the ``updated_at`` trigger, including on raw SQL that bypasses the ORM
* ``TIMESTAMPTZ`` round-tripping with a real offset

The schema comes from running the actual Alembic migrations — see the
``migrated_database`` fixture — so these also prove the migration and the model
agree.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from hcc.models import User, UserRole

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

FAKE_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHR2YWx1ZQ$" + "a" * 43


def make_user(**overrides: object) -> User:
    values: dict[str, object] = {"username": "test-user", "password_hash": FAKE_HASH}
    values.update(overrides)
    return User(**values)


async def insert(session: AsyncSession, user: User) -> User:
    """Persist a user and reload it, so assertions see database values."""
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


async def test_user_can_be_created_and_read_back(db_session: AsyncSession) -> None:
    user = await insert(db_session, make_user(username="FelixStaude"))

    found = (
        await db_session.execute(select(User).where(User.username_canonical == "felixstaude"))
    ).scalar_one()

    assert found.id == user.id
    assert found.username == "FelixStaude"
    assert found.username_canonical == "felixstaude"
    assert found.password_hash == FAKE_HASH


async def test_id_is_a_server_readable_uuid(db_session: AsyncSession) -> None:
    user = await insert(db_session, make_user())

    assert isinstance(user.id, uuid.UUID)

    # Confirm PostgreSQL stored it in a real uuid column, not as text.
    column_type: str = (
        await db_session.execute(
            text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = 'users' AND column_name = 'id'"
            )
        )
    ).scalar_one()
    assert column_type == "uuid"


async def test_two_users_get_distinct_ids(db_session: AsyncSession) -> None:
    first = await insert(db_session, make_user(username="user-one"))
    second = await insert(db_session, make_user(username="user-two"))

    assert first.id != second.id


# ---------------------------------------------------------------------------
# Username uniqueness
# ---------------------------------------------------------------------------


async def test_duplicate_username_is_rejected(db_session: AsyncSession) -> None:
    await insert(db_session, make_user(username="felix"))

    db_session.add(make_user(username="felix"))
    with pytest.raises(IntegrityError, match="uq_users_username_canonical"):
        await db_session.commit()


@pytest.mark.parametrize("variant", ["FELIX", "Felix", "fElIx", "  felix  "])
async def test_username_uniqueness_is_case_insensitive(
    db_session: AsyncSession, variant: str
) -> None:
    # The whole point of the canonical column: these must all collide with
    # the existing "felix".
    await insert(db_session, make_user(username="felix"))

    db_session.add(make_user(username=variant))
    with pytest.raises(IntegrityError, match="uq_users_username_canonical"):
        await db_session.commit()


async def test_username_uniqueness_folds_compatibility_variants(
    db_session: AsyncSession,
) -> None:
    # A full-width rendering of "felix" (U+FF46 U+FF45 ...) must not be
    # registrable alongside the ASCII spelling.
    await insert(db_session, make_user(username="felix"))

    db_session.add(make_user(username="\uff46\uff45\uff4c\uff49\uff58"))
    with pytest.raises(IntegrityError, match="uq_users_username_canonical"):
        await db_session.commit()


async def test_distinct_usernames_coexist(db_session: AsyncSession) -> None:
    await insert(db_session, make_user(username="felix"))
    await insert(db_session, make_user(username="felicia"))

    count: int = (await db_session.execute(text("SELECT count(*) FROM users"))).scalar_one()
    assert count == 2


async def test_accented_username_is_distinct_from_its_ascii_form(
    db_session: AsyncSession,
) -> None:
    # Accents are a genuine distinction, not a confusable fold, so both may
    # exist. This documents the deliberate boundary of the normalization rule.
    await insert(db_session, make_user(username="felix"))
    await insert(db_session, make_user(username="félix"))

    count: int = (await db_session.execute(text("SELECT count(*) FROM users"))).scalar_one()
    assert count == 2


# ---------------------------------------------------------------------------
# Required fields and constraints
# ---------------------------------------------------------------------------


async def _expect_insert_rejected(
    db_session: AsyncSession, columns: str, values: str, match: str
) -> None:
    """Assert a raw INSERT is refused by the database, then clean up.

    Raw SQL on purpose: these constraints must hold for any write, including
    one that never passes through the model's validators. The INSERT itself
    raises, so there is no commit to make.
    """
    with pytest.raises(IntegrityError, match=match):
        await db_session.execute(
            # `columns` and `values` are fixed literals supplied by the
            # tests below, never user input. Raw SQL is the point here:
            # these constraints must hold for writes that never pass
            # through the model's validators.
            text(f"INSERT INTO users ({columns}) VALUES ({values})"),
            {"hash": FAKE_HASH},
        )
    # The failed statement aborted the transaction; reset it so the fixture's
    # truncate can run.
    await db_session.rollback()


async def test_username_is_required(db_session: AsyncSession) -> None:
    await _expect_insert_rejected(
        db_session,
        "id, username_canonical, password_hash",
        "gen_random_uuid(), 'nouser', :hash",
        match='null value in column "username"',
    )


async def test_username_canonical_is_required(db_session: AsyncSession) -> None:
    await _expect_insert_rejected(
        db_session,
        "id, username, password_hash",
        "gen_random_uuid(), 'nocanon', :hash",
        match='null value in column "username_canonical"',
    )


async def test_password_hash_is_required(db_session: AsyncSession) -> None:
    await _expect_insert_rejected(
        db_session,
        "id, username, username_canonical",
        "gen_random_uuid(), 'nohash', 'nohash'",
        match='null value in column "password_hash"',
    )


async def test_empty_password_hash_is_rejected_by_the_database(
    db_session: AsyncSession,
) -> None:
    # The model validator blocks this too, but a row with an empty hash must
    # be impossible even for a write that bypasses the ORM.
    await _expect_insert_rejected(
        db_session,
        "id, username, username_canonical, password_hash",
        "gen_random_uuid(), 'emptyhash', 'emptyhash', ''",
        match="ck_users_password_hash_not_empty",
    )


async def test_short_username_is_rejected_by_the_database(db_session: AsyncSession) -> None:
    await _expect_insert_rejected(
        db_session,
        "id, username, username_canonical, password_hash",
        "gen_random_uuid(), 'ab', 'abc', :hash",
        match="ck_users_username_length",
    )


async def test_short_canonical_username_is_rejected_by_the_database(
    db_session: AsyncSession,
) -> None:
    await _expect_insert_rejected(
        db_session,
        "id, username, username_canonical, password_hash",
        "gen_random_uuid(), 'abcd', 'ab', :hash",
        match="ck_users_username_canonical_length",
    )


# ---------------------------------------------------------------------------
# Role
# ---------------------------------------------------------------------------


async def test_role_defaults_to_viewer(db_session: AsyncSession) -> None:
    # Least privilege: a user created without an explicit role must not be
    # an admin.
    user = await insert(db_session, make_user())

    assert user.role is UserRole.VIEWER


async def test_role_server_default_applies_to_raw_inserts(db_session: AsyncSession) -> None:
    await db_session.execute(
        text(
            "INSERT INTO users (id, username, username_canonical, password_hash) "
            "VALUES (gen_random_uuid(), 'rawuser', 'rawuser', :hash)"
        ),
        {"hash": FAKE_HASH},
    )
    await db_session.commit()

    role: str = (
        await db_session.execute(
            text("SELECT role FROM users WHERE username_canonical = 'rawuser'")
        )
    ).scalar_one()
    assert role == "viewer"


@pytest.mark.parametrize("role", list(UserRole))
async def test_every_role_round_trips(db_session: AsyncSession, role: UserRole) -> None:
    user = await insert(db_session, make_user(username=f"user-{role.value}", role=role))

    assert user.role is role

    stored: str = (
        await db_session.execute(text("SELECT role FROM users WHERE id = :id"), {"id": user.id})
    ).scalar_one()
    # Values, not member names: "viewer", never "VIEWER".
    assert stored == role.value


async def test_unknown_role_is_rejected_by_the_enum_type(db_session: AsyncSession) -> None:
    # The native enum is the last line of defence if a write bypasses the
    # model's validator.
    with pytest.raises(DBAPIError, match="superuser"):
        await db_session.execute(
            text(
                "INSERT INTO users (id, username, username_canonical, password_hash, role) "
                "VALUES (gen_random_uuid(), 'rooty', 'rooty', :hash, 'superuser')"
            ),
            {"hash": FAKE_HASH},
        )
    await db_session.rollback()


async def test_enum_type_has_exactly_the_expected_labels(db_session: AsyncSession) -> None:
    labels: str = (
        await db_session.execute(
            text(
                "SELECT string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder) "
                "FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid "
                "WHERE t.typname = 'user_role'"
            )
        )
    ).scalar_one()

    assert labels == "viewer,operator,admin"


# ---------------------------------------------------------------------------
# is_active
# ---------------------------------------------------------------------------


async def test_is_active_defaults_to_true(db_session: AsyncSession) -> None:
    assert (await insert(db_session, make_user())).is_active is True


async def test_is_active_can_be_set_false(db_session: AsyncSession) -> None:
    user = await insert(db_session, make_user(is_active=False))
    assert user.is_active is False


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


async def test_created_at_is_populated_by_the_database(db_session: AsyncSession) -> None:
    before = datetime.now(UTC)
    user = await insert(db_session, make_user())
    after = datetime.now(UTC)

    assert user.created_at is not None
    assert before - timedelta(seconds=5) <= user.created_at <= after + timedelta(seconds=5)


async def test_timestamps_are_timezone_aware(db_session: AsyncSession) -> None:
    # A naive datetime here would mean the column lost its offset, making
    # "when did this happen" unanswerable across a DST change.
    user = await insert(db_session, make_user())

    assert user.created_at.tzinfo is not None
    assert user.created_at.utcoffset() is not None
    assert user.updated_at.tzinfo is not None


async def test_updated_at_is_set_on_insert(db_session: AsyncSession) -> None:
    user = await insert(db_session, make_user())

    assert user.updated_at is not None
    # Both default to now() in the same statement, so they match on insert.
    assert user.updated_at == user.created_at


async def test_updated_at_advances_on_update(db_session: AsyncSession) -> None:
    user = await insert(db_session, make_user())
    original_updated = user.updated_at
    original_created = user.created_at

    # now() is the transaction timestamp, so the update must be a separate
    # transaction to receive a later value.
    await asyncio.sleep(0.01)
    user.is_active = False
    await db_session.commit()
    await db_session.refresh(user)

    assert user.updated_at > original_updated
    # created_at must never move.
    assert user.created_at == original_created


async def test_updated_at_trigger_fires_for_raw_sql_bypassing_the_orm(
    db_session: AsyncSession,
) -> None:
    # This is why the trigger exists rather than an ORM-level `onupdate`: a
    # bulk UPDATE never passes through the ORM, and a stale updated_at would
    # make the column untrustworthy for auditing.
    user = await insert(db_session, make_user())
    original = user.updated_at

    await asyncio.sleep(0.01)
    await db_session.execute(text("UPDATE users SET is_active = false"))
    await db_session.commit()

    updated: datetime = (
        await db_session.execute(
            text("SELECT updated_at FROM users WHERE id = :id"), {"id": user.id}
        )
    ).scalar_one()
    assert updated > original


async def test_updated_at_cannot_be_overridden_by_the_client(
    db_session: AsyncSession,
) -> None:
    # The BEFORE UPDATE trigger assigns unconditionally, so a caller cannot
    # backdate a row to hide a change.
    user = await insert(db_session, make_user())
    forged = datetime(2000, 1, 1, tzinfo=UTC)

    await db_session.execute(
        text("UPDATE users SET is_active = false, updated_at = :forged WHERE id = :id"),
        {"forged": forged, "id": user.id},
    )
    await db_session.commit()

    stored: datetime = (
        await db_session.execute(
            text("SELECT updated_at FROM users WHERE id = :id"), {"id": user.id}
        )
    ).scalar_one()
    assert stored != forged
    assert stored > forged


async def test_no_op_update_leaves_updated_at_alone(db_session: AsyncSession) -> None:
    # The trigger's WHEN clause means updated_at records when the row's data
    # last actually changed, not when it was last written to.
    user = await insert(db_session, make_user())
    original = user.updated_at

    await asyncio.sleep(0.01)
    await db_session.execute(
        text("UPDATE users SET is_active = is_active WHERE id = :id"), {"id": user.id}
    )
    await db_session.commit()

    stored: datetime = (
        await db_session.execute(
            text("SELECT updated_at FROM users WHERE id = :id"), {"id": user.id}
        )
    ).scalar_one()
    assert stored == original


# ---------------------------------------------------------------------------
# last_login_at
# ---------------------------------------------------------------------------


async def test_last_login_at_is_null_for_a_new_user(db_session: AsyncSession) -> None:
    # Null means "never logged in". It must not be defaulted to now(), which
    # would claim a login that never happened.
    assert (await insert(db_session, make_user())).last_login_at is None


async def test_last_login_at_accepts_a_timestamp(db_session: AsyncSession) -> None:
    moment = datetime(2026, 3, 14, 9, 26, 53, tzinfo=UTC)
    user = await insert(db_session, make_user(last_login_at=moment))

    assert user.last_login_at == moment


async def test_last_login_at_preserves_a_non_utc_offset(db_session: AsyncSession) -> None:
    # TIMESTAMPTZ normalises to UTC internally; the instant must survive even
    # though the original offset is not stored.
    moment = datetime(2026, 3, 14, 9, 26, 53, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    user = await insert(db_session, make_user(last_login_at=moment))

    assert user.last_login_at == moment
    assert user.last_login_at.astimezone(UTC) == moment.astimezone(UTC)


async def test_last_login_at_can_be_cleared(db_session: AsyncSession) -> None:
    user = await insert(db_session, make_user(last_login_at=datetime.now(UTC)))

    user.last_login_at = None
    await db_session.commit()
    await db_session.refresh(user)

    assert user.last_login_at is None
