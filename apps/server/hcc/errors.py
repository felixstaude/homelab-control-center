"""Structured error types and centralised exception handling.

Every error the API returns has the same JSON shape, so clients parse one
contract instead of guessing per endpoint:

    {
      "error": {
        "code": "not_found",
        "message": "Host not found.",
        "details": {...},          # optional, machine-readable
        "request_id": "01J..."     # correlates with server logs
      }
    }

Engineering rules 5 and 6 are enforced here: errors carry explicit types, and
the catch-all handler logs with a full traceback before responding. Nothing is
swallowed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from hcc.logging import get_logger
from hcc.middleware import REQUEST_ID_HEADER, apply_security_headers

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = get_logger(__name__)


class ErrorDetail(BaseModel):
    """The ``error`` member of an error response."""

    code: str = Field(description="Stable machine-readable error code.")
    message: str = Field(description="Human-readable description. Safe to display.")
    details: dict[str, Any] | None = Field(
        default=None, description="Optional structured context, e.g. field validation errors."
    )
    request_id: str | None = Field(
        default=None, description="Correlates this response with server-side logs."
    )


class ErrorResponse(BaseModel):
    """The complete body of any non-2xx API response."""

    error: ErrorDetail


class AppError(Exception):
    """Base class for all errors this application raises deliberately.

    Carrying the HTTP status and a stable ``code`` on the exception keeps the
    mapping from domain failure to wire format in one place.
    """

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "internal_error"
    message: str = "An internal error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.status_code = status_code or self.status_code
        self.details: dict[str, Any] | None = dict(details) if details else None
        super().__init__(self.message)


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    message = "The requested resource does not exist."


class ValidationFailedError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "validation_failed"
    message = "The request payload is invalid."


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"
    message = "The request conflicts with the current state of the resource."


class ServiceUnavailableError(AppError):
    """A dependency the request needed is not currently usable."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "service_unavailable"
    message = "A required dependency is unavailable."


class InvalidCredentialsError(AppError):
    """Login failed.

    One error for every cause — wrong password, unknown username, inactive
    account — so that the endpoint cannot be used to enumerate usernames. The
    real reason is logged server-side; see
    :mod:`hcc.security.authentication`.
    """

    status_code = status.HTTP_401_UNAUTHORIZED
    code = "invalid_credentials"
    message = "Invalid username or password."


class NotAuthenticatedError(AppError):
    """No valid session was presented.

    Deliberately indistinguishable between "no cookie", "expired", "revoked"
    and "unknown token": a client that learns *which* would learn whether a
    captured token is live.
    """

    status_code = status.HTTP_401_UNAUTHORIZED
    code = "not_authenticated"
    message = "Authentication required."


class InsufficientPermissionsError(AppError):
    """The caller is authenticated but not permitted.

    403, not 401: re-authenticating would not help, so a client must not be
    prompted to log in again.

    One error for both reasons a request can be refused — the role is too low,
    or the capability is not enabled on the target host. The client learns only
    that it is not permitted; enumerating what a higher role or a different
    host would allow is information an attacker can use. The distinction is in
    the server log, where it is actionable.
    """

    status_code = status.HTTP_403_FORBIDDEN
    code = "insufficient_permissions"
    message = "You do not have permission to perform this action."


class CsrfValidationError(AppError):
    """A state-changing request failed its CSRF check.

    403 rather than 401: the caller is authenticated, but this particular
    request cannot be attributed to a deliberate action by them.
    """

    status_code = status.HTTP_403_FORBIDDEN
    code = "csrf_validation_failed"
    message = "CSRF validation failed."


class RateLimitedError(AppError):
    """Too many requests.

    ``retry_after_seconds`` is rendered into a ``Retry-After`` header by the
    handler, so a well-behaved client can back off rather than hammering.
    """

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"
    message = "Too many attempts. Please try again later."

    def __init__(self, retry_after_seconds: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.retry_after_seconds = max(1, retry_after_seconds)


def _request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return str(value) if value else None


def _harden_bypassed_response(request: Request, response: JSONResponse) -> None:
    """Apply middleware-provided headers to a response that bypassed it.

    Only the catch-all ``Exception`` handler needs this — see its comment.
    Reads configuration from ``app.state`` rather than closing over settings,
    and tolerates their absence: this runs while handling an unexpected error,
    so it must not be able to raise a second one and mask the first.
    """
    settings = getattr(request.app.state, "settings", None)
    if settings is not None and settings.security_headers_enabled:
        apply_security_headers(response, enable_csp=settings.effective_csp_enabled)

    request_id = _request_id(request)
    if request_id is not None:
        response.headers.setdefault(REQUEST_ID_HEADER, request_id)


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    request: Request,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorDetail(
            code=code,
            message=message,
            details=details,
            request_id=_request_id(request),
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json", exclude_none=True),
    )


# Maps HTTP status codes raised via HTTPException onto our stable code strings.
_STATUS_CODES: dict[int, str] = {
    status.HTTP_400_BAD_REQUEST: "bad_request",
    status.HTTP_401_UNAUTHORIZED: "unauthenticated",
    status.HTTP_403_FORBIDDEN: "forbidden",
    status.HTTP_404_NOT_FOUND: "not_found",
    status.HTTP_405_METHOD_NOT_ALLOWED: "method_not_allowed",
    status.HTTP_409_CONFLICT: "conflict",
    status.HTTP_422_UNPROCESSABLE_CONTENT: "validation_failed",
    status.HTTP_429_TOO_MANY_REQUESTS: "rate_limited",
    status.HTTP_503_SERVICE_UNAVAILABLE: "service_unavailable",
}


def register_exception_handlers(app: FastAPI) -> None:
    """Install the application's exception handlers.

    Ordering note: the ``Exception`` handler is the last resort. Anything it
    catches is a bug, so it logs at ``exception`` level with the traceback and
    returns an opaque 500 — internal details never reach the client.
    """

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        # Deliberate, expected failures: log at a level matching severity
        # rather than as errors, to keep real problems visible.
        log = logger.bind(
            error_code=exc.code,
            status_code=exc.status_code,
            path=request.url.path,
            request_id=_request_id(request),
        )
        if exc.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
            log.error("request_failed", message=exc.message)
        else:
            log.info("request_rejected", message=exc.message)

        response = _error_response(
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            request=request,
            details=exc.details,
        )
        if isinstance(exc, RateLimitedError):
            # Tells a well-behaved client exactly how long to wait, which is
            # cheaper for both sides than it guessing.
            response.headers["Retry-After"] = str(exc.retry_after_seconds)
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            # A cookie-based API must not send a bare `WWW-Authenticate:
            # Basic`, or browsers pop a native credential dialog over the SPA.
            response.headers["WWW-Authenticate"] = 'Cookie realm="hcc"'
        return response

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Surface which fields failed; this is actionable for API clients.
        fields = [
            {
                "location": ".".join(str(part) for part in error["loc"]),
                "message": error["msg"],
                "type": error["type"],
            }
            for error in exc.errors()
        ]
        logger.info(
            "request_validation_failed",
            path=request.url.path,
            field_count=len(fields),
            request_id=_request_id(request),
        )
        return _error_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="validation_failed",
            message="The request payload is invalid.",
            request=request,
            details={"fields": fields},
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException | HTTPException
    ) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, "http_error")
        message = exc.detail if isinstance(exc.detail, str) else code.replace("_", " ").capitalize()
        return _error_response(
            status_code=exc.status_code,
            code=code,
            message=message,
            request=request,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # Never swallowed: full traceback goes to the log with the request id
        # so the opaque client-facing response can still be traced.
        logger.exception(
            "unhandled_exception",
            path=request.url.path,
            method=request.method,
            exception_type=type(exc).__name__,
            request_id=_request_id(request),
        )
        response = _error_response(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="internal_error",
            message="An internal error occurred.",
            request=request,
        )
        # This handler is bound to `ServerErrorMiddleware`, the outermost
        # layer, so its response never travels back through the middleware
        # stack. The headers every other response gets from middleware must
        # therefore be applied here, or a 500 becomes the one response class
        # without them.
        _harden_bypassed_response(request, response)
        return response
