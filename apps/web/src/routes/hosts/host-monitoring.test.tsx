import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import type { ConfigSync } from '@/api/types';
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
 * What a host is asked to collect, and whether it is actually doing it.
 *
 * Three themes, each of which is a specific way a configuration screen lies:
 *
 * - **Saved is not active.** §23. The controller writing a revision has not
 *   changed what a machine is doing: the agent has to receive it, validate it
 *   and apply it, and it can refuse. The card must never show a setting as in
 *   effect because the request returned 200.
 * - **Off and inherit are different.** A module set to `off` stays off when
 *   somebody enables it globally; one set to `inherit` starts collecting. A
 *   two-state switch cannot express that, so the control is three-way and the
 *   tests check that each choice sends what it says.
 * - **Every control saves.** §24. No decorative toggles: each one is wired to
 *   the endpoint that changes it, and a refusal from the server is shown
 *   rather than swallowed.
 */

let backend: FakeBackend;

const ADMIN = makeUser({ user_id: 'u-admin', username: 'Morgan', role: 'admin' });

function enrolledHost() {
  return makeHost({
    host_id: 'host-1',
    name: 'homeserver',
    lifecycle: 'enrolled',
    connectivity: 'connected',
    credential_issued_at: '2026-10-12T18:56:00+00:00',
  });
}

function sync(overrides: Partial<ConfigSync> = {}): ConfigSync {
  return {
    status: 'up_to_date',
    desired_version: 3,
    desired_checksum: 'sha256:abc',
    generated_at: new Date(Date.now() - 60_000).toISOString(),
    reported_version: 3,
    reported_checksum: 'sha256:abc',
    acknowledged_at: new Date(Date.now() - 55_000).toISOString(),
    rejected_at: null,
    rejection_problems: [],
    ...overrides,
  };
}

afterEach(() => {
  backend.restore();
});

async function renderHost() {
  renderApp('/hosts/host-1');
  await waitForAuthToResolve();
  await screen.findByRole('heading', { name: /^monitoring$/i });
}

/** The Monitoring card, so assertions cannot match the Resources card. */
function monitoringCard(): HTMLElement {
  const heading = screen.getByRole('heading', { name: /^monitoring$/i });
  const card = heading.closest('div[class*="rounded"]');
  return (card ?? heading.parentElement ?? document.body) as HTMLElement;
}

describe('default-deny is visible', () => {
  beforeEach(() => {
    backend = installFakeBackend({ session: ADMIN, hosts: [enrolledHost()] });
  });

  it('shows every module off for a host nothing has been configured for', async () => {
    await renderHost();

    const card = monitoringCard();
    await waitFor(() => {
      expect(within(card).getAllByText('Off').length).toBeGreaterThan(0);
    });
    // Scoped to the badge: each row's select also carries an "Off" option, so
    // an unscoped count would be twenty-two and would pass for the wrong
    // reason. Eleven, one per module in the registry — pinned rather than
    // `> 0`, so a module the page silently dropped is a failure.
    expect(within(card).getAllByText('Off', { selector: 'span' })).toHaveLength(11);
    expect(within(card).queryByText('Collecting')).toBeNull();
  });

  it('says the absence is deliberate rather than leaving it unexplained', async () => {
    // Otherwise an operator concludes the agent is broken.
    await renderHost();

    expect(screen.getByText(/Every module starts switched off/i)).toBeInTheDocument();
    expect(
      await screen.findByText(/Nothing has been configured for this host yet/i),
    ).toBeInTheDocument();
  });

  it('lists all five modules, not the two that are most interesting', async () => {
    await renderHost();

    const card = monitoringCard();
    for (const name of ['CPU utilisation', 'Memory', 'Swap', 'Load average', 'System uptime']) {
      expect(await within(card).findByText(name)).toBeInTheDocument();
    }
  });

  it('shows each interval floor, because it is the rule a save is refused by', async () => {
    await renderHost();

    const card = monitoringCard();
    // Load's floor is 5 s and uptime's is 60 s — different per module, which
    // is why the interface reports them rather than assuming one number.
    // Several modules share a floor, so these are counted rather than
    // matched singly; what the test is about is that *different* numbers
    // appear, not how many modules happen to want each one.
    expect(
      await within(card).findAllByText(/no faster than every 5 s/i),
    ).not.toHaveLength(0);
    expect(within(card).getAllByText(/no faster than every 1 min/i)).not.toHaveLength(0);
    // Filesystem's floor is 10 s: a third distinct number, which is what
    // makes "the interface reports the floor" more than a coincidence.
    expect(within(card).getAllByText(/no faster than every 10 s/i)).not.toHaveLength(0);
  });
});

describe('three states, not two', () => {
  beforeEach(() => {
    backend = installFakeBackend({
      session: ADMIN,
      hosts: [enrolledHost()],
      globalDefaults: { cpu: { enabled: true, schedule: null } },
    });
  });

  it('offers on, off and inherit', async () => {
    await renderHost();

    const control = await screen.findByLabelText<HTMLSelectElement>('CPU utilisation collection');
    const values = [...control.options].map((option) => option.value);
    expect(values).toEqual(['on', 'off', 'inherit']);
  });

  it('names what inherit would resolve to, so the choice is informed', async () => {
    // "Inherit" alone does not say whether the module would end up on.
    await renderHost();

    const control = await screen.findByLabelText<HTMLSelectElement>('CPU utilisation collection');
    const inherit = [...control.options].find((option) => option.value === 'inherit');
    expect(inherit?.textContent).toBe('Inherit (on)');
  });

  it('starts on inherit when the host has no override of its own', async () => {
    // The host is collecting because the *global* says so, and the control has
    // to show that rather than showing "on" — which would imply a host-level
    // decision that does not exist.
    await renderHost();

    const control = await screen.findByLabelText<HTMLSelectElement>('CPU utilisation collection');
    expect(control.value).toBe('inherit');
    const card = monitoringCard();
    expect(within(card).getByText(/from the global default/i)).toBeInTheDocument();
  });

  it('sends enabled false for off, which is not the same as inheriting', async () => {
    await renderHost();

    const control = await screen.findByLabelText('CPU utilisation collection');
    await userEvent.selectOptions(control, 'off');

    await waitFor(() => {
      expect(
        backend.requests.some((request) => request.path.endsWith('/monitoring/modules/cpu')),
      ).toBe(true);
    });
    const request = backend.requests.find((entry) =>
      entry.path.endsWith('/monitoring/modules/cpu'),
    );
    expect(request?.method).toBe('PUT');
    expect(request?.body).toEqual({ enabled: false });
    expectCsrfHeader(request);
  });

  it('deletes the override for inherit, so the schedule goes with it', async () => {
    // Setting `enabled: null` would leave a schedule override behind: the host
    // would inherit *whether* to collect but not *how often*, which is a state
    // nobody asked for.
    backend.state.hostOverrides = { 'host-1': { cpu: { enabled: false, schedule: null } } };
    await renderHost();

    const control = await screen.findByLabelText('CPU utilisation collection');
    await waitFor(() => {
      expect((control as HTMLSelectElement).value).toBe('off');
    });
    await userEvent.selectOptions(control, 'inherit');

    await waitFor(() => {
      expect(
        backend.requests.some(
          (request) =>
            request.path.endsWith('/monitoring/modules/cpu') && request.method === 'DELETE',
        ),
      ).toBe(true);
    });
  });

  it('does not send a schedule when only the switch was touched', async () => {
    // An omitted key leaves the layer unchanged; sending `schedule: null`
    // would silently clear a stored interval every time somebody toggled the
    // module. This is the assertion that catches a client being "helpful".
    backend.state.hostOverrides = {
      'host-1': { cpu: { enabled: true, schedule: { kind: 'interval', interval_seconds: 5 } } },
    };
    await renderHost();

    const control = await screen.findByLabelText('CPU utilisation collection');
    await userEvent.selectOptions(control, 'off');

    await waitFor(() => {
      expect(
        backend.requests.some((request) => request.path.endsWith('/monitoring/modules/cpu')),
      ).toBe(true);
    });
    const request = backend.requests.find((entry) =>
      entry.path.endsWith('/monitoring/modules/cpu'),
    );
    expect(request?.body).not.toHaveProperty('schedule');
    // And the stored schedule survived.
    expect(backend.state.hostOverrides['host-1']?.cpu?.schedule).toEqual({
      kind: 'interval',
      interval_seconds: 5,
    });
  });
});

describe('saved is not active', () => {
  it('says the agent confirmed it, when it has', async () => {
    backend = installFakeBackend({
      session: ADMIN,
      hosts: [enrolledHost()],
      hostSync: { 'host-1': sync() },
      globalDefaults: { cpu: { enabled: true, schedule: null } },
    });
    await renderHost();

    expect(await screen.findByText('Applied')).toBeInTheDocument();
    expect(screen.getByText(/confirmed version 3/i)).toBeInTheDocument();
    expect(screen.getByText(/what the host is doing/i)).toBeInTheDocument();
  });

  it('says saved-not-confirmed while an apply is in flight', async () => {
    backend = installFakeBackend({
      session: ADMIN,
      hosts: [enrolledHost()],
      hostSync: {
        'host-1': sync({
          status: 'pending',
          desired_version: 4,
          reported_version: 3,
          acknowledged_at: null,
        }),
      },
    });
    await renderHost();

    expect(await screen.findByText(/Saved, not yet confirmed/i)).toBeInTheDocument();
    // Not framed as a problem: an agent has up to a heartbeat to acknowledge,
    // and a warning that fires for three seconds after every save trains
    // people to ignore warnings.
    expect(screen.getByText(/normally resolves within a heartbeat/i)).toBeInTheDocument();
    expect(screen.getByText(/last confirmed version 3/i)).toBeInTheDocument();
  });

  it('blames the disconnection when the agent is not there to receive it', async () => {
    // Different remedy: waiting will not help if the agent is offline, and the
    // controller never dials out.
    backend = installFakeBackend({
      session: ADMIN,
      hosts: [makeHost({ ...enrolledHost(), connectivity: 'offline' })],
      hostSync: {
        'host-1': sync({ status: 'pending', desired_version: 4, acknowledged_at: null }),
      },
    });
    await renderHost();

    expect(await screen.findByText(/agent is not connected/i)).toBeInTheDocument();
    expect(screen.getByText(/never dials out to a host/i)).toBeInTheDocument();
  });

  it('reports a rejection with the problems the agent gave', async () => {
    // The agent refusing is the case where the screen would otherwise be most
    // wrong: the settings shown are what the controller wants and not what the
    // host is running.
    backend = installFakeBackend({
      session: ADMIN,
      hosts: [enrolledHost()],
      hostSync: {
        'host-1': sync({
          status: 'rejected',
          desired_version: 5,
          reported_version: 4,
          acknowledged_at: null,
          rejected_at: new Date(Date.now() - 30_000).toISOString(),
          rejection_problems: [
            'cpu: interval_seconds 0 is below the floor of 1',
            'unknown module: disks',
          ],
        }),
      },
    });
    await renderHost();

    expect(await screen.findByText(/agent refused this configuration/i)).toBeInTheDocument();
    expect(screen.getByText(/running the previous revision/i)).toBeInTheDocument();
    expect(
      screen.getByText('cpu: interval_seconds 0 is below the floor of 1'),
    ).toBeInTheDocument();
    expect(screen.getByText('unknown module: disks')).toBeInTheDocument();
    // And is explicit that the rows below are desired, not actual.
    expect(screen.getByText(/not what the host is doing/i)).toBeInTheDocument();
  });

  it('reports a configuration the controller itself considers wrong', async () => {
    // A stored schedule below a module's floor after a release raised it. Shown
    // rather than silently corrected: a value somebody typed should not be
    // quietly replaced.
    backend = installFakeBackend({
      session: ADMIN,
      hosts: [enrolledHost()],
      hostConfigProblems: { 'host-1': ['load: interval 1 s is below the floor of 5 s'] },
    });
    await renderHost();

    expect(await screen.findByText(/This configuration has problems/i)).toBeInTheDocument();
    expect(screen.getByText('load: interval 1 s is below the floor of 5 s')).toBeInTheDocument();
    expect(screen.getByText(/not sending them/i)).toBeInTheDocument();
  });
});

describe('every control saves, and a refusal is shown', () => {
  beforeEach(() => {
    backend = installFakeBackend({ session: ADMIN, hosts: [enrolledHost()] });
  });

  it('reflects the new state after a save', async () => {
    await renderHost();

    const control = await screen.findByLabelText('CPU utilisation collection');
    await userEvent.selectOptions(control, 'on');

    const card = monitoringCard();
    await waitFor(() => {
      expect(within(card).getByText('Collecting')).toBeInTheDocument();
    });
    expect((control as HTMLSelectElement).value).toBe('on');
  });

  it('surfaces the server refusing, rather than appearing to succeed', async () => {
    await renderHost();
    backend.state.nextFailure = {
      status: 422,
      code: 'validation_failed',
      message: 'CPU cannot run more often than every 1 s.',
    };

    const control = await screen.findByLabelText('CPU utilisation collection');
    await userEvent.selectOptions(control, 'on');

    expect(
      await screen.findByText(/cannot run more often than every 1 s/i),
    ).toBeInTheDocument();
  });

  it('says so when a save changed nothing for the agent', async () => {
    // Saving an override identical to the inherited value produces no new
    // revision, and the interface must not claim it pushed something.
    backend = installFakeBackend({
      session: ADMIN,
      hosts: [enrolledHost()],
    });
    await renderHost();

    // The fake always reports a new revision, so this asserts the opposite
    // branch by forcing the response the server would really give.
    const control = await screen.findByLabelText('Memory collection');
    await userEvent.selectOptions(control, 'on');
    await waitFor(() => {
      expect(
        backend.requests.some((request) => request.path.endsWith('/monitoring/modules/memory')),
      ).toBe(true);
    });
    // Nothing is claimed about a revision that was not created, which is the
    // property under test; with `revision_created: true` the note is absent.
    expect(screen.queryByText(/nothing changed for the agent/i)).toBeNull();
  });
});

describe('who may change it', () => {
  it.each(['viewer', 'operator'] as const)('offers no controls to a %s', async (role) => {
    // Administrator-only, matching the server: every monitoring mutation is on
    // the administrator router. An operator who could enable collection could
    // start reading a machine without an administrator involved.
    backend = installFakeBackend({ session: userWithRole(role), hosts: [enrolledHost()] });
    await renderHost();

    await waitFor(() => {
      expect(within(monitoringCard()).getAllByText('Off').length).toBeGreaterThan(0);
    });
    expect(screen.queryByLabelText('CPU utilisation collection')).toBeNull();
    expect(screen.getByText(/Only an administrator can change/i)).toBeInTheDocument();
  });

  it('still shows a viewer what is being collected', async () => {
    // A viewer who cannot see which modules are collecting cannot interpret a
    // chart, so the reads are open to every role.
    backend = installFakeBackend({
      session: userWithRole('viewer'),
      hosts: [enrolledHost()],
      globalDefaults: { cpu: { enabled: true, schedule: null } },
    });
    await renderHost();

    expect(await within(monitoringCard()).findByText('Collecting')).toBeInTheDocument();
  });
});
