import { useQuery } from '@tanstack/react-query';
import { RefreshCw, ShieldCheck, ShieldAlert } from 'lucide-react';
import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';

import { fetchHostUpdateStatuses, fetchUpdateOverview } from '@/api/updates';
import type { HostUpdateStatus } from '@/api/types';
import { usePermissions } from '@/auth/use-auth';
import { PageHeader } from '@/components/page-header';
import { EmptyPanel, ErrorPanel, LoadingRows } from '@/components/states/panel-states';
import { Badge } from '@/components/ui/badge';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { MetricTile } from '@/components/ui/metric-tile';
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
import { queryKeys } from '@/lib/query-keys';
import { RolloutPanel } from '@/routes/updates/rollout-panel';
import { UpdateHistoryPanel } from '@/routes/updates/update-history-panel';
import { UpdatePolicyDialog } from '@/routes/updates/update-policy-dialog';
import {
  OUTCOMES,
  POLICY_LABELS,
  canStartUpdate,
  jobState,
  signaturePresentation,
} from '@/routes/updates/update-status';

/**
 * Update management.
 *
 * ## Everything here is real
 *
 * Like the host registry and unlike most of this application, there is no
 * `MockDataBanner` on this page and there must never be one. Every number came
 * from a host's own report or from the controller's release metadata, and a
 * column with no value shows nothing rather than a zero.
 *
 * ## Progress is reported, never simulated
 *
 * The bar on an in-flight update is derived from the state the agent last
 * reported, so a stuck download shows a bar that does not move. A bar that
 * advanced on a timer would be inventing progress, and somebody would believe
 * it — which is worse than no bar at all.
 *
 * ## Polling, for the same reason the host list polls
 *
 * An update changes without the browser doing anything: an agent picks up an
 * instruction, a download finishes, a service restarts. So this refetches on
 * an interval, faster than the host list because an update is a minutes-long
 * operation somebody is watching rather than a steady state.
 *
 * ## What an operator can and cannot do here
 *
 * Every role sees everything. Only an administrator sees the controls, and the
 * server refuses the rest regardless — the hiding is a kindness, not the
 * boundary (`docs/SECURITY.md`).
 */

/** How often the page refetches while somebody is looking at it. */
const REFRESH_INTERVAL_MS = 5_000;

export function UpdatesPage() {
  const { administer } = usePermissions();
  const [policyHost, setPolicyHost] = useState<HostUpdateStatus | null>(null);

  const overview = useQuery({
    queryKey: queryKeys.session.updateOverview(),
    queryFn: ({ signal }) => fetchUpdateOverview(signal),
    refetchInterval: REFRESH_INTERVAL_MS,
  });

  const hosts = useQuery({
    queryKey: queryKeys.session.hostUpdateStatuses(),
    queryFn: ({ signal }) => fetchHostUpdateStatuses(signal),
    refetchInterval: REFRESH_INTERVAL_MS,
  });

  // The version a new rollout would most likely target: whichever the
  // planner is offering to the most hosts. Derived from the plans rather
  // than from the manifest's `recommended`, because a host's plan already
  // accounts for its channel and its pin — and offering a version half the
  // fleet is not eligible for would be a form that fails on submit.
  const suggestedVersion = useMemo(() => {
    const counts = new Map<string, number>();
    for (const host of hosts.data ?? []) {
      const target = host.plan.target_version;
      if (target === null || !canStartUpdate(host.plan)) continue;
      counts.set(target, (counts.get(target) ?? 0) + 1);
    }
    let best: string | null = null;
    let bestCount = 0;
    for (const [target, count] of counts) {
      if (count > bestCount) {
        best = target;
        bestCount = count;
      }
    }
    return best;
  }, [hosts.data]);

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Updates"
        description="What the fleet is running, what is available, and what is in progress."
      />

      {overview.isError ? (
        <ErrorPanel
          title="Could not load update status"
          error={overview.error}
          onRetry={() => { void overview.refetch(); }}
        />
      ) : null}

      {overview.isPending ? <LoadingRows rows={2} /> : null}

      {overview.data ? (
        <>
          <section aria-label="Fleet summary" className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <MetricTile
              label="Hosts"
              value={String(overview.data.total_hosts)}
              caption={`${String(overview.data.up_to_date)} up to date`}
            />
            <MetricTile
              label="Updates available"
              value={String(overview.data.update_available)}
              {...(overview.data.blocked > 0
                ? { caption: `${String(overview.data.blocked)} blocked` }
                : {})}
            />
            <MetricTile
              label="In progress"
              value={String(overview.data.open_jobs)}
              {...(overview.data.failed_recently > 0
                ? { caption: `${String(overview.data.failed_recently)} failed this week` }
                : {})}
            />
            <MetricTile
              label="Automatic updates"
              value={String(overview.data.hosts_with_automatic_updates)}
              caption={`of ${String(overview.data.total_hosts)} hosts`}
            />
          </section>

          <Card>
            <CardHeader>
              <CardTitle>This controller</CardTitle>
              <CardDescription>
                What it is running, and whether it can vouch for what it offers.
              </CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-4">
              <dl className="grid gap-4 sm:grid-cols-3">
                <div>
                  <dt className="text-text-tertiary text-xs uppercase tracking-wide">Version</dt>
                  <dd className="text-text-primary font-mono text-sm">
                    {overview.data.controller_version}
                  </dd>
                </div>
                <div>
                  <dt className="text-text-tertiary text-xs uppercase tracking-wide">
                    Database revision
                  </dt>
                  <dd className="text-text-primary font-mono text-sm">
                    {overview.data.controller_schema_revision ?? 'unknown'}
                  </dd>
                </div>
                <div>
                  <dt className="text-text-tertiary text-xs uppercase tracking-wide">
                    Release metadata
                  </dt>
                  <dd className="flex items-center gap-2 text-sm">
                    <SignatureBadge status={overview.data.metadata_signature} />
                  </dd>
                </div>
              </dl>

              {/*
               * The explanation is always shown, not only on failure. An
               * administrator reading "Unsigned" needs to know what that
               * costs them — automatic updates will refuse — and a tooltip
               * they have to discover is not where that belongs.
               */}
              <p className="text-text-secondary text-sm">
                {signaturePresentation(overview.data.metadata_signature).explanation}
              </p>

              {overview.data.metadata_available ? null : (
                <p className="text-text-secondary text-sm">
                  {overview.data.metadata_detail ??
                    'No release metadata is configured, so this controller does not know what versions exist.'}
                </p>
              )}
            </CardContent>
          </Card>

          {overview.data.version_distribution.length > 0 ? (
            <Card>
              <CardHeader>
                <CardTitle>Agent versions</CardTitle>
                <CardDescription>
                  Newest first. A version the controller cannot parse is shown as the host
                  reported it rather than hidden.
                </CardDescription>
              </CardHeader>
              <CardContent>
                <ul className="flex flex-wrap gap-2">
                  {overview.data.version_distribution.map((entry) => (
                    <li key={entry.version}>
                      <Badge variant="neutral">
                        <span className="font-mono">{entry.version}</span>
                        <span className="text-text-tertiary">
                          {entry.hosts} {entry.hosts === 1 ? 'host' : 'hosts'}
                        </span>
                      </Badge>
                    </li>
                  ))}
                </ul>
              </CardContent>
            </Card>
          ) : null}
        </>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>Hosts</CardTitle>
          <CardDescription>
            Each host&apos;s installed version, what is available, and how it is allowed to
            update itself.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {hosts.isError ? (
            <ErrorPanel
              title="Could not load hosts"
              error={hosts.error}
              onRetry={() => { void hosts.refetch(); }}
            />
          ) : hosts.isPending ? (
            <LoadingRows rows={4} />
          ) : hosts.data.length === 0 ? (
            <EmptyPanel title="No hosts yet">
              Register a host and enroll its agent, and its update status appears here.
            </EmptyPanel>
          ) : (
            <Table>
              <TableHeader>
                <TableHeaderRow>
                  <TableHead>Host</TableHead>
                  <TableHead>Installed</TableHead>
                  <TableHead>Available</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Policy</TableHead>
                  <TableHead>In progress</TableHead>
                  {administer ? <TableHead>Actions</TableHead> : null}
                </TableHeaderRow>
              </TableHeader>
              <TableBody>
                {hosts.data.map((host) => (
                  <HostRow
                    key={host.host_id}
                    host={host}
                    isAdmin={administer}
                    onEditPolicy={() => { setPolicyHost(host); }}
                  />
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <RolloutPanel hosts={hosts.data ?? []} suggestedVersion={suggestedVersion} />

      <UpdateHistoryPanel />

      {policyHost ? (
        <UpdatePolicyDialog
          host={policyHost}
          channels={overview.data?.channels ?? ['stable']}
          onClose={() => { setPolicyHost(null); }}
        />
      ) : null}
    </div>
  );
}

function SignatureBadge({ status }: { status: string }) {
  const presentation = signaturePresentation(status);
  const Icon = status === 'valid' ? ShieldCheck : ShieldAlert;
  return (
    <Badge variant={presentation.tone}>
      <Icon aria-hidden className="size-3.5" />
      {presentation.label}
    </Badge>
  );
}

function HostRow({
  host,
  isAdmin,
  onEditPolicy,
}: {
  host: HostUpdateStatus;
  isAdmin: boolean;
  onEditPolicy: () => void;
}) {
  const outcome = OUTCOMES[host.plan.outcome];
  const job = host.open_job;

  return (
    <TableRow>
      <TableRowHeader>
        <Link
          to={`/hosts/${host.host_id}`}
          className="text-text-primary hover:text-accent font-medium"
        >
          {host.host_name}
        </Link>
      </TableRowHeader>
      <TableCell>
        {/*
         * Nothing invented: a host that has never reported a version shows a
         * dash, not a guess.
         */}
        <span className="font-mono text-sm">{host.installed_version ?? '—'}</span>
      </TableCell>
      <TableCell>
        <span className="font-mono text-sm">{host.plan.target_version ?? '—'}</span>
        {host.plan.target_channel && host.plan.target_channel !== 'stable' ? (
          <span className="text-text-tertiary ml-2 text-xs">{host.plan.target_channel}</span>
        ) : null}
      </TableCell>
      <TableCell>
        <div className="flex flex-col gap-1">
          <Badge variant={outcome.tone}>{outcome.label}</Badge>
          {/*
           * Every blocker, not the first. An administrator fixing one wants to
           * know about the second now rather than after another attempt.
           */}
          {host.plan.blockers.length > 0 ? (
            <ul className="text-text-tertiary flex flex-col gap-0.5 text-xs">
              {host.plan.blockers.map((blocker) => (
                <li key={blocker.code}>{blocker.detail}</li>
              ))}
            </ul>
          ) : null}
        </div>
      </TableCell>
      <TableCell>
        <div className="flex flex-col gap-1">
          <span className="text-text-secondary text-sm">{POLICY_LABELS[host.policy]}</span>
          {host.pinned_version ? (
            <span className="text-text-tertiary font-mono text-xs">{host.pinned_version}</span>
          ) : null}
          {host.maintenance_window ? (
            <span className="text-text-tertiary text-xs">
              {host.maintenance_window.description}
            </span>
          ) : null}
        </div>
      </TableCell>
      <TableCell>
        {job ? (
          <div className="flex flex-col gap-1">
            <Badge variant={jobState(job.state).tone}>{jobState(job.state).label}</Badge>
            <span className="text-text-tertiary font-mono text-xs">{job.to_version}</span>
          </div>
        ) : (
          <span className="text-text-tertiary text-sm">—</span>
        )}
      </TableCell>
      {isAdmin ? (
        <TableCell>
          <button
            type="button"
            onClick={onEditPolicy}
            className="text-accent hover:text-accent-strong inline-flex items-center gap-1.5 text-sm font-medium"
          >
            <RefreshCw aria-hidden className="size-3.5" />
            Manage
          </button>
        </TableCell>
      ) : null}
    </TableRow>
  );
}
