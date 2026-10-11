import type { ComponentProps } from 'react';

import { cn } from '@/lib/utils';

export type SeparatorProps = ComponentProps<'div'> & {
  orientation?: 'horizontal' | 'vertical';
};

export function Separator({
  className,
  orientation = 'horizontal',
  ...props
}: SeparatorProps) {
  return (
    <div
      // Purely visual: announced as a separator so structure is conveyed
      // without implying interactive content.
      role="separator"
      aria-orientation={orientation}
      className={cn(
        'bg-border-subtle shrink-0',
        orientation === 'horizontal' ? 'h-px w-full' : 'h-full w-px',
        className,
      )}
      {...props}
    />
  );
}
