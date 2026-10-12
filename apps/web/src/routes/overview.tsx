import { Boxes, Cpu, HardDrive, MemoryStick, Server } from 'lucide-react';
import { lazy, Suspense } from 'react';

import { MockDataBanner } from '@/components/mock-data-banner';
import { Skeleton } from '@/components/ui/skeleton';
import { PageHeader } from '@/components/page-header';
import { Badge } from '@/components/ui/badge';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { MetricTile } from '@/components/ui/metric-tile';
import { StatusBadge, StatusDot } from '@/components/ui/status-indicator';
import { MOCK_CONTAINERS, MOCK_HOSTS, MOCK_LOG_ENTRIES } from '@/mocks/overview';

// Recharts is roughly half the bundle and is used by one card, so it is kept
// off the critical path. See resource-chart.tsx.
const ResourceChart = lazy(() => import('@/routes/overview/resource-chart'));

/**
 * Overview page.
 *
 * Every value on this page comes from `src/mocks/overview.ts` and is static
 * sample data, declared as such by the banner at the top. The page exists to
 * demonstrate the intended information density and visual hierarchy of the
 * finished product; it performs no data fetching.
 */
export function OverviewPage() {
  return (
    <>
      <PageHeader
        title="Overview"
        description="Fleet-wide health across hosts, containers and network devices. This page is a design reference built from static fixtures — the real data pipeline is not implemented."
      />

      <MockDataBanner className="mb-6" />

      {/* --- Headline metrics --- */}
      <section aria-labelledby="fleet-metrics" className="mb-6">
        <h3 id="fleet-metrics" className="sr-only">
          Fleet metrics
        </h3>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <MetricTile
            label="Hosts"
            value="4"
            caption="3 reporting · 1 not reporting"
            icon={<Server className="size-4" />}
          />
          <MetricTile
            label="Containers"
            value="27"
            caption="25 running · 2 not running"
            icon={<Boxes className="size-4" />}
          />
          <MetricTile
            label="Peak CPU"
            value="72"
            unit="%"
            caption="sample-compute, 14:00"
            icon={<Cpu className="size-4" />}
          />
          <MetricTile
            label="Peak memory"
            value="86"
            unit="%"
            caption="sample-compute, now"
            icon={<MemoryStick className="size-4" />}
          />
        </div>
      </section>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-3">
        {/* --- Resource chart --- */}
        <Card className="xl:col-span-2">
          <CardHeader>
            <div>
              <CardTitle>Resource utilisation</CardTitle>
              <CardDescription>
                Sample 24-hour series for one host. Historical metrics storage
                arrives in Phase 3.
              </CardDescription>
            </div>
            <Badge variant="warn">Sample</Badge>
          </CardHeader>
          <CardContent>
            {/* Suspense fallback matches the chart's final height so the
             * card does not jump when the chunk resolves. */}
            <Suspense
              fallback={
                <div role="status" aria-label="Loading chart">
                  <Skeleton className="h-64 w-full" />
                </div>
              }
            >
              <ResourceChart />
            </Suspense>
          </CardContent>
        </Card>

        {/* --- Recent log entries --- */}
        <Card>
          <CardHeader>
            <div>
              <CardTitle>Recent events</CardTitle>
              <CardDescription>
                Sample entries. Log collection arrives in Phase 3.
              </CardDescription>
            </div>
            <Badge variant="warn">Sample</Badge>
          </CardHeader>
          <CardContent className="px-0 pb-0">
            <ul className="divide-border-subtle divide-y">
              {MOCK_LOG_ENTRIES.map((entry) => (
                <li key={entry.id} className="px-5 py-3">
                  <div className="mb-1.5 flex items-center gap-2">
                    <Badge
                      variant={
                        entry.level === 'ERROR'
                          ? 'critical'
                          : entry.level === 'WARN'
                            ? 'warn'
                            : 'neutral'
                      }
                    >
                      {entry.level}
                    </Badge>
                    <span className="text-text-faint truncate font-mono text-[11px]">
                      {entry.source}
                    </span>
                    <time
                      className="tabular text-text-faint ml-auto shrink-0 text-[11px]"
                      dateTime={entry.timestamp}
                    >
                      {entry.timestamp.slice(11, 19)}
                    </time>
                  </div>
                  {/* Log messages are preserved verbatim — never reformatted
                   * or summarised. See the product vision. */}
                  <p className="text-text-secondary font-mono text-xs leading-relaxed break-words">
                    {entry.message}
                  </p>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      </div>

      {/* --- Hosts table --- */}
      <Card className="mt-6">
        <CardHeader>
          <div>
            <CardTitle>Hosts</CardTitle>
            <CardDescription>
              Sample inventory. Host registration arrives in Phase 1.
            </CardDescription>
          </div>
          <Badge variant="warn">Sample</Badge>
        </CardHeader>
        <CardContent className="px-0 pb-0">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <caption className="sr-only">
                Sample host inventory with status and resource utilisation. Not
                real data.
              </caption>
              <thead>
                <tr className="border-border-subtle border-y">
                  <th
                    scope="col"
                    className="text-text-faint px-5 py-2.5 text-left text-xs font-medium tracking-wide uppercase"
                  >
                    Host
                  </th>
                  <th
                    scope="col"
                    className="text-text-faint px-3 py-2.5 text-left text-xs font-medium tracking-wide uppercase"
                  >
                    Status
                  </th>
                  <th
                    scope="col"
                    className="text-text-faint hidden px-3 py-2.5 text-left text-xs font-medium tracking-wide uppercase md:table-cell"
                  >
                    OS
                  </th>
                  <th
                    scope="col"
                    className="text-text-faint px-3 py-2.5 text-right text-xs font-medium tracking-wide uppercase"
                  >
                    CPU
                  </th>
                  <th
                    scope="col"
                    className="text-text-faint px-3 py-2.5 text-right text-xs font-medium tracking-wide uppercase"
                  >
                    Memory
                  </th>
                  <th
                    scope="col"
                    className="text-text-faint hidden px-3 py-2.5 text-right text-xs font-medium tracking-wide uppercase sm:table-cell"
                  >
                    Disk
                  </th>
                  <th
                    scope="col"
                    className="text-text-faint hidden px-5 py-2.5 text-right text-xs font-medium tracking-wide uppercase lg:table-cell"
                  >
                    Uptime
                  </th>
                </tr>
              </thead>
              <tbody className="divide-border-subtle divide-y">
                {MOCK_HOSTS.map((host) => (
                  <tr key={host.id} className="hover:bg-surface-hover/40 transition-colors">
                    <th scope="row" className="px-5 py-3 text-left font-normal">
                      <div className="flex items-center gap-2.5">
                        <StatusDot status={host.status} />
                        <span className="text-text-primary font-medium">{host.hostname}</span>
                      </div>
                      <span className="text-text-faint mt-0.5 block text-xs">
                        {host.containerCount} containers
                      </span>
                    </th>
                    <td className="px-3 py-3">
                      <StatusBadge
                        status={host.status}
                        label={host.status === 'unknown' ? 'No data' : undefined}
                      />
                    </td>
                    <td className="text-text-muted hidden px-3 py-3 md:table-cell">
                      {host.os}
                    </td>
                    <td className="px-3 py-3 text-right">
                      <UtilisationCell percent={host.cpuPercent} unknown={host.status === 'unknown'} />
                    </td>
                    <td className="px-3 py-3 text-right">
                      <UtilisationCell
                        percent={host.memoryPercent}
                        unknown={host.status === 'unknown'}
                      />
                    </td>
                    <td className="hidden px-3 py-3 text-right sm:table-cell">
                      <UtilisationCell percent={host.diskPercent} unknown={false} />
                    </td>
                    <td className="tabular text-text-muted hidden px-5 py-3 text-right lg:table-cell">
                      {host.uptime}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>

      {/* --- Containers --- */}
      <Card className="mt-6">
        <CardHeader>
          <div>
            <CardTitle>Containers needing attention</CardTitle>
            <CardDescription>
              Sample container states. Docker discovery arrives in Phase 4.
            </CardDescription>
          </div>
          <Badge variant="warn">Sample</Badge>
        </CardHeader>
        <CardContent className="px-0 pb-0">
          <ul className="divide-border-subtle divide-y">
            {MOCK_CONTAINERS.map((container) => (
              <li
                key={container.id}
                className="hover:bg-surface-hover/40 flex flex-wrap items-center gap-3 px-5 py-3 transition-colors"
              >
                <StatusDot status={container.status} />
                <div className="min-w-0 flex-1">
                  <div className="text-text-primary truncate text-sm font-medium">
                    {container.name}
                  </div>
                  <div className="text-text-faint truncate font-mono text-xs">
                    {container.image} · {container.host}
                  </div>
                </div>
                <HardDrive className="text-text-faint hidden size-3.5 sm:block" aria-hidden="true" />
                <Badge
                  variant={
                    container.state === 'running'
                      ? 'ok'
                      : container.state === 'restarting'
                        ? 'warn'
                        : 'critical'
                  }
                >
                  {container.state}
                </Badge>
                <span className="tabular text-text-faint w-12 shrink-0 text-right text-xs">
                  {container.since}
                </span>
              </li>
            ))}
          </ul>
        </CardContent>
      </Card>
    </>
  );
}

/**
 * A utilisation percentage with a thin bar.
 *
 * Renders an explicit em dash when the value is unknown. A monitoring tool
 * must never display absent data as 0%, which reads as "idle and healthy".
 */
function UtilisationCell({ percent, unknown }: { percent: number; unknown: boolean }) {
  if (unknown) {
    return <span className="text-text-faint text-xs">—</span>;
  }

  return (
    <div className="inline-flex w-20 flex-col items-end gap-1">
      <span className="tabular text-text-secondary text-xs">{percent}%</span>
      <span
        className="bg-surface-overlay h-1 w-full overflow-hidden rounded-full"
        aria-hidden="true"
      >
        <span
          className="bg-text-faint block h-full rounded-full"
          style={{ width: `${percent}%` }}
        />
      </span>
    </div>
  );
}
