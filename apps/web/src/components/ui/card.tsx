import type { ComponentProps } from 'react';

import { cn } from '@/lib/utils';

/**
 * Card — the primary content container.
 *
 * Composed from parts rather than configured by props, so a card can carry a
 * header with actions, a dense table, or a chart without the component
 * growing a `variant` for each case.
 */
export function Card({ className, ...props }: ComponentProps<'div'>) {
  return (
    <div
      className={cn(
        'bg-surface-raised border-border-subtle surface-lit rounded-xl border',
        className,
      )}
      {...props}
    />
  );
}

export function CardHeader({ className, ...props }: ComponentProps<'div'>) {
  return (
    <div
      className={cn(
        'flex items-start justify-between gap-4 px-5 pt-4 pb-3',
        className,
      )}
      {...props}
    />
  );
}

export function CardTitle({ className, ...props }: ComponentProps<'h3'>) {
  return (
    // Content is supplied by the caller via `children`, which the rule cannot
    // see from this generic wrapper. Call sites always pass visible text.
    // eslint-disable-next-line jsx-a11y/heading-has-content
    <h3
      className={cn('text-text-primary text-sm leading-none font-semibold', className)}
      {...props}
    />
  );
}

export function CardDescription({ className, ...props }: ComponentProps<'p'>) {
  return <p className={cn('text-text-muted mt-1.5 text-xs', className)} {...props} />;
}

export function CardContent({ className, ...props }: ComponentProps<'div'>) {
  return <div className={cn('px-5 pb-5', className)} {...props} />;
}

export function CardFooter({ className, ...props }: ComponentProps<'div'>) {
  return (
    <div
      className={cn(
        'border-border-subtle flex items-center gap-3 border-t px-5 py-3',
        className,
      )}
      {...props}
    />
  );
}
