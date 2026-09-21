"""Offline tests for the Step 17 SQLite cost plugin.

Everything here runs against a real SQLite database (in memory, or a temporary
file for the restart tests) - there is no network and no provider adapter
involved. Coverage follows the mandated properties:

* one stable ``event_id`` per logical billable provider event, so ``record()``
  is idempotent: a replay is a no-op, a re-used identity with different content
  fails closed, and two genuinely different events stay two rows;
* the transaction ownership rule is exactly the repository rule, so a
  rolled-back outer transaction leaves zero cost rows and a standalone
  ``record()`` persists;
* ``pricing_known`` keeps an unknown price distinguishable from a real
  zero-dollar price, and ``CostSummary`` never hides one behind the other;
* SQL filtering and ``CostQuery.matches`` agree;
* data survives a restart, filters included;
* failures stay visible (the plugin never swallows them) while remaining
  non-fatal to the caller's telemetry handling.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from architecture_assistant.architecture import (
    ARCHITECTURE_CURRENT,
    ROOT_PACKAGE,
    ArchitectureValidator,
    layer_of,
    scan_directory,
)
from architecture_assistant.domain.enums import Mode
from architecture_assistant.domain.models import Project
from architecture_assistant.infrastructure import (
    COST_TABLE_NAME,
    MIGRATIONS,
    TABLE_NAMES,
    SqliteCostPlugin,
    SqliteStorage,
    applied_versions,
    close_database,
    open_database,
)
from architecture_assistant.ports.capabilities import (
    CostIdentityConflictError,
    CostPort,
    CostQuery,
    CostRecord,
    CostSummary,
)

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(hours=4)
PROJECT = "Architecture Lifecycle Assistant"

#: The billable content of one logical event - used to build a second event that
#: is identical in every accounting field and differs only by identity.
IDENTICAL_CONTENT = {
    "provider": "openai",
    "model": "gpt-5.6",
    "input_tokens": 120,
    "output_tokens": 30,
    "cost_usd": 0.18,
    "pricing_known": True,
    "project": PROJECT,
    "step_no": 17,
    "created_at": NOW,
}


def make_record(**overrides) -> CostRecord:
    payload = {"event_id": "openai:chatcmpl-1", **IDENTICAL_CONTENT}
    payload.update(overrides)
    return CostRecord(**payload)


def row_count(connection: sqlite3.Connection) -> int:
    rows = connection.execute(
        f"SELECT COUNT(*) FROM {COST_TABLE_NAME}"
    ).fetchone()
    return int(rows[0])


def make_project() -> Project:
    return Project(name=PROJECT, plan_version="0.3", mode=Mode.MANUAL)


def seeded_records() -> tuple[CostRecord, ...]:
    """Five events spanning two projects, three providers, two steps and a window."""
    return (
        make_record(
            event_id="e1",
            provider="openai",
            project="A",
            step_no=17,
            input_tokens=10,
            output_tokens=2,
            cost_usd=0.1,
            pricing_known=True,
            created_at=NOW,
        ),
        make_record(
            event_id="e2",
            provider="claude",
            project="A",
            step_no=17,
            input_tokens=20,
            output_tokens=4,
            cost_usd=0.2,
            pricing_known=True,
            created_at=NOW,
        ),
        make_record(
            event_id="e3",
            provider="grok",
            project="B",
            step_no=18,
            input_tokens=30,
            output_tokens=6,
            cost_usd=0.4,
            pricing_known=True,
            created_at=LATER,
        ),
        make_record(
            event_id="e4",
            provider="openai",
            project="B",
            step_no=18,
            input_tokens=40,
            output_tokens=8,
            cost_usd=0.0,
            pricing_known=False,
            created_at=LATER,
        ),
        make_record(
            event_id="e5",
            provider="openai",
            project="A",
            step_no=18,
            input_tokens=50,
            output_tokens=10,
            cost_usd=0.5,
            pricing_known=True,
            created_at=NOW - timedelta(days=1),
        ),
    )


@pytest.fixture
def connection():
    conn = open_database(":memory:")
    yield conn
    close_database(conn)


@pytest.fixture
def plugin(connection) -> SqliteCostPlugin:
    return SqliteCostPlugin(connection)


@pytest.fixture
def storage(connection) -> SqliteStorage:
    return SqliteStorage(connection)


def _src_root() -> Path:
    return Path(__file__).resolve().parents[1] / "src" / "architecture_assistant"


class TestPortConformance:
    """The plugin is the concrete provider-neutral cost capability."""

    def test_the_plugin_is_a_cost_port(self, plugin) -> None:
        assert isinstance(plugin, CostPort)

    def test_the_plugin_decides_nothing(self, plugin) -> None:
        for name in ("vote", "rank", "decide", "advise", "judge"):
            assert not hasattr(plugin, name)

    def test_record_rejects_a_non_record(self, plugin) -> None:
        with pytest.raises(ValueError, match="must be a CostRecord"):
            plugin.record({"provider": "openai"})

    def test_query_rejects_a_non_query(self, plugin) -> None:
        with pytest.raises(ValueError, match="must be a CostQuery"):
            plugin.query({"provider": "openai"})


class TestMigration:
    """Migration v4 creates the idempotent accounting table."""

    def test_the_table_exists_and_is_declared(self, connection) -> None:
        names = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

        assert COST_TABLE_NAME in names
        assert COST_TABLE_NAME in TABLE_NAMES

    def test_the_migration_is_versioned_and_idempotent(self, connection) -> None:
        migration = MIGRATIONS[-1]

        assert migration.version == 4
        assert migration.name == "cost_records"
        assert applied_versions(connection) == tuple(
            item.version for item in MIGRATIONS
        )
        # re-applying changes nothing (the runner is idempotent)
        from architecture_assistant.infrastructure import apply_migrations

        assert apply_migrations(connection) == tuple(
            item.version for item in MIGRATIONS
        )

    def test_the_event_identity_is_unique_at_the_database_level(
        self, connection, plugin
    ) -> None:
        plugin.record(make_record())

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"INSERT INTO {COST_TABLE_NAME} "
                "(event_id, provider, created_at) VALUES (?, ?, ?)",
                ("openai:chatcmpl-1", "openai", NOW.isoformat()),
            )

    def test_the_migration_can_be_reversed(self, connection) -> None:
        MIGRATIONS[-1].down(connection)

        names = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert COST_TABLE_NAME not in names

    def test_a_cost_record_needs_no_step_row(self, connection, plugin) -> None:
        """Telemetry is not domain state: no foreign key, no speculative link."""
        plugin.record(make_record(step_no=999))

        assert row_count(connection) == 1
        assert plugin.query(CostQuery(step_no=999)).record_count == 1


class TestIdempotence:
    """One ``event_id`` identifies one logical billable provider event."""

    def test_the_same_event_twice_stores_exactly_one_row(
        self, connection, plugin
    ) -> None:
        plugin.record(make_record())
        plugin.record(make_record())

        assert row_count(connection) == 1
        assert plugin.query(CostQuery()).record_count == 1

    def test_a_replay_with_a_fresh_clock_is_still_a_no_op(
        self, connection, plugin
    ) -> None:
        """The clock is not part of the identity, so a replay keeps its row."""
        plugin.record(make_record(created_at=NOW))
        plugin.record(make_record(created_at=LATER))

        assert row_count(connection) == 1
        # the original timestamp survived: the replay did not rewrite the row
        assert plugin.query(CostQuery(from_time=NOW)).record_count == 1
        assert plugin.query(CostQuery(from_time=LATER)).record_count == 0

    def test_a_re_used_identity_with_different_content_fails_closed(
        self, connection, plugin
    ) -> None:
        plugin.record(make_record(cost_usd=0.18))

        with pytest.raises(
            CostIdentityConflictError, match="different accounting content"
        ):
            plugin.record(make_record(cost_usd=0.99))

        # nothing was overwritten and nothing was added
        assert row_count(connection) == 1
        assert plugin.query(CostQuery()).total_usd == 0.18

    @pytest.mark.parametrize(
        "field_name, value",
        [
            ("provider", "claude"),
            ("model", "other-model"),
            ("input_tokens", 1),
            ("output_tokens", 1),
            ("cost_usd", 0.0),
            ("pricing_known", False),
            ("project", "Other Project"),
            ("step_no", 18),
        ],
    )
    def test_every_accounting_field_participates_in_the_conflict(
        self, plugin, field_name, value
    ) -> None:
        plugin.record(make_record())

        with pytest.raises(CostIdentityConflictError):
            plugin.record(make_record(**{field_name: value}))

    def test_two_genuine_events_that_look_alike_are_two_rows(
        self, connection, plugin
    ) -> None:
        """Different event ids -> two billable events, however identical the rest."""
        plugin.record(make_record(event_id="openai:chatcmpl-1"))
        plugin.record(make_record(event_id="openai:chatcmpl-2"))

        assert row_count(connection) == 2
        summary = plugin.query(CostQuery())
        assert summary.record_count == 2
        assert summary.total_usd == 0.36
        assert summary.priced_record_count == 2

    def test_an_accidental_double_call_adds_nothing(
        self, connection, plugin
    ) -> None:
        record = make_record()

        for _ in range(5):
            plugin.record(record)

        assert row_count(connection) == 1


class TestAccountingSemantics:
    """Unknown price is not free, and the numbers are stored verbatim."""

    def test_a_priced_record_round_trips(self, plugin) -> None:
        plugin.record(make_record())

        summary = plugin.query(CostQuery())

        assert summary.total_usd == 0.18
        assert summary.input_tokens == 120
        assert summary.output_tokens == 30
        assert summary.record_count == 1
        assert summary.priced_record_count == 1
        assert summary.unpriced_record_count == 0

    def test_an_unknown_price_is_not_a_real_zero_price(self, plugin) -> None:
        plugin.record(
            make_record(
                event_id="openai:chatcmpl-1", cost_usd=0.0, pricing_known=False
            )
        )
        plugin.record(
            make_record(
                event_id="openai:chatcmpl-2", cost_usd=0.0, pricing_known=True
            )
        )

        summary = plugin.query(CostQuery())

        assert summary.total_usd == 0.0
        assert summary.record_count == 2
        assert summary.priced_record_count == 1
        assert summary.unpriced_record_count == 1

    def test_an_unpriced_record_is_never_presented_as_a_known_zero_cost(
        self, plugin
    ) -> None:
        plugin.record(
            make_record(cost_usd=0.0, pricing_known=False, step_no=17)
        )

        payload = plugin.query(CostQuery()).to_dict()

        assert payload["total_usd"] == 0.0
        assert payload["priced_record_count"] == 0
        assert payload["unpriced_record_count"] == 1

    def test_the_plugin_never_invents_a_price(self, plugin) -> None:
        plugin.record(
            make_record(
                event_id="openai:chatcmpl-9",
                cost_usd=0.0,
                pricing_known=False,
            )
        )

        assert plugin.query(CostQuery()).total_usd == 0.0

    def test_a_known_zero_and_an_unknown_zero_stay_distinguishable(
        self, plugin
    ) -> None:
        plugin.record(
            make_record(event_id="a", cost_usd=0.0, pricing_known=True)
        )
        plugin.record(
            make_record(event_id="b", cost_usd=0.0, pricing_known=False)
        )

        summary = plugin.query(CostQuery(provider="openai"))

        assert summary.record_count == 2
        assert summary.priced_record_count == 1
        assert summary.unpriced_record_count == 1


class TestTransactionOwnership:
    """The plugin obeys the same transaction rule as every other adapter."""

    def test_a_standalone_record_persists(self, connection, plugin) -> None:
        plugin.record(make_record())

        assert row_count(connection) == 1

    def test_a_rolled_back_outer_transaction_leaves_no_cost_row(
        self, connection, storage, plugin
    ) -> None:
        with pytest.raises(RuntimeError):
            with storage.transaction():
                plugin.record(make_record())
                raise RuntimeError("the step failed")

        assert row_count(connection) == 0
        assert plugin.query(CostQuery()).record_count == 0

    def test_a_committed_outer_transaction_persists_the_cost_row(
        self, connection, storage, plugin
    ) -> None:
        with storage.transaction():
            plugin.record(make_record())

        assert row_count(connection) == 1

    def test_domain_state_and_cost_roll_back_together(
        self, connection, storage, plugin
    ) -> None:
        with pytest.raises(RuntimeError):
            with storage.transaction():
                storage.projects.upsert(make_project())
                plugin.record(make_record())
                raise RuntimeError("the step failed")

        assert storage.projects.get(PROJECT) is None
        assert row_count(connection) == 0

    def test_a_record_inside_an_open_transaction_is_not_committed_early(
        self, connection, storage, plugin
    ) -> None:
        with storage.transaction():
            plugin.record(make_record())
            # the outer boundary still owns the write: nothing is committed yet
            assert storage.transaction_port.in_transaction is True

        assert row_count(connection) == 1


class TestQuery:
    """Filters narrow in SQL and agree with the canonical predicate."""

    def test_an_empty_table_reports_zeroes(self, plugin) -> None:
        summary = plugin.query(CostQuery())

        assert summary == CostSummary()
        assert summary.record_count == 0
        assert summary.unpriced_record_count == 0

    def test_the_summary_aggregates_every_record(self, plugin) -> None:
        plugin.record(
            make_record(
                event_id="a", input_tokens=10, output_tokens=2, cost_usd=0.1
            )
        )
        plugin.record(
            make_record(
                event_id="b", input_tokens=20, output_tokens=4, cost_usd=0.2
            )
        )

        summary = plugin.query(CostQuery())

        assert summary.record_count == 2
        assert summary.input_tokens == 30
        assert summary.output_tokens == 6
        assert summary.total_usd == 0.3

    @pytest.mark.parametrize(
        "cost_filter",
        [
            CostQuery(),
            CostQuery(project="A"),
            CostQuery(project="B"),
            CostQuery(step_no=17),
            CostQuery(step_no=18),
            CostQuery(provider="openai"),
            CostQuery(provider="claude"),
            CostQuery(from_time=NOW),
            CostQuery(to_time=NOW),
            CostQuery(from_time=NOW, to_time=LATER),
            CostQuery(project="A", step_no=18, provider="openai"),
            CostQuery(
                project="A",
                step_no=17,
                provider="claude",
                from_time=NOW,
                to_time=LATER,
            ),
        ],
    )
    def test_sql_filtering_equals_the_canonical_predicate(
        self, plugin, cost_filter
    ) -> None:
        records = seeded_records()
        for record in records:
            plugin.record(record)

        expected = [record for record in records if cost_filter.matches(record)]
        summary = plugin.query(cost_filter)

        assert summary.record_count == len(expected)
        assert summary.input_tokens == sum(r.input_tokens for r in expected)
        assert summary.output_tokens == sum(r.output_tokens for r in expected)
        assert summary.priced_record_count == sum(
            1 for r in expected if r.pricing_known
        )
        assert summary.unpriced_record_count == sum(
            1 for r in expected if not r.pricing_known
        )
        assert summary.total_usd == round(
            sum(r.cost_usd for r in expected), 6
        )

    def test_filters_combine_with_and(self, plugin) -> None:
        for record in seeded_records():
            plugin.record(record)

        assert plugin.query(CostQuery(project="A")).record_count == 3
        assert plugin.query(CostQuery(project="A", step_no=18)).record_count == 1
        assert (
            plugin.query(CostQuery(project="A", step_no=18, provider="openai"))
            .record_count
            == 1
        )
        assert (
            plugin.query(CostQuery(project="A", provider="claude")).record_count
            == 1
        )
        assert plugin.query(CostQuery(provider="grok")).record_count == 1

    def test_a_window_selects_only_that_window(self, plugin) -> None:
        for record in seeded_records():
            plugin.record(record)

        assert plugin.query(CostQuery(from_time=LATER)).record_count == 2
        assert plugin.query(CostQuery(to_time=NOW)).record_count == 3
        assert (
            plugin.query(CostQuery(from_time=NOW, to_time=NOW)).record_count == 2
        )

    def test_an_unmatched_filter_reports_a_zero_summary(self, plugin) -> None:
        plugin.record(make_record())

        summary = plugin.query(CostQuery(project="No such project"))

        assert summary == CostSummary()

    def test_the_summary_is_json_safe(self, plugin) -> None:
        for record in seeded_records():
            plugin.record(record)

        payload = plugin.query(CostQuery()).to_dict()

        assert json.loads(json.dumps(payload)) == payload
        assert payload["record_count"] == 5
        assert payload["unpriced_record_count"] == 1


class TestPersistenceAcrossRestart:
    """The database is the source of truth, so a restart loses nothing."""

    def test_records_and_filters_survive_a_restart(self, tmp_path) -> None:
        path = tmp_path / "cost.db"

        first = open_database(path)
        try:
            plugin = SqliteCostPlugin(first)
            for record in seeded_records():
                plugin.record(record)
            expected = plugin.query(CostQuery())
        finally:
            close_database(first)

        second = open_database(path)
        try:
            reopened = SqliteCostPlugin(second)

            assert reopened.query(CostQuery()) == expected
            assert reopened.query(CostQuery(project="A")).record_count == 3
            assert reopened.query(CostQuery(provider="openai")).record_count == 3
            assert reopened.query(CostQuery(step_no=18)).record_count == 3
            assert reopened.query(CostQuery(from_time=LATER)).record_count == 2
        finally:
            close_database(second)

    def test_a_replay_after_a_restart_adds_no_duplicate(self, tmp_path) -> None:
        path = tmp_path / "cost.db"
        record = make_record()

        first = open_database(path)
        try:
            SqliteCostPlugin(first).record(record)
        finally:
            close_database(first)

        second = open_database(path)
        try:
            plugin = SqliteCostPlugin(second)
            plugin.record(record)
            plugin.record(make_record(created_at=LATER))

            assert plugin.query(CostQuery()).record_count == 1
        finally:
            close_database(second)

    def test_a_conflict_after_a_restart_still_fails_closed(
        self, tmp_path
    ) -> None:
        path = tmp_path / "cost.db"

        first = open_database(path)
        try:
            SqliteCostPlugin(first).record(make_record())
        finally:
            close_database(first)

        second = open_database(path)
        try:
            with pytest.raises(CostIdentityConflictError):
                SqliteCostPlugin(second).record(make_record(cost_usd=9.99))

            assert SqliteCostPlugin(second).query(CostQuery()).record_count == 1
        finally:
            close_database(second)


class TestFailClosedVisibility:
    """The plugin never swallows a defect - the caller's telemetry sees it."""

    def test_a_conflict_propagates_out_of_record(self, plugin) -> None:
        plugin.record(make_record())

        with pytest.raises(CostIdentityConflictError):
            plugin.record(make_record(cost_usd=1.0))

    def test_a_database_failure_propagates(self, plugin, connection) -> None:
        connection.execute(f"DROP TABLE {COST_TABLE_NAME}")

        with pytest.raises(sqlite3.Error):
            plugin.record(make_record())

    def test_a_query_failure_propagates(self, plugin, connection) -> None:
        connection.execute(f"DROP TABLE {COST_TABLE_NAME}")

        with pytest.raises(sqlite3.Error):
            plugin.query(CostQuery())

    def test_every_write_statement_is_a_plain_insert(self) -> None:
        """No destructive or silently-ignoring upsert anywhere in the plugin."""
        statements = [
            value
            for name, value in vars(SqliteCostPlugin).items()
            if isinstance(value, str) and "INSERT" in value
        ]

        assert statements, "the plugin must own its INSERT statement"
        for statement in statements:
            assert "OR REPLACE" not in statement
            assert "OR IGNORE" not in statement
            assert "ON CONFLICT" not in statement
            assert statement.strip().startswith("INSERT INTO")

    def test_the_plugin_never_generates_its_own_identity(self) -> None:
        """An invented identity would destroy replay idempotence."""
        text = (_src_root() / "infrastructure" / "cost.py").read_text(
            encoding="utf-8"
        )

        for generator in ("uuid", "token_hex", "urandom", "random"):
            assert generator not in text, generator


class TestLayerBoundary:
    """The cost plugin is infrastructure and knows no provider adapter."""

    def test_the_plugin_imports_no_provider_adapter(self) -> None:
        text = (_src_root() / "infrastructure" / "cost.py").read_text(
            encoding="utf-8"
        )

        for forbidden in (
            "from .openai",
            "from .claude",
            "from .grok",
            "from ._http",
        ):
            assert forbidden not in text, forbidden

    def test_the_plugin_never_imports_outwards(self) -> None:
        source = scan_directory(_src_root())

        for target in source.imports_of(f"{ROOT_PACKAGE}.infrastructure.cost"):
            assert layer_of(target) not in (
                "architecture",
                "composition",
                "application",
            )

    def test_no_provider_adapter_writes_sqlite_itself(self) -> None:
        """Adapters report through the port - never through sqlite3 or the plugin."""
        source = scan_directory(_src_root())

        for module in ("openai", "claude", "grok", "openai_judge"):
            targets = source.imports_of(f"{ROOT_PACKAGE}.infrastructure.{module}")

            assert "sqlite3" not in targets, module
            assert f"{ROOT_PACKAGE}.infrastructure.cost" not in targets, module

    def test_the_whole_tree_still_satisfies_the_current_baseline(self) -> None:
        source = scan_directory(_src_root())
        result = ArchitectureValidator().validate(source)

        assert result.is_compliant is True
        assert result.violations == ()
        assert result.baseline_version == ARCHITECTURE_CURRENT.version
