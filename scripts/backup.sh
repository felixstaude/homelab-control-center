#!/usr/bin/env bash
#
# Back up a Homelab Control Center installation.
#
# One command, one file, and a manifest beside it saying what is in it and
# what is not.
#
# Usage:
#   ./scripts/backup.sh                              # the Compose deployment
#   ./scripts/backup.sh --output /mnt/backups
#   ./scripts/backup.sh --database-url postgresql://… # any reachable database
#   ./scripts/backup.sh --container hcc-postgres      # a container by name
#
# # What is in the backup
#
# A `pg_dump -Fc` of the database, which is **everything**: users, password
# hashes, sessions, hosts, agent credential digests, monitoring
# configuration, the audit trail and every measurement. The control plane
# owns all state and keeps none on disk, which is an architectural property
# rather than a convenience — it is what makes a one-file backup complete.
#
# # What is NOT in the backup, and why that matters
#
#   * `deploy/production/secrets/db_password` — the generated database
#     credential. A restore into a fresh volume creates a *new* password, so
#     this is not needed; a restore into an *existing* volume needs the
#     original, and the predictable failure of not having it is a controller
#     that cannot authenticate to its own database.
#   * `.env` — the operator's own configuration. One required field.
#   * The agent credentials on the monitored hosts. Those are in
#     `/etc/hcc-agent/credential` on each machine and are not the
#     controller's to back up. A restore brings back the *digests*, so the
#     agents reconnect with the credentials they already hold.
#
# The manifest written beside the dump says all of this, because a backup
# whose scope is only in a README is a backup somebody will discover the
# scope of during a restore.
#
# # The dump format
#
# `-Fc` (custom) rather than plain SQL: it is compressed, it can be restored
# selectively, and `pg_restore` can use it to rebuild into a differently-named
# database. A plain-SQL dump is more readable and is also 5–10× larger and
# all-or-nothing.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

OUTPUT_DIR="."
DATABASE_URL=""
CONTAINER=""
DATABASE_NAME="hcc"
DATABASE_USER="hcc"
PROJECT="hcc"
COMPOSE_FILE="$REPO_ROOT/deploy/production/compose.yaml"
QUIET=0

green() { [ "$QUIET" = 1 ] || printf '\033[0;32m%s\033[0m\n' "$1"; }
red()   { printf '\033[0;31m%s\033[0m\n' "$1" >&2; }
info()  { [ "$QUIET" = 1 ] || printf '\033[0;36m%s\033[0m\n' "$1"; }
dim()   { [ "$QUIET" = 1 ] || printf '\033[0;90m%s\033[0m\n' "$1"; }

die() { red "$1"; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --output) OUTPUT_DIR="${2:?--output needs a directory}"; shift 2 ;;
    --database-url) DATABASE_URL="${2:?--database-url needs a value}"; shift 2 ;;
    # A PostgreSQL container not managed by this Compose file — the
    # development database, or one started by hand. Needed because `pg_dump`
    # is frequently *not* installed on the machine running the controller:
    # the database lives in a container, and so does its client.
    --container) CONTAINER="${2:?--container needs a name}"; shift 2 ;;
    --database) DATABASE_NAME="${2:?--database needs a name}"; shift 2 ;;
    --user) DATABASE_USER="${2:?--user needs a name}"; shift 2 ;;
    --project) PROJECT="${2:?--project needs a value}"; shift 2 ;;
    --compose-file) COMPOSE_FILE="${2:?--compose-file needs a path}"; shift 2 ;;
    --quiet) QUIET=1; shift ;;
    -h|--help) sed -n '2,45p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

mkdir -p "$OUTPUT_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DUMP="$OUTPUT_DIR/hcc-$STAMP.dump"
MANIFEST="$OUTPUT_DIR/hcc-$STAMP.manifest.txt"

# ---------------------------------------------------------------------------
# Where the database is
# ---------------------------------------------------------------------------
# Two modes. `--database-url` dumps over the network from anywhere, which is
# what a developer or an external PostgreSQL needs. Without it, the dump runs
# *inside* the Compose stack's own postgres container — where the database is
# not published to the host at all, so there is nothing to connect to from
# outside and `pg_dump` has to be where the socket is.
if [ -n "$CONTAINER" ]; then
  RUNTIME=""
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    RUNTIME="docker"
  elif command -v podman >/dev/null 2>&1; then
    RUNTIME="podman"
  else
    die "no container runtime found"
  fi
  info "Dumping from the $CONTAINER container"
  # `exec -i` without a TTY: a binary dump on stdout is corrupted by terminal
  # processing, and the corruption only surfaces during a restore — which is
  # the worst possible time to find out.
  if ! $RUNTIME exec -i "$CONTAINER" \
    pg_dump --format=custom --username="$DATABASE_USER" "$DATABASE_NAME" > "$DUMP"; then
    rm -f "$DUMP"
    die "pg_dump inside $CONTAINER failed"
  fi
  SOURCE="container $CONTAINER, database $DATABASE_NAME"
elif [ -n "$DATABASE_URL" ]; then
  command -v pg_dump >/dev/null 2>&1 \
    || die "pg_dump is not installed. Use --container NAME to run it inside the \
database's own container, or install postgresql-client."
  info "Dumping $(printf '%s' "$DATABASE_URL" | sed 's|://[^@]*@|://<redacted>@|')"
  # --no-password: fail rather than prompt. A backup that blocks on a prompt
  # is a backup that silently stops happening when it is run from cron.
  if ! pg_dump --format=custom --no-password --file="$DUMP" "$DATABASE_URL"; then
    rm -f "$DUMP"
    die "pg_dump failed"
  fi
  SOURCE="$(printf '%s' "$DATABASE_URL" | sed 's|://[^@]*@|://<redacted>@|')"
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

  info "Dumping from the $PROJECT stack's postgres service"
  # `exec -T`: no TTY, so the binary dump on stdout is not mangled by terminal
  # processing. Without it the dump is corrupt in a way that only shows up
  # during a restore — which is the worst possible time to find out.
  # shellcheck disable=SC2086
  if ! $COMPOSE -p "$PROJECT" -f "$COMPOSE_FILE" exec -T postgres \
    pg_dump --format=custom --username=hcc hcc > "$DUMP"; then
    rm -f "$DUMP"
    die "pg_dump inside the stack failed"
  fi
  SOURCE="compose project $PROJECT, service postgres, database hcc"
fi

[ -s "$DUMP" ] || { rm -f "$DUMP"; die "the dump is empty"; }

# A custom-format dump starts with the magic "PGDMP". Checked, because an
# empty-but-nonzero file and a dump of an error message both have a size.
if ! head -c 5 "$DUMP" | grep -q "PGDMP"; then
  rm -f "$DUMP"
  die "the output is not a PostgreSQL custom-format dump; check the error above"
fi

SIZE="$(stat -c %s "$DUMP")"
DIGEST="$(sha256sum "$DUMP" | cut -d' ' -f1)"

# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------
# Written beside the dump rather than documented elsewhere. A backup whose
# scope lives in a README is a backup whose scope gets discovered during a
# restore.
cat > "$MANIFEST" <<MANIFEST
Homelab Control Center backup
=============================

taken          $STAMP
source         $SOURCE
file           $(basename "$DUMP")
size           $SIZE bytes
sha256         $DIGEST
format         PostgreSQL custom (pg_dump -Fc), restore with pg_restore

WHAT IS IN THIS FILE
--------------------
Everything the control plane owns: user accounts and their Argon2id password
hashes, sessions, the host registry, agent credential *digests*, monitoring
configuration and its revision history, the audit trail, and every stored
measurement.

The control plane keeps no state on disk, so this one file is the whole
installation.

WHAT IS NOT IN THIS FILE
------------------------
* The database password (deploy/production/secrets/db_password). A restore
  into a FRESH volume generates a new one and does not need this. A restore
  into an EXISTING volume needs the original, and not having it means a
  controller that cannot authenticate to its own database.
* Your .env — one required field, HCC_PUBLIC_ORIGIN.
* The agent credentials on your monitored hosts. Those live in
  /etc/hcc-agent/credential on each machine. This dump restores their
  digests, so the agents reconnect with the credentials they already hold —
  no re-enrolment is needed.
* TLS certificates, if you mounted your own.

TREAT THIS FILE AS A SECRET
---------------------------
It contains every password hash and every agent credential digest in the
installation. A hash is not a password and a digest is not a credential, but
both are worth brute-forcing and neither should leave your control.

HOW TO RESTORE
--------------
  ./scripts/restore.sh --from $(basename "$DUMP")

That script verifies this digest before it touches anything.
MANIFEST

green "✓ $DUMP"
dim "  $SIZE bytes, sha256 ${DIGEST:0:16}…"
green "✓ $MANIFEST"
dim "  says what is in the backup and what is not"
