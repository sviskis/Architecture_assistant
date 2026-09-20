"""Tests for the Risk Manager: register, field updates, lifecycle, audit, atomicity.

Like the ADR manager, every call must commit the risk write and exactly one audit
entry together, or neither.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

from architecture_assistant.application import (
    RISK_TRANSITIONS,
    InvalidRiskStatusTransitionError,
    RiskManager,
    RiskManagerError,
    RiskNotFoundError,
)
from architecture_assistant.domain.audit import (
    AuditAction,
    AuditEntityType,
    AuditEntry,
)
from architecture_assistant.domain.enums import RiskStatus, Severity
from architecture_assistant.domain.models import Risk
from architecture_assistant.infrastructure import (
    SqliteAuditRepository,
    SqliteRiskRepository,
    SqliteTransactionPort,
    close_database,
    open_database,
)

NOW = datetime(2026, 9, 20, 20, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 9, 20, 21, 0, tzinfo=timezone.utc)


def fixed_clock() -> datetime:
    """Deterministic clock."""
    return NOW


class AdjustableClock:
    """Clock whose value can be moved between calls."""

    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


@pytest.fixture
def connection():
    conn = open_database(":memory:")
    yield conn
    close_database(conn)


def make_manager(
    connection,
    *,
    risks: Any = None,
    audit: Any = None,
    clock: Any = fixed_clock,
) -> RiskManager:
    """Wire a RiskManager over real SQLite adapters (or injected fakes)."""
    repository = risks if risks is not None else SqliteRiskRepository(connection)
    audit_repository = (
        audit if audit is not None else SqliteAuditRepository(connection)
    )
    return RiskManager(
        repository,
        audit_repository,
        SqliteTransactionPort(connection),
        clock=clock,
    )


class FailingAuditRepository:
    """Real audit adapter that can be armed to raise on ``append``."""

    def __init__(self, inner: SqliteAuditRepository) -> None:
        self._inner = inner
        self.fail = False

    def append(self, entry: AuditEntry) -> None:
        if self.fail:
            raise RuntimeError("audit append failed")
        self._inner.append(entry)

    def list(self) -> tuple[AuditEntry, ...]:
        return self._inner.list()

    def list_for_entity(self, entity_type, entity_id):
        return self._inner.list_for_entity(entity_type, entity_id)


class FailingRiskRepository:
    """Real risk adapter that can be armed to raise on ``upsert``."""

    def __init__(self, inner: SqliteRiskRepository) -> None:
        self._inner = inner
        self.fail_upsert = False

    def upsert(self, risk: Risk) -> None:
        if self.fail_upsert:
            raise RuntimeError("risk upsert failed")
        self._inner.upsert(risk)

    def get(self, risk_id: str) -> Optional[Risk]:
        return self._inner.get(risk_id)

    def list(self) -> tuple[Risk, ...]:
        return self._inner.list()

    def delete(self, risk_id: str) -> bool:
        return self._inner.delete(risk_id)

    def list_open(self) -> tuple[Risk, ...]:
        return self._inner.list_open()


class TestRegister:
    def test_registers_an_open_risk_with_all_fields(self, connection) -> None:
        manager = make_manager(connection)
        risk = manager.register(
            "RISK-001",
            "Architecture drift during long builds",
            severity=Severity.HIGH,
            probability=0.25,
            impact=Severity.CRITICAL,
            owner="architect",
            mitigation="deterministic validator",
        )
        assert risk.status is RiskStatus.OPEN
        assert risk.severity is Severity.HIGH
        assert risk.probability == 0.25
        assert risk.impact is Severity.CRITICAL
        assert risk.owner == "architect"
        assert risk.mitigation == "deterministic validator"
        assert risk.created_at == NOW
        assert risk.updated_at == NOW
        assert SqliteRiskRepository(connection).get("RISK-001") == risk

    def test_defaults(self, connection) -> None:
        risk = make_manager(connection).register("RISK-001", "d")
        assert risk.severity is Severity.MEDIUM
        assert risk.impact is Severity.MEDIUM
        assert risk.probability == 0.5
        assert risk.owner == ""
        assert risk.mitigation == ""

    def test_duplicate_id_is_rejected(self, connection) -> None:
        manager = make_manager(connection)
        manager.register("RISK-001", "d")
        with pytest.raises(RiskManagerError, match="already exists"):
            manager.register("RISK-001", "d")

    def test_invalid_probability_is_rejected_and_nothing_is_written(
        self, connection
    ) -> None:
        manager = make_manager(connection)
        with pytest.raises(ValueError, match="probability must be between"):
            manager.register("RISK-001", "d", probability=1.5)
        assert SqliteRiskRepository(connection).get("RISK-001") is None


class TestUpdate:
    def test_updates_fields_and_keeps_the_status(self, connection) -> None:
        manager = make_manager(connection)
        manager.register("RISK-001", "d")
        updated = manager.update(
            "RISK-001",
            severity=Severity.CRITICAL,
            probability=0.9,
            impact=Severity.HIGH,
            owner="cto",
            mitigation="accepted tolerance",
        )
        assert updated.status is RiskStatus.OPEN
        assert updated.severity is Severity.CRITICAL
        assert updated.probability == 0.9
        assert updated.impact is Severity.HIGH
        assert updated.owner == "cto"
        assert SqliteRiskRepository(connection).get("RISK-001") == updated

    def test_update_requires_a_change(self, connection) -> None:
        manager = make_manager(connection)
        manager.register("RISK-001", "d")
        with pytest.raises(RiskManagerError, match="at least one changed field"):
            manager.update("RISK-001")

    def test_update_missing_risk_raises(self, connection) -> None:
        manager = make_manager(connection)
        with pytest.raises(RiskNotFoundError):
            manager.update("RISK-404", owner="x")

    def test_invalid_probability_leaves_the_risk_untouched(self, connection) -> None:
        manager = make_manager(connection)
        manager.register("RISK-001", "d")
        with pytest.raises(ValueError, match="probability"):
            manager.update("RISK-001", probability=2.0)
        assert SqliteRiskRepository(connection).get("RISK-001").probability == 0.5



class TestStatusTransitions:
    def test_transition_table_is_exact(self) -> None:
        assert RISK_TRANSITIONS[RiskStatus.OPEN] == frozenset(
            {RiskStatus.MITIGATED, RiskStatus.ACCEPTED, RiskStatus.CLOSED}
        )
        assert RISK_TRANSITIONS[RiskStatus.MITIGATED] == frozenset(
            {RiskStatus.CLOSED}
        )
        assert RISK_TRANSITIONS[RiskStatus.ACCEPTED] == frozenset(
            {RiskStatus.CLOSED}
        )
        assert RISK_TRANSITIONS[RiskStatus.CLOSED] == frozenset(
            {RiskStatus.OPEN}
        )

    def test_mitigate(self, connection) -> None:
        manager = make_manager(connection)
        manager.register("RISK-001", "d")
        assert manager.mitigate("RISK-001").status is RiskStatus.MITIGATED

    def test_accept(self, connection) -> None:
        manager = make_manager(connection)
        manager.register("RISK-001", "d")
        assert manager.accept("RISK-001").status is RiskStatus.ACCEPTED

    def test_close_from_open(self, connection) -> None:
        manager = make_manager(connection)
        manager.register("RISK-001", "d")
        assert manager.close("RISK-001").status is RiskStatus.CLOSED

    def test_close_from_mitigated(self, connection) -> None:
        manager = make_manager(connection)
        manager.register("RISK-001", "d")
        manager.mitigate("RISK-001")
        assert manager.close("RISK-001").status is RiskStatus.CLOSED

    def test_close_from_accepted(self, connection) -> None:
        manager = make_manager(connection)
        manager.register("RISK-001", "d")
        manager.accept("RISK-001")
        assert manager.close("RISK-001").status is RiskStatus.CLOSED

    def test_reopen(self, connection) -> None:
        manager = make_manager(connection)
        manager.register("RISK-001", "d")
        manager.close("RISK-001")
        assert manager.reopen("RISK-001").status is RiskStatus.OPEN

    def test_mitigate_from_closed_is_rejected(self, connection) -> None:
        manager = make_manager(connection)
        manager.register("RISK-001", "d")
        manager.close("RISK-001")
        with pytest.raises(InvalidRiskStatusTransitionError, match="CLOSED"):
            manager.mitigate("RISK-001")

    def test_accept_from_mitigated_is_rejected(self, connection) -> None:
        manager = make_manager(connection)
        manager.register("RISK-001", "d")
        manager.mitigate("RISK-001")
        with pytest.raises(InvalidRiskStatusTransitionError):
            manager.accept("RISK-001")

    def test_missing_risk_raises(self, connection) -> None:
        manager = make_manager(connection)
        for operation in (
            manager.mitigate,
            manager.accept,
            manager.close,
            manager.reopen,
        ):
            with pytest.raises(RiskNotFoundError):
                operation("RISK-404")

    def test_list_open_reflects_the_lifecycle(self, connection) -> None:
        manager = make_manager(connection)
        manager.register("RISK-001", "a")
        manager.register("RISK-002", "b")
        manager.mitigate("RISK-002")
        open_ids = [
            risk.id for risk in SqliteRiskRepository(connection).list_open()
        ]
        assert open_ids == ["RISK-001"]



class TestAuditTrail:
    def test_register_writes_exactly_one_entry(self, connection) -> None:
        manager = make_manager(connection)
        manager.register(
            "RISK-001",
            "d",
            severity=Severity.HIGH,
            probability=0.25,
            impact=Severity.CRITICAL,
            owner="architect",
        )
        entries = SqliteAuditRepository(connection).list()
        assert len(entries) == 1
        entry = entries[0]
        assert entry.entity_type is AuditEntityType.RISK
        assert entry.entity_id == "RISK-001"
        assert entry.action is AuditAction.CREATE
        assert entry.detail == {
            "id": "RISK-001",
            "severity": "HIGH",
            "probability": 0.25,
            "impact": "CRITICAL",
            "owner": "architect",
            "status": "OPEN",
        }
        assert entry.created_at == NOW

    def test_update_writes_one_entry_with_changed_fields(self, connection) -> None:
        manager = make_manager(connection)
        manager.register("RISK-001", "d")
        manager.update("RISK-001", owner="cto", mitigation="m")
        entries = SqliteAuditRepository(connection).list()
        assert [entry.action for entry in entries] == [
            AuditAction.CREATE,
            AuditAction.UPDATE,
        ]
        assert entries[-1].detail == {
            "risk_id": "RISK-001",
            "changed_fields": ["mitigation", "owner"],
        }

    @pytest.mark.parametrize(
        "operation,action,status_from,status_to",
        [
            ("mitigate", AuditAction.MITIGATE, "OPEN", "MITIGATED"),
            ("accept", AuditAction.ACCEPT, "OPEN", "ACCEPTED"),
            ("close", AuditAction.CLOSE, "OPEN", "CLOSED"),
        ],
        ids=["mitigate", "accept", "close"],
    )
    def test_status_operations_write_one_entry(
        self, connection, operation, action, status_from, status_to
    ) -> None:
        manager = make_manager(connection)
        manager.register("RISK-001", "d")
        getattr(manager, operation)("RISK-001")

        entries = SqliteAuditRepository(connection).list()
        assert [entry.action for entry in entries] == [AuditAction.CREATE, action]
        assert entries[-1].detail == {
            "risk_id": "RISK-001",
            "from": status_from,
            "to": status_to,
        }

    def test_reopen_writes_one_entry(self, connection) -> None:
        manager = make_manager(connection)
        manager.register("RISK-001", "d")
        manager.close("RISK-001")
        manager.reopen("RISK-001")
        entries = SqliteAuditRepository(connection).list()
        assert entries[-1].action is AuditAction.REOPEN
        assert entries[-1].detail == {
            "risk_id": "RISK-001",
            "from": "CLOSED",
            "to": "OPEN",
        }

    def test_every_operation_writes_exactly_one_entry(self, connection) -> None:
        manager = make_manager(connection)
        audit = SqliteAuditRepository(connection)

        manager.register("RISK-001", "d")
        assert len(audit.list()) == 1
        manager.update("RISK-001", owner="o")
        assert len(audit.list()) == 2
        manager.mitigate("RISK-001")
        assert len(audit.list()) == 3
        manager.close("RISK-001")
        assert len(audit.list()) == 4
        manager.reopen("RISK-001")
        assert len(audit.list()) == 5

    def test_audit_detail_is_structured_and_json_serializable(
        self, connection
    ) -> None:
        manager = make_manager(connection)
        manager.register("RISK-001", "d")
        entry = SqliteAuditRepository(connection).list()[0]
        assert isinstance(entry.detail, dict)
        assert json.loads(entry.detail_json()) == dict(entry.detail)

    def test_list_for_entity_filters_by_entity_type(self, connection) -> None:
        manager = make_manager(connection)
        manager.register("RISK-001", "a")
        manager.register("RISK-002", "b")

        audit = SqliteAuditRepository(connection)
        assert [
            entry.entity_id
            for entry in audit.list_for_entity(AuditEntityType.RISK, "RISK-001")
        ] == ["RISK-001"]
        assert audit.list_for_entity(AuditEntityType.ADR, "RISK-001") == ()



class TestAtomicity:
    """A manager call commits risk state + one audit entry, or neither."""

    def test_success_persists_both_state_and_audit(self, connection) -> None:
        manager = make_manager(connection)
        manager.register("RISK-001", "d")

        assert SqliteRiskRepository(connection).get("RISK-001") is not None
        assert len(SqliteAuditRepository(connection).list()) == 1

    def test_audit_failure_rolls_back_the_risk_write(self, connection) -> None:
        audit = FailingAuditRepository(SqliteAuditRepository(connection))
        manager = make_manager(connection, audit=audit)
        manager.register("RISK-001", "d")

        audit.fail = True
        with pytest.raises(RuntimeError, match="audit append failed"):
            manager.mitigate("RISK-001")

        stored = SqliteRiskRepository(connection).get("RISK-001")
        assert stored is not None
        assert stored.status is RiskStatus.OPEN
        assert len(audit.list()) == 1

    def test_domain_write_failure_skips_the_audit_entry(self, connection) -> None:
        repository = FailingRiskRepository(SqliteRiskRepository(connection))
        manager = make_manager(connection, risks=repository)
        manager.register("RISK-001", "d")

        repository.fail_upsert = True
        with pytest.raises(RuntimeError, match="risk upsert failed"):
            manager.mitigate("RISK-001")

        stored = SqliteRiskRepository(connection).get("RISK-001")
        assert stored is not None
        assert stored.status is RiskStatus.OPEN
        assert len(SqliteAuditRepository(connection).list()) == 1

    def test_rollback_leaves_neither_state_nor_audit(self, connection) -> None:
        audit = FailingAuditRepository(SqliteAuditRepository(connection))
        manager = make_manager(connection, audit=audit)

        audit.fail = True
        with pytest.raises(RuntimeError, match="audit append failed"):
            manager.register("RISK-001", "d")

        assert SqliteRiskRepository(connection).get("RISK-001") is None
        assert audit.list() == ()

    def test_connection_remains_usable_after_a_rollback(self, connection) -> None:
        audit = FailingAuditRepository(SqliteAuditRepository(connection))
        manager = make_manager(connection, audit=audit)

        audit.fail = True
        with pytest.raises(RuntimeError):
            manager.register("RISK-001", "d")

        audit.fail = False
        manager.register("RISK-001", "d")

        assert SqliteRiskRepository(connection).get("RISK-001") is not None
        assert len(audit.list()) == 1

    def test_reconnect_after_commit_sees_state_and_audit(self, tmp_path) -> None:
        path = tmp_path / "data" / "architecture_assistant.db"

        first = open_database(path)
        try:
            manager = make_manager(first)
            manager.register("RISK-001", "d")
            manager.mitigate("RISK-001")
        finally:
            close_database(first)

        second = open_database(path)
        try:
            stored = SqliteRiskRepository(second).get("RISK-001")
            assert stored is not None
            assert stored.status is RiskStatus.MITIGATED
            actions = [
                entry.action for entry in SqliteAuditRepository(second).list()
            ]
            assert actions == [AuditAction.CREATE, AuditAction.MITIGATE]
        finally:
            close_database(second)


class TestClockInjection:
    def test_clock_is_required(self, connection) -> None:
        with pytest.raises(TypeError):
            RiskManager(  # type: ignore[call-arg]
                SqliteRiskRepository(connection),
                SqliteAuditRepository(connection),
                SqliteTransactionPort(connection),
            )

    def test_non_callable_clock_is_rejected(self, connection) -> None:
        with pytest.raises(ValueError, match="clock must be a callable"):
            RiskManager(
                SqliteRiskRepository(connection),
                SqliteAuditRepository(connection),
                SqliteTransactionPort(connection),
                clock="not-a-clock",  # type: ignore[arg-type]
            )

    def test_timestamps_come_from_the_injected_clock(self, connection) -> None:
        clock = AdjustableClock()
        manager = make_manager(connection, clock=clock)
        manager.register("RISK-001", "d")
        clock.value = LATER
        manager.mitigate("RISK-001")

        entries = SqliteAuditRepository(connection).list()
        assert [entry.created_at for entry in entries] == [NOW, LATER]
        assert SqliteRiskRepository(connection).get("RISK-001").updated_at == LATER

    def test_module_does_not_read_the_system_time(self) -> None:
        import architecture_assistant.application.risk_manager as module

        source = Path(module.__file__ or "").read_text(encoding="utf-8")
        assert "datetime.now(" not in source
        assert "utc_now(" not in source

