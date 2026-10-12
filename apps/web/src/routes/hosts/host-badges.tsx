import { Badge } from '@/components/ui/badge';
import { StatusBadge } from '@/components/ui/status-indicator';
import type { Host, HostLifecycle } from '@/api/types';
import { LIFECYCLE_LABELS, hostHealth } from '@/routes/hosts/host-status';

/**
 * The badges that render a host's state.
 *
 * Components only. The mapping from connectivity onto the health vocabulary,
 * and the prose that explains each state, live in `host-status.ts` — split out
 * so this file exports nothing but components, which is what lets Vite's fast
 * refresh replace it without reloading the page.
 *
 * ## Three axes, three badges — not one status column
 *
 * The data model keeps enrollment, administrative state and connectivity
 * separate, and the interface has to as well. A single "status" column would
 * have to choose between them, and every choice is wrong somewhere: a disabled
 * host whose agent is still attached is *both* disabled and connected, and an
 * operator needs to see that disabling it has not taken effect yet rather than
 * reading one word that hides half of it.
 */

/** The connectivity badge. */
export function ConnectivityBadge({ host }: { host: Host }) {
  const { status, label } = hostHealth(host);
  return <StatusBadge status={status} label={label} />;
}

/**
 * The enrollment-state badge.
 *
 * Shown as metadata rather than as a status, because enrollment is a fact
 * about the record and not a judgement about its health. `revoked` gets the
 * critical variant: it is the one value that means somebody took access away,
 * which an operator scanning a list should not have to read twice.
 */
export function LifecycleBadge({ lifecycle }: { lifecycle: HostLifecycle }) {
  return (
    <Badge variant={lifecycle === 'revoked' ? 'critical' : 'neutral'}>
      {LIFECYCLE_LABELS[lifecycle]}
    </Badge>
  );
}

/**
 * The "disabled" badge, or nothing.
 *
 * Only rendered when a host is disabled. An "Enabled" badge on every row
 * would be noise on the common case and would make the exception harder to
 * spot, which is the opposite of what a badge is for.
 */
export function DisabledBadge({ isEnabled }: { isEnabled: boolean }) {
  if (isEnabled) return null;
  return <Badge variant="warn">Disabled</Badge>;
}

/**
 * The "enrollment pending" badge, or nothing.
 *
 * Reports that a credential is waiting to be used. It cannot report the
 * credential itself: only its digest is stored, so there is nothing to show
 * and no endpoint that could produce it.
 */
export function PendingEnrollmentBadge({ pending }: { pending: boolean }) {
  if (!pending) return null;
  return <Badge variant="accent">Enrolment pending</Badge>;
}
