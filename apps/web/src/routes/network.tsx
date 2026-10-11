import { Network } from 'lucide-react';

import { NotImplemented } from '@/components/not-implemented';
import { PageHeader } from '@/components/page-header';

export function NetworkPage() {
  return (
    <>
      <PageHeader
        title="Network"
        description="Manually registered devices, their reachability, and how they relate to each other."
      />
      <NotImplemented
        icon={Network}
        title="Network monitoring"
        summary="Register devices by address, check them over ICMP and configurable TCP probes, and lay them out in a topology you define. Devices are added deliberately — the platform does not scan or probe your network unprompted."
        milestone="Phase 6"
        planned={[
          'Manual device registration by IP or hostname',
          'ICMP reachability checks',
          'Configurable TCP port checks',
          'Visual topology view',
          'User-defined relationships between devices',
          'Docker containers surfaced from connected hosts',
        ]}
      />
    </>
  );
}
