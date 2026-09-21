"""Decision engine - one authoritative, explainable Decision.

The engine is the only place where advisor evidence and the deterministic
architecture gate meet, and the meeting is deliberately one-sided:

* the **deterministic gate is authoritative**. A REJECTED verdict stays REJECTED
  whatever the advisors said. An ACCEPTED verdict states that the
  *deterministic architecture checks* passed - it is not a claim that the design
  is correct, that the advisors approve, or that the implementation is free of
  risks;
* advisors are **consultative and never a vote**. Their findings are never
  counted into the outcome: no majority, no confidence total, no provider
  ranking, no tie-break. Counts appear in the rationale only as a description of
  what was recorded;
* with **no deterministic verdict** the engine refuses to invent one: every
  advisor execution failing is an ERROR and anything else is an ABSTAIN. Three
  identical SUPPORTING findings and no gate are still not an ACCEPTED.

The result is a :class:`DecisionResult` - the authoritative domain
:class:`Decision <architecture_assistant.domain.models.Decision>` *plus* the full
:class:`EvidenceView <architecture_assistant.application.evidence_merger.EvidenceView>`,
so the outcome is explainable without ever hiding an advisor concern.

Nothing here is persisted: the engine and the merger are pure semantics, and
workflow integration belongs to later orchestration work. The engine is pure in
the same way - no adapter call, no infrastructure exception, no network, no
persistence; only the clock is injectable.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional, Sequence

from ..domain.enums import DecisionStatus
from ..domain.models import Decision, utc_now
from .evidence_merger import (
    AdvisorObservation,
    EvidenceView,
    merge_evidence,
)

__all__ = [
    "DecisionEngineError",
    "DecisionResult",
    "synthesize_decision",
    "decide_from_observations",
]

#: Statuses a deterministic realization gate can hand over.
_SETTLED_STATUSES = (DecisionStatus.ACCEPTED, DecisionStatus.REJECTED)


class DecisionEngineError(ValueError):
    """The engine was handed input it cannot honestly turn into a decision."""


@dataclass(frozen=True)
class DecisionResult:
    """The full explainability object: the decision *and* the evidence it saw.

    ``decision`` is the authoritative domain outcome; ``evidence`` keeps every
    advisor finding - including the unresolved ones - so nothing an advisor said
    is hidden by the summary.
    """

    decision: Decision
    evidence: EvidenceView

    def __post_init__(self) -> None:
        if not isinstance(self.decision, Decision):
            raise DecisionEngineError(
                f"decision must be a Decision; got {self.decision!r}"
            )
        if not isinstance(self.evidence, EvidenceView):
            raise DecisionEngineError(
                f"evidence must be an EvidenceView; got {self.evidence!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "decision": self.decision.to_dict(),
            "evidence": self.evidence.to_dict(),
        }


def _validated_gate(
    gate: Optional[Decision], evidence: EvidenceView
) -> Optional[Decision]:
    """The settled verdict, cross-checked against the evidence it produced.

    A gate must be consistent with the view it is combined with: mixing a view
    merged against one verdict with a different (or absent) verdict would let a
    relation be validated by anchors that never applied to it.
    """
    if gate is None:
        if evidence.gate_status is not None:
            raise DecisionEngineError(
                "this evidence was merged against a gate verdict, so the gate "
                "must be passed to the engine as well"
            )
        return None
    if not isinstance(gate, Decision):
        raise DecisionEngineError(
            f"gate must be a Decision or None; got {gate!r}"
        )
    if gate.status not in _SETTLED_STATUSES:
        raise DecisionEngineError(
            "a deterministic gate verdict must be ACCEPTED or REJECTED; got "
            f"{gate.status.value!r}"
        )
    if evidence.gate_status is None:
        raise DecisionEngineError(
            "the evidence was merged without a gate verdict, so no relation in "
            "it could have been validated"
        )
    if evidence.gate_status is not gate.status:
        raise DecisionEngineError(
            f"gate verdict {gate.status.value!r} does not match the verdict the "
            f"evidence was merged against ({evidence.gate_status.value!r})"
        )
    return gate


def _status(
    gate: Optional[Decision], evidence: EvidenceView
) -> DecisionStatus:
    """The authoritative status - the gate first, and never a vote count.

    With no settled verdict there is exactly one distinction left: whether every
    advisor execution failed (``ERROR``) or whether the evidence simply is not
    adjudicable (``ABSTAIN``).
    """
    if gate is not None:
        return gate.status
    if evidence.errors and not evidence.findings and not evidence.abstained:
        return DecisionStatus.ERROR
    return DecisionStatus.ABSTAIN


def _decision_text(status: DecisionStatus) -> str:
    """The short, deterministic outcome phrase."""
    if status is DecisionStatus.ACCEPTED:
        return (
            "accepted: the implementation satisfies the deterministic "
            "architecture checks"
        )
    if status is DecisionStatus.REJECTED:
        return (
            "rejected: the implementation violates the deterministic "
            "architecture checks"
        )
    if status is DecisionStatus.ERROR:
        return "no decision: every advisor execution failed"
    return (
        "no decision: no deterministic verdict and no adjudicable advisor "
        "evidence"
    )


def _counts(evidence: EvidenceView) -> str:
    """A descriptive tally of what was recorded - never an input to the outcome."""
    return (
        f"{len(evidence.supporting_ids)} supporting, "
        f"{len(evidence.conflicting_ids)} conflicting, "
        f"{len(evidence.unresolved_ids)} unresolved, "
        f"{len(evidence.abstained)} abstention(s) and "
        f"{len(evidence.errors)} failure(s)"
    )


def _rationale(
    status: DecisionStatus, gate: Optional[Decision], evidence: EvidenceView
) -> str:
    """Why this outcome - with ACCEPTED explicitly scoped to the gate."""
    tally = _counts(evidence)
    if status is DecisionStatus.ACCEPTED:
        rules = ", ".join(gate.rules_applied) or "no rule recorded"
        return (
            "The deterministic architecture gate accepted the implementation "
            f"against rules ({rules}). This records deterministic architecture "
            "compliance only: it does not mean the design is correct, that the "
            "AI advisors approve, or that the implementation carries no risks. "
            "Advisors are consultative, never a vote, so their findings were "
            f"not counted into the outcome: {tally}. Every finding, including "
            "the unresolved ones, remains visible in the evidence view."
        )
    if status is DecisionStatus.REJECTED:
        rules = ", ".join(gate.rules_applied) or "no rule recorded"
        violations = len(gate.evidence_refs)
        return (
            "The deterministic architecture gate rejected the implementation "
            f"against rules ({rules}) with {violations} violation finding(s). A "
            "deterministic violation outranks every advisor, so advisor findings "
            "were consultative only and could not change this outcome: "
            f"{tally}. Every finding remains visible in the evidence view."
        )
    if status is DecisionStatus.ERROR:
        return (
            "There is no deterministic architecture verdict, and every advisor "
            f"execution failed, so there is nothing to weigh: {tally}. A failed "
            "execution is not evidence and is not a rejection - here no advisor "
            "produced a finding at all."
        )
    return (
        "There is no deterministic architecture verdict, so nothing can be "
        "stated about architecture compliance. Advisor findings are consultative "
        "and are never a vote, and no relation to a gate anchor could be "
        f"validated: {tally}. The outcome is an abstention, not a rejection."
    )


def _evidence_refs(
    status: DecisionStatus, gate: Optional[Decision], evidence: EvidenceView
) -> tuple[str, ...]:
    """Authoritative/supporting refs only - the archive lives in the view.

    A deterministic violation's own finding ids come first (they are the
    authoritative evidence), then the validated supporting findings. Unresolved
    and conflicting findings are deliberately not referenced as backing: they do
    not back anything yet, and they stay visible in the evidence view instead.
    """
    if gate is None:
        return ()
    ordered: list[str] = []
    if status is DecisionStatus.REJECTED:
        ordered.extend(gate.evidence_refs)
    ordered.extend(evidence.supporting_ids)
    seen: set[str] = set()
    result: list[str] = []
    for entry in ordered:
        if entry not in seen:
            seen.add(entry)
            result.append(entry)
    return tuple(result)


def _decision_id(
    status: DecisionStatus,
    step_no: Optional[int],
    gate: Optional[Decision],
    evidence: EvidenceView,
) -> str:
    """A deterministic, content-addressed id (the clock is never hashed)."""
    parts = [
        status.value,
        "" if step_no is None else str(step_no),
        "" if gate is None else gate.id,
        *evidence.supporting_ids,
        *evidence.conflicting_ids,
        *evidence.unresolved_ids,
    ]
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]
    return f"advisor-decision-{0 if step_no is None else step_no}-{digest}"


def synthesize_decision(
    evidence: EvidenceView,
    *,
    gate: Optional[Decision] = None,
    decision_id: Optional[str] = None,
    step_no: Optional[int] = None,
    clock: Callable[[], datetime] = utc_now,
) -> DecisionResult:
    """Turn a merged evidence view into one authoritative Decision.

    The deterministic gate decides the status; the evidence only explains it.
    Nothing an advisor said - how many agreed, how confident they were, which
    provider they were - can move the outcome.
    """
    if not isinstance(evidence, EvidenceView):
        raise DecisionEngineError(
            f"evidence must be an EvidenceView; got {evidence!r}"
        )
    verdict = _validated_gate(gate, evidence)
    if step_no is not None and (
        isinstance(step_no, bool) or not isinstance(step_no, int) or step_no < 1
    ):
        raise DecisionEngineError(
            f"step_no must be an int >= 1 or None; got {step_no!r}"
        )
    if decision_id is not None and (
        not isinstance(decision_id, str) or not decision_id.strip()
    ):
        raise DecisionEngineError(
            f"decision_id must be a non-empty string or None; got {decision_id!r}"
        )
    if not callable(clock):
        raise DecisionEngineError("clock must be a callable returning a datetime")

    status = _status(verdict, evidence)
    decision = Decision(
        id=decision_id or _decision_id(status, step_no, verdict, evidence),
        status=status,
        decision=_decision_text(status),
        rationale=_rationale(status, verdict, evidence),
        rules_applied=() if verdict is None else verdict.rules_applied,
        evidence_refs=_evidence_refs(status, verdict, evidence),
        perspectives=evidence.sources,
        step_no=step_no,
        created_at=clock(),
    )
    return DecisionResult(decision=decision, evidence=evidence)


def decide_from_observations(
    observations: Sequence[AdvisorObservation],
    *,
    gate: Optional[Decision] = None,
    decision_id: Optional[str] = None,
    step_no: Optional[int] = None,
    clock: Callable[[], datetime] = utc_now,
) -> DecisionResult:
    """Merge then decide - the convenience path over the two pure steps."""
    evidence = merge_evidence(observations, gate=gate)
    return synthesize_decision(
        evidence,
        gate=gate,
        decision_id=decision_id,
        step_no=step_no,
        clock=clock,
    )
