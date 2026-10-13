import { screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { MOCK_CONTAINERS } from '@/mocks/overview';
import {
  installFakeBackend,
  makeHost,
  makeLatest,
  makeSeries,
  makeUser,
  type FakeBackend,
} from '@/test/fake-backend';
import { renderApp, waitForAuthToResolve } from '@/test/render-app';

let backend: FakeBackend;

// The overview is behind authentication as of milestone 1.6, so these render
// it the way a signed-in operator reaches it.
beforeEach(() => {
  backend = installFakeBackend({ initialized: true, session: makeUser() });
});

afterEach(() => {
  backend.restore();
});

async function renderOverview() {
  renderApp('/');
  await waitForAuthToResolve();
}

describe('overview page', () => {
  /**
   * Engineering rule 9. This is the test that matters most on this page: if
   * someone removes the banner but leaves the fixtures, fabricated numbers
   * start looking like real telemetry.
   */
  it('declares its sample data as not being real measurements', async () => {
    await renderOverview();

    const notice = screen.getByRole('note');
    expect(notice).toHaveTextContent(/sample data/i);
    expect(notice).toHaveTextContent(/not real measurements/i);
  });

  it('marks every remaining sample card with a sample badge', async () => {
    await renderOverview();

    // Three now, not four: the host table is real and carries a "Live" badge
    // instead. What is left is the resource chart, the events list and the
    // container card — the features that do not exist yet.
    expect(screen.getAllByText('Sample')).toHaveLength(3);
  });

  it('shows no host table rows when the registry is empty, rather than fixtures', async () => {
    await renderOverview();

    expect(await screen.findByText(/No hosts yet/i)).toBeInTheDocument();
    // The old fixture names must not survive anywhere on the page.
    expect(screen.queryByText('sample-nas')).toBeNull();
    expect(screen.queryByText('sample-compute')).toBeNull();
  });

  it('lists only genuinely unhealthy containers under "needing attention"', async () => {
    /*
     * The section used to render every fixture, so two healthy `running`
     * containers appeared under a heading claiming they needed attention. On a
     * monitoring dashboard that is the worst kind of wrong: it teaches the
     * reader that the section's title means nothing, in the one place they
     * look when something is broken.
     */
    await renderOverview();

    const unhealthy = MOCK_CONTAINERS.filter((container) => container.state !== 'running');
    const healthy = MOCK_CONTAINERS.filter((container) => container.state === 'running');

    // The fixtures must contain some of each, or this test proves nothing.
    expect(unhealthy.length).toBeGreaterThan(0);
    expect(healthy.length).toBeGreaterThan(0);

    // Scoped to the card: a container name also appears as a log entry's
    // source in the events list, so an unscoped query is ambiguous.
    const heading = screen.getByText('Containers needing attention');
    // `closest` is typed as returning `Element`; the queries need an
    // `HTMLElement`, and in a rendered document it always is one.
    const card = heading.closest<HTMLElement>('div[class*="rounded-xl"]');
    expect(card).not.toBeNull();

    for (const container of unhealthy) {
      expect(within(card!).getByText(container.name)).toBeInTheDocument();
    }
    for (const container of healthy) {
      expect(within(card!).queryByText(container.name)).not.toBeInTheDocument();
    }
  });

  it('does not render the page name twice on screen', async () => {
    // The shell header owns the page `<h1>`; the `PageHeader` used to repeat
    // it eight pixels below as a visible `<h2>`. Both headings still exist —
    // the second is `sr-only`, so the outline is intact — and exactly one is
    // visible.
    await renderOverview();

    const headings = screen
      .getAllByRole('heading')
      .filter((heading) => heading.textContent === 'Overview');

    expect(headings).toHaveLength(2);
    expect(headings.filter((heading) => !heading.className.includes('sr-only'))).toHaveLength(
      1,
    );
  });

  it('keeps the page heading in the accessibility tree', async () => {
    // Hidden visually, not removed: the sections still sit beneath it, and
    // deleting it would leave the content with no heading of its own.
    await renderOverview();

    expect(screen.getByRole('heading', { name: 'Overview', level: 1 })).toBeInTheDocument();
  });
});

/**
 * The measured half of the page.
 *
 * Seeded with a real registry and real current values, because the point of
 * these is that the numbers come from the backend rather than from fixtures —
 * and the table does not exist at all when there are no hosts.
 */
describe('the measured fleet section', () => {
  beforeEach(() => {
    backend = installFakeBackend({
      initialized: true,
      session: makeUser(),
      hosts: [
        makeHost({
          host_id: 'host-1',
          name: 'homeserver',
          lifecycle: 'enrolled',
          connectivity: 'connected',
          environment: 'production',
        }),
        makeHost({
          host_id: 'host-2',
          name: 'quiet-box',
          lifecycle: 'enrolled',
          connectivity: 'offline',
        }),
      ],
      latestMetrics: {
        'host-1': [
          makeLatest({ series: makeSeries({ series_id: 1 }), value: 61.5 }),
          makeLatest({
            series: makeSeries({
              series_id: 2,
              module: 'memory',
              module_name: 'Memory',
              metric: 'utilisation',
              metric_name: 'Utilisation',
            }),
            value: 42,
          }),
        ],
      },
      metricSeries: { 'host-1': [makeSeries({ series_id: 1 })] },
    });
  });

  it('gives the measured host table an accessible caption', async () => {
    await renderOverview();

    expect(
      await screen.findByRole('table', { name: /registered hosts/i }),
    ).toBeInTheDocument();
  });

  it('does not put the sample banner over the measured section', async () => {
    // §25 in the direction people forget. A real host count under a "sample
    // data" banner teaches an operator to disbelieve the one number on the
    // page that is true, and after that the banner means nothing anywhere.
    await renderOverview();

    const measured = await screen.findByRole('table', { name: /registered hosts/i });
    expect(screen.getByRole('note').contains(measured)).toBe(false);
    const card = measured.closest<HTMLElement>('div[class*="rounded-xl"]');
    expect(card).not.toBeNull();
    expect(within(card!).queryByText('Sample')).toBeNull();
    expect(within(card!).getByText('Live')).toBeInTheDocument();
  });

  it('counts the real hosts and their connectivity', async () => {
    await renderOverview();

    expect(await screen.findByText('1 connected · 1 not connected')).toBeInTheDocument();
  });

  it('names the host a fleet peak belongs to', async () => {
    // "61.5%" prompts "on what?", and a dashboard that cannot answer that is a
    // number somebody has to go and look up.
    await renderOverview();

    expect(await screen.findByText('61.5')).toBeInTheDocument();
    expect(screen.getAllByText(/homeserver ·/).length).toBeGreaterThan(0);
  });

  it('shows a dash for a host that is reporting nothing, never a zero', async () => {
    // The most dangerous possible misreading: 0% reads as "idle and healthy",
    // which is the one wrong answer that looks fine.
    await renderOverview();

    const table = await screen.findByRole('table', { name: /registered hosts/i });
    const row = within(table).getByRole('row', { name: /quiet-box/i });
    expect(within(row).getAllByText('—').length).toBeGreaterThanOrEqual(2);
    expect(within(row).queryByText('0.0%')).toBeNull();
    expect(within(row).queryByText('0%')).toBeNull();
  });

  it('offers no column for data this phase does not collect', async () => {
    // A column of em dashes implies the value exists and is merely missing for
    // these hosts. Disk and OS are gone until there is something to put in
    // them.
    await renderOverview();

    const table = await screen.findByRole('table', { name: /registered hosts/i });
    expect(within(table).queryByRole('columnheader', { name: /disk/i })).toBeNull();
    expect(within(table).queryByRole('columnheader', { name: /^os$/i })).toBeNull();
    expect(within(table).getByRole('columnheader', { name: /cpu/i })).toBeInTheDocument();
  });

  it('links each host to its own page', async () => {
    await renderOverview();

    const link = await screen.findByRole('link', { name: 'homeserver' });
    expect(link).toHaveAttribute('href', '/hosts/host-1');
  });
});
