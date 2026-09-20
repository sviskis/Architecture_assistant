"""Unit tests for the deterministic finite state machines.

Coverage:
* every allowed Step transition,
* every forbidden Step transition,
* ``VERIFIED`` and ``ABORTED`` are terminal,
* retry ``REVISE -> READY`` (and ``FAILED -> READY``),
* the ``WAITING_APPROVAL`` approval flow,
* determinism (identical event sequences -> identical results),
* event/state validation and error reporting,
* the minimal, subordinate Task FSM.
"""

from __future__ import annotations

import pytest

from architecture_assistant.domain.enums import (
    Phase,
    StepEvent,
    StepState,
    TaskEvent,
    TaskState,
)
from architecture_assistant.domain.fsm import (
    STEP_TERMINAL_STATES,
    STEP_TRANSITIONS,
    TASK_TERMINAL_STATES,
    TASK_TRANSITIONS,
    InvalidTransitionError,
    StepStateMachine,
    TaskStateMachine,
)
from architecture_assistant.domain.models import Step, Task

ALLOWED_STEP = sorted(
    (
        (source, event, target)
        for (source, event), target in STEP_TRANSITIONS.items()
    ),
    key=lambda item: (item[0].value, item[1].value),
)
ALLOWED_STEP_IDS = [f"{s.value}+{e.value}->{t.value}" for s, e, t in ALLOWED_STEP]

FORBIDDEN_STEP = sorted(
    (
        (state, event)
        for state in StepState
        for event in StepEvent
        if (state, event) not in STEP_TRANSITIONS
    ),
    key=lambda item: (item[0].value, item[1].value),
)
FORBIDDEN_STEP_IDS = [f"{s.value}+{e.value}" for s, e in FORBIDDEN_STEP]

ALLOWED_TASK = sorted(
    (
        (source, event, target)
        for (source, event), target in TASK_TRANSITIONS.items()
    ),
    key=lambda item: (item[0].value, item[1].value),
)
ALLOWED_TASK_IDS = [f"{s.value}+{e.value}->{t.value}" for s, e, t in ALLOWED_TASK]

FORBIDDEN_TASK = sorted(
    (
        (state, event)
        for state in TaskState
        for event in TaskEvent
        if (state, event) not in TASK_TRANSITIONS
    ),
    key=lambda item: (item[0].value, item[1].value),
)
FORBIDDEN_TASK_IDS = [f"{s.value}+{e.value}" for s, e in FORBIDDEN_TASK]

HAPPY_PATH = (
    StepEvent.PREPARE,
    StepEvent.DISPATCH,
    StepEvent.CLINE_START,
    StepEvent.RECEIVE_REPORT,
    StepEvent.START_REVIEW,
    StepEvent.VERIFY,
)

RECOVERY_PATH = (
    StepEvent.PREPARE,
    StepEvent.DISPATCH,
    StepEvent.CLINE_START,
    StepEvent.RECEIVE_REPORT,
    StepEvent.START_REVIEW,
    StepEvent.REQUEST_REVISE,
    StepEvent.RETRY,
    StepEvent.DISPATCH,
    StepEvent.RECEIVE_REPORT,
    StepEvent.START_REVIEW,
    StepEvent.VERIFY,
)


class TestStepTransitionTable:
    def test_table_size_is_27(self) -> None:
        assert len(STEP_TRANSITIONS) == 27

    def test_transition_keys_are_unique(self) -> None:
        keys = list(STEP_TRANSITIONS.keys())
        assert len(set(keys)) == len(keys)

    def test_table_covers_only_known_states_and_events(self) -> None:
        for (source, event), target in STEP_TRANSITIONS.items():
            assert isinstance(source, StepState)
            assert isinstance(target, StepState)
            assert isinstance(event, StepEvent)

    @pytest.mark.parametrize("source,event,target", ALLOWED_STEP, ids=ALLOWED_STEP_IDS)
    def test_every_allowed_transition(
        self, source: StepState, event: StepEvent, target: StepState
    ) -> None:
        machine = StepStateMachine(initial_state=source)
        assert machine.state is source
        assert machine.can(event) is True
        assert machine.next_state(event) is target
        assert machine.apply(event) is target
        assert machine.state is target


class TestStepForbiddenTransitions:
    def test_forbidden_count_matches_state_event_product(self) -> None:
        assert len(FORBIDDEN_STEP) == len(StepState) * len(StepEvent) - len(
            STEP_TRANSITIONS
        )

    @pytest.mark.parametrize(
        "state,event", FORBIDDEN_STEP, ids=FORBIDDEN_STEP_IDS
    )
    def test_every_forbidden_transition_raises(
        self, state: StepState, event: StepEvent
    ) -> None:
        machine = StepStateMachine(initial_state=state)
        assert machine.can(event) is False
        with pytest.raises(InvalidTransitionError):
            machine.apply(event)
        # a rejected event must never mutate the machine
        assert machine.state is state


class TestStepTerminalStates:
    def test_terminal_states_are_verified_and_aborted(self) -> None:
        assert STEP_TERMINAL_STATES == frozenset(
            {StepState.VERIFIED, StepState.ABORTED}
        )

    @pytest.mark.parametrize("state", [StepState.VERIFIED, StepState.ABORTED])
    def test_terminal_state_accepts_no_event(self, state: StepState) -> None:
        machine = StepStateMachine(initial_state=state)
        assert machine.is_terminal() is True
        assert machine.allowed_events() == ()
        for event in StepEvent:
            assert machine.can(event) is False
            with pytest.raises(InvalidTransitionError):
                machine.apply(event)
        assert machine.state is state

    @pytest.mark.parametrize(
        "state",
        [
            StepState.REVISE,
            StepState.BLOCKED,
            StepState.CONFLICT,
            StepState.FAILED,
        ],
    )
    def test_recoverable_states_are_not_terminal(self, state: StepState) -> None:
        machine = StepStateMachine(initial_state=state)
        assert machine.is_terminal() is False
        assert machine.allowed_events() != ()

    def test_failed_is_not_terminal_because_it_can_retry(self) -> None:
        machine = StepStateMachine(initial_state=StepState.FAILED)
        assert machine.is_terminal() is False
        assert machine.apply(StepEvent.RETRY) is StepState.READY


class TestStepRetry:
    def test_revise_retry_returns_to_ready(self) -> None:
        machine = StepStateMachine(initial_state=StepState.REVISE)
        assert machine.apply(StepEvent.RETRY) is StepState.READY

    def test_failed_retry_returns_to_ready(self) -> None:
        machine = StepStateMachine(initial_state=StepState.FAILED)
        assert machine.apply(StepEvent.RETRY) is StepState.READY

    def test_recovery_loop_reaches_verified(self) -> None:
        machine = StepStateMachine()
        assert machine.apply_many(RECOVERY_PATH) is StepState.VERIFIED


class TestWaitingApprovalFlow:
    def test_ready_requests_approval(self) -> None:
        machine = StepStateMachine()
        machine.apply(StepEvent.PREPARE)
        assert machine.apply(StepEvent.REQUEST_APPROVAL) is StepState.WAITING_APPROVAL

    @pytest.mark.parametrize(
        "event,expected",
        [
            (StepEvent.APPROVE, StepState.DISPATCHED),
            (StepEvent.REJECT, StepState.READY),
            (StepEvent.BLOCK, StepState.BLOCKED),
            (StepEvent.ABORT, StepState.ABORTED),
        ],
        ids=["APPROVE", "REJECT", "BLOCK", "ABORT"],
    )
    def test_waiting_approval_outcomes(
        self, event: StepEvent, expected: StepState
    ) -> None:
        machine = StepStateMachine(initial_state=StepState.WAITING_APPROVAL)
        assert machine.apply(event) is expected

    def test_manual_approval_flow_reaches_verified(self) -> None:
        machine = StepStateMachine()
        final = machine.apply_many(
            [
                StepEvent.PREPARE,
                StepEvent.REQUEST_APPROVAL,
                StepEvent.APPROVE,
                StepEvent.CLINE_START,
                StepEvent.RECEIVE_REPORT,
                StepEvent.START_REVIEW,
                StepEvent.VERIFY,
            ]
        )
        assert final is StepState.VERIFIED

    def test_manual_rejection_returns_to_ready(self) -> None:
        machine = StepStateMachine()
        machine.apply_many([StepEvent.PREPARE, StepEvent.REQUEST_APPROVAL])
        assert machine.apply(StepEvent.REJECT) is StepState.READY
        assert machine.can(StepEvent.DISPATCH) is True


class TestStepHappyPath:
    def test_full_happy_path_trace(self) -> None:
        machine = StepStateMachine()
        trace = [machine.state]
        for event in HAPPY_PATH:
            trace.append(machine.apply(event))
        assert trace == [
            StepState.PENDING,
            StepState.READY,
            StepState.DISPATCHED,
            StepState.CLINE_WORKING,
            StepState.REPORT_RECEIVED,
            StepState.REVIEWING,
            StepState.VERIFIED,
        ]

    def test_dispatch_can_receive_report_without_cline_working(self) -> None:
        machine = StepStateMachine(initial_state=StepState.DISPATCHED)
        assert machine.apply(StepEvent.RECEIVE_REPORT) is StepState.REPORT_RECEIVED

    def test_conflict_can_resolve_back_to_ready(self) -> None:
        machine = StepStateMachine(initial_state=StepState.CONFLICT)
        assert machine.apply(StepEvent.RESOLVE) is StepState.READY

    def test_blocked_can_unblock_back_to_ready(self) -> None:
        machine = StepStateMachine(initial_state=StepState.BLOCKED)
        assert machine.apply(StepEvent.UNBLOCK) is StepState.READY


class TestStepDeterminism:
    def test_identical_sequences_produce_identical_results(self) -> None:
        first = StepStateMachine()
        second = StepStateMachine()
        assert first.apply_many(RECOVERY_PATH) is second.apply_many(RECOVERY_PATH)
        assert first.state is second.state

    def test_repeated_traces_are_identical(self) -> None:
        def trace() -> list:
            machine = StepStateMachine()
            result = [machine.state]
            for event in RECOVERY_PATH:
                result.append(machine.apply(event))
            return result

        assert trace() == trace()

    def test_copy_is_independent(self) -> None:
        machine = StepStateMachine()
        machine.apply(StepEvent.PREPARE)
        clone = machine.copy()
        clone.apply(StepEvent.DISPATCH)
        assert machine.state is StepState.READY
        assert clone.state is StepState.DISPATCHED

    def test_reset_returns_to_initial_state(self) -> None:
        machine = StepStateMachine()
        machine.apply_many(HAPPY_PATH)
        assert machine.reset() is StepState.PENDING



class TestStepStateMachineConstruction:
    def test_default_initial_state_is_pending(self) -> None:
        assert StepStateMachine().state is StepState.PENDING

    def test_fresh_machine_offers_only_prepare(self) -> None:
        assert StepStateMachine().allowed_events() == (StepEvent.PREPARE,)

    def test_unknown_initial_state_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            StepStateMachine(initial_state="NOT_A_STATE")

    def test_string_event_is_coerced(self) -> None:
        machine = StepStateMachine()
        assert machine.apply("PREPARE") is StepState.READY

    def test_unknown_event_raises_value_error(self) -> None:
        machine = StepStateMachine()
        with pytest.raises(ValueError):
            machine.apply("NOT_AN_EVENT")
        assert machine.can("NOT_AN_EVENT") is False

    def test_from_step_uses_step_state(self) -> None:
        step = Step(
            step_no=7,
            phase=Phase.CLINE,
            title="Cline Worker Adapter",
            state=StepState.DISPATCHED,
        )
        assert StepStateMachine.from_step(step).state is StepState.DISPATCHED


class TestErrorReporting:
    def test_error_exposes_state_event_and_allowed_events(self) -> None:
        machine = StepStateMachine(initial_state=StepState.PENDING)
        with pytest.raises(InvalidTransitionError) as excinfo:
            machine.apply(StepEvent.VERIFY)
        error = excinfo.value
        assert error.state is StepState.PENDING
        assert error.event is StepEvent.VERIFY
        assert error.allowed_events == (StepEvent.PREPARE,)
        assert "PREPARE" in str(error)

    def test_terminal_error_message_mentions_terminal_state(self) -> None:
        machine = StepStateMachine(initial_state=StepState.VERIFIED)
        with pytest.raises(InvalidTransitionError) as excinfo:
            machine.apply(StepEvent.PREPARE)
        assert "terminal state" in str(excinfo.value)

    def test_invalid_transition_is_not_a_silent_noop(self) -> None:
        machine = StepStateMachine(initial_state=StepState.READY)
        with pytest.raises(InvalidTransitionError):
            machine.apply(StepEvent.VERIFY)
        assert machine.state is StepState.READY


class TestTaskStateMachine:
    def test_task_table_is_minimal(self) -> None:
        assert len(TASK_TRANSITIONS) == 3
        assert len(TASK_TRANSITIONS) < len(STEP_TRANSITIONS)

    def test_task_terminal_states(self) -> None:
        assert TASK_TERMINAL_STATES == frozenset(
            {TaskState.REPORTED, TaskState.FAILED}
        )

    @pytest.mark.parametrize("source,event,target", ALLOWED_TASK, ids=ALLOWED_TASK_IDS)
    def test_every_allowed_task_transition(
        self, source: TaskState, event: TaskEvent, target: TaskState
    ) -> None:
        machine = TaskStateMachine(initial_state=source)
        assert machine.can(event) is True
        assert machine.apply(event) is target

    @pytest.mark.parametrize("state,event", FORBIDDEN_TASK, ids=FORBIDDEN_TASK_IDS)
    def test_every_forbidden_task_transition_raises(
        self, state: TaskState, event: TaskEvent
    ) -> None:
        machine = TaskStateMachine(initial_state=state)
        assert machine.can(event) is False
        with pytest.raises(InvalidTransitionError):
            machine.apply(event)

    def test_task_default_state_is_created(self) -> None:
        assert TaskStateMachine().state is TaskState.CREATED

    def test_task_fsm_is_subordinate_to_step_lifecycle(self) -> None:
        task_states = {state.value for state in TaskState}
        step_only_states = {
            "WAITING_APPROVAL",
            "CLINE_WORKING",
            "REVIEWING",
            "VERIFIED",
            "ABORTED",
            "CONFLICT",
            "BLOCKED",
        }
        assert task_states.isdisjoint(step_only_states)

    def test_from_task_uses_task_state(self) -> None:
        task = Task(
            step_no=1,
            phase=Phase.FOUNDATION,
            title="Domain models + FSM",
            state=TaskState.DISPATCHED,
        )
        assert TaskStateMachine.from_task(task).state is TaskState.DISPATCHED

    def test_task_dispatch_flow_is_deterministic(self) -> None:
        def trace() -> list:
            machine = TaskStateMachine()
            return [
                machine.state,
                machine.apply(TaskEvent.DISPATCH),
                machine.apply(TaskEvent.RECEIVE_REPORT),
            ]

        assert trace() == trace() == [
            TaskState.CREATED,
            TaskState.DISPATCHED,
            TaskState.REPORTED,
        ]

