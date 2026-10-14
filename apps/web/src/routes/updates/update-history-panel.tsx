import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';

import { fetchUpdateHistory } from '@/api/updates';
import { EmptyPanel, ErrorPanel, LoadingRows } from '@/components/states/panel-states';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableHeaderRow,
  TableRow,
  TableRowHeader,
} from '@/components/ui/table';
import { formatDateTime, machineDateTime } from '@/lib/datetime';
import { queryKeys } from '@/lib/query-keys';
import { jobState } from '@/routes/updates/update-status';

/** How many attempts one page shows. */
const PAGE_SIZE = 20;

/**
 * What the fleet actually did.
 *
 * ## Keyset pagination, not an offset
 *
 * A fleet's update history grows without bound, and `OFFSET 10000` makes the
 * database walk and discard ten thousand rows. The cursor is the oldest row's
 * timestamp, which also means a page cannot duplicate or hide a row because
 * another update finished while somebody was reading.
 *
 * ## The failure reason is shown in the row
 *
 * Not behind a click. "Why did this fail" is the only question anybody asks of
 * a history, and hiding the answer one interaction away makes the table
 * decorative.
 */
export function UpdateHistoryPanel({ hostId }: { hostId?: string } = {}) {
  const [cursors, setCursors] = useState<string[]>([]);
  const before = cursors.at(-1) ?? '';

  const history = useQuery({
    queryKey: queryKeys.session.updateHistory({
      hostId: hostId ?? '',
      before,
      limit: PAGE_SIZE,
    }),
    queryFn: ({ signal }) =>
      fetchUpdateHistory(
        {
          ...(hostId === undefined ? {} : { hostId }),
          ...(before === '' ? {} : { before }),
          limit: PAGE_SIZE,
        },
        signal,
      ),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle>Update history</CardTitle>
        <CardDescription>
          Every attempt, newest first — including the ones that failed and the ones that were
          rolled back.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {history.isError ? (
          <ErrorPanel
            title="Could not load the history"
            error={history.error}
            onRetry={() => { void history.refetch(); }}
          />
        ) : history.isPending ? (
          <LoadingRows rows={3} />
        ) : history.data.jobs.length === 0 ? (
          <EmptyPanel title="Nothing yet">
            No update has been attempted on this installation.
          </EmptyPanel>
        ) : (
          <>
            <Table>
              <TableHeader>
                <TableHeaderRow>
                  <TableHead>When</TableHead>
                  <TableHead>Change</TableHead>
                  <TableHead>Result</TableHead>
                  <TableHead>Started by</TableHead>
                </TableHeaderRow>
              </TableHeader>
              <TableBody>
                {history.data.jobs.map((job) => {
                  const state = jobState(job.state);
                  return (
                    <TableRow key={job.job_id}>
                      <TableRowHeader>
                        <time dateTime={machineDateTime(job.created_at)}>
                          {formatDateTime(job.created_at)}
                        </time>
                      </TableRowHeader>
                      <TableCell>
                        <span className="font-mono text-sm">
                          {job.from_version ?? 'unknown'} → {job.to_version}
                        </span>
                        {/*
                         * The version the host ended up on, when it is not the
                         * one that was asked for. That difference is the
                         * interesting case: a job that claims success while
                         * the host runs the old version is a rollback nobody
                         * noticed.
                         */}
                        {job.resulting_version && job.resulting_version !== job.to_version ? (
                          <span className="text-text-tertiary ml-2 font-mono text-xs">
                            now {job.resulting_version}
                          </span>
                        ) : null}
                      </TableCell>
                      <TableCell>
                        <div className="flex flex-col gap-1">
                          <Badge variant={state.tone}>{state.label}</Badge>
                          {job.error ? (
                            <span className="text-text-tertiary text-xs">{job.error}</span>
                          ) : null}
                        </div>
                      </TableCell>
                      <TableCell>
                        <span className="text-text-secondary text-sm">
                          {job.trigger === 'manual'
                            ? 'An administrator'
                            : job.trigger === 'policy'
                              ? 'Automatic policy'
                              : 'Rollout'}
                        </span>
                        {job.attempt > 1 ? (
                          <span className="text-text-tertiary ml-2 text-xs">
                            attempt {job.attempt}
                          </span>
                        ) : null}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>

            <div className="flex justify-between">
              <Button
                variant="secondary"
                size="sm"
                disabled={cursors.length === 0}
                onClick={() => { setCursors((current) => current.slice(0, -1)); }}
              >
                Newer
              </Button>
              <Button
                variant="secondary"
                size="sm"
                disabled={history.data.next_before === null}
                onClick={() => {
                  const next = history.data.next_before;
                  if (next !== null) setCursors((current) => [...current, next]);
                }}
              >
                Older
              </Button>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}
