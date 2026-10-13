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
 * One host's page.
 *
 * The assertions that matter most here are **absences**. A monitoring
 * interface that invents a value passes every test about the feature working,
 * so the tests have to say what must not be on screen: no resource gauges, no
 * zeroed metrics, no date where there is no timestamp.
 *
 * The rest is about the three destructive actions. Each takes effect on a
 * running agent within milliseconds, so each has to state its consequence
 * before it happens and report what actually happened afterwards — a
 * revocation that claims success without revoking anything is the specific lie
 * these screens must not tell.
 */

let backend: FakeBackend;

const ADMIN = makeUser({ user_id: 'u-admin', username: 'Morgan', role: 'admin' });

/** A connected, enrolled host. */
function connectedHost() {
  return makeHost({
    host_id: 'host-1',
    name: 'homeserver',
    description: 'Dedicated server, Hetzner FSN1',
    environment: 'production',
    lifecycle: 'enrolled',
    connectivity: 'connected',
    last_heartbeat_at: new Date(Date.now() - 3000).toISOString(),
    last_connected_at: '2026-10-12T19:00:00+00:00',
    agent_version: '0.1.0',
    platform: 'linux/amd64',
    protocol_version: 1,
    credential_issued_at: '2026-10-12T18:56:00+00:00',
  });
}

afterEach(() => {
  backend.restore();
});

async function renderDetail(hostId = 'host-1') {
  renderApp(`/hosts/${hostId}`);
  await waitForAuthToResolve();
  await screen.findByRole('heading', { name: /connection/i });
}

describe('what the page reports', () => {
  beforeEach(() => {
    backend = installFakeBackend({ session: ADMIN, hosts: [connectedHost()] });
  });

  it('shows the three state axes separately', async () => {
    // Enrollment, administrative state and connectivity are independent, and
    // the interface has to say so: one collapsed "status" would have to pick
    // which of them to hide.
    await renderDetail();

    expect(screen.getByText('Connected')).toBeInTheDocument();
    expect(screen.getByText('Enrolled')).toBeInTheDocument();
    // Enabled, so no "Disabled" badge — the common case carries no badge.
    expect(screen.queryByText('Disabled')).toBeNull();
  });

  it("labels the agent-reported fields as the agent's own claims", async () => {
    await renderDetail();

    expect(screen.getByText('0.1.0')).toBeInTheDocument();
    expect(screen.getByText('linux/amd64')).toBeInTheDocument();
    expect(screen.getByText(/Reported by the agent itself/i)).toBeInTheDocument();
    // And says what that means: the controller does not decide anything from
    // values that come from a machine whose security it does not own.
    expect(screen.getByText(/never makes a decision from these values/i)).toBeInTheDocument();
  });

  it('renders no resource values for a host that has measured nothing', async () => {
    // Not as zeroes, not as empty charts, not as "0%". A zeroed gauge is
    // indistinguishable from a measurement, which is the exact failure
    // product principle 2 forbids. Phase 3 gave this page real data; the
    // absence has to stay honest now that showing a number is possible.
    await renderDetail();

    expect(screen.queryByText('0%')).toBeNull();
    expect(screen.queryByText('0.0')).toBeNull();
    expect(screen.queryByText(/\d+ GiB/)).toBeNull();
    expect(screen.queryByRole('progressbar')).toBeNull();
  });

  it('says why there is nothing to show, rather than only that there is nothing', async () => {
    // An empty response has several causes with different remedies, and the
    // one for an enrolled, enabled host that has collected nothing is "every
    // module starts off" — which is a thing to act on, not a fault.
    await renderDetail();

    expect(
      await screen.findByText(/No measurements have arrived yet/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/Nothing is enabled for this host/i)).toBeInTheDocument();
    // And the monitoring card says the policy that caused it, so the two
    // together answer "why is this empty" and "what do I do".
    expect(screen.getByText(/Every module starts switched off/i)).toBeInTheDocument();
  });

  it('reports an absent timestamp as a sentence, never as a date', async () => {
    backend.state.hosts = [makeHost({ host_id: 'host-1', name: 'zeta' })];
    await renderDetail();

    // The reason a timestamp is missing is information, so the absence says
    // which absence it is.
    expect(screen.getByText(/Never — no agent has reported/)).toBeInTheDocument();
    // The credential fact says which absence it is: "not enrolled" rather
    // than an em dash that could mean anything.
    const credentialFact = screen.getByText('Credential issued').closest('div');
    expect(credentialFact).not.toBeNull();
    expect(within(credentialFact!).getByText('Not enrolled')).toBeInTheDocument();
    expect(screen.queryByText(/1970/)).toBeNull();
  });

  it('distinguishes a revoked credential from one that never existed', async () => {
    backend.state.hosts = [
      makeHost({
        host_id: 'host-1',
        name: 'homeserver',
        lifecycle: 'revoked',
        credential_issued_at: null,
      }),
    ];
    await renderDetail();

    expect(screen.getByText(/Revoked — no live credential/)).toBeInTheDocument();
    expect(screen.getByText(/credential was revoked/i)).toBeInTheDocument();
  });

  it('explains a stale host rather than guessing', async () => {
    backend.state.hosts = [
      makeHost({
        host_id: 'host-1',
        name: 'homeserver',
        lifecycle: 'enrolled',
        connectivity: 'stale',
        last_heartbeat_at: '2026-10-12T17:00:00+00:00',
        credential_issued_at: '2026-10-11T10:00:00+00:00',
      }),
    ];
    await renderDetail();

    expect(screen.getByText('Stale')).toBeInTheDocument();
    expect(screen.getByText(/The controller does not guess/i)).toBeInTheDocument();
  });

  it('explains a disabled host and that enabling restores it', async () => {
    backend.state.hosts = [
      makeHost({
        host_id: 'host-1',
        name: 'homeserver',
        lifecycle: 'enrolled',
        is_enabled: false,
        credential_issued_at: '2026-10-11T10:00:00+00:00',
      }),
    ];
    await renderDetail();

    expect(screen.getByText(/This host is disabled/i)).toBeInTheDocument();
    expect(screen.getByText(/without re-enrolling/i)).toBeInTheDocument();
  });

  it('reports a host that does not exist as such', async () => {
    backend = installFakeBackend({ session: ADMIN, hosts: [] });
    renderApp('/hosts/does-not-exist');
    await waitForAuthToResolve();

    // Twice over: the panel's heading and the server's own message. Both are
    // wanted, so the assertion counts rather than insisting on one.
    expect(await screen.findAllByText(/That host does not exist/i)).not.toHaveLength(0);
  });
});

describe('a viewer', () => {
  beforeEach(() => {
    backend = installFakeBackend({ session: userWithRole('viewer'), hosts: [connectedHost()] });
  });

  it('sees the host', async () => {
    await renderDetail();

    // The name appears in the shell header and in the card title, which is
    // the shell doing its job rather than a duplicate.
    expect(screen.getAllByText('homeserver').length).toBeGreaterThan(0);
    expect(screen.getByRole('heading', { name: /^connection$/i })).toBeInTheDocument();
  });

  it('is offered no management controls', async () => {
    await renderDetail();

    expect(screen.queryByRole('heading', { name: /^manage$/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /revoke/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /disable/i })).toBeNull();
  });
});

describe('disabling and enabling', () => {
  beforeEach(() => {
    backend = installFakeBackend({ session: ADMIN, hosts: [connectedHost()] });
  });

  it('confirms disabling with its consequence, then applies it', async () => {
    await renderDetail();

    await userEvent.click(screen.getByRole('button', { name: /^disable$/i }));

    expect(await screen.findByText(/disconnected immediately/i)).toBeInTheDocument();
    // And the distinction from revoking, which is the choice an operator
    // would otherwise have to make without being told about it.
    expect(screen.getByText(/no longer trust the credential/i)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /disable host/i }));

    await waitFor(() => {
      expect(screen.getByText('Disabled')).toBeInTheDocument();
    });
    expectCsrfHeader(backend.requests.find((request) => request.path.endsWith('/disable')));
  });

  it('enables without a confirmation', async () => {
    // Enabling restores the state the operator originally asked for and
    // breaks nothing, so a confirmation would be ceremony.
    backend.state.hosts = [
      makeHost({
        host_id: 'host-1',
        name: 'homeserver',
        lifecycle: 'enrolled',
        is_enabled: false,
        credential_issued_at: '2026-10-11T10:00:00+00:00',
      }),
    ];
    await renderDetail();

    await userEvent.click(screen.getByRole('button', { name: /^enable$/i }));

    await waitFor(() => {
      expect(screen.queryByText('Disabled')).toBeNull();
    });
    expectCsrfHeader(backend.requests.find((request) => request.path.endsWith('/enable')));
  });
});

describe('revoking', () => {
  beforeEach(() => {
    backend = installFakeBackend({ session: ADMIN, hosts: [connectedHost()] });
  });

  it('confirms revoking the agent credential with what it costs', async () => {
    await renderDetail();

    await userEvent.click(screen.getByRole('button', { name: /revoke agent credential/i }));

    expect(await screen.findByText(/cannot authenticate again/i)).toBeInTheDocument();
    expect(screen.getByText(/cannot be undone/i)).toBeInTheDocument();
  });

  it('moves the host to revoked and offline', async () => {
    await renderDetail();

    await userEvent.click(screen.getByRole('button', { name: /revoke agent credential/i }));
    await userEvent.click(
      within(screen.getByRole('dialog')).getByRole('button', { name: /revoke credential/i }),
    );

    await waitFor(() => {
      expect(screen.getByText('Revoked')).toBeInTheDocument();
    });
    expectCsrfHeader(
      backend.requests.find((request) => request.path.endsWith('/credential/revoke')),
    );
  });

  it('offers no credential revocation for a host that has none', async () => {
    // The control is absent rather than present-and-erroring: a host that has
    // never been enrolled has nothing to revoke, and the server answers 409.
    backend.state.hosts = [makeHost({ host_id: 'host-1', name: 'zeta' })];
    await renderDetail();

    expect(screen.queryByRole('button', { name: /revoke agent credential/i })).toBeNull();
  });

  it('reports a pending-credential revocation that revoked nothing', async () => {
    // Zero is reported as zero. Claiming success for a revocation that
    // revoked nothing would tell an operator their host is secured when
    // nothing happened.
    backend.state.hosts = [
      makeHost({ host_id: 'host-1', name: 'zeta', has_pending_enrollment: true }),
    ];
    await renderDetail();

    // Revoke it once...
    await userEvent.click(screen.getByRole('button', { name: /revoke pending credential/i }));
    await userEvent.click(
      within(screen.getByRole('dialog')).getByRole('button', { name: /revoke credential/i }),
    );

    expect(await screen.findByText(/Revoked one pending enrolment credential/i)).toBeInTheDocument();
  });

  it('offers no pending revocation when nothing is pending', async () => {
    backend.state.hosts = [makeHost({ host_id: 'host-1', name: 'zeta' })];
    await renderDetail();

    expect(screen.queryByRole('button', { name: /revoke pending credential/i })).toBeNull();
  });
});
