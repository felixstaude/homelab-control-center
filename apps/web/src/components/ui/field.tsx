import type { ComponentProps, ReactNode } from 'react';

import { cn } from '@/lib/utils';

/**
 * A labelled form field with optional hint and error text.
 *
 * Composed as one component rather than loose `Label` / `Input` / `<p>` parts
 * because the wiring between them is the part that is easy to get wrong and
 * invisible when it is: `htmlFor`, `aria-describedby`, `aria-invalid` and
 * `role="alert"` all have to agree, and every field needs the same agreement.
 * Doing it once means a screen reader announces the label, then the hint, then
 * the error — and announces a *new* error when one appears, rather than
 * leaving it as text only a sighted user notices.
 *
 * The caller renders the control via a function so it can be an `Input`, a
 * password field with a toggle, or anything else, while this component still
 * owns the identifiers.
 */

export function Label({ className, ...props }: ComponentProps<'label'>) {
  return (
    // `htmlFor` is supplied by the caller — `Field` below always passes it,
    // wired to the same id it gives the control. The rule cannot see across
    // that boundary from this generic wrapper.
    // eslint-disable-next-line jsx-a11y/label-has-associated-control
    <label
      className={cn('text-text-secondary text-xs font-medium', className)}
      {...props}
    />
  );
}

export type FieldProps = {
  /** Stable id; the control, label and messages are wired from it. */
  id: string;
  label: string;
  /** Static guidance, always announced. */
  hint?: string | undefined;
  /** Validation message. Its presence marks the control invalid. */
  error?: string | null | undefined;
  /** Renders the control with the accessibility attributes already resolved. */
  children: (controlProps: {
    id: string;
    'aria-invalid': boolean;
    'aria-describedby': string | undefined;
  }) => ReactNode;
  className?: string | undefined;
};

export function Field({ id, label, hint, error, children, className }: FieldProps) {
  const hintId = hint ? `${id}-hint` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  // Hint first, then error: that is the order they should be read in.
  const describedBy = [hintId, errorId].filter(Boolean).join(' ') || undefined;

  return (
    <div className={cn('flex flex-col gap-1.5', className)}>
      <Label htmlFor={id}>{label}</Label>

      {children({
        id,
        'aria-invalid': Boolean(error),
        'aria-describedby': describedBy,
      })}

      {hint ? (
        <p id={hintId} className="text-text-faint text-xs">
          {hint}
        </p>
      ) : null}

      {error ? (
        // `role="alert"` so the message is announced when it appears, not only
        // when the field is next focused.
        <p id={errorId} role="alert" className="text-status-critical text-xs">
          {error}
        </p>
      ) : null}
    </div>
  );
}
