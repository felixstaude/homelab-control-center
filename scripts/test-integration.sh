#!/usr/bin/env bash
#
# Backend integration tests against a disposable database.
#
# The integration suite truncates tables between tests and applies migrations
# from zero. Pointing it at the development database would destroy the
# developer's own data on every run, so this script creates a throwaway
# database, runs the suite against it, and drops it afterwards — on every exit
# path, including a failing one.
#
# Usage:
#   make db-up                      # PostgreSQL must be running
#   ./scripts/test-integration.sh
#   ./scripts/test-integration.sh tests/test_api_hosts_integration.py -k revoke
#
# Arguments are passed straight through to pytest. With none, the whole
# `integration` marker runs.
#
# Safety
# ------
# The database name is unique per run. The developer's own database is opened
# only to issue CREATE/DROP DATABASE, which cannot be done from inside the
# database being created.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

SERVER_DIR="$REPO_ROOT/apps/server"
# Absolute, because pytest runs with the server directory as its working
# directory and `cleanup` must still be able to find the interpreter it needs
# to drop the database.
VENV="$SERVER_DIR/.venv"

# Local development credentials, matching deploy/compose.yaml. Not a secret.
PG_SUPERUSER_URL="postgresql://hcc:hcc_local_dev@127.0.0.1:5432/hcc"
TEST_DB="hcc_it_$(date +%s)_$$"
TEST_DB_URL="postgresql://hcc:hcc_local_dev@127.0.0.1:5432/$TEST_DB"

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

CREATED=""

cleanup() {
  local status=$?
  if [[ -n "$CREATED" ]]; then
    # FORCE: drop even if a connection lingers, so a failed run still cleans
    # up rather than leaving a database behind on every attempt.
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

test -x "$VENV/bin/python" || {
  red "No virtualenv at $VENV. Run: make setup"
  exit 1
}

info "== Disposable database =="
PG_SUPERUSER_URL="$PG_SUPERUSER_URL" psql_super "CREATE DATABASE \"$TEST_DB\"" >/dev/null
CREATED=1
green "✓ created $TEST_DB"

info "== Integration suite =="
# No -m integration when specific paths are given: selecting a file and then
# filtering it to one marker surprises more often than it helps.
if [[ $# -eq 0 ]]; then
  set -- -m integration
fi

cd "$SERVER_DIR"
HCC_TEST_DATABASE_URL="$TEST_DB_URL" .venv/bin/python -m pytest "$@"
