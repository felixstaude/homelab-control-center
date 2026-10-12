"""Monitoring configuration endpoints, against real PostgreSQL.

Phase 2. Three themes run through these, and each is about not lying to the
operator:

* **Saved is not active.** §23 draws the line this suite sits on: a
  configuration the controller has stored is pending until the agent applies
  it. Every write response carries the sync status, and the tests assert it
  says ``pending`` rather than implying success.
* **Absent, null and false are three states**, and they survive HTTP. A
  request that changes the interval must not reset ``enabled``; a request that
  sends ``null`` must clear the override rather than set it false.
* **Only an administrator may change anything.** Enabling a module starts
  collecting information about somebody's machine, so the role boundary is
  asserted on every mutating route rather than assumed from its placement.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from hcc.models.user import UserRole
from hcc.monitoring.modules import MODULE_IDS

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
MONITORING = "/api/v1/monitoring"


def csrf(token: str) -> dict[str, str]:
    """The header a state-changing request must carry."""
    return {"X-CSRF-Token": token}


async def create_host(client: AsyncClient, token: str, name: str = "homeserver") -> str:
    response = await client.post(HOSTS, json={"name": name}, headers=csrf(token))
    assert response.status_code == 201, response.text
    host_id: str = response.json()["host_id"]
    return host_id


async def module_of(client: AsyncClient, host_id: str, module_id: str) -> dict[str, Any]:
    """One module's entry from a host's monitoring response."""
    response = await client.get(f"{HOSTS}/{host_id}/monitoring")
    assert response.status_code == 200, response.text
    for entry in response.json()["modules"]:
        if entry["module_id"] == module_id:
            return dict(entry)
    raise AssertionError(f"{module_id} missing from the response")


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------


async def test_the_catalogue_lists_every_module_with_its_limits(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    async with api_factory() as client:
        await seed_user(client, username="viewer", role=UserRole.VIEWER)
        await sign_in(client, "viewer")

        response = await client.get(f"{MONITORING}/modules")
        assert response.status_code == 200, response.text
        body = response.json()

    assert [module["id"] for module in body["modules"]] == list(MODULE_IDS)
    assert body["registry_version"] >= 1
    assert body["min_interval_seconds"] == 1
    for module in body["modules"]:
        assert module["schedule_kinds"] == ["interval"]
        assert module["min_interval_seconds"] >= 1
        assert module["default_interval_seconds"] >= module["min_interval_seconds"]
        assert module["metrics"], module["id"]


async def test_the_catalogue_never_claims_a_module_is_enabled(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """There is no such field, and the response must not acquire one: the
    catalogue describes capability, never state.
    """
    async with api_factory() as client:
        await seed_user(client, username="viewer", role=UserRole.VIEWER)
        await sign_in(client, "viewer")
        body = (await client.get(f"{MONITORING}/modules")).json()

    for module in body["modules"]:
        assert "enabled" not in module


async def test_the_catalogue_needs_a_session(api_factory: ApiFactory) -> None:
    """Not public: the module list describes what this deployment can collect."""
    async with api_factory() as client:
        assert (await client.get(f"{MONITORING}/modules")).status_code == 401


# ---------------------------------------------------------------------------
# Default-deny, end to end
# ---------------------------------------------------------------------------


async def test_a_new_host_has_every_module_off_and_inherited(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """§2.1 through the real HTTP path: registering a host enables nothing."""
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")
        host_id = await create_host(client, token)

        body = (await client.get(f"{HOSTS}/{host_id}/monitoring")).json()

    assert [entry["module_id"] for entry in body["modules"]] == list(MODULE_IDS)
    for entry in body["modules"]:
        assert entry["enabled"]["value"] is False
        assert entry["enabled"]["source"] == "default"
        assert entry["host_override"] is None
        assert entry["global_default"] == {"enabled": None, "schedule": None}


async def test_a_new_host_has_no_configuration_revision_yet(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """``none``, not ``up_to_date`` with an empty configuration. A host that has
    never been told anything is a different state from one that has been told
    "collect nothing" and confirmed it.
    """
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")
        host_id = await create_host(client, token)

        body = (await client.get(f"{HOSTS}/{host_id}/monitoring")).json()

    assert body["sync"]["status"] == "none"
    assert body["sync"]["desired_version"] is None
    assert body["sync"]["reported_version"] is None


# ---------------------------------------------------------------------------
# Enabling a module
# ---------------------------------------------------------------------------


async def test_an_administrator_enables_a_module_on_a_host(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")
        host_id = await create_host(client, token)

        response = await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu",
            json={"enabled": True, "schedule": {"kind": "interval", "interval_seconds": 5}},
            headers=csrf(token),
        )
        assert response.status_code == 200, response.text
        saved = response.json()

        cpu = await module_of(client, host_id, "cpu")

    assert saved["revision_created"] is True
    assert saved["revision_version"] == 1
    assert cpu["enabled"] == {"value": True, "source": "host"}
    assert cpu["schedule"]["value"]["interval_seconds"] == 5
    assert cpu["schedule"]["source"] == "host"


async def test_saving_a_configuration_reports_it_as_pending_not_active(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """The §23 line. No agent is connected, so nothing has applied anything,
    and the response must not imply otherwise.
    """
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")
        host_id = await create_host(client, token)

        saved = (
            await client.put(
                f"{HOSTS}/{host_id}/monitoring/modules/cpu",
                json={"enabled": True},
                headers=csrf(token),
            )
        ).json()

    assert saved["sync"]["status"] == "pending"
    assert saved["sync"]["desired_version"] == 1
    assert saved["sync"]["reported_version"] is None
    assert saved["sync"]["acknowledged_at"] is None


async def test_the_resolved_schedule_carries_a_concrete_jitter(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """The agent merges nothing, so the API must not hand it a null to fill in."""
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")
        host_id = await create_host(client, token)
        await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu",
            json={"enabled": True, "schedule": {"kind": "interval", "interval_seconds": 30}},
            headers=csrf(token),
        )
        cpu = await module_of(client, host_id, "cpu")

    assert cpu["schedule"]["value"] == {
        "kind": "interval",
        "interval_seconds": 30,
        "jitter_seconds": 3,
    }


async def test_disabling_a_module_is_a_new_revision(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")
        host_id = await create_host(client, token)
        await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu",
            json={"enabled": True},
            headers=csrf(token),
        )
        off = (
            await client.put(
                f"{HOSTS}/{host_id}/monitoring/modules/cpu",
                json={"enabled": False},
                headers=csrf(token),
            )
        ).json()

        cpu = await module_of(client, host_id, "cpu")

    assert off["revision_created"] is True
    assert off["revision_version"] == 2
    assert cpu["enabled"] == {"value": False, "source": "host"}


async def test_writing_back_an_identical_setting_mints_no_revision(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """Idempotent. The configuration is correct either way; the response says
    whether anything the agent would notice actually changed.
    """
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")
        host_id = await create_host(client, token)
        body = {"enabled": True, "schedule": {"kind": "interval", "interval_seconds": 5}}
        first = (
            await client.put(
                f"{HOSTS}/{host_id}/monitoring/modules/cpu", json=body, headers=csrf(token)
            )
        ).json()
        again = (
            await client.put(
                f"{HOSTS}/{host_id}/monitoring/modules/cpu", json=body, headers=csrf(token)
            )
        ).json()

    assert first["revision_created"] is True
    assert again["revision_created"] is False
    assert again["revision_version"] == first["revision_version"]


# ---------------------------------------------------------------------------
# Absent, null and false
# ---------------------------------------------------------------------------


async def test_changing_only_the_interval_leaves_enabled_alone(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """An omitted field means "leave this alone". A model that collapsed absent
    into null would silently reset the module to inherited here.
    """
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")
        host_id = await create_host(client, token)
        await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu",
            json={"enabled": True},
            headers=csrf(token),
        )
        await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu",
            json={"schedule": {"kind": "interval", "interval_seconds": 20}},
            headers=csrf(token),
        )
        cpu = await module_of(client, host_id, "cpu")

    assert cpu["enabled"] == {"value": True, "source": "host"}
    assert cpu["schedule"]["value"]["interval_seconds"] == 20


async def test_an_explicit_null_clears_the_field_back_to_inherited(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """``null`` is a value: inherit. Distinct from both ``false`` and absent."""
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")
        host_id = await create_host(client, token)
        await client.put(f"{MONITORING}/defaults/cpu", json={"enabled": True}, headers=csrf(token))
        await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu",
            json={"enabled": False},
            headers=csrf(token),
        )
        assert (await module_of(client, host_id, "cpu"))["enabled"]["source"] == "host"

        await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu",
            json={"enabled": None},
            headers=csrf(token),
        )
        cpu = await module_of(client, host_id, "cpu")

    # Back to the global default's true, not to false.
    assert cpu["enabled"] == {"value": True, "source": "global"}
    assert cpu["host_override"] == {"enabled": None, "schedule": None}


async def test_a_host_false_and_an_inherited_false_are_distinguishable_over_http(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """The mission's §5 warning, asserted through the API rather than the
    resolver. Both hosts collect nothing; they differ in why, and the interface
    needs that difference to answer "is this host customised?".
    """
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")
        inherited = await create_host(client, token, name="inherited")
        overridden = await create_host(client, token, name="overridden")

        await client.put(f"{MONITORING}/defaults/cpu", json={"enabled": False}, headers=csrf(token))
        await client.put(
            f"{HOSTS}/{overridden}/monitoring/modules/cpu",
            json={"enabled": False},
            headers=csrf(token),
        )

        left = await module_of(client, inherited, "cpu")
        right = await module_of(client, overridden, "cpu")

    assert left["enabled"] == {"value": False, "source": "global"}
    assert right["enabled"] == {"value": False, "source": "host"}
    assert left["host_override"] is None
    assert right["host_override"]["enabled"] is False


async def test_an_unknown_field_in_the_body_is_refused(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """A typo must surface rather than silently doing nothing."""
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")
        host_id = await create_host(client, token)
        response = await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu",
            json={"enabld": True},
            headers=csrf(token),
        )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Resetting
# ---------------------------------------------------------------------------


async def test_a_reset_makes_the_host_inherit_again(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")
        host_id = await create_host(client, token)
        await client.put(
            f"{MONITORING}/defaults/cpu",
            json={"enabled": True, "schedule": {"kind": "interval", "interval_seconds": 30}},
            headers=csrf(token),
        )
        await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu",
            json={"enabled": False, "schedule": {"kind": "interval", "interval_seconds": 5}},
            headers=csrf(token),
        )

        response = await client.delete(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu", headers=csrf(token)
        )
        assert response.status_code == 200, response.text
        cpu = await module_of(client, host_id, "cpu")

    assert cpu["enabled"] == {"value": True, "source": "global"}
    assert cpu["schedule"]["value"]["interval_seconds"] == 30
    assert cpu["schedule"]["source"] == "global"
    assert cpu["host_override"] is None


async def test_a_reset_of_something_already_inherited_succeeds(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """Idempotent, and recorded — "did anyone try to change this?" is a
    question the trail should answer.
    """
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")
        host_id = await create_host(client, token)
        response = await client.delete(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu", headers=csrf(token)
        )
    assert response.status_code == 200, response.text
    assert response.json()["setting"] == {"enabled": None, "schedule": None}


async def test_a_reset_returns_the_resulting_sync_state(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """200 with a body, not 204: the caller needs to show what the reset did."""
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")
        host_id = await create_host(client, token)
        await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu",
            json={"enabled": True},
            headers=csrf(token),
        )
        body = (
            await client.delete(f"{HOSTS}/{host_id}/monitoring/modules/cpu", headers=csrf(token))
        ).json()

    assert body["sync"]["status"] == "pending"
    assert body["revision_version"] == 2
    assert body["revision_created"] is True


# ---------------------------------------------------------------------------
# Global defaults
# ---------------------------------------------------------------------------


async def test_global_defaults_list_every_module_including_unconfigured_ones(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    async with api_factory() as client:
        await seed_user(client, username="viewer", role=UserRole.VIEWER)
        await sign_in(client, "viewer")
        body = (await client.get(f"{MONITORING}/defaults")).json()

    assert [entry["module_id"] for entry in body["defaults"]] == list(MODULE_IDS)
    for entry in body["defaults"]:
        assert entry["setting"] == {"enabled": None, "schedule": None}


async def test_a_global_default_reaches_every_host_that_does_not_override_it(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")
        first = await create_host(client, token, name="one")
        second = await create_host(client, token, name="two")

        saved = (
            await client.put(
                f"{MONITORING}/defaults/memory",
                json={"enabled": True, "schedule": {"kind": "interval", "interval_seconds": 15}},
                headers=csrf(token),
            )
        ).json()

        left = await module_of(client, first, "memory")
        right = await module_of(client, second, "memory")

    assert saved["hosts_changed"] == 2
    for entry in (left, right):
        assert entry["enabled"] == {"value": True, "source": "global"}
        assert entry["schedule"]["value"]["interval_seconds"] == 15


async def test_a_global_default_reports_only_the_hosts_it_changed(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")
        plain = await create_host(client, token, name="plain")
        await create_host(client, token, name="customised")
        listed = (await client.get(HOSTS)).json()["items"]
        pinned = next(host["host_id"] for host in listed if host["name"] == "customised")

        await client.put(
            f"{HOSTS}/{pinned}/monitoring/modules/swap",
            json={"enabled": False},
            headers=csrf(token),
        )
        saved = (
            await client.put(
                f"{MONITORING}/defaults/swap", json={"enabled": True}, headers=csrf(token)
            )
        ).json()

        assert (await module_of(client, plain, "swap"))["enabled"]["value"] is True
        assert (await module_of(client, pinned, "swap"))["enabled"]["value"] is False

    assert saved["hosts_changed"] == 1


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def test_an_interval_below_the_module_floor_is_refused_with_every_problem(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")
        host_id = await create_host(client, token)
        response = await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/load",
            json={"enabled": True, "schedule": {"kind": "interval", "interval_seconds": 1}},
            headers=csrf(token),
        )

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"]["code"] == "validation_failed"
    problems = body["error"]["details"]["problems"]
    assert any("at least 5" in problem for problem in problems)


async def test_a_refused_schedule_leaves_the_configuration_untouched(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")
        host_id = await create_host(client, token)
        await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu",
            json={"enabled": True, "schedule": {"kind": "interval", "interval_seconds": 10}},
            headers=csrf(token),
        )
        await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu",
            json={"schedule": {"kind": "interval", "interval_seconds": 0}},
            headers=csrf(token),
        )
        cpu = await module_of(client, host_id, "cpu")

    assert cpu["schedule"]["value"]["interval_seconds"] == 10


async def test_a_calendar_schedule_on_a_sampler_is_refused(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")
        host_id = await create_host(client, token)
        response = await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu",
            json={
                "enabled": True,
                "schedule": {"kind": "calendar", "timezone": "UTC", "times": ["03:00"]},
            },
            headers=csrf(token),
        )

    assert response.status_code == 422, response.text
    problems = response.json()["error"]["details"]["problems"]
    assert any("does not accept a calendar schedule" in problem for problem in problems)


async def test_an_unknown_module_is_a_404_naming_the_catalogue(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """404 rather than 422: the identifier is a path segment, so an unknown one
    is a resource that does not exist.
    """
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")
        host_id = await create_host(client, token)
        response = await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/docker",
            json={"enabled": True},
            headers=csrf(token),
        )

    assert response.status_code == 404, response.text
    assert "/api/v1/monitoring/modules" in response.json()["error"]["message"]


async def test_an_unknown_host_is_a_404(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")
        missing = "11111111-2222-3333-4444-555555555555"
        for method, url in (
            ("GET", f"{HOSTS}/{missing}/monitoring"),
            ("GET", f"{HOSTS}/{missing}/monitoring/revisions"),
        ):
            assert (await client.request(method, url)).status_code == 404, url
        response = await client.put(
            f"{HOSTS}/{missing}/monitoring/modules/cpu",
            json={"enabled": True},
            headers=csrf(token),
        )
        assert response.status_code == 404


async def test_an_overlong_module_id_is_refused_before_it_is_looked_up(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")
        host_id = await create_host(client, token)
        response = await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/{'x' * 200}",
            json={"enabled": True},
            headers=csrf(token),
        )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.OPERATOR])
async def test_only_an_administrator_may_enable_a_module(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, role: UserRole
) -> None:
    """Enabling collection is the act that starts gathering information about
    somebody's machine, so it is administrator-only until a permissions model
    says otherwise (§2.6).
    """
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        admin_token = await sign_in(client, "admin")
        host_id = await create_host(client, admin_token)

        await seed_user(client, username="other", role=role)
        token = await sign_in(client, "other")

        for method, url, body in (
            ("PUT", f"{MONITORING}/defaults/cpu", {"enabled": True}),
            ("PUT", f"{HOSTS}/{host_id}/monitoring/modules/cpu", {"enabled": True}),
            ("DELETE", f"{HOSTS}/{host_id}/monitoring/modules/cpu", None),
        ):
            response = await client.request(method, url, json=body, headers=csrf(token))
            assert response.status_code == 403, f"{method} {url}: {response.text}"


@pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.OPERATOR, UserRole.ADMIN])
async def test_every_role_may_read_the_configuration(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, role: UserRole
) -> None:
    """A viewer who cannot see which modules collect cannot interpret a chart."""
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        admin_token = await sign_in(client, "admin")
        host_id = await create_host(client, admin_token)

        await seed_user(client, username="reader", role=role)
        await sign_in(client, "reader")

        for url in (
            f"{MONITORING}/modules",
            f"{MONITORING}/defaults",
            f"{HOSTS}/{host_id}/monitoring",
            f"{HOSTS}/{host_id}/monitoring/revisions",
        ):
            assert (await client.get(url)).status_code == 200, url


async def test_a_mutation_without_a_csrf_token_is_refused(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """The existing browser protections are not weakened by a new router."""
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")
        host_id = await create_host(client, token)
        response = await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu", json={"enabled": True}
        )
    assert response.status_code == 403


async def test_an_anonymous_caller_reaches_nothing(api_factory: ApiFactory) -> None:
    async with api_factory() as client:
        missing = "11111111-2222-3333-4444-555555555555"
        for url in (
            f"{MONITORING}/modules",
            f"{MONITORING}/defaults",
            f"{HOSTS}/{missing}/monitoring",
            f"{HOSTS}/{missing}/monitoring/revisions",
        ):
            assert (await client.get(url)).status_code == 401, url


# ---------------------------------------------------------------------------
# Audit trail and history
# ---------------------------------------------------------------------------


async def test_every_configuration_change_is_audited(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
) -> None:
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")
        host_id = await create_host(client, token)

        await client.put(f"{MONITORING}/defaults/cpu", json={"enabled": True}, headers=csrf(token))
        await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/memory",
            json={"enabled": True},
            headers=csrf(token),
        )
        await client.delete(f"{HOSTS}/{host_id}/monitoring/modules/memory", headers=csrf(token))

        rows = await read_sql(
            client,
            "SELECT action FROM audit_events WHERE action LIKE 'monitoring.%' ORDER BY action",
        )

    assert [row[0] for row in rows] == [
        "monitoring.global_default_changed",
        "monitoring.host_override_changed",
        "monitoring.host_override_reset",
    ]


async def test_an_audit_event_names_the_module_and_the_resulting_state(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
) -> None:
    """The resolved values, so the trail says what the setting now *is* rather
    than which fields the request happened to send.
    """
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")
        host_id = await create_host(client, token)
        await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu",
            json={"enabled": True, "schedule": {"kind": "interval", "interval_seconds": 5}},
            headers=csrf(token),
        )
        rows = await read_sql(
            client,
            "SELECT details, target_host_id FROM audit_events "
            "WHERE action = 'monitoring.host_override_changed'",
        )

    details, target = rows[0]
    assert details["module"] == "cpu"
    assert details["module_name"] == "CPU utilisation"
    assert details["enabled"] is True
    assert details["inherits_enabled"] is False
    assert details["schedule"]["interval_seconds"] == 5
    assert str(target) == host_id


async def test_a_denied_configuration_attempt_is_recorded(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn, read_sql: ReadSql
) -> None:
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        admin_token = await sign_in(client, "admin")
        host_id = await create_host(client, admin_token)

        await seed_user(client, username="viewer", role=UserRole.VIEWER)
        token = await sign_in(client, "viewer")
        response = await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu",
            json={"enabled": True},
            headers=csrf(token),
        )
        assert response.status_code == 403

        rows = await read_sql(
            client,
            "SELECT count(*) FROM audit_events WHERE action = 'admin.access_denied'",
        )
    assert rows[0][0] >= 1


async def test_the_revision_history_is_newest_first_and_paged(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")
        host_id = await create_host(client, token)
        for module_id in ("cpu", "memory", "swap"):
            await client.put(
                f"{HOSTS}/{host_id}/monitoring/modules/{module_id}",
                json={"enabled": True},
                headers=csrf(token),
            )

        body = (await client.get(f"{HOSTS}/{host_id}/monitoring/revisions?limit=2")).json()

    assert body["total"] == 3
    assert [entry["version"] for entry in body["revisions"]] == [3, 2]
    assert body["limit"] == 2


async def test_the_revision_history_never_returns_the_documents(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """A page of twenty would be most of a megabyte, and the interface needs
    the versions and their outcomes rather than the bytes.
    """
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")
        host_id = await create_host(client, token)
        await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu",
            json={"enabled": True},
            headers=csrf(token),
        )
        body = (await client.get(f"{HOSTS}/{host_id}/monitoring/revisions")).json()

    assert body["revisions"]
    for entry in body["revisions"]:
        assert "document" not in entry
        assert entry["checksum"].startswith("sha256:")


async def test_a_revision_page_beyond_the_limit_is_refused(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")
        host_id = await create_host(client, token)
        response = await client.get(f"{HOSTS}/{host_id}/monitoring/revisions?limit=5000")
    assert response.status_code == 422


async def test_the_revision_reason_is_recorded_for_the_operator(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")
        host_id = await create_host(client, token)
        await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu",
            json={"enabled": True},
            headers=csrf(token),
        )
        await client.put(
            f"{MONITORING}/defaults/memory", json={"enabled": True}, headers=csrf(token)
        )
        body = (await client.get(f"{HOSTS}/{host_id}/monitoring/revisions")).json()

    reasons = [entry["reason"] for entry in body["revisions"]]
    assert reasons == ["global default changed", "host override changed"]


async def test_no_configuration_response_carries_credential_material(
    api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
) -> None:
    """A blunt check, kept because the host router's enrollment endpoints are
    adjacent and a copied response model is how a secret escapes.
    """
    async with api_factory() as client:
        await seed_user(client, username="admin", role=UserRole.ADMIN)
        token = await sign_in(client, "admin")
        host_id = await create_host(client, token)
        await client.put(
            f"{HOSTS}/{host_id}/monitoring/modules/cpu",
            json={"enabled": True},
            headers=csrf(token),
        )
        bodies = [
            (await client.get(f"{MONITORING}/modules")).text,
            (await client.get(f"{MONITORING}/defaults")).text,
            (await client.get(f"{HOSTS}/{host_id}/monitoring")).text,
            (await client.get(f"{HOSTS}/{host_id}/monitoring/revisions")).text,
        ]

    for body in bodies:
        for forbidden in ("credential", "token_hash", "secret", "password"):
            assert forbidden not in body.lower(), forbidden
