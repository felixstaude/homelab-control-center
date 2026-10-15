import { useQuery } from '@tanstack/react-query';
import { Activity, AlertTriangle } from 'lucide-react';
import { lazy, Suspense, useMemo, useState } from 'react';

import { fetchLatestMetrics, fetchMetricsStatus } from '@/api/metrics';
import type {
  Host,
  LatestValue,
  MetricSeries,
  MetricsStatus,
  ModuleMetricStatus,
} from '@/api/types';
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
  RESOURCE_MODULES,
  seriesLabel,
} from '@/routes/hosts/metric-format';
import { ResourcePanels } from '@/routes/hosts/resource-panels';

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
 * - **"No data" says why.** An empty response has at least five causes —
 *   nothing enabled, agent offline, enabled but not yet collected, collecting
 *   but every reading stale, and the collector failing every interval — and
 *   they have different remedies. The empty state uses `status` and the
 *   host's own connectivity to say which it is, rather than printing one
 *   sentence that is right a fifth of the time.
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
    // **Not polled.** This endpoint counts stored samples, which costs time
    // proportional to the host's history — so polling it would make the page
    // more expensive the longer the installation had been running, to keep a
    // line of text fresh that does not change meaningfully minute to minute.
    //
    // The live signal is `latest` above, which polls every ten seconds and is
    // cheap. This is fetched once per visit and left alone; enabling or
    // disabling a module invalidates the metrics root, so the one event that
    // really changes the answer does refresh it.
    staleTime: 5 * 60_000,
  });

  const values = useMemo(
    () => (latest.data ? [...latest.data.values].sort(compareLatest) : []),
    [latest.data],
  );

  // Split by shape, not by module name: a scalar belongs in a tile and a set
  // belongs in a table. The module set is what decides, because that is
  // where the knowledge of *which* label a module is keyed by lives.
  const tiles = useMemo(
    () => values.filter((value) => !RESOURCE_MODULES.has(value.series.module)),
    [values],
  );
  const panelled = useMemo(
    () => values.filter((value) => RESOURCE_MODULES.has(value.series.module)),
    [values],
  );

  // Modules whose last collection raised an error. Usually empty; when it is
  // not, it is the explanation for an otherwise inexplicable empty page.
  const failing = useMemo(
    () => (status.data?.modules ?? []).filter((module) => module.consecutive_failures > 0),
    [status.data],
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
            failing={failing}
          />
        ) : (
          <>
            {/* Scalars as tiles, sets as tables. A host with three
                filesystems, four disks and two interfaces produces
                fifty-five labelled series, and fifty-five tiles is a wall
                with the number somebody wanted somewhere in it. See
                resource-panels.tsx. */}
            {tiles.length > 0 ? (
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {tiles.map((value) => (
                  <LatestTile key={value.series.series_id} latest={value} />
                ))}
              </div>
            ) : null}
            {panelled.length > 0 ? <ResourcePanels values={panelled} /> : null}
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
 * Five distinguishable causes, because they have five different remedies and a
 * single "no data" message would send an operator to the wrong one. The order
 * is the order of likelihood for a host that was just set up, except for the
 * first — a collector that is failing is checked before everything else,
 * because when it applies it is the answer and no amount of enabling modules
 * or restarting agents will change it.
 */
function NoCurrentData({
  host,
  freshnessSeconds,
  sampleCount,
  lastSampleAt,
  failing,
}: {
  host: Host;
  freshnessSeconds: number;
  sampleCount: number | null;
  lastSampleAt: string | null;
  /** Modules whose last collection raised an error. Usually empty. */
  failing: ModuleMetricStatus[];
}) {
  const minutes = Math.max(1, Math.round(freshnessSeconds / 60));

  // A collector that is failing. Checked first because it is the one cause
  // the other four messages would actively mislead about: "turn on a module"
  // is wrong advice for a module that is on and erroring, and the agent
  // being connected makes the "agent is offline" reading wrong too.
  if (failing.length > 0) {
    return (
      <Alert
        variant="critical"
        title={
          failing.length === 1
            ? `${failing[0]?.module_name ?? 'A module'} is failing to collect`
            : `${String(failing.length)} modules are failing to collect`
        }
      >
        <p>
          The agent is running these modules and each attempt is raising an error, so
          nothing is being measured. The reasons are the agent&rsquo;s own, reported from
          the host:
        </p>
        <ul className="mt-2 space-y-1">
          {failing.map((module) => (
            <li key={module.module} className="text-sm break-words">
              <strong className="font-medium">{module.module_name}</strong>
              {module.last_error === null ? '' : ` — ${module.last_error}`}
            </li>
          ))}
        </ul>
      </Alert>
    );
  }

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
function ModuleActivity({ status }: { status: MetricsStatus }) {
  const failing = status.modules.filter((module) => module.consecutive_failures > 0);

  return (
    <div className="border-border-subtle border-t pt-5">
      <div className="flex items-center gap-2">
        <Activity aria-hidden="true" className="text-text-faint size-4" />
        <h3 className="text-text-primary text-sm font-semibold">Collection</h3>
        <span className="text-text-faint text-xs">
          {status.sample_count.toLocaleString()} measurement
          {status.sample_count === 1 ? '' : 's'} stored
        </span>
        {failing.length > 0 ? (
          <span className="text-status-critical text-xs font-medium">
            {failing.length} failing
          </span>
        ) : null}
      </div>
      <dl className="mt-3 grid gap-x-6 gap-y-3 sm:grid-cols-2 lg:grid-cols-3">
        {status.modules.map((module) => (
          <div key={module.module} className="min-w-0">
            <dt className="text-text-faint text-xs font-medium">{module.module_name}</dt>
            <dd className="mt-0.5 text-sm">
              <ModuleActivityState module={module} />
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

/**
 * One module's line in the Collection list.
 *
 * Four states, and the one that did not exist before Phase M is the
 * important one. "No data" used to be the answer for a module nobody had
 * enabled, a module enabled a second ago, and a module whose collector had
 * raised the same error every interval for a week. Those need three
 * different actions from an operator, and an interface that gave them one
 * sentence was sending two thirds of its readers to the wrong place.
 *
 * The error is **the agent's own words**, rendered as text. React escapes it;
 * nothing here parses it, matches on it, or decides anything from it. It
 * comes from a host that may be compromised and it is shown because an
 * operator needs it, not because it is trusted.
 */
function ModuleActivityState({ module }: { module: ModuleMetricStatus }) {
  if (module.consecutive_failures > 0) {
    return (
      <span className="flex min-w-0 flex-col gap-0.5">
        <span className="text-status-critical flex items-center gap-1.5 font-medium">
          <AlertTriangle aria-hidden="true" className="size-3.5 shrink-0" />
          Failing
          {module.consecutive_failures > 1
            ? ` · ${String(module.consecutive_failures)} collections`
            : ''}
        </span>
        {module.last_error === null ? null : (
          <span className="text-text-secondary text-xs break-words">{module.last_error}</span>
        )}
        {module.last_sample_at === null ? null : (
          // The last time it *worked*, which is what tells an operator
          // whether this started ten minutes or three weeks ago.
          <span className="text-text-faint text-xs">
            Last measurement{' '}
            <time
              dateTime={machineDateTime(module.last_sample_at)}
              title={formatDateTime(module.last_sample_at)}
            >
              {relativeTime(module.last_sample_at)}
            </time>
          </span>
        )}
      </span>
    );
  }

  if (module.last_sample_at !== null) {
    return (
      <time
        dateTime={machineDateTime(module.last_sample_at)}
        title={formatDateTime(module.last_sample_at)}
        className="text-text-primary"
      >
        {relativeTime(module.last_sample_at)}
      </time>
    );
  }

  if (module.last_reported_at !== null) {
    // Heard from, no error, and no samples. A rate collector's first
    // interval, or a module with genuinely nothing to report on this host —
    // no hwmon devices, no watched units. Not "no data", which reads as a
    // problem, and not a time, which there is no sample to report.
    return (
      <span className="text-text-muted">
        Collected, nothing to report{' · '}
        <time
          dateTime={machineDateTime(module.last_reported_at)}
          title={formatDateTime(module.last_reported_at)}
        >
          {relativeTime(module.last_reported_at)}
        </time>
      </span>
    );
  }

  return <span className="text-text-faint">No data</span>;
}
