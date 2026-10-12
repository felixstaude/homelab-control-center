import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  installFakeBackend,
  makeManagedUser,
  makeUser,
  userWithRole,
  type FakeBackend,
} from '@/test/fake-backend';
import { renderApp, waitForAuthToResolve } from '@/test/render-app';

/**
 * User administration, driven through the real API client.
 *
 * Every request in these tests goes through `apiRequest` to a stubbed `fetch`,
 * so the query-string construction, the CSRF header, the error mapping and the
 * cache invalidation are all the shipping code. Mocking the `api/users` module
 * instead would leave each of those untested while the tests still passed.
 */

let backend: FakeBackend;

const ADMIN = makeUser({ user_id: 'u-admin', username: 'Morgan', role: 'admin' });

/** Three accounts, one per role, with the administrator being the caller. */
function seedAccounts() {
  return [
    makeManagedUser({
      user_id: 'u-admin',
      username: 'Morgan',
      role: 'admin',
      last_login_at: '2026-01-02T09:00:00+00:00',
      active_session_count: 1,
    }),
    makeManagedUser({
      user_id: 'u-operator',
      username: 'Casey',
      display_name: 'Casey Jones',
      role: 'operator',
      active_session_count: 2,
    }),
    makeManagedUser({ user_id: 'u-viewer', username: 'Ren', role: 'viewer' }),
  ];
}

afterEach(() => {
  backend.restore();
});

async function renderUsers() {
  renderApp('/settings/users');
  await waitForAuthToResolve();
  // The table replaces the loading rows once the request resolves.
  await screen.findByRole('table', { name: /user accounts/i });
}

describe('user list', () => {
  beforeEach(() => {
    backend = installFakeBackend({ session: ADMIN, users: seedAccounts() });
  });

  it('renders every account with its role, status and session count', async () => {
    await renderUsers();

    const table = screen.getByRole('table', { name: /user accounts/i });
    expect(within(table).getByText('Morgan')).toBeInTheDocument();
    // The display name is the label, with the login name beneath it.
    expect(within(table).getByText('Casey Jones')).toBeInTheDocument();
    expect(within(table).getByText('Casey')).toBeInTheDocument();
    expect(within(table).getByText('Ren')).toBeInTheDocument();

    expect(within(table).getByText('Administrator')).toBeInTheDocument();
    expect(within(table).getByText('Operator')).toBeInTheDocument();
    expect(within(table).getByText('Viewer')).toBeInTheDocument();
    expect(within(table).getAllByText('Active')).toHaveLength(3);
  });

  it('renders a never-signed-in account as a dash, not a date', async () => {
    // The specific lie this avoids: a null `last_login_at` formatted as a
    // timestamp would read as "this account signed in at the epoch".
    await renderUsers();

    const row = screen.getByRole('row', { name: /Ren/ });
    // The `time` element is the last-sign-in cell specifically; the row also
    // renders a dash for "no active sessions", so a bare text query would be
    // ambiguous.
    const lastSignIn = within(row).getByText('—', { selector: 'time' });
    expect(lastSignIn).toBeInTheDocument();
    // And crucially not a formatted epoch, which is what a null rendered
    // through a date formatter looks like.
    expect(within(row).queryByText(/1970/)).not.toBeInTheDocument();
  });

  it('marks the signed-in administrator as themselves', async () => {
    await renderUsers();

    const row = screen.getByRole('row', { name: /Morgan/ });
    expect(within(row).getByText('This is you')).toBeInTheDocument();
  });

  it('gives every row action an accessible name naming the account', async () => {
    // Six identical "Edit" buttons would be unusable with a screen reader.
    await renderUsers();

    expect(screen.getByRole('button', { name: 'Edit Casey Jones' })).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Deactivate Casey Jones' }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Revoke sessions for Casey Jones' }),
    ).toBeInTheDocument();
  });

  it('disables session revocation for an account with no sessions', async () => {
    await renderUsers();

    expect(screen.getByRole('button', { name: 'Revoke sessions for Ren' })).toBeDisabled();
    expect(
      screen.getByRole('button', { name: 'Revoke sessions for Casey Jones' }),
    ).toBeEnabled();
  });

  it('sends the search term to the server rather than filtering locally', async () => {
    // The server owns the folding (NFKC and casefold), so a client-side
    // filter would disagree with it on exactly the inputs that matter.
    const user = userEvent.setup();
    await renderUsers();

    await user.type(screen.getByLabelText('Search'), 'cas');

    await waitFor(() => {
      const requests = backend.requestsFor('/users');
      expect(requests.some((request) => request.query.includes('search=cas'))).toBe(true);
    });
    await waitFor(() => {
      expect(screen.queryByText('Ren')).not.toBeInTheDocument();
    });
    expect(screen.getByText('Casey Jones')).toBeInTheDocument();
  });

  it('sends the role and status filters as query parameters', async () => {
    const user = userEvent.setup();
    await renderUsers();

    await user.selectOptions(screen.getByLabelText('Role'), 'operator');
    await user.selectOptions(screen.getByLabelText('Status'), 'active');

    await waitFor(() => {
      const requests = backend.requestsFor('/users');
      expect(
        requests.some(
          (request) =>
            request.query.includes('role=operator') &&
            request.query.includes('is_active=true'),
        ),
      ).toBe(true);
    });
  });

  it('offers to clear the filters when nothing matches', async () => {
    const user = userEvent.setup();
    await renderUsers();

    await user.type(screen.getByLabelText('Search'), 'nobody-by-that-name');

    expect(await screen.findByText(/No accounts match those filters/i)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Clear filters' }));

    expect(await screen.findByText('Ren')).toBeInTheDocument();
  });

  it('reports the visible range rather than implying stable page numbers', async () => {
    await renderUsers();

    // Offset pagination over a table being written to cannot promise that
    // page 2 holds the same rows twice, so the pager states a range instead
    // of numbered pages. `status` is the pager's own live region.
    const summary = screen.getByRole('status');
    expect(summary).toHaveTextContent(/Showing\s*1.*3.*of\s*3\s*accounts/);
    expect(screen.queryByRole('button', { name: '2' })).not.toBeInTheDocument();
  });

  it('disables both pager buttons on a single page', async () => {
    await renderUsers();

    expect(screen.getByRole('button', { name: /Previous/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: /Next/ })).toBeDisabled();
  });
});

describe('user list pagination', () => {
  beforeEach(() => {
    // Thirty accounts against a page size of twenty-five.
    backend = installFakeBackend({
      session: ADMIN,
      users: Array.from({ length: 30 }, (_, index) =>
        makeManagedUser({
          user_id: `u-${index}`,
          username: `account${String(index).padStart(2, '0')}`,
        }),
      ),
    });
  });

  it('walks to the next page without repeating a row', async () => {
    const user = userEvent.setup();
    await renderUsers();

    const firstPage = screen
      .getAllByRole('row')
      .map((row) => row.textContent)
      .filter((text) => text.includes('account'));

    await user.click(screen.getByRole('button', { name: /Next/ }));

    await waitFor(() => {
      expect(
        backend.requestsFor('/users').some((request) => request.query.includes('offset=25')),
      ).toBe(true);
    });
    await waitFor(() => {
      expect(screen.getByText('account29')).toBeInTheDocument();
    });
    // Nothing from page one is on page two.
    expect(firstPage.some((text) => text.includes('account29'))).toBe(false);
  });
});

describe('user list states', () => {
  it('shows an empty state before any account is created', async () => {
    backend = installFakeBackend({ session: ADMIN, users: [] });
    renderApp('/settings/users');
    await waitForAuthToResolve();

    expect(await screen.findByText('No accounts yet')).toBeInTheDocument();
  });

  it('shows the server message and a retry when the list fails', async () => {
    // One route fails, not the transport: failing everything would break
    // `/auth/me` too and land on the backend-unavailable screen instead of
    // the panel under test.
    backend = installFakeBackend({
      session: ADMIN,
      users: seedAccounts(),
      routeFailures: {
        '/users': { status: 500, code: 'internal_error', message: 'An internal error occurred.' },
      },
    });
    renderApp('/settings/users');
    await waitForAuthToResolve();

    expect(await screen.findByText(/Could not load the accounts/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Try again/ })).toBeInTheDocument();
  });

  it('announces that the table is loading', async () => {
    backend = installFakeBackend({ session: ADMIN, users: seedAccounts() });
    renderApp('/settings/users');

    // Before the request resolves, the skeleton is a labelled live region
    // rather than a silent block of empty cells.
    expect(await screen.findByRole('status', { name: 'Loading' })).toBeInTheDocument();
  });
});

describe('permissions', () => {
  it.each(['viewer', 'operator'] as const)(
    'hides the Users section from a %s',
    async (role) => {
      backend = installFakeBackend({ session: userWithRole(role) });
      renderApp('/settings');
      await waitForAuthToResolve();

      const nav = await screen.findByRole('navigation', { name: 'Settings sections' });
      expect(within(nav).queryByRole('link', { name: /Users/ })).not.toBeInTheDocument();
      expect(within(nav).queryByRole('link', { name: /Audit/ })).not.toBeInTheDocument();
      // The sections they may use are still offered.
      expect(within(nav).getByRole('link', { name: /My account/ })).toBeInTheDocument();
    },
  );

  it('explains the refusal when a viewer navigates directly', async () => {
    // Hiding the link is presentation; the server is the boundary. A viewer
    // who types the URL gets an explanation rather than a blank table or a
    // failed request.
    backend = installFakeBackend({ session: userWithRole('viewer') });
    renderApp('/settings/users');
    await waitForAuthToResolve();

    expect(
      await screen.findByText(/User administration requires the Administrator role/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/You are signed in as Viewer/i)).toBeInTheDocument();
  });

  it('does not request the account list for a viewer', async () => {
    // Not a security property — the server refuses it anyway — but asking for
    // data the interface will not render is a wasted round trip and a 403 in
    // the log that looks like an attack.
    backend = installFakeBackend({ session: userWithRole('viewer') });
    renderApp('/settings/users');
    await waitForAuthToResolve();
    await screen.findByText(/requires the Administrator role/i);

    expect(backend.requestsFor('/users')).toHaveLength(0);
  });

  it('shows the Users section to an administrator', async () => {
    backend = installFakeBackend({ session: ADMIN, users: seedAccounts() });
    renderApp('/settings');
    await waitForAuthToResolve();

    const nav = await screen.findByRole('navigation', { name: 'Settings sections' });
    expect(within(nav).getByRole('link', { name: /Users/ })).toBeInTheDocument();
  });
});

describe('creating an account', () => {
  beforeEach(() => {
    backend = installFakeBackend({ session: ADMIN, users: seedAccounts() });
  });

  async function openCreateDialog() {
    const user = userEvent.setup();
    await renderUsers();
    await user.click(screen.getByRole('button', { name: /New account/ }));
    await screen.findByRole('dialog', { name: /Create a user account/i });
    return user;
  }

  it('creates an account and shows it in the list', async () => {
    const user = await openCreateDialog();
    const dialog = screen.getByRole('dialog');

    await user.type(within(dialog).getByLabelText('Username'), 'newcomer');
    await user.type(
      within(dialog).getByLabelText('Display name (optional)'),
      'New Comer',
    );
    await user.type(within(dialog).getByLabelText('Password'), 'a long enough password');
    await user.type(
      within(dialog).getByLabelText('Confirm password'),
      'a long enough password',
    );
    await user.click(within(dialog).getByRole('button', { name: /Create account/ }));

    expect(await screen.findByText('New Comer')).toBeInTheDocument();
    // And the dialog closed rather than leaving the form open over the list.
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });
  });

  it('defaults the role to viewer, the least privilege', async () => {
    await openCreateDialog();

    const select = within(screen.getByRole('dialog')).getByLabelText<HTMLSelectElement>(
      'Role',
    );
    expect(select.value).toBe('viewer');
  });

  it('sends the chosen role', async () => {
    const user = await openCreateDialog();
    const dialog = screen.getByRole('dialog');

    await user.type(within(dialog).getByLabelText('Username'), 'anoperator');
    await user.type(within(dialog).getByLabelText('Password'), 'a long enough password');
    await user.type(
      within(dialog).getByLabelText('Confirm password'),
      'a long enough password',
    );
    await user.selectOptions(within(dialog).getByLabelText('Role'), 'operator');
    await user.click(within(dialog).getByRole('button', { name: /Create account/ }));

    await waitFor(() => {
      const post = backend.requests.find(
        (request) => request.method === 'POST' && request.path.endsWith('/users'),
      );
      expect(post?.body).toMatchObject({ username: 'anoperator', role: 'operator' });
    });
  });

  it('carries the CSRF header on the create request', async () => {
    const user = await openCreateDialog();
    const dialog = screen.getByRole('dialog');

    await user.type(within(dialog).getByLabelText('Username'), 'csrfcheck');
    await user.type(within(dialog).getByLabelText('Password'), 'a long enough password');
    await user.type(
      within(dialog).getByLabelText('Confirm password'),
      'a long enough password',
    );
    await user.click(within(dialog).getByRole('button', { name: /Create account/ }));

    await waitFor(() => {
      const post = backend.requests.find(
        (request) => request.method === 'POST' && request.path.endsWith('/users'),
      );
      expect(post?.headers['x-csrf-token']).toBeDefined();
    });
  });

  it('refuses to submit when the passwords do not match', async () => {
    const user = await openCreateDialog();
    const dialog = screen.getByRole('dialog');

    await user.type(within(dialog).getByLabelText('Username'), 'mismatch');
    await user.type(within(dialog).getByLabelText('Password'), 'a long enough password');
    await user.type(within(dialog).getByLabelText('Confirm password'), 'something else');
    await user.click(within(dialog).getByRole('button', { name: /Create account/ }));

    expect(
      await within(dialog).findByText('The two passwords do not match.'),
    ).toBeInTheDocument();
    expect(
      backend.requests.filter(
        (request) => request.method === 'POST' && request.path.endsWith('/users'),
      ),
    ).toHaveLength(0);
  });

  it('shows a duplicate username against the field', async () => {
    const user = await openCreateDialog();
    const dialog = screen.getByRole('dialog');

    // A case variant of an existing account: the server folds, so this is the
    // same username.
    await user.type(within(dialog).getByLabelText('Username'), 'MORGAN');
    await user.type(within(dialog).getByLabelText('Password'), 'a long enough password');
    await user.type(
      within(dialog).getByLabelText('Confirm password'),
      'a long enough password',
    );
    await user.click(within(dialog).getByRole('button', { name: /Create account/ }));

    expect(await within(dialog).findByText(/already taken/i)).toBeInTheDocument();
    // The dialog stays open, with the typed username still in it.
    expect(within(dialog).getByLabelText<HTMLInputElement>('Username').value).toBe('MORGAN');
  });

  it('shows the server policy problems for a weak password', async () => {
    const user = await openCreateDialog();
    const dialog = screen.getByRole('dialog');

    await user.type(within(dialog).getByLabelText('Username'), 'weakling');
    await user.type(within(dialog).getByLabelText('Password'), 'short');
    await user.type(within(dialog).getByLabelText('Confirm password'), 'short');
    await user.click(within(dialog).getByRole('button', { name: /Create account/ }));

    expect(await within(dialog).findByText(/does not meet the policy/i)).toBeInTheDocument();
  });

  it('clears the password fields after a failed attempt', async () => {
    const user = await openCreateDialog();
    const dialog = screen.getByRole('dialog');

    await user.type(within(dialog).getByLabelText('Username'), 'MORGAN');
    await user.type(within(dialog).getByLabelText('Password'), 'a long enough password');
    await user.type(
      within(dialog).getByLabelText('Confirm password'),
      'a long enough password',
    );
    await user.click(within(dialog).getByRole('button', { name: /Create account/ }));
    await within(dialog).findByText(/already taken/i);

    expect(within(dialog).getByLabelText<HTMLInputElement>('Password').value).toBe('');
    expect(within(dialog).getByLabelText<HTMLInputElement>('Confirm password').value).toBe('');
  });

  it('does not leave a password in the document after a successful create', async () => {
    const password = 'a-very-distinctive-password-value';
    const user = await openCreateDialog();
    const dialog = screen.getByRole('dialog');

    await user.type(within(dialog).getByLabelText('Username'), 'cleared');
    await user.type(within(dialog).getByLabelText('Password'), password);
    await user.type(within(dialog).getByLabelText('Confirm password'), password);
    await user.click(within(dialog).getByRole('button', { name: /Create account/ }));

    await waitFor(() => {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });
    // `outerHTML` includes attribute values, so a controlled input that
    // serialised its value would be caught here.
    expect(document.body.outerHTML).not.toContain(password);
  });

  it('sends one request even when the button is double-clicked', async () => {
    const user = await openCreateDialog();
    const dialog = screen.getByRole('dialog');

    await user.type(within(dialog).getByLabelText('Username'), 'doubleclick');
    await user.type(within(dialog).getByLabelText('Password'), 'a long enough password');
    await user.type(
      within(dialog).getByLabelText('Confirm password'),
      'a long enough password',
    );
    const submit = within(dialog).getByRole('button', { name: /Create account/ });
    await user.dblClick(submit);

    await waitFor(() => {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });
    expect(
      backend.requests.filter(
        (request) => request.method === 'POST' && request.path.endsWith('/users'),
      ),
    ).toHaveLength(1);
  });

  it('labels every field so the form is usable with a screen reader', async () => {
    await openCreateDialog();
    const dialog = screen.getByRole('dialog');

    for (const label of [
      'Username',
      'Display name (optional)',
      'Password',
      'Confirm password',
      'Role',
    ]) {
      expect(within(dialog).getByLabelText(label)).toBeInTheDocument();
    }
  });

  it('closes on Escape without creating anything', async () => {
    const user = await openCreateDialog();

    await user.keyboard('{Escape}');

    await waitFor(() => {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });
    expect(
      backend.requests.filter(
        (request) => request.method === 'POST' && request.path.endsWith('/users'),
      ),
    ).toHaveLength(0);
  });

  it('discards what was typed when reopened', async () => {
    // Including the password: keeping the form populated across an
    // open/close cycle would leave a secret in component state indefinitely.
    const user = await openCreateDialog();
    await user.type(screen.getByLabelText('Username'), 'abandoned');
    await user.keyboard('{Escape}');
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });

    await user.click(screen.getByRole('button', { name: /New account/ }));
    const dialog = await screen.findByRole('dialog');

    expect(within(dialog).getByLabelText<HTMLInputElement>('Username').value).toBe('');
  });
});

describe('editing an account', () => {
  beforeEach(() => {
    backend = installFakeBackend({ session: ADMIN, users: seedAccounts() });
  });

  it('changes a display name', async () => {
    const user = userEvent.setup();
    await renderUsers();

    await user.click(screen.getByRole('button', { name: 'Edit Casey Jones' }));
    const dialog = await screen.findByRole('dialog', { name: /Edit Casey Jones/i });

    const field = within(dialog).getByLabelText('Display name');
    await user.clear(field);
    await user.type(field, 'Casey Renamed');
    await user.click(within(dialog).getByRole('button', { name: /Save changes/ }));

    expect(await screen.findByText('Casey Renamed')).toBeInTheDocument();
  });

  it('sends only the fields that changed', async () => {
    // A PATCH that always sent everything would record a role-change audit
    // event for a role that did not change.
    const user = userEvent.setup();
    await renderUsers();

    await user.click(screen.getByRole('button', { name: 'Edit Casey Jones' }));
    const dialog = await screen.findByRole('dialog');
    await user.selectOptions(within(dialog).getByLabelText('Role'), 'admin');
    await user.click(within(dialog).getByRole('button', { name: /Save changes/ }));

    await waitFor(() => {
      const patch = backend.requests.find((request) => request.method === 'PATCH');
      expect(patch?.body).toEqual({ role: 'admin' });
    });
  });

  it('shows the immutable username without offering to change it', async () => {
    const user = userEvent.setup();
    await renderUsers();

    await user.click(screen.getByRole('button', { name: 'Edit Casey Jones' }));
    const dialog = await screen.findByRole('dialog');

    expect(within(dialog).getByText(/cannot be changed/i)).toBeInTheDocument();
    expect(within(dialog).queryByLabelText('Username')).not.toBeInTheDocument();
  });

  it('warns that a role change alters what the account may do', async () => {
    const user = userEvent.setup();
    await renderUsers();

    await user.click(screen.getByRole('button', { name: 'Edit Ren' }));
    const dialog = await screen.findByRole('dialog');
    await user.selectOptions(within(dialog).getByLabelText('Role'), 'admin');

    expect(
      within(dialog).getByText(/Administrators can manage every account/i),
    ).toBeInTheDocument();
  });

  it('disables saving until something changes', async () => {
    const user = userEvent.setup();
    await renderUsers();

    await user.click(screen.getByRole('button', { name: 'Edit Casey Jones' }));
    const dialog = await screen.findByRole('dialog');

    expect(within(dialog).getByRole('button', { name: /Save changes/ })).toBeDisabled();
  });

  it('explains why the only administrator cannot be demoted', async () => {
    // The refusal the server sends with its own code, so the interface can
    // say what to do instead of showing a generic conflict.
    backend.state.users = [
      makeManagedUser({ user_id: 'u-admin', username: 'Morgan', role: 'admin' }),
    ];
    const user = userEvent.setup();
    await renderUsers();

    await user.click(screen.getByRole('button', { name: 'Edit Morgan' }));
    const dialog = await screen.findByRole('dialog');
    await user.selectOptions(within(dialog).getByLabelText('Role'), 'viewer');
    await user.click(within(dialog).getByRole('button', { name: /Save changes/ }));

    expect(
      await within(dialog).findByText(/only active administrator/i),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByText(/Grant the Administrator role to another active account/i),
    ).toBeInTheDocument();
  });
});

describe('deactivation and reactivation', () => {
  beforeEach(() => {
    backend = installFakeBackend({ session: ADMIN, users: seedAccounts() });
  });

  it('confirms before deactivating, and says what will happen', async () => {
    const user = userEvent.setup();
    await renderUsers();

    await user.click(screen.getByRole('button', { name: 'Deactivate Casey Jones' }));
    const dialog = await screen.findByRole('dialog', { name: /Deactivate Casey Jones/i });

    expect(within(dialog).getByText(/Access ends immediately/i)).toBeInTheDocument();
    expect(within(dialog).getByText(/Nothing is deleted/i)).toBeInTheDocument();
    // Nothing has happened yet.
    expect(backend.requests.filter((request) => request.method === 'POST')).toHaveLength(0);
  });

  it('deactivates on confirmation and updates the row', async () => {
    const user = userEvent.setup();
    await renderUsers();

    await user.click(screen.getByRole('button', { name: 'Deactivate Casey Jones' }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'Deactivate account' }));

    await waitFor(() => {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });
    const row = await screen.findByRole('row', { name: /Casey Jones/ });
    expect(within(row).getByText('Inactive')).toBeInTheDocument();
  });

  it('cancels without acting', async () => {
    const user = userEvent.setup();
    await renderUsers();

    await user.click(screen.getByRole('button', { name: 'Deactivate Casey Jones' }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }));

    await waitFor(() => {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });
    expect(backend.requests.filter((request) => request.method === 'POST')).toHaveLength(0);
    const row = screen.getByRole('row', { name: /Casey Jones/ });
    expect(within(row).getByText('Active')).toBeInTheDocument();
  });

  it('offers reactivation for an inactive account and states that old sessions stay revoked', async () => {
    backend.state.users = [
      makeManagedUser({ user_id: 'u-admin', username: 'Morgan', role: 'admin' }),
      makeManagedUser({ user_id: 'u-gone', username: 'Dormant', is_active: false }),
    ];
    const user = userEvent.setup();
    await renderUsers();

    await user.click(screen.getByRole('button', { name: 'Reactivate Dormant' }));
    const dialog = await screen.findByRole('dialog', { name: /Reactivate Dormant/i });

    expect(within(dialog).getByText(/Old sessions stay revoked/i)).toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: 'Reactivate account' }));

    const row = await screen.findByRole('row', { name: /Dormant/ });
    await waitFor(() => {
      expect(within(row).getByText('Active')).toBeInTheDocument();
    });
  });

  it('explains the refusal when the only administrator would be deactivated', async () => {
    backend.state.users = [
      makeManagedUser({ user_id: 'u-admin', username: 'Morgan', role: 'admin' }),
    ];
    const user = userEvent.setup();
    await renderUsers();

    await user.click(screen.getByRole('button', { name: 'Deactivate Morgan' }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'Deactivate account' }));

    // The alert's heading and the server's message both contain the phrase, so
    // the assertion is on the remedy — which is the part an operator needs.
    expect(
      await within(dialog).findByText(/Grant the Administrator role to another active account/i),
    ).toBeInTheDocument();
  });
});

describe('revoking a user’s sessions', () => {
  beforeEach(() => {
    backend = installFakeBackend({ session: ADMIN, users: seedAccounts() });
  });

  it('explains that the sessions will be invalidated', async () => {
    const user = userEvent.setup();
    await renderUsers();

    await user.click(screen.getByRole('button', { name: 'Revoke sessions for Casey Jones' }));
    const dialog = await screen.findByRole('dialog', { name: /Revoke sessions/i });

    expect(
      within(dialog).getByText(/They will be signed out immediately/i),
    ).toBeInTheDocument();
    // And the distinction from deactivation is stated, because the two
    // buttons sit next to each other.
    expect(within(dialog).getByText(/can sign in again straight away/i)).toBeInTheDocument();
  });

  it('reports how many sessions were actually revoked', async () => {
    const user = userEvent.setup();
    await renderUsers();

    await user.click(screen.getByRole('button', { name: 'Revoke sessions for Casey Jones' }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'Revoke sessions' }));

    expect(await screen.findByText(/Revoked 2 sessions for Casey Jones/i)).toBeInTheDocument();
  });

  it('does not report success when a revocation fails', async () => {
    // The specific lie this avoids: a failed revocation presented as done,
    // leaving an operator believing a possibly-stolen session is dead.
    backend.state.nextFailure = {
      status: 500,
      code: 'internal_error',
      message: 'An internal error occurred.',
    };
    const user = userEvent.setup();
    await renderUsers();

    await user.click(screen.getByRole('button', { name: 'Revoke sessions for Casey Jones' }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'Revoke sessions' }));

    expect(await within(dialog).findByText(/internal error/i)).toBeInTheDocument();
    expect(screen.queryByText(/Revoked \d+ session/i)).not.toBeInTheDocument();
    // The dialog stays open, so the operator can see it did not work.
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('invalidates the list so a stale session count cannot persist', async () => {
    const user = userEvent.setup();
    await renderUsers();
    const before = backend.requestsFor('/users').length;

    await user.click(screen.getByRole('button', { name: 'Revoke sessions for Casey Jones' }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'Revoke sessions' }));

    await waitFor(() => {
      expect(backend.requestsFor('/users').length).toBeGreaterThan(before);
    });
    const row = await screen.findByRole('row', { name: /Casey Jones/ });
    await waitFor(() => {
      expect(within(row).getByTitle('No active sessions')).toBeInTheDocument();
    });
  });
});
