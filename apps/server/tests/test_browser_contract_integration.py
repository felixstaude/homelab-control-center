"""The contract the web client depends on.

Milestone 1.6. The individual mechanisms — session issuance, CSRF, throttling,
Origin validation — are tested in their own modules. These assert the
*sequence* a browser performs, and the handful of details that only matter
because a browser is the client:

* the CSRF cookie must be readable by script, or double-submit is impossible;
* the session cookie must not be;
* the role must arrive as the string the client is typed against;
* the Vite dev proxy rewrites ``Host`` while the browser keeps its own
  ``Origin``, which is a configuration the backend has to accept.

Written from the client's point of view on purpose. A backend test that only
checks its own internals can be green while the browser is unable to use it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from hcc.models.user import UserRole
from hcc.security.bootstrap import bootstrap_administrator

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager

    from httpx import AsyncClient

    ApiFactory = Callable[..., AbstractAsyncContextManager[AsyncClient]]

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"
SETUP_STATUS = "/api/v1/setup/status"
LOGIN = "/api/v1/auth/login"
LOGOUT = "/api/v1/auth/logout"
ME = "/api/v1/auth/me"

#: The dev-server origin, as `vite.config.ts` and `.env.example` set it.
VITE_ORIGIN = "http://localhost:5173"


async def bootstrap(client: AsyncClient, *, username: str = "FelixAdmin") -> None:
    async with client.app_state.database.session_factory() as db:  # type: ignore[attr-defined]
        await bootstrap_administrator(
            db,
            username=username,
            password=PASSWORD,
            hasher=client.app_state.password_hasher,  # type: ignore[attr-defined]
        )


def set_cookie_header(response: Any, name: str) -> str:
    """The raw ``Set-Cookie`` line for one cookie.

    Read raw because the attributes — ``HttpOnly``, ``Secure``, ``SameSite``
    — are the thing under test, and httpx's jar does not expose them.
    """
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(f"{name}="):
            return str(header)
    raise AssertionError(f"no Set-Cookie for {name!r}")


class TestFirstRunSequence:
    """The journey a browser makes on a fresh installation."""

    async def test_setup_status_then_bootstrap_then_login(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            # 1. The client asks whether setup is needed. No credential yet.
            assert (await client.get(SETUP_STATUS)).json() == {"initialized": False}

            # 2. The operator runs the local command.
            await bootstrap(client)

            # 3. The client re-checks — no restart, no new connection.
            assert (await client.get(SETUP_STATUS)).json() == {"initialized": True}

            # 4. And signs in with the account that was just created.
            response = await client.post(
                LOGIN, json={"username": "felixadmin", "password": PASSWORD}
            )
            assert response.status_code == 200
            assert response.json()["role"] == "admin"

    async def test_login_is_refused_before_bootstrap(self, api_factory: ApiFactory) -> None:
        # The reason the status endpoint exists: without it the client would
        # have to present this as a credential problem.
        async with api_factory() as client:
            response = await client.post(LOGIN, json={"username": "admin", "password": PASSWORD})

            assert response.status_code == 401
            assert response.json()["error"]["code"] == "invalid_credentials"

    async def test_the_display_form_is_returned_but_either_case_logs_in(
        self, api_factory: ApiFactory
    ) -> None:
        # The client shows `username` in the account menu, so it must be the
        # display form; login accepts any casing (ADR-014).
        async with api_factory() as client:
            await bootstrap(client, username="FelixAdmin")

            for attempt in ("FelixAdmin", "felixadmin", "FELIXADMIN"):
                client.cookies.clear()
                response = await client.post(
                    LOGIN, json={"username": attempt, "password": PASSWORD}
                )
                assert response.status_code == 200, attempt
                assert response.json()["username"] == "FelixAdmin"


class TestCookieContract:
    """What the browser must be able to do with the cookies it is given."""

    async def test_the_csrf_cookie_is_readable_by_script(self, api_factory: ApiFactory) -> None:
        # Not HttpOnly, deliberately: the client has to read it to echo it in
        # a header, and double-submit is impossible otherwise (ADR-030).
        async with api_factory() as client:
            await bootstrap(client)
            response = await client.post(
                LOGIN, json={"username": "felixadmin", "password": PASSWORD}
            )

            csrf = set_cookie_header(response, "hcc_csrf")
            assert "HttpOnly" not in csrf

    async def test_the_session_cookie_is_not_readable_by_script(
        self, api_factory: ApiFactory
    ) -> None:
        # The difference between an XSS bug and a stolen session.
        async with api_factory() as client:
            await bootstrap(client)
            response = await client.post(
                LOGIN, json={"username": "felixadmin", "password": PASSWORD}
            )

            assert "HttpOnly" in set_cookie_header(response, "hcc_session")

    async def test_both_cookies_are_scoped_to_the_whole_site(self, api_factory: ApiFactory) -> None:
        # Path=/ so a request from any route carries them, and no Domain so
        # they are host-only and not shared with sibling subdomains.
        async with api_factory() as client:
            await bootstrap(client)
            response = await client.post(
                LOGIN, json={"username": "felixadmin", "password": PASSWORD}
            )

            for name in ("hcc_session", "hcc_csrf"):
                header = set_cookie_header(response, name)
                assert "Path=/" in header, name
                assert "Domain" not in header, name

    async def test_the_csrf_token_in_the_body_matches_the_cookie(
        self, api_factory: ApiFactory
    ) -> None:
        # The client reads the cookie rather than this field, but a client
        # that used either must get the same value.
        async with api_factory() as client:
            await bootstrap(client)
            response = await client.post(
                LOGIN, json={"username": "felixadmin", "password": PASSWORD}
            )

            assert response.json()["csrf_token"] == client.cookies["hcc_csrf"]

    async def test_logout_clears_both_cookies(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            await bootstrap(client)
            csrf = (
                await client.post(LOGIN, json={"username": "felixadmin", "password": PASSWORD})
            ).json()["csrf_token"]

            response = await client.post(LOGOUT, headers={"X-CSRF-Token": csrf})

            assert response.status_code == 200
            for name in ("hcc_session", "hcc_csrf"):
                # An expired Set-Cookie, so the browser drops it. The server
                # has already revoked the session regardless.
                assert 'Max-Age="0"' in set_cookie_header(
                    response, name
                ) or "Max-Age=0" in set_cookie_header(response, name)


class TestSessionResponseContract:
    """The shape the client is typed against."""

    @pytest.mark.parametrize(
        ("role", "expected"),
        [(UserRole.VIEWER, "viewer"), (UserRole.OPERATOR, "operator"), (UserRole.ADMIN, "admin")],
    )
    async def test_the_role_is_the_wire_string_not_the_enum_name(
        self, api_factory: ApiFactory, role: UserRole, expected: str
    ) -> None:
        # Typing the field as `UserRole` in 1.6 put the enum into the OpenAPI
        # schema; this asserts the wire format did not change with it. The
        # client's union is `'viewer' | 'operator' | 'admin'`, so `VIEWER`
        # would break it.
        from hcc.models.user import User

        async with api_factory() as client:
            await bootstrap(client, username="TheAdmin")
            hasher = client.app_state.password_hasher  # type: ignore[attr-defined]
            async with client.app_state.database.session_factory() as db:  # type: ignore[attr-defined]
                db.add(
                    User(
                        username="RoleUser",
                        password_hash=hasher.hash(PASSWORD),
                        role=role,
                    )
                )
                await db.commit()

            body = (
                await client.post(LOGIN, json={"username": "roleuser", "password": PASSWORD})
            ).json()

            assert body["role"] == expected

    async def test_me_returns_the_same_shape_as_login(self, api_factory: ApiFactory) -> None:
        # The client reuses one type for both.
        async with api_factory() as client:
            await bootstrap(client)
            login_body = (
                await client.post(LOGIN, json={"username": "felixadmin", "password": PASSWORD})
            ).json()

            me_body = (await client.get(ME)).json()

            assert set(login_body) == set(me_body)
            assert me_body["user_id"] == login_body["user_id"]
            assert me_body["role"] == login_body["role"]

    async def test_the_session_response_carries_no_credential_material(
        self, api_factory: ApiFactory
    ) -> None:
        async with api_factory() as client:
            await bootstrap(client)
            await client.post(LOGIN, json={"username": "felixadmin", "password": PASSWORD})

            rendered = (await client.get(ME)).text

            assert PASSWORD not in rendered
            assert "$argon2" not in rendered
            assert "password" not in rendered.lower()
            # The session token itself is never returned; only the CSRF token.
            assert client.cookies["hcc_session"] not in rendered


class TestCacheability:
    """Nothing per-session may be stored by a cache the operator does not own.

    The client sends ``cache: 'no-store'`` on every request, but that binds
    only the browser. A shared cache decides from the response, so the
    guarantee has to be stated server-side.
    """

    async def test_the_session_response_is_not_storable(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            await bootstrap(client)
            await client.post(LOGIN, json={"username": "felixadmin", "password": PASSWORD})

            response = await client.get(ME)

            assert response.status_code == 200
            # A shared cache storing this would serve one operator's identity
            # to the next person through the proxy.
            assert response.headers["cache-control"] == "no-store"

    @pytest.mark.parametrize("path", ["/healthz", "/readyz", SETUP_STATUS])
    async def test_public_endpoints_are_not_storable(
        self, api_factory: ApiFactory, path: str
    ) -> None:
        async with api_factory() as client:
            response = await client.get(path)

            # A cached readiness response is a readiness probe that lies, and a
            # cached setup status would strand a browser on the setup screen
            # after the installation was initialised.
            assert response.headers["cache-control"] == "no-store"

    async def test_an_error_response_is_not_storable(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            response = await client.get(ME)

            assert response.status_code == 401
            assert response.headers["cache-control"] == "no-store"

    async def test_a_login_response_is_not_storable(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            await bootstrap(client)

            response = await client.post(
                LOGIN, json={"username": "felixadmin", "password": PASSWORD}
            )

            # This one carries the CSRF token and the Set-Cookie pair.
            assert response.headers["cache-control"] == "no-store"


class TestCsrfFromABrowser:
    """The cookie-to-header round trip, as the client performs it."""

    async def test_a_mutation_succeeds_with_the_cookie_value_in_the_header(
        self, api_factory: ApiFactory
    ) -> None:
        async with api_factory() as client:
            await bootstrap(client)
            await client.post(LOGIN, json={"username": "felixadmin", "password": PASSWORD})

            # Exactly what the client does: read the cookie, send it back.
            response = await client.post(
                LOGOUT, headers={"X-CSRF-Token": client.cookies["hcc_csrf"]}
            )

            assert response.status_code == 200

    async def test_a_mutation_without_the_header_is_refused_distinguishably(
        self, api_factory: ApiFactory
    ) -> None:
        # The client maps this code to its own `csrf` kind, which must not be
        # treated as an expired session.
        async with api_factory() as client:
            await bootstrap(client)
            await client.post(LOGIN, json={"username": "felixadmin", "password": PASSWORD})

            response = await client.post(LOGOUT)

            assert response.status_code == 403
            assert response.json()["error"]["code"] == "csrf_validation_failed"

    async def test_a_stale_token_from_a_previous_session_is_refused(
        self, api_factory: ApiFactory
    ) -> None:
        # Why the client reads the cookie per request instead of caching it.
        async with api_factory() as client:
            await bootstrap(client)
            first = (
                await client.post(LOGIN, json={"username": "felixadmin", "password": PASSWORD})
            ).json()["csrf_token"]

            # A second login replaces the session, and the token with it.
            second = (
                await client.post(LOGIN, json={"username": "felixadmin", "password": PASSWORD})
            ).json()["csrf_token"]
            assert first != second

            response = await client.post(LOGOUT, headers={"X-CSRF-Token": first})

            assert response.status_code == 403


class TestViteDevProxyTopology:
    """The configuration a contributor actually runs.

    `vite.config.ts` proxies `/api` with `changeOrigin: true`, so the backend
    receives ``Host: 127.0.0.1:8000`` while the browser's ``Origin`` stays
    ``http://localhost:5173``. Those two never agree, which is precisely why
    `.env.example` lists the dev server in ``HCC_CORS_ORIGINS`` — and why this
    is worth a test rather than a comment.
    """

    async def test_a_mutation_from_the_dev_server_origin_is_accepted(
        self, api_factory: ApiFactory
    ) -> None:
        async with api_factory(cors_origins=(VITE_ORIGIN,)) as client:
            await bootstrap(client)
            csrf = (
                await client.post(LOGIN, json={"username": "felixadmin", "password": PASSWORD})
            ).json()["csrf_token"]

            response = await client.post(
                LOGOUT,
                headers={"X-CSRF-Token": csrf, "Origin": VITE_ORIGIN},
            )

            assert response.status_code == 200, (
                "the documented development topology was refused: the Vite "
                "proxy rewrites Host, so the browser Origin never matches the "
                "request's own host and must come from HCC_CORS_ORIGINS"
            )

    async def test_the_dev_origin_is_refused_when_not_configured(
        self, api_factory: ApiFactory
    ) -> None:
        # The other half: it is allowed because it was configured, not because
        # anything accepts any origin.
        async with api_factory(cors_origins=()) as client:
            await bootstrap(client)
            csrf = (
                await client.post(LOGIN, json={"username": "felixadmin", "password": PASSWORD})
            ).json()["csrf_token"]

            response = await client.post(
                LOGOUT,
                headers={"X-CSRF-Token": csrf, "Origin": VITE_ORIGIN},
            )

            assert response.status_code == 403

    async def test_setup_status_is_reachable_through_the_proxy_path(
        self, api_factory: ApiFactory
    ) -> None:
        # The proxy forwards `/api/*`, so the client's base path must sit under
        # it. A status endpoint outside `/api` would not be proxied at all.
        async with api_factory() as client:
            assert SETUP_STATUS.startswith("/api/")
            assert (await client.get(SETUP_STATUS)).status_code == 200


class TestProductionProxyTopology:
    """Same-origin behind a TLS-terminating proxy, the documented production
    deployment."""

    async def test_a_mutation_with_the_public_origin_is_accepted(
        self, api_factory: ApiFactory
    ) -> None:
        async with api_factory(public_origin="https://hcc.example.com") as client:
            await bootstrap(client)
            csrf = (
                await client.post(LOGIN, json={"username": "felixadmin", "password": PASSWORD})
            ).json()["csrf_token"]

            response = await client.post(
                LOGOUT,
                headers={"X-CSRF-Token": csrf, "Origin": "https://hcc.example.com"},
            )

            assert response.status_code == 200

    async def test_the_whole_journey_behind_a_proxy(self, api_factory: ApiFactory) -> None:
        # Setup check, login, identity, logout — every step carrying the
        # browser's https Origin while the app serves plain HTTP internally.
        origin = {"Origin": "https://hcc.example.com"}
        async with api_factory(public_origin="https://hcc.example.com") as client:
            assert (await client.get(SETUP_STATUS, headers=origin)).json() == {"initialized": False}

            await bootstrap(client)
            assert (await client.get(SETUP_STATUS, headers=origin)).json() == {"initialized": True}

            login = await client.post(
                LOGIN,
                json={"username": "felixadmin", "password": PASSWORD},
                headers=origin,
            )
            assert login.status_code == 200
            csrf = login.json()["csrf_token"]

            assert (await client.get(ME, headers=origin)).status_code == 200
            assert (
                await client.post(LOGOUT, headers={**origin, "X-CSRF-Token": csrf})
            ).status_code == 200
            # Revoked server-side, so the cleared cookie is not what ends it.
            assert (await client.get(ME, headers=origin)).status_code == 401


class TestFailureCodesTheClientBranchesOn:
    """Each code the client maps to a distinct kind must be produced."""

    async def test_no_session_is_401_not_authenticated(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            response = await client.get(ME)

            assert response.status_code == 401
            assert response.json()["error"]["code"] == "not_authenticated"

    async def test_bad_credentials_are_401_invalid_credentials(
        self, api_factory: ApiFactory
    ) -> None:
        async with api_factory() as client:
            await bootstrap(client)

            response = await client.post(
                LOGIN, json={"username": "felixadmin", "password": "wrong"}
            )

            assert response.status_code == 401
            assert response.json()["error"]["code"] == "invalid_credentials"

    async def test_throttling_is_429_with_retry_after(self, api_factory: ApiFactory) -> None:
        # The client renders the number of seconds, so the header has to be
        # there and has to be an integer.
        async with api_factory(
            login_rate_limit_per_address=1,
            login_rate_limit_per_address_username=1,
            login_rate_limit_per_username=1000,
        ) as client:
            await bootstrap(client)
            await client.post(LOGIN, json={"username": "felixadmin", "password": "wrong"})

            response = await client.post(
                LOGIN, json={"username": "felixadmin", "password": "wrong"}
            )

            assert response.status_code == 429
            assert response.json()["error"]["code"] == "rate_limited"
            assert int(response.headers["retry-after"]) >= 1

    async def test_a_malformed_body_is_422_with_field_detail(self, api_factory: ApiFactory) -> None:
        # The client surfaces `details.fields`, so the shape matters.
        async with api_factory() as client:
            response = await client.post(LOGIN, json={"username": "only-a-username"})

            assert response.status_code == 422
            body = response.json()["error"]
            assert body["code"] == "validation_failed"
            assert isinstance(body["details"]["fields"], list)
            assert body["details"]["fields"][0]["location"].startswith("body")

    async def test_every_error_carries_a_request_id(self, api_factory: ApiFactory) -> None:
        # The client shows it on a 500 so an operator can find the log line.
        async with api_factory() as client:
            body = (await client.get(ME)).json()

            assert body["error"]["request_id"]
