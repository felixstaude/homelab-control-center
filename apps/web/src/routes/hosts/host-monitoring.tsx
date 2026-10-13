import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Check, RotateCcw, TriangleAlert } from 'lucide-react';
import { useState } from 'react';

import {
  fetchHostMonitoring,
  fetchModuleCatalogue,
  resetHostOverride,
  setHostOverride,
} from '@/api/monitoring';
import type {
  ConfigSync,
  EffectiveModule,
  Host,
  ModuleDescriptor,
  SettingSource,
} from '@/api/types';
import { usePermissions } from '@/auth/use-auth';
import { ErrorPanel, MutationError } from '@/components/states/panel-states';
import { Alert } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Select } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { Spinner } from '@/components/ui/spinner';
import { formatDateTime, machineDateTime, relativeTime } from '@/lib/datetime';
import { queryKeys } from '@/lib/query-keys';
import { describeSeconds } from '@/routes/hosts/metric-format';

/**
 * What this host collects, and whether its agent is actually doing it.
 *
 * ## Default-deny is visible, not just implemented
 *
 * §2.1: every module starts disabled, and enrolling a host activates nothing.
 * So this card's resting state for a new host is five modules all off, and the
 * copy says that is deliberate rather than leaving an operator to wonder
 * whether something failed.
 *
 * ## Saved is not active, and the card never conflates them
 *
 * §23. A controller that has written a revision has not thereby changed what a
 * machine is doing — the agent has to receive it, validate it and apply it,
 * and it can refuse. {@link SyncBanner} reports the gap, and the per-module
 * rows show the *desired* state while the banner qualifies the whole card. The
 * one thing this must never do is show a switch as on because the request
 * succeeded.
 *
 * ## Every control here saves
 *
 * §24: no decorative toggles. Each control is wired to the endpoint that
 * changes it, is disabled while in flight, and surfaces the server's own
 * refusal — including the interval floors, which are the server's rules and
 * are not reimplemented here as client-side validation that could disagree.
 *
 * ## Three states, not two
 *
 * A module on a host is `on`, `off`, or `inherit`. The third is not a tidier
 * spelling of "off": inheriting means the global default decides, so a host
 * set to inherit starts collecting the moment somebody enables the module
 * globally, and a host set to `off` does not. A two-state switch would make
 * that difference unrepresentable, so this is a three-way select.
 */

/** How often the sync state refetches, so a pending apply resolves on screen. */
const POLL_INTERVAL_MS = 5000;

type ModuleChoice = 'on' | 'off' | 'inherit';

export function HostMonitoringCard({ host }: { host: Host }) {
  const hostId = host.host_id;
  const permissions = usePermissions();

  const monitoring = useQuery({
    queryKey: queryKeys.session.hostMonitoring(hostId),
    queryFn: ({ signal }) => fetchHostMonitoring(hostId, signal),
    refetchInterval: POLL_INTERVAL_MS,
  });

  const catalogue = useQuery({
    queryKey: queryKeys.session.moduleCatalogue(),
    queryFn: ({ signal }) => fetchModuleCatalogue(signal),
    // The registry is a property of the release, not of the session.
    staleTime: Infinity,
  });

  return (
    <Card>
      <CardHeader>
        <div className="min-w-0">
          <CardTitle>Monitoring</CardTitle>
          <CardDescription>
            Which modules this host collects, and what its agent has confirmed it is running.
            Every module starts switched off: enrolling a host does not begin collecting
            anything until somebody asks for it.
          </CardDescription>
        </div>
      </CardHeader>

      <CardContent className="flex flex-col gap-5">
        {monitoring.isPending ? (
          <div className="flex flex-col gap-3">
            <Skeleton className="h-12" />
            <Skeleton className="h-12" />
            <Skeleton className="h-12" />
          </div>
        ) : monitoring.isError ? (
          <ErrorPanel
            error={monitoring.error}
            title="Could not load the monitoring configuration"
            onRetry={() => {
              void monitoring.refetch();
            }}
          />
        ) : (
          <>
            <SyncBanner sync={monitoring.data.sync} host={host} />

            {monitoring.data.problems.length > 0 ? (
              <Alert variant="warn" title="This configuration has problems">
                <p>
                  The controller considers these settings invalid and is not sending them. They
                  are shown rather than silently corrected, because a value somebody typed
                  should not be quietly replaced.
                </p>
                <ul className="mt-2 list-inside list-disc space-y-1">
                  {monitoring.data.problems.map((problem) => (
                    <li key={problem}>{problem}</li>
                  ))}
                </ul>
              </Alert>
            ) : null}

            <ul className="divide-border-subtle divide-y">
              {monitoring.data.modules.map((module) => (
                <ModuleRow
                  key={module.module_id}
                  hostId={hostId}
                  module={module}
                  descriptor={catalogue.data?.modules.find(
                    (entry) => entry.id === module.module_id,
                  )}
                  canEdit={permissions.configureMonitoring}
                />
              ))}
            </ul>

            {permissions.configureMonitoring ? null : (
              <p className="text-text-faint text-xs">
                Only an administrator can change what a host collects. An operator who could
                enable collection could start gathering data about somebody&rsquo;s machine
                without one.
              </p>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

/**
 * Desired versus applied, in a sentence.
 *
 * `pending` is not framed as a problem: an agent has up to a heartbeat
 * interval to acknowledge, and an interface that showed a warning for three
 * seconds after every save would train operators to ignore the warning.
 */
function SyncBanner({ sync, host }: { sync: ConfigSync; host: Host }) {
  if (sync.status === 'none') {
    return (
      <p className="text-text-secondary text-sm leading-relaxed">
        Nothing has been configured for this host yet, so its agent is collecting nothing.
      </p>
    );
  }

  if (sync.status === 'rejected') {
    return (
      <Alert variant="critical" title="The agent refused this configuration">
        <p>
          It is running the previous revision
          {sync.reported_version === null
            ? ''
            : ` (version ${String(sync.reported_version)})`}
          , so the settings below are what the controller wants, not what the host is doing.
          {sync.rejected_at === null ? null : (
            <>
              {' '}
              Refused{' '}
              <time dateTime={machineDateTime(sync.rejected_at)}>
                {relativeTime(sync.rejected_at)}
              </time>
              .
            </>
          )}
        </p>
        {sync.rejection_problems.length > 0 ? (
          <ul className="mt-2 list-inside list-disc space-y-1">
            {sync.rejection_problems.map((problem) => (
              <li key={problem} className="font-mono text-xs">
                {problem}
              </li>
            ))}
          </ul>
        ) : null}
      </Alert>
    );
  }

  if (sync.status === 'up_to_date') {
    return (
      <p className="text-text-secondary flex flex-wrap items-center gap-2 text-sm">
        <Badge variant="ok">
          <Check aria-hidden="true" className="size-3" />
          Applied
        </Badge>
        <span>
          The agent confirmed version {String(sync.desired_version)}
          {sync.acknowledged_at === null ? null : (
            <>
              {' '}
              <time
                dateTime={machineDateTime(sync.acknowledged_at)}
                title={formatDateTime(sync.acknowledged_at)}
              >
                {relativeTime(sync.acknowledged_at)}
              </time>
            </>
          )}
          . What is below is what the host is doing.
        </span>
      </p>
    );
  }

  // `pending` and `stale`: saved, not confirmed. The distinction between them
  // is whether the agent has ever acknowledged anything, and the remedy
  // differs — pending usually resolves itself, stale usually means the agent
  // is not connected.
  const offline = host.connectivity !== 'connected';
  return (
    <Alert variant={offline ? 'warn' : 'info'} title="Saved, not yet confirmed">
      {offline ? (
        <>
          The agent is not connected, so it has not received version{' '}
          {String(sync.desired_version)}. It will apply it on its next connection &mdash; the
          controller never dials out to a host. Until then this host is collecting whatever it
          was last told.
        </>
      ) : (
        <>
          Version {String(sync.desired_version)} has been sent and the agent has not
          acknowledged it yet. This normally resolves within a heartbeat.
          {sync.reported_version === null
            ? ' The agent has not yet reported any version.'
            : ` It last confirmed version ${String(sync.reported_version)}.`}
        </>
      )}
    </Alert>
  );
}

/** Where a resolved value came from, in words an operator can act on. */
const SOURCE_LABELS: Record<SettingSource, string> = {
  default: 'not configured anywhere',
  global: 'from the global default',
  host: 'set on this host',
};

function ModuleRow({
  hostId,
  module,
  descriptor,
  canEdit,
}: {
  hostId: string;
  module: EffectiveModule;
  descriptor: ModuleDescriptor | undefined;
  canEdit: boolean;
}) {
  const queryClient = useQueryClient();
  const [lastSave, setLastSave] = useState<{ revisionCreated: boolean } | null>(null);

  const invalidate = async () => {
    // Both roots: the configuration changed, and so did the set of series that
    // will start or stop producing data.
    await queryClient.invalidateQueries({ queryKey: queryKeys.session.monitoring() });
    await queryClient.invalidateQueries({ queryKey: queryKeys.session.metrics() });
  };

  const save = useMutation({
    mutationFn: async (choice: ModuleChoice) => {
      if (choice === 'inherit') {
        // Drops the whole override row, schedule included — which is what
        // "inherit" means. Setting `enabled: null` would leave a schedule
        // override behind, so the host would inherit whether to collect but
        // not how often.
        return await resetHostOverride(hostId, module.module_id);
      }
      // Only `enabled` is sent. Omitting `schedule` leaves any schedule
      // override untouched; sending `null` would silently clear it every time
      // somebody flipped the switch.
      return await setHostOverride(hostId, module.module_id, {
        enabled: choice === 'on',
      });
    },
    onSuccess: async (result) => {
      setLastSave({ revisionCreated: result.revision_created });
      await invalidate();
    },
  });

  // No override row at all, or one that says nothing about `enabled`, both
  // mean the global default decides. `?? null` collapses the two, which is
  // right here and would be wrong anywhere that had to *write* the setting:
  // clearing the row and setting the field to null are different requests.
  const storedEnabled = module.host_override?.enabled ?? null;
  const choice: ModuleChoice =
    storedEnabled === null ? 'inherit' : storedEnabled ? 'on' : 'off';

  const interval = intervalOf(module);

  return (
    <li className="flex flex-col gap-2 py-3 first:pt-0 last:pb-0">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-text-primary text-sm font-medium">{module.name}</span>
            {module.enabled.value ? (
              <Badge variant="accent">Collecting</Badge>
            ) : (
              <Badge variant="neutral">Off</Badge>
            )}
            {descriptor?.requires_privileges === true ? (
              <Badge variant="warn">Needs privileges</Badge>
            ) : null}
          </div>
          <p className="text-text-muted mt-1 text-xs leading-relaxed">
            {descriptor?.description ?? 'No description available in this release.'}
          </p>
          <p className="text-text-faint mt-1 text-xs">
            {/* The provenance shown is the *enabled* decision's, not the
             * schedule's. Both are available, and they answer different
             * questions: an operator looking at this row wants to know why
             * this host is collecting, and "Every 10 s · not configured
             * anywhere" — which is what showing the schedule's source
             * produced — reads as a contradiction. The schedule's source is
             * added only when it differs, where it is information rather
             * than noise. */}
            {module.enabled.value ? (
              <>
                Every {interval} &middot; {SOURCE_LABELS[module.enabled.source]}
                {module.schedule.source === module.enabled.source ? null : (
                  <> &middot; interval {SOURCE_LABELS[module.schedule.source]}</>
                )}
              </>
            ) : (
              <>{SOURCE_LABELS[module.enabled.source]}</>
            )}
            {descriptor === undefined ? null : (
              <>
                {' '}
                &middot; no faster than every{' '}
                {describeSeconds(descriptor.min_interval_seconds)}
              </>
            )}
          </p>
        </div>

        {canEdit ? (
          <div className="flex shrink-0 items-center gap-2">
            {save.isPending ? <Spinner /> : null}
            <label className="sr-only" htmlFor={`module-${module.module_id}`}>
              {module.name} collection
            </label>
            <Select
              id={`module-${module.module_id}`}
              className="w-auto"
              value={choice}
              disabled={save.isPending}
              onChange={(event) => {
                setLastSave(null);
                save.mutate(event.target.value as ModuleChoice);
              }}
            >
              <option value="on">On</option>
              <option value="off">Off</option>
              <option value="inherit">
                Inherit ({module.global_default.enabled === true ? 'on' : 'off'})
              </option>
            </Select>
            {choice === 'inherit' ? null : (
              <Button
                variant="ghost"
                size="sm"
                disabled={save.isPending}
                title="Remove this host's override so the global default decides"
                onClick={() => {
                  setLastSave(null);
                  save.mutate('inherit');
                }}
              >
                <RotateCcw />
                <span className="sr-only">Reset {module.name} to the global default</span>
              </Button>
            )}
          </div>
        ) : null}
      </div>

      {save.isError ? <MutationError error={save.error} /> : null}

      {lastSave !== null && !lastSave.revisionCreated ? (
        <p className="text-text-faint flex items-start gap-1.5 text-xs">
          <TriangleAlert aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
          Saved, but nothing changed for the agent: the resolved configuration is identical to
          what it already has, so no new revision was sent.
        </p>
      ) : null}
    </li>
  );
}

/** The resolved interval as text, or a description of a calendar schedule. */
function intervalOf(module: EffectiveModule): string {
  const schedule = module.schedule.value;
  if (schedule.kind === 'interval' && typeof schedule.interval_seconds === 'number') {
    return describeSeconds(schedule.interval_seconds);
  }
  // A calendar schedule. The next runs are the useful summary, and the server
  // computes them — the client must not reimplement time-zone arithmetic.
  const next = module.schedule.next_runs?.[0];
  return next === undefined ? schedule.kind : `${schedule.kind}, next ${relativeTime(next)}`;
}
