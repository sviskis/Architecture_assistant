"""Tests for the scheduler (the time dimension of the loop).

The scheduler may chain immediate transitions, but it must stop the moment the
loop needs an external event - no polling, no sleep, no spinning. Each test
therefore asserts both the final reason and the exact number of ticks.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from architecture_assistant.application import (
    DEFAULT_MAX_ITERATIONS,
    ContextBuilder,
    LoopHealth,
    LoopStatus,
    Orchestrator,
    Scheduler,
)
from architecture_assistant.domain.enums import (
    Mode,
    Phase,
    ReportStatus,
    RiskLevel,
    StepState,
)
from architecture_assistant.domain.models import Project, Step
from architecture_assistant.infrastructure import (
    SqliteStorage,
    close_database,
    open_database,
)
from architecture_assistant.ports.capabilities import (
    RealizationGate,
    WorkerRequest,
    WorkerResult,
)

NOW = datetime(2026, 9, 20, 20, 0, tzinfo=timezone.utc)


class FakeRealizationControl:
    """A passing ``RealizationControlPort`` for scheduler tests."""

    def gate(self, step_no: int, attempt: int) -> RealizationGate:
        return RealizationGate(
            compliant=True,
            step_no=step_no,
            attempt=attempt,
            baseline_version="1.1",
            violation_count=0,
            finding_ids=(),
            decision_id=f"realization-step-{step_no:03d}-attempt-{attempt:03d}",
        )


class FakeClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeWorker:
    def __init__(self, *, auto_publish: bool = False) -> None:
        self.auto_publish = auto_publish
        self.available: dict[tuple[int, int], WorkerResult] = {}
        self.dispatched: list[WorkerRequest] = []
        self.acknowledged: list[tuple[int, int]] = []

    def dispatch(self, request: WorkerRequest):
        self.dispatched.append(request)
        if self.auto_publish:
            self.available[
                (request.task.step_no, request.task.attempt)
            ] = WorkerResult(status=ReportStatus.DONE, summary="done")
        return "dispatched"

    def read_report(self, step_no: int, attempt: int):
        return self.available.get((step_no, attempt))

    def acknowledge_report(self, step_no: int, attempt: int):
        self.acknowledged.append((step_no, attempt))
        self.available.pop((step_no, attempt), None)
        return "archived"


def make_step(
    step_no: int = 9, state: StepState = StepState.READY
) -> Step:
    return Step(
        step_no=step_no,
        phase=Phase.LOOP,
        title="Orchestrator + Scheduler",
        state=state,
        attempt=1,
        risk=RiskLevel.LOW,
        requires_human=False,
        created_at=NOW,
    )


@pytest.fixture
def storage():
    conn = open_database(":memory:")
    storage = SqliteStorage(conn)
    yield storage
    close_database(conn)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def worker() -> FakeWorker:
    return FakeWorker()


def build(
    storage: SqliteStorage,
    worker: FakeWorker,
    clock: FakeClock,
    *,
    steps: tuple[Step, ...] = (),
    mode: Mode = Mode.AUTO,
    paused: bool = False,
    timeout: timedelta | None = timedelta(hours=1),
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
) -> Scheduler:
    storage.projects.upsert(
        Project(
            name="Architecture Lifecycle Assistant",
            plan_version="0.3",
            mode=mode,
            paused=paused,
            created_at=NOW,
        )
    )
    for step in steps:
        storage.steps.upsert(step)
    builder = ContextBuilder(
        storage.projects,
        storage.steps,
        storage.adrs,
        storage.risks,
        storage.architecture_versions,
        clock=clock,
    )
    orchestrator = Orchestrator(
        storage,
        worker,
        builder,
        realization_control=FakeRealizationControl(),
        clock=clock,
        timeout=timeout,
    )
    return Scheduler(orchestrator, max_iterations=max_iterations)


class TestConstruction:
    def test_orchestrator_is_required(self) -> None:
        with pytest.raises(ValueError, match="orchestrator must be"):
            Scheduler(object())  # type: ignore[arg-type]

    def test_max_iterations_must_be_positive(self, storage, worker, clock) -> None:
        scheduler = build(storage, worker, clock)
        with pytest.raises(ValueError, match="max_iterations must be an int"):
            Scheduler(scheduler.orchestrator, max_iterations=0)

    def test_exposes_its_configuration(self, storage, worker, clock) -> None:
        scheduler = build(storage, worker, clock, max_iterations=7)
        assert scheduler.max_iterations == 7
        assert isinstance(scheduler.orchestrator, Orchestrator)


class TestStopConditions:
    def test_stops_when_the_worker_report_is_due(
        self, storage, worker, clock
    ) -> None:
        scheduler = build(storage, worker, clock, steps=(make_step(),))
        run = scheduler.run_until_idle()

        assert run.final_tick.status is LoopStatus.WAITING
        assert run.stopped_because == "worker-report-pending"
        assert run.iterations == 3  # dispatch, start, wait - then stop
        assert run.transition_count == 2
        assert run.limit_reached is False

    def test_stops_at_human_approval(self, storage, worker, clock) -> None:
        scheduler = build(
            storage, worker, clock, steps=(make_step(),), mode=Mode.MANUAL
        )
        run = scheduler.run_until_idle()

        assert run.stopped_because == "human-approval-required"
        assert run.iterations == 2
        assert worker.dispatched == []

    def test_stops_on_unblock(self, storage, worker, clock) -> None:
        scheduler = build(
            storage,
            worker,
            clock,
            steps=(make_step(state=StepState.BLOCKED),),
        )
        run = scheduler.run_until_idle()
        assert run.iterations == 1
        assert run.stopped_because == "human-unblock-required"

    def test_stops_on_conflict_resolution(
        self, storage, worker, clock
    ) -> None:
        scheduler = build(
            storage,
            worker,
            clock,
            steps=(make_step(state=StepState.CONFLICT),),
        )
        run = scheduler.run_until_idle()
        assert run.iterations == 1
        assert run.stopped_because == "human-conflict-resolution-required"

    def test_stops_when_the_project_is_complete(
        self, storage, worker, clock
    ) -> None:
        scheduler = build(
            storage,
            worker,
            clock,
            steps=(make_step(state=StepState.VERIFIED),),
        )
        run = scheduler.run_until_idle()
        assert run.final_tick.status is LoopStatus.COMPLETE
        assert run.iterations == 1

    def test_stops_when_the_step_was_aborted(
        self, storage, worker, clock
    ) -> None:
        scheduler = build(
            storage,
            worker,
            clock,
            steps=(make_step(state=StepState.ABORTED),),
        )
        run = scheduler.run_until_idle()
        assert run.final_tick.status is LoopStatus.ABORTED
        assert run.stopped_because == "step-aborted"

    def test_stops_when_the_project_is_paused(
        self, storage, worker, clock
    ) -> None:
        scheduler = build(
            storage, worker, clock, steps=(make_step(),), paused=True
        )
        run = scheduler.run_until_idle()
        assert run.final_tick.status is LoopStatus.PAUSED
        assert run.stopped_because == "project-paused"

    def test_stops_when_there_are_no_steps(
        self, storage, worker, clock
    ) -> None:
        scheduler = build(storage, worker, clock)
        run = scheduler.run_until_idle()
        assert run.final_tick.status is LoopStatus.NO_STEPS
        assert run.stopped_because == "no-steps"

    def test_runs_a_complete_loop_when_everything_is_immediate(
        self, storage, clock
    ) -> None:
        worker = FakeWorker(auto_publish=True)
        scheduler = build(storage, worker, clock, steps=(make_step(),))
        run = scheduler.run_until_idle()

        assert run.final_tick.status is LoopStatus.COMPLETE
        assert [tick.to_state for tick in run.transitions] == [
            StepState.DISPATCHED,
            StepState.REPORT_RECEIVED,
            StepState.REVIEWING,
            StepState.VERIFIED,
        ]

    def test_stops_at_the_iteration_limit(self, storage, clock) -> None:
        worker = FakeWorker(auto_publish=True)
        scheduler = build(
            storage, worker, clock, steps=(make_step(),), max_iterations=2
        )
        run = scheduler.run_until_idle()

        assert run.limit_reached is True
        assert run.iterations == 2
        assert run.stopped_because == "iteration-limit"

        # The bound is per call, so the operator simply calls again.
        scheduler.run_until_idle()
        assert (
            scheduler.run_until_idle().final_tick.status
            is LoopStatus.COMPLETE
        )


class TestIdleBehaviour:
    def test_waiting_is_returned_not_polled(self, storage, worker, clock) -> None:
        scheduler = build(storage, worker, clock, steps=(make_step(),))
        first = scheduler.run_until_idle()
        assert first.stopped_because == "worker-report-pending"

        second = scheduler.run_until_idle()
        assert second.iterations == 1
        assert second.transition_count == 0
        assert second.stopped_because == "worker-report-pending"

    def test_an_idle_round_changes_nothing(self, storage, worker, clock) -> None:
        scheduler = build(storage, worker, clock, steps=(make_step(),))
        scheduler.run_until_idle()
        before = storage.steps.list()
        audit_before = storage.audit.list()

        scheduler.run_until_idle()

        assert storage.steps.list() == before
        assert storage.audit.list() == audit_before

    def test_run_summary_is_json_safe(self, storage, worker, clock) -> None:
        scheduler = build(storage, worker, clock, steps=(make_step(),))
        run = scheduler.run_until_idle()
        assert json.loads(json.dumps(run.to_dict())) == run.to_dict()
        assert run.to_dict()["iterations"] == run.iterations

    def test_stopped_because_without_ticks(self) -> None:
        from architecture_assistant.application import SchedulerRun

        assert SchedulerRun(ticks=()).stopped_because == "never-started"
        assert SchedulerRun(ticks=()).final_tick is None


class TestHealth:
    def test_health_is_read_only(self, storage, worker, clock) -> None:
        scheduler = build(storage, worker, clock, steps=(make_step(),))
        before = storage.steps.list()

        health = scheduler.health()

        assert isinstance(health, LoopHealth)
        assert health.current_step_no == 9
        assert health.current_state is StepState.READY
        assert health.next_step_no is None
        assert health.complete is False
        assert health.project_paused is False
        assert health.step_count == 1
        assert storage.steps.list() == before

    def test_health_after_a_complete_run(self, storage, clock) -> None:
        worker = FakeWorker(auto_publish=True)
        scheduler = build(storage, worker, clock, steps=(make_step(),))
        scheduler.run_until_idle()

        health = scheduler.health()

        assert health.complete is True
        assert health.current_step_no is None
        assert health.current_state is None

    def test_health_is_json_safe(self, storage, worker, clock) -> None:
        scheduler = build(storage, worker, clock, steps=(make_step(),))
        payload = scheduler.health().to_dict()
        assert json.loads(json.dumps(payload)) == payload

    def test_health_reports_a_paused_project(
        self, storage, worker, clock
    ) -> None:
        scheduler = build(
            storage, worker, clock, steps=(make_step(),), paused=True
        )
        assert scheduler.health().project_paused is True
