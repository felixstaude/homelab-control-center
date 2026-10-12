import type { ComponentProps } from 'react';

import { cn } from '@/lib/utils';

/**
 * Text input.
 *
 * The design system had no form controls before milestone 1.6 — there were no
 * forms. This is deliberately the minimum: one control, reading the same
 * surface, border, radius and focus tokens as every other interactive element,
 * so a login field looks like it belongs to the console rather than to a
 * generic template.
 *
 * `aria-invalid` drives the error styling rather than a separate prop, so the
 * visual state and the state announced to a screen reader cannot disagree.
 */
export function Input({ className, type = 'text', ...props }: ComponentProps<'input'>) {
  return (
    <input
      type={type}
      className={cn(
        'bg-surface-sunken border-border-default text-text-primary w-full rounded-md border',
        'h-9 px-3 text-sm',
        'placeholder:text-text-faint',
        'transition-colors duration-150 ease-(--ease-out-quint)',
        'hover:border-border-strong',
        'outline-none focus-visible:outline-2 focus-visible:outline-offset-2',
        'focus-visible:outline-(--color-focus-ring)',
        'disabled:cursor-not-allowed disabled:opacity-45',
        // Autofill on a dark theme otherwise paints a light background the
        // palette never intended.
        '[&:-webkit-autofill]:[-webkit-text-fill-color:var(--color-text-primary)]',
        '[&:-webkit-autofill]:[transition:background-color_9999s_ease-in-out_0s]',
        'aria-invalid:border-status-critical-border aria-invalid:focus-visible:outline-(--color-status-critical)',
        className,
      )}
      {...props}
    />
  );
}
