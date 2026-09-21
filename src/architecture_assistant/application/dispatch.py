"""Shared task publication - the one place a Step becomes a dispatched Task.

Two callers may realize the ``DISPATCH`` side of the workflow, and both use this
module and nothing else:

* the loop (:class:`~architecture_assistant.application.orchestrator.Orchestrator`),
  when the approval policy allows an automatic dispatch;
* the explicit human approval
  (:class:`~architecture_assistant.application.approval.ApprovalGate`), when a
  human approves an approval the loop already requested.

Order is the contract. Filesystem artifacts and the SQLite commit are **not** one
atomic transaction - they cannot be - so the guarantee is stated precisely:

1. the deterministic artifacts are built and published first (stable names,
   atomic temp-file plus ``os.replace`` inside the worker adapter);
2. only then does the caller persist the transition in one transaction.

A crash before (2) therefore leaves the step in its pre-dispatch state with
published, deterministic artifacts that the next attempt simply overwrites
(at-least-once delivery), while a persisted ``DISPATCHED`` can never exist
without its artifacts. That is **crash-safe, replay-safe and fail-closed** - not
atomic, and not claimed to be.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any, Mapping

from ..domain.enums import TaskEvent, TaskState
from ..domain.fsm import TaskStateMachine
from ..domain.models import Step, Task
from ..ports.capabilities import WorkerChannelPort, WorkerRequest
from .context import ContextBuilder

__all__ = ["build_task", "publish_task"]


def build_task(
    step: Step,
    *,
    attempt: int,
    instructions: Mapping[str, Any],
    report_schema: Mapping[str, Any],
    now: datetime,
) -> Task:
    """Build the subordinate Task for one dispatch of ``step``.

    Pure: no clock read, no storage access, no side effect - the caller supplies
    the exact attempt being dispatched and the stamp, so the task content is a
    deterministic function of the persisted step.
    """
    return Task(
        step_no=step.step_no,
        phase=step.phase,
        title=step.title,
        description=step.description,
        risk=step.risk,
        attempt=attempt,
        max_attempts=step.max_attempts,
        state=TaskState.CREATED,
        context_file=None,
        instructions=dict(instructions),
        report_schema=dict(report_schema),
        created_at=now,
    )


def publish_task(
    worker: WorkerChannelPort,
    context_builder: ContextBuilder,
    step: Step,
    *,
    attempt: int,
    instructions: Mapping[str, Any],
    report_schema: Mapping[str, Any],
    now: datetime,
) -> Task:
    """Publish the context and task artifacts, then return the dispatched Task.

    The context comes from the injected :class:`ContextBuilder` (never from an
    ad-hoc query) and is published before the task, which is the commit marker of
    the file channel.
    """
    snapshot = context_builder.build(step.step_no)
    task = build_task(
        step,
        attempt=attempt,
        instructions=instructions,
        report_schema=report_schema,
        now=now,
    )
    worker.dispatch(WorkerRequest(task=task, context=snapshot.to_dict()))
    return replace(
        task,
        state=TaskStateMachine.from_task(task).next_state(TaskEvent.DISPATCH),
    )
