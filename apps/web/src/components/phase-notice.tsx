import { Info } from 'lucide-react';

import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import { Badge } from '@/components/ui/badge';

/**
 * Header badge stating the project's current phase.
 *
 * Engineering rule 8: the shell must not look like a working product when no
 * monitoring exists yet. This is a persistent, honest marker rather than a
 * detail buried in a README.
 */
export function PhaseNotice() {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Badge variant="accent" className="cursor-help">
          <Info className="size-3" aria-hidden="true" />
          <span className="hidden sm:inline">Phase 1</span>
          <span className="sm:hidden">P1</span>
        </Badge>
      </TooltipTrigger>
      <TooltipContent>
        Identity, in progress. The shell, design system, authentication and
        user management are in place and real — the data is not. No agents,
        metrics, logs or notifications are implemented yet.
      </TooltipContent>
    </Tooltip>
  );
}
