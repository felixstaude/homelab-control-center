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

from typing import TYPE_CHECKING, Final

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from hcc.app import create_app
from hcc.config import Settings
from hcc.errors import register_exception_handlers
from hcc.web import (
    ASSET_CACHE_SECONDS,
    ASSET_DIRECTORY,
    WebClientMissingError,
    mount_web_client,
)

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


async def wire_bytes(app: FastAPI, path: str, *, accept_encoding: str | None) -> tuple[int, str]:
    """How many body bytes *app* actually sends for *path*, and the encoding.

    An HTTP client cannot answer this. httpx decompresses transparently, and
    Starlette omits ``Content-Length`` when it streams a compressed response —
    so ``len(response.content)`` is the *decoded* size for exactly the large
    responses whose compression matters most. The ASGI ``http.response.body``
    events are the only place the real figure appears.
    """
    headers = [] if accept_encoding is None else [(b"accept-encoding", accept_encoding.encode())]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    sent = 0
    encoding = "identity"

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        nonlocal sent, encoding
        if message["type"] == "http.response.start":
            headers = message["headers"]
            assert isinstance(headers, list)
            for key, value in headers:
                if key.lower() == b"content-encoding":
                    encoding = value.decode()
        elif message["type"] == "http.response.body":
            body = message.get("body", b"")
            assert isinstance(body, bytes)
            sent += len(body)

    await app(scope, receive, send)  # type: ignore[arg-type]
    return sent, encoding


#: An asset the size of a real bundle, and about as compressible.
#:
#: Minified JavaScript compresses roughly 3.3:1. Random bytes compress 1:1 and
#: a repeated character compresses about 1000:1, so neither would tell us
#: anything: the first would make a working middleware look broken and the
#: second would make a broken threshold look fine. Repeating a realistic
#: fragment of minified output lands in the right neighbourhood.
COMPRESSIBLE_ASSET: Final = (
    "export const t=(e,n)=>{const r={};for(const o of e)r[o.id]=n(o);return r};" * 2000
)


@pytest.fixture
def compressible_root(tmp_path: Path) -> Path:
    """A web root whose asset is large enough to be worth compressing."""
    (tmp_path / "index.html").write_text(INDEX, encoding="utf-8")
    assets = tmp_path / ASSET_DIRECTORY
    assets.mkdir()
    (assets / "index-big123.js").write_text(COMPRESSIBLE_ASSET, encoding="utf-8")
    # Deliberately under ASSET_COMPRESSION_MINIMUM_BYTES.
    (assets / "tiny-abc123.js").write_text("export const x=1;\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def compressing_app(compressible_root: Path) -> FastAPI:
    """An application with an API route, a probe, and compressible assets."""
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/api/v1/thing")
    async def thing() -> dict[str, str]:
        # Long enough that a compressing middleware would act on it, which is
        # what makes its *absence* from the compressed set an assertion rather
        # than an accident of size.
        return {"ok": "yes", "padding": "a" * 4000}

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    mount_web_client(app, compressible_root, api_prefix="/api")
    return app


class TestAssetsAreCompressed:
    """The one transfer every user of the installation makes.

    Before this, the control plane served the built client verbatim: a first
    load was 606 kB where 181 kB would do. Caddy in the TLS overlay compresses,
    but that overlay is optional and the plain Compose file is the documented
    default — so the compression belongs in the thing that always serves the
    file.
    """

    async def test_a_large_asset_is_compressed_when_the_client_asks(
        self, compressing_app: FastAPI
    ) -> None:
        sent, encoding = await wire_bytes(
            compressing_app, "/assets/index-big123.js", accept_encoding="gzip, deflate, br"
        )

        assert encoding == "gzip"
        # The real bundle measured 3.31:1. Asserting a floor of 2:1 keeps this
        # a test of "compression happened" rather than of zlib's tuning, which
        # is not this project's to guarantee.
        assert sent * 2 < len(COMPRESSIBLE_ASSET)

    async def test_the_bytes_survive_the_round_trip(self, compressible_root: Path) -> None:
        # Compression that corrupts is worse than no compression, and a
        # truncated JavaScript bundle fails in the browser rather than here.
        app = FastAPI()
        register_exception_handlers(app)
        mount_web_client(app, compressible_root, api_prefix="/api")

        async with asgi_client(app) as http:
            response = await http.get(
                "/assets/index-big123.js", headers={"accept-encoding": "gzip"}
            )

        assert response.headers["content-encoding"] == "gzip"
        # httpx decompresses, so this compares the delivered bytes with the
        # file on disk.
        assert response.text == COMPRESSIBLE_ASSET

    async def test_a_client_that_does_not_ask_gets_the_bytes_verbatim(
        self, compressing_app: FastAPI
    ) -> None:
        sent, encoding = await wire_bytes(
            compressing_app, "/assets/index-big123.js", accept_encoding=None
        )

        assert encoding == "identity"
        assert sent == len(COMPRESSIBLE_ASSET)

    async def test_a_small_asset_is_left_alone(self, compressing_app: FastAPI) -> None:
        # Below the threshold a gzip member's framing costs more than it saves,
        # and the response fits in one segment either way.
        _, encoding = await wire_bytes(
            compressing_app, "/assets/tiny-abc123.js", accept_encoding="gzip"
        )

        assert encoding == "identity"

    async def test_the_immutable_cache_header_survives_compression(
        self, compressible_root: Path
    ) -> None:
        # The middleware rebuilds the response headers, so the one-year cache
        # policy is set *inside* what it wraps and has to come back out. Losing
        # it would make every navigation re-download the bundle, which is the
        # thing the compression is here to make cheap.
        app = FastAPI()
        register_exception_handlers(app)
        mount_web_client(app, compressible_root, api_prefix="/api")

        async with asgi_client(app) as http:
            response = await http.get(
                "/assets/index-big123.js", headers={"accept-encoding": "gzip"}
            )

        assert response.headers["cache-control"] == (
            f"public, max-age={ASSET_CACHE_SECONDS}, immutable"
        )


class TestCompressionStopsAtTheAssetMount:
    """The BREACH boundary, asserted rather than commented.

    Compressing a response that contains both a secret and attacker-influenced
    text leaks the secret through the response length. The API's responses have
    exactly that shape — session-scoped, carrying a CSRF token, and echoing
    request values back in error messages — so the compression is attached to
    the asset mount and must stay there. These tests fail if somebody moves it
    onto the application, which is the obvious "improvement" to make.
    """

    async def test_an_api_response_is_not_compressed(self, compressing_app: FastAPI) -> None:
        _, encoding = await wire_bytes(
            compressing_app, "/api/v1/thing", accept_encoding="gzip, deflate, br"
        )

        assert encoding == "identity"

    async def test_the_document_is_not_compressed(self, compressing_app: FastAPI) -> None:
        # Also not worth it: index.html is about a kilobyte, and it is the one
        # file served `no-store`, so it is fetched on every navigation.
        _, encoding = await wire_bytes(compressing_app, "/", accept_encoding="gzip")

        assert encoding == "identity"

    async def test_a_client_route_is_not_compressed(self, compressing_app: FastAPI) -> None:
        _, encoding = await wire_bytes(compressing_app, "/hosts", accept_encoding="gzip")

        assert encoding == "identity"

    async def test_a_probe_is_not_compressed(self, compressing_app: FastAPI) -> None:
        _, encoding = await wire_bytes(compressing_app, "/healthz", accept_encoding="gzip")

        assert encoding == "identity"


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
