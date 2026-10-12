/**
 * Authentication and setup calls.
 *
 * Thin named wrappers over {@link apiRequest}, so components never hold a URL
 * string and the contract lives in one place.
 */

import { apiRequest } from '@/api/client';
import type { CurrentUser, LoginCredentials, LogoutResult, SetupStatus } from '@/api/types';

/**
 * Whether this installation has been initialized.
 *
 * Public — the client has no credential at this point. Returns one boolean;
 * see `apps/server/hcc/api/v1/setup.py` for why that is safe to disclose and
 * what is deliberately excluded.
 */
export function fetchSetupStatus(signal?: AbortSignal): Promise<SetupStatus> {
  return apiRequest<SetupStatus>('/setup/status', { signal });
}

/**
 * Resolve the current session.
 *
 * Throws an `ApiError` of kind `unauthenticated` when there is no usable
 * session, which is the ordinary unauthenticated case rather than a fault.
 */
export function fetchCurrentUser(signal?: AbortSignal): Promise<CurrentUser> {
  return apiRequest<CurrentUser>('/auth/me', { signal });
}

/**
 * Exchange credentials for a session.
 *
 * The password is passed in the request body and is never placed in the URL,
 * logged, or retained after this call. The browser sets the session and CSRF
 * cookies from the response; nothing is stored by this client.
 */
export function login(credentials: LoginCredentials, signal?: AbortSignal): Promise<CurrentUser> {
  return apiRequest<CurrentUser>('/auth/login', {
    method: 'POST',
    body: credentials,
    signal,
  });
}

/**
 * Revoke the current session server-side.
 *
 * Carries the CSRF token automatically (see `client.ts`). The server-side
 * revocation is the boundary — the cookies it clears are a courtesy — so a
 * failure here must be surfaced rather than treated as a successful logout.
 */
export function logout(signal?: AbortSignal): Promise<LogoutResult> {
  return apiRequest<LogoutResult>('/auth/logout', { method: 'POST', signal });
}
