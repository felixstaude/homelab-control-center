import * as Dialog from '@radix-ui/react-dialog';
import { Menu, X } from 'lucide-react';
import { useState } from 'react';
import { Outlet, useLocation } from 'react-router-dom';

import { AccountMenu } from '@/components/layout/account-menu';
import { Brand } from '@/components/layout/brand';
import { NAV_ITEMS } from '@/components/layout/navigation';
import { SidebarNav } from '@/components/layout/sidebar-nav';
import { Button } from '@/components/ui/button';
import { Separator } from '@/components/ui/separator';
import { PhaseNotice } from '@/components/phase-notice';

/**
 * Application shell: persistent sidebar, header, and the routed content area.
 *
 * Responsive strategy — the sidebar is a fixed rail from `lg` upward and
 * collapses into a Radix dialog drawer below it. Radix handles the focus trap,
 * escape-to-close and `aria-modal` wiring that a hand-rolled drawer usually
 * gets wrong.
 */
export function AppShell() {
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const location = useLocation();

  const currentItem = NAV_ITEMS.find((item) =>
    item.to === '/' ? location.pathname === '/' : location.pathname.startsWith(item.to),
  );

  return (
    <div className="bg-surface-base min-h-dvh">
      {/* Keyboard users land here first and can jump past the navigation. */}
      <a
        href="#main-content"
        className="bg-accent text-accent-contrast sr-only rounded-md px-4 py-2 text-sm font-semibold focus:not-sr-only focus:absolute focus:top-3 focus:left-3 focus:z-50"
      >
        Skip to content
      </a>

      {/* --- Desktop sidebar --- */}
      <aside className="border-border-subtle bg-surface-sunken fixed inset-y-0 left-0 z-30 hidden w-60 flex-col border-r lg:flex">
        <div className="flex h-14 items-center px-4">
          <Brand />
        </div>
        <Separator />
        <div className="flex-1 overflow-y-auto px-2 py-3">
          <SidebarNav />
        </div>
        <Separator />
        <div className="px-2 py-2">
          <AccountMenu />
        </div>
      </aside>

      {/* --- Mobile drawer --- */}
      <Dialog.Root open={mobileNavOpen} onOpenChange={setMobileNavOpen}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-40 bg-black/70 backdrop-blur-sm lg:hidden" />
          <Dialog.Content className="bg-surface-sunken border-border-subtle fixed inset-y-0 left-0 z-50 flex w-72 flex-col border-r lg:hidden">
            <Dialog.Title className="sr-only">Navigation</Dialog.Title>
            <div className="flex h-14 items-center justify-between px-4">
              <Brand />
              <Dialog.Close asChild>
                <Button variant="ghost" size="icon-sm" aria-label="Close navigation">
                  <X />
                </Button>
              </Dialog.Close>
            </div>
            <Separator />
            <div className="flex-1 overflow-y-auto px-2 py-3">
              {/* Closing on navigate prevents the drawer covering the page the
               * user just asked for. */}
              <SidebarNav
                onNavigate={() => {
                  setMobileNavOpen(false);
                }}
              />
            </div>
            {/* Repeated in the drawer: without it, a phone user has no way to
             * sign out, since the desktop sidebar is hidden below `lg`. */}
            <Separator />
            <div className="px-2 py-2">
              <AccountMenu />
            </div>
          </Dialog.Content>
        </Dialog.Portal>

        {/* --- Main column --- */}
        <div className="lg:pl-60">
          <header className="border-border-subtle bg-surface-base/85 sticky top-0 z-20 border-b backdrop-blur-md">
            <div className="flex h-14 items-center gap-3 px-4 sm:px-6">
              <Dialog.Trigger asChild>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  className="lg:hidden"
                  aria-label="Open navigation"
                >
                  <Menu />
                </Button>
              </Dialog.Trigger>

              <div className="min-w-0 flex-1">
                <h1 className="text-text-primary truncate text-sm font-semibold">
                  {currentItem?.label ?? 'Not found'}
                </h1>
                {currentItem ? (
                  <p className="text-text-faint hidden truncate text-xs sm:block">
                    {currentItem.description}
                  </p>
                ) : null}
              </div>

              <PhaseNotice />
            </div>
          </header>

          {/*
           * Content width.
           *
           * Was a flat `max-w-[88rem]` (1408px) with `lg:px-8`. On a 2560px
           * display that left roughly 550px of empty margin on each side while
           * the host table scrolled horizontally — the cards were not stretched,
           * they were simply confined, and the space went unused.
           *
           * Raised to 110rem (1760px) with a wider gutter above `2xl`. The cap
           * stays rather than going fluid, and that is the trade: the content
           * is grids of cards and a data table, so at an unbounded width a
           * four-up metric row becomes four very wide tiles with their numbers
           * far apart, and a table row becomes a line the eye has to track
           * across a metre of glass. 110rem uses the space a 1440p and 4K
           * display actually have without reaching either of those.
           */}
          <main id="main-content" className="px-4 py-6 sm:px-6 lg:px-8 2xl:px-12">
            <div className="mx-auto max-w-[110rem]">
              <Outlet />
            </div>
          </main>
        </div>
      </Dialog.Root>
    </div>
  );
}
