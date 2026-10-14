#!/usr/bin/env bash
#
# Restore a Homelab Control Center backup.
#
# Usage:
#   ./scripts/restore.sh --from hcc-20261014T000000Z.dump
#   ./scripts/restore.sh --from … --database-url postgresql://…
#   ./scripts/restore.sh --from … --container hcc-postgres
#   ./scripts/restore.sh --from … --dry-run
#
# # This destroys data, and says so before it does
#
# A restore replaces the contents of the target database. Every row written
# since the backup was taken is lost, which for this application means every
# measurement collected since then. So:
#
#   * `--dry-run` reports what it would do and does nothing.
#   * A non-empty target database is refused unless `--force` is given.
#   * The dump's digest is verified against its manifest, if the manifest is
#     beside it, **before** anything is dropped.
#
# # What a restore does not bring back
#
# The database password and your `.env`, neither of which is in the dump —
# and neither of which a restore needs if you are restoring into the stack
# that generated them. The agent credentials on your hosts are also not in
# it, and do not need to be: the dump restores their *digests*, so every
# agent reconnects with the credential it already holds. No re-enrolment.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DUMP=""
DATABASE_URL=""
CONTAINER=""
DATABASE_NAME="hcc"
DATABASE_USER="hcc"
PROJECT="hcc"
COMPOSE_FILE="$REPO_ROOT/deploy/production/compose.yaml"
DRY_RUN=0
FORCE=0

green() { printf '\033[0;32m%s\033[0m\n' "$1"; }
red()   { printf '\033[0;31m%s\033[0m\n' "$1" >&2; }
info()  { printf '\n\033[0;36m%s\033[0m\n' "$1"; }
dim()   { printf '\033[0;90m%s\033[0m\n' "$1"; }
warn()  { printf '\033[0;33m%s\033[0m\n' "$1"; }

die() { red "$1"; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --from) DUMP="${2:?--from needs a path}"; shift 2 ;;
    --database-url) DATABASE_URL="${2:?--database-url needs a value}"; shift 2 ;;
    # A PostgreSQL container not managed by this Compose file. `pg_restore` is
    # frequently not installed on the controller's host: the database lives in
    # a container, and so does its client.
    --container) CONTAINER="${2:?--container needs a name}"; shift 2 ;;
    --database) DATABASE_NAME="${2:?--database needs a name}"; shift 2 ;;
    --user) DATABASE_USER="${2:?--user needs a name}"; shift 2 ;;
    --project) PROJECT="${2:?--project needs a value}"; shift 2 ;;
    --compose-file) COMPOSE_FILE="${2:?--compose-file needs a path}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --force) FORCE=1; shift ;;
    -h|--help) sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[ -n "$DUMP" ] || die "--from is required: the dump to restore"
[ -f "$DUMP" ] || die "no such file: $DUMP"

info "== 1. The dump =="

if ! head -c 5 "$DUMP" | grep -q "PGDMP"; then
  die "$DUMP is not a PostgreSQL custom-format dump (no PGDMP header)"
fi
green "  ✓ a PostgreSQL custom-format dump"

# The manifest, if it is beside the dump. Verified *before* anything is
# dropped: a corrupted dump discovered halfway through a restore leaves a
# database that is neither the old one nor the new one.
MANIFEST="${DUMP%.dump}.manifest.txt"
if [ -f "$MANIFEST" ]; then
  expected="$(grep -oE '^sha256 +[0-9a-f]{64}' "$MANIFEST" | awk '{print $2}' || true)"
  if [ -n "$expected" ]; then
    actual="$(sha256sum "$DUMP" | cut -d' ' -f1)"
    if [ "$actual" = "$expected" ]; then
      green "  ✓ matches the digest in $(basename "$MANIFEST")"
    else
      die "the dump does not match its manifest:
  expected  $expected
  actual    $actual

Nothing has been changed. The file is corrupt or truncated — fetch it again."
    fi
  else
    warn "  ! $(basename "$MANIFEST") carries no digest; the dump cannot be verified"
  fi
else
  warn "  ! no manifest beside the dump; its integrity cannot be verified"
  dim "    scripts/backup.sh writes one. A dump without it still restores."
fi

# ---------------------------------------------------------------------------
# How to reach the database
# ---------------------------------------------------------------------------
if [ -n "$CONTAINER" ]; then
  RUNTIME=""
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    RUNTIME="docker"
  elif command -v podman >/dev/null 2>&1; then
    RUNTIME="podman"
  else
    die "no container runtime found"
  fi
  run_psql() {
    $RUNTIME exec -i "$CONTAINER" psql --username="$DATABASE_USER" \
      --quiet --tuples-only --no-align "$DATABASE_NAME" -c "$1"
  }
  run_restore() {
    $RUNTIME exec -i "$CONTAINER" pg_restore --username="$DATABASE_USER" \
      --no-owner --no-privileges --dbname="$DATABASE_NAME" < "$DUMP"
  }
  TARGET="container $CONTAINER, database $DATABASE_NAME"
elif [ -n "$DATABASE_URL" ]; then
  command -v pg_restore >/dev/null 2>&1 \
    || die "pg_restore is not installed. Use --container NAME to run it inside \
the database's own container, or install postgresql-client."
  run_psql() { psql --no-password --quiet --tuples-only --no-align "$DATABASE_URL" -c "$1"; }
  run_restore() {
    pg_restore --no-password --no-owner --no-privileges --dbname="$DATABASE_URL" "$DUMP"
  }
  TARGET="$(printf '%s' "$DATABASE_URL" | sed 's|://[^@]*@|://<redacted>@|')"
else
  COMPOSE=""
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
  elif command -v podman-compose >/dev/null 2>&1; then
    COMPOSE="podman-compose"
  else
    die "no Compose implementation found; pass --database-url instead"
  fi
  [ -f "$COMPOSE_FILE" ] || die "no Compose file at $COMPOSE_FILE"
  # shellcheck disable=SC2086
  run_psql() {
    $COMPOSE -p "$PROJECT" -f "$COMPOSE_FILE" exec -T postgres \
      psql --username=hcc --quiet --tuples-only --no-align hcc -c "$1"
  }
  # shellcheck disable=SC2086
  run_restore() {
    $COMPOSE -p "$PROJECT" -f "$COMPOSE_FILE" exec -T postgres \
      pg_restore --username=hcc --no-owner --no-privileges --dbname=hcc < "$DUMP"
  }
  TARGET="compose project $PROJECT, service postgres, database hcc"
fi

info "== 2. The target =="
dim "  $TARGET"

if ! tables="$(run_psql "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'" 2>/dev/null | tr -d '[:space:]')"; then
  die "cannot reach the target database. Is the stack running?"
fi
green "  ✓ reachable ($tables tables in the public schema)"

if [ "${tables:-0}" -gt 0 ] && [ "$FORCE" = 0 ] && [ "$DRY_RUN" = 0 ]; then
  users="$(run_psql "SELECT count(*) FROM users" 2>/dev/null | tr -d '[:space:]' || echo "?")"
  samples="$(run_psql "SELECT count(*) FROM metric_samples_raw" 2>/dev/null | tr -d '[:space:]' || echo "?")"
  die "the target database is not empty: $tables tables, $users users, $samples raw samples.

Restoring replaces all of it, and every measurement collected since the
backup was taken is lost. Pass --force if that is what you intend.

To see what would happen without doing it:
  $0 --from $DUMP --dry-run"
fi

# ---------------------------------------------------------------------------
if [ "$DRY_RUN" = 1 ]; then
  info "== Would do, in order =="
  if [ "${tables:-0}" -gt 0 ]; then
    echo "  1. DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
    echo "     (not pg_restore --clean: this schema is partitioned, and --clean"
    echo "      cannot drop a partition's inherited constraints)"
    echo "  2. pg_restore --no-owner --no-privileges"
  else
    echo "  1. pg_restore --no-owner --no-privileges"
  fi
  echo "     from $DUMP into:"
  echo "     $TARGET"
  echo "  3. Report the row counts afterwards"
  echo
  if [ "${tables:-0}" -gt 0 ]; then
    warn "The target has $tables tables and they would be dropped and replaced."
  else
    dim "The target is empty, so nothing would be lost."
  fi
  echo
  dim "The database password and your .env are not in the dump and are not"
  dim "touched. Agent credentials on your hosts are not either — their digests"
  dim "are restored, so every agent reconnects with the credential it holds."
  echo
  green "Nothing above has been done."
  exit 0
fi

info "== 3. Restoring =="

# ## Why the schema is dropped rather than pg_restore --clean
#
# `pg_restore --clean --if-exists` is the obvious choice and does not work on
# this schema. The metric tables are **partitioned**, and `--clean` emits a
# `DROP CONSTRAINT` per partition which PostgreSQL refuses:
#
#   ERROR: cannot drop inherited constraint "metric_samples_raw_default_pkey"
#          of relation "metric_samples_raw_default"
#
# Found by running it, against a real dump of a real schema with real
# partitions. Six errors, and a database left neither old nor new.
#
# `DROP SCHEMA public CASCADE` is the answer: PostgreSQL resolves the whole
# dependency graph itself, partitions included, and the restore then runs into
# an empty schema — which is also the only state a restore is actually
# specified for.
if [ "${tables:-0}" -gt 0 ]; then
  dim "  dropping the existing schema (partitioned tables cannot be dropped piecemeal)"
  if ! run_psql "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" >/dev/null 2>&1; then
    die "could not drop the existing schema; nothing has been restored"
  fi
  green "  ✓ the target schema is empty"
fi

# `--no-owner --no-privileges` so a dump taken as one role restores as
# another, which is what happens whenever the stack regenerates its password.
#
# pg_restore exits non-zero for warnings as well as errors, so the output is
# kept and inspected rather than the exit code trusted alone.
restore_log="$(mktemp)"
trap 'rm -f "$restore_log"' EXIT

if run_restore >"$restore_log" 2>&1; then
  green "  ✓ pg_restore completed"
else
  if grep -qiE "error:" "$restore_log"; then
    red "--- pg_restore ---"
    grep -iE "error:" "$restore_log" | head -20
    die "the restore reported errors; the database is in an unknown state"
  fi
  # Warnings only. Reported rather than hidden, because an operator should see
  # them once — and the row counts below are the evidence either way.
  green "  ✓ pg_restore completed with warnings"
  dim "  $(grep -icE "warning" "$restore_log" || true) warning(s)"
fi

info "== 4. What came back =="

for query in \
  "users:SELECT count(*) FROM users" \
  "hosts:SELECT count(*) FROM hosts" \
  "agent credentials:SELECT count(*) FROM agent_credentials" \
  "monitoring revisions:SELECT count(*) FROM host_config_revisions" \
  "audit events:SELECT count(*) FROM audit_events" \
  "raw samples:SELECT count(*) FROM metric_samples_raw"
do
  label="${query%%:*}"
  sql="${query#*:}"
  value="$(run_psql "$sql" 2>/dev/null | tr -d '[:space:]' || echo "?")"
  printf '  %-22s %s\n' "$label" "$value"
done

echo
green "✓ restored from $(basename "$DUMP")"
dim "Restart the control plane so it reconnects to the restored schema:"
dim "  docker compose -p $PROJECT restart hcc"
echo
dim "Your agents will reconnect on their own with the credentials they already"
dim "hold — their digests came back with the dump, so no host needs re-enrolling."
