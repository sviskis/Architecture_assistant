"""Tests for the read-only project monitor (Step 19).

The monitor adds no state, no algorithm and no write path: it is a pull-based
selector layer over the canonical projection. These tests pin exactly that:

* construction takes **nothing but the projection** - no storage, no
  transaction, no cost sink, no repository;
* the public surface is the documented read API and nothing else;
* the module imports no ``infrastructure`` and no write port;
* every read still works while every fake write method raises;
* the loop semantics it reports (current step, next step, blocking) are the
  loop's own numbers and the loop's own halt set, never a second opinion.
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

from architecture_assistant.application import (
    HALTING_REASONS,
    REPORT_SCHEMA_VERSION,
    LoopHealth,
    Monitor,
    ReportBuilder,
    ReportSnapshot,
)
from architecture_assistant.architecture import (
    ARCHITECTURE_CURRENT,
    ROOT_PACKAGE,
    ArchitectureValidator,
    layer_of,
    scan_directory,
)
from architecture_assistant.domain.enums import (
    ADRStatus,
    Mode,
    Phase,
    RiskLevel,
    RiskStatus,
    Severity,
    StepState,
)
from architecture_assistant.domain.models import (
    ADR,
    ArchitectureChangeRequest,
    ArchitectureVersion,
    Decision,
    Finding,
    Project,
    Risk,
    Step,
    Task,
)
from architecture_assistant.ports.capabilities import CostQuery, CostSummary

NOW = datetime(2026, 9, 21, 18, 0, tzinfo=timezone.utc)

_WRITE_ERROR = "the read-only monitor must never write to the source of truth"

#: The approved public surface of the monitor - read accessors only.
PUBLIC_API = {
    "snapshot",
    "to_dict",
    "health",
    "current_step",
    "next_step",
    "step_states",
    "blocking_steps",
    "open_risks",
    "change_requests",
    "cost_summary",
    "architecture_version",
}


def fixed_clock() -> datetime:
    return NOW


def _src_root() -> Path:
    return Path(__file__).resolve().parents[1] / "src" / "architecture_assistant"


# ---------------------------------------------------------------------------
# domain fixtures
# ---------------------------------------------------------------------------

def make_project(**overrides: Any) -> Project:
    data: dict[str, Any] = dict(
        name="Architecture Lifecycle Assistant",
        plan_version="0.3",
        mode=Mode.MANUAL,
        paused=False,
        created_at=NOW,
        updated_at=NOW,
    )
    data.update(overrides)
    return Project(**data)


def make_step(step_no: int = 1, state: StepState = StepState.VERIFIED) -> Step:
    return Step(
        step_no=step_no,
        phase=Phase.LOOP,
        title=f"Step {step_no}",
        state=state,
        attempt=1,
        max_attempts=3,
        risk=RiskLevel.LOW,
        requires_human=False,
        created_at=NOW,
        last_update_at=NOW,
    )


def make_task(step_no: int = 1, attempt: int = 1) -> Task:
    return Task(
        step_no=step_no,
        phase=Phase.LOOP,
        title=f"Task {step_no}.{attempt}",
        attempt=attempt,
        created_at=NOW,
    )


def make_architecture(
    version: str = "1.0", is_current: bool = True
) -> ArchitectureVersion:
    return ArchitectureVersion(
        version=version,
        baseline="Layered assistant core",
        rules=("RULE-1",),
        superseded_by=None if is_current else "9.9",
        is_current=is_current,
        created_at=NOW,
    )


def make_adr(adr_id: str = "ADR-001") -> ADR:
    return ADR(
        id=adr_id,
        title=f"ADR {adr_id}",
        status=ADRStatus.ACCEPTED,
        context="context",
        decision="decision",
        created_at=NOW,
    )


def make_risk(risk_id: str = "RISK-001", status: RiskStatus = RiskStatus.OPEN) -> Risk:
    return Risk(
        id=risk_id,
        description=f"Risk {risk_id}",
        severity=Severity.HIGH,
        probability=0.3,
        impact=Severity.HIGH,
        owner="architect",
        status=status,
        created_at=NOW,
    )


def make_finding(finding_id: str = "F-1") -> Finding:
    return Finding(
        id=finding_id,
        source="openai",
        claim="a claim",
        evidence=("application/monitor.py:1",),
        step_no=1,
        created_at=NOW,
    )


def make_decision(decision_id: str = "D-1") -> Decision:
    return Decision(id=decision_id, step_no=1, created_at=NOW)


def make_change_request(request_id: str = "ACR-001") -> ArchitectureChangeRequest:
    return ArchitectureChangeRequest(
        request_id=request_id,
        title=f"Change {request_id}",
        rationale="the baseline must move",
        source_version="1.0",
        target_version="1.1",
        rule_ids=("RULE-1",),
        adr_id="ADR-001",
        created_at=NOW,
    )


def make_health(**overrides: Any) -> LoopHealth:
    data: dict[str, Any] = dict(
        project_paused=False,
        step_count=2,
        current_step_no=2,
        current_state=StepState.READY,
        next_step_no=None,
        complete=False,
    )
    data.update(overrides)
    return LoopHealth(**data)


# ---------------------------------------------------------------------------
# read-only fake ports
# ---------------------------------------------------------------------------

class ReadOnlyRepository:
    """A repository port that only ever answers ``list()``.

    Every write method fails loudly, so any accidental write anywhere on the
    read path becomes a test failure instead of a silent mutation. ``items``
    stays assignable so a test can prove the monitor re-reads (no cache).
    """

    def __init__(self, items: tuple = ()) -> None:
        self.items = tuple(items)
        self.list_calls = 0

    def list(self) -> tuple:
        self.list_calls += 1
        return self.items

    def upsert(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError(_WRITE_ERROR)

    def append(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError(_WRITE_ERROR)

    def delete(self, *args: Any, **kwargs: Any) -> bool:
        raise AssertionError(_WRITE_ERROR)


class CostReader:
    """The read-only cost seam: ``query()`` only, never ``record()``."""

    def __init__(
        self,
        total: Optional[CostSummary] = None,
        by_step: Optional[dict[int, CostSummary]] = None,
    ) -> None:
        self.total = total or CostSummary()
        self.by_step = dict(by_step or {})

    def __call__(self, cost_filter: CostQuery) -> CostSummary:
        if cost_filter.step_no is None:
            return self.total
        return self.by_step.get(cost_filter.step_no, CostSummary())

    def record(self, cost: Any) -> None:
        raise AssertionError(_WRITE_ERROR)


class HealthReader:
    """The read-only loop-health seam."""

    def __init__(self, health: Optional[LoopHealth] = None) -> None:
        self.health = health or make_health()
        self.calls = 0

    def __call__(self) -> LoopHealth:
        self.calls += 1
        return self.health


def build_repositories(**overrides: Any) -> dict[str, ReadOnlyRepository]:
    repositories = {
        "projects": ReadOnlyRepository((make_project(),)),
        "steps": ReadOnlyRepository(
            (make_step(2, StepState.READY), make_step(1))
        ),
        "tasks": ReadOnlyRepository((make_task(2, 1), make_task(1, 2))),
        "architecture_versions": ReadOnlyRepository(
            (make_architecture("1.1"), make_architecture("1.0", False))
        ),
        "adrs": ReadOnlyRepository((make_adr("ADR-002"), make_adr("ADR-001"))),
        "risks": ReadOnlyRepository((make_risk("RISK-002"), make_risk("RISK-001"))),
        "findings": ReadOnlyRepository((make_finding("F-2"), make_finding("F-1"))),
        "decisions": ReadOnlyRepository(
            (make_decision("D-2"), make_decision("D-1"))
        ),
        "change_requests": ReadOnlyRepository(
            (make_change_request("ACR-002"), make_change_request("ACR-001"))
        ),
    }
    repositories.update(overrides)
    return repositories


def build_builder(
    repositories: Optional[dict[str, ReadOnlyRepository]] = None,
    *,
    cost: Optional[CostReader] = None,
    health: Optional[HealthReader] = None,
    clock: Any = fixed_clock,
) -> ReportBuilder:
    repos = repositories if repositories is not None else build_repositories()
    return ReportBuilder(
        repos["projects"],
        repos["steps"],
        repos["tasks"],
        repos["architecture_versions"],
        repos["adrs"],
        repos["risks"],
        repos["findings"],
        repos["decisions"],
        repos["change_requests"],
        cost_query=cost if cost is not None else CostReader(),
        health=health if health is not None else HealthReader(),
        clock=clock,
    )


def build_monitor(
    repositories: Optional[dict[str, ReadOnlyRepository]] = None,
    *,
    cost: Optional[CostReader] = None,
    health: Optional[HealthReader] = None,
) -> Monitor:
    return Monitor(build_builder(repositories, cost=cost, health=health))


class TestReadOnlyConstruction:
    """The monitor cannot write because it is never given anything that can."""

    def test_the_constructor_takes_only_the_projection(self) -> None:
        parameters = inspect.signature(Monitor.__init__).parameters

        assert list(parameters) == ["self", "report_builder"]
        for forbidden in (
            "storage",
            "transactions",
            "transaction",
            "cost",
            "audit",
            "repository",
            "clock",
        ):
            assert forbidden not in parameters

    def test_a_dependency_that_is_not_the_projection_is_refused(self) -> None:
        for wrong in (None, ReadOnlyRepository(), CostReader(), HealthReader()):
            with pytest.raises(ValueError, match="report_builder"):
                Monitor(wrong)

    def test_the_monitor_holds_nothing_but_the_projection(self) -> None:
        builder = build_builder()

        monitor = Monitor(builder)

        state = vars(monitor)
        assert set(state) == {"_report_builder"}
        assert state["_report_builder"] is builder
        assert isinstance(state["_report_builder"], ReportBuilder)

    def test_the_public_api_is_only_the_documented_read_surface(self) -> None:
        api = {name for name in dir(Monitor) if not name.startswith("_")}

        assert api == PUBLIC_API
        for name in PUBLIC_API:
            member = getattr(Monitor, name)
            assert callable(member), name
            assert (member.__doc__ or "").strip(), name

    def test_the_monitor_imports_no_write_port_and_no_infrastructure(self) -> None:
        targets = scan_directory(_src_root()).imports_of(
            f"{ROOT_PACKAGE}.application.monitor"
        )

        assert not any("infrastructure" in target for target in targets)
        assert not any(target == "sqlite3" for target in targets)
        assert not any("ports.transactions" in target for target in targets)
        assert not any("ports.storage" in target for target in targets)

    def test_the_monitor_imports_only_domain_ports_and_application(self) -> None:
        source = scan_directory(_src_root())
        layers = {
            layer_of(target)
            for target in source.imports_of(f"{ROOT_PACKAGE}.application.monitor")
            if target.startswith(ROOT_PACKAGE)
        }

        assert layers <= {"domain", "ports", "application"}

    def test_every_read_works_while_every_write_raises(self) -> None:
        """No fake write method may be reached by any read of the monitor."""
        monitor = build_monitor()

        assert isinstance(monitor.snapshot(), ReportSnapshot)
        assert isinstance(monitor.to_dict(), dict)
        assert isinstance(monitor.health(), LoopHealth)
        assert isinstance(monitor.current_step(), Step)
        assert monitor.next_step() is None
        assert monitor.step_states()
        assert isinstance(monitor.blocking_steps(), tuple)
        assert isinstance(monitor.open_risks(), tuple)
        assert isinstance(monitor.change_requests(), tuple)
        assert isinstance(monitor.cost_summary(), CostSummary)
        assert monitor.architecture_version() == "1.1"


class TestProjection:
    """The monitor is a pull-based view - it caches nothing and invents nothing."""

    def test_the_snapshot_is_the_canonical_projection(self) -> None:
        builder = build_builder()
        monitor = Monitor(builder)

        assert monitor.snapshot() == builder.build()

    def test_the_payload_is_the_projection_payload(self) -> None:
        builder = build_builder()

        payload = Monitor(builder).to_dict()

        assert payload == builder.build().to_dict()
        assert json.loads(json.dumps(payload)) == payload
        assert payload["schema_version"] == REPORT_SCHEMA_VERSION
        assert [
            request["request_id"] for request in payload["change_requests"]
        ] == ["ACR-001", "ACR-002"]

    def test_every_call_rereads_the_source_of_truth(self) -> None:
        repositories = build_repositories()
        monitor = build_monitor(repositories=repositories)

        assert monitor.step_states() == ((1, "VERIFIED"), (2, "READY"))

        repositories["steps"].items = (
            make_step(1),
            make_step(2),
            make_step(3),
        )

        assert monitor.step_states() == (
            (1, "VERIFIED"),
            (2, "VERIFIED"),
            (3, "VERIFIED"),
        )

    def test_the_health_seam_is_used_once_per_read(self) -> None:
        health = HealthReader()

        build_monitor(health=health).health()

        assert health.calls == 1


class TestLoopState:
    """The loop's own numbers are reported - never a second opinion."""

    def test_health_is_reused_verbatim(self) -> None:
        health = make_health(
            project_paused=True,
            step_count=7,
            current_step_no=2,
            current_state=StepState.CONFLICT,
            next_step_no=3,
            complete=False,
        )

        monitor = build_monitor(health=HealthReader(health))

        assert monitor.health() == health
        assert monitor.health().project_paused is True
        assert monitor.health().current_state is StepState.CONFLICT

    def test_current_step_is_the_lowest_step_that_is_not_verified(self) -> None:
        steps = (
            make_step(1, StepState.VERIFIED),
            make_step(2, StepState.BLOCKED),
            make_step(3, StepState.PENDING),
        )
        # The loop's own rule (`Orchestrator._current`): the lowest-numbered
        # step that is not VERIFIED. Computed here so the monitor's answer is
        # compared against the loop's rule instead of a copy of it.
        lowest_unverified = min(
            step.step_no
            for step in steps
            if step.state is not StepState.VERIFIED
        )

        monitor = build_monitor(
            repositories=build_repositories(steps=ReadOnlyRepository(steps)),
            health=HealthReader(make_health(current_step_no=lowest_unverified)),
        )

        assert lowest_unverified == 2
        assert monitor.current_step() is not None
        assert monitor.current_step().step_no == 2

    def test_the_monitor_does_not_re_derive_the_current_step(self) -> None:
        """A later READY step never displaces the loop's cursor."""
        repositories = build_repositories(
            steps=ReadOnlyRepository(
                (make_step(1, StepState.BLOCKED), make_step(2, StepState.READY))
            )
        )

        monitor = build_monitor(
            repositories=repositories,
            health=HealthReader(make_health(current_step_no=1, next_step_no=2)),
        )

        assert monitor.current_step().step_no == 1
        assert monitor.next_step().step_no == 2

    def test_a_finished_loop_reports_no_current_step(self) -> None:
        monitor = build_monitor(
            health=HealthReader(
                make_health(
                    current_step_no=None,
                    current_state=None,
                    next_step_no=None,
                    complete=True,
                )
            )
        )

        assert monitor.current_step() is None
        assert monitor.next_step() is None
        assert monitor.health().complete is True

    def test_a_reported_number_without_a_step_is_none(self) -> None:
        monitor = build_monitor(
            health=HealthReader(
                make_health(current_step_no=99, next_step_no=99)
            )
        )

        assert monitor.current_step() is None
        assert monitor.next_step() is None

    def test_step_states_are_ordered_by_step_number(self) -> None:
        monitor = build_monitor()

        assert monitor.step_states() == ((1, "VERIFIED"), (2, "READY"))


class TestBlockingSteps:
    """Blocking is the loop's halt set - not a second definition."""

    def test_blocking_is_exactly_the_loop_halt_set(self) -> None:
        steps = tuple(
            make_step(index, state)
            for index, state in enumerate(StepState, start=1)
        )
        monitor = build_monitor(
            repositories=build_repositories(steps=ReadOnlyRepository(steps))
        )

        blocking = monitor.blocking_steps()

        assert [step.step_no for step in blocking] == [
            index
            for index, state in enumerate(StepState, start=1)
            if state in HALTING_REASONS
        ]
        assert {step.state for step in blocking} == set(HALTING_REASONS)

    def test_the_halt_set_is_the_loop_three_human_states(self) -> None:
        assert set(HALTING_REASONS) == {
            StepState.WAITING_APPROVAL,
            StepState.BLOCKED,
            StepState.CONFLICT,
        }
        assert set(HALTING_REASONS.values()) == {
            "human-approval-required",
            "human-unblock-required",
            "human-conflict-resolution-required",
        }

    def test_revise_and_failed_are_not_blocking(self) -> None:
        """The loop resolves both itself, so neither is a human blocker."""
        monitor = build_monitor(
            repositories=build_repositories(
                steps=ReadOnlyRepository(
                    (
                        make_step(1, StepState.REVISE),
                        make_step(2, StepState.FAILED),
                    )
                )
            )
        )

        assert monitor.blocking_steps() == ()

    def test_working_and_terminal_states_are_not_blocking(self) -> None:
        for state in StepState:
            if state in HALTING_REASONS:
                continue
            monitor = build_monitor(
                repositories=build_repositories(
                    steps=ReadOnlyRepository((make_step(1, state),))
                )
            )
            assert monitor.blocking_steps() == (), state

    def test_blocking_follows_step_order(self) -> None:
        monitor = build_monitor(
            repositories=build_repositories(
                steps=ReadOnlyRepository(
                    (
                        make_step(5, StepState.CONFLICT),
                        make_step(2, StepState.READY),
                        make_step(3, StepState.BLOCKED),
                    )
                )
            )
        )

        assert [step.step_no for step in monitor.blocking_steps()] == [3, 5]

    def test_the_blocking_semantics_are_documented(self) -> None:
        documented = Monitor.blocking_steps.__doc__ or ""

        for state in HALTING_REASONS:
            assert state.value in documented, state
        assert "REVISE" in documented
        assert "FAILED" in documented
        assert "VERIFIED" in documented


class TestReadModel:
    """The remaining read accessors are pure selectors over the projection."""

    def test_open_risks_keeps_only_the_open_ones(self) -> None:
        monitor = build_monitor(
            repositories=build_repositories(
                risks=ReadOnlyRepository(
                    (
                        make_risk("RISK-003", RiskStatus.CLOSED),
                        make_risk("RISK-002", RiskStatus.MITIGATED),
                        make_risk("RISK-001"),
                    )
                )
            )
        )

        assert [risk.id for risk in monitor.open_risks()] == ["RISK-001"]
        assert monitor.open_risks()[0].status is RiskStatus.OPEN

    def test_change_requests_keep_the_projection_order(self) -> None:
        monitor = build_monitor()

        requests = monitor.change_requests()

        assert [request.request_id for request in requests] == [
            "ACR-001",
            "ACR-002",
        ]
        assert all(
            isinstance(request, ArchitectureChangeRequest)
            for request in requests
        )

    def test_change_requests_are_empty_when_nothing_was_proposed(self) -> None:
        monitor = build_monitor(
            repositories=build_repositories(
                change_requests=ReadOnlyRepository(())
            )
        )

        assert monitor.change_requests() == ()

    def test_cost_summary_is_the_projected_roll_up(self) -> None:
        cost = CostReader(
            total=CostSummary(
                total_usd=2.5,
                record_count=4,
                priced_record_count=4,
                unpriced_record_count=0,
            )
        )

        monitor = build_monitor(cost=cost)

        assert monitor.cost_summary().total_usd == 2.5
        assert monitor.cost_summary().record_count == 4

    def test_architecture_version_is_the_current_baseline(self) -> None:
        monitor = build_monitor()

        assert monitor.architecture_version() == "1.1"

    def test_architecture_version_is_none_without_a_current_baseline(self) -> None:
        monitor = build_monitor(
            repositories=build_repositories(
                architecture_versions=ReadOnlyRepository(
                    (make_architecture("1.0", False),)
                )
            )
        )

        assert monitor.architecture_version() is None


class TestLayerBoundary:
    """Step 19 added one application module and no permission anywhere."""

    def test_the_monitor_lives_in_the_application_layer(self) -> None:
        assert layer_of(f"{ROOT_PACKAGE}.application.monitor") == "application"

    def test_the_whole_tree_still_satisfies_the_current_baseline(self) -> None:
        result = ArchitectureValidator().validate(scan_directory(_src_root()))

        assert result.is_compliant is True
        assert result.violations == ()
        assert result.baseline_version == ARCHITECTURE_CURRENT.version
