"""Claude advisor adapter - the "Critical Reviewer / Risk Analyst" of the port.

The OpenAI adapter (Step 12) is the *Implementation Analyst*: it answers *"HOW
to realize this?"*. This adapter is the second, independent perspective - the
**Critical Reviewer / Risk Analyst** - and it answers a different question:

    **"WHAT can break, what is weak, what assumptions are risky?"**

Both perspectives are *consultative* and neither can decide anything:

* this adapter is **never** wired into ``decide_review``, the realization gate,
  the orchestrator or the architecture validator, so a model answer can never
  override a deterministic rule, a persisted baseline or a gate verdict;
* it returns a :class:`Finding <architecture_assistant.domain.models.Finding>` -
  never a :class:`Decision <architecture_assistant.domain.models.Decision>`, a
  version, a rule change or an ADR. There is no judge, no majority vote and no
  decision engine here: deterministic architecture rules outrank every advisor;
* it must **fail closed**. A timeout, a broken HTTP status, a malformed model
  answer or an explicit abstention raises a specific error - it never invents a
  risk to keep the loop moving.

The role is adapter metadata (the system-prompt persona), not a domain concept:
the finding carries ``source="claude"`` and the domain model is unchanged.

Wire format
-----------
Provider-specific facts live here and nowhere else: the Anthropic Messages
request/response shape (``system`` + ``messages`` in, ``content[0].text`` and
``usage.input_tokens``/``usage.output_tokens`` out), the ``x-api-key`` header
with ``anthropic-version``, and the ``ANTHROPIC_API_KEY`` environment variable.

Everything mechanical is shared with the other provider adapters through the
private ``._http`` module (HTTP DTOs, ``urllib`` transport, transport error,
retry classification, redaction, primitive validators, the deterministic
finding-id helper and the price model). Retry, abstain, cost and evidence
semantics are identical to the OpenAI adapter by design.

Model selection is *configuration, never architecture truth*: see
:func:`resolve_claude_model` for the documented resolution order.

Security / secrets
------------------
The API key is **never** hardcoded, never a default, never stored beyond
construction, never in the request body, never in the ``Finding``, never in a
``CostRecord``, never in ``repr`` and never in an error message: it travels only
in the ``x-api-key`` header. Response and exception text is redacted before it
is embedded in an error, so even a pathological provider echo cannot leak it.

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
    "CLAUDE_PROVIDER",
    "DEFAULT_CLAUDE_MODEL",
    "CLAUDE_MODEL_ENV_VAR",
    "CRITICAL_REVIEWER_ROLE",
    "CLAUDE_MESSAGES_URL",
    "CLAUDE_API_KEY_ENV_VAR",
    "CLAUDE_API_VERSION",
    "DEFAULT_CLAUDE_MAX_OUTPUT_TOKENS",
    "resolve_claude_model",
    "ClaudeAdvisorError",
    "ClaudeMissingApiKeyError",
    "ClaudeAdvisorTransportError",
    "ClaudeAdvisorTimeoutError",
    "ClaudeAdvisorRateLimitError",
    "ClaudeAdvisorHttpError",
    "ClaudeAdvisorInvalidResponseError",
    "ClaudeAdvisorAbstainError",
    "ClaudeAdvisorAdapter",
]

#: The provider id stored in ``Finding.source`` and ``CostRecord.provider``.
CLAUDE_PROVIDER = "claude"

#: The single documented adapter default - used only when neither the
#: constructor nor the environment names a model.
DEFAULT_CLAUDE_MODEL = "claude-3-5-haiku-latest"

#: Optional environment variable naming the model (configuration, not a rule).
CLAUDE_MODEL_ENV_VAR = "CLAUDE_MODEL"

#: The advisor persona: a risk/weakness reviewer, not an implementation analyst.
CRITICAL_REVIEWER_ROLE = "Critical Reviewer / Risk Analyst"

#: Anthropic Messages endpoint.
CLAUDE_MESSAGES_URL = "https://api.anthropic.com/v1/messages"

#: The environment variable the key is read from when none is injected.
CLAUDE_API_KEY_ENV_VAR = "ANTHROPIC_API_KEY"

#: Required ``anthropic-version`` header value (the documented API version).
CLAUDE_API_VERSION = "2023-06-01"

#: Claude Messages requires ``max_tokens``; this is the adapter's own budget.
DEFAULT_CLAUDE_MAX_OUTPUT_TOKENS = 1024

#: Used when the model abstains without putting anything in ``reason``.
_ABSTAIN_REASON_FALLBACK = "the model abstained without stating a reason"

_CONTRACT_STATUS_OK = "OK"
_CONTRACT_STATUS_ABSTAIN = "ABSTAIN"


def resolve_claude_model(model: Optional[str] = None) -> str:
    """Resolve the Claude model through one documented order.

    A provider model name is **configuration, never architecture truth**, so it
    is never hardcoded into a rule or a contract:

    1. the explicit ``model`` argument (constructor / composition config), when
       non-blank;
    2. the optional :data:`CLAUDE_MODEL_ENV_VAR` environment variable, when
       non-blank;
    3. :data:`DEFAULT_CLAUDE_MODEL` - the one documented adapter default.

    Nothing here checks that the model exists. The assistant must compose and run
    fully offline, so an unknown or retired model name is a provider-side failure
    later, never a start-up failure now.
    """
    if model is not None and str(model).strip():
        return str(model).strip()
    from_env = os.environ.get(CLAUDE_MODEL_ENV_VAR, "")
    if from_env.strip():
        return from_env.strip()
    return DEFAULT_CLAUDE_MODEL


class ClaudeAdvisorError(Exception):
    """Base class for every Claude advisor failure."""


class ClaudeMissingApiKeyError(ClaudeAdvisorError):
    """Raised when neither the argument nor the environment provides a key."""


class ClaudeAdvisorTransportError(ClaudeAdvisorError):
    """The request could not be delivered (retries used up)."""


class ClaudeAdvisorTimeoutError(ClaudeAdvisorTransportError):
    """The request timed out (retries used up)."""


class ClaudeAdvisorRateLimitError(ClaudeAdvisorError):
    """HTTP 429 (retries used up)."""


class ClaudeAdvisorHttpError(ClaudeAdvisorError):
    """A non-retryable status - or a retryable one that never recovered."""


class ClaudeAdvisorInvalidResponseError(ClaudeAdvisorError):
    """The answer violated the structured output contract.

    This is the *defect* path: the model returned something that is not the
    agreed json object, or an ``OK`` answer without the claim/evidence the
    contract requires. It is deliberately distinct from
    :class:`ClaudeAdvisorAbstainError` - a broken contract must never be
    reported as a deliberate refusal.
    """


class ClaudeAdvisorAbstainError(ClaudeAdvisorError):
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
        super().__init__(f"Claude advisor abstained: {text}")
        self.reason = text


class ClaudeAdvisorAdapter:
    """Claude implementation of the advisor port (Critical Reviewer).

    The adapter is *only* a capability: constructing it opens no connection, reads
    no key and changes no state, so the composition root may always build it and a
    project without ``ANTHROPIC_API_KEY`` simply never calls :meth:`advise`. The
    deterministic gate is entirely unaffected either way.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        url: str = CLAUDE_MESSAGES_URL,
        model: Optional[str] = None,
        role: str = CRITICAL_REVIEWER_ROLE,
        api_version: str = CLAUDE_API_VERSION,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_schedule: Sequence[float] = DEFAULT_BACKOFF_SCHEDULE,
        max_output_tokens: int = DEFAULT_CLAUDE_MAX_OUTPUT_TOKENS,
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
        self._model = _require_text(resolve_claude_model(model), "model")
        self._role = _require_text(role, "role")
        self._api_version = _require_text(api_version, "api_version")
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
        return CLAUDE_PROVIDER

    @property
    def model(self) -> str:
        """The resolved model asked for the review."""
        return self._model

    @property
    def role(self) -> str:
        """The advisor persona - adapter metadata, not a domain field."""
        return self._role

    @property
    def api_version(self) -> str:
        """The ``anthropic-version`` header value this adapter sends."""
        return self._api_version

    @property
    def is_configured(self) -> bool:
        """Whether an API key is available *now* (argument or environment).

        Checked lazily, so an environment key added after composition still
        works and a project without a key can run the whole deterministic system.
        The key itself is never stored beyond construction and never returned.
        """
        return bool(self._api_key) or bool(
            os.environ.get(CLAUDE_API_KEY_ENV_VAR, "").strip()
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
            f"{type(self).__name__}(provider={CLAUDE_PROVIDER!r}, "
            f"model={self._model!r}, role={self._role!r}, "
            f"configured={self.is_configured})"
        )

    # -- AdvisorPort -------------------------------------------------------
    def advise(self, query: AdvisorQuery) -> Finding:
        """Answer one review question with an evidence-based finding.

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
        from_env = os.environ.get(CLAUDE_API_KEY_ENV_VAR, "")
        if from_env.strip():
            return from_env
        raise ClaudeMissingApiKeyError(
            "no Claude API key available: pass api_key=... or set the "
            f"{CLAUDE_API_KEY_ENV_VAR} environment variable"
        )

    # -- request -----------------------------------------------------------
    def _build_payload(self, query: AdvisorQuery) -> bytes:
        """The exact request body - serialized once, reused for every attempt.

        The Anthropic Messages shape: ``system`` is a top-level field (not a
        message) and ``max_tokens`` is required. Canonical JSON (sorted keys,
        compact separators) keeps the bytes stable, which is what makes an
        identical retry payload testable.
        """
        body = {
            "model": self._model,
            "system": self._system_prompt(),
            "messages": [
                {"role": "user", "content": self._user_prompt(query)},
            ],
            "temperature": 0,
            "max_tokens": self._max_output_tokens,
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
            "Your perspective is critical review, never implementation advice: "
            "identify WHAT can break, what is weak and which assumptions are "
            "risky.\n"
            "Answer the review question with ONE json object (RFC 8259) and "
            "nothing else, matching exactly:\n"
            '{"status":"OK"|"ABSTAIN",'
            '"claim":"<one risk, weakness or risky assumption>",'
            '"evidence":["<concrete, checkable locator>"],'
            '"confidence":<number 0.0-1.0>,'
            '"severity":"LOW"|"MEDIUM"|"HIGH"|"CRITICAL",'
            '"reason":"<why you abstained; empty for OK>"}\n'
            "Rules:\n"
            '- "OK" requires a non-empty claim AND at least one evidence entry;\n'
            "- every evidence entry must be a concrete, checkable locator "
            "(module:line, rule id, symbol, file path or document citation) - an "
            "unsupported worry is not evidence;\n"
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
            "Question (WHAT can break, what is weak, what assumptions are "
            "risky?):\n"
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
                "x-api-key": api_key,
                "anthropic-version": self._api_version,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
            timeout=self._timeout,
        )
        attempts = self._max_retries + 1
        failure: Optional[ClaudeAdvisorError] = None
        for attempt in range(attempts):
            try:
                response = self._transport(request)
            except HttpTransportError as error:
                if not error.retryable:
                    raise ClaudeAdvisorTransportError(
                        _redact(str(error), api_key)
                    ) from error
                failure = (
                    ClaudeAdvisorTimeoutError(
                        f"Claude request timed out after {self._timeout}s"
                    )
                    if error.timed_out
                    else ClaudeAdvisorTransportError(
                        _redact(str(error), api_key)
                    )
                )
            else:
                status = response.status_code
                if 200 <= status < 300:
                    return response
                if not is_retryable_status(status):
                    # permanent: a bad key, a bad request, a missing model, ...
                    raise ClaudeAdvisorHttpError(
                        self._status_message(status, response, api_key)
                    )
                if status == HTTP_TOO_MANY_REQUESTS:
                    failure = ClaudeAdvisorRateLimitError(
                        self._status_message(status, response, api_key)
                    )
                else:
                    failure = ClaudeAdvisorHttpError(
                        self._status_message(status, response, api_key)
                    )
            if attempt + 1 < attempts:
                self._sleep(self._delay_for(attempt))
        if failure is None:  # pragma: no cover - unreachable by construction
            raise ClaudeAdvisorError("Claude request failed without a cause")
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
        return f"Claude request failed with HTTP {status}{suffix}"

    # -- response parsing ---------------------------------------------------
    def _parse_envelope(
        self, response: HttpResponse, secret: Optional[str]
    ) -> dict:
        """The Messages envelope as a JSON object."""
        try:
            data = response.json()
        except ValueError as error:
            raise ClaudeAdvisorInvalidResponseError(
                "Claude returned a body that is not JSON: "
                + _excerpt(_redact(response.text(), secret))
            ) from error
        if not isinstance(data, dict):
            raise ClaudeAdvisorInvalidResponseError(
                "Claude response must be a JSON object; got "
                f"{type(data).__name__}"
            )
        return data

    @staticmethod
    def _content_of(
        envelope: Mapping[str, Any], secret: Optional[str]
    ) -> str:
        """The first non-empty assistant text block - never redacted.

        Anthropic returns a *list* of content blocks, so this is deliberately
        provider-specific and NOT shared: only a ``text`` block carrying
        something is an answer, and no such block is a contract defect rather
        than a silently empty finding.
        """
        blocks = envelope.get("content")
        if not isinstance(blocks, list) or not blocks:
            raise ClaudeAdvisorInvalidResponseError(
                "Claude response carries no content blocks: "
                + _excerpt(_redact(json.dumps(envelope, default=str), secret))
            )
        for block in blocks:
            if not isinstance(block, Mapping):
                continue
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                return text
        raise ClaudeAdvisorInvalidResponseError(
            "Claude response carries no assistant text"
        )

    def _parse_contract(
        self, content: str, secret: Optional[str]
    ) -> dict:
        """The advisor contract object - strictly JSON, strictly an object."""
        text = content.strip()
        try:
            contract = json.loads(text)
        except ValueError as error:
            raise ClaudeAdvisorInvalidResponseError(
                "advisor answer is not JSON: "
                + _excerpt(_redact(text, secret))
            ) from error
        if not isinstance(contract, dict):
            raise ClaudeAdvisorInvalidResponseError(
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
        :class:`ClaudeAdvisorAbstainError`, never as a broken contract - even
        when it carries no claim. A missing or unknown ``status``, by contrast,
        is a contract violation, because nothing announced a refusal.

        The finding stays provider-neutral: only ``source`` names the provider,
        while ``claim``/``evidence``/``confidence``/``severity`` are exactly the
        domain fields every advisor fills. The Critical Reviewer persona changes
        the *question* the model answers, never the shape of the answer.
        """
        status = self._status_of(contract)
        if status == _CONTRACT_STATUS_ABSTAIN:
            raise ClaudeAdvisorAbstainError(self._abstain_reason(contract))
        claim = self._claim_of(contract)
        evidence = self._evidence_of(contract)
        confidence = self._confidence_of(contract)
        severity = self._severity_of(contract)
        return Finding(
            id=_finding_id(
                CLAUDE_PROVIDER, self._model, query.step_no, claim, evidence
            ),
            source=CLAUDE_PROVIDER,
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
        raise ClaudeAdvisorInvalidResponseError(
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
            raise ClaudeAdvisorInvalidResponseError(
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
            raise ClaudeAdvisorInvalidResponseError(
                'advisor answer field "evidence" must be a list of locators'
            )
        entries: list[str] = []
        for item in evidence:
            if not isinstance(item, str) or not item.strip():
                raise ClaudeAdvisorInvalidResponseError(
                    'every "evidence" entry must be a non-empty string'
                )
            entries.append(item.strip())
        if not entries:
            raise ClaudeAdvisorInvalidResponseError(
                'advisor answer field "evidence" must contain at least one '
                'locator on an "OK" answer - a worry without evidence is not a '
                "finding"
            )
        return tuple(entries)

    @staticmethod
    def _confidence_of(contract: Mapping[str, Any]) -> float:
        confidence = contract.get("confidence")
        if isinstance(confidence, bool) or not isinstance(
            confidence, (int, float)
        ):
            raise ClaudeAdvisorInvalidResponseError(
                'advisor answer field "confidence" must be a number between 0.0 '
                "and 1.0"
            )
        value = float(confidence)
        if not 0.0 <= value <= 1.0:
            raise ClaudeAdvisorInvalidResponseError(
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
            raise ClaudeAdvisorInvalidResponseError(
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
                    provider=CLAUDE_PROVIDER,
                    event_id=_cost_event_id(CLAUDE_PROVIDER, envelope),
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
        """The Anthropic token counts, or ``None`` when they are unusable.

        Deliberately provider-specific and NOT shared through ``._http``:
        Anthropic reports ``input_tokens``/``output_tokens``, not the
        ``prompt_tokens``/``completion_tokens`` pair OpenAI uses, so the mapping
        to the neutral ``(input, output)`` tuple belongs to this adapter.
        """
        usage = envelope.get("usage")
        if not isinstance(usage, Mapping):
            return None
        sent = usage.get("input_tokens")
        received = usage.get("output_tokens")
        for value in (sent, received):
            if isinstance(value, bool) or not isinstance(value, int):
                return None
            if value < 0:
                return None
        return int(sent), int(received)

    def _price_for(self, model: str) -> ModelPrice:
        """The injected price, or the documented unknown-price fallback."""
        return self._pricing.get(model, UNKNOWN_PRICE)
