"""The log-event contract: one validated, sanitized, JSON-safe shape.

Why this module exists
----------------------
The operator panel needs visibility into what the assistant is doing - above all
into an advisory architecture review, whose stages are otherwise invisible. A log
event is **not** the audit trail: the audit trail is persistent domain state,
while an event is ephemeral, operator-facing and never a source of truth. Nothing
in this module writes anywhere, and nothing here deletes anything.

Nothing here is a logging framework either. There is no global logger, no
handler, no formatter, no level filter and no configuration. The project issues
**no** ``logging`` call at all, and installing one process-wide logger would be
the wrong trade for this product:

* a process-wide logger is shared state: two assistant instances in one process
  (the test suite composes many) would share it, so the per-run event **order**
  that the panel promises would stop being deterministic;
* a global handler would also decide *where* events go, while this architecture
  requires that the core thread only puts plain data into a queue owned by the
  GUI worker, and that the Tk thread only drains and renders that queue;
* the core must stay observable exactly through its injected seams - never
  through a global name that another product or a test could silently intercept.

So an event is a validated ``dict`` handed to an injected callback: the review
takes ``on_event``, the GUI worker owns the
queue, and this module is the single definition of the shape, the vocabulary and
the sanitization rules. Levels use the familiar names, with ``WARN`` as the
operator-facing name of a warning.

Security rules, enforced here instead of trusted to convention
-------------------------------------------------------------
* a message is **sanitized**: credentials, authorization headers and key
  assignments are replaced by ``***REDACTED***``;
* a message is **collapsed** to a single line and **truncated**, so a provider
  body or a runaway exception text can never be pasted into the panel;
* only an exception **type name** is ever logged for a failure
  (:func:`exception_reason`) - never its message, which may quote a request.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

from ..domain.models import utc_now

__all__ = [
    "EVENT_LEVEL_INFO",
    "EVENT_LEVEL_WARN",
    "EVENT_LEVEL_ERROR",
    "EVENT_LEVELS",
    "LOG_COMPONENTS",
    "LOG_MESSAGE_LIMIT",
    "LOG_ACTION_LIMIT",
    "LOG_REVIEW_ID_LIMIT",
    "EVENT_FIELDS",
    "REDACTION_MARK",
    "LogEventError",
    "sanitize_text",
    "exception_reason",
    "component_for",
    "build_event",
]

#: The three levels the panel renders. ``WARN`` is a warning.
EVENT_LEVEL_INFO = "INFO"
EVENT_LEVEL_WARN = "WARN"
EVENT_LEVEL_ERROR = "ERROR"
EVENT_LEVELS = (EVENT_LEVEL_INFO, EVENT_LEVEL_WARN, EVENT_LEVEL_ERROR)

#: Every component the assistant itself logs under. An unknown provider source is
#: still shown - sanitized, never hidden - but the assistant's own vocabulary is
#: exactly this tuple, and the tests pin it.
LOG_COMPONENTS = (
    "GUI",
    "CoreWorker",
    "Gate",
    "OpenAI",
    "Claude",
    "Grok",
    "Merger",
    "Decision",
    "Judge",
    "Cost",
    "Synthesis",
    "Supervisor",
)

#: How long a rendered message may be, how long an action name may be, and how
#: much of a review id is kept.
LOG_MESSAGE_LIMIT = 400
LOG_ACTION_LIMIT = 48
LOG_REVIEW_ID_LIMIT = 96

#: What replaces anything that looks like a credential.
REDACTION_MARK = "***REDACTED***"

#: The exact key set of one event - pinned by the tests.
EVENT_FIELDS = (
    "seq",
    "timestamp",
    "level",
    "component",
    "action",
    "message",
    "step_no",
    "review_id",
)

#: Provider / stage sources -> the component the panel shows for them.
_COMPONENTS: dict[str, str] = {
    "openai": "OpenAI",
    "claude": "Claude",
    "grok": "Grok",
    "deepseek": "DeepSeek",
    "disabled": "Disabled",
    "gate": "Gate",
    "validator": "Gate",
    "architecture-validator": "Gate",
    "realization": "Gate",
    "merger": "Merger",
    "evidence": "Merger",
    "evidence-merger": "Merger",
    "decision": "Decision",
    "decision-engine": "Decision",
    "judge": "Judge",
    "cost": "Cost",
    "gui": "GUI",
    "ui": "GUI",
    "coreworker": "CoreWorker",
    "core-worker": "CoreWorker",
    "core": "CoreWorker",
    "review": "CoreWorker",
    "worker": "CoreWorker",
    "orchestrator": "CoreWorker",
    "scheduler": "CoreWorker",
    "synthesis": "Synthesis",
    "synthesizer": "Synthesis",
    "proposal": "Synthesis",
    "proposals": "Synthesis",
    "supervisor": "Supervisor",
    "supervision": "Supervisor",
    "supervisor-runtime": "Supervisor",
}

#: Anything that smells like a credential, and what it becomes. Order matters:
#: header/key assignments first, then bare tokens.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        # a header or key assignment, including an optional scheme and its token:
        # ``Authorization: Bearer abc123`` must lose *both* parts
        re.compile(
            r"(?i)\b(authorization|x-api-key|api[_-]?key|apikey)\b"
            r"\s*[:=]\s*((?:bearer|basic)\s+)?(\"[^\"]*\"|'[^']*'|\S+)"
        ),
        rf"\1={REDACTION_MARK}",
    ),
    (
        re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+/=]{6,}"),
        rf"\1 {REDACTION_MARK}",
    ),
    (
        re.compile(r"\bsk-[A-Za-z0-9_\-]{4,}", re.IGNORECASE),
        f"sk-{REDACTION_MARK}",
    ),
    (
        re.compile(r"(?i)\bkey\s*=\s*[\"']?[A-Za-z0-9._\-]{12,}"),
        f"key={REDACTION_MARK}",
    ),
)


class LogEventError(ValueError):
    """A log event was built from something the contract cannot carry."""


def sanitize_text(value: Any, *, limit: int = LOG_MESSAGE_LIMIT) -> str:
    """Make any value safe to render: redacted, single-line and bounded.

    Idempotent on purpose: an already sanitized message passes through unchanged,
    so a message can be re-validated at every boundary without drifting.
    """
    text = "" if value is None else str(value)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    # collapse newlines, tabs and repeated spaces: a provider body or a traceback
    # must never be pasted into the panel as-is
    text = " ".join(text.split())
    if limit and len(text) > limit:
        text = text[: max(limit - 1, 1)].rstrip() + "…"
    return text


def exception_reason(error: BaseException) -> str:
    """The **type name** of a failure - never its message, which may leak.

    ``str(error)`` can quote a URL, a header or a request body, so it is never
    allowed into an event; the type name is stable, non-sensitive and enough for
    an operator to act on.
    """
    return type(error).__name__


def component_for(source: str) -> str:
    """The log component for a provider or stage source.

    A known source maps to its canonical component; an unknown one is kept -
    sanitized - instead of being silently merged into a component it is not.
    """
    text = sanitize_text(source, limit=64).strip()
    if not text:
        raise LogEventError("a log component source must not be blank")
    return _COMPONENTS.get(text.casefold(), text)


def build_event(
    *,
    level: str,
    component: str,
    action: str,
    message: str,
    step_no: Optional[int] = None,
    review_id: Optional[str] = None,
    timestamp: Optional[datetime] = None,
    seq: Optional[int] = None,
) -> dict[str, Any]:
    """One validated, sanitized, JSON-safe log event.

    Raises :class:`LogEventError` for anything the panel could not render or could
    not trust: an unknown level, a blank field, a nonsense step or sequence
    number. The result contains only ``str`` / ``int`` / ``None`` values, so it
    survives a JSON round trip unchanged.
    """
    if level not in EVENT_LEVELS:
        raise LogEventError(f"level must be one of {EVENT_LEVELS}; got {level!r}")
    component_text = sanitize_text(component, limit=64)
    if not component_text:
        raise LogEventError("component must not be blank")
    action_text = sanitize_text(action, limit=LOG_ACTION_LIMIT)
    if not action_text:
        raise LogEventError("action must not be blank")
    message_text = sanitize_text(message)
    if not message_text:
        raise LogEventError("message must not be blank")
    if step_no is not None and (
        isinstance(step_no, bool) or not isinstance(step_no, int) or step_no < 1
    ):
        raise LogEventError(f"step_no must be an int >= 1 or None; got {step_no!r}")
    if seq is not None and (
        isinstance(seq, bool) or not isinstance(seq, int) or seq < 0
    ):
        raise LogEventError(f"seq must be an int >= 0 or None; got {seq!r}")
    if timestamp is None:
        stamp = utc_now()
    elif isinstance(timestamp, datetime):
        stamp = timestamp
    elif isinstance(timestamp, str):
        # re-validating an event that already carries its ISO stamp must work:
        # the queue owner re-checks every field without changing the moment
        try:
            stamp = datetime.fromisoformat(timestamp)
        except ValueError as error:
            raise LogEventError(
                f"timestamp must be a datetime or an ISO string; got {timestamp!r}"
            ) from error
    else:
        raise LogEventError(
            f"timestamp must be a datetime or None; got {timestamp!r}"
        )
    review_text = (
        None
        if review_id is None
        else (sanitize_text(review_id, limit=LOG_REVIEW_ID_LIMIT) or None)
    )
    return {
        "seq": seq,
        "timestamp": stamp.isoformat(),
        "level": level,
        "component": component_text,
        "action": action_text,
        "message": message_text,
        "step_no": step_no,
        "review_id": review_text,
    }
