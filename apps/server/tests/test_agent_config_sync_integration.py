"""Configuration synchronisation over the agent socket, against real PostgreSQL.

Phase 2. The ASGI application is called directly with a ``websocket`` scope,
the same way ``test_api_agents_integration.py`` does and for the same reason:
it is not a shortcut around the transport but the transport itself, with the
test standing where the server would.

What these cover that the service tests cannot:

* the **snapshot sent on connect**, so a freshly started agent is configured
  without waiting for a heartbeat
* the checksum verifying against the document's exact bytes **after a round
  trip through the frame** — the one property ADR-050 exists for
* acknowledgement and rejection arriving as frames, and what each does to the
  host's reported state
* the **push**: an administrator's save reaching a connected agent, driven by
  a real HTTP request against the same application while the socket is open
* that an agent cannot acknowledge, reject or request another host's
  configuration, because there is no field in which to name one
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest

from hcc.agents.protocol import PROTOCOL_VERSION
from hcc.models.user import UserRole
from hcc.monitoring.canonical import checksum_of_bytes
from hcc.monitoring.modules import MODULE_IDS

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from contextlib import AbstractAsyncContextManager

    from httpx import AsyncClient

    from hcc.models.user import User

    ApiFactory = Callable[..., AbstractAsyncContextManager[AsyncClient]]
    SeedUser = Callable[..., Awaitable[User]]
    SignIn = Callable[..., Awaitable[str]]
    ReadSql = Callable[..., Awaitable[Any]]

pytestmark = pytest.mark.integration

HOSTS = "/api/v1/hosts"
MONITORING = "/api/v1/monitoring"
ENROLL = "/api/v1/agents/enroll"
CONNECT = "/api/v1/agents/connect"


def csrf(token: str) -> dict[str, str]:
    return {"X-CSRF-Token": token}


def bearer(credential: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {credential}"}


async def enrolled_agent(
    client: AsyncClient, seed_user: SeedUser, sign_in: SignIn, name: str = "homeserver"
) -> tuple[str, str, str]:
    """A host with a live agent credential.

    Returns ``(host_id, agent_credential, csrf_token)`` — the token too,
    because most of these tests go on to change the configuration through the
    administrative API.
    """
    await seed_user(client, username="admin", role=UserRole.ADMIN)
    token = await sign_in(client, "admin")
    host_id = (await client.post(HOSTS, json={"name": name}, headers=csrf(token))).json()["host_id"]
    enrollment = (await client.post(f"{HOSTS}/{host_id}/enrollment", headers=csrf(token))).json()[
        "credential"
    ]
    credential = (
        await client.post(
            ENROLL,
            headers=bearer(enrollment),
            json={"agent_version": "0.1.0", "platform": "linux/amd64"},
        )
    ).json()["credential"]
    return host_id, credential, token


async def drive_socket(
    client: AsyncClient,
    *,
    credential: str,
    frames: list[str] | None = None,
    hold: float = 0.0,
) -> list[dict[str, Any]]:
    """Open the agent socket, send ``frames``, and return everything sent back."""
    app = client.app_state.app  # type: ignore[attr-defined]

    inbound: list[dict[str, Any]] = [{"type": "websocket.connect"}]
    for frame in frames or []:
        inbound.append({"type": "websocket.receive", "text": frame})
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        if inbound:
            return inbound.pop(0)
        if hold:
            await asyncio.sleep(hold)
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(_scope(credential), receive, send)
    return sent


def _scope(credential: str) -> dict[str, Any]:
    return {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "scheme": "ws",
        "http_version": "1.1",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 51234),
        "path": CONNECT,
        "raw_path": CONNECT.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"host", b"testserver"),
            (b"authorization", f"Bearer {credential}".encode()),
        ],
        "subprotocols": [],
        "state": {},
    }


def text_frames(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every JSON frame the controller sent, decoded."""
    return [
        json.loads(message["text"])
        for message in messages
        if message["type"] == "websocket.send" and message.get("text")
    ]


def only(frames: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [frame for frame in frames if frame["type"] == kind]


def heartbeat(**extra: object) -> str:
    return json.dumps({"v": PROTOCOL_VERSION, "type": "heartbeat", **extra})


# ---------------------------------------------------------------------------
# The snapshot on connect
# ---------------------------------------------------------------------------


async def test_a_connecting_agent_is_sent_its_configuration_at_once(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """Without waiting for a heartbeat.

    A freshly started agent has nothing to collect with until it has a
    configuration, so making it wait up to thirty seconds for one would be a
    gap at exactly the moment an operator is watching.
    """
    async with api_factory() as client:
        _, credential, _ = await enrolled_agent(client, seed_user, sign_in)
        frames = text_frames(await drive_socket(client, credential=credential))

    snapshots = only(frames, "config_snapshot")
    assert len(snapshots) == 1
    assert snapshots[0]["version"] == 1


async def test_the_snapshot_checksum_verifies_against_the_document_it_arrived_with(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """ADR-050's whole point, after a real round trip through the frame.

    The document travels as a **string**, so the digest covers exactly the
    bytes the agent received. A nested object would make this comparison
    depend on the agent re-serialising it the same way the controller did.
    """
    async with api_factory() as client:
        _, credential, _ = await enrolled_agent(client, seed_user, sign_in)
        frames = text_frames(await drive_socket(client, credential=credential))

    snapshot = only(frames, "config_snapshot")[0]
    assert isinstance(snapshot["document"], str)
    assert checksum_of_bytes(snapshot["document"].encode("utf-8")) == snapshot["checksum"]


async def test_the_first_configuration_an_agent_receives_enables_nothing(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """§2.1 at the only point where it finally matters: on the wire.

    Every earlier test of default-deny checks the controller's own view.
    This one checks what the agent is actually told, which is the claim the
    product makes.
    """
    async with api_factory() as client:
        _, credential, _ = await enrolled_agent(client, seed_user, sign_in)
        frames = text_frames(await drive_socket(client, credential=credential))

    document = json.loads(only(frames, "config_snapshot")[0]["document"])
    assert set(document["modules"]) == set(MODULE_IDS)
    assert all(module["enabled"] is False for module in document["modules"].values())


async def test_the_document_carries_the_limits_and_its_own_host_id(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    async with api_factory() as client:
        host_id, credential, _ = await enrolled_agent(client, seed_user, sign_in)
        frames = text_frames(await drive_socket(client, credential=credential))

    document = json.loads(only(frames, "config_snapshot")[0]["document"])
    assert document["host_id"] == host_id
    assert document["limits"]["min_interval_seconds"] == 1
    assert document["limits"]["module_min_interval_seconds"]["load"] == 5


async def test_an_agent_connecting_after_a_change_gets_the_newer_configuration(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """The reconnect path *is* the reconciliation.

    A host whose configuration changed while it was offline is brought up to
    date by connecting, with nobody touching it — which is what makes a
    dropped push survivable.
    """
    async with api_factory() as client:
        host_id, credential, token = await enrolled_agent(client, seed_user, sign_in)
        await drive_socket(client, credential=credential)

        await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu",
            json={"enabled": True, "schedule": {"kind": "interval", "interval_seconds": 5}},
            headers=csrf(token),
        )

        frames = text_frames(await drive_socket(client, credential=credential))

    snapshot = only(frames, "config_snapshot")[0]
    document = json.loads(snapshot["document"])
    assert snapshot["version"] == 2
    assert document["modules"]["cpu"]["enabled"] is True
    assert document["modules"]["cpu"]["schedule"]["interval_seconds"] == 5


async def test_reconnecting_with_nothing_changed_does_not_mint_a_revision(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
) -> None:
    """The idempotence the reconnect path depends on.

    Without it a flapping link would leave a host permanently "pending" on a
    configuration identical to the one it is already running, and the revision
    table would grow one row per reconnection.
    """
    async with api_factory() as client:
        _, credential, _ = await enrolled_agent(client, seed_user, sign_in)
        for _ in range(4):
            await drive_socket(client, credential=credential)
        rows = await read_sql(client, "SELECT count(*) FROM host_config_revisions")

    assert rows[0][0] == 1


# ---------------------------------------------------------------------------
# Requesting
# ---------------------------------------------------------------------------


async def test_an_agent_can_ask_for_its_configuration(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    async with api_factory() as client:
        _, credential, _ = await enrolled_agent(client, seed_user, sign_in)
        frames = text_frames(
            await drive_socket(
                client,
                credential=credential,
                frames=[json.dumps({"v": PROTOCOL_VERSION, "type": "config_request"})],
            )
        )

    snapshots = only(frames, "config_snapshot")
    # One on connect, one in answer to the request.
    assert len(snapshots) == 2
    assert snapshots[0] == snapshots[1]


async def test_a_config_request_carrying_a_host_id_is_refused(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """There is no field in which to name a host, and ``extra="forbid"`` makes
    inventing one a protocol error rather than an ignored key.

    This is the cross-host attack in its most direct form: an agent asking for
    somebody else's configuration. It fails because there is nothing to ask
    with, not because of a check that could be forgotten.
    """
    async with api_factory() as client:
        _, credential, _ = await enrolled_agent(client, seed_user, sign_in)
        other = "11111111-2222-3333-4444-555555555555"
        frames = text_frames(
            await drive_socket(
                client,
                credential=credential,
                frames=[
                    json.dumps({"v": PROTOCOL_VERSION, "type": "config_request", "host_id": other})
                ],
            )
        )

    closes = only(frames, "close")
    assert closes
    assert closes[0]["reason"] == "protocol_error"
    # Exactly one snapshot — the one sent on connect, for this host.
    assert len(only(frames, "config_snapshot")) == 1


# ---------------------------------------------------------------------------
# Acknowledging
# ---------------------------------------------------------------------------


async def test_an_acknowledgement_is_recorded(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
) -> None:
    async with api_factory() as client:
        host_id, credential, _ = await enrolled_agent(client, seed_user, sign_in)
        first = text_frames(await drive_socket(client, credential=credential))
        snapshot = only(first, "config_snapshot")[0]

        await drive_socket(
            client,
            credential=credential,
            frames=[
                json.dumps(
                    {
                        "v": PROTOCOL_VERSION,
                        "type": "config_ack",
                        "version": snapshot["version"],
                        "checksum": snapshot["checksum"],
                    }
                )
            ],
        )
        rows = await read_sql(
            client,
            "SELECT acknowledged_at IS NOT NULL FROM host_config_revisions "
            f"WHERE host_id = '{host_id}'",
        )

    assert rows[0][0] is True


async def test_an_acknowledgement_with_a_wrong_checksum_records_nothing_and_stays_connected(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
) -> None:
    """A version alone would let an agent claim to be running revision 1 while
    running something else, and the controller would report it up to date.

    Not fatal, though: the connection stays open, because an unmatched
    acknowledgement is most often an ordinary race — the agent acknowledging
    version 4 while the controller has already minted 5.
    """
    async with api_factory() as client:
        host_id, credential, _ = await enrolled_agent(client, seed_user, sign_in)
        messages = await drive_socket(
            client,
            credential=credential,
            frames=[
                json.dumps(
                    {
                        "v": PROTOCOL_VERSION,
                        "type": "config_ack",
                        "version": 1,
                        "checksum": "sha256:" + "b" * 64,
                    }
                ),
                heartbeat(),
            ],
        )
        rows = await read_sql(
            client,
            f"SELECT acknowledged_at FROM host_config_revisions WHERE host_id = '{host_id}'",
        )

    assert rows[0][0] is None
    # Still serving: the heartbeat after the bad acknowledgement was answered.
    assert only(text_frames(messages), "heartbeat_ack")


async def test_a_malformed_checksum_in_an_acknowledgement_is_a_protocol_error(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """Bounded at the protocol boundary: the field has a maximum length, so a
    megabyte of "checksum" never reaches a query.
    """
    async with api_factory() as client:
        _, credential, _ = await enrolled_agent(client, seed_user, sign_in)
        frames = text_frames(
            await drive_socket(
                client,
                credential=credential,
                frames=[
                    json.dumps(
                        {
                            "v": PROTOCOL_VERSION,
                            "type": "config_ack",
                            "version": 1,
                            "checksum": "x" * 5_000,
                        }
                    )
                ],
            )
        )

    closes = only(frames, "close")
    assert closes
    assert closes[0]["reason"] == "protocol_error"


async def test_an_agent_cannot_acknowledge_another_hosts_revision(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
) -> None:
    """Even with the right version and the right checksum.

    The host comes from the credential, so the acknowledgement is matched
    against *that* host's revisions and a neighbour's digest matches nothing.
    """
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")

        async def register(name: str) -> tuple[str, str]:
            host = (await client.post(HOSTS, json={"name": name}, headers=csrf(token))).json()[
                "host_id"
            ]
            enrollment = (
                await client.post(f"{HOSTS}/{host}/enrollment", headers=csrf(token))
            ).json()["credential"]
            credential = (await client.post(ENROLL, headers=bearer(enrollment), json={})).json()[
                "credential"
            ]
            return host, credential

        victim, _ = await register("victim")
        attacker, attacker_credential = await register("attacker")

        # Give the victim a revision with a known checksum.
        await client.put(
            f"{HOSTS}/{victim}/monitoring/modules/cpu",
            json={"enabled": True},
            headers=csrf(token),
        )
        victim_rows = await read_sql(
            client,
            f"SELECT version, checksum FROM host_config_revisions WHERE host_id = '{victim}'",
        )
        version, checksum = victim_rows[0]

        await drive_socket(
            client,
            credential=attacker_credential,
            frames=[
                json.dumps(
                    {
                        "v": PROTOCOL_VERSION,
                        "type": "config_ack",
                        "version": version,
                        "checksum": checksum,
                    }
                )
            ],
        )

        acked = await read_sql(
            client,
            "SELECT host_id FROM host_config_revisions WHERE acknowledged_at IS NOT NULL",
        )

    assert acked == []
    assert attacker != victim


# ---------------------------------------------------------------------------
# Rejecting
# ---------------------------------------------------------------------------


async def test_a_rejection_records_every_problem_and_is_audited(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
) -> None:
    async with api_factory() as client:
        host_id, credential, _ = await enrolled_agent(client, seed_user, sign_in)
        first = text_frames(await drive_socket(client, credential=credential))
        snapshot = only(first, "config_snapshot")[0]

        await drive_socket(
            client,
            credential=credential,
            frames=[
                json.dumps(
                    {
                        "v": PROTOCOL_VERSION,
                        "type": "config_nack",
                        "version": snapshot["version"],
                        "checksum": snapshot["checksum"],
                        "problems": ["swap: this host has no swap configured"],
                    }
                )
            ],
        )

        rows = await read_sql(
            client,
            "SELECT rejected_at IS NOT NULL, rejection_problems "
            f"FROM host_config_revisions WHERE host_id = '{host_id}'",
        )
        events = await read_sql(
            client,
            "SELECT outcome, details FROM audit_events WHERE action = 'monitoring.config_rejected'",
        )

    assert rows[0][0] is True
    assert rows[0][1] == ["swap: this host has no swap configured"]
    outcome, details = events[0]
    assert outcome == "failure"
    assert details["problems"] == ["swap: this host has no swap configured"]


async def test_a_rejection_has_no_human_actor(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
) -> None:
    """The agent refused it. Attributing that to whoever last saved the
    configuration would be a claim about a person who was not present.
    """
    async with api_factory() as client:
        _, credential, _ = await enrolled_agent(client, seed_user, sign_in)
        snapshot = only(
            text_frames(await drive_socket(client, credential=credential)), "config_snapshot"
        )[0]
        await drive_socket(
            client,
            credential=credential,
            frames=[
                json.dumps(
                    {
                        "v": PROTOCOL_VERSION,
                        "type": "config_nack",
                        "version": snapshot["version"],
                        "checksum": snapshot["checksum"],
                        "problems": ["nope"],
                    }
                )
            ],
        )
        rows = await read_sql(
            client,
            "SELECT actor_user_id FROM audit_events WHERE action = 'monitoring.config_rejected'",
        )

    assert rows[0][0] is None


async def test_an_oversized_problem_list_is_clamped_rather_than_disconnecting(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
) -> None:
    """A rejection that got the connection killed for being verbose would lose
    both the rejection and the information needed to fix the configuration.
    """
    async with api_factory() as client:
        host_id, credential, _ = await enrolled_agent(client, seed_user, sign_in)
        snapshot = only(
            text_frames(await drive_socket(client, credential=credential)), "config_snapshot"
        )[0]
        messages = await drive_socket(
            client,
            credential=credential,
            frames=[
                json.dumps(
                    {
                        "v": PROTOCOL_VERSION,
                        "type": "config_nack",
                        "version": snapshot["version"],
                        "checksum": snapshot["checksum"],
                        "problems": ["p" * 400] * 60,
                    }
                ),
                heartbeat(),
            ],
        )
        rows = await read_sql(
            client,
            f"SELECT rejection_problems FROM host_config_revisions WHERE host_id = '{host_id}'",
        )

    problems = rows[0][0]
    assert len(problems) == 33
    assert all(len(problem) <= 256 for problem in problems[:32])
    assert "further problem(s), not recorded" in problems[-1]
    # Still connected: the heartbeat after the rejection was answered.
    assert only(text_frames(messages), "heartbeat_ack")


# ---------------------------------------------------------------------------
# The heartbeat as the self-correcting loop
# ---------------------------------------------------------------------------


async def test_a_heartbeat_reports_the_running_configuration(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
) -> None:
    async with api_factory() as client:
        host_id, credential, _ = await enrolled_agent(client, seed_user, sign_in)
        snapshot = only(
            text_frames(await drive_socket(client, credential=credential)), "config_snapshot"
        )[0]
        await drive_socket(
            client,
            credential=credential,
            frames=[
                heartbeat(config_version=snapshot["version"], config_checksum=snapshot["checksum"])
            ],
        )
        rows = await read_sql(
            client,
            "SELECT reported_config_version, reported_config_checksum "
            f"FROM hosts WHERE id = '{host_id}'",
        )

    assert rows[0][0] == snapshot["version"]
    assert rows[0][1] == snapshot["checksum"]


async def test_the_acknowledgement_states_the_version_the_controller_has(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """The one field that makes the loop self-correcting: an agent comparing it
    against what it is running notices a missed push, a controller restart and
    its own stale local copy, without any of them needing a mechanism.
    """
    async with api_factory() as client:
        host_id, credential, token = await enrolled_agent(client, seed_user, sign_in)
        await drive_socket(client, credential=credential)
        await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/memory",
            json={"enabled": True},
            headers=csrf(token),
        )

        frames = text_frames(
            await drive_socket(client, credential=credential, frames=[heartbeat(config_version=1)])
        )

    ack = only(frames, "heartbeat_ack")[0]
    assert ack["config_version"] == 2


async def test_an_agent_that_has_never_been_configured_reports_no_version(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
) -> None:
    """By omission, not by claiming version zero.

    There is no version zero, and inventing one would make "never configured"
    indistinguishable from "configured, then something went wrong".
    """
    async with api_factory() as client:
        host_id, credential, _ = await enrolled_agent(client, seed_user, sign_in)
        await drive_socket(client, credential=credential, frames=[heartbeat()])
        rows = await read_sql(
            client, f"SELECT reported_config_version FROM hosts WHERE id = '{host_id}'"
        )

    assert rows[0][0] is None


async def test_a_claimed_version_of_zero_is_refused(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    async with api_factory() as client:
        _, credential, _ = await enrolled_agent(client, seed_user, sign_in)
        frames = text_frames(
            await drive_socket(client, credential=credential, frames=[heartbeat(config_version=0)])
        )
    assert only(frames, "close")[0]["reason"] == "protocol_error"


async def test_an_absurd_claimed_version_is_refused_before_it_reaches_a_column(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """Without the bound, an agent claiming 2**63 would be a failed insert on a
    path that should not be able to fail.
    """
    async with api_factory() as client:
        _, credential, _ = await enrolled_agent(client, seed_user, sign_in)
        frames = text_frames(
            await drive_socket(
                client, credential=credential, frames=[heartbeat(config_version=2**62)]
            )
        )
    assert only(frames, "close")[0]["reason"] == "protocol_error"


async def test_the_full_loop_reaches_up_to_date(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """Snapshot, acknowledge, heartbeat — and only then does the controller say
    the configuration is live.

    This is the §23 distinction completed: saved, then pending, then applied.
    """
    async with api_factory() as client:
        host_id, credential, token = await enrolled_agent(client, seed_user, sign_in)
        await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu",
            json={"enabled": True},
            headers=csrf(token),
        )
        assert (await client.get(f"{HOSTS}/{host_id}/monitoring")).json()["sync"][
            "status"
        ] == "pending"

        messages = await drive_socket(client, credential=credential)
        snapshot = only(text_frames(messages), "config_snapshot")[0]

        await drive_socket(
            client,
            credential=credential,
            frames=[
                json.dumps(
                    {
                        "v": PROTOCOL_VERSION,
                        "type": "config_ack",
                        "version": snapshot["version"],
                        "checksum": snapshot["checksum"],
                    }
                ),
                heartbeat(config_version=snapshot["version"], config_checksum=snapshot["checksum"]),
            ],
        )

        sync = (await client.get(f"{HOSTS}/{host_id}/monitoring")).json()["sync"]

    assert sync["status"] == "up_to_date"
    assert sync["reported_version"] == snapshot["version"]
    assert sync["acknowledged_at"] is not None


# ---------------------------------------------------------------------------
# The push
# ---------------------------------------------------------------------------


async def test_a_save_reaches_a_connected_agent_without_waiting_for_a_heartbeat(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """The push, driven by a real HTTP request against the same application
    while the socket is open.

    The socket's reader task and the HTTP handler are genuinely concurrent
    here, which is the thing worth testing: the handler must not write to the
    socket itself — it queues, and the reader sends.
    """
    async with api_factory() as client:
        host_id, credential, token = await enrolled_agent(client, seed_user, sign_in)

        app = client.app_state.app  # type: ignore[attr-defined]
        snapshot_seen = asyncio.Event()
        finished = asyncio.Event()
        sent: list[dict[str, Any]] = []

        connected = False

        async def receive() -> dict[str, Any]:
            nonlocal connected
            if not connected:
                connected = True
                return {"type": "websocket.connect"}
            # Then hold the socket open until the test has seen what it needs.
            await finished.wait()
            return {"type": "websocket.disconnect", "code": 1000}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)
            if message["type"] == "websocket.send":
                frame = json.loads(message["text"])
                if frame["type"] == "config_snapshot":
                    snapshot_seen.set()
                elif frame["type"] == "config_available":
                    finished.set()

        socket_task = asyncio.create_task(app(_scope(credential), receive, send))
        try:
            await asyncio.wait_for(snapshot_seen.wait(), timeout=5)

            response = await client.put(
                f"{HOSTS}/{host_id}/monitoring/modules/cpu",
                json={"enabled": True},
                headers=csrf(token),
            )
            assert response.status_code == 200, response.text

            await asyncio.wait_for(finished.wait(), timeout=5)
        finally:
            finished.set()
            await asyncio.wait_for(socket_task, timeout=5)

    available = only(text_frames(sent), "config_available")
    assert available
    assert available[0]["version"] == 2


async def test_the_push_carries_a_version_and_not_the_document(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """A notification, so a dropped one costs one heartbeat interval rather
    than a document — and so the queue holds pointers rather than megabytes.
    """
    from hcc.api.v1.agents import config_available_frame

    frame = json.loads(config_available_frame(7))
    assert frame == {"v": PROTOCOL_VERSION, "type": "config_available", "version": 7}
    assert "document" not in frame
    assert "checksum" not in frame
