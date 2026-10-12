import { defineConfig, devices } from '@playwright/test';

/**
 * Browser end-to-end configuration.
 *
 * These tests drive a real Chromium against a real control plane and a real
 * PostgreSQL. Everything else in this repository tests a layer; this tests
 * whether the layers fit together — which is the one thing a unit test cannot
 * tell you, and the thing most likely to be wrong after a milestone that
 * joined a frontend to a backend for the first time.
 *
 * ## What runs where
 *
 * `scripts/e2e.sh` owns the environment: it creates a **disposable database**,
 * applies migrations, starts the control plane on a dedicated port, builds the
 * client, and serves it through `vite preview` with the same `/api` proxy the
 * dev server uses. That script is also what makes these safe to run on a
 * developer's machine — it never touches the development database.
 *
 * Playwright therefore starts no servers itself. `webServer` is deliberately
 * unused: reproducing the migration and bootstrap sequence here would
 * duplicate the script, and the two would drift.
 *
 * ## Why the files are numbered
 *
 * The suites share one installation, and two of them assert *first-run* state
 * that cannot be restored once consumed: `10-auth-journey.spec.ts` tests the
 * uninitialised installation and spends its one-shot bootstrap, and
 * `20-host-journey.spec.ts` asserts an empty host registry. Everything after
 * them bootstraps tolerantly and creates whatever it needs.
 *
 * Playwright runs files in path order, so that ordering is a real contract.
 * It used to hold by alphabetical accident, until a file whose name sorted
 * before `auth-journey` was added and quietly broke both assertions. The
 * numeric prefixes make the order a declared property instead of a lucky one,
 * and `scripts/e2e.sh` refuses to run if a spec file is missing its prefix.
 * Gaps of ten leave room to insert a suite without renumbering.
 */

/** Where `scripts/e2e.sh` serves the built client. */
const BASE_URL = process.env.E2E_BASE_URL ?? 'http://127.0.0.1:4173';

export default defineConfig({
  testDir: './e2e',
  // One worker: the tests share one control plane and one database, and
  // several of them sign in and out of the same account. Parallelism here
  // would produce failures that depend on interleaving rather than on code.
  workers: 1,
  fullyParallel: false,
  // A flake that is retried into green is a flake nobody fixes.
  retries: 0,
  // Generous but finite: a hung step should fail, not stall the suite.
  timeout: 30_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI ? [['list'], ['github']] : [['list']],

  use: {
    baseURL: BASE_URL,
    // Captured only for a failure, so a passing run leaves nothing behind.
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
