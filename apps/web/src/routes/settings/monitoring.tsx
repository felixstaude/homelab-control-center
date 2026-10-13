import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { RotateCcw } from 'lucide-react';
import { useState } from 'react';

import {
  fetchGlobalDefaults,
  fetchModuleCatalogue,
  setGlobalDefault,
} from '@/api/monitoring';
import type { GlobalDefault, ModuleDescriptor, ScheduleDocument } from '@/api/types';
import { useAuth, usePermissions } from '@/auth/use-auth';
import { PageHeader } from '@/components/page-header';
import { ErrorPanel, MutationError, PermissionRequired } from '@/components/states/panel-states';
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
import { Input } from '@/components/ui/input';
import { Select } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import { Spinner } from '@/components/ui/spinner';
import { queryKeys } from '@/lib/query-keys';
import { describeSeconds } from '@/routes/hosts/metric-format';

/**
 * Installation-wide monitoring defaults.
 *
 * ## What makes this page dangerous, and what it does about that
 *
 * A global default applies to **every host that has no override of its own**,
 * so one switch here can start collection on an entire fleet. That is the
 * point of the layer — configuring forty hosts individually is not a workflow
 * — but it means the page has to be explicit about reach rather than looking
 * like a preferences panel. So each row states how many hosts it would affect
 * in words, and the copy says plainly that enabling a module starts collecting
 * from machines nobody visited.
 *
 * ## Off is the resting state, and that is a decision
 *
 * §2.1. Every module ships disabled, so a fresh installation collects nothing
 * from anything until somebody comes here or configures a host. The page says
 * so instead of leaving an operator to conclude the agent is broken.
 *
 * ## The interval floors are the server's, not reproduced here
 *
 * The catalogue reports each module's `min_interval_seconds` and the page
 * *displays* it, but the refusal comes from the server. A client-side check
 * would be a second implementation of a rule that can disagree with the first,
 * and the failure mode is a control that accepts a value the API then rejects
 * — or worse, one that silently clamps and saves something the operator did
 * not ask for (§7: do not silently ignore invalid settings).
 */

type ModuleChoice = 'on' | 'off' | 'unset';

export function SettingsMonitoring() {
  const permissions = usePermissions();
  const { state } = useAuth();

  // Not fetched for a role that cannot see the result. Not a security
  // property — the server enforces that — but an unavoidable 403 in the
  // console is noise somebody reports as a bug.
  const defaults = useQuery({
    queryKey: queryKeys.session.globalDefaults(),
    queryFn: ({ signal }) => fetchGlobalDefaults(signal),
    enabled: permissions.configureMonitoring,
  });

  const catalogue = useQuery({
    queryKey: queryKeys.session.moduleCatalogue(),
    queryFn: ({ signal }) => fetchModuleCatalogue(signal),
    staleTime: Infinity,
    enabled: permissions.configureMonitoring,
  });

  if (!permissions.configureMonitoring) {
    return (
      <>
        <PageHeader
          title="Monitoring"
          description="What every host collects unless it says otherwise."
        />
        <PermissionRequired
          required="admin"
          actual={state.status === 'authenticated' ? state.role : 'viewer'}
          what="Monitoring defaults"
        />
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Monitoring"
        description="What every host collects unless it overrides the setting itself."
      />

      <div className="flex flex-col gap-6">
        <Alert variant="info" title="These settings reach every host">
          A default applies to every host that has not overridden it, so enabling a module here
          starts collecting from machines you have not visited. Hosts with their own setting are
          unaffected &mdash; change those on the host&rsquo;s own page.
        </Alert>

        <Card>
          <CardHeader>
            <div className="min-w-0">
              <CardTitle>Modules</CardTitle>
              <CardDescription>
                Every module is off until somebody turns it on. Nothing is collected from a
                newly enrolled host by default, which is deliberate: a monitoring system should
                not start reading a machine because it was registered.
              </CardDescription>
            </div>
          </CardHeader>
          <CardContent>
            {defaults.isPending || catalogue.isPending ? (
              <div className="flex flex-col gap-3">
                <Skeleton className="h-16" />
                <Skeleton className="h-16" />
                <Skeleton className="h-16" />
              </div>
            ) : defaults.isError ? (
              <ErrorPanel
                error={defaults.error}
                title="Could not load the monitoring defaults"
                onRetry={() => {
                  void defaults.refetch();
                }}
              />
            ) : catalogue.isError ? (
              <ErrorPanel
                error={catalogue.error}
                title="Could not load the module catalogue"
                onRetry={() => {
                  void catalogue.refetch();
                }}
              />
            ) : (
              <ul className="divide-border-subtle divide-y">
                {catalogue.data.modules.map((descriptor) => (
                  <DefaultRow
                    key={descriptor.id}
                    descriptor={descriptor}
                    stored={defaults.data.defaults.find(
                      (entry) => entry.module_id === descriptor.id,
                    )}
                  />
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </div>
    </>
  );
}

function DefaultRow({
  descriptor,
  stored,
}: {
  descriptor: ModuleDescriptor;
  stored: GlobalDefault | undefined;
}) {
  const queryClient = useQueryClient();

  const invalidate = async () => {
    // The whole monitoring root: this changed the effective configuration of
    // every host that inherits, and nothing here knows which host views are
    // open. Also the metrics root, because a module just started or stopped
    // producing series.
    await queryClient.invalidateQueries({ queryKey: queryKeys.session.monitoring() });
    await queryClient.invalidateQueries({ queryKey: queryKeys.session.metrics() });
  };

  const save = useMutation({
    mutationFn: async (body: { enabled?: boolean | null; schedule?: ScheduleDocument | null }) =>
      await setGlobalDefault(descriptor.id, body),
    onSuccess: invalidate,
  });

  const storedEnabled = stored?.setting.enabled ?? null;
  const choice: ModuleChoice =
    storedEnabled === null ? 'unset' : storedEnabled ? 'on' : 'off';
  const storedSchedule = stored?.setting.schedule ?? null;
  const storedInterval =
    typeof storedSchedule?.interval_seconds === 'number'
      ? storedSchedule.interval_seconds
      : null;

  return (
    <li className="flex flex-col gap-3 py-4 first:pt-0 last:pb-0">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-text-primary text-sm font-medium">{descriptor.name}</span>
            {choice === 'on' ? (
              <Badge variant="accent">On by default</Badge>
            ) : choice === 'off' ? (
              <Badge variant="neutral">Off by default</Badge>
            ) : (
              <Badge variant="unknown">Not configured</Badge>
            )}
            {descriptor.requires_privileges ? (
              <Badge variant="warn">Needs privileges</Badge>
            ) : null}
          </div>
          <p className="text-text-muted mt-1 text-xs leading-relaxed">
            {descriptor.description}
          </p>
          <p className="text-text-faint mt-1 text-xs">
            {descriptor.metrics.length} metric{descriptor.metrics.length === 1 ? '' : 's'}{' '}
            &middot; default every {describeSeconds(descriptor.default_interval_seconds)} &middot;
            no faster than every {describeSeconds(descriptor.min_interval_seconds)}
            {/* "Not configured" and "off" resolve to the same behaviour and
             * are different facts: the first means nobody has decided, and
             * setting it to off later is a decision somebody can audit. */}
            {choice === 'unset' ? ' · nothing has been decided, so hosts collect nothing' : null}
          </p>
        </div>

        <div className="flex shrink-0 items-center gap-2">
          {save.isPending ? <Spinner /> : null}
          <label className="sr-only" htmlFor={`default-${descriptor.id}`}>
            {descriptor.name} default
          </label>
          <Select
            id={`default-${descriptor.id}`}
            className="w-auto"
            value={choice}
            disabled={save.isPending}
            onChange={(event) => {
              const next = event.target.value as ModuleChoice;
              // Only `enabled` is sent. Omitting `schedule` leaves the stored
              // interval alone — sending `null` would clear it every time
              // somebody toggled the module.
              save.mutate({ enabled: next === 'unset' ? null : next === 'on' });
            }}
          >
            <option value="on">On</option>
            <option value="off">Off</option>
            <option value="unset">Not configured</option>
          </Select>
        </div>
      </div>

      <IntervalField
        // Remounted when the stored value changes, which is how the draft is
        // reset without a render-phase setState. Keyed on the value rather
        // than reset in an effect so that an administrator typing is never
        // interrupted by a poll: the key only changes when the stored interval
        // actually did.
        key={storedInterval === null ? 'unset' : String(storedInterval)}
        descriptor={descriptor}
        current={storedInterval}
        disabled={save.isPending}
        onSave={(seconds) => {
          save.mutate({
            schedule:
              seconds === null
                ? null
                : { kind: 'interval', interval_seconds: seconds },
          });
        }}
      />

      {save.isError ? <MutationError error={save.error} /> : null}
    </li>
  );
}

/**
 * How often the module runs, by default.
 *
 * A text field rather than a slider or a set of presets, because the valid
 * range spans four orders of magnitude and the floor differs per module. The
 * field does not validate against the floor: it is displayed next to the
 * input, and the server is what refuses. A client that enforced it would be a
 * second copy of the rule, and the two can drift.
 */
function IntervalField({
  descriptor,
  current,
  disabled,
  onSave,
}: {
  descriptor: ModuleDescriptor;
  current: number | null;
  disabled: boolean;
  onSave: (seconds: number | null) => void;
}) {
  const canonical = current === null ? '' : String(current);
  const [draft, setDraft] = useState<string>(canonical);

  const parsed = draft.trim() === '' ? null : Number(draft);
  const isNumeric = parsed === null || (Number.isFinite(parsed) && Number.isInteger(parsed));
  const changed = draft.trim() !== canonical;

  return (
    <form
      className="flex flex-wrap items-end gap-2"
      onSubmit={(event) => {
        event.preventDefault();
        if (!isNumeric) return;
        onSave(parsed);
      }}
    >
      <div className="min-w-0">
        <label
          className="text-text-faint text-xs font-medium"
          htmlFor={`interval-${descriptor.id}`}
        >
          Interval (seconds)
        </label>
        <Input
          id={`interval-${descriptor.id}`}
          className="mt-1 w-32"
          inputMode="numeric"
          value={draft}
          disabled={disabled}
          placeholder={String(descriptor.default_interval_seconds)}
          onChange={(event) => {
            setDraft(event.target.value);
          }}
        />
      </div>
      <Button type="submit" variant="secondary" size="sm" disabled={disabled || !changed}>
        Save interval
      </Button>
      {current === null ? null : (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          disabled={disabled}
          title="Clear the stored interval so the module's own default applies"
          onClick={() => {
            onSave(null);
          }}
        >
          <RotateCcw />
          <span className="sr-only">Clear the {descriptor.name} interval</span>
        </Button>
      )}
      <p className="text-text-faint basis-full text-xs">
        {current === null
          ? `Nothing stored, so the module's own default of ${describeSeconds(descriptor.default_interval_seconds)} applies.`
          : `Stored: every ${describeSeconds(current)}.`}{' '}
        {isNumeric ? null : (
          <span className="text-status-critical">Enter a whole number of seconds.</span>
        )}
      </p>
    </form>
  );
}
