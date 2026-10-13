import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  ArrowLeft,
  KeyRound,
  Power,
  PowerOff,
  ShieldOff,
  Trash2,
} from 'lucide-react';
import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';

import { isApiError } from '@/api/errors';
import {
  disableHost,
  enableHost,
  fetchHost,
  revokeCredential,
  revokeEnrollment,
} from '@/api/hosts';
import type { Host } from '@/api/types';
import { usePermissions } from '@/auth/use-auth';
import { PageHeader } from '@/components/page-header';
import { ErrorPanel, MutationError } from '@/components/states/panel-states';
import { Alert } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Modal } from '@/components/ui/modal';
import { Skeleton } from '@/components/ui/skeleton';
import { Spinner } from '@/components/ui/spinner';
import { formatDateTime, machineDateTime, relativeTime } from '@/lib/datetime';
import { queryKeys } from '@/lib/query-keys';
import { EnrollmentDialog } from '@/routes/hosts/enrollment-dialog';
import { HostMetricsCard } from '@/routes/hosts/host-metrics';
import { HostMonitoringCard } from '@/routes/hosts/host-monitoring';
import {
  ConnectivityBadge,
  DisabledBadge,
  LifecycleBadge,
  PendingEnrollmentBadge,
} from '@/routes/hosts/host-badges';
import {
  CONNECTIVITY_DESCRIPTIONS,
  LIFECYCLE_DESCRIPTIONS,
} from '@/routes/hosts/host-status';

/**
 * One host.
 *
 * ## What it reports, and what it refuses to invent
 *
 * Phase 3 gave this page real measurements, and the discipline it had while it
 * had none is the part that must survive: a value appears when it has been
 * measured recently and is absent otherwise. Nothing is zero-filled, nothing
 * is carried forward, and a gap in a chart is drawn as a gap. See
 * `host-metrics.tsx`, which is built around those refusals, and
 * `host-monitoring.tsx`, which separates what the controller wants from what
 * the agent has confirmed it is doing.
 *
 * The rest is what has always been here: the three state axes, the connection
 * timestamps, and whatever the agent has said about itself. The self-reported
 * fields are labelled as such, because they come from a machine whose security
 * the controller does not own.
 *
 * ## Every destructive action is confirmed with its consequence
 *
 * Disabling, revoking an enrollment credential and revoking the agent
 * credential all take effect on a running agent within milliseconds, so each
 * is confirmed with a sentence saying what will happen — and the two
 * revocations are confirmed separately, because they are different actions
 * with different remedies and a single "revoke" control would force the
 * operator to pick a consequence they had not been told about.
 */

/** How often the detail refetches while the tab is visible. */
const POLL_INTERVAL_MS = 5000;

type PendingAction = 'disable' | 'revoke-enrollment' | 'revoke-credential' | null;

export function HostDetailPage() {
  const { hostId = '' } = useParams<{ hostId: string }>();
  const permissions = usePermissions();
  const queryClient = useQueryClient();

  const [enrolling, setEnrolling] = useState(false);
  const [pending, setPending] = useState<PendingAction>(null);
  /** The outcome of the last enrollment revocation, reported honestly. */
  const [lastRevocation, setLastRevocation] = useState<number | null>(null);

  const host = useQuery({
    queryKey: queryKeys.session.host(hostId),
    queryFn: ({ signal }) => fetchHost(hostId, signal),
    // Faster than the list: somebody on this page is usually watching for a
    // state they have just caused, and five seconds of lag on a single row is
    // cheap where ten on a whole page is not.
    refetchInterval: POLL_INTERVAL_MS,
    enabled: hostId !== '',
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.session.hosts() });
  };

  const setEnabled = useMutation({
    mutationFn: (next: boolean) => (next ? enableHost(hostId) : disableHost(hostId)),
    onSuccess: invalidate,
  });

  if (host.isPending) {
    return (
      <>
        <PageHeader title="Host" titleHidden description="Loading…" />
        <Card>
          <CardContent className="flex flex-col gap-3">
            <Skeleton className="h-6 w-48" />
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-2/3" />
          </CardContent>
        </Card>
      </>
    );
  }

  if (host.isError) {
    return (
      <>
        <PageHeader title="Host" titleHidden />
        <Card>
          <CardContent className="px-0">
            <ErrorPanel
              error={host.error}
              title={
                isApiError(host.error) && host.error.status === 404
                  ? 'That host does not exist'
                  : 'Could not load the host'
              }
              onRetry={() => {
                void host.refetch();
              }}
            />
          </CardContent>
        </Card>
        <BackLink />
      </>
    );
  }

  const data = host.data;

  return (
    <>
      <PageHeader
        title={data.name}
        titleHidden
        description={data.description ?? 'Linux host reporting through the agent.'}
      />

      <BackLink />

      <div className="flex flex-col gap-6">
        {/* --- State --- */}
        <Card>
          <CardHeader>
            <div className="min-w-0">
              <CardTitle>{data.name}</CardTitle>
              <CardDescription>
                Registered {formatDateTime(data.created_at)}
                {data.environment === null ? null : <> &middot; {data.environment}</>}
              </CardDescription>
            </div>
            <div className="flex flex-wrap items-center gap-1.5">
              <ConnectivityBadge host={data} />
              <DisabledBadge isEnabled={data.is_enabled} />
              <LifecycleBadge lifecycle={data.lifecycle} />
              <PendingEnrollmentBadge pending={data.has_pending_enrollment} />
            </div>
          </CardHeader>

          <CardContent className="flex flex-col gap-3">
            <p className="text-text-secondary text-sm leading-relaxed">
              {CONNECTIVITY_DESCRIPTIONS[data.connectivity]}{' '}
              {LIFECYCLE_DESCRIPTIONS[data.lifecycle]}
            </p>
            {!data.is_enabled ? (
              <Alert variant="warn" title="This host is disabled">
                Its agent is refused at the door. The enrolment and the history are kept, so
                enabling it again restores the connection without re-enrolling &mdash; the
                agent reconnects on its own within a minute.
              </Alert>
            ) : null}
            {data.lifecycle === 'revoked' ? (
              <Alert variant="critical" title="This host&rsquo;s credential was revoked">
                No agent can authenticate as this host until it is enrolled again. Issue a new
                enrolment credential and run <code className="font-mono">hcc-agent enroll</code>{' '}
                on the host.
              </Alert>
            ) : null}
          </CardContent>
        </Card>

        {/* --- Connection facts --- */}
        <Card>
          <CardHeader>
            <div className="min-w-0">
              <CardTitle>Connection</CardTitle>
              <CardDescription>
                What the controller has recorded. A value of &ldquo;never&rdquo; means exactly
                that &mdash; nothing here is a placeholder or a zero.
              </CardDescription>
            </div>
          </CardHeader>
          <CardContent>
            <dl className="grid gap-x-6 gap-y-4 sm:grid-cols-2">
              <TimestampFact
                label="Last heartbeat"
                value={data.last_heartbeat_at}
                relative
                absent="Never — no agent has reported"
              />
              <TimestampFact
                label="Last connected"
                value={data.last_connected_at}
                absent="Never"
              />
              <TimestampFact
                label="Last disconnected"
                value={data.last_disconnected_at}
                absent="Never"
              />
              <TimestampFact
                label="Credential issued"
                value={data.credential_issued_at}
                absent={
                  data.lifecycle === 'revoked' ? 'Revoked — no live credential' : 'Not enrolled'
                }
              />
            </dl>
          </CardContent>
        </Card>

        {/* --- What the agent says about itself --- */}
        <Card>
          <CardHeader>
            <div className="min-w-0">
              <CardTitle>Agent</CardTitle>
              <CardDescription>
                Reported by the agent itself, and shown so you can see what is deployed where.
                The controller never makes a decision from these values: they come from a
                machine whose security it does not own.
              </CardDescription>
            </div>
          </CardHeader>
          <CardContent>
            <dl className="grid gap-x-6 gap-y-4 sm:grid-cols-2">
              <Fact label="Version" value={data.agent_version} mono />
              <Fact label="Platform" value={data.platform} mono />
              <Fact
                label="Protocol"
                value={data.protocol_version === null ? null : `v${String(data.protocol_version)}`}
                mono
              />
              <TimestampFact
                label="Agent started"
                value={data.agent_started_at}
                absent="Not reported"
              />
            </dl>
          </CardContent>
        </Card>

        {/* --- Real measurements, and the absences that are real too --- */}
        <HostMetricsCard host={data} />

        {/* --- What this host is asked to collect, and what it confirmed --- */}
        <HostMonitoringCard host={data} />

        {/* --- Administrative actions --- */}
        {permissions.administer ? (
          <Card>
            <CardHeader>
              <div className="min-w-0">
                <CardTitle>Manage</CardTitle>
                <CardDescription>
                  Each of these takes effect on the running agent within milliseconds, not at
                  its next reconnection.
                </CardDescription>
              </div>
            </CardHeader>
            <CardContent className="flex flex-col gap-4">
              {lastRevocation !== null ? (
                <div role="status" className="text-text-secondary text-sm">
                  {lastRevocation === 0
                    ? 'There was no pending enrolment credential, so nothing was revoked.'
                    : lastRevocation === 1
                      ? 'Revoked one pending enrolment credential.'
                      : `Revoked ${String(lastRevocation)} pending enrolment credentials.`}
                </div>
              ) : null}

              {setEnabled.isError ? <MutationError error={setEnabled.error} /> : null}

              <div className="flex flex-wrap gap-2">
                <Button
                  variant="primary"
                  size="sm"
                  onClick={() => {
                    setEnrolling(true);
                  }}
                >
                  <KeyRound />
                  {data.lifecycle === 'enrolled' ? 'Re-enrol' : 'Issue enrolment credential'}
                </Button>

                {data.is_enabled ? (
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => {
                      setPending('disable');
                    }}
                  >
                    <PowerOff />
                    Disable
                  </Button>
                ) : (
                  <Button
                    variant="secondary"
                    size="sm"
                    disabled={setEnabled.isPending}
                    onClick={() => {
                      // Enabling needs no confirmation: it restores the state
                      // the operator originally asked for and breaks nothing.
                      setEnabled.mutate(true);
                    }}
                  >
                    {setEnabled.isPending ? <Spinner /> : <Power />}
                    Enable
                  </Button>
                )}

                {data.has_pending_enrollment ? (
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => {
                      setLastRevocation(null);
                      setPending('revoke-enrollment');
                    }}
                  >
                    <Trash2 />
                    Revoke pending credential
                  </Button>
                ) : null}

                {data.credential_issued_at === null ? null : (
                  <Button
                    variant="destructive"
                    size="sm"
                    onClick={() => {
                      setPending('revoke-credential');
                    }}
                  >
                    <ShieldOff />
                    Revoke agent credential
                  </Button>
                )}
              </div>
            </CardContent>
          </Card>
        ) : null}
      </div>

      {permissions.administer ? (
        <>
          <EnrollmentDialog
            // Remounted on open and on close, so a credential cannot outlive
            // the dialog that showed it. See the component's note.
            key={enrolling ? data.host_id : 'closed'}
            host={enrolling ? data : null}
            onOpenChange={(open) => {
              if (!open) setEnrolling(false);
            }}
          />
          <ActionConfirmation
            action={pending}
            host={data}
            onClose={() => {
              setPending(null);
            }}
            onEnrollmentRevoked={setLastRevocation}
          />
        </>
      ) : null}
    </>
  );
}

function BackLink() {
  return (
    <Link
      to="/hosts"
      className="text-text-muted hover:text-text-primary mb-4 inline-flex items-center gap-1.5
        text-sm"
    >
      <ArrowLeft aria-hidden="true" className="size-4" />
      All hosts
    </Link>
  );
}

/** One label-and-value pair. `null` renders a stated absence, never a zero. */
function Fact({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string | null;
  mono?: boolean;
}) {
  return (
    <div className="min-w-0">
      <dt className="text-text-faint text-xs font-medium">{label}</dt>
      <dd
        className={
          value === null
            ? 'text-text-faint mt-0.5 text-sm'
            : mono
              ? 'text-text-primary mt-0.5 font-mono text-sm break-all'
              : 'text-text-primary mt-0.5 text-sm'
        }
      >
        {value ?? 'Not reported'}
      </dd>
    </div>
  );
}

/**
 * A timestamp fact.
 *
 * `absent` is a sentence rather than a dash, because the reason a timestamp is
 * missing is information: "never, no agent has reported" and "revoked, no live
 * credential" are different facts, and both are more useful than the em dash
 * `lib/datetime.ts` uses where there is nothing more to say.
 */
function TimestampFact({
  label,
  value,
  absent,
  relative = false,
}: {
  label: string;
  value: string | null;
  absent: string;
  relative?: boolean;
}) {
  return (
    <div className="min-w-0">
      <dt className="text-text-faint text-xs font-medium">{label}</dt>
      <dd className="mt-0.5 text-sm">
        {value === null ? (
          <span className="text-text-faint">{absent}</span>
        ) : (
          <time
            dateTime={machineDateTime(value)}
            title={formatDateTime(value)}
            className="text-text-primary"
          >
            {relative ? relativeTime(value) : formatDateTime(value)}
          </time>
        )}
      </dd>
    </div>
  );
}

/**
 * The confirmation for each destructive action.
 *
 * One component rather than three dialogs, so the shape — a sentence saying
 * what will happen, then a button that does it — cannot differ between them.
 * The *sentences* differ, deliberately: the three actions have different
 * consequences and different remedies, and a shared "are you sure?" would be
 * the one thing an operator could not act on.
 */
function ActionConfirmation({
  action,
  host,
  onClose,
  onEnrollmentRevoked,
}: {
  action: PendingAction;
  host: Host;
  onClose: () => void;
  onEnrollmentRevoked: (count: number) => void;
}) {
  const queryClient = useQueryClient();

  const run = useMutation({
    mutationFn: async () => {
      switch (action) {
        case 'disable':
          return await disableHost(host.host_id);
        case 'revoke-enrollment': {
          const result = await revokeEnrollment(host.host_id);
          onEnrollmentRevoked(result.revoked);
          return result;
        }
        case 'revoke-credential':
          return await revokeCredential(host.host_id);
        case null:
          throw new Error('no action');
      }
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.session.hosts() });
      onClose();
    },
  });

  if (action === null) return null;

  const copy = {
    disable: {
      title: `Disable ${host.name}?`,
      body: (
        <>
          <p>
            The agent is disconnected immediately and refused on every attempt until this host
            is enabled again.
          </p>
          <p className="mt-2">
            Its enrolment and history are kept, so enabling it later restores the connection
            without re-enrolling. Use this for maintenance; use <em>revoke</em> if you no
            longer trust the credential.
          </p>
        </>
      ),
      confirm: 'Disable host',
      variant: 'secondary' as const,
    },
    'revoke-enrollment': {
      title: 'Revoke the pending enrolment credential?',
      body: (
        <p>
          The credential issued for this host and not yet used stops working at once. No agent
          is affected &mdash; this only kills a credential nobody has spent. Issue a new one
          when you are ready.
        </p>
      ),
      confirm: 'Revoke credential',
      variant: 'secondary' as const,
    },
    'revoke-credential': {
      title: `Revoke ${host.name}'s agent credential?`,
      body: (
        <>
          <p>
            The agent is disconnected within milliseconds and cannot authenticate again. It
            will exit rather than retry, so the host stops reporting until you re-enrol it.
          </p>
          <p className="mt-2">
            Do this when you believe the credential has been exposed, or when
            decommissioning the host. This cannot be undone &mdash; recovery means issuing a
            new enrolment credential and running <code className="font-mono">hcc-agent
            enroll</code> on the host.
          </p>
        </>
      ),
      confirm: 'Revoke credential',
      variant: 'destructive' as const,
    },
  }[action];

  return (
    <Modal
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      title={copy.title}
      footer={
        <>
          <Button variant="secondary" size="sm" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant={copy.variant}
            size="sm"
            disabled={run.isPending}
            onClick={() => {
              run.mutate();
            }}
          >
            {run.isPending ? <Spinner /> : null}
            {copy.confirm}
          </Button>
        </>
      }
    >
      <div className="text-text-secondary flex flex-col gap-3 text-sm leading-relaxed">
        {copy.body}
        {run.isError ? <MutationError error={run.error} /> : null}
      </div>
    </Modal>
  );
}
