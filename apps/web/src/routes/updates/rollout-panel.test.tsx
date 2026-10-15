import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import type { Rollout } from '@/api/types';
import {
  installFakeBackend,
  makeRollout,
  makeRolloutMember,
  makeUser,
  type FakeBackend,
} from '@/test/fake-backend';
import { renderApp, waitForAuthToResolve } from '@/test/render-app';

/**
 * Staged rollouts, driven through the real API client.
 *
 * Two distinctions carry this whole panel, and both are ones an interface can
 * quietly collapse:
 *
 * - **Succeeded is not healthy.** A member can have installed and not yet
 *   passed its post-install health check. Rendering those as one state makes
 *   the wait look like a hang — and the first thing somebody does about a
 *   hang is press resume, which is exactly wrong.
 * - **Skipped is not failed.** A powered-off host, or one already on the
 *   target version, says nothing about the release. Counting or colouring it
 *   as a failure would train somebody to ignore the colour that matters.
 */

let backend: FakeBackend;

const ADMIN = makeUser({ user_id: 'u-admin', username: 'Morgan', role: 'admin' });
const VIEWER = makeUser({ user_id: 'u-view', username: 'Robin', role: 'viewer' });

beforeEach(() => {
  backend = installFakeBackend({ session: ADMIN });
});

afterEach(() => {
  backend.restore();
});

/**
 * Seed the backend and open the page.
 *
 * Re-installs rather than mutating, for the reason `updates.test.tsx` gives:
 * the seed has to be in place before the first request, and a mutation after
 * render races the query.
 */
async function openUpdates(rollouts: Rollout[] = [], user: typeof ADMIN = ADMIN) {
  backend.restore();
  backend = installFakeBackend({ session: user, rollouts });
  renderApp('/updates');
  await waitForAuthToResolve();
  await screen.findByRole('heading', { name: 'Rollouts' });
}

describe('an empty fleet', () => {
  it('says there are no rollouts rather than showing an empty table', async () => {
    // The state of every installation. A fake whose default was "a rollout
    // is running" would make this the case nobody tested.
    await openUpdates();

    expect(await screen.findByText('No rollouts')).toBeInTheDocument();
  });
});

describe('a canary that has installed but not settled', () => {
  const settling = [
    makeRollout({
      state: 'canary',
      total: 3,
      succeeded: 1,
      pending: 2,
      members: [
        makeRolloutMember({ host_id: 'h-canary', batch: 0, state: 'succeeded', healthy: false }),
        makeRolloutMember({ host_id: 'h-two', batch: 1 }),
        makeRolloutMember({ host_id: 'h-three', batch: 1 }),
      ],
    }),
  ];

  it('says it is checking rather than calling it healthy', async () => {
    // The job concluded — the agent reconnected reporting the new version —
    // and the rollout is waiting to see whether the host is still there. An
    // operator has to be able to tell that apart from a stall.
    await openUpdates(settling);
    await userEvent.click(await screen.findByRole('button', { name: /Show 3 hosts/ }));

    const canary = (await screen.findByText('h-canary')).closest('li');
    expect(within(canary as HTMLElement).getByText('installed, checking')).toBeInTheDocument();
    expect(within(canary as HTMLElement).queryByText('healthy')).toBeNull();
  });

  it('counts healthy separately from installed', async () => {
    await openUpdates(settling);

    // "healthy" and "installed" are two labels in the summary, which is
    // what makes the gap between them visible at a glance.
    expect(await screen.findByText('healthy')).toBeInTheDocument();
    expect(screen.getByText('installed')).toBeInTheDocument();
  });
});

describe('a rollout stopped by the failure gate', () => {
  const stopped = [
    makeRollout({
      state: 'paused',
      failure_tolerance: 0,
      total: 3,
      failed: 1,
      pending: 2,
      paused_reason: '1 host(s) failed and this rollout tolerates 0.',
      members: [
        makeRolloutMember({
          host_id: 'h-canary',
          batch: 0,
          state: 'failed',
          detail: 'the service would not restart',
        }),
      ],
    }),
  ];

  it('shows why it stopped', async () => {
    await openUpdates(stopped);

    expect(await screen.findByText(/tolerates 0/)).toBeInTheDocument();
  });

  it('offers the override explicitly, not just a bare resume', async () => {
    // A bare resume re-reaches the same conclusion on the next pass. An
    // administrator would press it, watch nothing happen, and reasonably
    // conclude the feature is broken.
    await openUpdates(stopped);

    expect(
      await screen.findByRole('button', { name: /Resume despite 1 failure/ }),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Resume$/ })).toBeInTheDocument();
  });

  it('records the override once it is used', async () => {
    await openUpdates(stopped);

    await userEvent.click(
      await screen.findByRole('button', { name: /Resume despite 1 failure/ }),
    );

    await waitFor(() => {
      expect(backend.state.rollouts[0]?.failures_overridden).toBe(true);
    });
  });
});

describe('an administrator pause', () => {
  it('does not offer the failure override', async () => {
    // Nothing failed, so there is nothing to override — and offering the
    // flag would invite somebody to set a value that means nothing.
    await openUpdates([
      makeRollout({
        state: 'paused',
        total: 2,
        pending: 2,
        paused_reason: 'paused by an administrator',
      }),
    ]);

    expect(await screen.findByRole('button', { name: /^Resume$/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Resume despite/ })).toBeNull();
  });
});

describe('skipped hosts', () => {
  it('are shown apart from failures rather than as one', async () => {
    await openUpdates([
      makeRollout({
        state: 'completed',
        total: 2,
        succeeded: 1,
        skipped: 1,
        failed: 0,
        current_batch: null,
        members: [
          makeRolloutMember({ host_id: 'h-up', batch: 0, state: 'succeeded', healthy: true }),
          makeRolloutMember({
            host_id: 'h-off',
            batch: 1,
            state: 'skipped',
            detail: 'this host did not come online within 3600s of its batch starting',
          }),
        ],
      }),
    ]);
    await userEvent.click(await screen.findByRole('button', { name: /Show 2 hosts/ }));

    const skipped = (await screen.findByText('h-off')).closest('li');
    expect(within(skipped as HTMLElement).getByText('skipped')).toBeInTheDocument();
    expect(within(skipped as HTMLElement).queryByText('failed')).toBeNull();
    expect(screen.getByText(/did not come online/)).toBeInTheDocument();
  });
});

describe('a finished rollout', () => {
  it('keeps saying that its failures were overridden', async () => {
    // "This finished because somebody waved it through" is a fact the
    // summary has to keep, not one that disappears when the rollout ends.
    await openUpdates([
      makeRollout({
        state: 'completed',
        failures_overridden: true,
        total: 2,
        succeeded: 1,
        failed: 1,
        current_batch: null,
      }),
    ]);

    expect(await screen.findByText('failures overridden')).toBeInTheDocument();
  });

  it('offers no controls', async () => {
    await openUpdates([
      makeRollout({ state: 'completed', total: 1, succeeded: 1, current_batch: null }),
    ]);

    await screen.findByText('completed');
    expect(screen.queryByRole('button', { name: /Pause|Resume|Cancel/ })).toBeNull();
  });
});

describe('authorization', () => {
  it('shows a viewer the rollout and none of the controls', async () => {
    // The server refuses a viewer's mutation; hiding the control is kinder
    // than a button that errors, and is not the protection.
    await openUpdates(
      [makeRollout({ state: 'running', total: 2, pending: 2 })],
      VIEWER,
    );

    expect(await screen.findByText('running')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Pause|Cancel/ })).toBeNull();
  });
});
