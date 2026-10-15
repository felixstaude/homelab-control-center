#!/usr/bin/env bash
#
# Build, describe, sign and verify a release — and publish nothing.
#
# The four steps that have to happen in order and in one place, because doing
# them by hand is how a manifest gets signed that describes artifacts nobody
# rebuilt:
#
#   1. Build the agent archives reproducibly       (build-agent-artifacts.sh)
#   2. Verify each one against its own manifest    (verify-artifacts.sh)
#   3. Derive the controller's release manifest    (build-release-manifest.py)
#   4. Sign it, and verify the signature           (sign-release.py)
#
# What it deliberately does not do: tag, push, upload, create a GitHub
# Release, or touch a container registry. Those are decisions with a blast
# radius, and a script that performs them is a script somebody runs by
# accident. This writes files into dist/ and prints what to do with them.
#
# Usage:
#   ./scripts/prepare-release.sh --version 0.2.0 \
#       --base-url https://github.com/<owner>/<repo>/releases/download/v0.2.0 \
#       --protocol-versions 1,2 \
#       --private-key /secure/release.key
#
#   # Validate the whole pipeline without a production key. See below.
#   ./scripts/prepare-release.sh --version 0.2.0-rc.1 \
#       --base-url https://example.invalid/v0.2.0-rc.1 \
#       --protocol-versions 1,2 --ephemeral-key
#
# Options:
#   --version X.Y.Z          the release being prepared (required)
#   --base-url URL           where the archives will be published (required)
#   --protocol-versions L    comma-separated, e.g. 1,2 (required)
#   --private-key PATH       the publisher's signing key
#   --ephemeral-key          generate a throwaway key instead; see below
#   --add-signature PATH     also sign with this key, for a rotation overlap
#   --merge-into PATH        an existing manifest whose other versions to keep
#   --channel NAME           stable (default), beta or alpha
#   --min-controller V       oldest controller version that may drive this
#   --schema-requirement R   Alembic revision this release needs applied
#   --notes-url URL          where the release notes are
#   --minimum-supported V    oldest agent version the controller works with
#   --output DIR             default dist/
#   --arch LIST              default amd64,arm64,arm
#   --skip-build             reuse dist/agent from a previous run
#
# # --ephemeral-key establishes nothing about publisher identity
#
# It exists so the *pipeline* can be exercised — in CI, and by anybody who
# wants to see what a release looks like — without the production key being
# anywhere near the machine. The resulting signature is cryptographically
# fine and socially worthless: the key it proves possession of was created
# thirty seconds ago and destroyed immediately after.
#
# A manifest signed this way must never be published, and this script says so
# in its output and writes a marker file beside it so that a later step can
# refuse it. The marker is the mechanism; the warning is for the human.
#
# # Missing prerequisites are a refusal, not a default
#
# No step here guesses. A missing key, a missing base URL, an unparseable
# version: each stops the run before anything is built, naming what is
# missing. The alternative — carrying on and producing an unsigned manifest —
# is the failure mode where everything looks finished and the agents refuse
# every update a week later.

set -euo pipefail

GREEN='\033[0;32m' RED='\033[0;31m' CYAN='\033[0;36m' YELLOW='\033[0;33m'
DIM='\033[0;90m' RESET='\033[0m'

VERSION=""
BASE_URL=""
PROTOCOL_VERSIONS=""
PRIVATE_KEY=""
EPHEMERAL=0
ADD_SIGNATURE=""
MERGE_INTO=""
CHANNEL="stable"
MIN_CONTROLLER=""
SCHEMA_REQUIREMENT=""
NOTES_URL=""
MINIMUM_SUPPORTED=""
OUTPUT_DIR="dist"
ARCHES="amd64,arm64,arm"
SKIP_BUILD=0

while [ $# -gt 0 ]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --base-url) BASE_URL="$2"; shift 2 ;;
    --protocol-versions) PROTOCOL_VERSIONS="$2"; shift 2 ;;
    --private-key) PRIVATE_KEY="$2"; shift 2 ;;
    --ephemeral-key) EPHEMERAL=1; shift ;;
    --add-signature) ADD_SIGNATURE="$2"; shift 2 ;;
    --merge-into) MERGE_INTO="$2"; shift 2 ;;
    --channel) CHANNEL="$2"; shift 2 ;;
    --min-controller) MIN_CONTROLLER="$2"; shift 2 ;;
    --schema-requirement) SCHEMA_REQUIREMENT="$2"; shift 2 ;;
    --notes-url) NOTES_URL="$2"; shift 2 ;;
    --minimum-supported) MINIMUM_SUPPORTED="$2"; shift 2 ;;
    --output) OUTPUT_DIR="$2"; shift 2 ;;
    --arch) ARCHES="$2"; shift 2 ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    -h|--help) sed -n '2,67p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

fail() { printf "${RED}✗ %s${RESET}\n" "$*" >&2; exit 1; }
step() { printf "\n${CYAN}== %s ==${RESET}\n" "$*"; }
ok()   { printf "${GREEN}  ✓ %s${RESET}\n" "$*"; }
note() { printf "${DIM}  %s${RESET}\n" "$*"; }
warn() { printf "${YELLOW}  ! %s${RESET}\n" "$*"; }

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# --- Prerequisites, all of them, before anything is built -------------------

step "Prerequisites"

[ -n "$VERSION" ] || fail "--version is required"
[ -n "$BASE_URL" ] || fail "--base-url is required; there is no correct default, because the archives live wherever this release is published"
[ -n "$PROTOCOL_VERSIONS" ] || fail "--protocol-versions is required; an empty list means 'unknown', which blocks every automatic update"

case "$VERSION" in
  v*) fail "--version is ${VERSION}; semantic versions have no leading v" ;;
esac
# The same parser the agent and the controller use, so a version this accepts
# is one they can both order. Checked here rather than at signing time: a
# 90-second cross-compile for three architectures is a poor way to find out.
PYTHON="apps/server/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="python3"
"$PYTHON" - "$VERSION" <<'PY' || fail "--version is not a semantic version"
import re
import sys
pattern = (
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
sys.exit(0 if re.match(pattern, sys.argv[1]) else 1)
PY
ok "version ${VERSION} is a semantic version"

if [ "$EPHEMERAL" -eq 1 ]; then
  [ -z "$PRIVATE_KEY" ] || fail "--private-key and --ephemeral-key are mutually exclusive"
  [ -z "$ADD_SIGNATURE" ] || fail "--add-signature needs a real key run; a rotation overlap with a throwaway key is meaningless"
  warn "--ephemeral-key: the signature will prove possession of a key created"
  warn "  seconds ago and destroyed at exit. It establishes NO publisher"
  warn "  identity and the result must not be published."
else
  [ -n "$PRIVATE_KEY" ] || fail "--private-key is required (or --ephemeral-key to validate the pipeline without one)"
  [ -f "$PRIVATE_KEY" ] || fail "--private-key ${PRIVATE_KEY} does not exist"
  # A key anyone can read is a key anyone can sign with. Refused rather than
  # warned about: this is the only secret in the trust model.
  mode="$(stat -c '%a' "$PRIVATE_KEY")"
  case "$mode" in
    600|400) ok "signing key ${PRIVATE_KEY} (mode ${mode})" ;;
    *) fail "--private-key ${PRIVATE_KEY} is mode ${mode}; a release signing key must be 600 or 400" ;;
  esac
fi

[ -z "$ADD_SIGNATURE" ] || [ -f "$ADD_SIGNATURE" ] || fail "--add-signature ${ADD_SIGNATURE} does not exist"
[ -z "$MERGE_INTO" ] || [ -f "$MERGE_INTO" ] || fail "--merge-into ${MERGE_INTO} does not exist"

command -v go >/dev/null 2>&1 || fail "go is not on PATH"
"$PYTHON" -c 'import cryptography' 2>/dev/null \
  || fail "the signing tooling needs the 'cryptography' package: run 'make install-server'"
ok "toolchain present"

ARTIFACT_DIR="${OUTPUT_DIR}/agent"
MANIFEST="${OUTPUT_DIR}/agent-releases.json"

# --- 1. Build ---------------------------------------------------------------

if [ "$SKIP_BUILD" -eq 1 ]; then
  step "Build (skipped)"
  [ -f "${ARTIFACT_DIR}/artifacts.json" ] \
    || fail "--skip-build was given and ${ARTIFACT_DIR}/artifacts.json does not exist"
  note "reusing ${ARTIFACT_DIR}"
else
  step "Build"
  ./scripts/build-agent-artifacts.sh \
    --version "$VERSION" --output "$ARTIFACT_DIR" --arch "$ARCHES" --clean
fi

# --- 2. Verify the artifacts against their own manifest ---------------------

step "Verify artifacts"
./scripts/verify-artifacts.sh "$ARTIFACT_DIR" --expect-version "$VERSION" --execute

# --- 3. Derive the release manifest -----------------------------------------

step "Release manifest"
manifest_arguments=(
  --artifacts "${ARTIFACT_DIR}/artifacts.json"
  --base-url "$BASE_URL"
  --output "$MANIFEST"
  --channel "$CHANNEL"
  --protocol-versions "$PROTOCOL_VERSIONS"
)
[ -z "$MERGE_INTO" ] || manifest_arguments+=(--merge-into "$MERGE_INTO")
[ -z "$MIN_CONTROLLER" ] || manifest_arguments+=(--min-controller "$MIN_CONTROLLER")
[ -z "$SCHEMA_REQUIREMENT" ] || manifest_arguments+=(--schema-requirement "$SCHEMA_REQUIREMENT")
[ -z "$NOTES_URL" ] || manifest_arguments+=(--notes-url "$NOTES_URL")
[ -z "$MINIMUM_SUPPORTED" ] || manifest_arguments+=(--minimum-supported "$MINIMUM_SUPPORTED")

"$PYTHON" scripts/build-release-manifest.py "${manifest_arguments[@]}"
ok "$MANIFEST"

# --- 4. Sign, and verify the signature --------------------------------------

step "Signature"

EPHEMERAL_DIR=""
cleanup() {
  if [ -n "$EPHEMERAL_DIR" ] && [ -d "$EPHEMERAL_DIR" ]; then
    # Shredded rather than unlinked. The key is worthless, and a habit that
    # treats a throwaway key carelessly is the habit that will one day be
    # applied to a real one.
    find "$EPHEMERAL_DIR" -type f -exec shred -u {} + 2>/dev/null || true
    rm -rf "$EPHEMERAL_DIR"
  fi
}
trap cleanup EXIT

SIGNING_KEY="$PRIVATE_KEY"
PUBLIC_KEY=""
if [ "$EPHEMERAL" -eq 1 ]; then
  EPHEMERAL_DIR="$(mktemp -d)"
  chmod 700 "$EPHEMERAL_DIR"
  SIGNING_KEY="${EPHEMERAL_DIR}/release.key"
  PUBLIC_KEY="${EPHEMERAL_DIR}/release.pub"
  "$PYTHON" scripts/sign-release.py keygen \
    --private-key "$SIGNING_KEY" --public-key "$PUBLIC_KEY" >/dev/null
  ok "generated a throwaway keypair in $EPHEMERAL_DIR"
fi

rm -f "${MANIFEST}.sig"
"$PYTHON" scripts/sign-release.py sign --manifest "$MANIFEST" --private-key "$SIGNING_KEY"

if [ -n "$ADD_SIGNATURE" ]; then
  # The rotation overlap. Added second, so the primary key's signature is
  # first in the file and an operator reading it sees the current key first.
  "$PYTHON" scripts/sign-release.py sign \
    --manifest "$MANIFEST" --private-key "$ADD_SIGNATURE" --add
fi

# Verified against the public key derived from the private one, independently
# of the signing step's own check. Two checks of the same thing look
# redundant and are not: the first proves the tool wrote what it meant to,
# this proves the file on disk is what a verifier will read.
if [ -z "$PUBLIC_KEY" ]; then
  PUBLIC_KEY="$(mktemp)"
  trap 'cleanup; rm -f "$PUBLIC_KEY"' EXIT
  "$PYTHON" - "$SIGNING_KEY" "$PUBLIC_KEY" <<'PY'
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hcc.updates.signing import encode_public_key

private = Ed25519PrivateKey.from_private_bytes(Path(sys.argv[1]).read_bytes())
raw = private.public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw
)
Path(sys.argv[2]).write_text(encode_public_key(raw) + "\n", encoding="utf-8")
PY
fi
"$PYTHON" scripts/sign-release.py verify --manifest "$MANIFEST" --public-key "$PUBLIC_KEY"

# The marker. A later step that is about to publish can refuse on its
# presence, which is a check a human cannot forget to make.
MARKER="${MANIFEST}.ephemeral"
rm -f "$MARKER"
if [ "$EPHEMERAL" -eq 1 ]; then
  cat > "$MARKER" <<EOF
This manifest was signed with a throwaway key generated by
scripts/prepare-release.sh --ephemeral-key and destroyed immediately after.

The signature is cryptographically valid and establishes NOTHING about
publisher identity. Do not publish this manifest. Re-run prepare-release.sh
with --private-key pointing at the real signing key.
EOF
  warn "wrote ${MARKER} — this manifest must not be published"
fi

# --- What was produced, and what was not ------------------------------------

step "Prepared"
printf "  %s\n" "$ARTIFACT_DIR/"
for file in "$ARTIFACT_DIR"/*.tar.gz; do
  [ -e "$file" ] || continue
  printf "    %s\n" "$(basename "$file")"
done
printf "  %s\n" "$MANIFEST"
printf "  %s\n" "${MANIFEST}.sig"
[ ! -f "$MARKER" ] || printf "  %s\n" "$MARKER"

printf "\n${DIM}Nothing was tagged, uploaded or published. Next, by hand:${RESET}\n"
printf "${DIM}  1. Upload %s/*.tar.gz, the checksums and the SBOM to the release${RESET}\n" "$ARTIFACT_DIR"
printf "${DIM}     at %s${RESET}\n" "$BASE_URL"
printf "${DIM}  2. Ship %s and its .sig with the controller image${RESET}\n" "$MANIFEST"
printf "${DIM}  3. Configure HCC_RELEASE_SIGNING_KEYS with the public key${RESET}\n"
printf "${DIM}  4. Build agents with that key stamped in — see docs/RELEASING.md${RESET}\n"

if [ "$EPHEMERAL" -eq 1 ]; then
  printf "\n${YELLOW}This was an ephemeral-key run. Step 1 onwards does not apply.${RESET}\n"
fi
