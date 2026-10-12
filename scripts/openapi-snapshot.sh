#!/usr/bin/env bash
#
# Write the backend's OpenAPI schema to a committed snapshot the web client
# validates its types against.
#
# Usage:
#   ./scripts/openapi-snapshot.sh           # write the snapshot
#   ./scripts/openapi-snapshot.sh --check   # fail if it is out of date
#
# Why a snapshot, and not a generator
# -----------------------------------
# ADR-007 chose generated TypeScript types, and the roadmap reserves that work
# for milestone 1.15. This is the smaller half that is worth having first:
# the schema is committed, and `apps/web/src/api/openapi.test.ts` asserts the
# client's hand-written types match it. A backend change that alters a
# response the client consumes therefore fails a test, rather than being
# discovered at runtime.
#
# `--check` is what CI runs: it regenerates into a temporary file and diffs.
# Taking the schema from `app.openapi()` rather than from a running server
# means no port, no database and no fixtures are involved.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

SERVER_DIR="apps/server"
SNAPSHOT="apps/web/src/api/openapi.snapshot.json"
VENV="$SERVER_DIR/.venv"

green() { printf '\033[0;32m%s\033[0m\n' "$1"; }
red() { printf '\033[0;31m%s\033[0m\n' "$1"; }

if [[ ! -x "$VENV/bin/python" ]]; then
  red "Python venv missing. Run: make install-server"
  exit 1
fi

generate() {
  # A placeholder database URL: building the app opens no connection
  # (asyncpg connects lazily), and the schema does not depend on it. The
  # environment is otherwise cleared so a developer's .env cannot change the
  # generated schema — `docs_url`/`openapi_url` differ in production, and the
  # snapshot must be reproducible.
  (
    cd "$SERVER_DIR"
    env -i \
      PATH="/usr/bin:/bin" \
      HCC_DATABASE_URL="postgresql://schema:schema@127.0.0.1:5432/schema" \
      HCC_ENVIRONMENT="test" \
      "$REPO_ROOT/$VENV/bin/python" - <<'PY'
import json
import sys

from hcc.app import create_app
from hcc.config import Settings

app = create_app(Settings())
# sort_keys so the snapshot is diffable and the --check comparison is stable
# against dictionary ordering changes in FastAPI.
json.dump(app.openapi(), sys.stdout, indent=2, sort_keys=True)
sys.stdout.write("\n")
PY
  )
}

if [[ "${1:-}" == "--check" ]]; then
  if [[ ! -f "$SNAPSHOT" ]]; then
    red "✗ $SNAPSHOT does not exist. Run: make openapi"
    exit 1
  fi
  current="$(mktemp)"
  trap 'rm -f "$current"' EXIT
  generate >"$current"
  if diff -u "$SNAPSHOT" "$current" >/dev/null; then
    green "✓ OpenAPI snapshot is up to date"
  else
    red "✗ The API contract changed but $SNAPSHOT was not regenerated."
    red "  The web client's types are validated against this file, so a stale"
    red "  snapshot means the client may be typed against an API that no"
    red "  longer exists. Regenerate it and review the diff:"
    red "      make openapi"
    echo
    diff -u "$SNAPSHOT" "$current" | head -60
    exit 1
  fi
else
  generate >"$SNAPSHOT"
  green "✓ Wrote $SNAPSHOT"
fi
