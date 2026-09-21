"""Human Override - the controlled **state-control human write path**.

This module exists for one reason: a human must be able to change a specific
project / workflow state deliberately, and that act must be impossible to
perform silently. Every public method therefore requires an explicit ``actor``
and a non-empty ``reason``, and performs

* **exactly one domain mutation**,
* **exactly one audit entry**, and
* **one transaction boundary** around both,

so an override is either fully durable and fully traceable, or not written at
all. There is no "best effort" path and no silent state change anywhere here.

What it may do
--------------
* ``pause_project`` / ``resume_project`` - the project runtime flag. The loop
  only *respects* ``Project.paused`` (:class:`~.orchestrator.Orchestrator`), so
  this is the only writer of it.
* ``set_mode`` - the project operating mode, i.e. the input of the approval
  policy.
* ``unblock_step`` / ``resolve_step`` / ``abort_step`` - the **human events the
  FSM already defines**: ``UNBLOCK`` (``BLOCKED -> READY``), ``RESOLVE``
  (``CONFLICT -> READY``), ``ABORT`` (``WAITING_APPROVAL``/``BLOCKED``/
  ``CONFLICT``/``FAILED -> ABORTED``).

What it may never do
--------------------
* **It never bypasses the FSM.** The next state comes from
  :class:`~architecture_assistant.domain.fsm.StepStateMachine` - the
  authoritative table - and nothing else. An event that is not legal from the
  persisted state fails closed with :class:`OverrideTransitionError` before a
  single byte is written, so no domain invariant is ever circumvented quietly.
* **It cannot reach ``VERIFIED``.** No method exposes ``VERIFY`` (or any other
  non-human event: ``PREPARE``, ``REQUEST_APPROVAL``, ``DISPATCH``,
  ``CLINE_START``, ``RECEIVE_REPORT``, ``START_REVIEW``, ``REQUEST_REVISE``,
  ``RAISE_CONFLICT``, ``BLOCK``, ``FAIL``), so a human can never declare a step
  verified, never out-vote the realization gate and never rewrite an
  architecture verdict. ``VERIFIED`` and ``ABORTED`` are terminal in the FSM, so
  a verified outcome is literally untouchable from here.
* **It is not a workflow repair mechanism.** It does not invent transitions,
  does not reset ``attempt``/``max_attempts``, does not touch Tasks and does not
  fill gaps in the FSM. One such gap is deliberately left **unresolved** and is
  recorded in the Step 20 report instead of being papered over here: the
  ``REVISE``-with-exhausted-attempts dead end (kept as a documented V1
  limitation because the loop path cannot reach it). The other Step 20 gap -
  ``APPROVE -> DISPATCHED`` without a guaranteed task publication - was closed
  in Step 23 by :class:`~.approval.ApprovalGate`, a separate approval path that
  owns only ``APPROVE``/``REJECT``; it is deliberately **not** implemented here.
* **It is not part of the loop or of any decision.** Nothing in the
  orchestrator, the scheduler, the review policy or the realization gate
  consults it, and it holds no reference to the read-only monitor.

Separation of powers
--------------------
:class:`~.monitor.Monitor` is READ; :class:`HumanOverride` is CONTROLLED WRITE
for *state control*, while :class:`~.approval.ApprovalGate` is the separate
CONTROLLED WRITE path for the ``APPROVE``/``REJECT`` decisions. They are wired
separately in the composition root and are never handed to each other. This
module imports ``domain`` and ``ports`` only - never ``infrastructure``, never
``sqlite3``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any, Callable, Mapping

from ..domain.audit import AuditAction, AuditEntityType, AuditEntry
from ..domain.enums import Mode, StepEvent, StepState
from ..domain.fsm import InvalidTransitionError, StepStateMachine
from ..domain.models import Project, Step
from ..ports.repositories import (
    AuditRepository,
    ProjectRepository,
    StepRepository,
)
from ..ports.transactions import TransactionPort

__all__ = [
    "HumanOverride",
    "HumanOverrideError",
    "OverrideActorRequiredError",
    "OverrideReasonRequiredError",
    "OverrideProjectNotFoundError",
    "OverrideStepNotFoundError",
    "OverrideInvariantError",
    "OverrideNoChangeError",
    "OverrideTransitionError",
]


class HumanOverrideError(Exception):
    """Base class for human-override errors (nothing was written)."""


class OverrideActorRequiredError(HumanOverrideError):
    """Raised when an override carries no explicit actor."""


class OverrideReasonRequiredError(HumanOverrideError):
    """Raised when an override carries no non-empty reason."""


class OverrideProjectNotFoundError(HumanOverrideError):
    """Raised when the source of truth contains no project to control."""


class OverrideStepNotFoundError(HumanOverrideError):
    """Raised when the referenced step does not exist."""


class OverrideInvariantError(HumanOverrideError):
    """Raised when the source of truth violates a singleton invariant."""


class OverrideNoChangeError(HumanOverrideError):
    """Raised when the requested override would change nothing.

    Deliberately an error rather than a silent no-op: a no-op without an audit
    entry would be a hidden decision, and a no-op *with* one would record a
    change that never happened.
    """


class OverrideTransitionError(HumanOverrideError):
    """Raised when the authoritative FSM has no such transition.

    The step is left exactly as it was and nothing is written.
    """


class HumanOverride:
    """Controlled human write path over the project and Step workflow state.

    Depends on three repository ports and the one transaction port - never on
    ``StoragePort``, an adapter or the monitor - and takes the clock by
    injection, so it never reads the system time itself. Every method is a
    single, atomic, audited operation.
    """

    def __init__(
        self,
        projects: ProjectRepository,
        steps: StepRepository,
        audit: AuditRepository,
        transactions: TransactionPort,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        if not callable(clock):
            raise ValueError("clock must be a callable returning a datetime")
        self._projects = projects
        self._steps = steps
        self._audit = audit
        self._transactions = transactions
        self._clock = clock

    # -- project control ---------------------------------------------------

    def pause_project(self, *, actor: str, reason: str) -> Project:
        """Pause the project: the loop stops until a human resumes it.

        One project write + one ``PAUSE`` audit entry, in one transaction. The
        loop only *respects* ``Project.paused``; this method is its only writer.
        """
        _require_context(actor, reason)
        project = self._single_project()
        if project.paused:
            raise OverrideNoChangeError(
                f"project {project.name!r} is already paused"
            )
        now = self._clock()
        updated = replace(project, paused=True, updated_at=now)
        self._commit_project(
            updated,
            AuditAction.PAUSE,
            {
                "operation": "pause_project",
                "actor": actor,
                "reason": reason,
                "paused_before": False,
                "paused_after": True,
                "mode": project.mode.value,
            },
            now,
        )
        return updated

    def resume_project(self, *, actor: str, reason: str) -> Project:
        """Resume the project: the loop may advance again.

        One project write + one ``RESUME`` audit entry, in one transaction.
        """
        _require_context(actor, reason)
        project = self._single_project()
        if not project.paused:
            raise OverrideNoChangeError(
                f"project {project.name!r} is not paused"
            )
        now = self._clock()
        updated = replace(project, paused=False, updated_at=now)
        self._commit_project(
            updated,
            AuditAction.RESUME,
            {
                "operation": "resume_project",
                "actor": actor,
                "reason": reason,
                "paused_before": True,
                "paused_after": False,
                "mode": project.mode.value,
            },
            now,
        )
        return updated

    def set_mode(self, mode: Mode, *, actor: str, reason: str) -> Project:
        """Set the project operating mode - the input of the approval policy.

        One project write + one ``SET_MODE`` audit entry, in one transaction.
        The mode is the declaration of how much autonomy the operator grants;
        it never changes the authoritative review rules themselves.
        """
        _require_context(actor, reason)
        if not isinstance(mode, Mode):
            raise HumanOverrideError(f"mode must be a Mode; got {mode!r}")
        project = self._single_project()
        if project.mode is mode:
            raise OverrideNoChangeError(
                f"project {project.name!r} is already in mode {mode.value}"
            )
        now = self._clock()
        updated = replace(project, mode=mode, updated_at=now)
        self._commit_project(
            updated,
            AuditAction.SET_MODE,
            {
                "operation": "set_mode",
                "actor": actor,
                "reason": reason,
                "mode_before": project.mode.value,
                "mode_after": mode.value,
                "paused": project.paused,
            },
            now,
        )
        return updated

    # -- step workflow control (authoritative FSM events only) -------------

    def unblock_step(
        self, step_no: int, *, actor: str, reason: str
    ) -> Step:
        """``BLOCKED -> READY`` - the human event the loop waits for.

        Without it a blocked step would wait for a human forever: the loop
        halts on ``BLOCKED`` and never performs the ``UNBLOCK`` itself.
        """
        return self._apply_step_event(
            step_no,
            StepEvent.UNBLOCK,
            operation="unblock_step",
            actor=actor,
            reason=reason,
        )

    def resolve_step(
        self, step_no: int, *, actor: str, reason: str
    ) -> Step:
        """``CONFLICT -> READY`` - the human conflict-resolution event."""
        return self._apply_step_event(
            step_no,
            StepEvent.RESOLVE,
            operation="resolve_step",
            actor=actor,
            reason=reason,
        )

    def abort_step(self, step_no: int, *, actor: str, reason: str) -> Step:
        """``WAITING_APPROVAL``/``BLOCKED``/``CONFLICT``/``FAILED -> ABORTED``.

        The human stop. ``ABORTED`` is terminal, and it never counts as
        verified - the project is complete only when every step reached
        ``VERIFIED``.
        """
        return self._apply_step_event(
            step_no,
            StepEvent.ABORT,
            operation="abort_step",
            actor=actor,
            reason=reason,
        )

    def _apply_step_event(
        self,
        step_no: int,
        event: StepEvent,
        *,
        operation: str,
        actor: str,
        reason: str,
    ) -> Step:
        """Apply exactly one authoritative FSM event to one persisted step.

        The next state is a pure lookup in
        :class:`~architecture_assistant.domain.fsm.StepStateMachine`; an event
        that is not legal from the persisted state fails closed **before** the
        transaction is even opened. Only ``state``/``last_update_at`` (plus
        ``finished_at`` on ``ABORTED``) change - ``attempt`` is recorded for
        provenance and never rewritten, no Task is touched, and no other Step
        field is altered.
        """
        _require_context(actor, reason)
        step = self._require_step(step_no)
        try:
            to_state = StepStateMachine.from_step(step).next_state(event)
        except InvalidTransitionError as error:
            raise OverrideTransitionError(
                f"{operation} is not legal for step {step.step_no} in state "
                f"{step.state.value}: {error}"
            ) from error
        now = self._clock()
        fields: dict[str, Any] = {
            "state": to_state,
            "last_update_at": now,
        }
        if to_state is StepState.ABORTED:
            fields["finished_at"] = now
        updated = replace(step, **fields)
        entry = AuditEntry(
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
                "attempt": step.attempt,
            },
            created_at=now,
        )
        with self._transactions.transaction():
            self._steps.upsert(updated)
            self._audit.append(entry)
        return updated

    # -- internals ---------------------------------------------------------

    def _single_project(self) -> Project:
        projects = self._projects.list()
        if not projects:
            raise OverrideProjectNotFoundError(
                "the source of truth contains no project"
            )
        if len(projects) > 1:
            names = ", ".join(sorted(project.name for project in projects))
            raise OverrideInvariantError(
                f"expected exactly one project, found {len(projects)}: {names}"
            )
        return projects[0]

    def _require_step(self, step_no: int) -> Step:
        if isinstance(step_no, bool) or not isinstance(step_no, int):
            raise HumanOverrideError(
                f"step_no must be an int; got {step_no!r}"
            )
        step = self._steps.get(step_no)
        if step is None:
            raise OverrideStepNotFoundError(f"step {step_no!r} does not exist")
        return step

    def _commit_project(
        self,
        project: Project,
        action: AuditAction,
        detail: Mapping[str, Any],
        now: datetime,
    ) -> None:
        """Persist the project write and exactly one audit entry atomically."""
        entry = AuditEntry(
            entity_type=AuditEntityType.PROJECT,
            entity_id=project.name,
            action=action,
            detail=dict(detail),
            created_at=now,
        )
        with self._transactions.transaction():
            self._projects.upsert(project)
            self._audit.append(entry)


def _require_context(actor: Any, reason: Any) -> None:
    """Fail closed unless the override names both an actor and a reason.

    Called before any read and any write, so an anonymous or unexplained
    override cannot even reach the source of truth.
    """
    if not isinstance(actor, str) or not actor.strip():
        raise OverrideActorRequiredError(
            f"actor must be a non-empty string; got {actor!r}"
        )
    if not isinstance(reason, str) or not reason.strip():
        raise OverrideReasonRequiredError(
            f"reason must be a non-empty string; got {reason!r}"
        )
