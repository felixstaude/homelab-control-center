import { describe, expect, it } from 'vitest';

import type { MetricRangePoint } from '@/api/types';

import { medianGap, toGrid } from './metric-grid';

/**
 * The function that decides whether a chart tells the truth about a gap.
 *
 * §22: a gap must not be interpolated into something that falsely implies
 * measurements exist. The API honours that by omitting empty buckets, which
 * makes the gap implicit in the spacing — and a line chart joins consecutive
 * points regardless of how far apart they are. `toGrid` inserts an explicit
 * `null` into each hole so the line breaks.
 *
 * The failure this prevents is specific: a host switched off for four hours
 * drawn as a straight line across them looks like a host that was idling.
 */

const MINUTE = 60_000;

function point(
  minutesFromBase: number,
  value: number,
  bounds?: { minimum: number; maximum: number },
): MetricRangePoint {
  return {
    at: new Date(Date.UTC(2026, 9, 1, 12, 0, 0) + minutesFromBase * MINUTE).toISOString(),
    value,
    minimum: bounds?.minimum ?? null,
    maximum: bounds?.maximum ?? null,
    sample_count: bounds === undefined ? null : 12,
  };
}

describe('a continuous series is left alone', () => {
  it('returns one row per point with no nulls', () => {
    const grid = toGrid([point(0, 10), point(1, 11), point(2, 12), point(3, 13)]);

    expect(grid).toHaveLength(4);
    expect(grid.every((row) => row.value !== null)).toBe(true);
  });

  it('converts timestamps to epoch milliseconds for a real time axis', () => {
    // A string axis would space points evenly regardless of their actual
    // times, which would hide a gap just as effectively as interpolating it.
    const grid = toGrid([point(0, 10), point(1, 11)]);

    expect(grid[0]?.at).toBe(Date.UTC(2026, 9, 1, 12, 0, 0));
    expect(grid[1]?.at).toBe(Date.UTC(2026, 9, 1, 12, 1, 0));
  });
});

describe('a hole becomes an explicit break', () => {
  it('inserts a null between points that are too far apart', () => {
    // Four hours of nothing between two clusters. Without the null, the chart
    // draws a confident line across it.
    const grid = toGrid([
      point(0, 10),
      point(1, 11),
      point(2, 12),
      point(242, 40),
      point(243, 41),
    ]);

    const nulls = grid.filter((row) => row.value === null);
    expect(nulls).toHaveLength(1);
    // Positioned inside the hole, so the break lands where the data stops.
    expect(nulls[0]?.at).toBeGreaterThan(grid[2]?.at ?? 0);
    expect(nulls[0]?.at).toBeLessThan(Date.UTC(2026, 9, 1, 12, 0, 0) + 242 * MINUTE);
  });

  it('inserts one null per hole, not one per missing interval', () => {
    // One is enough to break the line. Filling a four-hour hole at the step
    // interval would add thousands of rows the chart draws nothing for.
    const grid = toGrid([point(0, 10), point(1, 11), point(600, 12), point(601, 13)]);

    expect(grid.filter((row) => row.value === null)).toHaveLength(1);
    expect(grid).toHaveLength(5);
  });

  it('breaks the line in both of several holes', () => {
    const grid = toGrid([
      point(0, 1),
      point(1, 2),
      point(100, 3),
      point(101, 4),
      point(300, 5),
      point(301, 6),
    ]);

    expect(grid.filter((row) => row.value === null)).toHaveLength(2);
  });

  it('tolerates jitter without fragmenting a healthy line', () => {
    // A scheduler firing at 10.2 s instead of 10 s must not produce a dashed
    // chart. The tolerance is 1.5 steps for exactly this.
    const times = [0, 1, 2.1, 3, 4.2, 5, 6.1];
    const grid = toGrid(times.map((minutes, index) => point(minutes, index)));

    expect(grid.filter((row) => row.value === null)).toHaveLength(0);
  });

  it('shows a single missed interval as a gap', () => {
    // The short outages are the ones worth seeing, so the tolerance must not
    // be so generous that one missed sample disappears.
    const grid = toGrid([point(0, 1), point(1, 2), point(4, 3), point(5, 4)]);

    expect(grid.filter((row) => row.value === null)).toHaveLength(1);
  });
});

describe('the step is inferred from the data, not assumed', () => {
  it('uses the median so one long outage cannot swallow later gaps', () => {
    // The mean of these gaps is inflated by the 200-minute outage. With a mean
    // step, the later 5-minute gap would be inside tolerance and invisible.
    const grid = toGrid([
      point(0, 1),
      point(1, 2),
      point(2, 3),
      point(202, 4),
      point(203, 5),
      point(208, 6),
    ]);

    expect(grid.filter((row) => row.value === null)).toHaveLength(2);
  });

  it('computes the median of an even number of gaps without crashing', () => {
    expect(medianGap([0, 60_000, 120_000])).toBe(60_000);
    expect(medianGap([0, 60_000, 120_000, 240_000])).toBe(60_000);
  });

  it('takes the lower middle value, so one hole cannot define the step', () => {
    // Two gaps — a three-point series with a hole in it. The upper middle *is*
    // the hole, so using it would make the step the outage's own length and
    // the break would never be drawn. Regression for exactly that.
    expect(medianGap([0, 60_000, 30 * 60_000])).toBe(60_000);
  });

  it('reports no step for fewer than two timestamps', () => {
    expect(medianGap([])).toBe(0);
    expect(medianGap([1000])).toBe(0);
  });
});

describe('degenerate inputs', () => {
  it('returns nothing for no points', () => {
    // An empty series is empty. The chart renders its own "nothing was
    // collected" state rather than an axis with no line.
    expect(toGrid([])).toEqual([]);
  });

  it('returns a single point unchanged, with no spacing to infer', () => {
    const grid = toGrid([point(0, 7)]);

    expect(grid).toHaveLength(1);
    expect(grid[0]?.value).toBe(7);
  });

  it('does not insert a break between two points, however far apart', () => {
    // With two points there is one gap, so it *is* the median — there is no
    // baseline to call it unusual against. Guessing would mean a two-point
    // series always drew a break.
    const grid = toGrid([point(0, 1), point(5000, 2)]);

    expect(grid).toHaveLength(2);
  });
});

describe('the min–max band', () => {
  it('expresses the range as a baseline plus a height', () => {
    // Recharts stacks raw values, so a band from minimum to maximum has to be
    // a baseline and a span rather than a pair.
    const grid = toGrid([point(0, 12, { minimum: 4, maximum: 100 })]);

    expect(grid[0]?.minimum).toBe(4);
    expect(grid[0]?.span).toBe(96);
  });

  it('leaves the band absent for the raw tier', () => {
    // Raw points have no minimum or maximum — they *are* the measurement — so
    // a band of zero height would be a drawn claim about nothing.
    const grid = toGrid([point(0, 12)]);

    expect(grid[0]?.minimum).toBeNull();
    expect(grid[0]?.span).toBeNull();
  });

  it('keeps the band null inside a gap', () => {
    // Otherwise the band would be drawn across a period with no data even
    // though the line correctly breaks.
    const grid = toGrid([
      point(0, 10, { minimum: 5, maximum: 20 }),
      point(1, 11, { minimum: 6, maximum: 21 }),
      point(500, 12, { minimum: 7, maximum: 22 }),
    ]);

    const hole = grid.find((row) => row.value === null);
    expect(hole?.minimum).toBeNull();
    expect(hole?.span).toBeNull();
  });
});
