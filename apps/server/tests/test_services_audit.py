"""Audit model and cursor unit tests.

Milestone 1.7. No database: these cover the pieces that are pure logic — the
keyset cursor's encoding, the model's assignment-time validation, and the
vocabulary's shape.

The keyset cursor is worth unit-testing rather than only exercising through
the endpoint because its failure mode is quiet. A cursor that round-trips
*almost* correctly produces a paginated list that silently skips or repeats
rows, which looks like missing data rather than like a bug.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from hcc.models.audit import (
    ACTION_MAX_LENGTH,
    AuditAction,
    AuditEvent,
    AuditOutcome,
)
from hcc.services.audit import (
    DEFAULT_PAGE_SIZE,
    MAX_CURSOR_LENGTH,
    MAX_PAGE_SIZE,
    AuditCursor,
    InvalidCursorError,
)


class TestCursor:
    def test_round_trips(self) -> None:
        cursor = AuditCursor(
            occurred_at=datetime(2026, 10, 12, 17, 30, 45, 123456, tzinfo=UTC),
            event_id=uuid.UUID("0f3c1a2b-0000-4000-8000-000000000001"),
        )

        decoded = AuditCursor.decode(cursor.encode())

        assert decoded == cursor

    def test_microseconds_survive(self) -> None:
        # `occurred_at` is the transaction timestamp, so two events in one
        # request share it to the microsecond. Losing precision here would
        # make the boundary between pages ambiguous — which is the thing the
        # composite key exists to prevent.
        cursor = AuditCursor(
            occurred_at=datetime(2026, 1, 1, 0, 0, 0, 1, tzinfo=UTC),
            event_id=uuid.uuid4(),
        )

        assert AuditCursor.decode(cursor.encode()).occurred_at.microsecond == 1

    def test_the_timezone_survives(self) -> None:
        cursor = AuditCursor(
            occurred_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC), event_id=uuid.uuid4()
        )

        assert AuditCursor.decode(cursor.encode()).occurred_at.tzinfo is not None

    def test_the_token_is_url_safe(self) -> None:
        # A `+` from standard base64 becomes a space when a query parameter is
        # decoded, which breaks only *some* cursors — the kind of bug found in
        # production rather than in a test.
        for _ in range(50):
            token = AuditCursor(occurred_at=datetime.now(UTC), event_id=uuid.uuid4()).encode()
            assert "+" not in token
            assert "/" not in token
            assert "=" not in token

    @pytest.mark.parametrize(
        "token",
        [
            "",
            "not-base64!!",
            # Valid base64, no separator.
            "YWJjZGVm",
            # Separator present, unparseable timestamp.
            "bm90LWEtdGltZXN0YW1wfDBmM2MxYTJiLTAwMDAtNDAwMC04MDAwLTAwMDAwMDAwMDAwMQ",
            # Valid timestamp, unparseable uuid.
            "MjAyNi0wMS0wMVQwMDowMDowMCswMDowMHxub3QtYS11dWlk",
        ],
    )
    def test_a_malformed_token_is_refused(self, token: str) -> None:
        with pytest.raises(InvalidCursorError):
            AuditCursor.decode(token)

    def test_an_over_long_token_is_refused_before_decoding(self) -> None:
        # Bounded before any work: a cursor is a fixed shape, so anything much
        # longer is either corrupt or an attempt to make the decoder chew.
        with pytest.raises(InvalidCursorError):
            AuditCursor.decode("A" * (MAX_CURSOR_LENGTH + 1))

    def test_the_error_message_does_not_describe_the_format(self) -> None:
        # The encoding is opaque by convention; the message should say what to
        # do, not how the token is built.
        with pytest.raises(InvalidCursorError) as caught:
            AuditCursor.decode("garbage")

        message = str(caught.value)
        assert "next_cursor" in message
        assert "base64" not in message.lower()

    def test_page_bounds_are_sane(self) -> None:
        assert 0 < DEFAULT_PAGE_SIZE <= MAX_PAGE_SIZE


class TestAuditEventModel:
    def test_the_table_is_registered_for_autogenerate(self) -> None:
        from hcc.db import Base

        # If this fails, hcc/models/__init__.py no longer imports the module
        # and Alembic autogenerate would emit a drop for the table.
        assert "audit_events" in Base.metadata.tables

    def test_an_unknown_outcome_is_refused_at_assignment(self) -> None:
        # The CHECK constraint is the guarantee; the validator makes the
        # failure happen where the value was set, with a readable message,
        # rather than as a driver error at flush.
        with pytest.raises(ValueError, match="invalid outcome"):
            AuditEvent(action=AuditAction.USER_CREATED.value, outcome="maybe")

    @pytest.mark.parametrize("outcome", list(AuditOutcome))
    def test_every_declared_outcome_is_accepted(self, outcome: AuditOutcome) -> None:
        event = AuditEvent(action=AuditAction.USER_CREATED.value, outcome=outcome.value)
        assert event.outcome == outcome.value

    def test_an_empty_action_is_refused(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            AuditEvent(action="   ", outcome=AuditOutcome.SUCCESS.value)

    def test_an_over_long_action_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at most"):
            AuditEvent(action="x" * (ACTION_MAX_LENGTH + 1), outcome=AuditOutcome.SUCCESS.value)

    def test_the_repr_omits_details(self) -> None:
        """``details`` is the one open-ended field, so it stays out of ``repr``.

        It is constructed from named fields today, so there is nothing
        sensitive in it — but a ``repr`` is exactly what reaches a log line or
        a traceback, and omitting it means a future careless caller cannot
        leak through this path.
        """
        event = AuditEvent(
            action=AuditAction.USER_CREATED.value,
            outcome=AuditOutcome.SUCCESS.value,
            details={"secret-ish": "value"},
        )

        rendered = repr(event)

        assert "secret-ish" not in rendered
        assert "user.created" in rendered

    def test_every_action_is_dotted_and_bounded(self) -> None:
        # The convention a reader and a prefix filter both rely on.
        for action in AuditAction:
            assert "." in action.value, action
            assert action.value == action.value.lower(), action
            assert len(action.value) <= ACTION_MAX_LENGTH, action

    def test_the_vocabulary_covers_every_relevant_event(self) -> None:
        """Pin the action set, so adding one is a deliberate change.

        The column is a plain string precisely so the vocabulary can grow
        without a migration (ADR-038) — which means nothing in the schema
        constrains it, and this is the only place a new action gets reviewed.
        """
        assert {action.value for action in AuditAction} == {
            "user.created",
            "user.updated",
            "user.role_changed",
            "user.deactivated",
            "user.reactivated",
            "user.sessions_revoked",
            "account.password_changed",
            "account.sessions_revoked",
            "admin.access_denied",
            # Milestone 1.8. Note what is absent: there is no
            # `host.agent_connected`. A flapping link would write two rows a
            # second and bury every administrative event — see
            # `hcc/models/audit.py`.
            "host.created",
            "host.updated",
            "host.disabled",
            "host.enabled",
            "host.enrollment_issued",
            "host.enrollment_revoked",
            "host.enrolled",
            "host.credential_revoked",
            "host.agent_auth_failed",
            # Phase 2. Enabling a module is the act that starts collecting
            # information about somebody's machine, so it is the act worth
            # recording. Absent, for the same reason `host.agent_connected`
            # is: no `monitoring.config_acknowledged`. An agent applying a
            # configuration happens on every reconnection, is not somebody
            # doing something, and is already durable on the revision row.
            "monitoring.global_default_changed",
            "monitoring.host_override_changed",
            "monitoring.host_override_reset",
            "monitoring.config_rejected",
            # Operation Evergreen. Every one of these is a decision about
            # running different code on somebody's machine, which makes them
            # the highest-consequence actions in the product. Note what is
            # absent: there is no `update.progress` or `update.state_changed`.
            # A fleet updating would write a row per host per transition and
            # bury every administrative event — the same reasoning that keeps
            # `host.agent_connected` out. Those live in `update_events`, which
            # is the right table for them.
            "update.policy_changed",
            "update.started",
            "update.cancelled",
            "update.downgrade_authorized",
            "update.rollout_created",
            "update.rollout_controlled",
        }
