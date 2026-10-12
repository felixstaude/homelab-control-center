import { Server } from 'lucide-react';

import { NotImplemented } from '@/components/not-implemented';
import { PageHeader } from '@/components/page-header';

export function HostsPage() {
  return (
    <>
      <PageHeader
        title="Hosts"
        // Duplicated by the shell header, which renders this page name as
        // the `<h1>`. See `PageHeader`.
        titleHidden
        description="Linux servers reporting through the Homelab Control Center agent."
      />
      <NotImplemented
        icon={Server}
        title="Host monitoring"
        summary="Register Linux hosts, pair them with an agent, and track their resources and availability centrally."
        milestone="Phase 1 and Phase 3"
        planned={[
          'Host registration with agent enrolment tokens',
          'CPU, memory, swap, disk and network metrics',
          'Availability and uptime tracking',
          'Hardware and OS inventory',
          'Historical resource usage with retention policies',
          'Per-host detail view with live updates',
        ]}
      />
    </>
  );
}
