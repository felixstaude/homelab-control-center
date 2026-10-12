/**
 * Typed API errors.
 *
 * The backend returns one envelope shape for every non-2xx response
 * (`docs/ARCHITECTURE.md` §3.2), so the client parses one contract and hands
 * components a discriminated error rather than a raw `Response`.
 *
 * ## Why `kind` is not just the status code
 *
 * The distinction that matters most is between two different 403s:
 *
 * - `insufficient_permissions` — the caller is authenticated and simply not
 *   allowed. Logging in again would not help, so the UI must *not* send them
 *   to the login page.
 * - `csrf_validation_failed` — the request could not be attributed to a
 *   deliberate action. The session may well be fine.
 *
 * Treating every 403 as an expired session is the classic version of this bug:
 * it logs a Viewer out whenever they touch something an Operator may do. The
 * backend deliberately separates 401 from 403 for exactly this reason
 * (`docs/SECURITY.md`), and throwing that information away in the client would
 * waste it.
 */

/** Every failure a request can produce, including transport failures. */
export type ApiErrorKind =
  /** 400 — malformed request. A bug in this client. */
  | 'bad_request'
  /** 401 — no usable session. Re-authentication is the remedy. */
  | 'unauthenticated'
  /** 403 `insufficient_permissions` — authenticated but not permitted. */
  | 'forbidden'
  /** 403 `csrf_validation_failed` — origin or CSRF token rejected. */
  | 'csrf'
  | 'not_found'
  | 'conflict'
  /** 422 — field validation failed; `details.fields` names them. */
  | 'validation'
  /** 429 — throttled. `retryAfterSeconds` is set when the server said. */
  | 'rate_limited'
  /** 500 — server fault. */
  | 'server'
  /** 502/503/504 — backend or a dependency unavailable. */
  | 'unavailable'
  /** The request never reached the server. */
  | 'network'
  /** The request exceeded its own deadline. */
  | 'timeout'
  /** The caller cancelled it. Not an error to report to the user. */
  | 'aborted'
  | 'unknown';

/** One field rejected by server-side validation. */
export type ValidationFieldError = {
  location: string;
  message: string;
  type: string;
};

/**
 * A failed API call.
 *
 * `message` is always safe to display: it comes from the server's envelope,
 * which never contains internal detail (the catch-all handler returns an
 * opaque message), or from a fixed string below.
 */
export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  /** HTTP status, or `null` when the request never completed. */
  readonly status: number | null;
  /** The backend's stable machine-readable code, when it sent one. */
  readonly code: string | null;
  /** Correlates with the server log. Worth showing for a 500. */
  readonly requestId: string | null;
  readonly details: Record<string, unknown> | null;
  /** From `Retry-After`, for a 429. */
  readonly retryAfterSeconds: number | null;

  constructor(
    kind: ApiErrorKind,
    message: string,
    options: {
      status?: number | null;
      code?: string | null;
      requestId?: string | null;
      details?: Record<string, unknown> | null;
      retryAfterSeconds?: number | null;
      cause?: unknown;
    } = {},
  ) {
    super(message, options.cause === undefined ? undefined : { cause: options.cause });
    this.name = 'ApiError';
    this.kind = kind;
    this.status = options.status ?? null;
    this.code = options.code ?? null;
    this.requestId = options.requestId ?? null;
    this.details = options.details ?? null;
    this.retryAfterSeconds = options.retryAfterSeconds ?? null;
  }

  /** Field errors from a 422, or an empty list. */
  get fieldErrors(): ValidationFieldError[] {
    const fields = this.details?.fields;
    if (!Array.isArray(fields)) return [];
    return fields.filter(
      (entry): entry is ValidationFieldError =>
        typeof entry === 'object' &&
        entry !== null &&
        typeof (entry as { message?: unknown }).message === 'string',
    );
  }
}

export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError;
}

/**
 * Whether this error means "your session is gone".
 *
 * **Only** a 401. Deliberately excludes 403 of either kind — see the module
 * docstring.
 */
export function isSessionExpired(value: unknown): boolean {
  return isApiError(value) && value.kind === 'unauthenticated';
}

/** Whether the caller is authenticated but not permitted to do this. */
export function isPermissionDenied(value: unknown): boolean {
  return isApiError(value) && value.kind === 'forbidden';
}

/** Whether the request was refused by the CSRF or Origin check. */
export function isCsrfFailure(value: unknown): boolean {
  return isApiError(value) && value.kind === 'csrf';
}

/**
 * Whether the backend could not be reached or is temporarily unwell.
 *
 * Used to show "backend unavailable" rather than "your session expired": a
 * dropped network must never be reported as a logout, because clearing the
 * authenticated UI on a transient blip is both wrong and alarming.
 */
export function isBackendUnreachable(value: unknown): boolean {
  return (
    isApiError(value) &&
    (value.kind === 'network' || value.kind === 'timeout' || value.kind === 'unavailable')
  );
}

/**
 * Whether retrying the identical request could plausibly succeed.
 *
 * Note what is absent: `rate_limited` is **not** retryable here. A client that
 * automatically retries a throttled login is an automatic password-retry loop,
 * which the milestone brief forbids and which would burn the user's remaining
 * budget on their behalf.
 */
export function isRetryable(value: unknown): boolean {
  return (
    isApiError(value) &&
    (value.kind === 'network' || value.kind === 'timeout' || value.kind === 'unavailable')
  );
}

/** A human-readable summary, for an error state or an inline message. */
export function describeApiError(value: unknown): string {
  if (isApiError(value)) return value.message;
  if (value instanceof Error) return value.message;
  return 'Something went wrong.';
}
