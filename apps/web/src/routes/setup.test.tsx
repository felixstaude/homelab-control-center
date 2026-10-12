/**
 * First-run setup experience.
 */

import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';

import { installFakeBackend, makeUser, type FakeBackend } from '@/test/fake-backend';
import { renderApp, waitForAuthToResolve } from '@/test/render-app';

let backend: FakeBackend;

afterEach(() => {
  backend.restore();
});

describe('setup required', () => {
  it('redirects to setup from a protected route when uninitialized', async () => {
    backend = installFakeBackend({ initialized: false, session: null });

    renderApp('/');
    await waitForAuthToResolve();

    expect(
      screen.getByRole('heading', { name: /Finish setting up this installation/ }),
    ).toBeInTheDocument();
  });

  it('redirects to setup from the login page when uninitialized', async () => {
    // Offering a sign-in form for an installation with no accounts would be a
    // dead end.
    backend = installFakeBackend({ initialized: false, session: null });

    renderApp('/login');
    await waitForAuthToResolve();

    expect(
      screen.getByRole('heading', { name: /Finish setting up this installation/ }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText('Password')).toBeNull();
  });

  it('shows the supported command for both deployment shapes', async () => {
    backend = installFakeBackend({ initialized: false, session: null });

    renderApp('/setup');
    await waitForAuthToResolve();

    expect(screen.getByText(/hcc bootstrap-admin/)).toBeInTheDocument();
    expect(screen.getByText('make bootstrap')).toBeInTheDocument();
    expect(screen.getByText('Container deployment')).toBeInTheDocument();
  });

  it('explains why there is no form on the page', async () => {
    // The page has to carry this reasoning, or the absence of a form reads as
    // an unfinished feature rather than a decision.
    backend = installFakeBackend({ initialized: false, session: null });

    renderApp('/setup');
    await waitForAuthToResolve();

    expect(screen.getByText(/Why not a form on this page\?/)).toBeInTheDocument();
    expect(screen.getByText(/claim the installation/)).toBeInTheDocument();
  });

  it('offers no way to create an administrator from the browser', async () => {
    // The security property: this page must not grow a weaker alternative to
    // the local command.
    backend = installFakeBackend({ initialized: false, session: null });

    renderApp('/setup');
    await waitForAuthToResolve();

    expect(screen.queryByLabelText(/password/i)).toBeNull();
    expect(screen.queryByLabelText(/username/i)).toBeNull();
    expect(screen.queryByRole('textbox')).toBeNull();
    // And nothing was posted anywhere.
    expect(backend.requests.filter((request) => request.method !== 'GET')).toHaveLength(0);
  });

  it('asks for no secret and displays none', async () => {
    backend = installFakeBackend({ initialized: false, session: null });

    renderApp('/setup');
    await waitForAuthToResolve();

    const text = document.body.textContent;
    // No bootstrap token, no password, nothing to paste in.
    expect(text).not.toMatch(/token/i);
    expect(text).not.toMatch(/paste/i);
    expect(text).toMatch(/never accepts one as an argument/);
  });
});

describe('re-checking the status', () => {
  it('moves on once the administrator has been created', async () => {
    backend = installFakeBackend({ initialized: false, session: null });

    renderApp('/setup');
    await waitForAuthToResolve();

    // The operator runs the command in a terminal…
    backend.state.initialized = true;
    // …and comes back to the page.
    await userEvent
      .setup()
      .click(screen.getByRole('button', { name: /I have created the administrator/ }));

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Setup complete' })).toBeInTheDocument();
    });
    expect(screen.getByRole('link', { name: /Continue to sign in/ })).toBeInTheDocument();
  });

  it('requires no reload to notice', async () => {
    backend = installFakeBackend({ initialized: false, session: null });

    renderApp('/setup');
    await waitForAuthToResolve();
    const before = backend.requestsFor('/setup/status').length;

    backend.state.initialized = true;
    await userEvent
      .setup()
      .click(screen.getByRole('button', { name: /I have created the administrator/ }));

    await waitFor(() => {
      expect(backend.requestsFor('/setup/status').length).toBeGreaterThan(before);
    });
    expect(screen.getByRole('heading', { name: 'Setup complete' })).toBeInTheDocument();
  });

  it('stays on the instructions when setup is still incomplete', async () => {
    backend = installFakeBackend({ initialized: false, session: null });

    renderApp('/setup');
    await waitForAuthToResolve();

    await userEvent
      .setup()
      .click(screen.getByRole('button', { name: /I have created the administrator/ }));

    await waitFor(() => {
      expect(backend.requestsFor('/setup/status').length).toBeGreaterThan(1);
    });
    expect(
      screen.getByRole('heading', { name: /Finish setting up this installation/ }),
    ).toBeInTheDocument();
  });

  it('handles setup completing in another browser', async () => {
    // Someone else — or the same operator in another tab — finishes setup.
    backend = installFakeBackend({ initialized: false, session: null });

    renderApp('/setup');
    await waitForAuthToResolve();

    backend.state.initialized = true;
    backend.state.session = makeUser();
    await userEvent
      .setup()
      .click(screen.getByRole('button', { name: /I have created the administrator/ }));

    // Signed in elsewhere as well, so this tab goes straight to the console
    // rather than offering setup instructions for a completed installation.
    await waitFor(() => {
      expect(screen.getByRole('navigation', { name: 'Primary' })).toBeInTheDocument();
    });
  });
});

describe('after initialization', () => {
  it('sends a late visitor from /setup to the login page', async () => {
    // Instructions for a command that would now refuse to run are worse than
    // no instructions.
    backend = installFakeBackend({ initialized: true, session: null });

    renderApp('/setup');
    await waitForAuthToResolve();

    expect(screen.getByRole('heading', { name: 'Setup complete' })).toBeInTheDocument();
  });

  it('sends an authenticated visitor from /setup to the dashboard', async () => {
    backend = installFakeBackend({ initialized: true, session: makeUser() });

    renderApp('/setup');
    await waitForAuthToResolve();

    expect(screen.getByRole('navigation', { name: 'Primary' })).toBeInTheDocument();
  });
});

describe('status unavailable', () => {
  it('reports the control plane as unreachable rather than uninitialized', async () => {
    // A generous test timeout: this path deliberately waits out the query
    // layer's two retries with backoff (~3s), because that is what a real
    // client does before declaring the backend unreachable.
    // A failed status check must not be read as "setup required": telling an
    // operator to bootstrap an installation that is already set up would make
    // them run a command that refuses, and look like data loss.
    backend = installFakeBackend({ transport: 'network', initialized: false });

    renderApp('/setup');

    await waitFor(
      () => {
        expect(screen.getByText('Control plane unreachable')).toBeInTheDocument();
      },
      { timeout: 10_000 },
    );
    expect(screen.queryByText(/Finish setting up/)).toBeNull();
  }, 20_000);

  it('recovers via retry once the backend answers', async () => {
    backend = installFakeBackend({ transport: 'network', initialized: false });

    renderApp('/setup');
    await waitFor(
      () => {
        expect(screen.getByText('Control plane unreachable')).toBeInTheDocument();
      },
      { timeout: 10_000 },
    );

    backend.state.transport = 'ok';
    await userEvent.setup().click(screen.getByRole('button', { name: /Retry/ }));

    await waitFor(
      () => {
        expect(screen.getByText(/Finish setting up this installation/)).toBeInTheDocument();
      },
      { timeout: 10_000 },
    );
  }, 20_000);
});
