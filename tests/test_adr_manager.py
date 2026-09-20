"""Tests for the ADR Manager: versioning, lifecycle, audit trail, atomicity.

The atomicity tests are the important ones: a manager call must commit the ADR
write and exactly one audit entry together, or neither.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

from architecture_assistant.application import (
    ADR_TRANSITIONS,
    ADRManager,
    AdrManagerError,
    AdrNotFoundError,
    InvalidAdrStatusTransitionError,
    InvalidSupersessionError,
)
from architecture_assistant.domain.audit import (
    AuditAction,
    AuditEntityType,
    AuditEntry,
)
from architecture_assistant.domain.enums import ADRStatus
from architecture_assistant.domain.models import ADR
from architecture_assistant.infrastructure import (
    SqliteADRRepository,
    SqliteAuditRepository,
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
    adrs: Any = None,
    audit: Any = None,
    clock: Any = fixed_clock,
) -> ADRManager:
    """Wire an ADRManager over real SQLite adapters (or injected fakes)."""
    repository = adrs if adrs is not None else SqliteADRRepository(connection)
    audit_repository = (
        audit if audit is not None else SqliteAuditRepository(connection)
    )
    return ADRManager(
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


class FailingADRRepository:
    """Real ADR adapter that can be armed to raise on ``upsert``."""

    def __init__(self, inner: SqliteADRRepository) -> None:
        self._inner = inner
        self.fail_upsert = False

    def upsert(self, adr: ADR) -> None:
        if self.fail_upsert:
            raise RuntimeError("adr upsert failed")
        self._inner.upsert(adr)

    def get(self, adr_id: str) -> Optional[ADR]:
        return self._inner.get(adr_id)

    def list(self) -> tuple[ADR, ...]:
        return self._inner.list()

    def delete(self, adr_id: str) -> bool:
        return self._inner.delete(adr_id)


class TestPropose:
    def test_creates_version_one_in_proposed_status(self, connection) -> None:
        manager = make_manager(connection)
        adr = manager.propose(
            "ADR-001", "Use SQLite as source of truth", decision="SQLite is it"
        )
        assert adr.version == 1
        assert adr.status is ADRStatus.PROPOSED
        assert adr.created_at == NOW
        assert adr.updated_at == NOW
        assert SqliteADRRepository(connection).get("ADR-001") == adr

    def test_duplicate_id_is_rejected(self, connection) -> None:
        manager = make_manager(connection)
        manager.propose("ADR-001", "First")
        with pytest.raises(AdrManagerError, match="already exists"):
            manager.propose("ADR-001", "Second")

    def test_sequence_fields_are_frozen_to_tuples(self, connection) -> None:
        manager = make_manager(connection)
        adr = manager.propose(
            "ADR-001", "t", consequences=["a", "b"], related=["ADR-002"]
        )
        assert adr.consequences == ("a", "b")
        assert adr.related == ("ADR-002",)


class TestStatusTransitions:
    def test_transition_table_is_exact(self) -> None:
        assert ADR_TRANSITIONS[ADRStatus.PROPOSED] == frozenset(
            {ADRStatus.ACCEPTED, ADRStatus.REJECTED}
        )
        assert ADR_TRANSITIONS[ADRStatus.ACCEPTED] == frozenset(
            {ADRStatus.DEPRECATED, ADRStatus.SUPERSEDED}
        )
        assert ADR_TRANSITIONS[ADRStatus.DEPRECATED] == frozenset(
            {ADRStatus.SUPERSEDED}
        )
        assert ADR_TRANSITIONS[ADRStatus.REJECTED] == frozenset()
        assert ADR_TRANSITIONS[ADRStatus.SUPERSEDED] == frozenset()

    def test_accept(self, connection) -> None:
        manager = make_manager(connection)
        manager.propose("ADR-001", "t")
        accepted = manager.accept("ADR-001")
        assert accepted.status is ADRStatus.ACCEPTED
        assert (
            SqliteADRRepository(connection).get("ADR-001").status
            is ADRStatus.ACCEPTED
        )

    def test_reject(self, connection) -> None:
        manager = make_manager(connection)
        manager.propose("ADR-001", "t")
        assert manager.reject("ADR-001").status is ADRStatus.REJECTED

    def test_deprecate(self, connection) -> None:
        manager = make_manager(connection)
        manager.propose("ADR-001", "t")
        manager.accept("ADR-001")
        assert manager.deprecate("ADR-001").status is ADRStatus.DEPRECATED

    def test_missing_adr_raises_not_found(self, connection) -> None:
        manager = make_manager(connection)
        for operation in (
            manager.accept,
            manager.reject,
            manager.deprecate,
        ):
            with pytest.raises(AdrNotFoundError):
                operation("ADR-404")

    def test_deprecate_from_proposed_is_rejected(self, connection) -> None:
        manager = make_manager(connection)
        manager.propose("ADR-001", "t")
        with pytest.raises(InvalidAdrStatusTransitionError, match="PROPOSED"):
            manager.deprecate("ADR-001")

    def test_rejected_is_terminal(self, connection) -> None:
        manager = make_manager(connection)
        manager.propose("ADR-001", "t")
        manager.reject("ADR-001")
        with pytest.raises(InvalidAdrStatusTransitionError):
            manager.accept("ADR-001")
        with pytest.raises(InvalidAdrStatusTransitionError):
            manager.deprecate("ADR-001")

    def test_status_change_bumps_updated_at_only(self, connection) -> None:
        clock = AdjustableClock()
        manager = make_manager(connection, clock=clock)
        manager.propose("ADR-001", "t")
        clock.value = LATER
        accepted = manager.accept("ADR-001")
        assert accepted.created_at == NOW
        assert accepted.updated_at == LATER
        assert accepted.version == 1



class TestVersioning:
    def test_amend_increments_version(self, connection) -> None:
        manager = make_manager(connection)
        manager.propose("ADR-001", "t", decision="v1 decision")
        amended = manager.amend("ADR-001", decision="v2 decision")
        assert amended.version == 2
        assert amended.decision == "v2 decision"
        assert SqliteADRRepository(connection).get("ADR-001").version == 2

    def test_amend_keeps_the_status(self, connection) -> None:
        manager = make_manager(connection)
        manager.propose("ADR-001", "t")
        manager.accept("ADR-001")
        amended = manager.amend("ADR-001", context="more context")
        assert amended.status is ADRStatus.ACCEPTED
        assert amended.version == 2

    def test_amend_requires_a_change(self, connection) -> None:
        manager = make_manager(connection)
        manager.propose("ADR-001", "t")
        with pytest.raises(AdrManagerError, match="at least one changed field"):
            manager.amend("ADR-001")

    def test_several_amends_accumulate_versions(self, connection) -> None:
        manager = make_manager(connection)
        manager.propose("ADR-001", "t")
        assert manager.amend("ADR-001", decision="a").version == 2
        assert manager.amend("ADR-001", decision="b").version == 3

    def test_amend_missing_adr_raises(self, connection) -> None:
        manager = make_manager(connection)
        with pytest.raises(AdrNotFoundError):
            manager.amend("ADR-404", decision="x")


class TestSupersession:
    def test_supersede_links_both_sides(self, connection) -> None:
        manager = make_manager(connection)
        manager.propose("ADR-001", "old")
        manager.accept("ADR-001")
        manager.propose("ADR-002", "new")

        old, successor = manager.supersede("ADR-001", "ADR-002")

        assert old.status is ADRStatus.SUPERSEDED
        assert old.superseded_by == "ADR-002"
        assert successor.status is ADRStatus.ACCEPTED
        repository = SqliteADRRepository(connection)
        assert repository.get("ADR-001").superseded_by == "ADR-002"
        assert repository.get("ADR-002").status is ADRStatus.ACCEPTED

    def test_supersede_with_an_already_accepted_successor(self, connection) -> None:
        manager = make_manager(connection)
        manager.propose("ADR-001", "old")
        manager.accept("ADR-001")
        manager.propose("ADR-002", "new")
        manager.accept("ADR-002")

        old, successor = manager.supersede("ADR-001", "ADR-002")

        assert old.status is ADRStatus.SUPERSEDED
        assert successor.status is ADRStatus.ACCEPTED

    def test_self_supersession_is_rejected(self, connection) -> None:
        manager = make_manager(connection)
        manager.propose("ADR-001", "t")
        manager.accept("ADR-001")
        with pytest.raises(InvalidSupersessionError, match="itself"):
            manager.supersede("ADR-001", "ADR-001")

    def test_double_supersession_is_rejected(self, connection) -> None:
        manager = make_manager(connection)
        manager.propose("ADR-001", "old")
        manager.accept("ADR-001")
        manager.propose("ADR-002", "new")
        manager.supersede("ADR-001", "ADR-002")
        manager.propose("ADR-003", "newest")
        with pytest.raises(InvalidSupersessionError, match="already superseded"):
            manager.supersede("ADR-001", "ADR-003")

    def test_superseded_is_terminal(self, connection) -> None:
        manager = make_manager(connection)
        manager.propose("ADR-001", "old")
        manager.accept("ADR-001")
        manager.propose("ADR-002", "new")
        manager.supersede("ADR-001", "ADR-002")
        with pytest.raises(InvalidAdrStatusTransitionError):
            manager.deprecate("ADR-001")

    def test_rejected_successor_is_rejected(self, connection) -> None:
        manager = make_manager(connection)
        manager.propose("ADR-001", "old")
        manager.accept("ADR-001")
        manager.propose("ADR-002", "new")
        manager.reject("ADR-002")
        with pytest.raises(InvalidSupersessionError, match="PROPOSED or ACCEPTED"):
            manager.supersede("ADR-001", "ADR-002")

    def test_missing_successor_is_rejected(self, connection) -> None:
        manager = make_manager(connection)
        manager.propose("ADR-001", "old")
        manager.accept("ADR-001")
        with pytest.raises(AdrNotFoundError):
            manager.supersede("ADR-001", "ADR-404")



class TestAuditTrail:
    def test_propose_writes_exactly_one_entry(self, connection) -> None:
        manager = make_manager(connection)
        manager.propose("ADR-001", "Use SQLite")
        entries = SqliteAuditRepository(connection).list()
        assert len(entries) == 1
        entry = entries[0]
        assert entry.entity_type is AuditEntityType.ADR
        assert entry.entity_id == "ADR-001"
        assert entry.action is AuditAction.CREATE
        assert entry.detail["version"] == 1
        assert entry.detail["status"] == "PROPOSED"
        assert entry.created_at == NOW

    @pytest.mark.parametrize(
        "operation,action,status_from,status_to",
        [
            ("accept", AuditAction.ACCEPT, "PROPOSED", "ACCEPTED"),
            ("reject", AuditAction.REJECT, "PROPOSED", "REJECTED"),
        ],
        ids=["accept", "reject"],
    )
    def test_status_operations_write_one_entry(
        self, connection, operation, action, status_from, status_to
    ) -> None:
        manager = make_manager(connection)
        manager.propose("ADR-001", "t")
        getattr(manager, operation)("ADR-001")

        entries = SqliteAuditRepository(connection).list()
        assert [entry.action for entry in entries] == [AuditAction.CREATE, action]
        last = entries[-1]
        assert last.entity_id == "ADR-001"
        assert last.detail == {
            "adr_id": "ADR-001",
            "from": status_from,
            "to": status_to,
        }

    def test_deprecate_writes_one_entry(self, connection) -> None:
        manager = make_manager(connection)
        manager.propose("ADR-001", "t")
        manager.accept("ADR-001")
        manager.deprecate("ADR-001")

        entries = SqliteAuditRepository(connection).list()
        assert [entry.action for entry in entries] == [
            AuditAction.CREATE,
            AuditAction.ACCEPT,
            AuditAction.DEPRECATE,
        ]
        assert entries[-1].detail["from"] == "ACCEPTED"
        assert entries[-1].detail["to"] == "DEPRECATED"

    def test_amend_writes_one_entry_with_version_delta(self, connection) -> None:
        manager = make_manager(connection)
        manager.propose("ADR-001", "t")
        manager.amend("ADR-001", decision="changed")

        entries = SqliteAuditRepository(connection).list()
        assert len(entries) == 2
        last = entries[-1]
        assert last.action is AuditAction.AMEND
        assert last.detail == {
            "adr_id": "ADR-001",
            "previous_version": 1,
            "new_version": 2,
            "changed_fields": ["decision"],
        }

    def test_supersede_writes_exactly_one_entry_for_both_sides(
        self, connection
    ) -> None:
        manager = make_manager(connection)
        manager.propose("ADR-001", "old")
        manager.accept("ADR-001")
        manager.propose("ADR-002", "new")
        manager.supersede("ADR-001", "ADR-002")

        entries = SqliteAuditRepository(connection).list()
        # CREATE 001, ACCEPT 001, CREATE 002, SUPERSEDE (001) -> one supersede entry
        assert [entry.action for entry in entries] == [
            AuditAction.CREATE,
            AuditAction.ACCEPT,
            AuditAction.CREATE,
            AuditAction.SUPERSEDE,
        ]
        supersede = [e for e in entries if e.action is AuditAction.SUPERSEDE]
        assert len(supersede) == 1
        assert supersede[0].entity_id == "ADR-001"
        assert supersede[0].detail == {
            "old_id": "ADR-001",
            "successor_id": "ADR-002",
            "old_status": "ACCEPTED",
            "old_status_after": "SUPERSEDED",
            "successor_status": "ACCEPTED",
        }

    def test_audit_detail_is_structured_and_json_serializable(
        self, connection
    ) -> None:
        manager = make_manager(connection)
        manager.propose("ADR-001", "t")
        entry = SqliteAuditRepository(connection).list()[0]
        assert isinstance(entry.detail, dict)
        assert json.loads(entry.detail_json()) == dict(entry.detail)
        assert entry.detail_json() == entry.detail_json()

    def test_list_for_entity_filters_and_orders(self, connection) -> None:
        manager = make_manager(connection)
        manager.propose("ADR-001", "one")
        manager.propose("ADR-002", "two")
        manager.accept("ADR-001")

        audit = SqliteAuditRepository(connection)
        assert [entry.action for entry in audit.list_for_entity(
            AuditEntityType.ADR, "ADR-001"
        )] == [AuditAction.CREATE, AuditAction.ACCEPT]
        assert [entry.entity_id for entry in audit.list_for_entity(
            AuditEntityType.ADR, "ADR-002"
        )] == ["ADR-002"]
        assert audit.list_for_entity(AuditEntityType.RISK, "ADR-001") == ()



class TestAtomicity:
    """A manager call commits ADR state + one audit entry, or neither."""

    def test_success_persists_both_state_and_audit(self, connection) -> None:
        manager = make_manager(connection)
        manager.propose("ADR-001", "t")

        assert SqliteADRRepository(connection).get("ADR-001") is not None
        assert len(SqliteAuditRepository(connection).list()) == 1

    def test_audit_failure_rolls_back_the_adr_write(self, connection) -> None:
        audit = FailingAuditRepository(SqliteAuditRepository(connection))
        manager = make_manager(connection, audit=audit)
        manager.propose("ADR-001", "t")

        audit.fail = True
        with pytest.raises(RuntimeError, match="audit append failed"):
            manager.accept("ADR-001")

        # the ADR update was rolled back ...
        stored = SqliteADRRepository(connection).get("ADR-001")
        assert stored is not None
        assert stored.status is ADRStatus.PROPOSED
        # ... and no failed audit entry was recorded
        assert len(audit.list()) == 1

    def test_domain_write_failure_skips_the_audit_entry(self, connection) -> None:
        repository = FailingADRRepository(SqliteADRRepository(connection))
        manager = make_manager(connection, adrs=repository)
        manager.propose("ADR-001", "t")

        repository.fail_upsert = True
        with pytest.raises(RuntimeError, match="adr upsert failed"):
            manager.accept("ADR-001")

        stored = SqliteADRRepository(connection).get("ADR-001")
        assert stored is not None
        assert stored.status is ADRStatus.PROPOSED
        # only the successful CREATE entry exists
        assert len(SqliteAuditRepository(connection).list()) == 1

    def test_rollback_leaves_neither_state_nor_audit(self, connection) -> None:
        audit = FailingAuditRepository(SqliteAuditRepository(connection))
        manager = make_manager(connection, audit=audit)

        audit.fail = True
        with pytest.raises(RuntimeError, match="audit append failed"):
            manager.propose("ADR-001", "t")

        assert SqliteADRRepository(connection).get("ADR-001") is None
        assert audit.list() == ()

    def test_connection_remains_usable_after_a_rollback(self, connection) -> None:
        audit = FailingAuditRepository(SqliteAuditRepository(connection))
        manager = make_manager(connection, audit=audit)

        audit.fail = True
        with pytest.raises(RuntimeError):
            manager.propose("ADR-001", "t")

        audit.fail = False
        manager.propose("ADR-001", "t")

        assert SqliteADRRepository(connection).get("ADR-001") is not None
        assert len(audit.list()) == 1

    def test_reconnect_after_commit_sees_state_and_audit(self, tmp_path) -> None:
        path = tmp_path / "data" / "architecture_assistant.db"

        first = open_database(path)
        try:
            manager = make_manager(first)
            manager.propose("ADR-001", "t")
            manager.accept("ADR-001")
        finally:
            close_database(first)

        second = open_database(path)
        try:
            stored = SqliteADRRepository(second).get("ADR-001")
            assert stored is not None
            assert stored.status is ADRStatus.ACCEPTED
            actions = [entry.action for entry in SqliteAuditRepository(second).list()]
            assert actions == [AuditAction.CREATE, AuditAction.ACCEPT]
        finally:
            close_database(second)


class TestClockInjection:
    def test_clock_is_required(self, connection) -> None:
        with pytest.raises(TypeError):
            ADRManager(  # type: ignore[call-arg]
                SqliteADRRepository(connection),
                SqliteAuditRepository(connection),
                SqliteTransactionPort(connection),
            )

    def test_non_callable_clock_is_rejected(self, connection) -> None:
        with pytest.raises(ValueError, match="clock must be a callable"):
            ADRManager(
                SqliteADRRepository(connection),
                SqliteAuditRepository(connection),
                SqliteTransactionPort(connection),
                clock="not-a-clock",  # type: ignore[arg-type]
            )

    def test_timestamps_come_from_the_injected_clock(self, connection) -> None:
        clock = AdjustableClock()
        manager = make_manager(connection, clock=clock)
        manager.propose("ADR-001", "t")
        clock.value = LATER
        manager.accept("ADR-001")

        entries = SqliteAuditRepository(connection).list()
        assert [entry.created_at for entry in entries] == [NOW, LATER]
        assert SqliteADRRepository(connection).get("ADR-001").updated_at == LATER

    def test_module_does_not_read_the_system_time(self) -> None:
        import architecture_assistant.application.adr_manager as module

        source = Path(module.__file__ or "").read_text(encoding="utf-8")
        assert "datetime.now(" not in source
        assert "utc_now(" not in source

