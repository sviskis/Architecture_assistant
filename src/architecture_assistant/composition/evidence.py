"""Convert provider adapter outcomes into provider-neutral observations.

This is the wiring seam of Step 15, and it is the **only** place that knows the
provider-specific advisor exception hierarchies. That is exactly what keeps the
application layer free of infrastructure knowledge: the merger and the decision
engine accept :class:`AdvisorObservation` and never see an
``OpenAIAdvisorError``, a ``ClaudeAdvisorError`` or a ``GrokAdvisorError``.

Secrets
-------
A reason is built from a fixed phrase plus the exception *type name* - never from
``repr(error)`` and never from the exception message. The adapters do redact
their own messages, but this seam must not depend on that promise: a provider
failure that already carried something sensitive must not be able to push it
into a report.

Relation
--------
``relation`` is **explicit metadata**. The current adapters report no relation at
all, so :func:`observe` defaults to :attr:`EvidenceRelation.UNRESOLVED` with no
target, and the merger honours anything stronger only when it can be validated
structurally against the deterministic gate.
"""

from __future__ import annotations

from typing import Callable, Optional

from ..application import (
    AdvisorObservation,
    EvidenceRelation,
    ObservationStatus,
)
from ..domain.models import Finding
from ..infrastructure import (
    ClaudeAdvisorAbstainError,
    ClaudeAdvisorError,
    DeepSeekAdvisorAbstainError,
    DeepSeekAdvisorError,
    DisabledAdvisorAbstainError,
    GrokAdvisorAbstainError,
    GrokAdvisorError,
    OpenAIAdvisorAbstainError,
    OpenAIAdvisorError,
)

__all__ = [
    "ABSTAIN_ERRORS",
    "ADVISOR_ERRORS",
    "ABSTAIN_REASON_PREFIX",
    "ERROR_REASON_PREFIX",
    "observe",
]

#: A deliberate refusal. Most specific first: these subclass the errors below.
#:
#: ``DisabledAdvisorAbstainError`` belongs here for the same reason the provider
#: abstentions do: an advisor the operator switched off **deliberately** answers
#: nothing, which is an abstention, never a provider failure. It makes no call at
#: all, so classifying it as an error would blame a provider that was never asked.
ABSTAIN_ERRORS: tuple[type[BaseException], ...] = (
    OpenAIAdvisorAbstainError,
    ClaudeAdvisorAbstainError,
    GrokAdvisorAbstainError,
    DeepSeekAdvisorAbstainError,
    DisabledAdvisorAbstainError,
)

#: Every other advisor failure (transport, HTTP, contract, missing key, ...).
ADVISOR_ERRORS: tuple[type[BaseException], ...] = (
    OpenAIAdvisorError,
    ClaudeAdvisorError,
    GrokAdvisorError,
    DeepSeekAdvisorError,
)

#: Stable, non-sensitive reason prefixes.
ABSTAIN_REASON_PREFIX = "advisor abstained"
ERROR_REASON_PREFIX = "advisor failed"


def _reason(prefix: str, error: BaseException) -> str:
    """A safe reason: a fixed phrase plus the exception type name.

    No message text and no ``repr`` - the type name is the only provider detail
    that travels, and it carries no payload.
    """
    return f"{prefix}: {type(error).__name__}"


def observe(
    source: str,
    run: Callable[[], Finding],
    *,
    relation: EvidenceRelation = EvidenceRelation.UNRESOLVED,
    relation_target: Optional[str] = None,
) -> AdvisorObservation:
    """Run one advisor and describe the outcome in provider-neutral terms.

    An abstention becomes ``ABSTAIN`` and any other advisor failure becomes
    ``ERROR``; neither is a rejection, and one failing provider never hides the
    findings of the others - each advisor is observed on its own.
    """
    if not callable(run):
        raise ValueError("run must be a callable returning a Finding")
    try:
        finding = run()
    except ABSTAIN_ERRORS as error:
        return AdvisorObservation(
            source=source,
            status=ObservationStatus.ABSTAIN,
            reason=_reason(ABSTAIN_REASON_PREFIX, error),
        )
    except ADVISOR_ERRORS as error:
        return AdvisorObservation(
            source=source,
            status=ObservationStatus.ERROR,
            reason=_reason(ERROR_REASON_PREFIX, error),
        )
    return AdvisorObservation(
        source=source,
        status=ObservationStatus.FINDING,
        finding=finding,
        relation=relation,
        relation_target=relation_target,
    )
