"""Host registry endpoints, against real PostgreSQL.

Milestone 1.8. Against the real database rather than a mocked repository,
because the properties being asserted are properties of the schema and of
PostgreSQL: case-insensitive name uniqueness, the partial unique index that
makes "one live credential per host" true, the conditional updates that make a
revocation atomic. A mock would assert that the code calls the methods the
test expects, which is a restatement of the code.

Two themes run through these, and both are about not lying to the operator:

* **The enrollment credential is unrecoverable once issued.** Several tests
  assert the *absence* of the secret — in list responses, in the detail
  response, in the audit trail, in the row. An endpoint that leaked it would
  pass every behavioural test about enrollment working, so absence has to be
  asserted on purpose.
* **A revocation reports what it did.** "Revoked" and "there was nothing to
  revoke" are different answers, and the tests pin which status code each one
  gets.

Concurrency lives in ``test_host_concurrency_integration.py``, which needs
genuinely independent connections.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from hcc.models.user import UserRole
from hcc.security.tokens import TOKEN_LENGTH

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


def csrf(token: str) -> dict[str, str]:
    """The header a state-changing request must carry."""
    return {"X-CSRF-Token": token}


async def create_host(
    client: AsyncClient, token: str, name: str, **extra: object
) -> dict[str, Any]:
    """Register a host through the real endpoint and return the response body."""
    response = await client.post(HOSTS, json={"name": name, **extra}, headers=csrf(token))
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def issue(client: AsyncClient, token: str, host_id: str, **body: object) -> dict[str, Any]:
    """Issue an enrollment credential and return the response body."""
    response = await client.post(
        f"{HOSTS}/{host_id}/enrollment", json=body or None, headers=csrf(token)
    )
    assert response.status_code == 201, response.text
    payload: dict[str, Any] = response.json()
    return payload


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    async def test_a_new_host_is_registered_enabled_and_unenrolled(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """Registering a record must not imply an agent, or mint a secret.

        If creation also issued a credential, every mistyped host name would
        leave a live secret with a fifteen-minute life somewhere in a browser.
        """
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")

            body = await create_host(client, token, "homeserver")

            assert body["name"] == "homeserver"
            assert body["lifecycle"] == "registered"
            assert body["is_enabled"] is True
            assert body["connectivity"] == "offline"
            assert body["has_pending_enrollment"] is False
            assert body["credential_issued_at"] is None

    async def test_a_new_host_reports_no_agent_facts(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """Null, not a zero and not a placeholder date.

        Product principle 2: an unreported value must stay distinguishable
        from a measured one, all the way to the client.
        """
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")

            body = await create_host(client, token, "homeserver")

            for field in (
                "last_heartbeat_at",
                "last_connected_at",
                "last_disconnected_at",
                "agent_version",
                "platform",
                "protocol_version",
                "agent_started_at",
            ):
                assert body[field] is None, field

    async def test_the_name_is_normalized_and_the_display_form_preserved(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")

            body = await create_host(client, token, "  Backup   NAS  ")

            assert body["name"] == "Backup NAS"

    async def test_a_duplicate_name_is_refused_case_insensitively(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            await create_host(client, token, "homeserver")

            response = await client.post(HOSTS, json={"name": "HomeServer"}, headers=csrf(token))

            assert response.status_code == 409
            assert response.json()["error"]["code"] == "host_name_taken"

    async def test_an_invalid_name_is_a_422_not_a_500(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")

            response = await client.post(HOSTS, json={"name": "bad\x00name"}, headers=csrf(token))

            assert response.status_code == 422

    async def test_a_whitespace_only_description_is_stored_as_null(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """So "cleared" has one representation rather than two.

        An empty string and a null render identically and compare unequal,
        which is how a "has a note" check starts disagreeing with the note.
        """
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")

            body = await create_host(client, token, "homeserver", description="   ")

            assert body["description"] is None

    async def test_a_forwarded_internal_field_is_rejected_not_ignored(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """Mass assignment is impossible, and says so.

        A silently-dropped field is how a client comes to believe it changed
        something it did not — so `extra="forbid"` makes it a 422.
        """
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")

            for field, value in (
                ("lifecycle", "enrolled"),
                ("connection_epoch", 99),
                ("current_connection_id", "00000000-0000-0000-0000-000000000000"),
                ("host_id", "00000000-0000-0000-0000-000000000000"),
                ("is_enabled", False),
            ):
                response = await client.post(
                    HOSTS, json={"name": f"host-{field}", field: value}, headers=csrf(token)
                )
                assert response.status_code == 422, f"{field} was accepted"


# ---------------------------------------------------------------------------
# Listing and reading
# ---------------------------------------------------------------------------


class TestListing:
    async def test_every_role_may_list_hosts(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """A Viewer who cannot see the fleet cannot use a monitoring product."""
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await seed_user(client, username="viewer", role=UserRole.VIEWER)
            token = await sign_in(client, "admin")
            await create_host(client, token, "homeserver")
            await client.post("/api/v1/auth/logout", headers=csrf(token))

            await sign_in(client, "viewer")
            response = await client.get(HOSTS)

            assert response.status_code == 200
            assert response.json()["total"] == 1

    async def test_the_list_is_ordered_by_canonical_name(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """Alphabetical, not by connectivity.

        An order that moved rows as agents connected and dropped would be
        unusable: the row the operator was about to click would not be there.
        """
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            for name in ("zeta", "Alpha", "mid"):
                await create_host(client, token, name)

            body = (await client.get(HOSTS)).json()

            assert [item["name"] for item in body["items"]] == ["Alpha", "mid", "zeta"]

    async def test_the_search_filter_folds_case(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            await create_host(client, token, "HomeServer")
            await create_host(client, token, "backup-nas")

            body = (await client.get(HOSTS, params={"search": "HOME"})).json()

            assert [item["name"] for item in body["items"]] == ["HomeServer"]

    async def test_a_search_term_with_like_metacharacters_matches_literally(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """An unescaped `%` would match every host.

        Not a vulnerability, but a confusing result in the screen an operator
        uses to find one machine out of fifty.
        """
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            await create_host(client, token, "homeserver")

            body = (await client.get(HOSTS, params={"search": "%"})).json()

            assert body["items"] == []

    async def test_the_lifecycle_filter_narrows_the_list(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            await create_host(client, token, "homeserver")

            registered = (await client.get(HOSTS, params={"lifecycle": "registered"})).json()
            enrolled = (await client.get(HOSTS, params={"lifecycle": "enrolled"})).json()

            assert registered["total"] == 1
            assert enrolled["total"] == 0

    async def test_pagination_reports_a_total_independent_of_the_page(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            for index in range(5):
                await create_host(client, token, f"host-{index}")

            body = (await client.get(HOSTS, params={"limit": 2, "offset": 2})).json()

            assert body["total"] == 5
            assert len(body["items"]) == 2
            assert body["limit"] == 2
            assert body["offset"] == 2

    async def test_an_out_of_range_offset_is_a_422_not_a_500(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """Python integers are unbounded; a bigint column is not.

        Without the bound, `?offset=10**30` reaches asyncpg and fails as an
        out-of-range bigint — an unhandled 500 produced by one query
        parameter.
        """
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await sign_in(client, "admin")

            response = await client.get(HOSTS, params={"offset": 10**30})

            assert response.status_code == 422

    async def test_an_unknown_host_is_a_404(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await sign_in(client, "admin")

            response = await client.get(f"{HOSTS}/00000000-0000-0000-0000-000000000000")

            assert response.status_code == 404

    async def test_a_malformed_host_id_is_a_422(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await sign_in(client, "admin")

            response = await client.get(f"{HOSTS}/not-a-uuid")

            assert response.status_code == 422


# ---------------------------------------------------------------------------
# Role enforcement
# ---------------------------------------------------------------------------


class TestRoleEnforcement:
    @pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.OPERATOR])
    async def test_a_non_administrator_cannot_register_a_host(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        role: UserRole,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="person", role=role)
            token = await sign_in(client, "person")

            response = await client.post(HOSTS, json={"name": "homeserver"}, headers=csrf(token))

            assert response.status_code == 403
            assert response.json()["error"]["code"] == "insufficient_permissions"

    @pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.OPERATOR])
    async def test_a_non_administrator_cannot_mint_an_enrollment_credential(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        role: UserRole,
    ) -> None:
        """The escalation this split exists to prevent.

        An Operator who could issue enrollment credentials could enroll a
        machine of their own choosing and feed the installation fabricated
        metrics — which fire and suppress notification rules.
        """
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await seed_user(client, username="person", role=role)
            admin_token = await sign_in(client, "admin")
            host = await create_host(client, admin_token, "homeserver")
            await client.post("/api/v1/auth/logout", headers=csrf(admin_token))

            token = await sign_in(client, "person")
            response = await client.post(
                f"{HOSTS}/{host['host_id']}/enrollment", headers=csrf(token)
            )

            assert response.status_code == 403

    async def test_a_refused_mutation_is_recorded_in_the_audit_trail(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        read_sql: ReadSql,
    ) -> None:
        """A Viewer probing the host mutations is a security signal.

        Recorded because it means either a confused operator or a session
        being probed, and both are worth an operator seeing later.
        """
        async with api_factory() as client:
            await seed_user(client, username="viewer", role=UserRole.VIEWER)
            token = await sign_in(client, "viewer")

            await client.post(HOSTS, json={"name": "homeserver"}, headers=csrf(token))

            rows = await read_sql(
                client,
                "SELECT action, outcome FROM audit_events WHERE action = 'admin.access_denied'",
            )
            assert [tuple(row) for row in rows] == [("admin.access_denied", "denied")]

    async def test_a_mutation_without_a_csrf_token_is_refused(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await sign_in(client, "admin")

            response = await client.post(HOSTS, json={"name": "homeserver"})

            assert response.status_code == 403
            assert response.json()["error"]["code"] == "csrf_validation_failed"


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


class TestUpdating:
    async def test_a_partial_update_leaves_the_other_fields_alone(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host = await create_host(
                client, token, "homeserver", description="note", environment="homelab"
            )

            body = (
                await client.patch(
                    f"{HOSTS}/{host['host_id']}",
                    json={"environment": "production"},
                    headers=csrf(token),
                )
            ).json()

            assert body["description"] == "note"
            assert body["environment"] == "production"
            assert body["name"] == "homeserver"

    async def test_an_explicit_null_clears_a_label_and_omission_does_not(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """The distinction a plain default cannot express.

        Collapsing "absent" into "null" would make it impossible to clear a
        description; collapsing the other way would clear it on every edit.
        """
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host = await create_host(client, token, "homeserver", description="note")
            url = f"{HOSTS}/{host['host_id']}"

            untouched = (
                await client.patch(url, json={"environment": "x"}, headers=csrf(token))
            ).json()
            assert untouched["description"] == "note"

            cleared = (
                await client.patch(url, json={"description": None}, headers=csrf(token))
            ).json()
            assert cleared["description"] is None

    async def test_renaming_is_permitted_and_keeps_the_identity(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """A host name is a label, not an identity (ADR-041 does not apply).

        The id is what a credential is bound to, so a rename cannot change
        what any agent can do.
        """
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host = await create_host(client, token, "homeserver")

            body = (
                await client.patch(
                    f"{HOSTS}/{host['host_id']}",
                    json={"name": "renamed"},
                    headers=csrf(token),
                )
            ).json()

            assert body["name"] == "renamed"
            assert body["host_id"] == host["host_id"]

    async def test_renaming_onto_another_host_is_a_conflict(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            first = await create_host(client, token, "alpha")
            await create_host(client, token, "beta")

            response = await client.patch(
                f"{HOSTS}/{first['host_id']}", json={"name": "BETA"}, headers=csrf(token)
            )

            assert response.status_code == 409
            assert response.json()["error"]["code"] == "host_name_taken"

    async def test_an_empty_update_is_accepted_and_records_nothing(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        read_sql: ReadSql,
    ) -> None:
        """A no-op is not an error, and must not look like a change.

        Refusing it would mean the client had to decide whether its own diff
        was empty; recording it would make the trail claim an edit happened.
        """
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host = await create_host(client, token, "homeserver")

            response = await client.patch(
                f"{HOSTS}/{host['host_id']}", json={}, headers=csrf(token)
            )

            assert response.status_code == 200
            rows = await read_sql(
                client, "SELECT count(*) FROM audit_events WHERE action = 'host.updated'"
            )
            assert rows[0][0] == 0

    async def test_a_lifecycle_field_in_the_body_is_rejected(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """No field here may put a host into a state the system has not reached."""
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host = await create_host(client, token, "homeserver")

            for field, value in (
                ("lifecycle", "enrolled"),
                ("is_enabled", False),
                ("last_heartbeat_at", "2026-01-01T00:00:00Z"),
                ("connection_epoch", 5),
            ):
                response = await client.patch(
                    f"{HOSTS}/{host['host_id']}", json={field: value}, headers=csrf(token)
                )
                assert response.status_code == 422, f"{field} was accepted"


# ---------------------------------------------------------------------------
# Enable and disable
# ---------------------------------------------------------------------------


class TestEnableDisable:
    async def test_disabling_keeps_the_enrollment_and_flips_the_flag(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """Two axes, not one overloaded field.

        Disabling is "not right now" and is undone by enabling; revoking is
        "that secret is no longer trusted". A control that did both would
        force a consequence the operator was not told about.
        """
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host = await create_host(client, token, "homeserver")

            body = (
                await client.post(f"{HOSTS}/{host['host_id']}/disable", headers=csrf(token))
            ).json()

            assert body["is_enabled"] is False
            assert body["lifecycle"] == "registered"

    async def test_enabling_is_idempotent_and_records_nothing_when_unchanged(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        read_sql: ReadSql,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host = await create_host(client, token, "homeserver")

            response = await client.post(f"{HOSTS}/{host['host_id']}/enable", headers=csrf(token))

            assert response.status_code == 200
            assert response.json()["is_enabled"] is True
            rows = await read_sql(
                client, "SELECT count(*) FROM audit_events WHERE action = 'host.enabled'"
            )
            assert rows[0][0] == 0

    async def test_the_round_trip_is_recorded_once_each_way(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        read_sql: ReadSql,
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host = await create_host(client, token, "homeserver")
            url = f"{HOSTS}/{host['host_id']}"

            await client.post(f"{url}/disable", headers=csrf(token))
            await client.post(f"{url}/enable", headers=csrf(token))

            rows = await read_sql(
                client,
                "SELECT action FROM audit_events WHERE action LIKE 'host.%' "
                "ORDER BY occurred_at, action",
            )
            assert [row[0] for row in rows] == [
                "host.created",
                "host.disabled",
                "host.enabled",
            ]


# ---------------------------------------------------------------------------
# Issuing an enrollment credential
#
# The one response in the API that contains a secret.
# ---------------------------------------------------------------------------


class TestEnrollmentIssuance:
    async def test_the_credential_is_returned_once_with_an_expiry(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host = await create_host(client, token, "homeserver")

            body = await issue(client, token, host["host_id"])

            assert len(body["credential"]) == TOKEN_LENGTH
            assert body["expires_in_seconds"] > 0
            assert body["superseded"] == 0

    async def test_only_the_digest_is_stored(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        read_sql: ReadSql,
    ) -> None:
        """The plaintext must exist nowhere but that one response.

        Asserted against the row rather than against the code, because this is
        the property every other enrollment test would still pass without.
        """
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host = await create_host(client, token, "homeserver")

            body = await issue(client, token, host["host_id"])

            rows = await read_sql(client, "SELECT token_hash FROM host_enrollments")
            stored = rows[0][0]
            assert stored != body["credential"]
            assert len(stored) == 64
            assert body["credential"] not in stored

    async def test_the_credential_is_not_in_the_audit_trail(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        read_sql: ReadSql,
    ) -> None:
        """Not the plaintext, and not the digest either.

        The digest is the lookup key, so recording it would let anyone reading
        the trail confirm whether a credential they captured is the live one.
        """
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host = await create_host(client, token, "homeserver")

            body = await issue(client, token, host["host_id"])

            rows = await read_sql(
                client,
                "SELECT details::text FROM audit_events WHERE action = 'host.enrollment_issued'",
            )
            details = rows[0][0]
            assert body["credential"] not in details
            assert "token" not in details.lower()

    async def test_no_read_endpoint_returns_the_credential(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """There is no "show again", because there could not be one.

        The list and detail responses report *that* an enrollment is pending
        and nothing more.
        """
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host = await create_host(client, token, "homeserver")
            body = await issue(client, token, host["host_id"])
            credential = body["credential"]

            detail = await client.get(f"{HOSTS}/{host['host_id']}")
            listing = await client.get(HOSTS)

            assert credential not in detail.text
            assert credential not in listing.text
            assert detail.json()["has_pending_enrollment"] is True

    async def test_issuing_again_revokes_the_previous_pending_credential(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        read_sql: ReadSql,
    ) -> None:
        """So what is on screen is the only thing that works.

        Two live credentials would make "the enrollment credential" ambiguous:
        the administrator sees the new one while an older one, read over
        somebody's shoulder last week, still enrolls.
        """
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host = await create_host(client, token, "homeserver")

            first = await issue(client, token, host["host_id"])
            second = await issue(client, token, host["host_id"])

            assert first["superseded"] == 0
            assert second["superseded"] == 1
            rows = await read_sql(
                client,
                "SELECT count(*) FROM host_enrollments WHERE revoked_at IS NULL "
                "AND consumed_at IS NULL",
            )
            assert rows[0][0] == 1

    async def test_the_credential_is_never_in_a_url(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """A path or query parameter would put it in the browser's history,
        the access log, and the `Referer` of whatever was clicked next.
        """
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host = await create_host(client, token, "homeserver")

            response = await client.post(
                f"{HOSTS}/{host['host_id']}/enrollment", headers=csrf(token)
            )

            assert response.status_code == 201
            assert response.json()["credential"] not in str(response.url)

    async def test_issuing_for_an_unknown_host_is_a_404(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")

            response = await client.post(
                f"{HOSTS}/00000000-0000-0000-0000-000000000000/enrollment",
                headers=csrf(token),
            )

            assert response.status_code == 404

    async def test_issuing_for_a_disabled_host_is_permitted(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """Enrollment and connection are separate steps.

        Preparing a host before enabling it is a reasonable order of
        operations; the agent enrolls and is then refused at the door.
        """
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host = await create_host(client, token, "homeserver")
            await client.post(f"{HOSTS}/{host['host_id']}/disable", headers=csrf(token))

            body = await issue(client, token, host["host_id"])

            assert len(body["credential"]) == TOKEN_LENGTH

    async def test_the_expiry_honours_the_configured_lifetime(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """The setting has to reach the service.

        A service that reached for its own module default would ignore the
        injected configuration, which is the failure the app's dependency
        arrangement exists to prevent — so it is asserted rather than assumed.
        """
        async with api_factory(agent_enrollment_ttl_seconds=60) as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host = await create_host(client, token, "homeserver")

            body = await issue(client, token, host["host_id"])

            assert body["expires_in_seconds"] == 60


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


class TestRevocation:
    async def test_revoking_a_pending_credential_reports_the_count(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host = await create_host(client, token, "homeserver")
            await issue(client, token, host["host_id"])

            response = await client.post(
                f"{HOSTS}/{host['host_id']}/enrollment/revoke", headers=csrf(token)
            )

            assert response.status_code == 200
            assert response.json()["revoked"] == 1

    async def test_revoking_nothing_reports_zero_rather_than_claiming_success(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """ "Nothing was pending" and "one was revoked" are different answers."""
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host = await create_host(client, token, "homeserver")

            response = await client.post(
                f"{HOSTS}/{host['host_id']}/enrollment/revoke", headers=csrf(token)
            )

            assert response.status_code == 200
            assert response.json()["revoked"] == 0

    async def test_revoking_for_an_unknown_host_is_a_404_not_a_cheerful_zero(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """A revocation that reports success without revoking anything is the
        one thing this must not do.
        """
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")

            response = await client.post(
                f"{HOSTS}/00000000-0000-0000-0000-000000000000/enrollment/revoke",
                headers=csrf(token),
            )

            assert response.status_code == 404

    async def test_revoking_a_credential_a_host_does_not_have_is_a_conflict(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        """Not a 200.

        An interface that said "revoked" when nothing was revoked would tell
        an operator their host is secured when it is not.
        """
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host = await create_host(client, token, "homeserver")

            response = await client.post(
                f"{HOSTS}/{host['host_id']}/credential/revoke", headers=csrf(token)
            )

            assert response.status_code == 409
            assert response.json()["error"]["code"] == "no_live_credential"

    async def test_a_revoked_enrollment_is_kept_as_evidence(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        read_sql: ReadSql,
    ) -> None:
        """The row stays so "issued, then revoked unused" remains answerable.

        Deleting it would erase the evidence rather than record the outcome.
        """
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host = await create_host(client, token, "homeserver")
            await issue(client, token, host["host_id"])
            await client.post(f"{HOSTS}/{host['host_id']}/enrollment/revoke", headers=csrf(token))

            rows = await read_sql(
                client,
                "SELECT revoked_at IS NOT NULL, consumed_at IS NULL FROM host_enrollments",
            )

            assert [tuple(row) for row in rows] == [(True, True)]
