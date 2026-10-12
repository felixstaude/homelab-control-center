/**
 * The authentication state machine.
 *
 * ## Why a discriminated union and not booleans
 *
 * `isLoading`, `isAuthenticated` and `error` as three independent values
 * describe sixteen combinations, most of which are nonsense — "loading and
 * authenticated and errored" is representable but meaningless — and the UI
 * then has to guess precedence between them. The guessing is where the bugs
 * live: the common one is rendering a protected page for a frame because
 * `isAuthenticated` defaulted to `false` and `isLoading` had not been checked
 * first.
 *
 * One `status` makes the states exhaustive and mutually exclusive, so a
 * `switch` either handles every case or fails to typecheck.
 *
 * ## The states
 *
 * ```
 *                     ┌──────────┐
 *                     │ checking │  startup, and any session re-check
 *                     └────┬─────┘
 *        ┌─────────────────┼──────────────────┬───────────────────┐
 *        ▼                 ▼                  ▼                   ▼
 * ┌──────────────┐  ┌───────────────┐  ┌───────────────┐  ┌─────────────┐
 * │setup_required│  │unauthenticated│  │ authenticated │  │ unavailable │
 * └──────────────┘  └───────┬───────┘  └───────┬───────┘  └──────┬──────┘
 *        │                  │  sign in ▲       │                 │ retry
 *        │ bootstrap        │          └───────┘ sign out        │
 *        └──────────────────┴────────────────────────────────────┘
 * ```
 *
 * `unavailable` is deliberately **not** folded into `unauthenticated`. A
 * dropped network must never be reported as a logout: it is not true, it
 * discards the user's place in the app, and on a flaky connection it produces
 * a login page that appears for no reason.
 */

import type { ApiError } from '@/api/errors';
import type { CurrentUser, UserRole } from '@/api/types';
import type { Permissions } from '@/auth/permissions';

/** Why the user is unauthenticated. Shapes the message on the login page. */
export type UnauthenticatedReason =
  /** First visit, or a reload with no session cookie. Say nothing. */
  | 'no_session'
  /** A session existed and stopped being valid. Worth explaining. */
  | 'session_expired'
  /** The user asked to sign out. Confirm it happened. */
  | 'signed_out';

export type AuthState =
  /**
   * Resolving. Shown on first load and while a session is re-checked.
   *
   * Nothing protected may render in this state — that is the whole reason it
   * is distinct from `unauthenticated`.
   */
  | { readonly status: 'checking' }
  /** The installation has no administrator yet. */
  | { readonly status: 'setup_required' }
  | {
      readonly status: 'unauthenticated';
      readonly reason: UnauthenticatedReason;
      /** Set when a sign-in attempt failed, for an inline message. */
      readonly error: ApiError | null;
    }
  | {
      readonly status: 'authenticated';
      readonly user: CurrentUser;
      readonly role: UserRole;
      readonly permissions: Permissions;
    }
  /**
   * The control plane could not be reached.
   *
   * Distinct from every other state: the session may well be fine, and the
   * remedy is to retry rather than to sign in.
   */
  | { readonly status: 'unavailable'; readonly error: ApiError };

/** Whether a protected route may render. */
export function isAuthenticated(state: AuthState): state is Extract<
  AuthState,
  { status: 'authenticated' }
> {
  return state.status === 'authenticated';
}

/**
 * Whether the app is still deciding.
 *
 * Route guards must treat this as "render nothing yet", never as
 * "unauthenticated" — otherwise a reload of a protected page flashes the login
 * screen before the session resolves.
 */
export function isResolving(state: AuthState): boolean {
  return state.status === 'checking';
}
