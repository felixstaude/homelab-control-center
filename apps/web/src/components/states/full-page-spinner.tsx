import { Spinner } from '@/components/ui/spinner';

/**
 * Full-viewport busy state.
 *
 * Used while authentication resolves, which is the one moment the app cannot
 * yet decide what to show. It renders the brand surface rather than a blank
 * page so a slow first load looks like the application starting rather than
 * like a failure.
 *
 * `role="status"` with `aria-live="polite"` announces the label once, without
 * interrupting whatever a screen-reader user was doing.
 */
export function FullPageSpinner({ label }: { label: string }) {
  return (
    <div
      className="bg-surface-base flex min-h-dvh items-center justify-center"
      role="status"
      aria-live="polite"
    >
      <div className="text-text-muted flex items-center gap-3 text-sm">
        <Spinner />
        <span>{label}</span>
      </div>
    </div>
  );
}
