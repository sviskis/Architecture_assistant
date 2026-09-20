"""Pure, deterministic loop policies.

Two decisions of the assistant <-> Cline loop are policy, not mechanism, and
they are therefore pure functions of domain state - trivially testable and
impossible to smuggle provider knowledge into:

* :func:`requires_approval` - does this step need a human ``APPROVE`` before it
  may be dispatched, given the project mode?
* :func:`decide_review` - which FSM event does a received report map to?

Neither function reads storage, the clock or any adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..domain.enums import Mode, ReportStatus, RiskLevel, StepEvent
from ..ports.capabilities import WorkerResult

__all__ = [
    "requires_approval",
    "ReviewOutcome",
    "decide_review",
]


def requires_approval(
    mode: Mode, risk: RiskLevel, requires_human: bool
) -> bool:
    """Whether a step must wait for human approval before dispatch.

    The approval policy is exactly the declared operating-mode semantics:

    * ``MANUAL`` - every step waits for approval;
    * ``SUPERVISED`` - ``LOW`` steps without an explicit human requirement may
      be dispatched automatically, everything else waits;
    * ``AUTO`` - ``LOW``/``MEDIUM`` steps without an explicit human requirement
      may be dispatched automatically, ``HIGH`` steps and any step that
      requires a human wait.

    An explicit ``requires_human`` flag always wins, in every mode.
    """
    if not isinstance(mode, Mode):
        raise ValueError(f"mode must be a Mode; got {mode!r}")
    if not isinstance(risk, RiskLevel):
        raise ValueError(f"risk must be a RiskLevel; got {risk!r}")
    if requires_human:
        return True
    if mode is Mode.MANUAL:
        return True
    if mode is Mode.SUPERVISED:
        return risk is not RiskLevel.LOW
    return risk is RiskLevel.HIGH  # Mode.AUTO


@dataclass(frozen=True)
class ReviewOutcome:
    """The deterministic FSM event a received report maps to."""

    event: StepEvent
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {"event": self.event.value, "reason": self.reason}


def decide_review(
    report: WorkerResult, *, attempt: int, max_attempts: int
) -> ReviewOutcome:
    """Map a provider-neutral report onto a review FSM event.

    The returned event is always legal **from** ``REVIEWING``: that state has
    exactly four exits (``VERIFY``, ``REQUEST_REVISE``, ``BLOCK``,
    ``RAISE_CONFLICT``), so a worker failure is expressed as a retry while
    attempts remain and as a human escalation once they are used up - never as
    a transition the authoritative FSM does not have.

    Ordering is deliberate: an architecture question or an unresolved issue is
    always a human decision, whatever the report status claims.
    """
    if not isinstance(report, WorkerResult):
        raise ValueError(f"report must be a WorkerResult; got {report!r}")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
        raise ValueError(f"attempt must be an int >= 0; got {attempt!r}")
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or max_attempts < 1
    ):
        raise ValueError(
            f"max_attempts must be an int >= 1; got {max_attempts!r}"
        )

    retryable = attempt < max_attempts

    if report.architecture_questions:
        return ReviewOutcome(StepEvent.BLOCK, "architecture-questions")
    if report.issues:
        return ReviewOutcome(StepEvent.BLOCK, "unresolved-issues")

    status = report.status
    if status is ReportStatus.DONE:
        return ReviewOutcome(StepEvent.VERIFY, "report-done")
    if status is ReportStatus.BLOCKED:
        return ReviewOutcome(StepEvent.BLOCK, "report-blocked")
    if status is ReportStatus.REVISE:
        if retryable:
            return ReviewOutcome(StepEvent.REQUEST_REVISE, "report-revise")
        return ReviewOutcome(StepEvent.BLOCK, "revise-attempts-exhausted")
    if status is ReportStatus.FAILED:
        if retryable:
            return ReviewOutcome(
                StepEvent.REQUEST_REVISE, "report-failed-retry"
            )
        return ReviewOutcome(StepEvent.BLOCK, "failed-attempts-exhausted")
    raise ValueError(f"unsupported report status {status!r}")
