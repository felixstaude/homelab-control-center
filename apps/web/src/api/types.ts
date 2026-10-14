/**
 * The API contract this client depends on.
 *
 * ## Why these are hand-written, for now
 *
 * ADR-007 chose generated types over hand-maintained ones, and the roadmap
 * reserves full OpenAPI generation for **milestone 1.15** ("Generated API
 * client … drift fails CI"). Taking that over here would be scope this
 * milestone was not asked for, so instead:
 *
 * - these four response shapes are written out by hand, because four small
 *   objects are cheap and a generator is not needed to express them;
 * - and `openapi.test.ts` validates every one of them against the backend's
 *   **real OpenAPI schema**, committed as a snapshot, so the types cannot
 *   drift from the server without a test failing.
 *
 * That is the minimum typed contract with genuine drift detection. Milestone
 * 1.15 replaces the hand-written half with generation; the drift check is the
 * part worth having first, because a type that merely *looks* right is the
 * failure mode generation exists to prevent.
 *
 * Regenerate the snapshot with `make openapi` after any API change.
 */

/** A role, as the backend's `user_role` enum spells it (ADR-013). */
export type UserRole = 'viewer' | 'operator' | 'admin';

/**
 * `GET /api/v1/auth/me` and `POST /api/v1/auth/login`.
 *
 * Both return the same shape. `csrf_token` is included because the client
 * needs it to make state-changing requests and it is useless without the
 * session cookie (ADR-030) — but the client reads it from the cookie rather
 * than holding this copy, so that it can never be stale.
 */
export type CurrentUser = {
  user_id: string;
  /** Display form, as the user typed it at creation. */
  username: string;
  /**
   * Human-readable label, or `null` when the account has none.
   *
   * `null` means "no separate label", never "unknown" — render {@link username}
   * in that case. Use {@link userLabel} rather than writing the fallback at
   * each call site.
   */
  display_name: string | null;
  role: UserRole;
  is_active: boolean;
  /** ISO-8601 absolute session expiry. */
  session_expires_at: string;
  csrf_token: string;
};

/**
 * What to call an account in the interface.
 *
 * One helper, so the "display name, else username" fallback cannot be spelled
 * differently in different places — and so an empty-looking cell is never the
 * result of a missing fallback. The backend keeps the empty string unstorable
 * precisely so this is a two-case decision rather than three.
 */
export function userLabel(user: {
  username: string;
  display_name: string | null;
}): string {
  return user.display_name ?? user.username;
}

/** `POST /api/v1/auth/logout`. */
export type LogoutResult = {
  status: 'logged_out';
};

/**
 * `GET /api/v1/setup/status`.
 *
 * One field, deliberately: it is a public endpoint. See
 * `apps/server/hcc/api/v1/setup.py`.
 */
export type SetupStatus = {
  initialized: boolean;
};

/** `GET /api/v1/info`. Authenticated since milestone 1.4. */
export type ApiInfo = {
  api_version: string;
  server_version: string;
  environment: string;
};

/** Credentials submitted to the login endpoint. */
export type LoginCredentials = {
  username: string;
  password: string;
};

/* -------------------------------------------------------------------------
 * User administration (milestone 1.7)
 * ---------------------------------------------------------------------- */

/**
 * One account, as `GET /api/v1/users` returns it.
 *
 * Note what is absent and will stay absent: no password hash, no
 * `username_canonical`, no session token. The server's response model is an
 * allow-list rather than a serialised row, and `openapi.test.ts` asserts the
 * two sides agree **exactly** — so a new server field reaches the browser only
 * once somebody decides it should.
 */
export type ManagedUser = {
  user_id: string;
  username: string;
  display_name: string | null;
  role: UserRole;
  is_active: boolean;
  /** ISO-8601. */
  created_at: string;
  /** ISO-8601, or `null` for "has never signed in". Never render as a date. */
  last_login_at: string | null;
  /**
   * Sessions that would currently authenticate this account.
   *
   * Counts the same thing session resolution does, including the idle
   * timeout, so a non-zero count means access that actually works.
   */
  active_session_count: number;
};

/** `GET /api/v1/users`. */
export type UserListResult = {
  items: ManagedUser[];
  /** Matching the filters, ignoring pagination. */
  total: number;
  limit: number;
  offset: number;
};

/** Query parameters for the user list. All optional. */
export type UserListParams = {
  /** Substring match against the normalized username. */
  search?: string | undefined;
  role?: UserRole | undefined;
  isActive?: boolean | undefined;
  limit?: number | undefined;
  offset?: number | undefined;
};

/** `POST /api/v1/users`. */
export type CreateUserRequest = {
  username: string;
  password: string;
  display_name?: string | null;
  /** Omitted means `viewer` — the server's conservative default. */
  role?: UserRole;
  is_active?: boolean;
};

/**
 * `PATCH /api/v1/users/{id}`.
 *
 * Partial: omitting a key leaves the field unchanged. `display_name: null`
 * **clears** it, which is why the field is optional *and* nullable — the two
 * states are different and the server distinguishes them.
 *
 * `username` is deliberately not here. Usernames are immutable (ADR-041).
 */
export type UpdateUserRequest = {
  display_name?: string | null;
  role?: UserRole;
  is_active?: boolean;
};

/** `POST .../sessions/revoke` and the self-service revocations. */
export type SessionRevocationResult = {
  /** Zero is a success that changed nothing, not a failure. */
  sessions_revoked: number;
};

/* -------------------------------------------------------------------------
 * Own account (milestone 1.7)
 * ---------------------------------------------------------------------- */

/** `GET /api/v1/account`. Carries no CSRF token — that is `/auth/me`'s job. */
export type AccountDetails = {
  user_id: string;
  username: string;
  display_name: string | null;
  role: UserRole;
  is_active: boolean;
  created_at: string;
  last_login_at: string | null;
};

/**
 * One of the caller's own sessions.
 *
 * `session_id` is the row's primary key, not the credential: the session token
 * is an `HttpOnly` cookie stored server-side only as a digest, and presenting
 * an id authenticates nothing. See `apps/server/hcc/api/v1/account.py`.
 */
export type OwnSession = {
  session_id: string;
  is_current: boolean;
  created_at: string;
  /** Accurate to within the server's touch interval, not to the second. */
  last_seen_at: string;
  expires_at: string;
  created_ip: string | null;
  /**
   * The raw, truncated `User-Agent` recorded at sign-in, or `null`.
   *
   * Rendered verbatim. Deliberately not parsed into a browser or operating
   * system label: that would be a guess presented as a fact, and wrong exactly
   * for the unusual clients worth noticing.
   */
  user_agent: string | null;
};

/** `GET /api/v1/account/sessions`. */
export type OwnSessionList = {
  items: OwnSession[];
  /** Always one of `items`, so the client never infers which session it is. */
  current_session_id: string;
};

/** `POST /api/v1/account/password`. Confirmation is checked client-side. */
export type PasswordChangeRequest = {
  current_password: string;
  new_password: string;
};

/** `POST /api/v1/account/password`. */
export type PasswordChangeResult = {
  status: string;
  /** Excludes the current session, which was replaced rather than ended. */
  other_sessions_revoked: number;
  session_expires_at: string;
};

/* -------------------------------------------------------------------------
 * Audit trail (milestone 1.7)
 * ---------------------------------------------------------------------- */

/** An account referenced by an audit event. */
export type AuditActor = {
  user_id: string;
  /** `null` when the account no longer exists. Render the id. */
  username: string | null;
  display_name: string | null;
};

/**
 * One recorded privileged operation.
 *
 * `action` is a plain string rather than a union, and that is the contract
 * (ADR-038): the vocabulary grows with every milestone that records something
 * new, so a client must render an unrecognised value rather than fail on it.
 */
export type AuditEvent = {
  event_id: string;
  occurred_at: string;
  action: string;
  outcome: string;
  actor: AuditActor | null;
  target: AuditActor | null;
  /**
   * The host the action was about, or `null`.
   *
   * Separate from `target`, which is an *account*: an event can have one, the
   * other or neither. Collapsing them would mean rendering a host id in a
   * field labelled as a username — and update actions are host-targeted,
   * which makes this the field somebody reading the trail most needs.
   */
  target_host_id: string | null;
  request_id: string | null;
  details: Record<string, unknown> | null;
};

/** `GET /api/v1/audit/events`. */
export type AuditEventList = {
  items: AuditEvent[];
  /**
   * Opaque. Pass back as `cursor` for the next page; `null` on the last.
   *
   * Keyset rather than an offset, so events recorded while an operator reads
   * cannot duplicate or hide rows.
   */
  next_cursor: string | null;
  total: number;
  limit: number;
};

/** Query parameters for the audit list. */
export type AuditEventParams = {
  action?: string | undefined;
  targetUserId?: string | undefined;
  actorUserId?: string | undefined;
  cursor?: string | undefined;
  limit?: number | undefined;
};

/* -------------------------------------------------------------------------
 * Host registry (milestone 1.8)
 * ---------------------------------------------------------------------- */

/**
 * How far through enrollment a host record is.
 *
 * Independent of connectivity and of administrative state — three orthogonal
 * axes, and conflating any two of them is the mistake the host schema is
 * shaped to avoid. A `revoked` host is not `registered`: the record has been
 * used, and re-enrolling it is a different operator decision from enrolling
 * one for the first time.
 */
export type HostLifecycle = 'registered' | 'enrolled' | 'revoked';

/**
 * Whether an agent is attached right now.
 *
 * **Derived by the server**, never a stored flag — so a controller crash
 * reports `stale` rather than leaving every host claiming to be connected
 * forever. `stale` is the honest third state: a connection is recorded and the
 * heartbeats stopped, and the controller genuinely cannot tell whether the
 * agent died or the network did.
 */
export type HostConnectivity = 'offline' | 'connected' | 'stale';

/**
 * One host, as `GET /api/v1/hosts` and `GET /api/v1/hosts/{id}` return it.
 *
 * Every nullable field here means "we have not been told", never zero and
 * never a placeholder date. Rendering `last_heartbeat_at: null` as a date
 * would be inventing a measurement, which is the one thing a monitoring
 * interface must not do.
 *
 * `agent_version`, `platform`, `protocol_version` and `agent_started_at` are
 * **self-reported by the agent** and therefore untrusted: they come from a
 * machine whose security the controller does not own. They are shown so an
 * operator can see what is deployed where, and nothing decides anything from
 * them.
 */
export type Host = {
  host_id: string;
  /** Display form, as the administrator typed it. */
  name: string;
  /** Operator's note, or `null` when there is none. */
  description: string | null;
  /** Free-text grouping label, or `null`. Nothing resolves against it. */
  environment: string | null;
  lifecycle: HostLifecycle;
  /**
   * Whether the operator currently wants this host connected.
   *
   * Orthogonal to {@link lifecycle} and {@link connectivity}: a disabled host
   * keeps its enrollment and its history, and its agent is refused at the
   * door.
   */
  is_enabled: boolean;
  connectivity: HostConnectivity;
  /** ISO-8601, or `null` when no agent has ever reported. */
  last_heartbeat_at: string | null;
  last_connected_at: string | null;
  last_disconnected_at: string | null;
  /** Self-reported by the agent. Advisory. */
  agent_version: string | null;
  /** Reported os/arch, e.g. `linux/amd64`. Advisory. */
  platform: string | null;
  protocol_version: number | null;
  agent_started_at: string | null;
  /**
   * Whether an unused, unexpired enrollment credential exists.
   *
   * A boolean and nothing more. The credential itself is unrecoverable once
   * issued — only its digest is stored — so there is no field that could
   * carry it and no endpoint that could return it.
   */
  has_pending_enrollment: boolean;
  /**
   * When the live agent credential was issued, or `null` when there is none.
   *
   * Distinguishes "never enrolled" from "credential revoked" without
   * disclosing anything about the credential.
   */
  credential_issued_at: string | null;
  created_at: string;
};

/** `GET /api/v1/hosts`. */
export type HostListResult = {
  items: Host[];
  /**
   * Hosts matching the name, lifecycle and status filters, ignoring
   * pagination.
   *
   * When a `connectivity` filter is supplied this counts what the *other*
   * filters matched, because connectivity is derived and is applied to the
   * page after it is fetched — so `items` may be shorter than `limit`. That
   * is a documented server-side trade, not a bug to work around here.
   */
  total: number;
  limit: number;
  offset: number;
};

/** Query parameters for the host list. */
export type HostListParams = {
  search?: string | undefined;
  lifecycle?: HostLifecycle | undefined;
  isEnabled?: boolean | undefined;
  connectivity?: HostConnectivity | undefined;
  limit?: number | undefined;
  offset?: number | undefined;
};

/** `POST /api/v1/hosts`. */
export type CreateHostRequest = {
  name: string;
  description?: string | undefined;
  environment?: string | undefined;
};

/**
 * `PATCH /api/v1/hosts/{id}`. Labels only.
 *
 * `null` clears a label; an omitted key leaves it alone. There is deliberately
 * no field here that could set the lifecycle, the administrative state or any
 * connectivity column: enrollment state changes by enrolling or revoking,
 * administrative state through the enable and disable endpoints, and
 * connectivity by an agent connecting. A field that could set any of them
 * would let an administrator type a host into a state the system had not
 * reached.
 */
export type UpdateHostRequest = {
  name?: string | undefined;
  description?: string | null | undefined;
  environment?: string | null | undefined;
};

/** `POST /api/v1/hosts/{id}/enrollment`. */
export type IssueEnrollmentRequest = {
  /**
   * Required to issue a credential for a host that already has an enrolled
   * agent, because consuming the new credential revokes the existing one and
   * disconnects that agent.
   */
  allow_reenrollment?: boolean | undefined;
};

/**
 * `POST /api/v1/hosts/{id}/enrollment`.
 *
 * **The only response in this API that contains a secret.** The credential is
 * not stored — only its digest is — so this body cannot be reproduced by any
 * later request, including a retry of this one. The interface has to show it
 * once and say so, and must not keep it anywhere it could outlive the dialog.
 */
export type IssuedEnrollment = {
  enrollment_id: string;
  /** Plaintext, shown once, never retrievable again. */
  credential: string;
  expires_at: string;
  /** Computed server-side, so a countdown does not depend on the browser's clock. */
  expires_in_seconds: number;
  /**
   * How many previously-pending credentials this issuance revoked.
   *
   * Non-zero means an earlier credential for this host has just stopped
   * working, which an administrator needs to be told rather than discover by
   * watching an agent fail.
   */
  superseded: number;
};

/** `POST /api/v1/hosts/{id}/enrollment/revoke`. */
export type HostRevocationResult = {
  /** Zero means there was nothing to revoke — the request changed nothing. */
  revoked: number;
};

/* ===========================================================================
 * Monitoring configuration (Phase 2)
 * ======================================================================== */

/** Every module the controller knows how to run. A closed set. */
export type ModuleId = 'cpu' | 'memory' | 'swap' | 'load' | 'uptime';

/** Which layer decided a resolved value. */
export type SettingSource = 'default' | 'global' | 'host';

/**
 * How a host's desired configuration relates to what its agent is running.
 *
 * §23's distinction, carried into the client because the interface must not
 * claim a setting is active merely because it saved: `up_to_date` is the only
 * value that means the agent is actually collecting what was asked for.
 *
 * - `none` — nothing has ever been configured for this host.
 * - `up_to_date` — the agent has acknowledged the newest revision.
 * - `pending` — saved, not yet acknowledged. Not a problem on its own.
 * - `rejected` — the agent refused it, and said why.
 * - `stale` — the agent is running an older revision than the newest.
 */
export type ConfigSyncStatus = 'none' | 'up_to_date' | 'pending' | 'rejected' | 'stale';

/** A schedule document, exactly as stored and as the agent receives it. */
export type ScheduleDocument = {
  kind: string;
  interval_seconds?: number;
  jitter_seconds?: number;
  [key: string]: unknown;
};

/**
 * One layer's stored opinion.
 *
 * `null` in either field means **this layer does not say** — it inherits. That
 * is a third state distinct from `false`, and conflating the two is the bug
 * this shape exists to prevent: "off here" and "whatever the global says" are
 * different instructions that happen to look the same when the global is off.
 */
export type StoredSetting = {
  enabled: boolean | null;
  schedule: ScheduleDocument | null;
};

export type ResolvedEnabled = {
  value: boolean;
  source: SettingSource;
};

export type ResolvedSchedule = {
  value: ScheduleDocument;
  source: SettingSource;
  /**
   * The next few executions, for a calendar schedule only.
   *
   * Null for an interval schedule, where the next run is arithmetic rather
   * than a calculation worth previewing.
   */
  next_runs: string[] | null;
};

/**
 * One module's effective configuration on one host, with its provenance.
 *
 * Both stored layers appear next to the resolved value because "inherited from
 * the global default of 10 s" and "overridden on this host to 10 s" are
 * identical in the value and are different facts — and only the second is
 * undone by clearing the override.
 */
export type EffectiveModule = {
  module_id: string;
  name: string;
  enabled: ResolvedEnabled;
  schedule: ResolvedSchedule;
  global_default: StoredSetting;
  /** The host's own override, or null when this host has none at all. */
  host_override: StoredSetting | null;
};

/** Desired versus applied. */
export type ConfigSync = {
  status: ConfigSyncStatus;
  desired_version: number | null;
  desired_checksum: string | null;
  generated_at: string | null;
  /** What the agent says it is running. Null means it has not said. */
  reported_version: number | null;
  reported_checksum: string | null;
  acknowledged_at: string | null;
  rejected_at: string | null;
  /** Every problem the agent reported. Empty unless rejected. */
  rejection_problems: string[];
};

/** `GET /api/v1/hosts/{id}/monitoring`. */
export type HostMonitoring = {
  host_id: string;
  modules: EffectiveModule[];
  /**
   * Configuration the controller itself considers wrong, e.g. a stored
   * schedule below a module's floor after a release lowered it. Reported
   * rather than silently corrected.
   */
  problems: string[];
  sync: ConfigSync;
};

/**
 * `PUT /api/v1/hosts/{id}/monitoring/modules/{module}` and the global form.
 *
 * Three-valued on purpose. `true`/`false` set the layer; `null` makes it
 * inherit; **omitting the key** leaves it unchanged. That is why the fields are
 * optional *and* nullable, and why the client must never fill them in
 * defensively — an omitted `schedule` and a `schedule: null` ask for different
 * things.
 */
export type ModuleSettingRequest = {
  enabled?: boolean | null;
  schedule?: ScheduleDocument | null;
};

/** The response to saving one module's setting. */
export type HostSettingSaved = {
  module_id: string;
  setting: StoredSetting;
  /**
   * Whether this save produced a new revision for the agent.
   *
   * False when the change did not alter the resolved document — saving an
   * override identical to the inherited value, for instance. Reported so the
   * interface does not claim it pushed something it did not.
   */
  revision_created: boolean;
  revision_version: number;
  sync: ConfigSync;
};

/** One global default. */
export type GlobalDefault = {
  module_id: string;
  setting: StoredSetting;
};

/** `GET /api/v1/monitoring/defaults`. */
export type GlobalDefaults = {
  defaults: GlobalDefault[];
};

/** One metric a module produces. */
export type MetricDescriptor = {
  id: string;
  name: string;
  unit: string;
  kind: string;
  /** The dimensions this metric is broken down by, e.g. `['core']`. */
  label_keys: string[];
};

/** One module in the catalogue. */
export type ModuleDescriptor = {
  id: string;
  name: string;
  description: string;
  platform: string;
  default_interval_seconds: number;
  /** The floor. A schedule below this is refused, not clamped. */
  min_interval_seconds: number;
  schedule_kinds: string[];
  requires_privileges: boolean;
  allows_duplicate_runs: boolean;
  data_schema_version: number;
  metrics: MetricDescriptor[];
};

/** `GET /api/v1/monitoring/modules`. */
export type ModuleCatalogue = {
  modules: ModuleDescriptor[];
  registry_version: number;
  document_version: number;
  min_interval_seconds: number;
  max_interval_seconds: number;
};

/* ===========================================================================
 * Metrics (Phase 3)
 * ======================================================================== */

/** One measurable stream a host has produced. */
export type MetricSeries = {
  series_id: number;
  module: string;
  metric: string;
  labels: Record<string, string>;
  unit: string;
  kind: string;
  module_name: string;
  metric_name: string;
  /** Null if this series has never received a sample. Never render as a date. */
  last_sample_at: string | null;
};

/** `GET /api/v1/hosts/{id}/metrics/series`. */
export type MetricSeriesList = {
  host_id: string;
  series: MetricSeries[];
};

export type LatestValue = {
  series: MetricSeries;
  value: number;
  recorded_at: string;
};

/**
 * `GET /api/v1/hosts/{id}/metrics/latest`.
 *
 * `values` is **empty when nothing recent exists** — not a list of zeroes. A
 * series whose newest sample is older than `freshness_seconds` is absent, so
 * the interface can say "no data in the last five minutes" rather than
 * presenting a three-day-old reading as current.
 */
export type LatestMetrics = {
  host_id: string;
  freshness_seconds: number;
  values: LatestValue[];
};

/**
 * One point of a historical series.
 *
 * `minimum` and `maximum` are present only for a rolled-up tier, and are not
 * decoration: a downsampled series drawn as a line of averages loses the
 * spikes fast sampling was turned on to catch.
 */
export type MetricRangePoint = {
  at: string;
  value: number;
  minimum: number | null;
  maximum: number | null;
  /** How many raw samples this bucket aggregates. Null for the raw tier. */
  sample_count: number | null;
};

/** Which storage tier answered a range query. */
export type MetricResolution = 'raw' | '5m' | '1h';

/** `GET /api/v1/hosts/{id}/metrics/range`. */
export type MetricRange = {
  series: MetricSeries;
  /** Reported because "this is hourly data" is needed to read a flat graph. */
  resolution: string;
  start: string;
  end: string;
  /**
   * Ordered by time, with **no points for empty buckets**. A gap is a gap:
   * nothing is interpolated, so a chart must draw a break rather than a line.
   */
  points: MetricRangePoint[];
  truncated: boolean;
};

/** What one module is doing on one host. */
export type ModuleMetricStatus = {
  module: string;
  module_name: string;
  series_count: number;
  last_sample_at: string | null;
};

/**
 * `GET /api/v1/hosts/{id}/metrics/status`.
 *
 * The question a chart cannot answer: an empty chart could mean "nothing
 * collected", "collection is off" or "the agent is offline", and only the
 * first is visible from the data.
 */
export type MetricsStatus = {
  host_id: string;
  sample_count: number;
  series_count: number;
  last_sample_at: string | null;
  /** Every module in the registry, including those that produced nothing. */
  modules: ModuleMetricStatus[];
};

/** Query parameters for a range request. */
export type MetricRangeParams = {
  seriesId: number;
  /** ISO-8601 **with a zone**. A naive timestamp is refused by the server. */
  start: string;
  end: string;
  maxPoints?: number | undefined;
  /** Omit to let the range and point budget choose, which is usually right. */
  resolution?: MetricResolution | undefined;
};

/* ===========================================================================
 * Agent releases (Nightshift II)
 * ======================================================================== */

/**
 * How much a platform has actually been exercised.
 *
 * Not a severity and not a preference — a statement about evidence.
 * `supported` means built, executed and covered by the installation matrix;
 * `best-effort` means built and executed under emulation only, with no
 * hardware behind the claim. The interface says which, because a Raspberry Pi
 * owner is entitled to know that nobody has run this on one.
 */
export type ReleaseTier = 'supported' | 'best-effort';

/** One architecture's download, with the digest that pins it. */
export type PlatformArtifact = {
  /** `linux/amd64`, `linux/arm64` or `linux/arm`. */
  platform: string;
  archive: string;
  url: string;
  /**
   * The archive's SHA-256.
   *
   * Rendered into the install command the administrator copies, so that
   * `sha256sum -c -` fails before the `tar` if the bytes are not the bytes the
   * controller expects. It arrives inside this authenticated session, which is
   * what makes it worth checking: an attacker who controls the download origin
   * cannot make a substituted tarball match it.
   */
  sha256: string;
  size_bytes: number;
  tier: ReleaseTier;
};

/**
 * `GET /api/v1/agent-releases`.
 *
 * `available: false` is a normal answer, not an error. No release of this
 * project has been published, so every build so far reports it — and the
 * interface then shows the build-from-source path rather than a download
 * command whose URL would not resolve.
 */
export type AgentReleases = {
  available: boolean;
  /** Why there is nothing to download. Null when `available`. */
  unavailable_reason: string | null;
  recommended_version: string | null;
  minimum_supported_version: string | null;
  platforms: PlatformArtifact[];
  protocol_version: number;
  supported_protocol_versions: number[];
  server_version: string;
};

/* ---------------------------------------------------------------------------
 * Update management (Operation Evergreen)
 * ------------------------------------------------------------------------ */

/**
 * Why a release cannot be installed on a host.
 *
 * `code` is for grouping and `detail` is the sentence to show. The detail is
 * written by the server because the useful part — which version, which
 * architecture, which migration — is only known there, and a client that
 * composed its own message would have to duplicate the compatibility rules.
 */
export type UpdateBlocker = {
  code: string;
  detail: string;
};

/**
 * What should happen to one host.
 *
 * `outcome` is a closed set the interface renders differently, not a boolean:
 * a host two minor versions behind on a controller too old to drive the newer
 * release is in a different situation from one that is current, and showing
 * both as "no update" would lie about which needs attention.
 */
export type UpdatePlan = {
  outcome:
    | 'up_to_date'
    | 'update_available'
    | 'update_available_incompatible'
    | 'downgrade'
    | 'pinned'
    | 'unknown_version'
    | 'unsupported'
    | 'metadata_unavailable';
  installed_version: string | null;
  target_version: string | null;
  target_channel: string | null;
  step: 'major' | 'minor' | 'patch' | 'prerelease' | 'none';
  /** Every blocker, not the first: fixing one should not hide the next. */
  blockers: UpdateBlocker[];
  detail: string | null;
  download_url: string | null;
  expected_sha256: string | null;
};

/** When a host is willing to be interrupted. */
export type MaintenanceWindow = {
  timezone: string;
  /** 0 = Sunday … 6 = Saturday. Empty means every day. */
  weekdays: number[];
  start: string;
  end: string;
  /** One line for display. Always names the zone. */
  description: string;
};

/** One update attempt. */
export type UpdateJob = {
  job_id: string;
  host_id: string;
  state: string;
  trigger: 'manual' | 'policy' | 'rollout';
  from_version: string | null;
  to_version: string;
  resulting_version: string | null;
  error: string | null;
  attempt: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  requested_by_user_id: string | null;
};

/** One state transition of an update. */
export type UpdateEvent = {
  /**
   * The log's own ordering.
   *
   * Not a timestamp: several transitions can share one, because PostgreSQL's
   * `now()` is the transaction time. Sort on this.
   */
  sequence: number;
  state: string;
  detail: string | null;
  source: 'controller' | 'agent';
  created_at: string;
};

/** One host's whole update situation. */
export type HostUpdateStatus = {
  host_id: string;
  host_name: string;
  installed_version: string | null;
  plan: UpdatePlan;
  policy: 'manual' | 'notify' | 'patch' | 'minor' | 'pinned';
  /** Whether this host installs anything by itself. */
  automatic: boolean;
  channel: string;
  pinned_version: string | null;
  maintenance_window: MaintenanceWindow | null;
  open_job: UpdateJob | null;
  last_job: UpdateJob | null;
};

/** How many hosts report one version. */
export type VersionCount = {
  version: string;
  hosts: number;
};

/** `GET /api/v1/updates/overview`. */
export type FleetUpdateOverview = {
  total_hosts: number;
  up_to_date: number;
  update_available: number;
  /** A newer release exists and something stops it. Still an update. */
  blocked: number;
  /** Unsupported or unreadable versions — hosts somebody has to look at. */
  needs_attention: number;
  /** Newest first. Unparseable versions last and visible. */
  version_distribution: VersionCount[];
  hosts_with_automatic_updates: number;
  open_jobs: number;
  failed_recently: number;
  controller_version: string;
  controller_schema_revision: string | null;
  /** Whether release metadata is configured. Not the same as "no updates". */
  metadata_available: boolean;
  metadata_detail: string | null;
  /**
   * The publisher signature's status.
   *
   * `valid` is the only value that establishes authenticity. `absent` means
   * nothing was signed — the state of an unpublished project — and
   * `untrusted_key` means something was, by a key this controller does not
   * hold. The interface must not render the last two as "signed".
   */
  metadata_signature:
    | 'valid'
    | 'absent'
    | 'untrusted_key'
    | 'invalid'
    | 'malformed'
    | 'unsupported_algorithm';
  metadata_signature_detail: string | null;
  /** The channels this controller understands, so the client hardcodes none. */
  channels: string[];
};

/** `GET /api/v1/updates/history`. */
export type UpdateHistory = {
  jobs: UpdateJob[];
  /** Pass as `before` for the next page, or `null` at the end. */
  next_before: string | null;
};

/** `PUT /api/v1/hosts/{host_id}/update-policy`. */
export type UpdatePolicyRequest = {
  policy: 'manual' | 'notify' | 'patch' | 'minor' | 'pinned';
  channel: string;
  pinned_version?: string | null;
  maintenance_window?: {
    timezone: string;
    weekdays: number[];
    start: string;
    end: string;
  } | null;
};

/** `POST /api/v1/hosts/{host_id}/updates`. */
export type StartUpdateRequest = {
  target_version?: string | null;
  allow_downgrade?: boolean;
};
