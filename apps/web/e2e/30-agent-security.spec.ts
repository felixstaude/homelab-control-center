import { execFileSync, spawnSync } from 'node:child_process';
import { chmodSync, readFileSync, renameSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';

import { expect, test } from '@playwright/test';

import {
  AGENT_BINARY,
  AGENT_SKIP_REASON,
  AGENT_STATE_DIR,
  BACKEND_URL,
  agentAvailable,
  apiFromPage,
  enrollAgent,
  ensureAdministrator,
  signInAndWait,
  sql,
  startAgent,
  waitForAgentExit,
  waitForAgentLog,
} from './helpers';

/**
 * The security review for agent enrollment and connection, as tests.
 *
 * Milestone 1.8 §21 lists an attack surface: enrollment hijacking, credential
 * replay, WebSocket authentication bypass, TLS downgrade, symlink attacks,
 * reconnect storms, revocation races. Each is written here as an attempt that
 * must fail, because a test that only exercises the happy path cannot tell a
 * working defence from an absent one — and a prose review cannot tell either.
 *
 * The attempts that need two *kinds* of credential are the ones worth having
 * at this level. A unit test can check that a bad agent credential is refused;
 * only a live run can check that a **browser session** is not accepted as one,
 * and that an agent credential is not accepted as a session — the two
 * mistakes that would collapse the whole separation.
 *
 * Several properties are asserted in the backend suite instead, where they
 * can be driven against real PostgreSQL rather than inferred:
 *
 * - concurrent enrollment with one credential →
 *   `test_host_concurrency_integration.py`
 * - concurrent revocation, and revoking while enrolling → same file
 * - a frame flood, an oversized frame, an unknown frame type →
 *   `test_api_agents_integration.py`
 * - that no column could hold a plaintext credential → `test_models_host.py`
 */

test.describe.configure({ mode: 'serial' });

test.beforeAll(() => {
  ensureAdministrator();
});

const credentialPath = () => `${AGENT_STATE_DIR}/credential`;

/** Run the agent with a doctored environment and return what it printed. */
function runAgent(
  args: string[],
  env: Record<string, string> = {},
): { code: number; output: string } {
  const result = spawnSync(AGENT_BINARY, args, {
    encoding: 'utf8',
    env: {
      ...process.env,
      HCC_AGENT_CONTROLLER_URL: BACKEND_URL,
      HCC_AGENT_CREDENTIAL_FILE: credentialPath(),
      HCC_AGENT_RECONNECT_MIN_WAIT: '1s',
      HCC_AGENT_RECONNECT_MAX_WAIT: '1s',
      ...env,
    },
    timeout: 20_000,
  });
  return { code: result.status ?? -1, output: `${result.stdout}${result.stderr}` };
}

test.describe('the two kinds of credential are not interchangeable', () => {
  let hostId = '';
  let enrolmentCredential = '';

  test('arrange: a host with a live agent credential', async ({ page }) => {
    test.skip(!agentAvailable(), AGENT_SKIP_REASON);

    await signInAndWait(page);
    await page.goto('/hosts');
    await page.getByRole('button', { name: /register/i }).first().click();
    await page.getByLabel('Name').fill('sec-host');
    await page.getByRole('button', { name: /^register host$/i }).click();
    await page.getByRole('button', { name: /issue credential/i }).click();

    const field = page.getByText(/^[A-Za-z0-9_-]{43}$/);
    await expect(field).toBeVisible();
    enrolmentCredential = (await field.innerText()).trim();

    const rows = sql("SELECT id::text FROM hosts WHERE name = 'sec-host'") as {
      id: string;
    }[];
    hostId = rows[0]!.id;

    enrollAgent(enrolmentCredential);
  });

  test('a browser session is not accepted as an agent credential', async ({ page }) => {
    // The attack: a stolen session cookie used to enroll a host of the
    // attacker's choosing, or to report as one. The enrollment endpoint reads
    // one header and nothing else, so the cookie is simply not consulted.
    await signInAndWait(page);

    const refused = await apiFromPage(page, '/api/v1/agents/enroll', {
      method: 'POST',
      body: {},
    });

    expect(refused.status).toBe(401);
    // `not_authenticated`, not `agent_credential_rejected`: nothing was
    // presented, which is a different answer from "what you presented is
    // wrong" and is the honest one.
    expect((refused.body as { error: { code: string } }).error.code).toBe('not_authenticated');
  });

  test('a session token is not accepted as an agent credential', async ({ page, request }) => {
    test.skip(!agentAvailable(), AGENT_SKIP_REASON);

    // One step further than the test above: the session's *value*, presented
    // as a bearer credential. A session token and an agent credential are the
    // same shape — 43 characters of base64url from the same generator — so
    // nothing but the lookup table keeps them apart.
    await signInAndWait(page);
    const cookies = await page.context().cookies();
    const session = cookies.find((candidate) => candidate.name === 'hcc_session');
    expect(session, 'no session cookie to replay').toBeDefined();

    const refused = await request.post(`${BACKEND_URL}/api/v1/agents/enroll`, {
      headers: { authorization: `Bearer ${session!.value}`, 'content-type': 'application/json' },
      data: {},
      failOnStatusCode: false,
    });

    expect(refused.status()).toBe(401);
  });

  test('an agent credential is not accepted as a session', async ({ request }) => {
    test.skip(!agentAvailable(), AGENT_SKIP_REASON);

    // The mirror image, and the one that would matter more: an agent
    // credential that worked as a session would turn one compromised host
    // into an account on the control plane.
    const credential = readFileSync(credentialPath(), 'utf8').trim();

    const asCookie = await request.get(`${BACKEND_URL}/api/v1/auth/me`, {
      headers: { cookie: `hcc_session=${credential}` },
      failOnStatusCode: false,
    });
    const asBearer = await request.get(`${BACKEND_URL}/api/v1/hosts`, {
      headers: { authorization: `Bearer ${credential}` },
      failOnStatusCode: false,
    });

    expect(asCookie.status()).toBe(401);
    // The host list is session-gated, and a bearer credential is not a
    // session — so an agent cannot read the fleet it is part of.
    expect(asBearer.status()).toBe(401);
  });

  test('an agent credential cannot be spent at the enrollment endpoint', async ({ request }) => {
    test.skip(!agentAvailable(), AGENT_SKIP_REASON);

    // Replay across endpoints: the durable credential presented where the
    // one-time one belongs. The two live in different tables, so the lookup
    // simply misses — but it is worth asserting, because a shared table
    // would have made this work.
    const credential = readFileSync(credentialPath(), 'utf8').trim();

    const refused = await request.post(`${BACKEND_URL}/api/v1/agents/enroll`, {
      headers: { authorization: `Bearer ${credential}`, 'content-type': 'application/json' },
      data: {},
      failOnStatusCode: false,
    });

    expect(refused.status()).toBe(401);
  });

  test('a spent enrollment credential cannot open a connection', async () => {
    test.skip(!agentAvailable(), AGENT_SKIP_REASON);

    // And the other direction: the one-time credential presented where the
    // durable one belongs, at the socket.
    const saved = readFileSync(credentialPath(), 'utf8');
    writeFileSync(credentialPath(), `${enrolmentCredential}\n`, { mode: 0o600 });
    try {
      const agent = startAgent();
      await waitForAgentLog(agent, 'rejected this agent');
      const code = await waitForAgentExit(agent, 20_000);
      expect(code).not.toBe(0);
    } finally {
      writeFileSync(credentialPath(), saved, { mode: 0o600 });
    }
  });

  test('an unrecognised credential is refused identically to a used one', async ({ request }) => {
    // No oracle: a caller must not be able to learn whether a credential they
    // hold was ever real. The audit trail records which it was, because an
    // administrator can act on that and an attacker cannot.
    const unknown = 'z'.repeat(43);

    const used = await request.post(`${BACKEND_URL}/api/v1/agents/enroll`, {
      headers: {
        authorization: `Bearer ${enrolmentCredential}`,
        'content-type': 'application/json',
      },
      data: {},
      failOnStatusCode: false,
    });
    const never = await request.post(`${BACKEND_URL}/api/v1/agents/enroll`, {
      headers: { authorization: `Bearer ${unknown}`, 'content-type': 'application/json' },
      data: {},
      failOnStatusCode: false,
    });

    expect(used.status()).toBe(never.status());
    // Compared without `request_id`, which differs by design: it is a
    // per-request correlation id that lets an operator line an error up with
    // the server log, and it discloses nothing about the credential. The
    // *code* and the *message* are what must be identical.
    const strip = (body: { error: { code: string; message: string } }) => ({
      code: body.error.code,
      message: body.error.message,
    });
    expect(strip((await used.json()) as { error: { code: string; message: string } })).toEqual(
      strip((await never.json()) as { error: { code: string; message: string } }),
    );
  });

  test('no unrecognised credential produces an audit row', async ({ request }) => {
    // An endpoint reachable without a session must not be a table-growth
    // amplifier. A digest that resolves to no host has nothing to attribute
    // a row to, so it is logged and not recorded.
    const before = sql(
      "SELECT count(*) AS n FROM audit_events WHERE action = 'host.agent_auth_failed'",
    ) as { n: string }[];

    for (let attempt = 0; attempt < 20; attempt += 1) {
      await request.post(`${BACKEND_URL}/api/v1/agents/enroll`, {
        headers: {
          authorization: `Bearer ${String(attempt).padStart(43, 'q')}`,
          'content-type': 'application/json',
        },
        data: {},
        failOnStatusCode: false,
      });
    }

    const after = sql(
      "SELECT count(*) AS n FROM audit_events WHERE action = 'host.agent_auth_failed'",
    ) as { n: string }[];
    expect(after[0]!.n).toBe(before[0]!.n);
  });

  test('a host cannot be claimed: the field is rejected, not ignored', async ({ request }) => {
    test.skip(!agentAvailable(), AGENT_SKIP_REASON);

    // The request model forbids unknown fields, so naming a host is a **422**
    // rather than a silently ignored hint — and that is the stronger answer.
    // A dropped field is how a caller comes to believe it set something it
    // did not; a refusal says plainly that there is no such parameter.
    //
    // This test originally asserted 401, on the reasoning that the credential
    // dependency raises before the body is validated. That is true only when
    // *no* credential is presented. With a well-formed one the dependency
    // succeeds, the body is validated, and the 422 comes first.
    const claimed = await request.post(`${BACKEND_URL}/api/v1/agents/enroll`, {
      headers: {
        authorization: `Bearer ${'a'.repeat(43)}`,
        'content-type': 'application/json',
      },
      data: { host_id: hostId },
      failOnStatusCode: false,
    });

    expect(claimed.status()).toBe(422);
  });

  test('the body refusal is not a credential oracle', async ({ request }) => {
    test.skip(!agentAvailable(), AGENT_SKIP_REASON);

    // The one thing worth checking about that 422: it must not depend on
    // whether the credential was real. If a malformed body answered 422 for a
    // live credential and 401 for a dead one, the body would be a probe for
    // which credentials exist.
    const bodies = await Promise.all(
      [enrolmentCredential, 'z'.repeat(43), 'q'.repeat(43)].map((credential) =>
        request.post(`${BACKEND_URL}/api/v1/agents/enroll`, {
          headers: { authorization: `Bearer ${credential}`, 'content-type': 'application/json' },
          data: { host_id: hostId },
          failOnStatusCode: false,
        }),
      ),
    );

    // One status for all three: a spent credential, and two that never
    // existed.
    expect(bodies.map((response) => response.status())).toEqual([422, 422, 422]);
  });

  test('a credential-less request is refused before the body is read', async ({ request }) => {
    // The complementary property, and the one that keeps this endpoint inside
    // the default-deny guarantee without an entry in the public allow-list:
    // with nothing presented, the dependency raises first and the answer is
    // 401 rather than a 422 that would describe the request schema.
    const refused = await request.post(`${BACKEND_URL}/api/v1/agents/enroll`, {
      headers: { 'content-type': 'application/json' },
      data: { host_id: 'anything', nonsense: true },
      failOnStatusCode: false,
    });

    expect(refused.status()).toBe(401);
  });
});

test.describe('the agent refuses to weaken its own transport', () => {
  test('plain HTTP to a non-loopback controller is refused', () => {
    test.skip(!agentAvailable(), AGENT_SKIP_REASON);

    // TLS downgrade. The agent's credential and every heartbeat cross this
    // connection, so an unverified or plaintext session invites anyone on the
    // path to collect a credential that authorizes reporting as this host.
    const result = runAgent(['status'], {
      HCC_AGENT_CONTROLLER_URL: 'http://hcc.example.com',
    });

    expect(result.code).not.toBe(0);
    expect(result.output).toContain('in the clear');
  });

  test('there is no way to skip certificate verification', () => {
    test.skip(!agentAvailable(), AGENT_SKIP_REASON);

    // Not as a flag, and not as an environment variable. The variable the
    // previous version of this agent honoured is now inert, which is the
    // outcome to want: silently ignored beats obeyed.
    const help = runAgent(['-h']);
    expect(help.output).toContain('no option to skip TLS verification');
    for (const flag of ['-insecure', '-skip-verify', '-no-verify', '-k ']) {
      expect(help.output).not.toContain(flag);
    }

    const ignored = runAgent(['status'], { HCC_AGENT_TLS_SKIP_VERIFY: 'true' });
    // Accepted *and ignored*: the run fails or succeeds on its own merits,
    // never because a verification-skipping variable was honoured.
    expect(ignored.output).not.toContain('verification is DISABLED');
  });

  test('no secret is accepted on the command line', () => {
    test.skip(!agentAvailable(), AGENT_SKIP_REASON);

    // A command line is readable by every local user through
    // /proc/<pid>/cmdline, so a flag is the one place a secret must never go.
    //
    // The patterns are precise rather than broad, and the reason is worth
    // stating: `--token-file` is permitted and documented, because it takes a
    // *path* and a path is not a secret. A bare `-token` pattern matched it,
    // so the first commit that added the automation flag turned this test red
    // — and the tempting fix is to delete the test. These patterns require a
    // value to follow, which is what actually distinguishes "takes a secret"
    // from "takes a path to one".
    const help = runAgent(['-h']);
    for (const flag of [
      '-token ',
      '-token=',
      '-credential ',
      '-credential=',
      '-secret',
      '-enrollment-token',
      '-enrolment-token',
      '-password',
    ]) {
      expect(help.output).not.toContain(flag);
    }

    // And the path-taking form stays documented, because without it the next
    // thing somebody reaches for is an environment variable — readable
    // through /proc/<pid>/environ and inherited by every child process.
    expect(help.output).toContain('--token-file');
  });
});

test.describe('the credential file is defended locally', () => {
  test('a world-readable credential is refused', () => {
    test.skip(!agentAvailable(), AGENT_SKIP_REASON);

    // A secret readable by every local user is not a secret. Refused rather
    // than silently fixed: chmod-ing a file the agent did not create would
    // hide a misconfiguration the operator needs to see.
    const original = readFileSync(credentialPath(), 'utf8');
    chmodSync(credentialPath(), 0o644);
    try {
      const result = runAgent(['status']);
      expect(result.code).not.toBe(0);
      expect(result.output).toContain('chmod 600');
    } finally {
      writeFileSync(credentialPath(), original, { mode: 0o600 });
      chmodSync(credentialPath(), 0o600);
    }
  });

  test('a symlinked credential is refused', () => {
    test.skip(!agentAvailable(), AGENT_SKIP_REASON);

    // The classic local attack: point the path at a file you own, so the
    // agent reads a credential you chose. `Lstat` rather than `Stat` is what
    // makes this work — `Stat` would report the *target's* mode and owner.
    const real = credentialPath();
    const moved = `${real}.moved`;
    const planted = `${AGENT_STATE_DIR}/planted`;
    renameSync(real, moved);
    writeFileSync(planted, `${'p'.repeat(43)}\n`, { mode: 0o600 });
    symlinkSync(planted, real);
    try {
      const result = runAgent(['status']);
      expect(result.code).not.toBe(0);
      expect(result.output).toContain('symbolic link');
    } finally {
      rmSync(real, { force: true });
      rmSync(planted, { force: true });
      renameSync(moved, real);
    }
  });

  test('the credential is written at mode 0600 and nothing wider', () => {
    test.skip(!agentAvailable(), AGENT_SKIP_REASON);

    const mode = execFileSync('stat', ['-c', '%a', credentialPath()], {
      encoding: 'utf8',
    }).trim();

    expect(mode).toBe('600');
  });
});

test.describe('nothing leaks a credential', () => {
  test('the controller log contains no credential material', () => {
    // The log is where a secret ends up when somebody prints a request or a
    // struct. Checked against the real log the running controller produced.
    const log = readFileSync('/tmp/hcc-e2e-server.log', 'utf8');

    // Every credential issued during this run, read back from the digests'
    // own table to be sure none was missed.
    const digests = sql('SELECT token_hash FROM host_enrollments') as {
      token_hash: string;
    }[];
    expect(digests.length).toBeGreaterThan(0);

    for (const { token_hash } of digests) {
      // Not even the digest: it is the lookup key, so a log line carrying it
      // would let whoever reads the log confirm whether a captured
      // credential is live.
      expect(log).not.toContain(token_hash);
    }
    // And no plaintext-shaped bearer value from an Authorization header.
    expect(log).not.toMatch(/Bearer [A-Za-z0-9_-]{43}/);
  });

  test('the agent log contains no credential material', async () => {
    test.skip(!agentAvailable(), AGENT_SKIP_REASON);

    const credential = readFileSync(credentialPath(), 'utf8').trim();
    const agent = startAgent();
    try {
      await waitForAgentLog(agent, 'connected to the controller');
      expect(agent.log()).not.toContain(credential);
      // The configuration is logged at startup, and prints the credential
      // *path* rather than its contents — which is what an operator
      // diagnosing a permission problem actually needs.
      expect(agent.log()).toContain(AGENT_STATE_DIR);
    } finally {
      agent.stop();
      await waitForAgentExit(agent, 10_000).catch(() => null);
    }
  });
});
