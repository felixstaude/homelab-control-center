"""Recording privileged operations.

Phase 1, milestone 1.7. Framework-free like the rest of :mod:`hcc.security` —
it uses SQLAlchemy but imports no FastAPI, so the same call works from a
request handler, a service method, a CLI command or a test.

The schema, the append-only property and the reason the action vocabulary is
not a PostgreSQL enum all live in :mod:`hcc.models.audit`. This module is
about *when* a row is written, and in whose transaction.

## A success event belongs to the mutation's transaction

:meth:`AuditService.record` adds a row and **does not commit**. The caller
commits, which is what makes the event and the change it describes one unit of
work: either the role changed and the event exists, or neither happened. There
is no window in which the database says a role was changed and no record says
who did it, and no window in which the trail claims a change that was rolled
back.

This is the same discipline the rest of the codebase applies to writes
(``docs/ARCHITECTURE.md`` §6.1: commits are the handler's responsibility), and
here it is load-bearing rather than stylistic.

## A denial cannot be, and the alternative is worse than it looks

An authorization failure raises out of a dependency before the handler runs,
and :meth:`hcc.db.Database.session` rolls the request's transaction back on
the way out. A denial event added to that transaction would therefore be
discarded — the one event class that records an attempt would be the one class
that never persisted.

So :meth:`AuditService.record_denial` opens its **own** short transaction and
commits it. Two consequences, both deliberate:

* **It cannot weaken authorization.** It only ever inserts a row and returns;
  the caller raises regardless of what happens here. If the insert fails, the
  failure is logged and swallowed *at this call site only*, because the
  alternative — letting an audit write turn a clean 403 into a 500 — would
  mean a database hiccup converting a correct refusal into a response some
  clients treat as retryable. Refusing the request is the security property;
  recording it is the evidence. Losing the evidence is bad, and it is strictly
  better than losing the refusal.
* **It is bounded by authentication, not by anything cheaper.** Authorization
  runs only after a session has resolved (``hcc/api/deps.py``), so generating
  denial rows requires a valid account. An authenticated low-privilege user
  can still grow the table at their request rate, which is the same exposure
  the structured log already has; what it is not is an unauthenticated write
  amplifier. Growth and pruning are covered in ``docs/SECURITY.md`` alongside
  the session table's.

## Details are built, never passed through

Every recording method takes named arguments and assembles ``details``
itself. There is deliberately no ``details: dict`` parameter a caller could
fill with a request body, a form payload or an exception string — the way
credential material reaches an audit log in practice is that somebody passed
the whole request. Adding a field here is a visible, reviewable change to this
module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hcc.logging import get_logger
from hcc.models.audit import AuditAction, AuditEvent, AuditOutcome

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from hcc.models.user import UserRole

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AuditContext:
    """Who is acting, and which request this is.

    Assembled by the API layer, which is the only layer that knows about
    requests, and passed down into the services. A frozen dataclass rather
    than two loose parameters threaded through every signature, so adding a
    future dimension — an actor's session id, say — does not change every
    service method.
    """

    actor_user_id: uuid.UUID | None
    #: The correlation id from :mod:`hcc.middleware`, so an event and the log
    #: lines for the same request can be lined up. ``None`` outside HTTP.
    request_id: str | None = None


class AuditService:
    """Writes audit events.

    Fully stateless, which is a deliberate consequence of how
    :meth:`record_denial` gets its database handle. The obvious design holds a
    session factory on the instance, and that would have forced this service
    to be constructed *after* the database — while every other security
    service is built at application-construction time, before the lifespan
    opens a connection, precisely so that tests which skip the lifespan still
    have them. Taking the factory as a parameter keeps that ordering intact
    and removes a field that could be ``None`` at exactly the wrong moment.
    """

    __slots__ = ()

    # --- Successes and deliberate refusals --------------------------------

    def record(
        self,
        db: AsyncSession,
        *,
        action: AuditAction,
        context: AuditContext,
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
        target_user_id: uuid.UUID | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """Stage an event in the caller's transaction.

        **Does not commit.** The caller does, together with the change this
        describes — see the module docstring.

        ``details`` is accepted here because the callers are the service
        methods below and in :mod:`hcc.services`, which build it from named
        fields. It is not reachable from a request body: no route hands user
        input to this parameter, and the typed request models forbid extra
        fields, so there is nothing to forward even by accident.
        """
        event = AuditEvent(
            action=action.value,
            outcome=outcome.value,
            actor_user_id=context.actor_user_id,
            target_user_id=target_user_id,
            request_id=context.request_id,
            details=details or None,
        )
        db.add(event)
        logger.info(
            "audit_event_recorded",
            action=action.value,
            outcome=outcome.value,
            actor_user_id=str(context.actor_user_id) if context.actor_user_id else None,
            target_user_id=str(target_user_id) if target_user_id else None,
        )
        return event

    # --- Denials ----------------------------------------------------------

    async def record_denial(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        context: AuditContext,
        path: str,
        method: str,
        required_role: UserRole,
        actual_role: UserRole,
    ) -> None:
        """Record an authorization refusal, in its own committed transaction.

        Never raises. The caller has already decided to refuse the request and
        must do so whatever happens here; see the module docstring for why
        that trade is the right way round.

        The recorded ``details`` are server-side facts — the route and method
        that were refused, and the two roles involved. None of it is echoed to
        the client, which is told only that it is not permitted
        (``hcc/errors.py``): the point of the record is that an operator can
        later see a Viewer probing administrative endpoints.
        """
        try:
            async with session_factory() as db:
                self.record(
                    db,
                    action=AuditAction.ADMIN_ACCESS_DENIED,
                    context=context,
                    outcome=AuditOutcome.DENIED,
                    details={
                        "path": path,
                        "method": method,
                        "required_role": required_role.value,
                        "actual_role": actual_role.value,
                    },
                )
                await db.commit()
        except Exception:
            # Deliberately broad, and deliberately swallowed *here only*. An
            # audit write that failed must not turn a correct 403 into a 500,
            # which some clients treat as retryable. Logged with the traceback
            # so the lost record is visible rather than silent.
            logger.exception(
                "audit_denial_record_failed",
                path=path,
                method=method,
            )
