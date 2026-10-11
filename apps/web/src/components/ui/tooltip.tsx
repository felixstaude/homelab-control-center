import * as TooltipPrimitive from '@radix-ui/react-tooltip';
import type { ComponentProps } from 'react';

import { cn } from '@/lib/utils';

/**
 * Tooltip, built on Radix so keyboard focus, escape-to-dismiss, hover intent
 * and `aria-describedby` wiring are handled rather than reimplemented.
 *
 * Tooltips carry supplementary detail only. Anything required to understand
 * the UI belongs in visible text — a tooltip is unreachable on touch devices.
 */
export const TooltipProvider = TooltipPrimitive.Provider;
export const Tooltip = TooltipPrimitive.Root;
export const TooltipTrigger = TooltipPrimitive.Trigger;

export function TooltipContent({
  className,
  sideOffset = 6,
  ...props
}: ComponentProps<typeof TooltipPrimitive.Content>) {
  return (
    <TooltipPrimitive.Portal>
      <TooltipPrimitive.Content
        sideOffset={sideOffset}
        className={cn(
          'bg-surface-overlay text-text-primary border-border-default shadow-overlay',
          'z-50 max-w-xs rounded-md border px-2.5 py-1.5 text-xs',
          'animate-in fade-in-0 zoom-in-95 data-[state=closed]:animate-out data-[state=closed]:fade-out-0',
          className,
        )}
        {...props}
      />
    </TooltipPrimitive.Portal>
  );
}
