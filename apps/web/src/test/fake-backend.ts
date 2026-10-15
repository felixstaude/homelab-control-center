/**
 * A fake control plane, for component and integration tests.
 *
 * Stubs `fetch` and answers the endpoints the client uses, with the **real**
 * response shapes and status codes — including the uniform error envelope, the
 * CSRF requirement on mutations, and the role gate on the administrative
 * routes.
 *
 * ## Why at the network boundary
 *
 * Mocking `useAuth` or the API modules would let a test pass while the
 * client's header assembly, cookie handling, query-string construction or
 * error mapping was broken. Stubbing `fetch` instead means everything between
 * the component and the socket is the code that ships. The response shapes are
 * kept honest by `src/api/openapi.test.ts`, which checks them against the
 * backend's committed schema — so this fake cannot quietly drift from the
 * server it imitates.
 *
 * ## Why it enforces rules rather than just returning fixtures
 *
 * The fake applies the role gate, rejects duplicate usernames, and refuses to
 * demote the last active administrator. That is more than a stub needs in
 * order to render a table, and it is the difference between a test that proves
 * the interface handles a 409 and one that proves it can display a happy path.
 * The rules are the *server's*, restated — so each one is a place this file
 * could drift, which is why the backend's own integration suite asserts the
 * same behaviours against real PostgreSQL. Neither suite is sufficient alone:
 * these cover what the browser does with the answer, those cover whether the
 * answer is right.
 */

import { expect, vi } from 'vitest';

import { CSRF_COOKIE_NAME } from '@/api/client';
import type {
  AuditEvent,
  ConfigSync,
  CurrentUser,
  FleetUpdateOverview,
  GlobalDefault,
  Host,
  HostUpdateStatus,
  LatestValue,
  ManagedUser,
  MetricRangePoint,
  MetricSeries,
  ModuleDescriptor,
  OwnSession,
  StoredSetting,
  Rollout,
  RolloutMember,
  UpdateJob,
  UpdatePlan,
  UserRole,
} from '@/api/types';

export const TEST_CSRF_TOKEN = 'test-csrf-token-value';

/** A user as `/auth/me` would return one. */
export function makeUser(overrides: Partial<CurrentUser> = {}): CurrentUser {
  return {
    user_id: '0f3c1a2b-0000-4000-8000-000000000001',
    username: 'FelixAdmin',
    display_name: null,
    role: 'admin',
    is_active: true,
    session_expires_at: '2030-01-01T00:00:00+00:00',
    csrf_token: TEST_CSRF_TOKEN,
    ...overrides,
  };
}

/** A managed account as `GET /users` would return one. */
export function makeManagedUser(overrides: Partial<ManagedUser> = {}): ManagedUser {
  return {
    user_id: '0f3c1a2b-0000-4000-8000-000000000002',
    username: 'someone',
    display_name: null,
    role: 'viewer',
    is_active: true,
    created_at: '2026-01-02T09:00:00+00:00',
    last_login_at: null,
    active_session_count: 0,
    ...overrides,
  };
}

/** One of the caller's own sessions. */
export function makeOwnSession(overrides: Partial<OwnSession> = {}): OwnSession {
  return {
    session_id: '0f3c1a2b-0000-4000-8000-00000000f001',
    is_current: false,
    created_at: '2026-01-02T09:00:00+00:00',
    last_seen_at: '2026-01-02T09:30:00+00:00',
    expires_at: '2026-01-02T21:00:00+00:00',
    created_ip: '192.0.2.10',
    user_agent: 'Mozilla/5.0 (X11; Linux x86_64)',
    ...overrides,
  };
}

/**
 * The overview an unpublished project reports.
 *
 * Not "nothing available" dressed up as healthy: `metadata_available` is
 * false and the signature is `absent`, which is what the server actually
 * says when no release has been published.
 */
export function emptyUpdateOverview(): FleetUpdateOverview {
  return {
    total_hosts: 0,
    up_to_date: 0,
    update_available: 0,
    blocked: 0,
    needs_attention: 0,
    version_distribution: [],
    hosts_with_automatic_updates: 0,
    open_jobs: 0,
    failed_recently: 0,
    controller_version: '0.1.0',
    controller_schema_revision: '0010_update_management',
    metadata_available: false,
    metadata_detail:
      'no release metadata is configured, so this controller does not know what versions exist',
    metadata_signature: 'absent',
    metadata_signature_detail:
      'no signature accompanies the release metadata, so its authenticity cannot be established',
    channels: ['stable', 'beta', 'alpha'],
  };
}

/** An update plan. Defaults to a host that is up to date. */
export function makeUpdatePlan(overrides: Partial<UpdatePlan> = {}): UpdatePlan {
  return {
    outcome: 'up_to_date',
    installed_version: '0.1.0',
    target_version: '0.1.0',
    target_channel: 'stable',
    step: 'none',
    blockers: [],
    detail: null,
    download_url: null,
    expected_sha256: null,
    ...overrides,
  };
}

/** One update attempt. */
export function makeUpdateJob(overrides: Partial<UpdateJob> = {}): UpdateJob {
  return {
    job_id: 'job-0001',
    host_id: 'host-connected',
    state: 'queued',
    trigger: 'manual',
    from_version: '0.1.0',
    to_version: '0.1.1',
    resulting_version: null,
    error: null,
    attempt: 1,
    created_at: '2026-10-14T12:00:00+00:00',
    started_at: null,
    finished_at: null,
    requested_by_user_id: 'u-admin',
    ...overrides,
  };
}

/**
 * One host's update situation.
 *
 * Defaults to the safe state: manual policy, no window, nothing in progress.
 * That is what a host with no policy row reports, and making it the default
 * means a test has to opt into automation rather than inherit it.
 */
export function makeHostUpdateStatus(
  overrides: Partial<HostUpdateStatus> = {},
): HostUpdateStatus {
  return {
    host_id: 'host-connected',
    host_name: 'homeserver',
    installed_version: '0.1.0',
    plan: makeUpdatePlan(),
    policy: 'manual',
    automatic: false,
    channel: 'stable',
    pinned_version: null,
    maintenance_window: null,
    open_job: null,
    last_job: null,
    ...overrides,
  };
}

/** An audit event. */
export function makeAuditEvent(overrides: Partial<AuditEvent> = {}): AuditEvent {
  return {
    event_id: '0f3c1a2b-0000-4000-8000-00000000e001',
    occurred_at: '2026-01-02T09:00:00+00:00',
    action: 'user.created',
    outcome: 'success',
    actor: {
      user_id: '0f3c1a2b-0000-4000-8000-000000000001',
      username: 'FelixAdmin',
      display_name: null,
    },
    target: {
      user_id: '0f3c1a2b-0000-4000-8000-000000000002',
      username: 'someone',
      display_name: null,
    },
    // Null by default: the default event is `user.created`, which has an
    // account target and no host. A host-targeted event overrides it.
    target_host_id: null,
    request_id: 'test-request',
    details: { role: 'viewer' },
    ...overrides,
  };
}

/**
 * A host as the registry would return one (milestone 1.8).
 *
 * Defaults to a host that has been registered and nothing more: not enrolled,
 * offline, every agent-reported field null. That is the state a host is
 * actually in for the minute between somebody creating it and somebody
 * enrolling it, and a fixture that defaulted to "connected with metrics"
 * would make the empty-state rendering the untested path.
 */
export function makeHost(overrides: Partial<Host> = {}): Host {
  return {
    host_id: '7a1f0c10-0000-4000-8000-000000000001',
    name: 'homeserver',
    description: null,
    environment: null,
    lifecycle: 'registered',
    is_enabled: true,
    connectivity: 'offline',
    last_heartbeat_at: null,
    last_connected_at: null,
    last_disconnected_at: null,
    agent_version: null,
    platform: null,
    protocol_version: null,
    agent_started_at: null,
    has_pending_enrollment: false,
    credential_issued_at: null,
    created_at: '2026-10-01T09:00:00+00:00',
    ...overrides,
  };
}

/** One recorded request, for asserting on headers and bodies. */
/**
 * The module catalogue, matching `contracts/monitoring-modules.json`.
 *
 * Five modules, because that is what the registry declares — not two. The
 * floors are the real ones: a test that used 1 s everywhere would not notice
 * an interface that offered an interval the server refuses.
 */
/**
 * The module catalogue.
 *
 * Transcribed from `contracts/monitoring-modules.json`, which is generated
 * from the server's registry and drift-checked by `make check-contract`. That
 * file is the source of truth; this is a copy because Vite will not import
 * from outside the project root and widening its allowed paths for a test
 * fixture is not a trade worth making.
 *
 * Kept faithful on purpose. Five modules, because that is what the registry
 * declares — not the two that are most interesting — and the real interval
 * floors, which differ per module: a fixture using 1 s everywhere would not
 * notice an interface offering an interval the server refuses. The metric ids
 * are the registry's too (`used`, not `used_bytes`); the interface displays
 * `metric_name` and never branches on the id, but a fixture that disagrees
 * with reality misleads whoever reads it next.
 */
export const FAKE_MODULES: readonly ModuleDescriptor[] = [
  {
    id: 'cpu',
    name: 'CPU utilisation',
    description: 'Processor utilisation, derived from /proc/stat deltas.',
    platform: 'linux',
    default_interval_seconds: 10,
    min_interval_seconds: 1,
    schedule_kinds: ['interval'],
    requires_privileges: false,
    allows_duplicate_runs: true,
    data_schema_version: 1,
    metrics: [
      { id: 'utilisation', name: 'CPU utilisation', unit: 'percent', kind: 'gauge', label_keys: [] },
      { id: 'core_utilisation', name: 'Per-core utilisation', unit: 'percent', kind: 'gauge', label_keys: ['core'] },
      { id: 'logical_cpus', name: 'Logical CPUs', unit: 'count', kind: 'gauge', label_keys: [] },
    ],
  },
  {
    id: 'memory',
    name: 'Memory',
    description: 'Physical memory in use, from /proc/meminfo.',
    platform: 'linux',
    default_interval_seconds: 10,
    min_interval_seconds: 1,
    schedule_kinds: ['interval'],
    requires_privileges: false,
    allows_duplicate_runs: true,
    data_schema_version: 1,
    metrics: [
      { id: 'utilisation', name: 'Memory used', unit: 'percent', kind: 'gauge', label_keys: [] },
      { id: 'total', name: 'Total memory', unit: 'bytes', kind: 'gauge', label_keys: [] },
      { id: 'available', name: 'Available memory', unit: 'bytes', kind: 'gauge', label_keys: [] },
      { id: 'used', name: 'Used memory', unit: 'bytes', kind: 'gauge', label_keys: [] },
    ],
  },
  {
    id: 'swap',
    name: 'Swap',
    description: 'Swap in use. Reports nothing on a host with no swap.',
    platform: 'linux',
    default_interval_seconds: 10,
    min_interval_seconds: 1,
    schedule_kinds: ['interval'],
    requires_privileges: false,
    allows_duplicate_runs: true,
    data_schema_version: 1,
    metrics: [
      { id: 'utilisation', name: 'Swap used', unit: 'percent', kind: 'gauge', label_keys: [] },
      { id: 'total', name: 'Total swap', unit: 'bytes', kind: 'gauge', label_keys: [] },
      { id: 'used', name: 'Used swap', unit: 'bytes', kind: 'gauge', label_keys: [] },
    ],
  },
  {
    id: 'load',
    name: 'Load average',
    description: 'One, five and fifteen minute load averages.',
    platform: 'linux',
    default_interval_seconds: 30,
    min_interval_seconds: 5,
    schedule_kinds: ['interval'],
    requires_privileges: false,
    allows_duplicate_runs: true,
    data_schema_version: 1,
    metrics: [
      { id: 'average', name: 'Load average', unit: 'load', kind: 'gauge', label_keys: ['window'] },
    ],
  },
  {
    id: 'uptime',
    name: 'System uptime',
    description: 'How long the host has been running.',
    platform: 'linux',
    default_interval_seconds: 300,
    min_interval_seconds: 60,
    schedule_kinds: ['interval'],
    requires_privileges: false,
    allows_duplicate_runs: true,
    data_schema_version: 1,
    metrics: [
      { id: 'seconds', name: 'System uptime', unit: 'seconds', kind: 'gauge', label_keys: [] },
    ],
  },
];

/** A series descriptor, with the registry names the API decorates it with. */
export function makeSeries(overrides: Partial<MetricSeries> = {}): MetricSeries {
  return {
    series_id: 1,
    module: 'cpu',
    metric: 'utilisation',
    labels: {},
    unit: 'percent',
    kind: 'gauge',
    module_name: 'CPU',
    metric_name: 'Utilisation',
    last_sample_at: new Date(Date.now() - 5000).toISOString(),
    ...overrides,
  };
}

/** A current value. Recent by default, so it survives the freshness window. */
export function makeLatest(overrides: Partial<LatestValue> = {}): LatestValue {
  return {
    series: makeSeries(),
    value: 12.5,
    recorded_at: new Date(Date.now() - 5000).toISOString(),
    ...overrides,
  };
}

/** Nothing configured anywhere: the resting state of a fresh installation. */
function unsetSetting(): StoredSetting {
  return { enabled: null, schedule: null };
}

/** The sync state for a host nothing has been configured for. */
function noSync(): ConfigSync {
  return {
    status: 'none',
    desired_version: null,
    desired_checksum: null,
    generated_at: null,
    reported_version: null,
    reported_checksum: null,
    acknowledged_at: null,
    rejected_at: null,
    rejection_problems: [],
  };
}

export type RecordedRequest = {
  method: string;
  path: string;
  /** Query string without the leading `?`, for asserting on filters. */
  query: string;
  headers: Record<string, string>;
  body: unknown;
};

/** How the fake should behave. Every field is mutable mid-test. */
/**
 * A release the fake controller can vouch for.
 *
 * Deliberately not the full `AgentReleases` shape: a test setting this up
 * cares about the version and the platforms, and the protocol fields are the
 * same in every case. The handler fills in the rest, so a test cannot
 * accidentally assert against a protocol version it invented.
 */
export type AgentReleasesFixture = {
  recommended_version: string;
  platforms: {
    platform: string;
    archive: string;
    url: string;
    sha256: string;
    size_bytes: number;
    tier: 'supported' | 'best-effort';
  }[];
};

export type FakeBackendState = {
  /** `/setup/status`. */
  initialized: boolean;
  /** The session `/auth/me` reports. `null` means unauthenticated. */
  session: CurrentUser | null;
  /** When set, every request fails this way before reaching a route. */
  transport: 'ok' | 'network' | 'timeout';
  /** When set, login fails with this instead of succeeding. */
  loginFailure: { status: number; code: string; message: string; retryAfter?: number } | null;
  /** When true, logout answers 500 — server-side revocation did not happen. */
  logoutFails: boolean;
  /** The user login returns on success. Defaults to {@link makeUser}. */
  loginResult: CurrentUser;

  /* --- Milestone 1.7 --- */

  /** The account table the administrative endpoints read and write. */
  users: ManagedUser[];
  /** The caller's own sessions. */
  ownSessions: OwnSession[];
  /** The audit trail, newest first. */
  auditEvents: AuditEvent[];
  /**
   * Forces the next administrative mutation to fail this way.
   *
   * For exercising a specific refusal — a rate limit, an unexpected 500 —
   * without having to arrange the state that would produce it. Cleared after
   * it fires once, so a test cannot accidentally make every later call fail.
   */
  nextFailure: { status: number; code: string; message: string; retryAfter?: number } | null;
  /**
   * Persistent failures, keyed by the path suffix they apply to.
   *
   * Distinct from {@link nextFailure}, which fires once, and from
   * {@link transport}, which fails *everything* — including `/auth/me`, so a
   * test that only wanted the user list to fail would instead land on the
   * backend-unavailable screen and never render the panel under test. This
   * fails one route and leaves authentication working.
   */
  routeFailures: Record<string, { status: number; code: string; message: string }>;
  /** The current password `POST /account/password` will accept. */
  currentPassword: string;

  /* --- Milestone 1.8 --- */

  /** The host registry the host endpoints read and write. */
  hosts: Host[];
  /**
   * The credential the next issuance returns.
   *
   * Fixed rather than random so a test can assert the value reached the
   * screen — and distinctive enough that a test asserting its *absence*
   * elsewhere cannot pass by coincidence.
   */
  enrollmentCredential: string;

  /* --- Agent releases (Nightshift II) --- */

  /**
   * What `GET /agent-releases` answers.
   *
   * `null` is the **default**, and it is the default because it is what every
   * build of this project actually reports: no release has been published, so
   * the interface shows the build-from-source path. A fake whose default was
   * "a release is available" would make the honest case the one nobody tested.
   */
  agentReleases: AgentReleasesFixture | null;

  /* --- Update management (Operation Evergreen) --- */

  /**
   * What `GET /updates/overview` answers.
   *
   * `null` is the default, and it produces the honest state of an
   * unpublished project: no release metadata, an unsigned manifest, and
   * nothing available. A fake whose default was "an update is waiting" would
   * make the case every real deployment is in the one nobody tested.
   */
  updateOverview: FleetUpdateOverview | null;

  /** What `GET /updates/hosts` answers. */
  hostUpdateStatuses: HostUpdateStatus[];

  /** What `GET /updates/history` answers. */
  updateHistory: UpdateJob[];

  /**
   * What `GET /updates/rollouts` answers.
   *
   * Empty by default, which is the state of every installation: a rollout is
   * something somebody creates, and a fake whose default was "a rollout is
   * running" would make the empty state the one nobody tested.
   */
  rollouts: Rollout[];

  /* --- Phase 2: monitoring configuration --- */

  /**
   * Global defaults, keyed by module id.
   *
   * Absent means "nothing has been said", which is a third state distinct
   * from `{enabled: false}` — and the one a fresh installation is in.
   */
  globalDefaults: Record<string, StoredSetting>;
  /** Host overrides, keyed by host id then module id. */
  hostOverrides: Record<string, Record<string, StoredSetting>>;
  /** The sync state each host's monitoring response reports. */
  hostSync: Record<string, ConfigSync>;
  /** Configuration problems the controller reports for a host. */
  hostConfigProblems: Record<string, string[]>;

  /* --- Phase 3: metrics --- */

  /**
   * Current values per host.
   *
   * Empty by default, which is the honest resting state: a host that has
   * collected nothing has no current values, and the interface must render
   * that as "no data" rather than as zeroes.
   */
  latestMetrics: Record<string, LatestValue[]>;
  /** Every series a host has produced, including ones that stopped. */
  metricSeries: Record<string, MetricSeries[]>;
  /** Range points per series id. */
  metricPoints: Record<number, MetricRangePoint[]>;
  /** What a range query reports as the tier that answered. */
  metricResolution: string;
  /** Total raw samples stored per host, for the status endpoint. */
  metricSampleCount: Record<string, number>;
};

export type FakeBackend = {
  readonly state: FakeBackendState;
  readonly requests: RecordedRequest[];
  /** Requests for one path, for counting duplicates. */
  requestsFor: (path: string) => RecordedRequest[];
  /** Restore the real `fetch` and clear cookies. */
  restore: () => void;
};

function envelope(
  status: number,
  code: string,
  message: string,
  extra: { retryAfter?: number; details?: Record<string, unknown> } = {},
): Response {
  return new Response(
    JSON.stringify({
      error: {
        code,
        message,
        request_id: 'test-request',
        ...(extra.details ? { details: extra.details } : {}),
      },
    }),
    {
      status,
      headers: {
        'Content-Type': 'application/json',
        ...(extra.retryAfter === undefined
          ? {}
          : { 'Retry-After': String(extra.retryAfter) }),
      },
    },
  );
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function clearCookies(): void {
  for (const part of document.cookie.split(';')) {
    const name = part.split('=')[0]?.trim();
    if (name) document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
  }
}

/** The server's username folding, as far as these tests need it. */
function fold(username: string): string {
  return username.normalize('NFKC').trim().toLowerCase();
}

/**
 * The server's blank-to-null mapping for a display name.
 *
 * Spelled out rather than written as `value?.trim() || null`, which relies on
 * the empty string being falsy — correct, but the kind of correctness that
 * reads as an oversight and that the lint rightly flags next to a `??`.
 */
function blankToNull(value: string | null | undefined): string | null {
  const trimmed = value?.trim() ?? '';
  return trimmed === '' ? null : trimmed;
}

/**
 * A path parameter from a matched route.
 *
 * The regexes below all have exactly one capture group, so the value is always
 * present — but `RegExpExecArray` is typed as possibly-sparse, and asserting
 * past that would hide a genuinely malformed path behind a crash.
 */
function captured(match: RegExpExecArray): string {
  return decodeURIComponent(match[1] ?? '');
}

/**
 * Install the fake. Call {@link FakeBackend.restore} in an `afterEach`.
 */
export function installFakeBackend(initial: Partial<FakeBackendState> = {}): FakeBackend {
  const state: FakeBackendState = {
    initialized: true,
    session: null,
    transport: 'ok',
    loginFailure: null,
    logoutFails: false,
    loginResult: makeUser(),
    users: [],
    ownSessions: [],
    auditEvents: [],
    nextFailure: null,
    routeFailures: {},
    currentPassword: 'correct horse battery staple',
    hosts: [],
    globalDefaults: {},
    hostOverrides: {},
    hostSync: {},
    hostConfigProblems: {},
    latestMetrics: {},
    metricSeries: {},
    metricPoints: {},
    metricResolution: 'raw',
    metricSampleCount: {},
    enrollmentCredential: 'TEST-ENROLMENT-CREDENTIAL-0000000000000000',
    agentReleases: null,
    updateOverview: null,
    hostUpdateStatuses: [],
    updateHistory: [],
    rollouts: [],
    ...initial,
  };

  const requests: RecordedRequest[] = [];
  clearCookies();

  // A browser holding a live session also holds the readable CSRF cookie the
  // server set at login. Seeding it here keeps the fake faithful: without it,
  // every test that starts signed-in would have to remember to set the cookie
  // before a mutation, and forgetting would look like a client bug.
  if (state.session) {
    document.cookie = `${CSRF_COOKIE_NAME}=${state.session.csrf_token}; path=/`;
  }

  /** Whether the caller is authenticated at all. */
  const requireSession = (): Response | null =>
    state.session
      ? null
      : envelope(401, 'not_authenticated', 'Authentication required.');

  /** Whether the caller may reach an administrative route. */
  const requireAdmin = (): Response | null => {
    const unauthenticated = requireSession();
    if (unauthenticated) return unauthenticated;
    if (state.session?.role !== 'admin') {
      return envelope(
        403,
        'insufficient_permissions',
        'You do not have permission to perform this action.',
      );
    }
    return null;
  };

  /** The CSRF header the backend requires on every mutation. */
  const requireCsrf = (headers: Record<string, string>): Response | null =>
    headers['x-csrf-token']
      ? null
      : envelope(403, 'csrf_validation_failed', 'CSRF validation failed.');

  /** Consume a forced failure, if one is armed. */
  const forcedFailure = (): Response | null => {
    const failure = state.nextFailure;
    if (!failure) return null;
    state.nextFailure = null;
    return envelope(failure.status, failure.code, failure.message, {
      ...(failure.retryAfter === undefined ? {} : { retryAfter: failure.retryAfter }),
    });
  };

  /** Active administrators other than `exceptId`. The last-admin guard. */
  const otherActiveAdmins = (exceptId: string): number =>
    state.users.filter(
      (user) => user.user_id !== exceptId && user.role === 'admin' && user.is_active,
    ).length;

  const lastAdministrator = (operation: string): Response =>
    envelope(
      409,
      'last_administrator',
      `Cannot ${operation}: this is the only active administrator, and an ` +
        'installation with no active administrator cannot be recovered through ' +
        'the interface. Grant the Administrator role to another active account first.',
    );

  /** Every global default, including modules nothing has been said about. */
  const globalDefaultList = (): GlobalDefault[] =>
    FAKE_MODULES.map((descriptor) => ({
      module_id: descriptor.id,
      setting: state.globalDefaults[descriptor.id] ?? unsetSetting(),
    }));

  /**
   * Apply one three-valued setting change, or refuse it.
   *
   * The whole point is the distinction between an absent key and a null one:
   * `{enabled: false}` must not clear a stored schedule, and `{schedule: null}`
   * must not change whether the module is enabled. A fake that merged the
   * whole body would hide a client that sent too much.
   *
   * The interval floor is enforced here because the server enforces it, and a
   * permissive fake would let an interface ship that offered a value the API
   * refuses.
   */
  const applySetting = (
    current: StoredSetting,
    requestBody: unknown,
    descriptor: ModuleDescriptor,
    store: (next: StoredSetting) => void,
  ): Response | null => {
    const fields = (requestBody ?? {}) as Record<string, unknown>;
    const next: StoredSetting = { enabled: current.enabled, schedule: current.schedule };

    if ('enabled' in fields) {
      const value = fields.enabled;
      if (value !== null && typeof value !== 'boolean') {
        return envelope(422, 'validation_failed', '`enabled` must be true, false or null.');
      }
      next.enabled = value;
    }

    if ('schedule' in fields) {
      const value = fields.schedule;
      if (value === null) {
        next.schedule = null;
      } else if (typeof value !== 'object') {
        return envelope(422, 'validation_failed', '`schedule` must be an object or null.');
      } else {
        const schedule = value as Record<string, unknown>;
        const kind = schedule.kind;
        if (typeof kind !== 'string' || !descriptor.schedule_kinds.includes(kind)) {
          return envelope(
            422,
            'validation_failed',
            `${descriptor.name} does not support that schedule kind.`,
            { details: { schedule_kinds: descriptor.schedule_kinds } },
          );
        }
        const seconds = schedule.interval_seconds;
        if (kind === 'interval') {
          if (typeof seconds !== 'number' || !Number.isInteger(seconds)) {
            return envelope(
              422,
              'validation_failed',
              '`interval_seconds` must be a whole number of seconds.',
            );
          }
          // Refused, never clamped: silently saving a different value than the
          // one somebody typed is the failure §7 forbids.
          if (seconds < descriptor.min_interval_seconds) {
            return envelope(
              422,
              'validation_failed',
              `${descriptor.name} cannot run more often than every ` +
                `${String(descriptor.min_interval_seconds)} s.`,
              { details: { min_interval_seconds: descriptor.min_interval_seconds } },
            );
          }
        }
        next.schedule = schedule as StoredSetting['schedule'];
      }
    }

    store(next);
    return null;
  };

  /** One module's resolved configuration on one host, with its provenance. */
  const effectiveModule = (hostId: string, descriptor: ModuleDescriptor) => {
    const global = state.globalDefaults[descriptor.id] ?? unsetSetting();
    const override = state.hostOverrides[hostId]?.[descriptor.id] ?? null;

    // Host beats global beats nothing, and "nothing" resolves to off — which
    // is default-deny expressed as resolution rather than as a special case.
    const enabledSource =
      override?.enabled != null ? 'host' : global.enabled != null ? 'global' : 'default';
    const enabledValue =
      enabledSource === 'host'
        ? override?.enabled === true
        : enabledSource === 'global'
          ? global.enabled === true
          : false;

    const scheduleSource =
      override?.schedule != null ? 'host' : global.schedule != null ? 'global' : 'default';
    const schedule =
      override?.schedule ??
      global.schedule ?? {
        kind: 'interval',
        interval_seconds: descriptor.default_interval_seconds,
        jitter_seconds: 0,
      };

    return {
      module_id: descriptor.id,
      name: descriptor.name,
      enabled: { value: enabledValue, source: enabledSource },
      schedule: { value: schedule, source: scheduleSource, next_runs: null },
      global_default: global,
      host_override: override,
    };
  };

  const handler = async (
    input: RequestInfo | URL,
    init?: RequestInit,
  ): Promise<Response> => {
    const raw = typeof input === 'string' ? input : input instanceof URL ? input.href : '';
    const [path = '', query = ''] = raw.split('?');
    const method = (init?.method ?? 'GET').toUpperCase();
    const headers: Record<string, string> = {};
    new Headers(init?.headers).forEach((value, key) => {
      headers[key.toLowerCase()] = value;
    });

    let body: unknown = undefined;
    if (typeof init?.body === 'string') {
      try {
        body = JSON.parse(init.body);
      } catch {
        body = init.body;
      }
    }
    requests.push({ method, path, query, headers, body });

    if (state.transport === 'network') {
      throw new TypeError('Failed to fetch');
    }
    if (state.transport === 'timeout') {
      // Never settles; the client's own deadline aborts it.
      return new Promise<Response>(() => {
        /* intentionally pending */
      });
    }

    for (const [suffix, failure] of Object.entries(state.routeFailures)) {
      if (path.endsWith(suffix)) {
        return envelope(failure.status, failure.code, failure.message);
      }
    }

    /* --- Authentication and setup (milestone 1.6) --- */

    if (path.endsWith('/setup/status') && method === 'GET') {
      return json({ initialized: state.initialized });
    }

    if (path.endsWith('/auth/me') && method === 'GET') {
      const refused = requireSession();
      if (refused) return refused;
      return json(state.session);
    }

    if (path.endsWith('/auth/login') && method === 'POST') {
      if (state.loginFailure) {
        const failure = state.loginFailure;
        return envelope(failure.status, failure.code, failure.message, {
          ...(failure.retryAfter === undefined ? {} : { retryAfter: failure.retryAfter }),
        });
      }
      state.session = state.loginResult;
      // The real backend sets this cookie, readable on purpose so the client
      // can echo it in a header. Setting it here exercises that whole path.
      document.cookie = `${CSRF_COOKIE_NAME}=${state.loginResult.csrf_token}; path=/`;
      return json(state.loginResult);
    }

    if (path.endsWith('/auth/logout') && method === 'POST') {
      const refusedCsrf = requireCsrf(headers);
      if (refusedCsrf) return refusedCsrf;
      if (state.logoutFails) {
        return envelope(500, 'internal_error', 'An internal error occurred.');
      }
      state.session = null;
      clearCookies();
      return json({ status: 'logged_out' });
    }

    if (path.endsWith('/updates/overview') && method === 'GET') {
      const refused = requireSession();
      if (refused) return refused;
      return json(state.updateOverview ?? emptyUpdateOverview());
    }

    if (path.endsWith('/updates/hosts') && method === 'GET') {
      const refused = requireSession();
      if (refused) return refused;
      return json(state.hostUpdateStatuses);
    }

    if (path.includes('/updates/history') && method === 'GET') {
      const refused = requireSession();
      if (refused) return refused;
      return json({ jobs: state.updateHistory, next_before: null });
    }

    if (path.endsWith('/updates/rollouts') && method === 'GET') {
      const refused = requireSession();
      if (refused) return refused;
      return json(state.rollouts);
    }

    // Pause, resume and cancel. Administrator only, as the server enforces —
    // asserted here so a client that offered the control to a viewer would
    // fail rather than silently succeed against a permissive fake.
    const rolloutControl = /\/updates\/rollouts\/([^/]+)\/(pause|resume|cancel)$/.exec(path);
    if (rolloutControl && method === 'POST') {
      const refused = requireSession();
      if (refused) return refused;
      if (state.session && state.session.role !== 'admin') {
        return envelope(403, 'insufficient_permissions', 'Administrator role required.');
      }
      const [, rolloutId, action] = rolloutControl;
      const found = state.rollouts.find((entry) => entry.rollout_id === rolloutId);
      if (!found) {
        return envelope(404, 'not_found', 'No such rollout.');
      }
      const payload = (body ?? {}) as { override_failures?: boolean };
      found.state =
        action === 'pause' ? 'paused' : action === 'cancel' ? 'cancelled' : 'running';
      if (action === 'resume' && payload.override_failures === true) {
        found.failures_overridden = true;
      }
      return json(found);
    }

    if (path.endsWith('/update-policy') && method === 'PUT') {
      const refused = requireSession();
      if (refused) return refused;
      if (state.session && state.session.role !== 'admin') {
        return envelope(403, 'insufficient_permissions', 'Administrator role required.');
      }
      // `body` is already parsed above, which is also what the recorded
      // request carries — re-parsing `init.body` here would be a second
      // source of truth for the same bytes.
      const policyBody = body as {
        policy: HostUpdateStatus['policy'];
        channel: string;
        pinned_version: string | null;
        maintenance_window: HostUpdateStatus['maintenance_window'] | null;
      };
      const hostId = path.split('/hosts/')[1]?.split('/')[0] ?? '';
      const existing =
        state.hostUpdateStatuses.find((entry) => entry.host_id === hostId) ??
        state.hostUpdateStatuses[0];
      if (!existing) return envelope(404, 'not_found', 'No such host.');
      const updated: HostUpdateStatus = {
        ...existing,
        policy: policyBody.policy,
        automatic: policyBody.policy === 'patch' || policyBody.policy === 'minor',
        channel: policyBody.channel,
        pinned_version: policyBody.pinned_version,
        maintenance_window: policyBody.maintenance_window
          ? {
              ...policyBody.maintenance_window,
              description: `${policyBody.maintenance_window.start}-${policyBody.maintenance_window.end} ${policyBody.maintenance_window.timezone}`,
            }
          : null,
      };
      state.hostUpdateStatuses = state.hostUpdateStatuses.map((entry) =>
        entry.host_id === updated.host_id ? updated : entry,
      );
      return json(updated);
    }

    if (path.includes('/updates') && path.includes('/hosts/') && method === 'POST') {
      const refused = requireSession();
      if (refused) return refused;
      if (state.session && state.session.role !== 'admin') {
        return envelope(403, 'insufficient_permissions', 'Administrator role required.');
      }
      const hostId = path.split('/hosts/')[1]?.split('/')[0] ?? '';
      const host = state.hostUpdateStatuses.find((entry) => entry.host_id === hostId);
      if (!host) return envelope(404, 'not_found', 'No such host.');
      const job = makeUpdateJob({
        host_id: hostId,
        to_version: host.plan.target_version ?? '0.1.1',
        from_version: host.installed_version,
        state: 'queued',
      });
      state.hostUpdateStatuses = state.hostUpdateStatuses.map((entry) =>
        entry.host_id === hostId ? { ...entry, open_job: job } : entry,
      );
      state.updateHistory = [job, ...state.updateHistory];
      return json(job, 201);
    }

    if (path.includes('/updates/jobs/') && path.endsWith('/cancel') && method === 'POST') {
      const refused = requireSession();
      if (refused) return refused;
      if (state.session && state.session.role !== 'admin') {
        return envelope(403, 'insufficient_permissions', 'Administrator role required.');
      }
      state.hostUpdateStatuses = state.hostUpdateStatuses.map((entry) =>
        entry.open_job ? { ...entry, open_job: null } : entry,
      );
      return json(makeUpdateJob({ state: 'cancelled' }));
    }

    if (path.endsWith('/agent-releases') && method === 'GET') {
      const refused = requireSession();
      if (refused) return refused;
      // Administrator-only on the server. The fake enforces it so a test
      // cannot accidentally prove the dialog works for an Operator.
      if (state.session && state.session.role !== 'admin') {
        return envelope(403, 'forbidden', 'Administrator role required.');
      }
      const releases = state.agentReleases;
      if (!releases) {
        return json({
          available: false,
          unavailable_reason:
            'No agent release has been published for this version of the control plane, ' +
            'so there is no verified download to offer. Build the agent from source.',
          recommended_version: null,
          minimum_supported_version: null,
          platforms: [],
          protocol_version: 1,
          supported_protocol_versions: [1],
          server_version: '0.1.0',
        });
      }
      return json({
        available: true,
        unavailable_reason: null,
        recommended_version: releases.recommended_version,
        minimum_supported_version: releases.recommended_version,
        platforms: releases.platforms,
        protocol_version: 1,
        supported_protocol_versions: [1],
        server_version: '0.1.0',
      });
    }

    if (path.endsWith('/info') && method === 'GET') {
      const refused = requireSession();
      if (refused) return refused;
      return json({ api_version: 'v1', server_version: '0.1.0', environment: 'test' });
    }

    /* --- Own account (milestone 1.7) --- */

    if (path.endsWith('/account') && method === 'GET') {
      const refused = requireSession();
      if (refused) return refused;
      // `requireSession` returned null, so there is a session; read it into a
      // local rather than asserting past the nullable field.
      const session = state.session;
      if (!session) return envelope(401, 'not_authenticated', 'Authentication required.');
      return json({
        user_id: session.user_id,
        username: session.username,
        display_name: session.display_name,
        role: session.role,
        is_active: session.is_active,
        created_at: '2026-01-01T08:00:00+00:00',
        last_login_at: '2026-01-02T09:00:00+00:00',
      });
    }

    if (path.endsWith('/account/sessions') && method === 'GET') {
      const refused = requireSession();
      if (refused) return refused;
      const current =
        state.ownSessions.find((session) => session.is_current) ?? state.ownSessions[0];
      return json({
        items: state.ownSessions,
        current_session_id: current?.session_id ?? '',
      });
    }

    if (path.endsWith('/account/sessions/revoke-others') && method === 'POST') {
      const refused = requireSession() ?? requireCsrf(headers) ?? forcedFailure();
      if (refused) return refused;
      const removed = state.ownSessions.filter((session) => !session.is_current).length;
      state.ownSessions = state.ownSessions.filter((session) => session.is_current);
      return json({ sessions_revoked: removed });
    }

    const revokeOne = /\/account\/sessions\/([^/]+)\/revoke$/.exec(path);
    if (revokeOne && method === 'POST') {
      const refused = requireSession() ?? requireCsrf(headers) ?? forcedFailure();
      if (refused) return refused;
      const sessionId = captured(revokeOne);
      const before = state.ownSessions.length;
      state.ownSessions = state.ownSessions.filter(
        (session) => session.session_id !== sessionId,
      );
      if (state.ownSessions.length === before) {
        return envelope(
          404,
          'not_found',
          'That session does not exist, or is no longer active.',
        );
      }
      return json({ sessions_revoked: 1 });
    }

    if (path.endsWith('/account/password') && method === 'POST') {
      const refused = requireSession() ?? requireCsrf(headers) ?? forcedFailure();
      if (refused) return refused;
      const payload = body as { current_password?: string; new_password?: string };
      if (payload.current_password !== state.currentPassword) {
        // 422 and never 401 — a 401 would be read by the client as an expired
        // session and sign the user out for a typo.
        return envelope(422, 'invalid_current_password', 'The current password is incorrect.');
      }
      const newPassword = payload.new_password ?? '';
      if (newPassword.length < 8) {
        return envelope(422, 'validation_failed', 'The new password does not meet the policy.', {
          details: { problems: ['password must be at least 8 characters'] },
        });
      }
      const otherRevoked = state.ownSessions.filter((session) => !session.is_current).length;
      state.ownSessions = state.ownSessions.filter((session) => session.is_current);
      state.currentPassword = newPassword;
      return json({
        status: 'password_changed',
        other_sessions_revoked: otherRevoked,
        session_expires_at: '2030-01-01T00:00:00+00:00',
      });
    }

    /* --- User administration (milestone 1.7) --- */

    if (path.endsWith('/users') && method === 'GET') {
      const refused = requireAdmin();
      if (refused) return refused;

      const params = new URLSearchParams(query);
      const search = fold(params.get('search') ?? '');
      const role = params.get('role');
      const isActive = params.get('is_active');
      const limit = Number(params.get('limit') ?? '25');
      const offset = Number(params.get('offset') ?? '0');

      const matching = state.users
        .filter((user) => (search ? fold(user.username).includes(search) : true))
        .filter((user) => (role ? user.role === role : true))
        .filter((user) =>
          isActive === null ? true : user.is_active === (isActive === 'true'),
        )
        // The server orders by canonical username with the id breaking ties.
        .sort((left, right) => fold(left.username).localeCompare(fold(right.username)));

      return json({
        items: matching.slice(offset, offset + limit),
        total: matching.length,
        limit,
        offset,
      });
    }

    if (path.endsWith('/users') && method === 'POST') {
      const refused = requireAdmin() ?? requireCsrf(headers) ?? forcedFailure();
      if (refused) return refused;

      const payload = body as {
        username?: string;
        password?: string;
        display_name?: string | null;
        role?: UserRole;
        is_active?: boolean;
      };
      const username = (payload.username ?? '').trim();

      if (state.users.some((user) => fold(user.username) === fold(username))) {
        return envelope(
          409,
          'username_taken',
          `The username '${username}' is already taken. Usernames are ` +
            'case-insensitive, so a different capitalisation is still the same account.',
        );
      }
      if (fold(username).length < 3) {
        return envelope(422, 'validation_failed', 'username must be at least 3 characters');
      }
      if ((payload.password ?? '').length < 8) {
        return envelope(422, 'validation_failed', 'The password does not meet the policy.', {
          details: { problems: ['password must be at least 8 characters'] },
        });
      }

      const created = makeManagedUser({
        user_id: `generated-${state.users.length + 1}`,
        username,
        display_name: blankToNull(payload.display_name),
        // Matches the server's conservative default.
        role: payload.role ?? 'viewer',
        is_active: payload.is_active ?? true,
      });
      state.users.push(created);
      return json(created, 201);
    }

    const userPath = /\/users\/([^/]+)$/.exec(path);
    if (userPath && (method === 'GET' || method === 'PATCH')) {
      const refused =
        method === 'PATCH'
          ? (requireAdmin() ?? requireCsrf(headers) ?? forcedFailure())
          : requireAdmin();
      if (refused) return refused;

      const userId = captured(userPath);
      const user = state.users.find((candidate) => candidate.user_id === userId);
      if (!user) {
        return envelope(404, 'not_found', 'That user account does not exist.');
      }
      if (method === 'GET') return json(user);

      const payload = body as {
        display_name?: string | null;
        role?: UserRole;
        is_active?: boolean;
      };
      const losesAdmin =
        (payload.role !== undefined && payload.role !== 'admin') ||
        payload.is_active === false;
      if (
        losesAdmin &&
        user.role === 'admin' &&
        user.is_active &&
        otherActiveAdmins(user.user_id) === 0
      ) {
        return lastAdministrator(
          payload.is_active === false
            ? 'deactivate this administrator'
            : "change this administrator's role",
        );
      }

      if ('display_name' in payload) {
        user.display_name = blankToNull(payload.display_name);
      }
      if (payload.role !== undefined) user.role = payload.role;
      if (payload.is_active !== undefined) {
        user.is_active = payload.is_active;
        if (!payload.is_active) user.active_session_count = 0;
      }
      return json(user);
    }

    const activation = /\/users\/([^/]+)\/(deactivate|reactivate)$/.exec(path);
    if (activation && method === 'POST') {
      const refused = requireAdmin() ?? requireCsrf(headers) ?? forcedFailure();
      if (refused) return refused;

      const userId = captured(activation);
      const user = state.users.find((candidate) => candidate.user_id === userId);
      if (!user) {
        return envelope(404, 'not_found', 'That user account does not exist.');
      }

      const activating = activation[2] === 'reactivate';
      if (
        !activating &&
        user.role === 'admin' &&
        user.is_active &&
        otherActiveAdmins(user.user_id) === 0
      ) {
        return lastAdministrator('deactivate this administrator');
      }

      user.is_active = activating;
      if (!activating) user.active_session_count = 0;
      return json(user);
    }

    const revokeUserSessions = /\/users\/([^/]+)\/sessions\/revoke$/.exec(path);
    if (revokeUserSessions && method === 'POST') {
      const refused = requireAdmin() ?? requireCsrf(headers) ?? forcedFailure();
      if (refused) return refused;

      const userId = captured(revokeUserSessions);
      const user = state.users.find((candidate) => candidate.user_id === userId);
      if (!user) {
        return envelope(404, 'not_found', 'That user account does not exist.');
      }
      const revoked = user.active_session_count;
      user.active_session_count = 0;
      return json({ sessions_revoked: revoked });
    }

    /* --- Audit (milestone 1.7) --- */

    if (path.endsWith('/audit/actions') && method === 'GET') {
      const refused = requireAdmin();
      if (refused) return refused;
      return json([
        'user.created',
        'user.updated',
        'user.role_changed',
        'user.deactivated',
        'user.reactivated',
        'user.sessions_revoked',
        'account.password_changed',
        'account.sessions_revoked',
        'admin.access_denied',
      ]);
    }

    if (path.endsWith('/audit/events') && method === 'GET') {
      const refused = requireAdmin();
      if (refused) return refused;

      const params = new URLSearchParams(query);
      const action = params.get('action');
      const cursor = params.get('cursor');
      const limit = Number(params.get('limit') ?? '25');

      if (cursor !== null && !cursor.startsWith('cursor-')) {
        return envelope(
          422,
          'validation_failed',
          'The pagination cursor is not valid. It should be the `next_cursor` ' +
            'from a previous response, unmodified.',
        );
      }

      const matching = state.auditEvents.filter((event) =>
        action ? event.action === action : true,
      );
      // Keyset paging, imitated: the cursor carries the index after the last
      // row already seen, which is what the real opaque token encodes a
      // position for.
      const start = cursor ? Number(cursor.replace('cursor-', '')) : 0;
      const page = matching.slice(start, start + limit);
      const nextIndex = start + limit;

      return json({
        items: page,
        next_cursor: nextIndex < matching.length ? `cursor-${nextIndex}` : null,
        total: matching.length,
        limit,
      });
    }

    /* --- Hosts (milestone 1.8) ---------------------------------------- */

    if (path.endsWith('/hosts') && method === 'GET') {
      // Any authenticated role, deliberately: a Viewer who cannot see the
      // fleet cannot use a monitoring product. The gate on the mutations
      // below is the administrator one.
      const refused = requireSession();
      if (refused) return refused;

      const params = new URLSearchParams(query);
      const search = fold(params.get('search') ?? '');
      const lifecycle = params.get('lifecycle');
      const connectivity = params.get('connectivity');
      const isEnabled = params.get('is_enabled');
      const limit = Number(params.get('limit') ?? '25');
      const offset = Number(params.get('offset') ?? '0');

      const matching = state.hosts
        .filter((host) => (search ? fold(host.name).includes(search) : true))
        .filter((host) => (lifecycle ? host.lifecycle === lifecycle : true))
        .filter((host) =>
          isEnabled === null ? true : host.is_enabled === (isEnabled === 'true'),
        )
        .slice()
        .sort((left, right) => fold(left.name).localeCompare(fold(right.name)));

      // The connectivity filter is applied to the *page*, after slicing, and
      // the total counts the other filters — exactly as the server does, so a
      // test cannot pass against a fake that is kinder than the real thing.
      const page = matching
        .slice(offset, offset + limit)
        .filter((host) => (connectivity ? host.connectivity === connectivity : true));

      return json({ items: page, total: matching.length, limit, offset });
    }

    const hostDetail = /\/hosts\/([^/]+)$/.exec(path);
    if (hostDetail && method === 'GET') {
      const refused = requireSession();
      if (refused) return refused;

      const host = state.hosts.find((candidate) => candidate.host_id === captured(hostDetail));
      if (!host) return envelope(404, 'not_found', 'That host does not exist.');
      return json(host);
    }

    if (path.endsWith('/hosts') && method === 'POST') {
      const refused = requireAdmin() ?? requireCsrf(headers) ?? forcedFailure();
      if (refused) return refused;

      const payload = body as {
        name?: string;
        description?: string;
        environment?: string;
      };
      const name = payload.name?.trim() ?? '';
      if (name === '') {
        return envelope(422, 'validation_failed', 'host name must not be empty');
      }
      if (state.hosts.some((host) => fold(host.name) === fold(name))) {
        return envelope(
          409,
          'host_name_taken',
          `The host name '${name}' is already in use. Host names are ` +
            'case-insensitive, so a different capitalisation is still the same host.',
        );
      }

      const host = makeHost({
        host_id: `host-${String(state.hosts.length + 1)}`,
        name,
        description: blankToNull(payload.description),
        environment: blankToNull(payload.environment),
      });
      state.hosts.push(host);
      return json(host, 201);
    }

    const hostAction = /\/hosts\/([^/]+)\/(disable|enable|enrollment|enrollment\/revoke|credential\/revoke)$/.exec(
      path,
    );
    if (hostAction && method === 'POST') {
      const refused = requireAdmin() ?? requireCsrf(headers) ?? forcedFailure();
      if (refused) return refused;

      const host = state.hosts.find((candidate) => candidate.host_id === captured(hostAction));
      if (!host) return envelope(404, 'not_found', 'That host does not exist.');
      const action = hostAction[2];

      if (action === 'disable' || action === 'enable') {
        host.is_enabled = action === 'enable';
        if (action === 'disable') {
          // The server clears the recorded connection, so a disabled host
          // reads as offline immediately rather than claiming an agent is
          // still attached.
          host.connectivity = 'offline';
          host.last_disconnected_at = '2026-10-12T19:00:00+00:00';
        }
        return json(host);
      }

      if (action === 'enrollment') {
        const payload = (body ?? {}) as { allow_reenrollment?: boolean };
        if (host.credential_issued_at !== null && payload.allow_reenrollment !== true) {
          return envelope(
            409,
            'host_already_enrolled',
            'This host already has an enrolled agent. Issuing a new enrolment ' +
              'credential will revoke the existing one and disconnect that agent ' +
              'until it is re-enrolled. Confirm re-enrollment to proceed.',
          );
        }
        const superseded = host.has_pending_enrollment ? 1 : 0;
        host.has_pending_enrollment = true;
        return json(
          {
            enrollment_id: 'enrolment-1',
            credential: state.enrollmentCredential,
            expires_at: '2026-10-12T19:15:00+00:00',
            expires_in_seconds: 900,
            superseded,
          },
          201,
        );
      }

      if (action === 'enrollment/revoke') {
        const revoked = host.has_pending_enrollment ? 1 : 0;
        host.has_pending_enrollment = false;
        return json({ revoked });
      }

      // credential/revoke
      if (host.credential_issued_at === null) {
        return envelope(
          409,
          'no_live_credential',
          'This host has no active agent credential, so there is nothing to revoke.',
        );
      }
      host.credential_issued_at = null;
      host.lifecycle = 'revoked';
      host.connectivity = 'offline';
      return json(host);
    }


    /* --- Phase 2: monitoring configuration ------------------------------ */

    if (path.endsWith('/monitoring/modules') && method === 'GET') {
      return json({
        modules: FAKE_MODULES,
        registry_version: 1,
        document_version: 1,
        min_interval_seconds: 1,
        max_interval_seconds: 86_400,
      });
    }

    if (path.endsWith('/monitoring/defaults') && method === 'GET') {
      return json({ defaults: globalDefaultList() });
    }

    const globalDefault = /\/monitoring\/defaults\/([^/]+)$/.exec(path);
    if (globalDefault !== null && method === 'PUT') {
      // Administrator-only, like every monitoring mutation. The gate is the
      // server's; restated here so a test cannot pass against a fake that is
      // more permissive than the thing it imitates.
      // The same chain every other mutation uses: the administrator gate, the
      // CSRF header the real server requires, then any failure a test armed.
      const refused = requireAdmin() ?? requireCsrf(headers) ?? forcedFailure();
      if (refused) return refused;
      const moduleId = captured(globalDefault);
      const descriptor = FAKE_MODULES.find((entry) => entry.id === moduleId);
      if (descriptor === undefined) {
        return envelope(404, 'not_found', `No such module: ${moduleId}.`);
      }
      const refusal = applySetting(
        (state.globalDefaults[moduleId] ??= unsetSetting()),
        body,
        descriptor,
        (next) => {
          state.globalDefaults[moduleId] = next;
        },
      );
      if (refusal !== null) return refusal;
      return json({ defaults: globalDefaultList() });
    }

    const hostMonitoring = /\/hosts\/([^/]+)\/monitoring$/.exec(path);
    if (hostMonitoring !== null && method === 'GET') {
      const hostId = captured(hostMonitoring);
      const host = state.hosts.find((candidate) => candidate.host_id === hostId);
      if (host === undefined) return envelope(404, 'not_found', 'No such host.');
      return json({
        host_id: hostId,
        modules: FAKE_MODULES.map((descriptor) => effectiveModule(hostId, descriptor)),
        problems: state.hostConfigProblems[hostId] ?? [],
        sync: state.hostSync[hostId] ?? noSync(),
      });
    }

    const hostModule = /\/hosts\/([^/]+)\/monitoring\/modules\/([^/]+)$/.exec(path);
    if (hostModule !== null && (method === 'PUT' || method === 'DELETE')) {
      // The same chain every other mutation uses: the administrator gate, the
      // CSRF header the real server requires, then any failure a test armed.
      const refused = requireAdmin() ?? requireCsrf(headers) ?? forcedFailure();
      if (refused) return refused;
      const hostId = captured(hostModule);
      const moduleId = hostModule[2] ?? '';
      const host = state.hosts.find((candidate) => candidate.host_id === hostId);
      if (host === undefined) return envelope(404, 'not_found', 'No such host.');
      const descriptor = FAKE_MODULES.find((entry) => entry.id === moduleId);
      if (descriptor === undefined) {
        return envelope(404, 'not_found', `No such module: ${moduleId}.`);
      }

      const overrides = (state.hostOverrides[hostId] ??= {});
      if (method === 'DELETE') {
        // Drops the whole override row, schedule included — which is what
        // "inherit" means, and is why the client has a distinct call for it.
        // eslint-disable-next-line @typescript-eslint/no-dynamic-delete
        delete overrides[moduleId];
      } else {
        const refusal = applySetting(
          (overrides[moduleId] ??= unsetSetting()),
          body,
          descriptor,
          (next) => {
            overrides[moduleId] = next;
          },
        );
        if (refusal !== null) return refusal;
      }

      const version = (state.hostSync[hostId]?.desired_version ?? 0) + 1;
      state.hostSync[hostId] = {
        ...(state.hostSync[hostId] ?? noSync()),
        status: 'pending',
        desired_version: version,
        generated_at: new Date().toISOString(),
      };
      return json({
        module_id: moduleId,
        setting: overrides[moduleId] ?? unsetSetting(),
        revision_created: true,
        revision_version: version,
        sync: state.hostSync[hostId],
      });
    }

    /* --- Phase 3: metrics ------------------------------------------------ */

    const metricsRoute = /\/hosts\/([^/]+)\/metrics\/(latest|series|range|status)$/.exec(path);
    if (metricsRoute !== null && method === 'GET') {
      const hostId = captured(metricsRoute);
      const leaf = metricsRoute[2];
      const host = state.hosts.find((candidate) => candidate.host_id === hostId);
      // 404 rather than an empty series: an empty series would look like a
      // host that exists and has collected nothing.
      if (host === undefined) return envelope(404, 'not_found', 'No such host.');

      const params = new URLSearchParams(query);
      const freshness = Number(params.get('freshness_seconds') ?? '300');
      const series = state.metricSeries[hostId] ?? [];

      if (leaf === 'latest') {
        const cutoff = Date.now() - freshness * 1000;
        // The freshness window is applied, not assumed: a stale value must be
        // absent, and a fake that returned everything would let a test pass
        // against an interface that showed three-day-old readings as current.
        const values = (state.latestMetrics[hostId] ?? []).filter(
          (entry) => new Date(entry.recorded_at).getTime() >= cutoff,
        );
        return json({ host_id: hostId, freshness_seconds: freshness, values });
      }

      if (leaf === 'series') {
        return json({ host_id: hostId, series });
      }

      if (leaf === 'status') {
        const newest = series
          .map((entry) => entry.last_sample_at)
          .filter((value): value is string => value !== null)
          .sort()
          .at(-1);
        return json({
          host_id: hostId,
          sample_count: state.metricSampleCount[hostId] ?? 0,
          series_count: series.length,
          last_sample_at: newest ?? null,
          modules: FAKE_MODULES.map((descriptor) => {
            const owned = series.filter((entry) => entry.module === descriptor.id);
            const last = owned
              .map((entry) => entry.last_sample_at)
              .filter((value): value is string => value !== null)
              .sort()
              .at(-1);
            return {
              module: descriptor.id,
              module_name: descriptor.name,
              series_count: owned.length,
              last_sample_at: last ?? null,
            };
          }),
        });
      }

      // range
      const seriesId = Number(params.get('series_id') ?? '0');
      const start = params.get('start') ?? '';
      const end = params.get('end') ?? '';
      // A naive timestamp is refused rather than assumed to be UTC, matching
      // the server — so a client that stopped sending zoned instants fails a
      // test instead of silently asking for the wrong window.
      for (const [field, value] of [['start', start], ['end', end]] as const) {
        if (!/(?:Z|[+-]\d{2}:?\d{2})$/.test(value)) {
          return envelope(
            422,
            'validation_failed',
            `\`${field}\` must carry a time zone, e.g. 2026-10-13T12:00:00Z.`,
            { details: { parameter: field } },
          );
        }
      }
      const found = series.find((entry) => entry.series_id === seriesId);
      // A series id alone is not authorization: one belonging to another host
      // answers 404, the same as one that does not exist.
      if (found === undefined) {
        return envelope(404, 'not_found', 'No such series on this host.');
      }
      return json({
        series: found,
        resolution: state.metricResolution,
        start,
        end,
        points: state.metricPoints[seriesId] ?? [],
        truncated: false,
      });
    }

    return envelope(404, 'not_found', 'The requested resource does not exist.');
  };

  vi.stubGlobal('fetch', vi.fn(handler));

  return {
    state,
    requests,
    requestsFor: (path: string) => requests.filter((request) => request.path.endsWith(path)),
    restore: () => {
      vi.unstubAllGlobals();
      clearCookies();
    },
  };
}

/** Assert a request carried the CSRF header the backend requires. */
export function expectCsrfHeader(request: RecordedRequest | undefined): void {
  expect(request, 'no such request was made').toBeDefined();
  expect(request?.headers['x-csrf-token']).toBe(TEST_CSRF_TOKEN);
}

/**
 * A user with a specific role.
 *
 * The usernames deliberately do not resemble the role labels the interface
 * renders ("Viewer", "Operator", "Administrator"): a fixture named `Operator`
 * makes `getByText('Operator')` ambiguous, which looks like a component bug.
 */
const USERNAME_BY_ROLE: Record<UserRole, string> = {
  viewer: 'Ren',
  operator: 'Casey',
  admin: 'Morgan',
};

export function userWithRole(role: UserRole): CurrentUser {
  return makeUser({ role, username: USERNAME_BY_ROLE[role] });
}

/**
 * A rollout, with the cautious defaults the API has.
 *
 * `members` is empty by default and a caller supplies them; the counts are
 * not derived from the members on purpose, because the server sends both and
 * a test that wanted them to disagree — a member list that has not caught up
 * with the counts — should be able to say so.
 */
export function makeRollout(overrides: Partial<Rollout> = {}): Rollout {
  return {
    rollout_id: 'r-1',
    name: 'to 0.2.0',
    to_version: '0.2.0',
    state: 'canary',
    canary_count: 1,
    batch_size: 5,
    failure_tolerance: 0,
    health_check_seconds: 120,
    offline_deadline_seconds: 3600,
    failures_overridden: false,
    window: null,
    paused_reason: null,
    created_at: new Date(Date.now() - 60_000).toISOString(),
    started_at: new Date(Date.now() - 30_000).toISOString(),
    finished_at: null,
    total: 0,
    succeeded: 0,
    failed: 0,
    skipped: 0,
    running: 0,
    pending: 0,
    current_batch: 0,
    members: [],
    ...overrides,
  };
}

/** One host's place in a rollout. */
export function makeRolloutMember(
  overrides: Partial<RolloutMember> = {},
): RolloutMember {
  return {
    host_id: 'h-1',
    batch: 0,
    state: 'pending',
    healthy: false,
    detail: null,
    ...overrides,
  };
}
