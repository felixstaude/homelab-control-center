#!/usr/bin/env bash
#
# Build the agent's release artifacts.
#
# One command, run identically by a developer and by the release workflow, so
# that what CI produces is what somebody can reproduce on their own machine.
# Nothing here publishes anything: it writes files into dist/agent and stops.
#
# Produces, for every target architecture:
#
#   hcc-agent_<version>_linux_<arch>.tar.gz   flat archive: the binary + LICENSE
#   checksums.txt                              sha256sum format, all archives
#   artifacts.json                             the machine-readable manifest
#   hcc-agent_<version>.sbom.spdx.json         SPDX 2.3 for the Go module graph
#
# Usage:
#   ./scripts/build-agent-artifacts.sh [--version X.Y.Z] [--output DIR]
#                                      [--arch amd64,arm64] [--clean]
#
# # Reproducibility
#
# Two builds of one commit produce byte-identical archives, and the release
# workflow proves it by building twice and comparing. That is not a vanity
# property: it is the only way a third party can check that the published
# binary came from the published source, and it is what makes the digest in
# the controller's install manifest mean something.
#
# What it takes, all of it load-bearing:
#
#   * -trimpath, so no absolute path from the build machine is baked in.
#   * CGO_ENABLED=0, so there is no host toolchain in the output and the
#     result is static — one artifact per architecture works on glibc and
#     musl alike, which is what a NAS needs.
#   * SOURCE_DATE_EPOCH pinned to the commit date, used for the stamped build
#     date *and* for every mtime in the archive.
#   * tar --sort=name with fixed owner, group and mode, because tar otherwise
#     records the build machine's uid and the order readdir happened to give.
#   * gzip -n, because gzip otherwise writes the source filename and the
#     current time into its own header.
#
# The one thing it does not pin is the Go toolchain: a different compiler
# version produces a different binary, legitimately. The manifest records
# which toolchain built it, so a mismatch is diagnosable rather than
# mysterious.

set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

GREEN='\033[0;32m' RED='\033[0;31m' CYAN='\033[0;36m' DIM='\033[0;90m' RESET='\033[0m'

AGENT_DIR="agents/linux"
OUTPUT_DIR="dist/agent"
VERSION=""
ARCHES="amd64,arm64,arm"
CLEAN=0

die() {
  printf "${RED}✗ %s${RESET}\n" "$*" >&2
  exit 1
}
step() { printf "${CYAN}▸ %s${RESET}\n" "$*"; }
ok() { printf "${GREEN}  ✓ %s${RESET}\n" "$*"; }
note() { printf "${DIM}  %s${RESET}\n" "$*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --version) VERSION="${2:?--version needs a value}"; shift 2 ;;
    --output) OUTPUT_DIR="${2:?--output needs a value}"; shift 2 ;;
    --arch) ARCHES="${2:?--arch needs a value}"; shift 2 ;;
    --clean) CLEAN=1; shift ;;
    -h|--help) sed -n '2,45p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

# --- Version -----------------------------------------------------------------
# The committed VERSION file is the single source of truth. A caller may
# override it — the release workflow passes the tag it was triggered by — and
# the workflow separately checks that the tag and the file agree, so an
# override here is a deliberate act rather than a way to publish a mismatch.
if [ -z "$VERSION" ]; then
  [ -f VERSION ] || die "no VERSION file and no --version"
  VERSION="$(tr -d '[:space:]' < VERSION)"
fi
[ -n "$VERSION" ] || die "version is empty"
case "$VERSION" in
  *[![:alnum:].+-]*) die "version contains characters that do not belong in a filename: $VERSION" ;;
esac

# --- Build stamps ------------------------------------------------------------
COMMIT="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
COMMIT_SHORT="${COMMIT:0:12}"

# SOURCE_DATE_EPOCH from the environment if the caller set one, else the
# commit's own date. Falling back to `date +%s` would make every build
# different, which is the opposite of the point.
if [ -z "${SOURCE_DATE_EPOCH:-}" ]; then
  if SOURCE_DATE_EPOCH="$(git log -1 --pretty=%ct 2>/dev/null)" && [ -n "$SOURCE_DATE_EPOCH" ]; then
    :
  else
    die "not a git checkout and SOURCE_DATE_EPOCH is unset; cannot build reproducibly"
  fi
fi
export SOURCE_DATE_EPOCH
BUILD_DATE="$(date -u -d "@$SOURCE_DATE_EPOCH" +%Y-%m-%dT%H:%M:%SZ)"

command -v go >/dev/null 2>&1 || die "go is not installed"
GO_VERSION="$(go version | awk '{print $3}')"

# Exported for the Python helpers below, which assemble the SBOM and the
# manifest. Passed through the environment rather than as argv so that adding
# a field does not mean renumbering positional arguments in two places.
export HCC_SBOM_VERSION="$VERSION"
export HCC_SBOM_COMMIT="$COMMIT_SHORT"
export HCC_SBOM_COMMIT_FULL="$COMMIT"
export HCC_BUILD_DATE="$BUILD_DATE"
export HCC_GO_VERSION="$GO_VERSION"

# GNU tar's --sort is what makes the archive deterministic; BSD tar has no
# equivalent. Checked rather than assumed, because the failure otherwise is a
# silently non-reproducible release.
tar --version 2>/dev/null | head -1 | grep -qi 'gnu tar' \
  || die "GNU tar is required (found: $(tar --version 2>/dev/null | head -1))"

# --- Output ------------------------------------------------------------------
if [ "$CLEAN" = 1 ] && [ -d "$OUTPUT_DIR" ]; then
  rm -rf "$OUTPUT_DIR"
fi
mkdir -p "$OUTPUT_DIR"

step "Building hcc-agent $VERSION"
note "commit      $COMMIT_SHORT"
note "build date  $BUILD_DATE (SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH)"
note "toolchain   $GO_VERSION"
note "output      $OUTPUT_DIR"
echo

LDFLAGS_PKG="github.com/felixstaude/homelab-control-center/agents/linux/internal/buildinfo"
LDFLAGS="-s -w"
LDFLAGS="$LDFLAGS -X ${LDFLAGS_PKG}.version=${VERSION}"
LDFLAGS="$LDFLAGS -X ${LDFLAGS_PKG}.commit=${COMMIT_SHORT}"
LDFLAGS="$LDFLAGS -X ${LDFLAGS_PKG}.buildDate=${BUILD_DATE}"

STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT

# Collected for the manifest, one line per artifact:
#   name<TAB>arch<TAB>sha256<TAB>size<TAB>binary_sha256<TAB>binary_size
MANIFEST_ROWS="$STAGING/rows.tsv"
: > "$MANIFEST_ROWS"

IFS=',' read -r -a ARCH_LIST <<< "$ARCHES"
for arch in "${ARCH_LIST[@]}"; do
  arch="$(echo "$arch" | tr -d '[:space:]')"
  [ -n "$arch" ] || continue

  case "$arch" in
    amd64|arm64|arm) ;;
    *) die "unsupported architecture: $arch (amd64, arm64, arm)" ;;
  esac

  step "linux/$arch"
  work="$STAGING/$arch"
  mkdir -p "$work"

  # GOARM is set for the 32-bit ARM target and only for it. Unset, the
  # toolchain's default has moved between releases, which would make the
  # output depend on the compiler's opinion rather than on this file. v6 is
  # the floor that covers a Raspberry Pi 1 and Zero; anything newer runs a
  # v6 binary fine.
  #
  # Assembled as an array passed to `env` rather than as an assignment prefix:
  # bash parses assignment prefixes before expansion, so a conditional
  # `${goarm:+GOARM=$goarm}` is read as the *command* and fails with
  # "GOARM=6: command not found".
  build_env=(CGO_ENABLED=0 GOOS=linux "GOARCH=$arch")
  if [ "$arch" = "arm" ]; then
    build_env+=(GOARM=6)
  fi

  (
    cd "$AGENT_DIR"
    env "${build_env[@]}" \
      go build -trimpath -mod=vendor -ldflags "$LDFLAGS" \
      -o "$work/hcc-agent" ./cmd/hcc-agent
  )
  binary_size="$(stat -c %s "$work/hcc-agent")"
  binary_sha="$(sha256sum "$work/hcc-agent" | cut -d' ' -f1)"
  ok "built ($(numfmt --to=iec --suffix=B "$binary_size" 2>/dev/null || echo "$binary_size bytes"))"

  # The archive is flat on purpose. The install command the interface renders
  # is `tar xzf …` followed by `./hcc-agent install`, so a leading directory
  # would make the documented command wrong.
  cp LICENSE "$work/LICENSE"
  chmod 0755 "$work/hcc-agent"
  chmod 0644 "$work/LICENSE"

  archive="hcc-agent_${VERSION}_linux_${arch}.tar.gz"
  tar \
    --sort=name \
    --format=gnu \
    --owner=0 --group=0 --numeric-owner \
    --mtime="@$SOURCE_DATE_EPOCH" \
    -C "$work" -cf "$STAGING/$archive.tar" hcc-agent LICENSE
  gzip -n -9 -c "$STAGING/$archive.tar" > "$OUTPUT_DIR/$archive"
  rm -f "$STAGING/$archive.tar"

  archive_size="$(stat -c %s "$OUTPUT_DIR/$archive")"
  archive_sha="$(sha256sum "$OUTPUT_DIR/$archive" | cut -d' ' -f1)"
  ok "packaged $archive"
  note "sha256 $archive_sha"

  printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$archive" "$arch" "$archive_sha" "$archive_size" "$binary_sha" "$binary_size" \
    >> "$MANIFEST_ROWS"
  echo
done

# --- checksums.txt -----------------------------------------------------------
# sha256sum's own format, so that `sha256sum -c checksums.txt` works and the
# one-line form the interface renders — `echo '<digest>  <file>' | sha256sum -c -`
# — is the same mechanism rather than a lookalike.
step "checksums.txt"
(
  cd "$OUTPUT_DIR"
  # Sorted, so the file does not depend on the order the loop above ran in.
  sha256sum hcc-agent_"$VERSION"_linux_*.tar.gz | sort -k2 > checksums.txt
)
ok "$(wc -l < "$OUTPUT_DIR/checksums.txt") entries"
echo

# --- SBOM --------------------------------------------------------------------
# Generated from `go list -deps`, which is the actual build's module set
# rather than a guess from go.mod. SPDX 2.3, minimal but valid: a document
# with a package per module, each carrying its version and the module proxy's
# own hash where one exists.
#
# Written by hand rather than with a third-party tool because the dependency
# graph here is one vendored package plus the standard library, and adding a
# supply-chain tool to document a supply chain of one is poor arithmetic.
step "SBOM"
SBOM="hcc-agent_${VERSION}.sbom.spdx.json"
(
  cd "$AGENT_DIR"
  go list -deps -mod=vendor -f '{{with .Module}}{{.Path}} {{.Version}}{{end}}' ./cmd/hcc-agent \
    | sort -u | grep -v '^$' > "$STAGING/modules.txt"
)
python3 - "$STAGING/modules.txt" "$OUTPUT_DIR/$SBOM" <<'PYTHON'
"""Emit a minimal, valid SPDX 2.3 document for the agent's module set."""

import json
import os
import sys
from datetime import UTC, datetime

modules_path, output_path = sys.argv[1], sys.argv[2]
version = os.environ["HCC_SBOM_VERSION"]
commit = os.environ["HCC_SBOM_COMMIT"]
epoch = int(os.environ["SOURCE_DATE_EPOCH"])

created = datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

packages = [
    {
        "SPDXID": "SPDXRef-Package-hcc-agent",
        "name": "hcc-agent",
        "versionInfo": version,
        "downloadLocation": "https://github.com/felixstaude/homelab-control-center",
        "filesAnalyzed": False,
        "licenseConcluded": "AGPL-3.0-or-later",
        "licenseDeclared": "AGPL-3.0-or-later",
        "copyrightText": "Copyright (c) 2026 Felix Staude",
        "supplier": "Person: Felix Staude",
        "sourceInfo": f"built from commit {commit}",
    }
]
relationships = [
    {
        "spdxElementId": "SPDXRef-DOCUMENT",
        "relationshipType": "DESCRIBES",
        "relatedSpdxElement": "SPDXRef-Package-hcc-agent",
    }
]

with open(modules_path, encoding="utf-8") as handle:
    entries = [line.split() for line in handle if line.strip()]

for index, entry in enumerate(entries):
    path = entry[0]
    module_version = entry[1] if len(entry) > 1 else "NOASSERTION"
    if path == "github.com/felixstaude/homelab-control-center/agents/linux":
        # The main module is already the package above.
        continue
    identifier = f"SPDXRef-Package-{index}-" + path.replace("/", "-").replace(".", "-")
    packages.append(
        {
            "SPDXID": identifier,
            "name": path,
            "versionInfo": module_version,
            "downloadLocation": f"https://proxy.golang.org/{path}/@v/{module_version}.zip",
            "filesAnalyzed": False,
            "licenseConcluded": "NOASSERTION",
            "licenseDeclared": "NOASSERTION",
            "copyrightText": "NOASSERTION",
            "externalRefs": [
                {
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": f"pkg:golang/{path}@{module_version}",
                }
            ],
        }
    )
    relationships.append(
        {
            "spdxElementId": "SPDXRef-Package-hcc-agent",
            "relationshipType": "DEPENDS_ON",
            "relatedSpdxElement": identifier,
        }
    )

document = {
    "spdxVersion": "SPDX-2.3",
    "dataLicense": "CC0-1.0",
    "SPDXID": "SPDXRef-DOCUMENT",
    "name": f"hcc-agent-{version}",
    "documentNamespace": (
        "https://github.com/felixstaude/homelab-control-center/spdx/"
        f"hcc-agent-{version}-{commit}"
    ),
    "creationInfo": {
        "created": created,
        "creators": ["Tool: scripts/build-agent-artifacts.sh", "Person: Felix Staude"],
    },
    "packages": packages,
    "relationships": relationships,
}

with open(output_path, "w", encoding="utf-8") as handle:
    json.dump(document, handle, indent=2, sort_keys=True)
    handle.write("\n")
PYTHON
ok "$SBOM ($(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["packages"]))' "$OUTPUT_DIR/$SBOM") packages)"
echo

# --- artifacts.json ----------------------------------------------------------
# The manifest the controller's install manifest is derived from, and the
# input to scripts/verify-artifacts.sh. Schema-versioned from the first
# release, because a consumer that cannot tell which shape it is reading is a
# consumer that breaks on the second one.
step "artifacts.json"
python3 - "$MANIFEST_ROWS" "$OUTPUT_DIR/artifacts.json" <<'PYTHON'
import hashlib
import json
import os
import sys

rows_path, output_path = sys.argv[1], sys.argv[2]
output_dir = os.path.dirname(output_path)

#: How much a given platform has actually been exercised, as opposed to
#: compiled. The release workflow upgrades these from the build job's claim to
#: what the verification job measured; this file states the floor.
#:
#: "supported" means: built, executed, and covered by the installation matrix.
#: "best-effort" means: built and executed under emulation, no hardware.
TIERS = {
    "amd64": "supported",
    "arm64": "supported",
    "arm": "best-effort",
}

artifacts = []
with open(rows_path, encoding="utf-8") as handle:
    for line in handle:
        if not line.strip():
            continue
        name, arch, sha, size, binary_sha, binary_size = line.rstrip("\n").split("\t")
        artifacts.append(
            {
                "name": name,
                "kind": "archive",
                "os": "linux",
                "arch": arch,
                "tier": TIERS.get(arch, "best-effort"),
                "sha256": sha,
                "size_bytes": int(size),
                "binary": {
                    "path": "hcc-agent",
                    "sha256": binary_sha,
                    "size_bytes": int(binary_size),
                },
            }
        )

artifacts.sort(key=lambda entry: entry["name"])

version = os.environ["HCC_SBOM_VERSION"]
sbom_name = f"hcc-agent_{version}.sbom.spdx.json"
checksums_name = "checksums.txt"


def digest(name: str) -> str:
    with open(os.path.join(output_dir, name), "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


manifest = {
    # Bumped when a consumer would have to change to read this file.
    "schema_version": 1,
    "product": "hcc-agent",
    "version": version,
    "commit": os.environ["HCC_SBOM_COMMIT_FULL"],
    "built_at": os.environ["HCC_BUILD_DATE"],
    "source_date_epoch": int(os.environ["SOURCE_DATE_EPOCH"]),
    "go_version": os.environ["HCC_GO_VERSION"],
    "reproducible": True,
    "artifacts": artifacts,
    "checksums": {"name": checksums_name, "sha256": digest(checksums_name)},
    "sbom": {"name": sbom_name, "format": "SPDX-2.3", "sha256": digest(sbom_name)},
}

with open(output_path, "w", encoding="utf-8") as handle:
    json.dump(manifest, handle, indent=2)
    handle.write("\n")
PYTHON
ok "artifacts.json"
echo

printf "${GREEN}✓ %s artifacts in %s${RESET}\n" "$(ls -1 "$OUTPUT_DIR" | wc -l)" "$OUTPUT_DIR"
ls -la "$OUTPUT_DIR" | tail -n +2
