"""Versioned, deterministic SQLite schema migrations.

``schema_migrations`` is the bootstrap table: it is created before any migration
runs and records which versions have been applied. The runner in
:mod:`architecture_assistant.infrastructure.sqlite` applies every migration
inside its own transaction, so a failed migration is never marked as applied.

Schema notes
------------
* One column per domain field (query-friendly, no opaque JSON blobs).
* Enums are stored as their ``TEXT`` value, datetimes as ISO-8601 ``TEXT``,
  ``bool`` as ``INTEGER`` 0/1, tuples as JSON arrays and mappings as JSON objects.
* The only foreign key is ``tasks.step_no -> steps.step_no``; it is semantically
  guaranteed by the domain model. No speculative relations between
  ADR/Risk/Finding/Decision are introduced.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable, Optional

__all__ = [
    "Migration",
    "MIGRATIONS",
    "BOOTSTRAP_SCHEMA_SQL",
    "TABLE_NAMES",
    "AUDIT_TABLE_NAME",
    "CHANGE_REQUEST_TABLE_NAME",
    "COST_TABLE_NAME",
    "PROPOSAL_TABLE_NAME",
    "SUPERVISION_TABLE_NAME",
]


#: Bootstrap DDL - always executed before the versioned migrations.
BOOTSTRAP_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""


@dataclass(frozen=True)
class Migration:
    """A single, atomically applied schema migration."""

    version: int
    name: str
    up: Callable[[sqlite3.Connection], None]
    down: Optional[Callable[[sqlite3.Connection], None]] = None


_MIGRATION_001_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE project (
        name TEXT PRIMARY KEY,
        plan_version TEXT NOT NULL,
        plan_hash TEXT NOT NULL DEFAULT '',
        mode TEXT NOT NULL,
        paused INTEGER NOT NULL DEFAULT 0,
        current_step_no_snapshot INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE steps (
        step_no INTEGER PRIMARY KEY,
        phase TEXT NOT NULL,
        title TEXT NOT NULL,
        state TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        attempt INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 3,
        risk TEXT NOT NULL DEFAULT 'LOW',
        requires_human INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        verified_at TEXT,
        last_update_at TEXT
    )
    """,
    """
    CREATE TABLE tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        step_no INTEGER NOT NULL,
        phase TEXT NOT NULL,
        title TEXT NOT NULL,
        state TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        risk TEXT NOT NULL DEFAULT 'MEDIUM',
        attempt INTEGER NOT NULL DEFAULT 1,
        max_attempts INTEGER NOT NULL DEFAULT 3,
        context_file TEXT,
        instructions TEXT NOT NULL DEFAULT '{}',
        report_schema TEXT NOT NULL DEFAULT '{}',
        report_status TEXT,
        created_at TEXT NOT NULL,
        CONSTRAINT uq_tasks_step_attempt UNIQUE (step_no, attempt),
        CONSTRAINT fk_tasks_step_no FOREIGN KEY (step_no)
            REFERENCES steps (step_no)
    )
    """,


    """
    CREATE TABLE architecture_versions (
        version TEXT PRIMARY KEY,
        baseline TEXT NOT NULL DEFAULT '',
        rules TEXT NOT NULL DEFAULT '[]',
        superseded_by TEXT,
        is_current INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE adrs (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        status TEXT NOT NULL,
        context TEXT NOT NULL DEFAULT '',
        decision TEXT NOT NULL DEFAULT '',
        consequences TEXT NOT NULL DEFAULT '[]',
        version INTEGER NOT NULL DEFAULT 1,
        superseded_by TEXT,
        related TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL,
        updated_at TEXT
    )
    """,
    """
    CREATE TABLE risks (
        id TEXT PRIMARY KEY,
        description TEXT NOT NULL,
        severity TEXT NOT NULL,
        probability REAL NOT NULL DEFAULT 0.5,
        impact TEXT NOT NULL,
        owner TEXT NOT NULL DEFAULT '',
        mitigation TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT
    )
    """,
    """
    CREATE TABLE findings (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        claim TEXT NOT NULL,
        evidence TEXT NOT NULL DEFAULT '[]',
        confidence REAL NOT NULL DEFAULT 0.5,
        severity TEXT NOT NULL,
        step_no INTEGER,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE decisions (
        id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        decision TEXT NOT NULL DEFAULT '',
        rationale TEXT NOT NULL DEFAULT '',
        rules_applied TEXT NOT NULL DEFAULT '[]',
        evidence_refs TEXT NOT NULL DEFAULT '[]',
        perspectives TEXT NOT NULL DEFAULT '[]',
        step_no INTEGER,
        created_at TEXT NOT NULL
    )
    """,
)


#: Tables created by migration v1 (the foundation schema).
_V1_TABLE_NAMES: tuple[str, ...] = (
    "project",
    "steps",
    "tasks",
    "architecture_versions",
    "adrs",
    "risks",
    "findings",
    "decisions",
)

#: Table created by migration v2 (append-only audit trail).
AUDIT_TABLE_NAME = "audit_entries"

#: Table created by migration v3 (architecture change requests).
CHANGE_REQUEST_TABLE_NAME = "architecture_change_requests"

#: Table created by migration v4 (idempotent cost accounting telemetry).
COST_TABLE_NAME = "cost_records"

#: Table created by migration v5 (managed-project architecture proposals).
PROPOSAL_TABLE_NAME = "architecture_proposals"

#: Table created by migration v6 (advisory supervision records).
SUPERVISION_TABLE_NAME = "supervision_records"

#: All source-of-truth tables created by the migrations (excluding the
#: ``schema_migrations`` bookkeeping table).
TABLE_NAMES: tuple[str, ...] = (
    _V1_TABLE_NAMES
    + (
        AUDIT_TABLE_NAME,
        CHANGE_REQUEST_TABLE_NAME,
        COST_TABLE_NAME,
        PROPOSAL_TABLE_NAME,
        SUPERVISION_TABLE_NAME,
    )
)


def _migration_001_initial_schema(conn: sqlite3.Connection) -> None:
    """Create the initial source-of-truth schema."""
    for statement in _MIGRATION_001_STATEMENTS:
        conn.execute(statement)


def _migration_001_down(conn: sqlite3.Connection) -> None:
    """Drop the initial schema (reverse order of creation)."""
    for table in reversed(_V1_TABLE_NAMES):
        conn.execute(f"DROP TABLE IF EXISTS {table}")


_MIGRATION_002_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE audit_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        action TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX idx_audit_entries_entity
        ON audit_entries (entity_type, entity_id, id)
    """,
)


def _migration_002_audit_trail(conn: sqlite3.Connection) -> None:
    """Create the append-only audit trail."""
    for statement in _MIGRATION_002_STATEMENTS:
        conn.execute(statement)


def _migration_002_down(conn: sqlite3.Connection) -> None:
    """Drop the audit trail."""
    conn.execute("DROP INDEX IF EXISTS idx_audit_entries_entity")
    conn.execute(f"DROP TABLE IF EXISTS {AUDIT_TABLE_NAME}")


_MIGRATION_003_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE architecture_change_requests (
        request_id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        rationale TEXT NOT NULL,
        source_version TEXT NOT NULL,
        target_version TEXT NOT NULL,
        rule_ids TEXT NOT NULL DEFAULT '[]',
        roadmap_impact TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL,
        adr_id TEXT NOT NULL,
        approved_by TEXT,
        approved_at TEXT,
        applied_at TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX idx_change_requests_status
        ON architecture_change_requests (status, created_at, request_id)
    """,
)


def _migration_003_architecture_change_requests(
    conn: sqlite3.Connection,
) -> None:
    """Create the architecture change request table (evolution state)."""
    for statement in _MIGRATION_003_STATEMENTS:
        conn.execute(statement)


def _migration_003_down(conn: sqlite3.Connection) -> None:
    """Drop the architecture change request table."""
    conn.execute("DROP INDEX IF EXISTS idx_change_requests_status")
    conn.execute(f"DROP TABLE IF EXISTS {CHANGE_REQUEST_TABLE_NAME}")


#: Cost accounting is *telemetry*, not domain state, so the table carries no
#: foreign key to ``steps``: a billable provider call may legitimately happen
#: before (or without) a persisted Step, and the existing schema rule is to add
#: no speculative relations between aggregates.
#:
#: ``event_id`` is ``UNIQUE`` because it is the stable identity of one logical
#: billable provider event - the database itself is the last backstop that makes
#: ``record()`` idempotent, even if a caller forgets to look first.
#:
#: The only index is on ``created_at``: the one clearly justified query pattern
#: is a time window ("today", "this month"). The remaining filters are low
#: cardinality equality checks on a small telemetry table, so no further indexes
#: and no analytics structures are introduced.
_MIGRATION_004_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE cost_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        provider TEXT NOT NULL,
        model TEXT NOT NULL DEFAULT '',
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0,
        pricing_known INTEGER NOT NULL DEFAULT 0,
        project TEXT,
        step_no INTEGER,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX idx_cost_records_created_at ON cost_records (created_at)
    """,
)


def _migration_004_cost_records(conn: sqlite3.Connection) -> None:
    """Create the idempotent cost accounting telemetry table."""
    for statement in _MIGRATION_004_STATEMENTS:
        conn.execute(statement)


def _migration_004_down(conn: sqlite3.Connection) -> None:
    """Drop the cost accounting telemetry table."""
    conn.execute("DROP INDEX IF EXISTS idx_cost_records_created_at")
    conn.execute(f"DROP TABLE IF EXISTS {COST_TABLE_NAME}")


#: Managed-project architecture proposals are **project-scoped** state: a
#: design for the project the assistant manages, never the assistant's own
#: baseline (which lives in ``architecture_versions`` and is declared in code).
#: The table carries no foreign key for the same reason the schema rule already
#: states - no speculative relations between aggregates - and because a proposal
#: may legitimately be recorded before any step exists.
#:
#: The two indexes are the query patterns the proposal board actually uses:
#: "the proposals of this project, newest first" and "the proposals in this
#: status" (the panel needs the open drafts).
_MIGRATION_005_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE architecture_proposals (
        proposal_id TEXT PRIMARY KEY,
        project TEXT NOT NULL,
        created_at TEXT NOT NULL,
        requirement TEXT NOT NULL,
        summary TEXT NOT NULL,
        source_review_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        modules TEXT NOT NULL DEFAULT '[]',
        data_flows TEXT NOT NULL DEFAULT '[]',
        external_dependencies TEXT NOT NULL DEFAULT '[]',
        architecture_rules TEXT NOT NULL DEFAULT '[]',
        proposed_rules TEXT NOT NULL DEFAULT '[]',
        risks TEXT NOT NULL DEFAULT '[]',
        adr_candidates TEXT NOT NULL DEFAULT '[]',
        implementation_phases TEXT NOT NULL DEFAULT '[]',
        unresolved_questions TEXT NOT NULL DEFAULT '[]',
        rationale TEXT NOT NULL DEFAULT '',
        review_digest TEXT NOT NULL DEFAULT '{}',
        architecture_version TEXT,
        revision_no INTEGER NOT NULL DEFAULT 1,
        revision_of TEXT,
        status TEXT NOT NULL,
        decided_by TEXT NOT NULL DEFAULT '',
        decided_at TEXT,
        decision_reason TEXT NOT NULL DEFAULT '',
        revision_feedback TEXT NOT NULL DEFAULT '',
        superseded_by TEXT
    )
    """,
    """
    CREATE INDEX idx_architecture_proposals_status
        ON architecture_proposals (status, created_at, proposal_id)
    """,
    """
    CREATE INDEX idx_architecture_proposals_project
        ON architecture_proposals (project, created_at, proposal_id)
    """,
)


def _migration_005_architecture_proposals(
    conn: sqlite3.Connection,
) -> None:
    """Create the managed-project architecture proposal table."""
    for statement in _MIGRATION_005_STATEMENTS:
        conn.execute(statement)


def _migration_005_down(conn: sqlite3.Connection) -> None:
    """Drop the managed-project architecture proposal table."""
    conn.execute("DROP INDEX IF EXISTS idx_architecture_proposals_status")
    conn.execute("DROP INDEX IF EXISTS idx_architecture_proposals_project")
    conn.execute(f"DROP TABLE IF EXISTS {PROPOSAL_TABLE_NAME}")


#: Supervision records (Step 28 Phase 1) are **advisory bookkeeping**: one row per
#: exact supervision identity, i.e. per
#: ``(project, step_no, attempt, source_report_hash, architecture_version)``. The
#: table is deliberately independent of ``steps``: it is not workflow state, it
#: carries no attempt counter and no FSM position, and a record may legitimately
#: exist for a report whose step a human later abandons.
#:
#: The two indexes are the query patterns the supervision code actually uses:
#: "the exact record for this report identity" (the gate and the tick) and "the
#: records that still need reconciliation" (the startup pass).
_MIGRATION_006_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE supervision_records (
        supervision_id TEXT PRIMARY KEY,
        project TEXT NOT NULL,
        step_no INTEGER NOT NULL,
        attempt INTEGER NOT NULL,
        source_report_hash TEXT NOT NULL,
        architecture_version TEXT NOT NULL,
        status TEXT NOT NULL,
        action TEXT,
        reason TEXT NOT NULL DEFAULT '',
        risk TEXT,
        instruction_for_cline TEXT NOT NULL DEFAULT '',
        requires_human INTEGER NOT NULL DEFAULT 0,
        provider TEXT NOT NULL DEFAULT '',
        cost_available INTEGER NOT NULL DEFAULT 0,
        evidence TEXT NOT NULL DEFAULT '[]',
        first_seen_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        decided_by TEXT NOT NULL DEFAULT '',
        decided_at TEXT,
        decision_reason TEXT NOT NULL DEFAULT '',
        sent_at TEXT,
        escalated_at TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX idx_supervision_identity
        ON supervision_records (
            project, step_no, attempt, source_report_hash, architecture_version
        )
    """,
    """
    CREATE INDEX idx_supervision_status
        ON supervision_records (status, created_at, supervision_id)
    """,
)


def _migration_006_supervision_records(conn: sqlite3.Connection) -> None:
    """Create the supervision record table."""
    for statement in _MIGRATION_006_STATEMENTS:
        conn.execute(statement)


def _migration_006_down(conn: sqlite3.Connection) -> None:
    """Drop the supervision record table."""
    conn.execute("DROP INDEX IF EXISTS idx_supervision_status")
    conn.execute("DROP INDEX IF EXISTS idx_supervision_identity")
    conn.execute(f"DROP TABLE IF EXISTS {SUPERVISION_TABLE_NAME}")


#: Ordered migration list. Versions must be unique and strictly increasing.
MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="initial_schema",
        up=_migration_001_initial_schema,
        down=_migration_001_down,
    ),
    Migration(
        version=2,
        name="audit_trail",
        up=_migration_002_audit_trail,
        down=_migration_002_down,
    ),
    Migration(
        version=3,
        name="architecture_change_requests",
        up=_migration_003_architecture_change_requests,
        down=_migration_003_down,
    ),
    Migration(
        version=4,
        name="cost_records",
        up=_migration_004_cost_records,
        down=_migration_004_down,
    ),
    Migration(
        version=5,
        name="architecture_proposals",
        up=_migration_005_architecture_proposals,
        down=_migration_005_down,
    ),
    Migration(
        version=6,
        name="supervision_records",
        up=_migration_006_supervision_records,
        down=_migration_006_down,
    ),
)
