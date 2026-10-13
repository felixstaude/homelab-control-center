#!/usr/bin/env bash
#
# Generate the cross-language contract files, or check them for drift.
#
# Some facts have to be known by three programs written in three languages:
# which monitoring modules exist, what each one's interval floor is, and which
# scheduling kinds it accepts. The controller owns them — the registry is
# Python code (`hcc/monitoring/modules.py`) — and the Go agent and the
# TypeScript client each need their own copy, because neither can import
# Python.
#
# Three hand-maintained copies of one table is the problem this script exists
# to prevent. The committed JSON is generated from the controller's registry,
# the Go and TypeScript test suites read it and assert their own copies match,
# and CI runs `--check` so a change on one side that is not reflected on the
# others fails rather than drifting.
#
# Same pattern, and the same reasoning, as scripts/openapi-snapshot.sh and
# ADR-037: generate, commit, and fail on drift.
#
# Usage:
#   ./scripts/contracts.sh            # regenerate
#   ./scripts/contracts.sh --check    # fail if the committed files are stale

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VENV="apps/server/.venv"
OUTPUT_DIR="contracts"
MODULES_FILE="$OUTPUT_DIR/monitoring-modules.json"
CALENDAR_FILE="$OUTPUT_DIR/calendar-cases.json"

green() { printf '\033[0;32m%s\033[0m\n' "$1"; }
red() { printf '\033[0;31m%s\033[0m\n' "$1"; }

# Which interpreter runs the generator. Same resolution as
# scripts/openapi-snapshot.sh, and for the same reason: both scripts run in
# CI's `server` job, which installs the backend into the runner's own Python
# with `pip install -e` and creates no venv. Requiring one meant neither
# script could run in the one place it was most needed.
#
# The venv wins when there is one, so a developer's run is pinned to the
# project's interpreter rather than whatever `python3` is first on PATH.
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
    red "  Run: make setup"
    exit 1
  fi
fi

# `hcc.cli.contracts` is what this script actually runs, so import it rather
# than the bare package: from apps/server the source tree is on sys.path and
# a bare `import hcc` would succeed against an interpreter with none of the
# dependencies installed.
if ! ( cd apps/server && "$PYTHON" -c 'import hcc.cli.contracts' ) >/dev/null 2>&1; then
  red "$PYTHON cannot import hcc.cli.contracts — the backend is not installed for it."
  red "  Run: make setup   (or: pip install -e 'apps/server[dev]')"
  exit 1
fi

CHECK=0
if [[ "${1:-}" == "--check" ]]; then
  CHECK=1
fi

mkdir -p "$OUTPUT_DIR"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# An absolute interpreter path: a relative one resolved from `apps/server`
# makes Python's site module warn about an unexpected `sys.prefix`, which is
# noise on every run of an otherwise silent script.
(cd apps/server && "$PYTHON" -m hcc.cli.contracts) >"$WORK/out.json"

"$PYTHON" - "$WORK" "$MODULES_FILE" "$CALENDAR_FILE" <<'PY'
import json
import pathlib
import sys

work, modules_path, calendar_path = (pathlib.Path(arg) for arg in sys.argv[1:4])
payload = json.loads((work / "out.json").read_text())

# Two files rather than one, because they have different audiences: the module
# registry is read by the Go agent *and* the browser client, while the calendar
# cases are only read by the two next-run implementations.
for path, document in (
    (modules_path, payload["modules"]),
    (calendar_path, payload["calendar"]),
):
    # Sorted keys and a trailing newline, so the committed file is stable and
    # a regeneration that changes nothing produces no diff.
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
PY

if [[ "$CHECK" -eq 1 ]]; then
  if git diff --quiet -- "$MODULES_FILE" "$CALENDAR_FILE"; then
    green "✓ Contract files are up to date"
  else
    red "✗ Contract files are stale. Run ./scripts/contracts.sh and commit the result."
    git --no-pager diff --stat -- "$MODULES_FILE" "$CALENDAR_FILE"
    exit 1
  fi
else
  green "✓ Wrote $MODULES_FILE and $CALENDAR_FILE"
fi
