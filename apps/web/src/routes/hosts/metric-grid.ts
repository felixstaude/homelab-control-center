import type { MetricRangePoint } from '@/api/types';

/**
 * Turning a sparse series into something a chart can draw honestly.
 *
 * In its own module rather than inside the chart component for two reasons: it
 * is the one piece of real logic in the charting path and deserves tests
 * without mounting Recharts, and a component file that also exports a helper
 * breaks React fast refresh.
 *
 * ## The problem this solves
 *
 * §22: a gap must not be interpolated into something that falsely implies
 * measurements exist. The API honours that by returning **no point** for a
 * period when nothing was collected — but that makes the gap *implicit in the
 * spacing*, and a line chart cannot see spacing. Given 10:00, 10:01 and 14:00
 * it draws a confident straight line across four missing hours, which reads as
 * "the host was idling" when the truth is "the host was not reporting".
 *
 * So the absence is made explicit: a `null` value is inserted into each hole,
 * and a null breaks the line. The chart then draws exactly what happened.
 */

/** One row of chart data. `null` means "not measured", and breaks the line. */
export type GridPoint = {
  /** Epoch milliseconds, so the axis can be a real time scale. */
  at: number;
  value: number | null;
  minimum: number | null;
  /**
   * Maximum minus minimum, for the stacked min–max band.
   *
   * Precomputed rather than derived in the chart because Recharts stacks raw
   * values: a band from minimum to maximum has to be expressed as a baseline
   * plus a height, not as a pair.
   */
  span: number | null;
};

/**
 * How much wider than the typical spacing counts as a gap.
 *
 * 1.5 is tolerant enough that ordinary jitter in a collection interval — a
 * scheduler firing at 10.2 s instead of 10 s, a batch delivered late — does
 * not fragment a healthy line into dashes, and tight enough that a single
 * missed interval is visible. A value near 1.0 would make every chart dashed;
 * a large one would hide short outages, which are the ones worth seeing.
 */
const GAP_TOLERANCE = 1.5;

/**
 * Expand measured points onto the timeline, with `null` in the holes.
 *
 * The step is inferred from the **median** interval between the points
 * actually received, not from the resolution that was requested: the server
 * chooses the tier, and the client should draw what it was sent. The median
 * rather than the mean, so one long outage cannot inflate the step and swallow
 * every real gap after it.
 */
export function toGrid(points: readonly MetricRangePoint[]): GridPoint[] {
  const rows: GridPoint[] = points.map((point) => ({
    at: new Date(point.at).getTime(),
    value: point.value,
    minimum: point.minimum,
    span:
      point.minimum === null || point.maximum === null
        ? null
        : point.maximum - point.minimum,
  }));

  // Nothing to interpolate across, and no spacing to infer a step from.
  if (rows.length < 2) return rows;

  const step = medianGap(rows.map((row) => row.at));
  if (step <= 0) return rows;
  const tolerance = step * GAP_TOLERANCE;

  const withGaps: GridPoint[] = [];
  for (const [index, row] of rows.entries()) {
    const previous = rows[index - 1];
    if (previous !== undefined && row.at - previous.at > tolerance) {
      // One null row in the middle of the hole. One is enough — it breaks the
      // line — and filling the gap with nulls at the step interval would add
      // thousands of rows the chart draws nothing for.
      withGaps.push({
        at: previous.at + (row.at - previous.at) / 2,
        value: null,
        minimum: null,
        span: null,
      });
    }
    withGaps.push(row);
  }
  return withGaps;
}

/**
 * The typical interval between consecutive timestamps.
 *
 * The **lower** middle value, not the upper one and not the mean of the two.
 * That matters at small counts and is not a rounding preference: with exactly
 * two gaps — a three-point series containing one hole — the upper middle *is*
 * the hole, so the step would be the outage's own length, the tolerance would
 * be half again as long, and the break would never be drawn. Taking the lower
 * middle makes the step the ordinary spacing, which is what it is meant to be.
 *
 * The bias is deliberate in the direction it errs. Choosing the smaller
 * typical spacing makes the detector more willing to draw a break, and of the
 * two mistakes available — a break where the data was merely late, or a
 * continuous line across a period with no data — only the second is a false
 * claim about a machine.
 */
export function medianGap(times: readonly number[]): number {
  const gaps: number[] = [];
  for (let index = 1; index < times.length; index += 1) {
    const current = times[index];
    const previous = times[index - 1];
    if (current !== undefined && previous !== undefined) gaps.push(current - previous);
  }
  if (gaps.length === 0) return 0;
  const sorted = [...gaps].sort((left, right) => left - right);
  return sorted[Math.floor((sorted.length - 1) / 2)] ?? 0;
}
