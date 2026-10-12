import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { LogOut, MonitorOff } from 'lucide-react';
import { useState } from 'react';

import { fetchOwnSessions, revokeOtherSessions, revokeOwnSession } from '@/api/account';
import type { OwnSession } from '@/api/types';
import {
  EmptyPanel,
  ErrorPanel,
  LoadingRows,
  MutationError,
} from '@/components/states/panel-states';
import { Alert } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Modal } from '@/components/ui/modal';
import { Spinner } from '@/components/ui/spinner';
import { formatDateTime, machineDateTime } from '@/lib/datetime';
import { queryKeys } from '@/lib/query-keys';

/**
 * Your sessions.
 *
 * ## What is shown, and what is deliberately not invented
 *
 * Creation time, last activity, expiry, and the client address recorded at
 * sign-in. The `User-Agent` is shown **verbatim** when the server has one, in
 * a monospace line, and as "Not recorded" when it does not.
 *
 * It is deliberately not parsed into "Chrome on macOS". That label would be a
 * guess presented as a fact, and it would be wrong exactly for the clients
 * worth noticing — a scripted client, an unusual browser, a forged header.
 * There is likewise no location: the server stores an IP and nothing else, and
 * turning an address into a city would mean either a third-party lookup
 * (ADR-009 forbids the external request) or a fabrication.
 *
 * ## Revoking the current session
 *
 * Allowed, and it is a sign-out. The client learns on its next request, which
 * answers 401, and the authentication state already turns that into the login
 * page with an explanation — so there is nothing special to do here beyond
 * saying what the button will do.
 */
export function SettingsSessions() {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState<'others' | OwnSession | null>(null);

  const sessions = useQuery({
    queryKey: queryKeys.session.accountSessions(),
    queryFn: ({ signal }) => fetchOwnSessions(signal),
    // Short, because a session can be revoked elsewhere at any moment and a
    // list that claims otherwise is the thing this screen exists to prevent.
    staleTime: 15_000,
  });

  const invalidate = async () => {
    await queryClient.invalidateQueries({ queryKey: queryKeys.session.accountSessions() });
    // An administrator's own user row shows a session count, so the user list
    // is stale too if one happens to be open.
    await queryClient.invalidateQueries({ queryKey: queryKeys.session.users() });
  };

  const revokeOne = useMutation({
    mutationFn: (sessionId: string) => revokeOwnSession(sessionId),
    onSuccess: async () => {
      await invalidate();
      setConfirming(null);
    },
  });

  const revokeRest = useMutation({
    mutationFn: revokeOtherSessions,
    onSuccess: async () => {
      await invalidate();
      setConfirming(null);
    },
  });

  const others = sessions.data
    ? sessions.data.items.filter((session) => !session.is_current).length
    : 0;

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <CardHeader>
          <div className="min-w-0">
            <CardTitle>Active sessions</CardTitle>
            <CardDescription>
              Every session that would currently sign you in. Expired and already-revoked
              sessions are not listed — they would not work, so showing them would bury the
              ones that do.
            </CardDescription>
          </div>
          <Button
            variant="secondary"
            size="sm"
            disabled={others === 0 || revokeRest.isPending}
            onClick={() => {
              setConfirming('others');
            }}
          >
            <MonitorOff />
            Sign out elsewhere
          </Button>
        </CardHeader>

        <CardContent className="px-0 pb-0">
          <div className="px-5">
            <MutationError error={revokeOne.error ?? revokeRest.error} />
          </div>

          {sessions.isPending ? (
            <LoadingRows rows={2} columns={3} />
          ) : sessions.isError ? (
            <ErrorPanel
              error={sessions.error}
              title="Could not load your sessions"
              onRetry={() => {
                void sessions.refetch();
              }}
            />
          ) : sessions.data.items.length === 0 ? (
            /*
             * Unreachable in practice — reading this page requires a live
             * session, so there is always at least one. Rendered rather than
             * omitted because "unreachable" plus a list index is how a blank
             * screen happens, and a stated empty state is cheaper than finding
             * out which assumption broke.
             */
            <EmptyPanel title="No active sessions">
              Your session may have just ended. Reload the page.
            </EmptyPanel>
          ) : (
            <ul className="divide-border-subtle divide-y">
              {sessions.data.items.map((session) => (
                <li key={session.session_id} className="px-5 py-4">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        {session.is_current ? (
                          <Badge variant="accent">This session</Badge>
                        ) : (
                          <Badge variant="neutral">Other session</Badge>
                        )}
                        {session.created_ip ? (
                          <span className="text-text-secondary font-mono text-xs">
                            {session.created_ip}
                          </span>
                        ) : (
                          <span className="text-text-faint text-xs">
                            Address not recorded
                          </span>
                        )}
                      </div>

                      <dl className="text-text-muted mt-2 grid gap-x-6 gap-y-1 text-xs sm:grid-cols-2">
                        <div className="flex gap-2">
                          <dt>Signed in</dt>
                          <dd className="text-text-secondary">
                            <time dateTime={machineDateTime(session.created_at)}>
                              {formatDateTime(session.created_at)}
                            </time>
                          </dd>
                        </div>
                        <div className="flex gap-2">
                          <dt>Last used</dt>
                          <dd className="text-text-secondary">
                            <time dateTime={machineDateTime(session.last_seen_at)}>
                              {formatDateTime(session.last_seen_at)}
                            </time>
                          </dd>
                        </div>
                        <div className="flex gap-2">
                          <dt>Expires</dt>
                          <dd className="text-text-secondary">
                            <time dateTime={machineDateTime(session.expires_at)}>
                              {formatDateTime(session.expires_at)}
                            </time>
                          </dd>
                        </div>
                      </dl>

                      {/*
                       * The raw header, unparsed. See the module docstring on
                       * why this is not turned into a browser name.
                       */}
                      <p className="text-text-faint mt-2 max-w-xl font-mono text-[11px] break-all">
                        {session.user_agent ?? 'User agent not recorded'}
                      </p>
                    </div>

                    <Button
                      variant="outline"
                      size="sm"
                      disabled={revokeOne.isPending}
                      onClick={() => {
                        setConfirming(session);
                      }}
                      aria-label={
                        session.is_current
                          ? 'Sign out of this session'
                          : `Revoke the session signed in at ${formatDateTime(session.created_at)}`
                      }
                    >
                      <LogOut />
                      {session.is_current ? 'Sign out' : 'Revoke'}
                    </Button>
                  </div>
                </li>
              ))}
            </ul>
          )}

          <div className="border-border-subtle border-t px-5 py-3">
            <p className="text-text-faint text-xs leading-relaxed">
              Sessions are held on the server, so revoking one takes effect on its next
              request rather than depending on the browser cooperating. Each also expires on
              its own — absolutely, and after a period of inactivity.
            </p>
          </div>
        </CardContent>
      </Card>

      {/* --- Confirmations --- */}

      {confirming === 'others' ? (
        <Modal
          open
          onOpenChange={(open) => {
            if (!open) {
              setConfirming(null);
              revokeRest.reset();
            }
          }}
          width="sm"
          title="Sign out everywhere else"
          description={`Ends ${others === 1 ? 'the other session' : `all ${others} other sessions`} for your account. This one keeps working.`}
          footer={
            <>
              <Button
                variant="ghost"
                onClick={() => {
                  setConfirming(null);
                }}
                disabled={revokeRest.isPending}
              >
                Cancel
              </Button>
              <Button
                variant="destructive"
                disabled={revokeRest.isPending}
                onClick={() => {
                  revokeRest.mutate();
                }}
              >
                {revokeRest.isPending ? <Spinner /> : null}
                {revokeRest.isPending ? 'Signing out…' : 'Sign out elsewhere'}
              </Button>
            </>
          }
        >
          <MutationError error={revokeRest.error} />
          <Alert variant="warn" title="Those sessions end immediately">
            Anyone using them has to sign in again. Your password is unchanged — use Change
            password if you think it is known to somebody else.
          </Alert>
        </Modal>
      ) : null}

      {confirming !== null && confirming !== 'others' ? (
        <Modal
          open
          onOpenChange={(open) => {
            if (!open) {
              setConfirming(null);
              revokeOne.reset();
            }
          }}
          width="sm"
          title={confirming.is_current ? 'Sign out of this session' : 'Revoke this session'}
          description={
            confirming.is_current
              ? 'Ends the session you are using right now.'
              : `Ends the session signed in on ${formatDateTime(confirming.created_at)}.`
          }
          footer={
            <>
              <Button
                variant="ghost"
                onClick={() => {
                  setConfirming(null);
                }}
                disabled={revokeOne.isPending}
              >
                Cancel
              </Button>
              <Button
                variant="destructive"
                disabled={revokeOne.isPending}
                onClick={() => {
                  revokeOne.mutate(confirming.session_id);
                }}
              >
                {revokeOne.isPending ? <Spinner /> : null}
                {revokeOne.isPending
                  ? 'Revoking…'
                  : confirming.is_current
                    ? 'Sign out'
                    : 'Revoke session'}
              </Button>
            </>
          }
        >
          <MutationError error={revokeOne.error} />
          {confirming.is_current ? (
            <Alert variant="critical" title="You will be signed out">
              You will be returned to the sign-in page and will need your password to come
              back.
            </Alert>
          ) : (
            <Alert variant="warn" title="That session ends immediately">
              Whoever is using it has to sign in again. This one is unaffected.
            </Alert>
          )}
        </Modal>
      ) : null}
    </div>
  );
}
