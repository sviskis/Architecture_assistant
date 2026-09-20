"""Unit tests for the versioned SQLite migrations and the migration runner.

Coverage: bootstrap table creation, full schema creation, version bookkeeping,
idempotence across restarts, atomicity of a failing migration (never marked as
applied, partial DDL rolled back), version validation and foreign-key pragma.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from architecture_assistant.infrastructure import (
    AUDIT_TABLE_NAME,
    BOOTSTRAP_SCHEMA_SQL,
    CHANGE_REQUEST_TABLE_NAME,
    MIGRATIONS,
    TABLE_NAMES,
    Migration,
    applied_versions,
    apply_migrations,
    close_database,
    open_database,
)


@pytest.fixture
def connection():
    conn = open_database(":memory:")
    yield conn
    close_database(conn)


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {row[0] for row in rows}


#: Every migration version, derived from the migration list itself so this stays
#: correct as new migrations are added.
EXPECTED_VERSIONS = tuple(migration.version for migration in MIGRATIONS)


def _noop(_connection: sqlite3.Connection) -> None:
    return None


def test_migration_list_is_versioned_and_ordered() -> None:
    versions = [migration.version for migration in MIGRATIONS]
    assert versions == sorted(set(versions))
    assert versions[0] == 1
    for migration in MIGRATIONS:
        assert migration.name
        assert callable(migration.up)


def test_bootstrap_sql_declares_version_as_primary_key() -> None:
    assert "PRIMARY KEY" in BOOTSTRAP_SCHEMA_SQL
    assert "version" in BOOTSTRAP_SCHEMA_SQL


def test_open_database_creates_all_source_of_truth_tables(connection) -> None:
    tables = _table_names(connection)
    assert set(TABLE_NAMES) <= tables
    assert "schema_migrations" in tables


def test_migration_version_one_is_applied(connection) -> None:
    assert applied_versions(connection) == EXPECTED_VERSIONS


def test_audit_trail_migration_creates_the_table(connection) -> None:
    assert AUDIT_TABLE_NAME in _table_names(connection)
    assert AUDIT_TABLE_NAME in TABLE_NAMES
    assert len(MIGRATIONS) >= 2
    assert MIGRATIONS[1].name == "audit_trail"


def test_change_request_migration_creates_the_table(connection) -> None:
    """Step 11 adds the persisted change-request lifecycle."""
    assert CHANGE_REQUEST_TABLE_NAME in _table_names(connection)
    assert CHANGE_REQUEST_TABLE_NAME in TABLE_NAMES
    assert MIGRATIONS[2].version == 3
    assert MIGRATIONS[2].name == "architecture_change_requests"
    columns = {
        row["name"]
        for row in connection.execute(
            f"PRAGMA table_info({CHANGE_REQUEST_TABLE_NAME})"
        ).fetchall()
    }
    assert columns == {
        "request_id",
        "title",
        "rationale",
        "source_version",
        "target_version",
        "rule_ids",
        "roadmap_impact",
        "status",
        "adr_id",
        "approved_by",
        "approved_at",
        "applied_at",
        "created_at",
    }
    indexes = {
        row["name"]
        for row in connection.execute(
            f"PRAGMA index_list({CHANGE_REQUEST_TABLE_NAME})"
        ).fetchall()
    }
    assert "idx_change_requests_status" in indexes


def test_the_change_request_table_is_empty_after_migrating(connection) -> None:
    """A migration never invents a change request; the engine owns that."""
    count = connection.execute(
        f"SELECT COUNT(*) FROM {CHANGE_REQUEST_TABLE_NAME}"
    ).fetchone()[0]
    assert count == 0


def test_schema_migrations_records_name_and_timestamp(connection) -> None:
    row = connection.execute(
        "SELECT version, name, applied_at FROM schema_migrations"
    ).fetchone()
    assert row["version"] == 1
    assert row["name"] == "initial_schema"
    assert row["applied_at"]


def test_version_is_unique_in_schema_migrations(connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) "
            "VALUES (1, 'duplicate', '2026-09-20T00:00:00+00:00')"
        )


def test_apply_migrations_is_idempotent(connection) -> None:
    before = applied_versions(connection)
    rows_before = connection.execute(
        "SELECT COUNT(*) FROM schema_migrations"
    ).fetchone()[0]
    assert apply_migrations(connection) == before
    assert apply_migrations(connection) == before
    rows_after = connection.execute(
        "SELECT COUNT(*) FROM schema_migrations"
    ).fetchone()[0]
    assert rows_before == rows_after == len(MIGRATIONS)


def test_bootstrap_runs_before_any_migration_is_registered() -> None:
    # an empty migration set still yields the bootstrap bookkeeping table
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        assert apply_migrations(connection, migrations=()) == ()
        assert "schema_migrations" in _table_names(connection)
    finally:
        connection.close()


def test_foreign_keys_pragma_is_enabled(connection) -> None:
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def _fresh_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def test_failing_migration_is_rolled_back_and_not_marked_applied() -> None:
    connection = _fresh_connection()
    try:

        def failing_up(conn: sqlite3.Connection) -> None:
            conn.execute("CREATE TABLE half_applied (id INTEGER PRIMARY KEY)")
            raise RuntimeError("migration boom")

        failing = (Migration(version=1, name="failing", up=failing_up),)
        with pytest.raises(RuntimeError, match="migration boom"):
            apply_migrations(connection, migrations=failing)

        tables = _table_names(connection)
        assert "half_applied" not in tables
        assert applied_versions(connection) == ()
        assert set(TABLE_NAMES).isdisjoint(tables)
    finally:
        connection.close()


def test_runner_can_recover_after_a_failed_migration() -> None:
    connection = _fresh_connection()
    try:

        def failing_up(conn: sqlite3.Connection) -> None:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            apply_migrations(
                connection,
                migrations=(Migration(1, "failing", failing_up),),
            )
        # the real migration set still applies cleanly afterwards
        assert apply_migrations(connection) == EXPECTED_VERSIONS
        assert set(TABLE_NAMES) <= _table_names(connection)
    finally:
        connection.close()


def test_pending_migration_failure_leaves_applied_versions_untouched(
    connection,
) -> None:
    def failing_up(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE half_applied (id INTEGER PRIMARY KEY)")
        raise RuntimeError("boom")

    applied_before = applied_versions(connection)
    # the real migrations (already applied -> skipped) plus one failing v99
    migrations = MIGRATIONS + (Migration(99, "failing", failing_up),)
    with pytest.raises(RuntimeError, match="boom"):
        apply_migrations(connection, migrations=migrations)

    assert applied_versions(connection) == applied_before
    assert "half_applied" not in _table_names(connection)


def test_duplicate_migration_versions_are_rejected(connection) -> None:
    duplicates = (
        Migration(1, "first", _noop),
        Migration(1, "second", _noop),
    )
    with pytest.raises(ValueError, match="duplicate migration version"):
        apply_migrations(connection, migrations=duplicates)


def test_out_of_order_migration_versions_are_rejected(connection) -> None:
    unordered = (
        Migration(2, "second", _noop),
        Migration(1, "first", _noop),
    )
    with pytest.raises(ValueError, match="strictly increasing"):
        apply_migrations(connection, migrations=unordered)


def test_file_database_creates_parent_directories(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "data" / "architecture_assistant.db"
    conn = open_database(target)
    try:
        assert target.exists()
        assert applied_versions(conn) == EXPECTED_VERSIONS
    finally:
        close_database(conn)


def test_reopening_a_file_database_does_not_reapply_migrations(
    tmp_path: Path,
) -> None:
    target = tmp_path / "architecture_assistant.db"
    first = open_database(target)
    try:
        rows_first = first.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]
    finally:
        close_database(first)

    second = open_database(target)
    try:
        rows_second = second.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]
        assert applied_versions(second) == EXPECTED_VERSIONS
    finally:
        close_database(second)
    assert rows_first == rows_second == len(MIGRATIONS)

