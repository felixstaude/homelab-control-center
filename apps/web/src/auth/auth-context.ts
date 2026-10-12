/**
 * The authentication context object.
 *
 * In its own module so that `auth-provider.tsx` exports only a component.
 * Mixing a context export into a component file breaks Fast Refresh: an edit
 * to the provider recreates the context, every consumer sees a new identity,
 * and the dev server falls back to a full reload.
 */

import { createContext } from 'react';

import type { CurrentUser, LoginCredentials } from '@/api/types';
import type { AuthState } from '@/auth/auth-state';

export type AuthContextValue = {
  readonly state: AuthState;
  /** True while a sign-in request is in flight. */
  readonly isSigningIn: boolean;
  /** True while a sign-out request is in flight. */
  readonly isSigningOut: boolean;
  /**
   * Authenticate. Resolves on success and rejects with an `ApiError`
   * otherwise, so a form can show the failure inline.
   */
  readonly signIn: (credentials: LoginCredentials) => Promise<CurrentUser>;
  /** Revoke the session server-side, then clear local state. */
  readonly signOut: () => Promise<void>;
  /** Re-check setup status and the session. Used by retry controls. */
  readonly refresh: () => Promise<void>;
};

export const AuthContext = createContext<AuthContextValue | null>(null);
