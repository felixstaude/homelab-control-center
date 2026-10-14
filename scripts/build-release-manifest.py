#!/usr/bin/env python3
"""Derive the controller's agent release manifest from a build's artifacts.

The controller renders an install command carrying the expected SHA-256 of the
archive it tells somebody to download. That digest has to be the *build's own*
rather than one transcribed by hand, so this reads the ``artifacts.json`` that
``scripts/build-agent-artifacts.sh`` wrote and turns it into the shape
``hcc/agents/releases.py`` validates.

Usage:
    scripts/build-release-manifest.py \\
        --artifacts dist/agent/artifacts.json \\
        --base-url https://github.com/felixstaude/homelab-control-center/releases/download/v0.1.0 \\
        --output dist/agent-releases.json

    # Merge a new version into an existing manifest, keeping the old ones so
    # that an operator upgrading from two versions back still sees a download.
    scripts/build-release-manifest.py --artifacts … --base-url … \\
        --merge-into dist/agent-releases.json --output dist/agent-releases.json

# Why --base-url is required and has no default

Because there is no correct default. The archives live wherever the release
was published, or wherever an operator mirrored them, and guessing a GitHub
URL for a release that may not exist is how a product ends up rendering a
command that 404s. The caller states where the bytes are; this script states
what they hash to.

Nothing here is run by a normal build. The controller ships **no** manifest
today — no release of this project has been published — and the endpoint says
so rather than inventing one. This script exists so that the first release can
produce one from its own artifacts in one command.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

#: Must match MANIFEST_SCHEMA_VERSION in hcc/agents/releases.py. Stated as a
#: literal rather than imported, because this script runs from a release
#: workflow that has no reason to install the server package.
SCHEMA_VERSION = 1

#: Go architecture → the platform key the controller and the interface use.
PLATFORMS = {
    "amd64": "linux/amd64",
    "arm64": "linux/arm64",
    "arm": "linux/arm",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifacts", required=True, type=Path, help="the build's artifacts.json"
    )
    parser.add_argument(
        "--base-url",
        required=True,
        help="where the archives are published, without a trailing slash",
    )
    parser.add_argument("--output", required=True, type=Path, help="where to write the manifest")
    parser.add_argument(
        "--merge-into",
        type=Path,
        help="an existing manifest whose other versions should be kept",
    )
    parser.add_argument(
        "--minimum-supported",
        help=(
            "the oldest agent version this controller works with. "
            "Defaults to the version being added, which is correct for a "
            "first release and wrong for any later one."
        ),
    )
    arguments = parser.parse_args()

    artifacts = json.loads(arguments.artifacts.read_text(encoding="utf-8"))
    if artifacts.get("schema_version") != SCHEMA_VERSION:
        print(
            f"artifacts.json has schema_version {artifacts.get('schema_version')!r}; "
            f"this script understands {SCHEMA_VERSION}",
            file=sys.stderr,
        )
        return 1
    if artifacts.get("product") != "hcc-agent":
        print(f"artifacts.json is for {artifacts.get('product')!r}, not hcc-agent", file=sys.stderr)
        return 1

    version = artifacts["version"]
    base_url = arguments.base_url.rstrip("/")
    if not base_url.startswith("https://") and not base_url.startswith(
        ("http://127.0.0.1", "http://localhost", "http://[::1]")
    ):
        # The same rule the controller enforces when it reads this file,
        # applied here so the failure is at build time rather than at the
        # first request.
        print(
            "--base-url must be https (or http on loopback, for a local mirror)",
            file=sys.stderr,
        )
        return 1

    platforms: dict[str, dict[str, object]] = {}
    for entry in artifacts["artifacts"]:
        if entry.get("kind") != "archive" or entry.get("os") != "linux":
            continue
        platform = PLATFORMS.get(entry["arch"])
        if platform is None:
            print(f"skipping unknown architecture {entry['arch']!r}", file=sys.stderr)
            continue
        platforms[platform] = {
            "archive": entry["name"],
            "url": f"{base_url}/{entry['name']}",
            "sha256": entry["sha256"],
            "size_bytes": entry["size_bytes"],
            "tier": entry["tier"],
        }

    if not platforms:
        print("no usable archives in artifacts.json", file=sys.stderr)
        return 1

    releases: dict[str, dict[str, object]] = {}
    minimum = arguments.minimum_supported or version
    if arguments.merge_into is not None and arguments.merge_into.exists():
        existing = json.loads(arguments.merge_into.read_text(encoding="utf-8"))
        if existing.get("schema_version") != SCHEMA_VERSION:
            print(
                f"{arguments.merge_into} has schema_version "
                f"{existing.get('schema_version')!r}; refusing to merge",
                file=sys.stderr,
            )
            return 1
        releases = dict(existing.get("releases") or {})
        if arguments.minimum_supported is None:
            # Keeping the existing floor is the right default when merging: a
            # new release does not by itself raise the oldest agent the
            # controller works with, and silently raising it would tell
            # operators to upgrade hosts that are fine.
            minimum = existing.get("minimum_supported", version)

    releases[version] = platforms

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "recommended": version,
        "minimum_supported": minimum,
        "releases": releases,
    }

    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"wrote {arguments.output}")
    print(f"  recommended        {version}")
    print(f"  minimum supported  {minimum}")
    for platform, artifact in sorted(platforms.items()):
        print(f"  {platform:<14} {artifact['sha256'][:16]}…  {artifact['url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
