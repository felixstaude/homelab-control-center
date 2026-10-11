"""SessionService integration tests against real PostgreSQL.

Covers the service directly, including the administrative operations that no
endpoint reaches yet — bulk revocation and pruning — so they are tested before
a later milestone depends on them.

Endpoint-level behaviour is in ``test_auth_integration.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select, text

from hcc.models import User, UserRole
from hcc.models.session import USER_AGENT_MAX_LENGTH, Session
from hcc.security.sessions import SessionService
from hcc.security.tokens import generate_token, hash_token

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from hcc.config import Settings

pytestmark = pytest.mark.integration

FAKE_HASH = "$argon2id$v=19$m=19456,t=2,p=1$c29tZXNhbHR2YWx1ZQ$" + "a" * 43


@pytest.fixture
def settings(api_settings_factory: Callable[..., Settings]) -> Settings:
    """Settings for the service under test."""
    return api_settings_factory(session_lifetime_seconds=3600)


@pytest.fixture
def service(settings: Settings) -> SessionService:
    return SessionService(settings)


async def make_user(db: AsyncSession, username: str = "felix") -> User:
    user = User(username=username, password_hash=FAKE_HASH, role=UserRole.ADMIN)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


async def test_create_stores_only_the_token_hash(
    db_session: AsyncSession, service: SessionService
) -> None:
    user = await make_user(db_session)

    issued = await service.create(db_session, user)
    await db_session.commit()

    stored = (await db_session.execute(select(Session.token_hash))).scalar_one()
    assert stored == hash_token(issued.token)
    assert stored != issued.token


async def test_create_records_client_metadata(
    db_session: AsyncSession, service: SessionService
) -> None:
    user = await make_user(db_session)

    await service.create(db_session, user, client_ip="192.0.2.10", user_agent="Mozilla/5.0 (test)")
    await db_session.commit()

    session = (await db_session.execute(select(Session))).scalar_one()
    assert session.created_ip == "192.0.2.10"
    assert session.created_user_agent == "Mozilla/5.0 (test)"


async def test_create_truncates_an_oversized_user_agent(
    db_session: AsyncSession, service: SessionService
) -> None:
    # The header is attacker-controlled and unbounded, so it is truncated
    # rather than allowed to raise a database error on the login path.
    user = await make_user(db_session)

    await service.create(db_session, user, user_agent="A" * 5000)
    await db_session.commit()

    session = (await db_session.execute(select(Session))).scalar_one()
    assert session.created_user_agent is not None
    assert len(session.created_user_agent) == USER_AGENT_MAX_LENGTH


async def test_create_issues_unique_tokens(
    db_session: AsyncSession, service: SessionService
) -> None:
    user = await make_user(db_session)

    issued = [await service.create(db_session, user) for _ in range(5)]
    await db_session.commit()

    assert len({item.token for item in issued}) == 5
    assert len({item.csrf_token for item in issued}) == 5
    # The CSRF token is independent of the session token, not derived from it.
    assert all(item.token != item.csrf_token for item in issued)


async def test_issued_session_repr_redacts_both_tokens(
    db_session: AsyncSession, service: SessionService
) -> None:
    user = await make_user(db_session)
    issued = await service.create(db_session, user)

    rendered = repr(issued)
    assert issued.token not in rendered
    assert issued.csrf_token not in rendered
    assert "<redacted>" in rendered


async def test_session_repr_omits_token_material(
    db_session: AsyncSession, service: SessionService
) -> None:
    user = await make_user(db_session)
    await service.create(db_session, user)
    await db_session.commit()

    session = (await db_session.execute(select(Session))).scalar_one()
    rendered = repr(session)

    assert session.token_hash not in rendered
    assert session.csrf_token not in rendered


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


async def test_resolve_returns_the_session_and_user(
    db_session: AsyncSession, service: SessionService
) -> None:
    user = await make_user(db_session)
    issued = await service.create(db_session, user)
    await db_session.commit()

    resolved = await service.resolve(db_session, issued.token)

    assert resolved is not None
    assert resolved.user.id == user.id
    assert resolved.session.id == issued.session_id
    assert resolved.csrf_token == issued.csrf_token


@pytest.mark.parametrize(
    ("token", "label"),
    [
        (None, "absent"),
        ("", "empty"),
        ("too-short", "malformed"),
        ("a" * 43, "well-formed but never issued"),
        ("x" * 1000, "oversized"),
    ],
)
async def test_resolve_rejects_bad_tokens(
    db_session: AsyncSession, service: SessionService, token: str | None, label: str
) -> None:
    # Fail-closed: every rejection returns None rather than raising, so a
    # caller cannot treat an error path as authenticated.
    await make_user(db_session)

    assert await service.resolve(db_session, token) is None, label


async def test_resolve_rejects_an_expired_session(
    db_session: AsyncSession, service: SessionService
) -> None:
    user = await make_user(db_session)
    issued = await service.create(db_session, user)
    await db_session.commit()

    await db_session.execute(
        text(
            "UPDATE sessions SET created_at = now() - interval '2 hours', "
            "expires_at = now() - interval '1 second'"
        )
    )
    await db_session.commit()

    assert await service.resolve(db_session, issued.token) is None


async def test_resolve_rejects_a_revoked_session(
    db_session: AsyncSession, service: SessionService
) -> None:
    user = await make_user(db_session)
    issued = await service.create(db_session, user)
    await db_session.commit()

    await service.revoke(db_session, issued.session_id)
    await db_session.commit()

    assert await service.resolve(db_session, issued.token) is None


async def test_resolve_rejects_a_session_of_an_inactive_user(
    db_session: AsyncSession, service: SessionService
) -> None:
    # `is_active` is re-checked on resolution rather than trusted from login
    # time, which is what makes deactivation take effect immediately.
    user = await make_user(db_session)
    issued = await service.create(db_session, user)
    await db_session.commit()

    user.is_active = False
    await db_session.commit()

    assert await service.resolve(db_session, issued.token) is None


async def test_resolve_rejects_an_idle_session(
    api_settings_factory: Callable[..., Settings], db_session: AsyncSession
) -> None:
    service = SessionService(api_settings_factory(session_idle_timeout_seconds=60))
    user = await make_user(db_session)
    issued = await service.create(db_session, user)
    await db_session.commit()

    await db_session.execute(text("UPDATE sessions SET last_seen_at = now() - interval '1 hour'"))
    await db_session.commit()

    assert await service.resolve(db_session, issued.token) is None


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


async def test_revoke_marks_the_row_rather_than_deleting_it(
    db_session: AsyncSession, service: SessionService
) -> None:
    # The row is kept so that "this session was revoked, and when" stays
    # answerable after the fact.
    user = await make_user(db_session)
    issued = await service.create(db_session, user)
    await db_session.commit()

    assert await service.revoke(db_session, issued.session_id) is True
    await db_session.commit()

    session = (await db_session.execute(select(Session))).scalar_one()
    assert session.revoked_at is not None


async def test_revoke_is_idempotent(db_session: AsyncSession, service: SessionService) -> None:
    # A double logout is a normal thing for a client to do.
    user = await make_user(db_session)
    issued = await service.create(db_session, user)
    await db_session.commit()

    assert await service.revoke(db_session, issued.session_id) is True
    assert await service.revoke(db_session, issued.session_id) is False


async def test_revoke_of_an_unknown_session_reports_false(
    db_session: AsyncSession, service: SessionService
) -> None:
    import uuid

    assert await service.revoke(db_session, uuid.uuid4()) is False


async def test_revoke_all_for_user_revokes_only_that_user(
    db_session: AsyncSession, service: SessionService
) -> None:
    # Needed by the administrative controls of a later milestone: a password
    # change or deactivation must not leave existing sessions working.
    first = await make_user(db_session, "first")
    second = await make_user(db_session, "second")
    for _ in range(3):
        await service.create(db_session, first)
    other = await service.create(db_session, second)
    await db_session.commit()

    assert await service.revoke_all_for_user(db_session, first.id) == 3
    await db_session.commit()

    assert await service.resolve(db_session, other.token) is not None
    live = (
        await db_session.execute(
            select(func.count()).select_from(Session).where(Session.revoked_at.is_(None))
        )
    ).scalar_one()
    assert live == 1


async def test_revoke_all_for_user_skips_already_revoked(
    db_session: AsyncSession, service: SessionService
) -> None:
    user = await make_user(db_session)
    issued = await service.create(db_session, user)
    await service.create(db_session, user)
    await db_session.commit()

    await service.revoke(db_session, issued.session_id)
    await db_session.commit()

    assert await service.revoke_all_for_user(db_session, user.id) == 1


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------


async def test_prune_keeps_live_and_recently_expired_sessions(
    db_session: AsyncSession, service: SessionService
) -> None:
    # Expired rows are retained for a window so a post-incident question —
    # when did this session end, from what address did it start — remains
    # answerable.
    user = await make_user(db_session)
    await service.create(db_session, user)
    await db_session.commit()

    await db_session.execute(
        text(
            "UPDATE sessions SET created_at = now() - interval '2 days', "
            "expires_at = now() - interval '1 day'"
        )
    )
    await db_session.commit()

    assert await service.prune_expired(db_session, retain_for_days=30) == 0


async def test_prune_deletes_sessions_past_the_retention_window(
    db_session: AsyncSession, service: SessionService
) -> None:
    user = await make_user(db_session)
    await service.create(db_session, user)
    await db_session.commit()

    await db_session.execute(
        text(
            "UPDATE sessions SET created_at = now() - interval '100 days', "
            "expires_at = now() - interval '99 days'"
        )
    )
    await db_session.commit()

    assert await service.prune_expired(db_session, retain_for_days=30) == 1
    await db_session.commit()

    remaining = (await db_session.execute(select(func.count()).select_from(Session))).scalar_one()
    assert remaining == 0


async def test_prune_leaves_live_sessions_alone(
    db_session: AsyncSession, service: SessionService
) -> None:
    user = await make_user(db_session)
    issued = await service.create(db_session, user)
    await db_session.commit()

    assert await service.prune_expired(db_session, retain_for_days=0) == 0
    assert await service.resolve(db_session, issued.token) is not None


# ---------------------------------------------------------------------------
# Database constraints
# ---------------------------------------------------------------------------


async def test_duplicate_token_hash_is_rejected(
    db_session: AsyncSession, service: SessionService
) -> None:
    # A collision or a double insert must be a database error, not an
    # ambiguous lookup returning two sessions.
    from sqlalchemy.exc import IntegrityError

    user = await make_user(db_session)
    token = generate_token()
    now = datetime.now(UTC)

    for _ in range(2):
        db_session.add(
            Session(
                user_id=user.id,
                token_hash=hash_token(token),
                csrf_token=generate_token(),
                created_at=now,
                last_seen_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )

    with pytest.raises(IntegrityError, match="uq_sessions_token_hash"):
        await db_session.commit()
    await db_session.rollback()


async def test_expiry_before_creation_is_rejected(
    db_session: AsyncSession, service: SessionService
) -> None:
    from sqlalchemy.exc import IntegrityError

    user = await make_user(db_session)
    now = datetime.now(UTC)

    db_session.add(
        Session(
            user_id=user.id,
            token_hash=hash_token(generate_token()),
            csrf_token=generate_token(),
            created_at=now,
            last_seen_at=now,
            expires_at=now - timedelta(seconds=1),
        )
    )

    with pytest.raises(IntegrityError, match="ck_sessions_expires_after_created"):
        await db_session.commit()
    await db_session.rollback()


async def test_session_for_an_unknown_user_is_rejected(
    db_session: AsyncSession, service: SessionService
) -> None:
    import uuid

    from sqlalchemy.exc import IntegrityError

    now = datetime.now(UTC)
    db_session.add(
        Session(
            user_id=uuid.uuid4(),
            token_hash=hash_token(generate_token()),
            csrf_token=generate_token(),
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(hours=1),
        )
    )

    with pytest.raises(IntegrityError, match="fk_sessions_user_id_users"):
        await db_session.commit()
    await db_session.rollback()
