"""Monitoring configuration: global defaults, host overrides, and revisions.

Phase 2, plus one observation table added later. Four tables, and the shape of
each is decided by a question the product has to be able to answer.

* :class:`GlobalModuleDefault` — "what does every host do unless told
  otherwise?" One row per module, at most.
* :class:`HostModuleOverride` — "what does *this* host do differently?" One row
  per host and module, at most, and a ``NULL`` column means **inherit** rather
  than "false" or "unset".
* :class:`HostConfigRevision` — "what document was this agent actually
  running?" A full snapshot per version, because that is the question, and a
  diff chain answers it only after a replay nobody wants to audit.
* :class:`HostModuleHealth` — "is this module's collector actually working?"
  The only one of the four that records an observation rather than an
  intention, which is why it is a separate table and not a column on the
  override row.

## Why ``NULL`` means inherit, in a column rather than in a convention

The mission's §5 names the failure directly: ambiguous combinations of
``enabled=false``, missing values and inherited values. The three states are
genuinely different and a schema where two of them share a representation
cannot be fixed in the API above it.

So each override column is nullable and ``NULL`` is inherit. A row with
*every* value column null is legal and means the same as no row — it overrides
nothing — but the service does not create one: a reset **deletes** the row
(:meth:`hcc.services.monitoring.MonitoringConfigService.reset_host_override`).
The audit trail records who reset what and when, with the actor as well, which
is strictly more than the row's ``updated_at`` could say; and keeping all-null
rows would make "has this host been customised?" a column-by-column check
rather than a row count. The nullability is still per column rather than
per row, because a host overriding only the *interval* while inheriting
``enabled`` is the common case and needs exactly one null.

## Revisions are immutable, and versions are monotonic per host

A revision's ``document`` and ``checksum`` are never updated after insert.
Acknowledgement state is, which is why it is on these rows rather than
somewhere a write could be mistaken for a document change: the pair that
identifies what an agent ran must not be rewritable, or an acknowledgement
from last month would start referring to something else.

Version monotonicity is enforced by a unique ``(host_id, version)`` and
assigned under the same per-host advisory lock the rest of the host invariants
use (ADR-044), so two administrators saving at once produce two revisions
rather than one lost update.

## Why no row per *module* per revision

A revision stores one document, not a row per module within it. The document
is the unit that is checksummed, sent and acknowledged, so decomposing it in
the database would mean reassembling it to verify a checksum — and a
reassembly is exactly the second serialisation ADR-050 exists to avoid.
"""

from __future__ import annotations

import enum
import uuid

# Imported at runtime: SQLAlchemy resolves `Mapped[...]` annotations at
# class-definition time. See hcc/models/user.py.
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    FetchedValue,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, validates

from hcc.db import Base
from hcc.monitoring.canonical import CHECKSUM_LENGTH
from hcc.monitoring.modules import MAX_MODULE_ID_LENGTH, MODULE_IDS

#: SQL fragment listing every known module, for the CHECK constraints.
#:
#: A CHECK rather than a foreign key to a modules table, because the registry
#: is **code**: it ships with the controller, it is versioned with it, and a
#: table would have to be kept in step by a migration that could disagree with
#: the code it mirrors. The cost is that adding a module needs a migration to
#: widen the constraint, which is the right amount of friction for a change
#: that also needs an agent release.
_MODULE_ID_SQL: Final = ", ".join(f"'{module_id}'" for module_id in MODULE_IDS)

#: Longest a single rejection problem string may be, and how many are kept.
#: An agent reporting why it refused a configuration is reporting
#: attacker-controlled text from a possibly-compromised host, so it is bounded
#: before it is stored rather than trusted by every later reader.
MAX_PROBLEM_LENGTH: Final = 256
MAX_PROBLEMS: Final = 32


class GlobalModuleDefault(Base):
    """What every host does with one module, unless overridden.

    Keyed by ``module_id`` rather than by a surrogate: there is at most one row
    per module by definition, so a surrogate key would permit two rows that the
    resolution layer would then have to choose between.
    """

    __tablename__ = "global_module_defaults"

    __table_args__ = (
        CheckConstraint(f"module_id IN ({_MODULE_ID_SQL})", name="module_id_is_known"),
    )

    module_id: Mapped[str] = mapped_column(
        String(MAX_MODULE_ID_LENGTH),
        primary_key=True,
        doc="A `hcc.monitoring.modules` identifier. Constrained to the registry.",
    )

    enabled: Mapped[bool | None] = mapped_column(
        Boolean,
        nullable=True,
        doc=(
            "Whether this module is on by default. NULL means inherit the "
            "registry default, which is off. Never defaulted to true by a "
            "migration: enabling collection is an administrator's act."
        ),
    )

    schedule: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        doc=(
            "A validated schedule document, or NULL to inherit the module's "
            "default interval. JSONB rather than text: nothing checksums this "
            "column — the revision document does — so key order is free."
        ),
    )

    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        nullable=True,
        doc=(
            "Who last changed it. No foreign key, for the reason "
            "`audit_events` has none (ADR-038): deleting a user must not "
            "erase the record of what they did."
        ),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        server_onupdate=FetchedValue(),
    )

    @validates("module_id")
    def _validate_module_id(self, _key: str, value: str) -> str:
        """Refuse an unknown module at assignment rather than at flush.

        Without this the failure is an opaque CHECK violation from the driver,
        pointing at the session instead of at the caller that chose the value.
        """
        if value not in MODULE_IDS:
            raise ValueError(
                f"unknown module {value!r}; this controller knows: {', '.join(MODULE_IDS)}"
            )
        return value

    def __repr__(self) -> str:
        return (
            f"GlobalModuleDefault(module_id={self.module_id!r}, "
            f"enabled={self.enabled!r}, has_schedule={self.schedule is not None})"
        )


class HostModuleOverride(Base):
    """What one host does differently with one module.

    Every value column is nullable and ``NULL`` means **inherit the global
    default** — not false, and not "unset" in a way that could be confused
    with false. See the module docstring.
    """

    __tablename__ = "host_module_overrides"

    __table_args__ = (
        # One override per host per module. Without this, two rows would make
        # resolution depend on row order, which is not a thing that exists.
        UniqueConstraint("host_id", "module_id", name="uq_host_module_overrides_host_module"),
        # Resolution reads every override for one host at once.
        Index("ix_host_module_overrides_host_id", "host_id"),
        CheckConstraint(f"module_id IN ({_MODULE_ID_SQL})", name="module_id_is_known"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    host_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        # Deleting a host removes its overrides: an override for a host that
        # does not exist can only ever be resolved against nothing.
        ForeignKey("hosts.id", ondelete="CASCADE"),
        nullable=False,
    )

    module_id: Mapped[str] = mapped_column(String(MAX_MODULE_ID_LENGTH), nullable=False)

    enabled: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, doc="True, false, or NULL to inherit the global default."
    )

    schedule: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, doc="A validated schedule document, or NULL to inherit."
    )

    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True, doc="Who last changed it. No foreign key (ADR-038)."
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        server_onupdate=FetchedValue(),
    )

    @property
    def is_empty(self) -> bool:
        """Whether this row overrides nothing.

        Legal, and identical in effect to having no row. The service deletes
        rather than empties on reset, so one of these should not arise from
        this codebase — it is here because the *schema* permits it and a
        reader of the table should not have to work out what such a row means.
        """
        return self.enabled is None and self.schedule is None

    @validates("module_id")
    def _validate_module_id(self, _key: str, value: str) -> str:
        if value not in MODULE_IDS:
            raise ValueError(
                f"unknown module {value!r}; this controller knows: {', '.join(MODULE_IDS)}"
            )
        return value

    def __repr__(self) -> str:
        return (
            f"HostModuleOverride(host_id={self.host_id!r}, module_id={self.module_id!r}, "
            f"enabled={self.enabled!r}, has_schedule={self.schedule is not None})"
        )


class HostConfigRevision(Base):
    """One immutable, numbered version of one host's effective configuration.

    The ``document`` column is ``text`` holding the **canonical bytes** the
    checksum covers and the agent received — not ``jsonb``, which reorders
    keys and normalises numbers and would therefore not reproduce the digest
    (ADR-050).
    """

    __tablename__ = "host_config_revisions"

    __table_args__ = (
        # Monotonic per host, enforced rather than hoped for.
        UniqueConstraint("host_id", "version", name="uq_host_config_revisions_host_version"),
        # "The newest revision for this host" is the hot read, on every
        # connection and every configuration save.
        Index("ix_host_config_revisions_host_id_version", "host_id", "version"),
        CheckConstraint("version >= 1", name="version_is_positive"),
        CheckConstraint(
            f"char_length(checksum) = {CHECKSUM_LENGTH}", name="checksum_is_prefixed_sha256"
        ),
        CheckConstraint(
            f"char_length(content_checksum) = {CHECKSUM_LENGTH}",
            name="content_checksum_is_prefixed_sha256",
        ),
        # A revision cannot be both acknowledged and rejected: they are the two
        # outcomes of one decision, and a row with both would make "what is
        # this agent running?" unanswerable.
        CheckConstraint(
            "NOT (acknowledged_at IS NOT NULL AND rejected_at IS NOT NULL)",
            name="not_both_acknowledged_and_rejected",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    host_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("hosts.id", ondelete="CASCADE"),
        nullable=False,
    )

    version: Mapped[int] = mapped_column(
        nullable=False,
        doc="Monotonic per host, starting at 1. Assigned under the per-host advisory lock.",
    )

    checksum: Mapped[str] = mapped_column(
        String(CHECKSUM_LENGTH),
        nullable=False,
        doc="`sha256:<hex>` over the UTF-8 bytes of `document`. What the agent acknowledges.",
    )

    content_checksum: Mapped[str] = mapped_column(
        String(CHECKSUM_LENGTH),
        nullable=False,
        doc=(
            "Digest over the configuration-dependent part of the document "
            "only — the modules and limits, without `version` or "
            "`generated_at`. Compared to decide whether a change warrants a "
            "new revision; a whole-document comparison would always differ, "
            "because `generated_at` always does, and every reconnection would "
            "mint a revision. Never sent to the agent."
        ),
    )

    document: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        doc=("The canonical JSON the agent received, byte for byte. Never updated after insert."),
    )

    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        doc=(
            "When the controller produced it. Also inside `document`, because "
            "a field the checksum does not cover can be altered undetectably."
        ),
    )

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        nullable=True,
        doc=(
            "Who caused it. NULL for a revision generated by the controller "
            "itself — the first one a host gets on connecting, which no "
            "operator asked for individually."
        ),
    )

    reason: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        doc=(
            "Why this revision exists: 'global default changed', 'host "
            "override set', 'initial'. Operator-facing history, not a code "
            "path — nothing branches on it."
        ),
    )

    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc=(
            "When the agent reported applying it, having verified the "
            "checksum. NULL means not yet — which is 'pending', not 'failed'."
        ),
    )

    rejected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc=(
            "When the agent refused it. A rejected revision stays rejected "
            "until an operator changes something: re-sending the same bad "
            "document on a timer turns one mistake into a log flood."
        ),
    )

    rejection_problems: Mapped[list[str] | None] = mapped_column(
        JSONB,
        nullable=True,
        doc=(
            "Every problem the agent reported, not the first. Bounded in "
            "count and length before storage: it is text from a host whose "
            "security the controller does not own."
        ),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        """Debug representation without the document.

        The document is several kilobytes and contains nothing secret, but a
        repr that prints it makes every log line holding one unreadable.
        """
        state = "pending"
        if self.acknowledged_at is not None:
            state = "acknowledged"
        elif self.rejected_at is not None:
            state = "rejected"
        return (
            f"HostConfigRevision(host_id={self.host_id!r}, version={self.version!r}, state={state})"
        )


#: Longest collector error this controller stores.
#:
#: The agent's protocol already bounds the field at 256 characters, so this is
#: the second of the two bounds rather than the only one — the protocol one
#: refuses the frame, this one is what the column can physically hold. Both,
#: because the column outlives any one version of the protocol model and a
#: reader of the schema should not have to go and find the other number.
MAX_COLLECTOR_ERROR_LENGTH: Final = 256


class HostModuleHealth(Base):
    """Whether one module's collector is actually working on one host.

    # The fourth reason a chart is empty

    ``GET /hosts/{id}/metrics/status`` could already tell "this module has
    collected nothing" from "this module does not exist"; the monitoring
    endpoint says whether collection is switched on, and the host endpoint
    whether the agent is connected. Between them they left one cause
    unreported and it is the interesting one: **collection is on, the agent is
    connected, and the collector is failing every interval.**

    The agent has always said so. :class:`hcc.agents.protocol.TelemetryEntry`
    carries an ``error`` field whose own description reads "a module that has
    been failing for an hour is information an operator needs" — and until
    this table the controller parsed it, validated it, and discarded it. A
    removed disk, a stopped Docker daemon or a systemd that stopped answering
    produced an enabled module with no data and no explanation, indefinitely.

    # Configuration and observation are different tables

    This is not a column on :class:`HostModuleOverride`. That row is what the
    operator *wants*; this one is what is *happening*. A module with no
    override row at all — inheriting the global default, which is the common
    case — still has health, and resetting an override deletes a row that
    must not take the failure history with it.

    # Success means the collection ran, not that it produced samples

    ``last_success_at`` advances whenever an entry arrives without an error,
    including one carrying no samples. That is not a loophole: a rate
    collector legitimately reports nothing on its first interval and on any
    interval where a counter went backwards, and calling those failures would
    make the first minute after every agent restart look broken. "Collected
    nothing" is already answerable from ``metric_series.last_sample_at``;
    this column answers "did the collector run".

    # The error survives recovery

    ``last_error`` and ``last_error_at`` are kept after a later success, and
    only ``consecutive_failures`` resets to zero. A module that fails forty
    times an hour and recovers between each one is a real condition, and a
    schema that cleared the evidence on every success could not express it.

    # Every timestamp here is the *controller's* clock

    Unlike ``metric_series.last_sample_at``, which records when the agent says
    it took a measurement. The difference is deliberate: a host whose clock is
    four hours out is one of the conditions this table exists to report, and a
    diagnostic timestamped by the thing being diagnosed is not a diagnostic.
    So these columns say when the controller *heard*, which it knows for
    certain, and the sample timestamps elsewhere say when the agent *claims*.
    """

    __tablename__ = "host_module_health"

    __table_args__ = (
        # One row per host per module. Ingest upserts on this, so without it
        # a batch would append rather than update and the newest row would be
        # whichever one a query happened to order first.
        UniqueConstraint("host_id", "module_id", name="uq_host_module_health_host_module"),
        Index("ix_host_module_health_host_id", "host_id"),
        CheckConstraint(f"module_id IN ({_MODULE_ID_SQL})", name="module_id_is_known"),
        CheckConstraint("consecutive_failures >= 0", name="failures_are_not_negative"),
        # An error string and no time for it, or the reverse, would be a row
        # the interface cannot render honestly: "failing, at some unknown
        # moment" and "failed at 14:03 for no stated reason" are both worse
        # than the absence this forbids them from becoming.
        CheckConstraint(
            "(last_error IS NULL) = (last_error_at IS NULL)",
            name="an_error_has_a_time_and_a_time_has_an_error",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    host_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("hosts.id", ondelete="CASCADE"),
        nullable=False,
    )

    module_id: Mapped[str] = mapped_column(String(MAX_MODULE_ID_LENGTH), nullable=False)

    last_reported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="When the controller last heard about this module, success or failure.",
    )

    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="When a collection last arrived without an error. See the class docstring.",
    )

    last_error_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, doc="When the last failure arrived."
    )

    last_error: Mapped[str | None] = mapped_column(
        String(MAX_COLLECTOR_ERROR_LENGTH),
        nullable=True,
        doc=(
            "Why the last collection produced nothing — usually the agent's "
            "own words, occasionally the controller's refusal of what it sent. "
            "Text from a host that may be compromised: bounded here, escaped "
            "by whatever renders it, never interpolated into anything that "
            "executes."
        ),
    )

    consecutive_failures: Mapped[int] = mapped_column(
        # Not nullable and defaulting to zero: "has never failed" and "failed
        # an unknown number of times" would otherwise look the same, and the
        # first is the overwhelmingly common case.
        nullable=False,
        server_default="0",
        default=0,
        type_=Integer,
        doc="Failures since the last success. Reset to zero by a success, never by time.",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        server_onupdate=FetchedValue(),
    )

    @validates("module_id")
    def _validate_module_id(self, _key: str, value: str) -> str:
        if value not in MODULE_IDS:
            raise ValueError(
                f"unknown module {value!r}; this controller knows: {', '.join(MODULE_IDS)}"
            )
        return value

    def __repr__(self) -> str:
        return (
            f"HostModuleHealth(host_id={self.host_id!r}, module_id={self.module_id!r}, "
            f"consecutive_failures={self.consecutive_failures!r})"
        )


#: Values :attr:`HostConfigRevision.reason` is set to by this codebase.
#:
#: Not a constraint: it is operator-facing prose and a future caller inventing
#: a more specific reason should not need a migration. Named so the existing
#: ones are discoverable and consistent.
REVISION_REASON_INITIAL: Final = "initial configuration"
REVISION_REASON_GLOBAL: Final = "global default changed"
REVISION_REASON_HOST: Final = "host override changed"
REVISION_REASON_RESET: Final = "host override reset to inherit"
REVISION_REASON_REGISTRY: Final = "module registry changed"

#: Every reason above, for the interface's filter and for tests that assert
#: the set has not silently grown.
REVISION_REASONS: Final = (
    REVISION_REASON_INITIAL,
    REVISION_REASON_GLOBAL,
    REVISION_REASON_HOST,
    REVISION_REASON_RESET,
    REVISION_REASON_REGISTRY,
)


class ConfigSyncStatus(enum.StrEnum):
    """Whether a host is running the configuration the controller intends.

    Five states, from ``AGENT_ARCHITECTURE.md`` §6.4 plus the one it does not
    name: a host with no configuration at all. Derived, never stored, for the
    same reason connectivity is (ADR-043) — a stored status is a status that
    stays wrong after the process that would have corrected it dies.
    """

    #: No revision has been generated. A registered-but-never-connected host.
    #: Distinct from ``up_to_date`` with an empty configuration, which is a
    #: host that *has* been told "collect nothing" and confirmed it.
    NONE = "none"
    #: The agent reports the newest revision, and the checksums agree.
    UP_TO_DATE = "up_to_date"
    #: A newer revision exists that the agent has not acknowledged yet. Not a
    #: problem on its own: an agent has up to one heartbeat interval to notice.
    PENDING = "pending"
    #: The agent refused the newest revision and said why. It is still running
    #: its last known good configuration, which is the correct behaviour — a
    #: monitoring agent that stops monitoring because someone typed a bad
    #: interval has failed at its job.
    REJECTED = "rejected"
    #: Pending for longer than the grace period. Shown as a problem rather
    #: than hidden: an agent silently running month-old configuration is
    #: exactly the failure a dashboard exists to reveal.
    STALE = "stale"


#: How long a revision may stay unacknowledged before it is reported stale.
#:
#: Four heartbeat intervals. Long enough that a reconnection, a missed
#: heartbeat and the round trip to fetch and apply a document all fit inside
#: it; short enough that an agent which has quietly stopped applying
#: configuration is visible within a couple of minutes.
CONFIG_ACK_GRACE_SECONDS: Final = 4 * 30


def derive_config_status(
    *,
    latest_version: int | None,
    latest_checksum: str | None,
    latest_generated_at: datetime | None,
    latest_acknowledged_at: datetime | None,
    latest_rejected_at: datetime | None,
    reported_version: int | None,
    reported_checksum: str | None,
    now: datetime,
    grace_seconds: int = CONFIG_ACK_GRACE_SECONDS,
) -> ConfigSyncStatus:
    """Compute synchronisation status from the stored facts.

    A pure function of its arguments, deliberately: it decides whether the
    interface claims a host's configuration is live, so it must be testable
    without a database, a socket or a clock.

    The order of the checks is the policy. Rejection beats everything, because
    an operator needs to know their change was refused before they are told it
    is merely late. Then the agreement test — **version and checksum
    together**, since a matching version with a different checksum means the
    agent is running something other than what the controller stored under
    that number, which is the one case a version comparison alone would call
    "up to date".
    """
    if latest_version is None:
        return ConfigSyncStatus.NONE
    if latest_rejected_at is not None:
        return ConfigSyncStatus.REJECTED
    if reported_version == latest_version and reported_checksum == latest_checksum:
        return ConfigSyncStatus.UP_TO_DATE
    # Unacknowledged. Whether that is merely pending or genuinely stale is
    # measured from when the revision was *generated*, not from the agent's
    # last heartbeat: a host that is offline has a pending configuration
    # because it is offline, and reporting that as stale as well would be two
    # alarms for one fact. Connectivity already says the host is not there.
    if latest_acknowledged_at is None and latest_generated_at is not None:
        age = (now - latest_generated_at).total_seconds()
        if age > grace_seconds:
            return ConfigSyncStatus.STALE
    return ConfigSyncStatus.PENDING


def bound_problems(problems: list[str] | tuple[str, ...]) -> list[str]:
    """Clamp agent-reported problems to what is safe to store.

    Both dimensions matter. An unbounded *count* lets one agent write
    megabytes into a JSONB column on every reconnection; an unbounded *length*
    lets it write one enormous string. Truncation is marked rather than silent,
    because a problem list that was cut off and does not say so sends an
    operator looking for a cause that was in the part discarded.
    """
    clamped = [problem[:MAX_PROBLEM_LENGTH] for problem in problems[:MAX_PROBLEMS]]
    remaining = len(problems) - MAX_PROBLEMS
    if remaining > 0:
        clamped.append(f"… and {remaining} further problem(s), not recorded")
    return clamped
