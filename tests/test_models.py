"""Unit tests for the frozen domain models.

Coverage: construction, defaults, validation errors, immutability and
deterministic ``to_dict`` / ``from_dict`` round-trips for every model.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timezone
from enum import Enum

import pytest

from architecture_assistant.domain.enums import (
    ACRStatus,
    ADRStatus,
    DecisionStatus,
    Mode,
    Phase,
    ReportStatus,
    RiskLevel,
    RiskStatus,
    Severity,
    StepState,
    TaskState,
)
from architecture_assistant.domain.models import (
    ADR,
    ArchitectureChangeRequest,
    ArchitectureVersion,
    Decision,
    DomainModel,
    Finding,
    Project,
    Risk,
    Step,
    Task,
    utc_now,
)

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)

SAMPLES = [
    Project(
        name="Architecture Lifecycle Assistant",
        plan_version="0.2",
        plan_hash="70c12e690df0",
        mode=Mode.SUPERVISED,
        paused=True,
        current_step_no_snapshot=3,
        created_at=NOW,
        updated_at=NOW,
    ),
    Step(
        step_no=7,
        phase=Phase.CLINE,
        title="Cline Worker Adapter",
        description="Structured contract",
        state=StepState.WAITING_APPROVAL,
        attempt=1,
        max_attempts=3,
        risk=RiskLevel.HIGH,
        requires_human=True,
        created_at=NOW,
        started_at=NOW,
        finished_at=None,
        verified_at=None,
        last_update_at=NOW,
    ),
    Task(
        step_no=1,
        phase=Phase.FOUNDATION,
        title="Domain modeļi + FSM",
        description="Domain models and deterministic FSM",
        risk=RiskLevel.MEDIUM,
        attempt=1,
        max_attempts=3,
        state=TaskState.DISPATCHED,
        context_file=".mini_build/context/step_001_context.json",
        instructions={"plan_first": True, "scope": "Only current step"},
        report_schema={"status": "DONE|REVISE|BLOCKED|FAILED"},
        report_status=ReportStatus.DONE,
        created_at=NOW,
    ),
    ArchitectureVersion(
        version="1.0",
        baseline="Layered assistant core",
        rules=("no-sqlite-in-domain", "no-api-in-domain"),
        superseded_by=None,
        is_current=True,
        created_at=NOW,
    ),
    ADR(
        id="ADR-001",
        title="Use SQLite as source of truth",
        status=ADRStatus.ACCEPTED,
        context="State must survive restarts",
        decision="SQLite is authoritative; Excel is an export",
        consequences=("Crash-safe state", "No Excel editing authority"),
        version=2,
        superseded_by=None,
        related=("ADR-002",),
        created_at=NOW,
        updated_at=NOW,
    ),
    Risk(
        id="RISK-001",
        description="Architecture drift during long builds",
        severity=Severity.HIGH,
        probability=0.25,
        impact=Severity.CRITICAL,
        owner="architect",
        mitigation="Deterministic architecture validator",
        status=RiskStatus.MITIGATED,
        created_at=NOW,
        updated_at=None,
    ),
    Finding(
        id="F-001",
        source="claude",
        claim="Domain layer leaks persistence concerns",
        evidence=("models.py imports sqlite3", "no repository port"),
        confidence=0.8,
        severity=Severity.HIGH,
        step_no=3,
        created_at=NOW,
    ),
    Decision(
        id="D-001",
        status=DecisionStatus.ACCEPTED,
        decision="Reject the finding",
        rationale="No persistence import present in the domain layer",
        rules_applied=("RULE-DOMAIN-01",),
        evidence_refs=("F-001",),
        perspectives=("openai", "claude", "grok"),
        step_no=3,
        created_at=NOW,
    ),
    ArchitectureChangeRequest(
        request_id="ACR-001",
        title="Admit the composition root as architecture v1.1",
        rationale="The object graph could only ever be wired by the test suite.",
        source_version="1.0",
        target_version="1.1",
        rule_ids=("unknown-layer", "forbidden-layer-import"),
        adr_id="ADR-008",
        roadmap_impact=("step-009", "step-010", "step-011"),
        status=ACRStatus.APPLIED,
        created_at=NOW,
        approved_by="user",
        approved_at=NOW,
        applied_at=NOW,
    ),
]

SAMPLE_IDS = [type(sample).__name__ for sample in SAMPLES]


def test_all_required_models_are_covered() -> None:
    """The eight Step 1 models plus the Step 11 change request."""
    assert {type(sample).__name__ for sample in SAMPLES} == {
        "Project",
        "Step",
        "Task",
        "ArchitectureVersion",
        "ADR",
        "Risk",
        "Finding",
        "Decision",
        "ArchitectureChangeRequest",
    }


def test_every_model_is_a_frozen_dataclass() -> None:
    for sample in SAMPLES:
        assert isinstance(sample, DomainModel)
        assert fields(sample)
        count = len(sample.to_dict())
        assert count == len(fields(sample))


@pytest.mark.parametrize("sample", SAMPLES, ids=SAMPLE_IDS)
def test_to_dict_is_json_serializable(sample: DomainModel) -> None:
    data = sample.to_dict()
    assert json.loads(json.dumps(data)) == data


@pytest.mark.parametrize("sample", SAMPLES, ids=SAMPLE_IDS)
def test_to_dict_contains_no_enum_or_tuple_values(sample: DomainModel) -> None:
    for name, value in sample.to_dict().items():
        assert not isinstance(value, Enum), name
        assert not isinstance(value, tuple), name


@pytest.mark.parametrize("sample", SAMPLES, ids=SAMPLE_IDS)
def test_to_dict_from_dict_round_trip(sample: DomainModel) -> None:
    rebuilt = type(sample).from_dict(sample.to_dict())
    assert rebuilt == sample
    assert rebuilt.to_dict() == sample.to_dict()


@pytest.mark.parametrize("sample", SAMPLES, ids=SAMPLE_IDS)
def test_models_are_immutable(sample: DomainModel) -> None:
    first_field = fields(sample)[0].name
    with pytest.raises(FrozenInstanceError):
        setattr(sample, first_field, "mutated")


@pytest.mark.parametrize("sample", SAMPLES, ids=SAMPLE_IDS)
def test_round_trip_is_deterministic(sample: DomainModel) -> None:
    assert sample.to_dict() == sample.to_dict()


VALIDATION_CASES = [
    (
        "project-empty-name",
        lambda: Project(name="", plan_version="0.2"),
        "name must be a non-empty string",
    ),
    (
        "project-empty-plan-version",
        lambda: Project(name="p", plan_version="  "),
        "plan_version must be a non-empty string",
    ),
    (
        "project-unknown-mode",
        lambda: Project(name="p", plan_version="0.2", mode="BOGUS"),
        "mode must be one of",
    ),
    (
        "project-non-bool-paused",
        lambda: Project(name="p", plan_version="0.2", paused="yes"),
        "paused must be a bool",
    ),
    (
        "project-snapshot-zero",
        lambda: Project(name="p", plan_version="0.2", current_step_no_snapshot=0),
        "current_step_no_snapshot must be >= 1",
    ),
    (
        "step-zero-number",
        lambda: Step(step_no=0, phase=Phase.FOUNDATION, title="t"),
        "step_no must be >= 1",
    ),
    (
        "step-unknown-phase",
        lambda: Step(step_no=1, phase="NOPE", title="t"),
        "phase must be one of",
    ),
    (
        "step-empty-title",
        lambda: Step(step_no=1, phase=Phase.FOUNDATION, title=""),
        "title must be a non-empty string",
    ),
    (
        "step-unknown-state",
        lambda: Step(step_no=1, phase=Phase.FOUNDATION, title="t", state="DONE"),
        "state must be one of",
    ),
    (
        "step-attempt-over-limit",
        lambda: Step(
            step_no=1, phase=Phase.FOUNDATION, title="t", attempt=4, max_attempts=3
        ),
        "must be <= max_attempts",
    ),
    (
        "step-negative-attempt",
        lambda: Step(step_no=1, phase=Phase.FOUNDATION, title="t", attempt=-1),
        "attempt must be >= 0",
    ),
    (
        "step-non-bool-requires-human",
        lambda: Step(
            step_no=1, phase=Phase.FOUNDATION, title="t", requires_human="yes"
        ),
        "requires_human must be a bool",
    ),
    (
        "task-zero-attempt",
        lambda: Task(step_no=1, phase=Phase.FOUNDATION, title="t", attempt=0),
        "attempt must be >= 1",
    ),
    (
        "task-unknown-report-status",
        lambda: Task(
            step_no=1, phase=Phase.FOUNDATION, title="t", report_status="BOGUS"
        ),
        "report_status must be one of",
    ),
    (
        "architecture-version-superseded-but-current",
        lambda: ArchitectureVersion(
            version="1.0", superseded_by="2.0", is_current=True
        ),
        "must have is_current=False",
    ),
    (
        "architecture-version-bare-string-rules",
        lambda: ArchitectureVersion(version="1.0", rules="RULE-1"),
        "must be a sequence of values",
    ),
    (
        "adr-superseded-without-reference",
        lambda: ADR(id="ADR-001", title="t", status=ADRStatus.SUPERSEDED),
        "requires superseded_by",
    ),
    (
        "adr-empty-id",
        lambda: ADR(id="", title="t"),
        "id must be a non-empty string",
    ),
    (
        "risk-probability-out-of-range",
        lambda: Risk(id="R-1", description="d", probability=1.5),
        "probability must be between 0.0 and 1.0",
    ),
    (
        "risk-empty-description",
        lambda: Risk(id="R-1", description=""),
        "description must be a non-empty string",
    ),
    (
        "finding-confidence-out-of-range",
        lambda: Finding(id="F-1", source="s", claim="c", confidence=-0.1),
        "confidence must be between 0.0 and 1.0",
    ),
    (
        "finding-empty-source",
        lambda: Finding(id="F-1", source="", claim="c"),
        "source must be a non-empty string",
    ),
    (
        "decision-accepted-without-text",
        lambda: Decision(id="D-1", status=DecisionStatus.ACCEPTED),
        "requires a non-empty decision",
    ),
    (
        "decision-bare-string-perspectives",
        lambda: Decision(id="D-1", perspectives="openai"),
        "must be a sequence of values",
    ),
    (
        "acr-same-source-and-target",
        lambda: ArchitectureChangeRequest(
            request_id="ACR-001",
            title="t",
            rationale="r",
            source_version="1.0",
            target_version="1.0",
            rule_ids=("r1",),
            adr_id="ADR-008",
        ),
        "must differ from source_version",
    ),
    (
        "acr-without-rules",
        lambda: ArchitectureChangeRequest(
            request_id="ACR-001",
            title="t",
            rationale="r",
            source_version="1.0",
            target_version="1.1",
            rule_ids=(),
            adr_id="ADR-008",
        ),
        "at least one rule id",
    ),
    (
        "acr-empty-request-id",
        lambda: ArchitectureChangeRequest(
            request_id="",
            title="t",
            rationale="r",
            source_version="1.0",
            target_version="1.1",
            rule_ids=("r1",),
            adr_id="ADR-008",
        ),
        "request_id must be a non-empty string",
    ),
    (
        "acr-approved-without-approver",
        lambda: ArchitectureChangeRequest(
            request_id="ACR-001",
            title="t",
            rationale="r",
            source_version="1.0",
            target_version="1.1",
            rule_ids=("r1",),
            adr_id="ADR-008",
            status=ACRStatus.APPROVED,
            approved_at=NOW,
        ),
        "requires approved_by",
    ),
    (
        "acr-proposed-with-approval",
        lambda: ArchitectureChangeRequest(
            request_id="ACR-001",
            title="t",
            rationale="r",
            source_version="1.0",
            target_version="1.1",
            rule_ids=("r1",),
            adr_id="ADR-008",
            approved_by="user",
        ),
        "must not carry approval fields",
    ),
    (
        "acr-applied-without-applied-at",
        lambda: ArchitectureChangeRequest(
            request_id="ACR-001",
            title="t",
            rationale="r",
            source_version="1.0",
            target_version="1.1",
            rule_ids=("r1",),
            adr_id="ADR-008",
            status=ACRStatus.APPLIED,
            approved_by="user",
            approved_at=NOW,
        ),
        "requires applied_at",
    ),
    (
        "acr-rejected-with-applied-at",
        lambda: ArchitectureChangeRequest(
            request_id="ACR-001",
            title="t",
            rationale="r",
            source_version="1.0",
            target_version="1.1",
            rule_ids=("r1",),
            adr_id="ADR-008",
            status=ACRStatus.REJECTED,
            applied_at=NOW,
        ),
        "must not carry applied_at",
    ),
]

VALIDATION_IDS = [case[0] for case in VALIDATION_CASES]


@pytest.mark.parametrize(
    "factory,message", [case[1:] for case in VALIDATION_CASES], ids=VALIDATION_IDS
)
def test_validation_errors(factory, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        factory()


def test_mode_enum_has_manual_supervised_auto() -> None:
    assert {member.value for member in Mode} == {"MANUAL", "SUPERVISED", "AUTO"}


def test_step_state_enum_matches_master_workflow() -> None:
    assert {member.value for member in StepState} == {
        "PENDING",
        "READY",
        "WAITING_APPROVAL",
        "DISPATCHED",
        "CLINE_WORKING",
        "REPORT_RECEIVED",
        "REVIEWING",
        "REVISE",
        "BLOCKED",
        "CONFLICT",
        "FAILED",
        "VERIFIED",
        "ABORTED",
    }



class TestDefaults:
    def test_project_defaults(self) -> None:
        project = Project(name="p", plan_version="0.2")
        assert project.mode is Mode.MANUAL
        assert project.paused is False
        assert project.plan_hash == ""
        assert project.current_step_no_snapshot is None
        assert project.created_at.tzinfo is not None
        assert project.updated_at.tzinfo is not None

    def test_step_defaults(self) -> None:
        step = Step(step_no=1, phase=Phase.FOUNDATION, title="t")
        assert step.state is StepState.PENDING
        assert step.attempt == 0
        assert step.max_attempts == 3
        assert step.risk is RiskLevel.LOW
        assert step.requires_human is False
        assert step.description == ""
        assert step.started_at is None
        assert step.can_retry is True

    def test_step_cannot_retry_at_attempt_limit(self) -> None:
        step = Step(
            step_no=1, phase=Phase.FOUNDATION, title="t", attempt=3, max_attempts=3
        )
        assert step.can_retry is False

    def test_task_defaults(self) -> None:
        task = Task(step_no=1, phase=Phase.FOUNDATION, title="t")
        assert task.state is TaskState.CREATED
        assert task.report_status is None
        assert task.instructions == {}
        assert task.report_schema == {}
        assert task.attempt == 1
        assert task.risk is RiskLevel.MEDIUM

    def test_risk_defaults(self) -> None:
        risk = Risk(id="R-1", description="d")
        assert risk.severity is Severity.MEDIUM
        assert risk.impact is Severity.MEDIUM
        assert risk.status is RiskStatus.OPEN
        assert risk.probability == 0.5

    def test_decision_defaults(self) -> None:
        decision = Decision(id="D-1")
        assert decision.status is DecisionStatus.PENDING
        assert decision.rules_applied == ()
        assert decision.evidence_refs == ()
        assert decision.perspectives == ()

    def test_utc_now_is_timezone_aware(self) -> None:
        assert utc_now().tzinfo is not None


class TestProjectCurrentStepIsNotAuthoritative:
    def test_field_is_named_snapshot(self) -> None:
        assert "current_step_no_snapshot" in {
            model_field.name for model_field in fields(Project)
        }

    def test_alias_returns_snapshot_value(self) -> None:
        project = Project(
            name="p", plan_version="0.2", current_step_no_snapshot=5
        )
        assert project.current_step_no == 5

    def test_model_docstring_states_non_authoritative(self) -> None:
        doc = (Project.__doc__ or "").lower()
        assert "non-authoritative" in doc


class TestEnumCoercion:
    def test_step_coerces_strings_to_enums(self) -> None:
        step = Step(
            step_no=1, phase="FOUNDATION", title="t", state="READY", risk="HIGH"
        )
        assert step.phase is Phase.FOUNDATION
        assert step.state is StepState.READY
        assert step.risk is RiskLevel.HIGH

    def test_project_coerces_mode_string(self) -> None:
        assert Project(name="p", plan_version="0.2", mode="AUTO").mode is Mode.AUTO

    def test_sequences_are_frozen_to_tuples(self) -> None:
        version = ArchitectureVersion(version="1.0", rules=["a", "b"])
        assert version.rules == ("a", "b")
        assert isinstance(version.rules, tuple)

    def test_finding_evidence_is_immutable(self) -> None:
        finding = Finding(id="F-1", source="s", claim="c", evidence=["e1"])
        assert isinstance(finding.evidence, tuple)


class TestFromDict:
    def test_missing_keys_fall_back_to_defaults(self) -> None:
        step = Step.from_dict(
            {"step_no": 2, "phase": "FOUNDATION", "title": "Context Manager"}
        )
        assert step.step_no == 2
        assert step.state is StepState.PENDING
        assert step.max_attempts == 3

    def test_unknown_keys_are_ignored(self) -> None:
        step = Step.from_dict(
            {
                "step_no": 2,
                "phase": "FOUNDATION",
                "title": "Context Manager",
                "future_field": "ignored",
            }
        )
        assert step.step_no == 2
        assert step.title == "Context Manager"

    def test_datetime_strings_are_parsed(self) -> None:
        project = Project.from_dict(
            {
                "name": "p",
                "plan_version": "0.2",
                "created_at": "2026-09-20T12:00:00+00:00",
                "updated_at": "2026-09-20T12:00:00+00:00",
            }
        )
        assert project.created_at == NOW

    def test_nested_mappings_are_copied(self) -> None:
        payload = {
            "step_no": 1,
            "phase": "FOUNDATION",
            "title": "t",
            "instructions": {"plan_first": True},
            "report_schema": {"status": "DONE"},
        }
        task = Task.from_dict(payload)
        assert task.instructions == {"plan_first": True}
        assert task.report_schema == {"status": "DONE"}
        assert task.to_dict()["instructions"] == {"plan_first": True}

