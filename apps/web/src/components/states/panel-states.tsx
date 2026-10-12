import { AlertTriangle, Inbox, Lock, RotateCw, type LucideIcon } from 'lucide-react';
import type { ReactNode } from 'react';

import { describeApiError, isApiError } from '@/api/errors';
import { ROLE_LABELS } from '@/auth/permissions';
import { Alert } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import type { UserRole } from '@/api/types';

/**
 * The four states every data panel has, in one place.
 *
 * Loading, empty, failed and not-permitted. Writing them per panel is how a
 * product ends up with an empty table that looks broken on one screen, a
 * spinner that never resolves on another, and a 403 rendered as "no results"
 * on a third — the last being the one that actually misleads, because "you
 * cannot see this" and "there is nothing here" are completely different facts.
 */

/** Shared frame: an icon, a heading, a line of explanation, and an action. */
function PanelMessage({
  icon: Icon,
  title,
  children,
  action,
  tone = 'neutral',
}: {
  icon: LucideIcon;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
  tone?: 'neutral' | 'critical';
}) {
  return (
    <div className="flex flex-col items-center px-6 py-12 text-center">
      <div
        className={
          tone === 'critical'
            ? 'bg-status-critical-bg border-status-critical-border text-status-critical mb-4 flex size-11 items-center justify-center rounded-xl border'
            : 'bg-surface-overlay border-border-default text-text-muted mb-4 flex size-11 items-center justify-center rounded-xl border'
        }
      >
        <Icon className="size-5" aria-hidden="true" />
      </div>
      <h3 className="text-text-primary text-sm font-semibold">{title}</h3>
      {children ? (
        <p className="text-text-muted mt-2 max-w-md text-sm leading-relaxed">{children}</p>
      ) : null}
      {action ? <div className="mt-5">{action}</div> : null}
    </div>
  );
}

/**
 * Skeleton rows while a table loads.
 *
 * Rows rather than a centred spinner, so the layout does not jump when the
 * data arrives — and `role="status"` with a label, so a screen reader is told
 * something is loading instead of being handed a silent block of empty cells.
 */
export function LoadingRows({ rows = 4, columns = 4 }: { rows?: number; columns?: number }) {
  return (
    <div role="status" aria-label="Loading" className="px-5 py-4">
      <div className="flex flex-col gap-3">
        {Array.from({ length: rows }, (_, row) => (
          <div key={row} className="flex items-center gap-4">
            {Array.from({ length: columns }, (_, column) => (
              <Skeleton
                key={column}
                // Varying widths, so it reads as content rather than as a
                // loading bar someone forgot to remove.
                className={column === 0 ? 'h-4 w-40' : 'h-4 flex-1'}
              />
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

/** Nothing matched, or nothing exists yet. */
export function EmptyPanel({
  title,
  children,
  action,
}: {
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <PanelMessage icon={Inbox} title={title} action={action}>
      {children}
    </PanelMessage>
  );
}

/**
 * A request failed.
 *
 * Shows the server's own message, which is always safe to display — the
 * uniform error envelope never carries internal detail, and the catch-all
 * handler returns an opaque message for anything unexpected
 * (`docs/ARCHITECTURE.md` §3.2). For a 500 the request id is shown too, since
 * that is the one thing that makes an opaque failure traceable in the log.
 */
export function ErrorPanel({
  error,
  onRetry,
  title = 'Could not load this',
}: {
  error: unknown;
  onRetry?: () => void;
  title?: string;
}) {
  const requestId = isApiError(error) ? error.requestId : null;

  return (
    <PanelMessage
      icon={AlertTriangle}
      title={title}
      tone="critical"
      action={
        onRetry ? (
          <Button variant="secondary" size="sm" onClick={onRetry}>
            <RotateCw />
            Try again
          </Button>
        ) : undefined
      }
    >
      {describeApiError(error)}
      {requestId ? (
        <>
          {' '}
          <span className="text-text-faint font-mono text-xs">({requestId})</span>
        </>
      ) : null}
    </PanelMessage>
  );
}

/**
 * The caller's role does not permit this section.
 *
 * Reached by typing the URL rather than by clicking, because the navigation
 * hides administrative sections from other roles. Says which role is needed
 * and which one the user holds — this is the client explaining its own
 * interface to an authenticated person, not the server answering a denial, so
 * there is no enumeration concern. The server's 403 deliberately says nothing
 * of the kind.
 */
export function PermissionRequired({
  required,
  actual,
  what,
}: {
  required: UserRole;
  actual: UserRole;
  what: string;
}) {
  return (
    <PanelMessage icon={Lock} title={`${what} requires the ${ROLE_LABELS[required]} role`}>
      You are signed in as {ROLE_LABELS[actual]}. Ask an administrator if you need access —
      this is enforced by the control plane, so nothing here would work without it.
    </PanelMessage>
  );
}

/**
 * A mutation failed, shown inline above the form or list it belongs to.
 *
 * Separate from {@link ErrorPanel} because the distinction is what the user
 * can do next: a failed read replaces the panel and offers a retry, while a
 * failed write leaves the form on screen with whatever was typed still in it.
 * Replacing a half-filled form with an error screen loses the work.
 */
export function MutationError({ error, title }: { error: unknown; title?: string }) {
  if (!error) return null;

  const rateLimited = isApiError(error) && error.kind === 'rate_limited';
  const retryAfter = isApiError(error) ? error.retryAfterSeconds : null;

  return (
    <Alert
      variant={rateLimited ? 'warn' : 'critical'}
      title={title ?? (rateLimited ? 'Too many attempts' : 'That did not work')}
      className="mb-4"
    >
      {describeApiError(error)}
      {rateLimited && retryAfter !== null ? (
        <> Try again in about {retryAfter} seconds.</>
      ) : null}
    </Alert>
  );
}
