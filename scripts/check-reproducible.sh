#!/usr/bin/env bash
#
# Prove the agent build is reproducible.
#
# Builds the full artifact set twice, into two directories, and compares every
# digest. Identical digests mean a third party can take the published source,
# run one command, and get bytes that match the published release — which is
# the only thing that makes the digest in the controller's install manifest a
# statement about the source rather than a statement about one machine.
#
# Both builds run in this process, on this machine, with the same toolchain.
# That proves the *build* is deterministic — no timestamps, no paths, no
# readdir order leaking in. It does **not** prove that a different machine
# gets the same answer; a different Go version certainly will not, which is
# why the manifest records the toolchain.
#
# Usage:  ./scripts/check-reproducible.sh      (or: make check-reproducible)

set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

GREEN='\033[0;32m' RED='\033[0;31m' CYAN='\033[0;36m' DIM='\033[0;90m' RESET='\033[0m'

WORKSPACE="$(mktemp -d)"
trap 'rm -rf "$WORKSPACE"' EXIT

printf "${CYAN}▸ First build${RESET}\n"
./scripts/build-agent-artifacts.sh --output "$WORKSPACE/first" >"$WORKSPACE/first.log" 2>&1 \
  || { cat "$WORKSPACE/first.log"; exit 1; }
printf "${DIM}  $(wc -l < "$WORKSPACE/first/checksums.txt") archives${RESET}\n"

# The Go build cache is deliberately left alone between the two builds. A
# clean second build would be a stronger claim, and also a slower one that
# tests the cache rather than the output; GOFLAGS=-a is available to anyone
# who wants it.
printf "${CYAN}▸ Second build${RESET}\n"
./scripts/build-agent-artifacts.sh --output "$WORKSPACE/second" >"$WORKSPACE/second.log" 2>&1 \
  || { cat "$WORKSPACE/second.log"; exit 1; }

printf "${CYAN}▸ Comparing${RESET}\n"
if ! diff -u "$WORKSPACE/first/checksums.txt" "$WORKSPACE/second/checksums.txt"; then
  printf "${RED}✗ the two builds produced different archives${RESET}\n"
  exit 1
fi
printf "${GREEN}  ✓ every archive digest is identical${RESET}\n"

# artifacts.json too, which catches a manifest that embeds something
# incidental — a wall-clock build date, say — even when the archives match.
if ! diff -u "$WORKSPACE/first/artifacts.json" "$WORKSPACE/second/artifacts.json"; then
  printf "${RED}✗ the two builds produced different manifests${RESET}\n"
  exit 1
fi
printf "${GREEN}  ✓ artifacts.json is identical${RESET}\n"

if ! diff -u \
  <(cd "$WORKSPACE/first" && ls -1 && sha256sum -- *) \
  <(cd "$WORKSPACE/second" && ls -1 && sha256sum -- *)
then
  printf "${RED}✗ the two output directories differ${RESET}\n"
  exit 1
fi

echo
printf "${GREEN}✓ the build is reproducible with this toolchain ($(go version | awk '{print $3}'))${RESET}\n"
