/**
 * The API contract this client depends on.
 *
 * ## Why these are hand-written, for now
 *
 * ADR-007 chose generated types over hand-maintained ones, and the roadmap
 * reserves full OpenAPI generation for **milestone 1.15** ("Generated API
 * client … drift fails CI"). Taking that over here would be scope this
 * milestone was not asked for, so instead:
 *
 * - these four response shapes are written out by hand, because four small
 *   objects are cheap and a generator is not needed to express them;
 * - and `openapi.test.ts` validates every one of them against the backend's
 *   **real OpenAPI schema**, committed as a snapshot, so the types cannot
 *   drift from the server without a test failing.
 *
 * That is the minimum typed contract with genuine drift detection. Milestone
 * 1.15 replaces the hand-written half with generation; the drift check is the
 * part worth having first, because a type that merely *looks* right is the
 * failure mode generation exists to prevent.
 *
 * Regenerate the snapshot with `make openapi` after any API change.
 */

/** A role, as the backend's `user_role` enum spells it (ADR-013). */
export type UserRole = 'viewer' | 'operator' | 'admin';

/**
 * `GET /api/v1/auth/me` and `POST /api/v1/auth/login`.
 *
 * Both return the same shape. `csrf_token` is included because the client
 * needs it to make state-changing requests and it is useless without the
 * session cookie (ADR-030) — but the client reads it from the cookie rather
 * than holding this copy, so that it can never be stale.
 */
export type CurrentUser = {
  user_id: string;
  /** Display form, as the user typed it at creation. */
  username: string;
  role: UserRole;
  is_active: boolean;
  /** ISO-8601 absolute session expiry. */
  session_expires_at: string;
  csrf_token: string;
};

/** `POST /api/v1/auth/logout`. */
export type LogoutResult = {
  status: 'logged_out';
};

/**
 * `GET /api/v1/setup/status`.
 *
 * One field, deliberately: it is a public endpoint. See
 * `apps/server/hcc/api/v1/setup.py`.
 */
export type SetupStatus = {
  initialized: boolean;
};

/** `GET /api/v1/info`. Authenticated since milestone 1.4. */
export type ApiInfo = {
  api_version: string;
  server_version: string;
  environment: string;
};

/** Credentials submitted to the login endpoint. */
export type LoginCredentials = {
  username: string;
  password: string;
};
