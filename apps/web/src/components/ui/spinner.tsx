import { Loader2 } from 'lucide-react';

import { cn } from '@/lib/utils';

/**
 * Busy indicator.
 *
 * `aria-hidden` and purely decorative: the accessible announcement belongs to
 * whatever is busy — a button's label changing to "Signing in…", or a
 * container with `aria-busy` — not to the spinner. A spinner that announced
 * itself would read as "loading" with no indication of what.
 *
 * The global `prefers-reduced-motion` rule in `index.css` collapses the
 * animation duration, so this stops spinning for users who asked for that
 * without needing a prop here.
 */
export function Spinner({ className }: { className?: string }) {
  return <Loader2 className={cn('size-4 animate-spin', className)} aria-hidden="true" />;
}
