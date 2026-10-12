/**
 * Access to the authentication state.
 *
 * A hook rather than exposing the context directly, so the "used outside the
 * provider" mistake fails loudly at the call site instead of producing a
 * `null` that reads as "unauthenticated" — which would silently render the
 * signed-out interface to a signed-in user.
 */

import { useContext } from 'react';

import { AuthContext, type AuthContextValue } from '@/auth/auth-context';
import type { Permissions } from '@/auth/permissions';

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (context === null) {
    throw new Error('useAuth must be used inside an <AuthProvider>');
  }
  return context;
}

/**
 * The current user's permissions, or everything denied.
 *
 * Denying when unauthenticated is the honest default: there is no user, so
 * there is nothing they may do. It also means a component may read
 * `permissions.administer` without first checking the auth status, and get a
 * safe answer rather than a crash.
 */
export function usePermissions(): Permissions {
  const { state } = useAuth();
  if (state.status === 'authenticated') return state.permissions;
  return {
    viewMonitoring: false,
    controlHosts: false,
    configureMonitoring: false,
    administer: false,
  };
}
