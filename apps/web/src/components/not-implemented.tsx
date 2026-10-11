import type { LucideIcon } from 'lucide-react';

import { Card } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';

export type NotImplementedProps = {
  icon: LucideIcon;
  /** The feature area, e.g. "Docker management". */
  title: string;
  /** What this section will do once built. */
  summary: string;
  /** Concrete capabilities planned, in delivery order. */
  planned: readonly string[];
  /** Roadmap milestone that delivers it, e.g. "Phase 3". */
  milestone: string;
};

/**
 * Honest placeholder for a section that does not exist yet.
 *
 * Engineering rule 8: no fake implementations. Rather than render an empty
 * table that looks broken, or mock rows that look real, each unbuilt section
 * states plainly what it will do and which milestone delivers it.
 */
export function NotImplemented({
  icon: Icon,
  title,
  summary,
  planned,
  milestone,
}: NotImplementedProps) {
  return (
    <Card className="overflow-hidden">
      <div className="bg-grid border-border-subtle border-b px-6 py-10">
        <div className="mx-auto flex max-w-xl flex-col items-center text-center">
          <div className="bg-surface-overlay border-border-default text-text-muted mb-4 flex size-11 items-center justify-center rounded-xl border">
            <Icon className="size-5" aria-hidden="true" />
          </div>
          <h3 className="text-text-primary text-base font-semibold">{title}</h3>
          <p className="text-text-muted mt-2 text-sm leading-relaxed">{summary}</p>
          <Badge variant="accent" className="mt-4">
            Planned for {milestone}
          </Badge>
        </div>
      </div>

      <div className="px-6 py-5">
        <h4 className="text-text-faint mb-3 text-xs font-medium tracking-wide uppercase">
          Planned capabilities
        </h4>
        <ul className="grid gap-2 sm:grid-cols-2">
          {planned.map((capability) => (
            <li key={capability} className="flex items-start gap-2.5 text-sm">
              <span
                className="bg-border-strong mt-[0.4375rem] size-1.5 shrink-0 rounded-full"
                aria-hidden="true"
              />
              <span className="text-text-secondary leading-snug">{capability}</span>
            </li>
          ))}
        </ul>
      </div>
    </Card>
  );
}
