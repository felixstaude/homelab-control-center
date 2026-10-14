"""Driving the agent WebSocket endpoint from a test.

``httpx`` has no WebSocket client, so these call the ASGI application directly
with a ``websocket`` scope. That is not a shortcut around the stack — it *is*
the interface Uvicorn uses, so the router-level credential dependency, the
exception handlers and the endpoint all run exactly as they do in production.

## Why this is a module rather than fixtures in conftest

Two test files need it — ``test_api_agents_integration.py`` for enrollment and
connection behaviour, and ``test_update_protocol_integration.py`` for the
update frames — and one importing the other breaks mypy: ``tests/`` has no
``__init__.py``, so a file imported as ``tests.test_x`` *and* collected as
``test_x`` is one source file under two module names.

A plain module, not named ``test_*``, is collected by neither and importable
by both. These are helpers rather than fixtures because most of them take
arguments that vary per call; a fixture would mean threading those through
``request.param``.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from contextlib import AbstractAsyncContextManager

    from httpx import AsyncClient

    from hcc.models.user import User

    ApiFactory = Callable[..., AbstractAsyncContextManager[AsyncClient]]
    SeedUser = Callable[..., Awaitable[User]]
    SignIn = Callable[..., Awaitable[str]]

HOSTS = "/api/v1/hosts"
ENROLL = "/api/v1/agents/enroll"
CONNECT = "/api/v1/agents/connect"


def csrf(token: str) -> dict[str, str]:
    return {"X-CSRF-Token": token}


def bearer(credential: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {credential}"}


async def register_and_issue(
    client: AsyncClient, seed_user: SeedUser, sign_in: SignIn, name: str = "homeserver"
) -> tuple[str, str]:
    """Arrange the administrative half: a host and a live enrollment credential.

    Through the real endpoints rather than by inserting rows, because the
    thing under test is the handover between the two halves — and a hand-made
    enrollment row would not prove the credential the administrator was shown
    is the one the agent can spend.
    """
    from hcc.models.user import UserRole

    await seed_user(client, username="admin", role=UserRole.ADMIN)
    token = await sign_in(client, "admin")
    host = (await client.post(HOSTS, json={"name": name}, headers=csrf(token))).json()
    issued = (
        await client.post(f"{HOSTS}/{host['host_id']}/enrollment", headers=csrf(token))
    ).json()
    return host["host_id"], issued["credential"]


async def enrolled_agent(
    client: AsyncClient, seed_user: SeedUser, sign_in: SignIn, name: str = "homeserver"
) -> tuple[str, str]:
    """A host with a live agent credential. Returns ``(host_id, credential)``."""
    host_id, enrollment_credential = await register_and_issue(client, seed_user, sign_in, name)
    body = (
        await client.post(
            ENROLL,
            headers=bearer(enrollment_credential),
            json={"agent_version": "0.1.0", "platform": "linux/amd64"},
        )
    ).json()
    return host_id, body["credential"]


async def enroll_another_host(client: AsyncClient, token: str, name: str) -> tuple[str, str]:
    """A second enrolled host, reusing an administrator already signed in.

    :func:`enrolled_agent` seeds its own administrator, so calling it twice
    would try to create the same account twice. This does the administrative
    half against the session that already exists.
    """
    host = (await client.post(HOSTS, json={"name": name}, headers=csrf(token))).json()
    issued = (
        await client.post(f"{HOSTS}/{host['host_id']}/enrollment", headers=csrf(token))
    ).json()
    enrolled = (
        await client.post(
            ENROLL,
            headers=bearer(issued["credential"]),
            json={"agent_version": "0.1.0", "platform": "linux/amd64"},
        )
    ).json()
    return host["host_id"], enrolled["credential"]


async def drive_socket(
    client: AsyncClient,
    *,
    headers: dict[str, str] | None = None,
    frames: list[str] | None = None,
    hold: float | None = None,
) -> list[dict[str, Any]]:
    """Open the agent socket, send *frames*, and return everything sent back.

    The ASGI application is called directly, as the real server does. ``hold``
    keeps the connection open after the last frame, for the tests that need
    the controller to act on its own — a supersession, a timeout.
    """
    app = client.app_state.app  # type: ignore[attr-defined]

    raw_headers: list[tuple[bytes, bytes]] = [(b"host", b"testserver")]
    cookie_header = "; ".join(f"{name}={value}" for name, value in client.cookies.items())
    if cookie_header:
        raw_headers.append((b"cookie", cookie_header.encode()))
    for name, value in (headers or {}).items():
        raw_headers.append((name.lower().encode(), value.encode()))

    inbound: list[dict[str, Any]] = [{"type": "websocket.connect"}]
    for frame in frames or []:
        inbound.append({"type": "websocket.receive", "text": frame})
    sent: list[dict[str, Any]] = []
    drained = asyncio.Event()

    async def receive() -> dict[str, Any]:
        if inbound:
            return inbound.pop(0)
        drained.set()
        if hold:
            await asyncio.sleep(hold)
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope: dict[str, Any] = {
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
        "headers": raw_headers,
        "subprotocols": [],
        "state": {},
    }

    await app(scope, receive, send)
    return sent


def accepted(messages: list[dict[str, Any]]) -> bool:
    return any(message["type"] == "websocket.accept" for message in messages)


def text_frames(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every JSON frame the controller sent, decoded."""
    return [
        json.loads(message["text"])
        for message in messages
        if message["type"] == "websocket.send" and message.get("text")
    ]
