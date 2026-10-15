import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useId, useMemo, useState } from 'react';

import { createRollout } from '@/api/updates';
import type { HostUpdateStatus } from '@/api/types';
import { Alert } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Field } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import { Modal } from '@/components/ui/modal';
import { Select } from '@/components/ui/select';
import { queryKeys } from '@/lib/query-keys';
import { OUTCOMES, canStartUpdate } from '@/routes/updates/update-status';
import { isApiError } from '@/api/errors';

/**
 * Creating a staged rollout.
 *
 * ## It offers the hosts that have something to install
 *
 * A rollout over a host already running the target version is not wrong — the
 * engine skips it, deliberately, so re-running over a partly-updated fleet
 * works — but offering those hosts *first* would make the common case look
 * like a mistake. So the list is ordered with the ones that have an update
 * available at the top, and every host is selectable.
 *
 * ## The defaults are the cautious ones
 *
 * One canary, batches of five, **zero failures tolerated**, a two-minute
 * health check. An administrator who wants to go faster has to say so, which
 * is the right way round for something that replaces software on every
 * machine they own.
 *
 * ## It says what will happen, before it happens
 *
 * The summary line under the form is built from the current values rather
 * than described in prose, because "1 canary, then 2 batches of 5, stopping
 * on the first failure" is the sentence somebody needs to check — and a
 * static description would be right until somebody changed a field.
 */
export function CreateRolloutDialog({
  hosts,
  defaultVersion,
  onClose,
}: {
  hosts: HostUpdateStatus[];
  /** The version most hosts would move to, offered as the default. */
  defaultVersion: string | null;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const nameId = useId();
  const versionId = useId();
  const canaryId = useId();
  const batchId = useId();
  const toleranceId = useId();
  const healthId = useId();

  const [name, setName] = useState(
    defaultVersion === null ? 'Rollout' : `Roll out ${defaultVersion}`,
  );
  const [version, setVersion] = useState(defaultVersion ?? '');
  const [selected, setSelected] = useState<ReadonlySet<string>>(() => new Set());
  const [canaryHost, setCanaryHost] = useState('');
  const [batchSize, setBatchSize] = useState(5);
  const [tolerance, setTolerance] = useState(0);
  const [healthSeconds, setHealthSeconds] = useState(120);

  // Hosts with an update available first, then the rest. Both selectable:
  // the engine skips a host already on the target, which is what makes
  // re-running a rollout over a partly-updated fleet work.
  const ordered = useMemo(
    () =>
      [...hosts].sort((left, right) => {
        const leftReady = canStartUpdate(left.plan) ? 0 : 1;
        const rightReady = canStartUpdate(right.plan) ? 0 : 1;
        if (leftReady !== rightReady) return leftReady - rightReady;
        return left.host_name.localeCompare(right.host_name);
      }),
    [hosts],
  );

  const create = useMutation({
    mutationFn: () =>
      createRollout({
        name: name.trim(),
        to_version: version.trim(),
        host_ids: [...selected],
        canary_host_ids: canaryHost === '' ? [] : [canaryHost],
        canary_count: 1,
        batch_size: batchSize,
        failure_tolerance: tolerance,
        health_check_seconds: healthSeconds,
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.session.updates() });
      onClose();
    },
  });

  const count = selected.size;
  const afterCanary = Math.max(0, count - 1);
  const batches = batchSize > 0 ? Math.ceil(afterCanary / batchSize) : 0;
  const ready = count > 0 && version.trim() !== '' && name.trim() !== '';

  function toggle(hostId: string) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(hostId)) {
        next.delete(hostId);
        // A canary that is no longer a member cannot be the canary. The
        // server refuses this too; doing it here means the refusal is never
        // reached by an ordinary sequence of clicks.
        if (canaryHost === hostId) setCanaryHost('');
      } else {
        next.add(hostId);
      }
      return next;
    });
  }

  return (
    <Modal
      open
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
      title="New rollout"
      description="A canary first, then batches. Creating it starts nothing — the controller picks it up on its next pass."
      width="lg"
      footer={
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button
            onClick={() => {
              create.mutate();
            }}
            disabled={!ready || create.isPending}
          >
            {create.isPending ? 'Creating…' : 'Create rollout'}
          </Button>
        </div>
      }
    >
      <div className="flex flex-col gap-5">
        {create.isError ? (
          <Alert variant="critical" title="The rollout was not created">
            {isApiError(create.error) ? create.error.message : 'Something went wrong.'}
          </Alert>
        ) : null}

        {/* The two required fields validate live rather than on submit, which
            is the opposite of `settings/account.tsx` and for a reason: that
            form's button is pressable and answers with what is missing, so it
            has a submit event to gate on. This one's button is disabled until
            the form is valid, so a message gated on submit would never appear
            and somebody would be left looking at a dead button. */}
        <div className="grid gap-4 sm:grid-cols-2">
          <Field
            id={nameId}
            label="Name"
            error={name.trim() === '' ? 'Give the rollout a name.' : null}
          >
            {(controlProps) => (
              <Input
                {...controlProps}
                value={name}
                onChange={(event) => {
                  setName(event.target.value);
                }}
              />
            )}
          </Field>
          <Field
            id={versionId}
            label="Version"
            hint="The release every host moves to."
            error={version.trim() === '' ? 'Enter the version to install.' : null}
          >
            {(controlProps) => (
              <Input
                {...controlProps}
                value={version}
                placeholder="0.2.0"
                onChange={(event) => {
                  setVersion(event.target.value);
                }}
              />
            )}
          </Field>
        </div>

        <section className="flex flex-col gap-2">
          <h3 className="text-text-primary text-sm font-medium">
            Hosts{count > 0 ? ` · ${String(count)} selected` : ''}
          </h3>
          <ul className="border-border-subtle max-h-56 overflow-y-auto rounded-lg border">
            {ordered.map((host) => (
              <li
                key={host.host_id}
                className="border-border-subtle flex items-center gap-3 border-b px-3 py-2 last:border-0"
              >
                <input
                  type="checkbox"
                  id={`rollout-host-${host.host_id}`}
                  checked={selected.has(host.host_id)}
                  onChange={() => {
                    toggle(host.host_id);
                  }}
                />
                <label
                  htmlFor={`rollout-host-${host.host_id}`}
                  className="min-w-0 flex-1 cursor-pointer text-sm"
                >
                  <span className="text-text-primary">{host.host_name}</span>{' '}
                  <span className="text-text-faint text-xs">
                    {host.installed_version ?? 'version unknown'}
                    {/* The planner's own word for a host with nothing to do,
                        not "current": pinned, unsupported and
                        version-unknown are all "not installing today" for
                        quite different reasons, and flattening them into
                        "current" would assert something nothing checked. */}
                    {canStartUpdate(host.plan)
                      ? ` → ${host.plan.target_version ?? ''}`
                      : ` · ${OUTCOMES[host.plan.outcome].label.toLowerCase()}`}
                  </span>
                </label>
              </li>
            ))}
          </ul>
          {ordered.length === 0 ? (
            <p className="text-text-faint text-xs">
              No hosts are registered, so there is nothing to roll out to.
            </p>
          ) : null}
        </section>

        <Field
          id={canaryId}
          label="Canary"
          hint="The one host that goes first. Left to the controller, it picks a connected one."
        >
          {(controlProps) => (
            <Select
              {...controlProps}
              value={canaryHost}
              onChange={(event) => {
                setCanaryHost(event.target.value);
              }}
            >
              <option value="">Let the controller choose</option>
              {ordered
                .filter((host) => selected.has(host.host_id))
                .map((host) => (
                  <option key={host.host_id} value={host.host_id}>
                    {host.host_name}
                  </option>
                ))}
            </Select>
          )}
        </Field>

        <div className="grid gap-4 sm:grid-cols-3">
          <Field id={batchId} label="Batch size" hint="Hosts per batch after the canary.">
            {(controlProps) => (
              <Input
                {...controlProps}
                type="number"
                min={1}
                max={100}
                value={batchSize}
                onChange={(event) => {
                  setBatchSize(Number(event.target.value));
                }}
              />
            )}
          </Field>
          <Field
            id={toleranceId}
            label="Failures allowed"
            hint="Zero stops on the first. Skipped hosts do not count."
          >
            {(controlProps) => (
              <Input
                {...controlProps}
                type="number"
                min={0}
                max={100}
                value={tolerance}
                onChange={(event) => {
                  setTolerance(Number(event.target.value));
                }}
              />
            )}
          </Field>
          <Field
            id={healthId}
            label="Health check (s)"
            hint="How long a host must stay healthy before the next batch."
          >
            {(controlProps) => (
              <Input
                {...controlProps}
                type="number"
                min={0}
                max={3600}
                value={healthSeconds}
                onChange={(event) => {
                  setHealthSeconds(Number(event.target.value));
                }}
              />
            )}
          </Field>
        </div>

        {/* Built from the current values rather than described in prose: a
            static sentence would be right until somebody changed a field,
            and this is the sentence somebody needs to check. */}
        <p className="text-text-secondary text-sm">
          {count === 0 ? (
            'Select at least one host.'
          ) : (
            <>
              1 canary
              {batches === 0
                ? ' and nothing after it'
                : `, then ${String(batches)} batch${batches === 1 ? '' : 'es'} of up to ${String(batchSize)}`}
              {tolerance === 0
                ? ', stopping on the first failure'
                : `, stopping after ${String(tolerance)} failures`}
              {healthSeconds === 0
                ? '. No health check: a host counts as done the moment it reports the new version.'
                : `. Each host must stay healthy for ${String(healthSeconds)}s before the next batch.`}
            </>
          )}
        </p>
      </div>
    </Modal>
  );
}
