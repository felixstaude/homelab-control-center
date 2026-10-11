import { Settings } from 'lucide-react';

import { NotImplemented } from '@/components/not-implemented';
import { PageHeader } from '@/components/page-header';

export function SettingsPage() {
  return (
    <>
      <PageHeader
        title="Settings"
        description="Agents, users, integrations and retention policy."
      />
      <NotImplemented
        icon={Settings}
        title="Configuration"
        summary="Manage agent enrolment, user accounts and roles, notification destinations, and how long collected data is retained."
        milestone="Phase 1 and Phase 10"
        planned={[
          'Agent enrolment tokens with rotation and revocation',
          'User accounts, sessions and role assignment',
          'Role-based permissions for management actions',
          'Notification destination credentials',
          'Metrics and log retention policy',
          'Audit log of all management actions',
        ]}
      />
    </>
  );
}
