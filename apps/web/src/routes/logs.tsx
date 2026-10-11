import { ScrollText } from 'lucide-react';

import { NotImplemented } from '@/components/not-implemented';
import { PageHeader } from '@/components/page-header';

export function LogsPage() {
  return (
    <>
      <PageHeader
        title="Logs"
        description="Searchable, filterable logs collected from configured sources."
      />
      <NotImplemented
        icon={ScrollText}
        title="Log monitoring"
        summary="Collect logs from configured sources while preserving the original message and timestamp exactly as emitted. Parsing is configurable because real applications do not agree on log formats."
        milestone="Phase 3"
        planned={[
          'Ingestion from journald, files and container logs',
          'Original messages and timestamps preserved verbatim',
          'Full-text search with structured filters',
          'Live tailing over WebSocket',
          'Level detection where a structured level exists',
          'User-defined parsing rules for unstructured output',
        ]}
      />
    </>
  );
}
