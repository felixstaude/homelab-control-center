"""The update protocol on a real authenticated socket.

Driven through the ASGI application with a ``websocket`` scope, the same way
``test_api_agents_integration.py`` does — not a shortcut around the endpoint
but the endpoint itself, with the real credential dependency, the real
negotiation and the real frame parser.

## The test this file exists for

`TestAVersionOneAgentKeepsWorking`. Protocol 2 added the first
controller→agent frames since the protocol existed, and
``protocol.ParseServerFrame`` in the agent **errors** on an unrecognised type
rather than ignoring it. So a controller that sent an `update_instruction` to
the already-installed fleet would make every agent report a protocol error and
disconnect — turning an update feature into a fleet-wide outage.

The gate is `UPDATE_PROTOCOL_VERSION`, and these tests are what hold it in
place. They are worth more than the ones covering the happy path, because the
happy path fails loudly and this would fail on somebody else's machines.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from agent_socket_harness import (
    CONNECT,
    accepted,
    bearer,
    drive_socket,
    enroll_another_host,
    enrolled_agent,
    text_frames,
)
from sqlalchemy import select

from hcc.agents.protocol import UPDATE_PROTOCOL_VERSION
from hcc.models.host import Host
from hcc.models.updates import UpdateEvent, UpdateJob, UpdateState, UpdateTrigger
from hcc.models.user import UserRole
from hcc.security.audit import AuditContext

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from contextlib import AbstractAsyncContextManager

    from httpx import AsyncClient

    from hcc.models.user import User

    # Declared locally rather than imported from conftest, matching
    # test_api_agents_integration.py: `tests/` has no `__init__.py`, so a
    # module imported as `tests.conftest` and collected as `conftest` is one
    # source file under two names, which mypy refuses.
    ApiFactory = Callable[..., AbstractAsyncContextManager[AsyncClient]]
    SeedUser = Callable[..., Awaitable[User]]
    SignIn = Callable[..., Awaitable[str]]

pytestmark = pytest.mark.integration

DIGEST = "b" * 64
AMD64 = "linux/amd64"


def heartbeat(version: int, **extra: object) -> str:
    return json.dumps({"v": version, "type": "heartbeat", **extra})


def frame(version: int, kind: str, **extra: object) -> str:
    return json.dumps({"v": version, "type": kind, **extra})


def manifest_document(*versions: str) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "recommended": versions[-1],
        "minimum_supported": versions[0],
        "channels": {"stable": versions[-1]},
        "releases": {
            version: {
                "channel": "stable",
                "protocol_versions": [1, 2],
                "platforms": {
                    AMD64: {
                        "archive": f"hcc-agent_{version}_linux_amd64.tar.gz",
                        "url": f"https://example.com/hcc-agent_{version}.tar.gz",
                        "sha256": DIGEST,
                        "size_bytes": 3_000_000,
                        "tier": "supported",
                    }
                },
            }
            for version in versions
        },
    }


async def queue_an_update(client: Any, host_id: str, manifest: Any) -> UpdateJob:
    """Create a queued job directly through the service.

    Through the service rather than by inserting a row, so the job is in the
    state the real path produces — including its events.
    """
    updates = client.app_state.update_service
    async with client.app_state.database.session_factory() as db:
        job = await updates.start_update(
            db,
            uuid.UUID(host_id),
            manifest=manifest,
            trigger=UpdateTrigger.MANUAL,
            context=AuditContext(actor_user_id=None, request_id="test"),
        )
        await db.commit()
        return job  # type: ignore[no-any-return]


async def set_host_version(client: Any, host_id: str, version: str) -> None:
    """Make the host look like it is running *version*.

    The enrollment endpoint already records what the agent reported, so this
    is only needed where a test wants a *different* version from the one
    `enrolled_agent` sends — which is every test that needs an update to be
    available.
    """
    async with client.app_state.database.session_factory() as db:
        host = (await db.execute(select(Host).where(Host.id == uuid.UUID(host_id)))).scalar_one()
        host.agent_version = version
        host.platform = AMD64
        await db.commit()


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


class TestAVersionOneAgentKeepsWorking:
    async def test_it_still_handshakes(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """Dropping 1 from the supported set would strand every installed agent.

        The controller cannot upgrade the fleet, so support for the old version
        is the only thing that makes the new one deployable.
        """
        async with api_factory() as client:
            _, credential = await enrolled_agent(client, seed_user, sign_in)

            messages = await drive_socket(
                client,
                headers={**bearer(credential), "X-HCC-Protocol-Version": "1"},
                frames=[heartbeat(1)],
            )

            assert accepted(messages), messages
            frames = text_frames(messages)
            assert any(f["type"] == "heartbeat_ack" for f in frames)

    async def test_it_is_never_told_about_an_update(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, tmp_path: Any
    ) -> None:
        """The gate, asserted with an update genuinely waiting.

        A version-1 agent is **never pointed at an instruction**, even when one
        is queued for its host. That is the property: it is not told about
        something it could not then be sent, because the instruction frame
        that would follow is one its parser refuses.

        The field is present and null rather than absent, which is how
        pydantic serialises an unset optional and is exactly what
        `config_version` beside it already does. Harmless either way — Go
        unmarshals a null string into "" — so the assertion is on the *value*,
        which is the thing that decides whether the agent asks for an
        instruction.
        """
        path = tmp_path / "releases.json"
        path.write_text(json.dumps(manifest_document("0.1.0", "0.1.1")))

        async with api_factory(agent_release_manifest=path) as client:
            host_id, credential = await enrolled_agent(client, seed_user, sign_in)
            await set_host_version(client, host_id, "0.1.0")
            from hcc.agents.releases import load_release_manifest

            job = await queue_an_update(client, host_id, load_release_manifest(path))
            assert job.state == UpdateState.QUEUED

            messages = await drive_socket(
                client,
                headers={**bearer(credential), "X-HCC-Protocol-Version": "1"},
                frames=[heartbeat(1)],
            )

            acks = [f for f in text_frames(messages) if f["type"] == "heartbeat_ack"]
            assert acks
            assert all(ack.get("update_instruction_id") is None for ack in acks)

    async def test_an_update_frame_from_a_version_one_agent_is_a_protocol_error(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        # An agent that handshook at 1 and then sent an update frame is either
        # confused or probing. Answering it would mean sending an
        # `update_instruction` to a parser that refuses one.
        async with api_factory() as client:
            _, credential = await enrolled_agent(client, seed_user, sign_in)

            messages = await drive_socket(
                client,
                headers={**bearer(credential), "X-HCC-Protocol-Version": "1"},
                frames=[frame(1, "update_request")],
            )

            frames = text_frames(messages)
            closes = [f for f in frames if f["type"] == "close"]
            assert closes, frames
            assert closes[0]["reason"] == "protocol_error"
            assert str(UPDATE_PROTOCOL_VERSION) in closes[0]["detail"]

    async def test_a_version_one_agent_still_receives_configuration(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        # The property that matters operationally: an old agent is not merely
        # tolerated, it keeps doing its job.
        async with api_factory() as client:
            _, credential = await enrolled_agent(client, seed_user, sign_in)

            messages = await drive_socket(
                client,
                headers={**bearer(credential), "X-HCC-Protocol-Version": "1"},
                frames=[heartbeat(1), frame(1, "config_request")],
            )

            frames = text_frames(messages)
            assert any(f["type"] == "config_snapshot" for f in frames)


# ---------------------------------------------------------------------------
# A version-2 agent
# ---------------------------------------------------------------------------


class TestAVersionTwoAgentIsOfferedTheUpdate:
    async def test_the_acknowledgement_names_the_waiting_instruction(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, tmp_path: Any
    ) -> None:
        path = tmp_path / "releases.json"
        path.write_text(json.dumps(manifest_document("0.1.0", "0.1.1")))

        async with api_factory(agent_release_manifest=path) as client:
            host_id, credential = await enrolled_agent(client, seed_user, sign_in)
            await set_host_version(client, host_id, "0.1.0")
            from hcc.agents.releases import load_release_manifest

            job = await queue_an_update(client, host_id, load_release_manifest(path))

            messages = await drive_socket(
                client,
                headers={**bearer(credential), "X-HCC-Protocol-Version": "2"},
                frames=[heartbeat(2)],
            )

            acks = [f for f in text_frames(messages) if f["type"] == "heartbeat_ack"]
            assert acks
            assert acks[0]["update_instruction_id"] == str(job.instruction_id)

    async def test_no_instruction_means_no_identifier(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        # A host with nothing waiting reports no identifier, so an agent that
        # branches on the value does not ask for an instruction that does not
        # exist.
        async with api_factory() as client:
            _, credential = await enrolled_agent(client, seed_user, sign_in)

            messages = await drive_socket(
                client,
                headers={**bearer(credential), "X-HCC-Protocol-Version": "2"},
                frames=[heartbeat(2)],
            )

            acks = [f for f in text_frames(messages) if f["type"] == "heartbeat_ack"]
            assert acks
            assert acks[0].get("update_instruction_id") is None

    async def test_the_instruction_carries_the_signed_metadata_verbatim(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, tmp_path: Any
    ) -> None:
        """The trust model, asserted on the wire.

        The frame carries the manifest's **exact bytes**, not a
        re-serialisation of the parsed model — because the signature covers
        those bytes and two JSON libraries do not agree on key order. A
        re-serialisation here would break every signature the agent checks.
        """
        from hcc.updates.signing import generate_keypair, sign_manifest

        document = json.dumps(manifest_document("0.1.0", "0.1.1"), indent=2)
        path = tmp_path / "releases.json"
        path.write_text(document)
        private, _ = generate_keypair()
        (tmp_path / "releases.json.sig").write_text(
            json.dumps(sign_manifest(document.encode(), private))
        )

        async with api_factory(agent_release_manifest=path) as client:
            host_id, credential = await enrolled_agent(client, seed_user, sign_in)
            await set_host_version(client, host_id, "0.1.0")
            from hcc.agents.releases import load_release_manifest

            await queue_an_update(client, host_id, load_release_manifest(path))

            messages = await drive_socket(
                client,
                headers={**bearer(credential), "X-HCC-Protocol-Version": "2"},
                frames=[heartbeat(2), frame(2, "update_request")],
            )

            instructions = [f for f in text_frames(messages) if f["type"] == "update_instruction"]
            assert instructions, text_frames(messages)
            instruction = instructions[0]
            # Byte-for-byte, including the indentation the file happens to
            # have. This is the assertion that would fail if somebody
            # "tidied" this into a model dump.
            assert instruction["metadata"] == document
            assert json.loads(instruction["signature"])["algorithm"] == "ed25519"
            assert instruction["to_version"] == "0.1.1"
            assert instruction["platform"] == AMD64

    async def test_the_instruction_carries_an_expiry(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, tmp_path: Any
    ) -> None:
        # The mechanism that keeps an offline host from installing last week's
        # decision. Enforced by the agent, because the controller cannot reach
        # a disconnected host to withdraw anything.
        path = tmp_path / "releases.json"
        path.write_text(json.dumps(manifest_document("0.1.0", "0.1.1")))

        async with api_factory(agent_release_manifest=path) as client:
            host_id, credential = await enrolled_agent(client, seed_user, sign_in)
            await set_host_version(client, host_id, "0.1.0")
            from hcc.agents.releases import load_release_manifest

            await queue_an_update(client, host_id, load_release_manifest(path))

            messages = await drive_socket(
                client,
                headers={**bearer(credential), "X-HCC-Protocol-Version": "2"},
                frames=[frame(2, "update_request")],
            )

            instruction = next(
                f for f in text_frames(messages) if f["type"] == "update_instruction"
            )
            expires = datetime.fromisoformat(instruction["expires_at"])
            assert expires > datetime.now(UTC)
            assert expires < datetime.now(UTC) + timedelta(days=1)

    async def test_a_request_with_nothing_open_is_answered_with_silence(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        # Between the acknowledgement that mentioned an instruction and the
        # request that arrived, an administrator may have cancelled it.
        # Silence is the right answer; a close frame would turn an ordinary
        # race into a reconnect.
        async with api_factory() as client:
            _, credential = await enrolled_agent(client, seed_user, sign_in)

            messages = await drive_socket(
                client,
                headers={**bearer(credential), "X-HCC-Protocol-Version": "2"},
                frames=[frame(2, "update_request")],
            )

            frames = text_frames(messages)
            assert not [f for f in frames if f["type"] == "update_instruction"]
            assert not [f for f in frames if f["type"] == "close"]


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


class TestAgentReportsOverTheSocket:
    async def test_progress_moves_the_job_and_records_an_event(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, tmp_path: Any
    ) -> None:
        path = tmp_path / "releases.json"
        path.write_text(json.dumps(manifest_document("0.1.0", "0.1.1")))

        async with api_factory(agent_release_manifest=path) as client:
            host_id, credential = await enrolled_agent(client, seed_user, sign_in)
            await set_host_version(client, host_id, "0.1.0")
            from hcc.agents.releases import load_release_manifest

            job = await queue_an_update(client, host_id, load_release_manifest(path))

            await drive_socket(
                client,
                headers={**bearer(credential), "X-HCC-Protocol-Version": "2"},
                frames=[
                    frame(
                        2,
                        "update_progress",
                        instruction_id=str(job.instruction_id),
                        state="downloading",
                        detail="3 MB",
                    )
                ],
            )

            async with client.app_state.database.session_factory() as db:  # type: ignore[attr-defined]
                stored = (
                    await db.execute(select(UpdateJob).where(UpdateJob.id == job.id))
                ).scalar_one()
                events = (
                    (
                        await db.execute(
                            select(UpdateEvent)
                            .where(UpdateEvent.job_id == job.id)
                            .order_by(UpdateEvent.id)
                        )
                    )
                    .scalars()
                    .all()
                )

            assert stored.state == UpdateState.DOWNLOADING
            assert events[-1].state == UpdateState.DOWNLOADING
            assert events[-1].source == "agent"
            assert events[-1].detail == "3 MB"

    async def test_a_result_finishes_the_job(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, tmp_path: Any
    ) -> None:
        path = tmp_path / "releases.json"
        path.write_text(json.dumps(manifest_document("0.1.0", "0.1.1")))

        async with api_factory(agent_release_manifest=path) as client:
            host_id, credential = await enrolled_agent(client, seed_user, sign_in)
            await set_host_version(client, host_id, "0.1.0")
            from hcc.agents.releases import load_release_manifest

            job = await queue_an_update(client, host_id, load_release_manifest(path))

            await drive_socket(
                client,
                headers={**bearer(credential), "X-HCC-Protocol-Version": "2"},
                frames=[
                    frame(
                        2,
                        "update_progress",
                        instruction_id=str(job.instruction_id),
                        state="downloading",
                    ),
                    frame(
                        2,
                        "update_progress",
                        instruction_id=str(job.instruction_id),
                        state="verifying",
                    ),
                    frame(
                        2, "update_progress", instruction_id=str(job.instruction_id), state="staged"
                    ),
                    frame(
                        2,
                        "update_progress",
                        instruction_id=str(job.instruction_id),
                        state="installing",
                    ),
                    frame(
                        2,
                        "update_progress",
                        instruction_id=str(job.instruction_id),
                        state="restarting",
                    ),
                    frame(
                        2,
                        "update_progress",
                        instruction_id=str(job.instruction_id),
                        state="verifying_health",
                    ),
                    frame(
                        2,
                        "update_result",
                        instruction_id=str(job.instruction_id),
                        state="succeeded",
                        resulting_version="0.1.1",
                    ),
                ],
            )

            async with client.app_state.database.session_factory() as db:  # type: ignore[attr-defined]
                stored = (
                    await db.execute(select(UpdateJob).where(UpdateJob.id == job.id))
                ).scalar_one()

            assert stored.state == UpdateState.SUCCEEDED
            assert stored.resulting_version == "0.1.1"
            assert stored.finished_at is not None

    async def test_a_report_the_machine_forbids_does_not_close_the_connection(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, tmp_path: Any
    ) -> None:
        """A bookkeeping disagreement must not become an outage on that host.

        An agent reporting `succeeded` straight from `queued` is refused — the
        transition table says so — and the connection stays up, because
        telemetry and configuration are still working and disconnecting would
        stop those too.
        """
        path = tmp_path / "releases.json"
        path.write_text(json.dumps(manifest_document("0.1.0", "0.1.1")))

        async with api_factory(agent_release_manifest=path) as client:
            host_id, credential = await enrolled_agent(client, seed_user, sign_in)
            await set_host_version(client, host_id, "0.1.0")
            from hcc.agents.releases import load_release_manifest

            job = await queue_an_update(client, host_id, load_release_manifest(path))

            messages = await drive_socket(
                client,
                headers={**bearer(credential), "X-HCC-Protocol-Version": "2"},
                frames=[
                    frame(
                        2,
                        "update_result",
                        instruction_id=str(job.instruction_id),
                        state="succeeded",
                        resulting_version="0.1.1",
                    ),
                    heartbeat(2),
                ],
            )

            frames = text_frames(messages)
            assert not [f for f in frames if f["type"] == "close"]
            assert [f for f in frames if f["type"] == "heartbeat_ack"]

            async with client.app_state.database.session_factory() as db:  # type: ignore[attr-defined]
                stored = (
                    await db.execute(select(UpdateJob).where(UpdateJob.id == job.id))
                ).scalar_one()
            assert stored.state == UpdateState.QUEUED

    async def test_an_agent_cannot_report_for_another_hosts_instruction(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, tmp_path: Any
    ) -> None:
        """Impersonation over the real socket.

        The service matches the instruction against the authenticated host, so
        the attacker's frame finds nothing. Logged and ignored rather than
        answered, so a probe learns nothing about whether the identifier
        exists.
        """
        path = tmp_path / "releases.json"
        path.write_text(json.dumps(manifest_document("0.1.0", "0.1.1")))

        async with api_factory(agent_release_manifest=path) as client:
            victim_id, _ = await enrolled_agent(client, seed_user, sign_in)
            await set_host_version(client, victim_id, "0.1.0")
            from hcc.agents.releases import load_release_manifest

            job = await queue_an_update(client, victim_id, load_release_manifest(path))

            # A second enrolled host, with its own credential.
            token = await sign_in(client, "admin")
            attacker_id, attacker_credential = await enroll_another_host(client, token, "attacker")
            await set_host_version(client, attacker_id, "0.1.0")

            await drive_socket(
                client,
                headers={
                    **bearer(attacker_credential),
                    "X-HCC-Protocol-Version": "2",
                },
                frames=[
                    frame(
                        2,
                        "update_progress",
                        instruction_id=str(job.instruction_id),
                        state="downloading",
                    )
                ],
            )

            async with client.app_state.database.session_factory() as db:  # type: ignore[attr-defined]
                stored = (
                    await db.execute(select(UpdateJob).where(UpdateJob.id == job.id))
                ).scalar_one()

            assert stored.state == UpdateState.QUEUED

    async def test_a_malformed_instruction_id_is_refused_by_the_parser(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        # Validated in the frame model, so it never reaches a UUID constructor
        # in the service.
        async with api_factory() as client:
            _, credential = await enrolled_agent(client, seed_user, sign_in)

            messages = await drive_socket(
                client,
                headers={**bearer(credential), "X-HCC-Protocol-Version": "2"},
                frames=[
                    frame(2, "update_progress", instruction_id="not-a-uuid", state="downloading")
                ],
            )

            closes = [f for f in text_frames(messages) if f["type"] == "close"]
            assert closes
            assert closes[0]["reason"] == "protocol_error"


class TestTheHandshakeStillReportsTheVersion:
    async def test_the_enrollment_response_names_protocol_two(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        # The agent reads this to decide whether it may use update frames at
        # all, so the value being current is load-bearing rather than cosmetic.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host = (
                await client.post(
                    "/api/v1/hosts",
                    json={"name": "versioned"},
                    headers={"X-CSRF-Token": token},
                )
            ).json()
            issued = (
                await client.post(
                    f"/api/v1/hosts/{host['host_id']}/enrollment",
                    headers={"X-CSRF-Token": token},
                )
            ).json()
            enrolled = (
                await client.post(
                    "/api/v1/agents/enroll",
                    headers={"Authorization": f"Bearer {issued['credential']}"},
                    json={"agent_version": "0.1.0", "platform": AMD64},
                )
            ).json()

            assert enrolled["protocol_version"] == UPDATE_PROTOCOL_VERSION

    async def test_the_connect_path_is_unchanged(self) -> None:
        # A protocol bump must not move the endpoint: an agent built against
        # version 1 dials this exact path, and changing it would strand the
        # fleet more thoroughly than a frame type ever could.
        assert CONNECT == "/api/v1/agents/connect"
