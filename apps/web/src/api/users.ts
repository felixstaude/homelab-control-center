/**
 * User administration calls.
 *
 * Thin named wrappers over {@link apiRequest}, like `api/auth.ts`, so no
 * component holds a URL string and the contract lives in one place. Every
 * function here reaches an Administrator-only endpoint; the server refuses the
 * rest regardless of what the interface rendered.
 */

import { apiRequest } from '@/api/client';
import type {
  CreateUserRequest,
  ManagedUser,
  SessionRevocationResult,
  UpdateUserRequest,
  UserListParams,
  UserListResult,
} from '@/api/types';

/**
 * Serialise list parameters into a query string.
 *
 * Omits anything undefined rather than sending an empty value: the backend
 * treats an absent `is_active` as "both" and an absent `search` as "no filter",
 * so `?search=` would be a different, narrower question than asking nothing.
 *
 * The camelCase-to-snake_case mapping is explicit rather than mechanical,
 * because the two names differ in exactly one place and a generic converter
 * would be more code than the mapping it replaces.
 */
function listQuery(params: UserListParams): string {
  const query = new URLSearchParams();
  if (params.search) query.set('search', params.search);
  if (params.role) query.set('role', params.role);
  if (params.isActive !== undefined) query.set('is_active', String(params.isActive));
  if (params.limit !== undefined) query.set('limit', String(params.limit));
  if (params.offset !== undefined) query.set('offset', String(params.offset));
  const rendered = query.toString();
  return rendered ? `?${rendered}` : '';
}

export function fetchUsers(
  params: UserListParams = {},
  signal?: AbortSignal,
): Promise<UserListResult> {
  return apiRequest<UserListResult>(`/users${listQuery(params)}`, { signal });
}

export function fetchUser(userId: string, signal?: AbortSignal): Promise<ManagedUser> {
  return apiRequest<ManagedUser>(`/users/${encodeURIComponent(userId)}`, { signal });
}

/**
 * Create an account.
 *
 * The password travels in the request body and is never placed in the URL,
 * logged, or retained after the call. The response carries no password
 * material — the administrator who created the account already knows it.
 */
export function createUser(body: CreateUserRequest): Promise<ManagedUser> {
  return apiRequest<ManagedUser>('/users', { method: 'POST', body });
}

/**
 * Change a display name, role or status.
 *
 * The body is passed through as given, so an omitted key really is omitted —
 * which is what lets a caller clear a display name with `null` while leaving
 * the role alone. Building the body with `undefined` values would send
 * `{"role": undefined}`, which `JSON.stringify` drops, so both spellings
 * happen to work; relying on that would be relying on a coincidence.
 */
export function updateUser(userId: string, body: UpdateUserRequest): Promise<ManagedUser> {
  return apiRequest<ManagedUser>(`/users/${encodeURIComponent(userId)}`, {
    method: 'PATCH',
    body,
  });
}

export function deactivateUser(userId: string): Promise<ManagedUser> {
  return apiRequest<ManagedUser>(`/users/${encodeURIComponent(userId)}/deactivate`, {
    method: 'POST',
  });
}

export function reactivateUser(userId: string): Promise<ManagedUser> {
  return apiRequest<ManagedUser>(`/users/${encodeURIComponent(userId)}/reactivate`, {
    method: 'POST',
  });
}

/**
 * End every live session belonging to an account.
 *
 * Distinct from deactivation: the account may sign in again immediately. The
 * result carries the count, so the interface can report what happened rather
 * than asserting success — a revocation that claims to have worked without
 * having revoked anything is the one thing this must not do.
 */
export function revokeUserSessions(userId: string): Promise<SessionRevocationResult> {
  return apiRequest<SessionRevocationResult>(
    `/users/${encodeURIComponent(userId)}/sessions/revoke`,
    { method: 'POST' },
  );
}
