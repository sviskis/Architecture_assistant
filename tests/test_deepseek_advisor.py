"""Tests for the DeepSeek advisor adapter (an added ``AdvisorPort``).

DeepSeek is a *fourth independent opinion*, never an authority: the coverage here
is the same contract the other provider adapters are held to.

* it implements ``AdvisorPort`` and returns a provider-neutral ``Finding``;
* the response contract (``status``/``claim``/``evidence``/``confidence``/
  ``severity``/``reason``) is validated strictly over the DeepSeek
  chat-completions shape, and an ``ABSTAIN`` is never reported as a broken
  contract;
* it fails closed: a missing key, an exhausted transport retry, a permanent HTTP
  status and a malformed answer each raise their own error;
* the API key is never rendered - not in ``repr``, not in the request body, not
  in an error message - and only ever travels in the ``Authorization`` header;
* cost telemetry is a best-effort side effect keyed by the provider response id;
* its Test Connection probe maps statuses onto the documented five verdicts.

Nothing here needs a network or a real key: the transport, the clock and the
sleep seam are injected.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from architecture_assistant.domain.enums import Severity
from architecture_assistant.infrastructure import (
    DEEPSEEK_API_KEY_ENV_VAR,
    DEEPSEEK_CHAT_COMPLETIONS_URL,
    DEEPSEEK_MODEL_ENV_VAR,
    DEEPSEEK_PROVIDER,
    DEFAULT_DEEPSEEK_MODEL,
    INDEPENDENT_REVIEWER_ROLE,
    DeepSeekAdvisorAbstainError,
    DeepSeekAdvisorAdapter,
    DeepSeekAdvisorError,
    DeepSeekAdvisorHttpError,
    DeepSeekAdvisorInvalidResponseError,
    DeepSeekAdvisorRateLimitError,
    DeepSeekAdvisorTimeoutError,
    DeepSeekAdvisorTransportError,
    DeepSeekMissingApiKeyError,
    HttpRequest,
    HttpResponse,
    HttpTransportError,
    ModelPrice,
    resolve_deepseek_model,
)
from architecture_assistant.ports.capabilities import (
    AdvisorPort,
    AdvisorQuery,
    ConnectionStatus,
    CostIdentityUnavailableError,
    CostPort,
    CostQuery,
    CostRecord,
    CostSummary,
)

FIXED = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)
LATER = FIXED + timedelta(hours=3)

#: A literal that is not, and never was, a real credential.
KEY = "deepseek-test-not-a-real-secret"

QUESTION = "What does the evidence actually support, on its own?"


def fixed_clock() -> datetime:
    return FIXED


def contract(**overrides) -> dict:
    """A complete, valid ``OK`` independent-review contract."""
    payload = {
        "status": "OK",
        "claim": "The evidence supports keeping the composition layer thin.",
        "evidence": ["architecture_assistant/composition/root.py:1", "ADR-008"],
        "confidence": 0.7,
        "severity": "MEDIUM",
        "reason": "",
    }
    payload.update(overrides)
    return payload


def envelope(content: str, *, usage: bool = True, response_id: str = "chatcmpl-ds") -> dict:
    """A DeepSeek chat-completions envelope carrying one assistant message."""
    payload: dict = {
        "id": response_id,
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
    """A 200 response carrying a valid independent-review contract."""
    return HttpResponse(200, body(envelope(json.dumps(contract(**overrides)))))


def response(payload, status_code: int = 200) -> HttpResponse:
    return HttpResponse(status_code, body(payload))


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
    def call_count(self) -> int:
        return len(self.requests)


@dataclass
class RecordingCostSink:
    """A minimal ``CostPort`` that records what the advisor reports."""

    records: list = field(default_factory=list)

    def record(self, cost: CostRecord) -> None:
        self.records.append(cost)

    def query(self, cost_filter: CostQuery) -> CostSummary:
        return CostSummary()


def make_query(**overrides) -> AdvisorQuery:
    payload = {
        "question": QUESTION,
        "step_no": 29,
        "context": {"module": "composition/root.py", "attempt": 1},
    }
    payload.update(overrides)
    return AdvisorQuery(**payload)


def make_advisor(transport=None, **overrides) -> DeepSeekAdvisorAdapter:
    """An adapter wired for offline testing (no real sleeping, key injected)."""
    kwargs: dict = {
        "transport": transport if transport is not None else FakeTransport(),
        "sleep": lambda _seconds: None,
        "clock": fixed_clock,
        "api_key": KEY,
    }
    kwargs.update(overrides)
    return DeepSeekAdvisorAdapter(**kwargs)


class TestPortConformance:
    """It is an independent advisor and nothing more."""

    def test_the_adapter_implements_the_advisor_port(self) -> None:
        advisor = make_advisor()

        assert isinstance(advisor, AdvisorPort)
        assert advisor.provider == DEEPSEEK_PROVIDER == "deepseek"
        assert advisor.model == DEFAULT_DEEPSEEK_MODEL == "deepseek-chat"
        assert advisor.role == INDEPENDENT_REVIEWER_ROLE

    def test_the_endpoint_and_the_environment_variables_are_its_own(self) -> None:
        assert DEEPSEEK_CHAT_COMPLETIONS_URL == (
            "https://api.deepseek.com/chat/completions"
        )
        assert DEEPSEEK_API_KEY_ENV_VAR == "DEEPSEEK_API_KEY"
        assert DEEPSEEK_MODEL_ENV_VAR == "DEEPSEEK_MODEL"

    def test_the_adapter_has_exactly_one_capability_method(self) -> None:
        assert hasattr(DeepSeekAdvisorAdapter, "advise")
        for name in ("judge", "decide", "verify", "merge", "vote"):
            assert not hasattr(DeepSeekAdvisorAdapter, name)

    def test_the_public_surface_is_advise_plus_its_identity(self) -> None:
        advisor = make_advisor()
        public = {name for name in dir(advisor) if not name.startswith("_")}

        assert public == {
            "advise",
            "is_configured",
            "last_telemetry_error",
            "model",
            "provider",
            "role",
        }

    def test_a_valid_answer_becomes_a_provider_neutral_finding(self) -> None:
        finding = make_advisor().advise(make_query())

        assert finding.source == "deepseek"
        assert finding.claim.startswith("The evidence supports")
        assert finding.evidence == (
            "architecture_assistant/composition/root.py:1",
            "ADR-008",
        )
        assert finding.confidence == 0.7
        assert finding.severity is Severity.MEDIUM
        assert finding.step_no == 29
        assert finding.created_at == FIXED
        assert finding.id.startswith("finding-deepseek-29-")

    def test_the_query_type_is_validated(self) -> None:
        with pytest.raises(ValueError, match="AdvisorQuery"):
            make_advisor().advise("not a query")

    def test_the_transport_primitives_are_shared_not_duplicated(self) -> None:
        from architecture_assistant.infrastructure import deepseek, openai

        assert deepseek.HttpRequest is openai.HttpRequest
        assert deepseek.HttpResponse is openai.HttpResponse
        assert deepseek.HttpTransportError is openai.HttpTransportError
        assert deepseek.urllib_transport is openai.urllib_transport
        assert deepseek.ModelPrice is openai.ModelPrice
        assert deepseek.is_retryable_status is openai.is_retryable_status


class TestModelResolution:
    """The model is configuration, never architecture truth."""

    def test_the_explicit_constructor_model_wins(self, monkeypatch) -> None:
        monkeypatch.setenv(DEEPSEEK_MODEL_ENV_VAR, "from-env")

        assert make_advisor(model="explicit-model").model == "explicit-model"

    def test_the_environment_names_the_model_when_no_argument_does(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv(DEEPSEEK_MODEL_ENV_VAR, "deepseek-from-env")

        assert make_advisor(model=None).model == "deepseek-from-env"

    def test_the_documented_default_is_the_last_word(self, monkeypatch) -> None:
        monkeypatch.delenv(DEEPSEEK_MODEL_ENV_VAR, raising=False)

        assert resolve_deepseek_model(None) == DEFAULT_DEEPSEEK_MODEL
        assert resolve_deepseek_model("   ") == DEFAULT_DEEPSEEK_MODEL


class TestSecrets:
    """The key travels in one header and appears nowhere else, ever."""

    def test_a_missing_key_raises_and_never_calls_the_transport(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv(DEEPSEEK_API_KEY_ENV_VAR, raising=False)
        transport = FakeTransport()
        advisor = make_advisor(transport=transport, api_key=None)

        assert advisor.is_configured is False
        with pytest.raises(DeepSeekMissingApiKeyError):
            advisor.advise(make_query())
        assert transport.call_count == 0

    def test_a_blank_key_counts_as_missing(self) -> None:
        assert make_advisor(api_key="   ").is_configured is False

    def test_the_environment_key_is_read_lazily(self, monkeypatch) -> None:
        monkeypatch.setenv(DEEPSEEK_API_KEY_ENV_VAR, KEY)

        assert make_advisor(api_key=None).is_configured is True

    def test_the_key_is_only_in_the_authorization_header(self) -> None:
        transport = FakeTransport()
        advisor = make_advisor(transport=transport)

        advisor.advise(make_query())

        request = transport.requests[0]
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        assert KEY not in request.text()

    def test_repr_never_renders_the_key(self) -> None:
        advisor = make_advisor()

        assert KEY not in repr(advisor)
        assert KEY not in str(advisor)
        assert "configured=True" in repr(advisor)

    def test_the_key_never_leaks_through_a_broken_answer(self) -> None:
        transport = FakeTransport([response(f"bad key {KEY}")])

        with pytest.raises(DeepSeekAdvisorInvalidResponseError) as raised:
            make_advisor(transport).advise(make_query())

        assert KEY not in str(raised.value)

    def test_the_key_never_leaks_through_an_error_message(self) -> None:
        transport = FakeTransport(
            [HttpResponse(401, f"bad key {KEY}".encode("utf-8"))]
        )

        with pytest.raises(DeepSeekAdvisorHttpError) as raised:
            make_advisor(transport).advise(make_query())

        assert KEY not in str(raised.value)


class TestAbstainAndContract:
    """An abstention is a deliberate answer; a broken contract is a defect."""

    def test_an_abstain_raises_its_own_error_with_the_models_reason(self) -> None:
        transport = FakeTransport(
            [
                answer(
                    status="ABSTAIN",
                    claim="",
                    evidence=[],
                    reason="the context is insufficient",
                )
            ]
        )

        with pytest.raises(DeepSeekAdvisorAbstainError) as raised:
            make_advisor(transport).advise(make_query())

        assert raised.value.reason == "the context is insufficient"

    def test_a_broken_contract_is_never_reported_as_an_abstain(self) -> None:
        transport = FakeTransport([response("not json at all")])

        with pytest.raises(DeepSeekAdvisorInvalidResponseError):
            make_advisor(transport).advise(make_query())

    @pytest.mark.parametrize(
        "override",
        [
            {"status": "MAYBE"},
            {"claim": "   "},
            {"evidence": []},
            {"confidence": 1.5},
            {"severity": "HUGE"},
        ],
    )
    def test_a_broken_contract_raises_invalid_response(self, override) -> None:
        transport = FakeTransport([answer(**override)])

        with pytest.raises(DeepSeekAdvisorInvalidResponseError):
            make_advisor(transport).advise(make_query())

    def test_an_empty_choice_list_is_a_contract_defect(self) -> None:
        transport = FakeTransport([response({"id": "x", "choices": []})])

        with pytest.raises(DeepSeekAdvisorInvalidResponseError):
            make_advisor(transport).advise(make_query())


class TestRetryPolicy:
    """Only a timeout, HTTP 429 and HTTP 5xx are retried."""

    def test_a_429_is_retried_and_then_fails_closed(self) -> None:
        transport = FakeTransport([HttpResponse(429, b"slow down")])

        with pytest.raises(DeepSeekAdvisorRateLimitError):
            make_advisor(transport).advise(make_query())

        assert transport.call_count == 3

    def test_an_exhausted_5xx_becomes_an_http_error(self) -> None:
        transport = FakeTransport([HttpResponse(500, b"boom")])

        with pytest.raises(DeepSeekAdvisorHttpError):
            make_advisor(transport).advise(make_query())

        assert transport.call_count == 3

    def test_a_retryable_transport_failure_ends_as_a_transport_error(self) -> None:
        transport = FakeTransport([HttpTransportError("down", retryable=True)])

        with pytest.raises(DeepSeekAdvisorTransportError):
            make_advisor(transport).advise(make_query())

        assert transport.call_count == 3

    def test_a_timeout_is_not_retried_when_the_transport_says_so(self) -> None:
        transport = FakeTransport(
            [HttpTransportError("timed out", retryable=False, timed_out=True)]
        )

        with pytest.raises(DeepSeekAdvisorTransportError):
            make_advisor(transport).advise(make_query())

        assert transport.call_count == 1

    def test_a_permanent_4xx_is_not_retried(self) -> None:
        transport = FakeTransport([HttpResponse(400, b"bad request")])

        with pytest.raises(DeepSeekAdvisorHttpError):
            make_advisor(transport).advise(make_query())

        assert transport.call_count == 1

    def test_a_retry_that_succeeds_uses_the_identical_payload(self) -> None:
        transport = FakeTransport([HttpResponse(503, b"boom"), answer()])

        finding = make_advisor(transport).advise(make_query())

        assert finding.source == "deepseek"
        assert transport.call_count == 2
        assert transport.requests[0] == transport.requests[1]


class TestCostTelemetry:
    """Cost reporting is a secondary, never-fatal side effect."""

    def test_one_successful_answer_records_one_priced_unknown_event(self) -> None:
        sink = RecordingCostSink()

        make_advisor(cost_sink=sink, project="demo").advise(make_query())

        assert len(sink.records) == 1
        record = sink.records[0]
        assert record.provider == "deepseek"
        assert record.model == DEFAULT_DEEPSEEK_MODEL
        assert record.event_id == "deepseek:chatcmpl-ds"
        assert record.input_tokens == 120
        assert record.output_tokens == 30
        assert record.pricing_known is False
        assert record.project == "demo"
        assert record.step_no == 29
        assert KEY not in repr(record)

    def test_a_priced_model_reports_its_cost(self) -> None:
        sink = RecordingCostSink()
        advisor = make_advisor(
            cost_sink=sink,
            pricing={DEFAULT_DEEPSEEK_MODEL: ModelPrice(1.0, 2.0)},
        )

        advisor.advise(make_query())

        assert sink.records[0].pricing_known is True
        assert sink.records[0].cost_usd == 0.18

    def test_no_usage_means_no_record(self) -> None:
        sink = RecordingCostSink()
        transport = FakeTransport(
            [response(envelope(json.dumps(contract()), usage=False))]
        )

        make_advisor(transport, cost_sink=sink).advise(make_query())

        assert sink.records == []

    def test_a_broken_contract_still_records_its_billable_cost(self) -> None:
        """The call was paid for either way - telemetry comes first.

        The envelope is valid (so the answer really was delivered); the *contract*
        inside it is broken, which is exactly the case where the call has already
        been paid for and the record must not be lost.
        """
        sink = RecordingCostSink()
        transport = FakeTransport([response(envelope('{"status":"OK"}'))])

        with pytest.raises(DeepSeekAdvisorInvalidResponseError):
            make_advisor(transport, cost_sink=sink).advise(make_query())

        assert len(sink.records) == 1

    def test_a_response_without_an_id_records_nothing(self) -> None:
        sink = RecordingCostSink()
        payload = envelope(json.dumps(contract()))
        del payload["id"]
        transport = FakeTransport([response(payload)])
        advisor = make_advisor(transport, cost_sink=sink)

        advisor.advise(make_query())

        assert sink.records == []
        assert isinstance(advisor.last_telemetry_error, CostIdentityUnavailableError)


class TestConnectionProbe:
    """The smallest safe provider call, reduced to one documented verdict."""

    def verdict(self, transport=None, **overrides) -> str:
        return make_advisor(transport=transport, **overrides)._test_connection()

    def test_a_2xx_is_connected(self) -> None:
        assert self.verdict() == ConnectionStatus.CONNECTED.value

    @pytest.mark.parametrize("status_code", [401, 403])
    def test_an_authentication_status_is_an_auth_error(self, status_code: int) -> None:
        transport = FakeTransport([HttpResponse(status_code, b"nope")])

        assert self.verdict(transport) == ConnectionStatus.AUTH_ERROR.value

    @pytest.mark.parametrize("status_code", [400, 404, 422])
    def test_a_model_status_is_a_model_error(self, status_code: int) -> None:
        transport = FakeTransport([HttpResponse(status_code, b"no such model")])

        assert self.verdict(transport) == ConnectionStatus.MODEL_ERROR.value

    @pytest.mark.parametrize("status_code", [429, 500, 503])
    def test_a_provider_status_is_a_provider_error(self, status_code: int) -> None:
        transport = FakeTransport([HttpResponse(status_code, b"boom")])

        assert self.verdict(transport) == ConnectionStatus.PROVIDER_ERROR.value

    def test_a_transport_failure_is_a_network_error(self) -> None:
        transport = FakeTransport([HttpTransportError("no route", retryable=True)])

        assert self.verdict(transport) == ConnectionStatus.NETWORK_ERROR.value

    def test_a_missing_key_is_an_auth_error_and_makes_no_call(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv(DEEPSEEK_API_KEY_ENV_VAR, raising=False)
        transport = FakeTransport()

        verdict = self.verdict(transport, api_key=None)

        assert verdict == ConnectionStatus.AUTH_ERROR.value
        assert transport.call_count == 0

    def test_the_probe_references_the_model_and_costs_one_token(self) -> None:
        transport = FakeTransport()

        self.verdict(transport, model="deepseek-reasoner")

        body = json.loads(transport.requests[0].text())
        assert body["model"] == "deepseek-reasoner"
        assert body["max_tokens"] == 1

    def test_the_probe_sends_the_key_only_as_a_header(self) -> None:
        transport = FakeTransport()

        self.verdict(transport)

        assert transport.requests[0].headers["Authorization"] == f"Bearer {KEY}"
        assert KEY not in transport.requests[0].text()

    def test_the_probe_is_not_retried(self) -> None:
        transport = FakeTransport([HttpResponse(500, b"boom")])

        self.verdict(transport)

        assert transport.call_count == 1
