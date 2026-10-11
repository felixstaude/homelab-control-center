import type { ReactNode } from 'react';

/**
 * Page title block.
 *
 * Renders an `<h2>`: the shell header already owns the page `<h1>`, so this
 * keeps a single, correctly ordered heading outline per page.
 */
export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        <h2 className="text-text-primary text-xl font-semibold tracking-tight">{title}</h2>
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
