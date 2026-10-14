"""GET /api/v1/agent-releases, against the real application.

Three things are worth testing here and the first is the one that matters
most: a controller with no release manifest — which is every build of this
project so far — must say so rather than produce a download command whose URL
would not resolve. A product that invents a plausible URL beside a digest
nobody can check is worse than one that says "build it from source".
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from hcc.agents.protocol import PROTOCOL_VERSION
from hcc.agents.releases import MANIFEST_SCHEMA_VERSION
from hcc.models.user import UserRole

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from contextlib import AbstractAsyncContextManager
    from pathlib import Path

    from httpx import AsyncClient

    from hcc.models.user import User

    ApiFactory = Callable[..., AbstractAsyncContextManager[AsyncClient]]
    SeedUser = Callable[..., Awaitable[User]]
    SignIn = Callable[..., Awaitable[str]]

pytestmark = pytest.mark.integration

RELEASES = "/api/v1/agent-releases"
DIGEST = "b" * 64


def manifest_bytes(**overrides: Any) -> bytes:
    document: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "recommended": "0.2.0",
        "minimum_supported": "0.1.0",
        "releases": {
            "0.2.0": {
                # Deliberately out of presentation order, to prove the
                # endpoint imposes its own rather than echoing the file's.
                "linux/arm": {
                    "archive": "hcc-agent_0.2.0_linux_arm.tar.gz",
                    "url": "https://example.test/hcc-agent_0.2.0_linux_arm.tar.gz",
                    "sha256": DIGEST,
                    "size_bytes": 2828214,
                    "tier": "best-effort",
                },
                "linux/amd64": {
                    "archive": "hcc-agent_0.2.0_linux_amd64.tar.gz",
                    "url": "https://example.test/hcc-agent_0.2.0_linux_amd64.tar.gz",
                    "sha256": DIGEST,
                    "size_bytes": 2931200,
                    "tier": "supported",
                },
            }
        },
    }
    document.update(overrides)
    return json.dumps(document).encode("utf-8")


class TestWithoutAManifest:
    async def test_it_reports_unavailable_with_a_reason(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await sign_in(client, "admin")

            response = await client.get(RELEASES)

            # 200, not 404. "No release has been published" is a fact about
            # this deployment, not an error in the request, and a 404 would
            # make the interface treat a healthy controller as broken.
            assert response.status_code == 200
            body = response.json()
            assert body["available"] is False
            assert body["platforms"] == []
            assert body["recommended_version"] is None
            assert "build the agent from source" in body["unavailable_reason"].lower()

    async def test_it_still_reports_the_protocol_version(
        self, api_factory: ApiFactory, seed_user: SeedUser, sign_in: SignIn
    ) -> None:
        async with api_factory() as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await sign_in(client, "admin")

            body = (await client.get(RELEASES)).json()

            # Useful regardless of whether a download is on offer: it is what
            # tells somebody whether an agent they built themselves can talk
            # to this controller.
            assert body["protocol_version"] == PROTOCOL_VERSION
            assert PROTOCOL_VERSION in body["supported_protocol_versions"]
            assert body["server_version"]


class TestWithAManifest:
    async def test_it_returns_the_recommended_version_and_its_platforms(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        tmp_path: Path,
    ) -> None:
        path = tmp_path / "agent-releases.json"
        path.write_bytes(manifest_bytes())

        async with api_factory(agent_release_manifest=path) as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await sign_in(client, "admin")

            body = (await client.get(RELEASES)).json()

            assert body["available"] is True
            assert body["unavailable_reason"] is None
            assert body["recommended_version"] == "0.2.0"
            assert body["minimum_supported_version"] == "0.1.0"
            # Presentation order, not the file's order: amd64 first, because
            # it is what most people need and a dropdown whose first entry
            # varies with the build is a dropdown that cannot be documented.
            assert [entry["platform"] for entry in body["platforms"]] == [
                "linux/amd64",
                "linux/arm",
            ]
            amd64 = body["platforms"][0]
            assert amd64["sha256"] == DIGEST
            assert amd64["archive"] == "hcc-agent_0.2.0_linux_amd64.tar.gz"
            assert amd64["tier"] == "supported"
            assert body["platforms"][1]["tier"] == "best-effort"

    async def test_a_recommended_version_with_no_platforms_reports_unavailable(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        tmp_path: Path,
    ) -> None:
        # A manifest whose `recommended` names a version it does not list.
        # Reported as unavailable rather than as an empty dropdown, which
        # would be an interface offering nothing with no explanation.
        path = tmp_path / "agent-releases.json"
        path.write_bytes(manifest_bytes(recommended="9.9.9"))

        async with api_factory(agent_release_manifest=path) as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await sign_in(client, "admin")

            body = (await client.get(RELEASES)).json()

            assert body["available"] is False
            assert "no architecture" in body["unavailable_reason"].lower()

    async def test_an_unusable_manifest_is_reported_rather_than_crashing(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        tmp_path: Path,
    ) -> None:
        path = tmp_path / "agent-releases.json"
        path.write_bytes(b"{not json")

        async with api_factory(agent_release_manifest=path) as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await sign_in(client, "admin")

            response = await client.get(RELEASES)

            assert response.status_code == 200
            body = response.json()
            assert body["available"] is False
            # A configured manifest that cannot be read is a deployment fault,
            # and the response says so differently from "none is configured" —
            # one needs fixing and the other is just the current state.
            assert "cannot be read" in body["unavailable_reason"]

    async def test_a_missing_manifest_file_is_reported_rather_than_crashing(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        tmp_path: Path,
    ) -> None:
        async with api_factory(agent_release_manifest=tmp_path / "absent.json") as client:
            await seed_user(client, username="admin", role=UserRole.ADMIN)
            await sign_in(client, "admin")

            body = (await client.get(RELEASES)).json()

            assert body["available"] is False
            assert "cannot be read" in body["unavailable_reason"]


class TestAuthorization:
    async def test_an_anonymous_caller_is_refused(self, api_factory: ApiFactory) -> None:
        async with api_factory() as client:
            assert (await client.get(RELEASES)).status_code == 401

    @pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.OPERATOR])
    async def test_a_non_administrator_is_refused(
        self,
        api_factory: ApiFactory,
        seed_user: SeedUser,
        sign_in: SignIn,
        role: UserRole,
    ) -> None:
        # It reveals this controller's version, which agent versions it
        # expects, and where their archives live — none of it useful to a
        # Viewer and all of it useful to somebody matching the deployment
        # against known vulnerabilities.
        async with api_factory() as client:
            await seed_user(client, username="person", role=role)
            await sign_in(client, "person")

            assert (await client.get(RELEASES)).status_code == 403
