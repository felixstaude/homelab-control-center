import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

/**
 * Page title block.
 *
 * Renders an `<h2>`: the shell header already owns the page `<h1>`, so this
 * keeps a single, correctly ordered heading outline per page.
 *
 * ## `titleHidden`, and the duplication it resolves
 *
 * The shell's sticky header renders the current section's name as the page
 * `<h1>`, taken from the navigation — it is the breadcrumb that tells you
 * where you are, and it stays visible as you scroll. A page whose
 * `PageHeader` title repeats that name therefore shows the same word twice,
 * eight pixels apart, which is what the Overview page did.
 *
 * The fix is not to delete the heading: it carries the `<h2>` that the page's
 * sections sit beneath, and removing it would leave the content with no
 * heading of its own and the description orphaned. So the title can be
 * rendered **visually hidden** instead — the outline and the accessible name
 * are unchanged, and only the visible duplicate goes.
 *
 * Opt-in rather than automatic. A page whose title genuinely differs from its
 * navigation label should show both, and deriving "are these the same?" here
 * would couple this component to the navigation table.
 */
export function PageHeader({
  title,
  titleHidden = false,
  description,
  actions,
}: {
  title: string;
  /** Keep the heading in the outline but not on screen. */
  titleHidden?: boolean;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <div
      className={cn(
        'flex flex-wrap items-end justify-between gap-4',
        // Without a visible title the block is just the description, so it
        // needs less space beneath it.
        titleHidden ? 'mb-4' : 'mb-6',
      )}
    >
      <div className="min-w-0">
        <h2
          className={cn(
            titleHidden
              ? 'sr-only'
              : 'text-text-primary text-xl font-semibold tracking-tight',
          )}
        >
          {title}
        </h2>
        {description ? (
          <p className="text-text-muted mt-1.5 max-w-2xl text-sm leading-relaxed">
            {description}
          </p>
        ) : null}
      </div>
      {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
    </div>
  );
}
