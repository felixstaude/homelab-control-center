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

from typing import TYPE_CHECKING, Any

import pytest
from fastapi import APIRouter
from sqlalchemy import text

# Runtime imports, not TYPE_CHECKING: these annotate FastAPI route
# handlers below, and FastAPI resolves endpoint annotations at runtime to
# build its dependency graph. Same constraint as hcc/api/deps.py.
from hcc.api.deps import AdminDep, OperatorDep, ViewerDep  # noqa: TC001
from hcc.app import PUBLIC_PATHS
from hcc.models import User, UserRole

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from contextlib import AbstractAsyncContextManager

    from httpx import AsyncClient

    ApiFactory = Callable[..., AbstractAsyncContextManager[AsyncClient]]

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"
LOGIN = "/api/v1/auth/login"

#: The expected public surface, written out literally.
#:
#: Duplicating `hcc.app.PUBLIC_PATHS` is the point: widening the allow-list in
#: the application then requires editing this test too, which makes it a
#: reviewed decision rather than a diff nobody notices.
EXPECTED_PUBLIC_PATHS = {
    "/healthz",
    "/readyz",
    "/api/v1/auth/login",
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


async def login_as(client: AsyncClient, username: str) -> str:
    """Log in and return the CSRF token."""
    response = await client.post(LOGIN, json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    token: str = response.json()["csrf_token"]
    return token


def iter_operations(client: AsyncClient) -> list[tuple[str, str]]:
    """Every (method, path) the running application advertises.

    Read from the OpenAPI schema rather than by walking ``app.routes``: this
    FastAPI version nests included routers behind private internals, and the
    schema is both stable and the thing clients actually see.
    """
    app = client.app_state.app  # type: ignore[attr-defined]
    return sorted(
        (method.upper(), path)
        for path, methods in app.openapi()["paths"].items()
        for method in methods
    )


# ---------------------------------------------------------------------------
# Default-deny
# ---------------------------------------------------------------------------


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
