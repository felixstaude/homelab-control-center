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
        Identity, in progress. The shell, design system, authentication, user
        management and the host registry are in place and real — and so is the
        agent connection: an enrolled host&rsquo;s connectivity on the Hosts
        page is measured, not sampled. Metrics, logs and notifications are not
        implemented yet, and the Overview page is still fixtures behind a
        banner.
      </TooltipContent>
    </Tooltip>
  );
}
