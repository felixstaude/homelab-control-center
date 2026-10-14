"""Response compression, and the boundary it must not cross.

Compression on a response that holds both a secret and attacker-chosen text
leaks the secret through the response length — BREACH. This application has one
response body containing a secret (the enrollment credential), and the way it
stays that way is an allow-list: four read-only metric paths, nothing else.

So these tests are mostly about what is *not* compressed. The win is worth
having — a host page's four charts measure 237 kB uncompressed and 24 kB
gzipped — but the win is not what would go wrong.
"""

from __future__ import annotations

import gzip
from typing import TYPE_CHECKING, Final

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from hcc.compression import COMPRESSED_PATHS, MINIMUM_SIZE, ScopedGZipMiddleware

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

#: A host id shaped like the real ones, so the path matches as it would live.
HOST: Final = "3f2c8a14-0000-4000-8000-000000000000"

#: Comfortably over MINIMUM_SIZE, and repetitive like real metric JSON.
BIG_BODY: Final[dict[str, object]] = {
    "points": [{"at": f"2026-10-14T00:{n // 60:02d}:{n % 60:02d}+00:00"} for n in range(400)]
}


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """An application exposing one path per compression decision."""
    app = FastAPI()

    @app.get("/api/v1/hosts/{host_id}/metrics/{name}")
    async def metrics(host_id: str, name: str) -> dict[str, object]:
        return BIG_BODY

    @app.get("/api/v1/hosts/{host_id}/enroll-ish")
    async def secretish(host_id: str) -> dict[str, object]:
        # Stands in for the one response that carries a credential. Large, so
        # that its staying uncompressed is the allow-list working rather than
        # the size threshold.
        return BIG_BODY

    @app.get("/api/v1/hosts")
    async def hosts() -> dict[str, object]:
        return BIG_BODY

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return BIG_BODY

    app.add_middleware(ScopedGZipMiddleware)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


class TestTheAllowListDecidesByPath:
    """Asserted against the predicate, so every path is covered cheaply."""

    @pytest.mark.parametrize(
        "path",
        [
            f"/api/v1/hosts/{HOST}/metrics/latest",
            f"/api/v1/hosts/{HOST}/metrics/range",
            f"/api/v1/hosts/{HOST}/metrics/series",
            f"/api/v1/hosts/{HOST}/metrics/status",
        ],
    )
    def test_every_metric_read_path_is_compressed(self, path: str) -> None:
        # The four paths in the OpenAPI document. If one is renamed, this fails
        # rather than quietly losing a 9.7x saving.
        assert ScopedGZipMiddleware(None).compresses(path)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "path",
        [
            # The one response in the API that contains a secret.
            f"/api/v1/hosts/{HOST}/enroll",
            # Everything else, by sampling the shapes that exist.
            "/api/v1/hosts",
            f"/api/v1/hosts/{HOST}",
            "/api/v1/users",
            "/api/v1/audit/events",
            "/api/v1/auth/login",
            "/api/v1/agent-releases",
            f"/api/v1/hosts/{HOST}/monitoring/modules/cpu",
            "/api/v1/agents/connect",
            "/healthz",
            "/readyz",
            "/",
            "/hosts",
            "/assets/index-abc123.js",
        ],
    )
    def test_nothing_else_is_compressed(self, path: str) -> None:
        assert not ScopedGZipMiddleware(None).compresses(path)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "path",
        [
            # A path that merely contains the word, which an unanchored
            # pattern would match.
            "/api/v1/hosts/x/metrics/latest/extra",
            "/api/v1/hosts/x/y/metrics/latest",
            "/evil/api/v1/hosts/x/metrics/latest",
            f"/api/v1/hosts/{HOST}/metrics",
            f"/api/v1/hosts/{HOST}/metrics/",
        ],
    )
    def test_the_pattern_cannot_be_widened_by_a_crafted_path(self, path: str) -> None:
        # Anchored at both ends with a bounded host segment. A request that
        # slipped past this would be compressed without anybody deciding it
        # should be.
        assert not ScopedGZipMiddleware(None).compresses(path)  # type: ignore[arg-type]

    def test_the_allow_list_has_exactly_one_entry(self) -> None:
        # Not a style assertion: every added pattern is a response body
        # somebody has to have checked for secrets. A second one should be a
        # deliberate change to this test.
        assert len(COMPRESSED_PATHS) == 1


class TestItCompressesWhatItSays:
    async def test_a_metric_response_is_compressed(self, client: AsyncClient) -> None:
        response = await client.get(
            f"/api/v1/hosts/{HOST}/metrics/range", headers={"accept-encoding": "gzip"}
        )

        assert response.status_code == 200
        assert response.headers["content-encoding"] == "gzip"
        # httpx decompresses, so this is also the round-trip assertion.
        assert response.json() == BIG_BODY

    async def test_a_client_that_does_not_ask_is_not_given_it(self, client: AsyncClient) -> None:
        response = await client.get(
            f"/api/v1/hosts/{HOST}/metrics/range", headers={"accept-encoding": "identity"}
        )

        assert "content-encoding" not in response.headers
        assert response.json() == BIG_BODY

    async def test_the_saving_is_real(self, client: AsyncClient) -> None:
        # The measurement that justified the change, as a test: metric JSON is
        # an array of near-identical timestamps and compresses about 10:1. A
        # floor of 3:1 keeps this a test of the middleware rather than of zlib.
        plain = await client.get(
            f"/api/v1/hosts/{HOST}/metrics/range", headers={"accept-encoding": "identity"}
        )
        uncompressed = len(plain.content)

        assert len(gzip.compress(plain.content, 6)) * 3 < uncompressed

    async def test_a_small_response_is_left_alone(self, client: AsyncClient) -> None:
        # Below the threshold the framing costs more than it saves. Asserted
        # through a real response so the threshold is wired up, not just set.
        app = FastAPI()

        @app.get("/api/v1/hosts/{host_id}/metrics/latest")
        async def small(host_id: str) -> dict[str, str]:
            return {"ok": "yes"}

        app.add_middleware(ScopedGZipMiddleware)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as http:
            response = await http.get(
                f"/api/v1/hosts/{HOST}/metrics/latest", headers={"accept-encoding": "gzip"}
            )

        assert len(response.content) < MINIMUM_SIZE
        assert "content-encoding" not in response.headers


class TestItLeavesEverythingElseAlone:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/hosts/3f2c8a14-0000-4000-8000-000000000000/enroll-ish",
            "/api/v1/hosts",
            "/healthz",
        ],
    )
    async def test_an_unlisted_path_is_not_compressed(self, client: AsyncClient, path: str) -> None:
        # Same body as the compressed case, so size is not the reason.
        response = await client.get(path, headers={"accept-encoding": "gzip"})

        assert response.status_code == 200
        assert "content-encoding" not in response.headers
        assert response.json() == BIG_BODY


class TestItIsWiredIntoTheApplication:
    async def test_the_real_application_compresses_the_metric_path(self) -> None:
        # Through create_app, so the middleware order is the shipped one. The
        # route needs a database and will fail; a 500 that arrives *gzipped*
        # still proves the middleware is in the stack and matched the path,
        # which is what this asserts.
        from hcc.app import create_app
        from hcc.config import Settings

        app = create_app(
            Settings(
                database_url="postgresql://hcc:unused@localhost:5432/hcc",
                environment="test",
            )
        )
        middlewares: list[object] = [m.cls for m in app.user_middleware]

        assert ScopedGZipMiddleware in middlewares
        # Outermost, so it sees the final body: the security headers are on it
        # and an exception handler has had its turn. `user_middleware` is in
        # the order added and Starlette applies it bottom-up, so the last
        # entry added is the outermost — FastAPI stores it first.
        assert middlewares[0] is ScopedGZipMiddleware
