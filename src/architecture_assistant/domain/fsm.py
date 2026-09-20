"""Deterministic finite state machines.

Two machines live here:

* :class:`StepStateMachine` - the authoritative Assistant <-> Cline workflow.
* :class:`TaskStateMachine` - a minimal, subordinate Cline-dispatch marker that
  never drives Step transitions.

Both are pure transition tables: for a given ``(state, event)`` pair the next
state is always the same, therefore identical event sequences always produce
identical results.
"""

from __future__ import annotations

from typing import Any, ClassVar, Iterable, Mapping

from .enums import StepEvent, StepState, TaskEvent, TaskState

__all__ = [
    "FSMError",
    "InvalidTransitionError",
    "FiniteStateMachine",
    "StepStateMachine",
    "TaskStateMachine",
    "STEP_TRANSITIONS",
    "TASK_TRANSITIONS",
    "STEP_TERMINAL_STATES",
    "TASK_TERMINAL_STATES",
]


class FSMError(Exception):
    """Base class for all FSM errors."""


class InvalidTransitionError(FSMError):
    """Raised when an event is not allowed from the current state."""

    def __init__(self, state: Any, event: Any, allowed_events: Iterable[Any]) -> None:
        self.state = state
        self.event = event
        self.allowed_events = tuple(allowed_events)
        if self.allowed_events:
            allowed = ", ".join(member.value for member in self.allowed_events)
        else:
            allowed = "<none - terminal state>"
        super().__init__(
            f"event {event.value!r} is not allowed from state {state.value!r}; "
            f"allowed events: {allowed}"
        )


class FiniteStateMachine:
    """Deterministic transition-table FSM.

    Subclasses declare ``TRANSITIONS`` (``{(state, event): next_state}``),
    ``STATES``, ``EVENTS``, ``EVENT_ENUM``, ``TERMINAL_STATES`` and
    ``INITIAL_STATE``. The machine itself holds only the current state; every
    transition is a pure lookup, so behaviour is fully deterministic.
    """

    TRANSITIONS: ClassVar[Mapping[Any, Any]] = {}
    STATES: ClassVar[tuple] = ()
    EVENTS: ClassVar[tuple] = ()
    EVENT_ENUM: ClassVar[Any] = None
    TERMINAL_STATES: ClassVar[frozenset] = frozenset()
    INITIAL_STATE: ClassVar[Any] = None

    def __init__(self, initial_state: Any = None) -> None:
        self._validate_transitions()
        self._state = self._check_state(
            self.INITIAL_STATE if initial_state is None else initial_state
        )

    # -- introspection ---------------------------------------------------
    @property
    def state(self) -> Any:
        """The current state."""
        return self._state

    def allowed_events(self) -> tuple:
        """Events that are legal from the current state, in canonical order."""
        return tuple(
            event
            for event in self.EVENTS
            if (self._state, event) in self.TRANSITIONS
        )

    def is_terminal(self, state: Any = None) -> bool:
        """Whether the given (or current) state has no outgoing transitions."""
        target = self._state if state is None else state
        return target in self.TERMINAL_STATES

    def next_state(self, event: Any) -> Any:
        """Pure lookup of the next state without mutating the machine."""
        resolved = self._resolve_event(event)
        key = (self._state, resolved)
        if key not in self.TRANSITIONS:
            raise InvalidTransitionError(self._state, resolved, self.allowed_events())
        return self.TRANSITIONS[key]

    # -- mutation --------------------------------------------------------
    def can(self, event: Any) -> bool:
        """Whether ``event`` is legal from the current state."""
        try:
            resolved = self._resolve_event(event)
        except ValueError:
            return False
        return (self._state, resolved) in self.TRANSITIONS

    def apply(self, event: Any) -> Any:
        """Apply ``event`` and return the new state, or raise on illegal input."""
        self._state = self.next_state(event)
        return self._state

    def apply_many(self, events: Iterable[Any]) -> Any:
        """Apply a sequence of events in order and return the final state."""
        for event in events:
            self.apply(event)
        return self._state

    def reset(self, state: Any = None) -> Any:
        """Reset the machine to ``state`` (or the initial state)."""
        self._state = self._check_state(
            self.INITIAL_STATE if state is None else state
        )
        return self._state

    def copy(self) -> "FiniteStateMachine":
        """Return an independent machine positioned at the current state."""
        return type(self)(initial_state=self._state)

    # -- internals -------------------------------------------------------
    def _check_state(self, state: Any) -> Any:
        if state not in self.STATES:
            raise ValueError(
                f"unknown state {state!r} for {type(self).__name__}"
            )
        return state

    def _resolve_event(self, event: Any) -> Any:
        if isinstance(event, self.EVENT_ENUM):
            return event
        if isinstance(event, str):
            try:
                return self.EVENT_ENUM(event)
            except ValueError:
                pass
        raise ValueError(f"unknown event {event!r} for {type(self).__name__}")

    @classmethod
    def _validate_transitions(cls) -> None:
        for (state, event), target in cls.TRANSITIONS.items():
            if state not in cls.STATES:
                raise ValueError(
                    f"{cls.__name__}: unknown source state {state!r}"
                )
            if target not in cls.STATES:
                raise ValueError(
                    f"{cls.__name__}: unknown target state {target!r}"
                )
            if not isinstance(event, cls.EVENT_ENUM):
                raise ValueError(f"{cls.__name__}: unknown event {event!r}")


# ---------------------------------------------------------------------------
# Step FSM (authoritative Assistant <-> Cline workflow)
# ---------------------------------------------------------------------------

#: Success terminal state for a Step.
STEP_TERMINAL_STATES: frozenset = frozenset({StepState.VERIFIED, StepState.ABORTED})

#: Every legal Step transition, exactly as approved in the master workflow.
STEP_TRANSITIONS: dict = {
    (StepState.PENDING, StepEvent.PREPARE): StepState.READY,
    (StepState.READY, StepEvent.REQUEST_APPROVAL): StepState.WAITING_APPROVAL,
    (StepState.READY, StepEvent.DISPATCH): StepState.DISPATCHED,
    (StepState.WAITING_APPROVAL, StepEvent.REJECT): StepState.READY,
    (StepState.WAITING_APPROVAL, StepEvent.APPROVE): StepState.DISPATCHED,
    (StepState.WAITING_APPROVAL, StepEvent.BLOCK): StepState.BLOCKED,
    (StepState.WAITING_APPROVAL, StepEvent.ABORT): StepState.ABORTED,
    (StepState.DISPATCHED, StepEvent.CLINE_START): StepState.CLINE_WORKING,
    (StepState.DISPATCHED, StepEvent.RECEIVE_REPORT): StepState.REPORT_RECEIVED,
    (StepState.DISPATCHED, StepEvent.FAIL): StepState.FAILED,
    (StepState.DISPATCHED, StepEvent.BLOCK): StepState.BLOCKED,
    (StepState.CLINE_WORKING, StepEvent.RECEIVE_REPORT): StepState.REPORT_RECEIVED,
    (StepState.CLINE_WORKING, StepEvent.FAIL): StepState.FAILED,
    (StepState.CLINE_WORKING, StepEvent.BLOCK): StepState.BLOCKED,
    (StepState.REPORT_RECEIVED, StepEvent.START_REVIEW): StepState.REVIEWING,
    (StepState.REVIEWING, StepEvent.VERIFY): StepState.VERIFIED,
    (StepState.REVIEWING, StepEvent.REQUEST_REVISE): StepState.REVISE,
    (StepState.REVIEWING, StepEvent.BLOCK): StepState.BLOCKED,
    (StepState.REVIEWING, StepEvent.RAISE_CONFLICT): StepState.CONFLICT,
    (StepState.REVISE, StepEvent.RETRY): StepState.READY,
    (StepState.BLOCKED, StepEvent.UNBLOCK): StepState.READY,
    (StepState.BLOCKED, StepEvent.ABORT): StepState.ABORTED,
    (StepState.CONFLICT, StepEvent.RESOLVE): StepState.READY,
    (StepState.CONFLICT, StepEvent.BLOCK): StepState.BLOCKED,
    (StepState.CONFLICT, StepEvent.ABORT): StepState.ABORTED,
    (StepState.FAILED, StepEvent.RETRY): StepState.READY,
    (StepState.FAILED, StepEvent.ABORT): StepState.ABORTED,
}


class StepStateMachine(FiniteStateMachine):
    """Authoritative Step workflow FSM.

    ``VERIFIED`` is the successful terminal state; ``ABORTED`` the cancelled
    terminal state. ``FAILED`` / ``BLOCKED`` / ``CONFLICT`` / ``REVISE`` remain
    recoverable.
    """

    TRANSITIONS: ClassVar[Mapping[Any, Any]] = STEP_TRANSITIONS
    STATES: ClassVar[tuple] = tuple(StepState)
    EVENTS: ClassVar[tuple] = tuple(StepEvent)
    EVENT_ENUM: ClassVar[Any] = StepEvent
    TERMINAL_STATES: ClassVar[frozenset] = STEP_TERMINAL_STATES
    INITIAL_STATE: ClassVar[Any] = StepState.PENDING

    @classmethod
    def from_step(cls, step: Any) -> "StepStateMachine":
        """Build a machine positioned at ``step.state``."""
        return cls(initial_state=step.state)


# ---------------------------------------------------------------------------
# Task FSM (minimal, subordinate Cline-dispatch marker)
# ---------------------------------------------------------------------------

#: Task states with no further outgoing transitions.
TASK_TERMINAL_STATES: frozenset = frozenset({TaskState.REPORTED, TaskState.FAILED})

#: Deliberately minimal: dispatch/report tracking only. It never drives Step
#: transitions and must stay a subset of the Step lifecycle vocabulary.
TASK_TRANSITIONS: dict = {
    (TaskState.CREATED, TaskEvent.DISPATCH): TaskState.DISPATCHED,
    (TaskState.DISPATCHED, TaskEvent.RECEIVE_REPORT): TaskState.REPORTED,
    (TaskState.DISPATCHED, TaskEvent.FAIL): TaskState.FAILED,
}


class TaskStateMachine(FiniteStateMachine):
    """Minimal Task dispatch FSM, subordinate to :class:`StepStateMachine`."""

    TRANSITIONS: ClassVar[Mapping[Any, Any]] = TASK_TRANSITIONS
    STATES: ClassVar[tuple] = tuple(TaskState)
    EVENTS: ClassVar[tuple] = tuple(TaskEvent)
    EVENT_ENUM: ClassVar[Any] = TaskEvent
    TERMINAL_STATES: ClassVar[frozenset] = TASK_TERMINAL_STATES
    INITIAL_STATE: ClassVar[Any] = TaskState.CREATED

    @classmethod
    def from_task(cls, task: Any) -> "TaskStateMachine":
        """Build a machine positioned at ``task.state``."""
        return cls(initial_state=task.state)

