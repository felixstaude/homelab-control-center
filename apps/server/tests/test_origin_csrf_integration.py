"""CSRF ``Origin`` validation across deployment topologies.

Milestone 1.5, and the regression suite for the verified HIGH-severity
defect: the allowed origin was derived from the ASGI request scheme, so behind
a TLS-terminating reverse proxy the application compared a browser's
``https://host`` against its own internal ``http://host`` and rejected every
state-changing request with a CSRF error that named the wrong cause.

The matrix below is deliberately explicit about topology, because the bug was
invisible in the one topology the tests covered (direct HTTP) and present in
the one the documentation recommends.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from hcc.models.user import User, UserRole

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager

    from httpx import AsyncClient

    ApiFactory = Callable[..., AbstractAsyncContextManager[AsyncClient]]

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"
USERNAME = "felix"
LOGIN = "/api/v1/auth/login"
LOGOUT = "/api/v1/auth/logout"

PUBLIC_ORIGIN = "https://hcc.example.com"
PROXY = "127.0.0.1"


async def seed_user(client: AsyncClient) -> None:
    hasher = client.app_state.password_hasher  # type: ignore[attr-defined]
    async with client.app_state.database.session_factory() as db:  # type: ignore[attr-defined]
        db.add(
            User(
                username=USERNAME,
                password_hash=hasher.hash(PASSWORD),
                role=UserRole.ADMIN,
                is_active=True,
            )
        )
        await db.commit()


async def login(client: AsyncClient, headers: dict[str, str] | None = None) -> str:
    """Log in and return the CSRF token."""
    response = await client.post(
        LOGIN, json={"username": USERNAME, "password": PASSWORD}, headers=headers
    )
    assert response.status_code == 200, response.text
    token: str = response.json()["csrf_token"]
    return token


async def mutate(
    client: AsyncClient, *, csrf: str | None = None, headers: dict[str, str] | None = None
) -> Any:
    """Issue a state-changing request — the path the Origin check guards."""
    combined = dict(headers or {})
    if csrf is not None:
        combined["X-CSRF-Token"] = csrf
    return await client.post(LOGOUT, headers=combined)


# ---------------------------------------------------------------------------
# Topology: direct, plain HTTP (local development)
# ---------------------------------------------------------------------------


class TestDirectHttpDevelopment:
    async def test_same_origin_request_succeeds(self, api_factory: ApiFactory) -> None:
        # The origin is derived from the request, so a direct deployment needs
        # no configuration at all.
        async with api_factory() as client:
            await seed_user(client)
            csrf = await login(client)

            response = await mutate(client, csrf=csrf, headers={"Origin": "http://testserver"})

            assert response.status_code == 200

    async def test_a_foreign_origin_is_refused(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            await seed_user(client)
            csrf = await login(client)

            response = await mutate(
                client, csrf=csrf, headers={"Origin": "https://evil.example.com"}
            )

            assert response.status_code == 403
            assert response.json()["error"]["code"] == "csrf_validation_failed"

    async def test_an_allow_listed_cors_origin_succeeds(self, api_factory: ApiFactory) -> None:
        # The cross-origin development case: the Vite dev server.
        async with api_factory(cors_origins=("http://localhost:5173",)) as client:
            await seed_user(client)
            csrf = await login(client)

            response = await mutate(client, csrf=csrf, headers={"Origin": "http://localhost:5173"})

            assert response.status_code == 200


# ---------------------------------------------------------------------------
# Topology: HTTPS reverse proxy, declared public origin
# ---------------------------------------------------------------------------


class TestBehindAnHttpsProxyWithPublicOrigin:
    """The recommended production configuration.

    ``HCC_PUBLIC_ORIGIN`` is authoritative, so the decision depends on nothing
    the client sends — not even a forwarded header.
    """

    async def test_the_browsers_https_origin_is_accepted(self, api_factory: ApiFactory) -> None:
        """The regression test for the defect.

        The application is serving plain HTTP internally — exactly what it
        sees behind a TLS-terminating proxy — while the browser sends an
        ``https://`` origin. Before milestone 1.5 this returned 403.
        """
        async with api_factory(public_origin=PUBLIC_ORIGIN) as client:
            await seed_user(client)
            csrf = await login(client)

            response = await mutate(client, csrf=csrf, headers={"Origin": PUBLIC_ORIGIN})

            assert response.status_code == 200, (
                f"state-changing request refused behind an HTTPS proxy: {response.text}"
            )

    async def test_a_foreign_origin_is_still_refused(self, api_factory: ApiFactory) -> None:
        async with api_factory(public_origin=PUBLIC_ORIGIN) as client:
            await seed_user(client)
            csrf = await login(client)

            response = await mutate(
                client, csrf=csrf, headers={"Origin": "https://evil.example.com"}
            )

            assert response.status_code == 403

    async def test_the_internal_origin_is_not_silently_also_allowed(
        self, api_factory: ApiFactory
    ) -> None:
        # An operator who has declared the public origin has also declared
        # that anything else is not it. Accepting the internal http:// view as
        # well would widen the check without being asked.
        async with api_factory(public_origin=PUBLIC_ORIGIN) as client:
            await seed_user(client)
            csrf = await login(client)

            response = await mutate(client, csrf=csrf, headers={"Origin": "http://testserver"})

            assert response.status_code == 403

    async def test_a_forged_forwarded_proto_changes_nothing(self, api_factory: ApiFactory) -> None:
        # With an explicit public origin the forwarded header is not consulted
        # at all, so it cannot be used to influence the decision.
        async with api_factory(public_origin=PUBLIC_ORIGIN) as client:
            await seed_user(client)
            csrf = await login(client)

            response = await mutate(
                client,
                csrf=csrf,
                headers={"Origin": "http://testserver", "X-Forwarded-Proto": "http"},
            )

            assert response.status_code == 403


# ---------------------------------------------------------------------------
# Topology: HTTPS reverse proxy, derived from a trusted forwarded header
# ---------------------------------------------------------------------------


class TestBehindATrustedProxyWithoutPublicOrigin:
    """The alternative configuration: believe the declared proxy's scheme."""

    async def test_forwarded_proto_from_a_trusted_proxy_is_believed(
        self, api_factory: ApiFactory
    ) -> None:
        async with api_factory(trusted_proxies=(PROXY,)) as client:
            await seed_user(client)
            csrf = await login(client)

            response = await mutate(
                client,
                csrf=csrf,
                headers={
                    "Origin": "https://testserver",
                    "X-Forwarded-Proto": "https",
                },
            )

            assert response.status_code == 200

    async def test_without_the_forwarded_header_the_transport_scheme_is_used(
        self, api_factory: ApiFactory
    ) -> None:
        async with api_factory(trusted_proxies=(PROXY,)) as client:
            await seed_user(client)
            csrf = await login(client)

            # No X-Forwarded-Proto, so the origin derives from http.
            assert (
                await mutate(client, csrf=csrf, headers={"Origin": "http://testserver"})
            ).status_code == 200

    async def test_a_spoofed_forwarded_proto_from_an_untrusted_peer_is_ignored(
        self, api_factory: ApiFactory
    ) -> None:
        """A caller must not be able to choose which origin is expected.

        The peer is not the declared proxy, so its ``X-Forwarded-Proto`` is
        not evidence. The expected origin therefore stays ``http://`` and the
        claimed ``https://`` origin is refused.
        """
        async with api_factory(client_address="203.0.113.77", trusted_proxies=(PROXY,)) as client:
            await seed_user(client)
            csrf = await login(client)

            response = await mutate(
                client,
                csrf=csrf,
                headers={
                    "Origin": "https://testserver",
                    "X-Forwarded-Proto": "https",
                },
            )

            assert response.status_code == 403

    async def test_a_forwarded_proto_is_ignored_when_no_proxy_is_declared(
        self, api_factory: ApiFactory
    ) -> None:
        async with api_factory(trusted_proxies=()) as client:
            await seed_user(client)
            csrf = await login(client)

            response = await mutate(
                client,
                csrf=csrf,
                headers={
                    "Origin": "https://testserver",
                    "X-Forwarded-Proto": "https",
                },
            )

            assert response.status_code == 403


# ---------------------------------------------------------------------------
# The Origin policy itself
# ---------------------------------------------------------------------------


class TestOriginPolicy:
    async def test_a_missing_origin_falls_back_to_the_csrf_token(
        self, api_factory: ApiFactory
    ) -> None:
        """The documented, deliberate gap.

        Non-browser clients — ``curl``, this test suite, a future CLI — send
        no ``Origin``, and browsers always send one for the cross-site
        requests this guards against. So an absent header is allowed through
        on the strength of the session-bound CSRF token alone.
        """
        async with api_factory(public_origin=PUBLIC_ORIGIN) as client:
            await seed_user(client)
            csrf = await login(client)

            response = await mutate(client, csrf=csrf)

            assert response.status_code == 200

    async def test_a_missing_origin_still_requires_the_csrf_token(
        self, api_factory: ApiFactory
    ) -> None:
        # The fallback is to the token, not to nothing.
        async with api_factory(public_origin=PUBLIC_ORIGIN) as client:
            await seed_user(client)
            await login(client)

            response = await mutate(client)

            assert response.status_code == 403
            assert response.json()["error"]["code"] == "csrf_validation_failed"

    @pytest.mark.parametrize(
        "origin",
        ["null", "not-an-origin", "https://", "https://host/path", "javascript://x"],
    )
    async def test_a_malformed_origin_is_refused(
        self, api_factory: ApiFactory, origin: str
    ) -> None:
        # A browser never produces these, so there is nothing to be
        # permissive about. `null` in particular is the opaque origin sent by
        # sandboxed and file:// contexts.
        async with api_factory(public_origin=PUBLIC_ORIGIN) as client:
            await seed_user(client)
            csrf = await login(client)

            response = await mutate(client, csrf=csrf, headers={"Origin": origin})

            assert response.status_code == 403

    async def test_an_equivalent_origin_spelling_is_accepted(self, api_factory: ApiFactory) -> None:
        # Both sides are canonicalised, so a default port and a trailing
        # difference in case compare equal rather than failing a string match.
        async with api_factory(public_origin=PUBLIC_ORIGIN) as client:
            await seed_user(client)
            csrf = await login(client)

            response = await mutate(
                client, csrf=csrf, headers={"Origin": "HTTPS://HCC.Example.com:443"}
            )

            assert response.status_code == 200

    async def test_both_checks_apply_to_a_state_changing_request(
        self, api_factory: ApiFactory
    ) -> None:
        # A correct origin does not excuse a missing token, and a correct
        # token does not excuse a wrong origin.
        async with api_factory(public_origin=PUBLIC_ORIGIN) as client:
            await seed_user(client)
            csrf = await login(client)

            # Right origin, no token.
            assert (await mutate(client, headers={"Origin": PUBLIC_ORIGIN})).status_code == 403
            # Right origin, wrong token.
            assert (
                await mutate(client, csrf="not-the-token", headers={"Origin": PUBLIC_ORIGIN})
            ).status_code == 403
            # Wrong origin, right token.
            assert (
                await mutate(client, csrf=csrf, headers={"Origin": "https://evil.test"})
            ).status_code == 403
            # Both right.
            assert (
                await mutate(client, csrf=csrf, headers={"Origin": PUBLIC_ORIGIN})
            ).status_code == 200

    async def test_safe_methods_need_neither(self, api_factory: ApiFactory) -> None:
        # Requiring a CSRF header on GET would break ordinary navigation, and
        # a GET that mutates state is a bug rather than a case to support.
        async with api_factory(public_origin=PUBLIC_ORIGIN) as client:
            await seed_user(client)
            await login(client)

            response = await client.get(
                "/api/v1/auth/me", headers={"Origin": "https://evil.example.com"}
            )

            assert response.status_code == 200

    async def test_an_unauthenticated_mutation_is_401_not_403(
        self, api_factory: ApiFactory
    ) -> None:
        # Authentication precedes CSRF: a caller with no session is told to
        # log in, not told its token is wrong.
        async with api_factory(public_origin=PUBLIC_ORIGIN) as client:
            response = await mutate(client, headers={"Origin": PUBLIC_ORIGIN})

            assert response.status_code == 401
            assert response.json()["error"]["code"] == "not_authenticated"
