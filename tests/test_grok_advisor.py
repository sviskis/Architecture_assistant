"""Offline tests for the Step 14 Grok advisor adapter.

Everything here runs without a network and without a real key: the transport is a
scripted fake, the key is a literal that never leaves the test process, and the
clock/sleep seams are injected. The adapter is the *Challenger / Alternative
Framing* perspective ("ARE we solving the right problem? What alternative
approach exists?"), and the coverage follows the mandated properties:

* it implements ``AdvisorPort`` and returns a provider-neutral ``Finding``;
* the response contract (``status``/``claim``/``evidence``/``confidence``/
  ``severity``/``reason``) is validated strictly over the xAI chat-completions
  shape (``choices[0].message.content``);
* an explicit ``ABSTAIN`` is a deliberate refusal (``AbstainError``), while a
  broken contract - including an ``OK`` answer with empty evidence - is a defect
  (``InvalidResponseError``);
* only timeouts, HTTP 429 and HTTP 5xx are retried, with an identical payload;
* the API key travels only in the ``Authorization`` header - never the payload,
  the ``Finding``, a ``CostRecord``, ``repr`` or an error message;
* usage is xAI's ``prompt_tokens``/``completion_tokens``;
* cost telemetry is secondary: a failing sink can never turn a valid finding
  into a failure;
* the model name is configuration - constructor, then environment, then one
  documented adapter default;
* the adapter stays independent: no judge, no vote, no evidence merger.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from architecture_assistant.architecture import (
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
    CHALLENGER_ROLE,
    DEFAULT_BACKOFF_SCHEDULE,
    DEFAULT_GROK_MAX_OUTPUT_TOKENS,
    DEFAULT_GROK_MODEL,
    DEFAULT_TIMEOUT_SECONDS,
    GROK_CHAT_COMPLETIONS_URL,
    GROK_MODEL_ENV_VAR,
    GROK_PROVIDER,
    UNKNOWN_PRICE,
    XAI_API_KEY_ENV_VAR,
    GrokAdvisorAbstainError,
    GrokAdvisorAdapter,
    GrokAdvisorError,
    GrokAdvisorHttpError,
    GrokAdvisorInvalidResponseError,
    GrokAdvisorRateLimitError,
    GrokAdvisorTimeoutError,
    GrokAdvisorTransportError,
    GrokMissingApiKeyError,
    HttpRequest,
    HttpResponse,
    HttpTransportError,
    ModelPrice,
    resolve_grok_model,
)
from architecture_assistant.ports.capabilities import (
    AdvisorPort,
    AdvisorQuery,
    CostIdentityUnavailableError,
    CostPort,
    CostQuery,
    CostRecord,
    CostSummary,
)

FIXED = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)
LATER = FIXED + timedelta(hours=3)

#: A literal that is not, and never was, a real credential.
KEY = "xai-test-not-a-real-secret"

QUESTION = "Are we solving the right problem, or is there another approach?"


def fixed_clock() -> datetime:
    return FIXED


def contract(**overrides) -> dict:
    """A complete, valid ``OK`` alternative-framing contract."""
    payload = {
        "status": "OK",
        "claim": "The problem could be reframed as a versioning concern.",
        "evidence": ["architecture/rules.py:312", "ADR-008"],
        "confidence": 0.6,
        "severity": "MEDIUM",
        "reason": "",
    }
    payload.update(overrides)
    return payload


def envelope(content: str, *, usage: bool = True) -> dict:
    """An xAI chat-completions envelope carrying one assistant message."""
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
    """A 200 response carrying a valid alternative-framing contract."""
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
        "step_no": 14,
        "context": {"module": "composition/root.py", "attempt": 1},
    }
    payload.update(overrides)
    return AdvisorQuery(**payload)


def make_advisor(transport=None, **overrides) -> GrokAdvisorAdapter:
    """An adapter wired for offline testing (no real sleeping, key injected)."""
    kwargs: dict = {
        "transport": transport if transport is not None else FakeTransport(),
        "sleep": lambda _seconds: None,
    }
    kwargs.update(overrides)
    if "api_key" not in kwargs:
        kwargs["api_key"] = KEY
    return GrokAdvisorAdapter(**kwargs)


def timeout_error() -> HttpTransportError:
    return HttpTransportError("request timed out after 30.0s", timed_out=True)


class TestPortConformance:
    """The adapter is a capability adapter, not a second decision engine."""

    def test_the_adapter_implements_the_advisor_port(self) -> None:
        advisor = make_advisor()

        assert isinstance(advisor, AdvisorPort)
        assert advisor.provider == GROK_PROVIDER == "grok"
        assert advisor.model == DEFAULT_GROK_MODEL == "grok-4.6"
        assert advisor.role == CHALLENGER_ROLE
        assert "Challenger" in CHALLENGER_ROLE
        assert "Alternative Framing" in CHALLENGER_ROLE

    def test_a_valid_answer_becomes_a_provider_neutral_finding(self) -> None:
        finding = make_advisor().advise(make_query())

        assert isinstance(finding, Finding)
        assert finding.source == "grok"
        assert finding.claim == (
            "The problem could be reframed as a versioning concern."
        )
        assert finding.evidence == ("architecture/rules.py:312", "ADR-008")
        assert finding.confidence == 0.6
        assert finding.severity is Severity.MEDIUM
        assert finding.step_no == 14

    def test_the_finding_has_exactly_the_domain_fields(self) -> None:
        """No provider-specific field may leak into the domain model."""
        assert {entry.name for entry in fields(Finding)} == {
            "id",
            "source",
            "claim",
            "evidence",
            "confidence",
            "severity",
            "step_no",
            "created_at",
        }

    def test_the_adapter_has_exactly_one_capability_method(self) -> None:
        assert hasattr(GrokAdvisorAdapter, "advise")
        for name in ("judge", "decide", "verify", "merge"):
            assert not hasattr(GrokAdvisorAdapter, name)

    def test_the_adapter_is_not_a_decision_engine(self) -> None:
        """No judge, no vote, no majority, no merger: advisors only explain."""
        advisor = make_advisor()
        for name in (
            "judge",
            "vote",
            "majority",
            "perspectives",
            "decision",
            "merge",
            "merge_evidence",
        ):
            assert not hasattr(advisor, name)

    def test_the_query_type_is_validated(self) -> None:
        with pytest.raises(ValueError, match="AdvisorQuery"):
            make_advisor().advise("not a query")

    def test_the_clock_is_injected(self) -> None:
        advisor = make_advisor(clock=fixed_clock)

        assert advisor.advise(make_query()).created_at == FIXED

    def test_the_step_is_carried_onto_the_finding(self) -> None:
        advisor = make_advisor()

        assert advisor.advise(make_query(step_no=None)).step_no is None
        assert advisor.advise(make_query(step_no=42)).step_no == 42

    def test_the_role_is_metadata_not_a_domain_field(self) -> None:
        advisor = make_advisor(role="Some Other Role")
        finding = advisor.advise(make_query())

        assert finding.source == "grok"
        assert "role" not in {entry.name for entry in fields(Finding)}


class TestModelResolution:
    """A provider model name is configuration, never architecture truth."""

    def test_the_explicit_constructor_model_wins(self, monkeypatch) -> None:
        monkeypatch.setenv(GROK_MODEL_ENV_VAR, "from-environment")

        assert make_advisor(model="from-argument").model == "from-argument"

    def test_the_environment_names_the_model_when_no_argument_is_given(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv(GROK_MODEL_ENV_VAR, "grok-from-env")

        assert make_advisor().model == "grok-from-env"

    def test_the_documented_default_is_the_last_resort(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv(GROK_MODEL_ENV_VAR, raising=False)

        assert make_advisor().model == DEFAULT_GROK_MODEL

    def test_resolution_order_is_argument_then_environment_then_default(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv(GROK_MODEL_ENV_VAR, raising=False)
        assert resolve_grok_model() == DEFAULT_GROK_MODEL

        monkeypatch.setenv(GROK_MODEL_ENV_VAR, "from-environment")
        assert resolve_grok_model() == "from-environment"
        assert resolve_grok_model("from-argument") == "from-argument"
        assert resolve_grok_model("   ") == "from-environment"

    def test_a_blank_model_argument_falls_through_to_the_default(
        self, monkeypatch
    ) -> None:
        """A blank argument is not a model: it is skipped, not an error."""
        monkeypatch.delenv(GROK_MODEL_ENV_VAR, raising=False)
        advisor = GrokAdvisorAdapter(api_key=KEY, model="   ")

        assert advisor.model == DEFAULT_GROK_MODEL

    def test_no_live_model_or_provider_is_required_to_run(
        self, monkeypatch
    ) -> None:
        """An unknown model is a provider-side failure, not a start-up one."""
        monkeypatch.delenv(GROK_MODEL_ENV_VAR, raising=False)
        advisor = make_advisor(model="some-model-that-may-not-exist")

        assert advisor.model == "some-model-that-may-not-exist"
        assert advisor.advise(make_query()).source == "grok"


class TestWireFormat:
    """The xAI chat-completions format - provider-specific, and only here."""

    def test_the_request_targets_the_documented_endpoint_with_a_timeout(
        self,
    ) -> None:
        transport = FakeTransport()

        make_advisor(transport).advise(make_query())

        request = transport.requests[0]
        assert isinstance(request, HttpRequest)
        assert request.url == GROK_CHAT_COMPLETIONS_URL
        assert request.url == "https://api.x.ai/v1/chat/completions"
        assert request.method == "POST"
        assert request.timeout == DEFAULT_TIMEOUT_SECONDS
        assert request.text() == request.body.decode("utf-8")

    def test_the_key_travels_in_the_authorization_header_only(self) -> None:
        transport = FakeTransport()

        make_advisor(transport).advise(make_query())

        headers = transport.requests[0].headers
        assert headers["Authorization"] == f"Bearer {KEY}"
        assert headers["Content-Type"] == "application/json"
        # no Anthropic-style header, and the key is nowhere else
        assert "x-api-key" not in headers
        assert "anthropic-version" not in headers
        assert KEY not in transport.bodies[0].decode("utf-8")

    def test_the_request_is_a_deterministic_chat_completion(self) -> None:
        transport = FakeTransport()

        make_advisor(transport).advise(make_query())

        payload = json.loads(transport.bodies[0])
        assert payload["model"] == DEFAULT_GROK_MODEL
        assert payload["temperature"] == 0
        assert payload["max_tokens"] == DEFAULT_GROK_MAX_OUTPUT_TOKENS
        assert [message["role"] for message in payload["messages"]] == [
            "system",
            "user",
        ]
        # structured json output is required by the contract
        assert payload["response_format"] == {"type": "json_object"}

    def test_the_system_prompt_declares_the_role_and_the_contract(self) -> None:
        transport = FakeTransport()

        make_advisor(transport).advise(make_query())

        system = json.loads(transport.bodies[0])["messages"][0]["content"]
        assert CHALLENGER_ROLE in system
        assert "outrank" in system
        assert "ARE we solving the right problem" in system
        assert '"status":"OK"|"ABSTAIN"' in system
        assert "evidence" in system
        assert "never a competing decision" in system

    def test_the_user_prompt_carries_the_reframing_question_and_the_step(
        self,
    ) -> None:
        transport = FakeTransport()

        make_advisor(transport).advise(make_query())

        content = json.loads(transport.bodies[0])["messages"][1]["content"]
        assert QUESTION in content
        assert "ARE we solving the right problem" in content
        assert "What alternative approach exists" in content
        assert "Step: 14" in content
        assert '"module":"composition/root.py"' in content

    def test_the_context_is_serialized_canonically(self) -> None:
        transport = FakeTransport()
        query = make_query(context={"b": 2, "a": 1})

        make_advisor(transport).advise(query)

        content = json.loads(transport.bodies[0])["messages"][1]["content"]
        assert '"a":1,"b":2' in content

    def test_the_request_body_is_byte_stable_for_one_query(self) -> None:
        first, second = FakeTransport(), FakeTransport()

        make_advisor(first).advise(make_query())
        make_advisor(second).advise(make_query())

        assert first.bodies[0] == second.bodies[0]

    def test_the_answer_comes_from_the_first_choice_message(self) -> None:
        payload = envelope(json.dumps(contract()))
        payload["choices"] = [
            {"message": {"role": "assistant", "content": ""}},
            {"message": {"role": "assistant", "content": "ignored"}},
        ]

        transport = FakeTransport([response(payload)])

        # the first choice has no usable content: that is a defect, not a
        # silent fallback to a later choice
        with pytest.raises(GrokAdvisorInvalidResponseError):
            make_advisor(transport).advise(make_query())


def _without(field_name: str) -> str:
    """A valid contract with one field removed, serialized."""
    payload = contract()
    payload.pop(field_name)
    return json.dumps(payload)


class TestEnvelope:
    """A broken chat-completions envelope is a defect, never an abstention."""

    @pytest.mark.parametrize(
        "payload",
        [
            b"not json",
            b"",
            b"[]",
            b'"text"',
            body({"choices": []}),
            body({"choices": [{}]}),
            body({"choices": [{"message": {}}]}),
            body({"choices": [{"message": {"content": ""}}]}),
            body({"choices": [{"message": {"content": "   "}}]}),
            body({"choices": [{"message": {"content": 42}}]}),
            body({"choices": "not a list"}),
        ],
    )
    def test_a_broken_envelope_raises_invalid_response(self, payload) -> None:
        transport = FakeTransport([HttpResponse(200, payload)])

        with pytest.raises(GrokAdvisorInvalidResponseError):
            make_advisor(transport).advise(make_query())

        assert transport.call_count == 1


class TestAbstention:
    """An explicit refusal is a legitimate outcome, distinct from a defect."""

    def test_an_abstain_raises_the_dedicated_error(self) -> None:
        transport = FakeTransport(
            [
                answer(
                    status="ABSTAIN",
                    claim="",
                    evidence=[],
                    reason="the framing question is out of scope here",
                )
            ]
        )

        with pytest.raises(GrokAdvisorAbstainError) as error:
            make_advisor(transport).advise(make_query())

        assert error.value.reason == "the framing question is out of scope here"
        assert transport.call_count == 1

    def test_an_abstain_without_a_reason_uses_the_documented_fallback(
        self,
    ) -> None:
        transport = FakeTransport([answer(status="ABSTAIN", reason="")])

        with pytest.raises(GrokAdvisorAbstainError) as error:
            make_advisor(transport).advise(make_query())

        assert error.value.reason
        assert "abstain" in str(error.value)

    def test_an_abstain_is_never_retried(self) -> None:
        transport = FakeTransport([answer(status="ABSTAIN", reason="no")])

        with pytest.raises(GrokAdvisorAbstainError):
            make_advisor(transport).advise(make_query())

        assert transport.call_count == 1

    def test_a_missing_status_is_a_defect_not_an_abstention(self) -> None:
        transport = FakeTransport(
            [response(body(envelope(_without("status"))))]
        )

        with pytest.raises(GrokAdvisorInvalidResponseError) as error:
            make_advisor(transport).advise(make_query())

        assert not isinstance(error.value, GrokAdvisorAbstainError)
        assert transport.call_count == 1

    def test_an_unknown_status_is_a_defect(self) -> None:
        transport = FakeTransport([answer(status="MAYBE")])

        with pytest.raises(GrokAdvisorInvalidResponseError) as error:
            make_advisor(transport).advise(make_query())

        assert not isinstance(error.value, GrokAdvisorAbstainError)

    def test_the_abstain_error_is_part_of_the_advisor_hierarchy(self) -> None:
        error = GrokAdvisorAbstainError("  thin context  ")

        assert isinstance(error, GrokAdvisorError)
        assert error.reason == "thin context"

    def test_a_blank_abstain_reason_falls_back(self) -> None:
        error = GrokAdvisorAbstainError("   ")

        assert error.reason
        assert "abstain" in str(error)


#: Every way a model answer can violate the contract. Each must be a *defect*.
INVALID_CONTRACTS: dict[str, str] = {
    "not-json": "definitely not json",
    "claim-missing": _without("claim"),
    "claim-blank": json.dumps(contract(claim="   ")),
    "claim-not-a-string": json.dumps(contract(claim=42)),
    "evidence-missing": _without("evidence"),
    "evidence-empty": json.dumps(contract(evidence=[])),
    "evidence-not-a-list": json.dumps(contract(evidence="architecture/x.py:1")),
    "evidence-blank-entry": json.dumps(contract(evidence=["a:1", "   "])),
    "evidence-not-strings": json.dumps(contract(evidence=[1, 2])),
    "confidence-missing": _without("confidence"),
    "confidence-out-of-range": json.dumps(contract(confidence=1.5)),
    "confidence-negative": json.dumps(contract(confidence=-0.1)),
    "confidence-not-a-number": json.dumps(contract(confidence="0.5")),
    "confidence-boolean": json.dumps(contract(confidence=True)),
    "severity-missing": _without("severity"),
    "severity-unknown": json.dumps(contract(severity="MAYBE")),
}


class TestInvalidResponse:
    """A broken contract is a defect and is never reported as an abstention."""

    @pytest.mark.parametrize("name", sorted(INVALID_CONTRACTS))
    def test_a_broken_contract_raises_invalid_response(self, name) -> None:
        transport = FakeTransport(
            [response(body(envelope(INVALID_CONTRACTS[name])))]
        )

        with pytest.raises(GrokAdvisorInvalidResponseError):
            make_advisor(transport).advise(make_query())

        assert transport.call_count == 1

    @pytest.mark.parametrize("name", sorted(INVALID_CONTRACTS))
    def test_a_broken_contract_is_never_reported_as_an_abstain(
        self, name
    ) -> None:
        transport = FakeTransport(
            [response(body(envelope(INVALID_CONTRACTS[name])))]
        )

        with pytest.raises(GrokAdvisorError) as error:
            make_advisor(transport).advise(make_query())

        assert not isinstance(error.value, GrokAdvisorAbstainError)

    def test_a_contract_that_is_a_json_array_is_a_defect(self) -> None:
        transport = FakeTransport(
            [response(body(envelope(json.dumps([1, 2]))))]
        )

        with pytest.raises(GrokAdvisorInvalidResponseError):
            make_advisor(transport).advise(make_query())

    def test_case_insensitive_tokens_are_accepted(self) -> None:
        transport = FakeTransport([answer(status="ok", severity="critical")])

        finding = make_advisor(transport).advise(make_query())

        assert finding.severity is Severity.CRITICAL
        assert finding.source == "grok"


class TestRetryPolicy:
    """Retry exactly: timeout, HTTP 429 and HTTP 5xx - nothing else."""

    def test_a_timeout_is_retried_and_then_succeeds(self) -> None:
        delays: list[float] = []
        transport = FakeTransport([timeout_error(), answer()])
        advisor = make_advisor(transport, sleep=delays.append)

        finding = advisor.advise(make_query())

        assert transport.call_count == 2
        assert finding.source == "grok"
        assert delays == [0.0]

    def test_a_429_is_retried_and_then_succeeds(self) -> None:
        transport = FakeTransport(
            [HttpResponse(429, b'{"error":"rate limited"}'), answer()]
        )

        finding = make_advisor(transport).advise(make_query())

        assert transport.call_count == 2
        assert finding.source == "grok"

    def test_a_5xx_is_retried_and_then_succeeds(self) -> None:
        transport = FakeTransport(
            [HttpResponse(503, b'{"error":"unavailable"}'), answer()]
        )

        assert make_advisor(transport).advise(make_query()).source == "grok"
        assert transport.call_count == 2

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    def test_a_permanent_client_error_is_never_retried(self, status) -> None:
        transport = FakeTransport([HttpResponse(status, b'{"error":"no"}')])

        with pytest.raises(GrokAdvisorHttpError) as error:
            make_advisor(transport).advise(make_query())

        assert not isinstance(error.value, GrokAdvisorRateLimitError)
        assert transport.call_count == 1

    def test_an_exhausted_429_becomes_a_rate_limit_error(self) -> None:
        transport = FakeTransport([HttpResponse(429, b"slow down")])

        with pytest.raises(GrokAdvisorRateLimitError):
            make_advisor(transport).advise(make_query())

        assert transport.call_count == 3  # first attempt + two retries

    def test_an_exhausted_5xx_becomes_an_http_error(self) -> None:
        transport = FakeTransport([HttpResponse(500, b"boom")])

        with pytest.raises(GrokAdvisorHttpError):
            make_advisor(transport).advise(make_query())

        assert transport.call_count == 3

    def test_an_exhausted_timeout_is_reported_as_a_timeout(self) -> None:
        transport = FakeTransport([timeout_error()])

        with pytest.raises(GrokAdvisorTimeoutError) as error:
            make_advisor(transport).advise(make_query())

        assert transport.call_count == 3
        assert isinstance(error.value, GrokAdvisorTransportError)
        assert "timed out" in str(error.value)

    def test_a_non_retryable_transport_failure_is_not_retried(self) -> None:
        transport = FakeTransport(
            [HttpTransportError("bad certificate", retryable=False)]
        )

        with pytest.raises(GrokAdvisorTransportError) as error:
            make_advisor(transport).advise(make_query())

        assert transport.call_count == 1
        assert not isinstance(error.value, GrokAdvisorTimeoutError)

    def test_an_exhausted_retryable_transport_failure_is_reported(self) -> None:
        transport = FakeTransport([HttpTransportError("connection reset")])

        with pytest.raises(GrokAdvisorTransportError):
            make_advisor(transport).advise(make_query())

        assert transport.call_count == 3

    def test_the_retry_payload_is_byte_identical(self) -> None:
        transport = FakeTransport(
            [HttpResponse(503, b"boom"), timeout_error(), answer()]
        )

        make_advisor(transport).advise(make_query())

        assert transport.call_count == 3
        assert len(set(transport.bodies)) == 1
        assert len({request.url for request in transport.requests}) == 1
        assert len({request.timeout for request in transport.requests}) == 1
        assert (
            len(
                {
                    tuple(sorted(request.headers.items()))
                    for request in transport.requests
                }
            )
            == 1
        )

    def test_the_backoff_schedule_is_bounded_and_deterministic(self) -> None:
        delays: list[float] = []
        transport = FakeTransport([HttpResponse(500, b"boom")])
        advisor = make_advisor(transport, max_retries=5, sleep=delays.append)

        with pytest.raises(GrokAdvisorHttpError):
            advisor.advise(make_query())

        assert delays == [0.0, 1.0, 2.0, 2.0, 2.0]
        assert DEFAULT_BACKOFF_SCHEDULE == (0.0, 1.0, 2.0)

    def test_the_retry_boundary_is_validated(self) -> None:
        with pytest.raises(ValueError, match="max_retries"):
            make_advisor(max_retries=-1)


class TestSecrets:
    """The key lives in the header, nowhere else - and it is redacted."""

    def test_the_environment_key_is_picked_up_lazily(self, monkeypatch) -> None:
        monkeypatch.setenv(XAI_API_KEY_ENV_VAR, "xai-from-env")
        advisor = make_advisor(api_key=None)

        assert advisor.is_configured is True
        assert "xai-from-env" not in repr(advisor)
        assert advisor.advise(make_query()).source == "grok"

    def test_a_missing_key_raises_before_any_transport_call(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv(XAI_API_KEY_ENV_VAR, raising=False)
        transport = FakeTransport()

        with pytest.raises(GrokMissingApiKeyError):
            make_advisor(transport, api_key=None).advise(make_query())

        assert transport.call_count == 0

    def test_a_blank_key_is_not_a_configured_key(self, monkeypatch) -> None:
        monkeypatch.delenv(XAI_API_KEY_ENV_VAR, raising=False)
        advisor = make_advisor(api_key="   ")

        assert advisor.is_configured is False
        with pytest.raises(GrokMissingApiKeyError):
            advisor.advise(make_query())

    def test_the_key_is_not_rendered(self) -> None:
        advisor = make_advisor()

        assert KEY not in repr(advisor)
        assert KEY not in str(advisor)
        assert "configured=True" in repr(advisor)

    def test_the_key_never_leaks_through_an_http_error(self) -> None:
        transport = FakeTransport(
            [HttpResponse(401, f"bad key {KEY}".encode("utf-8"))]
        )

        with pytest.raises(GrokAdvisorHttpError) as error:
            make_advisor(transport).advise(make_query())

        assert KEY not in str(error.value)
        assert "***" in str(error.value)

    def test_the_key_never_leaks_through_a_transport_failure(self) -> None:
        transport = FakeTransport(
            [HttpTransportError(f"connect failed for {KEY}")]
        )

        with pytest.raises(GrokAdvisorTransportError) as error:
            make_advisor(transport).advise(make_query())

        assert KEY not in str(error.value)

    def test_the_key_never_leaks_through_an_invalid_response(self) -> None:
        transport = FakeTransport(
            [HttpResponse(200, f"not json {KEY}".encode("utf-8"))]
        )

        with pytest.raises(GrokAdvisorInvalidResponseError) as error:
            make_advisor(transport).advise(make_query())

        assert KEY not in str(error.value)

    def test_the_key_is_not_in_the_request_body(self) -> None:
        transport = FakeTransport()

        make_advisor(transport).advise(make_query())

        body_text = transport.bodies[0].decode("utf-8")
        assert KEY not in body_text
        assert "Authorization" not in body_text

    def test_the_key_is_not_on_a_finding_or_a_cost_record(self) -> None:
        sink = RecordingCostSink()
        transport = FakeTransport()

        finding = make_advisor(transport, cost_sink=sink).advise(make_query())

        assert KEY not in json.dumps(finding.to_dict())
        assert KEY not in json.dumps(sink.records[0].to_dict())
        assert finding.to_dict()["source"] == "grok"


class TestCostTelemetry:
    """Cost is a secondary side effect - it can never fail a valid finding."""

    def test_a_real_answer_records_one_neutral_cost_entry(self) -> None:
        sink = RecordingCostSink()
        transport = FakeTransport()

        make_advisor(transport, cost_sink=sink, project="proj").advise(
            make_query()
        )

        assert len(sink.records) == 1
        record = sink.records[0]
        assert record.provider == "grok"
        assert record.model == DEFAULT_GROK_MODEL
        assert record.input_tokens == 120
        assert record.output_tokens == 30
        assert record.project == "proj"
        assert record.step_no == 14
        # the provider-native response id is the stable event identity
        assert record.event_id == "grok:chatcmpl-test"
        # no price injected: a documented unknown, never a guess
        assert record.cost_usd == 0.0
        assert record.pricing_known is False

    def test_a_response_without_a_provider_id_records_nothing(self) -> None:
        """No stable provider identity -> no event, but the finding survives."""
        reported: list[BaseException] = []
        sink = RecordingCostSink()
        payload = envelope(json.dumps(contract()))
        payload.pop("id")
        advisor = make_advisor(
            FakeTransport([response(payload)]),
            cost_sink=sink,
            on_telemetry_error=reported.append,
        )

        finding = advisor.advise(make_query())

        assert finding.source == "grok"
        assert sink.records == []
        assert isinstance(
            advisor.last_telemetry_error, CostIdentityUnavailableError
        )
        assert reported == [advisor.last_telemetry_error]

    def test_xai_usage_fields_are_mapped(self) -> None:
        sink = RecordingCostSink()
        payload = envelope(json.dumps(contract()))
        payload["usage"] = {"prompt_tokens": 7, "completion_tokens": 9}

        make_advisor(
            FakeTransport([response(payload)]),
            cost_sink=sink,
            pricing={DEFAULT_GROK_MODEL: ModelPrice(1.0, 2.0)},
        ).advise(make_query())

        assert sink.records[0].input_tokens == 7
        assert sink.records[0].output_tokens == 9
        assert sink.records[0].cost_usd == 0.025
        # a configured price means the figure is a known estimate
        assert sink.records[0].pricing_known is True

    def test_anthropic_usage_field_names_are_not_understood(self) -> None:
        """xAI parsing is provider-specific, not the Anthropic shape."""
        sink = RecordingCostSink()
        payload = envelope(json.dumps(contract()))
        payload["usage"] = {"input_tokens": 120, "output_tokens": 30}

        make_advisor(
            FakeTransport([response(payload)]), cost_sink=sink
        ).advise(make_query())

        assert sink.records == []

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

        make_advisor(
            FakeTransport([response(payload)]), cost_sink=sink
        ).advise(make_query())

        assert sink.records == []

    def test_a_failing_cost_sink_never_fails_a_valid_finding(self) -> None:
        sink = RecordingCostSink(error=RuntimeError("cost store down"))
        transport = FakeTransport()

        finding = make_advisor(transport, cost_sink=sink).advise(make_query())

        assert finding.source == "grok"
        assert sink.records == []

    def test_a_failing_cost_sink_is_surfaced_as_telemetry_error(self) -> None:
        sink = RecordingCostSink(error=RuntimeError("cost store down"))
        advisor = make_advisor(cost_sink=sink)

        assert advisor.advise(make_query()).source == "grok"
        assert isinstance(advisor.last_telemetry_error, RuntimeError)

    def test_the_telemetry_hook_is_optional_and_never_fatal(self) -> None:
        seen: list = []
        advisor = make_advisor(
            cost_sink=RecordingCostSink(error=RuntimeError("down")),
            on_telemetry_error=seen.append,
        )

        assert advisor.advise(make_query()).source == "grok"
        assert len(seen) == 1

        def exploding_hook(error) -> None:
            raise RuntimeError("hook broke")

        advisor2 = make_advisor(
            cost_sink=RecordingCostSink(error=RuntimeError("down")),
            on_telemetry_error=exploding_hook,
        )

        assert advisor2.advise(make_query()).source == "grok"

    def test_no_sink_means_no_cost_work_at_all(self) -> None:
        advisor = make_advisor()

        assert advisor.advise(make_query()).source == "grok"
        assert advisor.last_telemetry_error is None

    def test_the_sink_is_validated_at_construction(self) -> None:
        with pytest.raises(ValueError, match="cost_sink"):
            GrokAdvisorAdapter(api_key=KEY, cost_sink=object())

    def test_the_sink_may_be_a_full_cost_port(self) -> None:
        sink = RecordingCostSink()

        assert isinstance(sink, CostPort)
        make_advisor(cost_sink=sink).advise(make_query())
        assert len(sink.records) == 1

    def test_a_broken_contract_still_records_the_paid_call(self) -> None:
        """The call was paid for, so a contract violation must not lose cost."""
        sink = RecordingCostSink()
        transport = FakeTransport(
            [response(body(envelope("not json at all")))]
        )

        with pytest.raises(GrokAdvisorInvalidResponseError):
            make_advisor(transport, cost_sink=sink).advise(make_query())

        assert len(sink.records) == 1

    def test_a_failed_attempt_produces_no_record(self) -> None:
        sink = RecordingCostSink()
        transport = FakeTransport([HttpResponse(503, b"boom"), answer()])

        make_advisor(transport, cost_sink=sink).advise(make_query())

        assert len(sink.records) == 1  # only the successful attempt

    def test_an_unknown_price_is_documented_not_guessed(self) -> None:
        assert UNKNOWN_PRICE.cost(120, 30) == 0.0


class TestDeterminism:
    """The same claim on the same step is the same finding - the clock is not."""

    def test_the_same_answer_yields_the_same_finding_id(self) -> None:
        first = make_advisor(FakeTransport(), clock=fixed_clock)
        second = make_advisor(FakeTransport(), clock=lambda: LATER)

        assert first.advise(make_query()).id == second.advise(make_query()).id

    def test_the_same_question_twice_is_idempotent(self) -> None:
        advisor = make_advisor()

        assert (
            advisor.advise(make_query()).id == advisor.advise(make_query()).id
        )

    def test_a_different_claim_yields_a_different_finding_id(self) -> None:
        first = make_advisor(FakeTransport())
        second = make_advisor(
            FakeTransport([answer(claim="A completely different framing.")])
        )

        assert first.advise(make_query()).id != second.advise(make_query()).id

    def test_confidence_and_severity_do_not_change_the_id(self) -> None:
        first = make_advisor(FakeTransport())
        second = make_advisor(
            FakeTransport([answer(confidence=0.1, severity="LOW")])
        )

        assert first.advise(make_query()).id == second.advise(make_query()).id

    def test_the_finding_id_names_the_provider_and_the_step(self) -> None:
        finding = make_advisor().advise(make_query())

        assert finding.id.startswith("finding-grok-14-")

    def test_a_step_less_query_still_produces_a_stable_id(self) -> None:
        finding = make_advisor().advise(make_query(step_no=None))

        assert finding.id.startswith("finding-grok-0-")

    def test_the_finding_id_is_provider_scoped(self) -> None:
        """The same claim under another provider name is another finding."""
        from architecture_assistant.infrastructure._http import _finding_id

        grok = _finding_id("grok", DEFAULT_GROK_MODEL, 14, "claim", ("e",))
        other = _finding_id("openai", DEFAULT_GROK_MODEL, 14, "claim", ("e",))

        assert grok != other
        assert grok.startswith("finding-grok-14-")
        assert other.startswith("finding-openai-14-")


def _src_root() -> Path:
    return Path(__file__).resolve().parents[1] / "src" / "architecture_assistant"


class TestArchitectureBoundaries:
    """The shared layer stays narrow; deterministic rules stay in charge."""

    def test_the_grok_module_imports_the_expected_layers(self) -> None:
        source = scan_directory(_src_root())
        targets = source.imports_of(
            f"{ROOT_PACKAGE}.{INFRASTRUCTURE_LAYER}.grok"
        )
        layers = {layer_of(target) for target in targets}

        assert "domain" in layers
        assert "ports" in layers

    def test_the_shared_module_was_not_extended_for_grok(self) -> None:
        """Step 14 reuses ``_http.py``; it must not have grown for one provider."""
        import architecture_assistant.infrastructure._http as shared

        assert set(shared.__all__) == {
            "DEFAULT_TIMEOUT_SECONDS",
            "DEFAULT_MAX_RETRIES",
            "DEFAULT_BACKOFF_SCHEDULE",
            "HTTP_TOO_MANY_REQUESTS",
            "HTTP_SERVER_ERROR",
            "is_retryable_status",
            "UNKNOWN_PRICE",
            "ModelPrice",
            "HttpRequest",
            "HttpResponse",
            "HttpTransportError",
            "urllib_transport",
        }
        # no orchestration, no prompts, no contract parsing, no auth, no voting
        for name in (
            "advise",
            "GrokAdvisorAdapter",
            "ClaudeAdvisorAdapter",
            "OpenAIAdvisorAdapter",
            "AdvisorQuery",
            "api_key",
            "resolve_grok_model",
        ):
            assert not hasattr(shared, name)

    def test_the_shared_module_stays_private(self) -> None:
        import architecture_assistant.infrastructure as package

        assert "_http" not in package.__all__
        for name in package.__all__:
            assert not name.startswith("_"), name

    def test_the_transport_primitives_are_shared_not_duplicated(self) -> None:
        """All three advisors must use the *same* transport objects."""
        from architecture_assistant.infrastructure import claude, grok, openai

        for module in (claude, grok):
            assert module.HttpRequest is openai.HttpRequest
            assert module.HttpResponse is openai.HttpResponse
            assert module.HttpTransportError is openai.HttpTransportError
            assert module.urllib_transport is openai.urllib_transport
            assert module.ModelPrice is openai.ModelPrice
            assert module.UNKNOWN_PRICE is openai.UNKNOWN_PRICE
            assert module.is_retryable_status is openai.is_retryable_status

    def test_there_is_no_generic_base_advisor_adapter(self) -> None:
        """Three providers alone are not a reason for a framework."""
        from architecture_assistant.infrastructure import claude, openai

        assert GrokAdvisorAdapter.__bases__ == (object,)
        assert claude.ClaudeAdvisorAdapter.__bases__ == (object,)
        assert openai.OpenAIAdvisorAdapter.__bases__ == (object,)
        assert not issubclass(GrokAdvisorAdapter, openai.OpenAIAdvisorAdapter)
        for module in (claude, openai):
            assert not [name for name in dir(module) if "BaseAdvisor" in name]

    def test_the_three_providers_stay_distinct(self) -> None:
        from architecture_assistant.infrastructure import claude, openai

        assert GROK_PROVIDER == "grok"
        assert len(
            {GROK_PROVIDER, openai.OPENAI_PROVIDER, claude.CLAUDE_PROVIDER}
        ) == 3
        assert len(
            {
                CHALLENGER_ROLE,
                openai.IMPLEMENTATION_ANALYST_ROLE,
                claude.CRITICAL_REVIEWER_ROLE,
            }
        ) == 3
        assert len(
            {
                XAI_API_KEY_ENV_VAR,
                openai.OPENAI_API_KEY_ENV_VAR,
                claude.CLAUDE_API_KEY_ENV_VAR,
            }
        ) == 3
        assert len(
            {
                GROK_CHAT_COMPLETIONS_URL,
                openai.OPENAI_CHAT_COMPLETIONS_URL,
                claude.CLAUDE_MESSAGES_URL,
            }
        ) == 3

    def test_no_infrastructure_module_imports_outwards(self) -> None:
        source = scan_directory(_src_root())
        checked = 0
        for module in source.modules():
            if layer_of(module) != INFRASTRUCTURE_LAYER:
                continue
            checked += 1
            for target in source.imports_of(module):
                assert layer_of(target) not in (
                    ARCHITECTURE_LAYER,
                    COMPOSITION_LAYER,
                ), module
        assert checked > 0

    def test_domain_ports_and_application_do_not_know_the_shared_module(
        self,
    ) -> None:
        source = scan_directory(_src_root())
        checked = 0
        for module in source.modules():
            if layer_of(module) not in ("domain", "ports", "application"):
                continue
            checked += 1
            for target in source.imports_of(module):
                assert "_http" not in target, module
        assert checked > 0

    def test_the_whole_tree_still_satisfies_the_current_baseline(self) -> None:
        source = scan_directory(_src_root())
        result = ArchitectureValidator().validate(source)

        assert result.is_compliant is True
        assert result.violations == ()
        assert result.baseline_version == ARCHITECTURE_CURRENT.version

    def test_no_api_key_is_committed_anywhere(self) -> None:
        root = Path(__file__).resolve().parents[1]
        module = Path(GrokAdvisorAdapter.__init__.__code__.co_filename)
        text = module.read_text(encoding="utf-8")

        for pattern in ("xai-", "Bearer xai", "XAI_API_KEY ="):
            assert pattern not in text, pattern
        assert "os.environ.get(XAI_API_KEY_ENV_VAR" in text
        assert root.is_dir()

    def test_no_third_party_client_is_used(self) -> None:
        module = Path(GrokAdvisorAdapter.__init__.__code__.co_filename)
        text = module.read_text(encoding="utf-8")

        assert "import requests" not in text
        assert "import openai" not in text
        assert "import xai" not in text
