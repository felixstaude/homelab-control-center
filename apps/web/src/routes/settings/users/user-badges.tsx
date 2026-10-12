import { ROLE_LABELS } from '@/auth/permissions';
import { Badge } from '@/components/ui/badge';
import type { UserRole } from '@/api/types';

/**
 * Role and status chips.
 *
 * Shared by the table and the edit dialog, so a role reads the same in both.
 */

/**
 * Colour by privilege, not arbitrarily.
 *
 * Administrator uses the accent; Operator is neutral; Viewer is the quietest
 * available. The point is that the highest-privilege accounts are the ones an
 * operator scanning the list should notice first — a palette that made every
 * role equally loud would hide the thing worth seeing.
 *
 * Status critically *not* red for "inactive": a deactivated account is a
 * normal administrative state, not a fault, and red would read as an alarm
 * every time someone leaves the household.
 */
const ROLE_VARIANT: Record<UserRole, 'accent' | 'neutral' | 'unknown'> = {
  admin: 'accent',
  operator: 'neutral',
  viewer: 'unknown',
};

export function RoleBadge({ role }: { role: UserRole }) {
  return <Badge variant={ROLE_VARIANT[role]}>{ROLE_LABELS[role]}</Badge>;
}

export function StatusBadge({ isActive }: { isActive: boolean }) {
  return (
    <Badge variant={isActive ? 'ok' : 'unknown'}>{isActive ? 'Active' : 'Inactive'}</Badge>
  );
}

/**
 * Live session count.
 *
 * Zero is rendered as a dash rather than "0 sessions": a count of zero is the
 * normal resting state for most accounts, and a column of zeroes draws the eye
 * away from the rows that do have sessions.
 */
export function SessionCount({ count }: { count: number }) {
  if (count === 0) {
    return (
      <span className="text-text-faint text-xs" title="No active sessions">
        —
      </span>
    );
  }
  return (
    <span className="tabular text-text-secondary text-xs">
      {count}
      <span className="sr-only"> active {count === 1 ? 'session' : 'sessions'}</span>
    </span>
  );
}
