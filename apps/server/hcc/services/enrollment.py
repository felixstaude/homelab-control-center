"""Agent-facing enrollment and connection authentication.

Phase 1, milestone 1.8. The half of host management reached by an **agent
presenting a secret**, not by an administrator holding a session. The
administrative half — registering hosts, issuing enrollment credentials — is
:mod:`hcc.services.hosts`.

Framework-free: raises typed exceptions and leaves status codes and WebSocket
close frames to :mod:`hcc.api.v1.agents`.

## Enrollment is a conditional UPDATE, not a read followed by a write

Single use is the property that matters most here, and the obvious
implementation does not have it:

```
SELECT ... WHERE token_hash = :h AND consumed_at IS NULL;  -- fine, unused
UPDATE  ... SET consumed_at = now() WHERE id = :id;        -- go ahead
```

Two agents presenting the same credential at the same moment both read it as
unused, both proceed, and the installation ends with two enrolled identities
for one host — the duplicate-identity failure ``docs/AGENT_ARCHITECTURE.md``
§4 forbids. Under READ COMMITTED neither transaction sees the other's
uncommitted write, so there is nothing for either to notice.

What is used instead is one statement:

```
UPDATE host_enrollments SET consumed_at = now()
 WHERE token_hash = :h AND consumed_at IS NULL AND revoked_at IS NULL
   AND expires_at > now()
RETURNING id, host_id;
```

The second transaction blocks on the row lock, re-evaluates the ``WHERE``
against committed state when the first commits, finds ``consumed_at`` set, and
updates nothing. Exactly one caller gets a row, decided by the database rather
than by application care. That is also why this method needs no advisory lock
to be single-use — the lock it does take is for the *credential swap* that
follows.

## Refusals are opaque to the agent and specific in the audit trail

Every enrollment refusal is the same message, whether the credential never
existed, has expired, was already used, or was revoked. The agent can do
nothing differently for any of them, and answering the difference would turn
this endpoint into an oracle that confirms which digests are real.

The audit trail records which it actually was, because an administrator *can*
act on that — "somebody tried an expired credential for homeserver" and
"somebody tried a credential that has never existed" are different incidents.

The split is also why a failure is recorded **only when it resolves to a known
host**. An unrecognised digest has no host to attribute it to, and writing a
row per unrecognised digest would make an endpoint reachable without a session
into a table-growth amplifier. Those are logged and not recorded, and the
distinction is in :mod:`hcc.models.audit`.

## Connection state is guarded by an epoch

Each accepted connection increments ``hosts.connection_epoch`` and is given a
fresh ``current_connection_id``. Every later write by that connection —
heartbeat, disconnect — carries both and is applied only if both still match:

```
UPDATE hosts SET last_heartbeat_at = now()
 WHERE id = :host AND current_connection_id = :conn AND connection_epoch = :epoch;
```

Without this, an agent that reconnects while its previous socket is still
draining produces a disconnect from the *old* connection that clears the
*new* one's state — so a host that is in fact connected reads as offline until
its next heartbeat, and a flapping link makes the dashboard oscillate. The
guard makes a superseded connection's writes no-ops, and ``rowcount == 0`` is
how the endpoint learns to close it.
"""

from __future__ import annotations

import uuid as uuid_module
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, cast

from sqlalchemy import CursorResult, func, select, text
from sqlalchemy import update as sql_update
from sqlalchemy.exc import IntegrityError

from hcc.agents.protocol import PROTOCOL_VERSION, CloseReason
from hcc.logging import get_logger
from hcc.models.audit import AuditAction, AuditEvent, AuditOutcome
from hcc.models.host import (
    AGENT_VERSION_MAX_LENGTH,
    PLATFORM_MAX_LENGTH,
    AgentCredential,
    Host,
    HostEnrollment,
    HostLifecycle,
    check_lifecycle_transition,
)
from hcc.security.audit import AuditContext
from hcc.security.tokens import generate_token, hash_token, looks_like_token
from hcc.services.hosts import HOST_LOCK_CLASS

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from hcc.security.audit import AuditService

logger = get_logger(__name__)

#: How long the controller waits before recording another identical
#: authentication failure for the same host.
#:
#: A revoked agent retries on its own backoff schedule, and without this every
#: retry would append a row — so one machine nobody has fixed yet slowly fills
#: the audit trail and buries the administrative events an operator is looking
#: for. One minute keeps "this host is being refused" visible while making the
#: cost per host constant rather than proportional to its retry rate.
AUTH_FAILURE_AUDIT_WINDOW_SECONDS: Final = 60

#: Message returned for every enrollment refusal. One string, deliberately:
#: see the module docstring.
ENROLLMENT_REFUSED_MESSAGE: Final = (
    "The enrollment credential is not valid. It may have expired, already "
    "been used, or been revoked. Issue a new one from the host's page."
)


@dataclass(frozen=True, slots=True, repr=False)
class EnrolledAgent:
    """The outcome of a successful enrollment.

    ``credential`` is the long-lived agent credential, and this is the only
    time it exists outside the agent's filesystem — only its digest is stored
    (ADR-042). The agent writes it to disk with restrictive permissions and
    presents it on every connection thereafter.
    """

    host_id: uuid_module.UUID
    host_name: str
    #: Plaintext. Never logged, never stored, never returned again.
    credential: str
    protocol_version: int
    #: Whether this enrollment displaced a credential the host already had.
    #: Reported so the agent's own log says it took over rather than joined.
    superseded_previous: bool

    def __repr__(self) -> str:
        """Representation with the credential redacted.

        ``repr=False`` on the decorator, without which the generated ``repr``
        would overwrite this and print the plaintext into the first log line
        or traceback that touched the object.
        """
        return (
            f"EnrolledAgent(host_id={self.host_id!r}, host_name={self.host_name!r}, "
            f"credential=<redacted>, protocol_version={self.protocol_version!r})"
        )

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class AuthenticatedAgent:
    """An accepted agent connection.

    Everything the endpoint needs in order to write this connection's state
    and to recognise when it has been superseded.

    Deliberately **not** the ORM row. The accept path writes the host with a
    bulk ``UPDATE`` that goes past the identity map, so an attached ``Host``
    object would carry a stale epoch for the lifetime of the connection —
    hours — and the first future reader to trust it would reintroduce the lost
    update this dataclass exists downstream of. Carrying the two fields
    anything actually needs makes that impossible rather than merely unlikely.
    """

    host_id: uuid_module.UUID
    #: Display name, for log lines an operator reads. Captured at accept and
    #: never re-read: a rename mid-connection changes a label, not an identity.
    host_name: str
    credential_id: uuid_module.UUID
    #: Identifies this connection specifically. Fresh per accept, so two
    #: connections from the same agent are distinguishable.
    connection_id: uuid_module.UUID
    #: Monotonic. A write is applied only while this is still the host's
    #: current epoch — see the module docstring.
    epoch: int


class EnrollmentRefusedError(Exception):
    """The presented enrollment credential is not usable.

    One exception for every reason, carrying one message. The specific reason
    goes to the audit trail and the log, not to the caller.
    """

    def __init__(self) -> None:
        super().__init__(ENROLLMENT_REFUSED_MESSAGE)


class AgentAuthenticationError(Exception):
    """A presented agent credential was refused.

    Carries a :class:`CloseReason` so the endpoint maps one exception type to
    the right close frame. :attr:`CloseReason.UNAUTHORIZED` is deliberately
    the same for an unknown and a revoked credential; see
    :class:`hcc.agents.protocol.CloseReason`.
    """

    def __init__(self, reason: CloseReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


class AgentEnrollmentService:
    """Consumes enrollment credentials and authenticates agent connections.

    Stateless apart from its collaborators, so one instance is shared across
    connections. It holds no socket and no connection table: the registry of
    live connections is :mod:`hcc.agents.registry`, kept separate because one
    is durable state in PostgreSQL and the other is per-process memory, and
    conflating them is how a controller comes to believe a connection another
    process holds is its own.
    """

    __slots__ = ("_audit",)

    def __init__(self, *, audit: AuditService) -> None:
        self._audit = audit

    # --- Enrollment -------------------------------------------------------

    async def enroll(
        self,
        db: AsyncSession,
        *,
        credential: str,
        agent_version: str | None = None,
        platform: str | None = None,
        request_id: str | None = None,
        now: datetime | None = None,
    ) -> EnrolledAgent:
        """Claim a host with a one-time enrollment credential.

        Returns the long-lived agent credential, which is the only copy.

        Does not commit. The caller does, which is what makes the whole
        exchange one unit of work: the enrollment credential is consumed, the
        previous agent credential revoked, the new one issued and the host
        moved to ``enrolled`` together. There is no instant at which the
        credential has been spent and no new one exists, and none at which two
        are live.

        Raises:
            EnrollmentRefusedError: for every reason, with one message.
        """
        moment = now or datetime.now(UTC)
        context = AuditContext(actor_user_id=None, request_id=request_id)

        # Shape check first, so obvious junk — an empty header, a truncated
        # paste, a megabyte of padding — never reaches the database. Not a
        # security check: a value passing this is still only authenticated by
        # a successful digest lookup.
        if not looks_like_token(credential):
            logger.warning("agent_enrollment_refused", reason="malformed")
            raise EnrollmentRefusedError

        digest = hash_token(credential)

        # One statement decides single use. See the module docstring for why a
        # SELECT followed by an UPDATE does not.
        claimed = (
            await db.execute(
                sql_update(HostEnrollment)
                .where(
                    HostEnrollment.token_hash == digest,
                    HostEnrollment.consumed_at.is_(None),
                    HostEnrollment.revoked_at.is_(None),
                    HostEnrollment.expires_at > moment,
                )
                .values(consumed_at=moment)
                .returning(HostEnrollment.id, HostEnrollment.host_id)
            )
        ).first()

        if claimed is None:
            await self._record_enrollment_refusal(db, digest=digest, context=context, now=moment)
            raise EnrollmentRefusedError

        enrollment_id, host_id = claimed

        # The credential swap, which does need serialising: two tokens for one
        # host, or a concurrent administrative revocation, would otherwise race
        # on the partial unique index and one of them would fail on a
        # constraint rather than losing deterministically.
        await self._lock_host(db, host_id)

        host = (
            await db.execute(
                select(Host)
                .where(Host.id == host_id)
                .with_for_update()
                # Load the locked row's current values over anything this
                # session already holds (ADR-040).
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if host is None:
            # The host was deleted between issuance and enrollment. The
            # cascade should have removed the token with it, so this is a
            # should-not-happen rather than a race — refused the same way, and
            # logged loudly because it would mean the cascade is gone.
            logger.error("agent_enrollment_host_missing", host_id=str(host_id))
            raise EnrollmentRefusedError

        superseded = await self._revoke_live_credentials(db, host_id=host_id, now=moment)

        agent_credential = generate_token()
        record = AgentCredential(
            host_id=host_id,
            token_hash=hash_token(agent_credential),
            enrolled_from_enrollment_id=enrollment_id,
        )
        db.add(record)

        # Validated rather than assigned, so an impossible move is a caught
        # error instead of a row in a state nothing expects. `registered →
        # enrolled` and `revoked → enrolled` are the two legitimate paths, and
        # `enrolled → enrolled` is the re-enrollment of a working host.
        host.lifecycle = check_lifecycle_transition(host.lifecycle, HostLifecycle.ENROLLED)
        # Advisory self-description, bounded on the way in because it comes
        # from a possibly-compromised host.
        host.agent_version = _bounded(agent_version, AGENT_VERSION_MAX_LENGTH)
        host.platform = _bounded(platform, PLATFORM_MAX_LENGTH)

        try:
            await db.flush()
        except IntegrityError:
            # The partial unique index refused a second live credential. Only
            # reachable if a writer that does not take the host lock inserted
            # one concurrently; refused rather than retried, because the agent
            # will retry on its own schedule and a retry loop inside a
            # transaction is how a deadlock becomes a hang.
            logger.exception("agent_enrollment_credential_conflict", host_id=str(host_id))
            raise EnrollmentRefusedError from None

        self._audit.record(
            db,
            action=AuditAction.HOST_ENROLLED,
            context=context,
            target_host_id=host_id,
            details={
                # No credential, and no digest. The digest is the lookup key,
                # so recording it would let a reader of the trail confirm
                # whether a captured credential is the live one.
                "enrollment_id": str(enrollment_id),
                "credential_id": str(record.id),
                "superseded_previous": superseded > 0,
                "agent_version": host.agent_version,
                "platform": host.platform,
            },
        )
        logger.info(
            "agent_enrolled",
            host_id=str(host_id),
            credential_id=str(record.id),
            superseded_previous=superseded > 0,
            agent_version=host.agent_version,
            platform=host.platform,
        )
        return EnrolledAgent(
            host_id=host_id,
            host_name=host.name,
            credential=agent_credential,
            protocol_version=PROTOCOL_VERSION,
            superseded_previous=superseded > 0,
        )

    async def _record_enrollment_refusal(
        self,
        db: AsyncSession,
        *,
        digest: str,
        context: AuditContext,
        now: datetime,
    ) -> None:
        """Record a refused enrollment, when there is a host to attribute it to.

        Looks the digest up without the eligibility predicate, purely to find
        out whether it names a real enrollment row. If it does, the refusal is
        attributed to that host with the real reason; if it does not, it is
        logged and nothing is written — see the module docstring on why an
        unrecognised digest must not produce a row.

        Never raises. The caller is about to refuse the enrollment and must do
        so regardless.
        """
        try:
            row = (
                await db.execute(
                    select(
                        HostEnrollment.host_id,
                        HostEnrollment.consumed_at,
                        HostEnrollment.revoked_at,
                        HostEnrollment.expires_at,
                    ).where(HostEnrollment.token_hash == digest)
                )
            ).first()

            if row is None:
                # Nothing to attribute it to, and nothing worth a row.
                logger.warning("agent_enrollment_refused", reason="unknown_credential")
                return

            host_id, consumed_at, revoked_at, expires_at = row
            if consumed_at is not None:
                reason = "already_used"
            elif revoked_at is not None:
                reason = "revoked"
            elif expires_at <= now:
                reason = "expired"
            else:  # pragma: no cover - the conditional UPDATE already matched
                reason = "conflict"

            logger.warning("agent_enrollment_refused", reason=reason, host_id=str(host_id))

            # A refusal has nothing to commit with, so this is its own
            # transaction — the enrollment attempt itself is about to be
            # abandoned.
            await db.rollback()
            self._audit.record(
                db,
                action=AuditAction.HOST_AGENT_AUTH_FAILED,
                context=context,
                outcome=AuditOutcome.FAILURE,
                target_host_id=host_id,
                details={"stage": "enrollment", "reason": reason},
            )
            await db.commit()
        except Exception:
            # Deliberately broad and deliberately swallowed here only. An
            # audit write that failed must not turn a correct refusal into a
            # 500, which some clients treat as retryable.
            logger.exception("agent_enrollment_refusal_record_failed")

    # --- Connection authentication ----------------------------------------

    async def authenticate(
        self,
        db: AsyncSession,
        *,
        credential: str,
        agent_version: str | None = None,
        platform: str | None = None,
        protocol_version: int = PROTOCOL_VERSION,
        agent_started_at: datetime | None = None,
        request_id: str | None = None,
        now: datetime | None = None,
    ) -> AuthenticatedAgent:
        """Authenticate an agent connection and record it as accepted.

        The host's identity comes from the credential and from nothing the
        agent says. There is no ``host_id`` parameter, and the protocol has no
        field for one, so an agent cannot claim to be a host it does not hold
        the credential for.

        Increments the host's connection epoch and stamps a fresh connection
        id, both of which the caller must carry on every later write for this
        connection.

        ``last_heartbeat_at`` is stamped **here**, at accept, because a
        completed authenticated handshake is itself evidence of liveness.
        Without it a freshly connected host would derive as stale for its
        first thirty seconds, and the interface would report a problem that
        does not exist.

        Does not commit. The caller does.

        Raises:
            AgentAuthenticationError: carrying the close reason to send.
        """
        moment = now or datetime.now(UTC)
        context = AuditContext(actor_user_id=None, request_id=request_id)

        if not looks_like_token(credential):
            logger.warning("agent_authentication_refused", reason="malformed")
            raise AgentAuthenticationError(CloseReason.UNAUTHORIZED)

        digest = hash_token(credential)

        found = (
            await db.execute(select(AgentCredential).where(AgentCredential.token_hash == digest))
        ).scalar_one_or_none()

        if found is None:
            # No host to attribute it to. Logged, never recorded.
            logger.warning("agent_authentication_refused", reason="unknown_credential")
            raise AgentAuthenticationError(CloseReason.UNAUTHORIZED)

        host = found.host

        if found.revoked_at is not None:
            await self._record_auth_failure(
                db, host_id=host.id, reason="credential_revoked", context=context, now=moment
            )
            # The same answer the unknown case gets. An administrator sees the
            # difference in the trail; the holder of the credential does not.
            raise AgentAuthenticationError(CloseReason.UNAUTHORIZED)

        if not host.is_enabled:
            await self._record_auth_failure(
                db, host_id=host.id, reason="host_disabled", context=context, now=moment
            )
            # Told apart, because it is a legitimate operational state with an
            # obvious remedy and the agent has already proved it holds this
            # host's credential.
            raise AgentAuthenticationError(CloseReason.HOST_DISABLED)

        connection_id = uuid_module.uuid4()

        # The epoch is incremented **by the database**, in one statement, and
        # the new value is read back from ``RETURNING``.
        #
        # This was a read-modify-write — `epoch = host.connection_epoch + 1`
        # followed by an assignment — and that is a lost update. Four agents
        # authenticating at once all read 0 and all wrote 1, so the column
        # documented as a monotonic count of accepted connections was not
        # monotonic and not a count. Caught by
        # `test_concurrent_authentication_does_not_lose_the_connection_epoch`,
        # which is the only kind of test that could catch it: on one
        # connection the old code is correct.
        #
        # One statement rather than a lock, because this is the connection
        # accept path — the thing a fleet does all at once after a controller
        # restart — and `connection_epoch = connection_epoch + 1` is evaluated
        # against the committed row under PostgreSQL's row lock, so the
        # increments cannot be lost without the lock's cost.
        epoch = (
            await db.execute(
                sql_update(Host)
                .where(Host.id == host.id)
                .values(
                    connection_epoch=Host.connection_epoch + 1,
                    current_connection_id=connection_id,
                    last_connected_at=moment,
                    last_heartbeat_at=moment,
                    protocol_version=protocol_version,
                    agent_version=_bounded(agent_version, AGENT_VERSION_MAX_LENGTH),
                    platform=_bounded(platform, PLATFORM_MAX_LENGTH),
                    agent_started_at=agent_started_at,
                )
                .returning(Host.connection_epoch)
            )
        ).scalar_one()

        # Written once per connection rather than per heartbeat, so a
        # reconnect loop cannot turn this column into a write amplifier.
        found.last_used_at = moment

        await db.flush()

        logger.info(
            "agent_connection_accepted",
            host_id=str(host.id),
            connection_id=str(connection_id),
            epoch=epoch,
            protocol_version=protocol_version,
        )
        return AuthenticatedAgent(
            host_id=host.id,
            host_name=host.name,
            credential_id=found.id,
            connection_id=connection_id,
            epoch=epoch,
        )

    async def _record_auth_failure(
        self,
        db: AsyncSession,
        *,
        host_id: uuid_module.UUID,
        reason: str,
        context: AuditContext,
        now: datetime,
    ) -> None:
        """Record a refused connection, at most once per host per window.

        The suppression is why this is not a two-line call to the audit
        service. A revoked agent retries on its own backoff schedule forever,
        and a row per retry means one machine nobody has got round to fixing
        slowly buries every administrative event in the trail. One row a
        minute keeps the signal and bounds the cost.

        The window is checked with an indexed query against
        ``ix_audit_events_target_host_id``, so the suppression costs one cheap
        lookup per refusal rather than a scan.

        Never raises; the caller is about to refuse the connection regardless.
        """
        try:
            await db.rollback()
            recent = (
                await db.execute(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(
                        AuditEvent.target_host_id == host_id,
                        AuditEvent.action == AuditAction.HOST_AGENT_AUTH_FAILED.value,
                        AuditEvent.occurred_at
                        > now - timedelta(seconds=AUTH_FAILURE_AUDIT_WINDOW_SECONDS),
                    )
                )
            ).scalar_one()

            logger.warning(
                "agent_authentication_refused",
                reason=reason,
                host_id=str(host_id),
                recorded=recent == 0,
            )
            if recent:
                return

            self._audit.record(
                db,
                action=AuditAction.HOST_AGENT_AUTH_FAILED,
                context=context,
                outcome=AuditOutcome.FAILURE,
                target_host_id=host_id,
                details={"stage": "connection", "reason": reason},
            )
            await db.commit()
        except Exception:
            logger.exception("agent_auth_failure_record_failed", host_id=str(host_id))

    # --- Connection state -------------------------------------------------

    async def record_heartbeat(
        self,
        db: AsyncSession,
        *,
        agent: AuthenticatedAgent,
        agent_version: str | None = None,
        platform: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Stamp a heartbeat, if this connection is still the current one.

        Returns ``False`` when the connection has been superseded, which the
        caller must treat as "close this socket" rather than as an error. The
        guard is the whole point: an agent that reconnected while this socket
        was draining has a newer connection recorded, and letting this one
        write would make the dashboard oscillate.

        A bulk ``UPDATE`` rather than an ORM flush, deliberately. This is the
        most frequent write in the system — a thousand hosts at thirty seconds
        is 2,880,000 a day — so it touches one row with no identity-map
        bookkeeping, and ``updated_at`` is maintained by the trigger rather
        than by the ORM (ADR-015), which is exactly why that is a trigger.

        Does not commit. The caller does.
        """
        moment = now or datetime.now(UTC)
        values: dict[str, Any] = {"last_heartbeat_at": moment}
        if agent_version is not None:
            values["agent_version"] = _bounded(agent_version, AGENT_VERSION_MAX_LENGTH)
        if platform is not None:
            values["platform"] = _bounded(platform, PLATFORM_MAX_LENGTH)

        result = await db.execute(
            sql_update(Host)
            .where(
                Host.id == agent.host_id,
                Host.current_connection_id == agent.connection_id,
                Host.connection_epoch == agent.epoch,
            )
            .values(**values)
        )
        applied = cast("CursorResult[Any]", result).rowcount > 0
        if not applied:
            logger.info(
                "agent_heartbeat_superseded",
                host_id=str(agent.host_id),
                connection_id=str(agent.connection_id),
                epoch=agent.epoch,
            )
        return applied

    async def record_disconnect(
        self,
        db: AsyncSession,
        *,
        agent: AuthenticatedAgent,
        now: datetime | None = None,
    ) -> bool:
        """Clear this connection, if it is still the current one.

        The epoch guard is load-bearing here rather than merely tidy. Without
        it, a slow close from a superseded socket would clear the connection a
        newer accept had just recorded — so a host that *is* connected would
        read as offline, and a flapping link would make it flicker.

        ``last_heartbeat_at`` is deliberately **not** cleared. It is the record
        of when this host was last heard from, and a disconnect does not make
        that untrue; clearing it would discard the one fact an operator needs
        in order to ask how long a host has been gone.

        Returns whether it applied. Does not commit.
        """
        moment = now or datetime.now(UTC)
        result = await db.execute(
            sql_update(Host)
            .where(
                Host.id == agent.host_id,
                Host.current_connection_id == agent.connection_id,
                Host.connection_epoch == agent.epoch,
            )
            .values(current_connection_id=None, last_disconnected_at=moment)
        )
        applied = cast("CursorResult[Any]", result).rowcount > 0
        logger.info(
            "agent_connection_closed",
            host_id=str(agent.host_id),
            connection_id=str(agent.connection_id),
            epoch=agent.epoch,
            applied=applied,
        )
        return applied

    async def is_still_authorized(
        self, db: AsyncSession, *, agent: AuthenticatedAgent
    ) -> CloseReason | None:
        """Re-check an open connection's authorization, or ``None`` if fine.

        Called periodically by the endpoint rather than per frame. An
        administrator who revokes a credential or disables a host expects the
        agent to be cut off *now*, not at its next reconnection — and the
        controller cannot be notified by the request that did it, because that
        request may have been served by a different process.

        Polling is the honest mechanism at this scale: one indexed read per
        connection per interval. A push would need a channel between
        processes, which is the same single-process limitation stated in
        :mod:`hcc.agents.registry` and deferred for the same reason.
        """
        row = (
            await db.execute(
                select(Host.is_enabled, AgentCredential.revoked_at)
                .join(AgentCredential, AgentCredential.host_id == Host.id)
                .where(Host.id == agent.host_id, AgentCredential.id == agent.credential_id)
            )
        ).first()

        if row is None:
            # The credential or the host is gone. Close it; there is nothing
            # left that authorizes this connection.
            return CloseReason.UNAUTHORIZED
        is_enabled, revoked_at = row
        if revoked_at is not None:
            return CloseReason.UNAUTHORIZED
        if not is_enabled:
            return CloseReason.HOST_DISABLED
        return None

    # --- Helpers ----------------------------------------------------------

    async def _lock_host(self, db: AsyncSession, host_id: uuid_module.UUID) -> None:
        """Serialise against other writes to this host.

        The same key and the same reasoning as
        :meth:`hcc.services.hosts.HostService._lock_host` — shared through
        :data:`hcc.services.hosts.HOST_LOCK_CLASS` rather than redefined,
        because a lock key derived differently by two callers is a lock that
        does nothing.
        """
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:class, hashtext(:host_id))").bindparams(
                **{"class": HOST_LOCK_CLASS, "host_id": str(host_id)}
            )
        )

    async def _revoke_live_credentials(
        self, db: AsyncSession, *, host_id: uuid_module.UUID, now: datetime
    ) -> int:
        """Revoke the host's live agent credential. Returns how many.

        A conditional ``UPDATE``, so which row is live is decided by the
        database at the moment of the write. The partial unique index means
        there is at most one, but the statement does not depend on that — it
        would revoke two just as correctly, which is the right shape for a
        statement whose job is "leave none live".
        """
        result = await db.execute(
            sql_update(AgentCredential)
            .where(
                AgentCredential.host_id == host_id,
                AgentCredential.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        return cast("CursorResult[Any]", result).rowcount or 0


def _bounded(value: str | None, limit: int) -> str | None:
    """Truncate and clean an advisory string an agent reported about itself.

    Truncated rather than refused. These fields are cosmetic — a version
    string in a list cell — and refusing a connection because a host reports
    an over-long platform would turn a label into an availability problem. The
    bound exists so the value cannot be unbounded, not so it can be trusted.

    Whitespace-only becomes ``None``, so "the agent reported nothing" has one
    representation.
    """
    if value is None:
        return None
    collapsed = " ".join(value.split())[:limit]
    return collapsed or None
