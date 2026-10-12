/**
 * The HTTP client.
 *
 * One function, `apiRequest`, through which every call to the control plane
 * passes. Centralised so that credentials, CSRF, timeouts and error parsing
 * are decided once rather than per call site — the same reasoning the backend
 * applies to its session dependency.
 *
 * ## Credentials
 *
 * `credentials: 'same-origin'`, not `'include'`. The production deployment
 * serves the built client from the control plane itself, and the Vite dev
 * server proxies `/api` so development is same-origin too
 * (`docs/ARCHITECTURE.md` §12.2). `'include'` would additionally send cookies
 * cross-origin, which nothing here needs and which would widen what a
 * misconfiguration could do.
 *
 * ## CSRF
 *
 * The backend requires the session's CSRF token in `X-CSRF-Token` on every
 * state-changing authenticated request, compared against the session row
 * (ADR-030). The token arrives in a readable `hcc_csrf` cookie — readable on
 * purpose, since double-submit is impossible otherwise — so it is read from
 * `document.cookie` per request rather than cached.
 *
 * Reading it per request is the point: after login, logout or a session
 * change the cookie changes, and a cached value would be sent stale. The
 * cheapest way to never send a stale token is never to hold one.
 *
 * ## No token storage
 *
 * Nothing is written to `localStorage` or `sessionStorage`. The session lives
 * in an `HttpOnly` cookie the client cannot read, which is what keeps an XSS
 * bug from becoming a stolen session. Mirroring any of it into web storage
 * would undo that.
 */

import { ApiError, type ApiErrorKind } from '@/api/errors';

/**
 * Base path for the versioned API.
 *
 * Configurable so a deployment behind a path prefix can be supported without
 * a rebuild of every call site, but it is a *path*, never an origin: an
 * absolute URL here would move the API off-origin and break both the
 * same-origin cookie policy and the backend's Origin check.
 */
export const API_BASE_PATH: string = normalizeBasePath(
  import.meta.env.VITE_API_BASE_PATH ?? '/api/v1',
);

/** Default per-request deadline. */
export const DEFAULT_TIMEOUT_MS = 15_000;

/** Methods the backend treats as state-changing, and so CSRF-protects. */
const UNSAFE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

/** The cookie the backend puts the CSRF token in (`HCC_CSRF_COOKIE_NAME`). */
export const CSRF_COOKIE_NAME = 'hcc_csrf';
export const CSRF_HEADER_NAME = 'X-CSRF-Token';

function normalizeBasePath(value: string): string {
  const trimmed = value.trim();
  if (trimmed.includes('://') || trimmed.startsWith('//')) {
    // Fail at startup rather than producing cross-origin requests that break
    // cookies and Origin validation in a way that is hard to trace back here.
    throw new Error(
      `VITE_API_BASE_PATH must be a path, not a URL (got ${trimmed}). ` +
        'The client is served same-origin with the API.',
    );
  }
  const withLeading = trimmed.startsWith('/') ? trimmed : `/${trimmed}`;
  return withLeading.endsWith('/') ? withLeading.slice(0, -1) : withLeading;
}

/**
 * Read the CSRF token from the document's cookies.
 *
 * Returns `null` when absent, which is the normal unauthenticated state. The
 * request still goes out: letting the server answer 401 or 403 keeps one
 * authority on what is permitted, and a client-side guess could only ever
 * disagree with it.
 */
export function readCsrfToken(): string | null {
  // `document` is absent in a non-DOM environment; the client is only ever
  // used from the browser and from jsdom, but the guard keeps it honest.
  if (typeof document === 'undefined') return null;

  for (const part of document.cookie.split(';')) {
    const separator = part.indexOf('=');
    if (separator === -1) continue;
    if (part.slice(0, separator).trim() !== CSRF_COOKIE_NAME) continue;
    const raw = part.slice(separator + 1).trim();
    try {
      return decodeURIComponent(raw);
    } catch {
      // A cookie we cannot decode is not a token we should send.
      return null;
    }
  }
  return null;
}

export type ApiRequestOptions = {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  /** Serialised as JSON. Omit for a request with no body. */
  body?: unknown;
  /**
   * Caller cancellation — a React effect teardown, or a superseded query.
   *
   * `| undefined` is explicit because the project enables
   * `exactOptionalPropertyTypes`: a caller forwarding an optional signal
   * passes `undefined` rather than omitting the key.
   */
  signal?: AbortSignal | undefined;
  /** Overrides {@link DEFAULT_TIMEOUT_MS}. */
  timeoutMs?: number | undefined;
};

/**
 * The envelope, as far as the client dares assume.
 *
 * Parsed from `unknown` and narrowed field by field rather than cast: a cast
 * would assert a shape the network cannot be trusted to have, and
 * `response.json()` can legitimately produce `null` or an array.
 */
type ErrorEnvelopeFields = {
  code: string | null;
  message: string | null;
  details: Record<string, unknown> | null;
  requestId: string | null;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function readEnvelope(payload: unknown): ErrorEnvelopeFields {
  const fields: ErrorEnvelopeFields = {
    code: null,
    message: null,
    details: null,
    requestId: null,
  };
  if (!isRecord(payload)) return fields;

  const envelope = payload.error;
  if (!isRecord(envelope)) return fields;

  if (typeof envelope.code === 'string') fields.code = envelope.code;
  if (typeof envelope.message === 'string') fields.message = envelope.message;
  if (typeof envelope.request_id === 'string') fields.requestId = envelope.request_id;
  if (isRecord(envelope.details)) fields.details = envelope.details;
  return fields;
}

/**
 * Map a response onto an error kind.
 *
 * The 403 split is the important one: the backend sends
 * `csrf_validation_failed` and `insufficient_permissions` with the same
 * status, and they call for completely different UI.
 */
function kindFor(status: number, code: string | null): ApiErrorKind {
  switch (status) {
    case 400:
      return 'bad_request';
    case 401:
      return 'unauthenticated';
    case 403:
      return code === 'csrf_validation_failed' ? 'csrf' : 'forbidden';
    case 404:
      return 'not_found';
    case 409:
      return 'conflict';
    case 422:
      return 'validation';
    case 429:
      return 'rate_limited';
    case 500:
      return 'server';
    case 502:
    case 503:
    case 504:
      return 'unavailable';
    default:
      return status >= 500 ? 'server' : 'unknown';
  }
}

/** Fixed fallbacks, used only when the server sent no usable message. */
const FALLBACK_MESSAGES: Record<ApiErrorKind, string> = {
  bad_request: 'The request was rejected as malformed.',
  unauthenticated: 'Your session has ended. Please sign in again.',
  forbidden: 'You do not have permission to do that.',
  csrf: 'The request could not be verified. Please reload the page and retry.',
  not_found: 'That resource does not exist.',
  conflict: 'That conflicts with the current state.',
  validation: 'Some of the submitted values are invalid.',
  rate_limited: 'Too many attempts. Please wait before trying again.',
  server: 'The server encountered an internal error.',
  unavailable: 'The control plane is temporarily unavailable.',
  network: 'Cannot reach the control plane. Check that it is running.',
  timeout: 'The control plane did not respond in time.',
  aborted: 'The request was cancelled.',
  unknown: 'The request failed.',
};

function parseRetryAfter(response: Response): number | null {
  const header = response.headers.get('Retry-After');
  if (!header) return null;
  const seconds = Number.parseInt(header, 10);
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : null;
}

async function errorFromResponse(response: Response): Promise<ApiError> {
  let fields: ErrorEnvelopeFields = {
    code: null,
    message: null,
    details: null,
    requestId: null,
  };

  // A non-JSON error body is possible — a proxy's own 502 page, for example —
  // so parsing must not be what turns a 502 into an unhandled exception.
  try {
    fields = readEnvelope(await response.json());
  } catch {
    // Leave the fields null and fall back to a fixed message below.
  }

  const kind = kindFor(response.status, fields.code);
  return new ApiError(kind, fields.message ?? FALLBACK_MESSAGES[kind], {
    status: response.status,
    code: fields.code,
    details: fields.details,
    requestId: fields.requestId,
    retryAfterSeconds: parseRetryAfter(response),
  });
}


/**
 * Perform one API request.
 *
 * @throws ApiError for every failure, including transport failures, so a
 * caller has exactly one error type to handle.
 */
export async function apiRequest<T>(path: string, options: ApiRequestOptions = {}): Promise<T> {
  const { method = 'GET', body, signal, timeoutMs = DEFAULT_TIMEOUT_MS } = options;

  const url = path.startsWith('/') ? `${API_BASE_PATH}${path}` : `${API_BASE_PATH}/${path}`;

  const headers = new Headers({ Accept: 'application/json' });
  if (body !== undefined) headers.set('Content-Type', 'application/json');

  if (UNSAFE_METHODS.has(method)) {
    const token = readCsrfToken();
    if (token !== null) headers.set(CSRF_HEADER_NAME, token);
  }

  // A controller of our own, so the timeout and the caller's cancellation
  // collapse into one signal without depending on `AbortSignal.any`, which
  // jsdom does not implement everywhere.
  const controller = new AbortController();
  // A holder rather than a plain `let`: the flag is set inside the timer
  // callback, which TypeScript's control-flow analysis does not model, so a
  // local would be narrowed to `false` at the catch below and the timeout
  // branch would read as dead code.
  const deadline = { exceeded: false };

  const timer = setTimeout(() => {
    deadline.exceeded = true;
    controller.abort();
  }, timeoutMs);

  const abortFromCaller = () => {
    controller.abort();
  };
  signal?.addEventListener('abort', abortFromCaller, { once: true });

  try {
    if (signal?.aborted) controller.abort();

    const response = await fetch(url, {
      method,
      headers,
      // Only ever cookies, and only same-origin ones. See the module doc.
      credentials: 'same-origin',
      // Never cache an authenticated response: a browser cache hit after
      // logout would re-display another session's data.
      cache: 'no-store',
      redirect: 'error',
      signal: controller.signal,
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });

    if (!response.ok) throw await errorFromResponse(response);

    if (response.status === 204 || response.headers.get('Content-Length') === '0') {
      return undefined as T;
    }

    try {
      return (await response.json()) as T;
    } catch (cause) {
      throw new ApiError('server', 'The control plane returned a malformed response.', {
        status: response.status,
        cause,
      });
    }
  } catch (error) {
    if (error instanceof ApiError) throw error;

    // `fetch` rejects with an AbortError for both our timeout and the
    // caller's cancellation; only the flag distinguishes them, and the
    // difference matters because one is a fault and the other is routine.
    if (error instanceof DOMException && error.name === 'AbortError') {
      if (deadline.exceeded) {
        throw new ApiError('timeout', FALLBACK_MESSAGES.timeout, { cause: error });
      }
      throw new ApiError('aborted', FALLBACK_MESSAGES.aborted, { cause: error });
    }

    // Anything else from `fetch` means the request never completed: DNS,
    // connection refused, TLS. The message is fixed rather than taken from
    // the exception, whose text varies by browser and can include the URL.
    throw new ApiError('network', FALLBACK_MESSAGES.network, { cause: error });
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', abortFromCaller);
  }
}
