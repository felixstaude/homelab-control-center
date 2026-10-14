"""Manifest schema v2, and the v1 compatibility that makes it deployable.

The upconversion is the part worth testing hardest. A controller upgraded
ahead of its manifest — which is every controller, for the window between
building the image and regenerating the manifest — reads a v1 file. If that
stopped working, the upgrade that introduced v2 would break the install command
on every existing deployment.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from hcc.agents.releases import (
    CHANNELS,
    DEFAULT_CHANNEL,
    MANIFEST_SCHEMA_VERSION,
    SUPPORTED_MANIFEST_SCHEMA_VERSIONS,
    ManifestError,
    parse_release_manifest,
)

DIGEST = "a" * 64

PLATFORM: dict[str, Any] = {
    "archive": "hcc-agent_0.1.0_linux_amd64.tar.gz",
    "url": "https://example.com/hcc-agent_0.1.0_linux_amd64.tar.gz",
    "sha256": DIGEST,
    "size_bytes": 3_000_000,
    "tier": "supported",
}


def v1_document(**overrides: Any) -> dict[str, Any]:
    """A schema-v1 manifest, the shape an older release shipped."""
    document: dict[str, Any] = {
        "schema_version": 1,
        "recommended": "0.1.0",
        "minimum_supported": "0.1.0",
        "releases": {"0.1.0": {"linux/amd64": dict(PLATFORM)}},
    }
    document.update(overrides)
    return document


def v2_document(**overrides: Any) -> dict[str, Any]:
    """A schema-v2 manifest."""
    document: dict[str, Any] = {
        "schema_version": 2,
        "generated_at": "2026-10-14T12:00:00+00:00",
        "recommended": "0.1.0",
        "minimum_supported": "0.1.0",
        "channels": {"stable": "0.1.0"},
        "releases": {
            "0.1.0": {
                "channel": "stable",
                "released_at": "2026-10-14T12:00:00+00:00",
                "notes_url": "https://example.com/releases/0.1.0",
                "protocol_versions": [1, 2],
                "min_controller": "0.1.0",
                "schema_requirement": "0009",
                "platforms": {"linux/amd64": dict(PLATFORM)},
            }
        },
    }
    document.update(overrides)
    return document


def parse(document: dict[str, Any]) -> Any:
    return parse_release_manifest(json.dumps(document).encode("utf-8"))


class TestAV1ManifestStillLoads:
    def test_it_parses(self) -> None:
        manifest = parse(v1_document())

        assert manifest.schema_version == 1
        assert manifest.recommended == "0.1.0"

    def test_its_releases_become_stable_entries(self) -> None:
        # The only honest conversion: a v1 release declared no channel, and
        # the conservative reading of that is an ordinary release.
        manifest = parse(v1_document())

        assert manifest.releases["0.1.0"].channel == DEFAULT_CHANNEL
        assert manifest.channel_of("0.1.0") == "stable"

    def test_its_releases_declare_no_compatibility_constraints(self) -> None:
        # Absent means "no constraint", which is what a v1 release claimed.
        # Inventing constraints it never stated would withhold releases that
        # were perfectly installable before the upgrade.
        entry = parse(v1_document()).releases["0.1.0"]

        assert entry.protocol_versions == ()
        assert entry.min_controller is None
        assert entry.schema_requirement is None

    def test_the_platform_map_survives_intact(self) -> None:
        entry = parse(v1_document()).releases["0.1.0"]

        assert entry.platforms["linux/amd64"].sha256 == DIGEST
        assert entry.platforms["linux/amd64"].tier == "supported"

    def test_recommended_release_returns_the_platform_map(self) -> None:
        # The install-command endpoint's existing caller. The v2 shape must not
        # have changed what it gets back.
        release = parse(v1_document()).recommended_release()

        assert release is not None
        assert release["linux/amd64"].archive == PLATFORM["archive"]

    def test_a_channel_table_is_synthesised(self) -> None:
        # So every consumer can read `channels` unconditionally rather than
        # branching on the schema version it happened to be given.
        manifest = parse(v1_document())

        assert manifest.channels == {"stable": "0.1.0"}


class TestV2:
    def test_it_parses_with_everything_declared(self) -> None:
        manifest = parse(v2_document())
        entry = manifest.releases["0.1.0"]

        assert manifest.schema_version == 2
        assert manifest.generated_at == "2026-10-14T12:00:00+00:00"
        assert entry.protocol_versions == (1, 2)
        assert entry.min_controller == "0.1.0"
        assert entry.schema_requirement == "0009"
        assert entry.notes_url == "https://example.com/releases/0.1.0"

    def test_channels_may_name_different_versions(self) -> None:
        document = v2_document(
            channels={"stable": "0.1.0", "beta": "0.2.0-beta.1"},
            releases={
                "0.1.0": {"channel": "stable", "platforms": {"linux/amd64": dict(PLATFORM)}},
                "0.2.0-beta.1": {
                    "channel": "beta",
                    "platforms": {"linux/amd64": dict(PLATFORM)},
                },
            },
        )

        manifest = parse(document)

        assert manifest.channels["beta"] == "0.2.0-beta.1"
        assert manifest.channel_of("0.2.0-beta.1") == "beta"

    def test_a_release_defaults_to_stable_when_it_does_not_say(self) -> None:
        document = v2_document(releases={"0.1.0": {"platforms": {"linux/amd64": dict(PLATFORM)}}})

        assert parse(document).releases["0.1.0"].channel == "stable"

    def test_the_schema_version_this_build_writes_is_v2(self) -> None:
        assert MANIFEST_SCHEMA_VERSION == 2
        # And it can read both, which is the asymmetry that makes a schema
        # change deployable rather than a flag day.
        assert SUPPORTED_MANIFEST_SCHEMA_VERSIONS == (1, 2)


class TestRefusalsThatProtectTheUpdatePlanner:
    def test_a_channel_naming_a_missing_release_is_refused(self) -> None:
        # The failure mode this prevents: "an update is available", followed by
        # a download that 404s.
        document = v2_document(channels={"stable": "0.1.0", "beta": "0.9.9"})

        with pytest.raises(ManifestError, match="no release entry"):
            parse(document)

    def test_an_unknown_channel_is_refused(self) -> None:
        with pytest.raises(ManifestError, match="not one of"):
            parse(v2_document(channels={"nightly": "0.1.0"}))

    def test_an_unknown_channel_on_a_release_is_refused(self) -> None:
        document = v2_document(
            releases={
                "0.1.0": {"channel": "nightly", "platforms": {"linux/amd64": dict(PLATFORM)}}
            },
            channels={},
        )

        with pytest.raises(ManifestError, match="channel must be one of"):
            parse(document)

    def test_a_release_key_that_is_not_a_version_is_refused(self) -> None:
        # Every release key is compared against others by the planner. One that
        # cannot be parsed would either crash it or be silently skipped, and
        # silently skipping a release is how a fleet stops being offered
        # updates with nothing in the logs.
        document = v2_document(
            releases={"latest": {"platforms": {"linux/amd64": dict(PLATFORM)}}},
            recommended="latest",
            channels={},
        )

        with pytest.raises(ManifestError, match="not a semantic version"):
            parse(document)

    def test_a_min_controller_that_is_not_a_version_is_refused(self) -> None:
        # A constraint nobody can evaluate is worse than no constraint: it
        # would be silently ignored, and the release would install on a
        # controller it was meant to be withheld from.
        document = v2_document(
            releases={
                "0.1.0": {
                    "min_controller": "v1.0",
                    "platforms": {"linux/amd64": dict(PLATFORM)},
                }
            }
        )

        with pytest.raises(ManifestError, match="min_controller is not a semantic version"):
            parse(document)

    def test_a_non_version_recommended_is_refused(self) -> None:
        document = v2_document(
            recommended="latest",
            releases={"latest": {"platforms": {"linux/amd64": dict(PLATFORM)}}},
            channels={},
        )

        with pytest.raises(ManifestError, match="not a semantic version"):
            parse(document)

    def test_a_release_with_no_platforms_is_refused(self) -> None:
        document = v2_document(releases={"0.1.0": {"channel": "stable", "platforms": {}}})

        with pytest.raises(ManifestError, match="lists no platforms"):
            parse(document)

    def test_a_schema_version_beyond_what_is_understood_is_refused(self) -> None:
        with pytest.raises(ManifestError, match="this controller understands 1, 2"):
            parse(v2_document(schema_version=3))

    def test_a_v1_declaration_with_a_v2_shape_is_read_by_shape(self) -> None:
        # Somebody edited the file by hand. Reading by shape rather than by the
        # declaration means the error — if there is one — is about the content,
        # not a type confusion a long way from the cause.
        document = v1_document(
            schema_version=1,
            releases={"0.1.0": {"channel": "beta", "platforms": {"linux/amd64": dict(PLATFORM)}}},
            channels={"beta": "0.1.0"},
        )

        manifest = parse(document)

        assert manifest.releases["0.1.0"].channel == "beta"


class TestChannelOrdering:
    def test_channels_are_ordered_most_to_least_stable(self) -> None:
        # Load-bearing rather than cosmetic: a host on `stable` must never be
        # offered something from `beta`, and the comparison that enforces it
        # reads this tuple's order.
        assert CHANNELS == ("stable", "beta", "alpha")

    def test_the_default_channel_is_the_most_stable_one(self) -> None:
        # A release that forgot to declare a channel should be harder to
        # install automatically, not easier.
        assert CHANNELS[0] == DEFAULT_CHANNEL
