import { QueryClient } from '@tanstack/react-query';

/**
 * Shared TanStack Query client.
 *
 * Configured here but not yet used by any page: Phase 0 performs no data
 * fetching (engineering rule 8 — no fake API calls). The provider is installed
 * in `App` so that Phase 1 can add its first real query without restructuring
 * the application tree.
 *
 * The defaults below are chosen for a monitoring UI specifically.
 */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // Monitoring data ages quickly, but not within a few seconds. This
        // avoids a refetch storm when a user switches tabs repeatedly while
        // still keeping views current.
        staleTime: 10_000,
        gcTime: 5 * 60_000,

        // Refetch on focus is genuinely useful here: an operator returning to
        // the tab wants current state, not what was true ten minutes ago.
        refetchOnWindowFocus: true,

        // One retry. A homelab control plane is on the local network, so a
        // failure is usually a real outage worth surfacing promptly rather
        // than a transient blip worth hiding behind long retry chains.
        retry: 1,
        retryDelay: (attempt) => Math.min(1000 * 2 ** attempt, 5000),
      },
      mutations: {
        // Management actions (stopping a container) must never be retried
        // automatically: a retried stop could act on a container the user has
        // since restarted. Retries are the caller's explicit decision.
        retry: false,
      },
    },
  });
}
