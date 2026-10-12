import type { Host, HostConnectivity, HostLifecycle } from '@/api/types';
import type { HealthStatus } from '@/components/ui/status-indicator';

/**
 * How a host's state is rendered.
 *
 * ## Three axes, three badges — not one status column
 *
 * The data model keeps enrollment, administrative state and connectivity
 * separate, and the interface has to as well. A single "status" column would
 * have to choose between them, and every choice is wrong somewhere: a disabled
 * host whose agent is still attached is *both* disabled and connected, and an
 * operator needs to see that disabling it has not taken effect yet rather than
 * reading one word that hides half of it.
 *
 * ## Mapping connectivity onto the health vocabulary
 *
 * `HealthStatus` is the product's four words (`status-indicator.tsx`), used
 * for hosts, containers, devices and alerts alike. Connectivity maps onto it
 * with one deliberate subtlety: **offline is not always critical.**
 *
 * | Host state | Health | Why |
 * | --- | --- | --- |
 * | connected | `ok` | A recent heartbeat on a live connection. |
 * | stale | `warn` | A connection is recorded and the heartbeats stopped. The controller cannot tell whether the agent died or the network did, and `warn` is what "we do not know, and it is not fine" looks like. |
 * | offline, enrolled, enabled | `critical` | The operator wants this host connected and it is not. That is an alarm. |
 * | offline, disabled | `unknown` | Nothing is trying to connect, by the operator's own choice. Reporting `critical` here would be a false alarm, and a product that cries wolf about hosts somebody deliberately switched off is a product whose alarms get ignored. |
 * | offline, never enrolled | `unknown` | No agent has ever been installed. Not a failure — a step not yet taken. |
 *
 * `unknown` being first-class is what makes that table possible. A vocabulary
 * of only ok/warn/critical would force a registered-but-not-yet-enrolled host
 * to be reported as either healthy or broken, and it is neither.
 */

/** Connectivity, as a health status and a label. */
export function hostHealth(host: Host): { status: HealthStatus; label: string } {
  switch (host.connectivity) {
    case 'connected':
      return { status: 'ok', label: 'Connected' };
    case 'stale':
      return { status: 'warn', label: 'Stale' };
    case 'offline':
      if (!host.is_enabled) return { status: 'unknown', label: 'Offline' };
      if (host.lifecycle === 'registered') return { status: 'unknown', label: 'Not enrolled' };
      return { status: 'critical', label: 'Offline' };
  }
}

export const LIFECYCLE_LABELS: Record<HostLifecycle, string> = {
  registered: 'Not enrolled',
  enrolled: 'Enrolled',
  revoked: 'Revoked',
};

export const LIFECYCLE_DESCRIPTIONS: Record<HostLifecycle, string> = {
  registered: 'This host exists in the registry and no agent has ever claimed it.',
  enrolled: 'An agent has claimed this host and holds a live credential.',
  revoked:
    'An agent held a credential for this host and it has been revoked. Re-enrol to restore access.',
};

export const CONNECTIVITY_DESCRIPTIONS: Record<HostConnectivity, string> = {
  connected: 'A heartbeat arrived recently on a live connection.',
  stale:
    'A connection is recorded and the heartbeats stopped. The agent may have died, or the network may have. The controller does not guess.',
  offline: 'No connection is recorded.',
};
