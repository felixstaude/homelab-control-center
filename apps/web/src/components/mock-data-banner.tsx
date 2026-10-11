import { FlaskConical } from 'lucide-react';

import { cn } from '@/lib/utils';

/**
 * Banner marking a region of the UI as static sample data.
 *
 * Engineering rules 8 and 9: mock data must be unmistakably identified and
 * must not pretend to work. This banner is intentionally prominent and
 * non-dismissible — a dismissible notice would leave fabricated numbers
 * looking like live telemetry.
 *
 * Every component rendering values from `src/mocks` must sit beneath one.
 */
export function MockDataBanner({ className }: { className?: string }) {
  return (
    <div
      role="note"
      className={cn(
        'bg-status-warn-bg border-status-warn-border flex items-start gap-3 rounded-lg border px-4 py-3',
        className,
      )}
    >
      <FlaskConical className="text-status-warn mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <div className="min-w-0 text-xs leading-relaxed">
        <p className="text-status-warn font-semibold">
          Sample data — not real measurements
        </p>
        <p className="text-text-muted mt-1">
          Everything below is a hard-coded fixture used to demonstrate the
          intended layout and visual design. Nothing here is fetched from an
          API, and no host, container or metric shown actually exists. Real
          data arrives when agent ingestion lands in Phase 2.
        </p>
      </div>
    </div>
  );
}
