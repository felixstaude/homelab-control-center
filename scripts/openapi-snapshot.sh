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

# Which interpreter generates the schema.
#
# The project venv wins when there is one: that is what a developer has, and
# naming it explicitly keeps the snapshot independent of whatever `python3`
# happens to be first on PATH.
#
# Otherwise the ambient interpreter, because CI has no venv — the `server`
# job installs the backend into the runner's own Python with `pip install -e`
# and never creates one. Requiring a venv here meant this script could not
# run in the one place it was most needed, and the failure looked like a
# missing dependency rather than a wrong assumption.
#
# Absolute either way: the generator runs under `env -i` with a minimal PATH
# that would not find an interpreter by name.
if [[ -x "$VENV/bin/python" ]]; then
  PYTHON="$REPO_ROOT/$VENV/bin/python"
else
  PYTHON=""
  for candidate in python3 python; do
    if resolved="$(command -v "$candidate" 2>/dev/null)"; then
      PYTHON="$(cd "$(dirname "$resolved")" && pwd)/$(basename "$resolved")"
      break
    fi
  done
  if [[ -z "$PYTHON" ]]; then
    red "No Python interpreter found, and no venv at $VENV."
    red "  Run: make install-server"
    exit 1
  fi
fi

# Fail with the reason rather than a traceback from inside the heredoc. The
# working directory matches generate()'s, so this tests what that will do.
#
# `hcc.app`, not `hcc`: from $SERVER_DIR the source tree is on sys.path, so a
# bare `import hcc` succeeds against an interpreter with none of the
# dependencies installed and the real failure surfaces later as a traceback
# about structlog. Importing the module the generator imports is the check
# that actually distinguishes the two.
if ! ( cd "$SERVER_DIR" && "$PYTHON" -c 'import hcc.app' ) >/dev/null 2>&1; then
  red "$PYTHON cannot import hcc.app — the backend is not installed for it."
  red "  Run: make install-server   (or: pip install -e 'apps/server[dev]')"
  exit 1
fi

generate() {
  # A placeholder database URL: building the app opens no connection
  # (asyncpg connects lazily), and the schema does not depend on it. The
  # environment is otherwise cleared so a developer's .env cannot change the
  # generated schema — `docs_url`/`openapi_url` differ in production, and the
  # snapshot must be reproducible.
  #
  # The allowlist stays an allowlist. LD_LIBRARY_PATH is passed through only
  # when the caller has one, because an interpreter built `--enable-shared`
  # outside the system prefix — GitHub's hosted Python is exactly that —
  # cannot find its own libpython without it. It tells the dynamic loader
  # where to find a library; it cannot reach Settings, so it cannot change
  # the generated schema.
  local -a isolated=(
    PATH="/usr/bin:/bin"
    HCC_DATABASE_URL="postgresql://schema:schema@127.0.0.1:5432/schema"
    HCC_ENVIRONMENT="test"
  )
  if [[ -n "${LD_LIBRARY_PATH:-}" ]]; then
    isolated+=( LD_LIBRARY_PATH="$LD_LIBRARY_PATH" )
  fi

  (
    cd "$SERVER_DIR"
    env -i "${isolated[@]}" "$PYTHON" - <<'PY'
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
