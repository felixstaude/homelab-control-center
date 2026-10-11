import { NavLink } from 'react-router-dom';

import { NAV_ITEMS } from '@/components/layout/navigation';
import { cn } from '@/lib/utils';

export type SidebarNavProps = {
  /** Called after a navigation, so the mobile drawer can close itself. */
  onNavigate?: () => void;
};

/**
 * Primary navigation list.
 *
 * Shared by the desktop sidebar and the mobile drawer. Uses `NavLink` so the
 * active state comes from the router rather than from manually comparing
 * `location.pathname`, and sets `aria-current="page"` for screen readers.
 */
export function SidebarNav({ onNavigate }: SidebarNavProps) {
  return (
    <nav aria-label="Primary" className="flex flex-col gap-0.5">
      {NAV_ITEMS.map((item) => {
        const Icon = item.icon;
        return (
          <NavLink
            key={item.to}
            to={item.to}
            // `end` only for the index route, so "/" is not marked active on
            // every child path.
            end={item.to === '/'}
            onClick={onNavigate}
            className={({ isActive }) =>
              cn(
                'group relative flex items-center gap-3 rounded-lg px-3 py-2',
                'text-sm font-medium transition-colors duration-150',
                'outline-none focus-visible:outline-2 focus-visible:outline-offset-2',
                'focus-visible:outline-(--color-focus-ring)',
                isActive
                  ? 'bg-surface-raised text-text-primary'
                  : 'text-text-muted hover:bg-surface-raised/60 hover:text-text-secondary',
              )
            }
          >
            {({ isActive }) => (
              <>
                {/* Active marker: a left rail rather than a background-only
                 * change, so the active item is distinguishable without
                 * relying on a subtle contrast difference. */}
                <span
                  aria-hidden="true"
                  className={cn(
                    'absolute top-1/2 left-0 h-5 w-0.5 -translate-y-1/2 rounded-r-full transition-all',
                    isActive ? 'bg-accent opacity-100' : 'opacity-0',
                  )}
                />
                <Icon
                  className={cn(
                    'size-4 shrink-0 transition-colors',
                    isActive ? 'text-accent' : 'text-text-faint group-hover:text-text-muted',
                  )}
                  aria-hidden="true"
                />
                <span className="truncate">{item.label}</span>
              </>
            )}
          </NavLink>
        );
      })}
    </nav>
  );
}
