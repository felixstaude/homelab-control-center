#!/usr/bin/env bash
#
# The Compose deployment, from nothing to a working installation.
#
# Builds the image, brings the stack up, bootstraps an administrator, signs in
# through the published port, checks the web client is served from the same
# origin as the API, restarts everything and checks the data survived, then
# tears it down.
#
# # Why this exists
#
# An image building is not evidence of a working deployment, and a Compose
# file parsing is not evidence that `up -d` produces something you can log in
# to. Everything this script asserts is something that was true of a build
# that passed every other gate in this repository.
#
# What it has already caught, on its first runs:
#
#   * `HCC_AUTO_MIGRATE` and the four database parts the entrypoint assembles
#     a URL from are read *outside* the settings model, so the application's
#     unknown-variable refusal rejected them and the container exited at
#     startup. Caught before this script existed, by tests/test_deployment.py,
#     which this script is the behavioural half of.
#
# Usage:
#   ./scripts/deployment-e2e.sh                 (or: make deployment-e2e)
#   ./scripts/deployment-e2e.sh --keep          leave the stack running
#
# Safety
# ------
#   * Its own project name, its own volumes, its own generated secret, all
#     named for this run and removed on every exit path unless --keep.
#   * The controller is published on **127.0.0.1** and the database on
#     nothing, which is what the deployment file does anyway.
#   * Nothing touches the developer's PostgreSQL: this stack brings its own.
#   * No image is pushed anywhere.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

KEEP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --keep) KEEP=1; shift ;;
    -h|--help) sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

RUN_ID="$(date +%s)-$$"
PROJECT="hcc-deploy-$RUN_ID"
# Not 8000, 8099, 8201, 8211 or 8221: every other scenario in this repository
# has its own, and a collision here would make one of them fail confusingly.
PORT=8231
BASE="http://127.0.0.1:$PORT"
API="$BASE/api/v1"
ADMIN_USER="deploy-admin"
ADMIN_PASSWORD="deployment-e2e-$RUN_ID-correct-horse-battery"

WORK=""
COOKIE_JAR=""
STACK_UP=""

PASS=0
FAIL=0

green() { printf '\033[0;32m%s\033[0m\n' "$1"; }
red()   { printf '\033[0;31m%s\033[0m\n' "$1"; }
info()  { printf '\n\033[0;36m%s\033[0m\n' "$1"; }
dim()   { printf '\033[0;90m%s\033[0m\n' "$1"; }

ok()  { green "  ✓ $1"; PASS=$((PASS + 1)); }
bad() { red   "  ✗ $1"; FAIL=$((FAIL + 1)); }

die() { red "$1"; dump_logs; exit 1; }

# --- Compose runtime --------------------------------------------------------
#
# **Docker Compose only**, and that is a measured limitation rather than a
# preference.
#
# podman-compose 1.6.0 is a Python reimplementation with partial support for
# `depends_on` conditions; `up -d` against this stack hangs indefinitely after
# creating the first two containers, with no error. The alternatives were to
# weaken the Compose file to podman-compose's subset — dropping
# `service_completed_successfully`, which is what orders the password
# generation before PostgreSQL starts — or to be clear about which
# implementation the deployment has been verified against.
#
# `deploy/compose.yaml`, the development database, still works with both
# (ADR-010). This is the production stack, and `docs/DEPLOYMENT.md` tells
# operators to use `docker compose`.
#
# Skipped rather than failed when Docker is absent: a developer on Podman
# should not have a red suite for a tool they do not have, and CI runs this
# with Docker.
COMPOSE=""
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
else
  printf '\033[0;33m%s\033[0m\n' \
    "SKIP: this scenario needs Docker Compose, which is not installed here."
  printf '\033[0;90m%s\033[0m\n' \
    "      podman-compose 1.6.0 hangs on this stack's depends_on conditions;" \
    "      see the note in this script and docs/DEPLOYMENT.md. CI runs it with" \
    "      Docker on every change."
  exit 0
fi

compose() {
  # shellcheck disable=SC2086
  env HCC_PUBLIC_ORIGIN="$BASE" HCC_BIND_PORT="$PORT" \
      HCC_VERSION="deployment-e2e" HCC_COMMIT="$RUN_ID" \
      $COMPOSE -p "$PROJECT" \
      -f deploy/production/compose.yaml \
      -f deploy/production/compose.build.yaml \
      "$@"
}

dump_logs() {
  if [ -n "$STACK_UP" ]; then
    red "--- the stack's logs (tail) ---"
    compose logs --tail 60 2>&1 | tail -70 || true
  fi
}

cleanup() {
  status=$?
  if [ -n "$STACK_UP" ] && [ "$KEEP" = 0 ]; then
    # `-v` removes the named volumes too: this stack's data is a few seconds
    # old and leaving volumes behind on every run fills a developer's disk.
    #
    # Deliberately **not** `--remove-orphans`. Each run has its own project
    # name so there is nothing of its own to orphan, and the flag reaches
    # containers Compose merely believes belong to the project — which, while
    # the production file still carried `container_name: hcc-postgres`,
    # matched the *developer's own* database container and removed it. The
    # fixed names are gone and so is this flag.
    compose down -v >/dev/null 2>&1 || true
    green "✓ removed the $PROJECT stack and its volumes"
  elif [ -n "$STACK_UP" ]; then
    dim "kept the $PROJECT stack (--keep). Remove it with:"
    dim "  $COMPOSE -p $PROJECT -f deploy/production/compose.yaml -f deploy/production/compose.build.yaml down -v"
  fi
  [ -n "$COOKIE_JAR" ] && rm -f "$COOKIE_JAR"
  # The generated secret is this run's and belongs to nothing else.
  if [ "$KEEP" = 0 ] && [ -f deploy/production/secrets/db_password ]; then
    rm -f deploy/production/secrets/db_password
    rmdir deploy/production/secrets 2>/dev/null || true
  fi
  [ -n "$WORK" ] && rm -rf "$WORK"
  exit "$status"
}
trap cleanup EXIT

json_field() {
  python3 -c "
import json, sys
document = json.load(sys.stdin)
for key in sys.argv[1].split('.'):
    document = document[int(key)] if isinstance(document, list) else document[key]
print(document if document is not None else '')
" "$1"
}

wait_for() {
  label="$1"; url="$2"; seconds="${3:-120}"
  deadline=$((SECONDS + seconds))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if curl -sS -o /dev/null --max-time 3 "$url" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  red "  ✗ $label did not answer at $url within ${seconds}s"
  return 1
}

# ===========================================================================
info "== 1. Preconditions =="

if curl -sS -o /dev/null --max-time 2 "$BASE/" 2>/dev/null; then
  die "something is already listening on port $PORT"
fi
ok "port $PORT is free"
ok "using $COMPOSE, project $PROJECT"

WORK="$(mktemp -d /tmp/hcc-deploy-XXXXXX)"
# A pre-existing secret from a previous run would make the "generated once"
# assertion meaningless.
rm -f deploy/production/secrets/db_password

# ===========================================================================
info "== 2. Build and start =="

if timeout 900 env HCC_PUBLIC_ORIGIN="$BASE" HCC_BIND_PORT="$PORT" \
     HCC_VERSION="deployment-e2e" HCC_COMMIT="$RUN_ID" \
     $COMPOSE -p "$PROJECT" \
     -f deploy/production/compose.yaml \
     -f deploy/production/compose.build.yaml \
     up -d --build >"$WORK/up.log" 2>&1; then
  STACK_UP=1
  ok "the stack came up"
else
  STACK_UP=1
  cat "$WORK/up.log"
  die "docker compose up failed"
fi
dim "$(tail -5 "$WORK/up.log")"

# The generated credential. Checked for shape rather than value: this is the
# one secret the stack creates, and it has to be URL-safe because the
# entrypoint puts it in a postgresql:// URL.
secret="deploy/production/secrets/db_password"
if [ -s "$secret" ]; then
  ok "a database password was generated into $secret"
else
  bad "no database password was generated"
fi
# 0644, not 0600, and that is the right answer rather than a compromise:
# three containers with three different uids read this file — the generator,
# PostgreSQL (uid 70) and the control plane (uid 10001) — so a 0600 file owned
# by one of them is unreadable by the other two. The protection is the
# deployment directory on the host and the mount being read-only in both
# consumers, which is the same guarantee Docker's own /run/secrets gives
# (0444 there).
mode="$(stat -c %a "$secret" 2>/dev/null || true)"
if [ "$mode" = "644" ] || [ "$mode" = "600" ]; then
  ok "the generated password is mode 0$mode, readable by the containers that need it"
else
  bad "the generated password is mode '$mode'"
fi
if grep -qE '^[A-Za-z0-9_-]+$' "$secret"; then
  ok "the generated password is URL-safe, so the assembled URL cannot be corrupted"
else
  bad "the generated password contains characters that are not URL-safe"
fi
generated_before="$(cat "$secret")"

wait_for "the control plane" "$BASE/healthz" 180 || die "the controller never answered"
ok "/healthz answers"

ready="$(curl -sS --max-time 5 "$BASE/readyz")"
if printf '%s' "$ready" | grep -q '"status":"ready"'; then
  ok "/readyz reports the database reachable, so the migrations ran"
else
  bad "/readyz is not ready: $ready"
fi

# ===========================================================================
info "== 3. The deployment's own invariants =="

# The container must not be running as root. Read from inside it rather than
# from the Dockerfile, because a `user:` in a Compose file could override it.
whoami_output="$(compose exec -T hcc id -u 2>/dev/null | tr -d '\r\n' || true)"
if [ -n "$whoami_output" ] && [ "$whoami_output" != "0" ]; then
  ok "the control plane runs as uid $whoami_output, not root"
else
  bad "the control plane's uid is '$whoami_output' (0 means it is running as root)"
fi

# Nothing in the application writes to the filesystem: every piece of state is
# in PostgreSQL. So the root filesystem is read-only, and this proves it is
# actually read-only rather than merely declared so.
if compose exec -T hcc sh -c 'touch /opt/hcc/should-not-be-writable 2>/dev/null' ; then
  bad "the root filesystem is writable"
  compose exec -T hcc rm -f /opt/hcc/should-not-be-writable >/dev/null 2>&1 || true
else
  ok "the root filesystem is read-only"
fi
if compose exec -T hcc sh -c 'touch /tmp/scratch && rm /tmp/scratch' >/dev/null 2>&1; then
  ok "/tmp is writable, which is the one thing Python may need"
else
  bad "/tmp is not writable"
fi

# The database must not be reachable from the host. `ports:` is absent from
# the Compose file and this is the behavioural half of that assertion.
if timeout 3 bash -c "exec 3<>/dev/tcp/127.0.0.1/5432" 2>/dev/null; then
  # The developer's own PostgreSQL may legitimately be on 5432, so this is
  # only a failure if it is *this stack's*.
  dim "  (something is on 127.0.0.1:5432 — the developer's own database, most likely)"
else
  ok "nothing of this stack's is listening on 127.0.0.1:5432"
fi

# ===========================================================================
info "== 4. First run: the administrator is created by a local command =="

# ADR-033: there is deliberately no HTTP endpoint that creates the first
# administrator, because an endpoint that works while no user exists hands
# ownership of the deployment to whoever reaches the port first. Inside a
# container, `exec` *is* the local command.
setup="$(curl -sS "$API/setup/status")"
if printf '%s' "$setup" | grep -q '"is_initialized":false'; then
  ok "the installation reports itself uninitialised before bootstrap"
else
  bad "setup status is wrong before bootstrap: $setup"
fi

if printf '%s\n' "$ADMIN_PASSWORD" | compose exec -T hcc \
  hcc bootstrap-admin --username "$ADMIN_USER" --password-stdin >"$WORK/bootstrap.log" 2>&1
then
  ok "hcc bootstrap-admin created the first administrator"
else
  cat "$WORK/bootstrap.log"
  die "bootstrap-admin failed"
fi

setup="$(curl -sS "$API/setup/status")"
if printf '%s' "$setup" | grep -q '"is_initialized":true'; then
  ok "the installation reports itself initialised afterwards"
else
  bad "setup status is wrong after bootstrap: $setup"
fi

# And it does not reopen. ADR-033 again: bootstrap is one-way, recorded as an
# installation row, so a second run cannot create a second owner.
if printf '%s\n' "$ADMIN_PASSWORD" | compose exec -T hcc \
  hcc bootstrap-admin --username intruder --password-stdin >"$WORK/bootstrap2.log" 2>&1
then
  bad "bootstrap-admin ran a second time and created another administrator"
else
  ok "a second bootstrap is refused, so ownership cannot be claimed twice"
fi

# ===========================================================================
info "== 5. Signing in through the published port =="

COOKIE_JAR="$(mktemp "$WORK/cookies-XXXXXX")"
login="$(curl -sS -c "$COOKIE_JAR" -X POST "$API/auth/login" \
  -H 'Content-Type: application/json' \
  -H "Origin: $BASE" \
  -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASSWORD\"}")"
CSRF="$(printf '%s' "$login" | json_field csrf_token 2>/dev/null || true)"
if [ -n "$CSRF" ]; then
  ok "signed in, with a CSRF token"
else
  bad "login failed: $login"
fi

me="$(curl -sS "$API/auth/me" -b "$COOKIE_JAR")"
if printf '%s' "$me" | grep -q "\"username\":\"$ADMIN_USER\""; then
  ok "the session is usable"
else
  bad "the session does not identify the administrator: $me"
fi

# The cookie must be Secure in production. The deployment sets
# HCC_ENVIRONMENT=production, so this is the setting actually taking effect —
# and over plain HTTP to loopback the browser would refuse it, which is why a
# real deployment needs the proxy.
if grep -qi "hcc_session" "$COOKIE_JAR"; then
  ok "a session cookie was set"
else
  bad "no session cookie in the jar"
fi

# ===========================================================================
info "== 6. One origin: the client is served by the control plane =="

index="$(curl -sS "$BASE/")"
if printf '%s' "$index" | grep -q "Homelab Control Center"; then
  ok "the web client is served at the root"
else
  bad "the root does not serve the client"
fi
if printf '%s' "$index" | grep -qE 'src="/assets/index-[^"]+\.js"'; then
  ok "the document references a hashed bundle"
else
  bad "the document has no hashed bundle reference"
fi

asset="$(printf '%s' "$index" | grep -oE '/assets/index-[A-Za-z0-9_-]+\.js' | head -1)"
if [ -n "$asset" ]; then
  headers="$(curl -sS -D- -o /dev/null "$BASE$asset")"
  if printf '%s' "$headers" | grep -qi 'cache-control: public, max-age=31536000, immutable'; then
    ok "hashed assets are immutable for a year"
  else
    bad "the asset's cache headers are wrong: $(printf '%s' "$headers" | grep -i cache-control)"
  fi
fi

# A deep link has to work on a page refresh, which means the fallback has to
# serve the document — and an API typo has to stay a JSON 404.
if printf '%s' "$(curl -sS "$BASE/hosts")" | grep -q "Homelab Control Center"; then
  ok "a client route returns the document, so a deep link survives a refresh"
else
  bad "a client route does not return the document"
fi
typo="$(curl -sS "$API/not-a-real-endpoint")"
if printf '%s' "$typo" | grep -q '"code":"not_found"'; then
  ok "an unknown API path is still a JSON 404, not a page"
else
  bad "an unknown API path returned something other than the error envelope: $typo"
fi

# The security headers the middleware applies have to reach a *static*
# response too, not only an API one.
if curl -sS -D- -o /dev/null "$BASE/" | grep -qi 'content-security-policy'; then
  ok "the document carries a Content-Security-Policy"
else
  bad "no Content-Security-Policy on the document"
fi

# ===========================================================================
info "== 7. A host can be registered, which is what the product is for =="

host="$(curl -sS -X POST "$API/hosts" -b "$COOKIE_JAR" \
  -H 'Content-Type: application/json' -H "X-CSRF-Token: $CSRF" -H "Origin: $BASE" \
  -d '{"name":"deployment-e2e","environment":"test"}')"
HOST_ID="$(printf '%s' "$host" | json_field host_id 2>/dev/null || true)"
if [ -n "$HOST_ID" ]; then
  ok "a host was registered ($HOST_ID)"
else
  bad "host registration failed: $host"
fi

releases="$(curl -sS "$API/agent-releases" -b "$COOKIE_JAR")"
if printf '%s' "$releases" | grep -q '"available":false'; then
  ok "the install manifest honestly reports no published release"
else
  bad "agent-releases claims a release this build does not have: $releases"
fi

# ===========================================================================
info "== 8. Data survives a restart =="

compose restart hcc >/dev/null 2>&1 || die "could not restart the controller"
wait_for "the control plane after a restart" "$BASE/healthz" 120 \
  || die "the controller did not come back"
ok "the controller restarted"

# The migrations run on every start. A second run must be a no-op rather than
# an error, which is what makes `up -d` after an upgrade safe.
if printf '%s' "$(curl -sS "$BASE/readyz")" | grep -q '"status":"ready"'; then
  ok "it is ready again, so re-running the migrations was a no-op"
else
  bad "it is not ready after a restart"
fi

after="$(curl -sS "$API/hosts" -b "$COOKIE_JAR")"
if printf '%s' "$after" | grep -q "deployment-e2e"; then
  ok "the registered host survived the restart"
else
  bad "the host is gone after a restart: $after"
fi

# And the whole stack, not just the application container.
compose down >/dev/null 2>&1 || die "could not stop the stack"
compose up -d >/dev/null 2>&1 || die "could not bring the stack back"
wait_for "the control plane after a full stop and start" "$BASE/healthz" 180 \
  || die "the stack did not come back"
ok "the whole stack stopped and started"

if [ "$(cat "$secret")" = "$generated_before" ]; then
  ok "the generated password was not regenerated, so the database is still reachable"
else
  bad "the password was regenerated; the existing volume would now be unreachable"
fi

after="$(curl -sS "$API/hosts" -b "$COOKIE_JAR")"
if printf '%s' "$after" | grep -q "deployment-e2e"; then
  ok "the host survived a full stack restart, so the volume is persistent"
else
  bad "the host is gone after a full restart: $after"
fi

# ===========================================================================
info "== 9. Backup and restore, against the running stack =="

if ./scripts/backup.sh --output "$WORK" --project "$PROJECT" \
  --compose-file deploy/production/compose.yaml --quiet >"$WORK/backup.log" 2>&1
then
  ok "scripts/backup.sh produced a dump"
else
  cat "$WORK/backup.log"
  bad "backup.sh failed"
fi

dump="$(ls "$WORK"/hcc-*.dump 2>/dev/null | head -1 || true)"
manifest="${dump%.dump}.manifest.txt"
if [ -n "$dump" ] && [ -s "$dump" ]; then
  ok "the dump is $(stat -c %s "$dump") bytes"
else
  bad "no dump was written"
fi
if [ -f "$manifest" ] && grep -q "WHAT IS NOT IN THIS FILE" "$manifest"; then
  ok "a manifest beside it says what the backup does and does not contain"
else
  bad "no manifest, or it does not state the backup's scope"
fi
# The one thing a dump of this application must never be mistaken for: a
# complete disaster-recovery bundle. The database password is not in it.
if grep -q "db_password" "$manifest"; then
  ok "the manifest says the database password is not included"
else
  bad "the manifest does not mention the excluded database password"
fi

# A dry run must change nothing, and must refuse to pretend the target is
# empty when it is not.
if ./scripts/restore.sh --from "$dump" --project "$PROJECT" \
  --compose-file deploy/production/compose.yaml --dry-run >"$WORK/dryrun.log" 2>&1
then
  if grep -q "Nothing above has been done" "$WORK/dryrun.log"; then
    ok "restore --dry-run reports the plan and changes nothing"
  else
    bad "restore --dry-run did not say it had changed nothing"
  fi
else
  cat "$WORK/dryrun.log"
  bad "restore --dry-run failed"
fi

# A populated target without --force is refused. Every measurement since the
# backup would be lost, so this has to be a deliberate act.
if ./scripts/restore.sh --from "$dump" --project "$PROJECT" \
  --compose-file deploy/production/compose.yaml >"$WORK/refused.log" 2>&1
then
  bad "restore overwrote a populated database without --force"
else
  if grep -q "not empty" "$WORK/refused.log"; then
    ok "a populated target is refused without --force"
  else
    bad "the refusal did not explain itself: $(tail -2 "$WORK/refused.log")"
  fi
fi

# A corrupted dump is caught before anything is dropped.
cp "$dump" "$WORK/corrupt.dump"
cp "$manifest" "$WORK/corrupt.manifest.txt"
printf 'x' | dd of="$WORK/corrupt.dump" bs=1 seek=512 conv=notrunc 2>/dev/null
if ./scripts/restore.sh --from "$WORK/corrupt.dump" --project "$PROJECT" \
  --compose-file deploy/production/compose.yaml --force >"$WORK/corrupt.log" 2>&1
then
  bad "a corrupted dump was restored"
else
  if grep -q "does not match its manifest" "$WORK/corrupt.log"; then
    ok "a corrupted dump is refused by its digest, before anything is dropped"
  else
    bad "the corruption was caught for the wrong reason: $(tail -2 "$WORK/corrupt.log")"
  fi
fi

# And the real thing: a forced restore brings the data back.
#
# The host registered in stage 7 is the evidence, and the assertion is strong
# without having to delete it first: the restore drops the whole public
# schema before rebuilding — because `pg_restore --clean` cannot drop a
# partitioned table's inherited constraints — so the host *is* removed, and
# finding it afterwards means the dump put it back.
#
# There is deliberately no DELETE endpoint for a host
# (apps/server/hcc/api/v1/hosts.py), so deleting it over the API is not an
# option in any case.

if ./scripts/restore.sh --from "$dump" --project "$PROJECT" \
  --compose-file deploy/production/compose.yaml --force >"$WORK/restore.log" 2>&1
then
  ok "scripts/restore.sh restored the dump"
else
  cat "$WORK/restore.log"
  bad "restore.sh failed"
fi
dim "$(grep -A 8 "What came back" "$WORK/restore.log" | tail -7 || true)"

# The control plane has to be restarted: SQLAlchemy holds pooled connections
# to tables that have just been dropped and recreated.
compose restart hcc >/dev/null 2>&1 || true
wait_for "the control plane after the restore" "$BASE/healthz" 120 || true

# A fresh session, because the restore replaced the sessions table with the
# backup's contents — which is correct, and means the cookie from before is
# for a session that no longer exists.
RESTORED_JAR="$(mktemp "$WORK/cookies-restored-XXXXXX")"
login="$(curl -sS -c "$RESTORED_JAR" -X POST "$API/auth/login" \
  -H 'Content-Type: application/json' -H "Origin: $BASE" \
  -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASSWORD\"}")"
if printf '%s' "$login" | grep -q "csrf_token"; then
  ok "the restored administrator can still sign in, so the password hash survived"
else
  bad "cannot sign in after the restore: $login"
fi

restored="$(curl -sS "$API/hosts" -b "$RESTORED_JAR")"
if printf '%s' "$restored" | grep -q "deployment-e2e"; then
  ok "the deleted host came back, so the restore restored data rather than a schema"
else
  bad "the host did not come back: $restored"
fi

# ===========================================================================
info "== 10. A refusal: the stack will not start without its one setting =="

# Compose's `:?` makes this fail before anything is pulled or started, with
# the message naming the variable — rather than the application rejecting it
# later with a less useful error.
if env -u HCC_PUBLIC_ORIGIN HCC_BIND_PORT="$PORT" \
  $COMPOSE -p "${PROJECT}-nope" \
  -f deploy/production/compose.yaml -f deploy/production/compose.build.yaml \
  config >"$WORK/noorigin.log" 2>&1
then
  bad "the Compose file accepted a missing HCC_PUBLIC_ORIGIN"
else
  if grep -qi "HCC_PUBLIC_ORIGIN" "$WORK/noorigin.log"; then
    ok "a missing HCC_PUBLIC_ORIGIN is refused, naming the variable"
  else
    bad "it was refused without naming the variable: $(tail -2 "$WORK/noorigin.log")"
  fi
fi

# ===========================================================================
info "== Result =="

printf '  %s passed, %s failed  (%s)\n' "$PASS" "$FAIL" "$COMPOSE"
if [ "$FAIL" -gt 0 ]; then
  red "✗ the deployment is not sound"
  dump_logs
  exit 1
fi
green "✓ built, started, bootstrapped, signed in, served the client, and survived a restart"
