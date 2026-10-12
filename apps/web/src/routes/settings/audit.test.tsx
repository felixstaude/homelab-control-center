import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  installFakeBackend,
  makeAuditEvent,
  makeUser,
  userWithRole,
  type FakeBackend,
} from '@/test/fake-backend';
import { renderApp, waitForAuthToResolve } from '@/test/render-app';

/**
 * The audit viewer.
 *
 * The property worth testing hardest is the one that is easy to get wrong in a
 * way nobody notices for a year: an action this build does not recognise must
 * still render. The vocabulary grows with every milestone (ADR-038), so a
 * viewer that showed "Unknown action" — or worse, dropped the row — would
 * quietly hide exactly the newest events.
 */

let backend: FakeBackend;

const ADMIN = makeUser({ user_id: 'u-admin', username: 'Morgan', role: 'admin' });

afterEach(() => {
  backend.restore();
});

async function renderAudit() {
  renderApp('/settings/audit');
  await waitForAuthToResolve();
  await screen.findByRole('table', { name: /audit events/i });
}

describe('audit list', () => {
  beforeEach(() => {
    backend = installFakeBackend({
      session: ADMIN,
      auditEvents: [
        makeAuditEvent({
          event_id: 'e-1',
          action: 'user.role_changed',
          outcome: 'success',
          // The actor is the signed-in administrator, so the fixture and the
          // session agree about who did it.
          actor: { user_id: 'u-admin', username: 'Morgan', display_name: null },
          details: { from_role: 'viewer', to_role: 'operator' },
        }),
        makeAuditEvent({
          event_id: 'e-2',
          action: 'admin.access_denied',
          outcome: 'denied',
          target: null,
          details: { path: '/api/v1/users', required_role: 'admin', actual_role: 'viewer' },
        }),
      ],
    });
  });

  it('phrases a known action in human terms', async () => {
    await renderAudit();

    // Scoped to the table: the filter offers the same phrasing as an
    // `<option>`, so an unscoped query matches twice.
    const table = screen.getByRole('table', { name: /audit events/i });
    expect(within(table).getByText('Role changed')).toBeInTheDocument();
    expect(within(table).getByText('Administrative access denied')).toBeInTheDocument();
  });

  it('shows the outcome, with a denial as the loudest', async () => {
    await renderAudit();

    expect(screen.getByText('success')).toBeInTheDocument();
    expect(screen.getByText('denied')).toBeInTheDocument();
  });

  it('resolves the actor and target by name', async () => {
    await renderAudit();

    const row = screen.getByRole('row', { name: /Role changed/ });
    expect(within(row).getByText('Morgan')).toBeInTheDocument();
    expect(within(row).getByText('someone')).toBeInTheDocument();
  });

  it('renders the details the writer recorded', async () => {
    await renderAudit();

    const row = screen.getByRole('row', { name: /Role changed/ });
    expect(within(row).getByText('from_role')).toBeInTheDocument();
    expect(within(row).getByText('viewer')).toBeInTheDocument();
    expect(within(row).getByText('to_role')).toBeInTheDocument();
  });

  it('shows a dash where an action has no target', async () => {
    await renderAudit();

    const row = screen.getByRole('row', { name: /Administrative access denied/ });
    expect(within(row).getAllByText('—').length).toBeGreaterThan(0);
  });

  it('reports a deleted account honestly rather than inventing a name', async () => {
    // `audit_events` carries no foreign key, so an id can outlive its row —
    // which is the point. Showing the id is accurate; "Unknown user" would be
    // an invention, and dropping the event would hide it.
    backend.state.auditEvents = [
      makeAuditEvent({
        event_id: 'e-gone',
        target: {
          user_id: '0f3c1a2b-0000-4000-8000-0000000000ff',
          username: null,
          display_name: null,
        },
      }),
    ];
    await renderAudit();

    expect(screen.getByText(/0f3c1a2b….*deleted/)).toBeInTheDocument();
  });

  it('renders an action this build does not know about', async () => {
    /*
     * The forward-compatibility property. A newer control plane records
     * `host.enrolled`; this build has never heard of it and must still show
     * the row, with the identifier it was given.
     */
    backend.state.auditEvents = [
      makeAuditEvent({ event_id: 'e-future', action: 'host.enrolled' }),
    ];
    await renderAudit();

    expect(screen.getByText('host.enrolled')).toBeInTheDocument();
    expect(screen.queryByText(/unknown action/i)).not.toBeInTheDocument();
  });

  it('renders an outcome this build does not know about', async () => {
    backend.state.auditEvents = [
      makeAuditEvent({ event_id: 'e-odd', outcome: 'partial' }),
    ];
    await renderAudit();

    expect(screen.getByText('partial')).toBeInTheDocument();
  });

  it('escapes a details value rather than rendering it as markup', async () => {
    // Audit-log injection, in the shape it actually takes: a stored value the
    // viewer renders as HTML. React escapes by default; this pins it.
    backend.state.auditEvents = [
      makeAuditEvent({
        event_id: 'e-injection',
        details: { reason: '<img src=x onerror="alert(1)">' },
      }),
    ];
    await renderAudit();

    expect(screen.getByText('<img src=x onerror="alert(1)">')).toBeInTheDocument();
    expect(document.querySelector('img[src="x"]')).toBeNull();
  });

  it('states the trail is append-only with no endpoint to write to it', async () => {
    await renderAudit();

    expect(screen.getByText(/Append-only/i)).toBeInTheDocument();
    expect(screen.getByText(/no endpoint that can add to it/i)).toBeInTheDocument();
  });

  it('offers no control that would write or delete an event', async () => {
    await renderAudit();

    expect(screen.queryByRole('button', { name: /delete/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /clear/i })).not.toBeInTheDocument();
  });
});

describe('audit filtering', () => {
  beforeEach(() => {
    backend = installFakeBackend({
      session: ADMIN,
      auditEvents: [
        makeAuditEvent({ event_id: 'e-1', action: 'user.created' }),
        makeAuditEvent({ event_id: 'e-2', action: 'user.deactivated' }),
      ],
    });
  });

  it("offers the server's own vocabulary rather than a hard-coded list", async () => {
    await renderAudit();

    await waitFor(() => {
      expect(backend.requestsFor('/audit/actions')).toHaveLength(1);
    });
    const select = screen.getByLabelText('Filter by action');
    expect(within(select).getByRole('option', { name: 'Account created' })).toBeInTheDocument();
  });

  it('sends the action as a query parameter', async () => {
    const user = userEvent.setup();
    await renderAudit();

    await user.selectOptions(screen.getByLabelText('Filter by action'), 'user.created');

    await waitFor(() => {
      expect(
        backend
          .requestsFor('/audit/events')
          .some((request) => request.query.includes('action=user.created')),
      ).toBe(true);
    });
    const table = screen.getByRole('table', { name: /audit events/i });
    await waitFor(() => {
      expect(within(table).queryByText('Account deactivated')).not.toBeInTheDocument();
    });
    expect(within(table).getByText('Account created')).toBeInTheDocument();
  });

  it('shows an empty state scoped to the filter', async () => {
    const user = userEvent.setup();
    await renderAudit();

    await user.selectOptions(
      screen.getByLabelText('Filter by action'),
      'account.password_changed',
    );

    expect(await screen.findByText('No events of that kind yet')).toBeInTheDocument();
  });
});

describe('audit pagination', () => {
  beforeEach(() => {
    // Thirty events against a page size of twenty-five.
    backend = installFakeBackend({
      session: ADMIN,
      auditEvents: Array.from({ length: 30 }, (_, index) =>
        makeAuditEvent({
          event_id: `e-${index}`,
          action: 'user.created',
          details: { index },
        }),
      ),
    });
  });

  it('walks forward with the cursor the server supplied', async () => {
    const user = userEvent.setup();
    await renderAudit();

    await user.click(screen.getByRole('button', { name: /Next/ }));

    await waitFor(() => {
      expect(
        backend
          .requestsFor('/audit/events')
          .some((request) => request.query.includes('cursor=')),
      ).toBe(true);
    });
    // The last five events, which were not on page one.
    expect(await screen.findByText('29')).toBeInTheDocument();
  });

  it('walks back through the cursors it has already used', async () => {
    // Keyset pagination is forward-only — the server does not hand out a
    // previous cursor — so "previous" is navigation through the client's own
    // history. This asserts the history is kept.
    const user = userEvent.setup();
    await renderAudit();

    expect(screen.getByRole('button', { name: /Previous/ })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: /Next/ }));
    await screen.findByText('29');

    await user.click(screen.getByRole('button', { name: /Previous/ }));

    expect(await screen.findByText('0')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Previous/ })).toBeDisabled();
  });

  it('resets the cursor history when the filter changes', async () => {
    // A new filter is a new ordering, so a cursor from the old result set
    // names a position that no longer means anything — the next request must
    // carry none.
    const user = userEvent.setup();
    await renderAudit();

    await user.click(screen.getByRole('button', { name: /Next/ }));
    await screen.findByText('29');

    await user.selectOptions(screen.getByLabelText('Filter by action'), 'user.deactivated');

    await waitFor(() => {
      const latest = backend.requestsFor('/audit/events').at(-1);
      expect(latest?.query).toContain('action=user.deactivated');
      expect(latest?.query).not.toContain('cursor=');
    });
  });

  it('reports the total as a count, not as a page range', async () => {
    await renderAudit();

    expect(screen.getByRole('status')).toHaveTextContent('30 events recorded');
  });

  it('refuses a corrupted cursor rather than silently showing page one', async () => {
    backend = installFakeBackend({
      session: ADMIN,
      routeFailures: {
        '/audit/events': {
          status: 422,
          code: 'validation_failed',
          message:
            'The pagination cursor is not valid. It should be the `next_cursor` ' +
            'from a previous response, unmodified.',
        },
      },
    });
    renderApp('/settings/audit');
    await waitForAuthToResolve();

    expect(await screen.findByText(/Could not load the audit trail/i)).toBeInTheDocument();
    expect(screen.getByText(/cursor is not valid/i)).toBeInTheDocument();
  });
});

describe('audit permissions', () => {
  it.each(['viewer', 'operator'] as const)(
    'explains the refusal to a %s who navigates directly',
    async (role) => {
      // The trail names every account and every administrative action — for a
      // lower role that is the user list the listing endpoint refuses them.
      backend = installFakeBackend({ session: userWithRole(role) });
      renderApp('/settings/audit');
      await waitForAuthToResolve();

      expect(
        await screen.findByText(/The audit trail requires the Administrator role/i),
      ).toBeInTheDocument();
      expect(backend.requestsFor('/audit/events')).toHaveLength(0);
    },
  );
});
