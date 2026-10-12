import { AlertOctagon, RotateCcw } from 'lucide-react';
import { Component, type ErrorInfo, type ReactNode } from 'react';

import { Brand } from '@/components/layout/brand';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';

/**
 * Application error boundary.
 *
 * Without one, a render error anywhere unmounts the whole tree and leaves a
 * blank page — the worst possible failure for a tool whose purpose is to be
 * trusted during an incident, because it is indistinguishable from the server
 * being down.
 *
 * A class component because React provides no hook equivalent:
 * `componentDidCatch` has no functional counterpart.
 *
 * ## What it shows, and what it does not
 *
 * The message is fixed. A render error's text can contain anything that was in
 * scope — a username, a hostname, part of a response — and this screen may be
 * seen by someone who should not read it, or screenshotted into a bug report.
 * The detail belongs in the browser console, where a developer can find it and
 * a casual observer will not.
 *
 * There is no "report" button and no telemetry: nothing in this product phones
 * out (ADR-009).
 */
type Props = { children: ReactNode };
type State = { failed: boolean };

export class ErrorBoundary extends Component<Props, State> {
  override state: State = { failed: false };

  static getDerivedStateFromError(): State {
    return { failed: true };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    // The console, deliberately — see the class docstring. `console.error`
    // rather than a logger because the browser's own console is where a
    // developer looks, and shipping a log transport would be a network
    // dependency this product does not have.
    console.error('Unhandled render error', error, info.componentStack);
  }

  private readonly handleReload = () => {
    // A full reload rather than clearing the boundary's state: the tree that
    // threw may have left React state inconsistent, and "try the same broken
    // render again" is rarely what the user wants.
    window.location.reload();
  };

  override render(): ReactNode {
    if (!this.state.failed) return this.props.children;

    return (
      <div className="bg-surface-base flex min-h-dvh flex-col items-center justify-center px-4 py-10">
        <div className="w-full max-w-md">
          <div className="mb-6 flex justify-center">
            <Brand />
          </div>

          <Card>
            <CardContent className="pt-5">
              <div className="text-status-critical flex items-center gap-2">
                <AlertOctagon className="size-4" aria-hidden="true" />
                <h1 className="text-sm font-semibold">Something broke in the interface</h1>
              </div>

              <p className="text-text-secondary mt-2 text-xs leading-relaxed">
                This is a bug in the web client, not a problem with your installation.
                Your session and your data are unaffected.
              </p>

              <p className="text-text-faint mt-3 text-[11px] leading-relaxed">
                Details were written to the browser console.
              </p>

              <Button
                onClick={this.handleReload}
                variant="secondary"
                size="lg"
                className="mt-5 w-full"
              >
                <RotateCcw />
                Reload the page
              </Button>
            </CardContent>
          </Card>
        </div>
      </div>
    );
  }
}
