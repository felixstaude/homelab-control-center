import { RefreshCw, ServerCrash } from 'lucide-react';
import { useState } from 'react';

import type { ApiError } from '@/api/errors';
import { Brand } from '@/components/layout/brand';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Spinner } from '@/components/ui/spinner';

/**
 * The control plane cannot be reached.
 *
 * A distinct screen, not a redirect to login. The session may be perfectly
 * valid; what has failed is the network or the server. Telling the user they
 * are signed out would be false, would discard where they were, and on a
 * flaky connection would produce a login page for no reason.
 *
 * ## Retry is manual
 *
 * There is no automatic retry loop here. The query layer already retries a
 * transient failure twice with backoff (`lib/query-client.ts`); by the time
 * this screen appears, retrying has been tried. A page that then polls
 * forever hides a real outage behind a spinner and keeps hitting a server
 * that may be struggling.
 */
export function BackendUnavailable({
  error,
  onRetry,
}: {
  error: ApiError;
  onRetry: () => void;
}) {
  const [retrying, setRetrying] = useState(false);

  const handleRetry = () => {
    setRetrying(true);
    onRetry();
    // Re-enable shortly: the parent re-renders into another state when the
    // retry resolves, and if it does not, the user can try again rather than
    // being left with a permanently disabled button.
    setTimeout(() => {
      setRetrying(false);
    }, 1200);
  };

  return (
    <div className="bg-surface-base flex min-h-dvh flex-col items-center justify-center px-4 py-10">
      <div className="relative w-full max-w-md">
        <div className="mb-6 flex justify-center">
          <Brand />
        </div>

        <Card>
          <CardContent className="pt-5">
            <div className="text-status-critical flex items-center gap-2">
              <ServerCrash className="size-4" aria-hidden="true" />
              <h1 className="text-sm font-semibold">Control plane unreachable</h1>
            </div>

            <p className="text-text-secondary mt-2 text-xs leading-relaxed">
              {/*
               * The client's fixed message for the failure kind, never the
               * browser's own exception text — which varies and can contain
               * the internal URL.
               */}
              {error.message}
            </p>

            <p className="text-text-faint mt-3 text-[11px] leading-relaxed">
              Your session has not been ended. Check that the control plane is running
              and reachable, then retry.
            </p>

            {error.requestId ? (
              <p className="text-text-faint mt-3 font-mono text-[11px]">
                Request {error.requestId}
              </p>
            ) : null}

            <Button
              onClick={handleRetry}
              variant="secondary"
              size="lg"
              disabled={retrying}
              className="mt-5 w-full"
            >
              {retrying ? (
                <>
                  <Spinner />
                  Retrying…
                </>
              ) : (
                <>
                  <RefreshCw />
                  Retry
                </>
              )}
            </Button>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
