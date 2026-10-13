import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  expectCsrfHeader,
  installFakeBackend,
  makeUser,
  userWithRole,
  type FakeBackend,
} from '@/test/fake-backend';
import { renderApp, waitForAuthToResolve } from '@/test/render-app';

/**
 * Installation-wide monitoring defaults.
 *
 * The page with the widest reach in the product: one switch here changes what
 * every host that has not overridden it collects. So the tests are about three
 * things rather than about rendering a list —
 *
 * - **that the reach is stated**, because a control that silently starts
 *   reading forty machines should not look like a preferences toggle;
 * - **that "not configured" survives as its own state**, distinct from "off",
 *   since only one of them is a decision somebody made;
 * - **that an invalid interval is refused rather than clamped** (§7), and the
 *   refusal comes from the server rather than from a second copy of the rule
 *   living in the client.
 */

let backend: FakeBackend;

const ADMIN = makeUser({ user_id: 'u-admin', username: 'Morgan', role: 'admin' });

afterEach(() => {
  backend.restore();
});

async function renderSettings() {
  renderApp('/settings/monitoring');
  await waitForAuthToResolve();
  await screen.findByRole('heading', { name: /modules/i });
}

/** One module's row, so an assertion cannot match a neighbouring module. */
function row(moduleName: string): HTMLElement {
  const label = screen.getByText(moduleName);
  return label.closest('li') ?? label.parentElement ?? document.body;
}

describe('the resting state of a fresh installation', () => {
  beforeEach(() => {
    backend = installFakeBackend({ session: ADMIN });
  });

  it('shows every module as not configured, which is not the same as off', async () => {
    // "Nothing has been decided" and "somebody decided no" resolve to the same
    // behaviour and are different facts — and only the second is auditable.
    await renderSettings();

    // Scoped to the badge: each row's select also carries a "Not configured"
    // option, so an unscoped count would be ten and pass for the wrong reason.
    expect(
      await screen.findAllByText('Not configured', { selector: 'span' }),
    ).toHaveLength(5);
    expect(screen.queryByText('On by default')).toBeNull();
  });

  it('says what "not configured" means for collection', async () => {
    await renderSettings();

    const cpu = row('CPU utilisation');
    expect(
      within(cpu).getByText(/nothing has been decided, so hosts collect nothing/i),
    ).toBeInTheDocument();
  });

  it('states that these settings reach every host', async () => {
    // The reason this page is not a preferences panel.
    await renderSettings();

    expect(screen.getByText(/These settings reach every host/i)).toBeInTheDocument();
    expect(
      screen.getByText(/machines you have not visited/i),
    ).toBeInTheDocument();
  });

  it('says hosts with their own setting are unaffected', async () => {
    // Otherwise an administrator cannot predict what this switch will do.
    await renderSettings();

    expect(screen.getByText(/Hosts with their own setting are/i)).toBeInTheDocument();
  });

  it('explains that nothing collects until somebody asks', async () => {
    await renderSettings();

    expect(
      screen.getByText(/should not start reading a machine because it was registered/i),
    ).toBeInTheDocument();
  });
});

describe('each module reports the rules a save is judged by', () => {
  beforeEach(() => {
    backend = installFakeBackend({ session: ADMIN });
  });

  it('shows the per-module interval floor, not one global number', async () => {
    await renderSettings();

    // CPU's floor is 1 s, load's is 5 s, uptime's is 60 s. An interface that
    // showed one number would offer an interval the server refuses.
    expect(within(row('CPU utilisation')).getByText(/no faster than every 1 s/i)).toBeInTheDocument();
    expect(within(row('Load average')).getByText(/no faster than every 5 s/i)).toBeInTheDocument();
    expect(within(row('System uptime')).getByText(/no faster than every 1 min/i)).toBeInTheDocument();
  });

  it('shows the default that applies when nothing is stored', async () => {
    await renderSettings();

    const cpu = row('CPU utilisation');
    expect(
      within(cpu).getByText(/Nothing stored, so the module's own default of 10 s applies/i),
    ).toBeInTheDocument();
  });

  it('counts the metrics each module produces', async () => {
    await renderSettings();

    // Four and three, from contracts/monitoring-modules.json. Load has one,
    // which is the case that would read as "1 metrics" if the plural were not
    // handled.
    expect(within(row('Memory')).getByText(/4 metrics/i)).toBeInTheDocument();
    expect(within(row('CPU utilisation')).getByText(/3 metrics/i)).toBeInTheDocument();
    expect(within(row('Load average')).getByText(/1 metric ·/i)).toBeInTheDocument();
  });
});

describe('turning a module on', () => {
  beforeEach(() => {
    backend = installFakeBackend({ session: ADMIN });
  });

  it('sends only the enabled field, leaving any stored interval alone', async () => {
    // An omitted `schedule` leaves the layer unchanged. Sending `null` would
    // clear a stored interval every time somebody flipped the switch.
    await renderSettings();

    await userEvent.selectOptions(screen.getByLabelText('CPU utilisation default'), 'on');

    await waitFor(() => {
      expect(
        backend.requests.some((request) => request.path.endsWith('/monitoring/defaults/cpu')),
      ).toBe(true);
    });
    const request = backend.requests.find((entry) =>
      entry.path.endsWith('/monitoring/defaults/cpu'),
    );
    expect(request?.method).toBe('PUT');
    expect(request?.body).toEqual({ enabled: true });
    expect(request?.body).not.toHaveProperty('schedule');
    expectCsrfHeader(request);
  });

  it('reflects the new state', async () => {
    await renderSettings();

    await userEvent.selectOptions(screen.getByLabelText('CPU utilisation default'), 'on');

    expect(await within(row('CPU utilisation')).findByText('On by default')).toBeInTheDocument();
  });

  it('can return a module to not-configured, which is a third choice', async () => {
    backend.state.globalDefaults = { cpu: { enabled: true, schedule: null } };
    await renderSettings();

    const control = screen.getByLabelText('CPU utilisation default');
    await waitFor(() => {
      expect((control as HTMLSelectElement).value).toBe('on');
    });
    await userEvent.selectOptions(control, 'unset');

    await waitFor(() => {
      const request = backend.requests
        .filter((entry) => entry.path.endsWith('/monitoring/defaults/cpu'))
        .at(-1);
      expect(request?.body).toEqual({ enabled: null });
    });
  });
});

describe('the interval', () => {
  beforeEach(() => {
    backend = installFakeBackend({ session: ADMIN });
  });

  it('sends a schedule document, not a bare number', async () => {
    await renderSettings();

    const field = within(row('CPU utilisation')).getByLabelText('Interval (seconds)');
    await userEvent.type(field, '30');
    await userEvent.click(within(row('CPU utilisation')).getByRole('button', { name: /save interval/i }));

    await waitFor(() => {
      expect(
        backend.requests.some((request) => request.path.endsWith('/monitoring/defaults/cpu')),
      ).toBe(true);
    });
    const request = backend.requests.find((entry) =>
      entry.path.endsWith('/monitoring/defaults/cpu'),
    );
    expect(request?.body).toEqual({ schedule: { kind: 'interval', interval_seconds: 30 } });
  });

  it('is refused by the server below the floor, not clamped by the client', async () => {
    // §7. A client that clamped would save a value the operator did not ask
    // for; one that refused locally would be a second copy of the rule.
    await renderSettings();

    const load = row('Load average');
    const field = within(load).getByLabelText('Interval (seconds)');
    await userEvent.type(field, '1');
    await userEvent.click(within(load).getByRole('button', { name: /save interval/i }));

    expect(
      await screen.findByText(/cannot run more often than every 5 s/i),
    ).toBeInTheDocument();
    // And the value was not quietly stored.
    expect(backend.state.globalDefaults.load?.schedule ?? null).toBeNull();
  });

  it('sends the value the client was given, so the floor is really the server\'s', async () => {
    // The request goes out with 1, not with 5. If the client clamped, this
    // test would pass the previous one for the wrong reason.
    await renderSettings();

    const load = row('Load average');
    await userEvent.type(load.querySelector('input')!, '1');
    await userEvent.click(within(load).getByRole('button', { name: /save interval/i }));

    await waitFor(() => {
      expect(
        backend.requests.some((request) => request.path.endsWith('/monitoring/defaults/load')),
      ).toBe(true);
    });
    const request = backend.requests.find((entry) =>
      entry.path.endsWith('/monitoring/defaults/load'),
    );
    expect(request?.body).toEqual({ schedule: { kind: 'interval', interval_seconds: 1 } });
  });

  it('refuses to submit something that is not a whole number', async () => {
    // Caught before the request because it is a typing mistake rather than a
    // policy question — and the message says what to do.
    await renderSettings();

    const cpu = row('CPU utilisation');
    await userEvent.type(cpu.querySelector('input')!, '2.5');
    await userEvent.click(within(cpu).getByRole('button', { name: /save interval/i }));

    expect(within(cpu).getByText(/whole number of seconds/i)).toBeInTheDocument();
    expect(
      backend.requests.some((request) => request.path.endsWith('/monitoring/defaults/cpu')),
    ).toBe(false);
  });

  it('can clear a stored interval so the module default applies again', async () => {
    backend.state.globalDefaults = {
      cpu: { enabled: true, schedule: { kind: 'interval', interval_seconds: 45 } },
    };
    await renderSettings();

    const cpu = row('CPU utilisation');
    expect(await within(cpu).findByText(/Stored: every 45 s/i)).toBeInTheDocument();
    await userEvent.click(
      within(cpu).getByRole('button', { name: /clear the CPU utilisation interval/i }),
    );

    await waitFor(() => {
      const request = backend.requests
        .filter((entry) => entry.path.endsWith('/monitoring/defaults/cpu'))
        .at(-1);
      expect(request?.body).toEqual({ schedule: null });
    });
  });

  it('leaves the save button inert until something is typed', async () => {
    // §24: a control that cannot do anything should not look like it can.
    await renderSettings();

    const save = within(row('CPU utilisation')).getByRole('button', { name: /save interval/i });
    expect(save).toBeDisabled();
  });
});

describe('who may reach this page', () => {
  it.each(['viewer', 'operator'] as const)('refuses a %s', async (role) => {
    // Administrator-only, matching the server. An operator seeing controls the
    // API answers 403 to would be a confusing UX bug.
    backend = installFakeBackend({ session: userWithRole(role) });
    renderApp('/settings/monitoring');
    await waitForAuthToResolve();

    expect(
      await screen.findByText(/Monitoring defaults requires the Administrator role/i),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText('CPU utilisation default')).toBeNull();
  });

  it('does not fetch the defaults for a role that cannot see them', async () => {
    // Not a security property — the server enforces that — but a pointless 403
    // in the console is noise an operator reports as a bug.
    backend = installFakeBackend({ session: userWithRole('viewer') });
    renderApp('/settings/monitoring');
    await waitForAuthToResolve();
    await screen.findByText(/requires the Administrator role/i);

    expect(
      backend.requests.some((request) => request.path.endsWith('/monitoring/defaults')),
    ).toBe(false);
  });
});

describe('a failed read', () => {
  it('offers a retry rather than an empty list', async () => {
    backend = installFakeBackend({
      session: ADMIN,
      routeFailures: {
        '/monitoring/defaults': {
          status: 500,
          code: 'internal_error',
          message: 'Something went wrong.',
        },
      },
    });
    renderApp('/settings/monitoring');
    await waitForAuthToResolve();

    expect(
      await screen.findByText(/Could not load the monitoring defaults/i),
    ).toBeInTheDocument();
  });
});
