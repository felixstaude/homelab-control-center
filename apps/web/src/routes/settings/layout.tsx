import { NavLink, Outlet } from 'react-router-dom';

import { PageHeader } from '@/components/page-header';
import { usePermissions } from '@/auth/use-auth';
import { cn } from '@/lib/utils';

/**
 * Settings shell: the section navigation and the routed panel.
 *
 * ## Why nested routes rather than tab state
 *
 * Each section is a URL. That is what makes "send me the audit log" a link,
 * what makes the browser's Back button do the obvious thing between sections,
 * and what makes a reload stay where the operator was. Tab state in a
 * `useState` gives none of those and costs the same to write.
 *
 * It also keeps this file small. The alternative — one Settings component with
 * a switch over a tab index — is how a settings page becomes the largest
 * component in a codebase: every section's queries, mutations and dialogs end
 * up in one module because there is nowhere else to put them.
 *
 * ## Why the administrative sections are hidden rather than disabled
 *
 * A disabled control invites "why?", and the honest answer for a Viewer is
 * "this is not for you", which is better said by the section not being there.
 * That is a presentation decision and nothing more: the server refuses
 * `/api/v1/users` to a Viewer whatever this renders, and a Viewer who types
 * `/settings/users` gets the panel's own permission notice rather than data
 * (`docs/SECURITY.md` — the client is not an authorization boundary).
 */

type Section = {
  /** Relative path. `undefined` marks the index route. */
  to?: string;
  label: string;
  description: string;
  /** Whether the section is offered only to administrators. */
  adminOnly?: boolean;
};

const SECTIONS: readonly Section[] = [
  {
    label: 'General',
    description: 'Installation and deployment information',
  },
  {
    to: 'users',
    label: 'Users',
    description: 'Accounts, roles and access',
    adminOnly: true,
  },
  {
    to: 'account',
    label: 'My account',
    description: 'Your own details and password',
  },
  {
    to: 'sessions',
    label: 'Sessions',
    description: 'Where you are signed in',
  },
  {
    to: 'audit',
    label: 'Audit',
    description: 'Record of privileged actions',
    adminOnly: true,
  },
] as const;

export function SettingsLayout() {
  const permissions = usePermissions();
  const sections = SECTIONS.filter(
    (section) => !section.adminOnly || permissions.administer,
  );

  return (
    <>
      <PageHeader
        title="Settings"
        description="Accounts and access. Agent enrolment, notification destinations and retention policy arrive with the milestones that build them."
      />

      <div className="flex flex-col gap-6 lg:flex-row lg:gap-8">
        {/*
         * A `nav` with a list, so the sections are announced as a group and
         * their count is known before the user tabs through them.
         *
         * Horizontally scrollable below `lg` rather than wrapping: a wrapped
         * two-line tab strip pushes the panel below the fold on a phone, and
         * these labels are short enough to swipe.
         */}
        <nav
          aria-label="Settings sections"
          className="lg:w-56 lg:shrink-0"
        >
          <ul className="border-border-subtle flex gap-1 overflow-x-auto border-b pb-2 lg:flex-col lg:border-b-0 lg:pb-0">
            {sections.map((section) => (
              <li key={section.label} className="shrink-0 lg:shrink">
                <NavLink
                  to={section.to ?? '/settings'}
                  // `end` for the index route only, so "General" is not
                  // highlighted on every child path.
                  end={section.to === undefined}
                  className={({ isActive }) =>
                    cn(
                      'block rounded-lg px-3 py-2 text-sm font-medium',
                      'transition-colors duration-150',
                      'outline-none focus-visible:outline-2 focus-visible:outline-offset-2',
                      'focus-visible:outline-(--color-focus-ring)',
                      isActive
                        ? 'bg-surface-raised text-text-primary'
                        : 'text-text-muted hover:bg-surface-raised/60 hover:text-text-secondary',
                    )
                  }
                >
                  <span className="block">{section.label}</span>
                  <span className="text-text-faint hidden text-xs font-normal lg:block">
                    {section.description}
                  </span>
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>

        <div className="min-w-0 flex-1">
          <Outlet />
        </div>
      </div>
    </>
  );
}
