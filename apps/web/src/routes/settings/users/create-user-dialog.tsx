import { useMutation, useQueryClient } from '@tanstack/react-query';
import { UserPlus } from 'lucide-react';
import { useId, useRef, useState } from 'react';

import { isApiError } from '@/api/errors';
import { createUser } from '@/api/users';
import type { UserRole } from '@/api/types';
import { ROLE_DESCRIPTIONS, ROLE_LABELS } from '@/auth/permissions';
import { MutationError } from '@/components/states/panel-states';
import { Button } from '@/components/ui/button';
import { Field } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import { Modal } from '@/components/ui/modal';
import { Select } from '@/components/ui/select';
import { Spinner } from '@/components/ui/spinner';
import { queryKeys } from '@/lib/query-keys';

/**
 * Create-user form.
 *
 * ## What is validated here, and what is not
 *
 * Only what the client can know on its own: that the fields are non-empty and
 * that the two password entries match. Everything else — the username's
 * length, its normalization, its uniqueness, the password policy — is the
 * server's, and guessing at it here would produce a form that rejects an
 * account the server would have accepted, or accepts one it will refuse. The
 * server's own messages are shown verbatim instead.
 *
 * The confirmation field is the one thing genuinely worth checking locally:
 * it exists to catch a typo in a masked input, and the server has nothing to
 * compare it against that this form does not already have — which is why it
 * is not sent.
 *
 * ## The default role
 *
 * Viewer, matching the server's default. Spelled out in the form rather than
 * left to the server so the administrator *sees* what they are granting; a
 * role selector that opened on "Administrator" would make the dangerous choice
 * the easy one.
 */

const ROLES: readonly UserRole[] = ['viewer', 'operator', 'admin'];

export function CreateUserDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();

  const [username, setUsername] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [password, setPassword] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [role, setRole] = useState<UserRole>('viewer');
  const [submitted, setSubmitted] = useState(false);

  const usernameId = useId();
  const displayNameId = useId();
  const passwordId = useId();
  const confirmationId = useId();
  const roleId = useId();

  /**
   * Whether a submission is already in flight.
   *
   * A ref, not state, for the same reason the login form holds one: the submit
   * button is disabled from the mutation's pending flag, but that only takes
   * effect once React has re-rendered, and a double-click delivers its second
   * event first. Two submissions here would create one account and then
   * produce a confusing "username already taken" for the duplicate — or, with
   * different timing, two accounts.
   */
  const inFlight = useRef(false);

  const reset = () => {
    setUsername('');
    setDisplayName('');
    setPassword('');
    setConfirmation('');
    setRole('viewer');
    setSubmitted(false);
  };

  const mutation = useMutation({
    mutationFn: createUser,
    onSuccess: async () => {
      // Invalidate the whole user-administration subtree rather than one page:
      // a new account changes the total and may land on any page, and nothing
      // here knows which page the operator is looking at.
      await queryClient.invalidateQueries({ queryKey: queryKeys.session.users() });
      // The audit log gained an entry too, and an operator who switches to it
      // should not see a stale list.
      await queryClient.invalidateQueries({ queryKey: queryKeys.session.audit() });
      reset();
      onOpenChange(false);
    },
  });

  const usernameMissing = submitted && username.trim() === '';
  const passwordMissing = submitted && password === '';
  const mismatch = submitted && confirmation !== password;

  const handleSubmit = async (event: { preventDefault: () => void }) => {
    event.preventDefault();
    setSubmitted(true);

    if (username.trim() === '' || password === '' || confirmation !== password) return;
    if (inFlight.current) return;
    inFlight.current = true;

    try {
      await mutation.mutateAsync({
        username: username.trim(),
        password,
        // An empty box means "no display name". Sent as null rather than
        // omitted so the intent is explicit; the server maps blank onto null
        // either way.
        display_name: displayName.trim() === '' ? null : displayName.trim(),
        role,
      });
    } catch {
      // The mutation holds the typed error; `MutationError` renders it. The
      // passwords are cleared below so a failed attempt does not leave them
      // in a live component tree.
      setPassword('');
      setConfirmation('');
      setSubmitted(false);
    } finally {
      inFlight.current = false;
    }
  };

  // A duplicate username is the one failure that belongs against a field
  // rather than at the top of the form.
  const takenUsername =
    isApiError(mutation.error) && mutation.error.code === 'username_taken'
      ? mutation.error.message
      : null;

  return (
    <Modal
      open={open}
      onOpenChange={(next) => {
        // Discard whatever was typed when the dialog closes, including the
        // passwords. Keeping the form populated across an open/close cycle
        // would leave a password in state indefinitely.
        if (!next) {
          reset();
          mutation.reset();
        }
        onOpenChange(next);
      }}
      title="Create a user account"
      description="The account can sign in immediately with the password you set here. Tell its owner the password yourself — it is never shown again."
      footer={
        <>
          <Button
            variant="ghost"
            onClick={() => {
              onOpenChange(false);
            }}
            disabled={mutation.isPending}
          >
            Cancel
          </Button>
          <Button
            type="submit"
            form={`${usernameId}-form`}
            variant="primary"
            disabled={mutation.isPending}
          >
            {mutation.isPending ? <Spinner /> : <UserPlus />}
            {mutation.isPending ? 'Creating…' : 'Create account'}
          </Button>
        </>
      }
    >
      <MutationError error={takenUsername ? null : mutation.error} />

      <form
        id={`${usernameId}-form`}
        onSubmit={(event) => {
          void handleSubmit(event);
        }}
        className="flex flex-col gap-4"
        noValidate
      >
        <Field
          id={usernameId}
          label="Username"
          hint="Used to sign in. Case-insensitive, and cannot be changed afterwards."
          error={usernameMissing ? 'Enter a username.' : takenUsername}
        >
          {(controlProps) => (
            <Input
              {...controlProps}
              name="username"
              value={username}
              onChange={(event) => {
                setUsername(event.target.value);
              }}
              autoComplete="off"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
              required
              disabled={mutation.isPending}
            />
          )}
        </Field>

        <Field
          id={displayNameId}
          label="Display name (optional)"
          hint="How the account is shown in the interface. This one can be changed later."
        >
          {(controlProps) => (
            <Input
              {...controlProps}
              name="display_name"
              value={displayName}
              onChange={(event) => {
                setDisplayName(event.target.value);
              }}
              autoComplete="off"
              disabled={mutation.isPending}
            />
          )}
        </Field>

        <Field
          id={passwordId}
          label="Password"
          hint="At least 8 characters. No composition rules."
          error={passwordMissing ? 'Enter a password.' : null}
        >
          {(controlProps) => (
            <Input
              {...controlProps}
              name="password"
              type="password"
              value={password}
              onChange={(event) => {
                setPassword(event.target.value);
              }}
              // `new-password`, so a password manager offers to generate one
              // rather than filling the administrator's own credential.
              autoComplete="new-password"
              required
              disabled={mutation.isPending}
            />
          )}
        </Field>

        <Field
          id={confirmationId}
          label="Confirm password"
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

        <Field
          id={roleId}
          label="Role"
          hint={ROLE_DESCRIPTIONS[role]}
        >
          {(controlProps) => (
            <Select
              {...controlProps}
              name="role"
              value={role}
              onChange={(event) => {
                setRole(event.target.value as UserRole);
              }}
              disabled={mutation.isPending}
            >
              {ROLES.map((candidate) => (
                <option key={candidate} value={candidate}>
                  {ROLE_LABELS[candidate]}
                </option>
              ))}
            </Select>
          )}
        </Field>
      </form>
    </Modal>
  );
}
