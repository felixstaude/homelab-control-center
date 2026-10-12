/**
 * A fake control plane, for component and integration tests.
 *
 * Stubs `fetch` and answers the endpoints the client uses, with the **real**
 * response shapes and status codes — including the uniform error envelope, the
 * CSRF requirement on mutations, and the role gate on the administrative
 * routes.
 *
 * ## Why at the network boundary
 *
 * Mocking `useAuth` or the API modules would let a test pass while the
 * client's header assembly, cookie handling, query-string construction or
 * error mapping was broken. Stubbing `fetch` instead means everything between
 * the component and the socket is the code that ships. The response shapes are
 * kept honest by `src/api/openapi.test.ts`, which checks them against the
 * backend's committed schema — so this fake cannot quietly drift from the
 * server it imitates.
 *
 * ## Why it enforces rules rather than just returning fixtures
 *
 * The fake applies the role gate, rejects duplicate usernames, and refuses to
 * demote the last active administrator. That is more than a stub needs in
 * order to render a table, and it is the difference between a test that proves
 * the interface handles a 409 and one that proves it can display a happy path.
 * The rules are the *server's*, restated — so each one is a place this file
 * could drift, which is why the backend's own integration suite asserts the
 * same behaviours against real PostgreSQL. Neither suite is sufficient alone:
 * these cover what the browser does with the answer, those cover whether the
 * answer is right.
 */

import { expect, vi } from 'vitest';

import { CSRF_COOKIE_NAME } from '@/api/client';
import type {
  AuditEvent,
  CurrentUser,
  ManagedUser,
  OwnSession,
  UserRole,
} from '@/api/types';

export const TEST_CSRF_TOKEN = 'test-csrf-token-value';

/** A user as `/auth/me` would return one. */
export function makeUser(overrides: Partial<CurrentUser> = {}): CurrentUser {
  return {
    user_id: '0f3c1a2b-0000-4000-8000-000000000001',
    username: 'FelixAdmin',
    display_name: null,
    role: 'admin',
    is_active: true,
    session_expires_at: '2030-01-01T00:00:00+00:00',
    csrf_token: TEST_CSRF_TOKEN,
    ...overrides,
  };
}

/** A managed account as `GET /users` would return one. */
export function makeManagedUser(overrides: Partial<ManagedUser> = {}): ManagedUser {
  return {
    user_id: '0f3c1a2b-0000-4000-8000-000000000002',
    username: 'someone',
    display_name: null,
    role: 'viewer',
    is_active: true,
    created_at: '2026-01-02T09:00:00+00:00',
    last_login_at: null,
    active_session_count: 0,
    ...overrides,
  };
}

/** One of the caller's own sessions. */
export function makeOwnSession(overrides: Partial<OwnSession> = {}): OwnSession {
  return {
    session_id: '0f3c1a2b-0000-4000-8000-00000000f001',
    is_current: false,
    created_at: '2026-01-02T09:00:00+00:00',
    last_seen_at: '2026-01-02T09:30:00+00:00',
    expires_at: '2026-01-02T21:00:00+00:00',
    created_ip: '192.0.2.10',
    user_agent: 'Mozilla/5.0 (X11; Linux x86_64)',
    ...overrides,
  };
}

/** An audit event. */
export function makeAuditEvent(overrides: Partial<AuditEvent> = {}): AuditEvent {
  return {
    event_id: '0f3c1a2b-0000-4000-8000-00000000e001',
    occurred_at: '2026-01-02T09:00:00+00:00',
    action: 'user.created',
    outcome: 'success',
    actor: {
      user_id: '0f3c1a2b-0000-4000-8000-000000000001',
      username: 'FelixAdmin',
      display_name: null,
    },
    target: {
      user_id: '0f3c1a2b-0000-4000-8000-000000000002',
      username: 'someone',
      display_name: null,
    },
    request_id: 'test-request',
    details: { role: 'viewer' },
    ...overrides,
  };
}

/** One recorded request, for asserting on headers and bodies. */
export type RecordedRequest = {
  method: string;
  path: string;
  /** Query string without the leading `?`, for asserting on filters. */
  query: string;
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

  /* --- Milestone 1.7 --- */

  /** The account table the administrative endpoints read and write. */
  users: ManagedUser[];
  /** The caller's own sessions. */
  ownSessions: OwnSession[];
  /** The audit trail, newest first. */
  auditEvents: AuditEvent[];
  /**
   * Forces the next administrative mutation to fail this way.
   *
   * For exercising a specific refusal — a rate limit, an unexpected 500 —
   * without having to arrange the state that would produce it. Cleared after
   * it fires once, so a test cannot accidentally make every later call fail.
   */
  nextFailure: { status: number; code: string; message: string; retryAfter?: number } | null;
  /**
   * Persistent failures, keyed by the path suffix they apply to.
   *
   * Distinct from {@link nextFailure}, which fires once, and from
   * {@link transport}, which fails *everything* — including `/auth/me`, so a
   * test that only wanted the user list to fail would instead land on the
   * backend-unavailable screen and never render the panel under test. This
   * fails one route and leaves authentication working.
   */
  routeFailures: Record<string, { status: number; code: string; message: string }>;
  /** The current password `POST /account/password` will accept. */
  currentPassword: string;
};

export type FakeBackend = {
  readonly state: FakeBackendState;
  readonly requests: RecordedRequest[];
  /** Requests for one path, for counting duplicates. */
  requestsFor: (path: string) => RecordedRequest[];
  /** Restore the real `fetch` and clear cookies. */
  restore: () => void;
};

function envelope(
  status: number,
  code: string,
  message: string,
  extra: { retryAfter?: number; details?: Record<string, unknown> } = {},
): Response {
  return new Response(
    JSON.stringify({
      error: {
        code,
        message,
        request_id: 'test-request',
        ...(extra.details ? { details: extra.details } : {}),
      },
    }),
    {
      status,
      headers: {
        'Content-Type': 'application/json',
        ...(extra.retryAfter === undefined
          ? {}
          : { 'Retry-After': String(extra.retryAfter) }),
      },
    },
  );
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

/** The server's username folding, as far as these tests need it. */
function fold(username: string): string {
  return username.normalize('NFKC').trim().toLowerCase();
}

/**
 * The server's blank-to-null mapping for a display name.
 *
 * Spelled out rather than written as `value?.trim() || null`, which relies on
 * the empty string being falsy — correct, but the kind of correctness that
 * reads as an oversight and that the lint rightly flags next to a `??`.
 */
function blankToNull(value: string | null | undefined): string | null {
  const trimmed = value?.trim() ?? '';
  return trimmed === '' ? null : trimmed;
}

/**
 * A path parameter from a matched route.
 *
 * The regexes below all have exactly one capture group, so the value is always
 * present — but `RegExpExecArray` is typed as possibly-sparse, and asserting
 * past that would hide a genuinely malformed path behind a crash.
 */
function captured(match: RegExpExecArray): string {
  return decodeURIComponent(match[1] ?? '');
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
    users: [],
    ownSessions: [],
    auditEvents: [],
    nextFailure: null,
    routeFailures: {},
    currentPassword: 'correct horse battery staple',
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

  /** Whether the caller is authenticated at all. */
  const requireSession = (): Response | null =>
    state.session
      ? null
      : envelope(401, 'not_authenticated', 'Authentication required.');

  /** Whether the caller may reach an administrative route. */
  const requireAdmin = (): Response | null => {
    const unauthenticated = requireSession();
    if (unauthenticated) return unauthenticated;
    if (state.session?.role !== 'admin') {
      return envelope(
        403,
        'insufficient_permissions',
        'You do not have permission to perform this action.',
      );
    }
    return null;
  };

  /** The CSRF header the backend requires on every mutation. */
  const requireCsrf = (headers: Record<string, string>): Response | null =>
    headers['x-csrf-token']
      ? null
      : envelope(403, 'csrf_validation_failed', 'CSRF validation failed.');

  /** Consume a forced failure, if one is armed. */
  const forcedFailure = (): Response | null => {
    const failure = state.nextFailure;
    if (!failure) return null;
    state.nextFailure = null;
    return envelope(failure.status, failure.code, failure.message, {
      ...(failure.retryAfter === undefined ? {} : { retryAfter: failure.retryAfter }),
    });
  };

  /** Active administrators other than `exceptId`. The last-admin guard. */
  const otherActiveAdmins = (exceptId: string): number =>
    state.users.filter(
      (user) => user.user_id !== exceptId && user.role === 'admin' && user.is_active,
    ).length;

  const lastAdministrator = (operation: string): Response =>
    envelope(
      409,
      'last_administrator',
      `Cannot ${operation}: this is the only active administrator, and an ` +
        'installation with no active administrator cannot be recovered through ' +
        'the interface. Grant the Administrator role to another active account first.',
    );

  const handler = async (
    input: RequestInfo | URL,
    init?: RequestInit,
  ): Promise<Response> => {
    const raw = typeof input === 'string' ? input : input instanceof URL ? input.href : '';
    const [path = '', query = ''] = raw.split('?');
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
    requests.push({ method, path, query, headers, body });

    if (state.transport === 'network') {
      throw new TypeError('Failed to fetch');
    }
    if (state.transport === 'timeout') {
      // Never settles; the client's own deadline aborts it.
      return new Promise<Response>(() => {
        /* intentionally pending */
      });
    }

    for (const [suffix, failure] of Object.entries(state.routeFailures)) {
      if (path.endsWith(suffix)) {
        return envelope(failure.status, failure.code, failure.message);
      }
    }

    /* --- Authentication and setup (milestone 1.6) --- */

    if (path.endsWith('/setup/status') && method === 'GET') {
      return json({ initialized: state.initialized });
    }

    if (path.endsWith('/auth/me') && method === 'GET') {
      const refused = requireSession();
      if (refused) return refused;
      return json(state.session);
    }

    if (path.endsWith('/auth/login') && method === 'POST') {
      if (state.loginFailure) {
        const failure = state.loginFailure;
        return envelope(failure.status, failure.code, failure.message, {
          ...(failure.retryAfter === undefined ? {} : { retryAfter: failure.retryAfter }),
        });
      }
      state.session = state.loginResult;
      // The real backend sets this cookie, readable on purpose so the client
      // can echo it in a header. Setting it here exercises that whole path.
      document.cookie = `${CSRF_COOKIE_NAME}=${state.loginResult.csrf_token}; path=/`;
      return json(state.loginResult);
    }

    if (path.endsWith('/auth/logout') && method === 'POST') {
      const refusedCsrf = requireCsrf(headers);
      if (refusedCsrf) return refusedCsrf;
      if (state.logoutFails) {
        return envelope(500, 'internal_error', 'An internal error occurred.');
      }
      state.session = null;
      clearCookies();
      return json({ status: 'logged_out' });
    }

    if (path.endsWith('/info') && method === 'GET') {
      const refused = requireSession();
      if (refused) return refused;
      return json({ api_version: 'v1', server_version: '0.1.0', environment: 'test' });
    }

    /* --- Own account (milestone 1.7) --- */

    if (path.endsWith('/account') && method === 'GET') {
      const refused = requireSession();
      if (refused) return refused;
      // `requireSession` returned null, so there is a session; read it into a
      // local rather than asserting past the nullable field.
      const session = state.session;
      if (!session) return envelope(401, 'not_authenticated', 'Authentication required.');
      return json({
        user_id: session.user_id,
        username: session.username,
        display_name: session.display_name,
        role: session.role,
        is_active: session.is_active,
        created_at: '2026-01-01T08:00:00+00:00',
        last_login_at: '2026-01-02T09:00:00+00:00',
      });
    }

    if (path.endsWith('/account/sessions') && method === 'GET') {
      const refused = requireSession();
      if (refused) return refused;
      const current =
        state.ownSessions.find((session) => session.is_current) ?? state.ownSessions[0];
      return json({
        items: state.ownSessions,
        current_session_id: current?.session_id ?? '',
      });
    }

    if (path.endsWith('/account/sessions/revoke-others') && method === 'POST') {
      const refused = requireSession() ?? requireCsrf(headers) ?? forcedFailure();
      if (refused) return refused;
      const removed = state.ownSessions.filter((session) => !session.is_current).length;
      state.ownSessions = state.ownSessions.filter((session) => session.is_current);
      return json({ sessions_revoked: removed });
    }

    const revokeOne = /\/account\/sessions\/([^/]+)\/revoke$/.exec(path);
    if (revokeOne && method === 'POST') {
      const refused = requireSession() ?? requireCsrf(headers) ?? forcedFailure();
      if (refused) return refused;
      const sessionId = captured(revokeOne);
      const before = state.ownSessions.length;
      state.ownSessions = state.ownSessions.filter(
        (session) => session.session_id !== sessionId,
      );
      if (state.ownSessions.length === before) {
        return envelope(
          404,
          'not_found',
          'That session does not exist, or is no longer active.',
        );
      }
      return json({ sessions_revoked: 1 });
    }

    if (path.endsWith('/account/password') && method === 'POST') {
      const refused = requireSession() ?? requireCsrf(headers) ?? forcedFailure();
      if (refused) return refused;
      const payload = body as { current_password?: string; new_password?: string };
      if (payload.current_password !== state.currentPassword) {
        // 422 and never 401 — a 401 would be read by the client as an expired
        // session and sign the user out for a typo.
        return envelope(422, 'invalid_current_password', 'The current password is incorrect.');
      }
      const newPassword = payload.new_password ?? '';
      if (newPassword.length < 8) {
        return envelope(422, 'validation_failed', 'The new password does not meet the policy.', {
          details: { problems: ['password must be at least 8 characters'] },
        });
      }
      const otherRevoked = state.ownSessions.filter((session) => !session.is_current).length;
      state.ownSessions = state.ownSessions.filter((session) => session.is_current);
      state.currentPassword = newPassword;
      return json({
        status: 'password_changed',
        other_sessions_revoked: otherRevoked,
        session_expires_at: '2030-01-01T00:00:00+00:00',
      });
    }

    /* --- User administration (milestone 1.7) --- */

    if (path.endsWith('/users') && method === 'GET') {
      const refused = requireAdmin();
      if (refused) return refused;

      const params = new URLSearchParams(query);
      const search = fold(params.get('search') ?? '');
      const role = params.get('role');
      const isActive = params.get('is_active');
      const limit = Number(params.get('limit') ?? '25');
      const offset = Number(params.get('offset') ?? '0');

      const matching = state.users
        .filter((user) => (search ? fold(user.username).includes(search) : true))
        .filter((user) => (role ? user.role === role : true))
        .filter((user) =>
          isActive === null ? true : user.is_active === (isActive === 'true'),
        )
        // The server orders by canonical username with the id breaking ties.
        .sort((left, right) => fold(left.username).localeCompare(fold(right.username)));

      return json({
        items: matching.slice(offset, offset + limit),
        total: matching.length,
        limit,
        offset,
      });
    }

    if (path.endsWith('/users') && method === 'POST') {
      const refused = requireAdmin() ?? requireCsrf(headers) ?? forcedFailure();
      if (refused) return refused;

      const payload = body as {
        username?: string;
        password?: string;
        display_name?: string | null;
        role?: UserRole;
        is_active?: boolean;
      };
      const username = (payload.username ?? '').trim();

      if (state.users.some((user) => fold(user.username) === fold(username))) {
        return envelope(
          409,
          'username_taken',
          `The username '${username}' is already taken. Usernames are ` +
            'case-insensitive, so a different capitalisation is still the same account.',
        );
      }
      if (fold(username).length < 3) {
        return envelope(422, 'validation_failed', 'username must be at least 3 characters');
      }
      if ((payload.password ?? '').length < 8) {
        return envelope(422, 'validation_failed', 'The password does not meet the policy.', {
          details: { problems: ['password must be at least 8 characters'] },
        });
      }

      const created = makeManagedUser({
        user_id: `generated-${state.users.length + 1}`,
        username,
        display_name: blankToNull(payload.display_name),
        // Matches the server's conservative default.
        role: payload.role ?? 'viewer',
        is_active: payload.is_active ?? true,
      });
      state.users.push(created);
      return json(created, 201);
    }

    const userPath = /\/users\/([^/]+)$/.exec(path);
    if (userPath && (method === 'GET' || method === 'PATCH')) {
      const refused =
        method === 'PATCH'
          ? (requireAdmin() ?? requireCsrf(headers) ?? forcedFailure())
          : requireAdmin();
      if (refused) return refused;

      const userId = captured(userPath);
      const user = state.users.find((candidate) => candidate.user_id === userId);
      if (!user) {
        return envelope(404, 'not_found', 'That user account does not exist.');
      }
      if (method === 'GET') return json(user);

      const payload = body as {
        display_name?: string | null;
        role?: UserRole;
        is_active?: boolean;
      };
      const losesAdmin =
        (payload.role !== undefined && payload.role !== 'admin') ||
        payload.is_active === false;
      if (
        losesAdmin &&
        user.role === 'admin' &&
        user.is_active &&
        otherActiveAdmins(user.user_id) === 0
      ) {
        return lastAdministrator(
          payload.is_active === false
            ? 'deactivate this administrator'
            : "change this administrator's role",
        );
      }

      if ('display_name' in payload) {
        user.display_name = blankToNull(payload.display_name);
      }
      if (payload.role !== undefined) user.role = payload.role;
      if (payload.is_active !== undefined) {
        user.is_active = payload.is_active;
        if (!payload.is_active) user.active_session_count = 0;
      }
      return json(user);
    }

    const activation = /\/users\/([^/]+)\/(deactivate|reactivate)$/.exec(path);
    if (activation && method === 'POST') {
      const refused = requireAdmin() ?? requireCsrf(headers) ?? forcedFailure();
      if (refused) return refused;

      const userId = captured(activation);
      const user = state.users.find((candidate) => candidate.user_id === userId);
      if (!user) {
        return envelope(404, 'not_found', 'That user account does not exist.');
      }

      const activating = activation[2] === 'reactivate';
      if (
        !activating &&
        user.role === 'admin' &&
        user.is_active &&
        otherActiveAdmins(user.user_id) === 0
      ) {
        return lastAdministrator('deactivate this administrator');
      }

      user.is_active = activating;
      if (!activating) user.active_session_count = 0;
      return json(user);
    }

    const revokeUserSessions = /\/users\/([^/]+)\/sessions\/revoke$/.exec(path);
    if (revokeUserSessions && method === 'POST') {
      const refused = requireAdmin() ?? requireCsrf(headers) ?? forcedFailure();
      if (refused) return refused;

      const userId = captured(revokeUserSessions);
      const user = state.users.find((candidate) => candidate.user_id === userId);
      if (!user) {
        return envelope(404, 'not_found', 'That user account does not exist.');
      }
      const revoked = user.active_session_count;
      user.active_session_count = 0;
      return json({ sessions_revoked: revoked });
    }

    /* --- Audit (milestone 1.7) --- */

    if (path.endsWith('/audit/actions') && method === 'GET') {
      const refused = requireAdmin();
      if (refused) return refused;
      return json([
        'user.created',
        'user.updated',
        'user.role_changed',
        'user.deactivated',
        'user.reactivated',
        'user.sessions_revoked',
        'account.password_changed',
        'account.sessions_revoked',
        'admin.access_denied',
      ]);
    }

    if (path.endsWith('/audit/events') && method === 'GET') {
      const refused = requireAdmin();
      if (refused) return refused;

      const params = new URLSearchParams(query);
      const action = params.get('action');
      const cursor = params.get('cursor');
      const limit = Number(params.get('limit') ?? '25');

      if (cursor !== null && !cursor.startsWith('cursor-')) {
        return envelope(
          422,
          'validation_failed',
          'The pagination cursor is not valid. It should be the `next_cursor` ' +
            'from a previous response, unmodified.',
        );
      }

      const matching = state.auditEvents.filter((event) =>
        action ? event.action === action : true,
      );
      // Keyset paging, imitated: the cursor carries the index after the last
      // row already seen, which is what the real opaque token encodes a
      // position for.
      const start = cursor ? Number(cursor.replace('cursor-', '')) : 0;
      const page = matching.slice(start, start + limit);
      const nextIndex = start + limit;

      return json({
        items: page,
        next_cursor: nextIndex < matching.length ? `cursor-${nextIndex}` : null,
        total: matching.length,
        limit,
      });
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

/**
 * A user with a specific role.
 *
 * The usernames deliberately do not resemble the role labels the interface
 * renders ("Viewer", "Operator", "Administrator"): a fixture named `Operator`
 * makes `getByText('Operator')` ambiguous, which looks like a component bug.
 */
const USERNAME_BY_ROLE: Record<UserRole, string> = {
  viewer: 'Ren',
  operator: 'Casey',
  admin: 'Morgan',
};

export function userWithRole(role: UserRole): CurrentUser {
  return makeUser({ role, username: USERNAME_BY_ROLE[role] });
}
