import type { ComponentProps } from 'react';

import { cn } from '@/lib/utils';

/**
 * Loading placeholder.
 *
 * `aria-hidden` with a `role="status"` wrapper supplied by the caller: a
 * screen reader should hear "loading", not a description of grey rectangles.
 */
export function Skeleton({ className, ...props }: ComponentProps<'div'>) {
  return (
    <div
      aria-hidden="true"
      className={cn('bg-surface-overlay animate-pulse rounded-md', className)}
      {...props}
    />
  );
}
