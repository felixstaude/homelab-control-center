import { useMutation, useQueryClient } from '@tanstack/react-query';
import { ServerCog } from 'lucide-react';
import { useId, useState } from 'react';

import { createHost } from '@/api/hosts';
import type { Host } from '@/api/types';
import { MutationError } from '@/components/states/panel-states';
import { Button } from '@/components/ui/button';
import { Field } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import { Modal } from '@/components/ui/modal';
import { Spinner } from '@/components/ui/spinner';
import { queryKeys } from '@/lib/query-keys';

/**
 * Register a host.
 *
 * ## What this does *not* do
 *
 * It does not issue an enrollment credential, and that separation is the
 * server's rather than a simplification here: bundling them would mean every
 * mistyped host name also minted a live secret with a fifteen-minute life
 * somewhere in a browser. Registering a record and authorising an agent to
 * claim it are two decisions, and the dialog says so by offering the
 * enrollment step afterwards instead of silently taking it.
 *
 * ## What is validated here, and what is not
 *
 * Only that the name is non-empty. The length bounds, the Unicode
 * normalization, the whitespace collapse and the case-insensitive uniqueness
 * are all the server's, and guessing at them here would produce a form that
 * rejects a name the server would accept or accepts one it will refuse. The
 * server's own messages are shown verbatim.
 *
 * The one thing worth saying up front is that the name is a *label*: it is
 * case-insensitively unique, and it can be changed later without affecting
 * the agent, because a credential is bound to the host's id and not its name.
 */
export function CreateHostDialog({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Called with the new host, so the caller can offer enrollment next. */
  onCreated: (host: Host) => void;
}) {
  const queryClient = useQueryClient();

  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [environment, setEnvironment] = useState('');
  const [submitted, setSubmitted] = useState(false);

  const nameId = useId();
  const descriptionId = useId();
  const environmentId = useId();

  const create = useMutation({
    mutationFn: () =>
      createHost({
        name: name.trim(),
        // Omitted rather than sent empty: the server stores a whitespace-only
        // label as null, so sending "" would be asking for the same outcome
        // in a less obvious way.
        description: description.trim() || undefined,
        environment: environment.trim() || undefined,
      }),
    onSuccess: (host) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.session.hosts() });
      reset();
      onOpenChange(false);
      onCreated(host);
    },
  });

  const reset = () => {
    setName('');
    setDescription('');
    setEnvironment('');
    setSubmitted(false);
    create.reset();
  };

  const nameMissing = submitted && name.trim() === '';

  const submit = (event: React.SyntheticEvent) => {
    event.preventDefault();
    setSubmitted(true);
    if (name.trim() === '') return;
    create.mutate();
  };

  return (
    <Modal
      open={open}
      onOpenChange={(next) => {
        if (!next) reset();
        onOpenChange(next);
      }}
      title="Register a host"
      description="Creates the record. The agent is enrolled as a separate step, so a mistyped name does not mint a credential."
      footer={
        <>
          <Button
            variant="secondary"
            size="sm"
            type="button"
            onClick={() => {
              reset();
              onOpenChange(false);
            }}
          >
            Cancel
          </Button>
          <Button
            variant="primary"
            size="sm"
            type="submit"
            form="create-host-form"
            disabled={create.isPending}
          >
            {create.isPending ? <Spinner /> : <ServerCog />}
            Register host
          </Button>
        </>
      }
    >
      <form id="create-host-form" onSubmit={submit} className="flex flex-col gap-4">
        <Field
          id={nameId}
          label="Name"
          hint="Case-insensitively unique. Spaces are allowed. You can rename it later without affecting the agent."
          error={nameMissing ? 'Enter a name.' : undefined}
        >
          {(controlProps) => (
            <Input
              {...controlProps}
              name="name"
              value={name}
              onChange={(event) => {
                setName(event.target.value);
              }}
              placeholder="homeserver"
              autoComplete="off"
            />
          )}
        </Field>

        <Field
          id={descriptionId}
          label="Description (optional)"
          hint="A note for whoever reads this list next."
        >
          {(controlProps) => (
            <Input
              {...controlProps}
              name="description"
              value={description}
              onChange={(event) => {
                setDescription(event.target.value);
              }}
              placeholder="Dedicated server, Hetzner FSN1"
              autoComplete="off"
            />
          )}
        </Field>

        <Field
          id={environmentId}
          label="Environment (optional)"
          hint="A grouping label for your own use. Nothing resolves configuration against it."
        >
          {(controlProps) => (
            <Input
              {...controlProps}
              name="environment"
              value={environment}
              onChange={(event) => {
                setEnvironment(event.target.value);
              }}
              placeholder="production"
              autoComplete="off"
            />
          )}
        </Field>

        {create.isError ? <MutationError error={create.error} /> : null}
      </form>
    </Modal>
  );
}
