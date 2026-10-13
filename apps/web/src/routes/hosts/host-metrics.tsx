import { useQuery } from '@tanstack/react-query';
import { Activity } from 'lucide-react';
import { lazy, Suspense, useMemo, useState } from 'react';

import { fetchLatestMetrics, fetchMetricsStatus } from '@/api/metrics';
import type { Host, LatestValue, MetricSeries } from '@/api/types';
import { ErrorPanel } from '@/components/states/panel-states';
import { Alert } from '@/components/ui/alert';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { MetricTile } from '@/components/ui/metric-tile';
import { Select } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { formatDateTime, machineDateTime, relativeTime } from '@/lib/datetime';
import { queryKeys } from '@/lib/query-keys';
import {
  compareLatest,
  compareSeries,
  formatMetric,
  seriesLabel,
} from '@/routes/hosts/metric-format';

/**
 * A host's real measurements.
 *
 * ## The whole component is built around what it refuses to show
 *
 * Phase 3 replaced the "no metrics are collected yet" placeholder this card
 * used to be, and the honesty that placeholder had must survive the data
 * arriving. So:
 *
 * - **A series with no recent sample is absent, not zero.** The server leaves
 *   it out of `latest` and this renders nothing in its place. "CPU 0%" is a
 *   claim about a machine, and a zeroed tile is indistinguishable from a real
 *   measurement of zero.
 * - **"No data" says why.** An empty response has at least four causes —
 *   nothing enabled, agent offline, enabled but not yet collected, collecting
 *   but every reading stale — and they have different remedies. The empty
 *   state uses `status` and the host's own connectivity to say which it is,
 *   rather than printing one sentence that is right a quarter of the time.
 * - **Every value carries its age.** A reading is only current inside the
 *   freshness window, and the window is stated, so "nothing in the last five
 *   minutes" is distinguishable from "nothing, ever".
 *
 * ## The chart is lazy
 *
 * Recharts is around half the application bundle. It is loaded when this card
 * renders a history, not when the route does, so a host detail page opens
 * without waiting for a charting library.
 */

const MetricHistory = lazy(() => import('@/routes/hosts/metric-history'));

/** How often the current values refetch while the tab is visible. */
const POLL_INTERVAL_MS = 10_000;

export function HostMetricsCard({ host }: { host: Host }) {
  const hostId = host.host_id;

  const latest = useQuery({
    queryKey: queryKeys.session.latestMetrics(hostId),
    queryFn: ({ signal }) => fetchLatestMetrics(hostId, undefined, signal),
    refetchInterval: POLL_INTERVAL_MS,
  });

  const status = useQuery({
    queryKey: queryKeys.session.metricsStatus(hostId),
    queryFn: ({ signal }) => fetchMetricsStatus(hostId, signal),
    // Slower than the values: this answers "is anything arriving at all",
    // which does not change second to second, and it counts rows.
    refetchInterval: POLL_INTERVAL_MS * 3,
  });

  const values = useMemo(
    () => (latest.data ? [...latest.data.values].sort(compareLatest) : []),
    [latest.data],
  );

  return (
    <Card>
      <CardHeader>
        <div className="min-w-0">
          <CardTitle>Resources</CardTitle>
          <CardDescription>
            Measured on the host by the agent. A value is shown only while it is
            current; anything older than the freshness window is left out rather
            than presented as now.
          </CardDescription>
        </div>
        {latest.data && values.length > 0 ? (
          <FreshnessNote seconds={latest.data.freshness_seconds} />
        ) : null}
      </CardHeader>

      <CardContent className="flex flex-col gap-5">
        {latest.isPending ? (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            <Skeleton className="h-28" />
            <Skeleton className="h-28" />
            <Skeleton className="h-28" />
          </div>
        ) : latest.isError ? (
          <ErrorPanel
            error={latest.error}
            title="Could not load the current values"
            onRetry={() => {
              void latest.refetch();
            }}
          />
        ) : values.length === 0 ? (
          <NoCurrentData
            host={host}
            freshnessSeconds={latest.data.freshness_seconds}
            sampleCount={status.data?.sample_count ?? null}
            lastSampleAt={status.data?.last_sample_at ?? null}
          />
        ) : (
          <>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {values.map((value) => (
                <LatestTile key={value.series.series_id} latest={value} />
              ))}
            </div>
            <HistorySection hostId={hostId} values={values} />
          </>
        )}

        {status.data ? <ModuleActivity status={status.data} /> : null}
      </CardContent>
    </Card>
  );
}

/** The window the values were judged against, stated rather than implied. */
function FreshnessNote({ seconds }: { seconds: number }) {
  const minutes = Math.round(seconds / 60);
  return (
    <p className="text-text-faint shrink-0 text-xs">
      Current within {minutes >= 1 ? `${String(minutes)} min` : `${String(seconds)} s`}
    </p>
  );
}

/** One current value. */
function LatestTile({ latest }: { latest: LatestValue }) {
  const formatted = formatMetric(latest.value, latest.series.unit);
  return (
    <MetricTile
      label={seriesLabel(latest.series)}
      value={formatted.value}
      {...(formatted.unit === undefined ? {} : { unit: formatted.unit })}
      caption={`${latest.series.module_name} · ${relativeTime(latest.recorded_at)}`}
    />
  );
}

/**
 * Why there is nothing to show.
 *
 * Four distinguishable causes, because they have four different remedies and a
 * single "no data" message would send an operator to the wrong one. The order
 * is the order of likelihood for a host that was just set up.
 */
function NoCurrentData({
  host,
  freshnessSeconds,
  sampleCount,
  lastSampleAt,
}: {
  host: Host;
  freshnessSeconds: number;
  sampleCount: number | null;
  lastSampleAt: string | null;
}) {
  const minutes = Math.max(1, Math.round(freshnessSeconds / 60));

  // Nothing has ever arrived. Either collection is off or the agent has never
  // managed to deliver anything, and the host's own state tells them apart.
  if (sampleCount === 0 || (sampleCount === null && lastSampleAt === null)) {
    if (host.lifecycle === 'registered') {
      return (
        <Alert variant="info" title="This host has not been enrolled">
          No agent has claimed it, so there is nothing to collect from. Issue an
          enrolment credential and run <code className="font-mono">hcc-agent enroll</code> on
          the machine.
        </Alert>
      );
    }
    if (!host.is_enabled) {
      return (
        <Alert variant="warn" title="This host is disabled">
          Its agent is refused at the door, so no measurements can arrive. Enable the host to
          resume collection.
        </Alert>
      );
    }
    return (
      <Alert variant="info" title="No measurements have arrived yet">
        Nothing is enabled for this host, so there is nothing to measure. Turn on a module
        under <strong>Monitoring</strong> below; the first samples appear within a minute of
        the agent applying the change.
      </Alert>
    );
  }

  // Samples exist, but none inside the window. The agent has stopped
  // delivering — which is a different problem from never having started.
  return (
    <Alert variant="warn" title={`Nothing measured in the last ${String(minutes)} min`}>
      {lastSampleAt === null ? (
        <>This host has stored measurements, but none recent enough to show as current.</>
      ) : (
        <>
          The most recent measurement arrived{' '}
          <time dateTime={machineDateTime(lastSampleAt)} title={formatDateTime(lastSampleAt)}>
            {relativeTime(lastSampleAt)}
          </time>
          . Older readings are deliberately not shown as current values.{' '}
          {host.connectivity === 'connected'
            ? 'The agent is connected, so check whether its modules are still enabled.'
            : 'The agent is not connected, which is the likeliest reason.'}
        </>
      )}
    </Alert>
  );
}

/**
 * The history chart, with a series picker.
 *
 * One series at a time rather than everything at once. Overlaying memory in
 * bytes on CPU in percent needs two axes to be truthful, and a single shared
 * axis would either flatten the percentage into invisibility or scale the byte
 * count into nonsense.
 */
const WINDOWS = [
  { id: '1h', label: 'Last hour', seconds: 3600 },
  { id: '6h', label: 'Last 6 hours', seconds: 6 * 3600 },
  { id: '24h', label: 'Last 24 hours', seconds: 24 * 3600 },
  { id: '7d', label: 'Last 7 days', seconds: 7 * 86_400 },
  { id: '30d', label: 'Last 30 days', seconds: 30 * 86_400 },
] as const;

export type HistoryWindow = (typeof WINDOWS)[number];

function HistorySection({
  hostId,
  values,
}: {
  hostId: string;
  values: LatestValue[];
}) {
  // Only series with a current value are offered. A picker listing everything
  // the host has ever produced would let an operator select a series that
  // stopped reporting last month and be shown an empty chart with no
  // explanation.
  const series = useMemo(
    () => values.map((value) => value.series).sort(compareSeries),
    [values],
  );
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [windowId, setWindowId] = useState<string>('1h');

  const selected: MetricSeries | undefined =
    series.find((entry) => entry.series_id === selectedId) ?? series[0];
  const chosenWindow = WINDOWS.find((entry) => entry.id === windowId) ?? WINDOWS[0];

  if (selected === undefined) return null;

  return (
    <div className="border-border-subtle flex flex-col gap-4 border-t pt-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-text-primary text-sm font-semibold">History</h3>
          <p className="text-text-muted mt-0.5 text-xs">
            Gaps are real: a period when nothing was collected is drawn as a break, never as a
            line through it.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <label className="sr-only" htmlFor="metric-series">
            Series
          </label>
          <Select
            id="metric-series"
            className="w-auto"
            value={String(selected.series_id)}
            onChange={(event) => {
              setSelectedId(Number(event.target.value));
            }}
          >
            {series.map((entry) => (
              <option key={entry.series_id} value={String(entry.series_id)}>
                {entry.module_name} · {seriesLabel(entry)}
              </option>
            ))}
          </Select>
          <label className="sr-only" htmlFor="metric-window">
            Time range
          </label>
          <Select
            id="metric-window"
            className="w-auto"
            value={windowId}
            onChange={(event) => {
              setWindowId(event.target.value);
            }}
          >
            {WINDOWS.map((entry) => (
              <option key={entry.id} value={entry.id}>
                {entry.label}
              </option>
            ))}
          </Select>
        </div>
      </div>

      <Suspense fallback={<Skeleton className="h-64 w-full" />}>
        <MetricHistory hostId={hostId} series={selected} window={chosenWindow} />
      </Suspense>
    </div>
  );
}

/**
 * Which modules are producing data, including the ones that are not.
 *
 * Every module in the registry appears. "This module has no data" and "this
 * module does not exist on this controller" are different answers, and an
 * interface that listed only what reported would make them look the same.
 */
function ModuleActivity({
  status,
}: {
  status: { sample_count: number; modules: { module: string; module_name: string; series_count: number; last_sample_at: string | null }[] };
}) {
  return (
    <div className="border-border-subtle border-t pt-5">
      <div className="flex items-center gap-2">
        <Activity aria-hidden="true" className="text-text-faint size-4" />
        <h3 className="text-text-primary text-sm font-semibold">Collection</h3>
        <span className="text-text-faint text-xs">
          {status.sample_count.toLocaleString()} measurement
          {status.sample_count === 1 ? '' : 's'} stored
        </span>
      </div>
      <dl className="mt-3 grid gap-x-6 gap-y-3 sm:grid-cols-2 lg:grid-cols-3">
        {status.modules.map((module) => (
          <div key={module.module} className="min-w-0">
            <dt className="text-text-faint text-xs font-medium">{module.module_name}</dt>
            <dd className="mt-0.5 text-sm">
              {module.last_sample_at === null ? (
                <span className="text-text-faint">No data</span>
              ) : (
                <time
                  dateTime={machineDateTime(module.last_sample_at)}
                  title={formatDateTime(module.last_sample_at)}
                  className="text-text-primary"
                >
                  {relativeTime(module.last_sample_at)}
                </time>
              )}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}
