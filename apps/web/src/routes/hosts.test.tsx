import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  expectCsrfHeader,
  installFakeBackend,
  makeHost,
  makeUser,
  userWithRole,
  type FakeBackend,
} from '@/test/fake-backend';
import { renderApp, waitForAuthToResolve } from '@/test/render-app';

/**
 * The host registry, driven through the real API client.
 *
 * Every request goes through `apiRequest` to a stubbed `fetch`, so the
 * query-string construction, the CSRF header, the error mapping and the cache
 * invalidation are all the shipping code.
 *
 * Two things these tests care about more than they care about rendering:
 *
 * - **Nothing is invented.** A host with no heartbeat must not show a date, a
 *   host with no agent version must not show a zero, and the detail page must
 *   not show a resource panel at all. Each is asserted as an absence, because
 *   a fabricated value passes every test about the feature working.
 * - **The credential is shown once and kept nowhere.** Asserted both ways: it
 *   appears after issuing, and it is gone from the document after the dialog
 *   closes and reopens.
 */

let backend: FakeBackend;

const ADMIN = makeUser({ user_id: 'u-admin', username: 'Morgan', role: 'admin' });

/** A fleet with one host in each interesting state. */
function seedHosts() {
  return [
    makeHost({
      host_id: 'host-connected',
      name: 'homeserver',
      environment: 'homelab',
      lifecycle: 'enrolled',
      connectivity: 'connected',
      last_heartbeat_at: new Date(Date.now() - 4000).toISOString(),
      last_connected_at: '2026-10-12T19:00:00+00:00',
      agent_version: '0.1.0',
      platform: 'linux/amd64',
      protocol_version: 1,
      credential_issued_at: '2026-10-12T18:56:00+00:00',
    }),
    makeHost({
      host_id: 'host-stale',
      name: 'backup-nas',
      lifecycle: 'enrolled',
      connectivity: 'stale',
      last_heartbeat_at: '2026-10-12T17:00:00+00:00',
      agent_version: '0.1.0',
      credential_issued_at: '2026-10-11T10:00:00+00:00',
    }),
    makeHost({ host_id: 'host-new', name: 'zeta' }),
    makeHost({
      host_id: 'host-disabled',
      name: 'maintenance',
      lifecycle: 'enrolled',
      is_enabled: false,
      credential_issued_at: '2026-10-10T10:00:00+00:00',
    }),
  ];
}

afterEach(() => {
  backend.restore();
});

async function renderHosts() {
  renderApp('/hosts');
  await waitForAuthToResolve();
  await screen.findByRole('table', { name: /registered hosts/i });
}

describe('the host list', () => {
  beforeEach(() => {
    backend = installFakeBackend({ session: ADMIN, hosts: seedHosts() });
  });

  it('renders every host, ordered by name', async () => {
    await renderHosts();

    const table = screen.getByRole('table', { name: /registered hosts/i });
    const names = within(table)
      .getAllByRole('link')
      .map((link) => link.textContent);

    expect(names).toEqual(['backup-nas', 'homeserver', 'maintenance', 'zeta']);
  });

  it('reports connectivity in words, not only in colour', async () => {
    // Status is never conveyed by colour alone: the dot is `aria-hidden` and
    // the label beside it is what a screen reader and a colour-blind reader
    // both get.
    await renderHosts();

    const table = screen.getByRole('table', { name: /registered hosts/i });
    expect(within(table).getByText('Connected')).toBeInTheDocument();
    expect(within(table).getByText('Stale')).toBeInTheDocument();
  });

  it('shows a disabled host as disabled as well as offline', async () => {
    // Two axes, two badges. A single status column would have to pick one,
    // and an operator needs to know *why* a host is not connected.
    await renderHosts();

    const row = screen.getByRole('row', { name: /maintenance/i });
    expect(within(row).getByText('Disabled')).toBeInTheDocument();
    expect(within(row).getByText('Offline')).toBeInTheDocument();
  });

  it('says "Never" rather than a date for a host that has never reported', async () => {
    // The failure this is guarding against is rendering `null` through a date
    // formatter, which produces 1 January 1970 — a wrong answer that looks
    // like a real measurement.
    await renderHosts();

    const row = screen.getByRole('row', { name: /zeta/i });
    expect(within(row).getByText('Never')).toBeInTheDocument();
    expect(within(row).queryByText(/1970/)).toBeNull();
  });

  it('shows no agent version for a host that has never reported one', async () => {
    await renderHosts();

    const row = screen.getByRole('row', { name: /zeta/i });
    expect(within(row).queryByText(/0\.1\.0/)).toBeNull();
  });

  it('shows a relative time for a recent heartbeat, with the absolute one available', async () => {
    await renderHosts();

    const row = screen.getByRole('row', { name: /homeserver/i });
    const heartbeat = within(row).getByText('just now');

    // The absolute value is kept in the attributes, so nothing is lost by
    // rendering the relative one.
    expect(heartbeat.tagName).toBe('TIME');
    expect(heartbeat).toHaveAttribute('datetime');
    expect(heartbeat).toHaveAttribute('title');
  });

  it('filters by lifecycle through the query string', async () => {
    await renderHosts();

    await userEvent.selectOptions(screen.getByLabelText('Enrolment'), 'registered');

    await waitFor(() => {
      const requests = backend.requestsFor('/hosts');
      expect(requests.at(-1)?.query).toContain('lifecycle=registered');
    });
  });

  it('states the caveat when filtering by connectivity', async () => {
    // The server applies that filter within the page, so the total counts the
    // other filters. Saying so is better than a pager whose arithmetic looks
    // wrong.
    await renderHosts();

    await userEvent.selectOptions(screen.getByLabelText('Connectivity'), 'connected');

    expect(
      await screen.findByText(/filtered within each page/i),
    ).toBeInTheDocument();
  });

  it('offers to clear the filters when nothing matches', async () => {
    await renderHosts();

    await userEvent.type(screen.getByLabelText('Search'), 'nothing-matches-this');

    expect(await screen.findByText(/no hosts match those filters/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /clear filters/i }));
    await screen.findByRole('table', { name: /registered hosts/i });
  });

  it('polls, so connectivity changes appear without a reload', async () => {
    // Connectivity changes without the browser doing anything — an agent
    // connects, a heartbeat goes stale. A list that only loaded once would
    // show a state that is already false.
    await renderHosts();
    const before = backend.requestsFor('/hosts').length;

    backend.state.hosts[2]!.connectivity = 'connected';
    backend.state.hosts[2]!.lifecycle = 'enrolled';

    await waitFor(
      () => {
        expect(backend.requestsFor('/hosts').length).toBeGreaterThan(before);
      },
      { timeout: 15_000 },
    );
  }, 20_000);
});

describe('the empty registry', () => {
  it('invites an administrator to register the first host', async () => {
    backend = installFakeBackend({ session: ADMIN, hosts: [] });
    renderApp('/hosts');
    await waitForAuthToResolve();

    expect(await screen.findByText(/no hosts registered yet/i)).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /register the first host/i }),
    ).toBeInTheDocument();
  });

  it('does not invite a viewer to register one', async () => {
    // The control is hidden because a control that silently vanishes is
    // kinder than one that errors. The server refuses it regardless, which is
    // what makes the hiding a courtesy rather than the protection.
    backend = installFakeBackend({ session: userWithRole('viewer'), hosts: [] });
    renderApp('/hosts');
    await waitForAuthToResolve();

    expect(await screen.findByText(/no hosts registered yet/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /register/i })).toBeNull();
  });
});

describe('a viewer', () => {
  beforeEach(() => {
    backend = installFakeBackend({ session: userWithRole('viewer'), hosts: seedHosts() });
  });

  it('sees the fleet', async () => {
    // A Viewer who cannot see the hosts cannot use a monitoring product.
    await renderHosts();

    expect(screen.getByText('homeserver')).toBeInTheDocument();
  });

  it('is offered no administrative control', async () => {
    await renderHosts();

    expect(screen.queryByRole('button', { name: /register host/i })).toBeNull();
  });
});

describe('registering a host', () => {
  beforeEach(() => {
    backend = installFakeBackend({ session: ADMIN, hosts: [] });
  });

  it('creates the host and then offers to enrol it', async () => {
    // Two steps, deliberately: registering a record and authorising an agent
    // to claim it are different decisions, and bundling them would mean every
    // mistyped name also minted a live secret.
    renderApp('/hosts');
    await waitForAuthToResolve();

    await userEvent.click(await screen.findByRole('button', { name: /register the first host/i }));
    await userEvent.type(screen.getByLabelText('Name'), 'new-host');
    await userEvent.click(screen.getByRole('button', { name: /^register host$/i }));

    const created = backend.requestsFor('/hosts').find((request) => request.method === 'POST');
    expectCsrfHeader(created);
    expect(created?.body).toMatchObject({ name: 'new-host' });

    // The enrollment dialog opens on the new host, with the issue button and
    // no credential yet.
    expect(await screen.findByRole('heading', { name: /enrol new-host/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /issue credential/i })).toBeInTheDocument();
  });

  it("shows the server's own message for a duplicate name", async () => {
    // Seeded before rendering: the empty state has a different button, and a
    // test that rendered first would be asserting against a screen the
    // operator never sees in this scenario.
    backend.state.hosts = [makeHost({ name: 'homeserver' })];
    await renderHosts();

    await userEvent.click(screen.getByRole('button', { name: /register host/i }));
    await userEvent.type(screen.getByLabelText('Name'), 'HomeServer');
    await userEvent.click(screen.getByRole('button', { name: /^register host$/i }));

    // Verbatim from the server, name and all. Guessing at the rule here would
    // produce a form that rejects a name the server would accept — and the
    // server's own sentence is the one that explains *why* a different
    // capitalisation did not help.
    expect(
      await screen.findByText(/The host name 'HomeServer' is already in use/),
    ).toBeInTheDocument();
  });

  it('refuses to submit an empty name without contacting the server', async () => {
    renderApp('/hosts');
    await waitForAuthToResolve();

    await userEvent.click(
      await screen.findByRole('button', { name: /register the first host/i }),
    );
    await userEvent.click(screen.getByRole('button', { name: /^register host$/i }));

    expect(await screen.findByText('Enter a name.')).toBeInTheDocument();
    expect(
      backend.requestsFor('/hosts').filter((request) => request.method === 'POST'),
    ).toHaveLength(0);
  });
});

describe('the enrollment credential', () => {
  const CREDENTIAL = 'TEST-ENROLMENT-CREDENTIAL-0000000000000000';

  beforeEach(() => {
    backend = installFakeBackend({
      session: ADMIN,
      hosts: [makeHost({ host_id: 'host-new', name: 'zeta' })],
      enrollmentCredential: CREDENTIAL,
    });
  });

  async function openEnrollment() {
    renderApp('/hosts/host-new');
    await waitForAuthToResolve();
    await userEvent.click(
      await screen.findByRole('button', { name: /issue enrolment credential/i }),
    );
    await screen.findByRole('heading', { name: /enrol zeta/i });
  }

  it('is not fetched until it is asked for', async () => {
    // A credential must not be minted by navigating to a page. Issuing one is
    // an action with a consequence — it revokes any pending credential — so it
    // happens on a click and nowhere else.
    await openEnrollment();

    expect(screen.queryByText(CREDENTIAL)).toBeNull();
    expect(
      backend.requests.filter((request) => request.path.endsWith('/enrollment')),
    ).toHaveLength(0);
  });

  it('is shown once, with the warning that it cannot be retrieved', async () => {
    await openEnrollment();

    await userEvent.click(screen.getByRole('button', { name: /issue credential/i }));

    expect(await screen.findByText(CREDENTIAL)).toBeInTheDocument();
    expect(screen.getByText(/only time the credential is shown/i)).toBeInTheDocument();
    expectCsrfHeader(
      backend.requests.find((request) => request.path.endsWith('/enrollment')),
    );
  });

  it('is gone from the document once the dialog is closed', async () => {
    // The dialog is remounted per open, so the credential cannot survive in
    // component state. If it could, closing and reopening would show a secret
    // that may already have been superseded.
    await openEnrollment();
    await userEvent.click(screen.getByRole('button', { name: /issue credential/i }));
    await screen.findByText(CREDENTIAL);

    await userEvent.click(screen.getByRole('button', { name: /^done$/i }));

    await waitFor(() => {
      expect(screen.queryByText(CREDENTIAL)).toBeNull();
    });

    // Reopening shows the issue button again, not the previous credential.
    await userEvent.click(
      screen.getByRole('button', { name: /issue enrolment credential/i }),
    );
    expect(await screen.findByRole('button', { name: /issue credential/i })).toBeInTheDocument();
    expect(screen.queryByText(CREDENTIAL)).toBeNull();
  });

  it('never appears in the URL', async () => {
    await openEnrollment();
    await userEvent.click(screen.getByRole('button', { name: /issue credential/i }));
    await screen.findByText(CREDENTIAL);

    // Nothing in the client puts it there, and nothing should: a URL is in the
    // browser's history, in the `Referer` of the next click, and in the access
    // log of every proxy in between.
    expect(window.location.href).not.toContain(CREDENTIAL);
    for (const request of backend.requests) {
      expect(request.path).not.toContain(CREDENTIAL);
      expect(request.query).not.toContain(CREDENTIAL);
    }
  });

  it('turns an already-enrolled host into a confirmation, not an error', async () => {
    backend.state.hosts = [
      makeHost({
        host_id: 'host-new',
        name: 'zeta',
        lifecycle: 'enrolled',
        credential_issued_at: '2026-10-11T10:00:00+00:00',
      }),
    ];
    renderApp('/hosts/host-new');
    await waitForAuthToResolve();
    await userEvent.click(await screen.findByRole('button', { name: /re-enrol/i }));
    await userEvent.click(screen.getByRole('button', { name: /issue credential/i }));

    // The 409 becomes the decision the operator has not been asked for yet,
    // with the consequence stated.
    expect(
      await screen.findByText(/already has an enrolled agent/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/will be\s+disconnected/i)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /re-enrol anyway/i }));
    expect(await screen.findByText(CREDENTIAL)).toBeInTheDocument();
  });

  it('reports when issuing revoked a previous pending credential', async () => {
    backend.state.hosts = [
      makeHost({ host_id: 'host-new', name: 'zeta', has_pending_enrollment: true }),
    ];
    renderApp('/hosts/host-new');
    await waitForAuthToResolve();
    await userEvent.click(
      await screen.findByRole('button', { name: /issue enrolment credential/i }),
    );

    // Warned before, so the operator can stop.
    expect(screen.getByText(/already has a pending credential/i)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /issue credential/i }));

    // And told after, because an administrator who issued one ten minutes ago
    // needs to know it is dead rather than find out when an agent fails.
    expect(await screen.findByText(/previous credential was revoked/i)).toBeInTheDocument();
  });
});

describe('the install instructions beside the credential', () => {
  const CREDENTIAL = 'TEST-ENROLMENT-CREDENTIAL-0000000000000000';
  const DIGEST_AMD64 = 'a'.repeat(64);
  const DIGEST_ARM64 = 'b'.repeat(64);

  const RELEASES = {
    recommended_version: '0.2.0',
    platforms: [
      {
        platform: 'linux/amd64',
        archive: 'hcc-agent_0.2.0_linux_amd64.tar.gz',
        url: 'https://downloads.example.test/hcc-agent_0.2.0_linux_amd64.tar.gz',
        sha256: DIGEST_AMD64,
        size_bytes: 2931200,
        tier: 'supported' as const,
      },
      {
        platform: 'linux/arm64',
        archive: 'hcc-agent_0.2.0_linux_arm64.tar.gz',
        url: 'https://downloads.example.test/hcc-agent_0.2.0_linux_arm64.tar.gz',
        sha256: DIGEST_ARM64,
        size_bytes: 2651026,
        tier: 'supported' as const,
      },
      {
        platform: 'linux/arm',
        archive: 'hcc-agent_0.2.0_linux_arm.tar.gz',
        url: 'https://downloads.example.test/hcc-agent_0.2.0_linux_arm.tar.gz',
        sha256: 'c'.repeat(64),
        size_bytes: 2828214,
        tier: 'best-effort' as const,
      },
    ],
  };

  async function issue() {
    renderApp('/hosts/host-new');
    await waitForAuthToResolve();
    await userEvent.click(
      await screen.findByRole('button', { name: /issue enrolment credential/i }),
    );
    await screen.findByRole('heading', { name: /enrol zeta/i });
    await userEvent.click(screen.getByRole('button', { name: /issue credential/i }));
    await screen.findByText(CREDENTIAL);
  }

  describe('when the controller has a release to offer', () => {
    beforeEach(() => {
      backend = installFakeBackend({
        session: ADMIN,
        hosts: [makeHost({ host_id: 'host-new', name: 'zeta' })],
        enrollmentCredential: CREDENTIAL,
        agentReleases: RELEASES,
      });
    });

    it('shows the download command with the digest, defaulting to amd64', async () => {
      await issue();

      // The digest is the whole trust model: it reaches the operator inside
      // the session they are already authenticated in, so an attacker who
      // controls the download origin cannot make a substituted tarball match.
      expect(await screen.findByText(new RegExp(DIGEST_AMD64))).toBeInTheDocument();
      expect(screen.getByText(/hcc-agent_0\.2\.0_linux_amd64\.tar\.gz/)).toBeInTheDocument();
      expect(screen.getByText(/sha256sum -c -/)).toBeInTheDocument();
    });

    it('switches the command when another architecture is chosen', async () => {
      await issue();

      await userEvent.selectOptions(
        screen.getByLabelText(/architecture of the host/i),
        'linux/arm64',
      );

      expect(await screen.findByText(new RegExp(DIGEST_ARM64))).toBeInTheDocument();
      // And the previous architecture's digest is gone, rather than both
      // being on screen for somebody to copy the wrong one of.
      expect(screen.queryByText(new RegExp(DIGEST_AMD64))).toBeNull();
    });

    it('says which architectures nobody has run on real hardware', async () => {
      await issue();

      await userEvent.selectOptions(
        screen.getByLabelText(/architecture of the host/i),
        'linux/arm',
      );

      // `best-effort` is a statement about evidence, not a preference. A
      // Raspberry Pi owner is entitled to know it has only ever been emulated.
      expect(await screen.findByText(/best-effort/i)).toBeInTheDocument();
      expect(screen.getByText(/never run on real hardware/i)).toBeInTheDocument();
    });

    it('has a copy button for the command, separate from the credential', async () => {
      await issue();

      // Two buttons, each labelled with what it copies. Two unlabelled copy
      // buttons invites pasting the secret where the command belongs.
      expect(screen.getByRole('button', { name: /copy command/i })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /copy credential/i })).toBeInTheDocument();
      expect(screen.getByText(/this command contains no secret/i)).toBeInTheDocument();
    });

    it('never puts the credential in the install command', async () => {
      await issue();

      // The one assertion that would matter most if it ever broke. The
      // command is meant to be safe in shell history; the credential is not.
      const command = screen.getByText(/sha256sum -c -/).textContent;
      expect(command).not.toContain(CREDENTIAL);
      expect(command).not.toMatch(/--token[ =]/);
    });
  });

  describe('when no release has been published', () => {
    beforeEach(() => {
      // The default, and the state of every build of this project so far.
      backend = installFakeBackend({
        session: ADMIN,
        hosts: [makeHost({ host_id: 'host-new', name: 'zeta' })],
        enrollmentCredential: CREDENTIAL,
      });
    });

    it('says so and shows how to build the agent instead', async () => {
      await issue();

      expect(await screen.findByText(/no prebuilt agent is available/i)).toBeInTheDocument();
      expect(screen.getByText(/make agent-build/)).toBeInTheDocument();
    });

    it('invents no download URL and no digest', async () => {
      await issue();

      // A command that looks real and 404s costs an operator more time than a
      // paragraph telling them to build it — and a digest beside an invented
      // URL is a claim nobody can check.
      expect(screen.queryByText(/curl -fsSLO https:\/\/github/)).toBeNull();
      expect(screen.queryByText(/sha256sum -c -/)).toBeNull();
    });

    it('still shows the credential, because that is the part that expires', async () => {
      await issue();

      // The install command is a convenience; the credential cannot be
      // obtained again. A failure to render the first must not withhold the
      // second.
      expect(screen.getByText(CREDENTIAL)).toBeInTheDocument();
    });
  });

  describe('the expiry countdown', () => {
    beforeEach(() => {
      backend = installFakeBackend({
        session: ADMIN,
        hosts: [makeHost({ host_id: 'host-new', name: 'zeta' })],
        enrollmentCredential: CREDENTIAL,
      });
    });

    it('counts down from the server’s own figure', async () => {
      await issue();

      // Counted from `expires_in_seconds` rather than from `expires_at` minus
      // the browser's clock: a host whose clock is minutes out would
      // otherwise be shown an expiry that is wrong in whichever direction its
      // clock is wrong, and "expired" on a credential that is fine is the
      // worse of the two.
      expect(screen.getByText(/single use\. expires in/i)).toBeInTheDocument();
      expect(screen.getByText(/^\d+m \d{2}s$/)).toBeInTheDocument();
    });
  });
});
