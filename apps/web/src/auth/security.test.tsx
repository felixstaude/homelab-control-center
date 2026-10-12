/**
 * Security regressions for the client.
 *
 * Each of these pins a property that is easy to break without noticing,
 * because breaking it produces no visible symptom.
 */

import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';

import { ApiError, isPermissionDenied, isSessionExpired } from '@/api/errors';
import { installFakeBackend, makeUser, userWithRole, type FakeBackend } from '@/test/fake-backend';
import { normalizeRole, permissionsFor } from '@/auth/permissions';
import { renderApp, waitForAuthToResolve } from '@/test/render-app';

let backend: FakeBackend;

afterEach(() => {
  backend.restore();
});

const PASSWORD = 'correct horse battery staple';

describe('the password does not linger', () => {
  it('appears in no request other than the login body', async () => {
    backend = installFakeBackend({ session: null });
    renderApp('/login');
    await waitForAuthToResolve();

    const user = userEvent.setup();
    await user.type(screen.getByLabelText('Username'), 'FelixAdmin');
    await user.type(screen.getByLabelText('Password'), PASSWORD);
    await user.click(screen.getByRole('button', { name: 'Sign in' }));

    await waitFor(() => {
      expect(screen.getByRole('navigation', { name: 'Primary' })).toBeInTheDocument();
    });

    const carrying = backend.requests.filter(
      (request) => JSON.stringify(request.body ?? '').includes(PASSWORD),
    );
    expect(carrying).toHaveLength(1);
    expect(carrying[0]?.path).toContain('/auth/login');
    // And never in a header, where it could reach a proxy log.
    for (const request of backend.requests) {
      expect(JSON.stringify(request.headers)).not.toContain(PASSWORD);
    }
  });

  it('is gone from the document after signing in', async () => {
    backend = installFakeBackend({ session: null });
    renderApp('/login');
    await waitForAuthToResolve();

    const user = userEvent.setup();
    await user.type(screen.getByLabelText('Username'), 'FelixAdmin');
    await user.type(screen.getByLabelText('Password'), PASSWORD);
    await user.click(screen.getByRole('button', { name: 'Sign in' }));

    await waitFor(() => {
      expect(screen.getByRole('navigation', { name: 'Primary' })).toBeInTheDocument();
    });

    // Includes attribute values, so a controlled input that serialised its
    // value would be caught.
    expect(document.body.outerHTML).not.toContain(PASSWORD);
  });

  it('is cleared from the form after a failed attempt', async () => {
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

    const user = userEvent.setup();
    await user.type(screen.getByLabelText('Username'), 'FelixAdmin');
    await user.type(screen.getByLabelText('Password'), PASSWORD);
    await user.click(screen.getByRole('button', { name: 'Sign in' }));

    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeInTheDocument();
    });
    expect(screen.getByLabelText('Password')).toHaveValue('');
    expect(document.body.outerHTML).not.toContain(PASSWORD);
  });
});

describe('cache isolation between users', () => {
  it('shows nothing of the previous user after a different one signs in', async () => {
    // Two people on one browser — a shared workstation, or an operator
    // switching between an admin and a viewer account.
    backend = installFakeBackend({ session: makeUser({ username: 'FirstAdmin' }) });
    renderApp('/');
    await waitForAuthToResolve();
    expect(screen.getByText('FirstAdmin')).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /FirstAdmin/ }));
    await user.click(screen.getByRole('button', { name: 'Sign out' }));
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
    });

    backend.state.loginResult = makeUser({ username: 'SecondOperator', role: 'operator' });
    await user.type(screen.getByLabelText('Username'), 'SecondOperator');
    await user.type(screen.getByLabelText('Password'), PASSWORD);
    await user.click(screen.getByRole('button', { name: 'Sign in' }));

    await waitFor(() => {
      expect(screen.getByText('SecondOperator')).toBeInTheDocument();
    });
    // No trace of the first identity, and the role shown is the new one.
    expect(screen.queryByText('FirstAdmin')).toBeNull();
    expect(screen.getByText('Operator')).toBeInTheDocument();
    expect(screen.queryByText('Administrator')).toBeNull();
  });
});

describe('403 is not treated as a logout', () => {
  it('distinguishes a permission denial from an expired session', () => {
    // The structural reason this holds in the app: the auth state derives
    // only from `/auth/me`, and only its 401 produces a null session. This
    // pins the predicate those decisions are made with.
    const forbidden = new ApiError('forbidden', 'You do not have permission.', {
      status: 403,
      code: 'insufficient_permissions',
    });
    const csrf = new ApiError('csrf', 'CSRF validation failed.', {
      status: 403,
      code: 'csrf_validation_failed',
    });

    expect(isSessionExpired(forbidden)).toBe(false);
    expect(isSessionExpired(csrf)).toBe(false);
    expect(isPermissionDenied(forbidden)).toBe(true);
    expect(isPermissionDenied(csrf)).toBe(false);
  });
});

describe('role handling fails closed', () => {
  it.each(['viewer', 'operator', 'admin'] as const)('keeps the known role %s', (role) => {
    expect(normalizeRole(role)).toBe(role);
  });

  it.each(['superuser', 'root', 'ADMIN', '', 'admin ', 'owner'])(
    'treats the unrecognised role %j as the least privilege',
    (role) => {
      // The type says this cannot happen; the network says otherwise. The
      // client only decides what to render, so the smallest interface is the
      // safe default — and the server refuses anything beyond it regardless.
      expect(normalizeRole(role)).toBe('viewer');
    },
  );

  it('grants an unrecognised role nothing beyond reading', () => {
    const permissions = permissionsFor(normalizeRole('superuser'));

    expect(permissions.viewMonitoring).toBe(true);
    expect(permissions.controlHosts).toBe(false);
    expect(permissions.configureMonitoring).toBe(false);
    expect(permissions.administer).toBe(false);
  });

  it('gives each role exactly the permissions the matrix states', () => {
    expect(permissionsFor('viewer')).toEqual({
      viewMonitoring: true,
      controlHosts: false,
      configureMonitoring: false,
      administer: false,
    });
    expect(permissionsFor('operator')).toEqual({
      viewMonitoring: true,
      controlHosts: true,
      configureMonitoring: true,
      administer: false,
    });
    expect(permissionsFor('admin')).toEqual({
      viewMonitoring: true,
      controlHosts: true,
      configureMonitoring: true,
      administer: true,
    });
  });

  it.each([
    ['viewer', 'Viewer'],
    ['operator', 'Operator'],
    ['admin', 'Administrator'],
  ] as const)('shows the %s role as %s in the account menu', async (role, label) => {
    // One mount per role, so each case gets RTL's normal cleanup rather than
    // a hand-cleared document — an earlier loop over all three shared one
    // mount and failed for that reason alone.
    backend = installFakeBackend({ session: userWithRole(role) });

    renderApp('/');
    await waitForAuthToResolve();

    expect(screen.getByText(label)).toBeInTheDocument();
  });
});

describe('no unauthenticated data reaches the dashboard', () => {
  it('makes no authenticated request before the session resolves', async () => {
    // A request fired optimistically would 401, and on a protected page would
    // put an error on screen before the user had even been identified.
    backend = installFakeBackend({ session: null });

    renderApp('/');
    await waitForAuthToResolve();

    const paths = backend.requests.map((request) => request.path);
    // Only the two bootstrap-time reads.
    expect(paths.filter((path) => path.endsWith('/setup/status'))).toHaveLength(1);
    expect(paths.filter((path) => path.endsWith('/auth/me'))).toHaveLength(1);
    expect(paths.filter((path) => path.endsWith('/info'))).toHaveLength(0);
  });

  it('sends no mutation while unauthenticated', async () => {
    backend = installFakeBackend({ session: null });

    renderApp('/');
    await waitForAuthToResolve();

    expect(backend.requests.filter((request) => request.method !== 'GET')).toHaveLength(0);
  });
});
