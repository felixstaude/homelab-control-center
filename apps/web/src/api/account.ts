/**
 * Self-service account calls.
 *
 * Every one is scoped to the signed-in user by the server, from the resolved
 * session — there is no parameter naming whose account to act on, which is why
 * these need no role.
 */

import { apiRequest } from '@/api/client';
import type {
  AccountDetails,
  OwnSessionList,
  PasswordChangeRequest,
  PasswordChangeResult,
  SessionRevocationResult,
} from '@/api/types';

export function fetchAccount(signal?: AbortSignal): Promise<AccountDetails> {
  return apiRequest<AccountDetails>('/account', { signal });
}

export function fetchOwnSessions(signal?: AbortSignal): Promise<OwnSessionList> {
  return apiRequest<OwnSessionList>('/account/sessions', { signal });
}

/**
 * Revoke one of the caller's own sessions.
 *
 * Revoking the current one is permitted and is a sign-out: the client learns
 * on its next request, which answers 401, and the authentication state already
 * handles that.
 */
export function revokeOwnSession(sessionId: string): Promise<SessionRevocationResult> {
  return apiRequest<SessionRevocationResult>(
    `/account/sessions/${encodeURIComponent(sessionId)}/revoke`,
    { method: 'POST' },
  );
}

export function revokeOtherSessions(): Promise<SessionRevocationResult> {
  return apiRequest<SessionRevocationResult>('/account/sessions/revoke-others', {
    method: 'POST',
  });
}

/**
 * Change the caller's own password.
 *
 * Two things the caller must know about the response:
 *
 * - **The session is replaced.** The server revokes every session including
 *   this one and issues a new pair of cookies, so the browser stays signed in
 *   with fresh material while the token it held before stops working
 *   (ADR-040). Nothing is needed from the client for that — the cookies arrive
 *   on this response — but any cached CSRF value would now be stale, which is
 *   why the client reads the token per request rather than holding one.
 * - **A wrong current password is a 422, not a 401.** So it surfaces as an
 *   `ApiError` of kind `validation` with code `invalid_current_password`, and
 *   must be shown against the field rather than treated as an expired session.
 */
export function changePassword(body: PasswordChangeRequest): Promise<PasswordChangeResult> {
  return apiRequest<PasswordChangeResult>('/account/password', { method: 'POST', body });
}
