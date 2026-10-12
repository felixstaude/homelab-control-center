/**
 * API client behaviour.
 *
 * `fetch` is stubbed at the network boundary rather than mocking the client
 * itself, so these exercise the real header assembly, credential policy,
 * envelope parsing and abort handling.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  API_BASE_PATH,
  CSRF_COOKIE_NAME,
  CSRF_HEADER_NAME,
  apiRequest,
  readCsrfToken,
} from '@/api/client';
import {
  ApiError,
  isBackendUnreachable,
  isCsrfFailure,
  isPermissionDenied,
  isRetryable,
  isSessionExpired,
} from '@/api/errors';

/** Build a JSON `Response`, as `fetch` would resolve it. */
function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
}

/** The backend's uniform error envelope. */
function errorResponse(
  status: number,
  code: string,
  message: string,
  extra: { details?: unknown; requestId?: string; headers?: Record<string, string> } = {},
): Response {
  return new Response(
    JSON.stringify({
      error: {
        code,
        message,
        ...(extra.details === undefined ? {} : { details: extra.details }),
        ...(extra.requestId === undefined ? {} : { request_id: extra.requestId }),
      },
    }),
    {
      status,
      headers: { 'Content-Type': 'application/json', ...extra.headers },
    },
  );
}

const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal('fetch', fetchMock);
  // Clear cookies between tests so a CSRF token cannot leak across them.
  for (const part of document.cookie.split(';')) {
    const name = part.split('=')[0]?.trim();
    if (name) document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
  }
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function lastRequest(): { url: string; init: RequestInit } {
  const call = fetchMock.mock.calls.at(-1);
  expect(call, 'fetch was never called').toBeDefined();
  // `fetch` accepts a string, a URL or a Request; the client always passes a
  // string, and narrowing says so rather than stringifying an object.
  const input = call?.[0];
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : '';
  return { url, init: call?.[1] ?? {} };
}

function headerOf(name: string): string | null {
  return new Headers(lastRequest().init.headers).get(name);
}

describe('successful requests', () => {
  it('returns the parsed JSON body', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ initialized: true }));

    await expect(apiRequest<{ initialized: boolean }>('/setup/status')).resolves.toEqual({
      initialized: true,
    });
  });

  it('prefixes the path with the versioned base path', async () => {
    fetchMock.mockResolvedValue(jsonResponse({}));

    await apiRequest('/auth/me');

    expect(lastRequest().url).toBe(`${API_BASE_PATH}/auth/me`);
  });

  it('accepts a path without a leading slash', async () => {
    fetchMock.mockResolvedValue(jsonResponse({}));

    await apiRequest('auth/me');

    expect(lastRequest().url).toBe(`${API_BASE_PATH}/auth/me`);
  });

  it('sends a JSON body and content type for a mutation', async () => {
    fetchMock.mockResolvedValue(jsonResponse({}));

    await apiRequest('/auth/login', {
      method: 'POST',
      body: { username: 'felix', password: 'secret' },
    });

    expect(headerOf('Content-Type')).toBe('application/json');
    expect(lastRequest().init.body).toBe('{"username":"felix","password":"secret"}');
  });

  it('omits the content type when there is no body', async () => {
    fetchMock.mockResolvedValue(jsonResponse({}));

    await apiRequest('/auth/logout', { method: 'POST' });

    expect(headerOf('Content-Type')).toBeNull();
    expect(lastRequest().init.body).toBeUndefined();
  });

  it('returns undefined for 204 No Content', async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));

    await expect(apiRequest('/whatever', { method: 'DELETE' })).resolves.toBeUndefined();
  });
});

describe('credentials and caching', () => {
  it('sends same-origin credentials, not cross-origin ones', async () => {
    // 'include' would send cookies off-origin, which no supported deployment
    // needs; the client is served same-origin with the API.
    fetchMock.mockResolvedValue(jsonResponse({}));

    await apiRequest('/auth/me');

    expect(lastRequest().init.credentials).toBe('same-origin');
  });

  it('never caches a response', async () => {
    // A cache hit after logout would re-display a previous session's data.
    fetchMock.mockResolvedValue(jsonResponse({}));

    await apiRequest('/auth/me');

    expect(lastRequest().init.cache).toBe('no-store');
  });

  it('refuses to follow redirects', async () => {
    // A redirect from an API call is never expected, and following one could
    // replay credentials to another location.
    fetchMock.mockResolvedValue(jsonResponse({}));

    await apiRequest('/auth/me');

    expect(lastRequest().init.redirect).toBe('error');
  });

  it('puts no credentials in the URL', async () => {
    fetchMock.mockResolvedValue(jsonResponse({}));

    await apiRequest('/auth/login', {
      method: 'POST',
      body: { username: 'felix', password: 'correct horse battery staple' },
    });

    expect(lastRequest().url).not.toContain('correct');
    expect(lastRequest().url).not.toContain('felix');
  });

  it('writes nothing to web storage', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ csrf_token: 'token-value', username: 'felix' }),
    );

    await apiRequest('/auth/login', { method: 'POST', body: { username: 'f', password: 'p' } });

    // The session is an HttpOnly cookie; mirroring any of it into storage
    // would undo that protection.
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });
});

describe('CSRF handling', () => {
  it('sends the token from the cookie on a state-changing request', async () => {
    document.cookie = `${CSRF_COOKIE_NAME}=csrf-abc123; path=/`;
    fetchMock.mockResolvedValue(jsonResponse({}));

    await apiRequest('/auth/logout', { method: 'POST' });

    expect(headerOf(CSRF_HEADER_NAME)).toBe('csrf-abc123');
  });

  it.each(['POST', 'PUT', 'PATCH', 'DELETE'] as const)(
    'sends the token on %s',
    async (method) => {
      document.cookie = `${CSRF_COOKIE_NAME}=csrf-abc123; path=/`;
      fetchMock.mockResolvedValue(jsonResponse({}));

      await apiRequest('/thing', { method });

      expect(headerOf(CSRF_HEADER_NAME)).toBe('csrf-abc123');
    },
  );

  it('does not send the token on a safe request', async () => {
    // GET is not CSRF-protected server-side, and sending the header would
    // imply otherwise.
    document.cookie = `${CSRF_COOKIE_NAME}=csrf-abc123; path=/`;
    fetchMock.mockResolvedValue(jsonResponse({}));

    await apiRequest('/auth/me');

    expect(headerOf(CSRF_HEADER_NAME)).toBeNull();
  });

  it('omits the header when there is no cookie', async () => {
    // The ordinary unauthenticated case. The request still goes out so the
    // server decides, rather than the client guessing.
    fetchMock.mockResolvedValue(jsonResponse({}));

    await apiRequest('/auth/logout', { method: 'POST' });

    expect(headerOf(CSRF_HEADER_NAME)).toBeNull();
  });

  it('reads the current cookie on every request, never a cached one', async () => {
    // After login or logout the cookie changes; a cached token would be sent
    // stale and rejected.
    document.cookie = `${CSRF_COOKIE_NAME}=first-token; path=/`;
    // A fresh Response per call: a body can only be read once, so a single
    // shared instance would fail the second request.
    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse({})));
    await apiRequest('/one', { method: 'POST' });
    expect(headerOf(CSRF_HEADER_NAME)).toBe('first-token');

    document.cookie = `${CSRF_COOKIE_NAME}=second-token; path=/`;
    await apiRequest('/two', { method: 'POST' });

    expect(headerOf(CSRF_HEADER_NAME)).toBe('second-token');
  });

  it('picks the right cookie when others are present', () => {
    document.cookie = 'unrelated=value; path=/';
    document.cookie = `${CSRF_COOKIE_NAME}=the-token; path=/`;
    document.cookie = 'another=value; path=/';

    expect(readCsrfToken()).toBe('the-token');
  });

  it('decodes a percent-encoded cookie value', () => {
    document.cookie = `${CSRF_COOKIE_NAME}=${encodeURIComponent('a+b/c=')}; path=/`;

    expect(readCsrfToken()).toBe('a+b/c=');
  });

  it('returns null rather than a corrupt token', () => {
    document.cookie = `${CSRF_COOKIE_NAME}=%E0%A4%A; path=/`;

    expect(readCsrfToken()).toBeNull();
  });
});

describe('error mapping', () => {
  it('parses the envelope into a typed error', async () => {
    fetchMock.mockResolvedValue(
      errorResponse(404, 'not_found', "Host 'nas-01' not found.", {
        details: { host: 'nas-01' },
        requestId: 'req-123',
      }),
    );

    const error = await apiRequest('/hosts/nas-01').catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    const api = error as ApiError;
    expect(api.kind).toBe('not_found');
    expect(api.status).toBe(404);
    expect(api.code).toBe('not_found');
    expect(api.message).toBe("Host 'nas-01' not found.");
    expect(api.details).toEqual({ host: 'nas-01' });
    expect(api.requestId).toBe('req-123');
  });

  it.each([
    [400, 'bad_request', 'bad_request'],
    [401, 'not_authenticated', 'unauthenticated'],
    [404, 'not_found', 'not_found'],
    [409, 'conflict', 'conflict'],
    [422, 'validation_failed', 'validation'],
    [429, 'rate_limited', 'rate_limited'],
    [500, 'internal_error', 'server'],
    [502, 'bad_gateway', 'unavailable'],
    [503, 'service_unavailable', 'unavailable'],
  ])('maps HTTP %i to kind %s -> %s', async (status, code, kind) => {
    fetchMock.mockResolvedValue(errorResponse(status, code, 'message'));

    const error = (await apiRequest('/x').catch((caught: unknown) => caught)) as ApiError;

    expect(error.kind).toBe(kind);
  });

  it('separates an authorization 403 from a CSRF 403', async () => {
    // The distinction this client exists to preserve: one means "not allowed",
    // the other means "could not verify the request". Collapsing them logs a
    // Viewer out whenever they touch an Operator action.
    fetchMock.mockResolvedValue(
      errorResponse(403, 'insufficient_permissions', 'You do not have permission.'),
    );
    const forbidden = (await apiRequest('/x', { method: 'POST' }).catch(
      (caught: unknown) => caught,
    )) as ApiError;

    fetchMock.mockResolvedValue(
      errorResponse(403, 'csrf_validation_failed', 'CSRF validation failed.'),
    );
    const csrf = (await apiRequest('/x', { method: 'POST' }).catch(
      (caught: unknown) => caught,
    )) as ApiError;

    expect(forbidden.kind).toBe('forbidden');
    expect(csrf.kind).toBe('csrf');
    expect(isPermissionDenied(forbidden)).toBe(true);
    expect(isCsrfFailure(forbidden)).toBe(false);
    expect(isCsrfFailure(csrf)).toBe(true);
    expect(isPermissionDenied(csrf)).toBe(false);
    // Neither is an expired session.
    expect(isSessionExpired(forbidden)).toBe(false);
    expect(isSessionExpired(csrf)).toBe(false);
  });

  it('treats only a 401 as an expired session', async () => {
    fetchMock.mockResolvedValue(errorResponse(401, 'not_authenticated', 'Authentication required.'));

    const error = await apiRequest('/auth/me').catch((caught: unknown) => caught);

    expect(isSessionExpired(error)).toBe(true);
  });

  it('reads Retry-After from a 429', async () => {
    fetchMock.mockResolvedValue(
      errorResponse(429, 'rate_limited', 'Too many attempts.', {
        headers: { 'Retry-After': '42' },
      }),
    );

    const error = (await apiRequest('/auth/login', { method: 'POST' }).catch(
      (caught: unknown) => caught,
    )) as ApiError;

    expect(error.kind).toBe('rate_limited');
    expect(error.retryAfterSeconds).toBe(42);
  });

  it('does not treat a rate limit as retryable', async () => {
    // Auto-retrying a throttled login is an automatic password-retry loop and
    // would spend the user's remaining budget for them.
    fetchMock.mockResolvedValue(errorResponse(429, 'rate_limited', 'Too many attempts.'));

    const error = await apiRequest('/auth/login', { method: 'POST' }).catch(
      (caught: unknown) => caught,
    );

    expect(isRetryable(error)).toBe(false);
  });

  it('exposes field errors from a 422', async () => {
    fetchMock.mockResolvedValue(
      errorResponse(422, 'validation_failed', 'The request payload is invalid.', {
        details: {
          fields: [{ location: 'body.username', message: 'Field required', type: 'missing' }],
        },
      }),
    );

    const error = (await apiRequest('/auth/login', { method: 'POST' }).catch(
      (caught: unknown) => caught,
    )) as ApiError;

    expect(error.fieldErrors).toHaveLength(1);
    expect(error.fieldErrors[0]?.location).toBe('body.username');
  });

  it('falls back to a fixed message when the body is not an envelope', async () => {
    // A proxy's own 502 page, for example. Parsing must not be what turns a
    // 502 into an unhandled exception.
    fetchMock.mockResolvedValue(
      new Response('<html>502 Bad Gateway</html>', {
        status: 502,
        headers: { 'Content-Type': 'text/html' },
      }),
    );

    const error = (await apiRequest('/auth/me').catch((caught: unknown) => caught)) as ApiError;

    expect(error.kind).toBe('unavailable');
    expect(error.message).toContain('temporarily unavailable');
    expect(error.message).not.toContain('<html>');
  });

  it('reports a malformed success body as a server error', async () => {
    fetchMock.mockResolvedValue(
      new Response('not json', { status: 200, headers: { 'Content-Type': 'application/json' } }),
    );

    const error = (await apiRequest('/auth/me').catch((caught: unknown) => caught)) as ApiError;

    expect(error.kind).toBe('server');
    expect(error.message).toContain('malformed');
  });
});

describe('transport failures', () => {
  it('reports a network failure without leaking the underlying message', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch http://internal:8000/api/v1/auth/me'));

    const error = (await apiRequest('/auth/me').catch((caught: unknown) => caught)) as ApiError;

    expect(error.kind).toBe('network');
    expect(isBackendUnreachable(error)).toBe(true);
    expect(error.message).not.toContain('internal:8000');
  });

  it('distinguishes a timeout from a caller cancellation', async () => {
    vi.useFakeTimers();
    fetchMock.mockImplementation(
      (_url, init) =>
        new Promise<Response>((_resolve, reject) => {
          (init!).signal?.addEventListener('abort', () => {
            reject(new DOMException('aborted', 'AbortError'));
          });
        }),
    );

    const pending = apiRequest('/slow', { timeoutMs: 100 }).catch((caught: unknown) => caught);
    await vi.advanceTimersByTimeAsync(150);
    const error = (await pending) as ApiError;

    expect(error.kind).toBe('timeout');
    expect(isBackendUnreachable(error)).toBe(true);
  });

  it('reports a caller cancellation as aborted, not as a fault', async () => {
    const controller = new AbortController();
    fetchMock.mockImplementation(
      (_url, init) =>
        new Promise<Response>((_resolve, reject) => {
          (init!).signal?.addEventListener('abort', () => {
            reject(new DOMException('aborted', 'AbortError'));
          });
        }),
    );

    const pending = apiRequest('/slow', { signal: controller.signal }).catch(
      (caught: unknown) => caught,
    );
    controller.abort();
    const error = (await pending) as ApiError;

    expect(error.kind).toBe('aborted');
    // An abort is routine teardown, so it must not be shown as an outage.
    expect(isBackendUnreachable(error)).toBe(false);
    expect(isRetryable(error)).toBe(false);
  });

  it('aborts immediately when the caller signal is already aborted', async () => {
    const controller = new AbortController();
    controller.abort();
    fetchMock.mockImplementation(
      (_url, init) =>
        new Promise<Response>((_resolve, reject) => {
          if ((init!).signal?.aborted) {
            reject(new DOMException('aborted', 'AbortError'));
          }
        }),
    );

    const error = (await apiRequest('/x', { signal: controller.signal }).catch(
      (caught: unknown) => caught,
    )) as ApiError;

    expect(error.kind).toBe('aborted');
  });

  it('clears the timeout once a request settles', async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(jsonResponse({ ok: true }));

    await apiRequest('/fast', { timeoutMs: 50 });
    // If the timer were left pending, advancing past it would abort a
    // controller for a finished request and, in a real app, leak a timer per
    // request.
    await vi.advanceTimersByTimeAsync(200);

    expect(vi.getTimerCount()).toBe(0);
  });
});
