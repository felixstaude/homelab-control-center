import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  installFakeBackend,
  makeOwnSession,
  userWithRole,
  type FakeBackend,
} from '@/test/fake-backend';
import { renderApp, waitForAuthToResolve } from '@/test/render-app';

/**
 * My account, and the self-service password change.
 *
 * The password change is the most security-sensitive form in the product, so
 * these cover the three things that would be worst to get wrong: that a typo
 * in the current password does not sign the user out, that the new password
 * never lingers in the DOM, and that the consequence — being signed out
 * everywhere else — is stated before the button is pressed rather than after.
 */

let backend: FakeBackend;
const PASSWORD = 'correct horse battery staple';
const NEW_PASSWORD = 'a different sufficiently long passphrase';

afterEach(() => {
  backend.restore();
});

async function renderAccount() {
  renderApp('/settings/account');
  await waitForAuthToResolve();
  await screen.findByText('Your account');
}

describe('account details', () => {
  beforeEach(() => {
    backend = installFakeBackend({
      session: userWithRole('operator'),
      ownSessions: [makeOwnSession({ is_current: true })],
    });
  });

  it('is reachable by every role, not only administrators', async () => {
    // These operations are scoped to the caller by construction, so there is
    // no role to require.
    await renderAccount();

    // Scoped to the details list: the sidebar's account menu renders the
    // username too, so an unscoped query is ambiguous.
    const details = screen.getByText('Username').closest('dl');
    expect(details).not.toBeNull();
    expect(within(details!).getByText('Casey')).toBeInTheDocument();
    expect(within(details!).getByText('Operator')).toBeInTheDocument();
  });

  it('says the display name is unset rather than showing a blank', async () => {
    await renderAccount();

    expect(screen.getByText(/Not set — you are shown as Casey/)).toBeInTheDocument();
  });

  it('shows the role with its description', async () => {
    await renderAccount();

    expect(
      screen.getByText('Read access plus permitted operational actions.'),
    ).toBeInTheDocument();
  });

  it('says where the display name and role are changed', async () => {
    // Rather than offering fields that do nothing: there is no self-service
    // endpoint for either, and a disabled input would imply there is.
    await renderAccount();

    expect(
      screen.getByText(/managed by an administrator/i),
    ).toBeInTheDocument();
  });

  it('does not offer to change its own role', async () => {
    await renderAccount();

    expect(screen.queryByLabelText('Role')).not.toBeInTheDocument();
  });

  it('does not offer account deletion', async () => {
    await renderAccount();

    expect(screen.queryByRole('button', { name: /delete/i })).not.toBeInTheDocument();
  });
});

describe('changing a password', () => {
  beforeEach(() => {
    backend = installFakeBackend({
      session: userWithRole('viewer'),
      ownSessions: [
        makeOwnSession({ session_id: 's-current', is_current: true }),
        makeOwnSession({ session_id: 's-other' }),
      ],
      currentPassword: PASSWORD,
    });
  });

  async function fillForm(
    user: ReturnType<typeof userEvent.setup>,
    current: string,
    next: string,
    confirmation = next,
  ) {
    await user.type(screen.getByLabelText('Current password'), current);
    await user.type(screen.getByLabelText('New password'), next);
    await user.type(screen.getByLabelText('Confirm new password'), confirmation);
    await user.click(screen.getByRole('button', { name: 'Change password' }));
  }

  it('states the consequence before the button is pressed', async () => {
    await renderAccount();

    expect(screen.getByText(/This signs you out everywhere else/i)).toBeInTheDocument();
  });

  it('changes the password and reports how many sessions ended', async () => {
    const user = userEvent.setup();
    await renderAccount();

    await fillForm(user, PASSWORD, NEW_PASSWORD);

    expect(await screen.findByText('Password changed')).toBeInTheDocument();
    expect(screen.getByText(/Signed out of 1 other session/)).toBeInTheDocument();
    expect(screen.getByText(/still signed in here, on a new session/)).toBeInTheDocument();
  });

  it('says so plainly when there was nowhere else signed in', async () => {
    backend.state.ownSessions = [makeOwnSession({ session_id: 's-only', is_current: true })];
    const user = userEvent.setup();
    await renderAccount();

    await fillForm(user, PASSWORD, NEW_PASSWORD);

    expect(await screen.findByText(/only place/i)).toBeInTheDocument();
  });

  it('does not sign the user out when the current password is wrong', async () => {
    /*
     * The property this form exists to protect. The server answers 422 rather
     * than 401 precisely so the client does not read a typo as an expired
     * session — if it did, mistyping your own password would eject you from
     * the application.
     */
    const user = userEvent.setup();
    await renderAccount();

    await fillForm(user, 'not my password', NEW_PASSWORD);

    expect(
      await screen.findByText('The current password is incorrect.'),
    ).toBeInTheDocument();
    // Still on the page, still signed in.
    expect(screen.getByText('Your account')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Sign in' })).not.toBeInTheDocument();
  });

  it('shows a wrong current password against its own field', async () => {
    const user = userEvent.setup();
    await renderAccount();

    await fillForm(user, 'not my password', NEW_PASSWORD);
    const message = await screen.findByText('The current password is incorrect.');

    // `role="alert"`, so the message is announced when it appears rather than
    // only when the field is next focused.
    expect(message).toHaveAttribute('role', 'alert');
    const field = screen.getByLabelText('Current password');
    expect(field).toHaveAttribute('aria-invalid', 'true');
    expect(field.getAttribute('aria-describedby')).toContain(message.id);
  });

  it('keeps the current password after a policy rejection', async () => {
    // Only the field that was rejected is cleared: making the user retype the
    // one that was fine is needless.
    const user = userEvent.setup();
    await renderAccount();

    await fillForm(user, PASSWORD, 'short');
    await screen.findByText(/not accepted/i);

    expect(screen.getByLabelText<HTMLInputElement>('Current password').value).toBe(PASSWORD);
    expect(screen.getByLabelText<HTMLInputElement>('New password').value).toBe('');
  });

  it('lists every policy problem the server reported', async () => {
    const user = userEvent.setup();
    await renderAccount();

    await fillForm(user, PASSWORD, 'short');

    expect(await screen.findByText(/at least 8 characters/)).toBeInTheDocument();
  });

  it('refuses to submit when the confirmation does not match', async () => {
    const user = userEvent.setup();
    await renderAccount();

    await fillForm(user, PASSWORD, NEW_PASSWORD, 'something else');

    expect(
      await screen.findByText('The two passwords do not match.'),
    ).toBeInTheDocument();
    expect(
      backend.requests.filter((request) => request.path.endsWith('/account/password')),
    ).toHaveLength(0);
  });

  it('does not send the confirmation to the server', async () => {
    // The server has nothing to compare it against that this form does not
    // already have, so sending it would transmit the new password twice.
    const user = userEvent.setup();
    await renderAccount();

    await fillForm(user, PASSWORD, NEW_PASSWORD);
    await screen.findByText('Password changed');

    const request = backend.requests.find((candidate) =>
      candidate.path.endsWith('/account/password'),
    );
    expect(Object.keys(request?.body as object)).toEqual([
      'current_password',
      'new_password',
    ]);
  });

  it('carries the CSRF header', async () => {
    const user = userEvent.setup();
    await renderAccount();

    await fillForm(user, PASSWORD, NEW_PASSWORD);
    await screen.findByText('Password changed');

    const request = backend.requests.find((candidate) =>
      candidate.path.endsWith('/account/password'),
    );
    expect(request?.headers['x-csrf-token']).toBeDefined();
  });

  it('leaves neither password in the document afterwards', async () => {
    const user = userEvent.setup();
    await renderAccount();

    await fillForm(user, PASSWORD, NEW_PASSWORD);
    await screen.findByText('Password changed');

    // `outerHTML` includes attribute values, so a controlled input that
    // serialised its value would be caught here.
    expect(document.body.outerHTML).not.toContain(NEW_PASSWORD);
    expect(document.body.outerHTML).not.toContain(PASSWORD);
  });

  it('sends one request even when the button is double-clicked', async () => {
    const user = userEvent.setup();
    await renderAccount();

    await user.type(screen.getByLabelText('Current password'), PASSWORD);
    await user.type(screen.getByLabelText('New password'), NEW_PASSWORD);
    await user.type(screen.getByLabelText('Confirm new password'), NEW_PASSWORD);
    await user.dblClick(screen.getByRole('button', { name: 'Change password' }));

    await screen.findByText('Password changed');
    expect(
      backend.requests.filter((request) => request.path.endsWith('/account/password')),
    ).toHaveLength(1);
  });

  it('shows a rate limit with the wait, and does not retry', async () => {
    // An auto-retried password change is an automatic credential-retry loop
    // that spends the user's own budget.
    backend.state.nextFailure = {
      status: 429,
      code: 'rate_limited',
      message: 'Too many password change attempts. Please try again later.',
      retryAfter: 42,
    };
    const user = userEvent.setup();
    await renderAccount();

    await fillForm(user, PASSWORD, NEW_PASSWORD);

    expect(await screen.findByText(/Too many attempts/i)).toBeInTheDocument();
    expect(screen.getByText(/about 42 seconds/)).toBeInTheDocument();
    expect(
      backend.requests.filter((request) => request.path.endsWith('/account/password')),
    ).toHaveLength(1);
  });

  it('labels every field and describes the policy', async () => {
    await renderAccount();

    expect(screen.getByLabelText('Current password')).toBeInTheDocument();
    expect(screen.getByLabelText('New password')).toBeInTheDocument();
    expect(screen.getByLabelText('Confirm new password')).toBeInTheDocument();
    expect(screen.getByText(/At least 8 characters/)).toBeInTheDocument();
  });

  it('uses password inputs so nothing is shown on screen', async () => {
    await renderAccount();

    for (const label of ['Current password', 'New password', 'Confirm new password']) {
      expect(screen.getByLabelText(label)).toHaveAttribute('type', 'password');
    }
  });

  it('hints that the password manager should generate, not autofill', async () => {
    await renderAccount();

    expect(screen.getByLabelText('Current password')).toHaveAttribute(
      'autocomplete',
      'current-password',
    );
    expect(screen.getByLabelText('New password')).toHaveAttribute(
      'autocomplete',
      'new-password',
    );
  });
});

describe('settings navigation', () => {
  beforeEach(() => {
    backend = installFakeBackend({ session: userWithRole('viewer') });
  });

  it('is a URL per section, so a reload stays put', async () => {
    renderApp('/settings/account');
    await waitForAuthToResolve();

    const nav = await screen.findByRole('navigation', { name: 'Settings sections' });
    const link = within(nav).getByRole('link', { name: /My account/ });
    expect(link).toHaveAttribute('aria-current', 'page');
    // And the panel it points at is the one rendered.
    expect(await screen.findByText('Your account')).toBeInTheDocument();
  });

  it('shows the General section at the settings index', async () => {
    renderApp('/settings');
    await waitForAuthToResolve();

    expect(await screen.findByText('Installation')).toBeInTheDocument();
  });

  it('keeps the Settings item highlighted in the primary navigation', async () => {
    renderApp('/settings/account');
    await waitForAuthToResolve();
    await screen.findByText('Your account');

    const primary = screen.getByRole('navigation', { name: 'Primary' });
    expect(within(primary).getByRole('link', { name: 'Settings' })).toHaveAttribute(
      'aria-current',
      'page',
    );
  });

  it('navigates between sections without a reload', async () => {
    const user = userEvent.setup();
    renderApp('/settings/account');
    await waitForAuthToResolve();
    await screen.findByText('Your account');

    const nav = screen.getByRole('navigation', { name: 'Settings sections' });
    await user.click(within(nav).getByRole('link', { name: /Sessions/ }));

    expect(await screen.findByText('Active sessions')).toBeInTheDocument();
  });

  it('does not render a save notice for settings that are not persisted', async () => {
    // The General section is read-only by design: the control plane is
    // configured by environment variables, validated at startup and frozen.
    renderApp('/settings');
    await waitForAuthToResolve();
    await screen.findByText('Installation');

    expect(screen.queryByRole('button', { name: /save/i })).not.toBeInTheDocument();
    expect(screen.getByText(/nothing to edit here/i)).toBeInTheDocument();
  });

  it('marks the unbuilt configuration as planned rather than faking it', async () => {
    renderApp('/settings');
    await waitForAuthToResolve();

    expect(await screen.findByText(/Planned for Phase 7 onwards/)).toBeInTheDocument();
  });

  it('shows the real control plane version', async () => {
    renderApp('/settings');
    await waitForAuthToResolve();

    expect(await screen.findByText('0.1.0')).toBeInTheDocument();
    await waitFor(() => {
      expect(backend.requestsFor('/info')).toHaveLength(1);
    });
  });
});
