"""Evidence merger - a lossless, conservative view over advisor observations.

The assistant has three consultative advisors (OpenAI, Claude, Grok) and exactly
one authoritative decision maker: the deterministic architecture rules. This
module builds the bridge between them **without ever letting the advisors vote**.

Three rules define it:

* **Nothing is inferred.** Semantic agreement or disagreement is never derived
  from claim text, severity, confidence, provider identity or evidence overlap.
  Overlap only means "these findings concern the same locator" - nothing more.
* **Unknown beats invented certainty.** A relation between an advisor finding and
  the deterministic gate is honoured only when the caller *declares* it **and** it
  can be validated structurally against the gate's own anchors. Otherwise the
  finding stays :attr:`EvidenceRelation.UNRESOLVED`, which is a perfectly honest
  answer.
* **Nothing is lost.** Every finding is preserved together with its provider, a
  repeated delivery of the *same* finding collapses to one entry, and a delivery
  that reuses an id with different content is an error instead of a silent
  overwrite. Advisor findings are never discarded, reworded or melted together.

The merger is **pure**: no adapter call, no infrastructure exception handling, no
network, no persistence, no clock. Its input is a provider-neutral
:class:`AdvisorObservation` built by ``composition.evidence``, so this layer
never learns a provider adapter's error type.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Optional, Sequence

from ..domain.enums import DecisionStatus
from ..domain.models import Decision, Finding

__all__ = [
    "ObservationStatus",
    "EvidenceRelation",
    "AdvisorObservation",
    "ObservationOutcome",
    "EvidenceConflict",
    "EvidenceView",
    "EvidenceMergerError",
    "EvidenceIdentityCollisionError",
    "merge_evidence",
]


class ObservationStatus(StrEnum):
    """What one advisor execution produced.

    ``FINDING`` - a structured finding; ``ABSTAIN`` - a deliberate refusal;
    ``ERROR`` - the execution failed. An application-layer vocabulary: this is
    not a domain enumeration and it never becomes one.
    """

    FINDING = "FINDING"
    ABSTAIN = "ABSTAIN"
    ERROR = "ERROR"


class EvidenceRelation(StrEnum):
    """The caller-declared relation between a finding and the deterministic gate.

    ``UNRESOLVED`` is the default and the safe answer: no relation was declared,
    or the declared relation could not be validated structurally against the
    gate's anchors. ``SUPPORTING`` / ``CONTRADICTING`` are *explicit metadata*
    supplied by the caller - never an inference made here.
    """

    UNRESOLVED = "UNRESOLVED"
    SUPPORTING = "SUPPORTING"
    CONTRADICTING = "CONTRADICTING"


class EvidenceMergerError(ValueError):
    """The merger was handed input it cannot honestly process."""


class EvidenceIdentityCollisionError(EvidenceMergerError):
    """The same finding id arrived twice with different identity content.

    A finding id is a content hash of provider, model, step, claim and evidence,
    so two *different* findings can never share one. Seeing the same id with a
    different claim/evidence/step means corrupted evidence or a forged id - the
    merger fails closed instead of silently keeping one of them.
    """


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceMergerError(
            f"{field_name} must be a non-empty string; got {value!r}"
        )
    return value


def _optional_text(value: Any, field_name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise EvidenceMergerError(
            f"{field_name} must be a string or None; got {value!r}"
        )
    return value.strip() or None


@dataclass(frozen=True)
class AdvisorObservation:
    """One advisor outcome, in provider-neutral application terms.

    This is the *only* shape the merger accepts. ``composition.evidence``
    converts a provider adapter's result - its ``Finding`` or its specific
    exception - into this DTO, which is what keeps every provider error type out
    of the application layer.

    ``relation`` is **explicit metadata**, never an inference: the current
    adapters carry no relation information, so the conversion always supplies
    :attr:`EvidenceRelation.UNRESOLVED` and lets the merger validate anything
    stronger against the deterministic gate.
    """

    source: str
    status: ObservationStatus
    finding: Optional[Finding] = None
    reason: Optional[str] = None
    relation: EvidenceRelation = EvidenceRelation.UNRESOLVED
    relation_target: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _text(self.source, "source"))
        try:
            status = ObservationStatus(self.status)
        except ValueError as error:
            raise EvidenceMergerError(
                f"status must be one of "
                f"({', '.join(member.value for member in ObservationStatus)}); "
                f"got {self.status!r}"
            ) from error
        object.__setattr__(self, "status", status)
        try:
            relation = EvidenceRelation(self.relation)
        except ValueError as error:
            raise EvidenceMergerError(
                f"relation must be one of "
                f"({', '.join(member.value for member in EvidenceRelation)}); "
                f"got {self.relation!r}"
            ) from error
        object.__setattr__(self, "relation", relation)
        object.__setattr__(
            self, "relation_target", _optional_text(self.relation_target, "relation_target")
        )

        if status is ObservationStatus.FINDING:
            if not isinstance(self.finding, Finding):
                raise EvidenceMergerError(
                    "a FINDING observation must carry a Finding; got "
                    f"{self.finding!r}"
                )
            if self.source != self.finding.source:
                raise EvidenceMergerError(
                    "observation source must match the finding source; got "
                    f"{self.source!r} and {self.finding.source!r}"
                )
            if _optional_text(self.reason, "reason") is not None:
                raise EvidenceMergerError(
                    "a FINDING observation must not carry a reason"
                )
            if relation is not EvidenceRelation.UNRESOLVED and (
                self.relation_target is None
            ):
                raise EvidenceMergerError(
                    f"relation {relation.value} requires an explicit "
                    "relation_target to validate it against"
                )
            return

        # ABSTAIN / ERROR: no finding, a safe reason, and no relation at all
        if self.finding is not None:
            raise EvidenceMergerError(
                f"a {status.value} observation must not carry a Finding"
            )
        object.__setattr__(self, "reason", _text(self.reason, "reason"))
        if relation is not EvidenceRelation.UNRESOLVED:
            raise EvidenceMergerError(
                f"a {status.value} observation cannot relate to evidence"
            )
        if self.relation_target is not None:
            raise EvidenceMergerError(
                f"a {status.value} observation must not carry a relation_target"
            )


def _text_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise EvidenceMergerError(
            f"{field_name} must be a sequence of strings"
        )
    return tuple(_text(item, field_name) for item in value)


@dataclass(frozen=True)
class ObservationOutcome:
    """An advisor execution that produced no finding: abstention or failure.

    ``reason`` is a *sanitized* text supplied by the caller - it never carries a
    raw provider exception message, so nothing sensitive can leak through the
    merger into a report.
    """

    source: str
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _text(self.source, "source"))
        object.__setattr__(self, "reason", _text(self.reason, "reason"))

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {"source": self.source, "reason": self.reason}


@dataclass(frozen=True)
class EvidenceConflict:
    """Two *validated* and incompatible relations to one deterministic anchor.

    A conflict exists only when the caller explicitly declared opposing
    relations (at least one ``SUPPORTING`` and at least one ``CONTRADICTING``)
    **and** both were validated against the same gate anchor. It is never
    inferred from linguistics, provider identity, severity, confidence or
    locator overlap - so a merger in which nobody declared anything produces no
    conflicts at all.
    """

    target: str
    supporting_ids: tuple[str, ...] = ()
    contradicting_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "target", _text(self.target, "target"))
        supporting = _text_tuple(self.supporting_ids, "supporting_ids")
        contradicting = _text_tuple(self.contradicting_ids, "contradicting_ids")
        object.__setattr__(self, "supporting_ids", supporting)
        object.__setattr__(self, "contradicting_ids", contradicting)
        if not supporting or not contradicting:
            raise EvidenceMergerError(
                "a conflict needs at least one supporting and one contradicting "
                "finding id"
            )
        if set(supporting) & set(contradicting):
            raise EvidenceMergerError(
                "one finding cannot both support and contradict the same target"
            )

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "target": self.target,
            "supporting_ids": list(self.supporting_ids),
            "contradicting_ids": list(self.contradicting_ids),
        }


@dataclass(frozen=True)
class EvidenceView:
    """The merged, lossless picture of what the advisors said.

    Every finding stays in :attr:`findings` - including the ones nothing could be
    said about - so an unresolved advisor concern is *visible*, never hidden.
    The id tuples partition the findings by their effective relation, and the
    abstentions/failures are kept apart from the findings because they are not
    evidence at all.
    """

    findings: tuple[Finding, ...] = ()
    supporting_ids: tuple[str, ...] = ()
    conflicting_ids: tuple[str, ...] = ()
    unresolved_ids: tuple[str, ...] = ()
    abstained: tuple[ObservationOutcome, ...] = ()
    errors: tuple[ObservationOutcome, ...] = ()
    conflicts: tuple[EvidenceConflict, ...] = ()
    unresolved_questions: tuple[str, ...] = ()
    gate_status: Optional[DecisionStatus] = None
    gate_anchors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        findings = tuple(self.findings)
        for finding in findings:
            if not isinstance(finding, Finding):
                raise EvidenceMergerError(
                    f"findings must contain Finding objects; got {finding!r}"
                )
        ids = [finding.id for finding in findings]
        if len(set(ids)) != len(ids):
            raise EvidenceMergerError("findings must be unique by id")
        object.__setattr__(self, "findings", findings)

        supporting = _text_tuple(self.supporting_ids, "supporting_ids")
        conflicting = _text_tuple(self.conflicting_ids, "conflicting_ids")
        unresolved = _text_tuple(self.unresolved_ids, "unresolved_ids")
        object.__setattr__(self, "supporting_ids", supporting)
        object.__setattr__(self, "conflicting_ids", conflicting)
        object.__setattr__(self, "unresolved_ids", unresolved)

        known = set(ids)
        for name, group in (
            ("supporting_ids", supporting),
            ("conflicting_ids", conflicting),
            ("unresolved_ids", unresolved),
        ):
            unknown = [entry for entry in group if entry not in known]
            if unknown:
                raise EvidenceMergerError(
                    f"{name} references findings that are not in the view: "
                    f"{unknown}"
                )
        overlaps = (
            (set(supporting) & set(conflicting))
            | (set(supporting) & set(unresolved))
            | (set(conflicting) & set(unresolved))
        )
        if overlaps:
            raise EvidenceMergerError(
                "a finding has exactly one relation state; overlapping ids: "
                f"{sorted(overlaps)}"
            )
        placed = set(supporting) | set(conflicting) | set(unresolved)
        if placed != known:
            raise EvidenceMergerError(
                "every finding must be classified as supporting, conflicting or "
                f"unresolved; unclassified: {sorted(known - placed)}"
            )

        outcomes = tuple(self.abstained)
        for outcome in outcomes:
            if not isinstance(outcome, ObservationOutcome):
                raise EvidenceMergerError(
                    f"abstained must contain ObservationOutcome; got {outcome!r}"
                )
        object.__setattr__(self, "abstained", outcomes)
        failures = tuple(self.errors)
        for failure in failures:
            if not isinstance(failure, ObservationOutcome):
                raise EvidenceMergerError(
                    f"errors must contain ObservationOutcome; got {failure!r}"
                )
        object.__setattr__(self, "errors", failures)

        conflicts = tuple(self.conflicts)
        for conflict in conflicts:
            if not isinstance(conflict, EvidenceConflict):
                raise EvidenceMergerError(
                    f"conflicts must contain EvidenceConflict; got {conflict!r}"
                )
        object.__setattr__(self, "conflicts", conflicts)
        object.__setattr__(
            self,
            "unresolved_questions",
            _text_tuple(self.unresolved_questions, "unresolved_questions"),
        )
        if self.gate_status is not None and not isinstance(
            self.gate_status, DecisionStatus
        ):
            raise EvidenceMergerError(
                f"gate_status must be a DecisionStatus or None; got "
                f"{self.gate_status!r}"
            )
        object.__setattr__(
            self, "gate_anchors", _text_tuple(self.gate_anchors, "gate_anchors")
        )

    @property
    def finding_ids(self) -> tuple[str, ...]:
        """Every finding id, in the order the findings arrived."""
        return tuple(finding.id for finding in self.findings)

    @property
    def sources(self) -> tuple[str, ...]:
        """Every provider that delivered a finding, sorted and deduplicated."""
        return tuple(sorted({finding.source for finding in self.findings}))

    @property
    def has_conflicts(self) -> bool:
        """Whether any validated, unresolved conflict was found."""
        return bool(self.conflicts)

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "findings": [finding.to_dict() for finding in self.findings],
            "supporting_ids": list(self.supporting_ids),
            "conflicting_ids": list(self.conflicting_ids),
            "unresolved_ids": list(self.unresolved_ids),
            "abstained": [outcome.to_dict() for outcome in self.abstained],
            "errors": [outcome.to_dict() for outcome in self.errors],
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
            "unresolved_questions": list(self.unresolved_questions),
            "gate_status": (
                None if self.gate_status is None else self.gate_status.value
            ),
            "gate_anchors": list(self.gate_anchors),
        }


def _validated_gate(gate: Optional[Decision]) -> Optional[Decision]:
    """A settled realization-gate verdict, or ``None`` when there is none.

    Only a conclusive deterministic verdict may act as an anchor. A ``PENDING``,
    ``ABSTAIN`` or ``ERROR`` decision carries no authoritative ACCEPTED /
    REJECTED statement, so it is refused as an anchor instead of being
    reinterpreted into one.
    """
    if gate is None:
        return None
    if not isinstance(gate, Decision):
        raise EvidenceMergerError(
            f"gate must be a Decision or None; got {gate!r}"
        )
    if gate.status not in (DecisionStatus.ACCEPTED, DecisionStatus.REJECTED):
        raise EvidenceMergerError(
            "a deterministic gate verdict must be ACCEPTED or REJECTED; got "
            f"{gate.status.value!r}"
        )
    return gate


def _identity(finding: Finding) -> tuple[Any, ...]:
    """The fields a finding id is derived from - and nothing else.

    ``confidence``, ``severity`` and ``created_at`` are deliberately excluded:
    the finding id itself excludes them, because a re-ask that restates the same
    claim with a different certainty is the same logical finding.
    """
    return (finding.source, finding.claim, finding.evidence, finding.step_no)


def _question(conflict: EvidenceConflict) -> str:
    """A deterministic question for one validated conflict - no interpretation."""
    return (
        f"Unresolved evidence conflict on {conflict.target}: supporting "
        f"{', '.join(conflict.supporting_ids)} vs contradicting "
        f"{', '.join(conflict.contradicting_ids)}"
    )


def merge_evidence(
    observations: Sequence[AdvisorObservation],
    *,
    gate: Optional[Decision] = None,
) -> EvidenceView:
    """Merge observations into one conservative, lossless evidence view.

    The gate verdict is *never* recomputed here and is never overruled: it is
    used for exactly one thing - validating a caller-declared relation. A
    relation that cannot be matched against ``gate.rules_applied`` or
    ``gate.evidence_refs`` degrades to :attr:`EvidenceRelation.UNRESOLVED`, which
    is a downgrade, never an error: an unprovable relation is not a defect, it is
    simply unknown.
    """
    if isinstance(observations, (str, bytes)) or not isinstance(
        observations, Sequence
    ):
        raise EvidenceMergerError(
            "observations must be a sequence of AdvisorObservation"
        )
    verdict = _validated_gate(gate)
    anchors = (
        set(verdict.rules_applied) | set(verdict.evidence_refs)
        if verdict is not None
        else set()
    )

    findings: list[Finding] = []
    identity: dict[str, tuple[Any, ...]] = {}
    effective: dict[str, EvidenceRelation] = {}
    targets: dict[str, str] = {}
    abstained: list[ObservationOutcome] = []
    errors: list[ObservationOutcome] = []

    for observation in observations:
        if not isinstance(observation, AdvisorObservation):
            raise EvidenceMergerError(
                f"observations must contain AdvisorObservation; got {observation!r}"
            )
        if observation.status is ObservationStatus.ABSTAIN:
            abstained.append(
                ObservationOutcome(
                    source=observation.source, reason=observation.reason
                )
            )
            continue
        if observation.status is ObservationStatus.ERROR:
            errors.append(
                ObservationOutcome(
                    source=observation.source, reason=observation.reason
                )
            )
            continue

        finding = observation.finding
        signature = _identity(finding)
        previous = identity.get(finding.id)
        if previous is not None:
            if previous != signature:
                raise EvidenceIdentityCollisionError(
                    f"finding id {finding.id!r} arrived twice with different "
                    "identity content"
                )
            continue  # a repeated delivery of the very same finding
        identity[finding.id] = signature
        findings.append(finding)

        # a relation counts only when it was declared AND validates against the
        # deterministic anchors; otherwise it is honestly unknown
        target = observation.relation_target
        if (
            observation.relation is EvidenceRelation.UNRESOLVED
            or target is None
            or target not in anchors
        ):
            effective[finding.id] = EvidenceRelation.UNRESOLVED
        else:
            effective[finding.id] = observation.relation
            targets[finding.id] = target

    # a conflict needs *validated* opposing relations to the same anchor - two
    # unresolved findings are never turned into one
    grouped: dict[str, dict[str, list[str]]] = {}
    for finding_id, anchor in targets.items():
        bucket = grouped.setdefault(
            anchor, {"SUPPORTING": [], "CONTRADICTING": []}
        )
        bucket[effective[finding_id].value].append(finding_id)

    conflicts = tuple(
        EvidenceConflict(
            target=anchor,
            supporting_ids=tuple(grouped[anchor]["SUPPORTING"]),
            contradicting_ids=tuple(grouped[anchor]["CONTRADICTING"]),
        )
        for anchor in sorted(grouped)
        if grouped[anchor]["SUPPORTING"] and grouped[anchor]["CONTRADICTING"]
    )

    return EvidenceView(
        findings=tuple(findings),
        supporting_ids=tuple(
            finding_id
            for finding_id in identity
            if effective[finding_id] is EvidenceRelation.SUPPORTING
        ),
        conflicting_ids=tuple(
            finding_id
            for finding_id in identity
            if effective[finding_id] is EvidenceRelation.CONTRADICTING
        ),
        unresolved_ids=tuple(
            finding_id
            for finding_id in identity
            if effective[finding_id] is EvidenceRelation.UNRESOLVED
        ),
        abstained=tuple(abstained),
        errors=tuple(errors),
        conflicts=conflicts,
        unresolved_questions=tuple(_question(conflict) for conflict in conflicts),
        gate_status=None if verdict is None else verdict.status,
        gate_anchors=tuple(sorted(anchors)),
    )
