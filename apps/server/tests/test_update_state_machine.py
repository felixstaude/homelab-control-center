"""The update state machine, asserted as a whole rather than by example.

`ALLOWED_TRANSITIONS` is the authority on what a job may do, and the properties
worth checking are global ones: that nothing escapes a terminal state, that
every state is reachable, that no state is a dead end unless it is meant to be,
and that cancellation stops being offered at exactly the point the controller
can no longer keep that promise.

Those are not things an example-based test finds. A transition table with one
wrong entry passes every test written about the paths somebody thought of.
"""

from __future__ import annotations

import itertools

import pytest

from hcc.models.updates import (
    ALLOWED_TRANSITIONS,
    IN_FLIGHT_STATES,
    TERMINAL_ROLLOUT_STATES,
    TERMINAL_STATES,
    RolloutState,
    UpdateState,
    UpdateTrigger,
    can_transition,
    is_terminal,
)

#: The happy path, start to finish. Named because it is the sequence the
#: interface renders as progress, and a table that could not express it would
#: be wrong whatever else it allowed.
HAPPY_PATH = (
    UpdateState.AVAILABLE,
    UpdateState.QUEUED,
    UpdateState.DOWNLOADING,
    UpdateState.VERIFYING,
    UpdateState.STAGED,
    UpdateState.INSTALLING,
    UpdateState.RESTARTING,
    UpdateState.VERIFYING_HEALTH,
    UpdateState.SUCCEEDED,
)


class TestTheTableIsComplete:
    def test_every_state_has_an_entry(self) -> None:
        # A missing key makes `can_transition` refuse everything from that
        # state, which would strand a job silently.
        assert set(ALLOWED_TRANSITIONS) == set(UpdateState)

    def test_every_target_is_a_real_state(self) -> None:
        for source, targets in ALLOWED_TRANSITIONS.items():
            for target in targets:
                assert isinstance(target, UpdateState), f"{source} -> {target}"

    def test_no_state_transitions_to_itself(self) -> None:
        # A self-transition would let a job record the same state twice and
        # make the event history say something happened when nothing did.
        for state, targets in ALLOWED_TRANSITIONS.items():
            assert state not in targets

    def test_every_state_is_reachable_from_available(self) -> None:
        # An unreachable state is either dead code or a state the agent can
        # report and the controller will refuse — and the second is worse,
        # because it looks like the agent is misbehaving.
        reached = {UpdateState.AVAILABLE}
        frontier = [UpdateState.AVAILABLE]
        while frontier:
            current = frontier.pop()
            for target in ALLOWED_TRANSITIONS[current]:
                if target not in reached:
                    reached.add(target)
                    frontier.append(target)

        assert reached == set(UpdateState)

    def test_the_happy_path_is_expressible(self) -> None:
        for current, target in itertools.pairwise(HAPPY_PATH):
            assert can_transition(current, target), f"{current} -> {target}"


class TestTerminalStates:
    def test_nothing_leaves_a_terminal_state(self) -> None:
        # A host that wants to try again gets a new job, so the history keeps
        # both attempts instead of one row that changed its mind.
        for state in TERMINAL_STATES:
            assert ALLOWED_TRANSITIONS[state] == frozenset(), state

    def test_the_terminal_set_is_exactly_the_dead_ends(self) -> None:
        # Derived two ways and compared, so the set and the table cannot drift.
        dead_ends = {state for state, targets in ALLOWED_TRANSITIONS.items() if not targets}

        assert dead_ends == TERMINAL_STATES

    def test_deferred_is_terminal_and_is_not_a_failure(self) -> None:
        # Terminal for *this* job: the scheduler creates a new one when the
        # window opens, so a week-old decision is re-planned against current
        # metadata rather than replayed. And not a failure — conflating them
        # makes a healthy fleet waiting for Sunday look broken.
        assert UpdateState.DEFERRED in TERMINAL_STATES
        assert is_terminal(UpdateState.DEFERRED)
        # And it is not reachable *from* a failure, nor a failure from it:
        # the two are separate conclusions, not stages of one.
        assert not can_transition(UpdateState.FAILED, UpdateState.DEFERRED)
        assert not can_transition(UpdateState.DEFERRED, UpdateState.FAILED)

    def test_is_terminal_takes_the_string_the_column_holds(self) -> None:
        assert is_terminal("succeeded")
        assert not is_terminal("downloading")

    def test_an_unknown_state_is_not_terminal(self) -> None:
        # The safe answer: treating something unrecognised as finished would
        # abandon a job. A row somebody edited by hand should strand nothing.
        assert not is_terminal("who knows")
        assert not is_terminal("")


class TestCancellation:
    def test_cancellation_is_offered_before_the_binary_is_touched(self) -> None:
        for state in (
            UpdateState.AVAILABLE,
            UpdateState.QUEUED,
            UpdateState.DOWNLOADING,
            UpdateState.VERIFYING,
            UpdateState.STAGED,
        ):
            assert can_transition(state, UpdateState.CANCELLED), state

    def test_cancellation_stops_being_offered_once_installing(self) -> None:
        # From INSTALLING onwards "cancel" would be a promise the controller
        # cannot keep: the binary is being replaced on a machine it cannot
        # reach into. The agent's own rollback is what recovers from here, and
        # offering a button that does nothing is worse than offering none.
        for state in (
            UpdateState.INSTALLING,
            UpdateState.RESTARTING,
            UpdateState.VERIFYING_HEALTH,
        ):
            assert not can_transition(state, UpdateState.CANCELLED), state

    def test_rollback_is_reachable_from_exactly_the_states_that_can_roll_back(
        self,
    ) -> None:
        # Rolling back means putting the previous binary in place, which is
        # only meaningful once it has been moved aside.
        can_roll_back = {
            state for state in UpdateState if UpdateState.ROLLED_BACK in ALLOWED_TRANSITIONS[state]
        }

        assert can_roll_back == {
            UpdateState.INSTALLING,
            UpdateState.RESTARTING,
            UpdateState.VERIFYING_HEALTH,
        }


class TestFailureIsAlwaysAvailable:
    def test_every_in_flight_state_can_fail(self) -> None:
        # Anything that has been started can go wrong. A state with no path to
        # FAILED is a state a job can get stuck in forever.
        for state in IN_FLIGHT_STATES:
            assert can_transition(state, UpdateState.FAILED), state

    def test_available_cannot_fail_because_nothing_has_been_attempted(self) -> None:
        # It can be cancelled or deferred. "Failed" would imply an attempt.
        assert not can_transition(UpdateState.AVAILABLE, UpdateState.FAILED)

    def test_the_in_flight_set_excludes_every_terminal_state(self) -> None:
        assert not (IN_FLIGHT_STATES & TERMINAL_STATES)

    def test_in_flight_and_terminal_together_are_everything_but_available(self) -> None:
        # AVAILABLE is neither: nothing has been asked of the host, and
        # nothing has concluded. Stated as a test because a state that is in
        # neither set by accident would be invisible to both queries.
        assert set(UpdateState) - {UpdateState.AVAILABLE} == (IN_FLIGHT_STATES | TERMINAL_STATES)


class TestCanTransitionRefusesRatherThanRaising:
    @pytest.mark.parametrize(
        ("current", "target"),
        [
            ("nonsense", "queued"),
            ("queued", "nonsense"),
            ("", ""),
            ("QUEUED", "DOWNLOADING"),  # case matters; the column is lowercase
        ],
    )
    def test_an_unknown_state_is_a_refusal(self, current: str, target: str) -> None:
        # Called with values read from the database. A row somebody edited by
        # hand should not crash the scheduler.
        assert not can_transition(current, target)

    def test_the_whole_matrix_is_decidable(self) -> None:
        # Every pair returns a bool rather than raising, which is what lets the
        # service call this without a try block.
        for current, target in itertools.product(UpdateState, UpdateState):
            assert isinstance(can_transition(current, target), bool)


class TestTheRolloutStates:
    def test_a_rollout_progresses_canary_first(self) -> None:
        # The canary gating the first batch is the whole feature: a bad release
        # stops after one host rather than after forty.
        assert RolloutState.CANARY.value == "canary"
        assert [state.value for state in RolloutState][:3] == [
            "pending",
            "canary",
            "running",
        ]

    def test_terminal_rollout_states_are_the_two_that_end_it(self) -> None:
        assert {
            RolloutState.COMPLETED,
            RolloutState.CANCELLED,
        } == TERMINAL_ROLLOUT_STATES

    def test_paused_is_not_terminal_because_it_is_resumable(self) -> None:
        assert RolloutState.PAUSED not in TERMINAL_ROLLOUT_STATES


class TestTriggers:
    def test_every_trigger_is_distinct_and_named_for_who_decided(self) -> None:
        # "Who did this" is the first question asked when a fleet does
        # something unexpected, and "the policy did" and "an administrator did"
        # lead to different places.
        assert {trigger.value for trigger in UpdateTrigger} == {
            "manual",
            "policy",
            "rollout",
        }


class TestTheStateValuesAreStableOnTheWire:
    def test_every_state_value_is_lowercase_snake_case(self) -> None:
        # These cross the wire to the agent and the browser, and appear in a
        # CHECK constraint. A value that changed shape would be a migration
        # and a protocol change at once.
        for state in UpdateState:
            assert state.value == state.value.lower()
            assert " " not in state.value
            assert "-" not in state.value

    def test_the_check_constraint_lists_every_state(self) -> None:
        # The constraint is written out as SQL text, so it cannot be derived
        # from the enum at migration time. This is what keeps them in step: a
        # new state that the database would reject fails here instead of in
        # production.
        from sqlalchemy import CheckConstraint

        from hcc.db import Base

        table = Base.metadata.tables["update_jobs"]
        constraint = next(
            c
            for c in table.constraints
            if isinstance(c, CheckConstraint) and c.name == "ck_update_jobs_state_is_known"
        )
        sql = str(constraint.sqltext)

        for state in UpdateState:
            assert f"'{state.value}'" in sql, f"{state.value} is missing from the constraint"
