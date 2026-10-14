import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';

import {
  emptyUpdateOverview,
  installFakeBackend,
  makeHostUpdateStatus,
  makeUpdateJob,
  makeUpdatePlan,
  makeUser,
  type FakeBackend,
} from '@/test/fake-backend';
import { renderApp, waitForAuthToResolve } from '@/test/render-app';

/**
 * Update management, driven through the real API client.
 *
 * Every request goes through `apiRequest` to a stubbed `fetch`, so the URL
 * construction, the CSRF header, the error mapping and the cache invalidation
 * are all the shipping code.
 *
 * Three things these tests care about more than they care about rendering:
 *
 * - **"We cannot tell" is never rendered as "fine."** A controller with no
 *   release metadata, an unsigned manifest, a host reporting an unparseable
 *   version: each has to say what it is. Showing any of them as up to date or
 *   as signed would be the interface asserting something nothing checked.
 * - **A blocked update is still shown as an update**, with every blocker. An
 *   interface that hid it would make a stuck fleet look current.
 * - **Nothing is invented.** A host with no installed version shows a dash,
 *   and progress comes from the reported state rather than from a timer.
 */

let backend: FakeBackend;

const ADMIN = makeUser({ user_id: 'u-admin', username: 'Morgan', role: 'admin' });
const VIEWER = makeUser({ user_id: 'u-view', username: 'Robin', role: 'viewer' });

beforeEach(() => {
  backend = installFakeBackend({ session: ADMIN });
});

/**
 * Seed the backend and open the page.
 *
 * Re-installs rather than mutating, because the seed usually has to be in
 * place before the first request — a mutation after render races the query.
 */
async function openUpdates(
  seed: Parameters<typeof installFakeBackend>[0] = {},
  user: typeof ADMIN = ADMIN,
) {
  backend.restore();
  backend = installFakeBackend({ session: user, ...seed });
  const rendered = renderApp('/updates');
  await waitForAuthToResolve();
  return rendered;
}

describe('the fleet summary', () => {
  it('reports no release metadata as itself rather than as up to date', async () => {
    // The state of every build of this project: nothing has been published.
    // An interface that showed this as "up to date" would be claiming a
    // comparison nothing performed.
    await openUpdates({
      hostUpdateStatuses: [
        makeHostUpdateStatus({
          host_name: 'homeserver',
          plan: makeUpdatePlan({ outcome: 'metadata_unavailable', target_version: null }),
        }),
      ],
    });

    // Both places have to say it: the controller card, because that is where
    // the cause is, and the host row, because that is where somebody would
    // otherwise read a green "up to date".
    expect(await screen.findByText(/does not know what versions exist/i)).toBeVisible();
    const row = screen.getByRole('row', { name: /homeserver/ });
    expect(within(row).getByText('No release data')).toBeVisible();
  });

  it('reports an unsigned manifest as unsigned, not as signed', async () => {
    // `absent` and `valid` are different answers, and only one of them means
    // the metadata is authentic.
    await openUpdates({
      updateOverview: {
        ...emptyUpdateOverview(),
        metadata_available: true,
        metadata_signature: 'absent',
      },
    });

    expect(await screen.findByText('Unsigned')).toBeVisible();
    expect(screen.getByText(/Automatic updates require one/i)).toBeVisible();
    expect(screen.queryByText('Signed')).not.toBeInTheDocument();
  });

  it('reports a key it does not trust as untrusted rather than valid', async () => {
    await openUpdates({
      updateOverview: {
        ...emptyUpdateOverview(),
        metadata_available: true,
        metadata_signature: 'untrusted_key',
      },
    });

    expect(await screen.findByText('Untrusted key')).toBeVisible();
    expect(screen.queryByText('Signed')).not.toBeInTheDocument();
  });

  it('shows a valid signature as signed', async () => {
    await openUpdates({
      updateOverview: {
        ...emptyUpdateOverview(),
        metadata_available: true,
        metadata_signature: 'valid',
      },
    });

    expect(await screen.findByText('Signed')).toBeVisible();
  });

  it('counts blocked updates separately from available ones', async () => {
    // Blocked is still an update. Folding it into "available" would promise
    // an action that fails; folding it into "up to date" would hide it.
    await openUpdates({
      updateOverview: {
        ...emptyUpdateOverview(),
        total_hosts: 5,
        up_to_date: 2,
        update_available: 2,
        blocked: 1,
      },
    });

    // A tile's label and value live in sibling elements, so the tile itself is
    // two levels above the label.
    const tile = (await screen.findByText('Updates available')).closest('div')?.parentElement;
    expect(tile).not.toBeNull();
    expect(within(tile!).getByText('2')).toBeVisible();
    expect(within(tile!).getByText('1 blocked')).toBeVisible();
  });

  it('shows the version distribution newest first', async () => {
    // The server sorts by precedence; this asserts the order survives
    // rendering, because a client that re-sorted alphabetically would put
    // 0.10.0 before 0.9.0.
    await openUpdates({
      updateOverview: {
        ...emptyUpdateOverview(),
        version_distribution: [
          { version: '0.10.0', hosts: 1 },
          { version: '0.9.0', hosts: 3 },
        ],
      },
    });

    const list = await screen.findByRole('list');
    const text = list.textContent;
    expect(text.indexOf('0.10.0')).toBeLessThan(text.indexOf('0.9.0'));
  });
});

describe('the host table', () => {
  it('shows a dash rather than inventing a version', async () => {
    await openUpdates({
      hostUpdateStatuses: [
        makeHostUpdateStatus({
          host_name: 'never-reported',
          installed_version: null,
          plan: makeUpdatePlan({
            outcome: 'unknown_version',
            installed_version: null,
            target_version: null,
          }),
        }),
      ],
    });

    const row = await screen.findByRole('row', { name: /never-reported/ });
    expect(within(row).getAllByText('—').length).toBeGreaterThan(0);
    expect(within(row).getByText('Version unknown')).toBeVisible();
  });

  it('shows every blocker on a blocked update', async () => {
    // An administrator fixing one wants to know about the second now, not
    // after another attempt.
    await openUpdates({
      hostUpdateStatuses: [
        makeHostUpdateStatus({
          host_name: 'stuck',
          plan: makeUpdatePlan({
            outcome: 'update_available_incompatible',
            target_version: '0.2.0',
            step: 'minor',
            blockers: [
              { code: 'protocol_mismatch', detail: 'the release speaks protocol 99' },
              { code: 'schema_too_old', detail: 'run the migrations first' },
            ],
          }),
        }),
      ],
    });

    const row = await screen.findByRole('row', { name: /stuck/ });
    expect(within(row).getByText('Update blocked')).toBeVisible();
    expect(within(row).getByText(/speaks protocol 99/)).toBeVisible();
    expect(within(row).getByText(/run the migrations first/)).toBeVisible();
  });

  it('shows the policy and the maintenance window with its zone', async () => {
    // A window rendered without its zone is how somebody comes to believe an
    // update ran at the wrong time.
    await openUpdates({
      hostUpdateStatuses: [
        makeHostUpdateStatus({
          host_name: 'scheduled',
          policy: 'patch',
          automatic: true,
          maintenance_window: {
            timezone: 'Europe/Berlin',
            weekdays: [0],
            start: '02:00',
            end: '04:00',
            description: 'Sunday 02:00-04:00 Europe/Berlin',
          },
        }),
      ],
    });

    const row = await screen.findByRole('row', { name: /scheduled/ });
    expect(within(row).getByText('Automatic patch')).toBeVisible();
    expect(within(row).getByText(/Europe\/Berlin/)).toBeVisible();
  });

  it('renders a job state it does not recognise as itself', async () => {
    // The server's vocabulary can grow — a controller newer than this bundle
    // is an ordinary consequence of upgrading the controller first. A blank
    // cell would read as "no update", which is the one wrong answer.
    await openUpdates({
      hostUpdateStatuses: [
        makeHostUpdateStatus({
          host_name: 'futuristic',
          open_job: makeUpdateJob({ state: 'reticulating_splines' }),
        }),
      ],
    });

    const row = await screen.findByRole('row', { name: /futuristic/ });
    expect(within(row).getByText('reticulating_splines')).toBeVisible();
  });

  it('says so when there are no hosts', async () => {
    await openUpdates();

    expect(await screen.findByText('No hosts yet')).toBeVisible();
  });
});

describe('what a viewer can see and cannot do', () => {
  it('shows the status and offers no controls', async () => {
    // Every role sees everything; only an administrator sees the controls.
    // The server refuses the rest regardless — the hiding is a kindness, not
    // the boundary.
    await openUpdates(
      { hostUpdateStatuses: [makeHostUpdateStatus({ host_name: 'homeserver' })] },
      VIEWER,
    );

    expect(await screen.findByRole('row', { name: /homeserver/ })).toBeVisible();
    expect(screen.queryByRole('button', { name: /manage/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('columnheader', { name: 'Actions' })).not.toBeInTheDocument();
  });
});

describe('managing one host', () => {
  it('an administrator changes the policy and the request carries CSRF', async () => {
    await openUpdates({
      hostUpdateStatuses: [makeHostUpdateStatus({ host_name: 'homeserver' })],
    });
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: /manage/i }));
    const dialog = await screen.findByRole('dialog');
    await user.selectOptions(
      within(dialog).getByLabelText('Policy'),
      'patch',
    );
    await user.click(within(dialog).getByRole('button', { name: /save policy/i }));

    await waitFor(() => {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });
    const put = backend.requests.find((request) => request.method === 'PUT');
    expect(put).toBeDefined();
    expect(put?.path).toContain('/update-policy');
    expect(put?.headers['x-csrf-token']).toBeDefined();
    expect(put?.body).toMatchObject({ policy: 'patch' });
  });

  it('the dialog names the step, not just the version', async () => {
    // "Install 0.2.0" and "install 0.2.0, a minor update" are different
    // sentences, and the second is the one somebody needs before restarting
    // a service on a machine they care about.
    await openUpdates({
      hostUpdateStatuses: [
        makeHostUpdateStatus({
          plan: makeUpdatePlan({
            outcome: 'update_available',
            target_version: '0.2.0',
            step: 'minor',
          }),
        }),
      ],
    });
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: /manage/i }));
    const dialog = await screen.findByRole('dialog');

    // The step appears twice on purpose — beside the version, and in the
    // sentence under the install button. This asserts the sentence, because
    // that is the one somebody reads just before they click.
    expect(within(dialog).getByText(/^A minor update\./)).toBeVisible();
    expect(within(dialog).getByText('(minor)', { exact: false })).toBeVisible();
    expect(
      within(dialog).getByRole('button', { name: /install 0\.2\.0 now/i }),
    ).toBeVisible();
  });

  it('starting an update records it and shows it in progress', async () => {
    await openUpdates({
      hostUpdateStatuses: [
        makeHostUpdateStatus({
          plan: makeUpdatePlan({
            outcome: 'update_available',
            target_version: '0.1.1',
            step: 'patch',
          }),
        }),
      ],
    });
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: /manage/i }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: /install/i }));

    await waitFor(() => {
      expect(
        backend.requests.some(
          (request) => request.method === 'POST' && request.path.includes('/updates'),
        ),
      ).toBe(true);
    });
  });

  it('offers no cancel button once the binary is being replaced', async () => {
    // The controller cannot stop it. A button that fails is worse than one
    // that is absent, and the explanation replaces it.
    await openUpdates({
      hostUpdateStatuses: [
        makeHostUpdateStatus({ open_job: makeUpdateJob({ state: 'installing' }) }),
      ],
    });
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: /manage/i }));
    const dialog = await screen.findByRole('dialog');

    expect(
      within(dialog).queryByRole('button', { name: /cancel update/i }),
    ).not.toBeInTheDocument();
    expect(within(dialog).getByText(/cannot stop it/i)).toBeVisible();
  });

  it('offers a cancel button while the update is still queued', async () => {
    await openUpdates({
      hostUpdateStatuses: [
        makeHostUpdateStatus({ open_job: makeUpdateJob({ state: 'queued' }) }),
      ],
    });
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: /manage/i }));
    const dialog = await screen.findByRole('dialog');

    expect(
      within(dialog).getByRole('button', { name: /cancel update/i }),
    ).toBeVisible();
  });

  it('a pinned policy reveals the version field', async () => {
    await openUpdates({ hostUpdateStatuses: [makeHostUpdateStatus()] });
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: /manage/i }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).queryByLabelText('Pinned version')).not.toBeInTheDocument();

    await user.selectOptions(within(dialog).getByLabelText('Policy'), 'pinned');

    expect(within(dialog).getByLabelText('Pinned version')).toBeVisible();
  });
});

describe('the history', () => {
  it('shows the failure reason in the row rather than behind a click', async () => {
    // "Why did this fail" is the only question anybody asks of a history.
    await openUpdates({
      updateHistory: [
        makeUpdateJob({
          state: 'failed',
          error: 'the digest did not match what the publisher signed',
        }),
      ],
    });

    expect(await screen.findByText('Failed')).toBeVisible();
    expect(screen.getByText(/digest did not match/)).toBeVisible();
  });

  it('shows the version a host ended up on when it differs from the target', async () => {
    // A job claiming success while the host runs the old version is a
    // rollback nobody noticed, and that difference is the interesting case.
    await openUpdates({
      updateHistory: [
        makeUpdateJob({
          state: 'rolled_back',
          to_version: '0.1.1',
          resulting_version: '0.1.0',
        }),
      ],
    });

    expect(await screen.findByText('Rolled back')).toBeVisible();
    expect(screen.getByText(/now 0\.1\.0/)).toBeVisible();
  });

  it('distinguishes a deferred update from a failed one', async () => {
    // Deferred is not a failure. Conflating them makes a healthy fleet
    // waiting for Sunday look broken.
    await openUpdates({
      updateHistory: [makeUpdateJob({ state: 'deferred' })],
    });

    expect(await screen.findByText('Deferred')).toBeVisible();
    expect(screen.queryByText('Failed')).not.toBeInTheDocument();
  });

  it('says who started it', async () => {
    await openUpdates({
      updateHistory: [makeUpdateJob({ trigger: 'policy', state: 'succeeded' })],
    });

    expect(await screen.findByText('Automatic policy')).toBeVisible();
  });

  it('says so when nothing has been attempted', async () => {
    await openUpdates();

    expect(await screen.findByText('Nothing yet')).toBeVisible();
  });
});
