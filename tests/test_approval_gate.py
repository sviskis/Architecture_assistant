"""Step 23 - the human approval gate (the FIX for AQ-2).

The V1 final review proved that ``(WAITING_APPROVAL, APPROVE) -> DISPATCHED``
existed in the authoritative FSM while *no* human path could produce it, so every
approval-gated step (MANUAL always, SUPERVISED above LOW, AUTO above MEDIUM and
anything flagged ``requires_human``) could only be aborted.
:class:`~architecture_assistant.application.approval.ApprovalGate` closes that
gap for exactly two events - ``APPROVE`` and ``REJECT`` - without touching the
FSM, the schema or the loop.

What this module proves
-----------------------
* the applied targets are the **existing** FSM transitions, not invented ones
  (``APPROVE -> DISPATCHED``, ``REJECT -> READY``, read from the table);
* the gate realizes nothing else: no ``VERIFIED``, no realization verdict, no
  architecture/ADR/ACR write, no override operation, no monitor or reporting;
* an approval writes **one** Task row and **exactly one** audit entry in one
  transaction, and publishes the same deterministic artifacts the loop would;
* an illegal, repeated, anonymous or unexplained approval fails closed and
  writes (and publishes) nothing;
* crash safety, stated precisely and *not* as atomicity: filesystem publication
  and the SQLite commit live in different worlds, so the guarantees are
  **crash-safe** and **replay-safe** (a retry after a crash republishes the same
  files and commits the same single rows) and **fail-closed**;
* the five crash windows the Step 23 contract requires: A - death before
  publication (``WAITING_APPROVAL`` stays authoritative, nothing published);
  B - death after publication before the commit (rolled back, artifacts
  re-publishable); C - a clean commit (exactly one Task row, exactly one audit
  entry, the FSM-defined target state); D - a repeated approval after success
  (fails closed); E - full restart on file-backed SQLite (no duplicate dispatch
  or domain effect).

L1/L2/L3 fidelity matches the Step 22 recovery suite, and no Step 22 proof is
weakened by this module.
"""

from __future__ import annotations

import gc
import json
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest

from architecture_assistant.application import (
    ApprovalActorRequiredError,
    ApprovalError,
    ApprovalGate,
    ApprovalReasonRequiredError,
    ApprovalStepNotFoundError,
    ApprovalTransitionError,
)
from architecture_assistant.composition import (
    Composition,
    CompositionConfig,
    compose,
)
from architecture_assistant.domain.enums import (
    Mode,
    Phase,
    ReportStatus,
    RiskLevel,
    StepEvent,
    StepState,
    TaskState,
)
from architecture_assistant.domain.fsm import STEP_TRANSITIONS
from architecture_assistant.domain.models import Step
from architecture_assistant.infrastructure import ClineWorkerAdapter

#: A fixed stamp: no approval test depends on the wall clock.
NOW = datetime(2026, 9, 21, 22, 0, tzinfo=timezone.utc)

#: The step every test drives.
STEP_NO = 9

#: The subprocess crash driver shared with the Step 22 recovery suite.
CHILD = Path(__file__).with_name("_recovery_child.py")

#: The tree the realization gate inspects: the assistant itself.
REAL_SOURCE_ROOT = (
    Path(__file__).resolve().parents[1] / "src" / "architecture_assistant"
)


def fixed_clock() -> datetime:
    return NOW


def make_config(
    tmp_path: Any,
    *,
    timeout: Any = timedelta(hours=1),
    mode: Mode = Mode.AUTO,
) -> CompositionConfig:
    """A file-backed configuration: the database is a real file in ``tmp_path``."""
    return CompositionConfig(
        database_path=tmp_path / "assistant.db",
        exchange_dir=tmp_path / "cline",
        report_dir=tmp_path / "reports",
        source_root=REAL_SOURCE_ROOT,
        timeout=timeout,
        mode=mode,
        clock=fixed_clock,
    )


@pytest.fixture
def config(tmp_path) -> CompositionConfig:
    return make_config(tmp_path)


def make_step(
    state: StepState = StepState.READY,
    attempt: int = 1,
    max_attempts: int = 3,
    *,
    requires_human: bool = True,
) -> Step:
    """A step the approval policy gates (``requires_human`` by default)."""
    return Step(
        step_no=STEP_NO,
        phase=Phase.LOOP,
        title="Approval step",
        state=state,
        attempt=attempt,
        max_attempts=max_attempts,
        risk=RiskLevel.LOW,
        requires_human=requires_human,
        created_at=NOW,
        last_update_at=NOW,
    )


@contextmanager
def composed(config: CompositionConfig) -> Iterator[Composition]:
    """Compose and always close - the honest "the process ends" shape."""
    composition = compose(config)
    try:
        yield composition
    finally:
        composition.close()


def step_of(composition: Composition, step_no: int = STEP_NO) -> Step:
    step = composition.storage.steps.get(step_no)
    assert step is not None, step_no
    return step


def reach_waiting_approval(composition: Composition) -> None:
    """Drive the loop to ``WAITING_APPROVAL`` through the real policy."""
    composition.storage.steps.upsert(make_step())
    run = composition.run_until_idle()

    assert run.stopped_because == "human-approval-required"
    assert step_of(composition).state is StepState.WAITING_APPROVAL


def entries_of(composition: Composition, step_no: int = STEP_NO) -> list[Any]:
    """Every audit entry written for one step, in order."""
    return [
        entry
        for entry in composition.storage.audit.list()
        if entry.entity_id == str(step_no)
    ]


def adapter(config: CompositionConfig) -> ClineWorkerAdapter:
    return ClineWorkerAdapter(config.exchange_dir)


def task_path(config: CompositionConfig) -> Path:
    return adapter(config).task_path(STEP_NO, 1)


def context_path(config: CompositionConfig) -> Path:
    return adapter(config).context_path(STEP_NO, 1)


def report_path(config: CompositionConfig) -> Path:
    return adapter(config).report_path(STEP_NO, 1)


def write_report(config: CompositionConfig, status: ReportStatus) -> Path:
    """Publish a worker report exactly where the real channel expects it."""
    path = report_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "step_no": STEP_NO,
                "attempt": 1,
                "status": status.value,
                "summary": "approval test report",
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


def run_child(
    scenario: str, config: CompositionConfig
) -> subprocess.CompletedProcess:
    """Run one child scenario in a brand-new Python process."""
    return subprocess.run(
        [
            sys.executable,
            str(CHILD),
            scenario,
            str(config.database_path),
            str(config.exchange_dir),
            str(config.report_dir),
            str(config.source_root),
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )


def crash_child(scenario: str, config: CompositionConfig) -> str:
    """Run a crash scenario and prove the child really died on purpose."""
    completed = run_child(scenario, config)

    assert completed.returncode == 1, completed.stderr
    assert "CRASH_AT " in completed.stdout, completed.stdout
    assert "Traceback" not in completed.stderr, completed.stderr
    return completed.stdout.strip()


def bare_rows(database_path: Any, query: str) -> list[tuple]:
    """Read the database directly - no composition and no repository."""
    connection = sqlite3.connect(str(database_path))
    try:
        return [tuple(row) for row in connection.execute(query).fetchall()]
    finally:
        connection.close()


def snapshot(composition: Composition) -> tuple[Any, Any, Any]:
    """Everything an approval may touch: steps, tasks and the audit trail."""
    return (
        composition.storage.steps.list(),
        composition.storage.tasks.list(),
        composition.storage.audit.list(),
    )


def approval_entries(composition: Composition, operation: str) -> list[Any]:
    """Every audit entry the gate wrote for one operation."""
    return [
        entry
        for entry in entries_of(composition)
        if entry.detail.get("operation") == operation
    ]


class TestApprovalBoundaries:
    """The gate is exactly the two FSM events - and nothing else."""

    def test_the_public_api_is_only_approve_and_reject(self, config) -> None:
        with composed(config) as composition:
            api = {
                name
                for name in dir(composition.approval)
                if not name.startswith("_")
            }

        assert api == {"approve", "reject"}

    def test_the_gate_applies_the_existing_fsm_transitions(self) -> None:
        """The targets come from the authoritative table, not from this gate."""
        assert (
            STEP_TRANSITIONS[(StepState.WAITING_APPROVAL, StepEvent.APPROVE)]
            is StepState.DISPATCHED
        )
        assert (
            STEP_TRANSITIONS[(StepState.WAITING_APPROVAL, StepEvent.REJECT)]
            is StepState.READY
        )

    def test_the_gate_holds_only_the_ports_it_needs(self, config) -> None:
        with composed(config) as composition:
            assert set(vars(composition.approval)) == {
                "_steps",
                "_tasks",
                "_audit",
                "_transactions",
                "_context_builder",
                "_worker",
                "_instructions",
                "_report_schema",
                "_clock",
            }

    def test_the_gate_cannot_reach_read_architecture_or_override(
        self, config
    ) -> None:
        with composed(config) as composition:
            gate = composition.approval

            for missing in (
                "monitor",
                "report_builder",
                "realization_control",
                "architecture_versions",
                "adrs",
                "change_requests",
                "human_override",
                "cost_plugin",
                "pause_project",
                "unblock_step",
                "resolve_step",
                "abort_step",
                "set_mode",
            ):
                assert not hasattr(gate, missing), missing

    def test_the_constructor_refuses_a_foreign_context_builder(
        self, config
    ) -> None:
        with composed(config) as composition:
            with pytest.raises(ValueError, match="context_builder"):
                ApprovalGate(
                    composition.storage.steps,
                    composition.storage.tasks,
                    composition.storage.audit,
                    composition.storage,
                    object(),
                    composition.worker,
                )

    def test_the_constructor_refuses_a_foreign_worker(self, config) -> None:
        with composed(config) as composition:
            with pytest.raises(ValueError, match="worker"):
                ApprovalGate(
                    composition.storage.steps,
                    composition.storage.tasks,
                    composition.storage.audit,
                    composition.storage,
                    composition.context_builder,
                    object(),
                )

    def test_an_approval_without_an_actor_is_refused(self, config) -> None:
        with composed(config) as composition:
            reach_waiting_approval(composition)
            before = snapshot(composition)

            with pytest.raises(ApprovalActorRequiredError):
                composition.approval.approve(
                    STEP_NO, actor="   ", reason="looks fine"
                )

            assert snapshot(composition) == before
            assert not task_path(config).exists()
            assert not context_path(config).exists()

    def test_an_approval_without_a_reason_is_refused(self, config) -> None:
        with composed(config) as composition:
            reach_waiting_approval(composition)
            before = snapshot(composition)

            with pytest.raises(ApprovalReasonRequiredError):
                composition.approval.approve(
                    STEP_NO, actor="operator", reason=""
                )

            assert snapshot(composition) == before
            assert not task_path(config).exists()

    def test_a_rejection_without_an_actor_is_refused(self, config) -> None:
        with composed(config) as composition:
            reach_waiting_approval(composition)
            before = snapshot(composition)

            with pytest.raises(ApprovalActorRequiredError):
                composition.approval.reject(
                    STEP_NO, actor=None, reason="not good enough"
                )

            assert snapshot(composition) == before

    def test_the_actor_and_reason_are_checked_before_any_read(self, config) -> None:
        """An anonymous approval cannot even reach the source of truth."""
        with composed(config) as composition:
            with pytest.raises(ApprovalActorRequiredError):
                composition.approval.approve(
                    12345, actor=None, reason="unknown step anyway"
                )

    def test_an_unknown_step_is_refused(self, config) -> None:
        with composed(config) as composition:
            before = snapshot(composition)

            with pytest.raises(ApprovalStepNotFoundError):
                composition.approval.approve(
                    12345, actor="operator", reason="typo"
                )

            assert snapshot(composition) == before

    def test_a_foreign_step_number_type_is_refused(self, config) -> None:
        with composed(config) as composition:
            with pytest.raises(ApprovalError):
                composition.approval.approve(
                    "9", actor="operator", reason="string step number"
                )

    @pytest.mark.parametrize("state", list(StepState))
    def test_the_gate_never_realizes_verified(self, config, state) -> None:
        """From any persisted state the gate lands on DISPATCHED or nothing."""
        with composed(config) as composition:
            composition.storage.steps.upsert(make_step(state))
            before = snapshot(composition)

            try:
                updated = composition.approval.approve(
                    STEP_NO, actor="operator", reason="contract pin"
                )
            except ApprovalTransitionError:
                assert snapshot(composition) == before
                assert not task_path(config).exists()
                return

            assert updated.state is StepState.DISPATCHED
            assert updated.state is not StepState.VERIFIED


class TestApprove:
    """``APPROVE``: one transition, one Task row, one audit entry, artifacts."""

    def test_a_requested_approval_becomes_a_dispatched_step(self, config) -> None:
        with composed(config) as composition:
            reach_waiting_approval(composition)

            updated = composition.approval.approve(
                STEP_NO, actor="operator", reason="plan reviewed"
            )

            assert updated.state is StepState.DISPATCHED
            assert step_of(composition).state is StepState.DISPATCHED

    def test_the_attempt_and_the_start_clock_match_a_loop_dispatch(
        self, config
    ) -> None:
        with composed(config) as composition:
            reach_waiting_approval(composition)

            updated = composition.approval.approve(
                STEP_NO, actor="operator", reason="plan reviewed"
            )

            assert updated.attempt == 1  # approval never invents an attempt
            assert updated.started_at == NOW  # the same clock rule as the loop
            assert updated.last_update_at == NOW

    def test_exactly_one_task_row_is_written(self, config) -> None:
        with composed(config) as composition:
            reach_waiting_approval(composition)
            assert composition.storage.tasks.list() == ()

            composition.approval.approve(
                STEP_NO, actor="operator", reason="plan reviewed"
            )

            tasks = composition.storage.tasks.list()
            assert len(tasks) == 1
            assert (tasks[0].step_no, tasks[0].attempt) == (STEP_NO, 1)
            assert tasks[0].state is TaskState.DISPATCHED

    def test_exactly_one_audit_entry_records_the_human_act(self, config) -> None:
        with composed(config) as composition:
            reach_waiting_approval(composition)
            before = len(entries_of(composition))

            composition.approval.approve(
                STEP_NO, actor="operator", reason="plan reviewed"
            )

            written = approval_entries(composition, "approve_step")
            assert len(written) == 1
            assert len(entries_of(composition)) == before + 1

            entry = written[0]
            assert entry.entity_id == str(STEP_NO)
            assert entry.created_at == NOW
            assert entry.detail["event"] == "APPROVE"
            assert entry.detail["from"] == "WAITING_APPROVAL"
            assert entry.detail["to"] == "DISPATCHED"
            assert entry.detail["actor"] == "operator"
            assert entry.detail["reason"] == "plan reviewed"
            assert entry.detail["attempt"] == 1
            assert entry.detail["step_no"] == STEP_NO

    def test_the_task_and_context_artifacts_are_published_atomically(
        self, config
    ) -> None:
        with composed(config) as composition:
            reach_waiting_approval(composition)

            composition.approval.approve(
                STEP_NO, actor="operator", reason="plan reviewed"
            )

        assert task_path(config).is_file()
        assert context_path(config).is_file()
        # Atomic temp-file writes leave no sibling behind.
        assert list(config.exchange_dir.rglob("*.tmp")) == []

        task = json.loads(task_path(config).read_text(encoding="utf-8"))
        assert task["step_no"] == STEP_NO
        assert task["attempt"] == 1
        assert Path(task["context_file"]).is_file()
        assert len(bare_rows(config.database_path, "SELECT * FROM tasks")) == 1

    def test_the_gate_publishes_the_same_task_the_loop_would(
        self, config, tmp_path
    ) -> None:
        """One shared publication seam, so the two paths cannot drift."""
        with composed(config) as composition:
            reach_waiting_approval(composition)
            composition.approval.approve(
                STEP_NO, actor="operator", reason="parity check"
            )
            approved = json.loads(task_path(config).read_text(encoding="utf-8"))
            approved_context = json.loads(
                context_path(config).read_text(encoding="utf-8")
            )

        other = make_config(tmp_path / "loop")
        with composed(other) as composition:
            composition.storage.steps.upsert(make_step(requires_human=False))
            assert (
                composition.run_until_idle().stopped_because
                == "worker-report-pending"
            )
            published = json.loads(task_path(other).read_text(encoding="utf-8"))
            published_context = json.loads(
                context_path(other).read_text(encoding="utf-8")
            )

        # The only legitimate difference is the absolute artifact path: the two
        # compositions use two different exchange directories.
        approved.pop("context_file")
        published.pop("context_file")

        assert approved == published
        assert approved_context.keys() == published_context.keys()

    def test_an_approved_step_runs_to_verification(self, config) -> None:
        """AQ-2 resolved: an approval-gated step can now complete."""
        with composed(config) as composition:
            reach_waiting_approval(composition)
            assert composition.monitor.blocking_steps()[0].step_no == STEP_NO

            composition.approval.approve(
                STEP_NO, actor="operator", reason="plan reviewed"
            )

            assert (
                composition.run_until_idle().stopped_because
                == "worker-report-pending"
            )
            assert (
                composition.monitor.health().current_state
                is StepState.CLINE_WORKING
            )
            assert composition.monitor.blocking_steps() == ()

            write_report(config, ReportStatus.DONE)

            run = composition.run_until_idle()

            assert run.stopped_because == "all-steps-verified"
            assert step_of(composition).state is StepState.VERIFIED
            assert composition.health().complete is True
            assert len(list(adapter(config).archive_dir().glob("*.json"))) == 1
            assert not report_path(config).exists()


class TestApproveFailsClosed:
    """An illegal, repeated or premature approval writes and publishes nothing."""

    @pytest.mark.parametrize(
        "state",
        [
            StepState.PENDING,
            StepState.READY,
            StepState.DISPATCHED,
            StepState.CLINE_WORKING,
            StepState.REPORT_RECEIVED,
            StepState.REVIEWING,
            StepState.REVISE,
            StepState.FAILED,
            StepState.BLOCKED,
            StepState.CONFLICT,
            StepState.VERIFIED,
            StepState.ABORTED,
        ],
    )
    def test_only_waiting_approval_may_be_approved(self, config, state) -> None:
        with composed(config) as composition:
            composition.storage.steps.upsert(make_step(state))
            before = snapshot(composition)

            with pytest.raises(ApprovalTransitionError):
                composition.approval.approve(
                    STEP_NO, actor="operator", reason="eager"
                )

            assert snapshot(composition) == before
            assert not task_path(config).exists()
            assert not context_path(config).exists()

    def test_a_repeated_approval_after_success_fails_closed(self, config) -> None:
        """Case D: one approval, one dispatch - never a second."""
        with composed(config) as composition:
            reach_waiting_approval(composition)
            composition.approval.approve(
                STEP_NO, actor="operator", reason="first decision"
            )
            before = snapshot(composition)

            with pytest.raises(ApprovalTransitionError):
                composition.approval.approve(
                    STEP_NO, actor="operator", reason="changed my mind"
                )

            assert snapshot(composition) == before
            assert len(composition.storage.tasks.list()) == 1
            assert len(approval_entries(composition, "approve_step")) == 1

    def test_a_rejected_step_needs_a_new_request_before_approval(
        self, config
    ) -> None:
        with composed(config) as composition:
            reach_waiting_approval(composition)
            composition.approval.reject(
                STEP_NO, actor="operator", reason="rework required"
            )
            before = snapshot(composition)

            with pytest.raises(ApprovalTransitionError):
                composition.approval.approve(
                    STEP_NO, actor="operator", reason="reconsidered"
                )

            assert snapshot(composition) == before


class TestReject:
    """``REJECT``: the authoritative transition back to ``READY``, nothing else."""

    def test_a_rejected_approval_returns_the_step_to_ready(self, config) -> None:
        with composed(config) as composition:
            reach_waiting_approval(composition)

            updated = composition.approval.reject(
                STEP_NO, actor="operator", reason="not good enough"
            )

            assert updated.state is StepState.READY
            assert step_of(composition).state is StepState.READY
            assert updated.attempt == 1  # rejection never touches the attempt
            assert updated.last_update_at == NOW

    def test_reject_writes_one_audit_entry_and_no_task(self, config) -> None:
        with composed(config) as composition:
            reach_waiting_approval(composition)
            before = len(entries_of(composition))

            composition.approval.reject(
                STEP_NO, actor="operator", reason="not good enough"
            )

            written = approval_entries(composition, "reject_step")
            assert len(written) == 1
            assert len(entries_of(composition)) == before + 1
            assert written[0].detail["event"] == "REJECT"
            assert written[0].detail["from"] == "WAITING_APPROVAL"
            assert written[0].detail["to"] == "READY"
            assert written[0].detail["actor"] == "operator"
            assert written[0].detail["reason"] == "not good enough"
            assert composition.storage.tasks.list() == ()

        assert not task_path(config).exists()
        assert not context_path(config).exists()

    def test_the_loop_then_requests_approval_again(self, config) -> None:
        """Documented semantics: ``REJECT`` returns to ``READY``; the policy rules."""
        with composed(config) as composition:
            reach_waiting_approval(composition)
            composition.approval.reject(
                STEP_NO, actor="operator", reason="rework required"
            )

            run = composition.run_until_idle()

            assert run.stopped_because == "human-approval-required"
            assert step_of(composition).state is StepState.WAITING_APPROVAL
            assert step_of(composition).attempt == 1
            assert composition.storage.tasks.list() == ()

    @pytest.mark.parametrize(
        "state", [StepState.READY, StepState.DISPATCHED, StepState.VERIFIED]
    )
    def test_reject_from_another_state_fails_closed(self, config, state) -> None:
        with composed(config) as composition:
            composition.storage.steps.upsert(make_step(state))
            before = snapshot(composition)

            with pytest.raises(ApprovalTransitionError):
                composition.approval.reject(
                    STEP_NO, actor="operator", reason="not applicable"
                )

            assert snapshot(composition) == before

    def test_a_rejection_is_a_decision_not_a_stop(self, config) -> None:
        """``REJECT`` is not a termination - ``ABORT`` is the human stop.

        After a rejection the step is ``READY`` (where the authoritative FSM
        allows no abort), so the policy asks for approval again and the human
        then stops the step with the override - which is the documented pair of
        human paths.
        """
        with composed(config) as composition:
            reach_waiting_approval(composition)
            composition.approval.reject(
                STEP_NO, actor="operator", reason="rework required"
            )
            assert composition.health().complete is False

            composition.run_until_idle()
            assert step_of(composition).state is StepState.WAITING_APPROVAL

            composition.human_override.abort_step(
                STEP_NO, actor="operator", reason="stopping this step"
            )

            assert (
                composition.run_until_idle().stopped_because == "step-aborted"
            )
            assert step_of(composition).state is StepState.ABORTED


class TestApprovalRestart:
    """Case E: the approved dispatch survives a full close and recreate."""

    def test_an_approved_dispatch_survives_a_full_close_and_recreate(
        self, config
    ) -> None:
        first = compose(config)
        reach_waiting_approval(first)
        first.approval.approve(
            STEP_NO, actor="operator", reason="plan reviewed"
        )
        previous = {
            "connection": first.connection,
            "storage": first.storage,
            "orchestrator": first.orchestrator,
            "worker": first.worker,
            "approval": first.approval,
            "human_override": first.human_override,
        }

        first.close()
        with pytest.raises(sqlite3.ProgrammingError):
            previous["connection"].execute("SELECT 1")
        del first
        gc.collect()

        second = compose(config)
        try:
            for name, dead in previous.items():
                assert getattr(second, name) is not dead, name

            assert step_of(second).state is StepState.DISPATCHED
            assert step_of(second).attempt == 1
            assert len(second.storage.tasks.list()) == 1
            assert len(approval_entries(second, "approve_step")) == 1
            assert task_path(config).is_file()
            assert context_path(config).is_file()
            assert bare_rows(
                config.database_path, "SELECT state FROM steps"
            ) == [("DISPATCHED",)]
            assert bare_rows(
                config.database_path, "SELECT COUNT(*) FROM tasks"
            ) == [(1,)]

            run = second.run_until_idle()

            assert run.stopped_because == "worker-report-pending"
            assert step_of(second).state is StepState.CLINE_WORKING
            assert step_of(second).attempt == 1
            assert len(second.storage.tasks.list()) == 1
            assert len(approval_entries(second, "approve_step")) == 1
        finally:
            second.close()

    def test_the_file_channel_is_not_duplicated_by_the_restart(
        self, config
    ) -> None:
        with composed(config) as composition:
            reach_waiting_approval(composition)
            composition.approval.approve(
                STEP_NO, actor="operator", reason="plan reviewed"
            )

        published = task_path(config).read_bytes()

        with composed(config) as composition:
            assert (
                composition.run_until_idle().stopped_because
                == "worker-report-pending"
            )
            assert step_of(composition).state is StepState.CLINE_WORKING

        assert task_path(config).read_bytes() == published
        assert list(config.exchange_dir.rglob("*_task.json")) == [
            task_path(config)
        ]
        assert list(config.exchange_dir.rglob("*.tmp")) == []


class TestApprovalCrash:
    """Real subprocess deaths around the approval: cases A, B, C, D and E."""

    def test_case_a_a_death_before_publication_keeps_waiting_approval(
        self, config
    ) -> None:
        stdout = crash_child("approve_crash_before_publish", config)
        assert "worker-dispatch" in stdout

        with composed(config) as composition:
            assert step_of(composition).state is StepState.WAITING_APPROVAL
            assert composition.storage.tasks.list() == ()
            assert approval_entries(composition, "approve_step") == []

        assert not task_path(config).exists()
        assert not context_path(config).exists()

    def test_case_a_the_precondition_came_from_the_real_policy(
        self, config
    ) -> None:
        """``WAITING_APPROVAL`` is a loop decision, not a seeded magic state."""
        crash_child("approve_crash_before_publish", config)

        entries = bare_rows(
            config.database_path,
            "SELECT detail FROM audit_entries WHERE entity_type = 'STEP'",
        )

        assert len(entries) == 1
        assert json.loads(entries[0][0])["event"] == "REQUEST_APPROVAL"

    def test_case_b_a_death_after_publication_rolls_the_approval_back(
        self, config
    ) -> None:
        stdout = crash_child("approve_crash_before_commit", config)
        assert "audit-append" in stdout

        # The DB commit rolled back; the published artifacts are deterministic.
        assert bare_rows(
            config.database_path, "SELECT state FROM steps"
        ) == [("WAITING_APPROVAL",)]
        assert bare_rows(
            config.database_path, "SELECT COUNT(*) FROM tasks"
        ) == [(0,)]
        assert task_path(config).is_file()
        assert context_path(config).is_file()

    def test_case_b_the_approval_can_be_replayed_after_that_crash(
        self, config
    ) -> None:
        crash_child("approve_crash_before_commit", config)
        published = task_path(config).read_bytes()

        with composed(config) as composition:
            composition.approval.approve(
                STEP_NO, actor="operator", reason="retry after the crash"
            )

            assert step_of(composition).state is StepState.DISPATCHED
            assert len(composition.storage.tasks.list()) == 1
            assert len(approval_entries(composition, "approve_step")) == 1

        assert task_path(config).read_bytes() == published
        assert bare_rows(
            config.database_path, "SELECT COUNT(*) FROM tasks"
        ) == [(1,)]

    def test_case_c_a_clean_child_approval_commits_exactly_once(
        self, config
    ) -> None:
        completed = run_child("approve", config)

        assert completed.returncode == 0, completed.stderr
        assert "APPROVED" in completed.stdout
        assert "Traceback" not in completed.stderr

        with composed(config) as composition:
            assert step_of(composition).state is StepState.DISPATCHED
            assert step_of(composition).started_at is not None
            assert len(composition.storage.tasks.list()) == 1
            assert len(approval_entries(composition, "approve_step")) == 1

    def test_case_d_a_repeated_approval_after_the_child_succeeded_fails_closed(
        self, config
    ) -> None:
        run_child("approve", config)

        with composed(config) as composition:
            before = snapshot(composition)

            with pytest.raises(ApprovalTransitionError):
                composition.approval.approve(
                    STEP_NO, actor="operator", reason="again"
                )

            assert snapshot(composition) == before
            assert len(composition.storage.tasks.list()) == 1
            assert len(approval_entries(composition, "approve_step")) == 1

    def test_case_e_state_and_artifacts_survive_a_crash_after_the_commit(
        self, config
    ) -> None:
        stdout = crash_child("approve_then_crash", config)
        assert "after-approve-commit" in stdout

        published = task_path(config).read_bytes()

        with composed(config) as composition:
            assert step_of(composition).state is StepState.DISPATCHED
            assert len(composition.storage.tasks.list()) == 1

            run = composition.run_until_idle()

            assert run.stopped_because == "worker-report-pending"
            assert step_of(composition).state is StepState.CLINE_WORKING
            assert step_of(composition).attempt == 1
            assert len(composition.storage.tasks.list()) == 1
            assert len(approval_entries(composition, "approve_step")) == 1

        assert task_path(config).read_bytes() == published
        assert bare_rows(
            config.database_path, "SELECT COUNT(*) FROM tasks"
        ) == [(1,)]
