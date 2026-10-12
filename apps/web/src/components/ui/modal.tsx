import * as Dialog from '@radix-ui/react-dialog';
import { X } from 'lucide-react';
import type { ReactNode } from 'react';

import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

/**
 * Modal dialog.
 *
 * Built on Radix's dialog, which the app already depends on for the mobile
 * navigation drawer. The parts worth not hand-rolling are the ones that are
 * invisible when wrong: the focus trap, restoring focus to whatever opened the
 * dialog, `aria-modal` and the labelled-by wiring, Escape to close, and
 * scroll locking. A dialog that loses focus to the page behind it is unusable
 * with a keyboard and perfectly fine with a mouse, so it is exactly the defect
 * that ships.
 *
 * Named `Modal` rather than `Dialog` so the Radix namespace import stays
 * readable at the call sites that also use `Dialog.Close`.
 *
 * ## Why the title is required
 *
 * `title` is not optional, and Radix is the reason: a dialog with no
 * accessible name is announced as just "dialog". Making it a required prop
 * means a nameless one cannot be written by omission.
 */
export function Modal({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
  width = 'md',
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  /** Optional sub-heading, wired to `aria-describedby`. */
  description?: ReactNode;
  children: ReactNode;
  /** Actions, right-aligned on a divided footer. */
  footer?: ReactNode;
  width?: 'sm' | 'md' | 'lg';
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-black/70 backdrop-blur-sm" />
        <Dialog.Content
          className={cn(
            'bg-surface-raised border-border-default shadow-overlay',
            'fixed top-1/2 left-1/2 z-50 w-[calc(100vw-2rem)] -translate-x-1/2 -translate-y-1/2',
            'rounded-xl border',
            // The content area scrolls rather than the dialog growing past the
            // viewport, so a long form stays reachable on a short window.
            'max-h-[calc(100dvh-4rem)] overflow-y-auto',
            {
              sm: 'max-w-sm',
              md: 'max-w-lg',
              lg: 'max-w-2xl',
            }[width],
          )}
        >
          <div className="border-border-subtle flex items-start justify-between gap-4 border-b px-5 py-4">
            <div className="min-w-0">
              <Dialog.Title className="text-text-primary text-sm font-semibold">
                {title}
              </Dialog.Title>
              {description ? (
                <Dialog.Description className="text-text-muted mt-1.5 text-xs leading-relaxed">
                  {description}
                </Dialog.Description>
              ) : null}
            </div>
            <Dialog.Close asChild>
              <Button variant="ghost" size="icon-sm" aria-label="Close">
                <X />
              </Button>
            </Dialog.Close>
          </div>

          <div className="px-5 py-4">{children}</div>

          {footer ? (
            <div className="border-border-subtle flex items-center justify-end gap-2 border-t px-5 py-3">
              {footer}
            </div>
          ) : null}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
