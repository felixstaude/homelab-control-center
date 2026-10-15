"""Agent enrollment and the authenticated connection, against real PostgreSQL.

Milestone 1.8. These exercise the **whole controller side** of the live-host
flow: an administrator issues a credential through the real endpoint, an agent
spends it through the real endpoint, and the resulting identity opens a real
WebSocket and heartbeats.

``httpx`` has no WebSocket client, so the socket tests call the ASGI
application directly with a ``websocket`` scope. That is not a shortcut around
the stack — it *is* the interface Uvicorn uses, so the router-level credential
dependency, the exception handlers and the endpoint all run exactly as they do
in production. The Go agent is driven against a real server in
``scripts/e2e.sh``; this file is about the controller's behaviour, including
the refusals, which a browser-level test cannot reach.

What is asserted most carefully is the set of things that must **not** happen:

* the same enrollment credential succeeding twice
* an expired, revoked or unknown credential being distinguishable from each
  other in the response
* a revoked agent credential still opening a socket
* a superseded connection clearing its replacement's state
* a session cookie being accepted as an agent credential, or vice versa
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from agent_socket_harness import (
    ENROLL,
    HOSTS,
    accepted,
    bearer,
    csrf,
    drive_socket,
    enrolled_agent,
    register_and_issue,
    text_frames,
)
from sqlalchemy import text

from hcc.agents.protocol import (
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    CloseReason,
    MessageType,
)

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


def heartbeat_frame(**extra: object) -> str:
    return json.dumps({"v": PROTOCOL_VERSION, "type": "heartbeat", **extra})


# ---------------------------------------------------------------------------
# Enrollment
# ---------------------------------------------------------------------------


class TestEnrollment:
    async def test_a_valid_credential_yields_an_agent_identity(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            host_id, credential = await register_and_issue(client, seed_user, sign_in)

            response = await client.post(
                ENROLL,
                headers=bearer(credential),
                json={"agent_version": "0.1.0", "platform": "linux/amd64"},
            )

            assert response.status_code == 200, response.text
            body = response.json()
            assert body["host_id"] == host_id
            assert body["host_name"] == "homeserver"
            assert body["protocol_version"] == PROTOCOL_VERSION
            assert body["credential"] != credential
            assert body["superseded_previous"] is False

    async def test_enrollment_moves_the_host_to_enrolled(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            host_id, credential = await register_and_issue(client, seed_user, sign_in)

            await client.post(ENROLL, headers=bearer(credential), json={})

            host = (await client.get(f"{HOSTS}/{host_id}")).json()
            assert host["lifecycle"] == "enrolled"
            assert host["agent_version"] == "0.1.0" or host["agent_version"] is None
            assert host["credential_issued_at"] is not None
            # Enrolled is not connected. The agent has an identity and has not
            # opened a socket, and the interface must not conflate the two.
            assert host["connectivity"] == "offline"

    async def test_the_credential_cannot_be_spent_twice(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """Single use, which is the property the whole design turns on.

        A credential usable twice is a credential replayable by whoever read it
        over somebody's shoulder.
        """
        async with api_factory() as client:
            _, credential = await register_and_issue(client, seed_user, sign_in)

            first = await client.post(ENROLL, headers=bearer(credential), json={})
            second = await client.post(ENROLL, headers=bearer(credential), json={})

            assert first.status_code == 200
            assert second.status_code == 401
            assert second.json()["error"]["code"] == "agent_credential_rejected"

    async def test_concurrent_attempts_with_one_credential_produce_one_identity(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
    ) -> None:
        """The race a SELECT-then-UPDATE would lose.

        Two agents presenting the same credential at the same moment must not
        both enroll: that is the duplicate-identity failure, and it is decided
        by the conditional UPDATE rather than by application care.
        """
        async with api_factory() as client:
            _, credential = await register_and_issue(client, seed_user, sign_in)

            responses = await asyncio.gather(
                *(client.post(ENROLL, headers=bearer(credential), json={}) for _ in range(5))
            )

            statuses = sorted(response.status_code for response in responses)
            assert statuses == [200, 401, 401, 401, 401], statuses
            rows = await read_sql(
                client,
                "SELECT count(*) FROM agent_credentials WHERE revoked_at IS NULL",
            )
            assert rows[0][0] == 1

    @pytest.mark.parametrize(
        "sabotage",
        [
            "UPDATE host_enrollments SET revoked_at = now()",
            # Both timestamps move: the CHECK forbids an expiry before the
            # creation, so backdating only `expires_at` is refused by the
            # database — which is the constraint doing its job.
            (
                "UPDATE host_enrollments SET created_at = now() - interval '2 hours', "
                "expires_at = now() - interval '1 hour'"
            ),
            "UPDATE host_enrollments SET consumed_at = now()",
        ],
    )
    async def test_every_refusal_is_indistinguishable_to_the_agent(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        sabotage: str,
    ) -> None:
        """One status and one message for every cause.

        An endpoint that answered the difference would confirm to the holder of
        a captured credential that it was once real. The audit trail records
        which it was, where an administrator can act on it.
        """
        async with api_factory() as client:
            _, credential = await register_and_issue(client, seed_user, sign_in)
            async with client.app_state.database.session_factory() as db:  # type: ignore[attr-defined]
                await db.execute(text(sabotage))
                await db.commit()

            response = await client.post(ENROLL, headers=bearer(credential), json={})

            assert response.status_code == 401
            assert response.json()["error"]["code"] == "agent_credential_rejected"
            assert "expired" in response.json()["error"]["message"]

    async def test_an_unknown_credential_matches_the_known_bad_response_exactly(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """Including the body, not only the status.

        A different message for "never existed" would be the oracle this is
        shaped to avoid.
        """
        async with api_factory() as client:
            _, credential = await register_and_issue(client, seed_user, sign_in)
            await client.post(ENROLL, headers=bearer(credential), json={})

            used = await client.post(ENROLL, headers=bearer(credential), json={})
            unknown = await client.post(ENROLL, headers=bearer("z" * 43), json={})

            assert used.status_code == unknown.status_code
            assert used.json()["error"]["code"] == unknown.json()["error"]["code"]
            assert used.json()["error"]["message"] == unknown.json()["error"]["message"]

    async def test_a_refusal_for_a_known_host_is_audited_and_an_unknown_one_is_not(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        read_sql: ReadSql,
    ) -> None:
        """An unrecognised digest has no host to attribute it to.

        Writing a row per unrecognised digest would make an endpoint reachable
        without a session into a table-growth amplifier.
        """
        async with api_factory() as client:
            _, credential = await register_and_issue(client, seed_user, sign_in)
            await client.post(ENROLL, headers=bearer(credential), json={})

            await client.post(ENROLL, headers=bearer("z" * 43), json={})
            after_unknown = await read_sql(
                client,
                "SELECT count(*) FROM audit_events WHERE action = 'host.agent_auth_failed'",
            )

            await client.post(ENROLL, headers=bearer(credential), json={})
            after_known = await read_sql(
                client,
                "SELECT count(*) FROM audit_events WHERE action = 'host.agent_auth_failed'",
            )

            assert after_unknown[0][0] == 0
            assert after_known[0][0] == 1

    async def test_the_audit_record_names_the_reason_and_carries_no_digest(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        read_sql: ReadSql,
    ) -> None:
        async with api_factory() as client:
            host_id, credential = await register_and_issue(client, seed_user, sign_in)
            await client.post(ENROLL, headers=bearer(credential), json={})
            await client.post(ENROLL, headers=bearer(credential), json={})

            rows = await read_sql(
                client,
                "SELECT target_host_id::text, details::text FROM audit_events "
                "WHERE action = 'host.agent_auth_failed'",
            )

            recorded_host, details = rows[0]
            assert recorded_host == host_id
            assert '"reason": "already_used"' in details
            assert credential not in details
            assert "hash" not in details

    async def test_re_enrollment_revokes_the_previous_credential(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """The reinstalled-machine path, and the one that must not leave two.

        Two live credentials would mean revoking "the" credential silently
        leaves the other working.
        """
        async with api_factory() as client:
            host_id, first_credential = await register_and_issue(client, seed_user, sign_in)
            first = (await client.post(ENROLL, headers=bearer(first_credential), json={})).json()

            token = await sign_in(client, "admin")
            second_issued = (
                await client.post(
                    f"{HOSTS}/{host_id}/enrollment",
                    json={"allow_reenrollment": True},
                    headers=csrf(token),
                )
            ).json()
            second = (
                await client.post(ENROLL, headers=bearer(second_issued["credential"]), json={})
            ).json()

            assert second["superseded_previous"] is True
            assert second["credential"] != first["credential"]
            # The old agent credential must no longer open a socket.
            messages = await drive_socket(client, headers=bearer(first["credential"]))
            assert not accepted(messages)

    async def test_a_session_cookie_is_not_accepted_as_an_agent_credential(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """The two credential kinds never stand in for one another.

        A stolen browser cookie must not enroll a host, and this endpoint
        reads one header and nothing else.
        """
        async with api_factory() as client:
            await register_and_issue(client, seed_user, sign_in)

            response = await client.post(ENROLL, json={})

            assert response.status_code == 401
            assert response.json()["error"]["code"] == "not_authenticated"

    @pytest.mark.parametrize(
        "header",
        [
            {"Authorization": "Basic abc"},
            {"Authorization": "Bearer"},
            {"Authorization": "Bearer "},
            {"Authorization": "Bearer " + "x" * 400},
            {"Authorization": ""},
        ],
    )
    async def test_a_malformed_authorization_header_is_a_401(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        header: dict[str, str],
    ) -> None:
        async with api_factory() as client:
            await register_and_issue(client, seed_user, sign_in)

            response = await client.post(ENROLL, headers=header, json={})

            assert response.status_code == 401

    async def test_the_credential_is_never_in_the_query_string(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """There is no parameter that would carry it.

        A query parameter would put a long-lived secret in the access log of
        every proxy between the agent and the controller.
        """
        async with api_factory() as client:
            _, credential = await register_and_issue(client, seed_user, sign_in)

            response = await client.post(f"{ENROLL}?credential={credential}", json={})

            # Refused for want of the header, not accepted from the URL.
            assert response.status_code == 401

    async def test_a_host_id_in_the_body_is_rejected_not_honoured(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """An agent has nothing to claim to be.

        The host comes from the credential; a field naming one would be a
        field an agent could use to claim a different host.
        """
        async with api_factory() as client:
            _, credential = await register_and_issue(client, seed_user, sign_in)

            response = await client.post(
                ENROLL,
                headers=bearer(credential),
                json={"host_id": "00000000-0000-0000-0000-000000000000"},
            )

            assert response.status_code == 422


# ---------------------------------------------------------------------------
# The connection
# ---------------------------------------------------------------------------


class TestConnection:
    async def test_an_enrolled_agent_connects_and_the_host_reads_as_connected(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """Connected the moment the socket is open, not after the first beat.

        The handshake is itself evidence of liveness, so `last_heartbeat_at` is
        stamped at accept — without which a fresh connection would read as
        stale for thirty seconds and the interface would report a problem that
        does not exist.
        """
        async with api_factory() as client:
            host_id, credential = await enrolled_agent(client, seed_user, sign_in)

            messages = await drive_socket(
                client, headers=bearer(credential), frames=[heartbeat_frame()]
            )

            assert accepted(messages), messages
            acks = [f for f in text_frames(messages) if f["type"] == MessageType.HEARTBEAT_ACK]
            assert acks, text_frames(messages)
            assert acks[0]["interval_seconds"] > 0

            host = (await client.get(f"{HOSTS}/{host_id}")).json()
            # Disconnected cleanly by the end of the exchange, which is the
            # honest state: the socket is gone.
            assert host["connectivity"] == "offline"
            assert host["last_connected_at"] is not None
            assert host["last_heartbeat_at"] is not None
            assert host["last_disconnected_at"] is not None
            assert host["protocol_version"] == PROTOCOL_VERSION

    async def test_the_heartbeat_is_recorded_while_the_socket_is_open(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
    ) -> None:
        """Read from the row mid-connection, because the response cannot show it.

        By the time `drive_socket` returns the connection has closed, so
        "connected" has to be observed while it is still open.
        """
        async with api_factory() as client:
            host_id, credential = await enrolled_agent(client, seed_user, sign_in)
            observed: list[tuple[Any, ...]] = []

            async def watch() -> None:
                await asyncio.sleep(0.15)
                rows = await read_sql(
                    client,
                    "SELECT current_connection_id IS NOT NULL, connection_epoch "
                    f"FROM hosts WHERE id = '{host_id}'",
                )
                observed.append(tuple(rows[0]))

            await asyncio.gather(
                drive_socket(
                    client,
                    headers=bearer(credential),
                    frames=[heartbeat_frame()],
                    hold=0.4,
                ),
                watch(),
            )

            assert observed == [(True, 1)]

    async def test_a_revoked_credential_cannot_open_a_socket(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """Mission step 18, asserted at the socket rather than at the API."""
        async with api_factory() as client:
            host_id, credential = await enrolled_agent(client, seed_user, sign_in)
            token = await sign_in(client, "admin")
            revoked = await client.post(f"{HOSTS}/{host_id}/credential/revoke", headers=csrf(token))
            assert revoked.status_code == 200

            messages = await drive_socket(client, headers=bearer(credential))

            assert not accepted(messages), messages
            start = next(m for m in messages if m["type"] == "websocket.http.response.start")
            assert start["status"] == 401

    async def test_revoking_clears_the_recorded_connection(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """Otherwise the interface shows an attached agent that is locked out."""
        async with api_factory() as client:
            host_id, credential = await enrolled_agent(client, seed_user, sign_in)
            await drive_socket(client, headers=bearer(credential), frames=[heartbeat_frame()])
            token = await sign_in(client, "admin")

            body = (
                await client.post(f"{HOSTS}/{host_id}/credential/revoke", headers=csrf(token))
            ).json()

            assert body["lifecycle"] == "revoked"
            assert body["connectivity"] == "offline"

    async def test_a_disabled_host_is_told_so_rather_than_refused_opaquely(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """The credential is valid, so there is nothing left to withhold.

        The agent needs to tell "my credential is wrong" apart from "the
        operator switched this host off", because only one of those is
        something it should keep retrying quietly.
        """
        async with api_factory() as client:
            host_id, credential = await enrolled_agent(client, seed_user, sign_in)
            token = await sign_in(client, "admin")
            await client.post(f"{HOSTS}/{host_id}/disable", headers=csrf(token))

            messages = await drive_socket(client, headers=bearer(credential))

            assert accepted(messages), messages
            closes = [f for f in text_frames(messages) if f["type"] == MessageType.CLOSE]
            assert closes[0]["reason"] == CloseReason.HOST_DISABLED.value

    async def test_an_anonymous_handshake_is_refused_before_accept(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            await enrolled_agent(client, seed_user, sign_in)

            messages = await drive_socket(client)

            assert not accepted(messages), messages

    async def test_a_browser_session_does_not_open_the_agent_socket(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """An administrator's cookie is not an agent credential.

        `drive_socket` forwards the client's cookies, so the session really is
        presented — and must still be refused.
        """
        async with api_factory() as client:
            await enrolled_agent(client, seed_user, sign_in)
            await sign_in(client, "admin")

            messages = await drive_socket(client, headers={"Origin": "http://testserver"})

            assert not accepted(messages), messages

    async def test_an_unsupported_protocol_version_is_refused_with_the_supported_set(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """A mismatch must be a clear message, not frames parsed wrongly.

        The controller cannot upgrade the fleet, so "they must match" is not a
        rule it can enforce — it can only state what it speaks.
        """
        async with api_factory() as client:
            _, credential = await enrolled_agent(client, seed_user, sign_in)

            messages = await drive_socket(
                client,
                headers={**bearer(credential), "X-HCC-Protocol-Version": "99"},
            )

            assert not accepted(messages), messages
            start = next(m for m in messages if m["type"] == "websocket.http.response.start")
            assert start["status"] == 400

    async def test_a_non_numeric_protocol_version_is_refused(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            _, credential = await enrolled_agent(client, seed_user, sign_in)

            messages = await drive_socket(
                client,
                headers={**bearer(credential), "X-HCC-Protocol-Version": "' OR 1=1"},
            )

            assert not accepted(messages), messages


# ---------------------------------------------------------------------------
# Protocol handling on an authenticated socket
# ---------------------------------------------------------------------------


class TestFrameHandling:
    @pytest.mark.parametrize(
        ("frame", "expected"),
        [
            ("not json at all", CloseReason.PROTOCOL_ERROR),
            ("[]", CloseReason.PROTOCOL_ERROR),
            ('"a string"', CloseReason.PROTOCOL_ERROR),
            ('{"type": "heartbeat"}', CloseReason.UNSUPPORTED_PROTOCOL),
            ('{"v": 99, "type": "heartbeat"}', CloseReason.UNSUPPORTED_PROTOCOL),
            ('{"v": true, "type": "heartbeat"}', CloseReason.UNSUPPORTED_PROTOCOL),
            ('{"v": 1, "type": "exec"}', CloseReason.PROTOCOL_ERROR),
            ('{"v": 1, "type": "heartbeat", "surprise": 1}', CloseReason.PROTOCOL_ERROR),
            ('{"v": 1, "type": "heartbeat", "uptime_seconds": -5}', CloseReason.PROTOCOL_ERROR),
        ],
    )
    async def test_a_bad_frame_closes_the_connection_with_a_reason(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        frame: str,
        expected: CloseReason,
    ) -> None:
        async with api_factory() as client:
            _, credential = await enrolled_agent(client, seed_user, sign_in)

            messages = await drive_socket(client, headers=bearer(credential), frames=[frame])

            closes = [f for f in text_frames(messages) if f["type"] == MessageType.CLOSE]
            assert closes, text_frames(messages)
            assert closes[0]["reason"] == expected.value

    async def test_an_oversized_frame_is_refused_without_being_parsed(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """One agent streaming an enormous frame must not be a memory attack."""
        async with api_factory() as client:
            _, credential = await enrolled_agent(client, seed_user, sign_in)
            oversized = json.dumps(
                {
                    "v": PROTOCOL_VERSION,
                    "type": "heartbeat",
                    "platform": "x" * (MAX_MESSAGE_BYTES + 100),
                }
            )

            messages = await drive_socket(client, headers=bearer(credential), frames=[oversized])

            closes = [f for f in text_frames(messages) if f["type"] == MessageType.CLOSE]
            assert closes[0]["reason"] == CloseReason.PROTOCOL_ERROR.value

    async def test_a_protocol_error_does_not_echo_the_agents_own_string_back(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """Otherwise the controller is a reflector for whatever an agent sends."""
        async with api_factory() as client:
            _, credential = await enrolled_agent(client, seed_user, sign_in)
            marker = "REFLECTED-MARKER-9f2c"

            messages = await drive_socket(
                client,
                headers=bearer(credential),
                frames=[json.dumps({"v": 1, "type": marker})],
            )

            assert marker not in json.dumps(text_frames(messages))

    async def test_a_frame_flood_is_rate_limited_rather_than_served(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            _, credential = await enrolled_agent(client, seed_user, sign_in)

            messages = await drive_socket(
                client,
                headers=bearer(credential),
                frames=[heartbeat_frame() for _ in range(60)],
            )

            closes = [f for f in text_frames(messages) if f["type"] == MessageType.CLOSE]
            assert closes, "a 60-frame burst was served in full"
            assert closes[0]["reason"] == CloseReason.RATE_LIMITED.value

    async def test_the_agents_self_report_is_recorded_but_bounded(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
    ) -> None:
        """Truncated, not refused.

        These fields are cosmetic; refusing a connection because a host
        reports an over-long platform would turn a label into an availability
        problem.
        """
        async with api_factory() as client:
            host_id, credential = await enrolled_agent(client, seed_user, sign_in)

            await drive_socket(
                client,
                headers=bearer(credential),
                frames=[heartbeat_frame(agent_version="9.9.9", platform="l" * 64)],
            )

            rows = await read_sql(
                client,
                f"SELECT agent_version, length(platform) FROM hosts WHERE id = '{host_id}'",
            )
            assert rows[0][0] == "9.9.9"
            assert rows[0][1] == 64

    async def test_reconnecting_does_not_erase_what_the_host_already_reported(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
    ) -> None:
        """A reconnection says nothing about the host; it must not say "nothing".

        The connect handler has a credential and a protocol version and no
        self-description — an agent reports its version in a *heartbeat*. The
        accept path wrote those columns unconditionally anyway, so every
        reconnection blanked them until the next heartbeat arrived.

        Not cosmetic. ``agent_version`` is what the update planner compares
        against the manifest, so in that window the host's plan is
        ``unknown_version`` and the fleet's version distribution reads
        "unknown" — for the whole fleet at once after a controller restart,
        which is exactly when somebody is looking at it. ``update-e2e.sh``
        §16 caught it forty milliseconds after a reconnect.

        Two sockets rather than one: on a single connection the old code is
        correct, which is why nothing had noticed.
        """
        async with api_factory() as client:
            host_id, credential = await enrolled_agent(client, seed_user, sign_in)

            await drive_socket(
                client,
                headers=bearer(credential),
                frames=[heartbeat_frame(agent_version="0.1.1-test", platform="linux/amd64")],
            )
            before = await read_sql(
                client,
                f"SELECT agent_version, platform FROM hosts WHERE id = '{host_id}'",
            )
            assert before[0][0] == "0.1.1-test"

            # A second connection that says nothing about itself, which is
            # what every reconnection does.
            await drive_socket(client, headers=bearer(credential), frames=[])

            after = await read_sql(
                client,
                f"SELECT agent_version, platform FROM hosts WHERE id = '{host_id}'",
            )
            assert after[0][0] == "0.1.1-test"
            assert after[0][1] == "linux/amd64"


# ---------------------------------------------------------------------------
# Supersession and reconnection
# ---------------------------------------------------------------------------


class TestSupersession:
    async def test_a_second_connection_supersedes_the_first(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """And the first is told why, so it does not back off needlessly.

        A supersession is usually the agent's own reconnection, so treating it
        as an error would make a recovering agent wait before doing the thing
        it has already succeeded at.
        """
        async with api_factory() as client:
            _, credential = await enrolled_agent(client, seed_user, sign_in)

            async def second() -> list[dict[str, Any]]:
                await asyncio.sleep(0.2)
                return await drive_socket(
                    client, headers=bearer(credential), frames=[heartbeat_frame()]
                )

            first_messages, second_messages = await asyncio.gather(
                drive_socket(
                    client,
                    headers=bearer(credential),
                    frames=[heartbeat_frame()],
                    hold=1.0,
                ),
                second(),
            )

            assert accepted(second_messages)
            closes = [f for f in text_frames(first_messages) if f["type"] == MessageType.CLOSE]
            assert closes, text_frames(first_messages)
            assert closes[0]["reason"] == CloseReason.SUPERSEDED.value

    async def test_the_superseded_connection_does_not_clear_the_newer_state(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
    ) -> None:
        """The epoch guard, asserted at the row.

        Without it, the old connection's disconnect clears the new one's
        `current_connection_id` — so a host that *is* connected reads as
        offline, and a flapping link makes the dashboard oscillate.
        """
        async with api_factory() as client:
            host_id, credential = await enrolled_agent(client, seed_user, sign_in)
            observed: list[tuple[Any, ...]] = []

            async def second() -> list[dict[str, Any]]:
                await asyncio.sleep(0.2)
                return await drive_socket(
                    client,
                    headers=bearer(credential),
                    frames=[heartbeat_frame()],
                    hold=0.8,
                )

            async def watch() -> None:
                # After the first has been superseded and closed, while the
                # second is still open.
                await asyncio.sleep(0.6)
                rows = await read_sql(
                    client,
                    "SELECT current_connection_id IS NOT NULL, connection_epoch "
                    f"FROM hosts WHERE id = '{host_id}'",
                )
                observed.append(tuple(rows[0]))

            await asyncio.gather(
                drive_socket(
                    client,
                    headers=bearer(credential),
                    frames=[heartbeat_frame()],
                    hold=0.4,
                ),
                second(),
                watch(),
            )

            assert observed == [(True, 2)], observed

    async def test_reconnecting_keeps_the_same_host_identity(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """Mission step 16. The credential is bound to the id, not the socket."""
        async with api_factory() as client:
            host_id, credential = await enrolled_agent(client, seed_user, sign_in)

            for _ in range(3):
                messages = await drive_socket(
                    client, headers=bearer(credential), frames=[heartbeat_frame()]
                )
                assert accepted(messages)

            host = (await client.get(f"{HOSTS}/{host_id}")).json()
            assert host["host_id"] == host_id
            assert host["lifecycle"] == "enrolled"

    async def test_the_epoch_increases_once_per_accepted_connection(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
    ) -> None:
        async with api_factory() as client:
            host_id, credential = await enrolled_agent(client, seed_user, sign_in)

            for _ in range(3):
                await drive_socket(client, headers=bearer(credential), frames=[heartbeat_frame()])

            rows = await read_sql(
                client, f"SELECT connection_epoch FROM hosts WHERE id = '{host_id}'"
            )
            assert rows[0][0] == 3


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------


class TestStaleness:
    async def test_a_connection_with_a_frozen_heartbeat_reads_as_stale(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """The controller-crash case, which is the reason connectivity is derived.

        A stored `is_connected` boolean would still read true here, because
        the code that clears it never ran — and the dashboard would report
        healthy connectivity for a host connected to nothing.
        """
        async with api_factory() as client:
            host_id, credential = await enrolled_agent(client, seed_user, sign_in)
            await drive_socket(client, headers=bearer(credential), frames=[heartbeat_frame()])

            # Simulate the crash: a connection is recorded and the heartbeat
            # is old. Written directly because a crashed controller is exactly
            # the state no endpoint can produce.
            async with client.app_state.database.session_factory() as db:  # type: ignore[attr-defined]
                await db.execute(
                    text(
                        "UPDATE hosts SET current_connection_id = gen_random_uuid(), "
                        "last_heartbeat_at = :stale WHERE id = :id"
                    ),
                    {"id": host_id, "stale": datetime.now(UTC) - timedelta(hours=1)},
                )
                await db.commit()

            host = (await client.get(f"{HOSTS}/{host_id}")).json()

            assert host["connectivity"] == "stale"

    async def test_the_configured_grace_decides_when_a_host_is_stale(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """The setting has to reach the derivation, not just the module default."""
        async with api_factory(agent_heartbeat_grace_seconds=10) as client:
            host_id, credential = await enrolled_agent(client, seed_user, sign_in)
            await drive_socket(client, headers=bearer(credential), frames=[heartbeat_frame()])

            async with client.app_state.database.session_factory() as db:  # type: ignore[attr-defined]
                await db.execute(
                    text(
                        "UPDATE hosts SET current_connection_id = gen_random_uuid(), "
                        "last_heartbeat_at = :stale WHERE id = :id"
                    ),
                    {"id": host_id, "stale": datetime.now(UTC) - timedelta(seconds=30)},
                )
                await db.commit()

            host = (await client.get(f"{HOSTS}/{host_id}")).json()

            assert host["connectivity"] == "stale"

    async def test_a_clean_disconnect_keeps_the_last_heartbeat(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """It is the record of when the host was last heard from.

        Clearing it would discard the one fact an operator needs in order to
        ask how long a host has been gone.
        """
        async with api_factory() as client:
            host_id, credential = await enrolled_agent(client, seed_user, sign_in)

            await drive_socket(client, headers=bearer(credential), frames=[heartbeat_frame()])

            host = (await client.get(f"{HOSTS}/{host_id}")).json()
            assert host["connectivity"] == "offline"
            assert host["last_heartbeat_at"] is not None
