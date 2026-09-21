"""Tests for the read-only Logs tab and the log-event contract behind it.

The log is the operator's window into what the assistant is doing - above all
into an advisory architecture review, whose stages are otherwise invisible. These
tests pin what that window promises:

* one validated, sanitized, JSON-safe entry shape with a documented vocabulary;
* secrets, authorization headers and provider bodies can never reach it;
* the core thread queues events in the order they happened and the Tk thread only
  drains and renders plain dictionaries;
* a review logs every documented stage - including the ones that did **not**
  happen, like "the judge was not consulted";
* a failure is always logged, with the exception *type* only;
* clearing the view deletes nothing, because the view is not storage.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

from architecture_assistant.application import (
    EVENT_FIELDS,
    EVENT_LEVEL_ERROR,
    EVENT_LEVEL_INFO,
    EVENT_LEVEL_WARN,
    EVENT_LEVELS,
    LOG_COMPONENTS,
    AdvisorReviewer,
    ArchitectureReview,
    EvidenceRelation,
    JudgeUseCase,
    LogEventError,
    build_event,
    component_for,
    exception_reason,
    sanitize_text,
)
from architecture_assistant.composition import (
    CompositionConfig,
    compose,
    observe,
)
from architecture_assistant.domain.enums import (
    DecisionStatus,
    Phase,
    Severity,
    StepState,
)
from architecture_assistant.domain.models import (
    Decision,
    Finding,
    Step,
)
from architecture_assistant.infrastructure import (
    ClaudeAdvisorAbstainError,
    OpenAIAdvisorError,
)
from architecture_assistant.ports.capabilities import (
    CostSummary,
    RealizationCheckResult,
)
from architecture_assistant_gui.controller import GuiController
from architecture_assistant_gui.core import (
    EVENT_LIMIT,
    CoreWorker,
    JobResult,
    describe_error,
)

NOW = datetime(2026, 9, 21, 23, 30, tzinfo=timezone.utc)
SOURCE_ROOT = "src/architecture_assistant"
PROJECT = "youtube_to_mp3"
QUESTION = "Review the architecture: module boundaries and failure points."
RULE_ONE = "unknown-layer"
RULE_TWO = "forbidden-layer-import"
RULES = (RULE_ONE, RULE_TWO, "sqlite-outside-infrastructure")

#: A literal that is not, and never was, a real credential.
KEY = "sk-test-not-a-real-secret"

#: A provider body, as a response would carry it.
BODY = (
    '{"choices": [{"message": {"role": "assistant", "content": '
    '"a very long provider answer that must never be pasted into a panel"}}], '
    '"usage": {"prompt_tokens": 1234, "completion_tokens": 5678}}'
)


def fixed_clock() -> datetime:
    return NOW


def make_config(tmp_path: Path, **overrides: Any) -> CompositionConfig:
    """A real composition config over a temporary database."""
    data: dict[str, Any] = dict(
        database_path=tmp_path / "assistant.db",
        exchange_dir=tmp_path / "cline",
        report_dir=tmp_path / "reports",
        source_root=Path(SOURCE_ROOT),
        project_name=PROJECT,
        plan_version="1.0",
        clock=fixed_clock,
    )
    data.update(overrides)
    return CompositionConfig(**data)


def an_event(**overrides: Any) -> dict[str, Any]:
    """One valid event, for the plumbing tests."""
    data: dict[str, Any] = dict(
        level=EVENT_LEVEL_INFO,
        component="GUI",
        action="note",
        message="an entry",
        timestamp=fixed_clock(),
    )
    data.update(overrides)
    return build_event(**data)


def make_step(step_no: int = 1, **overrides: Any) -> Step:
    data: dict[str, Any] = dict(
        step_no=step_no,
        phase=Phase.CONTEXT,
        title=f"Step {step_no}",
        state=StepState.PENDING,
        attempt=0,
        max_attempts=3,
        created_at=NOW,
    )
    data.update(overrides)
    return Step(**data)


def finding(source: str, *, step_no: Optional[int] = 1) -> Finding:
    return Finding(
        id=f"finding-{source}-{step_no or 0}-abc123abc123",
        source=source,
        claim=f"{source} claim",
        evidence=("application/context.py:12",),
        confidence=0.5,
        severity=Severity.MEDIUM,
        step_no=step_no,
        created_at=NOW,
    )


def make_snapshot():
    """The projection a review reads, with one current step."""
    from architecture_assistant.application import LoopHealth, ReportSnapshot
    from architecture_assistant.domain.models import ArchitectureVersion, Project

    step = make_step()
    version = ArchitectureVersion(
        version="1.1", baseline="Layered", rules=RULES, created_at=NOW
    )
    return ReportSnapshot(
        project=Project(
            name=PROJECT,
            plan_version="1.0",
            mode="MANUAL",
            paused=False,
            created_at=NOW,
            updated_at=NOW,
        ),
        steps=(step,),
        tasks=(),
        architecture=version,
        architecture_versions=(version,),
        adrs=(),
        risks=(),
        findings=(),
        decisions=(),
        change_requests=(),
        cost=CostSummary(),
        cost_by_step=(),
        health=LoopHealth(
            project_paused=False,
            step_count=1,
            current_step_no=1,
            current_state=StepState.PENDING,
            next_step_no=None,
            complete=False,
        ),
        generated_at=NOW,
    )


# ---------------------------------------------------------------------------
# fakes: three scripted advisors, one check, one judge
# ---------------------------------------------------------------------------


class FakeAdvisor:
    """A scripted advisor: a finding, an abstention or a provider failure."""

    def __init__(self, source: str, *, behaviour: str = "finding") -> None:
        self.provider = source
        self.behaviour = behaviour

    def advise(self, query: Any) -> Finding:
        if self.behaviour == "abstain":
            raise ClaudeAdvisorAbstainError("too little context")
        if self.behaviour == "error":
            raise OpenAIAdvisorError("the provider is unavailable")
        return finding(self.provider, step_no=query.step_no)


class FakeCheck:
    """A read-only realization check: a settled verdict and no persistence."""

    def __init__(
        self,
        *,
        compliant: bool = True,
        failure: Optional[BaseException] = None,
    ) -> None:
        self.compliant = compliant
        self.failure = failure

    def check(self, step_no: int, attempt: int) -> RealizationCheckResult:
        if self.failure is not None:
            raise self.failure
        status = (
            DecisionStatus.ACCEPTED if self.compliant else DecisionStatus.REJECTED
        )
        findings = () if self.compliant else (finding("architecture-validator"),)
        decision = Decision(
            id=f"realization-{step_no}-{attempt}",
            status=status,
            decision="the deterministic gate verdict",
            rationale="deterministic rules decided this",
            rules_applied=RULES,
            evidence_refs=tuple(item.id for item in findings),
            step_no=step_no,
            created_at=NOW,
        )
        return RealizationCheckResult(
            compliant=self.compliant,
            baseline_version="1.1",
            decision=decision,
            findings=findings,
        )


class FakeJudge:
    """A judge implementation that answers, or fails on purpose."""

    def __init__(self, *, failure: Optional[BaseException] = None) -> None:
        self.failure = failure

    def judge(self, conflict: Any) -> Decision:
        if self.failure is not None:
            raise self.failure
        return Decision(
            id="judge-1",
            status=DecisionStatus.ACCEPTED,
            decision="the judge weighed the named findings",
            rationale="the judge weighed the named findings",
            evidence_refs=tuple(item.id for item in conflict.findings),
            created_at=NOW,
        )


def scripted_review(
    *,
    behaviours: tuple[str, ...] = ("finding", "finding", "finding"),
    declarations: tuple[tuple[str, EvidenceRelation, str], ...] = (),
    check: Optional[FakeCheck] = None,
    judge: Optional[Any] = None,
) -> ArchitectureReview:
    """A review over three scripted advisors, with explicit relations only."""
    sources = ("openai", "claude", "grok")
    declared = {
        source: (relation, target) for source, relation, target in declarations
    }
    reviewers = tuple(
        AdvisorReviewer(
            source,
            FakeAdvisor(source, behaviour=behaviour),
            relation=declared.get(
                source, (EvidenceRelation.UNRESOLVED, None)
            )[0],
            relation_target=declared.get(
                source, (EvidenceRelation.UNRESOLVED, None)
            )[1],
        )
        for source, behaviour in zip(sources, behaviours)
    )
    return ArchitectureReview(
        reviewers,
        observer=observe,
        source_root=SOURCE_ROOT,
        judge=JudgeUseCase(judge=FakeJudge()) if judge is None else judge,
        check=FakeCheck() if check is None else check,
        cost_query=lambda _filter: CostSummary(
            total_usd=0.01,
            input_tokens=10,
            output_tokens=5,
            record_count=1,
            priced_record_count=1,
        ),
        clock=fixed_clock,
    )


def run_recording(
    review: ArchitectureReview, *, question: str = QUESTION
) -> list[dict[str, Any]]:
    """Run one review and return the events it emitted, in order."""
    events: list[dict[str, Any]] = []
    review.review(make_snapshot(), question=question, on_event=events.append)
    return events


def stages(events: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """The (component, action) sequence of a run."""
    return [(event["component"], event["action"]) for event in events]


# ---------------------------------------------------------------------------
# the contract: one shape, plain data, a documented vocabulary
# ---------------------------------------------------------------------------


class TestTheEventContract:
    """An event is plain, validated, bounded data - nothing else."""

    def test_the_field_set_is_exactly_the_documented_one(self) -> None:
        assert tuple(an_event()) == EVENT_FIELDS

    def test_an_event_is_plain_json_safe_data(self) -> None:
        event = an_event(step_no=9, review_id="architecture-review-9-abc123")

        assert json.loads(json.dumps(event)) == event
        for value in event.values():
            assert value is None or isinstance(value, (str, int))

    def test_an_unknown_level_is_refused(self) -> None:
        with pytest.raises(LogEventError):
            an_event(level="DEBUG")
        with pytest.raises(LogEventError):
            an_event(level="info")

    def test_a_blank_field_is_refused(self) -> None:
        for overrides in (
            {"message": "   "},
            {"component": ""},
            {"action": ""},
            {"message": None},
        ):
            with pytest.raises(LogEventError):
                an_event(**overrides)

    def test_a_nonsense_step_or_sequence_is_refused(self) -> None:
        for overrides in (
            {"step_no": 0},
            {"step_no": -1},
            {"step_no": True},
            {"step_no": "9"},
            {"seq": -1},
            {"seq": "1"},
        ):
            with pytest.raises(LogEventError):
                an_event(**overrides)

    def test_a_bad_timestamp_is_refused(self) -> None:
        with pytest.raises(LogEventError):
            an_event(timestamp="yesterday")
        with pytest.raises(LogEventError):
            an_event(timestamp=12345)

    def test_a_long_message_is_bounded(self) -> None:
        event = an_event(message="x" * 5000)

        assert len(event["message"]) <= 400
        assert event["message"].endswith("…")

    def test_a_long_action_is_bounded(self) -> None:
        event = an_event(action="a" * 200)

        assert len(event["action"]) <= 48

    def test_the_component_vocabulary_is_the_documented_one(self) -> None:
        assert LOG_COMPONENTS == (
            "GUI",
            "CoreWorker",
            "Gate",
            "OpenAI",
            "Claude",
            "Grok",
            "Merger",
            "Decision",
            "Judge",
            "Cost",
            "Synthesis",
            "Supervisor",
        )
        for source, expected in (
            ("openai", "OpenAI"),
            ("CLAUDE", "Claude"),
            ("grok", "Grok"),
            ("gate", "Gate"),
            ("merger", "Merger"),
            ("decision", "Decision"),
            ("judge", "Judge"),
            ("cost", "Cost"),
            ("gui", "GUI"),
            ("coreworker", "CoreWorker"),
        ):
            assert component_for(source) == expected
        # an unknown source is kept, sanitized - never silently reassigned
        assert component_for("some-new-provider") == "some-new-provider"
        with pytest.raises(LogEventError):
            component_for("  ")


# ---------------------------------------------------------------------------
# sanitization: secrets, headers and bodies never reach the panel
# ---------------------------------------------------------------------------


class TestSanitization:
    """The panel shows what happened - never a credential, header or body."""

    def test_an_api_key_is_redacted(self) -> None:
        text = sanitize_text(f"the request used {KEY} and failed")

        assert "sk-test-not-a-real-secret" not in text
        assert "***REDACTED***" in text

    def test_an_authorization_header_is_redacted(self) -> None:
        text = sanitize_text("Authorization: Bearer abcdef123456ghijkl")

        assert "abcdef123456ghijkl" not in text
        assert "Bearer" not in text
        assert "Authorization=***REDACTED***" in text

    def test_a_key_assignment_is_redacted(self) -> None:
        for raw in (
            f"api_key={KEY}",
            f"OPENAI_API_KEY='{KEY}'",
            'x-api-key: "abc123456789012345"',
        ):
            text = sanitize_text(raw)
            assert "sk-test-not-a-real-secret" not in text
            assert "abc123456789012345" not in text
            assert "***REDACTED***" in text

    def test_a_provider_body_is_collapsed_and_bounded(self) -> None:
        raw = BODY + '\n   {"usage": 1}\n\t more body'
        text = sanitize_text(raw)

        assert "\n" not in text and "\t" not in text
        assert "  " not in text
        assert len(text) <= 400
        assert "a very long provider answer" in text

    def test_sanitizing_is_idempotent(self) -> None:
        once = sanitize_text(f"Authorization: Bearer abcdef123456 and {KEY}")

        assert sanitize_text(once) == once

    def test_a_failure_is_named_but_never_quoted(self) -> None:
        error = RuntimeError(f"401 from the API with {KEY} in the header")

        reason = exception_reason(error)

        assert reason == "RuntimeError"
        assert "sk-test" not in reason
        event = an_event(
            level=EVENT_LEVEL_ERROR,
            component="OpenAI",
            action="error",
            message=f"advisor openai failed: {reason}",
        )
        assert event["message"] == "advisor openai failed: RuntimeError"


# ---------------------------------------------------------------------------
# the queue: CoreWorker owns one thread-safe event queue
# ---------------------------------------------------------------------------


class TestTheEventQueue:
    """The worker queued the events; the order it drained them in is the truth."""

    def test_events_are_drained_in_emission_order(self, tmp_path) -> None:
        worker = CoreWorker(make_config(tmp_path))

        for index in range(25):
            worker.record_event(
                EVENT_LEVEL_INFO, "GUI", f"action-{index}", f"message {index}"
            )
        drained = worker.events()

        assert [event["action"] for event in drained] == [
            f"action-{index}" for index in range(25)
        ]
        assert [event["seq"] for event in drained] == list(range(1, 26))
        assert worker.events() == []

    def test_the_queue_keeps_only_the_newest_entries(self, tmp_path) -> None:
        worker = CoreWorker(make_config(tmp_path))

        for index in range(EVENT_LIMIT + 10):
            worker.record_event(
                EVENT_LEVEL_INFO, "GUI", "note", f"message {index}"
            )
        drained = worker.events()

        assert len(drained) == EVENT_LIMIT
        assert drained[-1]["message"] == f"message {EVENT_LIMIT + 9}"
        assert drained[0]["message"] == "message 10"

    def test_a_forwarded_event_keeps_its_timestamp(self, tmp_path) -> None:
        worker = CoreWorker(make_config(tmp_path))
        event = an_event(action="from-the-core", timestamp=fixed_clock())

        forwarded = worker.forward_event(event)

        assert forwarded["timestamp"] == event["timestamp"]
        assert forwarded["seq"] == 1
        assert worker.events() == [forwarded]

    def test_a_forwarded_event_is_never_trusted_blindly(self, tmp_path) -> None:
        worker = CoreWorker(make_config(tmp_path))
        broken_level = {**an_event(), "level": "TRACE"}
        broken_step = {**an_event(), "step_no": "nine"}

        for payload in (broken_level, broken_step, ["not", "a", "mapping"]):
            with pytest.raises((LogEventError, ValueError, TypeError)):
                worker.forward_event(payload)

        assert worker.events() == []

    def test_a_strict_producer_refuses_a_bad_event(self, tmp_path) -> None:
        worker = CoreWorker(make_config(tmp_path))

        with pytest.raises(LogEventError):
            worker.record_event("TRACE", "GUI", "note", "message")
        with pytest.raises(LogEventError):
            worker.record_event(EVENT_LEVEL_INFO, "GUI", "note", "  ")
        assert worker.events() == []

    def test_the_progress_line_is_derived_from_the_event(self, tmp_path) -> None:
        worker = CoreWorker(make_config(tmp_path))

        worker.record_event(EVENT_LEVEL_INFO, "Judge", "not-consulted", "why not")

        assert worker.progress() == ["Judge: not-consulted"]

    def test_an_event_message_is_sanitized_at_the_queue_door(
        self, tmp_path
    ) -> None:
        worker = CoreWorker(make_config(tmp_path))

        stored = worker.record_event(
            EVENT_LEVEL_ERROR,
            "OpenAI",
            "error",
            f"failed with Authorization: Bearer abcdef123456 and {KEY}",
        )

        assert "abcdef123456" not in stored["message"]
        assert "sk-test" not in stored["message"]
        assert "***REDACTED***" in stored["message"]


# ---------------------------------------------------------------------------
# the review logs every documented stage, in order
# ---------------------------------------------------------------------------


class TestReviewLogs:
    """Every stage of a review is visible, in the order it happened."""

    def test_the_documented_stages_are_logged_in_order(self) -> None:
        events = run_recording(scripted_review())

        assert stages(events) == [
            ("CoreWorker", "context"),
            ("Gate", "check"),
            ("OpenAI", "start"),
            ("OpenAI", "result"),
            ("Claude", "start"),
            ("Claude", "result"),
            ("Grok", "start"),
            ("Grok", "result"),
            ("Merger", "merge"),
            ("Decision", "decide"),
            ("Judge", "not-consulted"),
            ("Cost", "collect"),
            ("CoreWorker", "review-complete"),
        ]
        assert all(event["step_no"] == 1 for event in events)
        assert events[-1]["review_id"].startswith("architecture-review-1-")

    def test_a_provider_error_does_not_stop_later_logs(self) -> None:
        events = run_recording(
            scripted_review(behaviours=("finding", "error", "finding"))
        )

        sequence = stages(events)
        assert ("Claude", "error") in sequence
        assert sequence.index(("Claude", "error")) < sequence.index(
            ("Grok", "start")
        )
        assert sequence.index(("Grok", "result")) < sequence.index(
            ("Merger", "merge")
        )
        assert sequence[-1] == ("CoreWorker", "review-complete")
        failed = [event for event in events if event["action"] == "error"][0]
        assert failed["level"] == EVENT_LEVEL_ERROR
        assert "OpenAIAdvisorError" in failed["message"]

    def test_an_abstention_is_logged_as_a_warning(self) -> None:
        events = run_recording(
            scripted_review(behaviours=("finding", "abstain", "finding"))
        )

        abstained = [event for event in events if event["action"] == "abstain"]
        assert len(abstained) == 1
        assert abstained[0]["component"] == "Claude"
        assert abstained[0]["level"] == EVENT_LEVEL_WARN
        assert "abstained" in abstained[0]["message"]

    def test_a_gate_failure_is_logged_and_the_review_continues(self) -> None:
        review = scripted_review(
            check=FakeCheck(failure=RuntimeError("the control plane is broken"))
        )

        events = run_recording(review)

        failed = [event for event in events if event["component"] == "Gate"][0]
        assert failed["level"] == EVENT_LEVEL_ERROR
        assert failed["action"] == "error"
        assert "RuntimeError" in failed["message"]
        assert "the control plane is broken" not in json.dumps(events)
        assert stages(events)[-1] == ("CoreWorker", "review-complete")

    def test_judge_not_consulted_is_logged_explicitly(self) -> None:
        events = run_recording(scripted_review())

        judge = [event for event in events if event["component"] == "Judge"][0]

        assert judge["action"] == "not-consulted"
        assert judge["level"] == EVENT_LEVEL_INFO
        assert judge["message"] == (
            "judge not consulted: no evidence conflict was merged"
        )
        assert not any(
            event["component"] == "Judge" and event["action"] == "start"
            for event in events
        )

    def test_a_judge_without_an_implementation_is_logged_too(self) -> None:
        review = scripted_review(
            declarations=(
                ("openai", EvidenceRelation.SUPPORTING, RULE_ONE),
                ("claude", EvidenceRelation.CONTRADICTING, RULE_ONE),
            ),
            judge=JudgeUseCase(),
        )

        events = run_recording(review)

        judge = [event for event in events if event["component"] == "Judge"]
        # the use-case was asked, found no implementation, and said so
        assert [event["action"] for event in judge] == ["start", "not-consulted"]
        assert "no judge implementation" in judge[-1]["message"]

    def test_a_consulted_judge_logs_start_and_result(self) -> None:
        review = scripted_review(
            declarations=(
                ("openai", EvidenceRelation.SUPPORTING, RULE_ONE),
                ("claude", EvidenceRelation.CONTRADICTING, RULE_ONE),
            )
        )

        events = run_recording(review)

        assert ("Judge", "start") in stages(events)
        assert ("Judge", "result") in stages(events)

    def test_a_failing_judge_is_logged_as_an_error(self) -> None:
        review = scripted_review(
            declarations=(
                ("openai", EvidenceRelation.SUPPORTING, RULE_ONE),
                ("claude", EvidenceRelation.CONTRADICTING, RULE_ONE),
            ),
            judge=JudgeUseCase(
                judge=FakeJudge(
                    failure=RuntimeError("the judge provider exploded")
                )
            ),
        )

        events = run_recording(review)

        judge = [event for event in events if event["component"] == "Judge"]
        assert [event["action"] for event in judge] == ["start", "error"]
        assert judge[-1]["level"] == EVENT_LEVEL_ERROR
        assert "RuntimeError" in judge[-1]["message"]
        assert "the judge provider exploded" not in json.dumps(events)
        assert stages(events)[-1] == ("CoreWorker", "review-complete")

    def test_a_completed_review_is_logged_with_its_id(self) -> None:
        events = run_recording(scripted_review())

        completed = [
            event for event in events if event["action"] == "review-complete"
        ]

        assert len(completed) == 1
        assert completed[0]["level"] == EVENT_LEVEL_INFO
        assert completed[0]["review_id"].startswith("architecture-review-1-")
        assert "3 advisor(s)" in completed[0]["message"]
        # the early stages run before the content-addressed id exists, so only
        # the later ones can carry it
        assert events[0]["review_id"] is None
        assert [event["review_id"] for event in events[-5:]] == (
            [completed[0]["review_id"]] * 5
        )

    def test_every_review_event_uses_the_documented_vocabulary(self) -> None:
        events = run_recording(
            scripted_review(behaviours=("finding", "abstain", "error"))
        )

        for event in events:
            assert set(event) == set(EVENT_FIELDS)
            assert event["level"] in EVENT_LEVELS
            assert event["component"] in LOG_COMPONENTS
            assert event["message"]
            assert json.loads(json.dumps(event)) == event

    def test_a_failing_hook_never_fails_the_review(self) -> None:
        def explode(_event: Any) -> None:
            raise RuntimeError("the UI is gone")

        review = scripted_review()

        result = review.review(
            make_snapshot(), question=QUESTION, on_event=explode
        )

        assert result.review_id.startswith("architecture-review-1-")


# ---------------------------------------------------------------------------
# the panel: read-only view, plain data, no core access from the Tk thread
# ---------------------------------------------------------------------------


class BlockedWorker:
    """A worker that fails loudly if the Tk thread reaches for it at all."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the Tk thread touched the core: {name}")


class BlockedRunner:
    """A runner whose worker is off limits - it proves the boundary."""

    def __init__(self) -> None:
        self.worker = BlockedWorker()
        self.submitted: list[str] = []
        self.is_busy = False

    def submit(self, label: str, action: Any) -> bool:
        self.submitted.append(label)
        return True


class TestTheLogsInThePanel:
    """The controller renders the log from plain data and nothing else."""

    def test_the_log_view_is_read_only_plain_data(self) -> None:
        controller = GuiController(BlockedRunner(), actor="operator")

        controller.log("one entry", component="GUI", action="refresh")
        controller.drain_events([an_event(component="Gate", action="check")])

        view = controller.view_model()["logs"]
        assert json.loads(json.dumps(view)) == view
        assert view["count"] == 2
        assert view["limit"] == controller.log_limit
        for row in view["rows"]:
            assert all(isinstance(cell, str) for cell in row)

    def test_the_newest_entry_is_first(self) -> None:
        controller = GuiController(BlockedRunner(), actor="operator")

        controller.drain_events(
            [
                an_event(action="first", message="older"),
                an_event(action="second", message="newer"),
            ]
        )

        rows = controller.log_rows()
        assert [row[4] for row in rows] == ["second", "first"]
        assert rows[0][5] == "newer"
        # the data order is still the order it happened in
        assert [entry["action"] for entry in controller.log_entries()] == [
            "first",
            "second",
        ]

    def test_the_tk_thread_never_touches_the_core_or_a_provider(self) -> None:
        runner = BlockedRunner()
        controller = GuiController(runner, actor="operator")

        controller.log("a panel entry")
        controller.drain_events([an_event()])
        assert controller.submit("refresh") is True
        controller.apply_result(
            JobResult(
                label="refresh",
                payload={"payload": {"health": {"step_count": 0}}, "audit": []},
            )
        )
        controller.drain_progress(["GUI: refresh"])
        json.dumps(controller.view_model())
        controller.clear_log_view()

        assert runner.submitted == ["refresh"]
        assert controller.log_count == 0

    def test_a_queue_payload_that_is_not_an_event_is_dropped(self) -> None:
        controller = GuiController(BlockedRunner(), actor="operator")

        controller.drain_events(
            [
                an_event(action="good"),
                {"level": "TRACE", "component": "GUI"},
                "not an event",  # type: ignore[list-item]
                {**an_event(), "step_no": "nine"},
            ]
        )

        assert [entry["action"] for entry in controller.log_entries()] == ["good"]

    def test_a_secret_cannot_reach_the_view(self) -> None:
        controller = GuiController(BlockedRunner(), actor="operator")

        controller.log(
            f"rejected: Authorization: Bearer abcdef123456 with {KEY}",
            level=EVENT_LEVEL_WARN,
            action="auth",
        )
        controller.drain_events(
            [
                an_event(
                    component="OpenAI", action="error", message=f"api_key={KEY}"
                )
            ]
        )

        blob = json.dumps(controller.view_model()["logs"])
        assert "abcdef123456" not in blob
        assert "sk-test-not-a-real-secret" not in blob
        assert "***REDACTED***" in blob
        assert "Bearer abcdef123456" not in "\n".join(controller.log_lines())

    def test_the_stage_label_follows_the_newest_event(self) -> None:
        controller = GuiController(BlockedRunner(), actor="operator")

        controller.drain_progress(["OpenAI: start"])
        controller.drain_events(
            [an_event(component="Judge", action="not-consulted")]
        )

        assert controller.review_progress == "Judge: not-consulted"

    def test_the_log_limit_is_respected_by_the_view(self) -> None:
        controller = GuiController(BlockedRunner(), actor="operator", log_limit=5)

        controller.drain_events(
            [an_event(action=f"action-{index}") for index in range(12)]
        )

        assert controller.log_count == 5
        assert [entry["action"] for entry in controller.log_entries()] == [
            f"action-{index}" for index in range(7, 12)
        ]


class TestClearingTheView:
    """Clearing the view deletes nothing - the view is not storage."""

    def test_clearing_empties_the_view_and_keeps_the_state(self, tmp_path) -> None:
        composition = compose(make_config(tmp_path))
        try:
            storage = composition.storage
            storage.steps.upsert(make_step())
            before = (storage.steps.list(), storage.audit.list())

            controller = GuiController(BlockedRunner(), actor="operator")
            controller.log("one entry")
            controller.drain_events([an_event(action="context")])

            assert controller.log_count == 2
            controller.clear_log_view()

            assert controller.log_count == 0
            assert controller.log_lines() == []
            assert controller.log_rows() == []
            assert controller.view_model()["logs"]["count"] == 0
            assert "Nothing was deleted" in controller.status
            assert (storage.steps.list(), storage.audit.list()) == before
        finally:
            composition.close()

    def test_new_events_appear_after_a_clear(self) -> None:
        controller = GuiController(BlockedRunner(), actor="operator")

        controller.log("before")
        controller.clear_log_view()
        controller.drain_events([an_event(action="after")])

        assert [entry["action"] for entry in controller.log_entries()] == ["after"]


# ---------------------------------------------------------------------------
# the composed logs: a real worker, a real database, no API key anywhere
# ---------------------------------------------------------------------------


class TestTheComposedLogs:
    """The whole path, offline: worker queue, sanitized events, nothing written."""

    def test_a_review_logs_its_start_and_its_completion(self, tmp_path) -> None:
        worker = CoreWorker(make_config(tmp_path))
        worker.open()
        try:
            worker.run_architecture_review(QUESTION)
            events = worker.events()
        finally:
            worker.close()

        actions = [event["action"] for event in events]
        assert actions[0] == "review-start"
        assert actions[-1] == "review-complete"
        assert [event["seq"] for event in events] == list(
            range(1, len(events) + 1)
        )
        assert events[-1]["review_id"].startswith("architecture-review-")

    def test_an_offline_review_leaks_no_secret(self, tmp_path) -> None:
        worker = CoreWorker(make_config(tmp_path))
        worker.open()
        try:
            worker.run_architecture_review(QUESTION)
            blob = json.dumps(worker.events())
        finally:
            worker.close()

        for forbidden in (
            "sk-",
            "Bearer",
            "Authorization",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "XAI_API_KEY",
            "api_key",
            "api-key",
        ):
            assert forbidden not in blob
        # the failure is still useful: the exception type travels, nothing else
        assert "MissingApiKeyError" in blob or "AdvisorError" in blob

    def test_every_composed_event_is_documented_and_ordered(self, tmp_path) -> None:
        worker = CoreWorker(make_config(tmp_path))
        worker.open()
        try:
            worker.run_architecture_review(QUESTION)
            events = worker.events()
        finally:
            worker.close()

        assert len(events) >= 6
        for event in events:
            assert set(event) == set(EVENT_FIELDS)
            assert event["level"] in EVENT_LEVELS
            assert event["component"] in LOG_COMPONENTS
            assert event["seq"] is not None
            assert json.loads(json.dumps(event)) == event
        assert [event["seq"] for event in events] == sorted(
            event["seq"] for event in events
        )
        # with no plan there is no current step: the gate says so out loud, and
        # the judge is explicitly *not* consulted rather than silently skipped
        assert any(
            event["component"] == "Gate"
            and "gate unavailable" in event["message"]
            for event in events
        )
        assert any(event["action"] == "not-consulted" for event in events)

    def test_a_review_failure_is_always_logged(self, tmp_path) -> None:
        worker = CoreWorker(make_config(tmp_path))
        worker.open()
        try:
            # break the source of truth underneath the worker, as a crash would
            with sqlite3.connect(str(worker.config.database_path)) as handle:
                handle.execute("DELETE FROM project")
            with pytest.raises(BaseException):
                worker.run_architecture_review(QUESTION)
            events = worker.events()
        finally:
            worker.close()

        failure = events[-1]
        assert failure["component"] == "CoreWorker"
        assert failure["action"] == "review-error"
        assert failure["level"] == EVENT_LEVEL_ERROR
        assert failure["message"].startswith("architecture review failed: ")
        # the type name only - never the exception message
        assert "ProjectMissingError" in failure["message"]
        # the run was announced before the failing read, so both ends are visible
        assert [event["action"] for event in events] == [
            "review-start",
            "review-error",
        ]

    def test_the_panel_drains_a_real_review_into_its_view(self, tmp_path) -> None:
        worker = CoreWorker(make_config(tmp_path))
        worker.open()
        controller = GuiController(BlockedRunner(), actor="operator")
        try:
            worker.run_architecture_review(QUESTION)
            controller.drain_events(worker.events())
            controller.apply_result(
                JobResult(
                    label="run_review",
                    payload={
                        "review": {},
                        "payload": {"health": {"step_count": 0}},
                    },
                )
            )
        finally:
            worker.close()

        rows = controller.log_rows()
        assert any(row[3] == "OpenAI" for row in rows)
        assert any(row[3] == "Judge" for row in rows)
        view = controller.view_model()["logs"]
        assert json.loads(json.dumps(view)) == view
        assert view["rows"][0][4] == "run_review"  # the panel's own summary, newest
