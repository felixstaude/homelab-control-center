/**
 * What this controller can vouch for, for the install command.
 *
 * One read, Administrator-only on the server. The response carries the
 * expected SHA-256 of each architecture's archive, which is the whole reason
 * this endpoint exists rather than the interface linking to a releases page:
 * the digest has to travel inside the session the administrator is already
 * authenticated in, so that an attacker who controls the download origin
 * cannot make a substituted tarball match it.
 *
 * Nothing here caches the response in a way that outlives the session — it is
 * an ordinary query under the `session` root, cleared on sign-out like
 * everything else.
 */

import { apiRequest } from '@/api/client';
import type { AgentReleases } from '@/api/types';

export function fetchAgentReleases(signal?: AbortSignal): Promise<AgentReleases> {
  return apiRequest<AgentReleases>('/agent-releases', { signal });
}
