"""Password hashing integration with the User model and PostgreSQL.

Covers the seam between milestone 1.1 (the `users` table) and 1.2 (hashing):
that a real Argon2id hash survives a round trip through the column, that the
model's own validators accept it, and that a parameter upgrade can be
persisted.

There is still no login flow — these tests exercise storage and retrieval,
not authentication.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select, text

from hcc.models import User, UserRole
from hcc.models.user import PASSWORD_HASH_MAX_LENGTH
from hcc.security.password import (
    MAX_MEMORY_COST_KIB,
    MAX_PARALLELISM,
    MAX_TIME_COST,
    MIN_MEMORY_COST_KIB,
    MIN_PARALLELISM,
    MIN_TIME_COST,
    Argon2Parameters,
    PasswordHasher,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"

# Cheapest permitted parameters, to keep these tests quick.
FAST_PARAMETERS = Argon2Parameters(
    memory_cost=MIN_MEMORY_COST_KIB,
    time_cost=MIN_TIME_COST,
    parallelism=MIN_PARALLELISM,
)


@pytest.fixture(scope="module")
def hasher() -> PasswordHasher:
    return PasswordHasher(FAST_PARAMETERS)


async def test_hash_round_trips_through_the_column(
    db_session: AsyncSession, hasher: PasswordHasher
) -> None:
    encoded = hasher.hash(PASSWORD)

    db_session.add(User(username="felix", password_hash=encoded, role=UserRole.ADMIN))
    await db_session.commit()

    stored = (
        await db_session.execute(select(User).where(User.username_canonical == "felix"))
    ).scalar_one()

    # Byte-for-byte identical: no truncation by the column, no normalisation
    # by the driver.
    assert stored.password_hash == encoded
    assert hasher.verify(stored.password_hash, PASSWORD) is True
    assert hasher.verify(stored.password_hash, "wrong password") is False


async def test_default_parameter_hash_fits_the_column(
    db_session: AsyncSession,
) -> None:
    # The shipped defaults, not the fast test parameters: this is the length
    # that will actually be stored in production.
    encoded = PasswordHasher().hash(PASSWORD)

    assert len(encoded) <= PASSWORD_HASH_MAX_LENGTH

    db_session.add(User(username="defaults", password_hash=encoded))
    await db_session.commit()

    length: int = (
        await db_session.execute(
            text("SELECT char_length(password_hash) FROM users WHERE username = 'defaults'")
        )
    ).scalar_one()
    assert length == len(encoded) == 97


async def test_widest_permitted_parameters_still_fit_the_column(
    db_session: AsyncSession,
) -> None:
    # Guards against a future parameter increase overflowing the column. The
    # encoded length grows with the number of digits in the parameters, so
    # this is built from the maximum permitted values rather than by paying
    # for a 4 GiB hash.
    salt_b64, digest_b64 = "A" * 22, "B" * 43
    widest = (
        f"$argon2id$v=19$m={MAX_MEMORY_COST_KIB},t={MAX_TIME_COST},p={MAX_PARALLELISM}$"
        f"{salt_b64}${digest_b64}"
    )
    assert len(widest) <= PASSWORD_HASH_MAX_LENGTH

    # The column accepts it, so raising the parameters later needs no migration.
    db_session.add(User(username="widest", password_hash=widest))
    await db_session.commit()

    stored = (
        await db_session.execute(select(User).where(User.username_canonical == "widest"))
    ).scalar_one()
    assert stored.password_hash == widest


async def test_each_user_gets_a_distinct_hash_for_the_same_password(
    db_session: AsyncSession, hasher: PasswordHasher
) -> None:
    # Per-hash salts mean two accounts sharing a password are not identifiable
    # as such from the database.
    for name in ("alice", "bob"):
        db_session.add(User(username=name, password_hash=hasher.hash(PASSWORD)))
    await db_session.commit()

    hashes: Sequence[str] = (
        (await db_session.execute(text("SELECT password_hash FROM users ORDER BY username")))
        .scalars()
        .all()
    )

    assert len(hashes) == 2
    assert hashes[0] != hashes[1]
    assert all(hasher.verify(value, PASSWORD) for value in hashes)


async def test_model_rejects_an_empty_hash_before_the_database_does(
    hasher: PasswordHasher,
) -> None:
    # The model validator from milestone 1.1 and the CHECK constraint both
    # guard this; the validator fails earlier and more clearly.
    with pytest.raises(ValueError, match="non-empty"):
        User(username="empty", password_hash="")


async def test_parameter_upgrade_can_be_persisted(
    db_session: AsyncSession, hasher: PasswordHasher
) -> None:
    # The full intended flow for a transparent upgrade, minus the login
    # endpoint that will drive it: an existing row hashed under old
    # parameters is verified, detected as stale, re-hashed and stored.
    old_hash = hasher.hash(PASSWORD)
    db_session.add(User(username="upgradable", password_hash=old_hash))
    await db_session.commit()

    stronger = PasswordHasher(
        Argon2Parameters(
            memory_cost=FAST_PARAMETERS.memory_cost * 2,
            time_cost=FAST_PARAMETERS.time_cost,
            parallelism=FAST_PARAMETERS.parallelism,
        )
    )

    user = (
        await db_session.execute(select(User).where(User.username_canonical == "upgradable"))
    ).scalar_one()

    assert stronger.verify(user.password_hash, PASSWORD) is True
    assert stronger.needs_rehash(user.password_hash) is True

    user.password_hash = stronger.hash(PASSWORD)
    await db_session.commit()
    await db_session.refresh(user)

    assert user.password_hash != old_hash
    assert stronger.needs_rehash(user.password_hash) is False
    assert stronger.verify(user.password_hash, PASSWORD) is True
    # The old password is unchanged — an upgrade re-hashes, it does not reset.
    assert f"m={FAST_PARAMETERS.memory_cost * 2}" in user.password_hash


async def test_changing_the_hash_bumps_updated_at(
    db_session: AsyncSession, hasher: PasswordHasher
) -> None:
    # A password change must be visible in the audit-relevant timestamp, which
    # the trigger from milestone 1.1 maintains.
    user = User(username="rotator", password_hash=hasher.hash(PASSWORD))
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    original_updated_at = user.updated_at

    user.password_hash = hasher.hash("an entirely new password")
    await db_session.commit()
    await db_session.refresh(user)

    assert user.updated_at > original_updated_at
    assert hasher.verify(user.password_hash, "an entirely new password") is True
    assert hasher.verify(user.password_hash, PASSWORD) is False


async def test_user_repr_never_exposes_the_hash(hasher: PasswordHasher) -> None:
    # Both halves of the guarantee: the model omits it from __repr__, and the
    # hash itself contains no plaintext.
    encoded = hasher.hash(PASSWORD)
    user = User(username="discreet", password_hash=encoded)

    rendered = repr(user)
    assert encoded not in rendered
    assert "argon2" not in rendered
    assert PASSWORD not in encoded
