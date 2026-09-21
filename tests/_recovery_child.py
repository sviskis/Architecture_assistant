"""Subprocess crash driver for the Step 22 recovery tests.

Invoked by ``tests/test_recovery.py`` as::

    python tests/_recovery_child.py <scenario> <database_path> \
        <exchange_dir> <report_dir> <source_root>

Every crash scenario ends with ``os._exit(1)`` - no ``atexit`` handler, no
buffered flush, no ``close()`` and no rollback: exactly what a killed process
leaves behind. ``advance`` is the control scenario and exits ``0``.

Before dying, a crash scenario prints a ``CRASH_AT <point>`` marker. The parent
asserts that marker: a child that merely failed to start would also exit ``1``,
so the marker (plus "no traceback on stderr") is what makes the crash
*deliberate* instead of accidental.

This file is deliberately **not** a pytest module (it does not match
``test_*.py``): it is the crashed process, not a test.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

#: The step every crash scenario drives.
STEP_NO = 9

#: A fixed stamp: recovery tests never depend on the wall clock.
NOW = datetime(2026, 9, 21, 22, 0, tzinfo=timezone.utc)

#: The cost event identity reused by the cost scenarios. It **must** match
#: ``tests/test_recovery.py``, which replays exactly this event after the crash.
COST_EVENT_ID = "openai:recovery-event"

#: How many scenarios the CLI contract expects (program name + 5 arguments).
_ARGV_LENGTH = 6


def _bootstrap_library_path(source_root: str) -> str:
    """Make the package importable without touching ``pyproject.toml``.

    ``source_root`` is the tree the realization gate inspects; the importable
    library root is its parent (``.../src``).
    """
    resolved = Path(source_root).resolve()
    library_root = str(resolved.parent)
    if library_root not in sys.path:
        sys.path.insert(0, library_root)
    return library_root


def _clock() -> datetime:
    """The injected clock every child process uses."""
    return NOW


if len(sys.argv) >= _ARGV_LENGTH:
    # Deliberately before the package imports below: the child must be runnable
    # from any working directory without pyproject.toml help.
    _bootstrap_library_path(sys.argv[5])


from architecture_assistant.composition import (  # noqa: E402
    CompositionConfig,
    compose,
)
from architecture_assistant.domain.enums import (  # noqa: E402
    Mode,
    Phase,
    ReportStatus,
    RiskLevel,
    StepState,
)
from architecture_assistant.domain.models import Step  # noqa: E402
from architecture_assistant.infrastructure import (  # noqa: E402
    ClineWorkerAdapter,
)
from architecture_assistant.ports.capabilities import CostRecord  # noqa: E402


# ---------------------------------------------------------------------------
# crash injection
# ---------------------------------------------------------------------------

def _crash(point: str) -> None:
    """Announce where the process is about to die, then really die."""
    print(f"CRASH_AT {point}", flush=True)
    os._exit(1)


class Killer:
    """An audit port that kills the process **before** it writes.

    Killing before the append places the process death exactly between the two
    writes of one transaction: the domain write already happened, the audit
    entry never did.
    """

    def __init__(self, inner: object) -> None:
        self._inner = inner

    def append(self, entry: object) -> None:
        _crash("audit-append")

    def list(self) -> object:
        return getattr(self._inner, "list")()

    def list_for_entity(self, *args: object, **kwargs: object) -> object:
        return getattr(self._inner, "list_for_entity")(*args, **kwargs)


class KillerSteps:
    """A step port that kills the process when the step is written."""

    def __init__(self, inner: object) -> None:
        self._inner = inner

    def upsert(self, step: object) -> None:
        _crash("step-upsert")

    def get(self, step_no: int) -> object:
        return getattr(self._inner, "get")(step_no)

    def list(self) -> object:
        return getattr(self._inner, "list")()

    def list_by_state(self, state: object) -> object:
        return getattr(self._inner, "list_by_state")(state)

    def delete(self, step_no: int) -> object:
        return getattr(self._inner, "delete")(step_no)


class CrashingDispatch:
    """A worker that kills the process when the task is published."""

    def __init__(self, inner: object) -> None:
        self._inner = inner

    def dispatch(self, request: object) -> None:
        _crash("worker-dispatch")

    def __getattr__(self, name: str) -> object:
        return getattr(object.__getattribute__(self, "_inner"), name)


class CrashingAck:
    """A worker that kills the process instead of acknowledging the report.

    Everything else is delegated, so the loop keeps working normally until the
    acknowledgement - which is exactly the window F-1 is about.
    """

    def __init__(self, inner: object) -> None:
        self._inner = inner

    def acknowledge_report(self, step_no: int, attempt: int) -> None:
        _crash("acknowledge_report")

    def __getattr__(self, name: str) -> object:
        return getattr(object.__getattribute__(self, "_inner"), name)


# ---------------------------------------------------------------------------
# journeys
# ---------------------------------------------------------------------------

def _make_step(
    *,
    state: StepState = StepState.PENDING,
    attempt: int = 0,
    max_attempts: int = 3,
    requires_human: bool = False,
) -> Step:
    """The step every crash scenario drives."""
    return Step(
        step_no=STEP_NO,
        phase=Phase.LOOP,
        title="Recovery step",
        state=state,
        attempt=attempt,
        max_attempts=max_attempts,
        risk=RiskLevel.LOW,
        requires_human=requires_human,
        created_at=NOW,
        last_update_at=NOW,
    )


def _row(composition: object) -> Step:
    """The persisted step, read back through the composition's own storage."""
    step = composition.storage.steps.get(STEP_NO)
    if step is None:
        raise SystemExit("the scenario step is not persisted")
    return step


def _seed(
    composition: object,
    *,
    state: StepState = StepState.PENDING,
    attempt: int = 0,
    max_attempts: int = 3,
    requires_human: bool = False,
) -> None:
    composition.storage.steps.upsert(
        _make_step(
            state=state,
            attempt=attempt,
            max_attempts=max_attempts,
            requires_human=requires_human,
        )
    )


def _reach_waiting_approval(composition: object) -> None:
    """Drive the loop to ``WAITING_APPROVAL`` through the real approval policy."""
    _seed(
        composition,
        state=StepState.READY,
        attempt=1,
        requires_human=True,
    )
    _tick_until(composition, StepState.WAITING_APPROVAL)


def _arm_approval_worker(composition: object, worker: object) -> None:
    """Arm a worker on the approval gate's own reference."""
    composition.approval._worker = worker
    if composition.approval._worker is not worker:
        raise SystemExit("the approval worker injection did not take effect")


def _arm_approval_audit_killer(composition: object) -> None:
    """Arm the killer on the audit port the approval gate actually holds."""
    composition.approval._audit = Killer(composition.approval._audit)
    if not isinstance(composition.approval._audit, Killer):
        raise SystemExit("the approval audit injection did not take effect")


def _tick_until(
    composition: object, state: StepState, *, limit: int = 20
) -> None:
    """Tick until the persisted step reaches ``state`` (fail loudly otherwise)."""
    for _ in range(limit):
        if _row(composition).state is state:
            return
        composition.orchestrator.run_once()
    raise SystemExit(f"the loop never reached {state}")


def _cost_record(config: object) -> CostRecord:
    return CostRecord(
        provider="openai",
        event_id=COST_EVENT_ID,
        input_tokens=10,
        output_tokens=2,
        cost_usd=0.5,
        pricing_known=True,
        project=config.project_name,
        step_no=STEP_NO,
        created_at=NOW,
    )


def _write_report(
    config: object, status: ReportStatus, *, attempt: int = 1
) -> Path:
    """Publish a worker report exactly where the real channel expects it."""
    path = ClineWorkerAdapter(config.exchange_dir).report_path(STEP_NO, attempt)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "step_no": STEP_NO,
                "attempt": attempt,
                "status": status.value,
                "summary": f"recovery child report ({status.value})",
                "files_created": [],
                "files_changed": [],
                "files_deleted": [],
                "issues": [],
                "architecture_questions": [],
                "dependencies_added": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def _journey_to_report(
    composition: object, config: object, status: ReportStatus
) -> None:
    """Drive the loop to ``CLINE_WORKING``, then publish a report for it."""
    _seed(composition, attempt=1)
    composition.run_until_idle()
    if _row(composition).state is not StepState.CLINE_WORKING:
        raise SystemExit("the journey did not reach CLINE_WORKING")
    _write_report(config, status, attempt=1)


# ---------------------------------------------------------------------------
# scenarios
# ---------------------------------------------------------------------------

def _advance(composition: object, config: object) -> None:
    """Control scenario: a full advance that exits cleanly (code 0)."""
    _seed(composition, attempt=1)
    composition.run_until_idle()
    print("ADVANCED", flush=True)


def _crash_after_dispatch_commit(composition: object, config: object) -> None:
    """Die right after a dispatch has been committed."""
    _seed(composition, attempt=1)
    _tick_until(composition, StepState.DISPATCHED)
    _crash("after-dispatch-commit")


def _crash_after_report_written(composition: object, config: object) -> None:
    _journey_to_report(composition, config, ReportStatus.DONE)
    _crash("after-report-written")


def _crash_after_report_received(composition: object, config: object) -> None:
    _journey_to_report(composition, config, ReportStatus.DONE)
    composition.orchestrator.run_once()
    _crash("after-report-received")


def _crash_in_reviewing(composition: object, config: object) -> None:
    _journey_to_report(composition, config, ReportStatus.DONE)
    _tick_until(composition, StepState.REVIEWING)
    _crash("in-reviewing")


def _crash_after_verified_before_ack(
    composition: object, config: object
) -> None:
    """The F-1 window: the outcome commits, the acknowledgement never happens."""
    _journey_to_report(composition, config, ReportStatus.DONE)
    worker = composition.orchestrator._worker
    composition.orchestrator._worker = CrashingAck(worker)
    if composition.orchestrator._worker is worker:
        raise SystemExit("the acknowledgement injection did not take effect")
    composition.run_until_idle()
    raise SystemExit("the acknowledgement injection never fired")


def _crash_inside_transition(composition: object, config: object) -> None:
    """Die between the step write and the audit write of one transaction."""
    _seed(composition, attempt=1)
    composition.storage._audit = Killer(composition.storage._audit)
    composition.orchestrator.run_once()
    raise SystemExit("the audit injection never fired")


def _arm_override_audit_killer(composition: object) -> None:
    """Arm the killer on the audit port the override use-case actually holds.

    ``HumanOverride`` is wired with its own repository references, so swapping
    ``storage._audit`` would not reach it - the injection must target the exact
    port the use-case uses.
    """
    composition.human_override._audit = Killer(composition.human_override._audit)
    if not isinstance(composition.human_override._audit, Killer):
        raise SystemExit("the override audit injection did not take effect")


def _crash_inside_override_step(composition: object, config: object) -> None:
    _seed(composition, state=StepState.BLOCKED, attempt=1)
    _arm_override_audit_killer(composition)
    composition.human_override.unblock_step(
        STEP_NO, actor="operator", reason="recovery child"
    )
    raise SystemExit("the audit injection never fired")


def _crash_inside_override_project(composition: object, config: object) -> None:
    _seed(composition, attempt=1)
    _arm_override_audit_killer(composition)
    composition.human_override.pause_project(
        actor="operator", reason="recovery child"
    )
    raise SystemExit("the audit injection never fired")


def _crash_inside_cost(composition: object, config: object) -> None:
    """Die inside one transaction that has already written a cost record."""
    _seed(composition, attempt=1)
    composition.storage._steps = KillerSteps(composition.storage._steps)
    with composition.storage.transaction():
        composition.cost_plugin.record(_cost_record(config))
        composition.storage.steps.upsert(_row(composition))
    raise SystemExit("the step injection never fired")


def _crash_after_cost_commit(composition: object, config: object) -> None:
    _seed(composition, attempt=1)
    composition.cost_plugin.record(_cost_record(config))
    _crash("after-cost-commit")


def _crash_during_retry(composition: object, config: object) -> None:
    """Die between the attempt bump and the audit write of the retry."""
    _journey_to_report(composition, config, ReportStatus.REVISE)
    _tick_until(composition, StepState.REVISE)
    composition.storage._audit = Killer(composition.storage._audit)
    composition.orchestrator.run_once()
    raise SystemExit("the audit injection never fired")


# ---------------------------------------------------------------------------
# approval scenarios (Step 23): the human APPROVE path
# ---------------------------------------------------------------------------

def _approve(composition: object, config: object) -> None:
    """Control scenario: a human approval that exits cleanly (code 0)."""
    _reach_waiting_approval(composition)
    composition.approval.approve(
        STEP_NO, actor="operator", reason="recovery child"
    )
    print("APPROVED", flush=True)


def _approve_then_crash(composition: object, config: object) -> None:
    """Die immediately after the approval commit."""
    _reach_waiting_approval(composition)
    composition.approval.approve(
        STEP_NO, actor="operator", reason="recovery child"
    )
    _crash("after-approve-commit")


def _approve_crash_before_publish(composition: object, config: object) -> None:
    """Die before any worker artifact is published (case A)."""
    _reach_waiting_approval(composition)
    _arm_approval_worker(
        composition, CrashingDispatch(composition.approval._worker)
    )
    composition.approval.approve(
        STEP_NO, actor="operator", reason="recovery child"
    )
    raise SystemExit("the dispatch injection never fired")


def _approve_crash_before_commit(composition: object, config: object) -> None:
    """Die after publication, before the DB commit of the approval (case B)."""
    _reach_waiting_approval(composition)
    _arm_approval_audit_killer(composition)
    composition.approval.approve(
        STEP_NO, actor="operator", reason="recovery child"
    )
    raise SystemExit("the approval audit injection never fired")


_SCENARIOS = {
    "advance": _advance,
    "crash_after_dispatch_commit": _crash_after_dispatch_commit,
    "crash_after_report_written": _crash_after_report_written,
    "crash_after_report_received": _crash_after_report_received,
    "crash_in_reviewing": _crash_in_reviewing,
    "crash_after_verified_before_ack": _crash_after_verified_before_ack,
    "crash_inside_transition": _crash_inside_transition,
    "crash_inside_override_step": _crash_inside_override_step,
    "crash_inside_override_project": _crash_inside_override_project,
    "crash_inside_cost": _crash_inside_cost,
    "crash_after_cost_commit": _crash_after_cost_commit,
    "crash_during_retry": _crash_during_retry,
    "approve": _approve,
    "approve_then_crash": _approve_then_crash,
    "approve_crash_before_publish": _approve_crash_before_publish,
    "approve_crash_before_commit": _approve_crash_before_commit,
}


def main(argv: list[str]) -> int:
    """Run one scenario; ``os._exit(1)`` happens inside the scenario itself."""
    if len(argv) != _ARGV_LENGTH:
        print(
            f"usage: {argv[0]} <scenario> <database> <exchange> "
            "<reports> <source_root>",
            flush=True,
        )
        return 2
    scenario = argv[1]
    if scenario not in _SCENARIOS:
        print(f"unknown scenario {scenario!r}", flush=True)
        return 2

    config = CompositionConfig(
        database_path=Path(argv[2]),
        exchange_dir=Path(argv[3]),
        report_dir=Path(argv[4]),
        mode=Mode.AUTO,
        timeout=timedelta(hours=1),
        source_root=Path(argv[5]),
        clock=_clock,
    )
    composition = compose(config)
    _SCENARIOS[scenario](composition, config)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
