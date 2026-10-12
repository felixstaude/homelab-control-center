/**
 * Audit trail calls.
 *
 * Read-only, and Administrator-only server-side. There is no write wrapper
 * because there is no write endpoint: the trail is written by the operations
 * it describes, and a route that appended to it would let a client fabricate
 * history.
 */

import { apiRequest } from '@/api/client';
import type { AuditEventList, AuditEventParams } from '@/api/types';

function eventQuery(params: AuditEventParams): string {
  const query = new URLSearchParams();
  if (params.action) query.set('action', params.action);
  if (params.targetUserId) query.set('target_user_id', params.targetUserId);
  if (params.actorUserId) query.set('actor_user_id', params.actorUserId);
  // The cursor is opaque and may contain characters that need escaping;
  // `URLSearchParams` handles that, which is why it is not interpolated.
  if (params.cursor) query.set('cursor', params.cursor);
  if (params.limit !== undefined) query.set('limit', String(params.limit));
  const rendered = query.toString();
  return rendered ? `?${rendered}` : '';
}

export function fetchAuditEvents(
  params: AuditEventParams = {},
  signal?: AbortSignal,
): Promise<AuditEventList> {
  return apiRequest<AuditEventList>(`/audit/events${eventQuery(params)}`, { signal });
}

/**
 * The action identifiers this server can record.
 *
 * Fetched rather than hard-coded so the filter cannot drift from the server's
 * vocabulary. It describes what this build *writes*, not what the table
 * *contains* — a stored event may carry an action absent from the list, which
 * is why the filter accepts any string and the renderer tolerates an unknown
 * one.
 */
export function fetchAuditActions(signal?: AbortSignal): Promise<string[]> {
  return apiRequest<string[]>('/audit/actions', { signal });
}
