#!/usr/bin/env bash
#
# Version consistency.
#
# Four files carry this project's version and they have to agree, because
# release artifacts are named from one of them, the controller reports another
# to the interface, and the install manifest the controller serves claims a
# third. A mismatch is not cosmetic: it makes the download URL in the install
# command point at a release that was never published under that name.
#
#   VERSION                     the single source of truth
#   apps/server/pyproject.toml  the Python distribution's version
#   apps/server/hcc/__init__.py __version__, which GET /api/v1/info returns
#   apps/web/package.json       the web client's version
#
# A tag, when one is given, has to match too: `v0.2.0` built from a tree whose
# VERSION says 0.1.0 produces artifacts named after a version nobody can find
# in the repository.
#
# Usage:
#   ./scripts/check-versions.sh            (or: make check-versions)
#   ./scripts/check-versions.sh --tag v0.1.0

set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

GREEN='\033[0;32m' RED='\033[0;31m' RESET='\033[0m'

TAG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --tag) TAG="${2:?--tag needs a value}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

failures=0
fail() {
  printf "${RED}  ✗ %s${RESET}\n" "$*"
  failures=$((failures + 1))
}
pass() { printf "${GREEN}  ✓ %s${RESET}\n" "$*"; }

[ -f VERSION ] || { fail "VERSION is missing"; exit 1; }
canonical="$(tr -d '[:space:]' < VERSION)"

if ! printf '%s' "$canonical" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.+-]+)?$'; then
  fail "VERSION ($canonical) is not a semantic version"
fi

# pyproject and package.json are read with a parser rather than a regex: a
# `version` key inside a dependency table would make grep agree with itself
# and with nothing else.
python_version="$(python3 -c '
import sys, tomllib
with open("apps/server/pyproject.toml", "rb") as handle:
    print(tomllib.load(handle)["project"]["version"])
')"
init_version="$(python3 -c '
import re, pathlib
text = pathlib.Path("apps/server/hcc/__init__.py").read_text(encoding="utf-8")
match = re.search(r"^__version__ = \"([^\"]+)\"", text, re.MULTILINE)
print(match.group(1) if match else "")
')"
web_version="$(python3 -c '
import json, pathlib
print(json.loads(pathlib.Path("apps/web/package.json").read_text(encoding="utf-8"))["version"])
')"

for pair in \
  "apps/server/pyproject.toml=$python_version" \
  "apps/server/hcc/__init__.py=$init_version" \
  "apps/web/package.json=$web_version"
do
  file="${pair%%=*}"
  value="${pair#*=}"
  if [ "$value" != "$canonical" ]; then
    fail "$file says $value, VERSION says $canonical"
  else
    pass "$file: $canonical"
  fi
done

if [ -n "$TAG" ]; then
  # A leading v is the tag convention; the artifacts are named without it.
  stripped="${TAG#v}"
  if [ "$stripped" != "$canonical" ]; then
    fail "tag $TAG does not match VERSION ($canonical)"
  else
    pass "tag $TAG matches VERSION"
  fi
fi

echo
if [ "$failures" -gt 0 ]; then
  printf "${RED}✗ %s version mismatch(es)${RESET}\n" "$failures"
  exit 1
fi
printf "${GREEN}✓ every version agrees on %s${RESET}\n" "$canonical"
