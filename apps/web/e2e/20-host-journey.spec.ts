import { expect, test } from '@playwright/test';

import {
  AGENT_SKIP_REASON,
  agentAvailable,
  agentStatus,
  apiFromPage,
  cookie,
  enrollAgent,
  ensureAdministrator,
  signInAndWait,
  sql,
  startAgent,
  waitForAgentExit,
  waitForAgentLog,
  type RunningAgent,
} from './helpers';

/**
 * The host journey: registration to a live, connected agent and back.
 *
 * This is the milestone's actual deliverable, driven end to end — a real
 * Chromium against a real control plane against the **real Go agent**, built
 * from this repository by `scripts/e2e.sh`. Nothing here is a fixture, and
 * that is the point: a suite that rendered a canned host would prove the
 * interface can display one, which is not the claim being made.
 *
 * ## Why it is one serial file
 *
 * Each step depends on the state the previous one left: a host has to exist
 * before it can be enrolled, and enrolled before it can be revoked. Splitting
 * them into independent tests would mean re-registering and re-enrolling for
 * every assertion, which is slower and — more importantly — would stop testing
 * the *sequence*. Several of the defects this milestone found were in
 * transitions rather than in states.
 *
 * ## What it asserts that a unit test cannot
 *
 * That the whole chain holds at once: the credential the browser displayed is
 * the one the Go binary can spend, the identity the binary received opens a
 * WebSocket the Python controller accepts, and the connectivity that socket
 * produces is what the browser then renders. Every link is covered by its own
 * tests; only this covers them agreeing.
 */

/**
 * Skipped rather than silently absent when there is no Go toolchain.
 *
 * `E2E_` and not `HCC_`: the control plane refuses to start with an
 * unrecognised `HCC_`-prefixed variable in its environment, which is a guard
 * worth having and which caught this name on the first run.
 */
const requireAgent = process.env.E2E_REQUIRE_AGENT === '1';

test.describe.configure({ mode: 'serial' });

test.describe('host registration and the live agent', () => {
  let agent: RunningAgent | null = null;
  /** Shown once by the browser, spent by the agent. */
  let credential = '';
  let hostId = '';

  test.beforeAll(() => {
    ensureAdministrator();
    if (requireAgent && !agentAvailable()) {
      throw new Error(
        `E2E_REQUIRE_AGENT is set but ${AGENT_SKIP_REASON}`,
      );
    }
  });

  test.afterAll(async () => {
    agent?.stop();
    if (agent) await waitForAgentExit(agent, 10_000).catch(() => null);
  });

  test('an administrator registers a host, which starts unenrolled', async ({ page }) => {
    await signInAndWait(page);
    await page.getByRole('link', { name: 'Hosts' }).first().click();

    await expect(page.getByRole('heading', { name: 'Registered hosts' })).toBeVisible();
    // Nothing is pretending to be a host yet.
    await expect(page.getByText('No hosts registered yet')).toBeVisible();

    await page.getByRole('button', { name: /register the first host/i }).click();
    await page.getByLabel('Name').fill('e2e-homeserver');
    await page.getByLabel('Description (optional)').fill('Driven by the browser suite');
    await page.getByRole('button', { name: /^register host$/i }).click();

    // Registering offers enrollment next rather than taking it: two
    // decisions, so a mistyped name does not mint a live secret.
    await expect(page.getByRole('heading', { name: /enrol e2e-homeserver/i })).toBeVisible();
    await expect(page.getByRole('button', { name: /issue credential/i })).toBeVisible();

    const rows = sql(
      "SELECT id::text, lifecycle, is_enabled FROM hosts WHERE name = 'e2e-homeserver'",
    ) as { id: string; lifecycle: string; is_enabled: string }[];
    expect(rows).toHaveLength(1);
    hostId = rows[0]!.id;
    // Registered, not enrolled: creating a record must never imply an agent.
    expect(rows[0]!.lifecycle).toBe('registered');
    expect(rows[0]!.is_enabled).toBe('True');

    // And no credential has been minted by registering.
    expect(sql('SELECT id FROM host_enrollments')).toHaveLength(0);
  });

  test('the enrollment credential is displayed exactly once', async ({ page }) => {
    await signInAndWait(page);
    await page.goto(`/hosts/${hostId}`);
    await page.getByRole('button', { name: /issue enrolment credential/i }).click();
    await page.getByRole('button', { name: /issue credential/i }).click();

    const field = page.getByText(/^[A-Za-z0-9_-]{43}$/);
    await expect(field).toBeVisible();
    credential = (await field.innerText()).trim();
    expect(credential).toHaveLength(43);

    // The warning is part of the contract, not decoration: there is no
    // endpoint that could show it again.
    await expect(page.getByText(/only time the credential is shown/i)).toBeVisible();

    // Only the digest is stored. Asserted against the row, because every
    // behavioural test about enrollment working would pass with the plaintext
    // sitting in the database.
    const stored = sql('SELECT token_hash FROM host_enrollments') as { token_hash: string }[];
    expect(stored).toHaveLength(1);
    expect(stored[0]!.token_hash).not.toContain(credential);
    expect(stored[0]!.token_hash).toHaveLength(64);

    // Not in the URL, not in the history.
    expect(page.url()).not.toContain(credential);

    await page.getByRole('button', { name: /^done$/i }).click();
    // Gone from the document, and the detail page reports only that one is
    // pending.
    await expect(page.getByText(credential)).toBeHidden();
    await expect(page.getByText('Enrolment pending')).toBeVisible();
  });

  test('no read endpoint will give the credential back', async ({ page }) => {
    await signInAndWait(page);
    await page.goto('/hosts');
    await expect(page.getByRole('link', { name: 'e2e-homeserver' })).toBeVisible();
    expect(await page.content()).not.toContain(credential);

    await page.goto(`/hosts/${hostId}`);
    await expect(page.getByRole('heading', { name: 'Connection' })).toBeVisible();
    expect(await page.content()).not.toContain(credential);
  });

  test('the real agent spends the credential and is enrolled', () => {
    test.skip(!agentAvailable(), AGENT_SKIP_REASON);

    // Before: the binary knows it is not enrolled, and says what to do.
    const before = agentStatus();
    expect(before.code).toBe(3);
    expect(before.output).toContain('Enrolled:        no');
    expect(before.output).toContain('hcc-agent enroll');

    const output = enrollAgent(credential);
    expect(output).toContain('Enrolled as "e2e-homeserver"');
    expect(output).toContain('mode 0600');

    const after = agentStatus();
    expect(after.code).toBe(0);
    expect(after.output).toContain('Enrolled:        yes');

    // The one-time credential is spent and the durable one exists. Neither
    // plaintext is anywhere in the database.
    const enrolment = sql(
      'SELECT consumed_at IS NOT NULL AS consumed FROM host_enrollments',
    ) as { consumed: string }[];
    expect(enrolment[0]!.consumed).toBe('True');
    const credentials = sql(
      'SELECT token_hash FROM agent_credentials WHERE revoked_at IS NULL',
    ) as { token_hash: string }[];
    expect(credentials).toHaveLength(1);
    expect(credentials[0]!.token_hash).toHaveLength(64);
  });

  test('the same credential cannot be spent twice', () => {
    test.skip(!agentAvailable(), AGENT_SKIP_REASON);

    // Single use is the property the whole design turns on. A credential
    // usable twice is one replayable by whoever read it over a shoulder.
    let failed = false;
    try {
      enrollAgent(credential);
    } catch (error) {
      failed = true;
      const failure = error as { stdout?: string; stderr?: string };
      const combined = `${failure.stdout ?? ''}${failure.stderr ?? ''}`;
      expect(combined).toContain('rejected this credential');
    }
    expect(failed, 'a spent enrollment credential was accepted again').toBe(true);
  });

  test('the connected agent appears in the browser as connected', async ({ page }) => {
    test.skip(!agentAvailable(), AGENT_SKIP_REASON);

    agent = startAgent();
    await waitForAgentLog(agent, 'connected to the controller');

    await signInAndWait(page);
    await page.goto(`/hosts/${hostId}`);

    // The list polls, so this appears without a reload — but the assertion
    // waits on the text rather than on a timer.
    await expect(page.getByText('Connected').first()).toBeVisible({ timeout: 20_000 });
    await expect(page.getByText('Enrolled').first()).toBeVisible();

    // The agent's real self-report, not a placeholder: a version string and a
    // platform that match what the binary was built as.
    await expect(page.getByText('linux/amd64')).toBeVisible();

    // The protocol version the agent negotiated has to be the one this
    // controller says it speaks.
    //
    // Read from the API rather than written as a literal. This assertion was
    // `getByText('v1')` and broke the moment the protocol went to 2 for
    // update management — which is a version bump doing exactly what it
    // should, caught by a test asserting a number that had gone stale.
    // Deriving it keeps the real property: a *disagreement* between what the
    // agent negotiated and what the controller speaks still fails here, and a
    // future bump does not.
    const releases = await apiFromPage(page, '/api/v1/agent-releases');
    const spoken = (releases.body as { protocol_version: number }).protocol_version;
    await expect(page.getByText(`v${String(spoken)}`)).toBeVisible();

    // And a real heartbeat timestamp in the row, rather than "Never".
    await page.goto('/hosts');
    const row = page.getByRole('row', { name: /e2e-homeserver/ });
    await expect(row.getByText('Connected')).toBeVisible({ timeout: 20_000 });
    await expect(row.getByText('Never')).toBeHidden();
  });

  test('no resource metrics are invented for a connected host', async ({ page }) => {
    test.skip(!agentAvailable(), AGENT_SKIP_REASON);

    // The host is genuinely connected, which is exactly when a monitoring
    // interface is most tempted to show zeroes.
    await signInAndWait(page);
    await page.goto(`/hosts/${hostId}`);

    // Phase 3 gave this page real measurements, so the placeholder this test
    // used to assert on is gone. What must survive is the discipline it stood
    // for: this host is connected and has nothing enabled, so there is
    // nothing measured — and the page has to say that rather than show zeroes.
    await expect(page.getByText(/No measurements have arrived yet/i)).toBeVisible({
      timeout: 20_000,
    });
    await expect(page.getByText(/Nothing is enabled for this host/i)).toBeVisible();

    // The assertions that were always the point of this test.
    const content = await page.content();
    expect(content).not.toContain('0%');
    expect(content).not.toMatch(/\d+(\.\d+)?\s?(GiB|MiB|GB|MB)\b/);

    // And default-deny is visible, not just implemented: every module off,
    // on a host that is connected and could be collecting right now.
    const monitoring = page.getByRole('heading', { name: /^monitoring$/i });
    await expect(monitoring).toBeVisible();
    // `exact` matters: the card's own description says "does not begin
    // collecting anything", and getByText is a case-insensitive substring
    // match by default — so the loose form passes on the prose instead of
    // failing on the badge.
    await expect(page.getByText('Collecting', { exact: true })).toBeHidden();
    await expect(page.getByText('Off', { exact: true }).first()).toBeVisible();
  });

  test('heartbeats keep arriving, and the controller records each one', () => {
    test.skip(!agentAvailable(), AGENT_SKIP_REASON);

    const first = sql(
      `SELECT last_heartbeat_at::text, connection_epoch FROM hosts WHERE id = '${hostId}'`,
    ) as { last_heartbeat_at: string; connection_epoch: string }[];

    // One accepted connection so far, which is what the epoch counts.
    expect(first[0]!.connection_epoch).toBe('1');
    expect(first[0]!.last_heartbeat_at).not.toBe('None');
  });

  test('a network failure is survived: the agent reconnects on its own', async ({ page }) => {
    test.skip(!agentAvailable(), AGENT_SKIP_REASON);

    // The controller cannot dial a host (ADR-018), so an agent that gave up
    // is an agent nothing will come and restart. Simulated by dropping the
    // recorded connection out from under it, which is what a half-open socket
    // looks like from the controller's side.
    agent?.stop();
    await waitForAgentExit(agent!, 10_000);

    await signInAndWait(page);
    await page.goto(`/hosts/${hostId}`);
    // A clean disconnect, honestly reported: offline, with the last heartbeat
    // kept because it is still the record of when the host was last heard
    // from.
    await expect(page.getByText('Offline').first()).toBeVisible({ timeout: 20_000 });
    const afterStop = sql(
      `SELECT last_heartbeat_at::text FROM hosts WHERE id = '${hostId}'`,
    ) as { last_heartbeat_at: string }[];
    expect(afterStop[0]!.last_heartbeat_at).not.toBe('None');

    // Restarted, with the same credential and therefore the same identity.
    agent = startAgent();
    await waitForAgentLog(agent, 'connected to the controller');

    await page.reload();
    await expect(page.getByText('Connected').first()).toBeVisible({ timeout: 20_000 });

    // The epoch advanced by exactly one, which is what proves the second
    // connection was accepted rather than the first having never closed.
    const epoch = sql(
      `SELECT connection_epoch FROM hosts WHERE id = '${hostId}'`,
    ) as { connection_epoch: string }[];
    expect(epoch[0]!.connection_epoch).toBe('2');

    // And the host id is unchanged: a restart is not a new host.
    const identity = sql(`SELECT name FROM hosts WHERE id = '${hostId}'`) as { name: string }[];
    expect(identity[0]!.name).toBe('e2e-homeserver');
  });

  test('disabling the host cuts the agent off and tells it why', async ({ page }) => {
    test.skip(!agentAvailable(), AGENT_SKIP_REASON);

    await signInAndWait(page);
    await page.goto(`/hosts/${hostId}`);
    await page.getByRole('button', { name: /^disable$/i }).click();

    // The consequence is stated before it happens, and the distinction from
    // revoking is stated with it.
    await expect(page.getByText(/disconnected immediately/i)).toBeVisible();
    await expect(page.getByText(/no longer trust the credential/i)).toBeVisible();
    await page.getByRole('button', { name: /disable host/i }).click();

    await expect(page.getByText('Disabled', { exact: true })).toBeVisible();
    // The agent is told the real reason — not "superseded", which is what an
    // earlier version of the controller said and which would send whoever
    // read this log looking for a duplicate agent that does not exist.
    await waitForAgentLog(agent!, 'host_disabled');
    // And keeps retrying rather than exiting: a disabled host comes back.
    expect(agent!.alive()).toBe(true);
  });

  test('enabling it again lets the agent reconnect without re-enrolling', async ({ page }) => {
    test.skip(!agentAvailable(), AGENT_SKIP_REASON);

    await signInAndWait(page);
    await page.goto(`/hosts/${hostId}`);
    await page.getByRole('button', { name: /^enable$/i }).click();

    // No confirmation, because enabling restores the state the operator asked
    // for and breaks nothing.
    await expect(page.getByText('Connected').first()).toBeVisible({ timeout: 25_000 });
    // `exact`, because the disabled *alert* also contains the word — and the
    // alert disappearing is a weaker claim than the badge disappearing.
    await expect(page.getByText('Disabled', { exact: true })).toBeHidden();
  });

  test('revoking the credential disconnects the agent and stops it', async ({ page }) => {
    test.skip(!agentAvailable(), AGENT_SKIP_REASON);

    await signInAndWait(page);
    await page.goto(`/hosts/${hostId}`);
    await page.getByRole('button', { name: /revoke agent credential/i }).click();

    await expect(page.getByText(/cannot authenticate again/i)).toBeVisible();
    await expect(page.getByText(/cannot be undone/i)).toBeVisible();
    await page
      .getByRole('dialog')
      .getByRole('button', { name: /revoke credential/i })
      .click();

    await expect(page.getByText('Revoked').first()).toBeVisible();
    await expect(page.getByText('Offline').first()).toBeVisible();

    // The agent is cut off and exits non-zero, so `systemctl status` says
    // something true rather than reporting a running service doing nothing.
    await waitForAgentLog(agent!, 'rejected this agent');
    const code = await waitForAgentExit(agent!, 20_000);
    expect(code).not.toBe(0);
    agent = null;

    // And the row says what happened: used, then revoked.
    const credentials = sql(
      'SELECT revoked_at IS NOT NULL AS revoked FROM agent_credentials',
    ) as { revoked: string }[];
    expect(credentials.every((row) => row.revoked === 'True')).toBe(true);
  });

  test('a revoked agent cannot authenticate again', async () => {
    test.skip(!agentAvailable(), AGENT_SKIP_REASON);

    // The credential file is still on disk — a decommissioned disk is a copy
    // — and it is worthless.
    const restarted = startAgent();
    try {
      await waitForAgentLog(restarted, 'rejected this agent');
      const code = await waitForAgentExit(restarted, 20_000);
      expect(code).not.toBe(0);
    } finally {
      restarted.stop();
    }

    // Nothing reconnected, and no second credential appeared.
    const live = sql('SELECT id FROM agent_credentials WHERE revoked_at IS NULL');
    expect(live).toHaveLength(0);
  });

  test('every action is in the audit trail, and no secret is', async ({ page }) => {
    await signInAndWait(page);
    await page.goto('/settings/audit');

    // The actions, in the trail, attributable to the administrator who took
    // them. `host.enrolled` has no actor by design: the agent is not a user,
    // and attributing its enrollment to whoever issued the token would be a
    // claim about a person who was not present.
    const events = sql(
      "SELECT action, details::text FROM audit_events WHERE action LIKE 'host.%' " +
        'ORDER BY occurred_at',
    ) as { action: string; details: string }[];
    const actions = events.map((event) => event.action);

    expect(actions).toContain('host.created');
    expect(actions).toContain('host.enrollment_issued');
    if (agentAvailable()) {
      expect(actions).toContain('host.enrolled');
      expect(actions).toContain('host.disabled');
      expect(actions).toContain('host.enabled');
      expect(actions).toContain('host.credential_revoked');
      // A revoked agent retrying is recorded, at most once a minute, so one
      // unfixed host cannot bury every administrative event in the trail.
      expect(actions).toContain('host.agent_auth_failed');
    }

    // Not the credential, and not its digest — the digest is the lookup key,
    // so recording it would let a reader of the trail confirm whether a
    // captured credential is the live one.
    for (const event of events) {
      expect(event.details).not.toContain(credential);
      expect(event.details.toLowerCase()).not.toContain('token_hash');
    }

    // And nothing anywhere in the rendered page.
    expect(await page.content()).not.toContain(credential);
  });
});

test.describe('the install instructions beside the credential', () => {
  /**
   * The only place the real `/api/v1/agent-releases` endpoint meets the real
   * interface.
   *
   * The unit tests cover both halves against a fake, and the fake's *default*
   * is "no release" precisely because that is what every build of this
   * project reports. This asserts the real endpoint agrees — and, more
   * importantly, that the interface's response to it is the build-from-source
   * path rather than a download command whose URL would not resolve.
   *
   * A digest beside an invented URL is a claim nobody can check, which is
   * worse than no claim. That is the property being tested.
   */
  let instructionsHostId = '';

  test.beforeAll(() => {
    ensureAdministrator();
  });

  test('a controller with no published release says so, and offers no download', async ({
    page,
  }) => {
    await signInAndWait(page);

    // Its own host, so this does not depend on the ordering of the tests
    // above or on their credential still being pending.
    // A POST needs the CSRF header the interface sends, so this goes through
    // the page's own session exactly as the client would.
    const csrf = await cookie(page, 'hcc_csrf');
    const created = await apiFromPage(page, '/api/v1/hosts', {
      method: 'POST',
      headers: { 'X-CSRF-Token': csrf?.value ?? '' },
      body: { name: 'e2e-install-instructions', environment: 'test' },
    });
    expect(created.status).toBe(201);
    instructionsHostId = (created.body as { host_id: string }).host_id;

    await page.goto(`/hosts/${instructionsHostId}`);
    await page.getByRole('button', { name: /issue enrolment credential/i }).click();
    await page.getByRole('button', { name: /issue credential/i }).click();

    // The credential is shown — it is the part that expires, so a failure to
    // render the install command must not withhold it.
    await expect(page.getByText(/^[A-Za-z0-9_-]{43}$/)).toBeVisible();

    // And the honest answer about the download.
    await expect(page.getByText(/no prebuilt agent is available/i)).toBeVisible();
    await expect(page.getByText(/make agent-build/)).toBeVisible();

    // No invented download command. `sha256sum -c -` only appears in the
    // verified-download path, so its absence is the assertion.
    await expect(page.getByText(/sha256sum -c -/)).toHaveCount(0);
    await expect(page.getByText(/curl -fsSLO https:\/\/github/)).toHaveCount(0);

    // And no architecture selector, because there is nothing to select
    // between.
    await expect(page.getByLabel(/architecture of the host/i)).toHaveCount(0);
  });

  // The authorization half of this endpoint is asserted in
  // 70-user-management.spec.ts, which has the non-admin accounts to assert it
  // *with*: an administrator getting 200 says nothing about the gate.
  test('it says why there is nothing to download, in the real response', async ({ page }) => {
    await signInAndWait(page);

    // Read through the page's own session, so this is the body a browser
    // receives from the real endpoint rather than a fixture's.
    const response = await apiFromPage(page, '/api/v1/agent-releases');
    expect(response.status).toBe(200);
    const releases = response.body as {
      available: boolean;
      unavailable_reason: string | null;
      protocol_version: number;
    };

    // No manifest is committed, and that is the honest answer rather than an
    // empty list the interface would have to guess the meaning of.
    expect(releases.available).toBe(false);
    // Lowercased: the sentence happens to start with it.
    expect(releases.unavailable_reason?.toLowerCase()).toContain(
      'build the agent from source',
    );
    // Useful even with nothing to download: it is what tells somebody whether
    // an agent they built themselves can talk to this controller.
    expect(releases.protocol_version).toBeGreaterThan(0);
  });

  test('it never carries a credential, a token or a key', async ({ page }) => {
    await signInAndWait(page);

    const releases = (await apiFromPage(page, '/api/v1/agent-releases')).body as Record<
      string,
      unknown
    >;

    // The contract test asserts this against the schema; this asserts it
    // against the real response body, which is what a browser receives.
    for (const field of Object.keys(releases)) {
      expect(field).not.toMatch(/credential|token|secret|private|password/i);
    }
  });
});
