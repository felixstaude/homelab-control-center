import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { Button } from '@/components/ui/button';

describe('Button', () => {
  it('defaults to type="button" so it cannot accidentally submit a form', () => {
    render(<Button>Restart</Button>);

    expect(screen.getByRole('button', { name: 'Restart' })).toHaveAttribute('type', 'button');
  });

  it('honours an explicit type', () => {
    render(<Button type="submit">Save</Button>);

    expect(screen.getByRole('button', { name: 'Save' })).toHaveAttribute('type', 'submit');
  });

  it('calls its handler on click', async () => {
    const onClick = vi.fn();
    render(<Button onClick={onClick}>Stop</Button>);

    await userEvent.click(screen.getByRole('button', { name: 'Stop' }));

    expect(onClick).toHaveBeenCalledOnce();
  });

  it('does not fire when disabled', async () => {
    const onClick = vi.fn();
    render(
      <Button disabled onClick={onClick}>
        Stop
      </Button>,
    );

    await userEvent.click(screen.getByRole('button', { name: 'Stop' }));

    expect(onClick).not.toHaveBeenCalled();
  });

  it('renders as its child element when asChild is set', () => {
    render(
      <Button asChild>
        <a href="/hosts">Hosts</a>
      </Button>,
    );

    // Must be a link, not a button wrapping a link (invalid HTML).
    expect(screen.getByRole('link', { name: 'Hosts' })).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('merges caller classes over component defaults', () => {
    render(<Button className="w-full">Wide</Button>);

    expect(screen.getByRole('button', { name: 'Wide' })).toHaveClass('w-full');
  });
});
