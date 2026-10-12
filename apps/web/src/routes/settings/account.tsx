import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { KeyRound } from 'lucide-react';
import { useId, useRef, useState } from 'react';

import { changePassword, fetchAccount } from '@/api/account';
import { isApiError } from '@/api/errors';
import { userLabel } from '@/api/types';
import { ROLE_DESCRIPTIONS, ROLE_LABELS } from '@/auth/permissions';
import { ErrorPanel, LoadingRows, MutationError } from '@/components/states/panel-states';
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
import { Field } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import { Spinner } from '@/components/ui/spinner';
import { formatDate, formatDateTime, machineDateTime } from '@/lib/datetime';
import { queryKeys } from '@/lib/query-keys';

/**
 * My account.
 *
 * ## What is not here, and why
 *
 * **No self-service role change.** Obviously — it would be a privilege
 * escalation endpoint with a form attached.
 *
 * **No self-service account deletion.** Deactivation is the removal mechanism
 * throughout this product, and self-deactivation would let the last
 * administrator lock the installation out of its own recovery path. An
 * administrator does it, where the last-administrator guard applies.
 *
 * **No self-service display-name editing.** This one is a genuine judgement
 * call rather than a security boundary, and it goes the conservative way: the
 * display name is how an administrator reviewing the user list identifies an
 * account, so letting every user rewrite their own label means the list an
 * administrator is auditing is populated with names those users chose. The
 * narrowly-scoped endpoint it would need does not exist, and this section says
 * where the change is made instead of offering a field that does nothing.
 */
export function SettingsAccount() {
  const account = useQuery({
    queryKey: queryKeys.session.account(),
    queryFn: ({ signal }) => fetchAccount(signal),
  });

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <CardHeader>
          <div>
            <CardTitle>Your account</CardTitle>
            <CardDescription>
              Your display name and role are managed by an administrator. Your password is
              yours to change below.
            </CardDescription>
          </div>
        </CardHeader>
        <CardContent>
          {account.isPending ? (
            <LoadingRows rows={4} columns={2} />
          ) : account.isError ? (
            <ErrorPanel
              error={account.error}
              onRetry={() => {
                void account.refetch();
              }}
            />
          ) : (
            <dl className="divide-border-subtle divide-y text-sm">
              <Fact label="Display name">
                {account.data.display_name ?? (
                  <span className="text-text-faint">
                    Not set — you are shown as {account.data.username}
                  </span>
                )}
              </Fact>
              <Fact label="Username">
                <span className="font-mono text-xs">{account.data.username}</span>
              </Fact>
              <Fact label="Role">
                <span className="flex flex-wrap items-center justify-end gap-2">
                  <Badge variant={account.data.role === 'admin' ? 'accent' : 'neutral'}>
                    {ROLE_LABELS[account.data.role]}
                  </Badge>
                  <span className="text-text-faint text-xs">
                    {ROLE_DESCRIPTIONS[account.data.role]}
                  </span>
                </span>
              </Fact>
              <Fact label="Status">
                <Badge variant={account.data.is_active ? 'ok' : 'unknown'}>
                  {account.data.is_active ? 'Active' : 'Inactive'}
                </Badge>
              </Fact>
              <Fact label="Last sign-in">
                <time dateTime={machineDateTime(account.data.last_login_at)}>
                  {formatDateTime(account.data.last_login_at)}
                </time>
              </Fact>
              <Fact label="Account created">
                <time dateTime={machineDateTime(account.data.created_at)}>
                  {formatDate(account.data.created_at)}
                </time>
              </Fact>
            </dl>
          )}
        </CardContent>
      </Card>

      <ChangePasswordCard
        username={account.data ? userLabel(account.data) : undefined}
      />
    </div>
  );
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-2 py-2.5 first:pt-0 last:pb-0">
      <dt className="text-text-muted">{label}</dt>
      <dd className="text-text-primary text-right">{children}</dd>
    </div>
  );
}

/**
 * Change-password form.
 *
 * Three things worth knowing about the flow it drives:
 *
 * 1. **The current password is required** even though the user already holds a
 *    valid session. A session proves possession of a cookie; changing a
 *    credential should require possession of the credential, so a stolen
 *    session cannot lock the real owner out of their own account.
 * 2. **Every other session is ended, and this one is replaced.** The server
 *    issues fresh cookies on the response, so the user stays signed in here
 *    while the token the browser held before stops working (ADR-040).
 * 3. **A wrong current password is a 422, not a 401.** The client treats 401
 *    as "your session has ended" and signs the user out — so the server
 *    deliberately does not send one, and this form shows the message against
 *    the field instead.
 */
function ChangePasswordCard({ username }: { username: string | undefined }) {
  const queryClient = useQueryClient();

  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [submitted, setSubmitted] = useState(false);
  const [outcome, setOutcome] = useState<{ otherSessionsRevoked: number } | null>(null);

  const formId = useId();
  const currentId = useId();
  const nextId = useId();
  const confirmationId = useId();

  /** Synchronous double-submit guard; see the login form for the reasoning. */
  const inFlight = useRef(false);

  const mutation = useMutation({
    mutationFn: changePassword,
    onSuccess: async (result) => {
      setOutcome({ otherSessionsRevoked: result.other_sessions_revoked });
      // The session list is now exactly one row, and the identity's expiry
      // moved, so both caches are stale.
      await queryClient.invalidateQueries({ queryKey: queryKeys.session.accountSessions() });
      await queryClient.invalidateQueries({ queryKey: queryKeys.session.currentUser() });
    },
  });

  const clearSecrets = () => {
    setCurrent('');
    setNext('');
    setConfirmation('');
    setSubmitted(false);
  };

  const currentMissing = submitted && current === '';
  const nextMissing = submitted && next === '';
  const mismatch = submitted && next !== '' && confirmation !== next;

  /** The server's message for a wrong current password, against its field. */
  const wrongCurrent =
    isApiError(mutation.error) && mutation.error.code === 'invalid_current_password'
      ? mutation.error.message
      : null;

  /** Policy violations, listed in full as the server reported them. */
  const policyProblems =
    isApiError(mutation.error) && Array.isArray(mutation.error.details?.problems)
      ? (mutation.error.details.problems as unknown[]).filter(
          (problem): problem is string => typeof problem === 'string',
        )
      : [];

  const handleSubmit = async (event: { preventDefault: () => void }) => {
    event.preventDefault();

    /*
     * A submit with nothing typed is a complete no-op — no validation
     * messages, and the previous outcome left alone.
     *
     * This is what makes a double-click safe here, and the reason is specific:
     * the form clears itself on success, so if the second click lands after
     * the first has resolved, the in-flight ref is already released and the
     * handler runs again against three empty fields. Without this guard it
     * cleared the "Password changed" banner the first click had just earned
     * and replaced it with "Enter your current password" — telling the user
     * their password change had failed when it had in fact succeeded. Caught
     * by `sends one request even when the button is double-clicked`.
     */
    if (current === '' && next === '' && confirmation === '') return;

    setSubmitted(true);
    setOutcome(null);

    if (current === '' || next === '' || confirmation !== next) return;
    if (inFlight.current) return;
    inFlight.current = true;

    try {
      await mutation.mutateAsync({ current_password: current, new_password: next });
      // Clear the plaintext from component state the moment it is no longer
      // needed. It remains in memory until React drops the closure, but
      // leaving it in state keeps it in a live component tree.
      clearSecrets();
    } catch {
      // The typed error is rendered below. The new password is cleared but the
      // current one is kept, so a policy rejection does not make the user
      // retype the field that was fine.
      setNext('');
      setConfirmation('');
      setSubmitted(false);
    } finally {
      inFlight.current = false;
    }
  };

  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>Change your password</CardTitle>
          <CardDescription>
            At least 8 characters, with no composition rules. Any Unicode is accepted and
            nothing is truncated.
          </CardDescription>
        </div>
      </CardHeader>
      <CardContent>
        {outcome ? (
          <Alert variant="info" title="Password changed" className="mb-4">
            {outcome.otherSessionsRevoked === 0 ? (
              <>
                This was the only place {username ?? 'your account'} was signed in. You are
                still signed in here, on a new session.
              </>
            ) : (
              <>
                Signed out of {outcome.otherSessionsRevoked} other{' '}
                {outcome.otherSessionsRevoked === 1 ? 'session' : 'sessions'}. You are still
                signed in here, on a new session.
              </>
            )}
          </Alert>
        ) : null}

        <MutationError
          error={wrongCurrent || policyProblems.length > 0 ? null : mutation.error}
        />

        {policyProblems.length > 0 ? (
          <Alert variant="critical" title="That password is not accepted" className="mb-4">
            <ul className="list-disc space-y-1 pl-4">
              {policyProblems.map((problem) => (
                <li key={problem}>{problem}</li>
              ))}
            </ul>
          </Alert>
        ) : null}

        <form
          id={formId}
          onSubmit={(event) => {
            void handleSubmit(event);
          }}
          className="flex max-w-sm flex-col gap-4"
          noValidate
        >
          <Field
            id={currentId}
            label="Current password"
            error={currentMissing ? 'Enter your current password.' : wrongCurrent}
          >
            {(controlProps) => (
              <Input
                {...controlProps}
                name="current_password"
                type="password"
                value={current}
                onChange={(event) => {
                  setCurrent(event.target.value);
                }}
                autoComplete="current-password"
                required
                disabled={mutation.isPending}
              />
            )}
          </Field>

          <Field
            id={nextId}
            label="New password"
            error={nextMissing ? 'Enter a new password.' : null}
          >
            {(controlProps) => (
              <Input
                {...controlProps}
                name="new_password"
                type="password"
                value={next}
                onChange={(event) => {
                  setNext(event.target.value);
                }}
                autoComplete="new-password"
                required
                disabled={mutation.isPending}
              />
            )}
          </Field>

          <Field
            id={confirmationId}
            label="Confirm new password"
            hint="Checked here, and not sent to the server — it has nothing to compare it against that this form does not."
            error={mismatch ? 'The two passwords do not match.' : null}
          >
            {(controlProps) => (
              <Input
                {...controlProps}
                name="confirm_password"
                type="password"
                value={confirmation}
                onChange={(event) => {
                  setConfirmation(event.target.value);
                }}
                autoComplete="new-password"
                required
                disabled={mutation.isPending}
              />
            )}
          </Field>

          <Alert variant="warn" title="This signs you out everywhere else">
            Every other session for your account is ended, and this one is replaced with a
            new one — so you stay signed in here.
          </Alert>

          <Button
            type="submit"
            variant="primary"
            disabled={mutation.isPending}
            className="self-start"
          >
            {mutation.isPending ? <Spinner /> : <KeyRound />}
            {mutation.isPending ? 'Changing…' : 'Change password'}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
