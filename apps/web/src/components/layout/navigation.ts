import {
  Bell,
  Boxes,
  LayoutDashboard,
  Network,
  RefreshCw,
  ScrollText,
  Server,
  Settings,
  type LucideIcon,
} from 'lucide-react';

export type NavItem = {
  /** Route path, also used as the React key. */
  to: string;
  label: string;
  icon: LucideIcon;
  /** Short description, shown in the mobile drawer and as a tooltip. */
  description: string;
};

/**
 * The application's primary navigation.
 *
 * Single source of truth: the sidebar, the mobile drawer and the router all
 * derive from this array, so a new section cannot appear in one place and be
 * missing from another.
 *
 * Order reflects an operator's triage path — overview first, then the layers
 * they drill into, with configuration last.
 */
export const NAV_ITEMS: readonly NavItem[] = [
  {
    to: '/',
    label: 'Overview',
    icon: LayoutDashboard,
    description: 'Fleet-wide health at a glance',
  },
  {
    to: '/hosts',
    label: 'Hosts',
    icon: Server,
    description: 'Linux servers and their resources',
  },
  {
    to: '/docker',
    label: 'Docker',
    icon: Boxes,
    description: 'Containers and compose projects',
  },
  {
    to: '/logs',
    label: 'Logs',
    icon: ScrollText,
    description: 'Search and stream collected logs',
  },
  {
    to: '/network',
    label: 'Network',
    icon: Network,
    description: 'Device reachability and topology',
  },
  {
    to: '/alerts',
    label: 'Alerts',
    icon: Bell,
    description: 'Notification rules and history',
  },
  {
    to: '/updates',
    label: 'Updates',
    icon: RefreshCw,
    description: 'Agent versions, policies and rollouts',
  },
  {
    to: '/settings',
    label: 'Settings',
    icon: Settings,
    description: 'Agents, integrations and access',
  },
] as const;
