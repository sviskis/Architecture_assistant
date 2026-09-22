"""Provider-neutral transport, cost and validation primitives.

This module is **private infrastructure** (the leading underscore is deliberate).
It exists for exactly one reason: the provider advisor adapters - ``openai``,
``claude`` and later ``grok`` - would otherwise copy-paste the same mechanical
code three times: the HTTP request/response DTOs, the ``urllib`` transport, the
neutral transport error, the retry *classification*, secret redaction, the
primitive validators, the deterministic finding-id helper and the price model.

It deliberately stays narrow, because a shared layer that grows into a generic
AI framework is worse than duplication:

* no advisor orchestration;
* no prompt construction;
* no provider response-contract parsing and no ABSTAIN semantics;
* no provider exception hierarchy, no provider model selection and no auth;
* no decision logic.

Everything provider-shaped stays in the provider adapter module. This module is
not a public API: it adds **nothing** to
``architecture_assistant.infrastructure.__all__``. The names below are only
re-exported publicly because they were already public before (through
``openai.py``), which is what keeps Step 12's surface backwards compatible.
"""

from __future__ import annotations

import hashlib
import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from ..ports.capabilities import ConnectionStatus, CostIdentityUnavailableError

__all__ = [
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
]

#: Default request timeout, shared by every provider adapter.
DEFAULT_TIMEOUT_SECONDS = 30.0

#: Retries *after* the first attempt (three attempts by default).
DEFAULT_MAX_RETRIES = 2

#: Bounded, deterministic, jitter-free backoff: the first retry is immediate and
#: later ones fall back to the last entry. No randomness anywhere.
DEFAULT_BACKOFF_SCHEDULE: tuple[float, ...] = (0.0, 1.0, 2.0)

#: The one 4xx the retry policy retries: "slow down".
HTTP_TOO_MANY_REQUESTS = 429

#: Any status at or above this is a server-side failure - retried as well.
HTTP_SERVER_ERROR = 500

#: Longest provider text embedded in an error message.
_BODY_EXCERPT_LIMIT = 300


def is_retryable_status(status_code: int) -> bool:
    """Whether one HTTP status is retried: HTTP 429, or any HTTP 5xx.

    Only this mechanical classification is shared. The retry *policy* - how many
    attempts, how long to wait, which typed error to raise - stays in each
    provider adapter, so an adapter can still adapt the policy without this
    module growing a framework.
    """
    if isinstance(status_code, bool) or not isinstance(status_code, int):
        raise ValueError(f"status_code must be an int; got {status_code!r}")
    return (
        status_code == HTTP_TOO_MANY_REQUESTS or status_code >= HTTP_SERVER_ERROR
    )


def _classify_connection_status(status_code: int) -> "ConnectionStatus":
    """Map one HTTP status of a minimal, authenticated probe to a verdict.

    This is the *mechanical* half of the Test Connection capability, and it is
    shared for the same reason :func:`is_retryable_status` is: every provider
    adapter would otherwise repeat it. The *probe* itself - the endpoint, the
    auth header scheme, the model reference and the request body - stays in each
    provider adapter, because that is the provider-shaped part.

    The probe always references the configured model, which is what makes
    ``MODEL ERROR`` distinguishable from ``CONNECTED``: in a well-formed minimal
    request the model is the only input a provider can reject with a 4xx that is
    not an authentication failure.

    * ``2xx`` -> ``CONNECTED``
    * ``401`` / ``403`` -> ``AUTH ERROR``
    * ``400`` / ``404`` / ``422`` -> ``MODEL ERROR``
    * ``429`` / any ``5xx`` -> ``PROVIDER ERROR``
    * anything else -> ``PROVIDER ERROR`` (an unexpected status is not the
      operator's configuration being wrong, so it is never blamed on the model)
    """
    if isinstance(status_code, bool) or not isinstance(status_code, int):
        raise ValueError(f"status_code must be an int; got {status_code!r}")
    if 200 <= status_code < 300:
        return ConnectionStatus.CONNECTED
    if status_code in (401, 403):
        return ConnectionStatus.AUTH_ERROR
    if status_code in (400, 404, 422):
        return ConnectionStatus.MODEL_ERROR
    return ConnectionStatus.PROVIDER_ERROR


class HttpTransportError(Exception):
    """A transport-level failure, raised *by* the transport itself.

    Provider-neutral on purpose: it does **not** inherit from any provider's
    advisor error, so an adapter can catch it and re-raise its own precisely
    typed failure without importing another provider's exception hierarchy.

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

    Kept here - not in ``ports`` - because it is a transport detail, not a
    capability contract of the assistant.
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
    not the assistant's knowledge. Without an injected entry an adapter reports
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


def _cost_event_id(provider: str, envelope: Mapping[str, Any]) -> str:
    """The provider-native stable identity of one billable provider response.

    Every supported provider returns a per-response id (OpenAI ``chatcmpl-…``,
    Claude ``msg_…``, Grok/xAI OpenAI-style ``id``), and that id is the one
    input which is both *stable across a replay of the same logical response*
    and *different for every genuine call*. Deriving the recorded event identity
    from it is what makes cost recording idempotent without collapsing two real
    calls into one.

    Nothing else an adapter holds has both properties: a hash of the request
    body is stable but identical for two genuine identical requests, and the
    clock or the token counts move (or repeat) independently of identity. So
    when a response carries no usable id this helper refuses to invent one and
    raises :class:`CostIdentityUnavailableError`; the adapter surfaces that as
    non-fatal telemetry and persists nothing.
    """
    response_id = envelope.get("id") if isinstance(envelope, Mapping) else None
    if not isinstance(response_id, str) or not response_id.strip():
        raise CostIdentityUnavailableError(
            f"the {provider} response carries no stable response id, so no "
            "cost event identity could be derived and nothing was recorded"
        )
    return f"{provider}:{response_id.strip()}"


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
