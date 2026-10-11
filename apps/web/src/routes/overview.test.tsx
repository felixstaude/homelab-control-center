import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';

import { App } from '@/App';
import { MOCK_HOSTS } from '@/mocks/overview';

function renderOverview() {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <App />
    </MemoryRouter>,
  );
}

describe('overview page', () => {
  /**
   * Engineering rule 9. This is the test that matters most on this page: if
   * someone removes the banner but leaves the fixtures, fabricated numbers
   * start looking like real telemetry.
   */
  it('declares its sample data as not being real measurements', () => {
    renderOverview();

    const notice = screen.getByRole('note');
    expect(notice).toHaveTextContent(/sample data/i);
    expect(notice).toHaveTextContent(/not real measurements/i);
  });

  it('marks every data card with a sample badge', () => {
    renderOverview();

    // One per card: resource chart, events, hosts, containers.
    expect(screen.getAllByText('Sample')).toHaveLength(4);
  });

  it('renders each mock host', () => {
    renderOverview();

    for (const host of MOCK_HOSTS) {
      expect(screen.getByText(host.hostname)).toBeInTheDocument();
    }
  });

  it('renders unknown-status hosts as having no data rather than zero usage', () => {
    renderOverview();

    // A host we have not heard from must not be shown as 0% and healthy.
    const unknownHost = MOCK_HOSTS.find((host) => host.status === 'unknown');
    expect(unknownHost).toBeDefined();
    expect(screen.getByText('No data')).toBeInTheDocument();
  });

  it('gives the host table an accessible caption', () => {
    renderOverview();

    expect(
      screen.getByRole('table', { name: /sample host inventory/i }),
    ).toBeInTheDocument();
  });
});
