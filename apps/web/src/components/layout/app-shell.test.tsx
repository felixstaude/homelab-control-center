import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';

import { App } from '@/App';
import { NAV_ITEMS } from '@/components/layout/navigation';

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
}

describe('application shell', () => {
  it('renders a navigation link for every section', () => {
    renderAt('/');
    const nav = screen.getByRole('navigation', { name: 'Primary' });

    for (const item of NAV_ITEMS) {
      expect(screen.getAllByRole('link', { name: item.label }).length).toBeGreaterThan(0);
    }
    // The desktop sidebar renders exactly one link per section.
    expect(nav).toBeInTheDocument();
  });

  it('marks the active route with aria-current for screen readers', () => {
    renderAt('/docker');

    const activeLink = screen.getByRole('link', { name: 'Docker' });
    expect(activeLink).toHaveAttribute('aria-current', 'page');

    expect(screen.getByRole('link', { name: 'Hosts' })).not.toHaveAttribute('aria-current');
  });

  it('exposes a skip-to-content link as the first focusable element', () => {
    renderAt('/');

    const skipLink = screen.getByRole('link', { name: 'Skip to content' });
    expect(skipLink).toHaveAttribute('href', '#main-content');
  });

  it('labels the main landmark so the skip link has a target', () => {
    renderAt('/');

    expect(screen.getByRole('main')).toHaveAttribute('id', 'main-content');
  });

  it('shows the current section name in the header', () => {
    renderAt('/alerts');

    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Alerts');
  });
});

describe('routing', () => {
  it.each(
    NAV_ITEMS.map((item) => [item.to, item.label] as const),
  )('renders the %s route without crashing', (path, label) => {
    renderAt(path);
    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent(label);
  });

  it('renders a 404 page for an unknown route', () => {
    renderAt('/no-such-page');

    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent('Page not found');
  });

  it('redirects /overview to the index route', () => {
    renderAt('/overview');

    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent('Overview');
  });
});
