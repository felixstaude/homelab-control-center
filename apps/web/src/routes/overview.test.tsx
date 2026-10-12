import { screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { MOCK_CONTAINERS, MOCK_HOSTS } from '@/mocks/overview';
import { installFakeBackend, makeUser, type FakeBackend } from '@/test/fake-backend';
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

  it('marks every data card with a sample badge', async () => {
    await renderOverview();

    // One per card: resource chart, events, hosts, containers.
    expect(screen.getAllByText('Sample')).toHaveLength(4);
  });

  it('renders each mock host', async () => {
    await renderOverview();

    for (const host of MOCK_HOSTS) {
      expect(screen.getByText(host.hostname)).toBeInTheDocument();
    }
  });

  it('renders unknown-status hosts as having no data rather than zero usage', async () => {
    await renderOverview();

    // A host we have not heard from must not be shown as 0% and healthy.
    const unknownHost = MOCK_HOSTS.find((host) => host.status === 'unknown');
    expect(unknownHost).toBeDefined();
    expect(screen.getByText('No data')).toBeInTheDocument();
  });

  it('gives the host table an accessible caption', async () => {
    await renderOverview();

    expect(
      screen.getByRole('table', { name: /sample host inventory/i }),
    ).toBeInTheDocument();
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
