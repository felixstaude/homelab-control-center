"""Serving the web client from the control plane.

The deployment the security model is written for is **one origin**: the API
under ``/api``, the probes at the root, and everything else the client. This
suite is about the boundary between those three, because getting it wrong is
not a cosmetic failure — a catch-all that answered ``/readyz`` with HTML would
make a broken deployment report itself healthy, and one that answered
``/api/v1/typo`` with a 200 page would turn a client bug into a rendering bug.

``httpx.AsyncClient`` over ``ASGITransport``, like the rest of the suite.
``starlette.testclient`` is deliberately not used: this project's pinned
Starlette deprecates it in favour of a package the project does not depend on,
and ``filterwarnings = ["error"]`` turns that deprecation into a collection
error — which is the right outcome for a test reaching for a tool the project
does not use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from hcc.app import create_app
from hcc.config import Settings
from hcc.errors import register_exception_handlers
from hcc.web import ASSET_CACHE_SECONDS, WebClientMissingError, mount_web_client

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

INDEX = "<!doctype html><html><head><title>Homelab Control Center</title></head></html>"

#: A database URL for a Settings object that is never connected to. These
#: tests build applications and never start their lifespan, so nothing opens a
#: connection; the field is required, so a value has to exist.
UNUSED_DATABASE_URL = "postgresql://hcc:unused@localhost:5432/hcc"


@pytest.fixture
def web_root(tmp_path: Path) -> Path:
    """A directory shaped like `apps/web/dist` after a build."""
    (tmp_path / "index.html").write_text(INDEX, encoding="utf-8")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index-abc123.js").write_text("export const x = 1;\n", encoding="utf-8")
    (assets / "index-def456.css").write_text(":root{}\n", encoding="utf-8")
    (tmp_path / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    return tmp_path


def asgi_client(app: FastAPI) -> AsyncClient:
    """An HTTP client speaking to *app* in-process."""
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


@pytest.fixture
async def client(web_root: Path) -> AsyncIterator[AsyncClient]:
    """An application with one API route, one probe, and the client mounted."""
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/api/v1/thing")
    async def thing() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # After the routes, which is the contract mount_web_client documents.
    mount_web_client(app, web_root, api_prefix="/api")
    async with asgi_client(app) as http:
        yield http


class TestTheClientIsServed:
    async def test_the_root_returns_the_document(self, client: AsyncClient) -> None:
        response = await client.get("/")

        assert response.status_code == 200
        assert "Homelab Control Center" in response.text
        assert response.headers["content-type"].startswith("text/html")

    async def test_a_client_route_returns_the_document_rather_than_a_404(
        self, client: AsyncClient
    ) -> None:
        # The SPA owns its own routing, so an unmatched path is a page the
        # *browser* resolves. Returning a 404 here would make every deep link
        # into the interface fail on a page refresh.
        for path in ["/hosts", "/settings/users", "/hosts/3f2c8a14/metrics"]:
            response = await client.get(path)
            assert response.status_code == 200, path
            assert "Homelab Control Center" in response.text

    async def test_the_document_is_never_cached(self, client: AsyncClient) -> None:
        # It is the file that names the hashed bundles. A cached copy pins the
        # browser to the previous deployment's JavaScript, which after an
        # upgrade that changed the API is a client talking to a server it does
        # not match.
        assert (await client.get("/")).headers["cache-control"] == "no-store"
        assert (await client.get("/hosts")).headers["cache-control"] == "no-store"

    async def test_hashed_assets_are_immutable_for_a_year(self, client: AsyncClient) -> None:
        response = await client.get("/assets/index-abc123.js")

        assert response.status_code == 200
        # Safe only because Vite puts a content hash in every filename: a
        # changed file has a changed name, so a cached response cannot be
        # stale.
        assert response.headers["cache-control"] == (
            f"public, max-age={ASSET_CACHE_SECONDS}, immutable"
        )


class TestItDoesNotShadowTheApplication:
    async def test_an_api_route_still_answers(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/thing")

        assert response.status_code == 200
        assert response.json() == {"ok": "yes"}

    async def test_an_unknown_api_path_is_a_json_404_not_a_page(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/typo")

        assert response.status_code == 404
        # The envelope, not HTML. A 200 page here would make a client bug look
        # like a rendering bug, and would make a monitoring check on the API go
        # green against the login screen.
        assert response.json()["error"]["code"] == "not_found"
        assert "text/html" not in response.headers.get("content-type", "")

    async def test_the_probes_still_answer(self, client: AsyncClient) -> None:
        response = await client.get("/healthz")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    async def test_an_unknown_probe_path_is_a_404_not_a_page(self, client: AsyncClient) -> None:
        # /readyz is not registered in this fixture, so without the reserved
        # list it would fall through to the client — and a monitoring check
        # watching it would report a deployment with no readiness endpoint as
        # ready.
        response = await client.get("/readyz")

        assert response.status_code == 404
        assert "text/html" not in response.headers.get("content-type", "")

    async def test_the_openapi_path_is_never_the_client(self, client: AsyncClient) -> None:
        # FastAPI registers /openapi.json itself, so the real route wins —
        # which is the point. What must never happen is the fallback answering
        # it with an HTML document that a tool fetching the schema will fail to
        # parse with a confusing message.
        response = await client.get("/openapi.json")

        assert "text/html" not in response.headers.get("content-type", "")
        assert "Homelab Control Center</title>" not in response.text


class TestItServesNothingElse:
    @pytest.mark.parametrize(
        "path",
        [
            "/assets/../index.html",
            "/assets/../../etc/passwd",
            "/assets/%2e%2e/%2e%2e/etc/passwd",
            "/assets/....//etc/passwd",
        ],
    )
    async def test_traversal_out_of_the_assets_directory_is_refused(
        self, client: AsyncClient, path: str
    ) -> None:
        response = await client.get(path)

        # Starlette's StaticFiles resolves and bounds every path inside the
        # root. A 200 here with /etc/passwd in it would be a file-disclosure
        # vulnerability in a monitoring console.
        #
        # 404 or a redirect are both acceptable answers; what is not is a 200
        # carrying something from outside the root, which the body assertion
        # covers whichever way the path was normalised.
        assert response.status_code in {200, 307, 404}, path
        assert "root:" not in response.text
        assert "/bin/" not in response.text

    async def test_only_get_reaches_the_client(self, client: AsyncClient) -> None:
        # The fallback is registered for GET alone. A POST to an unknown path
        # must not be answered with a 200 page, because a client that posted to
        # the wrong URL would then see success.
        for method in ("post", "put", "patch", "delete"):
            response = await client.request(method.upper(), "/hosts")
            assert response.status_code == 405, method


class TestItRefusesToStartWithoutABuild:
    def test_a_missing_directory_is_a_startup_failure(self, tmp_path: Path) -> None:
        app = FastAPI()

        with pytest.raises(WebClientMissingError, match="not a directory"):
            mount_web_client(app, tmp_path / "absent", api_prefix="/api")

    def test_a_directory_without_an_index_is_a_startup_failure(self, tmp_path: Path) -> None:
        # The shape a failed `npm run build` leaves behind. Refused loudly,
        # because the alternative is a container that reports itself ready and
        # answers every page with a 404 — healthy, and useless.
        (tmp_path / "assets").mkdir()
        app = FastAPI()

        with pytest.raises(WebClientMissingError, match=r"no index\.html"):
            mount_web_client(app, tmp_path, api_prefix="/api")

    async def test_a_build_with_no_assets_directory_still_starts(self, tmp_path: Path) -> None:
        # A client built with everything inlined has no assets directory.
        # Refusing to start over a convention would be the wrong call.
        (tmp_path / "index.html").write_text(INDEX, encoding="utf-8")
        app = FastAPI()
        register_exception_handlers(app)

        mount_web_client(app, tmp_path, api_prefix="/api")

        async with asgi_client(app) as http:
            assert (await http.get("/")).status_code == 200


class TestTheApplicationWiresItUp:
    async def test_an_unset_web_root_serves_no_client(self) -> None:
        # The development arrangement: Vite serves the client on its own port
        # and `cors_origins` names it. The API must not then answer a client
        # route with anything, because there is nothing to answer with.
        app = create_app(
            Settings(
                database_url=UNUSED_DATABASE_URL,
                environment="test",
            )
        )

        async with asgi_client(app) as http:
            assert (await http.get("/some/client/route")).status_code == 404

    async def test_a_configured_web_root_is_served_without_shadowing_the_api(
        self, web_root: Path
    ) -> None:
        app = create_app(
            Settings(
                database_url=UNUSED_DATABASE_URL,
                environment="test",
                web_root=web_root,
            )
        )

        async with asgi_client(app) as http:
            assert "Homelab Control Center" in (await http.get("/hosts")).text
            assert (await http.get("/healthz")).status_code == 200

        # And the API is still the API, which is the whole point of the
        # ordering in create_app.
        #
        # `raise_app_exceptions=False`, because the API route reached here
        # fails for want of a database and httpx would otherwise re-raise that
        # out of the transport. The 500 is the expected outcome and is the
        # evidence: a route that *matched* and failed proves the fallback did
        # not swallow it, which a 200 carrying index.html would not.
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://testserver",
        ) as http:
            api = await http.get("/api/v1/info")
            assert api.status_code == 500
            assert "text/html" not in api.headers.get("content-type", "")
            assert "Homelab Control Center</title>" not in api.text
