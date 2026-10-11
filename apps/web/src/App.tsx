import { QueryClientProvider } from '@tanstack/react-query';
import { useState } from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';

import { AppShell } from '@/components/layout/app-shell';
import { TooltipProvider } from '@/components/ui/tooltip';
import { createQueryClient } from '@/lib/query-client';
import { AlertsPage } from '@/routes/alerts';
import { DockerPage } from '@/routes/docker';
import { HostsPage } from '@/routes/hosts';
import { LogsPage } from '@/routes/logs';
import { NetworkPage } from '@/routes/network';
import { NotFoundPage } from '@/routes/not-found';
import { OverviewPage } from '@/routes/overview';
import { SettingsPage } from '@/routes/settings';

/**
 * Application root: providers and route table.
 *
 * The router itself lives in `main.tsx` so that tests can wrap `App` in a
 * `MemoryRouter` and drive navigation without a browser history.
 */
export function App() {
  // Created in state rather than at module scope so each mounted App — and
  // therefore each test — gets an isolated cache instead of sharing one.
  const [queryClient] = useState(createQueryClient);

  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider delayDuration={300}>
        <Routes>
          <Route element={<AppShell />}>
            <Route index element={<OverviewPage />} />
            <Route path="hosts" element={<HostsPage />} />
            <Route path="docker" element={<DockerPage />} />
            <Route path="logs" element={<LogsPage />} />
            <Route path="network" element={<NetworkPage />} />
            <Route path="alerts" element={<AlertsPage />} />
            <Route path="settings" element={<SettingsPage />} />
            {/* Legacy-friendly alias so /overview does not 404. */}
            <Route path="overview" element={<Navigate to="/" replace />} />
            <Route path="*" element={<NotFoundPage />} />
          </Route>
        </Routes>
      </TooltipProvider>
    </QueryClientProvider>
  );
}
