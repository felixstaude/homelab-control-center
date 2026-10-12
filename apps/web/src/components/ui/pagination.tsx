import { ChevronLeft, ChevronRight } from 'lucide-react';

import { Button } from '@/components/ui/button';

/**
 * Offset pager, for the user list.
 *
 * Deliberately previous/next with a stated range rather than numbered pages.
 * Numbered pages promise a stable mapping from number to contents, which
 * offset pagination over a table that is being written to cannot keep: an
 * account created while the operator reads page 1 shifts everything after it.
 * "26–50 of 51" says what is on screen without implying page 2 will contain
 * the same rows in a minute.
 *
 * The audit log uses a cursor instead, for the same reason stated from the
 * other direction — see {@link CursorPagination}.
 */
export function Pagination({
  total,
  limit,
  offset,
  onOffsetChange,
  busy = false,
  label,
}: {
  total: number;
  limit: number;
  offset: number;
  onOffsetChange: (offset: number) => void;
  /** Disables navigation while a fetch is in flight. */
  busy?: boolean;
  /** Plural noun for the range summary, e.g. "accounts". */
  label: string;
}) {
  // `total` can be zero, and `offset` can legitimately exceed it after a
  // deletion, so the displayed range is clamped rather than computed naively —
  // "showing 26–25 of 0" is the kind of output that makes a screen look broken.
  const first = total === 0 ? 0 : Math.min(offset + 1, total);
  const last = Math.min(offset + limit, total);
  const hasPrevious = offset > 0;
  const hasNext = offset + limit < total;

  return (
    <div className="border-border-subtle flex flex-wrap items-center justify-between gap-3 border-t px-5 py-3">
      <p className="text-text-muted text-xs" role="status">
        {total === 0 ? (
          <>No {label}</>
        ) : (
          <>
            Showing <span className="tabular text-text-secondary">{first}</span>–
            <span className="tabular text-text-secondary">{last}</span> of{' '}
            <span className="tabular text-text-secondary">{total}</span> {label}
          </>
        )}
      </p>

      <div className="flex items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          disabled={!hasPrevious || busy}
          onClick={() => {
            onOffsetChange(Math.max(0, offset - limit));
          }}
        >
          <ChevronLeft />
          Previous
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={!hasNext || busy}
          onClick={() => {
            onOffsetChange(offset + limit);
          }}
        >
          Next
          <ChevronRight />
        </Button>
      </div>
    </div>
  );
}

/**
 * Keyset pager, for the audit log.
 *
 * Forward-only, and that is not a shortcut: a cursor identifies a position in
 * the ordering, and going back needs the cursor of the *previous* page, which
 * the server does not hand out. The caller keeps the cursors it has already
 * used, so "previous" is navigation through that history rather than a query.
 *
 * No total-based range here. An append-only log's count moves while it is
 * being read, so "showing 51–100 of 4,312" would be stale the moment it
 * rendered; the count is reported separately as "4,312 events" where its
 * approximate nature does no harm.
 */
export function CursorPagination({
  hasPrevious,
  hasNext,
  onPrevious,
  onNext,
  busy = false,
  summary,
}: {
  hasPrevious: boolean;
  hasNext: boolean;
  onPrevious: () => void;
  onNext: () => void;
  busy?: boolean;
  summary: string;
}) {
  return (
    <div className="border-border-subtle flex flex-wrap items-center justify-between gap-3 border-t px-5 py-3">
      <p className="text-text-muted text-xs" role="status">
        {summary}
      </p>
      <div className="flex items-center gap-2">
        <Button variant="outline" size="sm" disabled={!hasPrevious || busy} onClick={onPrevious}>
          <ChevronLeft />
          Previous
        </Button>
        <Button variant="outline" size="sm" disabled={!hasNext || busy} onClick={onNext}>
          Next
          <ChevronRight />
        </Button>
      </div>
    </div>
  );
}
