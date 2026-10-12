import { cva, type VariantProps } from 'class-variance-authority';
import type { ReactNode } from 'react';

import { Brand } from '@/components/layout/brand';
import { Card, CardContent } from '@/components/ui/card';
import { cn } from '@/lib/utils';

/**
 * The chrome shared by every screen shown outside the console shell.
 *
 * Login, first-run setup and the backend-unavailable screen are each a single
 * centred card with the brand above it on the faint instrument grid. All three
 * had their own copy of that markup, and they had already drifted: the
 * unavailable screen was missing the background layer, which nothing would
 * have caught because no test asserts on decoration. Three copies also meant
 * the next full-page screen would be a fourth.
 *
 * The width is the only thing they legitimately differ in — instructions need
 * more room than a two-field form — so that is the one variant.
 */
const panel = cva('relative w-full', {
  variants: {
    width: {
      /** A form. Two fields and a button. */
      sm: 'max-w-sm',
      /** A short message with one action. */
      md: 'max-w-md',
      /** Prose, a command to copy, and a list of steps. */
      lg: 'max-w-xl',
    },
  },
  defaultVariants: { width: 'sm' },
});

export function FullPagePanel({
  width,
  className,
  footer,
  children,
}: {
  className?: string;
  /** Fine print below the card. Rendered only when given. */
  footer?: ReactNode;
  children: ReactNode;
} & VariantProps<typeof panel>) {
  return (
    <div className="bg-surface-base flex min-h-dvh flex-col items-center justify-center px-4 py-10">
      <div className="bg-grid pointer-events-none fixed inset-0 opacity-60" aria-hidden="true" />

      <div className={cn(panel({ width }), className)}>
        <div className="mb-6 flex justify-center">
          <Brand />
        </div>

        <Card>
          <CardContent className="pt-5">{children}</CardContent>
        </Card>

        {footer === undefined ? null : (
          // `text-muted` and `text-xs`, up from `text-faint` at 11px. The
          // faint token is intended for de-emphasised text *beside* something
          // brighter; as the only thing on the background it was dim enough to
          // be easy to miss entirely, which for a product name is the wrong
          // outcome. Still clearly secondary to the card.
          <p className="text-text-muted mt-5 text-center text-xs leading-relaxed">{footer}</p>
        )}
      </div>
    </div>
  );
}
