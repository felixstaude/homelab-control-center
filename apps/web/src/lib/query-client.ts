import { QueryClient } from '@tanstack/react-query';

import { isRetryable } from '@/api/errors';

/**
 * Shared TanStack Query client.
 *
 * The defaults below are chosen for a monitoring UI specifically, and the
 * retry policy is chosen for *this* API's error model.
 */

/** Retries for a genuinely transient failure. Beyond this, tell the user. */
const MAX_RETRIES = 2;

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

        /**
         * Retry only what retrying can fix.
         *
         * A blanket retry count is wrong for this API. Retrying a 401, 403 or
         * 422 cannot change the answer, so it only delays the message the user
         * needs — and retrying a 429 would spend the throttle budget the
         * server just told us we were out of, which on the login path is an
         * automatic password-retry loop.
         *
         * `isRetryable` is true only for network, timeout and 5xx-unavailable,
         * so this bounds exactly the failures a retry addresses.
         */
        retry: (failureCount, error) => failureCount < MAX_RETRIES && isRetryable(error),
        retryDelay: (attempt) => Math.min(1000 * 2 ** attempt, 5000),
      },
      mutations: {
        // Management actions (stopping a container) must never be retried
        // automatically: a retried stop could act on a container the user has
        // since restarted. Retries are the caller's explicit decision.
        //
        // The same reasoning covers sign-in: a retried login spends another
        // attempt from the user's throttle budget without being asked.
        retry: false,
      },
    },
  });
}
