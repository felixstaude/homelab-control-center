/**
 * Security properties of the live authentication flow.
 *
 * The component and backend suites already pin these one layer at a time.
 * What a real browser against a real control plane adds is the part neither
 * can see: whether the cookie attributes, the CORS configuration, the proxy
 * and the client's own logic combine into the behaviour each of them assumes.
 *
 * Several tests here deliberately attempt an attack and assert that it fails.
 * A test that only exercises the happy path cannot tell a working defence from
 * an absent one.
 */

import { expect, test } from '@playwright/test';

import {
  ADMIN_PASSWORD,
  ADMIN_USERNAME,
  BACKEND_URL,
  apiFromPage,
  cookie,
  ensureAdministrator,
  openAccountMenu,
  signIn,
  signInAndWait,
} from './helpers';

test.describe.configure({ mode: 'serial' });

// Runnable on its own: in a full run `auth-journey.spec.ts` has already
// bootstrapped and this exits 3, which is tolerated.
test.beforeAll(() => {
  ensureAdministrator();
});

const EVIL_ORIGIN = 'http://evil.example';

// ---------------------------------------------------------------------------
// Session fixation
// ---------------------------------------------------------------------------

test('a pre-set session cookie is replaced, not adopted', async ({ page, context }) => {
  // Session fixation: the attacker plants a session identifier they know, then
  // waits for the victim to authenticate it. The defence is that the server
  // issues its own identifier at login and never promotes a supplied one.
  const planted = 'a'.repeat(43);
  await context.addCookies([
    {
      name: 'hcc_session',
      value: planted,
      domain: '127.0.0.1',
      path: '/',
      httpOnly: true,
      sameSite: 'Lax',
    },
  ]);

  await page.goto('/login');
  // The planted value is not a session, so the client must show the login
  // page rather than treat it as one.
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();

  await signIn(page);
  await expect(page.getByRole('navigation', { name: 'Primary' })).toBeVisible();

  const session = await cookie(page, 'hcc_session');
  expect(session).toBeDefined();
  expect(session!.value).not.toBe(planted);
  expect(session!.httpOnly).toBe(true);
});

test('the session identifier changes on every sign-in', async ({ page }) => {
  // Even without a planted value: signing out and back in must not reuse the
  // previous identifier, or a captured cookie would stay useful.
  await signInAndWait(page);
  const first = (await cookie(page, 'hcc_session'))!.value;
  const firstCsrf = (await cookie(page, 'hcc_csrf'))!.value;

  await openAccountMenu(page);
  await page.getByRole('button', { name: 'Sign out' }).click();
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();

  await signIn(page);
  await expect(page.getByRole('navigation', { name: 'Primary' })).toBeVisible();
  const second = (await cookie(page, 'hcc_session'))!.value;
  const secondCsrf = (await cookie(page, 'hcc_csrf'))!.value;

  expect(second).not.toBe(first);
  // The CSRF token is session-bound, so it has to rotate with the session —
  // a stale token paired with a fresh session would fail every mutation.
  expect(secondCsrf).not.toBe(firstCsrf);
});

test('a cross-site login cannot establish a session', async ({ page, request }) => {
  // Login CSRF, which is session fixation by another route: force the victim's
  // browser into a session the attacker controls. Two things block it, and
  // both are worth pinning because either could be relaxed by accident.
  //
  // 1. The endpoint takes JSON, so a cross-origin HTML form — the only POST
  //    that needs no preflight — cannot produce a body it accepts.
  const formPost = await request.post(`${BACKEND_URL}/api/v1/auth/login`, {
    headers: { origin: EVIL_ORIGIN, 'content-type': 'application/x-www-form-urlencoded' },
    form: { username: ADMIN_USERNAME, password: ADMIN_PASSWORD },
  });
  expect(formPost.status()).toBe(422);
  expect(formPost.headers()['set-cookie']).toBeUndefined();

  // 2. A JSON request needs a CORS preflight, and a foreign origin is not
  //    granted one — so the browser never sends the request at all.
  //
  //    This has to run from a genuinely foreign document. An earlier version
  //    of this test ran the fetch from the application's own page, which is an
  //    allowed origin by configuration: it passed CORS exactly as it should
  //    and proved nothing. Serving an attacker page from `evil.example`
  //    through route interception is what makes the origin real.
  await page.route('http://evil.example/**', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'text/html',
      body: '<!doctype html><title>attacker</title><body></body>',
    }),
  );
  await page.goto('http://evil.example/');
  expect(new URL(page.url()).origin).toBe('http://evil.example');

  const blocked = await page.evaluate(
    async ([target, body]) => {
      try {
        const response = await fetch(target, {
          method: 'POST',
          mode: 'cors',
          credentials: 'include',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(body),
        });
        return { blocked: false, status: response.status };
      } catch {
        // A CORS refusal is indistinguishable from a network error to script,
        // which is the point: the attacker learns nothing either way.
        return { blocked: true, status: 0 };
      }
    },
    [
      `${BACKEND_URL}/api/v1/auth/login`,
      { username: ADMIN_USERNAME, password: ADMIN_PASSWORD },
    ] as const,
  );
  expect(blocked.blocked, 'a foreign origin was allowed to post credentials').toBe(true);

  // And no session cookie was planted in the browser by the attempt.
  const cookies = await page.context().cookies();
  expect(cookies.find((candidate) => candidate.name === 'hcc_session')).toBeUndefined();
});

// ---------------------------------------------------------------------------
// CSRF and Origin
// ---------------------------------------------------------------------------

test('a mutation without a CSRF token is refused', async ({ page }) => {
  await signInAndWait(page);

  const response = await apiFromPage(page, '/api/v1/auth/logout', { method: 'POST' });
  expect(response.status).toBe(403);
  expect(response.body).toMatchObject({ error: { code: 'csrf_validation_failed' } });

  // Refused, and nothing changed: the session is still usable.
  expect((await apiFromPage(page, '/api/v1/auth/me')).status).toBe(200);
});

test('a mutation with another session’s CSRF token is refused', async ({ browser }) => {
  // The token is session-bound, so one user's valid token must be useless in
  // another user's session. A token checked only for well-formedness, or
  // compared against a global secret, would pass this and should not.
  const firstContext = await browser.newContext();
  const first = await firstContext.newPage();
  await signInAndWait(first);
  const borrowed = (await cookie(first, 'hcc_csrf'))!.value;

  const secondContext = await browser.newContext();
  const second = await secondContext.newPage();
  await signInAndWait(second);
  const own = (await cookie(second, 'hcc_csrf'))!.value;
  expect(borrowed).not.toBe(own);

  const response = await apiFromPage(second, '/api/v1/auth/logout', {
    method: 'POST',
    headers: { 'x-csrf-token': borrowed },
  });
  expect(response.status).toBe(403);
  expect((await apiFromPage(second, '/api/v1/auth/me')).status).toBe(200);

  await firstContext.close();
  await secondContext.close();
});

test('a mutation from a foreign Origin is refused even with the right token', async ({
  page,
  request,
}) => {
  // The cookie and the token are both correct; only the Origin is wrong. This
  // is the case the Origin check exists for, and the one a double-submit
  // token alone cannot catch if the attacker can read the readable cookie.
  await signInAndWait(page);
  const session = (await cookie(page, 'hcc_session'))!.value;
  const csrf = (await cookie(page, 'hcc_csrf'))!.value;

  for (const origin of [
    EVIL_ORIGIN,
    'https://127.0.0.1:4173', // right host and port, wrong scheme
    'http://127.0.0.1:4174', // right host and scheme, wrong port
    'http://127.0.0.1.evil.example', // a prefix match that is a different host
    'null',
  ]) {
    const response = await request.post(`${BACKEND_URL}/api/v1/auth/logout`, {
      headers: {
        origin,
        'x-csrf-token': csrf,
        cookie: `hcc_session=${session}; hcc_csrf=${csrf}`,
      },
    });
    expect(response.status(), `Origin ${origin} should be refused`).toBe(403);
    const body = (await response.json()) as { error: { code: string } };
    expect(body.error.code).toBe('csrf_validation_failed');
  }

  // And the session survived every refusal.
  expect((await apiFromPage(page, '/api/v1/auth/me')).status).toBe(200);
});

test('a foreign origin gets no CORS grant', async ({ request }) => {
  // Even on a safe method: a reflected Access-Control-Allow-Origin with
  // credentials would let any site read authenticated responses.
  const response = await request.get(`${BACKEND_URL}/api/v1/info`, {
    headers: { origin: EVIL_ORIGIN },
  });
  const allowOrigin = response.headers()['access-control-allow-origin'];
  expect(allowOrigin).not.toBe(EVIL_ORIGIN);
  expect(allowOrigin).not.toBe('*');

  // The preflight must refuse it too, so the real request is never sent.
  const preflight = await request.fetch(`${BACKEND_URL}/api/v1/auth/logout`, {
    method: 'OPTIONS',
    headers: {
      origin: EVIL_ORIGIN,
      'access-control-request-method': 'POST',
      'access-control-request-headers': 'x-csrf-token',
    },
  });
  expect(preflight.headers()['access-control-allow-origin']).not.toBe(EVIL_ORIGIN);
});

// ---------------------------------------------------------------------------
// Redirects
// ---------------------------------------------------------------------------

test('no crafted destination can redirect off-origin after sign-in', async ({ page }) => {
  const origin = new URL(page.url() === 'about:blank' ? 'http://127.0.0.1:4173' : page.url())
    .origin;

  for (const attempt of [
    '/login?next=https://evil.example',
    '/login?next=//evil.example',
    '/login?redirect=https://evil.example',
    '/login?returnTo=/%2F%2Fevil.example',
    '/login#https://evil.example',
  ]) {
    await page.goto(attempt);
    await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
    await signIn(page);
    await expect(page.getByRole('navigation', { name: 'Primary' })).toBeVisible();

    // Wherever it landed, it is on this origin — which is the property that
    // matters. The client ignores these parameters entirely; this asserts
    // that an implementation which started honouring one could not send a
    // user somewhere else.
    expect(new URL(page.url()).origin, `${attempt} redirected off-origin`).toBe(origin);

    await openAccountMenu(page);
    await page.getByRole('button', { name: 'Sign out' }).click();
    await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
  }
});

test('a path that looks like an origin stays a path', async ({ page }) => {
  // The dangerous shapes themselves are unit-tested against
  // `safeRedirectPath`. What matters here is the end-to-end consequence: a
  // request for something shaped like an origin must be handled as one of our
  // own routes and must never move the browser off this origin.
  //
  // Note that a bare `//evil.example` cannot be used for this: the browser
  // resolves it as a protocol-relative URL before the application is involved
  // at all, so navigating to it would test the browser rather than the router.
  await page.goto('/login');
  const origin = new URL(page.url()).origin;

  for (const path of ['/%2F%2Fevil.example', '/..%2F..%2Fevil.example', '/login%2F%2Fevil']) {
    await page.goto(path);
    expect(new URL(page.url()).origin, `${path} left the origin`).toBe(origin);
    // And it resolved to one of our own screens rather than anything fetched
    // from elsewhere.
    await expect(page.locator('#root')).not.toBeEmpty();
  }

  // Signing in from there still lands on our own console.
  await page.goto('/login');
  await signIn(page);
  await expect(page.locator('#main-content')).toBeVisible();
  expect(new URL(page.url()).origin).toBe(origin);
});

// ---------------------------------------------------------------------------
// Caches
// ---------------------------------------------------------------------------

test('the back button after sign-out shows no authenticated content', async ({ page }) => {
  await signInAndWait(page);
  await page.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name: 'Hosts' }).click();
  await expect(page.getByRole('heading', { level: 2, name: 'Hosts' })).toBeVisible();

  await openAccountMenu(page);
  await page.getByRole('button', { name: 'Sign out' }).click();
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();

  await page.goBack();
  // A page restored from the back/forward cache with the previous session's
  // content still on it is the classic shared-computer disclosure.
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
  await expect(page.getByText(ADMIN_USERNAME)).toHaveCount(0);
});

test('authenticated API responses are not cacheable', async ({ page }) => {
  await signInAndWait(page);
  const headers = await page.evaluate(async () => {
    const response = await fetch('/api/v1/auth/me', { credentials: 'same-origin' });
    return {
      cacheControl: response.headers.get('cache-control'),
      status: response.status,
    };
  });
  expect(headers.status).toBe(200);
  // A shared proxy storing this would serve one operator's identity to
  // another.
  expect(headers.cacheControl ?? '').toMatch(/no-store|no-cache|private/);
});

// ---------------------------------------------------------------------------
// Disclosure
// ---------------------------------------------------------------------------

test('a failed sign-in reveals nothing about which part was wrong', async ({ page }) => {
  await page.goto('/login');

  const message = async (username: string, password: string): Promise<string> => {
    const response = await apiFromPage(page, '/api/v1/auth/login', {
      method: 'POST',
      body: { username, password },
    });
    expect(response.status).toBe(401);
    const body = response.body as { error: { code: string; message: string } };
    expect(body.error.code).toBe('invalid_credentials');
    return body.error.message;
  };

  const unknownUser = await message('NoSuchOperatorAtAll', 'whatever this is');
  const wrongPassword = await message(ADMIN_USERNAME, 'definitely not the password');

  // Byte-identical, including punctuation: a difference of any kind is an
  // enumeration oracle.
  expect(unknownUser).toBe(wrongPassword);
  expect(unknownUser).not.toContain(ADMIN_USERNAME);
});

test('an unknown username still pays the password-verification cost', async ({ page }) => {
  // Timing is the other half of the oracle. The server verifies a dummy hash
  // for an unknown user precisely so the two paths cost the same; if that were
  // removed, the unknown-user path would return in a fraction of the time.
  //
  // The assertion is deliberately loose — this measures a real network round
  // trip on shared CI hardware, and a tight bound would fail for reasons that
  // have nothing to do with the property. What it catches is the order-of-
  // magnitude difference that skipping Argon2 entirely would produce.
  await page.goto('/login');

  const timed = async (username: string): Promise<number> => {
    const started = Date.now();
    await apiFromPage(page, '/api/v1/auth/login', {
      method: 'POST',
      body: { username, password: 'definitely not the password' },
    });
    return Date.now() - started;
  };

  // Warm the path so neither measurement pays a one-off import or pool cost.
  await timed('WarmUpOnly');

  const unknown = await timed('NoSuchOperatorAtAll');
  const known = await timed(ADMIN_USERNAME);

  expect(
    unknown,
    `unknown-user login took ${unknown}ms against ${known}ms for a known user`,
  ).toBeGreaterThan(known * 0.25);
});

test('the health probes disclose nothing about the deployment', async ({ request }) => {
  for (const path of ['/healthz', '/readyz']) {
    const response = await request.get(`${BACKEND_URL}${path}`);
    expect(response.status()).toBe(200);
    const body = await response.text();
    // No version, no environment name, no dependency names, no hostname.
    expect(body).not.toMatch(/version|environment|postgres|asyncpg|sqlalchemy|python/i);
  }
});

test('API responses carry no server banner', async ({ request }) => {
  const response = await request.get(`${BACKEND_URL}/healthz`);
  const server = response.headers().server ?? '';
  // uvicorn's default banner names the server and is of no use to a client.
  expect(server).not.toMatch(/uvicorn|python/i);
});

// ---------------------------------------------------------------------------
// Cookie handling
// ---------------------------------------------------------------------------

test('the cookie pair has exactly the attributes the design depends on', async ({ page }) => {
  await signInAndWait(page);

  const session = await cookie(page, 'hcc_session');
  const csrf = await cookie(page, 'hcc_csrf');
  expect(session).toBeDefined();
  expect(csrf).toBeDefined();

  // HttpOnly on the session is what stops an XSS bug from becoming a stolen
  // session; readable on the CSRF token is what makes double-submit possible.
  // Getting either backwards breaks a security property silently.
  expect(session!.httpOnly).toBe(true);
  expect(csrf!.httpOnly).toBe(false);
  expect(session!.sameSite).toBe('Lax');
  expect(session!.path).toBe('/');
  expect(csrf!.path).toBe('/');

  // And the client can genuinely only see one of them.
  const visible = await page.evaluate(() => document.cookie);
  expect(visible).toContain('hcc_csrf');
  expect(visible).not.toContain('hcc_session');
});

test('signing out clears both cookies', async ({ page }) => {
  await signInAndWait(page);
  await openAccountMenu(page);
  await page.getByRole('button', { name: 'Sign out' }).click();
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();

  // A session cookie left in the jar would be sent on every later request and
  // would be a live credential if revocation were ever relaxed.
  expect(await cookie(page, 'hcc_session')).toBeUndefined();
  expect(await cookie(page, 'hcc_csrf')).toBeUndefined();
});

// ---------------------------------------------------------------------------
// Duplicate submission
// ---------------------------------------------------------------------------

test('double-clicking sign in spends one attempt, not two', async ({ page }) => {
  // Two sessions from one deliberate action is both a waste of the throttle
  // budget and a session nobody will ever revoke.
  await page.goto('/login');
  const requests: string[] = [];
  page.on('request', (request) => {
    if (request.url().includes('/auth/login')) requests.push(request.url());
  });

  await page.getByLabel('Username', { exact: true }).fill(ADMIN_USERNAME);
  await page.getByLabel('Password', { exact: true }).fill(ADMIN_PASSWORD);
  const submit = page.getByRole('button', { name: 'Sign in' });
  await submit.dblclick();

  await expect(page.getByRole('navigation', { name: 'Primary' })).toBeVisible();
  await page.waitForTimeout(500);
  expect(requests.length, 'the form submitted twice').toBe(1);
});

test('pressing Enter repeatedly spends one attempt', async ({ page }) => {
  await page.goto('/login');
  const requests: string[] = [];
  page.on('request', (request) => {
    if (request.url().includes('/auth/login')) requests.push(request.url());
  });

  await page.getByLabel('Username', { exact: true }).fill(ADMIN_USERNAME);
  await page.getByLabel('Password', { exact: true }).fill(ADMIN_PASSWORD);
  await page.keyboard.press('Enter');
  await page.keyboard.press('Enter');
  await page.keyboard.press('Enter');

  await expect(page.getByRole('navigation', { name: 'Primary' })).toBeVisible();
  await page.waitForTimeout(500);
  expect(requests.length).toBe(1);
});

// ---------------------------------------------------------------------------
// The public surface, as the running server actually routes it
// ---------------------------------------------------------------------------

test('every route the live server exposes is authenticated unless deliberately public', async ({
  request,
}) => {
  // The backend suite asserts this against the application object. This
  // asserts it against the process that is actually listening, which is where
  // a middleware ordering mistake or a stray mount would show up.
  const schema = await request.get(`${BACKEND_URL}/openapi.json`);
  expect(schema.status()).toBe(200);
  const document = (await schema.json()) as {
    paths: Record<string, Record<string, unknown>>;
  };

  const PUBLIC = new Set([
    'GET /healthz',
    'GET /readyz',
    'POST /api/v1/auth/login',
    'GET /api/v1/setup/status',
  ]);

  const unexpected: string[] = [];
  for (const [path, operations] of Object.entries(document.paths)) {
    for (const method of Object.keys(operations)) {
      if (!['get', 'post', 'put', 'patch', 'delete'].includes(method)) continue;
      if (path.includes('{')) continue; // no concrete id to call yet
      const label = `${method.toUpperCase()} ${path}`;

      const response = await request.fetch(`${BACKEND_URL}${path}`, {
        method: method.toUpperCase(),
        headers: { 'content-type': 'application/json' },
        ...(method === 'get' ? {} : { data: {} }),
        failOnStatusCode: false,
      });

      if (PUBLIC.has(label)) {
        expect(response.status(), `${label} is listed public but was refused`).not.toBe(401);
        continue;
      }
      // 401 is the required answer. A 422 would mean the body was validated
      // before authentication was checked, which leaks the schema; anything
      // 2xx means the route is public by accident.
      if (response.status() !== 401) {
        unexpected.push(`${label} -> ${response.status()}`);
      }
    }
  }

  expect(unexpected, 'routes reachable without a session').toEqual([]);
});
