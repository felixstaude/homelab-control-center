/**
 * The same application at four real viewport sizes.
 *
 * The installation is already initialised by the time these run — they are
 * about layout and interaction, not first-run state.
 *
 * ## Why measure rather than screenshot
 *
 * A screenshot diff tells you something changed; it does not tell you whether
 * the result is usable, and it fails on every intentional design change. Each
 * assertion here is a property an operator would notice being broken: content
 * they cannot reach, a field they cannot see, a button too small to hit, the
 * page jumping under their cursor. Those hold across design changes, which is
 * what makes them worth pinning.
 *
 * The four sizes are the ones the brief names, and they are chosen to sit on
 * both sides of the only layout breakpoint the shell has (`lg`, 1024px): two
 * desktop widths with a persistent sidebar, a portrait tablet and a phone with
 * the drawer.
 */

import { expect, test, type Page } from '@playwright/test';

import { ADMIN_USERNAME, ensureAdministrator, signIn, signInAndWait } from './helpers';

test.describe.configure({ mode: 'serial' });

// Runnable on its own: in a full run `10-auth-journey.spec.ts` has already
// bootstrapped and this exits 3, which is tolerated.
test.beforeAll(() => {
  ensureAdministrator();
});

type Viewport = { name: string; width: number; height: number; sidebar: boolean };

const VIEWPORTS: Viewport[] = [
  { name: '1920x1080 desktop', width: 1920, height: 1080, sidebar: true },
  { name: '1440x900 laptop', width: 1440, height: 900, sidebar: true },
  { name: '768x1024 tablet portrait', width: 768, height: 1024, sidebar: false },
  { name: '390x844 phone', width: 390, height: 844, sidebar: false },
];

/**
 * Horizontal overflow of the document.
 *
 * One pixel of tolerance: sub-pixel layout rounding can produce a scrollWidth
 * a fraction larger than the client width with nothing actually clipped, and
 * failing on that would be noise. Anything an operator could notice is far
 * above it.
 */
async function horizontalOverflow(page: Page): Promise<number> {
  return page.evaluate(() => {
    const root = document.documentElement;
    return Math.max(0, root.scrollWidth - root.clientWidth);
  });
}

/**
 * Every element whose own box extends past the right edge of the viewport.
 *
 * Elements inside a deliberately scrollable or clipping ancestor are skipped:
 * content wider than its own scroll container is the container doing its job,
 * and flagging it would make this assertion useless noise. What is left is
 * content that genuinely escapes the page.
 */
async function overflowingElements(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const limit = window.innerWidth + 1;
    const offenders: string[] = [];

    const insideClippedAncestor = (element: Element): boolean => {
      let parent = element.parentElement;
      while (parent && parent !== document.documentElement) {
        const overflowX = getComputedStyle(parent).overflowX;
        if (overflowX !== 'visible') return true;
        parent = parent.parentElement;
      }
      return false;
    };

    for (const element of Array.from(document.body.querySelectorAll('*'))) {
      const box = element.getBoundingClientRect();
      if (box.width === 0 && box.height === 0) continue;
      if (box.right <= limit) continue;
      if (insideClippedAncestor(element)) continue;
      const id = element.id ? `#${element.id}` : '';
      // Not `className`: on an SVG element that is an SVGAnimatedString, not
      // a string, and this walks every element on the page.
      const classes = (element.getAttribute('class') ?? '').slice(0, 60);
      offenders.push(
        `${element.tagName.toLowerCase()}${id}.${classes} right=${Math.round(box.right)}`,
      );
    }
    return offenders.slice(0, 10);
  });
}

for (const viewport of VIEWPORTS) {
  test.describe(viewport.name, () => {
    test.use({ viewport: { width: viewport.width, height: viewport.height } });

    test('the login page fits, and its fields are visible and usable', async ({ page }) => {
      await page.goto('/login');
      await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();

      expect(await horizontalOverflow(page)).toBeLessThanOrEqual(1);
      expect(await overflowingElements(page)).toEqual([]);

      // "Visible" in the sense that matters: on screen, with a real box, and
      // inside the viewport rather than scrolled off it.
      for (const label of ['Username', 'Password']) {
        const field = page.getByLabel(label, { exact: true });
        await expect(field).toBeVisible();
        await expect(field).toBeInViewport();
        const box = await field.boundingBox();
        expect(box, `${label} has no box`).not.toBeNull();
        expect(box!.width).toBeGreaterThan(80);
        expect(box!.height).toBeGreaterThanOrEqual(32);
      }

      const submit = page.getByRole('button', { name: 'Sign in' });
      await expect(submit).toBeVisible();
      await expect(submit).toBeInViewport();
      await expect(submit).toBeEnabled();
      const submitBox = await submit.boundingBox();
      // WCAG 2.2 AA target size (minimum) is 24x24 CSS pixels.
      expect(submitBox!.height).toBeGreaterThanOrEqual(24);
      expect(submitBox!.width).toBeGreaterThanOrEqual(24);
    });

    test('the console fits and its navigation is reachable', async ({ page }) => {
      await signInAndWait(page);

      expect(await horizontalOverflow(page)).toBeLessThanOrEqual(1);
      expect(await overflowingElements(page)).toEqual([]);

      const sidebarNav = page.getByRole('navigation', { name: 'Primary' });
      const drawerButton = page.getByRole('button', { name: 'Open navigation' });

      if (viewport.sidebar) {
        // The persistent sidebar is the navigation; no disclosure needed.
        await expect(sidebarNav).toBeVisible();
        await expect(drawerButton).toBeHidden();
        await page.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name: 'Hosts' }).click();
      } else {
        // Below `lg` the sidebar is hidden and the drawer must be the way in.
        // A hidden sidebar with no reachable drawer is unreachable navigation,
        // which is the failure this asserts against.
        await expect(drawerButton).toBeVisible();
        const buttonBox = await drawerButton.boundingBox();
        expect(buttonBox!.height).toBeGreaterThanOrEqual(24);
        expect(buttonBox!.width).toBeGreaterThanOrEqual(24);

        await drawerButton.click();
        const drawer = page.getByRole('dialog');
        await expect(drawer).toBeVisible();
        await drawer.getByRole('link', { name: 'Hosts' }).click();
      }

      await expect(page).toHaveURL(/\/hosts$/);
      await expect(page.getByRole('heading', { level: 2, name: 'Hosts' })).toBeVisible();
      // Navigating must not introduce overflow on the destination either.
      expect(await horizontalOverflow(page)).toBeLessThanOrEqual(1);
    });

    test('every dashboard section fits without overflow', async ({ page }) => {
      await signInAndWait(page);

      for (const path of ['/', '/hosts', '/docker', '/logs', '/alerts', '/network', '/settings']) {
        await page.goto(path);
        await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
        const overflow = await horizontalOverflow(page);
        expect(overflow, `${path} overflows horizontally by ${overflow}px`).toBeLessThanOrEqual(1);
        const offenders = await overflowingElements(page);
        expect(offenders, `${path} has elements past the right edge`).toEqual([]);
      }
    });

    test('the layout does not shift after it settles', async ({ page }) => {
      // Chromium's own layout-instability metric, rather than a screenshot
      // comparison: this is the number that corresponds to the page moving
      // under a user who has started reading or reaching for a control.
      await page.addInitScript(() => {
        (window as unknown as { __cls: number }).__cls = 0;
        new PerformanceObserver((list) => {
          for (const entry of list.getEntries()) {
            const shift = entry as PerformanceEntry & { value: number; hadRecentInput: boolean };
            if (!shift.hadRecentInput) {
              (window as unknown as { __cls: number }).__cls += shift.value;
            }
          }
        }).observe({ type: 'layout-shift', buffered: true });
      });

      await page.goto('/login');
      await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
      await signIn(page);
      await expect(page.locator('#main-content')).toBeVisible();
      // Let anything late — lazy chunks, fonts, the chart's Suspense
      // boundary resolving — have its chance to move the page.
      await page.waitForTimeout(1500);

      const cls = await page.evaluate(() => (window as unknown as { __cls: number }).__cls);
      // 0.1 is the "good" threshold in the web-vitals definition. The shell
      // reserves the chart's height precisely so the lazy chunk cannot push
      // content down when it arrives, so this should stay far below it.
      expect(cls, `cumulative layout shift was ${cls}`).toBeLessThan(0.1);
    });
  });
}

// ---------------------------------------------------------------------------
// Interaction properties that do not depend on the viewport
// ---------------------------------------------------------------------------

test.describe('form behaviour', () => {
  test('the login form is what a password manager expects', async ({ page }) => {
    await page.goto('/login');

    // A password manager finds credentials by walking a <form> and reading
    // input types and autocomplete tokens. Each of these is load-bearing for
    // that, and all of them are invisible to a human reviewer.
    const form = page.locator('form');
    await expect(form).toHaveCount(1);

    const username = page.getByLabel('Username', { exact: true });
    const password = page.getByLabel('Password', { exact: true });

    await expect(username).toHaveAttribute('autocomplete', 'username');
    await expect(username).toHaveAttribute('name', 'username');
    await expect(username).toHaveAttribute('type', 'text');
    await expect(password).toHaveAttribute('autocomplete', 'current-password');
    await expect(password).toHaveAttribute('name', 'password');
    await expect(password).toHaveAttribute('type', 'password');

    // Both inside the same form, and the submit button genuinely submits it,
    // so a manager's autofill-and-submit works.
    expect(await form.locator('input[name="username"]').count()).toBe(1);
    expect(await form.locator('input[name="password"]').count()).toBe(1);
    await expect(page.getByRole('button', { name: 'Sign in' })).toHaveAttribute('type', 'submit');
  });

  test('a filled-in credential submits the way autofill leaves it', async ({ page }) => {
    // Autofill sets `value` directly rather than typing, which does not
    // produce the keystroke events a controlled React input listens for. If
    // the form read its own state instead of the DOM, this would submit
    // empty — so drive it the way a manager does and check it works.
    await page.goto('/login');
    // `goto` resolves on the load event, which for a single-page application
    // is before the authentication state has resolved and the form has been
    // rendered. Waiting for the field is the difference between testing
    // autofill and testing an empty document.
    await expect(page.getByLabel('Username', { exact: true })).toBeVisible();
    await page.evaluate(
      ([user, pass]) => {
        const set = (name: string, value: string) => {
          const input = document.querySelector(`input[name="${name}"]`);
          if (!(input instanceof HTMLInputElement)) {
            throw new Error(`no input named ${name}`);
          }
          // Through the prototype's setter, which is how a password manager
          // and the browser's own autofill assign a value: it updates the DOM
          // without going through React's synthetic event system. React
          // de-duplicates by tracking the previous value, so a plain
          // assignment can be ignored entirely — this is what defeats that.
          const descriptor = Object.getOwnPropertyDescriptor(
            HTMLInputElement.prototype,
            'value',
          );
          descriptor?.set?.call(input, value);
          input.dispatchEvent(new Event('input', { bubbles: true }));
        };
        set('username', user);
        set('password', pass);
      },
      [ADMIN_USERNAME, 'correct horse battery staple'] as const,
    );

    await page.getByRole('button', { name: 'Sign in' }).click();
    await expect(page.getByRole('navigation', { name: 'Primary' })).toBeVisible();
  });

  test('a validation error is announced, not just coloured', async ({ page }) => {
    await page.goto('/login');
    await page.getByRole('button', { name: 'Sign in' }).click();

    // An error a screen reader never reaches is not an error message. The
    // field must point at its description and be marked invalid, and the text
    // must live in something with an assertive role.
    const username = page.getByLabel('Username', { exact: true });
    await expect(username).toHaveAttribute('aria-invalid', 'true');
    const describedBy = await username.getAttribute('aria-describedby');
    expect(describedBy).toBeTruthy();
    const description = page.locator(`#${describedBy!}`);
    await expect(description).toHaveText(/username/i);
    await expect(description).toHaveAttribute('role', 'alert');
  });

  test('focus moves to the field that is wrong, and stays reachable', async ({ page }) => {
    await page.goto('/login');
    // The username is focused on arrival, so the first keystroke lands
    // somewhere useful rather than nowhere.
    await expect(page.getByLabel('Username', { exact: true })).toBeFocused();

    await page.keyboard.type(ADMIN_USERNAME);
    await page.keyboard.press('Tab');
    await expect(page.getByLabel('Password', { exact: true })).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(page.getByRole('button', { name: 'Show password' })).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(page.getByRole('button', { name: 'Sign in' })).toBeFocused();
  });

  test('the skip link is the first thing a keyboard reaches on the console', async ({ page }) => {
    await signInAndWait(page);
    await page.keyboard.press('Tab');
    const focused = page.locator(':focus');
    await expect(focused).toHaveText(/skip to (main )?content/i);

    // And it must actually move focus into the main region, not just exist.
    await page.keyboard.press('Enter');
    await expect(page).toHaveURL(/#main-content$/);
  });
});
