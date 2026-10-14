/**
 * The administrator's user-management journey, in a real browser.
 *
 * Run by `scripts/e2e.sh` against a disposable database, real migrations, the
 * real control plane, the real `hcc bootstrap-admin`, and the production
 * bundle served through the `/api` proxy.
 *
 * ## What this catches that nothing else does
 *
 * The backend suite has no browser; the component suite stubs `fetch`. What
 * lives between them is everything this milestone actually added: whether a
 * session cookie issued by a *password change* is accepted on the next
 * request, whether a revocation performed in one browser context ends a
 * session held in another, whether a role reduced server-side changes what a
 * second browser is offered, and whether the production bundle's query strings
 * reach the endpoints they are meant to.
 *
 * ## Ordering, and why the file is serial
 *
 * This is one journey rather than twenty-five tests. The steps depend on each
 * other — an operator has to exist before it can be promoted, and promoted
 * before its own sessions can be revoked — and each `test` below is a station
 * on it, numbered to match `§21` of the milestone brief. Splitting them into
 * independent tests would mean rebuilding the account set per test, which is
 * slower and tests less.
 *
 * It runs last in the suite (alphabetically), after the first-run and session
 * specs have had their one chance to observe an uninitialised installation.
 *
 * ## Why the password change is not performed on the bootstrap administrator
 *
 * Step 21 changes an account's own password. Doing that to `E2eAdmin` would
 * invalidate `ADMIN_PASSWORD` in `helpers.ts` for everything that followed,
 * making the file order load-bearing in a way nobody would notice until a test
 * was added after it. The promoted operator is a real account changing its own
 * real password, which is the same journey with none of that coupling.
 */

import { expect, test, type Browser, type Page } from '@playwright/test';

import {
  ADMIN_PASSWORD,
  ADMIN_USERNAME,
  apiFromPage,
  ensureAdministrator,
  signIn,
  signInAndWait,
  signOut,
  sql,
} from './helpers';

test.describe.configure({ mode: 'serial' });

// Runnable on its own: in a full run `10-auth-journey.spec.ts` has already
// bootstrapped and this exits 3, which is tolerated.
test.beforeAll(() => {
  ensureAdministrator();
});

/** Accounts this journey creates. Not credentials for anything that outlives it. */
const OPERATOR = { username: 'E2eOperator', password: 'operator passphrase for e2e' };
const VIEWER = { username: 'E2eViewer', password: 'viewer passphrase for e2e' };
const OPERATOR_NEW_PASSWORD = 'operator rotated passphrase for e2e';

/** Navigate to a settings section and wait for its panel. */
async function openUsers(page: Page): Promise<void> {
  await page.goto('/settings/users');
  await expect(page.getByRole('table', { name: /user accounts/i })).toBeVisible();
}

/** The row for one account, found by its visible label. */
function userRow(page: Page, label: string) {
  return page.getByRole('row').filter({ hasText: label });
}

/**
 * Create an account through the interface.
 *
 * Through the dialog rather than through the API, because the dialog is what
 * the milestone added — a helper that posted JSON would test the endpoint the
 * backend suite already covers and skip the form entirely.
 */
async function createAccount(
  page: Page,
  { username, password, role }: { username: string; password: string; role: string },
): Promise<void> {
  await page.getByRole('button', { name: /New account/ }).click();
  const dialog = page.getByRole('dialog', { name: /Create a user account/i });
  await expect(dialog).toBeVisible();

  await dialog.getByLabel('Username').fill(username);
  await dialog.getByLabel('Password', { exact: true }).fill(password);
  await dialog.getByLabel('Confirm password').fill(password);
  await dialog.getByLabel('Role').selectOption(role);
  await dialog.getByRole('button', { name: /Create account/ }).click();

  await expect(dialog).toBeHidden();
  await expect(userRow(page, username)).toBeVisible();
}

/** A second, independent browser context — "another device". */
async function signedInContext(
  browser: Browser,
  baseURL: string,
  username: string,
  password: string,
): Promise<{ page: Page; close: () => Promise<void> }> {
  const context = await browser.newContext({ baseURL });
  const page = await context.newPage();
  await signInAndWait(page, username, password);
  return { page, close: () => context.close() };
}

// ---------------------------------------------------------------------------
// 1–4. Sign in, reach Settings, create an operator
// ---------------------------------------------------------------------------

test('an administrator signs in and reaches user management', async ({ page }) => {
  await signInAndWait(page);

  // Through the navigation, as an operator would, rather than by URL — this is
  // the path that has to work.
  await page.getByRole('link', { name: 'Settings' }).first().click();
  // `level: 1` is the shell header's heading. The page's own `<h2>` carries
  // the same words and is `sr-only` — deliberately still in the accessibility
  // tree (see `PageHeader`) — so an unqualified query matches both.
  await expect(page.getByRole('heading', { level: 1, name: 'Settings' })).toBeVisible();

  await page
    .getByRole('navigation', { name: 'Settings sections' })
    .getByRole('link', { name: /Users/ })
    .click();

  await expect(page.getByRole('table', { name: /user accounts/i })).toBeVisible();
  // The bootstrap administrator is there, and is recognised as the caller.
  await expect(userRow(page, ADMIN_USERNAME)).toContainText('This is you');
  await expect(userRow(page, ADMIN_USERNAME)).toContainText('Administrator');
});

test('the Users section survives a reload and a deep link', async ({ page }) => {
  await signInAndWait(page);
  await openUsers(page);

  await page.reload();
  await expect(page.getByRole('table', { name: /user accounts/i })).toBeVisible();
  await expect(page).toHaveURL(/\/settings\/users$/);
});

test('an administrator creates an operator', async ({ page }) => {
  await signInAndWait(page);
  await openUsers(page);

  await createAccount(page, { ...OPERATOR, role: 'operator' });

  await expect(userRow(page, OPERATOR.username)).toContainText('Operator');
  await expect(userRow(page, OPERATOR.username)).toContainText('Active');
});

test('a duplicate username is refused with an explanation', async ({ page }) => {
  await signInAndWait(page);
  await openUsers(page);

  await page.getByRole('button', { name: /New account/ }).click();
  const dialog = page.getByRole('dialog');
  // A case variant: usernames are case-insensitively unique.
  await dialog.getByLabel('Username').fill(OPERATOR.username.toUpperCase());
  await dialog.getByLabel('Password', { exact: true }).fill(OPERATOR.password);
  await dialog.getByLabel('Confirm password').fill(OPERATOR.password);
  await dialog.getByRole('button', { name: /Create account/ }).click();

  await expect(dialog.getByText(/already taken/i)).toBeVisible();
  await expect(dialog).toBeVisible();
});

// ---------------------------------------------------------------------------
// 5–9. Sign in as the operator; administrative surfaces are unavailable
// ---------------------------------------------------------------------------

test('the operator can sign in with the password it was given', async ({ page }) => {
  await signInAndWait(page);
  await signOut(page);

  await signIn(page, OPERATOR.username, OPERATOR.password);

  await expect(page.locator('#main-content')).toBeVisible();
  await expect(page.getByRole('button', { name: new RegExp(OPERATOR.username) })).toBeVisible();
});

test('the operator is offered no administrative sections', async ({ page }) => {
  await signInAndWait(page, OPERATOR.username, OPERATOR.password);
  await page.goto('/settings');

  const nav = page.getByRole('navigation', { name: 'Settings sections' });
  await expect(nav.getByRole('link', { name: /My account/ })).toBeVisible();
  await expect(nav.getByRole('link', { name: /Users/ })).toHaveCount(0);
  await expect(nav.getByRole('link', { name: /Audit/ })).toHaveCount(0);
});

test('the operator typing the URL gets an explanation, not data', async ({ page }) => {
  // Hiding the link is presentation. This is what happens when presentation
  // is bypassed.
  await signInAndWait(page, OPERATOR.username, OPERATOR.password);
  await page.goto('/settings/users');

  await expect(page.getByText(/requires the Administrator role/i)).toBeVisible();
  await expect(page.getByRole('table', { name: /user accounts/i })).toHaveCount(0);
});

test('every administrative endpoint answers 403 to the operator', async ({ page }) => {
  // The boundary, asserted from inside the browser with the real cookies —
  // so this is what the operator's own session is actually permitted to do.
  await signInAndWait(page, OPERATOR.username, OPERATOR.password);

  const csrf = (await page.context().cookies()).find((c) => c.name === 'hcc_csrf')?.value;
  expect(csrf).toBeTruthy();
  const headers = { 'x-csrf-token': csrf! };

  const refusals = [
    await apiFromPage(page, '/api/v1/users'),
    await apiFromPage(page, '/api/v1/audit/events'),
    // The release manifest describes what to download and verify onto a host.
    // Reading it is not a read of monitoring data, so it sits with the
    // administrative endpoints rather than with the dashboard's.
    await apiFromPage(page, '/api/v1/agent-releases'),
    await apiFromPage(page, '/api/v1/users', {
      method: 'POST',
      headers,
      body: { username: 'escalated', password: 'a long enough password' },
    }),
    await apiFromPage(page, `/api/v1/users/${'0'.repeat(8)}-0000-4000-8000-000000000000`, {
      method: 'PATCH',
      headers,
      body: { role: 'admin' },
    }),
  ];

  for (const response of refusals) {
    expect(response.status).toBe(403);
    expect((response.body as { error: { code: string } }).error.code).toBe(
      'insufficient_permissions',
    );
  }
});

test('the operator cannot promote itself', async ({ page }) => {
  // The privilege-escalation attempt, explicitly: a valid session plus a valid
  // CSRF token is still not an administrator.
  await signInAndWait(page, OPERATOR.username, OPERATOR.password);

  const own = await apiFromPage(page, '/api/v1/account');
  const userId = (own.body as { user_id: string }).user_id;
  const csrf = (await page.context().cookies()).find((c) => c.name === 'hcc_csrf')?.value;

  const response = await apiFromPage(page, `/api/v1/users/${userId}`, {
    method: 'PATCH',
    headers: { 'x-csrf-token': csrf! },
    body: { role: 'admin' },
  });

  expect(response.status).toBe(403);
  // And the role really is unchanged.
  const after = await apiFromPage(page, '/api/v1/account');
  expect((after.body as { role: string }).role).toBe('operator');
});

test('the operator may still manage its own account', async ({ page }) => {
  // The other half of the role boundary: self-service is not administrative.
  await signInAndWait(page, OPERATOR.username, OPERATOR.password);
  await page.goto('/settings/account');

  await expect(page.getByRole('heading', { name: 'Your account' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Change password' })).toBeVisible();

  await page.goto('/settings/sessions');
  await expect(page.getByText('This session')).toBeVisible();
});

// ---------------------------------------------------------------------------
// 10–12. Promote the operator, and watch the change take effect
// ---------------------------------------------------------------------------

test('an administrator promotes the operator', async ({ page }) => {
  await signInAndWait(page, OPERATOR.username, OPERATOR.password);
  await signOut(page, OPERATOR.username);
  await signIn(page, ADMIN_USERNAME, ADMIN_PASSWORD);
  // Wait for the console before navigating. Without it the `goto` below races
  // the login response, so the next page load is still unauthenticated and
  // lands on the login screen instead.
  await expect(page.locator('#main-content')).toBeVisible();
  await openUsers(page);

  await page.getByRole('button', { name: `Edit ${OPERATOR.username}` }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('Role').selectOption('admin');
  await expect(dialog.getByText(/Administrators can manage every account/i)).toBeVisible();
  await dialog.getByRole('button', { name: /Save changes/ }).click();

  await expect(dialog).toBeHidden();
  await expect(userRow(page, OPERATOR.username)).toContainText('Administrator');
});

test('the promotion takes effect on an existing session, with no new login', async ({
  browser,
  baseURL,
  page,
}) => {
  /*
   * The scenario the brief names, run with two real browsers.
   *
   * The promoted account's session was established *before* the promotion.
   * The backend reads the role from the user row on every request rather than
   * caching it at login (ADR-028), so that session gains the new permissions
   * without signing in again — and the interface notices.
   */
  const other = await signedInContext(browser, baseURL!, OPERATOR.username, OPERATOR.password);
  try {
    // Already an administrator by now, so this works immediately.
    await other.page.goto('/settings/users');
    await expect(other.page.getByRole('table', { name: /user accounts/i })).toBeVisible();

    // And demoting them again is visible to that same session.
    await signInAndWait(page, ADMIN_USERNAME, ADMIN_PASSWORD);
    await openUsers(page);
    await page.getByRole('button', { name: `Edit ${OPERATOR.username}` }).click();
    const dialog = page.getByRole('dialog');
    await dialog.getByLabel('Role').selectOption('operator');
    await dialog.getByRole('button', { name: /Save changes/ }).click();
    await expect(dialog).toBeHidden();

    // The other browser's next administrative request is refused, and its
    // interface stops offering the section.
    await other.page.reload();
    await expect(other.page.getByText(/requires the Administrator role/i)).toBeVisible();

    // Restored, because later steps need this account to be an administrator.
    await page.getByRole('button', { name: `Edit ${OPERATOR.username}` }).click();
    const again = page.getByRole('dialog');
    await again.getByLabel('Role').selectOption('admin');
    await again.getByRole('button', { name: /Save changes/ }).click();
    await expect(again).toBeHidden();
  } finally {
    await other.close();
  }
});

// ---------------------------------------------------------------------------
// 13–14. Create a viewer, and check its restrictions
// ---------------------------------------------------------------------------

test('an administrator creates a viewer', async ({ page }) => {
  await signInAndWait(page);
  await openUsers(page);

  await createAccount(page, { ...VIEWER, role: 'viewer' });

  await expect(userRow(page, VIEWER.username)).toContainText('Viewer');
});

test('the viewer is refused every administrative surface', async ({ page }) => {
  await signInAndWait(page, VIEWER.username, VIEWER.password);

  await page.goto('/settings/audit');
  await expect(page.getByText(/requires the Administrator role/i)).toBeVisible();

  const listing = await apiFromPage(page, '/api/v1/users');
  expect(listing.status).toBe(403);
  const events = await apiFromPage(page, '/api/v1/audit/events');
  expect(events.status).toBe(403);
  const releases = await apiFromPage(page, '/api/v1/agent-releases');
  expect(releases.status).toBe(403);

  // And the viewer is not signed out by being refused.
  await expect(page.locator('#main-content')).toBeVisible();
});

test('the default role for a new account is viewer', async ({ page }) => {
  // Least privilege, in the shipped bundle rather than only in the schema.
  await signInAndWait(page);
  await openUsers(page);
  await page.getByRole('button', { name: /New account/ }).click();

  await expect(page.getByRole('dialog').getByLabel('Role')).toHaveValue('viewer');
  await page.keyboard.press('Escape');
});

// ---------------------------------------------------------------------------
// 15–18. Deactivate the viewer, then reactivate it
// ---------------------------------------------------------------------------

test('an administrator deactivates the viewer', async ({ page }) => {
  await signInAndWait(page);
  await openUsers(page);

  await page.getByRole('button', { name: `Deactivate ${VIEWER.username}` }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByText(/Access ends immediately/i)).toBeVisible();
  await dialog.getByRole('button', { name: 'Deactivate account' }).click();

  await expect(dialog).toBeHidden();
  await expect(userRow(page, VIEWER.username)).toContainText('Inactive');
});

test('the deactivated viewer cannot authenticate', async ({ page }) => {
  await page.goto('/login');
  await signIn(page, VIEWER.username, VIEWER.password);

  await expect(page.getByText('Invalid username or password.')).toBeVisible();
  // The same message a wrong password gets: deactivation must not be
  // discoverable at the login endpoint.
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
});

test('an administrator reactivates the viewer', async ({ page }) => {
  await signInAndWait(page);
  await openUsers(page);

  await page.getByRole('button', { name: `Reactivate ${VIEWER.username}` }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByText(/Old sessions stay revoked/i)).toBeVisible();
  await dialog.getByRole('button', { name: 'Reactivate account' }).click();

  await expect(dialog).toBeHidden();
  await expect(userRow(page, VIEWER.username)).toContainText('Active');
});

test('the reactivated viewer can authenticate again', async ({ page }) => {
  await page.goto('/login');
  await signIn(page, VIEWER.username, VIEWER.password);

  await expect(page.locator('#main-content')).toBeVisible();
});

// ---------------------------------------------------------------------------
// 19–20. Revoke another user's sessions, and watch them stop working
// ---------------------------------------------------------------------------

test("an administrator revokes another account's sessions", async ({
  browser,
  baseURL,
  page,
}) => {
  /*
   * Two browsers, because that is the only way to assert the thing that
   * matters: a revocation performed here ends a session held *there*. A
   * single-context test can only observe its own cookie being cleared, which
   * is not the security property.
   */
  const victim = await signedInContext(browser, baseURL!, VIEWER.username, VIEWER.password);
  try {
    await expect(victim.page.locator('#main-content')).toBeVisible();

    await signInAndWait(page);
    await openUsers(page);

    /*
     * Read the count the administrative view reports rather than asserting a
     * number. Earlier stations on this journey signed this account in more
     * than once, so a hard-coded "1" would be asserting the journey's history
     * instead of the behaviour — and it did, until a step was added before
     * this one. What has to hold is that the view and the revocation agree.
     */
    const sessions = await userRow(page, VIEWER.username)
      .getByText(/\d+ active sessions?/)
      .innerText();
    const live = Number(/(\d+)/.exec(sessions)?.[1] ?? '0');
    expect(live).toBeGreaterThanOrEqual(1);

    await page.getByRole('button', { name: `Revoke sessions for ${VIEWER.username}` }).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog.getByText(/signed out immediately/i)).toBeVisible();
    await dialog.getByRole('button', { name: 'Revoke sessions' }).click();

    // The count is reported, not assumed — and it is the one the table showed.
    await expect(
      page.getByText(
        new RegExp(`Revoked ${live} session${live === 1 ? '' : 's'} for ${VIEWER.username}`),
      ),
    ).toBeVisible();
    // And the row now shows none.
    await expect(
      userRow(page, VIEWER.username).getByTitle('No active sessions'),
    ).toBeVisible();

    // And the other browser's session really is dead: its next request is
    // refused and it lands on the login page with an explanation.
    const refused = await apiFromPage(victim.page, '/api/v1/auth/me');
    expect(refused.status).toBe(401);

    await victim.page.reload();
    await expect(victim.page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
  } finally {
    await victim.close();
  }
});

test('the account may sign in again after a revocation', async ({ page }) => {
  // The difference from deactivation, in the browser.
  await page.goto('/login');
  await signIn(page, VIEWER.username, VIEWER.password);

  await expect(page.locator('#main-content')).toBeVisible();
});

test('a user can end one of its own sessions from another', async ({
  browser,
  baseURL,
  page,
}) => {
  await signInAndWait(page, VIEWER.username, VIEWER.password);
  await page.goto('/settings/sessions');

  /*
   * Start from a known state.
   *
   * Earlier stations left this account signed in more than once, so the list
   * begins with an arbitrary number of other sessions — and the property worth
   * asserting is that revoking *one named* session ends *that* session, which
   * needs exactly one other to point at. "Sign out elsewhere" clears the rest
   * first, which is itself part of the feature.
   */
  // Wait for the list before counting: `count()` does not auto-wait, so on an
  // unloaded page it answers zero and the clearing step below is skipped —
  // which is precisely what happened, and then failed one assertion later.
  await expect(page.getByText('This session')).toBeVisible();

  const elsewhere = page.getByRole('button', { name: /Sign out elsewhere/ });
  if (await elsewhere.isEnabled()) {
    await elsewhere.click();
    const clearing = page.getByRole('dialog');
    await clearing.getByRole('button', { name: 'Sign out elsewhere' }).click();
    await expect(clearing).toBeHidden();
  }
  await expect(page.getByText('Other session')).toHaveCount(0);
  await expect(page.getByText('This session')).toBeVisible();

  const other = await signedInContext(browser, baseURL!, VIEWER.username, VIEWER.password);
  try {
    await page.reload();
    // Exactly two now: this one, and the other context's.
    await expect(page.getByText('Other session')).toHaveCount(1);

    await page.getByRole('button', { name: /Revoke the session signed in/ }).click();
    const dialog = page.getByRole('dialog');
    await dialog.getByRole('button', { name: 'Revoke session' }).click();
    await expect(dialog).toBeHidden();

    await expect(page.getByText('Other session')).toHaveCount(0);
    // And it is the *other* browser that was ended, not this one.
    await expect(page.getByText('This session')).toBeVisible();
    expect((await apiFromPage(page, '/api/v1/auth/me')).status).toBe(200);
    expect((await apiFromPage(other.page, '/api/v1/auth/me')).status).toBe(401);
  } finally {
    await other.close();
  }
});

// ---------------------------------------------------------------------------
// 21–23. Change an account's own password
// ---------------------------------------------------------------------------

test('an account changes its own password, and stays signed in', async ({
  browser,
  baseURL,
  page,
}) => {
  /*
   * ADR-040 in a real browser, which is the only place the cookie half can be
   * checked: every other session ends, and *this* one is replaced — so the
   * browser stays signed in on material it did not have a moment ago.
   */
  const elsewhere = await signedInContext(
    browser,
    baseURL!,
    OPERATOR.username,
    OPERATOR.password,
  );
  try {
    await signInAndWait(page, OPERATOR.username, OPERATOR.password);
    const cookieBefore = (await page.context().cookies()).find(
      (cookie) => cookie.name === 'hcc_session',
    )?.value;
    expect(cookieBefore).toBeTruthy();

    await page.goto('/settings/account');
    await page.getByLabel('Current password').fill(OPERATOR.password);
    await page.getByLabel('New password', { exact: true }).fill(OPERATOR_NEW_PASSWORD);
    await page.getByLabel('Confirm new password').fill(OPERATOR_NEW_PASSWORD);
    await page.getByRole('button', { name: 'Change password' }).click();

    await expect(page.getByText('Password changed')).toBeVisible();
    // The count, whatever it is: earlier stations left this account signed in
    // several times, and the number is the server's to report.
    await expect(page.getByText(/Signed out of \d+ other sessions?\./)).toBeVisible();

    // Still signed in here, on a different token.
    const cookieAfter = (await page.context().cookies()).find(
      (cookie) => cookie.name === 'hcc_session',
    )?.value;
    expect(cookieAfter).toBeTruthy();
    expect(cookieAfter).not.toBe(cookieBefore);
    expect((await apiFromPage(page, '/api/v1/auth/me')).status).toBe(200);

    // And the other browser is out.
    expect((await apiFromPage(elsewhere.page, '/api/v1/auth/me')).status).toBe(401);
  } finally {
    await elsewhere.close();
  }
});

test('a wrong current password is refused without signing the user out', async ({ page }) => {
  // The server answers 422 rather than 401 precisely so a typo does not eject
  // somebody from the application.
  await signInAndWait(page, OPERATOR.username, OPERATOR_NEW_PASSWORD);
  await page.goto('/settings/account');

  await page.getByLabel('Current password').fill('not the password');
  await page.getByLabel('New password', { exact: true }).fill('another long enough passphrase');
  await page.getByLabel('Confirm new password').fill('another long enough passphrase');
  await page.getByRole('button', { name: 'Change password' }).click();

  await expect(page.getByText('The current password is incorrect.')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Your account' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Sign in' })).toHaveCount(0);
});

test('the old password no longer works and the new one does', async ({ page }) => {
  await page.goto('/login');
  await signIn(page, OPERATOR.username, OPERATOR.password);
  await expect(page.getByText('Invalid username or password.')).toBeVisible();

  await page.getByLabel('Password', { exact: true }).fill(OPERATOR_NEW_PASSWORD);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.locator('#main-content')).toBeVisible();
});

// ---------------------------------------------------------------------------
// 24. The audit trail
// ---------------------------------------------------------------------------

test('the audit trail records the journey', async ({ page }) => {
  await signInAndWait(page);
  await page.goto('/settings/audit');

  const table = page.getByRole('table', { name: /audit events/i });
  await expect(table).toBeVisible();

  // The events this journey produced, phrased for a reader.
  for (const phrase of [
    'Account created',
    'Role changed',
    'Account deactivated',
    'Account reactivated',
    'Sessions revoked by an administrator',
  ]) {
    await expect(table.getByText(phrase).first()).toBeVisible();
  }
});

test('a denied administrative attempt was recorded', async ({ page }) => {
  // The operator and viewer were refused `/api/v1/users` earlier. That is a
  // security signal, and the point of recording a denial is that it survives
  // the request's own rollback.
  await signInAndWait(page);
  await page.goto('/settings/audit');

  await page.getByLabel('Filter by action').selectOption('admin.access_denied');

  const table = page.getByRole('table', { name: /audit events/i });
  await expect(table.getByText('Administrative access denied').first()).toBeVisible();
  await expect(table.getByText('denied').first()).toBeVisible();
});

test('no audit event carries credential material', () => {
  /*
   * Read from the database rather than from the API, so this covers what was
   * *stored* rather than what a response chose to show. A filtered response
   * over a table containing secrets would be the more dangerous arrangement.
   */
  const rows = sql(
    "SELECT coalesce(string_agg(details::text, ' '), '') AS blob FROM audit_events",
  ) as { blob: string }[];
  const stored = rows[0]?.blob ?? '';

  expect(stored).not.toContain(ADMIN_PASSWORD);
  expect(stored).not.toContain(OPERATOR.password);
  expect(stored).not.toContain(OPERATOR_NEW_PASSWORD);
  expect(stored).not.toContain(VIEWER.password);
  expect(stored).not.toContain('$argon2');
  for (const forbidden of ['password_hash', 'token_hash', 'csrf']) {
    expect(stored.toLowerCase()).not.toContain(forbidden);
  }
});

test('the audit trail has no endpoint that writes to it', async ({ page }) => {
  // Append-only is a property of there being no route, not of a check.
  await signInAndWait(page);
  const csrf = (await page.context().cookies()).find((c) => c.name === 'hcc_csrf')?.value;

  for (const method of ['POST', 'PUT', 'PATCH', 'DELETE']) {
    const response = await apiFromPage(page, '/api/v1/audit/events', {
      method,
      headers: { 'x-csrf-token': csrf! },
      body: {},
    });
    expect(response.status).toBe(405);
  }
});

// ---------------------------------------------------------------------------
// 25. The last-administrator protection
// ---------------------------------------------------------------------------

test('the last active administrator cannot be demoted', async ({ page }) => {
  /*
   * Reduced to one administrator first, so the guard is actually under test —
   * asserting it while two exist would pass for the wrong reason.
   */
  await signInAndWait(page);
  await openUsers(page);

  // Demote the promoted account back to operator, leaving one administrator.
  await page.getByRole('button', { name: `Edit ${OPERATOR.username}` }).click();
  const demote = page.getByRole('dialog');
  await demote.getByLabel('Role').selectOption('operator');
  await demote.getByRole('button', { name: /Save changes/ }).click();
  await expect(demote).toBeHidden();
  await expect(userRow(page, OPERATOR.username)).toContainText('Operator');

  // Now the only one left refuses to be demoted.
  await page.getByRole('button', { name: `Edit ${ADMIN_USERNAME}` }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('Role').selectOption('viewer');
  await dialog.getByRole('button', { name: /Save changes/ }).click();

  await expect(dialog.getByText(/only active administrator/i)).toBeVisible();
  await expect(
    dialog.getByText(/Grant the Administrator role to another active account/i),
  ).toBeVisible();

  // Nothing changed.
  await page.keyboard.press('Escape');
  await expect(userRow(page, ADMIN_USERNAME)).toContainText('Administrator');
});

test('the last active administrator cannot be deactivated', async ({ page }) => {
  await signInAndWait(page);
  await openUsers(page);

  await page.getByRole('button', { name: `Deactivate ${ADMIN_USERNAME}` }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('button', { name: 'Deactivate account' }).click();

  // The alert's heading and the server's message both contain the phrase, so
  // the assertion is on the remedy — which is the part an operator acts on.
  await expect(
    dialog.getByText(/Grant the Administrator role to another active account/i),
  ).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(userRow(page, ADMIN_USERNAME)).toContainText('Active');
});

test('the refusal is enforced by the control plane, not by the interface', async ({ page }) => {
  // The interface explains it; the server decides it. A direct API call with a
  // valid session and a valid CSRF token is refused identically.
  await signInAndWait(page);
  const own = await apiFromPage(page, '/api/v1/account');
  const userId = (own.body as { user_id: string }).user_id;
  const csrf = (await page.context().cookies()).find((c) => c.name === 'hcc_csrf')?.value;

  const response = await apiFromPage(page, `/api/v1/users/${userId}`, {
    method: 'PATCH',
    headers: { 'x-csrf-token': csrf! },
    body: { role: 'viewer' },
  });

  expect(response.status).toBe(409);
  expect((response.body as { error: { code: string } }).error.code).toBe('last_administrator');
});

test('the installation is still initialised, and bootstrap has not reopened', async ({
  page,
}) => {
  // User management must not be able to reopen the one-way door (ADR-033).
  //
  // Navigated first: `apiFromPage` resolves a relative URL against the page's
  // own origin, and a fresh context starts on `about:blank` where there is
  // none. Deliberately *not* signed in — this is a public endpoint and the
  // answer must not depend on a session.
  await page.goto('/login');
  const status = await apiFromPage(page, '/api/v1/setup/status');

  expect(status.body).toEqual({ initialized: true });
});

// ---------------------------------------------------------------------------
// Viewport coverage
// ---------------------------------------------------------------------------

test.describe('user management across viewports', () => {
  for (const viewport of [
    { name: '1920x1080 desktop', width: 1920, height: 1080 },
    { name: '768x1024 tablet portrait', width: 768, height: 1024 },
    { name: '390x844 phone', width: 390, height: 844 },
  ]) {
    test(`the account table is usable at ${viewport.name}`, async ({ page }) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await signInAndWait(page);
      await openUsers(page);

      // Reachable, and the primary actions are present at every size.
      await expect(userRow(page, ADMIN_USERNAME)).toBeVisible();
      await expect(page.getByRole('button', { name: /New account/ })).toBeVisible();
      await expect(
        page.getByRole('button', { name: `Edit ${ADMIN_USERNAME}` }),
      ).toBeVisible();

      // The page itself must not scroll sideways; the table may, inside its
      // own wrapper, which is the intended behaviour for a wide table on a
      // narrow screen.
      const overflow = await page.evaluate(() => {
        const root = document.documentElement;
        return Math.max(0, root.scrollWidth - root.clientWidth);
      });
      expect(overflow).toBeLessThanOrEqual(1);
    });
  }
});
