"""OpenAI advisor adapter - the "Implementation Analyst" of :class:`AdvisorPort`.

The assistant is *deterministic first*: the architecture validator, the
realization gate, the step FSM and the review policy decide everything that can
be decided from code and persisted state. This adapter adds a **consultative**
capability on top of that - it answers the question *"HOW to realize this?"* with
a structured, evidence-based :class:`~architecture_assistant.domain.models.Finding`
and nothing else.

It deliberately cannot do more than that:

* it is **never** wired into ``decide_review``, the realization gate or the
  architecture validator, so a model answer can never override a deterministic
  rule, a persisted baseline or a gate verdict;
* it returns a :class:`Finding <architecture_assistant.domain.models.Finding>` -
  never a :class:`Decision <architecture_assistant.domain.models.Decision>`, a
  version, a rule change or an ADR. Judging conflicts is a later, separate step;
* it must **fail closed**. A timeout, a broken HTTP status, a malformed model
  answer or an explicit abstention raises a specific error - the adapter never
  invents a finding to keep the loop moving.

Only two provider-specific facts live here: the OpenAI chat-completions wire
format and the ``OPENAI_API_KEY`` environment variable. Nothing the rest of the
system sees is provider-shaped.

Security / secrets
------------------
The API key is **never** hardcoded, never a default value, never stored on the
instance, never part of the request body, never in ``repr`` and never in an error
message: it travels only in the ``Authorization`` header (or in the injected
``api_key`` argument). Response and exception text is redacted before it is
embedded in an error, so even a pathological provider echo cannot leak it.

Determinism
-----------
``temperature=0``, a canonically serialized payload, a deterministic finding id
(provider + model + step + claim + evidence - never the clock), a bounded,
jitter-free retry backoff and injectable ``clock``/``sleep``/``transport`` seams
make every behaviour reproducible offline, without a network or a real key.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping, Optional, Sequence

from ..domain.enums import Severity
from ..domain.models import Finding, utc_now
from ..ports.capabilities import AdvisorQuery, CostPort, CostRecord

__all__ = [
    "OPENAI_PROVIDER",
    "DEFAULT_OPENAI_MODEL",
    "IMPLEMENTATION_ANALYST_ROLE",
    "OPENAI_CHAT_COMPLETIONS_URL",
    "OPENAI_API_KEY_ENV_VAR",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_BACKOFF_SCHEDULE",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "ABSTAIN_REASON_FALLBACK",
    "CONTRACT_STATUS_OK",
    "CONTRACT_STATUS_ABSTAIN",
    "UNKNOWN_PRICE",
    "ModelPrice",
    "HttpRequest",
    "HttpResponse",
    "HttpTransportError",
    "OpenAIAdvisorError",
    "MissingApiKeyError",
    "OpenAIAdvisorTransportError",
    "OpenAIAdvisorTimeoutError",
    "OpenAIAdvisorRateLimitError",
    "OpenAIAdvisorHttpError",
    "OpenAIAdvisorInvalidResponseError",
    "OpenAIAdvisorAbstainError",
    "OpenAIAdvisorAdapter",
]

#: The provider id stored in ``Finding.source`` and ``CostRecord.provider``.
OPENAI_PROVIDER = "openai"

#: The single advisor role of this step. The role is *adapter metadata* (the
#: persona of the system prompt), not a domain concept: the domain contract of a
#: finding carries ``source``, and a second OpenAI role would be a second adapter.
IMPLEMENTATION_ANALYST_ROLE = "Implementation Analyst"

#: Chat-completions endpoint - the only OpenAI URL this codebase knows.
OPENAI_CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"

#: The environment variable the key is read from when none is injected.
OPENAI_API_KEY_ENV_VAR = "OPENAI_API_KEY"

#: Model asked for the implementation analysis. A *default*, injected by the
#: composition root - never a statement about price or availability.
DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"

DEFAULT_TIMEOUT_SECONDS = 30.0

#: Retries *after* the first attempt (three attempts by default).
DEFAULT_MAX_RETRIES = 2

#: Bounded, deterministic, jitter-free backoff: the first retry is immediate and
#: later ones fall back to the last entry. No randomness in this step.
DEFAULT_BACKOFF_SCHEDULE: tuple[float, ...] = (0.0, 1.0, 2.0)

DEFAULT_MAX_OUTPUT_TOKENS = 1024

#: Used when the model abstains without putting anything in ``reason``.
ABSTAIN_REASON_FALLBACK = "the model abstained without stating a reason"

CONTRACT_STATUS_OK = "OK"
CONTRACT_STATUS_ABSTAIN = "ABSTAIN"

_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_SERVER_ERROR = 500
_BODY_EXCERPT_LIMIT = 300


class OpenAIAdvisorError(Exception):
    """Base class for every OpenAI advisor failure."""


class MissingApiKeyError(OpenAIAdvisorError):
    """Raised when neither the argument nor the environment provides a key."""


class HttpTransportError(OpenAIAdvisorError):
    """A transport-level failure, raised *by* the transport itself.

    ``retryable`` is the transport's own judgement (connection reset, DNS, ...)
    and ``timed_out`` marks a socket timeout, so the adapter can report it
    precisely instead of guessing from a message.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        timed_out: bool = False,
    ) -> None:
        if not isinstance(retryable, bool):
            raise ValueError("retryable must be a bool")
        if not isinstance(timed_out, bool):
            raise ValueError("timed_out must be a bool")
        super().__init__(message)
        self.retryable = retryable
        self.timed_out = timed_out


class OpenAIAdvisorTransportError(OpenAIAdvisorError):
    """The request could not be delivered (retries used up)."""


class OpenAIAdvisorTimeoutError(OpenAIAdvisorTransportError):
    """The request timed out (retries used up)."""


class OpenAIAdvisorRateLimitError(OpenAIAdvisorError):
    """HTTP 429 (retries used up)."""


class OpenAIAdvisorHttpError(OpenAIAdvisorError):
    """A non-retryable status - or a retryable one that never recovered."""


class OpenAIAdvisorInvalidResponseError(OpenAIAdvisorError):
    """The answer violated the structured output contract.

    This is **not** an abstention: an abstention is the model saying so. A broken
    contract is a defect and must be told apart from a deliberate refusal.
    """


class OpenAIAdvisorAbstainError(OpenAIAdvisorError):
    """The model explicitly abstained or refused to answer."""

    def __init__(self, reason: str) -> None:
        text = (
            reason
            if isinstance(reason, str) and reason.strip()
            else ABSTAIN_REASON_FALLBACK
        )
        super().__init__(f"advisor abstained: {text}")
        self.reason = text


# ---------------------------------------------------------------------------
# infrastructure-private transport seam (never exported to ports/domain)
# ---------------------------------------------------------------------------

def _redact(text: str, secret: Optional[str]) -> str:
    """Remove ``secret`` from ``text`` before it can reach a message."""
    if not text:
        return text
    if secret:
        return text.replace(secret, "***")
    return text


def _excerpt(text: str, limit: int = _BODY_EXCERPT_LIMIT) -> str:
    """A short, single-line excerpt of provider text."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[:limit] + "..."


@dataclass(frozen=True)
class HttpRequest:
    """Provider-neutral HTTP request handed to a transport seam.

    Kept here - not in ``ports`` - because it is a transport detail of this
    adapter, not a capability contract of the assistant.
    """

    url: str
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)
    method: str = "POST"
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url.strip():
            raise ValueError("url must be a non-empty string")
        if not isinstance(self.body, bytes):
            raise ValueError(
                f"body must be bytes; got {type(self.body).__name__}"
            )
        if not isinstance(self.method, str) or not self.method.strip():
            raise ValueError("method must be a non-empty string")
        if isinstance(self.timeout, bool) or not isinstance(
            self.timeout, (int, float)
        ):
            raise ValueError(f"timeout must be a number; got {self.timeout!r}")
        if self.timeout <= 0:
            raise ValueError(f"timeout must be > 0; got {self.timeout!r}")
        object.__setattr__(
            self,
            "headers",
            {str(k): str(v) for k, v in dict(self.headers).items()},
        )

    def text(self) -> str:
        """The request body as text (tests inspect the payload through this)."""
        return self.body.decode("utf-8")


@dataclass(frozen=True)
class HttpResponse:
    """Provider-neutral HTTP response returned by a transport seam."""

    status_code: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.status_code, bool) or not isinstance(
            self.status_code, int
        ):
            raise ValueError(
                f"status_code must be an int; got {self.status_code!r}"
            )
        if not isinstance(self.body, bytes):
            raise ValueError(
                f"body must be bytes; got {type(self.body).__name__}"
            )
        object.__setattr__(
            self,
            "headers",
            {str(k): str(v) for k, v in dict(self.headers).items()},
        )

    def text(self) -> str:
        """The response body as text, with undecodable bytes replaced."""
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        """The parsed body - raises ``ValueError`` when it is not JSON."""
        return json.loads(self.text())


def urllib_transport(request: HttpRequest) -> HttpResponse:
    """Default transport: ``urllib`` only, no third-party HTTP client.

    HTTP error statuses are returned as an ordinary :class:`HttpResponse`, so the
    retry policy lives in exactly one place (the adapter); connection-level
    problems raise :class:`HttpTransportError`, tagged with what the adapter needs.
    """
    prepared = urllib.request.Request(
        request.url,
        data=request.body,
        headers=dict(request.headers),
        method=request.method,
    )
    try:
        with urllib.request.urlopen(prepared, timeout=request.timeout) as response:
            return HttpResponse(
                status_code=int(response.status),
                body=response.read(),
                headers=dict(response.headers),
            )
    except urllib.error.HTTPError as error:
        try:
            body = error.read()
        except Exception:  # pragma: no cover - defensive, body may be closed
            body = b""
        return HttpResponse(
            status_code=int(error.code),
            body=body,
            headers=dict(error.headers or {}),
        )
    except (socket.timeout, TimeoutError) as error:
        raise HttpTransportError(
            f"request timed out after {request.timeout}s", timed_out=True
        ) from error
    except urllib.error.URLError as error:
        reason = error.reason
        raise HttpTransportError(
            f"transport failure: {type(reason).__name__}",
            retryable=True,
            timed_out=isinstance(reason, (socket.timeout, TimeoutError)),
        ) from error
    except OSError as error:
        raise HttpTransportError(
            f"transport failure: {type(error).__name__}", retryable=True
        ) from error


@dataclass(frozen=True)
class ModelPrice:
    """USD per 1,000 tokens for one model - supplied by the caller.

    The core ships **no** price table: prices change, differ per contract and are
    not the assistant's knowledge. Without an injected entry the adapter reports
    ``cost_usd = 0.0`` (see :data:`UNKNOWN_PRICE`) - a documented "unknown price"
    result, never a guess.
    """

    prompt_per_1k: float = 0.0
    completion_per_1k: float = 0.0

    def __post_init__(self) -> None:
        for name in ("prompt_per_1k", "completion_per_1k"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a number; got {value!r}")
            if value < 0:
                raise ValueError(f"{name} must be >= 0; got {value!r}")

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        """Deterministic cost for one response, rounded to 6 decimals."""
        total = (
            (input_tokens / 1000.0) * self.prompt_per_1k
            + (output_tokens / 1000.0) * self.completion_per_1k
        )
        return round(total, 6)


#: Documented fallback for a model with no injected price: unknown, not free.
UNKNOWN_PRICE = ModelPrice()


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string; got {value!r}")
    return value


def _require_optional_text(value: Any, field_name: str) -> Optional[str]:
    if value is None:
        return None
    return _require_text(value, field_name)


def _require_number(
    value: Any, field_name: str, *, minimum: Optional[float] = None
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number; got {value!r}")
    number = float(value)
    if minimum is not None and number < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}; got {value!r}")
    return number


def _require_int(
    value: Any, field_name: str, *, minimum: Optional[int] = None
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an int; got {value!r}")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}; got {value!r}")
    return value


class OpenAIAdvisorAdapter:
    """OpenAI implementation of :class:`AdvisorPort` (Implementation Analyst).

    The adapter is *only* a capability: constructing it opens no connection, reads
    no key and changes no state, so the composition root may always build it and a
    project without ``OPENAI_API_KEY`` simply never calls :meth:`advise`.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        url: str = OPENAI_CHAT_COMPLETIONS_URL,
        model: str = DEFAULT_OPENAI_MODEL,
        role: str = IMPLEMENTATION_ANALYST_ROLE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_schedule: Sequence[float] = DEFAULT_BACKOFF_SCHEDULE,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
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
        self._model = _require_text(model, "model")
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
        return OPENAI_PROVIDER

    @property
    def model(self) -> str:
        """The model asked for the analysis."""
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
            os.environ.get(OPENAI_API_KEY_ENV_VAR, "").strip()
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
            f"{type(self).__name__}(provider={OPENAI_PROVIDER!r}, "
            f"model={self._model!r}, role={self._role!r}, "
            f"configured={self.is_configured})"
        )

    # -- AdvisorPort -------------------------------------------------------
    def advise(self, query: AdvisorQuery) -> Finding:
        """Answer one implementation question with an evidence-based finding.

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
        from_env = os.environ.get(OPENAI_API_KEY_ENV_VAR, "")
        if from_env.strip():
            return from_env
        raise MissingApiKeyError(
            "no OpenAI API key available: pass api_key=... or set the "
            f"{OPENAI_API_KEY_ENV_VAR} environment variable"
        )

    # -- request -----------------------------------------------------------
    def _build_payload(self, query: AdvisorQuery) -> bytes:
        """The exact request body - serialized once, reused for every attempt.

        Canonical JSON (sorted keys, compact separators) keeps the bytes stable,
        which is what makes an identical retry payload testable.
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
            "Answer the implementation question with ONE json object (RFC 8259) "
            "and nothing else, matching exactly:\n"
            '{"status":"OK"|"ABSTAIN",'
            '"claim":"<one sentence>",'
            '"evidence":["<concrete, checkable locator>"],'
            '"confidence":<number 0.0-1.0>,'
            '"severity":"LOW"|"MEDIUM"|"HIGH"|"CRITICAL",'
            '"reason":"<why you abstained; empty for OK>"}\n'
            "Rules:\n"
            '- "OK" requires a non-empty claim AND at least one evidence entry;\n'
            "- every evidence entry must be a concrete, checkable locator "
            "(module:line, rule id, symbol, file path or document citation) - an "
            "unsupported opinion is not evidence;\n"
            '- use "ABSTAIN" when the given context is insufficient or you refuse '
            'to answer, and explain it in "reason".'
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
            "Question (HOW to realize this?):\n"
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
        failure: Optional[OpenAIAdvisorError] = None
        for attempt in range(attempts):
            try:
                response = self._transport(request)
            except HttpTransportError as error:
                if not error.retryable:
                    raise OpenAIAdvisorTransportError(
                        _redact(str(error), api_key)
                    ) from error
                failure = (
                    OpenAIAdvisorTimeoutError(
                        f"OpenAI request timed out after {self._timeout}s"
                    )
                    if error.timed_out
                    else OpenAIAdvisorTransportError(
                        _redact(str(error), api_key)
                    )
                )
            else:
                status = response.status_code
                if 200 <= status < 300:
                    return response
                if status == _HTTP_TOO_MANY_REQUESTS:
                    failure = OpenAIAdvisorRateLimitError(
                        self._status_message(status, response, api_key)
                    )
                elif status >= _HTTP_SERVER_ERROR:
                    failure = OpenAIAdvisorHttpError(
                        self._status_message(status, response, api_key)
                    )
                else:
                    # permanent: a bad key, a bad request, a missing model, ...
                    raise OpenAIAdvisorHttpError(
                        self._status_message(status, response, api_key)
                    )
            if attempt + 1 < attempts:
                self._sleep(self._delay_for(attempt))
        if failure is None:  # pragma: no cover - unreachable by construction
            raise OpenAIAdvisorError("OpenAI request failed without a cause")
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
        return f"OpenAI request failed with HTTP {status}{suffix}"

    # -- response parsing ---------------------------------------------------
    def _parse_envelope(
        self, response: HttpResponse, secret: Optional[str]
    ) -> dict:
        """The chat-completions envelope as a JSON object."""
        try:
            data = response.json()
        except ValueError as error:
            raise OpenAIAdvisorInvalidResponseError(
                "OpenAI returned a body that is not JSON: "
                + _excerpt(_redact(response.text(), secret))
            ) from error
        if not isinstance(data, dict):
            raise OpenAIAdvisorInvalidResponseError(
                "OpenAI response must be a JSON object; got "
                f"{type(data).__name__}"
            )
        return data

    @staticmethod
    def _content_of(
        envelope: Mapping[str, Any], secret: Optional[str]
    ) -> str:
        """The assistant message text - validated, never redacted."""
        choices = envelope.get("choices")
        if not isinstance(choices, list) or not choices:
            raise OpenAIAdvisorInvalidResponseError(
                "OpenAI response carries no choices: "
                + _excerpt(_redact(json.dumps(envelope, default=str), secret))
            )
        first = choices[0]
        message = first.get("message") if isinstance(first, Mapping) else None
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, str) or not content.strip():
            raise OpenAIAdvisorInvalidResponseError(
                "OpenAI response carries no assistant content"
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
            raise OpenAIAdvisorInvalidResponseError(
                "advisor answer is not JSON: "
                + _excerpt(_redact(text, secret))
            ) from error
        if not isinstance(contract, dict):
            raise OpenAIAdvisorInvalidResponseError(
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
        :class:`OpenAIAdvisorAbstainError`, never as a broken contract - even when
        it carries no claim. A missing or unknown ``status``, by contrast, is a
        contract violation, because nothing announced a refusal.
        """
        status = self._status_of(contract)
        if status == CONTRACT_STATUS_ABSTAIN:
            raise OpenAIAdvisorAbstainError(self._abstain_reason(contract))
        claim = self._claim_of(contract)
        evidence = self._evidence_of(contract)
        confidence = self._confidence_of(contract)
        severity = self._severity_of(contract)
        return Finding(
            id=_finding_id(
                OPENAI_PROVIDER, self._model, query.step_no, claim, evidence
            ),
            source=OPENAI_PROVIDER,
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
        if token in (CONTRACT_STATUS_OK, CONTRACT_STATUS_ABSTAIN):
            return token
        raise OpenAIAdvisorInvalidResponseError(
            'advisor answer field "status" must be "OK" or "ABSTAIN"; got '
            f"{_excerpt(str(raw), 40)!r} - a missing or unknown status is a "
            "contract violation, not an abstention"
        )

    @staticmethod
    def _abstain_reason(contract: Mapping[str, Any]) -> str:
        reason = contract.get("reason")
        if isinstance(reason, str) and reason.strip():
            return reason.strip()
        return ABSTAIN_REASON_FALLBACK

    @staticmethod
    def _claim_of(contract: Mapping[str, Any]) -> str:
        claim = contract.get("claim")
        if not isinstance(claim, str) or not claim.strip():
            raise OpenAIAdvisorInvalidResponseError(
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
            raise OpenAIAdvisorInvalidResponseError(
                'advisor answer field "evidence" must be a list of locators'
            )
        entries: list[str] = []
        for item in evidence:
            if not isinstance(item, str) or not item.strip():
                raise OpenAIAdvisorInvalidResponseError(
                    'every "evidence" entry must be a non-empty string'
                )
            entries.append(item.strip())
        if not entries:
            raise OpenAIAdvisorInvalidResponseError(
                'advisor answer field "evidence" must contain at least one '
                'locator on an "OK" answer - a claim without evidence is not a '
                "finding"
            )
        return tuple(entries)

    @staticmethod
    def _confidence_of(contract: Mapping[str, Any]) -> float:
        confidence = contract.get("confidence")
        if isinstance(confidence, bool) or not isinstance(
            confidence, (int, float)
        ):
            raise OpenAIAdvisorInvalidResponseError(
                'advisor answer field "confidence" must be a number between 0.0 '
                "and 1.0"
            )
        value = float(confidence)
        if not 0.0 <= value <= 1.0:
            raise OpenAIAdvisorInvalidResponseError(
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
            raise OpenAIAdvisorInvalidResponseError(
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
        """
        usage = self._usage_of(envelope)
        if usage is None or self._cost_sink is None:
            return
        input_tokens, output_tokens = usage
        try:
            self._cost_sink.record(
                CostRecord(
                    provider=OPENAI_PROVIDER,
                    model=self._model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=self._price_for(self._model).cost(
                        input_tokens, output_tokens
                    ),
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
        """The provider token counts, or ``None`` when they are unusable."""
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


def _finding_id(
    provider: str,
    model: str,
    step_no: Optional[int],
    claim: str,
    evidence: Sequence[str],
) -> str:
    """The stable id of one *logical* advisor result.

    Only content that identifies the claim is hashed - provider, model, step,
    claim and evidence - so the same claim backed by the same evidence from the
    same model on the same step is always the same finding and a re-ask
    de-duplicates instead of piling up. The clock is deliberately excluded (an id
    that changed every run would be useless for de-duplication), and so are
    confidence and severity, so that a re-worded degree of certainty does not
    masquerade as a new finding.
    """
    parts = [
        provider,
        model,
        "" if step_no is None else str(step_no),
        claim,
        *evidence,
    ]
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]
    return f"finding-{provider}-{0 if step_no is None else step_no}-{digest}"









