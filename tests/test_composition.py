"""Tests for the composition root - the outer layer added by architecture v1.1.

Covers the mandated properties: the root may import every layer while no other
layer gains a permission, the canonical v1.0 baseline is superseded (never
rewritten) by v1.1 with its ADR, composing twice changes nothing, a Step 8
database is migrated forward, and the whole loop really runs through the file
channel. Step 11 adds the declared-change assertions: the v1.0 -> v1.1 change is
reconciled through the persisted change-request lifecycle (propose -> approve ->
apply), the request stays linked to its ADR and roadmap impact, and re-applying an
applied change writes nothing at all.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import architecture_assistant.composition.root as root_module
from architecture_assistant.application import (
    COMPOSITION_ROOT_ADR,
    PROJECT_NAME,
    PROJECT_PLAN_VERSION,
    STEP_9_PROVENANCE,
    ArchitectureBootstrap,
    ArchitectureEvolution,
    ArchitectureVersioning,
    BootstrapOutcome,
    ContextBuilder,
    LoopStatus,
    Orchestrator,
    RealizationControlUseCase,
    Scheduler,
)
from architecture_assistant.architecture import (
    APPLICATION_LAYER,
    ARCHITECTURE_CURRENT,
    ARCHITECTURE_LAYER,
    ARCHITECTURE_V1,
    ARCHITECTURE_V1_1,
    COMPOSITION_LAYER,
    DOMAIN_LAYER,
    INFRASTRUCTURE_LAYER,
    ROOT_PACKAGE,
    ArchitectureValidator,
    layer_of,
    scan_directory,
)
from architecture_assistant.composition import (
    COMPOSITION_LAYER_DRIFT_RISK,
    COMPOSITION_ROOT_ROADMAP_IMPACT,
    DEFAULT_APPROVER,
    DEFAULT_SOURCE_ROOT,
    V1_1_ACR_ID,
    ArchitectureRealizationAdapter,
    Composition,
    CompositionConfig,
    baseline_v1,
    baseline_v1_1,
    compose,
)
from architecture_assistant.domain.audit import AuditEntityType
from architecture_assistant.domain.enums import (
    ACRStatus,
    ADRStatus,
    Mode,
    Phase,
    RiskLevel,
    RiskStatus,
    StepState,
)
from architecture_assistant.domain.models import Step
from architecture_assistant.infrastructure import (
    OPENAI_API_KEY_ENV_VAR,
    ClineWorkerAdapter,
    OpenAIAdvisorAdapter,
    SqliteStorage,
    close_database,
    open_database,
)
from architecture_assistant.ports.capabilities import (
    AdvisorPort,
    RealizationCheckPort,
    RealizationControlPort,
    WorkerChannelPort,
    WorkerPort,
)

FIXED = datetime(2026, 9, 20, 20, 0, tzinfo=timezone.utc)


def fixed_clock() -> datetime:
    return FIXED


def _src_root() -> Path:
    return Path(__file__).resolve().parents[1] / "src" / "architecture_assistant"


#: The tree the composed assistant inspects by default: it inspects itself.
REAL_SOURCE_ROOT = _src_root()


@pytest.fixture
def config(tmp_path) -> CompositionConfig:
    return CompositionConfig(
        database_path=tmp_path / "assistant.db",
        exchange_dir=tmp_path / "cline",
        mode=Mode.AUTO,
        timeout=timedelta(hours=1),
        source_root=REAL_SOURCE_ROOT,
        clock=fixed_clock,
    )


@pytest.fixture
def composition(config):
    result = compose(config)
    yield result
    result.close()


def make_step(
    step_no: int = 9,
    state: StepState = StepState.READY,
    risk: RiskLevel = RiskLevel.LOW,
) -> Step:
    return Step(
        step_no=step_no,
        phase=Phase.LOOP,
        title="Orchestrator + Scheduler",
        state=state,
        attempt=1,
        risk=risk,
        requires_human=False,
        created_at=FIXED,
    )


class TestWiring:
    def test_compose_wires_every_collaborator(self, composition) -> None:
        assert isinstance(composition, Composition)
        assert isinstance(composition.storage, SqliteStorage)
        assert isinstance(composition.worker, ClineWorkerAdapter)
        assert isinstance(composition.context_builder, ContextBuilder)
        assert isinstance(composition.orchestrator, Orchestrator)
        assert isinstance(composition.scheduler, Scheduler)
        assert isinstance(composition.bootstrap, ArchitectureBootstrap)
        assert isinstance(composition.versioning, ArchitectureVersioning)
        assert composition.scheduler.orchestrator is composition.orchestrator

    def test_the_worker_implements_both_worker_contracts(
        self, composition
    ) -> None:
        assert isinstance(composition.worker, WorkerChannelPort)
        assert isinstance(composition.worker, WorkerPort)

    def test_the_exchange_directory_is_configurable(
        self, composition, config
    ) -> None:
        assert composition.worker.exchange_dir == config.exchange_dir

    def test_the_canonical_records_are_seeded(self, composition) -> None:
        project = composition.storage.projects.get(PROJECT_NAME)
        assert project is not None
        assert project.plan_version == PROJECT_PLAN_VERSION
        assert project.mode is Mode.AUTO
        assert project.paused is False

        adr_ids = tuple(adr.id for adr in composition.storage.adrs.list())
        assert "ADR-001" in adr_ids
        assert COMPOSITION_ROOT_ADR.id in adr_ids
        # RISK-006 is declared by the v1.0 -> v1.1 change and registered on apply.
        assert len(composition.storage.risks.list()) == 6
        assert all(
            risk.status is RiskStatus.OPEN
            for risk in composition.storage.risks.list()
        )

    def test_v1_is_kept_as_history_and_v1_1_is_current(
        self, composition
    ) -> None:
        old = composition.storage.architecture_versions.get(
            ARCHITECTURE_V1.version
        )
        new = composition.storage.architecture_versions.get(
            ARCHITECTURE_V1_1.version
        )
        assert old is not None and new is not None
        assert old.is_current is False
        assert old.superseded_by == ARCHITECTURE_V1_1.version
        assert new.is_current is True
        assert new.superseded_by is None
        assert (
            composition.versioning_summary.version_outcome
            is BootstrapOutcome.CREATED
        )

    def test_the_baseline_change_is_recorded_in_an_adr(
        self, composition
    ) -> None:
        adr = composition.storage.adrs.get(COMPOSITION_ROOT_ADR.id)
        assert adr is not None
        assert adr.status is ADRStatus.ACCEPTED
        assert adr.context.startswith(STEP_9_PROVENANCE)
        assert "composition" in adr.decision

    def test_the_current_baseline_is_the_validated_one(
        self, composition
    ) -> None:
        current = tuple(
            version
            for version in composition.storage.architecture_versions.list()
            if version.is_current
        )
        assert len(current) == 1
        assert current[0].version == ARCHITECTURE_CURRENT.version
        assert current[0].rules == ARCHITECTURE_CURRENT.rule_ids()


class TestDeclaredChanges:
    """Step 11: the change is authoritative state, not a code-side comment."""

    def test_the_engine_is_wired_over_the_declared_catalogue(
        self, composition
    ) -> None:
        assert isinstance(composition.evolution, ArchitectureEvolution)
        assert composition.evolution.versioning is composition.versioning
        assert composition.evolution_summary.request_ids == (V1_1_ACR_ID,)

    def test_the_change_request_is_persisted_and_applied(
        self, composition
    ) -> None:
        request = composition.storage.change_requests.get(V1_1_ACR_ID)
        assert request is not None
        assert request.status is ACRStatus.APPLIED
        assert request.source_version == ARCHITECTURE_V1.version
        assert request.target_version == ARCHITECTURE_V1_1.version
        assert request.adr_id == COMPOSITION_ROOT_ADR.id
        assert request.rule_ids == ARCHITECTURE_V1_1.rule_ids()
        assert request.roadmap_impact == COMPOSITION_ROOT_ROADMAP_IMPACT
        assert request.approved_by == DEFAULT_APPROVER
        assert request.approved_at is not None
        assert request.applied_at is not None

    def test_the_request_is_linked_structurally_to_its_adr_and_version(
        self, composition
    ) -> None:
        linked = composition.evolution.linked(V1_1_ACR_ID)
        assert linked.adr is not None
        assert linked.adr.id == COMPOSITION_ROOT_ADR.id
        assert linked.adr.status is ADRStatus.ACCEPTED
        assert linked.version is not None
        assert linked.version.version == ARCHITECTURE_V1_1.version
        assert linked.version.is_current is True

    def test_the_declared_risk_is_registered_by_the_apply(
        self, composition
    ) -> None:
        risk = composition.storage.risks.get(COMPOSITION_LAYER_DRIFT_RISK.id)
        assert risk is not None
        assert risk.status is RiskStatus.OPEN
        assert risk.description == COMPOSITION_LAYER_DRIFT_RISK.description
        assert composition.evolution.applied() == (
            composition.storage.change_requests.get(V1_1_ACR_ID),
        )
        assert composition.evolution.pending() == ()
        assert composition.evolution.approved() == ()

    def test_the_lifecycle_is_recorded_in_the_audit_trail(
        self, composition
    ) -> None:
        entries = tuple(
            entry
            for entry in composition.storage.audit.list()
            if entry.entity_type is AuditEntityType.ACR
            and entry.entity_id == V1_1_ACR_ID
        )
        assert tuple(entry.action.value for entry in entries) == (
            "CREATE",
            "UPDATE",
            "UPDATE",
        )
        assert entries[0].detail["status_to"] == "PROPOSED"
        assert entries[1].detail["status_to"] == "APPROVED"
        assert entries[2].detail["status_to"] == "APPLIED"
        assert entries[2].detail["risks_created"] == [
            COMPOSITION_LAYER_DRIFT_RISK.id
        ]

    def test_applying_an_applied_change_changes_nothing(
        self, composition
    ) -> None:
        versions = composition.storage.architecture_versions.list()
        audit = composition.storage.audit.list()
        request = composition.storage.change_requests.get(V1_1_ACR_ID)

        summary = composition.evolution.apply(V1_1_ACR_ID)

        assert summary.outcome is BootstrapOutcome.ALREADY_PRESENT
        assert summary.version_outcome is BootstrapOutcome.ALREADY_PRESENT
        assert summary.risks_created == ()
        assert composition.storage.architecture_versions.list() == versions
        assert composition.storage.audit.list() == audit
        assert composition.storage.change_requests.get(V1_1_ACR_ID) == request


class TestIdempotency:
    def test_compose_is_idempotent(self, composition, config) -> None:
        versions = composition.storage.architecture_versions.list()
        audit = composition.storage.audit.list()
        adrs = composition.storage.adrs.list()
        changes = composition.storage.change_requests.list()

        second = compose(config)
        try:
            assert second.bootstrap_summary.architecture_version is (
                BootstrapOutcome.ALREADY_PRESENT
            )
            assert second.versioning_summary.version_outcome is (
                BootstrapOutcome.ALREADY_PRESENT
            )
            assert second.versioning_summary.adrs_already_present == (
                COMPOSITION_ROOT_ADR.id,
            )
            assert second.evolution_summary.changes[0].outcome is (
                BootstrapOutcome.ALREADY_PRESENT
            )
            assert second.evolution_summary.changes[0].status is (
                ACRStatus.APPLIED
            )
            assert second.storage.change_requests.list() == changes
            assert second.storage.architecture_versions.list() == versions
            assert second.storage.audit.list() == audit
            assert second.storage.adrs.list() == adrs
        finally:
            second.close()

    def test_live_project_state_is_preserved_on_recompose(
        self, composition, config
    ) -> None:
        project = composition.storage.projects.get(PROJECT_NAME)
        assert project is not None
        composition.storage.projects.upsert(
            replace(project, mode=Mode.MANUAL, paused=True)
        )

        second = compose(config)
        try:
            reloaded = second.storage.projects.get(PROJECT_NAME)
            assert reloaded is not None
            assert reloaded.mode is Mode.MANUAL
            assert reloaded.paused is True
        finally:
            second.close()

    def test_a_step_8_database_is_migrated_forward(self, config) -> None:
        """The authoritative v1.0 row from Step 8 is superseded, not rewritten."""
        connection = open_database(config.database_path)
        try:
            storage = SqliteStorage(connection)
            ArchitectureBootstrap(
                storage, lambda: baseline_v1(FIXED), clock=fixed_clock
            ).seed()
            stored = storage.architecture_versions.get(ARCHITECTURE_V1.version)
            assert stored is not None and stored.is_current is True
            assert storage.architecture_versions.get("1.1") is None
            before = stored
        finally:
            close_database(connection)

        migrated = compose(config)
        try:
            summary = migrated.versioning_summary
            assert summary.version_outcome is BootstrapOutcome.CREATED
            assert summary.superseded == ARCHITECTURE_V1.version

            old = migrated.storage.architecture_versions.get("1.0")
            new = migrated.storage.architecture_versions.get("1.1")
            assert old is not None and new is not None
            assert old.baseline == before.baseline
            assert old.rules == before.rules
            assert old.created_at == before.created_at
            assert (old.is_current, old.superseded_by) == (False, "1.1")
            assert new.is_current is True
        finally:
            migrated.close()


def done_report(**overrides):
    payload = {
        "step_no": 9,
        "attempt": 1,
        "status": "DONE",
        "summary": "orchestrator implemented",
        "files_created": [],
        "files_changed": [],
        "files_deleted": [],
        "tests": {"passed": 10, "failed": 0, "command": "pytest"},
        "dependencies_added": [],
        "architecture_questions": [],
        "issues": [],
    }
    payload.update(overrides)
    return payload


def publish_report(
    composition: Composition, payload: dict, step_no: int = 9, attempt: int = 1
) -> Path:
    """Write a report into the channel, as the worker would."""
    path = composition.worker.report_path(step_no, attempt)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestEndToEnd:
    def test_the_loop_runs_through_the_file_channel(self, composition) -> None:
        composition.storage.steps.upsert(make_step())

        first = composition.run_until_idle()
        assert first.stopped_because == "worker-report-pending"
        assert composition.worker.task_path(9, 1).is_file()
        assert composition.worker.context_path(9, 1).is_file()
        assert composition.health().current_state is StepState.CLINE_WORKING

        written = composition.worker.task_path(9, 1).read_text(encoding="utf-8")
        task = json.loads(written)
        assert task["protocol"] == composition.config.protocol
        assert task["step_no"] == 9
        assert task["attempt"] == 1
        assert task["instructions"]["architecture_change"].startswith("STOP")
        assert task["report_schema"]["status"]

        publish_report(composition, done_report())

        final = composition.run_until_idle()
        assert final.final_tick.status is LoopStatus.COMPLETE
        step = composition.storage.steps.get(9)
        assert step is not None and step.state is StepState.VERIFIED
        assert not composition.worker.report_path(9, 1).is_file()
        assert len(tuple(composition.worker.archive_dir().glob("*.json"))) == 1
        assert composition.health().complete is True

    def test_an_architecture_question_blocks_the_step(self, composition) -> None:
        composition.storage.steps.upsert(make_step())
        composition.run_until_idle()
        publish_report(
            composition,
            done_report(
                architecture_questions=["is the composition layer legal?"]
            ),
        )

        run = composition.run_until_idle()

        assert run.stopped_because == "human-unblock-required"
        step = composition.storage.steps.get(9)
        assert step is not None and step.state is StepState.BLOCKED

    def test_an_unapproved_step_is_never_dispatched(self, config) -> None:
        config = replace(config, mode=Mode.MANUAL)
        composition = compose(config)
        try:
            composition.storage.steps.upsert(make_step())
            run = composition.run_until_idle()
            assert run.stopped_because == "human-approval-required"
            assert not composition.worker.task_path(9, 1).is_file()
        finally:
            composition.close()


class TestConfig:
    def test_defaults(self) -> None:
        config = CompositionConfig()
        assert config.project_name == PROJECT_NAME
        assert config.plan_version == PROJECT_PLAN_VERSION
        assert config.mode is Mode.MANUAL
        assert config.timeout is not None
        assert config.max_iterations >= 1

    def test_is_json_safe(self) -> None:
        payload = CompositionConfig().to_dict()
        assert json.loads(json.dumps(payload)) == payload
        assert payload["mode"] == "MANUAL"

    def test_mode_must_be_a_mode(self) -> None:
        with pytest.raises(ValueError, match="mode must be a Mode"):
            CompositionConfig(mode="AUTO")  # type: ignore[arg-type]

    def test_max_iterations_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="max_iterations"):
            CompositionConfig(max_iterations=0)

    def test_clock_must_be_callable(self) -> None:
        with pytest.raises(ValueError, match="clock must be a callable"):
            CompositionConfig(clock="now")  # type: ignore[arg-type]

    def test_baseline_factories_use_the_code_baseline(self) -> None:
        v1 = baseline_v1(FIXED)
        v11 = baseline_v1_1(FIXED)
        assert v1.version == ARCHITECTURE_V1.version
        assert v1.baseline == ARCHITECTURE_V1.description
        assert v1.rules == ARCHITECTURE_V1.rule_ids()
        assert v11.version == ARCHITECTURE_V1_1.version
        assert v11.rules == ARCHITECTURE_V1_1.rule_ids()
        assert v1.is_current is True and v11.is_current is True


class TestRealizationGateWiring:
    def test_compose_wires_the_realization_gate(self, composition) -> None:
        assert isinstance(
            composition.realization_adapter, ArchitectureRealizationAdapter
        )
        assert isinstance(
            composition.realization_control, RealizationControlUseCase
        )
        assert isinstance(composition.realization_control, RealizationControlPort)
        assert isinstance(
            composition.realization_adapter, RealizationCheckPort
        )
        # the loop is wired to the gate, not to something optional
        assert composition.orchestrator.realization_control is (
            composition.realization_control
        )

    def test_the_orchestrator_cannot_be_built_without_the_gate(self) -> None:
        """Production composition must never skip the architecture gate."""
        import inspect

        signature = inspect.signature(Orchestrator.__init__)
        parameter = signature.parameters["realization_control"]
        assert parameter.default is inspect.Parameter.empty

    def test_the_config_has_no_switch_to_disable_the_gate(self) -> None:
        names = set(CompositionConfig().to_dict())
        assert "source_root" in names
        assert not {name for name in names if "enable" in name or "disable" in name}

    def test_the_gate_uses_the_canonical_baseline(self, composition) -> None:
        assert composition.realization_control.canonical.version == (
            ARCHITECTURE_CURRENT.version
        )
        assert composition.realization_control.canonical.rule_ids == (
            ARCHITECTURE_CURRENT.rule_ids()
        )

    def test_the_source_root_of_the_adapter_is_injected(
        self, composition, config
    ) -> None:
        assert composition.realization_adapter.source_root == config.source_root
        assert composition.realization_adapter.baseline is ARCHITECTURE_CURRENT

    def test_the_config_reports_the_source_root(self) -> None:
        payload = CompositionConfig().to_dict()
        assert payload["source_root"] == str(DEFAULT_SOURCE_ROOT)

    def test_a_violating_target_tree_blocks_verification(self, config, tmp_path) -> None:
        """End-to-end: the injected tree decides, and a violation blocks."""
        target = tmp_path / "target"
        (target / "application").mkdir(parents=True)
        (target / "application" / "__init__.py").write_text("", encoding="utf-8")
        (target / "application" / "rogue.py").write_text(
            "import architecture_assistant.architecture.rules\n",
            encoding="utf-8",
        )
        (target / "domain").mkdir(parents=True)
        (target / "domain" / "__init__.py").write_text("", encoding="utf-8")

        composition = compose(replace(config, source_root=target))
        try:
            composition.storage.steps.upsert(make_step())
            composition.run_until_idle()
            publish_report(composition, done_report())

            run = composition.run_until_idle()

            assert run.stopped_because == "human-unblock-required"
            step = composition.storage.steps.get(9)
            assert step is not None and step.state is StepState.BLOCKED
            assert any(
                finding.claim == "unknown-layer"
                or finding.claim == "forbidden-layer-import"
                for finding in composition.storage.findings.list()
            )
        finally:
            composition.close()


class TestOptionalAdvisor:
    """Step 12: the advisor is an optional capability, never a decision maker."""

    def test_a_project_without_an_api_key_still_composes(
        self, config, monkeypatch
    ) -> None:
        monkeypatch.delenv(OPENAI_API_KEY_ENV_VAR, raising=False)

        composition = compose(config)
        try:
            advisor = composition.openai_advisor
            assert isinstance(advisor, OpenAIAdvisorAdapter)
            assert isinstance(advisor, AdvisorPort)
            assert advisor.is_configured is False
            # the deterministic side of the graph is entirely unaffected
            assert (
                composition.evolution.current_version().version
                == ARCHITECTURE_CURRENT.version
            )
            assert composition.evolution_summary.changes[0].status is (
                ACRStatus.APPLIED
            )
            # and the loop still runs, with no advisor anywhere in it
            composition.storage.steps.upsert(make_step())
            run = composition.run_until_idle()
            assert run.stopped_because == "worker-report-pending"
            assert composition.health().current_state is StepState.CLINE_WORKING
            assert composition.openai_advisor.is_configured is False
        finally:
            composition.close()

    def test_the_environment_key_is_picked_up_lazily(
        self, composition, monkeypatch
    ) -> None:
        monkeypatch.setenv(OPENAI_API_KEY_ENV_VAR, "sk-test-env-value")

        # composed before the key existed: the advisor notices it at call time
        assert composition.openai_advisor.is_configured is True
        assert "sk-test-env-value" not in repr(composition.openai_advisor)

    def test_the_advisor_is_not_wired_into_the_decision_path(
        self, composition
    ) -> None:
        advisor = composition.openai_advisor

        assert advisor is not composition.worker
        for collaborator in (
            composition.orchestrator,
            composition.scheduler,
            composition.realization_control,
            composition.realization_adapter,
        ):
            assert advisor is not collaborator
        assert "advisor" not in {
            name for name in dir(composition.orchestrator)
        }
        assert "advisor" not in {
            name for name in dir(composition.realization_control)
        }


class TestLayerBoundaries:
    def test_composition_may_import_every_layer(self) -> None:
        source = scan_directory(_src_root())
        module = f"{ROOT_PACKAGE}.{COMPOSITION_LAYER}.root"
        targets = {layer_of(target) for target in source.imports_of(module)}
        assert {
            DOMAIN_LAYER,
            APPLICATION_LAYER,
            ARCHITECTURE_LAYER,
            INFRASTRUCTURE_LAYER,
        } <= targets

    def test_composition_never_imports_sqlite3(self) -> None:
        source = scan_directory(_src_root())
        for module in source.modules():
            if layer_of(module) != COMPOSITION_LAYER:
                continue
            assert "sqlite3" not in source.imports_of(module), module

    def test_no_other_layer_may_import_composition(self) -> None:
        source = scan_directory(_src_root())
        for module in source.modules():
            layer = layer_of(module)
            if layer in (COMPOSITION_LAYER, "", None):
                continue
            for target in source.imports_of(module):
                assert layer_of(target) != COMPOSITION_LAYER, module

    def test_application_still_may_not_reach_outwards(self) -> None:
        """The Step 9 change must not have widened the application layer."""
        source = scan_directory(_src_root())
        checked = 0
        for module in source.modules():
            if layer_of(module) != APPLICATION_LAYER:
                continue
            checked += 1
            for target in source.imports_of(module):
                assert layer_of(target) not in (
                    ARCHITECTURE_LAYER,
                    INFRASTRUCTURE_LAYER,
                    COMPOSITION_LAYER,
                ), module
        assert checked > 0

    def test_ports_and_domain_are_untouched_by_the_change(self) -> None:
        source = scan_directory(_src_root())
        for module in source.modules():
            layer = layer_of(module)
            if layer not in (DOMAIN_LAYER, "ports"):
                continue
            for target in source.imports_of(module):
                assert layer_of(target) not in (
                    APPLICATION_LAYER,
                    ARCHITECTURE_LAYER,
                    INFRASTRUCTURE_LAYER,
                    COMPOSITION_LAYER,
                ), module

    def test_the_composition_root_holds_no_loop_decisions(self) -> None:
        """Wiring only: no policy, no FSM and no step reasoning in the root."""
        text = Path(root_module.__file__ or "").read_text(encoding="utf-8")
        for banned in (
            "requires_approval",
            "decide_review",
            "StepState",
            "StepEvent",
            "StepStateMachine",
            "TaskStateMachine",
        ):
            assert banned not in text, banned

    def test_the_realized_tree_satisfies_the_current_baseline(self) -> None:
        result = ArchitectureValidator().validate(scan_directory(_src_root()))
        assert result.is_compliant is True
        assert result.baseline_version == ARCHITECTURE_CURRENT.version
