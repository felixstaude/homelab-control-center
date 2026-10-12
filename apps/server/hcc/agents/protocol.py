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

## There is no command execution in this protocol

Deliberately, and it is the most important absence here. No message carries a
shell command, a script, a path to execute, or a module name to import. The
controller-to-agent direction in this milestone is limited to protocol
housekeeping — an acknowledgement and a close — and the agent-to-controller
direction to liveness and self-description.

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

from pydantic import BaseModel, Field

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


class MessageType(enum.StrEnum):
    """Every frame type that exists.

    A closed set, and the closure is the security property: an unrecognised
    ``type`` is refused rather than ignored, so a future frame cannot be
    smuggled past a controller that does not understand it.
    """

    #: Agent → controller. Liveness, plus the agent's view of itself.
    HEARTBEAT = "heartbeat"
    #: Controller → agent. Acknowledges a heartbeat and restates the interval,
    #: so an agent that has drifted converges without a redeploy.
    HEARTBEAT_ACK = "heartbeat_ack"
    #: Controller → agent. The connection is ending, and why.
    CLOSE = "close"


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


def parse_agent_frame(raw: str | bytes) -> Heartbeat:
    """Parse and validate one agent → controller frame.

    Every failure becomes a :class:`ProtocolError` carrying the close reason
    the endpoint should send. Four checks, in this order, and the order is the
    point: size before parse, so an oversized frame is never handed to a JSON
    parser; version before type, so an agent speaking a future protocol is
    told that rather than being told its frame type is unknown.

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

    message_type = document.get("type")
    if message_type != MessageType.HEARTBEAT.value:
        # Named without echoing the value: an agent-supplied string reflected
        # into a close frame would make the controller a reflector.
        raise ProtocolError(CloseReason.PROTOCOL_ERROR, "unknown or unexpected message type")

    try:
        return Heartbeat.model_validate(document)
    except ValueError as exc:
        # The validation error is *not* forwarded. It enumerates field names,
        # limits and received values, and this string is sent to the agent.
        raise ProtocolError(CloseReason.PROTOCOL_ERROR, "heartbeat failed validation") from exc
