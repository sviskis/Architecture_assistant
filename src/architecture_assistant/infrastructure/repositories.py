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
from ..domain.enums import (
    ACRStatus,
    SUPERVISION_RECONCILE_STATUSES,
    DeliberationStage,
    DeliberationStatus,
    ProposalStatus,
    RiskStatus,
    StepState,
    SupervisorStatus,
)
from ..domain.models import (
    ADR,
    ArchitectureChangeRequest,
    ArchitectureProposal,
    ArchitectureVersion,
    Decision,
    DeliberationArtifact,
    DeliberationRun,
    Finding,
    Project,
    Risk,
    Step,
    SupervisionRecord,
    Task,
)
from ..ports.repositories import TaskKey

__all__ = [
    "SqliteProjectRepository",
    "SqliteStepRepository",
    "SqliteTaskRepository",
    "SqliteArchitectureVersionRepository",
    "SqliteArchitectureChangeRequestRepository",
    "SqliteArchitectureProposalRepository",
    "SqliteDeliberationRepository",
    "SqliteSupervisionRepository",
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


def _row_to_architecture_proposal(row: sqlite3.Row) -> ArchitectureProposal:
    return ArchitectureProposal(
        proposal_id=row["proposal_id"],
        project=row["project"],
        created_at=_parse_dt(row["created_at"]),
        requirement=row["requirement"],
        summary=row["summary"],
        source_review_id=row["source_review_id"],
        fingerprint=row["fingerprint"],
        modules=_json_load_seq(row["modules"]),
        data_flows=_json_load_seq(row["data_flows"]),
        external_dependencies=_json_load_seq(row["external_dependencies"]),
        architecture_rules=_json_load_seq(row["architecture_rules"]),
        proposed_rules=_json_load_seq(row["proposed_rules"]),
        risks=_json_load_seq(row["risks"]),
        adr_candidates=_json_load_seq(row["adr_candidates"]),
        implementation_phases=_json_load_seq(row["implementation_phases"]),
        unresolved_questions=_json_load_seq(row["unresolved_questions"]),
        rationale=row["rationale"],
        review_digest=_json_load_map(row["review_digest"]),
        architecture_version=row["architecture_version"],
        revision_no=row["revision_no"],
        revision_of=row["revision_of"],
        status=row["status"],
        decided_by=row["decided_by"],
        decided_at=_parse_dt(row["decided_at"]),
        decision_reason=row["decision_reason"],
        revision_feedback=row["revision_feedback"],
        superseded_by=row["superseded_by"],
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


class SqliteArchitectureProposalRepository(_SqliteRepository):
    """SQLite adapter for **managed-project** proposals (key: ``proposal_id``).

    Project-scoped by construction: the adapter stores a design for the project
    the assistant manages and never touches ``architecture_versions``,
    ``architecture_change_requests``, ``adrs`` or ``risks``.
    """

    _UPSERT = """
        INSERT INTO architecture_proposals (
            proposal_id, project, created_at, requirement, summary,
            source_review_id, fingerprint, modules, data_flows,
            external_dependencies, architecture_rules, proposed_rules, risks,
            adr_candidates, implementation_phases, unresolved_questions,
            rationale, review_digest, architecture_version, revision_no,
            revision_of, status, decided_by, decided_at, decision_reason,
            revision_feedback, superseded_by
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(proposal_id) DO UPDATE SET
            project = excluded.project,
            created_at = excluded.created_at,
            requirement = excluded.requirement,
            summary = excluded.summary,
            source_review_id = excluded.source_review_id,
            fingerprint = excluded.fingerprint,
            modules = excluded.modules,
            data_flows = excluded.data_flows,
            external_dependencies = excluded.external_dependencies,
            architecture_rules = excluded.architecture_rules,
            proposed_rules = excluded.proposed_rules,
            risks = excluded.risks,
            adr_candidates = excluded.adr_candidates,
            implementation_phases = excluded.implementation_phases,
            unresolved_questions = excluded.unresolved_questions,
            rationale = excluded.rationale,
            review_digest = excluded.review_digest,
            architecture_version = excluded.architecture_version,
            revision_no = excluded.revision_no,
            revision_of = excluded.revision_of,
            status = excluded.status,
            decided_by = excluded.decided_by,
            decided_at = excluded.decided_at,
            decision_reason = excluded.decision_reason,
            revision_feedback = excluded.revision_feedback,
            superseded_by = excluded.superseded_by
    """

    def upsert(self, proposal: ArchitectureProposal) -> None:
        self._upsert(
            self._UPSERT,
            (
                proposal.proposal_id,
                proposal.project,
                _iso(proposal.created_at),
                proposal.requirement,
                proposal.summary,
                proposal.source_review_id,
                proposal.fingerprint,
                _json_dump_seq(proposal.modules),
                _json_dump_seq(proposal.data_flows),
                _json_dump_seq(proposal.external_dependencies),
                _json_dump_seq(proposal.architecture_rules),
                _json_dump_seq(proposal.proposed_rules),
                _json_dump_seq(proposal.risks),
                _json_dump_seq(proposal.adr_candidates),
                _json_dump_seq(proposal.implementation_phases),
                _json_dump_seq(proposal.unresolved_questions),
                proposal.rationale,
                _json_dump_map(proposal.review_digest),
                proposal.architecture_version,
                proposal.revision_no,
                proposal.revision_of,
                _enum_value(proposal.status),
                proposal.decided_by,
                _iso(proposal.decided_at),
                proposal.decision_reason,
                proposal.revision_feedback,
                proposal.superseded_by,
            ),
        )

    def get(self, proposal_id: str) -> Optional[ArchitectureProposal]:
        return self._fetch_one(
            "SELECT * FROM architecture_proposals WHERE proposal_id = ?",
            (proposal_id,),
            _row_to_architecture_proposal,
        )

    def list(self) -> tuple[ArchitectureProposal, ...]:
        return self._fetch_all(
            "SELECT * FROM architecture_proposals "
            "ORDER BY created_at ASC, proposal_id ASC",
            (),
            _row_to_architecture_proposal,
        )

    def list_by_status(
        self, status: ProposalStatus
    ) -> tuple[ArchitectureProposal, ...]:
        """Proposals in one lifecycle status, oldest first.

        Accepts a :class:`ProposalStatus` or its plain string value, exactly
        like every other adapter query in this module.
        """
        return self._fetch_all(
            "SELECT * FROM architecture_proposals "
            "WHERE status = ? ORDER BY created_at ASC, proposal_id ASC",
            (_enum_value(status),),
            _row_to_architecture_proposal,
        )

    def list_for_project(self, project: str) -> tuple[ArchitectureProposal, ...]:
        """The proposals of one managed project, oldest first."""
        return self._fetch_all(
            "SELECT * FROM architecture_proposals "
            "WHERE project = ? ORDER BY created_at ASC, proposal_id ASC",
            (project,),
            _row_to_architecture_proposal,
        )

    def delete(self, proposal_id: str) -> bool:
        """Delete a proposal row.

        Present for contract consistency with the other repositories only: the
        proposal use-cases never delete, so a proposal stays auditable forever.
        """
        return self._delete(
            "DELETE FROM architecture_proposals WHERE proposal_id = ?",
            (proposal_id,),
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


def _row_to_supervision_record(row: sqlite3.Row) -> SupervisionRecord:
    return SupervisionRecord(
        supervision_id=row["supervision_id"],
        project=row["project"],
        step_no=row["step_no"],
        attempt=row["attempt"],
        source_report_hash=row["source_report_hash"],
        architecture_version=row["architecture_version"],
        status=row["status"],
        action=row["action"],
        reason=row["reason"],
        risk=row["risk"],
        instruction_for_cline=row["instruction_for_cline"],
        requires_human=bool(row["requires_human"]),
        provider=row["provider"],
        cost_available=bool(row["cost_available"]),
        evidence=_json_load_seq(row["evidence"]),
        first_seen_at=_parse_dt(row["first_seen_at"]),
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
        decided_by=row["decided_by"],
        decided_at=_parse_dt(row["decided_at"]),
        decision_reason=row["decision_reason"],
        sent_at=_parse_dt(row["sent_at"]),
        escalated_at=_parse_dt(row["escalated_at"]),
    )


class SqliteSupervisionRepository(_SqliteRepository):
    """SQLite adapter for **supervision records** (key: ``supervision_id``).

    Advisory bookkeeping only: the adapter writes one table
    (``supervision_records``) and never touches ``steps``, ``tasks``,
    ``projects``, ``architecture_versions``, ``adrs``, ``risks`` or
    ``architecture_change_requests`` - a supervisor's recommendation is not
    workflow state and can never move the FSM.
    """

    _UPSERT = """
        INSERT INTO supervision_records (
            supervision_id, project, step_no, attempt, source_report_hash,
            architecture_version, status, action, reason, risk,
            instruction_for_cline, requires_human, provider, cost_available,
            evidence, first_seen_at, created_at, updated_at, decided_by,
            decided_at, decision_reason, sent_at, escalated_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?
        )
        ON CONFLICT(supervision_id) DO UPDATE SET
            project = excluded.project,
            step_no = excluded.step_no,
            attempt = excluded.attempt,
            source_report_hash = excluded.source_report_hash,
            architecture_version = excluded.architecture_version,
            status = excluded.status,
            action = excluded.action,
            reason = excluded.reason,
            risk = excluded.risk,
            instruction_for_cline = excluded.instruction_for_cline,
            requires_human = excluded.requires_human,
            provider = excluded.provider,
            cost_available = excluded.cost_available,
            evidence = excluded.evidence,
            first_seen_at = excluded.first_seen_at,
            created_at = excluded.created_at,
            updated_at = excluded.updated_at,
            decided_by = excluded.decided_by,
            decided_at = excluded.decided_at,
            decision_reason = excluded.decision_reason,
            sent_at = excluded.sent_at,
            escalated_at = excluded.escalated_at
    """

    def upsert(self, record: SupervisionRecord) -> None:
        self._upsert(
            self._UPSERT,
            (
                record.supervision_id,
                record.project,
                record.step_no,
                record.attempt,
                record.source_report_hash,
                record.architecture_version,
                _enum_value(record.status),
                _enum_value(record.action),
                record.reason,
                _enum_value(record.risk),
                record.instruction_for_cline,
                _bool_int(record.requires_human),
                record.provider,
                _bool_int(record.cost_available),
                _json_dump_seq(record.evidence),
                _iso(record.first_seen_at),
                _iso(record.created_at),
                _iso(record.updated_at),
                record.decided_by,
                _iso(record.decided_at),
                record.decision_reason,
                _iso(record.sent_at),
                _iso(record.escalated_at),
            ),
        )

    def get(self, supervision_id: str) -> Optional[SupervisionRecord]:
        return self._fetch_one(
            "SELECT * FROM supervision_records WHERE supervision_id = ?",
            (supervision_id,),
            _row_to_supervision_record,
        )

    def find_current(
        self,
        project: str,
        step_no: int,
        attempt: int,
        source_report_hash: str,
        architecture_version: str,
    ) -> Optional[SupervisionRecord]:
        """The record for one exact supervision identity, or ``None``.

        The five columns are the identity, and the unique index over them is what
        makes "one record per exact report" a database-level guarantee rather
        than a convention.
        """
        return self._fetch_one(
            "SELECT * FROM supervision_records WHERE project = ? "
            "AND step_no = ? AND attempt = ? AND source_report_hash = ? "
            "AND architecture_version = ?",
            (
                project,
                step_no,
                attempt,
                source_report_hash,
                architecture_version,
            ),
            _row_to_supervision_record,
        )

    def list(self) -> tuple[SupervisionRecord, ...]:
        return self._fetch_all(
            "SELECT * FROM supervision_records "
            "ORDER BY created_at ASC, rowid ASC",
            (),
            _row_to_supervision_record,
        )

    def list_for_step(
        self, project: str, step_no: int, attempt: int
    ) -> tuple[SupervisionRecord, ...]:
        """Every record of one step/attempt, oldest first.

        Ordered by ``(created_at, rowid)``: the moment the identity appeared, and
        then the order the rows were written. The insert order is what makes
        "which report came last" answerable even when two identities share one
        clock reading - which is exactly what a frozen test clock (or two
        reports inside one second) produces.
        """
        return self._fetch_all(
            "SELECT * FROM supervision_records WHERE project = ? "
            "AND step_no = ? AND attempt = ? "
            "ORDER BY created_at ASC, rowid ASC",
            (project, step_no, attempt),
            _row_to_supervision_record,
        )

    def list_by_status(
        self, status: SupervisorStatus
    ) -> tuple[SupervisionRecord, ...]:
        return self._fetch_all(
            "SELECT * FROM supervision_records WHERE status = ? "
            "ORDER BY created_at ASC, rowid ASC",
            (_enum_value(status),),
            _row_to_supervision_record,
        )

    def list_unresolved(self) -> tuple[SupervisionRecord, ...]:
        """The records a restart must reconcile, oldest first.

        Exactly :data:`~architecture_assistant.domain.enums.SUPERVISION_RECONCILE_STATUSES`:
        an analysis that was owed when the process died, and a send intent whose
        publication may or may not have completed.
        """
        pending = sorted(
            member.value for member in SUPERVISION_RECONCILE_STATUSES
        )
        placeholders = ", ".join("?" for _ in pending)
        return self._fetch_all(
            "SELECT * FROM supervision_records "
            f"WHERE status IN ({placeholders}) "
            "ORDER BY created_at ASC, rowid ASC",
            tuple(pending),
            _row_to_supervision_record,
        )

    def delete(self, supervision_id: str) -> bool:
        """Delete a supervision row.

        Present for contract consistency with the other repositories only: the
        supervision use-cases never delete, so the supervision history of an
        attempt stays auditable forever.
        """
        return self._delete(
            "DELETE FROM supervision_records WHERE supervision_id = ?",
            (supervision_id,),
        )


# ---------------------------------------------------------------------------
# deliberation (Step 29) - one run row plus one addressable row per stage
# ---------------------------------------------------------------------------

#: The lifecycle order of the six stages. Artifacts are presented in *stage*
#: order rather than in the (arbitrary) lexical order of their enum values, so a
#: round history reads exactly as the deliberation ran.
_DELIBERATION_STAGE_ORDER: dict[str, int] = {
    stage: index
    for index, stage in enumerate(
        (
            "AGENT_A_ROUND1",
            "AGENT_B_ROUND1",
            "LEAD_REVIEW",
            "AGENT_A_ROUND2",
            "AGENT_B_ROUND2",
            "FINAL_SYNTHESIS",
        )
    )
}


def _row_to_deliberation_run(row: sqlite3.Row) -> DeliberationRun:
    return DeliberationRun(
        deliberation_id=row["deliberation_id"],
        project=row["project"],
        requirement=row["requirement"],
        fingerprint=row["fingerprint"],
        context_fingerprint=row["context_fingerprint"],
        agent_a_provider=row["agent_a_provider"],
        agent_a_model=row["agent_a_model"],
        agent_b_provider=row["agent_b_provider"],
        agent_b_model=row["agent_b_model"],
        lead_provider=row["lead_provider"],
        lead_model=row["lead_model"],
        status=row["status"],
        max_review_rounds=row["max_review_rounds"],
        revision_no=row["revision_no"],
        revision_of_deliberation_id=row["revision_of_deliberation_id"],
        error_reason=row["error_reason"],
        stale_reason=row["stale_reason"],
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
    )


def _row_to_deliberation_artifact(row: sqlite3.Row) -> DeliberationArtifact:
    return DeliberationArtifact(
        artifact_id=row["artifact_id"],
        deliberation_id=row["deliberation_id"],
        stage=row["stage"],
        slot=row["slot"],
        status=row["status"],
        content=_json_load_map(row["content"]),
        fingerprint=row["fingerprint"],
        created_at=_parse_dt(row["created_at"]),
    )


def _stage_sort_key(artifact: DeliberationArtifact) -> tuple:
    """A deterministic, lifecycle-ordered sort key for artifacts."""
    return (
        _DELIBERATION_STAGE_ORDER.get(str(artifact.stage), 99),
        artifact.slot,
        artifact.created_at.isoformat(),
        artifact.artifact_id,
    )


class SqliteDeliberationRepository(_SqliteRepository):
    """SQLite adapter for deliberations (key: ``deliberation_id``).

    Advisory, managed-project scoped state: it never touches
    ``architecture_versions``, ``architecture_change_requests``, ``adrs``,
    ``risks`` or the managed project's proposal table. A run is created once and
    then only ever advances through its lifecycle, so ``delete`` exists for
    contract consistency with the other repositories and is never called by the
    use-case - a deliberation must stay auditable.
    """

    _RUN_UPSERT = """
        INSERT INTO deliberation_runs (
            deliberation_id, project, requirement, fingerprint,
            context_fingerprint, agent_a_provider, agent_a_model,
            agent_b_provider, agent_b_model, lead_provider, lead_model,
            status, max_review_rounds, revision_no,
            revision_of_deliberation_id, error_reason, stale_reason,
            created_at, updated_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(deliberation_id) DO UPDATE SET
            project = excluded.project,
            requirement = excluded.requirement,
            fingerprint = excluded.fingerprint,
            context_fingerprint = excluded.context_fingerprint,
            agent_a_provider = excluded.agent_a_provider,
            agent_a_model = excluded.agent_a_model,
            agent_b_provider = excluded.agent_b_provider,
            agent_b_model = excluded.agent_b_model,
            lead_provider = excluded.lead_provider,
            lead_model = excluded.lead_model,
            status = excluded.status,
            max_review_rounds = excluded.max_review_rounds,
            revision_no = excluded.revision_no,
            revision_of_deliberation_id = excluded.revision_of_deliberation_id,
            error_reason = excluded.error_reason,
            stale_reason = excluded.stale_reason,
            created_at = excluded.created_at,
            updated_at = excluded.updated_at
    """

    _ARTIFACT_UPSERT = """
        INSERT INTO deliberation_artifacts (
            artifact_id, deliberation_id, stage, slot, status, content,
            fingerprint, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(artifact_id) DO UPDATE SET
            deliberation_id = excluded.deliberation_id,
            stage = excluded.stage,
            slot = excluded.slot,
            status = excluded.status,
            content = excluded.content,
            fingerprint = excluded.fingerprint,
            created_at = excluded.created_at
    """

    def upsert_run(self, run: DeliberationRun) -> None:
        self._upsert(
            self._RUN_UPSERT,
            (
                run.deliberation_id,
                run.project,
                run.requirement,
                run.fingerprint,
                run.context_fingerprint,
                run.agent_a_provider,
                run.agent_a_model,
                run.agent_b_provider,
                run.agent_b_model,
                run.lead_provider,
                run.lead_model,
                _enum_value(run.status),
                run.max_review_rounds,
                run.revision_no,
                run.revision_of_deliberation_id,
                run.error_reason,
                run.stale_reason,
                _iso(run.created_at),
                _iso(run.updated_at),
            ),
        )

    def get_run(self, deliberation_id: str) -> Optional[DeliberationRun]:
        return self._fetch_one(
            "SELECT * FROM deliberation_runs WHERE deliberation_id = ?",
            (deliberation_id,),
            _row_to_deliberation_run,
        )

    def list_runs(self) -> tuple[DeliberationRun, ...]:
        return self._fetch_all(
            "SELECT * FROM deliberation_runs "
            "ORDER BY created_at ASC, deliberation_id ASC",
            (),
            _row_to_deliberation_run,
        )

    def list_runs_by_status(
        self, status: DeliberationStatus
    ) -> tuple[DeliberationRun, ...]:
        return self._fetch_all(
            "SELECT * FROM deliberation_runs WHERE status = ? "
            "ORDER BY created_at ASC, deliberation_id ASC",
            (_enum_value(status),),
            _row_to_deliberation_run,
        )

    def list_runs_for_project(self, project: str) -> tuple[DeliberationRun, ...]:
        return self._fetch_all(
            "SELECT * FROM deliberation_runs WHERE project = ? "
            "ORDER BY created_at ASC, deliberation_id ASC",
            (project,),
            _row_to_deliberation_run,
        )

    def upsert_artifact(self, artifact: DeliberationArtifact) -> None:
        self._upsert(
            self._ARTIFACT_UPSERT,
            (
                artifact.artifact_id,
                artifact.deliberation_id,
                _enum_value(artifact.stage),
                artifact.slot,
                _enum_value(artifact.status),
                _json_dump_map(artifact.content),
                artifact.fingerprint,
                _iso(artifact.created_at),
            ),
        )

    def get_artifact(self, artifact_id: str) -> Optional[DeliberationArtifact]:
        return self._fetch_one(
            "SELECT * FROM deliberation_artifacts WHERE artifact_id = ?",
            (artifact_id,),
            _row_to_deliberation_artifact,
        )

    def list_artifacts(
        self, deliberation_id: str
    ) -> tuple[DeliberationArtifact, ...]:
        """Every artifact of one run, in lifecycle stage order."""
        fetched = self._fetch_all(
            "SELECT * FROM deliberation_artifacts WHERE deliberation_id = ?",
            (deliberation_id,),
            _row_to_deliberation_artifact,
        )
        return tuple(sorted(fetched, key=_stage_sort_key))

    def list_artifacts_for_stage(
        self, deliberation_id: str, stage: DeliberationStage, slot: str = ""
    ) -> tuple[DeliberationArtifact, ...]:
        """The artifacts of one stage (optionally one seat), oldest first."""
        if slot:
            return self._fetch_all(
                "SELECT * FROM deliberation_artifacts "
                "WHERE deliberation_id = ? AND stage = ? AND slot = ? "
                "ORDER BY created_at ASC, artifact_id ASC",
                (deliberation_id, _enum_value(stage), slot),
                _row_to_deliberation_artifact,
            )
        return self._fetch_all(
            "SELECT * FROM deliberation_artifacts "
            "WHERE deliberation_id = ? AND stage = ? "
            "ORDER BY created_at ASC, artifact_id ASC",
            (deliberation_id, _enum_value(stage)),
            _row_to_deliberation_artifact,
        )

    def delete(self, deliberation_id: str) -> bool:
        """Delete one run together with its stage artifacts."""
        self._execute_write(
            "DELETE FROM deliberation_artifacts WHERE deliberation_id = ?",
            (deliberation_id,),
        )
        return self._delete(
            "DELETE FROM deliberation_runs WHERE deliberation_id = ?",
            (deliberation_id,),
        )

