import { cva, type VariantProps } from 'class-variance-authority';
import { AlertTriangle, CircleAlert, Info, type LucideIcon } from 'lucide-react';
import type { ComponentProps } from 'react';

import { cn } from '@/lib/utils';

/**
 * Inline message block.
 *
 * Uses the semantic status tokens, so an error here is the same red as a
 * critical host and an informational note is the same blue as an info badge —
 * the console reads as one product rather than one palette per screen.
 *
 * Each variant pairs its colour with an icon. Colour alone fails colour-blind
 * users, which is the same reason `StatusDot` is always rendered beside a text
 * label.
 */
const alertVariants = cva(
  ['flex items-start gap-3 rounded-lg border px-4 py-3 text-sm'],
  {
    variants: {
      variant: {
        info: 'bg-status-info-bg border-status-info-border text-status-info',
        warn: 'bg-status-warn-bg border-status-warn-border text-status-warn',
        critical:
          'bg-status-critical-bg border-status-critical-border text-status-critical',
      },
    },
    defaultVariants: { variant: 'info' },
  },
);

const VARIANT_ICONS: Record<'info' | 'warn' | 'critical', LucideIcon> = {
  info: Info,
  warn: AlertTriangle,
  critical: CircleAlert,
};

export type AlertProps = ComponentProps<'div'> &
  VariantProps<typeof alertVariants> & {
    /** Short heading. The body is `children`. */
    title?: string | undefined;
  };

export function Alert({ className, variant = 'info', title, children, ...props }: AlertProps) {
  const Icon = VARIANT_ICONS[variant ?? 'info'];

  return (
    <div className={cn(alertVariants({ variant }), className)} {...props}>
      <Icon className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        {title ? <p className="font-semibold">{title}</p> : null}
        {children ? (
          // The body is deliberately a softer colour than the heading: the
          // variant hue carries the severity, and a whole paragraph in it is
          // hard to read on a dark surface.
          <div className={cn('text-text-secondary', title ? 'mt-1' : '')}>{children}</div>
        ) : null}
      </div>
    </div>
  );
}
