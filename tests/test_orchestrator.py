"""Tests for the event-driven orchestrator.

Covered properties (mandated for Step 9): one persisted transition per tick with
exactly one audit entry, the approval policy per mode, every review outcome,
retry with the attempt increment in the same transaction, timeout that never
discards an available report, restart recovery, ``read -> persist -> ack``
ordering, and the "ABORTED/BLOCKED/CONFLICT never activates the next step" rule.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from architecture_assistant.application import (
    ContextBuilder,
    LoopInvariantError,
    LoopStatus,
    Orchestrator,
)
from architecture_assistant.domain.audit import (
    AuditAction,
    AuditEntityType,
    AuditEntry,
)
from architecture_assistant.domain.enums import (
    Mode,
    Phase,
    ReportStatus,
    RiskLevel,
    StepState,
    TaskState,
)
from architecture_assistant.domain.models import Project, Step
from architecture_assistant.infrastructure import (
    SqliteStorage,
    close_database,
    open_database,
)
from architecture_assistant.ports.capabilities import (
    RealizationControlPort,
    RealizationGate,
    WorkerRequest,
    WorkerResult,
)
from architecture_assistant.ports.repositories import TaskKey

NOW = datetime(2026, 9, 20, 20, 0, tzinfo=timezone.utc)
PROJECT = "Architecture Lifecycle Assistant"


class FakeClock:
    """Deterministic, advanceable clock."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **parts: float) -> None:
        self.now = self.now + timedelta(**parts)


def result(
    status: ReportStatus = ReportStatus.DONE, **overrides
) -> WorkerResult:
    payload = {
        "status": status,
        "summary": "step finished",
        "artifacts": (),
        "issues": (),
        "architecture_questions": (),
    }
    payload.update(overrides)
    return WorkerResult(**payload)


class FakeWorker:
    """In-memory :class:`WorkerChannelPort` implementation."""

    def __init__(
        self,
        *,
        auto_publish: bool = False,
        report: WorkerResult | None = None,
    ) -> None:
        self.auto_publish = auto_publish
        self.report = report or result()
        self.available: dict[tuple[int, int], WorkerResult] = {}
        self.dispatched: list[WorkerRequest] = []
        self.acknowledged: list[tuple[int, int]] = []
        self.ack_error: Exception | None = None
        self.dispatch_error: Exception | None = None

    def publish(
        self, step_no: int, attempt: int, report: WorkerResult | None = None
    ) -> None:
        self.available[(step_no, attempt)] = report or self.report

    def dispatch(self, request: WorkerRequest):
        if self.dispatch_error is not None:
            raise self.dispatch_error
        self.dispatched.append(request)
        if self.auto_publish:
            self.publish(request.task.step_no, request.task.attempt)
        return ("dispatch", request.task.step_no, request.task.attempt)

    def read_report(self, step_no: int, attempt: int):
        return self.available.get((step_no, attempt))

    def acknowledge_report(self, step_no: int, attempt: int):
        if self.ack_error is not None:
            raise self.ack_error
        if (step_no, attempt) not in self.available:
            raise RuntimeError(f"no report for {step_no}/{attempt}")
        self.acknowledged.append((step_no, attempt))
        del self.available[(step_no, attempt)]
        return f"archived-{step_no}-{attempt}"


class CountingContextBuilder(ContextBuilder):
    """A ContextBuilder that records its calls (proves it is used)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[int] = []

    def build(self, step_no: int):
        self.calls.append(step_no)
        return super().build(step_no)


class FakeRealizationControl:
    """A ``RealizationControlPort`` fake for loop tests.

    Defaults to *passing*, so the Step 9 loop behaviour can be tested without
    the architecture layer; the Step 10 gate tests configure it explicitly.
    """

    def __init__(
        self,
        *,
        compliant: bool = True,
        error: Exception | None = None,
        baseline_version: str = "1.1",
    ) -> None:
        self.compliant = compliant
        self.error = error
        self.baseline_version = baseline_version
        self.calls: list[tuple[int, int]] = []

    def gate(self, step_no: int, attempt: int) -> RealizationGate:
        self.calls.append((step_no, attempt))
        if self.error is not None:
            raise self.error
        finding_ids = (
            () if self.compliant else (f"finding-{step_no:03d}-{attempt:03d}-deadbeef",)
        )
        return RealizationGate(
            compliant=self.compliant,
            step_no=step_no,
            attempt=attempt,
            baseline_version=self.baseline_version,
            violation_count=len(finding_ids),
            finding_ids=finding_ids,
            decision_id=f"realization-step-{step_no:03d}-attempt-{attempt:03d}",
        )


def make_step(
    step_no: int = 9,
    *,
    state: StepState = StepState.READY,
    attempt: int = 1,
    max_attempts: int = 3,
    risk: RiskLevel = RiskLevel.LOW,
    requires_human: bool = False,
    phase: Phase = Phase.LOOP,
    title: str = "Orchestrator + Scheduler",
    started_at: datetime | None = None,
    last_update_at: datetime | None = None,
) -> Step:
    return Step(
        step_no=step_no,
        phase=phase,
        title=title,
        state=state,
        attempt=attempt,
        max_attempts=max_attempts,
        risk=risk,
        requires_human=requires_human,
        created_at=NOW,
        started_at=started_at,
        last_update_at=last_update_at,
    )


def seed(
    storage: SqliteStorage,
    *steps: Step,
    mode: Mode = Mode.AUTO,
    paused: bool = False,
) -> None:
    storage.projects.upsert(
        Project(
            name=PROJECT,
            plan_version="0.3",
            mode=mode,
            paused=paused,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    for step in steps or (make_step(),):
        storage.steps.upsert(step)


@pytest.fixture
def connection():
    conn = open_database(":memory:")
    yield conn
    close_database(conn)


@pytest.fixture
def storage(connection) -> SqliteStorage:
    return SqliteStorage(connection)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def worker() -> FakeWorker:
    return FakeWorker()


@pytest.fixture
def builder(storage, clock) -> CountingContextBuilder:
    return CountingContextBuilder(
        storage.projects,
        storage.steps,
        storage.adrs,
        storage.risks,
        storage.architecture_versions,
        clock=clock,
    )


@pytest.fixture
def realization() -> FakeRealizationControl:
    return FakeRealizationControl()


@pytest.fixture
def orchestrator(storage, worker, builder, realization, clock) -> Orchestrator:
    return Orchestrator(
        storage,
        worker,
        builder,
        realization_control=realization,
        clock=clock,
        timeout=timedelta(hours=1),
    )


def step_row(storage: SqliteStorage, step_no: int = 9) -> Step:
    step = storage.steps.get(step_no)
    assert step is not None
    return step


def audit_rows(storage: SqliteStorage) -> tuple[AuditEntry, ...]:
    return tuple(
        entry
        for entry in storage.audit.list()
        if entry.entity_type is AuditEntityType.STEP
    )


def run_ticks(orchestrator: Orchestrator, count: int):
    """Run ``count`` ticks and return their results."""
    return [orchestrator.run_once() for _ in range(count)]


class TestConstruction:
    def test_worker_must_implement_the_channel_port(
        self, storage, builder, realization, clock
    ) -> None:
        with pytest.raises(ValueError, match="WorkerChannelPort"):
            Orchestrator(
                storage,
                object(),
                builder,
                realization_control=realization,
                clock=clock,
            )

    def test_context_builder_is_required(
        self, storage, worker, realization, clock
    ) -> None:
        with pytest.raises(ValueError, match="ContextBuilder"):
            Orchestrator(
                storage,
                worker,
                object(),
                realization_control=realization,
                clock=clock,
            )

    def test_clock_must_be_callable(
        self, storage, worker, builder, realization
    ) -> None:
        with pytest.raises(ValueError, match="clock must be a callable"):
            Orchestrator(
                storage,
                worker,
                builder,
                realization_control=realization,
                clock="nope",
            )

    def test_timeout_must_be_positive(
        self, storage, worker, builder, realization, clock
    ) -> None:
        with pytest.raises(ValueError, match="timeout must be positive"):
            Orchestrator(
                storage,
                worker,
                builder,
                realization_control=realization,
                clock=clock,
                timeout=timedelta(0),
            )

    def test_timeout_can_be_disabled(
        self, storage, worker, builder, realization, clock
    ) -> None:
        value = Orchestrator(
            storage,
            worker,
            builder,
            realization_control=realization,
            clock=clock,
            timeout=None,
        )
        assert value.timeout is None

    def test_instructions_and_report_schema_are_configurable(
        self, storage, worker, builder, realization, clock
    ) -> None:
        value = Orchestrator(
            storage,
            worker,
            builder,
            realization_control=realization,
            clock=clock,
            instructions={"scope": "custom"},
            report_schema={"status": "text"},
        )
        assert value is not None

    def test_realization_control_is_mandatory(
        self, storage, worker, builder, clock
    ) -> None:
        """The loop must never be buildable without the architecture gate."""
        with pytest.raises(TypeError):
            Orchestrator(storage, worker, builder, clock=clock)  # type: ignore[call-arg]

    def test_a_missing_realization_gate_is_rejected(
        self, storage, worker, builder, clock
    ) -> None:
        with pytest.raises(ValueError, match="RealizationControlPort"):
            Orchestrator(
                storage,
                worker,
                builder,
                realization_control=None,  # type: ignore[arg-type]
                clock=clock,
            )

    def test_an_object_without_gate_is_rejected(
        self, storage, worker, builder, clock
    ) -> None:
        with pytest.raises(ValueError, match="RealizationControlPort"):
            Orchestrator(
                storage,
                worker,
                builder,
                realization_control=object(),  # type: ignore[arg-type]
                clock=clock,
            )

    def test_the_gate_is_exposed(self, storage, worker, builder, realization, clock) -> None:
        value = Orchestrator(
            storage,
            worker,
            builder,
            realization_control=realization,
            clock=clock,
        )
        assert value.realization_control is realization
        assert isinstance(realization, RealizationControlPort)


class TestSourceOfTruthInvariants:
    def test_no_project_is_an_error(self, storage, orchestrator) -> None:
        with pytest.raises(LoopInvariantError, match="no project"):
            orchestrator.run_once()

    def test_two_projects_are_an_error(
        self, storage, orchestrator
    ) -> None:
        seed(storage)
        storage.projects.upsert(
            Project(name="another", plan_version="0.3", created_at=NOW)
        )
        with pytest.raises(LoopInvariantError, match="exactly one project"):
            orchestrator.run_once()

    def test_no_steps_reports_no_steps(self, storage, orchestrator) -> None:
        storage.projects.upsert(
            Project(name=PROJECT, plan_version="0.3", created_at=NOW)
        )
        tick = orchestrator.run_once()
        assert tick.status is LoopStatus.NO_STEPS
        assert tick.reason == "no-steps"

    def test_paused_project_does_nothing(self, storage, orchestrator) -> None:
        seed(storage, paused=True)
        tick = orchestrator.run_once()
        assert tick.status is LoopStatus.PAUSED
        assert step_row(storage).state is StepState.READY
        assert audit_rows(storage) == ()

    def test_health_reflects_pause(self, storage, orchestrator) -> None:
        seed(storage, paused=True)
        assert orchestrator.is_paused() is True
        assert orchestrator.step_count() == 1


class TestHappyPath:
    def test_pending_is_prepared_with_an_attempt_number(
        self, storage, orchestrator
    ) -> None:
        seed(storage, make_step(state=StepState.PENDING, attempt=0))
        tick = orchestrator.run_once()
        assert tick.event is not None and tick.event.value == "PREPARE"
        step = step_row(storage)
        assert step.state is StepState.READY
        assert step.attempt == 1

    def test_full_loop_to_verified(self, storage, worker, orchestrator) -> None:
        seed(storage)

        first = orchestrator.run_once()
        assert first.status is LoopStatus.TRANSITIONED
        assert first.to_state is StepState.DISPATCHED
        assert len(worker.dispatched) == 1
        assert step_row(storage).started_at == NOW

        second = orchestrator.run_once()
        assert second.to_state is StepState.CLINE_WORKING

        third = orchestrator.run_once()
        assert third.status is LoopStatus.WAITING
        assert third.reason == "worker-report-pending"
        assert third.from_state is StepState.CLINE_WORKING

        worker.publish(9, 1)
        fourth = orchestrator.run_once()
        assert fourth.to_state is StepState.REPORT_RECEIVED

        fifth = orchestrator.run_once()
        assert fifth.to_state is StepState.REVIEWING
        assert worker.acknowledged == []  # the outcome is not durable yet

        sixth = orchestrator.run_once()
        assert sixth.to_state is StepState.VERIFIED
        assert sixth.acknowledged is True
        assert worker.acknowledged == [(9, 1)]

        step = step_row(storage)
        assert step.verified_at == NOW
        assert step.finished_at == NOW
        assert step.last_update_at == NOW

        assert orchestrator.run_once().status is LoopStatus.COMPLETE
        assert orchestrator.is_complete() is True

    def test_one_audit_entry_per_persisted_transition(
        self, storage, worker, orchestrator
    ) -> None:
        seed(storage)
        ticks = [
            orchestrator.run_once(),  # READY -> DISPATCHED
            orchestrator.run_once(),  # DISPATCHED -> CLINE_WORKING
            orchestrator.run_once(),  # WAITING for the worker
        ]
        worker.publish(9, 1)
        ticks.append(orchestrator.run_once())  # -> REPORT_RECEIVED
        ticks.append(orchestrator.run_once())  # -> REVIEWING
        ticks.append(orchestrator.run_once())  # -> VERIFIED

        entries = audit_rows(storage)
        assert len(entries) == len(
            [tick for tick in ticks if tick.transitioned]
        )
        assert all(
            entry.entity_type is AuditEntityType.STEP
            and entry.entity_id == "9"
            and entry.action is AuditAction.UPDATE
            for entry in entries
        )
        assert [entry.detail["to"] for entry in entries] == [
            "DISPATCHED",
            "CLINE_WORKING",
            "REPORT_RECEIVED",
            "REVIEWING",
            "VERIFIED",
        ]

    def test_audit_entry_carries_the_full_transition(
        self, storage, orchestrator
    ) -> None:
        seed(storage)
        orchestrator.run_once()
        detail = dict(audit_rows(storage)[0].detail)
        assert detail == {
            "step_no": 9,
            "event": "DISPATCH",
            "from": "READY",
            "to": "DISPATCHED",
            "attempt": 1,
            "reason": "dispatched",
        }

    def test_context_is_built_by_the_injected_builder(
        self, storage, worker, builder, orchestrator
    ) -> None:
        seed(storage)
        orchestrator.run_once()
        assert builder.calls == [9]
        request = worker.dispatched[0]
        assert request.task.step_no == 9
        context = dict(request.context)
        assert context["step"]["step_no"] == 9
        assert context["project"]["name"] == PROJECT
        assert context["schema_version"] == "1.0"

    def test_dispatch_persists_the_subordinate_task(
        self, storage, worker, orchestrator
    ) -> None:
        seed(storage)
        orchestrator.run_once()
        task = storage.tasks.get(TaskKey(9, 1))
        assert task is not None
        assert task.state is TaskState.DISPATCHED
        assert task.instructions
        assert task.report_schema

    def test_receiving_a_report_marks_the_task_reported(
        self, storage, worker, orchestrator
    ) -> None:
        seed(storage)
        orchestrator.run_once()
        worker.publish(9, 1, result(ReportStatus.REVISE))
        run_ticks(orchestrator, 2)  # DISPATCHED -> CLINE_WORKING -> REPORTED
        task = storage.tasks.get(TaskKey(9, 1))
        assert task is not None
        assert task.state is TaskState.REPORTED
        assert task.report_status is ReportStatus.REVISE

    def test_tick_result_is_json_safe(self, storage, orchestrator) -> None:
        seed(storage)
        tick = orchestrator.run_once()
        assert json.loads(json.dumps(tick.to_dict())) == tick.to_dict()
        assert tick.to_dict()["status"] == "TRANSITIONED"

    def test_a_failed_dispatch_never_leaves_a_dispatched_step(
        self, storage, worker, orchestrator
    ) -> None:
        seed(storage)
        worker.dispatch_error = RuntimeError("the channel is down")
        with pytest.raises(RuntimeError, match="channel is down"):
            orchestrator.run_once()
        assert step_row(storage).state is StepState.READY
        assert audit_rows(storage) == ()

        worker.dispatch_error = None
        tick = orchestrator.run_once()
        assert tick.to_state is StepState.DISPATCHED
        assert len(worker.dispatched) == 1


class TestApprovalPolicyInTheLoop:
    def test_manual_waits_for_approval(
        self, storage, worker, orchestrator
    ) -> None:
        seed(storage, make_step(risk=RiskLevel.LOW), mode=Mode.MANUAL)
        tick = orchestrator.run_once()
        assert tick.to_state is StepState.WAITING_APPROVAL
        assert worker.dispatched == []
        assert orchestrator.run_once().reason == "human-approval-required"
        assert step_row(storage).state is StepState.WAITING_APPROVAL

    def test_supervised_low_dispatches(
        self, storage, worker, orchestrator
    ) -> None:
        seed(storage, make_step(risk=RiskLevel.LOW), mode=Mode.SUPERVISED)
        assert orchestrator.run_once().to_state is StepState.DISPATCHED
        assert len(worker.dispatched) == 1

    def test_supervised_medium_waits(
        self, storage, worker, orchestrator
    ) -> None:
        seed(storage, make_step(risk=RiskLevel.MEDIUM), mode=Mode.SUPERVISED)
        assert orchestrator.run_once().to_state is StepState.WAITING_APPROVAL
        assert worker.dispatched == []

    def test_auto_medium_dispatches(self, storage, worker, orchestrator) -> None:
        seed(storage, make_step(risk=RiskLevel.MEDIUM), mode=Mode.AUTO)
        assert orchestrator.run_once().to_state is StepState.DISPATCHED

    def test_auto_high_waits(self, storage, worker, orchestrator) -> None:
        seed(storage, make_step(risk=RiskLevel.HIGH), mode=Mode.AUTO)
        assert orchestrator.run_once().to_state is StepState.WAITING_APPROVAL
        assert worker.dispatched == []

    def test_explicit_human_flag_waits_even_in_auto(
        self, storage, worker, orchestrator
    ) -> None:
        seed(
            storage,
            make_step(risk=RiskLevel.LOW, requires_human=True),
            mode=Mode.AUTO,
        )
        assert orchestrator.run_once().to_state is StepState.WAITING_APPROVAL
        assert worker.dispatched == []


def reach_reviewing(
    storage: SqliteStorage,
    worker: FakeWorker,
    orchestrator: Orchestrator,
    report: WorkerResult,
    *,
    step: Step,
    mode: Mode = Mode.AUTO,
) -> None:
    """Seed ``step`` and advance the loop until it is REVIEWING."""
    seed(storage, step, mode=mode)
    orchestrator.run_once()  # READY -> DISPATCHED
    worker.publish(step.step_no, step.attempt, report)
    orchestrator.run_once()  # DISPATCHED -> REPORT_RECEIVED
    orchestrator.run_once()  # REPORT_RECEIVED -> REVIEWING
    assert step_row(storage, step.step_no).state is StepState.REVIEWING


class TestReviewOutcomes:
    def test_done_verifies_and_acknowledges(
        self, storage, worker, orchestrator
    ) -> None:
        reach_reviewing(storage, worker, orchestrator, result(), step=make_step())
        tick = orchestrator.run_once()
        assert tick.to_state is StepState.VERIFIED
        assert tick.reason == "report-done"
        assert tick.acknowledged is True
        assert worker.acknowledged == [(9, 1)]

    def test_revise_enters_the_revise_state(
        self, storage, worker, orchestrator
    ) -> None:
        reach_reviewing(
            storage, worker, orchestrator, result(ReportStatus.REVISE),
            step=make_step(),
        )
        tick = orchestrator.run_once()
        assert tick.to_state is StepState.REVISE
        assert tick.attempt == 1
        assert worker.acknowledged == [(9, 1)]

    def test_blocked_halts_the_loop_and_acknowledges(
        self, storage, worker, orchestrator
    ) -> None:
        reach_reviewing(
            storage, worker, orchestrator, result(ReportStatus.BLOCKED),
            step=make_step(),
        )
        tick = orchestrator.run_once()
        assert tick.to_state is StepState.BLOCKED
        assert orchestrator.run_once().reason == "human-unblock-required"
        assert worker.acknowledged == [(9, 1)]

    def test_failed_status_is_retried_while_attempts_remain(
        self, storage, worker, orchestrator
    ) -> None:
        reach_reviewing(
            storage,
            worker,
            orchestrator,
            result(ReportStatus.FAILED),
            step=make_step(attempt=1, max_attempts=3),
        )
        tick = orchestrator.run_once()
        assert tick.to_state is StepState.REVISE
        assert tick.reason == "report-failed-retry"
        assert worker.acknowledged == [(9, 1)]

    def test_failed_status_escalates_once_attempts_are_spent(
        self, storage, worker, orchestrator
    ) -> None:
        reach_reviewing(
            storage,
            worker,
            orchestrator,
            result(ReportStatus.FAILED),
            step=make_step(attempt=3, max_attempts=3),
        )
        tick = orchestrator.run_once()
        assert tick.to_state is StepState.BLOCKED
        assert tick.reason == "failed-attempts-exhausted"

    def test_architecture_questions_block_the_step(
        self, storage, worker, orchestrator
    ) -> None:
        reach_reviewing(
            storage,
            worker,
            orchestrator,
            result(
                ReportStatus.DONE,
                architecture_questions=(
                    "should the composition layer import architecture?",
                ),
            ),
            step=make_step(),
        )
        tick = orchestrator.run_once()
        assert tick.to_state is StepState.BLOCKED
        assert tick.reason == "architecture-questions"

    def test_unresolved_issues_block_the_step(
        self, storage, worker, orchestrator
    ) -> None:
        reach_reviewing(
            storage,
            worker,
            orchestrator,
            result(ReportStatus.DONE, issues=("tests are red",)),
            step=make_step(),
        )
        tick = orchestrator.run_once()
        assert tick.to_state is StepState.BLOCKED
        assert tick.reason == "unresolved-issues"


class TestRetryAndAbort:
    def test_retry_advances_the_attempt_and_returns_to_ready(
        self, storage, worker, orchestrator
    ) -> None:
        reach_reviewing(
            storage, worker, orchestrator, result(ReportStatus.REVISE),
            step=make_step(attempt=1),
        )
        orchestrator.run_once()  # -> REVISE
        before = len(audit_rows(storage))

        tick = orchestrator.run_once()  # RETRY

        assert tick.from_state is StepState.REVISE
        assert tick.to_state is StepState.READY
        assert tick.attempt == 2
        step = step_row(storage)
        assert step.state is StepState.READY
        assert step.attempt == 2

        entries = audit_rows(storage)
        assert len(entries) == before + 1
        assert entries[-1].detail["attempt"] == 2
        assert entries[-1].detail["from"] == "REVISE"
        assert entries[-1].detail["to"] == "READY"

    def test_the_next_dispatch_uses_the_new_attempt(
        self, storage, worker, orchestrator
    ) -> None:
        reach_reviewing(
            storage, worker, orchestrator, result(ReportStatus.REVISE),
            step=make_step(attempt=1),
        )
        run_ticks(orchestrator, 2)  # REVISE -> READY
        orchestrator.run_once()  # READY -> DISPATCHED again
        assert worker.dispatched[-1].task.attempt == 2
        assert step_row(storage).attempt == 2

    def test_revise_with_exhausted_attempts_escalates_to_a_human(
        self, storage, worker, orchestrator
    ) -> None:
        reach_reviewing(
            storage,
            worker,
            orchestrator,
            result(ReportStatus.REVISE),
            step=make_step(attempt=3, max_attempts=3),
        )
        tick = orchestrator.run_once()
        assert tick.to_state is StepState.BLOCKED
        assert tick.reason == "revise-attempts-exhausted"
        assert orchestrator.run_once().status is LoopStatus.WAITING
        assert orchestrator.is_complete() is False

    def test_failed_status_retries_through_revise_while_attempts_remain(
        self, storage, worker, orchestrator
    ) -> None:
        reach_reviewing(
            storage,
            worker,
            orchestrator,
            result(ReportStatus.FAILED),
            step=make_step(attempt=1, max_attempts=3),
        )
        orchestrator.run_once()  # -> REVISE (a worker failure with budget left)
        tick = orchestrator.run_once()
        assert tick.to_state is StepState.READY
        assert tick.attempt == 2

    def test_the_timeout_failure_path_retries_then_aborts(
        self, storage, worker, orchestrator, clock
    ) -> None:
        seed(storage, make_step(attempt=3, max_attempts=3))
        orchestrator.run_once()  # -> DISPATCHED
        clock.advance(hours=2)
        assert orchestrator.run_once().to_state is StepState.FAILED
        assert orchestrator.run_once().to_state is StepState.ABORTED
        assert step_row(storage).finished_at == clock.now

    def test_blocked_keeps_the_loop_waiting(
        self, storage, worker, orchestrator
    ) -> None:
        reach_reviewing(
            storage,
            worker,
            orchestrator,
            result(ReportStatus.BLOCKED),
            step=make_step(attempt=1),
        )
        run_ticks(orchestrator, 1)  # -> BLOCKED
        assert orchestrator.run_once().status is LoopStatus.WAITING


class TestTimeout:
    def test_the_deadline_fails_a_silent_worker(
        self, storage, worker, orchestrator, clock
    ) -> None:
        seed(storage)
        orchestrator.run_once()  # READY -> DISPATCHED (timestamps recorded)
        assert step_row(storage).last_update_at == NOW

        clock.advance(hours=2)
        tick = orchestrator.run_once()

        assert tick.to_state is StepState.FAILED
        assert tick.reason == "timeout"
        assert audit_rows(storage)[-1].detail["reason"] == "timeout"

    def test_the_deadline_applies_while_the_worker_is_working(
        self, storage, worker, orchestrator, clock
    ) -> None:
        seed(storage)
        run_ticks(orchestrator, 1)  # -> DISPATCHED
        clock.advance(minutes=30)
        tick = orchestrator.run_once()
        assert tick.to_state is StepState.CLINE_WORKING
        assert step_row(storage).last_update_at == clock.now

        clock.advance(hours=2)
        assert orchestrator.run_once().to_state is StepState.FAILED

    def test_an_available_report_beats_the_deadline(
        self, storage, worker, orchestrator, clock
    ) -> None:
        seed(storage)
        orchestrator.run_once()  # -> DISPATCHED
        worker.publish(9, 1)
        clock.advance(hours=5)

        tick = orchestrator.run_once()

        assert tick.to_state is StepState.REPORT_RECEIVED
        assert tick.reason == "report-received"

    def test_without_a_timestamp_the_deadline_is_inconclusive(
        self, storage, worker, orchestrator, clock
    ) -> None:
        seed(storage, make_step(state=StepState.CLINE_WORKING))
        clock.advance(hours=99)
        tick = orchestrator.run_once()
        assert tick.status is LoopStatus.WAITING
        assert tick.reason == "worker-report-pending"

    def test_the_deadline_can_be_disabled(
        self, storage, worker, builder, realization, clock
    ) -> None:
        seed(storage)
        value = Orchestrator(
            storage,
            worker,
            builder,
            realization_control=realization,
            clock=clock,
            timeout=None,
        )
        value.run_once()  # -> DISPATCHED
        clock.advance(hours=99)
        assert value.run_once().to_state is StepState.CLINE_WORKING
        clock.advance(hours=99)
        assert value.run_once().status is LoopStatus.WAITING


class TestRestartRecovery:
    """A "restart" is simply a new orchestrator over the same source of truth."""

    def restart(
        self, storage, worker, builder, clock, realization=None
    ) -> Orchestrator:
        """Build a *new* orchestrator over the same source of truth."""
        return Orchestrator(
            storage,
            worker,
            builder,
            realization_control=realization or FakeRealizationControl(),
            clock=clock,
            timeout=timedelta(hours=1),
        )

    def test_restart_resumes_from_the_persisted_state(
        self, storage, worker, builder, clock, orchestrator
    ) -> None:
        seed(storage)
        orchestrator.run_once()  # READY -> DISPATCHED
        worker.publish(9, 1)

        resumed = self.restart(storage, worker, builder, clock)

        assert resumed.run_once().to_state is StepState.REPORT_RECEIVED

    def test_crash_after_report_received_before_ack(
        self, storage, worker, builder, clock, orchestrator
    ) -> None:
        seed(storage)
        orchestrator.run_once()  # -> DISPATCHED
        worker.publish(9, 1)
        orchestrator.run_once()  # -> REPORT_RECEIVED
        assert step_row(storage).state is StepState.REPORT_RECEIVED
        assert worker.available  # still on the channel, not acked
        assert worker.acknowledged == []

        resumed = self.restart(storage, worker, builder, clock)
        assert resumed.run_once().to_state is StepState.REVIEWING
        # The outcome is still not durable, so the report must survive.
        assert worker.available
        assert worker.acknowledged == []

        assert resumed.run_once().to_state is StepState.VERIFIED
        assert worker.acknowledged == [(9, 1)]
        assert worker.available == {}

    def test_crash_in_reviewing_before_ack(
        self, storage, worker, builder, clock, orchestrator
    ) -> None:
        reach_reviewing(
            storage, worker, orchestrator, result(), step=make_step()
        )
        assert step_row(storage).state is StepState.REVIEWING
        assert worker.available  # never discarded before the outcome exists

        resumed = self.restart(storage, worker, builder, clock)
        tick = resumed.run_once()

        assert tick.to_state is StepState.VERIFIED
        assert worker.acknowledged == [(9, 1)]
        # Exactly one outcome: the un-acked report was re-read, not re-applied.
        targets = [entry.detail["to"] for entry in audit_rows(storage)]
        assert targets.count("VERIFIED") == 1

    def test_a_restart_after_the_outcome_never_reprocesses(
        self, storage, worker, builder, clock, orchestrator
    ) -> None:
        reach_reviewing(
            storage, worker, orchestrator, result(), step=make_step()
        )
        orchestrator.run_once()  # -> VERIFIED + ack
        before = len(audit_rows(storage))

        resumed = self.restart(storage, worker, builder, clock)
        assert resumed.run_once().status is LoopStatus.COMPLETE
        assert len(audit_rows(storage)) == before

    def test_a_failing_acknowledgement_does_not_lose_the_outcome(
        self, storage, worker, builder, clock, orchestrator
    ) -> None:
        reach_reviewing(
            storage, worker, orchestrator, result(), step=make_step()
        )
        worker.ack_error = RuntimeError("the archive is read-only")

        tick = orchestrator.run_once()

        assert tick.to_state is StepState.VERIFIED
        assert tick.acknowledged is False
        assert tick.ack_error is not None
        assert "read-only" in tick.ack_error
        assert worker.available  # stays on the channel, but the outcome is durable

        worker.ack_error = None
        resumed = self.restart(storage, worker, builder, clock)
        assert resumed.run_once().status is LoopStatus.COMPLETE
        assert step_row(storage).state is StepState.VERIFIED


class TestCurrentAndNext:
    def test_current_is_the_lowest_step_that_is_not_verified(
        self, storage, orchestrator
    ) -> None:
        seed(
            storage,
            make_step(9, state=StepState.VERIFIED),
            make_step(10, state=StepState.PENDING, title="Realization Control"),
        )
        current = orchestrator.current_step()
        assert current is not None and current.step_no == 10

    def test_the_next_step_never_jumps_the_queue(
        self, storage, worker, orchestrator
    ) -> None:
        seed(
            storage,
            make_step(9),
            make_step(10, state=StepState.PENDING, title="Realization Control"),
        )
        assert orchestrator.current_step().step_no == 9
        assert orchestrator.next_step_no() == 10

        orchestrator.run_once()

        assert step_row(storage, 9).state is StepState.DISPATCHED
        assert step_row(storage, 10).state is StepState.PENDING
        assert [request.task.step_no for request in worker.dispatched] == [9]

    def test_a_blocked_step_keeps_the_next_step_inactive(
        self, storage, worker, orchestrator
    ) -> None:
        seed(
            storage,
            make_step(9, state=StepState.BLOCKED),
            make_step(10, state=StepState.PENDING, title="Realization Control"),
        )
        tick = orchestrator.run_once()
        assert tick.status is LoopStatus.WAITING
        assert tick.step_no == 9
        assert tick.reason == "human-unblock-required"
        assert orchestrator.current_step().step_no == 9
        assert orchestrator.next_step_no() == 10
        assert step_row(storage, 10).state is StepState.PENDING
        assert worker.dispatched == []

    def test_an_aborted_step_stops_the_project(
        self, storage, worker, orchestrator
    ) -> None:
        seed(
            storage,
            make_step(9, state=StepState.ABORTED),
            make_step(10, state=StepState.PENDING, title="Realization Control"),
        )
        tick = orchestrator.run_once()
        assert tick.status is LoopStatus.ABORTED
        assert tick.reason == "step-aborted"
        assert orchestrator.is_complete() is False
        assert step_row(storage, 10).state is StepState.PENDING
        assert worker.dispatched == []

    def test_a_conflict_step_halts_the_loop(
        self, storage, worker, orchestrator
    ) -> None:
        seed(storage, make_step(9, state=StepState.CONFLICT))
        tick = orchestrator.run_once()
        assert tick.status is LoopStatus.WAITING
        assert tick.reason == "human-conflict-resolution-required"
        assert worker.dispatched == []

    def test_waiting_for_approval_halts_the_loop(
        self, storage, worker, orchestrator
    ) -> None:
        seed(storage, make_step(9, state=StepState.WAITING_APPROVAL))
        tick = orchestrator.run_once()
        assert tick.status is LoopStatus.WAITING
        assert tick.reason == "human-approval-required"
        assert worker.dispatched == []

    def test_complete_only_when_every_step_is_verified(
        self, storage, orchestrator
    ) -> None:
        seed(
            storage,
            make_step(9, state=StepState.VERIFIED),
            make_step(10, state=StepState.VERIFIED, title="Realization Control"),
        )
        assert orchestrator.is_complete() is True
        tick = orchestrator.run_once()
        assert tick.status is LoopStatus.COMPLETE
        assert tick.reason == "all-steps-verified"

    def test_aborted_is_never_equivalent_to_verified(
        self, storage, orchestrator
    ) -> None:
        seed(storage, make_step(9, state=StepState.ABORTED))
        assert orchestrator.is_complete() is False


class ExplodingAudit:
    """An audit port whose ``append`` always fails (to prove atomicity)."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def append(self, entry: AuditEntry) -> None:
        raise self.error

    def list(self) -> tuple[AuditEntry, ...]:
        return ()

    def list_for_entity(self, *args, **kwargs) -> tuple[AuditEntry, ...]:
        return ()


class TestTransactionAtomicity:
    def test_a_retry_rolls_back_both_the_state_and_the_attempt(
        self, storage, worker, orchestrator, monkeypatch
    ) -> None:
        reach_reviewing(
            storage,
            worker,
            orchestrator,
            result(ReportStatus.REVISE),
            step=make_step(attempt=1),
        )
        orchestrator.run_once()  # -> REVISE

        monkeypatch.setattr(
            storage, "_audit", ExplodingAudit(RuntimeError("audit down"))
        )
        with pytest.raises(RuntimeError, match="audit down"):
            orchestrator.run_once()  # RETRY

        step = step_row(storage)
        assert step.state is StepState.REVISE
        assert step.attempt == 1

    def test_a_dispatch_rolls_back_the_step_and_the_task(
        self, storage, worker, orchestrator, monkeypatch
    ) -> None:
        seed(storage)
        monkeypatch.setattr(
            storage, "_audit", ExplodingAudit(RuntimeError("audit down"))
        )
        with pytest.raises(RuntimeError, match="audit down"):
            orchestrator.run_once()

        assert step_row(storage).state is StepState.READY
        assert storage.tasks.list() == ()

    def test_a_review_outcome_rolls_back_and_keeps_the_report(
        self, storage, worker, orchestrator, monkeypatch
    ) -> None:
        reach_reviewing(
            storage, worker, orchestrator, result(), step=make_step()
        )
        monkeypatch.setattr(
            storage, "_audit", ExplodingAudit(RuntimeError("audit down"))
        )
        with pytest.raises(RuntimeError, match="audit down"):
            orchestrator.run_once()

        assert step_row(storage).state is StepState.REVIEWING
        # Nothing was persisted, so the report must not have been consumed.
        assert worker.acknowledged == []
        assert worker.available





