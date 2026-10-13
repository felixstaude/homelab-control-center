import { useQuery } from '@tanstack/react-query';
import { useMemo } from 'react';
import {
  Area,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip as RechartsTooltip,
  XAxis,
  YAxis,
} from 'recharts';

import { fetchMetricRange } from '@/api/metrics';
import type { MetricSeries } from '@/api/types';
import { ErrorPanel } from '@/components/states/panel-states';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import { queryKeys } from '@/lib/query-keys';
import { axisDomain, formatMetric, formatMetricInline } from '@/routes/hosts/metric-format';
import { toGrid } from '@/routes/hosts/metric-grid';

/**
 * One series over time.
 *
 * Loaded lazily by `host-metrics.tsx`: Recharts is roughly half the
 * application bundle and is needed by this component alone.
 *
 * ## A gap is drawn as a gap
 *
 * §22, and it is the reason this uses `ComposedChart` with
 * `connectNulls={false}` rather than feeding Recharts the points it was given.
 * The API returns **no point** for a period when nothing was collected, and a
 * line chart handed [10:00, 10:01, 14:00] draws a confident straight line
 * across four missing hours. So the series is expanded onto a regular grid
 * with explicit `null` values in the holes, and nulls break the line.
 *
 * The alternative — plotting only real points and letting the line connect
 * them — would make a host that was switched off look like a host that was
 * idling, which is a false claim about a machine.
 *
 * ## A rolled-up tier draws its range, not just its average
 *
 * §19 keeps minima and maxima through downsampling precisely so they can be
 * shown. An hourly point drawn as a single average hides a 100% spike inside a
 * 12% hour, so when the tier is rolled up the chart draws a min–max band
 * behind the average line and says which tier it is.
 */

type Window = { id: string; label: string; seconds: number };

/**
 * How many points to ask for.
 *
 * Matched to a chart a few hundred pixels wide: more points than pixels is
 * data the eye cannot resolve and bytes the browser has to parse. The server
 * picks the storage tier from this budget and the range, which is why the
 * number is a request rather than a limit applied afterwards.
 */
const MAX_POINTS = 600;

/** Human names for the storage tiers. */
const RESOLUTION_LABELS: Record<string, string> = {
  raw: 'every sample',
  '5m': '5-minute averages',
  '1h': 'hourly averages',
};

export default function MetricHistory({
  hostId,
  series,
  window,
}: {
  hostId: string;
  series: MetricSeries;
  window: Window;
}) {
  // The instants are resolved here and deliberately kept out of the query key:
  // a key containing `Date.now()` changes on every render, so every poll would
  // be a cache miss and the chart would unmount and flash empty.
  const range = useQuery({
    queryKey: queryKeys.session.metricRange({
      hostId,
      seriesId: series.series_id,
      window: window.id,
    }),
    queryFn: ({ signal }) => {
      const end = new Date();
      const start = new Date(end.getTime() - window.seconds * 1000);
      return fetchMetricRange(
        hostId,
        {
          seriesId: series.series_id,
          // Always zoned: the server refuses a naive timestamp rather than
          // assuming UTC, so a browser in another zone cannot silently ask
          // for the wrong window.
          start: start.toISOString(),
          end: end.toISOString(),
          maxPoints: MAX_POINTS,
        },
        signal,
      );
    },
    // Long enough that panning between ranges is instant, short enough that a
    // chart left open does not drift far from the tiles above it.
    refetchInterval: 30_000,
    staleTime: 15_000,
  });

  const gridded = useMemo(
    () => (range.data ? toGrid(range.data.points) : []),
    [range.data],
  );

  if (range.isPending) return <Skeleton className="h-64 w-full" />;

  if (range.isError) {
    return (
      <ErrorPanel
        error={range.error}
        title="Could not load the history"
        onRetry={() => {
          void range.refetch();
        }}
      />
    );
  }

  const data = range.data;
  const measured = data.points.length;

  if (measured === 0) {
    return (
      <div
        className="border-border-subtle text-text-muted flex h-64 items-center justify-center
          rounded-lg border border-dashed px-6 text-center text-sm"
      >
        Nothing was collected for this series in {window.label.toLowerCase()}. The chart is
        empty because the period is empty &mdash; not because the values were zero.
      </div>
    );
  }

  const rolled = data.resolution !== 'raw';
  const domain = axisDomain(series.unit);

  return (
    <div className="flex flex-col gap-3">
      <div className="h-64 w-full">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={gridded} margin={{ top: 4, right: 4, bottom: 0, left: -16 }}>
            <defs>
              <linearGradient id="fill-metric-band" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="var(--color-accent)" stopOpacity={0.22} />
                <stop offset="100%" stopColor="var(--color-accent)" stopOpacity={0.06} />
              </linearGradient>
            </defs>
            <XAxis
              dataKey="at"
              type="number"
              scale="time"
              // The real time extent, not the extent of the points. A series
              // that stopped half way through the window must show the empty
              // half rather than stretching to fill the panel.
              domain={[new Date(data.start).getTime(), new Date(data.end).getTime()]}
              tickFormatter={(value: number) => tickLabel(value, window.seconds)}
              stroke="var(--color-text-faint)"
              tick={{ fontSize: 11 }}
              tickLine={false}
              axisLine={false}
              minTickGap={32}
            />
            <YAxis
              stroke="var(--color-text-faint)"
              tick={{ fontSize: 11 }}
              tickLine={false}
              axisLine={false}
              width={56}
              {...(domain === undefined ? {} : { domain })}
              tickFormatter={(value: number) => {
                const formatted = formatMetric(value, series.unit);
                return formatted.unit === undefined
                  ? formatted.value
                  : `${formatted.value}${formatted.unit === '%' ? '%' : ''}`;
              }}
            />
            <RechartsTooltip
              contentStyle={{
                backgroundColor: 'var(--color-surface-overlay)',
                border: '1px solid var(--color-border-default)',
                borderRadius: 'var(--radius-md)',
                fontSize: 12,
                color: 'var(--color-text-primary)',
              }}
              labelStyle={{ color: 'var(--color-text-muted)' }}
              labelFormatter={(value) =>
                new Date(Number(value)).toLocaleString(undefined, {
                  dateStyle: 'medium',
                  timeStyle: 'medium',
                })
              }
              formatter={(value, name) => {
                if (typeof value !== 'number') return ['—', String(name)];
                return [formatMetricInline(value, series.unit), String(name)];
              }}
            />
            {rolled ? (
              <>
                {/* The band is drawn as a stacked pair: a transparent area up
                 * to the minimum, then a filled one spanning the range. Two
                 * stacked areas rather than one with an array value, because
                 * Recharts has no native range area and faking it with a
                 * filled `maximum` would paint over the zero line. */}
                <Area
                  type="monotone"
                  dataKey="minimum"
                  name="Minimum"
                  stackId="band"
                  stroke="none"
                  fill="transparent"
                  connectNulls={false}
                  isAnimationActive={false}
                />
                <Area
                  type="monotone"
                  dataKey="span"
                  name="Range"
                  stackId="band"
                  stroke="none"
                  fill="url(#fill-metric-band)"
                  connectNulls={false}
                  isAnimationActive={false}
                />
              </>
            ) : null}
            <Line
              type="monotone"
              dataKey="value"
              name={series.metric_name}
              stroke="var(--color-accent)"
              strokeWidth={2}
              dot={false}
              // The whole point: a null breaks the line, so a period with no
              // measurements is a visible gap rather than an interpolation.
              connectNulls={false}
              isAnimationActive={false}
            />
          </ComposedChart>
        </ResponsiveContainer>
      </div>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs">
        <span className="flex items-center gap-1.5">
          <span className="bg-accent h-0.5 w-4 rounded-full" aria-hidden="true" />
          <span className="text-text-muted">{series.metric_name}</span>
        </span>
        {rolled ? (
          <span className="flex items-center gap-1.5">
            <span
              className="bg-accent/20 border-accent-muted h-3 w-4 rounded-sm border"
              aria-hidden="true"
            />
            <span className="text-text-muted">Minimum to maximum</span>
          </span>
        ) : null}
        <Badge variant="neutral">
          {RESOLUTION_LABELS[data.resolution] ?? data.resolution}
        </Badge>
        <span className="text-text-faint">
          {measured.toLocaleString()} point{measured === 1 ? '' : 's'}
        </span>
        {data.truncated ? (
          <span className="text-status-warn">
            Shortened to fit the point budget — narrow the range to see all of it.
          </span>
        ) : null}
      </div>
    </div>
  );
}

/**
 * An axis tick label scaled to the window.
 *
 * A seven-day chart labelled with seconds is unreadable, and an hour labelled
 * with dates says nothing. The threshold is two days because that is where
 * the time of day stops being the useful part.
 */
function tickLabel(value: number, windowSeconds: number): string {
  const at = new Date(value);
  if (windowSeconds > 2 * 86_400) {
    return at.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }
  return at.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}
