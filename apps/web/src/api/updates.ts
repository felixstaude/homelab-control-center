/**
 * Update management calls.
 *
 * Thin named wrappers over {@link apiRequest}, like `api/hosts.ts`, so no
 * component holds a URL string.
 *
 * ## Two privilege levels, as elsewhere
 *
 * The four reads are available to every authenticated role — a viewer who
 * cannot see that the fleet is behind cannot do their job — and the three
 * mutations are Administrator-only. That split is the server's and is enforced
 * there (`apps/server/hcc/api/v1/updates.py`); this module does not reproduce
 * it. The interface hides the controls a role cannot use because a control
 * that silently vanishes is kinder than one that errors, not because hiding
 * it is the protection.
 *
 * ## Nothing here returns a secret
 *
 * An update plan carries a download URL and a digest, both of which are
 * public release metadata. There is no credential in any of these responses,
 * which is asserted against the real bodies in the backend suite.
 */

import { apiRequest } from '@/api/client';
import type {
  FleetUpdateOverview,
  HostUpdateStatus,
  StartUpdateRequest,
  UpdateEvent,
  UpdateHistory,
  UpdateJob,
  UpdatePolicyRequest,
} from '@/api/types';

/** The fleet's update status. Every authenticated role. */
export function fetchUpdateOverview(signal?: AbortSignal): Promise<FleetUpdateOverview> {
  return apiRequest<FleetUpdateOverview>('/api/v1/updates/overview', { signal });
}

/** Every host's update situation. Every authenticated role. */
export function fetchHostUpdateStatuses(signal?: AbortSignal): Promise<HostUpdateStatus[]> {
  return apiRequest<HostUpdateStatus[]>('/api/v1/updates/hosts', { signal });
}

/** One host's update situation. Every authenticated role. */
export function fetchHostUpdateStatus(
  hostId: string,
  signal?: AbortSignal,
): Promise<HostUpdateStatus> {
  return apiRequest<HostUpdateStatus>(`/api/v1/hosts/${hostId}/updates`, { signal });
}

/**
 * Update history, newest first.
 *
 * `before` is a keyset cursor rather than an offset: a fleet's history grows
 * without bound, and `OFFSET 10000` makes the database walk and discard ten
 * thousand rows.
 */
export function fetchUpdateHistory(
  params: { hostId?: string; limit?: number; before?: string } = {},
  signal?: AbortSignal,
): Promise<UpdateHistory> {
  const query = new URLSearchParams();
  if (params.hostId !== undefined) query.set('host_id', params.hostId);
  if (params.limit !== undefined) query.set('limit', String(params.limit));
  if (params.before !== undefined) query.set('before', params.before);
  const suffix = query.size > 0 ? `?${query.toString()}` : '';
  return apiRequest<UpdateHistory>(`/api/v1/updates/history${suffix}`, { signal });
}

/**
 * One update's transitions, oldest first.
 *
 * "It reached staged and then failed" and "it never got past queued" are
 * different problems with the same final state, and this is where that
 * difference lives.
 */
export function fetchUpdateEvents(
  jobId: string,
  signal?: AbortSignal,
): Promise<UpdateEvent[]> {
  return apiRequest<UpdateEvent[]>(`/api/v1/updates/jobs/${jobId}/events`, { signal });
}

/** Set a host's update policy. Administrator-only, and audited. */
export function setUpdatePolicy(
  hostId: string,
  body: UpdatePolicyRequest,
): Promise<HostUpdateStatus> {
  return apiRequest<HostUpdateStatus>(`/api/v1/hosts/${hostId}/update-policy`, {
    method: 'PUT',
    body,
  });
}

/** Start an update on one host. Administrator-only, and audited. */
export function startUpdate(
  hostId: string,
  body: StartUpdateRequest = {},
): Promise<UpdateJob> {
  return apiRequest<UpdateJob>(`/api/v1/hosts/${hostId}/updates`, {
    method: 'POST',
    body,
  });
}

/**
 * Cancel an update. Administrator-only.
 *
 * Refused once the binary is being replaced — the controller cannot reach
 * into the host to stop it — and the refusal says so. The interface hides the
 * control at that point rather than offering a button that fails.
 */
export function cancelUpdate(jobId: string, reason?: string): Promise<UpdateJob> {
  return apiRequest<UpdateJob>(`/api/v1/updates/jobs/${jobId}/cancel`, {
    method: 'POST',
    body: { reason: reason ?? null },
  });
}
