import type { ComponentProps } from 'react';

import { cn } from '@/lib/utils';

/**
 * Data table parts.
 *
 * Extracted from the Overview page's host table, which had the styling inline
 * because it was the only table in the app. The user list is the second, and
 * a second copy of the same classes is how two tables in one product start
 * looking like two products.
 *
 * Plain semantic elements with classes, not a headless table library: these
 * tables are rendered from already-sorted, already-paginated server responses,
 * so there is no client-side sorting, grouping or virtualisation for a library
 * to own. What matters is that the markup stays `<table>` — a grid of `<div>`s
 * loses row and column association for a screen reader, which is the whole
 * value of a table.
 */

export function Table({ className, ...props }: ComponentProps<'table'>) {
  return (
    // Horizontal scroll on the wrapper rather than the table, so narrow
    // viewports scroll the columns instead of squeezing them unreadably.
    <div className="overflow-x-auto">
      <table className={cn('w-full text-sm', className)} {...props} />
    </div>
  );
}

export function TableHead({ className, ...props }: ComponentProps<'thead'>) {
  return <thead className={cn('', className)} {...props} />;
}

export function TableHeaderRow({ className, ...props }: ComponentProps<'tr'>) {
  return <tr className={cn('border-border-subtle border-y', className)} {...props} />;
}

export function TableHeader({ className, ...props }: ComponentProps<'th'>) {
  return (
    <th
      // `scope` defaults to `col`, which is right for every header in these
      // tables; a row header passes `scope="row"` explicitly.
      scope="col"
      className={cn(
        'text-text-faint px-3 py-2.5 text-left text-xs font-medium tracking-wide uppercase',
        'first:pl-5 last:pr-5',
        className,
      )}
      {...props}
    />
  );
}

export function TableBody({ className, ...props }: ComponentProps<'tbody'>) {
  return <tbody className={cn('divide-border-subtle divide-y', className)} {...props} />;
}

export function TableRow({ className, ...props }: ComponentProps<'tr'>) {
  return (
    <tr className={cn('hover:bg-surface-hover/40 transition-colors', className)} {...props} />
  );
}

export function TableCell({ className, ...props }: ComponentProps<'td'>) {
  return <td className={cn('px-3 py-3 first:pl-5 last:pr-5', className)} {...props} />;
}

/** A row's identifying cell. A `<th scope="row">`, so it labels the row. */
export function TableRowHeader({ className, ...props }: ComponentProps<'th'>) {
  return (
    <th
      scope="row"
      className={cn('px-3 py-3 text-left font-normal first:pl-5 last:pr-5', className)}
      {...props}
    />
  );
}
