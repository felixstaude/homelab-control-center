"""HTTP middleware.

Phase 0 ships one middleware: request correlation and access logging. It
assigns every request an id, binds it to the structlog context so that *all*
log lines emitted while handling that request carry it, and logs one structured
line per completed request.
"""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING

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
