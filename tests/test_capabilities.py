"""Tests for the capability ports and their provider-neutral message objects."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from typing import Any, Mapping

import pytest

from architecture_assistant.domain.enums import Phase, ReportStatus, Severity
from architecture_assistant.domain.models import Decision, Finding, Task
from architecture_assistant.ports import (
    AdvisorPort,
    AdvisorQuery,
    CostIdentityConflictError,
    CostIdentityUnavailableError,
    CostPort,
    CostQuery,
    CostRecord,
    CostSummary,
    JudgeConflict,
    JudgePort,
    Notification,
    NotifierPort,
    PluginCapability,
    ReportingPort,
    WorkerPort,
    WorkerRequest,
    WorkerResult,
)

NOW = datetime(2026, 9, 20, 20, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 9, 20, 21, 0, tzinfo=timezone.utc)


def make_task() -> Task:
    return Task(
        step_no=6, phase=Phase.PLUGINS, title="Hybrid Plugin Core + Ports"
    )


def make_finding(source: str = "openai") -> Finding:
    return Finding(id=f"F-{source}", source=source, claim="a claim")


class TestPluginCapability:
    def test_exposes_the_seven_capabilities(self) -> None:
        assert {member.value for member in PluginCapability} == {
            "worker",
            "advisor",
            "judge",
            "storage",
            "cost",
            "reporting",
            "notifier",
        }

    def test_is_a_stable_string_enum(self) -> None:
        assert PluginCapability.WORKER == "worker"
        assert str(PluginCapability.JUDGE) == "judge"


class TestWorkerRequest:
    def test_holds_task_and_context(self) -> None:
        request = WorkerRequest(task=make_task(), context={"step": 6})
        assert request.task.step_no == 6
        assert dict(request.context) == {"step": 6}

    def test_context_defaults_to_empty_mapping(self) -> None:
        assert dict(WorkerRequest(task=make_task()).context) == {}

    def test_requires_a_task(self) -> None:
        with pytest.raises(ValueError, match="task must be a Task"):
            WorkerRequest(task="not-a-task")

    def test_is_frozen(self) -> None:
        request = WorkerRequest(task=make_task())
        with pytest.raises(FrozenInstanceError):
            request.task = make_task()

    def test_to_dict_is_json_safe_and_deterministic(self) -> None:
        request = WorkerRequest(task=make_task(), context={"b": 1, "a": 2})
        payload = request.to_dict()
        assert json.loads(json.dumps(payload)) == payload
        assert payload == request.to_dict()
        assert payload["task"]["step_no"] == 6


class TestWorkerResult:
    def test_defaults(self) -> None:
        result = WorkerResult(status=ReportStatus.DONE)
        assert result.status is ReportStatus.DONE
        assert result.summary == ""
        assert result.artifacts == ()
        assert result.issues == ()
        assert result.architecture_questions == ()
        assert result.raw is None

    def test_coerces_string_status(self) -> None:
        assert WorkerResult(status="REVISE").status is ReportStatus.REVISE

    def test_unknown_status_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="status must be one of"):
            WorkerResult(status="NOPE")

    def test_sequences_are_frozen_to_tuples(self) -> None:
        result = WorkerResult(
            status=ReportStatus.DONE,
            artifacts=["a.py", "b.py"],
            issues=["one"],
            architecture_questions=["q1"],
        )
        assert result.artifacts == ("a.py", "b.py")
        assert result.issues == ("one",)
        assert result.architecture_questions == ("q1",)

    def test_bare_string_sequence_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be a sequence of strings"):
            WorkerResult(status=ReportStatus.DONE, artifacts="a.py")

    def test_raw_must_be_a_mapping_when_present(self) -> None:
        with pytest.raises(ValueError, match="raw must be a mapping"):
            WorkerResult(status=ReportStatus.DONE, raw=["nope"])

    def test_is_frozen(self) -> None:
        result = WorkerResult(status=ReportStatus.DONE)
        with pytest.raises(FrozenInstanceError):
            result.status = ReportStatus.FAILED

    def test_to_dict_is_json_safe_and_deterministic(self) -> None:
        result = WorkerResult(
            status=ReportStatus.BLOCKED,
            summary="blocked on input",
            artifacts=("a.py",),
            issues=("missing spec",),
            architecture_questions=("which store?",),
            raw={"status": "BLOCKED"},
        )
        payload = result.to_dict()
        assert json.loads(json.dumps(payload)) == payload
        assert payload == {
            "status": "BLOCKED",
            "summary": "blocked on input",
            "artifacts": ["a.py"],
            "issues": ["missing spec"],
            "architecture_questions": ["which store?"],
            "raw": {"status": "BLOCKED"},
        }

    def test_to_dict_keeps_raw_none_distinguishable(self) -> None:
        assert WorkerResult(status=ReportStatus.DONE).to_dict()["raw"] is None


class TestAdvisorQuery:
    def test_holds_question_context_and_step(self) -> None:
        query = AdvisorQuery(
            question="Is this sound?", context={"k": "v"}, step_no=6
        )
        assert query.question == "Is this sound?"
        assert dict(query.context) == {"k": "v"}
        assert query.step_no == 6

    def test_defaults(self) -> None:
        query = AdvisorQuery(question="q")
        assert dict(query.context) == {}
        assert query.step_no is None

    def test_empty_question_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="question must be a non-empty"):
            AdvisorQuery(question="   ")

    def test_non_positive_step_no_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="step_no must be >= 1"):
            AdvisorQuery(question="q", step_no=0)

    def test_is_frozen(self) -> None:
        query = AdvisorQuery(question="q")
        with pytest.raises(FrozenInstanceError):
            query.question = "other"


class TestJudgeConflict:
    def test_holds_findings(self) -> None:
        findings = (make_finding("openai"), make_finding("claude"))
        conflict = JudgeConflict(question="Which holds?", findings=findings)
        assert conflict.findings == findings
        assert dict(conflict.context) == {}

    def test_requires_at_least_one_finding(self) -> None:
        with pytest.raises(ValueError, match="at least one Finding"):
            JudgeConflict(question="q", findings=())

    def test_rejects_non_finding_entries(self) -> None:
        with pytest.raises(ValueError, match="must contain Finding objects"):
            JudgeConflict(question="q", findings=("nope",))

    def test_is_frozen(self) -> None:
        conflict = JudgeConflict(question="q", findings=(make_finding(),))
        with pytest.raises(FrozenInstanceError):
            conflict.question = "other"


class TestCostRecord:
    def test_defaults(self) -> None:
        record = CostRecord(event_id="evt-1", provider="openai")
        assert record.event_id == "evt-1"
        assert record.model == ""
        assert record.input_tokens == 0
        assert record.output_tokens == 0
        assert record.cost_usd == 0.0
        # unknown unless explicitly priced - never assumed to be free
        assert record.pricing_known is False
        assert record.project is None
        assert record.step_no is None
        assert isinstance(record.created_at, datetime)

    def test_full_record(self) -> None:
        record = CostRecord(
            event_id="openai:chatcmpl-abc",
            provider="claude",
            model="opus",
            input_tokens=100,
            output_tokens=50,
            cost_usd=1.25,
            pricing_known=True,
            project="Architecture Lifecycle Assistant",
            step_no=6,
            created_at=NOW,
        )
        assert record.event_id == "openai:chatcmpl-abc"
        assert record.cost_usd == 1.25
        assert record.pricing_known is True
        assert record.step_no == 6
        assert record.created_at == NOW

    def test_empty_provider_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="provider must be a non-empty"):
            CostRecord(event_id="evt-1", provider="")

    def test_negative_token_counts_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="input_tokens must be >= 0"):
            CostRecord(event_id="evt-1", provider="p", input_tokens=-1)
        with pytest.raises(ValueError, match="output_tokens must be >= 0"):
            CostRecord(event_id="evt-1", provider="p", output_tokens=-1)

    def test_negative_cost_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="cost_usd must be >= 0"):
            CostRecord(event_id="evt-1", provider="p", cost_usd=-0.01)

    def test_non_positive_step_no_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="step_no must be >= 1"):
            CostRecord(event_id="evt-1", provider="p", step_no=0)

    def test_empty_event_id_is_rejected(self) -> None:
        """Without a stable identity there is no idempotent cost event."""
        with pytest.raises(ValueError, match="event_id must be a non-empty"):
            CostRecord(event_id="", provider="p")
        with pytest.raises(ValueError, match="event_id must be a non-empty"):
            CostRecord(event_id="   ", provider="p")

    def test_non_bool_pricing_known_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="pricing_known must be a bool"):
            CostRecord(event_id="evt-1", provider="p", pricing_known=1)

    def test_an_unknown_price_is_not_the_same_as_a_real_zero_price(self) -> None:
        """Both carry 0.0 - only ``pricing_known`` tells them apart."""
        unpriced = CostRecord(event_id="evt-1", provider="p", cost_usd=0.0)
        priced_zero = CostRecord(
            event_id="evt-2", provider="p", cost_usd=0.0, pricing_known=True
        )

        assert unpriced.cost_usd == priced_zero.cost_usd == 0.0
        assert unpriced.pricing_known is False
        assert priced_zero.pricing_known is True
        assert unpriced != priced_zero

    def test_to_dict_is_json_safe_and_deterministic(self) -> None:
        payload = CostRecord(event_id="evt-1", provider="p", step_no=6, created_at=NOW).to_dict()
        assert json.loads(json.dumps(payload)) == payload
        assert payload["created_at"] == NOW.isoformat()
        assert payload["event_id"] == "evt-1"
        assert payload["pricing_known"] is False
        assert payload == CostRecord(event_id="evt-1", provider="p", step_no=6, created_at=NOW).to_dict()



class TestCostQuery:
    def test_empty_query_matches_everything(self) -> None:
        assert CostQuery().matches(CostRecord(event_id="evt-1", provider="any")) is True

    def test_from_time_after_to_time_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be <= to_time"):
            CostQuery(from_time=LATER, to_time=NOW)

    def test_filters_by_project(self) -> None:
        query = CostQuery(project="A")
        assert query.matches(CostRecord(event_id="evt-1", provider="p", project="A")) is True
        assert query.matches(CostRecord(event_id="evt-1", provider="p", project="B")) is False
        assert query.matches(CostRecord(event_id="evt-1", provider="p")) is False

    def test_filters_by_step(self) -> None:
        query = CostQuery(step_no=6)
        assert query.matches(CostRecord(event_id="evt-1", provider="p", step_no=6)) is True
        assert query.matches(CostRecord(event_id="evt-1", provider="p", step_no=7)) is False
        assert query.matches(CostRecord(event_id="evt-1", provider="p")) is False

    def test_filters_by_provider(self) -> None:
        query = CostQuery(provider="openai")
        assert query.matches(CostRecord(event_id="evt-1", provider="openai")) is True
        assert query.matches(CostRecord(event_id="evt-1", provider="claude")) is False

    def test_filters_by_time_window(self) -> None:
        query = CostQuery(from_time=NOW, to_time=LATER)
        earlier = CostRecord(
            event_id="evt-1", provider="p", created_at=datetime(2026, 9, 20, 19, 0, tzinfo=timezone.utc)
        )
        later = CostRecord(
            event_id="evt-1", provider="p", created_at=datetime(2026, 9, 20, 22, 0, tzinfo=timezone.utc)
        )
        assert query.matches(CostRecord(event_id="evt-1", provider="p", created_at=NOW)) is True
        assert query.matches(CostRecord(event_id="evt-1", provider="p", created_at=LATER)) is True
        assert query.matches(earlier) is False
        assert query.matches(later) is False

    def test_all_filters_combine_with_and(self) -> None:
        query = CostQuery(
            project="A",
            step_no=6,
            provider="openai",
            from_time=NOW,
            to_time=LATER,
        )
        matching = CostRecord(
            event_id="evt-1", provider="openai", project="A", step_no=6, created_at=NOW
        )
        wrong_step = CostRecord(
            event_id="evt-1", provider="openai", project="A", step_no=7, created_at=NOW
        )
        assert query.matches(matching) is True
        assert query.matches(wrong_step) is False

    def test_to_dict_is_json_safe(self) -> None:
        query = CostQuery(
            project="A", step_no=6, provider="p", from_time=NOW, to_time=LATER
        )
        payload = query.to_dict()
        assert json.loads(json.dumps(payload)) == payload
        assert payload["from_time"] == NOW.isoformat()
        assert CostQuery().to_dict()["from_time"] is None

    def test_is_frozen(self) -> None:
        query = CostQuery()
        with pytest.raises(FrozenInstanceError):
            query.provider = "x"


class TestCostSummary:
    def test_defaults(self) -> None:
        summary = CostSummary()
        assert summary.total_usd == 0.0
        assert summary.input_tokens == 0
        assert summary.output_tokens == 0
        assert summary.record_count == 0
        assert summary.priced_record_count == 0
        assert summary.unpriced_record_count == 0

    def test_negative_total_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="total_usd must be >= 0"):
            CostSummary(total_usd=-1.0)

    def test_negative_record_count_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="record_count must be >= 0"):
            CostSummary(record_count=-1)

    def test_negative_priced_counts_are_rejected(self) -> None:
        with pytest.raises(
            ValueError, match="priced_record_count must be >= 0"
        ):
            CostSummary(priced_record_count=-1)
        with pytest.raises(
            ValueError, match="unpriced_record_count must be >= 0"
        ):
            CostSummary(unpriced_record_count=-1)

    def test_the_priced_counts_must_partition_the_records(self) -> None:
        """Every record is either priced or unpriced - never neither."""
        with pytest.raises(
            ValueError, match="must equal record_count"
        ):
            CostSummary(record_count=2, priced_record_count=1)

    def test_an_unpriced_record_is_not_reported_as_a_known_zero_cost(
        self,
    ) -> None:
        priced = CostSummary(
            total_usd=1.5,
            input_tokens=10,
            output_tokens=5,
            record_count=1,
            priced_record_count=1,
        )
        unpriced = CostSummary(
            input_tokens=10,
            output_tokens=5,
            record_count=1,
            unpriced_record_count=1,
        )

        assert priced.total_usd == 1.5
        assert unpriced.total_usd == 0.0
        # the figures are equal-looking, so the contract must say why
        assert unpriced.priced_record_count == 0
        assert unpriced.unpriced_record_count == 1
        assert unpriced.to_dict()["unpriced_record_count"] == 1

    def test_to_dict_is_json_safe_and_deterministic(self) -> None:
        summary = CostSummary(
            total_usd=1.5,
            input_tokens=10,
            output_tokens=5,
            record_count=2,
            priced_record_count=1,
            unpriced_record_count=1,
        )
        payload = summary.to_dict()
        assert json.loads(json.dumps(payload)) == payload
        assert payload == summary.to_dict()
        assert payload["priced_record_count"] == 1
        assert payload["unpriced_record_count"] == 1


class TestCostIdentityErrors:
    """The two cost-accounting failures are part of the provider-neutral port."""

    def test_the_conflict_error_is_a_plain_exception(self) -> None:
        assert issubclass(CostIdentityConflictError, Exception)

    def test_the_unavailable_identity_error_is_a_plain_exception(self) -> None:
        assert issubclass(CostIdentityUnavailableError, Exception)

    def test_the_two_errors_are_distinct(self) -> None:
        assert CostIdentityConflictError is not CostIdentityUnavailableError
        assert not issubclass(
            CostIdentityConflictError, CostIdentityUnavailableError
        )


class TestNotification:
    def test_defaults_and_enum_coercion(self) -> None:
        message = Notification(level="HIGH", title="Blocked")
        assert message.level is Severity.HIGH
        assert message.body == ""

    def test_empty_title_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="title must be a non-empty"):
            Notification(level=Severity.LOW, title="   ")

    def test_unknown_level_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="level must be one of"):
            Notification(level="NOPE", title="t")

    def test_to_dict_is_json_safe(self) -> None:
        payload = Notification(level=Severity.LOW, title="t", body="b").to_dict()
        assert json.loads(json.dumps(payload)) == payload
        assert payload == {"level": "LOW", "title": "t", "body": "b"}



# ---------------------------------------------------------------------------
# structural conformance: the ports stay provider-neutral
# ---------------------------------------------------------------------------

class _FakeWorker:
    def run(self, request: WorkerRequest) -> WorkerResult:
        return WorkerResult(status=ReportStatus.DONE)


class _FakeAdvisor:
    def advise(self, query: AdvisorQuery) -> Finding:
        return make_finding()


class _FakeJudge:
    def judge(self, conflict: JudgeConflict) -> Decision:
        return Decision(id="D-1", decision="go")


class _FakeCost:
    def record(self, cost: CostRecord) -> None:
        return None

    def query(self, cost_filter: CostQuery) -> CostSummary:
        return CostSummary()


class _FakeReporting:
    def render(self, payload: Mapping[str, Any]) -> str:
        return "# report"


class _FakeNotifier:
    def notify(self, message: Notification) -> None:
        return None


class TestPortConformance:
    @pytest.mark.parametrize(
        "fake,port",
        [
            (_FakeWorker(), WorkerPort),
            (_FakeAdvisor(), AdvisorPort),
            (_FakeJudge(), JudgePort),
            (_FakeCost(), CostPort),
            (_FakeReporting(), ReportingPort),
            (_FakeNotifier(), NotifierPort),
        ],
        ids=["worker", "advisor", "judge", "cost", "reporting", "notifier"],
    )
    def test_a_structural_implementation_satisfies_the_port(
        self, fake: Any, port: type
    ) -> None:
        assert isinstance(fake, port)

    def test_ports_are_distinct_contracts(self) -> None:
        assert not isinstance(_FakeWorker(), AdvisorPort)
        assert not isinstance(_FakeAdvisor(), WorkerPort)
        assert not isinstance(_FakeJudge(), NotifierPort)
        assert not isinstance(_FakeNotifier(), JudgePort)

    def test_ports_expose_only_the_expected_methods(self) -> None:
        assert not hasattr(_FakeWorker(), "advisor")
        assert not hasattr(_FakeAdvisor(), "run")
        assert set(vars(_FakeWorker)) >= {"run"}

    def test_capability_ports_have_no_storage_or_transaction_surface(self) -> None:
        for fake in (_FakeWorker(), _FakeAdvisor(), _FakeJudge(), _FakeCost(),
                     _FakeReporting(), _FakeNotifier()):
            assert not hasattr(fake, "transaction")
            assert not hasattr(fake, "connection")

