"""Update management: policies, jobs, events and rollouts.

Four tables, and the shape of each is decided by a question the product has to
be able to answer.

* :class:`HostUpdatePolicy` — "what is this host allowed to do by itself?" One
  row per host, at most. Absent means the default, and the default is manual.
* :class:`UpdateJob` — "what is happening to this host right now?" The durable
  state machine. At most one open job per host, enforced by a partial unique
  index rather than by hope.
* :class:`UpdateEvent` — "what happened, and when?" Append-only, one row per
  state transition. This is the history the interface shows and the audit of
  what the fleet actually did.
* :class:`UpdateRollout` and :class:`UpdateRolloutMember` — "how is this
  version reaching the fleet?" Canary, then batches.

## Why the state machine is in the database and not in memory

Because the controller restarts. A scheduler holding rollout state in a
dictionary loses a fleet-wide operation on every deployment, and the hosts do
not know that — they have an instruction with an expiry and will act on it.
Recovering from "some unknown subset of forty hosts was told to upgrade" is not
a recovery procedure anybody can write.

So every transition is a row, the scheduler reconciles from the table on start,
and the worst a restart costs is one reconciliation pass. ADR-046 already
states this controller is one process; that makes the table the only shared
memory there is, not a cache in front of something else.

## Why `UpdateEvent` is append-only and `UpdateJob` is not

They answer different questions. "What is the state now" wants one mutable row
— a job that recorded its state only as events would need a replay to answer
the most common question in the system. "What happened" wants immutable rows,
because an update that failed, was retried and succeeded is three facts and the
middle one is the interesting one.

Keeping both is the standard trade: a little denormalisation against a replay
on every page load. The job's ``state`` is derivable from its events, and a
test asserts they agree.

## One open job per host, in the schema

``ix_update_jobs_one_open_per_host`` is a partial unique index over
``host_id`` where the state is non-terminal. Two administrators clicking
"update" at the same moment, or a scheduler racing a manual action, is not a
rare case in a fleet tool — it is Tuesday. Enforcing it in the service layer
alone means the race is lost silently and a host receives two instructions;
enforcing it in the schema means the second transaction gets an integrity error
the service turns into "an update is already running".
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    FetchedValue,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hcc.db import Base

#: Longest version string stored. Matches ``AGENT_VERSION_MAX_LENGTH`` in the
#: host model, because these columns hold the same kind of value and a
#: narrower one here would refuse a version the host already reported.
VERSION_MAX_LENGTH: Final = 64

#: Longest release channel name. The set is closed (`stable`, `beta`, `alpha`)
#: and the column is checked against it, so this is generous on purpose.
CHANNEL_MAX_LENGTH: Final = 16

#: Longest IANA time zone name. "America/Argentina/ComodRivadavia" is 31.
TIMEZONE_MAX_LENGTH: Final = 64

#: How much of a failure is kept. Long enough for a stack-free explanation and
#: a command to run; short enough that a pathological error cannot make the
#: table the largest thing in the database.
ERROR_MAX_LENGTH: Final = 2000

#: Longest rollout name an administrator can give.
ROLLOUT_NAME_MAX_LENGTH: Final = 120


class UpdateState(enum.StrEnum):
    """Where one update attempt has got to.

    The order of declaration is the order of progress, which is used by
    :data:`TERMINAL_STATES` and by the interface's progress rendering. It is
    not relied on for comparisons — a state is a name, not a number, because a
    numeric state that could be compared would invite ``state > INSTALLING``
    and that is a question with no correct answer once rollback exists.
    """

    #: An update exists and nothing has been asked of the host yet.
    AVAILABLE = "available"
    #: The controller has decided this host should update.
    QUEUED = "queued"
    #: The agent is fetching the archive.
    DOWNLOADING = "downloading"
    #: The agent is checking the signature and the digest.
    VERIFYING = "verifying"
    #: Verified and unpacked, not yet installed.
    STAGED = "staged"
    #: The binary is being replaced.
    INSTALLING = "installing"
    #: The service is restarting.
    RESTARTING = "restarting"
    #: Restarted, waiting for it to prove it works.
    VERIFYING_HEALTH = "verifying_health"
    #: Done, and the host is running the new version.
    SUCCEEDED = "succeeded"
    #: Not done. ``error`` says why.
    FAILED = "failed"
    #: Failed, and the previous binary is back in service.
    ROLLED_BACK = "rolled_back"
    #: An administrator stopped it.
    CANCELLED = "cancelled"
    #: Outside its maintenance window, or the host was offline too long. **Not
    #: a failure** — conflating the two makes a healthy fleet waiting for
    #: Sunday look broken.
    DEFERRED = "deferred"


#: States from which nothing further will happen on its own.
#:
#: ``DEFERRED`` is terminal for *this job*: the scheduler creates a new one
#: when the window opens rather than reviving an old decision, because an
#: instruction that has been sitting for a week should be re-planned against
#: current metadata rather than replayed.
TERMINAL_STATES: Final = frozenset(
    {
        UpdateState.SUCCEEDED,
        UpdateState.FAILED,
        UpdateState.ROLLED_BACK,
        UpdateState.CANCELLED,
        UpdateState.DEFERRED,
    }
)

#: States in which the host has been told to do something and has not finished.
#:
#: The distinction that matters for cancellation: a ``QUEUED`` job can be
#: cancelled cleanly because nothing has happened yet, while a ``RESTARTING``
#: one cannot be un-started and cancelling it only stops the controller from
#: waiting.
IN_FLIGHT_STATES: Final = frozenset(
    {
        UpdateState.QUEUED,
        UpdateState.DOWNLOADING,
        UpdateState.VERIFYING,
        UpdateState.STAGED,
        UpdateState.INSTALLING,
        UpdateState.RESTARTING,
        UpdateState.VERIFYING_HEALTH,
    }
)

#: Every legal transition. The authority on what the state machine may do.
#:
#: A table rather than a chain of ``if`` statements in the service, because the
#: interesting property — "can a job go from RESTARTING back to STAGED" — is a
#: lookup here and would otherwise be a reading exercise. Tested exhaustively.
#:
#: Note what is *absent*: nothing leaves a terminal state. A host that wants to
#: try again gets a new job, so the history keeps both attempts instead of one
#: row that changed its mind.
ALLOWED_TRANSITIONS: Final[dict[UpdateState, frozenset[UpdateState]]] = {
    UpdateState.AVAILABLE: frozenset(
        {UpdateState.QUEUED, UpdateState.CANCELLED, UpdateState.DEFERRED}
    ),
    UpdateState.QUEUED: frozenset(
        {
            UpdateState.DOWNLOADING,
            UpdateState.CANCELLED,
            UpdateState.DEFERRED,
            UpdateState.FAILED,
        }
    ),
    UpdateState.DOWNLOADING: frozenset(
        {UpdateState.VERIFYING, UpdateState.FAILED, UpdateState.CANCELLED}
    ),
    UpdateState.VERIFYING: frozenset(
        {UpdateState.STAGED, UpdateState.FAILED, UpdateState.CANCELLED}
    ),
    UpdateState.STAGED: frozenset(
        {UpdateState.INSTALLING, UpdateState.FAILED, UpdateState.CANCELLED}
    ),
    # From INSTALLING onwards, cancellation is no longer offered: the binary is
    # being replaced, and "cancel" would be a promise the controller cannot
    # keep. The agent's own rollback is what recovers from here.
    UpdateState.INSTALLING: frozenset(
        {UpdateState.RESTARTING, UpdateState.FAILED, UpdateState.ROLLED_BACK}
    ),
    # RESTARTING may go straight to SUCCEEDED, and that is not a shortcut.
    #
    # In the controller-driven path the updater runs *inside the service
    # process*, so `systemctl restart` terminates the thing performing the
    # install. The agent therefore reports `restarting` and then dies; it
    # cannot report `verifying_health`, because there is no process left to
    # report it. The controller concludes the job when the replacement
    # connects — and that reconnection, with the target version, **is** the
    # health evidence. See UpdateService.reconcile_on_connect.
    #
    # VERIFYING_HEALTH is still reachable, from the operator-driven path:
    # `hcc-agent upgrade` runs in a separate process from the service, so it
    # survives the restart and waits for the agent to come back itself.
    UpdateState.RESTARTING: frozenset(
        {
            UpdateState.VERIFYING_HEALTH,
            UpdateState.SUCCEEDED,
            UpdateState.FAILED,
            UpdateState.ROLLED_BACK,
        }
    ),
    UpdateState.VERIFYING_HEALTH: frozenset(
        {UpdateState.SUCCEEDED, UpdateState.FAILED, UpdateState.ROLLED_BACK}
    ),
    UpdateState.SUCCEEDED: frozenset(),
    UpdateState.FAILED: frozenset(),
    UpdateState.ROLLED_BACK: frozenset(),
    UpdateState.CANCELLED: frozenset(),
    UpdateState.DEFERRED: frozenset(),
}


class UpdateTrigger(enum.StrEnum):
    """What caused an update attempt.

    Stored because "who did this" is the first question asked when a fleet
    does something unexpected, and "the policy did" and "an administrator did"
    lead to different places. The actor's user id is stored alongside for the
    manual cases; a policy has no user and the column is null, which is itself
    the answer.
    """

    #: An administrator asked for this host specifically.
    MANUAL = "manual"
    #: An automatic policy decided.
    POLICY = "policy"
    #: Part of a rollout.
    ROLLOUT = "rollout"


class RolloutState(enum.StrEnum):
    """Where a staged rollout has got to."""

    #: Created, nothing started.
    PENDING = "pending"
    #: The canary is updating.
    CANARY = "canary"
    #: Batches are progressing.
    RUNNING = "running"
    #: Stopped by an administrator or by the failure tolerance. Resumable.
    PAUSED = "paused"
    #: Every member reached a terminal state.
    COMPLETED = "completed"
    #: Abandoned.
    CANCELLED = "cancelled"


TERMINAL_ROLLOUT_STATES: Final = frozenset({RolloutState.COMPLETED, RolloutState.CANCELLED})


class HostUpdatePolicy(Base):
    """What one host is allowed to do by itself.

    One row per host, at most. **No row means the default**, and the default is
    manual with no window — so a host that has never been configured updates
    only when an administrator says so. That is the single most important
    property in this file: the safe behaviour is the one that needs no row.
    """

    __tablename__ = "host_update_policies"

    host_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("hosts.id", ondelete="CASCADE"),
        primary_key=True,
        doc="The host. Primary key, so one policy per host is structural.",
    )

    policy: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=text("'manual'"),
        doc="manual | notify | patch | minor | pinned. See hcc.updates.policy.",
    )

    channel: Mapped[str] = mapped_column(
        String(CHANNEL_MAX_LENGTH),
        nullable=False,
        server_default=text("'stable'"),
        doc="The most instability this host accepts: stable | beta | alpha.",
    )

    pinned_version: Mapped[str | None] = mapped_column(
        String(VERSION_MAX_LENGTH),
        nullable=True,
        doc=(
            "Hold the host at exactly this version. Null unless the policy is "
            "'pinned' — and the check constraint enforces the pairing, because a "
            "pinned policy with no version is a host held at nothing."
        ),
    )

    window_timezone: Mapped[str | None] = mapped_column(
        String(TIMEZONE_MAX_LENGTH),
        nullable=True,
        doc="IANA zone for the maintenance window. Null means no window.",
    )

    window_weekdays: Mapped[list[int] | None] = mapped_column(
        JSONB,
        nullable=True,
        doc="0 = Sunday … 6 = Saturday. Null or empty means every day.",
    )

    window_start_minute: Mapped[int | None] = mapped_column(
        SmallInteger,
        nullable=True,
        doc=(
            "Minutes after local midnight, 0-1439. Minutes rather than a TIME "
            "column because the value is wall-clock in another zone, and a TIME "
            "invites a driver or a query to attach an offset to it."
        ),
    )

    window_end_minute: Mapped[int | None] = mapped_column(
        SmallInteger,
        nullable=True,
        doc="Minutes after local midnight, 0-1439. Equal to start means all day.",
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

    __table_args__ = (
        CheckConstraint(
            "policy IN ('manual', 'notify', 'patch', 'minor', 'pinned')",
            name="policy_is_known",
        ),
        CheckConstraint(
            "channel IN ('stable', 'beta', 'alpha')",
            name="channel_is_known",
        ),
        # A pinned policy with no version is a host held at nothing, and a
        # version with another policy is a value that silently does nothing.
        # Both are confusing in the interface and both are cheap to forbid.
        CheckConstraint(
            "(policy = 'pinned') = (pinned_version IS NOT NULL)",
            name="pin_and_policy_agree",
        ),
        CheckConstraint(
            "window_start_minute IS NULL OR window_start_minute BETWEEN 0 AND 1439",
            name="window_start_in_range",
        ),
        CheckConstraint(
            "window_end_minute IS NULL OR window_end_minute BETWEEN 0 AND 1439",
            name="window_end_in_range",
        ),
        # A window is all-or-nothing: a zone with no times, or times with no
        # zone, is a half-configured window that would be evaluated in a zone
        # nobody chose.
        CheckConstraint(
            "(window_timezone IS NULL) = (window_start_minute IS NULL) "
            "AND (window_start_minute IS NULL) = (window_end_minute IS NULL)",
            name="window_is_all_or_nothing",
        ),
    )


class UpdateJob(Base):
    """One update attempt for one host.

    The durable state machine. Created when the controller decides a host
    should move, and never reused — a retry is a new job, so the history keeps
    both attempts rather than one row that changed its mind.
    """

    __tablename__ = "update_jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    host_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("hosts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    #: The identifier the agent sees, and the replay-protection token.
    #:
    #: Separate from ``id`` deliberately. The primary key is an internal
    #: reference that appears in URLs and foreign keys; this is what travels
    #: over the socket and what the agent records as "the last instruction I
    #: acted on". Keeping them separate means an instruction identifier can be
    #: rotated — if an instruction is re-issued after an expiry, say — without
    #: breaking every row that references the job.
    instruction_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, unique=True, default=uuid.uuid4
    )

    state: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        server_default=text("'available'"),
        doc="See UpdateState. The current state; the events table has the path.",
    )

    trigger: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        doc="manual | policy | rollout. Why this job exists.",
    )

    #: Who asked, for a manual job. Null for a policy or rollout decision,
    #: and the null is the answer rather than missing data.
    #:
    #: ``ON DELETE SET NULL``: deleting a user must not delete the history of
    #: what their fleet did, and must not be blocked by it either.
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    rollout_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("update_rollouts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    from_version: Mapped[str | None] = mapped_column(
        String(VERSION_MAX_LENGTH),
        nullable=True,
        doc="What the host reported before the update. Null if it reported nothing.",
    )

    to_version: Mapped[str] = mapped_column(
        String(VERSION_MAX_LENGTH), nullable=False, doc="The target version."
    )

    platform: Mapped[str] = mapped_column(
        String(32), nullable=False, doc="GOOS/GOARCH the instruction was built for."
    )

    #: The digest from the *signed* metadata, recorded so the history can show
    #: what was actually authorised rather than what the download served.
    expected_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    #: When the instruction stops being valid.
    #:
    #: An agent offline for a day must not wake up and install last week's
    #: decision. Enforced by the agent as well, which is the half that matters
    #: — the controller cannot reach a disconnected host to withdraw anything.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    error: Mapped[str | None] = mapped_column(
        Text, nullable=True, doc="Why it failed, for a human. Null unless it did."
    )

    #: What the host reported running after the attempt.
    #:
    #: Recorded separately from ``to_version`` because they can differ, and the
    #: difference is the interesting case: a job that reports SUCCEEDED while
    #: the host runs the old version is a rollback nobody noticed.
    resulting_version: Mapped[str | None] = mapped_column(String(VERSION_MAX_LENGTH), nullable=True)

    attempt: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("1"),
        doc="Which try this is, for backoff and for the retry limit.",
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
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="When the host began work, as opposed to when the job was created.",
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, doc="When it reached a terminal state."
    )

    __table_args__ = (
        CheckConstraint(
            "state IN ('available', 'queued', 'downloading', 'verifying', 'staged', "
            "'installing', 'restarting', 'verifying_health', 'succeeded', 'failed', "
            "'rolled_back', 'cancelled', 'deferred')",
            name="state_is_known",
        ),
        CheckConstraint("trigger IN ('manual', 'policy', 'rollout')", name="trigger_is_known"),
        CheckConstraint("attempt >= 1", name="attempt_is_positive"),
        CheckConstraint("char_length(expected_sha256) = 64", name="digest_is_sha256"),
        # At most one open job per host, in the schema.
        #
        # Two administrators clicking "update" at the same moment, or a
        # scheduler racing a manual action, is not a rare case in a fleet tool.
        # Enforcing this only in the service means the race is lost silently
        # and a host receives two instructions; here, the second transaction
        # gets an integrity error the service turns into a clear refusal.
        Index(
            "ix_update_jobs_one_open_per_host",
            "host_id",
            unique=True,
            postgresql_where=text(
                "state NOT IN ('succeeded', 'failed', 'rolled_back', 'cancelled', 'deferred')"
            ),
        ),
        # The history view's query: a host's attempts, newest first.
        Index("ix_update_jobs_host_created", "host_id", "created_at"),
    )


class UpdateEvent(Base):
    """One state transition, append-only.

    This is the history. A job's ``state`` says where it is; these rows say how
    it got there, which is what somebody debugging a failed fleet update
    actually needs — "it reached STAGED and then failed" and "it never got past
    QUEUED" are different problems with the same final state.

    Never updated after insert. An event that could be rewritten is not a
    history.
    """

    __tablename__ = "update_events"

    #: A database-assigned sequence, and the ordering of this log.
    #:
    #: Not a UUID, and it is the one non-obvious choice in this table. An
    #: append-only log has to be *orderable*, and ``created_at`` cannot do it:
    #: PostgreSQL's ``now()`` is the **transaction** timestamp, so every event
    #: written in one transaction shares it — and ``start_update`` writes two.
    #: Ordering by ``(created_at, uuid)`` is then ordering by a random number,
    #: which showed a job's history backwards about half the time.
    #:
    #: The audit log keeps a UUID and documents the transaction timestamp as
    #: correct *there*, which it is: one audit event per mutation, so there is
    #: nothing to tie-break. This table is the other shape.
    #:
    #: ``Identity(always=True)``, so the sequence is the database's and the
    #: application cannot supply one — an event log whose order a caller could
    #: choose is not a log.
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)

    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("update_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    #: Denormalised from the job so the fleet-wide history view does not need a
    #: join, and so a host's history survives in the shape the interface wants.
    #: The job already cascades from the host, so this adds no lifetime
    #: question — it is the same row's host, stored twice on purpose.
    host_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("hosts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    state: Mapped[str] = mapped_column(String(24), nullable=False, doc="The state entered.")

    detail: Mapped[str | None] = mapped_column(
        Text, nullable=True, doc="Why, when the transition has a reason worth keeping."
    )

    #: Where this transition was decided.
    #:
    #: 'controller' or 'agent'. Worth storing: an agent reporting FAILED and a
    #: controller marking a job FAILED because the host went silent are
    #: different events with the same name, and only one of them means the
    #: update was attempted.
    source: Mapped[str] = mapped_column(String(16), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    __table_args__ = (
        CheckConstraint("source IN ('controller', 'agent')", name="source_is_known"),
        # No descending index beside the ascending one `index=True` puts on
        # `created_at`. PostgreSQL scans a btree in either direction, so a
        # single-column DESC index serves no query the plain one cannot — and
        # it would cost a second write on every state transition of every
        # host, which is the most frequent insert this feature makes.
    )


class UpdateRollout(Base):
    """One version reaching a set of hosts, in stages.

    A canary first, then batches. The point is that a bad release stops after
    one host rather than after forty, so the canary's health check gating the
    first batch is the whole feature and not a refinement of it.
    """

    __tablename__ = "update_rollouts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    name: Mapped[str] = mapped_column(
        String(ROLLOUT_NAME_MAX_LENGTH),
        nullable=False,
        doc="What an administrator called it, for finding it again.",
    )

    to_version: Mapped[str] = mapped_column(String(VERSION_MAX_LENGTH), nullable=False)

    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'pending'"))

    batch_size: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("5"),
        doc="How many hosts update at once after the canary.",
    )

    failure_tolerance: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
        doc=(
            "How many failures are accepted before the rollout pauses itself. "
            "Zero by default: the first failure stops it, which is what somebody "
            "rolling out to a fleet they care about wants."
        ),
    )

    #: How long a host may stay offline before it is skipped.
    #:
    #: Without this a single powered-off NAS holds a rollout open forever, and
    #: a rollout that cannot finish is worse than one that reports what it
    #: skipped.
    offline_deadline_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("3600")
    )

    canary_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("1"),
        doc=(
            "How many hosts are in batch 0. One by default, which is what "
            "'canary' means; more is for a fleet where one host is not "
            "representative of the rest."
        ),
    )

    #: How long a host must stay healthy *after* its install before the next
    #: batch may start.
    #:
    #: This is the column that stops "the process restarted" meaning "the
    #: upgrade succeeded". The job concludes when the agent reconnects
    #: reporting the target version, which is a real check and a narrow one:
    #: an agent that comes up, reports its version and then crashes a minute
    #: later has satisfied it. The rollout waits to see whether the host is
    #: still there, still acknowledging its configuration, and still
    #: delivering telemetry — and only then starts the next batch.
    #:
    #: Zero disables the wait, which is for a test and for an operator who
    #: knows what they are doing.
    health_check_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("120")
    )

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, doc="When the first batch began."
    )

    #: A maintenance window for the rollout as a whole, in the same shape
    #: `host_update_policies` uses.
    #:
    #: A rollout and a host policy can each have one, and they are different
    #: statements: "this fleet change happens at night" and "this host is only
    #: interruptible at night". Both are honoured; the host's is checked by
    #: `decide`, this one by the rollout engine.
    window_timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    window_weekdays: Mapped[list[int] | None] = mapped_column(ARRAY(SmallInteger), nullable=True)
    window_start_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    window_end_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: An administrator said "go on anyway" after the failure gate stopped it.
    #:
    #: Recorded rather than inferred from the state, because "resumed after a
    #: failure" and "resumed after a pause" are different facts and the second
    #: must not look like the first in an audit trail.
    failures_overridden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    paused_reason: Mapped[str | None] = mapped_column(
        Text, nullable=True, doc="Why it stopped, when it stopped itself."
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
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'canary', 'running', 'paused', 'completed', 'cancelled')",
            name="state_is_known",
        ),
        CheckConstraint("batch_size >= 1", name="batch_size_is_positive"),
        CheckConstraint("canary_count >= 1", name="canary_count_is_positive"),
        CheckConstraint("health_check_seconds >= 0", name="health_check_is_sane"),
        CheckConstraint(
            "window_start_minute IS NULL OR window_start_minute BETWEEN 0 AND 1439",
            name="window_start_in_range",
        ),
        CheckConstraint(
            "window_end_minute IS NULL OR window_end_minute BETWEEN 0 AND 1439",
            name="window_end_in_range",
        ),
        CheckConstraint("failure_tolerance >= 0", name="failure_tolerance_is_not_negative"),
        CheckConstraint(
            "offline_deadline_seconds >= 60",
            name="offline_deadline_is_sane",
        ),
        Index("ix_update_rollouts_state", "state"),
    )


class UpdateRolloutMember(Base):
    """One host's place in a rollout.

    Batch 0 is the canary, by convention and by the check constraint's
    permissiveness rather than by a separate column — a boolean ``is_canary``
    alongside a batch index would allow "canary in batch 3", which means
    nothing.
    """

    __tablename__ = "update_rollout_members"

    rollout_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("update_rollouts.id", ondelete="CASCADE"),
        primary_key=True,
    )

    host_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("hosts.id", ondelete="CASCADE"),
        primary_key=True,
    )

    batch: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        doc="0 is the canary; 1 and up are the batches, in order.",
    )

    #: The job created for this member, once one has been.
    #:
    #: Null until the batch starts, which is how "not yet attempted" is
    #: represented without a second state column that could disagree with the
    #: job's own.
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("update_jobs.id", ondelete="SET NULL"), nullable=True
    )

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        doc="When this member's batch began. The offline deadline runs from here.",
    )

    #: A host that was offline past the deadline, or that could not be
    #: started for a reason that is not a failure.
    #:
    #: Skipped is a **third outcome** beside succeeded and failed, and it does
    #: not count against the failure tolerance: a powered-off NAS is not a bad
    #: release, and treating it as one would stop a rollout for a reason that
    #: says nothing about the version being deployed.
    skipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    skipped_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: When the post-install health check passed.
    #:
    #: Null while a member is in flight *and* null for a member that
    #: installed and then went away — which is the distinction the canary gate
    #: turns on. See [UpdateRollout.health_check_seconds].
    healthy_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("batch >= 0", name="batch_is_not_negative"),
        Index("ix_update_rollout_members_rollout_batch", "rollout_id", "batch"),
    )


def is_terminal(state: str) -> bool:
    """Whether *state* is one nothing further happens from.

    Takes a string because that is what the column holds, and converting at
    every call site is how a comparison against the wrong type gets written.
    An unknown state is reported as non-terminal: the safe answer, since
    treating something unrecognised as finished would abandon a job.
    """
    try:
        return UpdateState(state) in TERMINAL_STATES
    except ValueError:
        return False


def can_transition(current: str, target: str) -> bool:
    """Whether a job may move from *current* to *target*.

    The single authority, consulted by the service and asserted exhaustively by
    the tests. An unknown state on either side is a refusal rather than an
    exception, because this is called with values read from the database and a
    row somebody edited by hand should not crash the scheduler.
    """
    try:
        return UpdateState(target) in ALLOWED_TRANSITIONS[UpdateState(current)]
    except (ValueError, KeyError):
        return False
