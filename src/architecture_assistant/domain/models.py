"""Dependency-free domain models.

Frozen standard-library dataclasses with explicit validation and deterministic
dict serialization. The module deliberately contains **no** persistence, network,
UI or plugin code - it is pure domain state.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from enum import Enum
from typing import Any, ClassVar, Mapping, Optional

from .enums import (
    ACRStatus,
    ADRStatus,
    DELIBERATION_MAX_REVIEW_ROUNDS,
    SUPERVISION_ALLOWING_STATUSES,
    DecisionStatus,
    DeliberationStage,
    DeliberationStageStatus,
    DeliberationStatus,
    Mode,
    Phase,
    ProposalStatus,
    ReportStatus,
    RiskLevel,
    RiskStatus,
    Severity,
    StepState,
    SupervisorAction,
    SupervisorRisk,
    SupervisorStatus,
    TaskState,
)

__all__ = [
    "Project",
    "Step",
    "Task",
    "ArchitectureVersion",
    "ArchitectureChangeRequest",
    "ArchitectureProposal",
    "DeliberationRun",
    "DeliberationArtifact",
    "SupervisionRecord",
    "ADR",
    "Risk",
    "Finding",
    "Decision",
    "utc_now",
]


def utc_now() -> datetime:
    """Return the current timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string; got {value!r}")
    return value


def _require_datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime; got {value!r}")
    return value


def _optional_datetime(value: Any, field_name: str) -> Optional[datetime]:
    if value is None:
        return None
    return _require_datetime(value, field_name)


def _require_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a bool; got {value!r}")
    return value


def _require_int(
    value: Any,
    field_name: str,
    *,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an int; got {value!r}")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}; got {value!r}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field_name} must be <= {maximum}; got {value!r}")
    return value


def _require_score(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number; got {value!r}")
    score = float(value)
    if not 0.0 <= score <= 1.0:
        raise ValueError(f"{field_name} must be between 0.0 and 1.0; got {value!r}")
    return score


def _coerce_enum(value: Any, enum_cls: type, field_name: str) -> Enum:
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value)
        except ValueError:
            pass
    valid = ", ".join(member.value for member in enum_cls)
    raise ValueError(f"{field_name} must be one of ({valid}); got {value!r}")


def _coerce_optional_enum(
    value: Any, enum_cls: type, field_name: str
) -> Optional[Enum]:
    if value is None:
        return None
    return _coerce_enum(value, enum_cls, field_name)


def _freeze_tuple(value: Any, field_name: str) -> tuple:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        raise ValueError(
            f"{field_name} must be a sequence of values, not a bare string"
        )
    return tuple(value)


def _freeze_mappings(value: Any, field_name: str) -> tuple:
    """Freeze a sequence of JSON-safe structured records.

    A proposal section (a module, a risk, an ADR candidate, a phase) is a
    structured record, so each entry must be a mapping - a bare string would
    silently turn a record into one field and is refused here.
    """
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        raise ValueError(
            f"{field_name} must be a sequence of mappings, not a bare string"
        )
    records = tuple(value)
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError(
                f"{field_name} entries must be mappings; got {record!r}"
            )
    return records


class DomainModel:
    """Mixin adding deterministic ``to_dict`` / ``from_dict`` to frozen dataclasses.

    Field kinds are declared explicitly per model via ``_ENUM_FIELDS``,
    ``_DATETIME_FIELDS``, ``_TUPLE_FIELDS`` and ``_MAPPING_FIELDS`` so that
    serialization is predictable (enum -> value, datetime -> ISO 8601,
    tuple -> list, mapping -> dict).
    """

    _ENUM_FIELDS: ClassVar[Mapping[str, type]] = {}
    _DATETIME_FIELDS: ClassVar[tuple[str, ...]] = ()
    _TUPLE_FIELDS: ClassVar[tuple[str, ...]] = ()
    _MAPPING_FIELDS: ClassVar[tuple[str, ...]] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict representation of this model."""
        result: dict[str, Any] = {}
        for model_field in fields(self):
            name = model_field.name
            value = getattr(self, name)
            if value is not None and name in self._ENUM_FIELDS:
                value = value.value
            elif value is not None and name in self._DATETIME_FIELDS:
                value = value.isoformat()
            elif name in self._TUPLE_FIELDS:
                value = list(value)
            elif name in self._MAPPING_FIELDS:
                value = dict(value)
            result[name] = value
        return result

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DomainModel":
        """Rebuild a model from a dict produced by :meth:`to_dict`.

        Unknown keys are ignored (forward compatible with later schema steps);
        missing keys fall back to the dataclass defaults.
        """
        kwargs: dict[str, Any] = {}
        for model_field in fields(cls):
            name = model_field.name
            if name not in data:
                continue
            value = data[name]
            if value is not None and name in cls._ENUM_FIELDS:
                value = cls._ENUM_FIELDS[name](value)
            elif value is not None and name in cls._DATETIME_FIELDS:
                if not isinstance(value, datetime):
                    value = datetime.fromisoformat(value)
            elif name in cls._TUPLE_FIELDS:
                value = tuple(value) if value is not None else ()
            elif name in cls._MAPPING_FIELDS:
                value = dict(value) if value is not None else {}
            kwargs[name] = value
        return cls(**kwargs)



@dataclass(frozen=True)
class Project(DomainModel):
    """Top-level project aggregate.

    ``current_step_no_snapshot`` is an explicitly **non-authoritative** cache.
    The authoritative current step is derived from the Steps repository in later
    steps; this field must never be treated as a second source of truth.
    """

    name: str
    plan_version: str
    plan_hash: str = ""
    mode: Mode = Mode.MANUAL
    paused: bool = False
    current_step_no_snapshot: Optional[int] = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    _ENUM_FIELDS: ClassVar[Mapping[str, type]] = {"mode": Mode}
    _DATETIME_FIELDS: ClassVar[tuple[str, ...]] = ("created_at", "updated_at")

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _require_text(self.name, "name"))
        object.__setattr__(
            self, "plan_version", _require_text(self.plan_version, "plan_version")
        )
        object.__setattr__(self, "plan_hash", self.plan_hash or "")
        object.__setattr__(self, "mode", _coerce_enum(self.mode, Mode, "mode"))
        object.__setattr__(self, "paused", _require_bool(self.paused, "paused"))
        if self.current_step_no_snapshot is not None:
            object.__setattr__(
                self,
                "current_step_no_snapshot",
                _require_int(
                    self.current_step_no_snapshot,
                    "current_step_no_snapshot",
                    minimum=1,
                ),
            )
        object.__setattr__(
            self, "created_at", _require_datetime(self.created_at, "created_at")
        )
        object.__setattr__(
            self, "updated_at", _require_datetime(self.updated_at, "updated_at")
        )

    @property
    def current_step_no(self) -> Optional[int]:
        """Non-authoritative snapshot alias; NOT a source of truth."""
        return self.current_step_no_snapshot


@dataclass(frozen=True)
class Step(DomainModel):
    """A single build-plan step and its master-workflow state."""

    step_no: int
    phase: Phase
    title: str
    state: StepState = StepState.PENDING
    description: str = ""
    attempt: int = 0
    max_attempts: int = 3
    risk: RiskLevel = RiskLevel.LOW
    requires_human: bool = False
    created_at: datetime = field(default_factory=utc_now)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    verified_at: Optional[datetime] = None
    last_update_at: Optional[datetime] = None

    _ENUM_FIELDS: ClassVar[Mapping[str, type]] = {"phase": Phase, "state": StepState, "risk": RiskLevel}
    _DATETIME_FIELDS: ClassVar[tuple[str, ...]] = (
        "created_at",
        "started_at",
        "finished_at",
        "verified_at",
        "last_update_at",
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "step_no", _require_int(self.step_no, "step_no", minimum=1)
        )
        object.__setattr__(self, "phase", _coerce_enum(self.phase, Phase, "phase"))
        object.__setattr__(self, "title", _require_text(self.title, "title"))
        object.__setattr__(self, "state", _coerce_enum(self.state, StepState, "state"))
        object.__setattr__(self, "risk", _coerce_enum(self.risk, RiskLevel, "risk"))
        object.__setattr__(
            self, "attempt", _require_int(self.attempt, "attempt", minimum=0)
        )
        object.__setattr__(
            self,
            "max_attempts",
            _require_int(self.max_attempts, "max_attempts", minimum=1),
        )
        if self.attempt > self.max_attempts:
            raise ValueError(
                f"attempt ({self.attempt}) must be <= max_attempts ({self.max_attempts})"
            )
        object.__setattr__(
            self, "requires_human", _require_bool(self.requires_human, "requires_human")
        )
        object.__setattr__(
            self, "created_at", _require_datetime(self.created_at, "created_at")
        )
        for name in ("started_at", "finished_at", "verified_at", "last_update_at"):
            object.__setattr__(
                self, name, _optional_datetime(getattr(self, name), name)
            )

    @property
    def can_retry(self) -> bool:
        """Whether another attempt is still allowed."""
        return self.attempt < self.max_attempts



@dataclass(frozen=True)
class Task(DomainModel):
    """A Cline dispatch unit derived from a Step.

    The Task carries the structured Assistant -> Cline contract
    (``instructions`` / ``report_schema``). Its ``state`` is a minimal,
    subordinate dispatch marker; the Step FSM stays authoritative.
    """

    step_no: int
    phase: Phase
    title: str
    description: str = ""
    risk: RiskLevel = RiskLevel.MEDIUM
    attempt: int = 1
    max_attempts: int = 3
    state: TaskState = TaskState.CREATED
    context_file: Optional[str] = None
    instructions: Mapping[str, Any] = field(default_factory=dict)
    report_schema: Mapping[str, Any] = field(default_factory=dict)
    report_status: Optional[ReportStatus] = None
    created_at: datetime = field(default_factory=utc_now)

    _ENUM_FIELDS: ClassVar[Mapping[str, type]] = {
        "phase": Phase,
        "risk": RiskLevel,
        "state": TaskState,
        "report_status": ReportStatus,
    }
    _DATETIME_FIELDS: ClassVar[tuple[str, ...]] = ("created_at",)
    _MAPPING_FIELDS: ClassVar[tuple[str, ...]] = ("instructions", "report_schema")

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "step_no", _require_int(self.step_no, "step_no", minimum=1)
        )
        object.__setattr__(self, "phase", _coerce_enum(self.phase, Phase, "phase"))
        object.__setattr__(self, "title", _require_text(self.title, "title"))
        object.__setattr__(self, "risk", _coerce_enum(self.risk, RiskLevel, "risk"))
        object.__setattr__(self, "state", _coerce_enum(self.state, TaskState, "state"))
        object.__setattr__(
            self,
            "report_status",
            _coerce_optional_enum(self.report_status, ReportStatus, "report_status"),
        )
        object.__setattr__(
            self, "attempt", _require_int(self.attempt, "attempt", minimum=1)
        )
        object.__setattr__(
            self,
            "max_attempts",
            _require_int(self.max_attempts, "max_attempts", minimum=1),
        )
        if self.attempt > self.max_attempts:
            raise ValueError(
                f"attempt ({self.attempt}) must be <= max_attempts ({self.max_attempts})"
            )
        object.__setattr__(
            self, "instructions", dict(self.instructions or {})
        )
        object.__setattr__(
            self, "report_schema", dict(self.report_schema or {})
        )
        object.__setattr__(
            self, "created_at", _require_datetime(self.created_at, "created_at")
        )


@dataclass(frozen=True)
class ArchitectureVersion(DomainModel):
    """A versioned architecture baseline plus its deterministic rules."""

    version: str
    baseline: str = ""
    rules: tuple[str, ...] = ()
    superseded_by: Optional[str] = None
    is_current: bool = True
    created_at: datetime = field(default_factory=utc_now)

    _DATETIME_FIELDS: ClassVar[tuple[str, ...]] = ("created_at",)
    _TUPLE_FIELDS: ClassVar[tuple[str, ...]] = ("rules",)

    def __post_init__(self) -> None:
        object.__setattr__(self, "version", _require_text(self.version, "version"))
        object.__setattr__(
            self, "is_current", _require_bool(self.is_current, "is_current")
        )
        object.__setattr__(self, "rules", _freeze_tuple(self.rules, "rules"))
        if self.superseded_by is not None:
            object.__setattr__(
                self,
                "superseded_by",
                _require_text(self.superseded_by, "superseded_by"),
            )
            if self.is_current:
                raise ValueError(
                    "a version with superseded_by set must have is_current=False"
                )
        object.__setattr__(
            self, "created_at", _require_datetime(self.created_at, "created_at")
        )



@dataclass(frozen=True)
class ArchitectureChangeRequest(DomainModel):
    """An authoritative request to evolve the architecture baseline.

    This is **long-lived state**, not a transient DTO: it survives restarts,
    carries its own lifecycle (``PROPOSED -> APPROVED -> APPLIED`` or
    ``PROPOSED -> REJECTED``) and links *structurally* to its ADR (``adr_id``)
    and to the two baselines it moves between (``source_version`` /
    ``target_version``). Free-form ADR prose is a human-readable explanation,
    never the referential link.
    """

    request_id: str
    title: str
    rationale: str
    source_version: str
    target_version: str
    rule_ids: tuple[str, ...]
    adr_id: str
    roadmap_impact: tuple[str, ...] = ()
    status: ACRStatus = ACRStatus.PROPOSED
    created_at: datetime = field(default_factory=utc_now)
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    applied_at: Optional[datetime] = None

    _ENUM_FIELDS: ClassVar[Mapping[str, type]] = {"status": ACRStatus}
    _DATETIME_FIELDS: ClassVar[tuple[str, ...]] = (
        "created_at",
        "approved_at",
        "applied_at",
    )
    _TUPLE_FIELDS: ClassVar[tuple[str, ...]] = ("rule_ids", "roadmap_impact")

    def __post_init__(self) -> None:
        for name in (
            "request_id",
            "title",
            "rationale",
            "source_version",
            "target_version",
            "adr_id",
        ):
            object.__setattr__(
                self, name, _require_text(getattr(self, name), name)
            )
        object.__setattr__(
            self, "status", _coerce_enum(self.status, ACRStatus, "status")
        )
        rules = _freeze_tuple(self.rule_ids, "rule_ids")
        if not rules:
            raise ValueError("rule_ids must declare at least one rule id")
        for rule_id in rules:
            _require_text(rule_id, "rule_ids entry")
        object.__setattr__(self, "rule_ids", rules)
        impact = _freeze_tuple(self.roadmap_impact, "roadmap_impact")
        for item in impact:
            _require_text(item, "roadmap_impact entry")
        object.__setattr__(self, "roadmap_impact", impact)
        if self.source_version == self.target_version:
            raise ValueError("target_version must differ from source_version")
        object.__setattr__(
            self, "created_at", _require_datetime(self.created_at, "created_at")
        )
        object.__setattr__(
            self, "approved_at", _optional_datetime(self.approved_at, "approved_at")
        )
        object.__setattr__(
            self, "applied_at", _optional_datetime(self.applied_at, "applied_at")
        )
        if self.approved_by is not None:
            object.__setattr__(
                self, "approved_by", _require_text(self.approved_by, "approved_by")
            )
        if self.status in (ACRStatus.APPROVED, ACRStatus.APPLIED):
            if not self.approved_by:
                raise ValueError(
                    f"status {self.status.value} requires approved_by"
                )
            if self.approved_at is None:
                raise ValueError(
                    f"status {self.status.value} requires approved_at"
                )
        elif self.approved_by is not None or self.approved_at is not None:
            raise ValueError(
                f"status {self.status.value} must not carry approval fields"
            )
        if self.status is ACRStatus.APPLIED:
            if self.applied_at is None:
                raise ValueError("status APPLIED requires applied_at")
        elif self.applied_at is not None:
            raise ValueError(
                f"status {self.status.value} must not carry applied_at"
            )

    @property
    def is_terminal(self) -> bool:
        """``REJECTED`` and ``APPLIED`` are terminal; both are never revived."""
        return self.status in (ACRStatus.REJECTED, ACRStatus.APPLIED)


@dataclass(frozen=True)
class ArchitectureProposal(DomainModel):
    """A **managed-project** architecture proposal - never a baseline.

    Scope (the boundary this type exists to keep)
    ---------------------------------------------
    :class:`ArchitectureVersion` describes the architecture of the assistant
    **itself**: it is declared in code and enforced by the deterministic
    validator over the assistant's own source tree. This proposal describes the
    architecture of the **project the assistant manages** (for example
    ``youtube_to_mp3``). The two are deliberately unrelated:

    * a proposal never becomes an :class:`ArchitectureVersion`;
    * it never creates an :class:`ArchitectureChangeRequest`;
    * it never writes an assistant ADR, an assistant risk or an assistant rule;
    * it is never read by the realization gate.

    ``ProposalStatus.APPROVED`` therefore means exactly one thing: *this is the
    human-approved design for this managed project*. Everything the proposal
    carries is advisory content until a human approves it, and even then it stays
    project-scoped state. ``requirement`` is the operator's own text, echoed
    **verbatim** - synthesis never rewrites it.

    ``revision_no`` / ``revision_of`` are the proposal's own versioning: a
    revision is a *new* row whose predecessor is marked
    ``ProposalStatus.SUPERSEDED``, so all revisions are retained.
    """

    proposal_id: str
    project: str
    created_at: datetime
    requirement: str
    summary: str
    source_review_id: str
    fingerprint: str
    modules: tuple[Mapping[str, Any], ...] = ()
    data_flows: tuple[Mapping[str, Any], ...] = ()
    external_dependencies: tuple[Mapping[str, Any], ...] = ()
    architecture_rules: tuple[str, ...] = ()
    proposed_rules: tuple[str, ...] = ()
    risks: tuple[Mapping[str, Any], ...] = ()
    adr_candidates: tuple[Mapping[str, Any], ...] = ()
    implementation_phases: tuple[Mapping[str, Any], ...] = ()
    unresolved_questions: tuple[str, ...] = ()
    rationale: str = ""
    review_digest: Mapping[str, Any] = field(default_factory=dict)
    architecture_version: Optional[str] = None
    revision_no: int = 1
    revision_of: Optional[str] = None
    status: ProposalStatus = ProposalStatus.DRAFT
    decided_by: str = ""
    decided_at: Optional[datetime] = None
    decision_reason: str = ""
    revision_feedback: str = ""
    superseded_by: Optional[str] = None

    _ENUM_FIELDS: ClassVar[Mapping[str, type]] = {"status": ProposalStatus}
    _DATETIME_FIELDS: ClassVar[tuple[str, ...]] = ("created_at", "decided_at")
    _TUPLE_FIELDS: ClassVar[tuple[str, ...]] = (
        "modules",
        "data_flows",
        "external_dependencies",
        "architecture_rules",
        "proposed_rules",
        "risks",
        "adr_candidates",
        "implementation_phases",
        "unresolved_questions",
    )
    _MAPPING_FIELDS: ClassVar[tuple[str, ...]] = ("review_digest",)

    def __post_init__(self) -> None:
        for name in (
            "proposal_id",
            "project",
            "requirement",
            "summary",
            "source_review_id",
            "fingerprint",
        ):
            object.__setattr__(
                self, name, _require_text(getattr(self, name), name)
            )
        object.__setattr__(
            self, "created_at", _require_datetime(self.created_at, "created_at")
        )
        object.__setattr__(
            self, "status", _coerce_enum(self.status, ProposalStatus, "status")
        )
        for name in (
            "modules",
            "data_flows",
            "external_dependencies",
            "risks",
            "adr_candidates",
            "implementation_phases",
        ):
            object.__setattr__(
                self, name, _freeze_mappings(getattr(self, name), name)
            )
        for name in (
            "architecture_rules",
            "proposed_rules",
            "unresolved_questions",
        ):
            object.__setattr__(
                self, name, _freeze_tuple(getattr(self, name), name)
            )
        for rule in (*self.architecture_rules, *self.proposed_rules):
            _require_text(rule, "rule text")
        for question in self.unresolved_questions:
            _require_text(question, "unresolved_questions entry")
        object.__setattr__(self, "review_digest", dict(self.review_digest or {}))
        object.__setattr__(
            self,
            "revision_no",
            _require_int(self.revision_no, "revision_no", minimum=1),
        )
        if self.revision_no == 1:
            if self.revision_of is not None:
                raise ValueError(
                    "revision_of must be None for the first revision "
                    "(revision_no 1)"
                )
        else:
            object.__setattr__(
                self,
                "revision_of",
                _require_text(self.revision_of, "revision_of"),
            )
            if self.revision_of == self.proposal_id:
                raise ValueError("a revision must not supersede itself")
        object.__setattr__(
            self, "decided_at", _optional_datetime(self.decided_at, "decided_at")
        )
        if self.decided_by and self.decided_at is None:
            raise ValueError("decided_by requires decided_at")
        if self.decided_at is not None and not self.decided_by.strip():
            raise ValueError("decided_at requires decided_by")
        if self.superseded_by is not None:
            object.__setattr__(
                self,
                "superseded_by",
                _require_text(self.superseded_by, "superseded_by"),
            )
            if self.status is not ProposalStatus.SUPERSEDED:
                raise ValueError(
                    f"status {self.status.value} must not carry superseded_by"
                )
        decided = (
            ProposalStatus.APPROVED,
            ProposalStatus.REJECTED,
            ProposalStatus.REVISION_REQUESTED,
        )
        if self.status in decided:
            if not self.decided_by.strip():
                raise ValueError(
                    f"status {self.status.value} requires decided_by"
                )
            if self.decided_at is None:
                raise ValueError(
                    f"status {self.status.value} requires decided_at"
                )
            if not self.decision_reason.strip():
                raise ValueError(
                    f"status {self.status.value} requires decision_reason"
                )
        elif self.status is ProposalStatus.DRAFT:
            if self.decided_by or self.decided_at is not None:
                raise ValueError("a DRAFT proposal must not carry a decision")
        if (
            self.status is ProposalStatus.REVISION_REQUESTED
            and not self.revision_feedback.strip()
        ):
            raise ValueError(
                "status REVISION_REQUESTED requires revision_feedback"
            )

    @property
    def is_decided(self) -> bool:
        """Whether a human already decided this proposal (any decision)."""
        return self.status is not ProposalStatus.DRAFT

    @property
    def is_settled(self) -> bool:
        """Whether nothing more can happen to this proposal.

        ``APPROVED``, ``REJECTED`` and ``SUPERSEDED`` are settled; a ``DRAFT`` or
        a ``REVISION_REQUESTED`` proposal is still open. This is the proposal's
        own vocabulary - it never borrows the ACR lifecycle.
        """
        return self.status in (
            ProposalStatus.APPROVED,
            ProposalStatus.REJECTED,
            ProposalStatus.SUPERSEDED,
        )


@dataclass(frozen=True)
class DeliberationRun(DomainModel):
    """One controlled architecture deliberation over one operator requirement.

    Scope (the boundary this type exists to keep)
    ---------------------------------------------
    A deliberation is **advisory** and **managed-project scoped**: it reviews the
    design of the project the assistant manages. It is therefore unrelated to
    ``ArchitectureVersion`` (the assistant's own baseline), to
    ``ArchitectureChangeRequest``, to an assistant ADR/risk/rule and to the
    realization gate. It also carries **no** workflow authority: it cannot set
    ``VERIFIED``, move a Step, or approve anything.

    The run is the *identity* half of the pair
    ------------------------------------------
    ``deliberation_id`` is a deterministic content hash of the material inputs
    (project, requirement, the three configured provider/model pairs, the bounded
    context fingerprint and the revision lineage), never a timestamp. Every
    stage result is a separate :class:`DeliberationArtifact` row, so the
    lifecycle stays queryable and a stage is individually addressable.

    ``revision_no`` / ``revision_of_deliberation_id`` are the run's own lineage:
    a human "re-run with feedback" creates a **new** run pointing at the previous
    one, and the previous run stays immutable. Nothing here re-deliberates by
    itself.
    """

    deliberation_id: str
    project: str
    requirement: str
    fingerprint: str
    context_fingerprint: str
    agent_a_provider: str
    agent_a_model: str
    agent_b_provider: str
    agent_b_model: str
    lead_provider: str
    lead_model: str
    created_at: datetime
    updated_at: datetime
    status: DeliberationStatus = DeliberationStatus.DRAFT
    max_review_rounds: int = DELIBERATION_MAX_REVIEW_ROUNDS
    revision_no: int = 1
    revision_of_deliberation_id: Optional[str] = None
    error_reason: str = ""
    stale_reason: str = ""

    _ENUM_FIELDS: ClassVar[Mapping[str, type]] = {"status": DeliberationStatus}
    _DATETIME_FIELDS: ClassVar[tuple[str, ...]] = ("created_at", "updated_at")

    def __post_init__(self) -> None:
        for name in (
            "deliberation_id",
            "project",
            "requirement",
            "fingerprint",
            "context_fingerprint",
            "agent_a_provider",
            "agent_a_model",
            "agent_b_provider",
            "agent_b_model",
            "lead_provider",
            "lead_model",
        ):
            object.__setattr__(
                self, name, _require_text(getattr(self, name), name)
            )
        object.__setattr__(
            self,
            "status",
            _coerce_enum(self.status, DeliberationStatus, "status"),
        )
        for name in ("created_at", "updated_at"):
            object.__setattr__(
                self, name, _require_datetime(getattr(self, name), name)
            )
        object.__setattr__(
            self,
            "max_review_rounds",
            _require_int(
                self.max_review_rounds, "max_review_rounds", minimum=1, maximum=1
            ),
        )
        object.__setattr__(
            self,
            "revision_no",
            _require_int(self.revision_no, "revision_no", minimum=1),
        )
        if self.revision_no == 1:
            if self.revision_of_deliberation_id is not None:
                raise ValueError(
                    "revision_of_deliberation_id must be None for the first "
                    "revision (revision_no 1)"
                )
        else:
            object.__setattr__(
                self,
                "revision_of_deliberation_id",
                _require_text(
                    self.revision_of_deliberation_id,
                    "revision_of_deliberation_id",
                ),
            )
            if self.revision_of_deliberation_id == self.deliberation_id:
                raise ValueError("a revision must not supersede itself")
        object.__setattr__(self, "error_reason", self.error_reason or "")
        object.__setattr__(self, "stale_reason", self.stale_reason or "")

    @property
    def is_settled(self) -> bool:
        """Whether nothing more can happen without a new run.

        ``READY_FOR_PROPOSAL`` is settled *for the deliberation*: the synthesis
        exists and only a human proposal decision remains.
        """
        return self.status in (
            DeliberationStatus.READY_FOR_PROPOSAL,
            DeliberationStatus.ERROR,
            DeliberationStatus.CANCELLED,
        )

    @property
    def is_terminal_failure(self) -> bool:
        """Whether the run stopped without ever reaching a final synthesis."""
        return self.status in (
            DeliberationStatus.ERROR,
            DeliberationStatus.CANCELLED,
        )


@dataclass(frozen=True)
class DeliberationArtifact(DomainModel):
    """One addressable stage result of one deliberation.

    ``stage`` names which of the six stages produced it, ``slot`` names which
    seat answered (``agent_a`` / ``agent_b`` / ``lead``) and ``content`` carries
    the stage's own structured, JSON-safe result. ``fingerprint`` is a content
    hash of ``(deliberation_id, stage, slot, content)``, so a rewritten artifact
    is detectable and a downstream stage can depend on the **exact** upstream
    identity instead of on a timestamp.

    An artifact never carries a credential and never carries hidden provider
    reasoning: the adapters return structured conclusions only, and the content
    is validated by the use-case before it is stored.
    """

    artifact_id: str
    deliberation_id: str
    stage: DeliberationStage
    status: DeliberationStageStatus
    content: Mapping[str, Any] = field(default_factory=dict)
    fingerprint: str = ""
    slot: str = ""
    created_at: datetime = field(default_factory=utc_now)

    _ENUM_FIELDS: ClassVar[Mapping[str, type]] = {
        "stage": DeliberationStage,
        "status": DeliberationStageStatus,
    }
    _DATETIME_FIELDS: ClassVar[tuple[str, ...]] = ("created_at",)
    _MAPPING_FIELDS: ClassVar[tuple[str, ...]] = ("content",)

    def __post_init__(self) -> None:
        for name in ("artifact_id", "deliberation_id"):
            object.__setattr__(
                self, name, _require_text(getattr(self, name), name)
            )
        object.__setattr__(
            self, "stage", _coerce_enum(self.stage, DeliberationStage, "stage")
        )
        object.__setattr__(
            self,
            "status",
            _coerce_enum(self.status, DeliberationStageStatus, "status"),
        )
        object.__setattr__(self, "content", dict(self.content or {}))
        object.__setattr__(self, "slot", self.slot or "")
        if self.fingerprint:
            object.__setattr__(
                self,
                "fingerprint",
                _require_text(self.fingerprint, "fingerprint"),
            )
        object.__setattr__(
            self,
            "created_at",
            _require_datetime(self.created_at, "created_at"),
        )

    @property
    def is_usable(self) -> bool:
        """Whether a downstream stage may depend on this artifact."""
        return self.status is DeliberationStageStatus.COMPLETE


@dataclass(frozen=True)
class ADR(DomainModel):
    """A versioned Architecture Decision Record."""

    id: str
    title: str
    status: ADRStatus = ADRStatus.PROPOSED
    context: str = ""
    decision: str = ""
    consequences: tuple[str, ...] = ()
    version: int = 1
    superseded_by: Optional[str] = None
    related: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=utc_now)
    updated_at: Optional[datetime] = None

    _ENUM_FIELDS: ClassVar[Mapping[str, type]] = {"status": ADRStatus}
    _DATETIME_FIELDS: ClassVar[tuple[str, ...]] = ("created_at", "updated_at")
    _TUPLE_FIELDS: ClassVar[tuple[str, ...]] = ("consequences", "related")

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _require_text(self.id, "id"))
        object.__setattr__(self, "title", _require_text(self.title, "title"))
        object.__setattr__(
            self, "status", _coerce_enum(self.status, ADRStatus, "status")
        )
        object.__setattr__(
            self, "version", _require_int(self.version, "version", minimum=1)
        )
        object.__setattr__(
            self, "consequences", _freeze_tuple(self.consequences, "consequences")
        )
        object.__setattr__(self, "related", _freeze_tuple(self.related, "related"))
        if self.superseded_by is not None:
            object.__setattr__(
                self,
                "superseded_by",
                _require_text(self.superseded_by, "superseded_by"),
            )
        if self.status is ADRStatus.SUPERSEDED and not self.superseded_by:
            raise ValueError("status SUPERSEDED requires superseded_by")
        object.__setattr__(
            self, "created_at", _require_datetime(self.created_at, "created_at")
        )
        object.__setattr__(
            self, "updated_at", _optional_datetime(self.updated_at, "updated_at")
        )


@dataclass(frozen=True)
class Risk(DomainModel):
    """A risk-register entry with owner, mitigation and lifecycle status."""

    id: str
    description: str
    severity: Severity = Severity.MEDIUM
    probability: float = 0.5
    impact: Severity = Severity.MEDIUM
    owner: str = ""
    mitigation: str = ""
    status: RiskStatus = RiskStatus.OPEN
    created_at: datetime = field(default_factory=utc_now)
    updated_at: Optional[datetime] = None

    _ENUM_FIELDS: ClassVar[Mapping[str, type]] = {
        "severity": Severity,
        "impact": Severity,
        "status": RiskStatus,
    }
    _DATETIME_FIELDS: ClassVar[tuple[str, ...]] = ("created_at", "updated_at")

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _require_text(self.id, "id"))
        object.__setattr__(
            self, "description", _require_text(self.description, "description")
        )
        object.__setattr__(
            self, "severity", _coerce_enum(self.severity, Severity, "severity")
        )
        object.__setattr__(
            self, "probability", _require_score(self.probability, "probability")
        )
        object.__setattr__(self, "impact", _coerce_enum(self.impact, Severity, "impact"))
        object.__setattr__(self, "status", _coerce_enum(self.status, RiskStatus, "status"))
        object.__setattr__(
            self, "created_at", _require_datetime(self.created_at, "created_at")
        )
        object.__setattr__(
            self, "updated_at", _optional_datetime(self.updated_at, "updated_at")
        )



@dataclass(frozen=True)
class Finding(DomainModel):
    """An evidence-based finding produced by an advisor/challenger role."""

    id: str
    source: str
    claim: str
    evidence: tuple[str, ...] = ()
    confidence: float = 0.5
    severity: Severity = Severity.MEDIUM
    step_no: Optional[int] = None
    created_at: datetime = field(default_factory=utc_now)

    _ENUM_FIELDS: ClassVar[Mapping[str, type]] = {"severity": Severity}
    _DATETIME_FIELDS: ClassVar[tuple[str, ...]] = ("created_at",)
    _TUPLE_FIELDS: ClassVar[tuple[str, ...]] = ("evidence",)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _require_text(self.id, "id"))
        object.__setattr__(self, "source", _require_text(self.source, "source"))
        object.__setattr__(self, "claim", _require_text(self.claim, "claim"))
        object.__setattr__(
            self, "evidence", _freeze_tuple(self.evidence, "evidence")
        )
        object.__setattr__(
            self, "confidence", _require_score(self.confidence, "confidence")
        )
        object.__setattr__(
            self, "severity", _coerce_enum(self.severity, Severity, "severity")
        )
        if self.step_no is not None:
            object.__setattr__(
                self,
                "step_no",
                _require_int(self.step_no, "step_no", minimum=1),
            )
        object.__setattr__(
            self, "created_at", _require_datetime(self.created_at, "created_at")
        )


@dataclass(frozen=True)
class Decision(DomainModel):
    """The decision-engine outcome (rules > evidence > perspectives)."""

    id: str
    status: DecisionStatus = DecisionStatus.PENDING
    decision: str = ""
    rationale: str = ""
    rules_applied: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    perspectives: tuple[str, ...] = ()
    step_no: Optional[int] = None
    created_at: datetime = field(default_factory=utc_now)

    _ENUM_FIELDS: ClassVar[Mapping[str, type]] = {"status": DecisionStatus}
    _DATETIME_FIELDS: ClassVar[tuple[str, ...]] = ("created_at",)
    _TUPLE_FIELDS: ClassVar[tuple[str, ...]] = (
        "rules_applied",
        "evidence_refs",
        "perspectives",
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _require_text(self.id, "id"))
        object.__setattr__(
            self, "status", _coerce_enum(self.status, DecisionStatus, "status")
        )
        object.__setattr__(
            self, "rules_applied", _freeze_tuple(self.rules_applied, "rules_applied")
        )
        object.__setattr__(
            self, "evidence_refs", _freeze_tuple(self.evidence_refs, "evidence_refs")
        )
        object.__setattr__(
            self, "perspectives", _freeze_tuple(self.perspectives, "perspectives")
        )
        if (
            self.status in (DecisionStatus.ACCEPTED, DecisionStatus.REJECTED)
            and not self.decision.strip()
        ):
            raise ValueError(
                f"status {self.status.value} requires a non-empty decision"
            )
        if self.step_no is not None:
            object.__setattr__(
                self,
                "step_no",
                _require_int(self.step_no, "step_no", minimum=1),
            )
        object.__setattr__(
            self, "created_at", _require_datetime(self.created_at, "created_at")
        )


@dataclass(frozen=True)
class SupervisionRecord(DomainModel):
    """One persisted supervision identity - one exact worker report.

    The record is keyed by ``supervision_id``, which is derived from
    ``(project, step_no, attempt, source_report_hash, architecture_version)`` and
    from nothing else: not from the supervisor's answer, not from a reason, a
    risk, an instruction or a timestamp. That is what makes the identity stable
    across a restart *and* what makes a rewritten report a genuinely new record -
    the decision taken for ``R1`` can never authorize ``R2``, because ``R2``
    simply has a different row.

    This state is **advisory bookkeeping, never authorization**: the record can
    describe a recommended directive and a human decision about it, but it holds
    no Step, no attempt counter and no baseline. The assistant's own tables stay
    the single source of truth for the workflow, and nothing here is ever written
    from a provider adapter.

    ``first_seen_at`` is the report's own first-seen moment for *this* hash. For a
    :attr:`SupervisorStatus.MALFORMED` record it doubles as the per-hash
    stability anchor (the bytes never change without changing the hash), so the
    absolute malformed deadline of one step/attempt is the **minimum**
    ``first_seen_at`` across its malformed records - persisted, restart-safe and
    deliberately never reset when the malformed bytes change.
    """

    supervision_id: str
    project: str
    step_no: int
    attempt: int
    source_report_hash: str
    architecture_version: str
    status: SupervisorStatus
    action: Optional[SupervisorAction] = None
    reason: str = ""
    risk: Optional[SupervisorRisk] = None
    instruction_for_cline: str = ""
    requires_human: bool = False
    provider: str = ""
    cost_available: bool = False
    evidence: tuple[str, ...] = ()
    first_seen_at: datetime = field(default_factory=utc_now)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    decided_by: str = ""
    decided_at: Optional[datetime] = None
    decision_reason: str = ""
    sent_at: Optional[datetime] = None
    escalated_at: Optional[datetime] = None

    _ENUM_FIELDS: ClassVar[Mapping[str, type]] = {
        "status": SupervisorStatus,
        "action": SupervisorAction,
        "risk": SupervisorRisk,
    }
    _DATETIME_FIELDS: ClassVar[tuple[str, ...]] = (
        "first_seen_at",
        "created_at",
        "updated_at",
        "decided_at",
        "sent_at",
        "escalated_at",
    )
    _TUPLE_FIELDS: ClassVar[tuple[str, ...]] = ("evidence",)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "supervision_id",
            _require_text(self.supervision_id, "supervision_id"),
        )
        object.__setattr__(
            self, "project", _require_text(self.project, "project")
        )
        object.__setattr__(
            self, "step_no", _require_int(self.step_no, "step_no", minimum=1)
        )
        object.__setattr__(
            self, "attempt", _require_int(self.attempt, "attempt", minimum=1)
        )
        digest = self.source_report_hash
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError(
                "source_report_hash must be a 64-character lowercase SHA-256 "
                f"hex digest; got {digest!r}"
            )
        object.__setattr__(
            self,
            "architecture_version",
            _require_text(self.architecture_version, "architecture_version"),
        )
        object.__setattr__(
            self,
            "status",
            _coerce_enum(self.status, SupervisorStatus, "status"),
        )
        object.__setattr__(
            self,
            "action",
            _coerce_optional_enum(self.action, SupervisorAction, "action"),
        )
        object.__setattr__(
            self,
            "risk",
            _coerce_optional_enum(self.risk, SupervisorRisk, "risk"),
        )
        object.__setattr__(self, "reason", self.reason or "")
        object.__setattr__(
            self, "instruction_for_cline", self.instruction_for_cline or ""
        )
        object.__setattr__(
            self,
            "requires_human",
            _require_bool(self.requires_human, "requires_human"),
        )
        object.__setattr__(self, "provider", self.provider or "")
        object.__setattr__(
            self,
            "cost_available",
            _require_bool(self.cost_available, "cost_available"),
        )
        object.__setattr__(
            self, "evidence", _freeze_tuple(self.evidence, "evidence")
        )
        for name in ("first_seen_at", "created_at", "updated_at"):
            object.__setattr__(
                self, name, _require_datetime(getattr(self, name), name)
            )
        for name in ("decided_at", "sent_at", "escalated_at"):
            object.__setattr__(
                self, name, _optional_datetime(getattr(self, name), name)
            )
        object.__setattr__(
            self, "decided_by", self.decided_by or ""
        )
        object.__setattr__(
            self, "decision_reason", self.decision_reason or ""
        )
        if self.status is SupervisorStatus.MALFORMED and self.instruction_for_cline:
            raise ValueError(
                "a MALFORMED record must not carry an instruction: the "
                "supervisor is never asked about bytes it cannot describe"
            )
        if self.status is SupervisorStatus.REJECTED and (
            not self.decided_by.strip() or not self.decision_reason.strip()
        ):
            raise ValueError(
                "status REJECTED requires an explicit, audited human decision "
                "(decided_by and decision_reason)"
            )
        if self.status is SupervisorStatus.WAIVED and not self.decided_by.strip():
            raise ValueError(
                "status WAIVED requires an explicit human decision (decided_by)"
            )
        if self.status is SupervisorStatus.SENT and self.sent_at is None:
            raise ValueError("status SENT requires sent_at")
        if (
            self.status is SupervisorStatus.ESCALATED
            and self.escalated_at is None
        ):
            raise ValueError("status ESCALATED requires escalated_at")

    @property
    def malformed(self) -> bool:
        """Whether this record tracks bytes the worker contract refused."""
        return self.status is SupervisorStatus.MALFORMED

    @property
    def blocks(self) -> bool:
        """Whether this record still blocks the authoritative review."""
        return self.status not in SUPERVISION_ALLOWING_STATUSES

    @property
    def has_directive(self) -> bool:
        """Whether this record carries a directive worth delivering."""
        return bool(self.instruction_for_cline.strip())
