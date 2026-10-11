import { Bell } from 'lucide-react';

import { NotImplemented } from '@/components/not-implemented';
import { PageHeader } from '@/components/page-header';

export function AlertsPage() {
  return (
    <>
      <PageHeader
        title="Alerts"
        description="Notification rules you define, and the history of what they fired."
      />
      <NotImplemented
        icon={Bell}
        title="Notification rules"
        summary="A rule engine giving you complete control over what notifies you and when. Rules are explicit and testable; the platform never invents explanations for an error or decides on its own what matters."
        milestone="Phase 8"
        planned={[
          'Conditions on log level, message, container, host and source',
          'Exact, substring and regular-expression matching',
          'AND/OR logic with nested condition groups',
          'Per-rule cooldowns to prevent notification storms',
          'Destinations including Ntfy, webhooks and email',
          'Dry-run testing against sample or historical events',
        ]}
      />
    </>
  );
}
