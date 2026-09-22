"""Deliberation adapters: one architect seat and one chair, over the same transport.

Two provider-neutral ports are implemented here, and nothing else:

* :class:`DeliberationAgentAdapter` - one *independent* architect
  (``DeliberationAgentPort``): ``analyse`` for Round 1, ``reconsider`` for Round 2;
* :class:`DeliberationLeadAdapter` - the *chair* (``DeliberationLeadPort``):
  ``review`` for the review packet, ``synthesize`` for the final design.

Why not ``AdvisorPort``
-----------------------
An :class:`~architecture_assistant.ports.capabilities.AdvisorPort` answers with a
single gate-anchored ``Finding`` (one claim, one confidence, one severity). A
deliberation seat answers with a whole **proposal** - modules, data flows,
dependencies, risks, assumptions, open questions, alternatives - and then with a
``KEEP``/``REVISE``/``WITHDRAW`` reconsideration. Forcing either into a ``Finding``
would make the contract a lie, so the deliberation has its own two ports (Step 29,
approved decision 3).

What is reused
--------------
The provider *vocabulary*, the credential mechanism and the shared transport
machinery: endpoints, auth headers, model defaults and error classes come from the
existing adapters, and the HTTP/retry/price plumbing comes from the private
``_http`` module the advisor adapters already share. A provider name is therefore
still the only thing configuration has to know.

What may never happen
---------------------
No credential is logged, echoed, embedded in a result or included in an exception
message; a failure is described by its **type name** only. The adapters return
structured, JSON-safe content and never a raw transcript, and they hold no
storage, no repository, no transaction boundary and no workflow authority.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping, Optional, Sequence

from ..domain.models import utc_now
from ..ports.capabilities import (
    AgentAnalysisQuery,
    AgentAnalysisResult,
    AgentReconsiderQuery,
    AgentReconsiderResult,
    CostPort,
    CostQuery,
    CostRecord,
    FinalSynthesisResult,
    LeadReviewQuery,
    LeadReviewResult,
    LeadSynthesisQuery,
)
from ._http import (
    DEFAULT_BACKOFF_SCHEDULE,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    UNKNOWN_PRICE,
    HttpRequest,
    HttpResponse,
    HttpTransportError,
    ModelPrice,
    is_retryable_status,
    urllib_transport,
)
from .claude import (
    CLAUDE_API_KEY_ENV_VAR,
    CLAUDE_API_VERSION,
    CLAUDE_MESSAGES_URL,
    CLAUDE_MODEL_ENV_VAR,
    CLAUDE_PROVIDER,
    DEFAULT_CLAUDE_MAX_OUTPUT_TOKENS,
    DEFAULT_CLAUDE_MODEL,
)
from .deepseek import (
    DEEPSEEK_API_KEY_ENV_VAR,
    DEEPSEEK_CHAT_COMPLETIONS_URL,
    DEEPSEEK_MODEL_ENV_VAR,
    DEEPSEEK_PROVIDER,
    DEFAULT_DEEPSEEK_MAX_OUTPUT_TOKENS,
    DEFAULT_DEEPSEEK_MODEL,
)
from .grok import (
    DEFAULT_GROK_MAX_OUTPUT_TOKENS,
    DEFAULT_GROK_MODEL,
    GROK_CHAT_COMPLETIONS_URL,
    GROK_MODEL_ENV_VAR,
    GROK_PROVIDER,
    XAI_API_KEY_ENV_VAR,
)
from .openai import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_OPENAI_MODEL,
    OPENAI_API_KEY_ENV_VAR,
    OPENAI_CHAT_COMPLETIONS_URL,
    OPENAI_PROVIDER,
)

__all__ = [
    "DELIBERATION_LEAD_PROVIDERS",
    "DELIBERATION_AGENT_ROLE",
    "DELIBERATION_LEAD_ROLE",
    "DeliberationAdapterError",
    "DeliberationMissingApiKeyError",
    "DeliberationTransportError",
    "DeliberationHttpError",
    "DeliberationRateLimitError",
    "DeliberationInvalidResponseError",
    "DeliberationAgentAdapter",
    "DeliberationLeadAdapter",
    "DEFAULT_DELIBERATION_MAX_OUTPUT_TOKENS",
    "DEFAULT_DELIBERATION_TEMPERATURE",
    "resolve_deliberation_model",
]


class DeliberationAdapterError(Exception):
    """Base class for the deliberation adapters' failures."""


class DeliberationMissingApiKeyError(DeliberationAdapterError):
    """No credential was configured for the requested provider."""


class DeliberationTransportError(DeliberationAdapterError):
    """The request never produced a usable HTTP response."""


class DeliberationHttpError(DeliberationAdapterError):
    """A permanent, non-retryable HTTP status was returned."""


class DeliberationRateLimitError(DeliberationAdapterError):
    """The provider rate-limited the request (bounded retries exhausted)."""


class DeliberationInvalidResponseError(DeliberationAdapterError):
    """The response did not carry a usable JSON object."""


#: The roles, so an event or a report can name the seat without a provider.
DELIBERATION_AGENT_ROLE = "deliberation architect"
DELIBERATION_LEAD_ROLE = "architecture chair"

#: Which providers this build can actually serve as a seat or as the chair. It is
#: deliberately the same closed set the provider settings offer.
DELIBERATION_LEAD_PROVIDERS: tuple[str, ...] = (
    OPENAI_PROVIDER,
    CLAUDE_PROVIDER,
    GROK_PROVIDER,
    DEEPSEEK_PROVIDER,
)

DEFAULT_DELIBERATION_MAX_OUTPUT_TOKENS = 4096
DEFAULT_DELIBERATION_TEMPERATURE = 0.2


# ---------------------------------------------------------------------------
# the provider profiles (endpoints, auth and defaults come from the adapters)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _ProviderProfile:
    """Everything provider-specific about one deliberation request.

    ``style`` selects the request/response dialect: ``chat`` is the
    OpenAI-compatible chat-completions shape (OpenAI, Grok, DeepSeek) and
    ``messages`` is Anthropic's messages shape (Claude). Both are the shapes the
    existing advisor adapters already speak.
    """

    provider: str
    endpoint: str
    key_env_var: str
    model_env_var: str
    default_model: str
    max_output_tokens: int
    style: str


_PROFILES: dict[str, _ProviderProfile] = {
    OPENAI_PROVIDER: _ProviderProfile(
        provider=OPENAI_PROVIDER,
        endpoint=OPENAI_CHAT_COMPLETIONS_URL,
        key_env_var=OPENAI_API_KEY_ENV_VAR,
        model_env_var="OPENAI_MODEL",
        default_model=DEFAULT_OPENAI_MODEL,
        max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
        style="chat",
    ),
    GROK_PROVIDER: _ProviderProfile(
        provider=GROK_PROVIDER,
        endpoint=GROK_CHAT_COMPLETIONS_URL,
        key_env_var=XAI_API_KEY_ENV_VAR,
        model_env_var=GROK_MODEL_ENV_VAR,
        default_model=DEFAULT_GROK_MODEL,
        max_output_tokens=DEFAULT_GROK_MAX_OUTPUT_TOKENS,
        style="chat",
    ),
    DEEPSEEK_PROVIDER: _ProviderProfile(
        provider=DEEPSEEK_PROVIDER,
        endpoint=DEEPSEEK_CHAT_COMPLETIONS_URL,
        key_env_var=DEEPSEEK_API_KEY_ENV_VAR,
        model_env_var=DEEPSEEK_MODEL_ENV_VAR,
        default_model=DEFAULT_DEEPSEEK_MODEL,
        max_output_tokens=DEFAULT_DEEPSEEK_MAX_OUTPUT_TOKENS,
        style="chat",
    ),
    CLAUDE_PROVIDER: _ProviderProfile(
        provider=CLAUDE_PROVIDER,
        endpoint=CLAUDE_MESSAGES_URL,
        key_env_var=CLAUDE_API_KEY_ENV_VAR,
        model_env_var=CLAUDE_MODEL_ENV_VAR,
        default_model=DEFAULT_CLAUDE_MODEL,
        max_output_tokens=DEFAULT_CLAUDE_MAX_OUTPUT_TOKENS,
        style="messages",
    ),
}


def profile_for(provider: str) -> _ProviderProfile:
    """The profile of one provider, or fail closed for an unknown one."""
    key = str(provider).strip().casefold()
    profile = _PROFILES.get(key)
    if profile is None:
        supported = ", ".join(sorted(_PROFILES))
        raise DeliberationAdapterError(
            f"provider must be one of ({supported}); got {provider!r}"
        )
    return profile


def resolve_deliberation_model(provider: str, model: str = "") -> str:
    """The model: explicit, then the provider's environment, then its default."""
    profile = profile_for(provider)
    if isinstance(model, str) and model.strip():
        return model.strip()
    import os

    from_env = os.environ.get(profile.model_env_var, "").strip()
    return from_env or profile.default_model


# ---------------------------------------------------------------------------
# the shared request machinery
# ---------------------------------------------------------------------------

_JSON_INSTRUCTION = (
    " Answer with ONE JSON object and nothing else. Do not wrap it in prose, "
    "do not add markdown and do not invent fields beyond the requested ones. "
    "Every list you cannot support stays an empty list."
)


def _extract_object(text: str) -> dict[str, Any]:
    """The first JSON object in a provider answer, or fail closed."""
    if not isinstance(text, str) or not text.strip():
        raise DeliberationInvalidResponseError(
            "the provider returned no text at all"
        )
    candidate = text.strip()
    if candidate.startswith("```"):
        first = candidate.find("\n")
        if first != -1:
            candidate = candidate[first + 1 :]
        if candidate.rstrip().endswith("```"):
            candidate = candidate.rstrip()[:-3]
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end <= start:
        raise DeliberationInvalidResponseError(
            "the provider answer carried no JSON object"
        )
    try:
        parsed = json.loads(candidate[start : end + 1])
    except ValueError as error:
        raise DeliberationInvalidResponseError(
            "the provider answer was not valid JSON"
        ) from error
    if not isinstance(parsed, Mapping):
        raise DeliberationInvalidResponseError(
            "the provider answer was not a JSON object"
        )
    return dict(parsed)


def _prompt(
    *,
    role_instruction: str,
    payload: Mapping[str, Any],
    schema: Sequence[str],
) -> str:
    """One deterministic prompt: bounded JSON in, bounded JSON out."""
    return (
        f"{role_instruction}{_JSON_INSTRUCTION}\n\n"
        "INPUT (JSON):\n"
        + json.dumps(dict(payload), sort_keys=True, default=str)
        + "\n\nREQUIRED JSON KEYS:\n"
        + ", ".join(schema)
    )


# ---------------------------------------------------------------------------
# the provider client both adapters share
# ---------------------------------------------------------------------------

class _DeliberationClient:
    """One provider, one model, one credential and one bounded retry policy.

    It owns the request/response dialect, the retry classification and the cost
    telemetry. It holds no storage, no repository and no transaction boundary, and
    it never renders a credential: a failure is described by its type name or by a
    provider-neutral status word.
    """

    def __init__(
        self,
        provider: str,
        model: str = "",
        credential: Optional[str] = None,
        *,
        role: str = DELIBERATION_AGENT_ROLE,
        project: Optional[str] = None,
        cost_sink: Optional[CostPort] = None,
        clock: Callable[[], datetime] = utc_now,
        transport: Optional[Callable[[HttpRequest], HttpResponse]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff: Sequence[float] = DEFAULT_BACKOFF_SCHEDULE,
        max_output_tokens: int = DEFAULT_DELIBERATION_MAX_OUTPUT_TOKENS,
        price: Optional[ModelPrice] = None,
    ) -> None:
        import os
        import time

        self._profile = profile_for(provider)
        self._provider = self._profile.provider
        self._model = resolve_deliberation_model(provider, model)
        configured = (
            credential
            if credential is not None
            else os.environ.get(self._profile.key_env_var, "")
        )
        self._credential = str(configured or "")
        self._role = role
        self._project = project
        self._cost_sink = cost_sink
        self._clock = clock
        self._transport = transport or urllib_transport
        self._sleep = sleep or time.sleep
        self._timeout = float(timeout)
        self._max_retries = int(max_retries)
        self._backoff = tuple(backoff)
        self._max_output_tokens = int(max_output_tokens)
        self._price = price or UNKNOWN_PRICE

    @property
    def provider(self) -> str:
        """The provider id."""
        return self._provider

    @property
    def model(self) -> str:
        """The resolved model name."""
        return self._model

    @property
    def role(self) -> str:
        """The seat's role name (never a credential, never a prompt)."""
        return self._role

    @property
    def is_configured(self) -> bool:
        """Whether a credential is available for this provider."""
        return bool(self._credential)

    # -- the request -------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        if not self._credential:
            raise DeliberationMissingApiKeyError(
                f"no credential is configured for provider {self._provider!r}"
            )
        if self._profile.style == "messages":
            return {
                "content-type": "application/json",
                "x-api-key": self._credential,
                "anthropic-version": CLAUDE_API_VERSION,
            }
        return {
            "content-type": "application/json",
            "authorization": f"Bearer {self._credential}",
        }

    def _body(self, prompt: str) -> bytes:
        limit = min(self._max_output_tokens, self._profile.max_output_tokens)
        if self._profile.style == "messages":
            payload: dict[str, Any] = {
                "model": self._model,
                "max_tokens": limit,
                "messages": [{"role": "user", "content": prompt}],
            }
        else:
            payload = {
                "model": self._model,
                "max_tokens": limit,
                "temperature": DEFAULT_DELIBERATION_TEMPERATURE,
                "messages": [{"role": "user", "content": prompt}],
            }
        return json.dumps(payload).encode("utf-8")

    def _text(self, payload: Mapping[str, Any]) -> str:
        if self._profile.style == "messages":
            blocks = payload.get("content") or ()
            if isinstance(blocks, Sequence):
                for block in blocks:
                    if isinstance(block, Mapping) and isinstance(
                        block.get("text"), str
                    ):
                        return block["text"]
            raise DeliberationInvalidResponseError(
                "the provider answer carried no text block"
            )
        choices = payload.get("choices") or ()
        if isinstance(choices, Sequence) and choices:
            first = choices[0]
            if isinstance(first, Mapping):
                message = first.get("message")
                if isinstance(message, Mapping) and isinstance(
                    message.get("content"), str
                ):
                    return message["content"]
        raise DeliberationInvalidResponseError(
            "the provider answer carried no message content"
        )

    def _usage(self, payload: Mapping[str, Any]) -> tuple[int, int]:
        usage = payload.get("usage")
        if not isinstance(usage, Mapping):
            return 0, 0

        def number(name: str) -> int:
            value = usage.get(name, 0)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
            return 0

        return (
            number("prompt_tokens") or number("input_tokens"),
            number("completion_tokens") or number("output_tokens"),
        )

    def _decode(self, response: HttpResponse) -> Mapping[str, Any]:
        try:
            parsed = json.loads(response.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            raise DeliberationInvalidResponseError(
                "the provider response was not valid JSON"
            ) from error
        if not isinstance(parsed, Mapping):
            raise DeliberationInvalidResponseError(
                "the provider response was not a JSON object"
            )
        return parsed

    def request(self, prompt: str) -> HttpResponse:
        """One request with at most ``max_retries`` bounded retries.

        Retry is limited to transient failures: a transport error, a rate limit or
        a 5xx. An authentication failure, a model error and one malformed answer
        are **never** retried - the operator decides.
        """
        attempt = 0
        while True:
            try:
                response = self._transport(
                    HttpRequest(
                        url=self._profile.endpoint,
                        body=self._body(prompt),
                        headers=self._headers(),
                        timeout=self._timeout,
                    )
                )
            except HttpTransportError as error:
                if attempt >= self._max_retries:
                    raise DeliberationTransportError(
                        "the provider could not be reached"
                    ) from error
            else:
                if 200 <= response.status_code < 300:
                    return response
                if response.status_code == 429:
                    if attempt >= self._max_retries:
                        raise DeliberationRateLimitError(
                            "the provider rate-limited the request"
                        )
                elif is_retryable_status(response.status_code):
                    if attempt >= self._max_retries:
                        raise DeliberationHttpError(
                            "the provider returned a server error"
                        )
                elif response.status_code in (401, 403):
                    raise DeliberationMissingApiKeyError(
                        "the provider rejected the configured credential"
                    )
                else:
                    raise DeliberationHttpError(
                        "the provider refused the request (permanent status)"
                    )
            self._sleep(self._backoff[min(attempt, len(self._backoff) - 1)])
            attempt += 1

    def ask(self, prompt: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """One complete call: the request, the telemetry, the JSON object.

        Returns ``(content, cost)`` where ``cost`` is the provider-reported figure
        (possibly ``{}`` for "unavailable") - never a fabricated zero.
        """
        response = self.request(prompt)
        payload = self._decode(response)
        content = _extract_object(self._text(payload))
        cost = self.cost_of(payload)
        self._record_cost(payload)
        return content, cost

    def _priced(self) -> bool:
        """Whether a configured price makes a figure meaningful."""
        if self._price == UNKNOWN_PRICE:
            return False
        return bool(self._price.prompt_per_1k or self._price.completion_per_1k)

    def _amount(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens / 1000.0 * self._price.prompt_per_1k
            + output_tokens / 1000.0 * self._price.completion_per_1k
        )

    def cost_of(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """The per-call cost the stage records, or ``{}`` when none is known.

        A missing price keeps ``available`` false and ``total_usd`` ``None`` - a
        token count is never presented as a dollar figure that was not computed.
        """
        input_tokens, output_tokens = self._usage(payload)
        priced = self._priced()
        if not priced and not input_tokens and not output_tokens:
            return {}
        return {
            "available": bool(priced),
            "pricing_known": bool(priced),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_usd": (
                round(self._amount(input_tokens, output_tokens), 6)
                if priced
                else None
            ),
        }

    def _record_cost(self, payload: Mapping[str, Any]) -> None:
        """Record what the provider reported; never invent a figure."""
        if self._cost_sink is None:
            return
        input_tokens, output_tokens = self._usage(payload)
        priced = self._priced()
        event_id = f"{self._provider}:{self._model}:{input_tokens}:{output_tokens}"
        try:
            self._cost_sink.record(
                CostRecord(
                    provider=self._provider,
                    event_id=event_id,
                    model=self._model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=(
                        round(self._amount(input_tokens, output_tokens), 6)
                        if priced
                        else 0.0
                    ),
                    pricing_known=bool(priced),
                    project=self._project,
                    created_at=self._clock(),
                )
            )
        except Exception:  # noqa: BLE001 - telemetry never breaks a stage
            return

    def _test_connection(self) -> str:
        """One smallest safe authenticated call; a status word, never an error."""
        from ..ports.capabilities import ConnectionStatus

        if not self.is_configured:
            return ConnectionStatus.AUTH_ERROR.value
        prompt = 'Return exactly this JSON object and nothing else: {"ok": true}'
        try:
            response = self.request(prompt)
        except DeliberationMissingApiKeyError:
            return ConnectionStatus.AUTH_ERROR.value
        except DeliberationTransportError:
            return ConnectionStatus.NETWORK_ERROR.value
        except DeliberationAdapterError:
            return ConnectionStatus.PROVIDER_ERROR.value
        if response.status_code in (400, 404, 422):
            return ConnectionStatus.MODEL_ERROR.value
        try:
            payload = self._decode(response)
            _extract_object(self._text(payload))
        except DeliberationAdapterError:
            return ConnectionStatus.PROVIDER_ERROR.value
        return ConnectionStatus.CONNECTED.value


#: The JSON keys each stage asks for. They mirror the application's section
#: contracts (``ROUND1_SECTIONS``, ``LEAD_REVIEW_SECTIONS``,
#: ``SYNTHESIS_SECTIONS``): the infrastructure layer may not import the
#: application layer, so the vocabulary is restated here as the *request* side of
#: the same contract. A key the provider omits is stored as an empty list by the
#: use-case.
_ROUND1_KEYS: tuple[str, ...] = (
    "proposal_summary",
    "modules",
    "dependencies",
    "data_flows",
    "external_dependencies",
    "architecture_choices",
    "risks",
    "assumptions",
    "open_questions",
    "alternatives",
    "recommended_decisions",
    "evidence",
    "confidence",
)

_ROUND2_KEYS: tuple[str, ...] = (
    "decision",
    "revised_summary",
    "changed_decisions",
    "unchanged_decisions",
    "remaining_disagreements",
    "new_risks",
    "remaining_questions",
)

_LEAD_REVIEW_KEYS: tuple[str, ...] = (
    "agreements",
    "conflicts",
    "weak_assumptions",
    "missing_information",
    "risk_deltas",
    "questions_for_agent_a",
    "questions_for_agent_b",
    "common_questions",
    "recommendations_to_reconsider",
    "unresolved_questions",
)

_SYNTHESIS_KEYS: tuple[str, ...] = (
    "summary",
    "modules",
    "responsibilities",
    "dependencies",
    "boundaries",
    "data_flows",
    "external_dependencies",
    "architecture_decisions",
    "accepted_points",
    "rejected_alternatives",
    "remaining_disagreements",
    "risks",
    "adr_candidates",
    "implementation_phases",
    "unresolved_questions",
    "rationale",
)

_ARCHITECT_INSTRUCTION = (
    "You are one independent architect of a two-architect architecture review"
    " board. Analyse the operator's requirement and propose an architecture for"
    " the project the assistant manages. You have NOT seen any other architect's"
    " answer: reason entirely from the requirement and the context you were"
    " given, and state your assumptions and open questions honestly. Report only"
    " conclusions and evidence - never your private reasoning process."
)

_RECONSIDER_INSTRUCTION = (
    "You are one independent architect of a two-architect architecture review"
    " board. You are reviewing YOUR OWN proposal after a chair's structured"
    " criticism, and you can also see the other architect's claims. Reconsider"
    " your own work: keep it, revise it or withdraw it. Answer with the decision"
    " KEEP, REVISE or WITHDRAW and report precisely what changed."
)

_CHAIR_REVIEW_INSTRUCTION = (
    "You are the chair of a two-architect architecture review board. Two"
    " independent architects answered the same requirement. Compare their"
    " structured proposals and report agreements, contradictions, weak"
    " assumptions, missing information, risk changes and the questions each"
    " architect must answer. Do NOT vote, do not rank the architects and do not"
    " pick a winner - report the evidence and the trade-offs."
)

_CHAIR_SYNTHESIS_INSTRUCTION = (
    "You are the chair of a two-architect architecture review board. Synthesize"
    " the final advisory architecture from the two proposals, your own review and"
    " the two reconsiderations. Reason over evidence and trade-offs; never count"
    " votes and never claim a consensus that does not exist. Carry a remaining"
    " disagreement through explicitly."
)


class DeliberationAgentAdapter(_DeliberationClient):
    """One provider-backed architect: ``analyse`` (Round 1) and ``reconsider``."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("role", DELIBERATION_AGENT_ROLE)
        super().__init__(*args, **kwargs)

    def analyse(self, query: AgentAnalysisQuery) -> AgentAnalysisResult:
        """One independent Round-1 analysis."""
        prompt = _prompt(
            role_instruction=_ARCHITECT_INSTRUCTION,
            payload={
                "project": query.project,
                "requirement": query.requirement,
                "deliberation_id": query.deliberation_id,
                "seat": query.slot,
                "round": 1,
                "context": dict(query.context),
            },
            schema=_ROUND1_KEYS,
        )
        content, cost = self.ask(prompt)
        return AgentAnalysisResult(
            source=self.provider, content=content, cost=cost
        )

    def reconsider(self, query: AgentReconsiderQuery) -> AgentReconsiderResult:
        """One bounded Round-2 reconsideration of this seat's own proposal."""
        prompt = _prompt(
            role_instruction=_RECONSIDER_INSTRUCTION,
            payload={
                "project": query.project,
                "requirement": query.requirement,
                "deliberation_id": query.deliberation_id,
                "seat": query.slot,
                "round": 2,
                "your_round_1": dict(query.own_round1),
                "chair_critique_for_you": dict(query.lead_critique),
                "other_architect_claims": dict(query.peer_claims),
                "context": dict(query.context),
            },
            schema=_ROUND2_KEYS,
        )
        content, cost = self.ask(prompt)
        return AgentReconsiderResult(
            source=self.provider, content=content, cost=cost
        )


class DeliberationLeadAdapter(_DeliberationClient):
    """The provider-backed chair: ``review`` and ``synthesize``."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("role", DELIBERATION_LEAD_ROLE)
        super().__init__(*args, **kwargs)

    def review(self, query: LeadReviewQuery) -> LeadReviewResult:
        """The chair's structured review of both Round-1 proposals."""
        prompt = _prompt(
            role_instruction=_CHAIR_REVIEW_INSTRUCTION,
            payload={
                "project": query.project,
                "requirement": query.requirement,
                "deliberation_id": query.deliberation_id,
                "agent_a": dict(query.agent_a),
                "agent_b": dict(query.agent_b),
                "context": dict(query.context),
            },
            schema=_LEAD_REVIEW_KEYS,
        )
        content, cost = self.ask(prompt)
        return LeadReviewResult(source=self.provider, content=content, cost=cost)

    def synthesize(self, query: LeadSynthesisQuery) -> FinalSynthesisResult:
        """The chair's final synthesis of the exact five upstream results."""
        prompt = _prompt(
            role_instruction=_CHAIR_SYNTHESIS_INSTRUCTION,
            payload={
                "project": query.project,
                "requirement": query.requirement,
                "deliberation_id": query.deliberation_id,
                "agent_a_round_1": dict(query.agent_a_round1),
                "agent_b_round_1": dict(query.agent_b_round1),
                "your_review": dict(query.lead_review),
                "agent_a_round_2": dict(query.agent_a_round2),
                "agent_b_round_2": dict(query.agent_b_round2),
                "context": dict(query.context),
            },
            schema=_SYNTHESIS_KEYS,
        )
        content, cost = self.ask(prompt)
        return FinalSynthesisResult(
            source=self.provider, content=content, cost=cost
        )

