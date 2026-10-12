/**
 * Authentication provider.
 *
 * Owns the two queries the whole app's auth state is derived from, and the
 * sign-in / sign-out mutations.
 *
 * ## Both queries run in parallel
 *
 * Setup status and the current session are fetched together rather than
 * sequentially. Chaining them would be the obvious reading — "find out whether
 * setup is needed, then ask who I am" — but it costs a second round trip on
 * every cold load, and asking `/auth/me` on an uninitialized installation is
 * harmless: it answers 401, which the fetcher already treats as data.
 *
 * ## A 401 is data, not an error
 *
 * `fetchSession` catches 401 and returns `null`. "No session" is the ordinary
 * answer for an anonymous visitor, not a failure, and modelling it as a query
 * error would put it through retry and error-boundary machinery built for
 * faults. Only genuine faults — network, timeout, 5xx — reach the query's
 * error state, which is what makes `unavailable` meaningful.
 *
 * ## Cache isolation
 *
 * `queryClient.clear()` runs on both sign-in and sign-out. On sign-out the
 * reason is obvious. On **sign-in** it matters just as much: a different user
 * may be signing in on the same browser, and a cache entry surviving that
 * boundary would show them the previous user's data.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';

import { fetchCurrentUser, fetchSetupStatus, login, logout } from '@/api/auth';
import { ApiError, isApiError, isPermissionDenied, isSessionExpired } from '@/api/errors';
import type { CurrentUser, LoginCredentials } from '@/api/types';
import { AuthContext, type AuthContextValue } from '@/auth/auth-context';
import { type AuthState, type UnauthenticatedReason } from '@/auth/auth-state';
import { normalizeRole, permissionsFor } from '@/auth/permissions';
import { queryKeys } from '@/lib/query-keys';

/**
 * How long a resolved session is trusted without re-asking.
 *
 * Short, because the server can revoke a session at any moment — a logout
 * elsewhere, a deactivated account — and the client should notice reasonably
 * promptly. It is not a security boundary either way: every request is
 * authorized server-side regardless of what this cache says.
 */
const SESSION_STALE_TIME_MS = 30_000;

/** Setup status changes once in the lifetime of an installation. */
const SETUP_STALE_TIME_MS = 5 * 60_000;

/**
 * Shortest gap between session refetches triggered by a permission denial.
 *
 * A guard against a burst: a page with several admin-only queries can produce
 * three 403s in one render pass, and each one asking for the session back
 * would be three identical requests. Short enough that a genuine role change
 * is picked up on the next interaction, long enough that a storm is one call.
 */
const ROLE_RECHECK_THROTTLE_MS = 2_000;

/**
 * Resolve the session, treating "no session" as data.
 *
 * @returns the current user, or `null` when unauthenticated.
 */
async function fetchSession(signal: AbortSignal): Promise<CurrentUser | null> {
  try {
    return await fetchCurrentUser(signal);
  } catch (error) {
    if (isSessionExpired(error)) return null;
    throw error;
  }
}

/** Narrow an unknown query error to an `ApiError`. */
function asApiError(error: unknown): ApiError {
  if (isApiError(error)) return error;
  return new ApiError('unknown', 'The request failed.', { cause: error });
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();

  /**
   * Why the user is currently unauthenticated.
   *
   * Held outside the queries because it describes a *transition* — "your
   * session expired", "you signed out" — which no single query result can
   * express: both produce the same `null` session.
   */
  const [reason, setReason] = useState<UnauthenticatedReason>('no_session');
  const [signInError, setSignInError] = useState<ApiError | null>(null);

  /**
   * Whether a session was ever established in this tab.
   *
   * Lets a later `null` be reported as an expiry rather than as a first visit,
   * which is the difference between "Your session ended" and an unexplained
   * login page.
   *
   * State rather than a ref: the auth state is *derived* from it during
   * render, and a ref read during render is a side effect in disguise — the
   * render would not re-run when the value changed. The extra render when a
   * session first appears is the correct cost of that.
   */
  const [everAuthenticated, setEverAuthenticated] = useState(false);

  const setupQuery = useQuery({
    queryKey: queryKeys.public.setupStatus(),
    queryFn: ({ signal }) => fetchSetupStatus(signal),
    staleTime: SETUP_STALE_TIME_MS,
  });

  const sessionQuery = useQuery({
    queryKey: queryKeys.session.currentUser(),
    queryFn: ({ signal }) => fetchSession(signal),
    staleTime: SESSION_STALE_TIME_MS,
    // The window regaining focus is a good moment to notice a session that
    // was revoked while the tab sat in the background.
    refetchOnWindowFocus: true,
  });

  /**
   * Notice when a session is established, so a later disappearance can be
   * reported as an expiry rather than as a first visit.
   *
   * Driven by a cache subscription rather than by an effect that inspects
   * render output. Two reasons: the establishment of a session genuinely *is*
   * a cache event, and setting state synchronously in an effect body triggers
   * the cascading re-render that `react-hooks/set-state-in-effect` exists to
   * prevent. The subscription callback is an ordinary event, so the update is
   * scheduled the way any other event's would be.
   */
  useEffect(() => {
    const key = JSON.stringify(queryKeys.session.currentUser());
    return queryClient.getQueryCache().subscribe((event) => {
      if (event.type !== 'updated') return;
      if (JSON.stringify(event.query.queryKey) !== key) return;
      if (event.query.state.data) setEverAuthenticated(true);
    });
  }, [queryClient]);

  /**
   * Revalidate the cached identity when the server refuses a request.
   *
   * The scenario this exists for: an administrator changes an operator into a
   * viewer while that operator has the console open. The backend enforces the
   * new role on the operator's very next privileged request — the role is read
   * from the user row every time, never cached from login (ADR-028) — so
   * nothing is *granted* by the stale client state. What goes wrong without
   * this is the interface: it keeps offering administrative controls that now
   * answer 403, so the user is told "you do not have permission" by a screen
   * that is still inviting them to try.
   *
   * A 403 is therefore read as evidence that the cached identity may be out of
   * date, and the session is re-fetched. Deliberately **not** a sign-out: 403
   * means the session is fine and the role is not sufficient, and
   * `isSessionExpired` stays 401-only for exactly that reason.
   *
   * A **401** from any request gets the same treatment, and that half closes a
   * real gap. The session had previously resolved, so something ended it
   * elsewhere — an administrator revoked it, the account was deactivated, it
   * expired. The refetch resolves to `null`, which moves the state to
   * `unauthenticated` and sends the user to the login page with an
   * explanation. Without it the console kept rendering the authenticated shell
   * until the session query happened to go stale, so a deactivated user sat
   * looking at a page where nothing worked and nothing said why.
   *
   * The two cases take the same action for different reasons and that is
   * deliberate: both mean "ask the server who I am", and the *answer* is what
   * distinguishes them. Nothing here decides to sign anybody out — the
   * refetched identity does, which keeps one authority over that decision.
   *
   * A cache subscription rather than a global `onError`, because the
   * `QueryClient` is not constructible with a handler that can reach back into
   * it, and because both queries and mutations have to be covered — a denied
   * mutation is the more likely trigger, since that is what an operator was
   * trying to do.
   *
   * Why not polling: a timer that re-checks the role every few seconds would
   * spend requests continuously to catch an event that happens about never,
   * and the backend is already authoritative on every request. Why not a
   * WebSocket: a socket for role synchronisation alone is the kind of
   * machinery `docs/ARCHITECTURE.md` §3.3 exists to keep out.
   */
  const lastRoleRecheck = useRef(0);
  useEffect(() => {
    const recheck = (error: unknown) => {
      if (!isPermissionDenied(error) && !isSessionExpired(error)) return;
      const now = Date.now();
      if (now - lastRoleRecheck.current < ROLE_RECHECK_THROTTLE_MS) return;
      lastRoleRecheck.current = now;
      void queryClient.invalidateQueries({ queryKey: queryKeys.session.currentUser() });
    };

    const unsubscribeQueries = queryClient.getQueryCache().subscribe((event) => {
      if (event.type !== 'updated') return;
      recheck(event.query.state.error);
    });
    const unsubscribeMutations = queryClient.getMutationCache().subscribe((event) => {
      if (event.type !== 'updated') return;
      recheck(event.mutation.state.error);
    });

    return () => {
      unsubscribeQueries();
      unsubscribeMutations();
    };
  }, [queryClient]);

  const signInMutation = useMutation({
    mutationFn: (credentials: LoginCredentials) => login(credentials),
    onSuccess: (user) => {
      setSignInError(null);
      setEverAuthenticated(true);
      // Drop everything cached before this sign-in: a different user may be
      // signing in on this browser.
      queryClient.clear();
      queryClient.setQueryData(queryKeys.session.currentUser(), user);
    },
    onError: (error: unknown) => {
      setSignInError(asApiError(error));
    },
  });

  /**
   * Drop local authenticated state.
   *
   * Only called once the server has actually revoked the session, or has told
   * us it was already gone.
   */
  const clearLocalSession = useCallback(() => {
    setReason('signed_out');
    setSignInError(null);
    setEverAuthenticated(false);
    queryClient.clear();
    queryClient.setQueryData(queryKeys.session.currentUser(), null);
  }, [queryClient]);

  const signOutMutation = useMutation({
    mutationFn: () => logout(),
    onSuccess: clearLocalSession,
    onError: (error: unknown) => {
      /*
       * A failed sign-out must **not** clear local state.
       *
       * Server-side revocation is the boundary (ADR-028): if the server still
       * holds the session, the user is still signed in, and showing them the
       * signed-out interface would be a lie about the one thing that matters
       * here. Keeping the authenticated state also keeps the error visible —
       * an earlier version cleared state regardless, the route guard
       * redirected to the login page, and the explanation was unmounted
       * before anyone could read it. The user was then told nothing and
       * believed they had signed out of a session that was still live.
       *
       * The exception is a 401: that means the session is already gone, which
       * is the outcome sign-out wanted.
       */
      if (isSessionExpired(error)) clearLocalSession();
    },
  });

  const signIn = useCallback(
    async (credentials: LoginCredentials) => signInMutation.mutateAsync(credentials),
    [signInMutation],
  );

  const signOut = useCallback(async () => {
    // Rejects on failure, so the caller can show it. Deliberately not
    // swallowed: see `onError` above.
    await signOutMutation.mutateAsync();
  }, [signOutMutation]);

  const refresh = useCallback(async () => {
    await Promise.all([setupQuery.refetch(), sessionQuery.refetch()]);
  }, [setupQuery, sessionQuery]);

  const state = useMemo<AuthState>(() => {
    // Order matters. Reachability first, then initialization, then identity:
    // each answer is meaningless without the one before it.

    // A fault on either query means the control plane is not answering. Said
    // before anything else, so a network blip is never rendered as a logout.
    const transportError = setupQuery.error ?? sessionQuery.error;
    if (transportError) {
      return { status: 'unavailable', error: asApiError(transportError) };
    }

    // Still deciding. Explicitly not "unauthenticated": a route guard that
    // conflated them would flash the login page on every reload.
    if (setupQuery.isPending || sessionQuery.isPending) {
      return { status: 'checking' };
    }

    if (setupQuery.data && !setupQuery.data.initialized) {
      return { status: 'setup_required' };
    }

    const user = sessionQuery.data;
    if (user) {
      const role = normalizeRole(user.role);
      return {
        status: 'authenticated',
        user,
        role,
        permissions: permissionsFor(role),
      };
    }

    return {
      status: 'unauthenticated',
      reason: everAuthenticated ? 'session_expired' : reason,
      error: signInError,
    };
  }, [
    setupQuery.data,
    setupQuery.error,
    setupQuery.isPending,
    sessionQuery.data,
    sessionQuery.error,
    sessionQuery.isPending,
    everAuthenticated,
    reason,
    signInError,
  ]);

  const value = useMemo<AuthContextValue>(
    () => ({
      state,
      isSigningIn: signInMutation.isPending,
      isSigningOut: signOutMutation.isPending,
      signIn,
      signOut,
      refresh,
    }),
    [state, signInMutation.isPending, signOutMutation.isPending, signIn, signOut, refresh],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
