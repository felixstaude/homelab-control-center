import type { ReactNode } from 'react';

import { useQuery } from '@tanstack/react-query';
import { Settings2 } from 'lucide-react';

import { fetchApiInfo } from '@/api/meta';
import { ErrorPanel, LoadingRows } from '@/components/states/panel-states';
import { NotImplemented } from '@/components/not-implemented';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { queryKeys } from '@/lib/query-keys';

/**
 * General settings.
 *
 * Deliberately **not** a form. There is no server-side configuration surface
 * to write to: the control plane is configured entirely by environment
 * variables, validated once at startup and immutable thereafter
 * (`docs/ARCHITECTURE.md` §12.3, `Settings(frozen=True)`). Rendering editable
 * fields here would be a placeholder that pretends to work, and showing a
 * "Saved" notice for a value that goes nowhere is the specific failure mode
 * the engineering rules name.
 *
 * So this section shows what the installation *is* — read from the
 * authenticated `/info` endpoint, which is real — and states plainly which
 * configuration arrives with which milestone.
 *
 * It also carries the licence and a link to the source. That is not
 * decoration: this is AGPL-3.0 software, and §13 requires anyone running a
 * *modified* version as a network service to offer its users the
 * corresponding source. Putting the link where the installation describes
 * itself means a fork that changes the code inherits somewhere obvious to
 * change the URL, rather than having to be told the obligation exists.
 */

/** Where this build's source lives. Change this if you publish a fork. */
const SOURCE_URL = 'https://github.com/felixstaude/homelab-control-center';
const SOURCE_LABEL = 'felixstaude/homelab-control-center';

export function SettingsGeneral() {
  const info = useQuery({
    queryKey: queryKeys.session.apiInfo(),
    queryFn: ({ signal }) => fetchApiInfo(signal),
    // Immutable for the life of the process, so there is nothing to refresh.
    staleTime: Number.POSITIVE_INFINITY,
  });

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <CardHeader>
          <div>
            <CardTitle>Installation</CardTitle>
            <CardDescription>
              Reported by the control plane. Configuration is read from the environment at
              startup and cannot be changed at runtime, so there is nothing to edit here.
            </CardDescription>
          </div>
        </CardHeader>
        <CardContent>
          {info.isPending ? (
            <LoadingRows rows={3} columns={2} />
          ) : info.isError ? (
            <ErrorPanel
              error={info.error}
              onRetry={() => {
                void info.refetch();
              }}
            />
          ) : (
            <dl className="divide-border-subtle divide-y text-sm">
              <Fact label="Control plane version" value={info.data.server_version} />
              <Fact label="API version" value={info.data.api_version} />
              <Fact label="Environment" value={info.data.environment} />
              <Fact label="Licence" value="AGPL-3.0-or-later" />
              <Fact
                label="Source code"
                value={
                  <a
                    href={SOURCE_URL}
                    target="_blank"
                    rel="noreferrer"
                    className="text-text-primary hover:text-accent"
                  >
                    {SOURCE_LABEL}
                  </a>
                }
              />
            </dl>
          )}
        </CardContent>
      </Card>

      <NotImplemented
        icon={Settings2}
        title="Installation configuration"
        summary="Monitoring modules and their intervals are configured under Monitoring, and hosts under Hosts. What is listed below has no screen anywhere yet, and arrives with the subsystem that owns it rather than as a form that saves nothing."
        milestone="Phase 7 onwards"
        planned={[
          'Per-host management capabilities, all default-off',
          'Notification destinations and their credentials',
          'Retention policy, editable rather than fixed at startup',
          'Agent credential rotation with an overlap window',
        ]}
      />
    </div>
  );
}

/** One label/value pair in the installation list. */
function Fact({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-2 py-2.5 first:pt-0 last:pb-0">
      <dt className="text-text-muted">{label}</dt>
      <dd className="text-text-primary font-mono text-xs">{value}</dd>
    </div>
  );
}
