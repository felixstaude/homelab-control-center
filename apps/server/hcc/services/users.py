"""Administrative user management.

Phase 1, milestone 1.7. The rules that govern creating, listing and changing
accounts. Framework-free: raises typed exceptions and leaves status codes to
:mod:`hcc.api.v1.users`.

## The last-active-administrator invariant

**An ordinary management operation must never leave the installation with no
active administrator.** This is an availability property with a security
consequence: an installation with no administrator cannot create one, because
bootstrap is a one-way door that does not reopen (ADR-033) — deliberately, so
that nobody can claim an installation by emptying its user table. Locking
yourself out is therefore permanent short of database access.

### Why a count followed by an update is not enough

The obvious implementation is wrong, and it is wrong in a way testing against
one connection cannot show:

```
SELECT count(*) FROM users WHERE role = 'admin' AND is_active;   -- 2, fine
UPDATE users SET role = 'viewer' WHERE id = :target;             -- go ahead
```

Two administrators demoting each other at the same moment both read 2, both
decide they are safe, and both commit. The installation ends with zero
administrators and every check passed. Under PostgreSQL's default READ
COMMITTED isolation neither transaction sees the other's uncommitted write, so
there is nothing for either one to notice.

### What is used instead

Every operation that can reduce the set of active administrators takes a
**transaction-scoped advisory lock** on one fixed key before it reads, and
holds it until the caller commits:

```
SELECT pg_advisory_xact_lock(:key);   -- serialise with every other such op
SELECT count(*) ... ;                 -- now a post-lock, committed-state read
UPDATE ... ;
COMMIT;                               -- releases the lock
```

Because the count runs *after* the lock is held, and READ COMMITTED gives each
statement a fresh snapshot, the second transaction's count sees the first
transaction's committed demotion. It then finds zero other active
administrators and refuses. The invariant holds for any number of concurrent
attempts, and the serialisation costs nothing a human will notice: user
administration is a handful of operations a month, not a request path.

This is the same mechanism :mod:`hcc.security.bootstrap` uses for the
first-run invariant, with the same transaction-scoped lifetime — released on
commit, rollback or crash, so an interrupted attempt leaves nothing to clean
up.

The lock is taken by **every** mutating operation here, not only the ones that
could break the invariant. Two reasons: a display-name change and a role
change to the same row would otherwise be a lost-update race, and "which
operations need the lock?" is a question whose wrong answer is silent. The
cost of over-locking a low-volume administrative path is nil; the cost of
under-locking it is an unrecoverable installation.

### The honest limitation

The lock protects writers that take it, which is every write path in this
application — all of them funnel through this module. A raw ``UPDATE users``
issued directly against the database bypasses it. That is accepted rather than
solved: it requires the database credentials, and an adversary with those is
already out of scope (``docs/SECURITY.md``), while the alternatives all fail.
A unique index cannot express "at least one row exists". An exclusion
constraint cannot either. A statement-level trigger that counts has the exact
race described above unless it, too, serialises — at which point it is this
mechanism, written somewhere harder to find. Recorded as ADR-039.

## Usernames are immutable

There is no code path here that changes ``username`` or
``username_canonical``, and that is a decision rather than an omission — see
ADR-041 and :mod:`hcc.models.user`. ``display_name`` is the mutable label.

## Mass assignment is not possible

:class:`UserUpdate` enumerates the three fields an administrator may change.
There is no path from a request body to an arbitrary column: the route's
Pydantic model forbids unknown fields, it is translated into this dataclass
field by field, and nothing here accepts a dict. So ``password_hash``,
``created_at``, ``last_login_at``, ``id`` and ``username_canonical`` are not
merely unlisted — there is no mechanism that would carry them.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from hcc.logging import get_logger
from hcc.models.audit import AuditAction, AuditOutcome
from hcc.models.session import Session
from hcc.models.user import (
    User,
    UserRole,
    fold_username,
    normalize_display_name,
    normalize_username,
)

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement
    from sqlalchemy.sql.selectable import ScalarSelect

    from hcc.security.audit import AuditContext, AuditService
    from hcc.security.password import PasswordHasher
    from hcc.security.sessions import SessionService

logger = get_logger(__name__)

#: Advisory-lock key guarding the active-administrator invariant. An arbitrary
#: but fixed 64-bit constant; every writer must use this same value or the lock
#: does nothing. Greppable and stable across releases rather than derived.
#:
#: 0x48434341_00000001 — "HCCA" (Homelab Control Center Admin invariant), slot 1.
#: Deliberately distinct from ``BOOTSTRAP_LOCK_KEY`` (0x48434342…): sharing one
#: key would make a bootstrap attempt block on a role change for no reason.
ADMIN_INVARIANT_LOCK_KEY: Final = 0x4843434100000001

#: Default and maximum page size for the user list. The maximum is the half
#: that matters: without it, ``?limit=1000000`` is an authenticated request
#: that makes the server materialise the whole table.
DEFAULT_PAGE_SIZE: Final = 25
MAX_PAGE_SIZE: Final = 100

#: Longest accepted search term. A search is folded and used in a ``LIKE``, so
#: an unbounded term would be an unbounded pattern.
MAX_SEARCH_LENGTH: Final = 64


class Unset(enum.Enum):
    """Sentinel for "this field was not submitted".

    An enum singleton rather than ``object()`` because mypy narrows it: a
    field typed ``str | None | Unset`` forces the three cases to be handled
    explicitly, which is the point. ``None`` and "absent" are genuinely
    different for ``display_name`` — one clears the label, the other leaves it
    alone — and collapsing them would make it impossible to clear.
    """

    UNSET = "unset"


UNSET: Final = Unset.UNSET


@dataclass(frozen=True, slots=True)
class UserUpdate:
    """The complete set of fields an administrator may change.

    Frozen, and every field defaults to :data:`UNSET`, so a partial update
    names only what it changes. See the module docstring on mass assignment.
    """

    display_name: str | Unset | None = UNSET
    role: UserRole | Unset = UNSET
    is_active: bool | Unset = UNSET

    @property
    def is_empty(self) -> bool:
        """Whether this update would change nothing."""
        return self.display_name is UNSET and self.role is UNSET and self.is_active is UNSET


@dataclass(frozen=True, slots=True)
class UserRecord:
    """A user row plus the derived facts the API reports alongside it."""

    user: User
    #: Currently usable sessions, as :meth:`SessionService.live_session_predicate`
    #: defines usable. Computed in the listing query, not per row — see
    #: :meth:`UserService.list_users`.
    active_session_count: int


@dataclass(frozen=True, slots=True)
class UserPage:
    """One page of users, and enough context to render a pager."""

    items: Sequence[UserRecord]
    #: Total matching the filters, ignoring pagination. Needed so the
    #: interface can say "41 users" rather than "at least 25".
    total: int
    limit: int
    offset: int


# --- Failures ---------------------------------------------------------------


class UserServiceError(Exception):
    """Base class for refusals from this module.

    Every subclass carries a message safe to show an operator: it names what
    is wrong and what to do about it, and never contains a password, a hash or
    a token.
    """


class UserNotFoundError(UserServiceError):
    """No such account.

    Mapped to 404. Deliberately the same for an id that never existed and one
    that was deleted — there is nothing useful in the distinction and
    answering it would confirm that an id was once real.
    """

    def __init__(self, user_id: uuid.UUID) -> None:
        self.user_id = user_id
        super().__init__("That user account does not exist.")


class UsernameTakenError(UserServiceError):
    """The username is already in use, case-insensitively.

    Mapped to 409. The message names the username, which is safe: this is an
    administrator-only endpoint, so the caller may already list every account.
    The same disclosure on a public registration form would be a user-
    enumeration oracle, which is one reason there is no public registration.
    """

    def __init__(self, username: str) -> None:
        self.username = username
        super().__init__(
            f"The username {username!r} is already taken. Usernames are "
            "case-insensitive, so a different capitalisation is still the "
            "same account."
        )


class LastAdministratorError(UserServiceError):
    """The operation would leave no active administrator.

    Mapped to 409 with its own error code, because the interface needs to
    explain *this* specific refusal rather than show a generic conflict: the
    remedy is "promote someone else first", which the operator cannot guess
    from "conflict".
    """

    def __init__(self, operation: str) -> None:
        self.operation = operation
        super().__init__(
            f"Cannot {operation}: this is the only active administrator, and "
            "an installation with no active administrator cannot be "
            "recovered through the interface. Grant the Administrator role to "
            "another active account first."
        )


class UserService:
    """Creates, lists and changes user accounts.

    Stateless apart from its collaborators, so one instance is shared across
    requests.
    """

    __slots__ = ("_audit", "_hasher", "_sessions")

    def __init__(
        self,
        *,
        hasher: PasswordHasher,
        sessions: SessionService,
        audit: AuditService,
    ) -> None:
        self._hasher = hasher
        self._sessions = sessions
        self._audit = audit

    # --- Reading ----------------------------------------------------------

    def _active_session_count_subquery(self) -> ScalarSelect[int]:
        """Live sessions per user, as a scalar correlated subquery.

        A subquery rather than a ``LEFT JOIN`` onto a grouped derived table
        because it keeps the main query's shape identical with and without the
        count, and it is what makes the listing **two** queries regardless of
        page size. Counting per row in Python would be one query per user —
        the N+1 this exists to avoid, and one that only shows up once an
        installation has more than a handful of accounts.
        """
        return (
            select(func.count())
            .select_from(Session)
            .where(
                Session.user_id == User.id,
                self._sessions.live_session_predicate(),
            )
            .correlate(User)
            .scalar_subquery()
        )

    def _filter_clauses(
        self,
        *,
        search: str | None,
        role: UserRole | None,
        is_active: bool | None,
    ) -> list[ColumnElement[bool]]:
        """Build the list endpoint's WHERE clauses.

        Returns clauses rather than taking and returning a statement, which
        is what lets the page query and the count query share one definition
        of "matching" without either losing its row types — a helper typed to
        accept any ``Select`` erases what it was given, so the rows came back
        as ``object`` and every field access downstream had to be trusted
        rather than checked. Sharing matters either way: a total computed
        under different filters than the rows is a pager that lies.
        """
        clauses: list[ColumnElement[bool]] = []
        if search:
            # Folded with `fold_username`, not `normalize_username`: a search
            # term is not a username and must not inherit its policy. See
            # `hcc/models/user.py`.
            folded = fold_username(search)[:MAX_SEARCH_LENGTH]
            if folded:
                # Escape the LIKE metacharacters, or a term containing `%`
                # matches everything and one containing `_` matches any
                # character — a confusing result rather than a vulnerability,
                # but a confusing result in a security screen.
                escaped = folded.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                # Matched against the canonical column, which is already
                # casefolded and NFKC-normalised, so `ILIKE` would be
                # redundant work and `LIKE` is correct.
                clauses.append(User.username_canonical.like(f"%{escaped}%", escape="\\"))
        if role is not None:
            clauses.append(User.role == role)
        if is_active is not None:
            clauses.append(User.is_active.is_(is_active))
        return clauses

    async def list_users(
        self,
        db: AsyncSession,
        *,
        search: str | None = None,
        role: UserRole | None = None,
        is_active: bool | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> UserPage:
        """Return one page of accounts matching the filters.

        Two queries, always: the page and the total. Neither grows with the
        number of accounts beyond the page itself, and ``limit`` is clamped to
        :data:`MAX_PAGE_SIZE` here as well as in the route's validation —
        two layers, because this is the bound that keeps an authenticated
        caller from asking the server to materialise the whole table.

        **Ordering is by canonical username**, with ``id`` breaking ties so it
        is total. Deliberately not by creation time: an operator paging
        through accounts is reading an alphabetical list, and a newly created
        account appearing at the top would shift every subsequent page under
        them. It still shifts the pages that sort after it, which is inherent
        to offset pagination; at the scale this product targets — tens of
        accounts, one page in practice — that is the right trade against the
        cursor machinery the audit log genuinely needs.
        """
        bounded_limit = max(1, min(limit, MAX_PAGE_SIZE))
        bounded_offset = max(0, offset)

        clauses = self._filter_clauses(search=search, role=role, is_active=is_active)

        total = (
            await db.execute(select(func.count()).select_from(User).where(*clauses))
        ).scalar_one()

        rows = (
            await db.execute(
                select(User, self._active_session_count_subquery())
                .where(*clauses)
                .order_by(User.username_canonical.asc(), User.id.asc())
                .limit(bounded_limit)
                .offset(bounded_offset)
            )
        ).all()

        return UserPage(
            items=[UserRecord(user=user, active_session_count=count) for user, count in rows],
            total=total,
            limit=bounded_limit,
            offset=bounded_offset,
        )

    async def get_user(self, db: AsyncSession, user_id: uuid.UUID) -> UserRecord:
        """Return one account.

        Raises:
            UserNotFoundError: no such account.
        """
        row = (
            await db.execute(
                select(User, self._active_session_count_subquery()).where(User.id == user_id)
            )
        ).first()
        if row is None:
            raise UserNotFoundError(user_id)
        user, count = row
        return UserRecord(user=user, active_session_count=count)

    # --- The invariant ----------------------------------------------------

    async def _lock_admin_invariant(self, db: AsyncSession) -> None:
        """Serialise this transaction against every other user mutation.

        Transaction-scoped, so it is released by the caller's commit or
        rollback and an interrupted request leaves no lock behind. Must be
        taken **before** any read whose result is used to decide, or the
        decision is made from a snapshot the lock does not cover — which is
        the entire race. See the module docstring.
        """
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:key)").bindparams(key=ADMIN_INVARIANT_LOCK_KEY)
        )

    async def _count_other_active_admins(self, db: AsyncSession, *, excluding: uuid.UUID) -> int:
        """How many active administrators there are, other than ``excluding``.

        Only correct when :meth:`_lock_admin_invariant` is already held in
        this transaction. Called nowhere else.
        """
        return (
            await db.execute(
                select(func.count())
                .select_from(User)
                .where(
                    User.id != excluding,
                    User.role == UserRole.ADMIN,
                    User.is_active.is_(True),
                )
            )
        ).scalar_one()

    async def _guard_admin_invariant(
        self,
        db: AsyncSession,
        *,
        user: User,
        operation: str,
        action: AuditAction,
        context: AuditContext,
    ) -> None:
        """Enforce the invariant, recording the refusal if it fires.

        Wraps :meth:`_require_another_active_admin` so that a refusal is
        persisted rather than only raised. ``action`` is the operation that
        was *attempted*, so the trail distinguishes a blocked demotion from a
        blocked deactivation — which matters, because the two have different
        remedies and an operator reading the trail is trying to work out which
        one somebody was attempting.
        """
        try:
            await self._require_another_active_admin(db, user=user, operation=operation)
        except LastAdministratorError:
            await self._record_refusal(
                db,
                action=action,
                context=context,
                target_user_id=user.id,
                reason="last_administrator",
            )
            raise

    async def _require_another_active_admin(
        self, db: AsyncSession, *, user: User, operation: str
    ) -> None:
        """Refuse an operation that would remove the last active administrator.

        A no-op unless ``user`` is *currently* an active administrator:
        demoting a viewer, or deactivating an already-inactive administrator,
        cannot reduce the active-administrator count and so is never refused.
        Getting that condition wrong in the other direction would make the
        guard fire on operations it has no business blocking.

        Raises:
            LastAdministratorError: ``user`` is the only one left.
        """
        if user.role is not UserRole.ADMIN or not user.is_active:
            return
        if await self._count_other_active_admins(db, excluding=user.id) > 0:
            return
        raise LastAdministratorError(operation)

    # --- Writing ----------------------------------------------------------

    async def _load_for_update(self, db: AsyncSession, user_id: uuid.UUID) -> User:
        """Load a user row, locked for this transaction.

        ``FOR UPDATE`` on top of the advisory lock is belt-and-braces: the
        advisory lock already serialises every path through this module, so
        the row lock adds nothing against those. It is here for the writer
        that does not take the advisory lock — notably ``last_login_at`` on
        the login path, which updates the same row — so that a concurrent
        login cannot interleave with an administrative change to produce a row
        carrying half of each.
        """
        user = (
            await db.execute(
                select(User)
                .where(User.id == user_id)
                .with_for_update()
                # Load the locked row's *current* values over anything this
                # session already holds. Without it, an administrator editing
                # their own account would be diffed against the copy session
                # resolution loaded at the start of the request, so a
                # concurrently-changed role would be read as unchanged — the
                # lock would hold and the decision would be made from stale
                # data. See the longer note in `hcc/services/account.py`.
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if user is None:
            raise UserNotFoundError(user_id)
        return user

    async def _record_refusal(
        self,
        db: AsyncSession,
        *,
        action: AuditAction,
        context: AuditContext,
        target_user_id: uuid.UUID | None,
        reason: str,
    ) -> None:
        """Persist a record that a business rule refused an operation.

        A refusal has nothing to commit with — by definition the mutation did
        not happen — so this is the one place a service commits. It rolls back
        first, because the duplicate-username case is discovered through an
        ``IntegrityError`` that leaves the transaction unusable, and a
        rollback is a no-op in the cases where nothing was written.

        Never raises. The caller is about to raise a typed refusal and must do
        so regardless; an audit write that failed should not convert a
        deliberate 409 into a 500. Logged with the traceback so the lost
        record is visible. Same trade, and the same reasoning, as
        :meth:`hcc.security.audit.AuditService.record_denial`.
        """
        try:
            await db.rollback()
            self._audit.record(
                db,
                action=action,
                context=context,
                outcome=AuditOutcome.FAILURE,
                target_user_id=target_user_id,
                details={"reason": reason},
            )
            await db.commit()
        except Exception:
            logger.exception(
                "audit_refusal_record_failed",
                action=action.value,
                reason=reason,
            )

    async def create_user(
        self,
        db: AsyncSession,
        *,
        username: str,
        password: str,
        context: AuditContext,
        display_name: str | None = None,
        role: UserRole = UserRole.VIEWER,
        is_active: bool = True,
    ) -> User:
        """Create an account.

        ``role`` defaults to :attr:`UserRole.VIEWER`, the least privilege. The
        default is here rather than only in the route's schema so that every
        caller gets it: an account created without an explicit role must never
        come out an administrator, and a default living only in a Pydantic
        model would not bind a CLI command or a test.

        Order of operations, each for a reason:

        1. Normalize and validate the username and display name — before any
           expensive work, so bad input fails cheaply.
        2. Hash the password, which enforces the policy as a side effect and
           is therefore the single source of truth for it.
        3. Check the username is free, for a readable refusal.
        4. Insert and flush, so the unique constraint — the actual guarantee —
           is what decides under concurrency.

        The advisory lock is deliberately **not** taken: creating an account
        cannot reduce the number of active administrators, so it has no
        interaction with the invariant, and serialising it would mean holding
        a database lock across an Argon2 hash.

        Raises:
            ValueError: the username or display name is invalid.
            PasswordPolicyError: the password violates the policy. Carries
                every problem; never carries the password.
            UsernameTakenError: the canonical username already exists.
        """
        canonical = normalize_username(username)
        label = normalize_display_name(display_name)

        # Enforces the policy and raises PasswordPolicyError. Done before the
        # uniqueness check so that a request with both problems reports the
        # password one too, rather than the operator fixing the name and then
        # discovering the password was rejected as well.
        password_hash = await self._hasher.hash_async(password)

        if (
            await db.execute(select(User.id).where(User.username_canonical == canonical))
        ).scalar_one_or_none() is not None:
            # Recorded here as well as on the constraint path below. Both
            # reach the same refusal, and only one of them is the common case
            # — so recording solely in the `IntegrityError` handler meant the
            # ordinary duplicate-username attempt left no trace at all, which
            # is the attempt an operator reviewing the trail would actually be
            # looking for.
            await self._record_refusal(
                db,
                action=AuditAction.USER_CREATED,
                context=context,
                target_user_id=None,
                reason="username_taken",
            )
            raise UsernameTakenError(username)

        user = User(
            username=username,
            display_name=label,
            password_hash=password_hash,
            role=role,
            is_active=is_active,
        )
        db.add(user)

        try:
            # Assigns server-side defaults and surfaces the unique constraint
            # now rather than at the caller's commit, where the failure would
            # be attributed to whatever else is in the transaction.
            await db.flush()
        except IntegrityError:
            # Another request created the same canonical username between the
            # check above and this flush. The constraint is what makes
            # concurrent creation safe; the check above only improves the
            # message in the common case.
            await self._record_refusal(
                db,
                action=AuditAction.USER_CREATED,
                context=context,
                target_user_id=None,
                reason="username_taken",
            )
            raise UsernameTakenError(username) from None

        self._audit.record(
            db,
            action=AuditAction.USER_CREATED,
            context=context,
            target_user_id=user.id,
            details={
                # The canonical username, not the display form: this is the
                # identity the account authenticates with, and it is what a
                # later reader needs in order to match the event to an
                # account whose display name has since changed.
                "username_canonical": canonical,
                "role": role.value,
                "is_active": is_active,
                "has_display_name": label is not None,
            },
        )
        logger.info(
            "user_created",
            user_id=str(user.id),
            username_canonical=canonical,
            role=role.value,
            is_active=is_active,
            actor_user_id=str(context.actor_user_id) if context.actor_user_id else None,
        )
        return user

    async def update_user(
        self,
        db: AsyncSession,
        *,
        user_id: uuid.UUID,
        update: UserUpdate,
        context: AuditContext,
    ) -> User:
        """Apply an administrative change to an account.

        Takes the advisory lock first, then reads, then decides — the ordering
        the invariant depends on. Writes one audit event per *kind* of change,
        so that "changed their role" and "deactivated them" remain separately
        searchable even when they arrive in one request.

        Does not commit. The caller does, which is what binds the change and
        its audit events into one unit of work.

        Raises:
            UserNotFoundError: no such account.
            LastAdministratorError: the change would leave no active
                administrator.
            ValueError: the display name is invalid.
        """
        await self._lock_admin_invariant(db)
        user = await self._load_for_update(db, user_id)

        # Evaluate every refusal before applying anything, so a rejected
        # request leaves the row untouched rather than half-updated.
        if update.role is not UNSET and update.role is not UserRole.ADMIN:
            await self._guard_admin_invariant(
                db,
                user=user,
                operation="change this administrator's role",
                action=AuditAction.USER_ROLE_CHANGED,
                context=context,
            )
        if update.is_active is False:
            await self._guard_admin_invariant(
                db,
                user=user,
                operation="deactivate this administrator",
                action=AuditAction.USER_DEACTIVATED,
                context=context,
            )

        events: list[tuple[AuditAction, dict[str, Any]]] = []

        if update.display_name is not UNSET:
            previous = user.display_name
            user.display_name = update.display_name
            if user.display_name != previous:
                events.append(
                    (
                        AuditAction.USER_UPDATED,
                        # Whether a label exists, not what it says. The value
                        # is not sensitive, but an audit trail that duplicates
                        # every field it ever saw becomes a second copy of the
                        # user table with none of its constraints.
                        {"field": "display_name", "cleared": user.display_name is None},
                    )
                )

        if update.role is not UNSET and update.role is not user.role:
            events.append(
                (
                    AuditAction.USER_ROLE_CHANGED,
                    {"from_role": user.role.value, "to_role": update.role.value},
                )
            )
            user.role = update.role

        revoked = 0
        if update.is_active is not UNSET and update.is_active is not user.is_active:
            user.is_active = update.is_active
            if update.is_active:
                events.append((AuditAction.USER_REACTIVATED, {}))
            else:
                # Deactivation already stops authentication on the next
                # request, because `is_active` is re-checked every time
                # (ADR-028) — the revocation is not what makes it effective.
                # It is here so the session rows agree with reality: without
                # it, a deactivated account would keep reporting live sessions
                # in the administrative view and in its own session list.
                revoked = await self._sessions.revoke_all_for_user(db, user.id)
                events.append((AuditAction.USER_DEACTIVATED, {"sessions_revoked": revoked}))

        for action, details in events:
            self._audit.record(
                db,
                action=action,
                context=context,
                target_user_id=user.id,
                details=details or None,
            )

        if events:
            logger.info(
                "user_updated",
                user_id=str(user.id),
                actions=[action.value for action, _ in events],
                actor_user_id=str(context.actor_user_id) if context.actor_user_id else None,
            )
        await db.flush()
        return user

    async def set_active(
        self,
        db: AsyncSession,
        *,
        user_id: uuid.UUID,
        is_active: bool,
        context: AuditContext,
    ) -> User:
        """Deactivate or reactivate an account.

        A thin wrapper over :meth:`update_user` rather than a second
        implementation. The dedicated endpoints exist because "deactivate this
        account" is a distinct, confirmable action rather than a field edit —
        but routing both through one method is what guarantees the
        last-administrator guard cannot be enforced on one path and forgotten
        on the other.

        Idempotent: deactivating an inactive account is accepted and records
        nothing, because the caller's intent is already satisfied and a
        spurious audit event would make the trail misleading.
        """
        return await self.update_user(
            db, user_id=user_id, update=UserUpdate(is_active=is_active), context=context
        )

    async def revoke_sessions(
        self,
        db: AsyncSession,
        *,
        user_id: uuid.UUID,
        context: AuditContext,
    ) -> int:
        """Revoke every live session belonging to an account. Returns how many.

        Administrative, so it revokes **all** of the target's sessions
        including the one they may be using right now; that is the point of
        the action. Revoking your own sessions through this path is permitted
        and will end your own session on the next request — the interface
        confirms it rather than the server refusing it, because an
        administrator signing themselves out everywhere is a legitimate thing
        to want.

        Idempotent: an account with no live sessions returns 0 and still
        records the attempt, so "nothing happened" is distinguishable from
        "nobody tried".

        Raises:
            UserNotFoundError: no such account. Checked rather than assumed,
                so that a wrong id reports 404 instead of silently succeeding
                against nothing — a revocation that reports success without
                having revoked anything is the specific lie this must not
                tell.
        """
        if (
            await db.execute(select(User.id).where(User.id == user_id))
        ).scalar_one_or_none() is None:
            raise UserNotFoundError(user_id)

        count = await self._sessions.revoke_all_for_user(db, user_id)
        self._audit.record(
            db,
            action=AuditAction.USER_SESSIONS_REVOKED,
            context=context,
            target_user_id=user_id,
            details={"sessions_revoked": count},
        )
        logger.info(
            "user_sessions_revoked",
            user_id=str(user_id),
            count=count,
            actor_user_id=str(context.actor_user_id) if context.actor_user_id else None,
        )
        return count
