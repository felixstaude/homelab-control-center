import { cva, type VariantProps } from 'class-variance-authority';
import type { ComponentProps } from 'react';

import { cn } from '@/lib/utils';

const badgeVariants = cva(
  'inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-xs font-medium whitespace-nowrap',
  {
    variants: {
      variant: {
        neutral: 'bg-surface-overlay border-border-default text-text-secondary',
        ok: 'bg-status-ok-bg border-status-ok-border text-status-ok',
        warn: 'bg-status-warn-bg border-status-warn-border text-status-warn',
        critical: 'bg-status-critical-bg border-status-critical-border text-status-critical',
        info: 'bg-status-info-bg border-status-info-border text-status-info',
        unknown: 'bg-status-unknown-bg border-status-unknown-border text-status-unknown',
        accent: 'bg-accent-subtle border-accent-muted text-accent',
      },
    },
    defaultVariants: {
      variant: 'neutral',
    },
  },
);

export type BadgeProps = ComponentProps<'span'> & VariantProps<typeof badgeVariants>;

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export { badgeVariants };
