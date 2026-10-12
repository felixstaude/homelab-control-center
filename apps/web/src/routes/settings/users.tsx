import { useQuery } from '@tanstack/react-query';
import { LogOut, Pencil, Search, UserPlus, UserX, UserCheck } from 'lucide-react';
import { useId, useState } from 'react';

import {
  deactivateUser,
  fetchUsers,
  reactivateUser,
  revokeUserSessions,
} from '@/api/users';
import { userLabel, type ManagedUser, type UserRole } from '@/api/types';
import { ROLE_LABELS } from '@/auth/permissions';
import { useAuth, usePermissions } from '@/auth/use-auth';
import {
  EmptyPanel,
  ErrorPanel,
  LoadingRows,
  PermissionRequired,
} from '@/components/states/panel-states';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Pagination } from '@/components/ui/pagination';
import { Select } from '@/components/ui/select';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableHeaderRow,
  TableRow,
  TableRowHeader,
} from '@/components/ui/table';
import { formatDate, formatDateTime, machineDateTime } from '@/lib/datetime';
import { queryKeys } from '@/lib/query-keys';
import { ConfirmActionDialog } from '@/routes/settings/users/confirm-action-dialog';
import { CreateUserDialog } from '@/routes/settings/users/create-user-dialog';
import { EditUserDialog } from '@/routes/settings/users/edit-user-dialog';
import { RoleBadge, SessionCount, StatusBadge } from '@/routes/settings/users/user-badges';

/**
 * User administration.
 *
 * ## Filters live in component state, not the URL
 *
 * A deliberate split from the section navigation, which *is* in the URL. A
 * section is a place worth linking to and returning to; a search term halfway
 * through typing is not, and putting it in the URL would push a history entry
 * per keystroke unless debounced — at which point the Back button walks
 * backwards through someone's typing.
 *
 * ## Which action goes where
 *
 * The row offers four. Editing a display name or role is a form, so it opens
 * one. Deactivating, reactivating and revoking sessions all take effect
 * immediately on somebody else's session, so each is confirmed with a sentence
 * saying what will happen. They are not bundled into the edit form, because an
 * access change should not be something that happens while correcting a typo.
 */

const PAGE_SIZE = 25;

/** "Any" is the absence of a filter, spelled explicitly so the key is stable. */
const ANY = '';

type RoleFilter = UserRole | typeof ANY;
type StatusFilter = 'active' | 'inactive' | typeof ANY;

/** Which confirmation is open, and for whom. */
type PendingAction =
  | { kind: 'deactivate'; user: ManagedUser }
  | { kind: 'reactivate'; user: ManagedUser }
  | { kind: 'revoke-sessions'; user: ManagedUser }
  | null;

export function SettingsUsers() {
  const { state } = useAuth();
  const permissions = usePermissions();

  const [search, setSearch] = useState('');
  const [roleFilter, setRoleFilter] = useState<RoleFilter>(ANY);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>(ANY);
  const [offset, setOffset] = useState(0);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<ManagedUser | null>(null);
  const [pending, setPending] = useState<PendingAction>(null);
  /** Result of the last revocation, so the outcome is reported honestly. */
  const [lastRevocation, setLastRevocation] = useState<{
    label: string;
    count: number;
  } | null>(null);

  const searchId = useId();
  const roleId = useId();
  const statusId = useId();

  const isActive =
    statusFilter === ANY ? undefined : statusFilter === 'active' ? true : false;

  const users = useQuery({
    queryKey: queryKeys.session.userList({
      search,
      role: roleFilter,
      status: statusFilter,
      limit: PAGE_SIZE,
      offset,
    }),
    queryFn: ({ signal }) =>
      fetchUsers(
        {
          search: search || undefined,
          role: roleFilter || undefined,
          isActive,
          limit: PAGE_SIZE,
          offset,
        },
        signal,
      ),
    // The previous page stays on screen while the next loads, so changing a
    // filter does not blank the table and jump the layout.
    placeholderData: (previous) => previous,
    // Not retried past the client's default: a 403 here is answered by the
    // guard below rather than by trying again.
    enabled: permissions.administer,
  });

  // Direct navigation by a non-administrator. The server refuses the data
  // regardless; this explains why the screen is empty instead of showing a
  // failed request.
  if (!permissions.administer) {
    return (
      <PermissionRequired
        required="admin"
        actual={state.status === 'authenticated' ? state.role : 'viewer'}
        what="User administration"
      />
    );
  }

  const resetToFirstPage = () => {
    setOffset(0);
  };

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <CardHeader>
          <div className="min-w-0">
            <CardTitle>User accounts</CardTitle>
            <CardDescription>
              Every account on this installation. Roles are enforced by the control plane on
              every request, so a change takes effect immediately without the account signing
              in again.
            </CardDescription>
          </div>
          <Button
            variant="primary"
            size="sm"
            onClick={() => {
              setCreating(true);
            }}
          >
            <UserPlus />
            New account
          </Button>
        </CardHeader>

        {/* --- Filters --- */}
        <div className="border-border-subtle grid gap-3 border-t px-5 py-4 sm:grid-cols-[minmax(0,1fr)_auto_auto]">
          <div>
            <label htmlFor={searchId} className="text-text-secondary text-xs font-medium">
              Search
            </label>
            <div className="relative mt-1.5">
              <Search
                aria-hidden="true"
                className="text-text-faint pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2"
              />
              <Input
                id={searchId}
                type="search"
                value={search}
                onChange={(event) => {
                  setSearch(event.target.value);
                  resetToFirstPage();
                }}
                placeholder="Username"
                autoComplete="off"
                className="pl-9"
              />
            </div>
          </div>

          <div>
            <label htmlFor={roleId} className="text-text-secondary text-xs font-medium">
              Role
            </label>
            <div className="mt-1.5 sm:w-40">
              <Select
                id={roleId}
                value={roleFilter}
                onChange={(event) => {
                  setRoleFilter(event.target.value as RoleFilter);
                  resetToFirstPage();
                }}
              >
                <option value={ANY}>Any role</option>
                <option value="viewer">{ROLE_LABELS.viewer}</option>
                <option value="operator">{ROLE_LABELS.operator}</option>
                <option value="admin">{ROLE_LABELS.admin}</option>
              </Select>
            </div>
          </div>

          <div>
            <label htmlFor={statusId} className="text-text-secondary text-xs font-medium">
              Status
            </label>
            <div className="mt-1.5 sm:w-40">
              <Select
                id={statusId}
                value={statusFilter}
                onChange={(event) => {
                  setStatusFilter(event.target.value as StatusFilter);
                  resetToFirstPage();
                }}
              >
                <option value={ANY}>Any status</option>
                <option value="active">Active</option>
                <option value="inactive">Inactive</option>
              </Select>
            </div>
          </div>
        </div>

        <CardContent className="px-0 pb-0">
          {lastRevocation ? (
            // The outcome of the last revocation, reported rather than
            // assumed. A count of zero is stated as such — claiming success
            // for a revocation that revoked nothing is the specific lie this
            // avoids.
            <div
              role="status"
              className="border-border-subtle text-text-secondary border-b px-5 py-3 text-sm"
            >
              {lastRevocation.count === 0 ? (
                <>
                  {lastRevocation.label} had no active sessions, so nothing was revoked.
                </>
              ) : (
                <>
                  Revoked {lastRevocation.count}{' '}
                  {lastRevocation.count === 1 ? 'session' : 'sessions'} for{' '}
                  {lastRevocation.label}.
                </>
              )}
            </div>
          ) : null}

          {users.isPending ? (
            <LoadingRows rows={5} columns={5} />
          ) : users.isError ? (
            <ErrorPanel
              error={users.error}
              title="Could not load the accounts"
              onRetry={() => {
                void users.refetch();
              }}
            />
          ) : users.data.items.length === 0 ? (
            <EmptyPanel
              title={
                search || roleFilter || statusFilter
                  ? 'No accounts match those filters'
                  : 'No accounts yet'
              }
              action={
                search || roleFilter || statusFilter ? (
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => {
                      setSearch('');
                      setRoleFilter(ANY);
                      setStatusFilter(ANY);
                      resetToFirstPage();
                    }}
                  >
                    Clear filters
                  </Button>
                ) : undefined
              }
            >
              {search || roleFilter || statusFilter
                ? 'Try a shorter search term, or clear the filters.'
                : 'Create one to get started.'}
            </EmptyPanel>
          ) : (
            <>
              <Table>
                <caption className="sr-only">
                  User accounts, with role, status, session count and dates.
                </caption>
                <TableHead>
                  <TableHeaderRow>
                    <TableHeader>Account</TableHeader>
                    <TableHeader>Role</TableHeader>
                    <TableHeader>Status</TableHeader>
                    <TableHeader className="text-right">Sessions</TableHeader>
                    <TableHeader className="hidden md:table-cell">Last sign-in</TableHeader>
                    <TableHeader className="hidden lg:table-cell">Created</TableHeader>
                    <TableHeader className="text-right">
                      <span className="sr-only">Actions</span>
                    </TableHeader>
                  </TableHeaderRow>
                </TableHead>
                <TableBody>
                  {users.data.items.map((user) => (
                    <UserRowView
                      key={user.user_id}
                      user={user}
                      isSelf={
                        state.status === 'authenticated' &&
                        state.user.user_id === user.user_id
                      }
                      onEdit={() => {
                        setEditing(user);
                      }}
                      onAction={(kind) => {
                        setLastRevocation(null);
                        setPending({ kind, user });
                      }}
                    />
                  ))}
                </TableBody>
              </Table>

              <Pagination
                total={users.data.total}
                limit={users.data.limit}
                offset={users.data.offset}
                onOffsetChange={setOffset}
                busy={users.isFetching}
                label="accounts"
              />
            </>
          )}
        </CardContent>
      </Card>

      <CreateUserDialog open={creating} onOpenChange={setCreating} />

      <EditUserDialog
        user={editing}
        onOpenChange={(open) => {
          if (!open) setEditing(null);
        }}
      />

      {pending ? (
        <ActionConfirmation
          pending={pending}
          onClose={() => {
            setPending(null);
          }}
          onRevoked={(count) => {
            setLastRevocation({ label: userLabel(pending.user), count });
          }}
        />
      ) : null}
    </div>
  );
}

/** One account's row. */
function UserRowView({
  user,
  isSelf,
  onEdit,
  onAction,
}: {
  user: ManagedUser;
  isSelf: boolean;
  onEdit: () => void;
  onAction: (kind: 'deactivate' | 'reactivate' | 'revoke-sessions') => void;
}) {
  return (
    <TableRow>
      <TableRowHeader>
        <span className="text-text-primary block font-medium">{userLabel(user)}</span>
        {/*
         * The username is shown beneath the label whenever they differ, so the
         * thing the account signs in with is never hidden behind a nickname —
         * an administrator checking who has access needs the login name.
         */}
        {user.display_name === null ? null : (
          <span className="text-text-faint mt-0.5 block font-mono text-xs">
            {user.username}
          </span>
        )}
        {isSelf ? (
          <span className="text-accent mt-0.5 block text-xs">This is you</span>
        ) : null}
      </TableRowHeader>

      <TableCell>
        <RoleBadge role={user.role} />
      </TableCell>

      <TableCell>
        <StatusBadge isActive={user.is_active} />
      </TableCell>

      <TableCell className="text-right">
        <SessionCount count={user.active_session_count} />
      </TableCell>

      <TableCell className="text-text-muted hidden md:table-cell">
        <time dateTime={machineDateTime(user.last_login_at)}>
          {formatDateTime(user.last_login_at)}
        </time>
      </TableCell>

      <TableCell className="text-text-muted hidden lg:table-cell">
        <time dateTime={machineDateTime(user.created_at)}>{formatDate(user.created_at)}</time>
      </TableCell>

      <TableCell className="text-right">
        <div className="flex items-center justify-end gap-1">
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={onEdit}
            // The accessible name carries the account, so a screen reader
            // user hears which row's button they are on rather than six
            // identical "Edit" buttons.
            aria-label={`Edit ${userLabel(user)}`}
          >
            <Pencil />
          </Button>

          <Button
            variant="ghost"
            size="icon-sm"
            onClick={() => {
              onAction('revoke-sessions');
            }}
            disabled={user.active_session_count === 0}
            aria-label={`Revoke sessions for ${userLabel(user)}`}
          >
            <LogOut />
          </Button>

          {user.is_active ? (
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={() => {
                onAction('deactivate');
              }}
              aria-label={`Deactivate ${userLabel(user)}`}
            >
              <UserX />
            </Button>
          ) : (
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={() => {
                onAction('reactivate');
              }}
              aria-label={`Reactivate ${userLabel(user)}`}
            >
              <UserCheck />
            </Button>
          )}
        </div>
      </TableCell>
    </TableRow>
  );
}

/**
 * The confirmation for whichever row action is pending.
 *
 * One component per action would mean three copies of the dialog plumbing;
 * one component with a switch over the action keeps the *wording* — which is
 * the part that matters — in one readable place.
 */
function ActionConfirmation({
  pending,
  onClose,
  onRevoked,
}: {
  pending: NonNullable<PendingAction>;
  onClose: () => void;
  onRevoked: (count: number) => void;
}) {
  const label = userLabel(pending.user);

  if (pending.kind === 'revoke-sessions') {
    return (
      <ConfirmActionDialog
        open
        onOpenChange={(open) => {
          if (!open) onClose();
        }}
        title={`Revoke sessions for ${label}`}
        description="Ends every session this account currently has, wherever it is signed in."
        warning={{
          tone: 'warn',
          title: 'They will be signed out immediately',
          body: 'The account itself is unchanged and can sign in again straight away. Use this when a session may have been taken over; use Deactivate if the person should no longer have access at all.',
        }}
        confirmLabel="Revoke sessions"
        pendingLabel="Revoking…"
        variant="destructive"
        action={() => revokeUserSessions(pending.user.user_id)}
        onDone={(result) => {
          const count = (result as { sessions_revoked?: number }).sessions_revoked;
          onRevoked(typeof count === 'number' ? count : 0);
        }}
      />
    );
  }

  if (pending.kind === 'deactivate') {
    return (
      <ConfirmActionDialog
        open
        onOpenChange={(open) => {
          if (!open) onClose();
        }}
        title={`Deactivate ${label}`}
        description="Removes this account's access without deleting it."
        warning={{
          tone: 'critical',
          title: 'Access ends immediately',
          body: 'Existing sessions stop working on their next request and the account can no longer sign in. Nothing is deleted — its history stays intact and it can be reactivated later.',
        }}
        confirmLabel="Deactivate account"
        pendingLabel="Deactivating…"
        variant="destructive"
        action={() => deactivateUser(pending.user.user_id)}
      />
    );
  }

  return (
    <ConfirmActionDialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      title={`Reactivate ${label}`}
      description="Restores this account's ability to sign in."
      warning={{
        tone: 'warn',
        title: 'Old sessions stay revoked',
        body: 'The account signs in again to get a new session. Sessions ended by the deactivation are not restored — a revoked token stays revoked.',
      }}
      confirmLabel="Reactivate account"
      pendingLabel="Reactivating…"
      action={() => reactivateUser(pending.user.user_id)}
    />
  );
}
