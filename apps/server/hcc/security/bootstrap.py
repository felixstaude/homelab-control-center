"""First-run bootstrap: creating the initial administrator.

Phase 1, milestone 1.5. Framework-free like the rest of :mod:`hcc.security` —
it uses SQLAlchemy but imports no FastAPI and no CLI machinery, so the same
code path serves the ``hcc bootstrap-admin`` command, a test, and any future
authenticated mechanism.

## The security model, in one sentence

Creating the first administrator requires **local access to the controller's
database**, not merely the observation that no administrator exists yet.

## Why there is no public bootstrap endpoint

The obvious design — expose ``POST /api/v1/setup`` while the ``users`` table
is empty — gives away ownership of the deployment to whoever reaches the port
first. On a homelab that is a realistic race: the control plane is often
reachable on the LAN before the operator has finished configuring it, and the
window is as long as the gap between "container started" and "operator opened
the browser". Losing that race means an attacker holds the administrator
account, and the real operator cannot tell by looking: the installation simply
appears to be already set up.

An empty table is a statement about the current user list, not proof of
anything. So bootstrap is authorized by something an attacker on the network
does not have — the ability to run a command on the host, or in the container,
with the database credentials. That is the same authority required to read
every session token and password hash anyway, so it grants nothing new.

## Why the invariant is a row, not a count

:class:`hcc.models.installation.Installation` holds one row meaning "this
installation has been initialized". Bootstrap is refused while that row
exists.

Deriving the invariant from ``SELECT count(*) FROM users`` instead would mean
bootstrap reopens whenever the last account is deleted or the administrator is
deactivated — handing ownership to whoever noticed. The row is written once
and never removed, so initialization is a one-way door.

## Why this is atomic

Two mechanisms, and only the second is load-bearing:

1. A **transaction-scoped PostgreSQL advisory lock**
   (``pg_advisory_xact_lock``) serialises bootstrap attempts across processes
   and connections. It exists so the losing attempt gets a readable
   "already initialized" error rather than an integrity error. It is released
   automatically when the transaction ends, including on crash or rollback —
   there is no lock to clean up after an interrupted attempt.

2. The **singleton primary key** on ``installation``. ``id`` is a boolean with
   ``CHECK (id)``, so a second insert violates the primary key. This holds
   even if the advisory lock is never taken, so correctness does not depend on
   every writer remembering to cooperate.

Both the user row and the installation row are written in **one transaction**,
so an interrupted attempt leaves neither: there is no state in which an
administrator exists but the installation is unmarked, or vice versa.

## What this module deliberately does not do

It does not log in, issue a session, send a password anywhere, or print one.
It takes a plaintext password, hands it to the existing
:class:`~hcc.security.password.PasswordHasher`, and keeps no reference to it.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from hcc.logging import get_logger
from hcc.models.installation import SINGLETON_ID, Installation
from hcc.models.user import User, UserRole, normalize_username

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from hcc.security.password import PasswordHasher

logger = get_logger(__name__)

#: Advisory-lock key for bootstrap. An arbitrary but fixed 64-bit constant;
#: every process that bootstraps must use this same value or the lock does
#: nothing. Namespaced in a comment rather than derived from a hash so it is
#: greppable and stable across releases.
#:
#: 0x48434342_00000001 — "HCCB" (Homelab Control Center Bootstrap), slot 1.
BOOTSTRAP_LOCK_KEY: Final = 0x4843434200000001


class BootstrapMethod(enum.StrEnum):
    """How an installation was initialized.

    Recorded on the installation row. ``CLI`` is the only member today; the
    enum exists so that a future mechanism is distinguishable in the record
    rather than indistinguishable from a local command.
    """

    CLI = "cli"


class BootstrapError(Exception):
    """Base class for bootstrap failures.

    Every subclass carries a message safe to show an operator: it names what
    is wrong and what to do, and never contains a password, a hash or a
    connection string.
    """


class AlreadyInitializedError(BootstrapError):
    """The installation has already been initialized.

    Raised whether or not any user currently exists, because the invariant is
    the installation row and not the user list — see the module docstring.
    """

    def __init__(self, initialized_at: datetime) -> None:
        self.initialized_at = initialized_at
        super().__init__(
            "This installation was already initialized at "
            f"{initialized_at.isoformat()}. Creating another initial "
            "administrator is refused. Use an existing administrator account, "
            "or see docs/DEVELOPMENT.md for recovery options."
        )


class ConcurrentBootstrapError(BootstrapError):
    """Another bootstrap attempt won the race.

    Distinct from :class:`AlreadyInitializedError` only in how it was
    discovered — the database refused the insert rather than the pre-check
    finding a row. Both mean the same thing to an operator, and both leave the
    installation initialized exactly once.
    """

    def __init__(self) -> None:
        super().__init__(
            "Another bootstrap attempt completed first, so this one was "
            "refused. The installation is initialized; no second "
            "administrator was created."
        )


class UsernameTakenError(BootstrapError):
    """The requested username already exists.

    Reachable only in the narrow case where users exist but the installation
    row does not — for example a database restored from a backup taken before
    this milestone. Reported distinctly because "pick another name" is a
    different instruction from "this is already initialized".
    """

    def __init__(self, username: str) -> None:
        self.username = username
        super().__init__(f"The username {username!r} is already taken.")


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """What was created. Carries no credential material."""

    user_id: uuid.UUID
    username: str
    username_canonical: str
    role: UserRole
    initialized_at: datetime


def _violated_constraint(error: IntegrityError) -> str | None:
    """Name the constraint an integrity error violated, if the driver says.

    asyncpg exposes ``constraint_name`` on the exception; psycopg puts it on a
    ``diag`` object. Both are checked so the log line stays useful if the
    driver is ever swapped, and neither the message nor any row value is read.
    """
    original: object = error.orig
    direct = getattr(original, "constraint_name", None)
    if isinstance(direct, str):
        return direct
    diagnostics = getattr(original, "diag", None)
    from_diag = getattr(diagnostics, "constraint_name", None)
    return from_diag if isinstance(from_diag, str) else None


async def is_initialized(db: AsyncSession) -> datetime | None:
    """Return when the installation was initialized, or ``None``.

    A plain read with no locking: safe for a status check, and deliberately
    **not** what :func:`bootstrap_administrator` relies on to decide. Checking
    here and inserting later would be exactly the race this module exists to
    avoid.
    """
    return (
        await db.execute(select(Installation.initialized_at).where(Installation.id.is_(True)))
    ).scalar_one_or_none()


async def count_users(db: AsyncSession) -> int:
    """Return the number of user accounts.

    For diagnostics only — the CLI reports it so an operator can tell "fresh
    database" from "restored backup with users but no installation row". It is
    not an authorization input.
    """
    return (await db.execute(select(func.count()).select_from(User))).scalar_one()


async def bootstrap_administrator(
    db: AsyncSession,
    *,
    username: str,
    password: str,
    hasher: PasswordHasher,
    method: BootstrapMethod = BootstrapMethod.CLI,
) -> BootstrapResult:
    """Create the initial administrator, exactly once.

    The caller provides an :class:`~sqlalchemy.ext.asyncio.AsyncSession` and
    **must not have an open transaction doing other work**: this function
    commits, because the user row and the installation row have to land
    together or not at all.

    Order of operations, each step for a reason:

    1. Normalize and validate the username, and validate the password against
       the existing policy — before taking any lock, so bad input fails fast
       and cheaply and cannot hold the lock while Argon2 runs.
    2. Hash the password. Deliberately outside the lock: hashing takes tens of
       milliseconds and holding a database lock across it is pointless.
    3. Take the advisory lock, re-read the installation row, insert both rows,
       commit.

    Raises:
        ValueError: the username violates
            :func:`~hcc.models.user.normalize_username`.
        PasswordPolicyError: the password violates the policy. Carries every
            problem, never the password.
        AlreadyInitializedError: the installation row already exists.
        ConcurrentBootstrapError: another attempt committed first.
        UsernameTakenError: the username exists although the installation is
            not marked initialized.
    """
    # --- 1. Validate before doing anything expensive or exclusive ---------
    # normalize_username raises ValueError with an actionable message. The
    # display form keeps the operator's capitalisation (ADR-014).
    canonical = normalize_username(username)

    # Hashing enforces the password policy itself, and raises
    # PasswordPolicyError listing every problem. Calling it here rather than
    # validating separately keeps one source of truth for the policy.

    # --- 2. Hash outside the lock ----------------------------------------
    password_hash = await hasher.hash_async(password)

    # --- 3. The exclusive section ----------------------------------------
    try:
        # Transaction-scoped: released at COMMIT or ROLLBACK, so an
        # interrupted attempt leaves no lock behind. Serialises attempts so
        # the loser gets AlreadyInitializedError rather than IntegrityError;
        # the singleton primary key is the actual guarantee.
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:key)").bindparams(key=BOOTSTRAP_LOCK_KEY)
        )

        # Re-read *inside* the lock. The read in the CLI's pre-flight check is
        # advisory; this one decides.
        existing = await is_initialized(db)
        if existing is not None:
            # `try` is the point: the `except BaseException` branch rolls the
            # transaction back, which releases the advisory lock. Raising
            # outside it — what TRY301 asks for — would leave an open
            # transaction holding the lock for the rest of the session, and a
            # later bootstrap attempt would block forever.
            raise AlreadyInitializedError(existing)  # noqa: TRY301

        if (
            await db.execute(select(User.id).where(User.username_canonical == canonical))
        ).scalar_one_or_none() is not None:
            raise UsernameTakenError(username)  # noqa: TRY301

        now = datetime.now(UTC)
        user = User(
            username=username,
            password_hash=password_hash,
            role=UserRole.ADMIN,
            is_active=True,
        )
        db.add(user)
        # Assigns the application-generated UUID (ADR-016) so the installation
        # row can reference it in the same transaction without a round trip.
        await db.flush()

        db.add(
            Installation(
                id=SINGLETON_ID,
                initialized_at=now,
                initialized_by_user_id=user.id,
                bootstrap_method=method.value,
            )
        )

        # One commit for both rows. Either the installation is initialized
        # with an administrator, or nothing happened.
        await db.commit()

    except IntegrityError as exc:
        # The singleton primary key refused a second installation row, or the
        # username unique constraint refused a duplicate. Either way nothing
        # from this attempt persists.
        await db.rollback()
        logger.warning(
            "bootstrap_refused",
            reason="integrity_conflict",
            # The constraint name, not the driver's message: a message can
            # quote the row values that violated the constraint.
            constraint=_violated_constraint(exc),
        )
        raise ConcurrentBootstrapError from exc
    except BaseException:
        # Includes BootstrapError, ValueError and cancellation. Rolling back
        # explicitly means a refused attempt never leaves a dangling
        # transaction holding the advisory lock for the rest of the session.
        await db.rollback()
        raise

    logger.info(
        "bootstrap_completed",
        user_id=str(user.id),
        # The canonical username is not a secret and is what an operator needs
        # in order to log in. No password material is logged here or anywhere.
        username_canonical=canonical,
        role=UserRole.ADMIN.value,
        method=method.value,
    )

    return BootstrapResult(
        user_id=user.id,
        username=user.username,
        username_canonical=canonical,
        role=UserRole.ADMIN,
        initialized_at=now,
    )
