/**
 * Role helpers.
 *
 * The backend is the authorization boundary; nothing here is a security
 * control. These exist so that components ask one question — "may this user do
 * X?" — in one place, rather than scattering `role === 'admin'` comparisons
 * that drift as the role model grows.
 *
 * ## Hiding a control is not protecting it
 *
 * Every helper below decides what the *interface* offers. The server decides
 * what actually happens, and will answer `403 insufficient_permissions`
 * regardless of what the client rendered (`docs/SECURITY.md`). A frontend
 * check that disagreed with the backend would be a UX bug, not a hole.
 *
 * ## Roles are hierarchical
 *
 * `viewer < operator < admin`, mirroring `hcc/security/authorization.py`. The
 * ordering is the backend's design, not a coincidence of the current matrix:
 * granularity is meant to come from the per-host capability axis (ADR-023), so
 * a minimum-role test is the right primitive here too.
 */

import type { UserRole } from '@/api/types';

/** Position in the ordering. Higher admits everything lower admits. */
const ROLE_RANK: Record<UserRole, number> = {
  viewer: 0,
  operator: 1,
  admin: 2,
};

/** How each role is written in the interface. */
export const ROLE_LABELS: Record<UserRole, string> = {
  viewer: 'Viewer',
  operator: 'Operator',
  admin: 'Administrator',
};

/** One-line description of what each role may do, for an account menu. */
export const ROLE_DESCRIPTIONS: Record<UserRole, string> = {
  viewer: 'Read-only access to monitoring data.',
  operator: 'Read access plus permitted operational actions.',
  admin: 'Full access, including configuration and user management.',
};

/**
 * Coerce a server-supplied role string into a known role.
 *
 * Unknown values become `viewer` — the least privilege.
 *
 * The type says this cannot happen, and at runtime it can: the value crosses
 * the network. The backend's own `role_rank` raises rather than defaulting,
 * because there a silent default would *grant* access by treating an unranked
 * role as Viewer. Here the direction is reversed — the client only decides
 * what to render — so defaulting to the least privilege shows the smallest
 * interface rather than the largest, and the server refuses anything the user
 * should not have reached anyway.
 */
export function normalizeRole(value: string): UserRole {
  return value in ROLE_RANK ? (value as UserRole) : 'viewer';
}

/** Whether `role` is at least `minimum` in the ordering. */
export function hasAtLeastRole(role: UserRole, minimum: UserRole): boolean {
  return ROLE_RANK[role] >= ROLE_RANK[minimum];
}

/**
 * What the interface may offer, derived from one role.
 *
 * A named set rather than scattered role comparisons, so that adding a
 * capability means adding a field here and the call sites keep reading as
 * intent ("may configure hosts") instead of as a role check.
 */
export type Permissions = {
  /** Read monitoring data. Every authenticated role may. */
  readonly viewMonitoring: boolean;
  /** Start, stop or restart things on a host. Operator and above. */
  readonly controlHosts: boolean;
  /**
   * Change module, schedule and retention configuration.
   *
   * **Administrator only**, which is narrower than the designed model in
   * `docs/SECURITY.md` — and matches what the server actually enforces: every
   * monitoring mutation is mounted on the administrator router
   * (`hcc/api/v1/router.py`). The reason it is not an Operator action is
   * specific rather than conservative: enabling a module starts reading a
   * machine, and someone who could do that could begin collecting data about
   * a host without an administrator ever being involved.
   *
   * Set to Operator here it would be a UX bug with a confusing shape — the
   * interface would offer a control that answers 403 — so the flag tracks the
   * enforcement, and widening it means widening the server first.
   */
  readonly configureMonitoring: boolean;
  /** Manage users, agents and global settings. Administrator only. */
  readonly administer: boolean;
};

/**
 * Derive the permission set for a role.
 *
 * Mirrors the matrix in `docs/SECURITY.md`. Per-host capabilities are a second
 * axis the backend evaluates and the client cannot: a host with
 * `docker.control` off refuses an Operator regardless of role, which is why
 * these are "what the interface may offer", not "what will succeed".
 */
export function permissionsFor(role: UserRole): Permissions {
  return {
    viewMonitoring: true,
    controlHosts: hasAtLeastRole(role, 'operator'),
    // Administrator, tracking the server rather than the designed model. See
    // the field's own note for why that is not merely cautious.
    configureMonitoring: hasAtLeastRole(role, 'admin'),
    administer: hasAtLeastRole(role, 'admin'),
  };
}

/**
 * Why an action is unavailable, for a tooltip or a disabled control.
 *
 * Explaining beats hiding: a control that silently vanishes leaves the user
 * wondering whether the feature exists at all.
 */
export function explainMissingRole(required: UserRole, actual: UserRole): string {
  return `Requires the ${ROLE_LABELS[required]} role. You are signed in as ${ROLE_LABELS[actual]}.`;
}
