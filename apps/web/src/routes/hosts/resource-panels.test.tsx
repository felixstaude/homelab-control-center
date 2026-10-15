import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { LatestValue } from '@/api/types';
import { makeLatest, makeSeries } from '@/test/fake-backend';

import {
  ContainerPanel,
  DiskPanel,
  FilesystemPanel,
  NetworkPanel,
  SensorPanel,
} from './resource-panels';

/**
 * The three panels that render a *set* rather than a scalar.
 *
 * Most of these assert an absence. Every lookup in this file can come back
 * undefined — `inodes_utilisation` genuinely does not exist on btrfs,
 * `speed` does not exist on a wireless link, `up` does not exist where the
 * kernel says `operstate=unknown` — and the product rule is that an absent
 * measurement renders as an em dash and never as a zero. A test suite that
 * only fed complete data would pass with every one of those rendering "0".
 */

function value(
  module: string,
  metric: string,
  labels: Record<string, string>,
  unit: string,
  amount: number,
): LatestValue {
  return makeLatest({
    series: makeSeries({ module, metric, labels, unit, module_name: module, metric_name: metric }),
    value: amount,
  });
}

const GIB = 1024 ** 3;

describe('FilesystemPanel', () => {
  const rootAndBoot = [
    value('filesystem', 'total', { mount: '/' }, 'bytes', 100 * GIB),
    value('filesystem', 'used', { mount: '/' }, 'bytes', 40 * GIB),
    value('filesystem', 'available', { mount: '/' }, 'bytes', 55 * GIB),
    value('filesystem', 'utilisation', { mount: '/' }, 'percent', 42.1),
    value('filesystem', 'readonly', { mount: '/' }, 'count', 0),
    value('filesystem', 'total', { mount: '/boot' }, 'bytes', 2 * GIB),
    value('filesystem', 'utilisation', { mount: '/boot' }, 'percent', 45.3),
    value('filesystem', 'inodes_utilisation', { mount: '/boot' }, 'percent', 0.8),
    value('filesystem', 'readonly', { mount: '/boot' }, 'count', 0),
  ];

  it('renders one row per mount point', () => {
    render(<FilesystemPanel values={rootAndBoot} />);

    expect(screen.getByText('/')).toBeInTheDocument();
    expect(screen.getByText('/boot')).toBeInTheDocument();
  });

  it('shows an em dash where a filesystem has no inode table', () => {
    // btrfs and XFS report no inode count at all. "0%" would be a
    // measurement this system did not take.
    render(<FilesystemPanel values={rootAndBoot} />);

    const root = screen.getByText('/').closest('tr');
    expect(root).not.toBeNull();
    expect(within(root as HTMLElement).getByText('—')).toBeInTheDocument();
  });

  it('badges a read-only mount, because that is how a failing disk shows up', () => {
    // The kernel remounts a filesystem read-only after an I/O error, so this
    // is often the first visible sign of a disk going.
    render(
      <FilesystemPanel
        values={[
          ...rootAndBoot,
          value('filesystem', 'utilisation', { mount: '/srv' }, 'percent', 10),
          value('filesystem', 'readonly', { mount: '/srv' }, 'count', 1),
        ]}
      />,
    );

    const srv = screen.getByText('/srv').closest('tr');
    expect(within(srv as HTMLElement).getByText('read-only')).toBeInTheDocument();
    const root = screen.getByText('/').closest('tr');
    expect(within(root as HTMLElement).queryByText('read-only')).toBeNull();
  });

  it('renders nothing at all when no filesystem series exist', () => {
    // Not an empty table with headings: a host that has not enabled the
    // module should see no filesystem section, not an invitation to wonder
    // where its disks went.
    const { container } = render(<FilesystemPanel values={[]} />);

    expect(container).toBeEmptyDOMElement();
  });

  it('describes capacity as the filesystem s, not the disk s', () => {
    // "My 4 TB disk shows as 3.6 TB" is the first question this panel gets.
    render(<FilesystemPanel values={rootAndBoot} />);

    expect(screen.getByText(/smaller than the block device/i)).toBeInTheDocument();
  });

  it('gives the usage bar an accessible label rather than only a width', () => {
    render(<FilesystemPanel values={rootAndBoot} />);

    expect(screen.getByLabelText('/ is 42.1% full')).toBeInTheDocument();
  });
});

describe('DiskPanel', () => {
  const stacked = [
    value('diskio', 'read_bytes', { device: 'nvme0n1' }, 'bytes_per_second', 0),
    value('diskio', 'write_bytes', { device: 'nvme0n1' }, 'bytes_per_second', 1024 * 1024),
    value('diskio', 'read_ops', { device: 'nvme0n1' }, 'operations_per_second', 0),
    value('diskio', 'write_ops', { device: 'nvme0n1' }, 'operations_per_second', 12),
    value('diskio', 'utilisation', { device: 'nvme0n1' }, 'percent', 3.5),
    value('diskio', 'write_bytes', { device: 'dm-0' }, 'bytes_per_second', 1024 * 1024),
    value('diskio', 'utilisation', { device: 'dm-0' }, 'percent', 3.4),
  ];

  it('renders one row per device and does not sum them', () => {
    // Stacked storage reports the same bytes at every layer. A total row
    // would be inflated by however many layers the host has, which is a
    // number nobody can see from here.
    render(<DiskPanel values={stacked} />);

    expect(screen.getByText('nvme0n1')).toBeInTheDocument();
    expect(screen.getByText('dm-0')).toBeInTheDocument();
    expect(screen.queryByText(/total/i)).toBeNull();
    expect(screen.getByText(/do not sum/i)).toBeInTheDocument();
  });

  it('formats throughput in binary multiples and operations in decimal', () => {
    render(<DiskPanel values={stacked} />);

    const row = screen.getByText('nvme0n1').closest('tr');
    expect(within(row as HTMLElement).getByText('1.00 MiB/s')).toBeInTheDocument();
    expect(within(row as HTMLElement).getByText('12 IOPS')).toBeInTheDocument();
  });

  it('shows busy time as saturation, labelled as such', () => {
    // A disk at 100% doing 2 MB/s is doing random I/O, which is the
    // diagnosis throughput alone never gives.
    render(<DiskPanel values={stacked} />);

    expect(
      screen.getByLabelText('nvme0n1 was busy 3.5% of the interval'),
    ).toBeInTheDocument();
  });
});

describe('NetworkPanel', () => {
  const interfaces = [
    value('network', 'receive_bytes', { interface: 'enp6s0' }, 'bytes_per_second', 1_048_576),
    value('network', 'transmit_bytes', { interface: 'enp6s0' }, 'bytes_per_second', 1024),
    value('network', 'receive_packets', { interface: 'enp6s0' }, 'packets_per_second', 900),
    value('network', 'transmit_packets', { interface: 'enp6s0' }, 'packets_per_second', 400),
    value('network', 'receive_errors', { interface: 'enp6s0' }, 'packets_per_second', 0),
    value('network', 'receive_drops', { interface: 'enp6s0' }, 'packets_per_second', 0),
    value('network', 'up', { interface: 'enp6s0' }, 'count', 1),
    value('network', 'speed', { interface: 'enp6s0' }, 'bits_per_second', 1_000_000_000),
  ];

  it('shows the link state and speed', () => {
    render(<NetworkPanel values={interfaces} />);

    const row = screen.getByText('enp6s0').closest('tr');
    expect(within(row as HTMLElement).getByText('up')).toBeInTheDocument();
    expect(within(row as HTMLElement).getByText('1 Gbit/s')).toBeInTheDocument();
  });

  it('does not call a tunnel down when the kernel says unknown', () => {
    // Most virtual interfaces report operstate=unknown. Rendering that as a
    // dead link would show every WireGuard tunnel on the host as broken.
    render(
      <NetworkPanel
        values={[value('network', 'receive_bytes', { interface: 'wg0' }, 'bytes_per_second', 10)]}
      />,
    );

    const row = screen.getByText('wg0').closest('tr');
    expect(within(row as HTMLElement).getByText('unknown')).toBeInTheDocument();
    expect(within(row as HTMLElement).queryByText('down')).toBeNull();
  });

  it('omits a speed the driver does not report rather than showing zero', () => {
    render(
      <NetworkPanel
        values={[
          value('network', 'receive_bytes', { interface: 'wg0' }, 'bytes_per_second', 10),
          value('network', 'up', { interface: 'wg0' }, 'count', 1),
        ]}
      />,
    );

    const row = screen.getByText('wg0').closest('tr');
    expect(within(row as HTMLElement).queryByText(/bit\/s/)).toBeNull();
  });

  it('says "none" when there are no errors or drops', () => {
    // On a healthy host these are all zero, and four columns of zeroes is
    // four columns of nothing.
    render(<NetworkPanel values={interfaces} />);

    const row = screen.getByText('enp6s0').closest('tr');
    expect(within(row as HTMLElement).getByText('none')).toBeInTheDocument();
  });

  it('names errors and drops separately once either is non-zero', () => {
    // They mean different things: an error is a frame the hardware could not
    // process and a drop is one the kernel discarded for want of queue
    // space. A combined "problems" figure would make an operator guess.
    render(
      <NetworkPanel
        values={[
          ...interfaces.filter((entry) => entry.series.metric !== 'receive_drops'),
          value('network', 'receive_drops', { interface: 'enp6s0' }, 'packets_per_second', 2.5),
        ]}
      />,
    );

    const row = screen.getByText('enp6s0').closest('tr');
    expect(within(row as HTMLElement).getByText(/rx drop 2\.5\/s/)).toBeInTheDocument();
  });

  it('says why some interfaces are missing', () => {
    // A user who knows they have a docker0 will look for it. The panel says
    // where its traffic went rather than leaving them to wonder.
    render(<NetworkPanel values={interfaces} />);

    expect(screen.getByText(/already counted elsewhere/i)).toBeInTheDocument();
  });
});

describe('column headings', () => {
  it('does not use one name for two columns', () => {
    // Found by reading the rendered table: the proportion bar and the byte
    // count were both headed "Used". Ambiguous to read, and worse to hear —
    // a screen reader announces the same column name twice with no way to
    // tell which is which.
    render(
      <FilesystemPanel
        values={[value('filesystem', 'utilisation', { mount: '/' }, 'percent', 10)]}
      />,
    );

    const headings = screen.getAllByRole('columnheader').map((cell) => cell.textContent);

    expect(new Set(headings).size).toBe(headings.length);
  });
});

describe('SensorPanel', () => {
  it('names each reading by its chip and keeps two drives apart', () => {
    // The whole point of the identity scheme: a machine with two NVMe
    // drives has two chips called `nvme`, each reporting `Composite`.
    render(
      <SensorPanel
        values={[
          value('sensors', 'temperature', { sensor: 'nvme-nvme0 Composite' }, 'celsius', 32.9),
          value('sensors', 'temperature', { sensor: 'nvme-nvme1 Composite' }, 'celsius', 34.9),
        ]}
      />,
    );

    expect(screen.getByText('nvme-nvme0 Composite')).toBeInTheDocument();
    expect(screen.getByText('nvme-nvme1 Composite')).toBeInTheDocument();
    expect(screen.getByText('32.9°C')).toBeInTheDocument();
  });

  it('shows a stopped fan as 0 RPM rather than hiding it', () => {
    // On a machine with a dead fan this is the most important number on the
    // page, and omitting it would make a failure look like a fan nobody is
    // watching.
    render(
      <SensorPanel
        values={[value('sensors', 'fan_speed', { sensor: 'z53 Fan speed' }, 'rpm', 0)]}
      />,
    );

    expect(screen.getByText('0 RPM')).toBeInTheDocument();
  });

  it('renders nothing when the host has no sensors', () => {
    const { container } = render(<SensorPanel values={[]} />);

    expect(container).toBeEmptyDOMElement();
  });
});

describe('ContainerPanel', () => {
  const containers = [
    value('docker', 'containers_total', {}, 'count', 3),
    value('docker', 'containers_running', {}, 'count', 1),
    value('docker', 'running', { container: 'web' }, 'count', 1),
    value('docker', 'cpu_utilisation', { container: 'web' }, 'percent', 212.5),
    value('docker', 'memory_bytes', { container: 'web' }, 'bytes', 128 * 1024 * 1024),
    value('docker', 'memory_utilisation', { container: 'web' }, 'percent', 12.4),
    value('docker', 'restart_count', { container: 'web' }, 'count', 2),
    value('docker', 'running', { container: 'old-worker' }, 'count', 0),
  ];

  it('lists stopped containers too, because that is the question people ask', () => {
    render(<ContainerPanel values={containers} />);

    const stopped = screen.getByText('old-worker').closest('tr');
    expect(within(stopped as HTMLElement).getByText('stopped')).toBeInTheDocument();
    // And its empty columns are em dashes, not zeros: the agent does not
    // query a stopped container, so there is nothing to render.
    expect(within(stopped as HTMLElement).getAllByText('—').length).toBeGreaterThan(0);
  });

  it('says it cannot change anything', () => {
    // This is monitoring. A reader should not have to find out by looking
    // for buttons that are not there.
    render(<ContainerPanel values={containers} />);

    expect(screen.getByText(/cannot start, stop or change anything/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /stop|restart|start/i })).toBeNull();
  });

  it('reports CPU above 100% without clamping it', () => {
    // A container using two cores reads 200% in `docker stats`, and
    // rescaling it to a share of the host would disagree with every other
    // tool the operator has.
    render(<ContainerPanel values={containers} />);

    const row = screen.getByText('web').closest('tr');
    expect(within(row as HTMLElement).getByText('212.5%')).toBeInTheDocument();
  });

  it('shows no health badge where the image declares no healthcheck', () => {
    render(<ContainerPanel values={containers} />);

    const row = screen.getByText('web').closest('tr');
    expect(within(row as HTMLElement).queryByText('healthy')).toBeNull();
    expect(within(row as HTMLElement).queryByText('unhealthy')).toBeNull();
  });

  it('badges an unhealthy container', () => {
    render(
      <ContainerPanel
        values={[
          ...containers,
          value('docker', 'healthy', { container: 'web' }, 'count', 0),
        ]}
      />,
    );

    const row = screen.getByText('web').closest('tr');
    expect(within(row as HTMLElement).getByText('unhealthy')).toBeInTheDocument();
  });

  it('renders nothing when the host runs no container monitoring', () => {
    const { container } = render(<ContainerPanel values={[]} />);

    expect(container).toBeEmptyDOMElement();
  });
});
