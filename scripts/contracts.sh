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
VERSION_FILE="$OUTPUT_DIR/version-ordering.json"
SIGNING_FILE="$OUTPUT_DIR/release-signing.json"
# The browser test fixture, *generated* rather than transcribed.
#
# It lives under apps/web because that is the Docker build context: the
# client image is built from apps/web alone, so a test file importing
# ../../../../contracts/ compiles locally and fails `docker compose build`
# with "Cannot find module". That is not a hypothetical either — it is how
# this file came to be generated.
#
# Generating it also removes the drift the hand-written copy had: six
# modules landed in the registry during Operation Horizon and the fixture
# claimed five throughout, so every browser test ran against a
# five-module world and nothing failed.
WEB_MODULES_FILE="apps/web/src/test/module-catalogue.generated.ts"

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

"$PYTHON" - "$WORK" "$MODULES_FILE" "$CALENDAR_FILE" "$VERSION_FILE" "$SIGNING_FILE" \
  "$WEB_MODULES_FILE" <<'PY'
import json
import pathlib
import sys

work, modules_path, calendar_path, version_path, signing_path, web_modules_path = (
    pathlib.Path(arg) for arg in sys.argv[1:7]
)
payload = json.loads((work / "out.json").read_text())

# Three files rather than one, because they have different audiences: the
# module registry is read by the Go agent *and* the browser client, the
# calendar cases by the two next-run implementations, and the version ordering
# by the agent's own semver parser.
for path, document in (
    (modules_path, payload["modules"]),
    (calendar_path, payload["calendar"]),
    (version_path, payload["version_ordering"]),
    (signing_path, payload["release_signing"]),
):
    # Sorted keys and a trailing newline, so the committed file is stable and
    # a regeneration that changes nothing produces no diff.
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


def ts(value: object) -> str:
    """A TypeScript literal, single-quoted to match the project's style."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(ts(item) for item in value) + "]"
    text = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{text}'"


lines = [
    "// Generated by scripts/contracts.sh from the controller's module",
    "// registry. Do not edit: `make check-contract` fails when it is stale.",
    "//",
    "// The browser test fixture used to be a hand-written copy of",
    "// contracts/monitoring-modules.json, kept in step by a comment asking",
    "// the next reader to keep it in step. It fell six modules behind and",
    "// nothing failed, because the only thing that would have noticed was",
    "// the copy itself. Generated, it cannot drift.",
    "",
    "import type { ModuleDescriptor } from '@/api/types';",
    "",
    "export const CONTRACT_MODULES: readonly ModuleDescriptor[] = [",
]
for module in payload["modules"]["modules"]:
    lines.append("  {")
    for key in (
        "id",
        "name",
        "description",
        "platform",
        "default_interval_seconds",
        "min_interval_seconds",
        "schedule_kinds",
        "requires_privileges",
        "allows_duplicate_runs",
        "data_schema_version",
    ):
        lines.append(f"    {key}: {ts(module[key])},")
    lines.append("    metrics: [")
    for metric in module["metrics"]:
        fields = ", ".join(
            f"{key}: {ts(metric[key])}" for key in ("id", "name", "unit", "kind", "label_keys")
        )
        lines.append(f"      {{ {fields} }},")
    lines.append("    ],")
    lines.append("  },")
lines.append("];")
web_modules_path.write_text("\n".join(lines) + "\n")
PY

if [[ "$CHECK" -eq 1 ]]; then
  if git diff --quiet -- "$MODULES_FILE" "$CALENDAR_FILE" "$VERSION_FILE" "$SIGNING_FILE" \
    "$WEB_MODULES_FILE"; then
    green "✓ Contract files are up to date"
  else
    red "✗ Contract files are stale. Run ./scripts/contracts.sh and commit the result."
    git --no-pager diff --stat -- "$MODULES_FILE" "$CALENDAR_FILE" "$VERSION_FILE" \
      "$SIGNING_FILE" "$WEB_MODULES_FILE"
    exit 1
  fi
else
  green "✓ Wrote $MODULES_FILE, $CALENDAR_FILE, $VERSION_FILE, $SIGNING_FILE"
  green "  and $WEB_MODULES_FILE"
fi
