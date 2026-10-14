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

# Why --protocol-versions is required

Because an empty list is not "no constraint" — it is "unknown", and
``hcc.updates.compat`` treats unknown as a blocker for an automatic update.
A release built without it would install fine by hand and then silently never
be picked up by any host with an automatic policy, which is the hardest kind
of bug to notice: everything works, nothing happens. Stating it is cheap;
discovering it is not.

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

# This writes the manifest; it does not sign it

Authenticity is a detached ``.sig`` over these exact bytes, made by
``scripts/sign-release.py`` with a key this script never sees. Keeping the two
apart is the point: a build machine that could sign would make the signature
worth no more than the build machine.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

#: Must match MANIFEST_SCHEMA_VERSION in hcc/agents/releases.py. Stated as a
#: literal rather than imported, because this script runs from a release
#: workflow that has no reason to install the server package.
SCHEMA_VERSION = 2

#: The shape of the ``artifacts.json`` this script *reads*, which is written by
#: ``scripts/build-agent-artifacts.sh``.
#:
#: A separate constant from :data:`SCHEMA_VERSION`, which they were not while
#: both happened to be 1. The moment the manifest went to v2 the single
#: constant started refusing every real artifacts.json with "this script
#: understands 2" — two unrelated schemas sharing one number is a coincidence,
#: not a fact.
ARTIFACTS_SCHEMA_VERSION = 1

#: Every shape this script can *read* when merging. A manifest published by an
#: older release is a legitimate input, and refusing it would mean the first
#: v2 release could not carry forward the downloads of the ones before it.
MERGEABLE_SCHEMA_VERSIONS = (1, 2)

#: Must match CHANNELS in hcc/agents/releases.py, same order.
CHANNELS = ("stable", "beta", "alpha")

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
    parser.add_argument(
        "--channel",
        default="stable",
        choices=CHANNELS,
        help="which channel this release goes into (default: stable)",
    )
    parser.add_argument(
        "--protocol-versions",
        required=True,
        help=(
            "comma-separated agent protocol versions this release speaks, "
            "e.g. '1,2'. Required: see the note in this file's docstring."
        ),
    )
    parser.add_argument(
        "--min-controller",
        help="the oldest controller version that may drive this release",
    )
    parser.add_argument(
        "--schema-requirement",
        help=(
            "the Alembic revision this release needs applied, e.g. "
            "'0010_update_management'. Omit when it needs nothing in particular."
        ),
    )
    parser.add_argument("--notes-url", help="where the release notes are")
    parser.add_argument(
        "--released-at",
        help="RFC 3339 timestamp, for display. Defaults to now, in UTC.",
    )
    arguments = parser.parse_args()

    try:
        protocol_versions = _parse_protocol_versions(arguments.protocol_versions)
    except ValueError as error:
        print(f"--protocol-versions: {error}", file=sys.stderr)
        return 1

    artifacts = json.loads(arguments.artifacts.read_text(encoding="utf-8"))
    if artifacts.get("schema_version") != ARTIFACTS_SCHEMA_VERSION:
        print(
            f"artifacts.json has schema_version {artifacts.get('schema_version')!r}; "
            f"this script understands {ARTIFACTS_SCHEMA_VERSION}",
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

    releases: dict[str, Any] = {}
    channels: dict[str, str] = {}
    minimum = arguments.minimum_supported or version
    # Deliberately not defaulted to `version`: a release in a pre-release
    # channel must not become the recommendation, and starting from `version`
    # would make the check below pass for exactly the case it exists to catch.
    recommended: str | None = None
    if arguments.merge_into is not None and arguments.merge_into.exists():
        existing = json.loads(arguments.merge_into.read_text(encoding="utf-8"))
        if existing.get("schema_version") not in MERGEABLE_SCHEMA_VERSIONS:
            understood = ", ".join(str(value) for value in MERGEABLE_SCHEMA_VERSIONS)
            print(
                f"{arguments.merge_into} has schema_version "
                f"{existing.get('schema_version')!r}; this script can merge {understood}",
                file=sys.stderr,
            )
            return 1
        releases = _upconvert_releases(existing.get("releases") or {})
        channels = dict(existing.get("channels") or {})
        if not channels and isinstance(existing.get("recommended"), str):
            # A v1 manifest has no channel table and its `recommended` is the
            # stable release by definition. The same synthesis the controller
            # performs on read, done here so a merged file is a real v2 file.
            channels["stable"] = existing["recommended"]
        if isinstance(existing.get("recommended"), str):
            recommended = existing["recommended"]
        if arguments.minimum_supported is None:
            # Keeping the existing floor is the right default when merging: a
            # new release does not by itself raise the oldest agent the
            # controller works with, and silently raising it would tell
            # operators to upgrade hosts that are fine.
            minimum = existing.get("minimum_supported", version)

    released_at = arguments.released_at or (
        dt.datetime.now(tz=dt.UTC).replace(microsecond=0).isoformat()
    )

    entry: dict[str, Any] = {
        "channel": arguments.channel,
        "released_at": released_at,
        "protocol_versions": protocol_versions,
        "platforms": platforms,
    }
    # Omitted rather than written as null, because absent means "no
    # constraint" and an explicit null says the same thing more loudly. A
    # manifest somebody reads should only contain the constraints that exist.
    if arguments.min_controller:
        entry["min_controller"] = arguments.min_controller
    if arguments.schema_requirement:
        entry["schema_requirement"] = arguments.schema_requirement
    if arguments.notes_url:
        entry["notes_url"] = arguments.notes_url

    releases[version] = entry
    channels[arguments.channel] = version

    # `recommended` is what the interface tells somebody to install on a fresh
    # host, so only a stable release may move it. A beta that became the
    # recommendation would hand a pre-release to every new installation,
    # including the ones that never opted into one.
    if arguments.channel == "stable":
        recommended = version
    elif recommended is None or recommended not in releases:
        print(
            f"--channel {arguments.channel} cannot set `recommended`, and the "
            f"manifest being merged does not name a stable release to keep. "
            f"Publish a stable release first, or pass --channel stable.",
            file=sys.stderr,
        )
        return 1

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": dt.datetime.now(tz=dt.UTC).replace(microsecond=0).isoformat(),
        "recommended": recommended,
        "minimum_supported": minimum,
        "channels": channels,
        "releases": releases,
    }

    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"wrote {arguments.output}")
    print(f"  added              {version} to {arguments.channel}")
    print(f"  recommended        {recommended}")
    print(f"  minimum supported  {minimum}")
    print(f"  protocol versions  {', '.join(str(value) for value in protocol_versions)}")
    for platform, artifact in sorted(platforms.items()):
        print(f"  {platform:<14} {artifact['sha256'][:16]}…  {artifact['url']}")
    print()
    print("Not signed. The controller refuses an automatic update without a")
    print("valid publisher signature, so the next step is:")
    print(f"  scripts/sign-release.py sign --manifest {arguments.output} --private-key …")
    return 0


def _parse_protocol_versions(raw: str) -> tuple[int, ...]:
    """Parse ``'1,2'`` into ``(1, 2)``, refusing anything else.

    Sorted and deduplicated, because the manifest is compared against the
    controller's own supported tuple and a duplicate would make two identical
    manifests differ.
    """
    values = []
    for part in raw.split(","):
        stripped = part.strip()
        if not stripped:
            continue
        if not stripped.isdigit():
            raise ValueError(f"{stripped!r} is not a protocol version")
        values.append(int(stripped))
    if not values:
        raise ValueError("names no versions. An empty list blocks every automatic update.")
    return tuple(sorted(set(values)))


def _upconvert_releases(releases: dict[str, Any]) -> dict[str, Any]:
    """Read v1's platform maps as v2's release entries.

    Distinguished by shape rather than by the declared ``schema_version``, for
    the same reason the controller does it that way: a file that declares one
    and is shaped like the other was edited by hand, and guessing from the
    declaration produces an error a long way from the cause.

    The converted entry declares **nothing** — no channel constraints beyond
    the default, no protocol versions — which is exactly what a v1 release
    claimed. That means an old release will not be chosen for an automatic
    update, which is the correct and conservative reading: nobody recorded
    what protocol it speaks.
    """
    converted: dict[str, Any] = {}
    for version, entry in releases.items():
        if isinstance(entry, dict) and "platforms" in entry:
            converted[version] = entry
        else:
            converted[version] = {"channel": "stable", "platforms": entry}
    return converted


if __name__ == "__main__":
    raise SystemExit(main())
