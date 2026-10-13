"""Serving the web client from the control plane.

## Why the control plane serves it at all

The whole security model assumes **one origin**. Session cookies are
``SameSite=Lax`` and ``HttpOnly``; state-changing requests need a session-bound
CSRF token *and* a matching ``Origin``; CORS has no entries in production
because a same-origin client needs no exception (:mod:`hcc.config`). Serving
the client from a second origin would mean either a CORS allow-list with
credentials — the arrangement ``_validate_cors_origins`` refuses a wildcard for
— or a second TLS certificate and a second name for the same deployment.

So the deployment is: one container, one port, one origin. The API lives under
``/api``, the operational probes at ``/healthz`` and ``/readyz``, and
everything else is the client.

This module is the piece that was missing. Before it, there was no
implementation of the single-origin deployment the rest of the application was
written against — development ran two servers with a CORS exception, and
``deploy/compose.yaml`` started PostgreSQL and nothing else.

## What it will not do

**It will not shadow an API path.** A catch-all that returned ``index.html``
for anything unmatched would turn ``GET /api/v1/typo`` into an HTML page with
status 200, so a client bug would look like a rendering bug and a monitoring
check on the API would go green against the login screen. Paths under the API
prefix and the operational probes are answered with the application's own JSON
404 instead.

**It will not serve anything the build did not produce.** Starlette's
:class:`~starlette.staticfiles.StaticFiles` resolves and bounds every path
inside the root, so ``..`` and absolute paths are refused before any read. The
fallback serves exactly one file — ``index.html`` — and never a path taken from
the request, which is what makes a traversal attempt against it structurally
impossible rather than checked.

**It will not start if the build is missing.** A container whose client failed
to build would otherwise serve a 404 for every page while reporting itself
ready, which is the worst shape of deployment failure: healthy, and useless.

## Caching

Two rules, and they are opposites for a reason.

* ``/assets/*`` is immutable for a year. Vite puts a content hash in every
  filename, so a changed file has a changed name and a cached one can never be
  stale.
* ``index.html`` is ``no-store``. It is the file that names the hashed bundles,
  so a cached copy pins a browser to the previous deployment's JavaScript —
  which, after an upgrade that changed the API, is a client talking to a server
  it does not match.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from starlette.responses import FileResponse, Response
from starlette.staticfiles import StaticFiles

from hcc.errors import NotFoundError
from hcc.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

logger = get_logger(__name__)

#: One year, which is what "immutable" means in practice.
ASSET_CACHE_SECONDS: Final = 31_536_000

#: The directory Vite writes hashed bundles into. Mounted separately from the
#: fallback so it can carry the opposite cache policy.
ASSET_DIRECTORY: Final = "assets"

#: Paths that must never be answered with the client.
#:
#: Everything the application serves itself. A request to one of these that
#: matched no route is a 404 from the API, not a page: a monitoring check on
#: ``/readyz`` that got 200 and an HTML body would report a broken deployment
#: as healthy.
RESERVED_PREFIXES: Final = ("/healthz", "/readyz", "/docs", "/redoc", "/openapi.json")


class WebClientMissingError(RuntimeError):
    """The configured web root does not contain a built client.

    Raised at startup, deliberately. The alternative — mounting nothing and
    carrying on — produces a container that answers ``/readyz`` with "ready"
    and every page with a 404, and an operator who has to work out that the
    *build* failed rather than the deployment.
    """


def mount_web_client(app: FastAPI, root: Path, *, api_prefix: str) -> None:
    """Serve the built web client from *root*.

    Must be called **after** every API router is included: Starlette matches
    routes in registration order, and the fallback below matches every path.

    Args:
        app: the application to mount onto.
        root: the directory holding ``index.html`` and ``assets/``.
        api_prefix: the prefix the API is served under, so the fallback can
            refuse to answer for it.

    Raises:
        WebClientMissingError: *root* is not a directory, or has no
            ``index.html``.
    """
    index = root / "index.html"
    if not root.is_dir():
        raise WebClientMissingError(
            f"HCC_WEB_ROOT is {root} and that is not a directory. "
            "Build the client with `npm run build` in apps/web, or unset the "
            "variable to run the API without a client."
        )
    if not index.is_file():
        raise WebClientMissingError(
            f"HCC_WEB_ROOT is {root} and it contains no index.html. "
            "That directory is meant to be apps/web/dist after `npm run build`."
        )

    assets = root / ASSET_DIRECTORY
    if assets.is_dir():
        app.mount(
            f"/{ASSET_DIRECTORY}",
            _ImmutableStaticFiles(directory=assets),
            name="web-assets",
        )
    else:
        # Not fatal: a client built with everything inlined would have no
        # assets directory, and refusing to start over a convention would be
        # the wrong call. Logged, because in practice it means a partial copy.
        logger.warning("web_client_has_no_assets_directory", path=str(assets))

    # Registered last and matching everything, which is what makes the order
    # of this call load-bearing.
    @app.get("/{path:path}", include_in_schema=False)
    async def client(path: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        """Return ``index.html`` so the client can route the request itself.

        The SPA owns its own routing, so an unmatched path is a page the
        *browser* resolves — which is why this returns 200 and the document
        rather than a 404. ``apps/web/src/routes/not-found.tsx`` is what the
        user sees for a path the client does not know either.

        Reserved prefixes are the exception and get the application's own JSON
        404, because a caller asking for ``/api/v1/typo`` or ``/readyz`` is not
        a browser navigating.
        """
        requested = "/" + path
        if requested.startswith(api_prefix) or requested.startswith(RESERVED_PREFIXES):
            raise NotFoundError("No such endpoint.")
        # One fixed file. Nothing from the request reaches a filesystem path
        # here, which is what makes traversal impossible rather than guarded
        # against.
        return FileResponse(
            index,
            # The file that names the hashed bundles. A cached copy pins the
            # browser to the previous deployment's JavaScript.
            headers={"Cache-Control": "no-store"},
        )

    logger.info("web_client_mounted", root=str(root))


class _ImmutableStaticFiles(StaticFiles):
    """Static files with a one-year immutable cache.

    Safe only because every filename carries a content hash: a changed file
    has a changed name, so a cached response can never be stale. Applying this
    to un-hashed files would pin browsers to a year-old bundle.
    """

    def file_response(self, *args: object, **kwargs: object) -> Response:
        response = super().file_response(*args, **kwargs)  # type: ignore[arg-type]
        response.headers["Cache-Control"] = f"public, max-age={ASSET_CACHE_SECONDS}, immutable"
        return response
