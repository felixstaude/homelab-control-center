/**
 * First-run setup page.
 *
 * Shown when `/setup/status` reports an uninitialized installation.
 *
 * ## It instructs; it does not initialize
 *
 * There is no form here, and that is the design rather than an omission.
 * Creating the first administrator requires access to the controller's
 * database, proved by running a local command (ADR-033). An HTTP form would
 * hand ownership of the deployment to whoever reached the port first, and on a
 * homelab that race is real — the control plane is usually reachable on the
 * LAN before the operator has opened the browser.
 *
 * So this page explains what to run and where, and watches for the
 * installation to become initialized. The milestone brief is explicit that a
 * weaker alternative must not be offered for convenience; this is what
 * "integrate with the actual contract" looks like when the contract is
 * deliberately not an endpoint.
 *
 * Nothing secret is shown or requested. The command contains no token, and the
 * page never asks the operator to paste a credential into the browser.
 */

import { CheckCircle2, RefreshCw, Terminal } from 'lucide-react';
import { useState } from 'react';
import { Link } from 'react-router-dom';

import { useAuth } from '@/auth/use-auth';
import { Brand } from '@/components/layout/brand';
import { Alert } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Separator } from '@/components/ui/separator';
import { Spinner } from '@/components/ui/spinner';

/**
 * Deployment-specific forms of the same command.
 *
 * Both are shown because the right one depends on how the operator installed
 * the control plane, and guessing wrong wastes their time. The container form
 * is listed first: it is the documented deployment target (`ARCHITECTURE` §12).
 */
const COMMANDS: { label: string; hint: string; command: string }[] = [
  {
    label: 'Container deployment',
    hint: 'Run inside the running control-plane container.',
    command: 'docker compose exec -it hcc-server hcc bootstrap-admin --username admin',
  },
  {
    label: 'From a source checkout',
    hint: 'Run from the repository root.',
    command: 'make bootstrap',
  },
];

function CommandBlock({ command }: { command: string }) {
  return (
    <pre className="bg-surface-sunken border-border-subtle text-text-secondary overflow-x-auto rounded-md border px-3 py-2 font-mono text-xs">
      <code>{command}</code>
    </pre>
  );
}

export function SetupPage() {
  const { state, refresh } = useAuth();
  const [checking, setChecking] = useState(false);

  /*
   * Derived from an affirmative signal, never from "not setup_required".
   * Negating one status silently treats `checking` and `unavailable` as
   * success — which is how an unreachable backend came to display "Setup
   * complete". The gate above now resolves both of those before this renders,
   * and stating the condition positively means a future state added to the
   * machine defaults to the instructions rather than to a claim of success.
   */
  const initialized = state.status === 'unauthenticated' || state.status === 'authenticated';

  const handleRecheck = async () => {
    setChecking(true);
    try {
      await refresh();
    } finally {
      setChecking(false);
    }
  };

  return (
    <div className="bg-surface-base flex min-h-dvh flex-col items-center justify-center px-4 py-10">
      <div className="bg-grid pointer-events-none fixed inset-0 opacity-60" aria-hidden="true" />

      <div className="relative w-full max-w-xl">
        <div className="mb-6 flex justify-center">
          <Brand />
        </div>

        <Card>
          <CardContent className="pt-5">
            {initialized ? (
              /*
               * Reached when setup completed — here, or in another browser, or
               * in a second tab. No restart is involved: the status query is
               * re-read and this page becomes a link onward.
               */
              <>
                <div className="text-status-ok flex items-center gap-2">
                  <CheckCircle2 className="size-4" aria-hidden="true" />
                  <h1 className="text-sm font-semibold">Setup complete</h1>
                </div>
                <p className="text-text-muted mt-2 text-xs">
                  An administrator account exists. You can sign in now.
                </p>
                <Button asChild variant="primary" size="lg" className="mt-5 w-full">
                  <Link to="/login">Continue to sign in</Link>
                </Button>
              </>
            ) : (
              <>
                <h1 className="text-text-primary text-base font-semibold">
                  Finish setting up this installation
                </h1>
                <p className="text-text-muted mt-1.5 text-xs leading-relaxed">
                  This control plane has no administrator yet. Create one by running a
                  command on the host, then return here.
                </p>

                <Alert variant="info" title="Why not a form on this page?" className="mt-4">
                  Creating the first administrator requires access to this server, not
                  just access to this page. A setup form reachable over the network would
                  let anyone who found it first claim the installation.
                </Alert>

                <Separator className="my-5" />

                <div className="flex flex-col gap-4">
                  {COMMANDS.map((entry) => (
                    <div key={entry.label}>
                      <div className="mb-1.5 flex items-center gap-2">
                        <Terminal className="text-text-faint size-3.5" aria-hidden="true" />
                        <span className="text-text-secondary text-xs font-medium">
                          {entry.label}
                        </span>
                      </div>
                      <CommandBlock command={entry.command} />
                      <p className="text-text-faint mt-1 text-[11px]">{entry.hint}</p>
                    </div>
                  ))}
                </div>

                <p className="text-text-faint mt-4 text-[11px] leading-relaxed">
                  The command prompts for a password twice and never accepts one as an
                  argument. It can only be run once per installation.
                </p>

                <Separator className="my-5" />

                <Button
                  onClick={() => {
                    void handleRecheck();
                  }}
                  variant="secondary"
                  size="lg"
                  disabled={checking}
                  className="w-full"
                >
                  {checking ? (
                    <>
                      <Spinner />
                      Checking…
                    </>
                  ) : (
                    <>
                      <RefreshCw />
                      I have created the administrator
                    </>
                  )}
                </Button>
              </>
            )}
          </CardContent>
        </Card>

        <p className="text-text-faint mt-5 text-center text-[11px] leading-relaxed">
          See <span className="font-mono">docs/DEVELOPMENT.md</span> for the full
          first-run walkthrough.
        </p>
      </div>
    </div>
  );
}
