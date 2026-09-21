"""Grok advisor adapter - the "Challenger / Alternative Framing" of the port.

The assistant already has two consultative perspectives: the OpenAI adapter
(Step 12) is the *Implementation Analyst* ("HOW to realize this?") and the Claude
adapter (Step 13) is the *Critical Reviewer / Risk Analyst* ("WHAT can break,
what is weak, what assumptions are risky?"). This adapter adds the third,
deliberately different one - the **Challenger / Alternative Framing**:

    **"ARE we solving the right problem? What alternative approach exists?"**

All three are *consultative* and none of them can decide anything:

* this adapter is **never** wired into ``decide_review``, the realization gate,
  the orchestrator or the architecture validator, so a model answer can never
  override a deterministic rule, a persisted baseline or a gate verdict;
* it returns a :class:`Finding <architecture_assistant.domain.models.Finding>` -
  never a :class:`Decision <architecture_assistant.domain.models.Decision>`, a
  version, a rule change or an ADR. There is **no** evidence merger, **no** judge,
  **no** majority vote and **no** decision engine here: the three advisors stay
  fully independent, they never vote, and deterministic architecture rules
  outrank every one of them;
* it must **fail closed**. A timeout, a broken HTTP status, a malformed model
  answer or an explicit abstention raises a specific error - it never invents an
  alternative to keep the loop moving.

The role is adapter metadata (the system-prompt persona), not a domain concept:
the finding carries ``source="grok"`` and the domain model is unchanged.

Wire format
-----------
Provider-specific facts live here and nowhere else: the xAI chat-completions
request/response shape (``messages`` in, ``choices[0].message.content`` and
``usage.prompt_tokens``/``completion_tokens`` out), the ``Authorization: Bearer``
header, the ``https://api.x.ai/v1/chat/completions`` endpoint and the
``XAI_API_KEY`` environment variable. xAI is OpenAI-compatible, so the shape looks
like the OpenAI adapter's - it is still this module's own code, never shared.

Everything mechanical is shared with the other provider adapters through the
private ``._http`` module (HTTP DTOs, ``urllib`` transport, transport error,
retry classification, redaction, primitive validators, the deterministic
finding-id helper and the price model). ``_http.py`` is deliberately NOT extended
for this step. Retry, abstain, cost and evidence semantics are identical to the
OpenAI and Claude adapters by design.

Model selection is *configuration, never architecture truth*: see
:func:`resolve_grok_model` for the documented resolution order.

Security / secrets
------------------
The API key is **never** hardcoded, never a default, never stored beyond
construction, never in the request body, never in the ``Finding``, never in a
``CostRecord``, never in ``repr``, never in an error message and never in a
report: it travels only in the ``Authorization`` header set here. Response and
exception text is redacted before it is embedded in an error, so even a
pathological provider echo cannot leak it.

Determinism
-----------
``temperature=0``, a canonically serialized payload, a deterministic finding id
(provider + model + step + claim + evidence - never the clock), a bounded,
jitter-free retry backoff and injectable ``clock``/``sleep``/``transport`` seams
make every behaviour reproducible offline, without a network or a real key.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any, Callable, Mapping, Optional, Sequence

from ..domain.enums import Severity
from ..domain.models import Finding, utc_now
from ..ports.capabilities import AdvisorQuery, CostPort, CostRecord
from ._http import (
    DEFAULT_BACKOFF_SCHEDULE,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    HTTP_TOO_MANY_REQUESTS,
    UNKNOWN_PRICE,
    HttpRequest,
    HttpResponse,
    HttpTransportError,
    ModelPrice,
    _cost_event_id,
    _excerpt,
    _finding_id,
    _redact,
    _require_int,
    _require_number,
    _require_optional_text,
    _require_text,
    is_retryable_status,
    urllib_transport,
)

__all__ = [
    "GROK_PROVIDER",
    "DEFAULT_GROK_MODEL",
    "GROK_MODEL_ENV_VAR",
    "CHALLENGER_ROLE",
    "GROK_CHAT_COMPLETIONS_URL",
    "XAI_API_KEY_ENV_VAR",
    "DEFAULT_GROK_MAX_OUTPUT_TOKENS",
    "resolve_grok_model",
    "GrokAdvisorError",
    "GrokMissingApiKeyError",
    "GrokAdvisorTransportError",
    "GrokAdvisorTimeoutError",
    "GrokAdvisorRateLimitError",
    "GrokAdvisorHttpError",
    "GrokAdvisorInvalidResponseError",
    "GrokAdvisorAbstainError",
    "GrokAdvisorAdapter",
]

#: The provider id stored in ``Finding.source`` and ``CostRecord.provider``.
GROK_PROVIDER = "grok"

#: The single documented adapter default - used only when neither the
#: constructor nor the environment names a model. A model name is configuration,
#: never a statement about price, availability or architecture.
DEFAULT_GROK_MODEL = "grok-4.6"

#: Optional environment variable naming the model (configuration, not a rule).
GROK_MODEL_ENV_VAR = "GROK_MODEL"

#: The advisor persona: a challenger that reframes the problem - not an analyst
#: and not a critic. Adapter metadata, not a domain concept.
CHALLENGER_ROLE = "Challenger / Alternative Framing"

#: xAI chat-completions endpoint - the only Grok URL this codebase knows.
GROK_CHAT_COMPLETIONS_URL = "https://api.x.ai/v1/chat/completions"

#: The environment variable the key is read from when none is injected.
XAI_API_KEY_ENV_VAR = "XAI_API_KEY"

#: Grok's own output budget for one challenge (request shaping, not shared).
DEFAULT_GROK_MAX_OUTPUT_TOKENS = 1024

#: Used when the model abstains without putting anything in ``reason``.
_ABSTAIN_REASON_FALLBACK = "the model abstained without stating a reason"

_CONTRACT_STATUS_OK = "OK"
_CONTRACT_STATUS_ABSTAIN = "ABSTAIN"


def resolve_grok_model(model: Optional[str] = None) -> str:
    """Resolve the Grok model through one documented order.

    A provider model name is **configuration, never architecture truth**, so it
    is never hardcoded into a rule or a contract:

    1. the explicit ``model`` argument (constructor / composition config), when
       non-blank;
    2. the optional :data:`GROK_MODEL_ENV_VAR` environment variable, when
       non-blank;
    3. :data:`DEFAULT_GROK_MODEL` - the one documented adapter default.

    Nothing here checks that the model exists. The assistant must compose and run
    fully offline, so an unknown or retired model name is a provider-side failure
    later, never a start-up failure now.
    """
    if model is not None and str(model).strip():
        return str(model).strip()
    from_env = os.environ.get(GROK_MODEL_ENV_VAR, "")
    if from_env.strip():
        return from_env.strip()
    return DEFAULT_GROK_MODEL


class GrokAdvisorError(Exception):
    """Base class for every Grok advisor failure."""


class GrokMissingApiKeyError(GrokAdvisorError):
    """Raised when neither the argument nor the environment provides a key."""


class GrokAdvisorTransportError(GrokAdvisorError):
    """The request could not be delivered (retries used up)."""


class GrokAdvisorTimeoutError(GrokAdvisorTransportError):
    """The request timed out (retries used up)."""


class GrokAdvisorRateLimitError(GrokAdvisorError):
    """HTTP 429 (retries used up)."""


class GrokAdvisorHttpError(GrokAdvisorError):
    """A non-retryable status - or a retryable one that never recovered."""


class GrokAdvisorInvalidResponseError(GrokAdvisorError):
    """The answer violated the structured output contract.

    This is the *defect* path: the model returned something that is not the
    agreed json object, or an ``OK`` answer without the claim/evidence the
    contract requires. It is deliberately distinct from
    :class:`GrokAdvisorAbstainError` - a broken contract must never be reported
    as a deliberate refusal.
    """


class GrokAdvisorAbstainError(GrokAdvisorError):
    """The model explicitly abstained or refused to answer.

    A legitimate outcome, not a defect: the surrounding system decides what an
    abstention means; the adapter only reports it faithfully, with the model's
    own reason (or a documented fallback when it gave none).
    """

    def __init__(self, reason: str) -> None:
        text = (
            reason.strip()
            if isinstance(reason, str) and reason.strip()
            else _ABSTAIN_REASON_FALLBACK
        )
        super().__init__(f"Grok advisor abstained: {text}")
        self.reason = text


class GrokAdvisorAdapter:
    """Grok implementation of the advisor port (Challenger / Alternative Framing).

    The adapter is *only* a capability: constructing it opens no connection, reads
    no key and changes no state, so the composition root may always build it and a
    project without ``XAI_API_KEY`` simply never calls :meth:`advise`. The
    deterministic gate is entirely unaffected either way.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        url: str = GROK_CHAT_COMPLETIONS_URL,
        model: Optional[str] = None,
        role: str = CHALLENGER_ROLE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_schedule: Sequence[float] = DEFAULT_BACKOFF_SCHEDULE,
        max_output_tokens: int = DEFAULT_GROK_MAX_OUTPUT_TOKENS,
        transport: Optional[Callable[[HttpRequest], HttpResponse]] = None,
        cost_sink: Optional[CostPort] = None,
        pricing: Optional[Mapping[str, ModelPrice]] = None,
        project: Optional[str] = None,
        clock: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], None] = time.sleep,
        on_telemetry_error: Optional[Callable[[BaseException], None]] = None,
    ) -> None:
        if api_key is not None and not isinstance(api_key, str):
            raise ValueError(f"api_key must be a string or None; got {api_key!r}")
        # An empty or blank key is "not provided": it never counts as configured.
        self._api_key: Optional[str] = (
            api_key if isinstance(api_key, str) and api_key.strip() else None
        )
        self._url = _require_text(url, "url")
        # config -> environment -> documented default; never architecture truth
        self._model = _require_text(resolve_grok_model(model), "model")
        self._role = _require_text(role, "role")
        self._timeout = _require_number(
            timeout_seconds, "timeout_seconds", minimum=1e-6
        )
        self._max_retries = _require_int(max_retries, "max_retries", minimum=0)
        if isinstance(backoff_schedule, (str, bytes)) or not isinstance(
            backoff_schedule, Sequence
        ):
            raise ValueError("backoff_schedule must be a sequence of numbers")
        delays = tuple(
            _require_number(delay, "backoff_schedule entry", minimum=0.0)
            for delay in backoff_schedule
        )
        if not delays:
            raise ValueError("backoff_schedule must contain at least one delay")
        self._backoff = delays
        self._max_output_tokens = _require_int(
            max_output_tokens, "max_output_tokens", minimum=1
        )
        if transport is not None and not callable(transport):
            raise ValueError("transport must be a callable or None")
        self._transport: Callable[[HttpRequest], HttpResponse] = (
            transport if transport is not None else urllib_transport
        )
        if cost_sink is not None and not callable(
            getattr(cost_sink, "record", None)
        ):
            raise ValueError("cost_sink must implement record(CostRecord)")
        self._cost_sink = cost_sink
        if pricing is not None and not isinstance(pricing, Mapping):
            raise ValueError("pricing must be a mapping of model -> ModelPrice")
        for name, price in dict(pricing or {}).items():
            if not isinstance(price, ModelPrice):
                raise ValueError(
                    f"pricing[{name!r}] must be a ModelPrice; got {price!r}"
                )
        self._pricing: dict[str, ModelPrice] = dict(pricing or {})
        self._project = _require_optional_text(project, "project")
        if not callable(clock):
            raise ValueError("clock must be a callable returning a datetime")
        self._clock = clock
        if not callable(sleep):
            raise ValueError("sleep must be a callable taking seconds")
        self._sleep = sleep
        if on_telemetry_error is not None and not callable(on_telemetry_error):
            raise ValueError("on_telemetry_error must be a callable or None")
        self._on_telemetry_error = on_telemetry_error
        self._last_telemetry_error: Optional[BaseException] = None

    # -- read-only identity ------------------------------------------------
    @property
    def provider(self) -> str:
        """The provider id this adapter reports as ``Finding.source``."""
        return GROK_PROVIDER

    @property
    def model(self) -> str:
        """The resolved model asked for the challenge."""
        return self._model

    @property
    def role(self) -> str:
        """The advisor persona - adapter metadata, not a domain field."""
        return self._role

    @property
    def is_configured(self) -> bool:
        """Whether an API key is available *now* (argument or environment).

        Checked lazily, so an environment key added after composition still
        works and a project without a key can run the whole deterministic system.
        The key itself is never stored beyond construction and never returned.
        """
        return bool(self._api_key) or bool(
            os.environ.get(XAI_API_KEY_ENV_VAR, "").strip()
        )

    @property
    def last_telemetry_error(self) -> Optional[BaseException]:
        """The last non-fatal cost-telemetry failure, if any.

        Cost reporting is a *secondary side effect*: when it fails the advisor
        still returns its finding, and the failure is surfaced here (and to the
        optional ``on_telemetry_error`` hook) instead of being raised.
        """
        return self._last_telemetry_error

    def __repr__(self) -> str:
        """Deliberately key-free - the API key must never be rendered."""
        return (
            f"{type(self).__name__}(provider={GROK_PROVIDER!r}, "
            f"model={self._model!r}, role={self._role!r}, "
            f"configured={self.is_configured})"
        )

    # -- AdvisorPort -------------------------------------------------------
    def advise(self, query: AdvisorQuery) -> Finding:
        """Answer one reframing question with an evidence-based finding.

        Raises instead of guessing: a missing key, an unrecoverable transport or
        status failure, a broken output contract, or an explicit abstention each
        raise their own error. Cost telemetry is recorded on the side and can
        never turn a valid finding into a failure.
        """
        if not isinstance(query, AdvisorQuery):
            raise ValueError(f"query must be an AdvisorQuery; got {query!r}")
        self._last_telemetry_error = None
        api_key = self._resolve_api_key()
        payload = self._build_payload(query)
        response = self._post(payload, api_key)
        envelope = self._parse_envelope(response, api_key)
        content = self._content_of(envelope, api_key)
        # telemetry BEFORE the contract check: the call was paid for either way,
        # and a broken contract still must not lose the cost record
        self._record_cost(envelope, query)
        contract = self._parse_contract(content, api_key)
        return self._build_finding(contract, query)

    # -- secrets -----------------------------------------------------------
    def _resolve_api_key(self) -> str:
        """The key for this call: argument first, environment second."""
        if self._api_key:
            return self._api_key
        from_env = os.environ.get(XAI_API_KEY_ENV_VAR, "")
        if from_env.strip():
            return from_env
        raise GrokMissingApiKeyError(
            "no xAI API key available: pass api_key=... or set the "
            f"{XAI_API_KEY_ENV_VAR} environment variable"
        )

    # -- request -----------------------------------------------------------
    def _build_payload(self, query: AdvisorQuery) -> bytes:
        """The exact request body - serialized once, reused for every attempt.

        The xAI chat-completions shape (OpenAI-compatible): a ``system`` message,
        a ``user`` message and an explicit ``json_object`` response format, since
        the structured contract is what the rest of the system consumes. Canonical
        JSON (sorted keys, compact separators) keeps the bytes stable, which is
        what makes an identical retry payload testable.
        """
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": self._user_prompt(query)},
            ],
            "temperature": 0,
            "max_tokens": self._max_output_tokens,
            "response_format": {"type": "json_object"},
        }
        return json.dumps(
            body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    def _system_prompt(self) -> str:
        """The role, the output contract and the rules - deterministic text."""
        return (
            f'You are the "{self._role}" advisor of a deterministic architecture '
            "lifecycle assistant. The assistant's architecture rules, validators "
            "and gates always outrank you: never suggest bypassing them, never "
            "claim authority over them and never state that a rule may be "
            "ignored.\n"
            "Your perspective is deliberately different from an implementation "
            "analyst and from a critic: challenge the framing. Ask ARE we solving "
            "the right problem, and name what alternative approach exists.\n"
            "Answer the reframing question with ONE json object (RFC 8259) and "
            "nothing else, matching exactly:\n"
            '{"status":"OK"|"ABSTAIN",'
            '"claim":"<one alternative framing or a challenged assumption>",'
            '"evidence":["<concrete, checkable locator>"],'
            '"confidence":<number 0.0-1.0>,'
            '"severity":"LOW"|"MEDIUM"|"HIGH"|"CRITICAL",'
            '"reason":"<why you abstained; empty for OK>"}\n'
            "Rules:\n"
            '- "OK" requires a non-empty claim AND at least one evidence entry;\n'
            "- every evidence entry must be a concrete, checkable locator "
            "(module:line, rule id, symbol, file path or document citation) - a "
            "provocative idea without evidence is not a finding;\n"
            "- propose ONE alternative or challenge, never a competing decision: "
            "you cannot overrule a rule, a baseline or a gate verdict;\n"
            '- use "ABSTAIN" when the given context is insufficient or you '
            'refuse to answer, and explain it in "reason".'
        )

    def _user_prompt(self, query: AdvisorQuery) -> str:
        """The question plus its canonically serialized context."""
        context = json.dumps(
            dict(query.context),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        step = "unknown" if query.step_no is None else str(query.step_no)
        return (
            "Question (ARE we solving the right problem? What alternative "
            "approach exists?):\n"
            f"{query.question}\n"
            f"Step: {step}\n"
            "Context (json):\n"
            f"{context}"
        )

    # -- transport + retry policy ------------------------------------------
    def _post(self, payload: bytes, api_key: str) -> HttpResponse:
        """Send the request, retrying **only** the retryable failures.

        Retried: a transport timeout, HTTP 429 and HTTP 5xx. Never retried:
        permanent 4xx, a malformed model answer, a contract violation and an
        abstention. The same :class:`HttpRequest` object - hence the same payload
        bytes and headers - is used for every attempt.
        """
        request = HttpRequest(
            url=self._url,
            body=payload,
            headers={
                # the only place the key ever appears
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
            timeout=self._timeout,
        )
        attempts = self._max_retries + 1
        failure: Optional[GrokAdvisorError] = None
        for attempt in range(attempts):
            try:
                response = self._transport(request)
            except HttpTransportError as error:
                if not error.retryable:
                    raise GrokAdvisorTransportError(
                        _redact(str(error), api_key)
                    ) from error
                failure = (
                    GrokAdvisorTimeoutError(
                        f"Grok request timed out after {self._timeout}s"
                    )
                    if error.timed_out
                    else GrokAdvisorTransportError(
                        _redact(str(error), api_key)
                    )
                )
            else:
                status = response.status_code
                if 200 <= status < 300:
                    return response
                if not is_retryable_status(status):
                    # permanent: a bad key, a bad request, a missing model, ...
                    raise GrokAdvisorHttpError(
                        self._status_message(status, response, api_key)
                    )
                if status == HTTP_TOO_MANY_REQUESTS:
                    failure = GrokAdvisorRateLimitError(
                        self._status_message(status, response, api_key)
                    )
                else:
                    failure = GrokAdvisorHttpError(
                        self._status_message(status, response, api_key)
                    )
            if attempt + 1 < attempts:
                self._sleep(self._delay_for(attempt))
        if failure is None:  # pragma: no cover - unreachable by construction
            raise GrokAdvisorError("Grok request failed without a cause")
        raise failure

    def _delay_for(self, attempt: int) -> float:
        """Bounded deterministic backoff - no jitter, no randomness."""
        return self._backoff[min(attempt, len(self._backoff) - 1)]

    @staticmethod
    def _status_message(
        status: int, response: HttpResponse, secret: Optional[str]
    ) -> str:
        """A short status message - the provider text is redacted first."""
        detail = _excerpt(_redact(response.text(), secret))
        suffix = f": {detail}" if detail else ""
        return f"Grok request failed with HTTP {status}{suffix}"

    # -- response parsing ---------------------------------------------------
    def _parse_envelope(
        self, response: HttpResponse, secret: Optional[str]
    ) -> dict:
        """The chat-completions envelope as a JSON object."""
        try:
            data = response.json()
        except ValueError as error:
            raise GrokAdvisorInvalidResponseError(
                "Grok returned a body that is not JSON: "
                + _excerpt(_redact(response.text(), secret))
            ) from error
        if not isinstance(data, dict):
            raise GrokAdvisorInvalidResponseError(
                "Grok response must be a JSON object; got "
                f"{type(data).__name__}"
            )
        return data

    @staticmethod
    def _content_of(
        envelope: Mapping[str, Any], secret: Optional[str]
    ) -> str:
        """The assistant message text from ``choices[0]`` - never redacted.

        This is deliberately provider-specific and NOT shared: the xAI
        chat-completions shape puts the answer at
        ``choices[0].message.content``, and no usable content there is a contract
        defect rather than a silently empty finding.
        """
        choices = envelope.get("choices")
        if not isinstance(choices, list) or not choices:
            raise GrokAdvisorInvalidResponseError(
                "Grok response carries no choices: "
                + _excerpt(_redact(json.dumps(envelope, default=str), secret))
            )
        first = choices[0]
        message = first.get("message") if isinstance(first, Mapping) else None
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, str) or not content.strip():
            raise GrokAdvisorInvalidResponseError(
                "Grok response carries no assistant content"
            )
        return content

    def _parse_contract(
        self, content: str, secret: Optional[str]
    ) -> dict:
        """The advisor contract object - strictly JSON, strictly an object."""
        text = content.strip()
        try:
            contract = json.loads(text)
        except ValueError as error:
            raise GrokAdvisorInvalidResponseError(
                "advisor answer is not JSON: "
                + _excerpt(_redact(text, secret))
            ) from error
        if not isinstance(contract, dict):
            raise GrokAdvisorInvalidResponseError(
                "advisor answer must be a JSON object; got "
                f"{type(contract).__name__}"
            )
        return contract

    # -- contract -> domain Finding ----------------------------------------
    def _build_finding(
        self, contract: Mapping[str, Any], query: AdvisorQuery
    ) -> Finding:
        """Map a validated contract object onto a domain :class:`Finding`.

        The abstention check comes first on purpose: an ``ABSTAIN`` answer is a
        deliberate refusal and must be reported as
        :class:`GrokAdvisorAbstainError`, never as a broken contract - even when
        it carries no claim. A missing or unknown ``status``, by contrast, is a
        contract violation, because nothing announced a refusal.

        The finding stays provider-neutral: only ``source`` names the provider,
        while ``claim``/``evidence``/``confidence``/``severity`` are exactly the
        domain fields every advisor fills. The Challenger persona changes the
        *question* the model answers, never the shape of the answer - and the
        finding is one independent opinion, never a vote.
        """
        status = self._status_of(contract)
        if status == _CONTRACT_STATUS_ABSTAIN:
            raise GrokAdvisorAbstainError(self._abstain_reason(contract))
        claim = self._claim_of(contract)
        evidence = self._evidence_of(contract)
        confidence = self._confidence_of(contract)
        severity = self._severity_of(contract)
        return Finding(
            id=_finding_id(
                GROK_PROVIDER, self._model, query.step_no, claim, evidence
            ),
            source=GROK_PROVIDER,
            claim=claim,
            evidence=evidence,
            confidence=confidence,
            severity=severity,
            step_no=query.step_no,
            created_at=self._clock(),
        )

    @staticmethod
    def _status_of(contract: Mapping[str, Any]) -> str:
        """The declared outcome token, normalized but strictly validated."""
        raw = contract.get("status")
        token = raw.strip().upper() if isinstance(raw, str) else ""
        if token in (_CONTRACT_STATUS_OK, _CONTRACT_STATUS_ABSTAIN):
            return token
        raise GrokAdvisorInvalidResponseError(
            'advisor answer field "status" must be "OK" or "ABSTAIN"; got '
            f"{_excerpt(str(raw), 40)!r} - a missing or unknown status is a "
            "contract violation, not an abstention"
        )

    @staticmethod
    def _abstain_reason(contract: Mapping[str, Any]) -> str:
        reason = contract.get("reason")
        if isinstance(reason, str) and reason.strip():
            return reason.strip()
        return _ABSTAIN_REASON_FALLBACK

    @staticmethod
    def _claim_of(contract: Mapping[str, Any]) -> str:
        claim = contract.get("claim")
        if not isinstance(claim, str) or not claim.strip():
            raise GrokAdvisorInvalidResponseError(
                'advisor answer field "claim" must be a non-empty string on an '
                '"OK" answer'
            )
        return claim.strip()

    @staticmethod
    def _evidence_of(contract: Mapping[str, Any]) -> tuple[str, ...]:
        """The evidence locators - at least one, each a non-empty string."""
        evidence = contract.get("evidence")
        if isinstance(evidence, (str, bytes)) or not isinstance(
            evidence, (list, tuple)
        ):
            raise GrokAdvisorInvalidResponseError(
                'advisor answer field "evidence" must be a list of locators'
            )
        entries: list[str] = []
        for item in evidence:
            if not isinstance(item, str) or not item.strip():
                raise GrokAdvisorInvalidResponseError(
                    'every "evidence" entry must be a non-empty string'
                )
            entries.append(item.strip())
        if not entries:
            raise GrokAdvisorInvalidResponseError(
                'advisor answer field "evidence" must contain at least one '
                'locator on an "OK" answer - a provocative idea without evidence '
                "is not a finding"
            )
        return tuple(entries)

    @staticmethod
    def _confidence_of(contract: Mapping[str, Any]) -> float:
        confidence = contract.get("confidence")
        if isinstance(confidence, bool) or not isinstance(
            confidence, (int, float)
        ):
            raise GrokAdvisorInvalidResponseError(
                'advisor answer field "confidence" must be a number between 0.0 '
                "and 1.0"
            )
        value = float(confidence)
        if not 0.0 <= value <= 1.0:
            raise GrokAdvisorInvalidResponseError(
                'advisor answer field "confidence" must be between 0.0 and 1.0; '
                f"got {value!r}"
            )
        return value

    @staticmethod
    def _severity_of(contract: Mapping[str, Any]) -> Severity:
        raw = contract.get("severity")
        token = raw.strip().upper() if isinstance(raw, str) else raw
        try:
            return Severity(token)
        except ValueError as error:
            valid = ", ".join(member.value for member in Severity)
            raise GrokAdvisorInvalidResponseError(
                f'advisor answer field "severity" must be one of ({valid}); got '
                f"{_excerpt(str(raw), 40)!r}"
            ) from error

    # -- cost telemetry (secondary, never fatal) ---------------------------
    def _record_cost(
        self, envelope: Mapping[str, Any], query: AdvisorQuery
    ) -> None:
        """Record cost telemetry for a billable answer - never turn success into
        failure.

        Only ever called for a real 2xx API answer: a failed attempt (timeout,
        429, 5xx) carries no ``usage`` and produces no record, so a retry that
        finally succeeds contributes exactly one record. If the provider sends no
        usage, nothing is recorded - this adapter never invents token counts.

        The record is best-effort by design: a failing ``CostPort`` is surfaced
        through ``last_telemetry_error``, never raised, so a valid finding is
        never lost because telemetry broke. The API key is not part of a record.

        ``event_id`` is the provider-native response id, the stable identity of
        this one logical billable event: a redelivery or a repeated ``record()``
        of the same response is recognised instead of counted twice, while two
        genuine calls stay two events. A 2xx that carries usage but no response
        id records nothing - the adapter raises ``CostIdentityUnavailableError``
        into its own telemetry path rather than inventing an identity from the
        clock or the request body.
        """
        usage = self._usage_of(envelope)
        if usage is None or self._cost_sink is None:
            return
        input_tokens, output_tokens = usage
        try:
            self._cost_sink.record(
                CostRecord(
                    provider=GROK_PROVIDER,
                    event_id=_cost_event_id(GROK_PROVIDER, envelope),
                    model=self._model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=self._price_for(self._model).cost(
                        input_tokens, output_tokens
                    ),
                    pricing_known=self._model in self._pricing,
                    project=self._project,
                    step_no=query.step_no,
                    created_at=self._clock(),
                )
            )
        except Exception as error:  # telemetry is secondary by definition
            self._last_telemetry_error = error
            self._notify_telemetry_error(error)

    def _notify_telemetry_error(self, error: Exception) -> None:
        hook = self._on_telemetry_error
        if hook is None:
            return
        try:
            hook(error)
        except Exception:  # a failing hook must not fail the advisor either
            return

    @staticmethod
    def _usage_of(envelope: Mapping[str, Any]) -> Optional[tuple[int, int]]:
        """The xAI token counts, or ``None`` when they are unusable.

        Provider-specific and NOT shared through ``._http``: the xAI
        chat-completions shape reports ``prompt_tokens``/``completion_tokens``,
        and the mapping to the neutral ``(input, output)`` tuple belongs to this
        adapter.
        """
        usage = envelope.get("usage")
        if not isinstance(usage, Mapping):
            return None
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        for value in (prompt, completion):
            if isinstance(value, bool) or not isinstance(value, int):
                return None
            if value < 0:
                return None
        return int(prompt), int(completion)

    def _price_for(self, model: str) -> ModelPrice:
        """The injected price, or the documented unknown-price fallback."""
        return self._pricing.get(model, UNKNOWN_PRICE)
