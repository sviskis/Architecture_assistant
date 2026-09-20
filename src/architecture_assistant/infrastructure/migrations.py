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

#: All source-of-truth tables created by the migrations (excluding the
#: ``schema_migrations`` bookkeeping table).
TABLE_NAMES: tuple[str, ...] = (
    _V1_TABLE_NAMES + (AUDIT_TABLE_NAME, CHANGE_REQUEST_TABLE_NAME)
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
)

