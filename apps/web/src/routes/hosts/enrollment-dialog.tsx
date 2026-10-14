import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Check, Copy, KeyRound } from 'lucide-react';
import { useEffect, useState } from 'react';

import { fetchAgentReleases } from '@/api/agent-releases';
import { isApiError } from '@/api/errors';
import { issueEnrollment } from '@/api/hosts';
import type { AgentReleases, Host, IssuedEnrollment } from '@/api/types';
import { MutationError } from '@/components/states/panel-states';
import { Alert } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Modal } from '@/components/ui/modal';
import { Spinner } from '@/components/ui/spinner';
import { formatDateTime } from '@/lib/datetime';
import { queryKeys } from '@/lib/query-keys';
import { InstallInstructions } from '@/routes/hosts/install-instructions';

/**
 * Issue and display a one-time enrollment credential.
 *
 * ## The only screen in this product that shows a secret
 *
 * The credential exists in exactly one response body and nowhere else. The
 * server keeps only its SHA-256 digest, so there is no endpoint that could
 * return it again — which means this dialog is not "the convenient place to
 * see it", it is the *only* place, and the copy says so plainly rather than
 * implying a settings page where it could be found later.
 *
 * Three consequences for how this component is written:
 *
 * - **It is not in a query.** `useMutation`, not `useQuery`, so the value
 *   never enters the TanStack Query cache — where it would sit until the cache
 *   was cleared, be returned to any component asking for the same key, and
 *   survive the dialog closing.
 * - **It is dropped when the dialog closes.** Every call site gives this
 *   component a `key` that changes when it opens, closes or is aimed at a
 *   different host, so React discards the whole instance and its state with
 *   it. A `useEffect` that reset the fields would have done the same thing
 *   one render later and only for the cases somebody remembered to list;
 *   remounting cannot be partially right. Reopening therefore shows the issue
 *   button, never a credential from ten minutes ago.
 * - **It is never put in a URL.** Not as a route parameter, not as a query
 *   string. A credential in the address bar is in the browser's history, in
 *   the `Referer` of whatever is clicked next, and in the access log of every
 *   proxy in between.
 *
 * ## Re-enrollment is a second, explicit step
 *
 * A host that already has an enrolled agent refuses to issue a credential
 * unless re-enrollment is confirmed, because consuming the new credential
 * revokes the one the running agent is using. The server returns a 409 with
 * `host_already_enrolled`; this dialog turns that into a confirmation rather
 * than an error, so the operator makes the decision with the consequence in
 * front of them.
 */
export function EnrollmentDialog({
  host,
  onOpenChange,
}: {
  host: Host | null;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [issued, setIssued] = useState<IssuedEnrollment | null>(null);
  /** Set when the server reports the host is already enrolled. */
  const [needsConfirmation, setNeedsConfirmation] = useState(false);
  const [copied, setCopied] = useState(false);

  /**
   * What the controller can vouch for, for the install command.
   *
   * Fetched as soon as the dialog opens rather than after issuing, so the
   * command is on screen at the same moment as the credential — the
   * credential expires and the command does not, and making somebody wait for
   * a second request while a fifteen-minute clock runs is the wrong order.
   *
   * A failure here is deliberately not fatal: the install command is a
   * convenience and the credential is the thing that cannot be obtained
   * again. `releases` is then undefined and the instructions component falls
   * back to the build-from-source path.
   */
  const releases = useQuery({
    queryKey: queryKeys.session.agentReleases(),
    queryFn: ({ signal }) => fetchAgentReleases(signal),
    // The answer changes only when the controller is upgraded.
    staleTime: 5 * 60 * 1000,
    retry: false,
  });

  const issue = useMutation({
    mutationFn: (allowReenrollment: boolean) =>
      issueEnrollment(host?.host_id ?? '', { allow_reenrollment: allowReenrollment }),
    onSuccess: (result) => {
      setIssued(result);
      setNeedsConfirmation(false);
      // The list and detail views show `has_pending_enrollment`, which has
      // just changed.
      void queryClient.invalidateQueries({ queryKey: queryKeys.session.hosts() });
    },
    onError: (error) => {
      // Not an error the operator caused — a decision they have not been
      // asked for yet. Turned into the confirmation step rather than shown as
      // a failure.
      if (isApiError(error) && error.code === 'host_already_enrolled') {
        setNeedsConfirmation(true);
      }
    },
  });

  if (!host) return null;

  const copy = () => {
    if (!issued) return;
    // Best effort, and the try/catch is not decoration.
    // `navigator.clipboard` is typed as always present and is genuinely
    // `undefined` over plain HTTP on a non-loopback origin — where reading
    // `.writeText` off it throws *synchronously*, before any promise exists
    // for `.catch` to attach to.
    //
    // Which is also why the credential is always *selectable* text: a copy
    // button that is the only way to get the value is a copy button that
    // strands whoever it fails for.
    try {
      void navigator.clipboard
        .writeText(issued.credential)
        .then(() => {
          setCopied(true);
        })
        .catch(() => {
          setCopied(false);
        });
    } catch {
      setCopied(false);
    }
  };

  return (
    <Modal
      open
      onOpenChange={onOpenChange}
      title={issued ? 'Enrolment credential' : `Enrol ${host.name}`}
      description={
        issued
          ? 'Shown once. There is no way to retrieve it again.'
          : 'Issues a single-use credential the agent on this host exchanges for a permanent one.'
      }
      width="lg"
      footer={
        issued ? (
          <Button
            variant="primary"
            size="sm"
            onClick={() => {
              onOpenChange(false);
            }}
          >
            Done
          </Button>
        ) : (
          <>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => {
                onOpenChange(false);
              }}
            >
              Cancel
            </Button>
            <Button
              variant={needsConfirmation ? 'destructive' : 'primary'}
              size="sm"
              disabled={issue.isPending}
              onClick={() => {
                issue.mutate(needsConfirmation);
              }}
            >
              {issue.isPending ? <Spinner /> : <KeyRound />}
              {needsConfirmation ? 'Re-enrol anyway' : 'Issue credential'}
            </Button>
          </>
        )
      }
    >
      {issued ? (
        <IssuedCredential
          issued={issued}
          copied={copied}
          onCopy={copy}
          hostName={host.name}
          releases={releases.data}
        />
      ) : (
        <div className="flex flex-col gap-4">
          <p className="text-text-secondary text-sm leading-relaxed">
            The credential is valid for a short time, can be used once, and only works for{' '}
            <span className="text-text-primary font-medium">{host.name}</span>. Revoke it from
            this host&rsquo;s page if you paste it somewhere you did not mean to.
          </p>

          {host.has_pending_enrollment ? (
            <Alert variant="warn" title="This host already has a pending credential">
              Issuing another one revokes it immediately. The credential currently waiting to
              be used will stop working.
            </Alert>
          ) : null}

          {needsConfirmation ? (
            <Alert variant="critical" title="This host already has an enrolled agent">
              <p>
                Enrolling again revokes the credential that agent is using. It will be
                disconnected and will stay disconnected until you enrol it with the new
                credential.
              </p>
              <p className="mt-2">
                Do this when you are reinstalling the host, or when you believe its credential
                has been exposed.
              </p>
            </Alert>
          ) : null}

          {issue.isError && !needsConfirmation ? (
            <MutationError error={issue.error} />
          ) : null}
        </div>
      )}
    </Modal>
  );
}

/**
 * How often the expiry countdown redraws.
 *
 * One second. The credential is valid for minutes and the countdown is the
 * only thing on screen that tells somebody whether they still have time to
 * walk to the machine — a minute-granularity clock would sit on "1 minute"
 * through the last fifty-nine seconds of it.
 */
const COUNTDOWN_INTERVAL_MS = 1000;

/**
 * Seconds left before the credential expires.
 *
 * Counted down from the server's own `expires_in_seconds` rather than from
 * `expires_at` minus the browser's clock. A host whose clock is minutes out
 * would otherwise be shown an expiry that is wrong in whichever direction
 * their clock is wrong — and "expired" on a credential that is fine is the
 * worse of the two.
 */
function useExpiryCountdown(initialSeconds: number): number {
  // Captured once per mount, which is also once per credential: every call
  // site gives this dialog a `key` that changes when it opens, so React
  // discards the instance rather than reusing it.
  const [startedAt] = useState(() => Date.now());
  const [now, setNow] = useState(startedAt);

  useEffect(() => {
    const timer = setInterval(() => {
      setNow(Date.now());
    }, COUNTDOWN_INTERVAL_MS);
    return () => {
      clearInterval(timer);
    };
  }, []);

  // Derived from elapsed wall time rather than decremented on each tick. An
  // interval in a background tab — which this dialog will be while somebody
  // walks to a machine — is throttled and fires fewer times than the seconds
  // that passed, so a decrementing counter would run slow and claim time the
  // operator does not have.
  const elapsed = Math.floor((now - startedAt) / 1000);
  return Math.max(0, initialSeconds - elapsed);
}

function formatRemaining(seconds: number): string {
  if (seconds <= 0) return 'expired';
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  if (minutes === 0) return `${String(rest)}s`;
  return `${String(minutes)}m ${String(rest).padStart(2, '0')}s`;
}

/** The credential itself, with the one-time warning that goes with it. */
function IssuedCredential({
  issued,
  copied,
  onCopy,
  hostName,
  releases,
}: {
  issued: IssuedEnrollment;
  copied: boolean;
  onCopy: () => void;
  hostName: string;
  releases: AgentReleases | undefined;
}) {
  const remaining = useExpiryCountdown(issued.expires_in_seconds);
  const expired = remaining <= 0;

  return (
    <div className="flex flex-col gap-4">
      <Alert variant="warn" title="Copy this now">
        This is the only time the credential is shown. The control plane stores only a hash of
        it, so no page and no API call can produce it again &mdash; if you lose it, issue
        another.
      </Alert>

      {issued.superseded > 0 ? (
        <Alert variant="critical" title="A previous credential was revoked">
          {issued.superseded === 1
            ? 'One credential that had been issued for this host and not yet used has stopped working.'
            : `${String(issued.superseded)} credentials that had been issued for this host and not yet used have stopped working.`}
        </Alert>
      ) : null}

      {expired ? (
        <Alert variant="critical" title="This credential has expired">
          Close this dialog and issue another one. The agent will refuse it, and the controller
          records the attempt.
        </Alert>
      ) : null}

      <div>
        <div className="mb-1.5 flex items-center justify-between gap-2">
          <span className="text-text-secondary text-xs font-medium">
            Enrolment credential &mdash; this is the secret
          </span>
          <Button variant="secondary" size="sm" onClick={onCopy}>
            {copied ? <Check /> : <Copy />}
            {copied ? 'Copied' : 'Copy credential'}
          </Button>
        </div>
        {/*
         * Selectable text, not an input. An `<input readonly>` invites a
         * browser's password manager to offer to save it, and this is the one
         * value that must not end up in a password manager keyed to this site.
         *
         * `break-all` rather than a scroll container: a credential split
         * across lines can still be selected in full, whereas one that
         * overflows horizontally gets copied in part by anyone dragging to
         * select it.
         */}
        <output
          className="bg-surface-overlay border-border-default text-text-primary block w-full
            rounded-lg border px-3 py-2.5 font-mono text-sm break-all select-all"
        >
          {issued.credential}
        </output>
        {/*
         * A live countdown, not just the expiry timestamp. The timestamp
         * answers "when", and the question somebody standing between a
         * browser and a server actually has is "do I still have time".
         *
         * `aria-live="off"`: a region that announced every second would make
         * the dialog unusable with a screen reader, and the remaining time is
         * also stated in the timestamp beside it.
         */}
        <p className="text-text-faint mt-1.5 text-xs" aria-live="off">
          Single use. Expires in{' '}
          <span className={expired ? 'text-status-critical-fg' : 'text-text-secondary'}>
            {formatRemaining(remaining)}
          </span>{' '}
          &mdash; at {formatDateTime(issued.expires_at)}.
        </p>
      </div>

      <div className="border-border-subtle border-t pt-4">
        <h3 className="text-text-primary text-sm font-medium">On {hostName}, run</h3>
        <p className="text-text-muted mt-1 mb-3 text-xs leading-relaxed">
          The installer asks for the credential with the input hidden, so it never reaches a
          command line or a shell history. Paste it when it asks.
        </p>
        <InstallInstructions releases={releases} controllerOrigin={window.location.origin} />
      </div>
    </div>
  );
}
