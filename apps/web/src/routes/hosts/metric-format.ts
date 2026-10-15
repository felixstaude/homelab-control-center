import type { LatestValue, MetricSeries } from '@/api/types';

/**
 * Turning a number and a unit into text an operator can read.
 *
 * In one module because the same value appears in a tile, a chart axis and a
 * tooltip, and three formatters would disagree about whether 1 GiB is 1024 MiB
 * or 1000 MB — which is the kind of discrepancy that makes somebody distrust
 * the whole page.
 *
 * ## There is no formatter for "no value"
 *
 * Deliberate. Every function here takes a `number`, so a caller holding
 * `null` cannot route it through and receive `"0%"` or `"—"` by accident: it
 * has to decide, at the call site, what an absence should look like. That is
 * the product rule (§21: do not display zero when the actual value is
 * unknown) expressed as a type rather than as a convention.
 */

/** Units the backend declares. Anything else is rendered as a bare number. */
export type MetricUnit =
  | 'percent'
  | 'bytes'
  | 'seconds'
  | 'count'
  | 'load'
  | 'bytes_per_second'
  | 'operations_per_second'
  | 'packets_per_second'
  | 'bits_per_second'
  | 'celsius'
  | 'rpm';

/** A formatted value, split so a tile can style the unit separately. */
export type FormattedMetric = {
  /** The number, formatted. Never includes the unit. */
  value: string;
  /** The unit as a suffix, or undefined where the value is self-describing. */
  unit?: string;
};

/**
 * Binary multiples, because this is memory and disk.
 *
 * The backend reports `MemAvailable` and friends in bytes derived from
 * kibibytes, so showing them in powers of ten would disagree with every other
 * tool on the host — `free -h`, `df -h` and the kernel all use binary
 * multiples, and a dashboard that said 8.6 GB where `free` said 8 GiB would be
 * read as a bug in one of them.
 */
const BINARY_UNITS = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'] as const;

/** Significant digits for a byte count, by magnitude. */
function byteDigits(scaled: number): number {
  if (scaled >= 100) return 0;
  if (scaled >= 10) return 1;
  return 2;
}

export function formatBytes(bytes: number): FormattedMetric {
  const sign = bytes < 0 ? '-' : '';
  let remaining = Math.abs(bytes);
  let index = 0;
  while (remaining >= 1024 && index < BINARY_UNITS.length - 1) {
    remaining /= 1024;
    index += 1;
  }
  // Bytes themselves are integers; there is no such thing as 1.25 bytes.
  const digits = index === 0 ? 0 : byteDigits(remaining);
  // `?? 'B'` is unreachable — `index` is clamped by the loop — but the index
  // signature on a tuple is not something the compiler can narrow, and an
  // assertion here would be the one place in this module that could lie.
  return {
    value: `${sign}${remaining.toFixed(digits)}`,
    unit: BINARY_UNITS[index] ?? 'B',
  };
}

/**
 * A percentage.
 *
 * One decimal place. Zero would make a CPU moving between 12.4% and 12.6%
 * look motionless, and two is precision the measurement does not have — the
 * value is derived from `/proc/stat` deltas over a sampling interval, and the
 * third digit is noise.
 */
export function formatPercent(value: number): FormattedMetric {
  return { value: value.toFixed(1), unit: '%' };
}

/**
 * A duration in seconds, rendered as the largest two units that fit.
 *
 * "14d 6h" rather than "1231200s" or "14.26 days": uptime is read at a glance,
 * and the units an operator thinks in are days and hours. Two components
 * rather than all four, because "14d 6h 32m 11s" is a precision nobody acts
 * on and it changes every second.
 */
export function formatDuration(seconds: number): FormattedMetric {
  const total = Math.max(0, Math.floor(seconds));
  const days = Math.floor(total / 86_400);
  const hours = Math.floor((total % 86_400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;

  if (days > 0) return { value: `${String(days)}d ${String(hours)}h` };
  if (hours > 0) return { value: `${String(hours)}h ${String(minutes)}m` };
  if (minutes > 0) return { value: `${String(minutes)}m ${String(secs)}s` };
  return { value: String(secs), unit: 's' };
}

/**
 * A load average.
 *
 * Two decimals and no unit, exactly as `/proc/loadavg` and `uptime` print it.
 * A load average is not a percentage and must not be shown as one: 2.0 on an
 * eight-core machine is idle and on a single-core machine is saturated, so a
 * "200%" rendering would be actively misleading.
 */
export function formatLoad(value: number): FormattedMetric {
  return { value: value.toFixed(2) };
}

/** A plain count. Grouped, because 1048576 is unreadable and 1,048,576 is not. */
export function formatCount(value: number): FormattedMetric {
  return { value: value.toLocaleString() };
}

/**
 * A byte rate.
 *
 * Binary multiples, like {@link formatBytes}, and for the same reason: every
 * other tool on the host reports transfer in MiB/s and a dashboard that
 * disagreed by 4.8% would be read as broken.
 *
 * A separate function rather than `formatBytes(value).value + '/s'` because
 * the rounding is different. A byte *count* of 1023 is "1023 B"; a byte
 * *rate* of 1023 is also "1023 B/s", but a rate of 0 is extremely common —
 * most interfaces are idle most of the time — and rendering it as "0 B/s"
 * rather than "0.00 B/s" is what keeps a column of idle interfaces readable.
 */
export function formatRate(bytesPerSecond: number): FormattedMetric {
  const formatted = formatBytes(bytesPerSecond);
  return { value: formatted.value, unit: `${formatted.unit ?? 'B'}/s` };
}

/** Decimal multiples for a quantity that is counted rather than addressed. */
const DECIMAL_UNITS = ['', 'k', 'M', 'G', 'T'] as const;

function scaleDecimal(value: number): { scaled: number; prefix: string } {
  const sign = value < 0 ? -1 : 1;
  let remaining = Math.abs(value);
  let index = 0;
  while (remaining >= 1000 && index < DECIMAL_UNITS.length - 1) {
    remaining /= 1000;
    index += 1;
  }
  return { scaled: sign * remaining, prefix: DECIMAL_UNITS[index] ?? '' };
}

/**
 * A rate of discrete things — disk operations, network packets.
 *
 * Decimal multiples, not binary: these are counts of events, and nobody has
 * ever said "1 kibi-IOPS". The same reasoning that makes bytes binary makes
 * these decimal — match what the rest of the world prints.
 */
export function formatEventRate(perSecond: number, noun: string): FormattedMetric {
  const { scaled, prefix } = scaleDecimal(perSecond);
  const digits = prefix === '' ? (Number.isInteger(scaled) ? 0 : 1) : 1;
  return { value: scaled.toFixed(digits), unit: `${prefix}${noun}` };
}

/**
 * A link speed, in the unit the hardware is sold in.
 *
 * Decimal, and bits rather than bytes: a "1 Gb" port is 1,000,000,000 bit/s
 * and 125 MB/s, and rendering it as 125 would make an operator doubt the
 * whole panel.
 */
/**
 * A temperature.
 *
 * One decimal, and the degree sign attached with no space — "32.9°C", which
 * is how every thermal tool prints it. The sensors report millidegrees, so
 * the first decimal is real precision rather than noise; a second would be,
 * since no consumer thermistor resolves a hundredth of a degree.
 */
export function formatCelsius(value: number): FormattedMetric {
  return { value: value.toFixed(1), unit: '°C' };
}

/**
 * A fan speed.
 *
 * No decimals: a tachometer counts pulses over an interval, so the units
 * digit is already an approximation and a tenth of an RPM is invented
 * precision.
 */
export function formatRpm(value: number): FormattedMetric {
  return { value: Math.round(value).toLocaleString(), unit: 'RPM' };
}

export function formatBitRate(bitsPerSecond: number): FormattedMetric {
  const { scaled, prefix } = scaleDecimal(bitsPerSecond);
  return {
    value: Number.isInteger(scaled) ? String(scaled) : scaled.toFixed(1),
    unit: `${prefix}bit/s`,
  };
}

/**
 * A configured interval, in the unit an operator thinks in.
 *
 * Distinct from {@link formatDuration}, which renders a *measured* duration
 * like uptime. An interval of 300 reads as "5 min", not "5m 0s": one is a
 * setting somebody chose and the other is a clock.
 */
export function describeSeconds(seconds: number): string {
  if (seconds < 60) return `${String(seconds)} s`;
  if (seconds < 3600) {
    const minutes = seconds / 60;
    return `${Number.isInteger(minutes) ? String(minutes) : minutes.toFixed(1)} min`;
  }
  const hours = seconds / 3600;
  return `${Number.isInteger(hours) ? String(hours) : hours.toFixed(1)} h`;
}

/** Format a value according to the unit its series declares. */
export function formatMetric(value: number, unit: string): FormattedMetric {
  switch (unit) {
    case 'percent':
      return formatPercent(value);
    case 'bytes':
      return formatBytes(value);
    case 'seconds':
      return formatDuration(value);
    case 'load':
      return formatLoad(value);
    case 'count':
      return formatCount(value);
    case 'bytes_per_second':
      return formatRate(value);
    case 'operations_per_second':
      return formatEventRate(value, 'IOPS');
    case 'packets_per_second':
      return formatEventRate(value, 'pkt/s');
    case 'bits_per_second':
      return formatBitRate(value);
    case 'celsius':
      return formatCelsius(value);
    case 'rpm':
      return formatRpm(value);
    default:
      // A unit this release does not know about. The number is shown with the
      // unit as a literal suffix rather than hidden: the data exists, and an
      // operator can read "4 jiffies" even if the client cannot style it.
      return { value: String(value), unit };
  }
}

/** The formatted value and unit joined, for a tooltip or an aria-label. */
export function formatMetricInline(value: number, unit: string): string {
  const formatted = formatMetric(value, unit);
  if (formatted.unit === undefined) return formatted.value;
  // No space before a percent sign or a degree sign; one before everything
  // else. "32.9 °C" and "9.6 %" read as typographical mistakes.
  const tight = formatted.unit === '%' || formatted.unit.startsWith('°');
  return `${formatted.value}${tight ? '' : ' '}${formatted.unit}`;
}

/**
 * A series' display name, including its labels.
 *
 * "CPU utilisation" for an unlabelled series, "CPU utilisation (core 3)" for a
 * labelled one. The labels are rendered rather than dropped because two series
 * of the same metric are otherwise indistinguishable in a legend, and a chart
 * with eight identically-named lines is a chart nobody can read.
 */
export function seriesLabel(series: MetricSeries): string {
  const labels = Object.entries(series.labels);
  if (labels.length === 0) return series.metric_name;
  const rendered = labels
    .map(([key, value]) => `${key} ${value}`)
    .sort((left, right) => left.localeCompare(right))
    .join(', ');
  return `${series.metric_name} (${rendered})`;
}

/**
 * The y-axis domain for a unit.
 *
 * `percent` is pinned to 0–100 so that a quiet host's chart is visibly quiet.
 * Auto-scaling it would redraw a machine idling between 0.4% and 1.1% as a
 * dramatic sawtooth filling the panel, which reads as a problem — the single
 * most misleading thing a CPU chart can do.
 *
 * Everything else auto-scales, because there is no meaningful ceiling: memory
 * depends on the host, load depends on the core count, and a fixed domain
 * would flatten a real signal.
 */
export function axisDomain(unit: string): [number, number] | undefined {
  return unit === 'percent' ? [0, 100] : undefined;
}

/**
 * Order series for display: by module as the catalogue lists them, then name.
 *
 * A stable order matters more than which order: values that reshuffle between
 * polls are unreadable, and the eye learns a fixed position.
 */
const MODULE_ORDER = [
  'cpu',
  'memory',
  'swap',
  'load',
  'uptime',
  'filesystem',
  'diskio',
  'network',
  'sensors',
  'docker',
  'systemd',
];

export function compareSeries(left: MetricSeries, right: MetricSeries): number {
  const leftModule = MODULE_ORDER.indexOf(left.module);
  const rightModule = MODULE_ORDER.indexOf(right.module);
  // A module the client does not know sorts last rather than first, so an
  // unrecognised series cannot displace the ones an operator came to see.
  const leftRank = leftModule === -1 ? MODULE_ORDER.length : leftModule;
  const rightRank = rightModule === -1 ? MODULE_ORDER.length : rightModule;
  if (leftRank !== rightRank) return leftRank - rightRank;
  return seriesLabel(left).localeCompare(seriesLabel(right));
}

export function compareLatest(left: LatestValue, right: LatestValue): number {
  return compareSeries(left.series, right.series);
}

/**
 * The modules rendered as tables rather than as tiles.
 *
 * A tile is right for a host-wide scalar and wrong for a *set*: a laptop
 * with three filesystems, four block devices and two interfaces produces
 * fifty-five labelled series, and fifty-five tiles is a wall with the one
 * number somebody wanted somewhere in it. The question these modules answer
 * is comparative — *which* filesystem is filling up — and that is a table.
 *
 * Here rather than in `resource-panels.tsx` so that file exports only
 * components, which is what React fast refresh needs.
 */
export const RESOURCE_MODULES: ReadonlySet<string> = new Set([
  'filesystem',
  'diskio',
  'network',
  'sensors',
  'docker',
  'systemd',
]);
