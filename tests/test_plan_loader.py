"""Tests for the plan loader: validation, identity, atomicity, idempotence.

The loader is the only way a plan enters the source of truth, so these tests pin
the three promises it makes: an invalid plan writes **nothing**, an import is
**all or nothing**, and a plan can neither touch the project identity nor invent
workflow state. The fakes below mirror the ones used for the human write paths,
and the atomicity tests run against the real SQLite storage, because only a real
transaction can prove a real rollback.
"""

from __future__ import annotations

import ast
import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

from architecture_assistant.application import (
    MAX_ISSUES,
    MAX_PLAN_CHARS,
    MAX_PLAN_STEPS,
    PLAN_SCHEMA_VERSION,
    PlanActorRequiredError,
    PlanConflictError,
    PlanFormatError,
    PlanInvariantError,
    PlanLoader,
    PlanProjectMismatchError,
    PlanReasonRequiredError,
    PlanVersionMismatchError,
    parse_plan,
)
from architecture_assistant.domain.audit import (
    AuditAction,
    AuditEntityType,
    AuditEntry,
)
from architecture_assistant.domain.enums import Mode, Phase, RiskLevel, StepState
from architecture_assistant.domain.fsm import StepStateMachine
from architecture_assistant.domain.models import Project, Step
from architecture_assistant.infrastructure import (
    SqliteStorage,
    close_database,
    open_database,
)

NOW = datetime(2026, 9, 21, 22, 0, tzinfo=timezone.utc)

PROJECT_NAME = "Architecture Lifecycle Assistant"
PLAN_VERSION = "0.3"
ACTOR = "gints"
REASON = "the approved youtube_to_mp3 pilot plan"

#: The module under test - read by the boundary test at the bottom.
MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "architecture_assistant"
    / "application"
    / "plan_loader.py"
)

#: The shipped pilot plan: it must stay loadable, so it is part of the suite.
EXAMPLE_PLAN = (
    Path(__file__).resolve().parents[1]
    / "examples"
    / "plan_youtube_to_mp3.json"
)


def steps_payload(count: int = 2, **overrides: Any) -> list[dict[str, Any]]:
    """A canonical steps array: contiguous, valid, deterministic."""
    phases = (Phase.CONTEXT, Phase.ARCHITECTURE, Phase.CLINE, Phase.TEST)
    return [
        {
            "step_no": number,
            "phase": phases[(number - 1) % len(phases)].value,
            "title": f"Step {number}",
            "risk": RiskLevel.LOW.value,
            "max_attempts": 3,
            "requires_human": number == count,
            **overrides,
        }
        for number in range(1, count + 1)
    ]


def plan_document(**overrides: Any) -> dict[str, Any]:
    """A canonical plan document, with any key replaceable."""
    document: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "plan_version": PLAN_VERSION,
        "project": {"name": PROJECT_NAME, "mode": Mode.MANUAL.value},
        "steps": steps_payload(),
    }
    document.update(overrides)
    return document


def plan_text(**overrides: Any) -> str:
    """The canonical plan as text (what the core receives)."""
    return json.dumps(plan_document(**overrides), indent=2)


def make_project(**overrides: Any) -> Project:
    """The authoritative single project row."""
    data: dict[str, Any] = dict(
        name=PROJECT_NAME,
        plan_version=PLAN_VERSION,
        mode=Mode.MANUAL,
        paused=False,
        created_at=NOW,
        updated_at=NOW,
    )
    data.update(overrides)
    return Project(**data)


def make_step(
    step_no: int = 1, state: StepState = StepState.PENDING, **overrides: Any
) -> Step:
    """An already persisted step, for the conflict tests."""
    data: dict[str, Any] = dict(
        step_no=step_no,
        phase=Phase.CONTEXT,
        title=f"Existing step {step_no}",
        state=state,
        attempt=0,
        max_attempts=3,
        risk=RiskLevel.LOW,
        requires_human=False,
        created_at=NOW,
    )
    data.update(overrides)
    return Step(**data)


def plan_without(*keys: str, **overrides: Any) -> str:
    """The canonical plan text with whole keys removed (not merely null)."""
    document = plan_document(**overrides)
    for key in keys:
        document.pop(key, None)
    return json.dumps(document, indent=2)


def _sha256(text: str) -> str:
    """The plan-file identity, computed independently of the module."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fixed_clock() -> datetime:
    """The injected clock: no test depends on the wall clock."""
    return NOW


# ---------------------------------------------------------------------------
# fakes: three repository ports, one transaction port
# ---------------------------------------------------------------------------


class RecordingTransaction:
    """A transaction port that records the commit/rollback boundary."""

    def __init__(self) -> None:
        self.opened = 0
        self.committed = 0
        self.rolled_back = 0

    @contextmanager
    def transaction(self):
        self.opened += 1
        try:
            yield
        except BaseException:
            self.rolled_back += 1
            raise
        self.committed += 1


class FakeProjectRepository:
    """In-memory project port that counts reads and records any write."""

    def __init__(
        self, projects: tuple = (), *, fail_on_upsert: bool = False
    ) -> None:
        self.items = {project.name: project for project in projects}
        self.reads = 0
        self.upserts = 0
        self._fail = fail_on_upsert

    def upsert(self, project: Project) -> None:
        self.upserts += 1
        if self._fail:
            raise RuntimeError("project write failed")
        self.items[project.name] = project

    def get(self, name: str) -> Optional[Project]:
        self.reads += 1
        return self.items.get(name)

    def list(self) -> tuple:
        self.reads += 1
        return tuple(self.items.values())

    def delete(self, name: str) -> bool:
        return self.items.pop(name, None) is not None


class FakeStepRepository:
    """In-memory step port with an optional write failure."""

    def __init__(
        self, steps: tuple = (), *, fail_on_upsert: bool = False
    ) -> None:
        self.items = {step.step_no: step for step in steps}
        self.upserts = 0
        self._fail = fail_on_upsert

    def upsert(self, step: Step) -> None:
        self.upserts += 1
        if self._fail:
            raise RuntimeError("step write failed")
        self.items[step.step_no] = step

    def get(self, step_no: int) -> Optional[Step]:
        return self.items.get(step_no)

    def list(self) -> tuple:
        return tuple(self.items.values())

    def list_by_state(self, state: StepState) -> tuple:
        return tuple(step for step in self.items.values() if step.state is state)

    def delete(self, step_no: int) -> bool:
        return self.items.pop(step_no, None) is not None


class FakeAuditRepository:
    """Append-only audit port with an optional append failure."""

    def __init__(self, entries: tuple = (), *, fail_on_append: bool = False):
        self.entries = list(entries)
        self.appends = 0
        self._fail = fail_on_append

    def append(self, entry: AuditEntry) -> None:
        self.appends += 1
        if self._fail:
            raise RuntimeError("audit write failed")
        self.entries.append(entry)

    def list(self) -> tuple:
        return tuple(self.entries)

    def list_for_entity(self, entity_type: Any, entity_id: str) -> tuple:
        return tuple(
            entry
            for entry in self.entries
            if entry.entity_type == entity_type and entry.entity_id == entity_id
        )


def build_loader(
    *,
    projects: Any = None,
    steps: Any = None,
    audit: Any = None,
    transactions: Any = None,
    clock: Any = fixed_clock,
) -> PlanLoader:
    """A loader wired to fakes, unless a collaborator is supplied."""
    return PlanLoader(
        projects
        if projects is not None
        else FakeProjectRepository((make_project(),)),
        steps if steps is not None else FakeStepRepository(),
        audit if audit is not None else FakeAuditRepository(),
        transactions
        if transactions is not None
        else RecordingTransaction(),
        clock=clock,
    )


# ---------------------------------------------------------------------------
# a valid plan imports - atomically, in the canonical initial state
# ---------------------------------------------------------------------------


class TestValidImport:
    """A valid plan becomes the project's plan - completely and atomically."""

    def test_a_valid_plan_imports_successfully(self) -> None:
        steps = FakeStepRepository()
        audit = FakeAuditRepository()
        transactions = RecordingTransaction()
        loader = build_loader(
            steps=steps, audit=audit, transactions=transactions
        )

        outcome = loader.import_plan(
            plan_text(), actor=ACTOR, reason=REASON, source_file="plan.json"
        ).to_dict()

        assert outcome["action"] == "import_plan"
        assert outcome["project_name"] == PROJECT_NAME
        assert outcome["step_count"] == 2
        assert outcome["first_step_no"] == 1
        assert outcome["last_step_no"] == 2
        assert outcome["plan_version"] == PLAN_VERSION
        assert outcome["imported_at"] == NOW.isoformat()
        assert sorted(steps.items) == [1, 2]
        assert transactions.opened == 1
        assert transactions.committed == 1
        assert transactions.rolled_back == 0

    def test_imported_steps_use_the_fsm_initial_state(self) -> None:
        steps = FakeStepRepository()
        loader = build_loader(steps=steps)

        loader.import_plan(plan_text(), actor=ACTOR, reason=REASON)

        for step in steps.items.values():
            assert step.state is StepStateMachine.INITIAL_STATE
            assert step.state is StepState.PENDING
            assert step.attempt == 0
            assert step.created_at == NOW
            assert step.started_at is None
            assert step.finished_at is None
            assert step.verified_at is None
            assert step.last_update_at is None

    def test_the_plan_fields_are_mapped_onto_the_domain_step(self) -> None:
        steps = FakeStepRepository()
        loader = build_loader(steps=steps)

        loader.import_plan(plan_text(), actor=ACTOR, reason=REASON)

        step = steps.items[2]
        assert (step.phase, step.title) == (Phase.ARCHITECTURE, "Step 2")
        assert (step.max_attempts, step.risk) == (3, RiskLevel.LOW)
        assert step.requires_human is True
        assert step.description == ""

    def test_the_documented_defaults_apply_when_optional_keys_are_absent(
        self,
    ) -> None:
        minimal = {
            "step_no": 1,
            "phase": "TEST",
            "title": "Only the required fields",
        }
        steps = FakeStepRepository()
        loader = build_loader(steps=steps)
        text = plan_text(steps=[minimal])

        plan = parse_plan(text)
        loader.import_plan(text, actor=ACTOR, reason=REASON)

        assert plan.steps[0].risk is RiskLevel.LOW
        assert plan.steps[0].max_attempts == 3
        assert plan.steps[0].requires_human is False
        assert plan.steps[0].description == ""
        assert steps.items[1].risk is RiskLevel.LOW
        assert steps.items[1].requires_human is False

    def test_exactly_one_plan_audit_entry_is_written(self) -> None:
        audit = FakeAuditRepository()
        steps = FakeStepRepository()
        loader = build_loader(steps=steps, audit=audit)
        text = plan_text()

        loader.import_plan(
            text, actor=ACTOR, reason=REASON, source_file="p.json"
        )

        assert steps.upserts == 2
        assert audit.appends == 1
        (entry,) = audit.entries
        assert entry.entity_type is AuditEntityType.PLAN
        assert entry.action is AuditAction.IMPORT
        assert entry.entity_id == _sha256(text)
        assert entry.created_at == NOW
        detail = dict(entry.detail)
        assert detail["operation"] == "load-plan"
        assert detail["project_name"] == PROJECT_NAME
        assert detail["actor"] == ACTOR
        assert detail["reason"] == REASON
        assert detail["source_file"] == "p.json"
        assert detail["plan_version"] == PLAN_VERSION
        assert detail["schema_version"] == PLAN_SCHEMA_VERSION
        assert detail["plan_hash"] == _sha256(text)
        assert detail["step_count"] == 2
        assert detail["first_step_no"] == 1
        assert detail["last_step_no"] == 2
        assert detail["phases"] == ["CONTEXT", "ARCHITECTURE"]

    def test_the_audit_entry_is_findable_by_the_plan_hash(self) -> None:
        audit = FakeAuditRepository()
        loader = build_loader(audit=audit)

        loader.import_plan(plan_text(), actor=ACTOR, reason=REASON)

        found = audit.list_for_entity(AuditEntityType.PLAN, _sha256(plan_text()))
        assert len(found) == 1
        assert found[0].action is AuditAction.IMPORT


# ---------------------------------------------------------------------------
# the preview: a full report, and not a single write
# ---------------------------------------------------------------------------


class TestPreview:
    """A preview describes what an import would do - and writes nothing."""

    def test_the_preview_reports_project_steps_risks_and_phases(self) -> None:
        preview = build_loader().preview(plan_text()).to_dict()

        assert preview["valid"] is True
        assert preview["importable"] is True
        assert preview["blocked_reason"] == ""
        assert preview["project_name"] == PROJECT_NAME
        assert preview["project_matches"] is True
        assert preview["mode"] == "MANUAL"
        assert preview["db_mode"] == "MANUAL"
        assert preview["mode_matches"] is True
        assert preview["plan_version"] == PLAN_VERSION
        assert preview["plan_version_declared"] is True
        assert preview["plan_version_matches"] is True
        assert preview["plan_hash"] == _sha256(plan_text())
        assert preview["step_count"] == 2
        assert (preview["first_step_no"], preview["last_step_no"]) == (1, 2)
        assert preview["risk_counts"] == {"LOW": 2, "MEDIUM": 0, "HIGH": 0}
        assert preview["requires_human_count"] == 1
        assert preview["phases"] == ["CONTEXT", "ARCHITECTURE"]
        assert preview["db_step_count"] == 0
        assert preview["db_project_name"] == PROJECT_NAME
        assert preview["db_plan_version"] == PLAN_VERSION
        assert preview["issues"] == []

    def test_the_plan_hash_is_the_sha256_of_the_text(self) -> None:
        text = plan_text()

        assert parse_plan(text).plan_hash == _sha256(text)
        assert build_loader().preview(text).plan_hash == _sha256(text)

    def test_the_preview_never_writes(self) -> None:
        projects = FakeProjectRepository((make_project(),))
        steps = FakeStepRepository()
        audit = FakeAuditRepository()
        transactions = RecordingTransaction()
        loader = build_loader(
            projects=projects,
            steps=steps,
            audit=audit,
            transactions=transactions,
        )

        loader.preview(plan_text())
        loader.preview("this is not json")
        loader.preview(plan_without("steps"))

        assert steps.upserts == 0
        assert audit.appends == 0
        assert projects.upserts == 0
        assert transactions.opened == 0
        assert steps.list() == ()
        assert audit.list() == ()

    def test_the_preview_reports_every_issue_of_an_invalid_plan(self) -> None:
        preview = build_loader().preview("{not json").to_dict()

        assert preview["valid"] is False
        assert preview["importable"] is False
        assert preview["blocked_reason"] == "the plan file is invalid"
        assert [issue["path"] for issue in preview["issues"]] == ["<file>"]

    def test_a_plan_without_plan_version_does_not_invent_a_second_value(
        self,
    ) -> None:
        text = plan_without("plan_version")
        preview = build_loader().preview(text).to_dict()

        assert parse_plan(text).plan_version == ""
        assert preview["plan_version"] == PLAN_VERSION  # the database's own
        assert preview["plan_version_declared"] is False
        assert preview["plan_version_matches"] is True
        assert preview["importable"] is True

    def test_a_plan_without_mode_leaves_the_mode_alone(self) -> None:
        text = plan_without("mode", project={"name": PROJECT_NAME})
        preview = build_loader().preview(text).to_dict()

        assert preview["mode"] == ""
        assert preview["db_mode"] == "MANUAL"
        assert preview["mode_matches"] is True
        assert preview["importable"] is True

    def test_a_different_mode_is_reported_but_not_applied(self) -> None:
        text = plan_text(project={"name": PROJECT_NAME, "mode": "AUTO"})
        project = make_project()
        projects = FakeProjectRepository((project,))
        loader = build_loader(projects=projects)

        preview = loader.preview(text).to_dict()
        assert preview["mode"] == "AUTO"
        assert preview["mode_matches"] is False
        assert preview["importable"] is True

        loader.import_plan(text, actor=ACTOR, reason=REASON)
        assert projects.upserts == 0
        assert projects.items[PROJECT_NAME] is project
        assert projects.items[PROJECT_NAME].mode is Mode.MANUAL


# ---------------------------------------------------------------------------
# an invalid plan writes nothing - and every problem is reported
# ---------------------------------------------------------------------------


class TestInvalidPlansWriteNothing:
    """Validation runs before any write, and reports every problem at once."""

    def _refuse(
        self, text: str
    ) -> tuple[PlanFormatError, FakeStepRepository, FakeAuditRepository]:
        """Import an invalid plan and prove that nothing reached the ports."""
        steps = FakeStepRepository()
        audit = FakeAuditRepository()
        loader = build_loader(steps=steps, audit=audit)

        with pytest.raises(PlanFormatError) as caught:
            loader.import_plan(text, actor=ACTOR, reason=REASON)

        assert steps.upserts == 0
        assert audit.appends == 0
        assert steps.list() == ()
        assert audit.list() == ()
        return caught.value, steps, audit

    def test_invalid_json_writes_nothing(self) -> None:
        error, _, _ = self._refuse('{"schema_version": "1.0",,}')

        assert error.issues[0].path == "<file>"
        assert "not valid JSON" in error.issues[0].message

    def test_a_non_object_root_writes_nothing(self) -> None:
        error, _, _ = self._refuse("[1, 2, 3]")

        assert error.issues[0].path == "<root>"
        assert "one JSON object" in error.issues[0].message

    def test_a_missing_schema_version_writes_nothing(self) -> None:
        error, _, _ = self._refuse(plan_without("schema_version"))

        assert [issue.path for issue in error.issues] == ["schema_version"]
        assert error.issues[0].problem == "is required"

    def test_an_unsupported_schema_version_writes_nothing(self) -> None:
        error, _, _ = self._refuse(plan_text(schema_version="2.0"))

        assert error.issues[0].path == "schema_version"
        assert "unsupported" in error.issues[0].message

    def test_empty_steps_writes_nothing(self) -> None:
        error, _, _ = self._refuse(plan_text(steps=[]))

        assert [issue.path for issue in error.issues] == ["steps"]
        assert "at least one step" in error.issues[0].message

    def test_a_missing_steps_list_writes_nothing(self) -> None:
        error, _, _ = self._refuse(plan_without("steps"))

        assert error.issues[0].problem == "is required"

    def test_a_non_list_steps_value_writes_nothing(self) -> None:
        error, _, _ = self._refuse(plan_text(steps={"1": {}}))

        assert [issue.path for issue in error.issues] == ["steps"]

    def test_a_non_object_step_writes_nothing(self) -> None:
        error, _, _ = self._refuse(plan_text(steps=["step one"]))

        assert [issue.path for issue in error.issues] == ["steps[0]"]

    def test_duplicate_step_numbers_write_nothing(self) -> None:
        payload = steps_payload(2)
        error, _, _ = self._refuse(plan_text(steps=[payload[0], payload[0]]))

        assert "steps[step_no=1]" in [issue.path for issue in error.issues]
        assert any("appears 2 times" in issue.message for issue in error.issues)

    def test_non_contiguous_step_numbers_write_nothing(self) -> None:
        payload = steps_payload(3)
        error, _, _ = self._refuse(plan_text(steps=[payload[0], payload[2]]))

        messages = [issue.message for issue in error.issues]
        assert "step numbers are missing: [2]" in messages
        assert "step numbers are outside 1..2: [3]" in messages

    def test_an_invalid_phase_writes_nothing(self) -> None:
        # "ANALYSIS" is not a Phase: the assistant's own vocabulary is the model.
        step = {**steps_payload(1)[0], "phase": "ANALYSIS"}
        error, _, _ = self._refuse(plan_text(steps=[step]))

        assert error.issues[0].path == "steps[0].phase"
        assert error.issues[0].value == "'ANALYSIS'"
        assert "FOUNDATION" in error.issues[0].problem

    def test_an_invalid_risk_writes_nothing(self) -> None:
        step = {**steps_payload(1)[0], "risk": "SEVERE"}
        error, _, _ = self._refuse(plan_text(steps=[step]))

        assert [issue.path for issue in error.issues] == ["steps[0].risk"]

    def test_an_invalid_mode_writes_nothing(self) -> None:
        error, _, _ = self._refuse(
            plan_text(project={"name": PROJECT_NAME, "mode": "TURBO"})
        )

        assert [issue.path for issue in error.issues] == ["project.mode"]

    def test_invalid_max_attempts_writes_nothing(self) -> None:
        for bad in (0, -1, "3", True, 2.5):
            step = {**steps_payload(1)[0], "max_attempts": bad}
            error, _, _ = self._refuse(plan_text(steps=[step]))

            assert [
                issue.path for issue in error.issues
            ] == ["steps[0].max_attempts"], bad

    def test_a_non_boolean_requires_human_writes_nothing(self) -> None:
        step = {**steps_payload(1)[0], "requires_human": "yes"}
        error, _, _ = self._refuse(plan_text(steps=[step]))

        assert [
            issue.path for issue in error.issues
        ] == ["steps[0].requires_human"]

    def test_a_missing_title_writes_nothing(self) -> None:
        error, _, _ = self._refuse(
            plan_text(steps=[{"step_no": 1, "phase": "CONTEXT"}])
        )

        assert [issue.path for issue in error.issues] == ["steps[0].title"]

    def test_a_missing_project_writes_nothing(self) -> None:
        error, _, _ = self._refuse(plan_without("project"))

        assert [issue.path for issue in error.issues] == ["project"]

    def test_an_empty_project_name_writes_nothing(self) -> None:
        error, _, _ = self._refuse(plan_text(project={"name": "   "}))

        assert [issue.path for issue in error.issues] == ["project.name"]

    def test_a_blank_plan_version_writes_nothing(self) -> None:
        error, _, _ = self._refuse(plan_text(plan_version="  "))

        assert [issue.path for issue in error.issues] == ["plan_version"]

    def test_unknown_fields_are_refused(self) -> None:
        step = {**steps_payload(1)[0], "state": "VERIFIED", "attempt": 3}
        error, _, _ = self._refuse(
            plan_text(
                status="DONE",
                project={"name": PROJECT_NAME, "mode": "MANUAL", "id": "p1"},
                steps=[step],
            )
        )

        assert sorted(issue.path for issue in error.issues) == [
            "project.id",
            "steps[0].attempt",
            "steps[0].state",
            "{root}.status",
        ]
        assert all("refused" in issue.message for issue in error.issues)

    def test_all_problems_are_reported_together(self) -> None:
        step = {
            "step_no": 0,
            "phase": "ANALYSIS",
            "title": "",
            "max_attempts": 0,
        }
        error, _, _ = self._refuse(
            plan_text(steps=[step], project={"name": ""})
        )

        assert sorted(issue.path for issue in error.issues) == [
            "project.name",
            "steps[0].max_attempts",
            "steps[0].phase",
            "steps[0].step_no",
            "steps[0].title",
        ]
        assert len(error.issues) == 5
        assert "5 problem(s)" in str(error)

    def test_an_issue_carries_path_problem_value_and_message(self) -> None:
        step = {**steps_payload(1)[0], "max_attempts": -1}
        error, _, _ = self._refuse(plan_text(steps=[step]))

        (issue,) = error.issues
        assert issue.path == "steps[0].max_attempts"
        assert issue.problem == "must be an integer >= 1"
        assert issue.value == "-1"
        assert issue.message == "steps[0].max_attempts must be an integer >= 1"
        assert issue.to_dict() == {
            "path": "steps[0].max_attempts",
            "problem": "must be an integer >= 1",
            "value": "-1",
            "message": "steps[0].max_attempts must be an integer >= 1",
        }

    def test_an_offending_value_is_rendered_safely(self) -> None:
        step = {**steps_payload(1)[0], "notes": "x" * 400}
        error, _, _ = self._refuse(plan_text(steps=[step]))

        assert error.issues[0].path == "steps[0].notes"
        assert len(error.issues[0].value) <= 80
        assert error.issues[0].value.endswith("...")

    def test_too_many_steps_are_refused(self) -> None:
        error, _, _ = self._refuse(
            plan_text(steps=steps_payload(MAX_PLAN_STEPS + 1))
        )

        assert [issue.path for issue in error.issues] == ["steps"]
        assert "at most" in error.issues[0].message

    def test_the_issue_report_is_capped_with_an_honest_marker(self) -> None:
        broken = [{"step_no": None} for _ in range(20)]  # three problems each
        error, _, _ = self._refuse(plan_text(steps=broken))

        assert len(error.issues) == MAX_ISSUES + 1
        assert error.issues[-1].path == "<report>"
        assert "were not listed" in error.issues[-1].message

    def test_a_non_string_plan_is_refused(self) -> None:
        for text in (None, 42, ["steps"]):
            with pytest.raises(PlanFormatError) as caught:
                parse_plan(text)

            assert caught.value.issues[0].path == "<plan>"

    def test_a_plan_larger_than_the_limit_is_refused(self) -> None:
        with pytest.raises(PlanFormatError) as caught:
            parse_plan("x" * (MAX_PLAN_CHARS + 1))

        assert "too large" in caught.value.issues[0].message

    def test_the_validation_is_pure_and_repeatable(self) -> None:
        text = plan_text()
        first = parse_plan(text)
        second = parse_plan(text)

        assert first == second
        assert first.steps == second.steps


# ---------------------------------------------------------------------------
# the project identity is matched, never written
# ---------------------------------------------------------------------------


class TestProjectIdentity:
    """The plan is descriptive: it must match the database, and it never writes."""

    def test_a_different_project_name_blocks_the_preview(self) -> None:
        text = plan_text(project={"name": "youtube_to_mp3", "mode": "MANUAL"})
        preview = build_loader().preview(text).to_dict()

        assert preview["valid"] is True
        assert preview["importable"] is False
        assert preview["project_matches"] is False
        assert preview["project_name"] == "youtube_to_mp3"
        assert preview["db_project_name"] == PROJECT_NAME
        assert "youtube_to_mp3" in preview["blocked_reason"]
        assert PROJECT_NAME in preview["blocked_reason"]

    def test_a_different_project_name_is_refused_without_writing(self) -> None:
        steps = FakeStepRepository()
        audit = FakeAuditRepository()
        projects = FakeProjectRepository((make_project(),))
        loader = build_loader(projects=projects, steps=steps, audit=audit)

        with pytest.raises(PlanProjectMismatchError, match="youtube_to_mp3"):
            loader.import_plan(
                plan_text(project={"name": "youtube_to_mp3"}),
                actor=ACTOR,
                reason=REASON,
            )

        assert steps.upserts == 0
        assert audit.appends == 0
        assert projects.upserts == 0

    def test_the_pilot_project_matches_its_own_database(self) -> None:
        projects = FakeProjectRepository(
            (make_project(name="youtube_to_mp3", plan_version="1.0"),)
        )
        loader = build_loader(projects=projects)
        text = plan_text(
            project={"name": "youtube_to_mp3", "mode": "MANUAL"},
            plan_version="1.0",
        )

        assert loader.preview(text).importable is True
        result = loader.import_plan(text, actor=ACTOR, reason=REASON)

        assert result.project_name == "youtube_to_mp3"
        assert result.plan_version == "1.0"

    def test_a_different_plan_version_blocks_the_preview(self) -> None:
        preview = build_loader().preview(plan_text(plan_version="9.9")).to_dict()

        assert preview["valid"] is True
        assert preview["importable"] is False
        assert preview["plan_version_matches"] is False
        assert preview["plan_version"] == "9.9"
        assert preview["db_plan_version"] == PLAN_VERSION
        assert "9.9" in preview["blocked_reason"]

    def test_a_different_plan_version_is_refused_without_writing(self) -> None:
        steps = FakeStepRepository()
        audit = FakeAuditRepository()
        loader = build_loader(steps=steps, audit=audit)

        with pytest.raises(PlanVersionMismatchError, match="9.9"):
            loader.import_plan(
                plan_text(plan_version="9.9"), actor=ACTOR, reason=REASON
            )

        assert steps.upserts == 0
        assert audit.appends == 0

    def test_a_matching_plan_version_is_required_to_be_exact(self) -> None:
        projects = FakeProjectRepository(
            (make_project(plan_version="1.0"),)
        )
        loader = build_loader(projects=projects)

        assert loader.preview(plan_text(plan_version="1.0")).importable is True
        assert loader.preview(plan_text(plan_version="1.0.0")).importable is False

    def test_the_project_row_is_never_written(self) -> None:
        project = make_project()
        before = project.to_dict()
        projects = FakeProjectRepository((project,))
        loader = build_loader(projects=projects)

        loader.import_plan(plan_text(), actor=ACTOR, reason=REASON)

        assert projects.upserts == 0
        assert projects.items[PROJECT_NAME] is project
        assert projects.items[PROJECT_NAME].to_dict() == before
        assert projects.items[PROJECT_NAME].plan_version == PLAN_VERSION
        assert projects.items[PROJECT_NAME].plan_hash == ""
        assert projects.items[PROJECT_NAME].mode is Mode.MANUAL
        assert projects.items[PROJECT_NAME].updated_at == NOW

    def test_a_missing_project_is_an_invariant_error(self) -> None:
        loader = build_loader(projects=FakeProjectRepository(()))

        with pytest.raises(PlanInvariantError, match="no project"):
            loader.preview(plan_text())
        with pytest.raises(PlanInvariantError, match="no project"):
            loader.import_plan(plan_text(), actor=ACTOR, reason=REASON)

    def test_two_projects_are_an_invariant_error(self) -> None:
        projects = FakeProjectRepository(
            (make_project(), make_project(name="another project"))
        )
        loader = build_loader(projects=projects)

        with pytest.raises(PlanInvariantError, match="exactly one project"):
            loader.preview(plan_text())
        with pytest.raises(PlanInvariantError, match="exactly one project"):
            loader.import_plan(plan_text(), actor=ACTOR, reason=REASON)


# ---------------------------------------------------------------------------
# idempotence: the initial plan only, and nothing is merged
# ---------------------------------------------------------------------------


class TestIdempotence:
    """A second import is refused - never merged, replaced or renumbered."""

    def test_a_second_import_is_rejected_without_duplicates(self) -> None:
        steps = FakeStepRepository()
        audit = FakeAuditRepository()
        transactions = RecordingTransaction()
        loader = build_loader(
            steps=steps, audit=audit, transactions=transactions
        )
        text = plan_text()

        loader.import_plan(text, actor=ACTOR, reason=REASON)
        imported = dict(steps.items)

        with pytest.raises(PlanConflictError, match="already loaded"):
            loader.import_plan(text, actor=ACTOR, reason=REASON)

        assert steps.items == imported
        assert sorted(steps.items) == [1, 2]
        assert steps.upserts == 2
        assert audit.appends == 1
        assert transactions.opened == 2
        assert transactions.committed == 1
        assert transactions.rolled_back == 1

    def test_a_preview_of_an_initialised_database_is_blocked(self) -> None:
        steps = FakeStepRepository((make_step(1), make_step(2)))
        loader = build_loader(steps=steps)

        preview = loader.preview(plan_text()).to_dict()

        assert preview["valid"] is True
        assert preview["importable"] is False
        assert preview["db_step_count"] == 2
        assert "already holds 2 step(s)" in preview["blocked_reason"]
        assert steps.upserts == 0

    def test_the_conflict_is_decided_inside_the_transaction(self) -> None:
        steps = FakeStepRepository((make_step(1),))
        transactions = RecordingTransaction()
        loader = build_loader(steps=steps, transactions=transactions)

        with pytest.raises(PlanConflictError):
            loader.import_plan(plan_text(), actor=ACTOR, reason=REASON)

        assert transactions.opened == 1
        assert transactions.rolled_back == 1

    def test_the_loader_exposes_no_merge_or_replacement_surface(self) -> None:
        public = {name for name in dir(PlanLoader) if not name.startswith("_")}

        assert public == {"preview", "import_plan"}


# ---------------------------------------------------------------------------
# the real source of truth: one transaction, a real rollback
# ---------------------------------------------------------------------------


class BrokenRepository:
    """A real repository whose ``upsert`` is deliberately broken."""

    def __init__(self, inner: Any, message: str = "write failed") -> None:
        self._inner = inner
        self._message = message

    def upsert(self, item: Any) -> None:
        raise RuntimeError(self._message)

    def get(self, key: Any) -> Any:
        return self._inner.get(key)

    def list(self) -> Any:
        return self._inner.list()

    def delete(self, key: Any) -> Any:
        return self._inner.delete(key)

    def list_by_state(self, state: Any) -> Any:
        return self._inner.list_by_state(state)


class BrokenAudit:
    """A real audit repository whose ``append`` is deliberately broken."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def append(self, entry: AuditEntry) -> None:
        raise RuntimeError("audit write failed")

    def list(self) -> Any:
        return self._inner.list()

    def list_for_entity(self, entity_type: Any, entity_id: str) -> Any:
        return self._inner.list_for_entity(entity_type, entity_id)


@pytest.fixture
def sqlite_storage():
    connection = open_database(":memory:")
    yield SqliteStorage(connection)
    close_database(connection)


def sqlite_loader(
    storage: SqliteStorage, *, steps: Any = None, audit: Any = None
) -> PlanLoader:
    """A loader over the real storage, with real transactions."""
    return PlanLoader(
        storage.projects,
        steps if steps is not None else storage.steps,
        audit if audit is not None else storage.audit,
        storage,
        clock=fixed_clock,
    )


class TestAtomicity:
    """One transaction per import, and a real failure rolls everything back."""

    def test_a_successful_import_is_committed(self, sqlite_storage) -> None:
        sqlite_storage.projects.upsert(make_project())

        result = sqlite_loader(sqlite_storage).import_plan(
            plan_text(), actor=ACTOR, reason=REASON, source_file="real.json"
        )

        steps = sqlite_storage.steps.list()
        assert [step.step_no for step in steps] == [1, 2]
        assert all(step.state is StepState.PENDING for step in steps)
        assert result.plan_hash == _sha256(plan_text())

        (entry,) = sqlite_storage.audit.list()
        assert entry.entity_type is AuditEntityType.PLAN
        assert entry.action is AuditAction.IMPORT
        assert entry.entity_id == _sha256(plan_text())
        assert entry.detail["source_file"] == "real.json"
        assert entry.detail["phases"] == ["CONTEXT", "ARCHITECTURE"]

    def test_a_failing_step_write_rolls_back_every_step(
        self, sqlite_storage
    ) -> None:
        sqlite_storage.projects.upsert(make_project())
        loader = sqlite_loader(
            sqlite_storage, steps=BrokenRepository(sqlite_storage.steps)
        )

        with pytest.raises(RuntimeError, match="write failed"):
            loader.import_plan(plan_text(), actor=ACTOR, reason=REASON)

        assert sqlite_storage.steps.list() == ()
        assert sqlite_storage.audit.list() == ()

    def test_a_failing_audit_append_rolls_back_every_step(
        self, sqlite_storage
    ) -> None:
        sqlite_storage.projects.upsert(make_project())
        loader = sqlite_loader(
            sqlite_storage, audit=BrokenAudit(sqlite_storage.audit)
        )
        text = plan_text(steps=steps_payload(4))

        with pytest.raises(RuntimeError, match="audit write failed"):
            loader.import_plan(text, actor=ACTOR, reason=REASON)

        assert sqlite_storage.steps.list() == ()
        assert sqlite_storage.audit.list() == ()

    def test_nothing_is_written_before_the_transaction(
        self, sqlite_storage
    ) -> None:
        sqlite_storage.projects.upsert(make_project())
        loader = sqlite_loader(sqlite_storage)

        with pytest.raises(PlanFormatError):
            loader.import_plan("{not json", actor=ACTOR, reason=REASON)
        with pytest.raises(PlanProjectMismatchError):
            loader.import_plan(
                plan_text(project={"name": "youtube_to_mp3"}),
                actor=ACTOR,
                reason=REASON,
            )

        assert sqlite_storage.steps.list() == ()
        assert sqlite_storage.audit.list() == ()

    def test_the_real_project_row_survives_an_import(
        self, sqlite_storage
    ) -> None:
        sqlite_storage.projects.upsert(make_project())
        before = sqlite_storage.projects.get(PROJECT_NAME)

        sqlite_loader(sqlite_storage).import_plan(
            plan_text(), actor=ACTOR, reason=REASON
        )

        after = sqlite_storage.projects.get(PROJECT_NAME)
        assert after == before
        assert after.plan_version == PLAN_VERSION
        assert after.plan_hash == ""
        assert after.mode is Mode.MANUAL
        assert after.updated_at == NOW


# ---------------------------------------------------------------------------
# the human context: an import is attributable, before anything is read
# ---------------------------------------------------------------------------


class TestRequiredContext:
    """An import needs an actor and a reason - checked before any read."""

    def test_an_actor_is_required_before_any_read(self) -> None:
        projects = FakeProjectRepository((make_project(),))
        loader = build_loader(projects=projects)

        for actor in ("", "   ", None, 42):
            with pytest.raises(PlanActorRequiredError, match="actor"):
                loader.import_plan(plan_text(), actor=actor, reason=REASON)

        assert projects.reads == 0

    def test_a_reason_is_required_before_any_read(self) -> None:
        projects = FakeProjectRepository((make_project(),))
        loader = build_loader(projects=projects)

        for reason in ("", "   ", None, 42):
            with pytest.raises(PlanReasonRequiredError, match="reason"):
                loader.import_plan(plan_text(), actor=ACTOR, reason=reason)

        assert projects.reads == 0

    def test_the_context_is_checked_before_the_plan_is_parsed(self) -> None:
        steps = FakeStepRepository()
        loader = build_loader(steps=steps)

        with pytest.raises(PlanActorRequiredError):
            loader.import_plan(
                "this is not json at all", actor="", reason=REASON
            )

        assert steps.upserts == 0

    def test_a_preview_needs_no_actor_at_all(self) -> None:
        # Reading is free and unattributable; only the write is a human act.
        assert build_loader().preview(plan_text()).importable is True


# ---------------------------------------------------------------------------
# the shipped pilot plan
# ---------------------------------------------------------------------------


class TestPilotPlan:
    """The youtube_to_mp3 plan ships with the assistant and must stay loadable."""

    def test_the_example_plan_is_valid(self) -> None:
        assert EXAMPLE_PLAN.is_file()
        plan = parse_plan(EXAMPLE_PLAN.read_text(encoding="utf-8"))

        assert plan.project_name == "youtube_to_mp3"
        assert plan.plan_version == "1.0"
        assert len(plan.steps) >= 8
        assert [step.step_no for step in plan.steps] == list(
            range(1, len(plan.steps) + 1)
        )
        assert all(step.phase in tuple(Phase) for step in plan.steps)

    def test_the_example_plan_imports_into_its_own_database(self) -> None:
        text = EXAMPLE_PLAN.read_text(encoding="utf-8")
        projects = FakeProjectRepository(
            (make_project(name="youtube_to_mp3", plan_version="1.0"),)
        )
        steps = FakeStepRepository()
        audit = FakeAuditRepository()
        loader = build_loader(projects=projects, steps=steps, audit=audit)

        assert loader.preview(text).importable is True

        result = loader.import_plan(
            text, actor=ACTOR, reason=REASON, source_file=str(EXAMPLE_PLAN)
        )

        assert result.project_name == "youtube_to_mp3"
        assert result.step_count == len(steps.items)
        assert sorted(steps.items) == list(range(1, result.step_count + 1))
        assert all(
            step.state is StepState.PENDING for step in steps.items.values()
        )
        assert audit.entries[0].detail["project_name"] == "youtube_to_mp3"

    def test_the_example_plan_is_refused_by_the_assistants_own_database(
        self,
    ) -> None:
        text = EXAMPLE_PLAN.read_text(encoding="utf-8")

        with pytest.raises(PlanProjectMismatchError, match="youtube_to_mp3"):
            build_loader().import_plan(text, actor=ACTOR, reason=REASON)

    def test_the_loader_carries_no_project_specific_logic(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8").lower()

        for fragment in ("youtube", "mp3", "download", "transcri"):
            assert fragment not in source, fragment


# ---------------------------------------------------------------------------
# the module surface: five collaborators, stdlib and ports only
# ---------------------------------------------------------------------------


class TestModuleSurface:
    """The loader can do exactly what it promises, and nothing else."""

    def test_the_loader_holds_exactly_five_collaborators(self) -> None:
        assert set(vars(build_loader())) == {
            "_projects",
            "_steps",
            "_audit",
            "_transactions",
            "_clock",
        }

    def test_the_clock_must_be_callable(self) -> None:
        with pytest.raises(ValueError, match="clock"):
            build_loader(clock=None)

    def test_the_module_imports_only_stdlib_and_inner_layers(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        absolute: set[str] = set()
        relative: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                absolute.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    relative.add(node.module or "")
                else:
                    absolute.add(node.module or "")

        assert absolute == {
            "__future__",
            "hashlib",
            "json",
            "dataclasses",
            "datetime",
            "typing",
        }
        assert relative == {
            "domain.audit",
            "domain.enums",
            "domain.fsm",
            "domain.models",
            "ports.repositories",
            "ports.transactions",
        }

    def test_the_module_never_reads_a_file_or_the_system_clock(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")

        for fragment in (
            "import sqlite3",
            "datetime.now",
            "utc_now",
            "time.time",
            "open(",
        ):
            assert fragment not in source, fragment
