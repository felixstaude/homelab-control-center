"""Host registry, enrollment tokens and agent credentials.

Phase 1, milestone 1.8. Three tables, one per concern, because they have
genuinely different lifetimes:

* :class:`Host` — the controller's durable record of a machine. Created by an
  administrator, never by an agent.
* :class:`HostEnrollment` — a short-lived, single-use token that lets exactly
  one agent claim exactly one host.
* :class:`AgentCredential` — the long-lived secret an enrolled agent uses to
  authenticate every connection.

## The host record is the identity, and an agent cannot create one

There is no self-registration. A monitoring system that trusts anything
claiming to be a host is one anybody on the network can poison with fabricated
metrics — and fabricated metrics fire or suppress notification rules
(``docs/AGENT_ARCHITECTURE.md`` §4). So the administrator creates the record,
and enrollment binds an agent to a record that already exists.

This is also what gives a reinstalled machine continuity: re-enrolling against
the same host record keeps its history, and that is the administrator's
explicit choice rather than an inference from a hostname or a MAC address,
both of which change for innocent reasons and are trivially forgeable.

## Administrative state and connectivity are separate axes

They are not one overloaded field, and conflating them is the mistake this
schema is shaped to avoid. A disabled host still has a real
``last_connected_at``; a host whose heartbeats stopped is not thereby
*revoked*; a host with no credential is not *offline* in the same sense as one
whose agent just died.

| Axis | Column(s) | Meaning |
| --- | --- | --- |
| Enrollment | ``lifecycle`` | Has an agent ever successfully claimed this record? |
| Administrative | ``is_enabled`` | Does the operator currently want this host connected? |
| Connectivity | ``current_connection_id`` + ``last_heartbeat_at`` | Attached right now? |

## Connectivity is derived, never stored as a boolean

There is deliberately no ``is_connected`` column. A boolean set on connect and
cleared on disconnect is wrong in the one case that matters: if the controller
process dies, nothing runs the clearing code, and every host stays "connected"
forever — the dashboard then reports healthy connectivity for a fleet that is
not connected to anything. That is the precise failure
``docs/PRODUCT_VISION.md`` principle 2 forbids.

So the state is computed from two facts that cannot go stale in a lie-shaped
direction: whether a connection is currently recorded, and how long ago the
last heartbeat arrived. A controller crash leaves
``current_connection_id`` set and ``last_heartbeat_at`` frozen, which derives
as **stale** — the honest answer, and visibly a problem. See ADR-043.

## Why there is no row per heartbeat

A heartbeat updates columns on the host row; it never inserts history. At the
default 30-second interval, 1,000 hosts would otherwise write 2.9 million rows
a day to hold information whose only consumer is "when did we last hear from
this host". Per-connection history is explicitly not retained in this
milestone, and the interface says so rather than showing an empty chart.
"""

from __future__ import annotations

import enum
import unicodedata
import uuid

# Imported at runtime: SQLAlchemy resolves `Mapped[...]` annotations at
# class-definition time. See hcc/models/user.py.
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    FetchedValue,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from hcc.db import Base
from hcc.security.tokens import TOKEN_HASH_LENGTH

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Host name bounds. The lower bound rejects a name too short to identify
#: anything; the upper keeps the column narrow enough to index.
HOST_NAME_MIN_LENGTH: Final = 1
HOST_NAME_MAX_LENGTH: Final = 64

#: Free-text description bound. Generous, but bounded: it is operator input
#: that ends up in a list cell.
HOST_DESCRIPTION_MAX_LENGTH: Final = 512

#: Grouping label — "production", "homelab", "rack-2". One short string rather
#: than a host-groups table, which ``AGENT_ARCHITECTURE.md`` §6.1 reserves as
#: a configuration layer and deliberately defers. This is metadata for the
#: operator's own eyes, not a layer anything resolves against.
HOST_ENVIRONMENT_MAX_LENGTH: Final = 32

#: Advisory strings an agent reports about itself. Attacker-controlled from a
#: compromised host, so bounded and never used for a decision.
AGENT_VERSION_MAX_LENGTH: Final = 64
PLATFORM_MAX_LENGTH: Final = 64


class HostLifecycle(enum.StrEnum):
    """How far through enrollment a host record has got.

    A small closed set stored as a constrained ``varchar`` rather than a native
    PostgreSQL enum. The reasoning is ADR-038's, applied here: this is not an
    authorization surface, so a future member should not require
    ``ALTER TYPE`` — but it *is* closed today, so a CHECK does the enforcing.
    """

    #: Created by an administrator. No agent has ever claimed it.
    REGISTERED = "registered"
    #: An agent has enrolled and holds a live credential.
    ENROLLED = "enrolled"
    #: Enrollment was completed and the credential has since been revoked.
    #: Distinct from ``registered``: the record has been used, which is worth
    #: knowing, and re-enrollment is a different operator decision from a
    #: first enrollment.
    REVOKED = "revoked"


#: Every lifecycle value, for the CHECK constraint and the migration.
HOST_LIFECYCLE_VALUES: Final = tuple(member.value for member in HostLifecycle)


#: The permitted lifecycle transitions, as a whitelist.
#:
#: Written as a table rather than scattered through the service functions that
#: perform the moves, so that "can this happen?" has one answer and an illegal
#: move is a caught error instead of a row in a state nothing expects.
#:
#: What is *absent* carries the meaning:
#:
#: * ``registered → revoked`` — there is no credential to revoke. Allowing it
#:   would let the interface show a host as revoked that was never enrolled,
#:   which reads as "we took the agent's access away" when nothing happened.
#: * ``enrolled → registered`` and ``revoked → registered`` — ``registered``
#:   means "no agent has ever claimed this". Going back would erase the fact
#:   that one did, and that fact is the reason ``revoked`` exists as a state
#:   distinct from ``registered``.
#:
#: ``enrolled → enrolled`` *is* permitted: re-enrolling an already-enrolled
#: host revokes the old credential and issues a new one in one transaction,
#: and the lifecycle legitimately does not move.
HOST_LIFECYCLE_TRANSITIONS: Final[Mapping[HostLifecycle, frozenset[HostLifecycle]]] = {
    HostLifecycle.REGISTERED: frozenset({HostLifecycle.ENROLLED}),
    HostLifecycle.ENROLLED: frozenset({HostLifecycle.ENROLLED, HostLifecycle.REVOKED}),
    HostLifecycle.REVOKED: frozenset({HostLifecycle.ENROLLED}),
}


class HostLifecycleError(ValueError):
    """An attempted lifecycle move that the state machine forbids.

    A ``ValueError`` subclass so a caller that does not care about the
    distinction still catches it, and a distinct type so the API layer can map
    it to a 409 rather than to a 500.
    """


def check_lifecycle_transition(current: str, target: str) -> HostLifecycle:
    """Validate a lifecycle move, returning the target as an enum member.

    Raises:
        ValueError: if either value is not a known lifecycle.
        HostLifecycleError: if both are known but the move is not permitted.
    """
    from_state = HostLifecycle(current)
    to_state = HostLifecycle(target)
    if to_state not in HOST_LIFECYCLE_TRANSITIONS[from_state]:
        permitted = ", ".join(sorted(HOST_LIFECYCLE_TRANSITIONS[from_state])) or "none"
        raise HostLifecycleError(
            f"a host cannot move from {from_state.value!r} to {to_state.value!r}; "
            f"permitted from {from_state.value!r}: {permitted}"
        )
    return to_state


class HostConnectivity(enum.StrEnum):
    """Whether an agent is attached *right now*.

    Three states, not two, and the third is the one that makes this honest.
    """

    #: No connection is recorded. Either no agent has ever connected, or the
    #: last one disconnected cleanly.
    OFFLINE = "offline"
    #: A connection is recorded and the last heartbeat is recent.
    CONNECTED = "connected"
    #: A connection is recorded but the heartbeats stopped. The socket may be
    #: half-open, the agent may be wedged, or the controller may have been
    #: restarted — the controller genuinely does not know which, and says so
    #: instead of guessing "connected" or "offline".
    STALE = "stale"


#: How often an agent heartbeats. Fixed by the protocol, not configurable off
#: (``AGENT_ARCHITECTURE.md`` §9): a host that can suppress its own liveness
#: signal can hide its own outage.
HEARTBEAT_INTERVAL_SECONDS: Final = 30

#: How long a recorded connection may go without a heartbeat before it is
#: reported as stale. Three intervals, so a single dropped heartbeat — a
#: GC pause, a brief network stall — does not flap the status, while a genuine
#: failure surfaces inside a minute and a half.
HEARTBEAT_GRACE_SECONDS: Final = HEARTBEAT_INTERVAL_SECONDS * 3


def derive_connectivity(
    *,
    current_connection_id: uuid.UUID | None,
    last_heartbeat_at: datetime | None,
    now: datetime,
    grace_seconds: int = HEARTBEAT_GRACE_SECONDS,
) -> HostConnectivity:
    """Compute connectivity from the two stored facts.

    A pure function of its arguments, deliberately: it is the one piece of
    logic that decides whether the dashboard claims a host is up, and it must
    be testable without a database, a socket or a clock.

    The asymmetry is intentional. *Offline* is asserted only when no connection
    is recorded at all — a fact the controller wrote itself on a clean
    disconnect. Everything else is at worst *stale*. A recorded connection with
    no heartbeat is never reported as offline, because "the agent went away"
    and "we stopped listening" are different incidents and only the host's
    operator can tell them apart.
    """
    if current_connection_id is None:
        return HostConnectivity.OFFLINE
    # Should not arise: accepting a connection stamps `last_heartbeat_at`,
    # because a completed authenticated handshake is itself evidence of
    # liveness — without that a freshly connected host would read as stale for
    # its first thirty seconds. Handled anyway, and resolved towards stale
    # rather than connected: "connected" is a claim that needs evidence.
    if last_heartbeat_at is None:
        return HostConnectivity.STALE
    if now - last_heartbeat_at > timedelta(seconds=grace_seconds):
        return HostConnectivity.STALE
    return HostConnectivity.CONNECTED


def normalize_host_name(raw: str) -> str:
    """Return the stored form of a host name.

    NFKC, whitespace collapse, and a rejection of the control characters that
    survive it — the folding ``normalize_username`` applies, without the
    casefold and with interior spaces permitted.

    Case is **preserved** but not significant for uniqueness: ``Homeserver``
    and ``homeserver`` are the same host, because an operator who registers
    both has made a mistake they will not notice in a list. The canonical form
    carries the constraint, exactly as it does for usernames (ADR-014).

    Interior whitespace is allowed — "backup nas" is a reasonable name — but
    runs are collapsed, so two names differing only in spacing cannot both
    exist and render identically.

    The collapse happens **first**, and that settles what counts as a control
    character here: every whitespace character, including a tab and a newline,
    is whitespace and becomes a single space. Only the non-whitespace control
    and format characters are refused. Rejecting a newline while silently
    collapsing a tab would be an arbitrary line to draw, and the danger a
    newline poses — breaking a log line, splitting one name into two — is
    removed by the collapse rather than needing a refusal. What remains
    genuinely cannot be made safe by folding: a NUL, a bidirectional override
    that renders a name backwards, a zero-width space that makes two distinct
    names look identical.

    Raises:
        ValueError: if the result is empty, too long, or contains a
            non-whitespace control character. Raised rather than returned so a
            caller cannot accidentally persist an invalid name.
    """
    if not isinstance(raw, str):  # pragma: no cover - defensive, typed as str
        raise TypeError(f"host name must be a string, got {type(raw).__name__}")

    folded = unicodedata.normalize("NFKC", raw).strip()
    # Collapse internal whitespace runs to a single space.
    collapsed = " ".join(folded.split())

    if not collapsed:
        raise ValueError("host name must not be empty or whitespace only")

    for character in collapsed:
        # Cc control, Cf format, Cs surrogate, Co private use, Cn unassigned.
        # Whitespace never reaches here: the collapse above consumed it, which
        # is why a tab and a newline are folded rather than refused.
        if unicodedata.category(character).startswith("C"):
            raise ValueError(
                f"host name must not contain control characters (found U+{ord(character):04X})"
            )
    if len(collapsed) > HOST_NAME_MAX_LENGTH:
        raise ValueError(
            f"host name must be at most {HOST_NAME_MAX_LENGTH} characters, got {len(collapsed)}"
        )
    return collapsed


def canonical_host_name(raw: str) -> str:
    """Return the uniqueness key for a host name.

    :func:`normalize_host_name` plus a casefold. Separate from the stored form
    so the operator's capitalisation survives while ``Homeserver`` and
    ``homeserver`` remain one host.
    """
    return normalize_host_name(raw).casefold()


class Host(Base):
    """A machine the controller knows about.

    Timestamps are timezone-aware and ``updated_at`` is maintained by the
    shared trigger (ADR-015), so it is correct even for a write that does not
    pass through the ORM.
    """

    __tablename__ = "hosts"

    __table_args__ = (
        # Case-insensitive uniqueness, backed by an index that also serves the
        # name lookup the create path needs.
        UniqueConstraint("name_canonical", name="uq_hosts_name_canonical"),
        # The list orders by canonical name, and filters by lifecycle and by
        # administrative state.
        Index("ix_hosts_lifecycle", "lifecycle"),
        # Connectivity derivation reads these two together on every list.
        Index("ix_hosts_last_heartbeat_at", "last_heartbeat_at"),
        CheckConstraint(
            f"char_length(name) BETWEEN {HOST_NAME_MIN_LENGTH} AND {HOST_NAME_MAX_LENGTH}",
            name="name_length",
        ),
        CheckConstraint(
            f"char_length(name_canonical) BETWEEN "
            f"{HOST_NAME_MIN_LENGTH} AND {HOST_NAME_MAX_LENGTH}",
            name="name_canonical_length",
        ),
        CheckConstraint(
            "lifecycle IN ('registered', 'enrolled', 'revoked')",
            name="lifecycle_is_known",
        ),
        # A connection epoch counts accepted connections and only ever
        # increases. Negative would mean an arithmetic bug.
        CheckConstraint("connection_epoch >= 0", name="connection_epoch_not_negative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    name: Mapped[str] = mapped_column(
        String(HOST_NAME_MAX_LENGTH),
        nullable=False,
        doc="Display form, as the administrator typed it.",
    )

    name_canonical: Mapped[str] = mapped_column(
        String(HOST_NAME_MAX_LENGTH),
        nullable=False,
        doc=(
            "Casefolded form carrying the unique constraint. Derived from "
            "`name`; never assign this directly."
        ),
    )

    description: Mapped[str | None] = mapped_column(
        String(HOST_DESCRIPTION_MAX_LENGTH),
        nullable=True,
        doc="Operator's note. Null means none was given, not 'unknown'.",
    )

    environment: Mapped[str | None] = mapped_column(
        String(HOST_ENVIRONMENT_MAX_LENGTH),
        nullable=True,
        doc=(
            "Free-text grouping label for the operator's own use. Nothing "
            "resolves configuration against it — host groups are a deferred "
            "configuration layer, not this."
        ),
    )

    # --- Enrollment and administrative state -----------------------------

    lifecycle: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=HostLifecycle.REGISTERED.value,
        server_default=text("'registered'"),
        doc="A `HostLifecycle` value. How far through enrollment this record is.",
    )

    is_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
        doc=(
            "Whether the operator currently wants this host connected. "
            "Orthogonal to `lifecycle` and to connectivity: a disabled host "
            "keeps its enrollment and its history, and its agent is refused "
            "at the door."
        ),
    )

    # --- Connectivity ----------------------------------------------------
    #
    # Transient. There is deliberately no `is_connected` boolean — see the
    # module docstring and ADR-043.

    current_connection_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        nullable=True,
        doc=(
            "The connection the controller believes is attached, or null when "
            "none is. Set on accept, cleared on a clean disconnect. A "
            "controller crash leaves it set with a frozen heartbeat, which "
            "derives as 'stale' rather than as 'connected'."
        ),
    )

    connection_epoch: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default=text("0"),
        doc=(
            "Monotonic count of accepted connections. A connection may only "
            "write state while it holds the current epoch, which is what "
            "stops a slow disconnect from a superseded socket clearing a "
            "newer connection's status."
        ),
    )

    last_connected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, doc="When a connection was last accepted."
    )

    last_disconnected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, doc="When a connection last ended."
    )

    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc=(
            "When the last heartbeat arrived. Null means no agent has ever "
            "heartbeated; it must never be rendered as a date."
        ),
    )

    # --- Advisory facts the agent reports --------------------------------
    #
    # Every field below is supplied by the agent and is therefore untrusted
    # input from a possibly-compromised host. None of it is used for an
    # authorization or identity decision; it exists so an operator can see
    # which version is deployed where.

    agent_version: Mapped[str | None] = mapped_column(
        String(AGENT_VERSION_MAX_LENGTH),
        nullable=True,
        doc="Reported by the agent. Advisory, never trusted for a decision.",
    )

    protocol_version: Mapped[int | None] = mapped_column(
        nullable=True,
        doc="The protocol major version negotiated on the last connection.",
    )

    platform: Mapped[str | None] = mapped_column(
        String(PLATFORM_MAX_LENGTH),
        nullable=True,
        doc="Reported os/arch, e.g. 'linux/amd64'. Advisory.",
    )

    agent_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc=(
            "When the connected agent process started, as it reports. Lets an "
            "operator tell a reconnect from a restart. Advisory."
        ),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        # The shared trigger does the work; this tells the ORM to refetch.
        server_onupdate=FetchedValue(),
    )

    @validates("name")
    def _normalize_and_sync_canonical(self, _key: str, value: str) -> str:
        """Keep ``name_canonical`` in step with ``name``.

        Runs on every assignment, including in the constructor, so the two
        columns cannot drift — the same invariant ``User.username`` maintains,
        and for the same reason.
        """
        stored = normalize_host_name(value)
        self.name_canonical = stored.casefold()
        return stored

    @validates("name_canonical")
    def _reject_direct_canonical_assignment(self, _key: str, value: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError("name_canonical is derived from name; set name instead")
        return value

    @validates("lifecycle")
    def _validate_lifecycle(self, _key: str, value: Any) -> str:
        """Coerce and validate at assignment rather than at flush.

        Without this an unknown value would fail as an opaque CHECK violation
        from the driver, pointing at the session instead of the caller.
        """
        if isinstance(value, HostLifecycle):
            return value.value
        try:
            return HostLifecycle(value).value
        except ValueError as exc:
            permitted = ", ".join(HOST_LIFECYCLE_VALUES)
            raise ValueError(
                f"invalid host lifecycle {value!r}; permitted values are: {permitted}"
            ) from exc

    def connectivity(
        self, now: datetime, *, grace_seconds: int = HEARTBEAT_GRACE_SECONDS
    ) -> HostConnectivity:
        """This host's connectivity at ``now``.

        ``now`` is a parameter rather than read from the clock inside, so that
        a list of hosts is all derived against one instant — otherwise two
        hosts in the same response could be judged against timestamps
        milliseconds apart, and a status could differ between two renderings of
        the same data.
        """
        return derive_connectivity(
            current_connection_id=self.current_connection_id,
            last_heartbeat_at=self.last_heartbeat_at,
            now=now,
            grace_seconds=grace_seconds,
        )

    def __repr__(self) -> str:
        """Debug representation. No secrets exist on this row to redact."""
        return (
            f"Host(id={self.id!r}, name={self.name!r}, "
            f"lifecycle={self.lifecycle!r}, is_enabled={self.is_enabled!r})"
        )


class HostEnrollment(Base):
    """A one-time token that lets one agent claim one host.

    ## Why this is a separate secret from the agent credential

    Deliberately, and the distinction is the point (``AGENT_ARCHITECTURE.md``
    §4.1). *This* is the secret that gets pasted into a terminal, lands in
    shell history, and is read over somebody's shoulder. Making it single-use
    and short-lived means that exposure is survivable. The long-lived
    credential never passes through a human's clipboard.

    ## Only the digest is stored

    An unsalted SHA-256 of a 256-bit CSPRNG token, never the token. See
    ADR-042 for why SHA-256 rather than Argon2id, which
    ``AGENT_ARCHITECTURE.md`` §4.2 originally specified: there is no candidate
    list to slow down, and a salted hash cannot be looked up by index, so
    verification would mean scanning every pending token.
    """

    __tablename__ = "host_enrollments"

    __table_args__ = (
        # The hot path is "resolve this presented token", so uniqueness on the
        # digest both serves that lookup and makes a collision or a
        # double-insert a database error rather than an ambiguous query.
        Index("uq_host_enrollments_token_hash", "token_hash", unique=True),
        # "Does this host have a usable token" — the list and detail views.
        Index("ix_host_enrollments_host_id", "host_id"),
        # The pruning job scans by expiry.
        Index("ix_host_enrollments_expires_at", "expires_at"),
        CheckConstraint(
            f"char_length(token_hash) = {TOKEN_HASH_LENGTH}",
            name="token_hash_is_sha256_hex",
        ),
        CheckConstraint("expires_at > created_at", name="expires_after_created"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    host_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        # Deleting a host removes its pending tokens: a token pointing at a
        # deleted host could otherwise enroll an agent against nothing.
        ForeignKey("hosts.id", ondelete="CASCADE"),
        nullable=False,
    )

    token_hash: Mapped[str] = mapped_column(
        String(TOKEN_HASH_LENGTH),
        nullable=False,
        doc="Hex SHA-256 of the token. The token itself is never stored.",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        doc="Absolute expiry. Computed at issue; never extended.",
    )

    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc=(
            "When enrollment consumed this token. Null means unused. The row "
            "is kept so that 'this token was used, at this time' stays "
            "answerable rather than the evidence being deleted."
        ),
    )

    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="When an administrator revoked it before use. Null means never.",
    )

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        nullable=True,
        doc=(
            "Who issued it. No foreign key, for the reason `audit_events` has "
            "none (ADR-038): deleting a user must not erase the record of "
            "what they did."
        ),
    )

    host: Mapped[Host] = relationship(lazy="joined")
    """The host this token enrolls.

    Eagerly joined: resolving a token always needs the host in order to check
    eligibility, so a lazy load would mean a second query — and under async
    SQLAlchemy a lazy load outside an await boundary raises.
    """

    def __repr__(self) -> str:
        """Debug representation with no token material.

        The digest is not directly replayable, but it is the lookup key:
        printing it would let anyone reading the log confirm whether a
        captured token is live.
        """
        return (
            f"HostEnrollment(id={self.id!r}, host_id={self.host_id!r}, "
            f"expires_at={self.expires_at!r}, consumed={self.consumed_at is not None}, "
            f"revoked={self.revoked_at is not None})"
        )


class AgentCredential(Base):
    """The long-lived secret proving "I am the agent for host X".

    Authorizes exactly one thing: reporting as that host and receiving that
    host's commands. It is not a user credential with a different shape — it
    cannot read another host's data, cannot enumerate the fleet, and cannot
    issue commands (``AGENT_ARCHITECTURE.md`` §3).

    That is what contains a compromised host: an attacker with root on one
    monitored machine gets that machine's credential, and that credential is
    worth one machine.

    Stored as an unsalted SHA-256 digest, for the reasons in ADR-042 — and
    here the indexed-lookup argument is decisive rather than merely
    convenient, because this digest is resolved on **every** agent
    reconnection.
    """

    __tablename__ = "agent_credentials"

    __table_args__ = (
        Index("uq_agent_credentials_token_hash", "token_hash", unique=True),
        Index("ix_agent_credentials_host_id", "host_id"),
        # At most one live credential per host, enforced by the database
        # rather than by application care. Without it, a re-enrollment race
        # could leave two valid credentials for one host and revoking "the"
        # credential would silently leave the other working.
        Index(
            "uq_agent_credentials_one_live_per_host",
            "host_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        CheckConstraint(
            f"char_length(token_hash) = {TOKEN_HASH_LENGTH}",
            name="token_hash_is_sha256_hex",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    host_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("hosts.id", ondelete="CASCADE"),
        nullable=False,
    )

    token_hash: Mapped[str] = mapped_column(
        String(TOKEN_HASH_LENGTH),
        nullable=False,
        doc="Hex SHA-256 of the credential. The credential itself is never stored.",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc=(
            "When it was revoked. Null means live. The row is kept, so that "
            "'this credential was revoked at this time' stays answerable and "
            "so the partial unique index above has something to exclude."
        ),
    )

    enrolled_from_enrollment_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        nullable=True,
        doc=(
            "Which one-time token produced this credential. No foreign key: "
            "the token row may be pruned long before the credential is, and a "
            "cascade would then take a live credential with it."
        ),
    )

    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc=(
            "When this credential last authenticated a connection. Written at "
            "most once per connection rather than per heartbeat, so a "
            "reconnect loop cannot turn this column into a write amplifier."
        ),
    )

    host: Mapped[Host] = relationship(lazy="joined")
    """The host this credential speaks for. Eagerly joined, as for enrollments."""

    def __repr__(self) -> str:
        """Debug representation with no credential material."""
        return (
            f"AgentCredential(id={self.id!r}, host_id={self.host_id!r}, "
            f"revoked={self.revoked_at is not None})"
        )
