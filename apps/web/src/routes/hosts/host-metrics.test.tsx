import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  installFakeBackend,
  makeHost,
  makeLatest,
  makeSeries,
  makeUser,
  userWithRole,
  type FakeBackend,
} from '@/test/fake-backend';
import { renderApp, waitForAuthToResolve } from '@/test/render-app';

/**
 * A host's measurements, and the four ways this screen could lie.
 *
 * Every test here is a form of one question: **can the interface show a number
 * that is not true?** The feature working is not the interesting property — a
 * dashboard that renders values is easy, and one that renders them only when
 * they are real is the whole job. So the assertions are mostly about what is
 * *not* on screen:
 *
 * - no zero where the value is unknown (§21)
 * - no stale reading presented as current
 * - no sample count, timestamp or module row invented for a host with none
 * - no chart line drawn across a period with no data (§22)
 *
 * The chart itself is asserted through its own unit tests
 * (`metric-grid.test.ts`): Recharts needs a layout to render, and jsdom has
 * none, so a test that mounted it would assert the fallback rather than the
 * drawing. What is asserted here is everything around it — which series are
 * offered, what the resolution label says, and that an empty range says why it
 * is empty.
 */

let backend: FakeBackend;

const ADMIN = makeUser({ user_id: 'u-admin', username: 'Morgan', role: 'admin' });

function enrolledHost() {
  return makeHost({
    host_id: 'host-1',
    name: 'homeserver',
    lifecycle: 'enrolled',
    connectivity: 'connected',
    credential_issued_at: '2026-10-12T18:56:00+00:00',
    last_heartbeat_at: new Date(Date.now() - 3000).toISOString(),
  });
}

afterEach(() => {
  backend.restore();
});

async function renderHost() {
  renderApp('/hosts/host-1');
  await waitForAuthToResolve();
  await screen.findByRole('heading', { name: /resources/i });
}

/** The Resources card, so an assertion cannot match text from another card. */
function resourcesCard(): HTMLElement {
  const heading = screen.getByRole('heading', { name: /resources/i });
  const card = heading.closest('div.rounded-xl, div[class*="rounded"]');
  return (card ?? heading.parentElement ?? document.body) as HTMLElement;
}

describe('a host that has measured nothing', () => {
  beforeEach(() => {
    backend = installFakeBackend({ session: ADMIN, hosts: [enrolledHost()] });
  });

  it('shows no values rather than a row of zeroes', async () => {
    await renderHost();

    // The specific forms a zero would take. Each of these on screen would be
    // a claim about a machine nobody measured.
    expect(screen.queryByText('0%')).toBeNull();
    expect(screen.queryByText('0.0')).toBeNull();
    expect(screen.queryByText('0 B')).toBeNull();
    expect(screen.queryByText('0.00')).toBeNull();
  });

  it('says what to do about it', async () => {
    await renderHost();

    expect(
      await screen.findByText(/No measurements have arrived yet/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/Nothing is enabled for this host/i)).toBeInTheDocument();
  });

  it('reports zero stored samples as zero, which is a count and not a measurement', async () => {
    // Worth distinguishing: "0 measurements stored" is a true statement about
    // the database. "CPU 0%" would be a false statement about the host. The
    // first is fine to show and the second is not.
    await renderHost();

    expect(await screen.findByText(/0 measurements stored/i)).toBeInTheDocument();
  });

  it('lists every module as having no data, rather than omitting them', async () => {
    // "This module has no data" and "this module does not exist on this
    // controller" are different answers.
    await renderHost();

    const card = resourcesCard();
    await waitFor(() => {
      expect(within(card).getAllByText('No data').length).toBeGreaterThan(0);
    });
    for (const name of ['CPU utilisation', 'Memory', 'Swap', 'Load average', 'System uptime']) {
      expect(within(card).getByText(name)).toBeInTheDocument();
    }
    // One per module in the registry, including the four nobody enabled.
    expect(within(card).getAllByText('No data')).toHaveLength(5);
  });

  it('offers no history chart, because there is no series to chart', async () => {
    await renderHost();

    expect(screen.queryByRole('heading', { name: /^history$/i })).toBeNull();
  });
});

describe('a host with current values', () => {
  beforeEach(() => {
    backend = installFakeBackend({
      session: ADMIN,
      hosts: [enrolledHost()],
      latestMetrics: {
        'host-1': [
          makeLatest({ series: makeSeries({ series_id: 1 }), value: 37.25 }),
          makeLatest({
            series: makeSeries({
              series_id: 2,
              module: 'memory',
              module_name: 'Memory',
              metric: 'used_bytes',
              metric_name: 'Used',
              unit: 'bytes',
            }),
            value: 8 * 1024 ** 3,
          }),
          makeLatest({
            series: makeSeries({
              series_id: 3,
              module: 'load',
              module_name: 'Load average',
              metric: 'load1',
              metric_name: '1 minute',
              unit: 'load',
            }),
            value: 0.42,
          }),
        ],
      },
      metricSeries: {
        'host-1': [
          makeSeries({ series_id: 1 }),
          makeSeries({ series_id: 2, module: 'memory', module_name: 'Memory', unit: 'bytes' }),
          makeSeries({ series_id: 3, module: 'load', module_name: 'Load average', unit: 'load' }),
        ],
      },
      metricSampleCount: { 'host-1': 4096 },
    });
  });

  it('renders each value in its own unit', async () => {
    await renderHost();

    // Percent to one decimal, bytes in binary multiples, load with no unit at
    // all — the three cases that would be wrong if one formatter handled them.
    expect(await screen.findByText('37.3')).toBeInTheDocument();
    expect(screen.getByText('8.00')).toBeInTheDocument();
    expect(screen.getByText('GiB')).toBeInTheDocument();
    expect(screen.getByText('0.42')).toBeInTheDocument();
  });

  it('states the freshness window it applied', async () => {
    // So the interface says "current within 5 min" rather than implying "now".
    await renderHost();

    expect(await screen.findByText(/Current within 5 min/i)).toBeInTheDocument();
  });

  it('shows each value with its age', async () => {
    await renderHost();

    // Not decoration: a reading is only current inside the window, and the age
    // is what lets somebody judge it rather than trust it. A fixture five
    // seconds old renders as "just now" — `relativeTime` only says "ago" past a
    // minute — so this accepts either rather than pinning the wording.
    // The module name and the age, which is what the tile caption is for.
    // Matched exactly rather than by prefix: the monitoring card's rows also
    // begin with a module name, and a loose pattern would pass on those.
    expect(await screen.findByText('CPU \u00b7 just now')).toBeInTheDocument();
    expect(screen.getByText('Memory \u00b7 just now')).toBeInTheDocument();
    expect(screen.getByText('Load average \u00b7 just now')).toBeInTheDocument();
  });

  it('offers a history chart with one series at a time', async () => {
    // Overlaying memory in bytes on CPU in percent needs two axes to be
    // truthful, so the picker is deliberately single-select.
    await renderHost();

    expect(await screen.findByRole('heading', { name: /^history$/i })).toBeInTheDocument();
    const picker = screen.getByLabelText<HTMLSelectElement>('Series');
    expect(picker).toBeInstanceOf(HTMLSelectElement);
    expect(picker.options).toHaveLength(3);
  });

  it('orders the series by the catalogue, not by id or alphabet', async () => {
    await renderHost();

    const picker = await screen.findByLabelText<HTMLSelectElement>('Series');
    const labels = [...picker.options].map((option) => option.textContent);
    expect(labels[0]).toMatch(/CPU/);
    expect(labels[1]).toMatch(/Memory/);
    expect(labels[2]).toMatch(/Load average/);
  });

  it('lets the operator choose a range', async () => {
    await renderHost();

    const windows = await screen.findByLabelText<HTMLSelectElement>('Time range');
    const labels = [...windows.options].map((option) => option.textContent);
    expect(labels).toEqual([
      'Last hour',
      'Last 6 hours',
      'Last 24 hours',
      'Last 7 days',
      'Last 30 days',
    ]);
  });

  it('asks the server for a zoned time range', async () => {
    // The server refuses a naive timestamp rather than assuming UTC, so a
    // browser in another zone cannot silently ask for the wrong window. This
    // asserts the client holds up its end.
    await renderHost();

    await waitFor(() => {
      expect(backend.requests.some((request) => request.path.endsWith('/metrics/range'))).toBe(
        true,
      );
    });
    const request = backend.requests.find((entry) => entry.path.endsWith('/metrics/range'));
    const params = new URLSearchParams(request?.query ?? '');
    expect(params.get('start')).toMatch(/(?:Z|[+-]\d{2}:?\d{2})$/);
    expect(params.get('end')).toMatch(/(?:Z|[+-]\d{2}:?\d{2})$/);
    expect(params.get('series_id')).toBe('1');
  });

  it('does not name a resolution, letting the range and budget choose', async () => {
    // Naming one can return a truncated series; the automatic choice always
    // fits. The response says which tier answered either way.
    await renderHost();

    await waitFor(() => {
      expect(backend.requests.some((request) => request.path.endsWith('/metrics/range'))).toBe(
        true,
      );
    });
    const request = backend.requests.find((entry) => entry.path.endsWith('/metrics/range'));
    expect(new URLSearchParams(request?.query ?? '').get('resolution')).toBeNull();
  });
});

describe('a host whose readings have gone stale', () => {
  beforeEach(() => {
    const threeDaysAgo = new Date(Date.now() - 3 * 86_400_000).toISOString();
    backend = installFakeBackend({
      session: ADMIN,
      hosts: [makeHost({ ...enrolledHost(), connectivity: 'offline' })],
      latestMetrics: {
        'host-1': [
          makeLatest({
            series: makeSeries({ series_id: 1, last_sample_at: threeDaysAgo }),
            value: 73.5,
            recorded_at: threeDaysAgo,
          }),
        ],
      },
      metricSeries: { 'host-1': [makeSeries({ series_id: 1, last_sample_at: threeDaysAgo })] },
      metricSampleCount: { 'host-1': 2048 },
    });
  });

  it('does not present a three-day-old reading as the current one', async () => {
    // The whole design of the latest endpoint. A value returned without
    // comment would be shown as "now", and nothing in the response would say
    // otherwise.
    await renderHost();

    expect(screen.queryByText('73.5')).toBeNull();
  });

  it('says nothing was measured recently, and when it last was', async () => {
    await renderHost();

    expect(await screen.findByText(/Nothing measured in the last 5 min/i)).toBeInTheDocument();
    expect(screen.getByText(/most recent measurement arrived/i)).toBeInTheDocument();
  });

  it('names the likely reason, which depends on the agent being connected', async () => {
    // "The agent is not connected" and "check whether its modules are still
    // enabled" send somebody to different places, so the message picks one.
    await renderHost();

    expect(await screen.findByText(/agent is not connected/i)).toBeInTheDocument();
  });

  it('still lists the series, so the history remains reachable', async () => {
    // "No CPU data" and "CPU data until Tuesday" are different answers, and
    // the second must not be rendered as the first.
    await renderHost();

    await waitFor(() => {
      expect(backend.requests.some((r) => r.path.endsWith('/metrics/status'))).toBe(true);
    });
    const card = resourcesCard();
    expect(within(card).getByText(/2,048 measurements stored/i)).toBeInTheDocument();
  });
});

describe('a host that cannot be collecting at all', () => {
  it('says it has not been enrolled, rather than that it has no data', async () => {
    backend = installFakeBackend({
      session: ADMIN,
      hosts: [makeHost({ host_id: 'host-1', name: 'new-box', lifecycle: 'registered' })],
    });
    await renderHost();

    expect(await screen.findByText(/has not been enrolled/i)).toBeInTheDocument();
    // And points at the actual next step rather than at the monitoring card,
    // which would be useless until an agent exists.
    expect(screen.getByText(/hcc-agent enroll/)).toBeInTheDocument();
  });

  it('says it is disabled, which is a different remedy', async () => {
    backend = installFakeBackend({
      session: ADMIN,
      hosts: [makeHost({ ...enrolledHost(), is_enabled: false })],
    });
    await renderHost();

    const alerts = await screen.findAllByText(/disabled/i);
    expect(alerts.length).toBeGreaterThan(0);
    expect(screen.getByText(/no measurements can arrive/i)).toBeInTheDocument();
  });
});

describe('every role may read metrics', () => {
  it.each(['viewer', 'operator', 'admin'] as const)(
    'shows the values to a %s',
    async (role) => {
      // A viewer who cannot see a chart cannot use a monitoring product.
      backend = installFakeBackend({
        session: userWithRole(role),
        hosts: [enrolledHost()],
        latestMetrics: {
          'host-1': [makeLatest({ series: makeSeries({ series_id: 1 }), value: 55.5 })],
        },
        metricSeries: { 'host-1': [makeSeries({ series_id: 1 })] },
      });
      await renderHost();

      expect(await screen.findByText('55.5')).toBeInTheDocument();
    },
  );
});

describe('a failed read', () => {
  it('offers a retry rather than an empty card', async () => {
    // An error must not look like "this host has no data", which is the one
    // misreading that would send somebody to debug the wrong machine.
    backend = installFakeBackend({
      session: ADMIN,
      hosts: [enrolledHost()],
      routeFailures: {
        '/metrics/latest': {
          status: 500,
          code: 'internal_error',
          message: 'Something went wrong.',
        },
      },
    });
    await renderHost();

    expect(
      await screen.findByText(/Could not load the current values/i),
    ).toBeInTheDocument();
    const retry = screen.getByRole('button', { name: /try again|retry/i });
    await userEvent.click(retry);
    expect(retry).toBeInTheDocument();
  });
});
