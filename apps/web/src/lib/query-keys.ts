/**
 * Query keys.
 *
 * Declared centrally so that invalidation is reliable: a key spelled slightly
 * differently at a call site produces a second cache entry that nothing ever
 * invalidates, which is how stale data outlives the thing that should have
 * cleared it.
 *
 * ## The public / authenticated split
 *
 * Keys are grouped under two roots:
 *
 * - `public` — data that does not depend on who is asking. Only
 *   `/setup/status` so far.
 * - `session` — everything that does.
 *
 * The split documents which data is session-scoped, and gives future code a
 * root to invalidate when only authenticated data is affected.
 *
 * Note what logout does *not* do: it calls `queryClient.clear()`, dropping
 * **every** entry rather than only the `session` root. That is deliberate.
 * Clearing a chosen subtree relies on every future query having been filed
 * under the right root, and the failure mode of getting that wrong is one
 * user's data surviving into another user's session. Re-fetching the public
 * setup status afterwards costs one cheap request; the alternative costs
 * correctness.
 */

export const queryKeys = {
  /** Data identical for every caller. */
  public: {
    all: ['public'] as const,
    setupStatus: () => ['public', 'setup-status'] as const,
  },

  /** Data scoped to the current session. Cleared on login and logout. */
  session: {
    all: ['session'] as const,
    /** The current identity, or `null` when unauthenticated. */
    currentUser: () => ['session', 'current-user'] as const,
    /** Authenticated server metadata. */
    apiInfo: () => ['session', 'api-info'] as const,
  },
} as const;
