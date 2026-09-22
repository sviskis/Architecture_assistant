"""Tests for the audit trail primitives and the transaction boundary port.

Covers ``AuditEntry`` validation/serialization, protocol conformance of the
append-only audit adapter, and the atomic commit/rollback semantics of the
SQLite transaction boundary (including nesting).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from architecture_assistant.domain.audit import (
    AuditAction,
    AuditEntityType,
    AuditEntry,
)
from architecture_assistant.infrastructure import (
    SqliteAuditRepository,
    SqliteTransactionPort,
    close_database,
    open_database,
)
from architecture_assistant.ports import AuditRepository, TransactionPort

NOW = datetime(2026, 9, 20, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def connection():
    conn = open_database(":memory:")
    yield conn
    close_database(conn)


def make_entry(entity_id: str = "ADR-001", **overrides) -> AuditEntry:
    data = dict(
        entity_type=AuditEntityType.ADR,
        entity_id=entity_id,
        action=AuditAction.CREATE,
        detail={"id": entity_id},
        created_at=NOW,
    )
    data.update(overrides)
    return AuditEntry(**data)


class TestAuditEntry:
    def test_coerces_string_enum_values(self) -> None:
        entry = AuditEntry(
            entity_type="ADR", entity_id="ADR-001", action="CREATE"
        )
        assert entry.entity_type is AuditEntityType.ADR
        assert entry.action is AuditAction.CREATE

    def test_empty_entity_id_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="entity_id must be a non-empty"):
            AuditEntry(
                entity_type=AuditEntityType.ADR,
                entity_id="   ",
                action=AuditAction.CREATE,
            )

    def test_unknown_action_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="action must be one of"):
            AuditEntry(
                entity_type=AuditEntityType.ADR,
                entity_id="X",
                action="NOPE",
            )

    def test_unknown_entity_type_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="entity_type must be one of"):
            AuditEntry(entity_type="NOPE", entity_id="X", action="CREATE")

    def test_non_json_serializable_detail_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="detail must be JSON-serializable"):
            AuditEntry(
                entity_type=AuditEntityType.ADR,
                entity_id="X",
                action=AuditAction.CREATE,
                detail={"bad": {1, 2}},
            )

    def test_detail_json_is_deterministic(self) -> None:
        entry = make_entry(detail={"b": 1, "a": 2})
        assert entry.detail_json() == '{"a": 2, "b": 1}'

    def test_default_detail_is_an_empty_mapping(self) -> None:
        entry = AuditEntry(
            entity_type=AuditEntityType.ADR,
            entity_id="X",
            action=AuditAction.CREATE,
        )
        assert dict(entry.detail) == {}

    def test_to_dict_from_dict_round_trip(self) -> None:
        entry = make_entry(detail={"k": "v"})
        assert AuditEntry.from_dict(entry.to_dict()) == entry

    def test_entity_types_are_adr_risk_step_and_acr(self) -> None:
        """``STEP`` was added with the Step 9 orchestrator, ``ACR`` with Step 11,
        ``PROJECT`` with the Step 20 human-override use-case, ``PLAN`` with the
        Step 25 plan loader, ``PROPOSAL`` with the Step 27 managed-project
        architecture proposal, ``SUPERVISION`` with the Step 28 advisory
        supervision use-case (durable supervision decisions only) and
        ``DELIBERATION`` with the Step 29 controlled architecture deliberation
        (lifecycle acts only, never a read).
        """
        assert {member.value for member in AuditEntityType} == {
            "ADR",
            "RISK",
            "STEP",
            "ACR",
            "PROJECT",
            "PLAN",
            "PROPOSAL",
            "SUPERVISION",
            "DELIBERATION",
        }

    def test_actions_are_canonical(self) -> None:
        """``PAUSE``/``RESUME``/``SET_MODE`` are the Step 20 project vocabulary.

        No step-level verb was added with Step 20: a step transition stays
        ``UPDATE`` (the Step entity's one transition action) and the human
        provenance is carried by ``detail``. ``IMPORT`` is the Step 25 plan
        verb and belongs to the ``PLAN`` entity alone.
        """
        assert {member.value for member in AuditAction} == {
            "CREATE",
            "ACCEPT",
            "REJECT",
            "DEPRECATE",
            "SUPERSEDE",
            "AMEND",
            "UPDATE",
            "MITIGATE",
            "CLOSE",
            "REOPEN",
            "PAUSE",
            "RESUME",
            "SET_MODE",
            "IMPORT",
        }


class TestProtocolConformance:
    def test_sqlite_transaction_port_satisfies_the_protocol(
        self, connection
    ) -> None:
        assert isinstance(SqliteTransactionPort(connection), TransactionPort)

    def test_audit_adapter_satisfies_the_port(self, connection) -> None:
        assert isinstance(SqliteAuditRepository(connection), AuditRepository)

    def test_audit_adapter_is_append_only(self, connection) -> None:
        repository = SqliteAuditRepository(connection)
        assert not hasattr(repository, "update")
        assert not hasattr(repository, "delete")


class TestTransactionBoundary:
    def test_commit_makes_writes_visible(self, connection) -> None:
        port = SqliteTransactionPort(connection)
        repository = SqliteAuditRepository(connection)
        with port.transaction():
            repository.append(make_entry("A"))
        assert len(repository.list()) == 1

    def test_rollback_discards_writes(self, connection) -> None:
        port = SqliteTransactionPort(connection)
        repository = SqliteAuditRepository(connection)
        with pytest.raises(RuntimeError, match="boom"):
            with port.transaction():
                repository.append(make_entry("A"))
                raise RuntimeError("boom")
        assert repository.list() == ()

    def test_nested_transactions_join_the_outer_one(self, connection) -> None:
        port = SqliteTransactionPort(connection)
        repository = SqliteAuditRepository(connection)
        with pytest.raises(RuntimeError, match="boom"):
            with port.transaction():
                repository.append(make_entry("A"))
                with port.transaction():  # nested: must NOT commit
                    repository.append(make_entry("B"))
                raise RuntimeError("boom")
        assert repository.list() == ()

    def test_in_transaction_flag(self, connection) -> None:
        port = SqliteTransactionPort(connection)
        assert port.in_transaction is False
        with port.transaction():
            assert port.in_transaction is True
        assert port.in_transaction is False

    def test_isolation_level_is_restored_after_the_block(
        self, connection
    ) -> None:
        port = SqliteTransactionPort(connection)
        before = connection.isolation_level
        with port.transaction():
            assert connection.isolation_level is None
        assert connection.isolation_level == before

    def test_standalone_writes_still_commit(self, connection) -> None:
        repository = SqliteAuditRepository(connection)
        repository.append(make_entry("A"))
        assert repository.list() != ()
        assert connection.isolation_level == ""

    def test_order_is_the_append_order(self, connection) -> None:
        repository = SqliteAuditRepository(connection)
        repository.append(make_entry("first"))
        repository.append(make_entry("second"))
        repository.append(make_entry("third"))
        assert [entry.entity_id for entry in repository.list()] == [
            "first",
            "second",
            "third",
        ]
