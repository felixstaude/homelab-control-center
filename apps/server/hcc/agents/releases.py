"""The agent releases this controller knows about.

## Why the controller holds this at all

The install command an administrator copies out of the interface carries the
expected SHA-256 of the archive it tells them to download:

```bash
curl -fsSLO https://.../hcc-agent_0.2.0_linux_amd64.tar.gz
echo '9f2c…  hcc-agent_0.2.0_linux_amd64.tar.gz' | sha256sum -c -
tar xzf hcc-agent_0.2.0_linux_amd64.tar.gz
sudo ./hcc-agent install --controller https://hcc.example.com
```

That digest is the whole point. The administrator is already authenticated to
this controller over TLS — it is the thing holding their fleet's credentials —
so **the controller, not the download origin, is what vouches for the binary**.
An attacker who controls the mirror, or the network between the host and the
mirror, cannot make a substituted tarball match a digest that arrived inside
the administrator's own session.

Three consequences, all of them the reason this module exists rather than a
runtime fetch of ``checksums.txt``:

1. **No runtime dependency on GitHub for trust.** The controller does not go
   and look; it already has the digests, baked in when its image was built.
2. **The digest travels in the session the administrator is already in.**
3. **Air-gapped and mirrored installs work.** Point ``base_url`` at an
   internal mirror and the digest still pins the bytes.

## What happens when there is no manifest

Nothing is faked. No release of this project has been published, so a source
checkout has no manifest and :func:`load_release_manifest` returns ``None``.
The endpoint then reports ``available: false`` with a reason, and the
interface shows the build-from-source path instead of a download command
pointing at a URL that would 404.

That is deliberate and is the rule the whole product is held to: a feature
that is not there says so rather than producing something that looks like it
works. A manifest with invented URLs would be worse than no manifest, because
the digest beside an invented URL is a claim nobody can check.

## Where the file comes from

``scripts/build-release-manifest.py`` derives it from the ``artifacts.json``
that ``scripts/build-agent-artifacts.sh`` produces, so the digests are the
build's own rather than transcribed. The controller image bakes the result in
and points :attr:`~hcc.config.Settings.agent_release_manifest` at it.

## Schema v2: channels and compatibility

v1 answered one question — "what should somebody install on a fresh host" —
and it answered it well enough that the shape is kept. Update management asks
more: which channel is a release in, which controller versions can drive it,
which protocol versions does it speak, which database schema does it need.

So v2 adds a per-release entry around the platform map, plus a channel table
naming the current release in each channel. Both versions are accepted. A v1
manifest is a legitimate thing for an older release to have shipped, and
refusing it would turn every existing example into a startup failure — so v1 is
**upconverted on read**: its releases become ``stable`` entries with no
compatibility constraints, which is exactly what they claimed.

Authenticity is *not* a field here. The signature is detached — a separate
``.sig`` file over the manifest's exact bytes — because two JSON libraries do
not agree on key order, integer rendering or non-ASCII escaping, and a
canonicalisation step would turn a formatting difference into something
indistinguishable from tampering. See :mod:`hcc.updates.signing`.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel, Field, field_validator, model_validator

from hcc.logging import get_logger
from hcc.updates.version import InvalidVersionError
from hcc.updates.version import parse as parse_version

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)

#: The manifest shape this build *writes*.
#:
#: A consumer that cannot tell which shape it is reading breaks on the second
#: one, so the version is required and an unknown one is a refusal rather than
#: a best-effort parse.
MANIFEST_SCHEMA_VERSION: Final = 2

#: Every shape this build can *read*.
#:
#: Reading more than is written is the asymmetry that makes a schema change
#: deployable: a controller upgraded ahead of its manifest still works.
SUPPORTED_MANIFEST_SCHEMA_VERSIONS: Final = (1, 2)

#: Release channels, ordered from most to least stable.
#:
#: Ordered rather than a set because the order is load-bearing: a host on
#: ``stable`` must never be offered something from ``beta``, and the comparison
#: that enforces it needs to know which way round they go.
CHANNELS: Final = ("stable", "beta", "alpha")

#: The channel a release claims when it does not say — and the channel a v1
#: manifest's releases are upconverted into.
#:
#: ``stable`` rather than ``alpha``: a release that forgot to declare a channel
#: should be *harder* to install automatically, not easier, and defaulting to a
#: pre-release channel would hide it from the hosts most likely to want it
#: while exposing the ones that opted into pre-releases. The conservative
#: reading of a missing field is that it is an ordinary release.
DEFAULT_CHANNEL: Final = "stable"

#: Largest manifest file that will be read. A manifest for three
#: architectures and a handful of versions is a couple of kilobytes; the bound
#: is what stops a misconfigured path from being the process's memory.
MAX_MANIFEST_BYTES: Final = 256 * 1024

#: Characters that must not appear in a manifest's URL or archive name.
#:
#: Both values are interpolated into the install command the interface renders
#: — a command a person pastes into a root shell:
#:
#:     curl -fsSLO <url>
#:     echo '<digest>  <archive>' | sha256sum -c -
#:     tar xzf <archive>
#:
#: So a `;`, a backtick, a `$(`, or a `'` that closes the quoting around the
#: digest line is command injection into that shell. The manifest is shipped
#: inside the controller's own image, so an attacker needs to have influenced
#: the build — but "the value is trusted" is exactly the assumption that makes
#: a supply-chain compromise worth more than it should be, and no legitimate
#: release URL or filename contains any of these.
#:
#: Refused rather than escaped: the interface would have to know which shell,
#: and a release artifact whose name needs quoting is a release artifact that
#: should be renamed.
SHELL_UNSAFE_CHARACTERS: Final = frozenset("'\"`$;&|<>()\\\n\r\t \x00")

#: Platforms the interface may offer, in the order it offers them.
#:
#: Ordered deliberately rather than alphabetically: ``amd64`` is the default
#: selection because it is what most people need, and an ordering that put
#: ``arm`` first would make the common case a decision.
SUPPORTED_PLATFORMS: Final = ("linux/amd64", "linux/arm64", "linux/arm")


class PlatformRelease(BaseModel):
    """One architecture's archive for one version."""

    model_config = {"extra": "forbid", "frozen": True}

    archive: str = Field(
        description="The archive's filename, as published.",
        min_length=1,
        max_length=255,
    )
    url: str = Field(
        description="Where to download it. May point at a mirror.",
        min_length=1,
        max_length=2048,
    )
    sha256: str = Field(
        description="The archive's SHA-256, lowercase hexadecimal.",
        min_length=64,
        max_length=64,
    )
    size_bytes: int = Field(description="The archive's size, for the progress a curl shows.", ge=1)
    tier: str = Field(
        description=(
            "How much this platform has actually been exercised. 'supported' "
            "means built, executed and covered by the installation matrix; "
            "'best-effort' means built and executed under emulation only."
        )
    )

    @field_validator("sha256")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        lowered = value.lower()
        if any(character not in "0123456789abcdef" for character in lowered):
            raise ValueError("sha256 must be lowercase hexadecimal")
        return lowered

    @field_validator("archive", "url")
    @classmethod
    def _validate_shell_safe(cls, value: str) -> str:
        """Refuse a value that would be unsafe in the rendered command.

        See :data:`SHELL_UNSAFE_CHARACTERS`. This runs for both fields,
        because both are interpolated into that command.
        """
        offending = sorted(SHELL_UNSAFE_CHARACTERS & set(value))
        if offending:
            raise ValueError(
                "contains characters that are not safe in the install command the "
                f"interface renders: {offending!r}. No legitimate release URL or "
                "filename needs them."
            )
        return value

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        """Refuse a URL that is not https, except on loopback.

        The same rule the agent applies to its controller URL, applied to the
        download. A plaintext download is not fatal here — the digest pins the
        bytes whatever happened in transit — but offering one would teach the
        wrong lesson, and an operator who sees ``http://`` in a command the
        product generated will reasonably assume it is fine.
        """
        if value.startswith("https://"):
            return value
        if value.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
            return value
        raise ValueError("url must be https (or http on loopback, for a local mirror)")

    @field_validator("tier")
    @classmethod
    def _validate_tier(cls, value: str) -> str:
        if value not in {"supported", "best-effort"}:
            raise ValueError("tier must be 'supported' or 'best-effort'")
        return value


class ReleaseEntry(BaseModel):
    """One release: its platforms, and what it needs to run.

    Every compatibility field is optional, and absent means "no constraint".
    That is what makes a v1 manifest upconvertible: its releases declared
    nothing, so they constrain nothing, which is exactly what they meant.
    """

    model_config = {"extra": "forbid", "frozen": True}

    channel: str = Field(
        default=DEFAULT_CHANNEL,
        description="stable | beta | alpha. See CHANNELS for the ordering.",
    )
    released_at: str | None = Field(
        default=None, description="RFC 3339, for display. Not used for ordering."
    )
    notes_url: str | None = Field(
        default=None, max_length=2048, description="Where the release notes are."
    )
    protocol_versions: tuple[int, ...] = Field(
        default=(),
        description=(
            "Agent protocol versions this release speaks. Empty means unknown, "
            "which is treated as a compatibility failure for an automatic "
            "update rather than as permission."
        ),
    )
    min_controller: str | None = Field(
        default=None,
        max_length=64,
        description="The oldest controller version that may drive this release.",
    )
    schema_requirement: str | None = Field(
        default=None,
        max_length=32,
        description=(
            "The Alembic revision this release needs applied. Compared against "
            "what the database actually reports, not against what the "
            "controller was built with."
        ),
    )
    platforms: dict[str, PlatformRelease] = Field(description="platform → archive.")

    @field_validator("channel")
    @classmethod
    def _validate_channel(cls, value: str) -> str:
        if value not in CHANNELS:
            raise ValueError(f"channel must be one of {', '.join(CHANNELS)}, got {value!r}")
        return value

    @field_validator("platforms")
    @classmethod
    def _validate_platforms(cls, value: dict[str, PlatformRelease]) -> dict[str, PlatformRelease]:
        if not value:
            raise ValueError("lists no platforms")
        for platform in value:
            if platform not in SUPPORTED_PLATFORMS:
                raise ValueError(
                    f"names platform {platform!r}, which is not one of "
                    f"{', '.join(SUPPORTED_PLATFORMS)}"
                )
        return value

    @field_validator("min_controller")
    @classmethod
    def _validate_min_controller(cls, value: str | None) -> str | None:
        """Refuse a constraint that cannot be evaluated.

        A ``min_controller`` nobody can parse is worse than none: it would be
        silently ignored, and the release would install on a controller it was
        meant to be withheld from.
        """
        if value is None:
            return None
        try:
            parse_version(value)
        except InvalidVersionError as error:
            raise ValueError(f"min_controller is not a semantic version: {error}") from error
        return value


class ReleaseManifest(BaseModel):
    """Every agent release this controller was built alongside."""

    model_config = {"extra": "forbid", "frozen": True}

    schema_version: int = Field(description="The manifest shape.")
    generated_at: str | None = Field(
        default=None, description="When the build wrote this, RFC 3339. For display."
    )
    recommended: str = Field(
        description="The version the interface tells people to install.",
        min_length=1,
        max_length=64,
    )
    minimum_supported: str = Field(
        description=(
            "The oldest agent version this controller is known to work with. "
            "Advisory for the *connection* — the protocol version is what that "
            "negotiates — and load-bearing for update planning, which reports "
            "an agent below this as `unsupported`."
        ),
        min_length=1,
        max_length=64,
    )
    channels: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "channel → the current version in it. Absent channels have no "
            "release, which is normal: a project with no beta has no beta entry."
        ),
    )
    releases: dict[str, ReleaseEntry] = Field(description="version → release.")

    @model_validator(mode="before")
    @classmethod
    def _upconvert(cls, data: Any) -> Any:
        """Read a v1 manifest as a v2 one.

        v1's ``releases[version]`` is a platform map; v2's is an entry with a
        platform map inside it. Distinguished by shape rather than by trusting
        ``schema_version``, because a file that declares v1 and is shaped like
        v2 is a file somebody edited by hand, and guessing from the declaration
        would produce a confusing error a long way from the cause.

        Runs ``mode="before"``, so the upconverted shape is what field
        validation sees and there is one validated model rather than two.
        """
        if not isinstance(data, dict):
            return data
        releases = data.get("releases")
        if not isinstance(releases, dict):
            return data
        converted: dict[str, Any] = {}
        for version, entry in releases.items():
            if isinstance(entry, dict) and "platforms" in entry:
                converted[version] = entry
                continue
            # A v1 platform map. Wrapped, declaring nothing — which is what it
            # claimed, and so the only honest conversion.
            converted[version] = {"channel": DEFAULT_CHANNEL, "platforms": entry}
        updated = dict(data)
        updated["releases"] = converted
        # A v1 manifest has no channel table. Its `recommended` is the stable
        # release by definition, so synthesising that one entry loses nothing
        # and means every consumer can read `channels` unconditionally.
        if not updated.get("channels") and isinstance(updated.get("recommended"), str):
            updated["channels"] = {DEFAULT_CHANNEL: updated["recommended"]}
        return updated

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if value not in SUPPORTED_MANIFEST_SCHEMA_VERSIONS:
            understood = ", ".join(str(v) for v in SUPPORTED_MANIFEST_SCHEMA_VERSIONS)
            raise ValueError(f"schema_version is {value}; this controller understands {understood}")
        return value

    @field_validator("channels")
    @classmethod
    def _validate_channels(cls, value: dict[str, str]) -> dict[str, str]:
        for channel in value:
            if channel not in CHANNELS:
                raise ValueError(
                    f"channels names {channel!r}, which is not one of {', '.join(CHANNELS)}"
                )
        return value

    @model_validator(mode="after")
    def _validate_references(self) -> ReleaseManifest:
        """Every version named anywhere has to exist, and has to be a version.

        A channel pointing at a release that is not in the file is the failure
        mode that produces "an update is available" followed by a 404, and it
        is cheap to refuse here instead.
        """
        for version in self.releases:
            try:
                parse_version(version)
            except InvalidVersionError as error:
                raise ValueError(
                    f"release key {version!r} is not a semantic version: {error}"
                ) from error
        for name, version in self.channels.items():
            if version not in self.releases:
                raise ValueError(
                    f"channel {name!r} names version {version!r}, which has no release entry"
                )
        if self.recommended not in self.releases:
            raise ValueError(f"recommended is {self.recommended!r}, which has no release entry")
        for field in ("recommended", "minimum_supported"):
            value: str = getattr(self, field)
            try:
                parse_version(value)
            except InvalidVersionError as error:
                raise ValueError(f"{field} is not a semantic version: {error}") from error
        return self

    def recommended_release(self) -> dict[str, PlatformRelease] | None:
        """The platforms available for the recommended version.

        Returns the platform map rather than the entry, because that is what
        the install-command endpoint has always consumed and the v2 shape
        should not have changed its caller.
        """
        entry = self.releases.get(self.recommended)
        return entry.platforms if entry is not None else None

    def channel_of(self, version: str) -> str:
        """Which channel a version is in, defaulting for an unknown one."""
        entry = self.releases.get(version)
        return entry.channel if entry is not None else DEFAULT_CHANNEL


class ManifestError(Exception):
    """A manifest that exists and cannot be used.

    Distinguished from absence on purpose. "No manifest is configured" is the
    normal state of a source checkout; "a manifest is configured and is
    broken" is a deployment fault, and reporting the second as the first would
    hide a misconfiguration behind a feature that looks merely unavailable.
    """


def parse_release_manifest(payload: bytes) -> ReleaseManifest:
    """Parse and validate a manifest from bytes.

    Separate from the file loading so a test — and the endpoint's own
    validation — can exercise it without a filesystem.
    """
    if len(payload) > MAX_MANIFEST_BYTES:
        raise ManifestError(
            f"the manifest is {len(payload)} bytes, over the {MAX_MANIFEST_BYTES} byte limit"
        )
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ManifestError(f"the manifest is not valid JSON: {error}") from error
    if not isinstance(document, dict):
        raise ManifestError("the manifest is not a JSON object")
    try:
        return ReleaseManifest.model_validate(document)
    except ValueError as error:
        raise ManifestError(f"the manifest is not usable: {error}") from error


def read_release_manifest(path: Path) -> ReleaseManifest:
    """Read and validate the manifest at *path*.

    Raises:
        ManifestError: the file cannot be read, or is not a usable manifest.
    """
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ManifestError(f"cannot read {path}: {error}") from error
    return parse_release_manifest(payload)


@lru_cache(maxsize=4)
def _cached(path: Path, modified_at: float, size: int) -> ReleaseManifest:
    """Parse once per (path, mtime, size).

    Keyed on the file's identity rather than on the path alone, so replacing
    the manifest — which is what an image upgrade does — is picked up without
    a restart, and so the cache cannot serve a stale document for the lifetime
    of the process.
    """
    del modified_at, size  # part of the key, not the computation
    return read_release_manifest(path)


def load_release_manifest(path: Path | None) -> ReleaseManifest | None:
    """Return the configured manifest, or ``None`` when none is configured.

    ``None`` for *absence*, which is the normal state: no release of this
    project has been published, so a source checkout and the current container
    image both have nothing to point this at.

    Raises:
        ManifestError: a path is configured and does not yield a usable
            manifest. Loud, because that is a deployment fault rather than a
            feature that is merely not there.
    """
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError as error:
        raise ManifestError(f"cannot read the agent release manifest {path}: {error}") from error
    return _cached(path, stat.st_mtime, stat.st_size)
