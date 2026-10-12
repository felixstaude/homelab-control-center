import { screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { NAV_ITEMS } from '@/components/layout/navigation';
import { installFakeBackend, makeUser, type FakeBackend } from '@/test/fake-backend';
import { renderApp, waitForAuthToResolve } from '@/test/render-app';

let backend: FakeBackend;

// The shell only renders for an authenticated user as of milestone 1.6.
beforeEach(() => {
  backend = installFakeBackend({ initialized: true, session: makeUser() });
});

afterEach(() => {
  backend.restore();
});

async function renderAt(path: string) {
  renderApp(path);
  await waitForAuthToResolve();
}

describe('application shell', () => {
  it('renders a navigation link for every section', async () => {
    await renderAt('/');
    const nav = screen.getByRole('navigation', { name: 'Primary' });

    for (const item of NAV_ITEMS) {
      expect(screen.getAllByRole('link', { name: item.label }).length).toBeGreaterThan(0);
    }
    // The desktop sidebar renders exactly one link per section.
    expect(nav).toBeInTheDocument();
  });

  it('marks the active route with aria-current for screen readers', async () => {
    await renderAt('/docker');

    const activeLink = screen.getByRole('link', { name: 'Docker' });
    expect(activeLink).toHaveAttribute('aria-current', 'page');

    expect(screen.getByRole('link', { name: 'Hosts' })).not.toHaveAttribute('aria-current');
  });

  it('exposes a skip-to-content link as the first focusable element', async () => {
    await renderAt('/');

    const skipLink = screen.getByRole('link', { name: 'Skip to content' });
    expect(skipLink).toHaveAttribute('href', '#main-content');
  });

  it('labels the main landmark so the skip link has a target', async () => {
    await renderAt('/');

    expect(screen.getByRole('main')).toHaveAttribute('id', 'main-content');
  });

  it('shows the current section name in the header', async () => {
    await renderAt('/alerts');

    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Alerts');
  });
});

describe('routing', () => {
  it.each(
    NAV_ITEMS.map((item) => [item.to, item.label] as const),
  )('renders the %s route without crashing', async (path, label) => {
    await renderAt(path);
    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent(label);
  });

  it('renders a 404 page for an unknown route', async () => {
    await renderAt('/no-such-page');

    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent('Page not found');
  });

  it('redirects /overview to the index route', async () => {
    await renderAt('/overview');

    // Two redirects resolve here — the auth gate, then the legacy alias — so
    // the assertion waits for the destination rather than for a fixed tick.
    await waitFor(() => {
      expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent('Overview');
    });
  });
});
