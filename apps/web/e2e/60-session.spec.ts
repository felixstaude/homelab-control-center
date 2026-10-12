/**
 * Session lifecycle against a real control plane.
 *
 * These are the states a unit test cannot reach honestly: a session that has
 * genuinely aged out in the database, a session revoked server-side while a
 * browser still holds its cookie, a backend that has stopped answering, and a
 * throttle budget that has actually been spent.
 *
 * Several of them are reached by writing to the database directly. That is
 * deliberate — there is no API for "make this session stale", and a session
 * expiring is not an API operation but the passage of time. Driving it through
 * SQL is the only way to test the real code path rather than a stub of it.
 */

import { expect, test } from '@playwright/test';

import {
  ADMIN_PASSWORD,
  ADMIN_USERNAME,
  THROTTLE_WINDOW_SECONDS,
  apiFromPage,
  cookie,
  openAccountMenu,
  signIn,
  signInAndWait,
  sql,
  ensureAdministrator,
} from './helpers';

test.describe.configure({ mode: 'serial' });

// Runnable on its own: in a full run `10-auth-journey.spec.ts` has already
// bootstrapped and this exits 3, which is tolerated.
test.beforeAll(() => {
  ensureAdministrator();
});

/** Rows in `sessions` that are still usable right now. */
function liveSessionCount(): number {
  const rows = sql(
    'SELECT count(*) AS n FROM sessions WHERE revoked_at IS NULL AND expires_at > now()',
  );
  return Number((rows[0] as { n: string }).n);
}

// ---------------------------------------------------------------------------
// Restoration
// ---------------------------------------------------------------------------

test('the session survives a full client restart, not just a reload', async ({ browser }) => {
  // "Restarting the frontend" for a single-page application means the client
  // losing all of its in-memory state while the cookie jar survives — a new
  // browsing context with the same cookies reproduces exactly that, and is a
  // stronger check than a reload because nothing at all is carried over in
  // JavaScript.
  const first = await browser.newContext();
  const page = await first.newPage();
  await signInAndWait(page);
  const state = await first.storageState();
  await first.close();

  const second = await browser.newContext({ storageState: state });
  const restored = await second.newPage();
  await restored.goto('/');

  // Straight to the console: no login page in between, and the identity is
  // the one the server knows rather than anything the client remembered.
  await expect(restored.getByRole('navigation', { name: 'Primary' })).toBeVisible();
  await expect(restored.getByText(ADMIN_USERNAME)).toBeVisible();
  await second.close();
});

test('a deep link survives a client restart', async ({ browser }) => {
  const first = await browser.newContext();
  const page = await first.newPage();
  await signInAndWait(page);
  const state = await first.storageState();
  await first.close();

  const second = await browser.newContext({ storageState: state });
  const restored = await second.newPage();
  await restored.goto('/network');
  await expect(restored.getByRole('heading', { level: 2, name: 'Network' })).toBeVisible();
  await expect(restored).toHaveURL(/\/network$/);
  await second.close();
});

// ---------------------------------------------------------------------------
// Expiry and staleness
// ---------------------------------------------------------------------------

test('an expired session is refused and the user is asked to sign in again', async ({ page }) => {
  await signInAndWait(page);

  // Age every live session past its expiry. The cookie in the browser is
  // untouched and still well-formed — only the server's record has aged,
  // which is precisely the real case.
  //
  // `created_at` moves too: the table has a CHECK that expiry follows
  // creation, so a row cannot be aged by moving only one end of it. That
  // constraint is doing its job — it is why this needed two columns.
  sql(
    "UPDATE sessions SET created_at = now() - interval '2 hours'," +
      " expires_at = now() - interval '1 hour' WHERE revoked_at IS NULL",
  );

  const me = await apiFromPage(page, '/api/v1/auth/me');
  expect(me.status).toBe(401);

  // And the interface agrees with the server rather than continuing to show a
  // signed-in console.
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
});

test('an expired session can be replaced by signing in again', async ({ page }) => {
  // Recovery, not just refusal: the throttle must not have been spent and the
  // old cookie must not interfere with the new session.
  await page.goto('/login');
  await signIn(page);
  await expect(page.getByRole('navigation', { name: 'Primary' })).toBeVisible();
  const me = await apiFromPage(page, '/api/v1/auth/me');
  expect(me.status).toBe(200);
});

test('a session revoked server-side stops working immediately', async ({ page }) => {
  await signInAndWait(page);
  expect(liveSessionCount()).toBeGreaterThan(0);

  sql('UPDATE sessions SET revoked_at = now() WHERE revoked_at IS NULL');

  const me = await apiFromPage(page, '/api/v1/auth/me');
  expect(me.status).toBe(401);
  // A revoked session must not be resurrected by the next request, which a
  // last-seen-touching implementation could plausibly get wrong.
  const again = await apiFromPage(page, '/api/v1/auth/me');
  expect(again.status).toBe(401);
});

test('a forged session cookie is refused', async ({ page, context }) => {
  await signInAndWait(page);
  const session = await cookie(page, 'hcc_session');
  expect(session).toBeDefined();

  // Same shape, different value: the server stores only a hash, so a guessed
  // token must simply not resolve.
  await context.clearCookies({ name: 'hcc_session' });
  await context.addCookies([
    {
      name: 'hcc_session',
      value: 'f'.repeat(session!.value.length),
      domain: '127.0.0.1',
      path: '/',
      httpOnly: true,
      sameSite: 'Lax',
    },
  ]);

  const me = await apiFromPage(page, '/api/v1/auth/me');
  expect(me.status).toBe(401);
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
});

// ---------------------------------------------------------------------------
// Sign-out
// ---------------------------------------------------------------------------

test('signing out revokes the row, not just the cookie', async ({ page }) => {
  await signInAndWait(page);
  const before = liveSessionCount();
  expect(before).toBeGreaterThan(0);

  await openAccountMenu(page);
  await page.getByRole('button', { name: 'Sign out' }).click();
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();

  // The server's own record is what matters: a client that merely dropped its
  // cookie would leave a usable session behind for anyone who had captured it.
  expect(liveSessionCount()).toBe(before - 1);
});

test('signing out twice in quick succession is not an error', async ({ page }) => {
  // The race: the menu is still open when the first request is in flight, and
  // an impatient second click must not produce an error screen or leave the
  // user looking at a signed-in console.
  await signInAndWait(page);

  const logouts: string[] = [];
  page.on('request', (request) => {
    if (request.url().includes('/auth/logout')) logouts.push(request.method());
  });

  await openAccountMenu(page);
  const signOut = page.getByRole('button', { name: 'Sign out' });

  // Both clicks issued without awaiting the first, which is what an impatient
  // double-click actually is. Short timeouts and `allSettled` because the
  // second click legitimately loses its target when the menu closes — waiting
  // the default timeout out for that would consume the whole test budget.
  await Promise.allSettled([
    signOut.click({ timeout: 2000 }),
    signOut.click({ timeout: 2000, force: true }),
  ]);

  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
  // One revocation, not two: a second request would 401 and could be mistaken
  // for an expired session, which tells the user the wrong story.
  expect(logouts.length, `sent ${logouts.length} logout requests`).toBe(1);
  // A deliberate sign-out is confirmed as such, not reported as an error or
  // as something that happened to the user.
  await expect(page.getByRole('alert')).toHaveCount(0);
  await expect(page.getByText('You are signed out')).toBeVisible();
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
});

// ---------------------------------------------------------------------------
// The state before the answer arrives
// ---------------------------------------------------------------------------

test('a slow session check shows a labelled loading state, not a login page', async ({ page }) => {
  // The window between "the page has loaded" and "we know who you are" is
  // where the worst version of this bug lives: showing the login page to
  // someone who is in fact signed in, or flashing the console to someone who
  // is not. Delaying the real response is the only way to hold that window
  // open long enough to assert on.
  await page.route('**/api/v1/auth/me', async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 2500));
    await route.continue();
  });

  await page.goto('/');

  // Announced, not just a spinning shape: `status` is the role a screen
  // reader needs to report that something is in progress.
  const loading = page.getByRole('status');
  await expect(loading).toBeVisible();
  await expect(loading).toContainText(/checking your session/i);

  // And emphatically neither of the two answers.
  await expect(page.getByRole('heading', { name: 'Sign in' })).toHaveCount(0);
  await expect(page.locator('#main-content')).toHaveCount(0);

  // It resolves to the right answer once the response lands.
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible({ timeout: 20_000 });
});

test('a slow session check resolves to the console for a signed-in user', async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await signInAndWait(page);
  const state = await context.storageState();
  await context.close();

  const slow = await browser.newContext({ storageState: state });
  const restored = await slow.newPage();
  await restored.route('**/api/v1/auth/me', async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    await route.continue();
  });

  await restored.goto('/hosts');
  await expect(restored.getByRole('status')).toContainText(/checking your session/i);
  // Never the login page on the way: a signed-in user must not be shown a
  // sign-in form and then have it yanked away.
  await expect(restored.getByRole('heading', { name: 'Sign in' })).toHaveCount(0);

  await expect(restored.locator('#main-content')).toBeVisible({ timeout: 20_000 });
  await expect(restored).toHaveURL(/\/hosts$/);
  await slow.close();
});

// ---------------------------------------------------------------------------
// 401 versus 403
// ---------------------------------------------------------------------------

test('a 403 does not sign the user out', async ({ page }) => {
  await signInAndWait(page);

  // A real 403 from the real server: a mutation with a wrong CSRF token. The
  // session behind it is perfectly valid, and treating this as an expired
  // session would throw the user out of a working session.
  const denied = await apiFromPage(page, '/api/v1/auth/logout', {
    method: 'POST',
    headers: { 'x-csrf-token': 'not-the-right-token' },
  });
  expect(denied.status).toBe(403);
  expect(denied.body).toMatchObject({ error: { code: 'csrf_validation_failed' } });

  // Still signed in, on both sides.
  await expect(page.getByRole('navigation', { name: 'Primary' })).toBeVisible();
  const me = await apiFromPage(page, '/api/v1/auth/me');
  expect(me.status).toBe(200);

  // And the refused request did not revoke anything.
  await page.reload();
  await expect(page.getByRole('navigation', { name: 'Primary' })).toBeVisible();
});

test('unauthenticated access to a protected API route is refused', async ({ page }) => {
  await page.goto('/login');
  for (const path of ['/api/v1/auth/me', '/api/v1/info']) {
    const response = await apiFromPage(page, path);
    expect(response.status, `${path} should require a session`).toBe(401);
    expect(response.body).toMatchObject({ error: { code: 'not_authenticated' } });
  }
});

test('unauthenticated access to a protected page lands on login, never on data', async ({
  page,
}) => {
  for (const path of ['/', '/hosts', '/docker', '/logs', '/alerts', '/network', '/settings']) {
    await page.goto(path);
    await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
    // Not a flash of the console behind the redirect: the shell's navigation
    // must not be in the document at all.
    await expect(page.getByRole('navigation', { name: 'Primary' })).toHaveCount(0);
  }
});

// ---------------------------------------------------------------------------
// The backend going away, and coming back
// ---------------------------------------------------------------------------

test('an unreachable backend is reported as unreachable, not as a sign-out', async ({ page }) => {
  // Abort at the network layer, which is what the browser sees when the
  // control plane is down, a proxy is misconfigured, or the network drops.
  await page.route('**/api/**', (route) => route.abort('connectionrefused'));
  await page.goto('/');

  const unavailable = page.getByText(/cannot reach the control plane/i);
  await expect(unavailable).toBeVisible({ timeout: 20_000 });
  // The distinction that matters: this is not the login page, because the user
  // has not been signed out of anything.
  await expect(page.getByRole('heading', { name: 'Sign in' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Retry' })).toBeVisible();
});

test('the application recovers when the backend comes back', async ({ page }) => {
  await page.route('**/api/**', (route) => route.abort('connectionrefused'));
  await page.goto('/');
  await expect(page.getByText(/cannot reach the control plane/i)).toBeVisible({ timeout: 20_000 });

  // The backend returns. No reload: the retry button must be able to recover
  // the application in place, because telling a user to reload is an admission
  // the state machine cannot recover.
  await page.unroute('**/api/**');
  await page.getByRole('button', { name: 'Retry' }).click();

  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible({ timeout: 20_000 });
  await signIn(page);
  await expect(page.getByRole('navigation', { name: 'Primary' })).toBeVisible();
});

test('a backend failure mid-session does not discard the session', async ({ page }) => {
  await signInAndWait(page);

  // The session is established, then the network drops. The user must not be
  // told they signed out, and when it returns they must still be signed in —
  // the server never revoked anything.
  await page.route('**/api/**', (route) => route.abort('connectionrefused'));
  await page.reload();
  await expect(page.getByText(/cannot reach the control plane/i)).toBeVisible({ timeout: 20_000 });
  await expect(page.getByRole('heading', { name: 'Sign in' })).toHaveCount(0);

  await page.unroute('**/api/**');
  await page.getByRole('button', { name: 'Retry' }).click();
  await expect(page.getByRole('navigation', { name: 'Primary' })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText(ADMIN_USERNAME)).toBeVisible();
});

// ---------------------------------------------------------------------------
// Throttling
//
// Last in the file on purpose: it spends a real budget on a real server, and
// the window has to be waited out before anything else can sign in.
// ---------------------------------------------------------------------------

test('repeated failed logins are throttled, and the UI says so without retrying', async ({
  page,
}) => {
  test.setTimeout((THROTTLE_WINDOW_SECONDS + 60) * 1000);

  await page.goto('/login');

  // Spend the per-address-per-username budget against the real account with
  // the wrong password. Driven through the page so these are genuine requests
  // from the browser's address, counted by the real limiter.
  let throttled = false;
  let attempts = 0;
  for (let i = 0; i < 25 && !throttled; i += 1) {
    const response = await apiFromPage(page, '/api/v1/auth/login', {
      method: 'POST',
      body: { username: ADMIN_USERNAME, password: 'definitely not the password' },
    });
    attempts += 1;
    if (response.status === 429) {
      throttled = true;
      expect(response.body).toMatchObject({ error: { code: 'rate_limited' } });
    } else {
      expect(response.status, 'a wrong password should be 401 until throttled').toBe(401);
    }
  }
  expect(throttled, `never throttled after ${attempts} wrong passwords`).toBe(true);

  // Now the interface: a throttled attempt must say so, and must not quietly
  // retry — a retry here spends the user's own remaining budget.
  const loginRequests: number[] = [];
  page.on('request', (request) => {
    if (request.url().includes('/auth/login')) loginRequests.push(Date.now());
  });
  await signIn(page, ADMIN_USERNAME, ADMIN_PASSWORD);

  const alert = page.getByRole('alert');
  await expect(alert).toBeVisible();
  await expect(alert).toContainText(/too many|try again/i);
  // Even the correct password is refused while throttled, which is the point
  // of checking the limiter before verification.
  await expect(page.getByRole('navigation', { name: 'Primary' })).toHaveCount(0);

  await page.waitForTimeout(3000);
  expect(loginRequests.length, 'the client retried a throttled login').toBe(1);

  // Wait the window out so the rest of the run is not throttled. The limiter
  // has no permanent lockout, which is the property being relied on here.
  await page.waitForTimeout((THROTTLE_WINDOW_SECONDS + 1) * 1000);
  await page.reload();
  await signIn(page, ADMIN_USERNAME, ADMIN_PASSWORD);
  await expect(page.getByRole('navigation', { name: 'Primary' })).toBeVisible();
});
