"""Credential authentication.

Turns a username and password into a session, or into a uniform failure.
Uses SQLAlchemy but imports no FastAPI: the HTTP concerns — cookies, status
codes, the CSRF header — belong to :mod:`hcc.api.v1.auth`.

## The ordering of checks is the security property

1. **Throttle first.** The rate limiter is consulted before anything
   expensive. An Argon2 verification costs ~64 MiB and ~50 ms by design, and
   this endpoint is reachable without authentication, so a limiter consulted
   after the work still lets an attacker spend the server's memory.
2. **Look up the user** by canonical username, reusing the normalization from
   :mod:`hcc.models.user` so that ``Admin`` and ``admin`` are the same
   account here as everywhere else.
3. **Always verify a password**, even when the username is unknown — against a
   fixed dummy hash. See below.
4. **Then check whether the account is active.** Deliberately after
   verification, so an inactive account costs the same time as an active one.
5. **Issue a session** only if everything held.

## Every failure looks identical

Wrong password, unknown username, inactive account and a malformed stored hash
all produce the same :class:`AuthenticationFailedError` with the same code and the
same message. The client cannot tell them apart.

This is not politeness. A login that distinguishes "no such user" from "wrong
password" is a username oracle: an attacker enumerates valid accounts at the
unauthenticated endpoint, then targets only those. The operator still gets the
real reason, in the server log.

## Timing

An unknown username would otherwise return in microseconds while a known one
takes ~50 ms, which is a trivially measurable oracle that no amount of
identical messaging hides. So an unknown username is verified against a
**precomputed dummy hash** — real Argon2 work at the configured parameters,
discarded.

This narrows the gap; it does not close it. Remaining differences — one
database query versus none, the dummy hash being a constant — are small
relative to network jitter, and the rate limiter bounds how many samples an
attacker can collect. Closing it completely would need constant-time work
across the whole request, which is not achievable in a Python web application
and is not claimed here. Recorded as a known limitation in ``docs/SECURITY.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from sqlalchemy import select

from hcc.logging import get_logger
from hcc.models.user import User, normalize_username

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from hcc.security.password import PasswordHasher
    from hcc.security.ratelimit import LoginThrottle
    from hcc.security.sessions import IssuedSession, SessionService

logger = get_logger(__name__)

#: The password hashed to produce the dummy verification hash. Not a
#: credential: it authenticates nothing, because the hash it produces is never
#: attached to a user row. It exists only to give the verifier real work to do
#: when there is no stored hash to check against.
_DUMMY_PASSWORD: Final = "hcc-dummy-verification-password-not-a-credential"  # noqa: S105


class AuthenticationFailedError(Exception):
    """Credentials were not accepted.

    Carries ``reason`` for the server-side log only. The HTTP layer maps every
    instance to one generic response, so the distinction never reaches a
    client — see the module docstring.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"authentication failed: {reason}")


class LoginRateLimitedError(Exception):
    """Too many attempts. Raised before any password verification."""

    def __init__(self, retry_after_seconds: int, scope: str) -> None:
        self.retry_after_seconds = retry_after_seconds
        #: Which budget refused this. For logging only: telling a client
        #: whether it hit the address or the username budget would reveal
        #: whether that username is being attacked by someone else.
        self.scope = scope
        super().__init__(f"login rate limited ({scope})")


@dataclass(frozen=True, slots=True)
class LoginRequest:
    """Inputs to an authentication attempt.

    ``__repr__`` is suppressed below because this holds a plaintext password
    and these objects appear in handler locals, which a traceback renders.
    """

    username: str
    password: str
    client_ip: str
    user_agent: str | None = None

    def __repr__(self) -> str:
        return f"LoginRequest(username={self.username!r}, password=<redacted>, ...)"


class AuthenticationService:
    """Authenticates credentials and issues sessions."""

    __slots__ = ("_dummy_hash", "_hasher", "_sessions", "_throttle")

    def __init__(
        self,
        *,
        hasher: PasswordHasher,
        sessions: SessionService,
        throttle: LoginThrottle,
    ) -> None:
        self._hasher = hasher
        self._sessions = sessions
        self._throttle = throttle
        # Computed once at construction, not per request: it is a constant,
        # and paying ~50 ms for it on every unknown-username attempt would
        # double the cost of exactly the requests an attacker generates.
        self._dummy_hash = hasher.hash(_DUMMY_PASSWORD)

    async def login(self, db: AsyncSession, request: LoginRequest) -> IssuedSession:
        """Authenticate and issue a session.

        Raises:
            LoginRateLimitedError: before any verification work is done.
            AuthenticationFailedError: for every other failure, indistinguishably.
        """
        # Normalization can reject input (too long, control characters). A bad
        # username is an authentication failure, not a validation error: the
        # endpoint must not reveal which usernames are even well-formed.
        try:
            canonical = normalize_username(request.username)
        except (ValueError, TypeError):
            canonical = ""

        # --- 1. Throttle, before anything expensive --------------------
        # Keyed on the canonical form so that casing variants share a budget.
        # An unparseable username keys to "" so that junk input is still
        # throttled rather than being a free path.
        decision = self._throttle.check(address=request.client_ip, username_canonical=canonical)
        if not decision.allowed:
            logger.warning(
                "login_rate_limited",
                scope=decision.scope,
                retry_after_seconds=decision.retry_after_seconds,
            )
            raise LoginRateLimitedError(decision.retry_after_seconds, decision.scope)

        # --- 2. Look up the user ---------------------------------------
        user = (
            None
            if not canonical
            else (
                await db.execute(select(User).where(User.username_canonical == canonical))
            ).scalar_one_or_none()
        )

        # --- 3. Always do real verification work -----------------------
        if user is None:
            await self._hasher.verify_async(self._dummy_hash, request.password)
            logger.info("login_failed", reason="unknown_username")
            raise AuthenticationFailedError("unknown_username")

        verified = await self._hasher.verify_async(user.password_hash, request.password)

        if not verified:
            logger.info("login_failed", reason="bad_password", user_id=str(user.id))
            raise AuthenticationFailedError("bad_password")

        # --- 4. Only now consider account state ------------------------
        # After verification, so that an inactive account is not detectable by
        # how quickly it is refused.
        if not user.is_active:
            logger.warning("login_failed", reason="user_inactive", user_id=str(user.id))
            raise AuthenticationFailedError("user_inactive")

        # --- 5. Issue the session --------------------------------------
        issued = await self._sessions.create(
            db,
            user,
            client_ip=request.client_ip,
            user_agent=request.user_agent,
        )

        # Clearing the budgets only on success means a user who mistyped a few
        # times is not left throttled, while an attacker who never succeeds
        # can never clear theirs.
        self._throttle.reset(address=request.client_ip, username_canonical=canonical)

        logger.info(
            "login_succeeded",
            user_id=str(user.id),
            session_id=str(issued.session_id),
            role=user.role.value,
        )
        return issued

    async def rehash_if_needed(self, db: AsyncSession, user: User, password: str) -> bool:
        """Upgrade a stored hash to the current parameters, if it is stale.

        This is the one moment at which an upgrade is possible, because it is
        the only point where the plaintext is available. Called after a
        successful login; a failure here must never fail the login, since the
        user's credentials were correct and the old hash still works.

        Returns whether the hash was replaced. See ADR-025.
        """
        if not self._hasher.needs_rehash(user.password_hash):
            return False

        user.password_hash = await self._hasher.hash_async(password)
        await db.flush()
        logger.info("password_hash_upgraded", user_id=str(user.id))
        return True
