"""The update management API, against real PostgreSQL.

What is asserted hardest is the **authorization boundary**. Every write here is
a decision about running different code on somebody's machine, so an operator
being able to start one would mean an operator can install a version of their
choosing on every host they can see. That is asserted by trying it, for every
write endpoint, rather than by reading the router.

The read side is asserted to be open to every role for the opposite reason: a
viewer who cannot see that the fleet is two versions behind cannot do their
job, and an interface whose most useful page is administrator-only is a worse
product.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

import pytest

from hcc.agents.protocol import PROTOCOL_VERSION
from hcc.models.user import UserRole

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from contextlib import AbstractAsyncContextManager

    from httpx import AsyncClient

    from hcc.models.user import User

    ApiFactory = Callable[..., AbstractAsyncContextManager[AsyncClient]]
    SeedUser = Callable[..., Awaitable[User]]
    SignIn = Callable[..., Awaitable[str]]

pytestmark = pytest.mark.integration

HOSTS = "/api/v1/hosts"
OVERVIEW = "/api/v1/updates/overview"
STATUSES = "/api/v1/updates/hosts"
HISTORY = "/api/v1/updates/history"
AMD64 = "linux/amd64"
DIGEST = "c" * 64


def csrf(token: str) -> dict[str, str]:
    return {"X-CSRF-Token": token}


def manifest_document(*versions: str) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "recommended": versions[-1],
        "minimum_supported": versions[0],
        "channels": {"stable": versions[-1]},
        "releases": {
            version: {
                "channel": "stable",
                "protocol_versions": [1, 2],
                "platforms": {
                    AMD64: {
                        "archive": f"hcc-agent_{version}.tar.gz",
                        "url": f"https://example.com/hcc-agent_{version}.tar.gz",
                        "sha256": DIGEST,
                        "size_bytes": 3_000_000,
                        "tier": "supported",
                    }
                },
            }
            for version in versions
        },
    }


async def a_host(client: AsyncClient, token: str, *, name: str = "homeserver") -> str:
    """A registered host with an agent that has reported a version."""
    host = (await client.post(HOSTS, json={"name": name}, headers=csrf(token))).json()
    issued = (
        await client.post(f"{HOSTS}/{host['host_id']}/enrollment", headers=csrf(token))
    ).json()
    await client.post(
        "/api/v1/agents/enroll",
        headers={"Authorization": f"Bearer {issued['credential']}"},
        json={"agent_version": "0.1.0", "platform": AMD64},
    )
    return str(host["host_id"])


class TestTheReadSideIsOpenToEveryRole:
    @pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.OPERATOR, UserRole.VIEWER])
    async def test_the_overview_is_readable(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, role: UserRole
    ) -> None:
        # A viewer who cannot see that the fleet is behind cannot do their job.
        async with api_factory() as client:
            await seed_user(client, username="reader", role=role)
            await sign_in(client, "reader")

            response = await client.get(OVERVIEW)

            assert response.status_code == 200, response.text
            body = response.json()
            assert body["controller_version"]
            assert body["channels"] == ["stable", "beta", "alpha"]

    async def test_an_anonymous_caller_is_refused(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            assert (await client.get(OVERVIEW)).status_code == 401
            assert (await client.get(STATUSES)).status_code == 401
            assert (await client.get(HISTORY)).status_code == 401

    async def test_the_overview_reports_no_metadata_distinctly(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        # "No release metadata is configured" and "no updates available" are
        # different answers, and the first is the state of this repository.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await sign_in(client, "admin")

            body = (await client.get(OVERVIEW)).json()

            assert body["metadata_available"] is False
            assert body["metadata_signature"] == "absent"
            assert body["update_available"] == 0

    async def test_the_overview_reports_the_signature_status(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, tmp_path: Any
    ) -> None:
        # Signed by a key this controller does not trust, which is the state of
        # a deployment that has metadata and no configured key — and it must
        # read as `untrusted_key` rather than `valid`, or the interface would
        # tell an administrator the metadata is authentic when nothing checked.
        from hcc.updates.signing import generate_keypair, sign_manifest

        document = json.dumps(manifest_document("0.1.0", "0.1.1"))
        path = tmp_path / "releases.json"
        path.write_text(document)
        private, _ = generate_keypair()
        (tmp_path / "releases.json.sig").write_text(
            json.dumps(sign_manifest(document.encode(), private))
        )

        async with api_factory(agent_release_manifest=path) as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await sign_in(client, "admin")

            body = (await client.get(OVERVIEW)).json()

            assert body["metadata_available"] is True
            assert body["metadata_signature"] == "untrusted_key"
            assert body["metadata_signature_detail"] is not None

    async def test_a_trusted_key_reports_valid(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, tmp_path: Any
    ) -> None:
        from hcc.updates.signing import encode_public_key, generate_keypair, sign_manifest

        document = json.dumps(manifest_document("0.1.0", "0.1.1"))
        path = tmp_path / "releases.json"
        path.write_text(document)
        private, public = generate_keypair()
        (tmp_path / "releases.json.sig").write_text(
            json.dumps(sign_manifest(document.encode(), private))
        )

        async with api_factory(
            agent_release_manifest=path, release_signing_keys=(encode_public_key(public),)
        ) as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await sign_in(client, "admin")

            body = (await client.get(OVERVIEW)).json()

            assert body["metadata_signature"] == "valid"

    async def test_a_hosts_status_names_its_plan_and_policy(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, tmp_path: Any
    ) -> None:
        path = tmp_path / "releases.json"
        path.write_text(json.dumps(manifest_document("0.1.0", "0.1.1")))

        async with api_factory(agent_release_manifest=path) as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host_id = await a_host(client, token)

            body = (await client.get(f"{HOSTS}/{host_id}/updates")).json()

            assert body["installed_version"] == "0.1.0"
            assert body["plan"]["outcome"] == "update_available"
            assert body["plan"]["target_version"] == "0.1.1"
            assert body["plan"]["step"] == "patch"
            # The default, and the most important one in the feature.
            assert body["policy"] == "manual"
            assert body["automatic"] is False
            assert body["maintenance_window"] is None

    async def test_a_blocked_update_is_reported_with_its_blockers(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, tmp_path: Any
    ) -> None:
        # The failure this prevents: an interface showing "no update" for a
        # host that is stuck, which makes a stuck fleet look current.
        document = manifest_document("0.1.0", "0.2.0")
        document["releases"]["0.2.0"]["protocol_versions"] = [99]
        path = tmp_path / "releases.json"
        path.write_text(json.dumps(document))

        async with api_factory(agent_release_manifest=path) as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host_id = await a_host(client, token)

            body = (await client.get(f"{HOSTS}/{host_id}/updates")).json()

            assert body["plan"]["outcome"] == "update_available_incompatible"
            codes = [blocker["code"] for blocker in body["plan"]["blockers"]]
            assert "protocol_mismatch" in codes
            assert body["plan"]["blockers"][0]["detail"]

    async def test_an_unknown_host_is_a_404(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await sign_in(client, "admin")

            response = await client.get(f"{HOSTS}/{uuid.uuid4()}/updates")

            assert response.status_code == 404
            assert response.json()["error"]["code"] == "not_found"

    async def test_the_history_is_empty_and_paginated_rather_than_absent(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await sign_in(client, "admin")

            body = (await client.get(HISTORY)).json()

            assert body["jobs"] == []
            # No cursor when the page was not full: offering one would cost a
            # request to learn there is nothing after it.
            assert body["next_before"] is None

    async def test_a_malformed_history_cursor_is_refused(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await sign_in(client, "admin")

            naive = await client.get(HISTORY, params={"before": "2026-10-14T12:00:00"})
            nonsense = await client.get(HISTORY, params={"before": "last Tuesday"})

            assert naive.status_code == 422
            assert "time zone" in naive.json()["error"]["message"]
            assert nonsense.status_code == 422

    async def test_an_absurd_history_limit_is_refused_by_the_schema(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await sign_in(client, "admin")

            assert (await client.get(HISTORY, params={"limit": 10_000})).status_code == 422
            assert (await client.get(HISTORY, params={"limit": 0})).status_code == 422


class TestEveryWriteIsAdministratorOnly:
    """Asserted by trying it, for every write endpoint.

    An operator who could start an update could install a version of their
    choosing on every host they can see, which is a larger privilege than the
    role is meant to carry.
    """

    @pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.VIEWER])
    async def test_setting_a_policy_is_refused(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, role: UserRole
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host_id = await a_host(client, token)

            await seed_user(client, username="other", role=role)
            other = await sign_in(client, "other")

            response = await client.put(
                f"{HOSTS}/{host_id}/update-policy",
                json={"policy": "patch", "channel": "stable"},
                headers=csrf(other),
            )

            assert response.status_code == 403
            assert response.json()["error"]["code"] == "insufficient_permissions"

    @pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.VIEWER])
    async def test_starting_an_update_is_refused(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, role: UserRole
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host_id = await a_host(client, token)

            await seed_user(client, username="other", role=role)
            other = await sign_in(client, "other")

            response = await client.post(f"{HOSTS}/{host_id}/updates", json={}, headers=csrf(other))

            assert response.status_code == 403

    @pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.VIEWER])
    async def test_cancelling_is_refused(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, role: UserRole
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="other", role=role)
            other = await sign_in(client, "other")

            response = await client.post(
                f"/api/v1/updates/jobs/{uuid.uuid4()}/cancel", json={}, headers=csrf(other)
            )

            # 403 rather than 404: the role check runs before the lookup, so a
            # non-administrator cannot use the endpoint to discover whether a
            # job identifier exists.
            assert response.status_code == 403

    @pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.VIEWER])
    async def test_expiring_is_refused(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, role: UserRole
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="other", role=role)
            other = await sign_in(client, "other")

            response = await client.post("/api/v1/updates/expire", headers=csrf(other))

            assert response.status_code == 403

    async def test_a_write_without_csrf_is_refused(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        # The session alone is not enough. Asserted here as well as in the
        # security suite, because every endpoint added has to be inside that
        # protection rather than beside it.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host_id = await a_host(client, token)

            response = await client.post(f"{HOSTS}/{host_id}/updates", json={})

            assert response.status_code == 403


class TestSettingAPolicy:
    async def test_an_administrator_enables_automatic_patch_updates(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host_id = await a_host(client, token)

            response = await client.put(
                f"{HOSTS}/{host_id}/update-policy",
                json={
                    "policy": "patch",
                    "channel": "beta",
                    "maintenance_window": {
                        "timezone": "Europe/Berlin",
                        "weekdays": [0],
                        "start": "02:00",
                        "end": "04:00",
                    },
                },
                headers=csrf(token),
            )

            assert response.status_code == 200, response.text
            body = response.json()
            assert body["policy"] == "patch"
            assert body["automatic"] is True
            assert body["channel"] == "beta"
            window = body["maintenance_window"]
            assert window["timezone"] == "Europe/Berlin"
            assert window["start"] == "02:00"
            # The zone is always named in the rendered description: a window
            # shown as "02:00-04:00" with the reader's offset assumed is how
            # somebody comes to believe an update ran at the wrong time.
            assert "Europe/Berlin" in window["description"]
            assert "Sunday" in window["description"]

    async def test_it_is_audited_with_whether_it_became_automatic(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host_id = await a_host(client, token)
            await client.put(
                f"{HOSTS}/{host_id}/update-policy",
                json={"policy": "minor", "channel": "stable"},
                headers=csrf(token),
            )

            events = (
                await client.get("/api/v1/audit/events", params={"action": "update.policy_changed"})
            ).json()

            assert events["total"] == 1
            event = events["items"][0]
            assert event["outcome"] == "success"
            # The actor comes from the resolved session, never from the body —
            # which is what makes the attribution trustworthy.
            assert event["actor"]["username"] == "admin"
            # `target` is an account and `target_host_id` is a host: an event
            # can have one, the other or neither. A policy change has no
            # account target, which is why this asserts the host field.
            assert event["target"] is None
            assert event["target_host_id"] == host_id
            # The substantive content: what the policy became, and whether it
            # made the host automatic — the fact somebody will want to find.
            assert event["details"]["policy"] == "minor"
            assert event["details"]["automatic"] is True

    async def test_a_pinned_policy_without_a_version_is_refused(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        # Refused by the request model, so the caller gets a 422 naming the
        # field rather than a 500 from the check constraint.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host_id = await a_host(client, token)

            response = await client.put(
                f"{HOSTS}/{host_id}/update-policy",
                json={"policy": "pinned", "channel": "stable"},
                headers=csrf(token),
            )

            assert response.status_code == 422

    async def test_a_version_without_the_pinned_policy_is_refused(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host_id = await a_host(client, token)

            response = await client.put(
                f"{HOSTS}/{host_id}/update-policy",
                json={"policy": "patch", "channel": "stable", "pinned_version": "0.1.0"},
                headers=csrf(token),
            )

            assert response.status_code == 422

    async def test_an_unknown_zone_is_refused_with_a_useful_message(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host_id = await a_host(client, token)

            response = await client.put(
                f"{HOSTS}/{host_id}/update-policy",
                json={
                    "policy": "patch",
                    "channel": "stable",
                    "maintenance_window": {
                        "timezone": "Mars/Olympus_Mons",
                        "weekdays": [],
                        "start": "02:00",
                        "end": "04:00",
                    },
                },
                headers=csrf(token),
            )

            assert response.status_code == 422
            assert "IANA" in response.json()["error"]["message"]

    async def test_a_nonsense_time_is_refused(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host_id = await a_host(client, token)

            response = await client.put(
                f"{HOSTS}/{host_id}/update-policy",
                json={
                    "policy": "patch",
                    "channel": "stable",
                    "maintenance_window": {
                        "timezone": "UTC",
                        "weekdays": [],
                        "start": "half past two",
                        "end": "04:00",
                    },
                },
                headers=csrf(token),
            )

            assert response.status_code == 422

    async def test_weekdays_are_sorted_and_deduplicated(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        # So two requests that mean the same thing store the same thing, and
        # the interface does not have to care what order a form submitted.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host_id = await a_host(client, token)

            body = (
                await client.put(
                    f"{HOSTS}/{host_id}/update-policy",
                    json={
                        "policy": "patch",
                        "channel": "stable",
                        "maintenance_window": {
                            "timezone": "UTC",
                            "weekdays": [6, 0, 6],
                            "start": "02:00",
                            "end": "04:00",
                        },
                    },
                    headers=csrf(token),
                )
            ).json()

            assert body["maintenance_window"]["weekdays"] == [0, 6]

    async def test_an_unknown_policy_is_refused_by_the_schema(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        # There is deliberately no "major" policy, and the schema is where
        # that is enforced rather than in a branch that could be reordered.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host_id = await a_host(client, token)

            for policy in ("major", "always", ""):
                response = await client.put(
                    f"{HOSTS}/{host_id}/update-policy",
                    json={"policy": policy, "channel": "stable"},
                    headers=csrf(token),
                )
                assert response.status_code == 422, policy


class TestStartingAndCancelling:
    async def test_an_administrator_starts_an_update(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, tmp_path: Any
    ) -> None:
        path = tmp_path / "releases.json"
        path.write_text(json.dumps(manifest_document("0.1.0", "0.1.1")))

        async with api_factory(agent_release_manifest=path) as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host_id = await a_host(client, token)

            response = await client.post(f"{HOSTS}/{host_id}/updates", json={}, headers=csrf(token))

            assert response.status_code == 201, response.text
            body = response.json()
            assert body["state"] == "queued"
            assert body["from_version"] == "0.1.0"
            assert body["to_version"] == "0.1.1"
            assert body["trigger"] == "manual"
            assert body["requested_by_user_id"] is not None

    async def test_a_second_start_is_a_409_naming_the_open_update(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, tmp_path: Any
    ) -> None:
        path = tmp_path / "releases.json"
        path.write_text(json.dumps(manifest_document("0.1.0", "0.1.1")))

        async with api_factory(agent_release_manifest=path) as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host_id = await a_host(client, token)
            await client.post(f"{HOSTS}/{host_id}/updates", json={}, headers=csrf(token))

            response = await client.post(f"{HOSTS}/{host_id}/updates", json={}, headers=csrf(token))

            assert response.status_code == 409
            assert response.json()["error"]["code"] == "update_already_running"
            # Naming it, because "an update is already running" without saying
            # which forces the administrator to go and look.
            assert "0.1.1" in response.json()["error"]["message"]

    async def test_starting_with_nothing_to_install_is_a_422_that_says_why(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, tmp_path: Any
    ) -> None:
        path = tmp_path / "releases.json"
        path.write_text(json.dumps(manifest_document("0.1.0")))

        async with api_factory(agent_release_manifest=path) as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host_id = await a_host(client, token)

            response = await client.post(f"{HOSTS}/{host_id}/updates", json={}, headers=csrf(token))

            assert response.status_code == 422
            assert response.json()["error"]["code"] == "update_not_possible"
            assert "up_to_date" in response.json()["error"]["message"]

    async def test_starting_a_blocked_update_names_every_blocker(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, tmp_path: Any
    ) -> None:
        document = manifest_document("0.1.0", "0.2.0")
        document["releases"]["0.2.0"]["protocol_versions"] = [99]
        document["releases"]["0.2.0"]["min_controller"] = "9.9.9"
        path = tmp_path / "releases.json"
        path.write_text(json.dumps(document))

        async with api_factory(agent_release_manifest=path) as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host_id = await a_host(client, token)

            response = await client.post(f"{HOSTS}/{host_id}/updates", json={}, headers=csrf(token))

            assert response.status_code == 422
            message = response.json()["error"]["message"]
            assert "protocol" in message
            assert "Upgrade the controller first" in message

    async def test_an_administrator_cancels_a_queued_update(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, tmp_path: Any
    ) -> None:
        path = tmp_path / "releases.json"
        path.write_text(json.dumps(manifest_document("0.1.0", "0.1.1")))

        async with api_factory(agent_release_manifest=path) as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host_id = await a_host(client, token)
            job = (
                await client.post(f"{HOSTS}/{host_id}/updates", json={}, headers=csrf(token))
            ).json()

            response = await client.post(
                f"/api/v1/updates/jobs/{job['job_id']}/cancel",
                json={"reason": "changed my mind"},
                headers=csrf(token),
            )

            assert response.status_code == 200, response.text
            assert response.json()["state"] == "cancelled"

            # And the slot is free again.
            again = await client.post(f"{HOSTS}/{host_id}/updates", json={}, headers=csrf(token))
            assert again.status_code == 201

    async def test_cancelling_an_unknown_job_is_a_404(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")

            response = await client.post(
                f"/api/v1/updates/jobs/{uuid.uuid4()}/cancel", json={}, headers=csrf(token)
            )

            assert response.status_code == 404

    async def test_the_history_records_the_attempt_with_its_events(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, tmp_path: Any
    ) -> None:
        path = tmp_path / "releases.json"
        path.write_text(json.dumps(manifest_document("0.1.0", "0.1.1")))

        async with api_factory(agent_release_manifest=path) as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host_id = await a_host(client, token)
            job = (
                await client.post(f"{HOSTS}/{host_id}/updates", json={}, headers=csrf(token))
            ).json()

            history = (await client.get(HISTORY)).json()
            events = (await client.get(f"/api/v1/updates/jobs/{job['job_id']}/events")).json()

            assert [entry["job_id"] for entry in history["jobs"]] == [job["job_id"]]
            # Ordered by the log's own sequence, which is why there is one:
            # several transitions share a transaction timestamp.
            assert [event["state"] for event in events] == ["available", "queued"]
            assert [event["sequence"] for event in events] == sorted(
                event["sequence"] for event in events
            )
            assert all(event["source"] == "controller" for event in events)

    async def test_events_for_an_unknown_job_are_a_404(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await sign_in(client, "admin")

            response = await client.get(f"/api/v1/updates/jobs/{uuid.uuid4()}/events")

            assert response.status_code == 404


class TestNoResponseCarriesASecret:
    async def test_no_update_response_has_a_credential_shaped_field(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, tmp_path: Any
    ) -> None:
        """Asserted against the real bodies rather than the schema.

        The update instruction carries release metadata and a publisher
        signature, both public by construction — the signature is verifiable
        precisely because the key is not a secret. Nothing else should be
        there, and this is the check that would notice a digest or a token
        creeping into a response somebody pasted into an issue.
        """
        import re

        path = tmp_path / "releases.json"
        path.write_text(json.dumps(manifest_document("0.1.0", "0.1.1")))
        forbidden = re.compile(r"credential|secret|private|password|token", re.IGNORECASE)

        async with api_factory(agent_release_manifest=path) as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            token = await sign_in(client, "admin")
            host_id = await a_host(client, token)
            job = (
                await client.post(f"{HOSTS}/{host_id}/updates", json={}, headers=csrf(token))
            ).json()

            for path_ in (
                OVERVIEW,
                STATUSES,
                HISTORY,
                f"{HOSTS}/{host_id}/updates",
                f"/api/v1/updates/jobs/{job['job_id']}/events",
            ):
                body = (await client.get(path_)).text
                assert not forbidden.search(body), path_

    async def test_the_protocol_version_the_controller_reports_is_current(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        # Not an update endpoint, and asserted here because the update
        # instruction gate reads it: an agent decides whether it may use
        # update frames from this number.
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await sign_in(client, "admin")

            body = (await client.get("/api/v1/agent-releases")).json()

            assert body["protocol_version"] == PROTOCOL_VERSION
            assert 1 in body["supported_protocol_versions"]
