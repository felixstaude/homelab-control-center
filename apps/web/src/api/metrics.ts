/**
 * Metric query calls.
 *
 * All reads. There is deliberately no write function here and there is no
 * endpoint for one to call: metrics arrive over the authenticated agent
 * connection, and a path that accepted a measurement from a browser session
 * would be a way to fabricate history.
 *
 * ## Nothing in this module substitutes a value
 *
 * The server returns an empty list where it has no data, and this module
 * passes that through. No default of zero, no last-known value carried
 * forward, no interpolation across a gap. A caller that wants to render
 * "unknown" has to be given the absence to render it from, and a client that
 * helpfully supplied `0` would make that impossible — "CPU 0%" is a claim
 * about a machine.
 */

import { apiRequest } from '@/api/client';
import type {
  LatestMetrics,
  MetricRange,
  MetricRangeParams,
  MetricSeriesList,
  MetricsStatus,
} from '@/api/types';

/**
 * The newest value of each series, if it is recent enough to be current.
 *
 * `freshnessSeconds` widens or narrows the window; the response repeats the
 * value that was applied, so the interface can state the claim it is making
 * rather than implying "now".
 */
export function fetchLatestMetrics(
  hostId: string,
  freshnessSeconds?: number,
  signal?: AbortSignal,
): Promise<LatestMetrics> {
  const query =
    freshnessSeconds === undefined
      ? ''
      : `?freshness_seconds=${String(freshnessSeconds)}`;
  return apiRequest<LatestMetrics>(
    `/hosts/${encodeURIComponent(hostId)}/metrics/latest${query}`,
    { signal },
  );
}

/** Every series a host has produced, including ones that have stopped. */
export function fetchMetricSeries(
  hostId: string,
  signal?: AbortSignal,
): Promise<MetricSeriesList> {
  return apiRequest<MetricSeriesList>(
    `/hosts/${encodeURIComponent(hostId)}/metrics/series`,
    { signal },
  );
}

/**
 * One series over a time range.
 *
 * `resolution` is left out by default on purpose. The server picks the tier
 * from the range and the point budget, and that choice always fits; naming one
 * can return a truncated series. The response says which tier answered either
 * way.
 *
 * `start` and `end` must carry a zone. The server refuses a naive timestamp
 * rather than assuming UTC, so a browser in another zone cannot silently ask
 * for a window hours away from the one it meant.
 */
export function fetchMetricRange(
  hostId: string,
  params: MetricRangeParams,
  signal?: AbortSignal,
): Promise<MetricRange> {
  const query = new URLSearchParams({
    series_id: String(params.seriesId),
    start: params.start,
    end: params.end,
  });
  if (params.maxPoints !== undefined) query.set('max_points', String(params.maxPoints));
  if (params.resolution !== undefined) query.set('resolution', params.resolution);
  return apiRequest<MetricRange>(
    `/hosts/${encodeURIComponent(hostId)}/metrics/range?${query.toString()}`,
    { signal },
  );
}

/**
 * Whether anything is actually arriving for a host.
 *
 * The question a chart cannot answer. An empty chart could mean "nothing
 * collected", "collection is off" or "the agent is offline"; this answers the
 * first, the monitoring endpoint answers the second, and the host endpoint the
 * third.
 */
export function fetchMetricsStatus(
  hostId: string,
  signal?: AbortSignal,
): Promise<MetricsStatus> {
  return apiRequest<MetricsStatus>(
    `/hosts/${encodeURIComponent(hostId)}/metrics/status`,
    { signal },
  );
}
