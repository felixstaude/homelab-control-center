import { cva, type VariantProps } from 'class-variance-authority';
import type { ComponentProps } from 'react';

import { cn } from '@/lib/utils';

/**
 * The canonical health vocabulary for the whole product.
 *
 * Defined once here so a host, a container, a network device and an alert all
 * use the same four words and the same four colours. `unknown` is explicit
 * and first-class: a monitoring tool must distinguish "this is healthy" from
 * "we have not heard from this", and must never render missing data as OK.
 */
export type HealthStatus = 'ok' | 'warn' | 'critical' | 'unknown';

export const STATUS_LABELS: Record<HealthStatus, string> = {
  ok: 'Healthy',
  warn: 'Degraded',
  critical: 'Critical',
  unknown: 'Unknown',
};

const dotVariants = cva('relative inline-flex shrink-0 rounded-full', {
  variants: {
    status: {
      ok: 'bg-status-ok',
      warn: 'bg-status-warn',
      critical: 'bg-status-critical',
      unknown: 'bg-status-unknown',
    },
    size: {
      sm: 'size-1.5',
      md: 'size-2',
      lg: 'size-2.5',
    },
  },
  defaultVariants: { status: 'unknown', size: 'md' },
});

export type StatusDotProps = Omit<ComponentProps<'span'>, 'children'> &
  VariantProps<typeof dotVariants> & {
    status: HealthStatus;
    /** Emit a soft halo. Reserved for states needing attention. */
    pulse?: boolean;
  };

/**
 * A status dot.
 *
 * Decorative by default (`aria-hidden`): the dot alone conveys meaning only
 * by colour, which fails both colour-blind users and screen readers. Pair it
 * with a text label — `StatusBadge` does exactly that.
 */
export function StatusDot({ className, status, size, pulse = false, ...props }: StatusDotProps) {
  return (
    <span className="relative inline-flex items-center" aria-hidden="true" {...props}>
      {pulse ? (
        <span
          className={cn(
            dotVariants({ status, size }),
            'absolute animate-ping opacity-60',
            className,
          )}
        />
      ) : null}
      <span className={cn(dotVariants({ status, size }), className)} />
    </span>
  );
}

export type StatusBadgeProps = Omit<ComponentProps<'span'>, 'children'> & {
  status: HealthStatus;
  /**
   * Override the default label, e.g. "Running" instead of "Healthy".
   *
   * Explicitly accepts `undefined` so callers can pass a conditional
   * expression directly under `exactOptionalPropertyTypes`.
   */
  label?: string | undefined;
};

const badgeStatusVariants = cva(
  'inline-flex items-center gap-2 rounded-md border px-2 py-0.5 text-xs font-medium whitespace-nowrap',
  {
    variants: {
      status: {
        ok: 'bg-status-ok-bg border-status-ok-border text-status-ok',
        warn: 'bg-status-warn-bg border-status-warn-border text-status-warn',
        critical: 'bg-status-critical-bg border-status-critical-border text-status-critical',
        unknown: 'bg-status-unknown-bg border-status-unknown-border text-status-unknown',
      },
    },
    defaultVariants: { status: 'unknown' },
  },
);

/** A status dot paired with its text label — the accessible default. */
export function StatusBadge({ className, status, label, ...props }: StatusBadgeProps) {
  return (
    <span className={cn(badgeStatusVariants({ status }), className)} {...props}>
      <StatusDot status={status} size="sm" pulse={status === 'critical'} />
      {label ?? STATUS_LABELS[status]}
    </span>
  );
}
