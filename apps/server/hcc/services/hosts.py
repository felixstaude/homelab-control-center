"""Administrative host management and enrollment issuance.

Phase 1, milestone 1.8. The rules that govern registering hosts, enabling and
disabling them, and issuing the one-time credentials an agent enrolls with.
Framework-free: raises typed exceptions and leaves status codes to
:mod:`hcc.api.v1.hosts`.

The agent-facing half — consuming an enrollment credential, authenticating a
connection — is :mod:`hcc.services.enrollment`. The split follows the
boundary that matters: everything here is reached by an authenticated
administrator through the browser, and everything there is reached by an agent
presenting a secret. One module per trust level, so a reader never has to work
out which kind of caller a function serves.

## The enrollment credential is shown exactly once

:meth:`HostService.issue_enrollment` returns the plaintext credential, and that
return value is the only time it exists outside the agent's filesystem. Only
its SHA-256 digest is stored (ADR-042), so there is no path — not a GET, not a
list, not an error trace — that can produce it again. An administrator who
loses it issues another one.

This is why the issuing method returns a dataclass carrying both the row and
the plaintext rather than just the row: a caller that forgot to capture the
secret gets a type error, not a silently unusable token.

## Issuing revokes the previous pending credential

Deliberately, and it is the sort of helpfulness that is actually a security
property. If two pending credentials could exist for one host, "the enrollment
credential" would be ambiguous — the administrator sees the new one on screen
while an older one, read over somebody's shoulder last week, is still live.
Issuing therefore revokes any still-pending credential for that host in the
same transaction, so what is on screen is what works and nothing else does.

## Re-enrolling an enrolled host must be asked for explicitly

A host that already has a live agent credential refuses to issue an enrollment
credential unless the caller passes ``allow_reenrollment=True``. The reason is
not paperwork: consuming an enrollment credential revokes the host's existing
credential, so an enrollment credential issued against a working host is a
loaded gun pointed at a running agent. Requiring the caller to say so turns
"I clicked the wrong row" into a refusal instead of an outage.

## Why the advisory lock is per host, not global

:mod:`hcc.services.users` serialises **every** user mutation on one key,
because the invariant it protects is installation-wide ("at least one active
administrator") and user administration is a handful of operations a month.

Neither holds here. The invariants are per host — at most one live credential,
at most one pending enrollment — and an agent fleet reconnecting after a
controller restart is a burst of concurrent writes, not a human clicking
buttons. A global lock would serialise the whole fleet behind one mutex at
exactly the moment it is busiest. So the key is derived from the host id, and
two hosts never wait on each other. See ADR-044.
"""

from __future__ import annotations

import enum
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, cast

from sqlalchemy import CursorResult, func, select, text
from sqlalchemy import update as sql_update
from sqlalchemy.exc import IntegrityError

from hcc.logging import get_logger
from hcc.models.audit import AuditAction, AuditOutcome
from hcc.models.host import (
    HEARTBEAT_GRACE_SECONDS,
    AgentCredential,
    Host,
    HostConnectivity,
    HostEnrollment,
    HostLifecycle,
    canonical_host_name,
    normalize_host_name,
)
from hcc.security.tokens import generate_token, hash_token

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement
    from sqlalchemy.sql.selectable import ScalarSelect

    from hcc.security.audit import AuditContext, AuditService

logger = get_logger(__name__)

#: Advisory-lock class for per-host serialisation. The two-argument form of
#: ``pg_advisory_xact_lock`` takes ``(classid, objid)``, so this constant
#: separates host locks from any other advisory lock the application might
#: take with a colliding object id.
#:
#: 0x48434348 — "HCCH" (Homelab Control Center Host). Deliberately distinct
#: from the single-key locks in :mod:`hcc.services.users` and
#: :mod:`hcc.security.bootstrap`, which use the one-argument form and
#: therefore occupy a different lock space entirely.
HOST_LOCK_CLASS: Final = 0x48434348

#: Default and maximum page size for the host list. The maximum is the half
#: that matters: without it, ``?limit=1000000`` is an authenticated request
#: that makes the server materialise every host.
DEFAULT_PAGE_SIZE: Final = 25
MAX_PAGE_SIZE: Final = 100

#: Longest accepted search term. A search is folded and used in a ``LIKE``, so
#: an unbounded term would be an unbounded pattern.
MAX_SEARCH_LENGTH: Final = 64

#: Largest accepted ``offset``. Not a product limit — nobody pages a million
#: hosts deep — but a bound on what reaches the database: Python integers are
#: unbounded, and an out-of-range bigint is an unhandled 500 produced by one
#: query parameter. Same reasoning as :mod:`hcc.services.users`.
MAX_OFFSET: Final = 1_000_000

#: Fallback enrollment lifetime, used when no settings object is supplied.
#: The configured value is ``agent_enrollment_ttl_seconds``.
DEFAULT_ENROLLMENT_TTL_SECONDS: Final = 900


class Unset(enum.Enum):
    """Sentinel for "this field was not submitted".

    An enum singleton rather than ``object()`` because mypy narrows it, which
    forces the three cases of ``str | None | Unset`` to be handled explicitly.
    ``None`` and "absent" are genuinely different for ``description`` — one
    clears the note, the other leaves it alone — and collapsing them would
    make it impossible to clear. Same device as :mod:`hcc.services.users`.
    """

    UNSET = "unset"


UNSET: Final = Unset.UNSET


@dataclass(frozen=True, slots=True)
class HostUpdate:
    """The complete set of fields an administrator may change on a host.

    Frozen, and every field defaults to :data:`UNSET`, so a partial update
    names only what it changes.

    There is no path from a request body to an arbitrary column: the route's
    Pydantic model forbids unknown fields, it is translated into this
    dataclass field by field, and nothing here accepts a dict. So
    ``lifecycle``, ``connection_epoch``, ``last_heartbeat_at`` and
    ``name_canonical`` are not merely unlisted — there is no mechanism that
    would carry them.

    ``name`` **is** mutable, unlike ``User.username`` (ADR-041). The two look
    alike and are not: a username is an identity someone authenticates with,
    while a host name is a label the operator reads. A host's identity is its
    id, which is what the agent credential is bound to, so renaming a host
    cannot affect what any agent can do.
    """

    name: str | Unset = UNSET
    description: str | Unset | None = UNSET
    environment: str | Unset | None = UNSET

    @property
    def is_empty(self) -> bool:
        """Whether this update would change nothing."""
        return self.name is UNSET and self.description is UNSET and self.environment is UNSET


@dataclass(frozen=True, slots=True)
class HostRecord:
    """A host row plus the derived facts the API reports alongside it.

    Derived rather than stored, and computed against one instant for the whole
    page — see :meth:`HostService.list_hosts`.
    """

    host: Host
    #: Computed from ``current_connection_id`` and ``last_heartbeat_at``. Never
    #: read from a column, because there is no such column (ADR-043).
    connectivity: HostConnectivity
    #: Whether an unused, unexpired enrollment credential exists. A boolean,
    #: not the credential and not its digest: the interface needs to say
    #: "enrollment pending", and nothing more is safe to disclose.
    has_pending_enrollment: bool
    #: When the host's live agent credential was issued, or ``None`` when it
    #: has none. Lets the interface distinguish "never enrolled" from
    #: "credential revoked" without exposing anything about the credential.
    credential_issued_at: datetime | None


@dataclass(frozen=True, slots=True)
class HostPage:
    """One page of hosts, and enough context to render a pager."""

    items: Sequence[HostRecord]
    #: Total matching the filters, ignoring pagination, so the interface can
    #: say "41 hosts" rather than "at least 25".
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True, repr=False)
class IssuedEnrollment:
    """A freshly issued enrollment credential, and the secret itself.

    ``credential`` is the **only** copy outside the agent's filesystem, and it
    exists for the duration of one response. Returning it alongside the row,
    rather than returning the row and expecting the caller to have kept the
    secret, is what makes "show it once" a type-level fact instead of a
    convention.
    """

    enrollment: HostEnrollment
    #: Plaintext. Never logged, never stored, never returned again.
    credential: str
    expires_at: datetime
    #: How many previously-pending credentials this issuance revoked. Reported
    #: so the interface can say "the previous credential no longer works",
    #: which an administrator needs to know and would otherwise discover by
    #: watching an agent fail.
    superseded: int

    def __repr__(self) -> str:
        """Representation with the credential redacted.

        A dataclass ``repr`` is exactly what ends up in a log line or a
        traceback, and the generated one would print the plaintext — hence
        ``repr=False`` on the decorator, without which the decorator would
        overwrite this method and the redaction would silently not happen.
        """
        return (
            f"IssuedEnrollment(enrollment_id={self.enrollment.id!r}, "
            f"credential=<redacted>, expires_at={self.expires_at!r}, "
            f"superseded={self.superseded!r})"
        )

    __str__ = __repr__


# --- Failures ---------------------------------------------------------------


class HostServiceError(Exception):
    """Base class for refusals from this module.

    Every subclass carries a message safe to show an operator: it names what
    is wrong and what to do about it, and never contains an enrollment
    credential, an agent credential or a digest.
    """


class HostNotFoundError(HostServiceError):
    """No such host.

    Mapped to 404. Deliberately the same for an id that never existed and one
    that was deleted.
    """

    def __init__(self, host_id: uuid.UUID) -> None:
        self.host_id = host_id
        super().__init__("That host does not exist.")


class HostNameTakenError(HostServiceError):
    """The host name is already in use, case-insensitively.

    Mapped to 409. The message names the host, which is safe: this is an
    administrator-only endpoint, so the caller may already list every host.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(
            f"The host name {name!r} is already in use. Host names are "
            "case-insensitive, so a different capitalisation is still the "
            "same host."
        )


class HostAlreadyEnrolledError(HostServiceError):
    """The host has a live agent credential and re-enrollment was not requested.

    Mapped to 409 with its own code, because the remedy is a decision rather
    than a correction: enrolling again revokes the credential the running
    agent is using, so the interface has to confirm that is what was meant.
    """

    def __init__(self, host_id: uuid.UUID) -> None:
        self.host_id = host_id
        super().__init__(
            "This host already has an enrolled agent. Issuing a new "
            "enrollment credential will revoke the existing one and "
            "disconnect that agent until it is re-enrolled. Confirm "
            "re-enrollment to proceed."
        )


class NoLiveCredentialError(HostServiceError):
    """There is no live agent credential to revoke.

    Mapped to 409. Refused rather than reported as a cheerful success,
    because "revoked" and "there was nothing to revoke" are different answers
    and an interface that conflates them tells an operator their host is
    secured when nothing happened.
    """

    def __init__(self, host_id: uuid.UUID) -> None:
        self.host_id = host_id
        super().__init__("This host has no active agent credential, so there is nothing to revoke.")


class HostService:
    """Registers, lists and changes hosts, and issues enrollment credentials.

    Stateless apart from its collaborators, so one instance is shared across
    requests.
    """

    __slots__ = ("_audit", "_enrollment_ttl", "_heartbeat_grace")

    def __init__(
        self,
        *,
        audit: AuditService,
        enrollment_ttl_seconds: int = DEFAULT_ENROLLMENT_TTL_SECONDS,
        heartbeat_grace_seconds: int = HEARTBEAT_GRACE_SECONDS,
    ) -> None:
        self._audit = audit
        self._enrollment_ttl = timedelta(seconds=enrollment_ttl_seconds)
        self._heartbeat_grace = heartbeat_grace_seconds

    # --- Reading ----------------------------------------------------------

    def _pending_enrollment_subquery(self, now: datetime) -> ScalarSelect[int]:
        """Whether a usable enrollment credential exists, per host.

        A correlated scalar subquery rather than a join onto a grouped derived
        table, for the reason :mod:`hcc.services.users` gives: it keeps the
        main query's shape identical with and without the flag, and it makes
        the listing a fixed number of queries regardless of page size.
        Computing it per row in Python would be one query per host.
        """
        return (
            select(func.count())
            .select_from(HostEnrollment)
            .where(
                HostEnrollment.host_id == Host.id,
                HostEnrollment.consumed_at.is_(None),
                HostEnrollment.revoked_at.is_(None),
                HostEnrollment.expires_at > now,
            )
            .correlate(Host)
            .scalar_subquery()
        )

    def _live_credential_issued_at_subquery(self) -> ScalarSelect[datetime]:
        """When the host's live credential was issued, or null.

        ``max`` over a set the partial unique index guarantees holds at most
        one row. The aggregate is there so the subquery is scalar whether or
        not that row exists, rather than relying on a ``LIMIT 1`` whose
        correctness would depend on an ordering nothing specifies.
        """
        return (
            select(func.max(AgentCredential.created_at))
            .select_from(AgentCredential)
            .where(
                AgentCredential.host_id == Host.id,
                AgentCredential.revoked_at.is_(None),
            )
            .correlate(Host)
            .scalar_subquery()
        )

    def _filter_clauses(
        self,
        *,
        search: str | None,
        lifecycle: HostLifecycle | None,
        is_enabled: bool | None,
    ) -> list[ColumnElement[bool]]:
        """Build the list endpoint's WHERE clauses.

        Returns clauses rather than taking and returning a statement, so the
        page query and the count query share one definition of "matching"
        without either losing its row types. Sharing matters either way: a
        total computed under different filters than the rows is a pager that
        lies.
        """
        clauses: list[ColumnElement[bool]] = []
        if search:
            folded = _fold_search(search)[:MAX_SEARCH_LENGTH]
            if folded:
                # Escape the LIKE metacharacters, or a term containing `%`
                # matches every host and one containing `_` matches any
                # character — a confusing result rather than a vulnerability.
                escaped = folded.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                # Matched against the canonical column, which is already
                # casefolded and NFKC-normalised, so `ILIKE` would be
                # redundant work and `LIKE` is correct.
                clauses.append(Host.name_canonical.like(f"%{escaped}%", escape="\\"))
        if lifecycle is not None:
            clauses.append(Host.lifecycle == lifecycle.value)
        if is_enabled is not None:
            clauses.append(Host.is_enabled.is_(is_enabled))
        return clauses

    async def list_hosts(
        self,
        db: AsyncSession,
        *,
        search: str | None = None,
        lifecycle: HostLifecycle | None = None,
        is_enabled: bool | None = None,
        connectivity: HostConnectivity | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
        now: datetime | None = None,
    ) -> HostPage:
        """Return one page of hosts matching the filters.

        **One instant for the whole page.** ``now`` is captured once and used
        for every connectivity derivation and for the pending-enrollment
        subquery. Reading the clock per row would mean two hosts in the same
        response were judged against timestamps milliseconds apart, so a host
        exactly on the grace boundary could render differently in two
        otherwise identical responses.

        **Ordering is by canonical name**, with ``id`` breaking ties so it is
        total. Deliberately not by connectivity or by creation time: an
        operator scanning for a host is reading an alphabetical list, and an
        order that moved rows as agents connected and dropped would be
        unusable — the row you were about to click would not be there.

        The ``connectivity`` filter is applied **in Python**, after the page
        is fetched, and that is a documented limitation rather than an
        oversight. Connectivity is derived from two columns and a clock, so
        filtering it in SQL would mean duplicating the derivation as a WHERE
        clause — two implementations of the one rule that decides whether the
        dashboard claims a host is up, free to disagree. The cost is that a
        connectivity-filtered page can return fewer than ``limit`` rows, which
        the response reports honestly through ``total``. At this product's
        scale — tens of hosts — it is the right trade; when it stops being
        one, the fix is a stored projection refreshed on heartbeat, not a
        second copy of the rule.
        """
        moment = now or datetime.now(UTC)
        bounded_limit = max(1, min(limit, MAX_PAGE_SIZE))
        bounded_offset = max(0, offset)

        clauses = self._filter_clauses(search=search, lifecycle=lifecycle, is_enabled=is_enabled)

        total = (
            await db.execute(select(func.count()).select_from(Host).where(*clauses))
        ).scalar_one()

        rows = (
            await db.execute(
                select(
                    Host,
                    self._pending_enrollment_subquery(moment),
                    self._live_credential_issued_at_subquery(),
                )
                .where(*clauses)
                .order_by(Host.name_canonical.asc(), Host.id.asc())
                .limit(bounded_limit)
                .offset(bounded_offset)
            )
        ).all()

        items = [
            HostRecord(
                host=host,
                connectivity=host.connectivity(moment, grace_seconds=self._heartbeat_grace),
                has_pending_enrollment=pending > 0,
                credential_issued_at=issued_at,
            )
            for host, pending, issued_at in rows
        ]
        if connectivity is not None:
            items = [record for record in items if record.connectivity is connectivity]

        return HostPage(items=items, total=total, limit=bounded_limit, offset=bounded_offset)

    async def get_host(
        self, db: AsyncSession, host_id: uuid.UUID, *, now: datetime | None = None
    ) -> HostRecord:
        """Return one host.

        Raises:
            HostNotFoundError: no such host.
        """
        moment = now or datetime.now(UTC)
        row = (
            await db.execute(
                select(
                    Host,
                    self._pending_enrollment_subquery(moment),
                    self._live_credential_issued_at_subquery(),
                ).where(Host.id == host_id)
            )
        ).first()
        if row is None:
            raise HostNotFoundError(host_id)
        host, pending, issued_at = row
        return HostRecord(
            host=host,
            connectivity=host.connectivity(moment, grace_seconds=self._heartbeat_grace),
            has_pending_enrollment=pending > 0,
            credential_issued_at=issued_at,
        )

    # --- Locking ----------------------------------------------------------

    async def _lock_host(self, db: AsyncSession, host_id: uuid.UUID) -> None:
        """Serialise this transaction against other writes to the same host.

        Transaction-scoped, so it is released by the caller's commit or
        rollback and an interrupted request leaves no lock behind. Must be
        taken **before** any read whose result is used to decide, or the
        decision is made from a snapshot the lock does not cover — which is
        the whole race (ADR-039).

        The object id is ``hashtext(host_id::text)``, computed by PostgreSQL
        rather than in Python. Two reasons: it is stable across processes and
        releases without this module having to define a hash, and every
        participant computes it the same way by construction — a lock key
        derived differently by two callers is a lock that does nothing.

        A hash collision between two host ids would make them share a lock,
        which costs a little unnecessary serialisation and breaks no
        invariant: the correctness guarantees are the partial unique index and
        the conditional updates, and the lock only makes the outcome
        deterministic rather than a retry.
        """
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:class, hashtext(:host_id))").bindparams(
                **{"class": HOST_LOCK_CLASS, "host_id": str(host_id)}
            )
        )

    async def _load_for_update(self, db: AsyncSession, host_id: uuid.UUID) -> Host:
        """Load a host row, locked for this transaction.

        ``populate_existing`` is not optional. Without it, the locked row's
        values are discarded in favour of whatever this session's identity map
        already holds — so a concurrent change would be read as unchanged, the
        lock would be held, and the decision would still be made from stale
        data (ADR-040).
        """
        host = (
            await db.execute(
                select(Host)
                .where(Host.id == host_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if host is None:
            raise HostNotFoundError(host_id)
        return host

    async def _record_refusal(
        self,
        db: AsyncSession,
        *,
        action: AuditAction,
        context: AuditContext,
        host_id: uuid.UUID | None,
        reason: str,
    ) -> None:
        """Persist a record that a business rule refused an operation.

        A refusal has nothing to commit with — by definition the mutation did
        not happen — so this is the one place this service commits. It rolls
        back first, because the duplicate-name case is discovered through an
        ``IntegrityError`` that leaves the transaction unusable, and a rollback
        is a no-op in the cases where nothing was written.

        Never raises. The caller is about to raise a typed refusal and must do
        so regardless; an audit write that failed should not convert a
        deliberate 409 into a 500. Same trade, and the same reasoning, as
        :meth:`hcc.security.audit.AuditService.record_denial`.
        """
        try:
            await db.rollback()
            self._audit.record(
                db,
                action=action,
                context=context,
                outcome=AuditOutcome.FAILURE,
                target_host_id=host_id,
                details={"reason": reason},
            )
            await db.commit()
        except Exception:
            logger.exception("audit_refusal_record_failed", action=action.value, reason=reason)

    # --- Writing ----------------------------------------------------------

    async def create_host(
        self,
        db: AsyncSession,
        *,
        name: str,
        context: AuditContext,
        description: str | None = None,
        environment: str | None = None,
    ) -> Host:
        """Register a host.

        The new host is ``registered`` and enabled, and has **no** enrollment
        credential: creating the record and authorising an agent to claim it
        are two separate decisions, and bundling them would mean every
        accidental host creation also minted a live secret.

        No advisory lock. Creating a host touches no other host's invariants,
        and the unique constraint on ``name_canonical`` — not a check in this
        method — is what makes concurrent creation safe.

        Raises:
            ValueError: the name is invalid.
            HostNameTakenError: the canonical name already exists.
        """
        stored_name = normalize_host_name(name)
        canonical = canonical_host_name(name)

        if (
            await db.execute(select(Host.id).where(Host.name_canonical == canonical))
        ).scalar_one_or_none() is not None:
            # Recorded here as well as on the constraint path below. Both reach
            # the same refusal and only one is the common case, so recording
            # solely in the `IntegrityError` handler would leave the ordinary
            # duplicate-name attempt with no trace at all.
            await self._record_refusal(
                db,
                action=AuditAction.HOST_CREATED,
                context=context,
                host_id=None,
                reason="name_taken",
            )
            raise HostNameTakenError(stored_name)

        host = Host(
            name=stored_name,
            description=_clean_optional(description),
            environment=_clean_optional(environment),
            lifecycle=HostLifecycle.REGISTERED,
            is_enabled=True,
        )
        db.add(host)

        try:
            # Assigns server-side defaults and surfaces the unique constraint
            # now rather than at the caller's commit, where the failure would
            # be attributed to whatever else is in the transaction.
            await db.flush()
        except IntegrityError:
            # Another request created the same canonical name between the check
            # above and this flush. The constraint is what makes concurrent
            # creation safe; the check above only improves the message.
            await self._record_refusal(
                db,
                action=AuditAction.HOST_CREATED,
                context=context,
                host_id=None,
                reason="name_taken",
            )
            raise HostNameTakenError(stored_name) from None

        self._audit.record(
            db,
            action=AuditAction.HOST_CREATED,
            context=context,
            target_host_id=host.id,
            details={
                # The canonical name, not the display form: it is the stable
                # key, and it is what lets a later reader match the event to a
                # host that has since been renamed.
                "name_canonical": canonical,
                "has_description": host.description is not None,
                "environment": host.environment,
            },
        )
        logger.info(
            "host_created",
            host_id=str(host.id),
            name_canonical=canonical,
            actor_user_id=str(context.actor_user_id) if context.actor_user_id else None,
        )
        return host

    async def update_host(
        self,
        db: AsyncSession,
        *,
        host_id: uuid.UUID,
        update: HostUpdate,
        context: AuditContext,
    ) -> Host:
        """Apply an administrative change to a host's labels.

        Changes labels only. There is no field here that can alter enrollment
        state, connectivity or the connection epoch — see :class:`HostUpdate`.

        Does not commit. The caller does, which is what binds the change and
        its audit event into one unit of work.

        Raises:
            HostNotFoundError: no such host.
            HostNameTakenError: the new name collides with another host.
            ValueError: the name is invalid.
        """
        await self._lock_host(db, host_id)
        host = await self._load_for_update(db, host_id)

        changed: dict[str, Any] = {}
        # Captured before anything is applied, because the refusal path below
        # rolls back — after which `host` is expired and reading its name
        # would emit a query against a dead transaction.
        requested_name = host.name

        if update.name is not UNSET:
            previous_canonical = host.name_canonical
            # Assignment normalises and re-derives the canonical name, so an
            # invalid name raises here rather than at flush.
            host.name = update.name
            requested_name = host.name
            if host.name_canonical != previous_canonical:
                changed["name_canonical"] = host.name_canonical

        if update.description is not UNSET:
            cleaned = _clean_optional(update.description)
            if cleaned != host.description:
                host.description = cleaned
                # Whether a note exists, not what it says: an audit trail that
                # duplicated every field it saw would be a second copy of this
                # table with none of its constraints.
                changed["description_cleared"] = cleaned is None

        if update.environment is not UNSET:
            cleaned = _clean_optional(update.environment)
            if cleaned != host.environment:
                host.environment = cleaned
                changed["environment"] = cleaned

        if not changed:
            # Nothing to record. A no-op is not an error — the caller's intent
            # is already satisfied — but a spurious audit event would make the
            # trail misleading.
            return host

        self._audit.record(
            db,
            action=AuditAction.HOST_UPDATED,
            context=context,
            target_host_id=host.id,
            details=changed,
        )
        logger.info(
            "host_updated",
            host_id=str(host.id),
            fields=sorted(changed),
            actor_user_id=str(context.actor_user_id) if context.actor_user_id else None,
        )

        try:
            await db.flush()
        except IntegrityError:
            await self._record_refusal(
                db,
                action=AuditAction.HOST_UPDATED,
                context=context,
                host_id=host_id,
                reason="name_taken",
            )
            raise HostNameTakenError(requested_name) from None
        return host

    async def set_enabled(
        self,
        db: AsyncSession,
        *,
        host_id: uuid.UUID,
        is_enabled: bool,
        context: AuditContext,
    ) -> Host:
        """Allow or refuse this host's agent at the door.

        Disabling does **not** revoke the credential, and that distinction is
        the point of having two axes. Disabling is "I do not want this host
        connected right now" — a maintenance window, a machine being moved —
        and it is reversible by flipping the flag back. Revoking is "that
        secret is no longer trusted", and it is reversible only by
        re-enrolling the agent.

        It also does not wait for the agent to notice: the connection is
        dropped and subsequent authentication attempts are refused while the
        flag is off (:mod:`hcc.services.enrollment`).

        Idempotent: enabling an enabled host is accepted and records nothing,
        because the caller's intent is already satisfied.

        Raises:
            HostNotFoundError: no such host.
        """
        await self._lock_host(db, host_id)
        host = await self._load_for_update(db, host_id)

        if host.is_enabled is is_enabled:
            return host

        host.is_enabled = is_enabled
        action = AuditAction.HOST_ENABLED if is_enabled else AuditAction.HOST_DISABLED
        self._audit.record(db, action=action, context=context, target_host_id=host.id, details=None)
        logger.info(
            "host_enabled_changed",
            host_id=str(host.id),
            is_enabled=is_enabled,
            actor_user_id=str(context.actor_user_id) if context.actor_user_id else None,
        )
        await db.flush()
        return host

    # --- Enrollment credentials -------------------------------------------

    async def issue_enrollment(
        self,
        db: AsyncSession,
        *,
        host_id: uuid.UUID,
        context: AuditContext,
        allow_reenrollment: bool = False,
        now: datetime | None = None,
    ) -> IssuedEnrollment:
        """Mint a one-time enrollment credential for a host.

        The returned plaintext is the only copy. Only its SHA-256 digest is
        stored (ADR-042), so no later request can produce it — including this
        one, retried.

        Order of operations, each load-bearing:

        1. Lock the host, so two administrators issuing at once produce one
           pending credential rather than two.
        2. Check for a live agent credential, and refuse unless
           ``allow_reenrollment`` — because consuming this credential will
           revoke that one.
        3. Revoke any still-pending credential for this host, so what is on
           screen is the only thing that works.
        4. Generate, hash, insert.

        Does not commit. The caller does, which is what makes the revocation
        of the previous credential and the issuance of this one one unit of
        work — there is no instant at which both are live, and none at which
        neither is.

        Raises:
            HostNotFoundError: no such host.
            HostAlreadyEnrolledError: a live credential exists and
                ``allow_reenrollment`` is false.
        """
        moment = now or datetime.now(UTC)

        await self._lock_host(db, host_id)
        # Loaded for its existence check and its row lock; the row's own
        # fields are not needed here, so the result is deliberately discarded.
        await self._load_for_update(db, host_id)

        if not allow_reenrollment:
            live = (
                await db.execute(
                    select(AgentCredential.id).where(
                        AgentCredential.host_id == host_id,
                        AgentCredential.revoked_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if live is not None:
                await self._record_refusal(
                    db,
                    action=AuditAction.HOST_ENROLLMENT_ISSUED,
                    context=context,
                    host_id=host_id,
                    reason="already_enrolled",
                )
                raise HostAlreadyEnrolledError(host_id)

        superseded = await self._revoke_pending_enrollments(db, host_id=host_id, now=moment)

        credential = generate_token()
        expires_at = moment + self._enrollment_ttl
        enrollment = HostEnrollment(
            host_id=host_id,
            token_hash=hash_token(credential),
            expires_at=expires_at,
            created_by_user_id=context.actor_user_id,
        )
        db.add(enrollment)
        await db.flush()

        self._audit.record(
            db,
            action=AuditAction.HOST_ENROLLMENT_ISSUED,
            context=context,
            target_host_id=host_id,
            details={
                # That one was issued and when it dies. Never the credential,
                # and never its digest: the digest is the lookup key, so
                # recording it would let a reader of the trail confirm whether
                # a credential they captured is the live one.
                "enrollment_id": str(enrollment.id),
                "expires_at": expires_at.isoformat(),
                "superseded": superseded,
                "reenrollment": allow_reenrollment,
            },
        )
        logger.info(
            "host_enrollment_issued",
            host_id=str(host_id),
            enrollment_id=str(enrollment.id),
            expires_at=expires_at.isoformat(),
            superseded=superseded,
            actor_user_id=str(context.actor_user_id) if context.actor_user_id else None,
        )
        return IssuedEnrollment(
            enrollment=enrollment,
            credential=credential,
            expires_at=expires_at,
            superseded=superseded,
        )

    async def _revoke_pending_enrollments(
        self, db: AsyncSession, *, host_id: uuid.UUID, now: datetime
    ) -> int:
        """Revoke every unused, unexpired enrollment credential for a host.

        A single conditional ``UPDATE``, so the decision about which rows are
        still pending is made by the database at the moment of the write
        rather than from a list read earlier. ``consumed_at IS NULL`` is part
        of the predicate: a credential an agent already used must not be
        marked revoked, because "used" and "revoked before use" are different
        facts and the trail has to keep them apart.

        Returns how many rows it revoked.
        """
        result = await db.execute(
            sql_update(HostEnrollment)
            .where(
                HostEnrollment.host_id == host_id,
                HostEnrollment.consumed_at.is_(None),
                HostEnrollment.revoked_at.is_(None),
                HostEnrollment.expires_at > now,
            )
            .values(revoked_at=now)
        )
        # `rowcount` is a CursorResult attribute; AsyncSession.execute is
        # typed as returning the more general Result.
        return cast("CursorResult[Any]", result).rowcount or 0

    async def revoke_enrollments(
        self, db: AsyncSession, *, host_id: uuid.UUID, context: AuditContext
    ) -> int:
        """Revoke a host's pending enrollment credentials. Returns how many.

        The "I pasted that into the wrong window" action. Takes effect
        immediately: an agent presenting a revoked credential is refused, and
        the credential cannot be un-revoked.

        Idempotent, and it reports the count rather than asserting success, so
        "there was nothing pending" is distinguishable from "one was revoked".

        Raises:
            HostNotFoundError: no such host. Checked rather than assumed, so a
                wrong id reports 404 instead of succeeding against nothing — a
                revocation that reports success without having revoked
                anything is the specific lie this must not tell.
        """
        await self._lock_host(db, host_id)
        await self._load_for_update(db, host_id)

        count = await self._revoke_pending_enrollments(db, host_id=host_id, now=datetime.now(UTC))
        self._audit.record(
            db,
            action=AuditAction.HOST_ENROLLMENT_REVOKED,
            context=context,
            target_host_id=host_id,
            details={"revoked": count},
        )
        logger.info(
            "host_enrollments_revoked",
            host_id=str(host_id),
            count=count,
            actor_user_id=str(context.actor_user_id) if context.actor_user_id else None,
        )
        return count

    async def revoke_credential(
        self, db: AsyncSession, *, host_id: uuid.UUID, context: AuditContext
    ) -> Host:
        """Revoke a host's live agent credential.

        The agent is cut off at its next authentication attempt, which for a
        connected agent is immediately — the controller drops the connection
        when the credential it authenticated with stops being live
        (:mod:`hcc.services.enrollment`).

        The host moves to ``revoked``, which is deliberately not the same as
        ``registered``: the record has been used, and re-enrolling a host that
        once had an agent is a different operator decision from enrolling one
        for the first time.

        Raises:
            HostNotFoundError: no such host.
            NoLiveCredentialError: there is no live credential. Refused rather
                than reported as success, because an interface that says
                "revoked" when nothing was revoked tells an operator their
                host is secured when it is not.
        """
        await self._lock_host(db, host_id)
        host = await self._load_for_update(db, host_id)

        now = datetime.now(UTC)
        result = await db.execute(
            sql_update(AgentCredential)
            .where(
                AgentCredential.host_id == host_id,
                AgentCredential.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        revoked = cast("CursorResult[Any]", result).rowcount or 0
        if revoked == 0:
            await self._record_refusal(
                db,
                action=AuditAction.HOST_CREDENTIAL_REVOKED,
                context=context,
                host_id=host_id,
                reason="no_live_credential",
            )
            raise NoLiveCredentialError(host_id)

        host.lifecycle = HostLifecycle.REVOKED
        # Drop the recorded connection as well. Without this the host would
        # keep deriving as connected or stale while holding a credential
        # nothing can authenticate with — the interface would show an attached
        # agent that is in fact locked out.
        host.current_connection_id = None
        host.last_disconnected_at = now

        self._audit.record(
            db,
            action=AuditAction.HOST_CREDENTIAL_REVOKED,
            context=context,
            target_host_id=host_id,
            details={"credentials_revoked": revoked},
        )
        logger.info(
            "host_credential_revoked",
            host_id=str(host_id),
            count=revoked,
            actor_user_id=str(context.actor_user_id) if context.actor_user_id else None,
        )
        await db.flush()
        return host


# --- Helpers ----------------------------------------------------------------


def _fold_search(term: str) -> str:
    """Fold a search term the way canonical host names are folded.

    Not :func:`canonical_host_name`: a search term is not a host name and must
    not inherit its policy. Somebody typing ``%`` or a stray control character
    into a filter box is filtering badly, not submitting an invalid name, and
    raising at them would be absurd. So this folds and never raises, and the
    caller escapes the result before it reaches a ``LIKE``.
    """
    folded = unicodedata.normalize("NFKC", term).strip().casefold()
    return " ".join(folded.split())


def _clean_optional(value: str | None) -> str | None:
    """Normalise an optional free-text label.

    Whitespace-only becomes ``None``, so "cleared" has one representation
    rather than two that render identically and compare unequal.
    """
    if value is None:
        return None
    collapsed = " ".join(value.split())
    return collapsed or None
