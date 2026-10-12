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
  /**
   * Human-readable label, or `null` when the account has none.
   *
   * `null` means "no separate label", never "unknown" — render {@link username}
   * in that case. Use {@link userLabel} rather than writing the fallback at
   * each call site.
   */
  display_name: string | null;
  role: UserRole;
  is_active: boolean;
  /** ISO-8601 absolute session expiry. */
  session_expires_at: string;
  csrf_token: string;
};

/**
 * What to call an account in the interface.
 *
 * One helper, so the "display name, else username" fallback cannot be spelled
 * differently in different places — and so an empty-looking cell is never the
 * result of a missing fallback. The backend keeps the empty string unstorable
 * precisely so this is a two-case decision rather than three.
 */
export function userLabel(user: {
  username: string;
  display_name: string | null;
}): string {
  return user.display_name ?? user.username;
}

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

/* -------------------------------------------------------------------------
 * User administration (milestone 1.7)
 * ---------------------------------------------------------------------- */

/**
 * One account, as `GET /api/v1/users` returns it.
 *
 * Note what is absent and will stay absent: no password hash, no
 * `username_canonical`, no session token. The server's response model is an
 * allow-list rather than a serialised row, and `openapi.test.ts` asserts the
 * two sides agree **exactly** — so a new server field reaches the browser only
 * once somebody decides it should.
 */
export type ManagedUser = {
  user_id: string;
  username: string;
  display_name: string | null;
  role: UserRole;
  is_active: boolean;
  /** ISO-8601. */
  created_at: string;
  /** ISO-8601, or `null` for "has never signed in". Never render as a date. */
  last_login_at: string | null;
  /**
   * Sessions that would currently authenticate this account.
   *
   * Counts the same thing session resolution does, including the idle
   * timeout, so a non-zero count means access that actually works.
   */
  active_session_count: number;
};

/** `GET /api/v1/users`. */
export type UserListResult = {
  items: ManagedUser[];
  /** Matching the filters, ignoring pagination. */
  total: number;
  limit: number;
  offset: number;
};

/** Query parameters for the user list. All optional. */
export type UserListParams = {
  /** Substring match against the normalized username. */
  search?: string | undefined;
  role?: UserRole | undefined;
  isActive?: boolean | undefined;
  limit?: number | undefined;
  offset?: number | undefined;
};

/** `POST /api/v1/users`. */
export type CreateUserRequest = {
  username: string;
  password: string;
  display_name?: string | null;
  /** Omitted means `viewer` — the server's conservative default. */
  role?: UserRole;
  is_active?: boolean;
};

/**
 * `PATCH /api/v1/users/{id}`.
 *
 * Partial: omitting a key leaves the field unchanged. `display_name: null`
 * **clears** it, which is why the field is optional *and* nullable — the two
 * states are different and the server distinguishes them.
 *
 * `username` is deliberately not here. Usernames are immutable (ADR-041).
 */
export type UpdateUserRequest = {
  display_name?: string | null;
  role?: UserRole;
  is_active?: boolean;
};

/** `POST .../sessions/revoke` and the self-service revocations. */
export type SessionRevocationResult = {
  /** Zero is a success that changed nothing, not a failure. */
  sessions_revoked: number;
};

/* -------------------------------------------------------------------------
 * Own account (milestone 1.7)
 * ---------------------------------------------------------------------- */

/** `GET /api/v1/account`. Carries no CSRF token — that is `/auth/me`'s job. */
export type AccountDetails = {
  user_id: string;
  username: string;
  display_name: string | null;
  role: UserRole;
  is_active: boolean;
  created_at: string;
  last_login_at: string | null;
};

/**
 * One of the caller's own sessions.
 *
 * `session_id` is the row's primary key, not the credential: the session token
 * is an `HttpOnly` cookie stored server-side only as a digest, and presenting
 * an id authenticates nothing. See `apps/server/hcc/api/v1/account.py`.
 */
export type OwnSession = {
  session_id: string;
  is_current: boolean;
  created_at: string;
  /** Accurate to within the server's touch interval, not to the second. */
  last_seen_at: string;
  expires_at: string;
  created_ip: string | null;
  /**
   * The raw, truncated `User-Agent` recorded at sign-in, or `null`.
   *
   * Rendered verbatim. Deliberately not parsed into a browser or operating
   * system label: that would be a guess presented as a fact, and wrong exactly
   * for the unusual clients worth noticing.
   */
  user_agent: string | null;
};

/** `GET /api/v1/account/sessions`. */
export type OwnSessionList = {
  items: OwnSession[];
  /** Always one of `items`, so the client never infers which session it is. */
  current_session_id: string;
};

/** `POST /api/v1/account/password`. Confirmation is checked client-side. */
export type PasswordChangeRequest = {
  current_password: string;
  new_password: string;
};

/** `POST /api/v1/account/password`. */
export type PasswordChangeResult = {
  status: string;
  /** Excludes the current session, which was replaced rather than ended. */
  other_sessions_revoked: number;
  session_expires_at: string;
};

/* -------------------------------------------------------------------------
 * Audit trail (milestone 1.7)
 * ---------------------------------------------------------------------- */

/** An account referenced by an audit event. */
export type AuditActor = {
  user_id: string;
  /** `null` when the account no longer exists. Render the id. */
  username: string | null;
  display_name: string | null;
};

/**
 * One recorded privileged operation.
 *
 * `action` is a plain string rather than a union, and that is the contract
 * (ADR-038): the vocabulary grows with every milestone that records something
 * new, so a client must render an unrecognised value rather than fail on it.
 */
export type AuditEvent = {
  event_id: string;
  occurred_at: string;
  action: string;
  outcome: string;
  actor: AuditActor | null;
  target: AuditActor | null;
  request_id: string | null;
  details: Record<string, unknown> | null;
};

/** `GET /api/v1/audit/events`. */
export type AuditEventList = {
  items: AuditEvent[];
  /**
   * Opaque. Pass back as `cursor` for the next page; `null` on the last.
   *
   * Keyset rather than an offset, so events recorded while an operator reads
   * cannot duplicate or hide rows.
   */
  next_cursor: string | null;
  total: number;
  limit: number;
};

/** Query parameters for the audit list. */
export type AuditEventParams = {
  action?: string | undefined;
  targetUserId?: string | undefined;
  actorUserId?: string | undefined;
  cursor?: string | undefined;
  limit?: number | undefined;
};
