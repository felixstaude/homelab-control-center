import { Activity } from 'lucide-react';

import { cn } from '@/lib/utils';

/**
 * Product wordmark.
 *
 * An inline icon plus text rather than an image asset: it stays crisp at any
 * size, inherits theme colours, and adds no network request.
 */
export function Brand({ className }: { className?: string }) {
  return (
    <div className={cn('flex items-center gap-2.5', className)}>
      <div
        className={cn(
          'bg-accent-subtle border-accent-muted text-accent',
          'flex size-8 shrink-0 items-center justify-center rounded-lg border',
        )}
      >
        <Activity className="size-4" aria-hidden="true" />
      </div>
      <div className="min-w-0 leading-tight">
        <div className="text-text-primary truncate text-sm font-semibold">
          Homelab Control
        </div>
        <div className="text-text-faint truncate text-[11px]">Center</div>
      </div>
    </div>
  );
}
