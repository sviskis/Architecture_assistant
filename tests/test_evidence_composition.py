"""Tests for the Step 15 composition seam.

``composition.evidence.observe`` is the only place that knows a provider
adapter's error type. These tests pin that mapping (finding / abstention /
failure), the default relation, the sanitised reasons, and the fact that a real
three-advisor run lands in one merged view without any relation being invented.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from architecture_assistant.application import (
    AdvisorObservation,
    EvidenceRelation,
    ObservationStatus,
    decide_from_observations,
    merge_evidence,
)
from architecture_assistant.composition import (
    ABSTAIN_ERRORS,
    ABSTAIN_REASON_PREFIX,
    ADVISOR_ERRORS,
    ERROR_REASON_PREFIX,
    observe,
)
from architecture_assistant.domain.enums import DecisionStatus, Severity
from architecture_assistant.domain.models import Decision, Finding
from architecture_assistant.infrastructure import (
    CLAUDE_API_KEY_ENV_VAR,
    DEEPSEEK_API_KEY_ENV_VAR,
    OPENAI_API_KEY_ENV_VAR,
    XAI_API_KEY_ENV_VAR,
    ClaudeAdvisorAbstainError,
    ClaudeAdvisorAdapter,
    ClaudeAdvisorHttpError,
    DeepSeekAdvisorAbstainError,
    DeepSeekAdvisorError,
    DisabledAdvisorAbstainError,
    DisabledAdvisorError,
    GrokAdvisorAbstainError,
    GrokAdvisorAdapter,
    GrokAdvisorTimeoutError,
    HttpRequest,
    HttpResponse,
    OpenAIAdvisorAbstainError,
    OpenAIAdvisorAdapter,
    OpenAIAdvisorHttpError,
)
from architecture_assistant.ports.capabilities import AdvisorQuery

FIXED = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)

#: A literal that is not, and never was, a real credential.
KEY = "sk-ant-test-not-a-real-secret"

QUESTION = "Are we solving the right problem?"


def fixed_clock() -> datetime:
    return FIXED


def query() -> AdvisorQuery:
    return AdvisorQuery(
        question=QUESTION,
        step_no=15,
        context={"module": "application/evidence_merger.py"},
    )


def contract(**overrides) -> dict:
    payload = {
        "status": "OK",
        "claim": "a structured claim",
        "evidence": ["application/context.py:12"],
        "confidence": 0.5,
        "severity": "MEDIUM",
        "reason": "",
    }
    payload.update(overrides)
    return payload


def envelope(payload: dict, *, shape: str) -> HttpResponse:
    """Wrap a contract in the provider's own envelope shape."""
    text = json.dumps(payload)
    if shape == "anthropic":
        body = {
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 5, "output_tokens": 7},
        }
    else:
        body = {
            "choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7},
        }
    return HttpResponse(200, json.dumps(body).encode("utf-8"))


class ScriptedTransport:
    """A minimal offline transport: each call returns the next response."""

    def __init__(self, *responses: HttpResponse) -> None:
        self._responses = list(responses)
        self.calls = 0

    def __call__(self, request: HttpRequest) -> HttpResponse:
        index = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[index]


def finding(source: str = "openai") -> Finding:
    return Finding(
        id=f"finding-{source}-15-abc123abc123",
        source=source,
        claim="a risk",
        evidence=("application/context.py:12",),
        confidence=0.5,
        severity=Severity.MEDIUM,
        step_no=15,
    )


def gate(status: DecisionStatus = DecisionStatus.ACCEPTED) -> Decision:
    return Decision(
        id="realization-step-015-attempt-001",
        status=status,
        decision="compliant" if status is DecisionStatus.ACCEPTED else "violates",
        rationale="fresh deterministic validation of step 015 attempt 001",
        rules_applied=("unknown-layer",),
        evidence_refs=(),
        perspectives=(),
        step_no=15,
    )


class TestObserve:
    """One advisor run becomes one provider-neutral observation."""

    def test_a_finding_becomes_a_finding_observation(self) -> None:
        produced = finding()

        observation = observe("openai", lambda: produced)

        assert isinstance(observation, AdvisorObservation)
        assert observation.status is ObservationStatus.FINDING
        assert observation.finding is produced
        assert observation.reason is None

    def test_the_default_relation_is_unresolved(self) -> None:
        observation = observe("openai", lambda: finding())

        assert observation.relation is EvidenceRelation.UNRESOLVED
        assert observation.relation_target is None

    def test_an_explicit_relation_is_passed_through_unchecked(self) -> None:
        """The seam never validates - the merger does, against the gate."""
        observation = observe(
            "openai",
            lambda: finding(),
            relation=EvidenceRelation.SUPPORTING,
            relation_target="forbidden-layer-import",
        )

        assert observation.relation is EvidenceRelation.SUPPORTING
        assert observation.relation_target == "forbidden-layer-import"

    @pytest.mark.parametrize(
        "error",
        [
            OpenAIAdvisorAbstainError("the model refused"),
            ClaudeAdvisorAbstainError("the model refused"),
            GrokAdvisorAbstainError("the model refused"),
        ],
    )
    def test_every_provider_abstention_becomes_an_abstain(self, error) -> None:
        def run():
            raise error

        observation = observe("openai", run)

        assert observation.status is ObservationStatus.ABSTAIN
        assert observation.finding is None
        assert observation.reason.startswith(ABSTAIN_REASON_PREFIX)

    @pytest.mark.parametrize(
        "error",
        [
            OpenAIAdvisorHttpError("http 500"),
            ClaudeAdvisorHttpError("http 500"),
            GrokAdvisorTimeoutError("timed out"),
        ],
    )
    def test_every_provider_failure_becomes_an_error(self, error) -> None:
        def run():
            raise error

        observation = observe("claude", run)

        assert observation.status is ObservationStatus.ERROR
        assert observation.reason.startswith(ERROR_REASON_PREFIX)

    def test_the_reason_names_the_exception_type(self) -> None:
        def run():
            raise GrokAdvisorTimeoutError("timed out after 30s")

        observation = observe("grok", run)

        assert "GrokAdvisorTimeoutError" in observation.reason
        assert "timed out after 30s" not in observation.reason

    def test_a_provider_exception_message_is_never_used(self) -> None:
        secret = "sk-ant-super-secret-value"

        def run():
            raise ClaudeAdvisorHttpError(f"failed with {secret}")

        observation = observe("claude", run)

        assert secret not in observation.reason
        assert observation.reason == "advisor failed: ClaudeAdvisorHttpError"

    def test_the_exception_repr_is_never_called(self) -> None:
        class Explosive(OpenAIAdvisorHttpError):
            def __repr__(self) -> str:  # pragma: no cover - must never run
                raise AssertionError("repr must never be called")

        def run():
            raise Explosive("boom")

        observation = observe("openai", run)

        assert observation.status is ObservationStatus.ERROR
        assert observation.reason == "advisor failed: Explosive"

    def test_a_non_callable_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="run must be a callable"):
            observe("openai", "not callable")

    def test_a_non_finding_result_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must carry a Finding"):
            observe("openai", lambda: "not a finding")

    def test_a_source_mismatch_is_rejected(self) -> None:
        produced = finding(source="claude")

        with pytest.raises(ValueError, match="must match the finding"):
            observe("openai", lambda: produced)

    def test_an_abstention_error_is_more_specific_than_an_advisor_error(
        self,
    ) -> None:
        for abstain_error in ABSTAIN_ERRORS:
            if abstain_error is DisabledAdvisorAbstainError:
                # The one deliberate exception. A disabled slot has *no* provider
                # to fail, so its abstention belongs to no provider family - it is
                # still an abstention (never an ERROR), it simply is not a more
                # specific kind of provider failure.
                assert issubclass(abstain_error, DisabledAdvisorError)
                continue
            assert issubclass(abstain_error, ADVISOR_ERRORS)


def _advisors(broken: str | None = None):
    """The three real adapters, wired offline - optionally with one broken."""
    specs = {
        "openai": (OpenAIAdvisorAdapter, "openai"),
        "claude": (ClaudeAdvisorAdapter, "anthropic"),
        "grok": (GrokAdvisorAdapter, "openai"),
    }
    wired = []
    for source, (adapter_type, shape) in specs.items():
        if source == broken:
            transport = ScriptedTransport(HttpResponse(503, b'{"error":"down"}'))
        else:
            transport = ScriptedTransport(envelope(contract(), shape=shape))
        wired.append(
            (
                source,
                adapter_type(
                    api_key=KEY,
                    transport=transport,
                    sleep=lambda _seconds: None,
                ),
            )
        )
    return tuple(wired)


class TestRealAdapters:
    """A real three-advisor run lands in one merged view - with no invention."""

    def test_a_real_abstention_carries_no_provider_text(self) -> None:
        payload = contract(
            status="ABSTAIN", claim="", evidence=[], reason="out of scope"
        )
        advisor = ClaudeAdvisorAdapter(
            api_key=KEY,
            transport=ScriptedTransport(envelope(payload, shape="anthropic")),
            sleep=lambda _seconds: None,
        )

        observation = observe("claude", lambda: advisor.advise(query()))

        assert observation.status is ObservationStatus.ABSTAIN
        assert observation.reason == (
            "advisor abstained: ClaudeAdvisorAbstainError"
        )
        assert "out of scope" not in observation.reason

    def test_a_missing_key_becomes_an_error_not_a_crash(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv(OPENAI_API_KEY_ENV_VAR, raising=False)
        advisor = OpenAIAdvisorAdapter(
            api_key=None,
            transport=ScriptedTransport(envelope(contract(), shape="openai")),
            sleep=lambda _seconds: None,
        )

        observation = observe("openai", lambda: advisor.advise(query()))

        assert observation.status is ObservationStatus.ERROR
        assert observation.reason == "advisor failed: MissingApiKeyError"

    def test_a_broken_contract_becomes_an_error(self) -> None:
        advisor = OpenAIAdvisorAdapter(
            api_key=KEY,
            transport=ScriptedTransport(HttpResponse(200, b'{"choices": []}')),
            sleep=lambda _seconds: None,
        )

        observation = observe("openai", lambda: advisor.advise(query()))

        assert observation.status is ObservationStatus.ERROR
        assert observation.reason == (
            "advisor failed: OpenAIAdvisorInvalidResponseError"
        )

    def test_three_real_adapters_land_in_one_view(self) -> None:
        observations = tuple(
            observe(source, lambda advisor=advisor: advisor.advise(query()))
            for source, advisor in _advisors()
        )

        view = merge_evidence(observations)

        assert len(view.findings) == 3
        assert view.sources == ("claude", "grok", "openai")
        # nothing was declared by any adapter, so nothing was inferred
        assert view.supporting_ids == ()
        assert view.conflicting_ids == ()
        assert view.unresolved_ids == view.finding_ids
        assert view.conflicts == ()
        assert view.abstained == ()
        assert view.errors == ()

    def test_one_broken_provider_does_not_block_the_others(self) -> None:
        observations = tuple(
            observe(source, lambda advisor=advisor: advisor.advise(query()))
            for source, advisor in _advisors(broken="grok")
        )

        view = merge_evidence(observations)

        assert len(view.findings) == 2
        assert view.sources == ("claude", "openai")
        assert [entry.source for entry in view.errors] == ["grok"]

    def test_the_full_pipeline_stays_with_the_deterministic_gate(self) -> None:
        observations = tuple(
            observe(source, lambda advisor=advisor: advisor.advise(query()))
            for source, advisor in _advisors()
        )

        result = decide_from_observations(
            observations, gate=gate(), step_no=15, clock=fixed_clock
        )

        assert result.decision.status is DecisionStatus.ACCEPTED
        assert len(result.evidence.findings) == 3
        assert result.decision.perspectives == ("claude", "grok", "openai")
        assert result.decision.rationale.startswith(
            "The deterministic architecture gate accepted"
        )

    def test_the_seam_knows_every_provider_family(self) -> None:
        assert {error.__name__ for error in ADVISOR_ERRORS} == {
            "OpenAIAdvisorError",
            "ClaudeAdvisorError",
            "GrokAdvisorError",
            # every provider adapter the composition can wire is classified
            "DeepSeekAdvisorError",
        }
        assert {error.__name__ for error in ABSTAIN_ERRORS} == {
            "OpenAIAdvisorAbstainError",
            "ClaudeAdvisorAbstainError",
            "GrokAdvisorAbstainError",
            "DeepSeekAdvisorAbstainError",
            # a disabled slot abstains without ever calling a provider
            "DisabledAdvisorAbstainError",
        }
        # the disabled advisor's *base* error is deliberately not a provider
        # family: there is no provider to fail, so a defect there is still
        # classified through its own abstain error above
        assert issubclass(DisabledAdvisorAbstainError, DisabledAdvisorError)

    def test_the_deepseek_abstain_is_more_specific_than_its_family(self) -> None:
        assert issubclass(DeepSeekAdvisorAbstainError, DeepSeekAdvisorError)

    def test_the_reason_prefixes_are_stable(self) -> None:
        assert ABSTAIN_REASON_PREFIX == "advisor abstained"
        assert ERROR_REASON_PREFIX == "advisor failed"

    def test_the_seam_covers_the_configured_providers(self, monkeypatch) -> None:
        """Every provider the composition root wires has a usable key name."""
        for env_var in (
            OPENAI_API_KEY_ENV_VAR,
            CLAUDE_API_KEY_ENV_VAR,
            XAI_API_KEY_ENV_VAR,
            DEEPSEEK_API_KEY_ENV_VAR,
        ):
            monkeypatch.setenv(env_var, "literal-value")
            assert env_var
