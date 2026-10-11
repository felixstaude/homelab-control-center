import { Boxes } from 'lucide-react';

import { NotImplemented } from '@/components/not-implemented';
import { PageHeader } from '@/components/page-header';

export function DockerPage() {
  return (
    <>
      <PageHeader
        title="Docker"
        description="Containers discovered on connected Docker hosts, with user-initiated lifecycle controls."
      />
      <NotImplemented
        icon={Boxes}
        title="Docker monitoring and management"
        summary="Automatically discover containers through the agent's local Docker socket, monitor their state, and run a strictly limited set of explicitly authorised lifecycle commands."
        milestone="Phase 4"
        planned={[
          'Automatic container discovery per host',
          'Container state and resource usage',
          'Log viewing with search and filters',
          'Start, stop and restart, each audited',
          'Compose project grouping',
          'No shell access and no arbitrary command execution, by design',
        ]}
      />
    </>
  );
}
