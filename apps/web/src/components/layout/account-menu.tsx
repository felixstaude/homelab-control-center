/**
 * Account menu, in the sidebar footer.
 *
 * ## Why this is hand-rolled and not a Radix dropdown
 *
 * The project already depends on Radix for the dialog and tooltip, so a
 * dropdown would be consistent — but it would also be a new dependency for a
 * menu with one action. This is a disclosure widget: a button that reveals a
 * panel, with Escape to close, click-outside to dismiss, and focus returned to
 * the trigger. That is small enough to own, and owning it keeps the dependency
 * list honest.
 *
 * What is *not* hand-rolled: the roving `role="menu"` keyboard model, which is
 * genuinely easy to get wrong. With a single action there is nothing to rove
 * between, so the panel is a plain region containing a button, and Tab moves
 * through it the way a user expects. Should a second action ever be added, a
 * real menu primitive becomes the right call.
 *
 * ## Placement
 *
 * The sidebar footer, replacing the static phase note. The brief asks not to
 * expand the sidebar or redesign the navigation, and the footer already
 * existed as a block of secondary text.
 */

import { ChevronUp, LogOut, UserRound } from 'lucide-react';
import { useEffect, useId, useRef, useState } from 'react';

import { describeApiError } from '@/api/errors';
import { ROLE_DESCRIPTIONS, ROLE_LABELS } from '@/auth/permissions';
import { useAuth } from '@/auth/use-auth';
import { Button } from '@/components/ui/button';
import { Spinner } from '@/components/ui/spinner';
import { cn } from '@/lib/utils';

export function AccountMenu() {
  const { state, signOut, isSigningOut } = useAuth();
  const [open, setOpen] = useState(false);
  const [signOutError, setSignOutError] = useState<string | null>(null);

  const containerRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelId = useId();

  // Escape closes and returns focus to the trigger, so a keyboard user is not
  // stranded inside a dismissed panel.
  useEffect(() => {
    if (!open) return;

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      setOpen(false);
      triggerRef.current?.focus();
    };
    const onPointerDown = (event: MouseEvent) => {
      if (containerRef.current?.contains(event.target as Node)) return;
      setOpen(false);
    };

    document.addEventListener('keydown', onKeyDown);
    document.addEventListener('mousedown', onPointerDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      document.removeEventListener('mousedown', onPointerDown);
    };
  }, [open]);

  if (state.status !== 'authenticated') return null;

  const { user, role } = state;

  const handleSignOut = async () => {
    setSignOutError(null);
    try {
      await signOut();
      // No navigation here. The auth state becomes `unauthenticated`, and the
      // route guard redirects — one mechanism rather than two that could
      // disagree.
    } catch (error) {
      // Server-side revocation is the boundary, so a failure must be shown
      // rather than swallowed: claiming the session was revoked when it was
      // not is the one thing logout must never do.
      setSignOutError(describeApiError(error));
    }
  };

  return (
    <div ref={containerRef} className="relative">
      {open ? (
        <div
          id={panelId}
          className={cn(
            'bg-surface-overlay border-border-default shadow-overlay',
            'absolute bottom-full left-0 z-40 mb-2 w-full rounded-lg border p-1.5',
          )}
        >
          <div className="px-2.5 py-2">
            <p className="text-text-primary truncate text-xs font-medium">{user.username}</p>
            <p className="text-text-faint mt-0.5 text-[11px]">{ROLE_DESCRIPTIONS[role]}</p>
          </div>

          <div className="bg-border-subtle mx-1 my-1 h-px" aria-hidden="true" />

          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              void handleSignOut();
            }}
            disabled={isSigningOut}
            className="w-full justify-start"
          >
            {isSigningOut ? <Spinner /> : <LogOut />}
            {isSigningOut ? 'Signing out…' : 'Sign out'}
          </Button>

          {signOutError ? (
            <p role="alert" className="text-status-critical px-2.5 py-1.5 text-[11px]">
              {signOutError}
            </p>
          ) : null}
        </div>
      ) : null}

      <button
        ref={triggerRef}
        type="button"
        onClick={() => {
          setOpen((current) => !current);
        }}
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        className={cn(
          'hover:bg-surface-raised flex w-full items-center gap-2.5 rounded-lg px-2 py-2',
          'transition-colors duration-150',
          'outline-none focus-visible:outline-2 focus-visible:outline-offset-2',
          'focus-visible:outline-(--color-focus-ring)',
        )}
      >
        <span
          className={cn(
            'bg-surface-raised border-border-subtle text-text-muted',
            'flex size-7 shrink-0 items-center justify-center rounded-md border',
          )}
          aria-hidden="true"
        >
          <UserRound className="size-3.5" />
        </span>
        <span className="min-w-0 flex-1 text-left leading-tight">
          <span className="text-text-secondary block truncate text-xs font-medium">
            {user.username}
          </span>
          <span className="text-text-faint block truncate text-[11px]">
            {ROLE_LABELS[role]}
          </span>
        </span>
        <ChevronUp
          className={cn(
            'text-text-faint size-3.5 shrink-0 transition-transform duration-150',
            open ? '' : 'rotate-180',
          )}
          aria-hidden="true"
        />
      </button>
    </div>
  );
}
