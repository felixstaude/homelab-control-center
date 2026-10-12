import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  installFakeBackend,
  makeOwnSession,
  userWithRole,
  type FakeBackend,
} from '@/test/fake-backend';
import { renderApp, waitForAuthToResolve } from '@/test/render-app';

/**
 * Own-session management.
 *
 * The assertions that matter most here are negative: that no session token
 * appears, that no browser or operating-system label is invented from a
 * `User-Agent`, and that no location is shown. Each of those would be the
 * interface asserting something it does not know.
 */

let backend: FakeBackend;

const CURRENT = makeOwnSession({
  session_id: 's-current',
  is_current: true,
  created_ip: '192.0.2.10',
  user_agent: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
});

const OTHER = makeOwnSession({
  session_id: 's-other',
  is_current: false,
  created_at: '2026-01-01T07:00:00+00:00',
  created_ip: '198.51.100.4',
  user_agent: null,
});

afterEach(() => {
  backend.restore();
});

async function renderSessions() {
  renderApp('/settings/sessions');
  await waitForAuthToResolve();
  await screen.findByText('Active sessions');
}

describe('session list', () => {
  beforeEach(() => {
    backend = installFakeBackend({
      session: userWithRole('viewer'),
      ownSessions: [CURRENT, OTHER],
    });
  });

  it('is reachable by every role', async () => {
    await renderSessions();

    expect(screen.getByText('This session')).toBeInTheDocument();
  });

  it('marks exactly one session as the current one', async () => {
    await renderSessions();

    expect(screen.getAllByText('This session')).toHaveLength(1);
    expect(screen.getAllByText('Other session')).toHaveLength(1);
  });

  it('shows creation, activity and expiry for each session', async () => {
    await renderSessions();

    expect(screen.getAllByText('Signed in')).toHaveLength(2);
    expect(screen.getAllByText('Last used')).toHaveLength(2);
    expect(screen.getAllByText('Expires')).toHaveLength(2);
  });

  it('shows the recorded address', async () => {
    await renderSessions();

    expect(screen.getByText('192.0.2.10')).toBeInTheDocument();
    expect(screen.getByText('198.51.100.4')).toBeInTheDocument();
  });

  it('renders the user agent verbatim rather than inferring a browser', async () => {
    /*
     * A "Chrome on Linux" label would be a guess presented as a fact, and it
     * would be wrong exactly for the clients worth noticing. The raw string is
     * honest; the test pins that choice so a later "improvement" that parses
     * it has to argue with this.
     */
    await renderSessions();

    expect(
      screen.getByText('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Chrome/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Firefox/i)).not.toBeInTheDocument();
  });

  it('says a user agent was not recorded rather than inventing one', async () => {
    await renderSessions();

    expect(screen.getByText('User agent not recorded')).toBeInTheDocument();
  });

  it('invents no geographical location', async () => {
    await renderSessions();

    // The server stores an address and nothing else. A city would mean either
    // a third-party lookup (ADR-009 forbids the request) or a fabrication.
    expect(screen.queryByText(/location/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/country/i)).not.toBeInTheDocument();
  });

  it('shows no session token anywhere', async () => {
    await renderSessions();

    // The token is an HttpOnly cookie the client cannot read, and the id is a
    // separate primary key. Neither a token nor a hash should be renderable.
    expect(document.body.textContent).not.toContain('token');
  });

  it('explains that revocation is server-side', async () => {
    await renderSessions();

    expect(screen.getByText(/held on the server/i)).toBeInTheDocument();
  });

  it('announces that the list is loading', async () => {
    renderApp('/settings/sessions');

    expect(await screen.findByRole('status', { name: 'Loading' })).toBeInTheDocument();
  });
});

describe('revoking another session', () => {
  beforeEach(() => {
    backend = installFakeBackend({
      session: userWithRole('viewer'),
      ownSessions: [CURRENT, OTHER],
    });
  });

  it('confirms first, naming when that session signed in', async () => {
    const user = userEvent.setup();
    await renderSessions();

    await user.click(screen.getByRole('button', { name: /Revoke the session signed in/ }));
    const dialog = await screen.findByRole('dialog', { name: /Revoke this session/i });

    expect(within(dialog).getByText(/That session ends immediately/i)).toBeInTheDocument();
    expect(within(dialog).getByText(/This one is unaffected/i)).toBeInTheDocument();
    expect(backend.requests.filter((request) => request.method === 'POST')).toHaveLength(0);
  });

  it('removes it from the list after confirming', async () => {
    const user = userEvent.setup();
    await renderSessions();

    await user.click(screen.getByRole('button', { name: /Revoke the session signed in/ }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'Revoke session' }));

    await waitFor(() => {
      expect(screen.queryByText('Other session')).not.toBeInTheDocument();
    });
    expect(screen.getByText('This session')).toBeInTheDocument();
  });

  it('carries the CSRF header', async () => {
    const user = userEvent.setup();
    await renderSessions();

    await user.click(screen.getByRole('button', { name: /Revoke the session signed in/ }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'Revoke session' }));

    await waitFor(() => {
      const post = backend.requests.find((request) => request.method === 'POST');
      expect(post?.headers['x-csrf-token']).toBeDefined();
    });
  });

  it('surfaces a failure rather than removing the row anyway', async () => {
    // No optimistic update: a row removed and then restored would have told
    // the user a session was dead when it is live.
    backend.state.nextFailure = {
      status: 500,
      code: 'internal_error',
      message: 'An internal error occurred.',
    };
    const user = userEvent.setup();
    await renderSessions();

    await user.click(screen.getByRole('button', { name: /Revoke the session signed in/ }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'Revoke session' }));

    expect(await within(dialog).findByText(/internal error/i)).toBeInTheDocument();
    // Still listed, because it still exists.
    expect(screen.getByText('Other session')).toBeInTheDocument();
  });
});

describe('revoking the current session', () => {
  beforeEach(() => {
    backend = installFakeBackend({
      session: userWithRole('viewer'),
      ownSessions: [CURRENT],
    });
  });

  it('warns that the user will be signed out', async () => {
    const user = userEvent.setup();
    await renderSessions();

    await user.click(screen.getByRole('button', { name: 'Sign out of this session' }));
    const dialog = await screen.findByRole('dialog', { name: /Sign out of this session/i });

    expect(within(dialog).getByText(/You will be signed out/i)).toBeInTheDocument();
    expect(within(dialog).getByText(/need your password to come back/i)).toBeInTheDocument();
  });
});

describe('signing out elsewhere', () => {
  beforeEach(() => {
    backend = installFakeBackend({
      session: userWithRole('viewer'),
      ownSessions: [CURRENT, OTHER],
    });
  });

  it('is disabled when there is nowhere else', async () => {
    backend.state.ownSessions = [CURRENT];
    await renderSessions();

    expect(screen.getByRole('button', { name: /Sign out elsewhere/ })).toBeDisabled();
  });

  it('ends the other sessions and keeps this one', async () => {
    const user = userEvent.setup();
    await renderSessions();

    await user.click(screen.getByRole('button', { name: /Sign out elsewhere/ }));
    const dialog = await screen.findByRole('dialog', { name: /Sign out everywhere else/i });
    expect(within(dialog).getByText(/Those sessions end immediately/i)).toBeInTheDocument();

    await user.click(within(dialog).getByRole('button', { name: 'Sign out elsewhere' }));

    await waitFor(() => {
      expect(screen.queryByText('Other session')).not.toBeInTheDocument();
    });
    expect(screen.getByText('This session')).toBeInTheDocument();
  });

  it('points at the password change for a possibly-compromised credential', async () => {
    // Revoking sessions does not rotate the password, and saying so is the
    // difference between the user doing the right thing and believing they
    // already have.
    const user = userEvent.setup();
    await renderSessions();

    await user.click(screen.getByRole('button', { name: /Sign out elsewhere/ }));
    const dialog = await screen.findByRole('dialog');

    expect(within(dialog).getByText(/Your password is unchanged/i)).toBeInTheDocument();
  });

  it('closes on Escape without revoking anything', async () => {
    const user = userEvent.setup();
    await renderSessions();

    await user.click(screen.getByRole('button', { name: /Sign out elsewhere/ }));
    await screen.findByRole('dialog');
    await user.keyboard('{Escape}');

    await waitFor(() => {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });
    expect(backend.requests.filter((request) => request.method === 'POST')).toHaveLength(0);
    expect(screen.getByText('Other session')).toBeInTheDocument();
  });
});

describe('session list failure', () => {
  it('shows the error and a retry', async () => {
    backend = installFakeBackend({
      session: userWithRole('viewer'),
      routeFailures: {
        '/account/sessions': {
          status: 500,
          code: 'internal_error',
          message: 'An internal error occurred.',
        },
      },
    });
    renderApp('/settings/sessions');
    await waitForAuthToResolve();

    expect(await screen.findByText(/Could not load your sessions/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Try again/ })).toBeInTheDocument();
  });
});
