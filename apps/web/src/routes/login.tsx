/**
 * Login page.
 *
 * The one public, interactive screen. Styled from the same tokens as the
 * console — the brand mark, a card on the base surface, the faint instrument
 * grid — so it reads as the front door of this product rather than a generic
 * sign-in template. No illustration, no animation: an operator reaching this
 * page wants to get past it.
 */

import { Eye, EyeOff, LogIn } from 'lucide-react';
import { useEffect, useId, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';

import { isApiError } from '@/api/errors';
import { useAuth } from '@/auth/use-auth';
import { FullPagePanel } from '@/components/layout/full-page-panel';
import { Alert } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Field } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import { Spinner } from '@/components/ui/spinner';
import { safeRedirectTarget, type RedirectState } from '@/lib/redirect';

/** What to tell the user about how they arrived here. */
function arrivalNotice(
  reason: 'no_session' | 'session_expired' | 'signed_out',
): { variant: 'info'; title: string; body: string } | null {
  switch (reason) {
    case 'session_expired':
      return {
        variant: 'info',
        title: 'Your session ended',
        body: 'Sessions expire after a period of inactivity, and when an administrator revokes them. Sign in to continue.',
      };
    case 'signed_out':
      return {
        variant: 'info',
        title: 'You are signed out',
        // Confirmation of something the user just did, so it describes their
        // action and what it achieved. The earlier wording — "your session was
        // revoked on the server" — described it as something that happened to
        // them, which is the expiry case's message, not this one.
        body: 'Your session has been closed on the server. Sign in again to continue.',
      };
    case 'no_session':
      // A first visit needs no explanation.
      return null;
  }
}

export function LoginPage() {
  const { state, signIn, isSigningIn } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [passwordVisible, setPasswordVisible] = useState(false);
  /** Set on submit, so a field error appears only after an attempt. */
  const [submitted, setSubmitted] = useState(false);

  const usernameId = useId();
  const passwordId = useId();
  const usernameRef = useRef<HTMLInputElement>(null);
  /**
   * Whether a submission is already in flight.
   *
   * A ref, not state, and deliberately so. The submit button is disabled from
   * `isSigningIn`, but that only takes effect once React has re-rendered — and
   * a double-click delivers its second event before that, so the handler ran
   * twice and sent two login requests. That created a second session nobody
   * would ever revoke and spent a second attempt from the throttle budget.
   *
   * A ref updates synchronously, so the guard holds within the same tick that
   * the first click is being handled. The `disabled` attribute stays as the
   * visible affordance; this is what makes it true.
   */
  const inFlight = useRef(false);

  // Focus the first field on arrival, so a keyboard user can type straight
  // away rather than tabbing past the page furniture.
  useEffect(() => {
    usernameRef.current?.focus();
  }, []);

  const failure = state.status === 'unauthenticated' ? state.error : null;
  const notice =
    state.status === 'unauthenticated' && !failure ? arrivalNotice(state.reason) : null;

  const usernameMissing = submitted && username.trim() === '';
  const passwordMissing = submitted && password === '';

  /**
   * Clear the password field after an attempt.
   *
   * `submitted` is reset with it, and that pairing matters: without it the
   * field this code just emptied is immediately flagged "Enter your
   * password", so a wrong-password attempt showed the server's message *and*
   * a validation error about a field the user had in fact filled in. Caught
   * by `shows the server message for invalid credentials`.
   */
  const clearPassword = () => {
    setPassword('');
    setSubmitted(false);
  };

  // Typed by what it uses rather than by `FormEvent`, which React 19's types
  // deprecate. The JSX below forwards the event, so inference at the call site
  // keeps this honest without naming a deprecated type.
  const handleSubmit = async (event: { preventDefault: () => void }) => {
    event.preventDefault();
    setSubmitted(true);

    // Client-side validation covers only emptiness. Anything more would be
    // the client second-guessing a policy the server owns, and a username
    // rule guessed here could reject an account that actually exists.
    if (username.trim() === '' || password === '') return;

    // A second submission while the first is unresolved is never what the
    // user meant, and it costs them an attempt from their own budget.
    if (inFlight.current) return;
    inFlight.current = true;

    try {
      await signIn({ username, password });
      // Clear the password from component state the moment it is no longer
      // needed. It is still in memory until React drops the closure, but
      // leaving it in state keeps it in a live component tree.
      clearPassword();
      // `replace`, so Back does not return to the login page of a session the
      // user has just started.
      void navigate(safeRedirectTarget(location.state as RedirectState), { replace: true });
    } catch {
      // The provider has already stored the typed error for display below.
      // Nothing is logged: a failed login must not put a username, let alone
      // a password, into the console.
      clearPassword();
    } finally {
      // `finally`, so a failed attempt leaves the form usable. Releasing it
      // only on success would turn one wrong password into a dead form.
      inFlight.current = false;
    }
  };

  const rateLimited = isApiError(failure) && failure.kind === 'rate_limited';

  return (
    <FullPagePanel
      width="sm"
      footer={
        <>
          Homelab Control Center · self-hosted
          <br />
          Sessions are server-side and revoked on sign-out.
        </>
      }
    >
      <h1 className="text-text-primary text-base font-semibold">Sign in</h1>
      <p className="text-text-muted mt-1 text-xs">
        Use the administrator account created during setup.
      </p>

      {notice ? (
        <Alert variant={notice.variant} title={notice.title} className="mt-4">
          {notice.body}
        </Alert>
      ) : null}

      {failure ? (
        <Alert
          variant={rateLimited ? 'warn' : 'critical'}
          title={rateLimited ? 'Too many attempts' : 'Sign-in failed'}
          className="mt-4"
        >
          {/*
           * The server's message, verbatim. For bad credentials it is
           * deliberately identical for a wrong password, an unknown
           * username and a disabled account, so this page cannot be
           * used to discover which usernames exist.
           */}
          {failure.message}
          {rateLimited && failure.retryAfterSeconds !== null ? (
            <> Try again in about {failure.retryAfterSeconds} seconds.</>
          ) : null}
        </Alert>
      ) : null}

      <form
        onSubmit={(event) => {
          void handleSubmit(event);
        }}
        className="mt-5 flex flex-col gap-4"
        noValidate
      >
        <Field
          id={usernameId}
          label="Username"
          error={usernameMissing ? 'Enter your username.' : null}
        >
          {(controlProps) => (
            <Input
              {...controlProps}
              ref={usernameRef}
              name="username"
              value={username}
              onChange={(event) => {
                setUsername(event.target.value);
              }}
              // Lets a password manager recognise and fill the pair.
              autoComplete="username"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
              required
              disabled={isSigningIn}
            />
          )}
        </Field>

        <Field
          id={passwordId}
          label="Password"
          error={passwordMissing ? 'Enter your password.' : null}
        >
          {(controlProps) => (
            <div className="relative">
              <Input
                {...controlProps}
                name="password"
                type={passwordVisible ? 'text' : 'password'}
                value={password}
                onChange={(event) => {
                  setPassword(event.target.value);
                }}
                autoComplete="current-password"
                required
                disabled={isSigningIn}
                className="pr-10"
              />
              <button
                type="button"
                onClick={() => {
                  setPasswordVisible((visible) => !visible);
                }}
                // The control's purpose and its current state, so a
                // screen reader announces both.
                aria-label={passwordVisible ? 'Hide password' : 'Show password'}
                aria-pressed={passwordVisible}
                className={cnToggle}
                disabled={isSigningIn}
              >
                {passwordVisible ? (
                  <EyeOff className="size-4" aria-hidden="true" />
                ) : (
                  <Eye className="size-4" aria-hidden="true" />
                )}
              </button>
            </div>
          )}
        </Field>

        <Button
          type="submit"
          variant="primary"
          size="lg"
          // Disabled while in flight so a second Enter cannot spend
          // another attempt from the throttle budget.
          disabled={isSigningIn}
          className="mt-1 w-full"
        >
          {isSigningIn ? (
            <>
              <Spinner />
              Signing in…
            </>
          ) : (
            <>
              <LogIn />
              Sign in
            </>
          )}
        </Button>
      </form>
    </FullPagePanel>
  );
}

/** Shared class list for the reveal toggle, kept out of the JSX for legibility. */
const cnToggle = [
  'text-text-faint hover:text-text-secondary absolute top-1/2 right-1 -translate-y-1/2',
  'flex size-7 items-center justify-center rounded-md',
  'transition-colors duration-150',
  'outline-none focus-visible:outline-2 focus-visible:outline-offset-2',
  'focus-visible:outline-(--color-focus-ring)',
  'disabled:pointer-events-none disabled:opacity-45',
].join(' ');
