/**
 * A fake control plane, for component and integration tests.
 *
 * Stubs `fetch` and answers the four endpoints the client uses, with the
 * **real** response shapes and status codes — including the uniform error
 * envelope and the CSRF requirement on mutations.
 *
 * ## Why at the network boundary
 *
 * Mocking `useAuth` or the API module would let a test pass while the client's
 * header assembly, cookie handling or error mapping was broken. Stubbing
 * `fetch` instead means everything between the component and the socket is the
 * code that ships. The response shapes are kept honest by
 * `src/api/openapi.test.ts`, which checks them against the backend's committed
 * schema — so this fake cannot quietly drift from the server it imitates.
 */

import { expect, vi } from 'vitest';

import { CSRF_COOKIE_NAME } from '@/api/client';
import type { CurrentUser, UserRole } from '@/api/types';

export const TEST_CSRF_TOKEN = 'test-csrf-token-value';

/** A user as `/auth/me` would return one. */
export function makeUser(overrides: Partial<CurrentUser> = {}): CurrentUser {
  return {
    user_id: '0f3c1a2b-0000-4000-8000-000000000001',
    username: 'FelixAdmin',
    role: 'admin',
    is_active: true,
    session_expires_at: '2030-01-01T00:00:00+00:00',
    csrf_token: TEST_CSRF_TOKEN,
    ...overrides,
  };
}

/** One recorded request, for asserting on headers and bodies. */
export type RecordedRequest = {
  method: string;
  path: string;
  headers: Record<string, string>;
  body: unknown;
};

/** How the fake should behave. Every field is mutable mid-test. */
export type FakeBackendState = {
  /** `/setup/status`. */
  initialized: boolean;
  /** The session `/auth/me` reports. `null` means unauthenticated. */
  session: CurrentUser | null;
  /** When set, every request fails this way before reaching a route. */
  transport: 'ok' | 'network' | 'timeout';
  /** When set, login fails with this instead of succeeding. */
  loginFailure: { status: number; code: string; message: string; retryAfter?: number } | null;
  /** When true, logout answers 500 — server-side revocation did not happen. */
  logoutFails: boolean;
  /** The user login returns on success. Defaults to {@link makeUser}. */
  loginResult: CurrentUser;
};

export type FakeBackend = {
  readonly state: FakeBackendState;
  readonly requests: RecordedRequest[];
  /** Requests for one path, for counting duplicates. */
  requestsFor: (path: string) => RecordedRequest[];
  /** Restore the real `fetch` and clear cookies. */
  restore: () => void;
};

function envelope(status: number, code: string, message: string, retryAfter?: number): Response {
  return new Response(JSON.stringify({ error: { code, message, request_id: 'test-request' } }), {
    status,
    headers: {
      'Content-Type': 'application/json',
      ...(retryAfter === undefined ? {} : { 'Retry-After': String(retryAfter) }),
    },
  });
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function clearCookies(): void {
  for (const part of document.cookie.split(';')) {
    const name = part.split('=')[0]?.trim();
    if (name) document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
  }
}

/**
 * Install the fake. Call {@link FakeBackend.restore} in an `afterEach`.
 */
export function installFakeBackend(initial: Partial<FakeBackendState> = {}): FakeBackend {
  const state: FakeBackendState = {
    initialized: true,
    session: null,
    transport: 'ok',
    loginFailure: null,
    logoutFails: false,
    loginResult: makeUser(),
    ...initial,
  };

  const requests: RecordedRequest[] = [];
  clearCookies();

  // A browser holding a live session also holds the readable CSRF cookie the
  // server set at login. Seeding it here keeps the fake faithful: without it,
  // every test that starts signed-in would have to remember to set the cookie
  // before a mutation, and forgetting would look like a client bug.
  if (state.session) {
    document.cookie = `${CSRF_COOKIE_NAME}=${state.session.csrf_token}; path=/`;
  }

  const handler = async (
    input: RequestInfo | URL,
    init?: RequestInit,
  ): Promise<Response> => {
    const path = typeof input === 'string' ? input : input instanceof URL ? input.pathname : '';
    const method = (init?.method ?? 'GET').toUpperCase();
    const headers: Record<string, string> = {};
    new Headers(init?.headers).forEach((value, key) => {
      headers[key.toLowerCase()] = value;
    });

    let body: unknown = undefined;
    if (typeof init?.body === 'string') {
      try {
        body = JSON.parse(init.body);
      } catch {
        body = init.body;
      }
    }
    requests.push({ method, path, headers, body });

    if (state.transport === 'network') {
      throw new TypeError('Failed to fetch');
    }
    if (state.transport === 'timeout') {
      // Never settles; the client's own deadline aborts it.
      return new Promise<Response>(() => {
        /* intentionally pending */
      });
    }

    if (path.endsWith('/setup/status') && method === 'GET') {
      return json({ initialized: state.initialized });
    }

    if (path.endsWith('/auth/me') && method === 'GET') {
      if (!state.session) {
        return envelope(401, 'not_authenticated', 'Authentication required.');
      }
      return json(state.session);
    }

    if (path.endsWith('/auth/login') && method === 'POST') {
      if (state.loginFailure) {
        const failure = state.loginFailure;
        return envelope(failure.status, failure.code, failure.message, failure.retryAfter);
      }
      state.session = state.loginResult;
      // The real backend sets this cookie, readable on purpose so the client
      // can echo it in a header. Setting it here exercises that whole path.
      document.cookie = `${CSRF_COOKIE_NAME}=${state.loginResult.csrf_token}; path=/`;
      return json(state.loginResult);
    }

    if (path.endsWith('/auth/logout') && method === 'POST') {
      // The real endpoint requires the CSRF header; a test that forgot it
      // should see the same 403 a browser would.
      if (!headers['x-csrf-token']) {
        return envelope(403, 'csrf_validation_failed', 'CSRF validation failed.');
      }
      if (state.logoutFails) {
        return envelope(500, 'internal_error', 'An internal error occurred.');
      }
      state.session = null;
      clearCookies();
      return json({ status: 'logged_out' });
    }

    return envelope(404, 'not_found', 'The requested resource does not exist.');
  };

  vi.stubGlobal('fetch', vi.fn(handler));

  return {
    state,
    requests,
    requestsFor: (path: string) => requests.filter((request) => request.path.endsWith(path)),
    restore: () => {
      vi.unstubAllGlobals();
      clearCookies();
    },
  };
}

/** Assert a request carried the CSRF header the backend requires. */
export function expectCsrfHeader(request: RecordedRequest | undefined): void {
  expect(request, 'no such request was made').toBeDefined();
  expect(request?.headers['x-csrf-token']).toBe(TEST_CSRF_TOKEN);
}

/** A role other than the default administrator. */
export function userWithRole(role: UserRole): CurrentUser {
  return makeUser({ role, username: role === 'viewer' ? 'ReadOnly' : 'Operator' });
}
