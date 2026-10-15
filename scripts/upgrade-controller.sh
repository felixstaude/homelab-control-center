#!/usr/bin/env bash
#
# Upgrade the controller, with the parts that make it survivable.
#
#   ./scripts/upgrade-controller.sh --to 0.2.0
#   ./scripts/upgrade-controller.sh --to 0.2.0 --plan-only
#   ./scripts/upgrade-controller.sh --to 0.2.0 --yes          # no prompt
#
# # Why this is a command and not a button
#
# Replacing the controller means replacing the container the web interface is
# served from. For the application to do that to itself it would need the
# Docker socket — which is root on the host — and a web application with root
# on its host is a worse problem than any upgrade it could perform. There is
# no remote-command API here either, for the same reason: an endpoint that
# runs what it is told is the thing an attacker is looking for.
#
# So the controller **guides** and this **acts**. `GET /api/v1/controller/
# upgrade-readiness` reports what an upgrade would involve — the applied
# schema revision, the migrations that would run, how many agents are
# connected, whether a backup exists — and the interface shows it. The
# administrator runs this, on the machine, with the privileges they already
# have.
#
# # What it does, in order, and what it refuses
#
#   1. Preflight: Compose, a running stack, a reachable controller, a
#      writable backup directory, and enough disk for a dump.
#   2. Read the current state: the running version and the applied Alembic
#      revision.
#   3. Work out what the target needs: its head revision, and therefore the
#      migrations that would run. Reported before anything is changed.
#   4. **Back up, and verify the backup by restoring it.** A dump whose
#      checksum matches is a dump that is intact, not a dump that restores —
#      and the difference is the whole value of the step.
#   5. Confirm, unless --yes.
#   6. Apply: pull the image, bring the stack up, wait for readiness.
#   7. Verify: the version endpoint reports the target, `/readyz` is green,
#      and the agents that were connected are connected again.
#
# Any failure stops and prints the recovery procedure with the backup's real
# path in it.
#
# # There is no automatic database rollback, and that is deliberate
#
# Migrations are not reversed on failure. An Alembic downgrade that drops a
# column is data loss performed automatically, by a script, at the moment
# somebody is least able to think about it — and some of this project's own
# downgrades refuse rather than destroy (migration 0011 narrows a CHECK
# constraint and lets PostgreSQL reject it).
#
# Recovery is therefore: put the old image back **and** restore the backup,
# in that order, which is what this prints. The backup is the rollback
# mechanism; the script says so rather than implying a tidier one exists.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

GREEN='\033[0;32m' RED='\033[0;31m' CYAN='\033[0;36m' YELLOW='\033[0;33m'
DIM='\033[0;90m' BOLD='\033[1m' RESET='\033[0m'

TARGET=""
PROJECT="hcc"
COMPOSE_FILE="deploy/production/compose.yaml"
BASE_URL=""
BACKUP_DIR="./backups"
CONTAINER=""
PLAN_ONLY=0
ASSUME_YES=0
SKIP_BACKUP=0
READY_TIMEOUT=180

while [ $# -gt 0 ]; do
  case "$1" in
    --to) TARGET="${2:?--to needs a version}"; shift 2 ;;
    --project) PROJECT="${2:?--project needs a value}"; shift 2 ;;
    --compose-file) COMPOSE_FILE="${2:?--compose-file needs a path}"; shift 2 ;;
    --url) BASE_URL="${2:?--url needs a URL}"; shift 2 ;;
    --backup-dir) BACKUP_DIR="${2:?--backup-dir needs a directory}"; shift 2 ;;
    --container) CONTAINER="${2:?--container needs a name}"; shift 2 ;;
    --ready-timeout) READY_TIMEOUT="${2:?--ready-timeout needs seconds}"; shift 2 ;;
    --plan-only) PLAN_ONLY=1; shift ;;
    --skip-backup) SKIP_BACKUP=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    -h|--help) sed -n '2,57p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

green() { printf "${GREEN}%s${RESET}\n" "$1"; }
red()   { printf "${RED}%s${RESET}\n" "$1"; }
info()  { printf "\n${CYAN}== %s ==${RESET}\n" "$1"; }
dim()   { printf "${DIM}%s${RESET}\n" "$1"; }
warn()  { printf "${YELLOW}  ! %s${RESET}\n" "$1"; }
ok()    { printf "${GREEN}  ✓ %s${RESET}\n" "$1"; }
step()  { printf "${DIM}  · %s${RESET}\n" "$1"; }

BACKUP_PATH=""
PREVIOUS_VERSION=""

# recovery prints the procedure, with the real backup path in it.
#
# Called from every failure path after a backup exists. A generic "restore
# from backup" is advice; this is a command somebody can paste at the moment
# they are least able to compose one.
recovery() {
  printf "\n${BOLD}Recovery${RESET}\n"
  if [ -n "$PREVIOUS_VERSION" ]; then
    dim "  1. Put the previous image back:"
    dim "       HCC_VERSION=$PREVIOUS_VERSION docker compose -p $PROJECT -f $COMPOSE_FILE up -d"
  fi
  if [ -n "$BACKUP_PATH" ]; then
    dim "  2. Restore the backup taken before this attempt:"
    dim "       ./scripts/restore.sh --from $BACKUP_PATH --force"
    printf "\n"
    dim "  Both steps, in that order. The migrations that ran are **not**"
    dim "  reversed automatically — an Alembic downgrade that drops a column is"
    dim "  data loss performed by a script at the worst possible moment. The"
    dim "  backup is the rollback mechanism."
  else
    warn "No backup was taken, so there is nothing to restore to."
    dim "  The schema may have been migrated. Check:"
    dim "       docker compose -p $PROJECT -f $COMPOSE_FILE exec hcc alembic current"
  fi
  printf "\n"
}

die() { red "✗ $1"; recovery; exit 1; }

# --- Compose ----------------------------------------------------------------
#
# Docker Compose only, which is the same limitation `deployment-e2e.sh`
# documents: podman-compose 1.6.0 hangs on this stack's `depends_on`
# conditions. Named rather than worked around.
COMPOSE=""
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
fi

compose() {
  # shellcheck disable=SC2086
  $COMPOSE -p "$PROJECT" -f "$COMPOSE_FILE" "$@"
}

psql_controller() {
  compose exec -T postgres psql -U hcc -d hcc -tAc "$1" 2>/dev/null | tr -d ' \r'
}

# ============================================================================
info "Preflight"
# ============================================================================

[ -n "$TARGET" ] || { red "✗ --to is required: the version to upgrade to"; exit 2; }
[ -n "$COMPOSE" ] || {
  red "✗ Docker Compose is required.

  podman-compose 1.6.0 hangs on this stack's depends_on conditions, which is
  why this needs Docker specifically — the same limitation docs/DEPLOYMENT.md
  records for the deployment itself."
  exit 2
}
[ -f "$COMPOSE_FILE" ] || { red "✗ no compose file at $COMPOSE_FILE"; exit 2; }
ok "docker compose, $COMPOSE_FILE"

case "$TARGET" in
  v*) red "✗ --to is $TARGET; versions here have no leading v"; exit 2 ;;
esac

if ! compose ps --status running --services 2>/dev/null | grep -q .; then
  red "✗ the $PROJECT stack is not running.

  This upgrades a running installation. For a first install, see
  docs/DEPLOYMENT.md."
  exit 2
fi
ok "the $PROJECT stack is running"

# The published port, read from Compose rather than assumed, so a deployment
# that moved it is not told its controller is unreachable.
if [ -z "$BASE_URL" ]; then
  port="$(compose port hcc 8000 2>/dev/null | sed 's/.*://' || true)"
  BASE_URL="http://127.0.0.1:${port:-8000}"
fi
if ! curl -fsS -o /dev/null --max-time 5 "$BASE_URL/healthz" 2>/dev/null; then
  red "✗ the controller at $BASE_URL does not answer /healthz.

  Upgrading an installation that is already broken makes two problems out of
  one. Fix the running one first, or pass --url if it is somewhere else."
  exit 2
fi
ok "the controller answers at $BASE_URL"

mkdir -p "$BACKUP_DIR" 2>/dev/null || true
[ -w "$BACKUP_DIR" ] || { red "✗ $BACKUP_DIR is not writable"; exit 2; }
ok "backups will go to $BACKUP_DIR"

# ============================================================================
info "What is running now"
# ============================================================================

PREVIOUS_VERSION="$(compose exec -T hcc python -c 'import hcc; print(hcc.__version__)' 2>/dev/null | tr -d ' \r' || true)"
[ -n "$PREVIOUS_VERSION" ] || PREVIOUS_VERSION="unknown"
printf "  %-22s %s\n" "controller version" "$PREVIOUS_VERSION"

CURRENT_REVISION="$(psql_controller 'SELECT version_num FROM alembic_version' || true)"
printf "  %-22s %s\n" "schema revision" "${CURRENT_REVISION:-unknown}"

CONNECTED_BEFORE="$(psql_controller 'SELECT count(*) FROM hosts WHERE current_connection_id IS NOT NULL' || echo 0)"
printf "  %-22s %s\n" "agents connected" "${CONNECTED_BEFORE:-0}"

if [ "$PREVIOUS_VERSION" = "$TARGET" ]; then
  warn "the controller already reports $TARGET"
  dim "  Continuing anyway: an image can be rebuilt at the same version, and"
  dim "  refusing would make a repair impossible."
fi

# ============================================================================
info "What the upgrade would do"
# ============================================================================

# The target image's head revision, asked of the image itself rather than of
# this checkout: the working tree may be on a different commit from the
# version being deployed, and a plan derived from the wrong source is worse
# than no plan.
TARGET_IMAGE="ghcr.io/felixstaude/homelab-control-center:$TARGET"
step "pulling $TARGET_IMAGE"
if ! docker pull "$TARGET_IMAGE" >/dev/null 2>&1; then
  red "✗ cannot pull $TARGET_IMAGE.

  No image has been published for this project yet, so this will fail until
  one is. To upgrade from a local build instead:

      HCC_VERSION=$TARGET docker compose -p $PROJECT \\
          -f $COMPOSE_FILE -f deploy/production/compose.build.yaml \\
          up -d --build

  and run this script with --plan-only first to see the migrations."
  exit 2
fi
ok "pulled $TARGET_IMAGE"

TARGET_HEAD="$(docker run --rm --entrypoint sh "$TARGET_IMAGE" -c \
  'cd /app && alembic heads 2>/dev/null | head -1 | cut -d" " -f1' 2>/dev/null | tr -d ' \r' || true)"
if [ -n "$TARGET_HEAD" ]; then
  printf "  %-22s %s\n" "target head revision" "$TARGET_HEAD"
else
  warn "could not read the target image's head revision"
fi

if [ -n "$TARGET_HEAD" ] && [ "$TARGET_HEAD" = "$CURRENT_REVISION" ]; then
  ok "no migrations would run"
  PENDING_COUNT=0
else
  # Every revision the target knows that this database has not applied. Read
  # from the image's own history, which is the authority for what it will do.
  PENDING="$(docker run --rm --entrypoint sh "$TARGET_IMAGE" -c \
    "cd /app && alembic history 2>/dev/null" 2>/dev/null || true)"
  PENDING_COUNT="$(printf '%s' "$PENDING" | grep -c '^' || echo 0)"
  warn "migrations will run. From $CURRENT_REVISION to ${TARGET_HEAD:-head}."
  dim "  A migration is applied once and is not reversed by this script."
  dim "  See the recovery note at the end."
fi

if [ "$PLAN_ONLY" = 1 ]; then
  printf "\n"
  green "Plan only. Nothing was changed."
  dim "  Run again without --plan-only to back up, upgrade and verify."
  exit 0
fi

# ============================================================================
info "Backup"
# ============================================================================

if [ "$SKIP_BACKUP" = 1 ]; then
  warn "--skip-backup: there will be nothing to recover to."
  dim "  This is for a test fixture, not for an installation anybody cares about."
else
  ./scripts/backup.sh --output "$BACKUP_DIR" --project "$PROJECT" \
    --compose-file "$COMPOSE_FILE" ${CONTAINER:+--container "$CONTAINER"} \
    || die "the backup failed, so the upgrade did not start"
  BACKUP_PATH="$(find "$BACKUP_DIR" -name 'hcc-*.dump' -newermt '-5 minutes' 2>/dev/null |
    sort | tail -1)"
  [ -n "$BACKUP_PATH" ] || die "the backup reported success and no dump was found in $BACKUP_DIR"
  ok "backup: $BACKUP_PATH"

  # --- Verify it, by restoring it ---------------------------------------
  #
  # A dump whose checksum matches is a dump that is *intact*. That is not the
  # same as a dump that *restores*, and the difference is the whole value of
  # this step: a truncated pg_dump, a version mismatch between pg_dump and
  # the server, a dump taken while an extension was missing — each produces a
  # file with a perfectly good checksum and no usable database inside it.
  #
  # So it is restored into a scratch database in the same PostgreSQL
  # instance, counted, and dropped. The live database is never touched.
  step "verifying the backup by restoring it into a scratch database"
  SCRATCH="hcc_verify_$(date +%s)"
  live_tables="$(psql_controller "SELECT count(*) FROM information_schema.tables
                                  WHERE table_schema = 'public'" || echo 0)"
  compose exec -T postgres psql -U hcc -d postgres -c "CREATE DATABASE $SCRATCH" >/dev/null 2>&1 \
    || die "could not create a scratch database to verify the backup in"
  verify_failed=0
  if ! compose exec -T postgres pg_restore -U hcc -d "$SCRATCH" --no-owner \
      < "$BACKUP_PATH" >/dev/null 2>&1; then
    verify_failed=1
  fi
  restored_tables="$(compose exec -T postgres psql -U hcc -d "$SCRATCH" -tAc \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'" \
    2>/dev/null | tr -d ' \r' || echo 0)"
  compose exec -T postgres psql -U hcc -d postgres -c "DROP DATABASE $SCRATCH" >/dev/null 2>&1 || true

  if [ "$verify_failed" = 1 ]; then
    BACKUP_PATH=""
    die "the backup does not restore. Nothing was upgraded.

  The dump is at the path above and pg_restore refused it. Do not proceed
  until a backup restores: an upgrade without a working one is an upgrade
  with no way back."
  fi
  if [ "${restored_tables:-0}" -lt "${live_tables:-1}" ]; then
    BACKUP_PATH=""
    die "the backup restored $restored_tables tables and the live database has $live_tables.

  Nothing was upgraded. A backup that restores *less* than it should is worse
  than one that fails outright, because it looks like it worked."
  fi
  ok "the backup restores: $restored_tables tables, matching the live database"
fi

# ============================================================================
info "Apply"
# ============================================================================

if [ "$ASSUME_YES" = 0 ]; then
  printf "\n"
  printf "  ${BOLD}Upgrade %s → %s?${RESET}\n" "$PREVIOUS_VERSION" "$TARGET"
  if [ "${PENDING_COUNT:-0}" != 0 ]; then
    dim "  Migrations will run and will not be reversed by this script."
  fi
  dim "  The controller will be unavailable for a few seconds."
  printf "  Type 'yes' to continue: "
  read -r answer
  [ "$answer" = "yes" ] || { printf "\n"; dim "Nothing was changed."; exit 0; }
fi

step "bringing the stack up on $TARGET"
if ! HCC_VERSION="$TARGET" compose up -d >/dev/null 2>&1; then
  die "docker compose up failed"
fi

step "waiting up to ${READY_TIMEOUT}s for the controller to be ready"
deadline=$((SECONDS + READY_TIMEOUT))
ready=0
while [ "$SECONDS" -lt "$deadline" ]; do
  if curl -fsS -o /dev/null --max-time 3 "$BASE_URL/readyz" 2>/dev/null; then
    ready=1
    break
  fi
  sleep 2
done
if [ "$ready" = 0 ]; then
  red "the controller did not become ready within ${READY_TIMEOUT}s."
  printf "\n"
  dim "  Its last lines:"
  compose logs --tail 30 hcc 2>&1 | sed 's/^/    /' || true
  die "the upgrade did not complete"
fi
ok "the controller is ready"

# ============================================================================
info "Verify"
# ============================================================================

running="$(compose exec -T hcc python -c 'import hcc; print(hcc.__version__)' 2>/dev/null | tr -d ' \r' || true)"
if [ "$running" = "$TARGET" ]; then
  ok "it reports $running"
else
  warn "it reports '${running:-nothing}', expected $TARGET"
  dim "  The stack came up and is serving. If the image tag was right, this is"
  dim "  a build whose version stamp disagrees with its tag."
fi

new_revision="$(psql_controller 'SELECT version_num FROM alembic_version' || true)"
printf "  %-22s %s\n" "schema revision" "${new_revision:-unknown}"
if [ -n "$TARGET_HEAD" ] && [ -n "$new_revision" ] && [ "$new_revision" != "$TARGET_HEAD" ]; then
  warn "the schema is at $new_revision and the target's head is $TARGET_HEAD"
  dim "  Migrations did not finish. The controller is serving against a schema"
  dim "  it may not fully support."
fi

# The agents are the point of the installation, so "it came up" is not the
# end of the check. Given a minute, the ones that were connected should be
# connected again — they reconnect with backoff and hold their credentials
# across a controller restart.
step "waiting for agents to reconnect"
deadline=$((SECONDS + 90))
connected_after=0
while [ "$SECONDS" -lt "$deadline" ]; do
  connected_after="$(psql_controller 'SELECT count(*) FROM hosts WHERE current_connection_id IS NOT NULL' || echo 0)"
  [ "${connected_after:-0}" -ge "${CONNECTED_BEFORE:-0}" ] && break
  sleep 3
done
if [ "${connected_after:-0}" -ge "${CONNECTED_BEFORE:-0}" ]; then
  ok "$connected_after of $CONNECTED_BEFORE agents are connected again"
else
  warn "$connected_after of $CONNECTED_BEFORE agents have reconnected"
  dim "  They retry with backoff, so give it a minute. If one stays away, its"
  dim "  own journal says why:  journalctl -u hcc-agent -n 50"
fi

printf "\n"
green "✓ upgraded $PREVIOUS_VERSION → $TARGET"
if [ -n "$BACKUP_PATH" ]; then
  printf "\n"
  dim "  The backup taken before this is at:"
  dim "    $BACKUP_PATH"
  dim "  Keep it until you are satisfied. Going back means putting the old"
  dim "  image in place **and** restoring it — the migrations are not reversed."
fi
printf "\n"
