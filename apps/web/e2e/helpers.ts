/**
 * Shared fixture access for the browser suites.
 *
 * `scripts/e2e.sh` owns the environment and passes its coordinates in through
 * the environment variables read below. Nothing here starts or stops a
 * process: a spec that managed its own server would drift from the script, and
 * the script is what makes these tests safe to run on a developer's machine.
 *
 * Not named `*.spec.ts`, so Playwright does not collect it as a suite.
 */

import { execFileSync, spawn, spawnSync } from 'node:child_process';
import { rmSync, writeFileSync } from 'node:fs';

import { expect, type Page } from '@playwright/test';

export const ADMIN_USERNAME = 'E2eAdmin';
/** Not a production credential: this database exists for one run. */
export const ADMIN_PASSWORD = 'correct horse battery staple';

export const BACKEND_URL = process.env.E2E_BACKEND_URL ?? 'http://127.0.0.1:8201';
export const DATABASE_URL = process.env.E2E_DATABASE_URL ?? '';
export const SERVER_DIR = process.env.E2E_SERVER_DIR ?? '';

/**
 * The throttle window the script configures, in seconds.
 *
 * The rate-limiting test has to wait one out, so it needs the real number
 * rather than a guess — a hardcoded wait that was shorter than the window
 * would leave every later test throttled.
 */
export const THROTTLE_WINDOW_SECONDS = Number(process.env.E2E_THROTTLE_WINDOW_SECONDS ?? '5');

/**
 * Run `hcc bootstrap-admin` against the disposable database.
 *
 * The real command, with the password on standard input — the same
 * non-interactive path `docs/DEVELOPMENT.md` documents for automation. Using
 * the command rather than inserting a row directly is the point: it verifies
 * that the documented procedure produces an account the browser can then
 * actually sign in with.
 */
export function bootstrapAdministrator(): string {
  return execFileSync(
    `${SERVER_DIR}/.venv/bin/hcc`,
    ['bootstrap-admin', '--username', ADMIN_USERNAME, '--password-stdin'],
    {
      cwd: SERVER_DIR,
      input: `${ADMIN_PASSWORD}\n`,
      encoding: 'utf8',
      env: { ...process.env, HCC_DATABASE_URL: DATABASE_URL },
    },
  );
}

/**
 * Make sure an administrator exists, whoever got here first.
 *
 * `10-auth-journey.spec.ts` bootstraps as part of testing the first-run
 * sequence, so in a full run this is a no-op that exits 3. It exists so that
 * every other suite can be run on its own — a file that silently depended on
 * another file having run first would be reported as a product failure the
 * moment anyone selected a subset, which is exactly what happened when this
 * was missing.
 */
export function ensureAdministrator(): void {
  try {
    bootstrapAdministrator();
  } catch (error) {
    const status = (error as { status?: number }).status;
    // 3 is "already initialized", which is the expected outcome here.
    if (status !== 3) throw error;
  }
}

/**
 * Execute one SQL statement against the disposable database and return the
 * rows as JSON.
 *
 * Used to age a session out and to count rows — states that cannot be reached
 * through the API at all, which is exactly why they are worth testing. Driven
 * through the server venv's `asyncpg` rather than a new npm dependency.
 */
export function sql(statement: string): unknown[] {
  const output = execFileSync(
    `${SERVER_DIR}/.venv/bin/python`,
    [
      '-c',
      [
        'import asyncio, json, os, sys, asyncpg',
        'async def main():',
        '    c = await asyncpg.connect(os.environ["E2E_DATABASE_URL"])',
        '    try:',
        '        rows = await c.fetch(sys.argv[1])',
        '    finally:',
        '        await c.close()',
        '    print(json.dumps([{k: str(v) for k, v in dict(r).items()} for r in rows]))',
        'asyncio.run(main())',
      ].join('\n'),
      statement,
    ],
    {
      encoding: 'utf8',
      env: { ...process.env, E2E_DATABASE_URL: DATABASE_URL },
    },
  );
  return JSON.parse(output) as unknown[];
}

/**
 * Fill the form and submit.
 *
 * `exact: true` on the labels: Playwright's `getByLabel` matches substrings by
 * default, so plain `'Password'` also matches the "Show password" toggle.
 */
export async function signIn(
  page: Page,
  username = ADMIN_USERNAME,
  password = ADMIN_PASSWORD,
): Promise<void> {
  await page.getByLabel('Username', { exact: true }).fill(username);
  await page.getByLabel('Password', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Sign in' }).click();
}

/**
 * Sign in from a blank page and wait until the console is on screen.
 *
 * The signal is the main region and the shell's heading, not the sidebar
 * navigation. Below the `lg` breakpoint the sidebar is `display: none` and so
 * is absent from the accessibility tree entirely — `getByRole` cannot see it,
 * which made an earlier version of this helper fail on the tablet and phone
 * viewports for a reason that had nothing to do with signing in.
 */
export async function signInAndWait(
  page: Page,
  username = ADMIN_USERNAME,
  password = ADMIN_PASSWORD,
): Promise<void> {
  await page.goto('/login');
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
  await signIn(page, username, password);
  await expect(page.locator('#main-content')).toBeVisible();
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
}

/**
 * Sign out through the account menu, as a user does.
 *
 * Waits for the login page rather than for the click to return: sign-out is a
 * server-side revocation followed by a route change, and asserting on the
 * destination is what proves both halves happened.
 */
export async function signOut(page: Page, username = ADMIN_USERNAME): Promise<void> {
  await page.getByRole('button', { name: new RegExp(username) }).click();
  await page.getByRole('button', { name: 'Sign out' }).click();
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
}

export async function openAccountMenu(page: Page): Promise<void> {
  await page.getByRole('button', { name: new RegExp(ADMIN_USERNAME) }).click();
}

/**
 * Call the API from inside the page, the way the application does.
 *
 * Same origin, real cookies, through the same proxy — so the result reflects
 * what the browser is actually permitted to do, not what a Node HTTP client
 * is. Returns the status and parsed body rather than throwing, because most
 * callers here are asserting on a refusal.
 */
export async function apiFromPage(
  page: Page,
  path: string,
  init: { method?: string; headers?: Record<string, string>; body?: unknown } = {},
): Promise<{ status: number; body: unknown }> {
  return page.evaluate(
    async ([target, options]) => {
      const response = await fetch(target, {
        method: options.method ?? 'GET',
        credentials: 'same-origin',
        headers: { 'content-type': 'application/json', ...(options.headers ?? {}) },
        ...(options.body === undefined ? {} : { body: JSON.stringify(options.body) }),
      });
      let body: unknown = null;
      try {
        body = await response.json();
      } catch {
        body = null;
      }
      return { status: response.status, body };
    },
    [path, init] as const,
  );
}

/** Read a cookie by name from the browser's jar for the page's origin. */
export async function cookie(
  page: Page,
  name: string,
): Promise<{ value: string; httpOnly: boolean; path: string; sameSite: string } | undefined> {
  const cookies = await page.context().cookies();
  const found = cookies.find((candidate) => candidate.name === name);
  if (!found) return undefined;
  return {
    value: found.value,
    httpOnly: found.httpOnly,
    path: found.path,
    sameSite: found.sameSite,
  };
}

/* -------------------------------------------------------------------------
 * The live agent (milestone 1.8)
 *
 * `scripts/e2e.sh` builds the real Go binary and passes its path in. These
 * helpers drive it the way an operator would — `enroll`, then `run` — so the
 * host journey proves a host genuinely connects rather than proving the
 * interface can render a fixture.
 *
 * The binary is optional. A contributor without a Go toolchain gets every
 * other suite and a stated skip; CI sets `E2E_REQUIRE_AGENT=1`, so a
 * missing toolchain there is a failure rather than a silent gap.
 * ---------------------------------------------------------------------- */

export const AGENT_BINARY = process.env.E2E_AGENT_BINARY ?? '';
export const AGENT_STATE_DIR = process.env.E2E_AGENT_STATE_DIR ?? '';

/** Whether a live-agent test can run at all. */
export function agentAvailable(): boolean {
  return AGENT_BINARY !== '' && AGENT_STATE_DIR !== '';
}

/**
 * Why a live-agent test is being skipped, as a sentence.
 *
 * A skip with no reason is a gap nobody notices; this one names the fix.
 */
export const AGENT_SKIP_REASON =
  'no Go toolchain was available when scripts/e2e.sh ran, so the agent was ' +
  'not built. Install Go 1.24+ and re-run to exercise the live host journey.';

/** Where the agent stores the credential it is given. */
function credentialPath(): string {
  return `${AGENT_STATE_DIR}/credential`;
}

/**
 * The agent's environment.
 *
 * Plain `http://127.0.0.1` is deliberate and is the one case the agent
 * permits without TLS: a controller on loopback is reached over a socket no
 * other machine can see. Any other host over http is refused at startup,
 * which `50-security.spec.ts` asserts.
 */
function agentEnv(extra: Record<string, string> = {}): Record<string, string> {
  return {
    ...process.env,
    HCC_AGENT_CONTROLLER_URL: BACKEND_URL,
    HCC_AGENT_CREDENTIAL_FILE: credentialPath(),
    HCC_AGENT_LOG_FORMAT: 'text',
    // Short, so a reconnection inside a test does not outlast its timeout.
    HCC_AGENT_RECONNECT_MIN_WAIT: '1s',
    HCC_AGENT_RECONNECT_MAX_WAIT: '2s',
    ...extra,
  } as Record<string, string>;
}

/**
 * Spend an enrollment credential, as `hcc-agent enroll` does.
 *
 * The credential goes into a 0600 file rather than the environment, matching
 * what `agents/linux/deploy/README.md` tells an operator to do — so this also
 * verifies that the documented procedure works.
 *
 * Returns the command's output. Throws with it attached on failure, because
 * "enrollment failed" with no reason is the least useful possible test
 * failure.
 */
export function enrollAgent(credential: string): string {
  const tokenPath = `${AGENT_STATE_DIR}/enrolment-token`;
  writeFileSync(tokenPath, `${credential}\n`, { mode: 0o600 });
  try {
    return execFileSync(AGENT_BINARY, ['enroll', '-force'], {
      encoding: 'utf8',
      env: agentEnv({ HCC_AGENT_ENROLLMENT_TOKEN_FILE: tokenPath }),
    });
  } finally {
    // Spent, so there is no reason to leave it on disk.
    rmSync(tokenPath, { force: true });
  }
}

/** `hcc-agent status`, with its exit code. */
export function agentStatus(): { code: number; output: string } {
  const result = spawnSync(AGENT_BINARY, ['status'], {
    encoding: 'utf8',
    env: agentEnv(),
  });
  return { code: result.status ?? -1, output: `${result.stdout}${result.stderr}` };
}

/** A running agent, and the handle to stop it. */
export type RunningAgent = {
  /** Everything the agent has logged so far. */
  log: () => string;
  stop: () => void;
  /** Whether the process is still running. */
  alive: () => boolean;
  /** The exit code, once it has exited. */
  exitCode: () => number | null;
};

/**
 * Start `hcc-agent run` in the background.
 *
 * Detached, so stopping it cannot leave a stray process attached to the test
 * runner's process group — a leaked agent would keep reconnecting and make
 * every later assertion about connectivity wrong.
 */
export function startAgent(): RunningAgent {
  let output = '';
  let exited: number | null = null;

  const child = spawn(AGENT_BINARY, ['run'], {
    env: agentEnv(),
    stdio: ['ignore', 'pipe', 'pipe'],
    detached: true,
  });
  child.stdout.on('data', (chunk: Buffer) => {
    output += chunk.toString();
  });
  child.stderr.on('data', (chunk: Buffer) => {
    output += chunk.toString();
  });
  child.on('exit', (code) => {
    exited = code;
  });

  return {
    log: () => output,
    alive: () => exited === null && child.exitCode === null,
    exitCode: () => exited ?? child.exitCode,
    stop: () => {
      if (child.exitCode === null) {
        // SIGTERM, which is what systemd sends — so this exercises the
        // graceful-shutdown path rather than killing the process outright.
        try {
          child.kill('SIGTERM');
        } catch {
          // Already gone.
        }
      }
    },
  };
}

/** Wait until the agent's log contains `substring`, or fail with the log. */
export async function waitForAgentLog(
  agent: RunningAgent,
  substring: string,
  timeoutMs = 15_000,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (agent.log().includes(substring)) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(
    `the agent never logged ${JSON.stringify(substring)} within ${String(timeoutMs)}ms.\n` +
      `Its log was:\n${agent.log()}`,
  );
}

/** Wait until the agent process has exited, and return its code. */
export async function waitForAgentExit(
  agent: RunningAgent,
  timeoutMs = 15_000,
): Promise<number | null> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!agent.alive()) return agent.exitCode();
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`the agent was still running after ${String(timeoutMs)}ms:\n${agent.log()}`);
}
