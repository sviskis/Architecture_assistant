"""Offline tests for the Step 16 OpenAI judge adapter.

Everything here runs without a network and without a real key: the transport is a
scripted fake, the key is a literal that never leaves the test process, and the
clock/sleep seams are injected. Coverage follows the mandated properties:

* the adapter implements ``JudgePort`` and reports ``provider == "openai"`` /
  ``role == "Evidence Judge"``;
* ``judge()`` always *returns* a ``Decision`` - it never raises on a provider
  failure - and it never maps a failure onto ``REJECTED``;
* ``ACCEPTED``/``REJECTED`` speak only about the explicit evidence conflict,
  never about architecture compliance;
* every cited ``evidence_ref`` must be one of the supplied findings; a
  fabricated, unknown or duplicated reference is an ``ERROR``;
* only timeouts, HTTP 429 and HTTP 5xx are retried, with an identical payload;
* the API key travels only in the ``Authorization`` header;
* cost telemetry is secondary: a failing sink can never destroy a judgment;
* the model name is configuration - constructor, then environment, then one
  documented adapter default.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
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
from architecture_assistant.domain.enums import DecisionStatus, Severity
from architecture_assistant.domain.models import Decision, Finding
from architecture_assistant.infrastructure import (
    DEFAULT_JUDGE_MAX_OUTPUT_TOKENS,
    DEFAULT_OPENAI_JUDGE_MODEL,
    EVIDENCE_JUDGE_ROLE,
    OPENAI_API_KEY_ENV_VAR,
    OPENAI_JUDGE_MODEL_ENV_VAR,
    UNKNOWN_PRICE,
    OpenAIJudgeAdapter,
    resolve_openai_judge_model,
)
from architecture_assistant.infrastructure._http import (
    DEFAULT_BACKOFF_SCHEDULE,
    DEFAULT_TIMEOUT_SECONDS,
    HttpRequest,
    HttpResponse,
    HttpTransportError,
    ModelPrice,
)
from architecture_assistant.ports.capabilities import (
    CostIdentityUnavailableError,
    CostPort,
    CostQuery,
    CostRecord,
    CostSummary,
    JudgeConflict,
    JudgePort,
)

FIXED = datetime(2026, 9, 21, 13, 0, tzinfo=timezone.utc)
LATER = FIXED + timedelta(hours=3)

#: A literal that is not, and never was, a real credential.
KEY = "sk-test-not-a-real-secret"

TARGET = "forbidden-layer-import"
SUPPORTING_ID = "finding-openai-16-supporting"
CONTRADICTING_ID = "finding-claude-16-contradicting"
QUESTION = (
    f"Unresolved evidence conflict on {TARGET}: supporting {SUPPORTING_ID} vs "
    f"contradicting {CONTRADICTING_ID}"
)


def fixed_clock() -> datetime:
    return FIXED


def make_finding(
    *,
    source: str = "openai",
    claim: str = "a claim",
    finding_id: str = SUPPORTING_ID,
    step_no: int | None = 16,
) -> Finding:
    return Finding(
        id=finding_id,
        source=source,
        claim=claim,
        evidence=("application/context.py:12",),
        confidence=0.5,
        severity=Severity.MEDIUM,
        step_no=step_no,
    )


def make_conflict(**overrides) -> JudgeConflict:
    payload = {
        "question": QUESTION,
        "findings": (
            make_finding(source="openai", finding_id=SUPPORTING_ID),
            make_finding(
                source="claude",
                claim="the opposite",
                finding_id=CONTRADICTING_ID,
            ),
        ),
        "context": {
            "target": TARGET,
            "supporting_ids": [SUPPORTING_ID],
            "contradicting_ids": [CONTRADICTING_ID],
            "gate_status": DecisionStatus.ACCEPTED.value,
        },
    }
    payload.update(overrides)
    return JudgeConflict(**payload)


def contract(**overrides) -> dict:
    """A complete, valid judge answer contract."""
    payload = {
        "status": "ACCEPTED",
        "rationale": "the supporting finding names the rule the other one misses",
        "evidence_refs": [SUPPORTING_ID],
        "reason": "",
    }
    payload.update(overrides)
    return payload


def envelope(content: str, *, usage: bool = True) -> dict:
    payload: dict = {
        "id": "chatcmpl-test",
        "choices": [{"message": {"role": "assistant", "content": content}}],
    }
    if usage:
        payload["usage"] = {"prompt_tokens": 120, "completion_tokens": 30}
    return payload


def body(payload) -> bytes:
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return json.dumps(payload).encode("utf-8")


def answer(**overrides) -> HttpResponse:
    return HttpResponse(200, body(envelope(json.dumps(contract(**overrides)))))


def response(payload, status_code: int = 200) -> HttpResponse:
    return HttpResponse(status_code, body(payload))


def timeout_error() -> HttpTransportError:
    return HttpTransportError("request timed out after 30.0s", timed_out=True)


@dataclass
class FakeTransport:
    """Scripted, recording transport seam (the last item repeats)."""

    script: list = field(default_factory=lambda: [answer()])
    requests: list = field(default_factory=list)

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.script) - 1)
        item = self.script[index]
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def bodies(self) -> list[bytes]:
        return [request.body for request in self.requests]

    @property
    def call_count(self) -> int:
        return len(self.requests)


@dataclass
class RecordingCostSink:
    """A minimal ``CostPort`` that records what the judge reports."""

    records: list = field(default_factory=list)
    error: Exception | None = None

    def record(self, cost: CostRecord) -> None:
        if self.error is not None:
            raise self.error
        self.records.append(cost)

    def query(self, cost_filter: CostQuery) -> CostSummary:
        return CostSummary()


def make_judge(transport=None, **overrides) -> OpenAIJudgeAdapter:
    """An adapter wired for offline testing (no real sleeping, key injected)."""
    kwargs: dict = {
        "transport": transport if transport is not None else FakeTransport(),
        "sleep": lambda _seconds: None,
    }
    kwargs.update(overrides)
    if "api_key" not in kwargs:
        kwargs["api_key"] = KEY
    return OpenAIJudgeAdapter(**kwargs)


class TestPortConformance:
    """An OpenAI adapter that implements the judge capability."""

    def test_the_adapter_implements_the_judge_port(self) -> None:
        judge = make_judge()

        assert isinstance(judge, JudgePort)
        assert judge.provider == "openai"
        assert judge.role == EVIDENCE_JUDGE_ROLE == "Evidence Judge"
        assert judge.model == DEFAULT_OPENAI_JUDGE_MODEL

    def test_the_adapter_has_exactly_one_capability_method(self) -> None:
        assert hasattr(OpenAIJudgeAdapter, "judge")
        for name in ("advise", "decide", "vote", "rank", "merge"):
            assert not hasattr(OpenAIJudgeAdapter, name)

    def test_the_clock_seam_is_injected(self) -> None:
        decision = make_judge(clock=fixed_clock).judge(make_conflict())

        assert decision.created_at == FIXED

    def test_the_conflict_type_is_validated(self) -> None:
        with pytest.raises(ValueError, match="JudgeConflict"):
            make_judge().judge("not a conflict")

    def test_the_decision_id_is_deterministic_and_clock_independent(self) -> None:
        first = make_judge(clock=fixed_clock).judge(make_conflict())
        second = make_judge(clock=lambda: LATER).judge(make_conflict())

        assert first.id == second.id
        assert first.id.startswith("judge-16-")

    def test_the_decision_carries_the_conflict_step(self) -> None:
        decision = make_judge().judge(make_conflict())

        assert decision.step_no == 16

    def test_the_repr_is_key_free(self) -> None:
        judge = make_judge()

        assert KEY not in repr(judge)
        assert "configured=True" in repr(judge)


class TestRequestContract:
    """The OpenAI wire shape - and the evidence discipline in the prompt."""

    def test_the_request_targets_the_openai_endpoint_with_a_timeout(self) -> None:
        transport = FakeTransport()

        make_judge(transport).judge(make_conflict())

        request = transport.requests[0]
        assert request.url == "https://api.openai.com/v1/chat/completions"
        assert request.method == "POST"
        assert request.timeout == DEFAULT_TIMEOUT_SECONDS

    def test_the_key_travels_in_the_authorization_header_only(self) -> None:
        transport = FakeTransport()

        make_judge(transport).judge(make_conflict())

        headers = transport.requests[0].headers
        assert headers["Authorization"] == f"Bearer {KEY}"
        assert "x-api-key" not in headers
        assert KEY not in transport.bodies[0].decode("utf-8")

    def test_the_request_is_a_deterministic_json_completion(self) -> None:
        transport = FakeTransport()

        make_judge(transport).judge(make_conflict())

        payload = json.loads(transport.bodies[0])
        assert payload["model"] == DEFAULT_OPENAI_JUDGE_MODEL
        assert payload["temperature"] == 0
        assert payload["max_tokens"] == DEFAULT_JUDGE_MAX_OUTPUT_TOKENS
        assert payload["response_format"] == {"type": "json_object"}
        assert [message["role"] for message in payload["messages"]] == [
            "system",
            "user",
        ]

    def test_the_system_prompt_states_the_evidence_discipline(self) -> None:
        transport = FakeTransport()

        make_judge(transport).judge(make_conflict())

        system = json.loads(transport.bodies[0])["messages"][0]["content"]
        assert EVIDENCE_JUDGE_ROLE in system
        assert "Use ONLY the findings" in system
        assert "do not browse" in system
        assert "insufficient" in system
        assert "never invent a finding id" in system
        assert "never decide architecture compliance" in system

    def test_the_user_prompt_carries_only_the_supplied_conflict(self) -> None:
        transport = FakeTransport()

        make_judge(transport).judge(make_conflict())

        user = json.loads(transport.bodies[0])["messages"][1]["content"]
        assert QUESTION in user
        assert TARGET in user
        assert SUPPORTING_ID in user
        assert CONTRADICTING_ID in user
        assert '"claim":"a claim"' in user

    def test_the_request_body_is_byte_stable(self) -> None:
        first, second = FakeTransport(), FakeTransport()

        make_judge(first).judge(make_conflict())
        make_judge(second).judge(make_conflict())

        assert first.bodies[0] == second.bodies[0]


class TestVerdictSemantics:
    """ACCEPTED/REJECTED speak about the conflict - never about compliance."""

    def test_an_accepted_verdict_favors_the_supporting_relation(self) -> None:
        decision = make_judge().judge(make_conflict())

        assert decision.status is DecisionStatus.ACCEPTED
        assert "supporting relation" in decision.decision
        assert TARGET in decision.decision
        # the outcome phrase never claims architecture compliance
        assert "compliance" not in decision.decision
        assert "compliant" not in decision.decision

    def test_a_rejected_verdict_favors_the_contradicting_relation(self) -> None:
        transport = FakeTransport(
            [answer(status="REJECTED", evidence_refs=[CONTRADICTING_ID])]
        )

        decision = make_judge(transport).judge(make_conflict())

        assert decision.status is DecisionStatus.REJECTED
        assert "contradicting relation" in decision.decision
        assert "compliance" not in decision.decision

    def test_the_rationale_scopes_itself_to_the_conflict(self) -> None:
        decision = make_judge().judge(make_conflict())

        assert "not a statement about architecture compliance" in (
            decision.rationale
        )
        assert "deterministic gate verdict is unchanged" in decision.rationale
        assert "names the rule the other one misses" in decision.rationale

    def test_the_cited_evidence_is_kept_in_order(self) -> None:
        transport = FakeTransport(
            [
                answer(
                    evidence_refs=[SUPPORTING_ID, CONTRADICTING_ID],
                    rationale="both sides were weighed",
                )
            ]
        )

        decision = make_judge(transport).judge(make_conflict())

        assert decision.evidence_refs == (SUPPORTING_ID, CONTRADICTING_ID)
        assert decision.perspectives == ("claude", "openai")

    def test_a_judge_applies_no_rules(self) -> None:
        decision = make_judge().judge(make_conflict())

        assert decision.rules_applied == ()

    def test_a_lower_case_status_is_accepted(self) -> None:
        transport = FakeTransport([answer(status="accepted")])

        decision = make_judge(transport).judge(make_conflict())

        assert decision.status is DecisionStatus.ACCEPTED


#: Every malformed judge answer that must become an ERROR (never a REJECTED).
MALFORMED_ANSWERS: dict[str, str] = {
    "not-json": "definitely not json",
    "json-array": json.dumps([1, 2]),
    "status-missing": json.dumps(
        {
            "rationale": "r",
            "evidence_refs": [SUPPORTING_ID],
        }
    ),
    "status-unknown": json.dumps(contract(status="MAYBE")),
    "status-not-a-string": json.dumps(contract(status=7)),
    "rationale-missing": json.dumps(
        {"status": "ACCEPTED", "evidence_refs": [SUPPORTING_ID]}
    ),
    "rationale-blank": json.dumps(contract(rationale="   ")),
    "rationale-not-a-string": json.dumps(contract(rationale=42)),
    "refs-missing": json.dumps({"status": "ACCEPTED", "rationale": "r"}),
    "refs-empty": json.dumps(contract(evidence_refs=[])),
    "refs-not-a-list": json.dumps(contract(evidence_refs=SUPPORTING_ID)),
    "refs-fabricated": json.dumps(contract(evidence_refs=["finding-invented-1"])),
    "refs-duplicated": json.dumps(
        contract(evidence_refs=[SUPPORTING_ID, SUPPORTING_ID])
    ),
    "refs-not-strings": json.dumps(contract(evidence_refs=[42])),
    "refs-blank-entry": json.dumps(contract(evidence_refs=["  "])),
}

#: Broken provider envelopes - also an ERROR, never an abstention or a rejection.
BROKEN_ENVELOPES: list = [
    b"not json",
    b"[]",
    body({"choices": []}),
    body({"choices": [{}]}),
    body({"choices": [{"message": {"content": ""}}]}),
    body({"choices": [{"message": {"content": 42}}]}),
]


class TestAbstainAndError:
    """A refusal stays a refusal; every failure becomes ERROR - never REJECTED."""

    def test_an_explicit_abstention_stays_an_abstention(self) -> None:
        transport = FakeTransport(
            [answer(status="ABSTAIN", reason="the supplied evidence is thin")]
        )

        decision = make_judge(transport).judge(make_conflict())

        assert decision.status is DecisionStatus.ABSTAIN
        assert "the supplied evidence is thin" in decision.rationale
        assert transport.call_count == 1

    def test_an_abstention_without_a_reason_uses_the_fallback(self) -> None:
        transport = FakeTransport([answer(status="ABSTAIN", reason="")])

        decision = make_judge(transport).judge(make_conflict())

        assert decision.status is DecisionStatus.ABSTAIN
        assert "without stating a reason" in decision.rationale

    def test_a_missing_key_is_an_error_not_an_exception(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv(OPENAI_API_KEY_ENV_VAR, raising=False)
        transport = FakeTransport()

        decision = make_judge(transport, api_key=None).judge(make_conflict())

        assert decision.status is DecisionStatus.ERROR
        assert "no OpenAI API key available" in decision.rationale
        assert transport.call_count == 0

    @pytest.mark.parametrize("name", sorted(MALFORMED_ANSWERS))
    def test_malformed_output_is_an_error(self, name) -> None:
        transport = FakeTransport(
            [response(body(envelope(MALFORMED_ANSWERS[name])))]
        )

        decision = make_judge(transport).judge(make_conflict())

        assert decision.status is DecisionStatus.ERROR
        assert transport.call_count == 1

    @pytest.mark.parametrize("payload", BROKEN_ENVELOPES)
    def test_a_broken_envelope_is_an_error(self, payload) -> None:
        transport = FakeTransport([HttpResponse(200, payload)])

        decision = make_judge(transport).judge(make_conflict())

        assert decision.status is DecisionStatus.ERROR
        assert transport.call_count == 1

    @pytest.mark.parametrize("name", sorted(MALFORMED_ANSWERS))
    def test_a_failure_is_never_a_rejection(self, name) -> None:
        transport = FakeTransport(
            [response(body(envelope(MALFORMED_ANSWERS[name])))]
        )

        decision = make_judge(transport).judge(make_conflict())

        assert decision.status is not DecisionStatus.REJECTED

    def test_an_http_failure_is_never_a_rejection(self) -> None:
        transport = FakeTransport([HttpResponse(500, b"boom")])

        decision = make_judge(transport).judge(make_conflict())

        assert decision.status is DecisionStatus.ERROR
        assert "HTTP 500" in decision.rationale

    def test_a_transport_failure_is_never_a_rejection(self) -> None:
        transport = FakeTransport([timeout_error()])

        decision = make_judge(transport).judge(make_conflict())

        assert decision.status is DecisionStatus.ERROR
        assert "timed out" in decision.rationale

    def test_a_failure_carries_no_evidence(self) -> None:
        transport = FakeTransport([HttpResponse(503, b"down")])

        decision = make_judge(transport).judge(make_conflict())

        assert decision.evidence_refs == ()
        assert decision.perspectives == ()

    def test_a_fabricated_reference_is_named_in_the_rationale(self) -> None:
        transport = FakeTransport(
            [answer(evidence_refs=["finding-invented-1"])]
        )

        decision = make_judge(transport).judge(make_conflict())

        assert decision.status is DecisionStatus.ERROR
        assert "finding-invented-1" in decision.rationale


class TestRetryPolicy:
    """Retry exactly: timeout, HTTP 429 and HTTP 5xx - nothing else."""

    def test_a_timeout_is_retried_and_then_resolves(self) -> None:
        delays: list[float] = []
        transport = FakeTransport([timeout_error(), answer()])
        judge = make_judge(transport, sleep=delays.append)

        decision = judge.judge(make_conflict())

        assert transport.call_count == 2
        assert decision.status is DecisionStatus.ACCEPTED
        assert delays == [0.0]

    def test_a_429_is_retried_and_then_resolves(self) -> None:
        transport = FakeTransport([HttpResponse(429, b"slow down"), answer()])

        decision = make_judge(transport).judge(make_conflict())

        assert transport.call_count == 2
        assert decision.status is DecisionStatus.ACCEPTED

    def test_a_5xx_is_retried_and_then_resolves(self) -> None:
        transport = FakeTransport([HttpResponse(503, b"down"), answer()])

        decision = make_judge(transport).judge(make_conflict())

        assert transport.call_count == 2
        assert decision.status is DecisionStatus.ACCEPTED

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    def test_a_permanent_client_error_is_never_retried(self, status) -> None:
        transport = FakeTransport([HttpResponse(status, b'{"error":"no"}')])

        decision = make_judge(transport).judge(make_conflict())

        assert decision.status is DecisionStatus.ERROR
        assert transport.call_count == 1

    def test_an_exhausted_timeout_becomes_an_error(self) -> None:
        transport = FakeTransport([timeout_error()])

        decision = make_judge(transport).judge(make_conflict())

        assert decision.status is DecisionStatus.ERROR
        assert transport.call_count == 3

    def test_an_exhausted_429_becomes_an_error(self) -> None:
        transport = FakeTransport([HttpResponse(429, b"slow down")])

        decision = make_judge(transport).judge(make_conflict())

        assert decision.status is DecisionStatus.ERROR
        assert transport.call_count == 3

    def test_a_non_retryable_transport_failure_is_not_retried(self) -> None:
        transport = FakeTransport(
            [HttpTransportError("bad certificate", retryable=False)]
        )

        decision = make_judge(transport).judge(make_conflict())

        assert decision.status is DecisionStatus.ERROR
        assert transport.call_count == 1

    def test_the_retry_payload_is_byte_identical(self) -> None:
        transport = FakeTransport(
            [HttpResponse(503, b"boom"), timeout_error(), answer()]
        )

        make_judge(transport).judge(make_conflict())

        assert transport.call_count == 3
        assert len(set(transport.bodies)) == 1
        assert len({request.url for request in transport.requests}) == 1
        assert len({request.timeout for request in transport.requests}) == 1

    def test_the_backoff_schedule_is_bounded_and_deterministic(self) -> None:
        delays: list[float] = []
        transport = FakeTransport([HttpResponse(500, b"boom")])
        judge = make_judge(transport, max_retries=5, sleep=delays.append)

        judge.judge(make_conflict())

        assert delays == [0.0, 1.0, 2.0, 2.0, 2.0]
        assert DEFAULT_BACKOFF_SCHEDULE == (0.0, 1.0, 2.0)

    def test_the_retry_boundary_is_validated(self) -> None:
        with pytest.raises(ValueError, match="max_retries"):
            make_judge(max_retries=-1)


class TestSecrets:
    """The key lives in the header, nowhere else - and it is redacted."""

    def test_the_environment_key_is_picked_up_lazily(self, monkeypatch) -> None:
        monkeypatch.setenv(OPENAI_API_KEY_ENV_VAR, "sk-env-literal")
        judge = make_judge(api_key=None)

        assert judge.is_configured is True
        assert "sk-env-literal" not in repr(judge)
        assert judge.judge(make_conflict()).status is DecisionStatus.ACCEPTED

    def test_a_blank_key_is_not_a_configured_key(self, monkeypatch) -> None:
        monkeypatch.delenv(OPENAI_API_KEY_ENV_VAR, raising=False)
        judge = make_judge(api_key="   ")

        assert judge.is_configured is False
        assert judge.judge(make_conflict()).status is DecisionStatus.ERROR

    def test_the_key_never_leaks_through_an_http_error(self) -> None:
        transport = FakeTransport(
            [HttpResponse(401, f"bad key {KEY}".encode("utf-8"))]
        )

        decision = make_judge(transport).judge(make_conflict())

        assert KEY not in decision.rationale

    def test_the_key_never_leaks_through_a_transport_failure(self) -> None:
        transport = FakeTransport(
            [HttpTransportError(f"connect failed for {KEY}")]
        )

        decision = make_judge(transport).judge(make_conflict())

        assert KEY not in decision.rationale

    def test_the_key_never_leaks_through_a_malformed_answer(self) -> None:
        transport = FakeTransport(
            [response(body(envelope(f"not json {KEY}")))]
        )

        decision = make_judge(transport).judge(make_conflict())

        assert KEY not in decision.rationale

    def test_the_decision_does_not_carry_the_key(self) -> None:
        decision = make_judge().judge(make_conflict())

        assert KEY not in json.dumps(decision.to_dict())

    def test_the_key_is_not_in_the_request_body(self) -> None:
        transport = FakeTransport()

        make_judge(transport).judge(make_conflict())

        assert KEY not in transport.bodies[0].decode("utf-8")


class TestCostTelemetry:
    """Cost is a secondary side effect - it can never destroy a judgment."""

    def test_a_real_answer_records_one_cost_entry(self) -> None:
        sink = RecordingCostSink()

        make_judge(FakeTransport(), cost_sink=sink, project="proj").judge(
            make_conflict()
        )

        assert len(sink.records) == 1
        record = sink.records[0]
        assert record.provider == "openai"
        assert record.model == DEFAULT_OPENAI_JUDGE_MODEL
        assert record.input_tokens == 120
        assert record.output_tokens == 30
        assert record.project == "proj"
        assert record.step_no == 16
        # the provider-native response id is the stable event identity
        assert record.event_id == "openai:chatcmpl-test"
        # no price injected: a documented unknown, never a guess
        assert record.cost_usd == 0.0
        assert record.pricing_known is False

    def test_a_response_without_a_provider_id_records_nothing(self) -> None:
        """No stable provider identity -> no event, but the judgment survives."""
        reported: list[BaseException] = []
        sink = RecordingCostSink()
        payload = envelope(json.dumps(contract()))
        payload.pop("id")
        judge = make_judge(
            FakeTransport([response(payload)]),
            cost_sink=sink,
            on_telemetry_error=reported.append,
        )

        decision = judge.judge(make_conflict())

        assert decision.status is DecisionStatus.ACCEPTED
        assert sink.records == []
        assert isinstance(
            judge.last_telemetry_error, CostIdentityUnavailableError
        )
        assert reported == [judge.last_telemetry_error]

    def test_pricing_is_injected_and_never_hardcoded(self) -> None:
        sink = RecordingCostSink()

        make_judge(
            FakeTransport(),
            cost_sink=sink,
            pricing={DEFAULT_OPENAI_JUDGE_MODEL: ModelPrice(1.0, 2.0)},
        ).judge(make_conflict())

        # 120 in / 30 out at 1.0 and 2.0 USD per 1k tokens
        assert sink.records[0].cost_usd == 0.18
        # a configured price means the figure is a known estimate
        assert sink.records[0].pricing_known is True

    def test_a_failing_cost_sink_never_destroys_a_judgment(self) -> None:
        sink = RecordingCostSink(error=RuntimeError("cost store down"))
        judge = make_judge(FakeTransport(), cost_sink=sink)

        decision = judge.judge(make_conflict())

        assert decision.status is DecisionStatus.ACCEPTED
        assert sink.records == []
        assert isinstance(judge.last_telemetry_error, RuntimeError)

    def test_the_telemetry_hook_is_optional_and_never_fatal(self) -> None:
        seen: list = []
        judge = make_judge(
            FakeTransport(),
            cost_sink=RecordingCostSink(error=RuntimeError("down")),
            on_telemetry_error=seen.append,
        )

        assert judge.judge(make_conflict()).status is DecisionStatus.ACCEPTED
        assert len(seen) == 1

    def test_a_failed_attempt_produces_no_record(self) -> None:
        sink = RecordingCostSink()
        transport = FakeTransport([HttpResponse(503, b"boom"), answer()])

        make_judge(transport, cost_sink=sink).judge(make_conflict())

        assert len(sink.records) == 1

    def test_a_failed_judging_call_reports_no_telemetry_error(self) -> None:
        judge = make_judge(FakeTransport([HttpResponse(500, b"boom")]))

        assert judge.judge(make_conflict()).status is DecisionStatus.ERROR
        assert judge.last_telemetry_error is None

    def test_the_sink_is_validated_at_construction(self) -> None:
        with pytest.raises(ValueError, match="cost_sink"):
            OpenAIJudgeAdapter(api_key=KEY, cost_sink=object())

    def test_the_sink_may_be_a_full_cost_port(self) -> None:
        sink = RecordingCostSink()

        assert isinstance(sink, CostPort)
        make_judge(cost_sink=sink).judge(make_conflict())
        assert len(sink.records) == 1

    def test_no_sink_means_no_cost_work_at_all(self) -> None:
        judge = make_judge()

        assert judge.judge(make_conflict()).status is DecisionStatus.ACCEPTED
        assert judge.last_telemetry_error is None


class TestModelResolution:
    """The model is configuration, never architecture truth."""

    def test_the_explicit_constructor_model_wins(self, monkeypatch) -> None:
        monkeypatch.setenv(OPENAI_JUDGE_MODEL_ENV_VAR, "from-environment")

        assert make_judge(model="from-argument").model == "from-argument"

    def test_the_environment_names_the_model(self, monkeypatch) -> None:
        monkeypatch.setenv(OPENAI_JUDGE_MODEL_ENV_VAR, "judge-from-env")

        assert make_judge().model == "judge-from-env"

    def test_the_documented_default_is_the_last_resort(self, monkeypatch) -> None:
        monkeypatch.delenv(OPENAI_JUDGE_MODEL_ENV_VAR, raising=False)

        judge = make_judge()

        assert judge.model == DEFAULT_OPENAI_JUDGE_MODEL
        assert DEFAULT_OPENAI_JUDGE_MODEL == "gpt-5.6"

    def test_the_resolution_order_is_documented(self, monkeypatch) -> None:
        monkeypatch.delenv(OPENAI_JUDGE_MODEL_ENV_VAR, raising=False)
        assert resolve_openai_judge_model() == DEFAULT_OPENAI_JUDGE_MODEL

        monkeypatch.setenv(OPENAI_JUDGE_MODEL_ENV_VAR, "from-environment")
        assert resolve_openai_judge_model() == "from-environment"
        assert resolve_openai_judge_model("from-argument") == "from-argument"
        assert resolve_openai_judge_model("   ") == "from-environment"

    def test_a_blank_argument_falls_through_to_the_default(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv(OPENAI_JUDGE_MODEL_ENV_VAR, raising=False)

        assert make_judge(model="   ").model == DEFAULT_OPENAI_JUDGE_MODEL

    def test_changing_the_model_needs_no_architecture_change(self) -> None:
        first = make_judge(model="a-judge-model").judge(make_conflict())
        second = make_judge(model="another-judge-model").judge(make_conflict())

        assert first.status is second.status
        assert first.decision == second.decision
        assert first.id == second.id  # the model is not part of the identity

    def test_no_live_model_is_required_to_run(self) -> None:
        judge = make_judge(model="a-model-that-may-not-exist")

        assert judge.judge(make_conflict()).status is DecisionStatus.ACCEPTED


def _src_root() -> Path:
    return Path(__file__).resolve().parents[1] / "src" / "architecture_assistant"


class TestLayerBoundary:
    """The adapter is infrastructure - it never reaches outwards."""

    def test_the_adapter_lives_in_infrastructure(self) -> None:
        source = scan_directory(_src_root())
        targets = source.imports_of(
            f"{ROOT_PACKAGE}.infrastructure.openai_judge"
        )
        layers = {layer_of(target) for target in targets}

        assert "infrastructure" in layers
        assert "domain" in layers
        assert "ports" in layers

    def test_the_adapter_never_imports_outwards(self) -> None:
        source = scan_directory(_src_root())
        targets = source.imports_of(
            f"{ROOT_PACKAGE}.infrastructure.openai_judge"
        )

        for target in targets:
            assert layer_of(target) not in ("architecture", "composition")

    def test_the_whole_tree_still_satisfies_the_current_baseline(self) -> None:
        source = scan_directory(_src_root())
        result = ArchitectureValidator().validate(source)

        assert result.is_compliant is True
        assert result.violations == ()
        assert result.baseline_version == ARCHITECTURE_CURRENT.version

    def test_no_api_key_is_committed_anywhere(self) -> None:
        module = _src_root() / "infrastructure" / "openai_judge.py"
        text = module.read_text(encoding="utf-8")

        for pattern in ("sk-", "Bearer sk", "OPENAI_API_KEY ="):
            assert pattern not in text, pattern
        assert "os.environ.get(OPENAI_API_KEY_ENV_VAR" in text

    def test_no_third_party_client_is_used(self) -> None:
        text = (_src_root() / "infrastructure" / "openai_judge.py").read_text(
            encoding="utf-8"
        )

        assert "import requests" not in text
        assert "import openai" not in text

    def test_no_voting_concept_exists_in_the_judge(self) -> None:
        import architecture_assistant.infrastructure.openai_judge as module

        names = {name.lower() for name in dir(module)}
        for forbidden in ("majority", "rank", "vote", "ballot"):
            assert not [name for name in names if forbidden in name]
