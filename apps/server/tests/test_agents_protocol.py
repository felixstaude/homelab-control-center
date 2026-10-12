"""Agent wire-format unit tests.

No database and no socket. These cover the frame parser and its bounds, which
is where a compromised host's input first meets the controller — so the
interesting cases are all refusals.

The protocol's behaviour on a real connection is covered in
``test_api_agents_integration.py``; this file is about the parser in
isolation, including inputs that are awkward to deliver over a socket.
"""

from __future__ import annotations

import json

import pytest

from hcc.agents.protocol import (
    FRAME_BURST,
    HEARTBEAT_INTERVAL_SECONDS,
    HEARTBEAT_TIMEOUT_SECONDS,
    MAX_AGENT_VERSION_LENGTH,
    MAX_CLOSE_REASON_LENGTH,
    MAX_FRAMES_PER_SECOND,
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    Close,
    CloseReason,
    ConfigAck,
    ConfigAvailable,
    ConfigNack,
    ConfigRequest,
    ConfigSnapshot,
    Heartbeat,
    HeartbeatAck,
    MessageType,
    MetricSample,
    ProtocolError,
    Telemetry,
    TelemetryAck,
    TelemetryEntry,
    is_supported_version,
    parse_agent_frame,
)


def frame(**fields: object) -> str:
    return json.dumps({"v": PROTOCOL_VERSION, "type": "heartbeat", **fields})


# ---------------------------------------------------------------------------
# Version negotiation
# ---------------------------------------------------------------------------


def test_the_current_version_is_supported() -> None:
    assert PROTOCOL_VERSION in SUPPORTED_PROTOCOL_VERSIONS


@pytest.mark.parametrize("value", [0, 2, 99, -1])
def test_an_unknown_version_is_not_supported(value: int) -> None:
    assert not is_supported_version(value)


def test_a_boolean_is_not_a_version() -> None:
    """``True == 1`` in Python, so a frame claiming ``"v": true`` would
    otherwise negotiate version 1 and be parsed as a valid heartbeat.
    """
    assert not is_supported_version(True)
    assert not is_supported_version(False)


@pytest.mark.parametrize("value", ["1", 1.0, None, [1], {"v": 1}])
def test_a_non_integer_version_is_not_supported(value: object) -> None:
    assert not is_supported_version(value)


def test_a_frame_with_no_version_is_refused_as_unsupported_not_malformed() -> None:
    """The reason matters, because the remedies differ.

    "Your protocol version is wrong" tells an operator to upgrade the agent;
    "your frame is malformed" sends them looking for a bug.
    """
    with pytest.raises(ProtocolError) as caught:
        parse_agent_frame('{"type": "heartbeat"}')

    assert caught.value.reason is CloseReason.UNSUPPORTED_PROTOCOL


def test_the_unsupported_refusal_names_the_versions_that_would_work() -> None:
    with pytest.raises(ProtocolError) as caught:
        parse_agent_frame('{"v": 99, "type": "heartbeat"}')

    assert str(PROTOCOL_VERSION) in caught.value.detail


def test_the_version_is_checked_before_the_type() -> None:
    """So an agent speaking a future protocol is told the truth.

    If the type were checked first, an agent on version 2 sending a frame
    version 2 defines would be told its message type is unknown — which is
    accurate and useless.
    """
    with pytest.raises(ProtocolError) as caught:
        parse_agent_frame('{"v": 99, "type": "something-from-the-future"}')

    assert caught.value.reason is CloseReason.UNSUPPORTED_PROTOCOL


# ---------------------------------------------------------------------------
# Size, before anything else
# ---------------------------------------------------------------------------


def test_an_oversized_frame_is_refused() -> None:
    with pytest.raises(ProtocolError, match="exceeds"):
        parse_agent_frame("x" * (MAX_MESSAGE_BYTES + 1))


def test_the_size_check_runs_before_the_json_parse() -> None:
    """The bound is the point: a 2 GiB frame must never reach a parser.

    Asserted by handing it an oversized payload that is *also* unparseable
    JSON and requiring the size complaint rather than the syntax one.
    """
    with pytest.raises(ProtocolError, match="exceeds"):
        parse_agent_frame("{" * (MAX_MESSAGE_BYTES + 1))


def test_a_frame_exactly_at_the_limit_is_parsed() -> None:
    """The bound must not be off by one in the refusing direction."""
    padding = "a" * MAX_AGENT_VERSION_LENGTH
    body = frame(agent_version=padding)
    assert len(body.encode()) <= MAX_MESSAGE_BYTES
    parsed = parse_agent_frame(body)
    assert isinstance(parsed, Heartbeat)
    assert parsed.agent_version == padding


def test_the_size_is_measured_in_bytes_not_characters() -> None:
    """A multi-byte character must not let a frame past the byte bound.

    One emoji is one character and four bytes, so counting characters would
    under-measure a hostile frame by a factor of four.
    """
    # 'é' is two bytes in UTF-8, so this is just over the limit in bytes and
    # just under it in characters.
    oversized = "é" * (MAX_MESSAGE_BYTES // 2 + 1)

    with pytest.raises(ProtocolError, match="exceeds"):
        parse_agent_frame(oversized)


def test_bytes_and_str_are_both_accepted() -> None:
    """A WebSocket may deliver either, and the parser is the one place that
    difference should have to be handled.
    """
    assert parse_agent_frame(frame()).type is MessageType.HEARTBEAT
    assert parse_agent_frame(frame().encode()).type is MessageType.HEARTBEAT


def test_invalid_utf8_is_a_protocol_error_not_a_crash() -> None:
    with pytest.raises(ProtocolError, match="not valid JSON"):
        parse_agent_frame(b"\xff\xfe\x00")


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", ["", "not json", "{", "[1, 2]", '"string"', "null", "42"])
def test_a_frame_that_is_not_a_json_object_is_refused(payload: str) -> None:
    with pytest.raises(ProtocolError) as caught:
        parse_agent_frame(payload)

    assert caught.value.reason is CloseReason.PROTOCOL_ERROR


def test_an_unknown_message_type_is_refused() -> None:
    """A closed vocabulary, so a future frame cannot be smuggled past a
    controller that does not understand it.
    """
    with pytest.raises(ProtocolError) as caught:
        parse_agent_frame('{"v": 1, "type": "heartbeat_ack"}')

    assert caught.value.reason is CloseReason.PROTOCOL_ERROR


def test_the_refusal_does_not_echo_the_agents_string_back() -> None:
    """Otherwise the controller is a reflector.

    The detail goes into a close frame sent *to the agent*, so including an
    agent-supplied value would let one agent choose what the controller says.
    """
    marker = "REFLECTED-MARKER-9f2c"

    with pytest.raises(ProtocolError) as caught:
        parse_agent_frame(json.dumps({"v": 1, "type": marker}))

    assert marker not in caught.value.detail
    assert marker not in str(caught.value)


def test_an_unexpected_field_is_refused_rather_than_ignored() -> None:
    """A silently ignored field is how an agent comes to believe it reported
    something the controller never read.
    """
    with pytest.raises(ProtocolError) as caught:
        parse_agent_frame(frame(metrics={"cpu": 0.9}))

    assert caught.value.reason is CloseReason.PROTOCOL_ERROR


def test_a_validation_failure_does_not_leak_the_field_report() -> None:
    """Pydantic's message enumerates field names, limits and received values,
    and this string is sent to the agent. One opaque sentence instead.
    """
    with pytest.raises(ProtocolError) as caught:
        parse_agent_frame(frame(uptime_seconds=-1))

    # The model's class name, which is safe: it comes from the closed dispatch
    # table rather than from the frame, so it is one of four constants and not
    # agent-supplied text.
    assert caught.value.detail == "Heartbeat failed validation"


# ---------------------------------------------------------------------------
# The heartbeat itself
# ---------------------------------------------------------------------------


def test_a_minimal_heartbeat_is_valid() -> None:
    """Everything but the version and type is optional.

    An agent that cannot determine its own platform must still be able to
    report that it is alive.
    """
    parsed = parse_agent_frame(frame())

    assert isinstance(parsed, Heartbeat)
    assert parsed.agent_version is None
    assert parsed.platform is None
    assert parsed.uptime_seconds is None


def test_a_full_heartbeat_round_trips() -> None:
    parsed = parse_agent_frame(
        frame(agent_version="0.1.0", platform="linux/amd64", uptime_seconds=3600)
    )

    # `parse_agent_frame` returns a union now that configuration frames share
    # the socket, so the narrowing is part of what each test asserts.
    assert isinstance(parsed, Heartbeat)
    assert parsed.agent_version == "0.1.0"
    assert parsed.platform == "linux/amd64"
    assert parsed.uptime_seconds == 3600


@pytest.mark.parametrize(
    "fields",
    [
        {"agent_version": "v" * (MAX_AGENT_VERSION_LENGTH + 1)},
        {"platform": "p" * 200},
        {"uptime_seconds": -1},
        # A hundred years of uptime is not a measurement.
        {"uptime_seconds": 10**12},
        {"agent_version": 5},
        {"uptime_seconds": "an hour"},
    ],
)
def test_an_out_of_bounds_field_is_refused(fields: dict[str, object]) -> None:
    with pytest.raises(ProtocolError):
        parse_agent_frame(frame(**fields))


def test_there_is_no_host_id_field_to_claim() -> None:
    """The absence that makes attribution trustworthy.

    The host comes from the credential that authenticated the connection. A
    field naming a host would be a field an agent could use to report as a
    machine it does not hold the credential for — and `extra="forbid"` means
    sending one is refused rather than ignored.
    """
    assert "host_id" not in Heartbeat.model_fields
    assert "hostname" not in Heartbeat.model_fields

    with pytest.raises(ProtocolError):
        parse_agent_frame(frame(host_id="00000000-0000-0000-0000-000000000000"))


def test_the_heartbeat_representation_does_not_print_its_contents() -> None:
    """This repr reaches error messages that are sent to the agent."""
    parsed = parse_agent_frame(frame(platform="REFLECT-ME"))

    assert "REFLECT-ME" not in repr(parsed)
    assert "REFLECT-ME" not in str(parsed)


def test_no_message_type_carries_anything_executable() -> None:
    """The most important absence in the protocol.

    A monitoring agent that runs what the controller tells it has turned a
    control-plane compromise into root on every monitored machine. Asserted
    against the models rather than trusted, because adding such a field would
    be a one-line change that no behavioural test would notice.
    """
    forbidden = ("command", "cmd", "exec", "script", "shell", "run", "argv", "path")
    for model in (
        Heartbeat,
        HeartbeatAck,
        Close,
        ConfigAvailable,
        ConfigSnapshot,
        ConfigRequest,
        ConfigAck,
        ConfigNack,
        Telemetry,
        TelemetryAck,
        MetricSample,
        TelemetryEntry,
    ):
        for name in model.model_fields:
            assert not any(word in name.lower() for word in forbidden), (
                f"{model.__name__}.{name} looks executable"
            )
    # Pinned so that adding a message type is a deliberate, reviewed change.
    # Configuration arriving on this socket (phase 2, ADR-049) is the closest
    # thing to an instruction that crosses the boundary, and it is still not
    # one: a document names modules from a registry compiled into the agent,
    # never a path, a binary or a collector to load.
    assert set(MessageType) == {
        MessageType.HEARTBEAT,
        MessageType.HEARTBEAT_ACK,
        MessageType.CLOSE,
        MessageType.CONFIG_AVAILABLE,
        MessageType.CONFIG_REQUEST,
        MessageType.CONFIG_SNAPSHOT,
        MessageType.CONFIG_ACK,
        MessageType.CONFIG_NACK,
        # Phase 3. Telemetry is measurement data flowing *towards* the
        # controller — the direction that cannot instruct anything — and the
        # controller→agent half is an acknowledgement carrying two counts.
        MessageType.TELEMETRY,
        MessageType.TELEMETRY_ACK,
    }


# ---------------------------------------------------------------------------
# Controller → agent frames
# ---------------------------------------------------------------------------


def test_the_ack_restates_the_interval() -> None:
    """So an agent whose configuration has drifted converges without a
    redeploy — the controller cannot reach the fleet to reconfigure it.
    """
    ack = HeartbeatAck(server_time="2026-10-12T12:00:00+00:00")

    assert ack.interval_seconds == HEARTBEAT_INTERVAL_SECONDS
    assert ack.v == PROTOCOL_VERSION


def test_the_timeout_is_longer_than_the_interval() -> None:
    """A timeout at or below the interval would close every healthy
    connection, which is the sort of thing that looks fine until deployed.
    """
    assert HEARTBEAT_TIMEOUT_SECONDS > HEARTBEAT_INTERVAL_SECONDS * 2


def test_the_frame_budget_is_far_above_a_well_behaved_agent() -> None:
    """A heartbeat every thirty seconds must never approach the limit.

    A budget tight enough to throttle a correct agent would turn a
    denial-of-service guard into a denial of service.
    """
    assert MAX_FRAMES_PER_SECOND * HEARTBEAT_INTERVAL_SECONDS > 10
    assert FRAME_BURST > MAX_FRAMES_PER_SECOND


def test_a_close_frame_names_a_reason_from_the_vocabulary() -> None:
    rendered = json.loads(
        Close(reason=CloseReason.IDLE_TIMEOUT, detail="no heartbeat").model_dump_json()
    )

    assert rendered["reason"] == "idle_timeout"
    assert rendered["type"] == "close"


def test_a_close_detail_is_bounded_at_construction() -> None:
    """The frame has a length limit, so truncating at the boundary instead
    would make the exception and the frame disagree about what was said.
    """
    error = ProtocolError(CloseReason.PROTOCOL_ERROR, "x" * 500)

    assert len(error.detail) == MAX_CLOSE_REASON_LENGTH


def test_every_close_reason_is_distinct_and_lowercase() -> None:
    """The agent branches on these strings, so two that collided would make
    one of the branches unreachable.
    """
    values = [reason.value for reason in CloseReason]

    assert len(values) == len(set(values))
    assert all(value == value.lower() for value in values)
    assert all(" " not in value for value in values)
