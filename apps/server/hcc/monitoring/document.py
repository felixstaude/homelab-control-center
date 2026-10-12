"""The configuration document an agent actually runs.

Phase 2, implementing ``docs/AGENT_ARCHITECTURE.md`` §6.2.

## The checksum is not in the document

It could not be: a digest over a document containing itself has no fixed
point. So a revision is a **pair** — the canonical document bytes and the
checksum over them — and both travel together in the frame that delivers it.
Stated because the obvious reading of §6.2's example, which shows ``checksum``
as a field, is the one that does not work.

## There are no capabilities in this document

``AGENT_ARCHITECTURE.md`` §6.2 sketches a ``capabilities`` map for remote
management. It is deliberately **absent** rather than present with every value
``false``. An all-false map would assert that a capability system exists and
that these are its settings, which is not true: no capability is implemented,
nothing reads such a field, and an agent that found one could reasonably infer
there is a mechanism to grant. When remote management arrives it will add the
key, bump :data:`DOCUMENT_VERSION`, and be a reviewable change rather than the
quiet population of a field that was already there.

## Limits are sent, not assumed

The agent re-validates every configuration before applying it
(``AGENT_ARCHITECTURE.md`` §6.3), and it must be able to do that while the
controller is unreachable. Sending the limits means the agent's validation
agrees with the controller's by construction rather than by both being
upgraded together — which, across the internet with no inbound ports, they
will not be.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from hcc.monitoring import modules as registry
from hcc.monitoring.canonical import canonicalize, checksum_of_bytes
from hcc.monitoring.schedule import MAX_INTERVAL_SECONDS, MIN_INTERVAL_SECONDS

if TYPE_CHECKING:
    import uuid
    from datetime import datetime

    from hcc.monitoring.resolve import EffectiveConfiguration

#: Version of the document *shape*. Bumped when a key is added, removed or
#: changes meaning — not when its contents change, which is what the
#: per-host revision number is for.
#:
#: An agent that does not recognise the shape version refuses the document and
#: keeps running its last known good one, which is the correct behaviour for a
#: binary that cannot be upgraded in step with the controller.
DOCUMENT_VERSION: Final = 1


@dataclass(frozen=True, slots=True)
class ConfigurationRevision:
    """One version of one host's configuration, ready to store and to send.

    Immutable, and that is the property the whole synchronisation story rests
    on: a revision's bytes never change after it is created, so a checksum an
    agent acknowledged a month ago still identifies exactly what it was given.

    ## Two checksums, because they answer two different questions

    :attr:`checksum` covers the whole document, ``version`` and
    ``generated_at`` included. It is what the agent verifies and acknowledges:
    "the bytes I applied are the bytes you sent".

    :attr:`content_checksum` covers only the part that depends on
    *configuration* — the modules and the limits. It answers "would generating
    a revision now produce anything new?", and without it the answer would
    always be yes: ``generated_at`` differs on every call, so a whole-document
    comparison would mint a fresh revision every time an agent connected, and
    a host nobody had touched would accumulate a revision per reconnection
    while reporting itself permanently out of date.

    Keeping both is cheaper than the alternatives. Dropping ``generated_at``
    from the document would make the comparison work but leave a stored
    configuration file that cannot say when it was produced; comparing parsed
    subtrees instead would mean no stored digest, so "which hosts differ from
    the global defaults?" could not be answered by a query.
    """

    host_id: uuid.UUID
    #: Monotonic per host, assigned by the controller.
    version: int
    #: The canonical bytes. What is stored, what is sent, and what
    #: :attr:`checksum` covers (ADR-050).
    document: bytes
    checksum: str
    #: Digest over the configuration-dependent part only. Compared to decide
    #: whether a new revision is warranted; never sent to the agent, which has
    #: no use for it.
    content_checksum: str
    generated_at: datetime

    @property
    def text(self) -> str:
        """The document as text, for storage in a ``text`` column.

        Decoded rather than stored as ``bytea`` because it is JSON and a human
        reading the table should see it. UTF-8 round-trips exactly, so the
        digest is unaffected.
        """
        return self.document.decode("utf-8")


def build_revision(
    *,
    host_id: uuid.UUID,
    version: int,
    effective: EffectiveConfiguration,
    generated_at: datetime,
) -> ConfigurationRevision:
    """Serialise an effective configuration into a numbered, checksummed revision.

    ``generated_at`` is a parameter rather than read from the clock, because
    the document is checksummed: a clock read inside would make two calls with
    identical inputs produce different digests, and "did the configuration
    change?" would answer yes every time it was asked.

    That is also why ``generated_at`` is **in** the document rather than
    beside it. An operator looking at a revision needs to know when it was
    produced, and a field the checksum does not cover is a field that can be
    altered without detection.
    """
    # Built first and checksummed on its own, so that the content digest is
    # over exactly the configuration-dependent subtree and cannot drift from
    # what the document contains — the same object goes into both.
    content: dict[str, Any] = {
        "document_version": DOCUMENT_VERSION,
        "registry_version": registry.REGISTRY_VERSION,
        "modules": effective.to_document(),
        "limits": {
            "min_interval_seconds": MIN_INTERVAL_SECONDS,
            "max_interval_seconds": MAX_INTERVAL_SECONDS,
            "module_min_interval_seconds": {
                module.id: module.min_interval_seconds for module in registry.MODULE_LIST
            },
        },
    }
    document: dict[str, Any] = {
        **content,
        "host_id": str(host_id),
        "version": version,
        "generated_at": generated_at.isoformat(),
    }
    canonical = canonicalize(document)
    return ConfigurationRevision(
        host_id=host_id,
        version=version,
        document=canonical,
        checksum=checksum_of_bytes(canonical),
        content_checksum=checksum_of_bytes(canonicalize(content)),
        generated_at=generated_at,
    )
