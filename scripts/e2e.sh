#!/usr/bin/env bash
#
# Browser end-to-end verification.
#
# Stands up a complete, disposable installation and drives it with a real
# Chromium through Playwright:
#
#   1. create a throwaway database
#   2. apply migrations
#   3. start the control plane on a dedicated port
#   4. build the web client and serve it with the /api proxy
#   5. run the browser tests (which bootstrap an administrator themselves)
#   6. tear everything down
#
# Usage:
#   make db-up            # PostgreSQL must be running
#   ./scripts/e2e.sh
#   ./scripts/e2e.sh --headed    # watch it happen
#   ./scripts/e2e.sh e2e/session.spec.ts -g 'expired'   # one test
#
# Arguments are passed straight through to Playwright. Note that the suites
# run serially against one installation, so selecting a subset can skip the
# bootstrap the later files depend on.
#
# Safety
# ------
# The database name is unique per run and is dropped at the end. The
# developer's own database is never migrated, truncated or bootstrapped — a
# test suite that initialised the development installation would consume its
# one-shot bootstrap and could not be run twice.
#
# Ports are deliberately not the development ones, so a running `make
# dev-server` does not collide with a run of this script, and a mistake cannot
# point the tests at the developer's instance.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

SERVER_DIR="apps/server"
WEB_DIR="apps/web"
VENV="$SERVER_DIR/.venv"

# Local development credentials, matching deploy/compose.yaml. Not a secret.
PG_SUPERUSER_URL="postgresql://hcc:hcc_local_dev@127.0.0.1:5432/hcc"
E2E_DB="hcc_e2e_$(date +%s)_$$"
E2E_DB_URL="postgresql://hcc:hcc_local_dev@127.0.0.1:5432/$E2E_DB"

# Not 8000/5173: a running dev stack must not be mistaken for this one.
BACKEND_PORT=8201
PREVIEW_PORT=4173

# The throttle window, shortened for the suite.
#
# The budgets stay at their production defaults, so the rate-limiting test
# exercises the real limit — but a 300-second window would mean every test
# after it ran against a throttled address. Two seconds is long enough for the
# test to spend a budget inside one window and short enough to wait out.
THROTTLE_WINDOW_SECONDS=2
BACKEND_URL="http://127.0.0.1:$BACKEND_PORT"
PREVIEW_URL="http://127.0.0.1:$PREVIEW_PORT"

SERVER_PID=""
PREVIEW_PID=""

green() { printf '\033[0;32m%s\033[0m\n' "$1"; }
red() { printf '\033[0;31m%s\033[0m\n' "$1"; }
info() { printf '\033[0;36m%s\033[0m\n' "$1"; }

psql_super() {
  # AUTOCOMMIT: CREATE/DROP DATABASE cannot run inside a transaction.
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

cleanup() {
  local status=$?
  info "== Teardown =="

  for pid in "$PREVIEW_PID" "$SERVER_PID"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
  done

  # FORCE: drop even if a connection lingers, so a failed run still cleans up
  # rather than leaving a database behind on every attempt.
  if PG_SUPERUSER_URL="$PG_SUPERUSER_URL" psql_super \
    "DROP DATABASE IF EXISTS \"$E2E_DB\" WITH (FORCE)" 2>/dev/null; then
    green "  ✓ dropped $E2E_DB"
  else
    red "  ✗ could not drop $E2E_DB — remove it by hand"
  fi

  exit "$status"
}
trap cleanup EXIT

# Fail fast on a port already in use, rather than testing somebody else's
# process and reporting the result as ours.
require_free_port() {
  if curl -sS -o /dev/null --max-time 2 "http://127.0.0.1:$1/" 2>/dev/null; then
    red "Something is already listening on port $1. Stop it first."
    exit 1
  fi
}

wait_for() {
  local url="$1" label="$2"
  for _ in $(seq 1 100); do
    if curl -sS -o /dev/null --max-time 2 "$url" 2>/dev/null; then
      green "  ✓ $label is up"
      return 0
    fi
    sleep 0.2
  done
  red "  ✗ $label did not come up at $url"
  return 1
}

# ---------------------------------------------------------------------------
info "== Preconditions =="

if [[ ! -x "$VENV/bin/python" ]]; then
  red "Python venv missing. Run: make install-server"
  exit 1
fi
if [[ ! -d "$WEB_DIR/node_modules" ]]; then
  red "Frontend dependencies missing. Run: make install-web"
  exit 1
fi
if [[ ! -d "$WEB_DIR/node_modules/@playwright/test" ]]; then
  red "Playwright missing. Run: cd $WEB_DIR && npm install"
  exit 1
fi
green "  ✓ toolchain present"

require_free_port "$BACKEND_PORT"
require_free_port "$PREVIEW_PORT"
green "  ✓ ports $BACKEND_PORT and $PREVIEW_PORT are free"

# ---------------------------------------------------------------------------
info "== Disposable database =="

PG_SUPERUSER_URL="$PG_SUPERUSER_URL" psql_super "CREATE DATABASE \"$E2E_DB\"" >/dev/null
green "  ✓ created $E2E_DB"

(cd "$SERVER_DIR" && HCC_DATABASE_URL="$E2E_DB_URL" "../../$VENV/bin/alembic" upgrade head) \
  >/tmp/hcc-e2e-migrate.log 2>&1 ||
  {
    red "  ✗ migrations failed"
    cat /tmp/hcc-e2e-migrate.log
    exit 1
  }
green "  ✓ migrations applied"

# ---------------------------------------------------------------------------
info "== Control plane =="

(
  cd "$SERVER_DIR"
  # `exec` so $! is the Python process and cleanup signals it directly.
  #
  # HCC_CORS_ORIGINS carries the preview origin: the proxy rewrites Host, so
  # the browser's Origin never matches the request's own host — the same
  # configuration a contributor runs with the dev server.
  exec env \
    HCC_DATABASE_URL="$E2E_DB_URL" \
    HCC_PORT="$BACKEND_PORT" \
    HCC_ENVIRONMENT=development \
    HCC_LOG_FORMAT=json \
    HCC_CORS_ORIGINS="[\"$PREVIEW_URL\"]" \
    HCC_LOGIN_RATE_LIMIT_WINDOW_SECONDS="$THROTTLE_WINDOW_SECONDS" \
    .venv/bin/python -m hcc.main
) >/tmp/hcc-e2e-server.log 2>&1 &
SERVER_PID=$!

wait_for "$BACKEND_URL/healthz" "control plane" || {
  cat /tmp/hcc-e2e-server.log
  exit 1
}

# The tests assert on the setup-required screen, so the installation must
# genuinely start uninitialised.
initialized=$(curl -sS "$BACKEND_URL/api/v1/setup/status")
if [[ "$initialized" != '{"initialized":false}' ]]; then
  red "  ✗ expected an uninitialised installation, got: $initialized"
  exit 1
fi
green "  ✓ installation is uninitialised"

# ---------------------------------------------------------------------------
info "== Web client =="

(cd "$WEB_DIR" && npm run build) >/tmp/hcc-e2e-build.log 2>&1 ||
  {
    red "  ✗ build failed"
    tail -30 /tmp/hcc-e2e-build.log
    exit 1
  }
green "  ✓ production build"

(
  cd "$WEB_DIR"
  # The built bundle, served with the same /api proxy — so this exercises the
  # shipped assets rather than transformed source.
  # --host 127.0.0.1 explicitly: `vite preview` otherwise binds `localhost`,
  # which can resolve to ::1 first, and the readiness probe below then finds
  # nothing on 127.0.0.1 while the server is in fact running.
  exec env HCC_DEV_BACKEND="$BACKEND_URL" \
    npx vite preview --port "$PREVIEW_PORT" --strictPort --host 127.0.0.1
) >/tmp/hcc-e2e-preview.log 2>&1 &
PREVIEW_PID=$!

wait_for "$PREVIEW_URL/" "preview server" || {
  cat /tmp/hcc-e2e-preview.log
  exit 1
}

# ---------------------------------------------------------------------------
info "== Browser tests =="

# Every argument is forwarded to Playwright, so the usual selectors work:
#   ./scripts/e2e.sh --headed
#   ./scripts/e2e.sh e2e/session.spec.ts -g 'expired session'
PLAYWRIGHT_ARGS=("$@")

(
  cd "$WEB_DIR"
  env \
    E2E_BASE_URL="$PREVIEW_URL" \
    E2E_BACKEND_URL="$BACKEND_URL" \
    E2E_DATABASE_URL="$E2E_DB_URL" \
    E2E_SERVER_DIR="$REPO_ROOT/$SERVER_DIR" \
    E2E_THROTTLE_WINDOW_SECONDS="$THROTTLE_WINDOW_SECONDS" \
    npx playwright test "${PLAYWRIGHT_ARGS[@]}"
)

echo
green "All end-to-end checks passed."
