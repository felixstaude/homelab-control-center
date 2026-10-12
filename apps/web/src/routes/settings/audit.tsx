import { useQuery } from '@tanstack/react-query';
import { useId, useState } from 'react';

import { fetchAuditActions, fetchAuditEvents } from '@/api/audit';
import type { AuditActor, AuditEvent } from '@/api/types';
import { usePermissions, useAuth } from '@/auth/use-auth';
import {
  EmptyPanel,
  ErrorPanel,
  LoadingRows,
  PermissionRequired,
} from '@/components/states/panel-states';
import { Badge } from '@/components/ui/badge';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { CursorPagination } from '@/components/ui/pagination';
import { Select } from '@/components/ui/select';
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

/**
 * Audit viewer.
 *
 * A foundation, deliberately: recent events, an action filter, and forward
 * pagination. Not a search interface and not a SIEM — the trail covers user
 * and session administration today, and building query tooling for a hundred
 * rows would be tooling designed against the wrong shape of data.
 *
 * ## Rendering an event
 *
 * `action` is a plain string, not a union, because the vocabulary grows with
 * every milestone that records something new (ADR-038). So an unrecognised
 * action **must** render rather than crash or disappear: {@link describeAction}
 * falls back to the raw identifier, which is honest and readable, instead of
 * "Unknown action" — which would hide the one piece of information the event
 * actually carried.
 *
 * `details` is shown as the server sent it, as text. React escapes by default,
 * so a stored value cannot become markup — which is the shape audit-log
 * injection takes in practice. The honest statement is that the protection is
 * the renderer's escaping plus a writer that never accepts caller input, not a
 * sanitiser.
 */

/** Human phrasing for the actions this build knows about. */
const ACTION_LABELS: Record<string, string> = {
  'user.created': 'Account created',
  'user.updated': 'Account details changed',
  'user.role_changed': 'Role changed',
  'user.deactivated': 'Account deactivated',
  'user.reactivated': 'Account reactivated',
  'user.sessions_revoked': 'Sessions revoked by an administrator',
  'account.password_changed': 'Password changed',
  'account.sessions_revoked': 'Own sessions revoked',
  'admin.access_denied': 'Administrative access denied',
};

/**
 * Phrase an action, falling back to the identifier.
 *
 * The fallback is the raw value rather than a placeholder, so an event
 * recorded by a newer control plane is still legible — `host.enrolled` reads
 * perfectly well on its own, and "Unknown action" would read as a defect while
 * discarding the only fact the row carried.
 */
function describeAction(action: string): string {
  return ACTION_LABELS[action] ?? action;
}

const OUTCOME_VARIANTS: Record<string, 'ok' | 'warn' | 'critical' | 'neutral'> = {
  success: 'ok',
  failure: 'warn',
  denied: 'critical',
};

/** `denied` is the loudest: somebody tried something they were not allowed to. */
function OutcomeBadge({ outcome }: { outcome: string }) {
  return <Badge variant={OUTCOME_VARIANTS[outcome] ?? 'neutral'}>{outcome}</Badge>;
}

/**
 * An actor or target.
 *
 * `null` username means the account no longer exists, which is reported as
 * such with the id shown in monospace. The id *is* what the trail recorded, so
 * displaying it is the accurate answer rather than a degraded one.
 */
function ActorCell({ actor }: { actor: AuditActor | null }) {
  if (actor === null) {
    return <span className="text-text-faint text-xs">—</span>;
  }
  if (actor.username === null) {
    return (
      <span className="text-text-faint font-mono text-xs" title="This account no longer exists">
        {actor.user_id.slice(0, 8)}… (deleted)
      </span>
    );
  }
  return (
    <span className="text-text-secondary">
      {actor.display_name ?? actor.username}
      {actor.display_name ? (
        <span className="text-text-faint ml-1.5 font-mono text-[11px]">
          {actor.username}
        </span>
      ) : null}
    </span>
  );
}

/**
 * One `details` value, as text.
 *
 * Every value the writer produces today is a string, a number or a boolean.
 * The object and fallback branches exist because `details` is typed
 * `Record<string, unknown>` — it crosses the network — and a value that
 * stringified to `[object Object]` would be a cell that looks like a bug while
 * silently discarding what the trail recorded.
 */
function renderDetailValue(value: unknown): string {
  if (value === null) return 'null';
  switch (typeof value) {
    case 'string':
      return value;
    case 'number':
    case 'boolean':
      return String(value);
    default:
      /*
       * Objects, arrays and anything exotic.
       *
       * `JSON.stringify` is typed as returning `string`, but at runtime it
       * returns `undefined` for a function or a symbol — neither of which can
       * survive a JSON response, which is why the type is honest for this
       * input and the `?? ''` a reader might expect is not written. An empty
       * cell for an impossible value is the right outcome anyway.
       */
      return JSON.stringify(value);
  }
}

/**
 * The event's `details`, as a compact key/value line.
 *
 * Flat by construction — the writer builds it from named scalar fields — so
 * one line per key is enough and a JSON blob would be noise. A nested value is
 * stringified rather than being skipped, because an omitted field in an audit
 * trail is worse than an ugly one.
 */
function DetailsCell({ details }: { details: Record<string, unknown> | null }) {
  if (details === null || Object.keys(details).length === 0) {
    return <span className="text-text-faint text-xs">—</span>;
  }
  return (
    <span className="text-text-muted text-xs">
      {Object.entries(details).map(([key, value], index) => (
        <span key={key}>
          {index > 0 ? ', ' : ''}
          <span className="text-text-faint">{key}</span>{' '}
          <span className="text-text-secondary font-mono">
            {renderDetailValue(value)}
          </span>
        </span>
      ))}
    </span>
  );
}

const PAGE_SIZE = 25;
const ANY = '';

export function SettingsAudit() {
  const { state } = useAuth();
  const permissions = usePermissions();

  const [action, setAction] = useState<string>(ANY);
  /**
   * Cursors already used, newest page last.
   *
   * Keyset pagination is forward-only — a cursor names a position and the
   * server does not hand out the previous one — so "previous" is navigation
   * through this history rather than a query. The first page's cursor is the
   * empty string, which is why the stack starts with one entry rather than
   * empty.
   */
  const [cursors, setCursors] = useState<string[]>([ANY]);
  const actionId = useId();

  const cursor = cursors[cursors.length - 1] ?? ANY;

  const actions = useQuery({
    queryKey: queryKeys.session.auditActions(),
    queryFn: ({ signal }) => fetchAuditActions(signal),
    // A constant for the life of the build.
    staleTime: Number.POSITIVE_INFINITY,
    enabled: permissions.administer,
  });

  const events = useQuery({
    queryKey: queryKeys.session.auditEvents({ action, cursor, limit: PAGE_SIZE }),
    queryFn: ({ signal }) =>
      fetchAuditEvents(
        { action: action || undefined, cursor: cursor || undefined, limit: PAGE_SIZE },
        signal,
      ),
    placeholderData: (previous) => previous,
    enabled: permissions.administer,
  });

  if (!permissions.administer) {
    return (
      <PermissionRequired
        required="admin"
        actual={state.status === 'authenticated' ? state.role : 'viewer'}
        what="The audit trail"
      />
    );
  }

  return (
    <Card>
      <CardHeader>
        <div className="min-w-0">
          <CardTitle>Audit trail</CardTitle>
          <CardDescription>
            Append-only record of privileged actions. Written by the operations themselves,
            so there is no endpoint that can add to it or remove from it. Covers account and
            session administration; later milestones add their own events.
          </CardDescription>
        </div>
        <div className="w-44 shrink-0">
          <label htmlFor={actionId} className="sr-only">
            Filter by action
          </label>
          <Select
            id={actionId}
            value={action}
            onChange={(event) => {
              setAction(event.target.value);
              // A new filter is a new ordering, so the cursor history no
              // longer applies — keeping it would page into positions from a
              // different result set.
              setCursors([ANY]);
            }}
          >
            <option value={ANY}>All actions</option>
            {(actions.data ?? []).map((candidate) => (
              <option key={candidate} value={candidate}>
                {describeAction(candidate)}
              </option>
            ))}
          </Select>
        </div>
      </CardHeader>

      <CardContent className="px-0 pb-0">
        {events.isPending ? (
          <LoadingRows rows={6} columns={4} />
        ) : events.isError ? (
          <ErrorPanel
            error={events.error}
            title="Could not load the audit trail"
            onRetry={() => {
              void events.refetch();
            }}
          />
        ) : events.data.items.length === 0 ? (
          <EmptyPanel title={action ? 'No events of that kind yet' : 'No events recorded yet'}>
            {action
              ? 'Nothing has produced this action on this installation.'
              : 'Creating an account, changing a role or revoking a session will appear here.'}
          </EmptyPanel>
        ) : (
          <>
            <Table>
              <caption className="sr-only">
                Audit events, newest first, with actor, target and outcome.
              </caption>
              <TableHead>
                <TableHeaderRow>
                  <TableHeader>When</TableHeader>
                  <TableHeader>Action</TableHeader>
                  <TableHeader>Outcome</TableHeader>
                  <TableHeader className="hidden md:table-cell">Actor</TableHeader>
                  <TableHeader className="hidden md:table-cell">Target</TableHeader>
                  <TableHeader className="hidden xl:table-cell">Details</TableHeader>
                </TableHeaderRow>
              </TableHead>
              <TableBody>
                {events.data.items.map((event) => (
                  <AuditRow key={event.event_id} event={event} />
                ))}
              </TableBody>
            </Table>

            <CursorPagination
              hasPrevious={cursors.length > 1}
              hasNext={events.data.next_cursor !== null}
              busy={events.isFetching}
              onPrevious={() => {
                setCursors((history) => history.slice(0, -1));
              }}
              onNext={() => {
                const next = events.data.next_cursor;
                if (next !== null) setCursors((history) => [...history, next]);
              }}
              summary={`${events.data.total} ${events.data.total === 1 ? 'event' : 'events'} recorded`}
            />
          </>
        )}
      </CardContent>
    </Card>
  );
}

function AuditRow({ event }: { event: AuditEvent }) {
  return (
    <TableRow>
      <TableRowHeader className="whitespace-nowrap">
        <time
          dateTime={machineDateTime(event.occurred_at)}
          className="text-text-secondary text-xs"
        >
          {formatDateTime(event.occurred_at)}
        </time>
      </TableRowHeader>
      <TableCell className="text-text-primary">{describeAction(event.action)}</TableCell>
      <TableCell>
        <OutcomeBadge outcome={event.outcome} />
      </TableCell>
      <TableCell className="hidden md:table-cell">
        <ActorCell actor={event.actor} />
      </TableCell>
      <TableCell className="hidden md:table-cell">
        <ActorCell actor={event.target} />
      </TableCell>
      <TableCell className="hidden xl:table-cell">
        <DetailsCell details={event.details} />
      </TableCell>
    </TableRow>
  );
}
