import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  installFakeBackend,
  makeAuditEvent,
  makeManagedUser,
  makeUser,
  type FakeBackend,
} from '@/test/fake-backend';
import { renderApp, waitForAuthToResolve } from '@/test/render-app';

/**
 * Authorization consistency when a role changes mid-session.
 *
 * The scenario from the milestone brief: an administrator demotes somebody who
 * has the console open. Two things have to hold, and only the second is this
 * file's subject.
 *
 * **The backend enforces the new role immediately.** It reads the role from
 * the user row on every request rather than caching it from login (ADR-028),
 * which the server's own integration suite asserts against real PostgreSQL —
 * `test_api_users_integration.py::test_a_demotion_takes_effect_on_the_next_request`.
 * Nothing the browser believes can grant access.
 *
 * **The interface stops offering what the server now refuses.** That is the
 * part tested here, and it is a usability property rather than a security one:
 * without it the console keeps showing administrative controls that answer
 * 403, so the user is told "you do not have permission" by a screen still
 * inviting them to try.
 *
 * The mechanism is a 403 triggering a session refetch. Deliberately not
 * polling — a timer spending requests continuously to catch an event that
 * happens about never — and deliberately not a WebSocket, which for role
 * synchronisation alone is the machinery `docs/ARCHITECTURE.md` §3.3 exists to
 * keep out.
 */

let backend: FakeBackend;

afterEach(() => {
  backend.restore();
});

describe('a role reduced mid-session', () => {
  beforeEach(() => {
    backend = installFakeBackend({
      session: makeUser({ user_id: 'u-admin', username: 'Morgan', role: 'admin' }),
      users: [
        makeManagedUser({ user_id: 'u-admin', username: 'Morgan', role: 'admin' }),
        makeManagedUser({ user_id: 'u-other', username: 'Casey', role: 'operator' }),
      ],
    });
  });

  it('re-asks who the user is after a permission denial', async () => {
    renderApp('/settings/users');
    await waitForAuthToResolve();
    await screen.findByRole('table', { name: /user accounts/i });
    const before = backend.requestsFor('/auth/me').length;

    // The role is reduced server-side, as another administrator would do it.
    backend.state.session = makeUser({
      user_id: 'u-admin',
      username: 'Morgan',
      role: 'viewer',
    });

    // The next administrative request is refused.
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Edit Casey' }));
    // Scoped to the dialog: the list's filter bar has a "Role" control too.
    const dialog = await screen.findByRole('dialog');
    await user.selectOptions(within(dialog).getByLabelText('Role'), 'viewer');
    await user.click(within(dialog).getByRole('button', { name: /Save changes/ }));

    // That denial made the client re-check its own identity.
    await waitFor(() => {
      expect(backend.requestsFor('/auth/me').length).toBeGreaterThan(before);
    });
  });

  it('stops offering administrative sections once the refresh lands', async () => {
    renderApp('/settings/users');
    await waitForAuthToResolve();
    await screen.findByRole('table', { name: /user accounts/i });

    backend.state.session = makeUser({
      user_id: 'u-admin',
      username: 'Morgan',
      role: 'viewer',
    });

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Edit Casey' }));
    const dialog = await screen.findByRole('dialog');
    await user.selectOptions(within(dialog).getByLabelText('Role'), 'viewer');
    await user.click(within(dialog).getByRole('button', { name: /Save changes/ }));

    // The panel becomes the permission explanation, and the section
    // disappears from the settings navigation.
    expect(
      await screen.findByText(/User administration requires the Administrator role/i),
    ).toBeInTheDocument();
    await waitFor(() => {
      const nav = screen.getByRole('navigation', { name: 'Settings sections' });
      expect(nav.textContent).not.toContain('Users');
    });
  });

  it('does not sign the user out for a 403', async () => {
    /*
     * The failure mode this guards against. A 403 means the session is fine
     * and the role is not sufficient; treating it as an expired session would
     * send an authenticated user to the login page and teach them to re-enter
     * their password when nothing is wrong with their credentials.
     */
    renderApp('/settings/users');
    await waitForAuthToResolve();
    await screen.findByRole('table', { name: /user accounts/i });

    backend.state.session = makeUser({
      user_id: 'u-admin',
      username: 'Morgan',
      role: 'viewer',
    });

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Edit Casey' }));
    const dialog = await screen.findByRole('dialog');
    await user.selectOptions(within(dialog).getByLabelText('Role'), 'viewer');
    await user.click(within(dialog).getByRole('button', { name: /Save changes/ }));

    await screen.findByText(/requires the Administrator role/i);
    expect(screen.queryByRole('heading', { name: 'Sign in' })).not.toBeInTheDocument();
    // And still inside the console shell.
    expect(screen.getByRole('main')).toBeInTheDocument();
  });

  it('coalesces a burst of denials into one identity check', async () => {
    // A page with several admin-only queries can produce three 403s in one
    // render pass; three identical refetches would be wasted requests.
    backend.state.auditEvents = [
      makeAuditEvent({ event_id: 'e-1', action: 'user.created' }),
    ];
    renderApp('/settings/audit');
    await waitForAuthToResolve();
    await screen.findByRole('table', { name: /audit events/i });

    const before = backend.requestsFor('/auth/me').length;
    backend.state.routeFailures = {
      '/audit/events': {
        status: 403,
        code: 'insufficient_permissions',
        message: 'You do not have permission to perform this action.',
      },
      '/audit/actions': {
        status: 403,
        code: 'insufficient_permissions',
        message: 'You do not have permission to perform this action.',
      },
    };

    const user = userEvent.setup();
    // One interaction, two denied requests.
    await user.selectOptions(screen.getByLabelText('Filter by action'), 'user.created');

    await waitFor(() => {
      expect(backend.requestsFor('/auth/me').length).toBeGreaterThan(before);
    });
    // At most one extra identity check, not one per denial.
    expect(backend.requestsFor('/auth/me').length).toBeLessThanOrEqual(before + 1);
  });

  it('leaves a 401 to sign the user out, as before', async () => {
    // The other direction: the role recheck must not have swallowed the
    // expired-session path.
    renderApp('/settings/users');
    await waitForAuthToResolve();
    await screen.findByRole('table', { name: /user accounts/i });

    backend.state.session = null;
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Edit Casey' }));
    const dialog = await screen.findByRole('dialog');
    await user.selectOptions(within(dialog).getByLabelText('Role'), 'viewer');
    await user.click(within(dialog).getByRole('button', { name: /Save changes/ }));

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
  });
});
