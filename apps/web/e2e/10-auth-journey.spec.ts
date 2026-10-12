/**
 * The operator's journey, in a real browser against a real control plane.
 *
 * Run by `scripts/e2e.sh`, which creates a disposable database, applies
 * migrations, starts the backend, builds the client and serves it through the
 * `/api` proxy. These tests then drive Chromium through the whole first-run
 * and authentication experience.
 *
 * ## What this catches that nothing else does
 *
 * Every other test in this repository stubs something: the backend tests have
 * no browser, and the component tests stub `fetch`. The failures that live in
 * between are real cookie attributes, the `Secure` flag, `SameSite`, the proxy
 * rewriting `Host`, the production bundle, and whether a redirect survives an
 * actual browser reload. Those are exactly the failures a milestone joining a
 * frontend to a backend produces.
 *
 * ## Ordering
 *
 * The tests run serially, in file order, against one installation — because
 * the journey *is* ordered: an installation is uninitialised exactly once, and
 * the setup screen can only be seen before bootstrap. Splitting that into
 * independent tests would mean recreating the database per test, which is
 * slower and tests less.
 */

import { execFileSync } from 'node:child_process';

import { expect, test } from '@playwright/test';

import {
  ADMIN_PASSWORD,
  ADMIN_USERNAME,
  BACKEND_URL,
  DATABASE_URL,
  SERVER_DIR,
  bootstrapAdministrator,
  openAccountMenu,
  signIn,
} from './helpers';

test.describe.configure({ mode: 'serial' });

// ---------------------------------------------------------------------------
// 1. A fresh installation
// ---------------------------------------------------------------------------

test('a fresh installation asks to be initialised', async ({ page }) => {
  await page.goto('/');

  await expect(
    page.getByRole('heading', { name: /Finish setting up this installation/ }),
  ).toBeVisible();
  // The supported command, and no form.
  await expect(page.getByText(/hcc bootstrap-admin/)).toBeVisible();
  await expect(page.getByLabel('Password', { exact: true })).toHaveCount(0);
});

test('the setup screen is shown for the login route too', async ({ page }) => {
  await page.goto('/login');

  await expect(
    page.getByRole('heading', { name: /Finish setting up this installation/ }),
  ).toBeVisible();
});

test('a protected route redirects to setup, not to a dashboard', async ({ page }) => {
  await page.goto('/settings');

  await expect(
    page.getByRole('heading', { name: /Finish setting up this installation/ }),
  ).toBeVisible();
  await expect(page.getByRole('navigation', { name: 'Primary' })).toHaveCount(0);
});

// ---------------------------------------------------------------------------
// 2. Bootstrap, through the documented command
// ---------------------------------------------------------------------------

test('the documented command initialises the installation, with no reload', async ({
  page,
}) => {
  // The real first-run sequence: the operator is looking at the setup screen,
  // runs the command in a terminal, and comes back to the same tab. The page
  // must notice without a reload, which is what the retry control is for —
  // navigating instead would hide whether that works.
  await page.goto('/setup');
  await expect(
    page.getByRole('heading', { name: /Finish setting up this installation/ }),
  ).toBeVisible();

  const output = bootstrapAdministrator();

  expect(output).toContain('Administrator created.');
  expect(output).toContain('admin');
  // Nothing secret in the output an operator might paste into an issue.
  expect(output).not.toContain(ADMIN_PASSWORD);
  expect(output).not.toContain('$argon2');

  await page.getByRole('button', { name: /I have created the administrator/ }).click();

  await expect(page.getByRole('heading', { name: 'Setup complete' })).toBeVisible();
  await expect(page.getByRole('link', { name: /Continue to sign in/ })).toBeVisible();
});

test('bootstrap cannot be run a second time', () => {
  // The one-way door, exercised against a live installation rather than a
  // test database fixture.
  let exitCode = 0;
  try {
    execFileSync(
      `${SERVER_DIR}/.venv/bin/hcc`,
      ['bootstrap-admin', '--username', 'Second', '--password-stdin'],
      {
        cwd: SERVER_DIR,
        input: `${ADMIN_PASSWORD}\n`,
        encoding: 'utf8',
        env: { ...process.env, HCC_DATABASE_URL: DATABASE_URL },
        stdio: ['pipe', 'pipe', 'pipe'],
      },
    );
  } catch (error) {
    exitCode = (error as { status?: number }).status ?? 0;
  }

  // 3 means "already initialized" — the code an idempotent provisioning
  // script treats as success.
  expect(exitCode).toBe(3);
});

// ---------------------------------------------------------------------------
// 3. Signing in
// ---------------------------------------------------------------------------

test('an anonymous visitor is sent to the login page', async ({ page }) => {
  await page.goto('/');

  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
  await expect(page.getByRole('navigation', { name: 'Primary' })).toHaveCount(0);
});

test('invalid credentials are refused without revealing anything', async ({ page }) => {
  await page.goto('/login');
  await signIn(page, 'no-such-user', 'definitely wrong');

  const alert = page.getByRole('alert');
  await expect(alert).toContainText('Invalid username or password.');
  // The same message a wrong password gets: no username oracle.
  await expect(alert).not.toContainText(/unknown|no such user|not found/i);
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
});

test('the administrator can sign in and reach the dashboard', async ({ page }) => {
  await page.goto('/login');
  await signIn(page);

  await expect(page.getByRole('navigation', { name: 'Primary' })).toBeVisible();
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Overview');
  // The account menu shows who is signed in, and with what role.
  await expect(page.getByText(ADMIN_USERNAME)).toBeVisible();
  await expect(page.getByText('Administrator')).toBeVisible();
});

test('the session cookie is HttpOnly and the CSRF cookie is not', async ({ page, context }) => {
  await page.goto('/login');
  await signIn(page);
  await expect(page.getByRole('navigation', { name: 'Primary' })).toBeVisible();

  const cookies = await context.cookies();
  const session = cookies.find((cookie) => cookie.name === 'hcc_session');
  const csrf = cookies.find((cookie) => cookie.name === 'hcc_csrf');

  expect(session, 'no session cookie was set').toBeDefined();
  expect(csrf, 'no CSRF cookie was set').toBeDefined();
  // The real browser's view of the attributes — not a parsed header.
  expect(session?.httpOnly).toBe(true);
  expect(csrf?.httpOnly).toBe(false);
  expect(session?.sameSite).toBe('Lax');
  expect(session?.path).toBe('/');

  // And the session token is genuinely unreachable from script, which is the
  // property HttpOnly exists for.
  const visible = await page.evaluate(() => document.cookie);
  expect(visible).not.toContain('hcc_session');
  expect(visible).toContain('hcc_csrf');
});

test('nothing is written to browser storage', async ({ page }) => {
  await page.goto('/login');
  await signIn(page);
  await expect(page.getByRole('navigation', { name: 'Primary' })).toBeVisible();

  const stored = await page.evaluate(() => ({
    local: Object.keys(localStorage),
    session: Object.keys(sessionStorage),
  }));

  expect(stored.local).toEqual([]);
  expect(stored.session).toEqual([]);
});

// ---------------------------------------------------------------------------
// 4. Staying signed in
// ---------------------------------------------------------------------------

test('a browser reload keeps the session and the page', async ({ page }) => {
  await page.goto('/login');
  await signIn(page);
  await expect(page.getByRole('navigation', { name: 'Primary' })).toBeVisible();

  await page.getByRole('link', { name: 'Hosts' }).click();
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Hosts');

  // The real thing: a full document reload, not a client-side remount.
  await page.reload();

  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Hosts');
  await expect(page.getByRole('heading', { name: 'Sign in' })).toHaveCount(0);
});

test('navigation between protected pages works', async ({ page }) => {
  await page.goto('/login');
  await signIn(page);
  await expect(page.getByRole('navigation', { name: 'Primary' })).toBeVisible();

  for (const [label, heading] of [
    ['Hosts', 'Hosts'],
    ['Settings', 'Settings'],
    ['Docker', 'Docker'],
    ['Overview', 'Overview'],
  ] as const) {
    await page.getByRole('link', { name: label }).click();
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(heading);
  }
});

test('browser back and forward behave', async ({ page }) => {
  await page.goto('/login');
  await signIn(page);
  await expect(page.getByRole('navigation', { name: 'Primary' })).toBeVisible();

  await page.getByRole('link', { name: 'Hosts' }).click();
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Hosts');
  await page.getByRole('link', { name: 'Alerts' }).click();
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Alerts');

  await page.goBack();
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Hosts');
  await page.goForward();
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Alerts');

  // Back past the login page must not land on it: the sign-in navigation
  // replaced that entry.
  await page.goBack();
  await page.goBack();
  await expect(page.getByRole('heading', { name: 'Sign in' })).toHaveCount(0);
});

test('a deep link returns the user to where they were going', async ({ page }) => {
  // Arrive unauthenticated at a protected page, sign in, and land there —
  // not on the dashboard.
  await page.goto('/settings');
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();

  await signIn(page);

  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Settings');
});

// ---------------------------------------------------------------------------
// 5. Signing out
// ---------------------------------------------------------------------------

test('signing out revokes the session server-side', async ({ page, context }) => {
  await page.goto('/login');
  await signIn(page);
  await expect(page.getByRole('navigation', { name: 'Primary' })).toBeVisible();

  // Capture the session cookie so the revocation can be proved directly,
  // rather than inferred from the UI.
  const before = (await context.cookies()).find((cookie) => cookie.name === 'hcc_session');
  expect(before?.value).toBeTruthy();

  await openAccountMenu(page);
  await page.getByRole('button', { name: 'Sign out' }).click();

  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
  await expect(page.getByText('You are signed out')).toBeVisible();

  // The cookie is gone from the browser…
  const after = (await context.cookies()).find((cookie) => cookie.name === 'hcc_session');
  expect(after).toBeUndefined();

  // …and, more importantly, replaying the captured token is refused. Cookie
  // clearing is a courtesy; server-side revocation is the boundary.
  const replay = await page.request.get(`${BACKEND_URL}/api/v1/auth/me`, {
    headers: { Cookie: `hcc_session=${before?.value ?? ''}` },
  });
  expect(replay.status()).toBe(401);
});

test('protected routes are inaccessible after signing out', async ({ page }) => {
  await page.goto('/login');
  await signIn(page);
  await expect(page.getByRole('navigation', { name: 'Primary' })).toBeVisible();

  await openAccountMenu(page);
  await page.getByRole('button', { name: 'Sign out' }).click();
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();

  for (const path of ['/', '/hosts', '/settings']) {
    await page.goto(path);
    await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
    await expect(page.getByRole('navigation', { name: 'Primary' })).toHaveCount(0);
  }
});

test('signing back in works', async ({ page }) => {
  await page.goto('/login');
  await signIn(page);
  await expect(page.getByRole('navigation', { name: 'Primary' })).toBeVisible();

  await openAccountMenu(page);
  await page.getByRole('button', { name: 'Sign out' }).click();
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();

  await signIn(page);
  await expect(page.getByRole('navigation', { name: 'Primary' })).toBeVisible();
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Overview');
});

// ---------------------------------------------------------------------------
// 6. The setup screen after initialisation
// ---------------------------------------------------------------------------

test('the setup route no longer offers instructions', async ({ page }) => {
  // Instructions for a command that would now refuse to run are worse than
  // no instructions.
  await page.goto('/setup');

  await expect(page.getByRole('heading', { name: 'Setup complete' })).toBeVisible();
  await expect(page.getByText(/hcc bootstrap-admin/)).toHaveCount(0);
});

// ---------------------------------------------------------------------------
// 7. Keyboard operation
// ---------------------------------------------------------------------------

test('the login form is operable from the keyboard alone', async ({ page }) => {
  await page.goto('/login');

  // The first field is focused on arrival, so typing works immediately.
  await expect(page.getByLabel('Username', { exact: true })).toBeFocused();
  await page.keyboard.type(ADMIN_USERNAME);
  await page.keyboard.press('Tab');
  await expect(page.getByLabel('Password', { exact: true })).toBeFocused();
  await page.keyboard.type(ADMIN_PASSWORD);
  await page.keyboard.press('Enter');

  await expect(page.getByRole('navigation', { name: 'Primary' })).toBeVisible();
});

test('the password reveal toggle works and reports its state', async ({ page }) => {
  await page.goto('/login');
  await page.getByLabel('Password', { exact: true }).fill(ADMIN_PASSWORD);

  const toggle = page.getByRole('button', { name: 'Show password' });
  await expect(toggle).toHaveAttribute('aria-pressed', 'false');
  await toggle.click();

  await expect(page.getByLabel('Password', { exact: true })).toHaveAttribute('type', 'text');
  await expect(page.getByRole('button', { name: 'Hide password' })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
});

// ---------------------------------------------------------------------------
// 8. Responsiveness
// ---------------------------------------------------------------------------

test('the console is usable on a phone-sized viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/login');
  await signIn(page);

  // The desktop sidebar is hidden; navigation is in the drawer.
  await expect(page.getByRole('button', { name: 'Open navigation' })).toBeVisible();
  await page.getByRole('button', { name: 'Open navigation' }).click();

  await expect(page.getByRole('navigation', { name: 'Primary' })).toBeVisible();
  // Sign-out has to be reachable here too, or a phone user is stuck.
  await expect(page.getByRole('button', { name: new RegExp(ADMIN_USERNAME) })).toBeVisible();
});

test('the login page is usable on a small laptop viewport', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto('/login');

  const card = page.getByRole('heading', { name: 'Sign in' });
  await expect(card).toBeInViewport();
  await expect(page.getByRole('button', { name: 'Sign in' })).toBeInViewport();
});

// ---------------------------------------------------------------------------
// 9. Security headers on the real server
// ---------------------------------------------------------------------------

test('API responses carry the hardening headers', async ({ page }) => {
  const response = await page.request.get(`${BACKEND_URL}/api/v1/setup/status`);

  expect(response.status()).toBe(200);
  const headers = response.headers();
  expect(headers['x-content-type-options']).toBe('nosniff');
  expect(headers['x-frame-options']).toBe('DENY');
  expect(headers['referrer-policy']).toBe('strict-origin-when-cross-origin');
  expect(headers['x-request-id']).toBeTruthy();
});

test('a cross-site mutation is refused by the Origin check', async ({ page }) => {
  // The CSRF defence, exercised against the running server: a request
  // carrying a valid session and a valid token but a foreign Origin.
  await page.goto('/login');
  await signIn(page);
  await expect(page.getByRole('navigation', { name: 'Primary' })).toBeVisible();

  const csrf = await page.evaluate(() => {
    const match = /(?:^|;\s*)hcc_csrf=([^;]*)/.exec(document.cookie);
    return match?.[1] ?? '';
  });
  expect(csrf).not.toBe('');

  const refused = await page.request.post(`${BACKEND_URL}/api/v1/auth/logout`, {
    headers: { 'X-CSRF-Token': decodeURIComponent(csrf), Origin: 'https://evil.example.com' },
  });

  expect(refused.status()).toBe(403);
  expect(await refused.json()).toMatchObject({
    error: { code: 'csrf_validation_failed' },
  });
  // And the session survived the refused request.
  await page.reload();
  await expect(page.getByRole('navigation', { name: 'Primary' })).toBeVisible();
});
