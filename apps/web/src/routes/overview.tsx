import { Boxes, HardDrive } from 'lucide-react';
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
import { StatusDot } from '@/components/ui/status-indicator';
import { MOCK_CONTAINERS, MOCK_LOG_ENTRIES } from '@/mocks/overview';
import { FleetSection } from '@/routes/overview/fleet-section';

// Recharts is roughly half the bundle and is used by one card, so it is kept
// off the critical path. See resource-chart.tsx.
const ResourceChart = lazy(() => import('@/routes/overview/resource-chart'));

/**
 * The fixtures that are genuinely not healthy.
 *
 * Computed once at module scope rather than per render: the fixtures are
 * static, so filtering on every render would be work that cannot produce a
 * different answer — and a new array identity each time is exactly what makes
 * a memoised child re-render for nothing.
 */
const ATTENTION_CONTAINERS = MOCK_CONTAINERS.filter(
  (container) => container.state !== 'running',
);

/**
 * Overview page.
 *
 * ## Two regions, and the boundary between them is the point
 *
 * The top is **measured**: host counts, connectivity, and the current CPU and
 * memory of every registered host, read from the agents. The bottom is
 * **sample data** for the features that do not exist yet — container
 * discovery, log collection — and keeps the banner this whole page used to sit
 * under.
 *
 * They are deliberately not interleaved. §25 forbids mixing sample values with
 * real totals, and the direction people think of is fiction presented as fact;
 * the other direction is just as bad. A real host count under a "sample data"
 * banner teaches an operator to disbelieve the one number on the page that is
 * true, and after that the banner means nothing anywhere.
 *
 * Nothing here was promoted by relabelling. The container count and the log
 * entries are still fixtures and still say so, because Docker discovery is
 * Phase 6 and log collection is Phase 5.
 */
export function OverviewPage() {
  return (
    <>
      {/*
       * `titleHidden`, because the shell header already renders "Overview" as
       * this page's `<h1>` — the two sat eight pixels apart. The heading stays
       * in the outline; only the visible duplicate goes. See `PageHeader`.
       */}
      <PageHeader
        title="Overview"
        titleHidden
        description="Fleet-wide health. Host inventory and resource utilisation are measured; containers and logs are still sample data and are marked."
      />

      <FleetSection />

      {/* --- Everything below is sample data, and says so --- */}
      <section aria-labelledby="sample-heading">
        <h3 id="sample-heading" className="sr-only">
          Sample data for features not yet implemented
        </h3>
        <MockDataBanner className="mb-6" />

        <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-2">
          <MetricTile
            label="Containers"
            value="27"
            caption="25 running · 2 not running — sample"
            icon={<Boxes className="size-4" />}
          />
          <MetricTile
            label="Network devices"
            value="6"
            caption="sample — discovery arrives in Phase 7"
            icon={<HardDrive className="size-4" />}
          />
        </div>

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
                Sample entries. Log collection arrives in Phase 5.
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

      {/* --- Containers --- */}
      <Card className="mt-6">
        <CardHeader>
          <div>
            <CardTitle>Containers needing attention</CardTitle>
            <CardDescription>
              Sample container states. Docker discovery arrives in Phase 6.
            </CardDescription>
          </div>
          <Badge variant="warn">Sample</Badge>
        </CardHeader>
        <CardContent className="px-0 pb-0">
          <ul className="divide-border-subtle divide-y">
            {/*
             * Filtered to the states the heading claims.
             *
             * This section listed every fixture, so two healthy `running`
             * containers appeared under "needing attention" — a heading that
             * described its contents incorrectly. On a monitoring dashboard
             * that is the worst kind of wrong: it teaches the reader that the
             * section's title means nothing, and the one place that matters is
             * the place they look when something is broken.
             *
             * Filtering rather than renaming, because a section that surfaces
             * the containers an operator has to deal with is the more useful
             * card to be designing — and the fixtures contain exactly that
             * (one restarting, one exited) alongside the healthy ones. The
             * sample-data banner above still applies, and the per-card
             * "Sample" badge stays: nothing here claims these are real
             * container states.
             */}
            {ATTENTION_CONTAINERS.map((container) => (
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
      </section>
    </>
  );
}
