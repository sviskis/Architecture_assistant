"""Concrete SQLite adapters - one class per repository port.

Each adapter owns exactly one aggregate and one key type, which keeps the API
unambiguous (unlike a single catch-all repository). All adapters may share the
same :class:`sqlite3.Connection` and the private helpers below.

Writes use ``INSERT ... ON CONFLICT(<key>) DO UPDATE SET ...`` - never
``INSERT OR REPLACE``, which would silently delete-and-reinsert the row and
break foreign keys, row identity and audit/history relations.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Optional, Sequence

from ..domain.audit import AuditEntityType, AuditEntry
from ..domain.enums import ACRStatus, RiskStatus, StepState
from ..domain.models import (
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
from ..ports.repositories import TaskKey

__all__ = [
    "SqliteProjectRepository",
    "SqliteStepRepository",
    "SqliteTaskRepository",
    "SqliteArchitectureVersionRepository",
    "SqliteArchitectureChangeRequestRepository",
    "SqliteADRRepository",
    "SqliteRiskRepository",
    "SqliteFindingRepository",
    "SqliteDecisionRepository",
    "SqliteAuditRepository",
]


# ---------------------------------------------------------------------------
# shared private helpers
# ---------------------------------------------------------------------------

def _enum_value(value: Any) -> Any:
    """Return the plain value of an enum member, or the value unchanged."""
    return value.value if isinstance(value, Enum) else value


def _bool_int(value: bool) -> int:
    """SQLite has no bool type: store 0/1."""
    return 1 if value else 0


def _iso(value: Optional[datetime]) -> Optional[str]:
    return None if value is None else value.isoformat()


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _json_dump_seq(values: Sequence[str]) -> str:
    """Serialize an ordered sequence as a JSON array (order preserved)."""
    return json.dumps(list(values))


def _json_load_seq(value: Any) -> tuple:
    if not value:
        return ()
    return tuple(json.loads(str(value)))


def _json_dump_map(values: Mapping[str, Any]) -> str:
    """Serialize a mapping as a JSON object with sorted keys (deterministic)."""
    return json.dumps(dict(values), sort_keys=True)


def _json_load_map(value: Any) -> dict:
    if not value:
        return {}
    return dict(json.loads(str(value)))


class _SqliteRepository:
    """Shared SQLite plumbing for the concrete adapters.

    Holds the connection only; every adapter declares its own SQL and key type.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    @property
    def connection(self) -> sqlite3.Connection:
        """The shared SQLite connection."""
        return self._connection

    def _execute_write(self, sql: str, params: Sequence[Any]) -> sqlite3.Cursor:
        """Execute a write, committing only when no outer boundary owns it.

        When an explicit transaction is open (``isolation_level is None``, set by
        :class:`~architecture_assistant.infrastructure.sqlite.SqliteTransactionPort`)
        the write joins that transaction and the boundary commits or rolls it
        back. Otherwise the adapter commits its own write, so standalone
        repository usage keeps its existing behaviour.
        """
        cursor = self._connection.execute(sql, tuple(params))
        if self._connection.isolation_level is not None:
            self._connection.commit()
        return cursor

    def _upsert(self, sql: str, params: Sequence[Any]) -> None:
        self._execute_write(sql, params)

    def _delete(self, sql: str, params: Sequence[Any]) -> bool:
        return self._execute_write(sql, params).rowcount > 0

    def _fetch_one(self, sql: str, params: Sequence[Any], build) -> Optional[Any]:
        row = self._connection.execute(sql, tuple(params)).fetchone()
        return None if row is None else build(row)

    def _fetch_all(self, sql: str, params: Sequence[Any], build) -> tuple:
        rows = self._connection.execute(sql, tuple(params)).fetchall()
        return tuple(build(row) for row in rows)


# ---------------------------------------------------------------------------
# row -> domain model converters
# ---------------------------------------------------------------------------

def _row_to_project(row: sqlite3.Row) -> Project:
    return Project(
        name=row["name"],
        plan_version=row["plan_version"],
        plan_hash=row["plan_hash"],
        mode=row["mode"],
        paused=bool(row["paused"]),
        current_step_no_snapshot=row["current_step_no_snapshot"],
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
    )


def _row_to_step(row: sqlite3.Row) -> Step:
    return Step(
        step_no=row["step_no"],
        phase=row["phase"],
        title=row["title"],
        state=row["state"],
        description=row["description"],
        attempt=row["attempt"],
        max_attempts=row["max_attempts"],
        risk=row["risk"],
        requires_human=bool(row["requires_human"]),
        created_at=_parse_dt(row["created_at"]),
        started_at=_parse_dt(row["started_at"]),
        finished_at=_parse_dt(row["finished_at"]),
        verified_at=_parse_dt(row["verified_at"]),
        last_update_at=_parse_dt(row["last_update_at"]),
    )


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        step_no=row["step_no"],
        phase=row["phase"],
        title=row["title"],
        description=row["description"],
        risk=row["risk"],
        attempt=row["attempt"],
        max_attempts=row["max_attempts"],
        state=row["state"],
        context_file=row["context_file"],
        instructions=_json_load_map(row["instructions"]),
        report_schema=_json_load_map(row["report_schema"]),
        report_status=row["report_status"],
        created_at=_parse_dt(row["created_at"]),
    )


def _row_to_architecture_version(row: sqlite3.Row) -> ArchitectureVersion:
    return ArchitectureVersion(
        version=row["version"],
        baseline=row["baseline"],
        rules=_json_load_seq(row["rules"]),
        superseded_by=row["superseded_by"],
        is_current=bool(row["is_current"]),
        created_at=_parse_dt(row["created_at"]),
    )


def _row_to_architecture_change_request(
    row: sqlite3.Row,
) -> ArchitectureChangeRequest:
    return ArchitectureChangeRequest(
        request_id=row["request_id"],
        title=row["title"],
        rationale=row["rationale"],
        source_version=row["source_version"],
        target_version=row["target_version"],
        rule_ids=_json_load_seq(row["rule_ids"]),
        adr_id=row["adr_id"],
        roadmap_impact=_json_load_seq(row["roadmap_impact"]),
        status=row["status"],
        created_at=_parse_dt(row["created_at"]),
        approved_by=row["approved_by"],
        approved_at=_parse_dt(row["approved_at"]),
        applied_at=_parse_dt(row["applied_at"]),
    )


def _row_to_adr(row: sqlite3.Row) -> ADR:
    return ADR(
        id=row["id"],
        title=row["title"],
        status=row["status"],
        context=row["context"],
        decision=row["decision"],
        consequences=_json_load_seq(row["consequences"]),
        version=row["version"],
        superseded_by=row["superseded_by"],
        related=_json_load_seq(row["related"]),
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
    )


def _row_to_risk(row: sqlite3.Row) -> Risk:
    return Risk(
        id=row["id"],
        description=row["description"],
        severity=row["severity"],
        probability=row["probability"],
        impact=row["impact"],
        owner=row["owner"],
        mitigation=row["mitigation"],
        status=row["status"],
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
    )


def _row_to_finding(row: sqlite3.Row) -> Finding:
    return Finding(
        id=row["id"],
        source=row["source"],
        claim=row["claim"],
        evidence=_json_load_seq(row["evidence"]),
        confidence=row["confidence"],
        severity=row["severity"],
        step_no=row["step_no"],
        created_at=_parse_dt(row["created_at"]),
    )


def _row_to_decision(row: sqlite3.Row) -> Decision:
    return Decision(
        id=row["id"],
        status=row["status"],
        decision=row["decision"],
        rationale=row["rationale"],
        rules_applied=_json_load_seq(row["rules_applied"]),
        evidence_refs=_json_load_seq(row["evidence_refs"]),
        perspectives=_json_load_seq(row["perspectives"]),
        step_no=row["step_no"],
        created_at=_parse_dt(row["created_at"]),
    )



# ---------------------------------------------------------------------------
# concrete adapters
# ---------------------------------------------------------------------------

class SqliteProjectRepository(_SqliteRepository):
    """SQLite adapter for the Project aggregate (key: ``name``)."""

    _UPSERT = """
        INSERT INTO project (
            name, plan_version, plan_hash, mode, paused,
            current_step_no_snapshot, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            plan_version = excluded.plan_version,
            plan_hash = excluded.plan_hash,
            mode = excluded.mode,
            paused = excluded.paused,
            current_step_no_snapshot = excluded.current_step_no_snapshot,
            created_at = excluded.created_at,
            updated_at = excluded.updated_at
    """

    def upsert(self, project: Project) -> None:
        self._upsert(
            self._UPSERT,
            (
                project.name,
                project.plan_version,
                project.plan_hash,
                project.mode.value,
                _bool_int(project.paused),
                project.current_step_no_snapshot,
                _iso(project.created_at),
                _iso(project.updated_at),
            ),
        )

    def get(self, name: str) -> Optional[Project]:
        return self._fetch_one(
            "SELECT * FROM project WHERE name = ?", (name,), _row_to_project
        )

    def list(self) -> tuple[Project, ...]:
        return self._fetch_all(
            "SELECT * FROM project ORDER BY name ASC", (), _row_to_project
        )

    def delete(self, name: str) -> bool:
        return self._delete("DELETE FROM project WHERE name = ?", (name,))


class SqliteStepRepository(_SqliteRepository):
    """SQLite adapter for Steps (key: ``step_no``)."""

    _UPSERT = """
        INSERT INTO steps (
            step_no, phase, title, state, description, attempt, max_attempts,
            risk, requires_human, created_at, started_at, finished_at,
            verified_at, last_update_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(step_no) DO UPDATE SET
            phase = excluded.phase,
            title = excluded.title,
            state = excluded.state,
            description = excluded.description,
            attempt = excluded.attempt,
            max_attempts = excluded.max_attempts,
            risk = excluded.risk,
            requires_human = excluded.requires_human,
            created_at = excluded.created_at,
            started_at = excluded.started_at,
            finished_at = excluded.finished_at,
            verified_at = excluded.verified_at,
            last_update_at = excluded.last_update_at
    """

    def upsert(self, step: Step) -> None:
        self._upsert(
            self._UPSERT,
            (
                step.step_no,
                step.phase.value,
                step.title,
                step.state.value,
                step.description,
                step.attempt,
                step.max_attempts,
                step.risk.value,
                _bool_int(step.requires_human),
                _iso(step.created_at),
                _iso(step.started_at),
                _iso(step.finished_at),
                _iso(step.verified_at),
                _iso(step.last_update_at),
            ),
        )

    def get(self, step_no: int) -> Optional[Step]:
        return self._fetch_one(
            "SELECT * FROM steps WHERE step_no = ?", (step_no,), _row_to_step
        )

    def list(self) -> tuple[Step, ...]:
        return self._fetch_all(
            "SELECT * FROM steps ORDER BY step_no ASC", (), _row_to_step
        )

    def list_by_state(self, state: StepState) -> tuple[Step, ...]:
        return self._fetch_all(
            "SELECT * FROM steps WHERE state = ? ORDER BY step_no ASC",
            (_enum_value(state),),
            _row_to_step,
        )

    def delete(self, step_no: int) -> bool:
        return self._delete("DELETE FROM steps WHERE step_no = ?", (step_no,))



class SqliteTaskRepository(_SqliteRepository):
    """SQLite adapter for Cline dispatch tasks (key: ``(step_no, attempt)``).

    The table has an internal ``id INTEGER PRIMARY KEY AUTOINCREMENT``, but that
    is purely an infrastructure detail: the port key is :class:`TaskKey` and the
    ``UNIQUE(step_no, attempt)`` constraint is the persistence invariant.
    """

    _UPSERT = """
        INSERT INTO tasks (
            step_no, phase, title, state, description, risk, attempt,
            max_attempts, context_file, instructions, report_schema,
            report_status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(step_no, attempt) DO UPDATE SET
            phase = excluded.phase,
            title = excluded.title,
            state = excluded.state,
            description = excluded.description,
            risk = excluded.risk,
            max_attempts = excluded.max_attempts,
            context_file = excluded.context_file,
            instructions = excluded.instructions,
            report_schema = excluded.report_schema,
            report_status = excluded.report_status,
            created_at = excluded.created_at
    """

    def upsert(self, task: Task) -> None:
        self._upsert(
            self._UPSERT,
            (
                task.step_no,
                task.phase.value,
                task.title,
                task.state.value,
                task.description,
                task.risk.value,
                task.attempt,
                task.max_attempts,
                task.context_file,
                _json_dump_map(task.instructions),
                _json_dump_map(task.report_schema),
                _enum_value(task.report_status),
                _iso(task.created_at),
            ),
        )

    def get(self, key: TaskKey) -> Optional[Task]:
        return self._fetch_one(
            "SELECT * FROM tasks WHERE step_no = ? AND attempt = ?",
            (key.step_no, key.attempt),
            _row_to_task,
        )

    def list(self) -> tuple[Task, ...]:
        return self._fetch_all(
            "SELECT * FROM tasks ORDER BY step_no ASC, attempt ASC",
            (),
            _row_to_task,
        )

    def list_for_step(self, step_no: int) -> tuple[Task, ...]:
        return self._fetch_all(
            "SELECT * FROM tasks WHERE step_no = ? ORDER BY attempt ASC",
            (step_no,),
            _row_to_task,
        )

    def delete(self, key: TaskKey) -> bool:
        return self._delete(
            "DELETE FROM tasks WHERE step_no = ? AND attempt = ?",
            (key.step_no, key.attempt),
        )


class SqliteArchitectureVersionRepository(_SqliteRepository):
    """SQLite adapter for architecture baselines (key: ``version``)."""

    _UPSERT = """
        INSERT INTO architecture_versions (
            version, baseline, rules, superseded_by, is_current, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(version) DO UPDATE SET
            baseline = excluded.baseline,
            rules = excluded.rules,
            superseded_by = excluded.superseded_by,
            is_current = excluded.is_current,
            created_at = excluded.created_at
    """

    def upsert(self, version: ArchitectureVersion) -> None:
        self._upsert(
            self._UPSERT,
            (
                version.version,
                version.baseline,
                _json_dump_seq(version.rules),
                version.superseded_by,
                _bool_int(version.is_current),
                _iso(version.created_at),
            ),
        )

    def get(self, version: str) -> Optional[ArchitectureVersion]:
        return self._fetch_one(
            "SELECT * FROM architecture_versions WHERE version = ?",
            (version,),
            _row_to_architecture_version,
        )

    def list(self) -> tuple[ArchitectureVersion, ...]:
        return self._fetch_all(
            "SELECT * FROM architecture_versions ORDER BY version ASC",
            (),
            _row_to_architecture_version,
        )

    def delete(self, version: str) -> bool:
        return self._delete(
            "DELETE FROM architecture_versions WHERE version = ?", (version,)
        )



class SqliteArchitectureChangeRequestRepository(_SqliteRepository):
    """SQLite adapter for change requests (key: ``request_id``)."""

    _UPSERT = """
        INSERT INTO architecture_change_requests (
            request_id, title, rationale, source_version, target_version,
            rule_ids, roadmap_impact, status, adr_id, approved_by,
            approved_at, applied_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(request_id) DO UPDATE SET
            title = excluded.title,
            rationale = excluded.rationale,
            source_version = excluded.source_version,
            target_version = excluded.target_version,
            rule_ids = excluded.rule_ids,
            roadmap_impact = excluded.roadmap_impact,
            status = excluded.status,
            adr_id = excluded.adr_id,
            approved_by = excluded.approved_by,
            approved_at = excluded.approved_at,
            applied_at = excluded.applied_at,
            created_at = excluded.created_at
    """

    def upsert(self, request: ArchitectureChangeRequest) -> None:
        self._upsert(
            self._UPSERT,
            (
                request.request_id,
                request.title,
                request.rationale,
                request.source_version,
                request.target_version,
                _json_dump_seq(request.rule_ids),
                _json_dump_seq(request.roadmap_impact),
                _enum_value(request.status),
                request.adr_id,
                request.approved_by,
                _iso(request.approved_at),
                _iso(request.applied_at),
                _iso(request.created_at),
            ),
        )

    def get(
        self, request_id: str
    ) -> Optional[ArchitectureChangeRequest]:
        return self._fetch_one(
            "SELECT * FROM architecture_change_requests WHERE request_id = ?",
            (request_id,),
            _row_to_architecture_change_request,
        )

    def list(self) -> tuple[ArchitectureChangeRequest, ...]:
        return self._fetch_all(
            "SELECT * FROM architecture_change_requests "
            "ORDER BY created_at ASC, request_id ASC",
            (),
            _row_to_architecture_change_request,
        )

    def list_by_status(
        self, status: ACRStatus
    ) -> tuple[ArchitectureChangeRequest, ...]:
        """Requests in one lifecycle status, oldest first.

        Accepts an :class:`ACRStatus` or its plain string value, exactly like
        every other adapter query in this module.
        """
        return self._fetch_all(
            "SELECT * FROM architecture_change_requests "
            "WHERE status = ? ORDER BY created_at ASC, request_id ASC",
            (_enum_value(status),),
            _row_to_architecture_change_request,
        )

    def delete(self, request_id: str) -> bool:
        """Delete a request row.

        Present for contract consistency with the other repositories only: the
        architecture-evolution use-case never deletes, so a change request stays
        auditable forever.
        """
        return self._delete(
            "DELETE FROM architecture_change_requests WHERE request_id = ?",
            (request_id,),
        )


class SqliteADRRepository(_SqliteRepository):
    """SQLite adapter for Architecture Decision Records (key: ``id``)."""

    _UPSERT = """
        INSERT INTO adrs (
            id, title, status, context, decision, consequences, version,
            superseded_by, related, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title = excluded.title,
            status = excluded.status,
            context = excluded.context,
            decision = excluded.decision,
            consequences = excluded.consequences,
            version = excluded.version,
            superseded_by = excluded.superseded_by,
            related = excluded.related,
            created_at = excluded.created_at,
            updated_at = excluded.updated_at
    """

    def upsert(self, adr: ADR) -> None:
        self._upsert(
            self._UPSERT,
            (
                adr.id,
                adr.title,
                adr.status.value,
                adr.context,
                adr.decision,
                _json_dump_seq(adr.consequences),
                adr.version,
                adr.superseded_by,
                _json_dump_seq(adr.related),
                _iso(adr.created_at),
                _iso(adr.updated_at),
            ),
        )

    def get(self, adr_id: str) -> Optional[ADR]:
        return self._fetch_one(
            "SELECT * FROM adrs WHERE id = ?", (adr_id,), _row_to_adr
        )

    def list(self) -> tuple[ADR, ...]:
        return self._fetch_all("SELECT * FROM adrs ORDER BY id ASC", (), _row_to_adr)

    def delete(self, adr_id: str) -> bool:
        return self._delete("DELETE FROM adrs WHERE id = ?", (adr_id,))


class SqliteRiskRepository(_SqliteRepository):
    """SQLite adapter for the risk register (key: ``id``)."""

    _UPSERT = """
        INSERT INTO risks (
            id, description, severity, probability, impact, owner, mitigation,
            status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            description = excluded.description,
            severity = excluded.severity,
            probability = excluded.probability,
            impact = excluded.impact,
            owner = excluded.owner,
            mitigation = excluded.mitigation,
            status = excluded.status,
            created_at = excluded.created_at,
            updated_at = excluded.updated_at
    """

    def upsert(self, risk: Risk) -> None:
        self._upsert(
            self._UPSERT,
            (
                risk.id,
                risk.description,
                risk.severity.value,
                float(risk.probability),
                risk.impact.value,
                risk.owner,
                risk.mitigation,
                risk.status.value,
                _iso(risk.created_at),
                _iso(risk.updated_at),
            ),
        )

    def get(self, risk_id: str) -> Optional[Risk]:
        return self._fetch_one(
            "SELECT * FROM risks WHERE id = ?", (risk_id,), _row_to_risk
        )

    def list(self) -> tuple[Risk, ...]:
        return self._fetch_all("SELECT * FROM risks ORDER BY id ASC", (), _row_to_risk)

    def list_open(self) -> tuple[Risk, ...]:
        return self._fetch_all(
            "SELECT * FROM risks WHERE status = ? ORDER BY id ASC",
            (RiskStatus.OPEN.value,),
            _row_to_risk,
        )

    def delete(self, risk_id: str) -> bool:
        return self._delete("DELETE FROM risks WHERE id = ?", (risk_id,))



class SqliteFindingRepository(_SqliteRepository):
    """SQLite adapter for advisor findings (key: ``id``)."""

    _UPSERT = """
        INSERT INTO findings (
            id, source, claim, evidence, confidence, severity, step_no,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            source = excluded.source,
            claim = excluded.claim,
            evidence = excluded.evidence,
            confidence = excluded.confidence,
            severity = excluded.severity,
            step_no = excluded.step_no,
            created_at = excluded.created_at
    """

    def upsert(self, finding: Finding) -> None:
        self._upsert(
            self._UPSERT,
            (
                finding.id,
                finding.source,
                finding.claim,
                _json_dump_seq(finding.evidence),
                float(finding.confidence),
                finding.severity.value,
                finding.step_no,
                _iso(finding.created_at),
            ),
        )

    def get(self, finding_id: str) -> Optional[Finding]:
        return self._fetch_one(
            "SELECT * FROM findings WHERE id = ?", (finding_id,), _row_to_finding
        )

    def list(self) -> tuple[Finding, ...]:
        return self._fetch_all(
            "SELECT * FROM findings ORDER BY id ASC", (), _row_to_finding
        )

    def delete(self, finding_id: str) -> bool:
        return self._delete("DELETE FROM findings WHERE id = ?", (finding_id,))


class SqliteDecisionRepository(_SqliteRepository):
    """SQLite adapter for decision-engine outcomes (key: ``id``)."""

    _UPSERT = """
        INSERT INTO decisions (
            id, status, decision, rationale, rules_applied, evidence_refs,
            perspectives, step_no, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            status = excluded.status,
            decision = excluded.decision,
            rationale = excluded.rationale,
            rules_applied = excluded.rules_applied,
            evidence_refs = excluded.evidence_refs,
            perspectives = excluded.perspectives,
            step_no = excluded.step_no,
            created_at = excluded.created_at
    """

    def upsert(self, decision: Decision) -> None:
        self._upsert(
            self._UPSERT,
            (
                decision.id,
                decision.status.value,
                decision.decision,
                decision.rationale,
                _json_dump_seq(decision.rules_applied),
                _json_dump_seq(decision.evidence_refs),
                _json_dump_seq(decision.perspectives),
                decision.step_no,
                _iso(decision.created_at),
            ),
        )

    def get(self, decision_id: str) -> Optional[Decision]:
        return self._fetch_one(
            "SELECT * FROM decisions WHERE id = ?",
            (decision_id,),
            _row_to_decision,
        )

    def list(self) -> tuple[Decision, ...]:
        return self._fetch_all(
            "SELECT * FROM decisions ORDER BY id ASC", (), _row_to_decision
        )

    def delete(self, decision_id: str) -> bool:
        return self._delete("DELETE FROM decisions WHERE id = ?", (decision_id,))



def _row_to_audit_entry(row: sqlite3.Row) -> AuditEntry:
    return AuditEntry(
        entity_type=row["entity_type"],
        entity_id=row["entity_id"],
        action=row["action"],
        detail=_json_load_map(row["detail"]),
        created_at=_parse_dt(row["created_at"]),
    )


class SqliteAuditRepository(_SqliteRepository):
    """Append-only SQLite adapter for the audit trail.

    There is intentionally no update or delete: audit history is immutable.
    Ordering is by the internal autoincrement id, which is the append order.
    """

    _INSERT = """
        INSERT INTO audit_entries (
            entity_type, entity_id, action, detail, created_at
        ) VALUES (?, ?, ?, ?, ?)
    """

    def append(self, entry: AuditEntry) -> None:
        self._execute_write(
            self._INSERT,
            (
                entry.entity_type.value,
                entry.entity_id,
                entry.action.value,
                entry.detail_json(),
                _iso(entry.created_at),
            ),
        )

    def list(self) -> tuple[AuditEntry, ...]:
        return self._fetch_all(
            "SELECT * FROM audit_entries ORDER BY id ASC",
            (),
            _row_to_audit_entry,
        )

    def list_for_entity(
        self, entity_type: AuditEntityType, entity_id: str
    ) -> tuple[AuditEntry, ...]:
        return self._fetch_all(
            "SELECT * FROM audit_entries "
            "WHERE entity_type = ? AND entity_id = ? ORDER BY id ASC",
            (_enum_value(entity_type), entity_id),
            _row_to_audit_entry,
        )

