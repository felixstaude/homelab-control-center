import { Slot } from '@radix-ui/react-slot';
import { cva, type VariantProps } from 'class-variance-authority';
import type { ComponentProps } from 'react';

import { cn } from '@/lib/utils';

/**
 * Button variants.
 *
 * Every interactive surface in the app uses this component, so hover, focus
 * and disabled behaviour are defined once. `cva` keeps the variant matrix
 * declarative rather than a thicket of ternaries at each call site.
 */
const buttonVariants = cva(
  [
    'inline-flex items-center justify-center gap-2 whitespace-nowrap',
    'rounded-md font-medium',
    'transition-colors duration-150 ease-(--ease-out-quint)',
    // Pointer events are removed when disabled so a disabled button cannot
    // receive a click through a wrapping label.
    'disabled:pointer-events-none disabled:opacity-45',
    // Icons inside buttons should never capture the pointer or stretch.
    "[&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
    'outline-none focus-visible:outline-2 focus-visible:outline-offset-2',
    'focus-visible:outline-(--color-focus-ring)',
  ],
  {
    variants: {
      variant: {
        primary: 'bg-accent text-accent-contrast hover:bg-accent-hover font-semibold',
        secondary:
          'bg-surface-raised text-text-primary border border-border-default hover:bg-surface-hover hover:border-border-strong',
        ghost: 'text-text-secondary hover:bg-surface-raised hover:text-text-primary',
        outline:
          'border border-border-default text-text-secondary hover:bg-surface-raised hover:text-text-primary',
        /* Destructive actions (stop a container, delete a host) are visually
         * distinct because they are not undoable. */
        destructive:
          'bg-status-critical-bg text-status-critical border border-status-critical-border hover:bg-status-critical hover:text-surface-base',
        link: 'text-accent underline-offset-4 hover:underline',
      },
      size: {
        sm: 'h-8 px-3 text-xs',
        md: 'h-9 px-4 text-sm',
        lg: 'h-10 px-5 text-sm',
        icon: 'size-9',
        'icon-sm': 'size-8',
      },
    },
    defaultVariants: {
      variant: 'secondary',
      size: 'md',
    },
  },
);

export type ButtonProps = ComponentProps<'button'> &
  VariantProps<typeof buttonVariants> & {
    /**
     * Render the child element instead of a `<button>`, forwarding styles and
     * behaviour. Used to style a router `<Link>` as a button without nesting
     * an anchor inside a button, which is invalid HTML.
     */
    asChild?: boolean;
  };

export function Button({
  className,
  variant,
  size,
  asChild = false,
  type,
  ...props
}: ButtonProps) {
  const Component = asChild ? Slot : 'button';

  return (
    <Component
      // Buttons inside a form default to `submit`, which causes accidental
      // submissions. Default to `button` unless a type is given explicitly.
      {...(asChild ? {} : { type: type ?? 'button' })}
      className={cn(buttonVariants({ variant, size }), className)}
      {...props}
    />
  );
}

export { buttonVariants };
