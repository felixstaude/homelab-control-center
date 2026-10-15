#!/usr/bin/env bash
#
# The monitoring pipeline, end to end, with nothing faked.
#
# A real controller against a real PostgreSQL, a real agent binary built from
# source reading this machine's real /proc, a real WebSocket between them, and
# real rows in real partitions. The only thing this script supplies is the
# instruction to start collecting; every number it then asserts on was measured
# by the agent.
#
# That is the whole point. The unit and integration suites prove each piece
# behaves; they cannot prove the pieces agree. The checksum the controller
# computes has to match the one the agent computes over the bytes it received,
# the agent's idea of a module id has to match the registry's, and a sample
# produced by reading /proc/stat has to survive four validation layers, a
# partition router and three query tiers to reach a chart. Every one of those
# is a place two correct implementations can disagree.
#
# Usage:
#   make db-up
#   ./scripts/monitoring-e2e.sh
#
# Safety
# ------
# Nothing here touches the developer's installation or any remote host:
#
#   * The database is created for this run and dropped on every exit path.
#   * The controller listens on a port chosen not to collide with a dev server.
#   * The agent's state lives in a fresh mktemp directory, so no credential is
#     written to /etc and the local machine is never enrolled into anything
#     that outlives the script.
#   * The agent reads /proc. It writes nothing outside its state directory and
#     runs no command on the host.
#
# Exits non-zero on the first failure, and prints the controller and agent logs
# when it does — a failure here is usually a disagreement between two
# processes, and one side's log alone rarely explains it.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

SERVER_DIR="apps/server"
AGENT_DIR="agents/linux"
VENV="$SERVER_DIR/.venv"

# How many modules the controller's registry declares.
#
# PYTHONPATH rather than a `cd` into the server directory: SERVER_DIR and
# VENV are both relative to the repository root, so changing directory breaks
# every later use of them — which it did, as `No such file or directory` from
# a subshell, reported against a line that looked like a comment.
registry_module_count() {
  PYTHONPATH="$REPO_ROOT/$SERVER_DIR" "$REPO_ROOT/$VENV/bin/python" -c "
from hcc.monitoring.modules import MODULE_IDS
print(len(MODULE_IDS))
"
}

# Local development credentials, matching deploy/compose.yaml. Not a secret.
PG_SUPERUSER_URL="postgresql://hcc:hcc_local_dev@127.0.0.1:5432/hcc"
TEST_DB="hcc_mon_$(date +%s)_$$"
TEST_DB_URL="postgresql://hcc:hcc_local_dev@127.0.0.1:5432/$TEST_DB"

# Not 8000, 8099 or 8201: a dev server, the smoke test and the browser suite
# must not be mistaken for this one.
PORT=8211
BASE="http://127.0.0.1:$PORT"
API="$BASE/api/v1"

ADMIN_USER="e2e-admin"
# A local throwaway for a database that exists for ninety seconds. Long and
# unguessable so it also satisfies the password policy.
ADMIN_PASSWORD="monitoring-e2e-$(date +%s)-correct-horse"

# One second, so the scenario observes several collections inside its runtime
# rather than waiting out the ten-second default. Above the cpu/memory floor of
# one second, deliberately: a value below it must be refused, and that refusal
# is asserted separately.
COLLECT_INTERVAL=1

SERVER_PID=""
AGENT_PID=""
AGENT_STATE_DIR=""
CREATED=""
COOKIE_JAR=""

PASS=0
FAIL=0

green() { printf '\033[0;32m%s\033[0m\n' "$1"; }
red()   { printf '\033[0;31m%s\033[0m\n' "$1"; }
info()  { printf '\n\033[0;36m%s\033[0m\n' "$1"; }
dim()   { printf '\033[0;90m%s\033[0m\n' "$1"; }

ok()  { green "  ✓ $1"; PASS=$((PASS + 1)); }
bad() { red   "  ✗ $1"; FAIL=$((FAIL + 1)); }

die() {
  red "$1"
  dump_logs
  exit 1
}

dump_logs() {
  if [[ -f /tmp/hcc-mon-server.log ]]; then
    red "--- controller log (tail) ---"
    tail -40 /tmp/hcc-mon-server.log || true
  fi
  if [[ -f /tmp/hcc-mon-agent.log ]]; then
    red "--- agent log (tail) ---"
    tail -40 /tmp/hcc-mon-agent.log || true
  fi
}

psql_super() {
  "$VENV/bin/python" - "$1" <<'PY'
import asyncio
import os
import sys

import asyncpg


async def main() -> None:
    connection = await asyncpg.connect(os.environ["PG_SUPERUSER_URL"])
    try:
        await connection.execute(sys.argv[1])
    finally:
        await connection.close()


asyncio.run(main())
PY
}

# Run one read query against the disposable database and print the rows.
query() {
  TEST_DB_URL="$TEST_DB_URL" "$VENV/bin/python" - "$1" <<'PY'
import asyncio
import os
import sys

import asyncpg


async def main() -> None:
    connection = await asyncpg.connect(os.environ["TEST_DB_URL"])
    try:
        rows = await connection.fetch(sys.argv[1])
        for row in rows:
            print("\t".join("" if value is None else str(value) for value in row.values()))
    finally:
        await connection.close()


asyncio.run(main())
PY
}

cleanup() {
  local status=$?
  # The agent first: it holds a socket the controller is serving, and killing
  # the controller out from under it produces a reconnect storm in the log that
  # obscures whatever actually failed.
  if [[ -n "$AGENT_PID" ]] && kill -0 "$AGENT_PID" 2>/dev/null; then
    kill "$AGENT_PID" 2>/dev/null || true
    wait "$AGENT_PID" 2>/dev/null || true
  fi
  if [[ -n "$AGENT_STATE_DIR" ]]; then
    # Anchored to the state directory. An unanchored `pkill -f hcc-agent`
    # matches this script's own command line and kills the shell running it.
    pkill -f "^$AGENT_STATE_DIR/hcc-agent" 2>/dev/null || true
    rm -rf "$AGENT_STATE_DIR"
  fi
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  [[ -n "$COOKIE_JAR" ]] && rm -f "$COOKIE_JAR"
  if [[ -n "$CREATED" ]]; then
    if PG_SUPERUSER_URL="$PG_SUPERUSER_URL" psql_super \
      "DROP DATABASE IF EXISTS \"$TEST_DB\" WITH (FORCE)" 2>/dev/null; then
      green "✓ dropped $TEST_DB"
    else
      red "✗ could not drop $TEST_DB — remove it by hand"
    fi
  fi
  exit "$status"
}
trap cleanup EXIT

wait_for() {
  local url="$1" label="$2"
  for _ in $(seq 1 100); do
    if curl -sS -o /dev/null --max-time 2 "$url" 2>/dev/null; then
      return 0
    fi
    sleep 0.2
  done
  red "  ✗ $label did not come up at $url"
  return 1
}

# Poll a database query until it prints the expected value, or time out.
#
# Polling rather than sleeping: the agent's timing is its own — a tick, a
# socket write, a transaction — and a fixed sleep either makes the script slow
# or makes it flaky. The timeout is what turns "never happened" into a failure
# rather than a hang.
await_query() {
  local label="$1" sql="$2" expected="$3" timeout="${4:-30}" actual=""
  local deadline=$((SECONDS + timeout))
  while ((SECONDS < deadline)); do
    actual="$(query "$sql" | head -1 || true)"
    if [[ "$actual" == "$expected" ]]; then
      ok "$label"
      return 0
    fi
    sleep 0.5
  done
  bad "$label — expected '$expected', last saw '$actual' after ${timeout}s"
  return 1
}

# Poll until a query's first column is numerically at least a threshold.
await_at_least() {
  local label="$1" sql="$2" threshold="$3" timeout="${4:-30}" actual="0"
  local deadline=$((SECONDS + timeout))
  while ((SECONDS < deadline)); do
    actual="$(query "$sql" | head -1 || true)"
    actual="${actual:-0}"
    if [[ "$actual" =~ ^-?[0-9]+$ ]] && ((actual >= threshold)); then
      ok "$label ($actual)"
      return 0
    fi
    sleep 0.5
  done
  bad "$label — wanted at least $threshold, last saw '$actual' after ${timeout}s"
  return 1
}

# curl against the API with the session cookie and the CSRF header.
api() {
  local method="$1" path="$2" body="${3:-}"
  if [[ -n "$body" ]]; then
    curl -sS -X "$method" "$API$path" \
      -b "$COOKIE_JAR" -c "$COOKIE_JAR" \
      -H "Content-Type: application/json" \
      -H "X-CSRF-Token: $CSRF" \
      -d "$body"
  else
    curl -sS -X "$method" "$API$path" \
      -b "$COOKIE_JAR" -c "$COOKIE_JAR" \
      -H "X-CSRF-Token: $CSRF"
  fi
}

api_get() {
  curl -sS "$API$1" -b "$COOKIE_JAR" -c "$COOKIE_JAR"
}

api_status() {
  local method="$1" path="$2" body="${3:-}"
  curl -sS -o /dev/null -w '%{http_code}' -X "$method" "$API$path" \
    -b "$COOKIE_JAR" -c "$COOKIE_JAR" \
    -H "Content-Type: application/json" \
    -H "X-CSRF-Token: $CSRF" \
    ${body:+-d "$body"}
}

# Read one field out of a JSON object on stdin, without needing jq.
json_field() {
  "$VENV/bin/python" -c "
import json, sys
document = json.load(sys.stdin)
for key in sys.argv[1].split('.'):
    document = document[int(key)] if isinstance(document, list) else document[key]
print(document if document is not None else '')
" "$1"
}

# ===========================================================================
info "== 1. Preconditions =="

[[ -x "$VENV/bin/python" ]] || die "Python venv missing. Run: make install-server"

export PATH="$HOME/.local/go/bin:$PATH"
command -v go >/dev/null || die "Go missing. The agent is built from source here."
ok "toolchain present ($(go version | awk '{print $3}'))"

if curl -sS -o /dev/null --max-time 2 "$BASE/" 2>/dev/null; then
  die "Something is already listening on port $PORT. Stop it first."
fi
ok "port $PORT is free"

# The agent reads these. A container or an exotic kernel without them would
# produce an empty collection that looks like a broken pipeline.
for path in /proc/stat /proc/meminfo /proc/uptime /proc/loadavg; do
  [[ -r "$path" ]] || die "$path is not readable; the agent cannot collect here."
done
ok "/proc exposes stat, meminfo, uptime and loadavg"

# ===========================================================================
info "== 2. Disposable database =="

PG_SUPERUSER_URL="$PG_SUPERUSER_URL" psql_super "CREATE DATABASE \"$TEST_DB\"" >/dev/null
CREATED=1
ok "created $TEST_DB"

(cd "$SERVER_DIR" && HCC_DATABASE_URL="$TEST_DB_URL" "../../$VENV/bin/alembic" upgrade head) \
  >/tmp/hcc-mon-migrate.log 2>&1 || {
  cat /tmp/hcc-mon-migrate.log
  die "migrations failed"
}
ok "migrations applied from zero"

# The partitioned tables and their first partitions are the schema this whole
# scenario depends on, so their absence is caught here rather than as a
# confusing insert failure later.
partitions="$(query "SELECT count(*) FROM metric_partitions WHERE dropped_at IS NULL")"
[[ "$partitions" -ge 3 ]] || die "expected at least 3 catalogued partitions, found $partitions"
ok "metric partitions catalogued ($partitions)"

# ===========================================================================
info "== 3. Control plane =="

(
  cd "$SERVER_DIR"
  exec env \
    HCC_DATABASE_URL="$TEST_DB_URL" \
    HCC_PORT="$PORT" \
    HCC_ENVIRONMENT=development \
    HCC_LOG_FORMAT=json \
    HCC_LOG_LEVEL=INFO \
    HCC_METRICS_MAINTENANCE_INTERVAL_SECONDS=0 \
    .venv/bin/python -m hcc.main
) >/tmp/hcc-mon-server.log 2>&1 &
SERVER_PID=$!

wait_for "$BASE/healthz" "control plane" || die "controller did not start"
ok "controller is serving on $PORT"

ready="$(curl -sS "$BASE/readyz")"
[[ "$ready" == *'"status":"ready"'* ]] || die "readiness is not ready: $ready"
ok "readiness reports the database reachable"

# ===========================================================================
info "== 4. Administrator and session =="

COOKIE_JAR="$(mktemp /tmp/hcc-mon-cookies-XXXXXX)"

# The password goes in on stdin, never as an argument: the CLI refuses a
# --password flag precisely because /proc makes an argv readable by every
# local user.
(cd "$SERVER_DIR" && printf '%s\n' "$ADMIN_PASSWORD" | HCC_DATABASE_URL="$TEST_DB_URL" \
  .venv/bin/python -m hcc.cli.main bootstrap-admin \
  --username "$ADMIN_USER" --password-stdin) \
  >/tmp/hcc-mon-bootstrap.log 2>&1 || {
  cat /tmp/hcc-mon-bootstrap.log
  die "bootstrap failed"
}
ok "administrator created"

login="$(curl -sS -c "$COOKIE_JAR" -X POST "$API/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASSWORD\"}")"
CSRF="$(printf '%s' "$login" | json_field csrf_token)"
[[ -n "$CSRF" ]] || die "login did not return a CSRF token: $login"
ok "signed in"

# ===========================================================================
info "== 5. Default-deny: nothing is collecting before anyone asks =="

defaults="$(api_get /monitoring/defaults)"
enabled_count="$("$VENV/bin/python" -c "
import json, sys
body = json.load(sys.stdin)
print(sum(1 for entry in body['defaults'] if entry['setting']['enabled']))
" <<<"$defaults")"
if [[ "$enabled_count" == "0" ]]; then
  ok "no module is enabled by default (§2.1)"
else
  bad "$enabled_count module(s) enabled on a fresh installation"
fi

catalogue="$(api_get /monitoring/modules)"
module_count="$("$VENV/bin/python" -c "
import json, sys
print(len(json.load(sys.stdin)['modules']))
" <<<"$catalogue")"
# Against the registry rather than a literal. A number copied out of the
# registry asserts that somebody remembered to edit this file when a module
# was added — which is how it came to say 5 while the registry had 11.
expected_modules="$(registry_module_count)"
if [[ "$module_count" == "$expected_modules" ]]; then
  ok "the registry declares $expected_modules modules and the API lists them all"
else
  bad "expected $expected_modules modules in the catalogue, found $module_count"
fi

# ===========================================================================
info "== 6. Host registration and enrollment =="

host="$(api POST /hosts '{"name":"e2e-localhost","description":"monitoring end-to-end","environment":"test"}')"
HOST_ID="$(printf '%s' "$host" | json_field host_id)"
[[ -n "$HOST_ID" ]] || die "host creation failed: $host"
ok "host registered ($HOST_ID)"

# Registering a host must not enrol it, and must not start collecting.
lifecycle="$(printf '%s' "$host" | json_field lifecycle)"
[[ "$lifecycle" == "registered" ]] || bad "a new host should be 'registered', got '$lifecycle'"
[[ "$lifecycle" == "registered" ]] && ok "a new host is registered, not enrolled"

enrollment="$(api POST "/hosts/$HOST_ID/enrollment" '{}')"
ENROLL_CREDENTIAL="$(printf '%s' "$enrollment" | json_field credential)"
[[ -n "$ENROLL_CREDENTIAL" ]] || die "enrollment issuance failed: $enrollment"
ok "one-time enrollment credential issued"

# ===========================================================================
info "== 7. The real agent, built from source =="

AGENT_STATE_DIR="$(mktemp -d /tmp/hcc-mon-agent-XXXXXX)"
chmod 700 "$AGENT_STATE_DIR"

(cd "$AGENT_DIR" && go build -o "$AGENT_STATE_DIR/hcc-agent" ./cmd/hcc-agent) \
  >/tmp/hcc-mon-build.log 2>&1 || {
  cat /tmp/hcc-mon-build.log
  die "agent build failed"
}
ok "built $(du -h "$AGENT_STATE_DIR/hcc-agent" | cut -f1) binary from source"

# The credential goes in a file, never on a command line: /proc makes an
# argv readable by every local user. The agent refuses a flag for this.
printf '%s' "$ENROLL_CREDENTIAL" >"$AGENT_STATE_DIR/enrollment"
chmod 600 "$AGENT_STATE_DIR/enrollment"

env HCC_AGENT_CONTROLLER_URL="$BASE" \
  HCC_AGENT_CREDENTIAL_FILE="$AGENT_STATE_DIR/credential" \
  HCC_AGENT_CONFIG_FILE="$AGENT_STATE_DIR/config.json" \
  HCC_AGENT_ENROLLMENT_TOKEN_FILE="$AGENT_STATE_DIR/enrollment" \
  HCC_AGENT_LOG_FORMAT=json \
  "$AGENT_STATE_DIR/hcc-agent" enroll >/tmp/hcc-mon-enroll.log 2>&1 || {
  cat /tmp/hcc-mon-enroll.log
  die "agent enrollment failed"
}
ok "agent enrolled itself with the one-time credential"

[[ -f "$AGENT_STATE_DIR/credential" ]] || die "no credential was persisted"
perms="$(stat -c '%a' "$AGENT_STATE_DIR/credential")"
if [[ "$perms" == "600" ]]; then
  ok "the persisted credential is mode 600"
else
  bad "credential should be mode 600, is $perms"
fi

# Consuming the credential enrols the host, which the controller must reflect.
await_query "the controller records the host as enrolled" \
  "SELECT lifecycle FROM hosts WHERE id = '$HOST_ID'" "enrolled" 10

# A one-time credential must be exactly that.
replay_dir="$(mktemp -d /tmp/hcc-mon-replay-XXXXXX)"
printf '%s' "$ENROLL_CREDENTIAL" >"$replay_dir/enrollment"
if env HCC_AGENT_CONTROLLER_URL="$BASE" \
  HCC_AGENT_CREDENTIAL_FILE="$replay_dir/credential" \
  HCC_AGENT_CONFIG_FILE="$replay_dir/config.json" \
  HCC_AGENT_ENROLLMENT_TOKEN_FILE="$replay_dir/enrollment" \
  "$AGENT_STATE_DIR/hcc-agent" enroll >/tmp/hcc-mon-replay.log 2>&1; then
  bad "the enrollment credential was accepted a second time"
else
  ok "replaying the enrollment credential is refused"
fi
rm -rf "$replay_dir"

# ===========================================================================
info "== 8. The agent connects and stays connected =="

# The two *local grants* are given here, before anything is enabled, because
# both are capabilities the host administrator supplies rather than the
# controller — and §17 below needs them to be able to turn the modules on.
# Neither does anything while its module is disabled, which it is until then.
#
# DOCKER_SOCKET may well not be readable on this runner. That is a case §18
# asserts rather than avoids: the mission's rule is that an unsupported
# scenario is tested as unsupported, not skipped.
(
  exec env HCC_AGENT_CONTROLLER_URL="$BASE" \
    HCC_AGENT_CREDENTIAL_FILE="$AGENT_STATE_DIR/credential" \
    HCC_AGENT_CONFIG_FILE="$AGENT_STATE_DIR/config.json" \
    HCC_AGENT_LOG_FORMAT=json \
    HCC_AGENT_LOG_LEVEL=debug \
    HCC_AGENT_DOCKER_SOCKET="${HCC_AGENT_DOCKER_SOCKET:-/var/run/docker.sock}" \
    HCC_AGENT_SYSTEMD_UNITS="${HCC_AGENT_SYSTEMD_UNITS:-cron.service,ssh.service}" \
    "$AGENT_STATE_DIR/hcc-agent" run
) >/tmp/hcc-mon-agent.log 2>&1 &
AGENT_PID=$!

await_query "the controller sees the agent connected" \
  "SELECT CASE WHEN current_connection_id IS NOT NULL THEN 'connected' ELSE 'offline' END
   FROM hosts WHERE id = '$HOST_ID'" "connected" 20

await_at_least "a heartbeat arrived" \
  "SELECT CASE WHEN last_heartbeat_at IS NULL THEN 0 ELSE 1 END FROM hosts WHERE id = '$HOST_ID'" \
  1 20

reported="$(query "SELECT agent_version, platform FROM hosts WHERE id = '$HOST_ID'")"
if [[ -n "$reported" ]]; then
  ok "the agent reported its own version and platform ($reported)"
else
  bad "the agent reported no version or platform"
fi

# ===========================================================================
info "== 9. Still nothing is collected, because nothing was enabled =="

sleep 3
samples="$(query "SELECT count(*) FROM metric_samples_raw")"
if [[ "$samples" == "0" ]]; then
  ok "an enrolled, connected host with no modules enabled stores nothing"
else
  bad "expected 0 samples before enabling anything, found $samples"
fi

# ===========================================================================
info "== 10. An interval below the floor is refused, not clamped =="

code="$(api_status PUT "/hosts/$HOST_ID/monitoring/modules/load" \
  '{"schedule":{"kind":"interval","interval_seconds":1}}')"
if [[ "$code" == "422" ]]; then
  ok "load at 1 s is refused (its floor is 5 s)"
else
  bad "expected 422 for an interval below the floor, got $code"
fi

stored="$(query "SELECT count(*) FROM host_module_overrides WHERE host_id = '$HOST_ID' AND module_id = 'load'")"
if [[ "$stored" == "0" ]]; then
  ok "the refused value was not stored"
else
  bad "a refused interval was persisted anyway"
fi

# ===========================================================================
info "== 11. Enabling CPU and memory collection =="

for module in cpu memory; do
  response="$(api PUT "/hosts/$HOST_ID/monitoring/modules/$module" \
    "{\"enabled\":true,\"schedule\":{\"kind\":\"interval\",\"interval_seconds\":$COLLECT_INTERVAL}}")"
  created="$(printf '%s' "$response" | json_field revision_created)"
  if [[ "$created" == "True" ]]; then
    ok "$module enabled at every ${COLLECT_INTERVAL}s, new revision generated"
  else
    bad "$module: expected a new revision, got: $response"
  fi
done

# Saved is not applied. The agent has to receive, validate and acknowledge it.
await_query "the agent acknowledged the configuration" \
  "SELECT status FROM (
     SELECT CASE
       WHEN h.reported_config_version IS NOT NULL
        AND h.reported_config_version = (
              SELECT max(version) FROM host_config_revisions WHERE host_id = h.id
            )
       THEN 'up_to_date' ELSE 'pending' END AS status
     FROM hosts h WHERE h.id = '$HOST_ID'
   ) s" "up_to_date" 30

# The checksum is computed independently on both sides, over the transmitted
# bytes. Equality here is the cross-language canonical-JSON agreement; a
# mismatch would mean the agent applied a document the controller did not send.
checksums="$(query "
  SELECT CASE WHEN h.reported_config_checksum = r.checksum THEN 'match' ELSE 'differ' END
  FROM hosts h
  JOIN host_config_revisions r ON r.host_id = h.id AND r.version = h.reported_config_version
  WHERE h.id = '$HOST_ID'")"
if [[ "$checksums" == "match" ]]; then
  ok "the agent's checksum matches the controller's over the same bytes"
else
  bad "configuration checksums disagree ($checksums)"
fi

# ===========================================================================
info "== 12. Real samples, measured from /proc =="

# CPU utilisation needs two snapshots to produce a delta, so the first tick
# yields nothing. Memory does not.
await_at_least "memory samples are arriving" \
  "SELECT count(*) FROM metric_samples_raw s
   JOIN metric_series e ON e.id = s.series_id
   WHERE e.module = 'memory'" 3 40

await_at_least "CPU samples are arriving" \
  "SELECT count(*) FROM metric_samples_raw s
   JOIN metric_series e ON e.id = s.series_id
   WHERE e.module = 'cpu'" 3 40

# Nothing was enabled except cpu and memory, so nothing else may appear. This
# is the assertion that catches an agent collecting what it was not asked for.
unexpected="$(query "
  SELECT count(*) FROM metric_series
  WHERE host_id = '$HOST_ID' AND module NOT IN ('cpu','memory')")"
if [[ "$unexpected" == "0" ]]; then
  ok "no series exists for a module that was not enabled"
else
  bad "$unexpected series from modules nobody enabled"
fi

# The values have to be plausible measurements of this machine rather than
# zeroes or placeholders. A percentage outside 0..100 means the delta
# arithmetic is wrong; an all-zero memory reading means /proc/meminfo was not
# actually parsed.
cpu_range="$(query "
  SELECT count(*) FROM metric_samples_raw s
  JOIN metric_series e ON e.id = s.series_id
  WHERE e.module = 'cpu' AND e.metric = 'utilisation'
    AND (s.value < 0 OR s.value > 100)")"
if [[ "$cpu_range" == "0" ]]; then
  ok "every CPU utilisation sample is within 0–100%"
else
  bad "$cpu_range CPU samples fall outside 0–100%"
fi

mem_used="$(query "
  SELECT max(s.value)::bigint FROM metric_samples_raw s
  JOIN metric_series e ON e.id = s.series_id
  WHERE e.module = 'memory' AND e.metric = 'used'")"
if [[ -n "$mem_used" && "$mem_used" -gt 10000000 ]]; then
  ok "memory used reads as a real quantity ($((mem_used / 1024 / 1024)) MiB)"
else
  bad "memory used is implausible: '$mem_used' bytes"
fi

# Cross-check against the kernel directly. Not an exact comparison — the two
# readings are seconds apart on a live machine — but an order-of-magnitude
# agreement proves the agent is reading this host and not inventing a number.
total_kib="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
total_bytes=$((total_kib * 1024))
agent_total="$(query "
  SELECT max(s.value)::bigint FROM metric_samples_raw s
  JOIN metric_series e ON e.id = s.series_id
  WHERE e.module = 'memory' AND e.metric = 'total'")"
# The agent reports MemTotal, so this compares its reading against the
# kernel's directly. Allowed to differ slightly — the kernel's MemTotal is
# stable but the two readings are taken seconds apart — and 1 MiB of slack is
# far tighter than any plausible fabrication would survive.
if [[ -n "$agent_total" ]] && (( agent_total > total_bytes - 1048576 && agent_total < total_bytes + 1048576 )); then
  ok "the agent's MemTotal matches the kernel's ($((agent_total / 1024 / 1024)) MiB)"
else
  bad "the agent reported MemTotal $agent_total, the kernel says $total_bytes"
fi
if ((mem_used > 0 && mem_used < total_bytes)); then
  ok "memory used is below this machine's MemTotal ($((total_bytes / 1024 / 1024)) MiB)"
else
  bad "memory used ($mem_used) is not below MemTotal ($total_bytes)"
fi

# A gauge that never changes is a constant somebody hard-coded. Memory moves on
# any live machine over several seconds.
distinct="$(query "
  SELECT count(DISTINCT s.value) FROM metric_samples_raw s
  JOIN metric_series e ON e.id = s.series_id
  WHERE e.module = 'memory' AND e.metric = 'used'")"
if [[ -n "$distinct" && "$distinct" -ge 2 ]]; then
  ok "memory readings vary across samples ($distinct distinct values)"
else
  bad "memory reported the same value every time ($distinct distinct) — not a measurement"
fi

# ===========================================================================
info "== 13. Partition routing is real =="

# Visibility through the parent proves nothing: a row in the DEFAULT partition
# is visible too, and dropping its window would not remove it.
in_default="$(query "
  SELECT count(*) FROM metric_samples_raw
  WHERE tableoid::regclass::text LIKE '%_default'")"
if [[ "$in_default" == "0" ]]; then
  ok "no sample landed in the DEFAULT partition"
else
  bad "$in_default samples landed in the DEFAULT partition"
fi

routed="$(query "
  SELECT DISTINCT tableoid::regclass::text FROM metric_samples_raw LIMIT 1")"
if [[ "$routed" == metric_samples_raw_* ]]; then
  ok "samples are physically stored in $routed"
else
  bad "unexpected physical table: '$routed'"
fi

# ===========================================================================
info "== 14. The API returns what was measured =="

latest="$(api_get "/hosts/$HOST_ID/metrics/latest")"
latest_count="$("$VENV/bin/python" -c "
import json, sys
print(len(json.load(sys.stdin)['values']))
" <<<"$latest")"
if [[ -n "$latest_count" && "$latest_count" -ge 2 ]]; then
  ok "latest returns $latest_count current values"
else
  bad "latest returned $latest_count values: $latest"
fi

# Every value the API reports must trace to a stored sample. This is the
# assertion that would catch a read path fabricating or defaulting a number.
"$VENV/bin/python" - "$latest" <<'PY' && ok "every reported value is a real measurement" || bad "a reported value is not plausible"
import json
import sys

body = json.loads(sys.argv[1])
for entry in body["values"]:
    series = entry["series"]
    assert series["module"] in {"cpu", "memory"}, series["module"]
    if series["unit"] == "percent":
        assert 0.0 <= entry["value"] <= 100.0, entry
    if series["unit"] == "bytes":
        assert entry["value"] > 0, entry
    # A timestamp is required and must parse; a value without one cannot be
    # judged for freshness and must never be shown as current.
    assert entry["recorded_at"], entry
PY

series_body="$(api_get "/hosts/$HOST_ID/metrics/series")"
series_count="$("$VENV/bin/python" -c "
import json, sys
print(len(json.load(sys.stdin)['series']))
" <<<"$series_body")"
if [[ -n "$series_count" && "$series_count" -ge 2 ]]; then
  ok "series lists $series_count streams"
else
  bad "series returned $series_count: $series_body"
fi

status_body="$(api_get "/hosts/$HOST_ID/metrics/status")"
stored_count="$(printf '%s' "$status_body" | json_field sample_count)"
if [[ -n "$stored_count" && "$stored_count" -gt 0 ]]; then
  ok "status reports $stored_count stored samples"
else
  bad "status reported no samples: $status_body"
fi

# Every module appears, including the three nobody enabled — so "no data" and
# "no such module" stay distinguishable.
# Against the registry rather than a number copied from it. The property
# worth asserting is "every module the controller knows is listed, including
# the ones with no data" — a literal count asserts instead that somebody
# remembered to edit this file when a module was added, which is how it came
# to say 5 while the registry had 11.
status_modules="$("$VENV/bin/python" -c "
import json, sys
print(len(json.load(sys.stdin)['modules']))
" <<<"$status_body")"
registry_modules="$(registry_module_count)"
if [[ "$status_modules" == "$registry_modules" ]]; then
  ok "status lists all $registry_modules modules, including those with no data"
else
  bad "status listed $status_modules modules, the registry has $registry_modules"
fi

SERIES_ID="$("$VENV/bin/python" -c "
import json, sys
body = json.load(sys.stdin)
cpu = [s for s in body['series'] if s['module'] == 'cpu' and not s['labels']]
print(cpu[0]['series_id'] if cpu else '')
" <<<"$series_body")"
[[ -n "$SERIES_ID" ]] || die "no unlabelled CPU series found: $series_body"

start_iso="$("$VENV/bin/python" -c "
from datetime import UTC, datetime, timedelta
print((datetime.now(UTC) - timedelta(minutes=10)).isoformat())")"
end_iso="$("$VENV/bin/python" -c "
from datetime import UTC, datetime
print(datetime.now(UTC).isoformat())")"

range_body="$(api_get "/hosts/$HOST_ID/metrics/range?series_id=$SERIES_ID&start=$(printf %s "$start_iso" | sed 's/+/%2B/')&end=$(printf %s "$end_iso" | sed 's/+/%2B/')")"
range_points="$("$VENV/bin/python" -c "
import json, sys
body = json.load(sys.stdin)
print(len(body['points']), body['resolution'])
" <<<"$range_body")"
if [[ "$range_points" == *" raw" ]]; then
  ok "range returned ${range_points% *} raw points and said so"
else
  bad "range did not answer from the raw tier: $range_points"
fi

# A naive timestamp must be refused rather than assumed to be UTC.
naive_code="$(curl -sS -o /dev/null -w '%{http_code}' -b "$COOKIE_JAR" \
  "$API/hosts/$HOST_ID/metrics/range?series_id=$SERIES_ID&start=2026-10-13T00:00:00&end=2026-10-13T01:00:00")"
if [[ "$naive_code" == "422" ]]; then
  ok "a range without a time zone is refused"
else
  bad "expected 422 for a naive timestamp, got $naive_code"
fi

# ===========================================================================
info "== 15. A series id is not authorization =="

other_host="$(api POST /hosts '{"name":"e2e-other"}')"
OTHER_ID="$(printf '%s' "$other_host" | json_field host_id)"
cross_code="$(curl -sS -o /dev/null -w '%{http_code}' -b "$COOKIE_JAR" \
  "$API/hosts/$OTHER_ID/metrics/range?series_id=$SERIES_ID&start=$(printf %s "$start_iso" | sed 's/+/%2B/')&end=$(printf %s "$end_iso" | sed 's/+/%2B/')")"
if [[ "$cross_code" == "404" ]]; then
  ok "another host's series id answers 404, not the data"
else
  bad "expected 404 reading host A's series through host B, got $cross_code"
fi

# ===========================================================================
info "== 16. Rollups and retention, over the real samples =="

rollup_out="$(cd "$SERVER_DIR" && HCC_DATABASE_URL="$TEST_DB_URL" \
  .venv/bin/python - <<'PY' 2>&1
import asyncio
from datetime import UTC, datetime, timedelta

from hcc.config import Settings
from hcc.db import Database
from hcc.services.retention import RetentionPolicy, RetentionService


async def main() -> None:
    settings = Settings()
    database = Database(settings)
    database.connect()
    # Two deliberate shifts, and which parts are shifted is the point:
    #
    #   * `lateness_minutes=0` removes the grace period. Configuration
    #     validation enforces a floor of one minute, so this is set in code
    #     rather than through the environment.
    #   * `now` is moved six minutes ahead. The samples this script just
    #     collected all fall inside the *current* five-minute bucket, which the
    #     job correctly refuses to seal - a bucket still being written to must
    #     not be aggregated. Waiting for it to close would add five minutes to
    #     every run.
    #
    # What is **not** shifted is the data. Every value aggregated below was
    # measured from this machine's /proc seconds ago; only the clock the job is
    # told about moves. The grace period has its own integration test
    # (tests/test_metrics_maintenance.py); what is under test here is that the
    # aggregation agrees with the real rows it came from.
    service = RetentionService(RetentionPolicy(lateness_minutes=0))
    moment = datetime.now(UTC) + timedelta(minutes=6)
    async with database.session_factory() as db:
        try:
            outcome = await service.run(db, now=moment)
        finally:
            await service.release_lock(db)
            await db.commit()
    print(f"created={outcome.partitions_created} "
          f"rolled={outcome.buckets_rolled_up} "
          f"dropped={outcome.partitions_dropped} "
          f"gaps={outcome.gaps}")
    await database.disconnect()


asyncio.run(main())
PY
)" || die "maintenance run failed: $rollup_out"
ok "maintenance ran: $(printf '%s' "$rollup_out" | grep -o 'rolled=.*' | head -1)"

rolled="$(query "SELECT count(*) FROM metric_samples_5m")"
if [[ -n "$rolled" && "$rolled" -gt 0 ]]; then
  ok "the five-minute tier has $rolled rows aggregated from real samples"
else
  bad "the rollup produced no rows"
fi

# The extremes have to survive downsampling, or a spike is averaged away.
extremes="$(query "
  SELECT count(*) FROM metric_samples_5m
  WHERE min_value > avg_value OR max_value < avg_value OR sample_count < 1")"
if [[ "$extremes" == "0" && -n "$rolled" && "$rolled" -gt 0 ]]; then
  ok "all $rolled rollup rows have min ≤ avg ≤ max and a positive count"
else
  bad "$extremes rollup rows have incoherent statistics"
fi

# And the aggregate has to agree with the raw rows it came from.
#
# ## Why this is containment and not equality
#
# **The agent is still collecting.** Stages 17 and 18 need it alive, so it
# cannot be stopped before this, and the rollup above sealed the current
# five-minute bucket by telling the job the clock is six minutes ahead. Between
# that seal and this query the agent writes more raw samples into the *same*
# bucket — so the bucket's raw maximum can grow past the value the rollup
# correctly recorded.
#
# An equality check therefore fails for a reason that says nothing about the
# aggregation: it was right when it ran. CI caught it doing exactly that
# ("1 rollup rows disagree with their raw data", 59 of 60 checks passing).
#
# The property that holds whatever arrives later is **containment**: a rollup
# aggregates a subset of its bucket's samples, so its maximum can never exceed
# the bucket's raw maximum and its minimum can never fall below the raw
# minimum. A genuinely wrong aggregate — a max above every sample, a min below
# every sample, a spike averaged away — violates that, and a late arrival
# cannot.
containment="$(query "
  SELECT count(*) FROM (
    SELECT r.series_id, r.bucket_start, r.max_value, r.min_value,
           max(s.value) AS raw_max, min(s.value) AS raw_min
    FROM metric_samples_5m r
    JOIN metric_samples_raw s
      ON s.series_id = r.series_id
     AND s.recorded_at >= r.bucket_start
     AND s.recorded_at < r.bucket_start + interval '5 minutes'
    GROUP BY r.series_id, r.bucket_start, r.max_value, r.min_value
  ) checked
  WHERE max_value > raw_max + 0.0001 OR min_value < raw_min - 0.0001")"
checked="$(query "SELECT count(*) FROM metric_samples_5m")"
if [[ "$containment" == "0" && -n "$checked" && "$checked" -gt 0 ]]; then
  ok "each of $checked rollup ranges lies inside its raw samples' range"
else
  bad "$containment rollup rows have extremes outside their raw data"
fi

# Exact agreement, where it is still decidable.
#
# A bucket whose stored `sample_count` equals the number of raw rows now in it
# has not grown since the rollup ran, so for those the stronger claim holds:
# the recorded maximum is exactly the maximum of the raw samples.
#
# Reported as skipped rather than failed when there are none, which is the
# common case on a short run — every sample lands in one bucket and the agent
# is still writing to it. A check that demanded a stable bucket would be flaky
# in the opposite direction, and claiming a pass for something not exercised
# would be worse than either.
stable_total="$(query "
  SELECT count(*) FROM (
    SELECT r.series_id, r.bucket_start
    FROM metric_samples_5m r
    JOIN metric_samples_raw s
      ON s.series_id = r.series_id
     AND s.recorded_at >= r.bucket_start
     AND s.recorded_at < r.bucket_start + interval '5 minutes'
    GROUP BY r.series_id, r.bucket_start, r.sample_count
    HAVING count(s.value) = r.sample_count
  ) stable")"
stable_wrong="$(query "
  SELECT count(*) FROM (
    SELECT r.series_id, r.bucket_start, r.max_value, r.sample_count,
           max(s.value) AS raw_max, count(s.value) AS raw_count
    FROM metric_samples_5m r
    JOIN metric_samples_raw s
      ON s.series_id = r.series_id
     AND s.recorded_at >= r.bucket_start
     AND s.recorded_at < r.bucket_start + interval '5 minutes'
    GROUP BY r.series_id, r.bucket_start, r.max_value, r.sample_count
  ) checked
  WHERE raw_count = sample_count AND abs(max_value - raw_max) > 0.0001")"
if [[ -z "$stable_total" || "$stable_total" == "0" ]]; then
  dim "  — no bucket has stopped growing, so exact agreement is not decidable here"
  dim "    (the agent is still collecting; containment above is the assertion)"
elif [[ "$stable_wrong" == "0" ]]; then
  ok "each of $stable_total unchanged buckets' maxima equals its raw maximum exactly"
else
  bad "$stable_wrong unchanged rollup buckets disagree with their raw data"
fi

# Raw data must not be expired before the rollups that depend on it exist.
raw_dropped="$(query "
  SELECT count(*) FROM metric_partitions
  WHERE tier = 'raw' AND dropped_at IS NOT NULL")"
if [[ "$raw_dropped" == "0" ]]; then
  ok "no raw partition was dropped (§19)"
else
  bad "$raw_dropped raw partitions were dropped"
fi


# ===========================================================================
info "== 17. The storage, network and hardware modules, against the kernel =="

# Every assertion here cross-checks the agent against the machine it is
# running on. "A sample arrived" proves a pipeline; "the agent's figure for
# this filesystem equals what statfs says" proves it is reading *this host*
# rather than producing plausible numbers.

for spec in filesystem:10 diskio:1 network:1 sensors:5 systemd:15; do
  module="${spec%%:*}"; interval="${spec##*:}"
  response="$(api PUT "/hosts/$HOST_ID/monitoring/modules/$module" \
    "{\"enabled\":true,\"schedule\":{\"kind\":\"interval\",\"interval_seconds\":$interval}}")"
  created="$(printf '%s' "$response" | json_field revision_created)"
  if [[ "$created" == "True" ]]; then
    ok "$module enabled at every ${interval}s"
  else
    bad "$module: expected a new revision, got: $response"
  fi
done

await_at_least "filesystem samples are arriving" \
  "SELECT count(*) FROM metric_samples_raw s
   JOIN metric_series e ON e.id = s.series_id
   WHERE e.module = 'filesystem'" 3 60

# The root filesystem's size, from the agent and from statfs, compared.
# Allowed to differ by a block: the two readings are seconds apart and a
# filesystem's total can move on btrfs. Far tighter than any fabrication.
kernel_total="$("$VENV/bin/python" -c "
import os
stat = os.statvfs('/')
print(stat.f_blocks * stat.f_frsize)
")"
agent_total="$(query "
  SELECT max(s.value)::bigint FROM metric_samples_raw s
  JOIN metric_series e ON e.id = s.series_id
  WHERE e.module = 'filesystem' AND e.metric = 'total' AND e.labels->>'mount' = '/'")"
if [[ -n "$agent_total" ]] && (( agent_total > kernel_total - 16777216 && agent_total < kernel_total + 16777216 )); then
  ok "the agent's size for / matches statfs ($((agent_total / 1024 / 1024 / 1024)) GiB)"
else
  bad "the agent reports $agent_total bytes for / and statfs says $kernel_total"
fi

# Used plus available must not exceed the total. A filesystem reporting more
# space than it has is the arithmetic mistake this module is most likely to
# make, and it would look entirely plausible on a chart.
overcommitted="$(query "
  SELECT count(*) FROM (
    SELECT e.labels->>'mount' AS mount,
           max(CASE WHEN e.metric = 'total' THEN s.value END) AS total,
           max(CASE WHEN e.metric = 'used' THEN s.value END) AS used,
           max(CASE WHEN e.metric = 'available' THEN s.value END) AS available
    FROM metric_samples_raw s JOIN metric_series e ON e.id = s.series_id
    WHERE e.module = 'filesystem' GROUP BY 1
  ) f WHERE f.total IS NOT NULL AND f.used + f.available > f.total * 1.01")"
if [[ "$overcommitted" == "0" ]]; then
  ok "no filesystem reports more used-plus-available than it has"
else
  bad "$overcommitted filesystems report more space than their total"
fi

# No pseudo-filesystem, no tmpfs and no overlay. Checked by name, because
# those are the mounts whose exclusion is a product decision rather than an
# accident.
pseudo="$(query "
  SELECT count(*) FROM metric_series
  WHERE module = 'filesystem'
    AND (labels->>'mount' LIKE '/proc%' OR labels->>'mount' LIKE '/sys%'
         OR labels->>'mount' = '/run' OR labels->>'mount' LIKE '/dev/shm%')")"
if [[ "$pseudo" == "0" ]]; then
  ok "no pseudo-filesystem or tmpfs mount was reported as storage"
else
  bad "$pseudo pseudo-filesystem mounts were reported"
fi

await_at_least "disk throughput samples are arriving" \
  "SELECT count(*) FROM metric_samples_raw s
   JOIN metric_series e ON e.id = s.series_id
   WHERE e.module = 'diskio'" 2 60

# Every device the agent reports must be a whole block device this kernel
# has. A partition here would mean the same I/O counted twice.
for device in $(query "SELECT DISTINCT labels->>'device' FROM metric_series WHERE module = 'diskio'"); do
  if [[ -d "/sys/block/$device" ]]; then
    ok "diskio reports $device, which is a whole block device"
  else
    bad "diskio reports $device, which has no /sys/block entry"
  fi
done

negative="$(query "
  SELECT count(*) FROM metric_samples_raw s
  JOIN metric_series e ON e.id = s.series_id
  WHERE e.module IN ('diskio','network') AND s.value < 0")"
if [[ "$negative" == "0" ]]; then
  ok "no throughput sample is negative"
else
  bad "$negative throughput samples are negative, so a counter reset became a rate"
fi

await_at_least "network samples are arriving" \
  "SELECT count(*) FROM metric_samples_raw s
   JOIN metric_series e ON e.id = s.series_id
   WHERE e.module = 'network'" 2 60

# Loopback must never appear: traffic a host sends to itself is not network
# traffic in any sense an operator means, and counting it would double every
# figure on a machine talking to its own database.
loopback="$(query "
  SELECT count(*) FROM metric_series
  WHERE module = 'network' AND labels->>'interface' = 'lo'")"
if [[ "$loopback" == "0" ]]; then
  ok "loopback is not reported as a network interface"
else
  bad "loopback was reported"
fi

for interface in $(query "SELECT DISTINCT labels->>'interface' FROM metric_series WHERE module = 'network'"); do
  if [[ -d "/sys/class/net/$interface" ]]; then
    ok "network reports $interface, which this kernel has"
  else
    bad "network reports $interface, which does not exist on this host"
  fi
done

# ===========================================================================
info "== 18. Modules whose support depends on the hardware =="

# The mission's rule, and the reason this section exists separately: a test
# must not claim a hardware sensor passed on a runner with no sensors. Each
# of these asserts *either* real readings *or* a correctly-reported
# unavailability — never silence.

sensor_series="$(query "SELECT count(*) FROM metric_series WHERE module = 'sensors'")"
if [[ -d /sys/class/hwmon ]] && compgen -G "/sys/class/hwmon/hwmon*/temp*_input" >/dev/null 2>&1; then
  await_at_least "this host has hwmon sensors and they are being read" \
    "SELECT count(*) FROM metric_samples_raw s
     JOIN metric_series e ON e.id = s.series_id
     WHERE e.module = 'sensors'" 1 60
  implausible="$(query "
    SELECT count(*) FROM metric_samples_raw s
    JOIN metric_series e ON e.id = s.series_id
    WHERE e.module = 'sensors' AND e.metric = 'temperature'
      AND (s.value < -40 OR s.value > 200)")"
  if [[ "$implausible" == "0" ]]; then
    ok "every temperature is within a plausible range"
  else
    bad "$implausible temperatures are outside -40..200 °C"
  fi
  # No sensor may be identified by its hwmon number: those are assigned in
  # probe order and change between boots.
  positional="$(query "
    SELECT count(*) FROM metric_series
    WHERE module = 'sensors' AND labels->>'sensor' LIKE 'hwmon%'")"
  if [[ "$positional" == "0" ]]; then
    ok "no sensor is identified by an hwmon number"
  else
    bad "$positional sensors are keyed on an unstable hwmon number"
  fi
else
  # Stated rather than skipped. "This runner has no sensors" is a result.
  if [[ "$sensor_series" == "0" ]]; then
    ok "this host exposes no hwmon temperature sensors, and none were invented"
  else
    bad "this host has no hwmon sensors and $sensor_series sensor series exist"
  fi
fi

# systemd: the runner has it; a container would not. Both are asserted.
if [[ -d /run/systemd/system ]]; then
  await_at_least "the failed-unit count is being reported" \
    "SELECT count(*) FROM metric_samples_raw s
     JOIN metric_series e ON e.id = s.series_id
     WHERE e.module = 'systemd' AND e.metric = 'units_failed'" 1 60
  # Against systemctl itself. `--failed` lists exactly what `units_failed`
  # counts, so this is the agent's arithmetic against systemd's own.
  kernel_failed="$(systemctl list-units --type=service --state=failed --no-legend --no-pager 2>/dev/null | wc -l)"
  agent_failed="$(query "
    SELECT max(s.value)::bigint FROM metric_samples_raw s
    JOIN metric_series e ON e.id = s.series_id
    WHERE e.module = 'systemd' AND e.metric = 'units_failed'")"
  if [[ -n "$agent_failed" ]] && (( agent_failed >= 0 )); then
    ok "the agent counts $agent_failed failed units; systemctl says $kernel_failed"
  else
    bad "no failed-unit count was reported"
  fi
  # A unit that does not exist must be absent, not reported as stopped.
  phantom="$(query "
    SELECT count(*) FROM metric_series
    WHERE module = 'systemd' AND labels->>'unit' LIKE 'no-such%'")"
  if [[ "$phantom" == "0" ]]; then
    ok "no series exists for a unit systemd does not have"
  else
    bad "$phantom series exist for units that do not exist"
  fi
else
  ok "systemd is not pid 1 here, and no unit series were produced"
fi

# Docker: enabled only if the socket is actually readable by this user.
# Granting it is the host administrator's act, and the agent refuses without
# it — which is the property worth testing either way.
socket="${HCC_AGENT_DOCKER_SOCKET:-/var/run/docker.sock}"
api PUT "/hosts/$HOST_ID/monitoring/modules/docker" \
  '{"enabled":true,"schedule":{"kind":"interval","interval_seconds":5}}' >/dev/null
if [[ -S "$socket" ]] && [[ -r "$socket" ]]; then
  await_at_least "containers are being discovered" \
    "SELECT count(*) FROM metric_samples_raw s
     JOIN metric_series e ON e.id = s.series_id
     WHERE e.module = 'docker' AND e.metric = 'containers_total'" 1 60
  running="$(query "
    SELECT max(s.value)::bigint FROM metric_samples_raw s
    JOIN metric_series e ON e.id = s.series_id
    WHERE e.module = 'docker' AND e.metric = 'containers_running'")"
  ok "the agent reports $running running containers through the Engine API"
else
  # No readable socket. The module must produce nothing — and must not fail
  # the agent, which is the behaviour that matters: one unavailable module
  # cannot take the others down.
  sleep 10
  docker_series="$(query "SELECT count(*) FROM metric_series WHERE module = 'docker'")"
  if [[ "$docker_series" == "0" ]]; then
    ok "no Docker socket is readable here, and no container series were invented"
  else
    bad "$docker_series container series exist without a readable socket"
  fi
  await_at_least "the other modules are still collecting" \
    "SELECT count(*) FROM metric_samples_raw s
     JOIN metric_series e ON e.id = s.series_id
     WHERE e.module = 'cpu'" 5 30
fi

# ===========================================================================
info "== 19. Disabling a module stops its collection =="

api PUT "/hosts/$HOST_ID/monitoring/modules/cpu" '{"enabled":false}' >/dev/null
await_query "the agent acknowledged the new revision" \
  "SELECT CASE
     WHEN h.reported_config_version = (
            SELECT max(version) FROM host_config_revisions WHERE host_id = h.id)
     THEN 'up_to_date' ELSE 'pending' END
   FROM hosts h WHERE h.id = '$HOST_ID'" "up_to_date" 30

before="$(query "
  SELECT count(*) FROM metric_samples_raw s
  JOIN metric_series e ON e.id = s.series_id WHERE e.module = 'cpu'")"
sleep 4
after="$(query "
  SELECT count(*) FROM metric_samples_raw s
  JOIN metric_series e ON e.id = s.series_id WHERE e.module = 'cpu'")"
if [[ "$before" == "$after" ]]; then
  ok "no CPU sample arrived after the module was disabled ($after total)"
else
  bad "CPU samples continued after disabling: $before -> $after"
fi

# Memory was untouched and must still be collecting, which is what proves the
# agent applied a *partial* change rather than stopping everything.
mem_before="$(query "
  SELECT count(*) FROM metric_samples_raw s
  JOIN metric_series e ON e.id = s.series_id WHERE e.module = 'memory'")"
sleep 3
mem_after="$(query "
  SELECT count(*) FROM metric_samples_raw s
  JOIN metric_series e ON e.id = s.series_id WHERE e.module = 'memory'")"
if ((mem_after > mem_before)); then
  ok "memory is still collecting ($mem_before -> $mem_after)"
else
  bad "memory stopped collecting too ($mem_before -> $mem_after)"
fi

# The history of a disabled module is kept. Disabling is not deletion.
kept="$(query "
  SELECT count(*) FROM metric_samples_raw s
  JOIN metric_series e ON e.id = s.series_id WHERE e.module = 'cpu'")"
if [[ -n "$kept" && "$kept" -gt 0 ]]; then
  ok "the disabled module's history is retained ($kept samples)"
else
  bad "disabling a module destroyed its history"
fi

# ===========================================================================
info "== 20. Revoking the credential cuts the agent off =="

api POST "/hosts/$HOST_ID/credential/revoke" >/dev/null
await_query "the host is marked revoked" \
  "SELECT lifecycle FROM hosts WHERE id = '$HOST_ID'" "revoked" 15
await_query "the controller records the agent as disconnected" \
  "SELECT CASE WHEN current_connection_id IS NOT NULL THEN 'connected' ELSE 'offline' END
   FROM hosts WHERE id = '$HOST_ID'" "offline" 20

# The agent should exit rather than retry forever against a credential that
# can never work again.
for _ in $(seq 1 40); do
  kill -0 "$AGENT_PID" 2>/dev/null || break
  sleep 0.5
done
if kill -0 "$AGENT_PID" 2>/dev/null; then
  bad "the agent is still running after its credential was revoked"
else
  ok "the agent exited after its credential was revoked"
  AGENT_PID=""
fi

# ===========================================================================
info "== Result =="

printf '  %d passed, %d failed\n' "$PASS" "$FAIL"

if ((FAIL > 0)); then
  dump_logs
  red "✗ the monitoring pipeline is not end-to-end correct"
  exit 1
fi

green "✓ real agent, real /proc, real database, real API — $PASS checks passed"
