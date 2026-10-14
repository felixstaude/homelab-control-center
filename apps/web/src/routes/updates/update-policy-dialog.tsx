import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useId, useState } from 'react';

import type { HostUpdateStatus, UpdatePolicyRequest } from '@/api/types';
import { cancelUpdate, setUpdatePolicy, startUpdate } from '@/api/updates';
import { MutationError } from '@/components/states/panel-states';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Field } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import { Modal } from '@/components/ui/modal';
import { Select } from '@/components/ui/select';
import { queryKeys } from '@/lib/query-keys';
import {
  OUTCOMES,
  POLICY_LABELS,
  STEP_LABELS,
  WEEKDAY_NAMES,
  canCancel,
  canStartUpdate,
  jobState,
} from '@/routes/updates/update-status';

/**
 * Managing one host's updates.
 *
 * Three things in one dialog, because an administrator opening it has one
 * question — "what should this host do about updates" — and splitting the
 * answer across three screens would make them navigate to find out.
 *
 * ## The confirmation names the step, not just the version
 *
 * "Install 0.2.0" and "install 0.2.0, a minor update, over 0.1.4" are
 * different sentences, and the second is the one somebody needs before
 * restarting a service on a machine they care about.
 *
 * ## Why the cancel button disappears rather than erroring
 *
 * From `installing` onwards the controller cannot stop the host: the binary is
 * already being replaced. The server refuses, and the interface stops offering
 * the control at the same point — a button that fails is worse than one that
 * is absent, and the explanation replaces it.
 */
export function UpdatePolicyDialog({
  host,
  channels,
  onClose,
}: {
  host: HostUpdateStatus;
  channels: string[];
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const policyId = useId();
  const channelId = useId();
  const pinId = useId();
  const zoneId = useId();
  const startId = useId();
  const endId = useId();

  const [policy, setPolicy] = useState<HostUpdateStatus['policy']>(host.policy);
  const [channel, setChannel] = useState(host.channel);
  const [pinnedVersion, setPinnedVersion] = useState(host.pinned_version ?? '');
  const [windowEnabled, setWindowEnabled] = useState(host.maintenance_window !== null);
  const [zone, setZone] = useState(
    host.maintenance_window?.timezone ??
      // The browser's own zone as the default, because an administrator
      // configuring "02:00" almost always means their own 02:00 — and
      // guessing UTC silently would schedule it hours away from what they
      // pictured.
      Intl.DateTimeFormat().resolvedOptions().timeZone,
  );
  const [weekdays, setWeekdays] = useState<number[]>(host.maintenance_window?.weekdays ?? []);
  const [start, setStart] = useState(host.maintenance_window?.start ?? '02:00');
  const [end, setEnd] = useState(host.maintenance_window?.end ?? '04:00');

  const invalidate = () =>
    void queryClient.invalidateQueries({ queryKey: queryKeys.session.updates() });

  const save = useMutation({
    mutationFn: () => {
      const body: UpdatePolicyRequest = {
        policy,
        channel,
        pinned_version: policy === 'pinned' ? pinnedVersion.trim() : null,
        maintenance_window: windowEnabled
          ? { timezone: zone, weekdays, start, end }
          : null,
      };
      return setUpdatePolicy(host.host_id, body);
    },
    onSuccess: () => {
      invalidate();
      onClose();
    },
  });

  const install = useMutation({
    mutationFn: () =>
      startUpdate(host.host_id, {
        allow_downgrade: host.plan.outcome === 'downgrade',
      }),
    onSuccess: invalidate,
  });

  const stop = useMutation({
    mutationFn: () => cancelUpdate(host.open_job?.job_id ?? ''),
    onSuccess: invalidate,
  });

  const outcome = OUTCOMES[host.plan.outcome];
  const job = host.open_job;

  return (
    <Modal
      open
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
      title={`Updates for ${host.host_name}`}
      description="What this host is running, and how it is allowed to change."
      width="lg"
      footer={
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>
            Close
          </Button>
          <Button onClick={() => { save.mutate(); }} disabled={save.isPending}>
            {save.isPending ? 'Saving…' : 'Save policy'}
          </Button>
        </div>
      }
    >
      <div className="flex flex-col gap-6">
        <section className="flex flex-col gap-2" aria-label="Current state">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={outcome.tone}>{outcome.label}</Badge>
            <span className="text-text-secondary text-sm">
              Installed <span className="font-mono">{host.installed_version ?? 'unknown'}</span>
              {host.plan.target_version ? (
                <>
                  {' · available '}
                  <span className="font-mono">{host.plan.target_version}</span>
                  {host.plan.step !== 'none' ? ` (${STEP_LABELS[host.plan.step]})` : null}
                </>
              ) : null}
            </span>
          </div>
          <p className="text-text-secondary text-sm">{outcome.explanation}</p>

          {host.plan.blockers.length > 0 ? (
            <ul className="text-text-secondary flex list-disc flex-col gap-1 pl-5 text-sm">
              {host.plan.blockers.map((blocker) => (
                <li key={blocker.code}>{blocker.detail}</li>
              ))}
            </ul>
          ) : null}
          {host.plan.detail ? (
            <p className="text-text-secondary text-sm">{host.plan.detail}</p>
          ) : null}
        </section>

        {job ? (
          <section className="flex flex-col gap-2" aria-label="Update in progress">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={jobState(job.state).tone}>{jobState(job.state).label}</Badge>
              <span className="text-text-secondary text-sm">
                installing <span className="font-mono">{job.to_version}</span>
              </span>
            </div>
            {canCancel(job) ? (
              <div>
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => { stop.mutate(); }}
                  disabled={stop.isPending}
                >
                  {stop.isPending ? 'Cancelling…' : 'Cancel update'}
                </Button>
              </div>
            ) : (
              <p className="text-text-tertiary text-sm">
                This update has reached the point where the controller cannot stop it: the
                binary is being replaced on the host. If the new version does not come up,
                the agent restores the previous one by itself.
              </p>
            )}
            {stop.isError ? <MutationError error={stop.error} /> : null}
          </section>
        ) : canStartUpdate(host.plan) ? (
          <section className="flex flex-col gap-2" aria-label="Install now">
            <Button onClick={() => { install.mutate(); }} disabled={install.isPending}>
              {install.isPending
                ? 'Starting…'
                : `Install ${host.plan.target_version ?? ''} now`}
            </Button>
            <p className="text-text-tertiary text-sm">
              {host.plan.outcome === 'downgrade'
                ? 'This is a downgrade. It is recorded separately in the audit trail.'
                : `A ${STEP_LABELS[host.plan.step]} update. The host picks it up on its next heartbeat and restarts its agent.`}
            </p>
            {install.isError ? <MutationError error={install.error} /> : null}
          </section>
        ) : null}

        <section className="flex flex-col gap-4" aria-label="Update policy">
          <Field
            id={policyId}
            label="Policy"
            hint="Automatic updates never cross a compatibility boundary, whatever the policy says. There is deliberately no option to install major versions automatically."
          >
            {(controlProps) => (
              <Select
                {...controlProps}
                value={policy}
                onChange={(event) => { setPolicy(event.target.value as HostUpdateStatus['policy']); }
                }
              >
                {(Object.keys(POLICY_LABELS) as HostUpdateStatus['policy'][]).map((value) => (
                  <option key={value} value={value}>
                    {POLICY_LABELS[value]}
                  </option>
                ))}
              </Select>
            )}
          </Field>

          <Field
            id={channelId}
            label="Release channel"
            hint="The most instability this host accepts. A host on beta still receives newer stable releases."
          >
            {(controlProps) => (
              <Select
                {...controlProps}
                value={channel}
                onChange={(event) => { setChannel(event.target.value); }}
              >
                {channels.map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </Select>
            )}
          </Field>

          {policy === 'pinned' ? (
            <Field
              id={pinId}
              label="Pinned version"
              hint="An exact version, e.g. 0.1.0. The host is held here until the pin changes."
            >
              {(controlProps) => (
                <Input
                  {...controlProps}
                  value={pinnedVersion}
                  onChange={(event) => { setPinnedVersion(event.target.value); }}
                  placeholder="0.1.0"
                />
              )}
            </Field>
          ) : null}
        </section>

        <section className="flex flex-col gap-4" aria-label="Maintenance window">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={windowEnabled}
              onChange={(event) => { setWindowEnabled(event.target.checked); }}
              className="accent-accent size-4"
            />
            <span className="text-text-primary">Only update inside a maintenance window</span>
          </label>

          {windowEnabled ? (
            <>
              <Field
                id={zoneId}
                label="Time zone"
                hint="An IANA name. The window is evaluated in this zone, not the reader's — so the times mean what you typed wherever somebody looks at them."
              >
                {(controlProps) => (
                  <Input
                    {...controlProps}
                    value={zone}
                    onChange={(event) => { setZone(event.target.value); }}
                    placeholder="Europe/Berlin"
                  />
                )}
              </Field>

              <div className="grid gap-4 sm:grid-cols-2">
                <Field id={startId} label="From">
                  {(controlProps) => (
                    <Input
                      {...controlProps}
                      type="time"
                      value={start}
                      onChange={(event) => { setStart(event.target.value); }}
                    />
                  )}
                </Field>
                <Field
                  id={endId}
                  label="To"
                  hint="A window may cross midnight — 22:00 to 02:00 is Sunday night, and is matched on the day it started."
                >
                  {(controlProps) => (
                    <Input
                      {...controlProps}
                      type="time"
                      value={end}
                      onChange={(event) => { setEnd(event.target.value); }}
                    />
                  )}
                </Field>
              </div>

              <fieldset className="flex flex-col gap-2">
                <legend className="text-text-secondary text-sm">
                  Days (none selected means every day)
                </legend>
                <div className="flex flex-wrap gap-2">
                  {WEEKDAY_NAMES.map((name, index) => (
                    <label key={name} className="flex items-center gap-1.5 text-sm">
                      <input
                        type="checkbox"
                        checked={weekdays.includes(index)}
                        onChange={(event) => {
                          setWeekdays((current) =>
                            event.target.checked
                              ? [...current, index].sort((a, b) => a - b)
                              : current.filter((day) => day !== index),
                          );
                        }}
                        className="accent-accent size-4"
                      />
                      <span className="text-text-secondary">{name.slice(0, 3)}</span>
                    </label>
                  ))}
                </div>
              </fieldset>
            </>
          ) : null}
        </section>

        {save.isError ? <MutationError error={save.error} /> : null}
      </div>
    </Modal>
  );
}
