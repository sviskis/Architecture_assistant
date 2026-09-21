"""Judge use-case - resolves ONLY genuine, explicit evidence conflicts.

The judge is the last resort of the evidence pipeline, and it is deliberately
narrow:

* it is consulted **only** for conflicts the deterministic layer could not
  resolve, i.e. the explicit :class:`EvidenceConflict` entries a Step 15
  :class:`EvidenceView` carries. No conflict means no judge call at all - it is
  structurally impossible to use it "for every decision";
* it is consulted **exactly once per conflict**, never with a combined
  mega-prompt for unrelated conflicts;
* it is **not** a fourth voter, not a majority vote and not a provider ranking.
  It gets one conflict and answers about that conflict;
* its answer is a **separate layer**. The authoritative deterministic gate
  :class:`Decision <architecture_assistant.domain.models.Decision>` is not
  touched, replaced or merged with here - the caller inspects the gate decision,
  the :class:`EvidenceView` and this :class:`JudgeRun` side by side.

Provider neutrality
-------------------
This module imports the ``JudgePort`` contract and nothing else that knows a
vendor. Which provider backs the judge (OpenAI today, a Claude judge later) is a
composition decision; the application layer never learns it and never imports a
concrete adapter.

Corruption is refused before the judge is ever called: a conflict that is not
part of the view, an unknown finding id, a duplicated reference or findings from
different steps all raise :class:`JudgeError` while building the conflict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..domain.models import Decision, Finding
from ..ports.capabilities import JudgeConflict, JudgePort
from .evidence_merger import EvidenceConflict, EvidenceView

__all__ = [
    "JudgeError",
    "JudgeJudgment",
    "JudgeRun",
    "build_judge_conflict",
    "JudgeUseCase",
]


class JudgeError(ValueError):
    """The judge layer was handed input it cannot honestly process."""


def _question(conflict: EvidenceConflict) -> str:
    """The same deterministic question template the merger already produced."""
    return (
        f"Unresolved evidence conflict on {conflict.target}: supporting "
        f"{', '.join(conflict.supporting_ids)} vs contradicting "
        f"{', '.join(conflict.contradicting_ids)}"
    )


def _finding_of(view: EvidenceView, finding_id: str, field_name: str) -> Finding:
    """Resolve exactly one finding by id - anything else is corruption."""
    matches = [finding for finding in view.findings if finding.id == finding_id]
    if not matches:
        raise JudgeError(
            f"{field_name} references {finding_id!r}, which is not in the view"
        )
    if len(matches) > 1:
        raise JudgeError(
            f"{field_name} references {finding_id!r} more than once"
        )
    return matches[0]


def build_judge_conflict(
    conflict: EvidenceConflict, view: EvidenceView
) -> JudgeConflict:
    """Turn one merged conflict into the structured conflict a judge receives.

    Pure and fail-closed: every check happens here, *before* any judge call, so a
    corrupt view can never reach a provider. The judge receives exactly the
    findings the conflict names - never the whole evidence set, never a chat
    history and never anything the conflict did not reference.
    """
    if not isinstance(conflict, EvidenceConflict):
        raise JudgeError(
            f"conflict must be an EvidenceConflict; got {conflict!r}"
        )
    if not isinstance(view, EvidenceView):
        raise JudgeError(f"view must be an EvidenceView; got {view!r}")
    if conflict not in view.conflicts:
        raise JudgeError(
            "the conflict is not one of the merged conflicts of this view"
        )
    if not conflict.target.strip():
        raise JudgeError("a conflict must name a target")
    if not conflict.supporting_ids or not conflict.contradicting_ids:
        raise JudgeError(
            "a conflict must name at least one supporting and one contradicting "
            "finding"
        )

    supporting = tuple(
        _finding_of(view, finding_id, "supporting_ids")
        for finding_id in conflict.supporting_ids
    )
    contradicting = tuple(
        _finding_of(view, finding_id, "contradicting_ids")
        for finding_id in conflict.contradicting_ids
    )
    findings = supporting + contradicting
    steps = {finding.step_no for finding in findings}
    if len(steps) != 1:
        raise JudgeError(
            "every finding of a conflict must belong to the same step; got "
            f"{sorted(str(step) for step in steps)}"
        )
    return JudgeConflict(
        question=_question(conflict),
        findings=findings,
        context={
            "target": conflict.target,
            "supporting_ids": list(conflict.supporting_ids),
            "contradicting_ids": list(conflict.contradicting_ids),
            "gate_status": (
                None if view.gate_status is None else view.gate_status.value
            ),
        },
    )


@dataclass(frozen=True)
class JudgeJudgment:
    """One explicit conflict and the judge's separate resolution of it."""

    conflict: EvidenceConflict
    decision: Decision

    def __post_init__(self) -> None:
        if not isinstance(self.conflict, EvidenceConflict):
            raise JudgeError(
                f"conflict must be an EvidenceConflict; got {self.conflict!r}"
            )
        if not isinstance(self.decision, Decision):
            raise JudgeError(
                f"decision must be a Decision; got {self.decision!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "conflict": self.conflict.to_dict(),
            "decision": self.decision.to_dict(),
        }


@dataclass(frozen=True)
class JudgeRun:
    """The judge layer: one judgment per explicit conflict, kept separately.

    ``judge_available`` is ``False`` when no judge implementation was injected -
    a clearly explicit unavailable state rather than a silent empty success.
    """

    judgments: tuple[JudgeJudgment, ...] = ()
    judge_available: bool = True

    def __post_init__(self) -> None:
        judgments = tuple(self.judgments)
        for judgment in judgments:
            if not isinstance(judgment, JudgeJudgment):
                raise JudgeError(
                    f"judgments must contain JudgeJudgment; got {judgment!r}"
                )
        object.__setattr__(self, "judgments", judgments)
        if not isinstance(self.judge_available, bool):
            raise JudgeError(
                f"judge_available must be a bool; got {self.judge_available!r}"
            )

    @property
    def count(self) -> int:
        """How many conflicts the judge was actually asked about."""
        return len(self.judgments)

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "judge_available": self.judge_available,
            "count": self.count,
            "judgments": [judgment.to_dict() for judgment in self.judgments],
        }


class JudgeUseCase:
    """Resolve explicit evidence conflicts through an injected ``JudgePort``.

    The use-case depends on the *port* only: which provider implements it is
    decided at composition time, so the application layer stays provider-neutral
    and the judge stays optional.
    """

    def __init__(self, judge: Optional[JudgePort] = None) -> None:
        if judge is not None and not callable(getattr(judge, "judge", None)):
            raise JudgeError("judge must implement JudgePort.judge(conflict)")
        self._judge = judge

    @property
    def judge(self) -> Optional[JudgePort]:
        """The injected judge implementation, if any."""
        return self._judge

    @property
    def is_available(self) -> bool:
        """Whether a judge implementation is injected at all."""
        return self._judge is not None

    def resolve(self, view: EvidenceView) -> JudgeRun:
        """Resolve every explicit conflict in the view - once each, nothing else.

        With no conflict the judge is not called at all. With two conflicts it is
        called twice, independently: unrelated conflicts are never combined. The
        authoritative deterministic decision is not returned, changed or merged -
        this run is a separate layer.
        """
        if not isinstance(view, EvidenceView):
            raise JudgeError(f"view must be an EvidenceView; got {view!r}")
        if self._judge is None:
            return JudgeRun(judgments=(), judge_available=False)
        judgments = tuple(
            JudgeJudgment(
                conflict=conflict,
                decision=self._judge.judge(build_judge_conflict(conflict, view)),
            )
            for conflict in view.conflicts
        )
        return JudgeRun(judgments=judgments, judge_available=True)
