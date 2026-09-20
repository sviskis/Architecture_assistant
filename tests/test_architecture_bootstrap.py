"""Tests for the architecture bootstrap reconciliation.

Covers the mandated properties: the canonical baseline is injected (never
duplicated here), every item resolves to created / already-present / conflict,
conflicts roll the whole transaction back, live project runtime state is never
reset, and the seeded state reconciles with the ContextBuilder and the realized
code.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest

import architecture_assistant.application.architecture_bootstrap as bootstrap_module
from architecture_assistant.application import (
    BOOTSTRAP_ADRS,
    BOOTSTRAP_RISKS,
    PROJECT_NAME,
    PROJECT_PLAN_VERSION,
    RETROSPECTIVE_NOTE,
    ArchitectureBootstrap,
    BootstrapConflictError,
    BootstrapError,
    BootstrapOutcome,
    ContextBuilder,
    ProjectNotFoundError,
)
from architecture_assistant.architecture import (
    ARCHITECTURE_V1,
    ArchitectureValidator,
    scan_directory,
)
from architecture_assistant.domain.audit import AuditAction
from architecture_assistant.domain.enums import (
    ADRStatus,
    Mode,
    Phase,
    RiskStatus,
    Severity,
    StepState,
)
from architecture_assistant.domain.models import (
    ADR,
    ArchitectureVersion,
    Project,
    Risk,
    Step,
)
from architecture_assistant.infrastructure import (
    SqliteStorage,
    close_database,
    open_database,
)

NOW = datetime(2026, 9, 20, 20, 0, tzinfo=timezone.utc)
ADR_IDS = tuple(spec.id for spec in BOOTSTRAP_ADRS)
RISK_IDS = tuple(spec.id for spec in BOOTSTRAP_RISKS)


def fixed_clock() -> datetime:
    return NOW


def canonical_version() -> ArchitectureVersion:
    """Composition-side factory: derive the spec from the code baseline.

    This is exactly where the canonical rule identifiers live, so the
    application layer never duplicates them.
    """
    return ArchitectureVersion(
        version=ARCHITECTURE_V1.version,
        baseline=ARCHITECTURE_V1.description,
        rules=ARCHITECTURE_V1.rule_ids(),
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
def bootstrap(storage) -> ArchitectureBootstrap:
    return ArchitectureBootstrap(storage, canonical_version, clock=fixed_clock)


def risk_like(spec, **overrides) -> Risk:
    """Build a Risk identical to a bootstrap spec, with optional deviations."""
    data = dict(
        id=spec.id,
        description=spec.description,
        severity=spec.severity,
        probability=spec.probability,
        impact=spec.impact,
        owner=spec.owner,
        mitigation=spec.mitigation,
        status=RiskStatus.OPEN,
        created_at=NOW,
    )
    data.update(overrides)
    return Risk(**data)


def adr_like(spec, **overrides) -> ADR:
    """Build an ADR identical to a bootstrap spec, with optional deviations."""
    data = dict(
        id=spec.id,
        title=spec.title,
        status=ADRStatus.ACCEPTED,
        context=spec.context,
        decision=spec.decision,
        consequences=tuple(spec.consequences),
        related=tuple(spec.related),
        created_at=NOW,
        updated_at=NOW,
    )
    data.update(overrides)
    return ADR(**data)


class TestSeedCreates:
    def test_creates_the_project_and_baseline(self, bootstrap, storage) -> None:
        summary = bootstrap.seed()

        assert summary.project is BootstrapOutcome.CREATED
        assert summary.architecture_version is BootstrapOutcome.CREATED

        project = storage.projects.get(PROJECT_NAME)
        assert project is not None
        assert project.plan_version == PROJECT_PLAN_VERSION

        version = storage.architecture_versions.get(ARCHITECTURE_V1.version)
        assert version is not None
        assert version.is_current is True
        assert version.baseline == ARCHITECTURE_V1.description

    def test_creates_every_adr_as_accepted(self, bootstrap, storage) -> None:
        summary = bootstrap.seed()
        assert summary.adrs_created == ADR_IDS

        for spec in BOOTSTRAP_ADRS:
            adr = storage.adrs.get(spec.id)
            assert adr is not None
            assert adr.status is ADRStatus.ACCEPTED
            assert adr.decision == spec.decision
            assert adr.version == 1

    def test_creates_every_risk_as_open(self, bootstrap, storage) -> None:
        summary = bootstrap.seed()
        assert summary.risks_created == RISK_IDS

        for spec in BOOTSTRAP_RISKS:
            risk = storage.risks.get(spec.id)
            assert risk is not None
            assert risk.status is RiskStatus.OPEN
            assert risk.severity is spec.severity
            assert risk.impact is spec.impact
            assert risk.owner == spec.owner
            assert risk.mitigation == spec.mitigation

    def test_summary_counts(self, bootstrap) -> None:
        summary = bootstrap.seed()
        assert summary.created_count == 2 + len(BOOTSTRAP_ADRS) + len(
            BOOTSTRAP_RISKS
        )

    def test_writes_the_audit_trail(self, bootstrap, storage) -> None:
        bootstrap.seed()
        entries = storage.audit.list()
        assert len(entries) == 2 * len(BOOTSTRAP_ADRS) + len(BOOTSTRAP_RISKS)

        actions = Counter(entry.action for entry in entries)
        assert actions[AuditAction.CREATE] == len(BOOTSTRAP_ADRS) + len(
            BOOTSTRAP_RISKS
        )
        assert actions[AuditAction.ACCEPT] == len(BOOTSTRAP_ADRS)

    def test_summary_to_dict_is_json_safe(self, bootstrap) -> None:
        payload = bootstrap.seed().to_dict()
        assert json.loads(json.dumps(payload)) == payload
        assert payload["project"] == "created"


class TestIdempotence:
    def test_second_seed_creates_nothing(self, bootstrap, storage) -> None:
        bootstrap.seed()
        summary = bootstrap.seed()

        assert summary.created_count == 0
        assert summary.project is BootstrapOutcome.ALREADY_PRESENT
        assert summary.architecture_version is BootstrapOutcome.ALREADY_PRESENT
        assert summary.adrs_already_present == ADR_IDS
        assert summary.risks_already_present == RISK_IDS

    def test_second_seed_adds_no_records(self, bootstrap, storage) -> None:
        bootstrap.seed()
        bootstrap.seed()

        assert len(storage.projects.list()) == 1
        assert len(storage.architecture_versions.list()) == 1
        assert len(storage.adrs.list()) == len(BOOTSTRAP_ADRS)
        assert len(storage.risks.list()) == len(BOOTSTRAP_RISKS)

    def test_second_seed_writes_no_audit_events(self, bootstrap, storage) -> None:
        bootstrap.seed()
        before = len(storage.audit.list())
        bootstrap.seed()
        assert len(storage.audit.list()) == before



class TestProjectReconciliation:
    def test_existing_project_runtime_state_is_preserved(
        self, bootstrap, storage
    ) -> None:
        storage.projects.upsert(
            Project(
                name=PROJECT_NAME,
                plan_version="0.2",
                mode=Mode.AUTO,
                paused=True,
                current_step_no_snapshot=5,
            )
        )

        summary = bootstrap.seed()

        assert summary.project is BootstrapOutcome.ALREADY_PRESENT
        project = storage.projects.get(PROJECT_NAME)
        assert project.mode is Mode.AUTO
        assert project.paused is True
        assert project.current_step_no_snapshot == 5
        assert project.plan_version == "0.2"

    def test_a_different_project_name_is_a_conflict(
        self, bootstrap, storage
    ) -> None:
        storage.projects.upsert(
            Project(name="Another Project", plan_version="0.3")
        )
        with pytest.raises(BootstrapConflictError, match="singleton"):
            bootstrap.seed()

    def test_multiple_projects_are_a_conflict(self, bootstrap, storage) -> None:
        storage.projects.upsert(Project(name=PROJECT_NAME, plan_version="0.3"))
        storage.projects.upsert(
            Project(name="Another Project", plan_version="0.3")
        )
        with pytest.raises(BootstrapConflictError, match="at most one project"):
            bootstrap.seed()

    def test_project_conflict_leaves_nothing_behind(
        self, bootstrap, storage
    ) -> None:
        storage.projects.upsert(
            Project(name="Another Project", plan_version="0.3")
        )

        with pytest.raises(BootstrapConflictError):
            bootstrap.seed()

        assert storage.architecture_versions.list() == ()
        assert storage.adrs.list() == ()
        assert storage.risks.list() == ()
        assert storage.audit.list() == ()


class TestVersionReconciliation:
    def test_matching_version_is_a_noop(self, bootstrap, storage) -> None:
        storage.architecture_versions.upsert(canonical_version())
        summary = bootstrap.seed()
        assert summary.architecture_version is BootstrapOutcome.ALREADY_PRESENT

    def test_differing_rules_are_a_conflict(self, bootstrap, storage) -> None:
        storage.architecture_versions.upsert(
            ArchitectureVersion(
                version=ARCHITECTURE_V1.version,
                baseline=ARCHITECTURE_V1.description,
                rules=("a-different-rule",),
                is_current=True,
                created_at=NOW,
            )
        )
        with pytest.raises(
            BootstrapConflictError, match="differs from the canonical baseline"
        ):
            bootstrap.seed()

    def test_version_conflict_rolls_back_everything(
        self, bootstrap, storage
    ) -> None:
        storage.architecture_versions.upsert(
            ArchitectureVersion(
                version=ARCHITECTURE_V1.version,
                baseline="a different baseline",
                rules=("a-different-rule",),
                is_current=True,
                created_at=NOW,
            )
        )

        with pytest.raises(BootstrapConflictError):
            bootstrap.seed()

        assert storage.projects.list() == ()
        assert storage.adrs.list() == ()
        assert storage.risks.list() == ()
        assert storage.audit.list() == ()

    def test_another_current_version_is_a_conflict(
        self, bootstrap, storage
    ) -> None:
        storage.architecture_versions.upsert(
            ArchitectureVersion(
                version="2.0",
                baseline="a newer baseline",
                rules=("a-different-rule",),
                is_current=True,
                created_at=NOW,
            )
        )
        with pytest.raises(BootstrapConflictError, match="already current"):
            bootstrap.seed()

    def test_version_history_is_never_rewritten(self, bootstrap, storage) -> None:
        existing = ArchitectureVersion(
            version="2.0",
            baseline="a newer baseline",
            rules=("a-different-rule",),
            is_current=True,
            created_at=NOW,
        )
        storage.architecture_versions.upsert(existing)

        with pytest.raises(BootstrapConflictError):
            bootstrap.seed()

        stored = storage.architecture_versions.get("2.0")
        assert stored == existing
        assert storage.architecture_versions.get(ARCHITECTURE_V1.version) is None



class TestAdrReconciliation:
    def test_matching_adr_is_a_noop(self, bootstrap, storage) -> None:
        spec = BOOTSTRAP_ADRS[0]
        storage.adrs.upsert(adr_like(spec))

        summary = bootstrap.seed()

        assert spec.id in summary.adrs_already_present
        assert spec.id not in summary.adrs_created

    def test_same_id_with_a_different_decision_is_a_conflict(
        self, bootstrap, storage
    ) -> None:
        spec = BOOTSTRAP_ADRS[0]
        storage.adrs.upsert(adr_like(spec, decision="a different decision"))

        with pytest.raises(BootstrapConflictError, match=spec.id):
            bootstrap.seed()

    def test_conflicting_adr_is_never_overwritten(
        self, bootstrap, storage
    ) -> None:
        spec = BOOTSTRAP_ADRS[0]
        original = adr_like(spec, decision="a different decision")
        storage.adrs.upsert(original)

        with pytest.raises(BootstrapConflictError):
            bootstrap.seed()

        assert storage.adrs.get(spec.id) == original

    def test_different_status_is_a_conflict(self, bootstrap, storage) -> None:
        spec = BOOTSTRAP_ADRS[0]
        storage.adrs.upsert(adr_like(spec, status=ADRStatus.PROPOSED))

        with pytest.raises(BootstrapConflictError, match=spec.id):
            bootstrap.seed()

    def test_a_later_adr_conflict_rolls_back_earlier_ones(
        self, bootstrap, storage
    ) -> None:
        spec = BOOTSTRAP_ADRS[-1]
        storage.adrs.upsert(adr_like(spec, decision="a different decision"))

        with pytest.raises(BootstrapConflictError):
            bootstrap.seed()

        # the conflict happened after the first ADRs were created
        assert [adr.id for adr in storage.adrs.list()] == [spec.id]
        assert storage.risks.list() == ()
        assert storage.audit.list() == ()


class TestRiskReconciliation:
    def test_matching_risk_is_a_noop(self, bootstrap, storage) -> None:
        spec = BOOTSTRAP_RISKS[0]
        storage.risks.upsert(risk_like(spec))

        summary = bootstrap.seed()

        assert spec.id in summary.risks_already_present
        assert spec.id not in summary.risks_created

    def test_same_id_with_a_different_mitigation_is_a_conflict(
        self, bootstrap, storage
    ) -> None:
        spec = BOOTSTRAP_RISKS[0]
        storage.risks.upsert(risk_like(spec, mitigation="a different mitigation"))

        with pytest.raises(BootstrapConflictError, match=spec.id):
            bootstrap.seed()

    def test_conflicting_risk_is_never_overwritten(
        self, bootstrap, storage
    ) -> None:
        spec = BOOTSTRAP_RISKS[0]
        original = risk_like(spec, mitigation="a different mitigation")
        storage.risks.upsert(original)

        with pytest.raises(BootstrapConflictError):
            bootstrap.seed()

        assert storage.risks.get(spec.id) == original

    def test_different_severity_is_a_conflict(self, bootstrap, storage) -> None:
        spec = BOOTSTRAP_RISKS[0]
        storage.risks.upsert(risk_like(spec, severity=Severity.LOW))

        with pytest.raises(BootstrapConflictError, match=spec.id):
            bootstrap.seed()


class TestAtomicRollback:
    def test_late_conflict_rolls_back_earlier_creations(
        self, bootstrap, storage
    ) -> None:
        # only the LAST risk conflicts, so everything created before it - the
        # project, the baseline, all ADRs and the earlier risks - must vanish
        spec = BOOTSTRAP_RISKS[-1]
        storage.risks.upsert(risk_like(spec, mitigation="a different mitigation"))

        with pytest.raises(BootstrapConflictError):
            bootstrap.seed()

        assert storage.projects.list() == ()
        assert storage.architecture_versions.list() == ()
        assert storage.adrs.list() == ()
        assert [risk.id for risk in storage.risks.list()] == [spec.id]
        assert storage.audit.list() == ()

    def test_a_failed_seed_can_be_retried_after_the_conflict_is_resolved(
        self, bootstrap, storage
    ) -> None:
        spec = BOOTSTRAP_RISKS[-1]
        storage.risks.upsert(risk_like(spec, mitigation="a different mitigation"))

        with pytest.raises(BootstrapConflictError):
            bootstrap.seed()

        storage.risks.delete(spec.id)
        summary = bootstrap.seed()

        assert summary.created_count == 2 + len(BOOTSTRAP_ADRS) + len(
            BOOTSTRAP_RISKS
        )
        assert len(storage.adrs.list()) == len(BOOTSTRAP_ADRS)



class TestInjectedBaseline:
    def test_bootstrap_module_does_not_hardcode_rule_ids(self) -> None:
        source = Path(bootstrap_module.__file__ or "").read_text(encoding="utf-8")
        for rule_id in ARCHITECTURE_V1.rule_ids():
            assert rule_id not in source

    def test_bootstrap_module_does_not_import_the_architecture_layer(self) -> None:
        source = Path(bootstrap_module.__file__ or "").read_text(encoding="utf-8")
        assert "architecture_assistant.architecture" not in source
        assert "from ..architecture" not in source

    def test_architecture_version_factory_must_be_callable(self, storage) -> None:
        with pytest.raises(ValueError, match="architecture_version_factory"):
            ArchitectureBootstrap(storage, "not-callable", clock=fixed_clock)

    def test_clock_must_be_callable(self, storage) -> None:
        with pytest.raises(ValueError, match="clock must be a callable"):
            ArchitectureBootstrap(storage, canonical_version, clock="nope")

    def test_a_custom_spec_is_used_verbatim(self, storage) -> None:
        custom = ArchitectureVersion(
            version="9.9",
            baseline="a custom baseline",
            rules=("custom-rule",),
            is_current=True,
            created_at=NOW,
        )
        bootstrap = ArchitectureBootstrap(
            storage, lambda: custom, clock=fixed_clock
        )
        bootstrap.seed()

        stored = storage.architecture_versions.get("9.9")
        assert stored is not None
        assert stored.rules == ("custom-rule",)

    def test_specs_only_use_existing_domain_enum_values(self) -> None:
        severities = {member.value for member in Severity}
        for spec in BOOTSTRAP_RISKS:
            assert spec.severity.value in severities
            assert spec.impact.value in severities
            assert 0.0 <= spec.probability <= 1.0


class TestRetrospectiveProvenance:
    def test_every_adr_spec_context_carries_the_note(self) -> None:
        for spec in BOOTSTRAP_ADRS:
            assert spec.context.startswith(RETROSPECTIVE_NOTE)

    def test_every_seeded_adr_context_carries_the_note(
        self, bootstrap, storage
    ) -> None:
        bootstrap.seed()
        for spec in BOOTSTRAP_ADRS:
            stored = storage.adrs.get(spec.id)
            assert stored is not None
            assert stored.context.startswith(RETROSPECTIVE_NOTE)

    def test_errors_share_a_bootstrap_base(self) -> None:
        assert issubclass(BootstrapConflictError, BootstrapError)


class TestReconciliation:
    def test_persisted_rules_match_the_code_baseline(
        self, bootstrap, storage
    ) -> None:
        bootstrap.seed()
        version = storage.architecture_versions.get(ARCHITECTURE_V1.version)
        assert version is not None
        assert version.rules == ARCHITECTURE_V1.rule_ids()

    def test_persisted_baseline_text_matches_the_code_baseline(
        self, bootstrap, storage
    ) -> None:
        bootstrap.seed()
        version = storage.architecture_versions.get(ARCHITECTURE_V1.version)
        assert version.baseline == ARCHITECTURE_V1.description

    def test_realized_code_satisfies_the_baseline(self) -> None:
        source_root = (
            Path(__file__).resolve().parents[1] / "src" / "architecture_assistant"
        )
        result = ArchitectureValidator().validate(scan_directory(source_root))
        assert result.is_compliant is True
        assert result.violations == ()

    def test_context_builder_reflects_the_bootstrapped_state(
        self, bootstrap, storage
    ) -> None:
        bootstrap.seed()
        storage.steps.upsert(
            Step(
                step_no=8,
                phase=Phase.ARCHITECTURE,
                title="Architecture Bootstrap Reconciliation",
                state=StepState.READY,
            )
        )
        builder = ContextBuilder(
            storage.projects,
            storage.steps,
            storage.adrs,
            storage.risks,
            storage.architecture_versions,
            clock=fixed_clock,
        )

        snapshot = builder.build(8)

        assert snapshot.project.name == PROJECT_NAME
        assert snapshot.architecture is not None
        assert snapshot.architecture.version == ARCHITECTURE_V1.version
        assert tuple(adr.id for adr in snapshot.adrs) == ADR_IDS
        assert tuple(risk.id for risk in snapshot.risks) == RISK_IDS

    def test_context_builder_needs_the_bootstrap_first(self, storage) -> None:
        storage.steps.upsert(
            Step(
                step_no=8,
                phase=Phase.ARCHITECTURE,
                title="Architecture Bootstrap Reconciliation",
                state=StepState.READY,
            )
        )
        builder = ContextBuilder(
            storage.projects,
            storage.steps,
            storage.adrs,
            storage.risks,
            storage.architecture_versions,
            clock=fixed_clock,
        )
        with pytest.raises(ProjectNotFoundError):
            builder.build(8)

