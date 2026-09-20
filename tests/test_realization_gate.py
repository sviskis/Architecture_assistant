"""Tests for the mandatory realization gate inside the orchestrator loop.

Covered: ``VERIFY`` is gated by the deterministic architecture check, a violation
downgrades the outcome to ``BLOCKED`` (and can never be out-voted by a ``DONE``
report), evidence is persisted *before* the report is acknowledged, a failing
gate or a failing persistence leaves the report un-acknowledged, and an earlier
attempt's verdict survives a later compliant attempt.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Sequence

import pytest

from architecture_assistant.application import (
    ContextBuilder,
    Orchestrator,
    RealizationControlError,
    RealizationControlUseCase,
)
from architecture_assistant.architecture import ARCHITECTURE_CURRENT
from architecture_assistant.composition import (
    ArchitectureRealizationAdapter,
    canonical_baseline,
)
from architecture_assistant.domain.audit import AuditEntityType, AuditEntry
from architecture_assistant.domain.enums import (
    DecisionStatus,
    Mode,
    Phase,
    ReportStatus,
    RiskLevel,
    StepState,
)
from architecture_assistant.domain.models import (
    ArchitectureVersion,
    Project,
    Step,
)
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
PROJECT = "Architecture Lifecycle Assistant"
STEP_NO = 10


#: A minimal tree that satisfies the current baseline completely.
COMPLIANT_TREE: Mapping[str, Sequence[str]] = {
    "domain/models.py": (),
    "ports/repositories.py": ("architecture_assistant.domain.models",),
    "application/context.py": (
        "architecture_assistant.domain.models",
        "architecture_assistant.ports.repositories",
    ),
    "architecture/rules.py": ("architecture_assistant.domain.enums",),
    "infrastructure/storage.py": (
        "architecture_assistant.domain.models",
        "architecture_assistant.ports.repositories",
    ),
    "composition/root.py": (
        "architecture_assistant.domain.models",
        "architecture_assistant.ports.repositories",
        "architecture_assistant.application.context",
        "architecture_assistant.architecture.rules",
        "architecture_assistant.infrastructure.storage",
    ),
}


def build_tree(
    root: Path, files: Mapping[str, Sequence[str]] | None = None
) -> Path:
    for relative, targets in (files or COMPLIANT_TREE).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        (path.parent / "__init__.py").write_text("", encoding="utf-8")
        path.write_text(
            "".join(f"import {target}\n" for target in targets),
            encoding="utf-8",
        )
    return root


def violating_tree(root: Path) -> Path:
    return build_tree(
        root,
        {
            **COMPLIANT_TREE,
            "application/rogue.py": (
                "architecture_assistant.architecture.rules",
            ),
        },
    )


class FakeClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeWorker:
    """In-memory WorkerChannelPort."""

    def __init__(self) -> None:
        self.available: dict[tuple[int, int], WorkerResult] = {}
        self.dispatched: list[WorkerRequest] = []
        self.acknowledged: list[tuple[int, int]] = []

    def publish(
        self, step_no: int, attempt: int, report: WorkerResult | None = None
    ) -> None:
        self.available[(step_no, attempt)] = report or done_report()

    def dispatch(self, request: WorkerRequest):
        self.dispatched.append(request)
        return "dispatched"

    def read_report(self, step_no: int, attempt: int):
        return self.available.get((step_no, attempt))

    def acknowledge_report(self, step_no: int, attempt: int):
        if (step_no, attempt) not in self.available:
            raise RuntimeError(f"no report for {step_no}/{attempt}")
        self.acknowledged.append((step_no, attempt))
        del self.available[(step_no, attempt)]
        return "archived"


def done_report(**overrides) -> WorkerResult:
    payload = {
        "status": ReportStatus.DONE,
        "summary": "realization control implemented",
        "artifacts": (),
        "issues": (),
        "architecture_questions": (),
    }
    payload.update(overrides)
    return WorkerResult(**payload)


class CountingGate:
    """A gate spy: records calls and returns a configurable verdict."""

    def __init__(
        self,
        *,
        compliant: bool = True,
        error: Exception | None = None,
    ) -> None:
        self.compliant = compliant
        self.error = error
        self.calls: list[tuple[int, int]] = []

    def gate(self, step_no: int, attempt: int) -> RealizationGate:
        self.calls.append((step_no, attempt))
        if self.error is not None:
            raise self.error
        finding_ids = () if self.compliant else ("finding-010-001-deadbeef",)
        return RealizationGate(
            compliant=self.compliant,
            step_no=step_no,
            attempt=attempt,
            baseline_version=ARCHITECTURE_CURRENT.version,
            violation_count=len(finding_ids),
            finding_ids=finding_ids,
            decision_id=f"realization-step-{step_no:03d}-attempt-{attempt:03d}",
        )


class ExplodingAudit:
    """An audit repository whose writes always fail."""

    def append(self, entry: AuditEntry) -> None:
        raise RuntimeError("audit storage is down")

    def list(self) -> tuple[AuditEntry, ...]:
        return ()

    def list_for_entity(self, *args, **kwargs) -> tuple[AuditEntry, ...]:
        return ()


def make_step(
    step_no: int = STEP_NO,
    *,
    state: StepState = StepState.READY,
    attempt: int = 1,
) -> Step:
    return Step(
        step_no=step_no,
        phase=Phase.CONTROL,
        title="Architecture Realization Control",
        state=state,
        attempt=attempt,
        max_attempts=3,
        risk=RiskLevel.LOW,
        requires_human=False,
        created_at=NOW,
    )


def seed_project(
    storage: SqliteStorage, *steps: Step, mode: Mode = Mode.AUTO
) -> None:
    storage.projects.upsert(
        Project(
            name=PROJECT,
            plan_version="0.3",
            mode=mode,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    for step in steps:
        storage.steps.upsert(step)


def seed_baseline(
    storage: SqliteStorage,
    *,
    version: str = ARCHITECTURE_CURRENT.version,
    rules: tuple[str, ...] | None = None,
) -> None:
    storage.architecture_versions.upsert(
        ArchitectureVersion(
            version=version,
            baseline="persisted baseline",
            rules=ARCHITECTURE_CURRENT.rule_ids() if rules is None else rules,
            is_current=True,
            created_at=NOW,
        )
    )


def real_use_case(
    storage: SqliteStorage, root: Path
) -> RealizationControlUseCase:
    return RealizationControlUseCase(
        storage,
        ArchitectureRealizationAdapter(root, clock=lambda: NOW),
        canonical=canonical_baseline(),
    )


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
def builder(storage, clock) -> ContextBuilder:
    return ContextBuilder(
        storage.projects,
        storage.steps,
        storage.adrs,
        storage.risks,
        storage.architecture_versions,
        clock=clock,
    )


def build_orchestrator(
    storage, worker, builder, realization, clock
) -> Orchestrator:
    return Orchestrator(
        storage,
        worker,
        builder,
        realization_control=realization,
        clock=clock,
        timeout=timedelta(hours=1),
    )


def step_row(storage: SqliteStorage, step_no: int = STEP_NO) -> Step:
    step = storage.steps.get(step_no)
    assert step is not None
    return step


def audit_reasons(storage: SqliteStorage) -> list[str]:
    return [
        entry.detail["reason"]
        for entry in storage.audit.list()
        if entry.entity_type is AuditEntityType.STEP
    ]


def reach_reviewing(storage, worker, orchestrator, *, report=None) -> None:
    """Walk the loop to ``REVIEWING`` with a report published."""
    seed_project(storage, make_step())
    orchestrator.run_once()  # READY -> DISPATCHED
    worker.publish(STEP_NO, 1, report or done_report())
    orchestrator.run_once()  # DISPATCHED -> REPORT_RECEIVED
    orchestrator.run_once()  # REPORT_RECEIVED -> REVIEWING
    assert step_row(storage).state is StepState.REVIEWING


class TestGateBlocksVerify:
    def test_a_compliant_run_still_verifies(
        self, storage, worker, builder, clock, tmp_path
    ) -> None:
        seed_baseline(storage)
        orchestrator = build_orchestrator(
            storage, worker, builder, real_use_case(storage, build_tree(tmp_path)), clock
        )
        reach_reviewing(storage, worker, orchestrator)

        tick = orchestrator.run_once()

        assert tick.to_state is StepState.VERIFIED
        assert tick.acknowledged is True
        assert [d.status for d in storage.decisions.list()] == [
            DecisionStatus.ACCEPTED
        ]
        assert storage.findings.list() == ()

    def test_a_violation_blocks_verify_with_evidence(
        self, storage, worker, builder, clock, tmp_path
    ) -> None:
        seed_baseline(storage)
        orchestrator = build_orchestrator(
            storage,
            worker,
            builder,
            real_use_case(storage, violating_tree(tmp_path)),
            clock,
        )
        reach_reviewing(storage, worker, orchestrator)

        tick = orchestrator.run_once()

        assert tick.to_state is StepState.BLOCKED
        assert tick.reason == "architecture-violation"
        assert tick.acknowledged is True
        assert step_row(storage).state is StepState.BLOCKED
        assert audit_reasons(storage)[-1] == "architecture-violation"
        decisions = storage.decisions.list()
        assert [decision.status for decision in decisions] == [
            DecisionStatus.REJECTED
        ]
        assert [finding.claim for finding in storage.findings.list()] == [
            "forbidden-layer-import"
        ]
        # and the loop now waits for a human instead of moving on
        assert orchestrator.run_once().reason == "human-unblock-required"

    def test_a_done_report_cannot_out_vote_a_violation(
        self, storage, worker, builder, clock
    ) -> None:
        """The deterministic gate wins over any worker claim."""
        seed_baseline(storage)
        gate = CountingGate(compliant=False)
        orchestrator = build_orchestrator(storage, worker, builder, gate, clock)
        reach_reviewing(storage, worker, orchestrator, report=done_report())

        tick = orchestrator.run_once()

        assert gate.calls == [(STEP_NO, 1)]
        assert tick.to_state is StepState.BLOCKED
        assert step_row(storage).state is not StepState.VERIFIED

    def test_the_gate_is_asked_once_per_attempt(
        self, storage, worker, builder, clock
    ) -> None:
        seed_baseline(storage)
        gate = CountingGate(compliant=True)
        orchestrator = build_orchestrator(storage, worker, builder, gate, clock)
        reach_reviewing(storage, worker, orchestrator)

        orchestrator.run_once()

        assert gate.calls == [(STEP_NO, 1)]

    def test_the_gate_is_not_consulted_for_a_revise_report(
        self, storage, worker, builder, clock
    ) -> None:
        """The check is scoped to the VERIFY path, nothing else."""
        seed_baseline(storage)
        gate = CountingGate(compliant=True)
        orchestrator = build_orchestrator(storage, worker, builder, gate, clock)
        reach_reviewing(
            storage,
            worker,
            orchestrator,
            report=done_report(status=ReportStatus.REVISE),
        )

        tick = orchestrator.run_once()

        assert tick.to_state is StepState.REVISE
        assert gate.calls == []

    def test_a_blocked_report_does_not_consult_the_gate(
        self, storage, worker, builder, clock
    ) -> None:
        seed_baseline(storage)
        gate = CountingGate(compliant=True)
        orchestrator = build_orchestrator(storage, worker, builder, gate, clock)
        reach_reviewing(
            storage,
            worker,
            orchestrator,
            report=done_report(status=ReportStatus.BLOCKED),
        )

        assert orchestrator.run_once().to_state is StepState.BLOCKED
        assert gate.calls == []


class TestAcknowledgementOrdering:
    def test_a_failing_gate_leaves_the_report_unacknowledged(
        self, storage, worker, builder, clock
    ) -> None:
        seed_baseline(storage)
        gate = CountingGate(error=RuntimeError("control plane is down"))
        orchestrator = build_orchestrator(storage, worker, builder, gate, clock)
        reach_reviewing(storage, worker, orchestrator)

        with pytest.raises(RuntimeError, match="control plane is down"):
            orchestrator.run_once()

        assert step_row(storage).state is StepState.REVIEWING
        assert worker.acknowledged == []
        assert worker.available  # the report is still there to be re-read

    def test_a_failing_persistence_leaves_the_report_unacknowledged(
        self, storage, worker, builder, clock, tmp_path, monkeypatch
    ) -> None:
        seed_baseline(storage)
        orchestrator = build_orchestrator(
            storage,
            worker,
            builder,
            real_use_case(storage, build_tree(tmp_path)),
            clock,
        )
        reach_reviewing(storage, worker, orchestrator)
        monkeypatch.setattr(storage, "_audit", ExplodingAudit())

        with pytest.raises(RuntimeError, match="audit storage is down"):
            orchestrator.run_once()

        assert step_row(storage).state is StepState.REVIEWING
        assert worker.acknowledged == []
        # the evidence of the rolled-back transaction is gone too
        assert storage.decisions.list() == ()

    def test_a_realization_error_leaves_the_report_unacknowledged(
        self, storage, worker, builder, clock, tmp_path
    ) -> None:
        seed_baseline(storage, version="9.9")
        orchestrator = build_orchestrator(
            storage,
            worker,
            builder,
            real_use_case(storage, build_tree(tmp_path)),
            clock,
        )
        reach_reviewing(storage, worker, orchestrator)

        with pytest.raises(RealizationControlError):
            orchestrator.run_once()

        assert step_row(storage).state is StepState.REVIEWING
        assert worker.acknowledged == []
        assert orchestrator.current_step().state is StepState.REVIEWING

    def test_two_current_baselines_fail_closed(
        self, storage, worker, builder, clock, tmp_path
    ) -> None:
        seed_baseline(storage)
        orchestrator = build_orchestrator(
            storage,
            worker,
            builder,
            real_use_case(storage, build_tree(tmp_path)),
            clock,
        )
        reach_reviewing(storage, worker, orchestrator)
        # the control plane is corrupted *after* the step was dispatched
        seed_baseline(storage, version="1.2")

        with pytest.raises(RealizationControlError, match="2 current"):
            orchestrator.run_once()

        assert step_row(storage).state is StepState.REVIEWING
        assert worker.acknowledged == []

    def test_the_evidence_is_durable_before_the_acknowledgement(
        self, storage, worker, builder, clock, tmp_path
    ) -> None:
        """At the moment of the ack, state + audit + evidence all exist."""
        seed_baseline(storage)
        orchestrator = build_orchestrator(
            storage,
            worker,
            builder,
            real_use_case(storage, violating_tree(tmp_path)),
            clock,
        )
        reach_reviewing(storage, worker, orchestrator)

        tick = orchestrator.run_once()

        assert tick.acknowledged is True
        assert storage.findings.list()
        assert storage.decisions.list()
        assert audit_reasons(storage)[-1] == "architecture-violation"
        assert step_row(storage).state is StepState.BLOCKED


class TestAttemptHistory:
    def test_a_fixed_second_attempt_verifies_and_keeps_both_verdicts(
        self, storage, worker, builder, clock, tmp_path
    ) -> None:
        seed_baseline(storage)
        first = build_orchestrator(
            storage,
            worker,
            builder,
            real_use_case(storage, violating_tree(tmp_path / "a1")),
            clock,
        )
        reach_reviewing(storage, worker, first)
        assert first.run_once().to_state is StepState.BLOCKED

        # a human unblocks the step and a new attempt begins
        blocked = step_row(storage)
        storage.steps.upsert(
            blocked.__class__(
                step_no=blocked.step_no,
                phase=blocked.phase,
                title=blocked.title,
                state=StepState.READY,
                attempt=2,
                max_attempts=blocked.max_attempts,
                risk=blocked.risk,
                requires_human=blocked.requires_human,
                created_at=blocked.created_at,
            )
        )
        second = build_orchestrator(
            storage,
            worker,
            builder,
            real_use_case(storage, build_tree(tmp_path / "a2")),
            clock,
        )
        worker.publish(STEP_NO, 2, done_report())

        ticks = [second.run_once() for _ in range(4)]

        assert ticks[-1].to_state is StepState.VERIFIED
        assert ticks[-1].acknowledged is True
        decisions = {d.id: d for d in storage.decisions.list()}
        assert decisions["realization-step-010-attempt-001"].status is (
            DecisionStatus.REJECTED
        )
        assert decisions["realization-step-010-attempt-002"].status is (
            DecisionStatus.ACCEPTED
        )
        # the earlier attempt's finding was preserved, not rewritten
        assert [
            finding.id
            for finding in storage.findings.list()
            if finding.id.startswith("finding-010-001-")
        ]
