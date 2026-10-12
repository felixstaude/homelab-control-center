import { useMutation, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, Check, Copy, KeyRound } from 'lucide-react';
import { useState } from 'react';

import { isApiError } from '@/api/errors';
import { issueEnrollment } from '@/api/hosts';
import type { Host, IssuedEnrollment } from '@/api/types';
import { MutationError } from '@/components/states/panel-states';
import { Alert } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Modal } from '@/components/ui/modal';
import { Spinner } from '@/components/ui/spinner';
import { formatDateTime } from '@/lib/datetime';
import { queryKeys } from '@/lib/query-keys';

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
        <IssuedCredential issued={issued} copied={copied} onCopy={copy} hostName={host.name} />
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

/** The credential itself, with the one-time warning that goes with it. */
function IssuedCredential({
  issued,
  copied,
  onCopy,
  hostName,
}: {
  issued: IssuedEnrollment;
  copied: boolean;
  onCopy: () => void;
  hostName: string;
}) {
  const minutes = Math.max(1, Math.round(issued.expires_in_seconds / 60));

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

      <div>
        <div className="mb-1.5 flex items-center justify-between gap-2">
          <span className="text-text-secondary text-xs font-medium">Enrolment credential</span>
          <Button variant="secondary" size="sm" onClick={onCopy}>
            {copied ? <Check /> : <Copy />}
            {copied ? 'Copied' : 'Copy'}
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
        <p className="text-text-faint mt-1.5 text-xs">
          Expires {formatDateTime(issued.expires_at)} &mdash; about {minutes}{' '}
          {minutes === 1 ? 'minute' : 'minutes'} from now.
        </p>
      </div>

      <div className="border-border-subtle border-t pt-4">
        <h3 className="text-text-primary text-sm font-medium">On {hostName}, run</h3>
        <p className="text-text-muted mt-1 text-xs leading-relaxed">
          Write the credential to a file rather than passing it on the command line or in the
          service&rsquo;s environment: a command line is readable by every local user through{' '}
          <code className="font-mono">/proc</code>, and so is a process&rsquo;s environment.
        </p>
        <pre
          className="bg-surface-overlay border-border-default text-text-secondary mt-2
            overflow-x-auto rounded-lg border px-3 py-2.5 font-mono text-xs leading-relaxed"
        >
          {[
            'sudo install -m 0600 /dev/null /root/hcc-enrolment-token',
            'sudo editor /root/hcc-enrolment-token    # paste the credential',
            '',
            'sudo HCC_AGENT_ENROLLMENT_TOKEN_FILE=/root/hcc-enrolment-token \\',
            '  hcc-agent enroll',
            '',
            'sudo chown hcc-agent:hcc-agent /etc/hcc-agent/credential',
            'sudo systemctl enable --now hcc-agent',
          ].join('\n')}
        </pre>
        <p className="text-text-faint mt-2 flex items-start gap-1.5 text-xs">
          <AlertTriangle aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
          <span>
            Install the agent first &mdash; see <code className="font-mono">
              agents/linux/deploy/README.md
            </code>{' '}
            in the repository. This host appears as connected within a second or two of the
            agent starting.
          </span>
        </p>
      </div>
    </div>
  );
}
