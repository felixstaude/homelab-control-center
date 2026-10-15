#!/usr/bin/env bash
#
# An isolated Homelab Control Center you can actually use, in one command.
#
#   ./scripts/demo.sh up        start it and print how to sign in
#   ./scripts/demo.sh status    what is running, and whether data is arriving
#   ./scripts/demo.sh logs      follow the controller and agent logs
#   ./scripts/demo.sh down      stop it, keeping the data
#   ./scripts/demo.sh down --clean   stop it and delete everything
#
# Or: make demo / make demo-status / make demo-logs / make demo-down.
#
# # What it starts
#
# A **real** controller against a **real** PostgreSQL, serving the **real**
# built web client, with a **real** agent process reporting **real** telemetry
# from this machine's own /proc and /sys. Nothing here is simulated and no
# sample data is inserted. If a chart has a line in it, that line is this
# computer.
#
# # What it does not touch
#
# Everything is a separate instance, and the separation is the point rather
# than a precaution:
#
#   * its own PostgreSQL **container** (hcc-demo-postgres) on its own
#     **port** (55433) with its own **volume** (hcc-demo-pgdata). The
#     development database on 5432 is not opened, migrated or read.
#   * its own controller on **127.0.0.1:8900**, which is not the development
#     server's 8000 and not any port the test scenarios use.
#   * its own agent state under `.demo/`, so the machine is **not** enrolled
#     into anything, no systemd unit is written, nothing needs root, and
#     `/etc` and `/var/lib` are untouched. `hcc-agent install` is a different
#     command and this is not it.
#   * a generated administrator password, written to `.demo/credentials` with
#     mode 0600. There is no default password in this file and no way to
#     supply one on a command line.
#
# Both ports bind to **127.0.0.1**. Nothing here is reachable from the
# network, and there is no flag to change that: a demo that could be exposed
# accidentally is one that will be.
#
# # Why not Compose
#
# `deploy/production/compose.yaml` is the real deployment and is verified
# against Docker Compose only — podman-compose 1.6.0 hangs on its
# `depends_on: service_completed_successfully`, which is documented in
# `scripts/deployment-e2e.sh` and `docs/DEPLOYMENT.md`. Plenty of people run
# Podman, including on the machine this was written on.
#
# So the demo uses one plain `run` invocation for PostgreSQL — a command both
# CLIs accept identically — and runs the controller and agent as host
# processes from this working tree. That also makes it fast (no image build)
# and makes a code change visible on a restart, which is what a demo is for.
#
# Use `make deployment-e2e` to exercise the containerised deployment itself.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

GREEN='\033[0;32m' RED='\033[0;31m' CYAN='\033[0;36m' YELLOW='\033[0;33m'
DIM='\033[0;90m' BOLD='\033[1m' RESET='\033[0m'

# --- Everything this instance is ------------------------------------------
#
# Named for the demo throughout, so nothing collides with the development
# stack and so `podman ps` tells an operator what a stray container is.
DEMO_DIR="$REPO_ROOT/.demo"
PG_CONTAINER="hcc-demo-postgres"
PG_VOLUME="hcc-demo-pgdata"
PG_PORT=55433
PG_IMAGE="docker.io/library/postgres:17-alpine"
# Throwaway credentials for a loopback-bound container, like
# deploy/compose.yaml's. Not a secret, and not reused for anything.
PG_USER="hcc_demo"
PG_PASSWORD="hcc_demo_local"
PG_DATABASE="hcc_demo"
DATABASE_URL="postgresql://$PG_USER:$PG_PASSWORD@127.0.0.1:$PG_PORT/$PG_DATABASE"

CONTROLLER_PORT=8900
BASE="http://127.0.0.1:$CONTROLLER_PORT"
API="$BASE/api/v1"
ADMIN_USER="demo-admin"

SERVER_DIR="$REPO_ROOT/apps/server"
WEB_DIR="$REPO_ROOT/apps/web"
AGENT_DIR="$REPO_ROOT/agents/linux"
VENV="$SERVER_DIR/.venv"

# The modules the demo turns on. Every one of them reads this machine's own
# kernel interfaces, and every one is **off** until this script enables it —
# the registry cannot express "enabled by default" (ADR-048), so there is no
# path by which starting a demo turns on collection anywhere else.
#
# `swap` is deliberately absent: a host with no swap reports the module
# unavailable, which is correct and looks like a failure to somebody seeing
# the product for the first time.
DEMO_MODULES="cpu:5 memory:5 load:5 uptime:60 filesystem:15 diskio:5 network:5"

green() { printf "${GREEN}%s${RESET}\n" "$1"; }
red()   { printf "${RED}%s${RESET}\n" "$1"; }
info()  { printf "\n${CYAN}%s${RESET}\n" "$1"; }
dim()   { printf "${DIM}%s${RESET}\n" "$1"; }
warn()  { printf "${YELLOW}%s${RESET}\n" "$1"; }
ok()    { printf "${GREEN}  ✓ %s${RESET}\n" "$1"; }
step()  { printf "${DIM}  · %s${RESET}\n" "$1"; }
die()   { red "✗ $1"; exit 1; }

# --- Container runtime ------------------------------------------------------
#
# `docker run` and `podman run` take the same arguments for what this needs,
# so the only difference is the binary. Checked once, named in the error, and
# never assumed.
detect_runtime() {
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    echo docker
  elif command -v podman >/dev/null 2>&1; then
    echo podman
  else
    echo ""
  fi
}

RUNTIME="$(detect_runtime)"

require_runtime() {
  [ -n "$RUNTIME" ] || die "no container runtime found. Install Docker or Podman.
  Fedora/RHEL:  sudo dnf install podman
  Debian:       sudo apt install podman
  Everywhere:   https://docs.docker.com/engine/install/"
}

container_state() {
  "$RUNTIME" inspect --format '{{.State.Status}}' "$PG_CONTAINER" 2>/dev/null || echo "absent"
}

process_alive() {
  # $1 is a pid file. Alive means the file exists and names a running process.
  [ -f "$1" ] || return 1
  local pid
  pid="$(cat "$1" 2>/dev/null || true)"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null
}

port_in_use() {
  # A port held by something that is not this demo is a refusal rather than a
  # confusing bind error three steps later.
  (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && { exec 3>&-; return 0; } || return 1
}

wait_for_url() {
  local label="$1" url="$2" seconds="${3:-60}"
  local deadline=$((SECONDS + seconds))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if curl -fsS -o /dev/null --max-time 3 "$url" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  red "  ✗ $label did not answer at $url within ${seconds}s"
  return 1
}

psql_demo() {
  "$RUNTIME" exec -i "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DATABASE" -tAc "$1"
}

json_field() {
  "$VENV/bin/python" -c "
import json, sys
document = json.load(sys.stdin)
for key in sys.argv[1].split('.'):
    document = document[int(key)] if isinstance(document, list) else document[key]
print('' if document is None else document)
" "$1"
}

# ============================================================================
# up
# ============================================================================

cmd_up() {
  local rebuild=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --rebuild) rebuild=1; shift ;;
      *) die "unknown option for up: $1" ;;
    esac
  done

  printf "\n${BOLD}Homelab Control Center — demo${RESET}\n"

  # --- 0. Preflight ---------------------------------------------------------
  info "Checking what is needed"
  require_runtime
  ok "container runtime: $RUNTIME"
  [ -x "$VENV/bin/python" ] || die "the backend is not installed. Run:  make setup"
  ok "backend virtualenv"
  command -v go >/dev/null 2>&1 || die "Go is not on PATH; the demo builds a real agent from source"
  ok "go $(go version | awk '{print $3}')"
  command -v curl >/dev/null 2>&1 || die "curl is not on PATH"

  if port_in_use "$CONTROLLER_PORT" && ! process_alive "$DEMO_DIR/controller.pid"; then
    die "port $CONTROLLER_PORT is already in use by something that is not this demo.
  Find it with:  ss -ltnp 'sport = :$CONTROLLER_PORT'"
  fi

  mkdir -p "$DEMO_DIR/agent"
  chmod 700 "$DEMO_DIR"

  # --- 1. An isolated PostgreSQL -------------------------------------------
  info "Database"
  case "$(container_state)" in
    running)
      ok "$PG_CONTAINER is already running on 127.0.0.1:$PG_PORT"
      ;;
    exited|created|stopped)
      step "starting the existing $PG_CONTAINER"
      "$RUNTIME" start "$PG_CONTAINER" >/dev/null
      ok "$PG_CONTAINER restarted, data preserved"
      ;;
    *)
      if port_in_use "$PG_PORT"; then
        die "port $PG_PORT is in use but $PG_CONTAINER does not exist.
  Something else is on the demo's database port."
      fi
      step "creating $PG_CONTAINER (first run pulls $PG_IMAGE)"
      # A named volume, so `down` keeps the data and `down --clean` can
      # remove it deliberately. Published on loopback only.
      "$RUNTIME" run -d \
        --name "$PG_CONTAINER" \
        -e POSTGRES_USER="$PG_USER" \
        -e POSTGRES_PASSWORD="$PG_PASSWORD" \
        -e POSTGRES_DB="$PG_DATABASE" \
        -e POSTGRES_INITDB_ARGS="--encoding=UTF8 --locale=C" \
        -p "127.0.0.1:$PG_PORT:5432" \
        -v "$PG_VOLUME:/var/lib/postgresql/data" \
        "$PG_IMAGE" >/dev/null
      ok "$PG_CONTAINER created on 127.0.0.1:$PG_PORT"
      ;;
  esac

  step "waiting for PostgreSQL to accept queries"
  local deadline=$((SECONDS + 90))
  until "$RUNTIME" exec "$PG_CONTAINER" pg_isready -U "$PG_USER" -d "$PG_DATABASE" >/dev/null 2>&1; do
    [ "$SECONDS" -lt "$deadline" ] || die "PostgreSQL did not become ready within 90s.
  Logs:  $RUNTIME logs $PG_CONTAINER"
    sleep 1
  done
  ok "PostgreSQL ready"

  # --- 2. Schema ------------------------------------------------------------
  info "Schema"
  (cd "$SERVER_DIR" && HCC_DATABASE_URL="$DATABASE_URL" "$VENV/bin/alembic" upgrade head) \
    >"$DEMO_DIR/migrate.log" 2>&1 || {
      tail -20 "$DEMO_DIR/migrate.log"
      die "migrations failed; see $DEMO_DIR/migrate.log"
    }
  ok "migrations applied ($(HCC_DATABASE_URL="$DATABASE_URL" bash -c "cd '$SERVER_DIR' && '$VENV/bin/alembic' current 2>/dev/null | head -1" || echo head))"

  # --- 3. The administrator -------------------------------------------------
  info "Administrator"
  if [ -f "$DEMO_DIR/credentials" ]; then
    ok "reusing the administrator from $DEMO_DIR/credentials"
  else
    # Generated, never defaulted and never accepted on a command line. 32
    # URL-safe characters is ~190 bits; the point is not the entropy but that
    # there is no value to copy out of this file into a real deployment.
    local password
    password="$("$VENV/bin/python" -c 'import secrets; print(secrets.token_urlsafe(24))')"
    (cd "$SERVER_DIR" && printf '%s\n' "$password" | HCC_DATABASE_URL="$DATABASE_URL" \
      "$VENV/bin/python" -m hcc.cli.main bootstrap-admin \
      --username "$ADMIN_USER" --password-stdin) >"$DEMO_DIR/bootstrap.log" 2>&1 || {
        tail -20 "$DEMO_DIR/bootstrap.log"
        die "could not create the administrator; see $DEMO_DIR/bootstrap.log"
      }
    # Written before anything else can fail, so a half-finished `up` still
    # leaves a usable password rather than an account nobody can reach.
    umask 077
    printf 'username=%s\npassword=%s\nurl=%s\n' "$ADMIN_USER" "$password" "$BASE" \
      >"$DEMO_DIR/credentials"
    chmod 600 "$DEMO_DIR/credentials"
    ok "created $ADMIN_USER with a generated password"
  fi

  # --- 4. The web client ----------------------------------------------------
  info "Web client"
  if [ "$rebuild" = 1 ] || [ ! -f "$WEB_DIR/dist/index.html" ]; then
    [ -d "$WEB_DIR/node_modules" ] || die "frontend dependencies are missing. Run:  make setup"
    step "building the production bundle (this takes a moment)"
    (cd "$WEB_DIR" && npm run build) >"$DEMO_DIR/web-build.log" 2>&1 || {
      tail -30 "$DEMO_DIR/web-build.log"
      die "the web build failed; see $DEMO_DIR/web-build.log"
    }
  fi
  ok "serving $WEB_DIR/dist"

  # --- 5. The controller ----------------------------------------------------
  info "Controller"
  if process_alive "$DEMO_DIR/controller.pid"; then
    ok "already running (pid $(cat "$DEMO_DIR/controller.pid"))"
  else
    # One origin: the API and the client are the same server on the same
    # port, which is what the session and CSRF model assumes. No CORS
    # entries, because a same-origin client needs no exception.
    #
    # `development` rather than `production` for the environment, and the
    # reason is honest rather than lazy: production refuses a non-secure
    # session cookie, and this is plain HTTP on loopback. A demo that
    # claimed to be production while relaxing a production check would be
    # teaching the wrong thing.
    (
      cd "$SERVER_DIR"
      HCC_DATABASE_URL="$DATABASE_URL" \
      HCC_ENVIRONMENT=development \
      HCC_HOST=127.0.0.1 \
      HCC_PORT="$CONTROLLER_PORT" \
      HCC_PUBLIC_ORIGIN="$BASE" \
      HCC_WEB_ROOT="$WEB_DIR/dist" \
      HCC_LOG_FORMAT=json \
      nohup "$VENV/bin/python" -m hcc.main >"$DEMO_DIR/controller.log" 2>&1 &
      echo $! >"$DEMO_DIR/controller.pid"
    )
    sleep 1
    process_alive "$DEMO_DIR/controller.pid" || {
      tail -30 "$DEMO_DIR/controller.log"
      die "the controller exited immediately; see $DEMO_DIR/controller.log"
    }
    wait_for_url "the controller" "$BASE/readyz" 60 || {
      tail -30 "$DEMO_DIR/controller.log"
      die "the controller never became ready; see $DEMO_DIR/controller.log"
    }
    ok "listening on $BASE (pid $(cat "$DEMO_DIR/controller.pid"))"
  fi

  # --- 6. Sign in -----------------------------------------------------------
  local password cookie_jar login csrf
  password="$(grep '^password=' "$DEMO_DIR/credentials" | cut -d= -f2-)"
  cookie_jar="$DEMO_DIR/cookies"
  rm -f "$cookie_jar"
  login="$(curl -fsS -c "$cookie_jar" -X POST "$API/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$password\"}")" \
    || die "could not sign in as $ADMIN_USER. Delete $DEMO_DIR and run up again."
  csrf="$(printf '%s' "$login" | json_field csrf_token)"
  [ -n "$csrf" ] || die "sign-in returned no CSRF token"
  chmod 600 "$cookie_jar"

  api() {
    curl -fsS -b "$cookie_jar" -X "$1" "$API$2" \
      -H 'Content-Type: application/json' -H "X-CSRF-Token: $csrf" \
      -H "Origin: $BASE" ${3:+-d "$3"}
  }

  # --- 7. A host, and an agent that enrols itself ---------------------------
  info "Test agent"
  local host_id
  if [ -f "$DEMO_DIR/agent/host_id" ] && [ -f "$DEMO_DIR/agent/credential" ]; then
    host_id="$(cat "$DEMO_DIR/agent/host_id")"
    ok "reusing the enrolled host $host_id"
  else
    local host
    host="$(api POST /hosts \
      '{"name":"demo-localhost","description":"this machine, reporting its own /proc","environment":"test"}')" \
      || die "could not register the demo host"
    host_id="$(printf '%s' "$host" | json_field host_id)"
    printf '%s' "$host_id" >"$DEMO_DIR/agent/host_id"
    ok "registered the host demo-localhost"

    local enrollment credential
    enrollment="$(api POST "/hosts/$host_id/enrollment" '{}')" \
      || die "could not issue an enrolment credential"
    credential="$(printf '%s' "$enrollment" | json_field credential)"
    [ -n "$credential" ] || die "enrolment returned no credential"

    # In a file, never on a command line: /proc makes an argv readable by
    # every local user, and the agent refuses a flag for this.
    umask 077
    printf '%s' "$credential" >"$DEMO_DIR/agent/enrollment"
    chmod 600 "$DEMO_DIR/agent/enrollment"
    ok "one-time enrolment credential issued"
  fi

  if [ "$rebuild" = 1 ] || [ ! -x "$DEMO_DIR/agent/hcc-agent" ]; then
    step "building the agent from source"
    (cd "$AGENT_DIR" && go build -o "$DEMO_DIR/agent/hcc-agent" ./cmd/hcc-agent) \
      >"$DEMO_DIR/agent-build.log" 2>&1 || {
        tail -20 "$DEMO_DIR/agent-build.log"
        die "the agent build failed; see $DEMO_DIR/agent-build.log"
      }
  fi
  ok "agent binary: $DEMO_DIR/agent/hcc-agent"

  if [ ! -f "$DEMO_DIR/agent/credential" ]; then
    env HCC_AGENT_CONTROLLER_URL="$BASE" \
      HCC_AGENT_CREDENTIAL_FILE="$DEMO_DIR/agent/credential" \
      HCC_AGENT_CONFIG_FILE="$DEMO_DIR/agent/config.json" \
      HCC_AGENT_ENROLLMENT_TOKEN_FILE="$DEMO_DIR/agent/enrollment" \
      "$DEMO_DIR/agent/hcc-agent" enroll >"$DEMO_DIR/agent-enroll.log" 2>&1 || {
        tail -20 "$DEMO_DIR/agent-enroll.log"
        die "the agent could not enrol; see $DEMO_DIR/agent-enroll.log"
      }
    rm -f "$DEMO_DIR/agent/enrollment"
    ok "agent enrolled; the one-time credential is spent and removed"
  fi

  # --- 8. Turn collection on ------------------------------------------------
  #
  # Explicitly, per module, which is the same path the interface uses. The
  # defaults in the registry cannot express "on", so this is the only way
  # anything gets collected — including here.
  info "Monitoring"
  local enabled=0
  for entry in $DEMO_MODULES; do
    local module="${entry%%:*}" interval="${entry##*:}"
    if api PUT "/hosts/$host_id/monitoring/modules/$module" \
      "{\"enabled\":true,\"schedule\":{\"kind\":\"interval\",\"interval_seconds\":$interval}}" \
      >/dev/null 2>&1; then
      enabled=$((enabled + 1))
    else
      warn "  ! could not enable $module"
    fi
  done
  ok "$enabled modules enabled on demo-localhost"

  # --- 9. Run the agent -----------------------------------------------------
  if process_alive "$DEMO_DIR/agent.pid"; then
    ok "agent already running (pid $(cat "$DEMO_DIR/agent.pid"))"
  else
    (
      HCC_AGENT_CONTROLLER_URL="$BASE" \
      HCC_AGENT_CREDENTIAL_FILE="$DEMO_DIR/agent/credential" \
      HCC_AGENT_CONFIG_FILE="$DEMO_DIR/agent/config.json" \
      HCC_AGENT_LOG_FORMAT=json \
      nohup "$DEMO_DIR/agent/hcc-agent" run >"$DEMO_DIR/agent.log" 2>&1 &
      echo $! >"$DEMO_DIR/agent.pid"
    )
    sleep 1
    process_alive "$DEMO_DIR/agent.pid" || {
      tail -20 "$DEMO_DIR/agent.log"
      die "the agent exited immediately; see $DEMO_DIR/agent.log"
    }
    ok "agent running (pid $(cat "$DEMO_DIR/agent.pid"))"
  fi

  # --- 10. Wait for something real to arrive --------------------------------
  step "waiting for the first telemetry"
  local samples=0
  deadline=$((SECONDS + 60))
  while [ "$SECONDS" -lt "$deadline" ]; do
    samples="$(psql_demo "SELECT count(*) FROM metric_samples_raw" 2>/dev/null || echo 0)"
    [ "${samples:-0}" -gt 0 ] && break
    sleep 2
  done
  if [ "${samples:-0}" -gt 0 ]; then
    local series
    series="$(psql_demo "SELECT count(*) FROM metric_series" 2>/dev/null || echo 0)"
    ok "$samples samples across $series series, collected from this machine"
  else
    warn "  ! no telemetry yet after 60s. The interface will show the host as"
    warn "    connected with no data. Check:  ./scripts/demo.sh logs"
  fi

  print_summary
}

print_summary() {
  local password
  password="$(grep '^password=' "$DEMO_DIR/credentials" | cut -d= -f2-)"

  printf "\n${BOLD}The demo is up.${RESET}\n\n"
  printf "  ${BOLD}%-12s${RESET} %s\n" "Open" "$BASE"
  printf "  ${BOLD}%-12s${RESET} %s\n" "Username" "$ADMIN_USER"
  printf "  ${BOLD}%-12s${RESET} %s\n" "Password" "$password"
  printf "\n"
  dim "  The password is also in .demo/credentials (mode 600)."
  dim "  Both ports are bound to 127.0.0.1 and are not reachable from the network."
  printf "\n"
  printf "  ${BOLD}What to look at${RESET}\n"
  dim "    Hosts → demo-localhost       live tiles and history for this machine"
  dim "    Hosts → demo-localhost → Monitoring   turn modules on and off"
  dim "    Settings → Monitoring        fleet-wide defaults"
  dim "    Settings → Audit             every change you just made, recorded"
  printf "\n"
  printf "  ${BOLD}Everything in a chart is real${RESET}\n"
  dim "    CPU, memory, load and uptime from /proc; filesystems from statfs;"
  dim "    disk throughput from /proc/diskstats; interfaces from /proc/net/dev."
  dim "    The Overview's container and log cards are labelled sample data and"
  dim "    say so on screen — those modules are not built yet."
  printf "\n"
  printf "  ${BOLD}Commands${RESET}\n"
  dim "    ./scripts/demo.sh status        is it working, and is data arriving"
  dim "    ./scripts/demo.sh logs          follow the controller and agent"
  dim "    ./scripts/demo.sh down          stop, keep the data"
  dim "    ./scripts/demo.sh down --clean  stop and delete everything"
  printf "\n"
}

# ============================================================================
# status
# ============================================================================

cmd_status() {
  printf "\n${BOLD}Homelab Control Center — demo status${RESET}\n\n"
  RUNTIME="$(detect_runtime)"

  local state="absent"
  [ -n "$RUNTIME" ] && state="$(container_state)"
  printf "  %-22s %s\n" "database" \
    "$([ "$state" = running ] && echo "running on 127.0.0.1:$PG_PORT" || echo "$state")"

  if process_alive "$DEMO_DIR/controller.pid"; then
    printf "  %-22s %s\n" "controller" "running on $BASE (pid $(cat "$DEMO_DIR/controller.pid"))"
  else
    printf "  %-22s %s\n" "controller" "stopped"
  fi

  if process_alive "$DEMO_DIR/agent.pid"; then
    printf "  %-22s %s\n" "agent" "running (pid $(cat "$DEMO_DIR/agent.pid"))"
  else
    printf "  %-22s %s\n" "agent" "stopped"
  fi

  if [ "$state" = running ]; then
    local samples series latest
    samples="$(psql_demo "SELECT count(*) FROM metric_samples_raw" 2>/dev/null || echo "?")"
    series="$(psql_demo "SELECT count(*) FROM metric_series" 2>/dev/null || echo "?")"
    latest="$(psql_demo "SELECT coalesce(to_char(max(recorded_at), 'YYYY-MM-DD HH24:MI:SS'), 'never') FROM metric_samples_raw" 2>/dev/null || echo "?")"
    printf "  %-22s %s\n" "samples" "$samples across $series series"
    printf "  %-22s %s\n" "most recent" "$latest UTC"
    printf "\n"
    # Per module, because "is data arriving" is really "is *this* arriving",
    # and an operator who enabled the network module wants to see that line.
    printf "  ${BOLD}%-14s %8s  %s${RESET}\n" "module" "series" "most recent"
    psql_demo "SELECT s.module, count(DISTINCT s.id), coalesce(to_char(max(s.last_sample_at), 'HH24:MI:SS'), '—')
               FROM metric_series s GROUP BY s.module ORDER BY s.module" 2>/dev/null |
      while IFS='|' read -r module count recent; do
        [ -n "$module" ] && printf "  %-14s %8s  %s\n" "$module" "$count" "$recent"
      done
  fi

  if [ -f "$DEMO_DIR/credentials" ]; then
    printf "\n"
    dim "  credentials: $DEMO_DIR/credentials"
  fi
  printf "\n"
}

# ============================================================================
# logs
# ============================================================================

cmd_logs() {
  local files=()
  [ -f "$DEMO_DIR/controller.log" ] && files+=("$DEMO_DIR/controller.log")
  [ -f "$DEMO_DIR/agent.log" ] && files+=("$DEMO_DIR/agent.log")
  [ ${#files[@]} -gt 0 ] || die "no logs yet. Start the demo with:  ./scripts/demo.sh up"
  dim "following ${files[*]} — ctrl-c to stop"
  tail -n 40 -F "${files[@]}"
}

# ============================================================================
# down
# ============================================================================

cmd_down() {
  local clean=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --clean) clean=1; shift ;;
      *) die "unknown option for down: $1" ;;
    esac
  done

  RUNTIME="$(detect_runtime)"
  info "Stopping the demo"

  # The agent first. It is the thing writing to the controller, and stopping
  # it first means the controller's last log lines are a clean shutdown
  # rather than a connection that vanished.
  for name in agent controller; do
    if process_alive "$DEMO_DIR/$name.pid"; then
      local pid
      pid="$(cat "$DEMO_DIR/$name.pid")"
      kill "$pid" 2>/dev/null || true
      # SIGTERM, then a bounded wait, then SIGKILL. The agent flushes its
      # telemetry queue on SIGTERM, so not waiting would discard the last
      # few seconds of data it had already collected.
      local waited=0
      while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt 10 ]; do
        sleep 1
        waited=$((waited + 1))
      done
      kill -9 "$pid" 2>/dev/null || true
      rm -f "$DEMO_DIR/$name.pid"
      ok "$name stopped"
    else
      rm -f "$DEMO_DIR/$name.pid"
      step "$name was not running"
    fi
  done

  if [ -n "$RUNTIME" ] && [ "$(container_state)" != absent ]; then
    if [ "$clean" = 1 ]; then
      "$RUNTIME" rm -f "$PG_CONTAINER" >/dev/null 2>&1 || true
      "$RUNTIME" volume rm "$PG_VOLUME" >/dev/null 2>&1 || true
      ok "removed $PG_CONTAINER and its volume"
    else
      "$RUNTIME" stop "$PG_CONTAINER" >/dev/null 2>&1 || true
      ok "stopped $PG_CONTAINER (data kept in volume $PG_VOLUME)"
    fi
  fi

  if [ "$clean" = 1 ]; then
    rm -rf "$DEMO_DIR"
    ok "removed .demo/ — the administrator, the credential and the logs are gone"
    printf "\n"
    dim "Nothing of the demo remains. './scripts/demo.sh up' starts fresh."
  else
    printf "\n"
    dim "Data kept. './scripts/demo.sh up' resumes with the same administrator,"
    dim "the same host and the history it has already collected."
    dim "To discard it all:  ./scripts/demo.sh down --clean"
  fi
  printf "\n"
}

# ============================================================================

usage() {
  sed -n '2,57p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

case "${1:-up}" in
  up) shift || true; cmd_up "$@" ;;
  status) shift || true; cmd_status "$@" ;;
  logs) shift || true; cmd_logs "$@" ;;
  down) shift || true; cmd_down "$@" ;;
  -h|--help|help) usage ;;
  *) red "unknown command: $1"; printf '\n'; usage; exit 2 ;;
esac
