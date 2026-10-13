/**
 * Monitoring configuration calls.
 *
 * Thin named wrappers over {@link apiRequest}, like `api/hosts.ts`.
 *
 * ## Reads are open, writes are administrator-only
 *
 * That split is the server's and is enforced there
 * (`apps/server/hcc/api/v1/monitoring.py`). A viewer who cannot see which
 * modules are collecting cannot interpret a chart; an operator who could
 * enable collection could start gathering data about somebody's machine
 * without an administrator.
 *
 * ## The three-valued settings are not a quirk to smooth over
 *
 * {@link setHostOverride} and {@link setGlobalDefault} take a body whose keys
 * are *optional and nullable*, and the distinction is load-bearing:
 *
 * - key present with `true`/`false` — set this layer
 * - key present with `null` — make this layer inherit the one above
 * - **key absent** — leave this layer exactly as it is
 *
 * So these functions pass the caller's object through untouched. Filling in a
 * missing `schedule` with `null` to make the type tidier would silently clear
 * a host's schedule override every time somebody toggled it on or off.
 */

import { apiRequest } from '@/api/client';
import type {
  GlobalDefaults,
  HostMonitoring,
  HostSettingSaved,
  ModuleCatalogue,
  ModuleSettingRequest,
} from '@/api/types';

/**
 * The module catalogue: what exists, what each costs, and the interval floors.
 *
 * Fetched rather than hard-coded in the client even though the set is closed,
 * because the floors and the permitted schedule kinds are the server's rules
 * and an interface that guessed them would offer controls that save with a 422.
 */
export function fetchModuleCatalogue(signal?: AbortSignal): Promise<ModuleCatalogue> {
  return apiRequest<ModuleCatalogue>('/monitoring/modules', { signal });
}

/** Every global default, including modules nothing has been said about. */
export function fetchGlobalDefaults(signal?: AbortSignal): Promise<GlobalDefaults> {
  return apiRequest<GlobalDefaults>('/monitoring/defaults', { signal });
}

/**
 * One host's effective configuration, its provenance, and its sync state.
 *
 * The sync state is the part an interface must not drop: a saved
 * configuration is not an active one, and this response is what distinguishes
 * them.
 */
export function fetchHostMonitoring(
  hostId: string,
  signal?: AbortSignal,
): Promise<HostMonitoring> {
  return apiRequest<HostMonitoring>(
    `/hosts/${encodeURIComponent(hostId)}/monitoring`,
    { signal },
  );
}

/**
 * Set or clear one module's global default.
 *
 * Applies to every host that has no override of its own, so this can change
 * what many machines are collecting in one request.
 */
export function setGlobalDefault(
  moduleId: string,
  body: ModuleSettingRequest,
): Promise<GlobalDefaults> {
  return apiRequest<GlobalDefaults>(
    `/monitoring/defaults/${encodeURIComponent(moduleId)}`,
    { method: 'PUT', body },
  );
}

/** Set or clear one module's override on one host. */
export function setHostOverride(
  hostId: string,
  moduleId: string,
  body: ModuleSettingRequest,
): Promise<HostSettingSaved> {
  return apiRequest<HostSettingSaved>(
    `/hosts/${encodeURIComponent(hostId)}/monitoring/modules/${encodeURIComponent(moduleId)}`,
    { method: 'PUT', body },
  );
}

/**
 * Remove a host's override entirely, so the module inherits the global again.
 *
 * Distinct from setting `enabled: null`: this drops the whole override row,
 * schedule included. The interface offers it as "inherit" because that is what
 * it means, not as "delete".
 */
export function resetHostOverride(
  hostId: string,
  moduleId: string,
): Promise<HostSettingSaved> {
  return apiRequest<HostSettingSaved>(
    `/hosts/${encodeURIComponent(hostId)}/monitoring/modules/${encodeURIComponent(moduleId)}`,
    { method: 'DELETE' },
  );
}
