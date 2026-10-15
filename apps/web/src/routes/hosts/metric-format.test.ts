import { describe, expect, it } from 'vitest';

import { makeSeries } from '@/test/fake-backend';

import {
  axisDomain,
  compareSeries,
  describeSeconds,
  formatBytes,
  formatDuration,
  formatLoad,
  formatMetric,
  formatMetricInline,
  formatPercent,
  seriesLabel,
} from './metric-format';

/**
 * Turning numbers into text, and the claims that depend on getting it right.
 *
 * Most of these look like unit-conversion trivia and are not. Each case is
 * here because the wrong answer would be a statement about a machine that is
 * false in a way nobody would catch by looking: binary versus decimal
 * multiples disagreeing with `free -h`, a load average rendered as a
 * percentage, a percentage axis that auto-scales a quiet host into a crisis.
 */

describe('bytes use binary multiples', () => {
  it.each([
    [0, '0', 'B'],
    [512, '512', 'B'],
    [1024, '1.00', 'KiB'],
    [1536, '1.50', 'KiB'],
    [1024 * 1024, '1.00', 'MiB'],
    [8 * 1024 ** 3, '8.00', 'GiB'],
    [1024 ** 4, '1.00', 'TiB'],
  ])('renders %i bytes as %s %s', (input, value, unit) => {
    expect(formatBytes(input)).toEqual({ value, unit });
  });

  it('agrees with what free -h would print, not with powers of ten', () => {
    // 8 GiB is 8589934592 bytes. Decimal multiples would call that "8.59 GB",
    // and a dashboard disagreeing with every other tool on the host reads as
    // a bug in one of them.
    expect(formatBytes(8_589_934_592)).toEqual({ value: '8.00', unit: 'GiB' });
  });

  it('shows no fractional bytes', () => {
    // There is no such thing as 1.25 bytes, so the smallest unit is integral.
    expect(formatBytes(999).value).toBe('999');
  });

  it('loses precision as the number grows, not before', () => {
    // Three significant figures is what fits a tile without wrapping. The
    // digit count steps down rather than being fixed, so 1.25 GiB and 125 GiB
    // are both readable.
    expect(formatBytes(1.25 * 1024 ** 3).value).toBe('1.25');
    expect(formatBytes(12.5 * 1024 ** 3).value).toBe('12.5');
    expect(formatBytes(125 * 1024 ** 3).value).toBe('125');
  });

  it('keeps the sign on a negative value rather than dropping it', () => {
    // A negative byte count is a bug upstream, and hiding the sign would turn
    // a visible one into a plausible number.
    expect(formatBytes(-2048)).toEqual({ value: '-2.00', unit: 'KiB' });
  });
});

describe('a percentage has one decimal place', () => {
  it('shows movement a whole number would hide', () => {
    // Zero decimals would make a CPU moving between 12.4% and 12.6% look
    // motionless.
    expect(formatPercent(12.4)).toEqual({ value: '12.4', unit: '%' });
    expect(formatPercent(12.6)).toEqual({ value: '12.6', unit: '%' });
  });

  it('does not claim precision the measurement does not have', () => {
    // The value comes from /proc/stat deltas over an interval; the third digit
    // is noise.
    expect(formatPercent(12.4567).value).toBe('12.5');
  });

  it('renders a real zero as zero', () => {
    // The point of §21 is that *unknown* must not render as zero. A measured
    // zero is a measurement and must render normally — the distinction is made
    // by the caller having a number at all.
    expect(formatPercent(0)).toEqual({ value: '0.0', unit: '%' });
  });
});

describe('a duration reads in the units an operator thinks in', () => {
  it.each([
    [0, '0'],
    [45, '45'],
    [90, '1m 30s'],
    [3600, '1h 0m'],
    [3661, '1h 1m'],
    [86_400, '1d 0h'],
    [1_231_200, '14d 6h'],
  ])('renders %i seconds as %s', (input, expected) => {
    expect(formatDuration(input).value).toBe(expected);
  });

  it('stops at two components', () => {
    // "14d 6h 32m 11s" is precision nobody acts on, and it changes every
    // second — so a tile showing it would never be still.
    expect(formatDuration(1_234_331).value).toBe('14d 6h');
  });

  it('never renders a negative duration', () => {
    // A clock moving backwards is possible; "-3s of uptime" is not a thing to
    // show somebody.
    expect(formatDuration(-10).value).toBe('0');
  });
});

describe('a load average is not a percentage', () => {
  it('prints what /proc/loadavg prints', () => {
    expect(formatLoad(2)).toEqual({ value: '2.00', unit: undefined });
    expect(formatLoad(0.07)).toEqual({ value: '0.07', unit: undefined });
  });

  it('carries no unit, because there is none', () => {
    // 2.0 on an eight-core machine is idle and on a single-core machine is
    // saturated, so a "200%" rendering would be actively misleading.
    expect(formatLoad(2).unit).toBeUndefined();
    expect(formatMetricInline(2, 'load')).toBe('2.00');
  });
});

describe('formatMetric dispatches on the unit the series declares', () => {
  it.each([
    ['percent', 42.5, '42.5%'],
    ['bytes', 2048, '2.00 KiB'],
    ['seconds', 7200, '2h 0m'],
    ['load', 1.5, '1.50'],
    ['count', 1_048_576, '1,048,576'],
  ])('renders a %s value', (unit, value, expected) => {
    expect(formatMetricInline(value, unit)).toBe(expected);
  });

  it('shows a unit this release does not know rather than hiding the value', () => {
    // The data exists, and an operator can read "4 jiffies" even if the client
    // cannot style it. Dropping it would lose a real measurement.
    expect(formatMetric(4, 'jiffies')).toEqual({ value: '4', unit: 'jiffies' });
    expect(formatMetricInline(4, 'jiffies')).toBe('4 jiffies');
  });
});

describe('the y-axis domain', () => {
  it('pins a percentage to 0–100', () => {
    // Auto-scaling would redraw a machine idling between 0.4% and 1.1% as a
    // sawtooth filling the panel, which reads as a problem. It is the single
    // most misleading thing a CPU chart can do.
    expect(axisDomain('percent')).toEqual([0, 100]);
  });

  it('lets every other unit scale, because there is no meaningful ceiling', () => {
    // Memory depends on the host and load on the core count; a fixed domain
    // would flatten a real signal.
    expect(axisDomain('bytes')).toBeUndefined();
    expect(axisDomain('load')).toBeUndefined();
    expect(axisDomain('seconds')).toBeUndefined();
  });
});

describe('series labels', () => {
  it('uses the metric name alone when there are no labels', () => {
    expect(seriesLabel(makeSeries({ metric_name: 'Utilisation' }))).toBe('Utilisation');
  });

  it('renders labels, so two series of one metric are told apart', () => {
    // A legend with eight identically-named lines is a chart nobody can read.
    expect(
      seriesLabel(makeSeries({ metric_name: 'Utilisation', labels: { core: '3' } })),
    ).toBe('Utilisation (core 3)');
  });

  it('orders multiple labels stably', () => {
    // Not cosmetic: an unstable order would make the same series appear under
    // two different names across polls.
    const label = seriesLabel(
      makeSeries({ metric_name: 'Rate', labels: { device: 'eth0', direction: 'rx' } }),
    );
    expect(label).toBe('Rate (device eth0, direction rx)');
  });
});

describe('series ordering', () => {
  it('follows the catalogue order, not the alphabet', () => {
    // The eye learns a fixed position, and values that reshuffle between
    // polls are unreadable.
    const ordered = [
      makeSeries({ series_id: 1, module: 'uptime' }),
      makeSeries({ series_id: 2, module: 'cpu' }),
      makeSeries({ series_id: 3, module: 'load' }),
      makeSeries({ series_id: 4, module: 'memory' }),
    ]
      .sort(compareSeries)
      .map((entry) => entry.module);

    expect(ordered).toEqual(['cpu', 'memory', 'load', 'uptime']);
  });

  it('sorts an unrecognised module last rather than first', () => {
    // A series from a module this release does not know must not displace the
    // ones an operator came to see.
    const ordered = [
      makeSeries({ series_id: 1, module: 'future-thing' }),
      makeSeries({ series_id: 2, module: 'cpu' }),
    ]
      .sort(compareSeries)
      .map((entry) => entry.module);

    expect(ordered).toEqual(['cpu', 'future-thing']);
  });
});

describe('describeSeconds renders a configured interval', () => {
  it.each([
    [1, '1 s'],
    [30, '30 s'],
    [60, '1 min'],
    [300, '5 min'],
    [90, '1.5 min'],
    [3600, '1 h'],
    [5400, '1.5 h'],
  ])('renders %i seconds as %s', (input, expected) => {
    expect(describeSeconds(input)).toBe(expected);
  });

  it('differs from a measured duration, deliberately', () => {
    // An interval of 300 is a setting somebody chose and reads as "5 min"; a
    // measured 300 seconds is a clock and reads as "5m 0s".
    expect(describeSeconds(300)).toBe('5 min');
    expect(formatDuration(300).value).toBe('5m 0s');
  });
});

describe('rates', () => {
  it('renders a byte rate in binary multiples with a per-second suffix', () => {
    // Binary, matching formatBytes and every other tool on the host. A
    // dashboard that disagreed with `iftop` by 4.8% would be read as broken.
    expect(formatMetricInline(1024 * 1024, 'bytes_per_second')).toBe('1.00 MiB/s');
    expect(formatMetricInline(0, 'bytes_per_second')).toBe('0 B/s');
  });

  it('renders disk operations in decimal multiples', () => {
    // Decimal, not binary: these are counts of events, and nobody has ever
    // said "1 kibi-IOPS".
    expect(formatMetricInline(1500, 'operations_per_second')).toBe('1.5 kIOPS');
    expect(formatMetricInline(42, 'operations_per_second')).toBe('42 IOPS');
  });

  it('renders packet rates in decimal multiples', () => {
    expect(formatMetricInline(2_500_000, 'packets_per_second')).toBe('2.5 Mpkt/s');
  });

  it('renders a link speed in bits, which is the unit the port is sold in', () => {
    // A "1 Gb" port is 125 MB/s. Rendering it as 125 would make an operator
    // doubt the whole panel.
    expect(formatMetricInline(1_000_000_000, 'bits_per_second')).toBe('1 Gbit/s');
    expect(formatMetricInline(2_500_000_000, 'bits_per_second')).toBe('2.5 Gbit/s');
  });

  it('still renders an unknown unit as a bare number with a suffix', () => {
    // The guard that keeps a new backend unit visible rather than hidden.
    expect(formatMetricInline(4, 'jiffies')).toBe('4 jiffies');
  });
});

describe('series ordering across the storage and network modules', () => {
  it('keeps the catalogue order rather than sorting alphabetically', () => {
    const ordered = [
      makeSeries({ module: 'network', metric: 'receive_bytes' }),
      makeSeries({ module: 'cpu', metric: 'utilisation' }),
      makeSeries({ module: 'diskio', metric: 'read_bytes' }),
      makeSeries({ module: 'filesystem', metric: 'used' }),
    ].sort(compareSeries);

    expect(ordered.map((entry) => entry.module)).toEqual([
      'cpu',
      'filesystem',
      'diskio',
      'network',
    ]);
  });
});
