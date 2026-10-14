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

    def test_a_recommended_version_with_no_release_entry_is_refused(self) -> None:
        # **Changed in the Evergreen milestone.** This used to be tolerated at
        # parse time, on the reasoning that the endpoint had somewhere useful
        # to report it. It does — but it reports a `ManifestError` as
        # "unavailable" with the error's own text, so refusing here loses no
        # reporting location and gains a far more precise message: "recommended
        # is '9.9.9', which has no release entry" rather than "the recommended
        # version lists no platform this build knows about".
        #
        # It also matters more than it did. A dangling reference used to cost
        # one unavailable install command; now the update planner reads the
        # same manifest, and a `recommended` pointing at nothing would become
        # "an update is available" followed by a download that 404s. Refusing
        # once here is cheaper than handling it at every consumer.
        with pytest.raises(ManifestError, match="no release entry"):
            parse_release_manifest(encode(recommended="9.9.9"))


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

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("url", "https://example.test/agent.tar.gz;id"),
            ("url", "https://example.test/`id`.tar.gz"),
            ("url", "https://example.test/$(id).tar.gz"),
            ("url", "https://example.test/a.tar.gz&&id"),
            ("url", "https://example.test/a.tar.gz|id"),
            ("url", "https://example.test/a\n.tar.gz"),
            ("url", "https://example.test/a .tar.gz"),
            ("archive", "agent'.tar.gz"),
            ("archive", 'agent".tar.gz'),
            ("archive", "agent b.tar.gz"),
            ("archive", "agent$(id).tar.gz"),
        ],
    )
    def test_a_value_that_would_be_unsafe_in_a_shell_is_refused(
        self, field: str, value: str
    ) -> None:
        # Both fields are interpolated into the install command the interface
        # renders, which somebody pastes into a root shell:
        #
        #     curl -fsSLO <url>
        #     echo '<digest>  <archive>' | sha256sum -c -
        #     tar xzf <archive>
        #
        # A ';' or a backtick is command injection into that shell, and a
        # single quote closes the quoting around the digest. Refused rather
        # than escaped: the interface would have to know which shell, and a
        # release artifact whose name needs quoting should be renamed.
        document = manifest_document()
        document["releases"]["0.2.0"]["linux/amd64"][field] = value

        with pytest.raises(ManifestError, match="not usable"):
            parse_release_manifest(json.dumps(document).encode("utf-8"))

    def test_an_ordinary_release_url_is_still_accepted(self) -> None:
        # The guard above must not refuse the real thing. A GitHub release
        # URL has a version with dots, a path with slashes, and nothing else.
        document = manifest_document()
        document["releases"]["0.2.0"]["linux/amd64"]["url"] = (
            "https://github.com/felixstaude/homelab-control-center/releases/"
            "download/v0.2.0/hcc-agent_0.2.0_linux_amd64.tar.gz"
        )

        manifest = parse_release_manifest(json.dumps(document).encode("utf-8"))

        release = manifest.recommended_release()
        assert release is not None
        assert release["linux/amd64"].url.endswith("hcc-agent_0.2.0_linux_amd64.tar.gz")

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

    @staticmethod
    def _artifacts(version: str = "0.4.0") -> dict[str, Any]:
        """A synthetic ``artifacts.json`` in the shape the build script writes."""
        return {
            "schema_version": 1,
            "product": "hcc-agent",
            "version": version,
            "commit": "0" * 40,
            "built_at": "2026-10-14T00:00:00Z",
            "source_date_epoch": 1760000000,
            "go_version": "go1.24.0",
            "reproducible": True,
            "artifacts": [
                {
                    "name": f"hcc-agent_{version}_linux_{architecture}.tar.gz",
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

    @staticmethod
    def _generate(tmp_path: Path, artifacts: dict[str, Any], *extra: str) -> Any:
        """Run the real build script on *artifacts*, returning the process."""
        import subprocess
        import sys

        artifacts_path = tmp_path / f"artifacts-{artifacts['version']}.json"
        artifacts_path.write_text(json.dumps(artifacts), encoding="utf-8")
        # Four directories up from this file: tests/ → server/ → apps/ → root.
        repository = Path(__file__).resolve().parents[3]
        return subprocess.run(
            [
                sys.executable,
                str(repository / "scripts" / "build-release-manifest.py"),
                "--artifacts",
                str(artifacts_path),
                "--base-url",
                f"https://example.test/releases/download/v{artifacts['version']}",
                "--output",
                str(tmp_path / "agent-releases.json"),
                "--protocol-versions",
                "1,2",
                *extra,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_a_generated_manifest_parses(self, tmp_path: Path) -> None:
        result = self._generate(tmp_path, self._artifacts())
        assert result.returncode == 0, result.stderr

        manifest = read_release_manifest(tmp_path / "agent-releases.json")

        assert manifest.schema_version == MANIFEST_SCHEMA_VERSION
        assert manifest.recommended == "0.4.0"
        assert manifest.minimum_supported == "0.4.0"
        # The channel table is what update planning reads, and a generated
        # manifest with an empty one would offer nothing to anybody.
        assert manifest.channels == {"stable": "0.4.0"}
        assert manifest.releases["0.4.0"].protocol_versions == (1, 2)
        release = manifest.recommended_release()
        assert release is not None
        assert set(release) == set(SUPPORTED_PLATFORMS)
        assert release["linux/arm"].tier == "best-effort"
        assert release["linux/amd64"].url.endswith("hcc-agent_0.4.0_linux_amd64.tar.gz")

    def test_a_beta_release_does_not_become_the_recommendation(self, tmp_path: Path) -> None:
        """A pre-release must not be what a fresh host is told to install.

        `recommended` is the install command the interface renders. A beta that
        moved it would hand a pre-release to every new installation, including
        every one that never opted into one.
        """
        assert self._generate(tmp_path, self._artifacts("0.4.0")).returncode == 0
        result = self._generate(
            tmp_path,
            self._artifacts("0.5.0-beta.1"),
            "--channel",
            "beta",
            "--merge-into",
            str(tmp_path / "agent-releases.json"),
        )
        assert result.returncode == 0, result.stderr

        manifest = read_release_manifest(tmp_path / "agent-releases.json")

        assert manifest.recommended == "0.4.0"
        assert manifest.channels == {"stable": "0.4.0", "beta": "0.5.0-beta.1"}
        assert set(manifest.releases) == {"0.4.0", "0.5.0-beta.1"}

    def test_a_first_release_in_a_prerelease_channel_is_refused(self, tmp_path: Path) -> None:
        """Because there would be nothing for `recommended` to name.

        The reader refuses a `recommended` with no release entry, so the
        alternative to this refusal is a manifest that cannot be loaded —
        caught at the first request instead of at build time.
        """
        result = self._generate(tmp_path, self._artifacts("0.5.0-beta.1"), "--channel", "beta")

        assert result.returncode != 0
        assert "Publish a stable release first" in result.stderr

    def test_merging_into_a_v1_manifest_upconverts_it(self, tmp_path: Path) -> None:
        """A manifest published by an older release is a legitimate input.

        Its releases become stable entries declaring nothing, which is what
        they claimed. They therefore will not be chosen for an automatic
        update — nobody recorded what protocol they speak — and that is the
        conservative reading rather than a loss.
        """
        output = tmp_path / "agent-releases.json"
        output.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "recommended": "0.3.0",
                    "minimum_supported": "0.2.0",
                    "releases": {
                        "0.3.0": {
                            "linux/amd64": {
                                "archive": "hcc-agent_0.3.0_linux_amd64.tar.gz",
                                "url": "https://example.test/hcc-agent_0.3.0_linux_amd64.tar.gz",
                                "sha256": DIGEST,
                                "size_bytes": 10,
                                "tier": "supported",
                            }
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        result = self._generate(tmp_path, self._artifacts("0.4.0"), "--merge-into", str(output))
        assert result.returncode == 0, result.stderr

        manifest = read_release_manifest(output)

        assert manifest.schema_version == MANIFEST_SCHEMA_VERSION
        assert manifest.recommended == "0.4.0"
        # Not raised to 0.4.0: a new release does not by itself stop the
        # controller working with the agents already out there.
        assert manifest.minimum_supported == "0.2.0"
        assert manifest.releases["0.3.0"].channel == "stable"
        assert manifest.releases["0.3.0"].protocol_versions == ()
        assert manifest.releases["0.4.0"].protocol_versions == (1, 2)

    def test_the_compatibility_constraints_survive_the_round_trip(self, tmp_path: Path) -> None:
        result = self._generate(
            tmp_path,
            self._artifacts(),
            "--min-controller",
            "0.1.0",
            "--schema-requirement",
            "0010_update_management",
            "--notes-url",
            "https://example.test/notes",
        )
        assert result.returncode == 0, result.stderr

        entry = read_release_manifest(tmp_path / "agent-releases.json").releases["0.4.0"]

        assert entry.min_controller == "0.1.0"
        assert entry.schema_requirement == "0010_update_management"
        assert entry.notes_url == "https://example.test/notes"
        assert entry.released_at is not None

    def test_an_empty_protocol_version_list_is_refused(self, tmp_path: Path) -> None:
        """Empty means "unknown", which blocks every automatic update.

        A release built that way installs fine by hand and is then silently
        never picked up by a host with an automatic policy: everything works,
        nothing happens. Refused where it is cheap to notice.
        """
        import subprocess
        import sys

        artifacts_path = tmp_path / "artifacts.json"
        artifacts_path.write_text(json.dumps(self._artifacts()), encoding="utf-8")
        repository = Path(__file__).resolve().parents[3]
        result = subprocess.run(
            [
                sys.executable,
                str(repository / "scripts" / "build-release-manifest.py"),
                "--artifacts",
                str(artifacts_path),
                "--base-url",
                "https://example.test/releases",
                "--output",
                str(tmp_path / "out.json"),
                "--protocol-versions",
                " , ",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode != 0
        assert "blocks every automatic update" in result.stderr

    def test_the_generated_manifest_is_unsigned(self, tmp_path: Path) -> None:
        """Signing is a separate tool with a key this script never sees.

        A build machine that could sign would make the signature worth no more
        than the build machine. The script says so on its way out, because an
        unsigned manifest is a manifest no automatic update will use.
        """
        result = self._generate(tmp_path, self._artifacts())

        assert not (tmp_path / "agent-releases.json.sig").exists()
        assert "sign-release.py" in result.stdout

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
                "--protocol-versions",
                "2",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        # The same rule the reader enforces, applied at build time so the
        # failure is not deferred to the first request.
        assert result.returncode != 0
        assert "must be https" in result.stderr
