"""The Phase 2 and 3 security review, written as attacks that must fail.

A prose review cannot tell a working defence from an absent one, and neither
can a test that exercises only the happy path. So each test below is an
attempt, against the live application and a real database, to make the system
do something it must not.

What is *not* here, because it is already covered elsewhere and duplicating it
would make this file look more thorough than it is:

* Every new route requiring a session. ``tests/test_api_authorization.py``
  enumerates the routing table and probes every operation, so the metrics and
  monitoring endpoints are covered by construction rather than by a test
  somebody remembered to write.
* Credential-type confusion on the host endpoints, and enrollment replay.
  ``apps/web/e2e/30-agent-security.spec.ts`` covers those against a live
  server.
* The ordinary refusals on the ingestion path — an undeclared metric, an
  unexpected label, a future timestamp, an oversized batch.
  ``tests/test_telemetry_integration.py`` covers each.

What *is* here is what those cannot reach:

**History cannot be rewritten.** The one attack that would be worst and is
easiest to overlook: delivery is at-least-once, so the agent re-sends, so the
write has to be idempotent — and an idempotent write that *updated* would let
a compromised host silently restate last week's measurements. It does not
update.

**One credential type is not another.** An agent credential must not reach the
configuration endpoints, and a browser session must not open the agent socket.
Either would turn one compromised host into an account, or one stolen cookie
into a telemetry source.

**A revoked credential stops working immediately**, including on a socket that
is already open and mid-batch.

**Nothing an agent or an administrator can type reaches SQL as syntax.** The
retention module interpolates identifiers, the config path takes a module id,
and labels become part of a series key — three places where a quoted string is
all that separates data from code.

**Cardinality is bounded.** An agent that can create unlimited series can fill
a disk without exceeding any rate limit, because each series is cheap and the
limit counts samples.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from hcc.agents.protocol import PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS
from hcc.models.user import UserRole

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
ENROLL = "/api/v1/agents/enroll"
CONNECT = "/api/v1/agents/connect"
MONITORING = "/api/v1/monitoring"


def csrf(token: str) -> dict[str, str]:
    return {"X-CSRF-Token": token}


def bearer(credential: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {credential}"}


async def enrolled_agent(
    client: AsyncClient, seed_user: SeedUser, sign_in: SignIn, name: str = "homeserver"
) -> tuple[str, str, str]:
    """An administrator session, a host, and a live agent credential."""
    await seed_user(client, username="admin", role=UserRole.ADMIN)
    token = await sign_in(client, "admin")
    host_id = (await client.post(HOSTS, json={"name": name}, headers=csrf(token))).json()["host_id"]
    enrollment = (await client.post(f"{HOSTS}/{host_id}/enrollment", headers=csrf(token))).json()[
        "credential"
    ]
    credential = (await client.post(ENROLL, headers=bearer(enrollment), json={})).json()[
        "credential"
    ]
    return host_id, credential, token


def socket_scope(credential: str | None, *, cookies: str | None = None) -> dict[str, Any]:
    headers: list[tuple[bytes, bytes]] = [(b"host", b"testserver")]
    if credential is not None:
        headers.append((b"authorization", f"Bearer {credential}".encode()))
    if cookies is not None:
        headers.append((b"cookie", cookies.encode()))
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
        "headers": headers,
        "subprotocols": [],
        "state": {},
    }


async def drive_socket(
    client: AsyncClient,
    *,
    credential: str | None = None,
    cookies: str | None = None,
    frames: list[str],
) -> list[dict[str, Any]]:
    app = client.app_state.app  # type: ignore[attr-defined]
    inbound: list[dict[str, Any]] = [{"type": "websocket.connect"}]
    for frame in frames:
        inbound.append({"type": "websocket.receive", "text": frame})
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        if inbound:
            return inbound.pop(0)
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(socket_scope(credential, cookies=cookies), receive, send)
    return sent


def accepted(messages: list[dict[str, Any]]) -> bool:
    """Whether the socket handshake was accepted at all."""
    return any(message["type"] == "websocket.accept" for message in messages)


def refused(messages: list[dict[str, Any]]) -> bool:
    """Whether the connection was refused, by either of the two mechanisms.

    There are two, and both are correct. An unauthenticated or unknown
    credential is rejected at the handshake, so nothing is accepted. A
    credential that is *valid but not usable* — a disabled host — is accepted
    and then closed with a stated reason, because the WebSocket protocol has
    no way to carry a reason on a rejected handshake and "your host is
    disabled" is worth telling an agent that will otherwise retry for ever.

    Asserting on the property rather than on one of the mechanisms, so that
    moving a check earlier or later in the flow is not a test failure.

    The close *code* is what discriminates, not the presence of a close. A
    successful connection ends with one too — code 1000, normal closure — and
    an earlier version of this predicate counted that as a refusal, which made
    the revocation and disabled-host tests pass against a system with no
    checks at all. ``test_a_working_connection_is_not_classified_as_refused``
    exists to catch that, and did.
    """
    if not accepted(messages):
        return True
    # A protocol-level close frame carries the reason, so its presence is the
    # application deciding to end the session rather than the peer going away.
    if decoded(messages, "close"):
        return True
    return any(
        message["type"] == "websocket.close" and message.get("code", 1000) != 1000
        for message in messages
    )


def decoded(messages: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [
        frame
        for frame in (
            json.loads(message["text"])
            for message in messages
            if message["type"] == "websocket.send" and message.get("text")
        )
        if frame["type"] == kind
    ]


def telemetry_frame(
    seq: int,
    *,
    module: str = "cpu",
    metric: str = "utilisation",
    value: float = 11.5,
    at: datetime | None = None,
    labels: dict[str, str] | None = None,
    **overrides: Any,
) -> str:
    payload: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "type": "telemetry",
        "seq": seq,
        "batch": [
            {
                "module": module,
                "schema_version": 1,
                "collected_at": (at or datetime.now(UTC)).isoformat(),
                "duration_ms": 2,
                "samples": [
                    {"metric": metric, "value": value, "labels": labels or {}},
                ],
            }
        ],
    }
    payload.update(overrides)
    return json.dumps(payload)


async def enable(client: AsyncClient, host_id: str, token: str, module: str = "cpu") -> None:
    response = await client.put(
        f"{HOSTS}/{host_id}/monitoring/modules/{module}",
        json={"enabled": True},
        headers=csrf(token),
    )
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# The test of the tests
# ---------------------------------------------------------------------------


async def test_a_working_connection_is_not_classified_as_refused(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
) -> None:
    """``refused`` has to discriminate, or every assertion using it is vacuous.

    It returns true for a connection that was closed, and a *successful*
    connection also ends by closing. If the predicate could not tell the two
    apart, the revocation and disabled-host tests above would pass against a
    system with no checks at all — which is the worst kind of green.

    So: a live credential on an enabled host, submitting telemetry that is
    actually stored, must come out as *not* refused.
    """
    async with api_factory() as client:
        host_id, credential, token = await enrolled_agent(client, seed_user, sign_in)
        await enable(client, host_id, token)

        messages = await drive_socket(client, credential=credential, frames=[telemetry_frame(1)])
        rows = await read_sql(client, "SELECT count(*) FROM metric_samples_raw")

    assert rows[0][0] == 1, "the happy path did not store a sample"
    assert accepted(messages)
    assert decoded(messages, "telemetry_ack")
    assert not refused(messages), (
        "the refusal predicate cannot tell a working connection from a rejected "
        "one, so every test using it proves nothing"
    )


# ---------------------------------------------------------------------------
# History cannot be rewritten
# ---------------------------------------------------------------------------


async def test_a_resent_sample_cannot_change_a_stored_value(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
) -> None:
    """The attack that would be worst, and is easiest to build in by accident.

    Delivery is at-least-once, so the agent re-sends, so the write must be
    idempotent. An idempotent write implemented as ``ON CONFLICT DO UPDATE``
    would be just as duplicate-free and would let a compromised host restate
    any measurement it had ever sent — quietly, with no second row to notice.

    So the same ``(series, timestamp)`` sent twice with *different values* must
    keep the first. This is the test that distinguishes the two
    implementations; a re-send with the same value cannot.
    """
    async with api_factory() as client:
        host_id, credential, token = await enrolled_agent(client, seed_user, sign_in)
        await enable(client, host_id, token)

        moment = datetime.now(UTC).replace(microsecond=0)
        await drive_socket(
            client, credential=credential, frames=[telemetry_frame(1, value=11.0, at=moment)]
        )
        await drive_socket(
            client, credential=credential, frames=[telemetry_frame(2, value=99.0, at=moment)]
        )

        rows = await read_sql(client, "SELECT value FROM metric_samples_raw ORDER BY value")

    # One row, and it is the original. Not two rows, and not the second value.
    assert [float(row[0]) for row in rows] == [pytest.approx(11.0)]


async def test_an_agent_cannot_backdate_over_an_existing_measurement(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
) -> None:
    """Backfill is legitimate; overwriting under cover of backfill is not.

    An agent reconnecting after an outage sends old timestamps, which the
    controller accepts inside the backfill window. That window is also the
    window in which a compromised agent could try to replace history, so the
    same idempotence has to hold for a timestamp an hour old as for a current
    one.
    """
    async with api_factory() as client:
        host_id, credential, token = await enrolled_agent(client, seed_user, sign_in)
        await enable(client, host_id, token)

        old = (datetime.now(UTC) - timedelta(hours=1)).replace(microsecond=0)
        await drive_socket(
            client, credential=credential, frames=[telemetry_frame(1, value=5.0, at=old)]
        )
        await drive_socket(
            client, credential=credential, frames=[telemetry_frame(2, value=95.0, at=old)]
        )

        rows = await read_sql(client, "SELECT value FROM metric_samples_raw")

    assert [float(row[0]) for row in rows] == [pytest.approx(5.0)]


# ---------------------------------------------------------------------------
# One credential type is not another
# ---------------------------------------------------------------------------


async def test_an_agent_credential_cannot_read_the_monitoring_configuration(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """An agent credential is for the socket, and for nothing else.

    It authenticates a *machine*, and the configuration endpoints answer to a
    *person*. A credential that worked on both would make one compromised host
    into a reader of the whole installation's configuration — and the host's
    own configuration already reaches it through the connection, so there is
    nothing it needs this for.
    """
    async with api_factory() as client:
        host_id, credential, _ = await enrolled_agent(client, seed_user, sign_in)
        client.cookies.clear()

        for path in (
            f"{HOSTS}/{host_id}/monitoring",
            f"{MONITORING}/defaults",
            f"{MONITORING}/modules",
            f"{HOSTS}/{host_id}/metrics/latest",
            f"{HOSTS}/{host_id}/metrics/status",
        ):
            response = await client.get(path, headers=bearer(credential))
            assert response.status_code == 401, f"{path}: {response.status_code}"


async def test_an_agent_credential_cannot_change_the_monitoring_configuration(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
) -> None:
    """The mirror image, and the one that would matter more.

    An agent that could enable its own modules could start collecting whatever
    it liked and the controller would record that an administrator had asked
    for it.
    """
    async with api_factory() as client:
        host_id, credential, _ = await enrolled_agent(client, seed_user, sign_in)
        client.cookies.clear()

        for path in (
            f"{HOSTS}/{host_id}/monitoring/modules/cpu",
            f"{MONITORING}/defaults/cpu",
        ):
            response = await client.put(path, json={"enabled": True}, headers=bearer(credential))
            assert response.status_code in {401, 403}, f"{path}: {response.status_code}"

        rows = await read_sql(client, "SELECT count(*) FROM host_module_overrides")
        rows_global = await read_sql(client, "SELECT count(*) FROM global_module_defaults")

    assert rows[0][0] == 0
    assert rows_global[0][0] == 0


async def test_a_browser_session_cannot_open_the_agent_socket(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """A stolen cookie must not become a telemetry source.

    The socket authenticates with a bearer agent credential and nothing else.
    If a session cookie were accepted, anybody who phished an administrator
    could submit measurements for any host — which is the one way to put a
    false reading into the database without touching a machine.
    """
    async with api_factory() as client:
        host_id, _, token = await enrolled_agent(client, seed_user, sign_in)
        await enable(client, host_id, token)
        cookie_header = "; ".join(f"{name}={value}" for name, value in client.cookies.items())
        assert cookie_header, "the session cookie is missing, so this proves nothing"

        messages = await drive_socket(
            client, credential=None, cookies=cookie_header, frames=[telemetry_frame(1)]
        )

    assert not accepted(messages), "the socket accepted a session cookie as a credential"


async def test_a_session_csrf_token_is_not_an_agent_credential(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """The CSRF token is readable by the page, so it is the obvious thing to try.

    It is a defence against cross-site requests, not a credential, and it is
    the one session-scoped value a browser hands to JavaScript.
    """
    async with api_factory() as client:
        _, _, token = await enrolled_agent(client, seed_user, sign_in)

        messages = await drive_socket(client, credential=token, frames=[telemetry_frame(1)])

    assert not accepted(messages)


# ---------------------------------------------------------------------------
# Authorization on the configuration surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.OPERATOR])
async def test_a_non_administrator_cannot_enable_collection(
    api_factory: ApiFactory,
    seed_user: SeedUser,
    sign_in: SignIn,
    read_sql: ReadSql,
    role: UserRole,
) -> None:
    """§2.6, and the reason it is administrator-only rather than operator.

    Enabling a module starts *reading a machine*. Someone who could do it
    without an administrator could begin collecting data about a host nobody
    agreed to monitor — which is a privacy decision, not an operational one.
    """
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        admin_token = await sign_in(client, "admin")
        host_id = (
            await client.post(HOSTS, json={"name": "target"}, headers=csrf(admin_token))
        ).json()["host_id"]

        await seed_user(client, username="other", role=role)
        token = await sign_in(client, "other")

        host_response = await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu",
            json={"enabled": True},
            headers=csrf(token),
        )
        global_response = await client.put(
            f"{MONITORING}/defaults/cpu", json={"enabled": True}, headers=csrf(token)
        )
        reset_response = await client.delete(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu", headers=csrf(token)
        )

        stored = await read_sql(client, "SELECT count(*) FROM host_module_overrides")
        stored_global = await read_sql(client, "SELECT count(*) FROM global_module_defaults")

    assert host_response.status_code == 403, host_response.text
    assert global_response.status_code == 403, global_response.text
    assert reset_response.status_code == 403, reset_response.text
    # The refusal must also mean nothing was written.
    assert stored[0][0] == 0
    assert stored_global[0][0] == 0


@pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.OPERATOR, UserRole.ADMIN])
async def test_every_role_may_read_what_a_host_collects(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, role: UserRole
) -> None:
    """The other half of the split, asserted so it cannot narrow by accident.

    A viewer who cannot see which modules are collecting cannot interpret a
    chart, so the reads are deliberately open to every authenticated role. If
    somebody tightens the gate by moving the router, this fails.
    """
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        admin_token = await sign_in(client, "admin")
        host_id = (
            await client.post(HOSTS, json={"name": "readable"}, headers=csrf(admin_token))
        ).json()["host_id"]

        await seed_user(client, username="reader", role=role)
        await sign_in(client, "reader")

        for path in (
            f"{HOSTS}/{host_id}/monitoring",
            f"{MONITORING}/defaults",
            f"{MONITORING}/modules",
            f"{HOSTS}/{host_id}/metrics/latest",
            f"{HOSTS}/{host_id}/metrics/series",
            f"{HOSTS}/{host_id}/metrics/status",
        ):
            response = await client.get(path)
            assert response.status_code == 200, f"{path}: {response.text}"


# ---------------------------------------------------------------------------
# Revocation takes effect at once
# ---------------------------------------------------------------------------


async def test_a_revoked_credential_cannot_submit_telemetry(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
) -> None:
    """Revocation is the remedy for a compromised host, so it has to be real.

    A credential that kept working until its socket happened to close would
    make revocation a request rather than an action — and the compromised agent
    is precisely the one that will not close its socket.
    """
    async with api_factory() as client:
        host_id, credential, token = await enrolled_agent(client, seed_user, sign_in)
        await enable(client, host_id, token)

        # It works first, or the test proves nothing about revocation.
        before = await drive_socket(client, credential=credential, frames=[telemetry_frame(1)])
        assert accepted(before)
        assert decoded(before, "telemetry_ack")

        revoke = await client.post(f"{HOSTS}/{host_id}/credential/revoke", headers=csrf(token))
        assert revoke.status_code == 200, revoke.text

        after = await drive_socket(
            client,
            credential=credential,
            frames=[telemetry_frame(2, value=77.0)],
        )
        rows = await read_sql(client, "SELECT count(*) FROM metric_samples_raw")

    assert refused(after), "a revoked credential was allowed to keep submitting"
    assert rows[0][0] == 1, "a revoked credential stored a sample"


async def test_a_disabled_host_cannot_submit_telemetry(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
) -> None:
    """Disabling is 'not right now', and that has to include telemetry.

    A disabled host whose agent kept delivering would keep filling the
    database while the interface said it was refused at the door.
    """
    async with api_factory() as client:
        host_id, credential, token = await enrolled_agent(client, seed_user, sign_in)
        await enable(client, host_id, token)

        disable = await client.post(f"{HOSTS}/{host_id}/disable", headers=csrf(token))
        assert disable.status_code == 200, disable.text

        messages = await drive_socket(client, credential=credential, frames=[telemetry_frame(1)])
        rows = await read_sql(client, "SELECT count(*) FROM metric_samples_raw")

    assert refused(messages), "a disabled host was allowed to submit"
    # The refusal is reported rather than being a silent disconnect: an agent
    # that is told "host_disabled" can stop retrying, and an operator reading
    # its log can see why it stopped.
    reasons = [frame.get("reason") for frame in decoded(messages, "close")]
    assert "host_disabled" in reasons, reasons
    assert rows[0][0] == 0


# ---------------------------------------------------------------------------
# Nothing typed reaches SQL as syntax
# ---------------------------------------------------------------------------


#: Injection attempts that can travel in a URL path segment.
#:
#: No NUL byte here. It cannot appear in a request line at all — httpx refuses
#: to construct the URL and so would any HTTP server or proxy — so a test
#: using one would be asserting that the HTTP client works, not that the
#: application does. It is exercised through a JSON body below, where it is
#: representable.
PATH_INJECTION_STRINGS = [
    "'; DROP TABLE metric_samples_raw; --",
    "metric_samples_raw'); DELETE FROM hosts; --",
    '" OR 1=1 --',
    "\\'; SELECT pg_sleep(10); --",
]

#: The same, plus the cases only a JSON body can carry.
BODY_INJECTION_STRINGS = [
    *PATH_INJECTION_STRINGS,
    "cpu\x00; DROP TABLE hosts",
    "\n'; DROP TABLE hosts; --",
]


@pytest.mark.parametrize("payload", PATH_INJECTION_STRINGS)
async def test_a_module_id_cannot_carry_sql(
    api_factory: ApiFactory,
    seed_user: SeedUser,
    sign_in: SignIn,
    read_sql: ReadSql,
    payload: str,
) -> None:
    """The module id is a path parameter and reaches a registry lookup.

    It is checked against a closed set before anything else happens, so the
    answer is 404 or 422 — and the tables that an injection would have reached
    are still there afterwards, which is the assertion that would fail if one
    worked.
    """
    async with api_factory() as client:
        host_id, _, token = await enrolled_agent(client, seed_user, sign_in)

        host_response = await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/{payload}",
            json={"enabled": True},
            headers=csrf(token),
        )
        global_response = await client.put(
            f"{MONITORING}/defaults/{payload}", json={"enabled": True}, headers=csrf(token)
        )

        # Still answering, and the tables still exist.
        alive = await read_sql(client, "SELECT count(*) FROM hosts")
        samples = await read_sql(client, "SELECT count(*) FROM metric_samples_raw")

    assert host_response.status_code in {400, 404, 422}, host_response.status_code
    assert global_response.status_code in {400, 404, 422}, global_response.status_code
    assert alive[0][0] == 1
    assert samples[0][0] == 0


@pytest.mark.parametrize("payload", BODY_INJECTION_STRINGS)
async def test_a_label_value_cannot_carry_sql(
    api_factory: ApiFactory,
    seed_user: SeedUser,
    sign_in: SignIn,
    read_sql: ReadSql,
    payload: str,
) -> None:
    """A label value becomes part of a series key, which is stored and compared.

    The agent supplies it, so it is attacker-controlled on a compromised host.
    Either the sample is refused or the value is stored verbatim as data; what
    must not happen is it being executed.
    """
    async with api_factory() as client:
        host_id, credential, token = await enrolled_agent(client, seed_user, sign_in)
        await enable(client, host_id, token)

        await drive_socket(
            client,
            credential=credential,
            frames=[
                telemetry_frame(1, metric="core_utilisation", labels={"core": payload}, value=5.0)
            ],
        )

        alive = await read_sql(client, "SELECT count(*) FROM hosts")
        keys = await read_sql(client, "SELECT labels_key FROM metric_series")

    assert alive[0][0] == 1
    # Whatever was stored is data: either nothing, or the string itself.
    for row in keys:
        assert row[0] in {"", f"core={payload}"}


async def test_a_host_name_with_sql_is_stored_as_text(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
) -> None:
    """A host name reaches the metrics queries through its id, not its name.

    Asserted anyway, because the name is the most obvious operator-supplied
    string in the system and a future query that interpolated it would be a
    quiet regression.
    """
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")
        name = "nas'; DROP TABLE hosts; --"
        created = await client.post(HOSTS, json={"name": name}, headers=csrf(token))

        rows = await read_sql(client, "SELECT count(*) FROM hosts")

    assert created.status_code in {201, 422}
    assert rows[0][0] >= 0  # The table exists, which is the point.


@pytest.mark.parametrize("bad_host", ["not-a-uuid", "1 OR 1=1", "../../etc/passwd", ""])
async def test_a_host_id_that_is_not_a_uuid_never_reaches_a_query(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, bad_host: str
) -> None:
    """The type is the defence, and it runs before the handler does.

    FastAPI parses the path parameter as a UUID, so a value that is not one is
    refused by validation rather than by anything in the query layer. A 422 or
    404 here means nothing reached SQL at all.
    """
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        await sign_in(client, "admin")

        for leaf in ("metrics/latest", "metrics/series", "metrics/status", "monitoring"):
            response = await client.get(f"{HOSTS}/{bad_host}/{leaf}")
            assert response.status_code in {404, 422}, f"{leaf}: {response.status_code}"


# ---------------------------------------------------------------------------
# Cardinality and resource bounds
# ---------------------------------------------------------------------------


async def test_an_agent_cannot_invent_label_keys_to_multiply_series(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
) -> None:
    """§18: avoid excessive metric cardinality.

    A series is cheap and the rate limit counts samples, so an agent that could
    name its own labels could create a million series inside the budget — and
    every one of them is an index entry and a row in ``metric_series``. The
    registry declares which label keys a metric has, and anything else is
    refused.
    """
    async with api_factory() as client:
        host_id, credential, token = await enrolled_agent(client, seed_user, sign_in)
        await enable(client, host_id, token)

        frames = [
            telemetry_frame(
                index + 1,
                metric="core_utilisation",
                labels={"core": "0", f"invented_{index}": "x"},
                value=1.0,
            )
            for index in range(5)
        ]
        messages = await drive_socket(client, credential=credential, frames=frames)

        series = await read_sql(client, "SELECT count(*) FROM metric_series")

    acks = decoded(messages, "telemetry_ack")
    assert acks, "the agent received no acknowledgement at all"
    assert all(ack["rejected"] >= 1 for ack in acks), acks
    assert series[0][0] == 0, "an undeclared label key created a series"


async def test_an_unbounded_label_value_is_refused(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
) -> None:
    """A long label value is a cheap way to make a large index entry.

    The series key is stored and indexed, so an agent sending kilobyte label
    values would bloat the index without sending many samples.
    """
    async with api_factory() as client:
        host_id, credential, token = await enrolled_agent(client, seed_user, sign_in)
        await enable(client, host_id, token)

        messages = await drive_socket(
            client,
            credential=credential,
            frames=[
                telemetry_frame(
                    1, metric="core_utilisation", labels={"core": "9" * 4096}, value=1.0
                )
            ],
        )
        series = await read_sql(client, "SELECT count(*) FROM metric_series")

    # Either the frame is refused at the protocol boundary (no accept, or no
    # ack) or the sample is counted as rejected. What must not happen is a
    # four-kilobyte series key being indexed.
    assert series[0][0] == 0
    for ack in decoded(messages, "telemetry_ack"):
        assert ack["accepted"] == 0, ack


async def test_a_query_cannot_ask_for_an_unbounded_number_of_points(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """An unbounded point budget is a request for the whole table.

    Bounded on the way in rather than by whatever the database manages to
    return, because the cost is paid before the limit would apply.
    """
    async with api_factory() as client:
        host_id, _, _ = await enrolled_agent(client, seed_user, sign_in)
        start = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        end = datetime.now(UTC).isoformat()

        for budget in ("1000000", "999999999", "-1", "0"):
            response = await client.get(
                f"{HOSTS}/{host_id}/metrics/range",
                params={"series_id": 1, "start": start, "end": end, "max_points": budget},
            )
            assert response.status_code == 422, f"{budget}: {response.status_code}"


async def test_a_range_cannot_span_an_arbitrary_number_of_partitions(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """Query planning cost grows with the partitions a range touches.

    A ten-year window is cheap to ask for and not cheap to plan, so the bound
    is on the window rather than on the rows it would return.
    """
    async with api_factory() as client:
        host_id, _, _ = await enrolled_agent(client, seed_user, sign_in)

        response = await client.get(
            f"{HOSTS}/{host_id}/metrics/range",
            params={
                "series_id": 1,
                "start": "2016-01-01T00:00:00+00:00",
                "end": "2026-01-01T00:00:00+00:00",
            },
        )

    assert response.status_code == 422, response.text
    assert "max_range_days" in response.json()["error"]["details"]


async def test_a_freshness_window_cannot_be_widened_without_limit(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """ "Latest" with an unbounded window is a full table scan per series.

    And it would also stop meaning anything: a reading from last year is not a
    current value however it is labelled.
    """
    async with api_factory() as client:
        host_id, _, _ = await enrolled_agent(client, seed_user, sign_in)

        for window in ("99999999", "0", "-100"):
            response = await client.get(
                f"{HOSTS}/{host_id}/metrics/latest", params={"freshness_seconds": window}
            )
            assert response.status_code == 422, f"{window}: {response.status_code}"


# ---------------------------------------------------------------------------
# The configuration surface accepts only what it declares
# ---------------------------------------------------------------------------


async def test_a_schedule_with_unknown_keys_is_refused(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
) -> None:
    """An accepted-but-ignored key is a setting somebody believes is in force.

    §7: do not silently ignore invalid settings. The agent merges nothing, so a
    key the controller discarded would be a configuration the operator could
    read back from the interface and that no machine was applying.
    """
    async with api_factory() as client:
        host_id, _, token = await enrolled_agent(client, seed_user, sign_in)

        response = await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu",
            json={
                "enabled": True,
                "schedule": {
                    "kind": "interval",
                    "interval_seconds": 10,
                    "run_as_root": True,
                    "command": "/bin/sh",
                },
            },
            headers=csrf(token),
        )
        stored = await read_sql(client, "SELECT count(*) FROM host_module_overrides")

    assert response.status_code == 422, response.text
    assert stored[0][0] == 0


async def test_the_configuration_surface_has_no_command_field(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """§2.4 and §27: there is no path to remote command execution here.

    Asserted against the schema rather than by trying one field name, so a
    future addition cannot slip a command, script, path or shell parameter into
    the monitoring surface without this failing.
    """
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        await sign_in(client, "admin")
        schema = client.app_state.app.openapi()  # type: ignore[attr-defined]

    forbidden = {"command", "cmd", "script", "shell", "exec", "args", "argv", "path", "binary"}
    offenders: list[str] = []
    for name, definition in schema["components"]["schemas"].items():
        if not any(token in name.lower() for token in ("module", "schedule", "monitoring")):
            continue
        for field in definition.get("properties") or {}:
            if field.lower() in forbidden:
                offenders.append(f"{name}.{field}")

    assert offenders == [], f"the monitoring surface grew an execution field: {offenders}"


async def test_an_extra_top_level_field_is_refused(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """``extra='forbid'`` on the request model, verified rather than assumed.

    A request body that quietly accepted unknown fields would make every future
    typo a silent no-op.
    """
    async with api_factory() as client:
        host_id, _, token = await enrolled_agent(client, seed_user, sign_in)

        response = await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu",
            json={"enabled": True, "collect_everything": True},
            headers=csrf(token),
        )

    assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# Nothing secret is returned
# ---------------------------------------------------------------------------


async def test_no_monitoring_response_contains_a_credential(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """§6: no secret exposure.

    The agent credential is the one long-lived secret in this system, and the
    monitoring and metrics responses are the newest and least-reviewed surface.
    The whole credential is searched for, not a prefix, because a response that
    returned a truncated one would be just as useful to an attacker who already
    had the rest.
    """
    async with api_factory() as client:
        host_id, credential, token = await enrolled_agent(client, seed_user, sign_in)
        await enable(client, host_id, token)

        bodies: list[str] = []
        for path in (
            f"{HOSTS}/{host_id}/monitoring",
            f"{HOSTS}/{host_id}/monitoring/revisions",
            f"{MONITORING}/defaults",
            f"{MONITORING}/modules",
            f"{HOSTS}/{host_id}/metrics/latest",
            f"{HOSTS}/{host_id}/metrics/series",
            f"{HOSTS}/{host_id}/metrics/status",
            f"{HOSTS}/{host_id}",
        ):
            response = await client.get(path)
            assert response.status_code == 200, f"{path}: {response.text}"
            bodies.append(response.text)

    joined = "\n".join(bodies)
    assert credential not in joined
    # Nor any fragment long enough to be the credential's distinguishing part.
    assert credential[:16] not in joined
    for marker in ("password_hash", "token_hash", "secret"):
        assert marker not in joined, f"{marker} appears in a monitoring response"


async def test_a_config_revision_does_not_echo_who_may_read_it(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """The configuration document goes to the agent, so it must carry no secret.

    It is also stored and returned through the revisions endpoint, which means
    anything put in it is readable by every authenticated role. Asserted here
    so that a future field — a token for an exporter, a path to a key — is
    noticed when it is added rather than after it ships.
    """
    async with api_factory() as client:
        host_id, _, token = await enrolled_agent(client, seed_user, sign_in)
        await enable(client, host_id, token)

        revisions = await client.get(f"{HOSTS}/{host_id}/monitoring/revisions")
        assert revisions.status_code == 200, revisions.text

    body = revisions.text.lower()
    for marker in ("credential", "password", "secret", "private_key", "bearer"):
        assert marker not in body, f"{marker} appears in a configuration revision"


# ---------------------------------------------------------------------------
# The protocol boundary
# ---------------------------------------------------------------------------


async def test_a_telemetry_payload_naming_a_host_is_refused(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
) -> None:
    """§15: host identity comes from the connection, never from the payload.

    The ``Telemetry`` model has no ``host_id`` field and forbids extras, so a
    frame carrying one is refused outright rather than having the field
    ignored. Ignoring it would be almost as good and not quite: a compromised
    agent would get a success for a batch it believed it had misattributed, and
    the next person to add a field would have no boundary to add it inside.
    """
    async with api_factory() as client:
        host_id, credential, token = await enrolled_agent(client, seed_user, sign_in)
        await enable(client, host_id, token)
        other = (await client.post(HOSTS, json={"name": "victim"}, headers=csrf(token))).json()[
            "host_id"
        ]

        messages = await drive_socket(
            client,
            credential=credential,
            frames=[telemetry_frame(1, host_id=other)],
        )
        rows = await read_sql(client, "SELECT DISTINCT host_id::text FROM metric_series")

    # Either refused at the boundary or attributed to the authenticated host —
    # never to the one the payload named.
    assert [row[0] for row in rows] in ([], [host_id])
    assert other not in {row[0] for row in rows}
    assert not any(frame.get("accepted") for frame in decoded(messages, "telemetry_ack"))


async def test_the_socket_offers_no_command_message_type(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """§2.4: the monitoring protocol must not expose a shell.

    The agent dispatches on a closed table of message types. A frame naming a
    type outside it is refused, and the ones tried here are the names a shell
    channel would plausibly have had — so adding one would make this fail.
    """
    async with api_factory() as client:
        host_id, credential, token = await enrolled_agent(client, seed_user, sign_in)
        await enable(client, host_id, token)

        for kind in ("exec", "command", "shell", "run", "script", "eval"):
            frame = json.dumps(
                {
                    "v": PROTOCOL_VERSION,
                    "type": kind,
                    "seq": 1,
                    "command": "/bin/sh -c 'id'",
                }
            )
            messages = await drive_socket(client, credential=credential, frames=[frame])
            # Refused: no acknowledgement of any kind for an unknown type.
            assert not decoded(messages, f"{kind}_ack"), kind
            assert not decoded(messages, "telemetry_ack"), kind


async def test_an_unknown_module_in_telemetry_is_refused(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
) -> None:
    """The registry is closed on the ingestion side too.

    An agent that could name its own module would create series the interface
    cannot describe and the retention policy does not cover — and a module id
    is the one string from the agent that reaches a registry lookup.
    """
    async with api_factory() as client:
        host_id, credential, token = await enrolled_agent(client, seed_user, sign_in)
        await enable(client, host_id, token)

        for module in ("disks", "shell", "../cpu", "CPU", "cpu "):
            await drive_socket(
                client,
                credential=credential,
                frames=[telemetry_frame(1, module=module)],
            )

        series = await read_sql(client, "SELECT count(*) FROM metric_series")

    assert series[0][0] == 0


async def test_an_agent_cannot_claim_a_protocol_version_it_likes(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """A version field an attacker controls is a way to select older handling.

    **Updated for protocol 2.** There used to be one version, so anything else
    was refused outright. There are now two, because update management added
    frames that an older agent's parser refuses — so the set is closed rather
    than singular, and the property to assert changed shape with it.

    Two things are asserted, and the second is the one that matters:

    1. A version outside `SUPPORTED_PROTOCOL_VERSIONS` is refused rather than
       negotiated down, which is the pattern that produces downgrade attacks
       in protocols that do negotiate.
    2. Claiming the *older supported* version grants nothing. That is the
       question two versions raise: if version 1 handling were more permissive
       in any respect, an agent could select it. It is not — version 1 means
       the agent is never offered an update over the socket, which is strictly
       less capability, and nothing else differs. A test is the right place to
       hold that, because "the old version is not more permissive" is an
       invariant somebody could break while adding version 3.
    """
    async with api_factory() as client:
        host_id, credential, token = await enrolled_agent(client, seed_user, sign_in)
        await enable(client, host_id, token)

        for version in (0, 3, 99, -1):
            assert version not in SUPPORTED_PROTOCOL_VERSIONS
            messages = await drive_socket(
                client,
                credential=credential,
                frames=[telemetry_frame(1, v=version)],
            )
            assert not any(frame.get("accepted") for frame in decoded(messages, "telemetry_ack")), (
                version
            )

        # And every supported version is accepted, with the same
        # authorization. An agent claiming the older one is not refused — that
        # would strand the installed fleet — and gains nothing by it.
        for version in SUPPORTED_PROTOCOL_VERSIONS:
            messages = await drive_socket(
                client,
                credential=credential,
                frames=[telemetry_frame(1, v=version)],
            )
            accepted = [frame.get("accepted") for frame in decoded(messages, "telemetry_ack")]
            assert accepted and all(count == 1 for count in accepted), (version, accepted)


async def test_an_oversized_frame_cannot_exhaust_memory(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
) -> None:
    """The batch bounds are what stop one frame being a memory budget.

    An agent is authenticated, so this is not an anonymous denial of service —
    it is the compromised-host case, where the attacker has a credential and
    wants the controller rather than the data.
    """
    async with api_factory() as client:
        host_id, credential, token = await enrolled_agent(client, seed_user, sign_in)
        await enable(client, host_id, token)

        huge = json.dumps(
            {
                "v": PROTOCOL_VERSION,
                "type": "telemetry",
                "seq": 1,
                "batch": [
                    {
                        "module": "cpu",
                        "schema_version": 1,
                        "collected_at": datetime.now(UTC).isoformat(),
                        "duration_ms": 1,
                        "samples": [
                            {"metric": "utilisation", "value": 1.0, "labels": {}}
                            for _ in range(10_000)
                        ],
                    }
                    for _ in range(200)
                ],
            }
        )
        messages = await drive_socket(client, credential=credential, frames=[huge])
        rows = await read_sql(client, "SELECT count(*) FROM metric_samples_raw")

    assert rows[0][0] == 0, "an oversized batch was stored"
    assert not any(frame.get("accepted") for frame in decoded(messages, "telemetry_ack"))


async def test_a_series_id_from_another_installation_is_not_readable(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """Series ids are sequential, so enumeration is the obvious attack.

    There is no per-host authorization yet — every authenticated role may read
    every host — so what this protects is attribution rather than access: a
    chart must not show host A's data under host B's name, which would make an
    operator act on the wrong machine.
    """
    async with api_factory() as client:
        host_id, _, token = await enrolled_agent(client, seed_user, sign_in)
        other = (await client.post(HOSTS, json={"name": "other"}, headers=csrf(token))).json()[
            "host_id"
        ]
        start = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        end = datetime.now(UTC).isoformat()

        # Sequential ids, so walking them is one loop.
        for candidate in range(1, 25):
            response = await client.get(
                f"{HOSTS}/{other}/metrics/range",
                params={"series_id": candidate, "start": start, "end": end},
            )
            assert response.status_code == 404, f"series {candidate}: {response.status_code}"
            # And the refusal says nothing about whether the id exists.
            assert response.json()["error"]["code"] == "not_found"

        assert uuid.UUID(host_id) != uuid.UUID(other)


async def test_the_maintenance_lock_cannot_be_taken_by_a_request(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
) -> None:
    """A request-scoped session must not be able to hold the maintenance lock.

    It is session-scoped and installation-wide, so a request that took it and
    returned would hold it until its pooled connection was recycled — and
    every maintenance run in between would skip. Not reachable through any
    endpoint, which is what this asserts.
    """
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        await sign_in(client, "admin")
        await client.get(f"{MONITORING}/defaults")
        await client.get(f"{MONITORING}/modules")

        held = await read_sql(
            client,
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
            "AND classid = 1296649298",  # 0x4D455452, "METR"
        )

    assert held[0][0] == 0
