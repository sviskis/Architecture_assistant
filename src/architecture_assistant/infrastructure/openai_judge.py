"""OpenAI judge adapter - the "Evidence Judge" of :class:`JudgePort`.

This is an **OpenAI** adapter that happens to implement the *judge* capability.
The two are deliberately kept apart:

* ``judge`` is the **role/capability** (``JudgePort``) - resolving one explicit,
  otherwise unresolvable evidence conflict;
* ``openai`` is the **provider** (``provider == "openai"``) - this module's
  transport, key and model.

A future ``ClaudeJudgeAdapter`` implements the same port with a different
provider; the service layer never learns which one it got.

What the judge is *not*
-----------------------
It is **not** a fourth voter, not a majority vote, not a provider ranking and it
never overrules the deterministic architecture gate. Its ``Decision`` is a
statement about **one evidence conflict only**:

* ``ACCEPTED`` - for this explicit evidence conflict, the judge favors the
  *supporting* relation;
* ``REJECTED`` - ... favors the *contradicting* relation;
* ``ABSTAIN`` - the judge cannot resolve this evidence conflict;
* ``ERROR`` - the judge execution failed.

None of those four means "the architecture is compliant". The authoritative
gate ``Decision`` stays exactly as the deterministic gate produced it, and the
judge's output is kept as a separate layer next to it.

Evidence discipline
-------------------
The judge may use **only** the findings and context it is handed. It never
introduces external facts, never browses, never infers missing evidence, and its
``evidence_refs`` are validated against the supplied findings - a fabricated or
unknown id is a contract defect, not a citation.

Failure semantics
-----------------
``judge()`` returns a :class:`Decision` and never invents a verdict: a timeout,
429 or 5xx is retried (identical payload, bounded deterministic backoff) and then
becomes ``ERROR``; a permanent 4xx becomes ``ERROR`` without retry; an explicit
model abstention becomes ``ABSTAIN``; malformed output becomes ``ERROR``. An
infrastructure failure is **never** mapped to ``REJECTED``.

Security / cost
---------------
``OPENAI_API_KEY`` travels only in the ``Authorization`` header - never in the
payload, a ``Decision``, ``repr`` or a rationale. Response text is redacted
before it can reach a message. Cost telemetry is a secondary side effect: a
failing ``CostPort`` is surfaced through ``last_telemetry_error`` and can never
turn a valid judge ``Decision`` into an error.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Optional, Sequence

from ..domain.enums import DecisionStatus
from ..domain.models import Decision, utc_now
from ..ports.capabilities import CostPort, CostRecord, JudgeConflict
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
    _redact,
    _require_int,
    _require_number,
    _require_optional_text,
    _require_text,
    is_retryable_status,
    urllib_transport,
)
from .openai import (
    OPENAI_API_KEY_ENV_VAR,
    OPENAI_CHAT_COMPLETIONS_URL,
    OPENAI_PROVIDER,
)

__all__ = [
    "EVIDENCE_JUDGE_ROLE",
    "DEFAULT_OPENAI_JUDGE_MODEL",
    "OPENAI_JUDGE_MODEL_ENV_VAR",
    "DEFAULT_JUDGE_MAX_OUTPUT_TOKENS",
    "OPENAI_JUDGE_FALLBACK_TARGET",
    "resolve_openai_judge_model",
    "OpenAIJudgeAdapter",
]

#: The advisor persona of this adapter - adapter metadata, not a domain concept.
EVIDENCE_JUDGE_ROLE = "Evidence Judge"

#: The single documented adapter default. A model name is **configuration, not
#: architecture truth**: it is resolved through :func:`resolve_openai_judge_model`
#: and can be changed without touching the domain or the architecture.
DEFAULT_OPENAI_JUDGE_MODEL = "gpt-5.6"

#: Optional environment variable naming the judge model (configuration, not a rule).
OPENAI_JUDGE_MODEL_ENV_VAR = "OPENAI_JUDGE_MODEL"

#: The judge's own output budget for one resolution (request shaping, not shared).
DEFAULT_JUDGE_MAX_OUTPUT_TOKENS = 1024

#: Used in the deterministic outcome phrase when a conflict carries no target.
OPENAI_JUDGE_FALLBACK_TARGET = "the disputed claim"

#: The only statuses a judge model may declare.
_VERDICT_STATUSES: Mapping[str, DecisionStatus] = {
    "ACCEPTED": DecisionStatus.ACCEPTED,
    "REJECTED": DecisionStatus.REJECTED,
    "ABSTAIN": DecisionStatus.ABSTAIN,
}

_OUTCOME_TEXT: Mapping[DecisionStatus, str] = {
    DecisionStatus.ACCEPTED: (
        "evidence conflict resolution: the judge favors the supporting relation"
    ),
    DecisionStatus.REJECTED: (
        "evidence conflict resolution: the judge favors the contradicting relation"
    ),
    DecisionStatus.ABSTAIN: (
        "evidence conflict resolution: the judge cannot resolve this conflict"
    ),
    DecisionStatus.ERROR: "evidence conflict resolution: the judge execution failed",
}


def resolve_openai_judge_model(model: Optional[str] = None) -> str:
    """Resolve the judge model through one documented order.

    1. the explicit ``model`` argument (constructor / composition config), when
       non-blank;
    2. the optional :data:`OPENAI_JUDGE_MODEL_ENV_VAR` environment variable, when
       non-blank;
    3. :data:`DEFAULT_OPENAI_JUDGE_MODEL` - the one documented adapter default.

    Nothing here checks that the model exists: the system must compose and run
    offline, so an unknown or retired model name is a provider-side failure
    later, never a start-up failure now. Changing the model is a configuration
    change - it never requires an architecture change.
    """
    if model is not None and str(model).strip():
        return str(model).strip()
    from_env = os.environ.get(OPENAI_JUDGE_MODEL_ENV_VAR, "")
    if from_env.strip():
        return from_env.strip()
    return DEFAULT_OPENAI_JUDGE_MODEL


#: Documented fallback when a judge abstains without stating a reason.
_ABSTAIN_REASON_FALLBACK = "the judge abstained without stating a reason"


class _JudgeAbstain(Exception):
    """Internal control flow: the model deliberately declined to resolve.

    Never escapes :meth:`OpenAIJudgeAdapter.judge` - it is translated into
    ``Decision(status=ABSTAIN)``.
    """

    def __init__(self, reason: str) -> None:
        text = (
            reason.strip()
            if isinstance(reason, str) and reason.strip()
            else _ABSTAIN_REASON_FALLBACK
        )
        super().__init__(text)
        self.reason = text


class _JudgeFailure(Exception):
    """Internal control flow: no resolution could be produced.

    Covers transport, HTTP, key and contract failures. Never escapes
    :meth:`OpenAIJudgeAdapter.judge` - it is translated into
    ``Decision(status=ERROR)``, never into ``REJECTED``.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class _JudgeVerdict:
    """A validated judge answer: a status plus the model's own reasoning."""

    status: DecisionStatus
    rationale: str
    evidence_refs: tuple[str, ...]


class OpenAIJudgeAdapter:
    """OpenAI implementation of :class:`JudgePort` (Evidence Judge).

    Constructing the adapter opens no connection, reads no key and changes no
    state, so the composition root may always build it: a project without
    ``OPENAI_API_KEY`` simply gets ``Decision(status=ERROR)`` instead of a
    resolution, and the deterministic workflow is unaffected.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        url: str = OPENAI_CHAT_COMPLETIONS_URL,
        model: Optional[str] = None,
        role: str = EVIDENCE_JUDGE_ROLE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_schedule: Sequence[float] = DEFAULT_BACKOFF_SCHEDULE,
        max_output_tokens: int = DEFAULT_JUDGE_MAX_OUTPUT_TOKENS,
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
        self._model = _require_text(resolve_openai_judge_model(model), "model")
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
        """The provider id of this adapter - it is an OpenAI adapter."""
        return OPENAI_PROVIDER

    @property
    def model(self) -> str:
        """The resolved judge model."""
        return self._model

    @property
    def role(self) -> str:
        """The judge persona - adapter metadata, not a domain concept."""
        return self._role

    @property
    def is_configured(self) -> bool:
        """Whether a key is available *now* (argument or environment).

        Checked lazily, so an environment key added after composition still works
        and a project without a key runs the whole deterministic system.
        """
        return bool(self._api_key) or bool(
            os.environ.get(OPENAI_API_KEY_ENV_VAR, "").strip()
        )

    @property
    def last_telemetry_error(self) -> Optional[BaseException]:
        """The last non-fatal cost-telemetry failure, if any."""
        return self._last_telemetry_error

    def __repr__(self) -> str:
        """Deliberately key-free - the API key must never be rendered."""
        return (
            f"{type(self).__name__}(provider={OPENAI_PROVIDER!r}, "
            f"model={self._model!r}, role={self._role!r}, "
            f"configured={self.is_configured})"
        )

    # -- JudgePort ---------------------------------------------------------
    def judge(self, conflict: JudgeConflict) -> Decision:
        """Resolve one explicit evidence conflict - never invent a verdict.

        Returns a :class:`Decision` in **every** case: the model's verdict, an
        ``ABSTAIN`` when the model declines, or an ``ERROR`` when the execution
        failed. A provider failure is never mapped onto ``REJECTED``, and the
        returned decision speaks only about this conflict - never about
        architecture compliance.
        """
        if not isinstance(conflict, JudgeConflict):
            raise ValueError(f"conflict must be a JudgeConflict; got {conflict!r}")
        self._last_telemetry_error = None
        try:
            api_key = self._resolve_api_key()
            payload = self._build_payload(conflict)
            response = self._post(payload, api_key)
            envelope = self._parse_envelope(response, api_key)
            content = self._content_of(envelope, api_key)
            # telemetry BEFORE the contract check: the call was paid for either
            # way, and a contract defect must not lose the cost record
            self._record_cost(envelope, conflict)
            verdict = self._parse_verdict(content, conflict, api_key)
        except _JudgeAbstain as abstain:
            return self._build_decision(
                conflict,
                status=DecisionStatus.ABSTAIN,
                rationale=(
                    "The judge could not resolve this evidence conflict: "
                    f"{abstain.reason}"
                ),
                evidence_refs=(),
            )
        except _JudgeFailure as failure:
            return self._build_decision(
                conflict,
                status=DecisionStatus.ERROR,
                rationale=(
                    "The judge execution failed, so this evidence conflict stays "
                    f"unresolved: {failure.message}"
                ),
                evidence_refs=(),
            )
        return self._build_decision(
            conflict,
            status=verdict.status,
            rationale=verdict.rationale,
            evidence_refs=verdict.evidence_refs,
        )

    # -- secrets -----------------------------------------------------------
    def _resolve_api_key(self) -> str:
        """The key for this call: argument first, environment second."""
        if self._api_key:
            return self._api_key
        from_env = os.environ.get(OPENAI_API_KEY_ENV_VAR, "")
        if from_env.strip():
            return from_env
        raise _JudgeFailure(
            "no OpenAI API key available: pass api_key=... or set the "
            f"{OPENAI_API_KEY_ENV_VAR} environment variable"
        )

    # -- request -----------------------------------------------------------
    def _build_payload(self, conflict: JudgeConflict) -> bytes:
        """The exact request body - serialized once, reused for every attempt."""
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": self._user_prompt(conflict)},
            ],
            "temperature": 0,
            "max_tokens": self._max_output_tokens,
            "response_format": {"type": "json_object"},
        }
        return json.dumps(
            body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    def _system_prompt(self) -> str:
        """The judge role, the evidence discipline and the output contract."""
        return (
            f'You are the "{self._role}" of a deterministic architecture '
            "lifecycle assistant. You resolve exactly ONE explicit evidence "
            "conflict and nothing else. The assistant's architecture rules, "
            "validators and gates always outrank you: you never decide "
            "architecture compliance, never override a deterministic verdict "
            "and never claim authority over a rule.\n"
            "Use ONLY the findings and the context you are given. Do not bring "
            "in external facts, do not browse, do not use any other knowledge "
            "and do not infer evidence that was not supplied. If the supplied "
            "evidence is insufficient to prefer one side, you must abstain.\n"
            "Answer with ONE json object (RFC 8259) and nothing else, matching "
            "exactly:\n"
            '{"status":"ACCEPTED"|"REJECTED"|"ABSTAIN",'
            '"rationale":"<why, citing only supplied findings>",'
            '"evidence_refs":["<id of a supplied finding you relied on>"],'
            '"reason":"<only for ABSTAIN; empty otherwise>"}\n'
            "Rules:\n"
            '- "ACCEPTED" means you favor the supporting relation for THIS '
            "conflict;\n"
            '- "REJECTED" means you favor the contradicting relation for THIS '
            "conflict;\n"
            '- "ABSTAIN" means you cannot resolve this conflict;\n'
            "- every evidence_refs entry must be the id of a finding that was "
            "supplied to you in this request;\n"
            "- never invent a finding id, never cite anything outside the "
            "supplied set, and never reference the architecture baseline."
        )

    def _user_prompt(self, conflict: JudgeConflict) -> str:
        """The conflict plus its findings/context, canonically serialized."""
        context = dict(conflict.context)
        target = str(context.get("target", "") or OPENAI_JUDGE_FALLBACK_TARGET)
        findings = json.dumps(
            [self._finding_payload(finding) for finding in conflict.findings],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        serialized_context = json.dumps(
            context,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return (
            f"Conflict: {conflict.question}\n"
            f"Target: {target}\n"
            f"Supporting finding ids: {_id_list(context.get('supporting_ids'))}\n"
            f"Contradicting finding ids: "
            f"{_id_list(context.get('contradicting_ids'))}\n"
            "Supplied findings (json):\n"
            f"{findings}\n"
            "Supplied context (json):\n"
            f"{serialized_context}"
        )

    @staticmethod
    def _finding_payload(finding) -> dict[str, Any]:
        """Only the supplied finding's own fields - nothing is added."""
        return {
            "id": finding.id,
            "source": finding.source,
            "claim": finding.claim,
            "evidence": list(finding.evidence),
            "confidence": finding.confidence,
            "severity": finding.severity.value,
            "step_no": finding.step_no,
        }

    # -- transport + retry policy ------------------------------------------
    def _post(self, payload: bytes, api_key: str) -> HttpResponse:
        """Send the request, retrying only the retryable failures.

        Retried: a transport timeout, HTTP 429 and HTTP 5xx - with the same
        :class:`HttpRequest` object, hence byte-identical every attempt. A
        permanent 4xx is never retried. Every exhausted path raises an internal
        :class:`_JudgeFailure` which ``judge()`` turns into ``Decision(ERROR)``.
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
        failure: Optional[str] = None
        for attempt in range(attempts):
            try:
                response = self._transport(request)
            except HttpTransportError as error:
                if not error.retryable:
                    raise _JudgeFailure(_redact(str(error), api_key)) from error
                failure = (
                    f"the request timed out after {self._timeout}s"
                    if error.timed_out
                    else _redact(str(error), api_key)
                )
            else:
                status = response.status_code
                if 200 <= status < 300:
                    return response
                failure = self._status_message(status, response, api_key)
                if not is_retryable_status(status):
                    # permanent: a bad key, a bad request, a missing model, ...
                    raise _JudgeFailure(failure)
            if attempt + 1 < attempts:
                self._sleep(self._delay_for(attempt))
        if failure is None:  # pragma: no cover - unreachable by construction
            raise _JudgeFailure("the judge request failed without a cause")
        raise _JudgeFailure(failure)

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
        return f"the judge request failed with HTTP {status}{suffix}"

    # -- response parsing ---------------------------------------------------
    def _parse_envelope(self, response: HttpResponse, secret: str) -> dict:
        """The chat-completions envelope as a JSON object."""
        try:
            data = response.json()
        except ValueError as error:
            raise _JudgeFailure(
                "the judge returned a body that is not JSON: "
                + _excerpt(_redact(response.text(), secret))
            ) from error
        if not isinstance(data, dict):
            raise _JudgeFailure(
                f"the judge response must be a JSON object; got "
                f"{type(data).__name__}"
            )
        return data

    @staticmethod
    def _content_of(envelope: Mapping[str, Any], secret: str) -> str:
        """The assistant message text from ``choices[0]`` - never redacted."""
        choices = envelope.get("choices")
        if not isinstance(choices, list) or not choices:
            raise _JudgeFailure(
                "the judge response carries no choices: "
                + _excerpt(_redact(json.dumps(envelope, default=str), secret))
            )
        first = choices[0]
        message = first.get("message") if isinstance(first, Mapping) else None
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, str) or not content.strip():
            raise _JudgeFailure("the judge response carries no assistant content")
        return content

    # -- contract -> domain Decision ---------------------------------------
    def _parse_verdict(
        self, content: str, conflict: JudgeConflict, secret: str
    ) -> _JudgeVerdict:
        """Validate the judge contract - including every cited finding id."""
        text = content.strip()
        try:
            contract = json.loads(text)
        except ValueError as error:
            raise _JudgeFailure(
                "the judge answer is not JSON: " + _excerpt(_redact(text, secret))
            ) from error
        if not isinstance(contract, Mapping):
            raise _JudgeFailure(
                "the judge answer must be a JSON object; got "
                f"{type(contract).__name__}"
            )
        raw_status = contract.get("status")
        token = raw_status.strip().upper() if isinstance(raw_status, str) else ""
        status = _VERDICT_STATUSES.get(token)
        if status is None:
            raise _JudgeFailure(
                'the judge answer field "status" must be "ACCEPTED", "REJECTED" '
                f'or "ABSTAIN"; got {_excerpt(str(raw_status), 40)!r}'
            )
        if status is DecisionStatus.ABSTAIN:
            reason = contract.get("reason")
            raise _JudgeAbstain(reason if isinstance(reason, str) else "")
        rationale = contract.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            raise _JudgeFailure(
                'the judge answer field "rationale" must be a non-empty string'
            )
        evidence_refs = self._evidence_refs_of(contract, conflict)
        relation = (
            "supporting"
            if status is DecisionStatus.ACCEPTED
            else "contradicting"
        )
        target = _target_of(conflict)
        detail = _excerpt(_redact(rationale.strip(), secret))
        return _JudgeVerdict(
            status=status,
            rationale=(
                f"About the explicit evidence conflict on {target} only, the "
                f"judge favors the {relation} relation. This is not a statement "
                "about architecture compliance and the deterministic gate "
                f"verdict is unchanged. Judge rationale: {detail}"
            ),
            evidence_refs=evidence_refs,
        )

    @staticmethod
    def _evidence_refs_of(
        contract: Mapping[str, Any], conflict: JudgeConflict
    ) -> tuple[str, ...]:
        """Every cited id must be one of the supplied findings - nothing else.

        A fabricated, unknown or duplicated reference is a contract defect, so
        the judge result becomes an ``ERROR`` rather than an accepted citation.
        """
        known = {finding.id for finding in conflict.findings}
        raw = contract.get("evidence_refs")
        if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
            raise _JudgeFailure(
                'the judge answer field "evidence_refs" must be a list of '
                "finding ids"
            )
        refs: list[str] = []
        for entry in raw:
            if not isinstance(entry, str) or not entry.strip():
                raise _JudgeFailure(
                    'every "evidence_refs" entry must be a non-empty string'
                )
            reference = entry.strip()
            if reference not in known:
                raise _JudgeFailure(
                    f"the judge cited {_excerpt(reference, 60)!r}, which is not "
                    "one of the supplied findings"
                )
            if reference in refs:
                raise _JudgeFailure(
                    f"the judge cited {_excerpt(reference, 60)!r} more than once"
                )
            refs.append(reference)
        if not refs:
            raise _JudgeFailure(
                'the judge answer field "evidence_refs" must cite at least one '
                "supplied finding"
            )
        return tuple(refs)

    def _build_decision(
        self,
        conflict: JudgeConflict,
        *,
        status: DecisionStatus,
        rationale: str,
        evidence_refs: tuple[str, ...],
    ) -> Decision:
        """The domain Decision - scoped to this conflict and nothing else."""
        target = _target_of(conflict)
        step_no = _shared_step_no(conflict)
        cited = set(evidence_refs)
        return Decision(
            id=self._decision_id(conflict, status, step_no),
            status=status,
            decision=f"{_OUTCOME_TEXT[status]} (conflict on {target})",
            rationale=rationale,
            rules_applied=(),
            evidence_refs=evidence_refs,
            perspectives=tuple(
                sorted(
                    {
                        finding.source
                        for finding in conflict.findings
                        if finding.id in cited
                    }
                )
            ),
            step_no=step_no,
            created_at=self._clock(),
        )

    @staticmethod
    def _decision_id(
        conflict: JudgeConflict,
        status: DecisionStatus,
        step_no: Optional[int],
    ) -> str:
        """Deterministic, content-addressed id (the clock is never hashed)."""
        parts = [
            _target_of(conflict),
            *sorted(finding.id for finding in conflict.findings),
            status.value,
        ]
        digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]
        return f"judge-{0 if step_no is None else step_no}-{digest}"

    # -- cost telemetry (secondary, never fatal) ---------------------------
    def _record_cost(
        self, envelope: Mapping[str, Any], conflict: JudgeConflict
    ) -> None:
        """Record cost telemetry for a billable judging call - never fatal.

        Only ever called for a real 2xx answer: a failed attempt carries no
        ``usage`` and produces no record, so a retry that finally succeeds
        contributes exactly one. A failing sink is surfaced through
        ``last_telemetry_error`` - it can never turn a valid judge ``Decision``
        into an error.

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
                    provider=OPENAI_PROVIDER,
                    event_id=_cost_event_id(OPENAI_PROVIDER, envelope),
                    model=self._model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=self._price_for(self._model).cost(
                        input_tokens, output_tokens
                    ),
                    pricing_known=self._model in self._pricing,
                    project=self._project,
                    step_no=_shared_step_no(conflict),
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
        except Exception:  # a failing hook must not fail the judge either
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


def _target_of(conflict: JudgeConflict) -> str:
    """The conflict's declared target, or a documented neutral fallback."""
    return str(
        dict(conflict.context).get("target", "") or OPENAI_JUDGE_FALLBACK_TARGET
    )


def _shared_step_no(conflict: JudgeConflict) -> Optional[int]:
    """The single step every supplied finding belongs to, when they agree."""
    steps = {finding.step_no for finding in conflict.findings}
    if len(steps) == 1:
        return next(iter(steps))
    return None


def _id_list(value: Any) -> str:
    """A stable, human-readable rendering of an id list for the prompt."""
    if isinstance(value, (list, tuple)):
        entries = [str(item) for item in value]
        return ", ".join(entries) if entries else "none"
    return "none"
