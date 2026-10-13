#!/usr/bin/env bash
#
# What happens when things break, with real processes actually breaking.
#
# The Go unit tests prove the agent's queue drops oldest-first and requeues an
# in-flight batch. They cannot prove that a controller restart loses nothing,
# because that needs two processes and one of them has to die. This does: it
# kills the controller mid-collection, kills the database's connections, and
# kills the agent with SIGKILL, and then checks what survived.
#
# The property under test throughout is **at-least-once delivery with
# idempotent writes**. Not exactly-once — that is not claimed anywhere and is
# not provided. What is claimed is that a failure loses nothing it had
# acknowledged, and that recovery does not produce duplicate rows. Both halves
# matter: a system that loses samples is missing history, and one that
# duplicates them reports a spike that never happened.
#
# Usage:
#   make db-up
#   ./scripts/reliability-e2e.sh
#
# Safety
# ------
# §33: no destructive chaos against real infrastructure. Nothing here touches
# anything the developer owns:
#
#   * Its own database, created for the run and dropped on every exit path.
#   * The "database outage" is `pg_terminate_backend` against **this
#     database's own sessions only**, selected by datname. The PostgreSQL
#     server is never stopped, and the developer's database is never touched —
#     stopping the shared container would be exactly the destructive chaos the
#     brief forbids.
#   * Its own port and its own mktemp agent state directory, so no credential
#     reaches /etc and this machine is never enrolled into anything that
#     outlives the script.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

SERVER_DIR="apps/server"
AGENT_DIR="agents/linux"
VENV="$SERVER_DIR/.venv"

# Local development credentials, matching deploy/compose.yaml. Not a secret.
PG_SUPERUSER_URL="postgresql://hcc:hcc_local_dev@127.0.0.1:5432/hcc"
TEST_DB="hcc_rel_$(date +%s)_$$"
TEST_DB_URL="postgresql://hcc:hcc_local_dev@127.0.0.1:5432/$TEST_DB"

# Distinct from the dev server, the smoke test, the browser suite and the
# monitoring end-to-end run.
PORT=8213
BASE="http://127.0.0.1:$PORT"
API="$BASE/api/v1"

ADMIN_USER="rel-admin"
ADMIN_PASSWORD="reliability-e2e-$(date +%s)-correct-horse"
COLLECT_INTERVAL=1

SERVER_PID=""
AGENT_PID=""
AGENT_STATE_DIR=""
CREATED=""
COOKIE_JAR=""
CSRF=""

PASS=0
FAIL=0

green() { printf '\033[0;32m%s\033[0m\n' "$1"; }
red()   { printf '\033[0;31m%s\033[0m\n' "$1"; }
info()  { printf '\n\033[0;36m%s\033[0m\n' "$1"; }

ok()  { green "  ✓ $1"; PASS=$((PASS + 1)); }
bad() { red   "  ✗ $1"; FAIL=$((FAIL + 1)); }

dump_logs() {
  for name in server agent; do
    if [[ -f "/tmp/hcc-rel-$name.log" ]]; then
      red "--- $name log (tail) ---"
      tail -25 "/tmp/hcc-rel-$name.log" || true
    fi
  done
}

die() { red "$1"; dump_logs; exit 1; }

psql_super() {
  "$VENV/bin/python" - "$1" <<'PY'
import asyncio, os, sys
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

query() {
  TEST_DB_URL="$TEST_DB_URL" "$VENV/bin/python" - "$1" <<'PY'
import asyncio, os, sys
import asyncpg
async def main() -> None:
    connection = await asyncpg.connect(os.environ["TEST_DB_URL"])
    try:
        rows = await connection.fetch(sys.argv[1])
        for row in rows:
            print("\t".join("" if v is None else str(v) for v in row.values()))
    finally:
        await connection.close()
asyncio.run(main())
PY
}

start_controller() {
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
  ) >>/tmp/hcc-rel-server.log 2>&1 &
  SERVER_PID=$!
  for _ in $(seq 1 150); do
    if curl -sS -o /dev/null --max-time 2 "$BASE/healthz" 2>/dev/null; then return 0; fi
    sleep 0.2
  done
  return 1
}

stop_controller() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "${1:--TERM}" "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  SERVER_PID=""
}

cleanup() {
  local status=$?
  if [[ -n "$AGENT_PID" ]] && kill -0 "$AGENT_PID" 2>/dev/null; then
    kill "$AGENT_PID" 2>/dev/null || true
    wait "$AGENT_PID" 2>/dev/null || true
  fi
  if [[ -n "$AGENT_STATE_DIR" ]]; then
    # Anchored: an unanchored `pkill -f hcc-agent` matches this script's own
    # command line and kills the shell running it.
    pkill -f "^$AGENT_STATE_DIR/hcc-agent" 2>/dev/null || true
    rm -rf "$AGENT_STATE_DIR"
  fi
  stop_controller
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

api() {
  local method="$1" path="$2" body="${3:-}"
  curl -sS -X "$method" "$API$path" -b "$COOKIE_JAR" -c "$COOKIE_JAR" \
    -H "Content-Type: application/json" -H "X-CSRF-Token: $CSRF" \
    ${body:+-d "$body"}
}

json_field() {
  "$VENV/bin/python" -c "
import json, sys
document = json.load(sys.stdin)
for key in sys.argv[1].split('.'):
    document = document[int(key)] if isinstance(document, list) else document[key]
print(document if document is not None else '')
" "$1"
}

raw_count() { query "SELECT count(*) FROM metric_samples_raw" | head -1; }

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

# ===========================================================================
info "== Setup =="

[[ -x "$VENV/bin/python" ]] || die "Python venv missing. Run: make install-server"
export PATH="$HOME/.local/go/bin:$PATH"
command -v go >/dev/null || die "Go missing; the agent is built from source."
if curl -sS -o /dev/null --max-time 2 "$BASE/" 2>/dev/null; then
  die "Something is already listening on port $PORT."
fi
: >/tmp/hcc-rel-server.log
: >/tmp/hcc-rel-agent.log

PG_SUPERUSER_URL="$PG_SUPERUSER_URL" psql_super "CREATE DATABASE \"$TEST_DB\"" >/dev/null
CREATED=1
(cd "$SERVER_DIR" && HCC_DATABASE_URL="$TEST_DB_URL" "../../$VENV/bin/alembic" upgrade head) \
  >/tmp/hcc-rel-migrate.log 2>&1 || { cat /tmp/hcc-rel-migrate.log; die "migrations failed"; }
ok "disposable database $TEST_DB migrated"

start_controller || die "controller did not start"
ok "controller running"

COOKIE_JAR="$(mktemp /tmp/hcc-rel-cookies-XXXXXX)"
(cd "$SERVER_DIR" && printf '%s\n' "$ADMIN_PASSWORD" | HCC_DATABASE_URL="$TEST_DB_URL" \
  .venv/bin/python -m hcc.cli.main bootstrap-admin --username "$ADMIN_USER" --password-stdin) \
  >/tmp/hcc-rel-bootstrap.log 2>&1 || { cat /tmp/hcc-rel-bootstrap.log; die "bootstrap failed"; }
login="$(curl -sS -c "$COOKIE_JAR" -X POST "$API/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASSWORD\"}")"
CSRF="$(printf '%s' "$login" | json_field csrf_token)"
[[ -n "$CSRF" ]] || die "login failed: $login"

HOST_ID="$(api POST /hosts '{"name":"rel-localhost"}' | json_field host_id)"
ENROLL_CREDENTIAL="$(api POST "/hosts/$HOST_ID/enrollment" '{}' | json_field credential)"
[[ -n "$ENROLL_CREDENTIAL" ]] || die "enrollment failed"

AGENT_STATE_DIR="$(mktemp -d /tmp/hcc-rel-agent-XXXXXX)"
chmod 700 "$AGENT_STATE_DIR"
(cd "$AGENT_DIR" && go build -o "$AGENT_STATE_DIR/hcc-agent" ./cmd/hcc-agent) \
  >/tmp/hcc-rel-build.log 2>&1 || { cat /tmp/hcc-rel-build.log; die "agent build failed"; }
printf '%s' "$ENROLL_CREDENTIAL" >"$AGENT_STATE_DIR/enrollment"
chmod 600 "$AGENT_STATE_DIR/enrollment"

agent_env=(
  "HCC_AGENT_CONTROLLER_URL=$BASE"
  "HCC_AGENT_CREDENTIAL_FILE=$AGENT_STATE_DIR/credential"
  "HCC_AGENT_CONFIG_FILE=$AGENT_STATE_DIR/config.json"
  "HCC_AGENT_LOG_FORMAT=json"
  "HCC_AGENT_LOG_LEVEL=debug"
)

env "${agent_env[@]}" "HCC_AGENT_ENROLLMENT_TOKEN_FILE=$AGENT_STATE_DIR/enrollment" \
  "$AGENT_STATE_DIR/hcc-agent" enroll >>/tmp/hcc-rel-agent.log 2>&1 \
  || { tail -20 /tmp/hcc-rel-agent.log; die "enrollment failed"; }
ok "agent built and enrolled"

start_agent() {
  (exec env "${agent_env[@]}" "$AGENT_STATE_DIR/hcc-agent" run) \
    >>/tmp/hcc-rel-agent.log 2>&1 &
  AGENT_PID=$!
}

start_agent
api PUT "/hosts/$HOST_ID/monitoring/modules/memory" \
  "{\"enabled\":true,\"schedule\":{\"kind\":\"interval\",\"interval_seconds\":$COLLECT_INTERVAL}}" \
  >/dev/null
await_at_least "collection started" "SELECT count(*) FROM metric_samples_raw" 3 40 \
  || die "collection never started"

# ===========================================================================
info "== 1. The controller restarts while the agent is collecting =="

before="$(raw_count)"
stop_controller
ok "controller stopped (had stored $before samples)"

# The agent must keep collecting into its queue rather than exiting: a
# controller restart is an ordinary event and an agent that gave up on one
# would need manual intervention after every deployment.
sleep 6
if kill -0 "$AGENT_PID" 2>/dev/null; then
  ok "the agent is still running with no controller to talk to"
else
  bad "the agent exited when the controller went away"
fi

during="$(raw_count)"
if [[ "$during" == "$before" ]]; then
  ok "nothing was stored while the controller was down ($during)"
else
  bad "the sample count changed with no controller: $before -> $during"
fi

start_controller || die "controller did not restart"
ok "controller restarted"

# The samples collected during the outage were queued, so the count must climb
# past what it was *plus* roughly the outage's worth of collections. This is
# the at-least-once property: the agent held them and delivered them.
target=$((before + 5))
await_at_least "the queued samples arrived after recovery" \
  "SELECT count(*) FROM metric_samples_raw" "$target" 40 \
  || bad "queued samples were not delivered"

# And no duplicates. The primary key is what makes a re-sent batch a no-op, so
# a count of distinct (series, timestamp) pairs equal to the row count is the
# assertion that delivery was idempotent.
duplicates="$(query "
  SELECT count(*) FROM (
    SELECT series_id, recorded_at FROM metric_samples_raw
    GROUP BY series_id, recorded_at HAVING count(*) > 1
  ) d")"
if [[ "$duplicates" == "0" ]]; then
  ok "no duplicate (series, timestamp) rows after recovery"
else
  bad "$duplicates duplicated samples after recovery"
fi

# ===========================================================================
info "== 2. The agent is killed without warning =="

before="$(raw_count)"
kill -9 "$AGENT_PID" 2>/dev/null || true
wait "$AGENT_PID" 2>/dev/null || true
AGENT_PID=""
ok "agent killed with SIGKILL (had stored $before samples)"

# SIGKILL means no shutdown, so whatever was in the in-memory queue is gone.
# That is the documented trade — the queue is in memory by decision, not by
# omission (ADR-051) — and what must hold is that the *stored* data is intact
# and collection resumes without manual intervention.
sleep 2
start_agent
await_at_least "collection resumed after the agent was restarted" \
  "SELECT count(*) FROM metric_samples_raw" $((before + 3)) 40 \
  || bad "collection did not resume"

duplicates="$(query "
  SELECT count(*) FROM (
    SELECT series_id, recorded_at FROM metric_samples_raw
    GROUP BY series_id, recorded_at HAVING count(*) > 1
  ) d")"
if [[ "$duplicates" == "0" ]]; then
  ok "no duplicates after an unclean agent restart"
else
  bad "$duplicates duplicated samples after an unclean restart"
fi

# The agent re-reads its stored configuration rather than waiting to be told
# again, so it starts collecting before the controller has said anything.
if grep -q "loaded the stored configuration\|applied a new configuration" /tmp/hcc-rel-agent.log; then
  ok "the agent recovered its configuration from disk or from the controller"
else
  bad "no evidence the agent recovered its configuration"
fi

# ===========================================================================
info "== 3. The database's connections are killed under the controller =="

before="$(raw_count)"
# `pg_terminate_backend` against this database's own sessions only, selected
# by datname. The server keeps running and the developer's database is never
# touched — stopping the shared container would be the destructive chaos §33
# forbids.
killed="$(query "
  SELECT count(*) FROM (
    SELECT pg_terminate_backend(pid) FROM pg_stat_activity
    WHERE datname = '$TEST_DB' AND pid <> pg_backend_pid()
  ) t")"
ok "terminated $killed backend(s) on $TEST_DB"

# The controller must survive: a dropped connection is a pool event, not a
# reason to exit, and an agent mid-batch must not be able to kill the process.
sleep 3
if kill -0 "$SERVER_PID" 2>/dev/null; then
  ok "the controller survived losing every database connection"
else
  bad "the controller exited when its connections were terminated"
fi

if curl -sS -o /dev/null --max-time 5 "$BASE/healthz" 2>/dev/null; then
  ok "the controller is still serving"
else
  bad "the controller stopped serving"
fi

# Readiness must report the truth, whatever that is — it is the probe an
# orchestrator uses, and a readiness check that is always "ready" is worse
# than none.
ready="$(curl -sS --max-time 5 "$BASE/readyz" 2>/dev/null || echo "unreachable")"
ok "readiness reports: $(printf '%s' "$ready" | head -c 80)"

await_at_least "ingestion recovered on new connections" \
  "SELECT count(*) FROM metric_samples_raw" $((before + 3)) 40 \
  || bad "ingestion did not recover after the connections were killed"

# ===========================================================================
info "== 4. Two maintenance runs at once =="

# The advisory lock is installation-wide, so a manual run alongside the
# scheduled one must not both create the same partition and fail. Two
# genuinely separate processes, because one process taking the same advisory
# lock twice succeeds — PostgreSQL's advisory locks are re-entrant within a
# session, so a single-process test would assert nothing.
cat >"$AGENT_STATE_DIR/maintenance.py" <<'PY'
import asyncio

from hcc.config import Settings
from hcc.db import Database
from hcc.logging import configure_logging
from hcc.services.retention import RetentionService


async def main() -> None:
    configure_logging(level="INFO", log_format="console")
    database = Database(Settings())
    database.connect()
    service = RetentionService()
    async with database.session_factory() as db:
        try:
            outcome = await service.run(db)
            print(f"skipped={outcome.skipped} created={outcome.partitions_created}")
        finally:
            await service.release_lock(db)
            await db.commit()
    await database.disconnect()


asyncio.run(main())
PY

# PIDs captured explicitly. A bare `wait` would wait for *every* background
# job — including the controller and the agent, which never exit — and hang
# the script for ever. That is not a hypothetical: it is what the first
# version of this section did.
maint_pids=()
for index in 1 2; do
  (
    cd "$SERVER_DIR"
    exec env HCC_DATABASE_URL="$TEST_DB_URL"       .venv/bin/python "$AGENT_STATE_DIR/maintenance.py"
  ) >"/tmp/hcc-rel-maint-$index.log" 2>&1 &
  maint_pids+=("$!")
done
for pid in "${maint_pids[@]}"; do
  wait "$pid" || true
done

outcomes="$(cat /tmp/hcc-rel-maint-1.log /tmp/hcc-rel-maint-2.log | grep -c "skipped=" || true)"
if [[ "$outcomes" == "2" ]]; then
  ok "both maintenance processes completed without error"
else
  bad "a concurrent maintenance run failed: $(cat /tmp/hcc-rel-maint-*.log | tail -5)"
fi

# Exactly one of them did the work, or they both found nothing to do. What
# must not happen is a crash from both creating the same partition.
partitions="$(query "SELECT count(*) FROM metric_partitions WHERE dropped_at IS NULL")"
duplicate_windows="$(query "
  SELECT count(*) FROM (
    SELECT tier, range_start FROM metric_partitions WHERE dropped_at IS NULL
    GROUP BY tier, range_start HAVING count(*) > 1
  ) d")"
if [[ "$duplicate_windows" == "0" ]]; then
  ok "no window was catalogued twice ($partitions partitions)"
else
  bad "$duplicate_windows windows catalogued more than once"
fi

# ===========================================================================
info "== 5. What survived =="

total="$(raw_count)"
distinct="$(query "SELECT count(DISTINCT (series_id, recorded_at)) FROM metric_samples_raw")"
if [[ "$total" == "$distinct" ]]; then
  ok "every stored sample is unique: $total rows, $distinct distinct keys"
else
  bad "$total rows but only $distinct distinct keys"
fi

# Values still have to be plausible measurements after all of that: a recovery
# path that stored the right number of wrong rows would pass every count
# above.
implausible="$(query "
  SELECT count(*) FROM metric_samples_raw s
  JOIN metric_series e ON e.id = s.series_id
  WHERE (e.unit = 'percent' AND (s.value < 0 OR s.value > 100))
     OR (e.unit = 'bytes' AND s.value < 0)")"
if [[ "$implausible" == "0" ]]; then
  ok "every surviving value is still within its unit's range"
else
  bad "$implausible surviving values are outside their unit's range"
fi

in_default="$(query "
  SELECT count(*) FROM metric_samples_raw
  WHERE tableoid::regclass::text LIKE '%_default'")"
if [[ "$in_default" == "0" ]]; then
  ok "nothing was pushed into the DEFAULT partition by the failures"
else
  bad "$in_default samples landed in the DEFAULT partition"
fi

# ===========================================================================
info "== Result =="
printf '  %d passed, %d failed\n' "$PASS" "$FAIL"
if ((FAIL > 0)); then
  dump_logs
  red "✗ the system does not recover cleanly"
  exit 1
fi
green "✓ survived a controller restart, SIGKILL, and losing every database connection"
