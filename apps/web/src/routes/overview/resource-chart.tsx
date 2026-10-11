import {
  Area,
  AreaChart,
  ResponsiveContainer,
  Tooltip as RechartsTooltip,
  XAxis,
  YAxis,
} from 'recharts';

import { MOCK_CPU_SERIES } from '@/mocks/overview';

/**
 * Resource utilisation chart for the Overview page.
 *
 * In its own module and loaded lazily: Recharts is roughly half the
 * application bundle, and it is needed by exactly one card. Keeping it off the
 * critical path means the shell and navigation render without waiting for a
 * charting library.
 *
 * The data is static sample data from `src/mocks/overview.ts`.
 */
export default function ResourceChart() {
  return (
    <>
      <div className="h-64 w-full">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart
            data={[...MOCK_CPU_SERIES]}
            margin={{ top: 4, right: 4, bottom: 0, left: -24 }}
          >
            <defs>
              <linearGradient id="fill-cpu" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="var(--color-accent)" stopOpacity={0.35} />
                <stop offset="100%" stopColor="var(--color-accent)" stopOpacity={0.02} />
              </linearGradient>
              <linearGradient id="fill-memory" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="var(--color-status-info)" stopOpacity={0.28} />
                <stop offset="100%" stopColor="var(--color-status-info)" stopOpacity={0.02} />
              </linearGradient>
            </defs>
            <XAxis
              dataKey="t"
              stroke="var(--color-text-faint)"
              tick={{ fontSize: 11 }}
              tickLine={false}
              axisLine={false}
              interval={3}
            />
            <YAxis
              stroke="var(--color-text-faint)"
              tick={{ fontSize: 11 }}
              tickLine={false}
              axisLine={false}
              domain={[0, 100]}
              unit="%"
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
            />
            <Area
              type="monotone"
              dataKey="cpu"
              name="CPU %"
              stroke="var(--color-accent)"
              strokeWidth={2}
              fill="url(#fill-cpu)"
            />
            <Area
              type="monotone"
              dataKey="memory"
              name="Memory %"
              stroke="var(--color-status-info)"
              strokeWidth={2}
              fill="url(#fill-memory)"
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>

      {/* Legend is plain markup rather than Recharts' <Legend>, so series
       * names are real text for screen readers and search. */}
      <div className="mt-3 flex items-center gap-4 text-xs">
        <span className="flex items-center gap-1.5">
          <span className="bg-accent size-2 rounded-full" aria-hidden="true" />
          <span className="text-text-muted">CPU</span>
        </span>
        <span className="flex items-center gap-1.5">
          <span className="bg-status-info size-2 rounded-full" aria-hidden="true" />
          <span className="text-text-muted">Memory</span>
        </span>
      </div>
    </>
  );
}
