"""What the interface needs in order to render a verified install command.

    GET /api/v1/agent-releases

## Why this is Administrator-only

It reveals this controller's version, which agent versions it expects to talk
to, and where their archives live. None of that is useful to a legitimate
Viewer and all of it is useful to somebody matching a deployment against known
vulnerabilities — the same reasoning that moved ``/api/v1/info`` behind a
session in milestone 1.4 (ADR-032), applied one role further up because this
is specifically the information you would want before attacking an agent.

It is also only ever needed by somebody who is about to install an agent, and
registering a host is already Administrator-only.

## Why it is not under /api/v1/agents

The design document
(``docs/plans/simple-installation-and-agent-distribution.md`` §4.3) proposed
``GET /api/v1/agents/install-manifest``. That path is wrong, and the amendment
is recorded here rather than silently: everything under ``/api/v1/agents`` is
mounted on a router whose dependency authenticates a **per-host agent
credential** and refuses a browser session. Mixing a session-gated route into
that prefix would make the module's own invariant — "a browser session is
never accepted here" — false for one path, which is exactly the kind of
exception that stops being noticed.

So this is ``/api/v1/agent-releases`` on the administrator router: privileged
by placement, in the prefix a reader would expect it in.

The design also had ``hcc-agent upgrade`` call this endpoint with its agent
credential. It does not: `upgrade` takes ``--from`` and ``--sha256``, which the
administrator copies from the host's page in the same session as everything
else. That keeps the endpoint to one caller and one credential type, and keeps
the agent from needing an HTTP client for anything but enrolment and the
socket.
"""

from __future__ import annotations

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from hcc import __version__
from hcc.agents.protocol import PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS
from hcc.agents.releases import (
    SUPPORTED_PLATFORMS,
    ManifestError,
    ReleaseManifest,
    load_release_manifest,
)
from hcc.api.deps import SettingsDep
from hcc.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/agent-releases", tags=["agents"])


class PlatformArtifactResponse(BaseModel):
    """One architecture's download, with the digest that pins it."""

    platform: str = Field(description="`linux/amd64`, `linux/arm64` or `linux/arm`.")
    archive: str = Field(description="The archive's filename.")
    url: str = Field(description="Where to download it from.")
    sha256: str = Field(
        description=(
            "The archive's SHA-256. Rendered into the install command so that "
            "`sha256sum -c -` fails before the `tar` if the bytes are not the "
            "bytes this controller expects."
        )
    )
    size_bytes: int = Field(description="The archive's size.")
    tier: str = Field(
        description=(
            "`supported` — built, executed and covered by the installation "
            "matrix. `best-effort` — built and executed under emulation only, "
            "with no hardware behind the claim."
        )
    )


class AgentReleasesResponse(BaseModel):
    """What this controller knows about installable agents."""

    available: bool = Field(
        description=(
            "Whether a verified download is on offer. False means no release "
            "manifest is configured, which is the state of any build made "
            "before the first release was published — the interface then shows "
            "the build-from-source path rather than a download command whose "
            "URL would not resolve."
        )
    )
    unavailable_reason: str | None = Field(
        default=None,
        description="Why there is nothing to download. Null when `available`.",
    )
    recommended_version: str | None = Field(
        default=None, description="The agent version to install. Null when unavailable."
    )
    minimum_supported_version: str | None = Field(
        default=None,
        description=(
            "The oldest agent version this controller is known to work with. "
            "Advisory: the protocol version is what a connection negotiates, "
            "and nothing is refused on the strength of this field."
        ),
    )
    platforms: list[PlatformArtifactResponse] = Field(
        default_factory=list,
        description="The architectures on offer, in the order to present them.",
    )
    protocol_version: int = Field(description="The agent protocol version this controller speaks.")
    supported_protocol_versions: list[int] = Field(
        description="Every protocol version this controller will accept on a connection."
    )
    server_version: str = Field(description="This controller's own version.")


def _unavailable(reason: str, settings_version: str) -> AgentReleasesResponse:
    return AgentReleasesResponse(
        available=False,
        unavailable_reason=reason,
        protocol_version=PROTOCOL_VERSION,
        supported_protocol_versions=sorted(SUPPORTED_PROTOCOL_VERSIONS),
        server_version=settings_version,
    )


def _platforms(manifest: ReleaseManifest) -> list[PlatformArtifactResponse]:
    """Render the recommended version's platforms, in presentation order."""
    release = manifest.recommended_release() or {}
    ordered: list[PlatformArtifactResponse] = []
    # Iterating the *supported* order rather than the manifest's keys, so the
    # interface's dropdown is stable whatever order the build wrote them in —
    # and so a platform the manifest offers that this build does not know
    # about is left out rather than rendered into a command.
    for platform in SUPPORTED_PLATFORMS:
        artifact = release.get(platform)
        if artifact is None:
            continue
        ordered.append(
            PlatformArtifactResponse(
                platform=platform,
                archive=artifact.archive,
                url=artifact.url,
                sha256=artifact.sha256,
                size_bytes=artifact.size_bytes,
                tier=artifact.tier,
            )
        )
    return ordered


@router.get(
    "",
    summary="The agent releases this controller can vouch for",
    response_model=AgentReleasesResponse,
    status_code=status.HTTP_200_OK,
)
async def agent_releases(settings: SettingsDep) -> AgentReleasesResponse:
    """Return the install manifest, or say why there is none.

    **200 in both cases.** "No release has been published" is a fact about
    this deployment, not an error in the request — a 404 would make the
    interface treat a perfectly healthy controller as broken, and a 503 would
    say the controller is unavailable when it is the download that is.
    """
    try:
        manifest = load_release_manifest(settings.agent_release_manifest)
    except ManifestError as error:
        # A configured manifest that cannot be used is a deployment fault, so
        # it is logged with the traceback — which names the parse step that
        # failed, and is the only place that detail exists. The *response*
        # carries no traceback: the interface's job is to tell the
        # administrator to build from source, not to show them a stack.
        logger.exception("agent_release_manifest_unusable", detail=str(error))
        return _unavailable(
            "This controller has an agent release manifest configured and it cannot "
            "be read. The server log says why. Until it is fixed, build the agent "
            "from source.",
            __version__,
        )

    if manifest is None:
        return _unavailable(
            "No agent release has been published for this version of the control "
            "plane, so there is no verified download to offer. Build the agent from "
            "source — see agents/linux/deploy/README.md.",
            __version__,
        )

    platforms = _platforms(manifest)
    if not platforms:
        # A manifest whose recommended version lists no platform this build
        # knows about. Reported as unavailable rather than as an empty
        # dropdown, which would be an interface offering nothing with no
        # explanation.
        logger.error(
            "agent_release_manifest_has_no_usable_platform",
            recommended=manifest.recommended,
        )
        return _unavailable(
            "The configured release manifest offers no architecture this control "
            "plane recognises. Build the agent from source.",
            __version__,
        )

    return AgentReleasesResponse(
        available=True,
        recommended_version=manifest.recommended,
        minimum_supported_version=manifest.minimum_supported,
        platforms=platforms,
        protocol_version=PROTOCOL_VERSION,
        supported_protocol_versions=sorted(SUPPORTED_PROTOCOL_VERSIONS),
        server_version=__version__,
    )
