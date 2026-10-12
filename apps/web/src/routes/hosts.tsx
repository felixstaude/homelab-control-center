import { useQuery } from '@tanstack/react-query';
import { Search, ServerCog } from 'lucide-react';
import { useId, useState } from 'react';
import { Link } from 'react-router-dom';

import { fetchHosts } from '@/api/hosts';
import type { Host, HostConnectivity, HostLifecycle } from '@/api/types';
import { usePermissions } from '@/auth/use-auth';
import { PageHeader } from '@/components/page-header';
import { EmptyPanel, ErrorPanel, LoadingRows } from '@/components/states/panel-states';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Pagination } from '@/components/ui/pagination';
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
import { formatDateTime, machineDateTime, relativeTime } from '@/lib/datetime';
import { queryKeys } from '@/lib/query-keys';
import { CreateHostDialog } from '@/routes/hosts/create-host-dialog';
import { EnrollmentDialog } from '@/routes/hosts/enrollment-dialog';
import {
  ConnectivityBadge,
  DisabledBadge,
  LifecycleBadge,
  PendingEnrollmentBadge,
} from '@/routes/hosts/host-badges';

/**
 * The host registry.
 *
 * ## This is the first screen in the product showing real data
 *
 * Every other page that renders anything beyond identity is still fixtures
 * behind a banner (`src/mocks/`). This one is not: every value here came from
 * an agent or from an administrator, and the columns that have no value show
 * nothing rather than a zero. There is no `MockDataBanner` here and there must
 * never be one — a banner on real data would be as misleading as its absence
 * on fake data.
 *
 * ## Polling, and why it is not a socket
 *
 * Connectivity changes without the browser doing anything: an agent connects,
 * a heartbeat goes stale, a network drops. So the list refetches on an
 * interval. A browser WebSocket would be lower-latency and is the wrong trade
 * today — it needs its own authentication story on the socket (the CSRF
 * double-submit header cannot be sent on a handshake), a reconnection
 * strategy in the client, and a per-session subscription on the server. Ten
 * seconds of latency on a fleet of tens of hosts is not a problem anybody
 * has; that machinery is.
 *
 * The interval stops while the tab is hidden, which TanStack Query does by
 * default with `refetchIntervalInBackground` left off. A console left open on
 * a second monitor overnight should not spend a request every ten seconds
 * until morning.
 *
 * ## Filters live in component state, not the URL
 *
 * The same decision the user list made, for the same reason: a search term
 * halfway through typing is not a place worth linking to, and putting it in
 * the URL would push a history entry per keystroke.
 */

const PAGE_SIZE = 25;

/** How often the list refetches while the tab is visible. */
const POLL_INTERVAL_MS = 10_000;

/** "Any" is the absence of a filter, spelled explicitly so the key is stable. */
const ANY = '';

type LifecycleFilter = HostLifecycle | typeof ANY;
type ConnectivityFilter = HostConnectivity | typeof ANY;
type EnabledFilter = 'enabled' | 'disabled' | typeof ANY;

export function HostsPage() {
  const permissions = usePermissions();

  const [search, setSearch] = useState('');
  const [lifecycleFilter, setLifecycleFilter] = useState<LifecycleFilter>(ANY);
  const [connectivityFilter, setConnectivityFilter] = useState<ConnectivityFilter>(ANY);
  const [enabledFilter, setEnabledFilter] = useState<EnabledFilter>(ANY);
  const [offset, setOffset] = useState(0);
  const [creating, setCreating] = useState(false);
  /** The host whose enrollment dialog is open, if any. */
  const [enrolling, setEnrolling] = useState<Host | null>(null);

  const searchId = useId();
  const lifecycleId = useId();
  const connectivityId = useId();
  const enabledId = useId();

  const isEnabled =
    enabledFilter === ANY ? undefined : enabledFilter === 'enabled' ? true : false;

  const hosts = useQuery({
    queryKey: queryKeys.session.hostList({
      search,
      lifecycle: lifecycleFilter,
      connectivity: connectivityFilter,
      enabled: enabledFilter,
      limit: PAGE_SIZE,
      offset,
    }),
    queryFn: ({ signal }) =>
      fetchHosts(
        {
          search: search || undefined,
          lifecycle: lifecycleFilter || undefined,
          connectivity: connectivityFilter || undefined,
          isEnabled,
          limit: PAGE_SIZE,
          offset,
        },
        signal,
      ),
    // The previous page stays on screen while the next loads, so changing a
    // filter or a poll landing does not blank the table and jump the layout.
    placeholderData: (previous) => previous,
    refetchInterval: POLL_INTERVAL_MS,
  });

  const filtered = Boolean(search || lifecycleFilter || connectivityFilter || enabledFilter);

  const clearFilters = () => {
    setSearch('');
    setLifecycleFilter(ANY);
    setConnectivityFilter(ANY);
    setEnabledFilter(ANY);
    setOffset(0);
  };

  return (
    <>
      <PageHeader
        title="Hosts"
        // Duplicated by the shell header, which renders this page name as
        // the `<h1>`. See `PageHeader`.
        titleHidden
        description="Linux servers reporting through the Homelab Control Center agent."
      />

      <Card>
        <CardHeader>
          <div className="min-w-0">
            <CardTitle>Registered hosts</CardTitle>
            <CardDescription>
              Connectivity is derived from each host&rsquo;s last heartbeat, not from a stored
              flag &mdash; so a controller restart reports hosts as stale rather than claiming
              they are still connected. This list refreshes every ten seconds while this tab
              is open.
            </CardDescription>
          </div>
          {permissions.administer ? (
            <Button
              variant="primary"
              size="sm"
              onClick={() => {
                setCreating(true);
              }}
            >
              <ServerCog />
              Register host
            </Button>
          ) : null}
        </CardHeader>

        {/* --- Filters --- */}
        <div
          className="border-border-subtle grid gap-3 border-t px-5 py-4
            sm:grid-cols-[minmax(0,1fr)_auto_auto_auto]"
        >
          <div>
            <label htmlFor={searchId} className="text-text-secondary text-xs font-medium">
              Search
            </label>
            <div className="relative mt-1.5">
              <Search
                aria-hidden="true"
                className="text-text-faint pointer-events-none absolute top-1/2 left-3 size-4
                  -translate-y-1/2"
              />
              <Input
                id={searchId}
                type="search"
                value={search}
                onChange={(event) => {
                  setSearch(event.target.value);
                  setOffset(0);
                }}
                placeholder="Host name"
                autoComplete="off"
                className="pl-9"
              />
            </div>
          </div>

          <FilterSelect
            id={connectivityId}
            label="Connectivity"
            value={connectivityFilter}
            onChange={(value) => {
              setConnectivityFilter(value as ConnectivityFilter);
              setOffset(0);
            }}
            options={[
              [ANY, 'Any'],
              ['connected', 'Connected'],
              ['stale', 'Stale'],
              ['offline', 'Offline'],
            ]}
          />

          <FilterSelect
            id={lifecycleId}
            label="Enrolment"
            value={lifecycleFilter}
            onChange={(value) => {
              setLifecycleFilter(value as LifecycleFilter);
              setOffset(0);
            }}
            options={[
              [ANY, 'Any'],
              ['registered', 'Not enrolled'],
              ['enrolled', 'Enrolled'],
              ['revoked', 'Revoked'],
            ]}
          />

          <FilterSelect
            id={enabledId}
            label="State"
            value={enabledFilter}
            onChange={(value) => {
              setEnabledFilter(value as EnabledFilter);
              setOffset(0);
            }}
            options={[
              [ANY, 'Any'],
              ['enabled', 'Enabled'],
              ['disabled', 'Disabled'],
            ]}
          />
        </div>

        <CardContent className="px-0 pb-0">
          {hosts.isPending ? (
            <LoadingRows rows={4} columns={5} />
          ) : hosts.isError ? (
            <ErrorPanel
              error={hosts.error}
              title="Could not load the hosts"
              onRetry={() => {
                void hosts.refetch();
              }}
            />
          ) : hosts.data.items.length === 0 ? (
            <EmptyPanel
              title={filtered ? 'No hosts match those filters' : 'No hosts registered yet'}
              action={
                filtered ? (
                  <Button variant="secondary" size="sm" onClick={clearFilters}>
                    Clear filters
                  </Button>
                ) : permissions.administer ? (
                  <Button
                    variant="primary"
                    size="sm"
                    onClick={() => {
                      setCreating(true);
                    }}
                  >
                    <ServerCog />
                    Register the first host
                  </Button>
                ) : undefined
              }
            >
              {filtered
                ? 'Try a shorter search term, or clear the filters.'
                : 'Register a host, then enrol its agent to start seeing it here.'}
            </EmptyPanel>
          ) : (
            <>
              <Table>
                <caption className="sr-only">
                  Registered hosts, with connectivity, enrolment state and the time of the
                  last heartbeat.
                </caption>
                <TableHead>
                  <TableHeaderRow>
                    <TableHeader>Host</TableHeader>
                    <TableHeader>Connectivity</TableHeader>
                    <TableHeader className="hidden sm:table-cell">Enrolment</TableHeader>
                    <TableHeader className="hidden md:table-cell">Last heartbeat</TableHeader>
                    <TableHeader className="hidden lg:table-cell">Agent</TableHeader>
                  </TableHeaderRow>
                </TableHead>
                <TableBody>
                  {hosts.data.items.map((host) => (
                    <HostRowView key={host.host_id} host={host} />
                  ))}
                </TableBody>
              </Table>

              <Pagination
                total={hosts.data.total}
                limit={hosts.data.limit}
                offset={hosts.data.offset}
                onOffsetChange={setOffset}
                busy={hosts.isFetching}
                label="hosts"
              />

              {connectivityFilter ? (
                // The honest caveat. The server applies this filter to the
                // page after fetching it, so the total counts what the other
                // filters matched — stating that is better than a pager whose
                // arithmetic looks wrong.
                <p className="text-text-faint border-border-subtle border-t px-5 py-3 text-xs">
                  Connectivity is derived rather than stored, so it is filtered within each
                  page. The total above counts hosts matching the other filters.
                </p>
              ) : null}
            </>
          )}
        </CardContent>
      </Card>

      {permissions.administer ? (
        <>
          <CreateHostDialog
            open={creating}
            onOpenChange={setCreating}
            // Offering enrollment straight after registering is the whole
            // reason the two are separate operations: the operator sees the
            // record exist, then decides to authorise an agent for it.
            onCreated={setEnrolling}
          />
          <EnrollmentDialog
            // Remounted per host, and again on close, so a credential cannot
            // outlive the dialog that showed it. See the component's note.
            key={enrolling?.host_id ?? 'closed'}
            host={enrolling}
            onOpenChange={(open) => {
              if (!open) setEnrolling(null);
            }}
          />
        </>
      ) : null}
    </>
  );
}

/** A labelled select, so the four filters do not repeat their wiring. */
function FilterSelect({
  id,
  label,
  value,
  onChange,
  options,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: readonly (readonly [string, string])[];
}) {
  return (
    <div>
      <label htmlFor={id} className="text-text-secondary text-xs font-medium">
        {label}
      </label>
      <div className="mt-1.5 sm:w-36">
        <Select
          id={id}
          value={value}
          onChange={(event) => {
            onChange(event.target.value);
          }}
        >
          {options.map(([optionValue, optionLabel]) => (
            <option key={optionValue} value={optionValue}>
              {optionLabel}
            </option>
          ))}
        </Select>
      </div>
    </div>
  );
}

/** One host's row. */
function HostRowView({ host }: { host: Host }) {
  return (
    <TableRow>
      <TableRowHeader>
        <Link
          to={`/hosts/${host.host_id}`}
          className="text-text-primary hover:text-accent font-medium"
        >
          {host.name}
        </Link>
        {host.environment === null ? null : (
          <span className="text-text-faint mt-0.5 block text-xs">{host.environment}</span>
        )}
      </TableRowHeader>

      <TableCell>
        <div className="flex flex-wrap items-center gap-1.5">
          <ConnectivityBadge host={host} />
          <DisabledBadge isEnabled={host.is_enabled} />
        </div>
      </TableCell>

      <TableCell className="hidden sm:table-cell">
        <div className="flex flex-wrap items-center gap-1.5">
          <LifecycleBadge lifecycle={host.lifecycle} />
          <PendingEnrollmentBadge pending={host.has_pending_enrollment} />
        </div>
      </TableCell>

      <TableCell className="hidden md:table-cell">
        {/*
         * Null is rendered as a sentence, not as a dash and not as a date.
         * "We have never heard from this host" and "we heard from it at
         * midnight on 1 January 1970" are different claims, and only one of
         * them is ever true.
         */}
        {host.last_heartbeat_at === null ? (
          <span className="text-text-faint text-sm">Never</span>
        ) : (
          <time
            dateTime={machineDateTime(host.last_heartbeat_at)}
            title={formatDateTime(host.last_heartbeat_at)}
            className="text-text-secondary text-sm"
          >
            {relativeTime(host.last_heartbeat_at)}
          </time>
        )}
      </TableCell>

      <TableCell className="hidden lg:table-cell">
        {host.agent_version === null ? (
          <span className="text-text-faint text-sm">&mdash;</span>
        ) : (
          <span className="text-text-secondary font-mono text-xs">{host.agent_version}</span>
        )}
      </TableCell>
    </TableRow>
  );
}
