import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

export type MetricTileProps = {
  label: string;
  /** Pre-formatted display value. Formatting belongs to the caller. */
  value: string;
  unit?: string;
  /** Short qualifier under the value, e.g. "of 64 GiB". */
  caption?: string;
  icon?: ReactNode;
  /** Optional visual, typically a sparkline. */
  visual?: ReactNode;
  className?: string;
};

/**
 * A single headline metric.
 *
 * Deliberately has no notion of thresholds or trend direction. Deciding that
 * 82% memory is "bad" is a product rule that belongs to the alerting engine
 * (Phase 5), not to a presentational tile — a tile that guessed would show
 * colours the alert rules disagree with.
 */
export function MetricTile({
  label,
  value,
  unit,
  caption,
  icon,
  visual,
  className,
}: MetricTileProps) {
  return (
    <div
      className={cn(
        'bg-surface-raised border-border-subtle surface-lit rounded-xl border p-4',
        'flex flex-col gap-3',
        className,
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-text-muted text-xs font-medium tracking-wide uppercase">
          {label}
        </span>
        {icon ? <span className="text-text-faint">{icon}</span> : null}
      </div>

      <div>
        <div className="flex items-baseline gap-1">
          <span className="tabular text-text-primary text-2xl leading-none font-semibold">
            {value}
          </span>
          {unit ? <span className="text-text-muted text-sm font-medium">{unit}</span> : null}
        </div>
        {caption ? <p className="text-text-faint mt-1.5 text-xs">{caption}</p> : null}
      </div>

      {visual ? <div className="-mb-1 h-10">{visual}</div> : null}
    </div>
  );
}
