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
    ApprovalGate,
    ArchitectureBootstrap,
    ArchitectureEvolution,
    ArchitectureVersioning,
    BootstrapOutcome,
    ContextBuilder,
    EvidenceView,
    HumanOverride,
    JudgeUseCase,
    LoopStatus,
    Monitor,
    Orchestrator,
    RealizationControlUseCase,
    ReportBuilder,
    ReportSnapshot,
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
    DEFAULT_REPORT_DIR,
    DEFAULT_SOURCE_ROOT,
    V1_1_ACR_ID,
    ArchitectureRealizationAdapter,
    Composition,
    CompositionConfig,
    baseline_v1,
    baseline_v1_1,
    compose,
)
from architecture_assistant.domain.audit import AuditAction, AuditEntityType
from architecture_assistant.infrastructure.markdown_reporting import SECTION_NAMES
from architecture_assistant.domain.enums import (
    ACRStatus,
    ADRStatus,
    DecisionStatus,
    Mode,
    Phase,
    RiskLevel,
    RiskStatus,
    StepState,
)
from architecture_assistant.domain.models import Decision, Step
from architecture_assistant.infrastructure import (
    CHALLENGER_ROLE,
    CLAUDE_API_KEY_ENV_VAR,
    CLAUDE_MODEL_ENV_VAR,
    CRITICAL_REVIEWER_ROLE,
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_EXCHANGE_DIR,
    DEFAULT_GROK_MODEL,
    DEFAULT_OPENAI_JUDGE_MODEL,
    EVIDENCE_JUDGE_ROLE,
    GROK_MODEL_ENV_VAR,
    OPENAI_API_KEY_ENV_VAR,
    OPENAI_JUDGE_MODEL_ENV_VAR,
    XAI_API_KEY_ENV_VAR,
    ClaudeAdvisorAdapter,
    ClineWorkerAdapter,
    ExcelReportingAdapter,
    GrokAdvisorAdapter,
    MarkdownReportingAdapter,
    OpenAIAdvisorAdapter,
    OpenAIJudgeAdapter,
    SqliteStorage,
    close_database,
    open_database,
)
from architecture_assistant.ports.capabilities import (
    AdvisorPort,
    CostQuery,
    CostRecord,
    CostSummary,
    JudgePort,
    RealizationCheckPort,
    RealizationControlPort,
    ReportingPort,
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
        report_dir=tmp_path / "reports",
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
        # The Step 23 final review adds the approval-gate boundary (ADR-009) and
        # the V1 freeze with its documented limitations (ADR-010).
        assert "ADR-009" in adr_ids
        assert "ADR-010" in adr_ids
        # RISK-006 is declared by the v1.0 -> v1.1 change and registered on apply;
        # RISK-007..RISK-011 are the five limitations the V1 review accepted.
        assert len(composition.storage.risks.list()) == 11
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

    # -- Step 13: the second, independent perspective is optional too ------
    def test_a_project_without_an_anthropic_key_still_composes(
        self, config, monkeypatch
    ) -> None:
        monkeypatch.delenv(CLAUDE_API_KEY_ENV_VAR, raising=False)

        composition = compose(config)
        try:
            advisor = composition.claude_advisor
            assert isinstance(advisor, ClaudeAdvisorAdapter)
            assert isinstance(advisor, AdvisorPort)
            assert advisor.provider == "claude"
            assert advisor.role == CRITICAL_REVIEWER_ROLE
            # no key is a start-up requirement: the advisor is simply unusable
            assert advisor.is_configured is False
            # the deterministic side of the graph is entirely unaffected
            assert (
                composition.evolution.current_version().version
                == ARCHITECTURE_CURRENT.version
            )
            composition.storage.steps.upsert(make_step())
            run = composition.run_until_idle()
            assert run.stopped_because == "worker-report-pending"
            assert composition.health().current_state is StepState.CLINE_WORKING
            assert composition.claude_advisor.is_configured is False
        finally:
            composition.close()

    def test_the_claude_environment_key_is_picked_up_lazily(
        self, composition, monkeypatch
    ) -> None:
        monkeypatch.setenv(CLAUDE_API_KEY_ENV_VAR, "sk-ant-test-env-value")

        # composed before the key existed: the advisor notices it at call time
        assert composition.claude_advisor.is_configured is True
        assert "sk-ant-test-env-value" not in repr(composition.claude_advisor)

    def test_the_claude_model_is_configuration_not_architecture_truth(
        self, composition, monkeypatch
    ) -> None:
        monkeypatch.delenv(CLAUDE_MODEL_ENV_VAR, raising=False)

        assert composition.claude_advisor.model == DEFAULT_CLAUDE_MODEL
        monkeypatch.setenv(CLAUDE_MODEL_ENV_VAR, "claude-from-the-environment")
        assert composition.claude_advisor.model == DEFAULT_CLAUDE_MODEL

    def test_the_claude_advisor_is_not_wired_into_the_decision_path(
        self, composition
    ) -> None:
        advisor = composition.claude_advisor

        assert advisor is not composition.worker
        assert advisor is not composition.openai_advisor
        for collaborator in (
            composition.orchestrator,
            composition.scheduler,
            composition.realization_control,
            composition.realization_adapter,
        ):
            assert advisor is not collaborator
            for name in ("advisor", "claude", "judge", "vote", "majority"):
                assert name not in {entry for entry in dir(collaborator)}

    def test_the_two_advisors_are_independent_and_never_vote(
        self, composition
    ) -> None:
        assert composition.openai_advisor.provider == "openai"
        assert composition.claude_advisor.provider == "claude"
        assert composition.openai_advisor is not composition.claude_advisor
        # the advisors are consultative only: no ballot, no ranking, no merger
        for name in ("majority_vote", "decision_engine", "evidence_merger"):
            assert not hasattr(composition, name)

    # -- Step 14: the third, independent perspective is optional too -------
    def test_a_project_without_an_xai_key_still_composes(
        self, config, monkeypatch
    ) -> None:
        monkeypatch.delenv(XAI_API_KEY_ENV_VAR, raising=False)

        composition = compose(config)
        try:
            advisor = composition.grok_advisor
            assert isinstance(advisor, GrokAdvisorAdapter)
            assert isinstance(advisor, AdvisorPort)
            assert advisor.provider == "grok"
            assert advisor.role == CHALLENGER_ROLE
            # no key is a start-up requirement: the advisor is simply unusable
            assert advisor.is_configured is False
            # the deterministic side of the graph is entirely unaffected
            assert (
                composition.evolution.current_version().version
                == ARCHITECTURE_CURRENT.version
            )
            composition.storage.steps.upsert(make_step())
            run = composition.run_until_idle()
            assert run.stopped_because == "worker-report-pending"
            assert composition.health().current_state is StepState.CLINE_WORKING
            assert composition.grok_advisor.is_configured is False
        finally:
            composition.close()

    def test_the_xai_environment_key_is_picked_up_lazily(
        self, composition, monkeypatch
    ) -> None:
        monkeypatch.setenv(XAI_API_KEY_ENV_VAR, "xai-test-env-value")

        # composed before the key existed: the advisor notices it at call time
        assert composition.grok_advisor.is_configured is True
        assert "xai-test-env-value" not in repr(composition.grok_advisor)

    def test_the_grok_model_is_configuration_not_architecture_truth(
        self, composition, monkeypatch
    ) -> None:
        monkeypatch.delenv(GROK_MODEL_ENV_VAR, raising=False)

        assert composition.grok_advisor.model == DEFAULT_GROK_MODEL
        monkeypatch.setenv(GROK_MODEL_ENV_VAR, "grok-from-the-environment")
        assert composition.grok_advisor.model == DEFAULT_GROK_MODEL

    def test_the_grok_advisor_is_not_wired_into_the_decision_path(
        self, composition
    ) -> None:
        advisor = composition.grok_advisor

        assert advisor is not composition.worker
        assert advisor is not composition.openai_advisor
        assert advisor is not composition.claude_advisor
        for collaborator in (
            composition.orchestrator,
            composition.scheduler,
            composition.realization_control,
            composition.realization_adapter,
        ):
            assert advisor is not collaborator
            for name in ("advisor", "grok", "judge", "vote", "majority", "merge"):
                assert name not in {entry for entry in dir(collaborator)}

    def test_the_three_advisors_are_independent_and_never_vote(
        self, composition
    ) -> None:
        advisors = (
            composition.openai_advisor,
            composition.claude_advisor,
            composition.grok_advisor,
        )

        assert [advisor.provider for advisor in advisors] == [
            "openai",
            "claude",
            "grok",
        ]
        # three distinct objects: nothing merges or aggregates their findings
        assert len({id(advisor) for advisor in advisors}) == 3
        # no ballot, no ranking and no merger - and the judge is its own layer
        for name in ("majority_vote", "decision_engine", "evidence_merger"):
            assert not hasattr(composition, name)
        assert composition.judge is not composition.openai_advisor


class TestOptionalJudge:
    """Step 16: the judge is a separate, optional layer - never a voter."""

    def test_a_project_without_an_openai_key_still_composes_and_judges(
        self, config, monkeypatch
    ) -> None:
        monkeypatch.delenv(OPENAI_API_KEY_ENV_VAR, raising=False)

        composition = compose(config)
        try:
            judge = composition.judge
            assert isinstance(judge, OpenAIJudgeAdapter)
            assert isinstance(judge, JudgePort)
            assert judge.provider == "openai"
            assert judge.role == EVIDENCE_JUDGE_ROLE
            # no key is never a start-up requirement: the judge simply cannot run
            assert judge.is_configured is False
            # with no explicit conflict the judge is never asked anything
            run = composition.judge_use_case.resolve(EvidenceView())
            assert run.count == 0
            assert run.judge_available is True
            assert run.to_dict()["judgments"] == []
            # the deterministic side of the graph is entirely unaffected
            assert (
                composition.evolution.current_version().version
                == ARCHITECTURE_CURRENT.version
            )
            composition.storage.steps.upsert(make_step())
            assert composition.run_until_idle().stopped_because == (
                "worker-report-pending"
            )
        finally:
            composition.close()

    def test_the_judge_use_case_depends_only_on_the_port(
        self, composition
    ) -> None:
        use_case = composition.judge_use_case

        assert isinstance(use_case, JudgeUseCase)
        assert use_case.is_available is True
        # the port-typed field is exactly what the use-case was given
        assert use_case.judge is composition.judge

    def test_the_judge_is_a_separate_layer_from_the_advisors(
        self, composition
    ) -> None:
        judge = composition.judge

        for advisor in (
            composition.openai_advisor,
            composition.claude_advisor,
            composition.grok_advisor,
        ):
            assert judge is not advisor
            assert not isinstance(advisor, JudgePort)

    def test_the_judge_is_not_wired_into_the_decision_path(
        self, composition
    ) -> None:
        judge = composition.judge

        assert judge is not composition.worker
        for collaborator in (
            composition.orchestrator,
            composition.scheduler,
            composition.realization_control,
            composition.realization_adapter,
        ):
            assert judge is not collaborator
            assert "judge" not in {entry for entry in dir(collaborator)}

    def test_the_judge_model_is_configuration_not_architecture_truth(
        self, composition, monkeypatch
    ) -> None:
        monkeypatch.delenv(OPENAI_JUDGE_MODEL_ENV_VAR, raising=False)

        assert composition.judge.model == DEFAULT_OPENAI_JUDGE_MODEL
        monkeypatch.setenv(
            OPENAI_JUDGE_MODEL_ENV_VAR, "judge-from-the-environment"
        )
        # the model is resolved at construction, so the composed adapter keeps it
        assert composition.judge.model == DEFAULT_OPENAI_JUDGE_MODEL

    def test_another_judge_implementation_can_be_injected(
        self,
    ) -> None:
        """Composition picks the provider; the use-case only sees JudgePort."""
        seen: list = []

        class _RecordingJudge:
            def judge(self, conflict):
                seen.append(conflict)
                return Decision(
                    id="judge-external-1",
                    status=DecisionStatus.ABSTAIN,
                    decision="external judge abstained",
                    rationale="a stand-in provider",
                )

        use_case = JudgeUseCase(judge=_RecordingJudge())

        assert use_case.judge is not None
        assert use_case.resolve(EvidenceView()).count == 0
        assert seen == []


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


class TestReadOnlyReporting:
    """Step 18: the projection and the Excel export are read-only."""

    def test_the_composition_wires_the_projection_and_the_export(
        self, composition
    ) -> None:
        assert isinstance(composition.report_builder, ReportBuilder)
        assert isinstance(composition.excel_reporting, ReportingPort)
        assert isinstance(composition.excel_reporting, ExcelReportingAdapter)

    def test_the_report_directory_defaults_to_its_own_folder(
        self, config
    ) -> None:
        assert DEFAULT_REPORT_DIR == Path("reports")
        assert DEFAULT_REPORT_DIR != DEFAULT_EXCHANGE_DIR
        assert config.report_dir == config.exchange_dir.parent / "reports"
        assert config.to_dict()["report_dir"] == str(config.report_dir)

    def test_a_custom_report_directory_is_honoured(self, config) -> None:
        custom = replace(
            config, report_dir=config.exchange_dir.parent / "artifacts"
        )

        composition = compose(custom)
        try:
            assert composition.excel_reporting.output_dir == custom.report_dir
        finally:
            composition.close()

    def test_the_projection_reflects_the_persisted_state(
        self, composition
    ) -> None:
        composition.storage.steps.upsert(make_step())

        snapshot = composition.report_builder.build()

        assert isinstance(snapshot, ReportSnapshot)
        assert snapshot.project.name == PROJECT_NAME
        assert [step.step_no for step in snapshot.steps] == [9]
        assert snapshot.architecture is not None
        assert snapshot.health.step_count == 1
        assert snapshot.cost.record_count == 0

    def test_the_projection_reads_the_composed_cost_plugin(
        self, composition
    ) -> None:
        composition.storage.steps.upsert(make_step())
        composition.cost_plugin.record(
            CostRecord(
                provider="openai",
                event_id="openai:chatcmpl-composed",
                input_tokens=10,
                output_tokens=2,
                cost_usd=0.5,
                pricing_known=True,
                project=PROJECT_NAME,
                step_no=9,
                created_at=FIXED,
            )
        )

        snapshot = composition.report_builder.build()

        assert snapshot.cost.record_count == 1
        assert snapshot.cost.total_usd == 0.5
        assert snapshot.cost.priced_record_count == 1
        assert [
            (step_no, summary.record_count)
            for step_no, summary in snapshot.cost_by_step
        ] == [(9, 1)]

    def test_the_excel_export_renders_the_real_snapshot(
        self, composition
    ) -> None:
        composition.storage.steps.upsert(make_step())

        locator = composition.excel_reporting.render(
            composition.report_builder.build().to_dict()
        )

        artifact = Path(locator)
        assert artifact.exists()
        assert artifact.suffix == ".xlsx"
        assert artifact.parent == composition.config.report_dir.resolve()

    def test_the_export_changes_no_domain_state(self, composition) -> None:
        composition.storage.steps.upsert(make_step())
        steps_before = composition.storage.steps.list()
        projects_before = composition.storage.projects.list()
        audit_before = composition.storage.audit.list()

        composition.excel_reporting.render(
            composition.report_builder.build().to_dict()
        )

        assert composition.storage.steps.list() == steps_before
        assert composition.storage.projects.list() == projects_before
        assert composition.storage.audit.list() == audit_before
        assert composition.cost_plugin.query(CostQuery()) == CostSummary()

    def test_the_projection_is_not_part_of_the_decision_path(
        self, composition
    ) -> None:
        builder = composition.report_builder

        for collaborator in (
            composition.orchestrator,
            composition.scheduler,
            composition.realization_control,
        ):
            assert builder is not collaborator
            assert "report" not in dir(collaborator)

    def test_the_deterministic_loop_is_unaffected_by_reporting(
        self, composition
    ) -> None:
        composition.storage.steps.upsert(make_step())

        run = composition.run_until_idle()

        assert run.stopped_because == "worker-report-pending"
        composition.excel_reporting.render(
            composition.report_builder.build().to_dict()
        )
        assert composition.health().current_step_no == 9


class TestReadOnlyMonitor:
    """Step 19: the monitor is a pull-based read view and nothing else."""

    def test_the_composition_wires_the_monitor(self, composition) -> None:
        assert isinstance(composition.monitor, Monitor)

    def test_the_monitor_is_handed_the_projection_and_nothing_else(
        self, composition
    ) -> None:
        """No write capability can reach it: it holds only the projection."""
        state = vars(composition.monitor)

        assert set(state) == {"_report_builder"}
        assert state["_report_builder"] is composition.report_builder

    def test_the_monitor_reads_the_composed_state(self, composition) -> None:
        composition.storage.steps.upsert(make_step())

        assert composition.monitor.architecture_version() == (
            ARCHITECTURE_CURRENT.version
        )
        assert composition.monitor.step_states() == ((9, "READY"),)
        assert composition.monitor.current_step().step_no == 9
        assert composition.monitor.next_step() is None
        assert composition.monitor.blocking_steps() == ()
        assert len(composition.monitor.open_risks()) == 11
        assert composition.monitor.cost_summary().record_count == 0
        assert composition.monitor.snapshot().project.name == PROJECT_NAME
        assert composition.monitor.health().current_step_no == 9

    def test_the_monitor_reports_the_persisted_change_requests(
        self, composition
    ) -> None:
        requests = composition.monitor.change_requests()

        assert [request.request_id for request in requests] == [V1_1_ACR_ID]
        assert requests[0].status is ACRStatus.APPLIED
        payload = composition.monitor.to_dict()
        assert payload["change_requests"][0]["request_id"] == V1_1_ACR_ID

    def test_a_full_read_pass_changes_no_domain_state(self, composition) -> None:
        composition.storage.steps.upsert(make_step())
        steps_before = composition.storage.steps.list()
        projects_before = composition.storage.projects.list()
        audit_before = composition.storage.audit.list()
        requests_before = composition.storage.change_requests.list()

        monitor = composition.monitor
        monitor.snapshot()
        monitor.to_dict()
        monitor.health()
        monitor.current_step()
        monitor.next_step()
        monitor.step_states()
        monitor.blocking_steps()
        monitor.open_risks()
        monitor.change_requests()
        monitor.cost_summary()
        monitor.architecture_version()

        assert composition.storage.steps.list() == steps_before
        assert composition.storage.projects.list() == projects_before
        assert composition.storage.audit.list() == audit_before
        assert composition.storage.change_requests.list() == requests_before
        assert composition.cost_plugin.query(CostQuery()) == CostSummary()

    def test_the_monitor_is_not_part_of_the_decision_path(
        self, composition
    ) -> None:
        monitor = composition.monitor

        for collaborator in (
            composition.orchestrator,
            composition.scheduler,
            composition.realization_control,
        ):
            assert monitor is not collaborator
            assert "monitor" not in dir(collaborator)

    def test_a_monitor_read_advances_no_loop_step(self, composition) -> None:
        composition.storage.steps.upsert(make_step())

        composition.monitor.snapshot()
        composition.monitor.blocking_steps()

        health = composition.health()
        assert health.current_step_no == 9
        assert health.current_state is StepState.READY
        assert composition.storage.steps.list()[0].state is StepState.READY


class TestHumanOverride:
    """Step 20: the one controlled human write path, wired apart from READ."""

    def test_the_composition_wires_the_override(self, composition) -> None:
        assert isinstance(composition.human_override, HumanOverride)

    def test_read_and_write_are_wired_separately(self, composition) -> None:
        """READ and CONTROLLED WRITE never hold each other."""
        assert composition.monitor is not composition.human_override
        assert set(vars(composition.monitor)) == {"_report_builder"}
        assert set(vars(composition.human_override)) == {
            "_projects",
            "_steps",
            "_audit",
            "_transactions",
            "_clock",
        }

    def test_the_monitor_gains_no_mutation_capability(
        self, composition
    ) -> None:
        monitor = composition.monitor

        for capability in (
            "pause_project",
            "resume_project",
            "set_mode",
            "unblock_step",
            "resolve_step",
            "abort_step",
            "actor",
            "reason",
            "human_override",
        ):
            assert not hasattr(monitor, capability), capability

    def test_the_override_is_not_part_of_the_decision_path(
        self, composition
    ) -> None:
        for collaborator in (
            composition.orchestrator,
            composition.scheduler,
            composition.realization_control,
            composition.monitor,
        ):
            assert "human_override" not in dir(collaborator)
        assert "report_builder" not in dir(composition.human_override)
        assert "monitor" not in dir(composition.human_override)

    def test_pause_stops_the_loop_and_resume_releases_it(
        self, composition
    ) -> None:
        composition.storage.steps.upsert(make_step())

        composition.human_override.pause_project(
            actor="operator", reason="maintenance window"
        )

        assert composition.run_until_idle().stopped_because == "project-paused"
        assert composition.monitor.health().project_paused is True

        composition.human_override.resume_project(
            actor="operator", reason="maintenance finished"
        )

        assert (
            composition.run_until_idle().stopped_because
            == "worker-report-pending"
        )
        assert composition.monitor.health().project_paused is False

    def test_set_mode_changes_the_approval_policy(self, composition) -> None:
        composition.storage.steps.upsert(make_step())

        composition.human_override.set_mode(
            Mode.MANUAL, actor="operator", reason="audit week"
        )

        assert (
            composition.run_until_idle().stopped_because
            == "human-approval-required"
        )
        assert (
            composition.monitor.health().current_state
            is StepState.WAITING_APPROVAL
        )

    def test_a_mode_change_does_not_release_a_waiting_step(
        self, composition
    ) -> None:
        """The policy gates dispatch - it never releases a waiting step.

        Once a step is ``WAITING_APPROVAL`` only a human event can move it: the
        authoritative FSM has no "release" transition, so switching back to
        ``AUTO`` changes nothing. The human exits are the approval gate
        (``APPROVE``/``REJECT``) and this module's ``abort_step``; a policy
        change is deliberately not one of them.
        """
        composition.storage.steps.upsert(make_step())
        composition.human_override.set_mode(
            Mode.MANUAL, actor="operator", reason="audit week"
        )
        assert (
            composition.run_until_idle().stopped_because
            == "human-approval-required"
        )

        composition.human_override.set_mode(
            Mode.AUTO, actor="operator", reason="audit week over"
        )

        assert (
            composition.run_until_idle().stopped_because
            == "human-approval-required"
        )
        assert (
            composition.monitor.health().current_state
            is StepState.WAITING_APPROVAL
        )
        assert [
            step.step_no for step in composition.monitor.blocking_steps()
        ] == [9]

        composition.human_override.abort_step(
            9, actor="operator", reason="approval withdrawn"
        )

        assert composition.run_until_idle().stopped_because == "step-aborted"

    def test_the_human_events_clear_the_states_the_monitor_reports(
        self, composition
    ) -> None:
        composition.storage.steps.upsert(make_step(state=StepState.BLOCKED))

        assert [
            step.step_no for step in composition.monitor.blocking_steps()
        ] == [9]

        composition.human_override.unblock_step(
            9, actor="operator", reason="dependency resolved"
        )

        assert composition.monitor.blocking_steps() == ()
        assert composition.monitor.health().current_state is StepState.READY
        assert (
            composition.run_until_idle().stopped_because
            == "worker-report-pending"
        )

    def test_abort_step_stops_the_project(self, composition) -> None:
        composition.storage.steps.upsert(make_step(state=StepState.BLOCKED))

        composition.human_override.abort_step(
            9, actor="operator", reason="no longer required"
        )

        assert composition.monitor.step_states() == ((9, "ABORTED"),)
        assert composition.run_until_idle().stopped_because == "step-aborted"
        assert composition.health().complete is False

    def test_every_override_writes_exactly_one_audit_entry(
        self, composition
    ) -> None:
        composition.storage.steps.upsert(make_step(state=StepState.BLOCKED))
        before = len(composition.storage.audit.list())

        composition.human_override.pause_project(
            actor="operator", reason="pause"
        )
        assert len(composition.storage.audit.list()) == before + 1

        composition.human_override.resume_project(
            actor="operator", reason="resume"
        )
        assert len(composition.storage.audit.list()) == before + 2

        composition.human_override.set_mode(
            Mode.SUPERVISED, actor="operator", reason="mode"
        )
        assert len(composition.storage.audit.list()) == before + 3

        composition.human_override.unblock_step(
            9, actor="operator", reason="unblock"
        )
        assert len(composition.storage.audit.list()) == before + 4

    def test_the_override_is_recorded_with_actor_and_reason(
        self, composition
    ) -> None:
        composition.human_override.pause_project(
            actor="operator", reason="maintenance"
        )

        (entry,) = composition.storage.audit.list_for_entity(
            AuditEntityType.PROJECT, PROJECT_NAME
        )

        assert entry.action is AuditAction.PAUSE
        assert entry.detail["operation"] == "pause_project"
        assert entry.detail["actor"] == "operator"
        assert entry.detail["reason"] == "maintenance"
        assert entry.detail["paused_before"] is False
        assert entry.detail["paused_after"] is True
        assert entry.created_at == FIXED


class TestMarkdownExport:
    """Step 21: the markdown document is the second renderer of one snapshot."""

    def test_the_composition_wires_the_markdown_export(self, composition) -> None:
        assert isinstance(composition.markdown_reporting, ReportingPort)
        assert isinstance(
            composition.markdown_reporting, MarkdownReportingAdapter
        )

    def test_both_renderers_write_into_the_same_report_directory(
        self, composition, config
    ) -> None:
        assert composition.markdown_reporting.output_dir == config.report_dir
        assert (
            composition.markdown_reporting.output_dir
            == composition.excel_reporting.output_dir
        )

    def test_the_markdown_export_renders_the_real_snapshot(
        self, composition
    ) -> None:
        composition.storage.steps.upsert(make_step())

        locator = composition.markdown_reporting.render(
            composition.report_builder.build().to_dict()
        )

        artifact = Path(locator)
        assert artifact.exists()
        assert artifact.suffix == ".md"
        assert artifact.parent == composition.config.report_dir.resolve()

        document = artifact.read_text(encoding="utf-8")
        assert document.startswith(f"# {PROJECT_NAME}\n")
        assert ARCHITECTURE_CURRENT.version in document
        assert [f"## {name}" for name in SECTION_NAMES] == [
            line for line in document.splitlines() if line.startswith("## ")
        ]

    def test_the_two_artifacts_coexist_for_one_snapshot(
        self, composition
    ) -> None:
        composition.storage.steps.upsert(make_step())
        payload = composition.report_builder.build().to_dict()

        excel = Path(composition.excel_reporting.render(payload))
        markdown = Path(composition.markdown_reporting.render(payload))

        assert excel.parent == markdown.parent
        assert excel.suffix == ".xlsx"
        assert markdown.suffix == ".md"
        assert excel.stem == markdown.stem  # one snapshot, one name, two formats

    def test_the_markdown_export_changes_no_domain_state(
        self, composition
    ) -> None:
        composition.storage.steps.upsert(make_step())
        steps_before = composition.storage.steps.list()
        projects_before = composition.storage.projects.list()
        audit_before = composition.storage.audit.list()
        requests_before = composition.storage.change_requests.list()

        composition.markdown_reporting.render(
            composition.report_builder.build().to_dict()
        )

        assert composition.storage.steps.list() == steps_before
        assert composition.storage.projects.list() == projects_before
        assert composition.storage.audit.list() == audit_before
        assert composition.storage.change_requests.list() == requests_before
        assert composition.cost_plugin.query(CostQuery()) == CostSummary()

    def test_the_markdown_export_is_not_part_of_the_decision_path(
        self, composition
    ) -> None:
        for collaborator in (
            composition.orchestrator,
            composition.scheduler,
            composition.realization_control,
            composition.monitor,
            composition.human_override,
        ):
            assert "markdown" not in dir(collaborator)
        assert "report_builder" not in dir(composition.markdown_reporting)

    def test_the_markdown_document_reflects_a_human_override(
        self, composition
    ) -> None:
        """The document reads the projection, so it shows the overridden state."""
        composition.human_override.pause_project(
            actor="operator", reason="maintenance"
        )

        locator = composition.markdown_reporting.render(
            composition.report_builder.build().to_dict()
        )

        document = Path(locator).read_text(encoding="utf-8")
        assert "| Paused | true |" in document


class TestApprovalGateComposition:
    """Step 23: the approval gate is wired as its own human path for APPROVE."""

    def test_the_composition_wires_the_approval_gate(self, composition) -> None:
        assert isinstance(composition.approval, ApprovalGate)

    def test_the_gate_holds_only_ports_it_needs(self, composition) -> None:
        assert set(vars(composition.approval)) == {
            "_steps",
            "_tasks",
            "_audit",
            "_transactions",
            "_context_builder",
            "_worker",
            "_instructions",
            "_report_schema",
            "_clock",
        }

    def test_the_gate_cannot_reach_read_architecture_or_override(
        self, composition
    ) -> None:
        gate = composition.approval

        for missing in (
            "monitor",
            "report_builder",
            "realization_control",
            "architecture_versions",
            "adrs",
            "change_requests",
            "human_override",
            "cost_plugin",
            "pause_project",
            "unblock_step",
            "resolve_step",
            "abort_step",
            "set_mode",
        ):
            assert not hasattr(gate, missing), missing

    def test_the_gate_is_not_part_of_the_decision_path(
        self, composition
    ) -> None:
        for collaborator in (
            composition.orchestrator,
            composition.scheduler,
            composition.realization_control,
            composition.monitor,
            composition.human_override,
        ):
            assert "approval" not in dir(collaborator)

    def test_an_approved_step_runs_through_the_loop(self, composition) -> None:
        """The gate realizes the FSM contract; the loop then takes over."""
        composition.human_override.set_mode(
            Mode.MANUAL, actor="operator", reason="audit week"
        )
        composition.storage.steps.upsert(make_step())

        assert (
            composition.run_until_idle().stopped_because
            == "human-approval-required"
        )

        approved = composition.approval.approve(
            9, actor="operator", reason="plan reviewed"
        )

        assert approved.state is StepState.DISPATCHED
        assert (
            composition.run_until_idle().stopped_because
            == "worker-report-pending"
        )
        assert (
            composition.monitor.health().current_state
            is StepState.CLINE_WORKING
        )

