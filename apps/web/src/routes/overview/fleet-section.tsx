import { Activity, Cpu, MemoryStick, Server } from 'lucide-react';
import { Link } from 'react-router-dom';

import type { Host } from '@/api/types';
import { EmptyPanel, ErrorPanel } from '@/components/states/panel-states';
import { Badge } from '@/components/ui/badge';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { MetricTile } from '@/components/ui/metric-tile';
import { Skeleton } from '@/components/ui/skeleton';
import { StatusBadge, StatusDot, type HealthStatus } from '@/components/ui/status-indicator';
import { relativeTime } from '@/lib/datetime';
import { formatMetric } from '@/routes/hosts/metric-format';
import {
  FLEET_SAMPLE_LIMIT,
  fleetPeak,
  hostValue,
  reportingCount,
  useFleet,
  type FleetHost,
} from '@/routes/overview/use-fleet';

/**
 * The part of the Overview that is real.
 *
 * ## Why this is a separate component from the sample cards
 *
 * §25: sample values must not be mixed with real totals, and the page used to
 * be entirely fixtures under one banner. Now that some of it is measured, the
 * two cannot share a region — a real host count sitting under a "sample data"
 * banner is the same class of lie as a fictional one presented as real, just
 * in the other direction. So the real section stands on its own and the
 * fixtures keep their banner.
 *
 * ## Columns that have no data source are absent, not empty
 *
 * The fixture table had Disk and OS columns. Neither is collected in this
 * phase, and a column of em dashes implies the value exists and is merely
 * missing for these hosts. They are gone until there is something to put in
 * them; `docs/ROADMAP.md` says when.
 *
 * ## A fleet peak names the host
 *
 * "86%" prompts "on what?", and a dashboard that cannot answer that is a
 * number somebody has to go and look up. The tile carries the host name.
 */
export function FleetSection() {
  const fleet = useFleet();

  if (fleet.isPending) {
    return (
      <section aria-labelledby="fleet-heading" className="mb-6 flex flex-col gap-6">
        <h3 id="fleet-heading" className="sr-only">
          Fleet
        </h3>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <Skeleton className="h-28" />
          <Skeleton className="h-28" />
          <Skeleton className="h-28" />
          <Skeleton className="h-28" />
        </div>
      </section>
    );
  }

  if (fleet.isError) {
    return (
      <section aria-labelledby="fleet-heading" className="mb-6">
        <h3 id="fleet-heading" className="sr-only">
          Fleet
        </h3>
        <Card>
          <CardContent className="px-0">
            <ErrorPanel error={fleet.error} title="Could not load the fleet" />
          </CardContent>
        </Card>
      </section>
    );
  }

  const connected = fleet.hosts.filter((host) => host.connectivity === 'connected').length;
  const reporting = reportingCount(fleet.entries);
  const cpuPeak = fleetPeak(fleet.entries, 'cpu', 'utilisation');
  const memoryPeak = fleetPeak(fleet.entries, 'memory', 'utilisation');

  return (
    <section aria-labelledby="fleet-heading" className="mb-6 flex flex-col gap-6">
      <h3 id="fleet-heading" className="sr-only">
        Fleet
      </h3>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <MetricTile
          label="Hosts"
          value={String(fleet.total)}
          caption={`${String(connected)} connected · ${String(fleet.total - connected)} not connected`}
          icon={<Server className="size-4" />}
        />
        <MetricTile
          label="Reporting"
          value={String(reporting)}
          // The fleet-level version of "is this working". A host can be
          // connected and collecting nothing, which is the resting state of a
          // newly enrolled machine, so the two counts are not the same number.
          caption={
            fleet.partial
              ? `of ${String(fleet.measured)} hosts checked`
              : `of ${String(fleet.total)} host${fleet.total === 1 ? '' : 's'}`
          }
          icon={<Activity className="size-4" />}
        />
        <PeakTile label="Peak CPU" peak={cpuPeak} icon={<Cpu className="size-4" />} />
        <PeakTile
          label="Peak memory"
          peak={memoryPeak}
          icon={<MemoryStick className="size-4" />}
        />
      </div>

      {fleet.partial ? (
        <p className="text-text-faint text-xs">
          Current values were read from the first {String(FLEET_SAMPLE_LIMIT)} hosts of{' '}
          {String(fleet.total)}. The peaks above are the highest among those, not across the
          whole fleet.
        </p>
      ) : null}

      <Card>
        <CardHeader>
          <div className="min-w-0">
            <CardTitle>Hosts</CardTitle>
            <CardDescription>
              Measured on each host by its agent. A dash means the value is not being
              collected or has not arrived &mdash; never that it is zero.
            </CardDescription>
          </div>
          <Badge variant="ok">Live</Badge>
        </CardHeader>
        <CardContent className="px-0 pb-0">
          {fleet.hosts.length === 0 ? (
            <EmptyPanel title="No hosts yet">
              Register a host and enrol its agent to start collecting.
            </EmptyPanel>
          ) : (
            <FleetTable entries={fleet.entries} />
          )}
        </CardContent>
      </Card>
    </section>
  );
}

/** A fleet peak, or a stated absence. */
function PeakTile({
  label,
  peak,
  icon,
}: {
  label: string;
  peak: { host: Host; value: number; recordedAt: string } | null;
  icon: React.ReactNode;
}) {
  if (peak === null) {
    // No host reported this metric — the common case for a module nobody
    // enabled. A tile reading "0%" would be a claim about every machine in
    // the fleet at once.
    return (
      <div
        className="bg-surface-raised border-border-subtle surface-lit flex flex-col gap-3
          rounded-xl border p-4"
      >
        <div className="flex items-center justify-between gap-2">
          <span className="text-text-muted text-xs font-medium tracking-wide uppercase">
            {label}
          </span>
          <span className="text-text-faint">{icon}</span>
        </div>
        <div>
          <div className="text-text-faint text-2xl leading-none font-semibold">—</div>
          <p className="text-text-faint mt-1.5 text-xs">Not being collected</p>
        </div>
      </div>
    );
  }

  const formatted = formatMetric(peak.value, 'percent');
  return (
    <MetricTile
      label={label}
      value={formatted.value}
      {...(formatted.unit === undefined ? {} : { unit: formatted.unit })}
      caption={`${peak.host.name} · ${relativeTime(peak.recordedAt)}`}
      icon={icon}
    />
  );
}

/** Connectivity as the health vocabulary the status components speak. */
function healthOf(host: Host): HealthStatus {
  if (!host.is_enabled) return 'unknown';
  switch (host.connectivity) {
    case 'connected':
      return 'ok';
    case 'stale':
      return 'warn';
    case 'offline':
      // Not `critical`: a homelab host being off is normal, and a dashboard
      // that painted every powered-down machine red would train its reader to
      // ignore red.
      return 'unknown';
  }
}

function FleetTable({ entries }: { entries: readonly FleetHost[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <caption className="sr-only">
          Registered hosts with their connectivity and current resource utilisation.
        </caption>
        <thead>
          <tr className="border-border-subtle border-y">
            <Th className="px-5 text-left">Host</Th>
            <Th className="px-3 text-left">Status</Th>
            <Th className="px-3 text-right">CPU</Th>
            <Th className="px-3 text-right">Memory</Th>
            <Th className="hidden px-5 text-right lg:table-cell">Uptime</Th>
          </tr>
        </thead>
        <tbody className="divide-border-subtle divide-y">
          {entries.map((entry) => (
            <tr key={entry.host.host_id} className="hover:bg-surface-hover/40 transition-colors">
              <th scope="row" className="px-5 py-3 text-left font-normal">
                <div className="flex items-center gap-2.5">
                  <StatusDot status={healthOf(entry.host)} />
                  <Link
                    to={`/hosts/${entry.host.host_id}`}
                    className="text-text-primary hover:text-accent font-medium"
                  >
                    {entry.host.name}
                  </Link>
                </div>
                {entry.host.environment === null ? null : (
                  <span className="text-text-faint mt-0.5 block text-xs">
                    {entry.host.environment}
                  </span>
                )}
              </th>
              <td className="px-3 py-3">
                <StatusBadge
                  status={healthOf(entry.host)}
                  label={
                    !entry.host.is_enabled
                      ? 'Disabled'
                      : entry.host.connectivity === 'offline'
                        ? 'Offline'
                        : undefined
                  }
                />
              </td>
              <td className="px-3 py-3 text-right">
                <UtilisationCell value={hostValue(entry, 'cpu', 'utilisation')} />
              </td>
              <td className="px-3 py-3 text-right">
                <UtilisationCell value={hostValue(entry, 'memory', 'utilisation')} />
              </td>
              <td className="tabular text-text-muted hidden px-5 py-3 text-right lg:table-cell">
                <UptimeCell value={hostValue(entry, 'uptime', 'seconds')} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Th({ className, children }: { className?: string; children: React.ReactNode }) {
  return (
    <th
      scope="col"
      className={`text-text-faint py-2.5 text-xs font-medium tracking-wide uppercase ${className ?? ''}`}
    >
      {children}
    </th>
  );
}

/**
 * A utilisation percentage with a thin bar, or a stated absence.
 *
 * `null` renders an em dash. A monitoring tool must never display absent data
 * as 0%, which reads as "idle and healthy" — the most dangerous possible
 * misreading, because it is the one that looks fine.
 */
function UtilisationCell({ value }: { value: number | null }) {
  if (value === null) {
    return (
      <span className="text-text-faint text-xs" title="Not collected, or not yet reported">
        —
      </span>
    );
  }

  const formatted = formatMetric(value, 'percent');
  return (
    <div className="inline-flex w-20 flex-col items-end gap-1">
      <span className="tabular text-text-secondary text-xs">
        {formatted.value}
        {formatted.unit}
      </span>
      <span
        className="bg-surface-overlay h-1 w-full overflow-hidden rounded-full"
        aria-hidden="true"
      >
        <span
          className="bg-text-faint block h-full rounded-full"
          // Clamped, because the bar is a visual and a value above 100 would
          // overflow its track. The number beside it is never clamped.
          style={{ width: `${String(Math.min(100, Math.max(0, value)))}%` }}
        />
      </span>
    </div>
  );
}

function UptimeCell({ value }: { value: number | null }) {
  if (value === null) return <span className="text-text-faint text-xs">—</span>;
  return <>{formatMetric(value, 'seconds').value}</>;
}
