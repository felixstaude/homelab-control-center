"""HTTP middleware.

Two middlewares:

* :class:`RequestContextMiddleware` — request correlation and access logging.
  Assigns every request an id, binds it to the structlog context so that *all*
  log lines emitted while handling that request carry it, and logs one
  structured line per completed request.
* :class:`SecurityHeadersMiddleware` — response hardening headers, added in
  milestone 1.3 alongside session cookies.
"""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING, Final

import structlog
from starlette.middleware.base import BaseHTTPMiddleware

from hcc.logging import get_logger

if TYPE_CHECKING:
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp

logger = get_logger(__name__)

# Clients may supply their own correlation id; we propagate it when present so
# a trace spans the proxy and the API.
REQUEST_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request id, bind logging context, and log the access line."""

    def __init__(self, app: ASGIApp, *, exclude_paths: frozenset[str] = frozenset()) -> None:
        super().__init__(app)
        # Health probes are hit continuously by orchestrators; logging each one
        # drowns the signal.
        self._exclude_paths = exclude_paths

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER)
        request_id = incoming if incoming and len(incoming) <= 128 else uuid.uuid4().hex
        request.state.request_id = request_id

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        should_log = request.url.path not in self._exclude_paths
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            # The exception handlers produce the response and log the
            # traceback; here we only record that the request terminated, then
            # re-raise so nothing is swallowed.
            duration_ms = (time.perf_counter() - started) * 1000
            logger.warning(
                "request_errored",
                method=request.method,
                path=request.url.path,
                duration_ms=round(duration_ms, 2),
            )
            raise
        finally:
            structlog.contextvars.unbind_contextvars("request_id")

        duration_ms = (time.perf_counter() - started) * 1000
        response.headers[REQUEST_ID_HEADER] = request_id

        if should_log:
            logger.info(
                "request_completed",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                duration_ms=round(duration_ms, 2),
            )

        return response


# Headers sent on every response. Values are static, so they are built once.
#
# Omitted deliberately:
#   * HSTS — belongs to the TLS-terminating reverse proxy, which knows whether
#     HTTPS is actually in use. Sending it from behind a plain-HTTP proxy would
#     either do nothing or lock a development host out of HTTP entirely.
#   * X-XSS-Protection — deprecated, and the legacy behaviour it enabled
#     introduced vulnerabilities of its own.
_STATIC_SECURITY_HEADERS: Final = {
    # Stops a browser from second-guessing a declared Content-Type, which is
    # how a JSON response gets interpreted as HTML and executed.
    "X-Content-Type-Options": "nosniff",
    # Legacy framing control, for browsers predating frame-ancestors.
    "X-Frame-Options": "DENY",
    # Do not leak the full URL (which may carry ids) to third parties, while
    # keeping same-origin referrers useful.
    "Referrer-Policy": "strict-origin-when-cross-origin",
    # This is a dashboard, not a site that needs the camera.
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    # Belt and braces for API responses: some clients honour this.
    "Cross-Origin-Opener-Policy": "same-origin",
}

# A deliberately tight policy for an SPA served same-origin by the control
# plane. `frame-ancestors 'none'` is the modern replacement for
# X-Frame-Options; `form-action 'self'` blocks a injected form posting
# credentials elsewhere.
_CONTENT_SECURITY_POLICY: Final = (
    "default-src 'self'; "
    "script-src 'self'; "
    # The design system sets CSS custom properties through inline styles, so
    # style-src cannot be 'self' alone. Inline *styles* are a far smaller risk
    # than inline scripts, which stay forbidden.
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    # No external endpoints: the client only ever talks to its own origin
    # (ADR-009).
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add hardening headers to every response.

    The Content-Security-Policy is applied only when enabled, which by default
    means production only. The Vite dev server needs inline scripts and a
    websocket for hot reload, so a policy strict enough to be worth having
    would break development — and a policy that gets weakened for development
    is one nobody trusts in production. See
    :attr:`hcc.config.Settings.effective_csp_enabled`.
    """

    def __init__(self, app: ASGIApp, *, enable_csp: bool) -> None:
        super().__init__(app)
        self._enable_csp = enable_csp

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)

        for header, value in _STATIC_SECURITY_HEADERS.items():
            # setdefault, not assignment: a handler that deliberately set its
            # own value for a specific response should win.
            response.headers.setdefault(header, value)

        if self._enable_csp:
            response.headers.setdefault("Content-Security-Policy", _CONTENT_SECURITY_POLICY)

        return response
