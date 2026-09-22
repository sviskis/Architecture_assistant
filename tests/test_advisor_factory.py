"""Tests for the composition-level advisor factory and the disabled advisor.

The factory is the one place that turns *plain configuration* - a provider token,
a model and a credential - into an ``AdvisorPort``. Two properties matter most:

* **the GUI never names an adapter class.** Everything it can ask for goes
  through :class:`AdvisorFactory` / :func:`provider_catalog`;
* **a selection is really used.** Choosing DeepSeek (or any provider) and a model
  must change which adapter answers and which model is asked - not just what the
  form shows.

The disabled advisor is covered here too: it must be a structurally valid
``AdvisorPort`` that makes **no provider call at all**, and the observation seam
must classify its abstention as an abstention.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from architecture_assistant.application import (
    AdvisorObservation,
    ObservationStatus,
)
from architecture_assistant.composition import (
    PROVIDER_DEFAULT_MODELS,
    AdvisorFactory,
    ConnectionStatus,
    assemble_reviewers,
    default_model_for,
    observe,
    probe_connection,
    provider_catalog,
    settings_from_mapping,
)
from architecture_assistant.infrastructure import (
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_DEEPSEEK_MODEL,
    DEFAULT_GROK_MODEL,
    DEFAULT_OPENAI_MODEL,
    DISABLED_PROVIDER,
    ClaudeAdvisorAdapter,
    DeepSeekAdvisorAdapter,
    DisabledAdvisorAbstainError,
    DisabledAdvisorAdapter,
    DisabledAdvisorError,
    GrokAdvisorAdapter,
    HttpRequest,
    HttpResponse,
    HttpTransportError,
    OpenAIAdvisorAdapter,
)
from architecture_assistant.ports.capabilities import AdvisorPort, AdvisorQuery

FIXED = datetime(2026, 9, 22, 11, 0, tzinfo=timezone.utc)

#: A literal that is not, and never was, a real credential.
KEY = "deepseek-test-factory-not-a-real-secret"

QUESTION = "Which provider answers this question, and with which model?"


def fixed_clock() -> datetime:
    return FIXED


def reply(model_echo: str, provider: str) -> HttpResponse:
    """A valid answer in that provider's own wire shape, echoing its model.

    The wire formats genuinely differ - Anthropic answers with a *list of content
    blocks* and reports ``input_tokens``/``output_tokens``, while OpenAI, xAI and
    DeepSeek answer with ``choices[0].message.content`` and
    ``prompt_tokens``/``completion_tokens`` - so the fake transport answers each
    provider in its own shape rather than pretending they are interchangeable.
    """
    content = json.dumps(
        {
            "status": "OK",
            "claim": f"answered by {model_echo}",
            "evidence": [f"echo:{QUESTION}"],
            "confidence": 0.5,
            "severity": "LOW",
            "reason": "",
        }
    )
    if provider == "claude":
        payload = {
            "id": f"msg-{model_echo}",
            "content": [{"type": "text", "text": content}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    else:
        payload = {
            "id": f"chatcmpl-{model_echo}",
            "choices": [
                {"message": {"role": "assistant", "content": content}}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
    return HttpResponse(200, json.dumps(payload).encode("utf-8"))


@dataclass
class FakeTransport:
    """A transport that records every request and answers in a provider's shape."""

    requests: list = field(default_factory=list)
    status_code: int = 200
    provider: str = "openai"

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if self.status_code != 200:
            return HttpResponse(self.status_code, b"nope")
        body = json.loads(request.text())
        return reply(str(body.get("model", "?")), self.provider)

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def models(self) -> list[str]:
        return [json.loads(request.text()).get("model") for request in self.requests]

    def questions(self) -> list[str]:
        return [QUESTION for request in self.requests if QUESTION in request.text()]


class TestCatalog:
    """The panel's choices come from configuration, never from a hardcoded list."""

    def test_every_supported_provider_is_offered_once(self) -> None:
        catalog = provider_catalog()

        assert [entry["provider"] for entry in catalog] == [
            "openai",
            "claude",
            "grok",
            "deepseek",
            "disabled",
        ]
        assert [entry["label"] for entry in catalog] == [
            "OpenAI",
            "Claude",
            "Grok",
            "DeepSeek",
            "Disabled",
        ]

    def test_every_offered_provider_is_available_and_key_free(self) -> None:
        for entry in provider_catalog():
            assert entry["available"] is True, entry
            assert "api_key" not in entry
            assert "secret" not in json.dumps(entry)

    def test_the_default_models_are_the_adapters_own_constants(self) -> None:
        assert PROVIDER_DEFAULT_MODELS == {
            "openai": DEFAULT_OPENAI_MODEL,
            "claude": DEFAULT_CLAUDE_MODEL,
            "grok": DEFAULT_GROK_MODEL,
            "deepseek": DEFAULT_DEEPSEEK_MODEL,
            "disabled": "",
        }
        assert default_model_for("deepseek") == DEFAULT_DEEPSEEK_MODEL
        assert default_model_for("nonsense") == ""

    def test_the_catalog_reports_availability_honestly(self) -> None:
        assert AdvisorFactory.available("deepseek") is True
        assert AdvisorFactory.available("not-a-provider") is False


class TestCreate:
    """Plain configuration in, an AdvisorPort out."""

    @pytest.mark.parametrize(
        ("provider", "expected"),
        [
            ("openai", OpenAIAdvisorAdapter),
            ("claude", ClaudeAdvisorAdapter),
            ("grok", GrokAdvisorAdapter),
            ("deepseek", DeepSeekAdvisorAdapter),
            ("disabled", DisabledAdvisorAdapter),
        ],
    )
    def test_each_provider_resolves_to_its_own_adapter(
        self, provider: str, expected: type
    ) -> None:
        adapter = AdvisorFactory.create(provider, "", None)

        assert isinstance(adapter, expected)
        assert isinstance(adapter, AdvisorPort)
        assert adapter.provider == provider

    def test_the_provider_token_is_case_and_space_insensitive(self) -> None:
        assert AdvisorFactory.create("  DeepSeek ", "", None).provider == "deepseek"

    def test_a_blank_model_falls_back_to_the_documented_default(self) -> None:
        assert (
            AdvisorFactory.create("deepseek", "", None).model
            == DEFAULT_DEEPSEEK_MODEL
        )
        assert (
            AdvisorFactory.create("openai", "", None).model == DEFAULT_OPENAI_MODEL
        )

    def test_a_pinned_model_is_honoured(self) -> None:
        assert (
            AdvisorFactory.create("deepseek", "deepseek-reasoner", None).model
            == "deepseek-reasoner"
        )

    def test_an_unknown_provider_is_refused(self) -> None:
        with pytest.raises(ValueError, match="provider must be one of"):
            AdvisorFactory.create("gemini", "", None)

    def test_a_non_string_credential_is_refused_without_echoing_it(self) -> None:
        with pytest.raises(ValueError):
            AdvisorFactory.create("openai", "", 12345)  # type: ignore[arg-type]


class TestDisabledAdvisor:
    """A disabled slot is a real AdvisorPort that calls nothing."""

    def test_it_implements_the_port_and_reports_itself(self) -> None:
        advisor = DisabledAdvisorAdapter()

        assert isinstance(advisor, AdvisorPort)
        assert advisor.provider == DISABLED_PROVIDER == "disabled"
        assert advisor.model == ""
        assert advisor.is_configured is False
        assert DISABLED_PROVIDER in repr(advisor)

    def test_it_holds_no_transport_at_all(self) -> None:
        """A provider call is not merely skipped - there is nothing to call."""
        advisor = DisabledAdvisorAdapter()

        assert not hasattr(advisor, "_transport")
        assert not hasattr(advisor, "_url")
        assert not hasattr(advisor, "_api_key")

    def test_advise_abstains_without_calling_anything(self) -> None:
        advisor = DisabledAdvisorAdapter()

        with pytest.raises(DisabledAdvisorAbstainError) as raised:
            advisor.advise(AdvisorQuery(question=QUESTION))

        assert "disabled" in str(raised.value)
        assert isinstance(raised.value, DisabledAdvisorError)
        assert raised.value.reason

    def test_the_observation_seam_calls_it_an_abstention_not_an_error(self) -> None:
        advisor = DisabledAdvisorAdapter()

        observation = observe(
            advisor.provider,
            lambda: advisor.advise(AdvisorQuery(question=QUESTION)),
        )

        assert isinstance(observation, AdvisorObservation)
        assert observation.status is ObservationStatus.ABSTAIN
        assert "DisabledAdvisorAbstainError" in observation.reason
        assert observation.finding is None

    def test_the_query_type_is_still_validated(self) -> None:
        with pytest.raises(ValueError, match="AdvisorQuery"):
            DisabledAdvisorAdapter().advise("not a query")

    def test_its_test_connection_makes_no_call_and_answers_disabled(self) -> None:
        assert (
            DisabledAdvisorAdapter()._test_connection()
            == ConnectionStatus.DISABLED.value
        )


class TestProbe:
    """Test Connection reduces one smallest-safe call to one verdict."""

    def test_the_disabled_provider_is_disabled(self) -> None:
        assert probe_connection("disabled") == ConnectionStatus.DISABLED.value

    def test_an_unknown_provider_is_a_provider_error(self) -> None:
        assert (
            probe_connection("not-a-provider")
            == ConnectionStatus.PROVIDER_ERROR.value
        )

    def test_a_good_configuration_is_connected(self) -> None:
        transport = FakeTransport()

        verdict = probe_connection(
            "deepseek", "deepseek-chat", KEY, transport=transport
        )

        assert verdict == ConnectionStatus.CONNECTED.value
        assert transport.call_count == 1

    def test_a_rejected_key_is_an_auth_error(self) -> None:
        transport = FakeTransport(status_code=401)

        verdict = probe_connection(
            "openai", "gpt-4.1-mini", KEY, transport=transport
        )

        assert verdict == ConnectionStatus.AUTH_ERROR.value

    def test_an_unknown_model_is_a_model_error(self) -> None:
        transport = FakeTransport(status_code=404)

        verdict = probe_connection("grok", "no-such-model", KEY, transport=transport)

        assert verdict == ConnectionStatus.MODEL_ERROR.value

    def test_a_refusing_provider_is_a_provider_error(self) -> None:
        transport = FakeTransport(status_code=500)

        verdict = probe_connection("claude", "claude-x", KEY, transport=transport)

        assert verdict == ConnectionStatus.PROVIDER_ERROR.value

    def test_no_route_is_a_network_error(self) -> None:
        def down(_request: HttpRequest) -> HttpResponse:
            raise HttpTransportError("no route to host", retryable=True)

        verdict = probe_connection("openai", "gpt-4.1-mini", KEY, transport=down)

        assert verdict == ConnectionStatus.NETWORK_ERROR.value

    def test_a_missing_key_is_an_auth_error(self, monkeypatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        assert (
            probe_connection("openai", "gpt-4.1-mini", None)
            == ConnectionStatus.AUTH_ERROR.value
        )

    def test_the_verdict_is_never_a_header_or_a_body(self) -> None:
        transport = FakeTransport(status_code=401)

        verdict = probe_connection("openai", "m", KEY, transport=transport)

        assert verdict in {status.value for status in ConnectionStatus}
        assert KEY not in verdict


class TestAssembleReviewers:
    """The configuration decides the reviewers - and nothing else does."""

    def test_one_reviewer_per_slot_in_the_configured_order(self) -> None:
        settings = settings_from_mapping(
            {
                "advisor_1": {
                    "provider": "deepseek",
                    "model": "deepseek-chat",
                    "api_key": KEY,
                },
                "advisor_2": {"provider": "disabled"},
                "advisor_3": {"provider": "openai", "model": "gpt-4.1-mini"},
            }
        )

        reviewers = assemble_reviewers(settings, clock=fixed_clock)

        assert [reviewer.source for reviewer in reviewers] == [
            "deepseek",
            "disabled",
            "openai",
        ]

    def test_a_default_slot_reuses_the_shared_canonical_instance(self) -> None:
        """One provider is one adapter object - never a second, parallel one."""
        canonical = {
            "openai": OpenAIAdvisorAdapter(transport=FakeTransport()),
            "claude": ClaudeAdvisorAdapter(transport=FakeTransport()),
            "grok": GrokAdvisorAdapter(transport=FakeTransport()),
        }

        reviewers = assemble_reviewers(
            settings_from_mapping({}), clock=fixed_clock, shared=canonical
        )

        assert [reviewer.advisor for reviewer in reviewers] == [
            canonical["openai"],
            canonical["claude"],
            canonical["grok"],
        ]

    def test_a_slot_that_pins_a_model_gets_its_own_adapter(self) -> None:
        canonical = {"openai": OpenAIAdvisorAdapter(transport=FakeTransport())}
        settings = settings_from_mapping(
            {"advisor_1": {"provider": "openai", "model": "gpt-from-settings"}}
        )

        reviewers = assemble_reviewers(
            settings, clock=fixed_clock, shared=canonical
        )

        assert reviewers[0].advisor is not canonical["openai"]
        assert reviewers[0].advisor.model == "gpt-from-settings"

    def test_settings_must_be_a_settings_object(self) -> None:
        with pytest.raises(ValueError):
            assemble_reviewers({"advisor_1": {}})  # type: ignore[arg-type]


class TestSelectionIsActuallyUsed:
    """The chosen provider and model decide which adapter answers, and how."""

    def make(self, provider: str, model: str, transport) -> AdvisorPort:
        return AdvisorFactory.create(
            provider, model, KEY, clock=fixed_clock, transport=transport
        )

    def transport_for(self, provider: str) -> FakeTransport:
        return FakeTransport(provider=provider)

    @pytest.mark.parametrize(
        "provider", ["openai", "claude", "grok", "deepseek"]
    )
    def test_the_selected_provider_answers_and_reports_itself(
        self, provider: str
    ) -> None:
        transport = self.transport_for(provider)
        advisor = self.make(provider, "", transport)

        finding = advisor.advise(AdvisorQuery(question=QUESTION, step_no=1))

        assert finding.source == provider
        assert transport.call_count == 1

    def test_the_selected_model_is_the_model_that_is_asked(self) -> None:
        transport = self.transport_for("deepseek")
        advisor = self.make("deepseek", "deepseek-reasoner", transport)

        advisor.advise(AdvisorQuery(question=QUESTION, step_no=1))

        assert transport.models() == ["deepseek-reasoner"]

    def test_the_default_model_is_the_one_asked_when_none_is_pinned(self) -> None:
        transport = self.transport_for("deepseek")
        advisor = self.make("deepseek", "", transport)

        advisor.advise(AdvisorQuery(question=QUESTION, step_no=1))

        assert transport.models() == [DEFAULT_DEEPSEEK_MODEL]

    def test_every_enabled_advisor_receives_the_same_question(self) -> None:
        transports = {
            provider: self.transport_for(provider)
            for provider in ("openai", "claude", "grok", "deepseek")
        }

        for provider, transport in transports.items():
            self.make(provider, "", transport).advise(
                AdvisorQuery(question=QUESTION, step_no=1)
            )

        for provider, transport in transports.items():
            assert transport.questions() == [QUESTION], provider

    def test_a_disabled_slot_abstains_while_the_others_still_answer(self) -> None:
        first = self.transport_for("deepseek")
        third = self.transport_for("openai")
        advisors = (
            self.make("deepseek", "deepseek-chat", first),
            DisabledAdvisorAdapter(),
            self.make("openai", "gpt-4.1-mini", third),
        )

        observations = [
            observe(
                advisor.provider,
                lambda advisor=advisor: advisor.advise(
                    AdvisorQuery(question=QUESTION, step_no=1)
                ),
            )
            for advisor in advisors
        ]

        assert [observation.status for observation in observations] == [
            ObservationStatus.FINDING,
            ObservationStatus.ABSTAIN,
            ObservationStatus.FINDING,
        ]
        # the two enabled advisors each got the question exactly once
        assert first.questions() == [QUESTION]
        assert third.questions() == [QUESTION]
        # ... and the disabled one has no transport that could have been called
        assert not hasattr(advisors[1], "_transport")
