"""The agent release manifest: parsing, and what it refuses.

Unit tests, no database. The endpoint that serves this is covered by
``test_api_agent_releases_integration.py``.

The refusals are the point of this suite. This manifest is the controller's
claim about which bytes an administrator should trust, rendered into a command
they will paste on a machine they own — so a manifest that is wrong in a way
nothing notices is worse than no manifest at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hcc.agents.releases import (
    MANIFEST_SCHEMA_VERSION,
    MAX_MANIFEST_BYTES,
    SUPPORTED_PLATFORMS,
    ManifestError,
    load_release_manifest,
    parse_release_manifest,
    read_release_manifest,
)

DIGEST = "a" * 64


def manifest_document(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "recommended": "0.2.0",
        "minimum_supported": "0.1.0",
        "releases": {
            "0.2.0": {
                "linux/amd64": {
                    "archive": "hcc-agent_0.2.0_linux_amd64.tar.gz",
                    "url": "https://example.test/hcc-agent_0.2.0_linux_amd64.tar.gz",
                    "sha256": DIGEST,
                    "size_bytes": 2931200,
                    "tier": "supported",
                },
            }
        },
    }
    document.update(overrides)
    return document


def encode(**overrides: Any) -> bytes:
    return json.dumps(manifest_document(**overrides)).encode("utf-8")


class TestParsing:
    def test_a_well_formed_manifest_parses(self) -> None:
        manifest = parse_release_manifest(encode())

        assert manifest.recommended == "0.2.0"
        assert manifest.minimum_supported == "0.1.0"
        release = manifest.recommended_release()
        assert release is not None
        assert release["linux/amd64"].sha256 == DIGEST

    def test_a_digest_is_normalised_to_lowercase(self) -> None:
        document = manifest_document()
        document["releases"]["0.2.0"]["linux/amd64"]["sha256"] = DIGEST.upper()

        manifest = parse_release_manifest(json.dumps(document).encode("utf-8"))

        release = manifest.recommended_release()
        assert release is not None
        assert release["linux/amd64"].sha256 == DIGEST

    def test_recommended_release_is_none_when_the_version_is_absent(self) -> None:
        manifest = parse_release_manifest(encode(recommended="9.9.9"))

        # Not an error at parse time: a manifest may legitimately carry old
        # versions, and "recommended names something not listed" is caught by
        # the endpoint, which has somewhere useful to report it.
        assert manifest.recommended_release() is None


class TestRefusals:
    def test_a_future_schema_version_is_refused(self) -> None:
        with pytest.raises(ManifestError, match="this controller understands"):
            parse_release_manifest(encode(schema_version=MANIFEST_SCHEMA_VERSION + 1))

    def test_malformed_json_is_refused(self) -> None:
        with pytest.raises(ManifestError, match="not valid JSON"):
            parse_release_manifest(b"{not json")

    def test_a_json_array_is_refused(self) -> None:
        with pytest.raises(ManifestError, match="not a JSON object"):
            parse_release_manifest(b"[]")

    def test_an_oversized_manifest_is_refused_before_parsing(self) -> None:
        with pytest.raises(ManifestError, match="over the"):
            parse_release_manifest(b"x" * (MAX_MANIFEST_BYTES + 1))

    def test_an_unknown_field_is_refused(self) -> None:
        # `extra="forbid"`: a field this build does not understand may be
        # load-bearing in the build that wrote it, and ignoring it would mean
        # rendering a command that omits something deliberate.
        with pytest.raises(ManifestError, match="not usable"):
            parse_release_manifest(encode(published_by="somebody"))

    def test_a_short_digest_is_refused(self) -> None:
        document = manifest_document()
        document["releases"]["0.2.0"]["linux/amd64"]["sha256"] = "abc"

        with pytest.raises(ManifestError, match="not usable"):
            parse_release_manifest(json.dumps(document).encode("utf-8"))

    def test_a_non_hexadecimal_digest_is_refused(self) -> None:
        document = manifest_document()
        document["releases"]["0.2.0"]["linux/amd64"]["sha256"] = "z" * 64

        with pytest.raises(ManifestError, match="hexadecimal"):
            parse_release_manifest(json.dumps(document).encode("utf-8"))

    @pytest.mark.parametrize(
        "url",
        [
            "http://example.test/agent.tar.gz",
            "ftp://example.test/agent.tar.gz",
            "file:///etc/passwd",
            "/etc/passwd",
        ],
    )
    def test_a_url_that_is_not_https_is_refused(self, url: str) -> None:
        # The digest pins the bytes whatever happened in transit, so a
        # plaintext download is not fatal — but a command the product
        # *generated* with http:// in it teaches the wrong lesson, and an
        # operator will reasonably assume it is fine.
        document = manifest_document()
        document["releases"]["0.2.0"]["linux/amd64"]["url"] = url

        with pytest.raises(ManifestError, match="not usable"):
            parse_release_manifest(json.dumps(document).encode("utf-8"))

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:8080/agent.tar.gz",
            "http://localhost/agent.tar.gz",
            "http://[::1]:9000/agent.tar.gz",
        ],
    )
    def test_plain_http_to_loopback_is_allowed_for_a_local_mirror(self, url: str) -> None:
        document = manifest_document()
        document["releases"]["0.2.0"]["linux/amd64"]["url"] = url

        manifest = parse_release_manifest(json.dumps(document).encode("utf-8"))

        release = manifest.recommended_release()
        assert release is not None
        assert release["linux/amd64"].url == url

    def test_an_unknown_platform_is_refused(self) -> None:
        # Refused rather than ignored: a manifest naming darwin/arm64 was
        # built by something that believes this product ships a macOS agent,
        # and quietly dropping the entry would hide that disagreement.
        document = manifest_document()
        document["releases"]["0.2.0"]["darwin/arm64"] = document["releases"]["0.2.0"]["linux/amd64"]

        with pytest.raises(ManifestError, match="not usable"):
            parse_release_manifest(json.dumps(document).encode("utf-8"))

    def test_a_tier_outside_the_vocabulary_is_refused(self) -> None:
        document = manifest_document()
        document["releases"]["0.2.0"]["linux/amd64"]["tier"] = "rock-solid"

        with pytest.raises(ManifestError, match="not usable"):
            parse_release_manifest(json.dumps(document).encode("utf-8"))

    def test_a_release_with_no_platforms_is_refused(self) -> None:
        with pytest.raises(ManifestError, match="not usable"):
            parse_release_manifest(encode(releases={"0.2.0": {}}))

    def test_a_zero_size_is_refused(self) -> None:
        document = manifest_document()
        document["releases"]["0.2.0"]["linux/amd64"]["size_bytes"] = 0

        with pytest.raises(ManifestError, match="not usable"):
            parse_release_manifest(json.dumps(document).encode("utf-8"))


class TestLoading:
    def test_no_configured_path_is_absence_rather_than_an_error(self) -> None:
        # The normal state: no release of this project has been published, so
        # there is nothing to point at. Absence has to be distinguishable from
        # breakage, because one is a feature that is not there yet and the
        # other is a deployment fault.
        assert load_release_manifest(None) is None

    def test_a_configured_path_that_does_not_exist_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(ManifestError, match="cannot read"):
            load_release_manifest(tmp_path / "absent.json")

    def test_a_configured_path_that_is_a_directory_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(ManifestError, match="cannot read"):
            load_release_manifest(tmp_path)

    def test_a_manifest_on_disk_round_trips(self, tmp_path: Path) -> None:
        path = tmp_path / "agent-releases.json"
        path.write_bytes(encode())

        manifest = load_release_manifest(path)

        assert manifest is not None
        assert manifest.recommended == "0.2.0"

    def test_replacing_the_file_is_picked_up(self, tmp_path: Path) -> None:
        # An image upgrade replaces this file. The cache is keyed on the
        # file's identity rather than on its path, so a stale document cannot
        # be served for the lifetime of the process.
        path = tmp_path / "agent-releases.json"
        path.write_bytes(encode())
        first = load_release_manifest(path)
        assert first is not None and first.recommended == "0.2.0"

        path.write_bytes(
            encode(
                recommended="0.3.0", releases={"0.3.0": manifest_document()["releases"]["0.2.0"]}
            )
        )

        second = load_release_manifest(path)
        assert second is not None
        assert second.recommended == "0.3.0"

    def test_read_release_manifest_reports_an_unreadable_file(self, tmp_path: Path) -> None:
        with pytest.raises(ManifestError, match="cannot read"):
            read_release_manifest(tmp_path / "nope.json")


class TestTheGeneratorAgreesWithTheReader:
    """The manifest the build script writes has to be one the server accepts.

    These are two programs in two languages reading one file, which is the
    shape of drift this project already has three mechanisms for. Here the
    coupling is cheap to test: generate a manifest from a synthetic
    ``artifacts.json`` with the real script and parse it with the real reader.
    """

    def test_a_generated_manifest_parses(self, tmp_path: Path) -> None:
        import subprocess
        import sys

        artifacts = {
            "schema_version": 1,
            "product": "hcc-agent",
            "version": "0.4.0",
            "commit": "0" * 40,
            "built_at": "2026-10-14T00:00:00Z",
            "source_date_epoch": 1760000000,
            "go_version": "go1.24.0",
            "reproducible": True,
            "artifacts": [
                {
                    "name": f"hcc-agent_0.4.0_linux_{architecture}.tar.gz",
                    "kind": "archive",
                    "os": "linux",
                    "arch": architecture,
                    "tier": tier,
                    "sha256": DIGEST,
                    "size_bytes": 1234,
                    "binary": {"path": "hcc-agent", "sha256": DIGEST, "size_bytes": 4321},
                }
                for architecture, tier in (
                    ("amd64", "supported"),
                    ("arm64", "supported"),
                    ("arm", "best-effort"),
                )
            ],
            "checksums": {"name": "checksums.txt", "sha256": DIGEST},
            "sbom": {"name": "x.spdx.json", "format": "SPDX-2.3", "sha256": DIGEST},
        }
        artifacts_path = tmp_path / "artifacts.json"
        artifacts_path.write_text(json.dumps(artifacts), encoding="utf-8")
        output = tmp_path / "agent-releases.json"

        # Four directories up from this file: tests/ → server/ → apps/ → root.
        repository = Path(__file__).resolve().parents[3]
        result = subprocess.run(
            [
                sys.executable,
                str(repository / "scripts" / "build-release-manifest.py"),
                "--artifacts",
                str(artifacts_path),
                "--base-url",
                "https://example.test/releases/download/v0.4.0",
                "--output",
                str(output),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

        manifest = read_release_manifest(output)

        assert manifest.recommended == "0.4.0"
        assert manifest.minimum_supported == "0.4.0"
        release = manifest.recommended_release()
        assert release is not None
        assert set(release) == set(SUPPORTED_PLATFORMS)
        assert release["linux/arm"].tier == "best-effort"
        assert release["linux/amd64"].url.endswith("hcc-agent_0.4.0_linux_amd64.tar.gz")

    def test_the_generator_refuses_a_plaintext_base_url(self, tmp_path: Path) -> None:
        import subprocess
        import sys

        artifacts_path = tmp_path / "artifacts.json"
        artifacts_path.write_text(
            json.dumps(
                {"schema_version": 1, "product": "hcc-agent", "version": "0.4.0", "artifacts": []}
            ),
            encoding="utf-8",
        )
        repository = Path(__file__).resolve().parents[3]

        result = subprocess.run(
            [
                sys.executable,
                str(repository / "scripts" / "build-release-manifest.py"),
                "--artifacts",
                str(artifacts_path),
                "--base-url",
                "http://mirror.example.test/releases",
                "--output",
                str(tmp_path / "out.json"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        # The same rule the reader enforces, applied at build time so the
        # failure is not deferred to the first request.
        assert result.returncode != 0
        assert "must be https" in result.stderr
