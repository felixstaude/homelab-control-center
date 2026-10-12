"""Self-service account endpoints, against real PostgreSQL.

Milestone 1.7. Password change and own-session management.

Two properties here need a real database and a real HTTP stack rather than a
unit test, and they are the two most likely to be got wrong:

* **Session rotation.** The password change revokes every session and issues a
  replacement, so the assertion that matters is that the cookie the client
  sent with *that request* no longer works while the one it received does.
  That is a statement about cookies and session rows together.
* **Concurrency.** Two simultaneous changes must not overwrite each other, and
  the row lock that prevents it is a PostgreSQL feature.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from httpx import ASGITransport, AsyncClient

from hcc.models.user import UserRole

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from contextlib import AbstractAsyncContextManager

    from hcc.models.user import User

    ApiFactory = Callable[..., AbstractAsyncContextManager[AsyncClient]]
    SeedUser = Callable[..., Awaitable[User]]
    SignIn = Callable[..., Awaitable[str]]
    ReadSql = Callable[..., Awaitable[Any]]

pytestmark = pytest.mark.integration

ACCOUNT = "/api/v1/account"
PASSWORD = "correct horse battery staple"
NEW_PASSWORD = "a different sufficiently long passphrase"
ME = "/api/v1/auth/me"


def csrf(token: str) -> dict[str, str]:
    return {"X-CSRF-Token": token}


# ---------------------------------------------------------------------------
# Account details
# ---------------------------------------------------------------------------


class TestAccountDetails:
    @pytest.mark.parametrize("role", list(UserRole))
    async def test_every_role_may_read_its_own_account(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        role: UserRole,
    ) -> None:
        # No role gate: these operations are scoped to the caller by
        # construction, so a Viewer must reach them.
        async with api_factory() as client:
            await seed_user(client, username="person", role=role, display_name="A Person")
            await sign_in(client, "person")

            response = await client.get(ACCOUNT)

            assert response.status_code == 200
            body = response.json()
            assert body["username"] == "person"
            assert body["display_name"] == "A Person"
            assert body["role"] == role.value

    async def test_last_login_is_populated_by_the_current_sign_in(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="person")
            await sign_in(client, "person")

            assert (await client.get(ACCOUNT)).json()["last_login_at"] is not None

    async def test_the_response_carries_no_credential_material(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="person")
            await sign_in(client, "person")

            response = await client.get(ACCOUNT)

            assert set(response.json()) == {
                "user_id",
                "username",
                "display_name",
                "role",
                "is_active",
                "created_at",
                "last_login_at",
            }
            assert "$argon2" not in response.text
            # No CSRF token either: that belongs to the authentication
            # handshake on /auth/me, not to an account description.
            assert "csrf" not in response.text.lower()

    async def test_an_anonymous_caller_is_refused(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            assert (await client.get(ACCOUNT)).status_code == 401


# ---------------------------------------------------------------------------
# Own sessions
# ---------------------------------------------------------------------------


class TestOwnSessions:
    async def test_lists_only_the_callers_sessions(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        """Object-level authorization, from the data side.

        Another account's sessions must be invisible. There is no field by
        which a caller could ask for them, which is the design — this asserts
        the design holds rather than that the filter is spelled correctly.
        """
        async with api_factory() as client:
            await seed_user(client, username="mine")
            await seed_user(client, username="theirs")

            await sign_in(client, "theirs")
            client.cookies.clear()
            await sign_in(client, "mine")
            await sign_in(client, "mine")

            body = (await client.get(f"{ACCOUNT}/sessions")).json()

            # Two sign-ins for "mine", but the second revoked the first
            # (session fixation protection), so one is live.
            assert len(body["items"]) == 1
            assert body["items"][0]["is_current"] is True

    async def test_identifies_the_current_session(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="person")
            await sign_in(client, "person")
            first_cookies = dict(client.cookies)

            # A second, independent client reusing the app: a second genuine
            # session for the same account.
            client.cookies.clear()
            await sign_in(client, "person")
            second_session = (await client.get(f"{ACCOUNT}/sessions")).json()

            assert second_session["current_session_id"] is not None
            current = [item for item in second_session["items"] if item["is_current"]]
            assert len(current) == 1
            assert current[0]["session_id"] == second_session["current_session_id"]
            assert first_cookies  # the first login did happen

    async def test_reports_timestamps_and_no_token(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        read_sql: ReadSql,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="person")
            await sign_in(client, "person")
            token = client.cookies.get("hcc_session")
            assert token

            response = await client.get(f"{ACCOUNT}/sessions")
            item = response.json()["items"][0]

            assert set(item) == {
                "session_id",
                "is_current",
                "created_at",
                "last_seen_at",
                "expires_at",
                "created_ip",
                "user_agent",
            }
            # Neither the token nor its hash appears. The id is a separate
            # uuid4 primary key and authenticates nothing.
            assert token not in response.text
            hashes = await read_sql(client, "SELECT token_hash FROM sessions")
            assert hashes[0][0] not in response.text

    async def test_the_session_id_is_not_derived_from_the_token(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        read_sql: ReadSql,
    ) -> None:
        # The claim that makes exposing the id safe, asserted rather than
        # assumed: the id is the row's primary key, unrelated to the
        # credential.
        async with api_factory() as client:
            await seed_user(client, username="person")
            await sign_in(client, "person")
            token = client.cookies.get("hcc_session")
            assert token

            rows = await read_sql(client, "SELECT id::text, token_hash FROM sessions")
            session_id, token_hash = rows[0]

            assert session_id not in token
            assert session_id not in token_hash
            assert token_hash not in session_id

    async def test_an_expired_session_is_not_listed(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="person")
            await sign_in(client, "person")
            current = (await client.get(f"{ACCOUNT}/sessions")).json()["current_session_id"]

            async with client.app_state.database.session_factory() as db:  # type: ignore[attr-defined]
                from sqlalchemy import text as sql_text

                await db.execute(
                    sql_text(
                        "INSERT INTO sessions "
                        "(id, user_id, token_hash, csrf_token, created_at, expires_at, "
                        " last_seen_at) "
                        "SELECT gen_random_uuid(), user_id, repeat('a', 64), repeat('b', 43), "
                        "       now() - interval '2 days', now() - interval '1 day', now() "
                        "FROM sessions LIMIT 1"
                    )
                )
                await db.commit()

            body = (await client.get(f"{ACCOUNT}/sessions")).json()

            assert [item["session_id"] for item in body["items"]] == [current]

    async def test_revoking_another_session_ends_it(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        api_settings_factory: Callable[..., Any],
    ) -> None:
        """Two real sessions, one revoked from the other.

        A second ``AsyncClient`` against the same app, so there are two
        genuinely independent cookie jars — which is what "another device"
        means and what a single client cannot represent.
        """
        async with api_factory() as client:
            await seed_user(client, username="person")

            # Session A, on a second client sharing the same app.
            transport = ASGITransport(app=client.app_state.app)  # type: ignore[attr-defined]
            async with AsyncClient(transport=transport, base_url="http://testserver") as other:
                response = await other.post(
                    "/api/v1/auth/login",
                    json={"username": "person", "password": PASSWORD},
                )
                assert response.status_code == 200
                other_session_id = (await other.get(f"{ACCOUNT}/sessions")).json()[
                    "current_session_id"
                ]

                # Session B revokes session A.
                token = await sign_in(client, "person")
                revoke = await client.post(
                    f"{ACCOUNT}/sessions/{other_session_id}/revoke", headers=csrf(token)
                )

                assert revoke.status_code == 200
                assert revoke.json()["sessions_revoked"] == 1
                # And A really is dead.
                assert (await other.get(ME)).status_code == 401
                # While B still works.
                assert (await client.get(ME)).status_code == 200

    async def test_revoking_someone_elses_session_is_a_404(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        """An insecure direct object reference, closed.

        A guessed or learned session id belonging to another account must not
        be revocable, and the refusal must be indistinguishable from "no such
        session" so the endpoint cannot be used to discover whose ids are
        live.
        """
        async with api_factory() as client:
            await seed_user(client, username="victim")
            await seed_user(client, username="attacker")

            await sign_in(client, "victim")
            victim_session = (await client.get(f"{ACCOUNT}/sessions")).json()["current_session_id"]
            victim_cookies = dict(client.cookies)

            client.cookies.clear()
            token = await sign_in(client, "attacker")
            response = await client.post(
                f"{ACCOUNT}/sessions/{victim_session}/revoke", headers=csrf(token)
            )

            assert response.status_code == 404

            # The victim's session is untouched.
            client.cookies.clear()
            for name, value in victim_cookies.items():
                client.cookies.set(name, value)
            assert (await client.get(ME)).status_code == 200

    async def test_an_unknown_session_id_is_the_same_404(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="person")
            token = await sign_in(client, "person")

            response = await client.post(
                f"{ACCOUNT}/sessions/00000000-0000-4000-8000-000000000000/revoke",
                headers=csrf(token),
            )

            assert response.status_code == 404

    async def test_revoking_the_current_session_is_a_sign_out(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="person")
            token = await sign_in(client, "person")
            current = (await client.get(f"{ACCOUNT}/sessions")).json()["current_session_id"]

            response = await client.post(
                f"{ACCOUNT}/sessions/{current}/revoke", headers=csrf(token)
            )

            assert response.status_code == 200
            assert (await client.get(ME)).status_code == 401

    async def test_revoke_others_keeps_the_current_session(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="person")

            transport = ASGITransport(app=client.app_state.app)  # type: ignore[attr-defined]
            async with AsyncClient(transport=transport, base_url="http://testserver") as other:
                await other.post(
                    "/api/v1/auth/login",
                    json={"username": "person", "password": PASSWORD},
                )
                token = await sign_in(client, "person")

                response = await client.post(
                    f"{ACCOUNT}/sessions/revoke-others", headers=csrf(token)
                )

                assert response.status_code == 200
                assert response.json()["sessions_revoked"] == 1
                assert (await other.get(ME)).status_code == 401
                assert (await client.get(ME)).status_code == 200

    async def test_revoke_others_with_nowhere_else_reports_zero(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # A success rather than a 404: "sign out everywhere else" when there is
        # nowhere else is already satisfied.
        async with api_factory() as client:
            await seed_user(client, username="person")
            token = await sign_in(client, "person")

            response = await client.post(f"{ACCOUNT}/sessions/revoke-others", headers=csrf(token))

            assert response.status_code == 200
            assert response.json()["sessions_revoked"] == 0

    async def test_revoke_others_is_not_captured_as_a_session_id(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # Route-ordering regression: `revoke-others` is declared before the
        # `{session_id}` route, so the literal must not be matched as an id.
        async with api_factory() as client:
            await seed_user(client, username="person")
            token = await sign_in(client, "person")

            response = await client.post(f"{ACCOUNT}/sessions/revoke-others", headers=csrf(token))

            assert response.status_code == 200
            assert "sessions_revoked" in response.json()

    @pytest.mark.parametrize(
        "path",
        [
            f"{ACCOUNT}/sessions/revoke-others",
            f"{ACCOUNT}/sessions/00000000-0000-4000-8000-000000000000/revoke",
        ],
    )
    async def test_revocation_requires_the_csrf_header(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        path: str,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="person")
            await sign_in(client, "person")

            response = await client.post(path)

            assert response.status_code == 403
            assert response.json()["error"]["code"] == "csrf_validation_failed"

    async def test_session_revocation_survives_a_restart(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        """Revocation is a database row, not process memory.

        The control plane is stateless between requests (ADR-028), so a
        revoked session must stay revoked across a restart. Simulated by
        building a second application against the same database — which is
        what a restart is, from the database's point of view.
        """
        async with api_factory() as first:
            await seed_user(first, username="person")
            token = await sign_in(first, "person")
            current = (await first.get(f"{ACCOUNT}/sessions")).json()["current_session_id"]
            cookies = dict(first.cookies)
            await first.post(f"{ACCOUNT}/sessions/{current}/revoke", headers=csrf(token))

        async with api_factory() as second:
            for name, value in cookies.items():
                second.cookies.set(name, value)
            assert (await second.get(ME)).status_code == 401


# ---------------------------------------------------------------------------
# Password change
# ---------------------------------------------------------------------------


class TestPasswordChange:
    async def test_the_new_password_works_and_the_old_one_does_not(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="person")
            token = await sign_in(client, "person")

            response = await client.post(
                f"{ACCOUNT}/password",
                json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
                headers=csrf(token),
            )
            assert response.status_code == 200, response.text

            client.cookies.clear()
            old = await client.post(
                "/api/v1/auth/login", json={"username": "person", "password": PASSWORD}
            )
            new = await client.post(
                "/api/v1/auth/login", json={"username": "person", "password": NEW_PASSWORD}
            )

            assert old.status_code == 401
            assert new.status_code == 200

    async def test_the_stored_hash_is_replaced(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        read_sql: ReadSql,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="person")
            before = (await read_sql(client, "SELECT password_hash FROM users"))[0][0]
            token = await sign_in(client, "person")

            await client.post(
                f"{ACCOUNT}/password",
                json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
                headers=csrf(token),
            )

            after = (await read_sql(client, "SELECT password_hash FROM users"))[0][0]
            assert after != before
            assert after.startswith("$argon2id$")

    async def test_the_session_is_rotated(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        """ADR-040, asserted on the wire.

        The cookie sent with the change request must stop working, and the one
        returned by it must work — so the user stays signed in while the token
        they held before the credential event is retired.
        """
        async with api_factory() as client:
            await seed_user(client, username="person")
            token = await sign_in(client, "person")
            before = client.cookies.get("hcc_session")

            response = await client.post(
                f"{ACCOUNT}/password",
                json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
                headers=csrf(token),
            )
            after = client.cookies.get("hcc_session")

            assert response.status_code == 200
            assert after is not None
            assert after != before

            # The client, now holding the replacement, is still signed in.
            assert (await client.get(ME)).status_code == 200

            # The pre-change token is refused.
            client.cookies.clear()
            assert before is not None
            client.cookies.set("hcc_session", before)
            assert (await client.get(ME)).status_code == 401

    async def test_the_csrf_token_is_rotated_with_the_session(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # The new session row has a new CSRF token, so the readable cookie has
        # to be reissued or the client's next mutation would present a stale
        # one and be refused.
        async with api_factory() as client:
            await seed_user(client, username="person")
            token = await sign_in(client, "person")

            await client.post(
                f"{ACCOUNT}/password",
                json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
                headers=csrf(token),
            )
            rotated = client.cookies.get("hcc_csrf")

            assert rotated is not None
            assert rotated != token
            # And the rotated token is the one that works.
            assert (
                await client.post(f"{ACCOUNT}/sessions/revoke-others", headers=csrf(rotated))
            ).status_code == 200

    async def test_every_other_session_is_revoked(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="person")

            transport = ASGITransport(app=client.app_state.app)  # type: ignore[attr-defined]
            async with AsyncClient(transport=transport, base_url="http://testserver") as other:
                await other.post(
                    "/api/v1/auth/login",
                    json={"username": "person", "password": PASSWORD},
                )
                assert (await other.get(ME)).status_code == 200

                token = await sign_in(client, "person")
                response = await client.post(
                    f"{ACCOUNT}/password",
                    json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
                    headers=csrf(token),
                )

                assert response.json()["other_sessions_revoked"] == 1
                assert (await other.get(ME)).status_code == 401

    async def test_a_wrong_current_password_is_422_not_401(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        """The status is the point, not an incidental choice.

        The web client treats 401 as "your session has ended" and signs the
        user out. Returning 401 for a mistyped current password would eject
        someone from the application for a typo — so this asserts the status
        *and* that the session survives.
        """
        async with api_factory() as client:
            await seed_user(client, username="person")
            token = await sign_in(client, "person")

            response = await client.post(
                f"{ACCOUNT}/password",
                json={"current_password": "not the password", "new_password": NEW_PASSWORD},
                headers=csrf(token),
            )

            assert response.status_code == 422
            assert response.json()["error"]["code"] == "invalid_current_password"
            # Still signed in.
            assert (await client.get(ME)).status_code == 200

    async def test_a_wrong_current_password_changes_nothing(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        read_sql: ReadSql,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="person")
            before = (await read_sql(client, "SELECT password_hash FROM users"))[0][0]
            token = await sign_in(client, "person")

            await client.post(
                f"{ACCOUNT}/password",
                json={"current_password": "wrong", "new_password": NEW_PASSWORD},
                headers=csrf(token),
            )

            after = (await read_sql(client, "SELECT password_hash FROM users"))[0][0]
            assert after == before
            # And the old password still signs in.
            client.cookies.clear()
            assert (
                await client.post(
                    "/api/v1/auth/login", json={"username": "person", "password": PASSWORD}
                )
            ).status_code == 200

    async def test_a_weak_new_password_is_rejected_with_the_policy(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="person")
            token = await sign_in(client, "person")

            response = await client.post(
                f"{ACCOUNT}/password",
                json={"current_password": PASSWORD, "new_password": "short"},
                headers=csrf(token),
            )

            assert response.status_code == 422
            assert response.json()["error"]["code"] == "validation_failed"
            problems = response.json()["error"]["details"]["problems"]
            assert any("at least 8 characters" in problem for problem in problems)

    async def test_no_response_or_log_carries_either_password(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        from structlog.testing import capture_logs

        async with api_factory() as client:
            await seed_user(client, username="person")
            token = await sign_in(client, "person")

            with capture_logs() as events:
                response = await client.post(
                    f"{ACCOUNT}/password",
                    json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
                    headers=csrf(token),
                )

            rendered = repr(events)
            assert PASSWORD not in response.text
            assert NEW_PASSWORD not in response.text
            assert "$argon2" not in response.text
            assert PASSWORD not in rendered
            assert NEW_PASSWORD not in rendered

    async def test_the_change_requires_the_csrf_header(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="person")
            await sign_in(client, "person")

            response = await client.post(
                f"{ACCOUNT}/password",
                json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
            )

            assert response.status_code == 403
            assert response.json()["error"]["code"] == "csrf_validation_failed"

    async def test_an_anonymous_caller_cannot_change_a_password(
        self, api_factory: ApiFactory
    ) -> None:
        async with api_factory() as client:
            response = await client.post(
                f"{ACCOUNT}/password",
                json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
            )

            assert response.status_code == 401

    async def test_a_confirmation_field_is_refused(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # Confirmation is a client-side guard against a typo in a masked
        # input; the server has nothing to compare it against that the client
        # does not already have, so sending it would transmit the new password
        # twice for no gain. ``extra="forbid"`` makes that explicit.
        async with api_factory() as client:
            await seed_user(client, username="person")
            token = await sign_in(client, "person")

            response = await client.post(
                f"{ACCOUNT}/password",
                json={
                    "current_password": PASSWORD,
                    "new_password": NEW_PASSWORD,
                    "confirm_password": NEW_PASSWORD,
                },
                headers=csrf(token),
            )

            assert response.status_code == 422

    async def test_the_change_is_rate_limited_per_account(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory(password_change_rate_limit_per_user=2) as client:
            await seed_user(client, username="person")
            token = await sign_in(client, "person")

            statuses = []
            for _ in range(4):
                response = await client.post(
                    f"{ACCOUNT}/password",
                    json={"current_password": "wrong", "new_password": NEW_PASSWORD},
                    headers=csrf(token),
                )
                statuses.append(response.status_code)

            assert 429 in statuses
            # And the refusal tells a well-behaved client how long to wait.
            last = await client.post(
                f"{ACCOUNT}/password",
                json={"current_password": "wrong", "new_password": NEW_PASSWORD},
                headers=csrf(token),
            )
            assert last.status_code == 429
            assert int(last.headers["Retry-After"]) >= 1

    async def test_the_throttle_is_separate_from_the_login_budget(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        """Spending the password-change budget must not lock out sign-in.

        Sharing the login throttle would mean changing your password a few
        times consumed the budget you need in order to log in — a
        self-inflicted lockout created by a limiter meant to prevent one.
        """
        async with api_factory(password_change_rate_limit_per_user=1) as client:
            await seed_user(client, username="person")
            token = await sign_in(client, "person")

            for _ in range(3):
                await client.post(
                    f"{ACCOUNT}/password",
                    json={"current_password": "wrong", "new_password": NEW_PASSWORD},
                    headers=csrf(token),
                )

            client.cookies.clear()
            login = await client.post(
                "/api/v1/auth/login", json={"username": "person", "password": PASSWORD}
            )

            assert login.status_code == 200

    async def test_concurrent_changes_do_not_overwrite_each_other(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        """The row lock, observed.

        Two simultaneous changes from different sessions. Without the lock
        both verify against the same old hash and the second overwrites the
        first, so the user ends up with the password from whichever attempt
        happened to land last — including one they believed had failed. With
        it, exactly one succeeds and the other is refused because its current
        password is no longer current.
        """
        third_password = "yet another sufficiently long passphrase"

        async with api_factory() as client:
            await seed_user(client, username="person")

            transport = ASGITransport(app=client.app_state.app)  # type: ignore[attr-defined]
            async with AsyncClient(transport=transport, base_url="http://testserver") as other:
                first_login = await other.post(
                    "/api/v1/auth/login",
                    json={"username": "person", "password": PASSWORD},
                )
                other_token = first_login.json()["csrf_token"]
                own_token = await sign_in(client, "person")

                results = await asyncio.gather(
                    client.post(
                        f"{ACCOUNT}/password",
                        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
                        headers=csrf(own_token),
                    ),
                    other.post(
                        f"{ACCOUNT}/password",
                        json={"current_password": PASSWORD, "new_password": third_password},
                        headers=csrf(other_token),
                    ),
                )

            statuses = sorted(response.status_code for response in results)
            # One applied; the other found its "current" password stale.
            assert statuses == [200, 422], [r.text for r in results]

            # Exactly one of the two candidates is now the password, and the
            # original is not.
            client.cookies.clear()
            assert (
                await client.post(
                    "/api/v1/auth/login", json={"username": "person", "password": PASSWORD}
                )
            ).status_code == 401
            accepted = 0
            for candidate in (NEW_PASSWORD, third_password):
                client.cookies.clear()
                attempt = await client.post(
                    "/api/v1/auth/login",
                    json={"username": "person", "password": candidate},
                )
                accepted += attempt.status_code == 200
            assert accepted == 1

    @pytest.mark.parametrize("role", list(UserRole))
    async def test_every_role_may_change_its_own_password(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        role: UserRole,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="person", role=role)
            token = await sign_in(client, "person")

            response = await client.post(
                f"{ACCOUNT}/password",
                json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
                headers=csrf(token),
            )

            assert response.status_code == 200
