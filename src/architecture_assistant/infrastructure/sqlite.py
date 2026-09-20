"""SQLite connection handling and the deterministic migration runner.

This module is the only place in the project that opens a SQLite connection.
Adapters in :mod:`architecture_assistant.infrastructure.repositories` receive an
already-open connection and never manage it themselves.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Union

from ..domain.models import utc_now
from .migrations import BOOTSTRAP_SCHEMA_SQL, MIGRATIONS, Migration

__all__ = [
    "DEFAULT_DATABASE_PATH",
    "DatabasePath",
    "open_database",
    "close_database",
    "apply_migrations",
    "applied_versions",
    "SqliteTransactionPort",
]

#: Either a filesystem path or ``":memory:"``.
DatabasePath = Union[str, Path]

#: Default location of the source-of-truth database.
#: ``.mini_build/`` is a separate controller area and is never touched.
DEFAULT_DATABASE_PATH: Path = Path("data") / "architecture_assistant.db"


def _resolve_path(path: DatabasePath) -> str:
    raw = str(path)
    if raw == ":memory:":
        return raw
    target = Path(raw)
    target.parent.mkdir(parents=True, exist_ok=True)
    return str(target)


def open_database(
    path: DatabasePath = DEFAULT_DATABASE_PATH,
    *,
    apply_schema: bool = True,
) -> sqlite3.Connection:
    """Open the SQLite source of truth, migrating it by default.

    ``PRAGMA foreign_keys = ON`` is enforced on every connection.
    """
    connection = sqlite3.connect(_resolve_path(path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    if apply_schema:
        apply_migrations(connection)
    return connection


def close_database(connection: sqlite3.Connection) -> None:
    """Close a connection opened by :func:`open_database`."""
    connection.close()


class SqliteTransactionPort:
    """SQLite transaction boundary over one shared connection.

    While a transaction is open the connection runs in explicit-transaction mode
    (``isolation_level is None``). The repository adapters detect that mode and
    stop committing their individual writes, so everything written inside the
    ``with`` block - domain state *and* the audit entry - becomes one atomic
    commit or one atomic rollback.

    Nested ``transaction()`` calls join the outermost transaction.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._depth = 0

    @property
    def connection(self) -> sqlite3.Connection:
        """The shared connection this boundary owns commits for."""
        return self._connection

    @property
    def in_transaction(self) -> bool:
        """Whether a transaction scope is currently open."""
        return self._depth > 0

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Commit on success, roll back on any exception."""
        if self._depth > 0:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return

        previous_isolation = self._connection.isolation_level
        self._connection.isolation_level = None  # explicit transaction control
        self._depth = 1
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")
        finally:
            self._depth = 0
            self._connection.isolation_level = previous_isolation


def _ensure_bootstrap(connection: sqlite3.Connection) -> None:
    """Create the ``schema_migrations`` bookkeeping table if missing."""
    connection.execute(BOOTSTRAP_SCHEMA_SQL)
    connection.commit()


def applied_versions(connection: sqlite3.Connection) -> tuple[int, ...]:
    """Return the applied migration versions, ascending and unique."""
    _ensure_bootstrap(connection)
    rows = connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version ASC"
    ).fetchall()
    return tuple(int(row[0]) for row in rows)


def _validate_migrations(migrations: tuple[Migration, ...]) -> None:
    """Fail fast on duplicate or out-of-order migration versions."""
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
    """Apply a single migration atomically.

    Uses explicit ``BEGIN IMMEDIATE``/``COMMIT``/``ROLLBACK`` because the
    ``sqlite3`` module only opens implicit transactions for DML - DDL such as
    ``CREATE TABLE`` would otherwise run in autocommit mode and could leave a
    half-applied schema behind. The ``schema_migrations`` row is written inside
    the same transaction, so a failure rolls back both the schema change and the
    bookkeeping entry.
    """
    previous_isolation = connection.isolation_level
    connection.isolation_level = None  # explicit transaction control
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            migration.up(connection)
            connection.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) "
                "VALUES (?, ?, ?)",
                (migration.version, migration.name, utc_now().isoformat()),
            )
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")
    finally:
        connection.isolation_level = previous_isolation


def apply_migrations(
    connection: sqlite3.Connection,
    migrations: tuple[Migration, ...] = MIGRATIONS,
) -> tuple[int, ...]:
    """Apply all pending migrations and return the applied versions.

    The runner is idempotent: already-applied versions are skipped, so calling
    it again after a restart changes nothing.
    """
    _validate_migrations(migrations)
    _ensure_bootstrap(connection)
    applied = set(applied_versions(connection))
    for migration in migrations:
        if migration.version in applied:
            continue
        _apply_one(connection, migration)
    return applied_versions(connection)

