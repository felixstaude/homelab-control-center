import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import {
  STATUS_LABELS,
  StatusBadge,
  StatusDot,
  type HealthStatus,
} from '@/components/ui/status-indicator';

const ALL_STATUSES: readonly HealthStatus[] = ['ok', 'warn', 'critical', 'unknown'];

describe('StatusDot', () => {
  it('is hidden from assistive technology because colour alone conveys nothing', () => {
    const { container } = render(<StatusDot status="ok" />);

    expect(container.querySelector('[aria-hidden="true"]')).toBeInTheDocument();
  });
});

describe('StatusBadge', () => {
  it.each(ALL_STATUSES)('pairs the %s dot with a readable text label', (status) => {
    render(<StatusBadge status={status} />);

    expect(screen.getByText(STATUS_LABELS[status])).toBeInTheDocument();
  });

  it('accepts a domain-specific label override', () => {
    render(<StatusBadge status="ok" label="Running" />);

    expect(screen.getByText('Running')).toBeInTheDocument();
    expect(screen.queryByText('Healthy')).not.toBeInTheDocument();
  });

  it('defines a label for every status in the health vocabulary', () => {
    // Guards against a new status being added without a label, which would
    // render an empty badge.
    for (const status of ALL_STATUSES) {
      expect(STATUS_LABELS[status]).toBeTruthy();
    }
  });
});
