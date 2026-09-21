"""Approval gate - the explicit human decision the approval policy waits for.

The loop stops on ``WAITING_APPROVAL`` and waits (``HALTING_REASONS``); the
authoritative FSM defines exactly four exits for that state, two of which are
human decisions. This module owns those two and nothing else:

* ``APPROVE`` - ``WAITING_APPROVAL -> DISPATCHED`` (``domain/fsm.py``),
* ``REJECT`` - ``WAITING_APPROVAL -> READY`` (``domain/fsm.py``),

plus the workflow mechanics ``APPROVE`` requires to be safe: publishing the same
deterministic task artifacts the loop publishes, one Task row and one audit
entry, written in one transaction.

Boundaries (deliberately narrow)
--------------------------------
It may: apply ``APPROVE``/``REJECT`` to one persisted step; write exactly one
Task row and exactly one audit entry per successful call; publish the artifacts
of an approved dispatch; require and record an explicit ``actor`` and a non-empty
``reason``.

It may never: reach ``VERIFIED`` (no event it can apply leads there, and the
target is checked); render or override a realization verdict (it holds no
realization gate and no architecture port); bypass the FSM (the next state is a
pure lookup in the authoritative table and an illegal event fails closed before
a single byte is written or published); force an arbitrary Step state (only the
two declared targets are accepted); mutate an ``ArchitectureVersion``, an ADR or
an ACR (it holds none of those ports); perform a Human Override operation
(``pause``/``resume``/``set_mode``/``unblock``/``resolve``/``abort`` belong to
:class:`~.human_override.HumanOverride`); or own monitor/reporting behaviour.

Relationship to ``HumanOverride``
---------------------------------
``HumanOverride`` remains the *state-control* human path; this gate is the
*approval* human path. They are wired separately, hold no reference to each
other, and both are explicit and audited. A step can leave ``WAITING_APPROVAL``
only through ``approve``, ``reject``, or the override's ``abort_step``.

Crash safety (precise, not overstated)
--------------------------------------
Filesystem artifacts and the SQLite commit are **not** one atomic transaction -
that is impossible across two systems. What is guaranteed instead: **crash-safe**
(a crash before the commit leaves the pre-approval state authoritative and the
published artifacts deterministic), **replay-safe** (a repeated call after such a
crash republishes the same files and commits the same single rows) and
**fail-closed** (an illegal event, an unknown step or a missing actor/reason
writes and publishes nothing).

``REJECT`` semantics
--------------------
``reject`` applies the authoritative ``REJECT`` transition, which returns the
step to ``READY``. Because the approval policy is a pure function of the persisted
step and project, the loop then requests approval again on its next tick - the
gate deliberately does not invent an approval-exemption flag. To end a step
instead of re-requesting approval, a human uses ``abort_step``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any, Callable, Mapping

from ..domain.audit import AuditAction, AuditEntityType, AuditEntry
from ..domain.enums import StepEvent, StepState
from ..domain.fsm import InvalidTransitionError, StepStateMachine
from ..domain.models import Step, utc_now
from ..ports.capabilities import WorkerChannelPort
from ..ports.repositories import AuditRepository, StepRepository, TaskRepository
from ..ports.transactions import TransactionPort
from .context import ContextBuilder
from .dispatch import publish_task
from .orchestrator import DEFAULT_REPORT_SCHEMA, DEFAULT_TASK_INSTRUCTIONS

__all__ = [
    "ApprovalGate",
    "ApprovalError",
    "ApprovalActorRequiredError",
    "ApprovalReasonRequiredError",
    "ApprovalStepNotFoundError",
    "ApprovalTransitionError",
]


class ApprovalError(Exception):
    """Base class for approval errors (nothing was written or published)."""


class ApprovalActorRequiredError(ApprovalError):
    """Raised when an approval carries no explicit actor."""


class ApprovalReasonRequiredError(ApprovalError):
    """Raised when an approval carries no non-empty reason."""


class ApprovalStepNotFoundError(ApprovalError):
    """Raised when the referenced step does not exist."""


class ApprovalTransitionError(ApprovalError):
    """Raised when the authoritative FSM has no such transition.

    The step is left exactly as it was and nothing is written or published.
    """


class ApprovalGate:
    """Controlled human path for the ``APPROVE``/``REJECT`` FSM events.

    Depends on three repository ports, the one transaction port, the context
    builder and the worker channel - never on ``StoragePort``, an adapter, the
    monitor or the realization gate - and takes the clock by injection.
    """

    def __init__(
        self,
        steps: StepRepository,
        tasks: TaskRepository,
        audit: AuditRepository,
        transactions: TransactionPort,
        context_builder: ContextBuilder,
        worker: WorkerChannelPort,
        *,
        instructions: Mapping[str, Any] = DEFAULT_TASK_INSTRUCTIONS,
        report_schema: Mapping[str, Any] = DEFAULT_REPORT_SCHEMA,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not callable(clock):
            raise ValueError("clock must be a callable returning a datetime")
        if not isinstance(context_builder, ContextBuilder):
            raise ValueError(
                "context_builder must be a ContextBuilder; "
                f"got {type(context_builder).__name__}"
            )
        if not isinstance(worker, WorkerChannelPort):
            raise ValueError(
                "worker must implement WorkerChannelPort "
                "(dispatch/read_report/acknowledge_report); got "
                f"{type(worker).__name__}"
            )
        self._steps = steps
        self._tasks = tasks
        self._audit = audit
        self._transactions = transactions
        self._context_builder = context_builder
        self._worker = worker
        self._instructions = dict(instructions or {})
        self._report_schema = dict(report_schema or {})
        self._clock = clock

    # -- the two human events ----------------------------------------------

    def approve(self, step_no: int, *, actor: str, reason: str) -> Step:
        """``WAITING_APPROVAL -> DISPATCHED``: the human go-ahead.

        The transition is validated first (fail-closed: nothing is written or
        published), then the deterministic artifacts are published, then the step
        transition, the Task row and exactly one audit entry are committed
        together. A crash before that commit leaves ``WAITING_APPROVAL``
        authoritative and the artifacts deterministic, so a retry is a safe
        replay - not a duplicate.
        """
        _require_context(actor, reason)
        step = self._require_step(step_no)
        to_state = self._target(
            step,
            StepEvent.APPROVE,
            expected=StepState.DISPATCHED,
            operation="approve_step",
        )
        now = self._clock()
        attempt = max(step.attempt, 1)
        dispatched = publish_task(
            self._worker,
            self._context_builder,
            step,
            attempt=attempt,
            instructions=self._instructions,
            report_schema=self._report_schema,
            now=now,
        )
        fields: dict[str, Any] = {
            "state": to_state,
            "attempt": attempt,
            "last_update_at": now,
        }
        if step.started_at is None:
            fields["started_at"] = now
        updated = replace(step, **fields)
        entry = self._entry(
            step,
            to_state,
            StepEvent.APPROVE,
            operation="approve_step",
            actor=actor,
            reason=reason,
            attempt=attempt,
            now=now,
        )
        with self._transactions.transaction():
            self._steps.upsert(updated)
            self._tasks.upsert(dispatched)
            self._audit.append(entry)
        return updated

    def reject(self, step_no: int, *, actor: str, reason: str) -> Step:
        """``WAITING_APPROVAL -> READY``: the authoritative rejection event.

        No Task is written and no artifact is published - the step simply returns
        to ``READY``, where the approval policy decides again. One step write and
        exactly one audit entry, in one transaction.
        """
        _require_context(actor, reason)
        step = self._require_step(step_no)
        to_state = self._target(
            step,
            StepEvent.REJECT,
            expected=StepState.READY,
            operation="reject_step",
        )
        now = self._clock()
        updated = replace(step, state=to_state, last_update_at=now)
        entry = self._entry(
            step,
            to_state,
            StepEvent.REJECT,
            operation="reject_step",
            actor=actor,
            reason=reason,
            attempt=step.attempt,
            now=now,
        )
        with self._transactions.transaction():
            self._steps.upsert(updated)
            self._audit.append(entry)
        return updated

    # -- internals ---------------------------------------------------------

    def _target(
        self,
        step: Step,
        event: StepEvent,
        *,
        expected: StepState,
        operation: str,
    ) -> StepState:
        """The authoritative target of ``event``, or fail closed.

        The target is also pinned to what this gate is allowed to realize, so a
        future FSM change cannot silently widen the gate's authority - in
        particular nothing here may ever realize ``VERIFIED``.
        """
        try:
            to_state = StepStateMachine.from_step(step).next_state(event)
        except InvalidTransitionError as error:
            raise ApprovalTransitionError(
                f"{operation} is not legal for step {step.step_no} in state "
                f"{step.state.value}: {error}"
            ) from error
        if to_state is StepState.VERIFIED or to_state is not expected:
            raise ApprovalTransitionError(
                f"{operation} would move step {step.step_no} from "
                f"{step.state.value} to {to_state.value}; this gate may only "
                f"realize {expected.value}"
            )
        return to_state

    def _require_step(self, step_no: int) -> Step:
        if isinstance(step_no, bool) or not isinstance(step_no, int):
            raise ApprovalError(f"step_no must be an int; got {step_no!r}")
        step = self._steps.get(step_no)
        if step is None:
            raise ApprovalStepNotFoundError(
                f"step {step_no!r} does not exist"
            )
        return step

    def _entry(
        self,
        step: Step,
        to_state: StepState,
        event: StepEvent,
        *,
        operation: str,
        actor: str,
        reason: str,
        attempt: int,
        now: datetime,
    ) -> AuditEntry:
        """One audit entry for one human approval act."""
        return AuditEntry(
            entity_type=AuditEntityType.STEP,
            entity_id=str(step.step_no),
            action=AuditAction.UPDATE,
            detail={
                "operation": operation,
                "actor": actor,
                "reason": reason,
                "step_no": step.step_no,
                "event": event.value,
                "from": step.state.value,
                "to": to_state.value,
                "attempt": attempt,
            },
            created_at=now,
        )


def _require_context(actor: Any, reason: Any) -> None:
    """Fail closed unless the approval names both an actor and a reason.

    Called before any read, write or publication, so an anonymous or unexplained
    approval cannot even reach the source of truth.
    """
    if not isinstance(actor, str) or not actor.strip():
        raise ApprovalActorRequiredError(
            f"actor must be a non-empty string; got {actor!r}"
        )
    if not isinstance(reason, str) or not reason.strip():
        raise ApprovalReasonRequiredError(
            f"reason must be a non-empty string; got {reason!r}"
        )
