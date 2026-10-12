"""Administrative user management, against real PostgreSQL.

Milestone 1.7. These run against the real database rather than a mocked
repository, and that is load-bearing rather than thorough-for-its-own-sake:
the properties being asserted — case-insensitive uniqueness under concurrency,
rollback on a constraint violation, the active-session count agreeing with
session resolution — are properties of PostgreSQL and of the schema. A mock
would assert that the code calls the methods the test expects, which is a
restatement of the code.

The last-administrator invariant has its own file,
``test_last_admin_integration.py``, because it needs genuinely concurrent
connections.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from hcc.models.user import USERNAME_MAX_LENGTH, UserRole

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
PASSWORD = "correct horse battery staple"


def csrf(token: str) -> dict[str, str]:
    """The header a state-changing request must carry."""
    return {"X-CSRF-Token": token}


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


class TestListing:
    async def test_returns_every_account_with_a_total(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await seed_user(client, username="operator", role=UserRole.OPERATOR)
            await seed_user(client, username="viewer", role=UserRole.VIEWER)
            await sign_in(client, "admin")

            body = (await client.get(USERS)).json()

            assert body["total"] == 3
            assert [item["username"] for item in body["items"]] == [
                "admin",
                "operator",
                "viewer",
            ]

    async def test_ordering_is_by_canonical_username_and_is_total(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # Alphabetical by the *canonical* form, so casing does not decide the
        # order — "Zeta" must not sort before "alpha" just because uppercase
        # letters come first in ASCII.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await seed_user(client, username="Zeta")
            await seed_user(client, username="alpha")
            await sign_in(client, "admin")

            usernames = [item["username"] for item in (await client.get(USERS)).json()["items"]]

            assert usernames == ["admin", "alpha", "Zeta"]

    async def test_pagination_is_deterministic_across_pages(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            for index in range(5):
                await seed_user(client, username=f"user{index}")
            await sign_in(client, "admin")

            first = (await client.get(USERS, params={"limit": 3, "offset": 0})).json()
            second = (await client.get(USERS, params={"limit": 3, "offset": 3})).json()

            assert first["total"] == second["total"] == 6
            assert len(first["items"]) == 3
            assert len(second["items"]) == 3
            # No row appears on both pages, which is the property offset
            # pagination over a total ordering actually guarantees.
            overlap = {item["user_id"] for item in first["items"]} & {
                item["user_id"] for item in second["items"]
            }
            assert overlap == set()

    async def test_limit_is_capped_rather_than_honoured(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # The bound that stops an authenticated caller asking the server to
        # materialise the whole table. Rejected at validation, so the request
        # never reaches a query.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await sign_in(client, "admin")

            response = await client.get(USERS, params={"limit": 100_000})

            assert response.status_code == 422

    async def test_search_folds_the_term_like_a_username(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await seed_user(client, username="FelixStaude")
            await sign_in(client, "admin")

            # Uppercase query against a canonical lowercase column.
            body = (await client.get(USERS, params={"search": "FELIX"})).json()

            assert [item["username"] for item in body["items"]] == ["FelixStaude"]
            assert body["total"] == 1

    async def test_search_accepts_a_term_too_short_to_be_a_username(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # A search term is not a username, so the three-character minimum must
        # not apply: typing one letter has to narrow the list, not error.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await seed_user(client, username="zebra")
            await sign_in(client, "admin")

            response = await client.get(USERS, params={"search": "z"})

            assert response.status_code == 200
            assert [item["username"] for item in response.json()["items"]] == ["zebra"]

    async def test_search_wildcards_are_escaped(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # `%` in a LIKE pattern matches everything. Unescaped, searching for
        # "%" would return every account while appearing to filter — a
        # misleading result in a screen an operator uses to audit access.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await seed_user(client, username="viewer")
            await sign_in(client, "admin")

            body = (await client.get(USERS, params={"search": "%"})).json()

            assert body["total"] == 0
            assert body["items"] == []

    async def test_role_and_status_filters_combine(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await seed_user(client, username="op-active", role=UserRole.OPERATOR)
            await seed_user(client, username="op-inactive", role=UserRole.OPERATOR, is_active=False)
            await sign_in(client, "admin")

            body = (
                await client.get(USERS, params={"role": "operator", "is_active": "true"})
            ).json()

            assert [item["username"] for item in body["items"]] == ["op-active"]
            assert body["total"] == 1

    async def test_the_total_respects_the_filters(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # A total computed under different filters than the rows is a pager
        # that lies, which is why the page and count queries share one clause
        # builder.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            for index in range(4):
                await seed_user(client, username=f"viewer{index}")
            await sign_in(client, "admin")

            body = (await client.get(USERS, params={"role": "viewer", "limit": 2})).json()

            assert len(body["items"]) == 2
            assert body["total"] == 4

    async def test_active_session_count_matches_real_sessions(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await seed_user(client, username="viewer")
            # Two sessions for the viewer, from two separate logins.
            await sign_in(client, "viewer")
            client.cookies.clear()
            await sign_in(client, "viewer")
            client.cookies.clear()
            await sign_in(client, "admin")

            body = (await client.get(USERS, params={"search": "viewer"})).json()

            assert body["items"][0]["active_session_count"] == 2

    async def test_a_revoked_session_is_not_counted(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        read_sql: ReadSql,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await seed_user(client, username="viewer")
            viewer_csrf = await sign_in(client, "viewer")
            await client.post("/api/v1/auth/logout", headers=csrf(viewer_csrf))
            client.cookies.clear()
            await sign_in(client, "admin")

            body = (await client.get(USERS, params={"search": "viewer"})).json()

            assert body["items"][0]["active_session_count"] == 0
            # And the row is still there, marked — not deleted.
            rows = await read_sql(
                client, "SELECT revoked_at IS NOT NULL FROM sessions ORDER BY created_at"
            )
            assert any(row[0] for row in rows)

    async def test_an_idle_session_is_not_counted(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        """The count must mean the same thing session resolution means.

        This is the defect the shared ``live_session_predicate`` exists to
        prevent. A filter of "not revoked and not expired" omits the idle
        timeout, so a session abandoned past it would be reported as active
        while being refused on its next request — the administrative view
        would show access that does not exist.
        """
        async with api_factory(session_idle_timeout_seconds=60) as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await seed_user(client, username="viewer")
            await sign_in(client, "viewer")
            client.cookies.clear()
            await sign_in(client, "admin")

            async with client.app_state.database.session_factory() as db:  # type: ignore[attr-defined]
                from sqlalchemy import text as sql_text

                await db.execute(
                    sql_text(
                        "UPDATE sessions SET last_seen_at = now() - interval '2 hours' "
                        "WHERE user_id = (SELECT id FROM users WHERE username_canonical = 'viewer')"
                    )
                )
                await db.commit()

            body = (await client.get(USERS, params={"search": "viewer"})).json()

            assert body["items"][0]["active_session_count"] == 0

    async def test_no_response_field_carries_a_hash_or_a_canonical_name(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="AdminUser", role=UserRole.ADMIN)
            await sign_in(client, "AdminUser")

            response = await client.get(USERS)
            item = response.json()["items"][0]

            assert set(item) == {
                "user_id",
                "username",
                "display_name",
                "role",
                "is_active",
                "created_at",
                "last_login_at",
                "active_session_count",
            }
            assert "$argon2" not in response.text
            assert "password" not in response.text.lower()
            assert "username_canonical" not in response.text


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


class TestCreation:
    async def test_creates_an_account_that_can_sign_in(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # The property that matters: the endpoint does not merely write a row,
        # it produces a working credential.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")

            response = await client.post(
                USERS,
                json={
                    "username": "NewOperator",
                    "password": PASSWORD,
                    "display_name": "New Operator",
                    "role": "operator",
                },
                headers=csrf(token),
            )

            assert response.status_code == 201, response.text
            assert response.json()["role"] == "operator"
            assert response.json()["display_name"] == "New Operator"

            client.cookies.clear()
            login = await client.post(
                "/api/v1/auth/login",
                json={"username": "newoperator", "password": PASSWORD},
            )
            assert login.status_code == 200
            assert login.json()["role"] == "operator"

    async def test_the_default_role_is_viewer(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # Least privilege. An account created without an explicit role must
        # never come out an administrator.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")

            response = await client.post(
                USERS,
                json={"username": "defaulted", "password": PASSWORD},
                headers=csrf(token),
            )

            assert response.status_code == 201
            assert response.json()["role"] == "viewer"
            assert response.json()["is_active"] is True

    async def test_the_response_carries_no_password_material(
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
                json={"username": "created", "password": PASSWORD},
                headers=csrf(token),
            )

            assert PASSWORD not in response.text
            assert "$argon2" not in response.text

    async def test_a_duplicate_username_is_a_conflict(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await seed_user(client, username="taken")
            token = await sign_in(client, "admin")

            response = await client.post(
                USERS,
                json={"username": "taken", "password": PASSWORD},
                headers=csrf(token),
            )

            assert response.status_code == 409
            assert response.json()["error"]["code"] == "username_taken"

    @pytest.mark.parametrize("variant", ["TAKEN", "Taken", "tAkEn"])
    async def test_a_case_variant_is_the_same_account(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        variant: str,
    ) -> None:
        # ADR-014: users expect `Admin` and `admin` to be one account, and a
        # near-duplicate differing only in case is an impersonation risk.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await seed_user(client, username="taken")
            token = await sign_in(client, "admin")

            response = await client.post(
                USERS,
                json={"username": variant, "password": PASSWORD},
                headers=csrf(token),
            )

            assert response.status_code == 409
            assert response.json()["error"]["code"] == "username_taken"

    async def test_a_unicode_near_duplicate_is_refused(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # Full-width "admin2" folds onto the ASCII spelling under NFKC, which
        # is exactly the confusable the normalization exists to collapse.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await seed_user(client, username="admin2")
            token = await sign_in(client, "admin")

            response = await client.post(
                USERS,
                # Written as escapes rather than literal full-width glyphs:
                # the point of the test is that these *look* like ASCII, which
                # makes them equally invisible in a source file.
                json={"username": "\uff41\uff44\uff4d\uff49\uff4e\uff12", "password": PASSWORD},
                headers=csrf(token),
            )

            assert response.status_code == 409

    async def test_a_weak_password_is_rejected_with_every_problem(
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
                json={"username": "weakling", "password": "short"},
                headers=csrf(token),
            )

            assert response.status_code == 422
            problems = response.json()["error"]["details"]["problems"]
            assert any("at least 8 characters" in problem for problem in problems)
            # The policy is described; the password never is.
            assert "short" not in response.text

    async def test_an_invalid_username_is_rejected(
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
                json={"username": "ab", "password": PASSWORD},
                headers=csrf(token),
            )

            assert response.status_code == 422
            assert "at least 3 characters" in response.text

    async def test_an_invalid_role_is_rejected_by_the_schema(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # The closed role set reaches the request schema, so "superadmin" is
        # refused before any of our code runs.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")

            response = await client.post(
                USERS,
                json={"username": "escalate", "password": PASSWORD, "role": "superadmin"},
                headers=csrf(token),
            )

            assert response.status_code == 422

    @pytest.mark.parametrize(
        "field", ["password_hash", "id", "created_at", "username_canonical", "last_login_at"]
    )
    async def test_an_unknown_field_is_refused_not_ignored(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        field: str,
    ) -> None:
        """Mass assignment, closed at the boundary.

        ``extra="forbid"`` means a request naming an internal column is
        **rejected**. Silently dropping it would be worse than either
        accepting or refusing: the caller would believe it had set the field.
        """
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")

            response = await client.post(
                USERS,
                json={"username": "injected", "password": PASSWORD, field: "whatever"},
                headers=csrf(token),
            )

            assert response.status_code == 422

    async def test_an_account_can_be_created_already_inactive(
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
                json={"username": "preprovisioned", "password": PASSWORD, "is_active": False},
                headers=csrf(token),
            )

            assert response.status_code == 201
            assert response.json()["is_active"] is False

            client.cookies.clear()
            login = await client.post(
                "/api/v1/auth/login",
                json={"username": "preprovisioned", "password": PASSWORD},
            )
            assert login.status_code == 401

    async def test_a_failed_creation_leaves_no_row_behind(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        read_sql: ReadSql,
    ) -> None:
        # Transaction rollback, observed rather than assumed: a refused
        # creation must not leave a half-written account that cannot sign in
        # and occupies the username.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await seed_user(client, username="occupied")
            token = await sign_in(client, "admin")

            before = await read_sql(client, "SELECT count(*) FROM users")
            await client.post(
                USERS,
                json={"username": "occupied", "password": PASSWORD},
                headers=csrf(token),
            )
            after = await read_sql(client, "SELECT count(*) FROM users")

            assert before[0][0] == after[0][0] == 2

    async def test_a_display_name_at_the_limit_is_accepted(
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
                json={"username": "longname", "password": PASSWORD, "display_name": "x" * 64},
                headers=csrf(token),
            )

            assert response.status_code == 201
            assert response.json()["display_name"] == "x" * 64

    async def test_an_over_long_display_name_is_rejected_not_truncated(
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
                json={"username": "longname", "password": PASSWORD, "display_name": "x" * 65},
                headers=csrf(token),
            )

            assert response.status_code == 422

    async def test_a_blank_display_name_becomes_null(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # Null means "no separate label"; the empty string would render as a
        # gap indistinguishable from a bug, so the two are not both storable.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")

            response = await client.post(
                USERS,
                json={"username": "unlabelled", "password": PASSWORD, "display_name": "   "},
                headers=csrf(token),
            )

            assert response.status_code == 201
            assert response.json()["display_name"] is None

    async def test_a_username_at_the_column_limit_is_accepted(
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
                json={"username": "u" * USERNAME_MAX_LENGTH, "password": PASSWORD},
                headers=csrf(token),
            )

            assert response.status_code == 201


# ---------------------------------------------------------------------------
# Updates
# ---------------------------------------------------------------------------


class TestUpdates:
    async def test_changes_a_display_name(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            target = await seed_user(client, username="renamed", display_name="Old Name")
            token = await sign_in(client, "admin")

            response = await client.patch(
                f"{USERS}/{target.id}",
                json={"display_name": "New Name"},
                headers=csrf(token),
            )

            assert response.status_code == 200
            assert response.json()["display_name"] == "New Name"
            # The login name is untouched.
            assert response.json()["username"] == "renamed"

    async def test_a_null_display_name_clears_it(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            target = await seed_user(client, username="labelled", display_name="Label")
            token = await sign_in(client, "admin")

            response = await client.patch(
                f"{USERS}/{target.id}", json={"display_name": None}, headers=csrf(token)
            )

            assert response.json()["display_name"] is None

    async def test_an_omitted_display_name_is_left_alone(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # The distinction `UNSET` exists for: omitting a key must not be the
        # same as sending null, or no partial update could ever preserve a
        # field it does not mention.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            target = await seed_user(client, username="labelled", display_name="Keep Me")
            token = await sign_in(client, "admin")

            response = await client.patch(
                f"{USERS}/{target.id}", json={"role": "operator"}, headers=csrf(token)
            )

            assert response.json()["display_name"] == "Keep Me"
            assert response.json()["role"] == "operator"

    async def test_a_role_change_takes_effect_on_the_next_request(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        """Authorization is read per request, not cached from login.

        The scenario from the brief: an administrator demotes an operator who
        already holds a session. The backend must enforce the new role on that
        session's next privileged request, with no re-login and no sweep.
        """
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            target = await seed_user(client, username="promoted", role=UserRole.VIEWER)

            # The target signs in as a viewer and is refused.
            await sign_in(client, "promoted")
            assert (await client.get(USERS)).status_code == 403
            viewer_cookies = dict(client.cookies)

            # An administrator promotes them, on a separate session.
            client.cookies.clear()
            admin_csrf = await sign_in(client, "admin")
            assert (
                await client.patch(
                    f"{USERS}/{target.id}", json={"role": "admin"}, headers=csrf(admin_csrf)
                )
            ).status_code == 200

            # The *same* session, unchanged, now passes.
            client.cookies.clear()
            for name, value in viewer_cookies.items():
                client.cookies.set(name, value)
            assert (await client.get(USERS)).status_code == 200

    async def test_a_demotion_takes_effect_on_the_next_request(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # The direction that matters for security: a session must lose access
        # the moment the role is reduced, without waiting for expiry.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            other = await seed_user(client, username="second", role=UserRole.ADMIN)

            await sign_in(client, "second")
            assert (await client.get(USERS)).status_code == 200
            demoted_cookies = dict(client.cookies)

            client.cookies.clear()
            admin_csrf = await sign_in(client, "admin")
            await client.patch(
                f"{USERS}/{other.id}", json={"role": "viewer"}, headers=csrf(admin_csrf)
            )

            client.cookies.clear()
            for name, value in demoted_cookies.items():
                client.cookies.set(name, value)
            response = await client.get(USERS)

            assert response.status_code == 403
            assert response.json()["error"]["code"] == "insufficient_permissions"

    async def test_an_empty_update_is_accepted_and_changes_nothing(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            target = await seed_user(client, username="untouched", display_name="Same")
            token = await sign_in(client, "admin")

            response = await client.patch(f"{USERS}/{target.id}", json={}, headers=csrf(token))

            assert response.status_code == 200
            assert response.json()["display_name"] == "Same"
            assert response.json()["role"] == "viewer"

    async def test_an_unknown_user_is_a_404(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")

            response = await client.patch(
                f"{USERS}/00000000-0000-4000-8000-000000000000",
                json={"role": "viewer"},
                headers=csrf(token),
            )

            assert response.status_code == 404

    async def test_a_malformed_id_is_a_422_not_a_500(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")

            response = await client.patch(
                f"{USERS}/not-a-uuid", json={"role": "viewer"}, headers=csrf(token)
            )

            assert response.status_code == 422

    @pytest.mark.parametrize("field", ["password_hash", "username", "username_canonical", "id"])
    async def test_an_unknown_update_field_is_refused(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        field: str,
    ) -> None:
        # ``username`` is in this list on purpose: usernames are immutable
        # (ADR-041), so an attempt to change one must fail loudly rather than
        # be silently dropped.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            target = await seed_user(client, username="target")
            token = await sign_in(client, "admin")

            response = await client.patch(
                f"{USERS}/{target.id}", json={field: "whatever"}, headers=csrf(token)
            )

            assert response.status_code == 422

    async def test_a_mutation_requires_the_csrf_header(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            target = await seed_user(client, username="target")
            await sign_in(client, "admin")

            response = await client.patch(f"{USERS}/{target.id}", json={"role": "operator"})

            assert response.status_code == 403
            assert response.json()["error"]["code"] == "csrf_validation_failed"


# ---------------------------------------------------------------------------
# Deactivation and reactivation
# ---------------------------------------------------------------------------


class TestActivation:
    async def test_deactivation_blocks_authentication_immediately(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            target = await seed_user(client, username="leaving")
            token = await sign_in(client, "admin")

            response = await client.post(f"{USERS}/{target.id}/deactivate", headers=csrf(token))

            assert response.status_code == 200
            assert response.json()["is_active"] is False

            client.cookies.clear()
            login = await client.post(
                "/api/v1/auth/login", json={"username": "leaving", "password": PASSWORD}
            )
            assert login.status_code == 401

    async def test_deactivation_invalidates_an_existing_session(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            target = await seed_user(client, username="leaving")

            await sign_in(client, "leaving")
            assert (await client.get("/api/v1/auth/me")).status_code == 200
            target_cookies = dict(client.cookies)

            client.cookies.clear()
            admin_csrf = await sign_in(client, "admin")
            await client.post(f"{USERS}/{target.id}/deactivate", headers=csrf(admin_csrf))

            client.cookies.clear()
            for name, value in target_cookies.items():
                client.cookies.set(name, value)
            response = await client.get("/api/v1/auth/me")

            assert response.status_code == 401
            assert response.json()["error"]["code"] == "not_authenticated"

    async def test_deactivation_revokes_sessions_so_counts_stay_honest(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # Deactivation already blocks authentication; the revocation is what
        # keeps the administrative session count from reporting access that
        # does not exist.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            target = await seed_user(client, username="leaving")
            await sign_in(client, "leaving")
            client.cookies.clear()
            admin_csrf = await sign_in(client, "admin")

            before = (await client.get(f"{USERS}/{target.id}")).json()
            assert before["active_session_count"] == 1

            after = (
                await client.post(f"{USERS}/{target.id}/deactivate", headers=csrf(admin_csrf))
            ).json()

            assert after["active_session_count"] == 0

    async def test_reactivation_restores_sign_in_but_not_old_sessions(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # A revoked token stays revoked. Reactivation grants the ability to
        # authenticate again, which is a different thing from resurrecting a
        # credential somebody may have taken.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            target = await seed_user(client, username="returning")
            await sign_in(client, "returning")
            target_cookies = dict(client.cookies)

            client.cookies.clear()
            admin_csrf = await sign_in(client, "admin")
            await client.post(f"{USERS}/{target.id}/deactivate", headers=csrf(admin_csrf))
            await client.post(f"{USERS}/{target.id}/reactivate", headers=csrf(admin_csrf))

            # The old cookie is still refused.
            client.cookies.clear()
            for name, value in target_cookies.items():
                client.cookies.set(name, value)
            assert (await client.get("/api/v1/auth/me")).status_code == 401

            # But a fresh sign-in works.
            client.cookies.clear()
            login = await client.post(
                "/api/v1/auth/login", json={"username": "returning", "password": PASSWORD}
            )
            assert login.status_code == 200

    async def test_deactivating_an_inactive_account_is_idempotent(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        read_sql: ReadSql,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            target = await seed_user(client, username="already", is_active=False)
            token = await sign_in(client, "admin")

            first = await client.post(f"{USERS}/{target.id}/deactivate", headers=csrf(token))
            second = await client.post(f"{USERS}/{target.id}/deactivate", headers=csrf(token))

            assert first.status_code == second.status_code == 200
            # No spurious audit events: a no-op that recorded one would make
            # the trail say something happened when nothing did.
            events = await read_sql(
                client,
                "SELECT count(*) FROM audit_events WHERE action = 'user.deactivated'",
            )
            assert events[0][0] == 0

    async def test_status_can_also_be_changed_through_patch(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # Both paths route through one service method, so the invariant cannot
        # be enforced on one and forgotten on the other.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            target = await seed_user(client, username="viapatch")
            token = await sign_in(client, "admin")

            response = await client.patch(
                f"{USERS}/{target.id}", json={"is_active": False}, headers=csrf(token)
            )

            assert response.json()["is_active"] is False


# ---------------------------------------------------------------------------
# Administrative session revocation
# ---------------------------------------------------------------------------


class TestSessionRevocation:
    async def test_revokes_every_session_and_reports_the_count(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            target = await seed_user(client, username="busy")
            await sign_in(client, "busy")
            client.cookies.clear()
            await sign_in(client, "busy")
            client.cookies.clear()
            admin_csrf = await sign_in(client, "admin")

            response = await client.post(
                f"{USERS}/{target.id}/sessions/revoke", headers=csrf(admin_csrf)
            )

            assert response.status_code == 200
            assert response.json()["sessions_revoked"] == 2

    async def test_revoked_sessions_stop_working(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            target = await seed_user(client, username="busy")
            await sign_in(client, "busy")
            target_cookies = dict(client.cookies)

            client.cookies.clear()
            admin_csrf = await sign_in(client, "admin")
            await client.post(f"{USERS}/{target.id}/sessions/revoke", headers=csrf(admin_csrf))

            client.cookies.clear()
            for name, value in target_cookies.items():
                client.cookies.set(name, value)
            assert (await client.get("/api/v1/auth/me")).status_code == 401

    async def test_the_account_may_sign_in_again_afterwards(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # The difference from deactivation, asserted rather than described.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            target = await seed_user(client, username="busy")
            await sign_in(client, "busy")
            client.cookies.clear()
            admin_csrf = await sign_in(client, "admin")
            await client.post(f"{USERS}/{target.id}/sessions/revoke", headers=csrf(admin_csrf))

            client.cookies.clear()
            login = await client.post(
                "/api/v1/auth/login", json={"username": "busy", "password": PASSWORD}
            )
            assert login.status_code == 200

    async def test_revoking_with_no_live_sessions_reports_zero(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            target = await seed_user(client, username="idle")
            token = await sign_in(client, "admin")

            response = await client.post(
                f"{USERS}/{target.id}/sessions/revoke", headers=csrf(token)
            )

            assert response.status_code == 200
            assert response.json()["sessions_revoked"] == 0

    async def test_revoking_an_unknown_account_is_a_404_not_a_cheerful_zero(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # A revocation that reports success without having revoked anything is
        # the one lie this endpoint must not tell.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")

            response = await client.post(
                f"{USERS}/00000000-0000-4000-8000-000000000000/sessions/revoke",
                headers=csrf(token),
            )

            assert response.status_code == 404

    async def test_revocation_is_idempotent(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            target = await seed_user(client, username="busy")
            await sign_in(client, "busy")
            client.cookies.clear()
            admin_csrf = await sign_in(client, "admin")

            first = await client.post(
                f"{USERS}/{target.id}/sessions/revoke", headers=csrf(admin_csrf)
            )
            second = await client.post(
                f"{USERS}/{target.id}/sessions/revoke", headers=csrf(admin_csrf)
            )

            assert first.json()["sessions_revoked"] == 1
            assert second.json()["sessions_revoked"] == 0


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


class TestAuthorization:
    OPERATIONS = (
        ("GET", USERS, None),
        ("POST", USERS, {"username": "newbie", "password": PASSWORD}),
        ("GET", f"{USERS}/00000000-0000-4000-8000-000000000000", None),
        ("PATCH", f"{USERS}/00000000-0000-4000-8000-000000000000", {"role": "viewer"}),
        ("POST", f"{USERS}/00000000-0000-4000-8000-000000000000/deactivate", None),
        ("POST", f"{USERS}/00000000-0000-4000-8000-000000000000/reactivate", None),
        ("POST", f"{USERS}/00000000-0000-4000-8000-000000000000/sessions/revoke", None),
    )

    @pytest.mark.parametrize(("method", "path", "body"), OPERATIONS)
    @pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.OPERATOR])
    async def test_non_administrators_are_refused_every_operation(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        role: UserRole,
        method: str,
        path: str,
        body: dict[str, Any] | None,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="person", role=role)
            token = await sign_in(client, "person")

            response = await client.request(method, path, json=body, headers=csrf(token))

            assert response.status_code == 403, response.text
            assert response.json()["error"]["code"] == "insufficient_permissions"

    @pytest.mark.parametrize(("method", "path", "body"), OPERATIONS)
    async def test_an_anonymous_caller_is_refused_with_401(
        self,
        api_factory: ApiFactory,
        method: str,
        path: str,
        body: dict[str, Any] | None,
    ) -> None:
        # 401, not 403: the distinction tells a client whether logging in
        # again would help.
        async with api_factory() as client:
            response = await client.request(method, path, json=body)

            assert response.status_code == 401
            assert response.json()["error"]["code"] == "not_authenticated"

    async def test_a_deactivated_administrator_is_refused_even_with_a_cookie(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="first", role=UserRole.ADMIN)
            second = await seed_user(client, username="second", role=UserRole.ADMIN)

            await sign_in(client, "second")
            second_cookies = dict(client.cookies)

            client.cookies.clear()
            first_csrf = await sign_in(client, "first")
            await client.post(f"{USERS}/{second.id}/deactivate", headers=csrf(first_csrf))

            client.cookies.clear()
            for name, value in second_cookies.items():
                client.cookies.set(name, value)
            response = await client.get(USERS)

            assert response.status_code == 401

    async def test_a_denial_does_not_reveal_the_required_role(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="viewer", role=UserRole.VIEWER)
            await sign_in(client, "viewer")

            body = (await client.get(USERS)).text

            assert "admin" not in body.lower()
            assert "role" not in body.lower()

    async def test_a_viewer_cannot_list_accounts_even_knowing_an_id(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # Broken object-level authorization, checked from the other side: the
        # gate is the role, so holding a valid id grants nothing.
        async with api_factory() as client:
            admin = await seed_user(client, username="admin", role=UserRole.ADMIN)
            await seed_user(client, username="viewer", role=UserRole.VIEWER)
            await sign_in(client, "viewer")

            response = await client.get(f"{USERS}/{admin.id}")

            assert response.status_code == 403
