"""Security regressions for user management.

Milestone 1.7. Each test here corresponds to an item from the security review
in the milestone brief (§22), and each asserts the specific thing that would
have gone wrong — not that the feature works, which the other suites cover.

Two of them exist because the review found a real defect:

* ``TestResourceBounds::test_an_enormous_offset_is_refused_not_a_500`` —
  ``offset`` was validated as ``>= 0`` only, and Python integers are
  unbounded, so one query parameter produced an out-of-range bigint in asyncpg
  and an unhandled 500.
* ``TestAuditLogInjection::test_an_attacker_chosen_path_is_bounded`` —
  authorization runs before a route's path parameters are validated, so the
  path recorded on a denial is chosen by whoever was refused. It is rendered
  as text and never interpolated, but it was unbounded on the way into the
  column.

The rest are guards against regressions that would be silent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

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

USERS = "/api/v1/users"
ACCOUNT = "/api/v1/account"
EVENTS = "/api/v1/audit/events"
PASSWORD = "correct horse battery staple"
UNKNOWN_ID = "00000000-0000-4000-8000-000000000000"


def csrf(token: str) -> dict[str, str]:
    return {"X-CSRF-Token": token}


#: Every state-changing operation this milestone added, with a body where one
#: is required. Enumerated so a new mutating route has to be added here, which
#: is what makes the CSRF and authorization sweeps below exhaustive rather
#: than illustrative.
MUTATIONS: list[tuple[str, str, dict[str, Any] | None]] = [
    ("POST", USERS, {"username": "somebody", "password": PASSWORD}),
    ("PATCH", f"{USERS}/{UNKNOWN_ID}", {"role": "viewer"}),
    ("POST", f"{USERS}/{UNKNOWN_ID}/deactivate", None),
    ("POST", f"{USERS}/{UNKNOWN_ID}/reactivate", None),
    ("POST", f"{USERS}/{UNKNOWN_ID}/sessions/revoke", None),
    ("POST", f"{ACCOUNT}/sessions/revoke-others", None),
    ("POST", f"{ACCOUNT}/sessions/{UNKNOWN_ID}/revoke", None),
    (
        "POST",
        f"{ACCOUNT}/password",
        {"current_password": PASSWORD, "new_password": "a different long passphrase"},
    ),
]


class TestCsrfCoverage:
    """Every mutation added by this milestone is CSRF-protected.

    The check lives in the shared session dependency precisely so a route
    cannot be added without it (ADR-030) — but "cannot" is a claim about a
    mechanism, and this is the claim about the routes.
    """

    @pytest.mark.parametrize(("method", "path", "body"), MUTATIONS)
    async def test_a_mutation_without_the_header_is_refused(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        method: str,
        path: str,
        body: dict[str, Any] | None,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await sign_in(client, "admin")

            response = await client.request(method, path, json=body)

            assert response.status_code == 403, f"{method} {path}"
            assert response.json()["error"]["code"] == "csrf_validation_failed"

    @pytest.mark.parametrize(("method", "path", "body"), MUTATIONS)
    async def test_a_forged_token_is_refused(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        method: str,
        path: str,
        body: dict[str, Any] | None,
    ) -> None:
        # The token is compared against the **session row**, not against a
        # cookie, so an attacker who can write cookies for a sibling domain
        # still cannot forge a pair that validates.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await sign_in(client, "admin")
            client.cookies.set("hcc_csrf", "f" * 43)

            response = await client.request(method, path, json=body, headers=csrf("f" * 43))

            assert response.status_code == 403, f"{method} {path}"
            assert response.json()["error"]["code"] == "csrf_validation_failed"

    @pytest.mark.parametrize(("method", "path", "body"), MUTATIONS)
    async def test_a_cross_origin_mutation_is_refused(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        method: str,
        path: str,
        body: dict[str, Any] | None,
    ) -> None:
        # A present `Origin` must match. The token alone is not enough when the
        # browser has told us where the request came from.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")

            response = await client.request(
                method,
                path,
                json=body,
                headers={**csrf(token), "Origin": "https://evil.example.com"},
            )

            assert response.status_code == 403, f"{method} {path}"
            assert response.json()["error"]["code"] == "csrf_validation_failed"


class TestPrivilegeEscalation:
    async def test_an_operator_cannot_promote_itself(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            operator = await seed_user(client, username="operator", role=UserRole.OPERATOR)
            token = await sign_in(client, "operator")

            response = await client.patch(
                f"{USERS}/{operator.id}", json={"role": "admin"}, headers=csrf(token)
            )

            assert response.status_code == 403
            # And the role really is unchanged.
            assert (await client.get(ACCOUNT)).json()["role"] == "operator"

    async def test_a_viewer_cannot_reach_user_management_with_a_valid_csrf_token(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # A valid session plus a valid token is still not an administrator.
        async with api_factory() as client:
            await seed_user(client, username="viewer", role=UserRole.VIEWER)
            token = await sign_in(client, "viewer")

            response = await client.post(
                USERS,
                json={"username": "escalated", "password": PASSWORD, "role": "admin"},
                headers=csrf(token),
            )

            assert response.status_code == 403
            assert response.json()["error"]["code"] == "insufficient_permissions"

    async def test_the_self_service_router_grants_nothing_administrative(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # `/account` is ungated by role, so the thing to check is that it
        # cannot be used to act on anybody else — which it cannot, because no
        # field names an account.
        async with api_factory() as client:
            await seed_user(client, username="viewer", role=UserRole.VIEWER)
            other = await seed_user(client, username="other", role=UserRole.ADMIN)
            token = await sign_in(client, "viewer")

            # The only id the self-service surface accepts is a session id, and
            # it is filtered by owner.
            response = await client.post(
                f"{ACCOUNT}/sessions/{other.id}/revoke", headers=csrf(token)
            )

            assert response.status_code == 404


class TestObjectLevelAuthorization:
    async def test_a_session_id_belonging_to_another_account_cannot_be_revoked(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        read_sql: ReadSql,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="victim")
            await seed_user(client, username="attacker")

            await sign_in(client, "victim")
            rows = await read_sql(client, "SELECT id::text FROM sessions")
            victim_session = rows[0][0]

            client.cookies.clear()
            token = await sign_in(client, "attacker")
            response = await client.post(
                f"{ACCOUNT}/sessions/{victim_session}/revoke", headers=csrf(token)
            )

            assert response.status_code == 404
            # Untouched.
            still_live = await read_sql(
                client,
                f"SELECT revoked_at IS NULL FROM sessions WHERE id = '{victim_session}'",
            )
            assert still_live[0][0] is True

    async def test_an_unknown_session_id_is_indistinguishable_from_a_foreign_one(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # Otherwise the endpoint is an oracle for which session ids are live.
        async with api_factory() as client:
            await seed_user(client, username="person")
            token = await sign_in(client, "person")

            unknown = await client.post(
                f"{ACCOUNT}/sessions/{UNKNOWN_ID}/revoke", headers=csrf(token)
            )

            assert unknown.status_code == 404
            assert unknown.json()["error"]["code"] == "not_found"

    async def test_a_viewer_is_refused_before_the_target_is_resolved(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # 403 rather than 404 for a non-existent id: the authorization check
        # must not become an existence oracle for a lower role.
        async with api_factory() as client:
            await seed_user(client, username="viewer", role=UserRole.VIEWER)
            await sign_in(client, "viewer")

            response = await client.get(f"{USERS}/{UNKNOWN_ID}")

            assert response.status_code == 403


class TestMassAssignment:
    @pytest.mark.parametrize(
        "field",
        [
            "password_hash",
            "id",
            "user_id",
            "created_at",
            "updated_at",
            "last_login_at",
            "username_canonical",
        ],
    )
    async def test_no_internal_field_can_be_set_on_creation(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        field: str,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")

            response = await client.post(
                USERS,
                json={"username": "injected", "password": PASSWORD, field: "x"},
                headers=csrf(token),
            )

            assert response.status_code == 422, field

    @pytest.mark.parametrize(
        "field", ["password_hash", "username", "username_canonical", "id", "created_at"]
    )
    async def test_no_internal_field_can_be_set_on_update(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        field: str,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            target = await seed_user(client, username="target")
            token = await sign_in(client, "admin")

            response = await client.patch(
                f"{USERS}/{target.id}", json={field: "x"}, headers=csrf(token)
            )

            assert response.status_code == 422, field

    async def test_a_password_cannot_be_set_through_the_update_endpoint(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # An administrator setting somebody else's password without knowing the
        # old one is a deliberate non-feature: the account's owner changes it,
        # or the administrator creates a new account. A field appearing here
        # would be a credential-reset path nobody reviewed.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            target = await seed_user(client, username="target")
            token = await sign_in(client, "admin")

            response = await client.patch(
                f"{USERS}/{target.id}", json={"password": PASSWORD}, headers=csrf(token)
            )

            assert response.status_code == 422


class TestResourceBounds:
    async def test_an_enormous_offset_is_refused_not_a_500(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        """The defect this test was written for.

        ``offset`` was bounded below and not above, and Python integers are
        unbounded — so this value passed validation and then failed inside
        asyncpg as an out-of-range bigint, producing an unhandled 500 from one
        query parameter.
        """
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await sign_in(client, "admin")

            response = await client.get(USERS, params={"offset": 10**30})

            assert response.status_code == 422
            assert response.json()["error"]["code"] == "validation_failed"

    async def test_a_negative_offset_is_refused(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await sign_in(client, "admin")

            assert (await client.get(USERS, params={"offset": -1})).status_code == 422

    async def test_an_over_long_search_term_is_refused(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # A search term becomes a `LIKE` pattern, so an unbounded term is an
        # unbounded pattern.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await sign_in(client, "admin")

            assert (await client.get(USERS, params={"search": "x" * 500})).status_code == 422

    async def test_an_over_long_password_is_refused_before_hashing(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # Argon2's cost grows with input length, so an unbounded password field
        # is a way to make the server do arbitrary work. Rejected by the
        # policy, never truncated (ADR-026).
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")

            response = await client.post(
                USERS,
                json={"username": "oversized", "password": "x" * 2000},
                headers=csrf(token),
            )

            assert response.status_code == 422
            assert "1024 bytes" in response.text

    async def test_a_request_body_with_a_megabyte_field_is_refused(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")

            response = await client.post(
                USERS,
                json={"username": "x" * 1_000_000, "password": PASSWORD},
                headers=csrf(token),
            )

            assert response.status_code == 422

    async def test_the_user_listing_is_two_queries_regardless_of_page_size(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        """No N+1 as the account count grows.

        The session count is a correlated subquery inside the page query, not a
        per-row lookup. Counted by instrumenting the engine, because the
        difference between one query and twenty-six is invisible in a response.
        """
        from sqlalchemy import event

        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            for index in range(20):
                await seed_user(client, username=f"user{index:02d}")
            await sign_in(client, "admin")

            statements: list[str] = []
            engine = client.app_state.database.engine.sync_engine  # type: ignore[attr-defined]

            def record(
                _conn: Any,
                _cursor: Any,
                statement: str,
                _parameters: Any,
                _context: Any,
                _executemany: bool,
            ) -> None:
                statements.append(statement)

            event.listen(engine, "before_cursor_execute", record)
            try:
                response = await client.get(USERS, params={"limit": 25})
            finally:
                event.remove(engine, "before_cursor_execute", record)

            assert response.status_code == 200
            assert len(response.json()["items"]) == 21

            # The session resolution for the request itself, plus the count and
            # the page. Bounded well below one-per-row either way.
            selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
            assert len(selects) <= 4, selects


class TestAuditLogInjection:
    async def test_an_attacker_chosen_path_is_bounded(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        read_sql: ReadSql,
    ) -> None:
        """The second defect this file was written for.

        Authorization runs before a route's path parameters are validated, so
        a caller refused at ``/api/v1/users/{user_id}`` has chosen whatever
        that segment contains — the recorded path looks server-derived and is
        not. It is now truncated on the way in.
        """
        from hcc.security.audit import MAX_DENIED_PATH_LENGTH

        async with api_factory() as client:
            await seed_user(client, username="viewer", role=UserRole.VIEWER)
            await sign_in(client, "viewer")

            await client.get(f"{USERS}/{'z' * 4000}")

            rows = await read_sql(
                client,
                "SELECT details->>'path' FROM audit_events WHERE action = 'admin.access_denied'",
            )
            assert len(rows) == 1
            assert len(rows[0][0]) <= MAX_DENIED_PATH_LENGTH

    async def test_markup_in_a_denied_path_is_stored_as_text(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        read_sql: ReadSql,
    ) -> None:
        """Stored verbatim, which is correct — the record says what was tried.

        Nothing interpolates it into markup, and the audit viewer renders it as
        text; the frontend suite pins that escaping.

        Worth recording what bounds the exposure, because it is narrower than
        it first appears: a denial is only written once a route has *matched*,
        and an escaped slash is decoded before matching — so
        ``/users/a%2Fb`` is two segments, matches nothing, and answers 404
        before authorization runs. The attacker-chosen portion is therefore one
        path segment containing no slash, truncated to
        ``MAX_DENIED_PATH_LENGTH``.
        """
        async with api_factory() as client:
            await seed_user(client, username="viewer", role=UserRole.VIEWER)
            await sign_in(client, "viewer")

            await client.get(f"{USERS}/%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E")

            rows = await read_sql(
                client,
                "SELECT details->>'path' FROM audit_events WHERE action = 'admin.access_denied'",
            )
            # One record, and its path is a JSON string value — not SQL the
            # server composed, and not markup it acted on.
            assert len(rows) == 1
            assert isinstance(rows[0][0], str)
            assert "<img" in rows[0][0]

    async def test_a_username_in_an_audit_detail_cannot_carry_control_characters(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        read_sql: ReadSql,
    ) -> None:
        # The one caller-supplied value stored in a detail is the canonical
        # username, and normalization already rejects control and format
        # characters (ADR-014) — so the trail cannot be made to contain
        # invisible text that would render as something else.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")

            refused = await client.post(
                USERS,
                json={"username": "ad​min2", "password": PASSWORD},
                headers=csrf(token),
            )
            assert refused.status_code == 422

            rows = await read_sql(
                client,
                "SELECT coalesce(string_agg(details::text, ' '), '') FROM audit_events",
            )
            assert "​" not in rows[0][0]


class TestDisclosure:
    async def test_no_administrative_response_carries_credential_material(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            admin = await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            session_token = client.cookies.get("hcc_session")
            assert session_token

            bodies = [
                (await client.get(USERS)).text,
                (await client.get(f"{USERS}/{admin.id}")).text,
                (await client.get(ACCOUNT)).text,
                (await client.get(f"{ACCOUNT}/sessions")).text,
                (await client.get(EVENTS)).text,
            ]

            for body in bodies:
                assert "$argon2" not in body
                assert session_token not in body
                # The CSRF token is returned by /auth/me, which is how
                # double-submit works at all — and by nothing else.
                assert token not in body
                assert "password_hash" not in body
                assert "token_hash" not in body
                assert "username_canonical" not in body

    async def test_the_session_list_discloses_no_token_material(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        read_sql: ReadSql,
    ) -> None:
        # The id is returned and is safe; the token's digest is the lookup key
        # and must not be.
        async with api_factory() as client:
            await seed_user(client, username="person")
            token = await sign_in(client, "person")

            body = (await client.get(f"{ACCOUNT}/sessions")).text
            hashes = await read_sql(client, "SELECT token_hash, csrf_token FROM sessions")

            assert hashes[0][0] not in body
            # The CSRF token is handed back by /auth/me, which is how
            # double-submit works at all — but it has no business in a session
            # inventory.
            assert hashes[0][1] not in body
            assert token not in body

    async def test_a_role_denial_names_no_role(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="viewer", role=UserRole.VIEWER)
            await sign_in(client, "viewer")

            body = (await client.get(USERS)).text.lower()

            assert "admin" not in body
            assert "operator" not in body
            assert "viewer" not in body

    async def test_no_secret_reaches_a_log_during_user_management(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        from structlog.testing import capture_logs

        new_password = "a different sufficiently long passphrase"

        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            target = await seed_user(client, username="target")
            token = await sign_in(client, "admin")
            session_token = client.cookies.get("hcc_session")
            assert session_token

            with capture_logs() as events:
                await client.post(
                    USERS,
                    json={"username": "created", "password": PASSWORD},
                    headers=csrf(token),
                )
                await client.patch(
                    f"{USERS}/{target.id}", json={"role": "operator"}, headers=csrf(token)
                )
                await client.post(
                    f"{ACCOUNT}/password",
                    json={"current_password": PASSWORD, "new_password": new_password},
                    headers=csrf(token),
                )

            rendered = repr(events)
            assert PASSWORD not in rendered
            assert new_password not in rendered
            assert "$argon2" not in rendered
            assert session_token not in rendered
            assert token not in rendered


class TestStaleAuthorization:
    async def test_a_revoked_session_cannot_perform_administration(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            admin = await seed_user(client, username="admin", role=UserRole.ADMIN)
            await seed_user(client, username="second", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            cookies = dict(client.cookies)

            client.cookies.clear()
            second_csrf = await sign_in(client, "second")
            await client.post(f"{USERS}/{admin.id}/sessions/revoke", headers=csrf(second_csrf))

            client.cookies.clear()
            for name, value in cookies.items():
                client.cookies.set(name, value)

            assert (await client.get(USERS)).status_code == 401
            assert (
                await client.post(
                    USERS,
                    json={"username": "ghost", "password": PASSWORD},
                    headers=csrf(token),
                )
            ).status_code == 401

    async def test_a_demoted_session_cannot_perform_administration(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="first", role=UserRole.ADMIN)
            second = await seed_user(client, username="second", role=UserRole.ADMIN)
            token = await sign_in(client, "second")
            cookies = dict(client.cookies)

            client.cookies.clear()
            first_csrf = await sign_in(client, "first")
            await client.patch(
                f"{USERS}/{second.id}", json={"role": "viewer"}, headers=csrf(first_csrf)
            )

            client.cookies.clear()
            for name, value in cookies.items():
                client.cookies.set(name, value)

            response = await client.post(
                USERS, json={"username": "ghost", "password": PASSWORD}, headers=csrf(token)
            )
            assert response.status_code == 403

    async def test_a_password_change_does_not_leave_the_old_token_usable(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # Session fixation's cousin: a token that survives a credential event.
        async with api_factory() as client:
            await seed_user(client, username="person")
            token = await sign_in(client, "person")
            old_cookie = client.cookies.get("hcc_session")

            await client.post(
                f"{ACCOUNT}/password",
                json={
                    "current_password": PASSWORD,
                    "new_password": "a different sufficiently long passphrase",
                },
                headers=csrf(token),
            )

            client.cookies.clear()
            assert old_cookie
            client.cookies.set("hcc_session", old_cookie)
            assert (await client.get("/api/v1/auth/me")).status_code == 401

    async def test_the_replacement_session_is_not_influenced_by_the_client(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        read_sql: ReadSql,
    ) -> None:
        """Session fixation, on the one path that issues a session after login.

        The token is generated server-side from a CSPRNG and nothing the caller
        sends influences it. Asserted by offering a candidate value in headers
        — the only place a caller could put one, since the request model forbids
        unknown body fields — and checking the issued token is neither that
        value nor the one the browser already held.

        An earlier draft sent a literal ``Cookie`` header instead, which
        replaces httpx's cookie jar wholesale: the request then arrived with no
        valid session at all and answered 401, so the test was exercising its
        own construction rather than the server.
        """
        proposed = "attacker-chosen-session-token"

        async with api_factory() as client:
            await seed_user(client, username="person")
            token = await sign_in(client, "person")
            before = client.cookies.get("hcc_session")

            response = await client.post(
                f"{ACCOUNT}/password",
                json={
                    "current_password": PASSWORD,
                    "new_password": "a different sufficiently long passphrase",
                },
                headers={**csrf(token), "X-Session-Token": proposed, "X-Csrf": proposed},
            )

            assert response.status_code == 200
            issued = client.cookies.get("hcc_session")
            assert issued is not None
            assert issued != proposed
            assert issued != before
            # Exactly one live session afterwards: the replacement, and nothing
            # the caller proposed.
            rows = await read_sql(client, "SELECT count(*) FROM sessions WHERE revoked_at IS NULL")
            assert rows[0][0] == 1


class TestErrorCodes:
    """A client has to be able to tell these apart, so each is pinned."""

    async def test_anonymous_is_401(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            response = await client.get(USERS)
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "not_authenticated"

    async def test_insufficient_role_is_403(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="viewer", role=UserRole.VIEWER)
            await sign_in(client, "viewer")
            response = await client.get(USERS)
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "insufficient_permissions"

    async def test_unknown_account_is_404(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await sign_in(client, "admin")
            response = await client.get(f"{USERS}/{UNKNOWN_ID}")
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "not_found"

    async def test_a_duplicate_username_is_409_with_its_own_code(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            response = await client.post(
                USERS, json={"username": "admin", "password": PASSWORD}, headers=csrf(token)
            )
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "username_taken"

    async def test_the_last_administrator_refusal_is_409_with_its_own_code(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        # A distinct code, because the remedy is different from a duplicate
        # username and the interface has to explain it.
        async with api_factory() as client:
            admin = await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            response = await client.patch(
                f"{USERS}/{admin.id}", json={"role": "viewer"}, headers=csrf(token)
            )
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "last_administrator"

    async def test_a_wrong_current_password_is_422_and_never_401(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        # Load-bearing: the client treats 401 as an ended session.
        async with api_factory() as client:
            await seed_user(client, username="person")
            token = await sign_in(client, "person")
            response = await client.post(
                f"{ACCOUNT}/password",
                json={"current_password": "wrong", "new_password": "a long enough one"},
                headers=csrf(token),
            )
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "invalid_current_password"

    async def test_no_response_carries_a_traceback_or_an_exception_type(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            admin = await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")

            bodies = [
                (await client.get(f"{USERS}/{UNKNOWN_ID}")).text,
                (
                    await client.patch(
                        f"{USERS}/{admin.id}", json={"role": "viewer"}, headers=csrf(token)
                    )
                ).text,
                (
                    await client.post(
                        USERS,
                        json={"username": "ab", "password": PASSWORD},
                        headers=csrf(token),
                    )
                ).text,
            ]

            for body in bodies:
                assert "Traceback" not in body
                assert "Error(" not in body
                assert "hcc.services" not in body
                assert "hcc_local_dev" not in body
