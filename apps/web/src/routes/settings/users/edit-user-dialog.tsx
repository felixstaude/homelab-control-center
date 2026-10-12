import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Save } from 'lucide-react';
import { useId, useState } from 'react';

import { isApiError } from '@/api/errors';
import { updateUser } from '@/api/users';
import { userLabel, type ManagedUser, type UpdateUserRequest, type UserRole } from '@/api/types';
import { ROLE_DESCRIPTIONS, ROLE_LABELS } from '@/auth/permissions';
import { MutationError } from '@/components/states/panel-states';
import { Alert } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Field } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import { Modal } from '@/components/ui/modal';
import { Select } from '@/components/ui/select';
import { Spinner } from '@/components/ui/spinner';
import { queryKeys } from '@/lib/query-keys';

/**
 * Edit a user's display name and role.
 *
 * Activation is **not** here, and that is deliberate: deactivating an account
 * removes someone's access, which is a decision rather than a field edit, so
 * it has its own confirmed action in the row menu. A checkbox tucked into a
 * form is how an access change gets made by accident while correcting a typo.
 *
 * The username is shown and not editable. Usernames are immutable (ADR-041),
 * and showing it read-only answers "which account am I editing" better than
 * omitting it would.
 *
 * ## Only what changed is sent
 *
 * The request carries just the fields that differ from the loaded account. A
 * `PATCH` that always sent everything would work — the server accepts it — but
 * it would record a role-change audit event for a role that did not change,
 * and an audit trail full of no-op entries is one nobody reads.
 */

const ROLES: readonly UserRole[] = ['viewer', 'operator', 'admin'];

export function EditUserDialog({
  user,
  onOpenChange,
}: {
  /** The account being edited, or `null` when the dialog is closed. */
  user: ManagedUser | null;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const formId = useId();
  const displayNameId = useId();
  const roleId = useId();

  /*
   * Keyed on the account, so opening the dialog for a different user resets
   * the fields.
   *
   * React remounts a component when its `key` changes, which is the cleanest
   * way to reset form state to new props — the alternative is an effect that
   * copies props into state, which fires on every render where the props look
   * different and is a well-known source of a form that fights the user as
   * they type.
   */
  if (user === null) return null;
  return (
    <EditUserForm
      key={user.user_id}
      user={user}
      formId={formId}
      displayNameId={displayNameId}
      roleId={roleId}
      onOpenChange={onOpenChange}
      queryClientInvalidate={async () => {
        await queryClient.invalidateQueries({ queryKey: queryKeys.session.users() });
        await queryClient.invalidateQueries({ queryKey: queryKeys.session.audit() });
        // The editing administrator may have just changed *their own* role, in
        // which case the cached identity is stale and the interface would keep
        // offering controls the server now refuses.
        await queryClient.invalidateQueries({
          queryKey: queryKeys.session.currentUser(),
        });
      }}
    />
  );
}

function EditUserForm({
  user,
  formId,
  displayNameId,
  roleId,
  onOpenChange,
  queryClientInvalidate,
}: {
  user: ManagedUser;
  formId: string;
  displayNameId: string;
  roleId: string;
  onOpenChange: (open: boolean) => void;
  queryClientInvalidate: () => Promise<void>;
}) {
  const [displayName, setDisplayName] = useState(user.display_name ?? '');
  const [role, setRole] = useState<UserRole>(user.role);

  const mutation = useMutation({
    mutationFn: (body: UpdateUserRequest) => updateUser(user.user_id, body),
    onSuccess: async () => {
      await queryClientInvalidate();
      onOpenChange(false);
    },
  });

  const trimmed = displayName.trim();
  const nextDisplayName = trimmed === '' ? null : trimmed;
  const displayNameChanged = nextDisplayName !== user.display_name;
  const roleChanged = role !== user.role;
  const nothingChanged = !displayNameChanged && !roleChanged;

  /**
   * The last-administrator refusal, surfaced against the role field.
   *
   * The server answers 409 with its own code so the interface can explain
   * *this* refusal rather than a generic conflict — the remedy is "promote
   * someone else first", which an operator cannot guess from "conflict".
   */
  const lastAdministrator =
    isApiError(mutation.error) && mutation.error.code === 'last_administrator'
      ? mutation.error.message
      : null;

  const handleSubmit = (event: { preventDefault: () => void }) => {
    event.preventDefault();
    if (nothingChanged || mutation.isPending) return;

    const body: UpdateUserRequest = {};
    if (displayNameChanged) body.display_name = nextDisplayName;
    if (roleChanged) body.role = role;
    mutation.mutate(body);
  };

  return (
    <Modal
      open
      onOpenChange={(next) => {
        if (!next) mutation.reset();
        onOpenChange(next);
      }}
      title={`Edit ${userLabel(user)}`}
      description={
        <>
          Signs in as <span className="text-text-secondary font-mono">{user.username}</span>.
          The username cannot be changed.
        </>
      }
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
            form={formId}
            variant="primary"
            disabled={mutation.isPending || nothingChanged}
          >
            {mutation.isPending ? <Spinner /> : <Save />}
            {mutation.isPending ? 'Saving…' : 'Save changes'}
          </Button>
        </>
      }
    >
      <MutationError error={lastAdministrator ? null : mutation.error} />

      <form
        id={formId}
        onSubmit={handleSubmit}
        className="flex flex-col gap-4"
        noValidate
      >
        <Field
          id={displayNameId}
          label="Display name"
          hint="Leave blank to show the username instead."
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

        <Field id={roleId} label="Role" hint={ROLE_DESCRIPTIONS[role]} error={lastAdministrator}>
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

        {roleChanged ? (
          <Alert variant="warn" title="This changes what the account may do">
            {role === 'admin'
              ? 'Administrators can manage every account, including yours, and read the audit trail.'
              : 'The change takes effect on this account’s next request. Any open session keeps working, with the new permissions.'}
          </Alert>
        ) : null}
      </form>
    </Modal>
  );
}
