"""Tests for the StoragePort facade and its SQLite implementation.

Verifies that the facade is a pure aggregation boundary: ten typed accessors
plus the **reused** Step 5 transaction boundary, with no business logic and no
second transaction semantics.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from architecture_assistant.domain.audit import (
    AuditAction,
    AuditEntityType,
    AuditEntry,
)
from architecture_assistant.domain.enums import (
    ACRStatus,
    Phase,
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
from architecture_assistant.infrastructure import (
    SqliteStorage,
    SqliteTransactionPort,
    close_database,
    open_database,
)
from architecture_assistant.ports import (
    ADRRepository,
    ArchitectureChangeRequestRepository,
    ArchitectureVersionRepository,
    AuditRepository,
    DecisionRepository,
    FindingRepository,
    ProjectRepository,
    RiskRepository,
    StepRepository,
    StoragePort,
    TaskRepository,
    TransactionPort,
)

NOW = datetime(2026, 9, 20, 20, 0, tzinfo=timezone.utc)

ACCESSORS = (
    "projects",
    "steps",
    "tasks",
    "architecture_versions",
    "change_requests",
    "adrs",
    "risks",
    "findings",
    "decisions",
    "audit",
)


@pytest.fixture
def connection():
    conn = open_database(":memory:")
    yield conn
    close_database(conn)


@pytest.fixture
def storage(connection) -> SqliteStorage:
    return SqliteStorage(connection)


def make_entry(entity_id: str = "X") -> AuditEntry:
    return AuditEntry(
        entity_type=AuditEntityType.ADR,
        entity_id=entity_id,
        action=AuditAction.CREATE,
        created_at=NOW,
    )


def make_step() -> Step:
    return Step(
        step_no=6,
        phase=Phase.PLUGINS,
        title="Hybrid Plugin Core + Ports",
        state=StepState.READY,
    )


class TestFacadeShape:
    @pytest.mark.parametrize("name", ACCESSORS)
    def test_exposes_every_repository_accessor(self, storage, name: str) -> None:
        assert hasattr(storage, name)
        assert getattr(storage, name) is not None

    def test_accessors_are_stable_shared_instances(self, storage) -> None:
        for name in ACCESSORS:
            assert getattr(storage, name) is getattr(storage, name)

    def test_every_accessor_satisfies_its_repository_port(self, storage) -> None:
        assert isinstance(storage.projects, ProjectRepository)
        assert isinstance(storage.steps, StepRepository)
        assert isinstance(storage.tasks, TaskRepository)
        assert isinstance(storage.architecture_versions, ArchitectureVersionRepository)
        assert isinstance(
            storage.change_requests, ArchitectureChangeRequestRepository
        )
        assert isinstance(storage.adrs, ADRRepository)
        assert isinstance(storage.risks, RiskRepository)
        assert isinstance(storage.findings, FindingRepository)
        assert isinstance(storage.decisions, DecisionRepository)
        assert isinstance(storage.audit, AuditRepository)

    def test_exposes_the_reused_step_five_transaction_boundary(
        self, storage, connection
    ) -> None:
        assert isinstance(storage.transaction_port, SqliteTransactionPort)
        assert isinstance(storage.transaction_port, TransactionPort)
        assert storage.transaction_port.connection is connection
        assert storage.connection is connection

    def test_adds_no_business_logic(self, storage) -> None:
        # inspect the class, not the instance: sqlite3.Connection happens to be
        # callable, so instance-level callable() would be misleading
        methods = {
            name
            for name in dir(type(storage))
            if not name.startswith("_")
            and callable(getattr(type(storage), name))
        }
        assert methods == {"transaction"}

    def test_public_surface_is_only_accessors_and_transaction(self, storage) -> None:
        public = {
            name for name in dir(storage) if not name.startswith("_")
        }
        assert public == set(ACCESSORS) | {
            "connection",
            "transaction_port",
            "transaction",
        }

    def test_sqlite_storage_satisfies_the_storage_port(self, storage) -> None:
        # inheriting TransactionPort keeps exactly one transaction contract
        assert issubclass(StoragePort, TransactionPort)
        assert isinstance(storage, StoragePort)

    def test_storage_port_requires_all_accessors_and_transaction(self) -> None:
        required = set(getattr(StoragePort, "__protocol_attrs__", ()))
        assert required == set(ACCESSORS) | {"transaction"}

    def test_incomplete_facade_is_not_a_storage_port(self) -> None:
        assert isinstance(object(), StoragePort) is False


class TestTransactionBoundaryReuse:
    """The facade must delegate transactions - never reimplement them."""

    def test_standalone_write_commits(self, storage) -> None:
        storage.audit.append(make_entry("A"))
        assert len(storage.audit.list()) == 1

    def test_rollback_discards_writes_made_through_the_facade(
        self, storage
    ) -> None:
        with pytest.raises(RuntimeError, match="boom"):
            with storage.transaction():
                storage.audit.append(make_entry("A"))
                storage.steps.upsert(make_step())
                raise RuntimeError("boom")

        assert storage.audit.list() == ()
        assert storage.steps.get(6) is None

    def test_atomic_write_across_two_repositories(self, storage) -> None:
        step = make_step()
        with storage.transaction():
            storage.steps.upsert(step)
            storage.audit.append(make_entry("A"))

        assert storage.steps.get(6) == step
        assert len(storage.audit.list()) == 1

    def test_nested_transactions_join_the_outer_one(self, storage) -> None:
        with pytest.raises(RuntimeError, match="boom"):
            with storage.transaction():
                storage.audit.append(make_entry("A"))
                with storage.transaction():
                    storage.steps.upsert(make_step())
                raise RuntimeError("boom")

        assert storage.audit.list() == ()
        assert storage.steps.get(6) is None

    def test_transaction_flag_reflects_the_shared_boundary(self, storage) -> None:
        assert storage.transaction_port.in_transaction is False
        with storage.transaction():
            assert storage.transaction_port.in_transaction is True
        assert storage.transaction_port.in_transaction is False


class TestFacadeRoundTrips:
    def test_step_round_trip(self, storage) -> None:
        step = make_step()
        storage.steps.upsert(step)
        assert storage.steps.get(6) == step

    def test_project_and_architecture_round_trip(self, storage) -> None:
        project = Project(
            name="Architecture Lifecycle Assistant", plan_version="0.2"
        )
        storage.projects.upsert(project)
        assert storage.projects.get(project.name) == project

        version = ArchitectureVersion(version="1.0", baseline="Layered")
        storage.architecture_versions.upsert(version)
        assert storage.architecture_versions.get("1.0") == version

    def test_change_request_round_trip(self, storage) -> None:
        """Step 11 state: the request is persisted, not a transient DTO."""
        request = ArchitectureChangeRequest(
            request_id="ACR-001",
            title="Admit the composition root",
            rationale="the object graph needs a home",
            source_version="1.0",
            target_version="1.1",
            rule_ids=("unknown-layer",),
            adr_id="ADR-008",
            roadmap_impact=("step-009", "step-010"),
        )
        storage.change_requests.upsert(request)
        assert storage.change_requests.get("ACR-001") == request
        assert storage.change_requests.list() == (request,)

    def test_change_requests_are_filterable_by_lifecycle_status(
        self, storage
    ) -> None:
        proposed = ArchitectureChangeRequest(
            request_id="ACR-001",
            title="t",
            rationale="r",
            source_version="1.0",
            target_version="1.1",
            rule_ids=("r1",),
            adr_id="ADR-008",
        )
        rejected = replace(
            proposed, request_id="ACR-002", status=ACRStatus.REJECTED
        )
        storage.change_requests.upsert(proposed)
        storage.change_requests.upsert(rejected)

        assert storage.change_requests.list_by_status(ACRStatus.PROPOSED) == (
            proposed,
        )
        assert storage.change_requests.list_by_status(ACRStatus.REJECTED) == (
            rejected,
        )
        assert storage.change_requests.list_by_status(ACRStatus.APPLIED) == ()

    def test_adr_and_risk_round_trip(self, storage) -> None:
        adr = ADR(id="ADR-006", title="Plugin core is port-only")
        storage.adrs.upsert(adr)
        assert storage.adrs.get("ADR-006") == adr

        risk = Risk(
            id="RISK-006",
            description="Plugin sprawl",
            severity=Severity.HIGH,
            impact=Severity.HIGH,
            status=RiskStatus.OPEN,
        )
        storage.risks.upsert(risk)
        assert storage.risks.get("RISK-006") == risk

    def test_findings_and_decisions_round_trip(self, storage) -> None:
        finding = Finding(id="F-1", source="openai", claim="c")
        storage.findings.upsert(finding)
        assert storage.findings.get("F-1") == finding

        decision = Decision(id="D-1", decision="go")
        storage.decisions.upsert(decision)
        assert storage.decisions.get("D-1") == decision

    def test_task_requires_its_step(self, storage) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            storage.tasks.upsert(
                Task(step_no=6, phase=Phase.PLUGINS, title="dispatch")
            )

    def test_audit_is_append_only_through_the_facade(self, storage) -> None:
        assert not hasattr(storage.audit, "update")
        assert not hasattr(storage.audit, "delete")
        assert not hasattr(storage.audit, "upsert")

    def test_facade_shares_one_connection_with_the_adapters(
        self, storage, connection
    ) -> None:
        storage.audit.append(make_entry("A"))
        rows = connection.execute(
            "SELECT COUNT(*) FROM audit_entries"
        ).fetchone()[0]
        assert rows == 1

