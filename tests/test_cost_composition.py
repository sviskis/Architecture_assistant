"""Tests for the Step 17 cost-plugin composition wiring.

The plugin itself is covered in ``test_cost_plugin.py``; what matters here is the
*wiring*: one ``CostPort`` instance, shared by the OpenAI advisor, the Claude
advisor, the Grok advisor and the OpenAI judge, injected as the same object, and
kept strictly as telemetry - it never enters the decision path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from architecture_assistant.composition import (
    DEFAULT_SOURCE_ROOT,
    Composition,
    CompositionConfig,
    compose,
)
from architecture_assistant.domain.enums import (
    DecisionStatus,
    Mode,
    Phase,
    RiskLevel,
    StepState,
)
from architecture_assistant.domain.models import Finding, Step
from architecture_assistant.infrastructure import (
    ClaudeAdvisorAdapter,
    GrokAdvisorAdapter,
    HttpResponse,
    OpenAIAdvisorAdapter,
    OpenAIJudgeAdapter,
    SqliteCostPlugin,
)
from architecture_assistant.ports.capabilities import (
    AdvisorQuery,
    CostPort,
    CostQuery,
    CostSummary,
    JudgeConflict,
)

FIXED = datetime(2026, 9, 21, 15, 0, tzinfo=timezone.utc)
PROJECT = "Architecture Lifecycle Assistant"

#: A literal that is not, and never was, a real credential.
KEY = "sk-test-not-a-real-secret"

ADVISOR_ANSWER = {
    "status": "OK",
    "claim": "Extract the snapshot builder into a pure helper.",
    "evidence": ["application/context.py:120"],
    "confidence": 0.8,
    "severity": "MEDIUM",
    "reason": "",
}

JUDGE_ANSWER = {
    "status": "ABSTAIN",
    "rationale": "the supplied findings are inconclusive",
    "evidence_refs": ["f-1"],
    "reason": "thin evidence",
}


def make_query() -> AdvisorQuery:
    return AdvisorQuery(
        question="How should the cost telemetry be persisted?",
        step_no=17,
        context={"module": "infrastructure/cost.py"},
    )


def make_conflict() -> JudgeConflict:
    return JudgeConflict(
        question="Unresolved evidence conflict on cost-table: supporting vs contradicting",
        findings=(
            Finding(
                id="f-1",
                source="openai",
                claim="a cost record may reference a step that does not exist",
            ),
        ),
        context={"target": "cost-table", "supporting_ids": ["f-1"]},
    )


def envelope(answer: dict) -> dict:
    """A provider response with a stable id and usable usage."""
    return {
        "id": "chatcmpl-composed",
        "choices": [
            {"message": {"role": "assistant", "content": json.dumps(answer)}}
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30},
    }


@dataclass
class FakeTransport:
    """A constant offline endpoint - the composed graph is never really called."""

    payload: dict = field(default_factory=lambda: envelope(ADVISOR_ANSWER))

    def __call__(self, request) -> HttpResponse:
        return HttpResponse(200, json.dumps(self.payload).encode("utf-8"))


def fixed_clock() -> datetime:
    return FIXED


def _src_root() -> Path:
    return Path(__file__).resolve().parents[1] / "src" / "architecture_assistant"


def make_step(step_no: int = 17) -> Step:
    return Step(
        step_no=step_no,
        phase=Phase.PLUGINS,
        title="Cost Plugin",
        state=StepState.READY,
        attempt=1,
        risk=RiskLevel.LOW,
        requires_human=False,
        created_at=FIXED,
    )


def advise_into(plugin: CostPort) -> None:
    """Drive one real advisor call into ``plugin`` and return the finding."""
    OpenAIAdvisorAdapter(
        api_key=KEY,
        transport=FakeTransport(),
        cost_sink=plugin,
        project=PROJECT,
        clock=fixed_clock,
    ).advise(make_query())


@pytest.fixture
def config(tmp_path) -> CompositionConfig:
    return CompositionConfig(
        database_path=tmp_path / "assistant.db",
        exchange_dir=tmp_path / "cline",
        mode=Mode.AUTO,
        timeout=timedelta(hours=1),
        source_root=DEFAULT_SOURCE_ROOT,
        clock=fixed_clock,
    )


@pytest.fixture
def composition(config):
    result = compose(config)
    yield result
    result.close()


class TestOneCostPortInstance:
    """ONE ``CostPort`` instance backs every provider adapter."""

    def test_the_composition_exposes_the_cost_port(self, composition) -> None:
        plugin = composition.cost_plugin

        assert isinstance(plugin, CostPort)
        assert isinstance(plugin, SqliteCostPlugin)

    def test_all_four_adapters_share_the_same_port(self, composition) -> None:
        plugin = composition.cost_plugin

        for adapter in (
            composition.openai_advisor,
            composition.claude_advisor,
            composition.grok_advisor,
            composition.judge,
        ):
            assert adapter._cost_sink is plugin

    def test_the_adapters_are_still_distinct_objects(self, composition) -> None:
        adapters = (
            composition.openai_advisor,
            composition.claude_advisor,
            composition.grok_advisor,
            composition.judge,
        )

        assert len({id(adapter) for adapter in adapters}) == 4

    def test_the_adapter_types_are_the_expected_ones(self, composition) -> None:
        assert isinstance(composition.openai_advisor, OpenAIAdvisorAdapter)
        assert isinstance(composition.claude_advisor, ClaudeAdvisorAdapter)
        assert isinstance(composition.grok_advisor, GrokAdvisorAdapter)
        assert isinstance(composition.judge, OpenAIJudgeAdapter)

    def test_the_composed_plugin_starts_empty(self, composition) -> None:
        assert composition.cost_plugin.query(CostQuery()) == CostSummary()

    def test_the_composition_declares_the_port_type(self) -> None:
        annotation = {item.name: item.type for item in fields(Composition)}[
            "cost_plugin"
        ]

        assert annotation == "CostPort"

    def test_the_composed_summary_is_json_safe(self, composition) -> None:
        payload = composition.cost_plugin.query(CostQuery()).to_dict()

        assert json.loads(json.dumps(payload)) == payload
        assert payload["record_count"] == 0


class TestTheComposedPluginIsUsable:
    """The injected port really persists into the composed database."""

    def test_an_advisor_reports_into_the_composed_plugin(
        self, composition
    ) -> None:
        OpenAIAdvisorAdapter(
            api_key=KEY,
            transport=FakeTransport(),
            cost_sink=composition.cost_plugin,
            project=PROJECT,
            clock=fixed_clock,
        ).advise(make_query())

        summary = composition.cost_plugin.query(CostQuery(project=PROJECT))

        assert summary.record_count == 1
        assert summary.input_tokens == 120
        assert summary.output_tokens == 30
        # no price table is configured here: an unknown, reported as unpriced
        assert summary.unpriced_record_count == 1
        assert summary.priced_record_count == 0

    def test_the_judge_reports_into_the_composed_plugin(
        self, composition
    ) -> None:
        judge = OpenAIJudgeAdapter(
            api_key=KEY,
            transport=FakeTransport(payload=envelope(JUDGE_ANSWER)),
            cost_sink=composition.cost_plugin,
            project=PROJECT,
            clock=fixed_clock,
        )

        decision = judge.judge(make_conflict())

        assert decision.status is DecisionStatus.ABSTAIN
        assert composition.cost_plugin.query(CostQuery()).record_count == 1

    def test_a_replayed_provider_response_adds_no_second_row(
        self, composition
    ) -> None:
        """End-to-end idempotence: the same response is one billable event."""
        advisor = OpenAIAdvisorAdapter(
            api_key=KEY,
            transport=FakeTransport(),
            cost_sink=composition.cost_plugin,
            project=PROJECT,
            clock=fixed_clock,
        )

        advisor.advise(make_query())
        advisor.advise(make_query())

        assert composition.cost_plugin.query(CostQuery()).record_count == 1


class TestCostStaysTelemetry:
    """The plugin is accounting only - it decides nothing."""

    def test_the_plugin_is_not_wired_into_the_decision_path(
        self, composition
    ) -> None:
        plugin = composition.cost_plugin

        assert plugin is not composition.worker
        for collaborator in (
            composition.orchestrator,
            composition.scheduler,
            composition.realization_control,
            composition.realization_adapter,
        ):
            assert plugin is not collaborator
            assert "cost" not in dir(collaborator)

    def test_the_deterministic_loop_records_no_cost(self, composition) -> None:
        """Only a provider call costs money, and the loop makes none."""
        composition.storage.steps.upsert(make_step())

        run = composition.run_until_idle()

        assert run.stopped_because == "worker-report-pending"
        assert composition.cost_plugin.query(CostQuery()) == CostSummary()

    def test_cost_rows_survive_a_recompose(self, config) -> None:
        first = compose(config)
        try:
            advise_into(first.cost_plugin)
            assert first.cost_plugin.query(CostQuery()).record_count == 1
        finally:
            first.close()

        second = compose(config)
        try:
            assert second.cost_plugin.query(CostQuery()).record_count == 1
        finally:
            second.close()

    def test_a_failing_cost_sink_never_changes_the_finding(
        self, composition
    ) -> None:
        """Adapter success + cost failure -> a valid finding, error surfaced."""

        class _BrokenSink:
            def record(self, cost) -> None:
                raise RuntimeError("cost store down")

            def query(self, cost_filter) -> CostSummary:
                return CostSummary()

        advisor = OpenAIAdvisorAdapter(
            api_key=KEY,
            transport=FakeTransport(),
            cost_sink=_BrokenSink(),
            project=PROJECT,
            clock=fixed_clock,
        )

        finding = advisor.advise(make_query())

        assert finding.source == "openai"
        assert finding.evidence
        assert isinstance(advisor.last_telemetry_error, RuntimeError)
