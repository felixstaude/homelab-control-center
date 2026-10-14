"""Release discovery: what update a host should be offered, and why not.

## The shape of the answer

"Is there an update" is not a boolean an interface can render usefully, and it
is not a boolean a policy can act on. A host two minor versions behind on a
controller too old to drive the newer release is in a different situation from
one that is simply current, and an interface that shows both as "no update
available" is lying to the administrator about which one needs attention.

So this module answers with an :class:`UpdatePlan`: a named outcome, the target
if there is one, the kind of step it would be, and — when something blocks it —
*every* reason rather than the first one found. An administrator who fixes one
blocker wants to know about the second one now, not after another round trip.

## Channels mean "the most instability I accept"

A host on ``stable`` considers only stable releases. A host on ``beta``
considers stable *and* beta, because somebody who opted into pre-releases
should still receive an ordinary release that happens to be newer — a beta
subscriber who stops getting stable updates has been punished for helping. A
host on ``alpha`` considers everything.

That is a subset rule, not an equality rule, and it falls out of
:data:`~hcc.agents.releases.CHANNELS` being ordered.

## Compatibility is a conjunction, evaluated completely

Five clauses, in :func:`evaluate_compatibility`. All of them are checked even
once one has failed, because the point is to report the whole truth about a
release rather than to short-circuit.

Absent constraints do not block. A schema-v1 release declared no protocol
versions and no minimum controller, and inventing requirements it never stated
would withhold releases that were installable before the manifest upgraded.
The one exception is deliberate and documented on
:data:`PROTOCOL_UNKNOWN_BLOCKS_AUTOMATIC`: an *automatic* update to a release
that does not say which protocol it speaks is refused, because "we do not know"
is not permission.

## What this module does not do

It does not decide whether an update *happens*. It has no clock, no database
and no knowledge of maintenance windows, and it never performs I/O. Policy
lives in :mod:`hcc.updates.policy` and scheduling in the rollout service; this
is the part that can be reasoned about and tested exhaustively, so it is kept
free of the parts that cannot.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from hcc.agents.releases import CHANNELS, DEFAULT_CHANNEL
from hcc.updates.version import (
    StepKind,
    Version,
    parse,
    step_kind,
    try_parse,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from hcc.agents.releases import PlatformRelease, ReleaseEntry, ReleaseManifest

#: Whether a release that does not declare its protocol versions may be
#: installed automatically.
#:
#: It may not. A manifest entry with no ``protocol_versions`` is a release
#: nobody recorded the compatibility of, and an automatic update is exactly the
#: situation where there is no human to notice. An operator who wants it anyway
#: can install it by hand, which is the right place for that decision.
#:
#: Stated as a constant rather than buried in a branch because it is the one
#: place this module treats absence as a blocker, and the asymmetry deserves a
#: name.
PROTOCOL_UNKNOWN_BLOCKS_AUTOMATIC: Final = True


class Outcome(enum.Enum):
    """What the update situation for one host is.

    A closed set. Every member is rendered differently by the interface and
    several are acted on differently by policy, which is why this is not a
    boolean with a message attached.
    """

    #: Running the newest release its channel offers.
    UP_TO_DATE = "up_to_date"
    #: A newer release exists and nothing blocks it.
    UPDATE_AVAILABLE = "update_available"
    #: A newer release exists and something blocks it. ``blockers`` says what.
    UPDATE_AVAILABLE_INCOMPATIBLE = "update_available_incompatible"
    #: The target is older than what is installed. Needs explicit authorization.
    DOWNGRADE = "downgrade"
    #: A version pin holds this host where it is.
    PINNED = "pinned"
    #: The host reports a version that is not a version, or is not in the
    #: metadata. Distinguished from `unsupported`: one is "we cannot tell", the
    #: other is "we can tell, and it is too old".
    UNKNOWN_VERSION = "unknown_version"
    #: Below the manifest's `minimum_supported`.
    UNSUPPORTED = "unsupported"
    #: No manifest, or it would not load. Not the same as "no update".
    METADATA_UNAVAILABLE = "metadata_unavailable"

    @property
    def is_actionable(self) -> bool:
        """Whether an administrator could start an update from this outcome."""
        return self in {Outcome.UPDATE_AVAILABLE, Outcome.DOWNGRADE}


class BlockerCode(enum.Enum):
    """Why a release cannot be installed on a host.

    Codes rather than prose, so the interface can group and the API can be
    asserted against. Each carries its own human sentence at the point it is
    raised, because the useful detail — which version, which architecture — is
    only available there.
    """

    #: The release speaks no protocol version this controller supports.
    PROTOCOL_MISMATCH = "protocol_mismatch"
    #: The release declares no protocol versions at all.
    PROTOCOL_UNDECLARED = "protocol_undeclared"
    #: This controller is older than the release requires.
    CONTROLLER_TOO_OLD = "controller_too_old"
    #: The database has not had the migration the release needs.
    SCHEMA_TOO_OLD = "schema_too_old"
    #: The release publishes no archive for this host's architecture.
    PLATFORM_UNAVAILABLE = "platform_unavailable"
    #: The target is in a channel this host has not opted into.
    CHANNEL_NOT_PERMITTED = "channel_not_permitted"
    #: Going backwards, without authorization to.
    DOWNGRADE_NOT_AUTHORIZED = "downgrade_not_authorized"
    #: The release metadata carries no signature this controller can verify.
    METADATA_UNAUTHENTICATED = "metadata_unauthenticated"


@dataclass(frozen=True, slots=True)
class Blocker:
    """One reason a release cannot be installed."""

    code: BlockerCode
    detail: str


@dataclass(frozen=True, slots=True)
class Compatibility:
    """Whether a release can run here, and everything wrong if not."""

    blockers: tuple[Blocker, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.blockers

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(blocker.code.value for blocker in self.blockers)


def permitted_channels(channel: str) -> tuple[str, ...]:
    """Which channels a host on *channel* will accept releases from.

    "The most instability I accept", so this is a prefix of
    :data:`~hcc.agents.releases.CHANNELS` — a beta subscriber still receives
    stable releases, because somebody who opted into pre-releases should not be
    punished for it by missing ordinary ones.

    An unrecognised channel yields only the most stable one. Failing closed:
    a typo in a stored policy must not widen what a host accepts.
    """
    if channel not in CHANNELS:
        return (DEFAULT_CHANNEL,)
    return CHANNELS[: CHANNELS.index(channel) + 1]


def evaluate_compatibility(
    entry: ReleaseEntry,
    *,
    controller_version: str,
    supported_protocol_versions: Iterable[int],
    applied_schema_revisions: frozenset[str],
    host_platform: str,
    automatic: bool,
    metadata_authentic: bool,
) -> Compatibility:
    """Check every compatibility clause for one release on one host.

    Args:
        entry: the release being considered.
        controller_version: this controller's own version.
        supported_protocol_versions: what this controller's socket accepts.
        applied_schema_revisions: every Alembic revision applied to the
            database. A membership test rather than an ordering comparison —
            Alembic revisions are a linked list with opaque identifiers, and
            "is this revision applied" is both exactly the question and
            trivially correct, where "is this revision at or before the head"
            needs the migration graph.
        host_platform: the host's ``GOOS/GOARCH``, as the agent reports it.
        automatic: whether this is an automatic update. Only affects the
            treatment of an undeclared protocol version; see
            :data:`PROTOCOL_UNKNOWN_BLOCKS_AUTOMATIC`.
        metadata_authentic: whether the manifest this release came from
            carries a signature this controller verified. A property of the
            *manifest* rather than of this entry, passed in because the
            outcome depends on both and a caller holding only one of them
            cannot decide.

    Every clause is evaluated even after one fails. An administrator fixing one
    blocker wants to know about the second one now.
    """
    blockers: list[Blocker] = []
    supported = tuple(supported_protocol_versions)

    # 0. Authenticity, before anything about the release itself.
    #
    # ## Why this blocks a manual start too, not only an automatic one
    #
    # Because the agent refuses unsigned metadata unconditionally. Its
    # signature gate is not conditioned on policy — it cannot be, since the
    # digest it checks comes *out of* the signed metadata, so without a
    # signature there is nothing establishing what the bytes should be.
    #
    # So a plan that offered a manual update here would offer an action that
    # always fails on the host. That is the "button that 422s" failure in its
    # worst form: the administrator presses it, the job goes to `failed` a
    # minute later, and the reason is a sentence about signatures on a page
    # about versions.
    #
    # The install command for a *fresh* host is deliberately different and is
    # unaffected: there the digest travels inside the administrator's own
    # authenticated session with this controller, and a human verifies it with
    # `sha256sum -c`. See hcc.agents.releases. An unattended agent has no such
    # session and no such human.
    if not metadata_authentic:
        blockers.append(
            Blocker(
                code=BlockerCode.METADATA_UNAUTHENTICATED,
                detail=(
                    "the release metadata carries no signature this controller "
                    "can verify, so nothing establishes which bytes this release "
                    "consists of. Agents refuse an update on that basis, so it is "
                    "not offered. Configure HCC_RELEASE_SIGNING_KEYS with the "
                    "publisher's key, or check who signed the metadata."
                ),
            )
        )

    # 1. Protocol.
    if entry.protocol_versions:
        if not set(entry.protocol_versions) & set(supported):
            blockers.append(
                Blocker(
                    code=BlockerCode.PROTOCOL_MISMATCH,
                    detail=(
                        f"the release speaks protocol "
                        f"{', '.join(str(v) for v in entry.protocol_versions)} and this "
                        f"controller speaks {', '.join(str(v) for v in supported)}"
                    ),
                )
            )
    elif automatic and PROTOCOL_UNKNOWN_BLOCKS_AUTOMATIC:
        blockers.append(
            Blocker(
                code=BlockerCode.PROTOCOL_UNDECLARED,
                detail=(
                    "the release does not declare which protocol versions it "
                    "speaks, so it will not be installed automatically. Install "
                    "it by hand if you know it is compatible."
                ),
            )
        )

    # 2. Controller version.
    if entry.min_controller is not None:
        required = parse(entry.min_controller)
        running = try_parse(controller_version)
        if running is None:
            blockers.append(
                Blocker(
                    code=BlockerCode.CONTROLLER_TOO_OLD,
                    detail=(
                        f"the release needs controller {required} and this "
                        f"controller reports {controller_version!r}, which is not a "
                        "version that can be compared"
                    ),
                )
            )
        elif running < required:
            blockers.append(
                Blocker(
                    code=BlockerCode.CONTROLLER_TOO_OLD,
                    detail=(
                        f"the release needs controller {required} or newer; this "
                        f"one is {running}. Upgrade the controller first."
                    ),
                )
            )

    # 3. Database schema.
    if entry.schema_requirement is not None and (
        entry.schema_requirement not in applied_schema_revisions
    ):
        blockers.append(
            Blocker(
                code=BlockerCode.SCHEMA_TOO_OLD,
                detail=(
                    f"the release needs database revision "
                    f"{entry.schema_requirement}, which is not applied. Run the "
                    "controller's migrations first."
                ),
            )
        )

    # 4. Architecture.
    if host_platform not in entry.platforms:
        available = ", ".join(sorted(entry.platforms)) or "none"
        blockers.append(
            Blocker(
                code=BlockerCode.PLATFORM_UNAVAILABLE,
                detail=(
                    f"the release publishes no archive for {host_platform}. Available: {available}."
                ),
            )
        )

    return Compatibility(blockers=tuple(blockers))


@dataclass(frozen=True, slots=True)
class UpdatePlan:
    """What should happen to one host, and why.

    Carries the whole answer rather than a boolean, so the interface, the API
    and the policy engine all read the same object instead of three partial
    views that can disagree.
    """

    outcome: Outcome
    #: What the host reports running, when it is a version this can parse.
    installed: Version | None = None
    #: What it would move to. None when there is nothing to move to.
    target: Version | None = None
    #: The archive for this host's architecture, when the target has one.
    artifact: PlatformRelease | None = None
    #: major | minor | patch | prerelease | none
    step: str = StepKind.NONE
    #: The channel the target is in.
    target_channel: str | None = None
    #: Everything standing in the way, empty when nothing is.
    blockers: tuple[Blocker, ...] = field(default_factory=tuple)
    #: A sentence for an operator, when the outcome needs one.
    detail: str | None = None

    @property
    def has_update(self) -> bool:
        """Whether a newer release exists, blocked or not.

        Distinct from ``outcome.is_actionable``: a blocked update is still an
        update the administrator should know about, and a dashboard that hid it
        would make a fleet look current when it is stuck.
        """
        return self.outcome in {
            Outcome.UPDATE_AVAILABLE,
            Outcome.UPDATE_AVAILABLE_INCOMPATIBLE,
        }


def _newest_in_channels(
    manifest: ReleaseManifest, channels: tuple[str, ...]
) -> tuple[Version, ReleaseEntry] | None:
    """The highest-precedence release in any of *channels*.

    Read from the release entries rather than from the channel table, because
    the table names one version per channel and the entries are the full set —
    a manifest can carry several stable releases, and "newest" should mean
    newest rather than "whatever the table happens to point at".
    """
    candidates: list[tuple[Version, ReleaseEntry]] = []
    for raw, entry in manifest.releases.items():
        if entry.channel not in channels:
            continue
        # Every key was validated as a version when the manifest was parsed,
        # so this cannot fail — and `try_parse` rather than `parse` keeps that
        # assumption from becoming an exception if it ever stops holding.
        parsed = try_parse(raw)
        if parsed is not None:
            candidates.append((parsed, entry))
    if not candidates:
        return None
    return max(candidates, key=lambda pair: pair[0])


def plan_update(
    manifest: ReleaseManifest | None,
    *,
    installed_version: str | None,
    host_platform: str,
    channel: str = DEFAULT_CHANNEL,
    pinned_version: str | None = None,
    controller_version: str,
    supported_protocol_versions: Iterable[int],
    applied_schema_revisions: frozenset[str] = frozenset(),
    allow_downgrade: bool = False,
    automatic: bool = False,
    metadata_authentic: bool = False,
) -> UpdatePlan:
    """Decide what update one host should be offered.

    Pure: no clock, no database, no I/O. Everything it needs is an argument,
    which is what makes the whole decision table testable.

    Args:
        manifest: the release metadata, or None when none is configured.
        installed_version: what the host reports. May be unparseable; that is a
            renderable outcome rather than an error.
        host_platform: ``GOOS/GOARCH``.
        channel: the most instability this host accepts.
        pinned_version: hold the host at exactly this version.
        controller_version: this controller's own version.
        supported_protocol_versions: what this controller's socket accepts.
        applied_schema_revisions: every applied Alembic revision.
        allow_downgrade: authorize going backwards.
        automatic: whether this decision is being made without a human.
        metadata_authentic: whether the manifest's signature verified against a
            key this controller trusts. **Defaults to False**, which fails
            closed: a caller that has not established authenticity has not
            established it, and defaulting the other way would mean every
            caller who forgot the argument silently offered unverifiable
            updates.
    """
    if manifest is None:
        return UpdatePlan(
            outcome=Outcome.METADATA_UNAVAILABLE,
            installed=try_parse(installed_version),
            detail=(
                "no release metadata is configured, so this controller does not "
                "know what versions exist. Updates cannot be planned."
            ),
        )

    installed = try_parse(installed_version)
    channels = permitted_channels(channel)

    # A pin is the strongest statement an administrator can make, so it is
    # resolved before anything else — including the channel, because pinning a
    # beta on a stable host is a deliberate act and refusing it would make the
    # pin advisory.
    if pinned_version is not None:
        return _plan_pinned(
            manifest,
            pinned_version=pinned_version,
            installed=installed,
            installed_version=installed_version,
            host_platform=host_platform,
            controller_version=controller_version,
            supported_protocol_versions=supported_protocol_versions,
            applied_schema_revisions=applied_schema_revisions,
            allow_downgrade=allow_downgrade,
            automatic=automatic,
            metadata_authentic=metadata_authentic,
        )

    newest = _newest_in_channels(manifest, channels)
    if newest is None:
        return UpdatePlan(
            outcome=Outcome.METADATA_UNAVAILABLE,
            installed=installed,
            detail=(
                f"the release metadata lists no release in "
                f"{' or '.join(channels)}, so there is nothing to offer."
            ),
        )
    target_version, target_entry = newest

    if installed is None:
        # The host reports something unparseable, or nothing at all. The newest
        # release is still reported so the interface has something to show, but
        # no comparison is claimed.
        return UpdatePlan(
            outcome=Outcome.UNKNOWN_VERSION,
            target=target_version,
            target_channel=target_entry.channel,
            artifact=target_entry.platforms.get(host_platform),
            detail=(
                f"this host reports agent version {installed_version!r}, which "
                "cannot be compared with the published releases. The newest "
                f"available is {target_version}."
            ),
        )

    minimum = try_parse(manifest.minimum_supported)
    if minimum is not None and installed < minimum:
        # Reported before the comparison, because "too old to be supported" is
        # a different conversation from "an update is available" — it may need
        # a reinstall rather than an upgrade.
        return UpdatePlan(
            outcome=Outcome.UNSUPPORTED,
            installed=installed,
            target=target_version,
            target_channel=target_entry.channel,
            artifact=target_entry.platforms.get(host_platform),
            step=step_kind(installed, target_version),
            detail=(
                f"this host runs {installed}, below the oldest supported version "
                f"{minimum}. Upgrading to {target_version} is untested from here; "
                "reinstalling the agent is the supported path."
            ),
        )

    if installed >= target_version:
        return UpdatePlan(
            outcome=Outcome.UP_TO_DATE,
            installed=installed,
            target=target_version,
            target_channel=target_entry.channel,
            step=StepKind.NONE,
        )

    compatibility = evaluate_compatibility(
        target_entry,
        controller_version=controller_version,
        supported_protocol_versions=supported_protocol_versions,
        applied_schema_revisions=applied_schema_revisions,
        host_platform=host_platform,
        automatic=automatic,
        metadata_authentic=metadata_authentic,
    )
    return UpdatePlan(
        outcome=(
            Outcome.UPDATE_AVAILABLE if compatibility.ok else Outcome.UPDATE_AVAILABLE_INCOMPATIBLE
        ),
        installed=installed,
        target=target_version,
        target_channel=target_entry.channel,
        artifact=target_entry.platforms.get(host_platform),
        step=step_kind(installed, target_version),
        blockers=compatibility.blockers,
    )


def _plan_pinned(
    manifest: ReleaseManifest,
    *,
    pinned_version: str,
    installed: Version | None,
    installed_version: str | None,
    host_platform: str,
    controller_version: str,
    supported_protocol_versions: Iterable[int],
    applied_schema_revisions: frozenset[str],
    allow_downgrade: bool,
    automatic: bool,
    metadata_authentic: bool,
) -> UpdatePlan:
    """Resolve a host held at an exact version."""
    pinned = try_parse(pinned_version)
    entry = manifest.releases.get(pinned_version)
    if pinned is None or entry is None:
        return UpdatePlan(
            outcome=Outcome.UNKNOWN_VERSION,
            installed=installed,
            detail=(
                f"this host is pinned to {pinned_version!r}, which is not a "
                "release this controller knows about. Change or clear the pin."
            ),
        )

    artifact = entry.platforms.get(host_platform)
    if installed is not None and installed == pinned:
        return UpdatePlan(
            outcome=Outcome.PINNED,
            installed=installed,
            target=pinned,
            target_channel=entry.channel,
            artifact=artifact,
            step=StepKind.NONE,
            detail=f"pinned to {pinned}, and running it.",
        )

    compatibility = evaluate_compatibility(
        entry,
        controller_version=controller_version,
        supported_protocol_versions=supported_protocol_versions,
        applied_schema_revisions=applied_schema_revisions,
        host_platform=host_platform,
        automatic=automatic,
        metadata_authentic=metadata_authentic,
    )

    if installed is not None and installed > pinned:
        # Going backwards. Reported as a downgrade whether or not it is
        # authorized, with the authorization expressed as a blocker — so the
        # interface can show "this would be a downgrade" and the reason it
        # cannot proceed in one place.
        blockers = compatibility.blockers
        if not allow_downgrade:
            blockers = (
                *blockers,
                Blocker(
                    code=BlockerCode.DOWNGRADE_NOT_AUTHORIZED,
                    detail=(
                        f"moving from {installed} to {pinned} is a downgrade, which "
                        "needs explicit authorization."
                    ),
                ),
            )
        return UpdatePlan(
            outcome=Outcome.DOWNGRADE,
            installed=installed,
            target=pinned,
            target_channel=entry.channel,
            artifact=artifact,
            step=step_kind(installed, pinned),
            blockers=blockers,
        )

    return UpdatePlan(
        outcome=(
            Outcome.UPDATE_AVAILABLE if compatibility.ok else Outcome.UPDATE_AVAILABLE_INCOMPATIBLE
        ),
        installed=installed,
        target=pinned,
        target_channel=entry.channel,
        artifact=artifact,
        step=step_kind(installed, pinned) if installed else StepKind.NONE,
        blockers=compatibility.blockers,
        detail=(
            None
            if installed is not None
            else f"this host reports {installed_version!r}; it is pinned to {pinned}."
        ),
    )


def version_distribution(installed: Mapping[str, int]) -> list[tuple[str, int]]:
    """Agent versions and their counts, newest first.

    For the overview's "what is the fleet running" panel. Sorted by version
    precedence rather than by count, because the useful reading is "how far
    behind is the tail", and unparseable versions sort last under a stable key
    so that a host reporting nonsense is visible rather than hidden or crashing
    the sort.
    """
    parsed: list[tuple[Version | None, str, int]] = [
        (try_parse(raw), raw, count) for raw, count in installed.items()
    ]
    known = sorted(
        ((version, raw, count) for version, raw, count in parsed if version is not None),
        key=lambda row: row[0],
        reverse=True,
    )
    unknown = sorted(
        ((version, raw, count) for version, raw, count in parsed if version is None),
        key=lambda row: row[1],
    )
    return [(raw, count) for _, raw, count in [*known, *unknown]]
