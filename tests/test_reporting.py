"""Tests for the read-only reporting projection (Step 18).

The projection is the *single* read access every read-only consumer uses, so
these tests pin three properties: it reads only (never writes, never receives a
transaction or storage facade), it is deterministic (stable ordering, stable
payload), and it is honest about the source-of-truth invariants it relies on.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

from architecture_assistant.application import (
    REPORT_SCHEMA_VERSION,
    LoopHealth,
    ProjectMissingError,
    ReportBuilder,
    ReportingInvariantError,
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

NOW = datetime(2026, 9, 21, 16, 0, tzinfo=timezone.utc)

_WRITE_ERROR = "the reporting projection must never write to the source of truth"


def fixed_clock() -> datetime:
    return NOW


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


def make_step(step_no: int = 1, **overrides: Any) -> Step:
    data: dict[str, Any] = dict(
        step_no=step_no,
        phase=Phase.FOUNDATION,
        title=f"Step {step_no}",
        state=StepState.VERIFIED,
        attempt=1,
        max_attempts=3,
        risk=RiskLevel.LOW,
        requires_human=False,
        created_at=NOW,
        last_update_at=NOW,
    )
    data.update(overrides)
    return Step(**data)


def make_task(step_no: int = 1, attempt: int = 1, **overrides: Any) -> Task:
    data: dict[str, Any] = dict(
        step_no=step_no,
        phase=Phase.FOUNDATION,
        title=f"Task {step_no}.{attempt}",
        attempt=attempt,
        created_at=NOW,
    )
    data.update(overrides)
    return Task(**data)


def make_architecture(
    version: str = "1.0", is_current: bool = True, **overrides: Any
) -> ArchitectureVersion:
    data: dict[str, Any] = dict(
        version=version,
        baseline="Layered assistant core",
        rules=("RULE-1",),
        superseded_by=None if is_current else "9.9",
        is_current=is_current,
        created_at=NOW,
    )
    data.update(overrides)
    return ArchitectureVersion(**data)


def make_adr(adr_id: str = "ADR-001", **overrides: Any) -> ADR:
    data: dict[str, Any] = dict(
        id=adr_id,
        title=f"ADR {adr_id}",
        status=ADRStatus.ACCEPTED,
        context="context",
        decision="decision",
        created_at=NOW,
    )
    data.update(overrides)
    return ADR(**data)


def make_risk(risk_id: str = "RISK-001", **overrides: Any) -> Risk:
    data: dict[str, Any] = dict(
        id=risk_id,
        description=f"Risk {risk_id}",
        severity=Severity.HIGH,
        probability=0.3,
        impact=Severity.HIGH,
        owner="architect",
        status=RiskStatus.OPEN,
        created_at=NOW,
    )
    data.update(overrides)
    return Risk(**data)


def make_finding(finding_id: str = "F-1", **overrides: Any) -> Finding:
    data: dict[str, Any] = dict(
        id=finding_id,
        source="openai",
        claim="a claim",
        evidence=("application/context.py:12",),
        step_no=1,
        created_at=NOW,
    )
    data.update(overrides)
    return Finding(**data)


def make_decision(decision_id: str = "D-1", **overrides: Any) -> Decision:
    data: dict[str, Any] = dict(id=decision_id, step_no=1, created_at=NOW)
    data.update(overrides)
    return Decision(**data)


def make_change_request(
    request_id: str = "ACR-001", **overrides: Any
) -> ArchitectureChangeRequest:
    data: dict[str, Any] = dict(
        request_id=request_id,
        title=f"Change {request_id}",
        rationale="the baseline must move",
        source_version="1.0",
        target_version="1.1",
        rule_ids=("RULE-1",),
        adr_id="ADR-001",
        created_at=NOW,
    )
    data.update(overrides)
    return ArchitectureChangeRequest(**data)


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


def _src_root() -> Path:
    return Path(__file__).resolve().parents[1] / "src" / "architecture_assistant"


# ---------------------------------------------------------------------------
# read-only fake ports
# ---------------------------------------------------------------------------

class ReadOnlyRepository:
    """A repository port that only ever answers ``list()``.

    Every write method fails loudly, so a single accidental write anywhere in the
    projection becomes a test failure instead of a silent mutation.
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
        self.filters: list[CostQuery] = []

    def __call__(self, cost_filter: CostQuery) -> CostSummary:
        self.filters.append(cost_filter)
        if cost_filter.step_no is None:
            return self.total
        return self.by_step.get(cost_filter.step_no, CostSummary())

    def record(self, cost: Any) -> None:
        raise AssertionError("the reporting projection must never record cost")


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
            (make_step(2, state=StepState.READY), make_step(1))
        ),
        "tasks": ReadOnlyRepository(
            (make_task(2, 1), make_task(1, 2), make_task(1, 1))
        ),
        "architecture_versions": ReadOnlyRepository(
            (make_architecture("1.1"), make_architecture("1.0", False))
        ),
        "adrs": ReadOnlyRepository((make_adr("ADR-002"), make_adr("ADR-001"))),
        "risks": ReadOnlyRepository(
            (make_risk("RISK-002"), make_risk("RISK-001"))
        ),
        "findings": ReadOnlyRepository(
            (make_finding("F-2"), make_finding("F-1"))
        ),
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


class TestProjection:
    """The builder reads every aggregate and projects it deterministically."""

    def test_it_projects_the_whole_project(self) -> None:
        snapshot = build_builder().build()

        assert snapshot.project.name == "Architecture Lifecycle Assistant"
        assert [step.step_no for step in snapshot.steps] == [1, 2]
        assert snapshot.architecture is not None
        assert snapshot.architecture.version == "1.1"
        assert snapshot.schema_version == REPORT_SCHEMA_VERSION
        assert snapshot.generated_at == NOW

    def test_every_collection_is_ordered_deterministically(self) -> None:
        snapshot = build_builder().build()

        assert [step.step_no for step in snapshot.steps] == [1, 2]
        assert [(task.step_no, task.attempt) for task in snapshot.tasks] == [
            (1, 1),
            (1, 2),
            (2, 1),
        ]
        assert [v.version for v in snapshot.architecture_versions] == [
            "1.0",
            "1.1",
        ]
        assert [adr.id for adr in snapshot.adrs] == ["ADR-001", "ADR-002"]
        assert [risk.id for risk in snapshot.risks] == ["RISK-001", "RISK-002"]
        assert [f.id for f in snapshot.findings] == ["F-1", "F-2"]
        assert [d.id for d in snapshot.decisions] == ["D-1", "D-2"]
        assert [r.request_id for r in snapshot.change_requests] == [
            "ACR-001",
            "ACR-002",
        ]

    def test_identical_state_produces_an_identical_snapshot(self) -> None:
        first = build_builder().build()
        second = build_builder().build()

        assert first == second
        assert first.to_dict() == second.to_dict()

    def test_every_repository_is_read_exactly_once(self) -> None:
        repositories = build_repositories()

        build_builder(repositories).build()

        for name, repository in repositories.items():
            assert repository.list_calls == 1, name

    def test_cost_is_aggregated_total_and_per_step(self) -> None:
        cost = CostReader(
            total=CostSummary(
                total_usd=1.5,
                record_count=3,
                priced_record_count=2,
                unpriced_record_count=1,
            ),
            by_step={
                1: CostSummary(
                    total_usd=0.5, record_count=1, priced_record_count=1
                )
            },
        )

        snapshot = build_builder(cost=cost).build()

        assert snapshot.cost.total_usd == 1.5
        assert snapshot.cost.unpriced_record_count == 1
        assert [step_no for step_no, _ in snapshot.cost_by_step] == [1, 2]
        assert snapshot.cost_by_step[0][1].total_usd == 0.5
        assert snapshot.cost_by_step[1][1].record_count == 0
        # one unfiltered query, then one per step
        assert len(cost.filters) == 3
        assert cost.filters[0] == CostQuery()

    def test_the_health_seam_is_used(self) -> None:
        health = HealthReader(make_health(project_paused=True, complete=True))

        snapshot = build_builder(health=health).build()

        assert snapshot.health.project_paused is True
        assert snapshot.health.complete is True
        assert health.calls == 1

    def test_the_clock_is_injected(self) -> None:
        later = NOW.replace(hour=18)

        snapshot = build_builder(clock=lambda: later).build()

        assert snapshot.generated_at == later

    def test_the_projection_never_writes(self) -> None:
        """Every fake raises on write, so reaching the end proves read-only."""
        snapshot = build_builder().build()

        assert isinstance(snapshot, ReportSnapshot)


class TestReportSnapshot:
    def test_the_payload_is_json_safe(self) -> None:
        payload = build_builder().build().to_dict()

        assert json.loads(json.dumps(payload)) == payload

    def test_the_payload_carries_the_expected_sections(self) -> None:
        payload = build_builder().build().to_dict()

        assert set(payload) == {
            "schema_version",
            "generated_at",
            "project",
            "steps",
            "tasks",
            "architecture",
            "architecture_versions",
            "adrs",
            "risks",
            "findings",
            "decisions",
            "change_requests",
            "cost",
            "cost_by_step",
            "health",
        }
        assert payload["schema_version"] == REPORT_SCHEMA_VERSION
        assert payload["generated_at"] == NOW.isoformat()
        assert payload["cost"]["record_count"] == 0
        assert [
            request["request_id"] for request in payload["change_requests"]
        ] == ["ACR-001", "ACR-002"]

    def test_it_is_frozen(self) -> None:
        snapshot = build_builder().build()

        with pytest.raises(FrozenInstanceError):
            snapshot.project = make_project(name="other")


class TestInvariants:
    """The projection is honest about the invariants it relies on."""

    def test_a_missing_project_is_refused(self) -> None:
        repositories = build_repositories(projects=ReadOnlyRepository(()))

        with pytest.raises(ProjectMissingError):
            build_builder(repositories).build()

    def test_two_projects_are_refused(self) -> None:
        repositories = build_repositories(
            projects=ReadOnlyRepository(
                (make_project(), make_project(name="Second"))
            )
        )

        with pytest.raises(ReportingInvariantError, match="exactly one project"):
            build_builder(repositories).build()

    def test_two_current_baselines_are_refused(self) -> None:
        repositories = build_repositories(
            architecture_versions=ReadOnlyRepository(
                (make_architecture("1.0"), make_architecture("1.1"))
            )
        )

        with pytest.raises(
            ReportingInvariantError, match="current architecture"
        ):
            build_builder(repositories).build()

    def test_no_current_baseline_is_allowed(self) -> None:
        repositories = build_repositories(
            architecture_versions=ReadOnlyRepository(
                (make_architecture("1.0", False),)
            )
        )

        snapshot = build_builder(repositories).build()

        assert snapshot.architecture is None
        assert snapshot.architecture_versions[0].version == "1.0"

    @pytest.mark.parametrize("seam", ["cost_query", "health", "clock"])
    def test_the_seams_must_be_callable(self, seam) -> None:
        repositories = build_repositories()
        kwargs = {
            "cost_query": CostReader(),
            "health": HealthReader(),
            "clock": fixed_clock,
        }
        kwargs[seam] = "not callable"

        with pytest.raises(ValueError, match=seam):
            ReportBuilder(
                repositories["projects"],
                repositories["steps"],
                repositories["tasks"],
                repositories["architecture_versions"],
                repositories["adrs"],
                repositories["risks"],
                repositories["findings"],
                repositories["decisions"],
                repositories["change_requests"],
                **kwargs,
            )


class TestLayerBoundary:
    """The projection stays application-only and read-only."""

    def test_the_projection_imports_no_write_port(self) -> None:
        source = scan_directory(_src_root())
        targets = source.imports_of(f"{ROOT_PACKAGE}.application.reporting")

        assert not any("ports.transactions" in target for target in targets)
        assert not any("ports.storage" in target for target in targets)
        assert not any("infrastructure" in target for target in targets)
        assert not any("sqlite3" == target for target in targets)

    def test_the_builder_takes_no_write_seam(self) -> None:
        parameters = set(inspect.signature(ReportBuilder.__init__).parameters)

        for forbidden in ("storage", "transactions", "transaction", "audit"):
            assert forbidden not in parameters

    def test_the_projection_imports_only_domain_and_ports(self) -> None:
        source = scan_directory(_src_root())
        layers = {
            layer_of(target)
            for target in source.imports_of(f"{ROOT_PACKAGE}.application.reporting")
            if target.startswith(ROOT_PACKAGE)
        }

        assert layers <= {"domain", "ports", "application"}

    def test_the_whole_tree_still_satisfies_the_current_baseline(self) -> None:
        source = scan_directory(_src_root())
        result = ArchitectureValidator().validate(source)

        assert result.is_compliant is True
        assert result.violations == ()
        assert result.baseline_version == ARCHITECTURE_CURRENT.version
