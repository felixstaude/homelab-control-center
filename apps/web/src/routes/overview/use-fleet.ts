import { useQueries, useQuery } from '@tanstack/react-query';

import { fetchLatestMetrics } from '@/api/metrics';
import { fetchHosts } from '@/api/hosts';
import type { Host, LatestValue } from '@/api/types';
import { queryKeys } from '@/lib/query-keys';

/**
 * Fleet-wide monitoring data, assembled in the browser.
 *
 * ## Why this fans out rather than calling one endpoint
 *
 * There is no fleet-wide metrics endpoint. Adding one is the right shape
 * eventually — a dashboard asking the server N+1 questions is a pattern that
 * stops working — and it is deliberately not done here, because a fleet
 * aggregate has product decisions in it that the API should not guess:
 * whether a host with CPU disabled counts toward "hosts reporting", whether a
 * peak is over hosts or over series, what happens when half the fleet has one
 * module enabled. Those belong in a designed endpoint rather than in the first
 * thing that made a number appear.
 *
 * So the browser asks, with a bound, and the interface states what it
 * measured. See `docs/PERFORMANCE.md` for the deferral.
 *
 * ## The bound is not a performance tweak, it is what keeps the page honest
 *
 * {@link FLEET_SAMPLE_LIMIT} hosts are queried. A larger installation gets a
 * number computed from a subset — and {@link FleetSummary.partial} says so, so
 * the tile can read "peak across 50 of 120 hosts" instead of silently
 * presenting a sample as a fleet maximum. A page that quietly aggregated
 * whatever it managed to fetch would be wrong in a way nobody could see.
 */

/**
 * How many hosts the overview queries for current values.
 *
 * Fifty. Comfortably above a homelab fleet, and low enough that the page makes
 * a bounded number of requests on a deployment that has grown past what this
 * approach suits — at which point the number is reported as partial rather
 * than becoming slowly wrong.
 */
export const FLEET_SAMPLE_LIMIT = 50;

/** How often the fleet view refetches while the tab is visible. */
const POLL_INTERVAL_MS = 15_000;

/** One host's contribution to the fleet view. */
export type FleetHost = {
  host: Host;
  /**
   * Current values for this host, or `null` while they are still loading or
   * if the request failed.
   *
   * `null` and `[]` are different and both are kept: an empty array means the
   * host reported nothing current, which is a fact, and `null` means this
   * client does not know — which must not be rendered as the first.
   */
  latest: LatestValue[] | null;
};

export type FleetSummary = {
  /** Every host, from the registry. Authoritative and cheap. */
  hosts: Host[];
  /** Total hosts the server reports, which may exceed what was queried. */
  total: number;
  /** Per-host current values, for the hosts that were queried. */
  entries: FleetHost[];
  /** True when more hosts exist than were queried for metrics. */
  partial: boolean;
  /** How many hosts the metric figures were computed from. */
  measured: number;
  isPending: boolean;
  isError: boolean;
  error: unknown;
};

export function useFleet(): FleetSummary {
  const hosts = useQuery({
    queryKey: queryKeys.session.hostList({
      search: '',
      lifecycle: '',
      enabled: '',
      connectivity: '',
      limit: FLEET_SAMPLE_LIMIT,
      offset: 0,
    }),
    queryFn: ({ signal }) => fetchHosts({ limit: FLEET_SAMPLE_LIMIT, offset: 0 }, signal),
    refetchInterval: POLL_INTERVAL_MS,
  });

  const items = hosts.data?.items ?? [];

  // One query per host, which is what makes them independently cacheable: the
  // host detail page asks for the same key, so navigating there is instant and
  // does not re-request what this page already has.
  const latest = useQueries({
    queries: items.map((host) => ({
      queryKey: queryKeys.session.latestMetrics(host.host_id),
      queryFn: ({ signal }: { signal: AbortSignal }) =>
        fetchLatestMetrics(host.host_id, undefined, signal),
      refetchInterval: POLL_INTERVAL_MS,
    })),
  });

  const entries: FleetHost[] = items.map((host, index) => ({
    host,
    // A failed or in-flight request contributes `null`, never an empty list:
    // "this host reported nothing" and "we do not know" are different claims
    // and the second must not be rendered as the first.
    latest: latest[index]?.data?.values ?? null,
  }));

  const total = hosts.data?.total ?? items.length;

  return {
    hosts: items,
    total,
    entries,
    partial: total > items.length,
    measured: entries.filter((entry) => entry.latest !== null).length,
    isPending: hosts.isPending,
    isError: hosts.isError,
    error: hosts.error,
  };
}

/**
 * The highest current value of one metric across the fleet, and whose it is.
 *
 * `null` when no host reported that metric — which is the common case for a
 * module nobody enabled, and must render as "no data" rather than as zero.
 *
 * Returns the host as well as the number, because a fleet peak with no name
 * attached is a statistic an operator cannot act on: "86%" prompts "on what?"
 * and the answer is already here.
 */
export function fleetPeak(
  entries: readonly FleetHost[],
  module: string,
  metric: string,
): { host: Host; value: number; recordedAt: string } | null {
  let best: { host: Host; value: number; recordedAt: string } | null = null;
  for (const entry of entries) {
    if (entry.latest === null) continue;
    for (const value of entry.latest) {
      if (value.series.module !== module || value.series.metric !== metric) continue;
      // Unlabelled series only. A per-core utilisation series would otherwise
      // make the fleet peak the busiest *core*, which is a different and much
      // more alarming number than the busiest host.
      if (Object.keys(value.series.labels).length > 0) continue;
      if (best === null || value.value > best.value) {
        best = { host: entry.host, value: value.value, recordedAt: value.recorded_at };
      }
    }
  }
  return best;
}

/**
 * One host's current value for a metric, or `null`.
 *
 * The same distinction as {@link FleetHost.latest}: `null` covers both "not
 * known yet" and "not reported", because from a table cell's point of view
 * they render the same — as an absence. What must never happen is either of
 * them rendering as a number.
 */
export function hostValue(
  entry: FleetHost,
  module: string,
  metric: string,
): number | null {
  if (entry.latest === null) return null;
  const found = entry.latest.find(
    (value) =>
      value.series.module === module &&
      value.series.metric === metric &&
      Object.keys(value.series.labels).length === 0,
  );
  return found?.value ?? null;
}

/** How many of the queried hosts are currently reporting any metric at all. */
export function reportingCount(entries: readonly FleetHost[]): number {
  return entries.filter((entry) => entry.latest !== null && entry.latest.length > 0).length;
}
