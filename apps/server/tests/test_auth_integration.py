"""Authentication endpoint integration tests against real PostgreSQL.

Exercises the full stack — middleware, dependencies, exception handlers,
cookies, the database — because the properties under test are properties of
the assembled system. A unit test of the login service could not show that the
session cookie is actually `HttpOnly`, or that `/auth/me` really stops working
after logout.

Session ageing is simulated by rewriting ``expires_at`` or ``last_seen_at``
directly rather than by sleeping, so the suite stays fast and cannot flake on
a loaded runner.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, cast

import pytest
from sqlalchemy import CursorResult, text

from hcc.models import User, UserRole

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from contextlib import AbstractAsyncContextManager

    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    #: The `api_factory` fixture: call it to get a client context manager.
    ApiFactory = Callable[..., AbstractAsyncContextManager[AsyncClient]]

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"
LOGIN = "/api/v1/auth/login"
LOGOUT = "/api/v1/auth/logout"
ME = "/api/v1/auth/me"


@asynccontextmanager
async def app_session(client: AsyncClient) -> AsyncIterator[AsyncSession]:
    """Open a database session against the app's own engine.

    Uses the session factory directly rather than iterating
    ``Database.session()``. Breaking or returning out of an ``async for`` over
    an async generator leaves it unclosed, so its cleanup runs at garbage
    collection — which leaked connections between tests and surfaced as an
    "Exception ignored in deallocator" warning.
    """
    state = client.app_state  # type: ignore[attr-defined]
    async with state.database.session_factory() as db:
        yield db


async def seed_user(
    client: AsyncClient,
    *,
    username: str = "Felix",
    password: str = PASSWORD,
    role: UserRole = UserRole.ADMIN,
    is_active: bool = True,
) -> User:
    """Create a user directly, since there is no registration endpoint."""
    hasher = client.app_state.password_hasher  # type: ignore[attr-defined]
    async with app_session(client) as db:
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


async def run_sql(client: AsyncClient, statement: str, **params: Any) -> Any:
    """Execute raw SQL against the app's database, returning a buffered result.

    The rows are materialised before the transaction commits: committing
    closes the cursor, so a caller that held the result and read it afterwards
    would get ``ResourceClosedError``.
    """
    async with app_session(client) as db:
        result = await db.execute(text(statement), params)
        # `returns_rows` is a CursorResult attribute; AsyncSession.execute is
        # typed as returning the more general Result.
        cursor = cast("CursorResult[Any]", result)
        buffered = result.freeze() if cursor.returns_rows else None
        await db.commit()
        return buffered() if buffered is not None else result


async def login(client: AsyncClient, *, username: str = "Felix", password: str = PASSWORD) -> Any:
    return await client.post(LOGIN, json={"username": username, "password": password})


def set_cookie_header(response: Any, name: str) -> str:
    """Return the raw Set-Cookie header for one cookie.

    Read from the raw header rather than the parsed cookie jar, because the
    attributes — `HttpOnly`, `Secure`, `SameSite` — are the thing under test
    and httpx's jar does not expose them.
    """
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(f"{name}="):
            return str(header)
    raise AssertionError(f"no Set-Cookie for {name!r} in {response.headers!r}")


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


class TestLogin:
    async def test_valid_credentials_succeed(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            user = await seed_user(client)

            response = await login(client)

            assert response.status_code == 200
            body = response.json()
            assert body["user_id"] == str(user.id)
            assert body["username"] == "Felix"
            assert body["role"] == "admin"
            assert body["is_active"] is True
            assert body["csrf_token"]

    async def test_username_is_matched_case_insensitively(self, api_factory: ApiFactory) -> None:
        # Reuses the canonical-username normalisation from milestone 1.1, so
        # login behaves the same way uniqueness does.
        async with api_factory() as client:
            await seed_user(client, username="Felix")

            for variant in ("felix", "FELIX", "FeLiX", "  Felix  "):
                client.cookies.clear()
                response = await login(client, username=variant)
                assert response.status_code == 200, variant
                # The display form is returned, not the variant typed.
                assert response.json()["username"] == "Felix"

    async def test_wrong_password_is_refused(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            await seed_user(client)

            response = await login(client, password="not the password")

            assert response.status_code == 401
            assert response.json()["error"]["code"] == "invalid_credentials"

    async def test_unknown_username_is_refused(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            await seed_user(client)

            response = await login(client, username="nobody-at-all")

            assert response.status_code == 401
            assert response.json()["error"]["code"] == "invalid_credentials"

    async def test_inactive_user_is_refused(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            await seed_user(client, username="dormant", is_active=False)

            response = await login(client, username="dormant")

            assert response.status_code == 401
            assert response.json()["error"]["code"] == "invalid_credentials"

    async def test_every_failure_is_indistinguishable(self, api_factory: ApiFactory) -> None:
        # The anti-enumeration property: a client must not be able to tell a
        # wrong password from an unknown user from a disabled account. If this
        # ever diverges, the login endpoint becomes a username oracle.
        async with api_factory() as client:
            await seed_user(client, username="active")
            await seed_user(client, username="dormant", is_active=False)

            responses = [
                await login(client, password="wrong", username="active"),
                await login(client, username="does-not-exist"),
                await login(client, username="dormant"),
                await login(client, username="dormant", password="wrong"),
            ]

            bodies = []
            for response in responses:
                assert response.status_code == 401
                body = response.json()["error"]
                # request_id differs by design; everything else must not.
                body.pop("request_id", None)
                bodies.append(body)

            assert all(body == bodies[0] for body in bodies), bodies

    async def test_no_session_is_created_for_a_failed_login(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            await seed_user(client)

            await login(client, password="wrong")

            result = await run_sql(client, "SELECT count(*) FROM sessions")
            assert result.scalar_one() == 0

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"username": "Felix"},
            {"password": PASSWORD},
            {"username": "", "password": PASSWORD},
            {"username": "Felix", "password": ""},
            {"username": "Felix", "password": PASSWORD, "extra": "field"},
            {"username": 123, "password": PASSWORD},
            {"username": ["a"], "password": PASSWORD},
            {"username": "x" * 5000, "password": PASSWORD},
            {"username": "Felix", "password": "x" * 5000},
        ],
    )
    async def test_malformed_payload_is_rejected_as_validation_error(
        self, api_factory: ApiFactory, payload: dict[str, Any]
    ) -> None:
        # 422 rather than 401: the request never reached authentication, so
        # calling it an authentication failure would be misleading. It also
        # reveals nothing about any account.
        async with api_factory() as client:
            await seed_user(client)

            response = await client.post(LOGIN, json=payload)

            assert response.status_code == 422
            assert response.json()["error"]["code"] == "validation_failed"

    async def test_non_json_body_is_rejected(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            response = await client.post(
                LOGIN, content=b"not json", headers={"content-type": "application/json"}
            )
            assert response.status_code == 422

    async def test_login_response_leaks_no_credential_material(
        self, api_factory: ApiFactory
    ) -> None:
        async with api_factory() as client:
            await seed_user(client)

            response = await login(client)
            text_body = response.text

            assert PASSWORD not in text_body
            assert "argon2" not in text_body
            assert "password_hash" not in text_body
            # The session token belongs in the cookie, never in the body: a
            # body is logged and cached far more readily than a Set-Cookie.
            session_cookie = client.cookies.get("hcc_session")
            assert session_cookie
            assert session_cookie not in text_body


# ---------------------------------------------------------------------------
# Cookies
# ---------------------------------------------------------------------------


class TestCookies:
    async def test_session_cookie_is_httponly(self, api_factory: ApiFactory) -> None:
        # The difference between an XSS bug and a stolen session.
        async with api_factory() as client:
            await seed_user(client)
            response = await login(client)

            header = set_cookie_header(response, "hcc_session")
            assert "HttpOnly" in header

    async def test_csrf_cookie_is_not_httponly(self, api_factory: ApiFactory) -> None:
        # Readable on purpose: the client has to echo it in a header.
        async with api_factory() as client:
            await seed_user(client)
            response = await login(client)

            header = set_cookie_header(response, "hcc_csrf")
            assert "HttpOnly" not in header

    async def test_cookies_are_samesite_lax(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            await seed_user(client)
            response = await login(client)

            for name in ("hcc_session", "hcc_csrf"):
                assert "SameSite=lax" in set_cookie_header(response, name)

    async def test_cookies_are_path_scoped_and_host_only(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            await seed_user(client)
            response = await login(client)

            for name in ("hcc_session", "hcc_csrf"):
                header = set_cookie_header(response, name)
                assert "Path=/" in header
                # No Domain attribute: the cookie must not be shared with
                # sibling subdomains, which on a homelab often host other
                # services.
                assert "Domain=" not in header

    async def test_cookies_are_not_secure_in_development(self, api_factory: ApiFactory) -> None:
        # Plain-HTTP local development must work without anyone disabling a
        # security flag by hand.
        async with api_factory(environment="development") as client:
            await seed_user(client)
            response = await login(client)

            assert "Secure" not in set_cookie_header(response, "hcc_session")

    async def test_cookies_are_secure_in_production(self, api_factory: ApiFactory) -> None:
        async with api_factory(environment="production") as client:
            await seed_user(client)
            response = await login(client)

            for name in ("hcc_session", "hcc_csrf"):
                assert "Secure" in set_cookie_header(response, name)

    async def test_cookie_max_age_matches_the_session_lifetime(
        self, api_factory: ApiFactory
    ) -> None:
        async with api_factory(session_lifetime_seconds=900) as client:
            await seed_user(client)
            response = await login(client)

            assert "Max-Age=900" in set_cookie_header(response, "hcc_session")

    async def test_session_token_is_never_stored_in_plaintext(
        self, api_factory: ApiFactory
    ) -> None:
        # The central storage property: a database dump must not yield
        # replayable sessions.
        async with api_factory() as client:
            await seed_user(client)
            await login(client)
            token = client.cookies.get("hcc_session")
            assert token

            result = await run_sql(client, "SELECT token_hash FROM sessions")
            stored = result.scalar_one()

            assert stored != token
            assert token not in stored
            assert len(stored) == 64  # hex sha256


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


class TestSessions:
    async def test_session_row_is_created_with_expected_fields(
        self, api_factory: ApiFactory
    ) -> None:
        async with api_factory() as client:
            user = await seed_user(client)

            await login(client)

            result = await run_sql(
                client,
                "SELECT user_id, revoked_at, created_ip, created_user_agent, "
                "expires_at > created_at AS sane_expiry FROM sessions",
            )
            row = result.one()
            assert row.user_id == user.id
            assert row.revoked_at is None
            assert row.created_ip  # recorded for the session list and audit
            assert row.sane_expiry is True

    async def test_me_returns_the_authenticated_identity(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            user = await seed_user(client, role=UserRole.OPERATOR)
            await login(client)

            response = await client.get(ME)

            assert response.status_code == 200
            body = response.json()
            assert body["user_id"] == str(user.id)
            assert body["role"] == "operator"
            assert body["csrf_token"]

    async def test_me_requires_a_session(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            response = await client.get(ME)

            assert response.status_code == 401
            assert response.json()["error"]["code"] == "not_authenticated"

    async def test_session_persists_across_requests(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            await seed_user(client)
            await login(client)

            for _ in range(5):
                assert (await client.get(ME)).status_code == 200

            # One session, not one per request.
            result = await run_sql(client, "SELECT count(*) FROM sessions")
            assert result.scalar_one() == 1

    async def test_each_login_creates_a_distinct_session_token(
        self, api_factory: ApiFactory
    ) -> None:
        async with api_factory() as client:
            await seed_user(client)

            await login(client)
            first = client.cookies.get("hcc_session")
            client.cookies.clear()
            await login(client)
            second = client.cookies.get("hcc_session")

            assert first != second

    @pytest.mark.parametrize(
        "cookie",
        [
            "",
            "not-a-token",
            "a" * 43,  # well-formed but never issued
            "a" * 500,
            "../../etc/passwd",
            "' OR 1=1 --",
        ],
    )
    async def test_invalid_session_cookie_is_refused(
        self, api_factory: ApiFactory, cookie: str
    ) -> None:
        # Identical refusal for malformed and for well-formed-but-unknown, so
        # a caller cannot probe whether a captured token is live.
        async with api_factory() as client:
            await seed_user(client)
            client.cookies.set("hcc_session", cookie)

            response = await client.get(ME)

            assert response.status_code == 401
            assert response.json()["error"]["code"] == "not_authenticated"

    async def test_expired_session_is_refused(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            await seed_user(client)
            await login(client)
            assert (await client.get(ME)).status_code == 200

            # Age the row rather than sleeping. `created_at` moves too: the
            # ck_sessions_expires_after_created constraint forbids an expiry
            # earlier than creation, which is exactly the nonsense it exists
            # to catch.
            await run_sql(
                client,
                "UPDATE sessions SET created_at = now() - interval '2 hours', "
                "expires_at = now() - interval '1 second'",
            )

            assert (await client.get(ME)).status_code == 401

    async def test_idle_session_is_refused(self, api_factory: ApiFactory) -> None:
        async with api_factory(session_idle_timeout_seconds=60) as client:
            await seed_user(client)
            await login(client)
            assert (await client.get(ME)).status_code == 200

            await run_sql(client, "UPDATE sessions SET last_seen_at = now() - interval '2 hours'")

            assert (await client.get(ME)).status_code == 401

    async def test_idle_timeout_can_be_disabled(self, api_factory: ApiFactory) -> None:
        async with api_factory(session_idle_timeout_seconds=0) as client:
            await seed_user(client)
            await login(client)

            await run_sql(client, "UPDATE sessions SET last_seen_at = now() - interval '30 days'")

            # Only the absolute lifetime applies now, and that has not passed.
            assert (await client.get(ME)).status_code == 200

    async def test_activity_advances_last_seen_at(self, api_factory: ApiFactory) -> None:
        # Backdated past the touch interval but well inside the idle timeout,
        # so the session is still valid and the touch is what is under test.
        # (Backdating by a full hour would trip the default 3600s idle
        # timeout, and the request would 401 without touching anything — which
        # is why the status code is asserted here rather than assumed.)
        async with api_factory(
            session_touch_interval_seconds=1, session_idle_timeout_seconds=3600
        ) as client:
            await seed_user(client)
            await login(client)

            await run_sql(client, "UPDATE sessions SET last_seen_at = now() - interval '5 minutes'")
            before = (await run_sql(client, "SELECT last_seen_at FROM sessions")).scalar_one()

            assert (await client.get(ME)).status_code == 200

            after = (await run_sql(client, "SELECT last_seen_at FROM sessions")).scalar_one()
            assert after > before

    async def test_touch_extends_the_idle_window(self, api_factory: ApiFactory) -> None:
        # The behaviour the touch exists for: an actively used session must
        # not hit the idle timeout. Without the commit in `_touch` the update
        # was rolled back and this failed.
        async with api_factory(
            session_touch_interval_seconds=1, session_idle_timeout_seconds=600
        ) as client:
            await seed_user(client)
            await login(client)

            # Nearly idle-expired, then used.
            await run_sql(client, "UPDATE sessions SET last_seen_at = now() - interval '9 minutes'")
            assert (await client.get(ME)).status_code == 200

            # The touch reset the idle clock, so a further 9 minutes of
            # inactivity is again survivable rather than cumulative.
            await run_sql(
                client, "UPDATE sessions SET last_seen_at = last_seen_at - interval '9 minutes'"
            )
            assert (await client.get(ME)).status_code == 200

    async def test_last_seen_at_is_not_written_on_every_request(
        self, api_factory: ApiFactory
    ) -> None:
        # The throttle that keeps a read-mostly dashboard from becoming
        # write-heavy.
        async with api_factory(session_touch_interval_seconds=3600) as client:
            await seed_user(client)
            await login(client)

            before = (await run_sql(client, "SELECT last_seen_at FROM sessions")).scalar_one()
            for _ in range(3):
                assert (await client.get(ME)).status_code == 200
            after = (await run_sql(client, "SELECT last_seen_at FROM sessions")).scalar_one()

            assert after == before

    async def test_deactivating_a_user_invalidates_their_sessions_immediately(
        self, api_factory: ApiFactory
    ) -> None:
        # `is_active` is re-checked on every request rather than trusted from
        # login time, which is what makes deactivation a usable control.
        async with api_factory() as client:
            await seed_user(client)
            await login(client)
            assert (await client.get(ME)).status_code == 200

            await run_sql(client, "UPDATE users SET is_active = false")

            assert (await client.get(ME)).status_code == 401

    async def test_deleting_a_user_cascades_to_sessions(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            await seed_user(client)
            await login(client)

            await run_sql(client, "DELETE FROM users")

            result = await run_sql(client, "SELECT count(*) FROM sessions")
            assert result.scalar_one() == 0


# ---------------------------------------------------------------------------
# Session fixation
# ---------------------------------------------------------------------------


class TestSessionFixation:
    async def test_a_preset_cookie_is_not_adopted(self, api_factory: ApiFactory) -> None:
        # The classic fixation attack: plant a token in the victim's browser,
        # wait for them to log in, then use it. The server generates its own
        # token and never adopts a client-supplied one.
        async with api_factory() as client:
            await seed_user(client)
            planted = "p" * 43
            client.cookies.set("hcc_session", planted, domain="testserver.local")

            response = await login(client)

            # Read from the raw Set-Cookie header: the jar would raise
            # CookieConflict with two cookies of the same name.
            issued = set_cookie_header(response, "hcc_session")
            assert planted not in issued

    async def test_logging_in_again_revokes_the_previous_session(
        self, api_factory: ApiFactory
    ) -> None:
        # A token planted *and already valid* — for instance one the attacker
        # obtained legitimately — must not survive the victim's login.
        async with api_factory() as client:
            await seed_user(client)

            await login(client)
            first_token = client.cookies.get("hcc_session")
            assert first_token

            await login(client)
            second_token = client.cookies.get("hcc_session")
            assert second_token != first_token

            # The old token is dead.
            client.cookies.set("hcc_session", first_token)
            assert (await client.get(ME)).status_code == 401

            result = await run_sql(
                client, "SELECT count(*) FROM sessions WHERE revoked_at IS NOT NULL"
            )
            assert result.scalar_one() == 1


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


class TestLogout:
    async def test_logout_revokes_the_session_server_side(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            await seed_user(client)
            csrf = (await login(client)).json()["csrf_token"]

            response = await client.post(LOGOUT, headers={"X-CSRF-Token": csrf})

            assert response.status_code == 200
            assert response.json()["status"] == "logged_out"

            result = await run_sql(client, "SELECT revoked_at FROM sessions")
            assert result.scalar_one() is not None

    async def test_session_stops_working_after_logout(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            await seed_user(client)
            csrf = (await login(client)).json()["csrf_token"]
            token = client.cookies.get("hcc_session")

            await client.post(LOGOUT, headers={"X-CSRF-Token": csrf})

            # Even replaying the exact cookie the server cleared.
            client.cookies.set("hcc_session", token or "")
            assert (await client.get(ME)).status_code == 401

    async def test_logout_clears_both_cookies(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            await seed_user(client)
            csrf = (await login(client)).json()["csrf_token"]

            response = await client.post(LOGOUT, headers={"X-CSRF-Token": csrf})

            cleared = {header.split("=")[0] for header in response.headers.get_list("set-cookie")}
            assert {"hcc_session", "hcc_csrf"} <= cleared
            assert not client.cookies.get("hcc_session")

    async def test_logout_requires_a_session(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            response = await client.post(LOGOUT, headers={"X-CSRF-Token": "x" * 43})

            assert response.status_code == 401

    async def test_double_logout_is_handled(self, api_factory: ApiFactory) -> None:
        # A normal thing for a client to do; it must not 500.
        async with api_factory() as client:
            await seed_user(client)
            csrf = (await login(client)).json()["csrf_token"]
            token = client.cookies.get("hcc_session")

            assert (await client.post(LOGOUT, headers={"X-CSRF-Token": csrf})).status_code == 200

            client.cookies.set("hcc_session", token or "")
            second = await client.post(LOGOUT, headers={"X-CSRF-Token": csrf})
            # The session is already revoked, so it is no longer authenticated.
            assert second.status_code == 401

    async def test_logout_only_ends_the_calling_session(self, api_factory: ApiFactory) -> None:
        # Two independent clients, same user: logging one out must not log out
        # the other.
        async with api_factory() as first, api_factory() as second:
            await seed_user(first)
            csrf_first = (await login(first)).json()["csrf_token"]
            await login(second)

            await first.post(LOGOUT, headers={"X-CSRF-Token": csrf_first})

            assert (await first.get(ME)).status_code == 401
            assert (await second.get(ME)).status_code == 200


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


class TestCsrf:
    async def test_state_changing_request_without_a_token_is_refused(
        self, api_factory: ApiFactory
    ) -> None:
        async with api_factory() as client:
            await seed_user(client)
            await login(client)

            response = await client.post(LOGOUT)

            assert response.status_code == 403
            assert response.json()["error"]["code"] == "csrf_validation_failed"

    @pytest.mark.parametrize("token", ["", "wrong", "a" * 43, "x", "null", "undefined"])
    async def test_wrong_csrf_token_is_refused(self, api_factory: ApiFactory, token: str) -> None:
        async with api_factory() as client:
            await seed_user(client)
            await login(client)

            response = await client.post(LOGOUT, headers={"X-CSRF-Token": token})

            assert response.status_code == 403

    async def test_another_sessions_csrf_token_is_refused(self, api_factory: ApiFactory) -> None:
        # The property that makes this a synchronizer token rather than a bare
        # double-submit: the token is bound to the server-side session, so one
        # session's token cannot authorise another's request.
        async with api_factory() as first, api_factory() as second:
            await seed_user(first)
            await login(first)
            other_csrf = (await login(second)).json()["csrf_token"]

            response = await first.post(LOGOUT, headers={"X-CSRF-Token": other_csrf})

            assert response.status_code == 403

    async def test_csrf_cookie_alone_is_not_sufficient(self, api_factory: ApiFactory) -> None:
        # A bare double-submit scheme would accept this, since cookie and
        # header would match. Here the header is still required, and a
        # cross-site attacker cannot set a header.
        async with api_factory() as client:
            await seed_user(client)
            await login(client)

            # The cookie is present and correct; the header is absent.
            assert client.cookies.get("hcc_csrf")
            response = await client.post(LOGOUT)

            assert response.status_code == 403

    async def test_a_forged_csrf_cookie_does_not_help(self, api_factory: ApiFactory) -> None:
        # Models an attacker who can write cookies for the domain — a
        # realistic risk where several services share a parent domain. They
        # can set both cookie and header to a value they chose, and it is
        # still refused, because the comparison is against the session row.
        async with api_factory() as client:
            await seed_user(client)
            await login(client)

            forged = "f" * 43
            client.cookies.set("hcc_csrf", forged)
            response = await client.post(LOGOUT, headers={"X-CSRF-Token": forged})

            assert response.status_code == 403

    async def test_correct_token_is_accepted(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            await seed_user(client)
            csrf = (await login(client)).json()["csrf_token"]

            response = await client.post(LOGOUT, headers={"X-CSRF-Token": csrf})

            assert response.status_code == 200

    async def test_token_from_me_is_usable(self, api_factory: ApiFactory) -> None:
        # How a client recovers its CSRF token after a page reload.
        async with api_factory() as client:
            await seed_user(client)
            await login(client)

            csrf = (await client.get(ME)).json()["csrf_token"]
            response = await client.post(LOGOUT, headers={"X-CSRF-Token": csrf})

            assert response.status_code == 200

    async def test_safe_methods_need_no_csrf_token(self, api_factory: ApiFactory) -> None:
        # Requiring one on GET would break ordinary navigation.
        async with api_factory() as client:
            await seed_user(client)
            await login(client)

            assert (await client.get(ME)).status_code == 200

    async def test_login_itself_needs_no_csrf_token(self, api_factory: ApiFactory) -> None:
        # There is no session yet, so there is no token to present. Login is
        # protected by credentials and by the rate limiter instead.
        async with api_factory() as client:
            await seed_user(client)

            assert (await login(client)).status_code == 200

    async def test_csrf_failure_does_not_revoke_the_session(self, api_factory: ApiFactory) -> None:
        # A failed CSRF check means "this request was not deliberate", not
        # "this session is compromised". Logging the user out would turn a
        # cross-site nuisance into a denial of service.
        async with api_factory() as client:
            await seed_user(client)
            await login(client)

            assert (await client.post(LOGOUT)).status_code == 403
            assert (await client.get(ME)).status_code == 200


class TestOriginValidation:
    async def test_matching_origin_is_accepted(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            await seed_user(client)
            csrf = (await login(client)).json()["csrf_token"]

            response = await client.post(
                LOGOUT,
                headers={"X-CSRF-Token": csrf, "Origin": "http://testserver"},
            )
            assert response.status_code == 200

    @pytest.mark.parametrize(
        "origin",
        [
            "https://evil.example.com",
            "http://testserver.evil.com",
            "null",
            "http://localhost:9999",
        ],
    )
    async def test_foreign_origin_is_refused(self, api_factory: ApiFactory, origin: str) -> None:
        async with api_factory() as client:
            await seed_user(client)
            csrf = (await login(client)).json()["csrf_token"]

            response = await client.post(LOGOUT, headers={"X-CSRF-Token": csrf, "Origin": origin})

            assert response.status_code == 403
            assert response.json()["error"]["code"] == "csrf_validation_failed"

    async def test_configured_cors_origin_is_accepted(self, api_factory: ApiFactory) -> None:
        # The development arrangement: the Vite dev server's origin is allowed
        # explicitly.
        async with api_factory(cors_origins=("http://localhost:5173",)) as client:
            await seed_user(client)
            csrf = (await login(client)).json()["csrf_token"]

            response = await client.post(
                LOGOUT,
                headers={"X-CSRF-Token": csrf, "Origin": "http://localhost:5173"},
            )
            assert response.status_code == 200

    async def test_absent_origin_falls_back_to_the_csrf_token(
        self, api_factory: ApiFactory
    ) -> None:
        # A deliberate gap, documented in hcc/api/v1/auth.py: non-browser
        # clients send no Origin, and browsers always do for the cross-site
        # requests this guards against.
        async with api_factory() as client:
            await seed_user(client)
            csrf = (await login(client)).json()["csrf_token"]

            response = await client.post(LOGOUT, headers={"X-CSRF-Token": csrf})

            assert response.status_code == 200

    async def test_absent_origin_still_requires_the_csrf_token(
        self, api_factory: ApiFactory
    ) -> None:
        async with api_factory() as client:
            await seed_user(client)
            await login(client)

            assert (await client.post(LOGOUT)).status_code == 403


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class TestRateLimiting:
    async def test_repeated_failures_are_throttled(self, api_factory: ApiFactory) -> None:
        async with api_factory(
            login_rate_limit_per_address=3, login_rate_limit_per_username=1000
        ) as client:
            await seed_user(client)

            statuses = [(await login(client, password="wrong")).status_code for _ in range(5)]

            assert statuses[:3] == [401, 401, 401]
            assert statuses[3:] == [429, 429]

    async def test_throttled_response_carries_retry_after(self, api_factory: ApiFactory) -> None:
        async with api_factory(login_rate_limit_per_address=1) as client:
            await seed_user(client)
            await login(client, password="wrong")

            response = await login(client, password="wrong")

            assert response.status_code == 429
            assert response.json()["error"]["code"] == "rate_limited"
            assert int(response.headers["retry-after"]) >= 1

    async def test_throttling_precedes_password_verification(self, api_factory: ApiFactory) -> None:
        # The ordering is the security property: a limiter consulted after the
        # 64 MiB Argon2 verification would still let an attacker exhaust
        # memory. Checked by confirming a throttled request is refused even
        # when the username does not exist, so no verification could have been
        # needed — and that the hasher's semaphore was never contended.
        async with api_factory(login_rate_limit_per_address=2) as client:
            await seed_user(client)

            for _ in range(2):
                await login(client, username="unknown-user")

            response = await login(client, username="unknown-user")
            assert response.status_code == 429

            # Correct credentials are refused too: the budget is spent.
            assert (await login(client)).status_code == 429

    async def test_throttling_is_per_username(self, api_factory: ApiFactory) -> None:
        async with api_factory(
            login_rate_limit_per_address=1000, login_rate_limit_per_username=2
        ) as client:
            await seed_user(client, username="target")
            await seed_user(client, username="bystander")

            for _ in range(2):
                await login(client, username="target", password="wrong")

            assert (await login(client, username="target")).status_code == 429
            # A different account is unaffected.
            assert (await login(client, username="bystander")).status_code == 200

    async def test_username_budget_is_case_insensitive(self, api_factory: ApiFactory) -> None:
        # Otherwise an attacker gets a fresh budget per casing variant.
        async with api_factory(
            login_rate_limit_per_address=1000, login_rate_limit_per_username=2
        ) as client:
            await seed_user(client, username="target")

            await login(client, username="target", password="wrong")
            await login(client, username="TARGET", password="wrong")

            assert (await login(client, username="TaRgEt")).status_code == 429

    async def test_successful_login_clears_the_budget(self, api_factory: ApiFactory) -> None:
        # A user who mistyped a few times must not be left throttled once they
        # get it right.
        async with api_factory(login_rate_limit_per_address=5) as client:
            await seed_user(client)

            for _ in range(3):
                await login(client, password="wrong")

            assert (await login(client)).status_code == 200

            # The budget was reset, so several more attempts are available.
            client.cookies.clear()
            assert (await login(client, password="wrong")).status_code == 401

    async def test_malformed_username_is_still_throttled(self, api_factory: ApiFactory) -> None:
        # Input that fails normalisation must not be a free path around the
        # limiter.
        async with api_factory(login_rate_limit_per_address=2) as client:
            await seed_user(client)

            for _ in range(2):
                await login(client, username="ab")  # too short to normalise

            assert (await login(client, username="ab")).status_code == 429


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


class TestConcurrency:
    async def test_concurrent_logins_all_succeed_with_distinct_sessions(
        self, api_factory: ApiFactory
    ) -> None:
        async with api_factory() as client:
            await seed_user(client)

            responses = await asyncio.gather(
                *(
                    client.post(LOGIN, json={"username": "Felix", "password": PASSWORD})
                    for _ in range(6)
                )
            )

            assert all(response.status_code == 200 for response in responses)

            result = await run_sql(client, "SELECT count(DISTINCT token_hash) FROM sessions")
            # Six logins, six distinct tokens; the last one replaced the
            # others' cookie but each row exists.
            assert result.scalar_one() == 6

    async def test_concurrent_failed_logins_do_not_error(self, api_factory: ApiFactory) -> None:
        # Exercises the hasher's concurrency semaphore under contention: more
        # simultaneous verifications than the cap, all of which must complete
        # rather than erroring or deadlocking.
        async with api_factory(
            password_hash_max_concurrency=2, login_rate_limit_per_address=1000
        ) as client:
            await seed_user(client)

            responses = await asyncio.gather(
                *(
                    client.post(LOGIN, json={"username": "Felix", "password": "wrong"})
                    for _ in range(8)
                )
            )

            assert {response.status_code for response in responses} == {401}

    async def test_concurrent_requests_on_one_session(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            await seed_user(client)
            await login(client)

            responses = await asyncio.gather(*(client.get(ME) for _ in range(10)))

            assert all(response.status_code == 200 for response in responses)


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------


class TestSecurityHeaders:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("x-content-type-options", "nosniff"),
            ("x-frame-options", "DENY"),
            ("referrer-policy", "strict-origin-when-cross-origin"),
        ],
    )
    async def test_headers_are_present(
        self, api_factory: ApiFactory, header: str, expected: str
    ) -> None:
        async with api_factory() as client:
            response = await client.get("/api/v1/info")
            assert response.headers[header] == expected

    async def test_headers_are_present_on_error_responses(self, api_factory: ApiFactory) -> None:
        # An error response is still a response a browser renders.
        async with api_factory() as client:
            response = await client.get(ME)

            assert response.status_code == 401
            assert response.headers["x-content-type-options"] == "nosniff"

    async def test_csp_is_absent_in_development(self, api_factory: ApiFactory) -> None:
        # A policy strict enough to be worth having would break the Vite dev
        # server; one weakened for development is one nobody trusts.
        async with api_factory(environment="development") as client:
            response = await client.get("/api/v1/info")
            assert "content-security-policy" not in response.headers

    async def test_csp_is_present_in_production(self, api_factory: ApiFactory) -> None:
        async with api_factory(environment="production") as client:
            response = await client.get("/healthz")

            policy = response.headers["content-security-policy"]
            assert "default-src 'self'" in policy
            assert "frame-ancestors 'none'" in policy
            assert "object-src 'none'" in policy
            # Inline scripts stay forbidden even though inline styles are
            # permitted for the design system's custom properties.
            assert "'unsafe-inline'" not in policy.split("script-src")[1].split(";")[0]

    async def test_hsts_is_not_set_by_the_application(self, api_factory: ApiFactory) -> None:
        # HSTS belongs to the TLS-terminating proxy, which knows whether HTTPS
        # is actually in use.
        async with api_factory(environment="production") as client:
            response = await client.get("/healthz")
            assert "strict-transport-security" not in response.headers

    async def test_401_does_not_trigger_a_browser_credential_dialog(
        self, api_factory: ApiFactory
    ) -> None:
        # A bare `WWW-Authenticate: Basic` would pop a native dialog over the
        # SPA.
        async with api_factory() as client:
            response = await client.get(ME)

            assert "Basic" not in response.headers.get("www-authenticate", "")


# ---------------------------------------------------------------------------
# Secret leakage
# ---------------------------------------------------------------------------


class TestNoSecretLeakage:
    async def test_password_never_appears_in_logs(self, api_factory: ApiFactory) -> None:
        from structlog.testing import capture_logs

        secret = "Sup3rSecret-Passphrase-X7"
        async with api_factory() as client:
            await seed_user(client, password=secret)

            with capture_logs() as events:
                await login(client, password=secret)
                await login(client, password="wrong-attempt-value")
                await client.get(ME)

            rendered = repr(events)
            assert secret not in rendered
            assert "wrong-attempt-value" not in rendered

    async def test_session_token_never_appears_in_logs(self, api_factory: ApiFactory) -> None:
        from structlog.testing import capture_logs

        async with api_factory() as client:
            await seed_user(client)
            await login(client)
            token = client.cookies.get("hcc_session")
            assert token

            with capture_logs() as events:
                await client.get(ME)
                await client.post(LOGOUT)

            rendered = repr(events)
            assert token not in rendered

    async def test_csrf_token_never_appears_in_logs(self, api_factory: ApiFactory) -> None:
        from structlog.testing import capture_logs

        async with api_factory() as client:
            await seed_user(client)
            csrf = (await login(client)).json()["csrf_token"]

            with capture_logs() as events:
                # A refused request logs that a token was presented, never its
                # value.
                await client.post(LOGOUT, headers={"X-CSRF-Token": "wrong-value-here"})

            rendered = repr(events)
            assert csrf not in rendered
            assert "wrong-value-here" not in rendered

    async def test_error_bodies_carry_no_internal_detail(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            await seed_user(client)

            for response in (
                await login(client, password="wrong"),
                await client.get(ME),
                await client.post(LOGOUT),
            ):
                body = response.text
                assert "argon2" not in body
                assert "Traceback" not in body
                assert "sqlalchemy" not in body.lower()
                assert "hcc_local_dev" not in body
