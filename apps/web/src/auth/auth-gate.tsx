/**
 * Route protection.
 *
 * Two components:
 *
 * - {@link RequireAuth} wraps the protected part of the route tree.
 * - {@link RequireAnonymous} wraps login, so a signed-in user is not shown a
 *   sign-in form.
 *
 * ## The flash problem
 *
 * The bug this exists to avoid is rendering a protected page for a frame
 * before authentication resolves. It happens whenever "not yet known" and
 * "not authenticated" are the same value: the guard sees `false`, renders, and
 * then corrects itself — by which point the user has seen the dashboard shell,
 * and on a slow connection, read it.
 *
 * `AuthState` keeps `checking` distinct precisely so the guard can render
 * *nothing conclusive* until the answer arrives. A test asserts no protected
 * content appears during that window.
 *
 * ## This is not a security boundary
 *
 * A determined user can edit the client and reach any route. Every protected
 * route's data comes from the API, which is default-deny server-side
 * (ADR-031), so what they would reach is an empty shell. The guard exists for
 * the honest user's benefit, not against the dishonest one.
 */

import type { ReactNode } from 'react';
import { Navigate, useLocation } from 'react-router-dom';

import { useAuth } from '@/auth/use-auth';
import { BackendUnavailable } from '@/components/states/backend-unavailable';
import { FullPageSpinner } from '@/components/states/full-page-spinner';
import { DEFAULT_REDIRECT, safeRedirectTarget, type RedirectState } from '@/lib/redirect';

export function RequireAuth({ children }: { children: ReactNode }) {
  const { state, refresh } = useAuth();
  const location = useLocation();

  switch (state.status) {
    case 'checking':
      // Deliberately not a redirect and not the children: the answer is not
      // known yet, so neither would be true.
      return <FullPageSpinner label="Checking your session…" />;

    case 'unavailable':
      // The session may well be valid; the control plane is simply not
      // answering. Sending the user to sign in would be a lie, and signing in
      // would not work either.
      return (
        <BackendUnavailable
          error={state.error}
          onRetry={() => {
            void refresh();
          }}
        />
      );

    case 'setup_required':
      return <Navigate to="/setup" replace />;

    case 'unauthenticated':
      return (
        <Navigate
          to="/login"
          replace
          // Where they were going, so login can return them. Carried in
          // location state rather than a query parameter — it stays out of
          // the address bar and out of any proxy log — and validated before
          // use in `lib/redirect.ts`.
          state={{ from: `${location.pathname}${location.search}${location.hash}` }}
        />
      );

    case 'authenticated':
      return children;
  }
}

export function RequireAnonymous({ children }: { children: ReactNode }) {
  const { state, refresh } = useAuth();
  const location = useLocation();

  switch (state.status) {
    case 'checking':
      return <FullPageSpinner label="Checking your session…" />;

    case 'unavailable':
      // Shown rather than the login form: a sign-in attempt cannot succeed
      // while the control plane is unreachable, and letting the user type
      // their password into a form that is going to fail wastes an attempt
      // from their throttle budget.
      return (
        <BackendUnavailable
          error={state.error}
          onRetry={() => {
            void refresh();
          }}
        />
      );

    case 'setup_required':
      return <Navigate to="/setup" replace />;

    case 'authenticated':
      // Already signed in. Honour a recorded destination if there is one, so
      // that a mid-navigation sign-in still lands where the user was going.
      return <Navigate to={safeRedirectTarget(location.state as RedirectState)} replace />;

    case 'unauthenticated':
      return children;
  }
}

/**
 * Gate for the setup page itself.
 *
 * Public, but pointless once the installation is initialized — so a visitor
 * who arrives late is sent to sign in rather than shown instructions for a
 * command that will refuse to run. `RequireSetupIncomplete` deliberately
 * *renders* its children in the `authenticated` case too, because the page
 * handles the "setup just completed" transition itself.
 */
export function AllowDuringSetup({ children }: { children: ReactNode }) {
  const { state } = useAuth();

  if (state.status === 'checking') {
    return <FullPageSpinner label="Checking installation status…" />;
  }
  if (state.status === 'authenticated') {
    return <Navigate to={DEFAULT_REDIRECT} replace />;
  }
  return children;
}
