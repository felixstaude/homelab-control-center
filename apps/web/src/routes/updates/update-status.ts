/**
 * The vocabulary for rendering update state.
 *
 * Split from the components for the same reason `host-status.ts` is: a module
 * that exports only components can be replaced by Vite's fast refresh without
 * reloading the page, and a module exporting both cannot.
 *
 * ## Nothing here invents a judgement
 *
 * Every mapping below turns a value the server sent into words and a colour.
 * None of them decides whether a situation is *acceptable* — that is the
 * server's, and it has already said so: a blocked update arrives with its
 * blockers, an unsupported version arrives with a sentence. The interface's
 * job is to render that faithfully, including rendering "we cannot tell" as
 * itself rather than as "fine".
 */

import type { HostUpdateStatus, RolloutMember, UpdateJob, UpdatePlan } from '@/api/types';

/** Badge variants, matching `components/ui/badge.tsx`. */
export type StatusTone = 'neutral' | 'ok' | 'warn' | 'critical' | 'info' | 'unknown' | 'accent';

/** How one outcome is shown. */
export type OutcomePresentation = {
  label: string;
  tone: StatusTone;
  /** One sentence, for a tooltip or a detail line. */
  explanation: string;
};

/**
 * The eight outcomes, each rendered differently.
 *
 * `update_available_incompatible` is `warn` rather than `critical`: the host
 * is working, it simply cannot take the newer release. `unsupported` is
 * `critical` because it is the one state where the installed agent is outside
 * what the controller is known to work with.
 *
 * `metadata_unavailable` is `unknown`, not `ok`. A controller with no release
 * metadata does not know whether an update exists, and showing that as "up to
 * date" would be the interface asserting something nothing checked.
 */
export const OUTCOMES: Record<UpdatePlan['outcome'], OutcomePresentation> = {
  up_to_date: {
    label: 'Up to date',
    tone: 'ok',
    explanation: 'Running the newest release its channel offers.',
  },
  update_available: {
    label: 'Update available',
    tone: 'info',
    explanation: 'A newer release exists and nothing blocks it.',
  },
  update_available_incompatible: {
    label: 'Update blocked',
    tone: 'warn',
    explanation:
      'A newer release exists and something stops it. The host is still working.',
  },
  downgrade: {
    label: 'Downgrade',
    tone: 'warn',
    explanation: 'The target is older than what is installed.',
  },
  pinned: {
    label: 'Pinned',
    tone: 'neutral',
    explanation: 'Held at a specific version by an administrator.',
  },
  unknown_version: {
    label: 'Version unknown',
    tone: 'unknown',
    explanation:
      'This host reports a version that cannot be compared with the published releases.',
  },
  unsupported: {
    label: 'Unsupported',
    tone: 'critical',
    explanation:
      'Older than the oldest version this controller is known to work with.',
  },
  metadata_unavailable: {
    label: 'No release data',
    tone: 'unknown',
    explanation:
      'This controller has no release metadata, so it cannot tell whether an update exists.',
  },
};

/** How one job state is shown. */
export type StatePresentation = {
  label: string;
  tone: StatusTone;
  /** Whether the host is working on it right now. */
  inFlight: boolean;
};

/**
 * Every update state the server can report.
 *
 * `deferred` is `neutral` and **not** a failure. Conflating the two would make
 * a healthy fleet waiting for its maintenance window look broken, which is
 * the single most likely way this interface could mislead somebody.
 *
 * `rolled_back` is `warn` rather than `critical`: the update failed *and the
 * service is running again*, which is the designed outcome of a failed health
 * check and needs a different reaction from a plain failure.
 */
export const JOB_STATES: Record<string, StatePresentation> = {
  available: { label: 'Available', tone: 'info', inFlight: false },
  queued: { label: 'Queued', tone: 'info', inFlight: true },
  downloading: { label: 'Downloading', tone: 'info', inFlight: true },
  verifying: { label: 'Verifying', tone: 'info', inFlight: true },
  staged: { label: 'Staged', tone: 'info', inFlight: true },
  installing: { label: 'Installing', tone: 'accent', inFlight: true },
  restarting: { label: 'Restarting', tone: 'accent', inFlight: true },
  verifying_health: { label: 'Checking health', tone: 'accent', inFlight: true },
  succeeded: { label: 'Succeeded', tone: 'ok', inFlight: false },
  failed: { label: 'Failed', tone: 'critical', inFlight: false },
  rolled_back: { label: 'Rolled back', tone: 'warn', inFlight: false },
  cancelled: { label: 'Cancelled', tone: 'neutral', inFlight: false },
  deferred: { label: 'Deferred', tone: 'neutral', inFlight: false },
};

/**
 * How a state the client does not recognise is shown.
 *
 * The server's vocabulary can grow — a controller newer than this bundle is an
 * ordinary consequence of upgrading the controller first — so an unknown
 * state renders as itself rather than as nothing. A blank cell would read as
 * "no update", which is the one wrong answer.
 */
export function jobState(state: string): StatePresentation {
  return JOB_STATES[state] ?? { label: state, tone: 'unknown', inFlight: false };
}

/** How one policy is shown. */
export const POLICY_LABELS: Record<HostUpdateStatus['policy'], string> = {
  manual: 'Manual',
  notify: 'Notify only',
  patch: 'Automatic patch',
  minor: 'Automatic minor',
  pinned: 'Pinned',
};

/** The step sizes, for the confirmation an administrator reads. */
export const STEP_LABELS: Record<UpdatePlan['step'], string> = {
  major: 'major',
  minor: 'minor',
  patch: 'patch',
  prerelease: 'pre-release',
  none: 'no change',
};

/** 0 = Sunday, matching the server and cron. */
export const WEEKDAY_NAMES = [
  'Sunday',
  'Monday',
  'Tuesday',
  'Wednesday',
  'Thursday',
  'Friday',
  'Saturday',
] as const;

/**
 * How the metadata signature is shown.
 *
 * **Only `valid` is presented as authentic.** `absent` is the honest state of
 * a project that has published nothing, and `untrusted_key` means something
 * was signed by a key this controller does not hold — rendering either as
 * "signed" would tell an administrator the metadata is authentic when nothing
 * verified it.
 */
export const SIGNATURE_PRESENTATION: Record<string, OutcomePresentation> = {
  valid: {
    label: 'Signed',
    tone: 'ok',
    explanation: 'The release metadata carries a valid publisher signature.',
  },
  absent: {
    label: 'Unsigned',
    tone: 'warn',
    explanation:
      'No signature accompanies the release metadata, so its authenticity cannot be established. Automatic updates require one.',
  },
  untrusted_key: {
    label: 'Untrusted key',
    tone: 'warn',
    explanation:
      'The metadata was signed by a key this controller does not trust. Configure HCC_RELEASE_SIGNING_KEYS, or check who signed it.',
  },
  invalid: {
    label: 'Signature invalid',
    tone: 'critical',
    explanation:
      'The signature does not verify. Either the metadata changed after it was signed, or it was not signed by the key it names.',
  },
  malformed: {
    label: 'Signature unreadable',
    tone: 'critical',
    explanation: 'The signature file is not readable as one.',
  },
  unsupported_algorithm: {
    label: 'Unsupported algorithm',
    tone: 'critical',
    explanation: 'The signature names an algorithm this controller does not implement.',
  },
};

export function signaturePresentation(status: string): OutcomePresentation {
  return (
    SIGNATURE_PRESENTATION[status] ?? {
      label: status,
      tone: 'unknown',
      explanation: 'This controller reported a signature status the interface does not know.',
    }
  );
}

/**
 * Whether an administrator could start an update from this plan.
 *
 * Mirrors `Outcome.is_actionable` on the server. Duplicated deliberately
 * rather than sent as a field: the interface needs it to decide whether to
 * render a button, and a button that appears and then 422s is worse than one
 * that is absent. The server refuses regardless, which is what makes this a
 * convenience rather than the boundary.
 */
export function canStartUpdate(plan: UpdatePlan): boolean {
  return plan.outcome === 'update_available' || plan.outcome === 'downgrade';
}

/**
 * Whether an administrator could cancel this job.
 *
 * Mirrors the transition table: cancellation stops being offered once the
 * binary is being replaced, because by then the controller cannot keep the
 * promise. Offering a button that does nothing is worse than offering none.
 */
export function canCancel(job: UpdateJob | null): boolean {
  if (job === null) return false;
  return ['available', 'queued', 'downloading', 'verifying', 'staged'].includes(job.state);
}

/**
 * How far through an update a job is, as a fraction.
 *
 * Derived from the *reported* state, never from elapsed time. A bar that
 * advanced on a timer would be inventing progress, and somebody would believe
 * it — so a job stuck in `downloading` shows a bar that does not move, which
 * is the true and useful answer.
 *
 * Returns `null` for a job that is not in flight: there is no progress to show
 * for one that has finished.
 */
export function jobProgress(job: UpdateJob | null): number | null {
  if (job === null) return null;
  const order = [
    'queued',
    'downloading',
    'verifying',
    'staged',
    'installing',
    'restarting',
    'verifying_health',
  ];
  const index = order.indexOf(job.state);
  if (index < 0) return null;
  return (index + 1) / (order.length + 1);
}

/**
 * A rollout member's state as an operator reads it.
 *
 * `succeeded` splits in two, and that split is the point. A member can be
 * succeeded and not healthy: the job concludes when the agent reconnects
 * reporting the target version, and the rollout then waits to see whether
 * the host is still there, still acknowledging its configuration and still
 * delivering telemetry.
 *
 * Rendering those as one state would make the wait look like a hang — and
 * the first thing somebody does about a hang is press resume, which is
 * exactly the wrong thing.
 *
 * `skipped` is neutral rather than a warning. A powered-off host, or one
 * already on the target version, says nothing about the release, and
 * colouring it would train somebody to ignore the colour.
 */
export function memberPresentation(member: RolloutMember): {
  label: string;
  variant: 'ok' | 'warn' | 'critical' | 'neutral' | 'info';
} {
  switch (member.state) {
    case 'succeeded':
      return member.healthy
        ? { label: 'healthy', variant: 'ok' }
        : { label: 'installed, checking', variant: 'info' };
    case 'failed':
      return { label: 'failed', variant: 'critical' };
    case 'skipped':
      return { label: 'skipped', variant: 'neutral' };
    case 'running':
      return { label: 'updating', variant: 'info' };
    default:
      return { label: 'waiting', variant: 'neutral' };
  }
}
