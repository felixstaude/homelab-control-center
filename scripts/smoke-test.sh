#!/usr/bin/env bash
#
# End-to-end smoke test for the Phase 0 foundation.
#
# Verifies that the pieces actually work together, rather than only that the
# unit tests pass: the control plane boots against a real PostgreSQL,
# migrations apply, the health probes report accurately, the error envelope is
# correct, and the agent builds and shuts down cleanly.
#
# Usage:
#   make db-up
#   ./scripts/smoke-test.sh
#
# Exits non-zero on the first failure.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

SERVER_DIR="apps/server"
AGENT_DIR="agents/linux"
VENV="$SERVER_DIR/.venv"

# Local development credentials, matching deploy/compose.yaml. Not a secret.
DATABASE_URL="postgresql://hcc:hcc_local_dev@127.0.0.1:5432/hcc"
# A port unlikely to collide with a running dev server.
PORT=8099
BASE="http://127.0.0.1:$PORT"

PASS=0
FAIL=0
SERVER_PID=""

green() { printf '\033[0;32m%s\033[0m\n' "$1"; }
red()   { printf '\033[0;31m%s\033[0m\n' "$1"; }
info()  { printf '\033[0;36m%s\033[0m\n' "$1"; }

ok()  { green "  ✓ $1"; PASS=$((PASS + 1)); }
bad() { red   "  ✗ $1"; FAIL=$((FAIL + 1)); }

cleanup() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# assert_status <description> <expected-code> <url>
assert_status() {
  local description="$1" expected="$2" url="$3" actual
  actual=$(curl -sS -o /dev/null -w '%{http_code}' "$url" || echo "000")
  if [[ "$actual" == "$expected" ]]; then
    ok "$description (HTTP $actual)"
  else
    bad "$description — expected HTTP $expected, got $actual"
  fi
}

# assert_json_contains <description> <url> <substring>
assert_json_contains() {
  local description="$1" url="$2" needle="$3" body
  body=$(curl -sS "$url" || echo "")
  if [[ "$body" == *"$needle"* ]]; then
    ok "$description"
  else
    bad "$description — response did not contain '$needle': $body"
  fi
}

# assert_absent <description> <url> <substring that must NOT appear>
assert_absent() {
  local description="$1" url="$2" needle="$3" body
  body=$(curl -sS "$url" || echo "")
  if [[ "$body" != *"$needle"* ]]; then
    ok "$description"
  else
    bad "$description — response leaked '$needle'"
  fi
}

# --------------------------------------------------------------------------
info "== Preconditions =="

if [[ ! -x "$VENV/bin/python" ]]; then
  red "Python venv missing. Run: make install-server"
  exit 1
fi
ok "Python venv present"

if ! "$VENV/bin/python" -c 'import hcc' 2>/dev/null; then
  red "hcc package not importable. Run: make install-server"
  exit 1
fi
ok "hcc package importable"

if ! command -v curl >/dev/null 2>&1; then
  red "curl is required"
  exit 1
fi

# --------------------------------------------------------------------------
info "== Database and migrations =="

if (cd "$SERVER_DIR" && HCC_DATABASE_URL="$DATABASE_URL" .venv/bin/alembic upgrade head >/dev/null 2>&1); then
  ok "Migrations applied"
else
  bad "Migrations failed — is PostgreSQL running? Try: make db-up"
  red "Cannot continue without a database."
  exit 1
fi

if (cd "$SERVER_DIR" && HCC_DATABASE_URL="$DATABASE_URL" .venv/bin/alembic check >/dev/null 2>&1); then
  ok "No model changes missing a migration"
else
  bad "alembic check reported a drift between models and migrations"
fi

# --------------------------------------------------------------------------
info "== Control plane =="

# Refuse to run if the port is already taken. Otherwise the HTTP checks below
# would silently pass against somebody else's server while our own failed to
# bind — a false green, which is worse than a failure.
if curl -sS -o /dev/null --max-time 2 "$BASE/healthz" 2>/dev/null; then
  red "Something is already listening on port $PORT."
  red "Stop it first, or the results below would describe that process, not this build."
  exit 1
fi
ok "Port $PORT is free"

(
  cd "$SERVER_DIR"
  # `exec` replaces the subshell, so $! is the Python process itself and
  # cleanup signals the server rather than orphaning it.
  exec env \
    HCC_DATABASE_URL="$DATABASE_URL" \
    HCC_PORT="$PORT" \
    HCC_ENVIRONMENT=development \
    HCC_LOG_FORMAT=json \
    .venv/bin/python -m hcc.main
) >/tmp/hcc-smoke-server.log 2>&1 &
SERVER_PID=$!

# Wait for the port to answer rather than sleeping a fixed amount.
for _ in $(seq 1 50); do
  if curl -sS -o /dev/null "$BASE/healthz" 2>/dev/null; then break; fi
  sleep 0.2
done

if ! curl -sS -o /dev/null "$BASE/healthz" 2>/dev/null; then
  bad "Control plane did not start; log follows"
  cat /tmp/hcc-smoke-server.log
  exit 1
fi
# Confirm the process we started is the one answering, not a survivor of a
# previous run that bound the port between the check above and now.
if ! kill -0 "$SERVER_PID" 2>/dev/null; then
  bad "The server process exited, but the port is answering; log follows"
  cat /tmp/hcc-smoke-server.log
  exit 1
fi
ok "Control plane started"

assert_status       "Liveness probe"              200 "$BASE/healthz"
assert_json_contains "Liveness reports ok"            "$BASE/healthz" '"status":"ok"'
assert_status       "Readiness probe"             200 "$BASE/readyz"
assert_json_contains "Readiness reports ready"        "$BASE/readyz" '"status":"ready"'
assert_status       "OpenAPI schema"              200 "$BASE/openapi.json"
assert_status       "Swagger UI (non-production)" 200 "$BASE/docs"

# --- Default-deny authentication (milestone 1.4) ---
# Every product route requires a session. Login is the only public one, and
# the health probes, which orchestrators cannot authenticate.
assert_status "Info endpoint requires a session"   401 "$BASE/api/v1/info"
assert_status "Current-user endpoint needs auth"  401 "$BASE/api/v1/auth/me"
assert_json_contains "Unauthenticated uses the envelope" \
  "$BASE/api/v1/auth/me" '"code":"not_authenticated"'

# Public endpoints must disclose as little as possible.
assert_absent "Liveness hides the version"  "$BASE/healthz" '"version"'
assert_absent "Readiness hides dependencies" "$BASE/readyz" 'postgres'

# Unversioned product routes must not exist (ADR-006).
assert_status "Unversioned product route absent" 404 "$BASE/api/info"
# Probes must stay outside the version prefix.
assert_status "Probe not served under /api/v1"   404 "$BASE/api/v1/healthz"

# Uniform error envelope. An unknown path under the versioned prefix is a 404
# rather than a 401: routing happens before authentication, so a nonexistent
# route cannot be probed for existence by its auth response.
assert_json_contains "404 uses the error envelope" "$BASE/api/v1/nope" '"code":"not_found"'
assert_json_contains "Errors carry a request id"   "$BASE/api/v1/nope" '"request_id"'

# Credentials must never appear in a response.
assert_absent "No DB password in readiness body" "$BASE/readyz" 'hcc_local_dev'
assert_absent "No DB password in error body"     "$BASE/api/v1/nope" 'hcc_local_dev'

# Request correlation header.
if curl -sSI "$BASE/healthz" | grep -qi 'x-request-id'; then
  ok "X-Request-ID header present"
else
  bad "X-Request-ID header missing"
fi

# Structured JSON logging.
if grep -q '"event": *"startup"' /tmp/hcc-smoke-server.log; then
  ok "Structured JSON startup log emitted"
else
  bad "Structured startup log not found"
fi

# Credentials must never appear in logs either.
if grep -q 'hcc_local_dev' /tmp/hcc-smoke-server.log; then
  bad "Database password leaked into the server log"
else
  ok "No database password in server logs"
fi

cleanup
SERVER_PID=""
ok "Control plane shut down"

# --------------------------------------------------------------------------
info "== Configuration validation (milestone 1.5) =="

# These run the real entrypoint and assert it refuses to start. Each guards a
# misconfiguration that is silent at runtime, so a startup failure is the
# protection — and that is only true if the process actually exits.

# assert_refuses_to_start <description> <env assignments...>
assert_refuses_to_start() {
  local description="$1"; shift
  local output code
  set +e
  output=$(cd "$SERVER_DIR" && env HCC_DATABASE_URL="$DATABASE_URL" HCC_PORT=8098 "$@" \
    .venv/bin/python -m hcc.main 2>&1)
  code=$?
  set -e
  if [[ "$code" -eq 2 ]]; then
    ok "$description (exit 2)"
  else
    bad "$description — expected exit 2, got $code: $(echo "$output" | head -2)"
  fi
}

assert_refuses_to_start "Production without a public origin or trusted proxy" \
  HCC_ENVIRONMENT=production
assert_refuses_to_start "Wildcard CORS origin with credentials enabled" \
  HCC_CORS_ORIGINS='["*"]'
assert_refuses_to_start "Per-username budget at or below the per-source budget" \
  HCC_LOGIN_RATE_LIMIT_PER_ADDRESS_USERNAME=10 HCC_LOGIN_RATE_LIMIT_PER_USERNAME=10
assert_refuses_to_start "Unparseable trusted-proxy entry" \
  HCC_TRUSTED_PROXIES='["10.0.0.300"]'
assert_refuses_to_start "Removed HCC_TRUSTED_PROXY_HEADERS still set" \
  HCC_TRUSTED_PROXY_HEADERS='["x-forwarded-for"]'

# A production deployment that IS configured must start, or the checks above
# would pass by refusing everything.
if (cd "$SERVER_DIR" && env HCC_DATABASE_URL="$DATABASE_URL" HCC_PORT=8098 \
      HCC_ENVIRONMENT=production HCC_PUBLIC_ORIGIN="https://hcc.example.com" \
      timeout 5 .venv/bin/python -c "
from hcc.app import create_app
from hcc.config import load_settings_or_exit
create_app(load_settings_or_exit())
print('built')
" >/dev/null 2>&1); then
  ok "Production with a public origin builds"
else
  bad "A correctly configured production app failed to build"
fi

# --------------------------------------------------------------------------
info "== Operator CLI (milestone 1.5) =="

if "$SERVER_DIR/.venv/bin/hcc" --version 2>/dev/null | grep -q "hcc "; then
  ok "hcc CLI reports its version"
else
  bad "hcc CLI did not report a version"
fi

if "$SERVER_DIR/.venv/bin/hcc" bootstrap-admin --help 2>&1 | grep -q -- "--password-stdin"; then
  ok "bootstrap-admin documents --password-stdin"
else
  bad "bootstrap-admin does not document --password-stdin"
fi

# The contract that keeps the secret out of /proc and shell history.
set +e
"$SERVER_DIR/.venv/bin/hcc" bootstrap-admin --username admin --password secret >/dev/null 2>&1
code=$?
set -e
if [[ "$code" -ne 0 ]]; then
  ok "bootstrap-admin refuses a --password argument"
else
  bad "bootstrap-admin accepted a password as a command-line argument"
fi

# --------------------------------------------------------------------------
info "== Agent =="

if ! command -v go >/dev/null 2>&1; then
  info "  (skipping: Go toolchain not on PATH)"
else
  if (cd "$AGENT_DIR" && go build -o bin/hcc-agent ./cmd/hcc-agent 2>/dev/null); then
    ok "Agent builds"
  else
    bad "Agent build failed"
  fi

  if "$AGENT_DIR/bin/hcc-agent" -version 2>/dev/null | grep -q 'hcc-agent'; then
    ok "Agent reports its version"
  else
    bad "Agent -version did not produce version output"
  fi

  # Must refuse to start without configuration, with exit code 2.
  set +e
  env -u HCC_AGENT_CONTROLLER_URL -u HCC_AGENT_TOKEN -u HCC_AGENT_TOKEN_FILE \
    "$AGENT_DIR/bin/hcc-agent" >/dev/null 2>&1
  code=$?
  set -e
  if [[ "$code" -eq 2 ]]; then
    ok "Agent rejects empty configuration with exit code 2"
  else
    bad "Agent should exit 2 on bad configuration, got $code"
  fi

  # Must refuse a world-readable token file.
  TOKEN_FILE=$(mktemp)
  echo "smoke-test-token" > "$TOKEN_FILE"
  chmod 644 "$TOKEN_FILE"
  set +e
  output=$(HCC_AGENT_CONTROLLER_URL="https://example.internal" \
    HCC_AGENT_TOKEN_FILE="$TOKEN_FILE" \
    "$AGENT_DIR/bin/hcc-agent" 2>&1)
  code=$?
  set -e
  if [[ "$code" -eq 2 && "$output" == *"readable"* ]]; then
    ok "Agent refuses a world-readable token file"
  else
    bad "Agent accepted a world-readable token file (exit $code)"
  fi
  rm -f "$TOKEN_FILE"
fi

# --------------------------------------------------------------------------
echo
if [[ "$FAIL" -eq 0 ]]; then
  green "All $PASS smoke checks passed."
  exit 0
else
  red "$FAIL of $((PASS + FAIL)) smoke checks failed."
  exit 1
fi
