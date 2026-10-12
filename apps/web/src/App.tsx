import { QueryClientProvider } from '@tanstack/react-query';
import { useState } from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';

import { AllowDuringSetup, RequireAnonymous, RequireAuth } from '@/auth/auth-gate';
import { AuthProvider } from '@/auth/auth-provider';
import { ErrorBoundary } from '@/components/error-boundary';
import { AppShell } from '@/components/layout/app-shell';
import { TooltipProvider } from '@/components/ui/tooltip';
import { createQueryClient } from '@/lib/query-client';
import { AlertsPage } from '@/routes/alerts';
import { DockerPage } from '@/routes/docker';
import { HostsPage } from '@/routes/hosts';
import { HostDetailPage } from '@/routes/hosts/host-detail';
import { LoginPage } from '@/routes/login';
import { LogsPage } from '@/routes/logs';
import { NetworkPage } from '@/routes/network';
import { NotFoundPage } from '@/routes/not-found';
import { OverviewPage } from '@/routes/overview';
import { SettingsAccount } from '@/routes/settings/account';
import { SettingsAudit } from '@/routes/settings/audit';
import { SettingsGeneral } from '@/routes/settings/general';
import { SettingsLayout } from '@/routes/settings/layout';
import { SettingsSessions } from '@/routes/settings/sessions';
import { SettingsUsers } from '@/routes/settings/users';
import { SetupPage } from '@/routes/setup';

/**
 * Application root: providers and route table.
 *
 * The router itself lives in `main.tsx` so that tests can wrap `App` in a
 * `MemoryRouter` and drive navigation without a browser history.
 *
 * ## Provider order
 *
 * `ErrorBoundary` is outermost, so a render error anywhere below it — including
 * in a provider — produces the recovery screen rather than a blank page.
 *
 * `QueryClientProvider` wraps `AuthProvider` because the auth state is derived
 * from two queries; the provider is a consumer of the cache, not its owner.
 *
 * ## Route structure
 *
 * Three groups, and the grouping is the access-control statement:
 *
 * - `/setup` — public, and only useful before initialization.
 * - `/login` — public, and only useful before signing in.
 * - everything else — behind `RequireAuth`, inside the existing `AppShell`.
 *
 * A new page added inside the shell is protected by where it sits, which is
 * the same reasoning the backend applies to its protected router (ADR-031):
 * the safe thing should be what happens by default.
 */
export function App() {
  // Created in state rather than at module scope so each mounted App — and
  // therefore each test — gets an isolated cache instead of sharing one.
  const [queryClient] = useState(createQueryClient);

  return (
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <TooltipProvider delayDuration={300}>
            <Routes>
              <Route
                path="/setup"
                element={
                  <AllowDuringSetup>
                    <SetupPage />
                  </AllowDuringSetup>
                }
              />
              <Route
                path="/login"
                element={
                  <RequireAnonymous>
                    <LoginPage />
                  </RequireAnonymous>
                }
              />

              <Route
                element={
                  <RequireAuth>
                    <AppShell />
                  </RequireAuth>
                }
              >
                <Route index element={<OverviewPage />} />
                <Route path="hosts" element={<HostsPage />} />
                {/*
                 * The host id is in the path, which is correct: a host *is* a
                 * place worth linking to and returning to, unlike a filter
                 * halfway through being typed. No secret ever goes here —
                 * the enrollment credential lives in one response body and in
                 * a dialog's local state, never in a URL that the browser's
                 * history, the `Referer` header and every proxy in between
                 * would record (milestone 1.8).
                 */}
                <Route path="hosts/:hostId" element={<HostDetailPage />} />
                <Route path="docker" element={<DockerPage />} />
                <Route path="logs" element={<LogsPage />} />
                <Route path="network" element={<NetworkPage />} />
                <Route path="alerts" element={<AlertsPage />} />

                {/*
                 * Settings is nested, so each section is a URL: linkable,
                 * reloadable, and navigable with the browser's own Back
                 * button. See `routes/settings/layout.tsx` for why that beats
                 * tab state held in a component.
                 *
                 * The administrative sections are inside the same guard as
                 * everything else and are not separately gated here. Placing
                 * them behind a client-side role check would imply the client
                 * is the boundary; it is not, and each panel explains the
                 * refusal the server is already making
                 * (`docs/SECURITY.md`).
                 */}
                <Route path="settings" element={<SettingsLayout />}>
                  <Route index element={<SettingsGeneral />} />
                  <Route path="users" element={<SettingsUsers />} />
                  <Route path="account" element={<SettingsAccount />} />
                  <Route path="sessions" element={<SettingsSessions />} />
                  <Route path="audit" element={<SettingsAudit />} />
                </Route>

                {/* Legacy-friendly alias so /overview does not 404. */}
                <Route path="overview" element={<Navigate to="/" replace />} />
                <Route path="*" element={<NotFoundPage />} />
              </Route>
            </Routes>
          </TooltipProvider>
        </AuthProvider>
      </QueryClientProvider>
    </ErrorBoundary>
  );
}
