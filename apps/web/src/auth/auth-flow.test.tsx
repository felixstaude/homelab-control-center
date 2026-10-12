/**
 * Authentication flows, end to end through the real app.
 *
 * The whole `App` is mounted against the fake control plane, so these
 * exercise the route guards, the state machine, the API client's header
 * assembly and the pages together — the way a browser does.
 */

import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { focusManager } from '@tanstack/react-query';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  TEST_CSRF_TOKEN,
  installFakeBackend,
  makeUser,
  type FakeBackend,
} from '@/test/fake-backend';
import { renderApp, waitForAuthToResolve } from '@/test/render-app';

let backend: FakeBackend;

afterEach(() => {
  backend.restore();
  vi.useRealTimers();
});

/**
 * Open the account menu.
 *
 * Selected by accessible name rather than by `aria-expanded`: the mobile
 * navigation trigger also carries that attribute, and the name is what a user
 * actually perceives.
 */
async function openAccountMenu(
  user: ReturnType<typeof userEvent.setup>,
  username = 'FelixAdmin',
) {
  await user.click(screen.getByRole('button', { name: new RegExp(username) }));
}

/** Fill the form and submit it. */
async function signIn(username = 'FelixAdmin', password = 'correct horse battery staple') {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText('Username'), username);
  await user.type(screen.getByLabelText('Password'), password);
  await user.click(screen.getByRole('button', { name: 'Sign in' }));
  return user;
}

// ---------------------------------------------------------------------------
// Session restoration
// ---------------------------------------------------------------------------

describe('session restoration', () => {
  it('restores the authenticated UI from an existing session cookie', async () => {
    // The browser-refresh case: no login, just a cookie the server honours.
    backend = installFakeBackend({ session: makeUser() });

    renderApp('/');
    await waitForAuthToResolve();

    expect(screen.getByRole('navigation', { name: 'Primary' })).toBeInTheDocument();
    expect(screen.getByText('FelixAdmin')).toBeInTheDocument();
  });

  it('shows the login page when there is no session', async () => {
    backend = installFakeBackend({ session: null });

    renderApp('/');
    await waitForAuthToResolve();

    expect(screen.getByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
  });

  it('asks the server exactly once per mount', async () => {
    // A duplicated session lookup on every mount would double the cost of
    // every page load.
    backend = installFakeBackend({ session: makeUser() });

    renderApp('/');
    await waitForAuthToResolve();

    expect(backend.requestsFor('/auth/me')).toHaveLength(1);
  });

  it('resolves setup status and the session in parallel, not in sequence', async () => {
    backend = installFakeBackend({ session: makeUser() });

    renderApp('/');
    await waitForAuthToResolve();

    // Both requests are issued before either has to resolve, so a cold load
    // costs one round trip rather than two.
    const paths = backend.requests.slice(0, 2).map((request) => request.path);
    expect(paths.some((path) => path.endsWith('/setup/status'))).toBe(true);
    expect(paths.some((path) => path.endsWith('/auth/me'))).toBe(true);
  });

  it('renders no protected content while the session is resolving', async () => {
    // The flash this guards against: a protected page appearing for a frame
    // because "not yet known" was treated as "not authenticated".
    backend = installFakeBackend({ session: makeUser() });

    renderApp('/');

    // Before resolution: the busy state, and nothing from the dashboard.
    expect(screen.getByRole('status')).toHaveTextContent('Checking your session…');
    expect(screen.queryByRole('navigation', { name: 'Primary' })).toBeNull();
    expect(screen.queryByRole('heading', { name: 'Sign in' })).toBeNull();

    await waitForAuthToResolve();
    expect(screen.getByRole('navigation', { name: 'Primary' })).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Signing in
// ---------------------------------------------------------------------------

describe('signing in', () => {
  it('authenticates and lands on the dashboard', async () => {
    backend = installFakeBackend({ session: null });

    renderApp('/login');
    await waitForAuthToResolve();
    await signIn();

    await waitFor(() => {
      expect(screen.getByRole('navigation', { name: 'Primary' })).toBeInTheDocument();
    });
    const login = backend.requestsFor('/auth/login')[0];
    expect(login?.method).toBe('POST');
    expect(login?.body).toEqual({
      username: 'FelixAdmin',
      password: 'correct horse battery staple',
    });
  });

  it('shows the server message for invalid credentials', async () => {
    backend = installFakeBackend({
      session: null,
      loginFailure: {
        status: 401,
        code: 'invalid_credentials',
        message: 'Invalid username or password.',
      },
    });

    renderApp('/login');
    await waitForAuthToResolve();
    await signIn('nobody', 'wrong password');

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('Invalid username or password.');
    });
    // Still on the login page, and no session was established.
    expect(screen.getByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
  });

  it('gives an unknown username the same message as a wrong password', async () => {
    // The backend returns one response for every cause; the client must not
    // add a distinction the server deliberately withheld.
    backend = installFakeBackend({
      session: null,
      loginFailure: {
        status: 401,
        code: 'invalid_credentials',
        message: 'Invalid username or password.',
      },
    });

    renderApp('/login');
    await waitForAuthToResolve();
    await signIn('definitely-not-a-user', 'whatever');

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('Invalid username or password.');
    });
    expect(screen.getByRole('alert').textContent).not.toMatch(/unknown|no such|not found/i);
  });

  it('reports a disabled account without saying so', async () => {
    // The backend answers an inactive account identically; this asserts the
    // client does not infer or embellish.
    backend = installFakeBackend({
      session: null,
      loginFailure: {
        status: 401,
        code: 'invalid_credentials',
        message: 'Invalid username or password.',
      },
    });

    renderApp('/login');
    await waitForAuthToResolve();
    await signIn('DisabledUser', 'correct horse battery staple');

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('Invalid username or password.');
    });
    expect(screen.getByRole('alert').textContent).not.toMatch(/disabled|inactive|locked/i);
  });

  it('shows a retry hint when throttled, and does not retry by itself', async () => {
    backend = installFakeBackend({
      session: null,
      loginFailure: {
        status: 429,
        code: 'rate_limited',
        message: 'Too many attempts. Please try again later.',
        retryAfter: 90,
      },
    });

    renderApp('/login');
    await waitForAuthToResolve();
    await signIn();

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('Too many attempts');
    });
    expect(screen.getByRole('alert')).toHaveTextContent('90 seconds');
    // Exactly one attempt: an automatic retry would spend another from the
    // budget the server just said was exhausted.
    expect(backend.requestsFor('/auth/login')).toHaveLength(1);
  });

  it('refuses to submit an empty form, without calling the API', async () => {
    backend = installFakeBackend({ session: null });

    renderApp('/login');
    await waitForAuthToResolve();
    await userEvent.setup().click(screen.getByRole('button', { name: 'Sign in' }));

    expect(screen.getByText('Enter your username.')).toBeInTheDocument();
    expect(screen.getByText('Enter your password.')).toBeInTheDocument();
    expect(backend.requestsFor('/auth/login')).toHaveLength(0);
  });

  it('puts no credentials in the request URL', async () => {
    backend = installFakeBackend({ session: null });

    renderApp('/login');
    await waitForAuthToResolve();
    await signIn('FelixAdmin', 'correct horse battery staple');

    await waitFor(() => {
      expect(backend.requestsFor('/auth/login')).toHaveLength(1);
    });
    for (const request of backend.requests) {
      expect(request.path).not.toContain('correct');
      expect(request.path).not.toContain('FelixAdmin');
    }
  });

  it('stores nothing in browser storage', async () => {
    backend = installFakeBackend({ session: null });

    renderApp('/login');
    await waitForAuthToResolve();
    await signIn();

    await waitFor(() => {
      expect(screen.getByRole('navigation', { name: 'Primary' })).toBeInTheDocument();
    });
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });

  it('submits on Enter from the password field', async () => {
    backend = installFakeBackend({ session: null });

    renderApp('/login');
    await waitForAuthToResolve();

    const user = userEvent.setup();
    await user.type(screen.getByLabelText('Username'), 'FelixAdmin');
    await user.type(screen.getByLabelText('Password'), 'correct horse battery staple{Enter}');

    await waitFor(() => {
      expect(backend.requestsFor('/auth/login')).toHaveLength(1);
    });
  });
});

// ---------------------------------------------------------------------------
// Signing out
// ---------------------------------------------------------------------------

describe('signing out', () => {
  it('revokes the session server-side and returns to login', async () => {
    backend = installFakeBackend({ session: makeUser() });

    renderApp('/');
    await waitForAuthToResolve();

    const user = userEvent.setup();
    await openAccountMenu(user);
    await user.click(screen.getByRole('button', { name: 'Sign out' }));

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
    });
    const logout = backend.requestsFor('/auth/logout')[0];
    expect(logout?.method).toBe('POST');
    expect(backend.state.session).toBeNull();
  });

  it('sends the CSRF token on logout', async () => {
    // Logout is a state-changing authenticated request, so the backend
    // requires the session's token in the header. The fake refuses without it.
    backend = installFakeBackend({ session: makeUser() });
    document.cookie = `hcc_csrf=${TEST_CSRF_TOKEN}; path=/`;

    renderApp('/');
    await waitForAuthToResolve();

    const user = userEvent.setup();
    await openAccountMenu(user);
    await user.click(screen.getByRole('button', { name: 'Sign out' }));

    await waitFor(() => {
      expect(backend.requestsFor('/auth/logout')).toHaveLength(1);
    });
    expect(backend.requestsFor('/auth/logout')[0]?.headers['x-csrf-token']).toBe(
      TEST_CSRF_TOKEN,
    );
  });

  it('confirms the sign-out on the login page', async () => {
    backend = installFakeBackend({ session: makeUser() });
    document.cookie = `hcc_csrf=${TEST_CSRF_TOKEN}; path=/`;

    renderApp('/');
    await waitForAuthToResolve();

    const user = userEvent.setup();
    await openAccountMenu(user);
    await user.click(screen.getByRole('button', { name: 'Sign out' }));

    await waitFor(() => {
      expect(screen.getByText('You are signed out')).toBeInTheDocument();
    });
  });

  it('reports a failed revocation instead of claiming success', async () => {
    // The single most important thing logout must not do: say the session was
    // revoked when the server never revoked it.
    backend = installFakeBackend({ session: makeUser(), logoutFails: true });
    document.cookie = `hcc_csrf=${TEST_CSRF_TOKEN}; path=/`;

    renderApp('/');
    await waitForAuthToResolve();

    const user = userEvent.setup();
    await openAccountMenu(user);
    await user.click(screen.getByRole('button', { name: 'Sign out' }));

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(/internal error/i);
    });
    // The server still holds the session, which is exactly why the failure is
    // surfaced rather than swallowed.
    expect(backend.state.session).not.toBeNull();
  });

  it('leaves no authenticated data visible afterwards', async () => {
    backend = installFakeBackend({ session: makeUser({ username: 'SecretAdmin' }) });
    document.cookie = `hcc_csrf=${TEST_CSRF_TOKEN}; path=/`;

    renderApp('/');
    await waitForAuthToResolve();
    expect(screen.getByText('SecretAdmin')).toBeInTheDocument();

    const user = userEvent.setup();
    await openAccountMenu(user, 'SecretAdmin');
    await user.click(screen.getByRole('button', { name: 'Sign out' }));

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
    });
    expect(screen.queryByText('SecretAdmin')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Session expiry
// ---------------------------------------------------------------------------

/**
 * Make the client notice that its session is gone.
 *
 * This models the real sequence rather than poking the cache: the tab sits in
 * the background long enough for the cached session to go stale, then the user
 * returns to it and the query refetches.
 *
 * Both halves are needed. `refetchOnWindowFocus` only refetches a **stale**
 * query, so focus alone changes nothing inside the 30-second stale window —
 * which is correct behaviour, and was why an earlier version of this test
 * waited three seconds and then failed.
 */
async function expireSessionInBackground(): Promise<void> {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  await vi.advanceTimersByTimeAsync(31_000);
  focusManager.setFocused(false);
  focusManager.setFocused(true);
}

describe('session expiry', () => {
  it('explains an expiry rather than silently showing the login page', async () => {
    backend = installFakeBackend({ session: makeUser() });

    renderApp('/');
    await waitForAuthToResolve();
    expect(screen.getByRole('navigation', { name: 'Primary' })).toBeInTheDocument();

    // The server revokes it — a logout elsewhere, or an idle timeout.
    backend.state.session = null;
    await expireSessionInBackground();

    await waitFor(() => {
      expect(screen.getByText('Your session ended')).toBeInTheDocument();
    });
  });

  it('does not show authenticated data after an expiry', async () => {
    backend = installFakeBackend({ session: makeUser({ username: 'StaleAdmin' }) });

    renderApp('/');
    await waitForAuthToResolve();

    backend.state.session = null;
    await expireSessionInBackground();

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
    });
    expect(screen.queryByText('StaleAdmin')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Backend unavailable
// ---------------------------------------------------------------------------

describe('backend unavailable', () => {
  // These deliberately wait out the query layer's two retries with backoff
  // (~3s), because that is what a real client does before declaring the
  // backend unreachable — hence the explicit per-test timeouts.

  it('says the control plane is unreachable, not that the user is signed out', async () => {
    backend = installFakeBackend({ transport: 'network' });

    renderApp('/');

    await waitFor(
      () => {
        expect(screen.getByText('Control plane unreachable')).toBeInTheDocument();
      },
      { timeout: 10_000 },
    );
    // The distinction that matters: the session was not ended, and saying so
    // would be false.
    expect(screen.getByText(/session has not been ended/i)).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Sign in' })).toBeNull();
  }, 20_000);

  it('recovers when the backend returns, via the retry control', async () => {
    backend = installFakeBackend({ transport: 'network', session: makeUser() });

    renderApp('/');
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
        expect(screen.getByRole('navigation', { name: 'Primary' })).toBeInTheDocument();
      },
      { timeout: 10_000 },
    );
  }, 20_000);

  it('shows the unavailable state on the login route too', async () => {
    // Letting the user type a password into a form that cannot succeed would
    // waste an attempt from their throttle budget.
    backend = installFakeBackend({ transport: 'network' });

    renderApp('/login');

    await waitFor(
      () => {
        expect(screen.getByText('Control plane unreachable')).toBeInTheDocument();
      },
      { timeout: 10_000 },
    );
    expect(screen.queryByLabelText('Password')).toBeNull();
  }, 20_000);
});
