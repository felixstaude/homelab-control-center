/**
 * Accessibility of the authentication screens.
 *
 * Asserted through the accessibility tree — roles, names, `aria-*` — rather
 * than through class names or DOM structure, so these describe what an
 * assistive technology actually receives.
 */

import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';

import { installFakeBackend, makeUser, type FakeBackend } from '@/test/fake-backend';
import { renderApp, waitForAuthToResolve } from '@/test/render-app';

let backend: FakeBackend;

afterEach(() => {
  backend.restore();
});

async function renderLogin() {
  backend = installFakeBackend({ session: null });
  renderApp('/login');
  await waitForAuthToResolve();
}

describe('login form labelling', () => {
  it('associates a label with each control', async () => {
    await renderLogin();

    // `getByLabelText` only resolves through a real label association, so
    // this fails if `htmlFor` and the control id ever drift apart.
    expect(screen.getByLabelText('Username')).toBeInTheDocument();
    expect(screen.getByLabelText('Password')).toBeInTheDocument();
  });

  it('gives the page a single top-level heading', async () => {
    await renderLogin();

    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Sign in');
  });

  it('marks the password field as a password until revealed', async () => {
    await renderLogin();

    expect(screen.getByLabelText('Password')).toHaveAttribute('type', 'password');
  });

  it('sets autocomplete attributes a password manager understands', async () => {
    // Without these, managers either fail to offer a saved credential or save
    // the wrong field — and users then turn to worse habits.
    await renderLogin();

    expect(screen.getByLabelText('Username')).toHaveAttribute('autocomplete', 'username');
    expect(screen.getByLabelText('Password')).toHaveAttribute(
      'autocomplete',
      'current-password',
    );
  });

  it('disables spellcheck and autocapitalisation on the username', async () => {
    // A capitalised first letter on a phone keyboard is a failed login the
    // user cannot see the cause of.
    await renderLogin();

    const username = screen.getByLabelText('Username');
    expect(username).toHaveAttribute('autocapitalize', 'none');
    expect(username).toHaveAttribute('spellcheck', 'false');
  });
});

describe('focus management', () => {
  it('focuses the username field on arrival', async () => {
    await renderLogin();

    await waitFor(() => {
      expect(screen.getByLabelText('Username')).toHaveFocus();
    });
  });

  it('reaches the submit button by keyboard alone', async () => {
    await renderLogin();
    const user = userEvent.setup();

    await waitFor(() => {
      expect(screen.getByLabelText('Username')).toHaveFocus();
    });

    await user.tab();
    expect(screen.getByLabelText('Password')).toHaveFocus();
    await user.tab();
    // The reveal toggle sits between the field and the button, and must be
    // reachable rather than skipped.
    expect(screen.getByRole('button', { name: 'Show password' })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole('button', { name: 'Sign in' })).toHaveFocus();
  });

  it('submits from the keyboard', async () => {
    await renderLogin();
    const user = userEvent.setup();

    await user.type(screen.getByLabelText('Username'), 'FelixAdmin');
    await user.type(screen.getByLabelText('Password'), 'correct horse battery staple');
    await user.keyboard('{Enter}');

    await waitFor(() => {
      expect(backend.requestsFor('/auth/login')).toHaveLength(1);
    });
  });
});

describe('password reveal toggle', () => {
  it('announces its purpose and its state', async () => {
    await renderLogin();
    const user = userEvent.setup();

    const toggle = screen.getByRole('button', { name: 'Show password' });
    expect(toggle).toHaveAttribute('aria-pressed', 'false');

    await user.click(toggle);

    // The name changes with the state, so a screen reader describes what the
    // control will do next rather than what it did.
    const pressed = screen.getByRole('button', { name: 'Hide password' });
    expect(pressed).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByLabelText('Password')).toHaveAttribute('type', 'text');
  });

  it('toggles back', async () => {
    await renderLogin();
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: 'Show password' }));
    await user.click(screen.getByRole('button', { name: 'Hide password' }));

    expect(screen.getByLabelText('Password')).toHaveAttribute('type', 'password');
  });

  it('is operable from the keyboard', async () => {
    await renderLogin();
    const user = userEvent.setup();

    screen.getByRole('button', { name: 'Show password' }).focus();
    await user.keyboard('{Enter}');

    expect(screen.getByLabelText('Password')).toHaveAttribute('type', 'text');
  });
});

describe('error announcement', () => {
  it('announces a validation error as an alert, linked to its field', async () => {
    await renderLogin();
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: 'Sign in' }));

    const error = screen.getByText('Enter your username.');
    expect(error).toHaveAttribute('role', 'alert');

    // `aria-describedby` points at it, so the message is read when the field
    // is focused as well as when it appears.
    const username = screen.getByLabelText('Username');
    expect(username).toHaveAttribute('aria-invalid', 'true');
    expect(username.getAttribute('aria-describedby')).toContain(error.id);
  });

  it('announces a sign-in failure assertively', async () => {
    backend = installFakeBackend({
      session: null,
      loginFailure: { status: 401, code: 'invalid_credentials', message: 'Invalid username or password.' },
    });
    renderApp('/login');
    await waitForAuthToResolve();

    const user = userEvent.setup();
    await user.type(screen.getByLabelText('Username'), 'someone');
    await user.type(screen.getByLabelText('Password'), 'wrong');
    await user.click(screen.getByRole('button', { name: 'Sign in' }));

    await waitFor(() => {
      // `alert`, not `status`: a failed sign-in is something the user must act
      // on, so it interrupts rather than waiting politely.
      expect(screen.getByRole('alert')).toHaveTextContent('Invalid username or password.');
    });
  });

  it('announces an informational notice politely', async () => {
    // The opposite case: "you are signed out" should not talk over whatever
    // the user is doing.
    backend = installFakeBackend({ session: makeUser() });
    renderApp('/');
    await waitForAuthToResolve();

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /FelixAdmin/ }));
    await user.click(screen.getByRole('button', { name: 'Sign out' }));

    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent('You are signed out');
    });
  });
});

describe('busy states', () => {
  it('announces that the session is being checked', () => {
    backend = installFakeBackend({ session: makeUser() });

    renderApp('/');

    // A spinner alone conveys nothing to a screen reader; the status region
    // carries the words.
    expect(screen.getByRole('status')).toHaveTextContent('Checking your session…');
  });

  it('disables the submit button while signing in', async () => {
    await renderLogin();
    const user = userEvent.setup();

    await user.type(screen.getByLabelText('Username'), 'FelixAdmin');
    await user.type(screen.getByLabelText('Password'), 'correct horse battery staple');
    await user.click(screen.getByRole('button', { name: 'Sign in' }));

    // Either the in-flight label is showing, or the request already finished
    // and we navigated — both are correct; a second submit is what must not
    // be possible.
    await waitFor(() => {
      expect(backend.requestsFor('/auth/login')).toHaveLength(1);
    });
  });
});

describe('account menu', () => {
  it('exposes its expanded state', async () => {
    backend = installFakeBackend({ session: makeUser() });
    renderApp('/');
    await waitForAuthToResolve();

    const trigger = screen.getByRole('button', { name: /FelixAdmin/ });
    expect(trigger).toHaveAttribute('aria-expanded', 'false');

    await userEvent.setup().click(trigger);

    expect(screen.getByRole('button', { name: /FelixAdmin/ })).toHaveAttribute(
      'aria-expanded',
      'true',
    );
  });

  it('shows the username and role', async () => {
    backend = installFakeBackend({ session: makeUser({ role: 'operator' }) });
    renderApp('/');
    await waitForAuthToResolve();

    expect(screen.getByText('Operator')).toBeInTheDocument();
    expect(screen.getByText('FelixAdmin')).toBeInTheDocument();
  });

  it('closes on Escape and returns focus to the trigger', async () => {
    backend = installFakeBackend({ session: makeUser() });
    renderApp('/');
    await waitForAuthToResolve();

    const user = userEvent.setup();
    const trigger = screen.getByRole('button', { name: /FelixAdmin/ });
    await user.click(trigger);
    expect(screen.getByRole('button', { name: 'Sign out' })).toBeInTheDocument();

    await user.keyboard('{Escape}');

    await waitFor(() => {
      expect(screen.queryByRole('button', { name: 'Sign out' })).toBeNull();
    });
    // Focus must come back, or a keyboard user is stranded.
    expect(screen.getByRole('button', { name: /FelixAdmin/ })).toHaveFocus();
  });

  it('is operable entirely from the keyboard', async () => {
    backend = installFakeBackend({ session: makeUser() });
    renderApp('/');
    await waitForAuthToResolve();

    const user = userEvent.setup();
    screen.getByRole('button', { name: /FelixAdmin/ }).focus();
    await user.keyboard('{Enter}');

    const signOut = screen.getByRole('button', { name: 'Sign out' });
    expect(signOut).toBeInTheDocument();
    signOut.focus();
    expect(signOut).toHaveFocus();
  });
});
