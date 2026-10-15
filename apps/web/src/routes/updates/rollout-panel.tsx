import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Pause, Play, X } from 'lucide-react';
import { useState } from 'react';

import { cancelRollout, fetchRollouts, pauseRollout, resumeRollout } from '@/api/updates';
import type { Rollout } from '@/api/types';
import { usePermissions } from '@/auth/use-auth';
import { EmptyPanel, ErrorPanel, LoadingRows } from '@/components/states/panel-states';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { relativeTime } from '@/lib/datetime';
import { queryKeys } from '@/lib/query-keys';
import { cn } from '@/lib/utils';
import { memberPresentation } from '@/routes/updates/update-status';

/**
 * Staged rollouts, and the one distinction the whole panel exists to show.
 *
 * ## Succeeded is not healthy
 *
 * A member can be `succeeded` and not `healthy`, and that pair is why the
 * backend has two fields rather than one. The job concludes when the agent
 * reconnects reporting the target version; the rollout waits to see whether
 * the host is still there, still acknowledging its configuration and still
 * delivering telemetry. So an operator watching a canary sees
 * "installed, checking" for a couple of minutes, and that is the product
 * working rather than stalling.
 *
 * Rendering those as one state would make the wait look like a hang, and the
 * first thing somebody would do about a hang is press resume — which is
 * exactly the wrong thing.
 *
 * ## Skipped is not failed
 *
 * A host offline past its deadline, or already on the target version, is
 * skipped. It is counted separately and coloured neutrally, because a
 * powered-off NAS says nothing about the release and a panel that showed it
 * in red would train somebody to ignore red.
 *
 * ## Resume offers the override explicitly
 *
 * A rollout the failure gate stopped pauses again on the next pass unless
 * the failures are overridden. If this panel offered a bare "resume", an
 * administrator would press it, watch nothing happen, and reasonably
 * conclude the feature is broken. So there are two buttons and the second
 * says what it does.
 */

/** How often this refetches while the tab is visible. */
const POLL_INTERVAL_MS = 10_000;

export function RolloutPanel() {
  // Administrator only, mirroring what the server enforces: create, pause,
  // resume and cancel are all on the administrator router.
  const { administer } = usePermissions();
  const rollouts = useQuery({
    queryKey: queryKeys.session.rollouts(),
    queryFn: ({ signal }) => fetchRollouts(signal),
    refetchInterval: POLL_INTERVAL_MS,
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle>Rollouts</CardTitle>
        <CardDescription>
          A canary first, then batches. The next batch starts only once the hosts before
          it have installed <em>and</em> stayed healthy — so a bad release stops after one
          machine rather than after forty.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {rollouts.isPending ? (
          <LoadingRows rows={2} />
        ) : rollouts.isError ? (
          <ErrorPanel
            error={rollouts.error}
            title="Could not load rollouts"
            onRetry={() => {
              void rollouts.refetch();
            }}
          />
        ) : rollouts.data.length === 0 ? (
          <EmptyPanel title="No rollouts">
            A rollout takes one version to a set of hosts in stages. Nothing has created
            one yet.
          </EmptyPanel>
        ) : (
          <div className="space-y-4">
            {rollouts.data.map((rollout) => (
              <RolloutRow
                key={rollout.rollout_id}
                rollout={rollout}
                canManage={administer}
              />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/** The badge for a rollout's own state. */
function stateBadge(state: Rollout['state']) {
  switch (state) {
    case 'completed':
      return { variant: 'ok' as const, label: 'completed' };
    case 'cancelled':
      return { variant: 'neutral' as const, label: 'cancelled' };
    case 'paused':
      return { variant: 'warn' as const, label: 'paused' };
    case 'canary':
      return { variant: 'info' as const, label: 'canary' };
    case 'running':
      return { variant: 'info' as const, label: 'running' };
    default:
      return { variant: 'neutral' as const, label: 'pending' };
  }
}

function RolloutRow({ rollout, canManage }: { rollout: Rollout; canManage: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const queryClient = useQueryClient();
  const badge = stateBadge(rollout.state);

  const invalidate = () => {
    // The root, not this key: a rollout control changes the rollout, the
    // host statuses and the history, which a mutation cannot enumerate.
    void queryClient.invalidateQueries({ queryKey: queryKeys.session.updates() });
  };

  const pause = useMutation({
    mutationFn: () => pauseRollout(rollout.rollout_id, { reason: 'paused from the interface' }),
    onSuccess: invalidate,
  });
  const resume = useMutation({
    mutationFn: (override: boolean) =>
      resumeRollout(rollout.rollout_id, { override_failures: override }),
    onSuccess: invalidate,
  });
  const abandon = useMutation({
    mutationFn: () =>
      cancelRollout(rollout.rollout_id, { reason: 'cancelled from the interface' }),
    onSuccess: invalidate,
  });

  const done = rollout.succeeded + rollout.failed + rollout.skipped;
  const active = rollout.state !== 'completed' && rollout.state !== 'cancelled';
  // A rollout the failure gate stopped. The override is offered only here,
  // because offering it on an administrator's own pause would invite
  // somebody to set a flag that means nothing.
  const stoppedByFailures = rollout.state === 'paused' && rollout.failed > rollout.failure_tolerance;

  return (
    <div className="border-border-subtle bg-surface-raised rounded-xl border p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h3 className="text-text-primary truncate text-sm font-semibold">{rollout.name}</h3>
            <Badge variant={badge.variant}>{badge.label}</Badge>
            {rollout.failures_overridden ? (
              // Shown for the life of the rollout, not only while it runs:
              // "this finished because somebody waved it through" is a fact
              // the summary has to keep.
              <Badge variant="warn">failures overridden</Badge>
            ) : null}
          </div>
          <p className="text-text-faint mt-0.5 text-xs">
            to {rollout.to_version} · {done} of {rollout.total} done
            {rollout.current_batch === null
              ? ''
              : ` · batch ${String(rollout.current_batch)}`}{' '}
            · created {relativeTime(rollout.created_at)}
          </p>
        </div>
        {canManage && active ? (
          <div className="flex shrink-0 flex-wrap gap-2">
            {rollout.state === 'paused' ? (
              <>
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() => {
                    resume.mutate(false);
                  }}
                  disabled={resume.isPending}
                >
                  <Play aria-hidden className="size-3.5" />
                  Resume
                </Button>
                {stoppedByFailures ? (
                  <Button
                    size="sm"
                    variant="destructive"
                    onClick={() => {
                      resume.mutate(true);
                    }}
                    disabled={resume.isPending}
                  >
                    Resume despite {rollout.failed} failure
                    {rollout.failed === 1 ? '' : 's'}
                  </Button>
                ) : null}
              </>
            ) : (
              <Button
                size="sm"
                variant="secondary"
                onClick={() => {
                  pause.mutate();
                }}
                disabled={pause.isPending}
              >
                <Pause aria-hidden className="size-3.5" />
                Pause
              </Button>
            )}
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                abandon.mutate();
              }}
              disabled={abandon.isPending}
            >
              <X aria-hidden className="size-3.5" />
              Cancel
            </Button>
          </div>
        ) : null}
      </div>

      {rollout.paused_reason === null ? null : (
        <p className="text-status-warn mt-2 text-xs">{rollout.paused_reason}</p>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
        <Count label="healthy" value={rollout.members.filter((m) => m.healthy).length} tone="ok" />
        <Count label="installed" value={rollout.succeeded} tone="info" />
        <Count label="updating" value={rollout.running} tone="info" />
        <Count label="waiting" value={rollout.pending} tone="muted" />
        {/* Separate from failed, and deliberately not coloured: a host that
            was offline or already on the target version is not a bad
            release. */}
        <Count label="skipped" value={rollout.skipped} tone="muted" />
        <Count label="failed" value={rollout.failed} tone="critical" />
      </div>

      <button
        type="button"
        className="text-text-faint hover:text-text-secondary mt-3 text-xs underline-offset-2 hover:underline"
        onClick={() => {
          setExpanded((open) => !open);
        }}
        aria-expanded={expanded}
      >
        {expanded ? 'Hide hosts' : `Show ${String(rollout.members.length)} hosts`}
      </button>

      {expanded ? (
        <ul className="mt-2 space-y-1">
          {rollout.members.map((member) => {
            const presentation = memberPresentation(member);
            return (
              <li
                key={member.host_id}
                className="border-border-subtle flex items-baseline justify-between gap-3 border-b pb-1 text-xs last:border-0"
              >
                <span className="text-text-secondary min-w-0 truncate">
                  <span className="text-text-faint mr-2">
                    {member.batch === 0 ? 'canary' : `batch ${String(member.batch)}`}
                  </span>
                  <code className="font-mono">{member.host_id.slice(0, 8)}</code>
                  {member.detail === null ? null : (
                    <span className="text-text-faint ml-2">{member.detail}</span>
                  )}
                </span>
                <Badge variant={presentation.variant}>{presentation.label}</Badge>
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}

function Count({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone: 'ok' | 'info' | 'critical' | 'muted';
}) {
  const colour =
    tone === 'ok'
      ? 'text-status-ok'
      : tone === 'info'
        ? 'text-status-info'
        : tone === 'critical'
          ? 'text-status-critical'
          : 'text-text-faint';
  return (
    <span className="text-text-faint">
      <span className={cn('font-medium tabular-nums', value > 0 ? colour : 'text-text-faint')}>
        {value}
      </span>{' '}
      {label}
    </span>
  );
}
