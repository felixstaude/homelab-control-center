import { useMutation, useQueryClient } from '@tanstack/react-query';
import type { ReactNode } from 'react';

import { isApiError } from '@/api/errors';
import { MutationError } from '@/components/states/panel-states';
import { Alert } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Modal } from '@/components/ui/modal';
import { Spinner } from '@/components/ui/spinner';
import { queryKeys } from '@/lib/query-keys';

/**
 * Confirmation for an action that removes or invalidates access.
 *
 * Deactivation, reactivation and session revocation all go through this. They
 * share a shape — one button, one irreversible-ish effect, one explanation of
 * what will happen — and writing three dialogs would mean three chances for
 * one of them to forget the explanation.
 *
 * ## Why these are confirmed at all
 *
 * Not because they are destructive in the "deletes data" sense — none of them
 * is. Because they take effect **immediately and remotely**: deactivating an
 * account signs its owner out mid-task, and revoking sessions does the same
 * without even changing anything an administrator can see afterwards. An
 * action whose consequence happens to somebody else deserves a sentence
 * describing it.
 *
 * ## Why no optimistic update
 *
 * The list is refetched after success rather than patched in place. These are
 * security-relevant state changes, and an optimistic update that had to be
 * rolled back would show an account as deactivated when it is not — which is
 * the single most misleading thing this screen could display. A round trip is
 * cheap; being wrong about who has access is not.
 */
export function ConfirmActionDialog({
  open,
  onOpenChange,
  title,
  description,
  warning,
  confirmLabel,
  pendingLabel,
  variant = 'primary',
  action,
  onDone,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: ReactNode;
  /** Shown as an alert above the buttons. The consequence, in one line. */
  warning?: { tone: 'warn' | 'critical'; title: string; body: ReactNode } | undefined;
  confirmLabel: string;
  pendingLabel: string;
  variant?: 'primary' | 'destructive';
  action: () => Promise<unknown>;
  /** Called with the mutation's result after a successful run. */
  onDone?: (result: unknown) => void;
}) {
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: action,
    onSuccess: async (result) => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.session.users() });
      await queryClient.invalidateQueries({ queryKey: queryKeys.session.audit() });
      // The acting administrator may have deactivated or revoked their own
      // account, in which case their cached identity is now wrong.
      await queryClient.invalidateQueries({ queryKey: queryKeys.session.currentUser() });
      await queryClient.invalidateQueries({ queryKey: queryKeys.session.accountSessions() });
      onDone?.(result);
      onOpenChange(false);
    },
  });

  const lastAdministrator =
    isApiError(mutation.error) && mutation.error.code === 'last_administrator'
      ? mutation.error.message
      : null;

  return (
    <Modal
      open={open}
      onOpenChange={(next) => {
        if (!next) mutation.reset();
        onOpenChange(next);
      }}
      width="sm"
      title={title}
      description={description}
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
            variant={variant}
            onClick={() => {
              mutation.mutate();
            }}
            disabled={mutation.isPending}
          >
            {mutation.isPending ? <Spinner /> : null}
            {mutation.isPending ? pendingLabel : confirmLabel}
          </Button>
        </>
      }
    >
      {lastAdministrator ? (
        <Alert variant="critical" title="This is the only active administrator">
          {lastAdministrator}
        </Alert>
      ) : (
        <>
          <MutationError error={mutation.error} />
          {warning ? (
            <Alert variant={warning.tone} title={warning.title}>
              {warning.body}
            </Alert>
          ) : null}
        </>
      )}
    </Modal>
  );
}
