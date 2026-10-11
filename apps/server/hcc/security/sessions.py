"""Server-side session lifecycle.

Creation, resolution, revocation and pruning. Uses SQLAlchemy but imports no
FastAPI: cookies, headers and status codes belong to the API layer, so this
module is callable from a request handler, a CLI command or a test alike.

The security rationale for the design lives in :mod:`hcc.models.session`
(why a session row rather than a JWT) and :mod:`hcc.security.tokens` (why
SHA-256 rather than Argon2).

## Resolution is fail-closed

:meth:`SessionService.resolve` returns ``None`` for every reason a session
might not be usable — absent, malformed, unknown, expired, idle too long,
revoked, or belonging to a deactivated user. It never raises for those, and
it never returns a partially valid result, so a caller cannot accidentally
treat "could not determine" as "authenticated".

## A deactivated user's sessions stop working immediately

The user is loaded with the session on every request, and ``is_active`` is
re-checked there rather than being trusted from login time. Disabling an
account therefore takes effect on that account's next request, with no
session sweep required — which is the property that makes deactivation a
usable administrative control.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, delete, select, update

from hcc.logging import get_logger
from hcc.models.session import USER_AGENT_MAX_LENGTH, Session
from hcc.security.tokens import generate_token, hash_token, looks_like_token

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from hcc.config import Settings
    from hcc.models.user import User

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """A newly created session, including the material the client needs.

    The plaintext ``token`` exists only here and in the ``Set-Cookie`` header
    the API layer builds from it. It is never stored, never logged, and cannot
    be recovered from the database afterwards — only its hash is kept.
    """

    session_id: uuid.UUID
    token: str
    csrf_token: str
    expires_at: datetime

    def __repr__(self) -> str:
        """Representation with the token redacted.

        These objects pass through the login handler, so a stray log line or a
        traceback rendering locals must not expose a live credential.
        """
        return (
            f"IssuedSession(session_id={self.session_id!r}, "
            f"token=<redacted>, csrf_token=<redacted>, "
            f"expires_at={self.expires_at!r})"
        )


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    """A resolved, currently valid session and its user."""

    session: Session
    user: User

    @property
    def csrf_token(self) -> str:
        return self.session.csrf_token


class SessionService:
    """Creates, resolves and revokes sessions.

    Stateless apart from its settings: the database holds all session state,
    so an instance is safe to share across requests.
    """

    __slots__ = ("_settings",)

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # --- Creation ---------------------------------------------------------

    async def create(
        self,
        db: AsyncSession,
        user: User,
        *,
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> IssuedSession:
        """Issue a new session for ``user``.

        The token is generated here, server-side, from a CSPRNG. Nothing the
        client sends influences it — which is what makes session fixation
        impossible: there is no path by which a caller can propose a session
        identifier and have the server adopt it.

        The caller is responsible for committing. Session creation is part of
        the login unit of work, and an uncommitted session must not be handed
        to a client.
        """
        token = generate_token()
        csrf_token = generate_token()
        now = datetime.now(UTC)

        session = Session(
            user_id=user.id,
            token_hash=hash_token(token),
            csrf_token=csrf_token,
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(seconds=self._settings.session_lifetime_seconds),
            created_ip=client_ip,
            # Attacker-controlled and unbounded, so it is truncated to the
            # column width rather than allowed to raise a database error.
            created_user_agent=(user_agent or None) and user_agent[:USER_AGENT_MAX_LENGTH],
        )
        db.add(session)
        # Assigns the server-side defaults and makes the row visible to this
        # transaction without committing it.
        await db.flush()

        logger.info(
            "session_created",
            session_id=str(session.id),
            user_id=str(user.id),
            expires_at=session.expires_at.isoformat(),
        )
        return IssuedSession(
            session_id=session.id,
            token=token,
            csrf_token=csrf_token,
            expires_at=session.expires_at,
        )

    # --- Resolution -------------------------------------------------------

    async def resolve(self, db: AsyncSession, token: str | None) -> AuthenticatedSession | None:
        """Resolve a presented token to a valid session, or ``None``.

        Fail-closed: every rejection path returns ``None``. The reason is
        logged at debug level for an operator, and never returned to the
        client — telling a caller *why* their session is invalid helps nobody
        but an attacker probing for live tokens.
        """
        if not token or not looks_like_token(token):
            # Rejected without a query: an absent or malformed cookie is the
            # common case for an unauthenticated request, and it must not cost
            # a database round trip.
            return None

        now = datetime.now(UTC)
        session = (
            await db.execute(select(Session).where(Session.token_hash == hash_token(token)))
        ).scalar_one_or_none()

        if session is None:
            return None

        reason = self._rejection_reason(session, now)
        if reason is not None:
            logger.debug(
                "session_rejected",
                session_id=str(session.id),
                reason=reason,
            )
            return None

        await self._touch(db, session, now)
        return AuthenticatedSession(session=session, user=session.user)

    def _rejection_reason(self, session: Session, now: datetime) -> str | None:
        """Why this session is unusable, or ``None`` if it is fine."""
        if session.revoked_at is not None:
            return "revoked"
        if session.expires_at <= now:
            return "expired"

        idle_timeout = self._settings.session_idle_timeout_seconds
        if idle_timeout > 0 and session.last_seen_at + timedelta(seconds=idle_timeout) <= now:
            return "idle_timeout"

        # Re-checked on every request rather than trusted from login time, so
        # that deactivating a user takes effect immediately.
        if not session.user.is_active:
            return "user_inactive"

        return None

    async def _touch(self, db: AsyncSession, session: Session, now: datetime) -> None:
        """Advance ``last_seen_at``, at most once per configured interval.

        Without the interval this would be a write on every authenticated
        request, which turns a read-mostly dashboard into a write-heavy one
        and produces needless row churn. The cost of the throttle is that the
        idle timeout is accurate only to within the interval.

        **This commits.** It has to: resolution happens in a dependency, and
        most authenticated requests are reads whose handler never commits — so
        without committing here the update would be rolled back when the
        request-scoped session closes, ``last_seen_at`` would never advance,
        and an actively used session would still hit the idle timeout. That was
        a real defect, caught by
        ``test_activity_advances_last_seen_at``.

        Committing here is safe because resolution runs before the handler
        body, so there is nothing else pending in the transaction to commit by
        accident. Recording that a session was used is also the right thing to
        persist even if the handler subsequently fails.
        """
        interval = timedelta(seconds=self._settings.session_touch_interval_seconds)
        if session.last_seen_at + interval > now:
            return

        # Issued as an UPDATE rather than by mutating the instance, so the
        # ORM does not also flush unrelated state the caller may hold.
        await db.execute(update(Session).where(Session.id == session.id).values(last_seen_at=now))
        await db.commit()
        session.last_seen_at = now

    # --- Revocation -------------------------------------------------------

    async def revoke(self, db: AsyncSession, session_id: uuid.UUID) -> bool:
        """Revoke one session. Returns whether it was live beforehand.

        Marks the row rather than deleting it, so that "this session was
        revoked at this time" stays answerable. Resolution rejects a revoked
        row, so the effect is immediate.

        Idempotent: revoking an already-revoked session returns ``False``
        rather than raising, because a double logout is a normal thing for a
        client to do.
        """
        result = await db.execute(
            update(Session)
            .where(Session.id == session_id, Session.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        # `rowcount` is a CursorResult attribute; AsyncSession.execute is
        # typed as returning the more general Result.
        revoked = cast("CursorResult[Any]", result).rowcount > 0
        if revoked:
            logger.info("session_revoked", session_id=str(session_id))
        return revoked

    async def revoke_all_for_user(self, db: AsyncSession, user_id: uuid.UUID) -> int:
        """Revoke every live session for a user. Returns how many.

        Needed by the administrative controls of a later milestone — changing
        a password or deactivating an account should not leave existing
        sessions working.
        """
        result = await db.execute(
            update(Session)
            .where(Session.user_id == user_id, Session.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        count = cast("CursorResult[Any]", result).rowcount
        if count:
            logger.info("sessions_revoked_for_user", user_id=str(user_id), count=count)
        return count

    # --- Maintenance ------------------------------------------------------

    async def prune_expired(self, db: AsyncSession, *, retain_for_days: int = 30) -> int:
        """Delete sessions that expired longer than ``retain_for_days`` ago.

        Expired rows are kept for a window so that a post-incident question —
        when did this session end, and from what address did it start — is
        still answerable. Beyond that they are only growth.

        Not yet scheduled; the scheduler lands with the rest of the periodic
        jobs. Provided now so the table has a defined growth story rather than
        an open-ended one.
        """
        cutoff = datetime.now(UTC) - timedelta(days=retain_for_days)
        result = await db.execute(delete(Session).where(Session.expires_at < cutoff))
        count = cast("CursorResult[Any]", result).rowcount
        if count:
            logger.info("sessions_pruned", count=count, retain_for_days=retain_for_days)
        return count
