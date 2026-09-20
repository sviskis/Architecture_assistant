"""Unit tests for the concrete SQLite repository adapters.

Coverage: port conformance, upsert/get round-trips, idempotent upserts, delete
semantics, deterministic list ordering, adapter-specific queries, the
``tasks.step_no -> steps.step_no`` foreign key, row-identity preservation on
upsert (proving ``ON CONFLICT ... DO UPDATE`` is used instead of
``INSERT OR REPLACE``) and restart recovery on a file-backed database.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

from architecture_assistant.domain.enums import (
    ACRStatus,
    ADRStatus,
    DecisionStatus,
    Mode,
    Phase,
    ReportStatus,
    RiskLevel,
    RiskStatus,
    Severity,
    StepState,
    TaskState,
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
from architecture_assistant.infrastructure import (
    DEFAULT_DATABASE_PATH,
    MIGRATIONS,
    SqliteADRRepository,
    SqliteArchitectureChangeRequestRepository,
    SqliteArchitectureVersionRepository,
    SqliteDecisionRepository,
    SqliteFindingRepository,
    SqliteProjectRepository,
    SqliteRiskRepository,
    SqliteStepRepository,
    SqliteTaskRepository,
    applied_versions,
    close_database,
    open_database,
)
from architecture_assistant.ports.repositories import (
    ADRRepository,
    ArchitectureChangeRequestRepository,
    ArchitectureVersionRepository,
    DecisionRepository,
    FindingRepository,
    ProjectRepository,
    RiskRepository,
    StepRepository,
    TaskKey,
    TaskRepository,
)

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 9, 20, 13, 30, tzinfo=timezone.utc)

PROJECT_NAME = "Architecture Lifecycle Assistant"

#: Every migration version, derived from the migration list itself.
EXPECTED_MIGRATION_VERSIONS = tuple(
    migration.version for migration in MIGRATIONS
)


def make_project(**overrides: Any) -> Project:
    data: dict[str, Any] = dict(
        name=PROJECT_NAME,
        plan_version="0.2",
        plan_hash="70c12e690df0",
        mode=Mode.SUPERVISED,
        paused=True,
        current_step_no_snapshot=2,
        created_at=NOW,
        updated_at=LATER,
    )
    data.update(overrides)
    return Project(**data)


def make_step(**overrides: Any) -> Step:
    data: dict[str, Any] = dict(
        step_no=2,
        phase=Phase.FOUNDATION,
        title="SQLite schema + repository ports",
        state=StepState.READY,
        description="tables, migrations, ports",
        attempt=1,
        max_attempts=3,
        risk=RiskLevel.HIGH,
        requires_human=True,
        created_at=NOW,
        started_at=LATER,
        finished_at=None,
        verified_at=None,
        last_update_at=LATER,
    )
    data.update(overrides)
    return Step(**data)


def make_task(**overrides: Any) -> Task:
    data: dict[str, Any] = dict(
        step_no=2,
        phase=Phase.FOUNDATION,
        title="SQLite schema + repository ports",
        description="dispatch unit",
        risk=RiskLevel.HIGH,
        attempt=1,
        max_attempts=3,
        state=TaskState.DISPATCHED,
        context_file=".mini_build/context/step_002_context.json",
        instructions={"plan_first": True, "scope": "Only current step"},
        report_schema={"status": "DONE|REVISE|BLOCKED|FAILED"},
        report_status=ReportStatus.DONE,
        created_at=NOW,
    )
    data.update(overrides)
    return Task(**data)


def make_architecture_version(**overrides: Any) -> ArchitectureVersion:
    data: dict[str, Any] = dict(
        version="1.0",
        baseline="Layered assistant core",
        rules=("no-sqlite-in-domain", "no-api-in-domain"),
        superseded_by=None,
        is_current=True,
        created_at=NOW,
    )
    data.update(overrides)
    return ArchitectureVersion(**data)


def make_adr(**overrides: Any) -> ADR:
    data: dict[str, Any] = dict(
        id="ADR-001",
        title="Use SQLite as source of truth",
        status=ADRStatus.ACCEPTED,
        context="State must survive restarts",
        decision="SQLite is authoritative; Excel is an export only",
        consequences=("Crash-safe state", "No Excel editing authority"),
        version=2,
        superseded_by=None,
        related=("ADR-002",),
        created_at=NOW,
        updated_at=LATER,
    )
    data.update(overrides)
    return ADR(**data)


def make_risk(**overrides: Any) -> Risk:
    data: dict[str, Any] = dict(
        id="RISK-001",
        description="Architecture drift during long builds",
        severity=Severity.HIGH,
        probability=0.25,
        impact=Severity.CRITICAL,
        owner="architect",
        mitigation="Deterministic architecture validator",
        status=RiskStatus.MITIGATED,
        created_at=NOW,
        updated_at=None,
    )
    data.update(overrides)
    return Risk(**data)


def make_finding(**overrides: Any) -> Finding:
    data: dict[str, Any] = dict(
        id="F-001",
        source="claude",
        claim="Domain layer leaks persistence concerns",
        evidence=("models.py imports sqlite3", "no repository port"),
        confidence=0.8,
        severity=Severity.HIGH,
        step_no=3,
        created_at=NOW,
    )
    data.update(overrides)
    return Finding(**data)


def make_decision(**overrides: Any) -> Decision:
    data: dict[str, Any] = dict(
        id="D-001",
        status=DecisionStatus.ACCEPTED,
        decision="Reject the finding",
        rationale="No persistence import present in the domain layer",
        rules_applied=("RULE-DOMAIN-01",),
        evidence_refs=("F-001",),
        perspectives=("openai", "claude", "grok"),
        step_no=3,
        created_at=NOW,
    )
    data.update(overrides)
    return Decision(**data)


def make_change_request(**overrides: Any) -> ArchitectureChangeRequest:
    data: dict[str, Any] = dict(
        request_id="ACR-001",
        title="Admit the composition root as architecture v1.1",
        rationale="The object graph could only ever be wired by the test suite.",
        source_version="1.0",
        target_version="1.1",
        rule_ids=("unknown-layer", "forbidden-layer-import"),
        adr_id="ADR-008",
        roadmap_impact=("step-009", "step-010", "step-011"),
        status=ACRStatus.APPLIED,
        created_at=NOW,
        approved_by="user",
        approved_at=NOW,
        applied_at=LATER,
    )
    data.update(overrides)
    return ArchitectureChangeRequest(**data)


def _seed_steps(connection: sqlite3.Connection, *step_numbers: int) -> None:
    """Create the Steps referenced by task rows (foreign-key target)."""
    repository = SqliteStepRepository(connection)
    for step_no in step_numbers:
        repository.upsert(make_step(step_no=step_no, title=f"Step {step_no}"))


@dataclass(frozen=True)
class RepoCase:
    """One repository adapter plus the data needed by the generic tests."""

    name: str
    build: Callable[[sqlite3.Connection], Any]
    port: type
    sample: Callable[..., Any]
    key: Any
    key_of: Callable[[Any], Any]
    ordered: Callable[[], tuple]
    count_sql: str
    seed: Optional[Callable[[sqlite3.Connection], None]] = None


CASES: tuple[RepoCase, ...] = (
    RepoCase(
        name="project",
        build=SqliteProjectRepository,
        port=ProjectRepository,
        sample=make_project,
        key=PROJECT_NAME,
        key_of=lambda model: model.name,
        ordered=lambda: (
            make_project(name="Zeta", plan_version="0.2"),
            make_project(name="Alpha", plan_version="0.2"),
        ),
        count_sql="SELECT COUNT(*) FROM project",
    ),
    RepoCase(
        name="step",
        build=SqliteStepRepository,
        port=StepRepository,
        sample=make_step,
        key=2,
        key_of=lambda model: model.step_no,
        ordered=lambda: (
            make_step(step_no=5, title="Step 5"),
            make_step(step_no=3, title="Step 3"),
        ),
        count_sql="SELECT COUNT(*) FROM steps",
    ),
    RepoCase(
        name="task",
        build=SqliteTaskRepository,
        port=TaskRepository,
        sample=make_task,
        key=TaskKey(step_no=2, attempt=1),
        key_of=lambda model: (model.step_no, model.attempt),
        ordered=lambda: (
            make_task(step_no=3, attempt=1, title="Step 3 attempt 1"),
            make_task(step_no=2, attempt=2, title="Step 2 attempt 2"),
            make_task(step_no=2, attempt=1, title="Step 2 attempt 1"),
        ),
        count_sql="SELECT COUNT(*) FROM tasks",
        seed=lambda connection: _seed_steps(connection, 1, 2, 3, 4, 5),
    ),
    RepoCase(
        name="architecture_version",
        build=SqliteArchitectureVersionRepository,
        port=ArchitectureVersionRepository,
        sample=make_architecture_version,
        key="1.0",
        key_of=lambda model: model.version,
        ordered=lambda: (
            make_architecture_version(version="2.0"),
            make_architecture_version(version="1.0"),
        ),
        count_sql="SELECT COUNT(*) FROM architecture_versions",
    ),
    RepoCase(
        name="adr",
        build=SqliteADRRepository,
        port=ADRRepository,
        sample=make_adr,
        key="ADR-001",
        key_of=lambda model: model.id,
        ordered=lambda: (
            make_adr(id="ADR-002", version=1),
            make_adr(id="ADR-001"),
        ),
        count_sql="SELECT COUNT(*) FROM adrs",
    ),
    RepoCase(
        name="risk",
        build=SqliteRiskRepository,
        port=RiskRepository,
        sample=make_risk,
        key="RISK-001",
        key_of=lambda model: model.id,
        ordered=lambda: (
            make_risk(id="RISK-002"),
            make_risk(id="RISK-001"),
        ),
        count_sql="SELECT COUNT(*) FROM risks",
    ),
    RepoCase(
        name="finding",
        build=SqliteFindingRepository,
        port=FindingRepository,
        sample=make_finding,
        key="F-001",
        key_of=lambda model: model.id,
        ordered=lambda: (
            make_finding(id="F-002"),
            make_finding(id="F-001"),
        ),
        count_sql="SELECT COUNT(*) FROM findings",
    ),
    RepoCase(
        name="decision",
        build=SqliteDecisionRepository,
        port=DecisionRepository,
        sample=make_decision,
        key="D-001",
        key_of=lambda model: model.id,
        ordered=lambda: (
            make_decision(id="D-002"),
            make_decision(id="D-001"),
        ),
        count_sql="SELECT COUNT(*) FROM decisions",
    ),
    RepoCase(
        name="change_request",
        build=SqliteArchitectureChangeRequestRepository,
        port=ArchitectureChangeRequestRepository,
        sample=make_change_request,
        key="ACR-001",
        key_of=lambda model: model.request_id,
        ordered=lambda: (
            make_change_request(request_id="ACR-002", title="Second change"),
            make_change_request(request_id="ACR-001"),
        ),
        count_sql="SELECT COUNT(*) FROM architecture_change_requests",
    ),
)

CASE_IDS = [case.name for case in CASES]

@pytest.fixture
def connection():
    conn = open_database(":memory:")
    yield conn
    close_database(conn)


def _repository(connection: sqlite3.Connection, case: RepoCase):
    """Seed any required parent rows and build the adapter under test."""
    if case.seed is not None:
        case.seed(connection)
    return case.build(connection)


class TestPortConformance:
    @pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
    def test_adapter_satisfies_its_port(self, connection, case: RepoCase) -> None:
        assert isinstance(_repository(connection, case), case.port)


class TestGenericRepositoryBehaviour:
    @pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
    def test_round_trip_preserves_the_domain_model(
        self, connection, case: RepoCase
    ) -> None:
        repository = _repository(connection, case)
        model = case.sample()
        repository.upsert(model)
        stored = repository.get(case.key)
        assert stored is not None
        assert stored == model
        assert stored.to_dict() == model.to_dict()

    @pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
    def test_repeated_upserts_keep_a_single_row(
        self, connection, case: RepoCase
    ) -> None:
        repository = _repository(connection, case)
        model = case.sample()
        repository.upsert(model)
        repository.upsert(model)
        repository.upsert(model)
        assert connection.execute(case.count_sql).fetchone()[0] == 1

    @pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
    def test_empty_repository_returns_none_and_empty_list(
        self, connection, case: RepoCase
    ) -> None:
        repository = _repository(connection, case)
        assert repository.get(case.key) is None
        assert repository.list() == ()

    @pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
    def test_delete_removes_the_row(self, connection, case: RepoCase) -> None:
        repository = _repository(connection, case)
        repository.upsert(case.sample())
        assert repository.delete(case.key) is True
        assert repository.get(case.key) is None
        assert repository.list() == ()

    @pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
    def test_delete_missing_key_returns_false(
        self, connection, case: RepoCase
    ) -> None:
        repository = _repository(connection, case)
        assert repository.delete(case.key) is False

    @pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
    def test_list_is_ordered_by_key(self, connection, case: RepoCase) -> None:
        repository = _repository(connection, case)
        models = case.ordered()
        for model in reversed(models):  # reverse insertion proves sorting
            repository.upsert(model)
        listed = repository.list()
        assert [case.key_of(model) for model in listed] == sorted(
            case.key_of(model) for model in models
        )

    @pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
    def test_list_is_repeatable(self, connection, case: RepoCase) -> None:
        repository = _repository(connection, case)
        for model in case.ordered():
            repository.upsert(model)
        assert repository.list() == repository.list()





class TestUpsertSemantics:
    """``ON CONFLICT ... DO UPDATE`` - never ``INSERT OR REPLACE``."""

    def test_step_upsert_updates_in_place(self, connection) -> None:
        repository = SqliteStepRepository(connection)
        repository.upsert(make_step(state=StepState.PENDING, attempt=0))
        repository.upsert(
            make_step(state=StepState.VERIFIED, attempt=1, started_at=LATER)
        )
        stored = repository.get(2)
        assert stored is not None
        assert stored.state is StepState.VERIFIED
        assert stored.attempt == 1
        assert connection.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == 1

    def test_step_upsert_with_dependent_task_keeps_the_foreign_key_valid(
        self, connection
    ) -> None:
        steps = SqliteStepRepository(connection)
        tasks = SqliteTaskRepository(connection)
        steps.upsert(make_step(state=StepState.DISPATCHED))
        tasks.upsert(make_task())

        # ON CONFLICT DO UPDATE keeps the referenced step row alive.
        # INSERT OR REPLACE would delete it and violate
        # tasks.step_no -> steps.step_no.
        steps.upsert(make_step(state=StepState.VERIFIED))
        assert steps.get(2).state is StepState.VERIFIED
        assert tasks.get(TaskKey(step_no=2, attempt=1)) is not None

    def test_task_upsert_preserves_the_internal_row_identity(
        self, connection
    ) -> None:
        _seed_steps(connection, 2)
        repository = SqliteTaskRepository(connection)
        repository.upsert(make_task())
        id_before = connection.execute(
            "SELECT id FROM tasks WHERE step_no = 2 AND attempt = 1"
        ).fetchone()[0]

        repository.upsert(
            make_task(
                state=TaskState.REPORTED,
                report_status=ReportStatus.REVISE,
                instructions={"plan_first": False},
            )
        )
        id_after = connection.execute(
            "SELECT id FROM tasks WHERE step_no = 2 AND attempt = 1"
        ).fetchone()[0]

        assert id_before == id_after  # REPLACE would allocate a new id
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        stored = repository.get(TaskKey(step_no=2, attempt=1))
        assert stored is not None
        assert stored.state is TaskState.REPORTED
        assert stored.report_status is ReportStatus.REVISE
        assert stored.instructions == {"plan_first": False}

    def test_risk_upsert_updates_status_in_place(self, connection) -> None:
        repository = SqliteRiskRepository(connection)
        repository.upsert(make_risk(status=RiskStatus.OPEN, probability=0.4))
        repository.upsert(
            make_risk(status=RiskStatus.ACCEPTED, probability=0.9, owner="cto")
        )
        stored = repository.get("RISK-001")
        assert stored is not None
        assert stored.status is RiskStatus.ACCEPTED
        assert stored.probability == 0.9
        assert stored.owner == "cto"
        assert connection.execute("SELECT COUNT(*) FROM risks").fetchone()[0] == 1


class TestForeignKeys:
    def test_task_requires_an_existing_step(self, connection) -> None:
        repository = SqliteTaskRepository(connection)
        with pytest.raises(sqlite3.IntegrityError):
            repository.upsert(make_task(step_no=99))

    def test_task_can_be_stored_once_the_step_exists(self, connection) -> None:
        _seed_steps(connection, 99)
        repository = SqliteTaskRepository(connection)
        repository.upsert(make_task(step_no=99))
        assert repository.get(TaskKey(step_no=99, attempt=1)) is not None

    def test_deleting_a_referenced_step_is_rejected(self, connection) -> None:
        steps = SqliteStepRepository(connection)
        steps.upsert(make_step())
        SqliteTaskRepository(connection).upsert(make_task())
        with pytest.raises(sqlite3.IntegrityError):
            steps.delete(2)

    def test_step_attempt_uniqueness_per_step(self, connection) -> None:
        _seed_steps(connection, 2)
        repository = SqliteTaskRepository(connection)
        repository.upsert(make_task(attempt=1))
        repository.upsert(make_task(attempt=2))
        repository.upsert(make_task(attempt=3))
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 3
        assert [task.attempt for task in repository.list_for_step(2)] == [1, 2, 3]



class TestAdapterSpecificQueries:
    def test_list_by_state_filters_and_orders(self, connection) -> None:
        repository = SqliteStepRepository(connection)
        repository.upsert(
            make_step(step_no=3, state=StepState.VERIFIED, title="Step 3")
        )
        repository.upsert(make_step(step_no=1, state=StepState.READY, title="Step 1"))
        repository.upsert(
            make_step(step_no=2, state=StepState.VERIFIED, title="Step 2")
        )
        assert [step.step_no for step in repository.list_by_state(StepState.VERIFIED)] == [
            2,
            3,
        ]
        assert repository.list_by_state(StepState.PENDING) == ()

    def test_list_by_state_accepts_a_plain_string(self, connection) -> None:
        repository = SqliteStepRepository(connection)
        repository.upsert(make_step(state=StepState.BLOCKED))
        assert len(repository.list_by_state("BLOCKED")) == 1

    def test_list_for_step_orders_by_attempt(self, connection) -> None:
        _seed_steps(connection, 2, 3)
        repository = SqliteTaskRepository(connection)
        repository.upsert(make_task(step_no=3, attempt=1))
        repository.upsert(make_task(step_no=2, attempt=2))
        repository.upsert(make_task(step_no=2, attempt=1))
        assert [task.attempt for task in repository.list_for_step(2)] == [1, 2]
        assert repository.list_for_step(99) == ()

    def test_list_open_returns_only_open_risks(self, connection) -> None:
        repository = SqliteRiskRepository(connection)
        repository.upsert(make_risk(id="RISK-001", status=RiskStatus.OPEN))
        repository.upsert(make_risk(id="RISK-002", status=RiskStatus.CLOSED))
        repository.upsert(make_risk(id="RISK-003", status=RiskStatus.OPEN))
        assert [risk.id for risk in repository.list_open()] == [
            "RISK-001",
            "RISK-003",
        ]

    def test_list_by_status_filters_change_requests(self, connection) -> None:
        repository = SqliteArchitectureChangeRequestRepository(connection)
        repository.upsert(make_change_request(request_id="ACR-001"))
        repository.upsert(
            make_change_request(
                request_id="ACR-002",
                status=ACRStatus.REJECTED,
                approved_by=None,
                approved_at=None,
                applied_at=None,
            )
        )
        repository.upsert(
            make_change_request(
                request_id="ACR-003",
                status=ACRStatus.PROPOSED,
                approved_by=None,
                approved_at=None,
                applied_at=None,
            )
        )
        assert [
            request.request_id
            for request in repository.list_by_status(ACRStatus.APPLIED)
        ] == ["ACR-001"]
        assert [
            request.request_id
            for request in repository.list_by_status(ACRStatus.PROPOSED)
        ] == ["ACR-003"]
        assert [
            request.request_id
            for request in repository.list_by_status("REJECTED")
        ] == ["ACR-002"]
        assert repository.list_by_status("NOT-A-STATUS") == ()


class TestFieldFidelity:
    def test_task_json_mappings_round_trip(self, connection) -> None:
        _seed_steps(connection, 2)
        repository = SqliteTaskRepository(connection)
        task = make_task(
            instructions={"plan_first": True, "nested": {"a": 1, "b": [1, 2]}},
            report_schema={"status": "DONE|REVISE|BLOCKED|FAILED"},
        )
        repository.upsert(task)
        stored = repository.get(TaskKey(step_no=2, attempt=1))
        assert stored is not None
        assert stored.instructions == task.instructions
        assert stored.report_schema == task.report_schema

    def test_tuple_order_is_preserved(self, connection) -> None:
        repository = SqliteADRRepository(connection)
        repository.upsert(
            make_adr(
                consequences=("z", "a", "m"),
                related=("ADR-009", "ADR-003"),
            )
        )
        stored = repository.get("ADR-001")
        assert stored is not None
        assert stored.consequences == ("z", "a", "m")
        assert stored.related == ("ADR-009", "ADR-003")

    def test_empty_sequences_round_trip_as_empty_tuples(self, connection) -> None:
        repository = SqliteFindingRepository(connection)
        repository.upsert(make_finding(evidence=()))
        stored = repository.get("F-001")
        assert stored is not None
        assert stored.evidence == ()

    def test_decision_keeps_all_three_sequences(self, connection) -> None:
        repository = SqliteDecisionRepository(connection)
        repository.upsert(make_decision())
        stored = repository.get("D-001")
        assert stored is not None
        assert stored.rules_applied == ("RULE-DOMAIN-01",)
        assert stored.evidence_refs == ("F-001",)
        assert stored.perspectives == ("openai", "claude", "grok")

    def test_optional_datetimes_stay_none(self, connection) -> None:
        repository = SqliteRiskRepository(connection)
        repository.upsert(make_risk(updated_at=None))
        stored = repository.get("RISK-001")
        assert stored is not None
        assert stored.updated_at is None

    def test_datetimes_keep_timezone_information(self, connection) -> None:
        repository = SqliteRiskRepository(connection)
        repository.upsert(make_risk())
        stored = repository.get("RISK-001")
        assert stored is not None
        assert stored.created_at == NOW
        assert stored.created_at.tzinfo is not None

    def test_optional_step_numbers_stay_none(self, connection) -> None:
        repository = SqliteFindingRepository(connection)
        repository.upsert(make_finding(step_no=None))
        stored = repository.get("F-001")
        assert stored is not None
        assert stored.step_no is None

    def test_bool_fields_round_trip(self, connection) -> None:
        repository = SqliteProjectRepository(connection)
        repository.upsert(make_project(paused=False))
        assert repository.get(PROJECT_NAME).paused is False
        repository.upsert(make_project(paused=True))
        assert repository.get(PROJECT_NAME).paused is True

    def test_float_fields_round_trip(self, connection) -> None:
        repository = SqliteRiskRepository(connection)
        repository.upsert(make_risk(probability=0.125))
        stored = repository.get("RISK-001")
        assert stored is not None
        assert stored.probability == 0.125



class TestRestartRecovery:
    """Anything written before a restart must be readable after reopening."""

    @pytest.fixture
    def database_path(self, tmp_path: Path) -> Path:
        return tmp_path / "data" / "architecture_assistant.db"

    @staticmethod
    def _write_all(connection: sqlite3.Connection) -> dict[str, Any]:
        _seed_steps(connection, 2, 3)
        written: dict[str, Any] = {
            "project": make_project(),
            "step": make_step(),
            "task": make_task(),
            "architecture_version": make_architecture_version(),
            "adr": make_adr(),
            "risk": make_risk(),
            "finding": make_finding(),
            "decision": make_decision(),
        }
        SqliteProjectRepository(connection).upsert(written["project"])
        SqliteStepRepository(connection).upsert(written["step"])
        SqliteTaskRepository(connection).upsert(written["task"])
        SqliteArchitectureVersionRepository(connection).upsert(
            written["architecture_version"]
        )
        SqliteADRRepository(connection).upsert(written["adr"])
        SqliteRiskRepository(connection).upsert(written["risk"])
        SqliteFindingRepository(connection).upsert(written["finding"])
        SqliteDecisionRepository(connection).upsert(written["decision"])
        return written

    def test_all_aggregates_survive_a_restart(self, database_path: Path) -> None:
        first = open_database(database_path)
        try:
            written = self._write_all(first)
            migrations_before = applied_versions(first)
        finally:
            close_database(first)

        second = open_database(database_path)
        try:
            assert applied_versions(second) == migrations_before == (
                EXPECTED_MIGRATION_VERSIONS
            )
            assert SqliteProjectRepository(second).get(PROJECT_NAME) == written[
                "project"
            ]
            assert SqliteStepRepository(second).get(2) == written["step"]
            assert SqliteTaskRepository(second).get(
                TaskKey(step_no=2, attempt=1)
            ) == written["task"]
            assert SqliteArchitectureVersionRepository(second).get(
                "1.0"
            ) == written["architecture_version"]
            assert SqliteADRRepository(second).get("ADR-001") == written["adr"]
            assert SqliteRiskRepository(second).get("RISK-001") == written["risk"]
            assert SqliteFindingRepository(second).get("F-001") == written["finding"]
            assert SqliteDecisionRepository(second).get("D-001") == written[
                "decision"
            ]
        finally:
            close_database(second)

    def test_restart_does_not_duplicate_rows(self, database_path: Path) -> None:
        first = open_database(database_path)
        try:
            self._write_all(first)
        finally:
            close_database(first)

        second = open_database(database_path)
        try:
            assert second.execute("SELECT COUNT(*) FROM project").fetchone()[0] == 1
            assert second.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
            assert (
                second.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
                == len(MIGRATIONS)
            )
        finally:
            close_database(second)

    def test_upsert_after_restart_updates_in_place(self, database_path: Path) -> None:
        first = open_database(database_path)
        try:
            self._write_all(first)
            id_before = first.execute(
                "SELECT id FROM tasks WHERE step_no = 2 AND attempt = 1"
            ).fetchone()[0]
        finally:
            close_database(first)

        second = open_database(database_path)
        try:
            SqliteTaskRepository(second).upsert(
                make_task(state=TaskState.REPORTED, report_status=ReportStatus.DONE)
            )
            id_after = second.execute(
                "SELECT id FROM tasks WHERE step_no = 2 AND attempt = 1"
            ).fetchone()[0]
            assert id_after == id_before
            assert second.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        finally:
            close_database(second)

    def test_default_database_path_targets_the_data_directory(self) -> None:
        assert DEFAULT_DATABASE_PATH.parent.name == "data"
        assert DEFAULT_DATABASE_PATH.name == "architecture_assistant.db"

