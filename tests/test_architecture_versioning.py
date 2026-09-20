"""Tests for the minimal versioned update of the architecture baseline.

Covers the mandated properties of the Step 9 architecture change: v1.0 is never
rewritten in place, v1.1 supersedes it, both records stay in history, the change
is idempotent, conflicts roll the whole transaction back, and the accompanying
ADR is recorded with its own provenance.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import architecture_assistant.application.architecture_versioning as versioning_module
from architecture_assistant.application import (
    COMPOSITION_ROOT_ADR,
    STEP_9_PROVENANCE,
    AdrSpec,
    ArchitectureVersioning,
    ArchitectureVersioningConflictError,
    ArchitectureVersioningError,
    BootstrapOutcome,
)
from architecture_assistant.architecture import (
    ARCHITECTURE_V1,
    ARCHITECTURE_V1_1,
)
from architecture_assistant.domain.enums import ADRStatus
from architecture_assistant.domain.models import ADR, ArchitectureVersion
from architecture_assistant.infrastructure import (
    SqliteStorage,
    close_database,
    open_database,
)

NOW = datetime(2026, 9, 20, 20, 0, tzinfo=timezone.utc)


def fixed_clock() -> datetime:
    return NOW


def canonical_v1() -> ArchitectureVersion:
    """The v1.0 record exactly as Step 8's bootstrap created it."""
    return ArchitectureVersion(
        version=ARCHITECTURE_V1.version,
        baseline=ARCHITECTURE_V1.description,
        rules=ARCHITECTURE_V1.rule_ids(),
        is_current=True,
        created_at=NOW,
    )


def canonical_v1_1() -> ArchitectureVersion:
    """Composition-side factory for the v1.1 baseline record."""
    return ArchitectureVersion(
        version=ARCHITECTURE_V1_1.version,
        baseline=ARCHITECTURE_V1_1.description,
        rules=ARCHITECTURE_V1_1.rule_ids(),
        is_current=True,
        created_at=NOW,
    )


@pytest.fixture
def connection():
    conn = open_database(":memory:")
    yield conn
    close_database(conn)


@pytest.fixture
def storage(connection) -> SqliteStorage:
    return SqliteStorage(connection)


@pytest.fixture
def versioning(storage) -> ArchitectureVersioning:
    return ArchitectureVersioning(storage, clock=fixed_clock)


@pytest.fixture
def seeded(storage) -> SqliteStorage:
    """A source of truth that already holds the authoritative v1.0 baseline."""
    storage.architecture_versions.upsert(canonical_v1())
    return storage


def current_versions(storage: SqliteStorage) -> tuple[ArchitectureVersion, ...]:
    return tuple(
        version
        for version in storage.architecture_versions.list()
        if version.is_current
    )


class TestIntroduce:
    def test_supersedes_the_predecessor(self, versioning, seeded) -> None:
        summary = versioning.introduce(canonical_v1_1(), supersedes="1.0")

        assert summary.version_outcome is BootstrapOutcome.CREATED
        assert summary.version == "1.1"
        assert summary.superseded == "1.0"
        assert summary.adrs_created == ("ADR-008",)

        old = seeded.architecture_versions.get("1.0")
        new = seeded.architecture_versions.get("1.1")
        assert old is not None and new is not None
        assert old.is_current is False
        assert old.superseded_by == "1.1"
        assert new.is_current is True
        assert new.superseded_by is None

    def test_v1_is_never_rewritten_in_place(self, versioning, seeded) -> None:
        before = seeded.architecture_versions.get("1.0")
        versioning.introduce(canonical_v1_1(), supersedes="1.0")
        after = seeded.architecture_versions.get("1.0")

        assert after is not None and before is not None
        assert after.baseline == before.baseline
        assert after.rules == before.rules
        assert after.created_at == before.created_at
        assert after.version == before.version
        # Only the lifecycle fields moved - the record itself is history.
        assert (after.is_current, after.superseded_by) == (False, "1.1")

    def test_exactly_one_current_baseline(self, versioning, seeded) -> None:
        versioning.introduce(canonical_v1_1(), supersedes="1.0")
        current = current_versions(seeded)
        assert tuple(version.version for version in current) == ("1.1",)

    def test_both_versions_stay_in_history(self, versioning, seeded) -> None:
        versioning.introduce(canonical_v1_1(), supersedes="1.0")
        assert tuple(
            version.version for version in seeded.architecture_versions.list()
        ) == ("1.0", "1.1")

    def test_the_change_is_idempotent(self, versioning, seeded) -> None:
        versioning.introduce(canonical_v1_1(), supersedes="1.0")
        audit_after_first = len(seeded.audit.list())

        summary = versioning.introduce(canonical_v1_1(), supersedes="1.0")

        assert summary.version_outcome is BootstrapOutcome.ALREADY_PRESENT
        assert summary.adrs_already_present == ("ADR-008",)
        assert summary.adrs_created == ()
        assert len(seeded.audit.list()) == audit_after_first
        assert len(seeded.architecture_versions.list()) == 2

    def test_a_first_baseline_needs_no_predecessor(
        self, versioning, storage
    ) -> None:
        summary = versioning.introduce(canonical_v1())
        assert summary.version_outcome is BootstrapOutcome.CREATED
        assert summary.superseded is None
        assert current_versions(storage)[0].version == "1.0"

    def test_summary_is_json_safe(self, versioning, seeded) -> None:
        summary = versioning.introduce(canonical_v1_1(), supersedes="1.0")
        assert json.loads(json.dumps(summary.to_dict())) == summary.to_dict()


class TestAccompanyingAdr:
    def test_adr_is_created_and_accepted(self, versioning, seeded) -> None:
        versioning.introduce(canonical_v1_1(), supersedes="1.0")
        adr = seeded.adrs.get(COMPOSITION_ROOT_ADR.id)
        assert adr is not None
        assert adr.status is ADRStatus.ACCEPTED
        assert adr.title == COMPOSITION_ROOT_ADR.title
        assert adr.context.startswith(STEP_9_PROVENANCE)
        assert adr.related == ("ADR-004", "ADR-005")

    def test_adr_records_the_decision_reason(self, versioning, seeded) -> None:
        versioning.introduce(canonical_v1_1(), supersedes="1.0")
        adr = seeded.adrs.get(COMPOSITION_ROOT_ADR.id)
        assert adr is not None
        assert "composition" in adr.decision
        assert "No existing layer gains any permission" in adr.decision

    def test_a_differing_adr_is_a_conflict_and_rolls_back(
        self, versioning, seeded
    ) -> None:
        seeded.adrs.upsert(
            ADR(
                id=COMPOSITION_ROOT_ADR.id,
                title="something else entirely",
                status=ADRStatus.ACCEPTED,
                context="other",
                decision="other",
                created_at=NOW,
            )
        )
        with pytest.raises(ArchitectureVersioningConflictError, match="differs"):
            versioning.introduce(canonical_v1_1(), supersedes="1.0")

        assert seeded.architecture_versions.get("1.1") is None
        assert seeded.architecture_versions.get("1.0").is_current is True

    def test_a_custom_adr_spec_is_used_verbatim(self, storage) -> None:
        spec = AdrSpec(
            id="ADR-900",
            title="a custom change",
            rationale="because",
            decision="do it",
            provenance="A later, explicit decision.",
        )
        versioning = ArchitectureVersioning(
            storage, clock=fixed_clock, adr_specs=(spec,)
        )
        storage.architecture_versions.upsert(canonical_v1())
        summary = versioning.introduce(canonical_v1_1(), supersedes="1.0")
        assert summary.adrs_created == ("ADR-900",)
        stored = storage.adrs.get("ADR-900")
        assert stored is not None
        assert stored.context.startswith("A later, explicit decision.")


class TestConflicts:
    def test_differing_stored_version_is_a_conflict(
        self, versioning, seeded
    ) -> None:
        seeded.architecture_versions.upsert(
            ArchitectureVersion(
                version="1.1",
                baseline="a different baseline",
                rules=("a-different-rule",),
                is_current=True,
                created_at=NOW,
            )
        )
        with pytest.raises(
            ArchitectureVersioningConflictError,
            match="differs from the canonical baseline",
        ):
            versioning.introduce(canonical_v1_1(), supersedes="1.0")

    def test_missing_predecessor_is_a_conflict(
        self, versioning, storage
    ) -> None:
        with pytest.raises(
            ArchitectureVersioningConflictError, match="does not exist"
        ):
            versioning.introduce(canonical_v1_1(), supersedes="1.0")
        assert storage.architecture_versions.list() == ()

    def test_already_superseded_predecessor_is_a_conflict(
        self, versioning, storage
    ) -> None:
        storage.architecture_versions.upsert(
            ArchitectureVersion(
                version="1.0",
                baseline=ARCHITECTURE_V1.description,
                rules=ARCHITECTURE_V1.rule_ids(),
                superseded_by="1.2",
                is_current=False,
                created_at=NOW,
            )
        )
        storage.architecture_versions.upsert(
            ArchitectureVersion(
                version="1.2",
                baseline="another baseline",
                rules=("a-different-rule",),
                is_current=True,
                created_at=NOW,
            )
        )
        with pytest.raises(
            ArchitectureVersioningConflictError, match="already superseded"
        ):
            versioning.introduce(canonical_v1_1(), supersedes="1.0")

    def test_a_non_current_predecessor_is_a_conflict(
        self, versioning, seeded
    ) -> None:
        seeded.architecture_versions.upsert(
            ArchitectureVersion(
                version="0.9",
                baseline="an older baseline",
                rules=("a-different-rule",),
                is_current=False,
                created_at=NOW,
            )
        )
        with pytest.raises(
            ArchitectureVersioningConflictError, match="not the"
        ):
            versioning.introduce(canonical_v1_1(), supersedes="0.9")

    def test_unnamed_predecessor_is_a_conflict(
        self, versioning, seeded
    ) -> None:
        """Never displace a current baseline without naming it."""
        with pytest.raises(
            ArchitectureVersioningConflictError, match="still current"
        ):
            versioning.introduce(canonical_v1_1())

    def test_inconsistent_history_is_a_conflict(
        self, versioning, seeded
    ) -> None:
        """The new version exists, but is not recorded as its successor."""
        seeded.architecture_versions.upsert(canonical_v1_1())
        with pytest.raises(
            ArchitectureVersioningConflictError,
            match="not recorded as superseded",
        ):
            versioning.introduce(canonical_v1_1(), supersedes="1.0")

    def test_missing_recorded_predecessor_is_a_conflict(
        self, versioning, seeded
    ) -> None:
        """v1.1 is stored as current, but the v1.0 it supersedes is gone."""
        seeded.architecture_versions.delete("1.0")
        seeded.architecture_versions.upsert(canonical_v1_1())
        with pytest.raises(
            ArchitectureVersioningConflictError, match="predecessor"
        ):
            versioning.introduce(canonical_v1_1(), supersedes="1.0")


class TestValidation:
    def test_spec_must_be_an_architecture_version(
        self, versioning, seeded
    ) -> None:
        with pytest.raises(ValueError, match="ArchitectureVersion"):
            versioning.introduce("1.1", supersedes="1.0")  # type: ignore[arg-type]

    def test_spec_must_be_current(self, versioning, seeded) -> None:
        historic = ArchitectureVersion(
            version="1.1",
            baseline=ARCHITECTURE_V1_1.description,
            rules=ARCHITECTURE_V1_1.rule_ids(),
            is_current=False,
            created_at=NOW,
        )
        with pytest.raises(ValueError, match="is_current=True"):
            versioning.introduce(historic, supersedes="1.0")

    def test_a_baseline_cannot_supersede_itself(
        self, versioning, seeded
    ) -> None:
        with pytest.raises(ValueError, match="cannot supersede itself"):
            versioning.introduce(canonical_v1_1(), supersedes="1.1")

    def test_clock_must_be_callable(self, storage) -> None:
        with pytest.raises(ValueError, match="clock must be a callable"):
            ArchitectureVersioning(storage, clock="nope")  # type: ignore[arg-type]

    def test_errors_share_a_versioning_base(self) -> None:
        assert issubclass(
            ArchitectureVersioningConflictError, ArchitectureVersioningError
        )


class TestLayerPurity:
    def test_module_does_not_import_the_architecture_layer(self) -> None:
        source = versioning_module.__file__ or ""
        text = open(source, encoding="utf-8").read()
        assert "architecture_assistant.architecture" not in text
        assert "from ..architecture" not in text

    def test_module_has_no_hardcoded_rule_ids(self) -> None:
        text = open(versioning_module.__file__ or "", encoding="utf-8").read()
        for rule_id in ARCHITECTURE_V1.rule_ids():
            assert rule_id not in text
