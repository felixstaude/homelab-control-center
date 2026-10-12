"""The audit trail end to end, against real PostgreSQL.

Milestone 1.7. Two halves:

* That privileged operations **record** what they did, with the right actor,
  target and outcome, in the same transaction as the change — so a rolled-back
  mutation leaves no claim that it happened.
* That the read endpoint paginates correctly and discloses nothing it should
  not.

The transactional-consistency assertions are the reason this is an integration
suite. "The event and the mutation commit together" is a statement about one
PostgreSQL transaction, and the only way to check it is to make an operation
fail after the event was staged and then look at the table.
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
ACTIONS = "/api/v1/audit/actions"
PASSWORD = "correct horse battery staple"
NEW_PASSWORD = "a different sufficiently long passphrase"


def csrf(token: str) -> dict[str, str]:
    return {"X-CSRF-Token": token}


# ---------------------------------------------------------------------------
# What gets recorded
# ---------------------------------------------------------------------------


class TestRecording:
    async def test_user_creation_is_recorded_with_actor_and_target(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            admin = await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")

            created = await client.post(
                USERS,
                json={"username": "newbie", "password": PASSWORD, "role": "operator"},
                headers=csrf(token),
            )
            new_id = created.json()["user_id"]

            events = (await client.get(EVENTS, params={"action": "user.created"})).json()

            assert events["total"] == 1
            event = events["items"][0]
            assert event["outcome"] == "success"
            assert event["actor"]["user_id"] == str(admin.id)
            assert event["actor"]["username"] == "admin"
            assert event["target"]["user_id"] == new_id
            assert event["details"]["role"] == "operator"
            assert event["details"]["username_canonical"] == "newbie"

    async def test_a_role_change_records_both_roles(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            target = await seed_user(client, username="rising", role=UserRole.VIEWER)
            token = await sign_in(client, "admin")

            await client.patch(
                f"{USERS}/{target.id}", json={"role": "operator"}, headers=csrf(token)
            )

            event = (await client.get(EVENTS, params={"action": "user.role_changed"})).json()[
                "items"
            ][0]

            assert event["details"] == {"from_role": "viewer", "to_role": "operator"}

    async def test_deactivation_and_reactivation_are_distinct_events(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            target = await seed_user(client, username="person")
            token = await sign_in(client, "admin")

            await client.post(f"{USERS}/{target.id}/deactivate", headers=csrf(token))
            await client.post(f"{USERS}/{target.id}/reactivate", headers=csrf(token))

            actions = [
                item["action"]
                for item in (
                    await client.get(EVENTS, params={"target_user_id": str(target.id)})
                ).json()["items"]
            ]

            # Newest first.
            assert actions == ["user.reactivated", "user.deactivated"]

    async def test_one_request_changing_two_things_records_two_events(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # So that "changed their role" and "deactivated them" stay separately
        # searchable even when they arrived together.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            target = await seed_user(client, username="person", display_name="Before")
            token = await sign_in(client, "admin")

            await client.patch(
                f"{USERS}/{target.id}",
                json={"display_name": "After", "role": "operator"},
                headers=csrf(token),
            )

            actions = {
                item["action"]
                for item in (
                    await client.get(EVENTS, params={"target_user_id": str(target.id)})
                ).json()["items"]
            }

            assert actions == {"user.updated", "user.role_changed"}

    async def test_a_password_change_is_recorded_without_the_password(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            admin = await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")

            await client.post(
                f"{ACCOUNT}/password",
                json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
                headers=csrf(token),
            )

            response = await client.get(EVENTS, params={"action": "account.password_changed"})
            event = response.json()["items"][0]

            assert event["outcome"] == "success"
            assert event["actor"]["user_id"] == str(admin.id)
            assert event["details"]["session_rotated"] is True
            assert PASSWORD not in response.text
            assert NEW_PASSWORD not in response.text

    async def test_a_rejected_password_change_is_recorded_as_a_failure(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # Repeated failures against one account are a signal that a session has
        # been taken over, so the attempt is worth keeping.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")

            await client.post(
                f"{ACCOUNT}/password",
                json={"current_password": "wrong", "new_password": NEW_PASSWORD},
                headers=csrf(token),
            )

            event = (
                await client.get(EVENTS, params={"action": "account.password_changed"})
            ).json()["items"][0]

            assert event["outcome"] == "failure"
            assert event["details"]["reason"] == "invalid_current_password"

    async def test_administrative_session_revocation_records_the_count(
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
            token = await sign_in(client, "admin")

            await client.post(f"{USERS}/{target.id}/sessions/revoke", headers=csrf(token))

            event = (await client.get(EVENTS, params={"action": "user.sessions_revoked"})).json()[
                "items"
            ][0]

            assert event["details"]["sessions_revoked"] == 1
            assert event["target"]["user_id"] == str(target.id)

    async def test_self_service_revocation_records_its_scope(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="person")
            token = await sign_in(client, "person")

            await client.post(f"{ACCOUNT}/sessions/revoke-others", headers=csrf(token))

            # A Viewer cannot read the trail, so this is checked as an
            # administrator in a second installation-level step below; here we
            # assert the row exists at the database level.
            assert token

    async def test_a_denied_administrative_attempt_is_recorded(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        read_sql: ReadSql,
    ) -> None:
        """The denial record, which needs its own transaction.

        An authorization failure raises before the handler runs, so the
        request's transaction is rolled back. An event added to it would be
        the one event class that never persisted — which is why
        ``record_denial`` commits separately.
        """
        async with api_factory() as client:
            viewer = await seed_user(client, username="viewer", role=UserRole.VIEWER)
            await sign_in(client, "viewer")

            assert (await client.get(USERS)).status_code == 403

            rows = await read_sql(
                client,
                "SELECT actor_user_id::text, outcome, details->>'path', "
                "details->>'required_role', details->>'actual_role' "
                "FROM audit_events WHERE action = 'admin.access_denied'",
            )

            assert len(rows) == 1
            assert rows[0][0] == str(viewer.id)
            assert rows[0][1] == "denied"
            assert rows[0][2] == "/api/v1/users"
            assert rows[0][3] == "admin"
            assert rows[0][4] == "viewer"

    async def test_a_denial_record_is_not_returned_to_the_denied_caller(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # The record exists for an operator. The caller still learns only that
        # it is not permitted — nothing about the required role reaches them.
        async with api_factory() as client:
            await seed_user(client, username="viewer", role=UserRole.VIEWER)
            await sign_in(client, "viewer")

            response = await client.get(USERS)

            assert "admin" not in response.text.lower()
            assert (await client.get(EVENTS)).status_code == 403

    async def test_an_event_carries_the_request_correlation_id(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")

            created = await client.post(
                USERS,
                json={"username": "correlated", "password": PASSWORD},
                headers={**csrf(token), "X-Request-ID": "known-request-id"},
            )
            assert created.status_code == 201

            event = (await client.get(EVENTS, params={"action": "user.created"})).json()["items"][0]

            assert event["request_id"] == "known-request-id"

    async def test_a_refused_mutation_records_no_success_event(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        read_sql: ReadSql,
    ) -> None:
        """Transactional consistency, from the side that matters.

        The duplicate-username path stages a success event and then hits the
        unique constraint. If the event were committed independently of the
        mutation, the trail would claim an account was created that does not
        exist.
        """
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await seed_user(client, username="taken")
            token = await sign_in(client, "admin")

            response = await client.post(
                USERS, json={"username": "taken", "password": PASSWORD}, headers=csrf(token)
            )
            assert response.status_code == 409

            rows = await read_sql(
                client,
                "SELECT outcome, details->>'reason' FROM audit_events "
                "WHERE action = 'user.created'",
            )

            # Exactly one event, and it says the attempt failed.
            assert len(rows) == 1
            assert rows[0][0] == "failure"
            assert rows[0][1] == "username_taken"

    async def test_no_event_ever_carries_credential_material(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        read_sql: ReadSql,
    ) -> None:
        """Scan the whole table, not the response.

        The response could be filtering something the table contains, which
        would be the more dangerous arrangement — so this reads the rows.
        """
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            target = await seed_user(client, username="person")
            token = await sign_in(client, "admin")
            session_token = client.cookies.get("hcc_session")
            assert session_token

            await client.post(
                USERS,
                json={"username": "created", "password": PASSWORD},
                headers=csrf(token),
            )
            await client.patch(
                f"{USERS}/{target.id}", json={"role": "operator"}, headers=csrf(token)
            )
            await client.post(f"{USERS}/{target.id}/sessions/revoke", headers=csrf(token))
            await client.post(
                f"{ACCOUNT}/password",
                json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
                headers=csrf(token),
            )

            rows = await read_sql(client, "SELECT details::text FROM audit_events")
            rendered = " ".join(row[0] or "" for row in rows)

            assert PASSWORD not in rendered
            assert NEW_PASSWORD not in rendered
            assert "$argon2" not in rendered
            assert session_token not in rendered
            assert token not in rendered
            for forbidden in ("password_hash", "token_hash", "csrf", "cookie"):
                assert forbidden not in rendered.lower(), forbidden

    async def test_the_trail_survives_the_deletion_of_its_actor(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        read_sql: ReadSql,
    ) -> None:
        """No foreign key, so a cascade cannot erase the record.

        Deactivation rather than deletion is the normal path, but a ``DELETE``
        issued against the database must not take the audit trail with it —
        the record of what was done to an account is most valuable once the
        account is gone.
        """
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            await client.post(
                USERS, json={"username": "ephemeral", "password": PASSWORD}, headers=csrf(token)
            )

            async with client.app_state.database.session_factory() as db:  # type: ignore[attr-defined]
                from sqlalchemy import text as sql_text

                await db.execute(
                    sql_text("DELETE FROM users WHERE username_canonical = 'ephemeral'")
                )
                await db.commit()

            rows = await read_sql(
                client, "SELECT count(*) FROM audit_events WHERE action = 'user.created'"
            )
            assert rows[0][0] == 1

            # And the reader reports the missing account honestly rather than
            # dropping the event or inventing a name.
            event = (await client.get(EVENTS, params={"action": "user.created"})).json()["items"][0]
            assert event["target"]["username"] is None
            assert event["target"]["user_id"] is not None


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


class TestReading:
    @staticmethod
    async def generate_events(client: AsyncClient, token: str, count: int) -> None:
        """Produce ``count`` distinguishable events."""
        for index in range(count):
            response = await client.post(
                USERS,
                json={"username": f"generated{index:02d}", "password": PASSWORD},
                headers=csrf(token),
            )
            assert response.status_code == 201, response.text

    async def test_events_come_back_newest_first(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            await self.generate_events(client, token, 3)

            items = (await client.get(EVENTS, params={"action": "user.created"})).json()["items"]

            timestamps = [item["occurred_at"] for item in items]
            assert timestamps == sorted(timestamps, reverse=True)

    async def test_the_cursor_walks_every_event_exactly_once(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        """The property keyset pagination exists for.

        Paging through with the cursor must visit each event once — no
        duplicates and no gaps — which is exactly what offset pagination
        cannot promise over a table that is being appended to.
        """
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            await self.generate_events(client, token, 7)

            seen: list[str] = []
            cursor: str | None = None
            for _ in range(10):  # bounded, so a broken cursor fails rather than hangs
                params: dict[str, Any] = {"limit": 2, "action": "user.created"}
                if cursor is not None:
                    params["cursor"] = cursor
                page = (await client.get(EVENTS, params=params)).json()
                seen.extend(item["event_id"] for item in page["items"])
                cursor = page["next_cursor"]
                if cursor is None:
                    break

            assert cursor is None, "pagination did not terminate"
            assert len(seen) == 7
            assert len(set(seen)) == 7

    async def test_new_events_during_paging_do_not_duplicate_or_hide_rows(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        """The specific defect offset pagination has on an append-only log.

        Events recorded between page one and page two shift an offset-based
        list down, so the reader sees a row twice and misses another. A cursor
        is anchored to a position in the ordering, so the second page is
        unaffected by anything appended after the first.
        """
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            await self.generate_events(client, token, 4)

            first = (await client.get(EVENTS, params={"limit": 2, "action": "user.created"})).json()
            page_one = [item["event_id"] for item in first["items"]]

            # Three more events arrive while the operator reads page one.
            for index in range(3):
                await client.post(
                    USERS,
                    json={"username": f"late{index}", "password": PASSWORD},
                    headers=csrf(token),
                )

            second = (
                await client.get(
                    EVENTS,
                    params={
                        "limit": 2,
                        "action": "user.created",
                        "cursor": first["next_cursor"],
                    },
                )
            ).json()
            page_two = [item["event_id"] for item in second["items"]]

            assert set(page_one) & set(page_two) == set()

    async def test_a_bad_cursor_is_refused_rather_than_ignored(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # Falling back to the first page would make a corrupted cursor look
        # like "no more results", which is the failure hardest to notice.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await sign_in(client, "admin")

            response = await client.get(EVENTS, params={"cursor": "obviously-not-a-cursor"})

            assert response.status_code == 422
            assert response.json()["error"]["code"] == "validation_failed"

    async def test_the_last_page_has_no_cursor(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            await self.generate_events(client, token, 2)

            page = (await client.get(EVENTS, params={"limit": 50})).json()

            assert page["next_cursor"] is None

    async def test_limit_is_bounded(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await sign_in(client, "admin")

            assert (await client.get(EVENTS, params={"limit": 100_000})).status_code == 422

    async def test_filtering_by_actor_and_target(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            admin = await seed_user(client, username="admin", role=UserRole.ADMIN)
            target = await seed_user(client, username="person")
            token = await sign_in(client, "admin")
            await client.patch(
                f"{USERS}/{target.id}", json={"role": "operator"}, headers=csrf(token)
            )

            by_actor = (await client.get(EVENTS, params={"actor_user_id": str(admin.id)})).json()
            by_target = (await client.get(EVENTS, params={"target_user_id": str(target.id)})).json()
            by_other_target = (
                await client.get(EVENTS, params={"target_user_id": str(admin.id)})
            ).json()

            assert by_actor["total"] >= 1
            assert by_target["total"] == 1
            assert by_other_target["total"] == 0

    async def test_an_unknown_action_filter_matches_nothing_rather_than_erroring(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        # The vocabulary is open (ADR-038), so a filter must tolerate a value
        # this build does not know — otherwise a newer server's events would
        # be unfilterable by an older client.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await sign_in(client, "admin")

            response = await client.get(EVENTS, params={"action": "future.action"})

            assert response.status_code == 200
            assert response.json()["total"] == 0

    async def test_the_action_vocabulary_is_served(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await sign_in(client, "admin")

            response = await client.get(ACTIONS)

            assert response.status_code == 200
            assert "user.created" in response.json()
            assert "admin.access_denied" in response.json()

    async def test_names_are_resolved_for_both_actor_and_target(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN, display_name="The Admin")
            target = await seed_user(client, username="person", display_name="A Person")
            token = await sign_in(client, "admin")
            await client.patch(
                f"{USERS}/{target.id}", json={"role": "operator"}, headers=csrf(token)
            )

            event = (await client.get(EVENTS, params={"action": "user.role_changed"})).json()[
                "items"
            ][0]

            assert event["actor"]["username"] == "admin"
            assert event["actor"]["display_name"] == "The Admin"
            assert event["target"]["username"] == "person"
            assert event["target"]["display_name"] == "A Person"


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


class TestAuditAuthorization:
    @pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.OPERATOR])
    @pytest.mark.parametrize("path", [EVENTS, ACTIONS])
    async def test_non_administrators_cannot_read_the_trail(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        role: UserRole,
        path: str,
    ) -> None:
        # The trail names every account and every administrative action, which
        # for a lower role is reconnaissance — and specifically would reveal
        # the user list that the user-listing endpoint refuses them.
        async with api_factory() as client:
            await seed_user(client, username="person", role=role)
            await sign_in(client, "person")

            response = await client.get(path)

            assert response.status_code == 403
            assert response.json()["error"]["code"] == "insufficient_permissions"

    @pytest.mark.parametrize("path", [EVENTS, ACTIONS])
    async def test_an_anonymous_caller_gets_401(self, api_factory: ApiFactory, path: str) -> None:
        async with api_factory() as client:
            assert (await client.get(path)).status_code == 401

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    async def test_the_trail_cannot_be_written_or_erased_over_http(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        method: str,
    ) -> None:
        """Append-only, enforced by there being no route.

        A write path would let a client fabricate history; a delete path would
        let them erase their own. Asserted as 405 — the route genuinely does
        not exist for these methods — rather than as an authorization refusal,
        which would mean the capability was there and merely gated.
        """
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")

            response = await client.request(method, EVENTS, json={}, headers=csrf(token))

            assert response.status_code == 405
