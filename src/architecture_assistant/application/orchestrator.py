"""The event-driven Assistant <-> Cline loop.

The orchestrator is a deterministic state machine *driver*: every call to
:meth:`Orchestrator.run_once` looks at the **persisted** state of the current
step and performs at most **one** FSM transition, persisting it together with
exactly one audit entry in a single transaction.

Design rules
------------
* **The persisted Step state is the only cursor.** Nothing about "where the loop
  is" is kept in memory, so restarting the process resumes exactly where it
  stopped (restart recovery is a property of the design, not extra code).
* **Waiting is free.** When the loop needs an external event (a worker report, a
  human approval, an unblock, a conflict resolution or a timeout deadline) it
  returns :attr:`LoopStatus.WAITING` instead of polling or sleeping.
* **Architecture compliance is fail-closed.** A realization gate
  (:class:`~architecture_assistant.ports.capabilities.RealizationControlPort`)
  is a **mandatory** dependency: ``VERIFY`` is only reachable when the realized
  source satisfies the authoritative baseline. A deterministic violation
  downgrades the outcome to ``BLOCKED`` and can never be out-voted.
* **One transition per tick, one audit entry per transition.**
* **The report is consumed last.** The report is read (``read_report`` -
  read-only and repeatable), the outcome is persisted, and only *then* is it
  acknowledged. A crash anywhere before the outcome is durable can therefore
  never lose a ``WorkerResult``.
* The orchestrator depends on ``domain``, ``ports`` and its own layer only: the
  worker is a :class:`WorkerChannelPort`, the context comes from the injected
  :class:`ContextBuilder`, and the concrete adapters are wired by the
  composition root.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Callable, Mapping, Optional

from ..domain.audit import AuditAction, AuditEntityType, AuditEntry
from ..domain.enums import (
    StepEvent,
    StepState,
    TaskEvent,
    TaskState,
)
from ..domain.fsm import (
    InvalidTransitionError,
    StepStateMachine,
    TaskStateMachine,
)
from ..domain.models import Project, Step, Task, utc_now
from ..ports.capabilities import (
    RealizationControlPort,
    WorkerChannelPort,
    WorkerRequest,
    WorkerResult,
)
from ..ports.repositories import TaskKey
from ..ports.storage import StoragePort
from .context import ContextBuilder
from .loop_policy import ReviewOutcome, decide_review, requires_approval

__all__ = [
    "DEFAULT_STEP_TIMEOUT",
    "DEFAULT_TASK_INSTRUCTIONS",
    "DEFAULT_REPORT_SCHEMA",
    "LoopStatus",
    "TickResult",
    "OrchestratorError",
    "LoopInvariantError",
    "Orchestrator",
]

#: Conservative default deadline for a dispatched step. The composition root
#: configures the value that fits the operator; ``None`` disables the check.
DEFAULT_STEP_TIMEOUT: Optional[timedelta] = timedelta(hours=24)

#: The Assistant -> Cline instruction block (provider neutral: a mapping the
#: worker adapter serializes in whatever way it needs).
DEFAULT_TASK_INSTRUCTIONS: Mapping[str, Any] = {
    "plan_first": True,
    "wait_for_approval_if_manual": True,
    "scope": "Only current step",
    "architecture_change": "STOP and report architecture_questions",
    "tests": "Run relevant tests and report counts",
    "report": "Write the exact report schema to from_cline",
}

#: The report schema the assistant expects back from the worker.
DEFAULT_REPORT_SCHEMA: Mapping[str, Any] = {
    "step_no": "int",
    "status": "DONE|REVISE|BLOCKED|FAILED",
    "files_created": [],
    "files_changed": [],
    "files_deleted": [],
    "tests": {"passed": "int", "failed": "int", "command": "str"},
    "dependencies_added": [],
    "architecture_questions": [],
    "issues": [],
    "summary": "str",
}


class LoopStatus(StrEnum):
    """Outcome of one loop tick."""

    #: Exactly one transition was persisted; calling again may progress further.
    TRANSITIONED = "TRANSITIONED"
    #: The loop needs an external event and must not spin.
    WAITING = "WAITING"
    #: Every step is VERIFIED - the project is complete.
    COMPLETE = "COMPLETE"
    #: The current step was aborted; the loop stops and never activates the next.
    ABORTED = "ABORTED"
    #: The source of truth holds no steps yet.
    NO_STEPS = "NO_STEPS"
    #: The project is paused; the loop does nothing until it is resumed.
    PAUSED = "PAUSED"


@dataclass(frozen=True)
class TickResult:
    """Deterministic description of one tick."""

    status: LoopStatus
    step_no: Optional[int] = None
    event: Optional[StepEvent] = None
    from_state: Optional[StepState] = None
    to_state: Optional[StepState] = None
    reason: str = ""
    attempt: Optional[int] = None
    acknowledged: bool = False
    ack_error: Optional[str] = None

    @property
    def transitioned(self) -> bool:
        """Whether the tick persisted a transition."""
        return self.status is LoopStatus.TRANSITIONED

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "status": self.status.value,
            "step_no": self.step_no,
            "event": None if self.event is None else self.event.value,
            "from_state": (
                None if self.from_state is None else self.from_state.value
            ),
            "to_state": None if self.to_state is None else self.to_state.value,
            "reason": self.reason,
            "attempt": self.attempt,
            "acknowledged": self.acknowledged,
            "ack_error": self.ack_error,
        }


class OrchestratorError(Exception):
    """Base class for orchestrator errors."""


class LoopInvariantError(OrchestratorError):
    """Raised when the source of truth violates a loop invariant."""


#: Step states that halt the loop and keep the step current until a human acts,
#: mapped to the exact reason the loop reports.
_HALTING_REASONS: dict = {
    StepState.WAITING_APPROVAL: "human-approval-required",
    StepState.BLOCKED: "human-unblock-required",
    StepState.CONFLICT: "human-conflict-resolution-required",
}


class Orchestrator:
    """Drives one Step of the Assistant <-> Cline loop per tick."""

    def __init__(
        self,
        storage: StoragePort,
        worker: WorkerChannelPort,
        context_builder: ContextBuilder,
        *,
        realization_control: RealizationControlPort,
        clock: Callable[[], datetime] = utc_now,
        timeout: Optional[timedelta] = DEFAULT_STEP_TIMEOUT,
        instructions: Mapping[str, Any] = DEFAULT_TASK_INSTRUCTIONS,
        report_schema: Mapping[str, Any] = DEFAULT_REPORT_SCHEMA,
    ) -> None:
        if not callable(clock):
            raise ValueError("clock must be a callable returning a datetime")
        if timeout is not None and not isinstance(timeout, timedelta):
            raise ValueError(f"timeout must be a timedelta or None; got {timeout!r}")
        if timeout is not None and timeout <= timedelta(0):
            raise ValueError(f"timeout must be positive; got {timeout!r}")
        if not isinstance(worker, WorkerChannelPort):
            raise ValueError(
                "worker must implement WorkerChannelPort "
                "(dispatch/read_report/acknowledge_report); got "
                f"{type(worker).__name__}"
            )
        if not isinstance(context_builder, ContextBuilder):
            raise ValueError(
                f"context_builder must be a ContextBuilder; "
                f"got {type(context_builder).__name__}"
            )
        # Mandatory, not optional: architecture compliance is fail-closed, so a
        # loop without a realization gate could reach VERIFIED unverified. The
        # composition root always injects one.
        if not isinstance(realization_control, RealizationControlPort):
            raise ValueError(
                "realization_control is required and must implement "
                "RealizationControlPort (gate(step_no, attempt)); the loop must "
                "never be able to VERIFY without the architecture gate"
            )
        self._storage = storage
        self._worker = worker
        self._context_builder = context_builder
        self._realization = realization_control
        self._clock = clock
        self._timeout = timeout
        self._instructions = dict(instructions or {})
        self._report_schema = dict(report_schema or {})

    # -- read-only introspection -----------------------------------------
    @property
    def realization_control(self) -> RealizationControlPort:
        """The mandatory architecture gate of the loop."""
        return self._realization

    @property
    def timeout(self) -> Optional[timedelta]:
        """The configured deadline, or ``None`` when the check is disabled."""
        return self._timeout

    def current_step(self) -> Optional[Step]:
        """The authoritative current step, derived from the source of truth.

        It is the *lowest-numbered* step that is not ``VERIFIED`` - never a
        stored snapshot, and never a step that jumps the queue: while the
        current step is not ``VERIFIED`` no later step can become current.
        """
        return self._current(self._storage.steps.list())

    def next_step_no(self) -> Optional[int]:
        """The step number after the current one - informational only.

        Provided for reporting; it never activates anything. A later step starts
        only when the current step reaches ``VERIFIED``.
        """
        steps = self._storage.steps.list()
        current = self._current(steps)
        if current is None:
            return None
        following = sorted(
            step.step_no for step in steps if step.step_no > current.step_no
        )
        return following[0] if following else None

    def is_complete(self) -> bool:
        """Whether every required step is ``VERIFIED``.

        ``ABORTED`` is a terminal *step* state but a **stop** condition for the
        project: it never counts as verified, so a project is complete only when
        all of its steps reached ``VERIFIED``.
        """
        steps = self._storage.steps.list()
        return bool(steps) and all(
            step.state is StepState.VERIFIED for step in steps
        )

    def is_paused(self) -> bool:
        """Whether the project is paused.

        Pause/resume is a runtime flag on the project (toggled by the human
        override step); the loop only *respects* it and never changes it.
        """
        return self._require_project().paused

    def step_count(self) -> int:
        """How many steps the source of truth currently holds."""
        return len(self._storage.steps.list())

    # -- the loop ---------------------------------------------------------
    def run_once(self) -> TickResult:
        """Perform **at most one** persisted transition of the current step.

        The returned :class:`TickResult` says exactly why the loop did (or did
        not) progress, so a caller can decide whether to call again immediately,
        wait for an external event, or stop.
        """
        project = self._require_project()
        if project.paused:
            return TickResult(
                status=LoopStatus.PAUSED, reason="project-paused"
            )
        steps = self._storage.steps.list()
        if not steps:
            return TickResult(status=LoopStatus.NO_STEPS, reason="no-steps")
        step = self._current(steps)
        if step is None:
            return TickResult(
                status=LoopStatus.COMPLETE, reason="all-steps-verified"
            )
        return self._advance(step, project)

    def _advance(self, step: Step, project: Project) -> TickResult:
        """Route the persisted current state to exactly one handler."""
        state = step.state
        if state is StepState.PENDING:
            # READY always carries a 1-based attempt number, so the first
            # dispatch needs no invented ordinal later.
            return self._transition(
                step,
                StepEvent.PREPARE,
                "prepared",
                attempt=max(step.attempt, 1),
            )
        if state is StepState.READY:
            return self._from_ready(step, project)
        if state in _HALTING_REASONS:
            return self._wait(step, _HALTING_REASONS[state])
        if state is StepState.DISPATCHED:
            return self._collect(step, allow_start=True)
        if state is StepState.CLINE_WORKING:
            return self._collect(step, allow_start=False)
        if state is StepState.REPORT_RECEIVED:
            return self._transition(
                step, StepEvent.START_REVIEW, "review-started"
            )
        if state is StepState.REVIEWING:
            return self._review(step)
        if state is StepState.REVISE or state is StepState.FAILED:
            return self._retry_or_abort(step)
        if state is StepState.ABORTED:
            return TickResult(
                status=LoopStatus.ABORTED,
                step_no=step.step_no,
                from_state=state,
                to_state=state,
                reason="step-aborted",
                attempt=step.attempt,
            )
        raise LoopInvariantError(f"unhandled step state {state!r}")

    # -- per-state handlers ------------------------------------------------
    def _from_ready(self, step: Step, project: Project) -> TickResult:
        """Approve and wait, or dispatch - decided by the approval policy."""
        if requires_approval(project.mode, step.risk, step.requires_human):
            return self._transition(
                step, StepEvent.REQUEST_APPROVAL, "approval-required"
            )
        return self._dispatch(step)

    def _dispatch(self, step: Step) -> TickResult:
        """Build the context, hand over the task, then record the dispatch.

        The context comes from the injected :class:`ContextBuilder` (never from
        an ad-hoc query), and it is published *before* the dispatch is persisted:
        a crash in between simply re-dispatches the same, deterministic task file
        for the same attempt (at-least-once), while a persisted dispatch can
        never exist without its task.
        """
        attempt = max(step.attempt, 1)
        snapshot = self._context_builder.build(step.step_no)
        task = Task(
            step_no=step.step_no,
            phase=step.phase,
            title=step.title,
            description=step.description,
            risk=step.risk,
            attempt=attempt,
            max_attempts=step.max_attempts,
            state=TaskState.CREATED,
            context_file=None,
            instructions=self._instructions,
            report_schema=self._report_schema,
            created_at=self._clock(),
        )
        request = WorkerRequest(task=task, context=snapshot.to_dict())
        self._worker.dispatch(request)
        dispatched = replace(
            task,
            state=TaskStateMachine.from_task(task).next_state(
                TaskEvent.DISPATCH
            ),
        )
        return self._transition(
            step,
            StepEvent.DISPATCH,
            "dispatched",
            attempt=attempt,
            task=dispatched,
            start_clock=True,
        )

    def _collect(self, step: Step, *, allow_start: bool) -> TickResult:
        """Poll for the report, fail on the deadline, else keep waiting.

        The report is always checked **before** the deadline, so a report that
        arrived in time is never discarded by a timeout.
        """
        report: Optional[WorkerResult] = self._worker.read_report(
            step.step_no, step.attempt
        )
        if report is not None:
            return self._transition(
                step,
                StepEvent.RECEIVE_REPORT,
                "report-received",
                task=self._advance_task(
                    step, TaskEvent.RECEIVE_REPORT, report_status=report.status
                ),
            )
        if self._timed_out(step):
            return self._transition(
                step,
                StepEvent.FAIL,
                "timeout",
                task=self._advance_task(step, TaskEvent.FAIL),
            )
        if allow_start:
            return self._transition(
                step, StepEvent.CLINE_START, "worker-started", start_clock=True
            )
        return self._wait(step, "worker-report-pending")

    def _review(self, step: Step) -> TickResult:
        """Decide the outcome, persist it, and only then acknowledge.

        Re-reading the report here is deliberate: until the outcome is durable
        the report must stay on disk, so a crash between ``REPORT_RECEIVED`` and
        this tick is recoverable - the very same ``WorkerResult`` is read again.

        Before anything may reach ``VERIFIED`` the mandatory realization gate
        runs: a deterministic architecture violation downgrades the outcome to
        ``BLOCK``. The gate verdict, the evidence it records and the step
        transition are one transaction, so either all of it is durable or none
        of it is - and the report is acknowledged only afterwards.
        """
        report: Optional[WorkerResult] = self._worker.read_report(
            step.step_no, step.attempt
        )
        if report is None:
            return self._wait(step, "report-missing-after-received")
        outcome = decide_review(
            report, attempt=step.attempt, max_attempts=step.max_attempts
        )
        result: TickResult
        if outcome.event is StepEvent.VERIFY:
            with self._storage.transaction():
                gate = self._realization.gate(step.step_no, step.attempt)
                if not gate.compliant:
                    outcome = ReviewOutcome(
                        StepEvent.BLOCK, "architecture-violation"
                    )
                result = self._transition(
                    step, outcome.event, outcome.reason
                )
        else:
            result = self._transition(step, outcome.event, outcome.reason)
        acknowledged, ack_error = self._acknowledge(step)
        return replace(
            result, acknowledged=acknowledged, ack_error=ack_error
        )

    def _retry_or_abort(self, step: Step) -> TickResult:
        """Retry with the next attempt, or abort once attempts are exhausted.

        The attempt increment and the state transition are one transaction, so a
        crash can never leave a step that claims to retry but still carries the
        old attempt number.
        """
        if step.can_retry:
            return self._transition(
                step, StepEvent.RETRY, "retry", attempt=step.attempt + 1
            )
        if step.state is StepState.FAILED:
            return self._transition(
                step, StepEvent.ABORT, "attempts-exhausted"
            )
        # REVISE has no terminating FSM exit (only RETRY -> READY, which the
        # domain forbids once attempts are exhausted). The loop waits for a
        # human decision instead of inventing a transition that does not exist.
        return self._wait(step, "revise-attempts-exhausted")

    def _wait(self, step: Step, reason: str) -> TickResult:
        """Report that the loop must not spin while an external event is due."""
        return TickResult(
            status=LoopStatus.WAITING,
            step_no=step.step_no,
            from_state=step.state,
            to_state=step.state,
            reason=reason,
            attempt=step.attempt,
        )

    # -- persistence -------------------------------------------------------
    def _transition(
        self,
        step: Step,
        event: StepEvent,
        reason: str,
        *,
        attempt: Optional[int] = None,
        task: Optional[Task] = None,
        start_clock: bool = False,
    ) -> TickResult:
        """Apply one FSM event and persist it with exactly one audit entry.

        Step state, the subordinate Task (when present) and the audit entry are
        written inside **one** transaction, so a persisted transition is always
        fully traceable or not written at all.
        """
        to_state = StepStateMachine.from_step(step).next_state(event)
        now = self._clock()
        attempt_value = step.attempt if attempt is None else attempt
        fields: dict[str, Any] = {
            "state": to_state,
            "attempt": attempt_value,
            "last_update_at": now,
        }
        if start_clock and step.started_at is None:
            fields["started_at"] = now
        if to_state is StepState.VERIFIED:
            fields["verified_at"] = now
            fields["finished_at"] = now
        elif to_state is StepState.ABORTED:
            fields["finished_at"] = now
        updated = replace(step, **fields)
        entry = AuditEntry(
            entity_type=AuditEntityType.STEP,
            entity_id=str(step.step_no),
            action=AuditAction.UPDATE,
            detail={
                "step_no": step.step_no,
                "event": event.value,
                "from": step.state.value,
                "to": to_state.value,
                "attempt": attempt_value,
                "reason": reason,
            },
            created_at=now,
        )
        with self._storage.transaction():
            self._storage.steps.upsert(updated)
            if task is not None:
                self._storage.tasks.upsert(task)
            self._storage.audit.append(entry)
        return TickResult(
            status=LoopStatus.TRANSITIONED,
            step_no=step.step_no,
            event=event,
            from_state=step.state,
            to_state=to_state,
            reason=reason,
            attempt=attempt_value,
        )

    def _acknowledge(self, step: Step) -> tuple[bool, Optional[str]]:
        """Consume the report **after** the outcome is durable.

        Best-effort by design: the outcome is already committed, so a failing
        acknowledgement may never roll it back or cause it to be processed
        twice. The failure is reported on the :class:`TickResult` instead of
        being hidden, and the report simply stays on disk - the step no longer
        reads it, because its persisted state has already moved on.
        """
        try:
            self._worker.acknowledge_report(step.step_no, step.attempt)
        except Exception as error:  # noqa: BLE001 - the durable outcome wins
            return False, f"{type(error).__name__}: {error}"
        return True, None

    def _advance_task(
        self,
        step: Step,
        event: TaskEvent,
        *,
        report_status: Optional[Any] = None,
    ) -> Optional[Task]:
        """Advance the subordinate Task marker, when one exists.

        The Task is a dispatch marker only, so an absent or non-advanceable Task
        never blocks the authoritative Step transition.
        """
        task = self._storage.tasks.get(TaskKey(step.step_no, step.attempt))
        if task is None:
            return None
        try:
            next_state = TaskStateMachine.from_task(task).next_state(event)
        except InvalidTransitionError:
            return None
        fields: dict[str, Any] = {"state": next_state}
        if report_status is not None:
            fields["report_status"] = report_status
        return replace(task, **fields)

    def _timed_out(self, step: Step) -> bool:
        """Whether the dispatched step exceeded its deadline.

        Uses the persisted Step timestamps only - never an in-memory guess. When
        the step carries no timestamp at all the check is inconclusive, and the
        loop prefers waiting over failing a step it cannot judge.
        """
        if self._timeout is None:
            return False
        reference = step.last_update_at or step.started_at
        if reference is None:
            return False
        return self._clock() - reference > self._timeout

    # -- source of truth ---------------------------------------------------
    def _current(self, steps: tuple[Step, ...]) -> Optional[Step]:
        """The lowest-numbered step that is not ``VERIFIED``."""
        for step in sorted(steps, key=lambda item: item.step_no):
            if step.state is not StepState.VERIFIED:
                return step
        return None

    def _require_project(self) -> Project:
        projects = self._storage.projects.list()
        if not projects:
            raise LoopInvariantError(
                "the source of truth contains no project"
            )
        if len(projects) > 1:
            names = ", ".join(sorted(project.name for project in projects))
            raise LoopInvariantError(
                f"expected exactly one project, found {len(projects)}: {names}"
            )
        return projects[0]


