"""Architecture review - one explicit, advisory three-advisor consultation.

The review runs the three existing, consultative advisors (OpenAI implementation
analyst, Claude critical reviewer, Grok challenger) over the existing structured
project state, merges their observations with the existing evidence merger, turns
that into the existing decision-engine result, and consults the existing judge
**only** for a structurally valid, explicitly declared evidence conflict.

It is deliberately **advisory and read-only**:

* it never changes workflow state, never reaches ``VERIFIED``, never writes an
  ADR or an ACR, never touches an ``ArchitectureVersion`` and never bypasses the
  realization gate - the deterministic gate stays the only verdict;
* it persists nothing. The advisor findings and the advisory decision live in the
  returned payload only; the only database side effect is the cost telemetry each
  adapter already records for itself;
* it neither infers nor invents: relations between an advisor finding and the
  deterministic gate are *explicit metadata* supplied where the reviewers are
  configured, so a review in which nobody declared anything has no conflicts and
  therefore does not call the judge at all;
* it never rewrites the operator's question. The question is passed verbatim to
  every ``AdvisorQuery``; there is one question, one review, no conversation.

Provider failure semantics are **not re-implemented here**: the provider error
families are classified by the existing ``composition.evidence.observe`` seam,
which is injected. That keeps ``ABSTAIN`` and ``ERROR`` exactly as the core
already defines them, and keeps this module free of any provider knowledge.

Imports are restricted to ``domain``, ``ports`` and sibling ``application``
modules - never ``infrastructure``, never a concrete adapter, never ``sqlite3``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, Union

from ..domain.enums import ADRStatus, DecisionStatus, RiskStatus
from ..domain.models import ADR, Decision, Finding, Risk, Step, utc_now
from ..ports.capabilities import (
    AdvisorPort,
    AdvisorQuery,
    CostQuery,
    CostSummary,
    RealizationCheckPort,
    RealizationCheckResult,
)
from .decision_engine import synthesize_decision
from .eventlog import (
    EVENT_LEVEL_ERROR,
    EVENT_LEVEL_INFO,
    EVENT_LEVEL_WARN,
    build_event,
    component_for,
    exception_reason,
)
from .evidence_merger import (
    AdvisorObservation,
    EvidenceRelation,
    EvidenceView,
    ObservationStatus,
    merge_evidence,
)
from .judge import JudgeUseCase
from .reporting import ReportSnapshot

__all__ = [
    "DEFAULT_REVIEW_QUESTION",
    "JUDGE_STATUS_OK",
    "JUDGE_STATUS_ERROR",
    "JUDGE_STATUS_NOT_CONSULTED",
    "JUDGE_STATUS_UNAVAILABLE",
    "ReviewError",
    "AdvisorReviewer",
    "ReviewStage",
    "JudgeOutcome",
    "ArchitectureReviewResult",
    "ArchitectureReview",
]

#: The question the panel offers by default. It is a *default*, not a rule: the
#: operator may replace it and the replacement is sent verbatim to every advisor.
DEFAULT_REVIEW_QUESTION = (
    "Review the current architecture state of this project against its "
    "authoritative baseline. Identify module boundaries, dependency risks, "
    "failure points and the simplest maintainable design, and substantiate "
    "every claim with a concrete, checkable locator."
)

#: How the judge layer resolved (or could not resolve) the merged conflicts.
JUDGE_STATUS_OK = "OK"
JUDGE_STATUS_ERROR = "ERROR"
JUDGE_STATUS_NOT_CONSULTED = "NOT_CONSULTED"
JUDGE_STATUS_UNAVAILABLE = "UNAVAILABLE"


class ReviewError(ValueError):
    """The review was handed input it cannot honestly process."""


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewError(f"{field_name} must be a non-empty string; got {value!r}")
    return value


def _plain(value: Any, field_name: str) -> Any:
    """Accept only plain data, so a payload is JSON-safe by construction."""
    if isinstance(value, Mapping):
        return {str(key): _plain(item, field_name) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item, field_name) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ReviewError(
        f"{field_name} must be plain JSON-safe data; got {value!r}"
    )


def _mapping(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ReviewError(f"{field_name} must be a mapping; got {value!r}")
    return _plain(dict(value), field_name)


# ---------------------------------------------------------------------------
# the review request and its result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdvisorReviewer:
    """One configured advisor, in its declared position.

    ``source`` is the provider id the adapter reports as ``Finding.source`` (the
    merger requires the observation source to match it). ``relation`` and
    ``relation_target`` are the **explicit metadata** the architecture has always
    required for a relation: nothing is ever inferred from text, severity,
    confidence, evidence overlap or provider identity. Left at their defaults a
    reviewer declares nothing, so its finding stays ``UNRESOLVED``.
    """

    source: str
    advisor: AdvisorPort
    relation: EvidenceRelation = EvidenceRelation.UNRESOLVED
    relation_target: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _text(self.source, "source"))
        if not callable(getattr(self.advisor, "advise", None)):
            raise ReviewError(
                "advisor must implement AdvisorPort.advise(query); got "
                f"{self.advisor!r}"
            )
        if not isinstance(self.relation, EvidenceRelation):
            raise ReviewError(
                f"relation must be an EvidenceRelation; got {self.relation!r}"
            )
        if self.relation_target is not None:
            object.__setattr__(
                self,
                "relation_target",
                _text(self.relation_target, "relation_target"),
            )
        if self.relation is not EvidenceRelation.UNRESOLVED and (
            self.relation_target is None
        ):
            raise ReviewError(
                f"relation {self.relation.value} requires an explicit "
                "relation_target to be validated against"
            )

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation (never the adapter itself)."""
        return {
            "source": self.source,
            "relation": self.relation.value,
            "relation_target": self.relation_target,
        }


@dataclass(frozen=True)
class ReviewStage:
    """What one advisor execution produced, plus its cost.

    ``relation`` is the **effective** relation after the merger validated the
    declared one against the deterministic gate anchors: a relation that could
    not be validated is reported honestly as ``UNRESOLVED``.
    """

    source: str
    status: str
    reason: str = ""
    finding: Optional[Mapping[str, Any]] = None
    relation: str = EvidenceRelation.UNRESOLVED.value
    relation_target: Optional[str] = None
    cost: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _text(self.source, "source"))
        object.__setattr__(self, "status", _text(self.status, "status"))
        object.__setattr__(self, "reason", self.reason or "")
        if self.finding is not None:
            object.__setattr__(self, "finding", _mapping(self.finding, "finding"))
        object.__setattr__(self, "relation", _text(self.relation, "relation"))
        if self.cost:
            object.__setattr__(self, "cost", _mapping(self.cost, "cost"))
        if self.status == ObservationStatus.FINDING.value and self.finding is None:
            raise ReviewError("a FINDING stage must carry its finding")
        if self.status != ObservationStatus.FINDING.value and self.finding:
            raise ReviewError(f"a {self.status} stage must not carry a finding")

    @property
    def finding_id(self) -> Optional[str]:
        """The finding id of a FINDING stage, or ``None``."""
        if not self.finding:
            return None
        return str(self.finding.get("id") or "") or None

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "source": self.source,
            "status": self.status,
            "reason": self.reason,
            "finding": None if self.finding is None else dict(self.finding),
            "relation": self.relation,
            "relation_target": self.relation_target,
            "cost": dict(self.cost),
        }


@dataclass(frozen=True)
class JudgeOutcome:
    """The judge layer of one review - kept separate from everything else.

    A judge failure is *sanitized here and never allowed to discard the rest of
    the review*: ``status`` becomes ``ERROR`` with the exception **type name**
    only (never a raw provider message), ``judgments`` stays empty, and the
    advisor findings, the merged evidence, the deterministic gate result and the
    advisory decision all remain intact.
    """

    available: bool
    consulted: bool
    status: str
    reason: str = ""
    judgments: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.available, bool):
            raise ReviewError(f"available must be a bool; got {self.available!r}")
        if not isinstance(self.consulted, bool):
            raise ReviewError(f"consulted must be a bool; got {self.consulted!r}")
        object.__setattr__(self, "status", _text(self.status, "status"))
        object.__setattr__(self, "reason", self.reason or "")
        object.__setattr__(
            self,
            "judgments",
            tuple(_mapping(judgment, "judgment") for judgment in self.judgments),
        )
        if self.status == JUDGE_STATUS_OK and not self.consulted:
            raise ReviewError("an OK judge outcome must have been consulted")
        if self.status == JUDGE_STATUS_ERROR and self.judgments:
            raise ReviewError("a failed judge outcome must not carry judgments")
        if self.status == JUDGE_STATUS_ERROR and not self.reason:
            raise ReviewError("a failed judge outcome must carry its reason")

    @property
    def count(self) -> int:
        """How many conflicts the judge actually answered."""
        return len(self.judgments)

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "available": self.available,
            "consulted": self.consulted,
            "status": self.status,
            "reason": self.reason,
            "count": self.count,
            "judgments": [dict(judgment) for judgment in self.judgments],
        }


@dataclass(frozen=True)
class ArchitectureReviewResult:
    """One complete, JSON-safe review - advisory, and written nowhere.

    Everything the deterministic layer said is kept *next to* everything the
    advisors said, never merged into it: ``deterministic_gate`` is the fresh
    read-only gate result, ``decision`` is the decision engine's explanatory
    outcome, and ``judge`` is a third, separate layer.
    """

    review_id: str
    reviewed_at: datetime
    question: str
    project: str
    source_root: str
    step_no: Optional[int]
    architecture_version: Optional[str]
    deterministic_gate: Mapping[str, Any]
    providers: tuple[ReviewStage, ...]
    evidence: Mapping[str, Any]
    conflicts: tuple[Mapping[str, Any], ...]
    judge: JudgeOutcome
    decision: Mapping[str, Any]
    cost: Mapping[str, Any]
    check_source_root: Optional[str] = None
    source_root_verified: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "review_id", _text(self.review_id, "review_id"))
        if not isinstance(self.reviewed_at, datetime):
            raise ReviewError(
                f"reviewed_at must be a datetime; got {self.reviewed_at!r}"
            )
        object.__setattr__(self, "question", _text(self.question, "question"))
        object.__setattr__(self, "project", _text(self.project, "project"))
        object.__setattr__(
            self, "source_root", _text(self.source_root, "source_root")
        )
        if self.step_no is not None and (
            isinstance(self.step_no, bool)
            or not isinstance(self.step_no, int)
            or self.step_no < 1
        ):
            raise ReviewError(f"step_no must be an int >= 1; got {self.step_no!r}")
        providers = tuple(self.providers)
        for stage in providers:
            if not isinstance(stage, ReviewStage):
                raise ReviewError(
                    f"providers must contain ReviewStage; got {stage!r}"
                )
        object.__setattr__(self, "providers", providers)
        object.__setattr__(
            self,
            "deterministic_gate",
            _mapping(self.deterministic_gate, "deterministic_gate"),
        )
        object.__setattr__(self, "evidence", _mapping(self.evidence, "evidence"))
        object.__setattr__(
            self,
            "conflicts",
            tuple(_mapping(conflict, "conflict") for conflict in self.conflicts),
        )
        if not isinstance(self.judge, JudgeOutcome):
            raise ReviewError(f"judge must be a JudgeOutcome; got {self.judge!r}")
        object.__setattr__(self, "decision", _mapping(self.decision, "decision"))
        object.__setattr__(self, "cost", _mapping(self.cost, "cost"))
        if not isinstance(self.source_root_verified, bool):
            raise ReviewError(
                "source_root_verified must be a bool; got "
                f"{self.source_root_verified!r}"
            )

    @property
    def findings(self) -> tuple[ReviewStage, ...]:
        """The stages that produced a finding, in declared order."""
        return tuple(
            stage
            for stage in self.providers
            if stage.status == ObservationStatus.FINDING.value
        )

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation (what the GUI receives)."""
        return {
            "review_id": self.review_id,
            "reviewed_at": self.reviewed_at.isoformat(),
            "question": self.question,
            "project": self.project,
            "source_root": self.source_root,
            "check_source_root": self.check_source_root,
            "source_root_verified": self.source_root_verified,
            "step_no": self.step_no,
            "architecture_version": self.architecture_version,
            "deterministic_gate": dict(self.deterministic_gate),
            "providers": [stage.to_dict() for stage in self.providers],
            "evidence": dict(self.evidence),
            "conflicts": [dict(conflict) for conflict in self.conflicts],
            "judge": self.judge.to_dict(),
            "decision": dict(self.decision),
            "cost": dict(self.cost),
            "persistence": {
                "written": False,
                "reason": (
                    "the review is advisory and read-only: nothing is persisted"
                ),
            },
        }


# ---------------------------------------------------------------------------
# pure building blocks
# ---------------------------------------------------------------------------


def _open_risks(snapshot: ReportSnapshot) -> tuple[Risk, ...]:
    """The open risks of the projection, in the projection's own order."""
    return tuple(
        risk for risk in snapshot.risks if risk.status is RiskStatus.OPEN
    )


def _accepted_adrs(snapshot: ReportSnapshot) -> tuple[ADR, ...]:
    """The accepted ADRs of the projection, in the projection's own order."""
    return tuple(
        adr for adr in snapshot.adrs if adr.status is ADRStatus.ACCEPTED
    )


def _outcome_level(status: ObservationStatus) -> str:
    """The log level of one advisor outcome: a finding informs, the rest warns."""
    if status is ObservationStatus.FINDING:
        return EVENT_LEVEL_INFO
    if status is ObservationStatus.ABSTAIN:
        return EVENT_LEVEL_WARN
    return EVENT_LEVEL_ERROR


def _outcome_action(status: ObservationStatus) -> str:
    """The log action of one advisor outcome."""
    if status is ObservationStatus.FINDING:
        return "result"
    if status is ObservationStatus.ABSTAIN:
        return "abstain"
    return "error"


def _outcome_message(source: str, observation: AdvisorObservation) -> str:
    """One sanitized line per advisor outcome - never the provider's own text.

    A finding is described by its own shape (severity, confidence, evidence
    count); an abstention or a failure quotes only the seam's already sanitized
    reason, which is a provider-neutral phrase plus an exception *type name*.
    """
    item = observation.finding
    if observation.status is ObservationStatus.FINDING and item is not None:
        return (
            f"advisor {source} returned a finding (severity "
            f"{item.severity.value}, confidence {item.confidence:.2f}, "
            f"{len(item.evidence)} evidence item(s))"
        )
    if observation.status is ObservationStatus.ABSTAIN:
        return f"advisor {source} abstained ({observation.reason})"
    return f"advisor {source} failed ({observation.reason})"


def _emit(
    on_event: Optional[Callable[[Mapping[str, Any]], None]],
    *,
    level: str,
    component: str,
    action: str,
    message: str,
    stamp: Callable[[], datetime],
    step_no: Optional[int] = None,
    review_id: Optional[str] = None,
) -> None:
    """Send one log event through the injected hook.

    Diagnostics must never be able to break the review, so both building and
    delivering the event are contained here. The event itself is built by the
    shared contract (:func:`~.eventlog.build_event`), which sanitizes the message
    and refuses anything the panel could not render.
    """
    if on_event is None:
        return
    try:
        event = build_event(
            level=level,
            component=component,
            action=action,
            message=message,
            step_no=step_no,
            review_id=review_id,
            timestamp=stamp(),
        )
    except Exception:  # a malformed diagnostic is dropped, never fatal
        return
    try:
        on_event(event)
    except Exception:  # a UI hook is secondary by definition
        return


def _current_step(snapshot: ReportSnapshot) -> Optional[Step]:
    """The step the loop itself reports as current, or ``None``."""
    step_no = snapshot.health.current_step_no
    if step_no is None:
        return None
    for step in snapshot.steps:
        if step.step_no == step_no:
            return step
    return None


def _review_context(
    snapshot: ReportSnapshot,
    *,
    question: str,
    source_root: str,
    gate: Mapping[str, Any],
) -> dict[str, Any]:
    """The compact, deterministic, JSON-safe context every advisor receives.

    It is a documented *subset* of the canonical projection plus the fresh gate
    result - never a chat history, never the whole snapshot, never a second read
    model. ``source_root`` travels with it so an advisor can see which tree the
    deterministic check actually inspected.
    """
    step = _current_step(snapshot)
    architecture = snapshot.architecture
    health = snapshot.health
    return {
        "review_question": question,
        "project": {
            "name": snapshot.project.name,
            "mode": snapshot.project.mode.value,
            "plan_version": snapshot.project.plan_version,
            "paused": snapshot.project.paused,
        },
        "source_root": source_root,
        "architecture": (
            None
            if architecture is None
            else {
                "version": architecture.version,
                "baseline": architecture.baseline,
                "rules": list(architecture.rules),
            }
        ),
        "step": (
            None
            if step is None
            else {
                "step_no": step.step_no,
                "phase": step.phase.value,
                "title": step.title,
                "state": step.state.value,
                "attempt": step.attempt,
                "risk": step.risk.value,
                "requires_human": step.requires_human,
                "description": step.description,
            }
        ),
        "health": {
            "step_count": health.step_count,
            "current_step_no": health.current_step_no,
            "current_state": (
                None if health.current_state is None else health.current_state.value
            ),
            "complete": health.complete,
            "project_paused": health.project_paused,
        },
        "open_risks": [
            {
                "id": risk.id,
                "severity": risk.severity.value,
                "probability": risk.probability,
                "impact": risk.impact.value,
                "description": risk.description,
                "mitigation": risk.mitigation,
            }
            for risk in _open_risks(snapshot)
        ],
        "adrs": [
            {
                "id": adr.id,
                "title": adr.title,
                "status": adr.status.value,
                "decision": adr.decision,
            }
            for adr in _accepted_adrs(snapshot)
        ],
        "change_requests": [
            {
                "request_id": request.request_id,
                "title": request.title,
                "status": request.status.value,
                "source_version": request.source_version,
                "target_version": request.target_version,
            }
            for request in snapshot.change_requests
        ],
        "deterministic_gate": dict(gate),
    }


def _review_id(
    question: str,
    step_no: Optional[int],
    gate: Optional[Decision],
    view: EvidenceView,
) -> str:
    """A content-addressed review id - the clock is never hashed."""
    parts = [
        question,
        "" if step_no is None else str(step_no),
        "" if gate is None else gate.id,
        *view.finding_ids,
        *[outcome.source for outcome in view.abstained],
        *[outcome.source for outcome in view.errors],
    ]
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]
    return (
        f"architecture-review-{0 if step_no is None else step_no}-{digest}"
    )


def _gate_payload(result: RealizationCheckResult) -> dict[str, Any]:
    """The fresh deterministic verdict, as plain data."""
    decision = result.decision
    return {
        "available": True,
        "reason": "",
        "compliant": bool(result.compliant),
        "baseline_version": result.baseline_version,
        "rules": list(decision.rules_applied),
        "violation_count": len(result.findings),
        "finding_ids": [finding.id for finding in result.findings],
        "decision_id": decision.id,
        "decision_status": decision.status.value,
    }


def _gate_unavailable(reason: str) -> dict[str, Any]:
    """An honest 'the deterministic verdict could not be rendered' payload."""
    return {
        "available": False,
        "reason": reason,
        "compliant": None,
        "baseline_version": None,
        "rules": [],
        "violation_count": 0,
        "finding_ids": [],
        "decision_id": None,
        "decision_status": None,
    }


def _cost_payload(
    summary: Optional[CostSummary], *, reason: str = ""
) -> dict[str, Any]:
    """One cost summary as plain data - never a fake zero without a reason."""
    if summary is None:
        return {
            "available": False,
            "reason": reason or "no cost capability was injected",
        }
    return {"available": True, "reason": "", **summary.to_dict()}


def _cost_total(summaries: Sequence[CostSummary]) -> CostSummary:
    """Add summaries - the priced/unpriced partition stays exact."""
    return CostSummary(
        total_usd=sum(summary.total_usd for summary in summaries),
        input_tokens=sum(summary.input_tokens for summary in summaries),
        output_tokens=sum(summary.output_tokens for summary in summaries),
        record_count=sum(summary.record_count for summary in summaries),
        priced_record_count=sum(
            summary.priced_record_count for summary in summaries
        ),
        unpriced_record_count=sum(
            summary.unpriced_record_count for summary in summaries
        ),
    )


def _stage(
    reviewer: AdvisorReviewer,
    observation: AdvisorObservation,
    view: EvidenceView,
    cost: Mapping[str, Any],
) -> ReviewStage:
    """One stage, with the relation the *merger* actually validated."""
    relation = EvidenceRelation.UNRESOLVED
    finding = observation.finding
    if observation.status is ObservationStatus.FINDING and finding is not None:
        if finding.id in view.supporting_ids:
            relation = EvidenceRelation.SUPPORTING
        elif finding.id in view.conflicting_ids:
            relation = EvidenceRelation.CONTRADICTING
    return ReviewStage(
        source=reviewer.source,
        status=observation.status.value,
        reason=observation.reason or "",
        finding=None if finding is None else finding.to_dict(),
        relation=relation.value,
        relation_target=reviewer.relation_target,
        cost=cost,
    )


def _same_path(left: Optional[str], right: str) -> bool:
    """Whether two reported roots name the same path (normalized, never guessed)."""
    if left is None:
        return False
    try:
        return Path(left) == Path(right)
    except (TypeError, ValueError, OSError):
        return False


# ---------------------------------------------------------------------------
# the use-case
# ---------------------------------------------------------------------------


class ArchitectureReview:
    """One explicit, advisory review of the current architecture state.

    Holds exactly the injected seams: the ordered reviewers, the provider-neutral
    observation observer, the optional judge use-case, the optional read-only
    realization check, the optional cost query and an injected clock. It receives
    no storage, no transaction, no repository, no monitor and no orchestrator.

    Every provider call is sequential and explicit: the three adapters share one
    cost sink over one SQLite connection, so parallel calls would write cost rows
    from several threads on a single connection. Ordering is therefore both a
    simplicity and a correctness decision, and the payload keeps the declared
    order.
    """

    def __init__(
        self,
        reviewers: Sequence[AdvisorReviewer],
        *,
        observer: Callable[[str, Callable[[], Finding]], AdvisorObservation],
        source_root: Union[str, Path],
        judge: Optional[JudgeUseCase] = None,
        check: Optional[RealizationCheckPort] = None,
        cost_query: Optional[Callable[[CostQuery], CostSummary]] = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not callable(observer):
            raise ReviewError(
                "observer must be a callable returning an AdvisorObservation"
            )
        configured = tuple(reviewers)
        if not configured:
            raise ReviewError("at least one reviewer must be configured")
        for reviewer in configured:
            if not isinstance(reviewer, AdvisorReviewer):
                raise ReviewError(
                    f"reviewers must contain AdvisorReviewer; got {reviewer!r}"
                )
        if judge is not None and not isinstance(judge, JudgeUseCase):
            raise ReviewError(
                f"judge must be a JudgeUseCase or None; got {judge!r}"
            )
        if check is not None and not isinstance(check, RealizationCheckPort):
            raise ReviewError(
                "check must implement RealizationCheckPort "
                "(check(step_no, attempt))"
            )
        if cost_query is not None and not callable(cost_query):
            raise ReviewError("cost_query must be a callable or None")
        if not callable(clock):
            raise ReviewError("clock must be a callable returning a datetime")
        if not isinstance(source_root, (str, Path)) or not str(source_root).strip():
            raise ReviewError(f"source_root must be a path; got {source_root!r}")
        self._reviewers = configured
        self._observer = observer
        self._source_root = str(source_root)
        self._judge = judge
        self._check = check
        self._cost_query = cost_query
        self._clock = clock

    # -- read-only introspection -------------------------------------------
    @property
    def reviewers(self) -> tuple[AdvisorReviewer, ...]:
        """The configured reviewers, in their declared order."""
        return self._reviewers

    @property
    def source_root(self) -> str:
        """The source tree this review is configured to report and check."""
        return self._source_root

    @property
    def has_judge(self) -> bool:
        """Whether a judge use-case was injected at all."""
        return self._judge is not None

    @property
    def has_check(self) -> bool:
        """Whether a read-only realization check was injected at all."""
        return self._check is not None

    # -- the review --------------------------------------------------------
    def review(
        self,
        snapshot: ReportSnapshot,
        *,
        question: str = DEFAULT_REVIEW_QUESTION,
        on_event: Optional[Callable[[Mapping[str, Any]], None]] = None,
    ) -> ArchitectureReviewResult:
        """Run one complete review - reads, provider calls and pure semantics only.

        The question is passed **verbatim** to every advisor (one question, one
        consultation, no conversation). The deterministic gate is checked freshly
        and read-only. Every provider is observed, merged, explained and - only
        for a structurally valid, explicitly declared conflict - judged. Nothing
        is written anywhere; the result is the only product.

        ``on_event`` receives one sanitized log event per stage (context, gate,
        each advisor's start and outcome, the merge, the decision, the judge and
        the cost), so an operator can see *what happened* without reading the
        result. The events are diagnostics: a failing hook never fails a review.
        """
        if not isinstance(snapshot, ReportSnapshot):
            raise ReviewError(f"snapshot must be a ReportSnapshot; got {snapshot!r}")
        if not isinstance(question, str) or not question.strip():
            raise ReviewError(
                "the review question is required and must not be blank"
            )
        started_at = self._clock()
        step = _current_step(snapshot)
        step_no = None if step is None else step.step_no
        attempt = 1 if step is None else max(step.attempt, 1)

        _emit(
            on_event,
            level=EVENT_LEVEL_INFO,
            component="CoreWorker",
            action="context",
            message=(
                "review context built: step "
                f"{'-' if step_no is None else step_no}, "
                f"{len(snapshot.steps)} step(s), "
                f"{len(_open_risks(snapshot))} open risk(s), "
                f"{len(_accepted_adrs(snapshot))} accepted ADR(s), source root "
                f"{self._source_root}"
            ),
            stamp=self._clock,
            step_no=step_no,
        )
        gate, gate_payload = self._gate(step_no, attempt, on_event=on_event)
        query = AdvisorQuery(
            question=question,
            context=_review_context(
                snapshot,
                question=question,
                source_root=self._source_root,
                gate=gate_payload,
            ),
            step_no=step_no,
        )

        observations: list[AdvisorObservation] = []
        summaries: list[CostSummary] = []
        stage_costs: list[dict[str, Any]] = []
        previous = started_at
        for reviewer in self._reviewers:
            component = component_for(reviewer.source)
            _emit(
                on_event,
                level=EVENT_LEVEL_INFO,
                component=component,
                action="start",
                message=(
                    f"advisor {reviewer.source} started (one question, step "
                    f"{'-' if step_no is None else step_no})"
                ),
                stamp=self._clock,
                step_no=step_no,
            )
            observations.append(
                self._observer(
                    reviewer.source,
                    lambda reviewer=reviewer: reviewer.advisor.advise(query),
                    relation=reviewer.relation,
                    relation_target=reviewer.relation_target,
                )
            )
            current = self._clock()
            summary = self._cost(
                provider=reviewer.source, from_time=previous, to_time=current
            )
            previous = current
            if summary is not None:
                summaries.append(summary)
            stage_costs.append(_cost_payload(summary))
            observation = observations[-1]
            _emit(
                on_event,
                level=_outcome_level(observation.status),
                component=component,
                action=_outcome_action(observation.status),
                message=_outcome_message(reviewer.source, observation),
                stamp=self._clock,
                step_no=step_no,
            )

        view = merge_evidence(observations, gate=gate)
        review_id = _review_id(question, step_no, gate, view)
        _emit(
            on_event,
            level=EVENT_LEVEL_INFO,
            component="Merger",
            action="merge",
            message=(
                f"evidence merged: {len(view.finding_ids)} finding(s), "
                f"{len(view.supporting_ids)} supporting, "
                f"{len(view.conflicting_ids)} conflicting, "
                f"{len(view.unresolved_ids)} unresolved, "
                f"{len(view.abstained)} abstain(s), {len(view.errors)} error(s), "
                f"{len(view.conflicts)} conflict(s)"
            ),
            stamp=self._clock,
            step_no=step_no,
            review_id=review_id,
        )
        stages = tuple(
            _stage(reviewer, observation, view, cost)
            for reviewer, observation, cost in zip(
                self._reviewers, observations, stage_costs
            )
        )

        result = synthesize_decision(
            view, gate=gate, step_no=step_no, clock=self._clock
        )
        _emit(
            on_event,
            level=EVENT_LEVEL_INFO,
            component="Decision",
            action="decide",
            message=(
                f"advisory decision {result.decision.status.value}: "
                f"{result.decision.decision}"
            ),
            stamp=self._clock,
            step_no=step_no,
            review_id=review_id,
        )

        judge = self._judge_outcome(
            view, on_event=on_event, step_no=step_no, review_id=review_id
        )
        finished_at = self._clock()
        if judge.consulted:
            judge_summary = self._cost(from_time=previous, to_time=finished_at)
            if judge_summary is not None:
                summaries.append(judge_summary)
            judge_cost = _cost_payload(judge_summary)
        else:
            judge_cost = _cost_payload(None, reason="the judge was not consulted")

        total = _cost_total(summaries) if summaries else None
        if total is None:
            _emit(
                on_event,
                level=EVENT_LEVEL_WARN,
                component="Cost",
                action="unavailable",
                message=(
                    "cost unavailable: no cost capability was injected, so this "
                    "review has no cost record"
                ),
                stamp=self._clock,
                step_no=step_no,
                review_id=review_id,
            )
        else:
            _emit(
                on_event,
                level=EVENT_LEVEL_INFO,
                component="Cost",
                action="collect",
                message=(
                    f"cost collected: {total.total_usd:.4f} USD over "
                    f"{total.record_count} record(s) "
                    f"({total.priced_record_count} priced, "
                    f"{total.unpriced_record_count} unpriced)"
                ),
                stamp=self._clock,
                step_no=step_no,
                review_id=review_id,
            )

        check_root = self._check_source_root()
        architecture = snapshot.architecture
        result_payload = ArchitectureReviewResult(
            review_id=review_id,
            reviewed_at=started_at,
            question=question,
            project=snapshot.project.name,
            source_root=self._source_root,
            check_source_root=check_root,
            source_root_verified=_same_path(check_root, self._source_root),
            step_no=step_no,
            architecture_version=(
                None if architecture is None else architecture.version
            ),
            deterministic_gate=gate_payload,
            providers=stages,
            evidence=view.to_dict(),
            conflicts=tuple(
                conflict.to_dict() for conflict in view.conflicts
            ),
            judge=judge,
            decision=result.decision.to_dict(),
            cost={
                "providers": [
                    {"source": reviewer.source, **cost}
                    for reviewer, cost in zip(self._reviewers, stage_costs)
                ],
                "judge": judge_cost,
                "total": (
                    _cost_payload(total)
                    if total is not None
                    else _cost_payload(
                        None,
                        reason="no cost record was produced for this review",
                    )
                ),
            },
        )
        _emit(
            on_event,
            level=EVENT_LEVEL_INFO,
            component="CoreWorker",
            action="review-complete",
            message=(
                f"review {review_id} complete: {len(stages)} advisor(s), "
                f"{len(result_payload.findings)} finding(s), "
                f"{len(result_payload.conflicts)} conflict(s), judge "
                f"{judge.status}, decision {result.decision.status.value}, "
                f"gate {'unavailable' if not gate_payload['available'] else ('compliant' if gate_payload['compliant'] else 'violated')}"
            ),
            stamp=self._clock,
            step_no=step_no,
            review_id=review_id,
        )
        return result_payload

    # -- the deterministic gate (read-only) --------------------------------
    def _gate(
        self,
        step_no: Optional[int],
        attempt: int,
        *,
        on_event: Optional[Callable[[Mapping[str, Any]], None]] = None,
        review_id: Optional[str] = None,
    ) -> tuple[Optional[Decision], dict[str, Any]]:
        """A fresh, read-only deterministic verdict - or an honest 'unavailable'.

        The gate is validated *now*, for this attempt, exactly as the loop does -
        but through the check port, which persists nothing: the review reads the
        verdict and never records it. A failure is described instead of raised, so
        a broken control plane cannot swallow an advisory review; the reason is
        the exception **type name** only.
        """
        if self._check is None:
            reason = "no realization check was injected"
            _emit(
                on_event,
                level=EVENT_LEVEL_WARN,
                component="Gate",
                action="check",
                message=f"deterministic gate unavailable: {reason}",
                stamp=self._clock,
                step_no=step_no,
                review_id=review_id,
            )
            return None, _gate_unavailable(reason)
        if step_no is None:
            reason = "there is no current step to check"
            _emit(
                on_event,
                level=EVENT_LEVEL_WARN,
                component="Gate",
                action="check",
                message=f"deterministic gate unavailable: {reason}",
                stamp=self._clock,
                step_no=step_no,
                review_id=review_id,
            )
            return None, _gate_unavailable(reason)
        try:
            result = self._check.check(step_no, attempt)
        except Exception as error:  # reported, never raised
            reason = exception_reason(error)
            _emit(
                on_event,
                level=EVENT_LEVEL_ERROR,
                component="Gate",
                action="error",
                message=(
                    f"deterministic gate check failed: {reason} - the review "
                    "continues without a gate verdict"
                ),
                stamp=self._clock,
                step_no=step_no,
                review_id=review_id,
            )
            return None, _gate_unavailable(reason)
        payload = _gate_payload(result)
        _emit(
            on_event,
            level=EVENT_LEVEL_INFO,
            component="Gate",
            action="check",
            message=(
                f"deterministic gate: "
                f"{'compliant' if payload['compliant'] else 'violation(s)'}, "
                f"baseline {payload['baseline_version']}, "
                f"{payload['violation_count']} violation(s), decision "
                f"{payload['decision_id']}"
            ),
            stamp=self._clock,
            step_no=step_no,
            review_id=review_id,
        )
        return result.decision, payload

    def _check_source_root(self) -> Optional[str]:
        """The tree the injected check actually inspects, when it reports one.

        Duck-typed on purpose: ``RealizationCheckPort`` has no ``source_root``, so
        this is an *optional* cross-check that lets the panel show whether the
        tree being reviewed is the configured one - it never replaces the
        configured value and never guesses one.
        """
        root = getattr(self._check, "source_root", None)
        if root is None:
            return None
        return str(root)

    # -- the judge (a separate layer) --------------------------------------
    def _judge_outcome(
        self,
        view: EvidenceView,
        *,
        on_event: Optional[Callable[[Mapping[str, Any]], None]] = None,
        step_no: Optional[int] = None,
        review_id: Optional[str] = None,
    ) -> JudgeOutcome:
        """Resolve the merged conflicts - or report, sanitized, why not.

        The judge is consulted **only** for the conflicts the merger validated:
        no conflict means no call at all, and no judge means an explicit
        unavailable state. A judge failure is caught at this boundary on purpose:
        the advisor stages, the merged evidence, the deterministic gate result and
        the advisory decision are already complete and must survive it - and only
        the exception *type name* travels, never a provider message.

        Every branch is logged, including the ones where the judge is **not**
        consulted, so "no judge was asked" is visible rather than implied.
        """
        if self._judge is None:
            return self._not_consulted(
                on_event,
                reason="no judge is configured",
                step_no=step_no,
                review_id=review_id,
                available=False,
            )
        if not view.conflicts:
            return self._not_consulted(
                on_event,
                reason="no evidence conflict was merged",
                step_no=step_no,
                review_id=review_id,
                available=self._judge.is_available,
            )
        _emit(
            on_event,
            level=EVENT_LEVEL_INFO,
            component="Judge",
            action="start",
            message=(
                f"judge asked about {len(view.conflicts)} evidence conflict(s)"
            ),
            stamp=self._clock,
            step_no=step_no,
            review_id=review_id,
        )
        try:
            run = self._judge.resolve(view)
        except Exception as error:  # sanitized at the orchestration boundary
            reason = exception_reason(error)
            _emit(
                on_event,
                level=EVENT_LEVEL_ERROR,
                component="Judge",
                action="error",
                message=(
                    f"judge failed: {reason} - the review continues without a "
                    "judge answer"
                ),
                stamp=self._clock,
                step_no=step_no,
                review_id=review_id,
            )
            return JudgeOutcome(
                available=self._judge.is_available,
                consulted=True,
                status=JUDGE_STATUS_ERROR,
                reason=reason,
            )
        if not run.judge_available:
            # a use-case without an implementation is not a silent empty success
            return self._not_consulted(
                on_event,
                reason="the judge use-case has no judge implementation configured",
                step_no=step_no,
                review_id=review_id,
                available=False,
            )
        _emit(
            on_event,
            level=EVENT_LEVEL_INFO,
            component="Judge",
            action="result",
            message=(
                f"judge answered {len(run.judgments)} of "
                f"{len(view.conflicts)} conflict(s)"
            ),
            stamp=self._clock,
            step_no=step_no,
            review_id=review_id,
        )
        return JudgeOutcome(
            available=True,
            consulted=True,
            status=JUDGE_STATUS_OK,
            judgments=tuple(judgment.to_dict() for judgment in run.judgments),
        )

    def _not_consulted(
        self,
        on_event: Optional[Callable[[Mapping[str, Any]], None]],
        *,
        reason: str,
        step_no: Optional[int],
        review_id: Optional[str],
        available: bool,
    ) -> JudgeOutcome:
        """One judge outcome for every 'the judge was not asked' case, logged."""
        _emit(
            on_event,
            level=EVENT_LEVEL_INFO,
            component="Judge",
            action="not-consulted",
            message=f"judge not consulted: {reason}",
            stamp=self._clock,
            step_no=step_no,
            review_id=review_id,
        )
        return JudgeOutcome(
            available=available,
            consulted=False,
            status=(
                JUDGE_STATUS_UNAVAILABLE
                if not available
                else JUDGE_STATUS_NOT_CONSULTED
            ),
            reason=reason,
        )

    # -- cost --------------------------------------------------------------
    def _cost(
        self,
        *,
        provider: Optional[str] = None,
        from_time: datetime,
        to_time: datetime,
    ) -> Optional[CostSummary]:
        """One window of the review, aggregated through the existing CostPort."""
        if self._cost_query is None:
            return None
        return self._cost_query(
            CostQuery(provider=provider, from_time=from_time, to_time=to_time)
        )
