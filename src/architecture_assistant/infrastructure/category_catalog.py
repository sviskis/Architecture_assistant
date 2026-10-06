"""Global catalog SQLite adapter - a database of its own.

The global category catalog is authoritative but **separate from every project
database**: it opens its own connection, runs its own ordered migrations and
keeps its own ``catalog_audit`` trail. It is never opened by
:func:`architecture_assistant.infrastructure.sqlite.open_database` and never
shares a connection with a project.

Two invariants are structural here:

* ``catalog_schema_migrations`` bookkeeps this database only, and every catalog
  migration is applied atomically (``BEGIN IMMEDIATE``/COMMIT/ROLLBACK);
* both catalog repositories share the transaction rule of the project adapters -
  a write joins the open explicit transaction when one exists and otherwise
  commits itself - so the use-case can commit a mutation and its audit entry
  together.

This module is the only one that imports :mod:`sqlite3` for the catalog.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Sequence, Union

from ..domain.category import (
    CatalogAuditAction,
    CatalogAuditEntry,
    Category,
)
from ..domain.models import utc_now
from .migrations import Migration

__all__ = [
    "CATALOG_BOOTSTRAP_SQL",
    "CATEGORY_TABLE_NAME",
    "CATALOG_AUDIT_TABLE_NAME",
    "CATALOG_MIGRATIONS",
    "CatalogPath",
    "open_category_database",
    "apply_catalog_migrations",
    "applied_catalog_versions",
    "SqliteCategoryRepository",
    "SqliteCatalogAuditRepository",
]

#: The bookkeeping table of the global catalog database (its own, never shared).
CATALOG_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS catalog_schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""

CATEGORY_TABLE_NAME = "categories"
CATALOG_AUDIT_TABLE_NAME = "catalog_audit"

#: Either a filesystem path or ``":memory:"``.
CatalogPath = Union[str, Path]

_CATEGORY_TABLE_DDL = f"""
CREATE TABLE {CATEGORY_TABLE_NAME} (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    spec_path TEXT NOT NULL,
    library_path TEXT NOT NULL,
    supervisor_rules_path TEXT NOT NULL,
    registry_hash TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_CATALOG_AUDIT_TABLE_DDL = f"""
CREATE TABLE {CATALOG_AUDIT_TABLE_NAME} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    category_id TEXT,
    old_value TEXT NOT NULL DEFAULT '{{}}',
    new_value TEXT NOT NULL DEFAULT '{{}}',
    reason TEXT NOT NULL
)
"""

_CATALOG_AUDIT_INDEX_DDL = (
    f"CREATE INDEX idx_catalog_audit_category "
    f"ON {CATALOG_AUDIT_TABLE_NAME} (category_id, id)"
)


def _migration_001_global_catalog(conn: sqlite3.Connection) -> None:
    """Create the category registry and its append-only audit trail."""
    conn.execute(_CATEGORY_TABLE_DDL)
    conn.execute(_CATALOG_AUDIT_TABLE_DDL)
    conn.execute(_CATALOG_AUDIT_INDEX_DDL)


def _migration_001_down(conn: sqlite3.Connection) -> None:
    """Drop the catalog tables."""
    conn.execute("DROP INDEX IF EXISTS idx_catalog_audit_category")
    conn.execute(f"DROP TABLE IF EXISTS {CATALOG_AUDIT_TABLE_NAME}")
    conn.execute(f"DROP TABLE IF EXISTS {CATEGORY_TABLE_NAME}")


#: Ordered catalog migrations. Versions must be unique and strictly increasing.
CATALOG_MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="global_category_catalog",
        up=_migration_001_global_catalog,
        down=_migration_001_down,
    ),
)


def _resolve_path(path: CatalogPath) -> str:
    raw = str(path)
    if raw == ":memory:":
        return raw
    target = Path(raw)
    target.parent.mkdir(parents=True, exist_ok=True)
    return str(target)


def _ensure_bootstrap(connection: sqlite3.Connection) -> None:
    """Create the catalog's own bookkeeping table if missing."""
    connection.execute(CATALOG_BOOTSTRAP_SQL)
    connection.commit()


def applied_catalog_versions(connection: sqlite3.Connection) -> tuple[int, ...]:
    """The applied catalog migration versions, ascending and unique."""
    _ensure_bootstrap(connection)
    rows = connection.execute(
        "SELECT version FROM catalog_schema_migrations ORDER BY version ASC"
    ).fetchall()
    return tuple(int(row[0]) for row in rows)


def _validate_migrations(migrations: tuple[Migration, ...]) -> None:
    seen: set[int] = set()
    previous = 0
    for migration in migrations:
        if migration.version in seen:
            raise ValueError(f"duplicate migration version {migration.version}")
        if migration.version <= previous:
            raise ValueError(
                "migration versions must be strictly increasing; "
                f"got {migration.version} after {previous}"
            )
        seen.add(migration.version)
        previous = migration.version


def _apply_one(connection: sqlite3.Connection, migration: Migration) -> None:
    """Apply one catalog migration atomically (DDL + bookkeeping together)."""
    previous_isolation = connection.isolation_level
    connection.isolation_level = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            migration.up(connection)
            connection.execute(
                "INSERT INTO catalog_schema_migrations "
                "(version, name, applied_at) VALUES (?, ?, ?)",
                (migration.version, migration.name, utc_now().isoformat()),
            )
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")
    finally:
        connection.isolation_level = previous_isolation


def apply_catalog_migrations(
    connection: sqlite3.Connection,
    migrations: tuple[Migration, ...] = CATALOG_MIGRATIONS,
) -> tuple[int, ...]:
    """Apply every pending catalog migration; idempotent across restarts."""
    _validate_migrations(migrations)
    _ensure_bootstrap(connection)
    applied = set(applied_catalog_versions(connection))
    for migration in migrations:
        if migration.version in applied:
            continue
        _apply_one(connection, migration)
    return applied_catalog_versions(connection)


def open_category_database(
    path: CatalogPath,
    *,
    apply_schema: bool = True,
) -> sqlite3.Connection:
    """Open the global catalog database, migrating it by default.

    ``PRAGMA foreign_keys = ON`` is enforced. This is a *different* database from
    any project's and is never opened by the project storage path.
    """
    connection = sqlite3.connect(_resolve_path(path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    if apply_schema:
        apply_catalog_migrations(connection)
    return connection


# ---------------------------------------------------------------------------
# shared private helpers
# ---------------------------------------------------------------------------

def _bool_int(value: bool) -> int:
    """SQLite has no bool type: store 0/1."""
    return 1 if value else 0


def _iso(value: Optional[datetime]) -> Optional[str]:
    return None if value is None else value.isoformat()


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _json_dump_map(values: Any) -> str:
    """Serialize a mapping as a JSON object with sorted keys (deterministic)."""
    return json.dumps(dict(values or {}), sort_keys=True)


def _json_load_map(value: Any) -> dict:
    if not value:
        return {}
    return dict(json.loads(str(value)))


class _CatalogRepository:
    """Shared SQLite plumbing for the two catalog adapters.

    Holds the connection only. ``_execute_write`` commits only when no outer
    boundary owns the write, exactly like the project adapters - so a write made
    inside ``SqliteTransactionPort.transaction()`` joins that transaction.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    @property
    def connection(self) -> sqlite3.Connection:
        """The shared catalog connection."""
        return self._connection

    def _execute_write(self, sql: str, params: Sequence[Any]) -> sqlite3.Cursor:
        cursor = self._connection.execute(sql, tuple(params))
        if self._connection.isolation_level is not None:
            self._connection.commit()
        return cursor


def _row_to_category(row: sqlite3.Row) -> Category:
    return Category(
        id=row["id"],
        name=row["name"],
        version=row["version"],
        spec_path=row["spec_path"],
        library_path=row["library_path"],
        supervisor_rules_path=row["supervisor_rules_path"],
        registry_hash=row["registry_hash"],
        enabled=bool(row["enabled"]),
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
    )


def _row_to_catalog_audit(row: sqlite3.Row) -> CatalogAuditEntry:
    return CatalogAuditEntry(
        id=int(row["id"]),
        timestamp=_parse_dt(row["timestamp"]),
        actor=row["actor"],
        action=CatalogAuditAction(row["action"]),
        category_id=row["category_id"],
        old_value=_json_load_map(row["old_value"]),
        new_value=_json_load_map(row["new_value"]),
        reason=row["reason"],
    )


# ---------------------------------------------------------------------------
# the two catalog adapters
# ---------------------------------------------------------------------------

class SqliteCategoryRepository(_CatalogRepository):
    """SQLite adapter for the global ``categories`` registry (key: ``id``).

    Writes use ``INSERT ... ON CONFLICT(id) DO UPDATE`` - never
    ``INSERT OR REPLACE`` - so a row keeps its identity and its ``created_at``,
    and the immutable ``id`` is never rewritten.
    """

    _UPSERT = f"""
        INSERT INTO {CATEGORY_TABLE_NAME} (
            id, name, version, spec_path, library_path, supervisor_rules_path,
            registry_hash, enabled, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            version = excluded.version,
            spec_path = excluded.spec_path,
            library_path = excluded.library_path,
            supervisor_rules_path = excluded.supervisor_rules_path,
            registry_hash = excluded.registry_hash,
            enabled = excluded.enabled,
            updated_at = excluded.updated_at
    """

    def upsert(self, category: Category) -> None:
        self._execute_write(
            self._UPSERT,
            (
                category.id,
                category.name,
                category.version,
                category.spec_path,
                category.library_path,
                category.supervisor_rules_path,
                category.registry_hash,
                _bool_int(category.enabled),
                _iso(category.created_at),
                _iso(category.updated_at),
            ),
        )

    def get(self, category_id: str) -> Optional[Category]:
        row = self._connection.execute(
            f"SELECT * FROM {CATEGORY_TABLE_NAME} WHERE id = ?",
            (category_id,),
        ).fetchone()
        return None if row is None else _row_to_category(row)

    def list(self) -> tuple[Category, ...]:
        rows = self._connection.execute(
            f"SELECT * FROM {CATEGORY_TABLE_NAME} ORDER BY id ASC"
        ).fetchall()
        return tuple(_row_to_category(row) for row in rows)


class SqliteCatalogAuditRepository(_CatalogRepository):
    """Append-only SQLite adapter for the catalog audit trail.

    There is intentionally no update or delete: catalog history is immutable.
    Ordering is by the internal autoincrement id, which is the append order.
    """

    _INSERT = f"""
        INSERT INTO {CATALOG_AUDIT_TABLE_NAME} (
            timestamp, actor, action, category_id, old_value, new_value, reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """

    def append(self, entry: CatalogAuditEntry) -> None:
        self._execute_write(
            self._INSERT,
            (
                _iso(entry.timestamp),
                entry.actor,
                entry.action.value,
                entry.category_id,
                _json_dump_map(entry.old_value),
                _json_dump_map(entry.new_value),
                entry.reason,
            ),
        )

    def list(self) -> tuple[CatalogAuditEntry, ...]:
        rows = self._connection.execute(
            f"SELECT * FROM {CATALOG_AUDIT_TABLE_NAME} ORDER BY id ASC"
        ).fetchall()
        return tuple(_row_to_catalog_audit(row) for row in rows)

    def list_for_category(
        self, category_id: str
    ) -> tuple[CatalogAuditEntry, ...]:
        rows = self._connection.execute(
            f"SELECT * FROM {CATALOG_AUDIT_TABLE_NAME} "
            "WHERE category_id = ? ORDER BY id ASC",
            (category_id,),
        ).fetchall()
        return tuple(_row_to_catalog_audit(row) for row in rows)
