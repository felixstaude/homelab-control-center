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

    /* --- User administration (milestone 1.7) --- */

    /**
     * The root for every user-administration query.
     *
     * Invalidating this one key refreshes the list on whatever page, filter
     * and search the operator is looking at, plus any open detail view. That
     * is what mutations do, and it is why the parameters are *inside* the key
     * rather than being a separate cache: a create or a role change has to
     * invalidate the page the operator is on, and nothing knows which page
     * that is except the cache itself.
     */
    users: () => ['session', 'users'] as const,
    /**
     * One page of the user list.
     *
     * The parameter object is part of the key, so two different filters are
     * two cache entries rather than one entry that thrashes. Spelled as an
     * object rather than positional arguments because adding a filter must
     * not silently change the key of every existing entry.
     */
    userList: (params: UserListKeyParams) => ['session', 'users', 'list', params] as const,
    user: (userId: string) => ['session', 'users', 'detail', userId] as const,

    /* --- Own account (milestone 1.7) --- */

    /** The caller's own account details. */
    account: () => ['session', 'account'] as const,
    /** The caller's own live sessions. */
    accountSessions: () => ['session', 'account', 'sessions'] as const,

    /* --- Audit trail (milestone 1.7) --- */

    audit: () => ['session', 'audit'] as const,
    auditEvents: (params: AuditEventKeyParams) =>
      ['session', 'audit', 'events', params] as const,
    auditActions: () => ['session', 'audit', 'actions'] as const,

    /* --- Host registry (milestone 1.8) --- */

    /**
     * The root for every host query.
     *
     * Invalidating this one key refreshes the list on whatever page, filter
     * and search the operator is looking at, plus any open detail view. That
     * is what every host mutation does, and it matters more here than for
     * users: enabling a host changes its connectivity a second later as the
     * agent reconnects, so a mutation that invalidated only the detail view
     * would leave the list showing a state that is already false.
     */
    hosts: () => ['session', 'hosts'] as const,
    hostList: (params: HostListKeyParams) => ['session', 'hosts', 'list', params] as const,
    host: (hostId: string) => ['session', 'hosts', 'detail', hostId] as const,

    /* --- Monitoring configuration (phase 2) --- */

    /**
     * The root for every monitoring-configuration query.
     *
     * Changing a *global* default changes the effective configuration of every
     * host that has no override, so a mutation cannot know which host views to
     * refresh — it invalidates this root and lets the cache sort it out. The
     * alternative, invalidating only the host that was on screen, would leave
     * every other open view asserting a setting that is no longer true.
     */
    monitoring: () => ['session', 'monitoring'] as const,
    /** The module catalogue: what exists, and the interval floors. */
    moduleCatalogue: () => ['session', 'monitoring', 'modules'] as const,
    globalDefaults: () => ['session', 'monitoring', 'defaults'] as const,
    hostMonitoring: (hostId: string) =>
      ['session', 'monitoring', 'host', hostId] as const,

    /* --- Agent releases (Nightshift II) --- */

    /**
     * What the controller can vouch for, for the install command.
     *
     * Session-scoped because the endpoint is Administrator-only, and
     * long-lived because the answer changes only when the controller is
     * upgraded — so the enrolment dialog does not refetch it every time it
     * opens.
     */
    agentReleases: () => ['session', 'agent-releases'] as const,

    /* --- Update management (Operation Evergreen) --- */

    /**
     * The root for every update query.
     *
     * Invalidated by every update mutation, and the breadth matters here more
     * than elsewhere: starting an update on one host changes that host's
     * status, the fleet counts, the history and the open-job count — four
     * views a mutation cannot enumerate. Invalidating the root lets the cache
     * refresh whichever of them is on screen.
     */
    updates: () => ['session', 'updates'] as const,
    updateOverview: () => ['session', 'updates', 'overview'] as const,
    hostUpdateStatuses: () => ['session', 'updates', 'hosts'] as const,
    hostUpdateStatus: (hostId: string) =>
      ['session', 'updates', 'host', hostId] as const,
    /**
     * One page of history.
     *
     * The cursor is in the key, so paging forward is a new entry rather than
     * one that thrashes — and going back shows the previous page from cache
     * instead of refetching it.
     */
    updateHistory: (params: UpdateHistoryKeyParams) =>
      ['session', 'updates', 'history', params] as const,
    updateEvents: (jobId: string) => ['session', 'updates', 'events', jobId] as const,

    /* --- Metrics (phase 3) --- */

    /**
     * The root for every metric query.
     *
     * Separate from `monitoring` because the two change for different reasons
     * and on different clocks: configuration changes when somebody saves,
     * metrics change continuously on their own. Enabling a module does
     * invalidate both — there is new data to look for — but a chart refetching
     * must not drag the configuration with it.
     */
    metrics: () => ['session', 'metrics'] as const,
    latestMetrics: (hostId: string) => ['session', 'metrics', 'latest', hostId] as const,
    metricSeries: (hostId: string) => ['session', 'metrics', 'series', hostId] as const,
    metricsStatus: (hostId: string) => ['session', 'metrics', 'status', hostId] as const,
    /**
     * One range query.
     *
     * The window is in the key as a *label* ("last hour") rather than as the
     * resolved instants. Putting the instants in would make the key change on
     * every tick of the clock, so every refetch would be a cache miss and the
     * chart would flash empty between them.
     */
    metricRange: (params: MetricRangeKeyParams) =>
      ['session', 'metrics', 'range', params] as const,
  },
} as const;

/**
 * The parts of the user-list query that identify a cache entry.
 *
 * Declared here rather than reusing `UserListParams` from `api/types.ts`, and
 * the difference matters: a key must be **serialisable and stable**, and
 * TanStack Query hashes it structurally. Re-using the request type would
 * invite `undefined`-valued keys, which hash differently from an absent key in
 * some orderings — producing a second entry that nothing ever invalidates.
 * These are all required, with explicit "no filter" values supplied by the
 * caller.
 */
export type UserListKeyParams = {
  search: string;
  role: string;
  status: string;
  limit: number;
  offset: number;
};

/** The same reasoning, for the audit list. */
export type AuditEventKeyParams = {
  action: string;
  cursor: string;
  limit: number;
};

/**
 * The same reasoning, for the host list.
 *
 * `connectivity` is in the key even though the server applies that filter
 * after fetching the page: two different connectivity filters really are two
 * different results, and sharing one cache entry between them would make
 * switching the filter show the previous answer.
 */
export type HostListKeyParams = {
  search: string;
  lifecycle: string;
  enabled: string;
  connectivity: string;
  limit: number;
  offset: number;
};

/**
 * The parts of a metric range query that identify a cache entry.
 *
 * `window` is the named range the operator chose, not the instants it resolves
 * to. Resolved instants would change on every render, making each refetch a
 * fresh entry and leaving the previous one to be garbage collected — the chart
 * would unmount and flash empty on every poll instead of updating in place.
 */
export type UpdateHistoryKeyParams = {
  hostId: string;
  before: string;
  limit: number;
};

export type MetricRangeKeyParams = {
  hostId: string;
  seriesId: number;
  window: string;
};
