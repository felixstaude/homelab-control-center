import { ChevronDown } from 'lucide-react';
import type { ComponentProps } from 'react';

import { cn } from '@/lib/utils';

/**
 * Select control.
 *
 * A styled **native** `<select>`, not a custom listbox. The project already
 * depends on Radix for the dialog and tooltip, so a Radix select would be
 * consistent — and it would also be a new dependency plus a hand-maintained
 * keyboard model for three enumerated values.
 *
 * What the native element gives for free, and what a custom one has to earn
 * back: type-ahead, the platform's own keyboard conventions, correct behaviour
 * with a screen reader, and a usable picker on a phone. The cost is that the
 * open menu is drawn by the operating system and so does not follow the dark
 * palette on every platform. That is a visible inconsistency for a few hundred
 * milliseconds, against an accessibility surface that is easy to get subtly
 * wrong and hard to notice — so the trade goes this way.
 *
 * `appearance-none` plus an explicit chevron, so the closed state does match
 * the console's other controls, which is where the eye actually rests.
 */
export function Select({ className, children, ...props }: ComponentProps<'select'>) {
  return (
    <div className="relative">
      <select
        className={cn(
          'bg-surface-sunken border-border-default text-text-primary w-full rounded-md border',
          'h-9 pr-9 pl-3 text-sm',
          'appearance-none',
          'transition-colors duration-150 ease-(--ease-out-quint)',
          'hover:border-border-strong',
          'outline-none focus-visible:outline-2 focus-visible:outline-offset-2',
          'focus-visible:outline-(--color-focus-ring)',
          'disabled:cursor-not-allowed disabled:opacity-45',
          'aria-invalid:border-status-critical-border',
          className,
        )}
        {...props}
      >
        {children}
      </select>
      <ChevronDown
        // Decorative: the select itself announces its role and value.
        aria-hidden="true"
        className="text-text-faint pointer-events-none absolute top-1/2 right-3 size-4 -translate-y-1/2"
      />
    </div>
  );
}
