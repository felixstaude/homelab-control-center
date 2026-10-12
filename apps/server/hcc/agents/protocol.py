"""The agent↔controller wire format.

Phase 1, milestone 1.8. One module, imported by both the WebSocket endpoint and
the protocol tests, so there is exactly one definition of what may cross that
boundary.

## Versioned from the first message, not from the first breaking change

:data:`PROTOCOL_VERSION` is negotiated in the handshake and every frame carries
``v``. That is worth doing now rather than when it is needed, because the thing
being versioned is a binary installed on machines the controller cannot reach.
An operator who upgrades the controller has not upgraded the fleet, and "the
agent and the controller must be the same version" is a rule that cannot be
enforced across the internet with no inbound ports.

So the controller states which versions it accepts, refuses a handshake outside
that set with a reason the agent can log, and the agent refuses to guess. A
mismatch is then a clear message in two logs instead of a frame parsed under the
wrong assumptions.

## Every bound here is a denial-of-service bound

An agent is an authenticated peer, but it runs on a machine whose security the
controller does not own — that is the entire point of monitoring it. A
compromised host presents exactly this interface, so each limit below answers
"what can one bad agent cost the controller":

* :data:`MAX_MESSAGE_BYTES` — a frame larger than this is refused before it is
  parsed. Without it, one agent streaming a 2 GiB JSON frame is a memory
  exhaustion attack with no exploit required.
* :data:`MAX_FRAMES_PER_SECOND` and :data:`FRAME_BURST` — a token bucket, so a
  flooding agent is disconnected rather than served. A heartbeat every thirty
  seconds needs none of this budget.
* :data:`HEARTBEAT_TIMEOUT_SECONDS` — an idle connection is closed. A
  half-open socket consumes a connection slot indefinitely otherwise, and
  "connected but silent" must not look like healthy.
* Field lengths on every string. The strings are advisory facts the agent
  reports about itself, which means they are attacker-controlled on a
  compromised host.

## Configuration travels here, not on a second HTTP channel

Phase 2 adds configuration distribution to this socket rather than to the
``GET /api/v1/agents/config`` plus ``POST .../config/ack`` pair
``docs/AGENT_ARCHITECTURE.md`` §6.3 originally described. The socket is already
authenticated and identity-bound, so there is no second credential path to
defend; a push removes the polling latency; and it is one fewer endpoint. See
ADR-049.

The **pull is still the guarantee**. Every heartbeat carries the agent's
running configuration version and checksum, and every acknowledgement carries
the version the controller believes is current — so an agent that missed a
push corrects itself on its next heartbeat. The push is a latency
optimisation on top of a self-correcting loop, which is the same
event-stream-plus-reconciliation shape ``docs/SCHEDULING.md`` §9.2 requires of
every event-driven module. A design where the push *was* the mechanism would
leave a host silently running month-old configuration after one dropped
frame.

## There is no command execution in this protocol

Deliberately, and it is the most important absence here. No message carries a
shell command, a script, a path to execute, or a module name to import. That
remains true with configuration on the wire, and it is worth being precise
about why, because a configuration document is the closest thing to an
instruction that crosses this boundary: the document names **modules from a
closed registry compiled into the agent**, never a path, a binary or a
collector to load. An agent receiving a module identifier it does not know
reports it unsupported; it cannot be told to go and find one.

A monitoring agent that will run what the controller tells it has turned a
control-plane compromise into root on every monitored machine. When remote
execution arrives it will be a separate, explicitly-enumerated capability with
its own authorization axis (``docs/AGENT_ARCHITECTURE.md`` §3), not a string
field that happens to reach a subprocess.
"""

from __future__ import annotations

import enum
import json
from typing import Any, Final, Literal

from pydantic import BaseModel, Field, field_validator

from hcc.models.monitoring import MAX_PROBLEM_LENGTH, MAX_PROBLEMS
from hcc.monitoring.canonical import CHECKSUM_LENGTH

#: The protocol version this controller speaks. Bumped for a breaking change
#: to the frame format or to the meaning of a field.
PROTOCOL_VERSION: Final = 1

#: Every version this controller will accept a handshake for. A tuple rather
#: than a comparison, so support for an old version is dropped by deleting an
#: entry — a visible, reviewable change — instead of by moving a floor.
SUPPORTED_PROTOCOL_VERSIONS: Final = (1,)

#: Largest frame the controller will read. 64 KiB is two orders of magnitude
#: more than any message defined here needs, and small enough that a thousand
#: agents each mid-frame is 64 MiB rather than unbounded.
#:
#: Unchanged by configuration arriving on this socket. A five-module document
#: is about 1 KiB and the ceiling applies to the **inbound** direction, where
#: the largest frame an agent can send is a rejection carrying
#: ``MAX_PROBLEMS`` problems of ``MAX_PROBLEM_LENGTH`` each — 8 KiB of text
#: plus JSON overhead, comfortably inside the bound. The outbound snapshot is
#: not governed by this at all; it is the controller's own document, and its
#: size is bounded by the registry being closed.
MAX_MESSAGE_BYTES: Final = 64 * 1024

#: Sustained frame rate one agent may use, and the burst it may spend at once.
#: A heartbeat every 30 seconds is 0.033/s, so a well-behaved agent never
#: approaches this; the budget exists to bound a misbehaving one.
MAX_FRAMES_PER_SECOND: Final = 5
FRAME_BURST: Final = 20

#: How often an agent must heartbeat, and how long the controller waits before
#: closing a silent connection. The timeout is deliberately longer than the
#: staleness grace in :mod:`hcc.models.host`: the controller reports a host as
#: stale well before it gives up on the socket, so a brief stall shows in the
#: interface without destroying a connection that is about to recover.
HEARTBEAT_INTERVAL_SECONDS: Final = 30
HEARTBEAT_TIMEOUT_SECONDS: Final = 120

#: Bounds on the self-reported strings. Attacker-controlled on a compromised
#: host, so they are bounded on the way in rather than trusted by every later
#: reader of the column.
MAX_AGENT_VERSION_LENGTH: Final = 64
MAX_PLATFORM_LENGTH: Final = 64
MAX_CLOSE_REASON_LENGTH: Final = 123

#: Largest configuration version an agent may claim to be running.
#:
#: Versions are assigned by the controller and are per host, so a real one is
#: small. The bound exists because the value arrives from a possibly
#: compromised host and lands in an integer column: without it, an agent
#: claiming version 2**63 would be a failed insert on a path that should not
#: be able to fail.
MAX_CONFIG_VERSION: Final = 2_000_000_000


class MessageType(enum.StrEnum):
    """Every frame type that exists.

    A closed set, and the closure is the security property: an unrecognised
    ``type`` is refused rather than ignored, so a future frame cannot be
    smuggled past a controller that does not understand it.
    """

    #: Agent → controller. Liveness, plus the agent's view of itself and of
    #: the configuration it is running.
    HEARTBEAT = "heartbeat"
    #: Controller → agent. Acknowledges a heartbeat, restates the interval so
    #: an agent that has drifted converges without a redeploy, and states the
    #: configuration version the controller believes is current — which is
    #: what makes the synchronisation loop self-correcting.
    HEARTBEAT_ACK = "heartbeat_ack"
    #: Controller → agent. The connection is ending, and why.
    CLOSE = "close"

    # --- Configuration synchronisation, phase 2 -----------------------------

    #: Controller → agent. A newer configuration exists. Carries the version
    #: and nothing else: it is a *notification*, so a dropped one costs at
    #: most one heartbeat interval of latency rather than a document.
    CONFIG_AVAILABLE = "config_available"
    #: Agent → controller. "Send me the current configuration." Sent on the
    #: agent's own initiative — after a notification, after noticing a version
    #: mismatch in an acknowledgement, or after a restart with no local copy.
    CONFIG_REQUEST = "config_request"
    #: Controller → agent. The configuration document, its version and the
    #: checksum over its exact bytes.
    CONFIG_SNAPSHOT = "config_snapshot"
    #: Agent → controller. "I verified and applied this version."
    CONFIG_ACK = "config_ack"
    #: Agent → controller. "I refused this version, and here is every reason."
    #: The agent keeps running its last known good configuration.
    CONFIG_NACK = "config_nack"


class CloseReason(enum.StrEnum):
    """Why the controller is ending a connection.

    A closed vocabulary so the agent can decide whether to retry, and so its
    log says something an operator can act on. The split between the first two
    is the one that matters:

    * :attr:`UNAUTHORIZED` is "this credential is not usable", and it is
      deliberately the same answer for a credential that never existed and one
      that was revoked. The agent cannot do anything differently for either,
      and distinguishing them over the wire would confirm to a holder of a
      stolen credential that it was once real. The *audit trail* records which
      it was, because an administrator can act on that.
    * :attr:`HOST_DISABLED` is "this credential is fine and the operator has
      switched this host off". Told apart because it is a legitimate
      operational state with an obvious remedy, and the agent has already
      proved it holds that host's credential — so there is nothing left to
      disclose.
    """

    UNAUTHORIZED = "unauthorized"
    HOST_DISABLED = "host_disabled"
    #: The handshake asked for a protocol version this controller does not
    #: speak. Carries the supported set, so the agent's log names the fix.
    UNSUPPORTED_PROTOCOL = "unsupported_protocol"
    #: A frame was malformed, too large, or of an unknown type.
    PROTOCOL_ERROR = "protocol_error"
    #: The agent exceeded its frame budget.
    RATE_LIMITED = "rate_limited"
    #: No heartbeat arrived within the timeout.
    IDLE_TIMEOUT = "idle_timeout"
    #: A newer connection for this host was accepted, so this one is
    #: redundant. Distinct from an error: the agent should not treat it as a
    #: reason to back off, because the newer connection is probably its own.
    SUPERSEDED = "superseded"
    #: The controller is shutting down. The agent should reconnect.
    CONTROLLER_SHUTDOWN = "controller_shutdown"


class AgentMessage(BaseModel):
    """Common shape of every agent → controller frame.

    ``extra="forbid"`` on each concrete type below, so an unexpected key is a
    protocol error rather than a silently ignored field. A silently ignored
    field is how an agent comes to believe it reported something the
    controller never read.
    """

    model_config = {"extra": "forbid"}

    v: int = Field(description="Protocol version. Must be one the controller supports.")
    type: MessageType


class Heartbeat(AgentMessage):
    """Agent → controller liveness.

    Carries the agent's self-description as well, so a reconnecting agent
    refreshes the reported version and platform without a second frame.

    **None of these fields is trusted for a decision.** The host's identity
    comes from the credential that authenticated the connection, never from
    anything in this frame — there is deliberately no ``host_id`` field, so
    there is nothing for an agent to claim to be. That is the server-side
    attribution rule (``docs/ARCHITECTURE.md`` §7.3) expressed as an absence.
    """

    type: Literal[MessageType.HEARTBEAT] = MessageType.HEARTBEAT

    agent_version: str | None = Field(
        default=None,
        max_length=MAX_AGENT_VERSION_LENGTH,
        description="The agent's own version string. Advisory.",
    )
    platform: str | None = Field(
        default=None,
        max_length=MAX_PLATFORM_LENGTH,
        description="Reported os/arch, e.g. 'linux/amd64'. Advisory.",
    )
    uptime_seconds: int | None = Field(
        default=None,
        ge=0,
        # A century, so a nonsense value is refused rather than stored and
        # rendered as a date in the far future.
        le=3_155_760_000,
        description=(
            "How long the agent process has been running, as it reports. "
            "Integer seconds (ADR-019). Advisory."
        ),
    )

    # --- Configuration state, phase 2 ---------------------------------------
    #
    # What makes the synchronisation loop self-correcting: the agent states
    # what it is running on every heartbeat, so a missed push is noticed
    # within one interval rather than never. Both fields are optional, because
    # an agent that has never been given a configuration has none to report
    # and must say so by *omission* rather than by claiming version zero —
    # there is no version zero, and inventing one would make "never
    # configured" indistinguishable from "configured, then reset".

    config_version: int | None = Field(
        default=None,
        ge=1,
        le=MAX_CONFIG_VERSION,
        description=(
            "The configuration version this agent is running, or absent if "
            "it has none. Never trusted as a fact about what *should* be "
            "running — only compared against what the controller stored."
        ),
    )
    config_checksum: str | None = Field(
        default=None,
        max_length=CHECKSUM_LENGTH,
        description=(
            "Checksum of the running configuration. Compared as well as the "
            "version, so a truncated or tampered document cannot be reported "
            "as applied under a version that looks right."
        ),
    )

    def __repr__(self) -> str:
        """Representation naming the frame, not its contents.

        Heartbeat fields are not secret, but this repr appears in protocol
        error messages that are sent *to the agent*, and a message echoing
        the frame back would make the controller a reflector for whatever an
        agent chose to put in it.
        """
        return f"Heartbeat(v={self.v})"

    __str__ = __repr__


class HeartbeatAck(BaseModel):
    """Controller → agent acknowledgement.

    Restates the interval so an agent whose configuration has drifted — or
    which was built against an older default — converges without being
    redeployed. The agent is still free to heartbeat more often; the
    controller's timeout is what it must stay inside.

    Also states the configuration version the controller believes is current.
    That single field is what makes the synchronisation loop self-correcting:
    an agent comparing it against what it is running notices a missed push,
    a controller restart and its own stale local copy, all without any of
    them needing a mechanism of their own.
    """

    model_config = {"extra": "forbid"}

    v: int = PROTOCOL_VERSION
    type: Literal[MessageType.HEARTBEAT_ACK] = MessageType.HEARTBEAT_ACK
    interval_seconds: int = Field(
        default=HEARTBEAT_INTERVAL_SECONDS,
        description="How often the controller expects a heartbeat.",
    )
    #: Server time, so an agent on a machine with a wrong clock can log the
    #: discrepancy. Advisory in the other direction: the controller stamps
    #: every timestamp it stores with its own clock regardless.
    server_time: str = Field(description="ISO-8601 controller time.")
    config_version: int | None = Field(
        default=None,
        description=(
            "The configuration version the controller has for this host, or "
            "null when it has none. An agent running something else should "
            "send a config_request."
        ),
    )


class ConfigAvailable(BaseModel):
    """Controller → agent: a newer configuration exists.

    A notification, not the document. The queue this travels on is bounded,
    and dropping a notification costs at most one heartbeat interval of
    latency because the acknowledgement carries the same version — whereas a
    queue of documents would be kilobytes per entry for a saving of one round
    trip.
    """

    model_config = {"extra": "forbid"}

    v: int = PROTOCOL_VERSION
    type: Literal[MessageType.CONFIG_AVAILABLE] = MessageType.CONFIG_AVAILABLE
    version: int = Field(description="The version now current for this host.")


class ConfigSnapshot(BaseModel):
    """Controller → agent: the configuration document itself.

    ``document`` is a **string**, not an object, and that is the whole of
    ADR-050. It carries the canonical bytes the checksum was computed over, so
    the agent verifies ``sha256(document) == checksum`` on exactly what
    arrived and then parses it. Sending an object instead would make the
    comparison depend on the agent re-serialising it identically to the
    controller — which Python's ``json`` and Go's ``encoding/json`` do not
    guarantee, and the failure would look like tampering.
    """

    model_config = {"extra": "forbid"}

    v: int = PROTOCOL_VERSION
    type: Literal[MessageType.CONFIG_SNAPSHOT] = MessageType.CONFIG_SNAPSHOT
    version: int
    checksum: str = Field(description="`sha256:<hex>` over the UTF-8 bytes of `document`.")
    document: str = Field(
        description="The canonical configuration JSON, byte for byte as checksummed."
    )


class Close(BaseModel):
    """Controller → agent termination notice.

    Sent as a frame *before* the WebSocket close, because a close code alone
    gives the agent four digits to interpret and nothing to log. The reason is
    from a closed vocabulary so the agent can branch on it without parsing
    prose.
    """

    model_config = {"extra": "forbid"}

    v: int = PROTOCOL_VERSION
    type: Literal[MessageType.CLOSE] = MessageType.CLOSE
    reason: CloseReason
    #: Short operator-facing detail. Never credential material, and never an
    #: exception string: an agent's log is not a place to leak a stack trace
    #: from the controller.
    detail: str | None = Field(default=None, max_length=MAX_CLOSE_REASON_LENGTH)
    #: Present only on :attr:`CloseReason.UNSUPPORTED_PROTOCOL`, so the agent's
    #: log can name the versions that would have worked.
    supported_versions: list[int] | None = None


class ConfigRequest(AgentMessage):
    """Agent → controller: "send me the current configuration".

    No fields. There is deliberately nothing to ask *for*: an agent cannot
    request a particular version, or another host's configuration, because the
    host is decided by the credential that authenticated the connection and
    the version is whatever is current. A ``host_id`` or a ``version`` field
    here would be a field to abuse, and neither has a legitimate use — an
    agent that wants an old configuration is an agent that should be told the
    current one.
    """

    type: Literal[MessageType.CONFIG_REQUEST] = MessageType.CONFIG_REQUEST


class ConfigAck(AgentMessage):
    """Agent → controller: "I verified and applied this version".

    Carries the checksum as well as the version, so a truncated or tampered
    document cannot be acknowledged as applied under a version that looks
    right (``AGENT_ARCHITECTURE.md`` §6.3). The controller matches on both and
    records nothing if either disagrees.
    """

    type: Literal[MessageType.CONFIG_ACK] = MessageType.CONFIG_ACK

    version: int = Field(ge=1, le=MAX_CONFIG_VERSION)
    checksum: str = Field(max_length=CHECKSUM_LENGTH)


class ConfigNack(AgentMessage):
    """Agent → controller: "I refused this version, and here is why".

    The agent keeps running its last known good configuration. A monitoring
    agent that stops monitoring because someone typed a bad interval has
    failed at its job (``AGENT_ARCHITECTURE.md`` §6.3).

    ``problems`` is bounded in both dimensions at the protocol boundary rather
    than only at the storage one. The strings come from a host whose security
    the controller does not own, and a bound applied only before the INSERT
    would still have parsed and held an unbounded list in memory first.
    """

    type: Literal[MessageType.CONFIG_NACK] = MessageType.CONFIG_NACK

    version: int = Field(ge=1, le=MAX_CONFIG_VERSION)
    checksum: str = Field(max_length=CHECKSUM_LENGTH)
    problems: list[str] = Field(
        default_factory=list,
        description="Every reason the configuration was refused, not the first.",
    )

    @field_validator("problems")
    @classmethod
    def _bound_problems(cls, value: list[str]) -> list[str]:
        """Clamp count and length rather than refusing the whole frame.

        Deliberately a clamp and not a ``max_length``. A ``max_length`` makes
        an over-long list a validation failure, which this protocol answers by
        **closing the connection** — so an agent making a legitimate rejection
        with thirty-three problems would be disconnected for being verbose,
        losing both the rejection and the information an operator needs in
        order to fix the configuration. The truncation is marked, so nobody
        goes looking for a cause that was in the part discarded.

        An unbounded list is still impossible: a frame large enough to carry
        one exceeds :data:`MAX_MESSAGE_BYTES` and is refused before it reaches
        a JSON parser.
        """
        clamped = [problem[:MAX_PROBLEM_LENGTH] for problem in value[:MAX_PROBLEMS]]
        remaining = len(value) - MAX_PROBLEMS
        if remaining > 0:
            clamped.append(f"… and {remaining} further problem(s), not recorded")
        return clamped


#: Every frame an agent may send. A closed union, so an unrecognised ``type``
#: is refused rather than ignored.
IncomingMessage = Heartbeat | ConfigRequest | ConfigAck | ConfigNack


class ProtocolError(Exception):
    """A frame the controller refuses to process.

    Carries a :class:`CloseReason` so the endpoint maps one exception type to
    the right close frame rather than deciding per call site.
    """

    def __init__(self, reason: CloseReason, detail: str) -> None:
        self.reason = reason
        # Bounded here as well as in the model: this string ends up in a frame
        # with a length limit, and truncating at the boundary rather than at
        # construction would mean the exception and the frame disagreed.
        self.detail = detail[:MAX_CLOSE_REASON_LENGTH]
        super().__init__(f"{reason.value}: {self.detail}")


def is_supported_version(version: object) -> bool:
    """Whether the controller speaks this protocol version.

    Takes ``object`` rather than ``int`` because the value arrives from a
    parsed JSON frame, where a string or a float is a real possibility rather
    than one the type checker has already excluded. ``True`` is excluded
    explicitly: in Python ``True == 1``, so a frame claiming ``"v": true``
    would otherwise negotiate version 1.
    """
    if isinstance(version, bool) or not isinstance(version, int):
        return False
    return version in SUPPORTED_PROTOCOL_VERSIONS


def parse_agent_frame(raw: str | bytes) -> IncomingMessage:
    """Parse and validate one agent → controller frame.

    Every failure becomes a :class:`ProtocolError` carrying the close reason
    the endpoint should send. The order of the checks is the point: size before
    parse, so an oversized frame is never handed to a JSON parser; version
    before type, so an agent speaking a future protocol is told that rather
    than being told its frame type is unknown; and **type before model**, so
    one concrete model validates the frame rather than a union reporting the
    failures of all four.

    Raises:
        ProtocolError: the frame is too large, is not an object, declares an
            unsupported version, has an unknown type, or fails validation.
    """
    payload = raw.encode("utf-8") if isinstance(raw, str) else raw
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ProtocolError(
            CloseReason.PROTOCOL_ERROR,
            f"frame exceeds {MAX_MESSAGE_BYTES} bytes",
        )

    try:
        document: Any = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProtocolError(CloseReason.PROTOCOL_ERROR, "frame is not valid JSON") from exc

    if not isinstance(document, dict):
        raise ProtocolError(CloseReason.PROTOCOL_ERROR, "frame is not a JSON object")

    if not is_supported_version(document.get("v")):
        raise ProtocolError(
            CloseReason.UNSUPPORTED_PROTOCOL,
            f"supported versions: {', '.join(str(v) for v in SUPPORTED_PROTOCOL_VERSIONS)}",
        )

    # `str(...)` rather than a cast: the value comes from parsed JSON and may
    # be any type, and a non-string simply will not match a key in the closed
    # table below — which is the refusal wanted, reached without a type check
    # that would have to enumerate what a `type` field is allowed not to be.
    declared_type = document.get("type")
    model = _INCOMING_BY_TYPE.get(declared_type) if isinstance(declared_type, str) else None
    if model is None:
        # Named without echoing the value: an agent-supplied string reflected
        # into a close frame would make the controller a reflector. Note that
        # this also refuses the controller→agent types — an agent sending a
        # `heartbeat_ack` is either confused or probing, and either way the
        # frame has no handler.
        raise ProtocolError(CloseReason.PROTOCOL_ERROR, "unknown or unexpected message type")

    try:
        return model.model_validate(document)
    except ValueError as exc:
        # The validation error is *not* forwarded. It enumerates field names,
        # limits and received values, and this string is sent to the agent.
        # The frame type is safe to name: it came from the closed table above,
        # so it is one of four constants rather than agent-supplied text.
        raise ProtocolError(
            CloseReason.PROTOCOL_ERROR, f"{model.__name__} failed validation"
        ) from exc


#: Dispatch table from wire ``type`` to model. Closed, and the closure is the
#: security property: a frame type absent from here has no handler and is
#: refused, so a future message cannot be smuggled past a controller that does
#: not understand it.
_INCOMING_BY_TYPE: Final[dict[str, type[IncomingMessage]]] = {
    MessageType.HEARTBEAT.value: Heartbeat,
    MessageType.CONFIG_REQUEST.value: ConfigRequest,
    MessageType.CONFIG_ACK.value: ConfigAck,
    MessageType.CONFIG_NACK.value: ConfigNack,
}
