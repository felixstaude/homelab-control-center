"""API authentication enforcement and role authorization.

Two halves:

* A **default-deny regression test** that walks every path in the running
  application's OpenAPI schema, calls it without credentials, and requires a
  401 unless the path is in the public allow-list. This is behavioural on
  purpose: it tests what the server actually does rather than introspecting
  dependency graphs, so it catches a route added anywhere — including one
  mounted outside ``hcc.api.v1.router``, which no amount of router nesting
  could prevent.

* **Role authorization tests** against a test-only router. Role-gated routes
  have no production call site yet — user management is a later milestone — so
  the dependencies are exercised on synthetic endpoints rather than by
  inventing product endpoints for the tests to lean on. The router is defined
  here and clearly marked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import pytest
from fastapi import APIRouter, Depends, WebSocket
from fastapi.routing import APIWebSocketRoute
from sqlalchemy import text
from starlette.routing import WebSocketRoute

# Runtime imports, not TYPE_CHECKING: these annotate FastAPI route
# handlers below, and FastAPI resolves endpoint annotations at runtime to
# build its dependency graph. Same constraint as hcc/api/deps.py.
from hcc.api.deps import AdminDep, OperatorDep, ViewerDep, get_current_session
from hcc.app import PUBLIC_PATHS
from hcc.models import User, UserRole

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterable, Iterator
    from contextlib import AbstractAsyncContextManager

    from httpx import AsyncClient

    ApiFactory = Callable[..., AbstractAsyncContextManager[AsyncClient]]

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"
LOGIN = "/api/v1/auth/login"
SETUP_STATUS = "/api/v1/setup/status"

#: Pseudo-method marking a WebSocket route in the enumeration, so one walk
#: describes both protocols.
WEBSOCKET: Final = "WEBSOCKET"

#: The expected public surface, written out literally.
#:
#: Duplicating `hcc.app.PUBLIC_PATHS` is the point: widening the allow-list in
#: the application then requires editing this test too, which makes it a
#: reviewed decision rather than a diff nobody notices.
EXPECTED_PUBLIC_PATHS = {
    "/healthz",
    "/readyz",
    "/api/v1/auth/login",
    # The web client needs this before it has a credential. One boolean, and
    # safe to disclose only because bootstrap has no public path — ADR-036.
    "/api/v1/setup/status",
    # FastAPI's own documentation endpoints. Public outside production and
    # absent inside it. They were missing from this list until milestone 1.5,
    # because the schema-based scan could not see them —
    # `TestDocumentationEndpoints` covers both halves.
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
}


# ---------------------------------------------------------------------------
# Test-only role-gated router
# ---------------------------------------------------------------------------

probe_router = APIRouter(prefix="/_probe", tags=["test-only"])
"""Synthetic endpoints for exercising the authorization dependencies.

**Not part of the product.** Mounted only by these tests, via the
`api_factory` fixture's `routers` argument.
"""


@probe_router.get("/viewer")
async def probe_viewer(current: ViewerDep) -> dict[str, str]:
    return {"role": current.user.role.value}


@probe_router.get("/operator")
async def probe_operator(current: OperatorDep) -> dict[str, str]:
    return {"role": current.user.role.value}


@probe_router.get("/admin")
async def probe_admin(current: AdminDep) -> dict[str, str]:
    return {"role": current.user.role.value}


@probe_router.post("/admin-action")
async def probe_admin_action(current: AdminDep) -> dict[str, str]:
    """A state-changing, role-gated route, for the CSRF interaction."""
    return {"role": current.user.role.value}


protected_socket_router = APIRouter(dependencies=[Depends(get_current_session)])
"""A WebSocket mounted the way a real one will be.

Carries the **same** router-level dependency object as
``hcc.api.v1.router._protected``, so this exercises the production
arrangement rather than an approximation of it;
``test_the_probe_socket_uses_the_production_dependency`` asserts the two
cannot drift apart.

**Not part of the product.** Mounted only by these tests.
"""


@protected_socket_router.websocket("/_ws-protected")
async def probe_protected_socket(socket: WebSocket) -> None:
    await socket.accept()
    await socket.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def seed_user(
    client: AsyncClient,
    *,
    username: str,
    role: UserRole,
    is_active: bool = True,
    password: str = PASSWORD,
) -> User:
    hasher = client.app_state.password_hasher  # type: ignore[attr-defined]
    async with client.app_state.database.session_factory() as db:  # type: ignore[attr-defined]
        user = User(
            username=username,
            password_hash=hasher.hash(password),
            role=role,
            is_active=is_active,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user


async def run_sql(client: AsyncClient, statement: str) -> None:
    async with client.app_state.database.session_factory() as db:  # type: ignore[attr-defined]
        await db.execute(text(statement))
        await db.commit()


async def bootstrap(client: AsyncClient, *, username: str = "admin") -> None:
    """Initialize the installation the way the CLI does.

    Calls the same service the command calls, rather than reimplementing the
    insert, so these tests cannot pass against a different invariant than the
    one bootstrap actually enforces.
    """
    from hcc.security.bootstrap import bootstrap_administrator

    async with client.app_state.database.session_factory() as db:  # type: ignore[attr-defined]
        await bootstrap_administrator(
            db,
            username=username,
            password=PASSWORD,
            hasher=client.app_state.password_hasher,  # type: ignore[attr-defined]
        )


async def login_as(client: AsyncClient, username: str) -> str:
    """Log in and return the CSRF token."""
    response = await client.post(LOGIN, json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    token: str = response.json()["csrf_token"]
    return token


def _walk_routes(routes: Iterable[Any]) -> Iterator[tuple[str, Any]]:
    """Yield ``(effective path, route)`` for everything the app will route.

    Three shapes appear in a FastAPI routing table:

    * a plain Starlette ``Route`` — the framework's own ``/docs`` and
      ``/openapi.json``, which carry their full path directly;
    * an ``_IncludedRouter`` — the wrapper this FastAPI version creates for
      ``include_router``, which does not flatten. Its ``effective_candidates()``
      expands one level, and nests further for each inner ``include_router``;
    * an ``_EffectiveRouteContext`` — the expansion of one route, carrying the
      prefix-resolved ``path`` and the ``original_route`` that says whether it
      is HTTP or WebSocket.

    This does reach into FastAPI's arrangement, which ADR-031 deliberately
    avoided when the scan was schema-based. The trade changed: the schema
    cannot see hidden or WebSocket routes at all, so a scan built on it was
    structurally unable to cover the surfaces of Phases 5, 7 and 11. The
    coupling is made safe by
    ``test_the_scan_covers_every_routed_operation``, which asserts the known
    production routes are present — so a FastAPI change that breaks this
    traversal fails loudly rather than silently reducing the scan to nothing.
    """
    for route in routes:
        candidates = getattr(route, "effective_candidates", None)
        if callable(candidates):
            yield from _walk_routes(candidates())
            # Routes deliberately matched last (path-parameter routes) live in
            # a separate list and are just as routable.
            yield from _walk_routes(route.effective_low_priority_routes())
            continue

        original = getattr(route, "original_route", None)
        if original is not None:
            # `path` is the prefix-resolved path for an HTTP route, but is
            # left empty for a WebSocket one; there, the already-prefixed
            # `starlette_route` carries it. Falling back rather than trusting
            # one field matters — an empty path would silently drop the route
            # from the scan, which is the failure mode this test exists to
            # prevent.
            effective = route.path or getattr(route.starlette_route, "path", "")
            if effective:
                yield effective, original
            continue

        path = getattr(route, "path", None)
        if path:
            yield path, route


def iter_operations(client: AsyncClient) -> list[tuple[str, str]]:
    """Every (method, path) the running application will actually route.

    Enumerated from ``app.routes`` rather than from the OpenAPI schema. The
    schema was the milestone 1.4 source and it has two blind spots that an
    audit demonstrated:

    * a route declared ``include_in_schema=False`` does not appear in it;
    * a **WebSocket** route never appears in it, because FastAPI's
      ``paths`` only describes HTTP operations.

    Both would therefore have been exempt from the default-deny guarantee
    while looking covered. Walking the real routing table closes that, and
    ``TestHiddenAndWebSocketRoutes`` below proves the detection works by
    mounting deliberately unprotected routes of each kind.

    WebSocket routes are reported with the pseudo-method ``WEBSOCKET`` so one
    enumeration can describe both protocols; callers dispatch on it.
    """
    app = client.app_state.app  # type: ignore[attr-defined]
    operations: set[tuple[str, str]] = set()

    for path, route in _walk_routes(app.routes):
        if isinstance(route, APIWebSocketRoute | WebSocketRoute):
            operations.add((WEBSOCKET, path))
            continue
        for method in getattr(route, "methods", None) or ():
            if method in ("HEAD", "OPTIONS"):
                # HEAD is generated alongside GET and carries no separate
                # authorization story; OPTIONS is CORS preflight, which must
                # stay reachable without a session or no browser could ever
                # make a cross-origin request.
                continue
            operations.add((method.upper(), path))

    return sorted(operations)


# ---------------------------------------------------------------------------
# Default-deny
# ---------------------------------------------------------------------------


async def attempt_websocket(
    client: AsyncClient, path: str, *, headers: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """Drive a WebSocket handshake against the app and return what it sent.

    ``httpx`` has no WebSocket client, so the ASGI application is called
    directly with a ``websocket`` scope. That is not a shortcut around the
    stack — it *is* the interface a real server uses, so dependencies,
    exception handlers and the router-level authentication dependency all run
    exactly as they would in production.

    A handshake that was **accepted** sends ``websocket.accept``. One that was
    refused sends either ``websocket.close`` or a *denial response*
    (``websocket.http.response.start``), which is how Starlette reports an
    exception raised before the upgrade.
    """
    app = client.app_state.app  # type: ignore[attr-defined]
    cookie_header = "; ".join(f"{name}={value}" for name, value in client.cookies.items())

    raw_headers: list[tuple[bytes, bytes]] = [(b"host", b"testserver")]
    if cookie_header:
        raw_headers.append((b"cookie", cookie_header.encode()))
    for name, value in (headers or {}).items():
        raw_headers.append((name.lower().encode(), value.encode()))

    inbound: list[dict[str, Any]] = [{"type": "websocket.connect"}]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        if inbound:
            return inbound.pop(0)
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
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": raw_headers,
        "subprotocols": [],
        "state": {},
    }

    await app(scope, receive, send)
    return sent


def was_accepted(messages: list[dict[str, Any]]) -> bool:
    """Whether a handshake reached ``accept`` — i.e. the socket was open."""
    return any(message["type"] == "websocket.accept" for message in messages)


class TestDefaultDeny:
    async def test_every_non_public_operation_requires_authentication(
        self, api_factory: ApiFactory
    ) -> None:
        """The regression test this milestone exists for.

        A route added without authentication shows up here as a non-401, and
        the failure names it.
        """
        async with api_factory(routers=[probe_router]) as client:
            unprotected: list[str] = []

            for method, path in iter_operations(client):
                if path in PUBLIC_PATHS:
                    continue
                response = await client.request(method, path)
                if response.status_code != 401:
                    unprotected.append(f"{method} {path} -> {response.status_code}")

            assert not unprotected, (
                "these operations are neither authenticated nor in the public "
                f"allow-list: {unprotected}"
            )

    async def test_the_scan_covers_every_routed_operation(self, api_factory: ApiFactory) -> None:
        # The enumeration is the foundation of the guarantee, so assert it is
        # reading the routing table rather than the schema: the probe router
        # is mounted and must appear, and so must the unversioned probes.
        async with api_factory(routers=[probe_router]) as client:
            operations = iter_operations(client)
            paths = {path for _, path in operations}

            assert "/healthz" in paths
            assert "/api/v1/auth/login" in paths
            assert "/api/v1/_probe/viewer" in paths
            assert ("POST", "/api/v1/_probe/admin-action") in operations

    async def test_every_public_operation_is_reachable_without_a_session(
        self, api_factory: ApiFactory
    ) -> None:
        # The allow-list must not drift the other way either: a path listed as
        # public but actually requiring a session is a stale entry, and a stale
        # allow-list is one nobody trusts to be accurate.
        async with api_factory() as client:
            for path in sorted(PUBLIC_PATHS):
                methods = [
                    method for method, candidate in iter_operations(client) if candidate == path
                ]
                assert methods, f"{path} is allow-listed but is not a route"
                for method in methods:
                    response = await client.request(method, path)
                    assert response.status_code != 401, (
                        f"{method} {path} is allow-listed as public but returned 401"
                    )

    async def test_public_allow_list_matches_the_expectation(self) -> None:
        # Widening the application's allow-list requires editing this literal.
        assert set(PUBLIC_PATHS) == EXPECTED_PUBLIC_PATHS

    async def test_every_public_path_documents_why(self) -> None:
        # An allow-list entry without a stated reason is one nobody can review.
        for path, reason in PUBLIC_PATHS.items():
            assert reason.strip(), f"{path} is public with no reason given"
            assert len(reason) > 40, f"{path}: the reason is too terse to be useful"

    async def test_a_route_added_without_protection_is_detected(
        self, api_factory: ApiFactory
    ) -> None:
        # Proves the regression test above can actually fail. A router mounted
        # outside the protected boundary — the exact mistake being guarded
        # against — must be visible as a non-401.
        leaky = APIRouter(prefix="/_leaky")

        @leaky.get("/oops")
        async def oops() -> dict[str, str]:
            return {"secret": "should have required a session"}

        async with api_factory(routers=[leaky]) as client:
            response = await client.get("/api/v1/_leaky/oops")

            assert response.status_code == 200
            assert "/api/v1/_leaky/oops" not in PUBLIC_PATHS

            offenders = [
                f"{method} {path}"
                for method, path in iter_operations(client)
                if path not in PUBLIC_PATHS
                and (await client.request(method, path)).status_code != 401
            ]
            assert "GET /api/v1/_leaky/oops" in offenders


class TestHiddenAndWebSocketRoutes:
    """The two blind spots the milestone 1.4 scan had.

    A route declared ``include_in_schema=False`` and **any** WebSocket route
    are both absent from the OpenAPI schema, so the schema-based scan treated
    them as covered while never testing them. These assert that the
    routing-table scan sees them, that the router-level dependency protects
    them, and — critically — that the detection actually fails on a
    deliberately unprotected one.
    """

    async def test_a_hidden_route_is_enumerated(self, api_factory: ApiFactory) -> None:
        hidden = APIRouter()

        @hidden.get("/_hidden", include_in_schema=False)
        async def hidden_endpoint(current: ViewerDep) -> dict[str, str]:
            return {"role": current.user.role.value}

        async with api_factory(routers=[hidden]) as client:
            operations = iter_operations(client)

            assert ("GET", "/api/v1/_hidden") in operations, (
                "a route hidden from the OpenAPI schema was not enumerated, so "
                "it would be exempt from the default-deny scan"
            )
            # And it is absent from the schema, which is the point.
            assert "/api/v1/_hidden" not in client.app_state.app.openapi()["paths"]  # type: ignore[attr-defined]

    async def test_a_hidden_route_still_requires_authentication(
        self, api_factory: ApiFactory
    ) -> None:
        hidden = APIRouter()

        @hidden.get("/_hidden", include_in_schema=False)
        async def hidden_endpoint(current: ViewerDep) -> dict[str, str]:
            return {"role": current.user.role.value}

        async with api_factory(routers=[hidden]) as client:
            assert (await client.get("/api/v1/_hidden")).status_code == 401

    async def test_a_websocket_route_is_enumerated(self, api_factory: ApiFactory) -> None:
        sockets = APIRouter()

        @sockets.websocket("/_ws")
        async def socket_endpoint(socket: WebSocket) -> None:
            await socket.accept()  # pragma: no cover - never reached unauthenticated

        async with api_factory(routers=[sockets]) as client:
            operations = iter_operations(client)

            assert (WEBSOCKET, "/api/v1/_ws") in operations, (
                "a WebSocket route was not enumerated; WebSocket routes never "
                "appear in the OpenAPI schema, so the schema-based scan could "
                "not have covered the agent and log-tail sockets of later phases"
            )
            assert "/api/v1/_ws" not in client.app_state.app.openapi()["paths"]  # type: ignore[attr-defined]

    async def test_a_websocket_on_the_protected_router_refuses_an_anonymous_handshake(
        self, api_factory: ApiFactory
    ) -> None:
        """A browser socket mounted the normal way is session-authenticated.

        This is the forward-looking half of the milestone: the log-tail socket
        of Phase 5 and the terminal socket of Phase 11 will be mounted exactly
        like this, and must not be open to an anonymous caller.
        """
        async with api_factory(routers=[protected_socket_router]) as client:
            messages = await attempt_websocket(client, "/api/v1/_ws-protected")

            assert not was_accepted(messages), (
                f"an anonymous WebSocket handshake was accepted: {messages}"
            )
            # Refused by an authentication decision, not by an internal error.
            # Starlette reports a pre-upgrade exception as a denial response.
            start = next(
                message
                for message in messages
                if message["type"] == "websocket.http.response.start"
            )
            assert start["status"] == 401

    async def test_an_authenticated_handshake_is_accepted(self, api_factory: ApiFactory) -> None:
        # The other direction: the protection must not make a legitimate
        # socket unusable. Without this, "refuses everything" would pass.
        async with api_factory(routers=[protected_socket_router]) as client:
            await seed_user(client, username="viewer", role=UserRole.VIEWER)
            await login_as(client, "viewer")

            messages = await attempt_websocket(
                client, "/api/v1/_ws-protected", headers={"Origin": "http://testserver"}
            )

            assert was_accepted(messages), messages

    async def test_a_websocket_handshake_requires_an_origin(self, api_factory: ApiFactory) -> None:
        """``Origin`` is mandatory on a WebSocket, unlike on HTTP.

        A handshake is not subject to CORS, so a browser will open a
        cross-origin socket and attach cookies; ``Origin`` is the only signal
        distinguishing a legitimate handshake from cross-site WebSocket
        hijacking. Browsers always send it, so requiring it costs a real
        client nothing — and the double-submit CSRF header cannot be sent on a
        handshake at all, so there is nothing else to check.
        """
        async with api_factory(routers=[protected_socket_router]) as client:
            await seed_user(client, username="viewer", role=UserRole.VIEWER)
            await login_as(client, "viewer")

            messages = await attempt_websocket(client, "/api/v1/_ws-protected")

            assert not was_accepted(messages), messages

    async def test_a_websocket_handshake_from_a_foreign_origin_is_refused(
        self, api_factory: ApiFactory
    ) -> None:
        async with api_factory(routers=[protected_socket_router]) as client:
            await seed_user(client, username="viewer", role=UserRole.VIEWER)
            await login_as(client, "viewer")

            messages = await attempt_websocket(
                client,
                "/api/v1/_ws-protected",
                headers={"Origin": "https://evil.example.com"},
            )

            assert not was_accepted(messages), messages

    async def test_an_unprotected_websocket_is_detected(self, api_factory: ApiFactory) -> None:
        """Proves the detection can fail.

        A WebSocket mounted outside the protected router — the mistake being
        guarded against — must be visible as an accepted anonymous handshake.
        Without this, "no unprotected sockets found" could mean the scan does
        nothing.
        """
        leaky = APIRouter()

        @leaky.websocket("/_ws-leaky")
        async def leaky_socket(socket: WebSocket) -> None:
            await socket.accept()
            await socket.close()

        async with api_factory(routers=[leaky]) as client:
            operations = iter_operations(client)
            assert (WEBSOCKET, "/api/v1/_ws-leaky") in operations

            messages = await attempt_websocket(client, "/api/v1/_ws-leaky")

            assert was_accepted(messages), (
                "a deliberately unprotected WebSocket was not accepted, so "
                "this check cannot distinguish protected from unprotected"
            )
            assert "/api/v1/_ws-leaky" not in PUBLIC_PATHS

    def test_the_probe_socket_uses_the_production_dependency(self) -> None:
        """The probe router must not drift from the real protected router.

        If the production router's authentication dependency were ever
        changed, a probe carrying the old one would keep passing while the
        real sockets went unprotected. Comparing the callables ties them
        together.
        """
        # importlib, not `import hcc.api.v1.router`: the package's __init__
        # re-exports the router *object* under the same name, which would
        # shadow the module.
        import importlib

        v1_router_module = importlib.import_module("hcc.api.v1.router")

        production = [
            dependency.dependency
            for dependency in v1_router_module._protected.dependencies  # noqa: SLF001
        ]
        probe = [dependency.dependency for dependency in protected_socket_router.dependencies]

        assert probe == production == [get_current_session]

    async def test_an_unprotected_hidden_route_is_detected(self, api_factory: ApiFactory) -> None:
        # The same self-check for the hidden-HTTP-route case.
        leaky = APIRouter()

        @leaky.get("/_hidden-leaky", include_in_schema=False)
        async def leaky_endpoint() -> dict[str, str]:
            return {"secret": "should have required a session"}

        async with api_factory(routers=[leaky]) as client:
            assert (await client.get("/api/v1/_hidden-leaky")).status_code == 200

            offenders = [
                f"{method} {path}"
                for method, path in iter_operations(client)
                if path not in PUBLIC_PATHS
                and method != WEBSOCKET
                and (await client.request(method, path)).status_code != 401
            ]
            assert "GET /api/v1/_hidden-leaky" in offenders


class TestDocumentationEndpoints:
    """FastAPI's own ``/docs`` and ``/openapi.json``.

    Public outside production, absent inside it. They were missing from the
    public inventory until milestone 1.5 because the schema-based scan could
    not see them — the schema does not describe the endpoints that serve the
    schema.
    """

    async def test_they_are_public_outside_production(self, api_factory: ApiFactory) -> None:
        async with api_factory(environment="test") as client:
            for path in ("/docs", "/openapi.json"):
                assert (await client.get(path)).status_code == 200, path

    async def test_they_are_in_the_public_inventory_with_a_reason(self) -> None:
        for path in ("/docs", "/openapi.json", "/docs/oauth2-redirect"):
            assert path in PUBLIC_PATHS
            assert "production" in PUBLIC_PATHS[path]

    async def test_they_do_not_exist_in_production(self, api_factory: ApiFactory) -> None:
        # The routes are not registered at all, so there is nothing to
        # authenticate — which is why they may be public elsewhere.
        async with api_factory(
            environment="production",
            public_origin="https://hcc.example.test",
        ) as client:
            paths = {path for _, path in iter_operations(client)}

            assert "/docs" not in paths
            assert "/openapi.json" not in paths
            assert (await client.get("/docs")).status_code == 404
            assert (await client.get("/openapi.json")).status_code == 404

    async def test_the_production_public_surface_is_only_the_product_paths(
        self, api_factory: ApiFactory
    ) -> None:
        # The claim docs/SECURITY.md makes, asserted where it is true.
        async with api_factory(
            environment="production",
            public_origin="https://hcc.example.test",
        ) as client:
            reachable = set()
            for method, path in iter_operations(client):
                if method == WEBSOCKET:
                    continue
                if (await client.request(method, path)).status_code != 401:
                    reachable.add(path)

            assert reachable == {
                "/healthz",
                "/readyz",
                "/api/v1/auth/login",
                "/api/v1/setup/status",
            }


# ---------------------------------------------------------------------------
# First-run setup status (milestone 1.6)
# ---------------------------------------------------------------------------


class TestSetupStatus:
    """The one public endpoint added in 1.6.

    It exists so the web client can distinguish "needs initializing" from
    "log in" before any credential exists. Being public, what it discloses is
    the whole question — so these assert the shape exactly, not merely that it
    works.
    """

    async def test_reports_not_initialized_on_a_fresh_installation(
        self, api_factory: ApiFactory
    ) -> None:
        async with api_factory() as client:
            response = await client.get(SETUP_STATUS)

            assert response.status_code == 200
            assert response.json() == {"initialized": False}

    async def test_reports_initialized_after_bootstrap(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            await bootstrap(client)

            response = await client.get(SETUP_STATUS)

            assert response.status_code == 200
            assert response.json() == {"initialized": True}

    async def test_is_reachable_without_a_session(self, api_factory: ApiFactory) -> None:
        # The whole point: the client has no credential at this stage.
        async with api_factory() as client:
            assert (await client.get(SETUP_STATUS)).status_code == 200

    async def test_discloses_exactly_one_field(self, api_factory: ApiFactory) -> None:
        """The disclosure budget, asserted rather than trusted.

        A later change that adds `initialized_at`, a user count or a username
        here would be a new unauthenticated disclosure, so the response shape
        is pinned.
        """
        async with api_factory() as client:
            await bootstrap(client, username="SomeAdministrator")

            response = await client.get(SETUP_STATUS)

            assert set(response.json()) == {"initialized"}
            rendered = response.text.lower()
            assert "someadministrator" not in rendered
            assert "initialized_at" not in rendered
            assert "count" not in rendered

    async def test_stays_initialized_after_every_user_is_deleted(
        self, api_factory: ApiFactory
    ) -> None:
        # It reads the installation row, not a user count — matching the
        # invariant bootstrap enforces (ADR-033). Reporting "setup required"
        # here would invite an operator to try a bootstrap that is refused.
        async with api_factory() as client:
            await bootstrap(client)
            await run_sql(client, "DELETE FROM users")

            assert (await client.get(SETUP_STATUS)).json() == {"initialized": True}

    async def test_is_listed_in_the_public_inventory_with_a_reason(self) -> None:
        assert SETUP_STATUS in PUBLIC_PATHS
        reason = PUBLIC_PATHS[SETUP_STATUS]
        assert "before any credential" in reason
        assert len(reason) > 40

    async def test_state_changing_methods_are_not_offered(self, api_factory: ApiFactory) -> None:
        # Public *and* mutating would be the setup endpoint ADR-033 refuses.
        async with api_factory() as client:
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                response = await client.request(method, SETUP_STATUS)
                assert response.status_code == 405, method

    async def test_no_other_setup_route_exists(self, api_factory: ApiFactory) -> None:
        # Guards against a future "convenience" endpoint appearing alongside
        # it: /setup/status is the only path under /setup.
        async with api_factory() as client:
            setup_paths = {path for _, path in iter_operations(client) if "/setup" in path}

            assert setup_paths == {SETUP_STATUS}


class TestPublicEndpointDisclosure:
    async def test_liveness_discloses_only_a_status(self, api_factory: ApiFactory) -> None:
        # Public, so it must not leak the release version: of no use to a
        # legitimate anonymous caller, of obvious use to someone matching the
        # deployment against known vulnerabilities.
        async with api_factory() as client:
            response = await client.get("/healthz")

            assert response.status_code == 200
            assert response.json() == {"status": "ok"}

    async def test_readiness_discloses_only_an_aggregate(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            response = await client.get("/readyz")

            assert response.status_code == 200
            assert response.json() == {"status": "ready"}

    async def test_public_endpoints_leak_no_system_detail(self, api_factory: ApiFactory) -> None:
        from hcc import __version__

        async with api_factory() as client:
            for path in ("/healthz", "/readyz"):
                body = (await client.get(path)).text
                assert __version__ not in body, path
                assert "postgres" not in body.lower(), path
                assert "environment" not in body.lower(), path
                assert "dependenc" not in body.lower(), path

    async def test_version_is_available_to_an_authenticated_caller(
        self, api_factory: ApiFactory
    ) -> None:
        # The information did not disappear, it moved behind a session.
        from hcc import __version__

        async with api_factory() as client:
            await seed_user(client, username="viewer", role=UserRole.VIEWER)
            await login_as(client, "viewer")

            body = (await client.get("/api/v1/info")).json()

            assert body["server_version"] == __version__
            assert body["environment"] == "test"


# ---------------------------------------------------------------------------
# Role authorization
# ---------------------------------------------------------------------------


@pytest.fixture
async def role_client(
    api_factory: ApiFactory,
) -> AsyncIterator[AsyncClient]:
    """A client with the probe router mounted and one user per role."""
    async with api_factory(routers=[probe_router]) as client:
        for role in UserRole:
            await seed_user(client, username=role.value, role=role)
        yield client


class TestRoleAuthorization:
    @pytest.mark.parametrize("role", list(UserRole))
    async def test_every_role_reaches_a_viewer_endpoint(
        self, role_client: AsyncClient, role: UserRole
    ) -> None:
        await login_as(role_client, role.value)

        response = await role_client.get("/api/v1/_probe/viewer")

        assert response.status_code == 200
        assert response.json()["role"] == role.value

    async def test_viewer_is_refused_an_operator_endpoint(self, role_client: AsyncClient) -> None:
        await login_as(role_client, "viewer")

        response = await role_client.get("/api/v1/_probe/operator")

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "insufficient_permissions"

    async def test_operator_reaches_an_operator_endpoint(self, role_client: AsyncClient) -> None:
        await login_as(role_client, "operator")

        assert (await role_client.get("/api/v1/_probe/operator")).status_code == 200

    async def test_admin_reaches_an_operator_endpoint(self, role_client: AsyncClient) -> None:
        # Roles are hierarchical: a higher role admits everything a lower one
        # does.
        await login_as(role_client, "admin")

        assert (await role_client.get("/api/v1/_probe/operator")).status_code == 200

    @pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.OPERATOR])
    async def test_non_admins_are_refused_an_admin_endpoint(
        self, role_client: AsyncClient, role: UserRole
    ) -> None:
        await login_as(role_client, role.value)

        response = await role_client.get("/api/v1/_probe/admin")

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "insufficient_permissions"

    async def test_admin_reaches_an_admin_endpoint(self, role_client: AsyncClient) -> None:
        await login_as(role_client, "admin")

        response = await role_client.get("/api/v1/_probe/admin")

        assert response.status_code == 200
        assert response.json()["role"] == "admin"

    async def test_denial_does_not_reveal_the_required_role(self, role_client: AsyncClient) -> None:
        # Enumerating what a higher role could do is information an attacker
        # can use. The specifics are in the server log, not the response.
        await login_as(role_client, "viewer")

        body = (await role_client.get("/api/v1/_probe/admin")).text

        assert "admin" not in body.lower()
        assert "operator" not in body.lower()
        assert "role" not in body.lower()

    async def test_denial_is_logged_with_the_specifics(self, role_client: AsyncClient) -> None:
        from structlog.testing import capture_logs

        await login_as(role_client, "viewer")

        with capture_logs() as events:
            await role_client.get("/api/v1/_probe/admin")

        denials = [e for e in events if e["event"] == "authorization_denied"]
        assert len(denials) == 1
        assert denials[0]["required_role"] == "admin"
        assert denials[0]["actual_role"] == "viewer"
        assert denials[0]["reason"] == "insufficient_role"


class TestAuthenticationVersusAuthorization:
    """The two checks must stay distinct and distinguishable."""

    async def test_no_session_on_a_role_gated_route_is_401_not_403(
        self, role_client: AsyncClient
    ) -> None:
        # 401 means "log in"; 403 means "you are not allowed". A client that
        # cannot tell them apart cannot react correctly.
        response = await role_client.get("/api/v1/_probe/admin")

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "not_authenticated"

    async def test_insufficient_role_is_403_not_401(self, role_client: AsyncClient) -> None:
        await login_as(role_client, "viewer")

        response = await role_client.get("/api/v1/_probe/admin")

        assert response.status_code == 403
        # Re-authenticating would not help, so the client must not be prompted
        # to log in again.
        assert "Basic" not in response.headers.get("www-authenticate", "")

    async def test_a_disabled_account_is_401_not_403(self, role_client: AsyncClient) -> None:
        # Deactivation invalidates the session itself, so it is an
        # authentication failure. `is_active` is re-checked on every request.
        await login_as(role_client, "admin")
        assert (await role_client.get("/api/v1/_probe/admin")).status_code == 200

        await run_sql(role_client, "UPDATE users SET is_active = false")

        response = await role_client.get("/api/v1/_probe/admin")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "not_authenticated"

    async def test_an_expired_session_is_401(self, role_client: AsyncClient) -> None:
        await login_as(role_client, "admin")

        await run_sql(
            role_client,
            "UPDATE sessions SET created_at = now() - interval '2 hours', "
            "expires_at = now() - interval '1 second'",
        )

        response = await role_client.get("/api/v1/_probe/admin")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "not_authenticated"

    async def test_a_revoked_session_is_401(self, role_client: AsyncClient) -> None:
        csrf = await login_as(role_client, "admin")
        await role_client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})

        response = await role_client.get("/api/v1/_probe/admin")
        assert response.status_code == 401

    async def test_authorization_runs_only_after_authentication(
        self, role_client: AsyncClient
    ) -> None:
        from structlog.testing import capture_logs

        # An unauthenticated request must not reach the role check at all —
        # otherwise a denial log would appear with no user to attribute it to.
        with capture_logs() as events:
            await role_client.get("/api/v1/_probe/admin")

        assert [e for e in events if e["event"] == "authorization_denied"] == []


class TestCsrfOnRoleGatedRoutes:
    async def test_state_changing_role_gated_route_requires_csrf(
        self, role_client: AsyncClient
    ) -> None:
        # Authorization must not displace the CSRF check: both apply.
        await login_as(role_client, "admin")

        response = await role_client.post("/api/v1/_probe/admin-action")

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "csrf_validation_failed"

    async def test_state_changing_role_gated_route_accepts_a_valid_token(
        self, role_client: AsyncClient
    ) -> None:
        csrf = await login_as(role_client, "admin")

        response = await role_client.post(
            "/api/v1/_probe/admin-action", headers={"X-CSRF-Token": csrf}
        )

        assert response.status_code == 200

    async def test_csrf_is_checked_even_when_the_role_is_insufficient(
        self, role_client: AsyncClient
    ) -> None:
        # Both checks live in the same dependency chain, so a forged
        # cross-site request from a low-privilege session is refused without
        # the role check being the only thing standing in the way.
        await login_as(role_client, "viewer")

        response = await role_client.post("/api/v1/_probe/admin-action")

        assert response.status_code == 403
        # CSRF is evaluated during session resolution, which precedes the role
        # check, so that is the reason reported.
        assert response.json()["error"]["code"] == "csrf_validation_failed"

    async def test_insufficient_role_refused_even_with_a_valid_csrf_token(
        self, role_client: AsyncClient
    ) -> None:
        csrf = await login_as(role_client, "viewer")

        response = await role_client.post(
            "/api/v1/_probe/admin-action", headers={"X-CSRF-Token": csrf}
        )

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "insufficient_permissions"


class TestSessionResolvedOnce:
    async def test_role_dependency_does_not_duplicate_the_session_lookup(
        self, role_client: AsyncClient
    ) -> None:
        # The role dependency depends on `get_current_session`, and the
        # endpoint receives the session from it, so FastAPI's per-request
        # dependency cache should resolve it once. Asserted through the
        # `session_created`/touch behaviour would be indirect; instead this
        # checks the observable consequence: one request, one session row,
        # and a successful response.
        await login_as(role_client, "admin")

        response = await role_client.get("/api/v1/_probe/admin")

        assert response.status_code == 200

        async with role_client.app_state.database.session_factory() as db:  # type: ignore[attr-defined]
            count = (await db.execute(text("SELECT count(*) FROM sessions"))).scalar_one()
        assert count == 1


class TestCapabilityPreparation:
    async def test_the_capability_axis_is_unavailable(self, api_factory: ApiFactory) -> None:
        # Recorded at the API level too: the application wires an Authorizer
        # with no resolver, so any capability check denies rather than being
        # approximated. The first real resolver arrives with per-host
        # configuration.
        async with api_factory() as client:
            authorizer = client.app_state.authorizer  # type: ignore[attr-defined]
            assert authorizer.supports_capabilities is False


class TestNoSecretLeakage:
    async def test_authorization_logs_carry_no_session_material(
        self, role_client: AsyncClient
    ) -> None:
        from structlog.testing import capture_logs

        csrf = await login_as(role_client, "viewer")
        token = role_client.cookies.get("hcc_session")
        assert token

        with capture_logs() as events:
            await role_client.get("/api/v1/_probe/admin")
            await role_client.post("/api/v1/_probe/admin-action", headers={"X-CSRF-Token": csrf})

        rendered = repr(events)
        assert token not in rendered
        assert csrf not in rendered
        assert PASSWORD not in rendered

    async def test_forbidden_response_carries_no_internal_detail(
        self, role_client: AsyncClient
    ) -> None:
        await login_as(role_client, "viewer")

        body = (await role_client.get("/api/v1/_probe/admin")).text

        assert "Traceback" not in body
        assert "InsufficientRoleError" not in body
        assert "hcc_local_dev" not in body


def test_probe_router_is_not_part_of_the_application() -> None:
    """The synthetic routes must never reach the product.

    They exist only in this module and are mounted by the test fixture, so a
    freshly built application must not advertise them.
    """
    from hcc.app import create_app
    from hcc.config import Settings

    app = create_app(
        Settings(
            environment="test",
            database_url="postgresql://u:p@localhost:5432/hcc",
        )
    )
    paths: dict[str, Any] = app.openapi()["paths"]

    assert not [path for path in paths if "_probe" in path or "_leaky" in path]
