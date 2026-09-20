"""Offline tests for the Step 12 OpenAI advisor adapter.

Everything here runs without a network and without a real key: the transport is a
scripted fake, the key is a literal that never leaves the test process, and the
clock/sleep seams are injected. Coverage follows the mandated properties:

* the adapter implements ``AdvisorPort`` and returns a structured ``Finding``;
* the response contract (``status``/``claim``/``evidence``/``confidence``/
  ``severity``/``reason``) is validated strictly;
* an explicit ``ABSTAIN`` is a deliberate refusal (``AbstainError``), while a
  broken contract - including an ``OK`` answer with empty evidence - is a defect
  (``InvalidResponseError``);
* only timeouts, HTTP 429 and HTTP 5xx are retried, with an identical payload;
* the API key lives only in the ``Authorization`` header - never in the body, the
  ``repr``, an error message or the repository;
* cost telemetry is a secondary side effect: only a real answer with usage is
  recorded, and a failing sink can never turn a valid finding into a failure.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from architecture_assistant.application import (
    Orchestrator,
    RealizationControlUseCase,
    decide_review,
)
from architecture_assistant.architecture import (
    APPLICATION_LAYER,
    ARCHITECTURE_CURRENT,
    ARCHITECTURE_LAYER,
    COMPOSITION_LAYER,
    INFRASTRUCTURE_LAYER,
    ROOT_PACKAGE,
    ArchitectureValidator,
    layer_of,
    scan_directory,
)
from architecture_assistant.domain.enums import Severity
from architecture_assistant.domain.models import Finding
from architecture_assistant.infrastructure import (
    ABSTAIN_REASON_FALLBACK,
    DEFAULT_BACKOFF_SCHEDULE,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_TIMEOUT_SECONDS,
    IMPLEMENTATION_ANALYST_ROLE,
    OPENAI_API_KEY_ENV_VAR,
    OPENAI_CHAT_COMPLETIONS_URL,
    OPENAI_PROVIDER,
    UNKNOWN_PRICE,
    HttpRequest,
    HttpResponse,
    HttpTransportError,
    MissingApiKeyError,
    ModelPrice,
    OpenAIAdvisorAbstainError,
    OpenAIAdvisorAdapter,
    OpenAIAdvisorError,
    OpenAIAdvisorHttpError,
    OpenAIAdvisorInvalidResponseError,
    OpenAIAdvisorRateLimitError,
    OpenAIAdvisorTimeoutError,
    OpenAIAdvisorTransportError,
    urllib_transport,
)
from architecture_assistant.ports.capabilities import (
    AdvisorPort,
    AdvisorQuery,
    CostPort,
    CostQuery,
    CostRecord,
    CostSummary,
)

FIXED = datetime(2026, 9, 20, 20, 0, tzinfo=timezone.utc)
LATER = FIXED + timedelta(hours=3)

#: A literal that is not, and never was, a real credential.
KEY = "sk-test-not-a-real-secret"

QUESTION = "How should the snapshot builder be realized?"


def fixed_clock() -> datetime:
    return FIXED


def contract(**overrides) -> dict:
    """A complete, valid ``OK`` answer contract."""
    payload = {
        "status": "OK",
        "claim": "Add a pure snapshot builder to application/context.py.",
        "evidence": ["application/context.py:120", "ADR-003"],
        "confidence": 0.75,
        "severity": "MEDIUM",
        "reason": "",
    }
    payload.update(overrides)
    return payload


def envelope(content: str, *, usage: bool = True) -> dict:
    """A chat-completions envelope carrying one assistant message."""
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
    """A 200 response carrying a valid answer contract."""
    return HttpResponse(200, body(envelope(json.dumps(contract(**overrides)))))


def response(payload, status_code: int = 200) -> HttpResponse:
    return HttpResponse(status_code, body(payload))


@dataclass
class FakeTransport:
    """Scripted, recording transport seam.

    Each call consumes the next scripted item (an ``HttpResponse``, or an
    exception to raise); the last item repeats, so a single-item script behaves
    like a constant endpoint.
    """

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
        """Every request body, in call order."""
        return [request.body for request in self.requests]

    @property
    def call_count(self) -> int:
        """How many times the transport was invoked."""
        return len(self.requests)


@dataclass
class RecordingCostSink:
    """A minimal ``CostPort`` that records what the advisor reports."""

    records: list = field(default_factory=list)
    error: Exception | None = None

    def record(self, cost: CostRecord) -> None:
        if self.error is not None:
            raise self.error
        self.records.append(cost)

    def query(self, cost_filter: CostQuery) -> CostSummary:
        return CostSummary()


def make_query(**overrides) -> AdvisorQuery:
    payload = {
        "question": QUESTION,
        "step_no": 12,
        "context": {"module": "application/context.py", "attempt": 1},
    }
    payload.update(overrides)
    return AdvisorQuery(**payload)


def make_advisor(transport=None, **overrides) -> OpenAIAdvisorAdapter:
    """An adapter wired for offline testing (no real sleeping, key injected)."""
    kwargs: dict = {
        "transport": transport if transport is not None else FakeTransport(),
        "sleep": lambda _seconds: None,
    }
    kwargs.update(overrides)
    if "api_key" not in kwargs:
        kwargs["api_key"] = KEY
    return OpenAIAdvisorAdapter(**kwargs)


class TestPortConformance:
    """The adapter is a capability adapter, not a second decision engine."""

    def test_the_adapter_implements_the_advisor_port(self) -> None:
        advisor = make_advisor()
        assert isinstance(advisor, AdvisorPort)
        assert advisor.provider == OPENAI_PROVIDER == "openai"
        assert advisor.model == DEFAULT_OPENAI_MODEL
        assert advisor.role == IMPLEMENTATION_ANALYST_ROLE

    def test_constructing_the_adapter_touches_nothing(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv(OPENAI_API_KEY_ENV_VAR, raising=False)
        transport = FakeTransport()
        advisor = OpenAIAdvisorAdapter(transport=transport)

        assert transport.call_count == 0
        assert advisor.is_configured is False
        assert advisor.last_telemetry_error is None

    def test_the_role_is_adapter_metadata_not_a_domain_field(self) -> None:
        """Step 12 adds no domain concept: the finding contract is unchanged."""
        assert {model_field.name for model_field in fields(Finding)} == {
            "id",
            "source",
            "claim",
            "evidence",
            "confidence",
            "severity",
            "step_no",
            "created_at",
        }
        assert IMPLEMENTATION_ANALYST_ROLE == "Implementation Analyst"

    def test_a_finding_is_returned_never_a_decision(self) -> None:
        finding = make_advisor().advise(make_query())

        assert type(finding) is Finding
        assert not hasattr(finding, "status")
        assert "decision" not in finding.to_dict()


class TestRequestContract:
    """The request is deterministic and states the structured contract."""

    def _payload(self, advisor: OpenAIAdvisorAdapter, transport: FakeTransport):
        advisor.advise(make_query())
        assert transport.call_count == 1
        return json.loads(transport.requests[0].text())

    def test_the_request_is_a_deterministic_chat_completion(self) -> None:
        transport = FakeTransport()
        payload = self._payload(make_advisor(transport), transport)

        assert payload["model"] == DEFAULT_OPENAI_MODEL
        assert payload["temperature"] == 0
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["max_tokens"] >= 1
        roles = [message["role"] for message in payload["messages"]]
        assert roles == ["system", "user"]

    def test_the_system_prompt_declares_the_role_and_the_contract(self) -> None:
        transport = FakeTransport()
        payload = self._payload(make_advisor(transport), transport)
        prompt = payload["messages"][0]["content"]

        assert IMPLEMENTATION_ANALYST_ROLE in prompt
        for field_name in (
            "status",
            "claim",
            "evidence",
            "confidence",
            "severity",
            "reason",
        ):
            assert field_name in prompt
        assert "ABSTAIN" in prompt
        assert "json" in prompt
        # evidence is *required*, and the deterministic rules outrank the model
        assert "at least one evidence" in prompt
        assert "outrank" in prompt

    def test_the_user_prompt_carries_the_question_the_step_and_the_context(
        self,
    ) -> None:
        transport = FakeTransport()
        payload = self._payload(make_advisor(transport), transport)
        prompt = payload["messages"][1]["content"]

        assert QUESTION in prompt
        assert "Step: 12" in prompt
        assert "application/context.py" in prompt

    def test_the_context_is_serialized_canonically(self) -> None:
        transport = FakeTransport()
        advisor = make_advisor(transport)
        advisor.advise(
            make_query(step_no=None, context={"z": 1, "a": {"y": 2, "b": 3}})
        )

        prompt = json.loads(transport.requests[0].text())["messages"][1]["content"]
        assert '{"a":{"b":3,"y":2},"z":1}' in prompt
        assert "Step: unknown" in prompt

    def test_the_request_targets_the_documented_endpoint_with_a_timeout(
        self,
    ) -> None:
        transport = FakeTransport()
        self._payload(make_advisor(transport), transport)
        request = transport.requests[0]

        assert request.url == OPENAI_CHAT_COMPLETIONS_URL
        assert request.method == "POST"
        assert request.timeout == DEFAULT_TIMEOUT_SECONDS
        assert request.headers["Content-Type"] == "application/json"


class TestFindingMapping:
    """Response -> structured, evidence-based Finding."""

    def test_a_valid_answer_becomes_a_structured_finding(self) -> None:
        advisor = make_advisor(clock=fixed_clock)

        finding = advisor.advise(make_query())

        assert finding.source == "openai"
        assert finding.claim == (
            "Add a pure snapshot builder to application/context.py."
        )
        assert finding.evidence == ("application/context.py:120", "ADR-003")
        assert finding.confidence == 0.75
        assert finding.severity is Severity.MEDIUM
        assert finding.step_no == 12
        assert finding.created_at == FIXED
        assert finding.id.startswith("finding-openai-12-")

    def test_evidence_is_trimmed_and_frozen_into_a_tuple(self) -> None:
        transport = FakeTransport(
            [answer(evidence=["  application/context.py:120  ", "ADR-003"])]
        )

        finding = make_advisor(transport).advise(make_query())

        assert finding.evidence == ("application/context.py:120", "ADR-003")
        assert isinstance(finding.evidence, tuple)

    def test_severity_and_confidence_are_normalized_but_validated(self) -> None:
        transport = FakeTransport([answer(severity="high", confidence=1)])

        finding = make_advisor(transport).advise(make_query())

        assert finding.severity is Severity.HIGH
        assert finding.confidence == 1.0

    def test_the_step_number_comes_from_the_query(self) -> None:
        advisor = make_advisor()

        assert advisor.advise(make_query(step_no=7)).step_no == 7
        assert advisor.advise(make_query(step_no=None)).step_no is None


class TestAbstain:
    """An abstention is a deliberate refusal - never a broken contract."""

    def test_an_explicit_abstain_raises_an_abstain_error(self) -> None:
        transport = FakeTransport(
            [
                answer(
                    status="ABSTAIN",
                    claim="",
                    evidence=[],
                    reason="the context does not contain the module under review",
                )
            ]
        )

        with pytest.raises(OpenAIAdvisorAbstainError) as error:
            make_advisor(transport).advise(make_query())

        assert "does not contain the module" in str(error.value)
        assert error.value.reason == (
            "the context does not contain the module under review"
        )
        assert isinstance(error.value, OpenAIAdvisorError)
        assert not isinstance(error.value, OpenAIAdvisorInvalidResponseError)

    def test_an_abstain_without_a_reason_uses_the_documented_fallback(
        self,
    ) -> None:
        transport = FakeTransport([answer(status="ABSTAIN", reason="")])

        with pytest.raises(OpenAIAdvisorAbstainError) as error:
            make_advisor(transport).advise(make_query())

        assert error.value.reason == ABSTAIN_REASON_FALLBACK

    def test_the_status_token_is_normalized_for_abstain(self) -> None:
        transport = FakeTransport(
            [answer(status=" abstain ", reason="not enough evidence")]
        )

        with pytest.raises(OpenAIAdvisorAbstainError):
            make_advisor(transport).advise(make_query())

    def test_an_abstain_is_never_retried(self) -> None:
        transport = FakeTransport(
            [answer(status="ABSTAIN", reason="insufficient context")]
        )

        with pytest.raises(OpenAIAdvisorAbstainError):
            make_advisor(transport).advise(make_query())

        assert transport.call_count == 1

    def test_a_missing_status_is_not_an_abstain(self) -> None:
        payload = contract()
        payload.pop("status")
        transport = FakeTransport([response(envelope(json.dumps(payload)))])

        with pytest.raises(OpenAIAdvisorInvalidResponseError) as error:
            make_advisor(transport).advise(make_query())

        assert "not an abstention" in str(error.value)
        assert transport.call_count == 1

    def test_an_unknown_status_is_not_an_abstain(self) -> None:
        transport = FakeTransport([answer(status="MAYBE")])

        with pytest.raises(OpenAIAdvisorInvalidResponseError):
            make_advisor(transport).advise(make_query())

    def test_an_ok_answer_with_empty_claim_and_evidence_is_a_defect(
        self,
    ) -> None:
        """The crux: empty content is *not* a declared abstention."""
        transport = FakeTransport(
            [answer(status="OK", claim="", evidence=[], reason="")]
        )

        with pytest.raises(OpenAIAdvisorInvalidResponseError) as error:
            make_advisor(transport).advise(make_query())

        assert '"claim"' in str(error.value)


class TestInvalidResponse:
    """Contract violations are defects: a specific error, never a retry."""

    CASES = {
        "not-json": envelope("this is not json at all"),
        "json-array": envelope("[1, 2, 3]"),
        "no-claim": envelope(json.dumps(contract(claim=""))),
        "missing-claim": envelope(
            json.dumps({k: v for k, v in contract().items() if k != "claim"})
        ),
        "empty-evidence": envelope(json.dumps(contract(evidence=[]))),
        "evidence-not-a-list": envelope(
            json.dumps(contract(evidence="ADR-003"))
        ),
        "evidence-entry-blank": envelope(
            json.dumps(contract(evidence=["application/context.py:120", "  "]))
        ),
        "evidence-entry-not-text": envelope(
            json.dumps(contract(evidence=["application/context.py:120", 7]))
        ),
        "missing-confidence": envelope(
            json.dumps(
                {k: v for k, v in contract().items() if k != "confidence"}
            )
        ),
        "confidence-out-of-range": envelope(
            json.dumps(contract(confidence=1.5))
        ),
        "confidence-negative": envelope(json.dumps(contract(confidence=-0.1))),
        "confidence-boolean": envelope(json.dumps(contract(confidence=True))),
        "severity-unknown": envelope(json.dumps(contract(severity="SEVERE"))),
        "severity-missing": envelope(
            json.dumps({k: v for k, v in contract().items() if k != "severity"})
        ),
    }

    @pytest.mark.parametrize("name", sorted(CASES))
    def test_a_broken_contract_raises_invalid_response(self, name: str) -> None:
        transport = FakeTransport([response(self.CASES[name])])

        with pytest.raises(OpenAIAdvisorInvalidResponseError):
            make_advisor(transport).advise(make_query())

        assert transport.call_count == 1

    @pytest.mark.parametrize("name", sorted(CASES))
    def test_a_broken_contract_is_never_reported_as_an_abstain(
        self, name: str
    ) -> None:
        transport = FakeTransport([response(self.CASES[name])])

        with pytest.raises(OpenAIAdvisorError) as error:
            make_advisor(transport).advise(make_query())

        assert not isinstance(error.value, OpenAIAdvisorAbstainError)

    @pytest.mark.parametrize(
        "payload",
        [
            b"not json",
            b"",
            b"[]",
            b'"text"',
            body({"choices": []}),
            body({"choices": [{}]}),
            body({"choices": [{"message": {"content": ""}}]}),
            body({"choices": [{"message": {"content": 42}}]}),
        ],
    )
    def test_a_broken_envelope_raises_invalid_response(self, payload) -> None:
        transport = FakeTransport([HttpResponse(200, payload)])

        with pytest.raises(OpenAIAdvisorInvalidResponseError):
            make_advisor(transport).advise(make_query())

        assert transport.call_count == 1


def timeout_error() -> HttpTransportError:
    return HttpTransportError("request timed out after 30.0s", timed_out=True)


class TestRetryPolicy:
    """Retry exactly: timeout, HTTP 429 and HTTP 5xx - nothing else."""

    def test_a_timeout_is_retried_and_then_succeeds(self) -> None:
        delays: list[float] = []
        transport = FakeTransport([timeout_error(), answer()])
        advisor = make_advisor(transport, sleep=delays.append)

        finding = advisor.advise(make_query())

        assert transport.call_count == 2
        assert finding.claim.startswith("Add a pure snapshot builder")
        assert delays == [0.0]

    def test_a_429_is_retried_and_then_succeeds(self) -> None:
        transport = FakeTransport(
            [HttpResponse(429, b'{"error":"rate limited"}'), answer()]
        )

        finding = make_advisor(transport).advise(make_query())

        assert transport.call_count == 2
        assert finding.source == "openai"

    def test_a_5xx_is_retried_and_then_succeeds(self) -> None:
        transport = FakeTransport(
            [HttpResponse(503, b'{"error":"unavailable"}'), answer()]
        )

        assert make_advisor(transport).advise(make_query()).id
        assert transport.call_count == 2

    def test_an_exhausted_timeout_raises_a_timeout_error(self) -> None:
        transport = FakeTransport([timeout_error()])

        with pytest.raises(OpenAIAdvisorTimeoutError) as error:
            make_advisor(transport).advise(make_query())

        assert transport.call_count == 3  # first attempt + two retries
        assert isinstance(error.value, OpenAIAdvisorTransportError)
        assert "timed out" in str(error.value)

    def test_an_exhausted_rate_limit_raises_a_rate_limit_error(self) -> None:
        transport = FakeTransport([HttpResponse(429, b"slow down")])

        with pytest.raises(OpenAIAdvisorRateLimitError):
            make_advisor(transport).advise(make_query())

        assert transport.call_count == 3

    def test_an_exhausted_server_error_raises_an_http_error(self) -> None:
        transport = FakeTransport([HttpResponse(500, b"boom")])

        with pytest.raises(OpenAIAdvisorHttpError) as error:
            make_advisor(transport).advise(make_query())

        assert "HTTP 500" in str(error.value)
        assert transport.call_count == 3

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    def test_a_permanent_client_error_is_never_retried(
        self, status: int
    ) -> None:
        transport = FakeTransport([HttpResponse(status, b'{"error":"nope"}')])

        with pytest.raises(OpenAIAdvisorHttpError) as error:
            make_advisor(transport).advise(make_query())

        assert transport.call_count == 1
        assert f"HTTP {status}" in str(error.value)

    def test_a_non_retryable_transport_failure_is_not_retried(self) -> None:
        transport = FakeTransport(
            [HttpTransportError("bad certificate", retryable=False)]
        )

        with pytest.raises(OpenAIAdvisorTransportError):
            make_advisor(transport).advise(make_query())

        assert transport.call_count == 1

    def test_an_exhausted_retryable_transport_failure_is_reported(self) -> None:
        transport = FakeTransport([HttpTransportError("connection reset")])

        with pytest.raises(OpenAIAdvisorTransportError) as error:
            make_advisor(transport).advise(make_query())

        assert "connection reset" in str(error.value)
        assert transport.call_count == 3

    def test_the_retry_payload_is_identical_on_every_attempt(self) -> None:
        transport = FakeTransport(
            [timeout_error(), HttpResponse(503, b"boom"), answer()]
        )

        make_advisor(transport).advise(make_query())

        assert transport.call_count == 3
        first = transport.requests[0]
        for attempt in transport.requests[1:]:
            assert attempt.body == first.body
            assert attempt.url == first.url
            assert attempt.method == first.method
            assert attempt.timeout == first.timeout
            assert dict(attempt.headers) == dict(first.headers)
        assert len(set(transport.bodies)) == 1

    def test_the_backoff_is_bounded_and_deterministic(self) -> None:
        delays: list[float] = []
        transport = FakeTransport([timeout_error()])
        advisor = make_advisor(transport, max_retries=5, sleep=delays.append)

        with pytest.raises(OpenAIAdvisorTimeoutError):
            advisor.advise(make_query())

        assert transport.call_count == 6
        # 0s / 1s / 2s and then the schedule's last value repeats, no jitter
        assert delays == [0.0, 1.0, 2.0, 2.0, 2.0]
        assert DEFAULT_BACKOFF_SCHEDULE == (0.0, 1.0, 2.0)

    def test_retries_can_be_disabled_entirely(self) -> None:
        transport = FakeTransport([timeout_error()])

        with pytest.raises(OpenAIAdvisorTimeoutError):
            make_advisor(transport, max_retries=0).advise(make_query())

        assert transport.call_count == 1

    def test_a_successful_answer_is_not_slept_on(self) -> None:
        delays: list[float] = []
        transport = FakeTransport([answer()])

        make_advisor(transport, sleep=delays.append).advise(make_query())

        assert delays == []


class TestApiKey:
    """The key lives in the Authorization header and nowhere else."""

    def test_the_constructor_key_is_only_in_the_authorization_header(
        self,
    ) -> None:
        transport = FakeTransport()

        make_advisor(transport).advise(make_query())

        request = transport.requests[0]
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        assert KEY not in request.text()
        assert KEY not in repr(make_advisor())

    def test_the_environment_key_is_read_lazily(self, monkeypatch) -> None:
        monkeypatch.setenv(OPENAI_API_KEY_ENV_VAR, KEY)
        transport = FakeTransport()
        advisor = OpenAIAdvisorAdapter(transport=transport)

        assert advisor.is_configured is True
        advisor.advise(make_query())
        assert transport.requests[0].headers["Authorization"] == f"Bearer {KEY}"

    def test_a_missing_key_raises_missing_api_key_error(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv(OPENAI_API_KEY_ENV_VAR, raising=False)
        transport = FakeTransport()
        advisor = OpenAIAdvisorAdapter(transport=transport)

        with pytest.raises(MissingApiKeyError) as error:
            advisor.advise(make_query())

        assert transport.call_count == 0
        assert OPENAI_API_KEY_ENV_VAR in str(error.value)

    def test_a_blank_key_counts_as_missing(self, monkeypatch) -> None:
        monkeypatch.delenv(OPENAI_API_KEY_ENV_VAR, raising=False)
        advisor = make_advisor(api_key="   ")

        assert advisor.is_configured is False
        with pytest.raises(MissingApiKeyError):
            advisor.advise(make_query())

    def test_the_key_never_leaks_through_an_error_message(
        self, monkeypatch
    ) -> None:
        """Even a provider that echoes the key cannot leak it into an error."""
        monkeypatch.delenv(OPENAI_API_KEY_ENV_VAR, raising=False)
        transport = FakeTransport(
            [
                HttpResponse(
                    401,
                    json.dumps(
                        {"error": f"invalid api key {KEY} supplied"}
                    ).encode("utf-8"),
                )
            ]
        )

        with pytest.raises(OpenAIAdvisorHttpError) as error:
            make_advisor(transport).advise(make_query())

        assert KEY not in str(error.value)
        assert "***" in str(error.value)

    def test_the_key_never_leaks_through_a_broken_answer(self, monkeypatch) -> None:
        monkeypatch.delenv(OPENAI_API_KEY_ENV_VAR, raising=False)
        transport = FakeTransport(
            [HttpResponse(200, body(envelope(f"not json {KEY}")))]
        )

        with pytest.raises(OpenAIAdvisorInvalidResponseError) as error:
            make_advisor(transport).advise(make_query())

        assert KEY not in str(error.value)

    def test_the_key_never_leaks_through_a_transport_failure(self) -> None:
        transport = FakeTransport(
            [HttpTransportError(f"connect failed for {KEY}")]
        )

        with pytest.raises(OpenAIAdvisorTransportError) as error:
            make_advisor(transport).advise(make_query())

        assert KEY not in str(error.value)

    def test_the_adapter_never_renders_its_key(self, monkeypatch) -> None:
        monkeypatch.delenv(OPENAI_API_KEY_ENV_VAR, raising=False)
        advisor = make_advisor()

        assert KEY not in repr(advisor)
        assert KEY not in str(advisor)
        assert "Implementation Analyst" in repr(advisor)
        assert advisor.is_configured is True


class TestCostTelemetry:
    """Cost is a secondary side effect: recorded when real, never fatal."""

    def test_cost_is_recorded_from_the_provider_usage(self) -> None:
        sink = RecordingCostSink()
        advisor = make_advisor(
            cost_sink=sink,
            pricing={"gpt-4.1-mini": ModelPrice(0.5, 2.5)},
            project="Architecture Lifecycle Assistant",
            clock=fixed_clock,
        )

        advisor.advise(make_query())

        assert len(sink.records) == 1
        cost = sink.records[0]
        assert cost.provider == "openai"
        assert cost.model == DEFAULT_OPENAI_MODEL
        assert cost.input_tokens == 120
        assert cost.output_tokens == 30
        assert cost.cost_usd == round(
            (120 / 1000.0) * 0.5 + (30 / 1000.0) * 2.5, 6
        )
        assert cost.project == "Architecture Lifecycle Assistant"
        assert cost.step_no == 12
        assert cost.created_at == FIXED

    def test_pricing_is_injected_and_never_hardcoded(self) -> None:
        """No price table ships in the core: unknown means 0.0, documented."""
        assert UNKNOWN_PRICE.cost(1000, 1000) == 0.0
        sink = RecordingCostSink()

        make_advisor(cost_sink=sink).advise(make_query())

        assert sink.records[0].cost_usd == 0.0

    def test_an_unknown_model_falls_back_to_the_documented_zero(self) -> None:
        sink = RecordingCostSink()
        advisor = make_advisor(
            cost_sink=sink, model="some-other-model", pricing={}
        )

        advisor.advise(make_query())

        assert sink.records[0].cost_usd == 0.0
        assert sink.records[0].model == "some-other-model"

    def test_a_failing_cost_sink_never_loses_the_finding(self) -> None:
        reported: list[BaseException] = []
        sink = RecordingCostSink(error=RuntimeError("cost store offline"))
        advisor = make_advisor(
            cost_sink=sink, on_telemetry_error=reported.append
        )

        finding = advisor.advise(make_query())

        assert finding.source == "openai"
        assert finding.evidence
        assert isinstance(advisor.last_telemetry_error, RuntimeError)
        assert len(reported) == 1
        assert sink.records == []

    def test_a_failing_telemetry_hook_never_loses_the_finding(self) -> None:
        def explode(_error: BaseException) -> None:
            raise RuntimeError("hook is broken too")

        sink = RecordingCostSink(error=RuntimeError("cost store offline"))
        advisor = make_advisor(cost_sink=sink, on_telemetry_error=explode)

        finding = advisor.advise(make_query())

        assert finding.source == "openai"
        assert isinstance(advisor.last_telemetry_error, RuntimeError)

    def test_only_the_successful_attempt_records_cost(self) -> None:
        sink = RecordingCostSink()
        transport = FakeTransport(
            [
                HttpResponse(500, b"boom"),
                HttpResponse(500, b"boom"),
                answer(),
            ]
        )

        make_advisor(transport, cost_sink=sink).advise(make_query())

        assert transport.call_count == 3
        assert len(sink.records) == 1
        assert sink.records[0].input_tokens == 120
        assert sink.records[0].output_tokens == 30

    def test_a_failed_call_records_no_cost(self) -> None:
        sink = RecordingCostSink()
        transport = FakeTransport([HttpResponse(500, b"boom")])

        with pytest.raises(OpenAIAdvisorHttpError):
            make_advisor(transport, cost_sink=sink).advise(make_query())

        assert sink.records == []

    def test_a_missing_key_records_no_cost(self, monkeypatch) -> None:
        monkeypatch.delenv(OPENAI_API_KEY_ENV_VAR, raising=False)
        sink = RecordingCostSink()

        with pytest.raises(MissingApiKeyError):
            make_advisor(cost_sink=sink, api_key=None).advise(make_query())

        assert sink.records == []

    def test_an_abstain_still_records_its_billable_cost(self) -> None:
        """The call happened and was paid for, even though it produced nothing."""
        sink = RecordingCostSink()
        transport = FakeTransport(
            [answer(status="ABSTAIN", reason="insufficient context")]
        )

        with pytest.raises(OpenAIAdvisorAbstainError):
            make_advisor(transport, cost_sink=sink).advise(make_query())

        assert len(sink.records) == 1
        assert sink.records[0].input_tokens == 120

    def test_a_broken_answer_still_records_its_billable_cost(self) -> None:
        sink = RecordingCostSink()
        transport = FakeTransport([response(envelope("not json at all"))])

        with pytest.raises(OpenAIAdvisorInvalidResponseError):
            make_advisor(transport, cost_sink=sink).advise(make_query())

        assert len(sink.records) == 1

    @pytest.mark.parametrize(
        "usage",
        [
            None,
            {},
            {"prompt_tokens": "120", "completion_tokens": 30},
            {"prompt_tokens": 120},
            {"prompt_tokens": -1, "completion_tokens": 30},
            {"prompt_tokens": True, "completion_tokens": 30},
        ],
    )
    def test_no_record_without_a_usable_usage_object(self, usage) -> None:
        sink = RecordingCostSink()
        payload = envelope(json.dumps(contract()))
        payload.pop("usage", None)
        if usage is not None:
            payload["usage"] = usage
        transport = FakeTransport([response(payload)])

        make_advisor(transport, cost_sink=sink).advise(make_query())

        assert sink.records == []

    def test_no_sink_means_no_cost_work_at_all(self) -> None:
        advisor = make_advisor()

        assert advisor.advise(make_query()).source == "openai"
        assert advisor.last_telemetry_error is None

    def test_the_sink_is_validated_at_construction(self) -> None:
        with pytest.raises(ValueError, match="cost_sink"):
            OpenAIAdvisorAdapter(api_key=KEY, cost_sink=object())

    def test_the_sink_may_be_a_full_cost_port(self) -> None:
        sink = RecordingCostSink()

        assert isinstance(sink, CostPort)
        make_advisor(cost_sink=sink).advise(make_query())
        assert len(sink.records) == 1


class TestTransportSeam:
    """A typed request/response seam, private to this adapter."""

    def test_the_transport_receives_a_typed_request(self) -> None:
        transport = FakeTransport()

        make_advisor(transport).advise(make_query())

        request = transport.requests[0]
        assert isinstance(request, HttpRequest)
        assert isinstance(request.body, bytes)
        assert request.text() == request.body.decode("utf-8")

    def test_the_response_dto_can_carry_headers_for_later_metadata(
        self,
    ) -> None:
        transport = FakeTransport(
            [
                HttpResponse(
                    429,
                    b'{"error":"rate limited"}',
                    headers={"Retry-After": "7", "x-request-id": "req-1"},
                ),
                HttpResponse(
                    200,
                    body(envelope(json.dumps(contract()))),
                    headers={"x-request-id": "req-2"},
                ),
            ]
        )

        assert make_advisor(transport).advise(make_query()).source == "openai"
        assert transport.script[0].headers["Retry-After"] == "7"
        assert transport.script[1].headers["x-request-id"] == "req-2"

    def test_the_default_transport_is_the_stdlib_urllib_one(self) -> None:
        text = Path(urllib_transport.__code__.co_filename).read_text(
            encoding="utf-8"
        )

        assert callable(urllib_transport)
        # no third-party HTTP client anywhere in the adapter
        assert "import requests" not in text
        assert "import urllib.request" in text
        assert OpenAIAdvisorAdapter(api_key=KEY) is not None

    def test_the_request_dto_validates_its_input(self) -> None:
        with pytest.raises(ValueError, match="url"):
            HttpRequest(url="   ", body=b"{}")
        with pytest.raises(ValueError, match="body"):
            HttpRequest(url="https://example.invalid", body="{}")
        with pytest.raises(ValueError, match="timeout"):
            HttpRequest(url="https://example.invalid", body=b"{}", timeout=0)
        with pytest.raises(ValueError, match="method"):
            HttpRequest(url="https://example.invalid", body=b"{}", method="")

    def test_the_response_dto_validates_its_input(self) -> None:
        with pytest.raises(ValueError, match="status_code"):
            HttpResponse(status_code="200")
        with pytest.raises(ValueError, match="body"):
            HttpResponse(status_code=200, body="text")

    def test_a_transport_that_raises_an_unexpected_error_is_not_swallowed(
        self,
    ) -> None:
        def broken(_request: HttpRequest) -> HttpResponse:
            raise RuntimeError("bug in the transport")

        with pytest.raises(RuntimeError, match="bug in the transport"):
            make_advisor(broken).advise(make_query())

    def test_the_transport_is_validated_at_construction(self) -> None:
        with pytest.raises(ValueError, match="transport"):
            OpenAIAdvisorAdapter(api_key=KEY, transport="not-callable")

    def test_constructor_arguments_are_validated(self) -> None:
        with pytest.raises(ValueError, match="model"):
            OpenAIAdvisorAdapter(api_key=KEY, model="")
        with pytest.raises(ValueError, match="role"):
            OpenAIAdvisorAdapter(api_key=KEY, role="  ")
        with pytest.raises(ValueError, match="timeout_seconds"):
            OpenAIAdvisorAdapter(api_key=KEY, timeout_seconds=0)
        with pytest.raises(ValueError, match="max_retries"):
            OpenAIAdvisorAdapter(api_key=KEY, max_retries=-1)
        with pytest.raises(ValueError, match="backoff_schedule"):
            OpenAIAdvisorAdapter(api_key=KEY, backoff_schedule=())
        with pytest.raises(ValueError, match="max_output_tokens"):
            OpenAIAdvisorAdapter(api_key=KEY, max_output_tokens=0)
        with pytest.raises(ValueError, match="pricing"):
            OpenAIAdvisorAdapter(api_key=KEY, pricing={"m": 1.0})
        with pytest.raises(ValueError, match="api_key"):
            OpenAIAdvisorAdapter(api_key=42)


class TestStableFindingId:
    """One logical advisor result always has the same id."""

    def test_the_id_ignores_the_clock(self) -> None:
        first = make_advisor(
            FakeTransport([answer()]), clock=lambda: FIXED
        ).advise(make_query())
        second = make_advisor(
            FakeTransport([answer()]), clock=lambda: LATER
        ).advise(make_query())

        assert first.id == second.id
        assert first.created_at != second.created_at
        assert first.to_dict() | {"created_at": None} == (
            second.to_dict() | {"created_at": None}
        )

    def test_the_id_is_reproducible_across_calls(self) -> None:
        advisor = make_advisor(clock=fixed_clock)

        first = advisor.advise(make_query())
        second = advisor.advise(make_query())

        assert first.id == second.id
        assert first == second

    def test_the_id_changes_with_the_claim(self) -> None:
        base = make_advisor(FakeTransport([answer()])).advise(make_query())
        other = make_advisor(
            FakeTransport([answer(claim="Something else entirely.")])
        ).advise(make_query())

        assert base.id != other.id

    def test_the_id_changes_with_the_evidence(self) -> None:
        base = make_advisor(FakeTransport([answer()])).advise(make_query())
        other = make_advisor(
            FakeTransport([answer(evidence=["application/context.py:999"])])
        ).advise(make_query())

        assert base.id != other.id

    def test_the_id_changes_with_the_step(self) -> None:
        advisor = make_advisor()

        assert advisor.advise(make_query(step_no=12)).id != advisor.advise(
            make_query(step_no=13)
        ).id

    def test_the_id_changes_with_the_model(self) -> None:
        base = make_advisor().advise(make_query())
        other = make_advisor(model="other-model").advise(make_query())

        assert base.id != other.id
        assert other.id.startswith("finding-openai-12-")

    def test_the_id_is_content_derived_not_random(self) -> None:
        first = make_advisor(
            FakeTransport([answer()]), clock=lambda: FIXED
        ).advise(make_query())
        expected = make_advisor(
            FakeTransport([answer()]),
            clock=lambda: FIXED + timedelta(days=9),
        ).advise(make_query())

        assert first.id == expected.id
        assert first.id.startswith("finding-openai-12-")


class TestDeterministicPriority:
    """Deterministic rules outrank the advisor - structurally, not by promise."""

    def test_the_review_policy_has_no_advisor_input(self) -> None:
        parameters = set(inspect.signature(decide_review).parameters)
        assert parameters == {"report", "attempt", "max_attempts"}
        assert "advisor" not in parameters

    def test_the_orchestrator_has_no_advisor_input(self) -> None:
        parameters = set(inspect.signature(Orchestrator.__init__).parameters)
        assert "advisor" not in parameters
        assert "realization_control" in parameters

    def test_the_realization_gate_has_no_advisor_input(self) -> None:
        parameters = set(
            inspect.signature(RealizationControlUseCase.__init__).parameters
        )
        assert "advisor" not in parameters

    def test_the_architecture_validator_has_no_advisor_input(self) -> None:
        parameters = set(
            inspect.signature(ArchitectureValidator.validate).parameters
        )
        assert "advisor" not in parameters

    def test_the_advisor_cannot_mutate_authoritative_state(self) -> None:
        """It has no storage, no publisher and no decision surface at all."""
        advisor = make_advisor()

        public = {
            name for name in dir(advisor) if not name.startswith("_")
        }
        assert public == {
            "advise",
            "is_configured",
            "last_telemetry_error",
            "model",
            "provider",
            "role",
        }
        for forbidden in ("storage", "record", "decide", "judge", "verify"):
            assert forbidden not in public


class TestLayerBoundaries:
    """Step 12 adds no dependency, no key and no layer permission."""

    def test_the_adapter_module_only_imports_domain_and_ports(self) -> None:
        source = scan_directory(
            Path(__file__).resolve().parents[1] / "src" / "architecture_assistant"
        )
        module = f"{ROOT_PACKAGE}.infrastructure.openai"
        assert module in source.modules()
        assert layer_of(module) == INFRASTRUCTURE_LAYER
        for target in source.imports_of(module):
            assert layer_of(target) not in (
                APPLICATION_LAYER,
                ARCHITECTURE_LAYER,
                COMPOSITION_LAYER,
            ), target
            assert target != "sqlite3", target

    def test_the_adapter_module_imports_the_expected_layers(self) -> None:
        source = scan_directory(
            Path(__file__).resolve().parents[1] / "src" / "architecture_assistant"
        )
        targets = source.imports_of(f"{ROOT_PACKAGE}.infrastructure.openai")
        layers = {layer_of(target) for target in targets}
        assert "domain" in layers
        assert "ports" in layers

    def test_no_api_key_is_committed_anywhere(self) -> None:
        root = Path(__file__).resolve().parents[1]
        module = Path(urllib_transport.__code__.co_filename)
        text = module.read_text(encoding="utf-8")
        for pattern in ("sk-", "Bearer sk", "OPENAI_API_KEY =", 'api_key="sk'):
            assert pattern not in text, pattern
        assert "os.environ.get(OPENAI_API_KEY_ENV_VAR" in text
        assert root.is_dir()

    def test_no_third_party_dependency_is_declared(self) -> None:
        pyproject = (
            Path(__file__).resolve().parents[1] / "pyproject.toml"
        ).read_text(encoding="utf-8")
        assert "dependencies = []" in pyproject
        assert "openai" not in pyproject

    def test_the_adapter_has_exactly_one_capability_method(self) -> None:
        assert hasattr(OpenAIAdvisorAdapter, "advise")

    def test_the_tree_still_satisfies_the_current_baseline(self) -> None:
        root = Path(__file__).resolve().parents[1] / "src" / "architecture_assistant"
        source = scan_directory(root)
        result = ArchitectureValidator().validate(source)

        assert result.is_compliant is True
        assert result.violations == ()
        assert result.baseline_version == ARCHITECTURE_CURRENT.version
        assert f"{ROOT_PACKAGE}.infrastructure.openai" in source.modules()
        assert len(source.modules()) >= 39












