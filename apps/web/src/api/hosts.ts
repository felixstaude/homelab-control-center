/**
 * Host registry calls.
 *
 * Thin named wrappers over {@link apiRequest}, like `api/users.ts`, so no
 * component holds a URL string and the contract lives in one place.
 *
 * ## Two privilege levels, in one module
 *
 * The two reads are available to every authenticated role and the seven
 * mutations are Administrator-only. That split is the server's and is enforced
 * there (`apps/server/hcc/api/v1/hosts.py`); this module does not try to
 * reproduce it. A Viewer who called {@link createHost} would get a 403, which
 * is correct — the interface hides the control because a control that
 * silently vanishes is kinder than one that errors, not because hiding it is
 * the protection.
 *
 * ## The enrollment credential
 *
 * {@link issueEnrollment} is the only call in the whole client that returns a
 * secret. It is returned and nothing more: this module does not cache it, log
 * it, or put it in a query key. See `routes/hosts/enrollment-dialog.tsx` for
 * what the interface does with it, and why it does not keep it.
 */

import { apiRequest } from '@/api/client';
import type {
  CreateHostRequest,
  Host,
  HostListParams,
  HostListResult,
  HostRevocationResult,
  IssueEnrollmentRequest,
  IssuedEnrollment,
  UpdateHostRequest,
} from '@/api/types';

/**
 * Serialise list parameters into a query string.
 *
 * Omits anything undefined rather than sending an empty value: the backend
 * treats an absent `is_enabled` as "both" and an absent `search` as "no
 * filter", so `?search=` would be a different, narrower question than asking
 * nothing.
 */
function listQuery(params: HostListParams): string {
  const query = new URLSearchParams();
  if (params.search) query.set('search', params.search);
  if (params.lifecycle) query.set('lifecycle', params.lifecycle);
  if (params.connectivity) query.set('connectivity', params.connectivity);
  if (params.isEnabled !== undefined) query.set('is_enabled', String(params.isEnabled));
  if (params.limit !== undefined) query.set('limit', String(params.limit));
  if (params.offset !== undefined) query.set('offset', String(params.offset));
  const rendered = query.toString();
  return rendered ? `?${rendered}` : '';
}

export function fetchHosts(
  params: HostListParams = {},
  signal?: AbortSignal,
): Promise<HostListResult> {
  return apiRequest<HostListResult>(`/hosts${listQuery(params)}`, { signal });
}

export function fetchHost(hostId: string, signal?: AbortSignal): Promise<Host> {
  return apiRequest<Host>(`/hosts/${encodeURIComponent(hostId)}`, { signal });
}

/**
 * Register a host.
 *
 * Creates the record only. It does **not** mint an enrollment credential, and
 * that separation is deliberate on the server: bundling them would mean every
 * mistyped host name also created a live secret with a fifteen-minute life.
 */
export function createHost(body: CreateHostRequest): Promise<Host> {
  return apiRequest<Host>('/hosts', { method: 'POST', body });
}

/** Change a host's name, description or environment. Labels only. */
export function updateHost(hostId: string, body: UpdateHostRequest): Promise<Host> {
  return apiRequest<Host>(`/hosts/${encodeURIComponent(hostId)}`, {
    method: 'PATCH',
    body,
  });
}

/**
 * Refuse this host's agent at the door, keeping its enrollment.
 *
 * Takes effect immediately: a connected agent is disconnected within
 * milliseconds and further attempts are refused while the host is disabled.
 * Distinct from revoking the credential — disabling is "not right now" and is
 * undone by enabling.
 */
export function disableHost(hostId: string): Promise<Host> {
  return apiRequest<Host>(`/hosts/${encodeURIComponent(hostId)}/disable`, {
    method: 'POST',
  });
}

/**
 * Allow this host's agent to connect again.
 *
 * Does not reconnect anything by itself, and cannot: the controller never
 * dials out to a host. The agent is retrying on its own backoff and will
 * succeed on its next attempt.
 */
export function enableHost(hostId: string): Promise<Host> {
  return apiRequest<Host>(`/hosts/${encodeURIComponent(hostId)}/enable`, {
    method: 'POST',
  });
}

/**
 * Mint the one-time credential an agent uses to claim this host.
 *
 * **The returned credential is the only copy.** Issuing also revokes any
 * still-pending credential for the host, so the one just returned is the only
 * one that works — `superseded` reports how many stopped working.
 */
export function issueEnrollment(
  hostId: string,
  body: IssueEnrollmentRequest = {},
): Promise<IssuedEnrollment> {
  return apiRequest<IssuedEnrollment>(
    `/hosts/${encodeURIComponent(hostId)}/enrollment`,
    { method: 'POST', body },
  );
}

/**
 * Kill an enrollment credential that has not been used yet.
 *
 * Returns the count rather than asserting success, so "there was nothing
 * pending" stays distinguishable from "one was revoked".
 */
export function revokeEnrollment(hostId: string): Promise<HostRevocationResult> {
  return apiRequest<HostRevocationResult>(
    `/hosts/${encodeURIComponent(hostId)}/enrollment/revoke`,
    { method: 'POST' },
  );
}

/**
 * Withdraw trust from this host's agent.
 *
 * The agent is cut off at once and has to be re-enrolled. A host with no live
 * credential is a 409 rather than a cheerful success, so the interface can say
 * what actually happened.
 */
export function revokeCredential(hostId: string): Promise<Host> {
  return apiRequest<Host>(`/hosts/${encodeURIComponent(hostId)}/credential/revoke`, {
    method: 'POST',
  });
}
