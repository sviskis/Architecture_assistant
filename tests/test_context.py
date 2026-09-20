"""Unit and integration tests for the Context Manager (Step 3).

The unit tests use fake repository ports to prove that :class:`ContextBuilder`
depends only on the port contracts, performs targeted reads, never writes and
takes its timestamp from the injected clock. The integration tests wire the
builder to the real SQLite adapters over an in-memory database.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

from architecture_assistant.application import (
    SCHEMA_VERSION,
    ContextBuilder,
    ContextError,
    ContextInvariantError,
    ContextSnapshot,
    ProjectNotFoundError,
    StepNotFoundError,
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
    ArchitectureVersion,
    Project,
    Risk,
    Step,
)
from architecture_assistant.infrastructure import (
    TABLE_NAMES,
    SqliteADRRepository,
    SqliteArchitectureVersionRepository,
    SqliteProjectRepository,
    SqliteRiskRepository,
    SqliteStepRepository,
    close_database,
    open_database,
)
from architecture_assistant.ports.repositories import (
    ADRRepository,
    ArchitectureVersionRepository,
    ProjectRepository,
    RiskRepository,
    StepRepository,
)

NOW = datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc)
PROJECT_NAME = "Architecture Lifecycle Assistant"


def fixed_clock() -> datetime:
    """Deterministic clock used by every test."""
    return NOW


def make_project(name: str = PROJECT_NAME, **overrides: Any) -> Project:
    data: dict[str, Any] = dict(
        name=name,
        plan_version="0.2",
        plan_hash="70c12e690df0",
        mode=Mode.MANUAL,
        paused=False,
        current_step_no_snapshot=None,
        created_at=NOW,
        updated_at=NOW,
    )
    data.update(overrides)
    return Project(**data)


def make_step(
    step_no: int, state: StepState = StepState.PENDING, **overrides: Any
) -> Step:
    data: dict[str, Any] = dict(
        step_no=step_no,
        phase=overrides.pop("phase", Phase.CONTEXT),
        title=overrides.pop("title", f"Step {step_no}"),
        state=state,
        description="",
        attempt=0,
        max_attempts=3,
        risk=RiskLevel.MEDIUM,
        requires_human=False,
        created_at=NOW,
        started_at=None,
        finished_at=None,
        verified_at=None,
        last_update_at=NOW,
    )
    data.update(overrides)
    return Step(**data)


def make_adr(
    adr_id: str, status: ADRStatus = ADRStatus.ACCEPTED, **overrides: Any
) -> ADR:
    data: dict[str, Any] = dict(
        id=adr_id,
        title=f"ADR {adr_id}",
        status=status,
        context="context",
        decision="decision",
        consequences=(),
        version=1,
        superseded_by=None,
        related=(),
        created_at=NOW,
        updated_at=None,
    )
    data.update(overrides)
    return ADR(**data)


def make_risk(
    risk_id: str, status: RiskStatus = RiskStatus.OPEN, **overrides: Any
) -> Risk:
    data: dict[str, Any] = dict(
        id=risk_id,
        description=f"Risk {risk_id}",
        severity=Severity.HIGH,
        probability=0.3,
        impact=Severity.HIGH,
        owner="architect",
        mitigation="mitigation",
        status=status,
        created_at=NOW,
        updated_at=None,
    )
    data.update(overrides)
    return Risk(**data)


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


# ---------------------------------------------------------------------------
# fake ports (prove the builder is decoupled from infrastructure)
# ---------------------------------------------------------------------------

_WRITE_ERROR = "ContextBuilder must not write to the source of truth"


class FakeProjectRepository:
    """Read-only fake; any write or untargeted read fails loudly."""

    def __init__(self, projects: tuple[Project, ...] = ()) -> None:
        self.projects = tuple(projects)
        self.list_calls = 0

    def upsert(self, project: Project) -> None:
        raise AssertionError(_WRITE_ERROR)

    def get(self, name: str) -> Optional[Project]:
        raise AssertionError("ContextBuilder must use list() for projects")

    def list(self) -> tuple[Project, ...]:
        self.list_calls += 1
        return self.projects

    def delete(self, name: str) -> bool:
        raise AssertionError(_WRITE_ERROR)


class FakeStepRepository:
    """Read-only fake; ``list()`` is forbidden to enforce targeted reads."""

    def __init__(self, steps: tuple[Step, ...] = ()) -> None:
        self._steps = {step.step_no: step for step in steps}
        self.list_by_state_calls: list[Any] = []

    def upsert(self, step: Step) -> None:
        raise AssertionError(_WRITE_ERROR)

    def get(self, step_no: int) -> Optional[Step]:
        return self._steps.get(step_no)

    def list(self) -> tuple[Step, ...]:
        raise AssertionError("ContextBuilder must query steps by state")

    def delete(self, step_no: int) -> bool:
        raise AssertionError(_WRITE_ERROR)

    def list_by_state(self, state: StepState) -> tuple[Step, ...]:
        self.list_by_state_calls.append(state)
        wanted = state if isinstance(state, StepState) else StepState(state)
        return tuple(
            self._steps[key]
            for key in sorted(self._steps)
            if self._steps[key].state == wanted
        )


class FakeADRRepository:
    def __init__(self, adrs: tuple[ADR, ...] = ()) -> None:
        self.adrs = tuple(sorted(adrs, key=lambda adr: adr.id))
        self.list_calls = 0

    def upsert(self, adr: ADR) -> None:
        raise AssertionError(_WRITE_ERROR)

    def get(self, adr_id: str) -> Optional[ADR]:
        raise AssertionError("ContextBuilder must use list() for ADRs")

    def list(self) -> tuple[ADR, ...]:
        self.list_calls += 1
        return self.adrs

    def delete(self, adr_id: str) -> bool:
        raise AssertionError(_WRITE_ERROR)


class FakeRiskRepository:
    """Read-only fake; only ``list_open()`` is allowed."""

    def __init__(self, risks: tuple[Risk, ...] = ()) -> None:
        self.risks = tuple(sorted(risks, key=lambda risk: risk.id))
        self.list_open_calls = 0

    def upsert(self, risk: Risk) -> None:
        raise AssertionError(_WRITE_ERROR)

    def get(self, risk_id: str) -> Optional[Risk]:
        raise AssertionError("ContextBuilder must use list_open() for risks")

    def list(self) -> tuple[Risk, ...]:
        raise AssertionError("ContextBuilder must use list_open() for risks")

    def delete(self, risk_id: str) -> bool:
        raise AssertionError(_WRITE_ERROR)

    def list_open(self) -> tuple[Risk, ...]:
        self.list_open_calls += 1
        return tuple(risk for risk in self.risks if risk.status == RiskStatus.OPEN)


class FakeArchitectureVersionRepository:
    def __init__(self, versions: tuple[ArchitectureVersion, ...] = ()) -> None:
        self.versions = tuple(sorted(versions, key=lambda version: version.version))
        self.list_calls = 0

    def upsert(self, version: ArchitectureVersion) -> None:
        raise AssertionError(_WRITE_ERROR)

    def get(self, version: str) -> Optional[ArchitectureVersion]:
        raise AssertionError("ContextBuilder must use list() for architecture")

    def list(self) -> tuple[ArchitectureVersion, ...]:
        self.list_calls += 1
        return self.versions

    def delete(self, version: str) -> bool:
        raise AssertionError(_WRITE_ERROR)


def make_builder(
    *,
    projects: tuple[Project, ...] = (),
    steps: tuple[Step, ...] = (),
    adrs: tuple[ADR, ...] = (),
    risks: tuple[Risk, ...] = (),
    versions: tuple[ArchitectureVersion, ...] = (),
    clock: Callable[[], datetime] = fixed_clock,
) -> tuple[ContextBuilder, dict[str, Any]]:
    """Build a ContextBuilder wired to fake ports, plus the fakes themselves."""
    repositories: dict[str, Any] = {
        "projects": FakeProjectRepository(projects),
        "steps": FakeStepRepository(steps),
        "adrs": FakeADRRepository(adrs),
        "risks": FakeRiskRepository(risks),
        "architecture_versions": FakeArchitectureVersionRepository(versions),
    }
    builder = ContextBuilder(
        repositories["projects"],
        repositories["steps"],
        repositories["adrs"],
        repositories["risks"],
        repositories["architecture_versions"],
        clock=clock,
    )
    return builder, repositories


def build_with(step_no: int, **kwargs: Any) -> ContextSnapshot:
    """Build a snapshot through the fake-port builder."""
    builder, _ = make_builder(**kwargs)
    return builder.build(step_no)



# ---------------------------------------------------------------------------
# fake-port unit tests
# ---------------------------------------------------------------------------

class TestFakePortsMatchProtocols:
    @pytest.mark.parametrize(
        "fake,port",
        [
            (FakeProjectRepository(), ProjectRepository),
            (FakeStepRepository(), StepRepository),
            (FakeADRRepository(), ADRRepository),
            (FakeRiskRepository(), RiskRepository),
            (FakeArchitectureVersionRepository(), ArchitectureVersionRepository),
        ],
        ids=["project", "step", "adr", "risk", "architecture_version"],
    )
    def test_fake_satisfies_its_port(self, fake: Any, port: type) -> None:
        assert isinstance(fake, port)


class TestErrorHierarchy:
    def test_all_context_errors_share_a_base(self) -> None:
        assert issubclass(StepNotFoundError, ContextError)
        assert issubclass(ProjectNotFoundError, ContextError)
        assert issubclass(ContextInvariantError, ContextError)


class TestProjectInvariant:
    def test_no_project_raises_project_not_found(self) -> None:
        builder, _ = make_builder(steps=(make_step(3),))
        with pytest.raises(ProjectNotFoundError, match="no project"):
            builder.build(3)

    def test_exactly_one_project_is_used(self) -> None:
        project = make_project()
        snapshot = build_with(3, projects=(project,), steps=(make_step(3),))
        assert snapshot.project == project

    def test_more_than_one_project_raises_invariant_error(self) -> None:
        builder, _ = make_builder(
            projects=(make_project("Zeta"), make_project("Alpha")),
            steps=(make_step(3),),
        )
        with pytest.raises(ContextInvariantError, match="exactly one project"):
            builder.build(3)

    def test_invariant_error_lists_the_conflicting_names(self) -> None:
        builder, _ = make_builder(
            projects=(make_project("Zeta"), make_project("Alpha")),
            steps=(make_step(3),),
        )
        with pytest.raises(ContextInvariantError, match="Alpha, Zeta"):
            builder.build(3)


class TestArchitectureInvariant:
    def test_no_current_version_yields_none(self) -> None:
        snapshot = build_with(3, projects=(make_project(),), steps=(make_step(3),))
        assert snapshot.architecture is None

    def test_only_superseded_versions_yield_none(self) -> None:
        snapshot = build_with(
            3,
            projects=(make_project(),),
            steps=(make_step(3),),
            versions=(make_architecture("0.9", is_current=False),),
        )
        assert snapshot.architecture is None

    def test_single_current_version_is_selected(self) -> None:
        current = make_architecture("1.0", is_current=True)
        snapshot = build_with(
            3,
            projects=(make_project(),),
            steps=(make_step(3),),
            versions=(make_architecture("0.9", is_current=False), current),
        )
        assert snapshot.architecture == current

    def test_multiple_current_versions_raise_invariant_error(self) -> None:
        builder, _ = make_builder(
            projects=(make_project(),),
            steps=(make_step(3),),
            versions=(
                make_architecture("1.0", is_current=True),
                make_architecture("2.0", is_current=True),
            ),
        )
        with pytest.raises(ContextInvariantError, match="at most one current"):
            builder.build(3)


class TestStepSelection:
    def test_missing_step_raises_step_not_found(self) -> None:
        builder, _ = make_builder(projects=(make_project(),))
        with pytest.raises(StepNotFoundError, match="step 9 does not exist"):
            builder.build(9)

    def test_target_step_is_included_unchanged(self) -> None:
        target = make_step(3, StepState.READY)
        snapshot = build_with(3, projects=(make_project(),), steps=(target,))
        assert snapshot.step == target
        assert snapshot.step.state is StepState.READY

    def test_previous_steps_include_only_verified_before_target(self) -> None:
        steps = (
            make_step(1, StepState.VERIFIED),
            make_step(2, StepState.BLOCKED),
            make_step(3, StepState.READY),
            make_step(4, StepState.VERIFIED),
        )
        snapshot = build_with(3, projects=(make_project(),), steps=steps)
        assert [step.step_no for step in snapshot.previous_steps] == [1]

    def test_previous_steps_are_ordered_by_step_no(self) -> None:
        steps = (
            make_step(3, StepState.READY),
            make_step(2, StepState.VERIFIED),
            make_step(1, StepState.VERIFIED),
        )
        snapshot = build_with(3, projects=(make_project(),), steps=steps)
        assert [step.step_no for step in snapshot.previous_steps] == [1, 2]

    def test_target_step_is_never_its_own_previous_step(self) -> None:
        snapshot = build_with(
            3,
            projects=(make_project(),),
            steps=(make_step(3, StepState.VERIFIED),),
        )
        assert snapshot.previous_steps == ()



class TestAdrSelection:
    def test_only_accepted_adrs_are_relevant(self) -> None:
        adrs = (
            make_adr("ADR-001", ADRStatus.ACCEPTED),
            make_adr("ADR-002", ADRStatus.PROPOSED),
            make_adr("ADR-003", ADRStatus.REJECTED),
            make_adr("ADR-004", ADRStatus.DEPRECATED),
            make_adr("ADR-005", ADRStatus.SUPERSEDED, superseded_by="ADR-006"),
            make_adr("ADR-006", ADRStatus.ACCEPTED),
        )
        snapshot = build_with(
            3, projects=(make_project(),), steps=(make_step(3),), adrs=adrs
        )
        assert [adr.id for adr in snapshot.adrs] == ["ADR-001", "ADR-006"]

    def test_adrs_are_ordered_by_id(self) -> None:
        adrs = (make_adr("ADR-010"), make_adr("ADR-002"), make_adr("ADR-007"))
        snapshot = build_with(
            3, projects=(make_project(),), steps=(make_step(3),), adrs=adrs
        )
        assert [adr.id for adr in snapshot.adrs] == ["ADR-002", "ADR-007", "ADR-010"]


class TestRiskSelection:
    def test_only_open_risks_are_relevant(self) -> None:
        risks = (
            make_risk("RISK-001", RiskStatus.OPEN),
            make_risk("RISK-002", RiskStatus.CLOSED),
            make_risk("RISK-003", RiskStatus.MITIGATED),
            make_risk("RISK-004", RiskStatus.ACCEPTED),
            make_risk("RISK-005", RiskStatus.OPEN),
        )
        snapshot = build_with(
            3, projects=(make_project(),), steps=(make_step(3),), risks=risks
        )
        assert [risk.id for risk in snapshot.risks] == ["RISK-001", "RISK-005"]

    def test_no_open_risks_yields_empty_tuple(self) -> None:
        snapshot = build_with(
            3,
            projects=(make_project(),),
            steps=(make_step(3),),
            risks=(make_risk("RISK-001", RiskStatus.CLOSED),),
        )
        assert snapshot.risks == ()


class TestTargetedReadsAndNoWrites:
    def test_builder_uses_only_the_targeted_queries(self) -> None:
        builder, repositories = make_builder(
            projects=(make_project(),),
            steps=(make_step(3, StepState.READY),),
            adrs=(make_adr("ADR-001"),),
            risks=(make_risk("RISK-001"),),
            versions=(make_architecture(),),
        )
        builder.build(3)

        assert repositories["projects"].list_calls == 1
        assert repositories["steps"].list_by_state_calls == [StepState.VERIFIED]
        assert repositories["adrs"].list_calls == 1
        assert repositories["risks"].list_open_calls == 1
        assert repositories["architecture_versions"].list_calls == 1

    def test_build_succeeds_although_every_write_and_untargeted_read_raises(
        self,
    ) -> None:
        # The fakes raise AssertionError on writes (and on non-targeted reads),
        # so a successful build proves only reads were performed.
        snapshot = build_with(
            3,
            projects=(make_project(),),
            steps=(make_step(3, StepState.READY),),
            adrs=(make_adr("ADR-001"),),
            risks=(make_risk("RISK-001"),),
            versions=(make_architecture(),),
        )
        assert snapshot.step.step_no == 3

    def test_step_query_is_never_repeated_within_one_build(self) -> None:
        builder, repositories = make_builder(
            projects=(make_project(),), steps=(make_step(3),)
        )
        builder.build(3)
        assert len(repositories["steps"].list_by_state_calls) == 1


class TestClockInjection:
    def test_generated_at_comes_from_the_injected_clock(self) -> None:
        sentinel = datetime(2030, 1, 1, 0, 0, tzinfo=timezone.utc)
        snapshot = build_with(
            3,
            projects=(make_project(),),
            steps=(make_step(3),),
            clock=lambda: sentinel,
        )
        assert snapshot.generated_at == sentinel

    def test_clock_is_a_required_keyword_argument(self) -> None:
        with pytest.raises(TypeError):
            ContextBuilder(  # type: ignore[call-arg]
                FakeProjectRepository(),
                FakeStepRepository(),
                FakeADRRepository(),
                FakeRiskRepository(),
                FakeArchitectureVersionRepository(),
            )

    def test_non_callable_clock_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="clock must be a callable"):
            ContextBuilder(
                FakeProjectRepository(),
                FakeStepRepository(),
                FakeADRRepository(),
                FakeRiskRepository(),
                FakeArchitectureVersionRepository(),
                clock="not-a-clock",  # type: ignore[arg-type]
            )

    def test_module_never_reads_the_system_time(self) -> None:
        import architecture_assistant.application.context as module

        source = Path(module.__file__ or "").read_text(encoding="utf-8")
        assert "datetime.now(" not in source
        assert "utc_now(" not in source



# ---------------------------------------------------------------------------
# snapshot semantics
# ---------------------------------------------------------------------------

class TestSnapshotSemantics:
    def test_snapshot_is_immutable(self) -> None:
        snapshot = build_with(3, projects=(make_project(),), steps=(make_step(3),))
        with pytest.raises(FrozenInstanceError):
            snapshot.step = make_step(4)  # type: ignore[misc]

    def test_snapshot_is_ephemeral_and_has_no_repository(self) -> None:
        snapshot = build_with(3, projects=(make_project(),), steps=(make_step(3),))
        assert not hasattr(snapshot, "save")
        assert not hasattr(snapshot, "repository")

    def test_to_dict_is_json_serializable(self) -> None:
        snapshot = build_with(
            3,
            projects=(make_project(),),
            steps=(make_step(3, StepState.READY),),
            adrs=(make_adr("ADR-001"),),
            risks=(make_risk("RISK-001"),),
            versions=(make_architecture(),),
        )
        data = snapshot.to_dict()
        assert json.loads(json.dumps(data)) == data

    def test_to_dict_reports_schema_version_and_timestamp(self) -> None:
        data = build_with(
            3, projects=(make_project(),), steps=(make_step(3),)
        ).to_dict()
        assert data["schema_version"] == SCHEMA_VERSION
        assert data["generated_at"] == NOW.isoformat()

    def test_to_dict_represents_absent_architecture_as_none(self) -> None:
        data = build_with(
            3, projects=(make_project(),), steps=(make_step(3),)
        ).to_dict()
        assert data["architecture"] is None

    def test_to_dict_is_deterministic(self) -> None:
        snapshot = build_with(
            3,
            projects=(make_project(),),
            steps=(make_step(3),),
            adrs=(make_adr("ADR-001"),),
            risks=(make_risk("RISK-001"),),
        )
        assert snapshot.to_dict() == snapshot.to_dict()

    def test_same_inputs_produce_equal_snapshots(self) -> None:
        kwargs: dict[str, Any] = dict(
            projects=(make_project(),),
            steps=(make_step(3, StepState.READY), make_step(1, StepState.VERIFIED)),
            adrs=(make_adr("ADR-001"),),
            risks=(make_risk("RISK-001"),),
            versions=(make_architecture(),),
        )
        assert build_with(3, **kwargs) == build_with(3, **kwargs)

    def test_to_dict_exposes_only_the_enumerated_context(self) -> None:
        data = build_with(3, projects=(make_project(),), steps=(make_step(3),)).to_dict()
        assert set(data) == {
            "schema_version",
            "generated_at",
            "project",
            "step",
            "previous_steps",
            "architecture",
            "adrs",
            "risks",
        }



# ---------------------------------------------------------------------------
# SQLite integration (real adapters over a real database)
# ---------------------------------------------------------------------------

@pytest.fixture
def connection():
    conn = open_database(":memory:")
    yield conn
    close_database(conn)


def _row_counts(connection) -> dict[str, int]:
    return {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in TABLE_NAMES
    }


def _seed_source_of_truth(connection) -> None:
    SqliteProjectRepository(connection).upsert(make_project())

    steps = SqliteStepRepository(connection)
    steps.upsert(make_step(1, StepState.VERIFIED, title="Step 1"))
    steps.upsert(make_step(2, StepState.VERIFIED, title="Step 2"))
    steps.upsert(make_step(3, StepState.READY, title="Step 3"))
    steps.upsert(make_step(4, StepState.PENDING, title="Step 4"))

    adrs = SqliteADRRepository(connection)
    adrs.upsert(make_adr("ADR-001", ADRStatus.ACCEPTED))
    adrs.upsert(make_adr("ADR-002", ADRStatus.PROPOSED))
    adrs.upsert(
        make_adr("ADR-003", ADRStatus.SUPERSEDED, superseded_by="ADR-001")
    )

    risks = SqliteRiskRepository(connection)
    risks.upsert(make_risk("RISK-001", RiskStatus.OPEN))
    risks.upsert(make_risk("RISK-002", RiskStatus.CLOSED))

    SqliteArchitectureVersionRepository(connection).upsert(
        make_architecture("1.0", is_current=True)
    )


def _sqlite_builder(connection) -> ContextBuilder:
    return ContextBuilder(
        SqliteProjectRepository(connection),
        SqliteStepRepository(connection),
        SqliteADRRepository(connection),
        SqliteRiskRepository(connection),
        SqliteArchitectureVersionRepository(connection),
        clock=fixed_clock,
    )


class TestSqliteIntegration:
    def test_builds_snapshot_from_the_source_of_truth(self, connection) -> None:
        _seed_source_of_truth(connection)
        snapshot = _sqlite_builder(connection).build(3)

        assert snapshot.project == make_project()
        assert snapshot.step.step_no == 3
        assert snapshot.step.state is StepState.READY
        assert [step.step_no for step in snapshot.previous_steps] == [1, 2]
        assert [adr.id for adr in snapshot.adrs] == ["ADR-001"]
        assert [risk.id for risk in snapshot.risks] == ["RISK-001"]
        assert snapshot.architecture is not None
        assert snapshot.architecture.version == "1.0"
        assert snapshot.schema_version == SCHEMA_VERSION
        assert snapshot.generated_at == NOW

    def test_build_does_not_write_to_the_database(self, connection) -> None:
        _seed_source_of_truth(connection)
        before = _row_counts(connection)
        builder = _sqlite_builder(connection)
        builder.build(1)
        builder.build(3)
        assert _row_counts(connection) == before

    def test_repeated_builds_are_deterministic(self, connection) -> None:
        _seed_source_of_truth(connection)
        builder = _sqlite_builder(connection)
        first = builder.build(3)
        second = builder.build(3)
        assert first == second
        assert first.to_dict() == second.to_dict()

    def test_reconnect_rebuilds_the_same_context(self, tmp_path: Path) -> None:
        path = tmp_path / "data" / "architecture_assistant.db"

        first_connection = open_database(path)
        try:
            _seed_source_of_truth(first_connection)
            before = _sqlite_builder(first_connection).build(3)
        finally:
            close_database(first_connection)

        second_connection = open_database(path)
        try:
            assert _sqlite_builder(second_connection).build(3) == before
        finally:
            close_database(second_connection)

    def test_missing_project_raises_project_not_found(self, connection) -> None:
        SqliteStepRepository(connection).upsert(make_step(3))
        with pytest.raises(ProjectNotFoundError):
            _sqlite_builder(connection).build(3)

    def test_multiple_projects_raise_invariant_error(self, connection) -> None:
        SqliteProjectRepository(connection).upsert(make_project("Zeta"))
        SqliteProjectRepository(connection).upsert(make_project("Alpha"))
        SqliteStepRepository(connection).upsert(make_step(3))
        with pytest.raises(ContextInvariantError):
            _sqlite_builder(connection).build(3)

    def test_missing_step_raises_step_not_found(self, connection) -> None:
        SqliteProjectRepository(connection).upsert(make_project())
        with pytest.raises(StepNotFoundError):
            _sqlite_builder(connection).build(99)

    def test_architecture_is_none_until_a_baseline_exists(self, connection) -> None:
        SqliteProjectRepository(connection).upsert(make_project())
        SqliteStepRepository(connection).upsert(make_step(3))
        assert _sqlite_builder(connection).build(3).architecture is None

    def test_multiple_current_architectures_raise(self, connection) -> None:
        SqliteProjectRepository(connection).upsert(make_project())
        SqliteStepRepository(connection).upsert(make_step(3))
        versions = SqliteArchitectureVersionRepository(connection)
        versions.upsert(make_architecture("1.0", is_current=True))
        versions.upsert(make_architecture("2.0", is_current=True))
        with pytest.raises(ContextInvariantError):
            _sqlite_builder(connection).build(3)

