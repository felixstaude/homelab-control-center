"""Self-service account operations.

Phase 1, milestone 1.7. What a user may do to their *own* account: change
their password, see where they are signed in, and end those sessions. No role
is required beyond being authenticated, because every operation here is scoped
to the caller by construction — the user id comes from the resolved session,
never from the request.

That last point is the whole security model of this module. There is no path
by which a caller names whose account to act on, so there is no object-level
authorization check to forget: the only id available is their own. A session
id *is* accepted, for "revoke this one", and
:meth:`hcc.security.sessions.SessionService.revoke_for_user` filters by owner
so a guessed id belonging to somebody else is indistinguishable from one that
does not exist.

## A password change rotates the current session and ends every other one

The decision, and the reasoning (ADR-040):

* **Every other session is revoked.** A password is changed either as hygiene
  or because it may be known to somebody else. In the second case, leaving
  existing sessions alive means the change accomplished nothing — the
  attacker's session outlives the credential it came from, for up to the
  absolute session lifetime.

* **The current session is replaced, not preserved.** A new token and a new
  CSRF token are issued and the old row is revoked, so the user stays signed
  in on the device they are using while the token they held before the change
  stops working. This follows the project's existing position rather than
  inventing one: login already revokes any presented session before issuing a
  new one, because a token that survives a credential event is a token an
  attacker may have planted (``hcc/api/v1/auth.py``). A password change is
  exactly such an event, and a session token that may have leaked — into a
  proxy log, a shared machine, an XSS payload — is retired by the same
  mechanism.

  Preserving it was the alternative, and it is cheaper to implement. It also
  means the one session an attacker is most likely to already hold is the one
  session the change does not touch.

Both halves are tested, including that the pre-change cookie is refused
afterwards.

## Concurrent changes cannot overwrite each other

The user row is locked ``FOR UPDATE`` for the duration of the change, so two
simultaneous requests serialise. Without it, both would verify against the
same old hash and the second would overwrite the first — so a user who
submitted twice could end up with the password from the attempt they thought
had failed. The lock makes the second request verify against the *new* hash
and be refused, which is the honest answer.

## The cost is bounded

A password change performs two Argon2 operations — one verify, one hash —
each costing ~64 MiB and ~50 ms by design. It is an authenticated endpoint, so
it is not reachable by an anonymous caller, but an authenticated one could
still use it as a CPU and memory amplifier. A dedicated rate limiter keyed on
the user id bounds that, independently of the login throttle, which this must
not share: spending the login budget here would let a user lock themselves out
of signing in by changing their password a few times.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from hcc.logging import get_logger
from hcc.models.audit import AuditAction, AuditOutcome
from hcc.models.user import User

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from hcc.models.session import Session
    from hcc.security.audit import AuditContext, AuditService
    from hcc.security.password import PasswordHasher
    from hcc.security.ratelimit import RateLimiter
    from hcc.security.sessions import IssuedSession, SessionService

logger = get_logger(__name__)


class AccountServiceError(Exception):
    """Base class for refusals from this module."""


class InvalidCurrentPasswordError(AccountServiceError):
    """The submitted current password did not verify.

    Mapped to **422**, and the status is a decision rather than a default.

    Not 401: the client treats 401 as "your session is gone" and signs the
    user out (``apps/web/src/api/errors.ts`` — ``isSessionExpired`` is 401
    only). Mistyping your current password would then eject you from the
    application, which is both wrong and alarming, and it is the exact bug
    that separation exists to prevent.

    Not 403 either: the caller is permitted to change their own password, so
    "you do not have permission" would be a false statement about
    authorization. What actually happened is that a submitted field failed
    verification, which is what 422 means everywhere else in this API.
    """

    def __init__(self) -> None:
        super().__init__("The current password is incorrect.")


class PasswordChangeRateLimitedError(AccountServiceError):
    """Too many password change attempts for this account."""

    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__("Too many password change attempts. Please try again later.")


class SessionNotFoundError(AccountServiceError):
    """No live session with that id belongs to the caller.

    Mapped to 404. Deliberately the same answer for "no such session", "that
    session belongs to someone else" and "it was already revoked", so the
    endpoint cannot be used to discover which session ids are real or whose
    they are.
    """

    def __init__(self) -> None:
        super().__init__("That session does not exist, or is no longer active.")


@dataclass(frozen=True, slots=True)
class PasswordChangeResult:
    """What a successful password change produced.

    Carries the replacement session the caller must send to the client. The
    plaintext token lives only inside :class:`IssuedSession`, which redacts it
    from its own ``repr``.
    """

    issued: IssuedSession
    #: How many *other* sessions were ended. Reported so the interface can say
    #: "signed out of 3 other sessions" rather than implying none existed.
    other_sessions_revoked: int


class AccountService:
    """Operations a user performs on their own account.

    Stateless apart from its collaborators, so one instance is shared across
    requests.
    """

    __slots__ = ("_audit", "_hasher", "_sessions", "_throttle")

    def __init__(
        self,
        *,
        hasher: PasswordHasher,
        sessions: SessionService,
        audit: AuditService,
        throttle: RateLimiter,
    ) -> None:
        self._hasher = hasher
        self._sessions = sessions
        self._audit = audit
        self._throttle = throttle

    # --- Sessions ---------------------------------------------------------

    async def list_sessions(self, db: AsyncSession, *, user_id: uuid.UUID) -> Sequence[Session]:
        """Return the caller's currently usable sessions, newest first."""
        return await self._sessions.list_live_for_user(db, user_id)

    async def revoke_session(
        self,
        db: AsyncSession,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        context: AuditContext,
    ) -> None:
        """Revoke one of the caller's own sessions.

        Permitted for the current session too — that is a sign-out, and
        refusing it would mean the interface had to explain why one row in a
        list behaves differently. The client notices on its next request,
        which answers 401, and the authentication state handles that already.

        Raises:
            SessionNotFoundError: no live session with that id belongs to the
                caller. The ownership filter lives in
                :meth:`SessionService.revoke_for_user`, so this cannot be
                satisfied by a session belonging to anyone else.
        """
        if not await self._sessions.revoke_for_user(db, user_id=user_id, session_id=session_id):
            raise SessionNotFoundError

        self._audit.record(
            db,
            action=AuditAction.ACCOUNT_SESSIONS_REVOKED,
            context=context,
            target_user_id=user_id,
            details={"scope": "one", "sessions_revoked": 1},
        )

    async def revoke_other_sessions(
        self,
        db: AsyncSession,
        *,
        user_id: uuid.UUID,
        current_session_id: uuid.UUID,
        context: AuditContext,
    ) -> int:
        """Revoke every session except the caller's current one. Returns how many.

        Idempotent, and a count of zero is a success rather than a 404: "sign
        out everywhere else" when there is nowhere else is already satisfied.
        The event is recorded either way, so an attempt is distinguishable
        from no attempt.
        """
        count = await self._sessions.revoke_others_for_user(
            db, user_id=user_id, keep_session_id=current_session_id
        )
        self._audit.record(
            db,
            action=AuditAction.ACCOUNT_SESSIONS_REVOKED,
            context=context,
            target_user_id=user_id,
            details={"scope": "others", "sessions_revoked": count},
        )
        logger.info("account_other_sessions_revoked", user_id=str(user_id), count=count)
        return count

    # --- Password ---------------------------------------------------------

    async def change_password(
        self,
        db: AsyncSession,
        *,
        user_id: uuid.UUID,
        current_password: str,
        new_password: str,
        context: AuditContext,
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> PasswordChangeResult:
        """Replace the caller's password, rotating their session.

        Order of operations, each step load-bearing:

        1. **Throttle**, before any Argon2 work. Two hashes per request at
           ~64 MiB each is the cost being bounded, and a limiter consulted
           afterwards would still let the caller spend it — the same ordering
           argument as the login path (ADR-029).
        2. **Lock the user row.** Serialises concurrent changes; see the
           module docstring.
        3. **Verify the current password** against the locked row, so the
           comparison cannot be against a hash another transaction has since
           replaced.
        4. **Hash the new password**, which enforces the policy.
        5. **Replace the hash, revoke every session, issue a new one** — all
           in the caller's transaction, so there is no instant at which the
           password has changed and the old sessions are still live.

        Does not commit. The caller does, and must send the returned session
        to the client only after that commit succeeds.

        Raises:
            PasswordChangeRateLimitedError: too many attempts for this account.
            InvalidCurrentPasswordError: the current password did not verify.
            PasswordPolicyError: the new password violates the policy. Carries
                every problem; never carries a password.
        """
        decision = self._throttle.check(str(user_id), scope="password_change")
        if not decision.allowed:
            logger.warning(
                "password_change_rate_limited",
                user_id=str(user_id),
                retry_after_seconds=decision.retry_after_seconds,
            )
            raise PasswordChangeRateLimitedError(decision.retry_after_seconds)

        user = (
            await db.execute(
                select(User)
                .where(User.id == user_id)
                .with_for_update()
                # `populate_existing` is not optional here, and leaving it out
                # was a real defect that the concurrency test caught.
                #
                # Session resolution has already loaded this very row — the
                # session's `user` relationship is eagerly joined on every
                # authenticated request — so the instance is in this session's
                # identity map. SQLAlchemy does not overwrite the attributes of
                # an object it already holds unless asked, so the `FOR UPDATE`
                # acquired the lock and then handed back the *pre-lock*
                # `password_hash`. Two simultaneous changes therefore both
                # verified against the same old hash and both succeeded: the
                # lock was working and the read was stale, which is the one
                # combination that looks correct in every single-request test.
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if user is None:
            # The session resolved to this id moments ago, so the account
            # being gone means it was deleted mid-request. Reported as a bad
            # current password rather than a 404: there is no account to
            # describe, and a distinct answer would only confirm the deletion.
            logger.warning("password_change_user_missing", user_id=str(user_id))
            raise InvalidCurrentPasswordError

        if not await self._hasher.verify_async(user.password_hash, current_password):
            logger.warning("password_change_rejected", user_id=str(user_id), reason="bad_password")
            # Recorded as a failure, in its own transaction: nothing else
            # happened, so there is nothing to commit it alongside. A wrong
            # current password is worth keeping — repeated attempts against
            # one account are a signal that a session has been taken over.
            await self._record_failed_change(db, user_id=user_id, context=context)
            raise InvalidCurrentPasswordError

        # Enforces the policy and raises PasswordPolicyError, so the policy
        # has exactly one implementation (ADR-026).
        new_hash = await self._hasher.hash_async(new_password)
        user.password_hash = new_hash

        # Every session, including the caller's current one. The replacement
        # is issued below, so the caller stays signed in with new material.
        revoked = await self._sessions.revoke_all_for_user(db, user_id)
        issued = await self._sessions.create(db, user, client_ip=client_ip, user_agent=user_agent)
        # `revoked` counted the current session too; what the interface wants
        # to report is how many *other* places were signed out.
        other_revoked = max(0, revoked - 1)

        self._audit.record(
            db,
            action=AuditAction.ACCOUNT_PASSWORD_CHANGED,
            context=context,
            target_user_id=user_id,
            details={"other_sessions_revoked": other_revoked, "session_rotated": True},
        )
        logger.info(
            "password_changed",
            user_id=str(user_id),
            other_sessions_revoked=other_revoked,
        )
        return PasswordChangeResult(issued=issued, other_sessions_revoked=other_revoked)

    async def _record_failed_change(
        self, db: AsyncSession, *, user_id: uuid.UUID, context: AuditContext
    ) -> None:
        """Persist a record of a rejected password change.

        Its own transaction, because the attempt changed nothing and so has
        nothing to commit with. Never raises: the caller is about to refuse
        the request and must do so regardless. Same trade as
        :meth:`hcc.security.audit.AuditService.record_denial`.
        """
        try:
            await db.rollback()
            self._audit.record(
                db,
                action=AuditAction.ACCOUNT_PASSWORD_CHANGED,
                context=context,
                outcome=AuditOutcome.FAILURE,
                target_user_id=user_id,
                details={"reason": "invalid_current_password"},
            )
            await db.commit()
        except Exception:
            logger.exception("audit_password_change_failure_record_failed", user_id=str(user_id))
