"""Plan Loader - the controlled initial plan import.

One question, one answer: *given the text of a plan file, may the assistant load
it as its initial plan?* The use-case is deliberately narrow.

* **Initial load only.** A database that already holds steps is never merged,
  replaced, renumbered or reset - the import is refused. Plan replacement and
  plan merging are out of scope.
* **Validate, never repair.** The text is parsed once by :func:`parse_plan`, which
  reports *every* problem it found (``PlanFormatError.issues``) and writes
  nothing. A plan that names a workflow state, an attempt, a timestamp or any
  other unknown field is refused instead of interpreted.
* **The plan cannot touch the project.** The plan's ``project`` block is
  descriptive metadata: it must *match* the authoritative singleton ``Project``
  row and it is never written - not the name, not the plan version, not the mode.
  Provenance lives in exactly one audit entry (``AuditEntityType.PLAN`` /
  ``AuditAction.IMPORT``) written in the same transaction as the steps.
* **One canonical initial state.** Every imported step is born in
  ``StepStateMachine.INITIAL_STATE`` with ``attempt=0``, so the loader can never
  fabricate workflow progress.

Imports are restricted to ``domain`` and ``ports`` - never ``infrastructure`` or
``sqlite3``. The clock is injected; this module never reads system time itself.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Optional, Sequence

from ..domain.audit import AuditAction, AuditEntityType, AuditEntry
from ..domain.enums import Mode, Phase, RiskLevel
from ..domain.fsm import StepStateMachine
from ..domain.models import Project, Step
from ..ports.repositories import (
    AuditRepository,
    ProjectRepository,
    StepRepository,
)
from ..ports.transactions import TransactionPort

__all__ = [
    "PLAN_SCHEMA_VERSION",
    "MAX_PLAN_STEPS",
    "MAX_PLAN_CHARS",
    "MAX_ISSUES",
    "PlanError",
    "PlanFormatError",
    "PlanConflictError",
    "PlanProjectMismatchError",
    "PlanVersionMismatchError",
    "PlanInvariantError",
    "PlanActorRequiredError",
    "PlanReasonRequiredError",
    "PlanIssue",
    "PlanStepSpec",
    "Plan",
    "PlanPreview",
    "PlanImportResult",
    "PlanLoader",
    "parse_plan",
]

#: The plan-file schema this loader understands. v1.1.1 supports exactly one
#: version and carries no migration machinery for plan files: an unknown version
#: is a validation error, never a guess.
PLAN_SCHEMA_VERSION = "1.0"

#: Fail-closed guards against the wrong file being picked - not workflow limits.
MAX_PLAN_STEPS = 500
MAX_PLAN_CHARS = 1_000_000

#: How many validation problems one report carries before it is truncated
#: (the count of the remaining problems is still reported).
MAX_ISSUES = 50

#: The exact key sets the schema allows. Anything else is refused, so a plan
#: cannot smuggle a state, an attempt or a timestamp into the source of truth.
_TOP_LEVEL_KEYS = frozenset({"schema_version", "plan_version", "project", "steps"})
_PROJECT_KEYS = frozenset({"name", "mode"})
_STEP_KEYS = frozenset(
    {
        "step_no",
        "phase",
        "title",
        "risk",
        "max_attempts",
        "requires_human",
        "description",
    }
)


class PlanError(Exception):
    """Base class for every plan-loader failure."""


class PlanFormatError(PlanError):
    """Raised when the plan text is not a valid plan file.

    Carries :attr:`issues` - **every** problem the parser found - so a UI can show
    them all together instead of one at a time.
    """

    def __init__(self, issues: Sequence["PlanIssue"]) -> None:
        self.issues = tuple(issues)
        summary = "; ".join(
            f"{issue.path}: {issue.message}" for issue in self.issues[:3]
        )
        remaining = len(self.issues) - 3
        if remaining > 0:
            summary += f"; and {remaining} more"
        super().__init__(
            f"plan is invalid ({len(self.issues)} problem(s)): {summary}"
        )


class PlanConflictError(PlanError):
    """Raised when the database already holds steps - the initial-load-only rule."""


class PlanProjectMismatchError(PlanError):
    """Raised when the plan names a different project than the database."""


class PlanVersionMismatchError(PlanError):
    """Raised when the plan declares a different plan version than the database."""


class PlanInvariantError(PlanError):
    """Raised when the source of truth violates the single-project invariant."""


class PlanActorRequiredError(PlanError):
    """Raised when no actor was given for the import."""


class PlanReasonRequiredError(PlanError):
    """Raised when no reason was given for the import."""


def _render(value: Any) -> str:
    """A short, safe rendering of an offending value for a report."""
    text = repr(value)
    return text if len(text) <= 80 else f"{text[:77]}..."


def _issue(path: str, problem: str, value: Any, message: str) -> "PlanIssue":
    """One problem, addressed by its path in the document."""
    return PlanIssue(
        path=path, problem=problem, value=_render(value), message=message
    )


@dataclass(frozen=True)
class PlanIssue:
    """One validation problem, addressed by path.

    ``value`` is the offending value rendered safely (never raw user data in a
    message), and ``message`` is the human-readable sentence a UI shows.
    """

    path: str
    problem: str
    value: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "path": self.path,
            "problem": self.problem,
            "value": self.value,
            "message": self.message,
        }


@dataclass(frozen=True)
class PlanStepSpec:
    """One validated plan step - the plan file's own shape, never a domain object."""

    step_no: int
    phase: Phase
    title: str
    risk: RiskLevel = RiskLevel.LOW
    max_attempts: int = 3
    requires_human: bool = False
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "step_no": self.step_no,
            "phase": self.phase.value,
            "title": self.title,
            "risk": self.risk.value,
            "max_attempts": self.max_attempts,
            "requires_human": self.requires_human,
            "description": self.description,
        }


@dataclass(frozen=True)
class Plan:
    """A parsed, validated plan file.

    ``plan_version`` is empty when the file omits it - the loader then means the
    database project's own plan version rather than inventing a second value.
    """

    schema_version: str
    project_name: str
    plan_hash: str
    steps: tuple[PlanStepSpec, ...]
    mode: Optional[Mode] = None
    plan_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "schema_version": self.schema_version,
            "project_name": self.project_name,
            "mode": None if self.mode is None else self.mode.value,
            "plan_version": self.plan_version,
            "plan_hash": self.plan_hash,
            "steps": [step.to_dict() for step in self.steps],
        }


@dataclass(frozen=True)
class PlanPreview:
    """What an import of this text would do - and what stands in the way.

    Produced by :meth:`PlanLoader.preview`, which reads only: a preview never
    writes, and an import that would be refused is *reported* through
    ``blocked_reason`` instead of raised, so the operator sees why before
    pressing anything. ``importable`` is the single answer a UI needs.
    """

    valid: bool
    importable: bool
    blocked_reason: str
    db_project_name: str
    db_plan_version: str
    db_mode: str
    db_step_count: int
    project_matches: bool
    plan_version_matches: bool
    mode_matches: bool
    project_name: str = ""
    mode: str = ""
    plan_version: str = ""
    plan_version_declared: bool = False
    plan_hash: str = ""
    step_count: int = 0
    first_step_no: Optional[int] = None
    last_step_no: Optional[int] = None
    risk_counts: tuple[tuple[str, int], ...] = ()
    requires_human_count: int = 0
    phases: tuple[str, ...] = ()
    issues: tuple[PlanIssue, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation (what the GUI receives)."""
        return {
            "valid": self.valid,
            "importable": self.importable,
            "blocked_reason": self.blocked_reason,
            "issues": [issue.to_dict() for issue in self.issues],
            "project_name": self.project_name,
            "mode": self.mode,
            "plan_version": self.plan_version,
            "plan_version_declared": self.plan_version_declared,
            "plan_hash": self.plan_hash,
            "step_count": self.step_count,
            "first_step_no": self.first_step_no,
            "last_step_no": self.last_step_no,
            "risk_counts": {level: count for level, count in self.risk_counts},
            "requires_human_count": self.requires_human_count,
            "phases": list(self.phases),
            "db_project_name": self.db_project_name,
            "db_plan_version": self.db_plan_version,
            "db_mode": self.db_mode,
            "db_step_count": self.db_step_count,
            "project_matches": self.project_matches,
            "plan_version_matches": self.plan_version_matches,
            "mode_matches": self.mode_matches,
        }


@dataclass(frozen=True)
class PlanImportResult:
    """The outcome of one successful import - plain, JSON-safe data."""

    project_name: str
    plan_version: str
    plan_hash: str
    step_count: int
    first_step_no: int
    last_step_no: int
    imported_at: datetime

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation (what the GUI receives)."""
        return {
            "action": "import_plan",
            "project_name": self.project_name,
            "plan_version": self.plan_version,
            "plan_hash": self.plan_hash,
            "step_count": self.step_count,
            "first_step_no": self.first_step_no,
            "last_step_no": self.last_step_no,
            "imported_at": self.imported_at.isoformat(),
        }


# ---------------------------------------------------------------------------
# validation: one pure function, every problem collected
# ---------------------------------------------------------------------------


def parse_plan(text: Any) -> Plan:
    """Parse and validate one plan file - pure, deterministic and write-free.

    Every problem is collected (up to :data:`MAX_ISSUES`) and raised together as
    :class:`PlanFormatError`. A JSON syntax error short-circuits, because nothing
    else can be validated without a document; anything else is validated
    field-by-field and cross-checked at the end.
    """
    if not isinstance(text, str):
        raise PlanFormatError(
            [
                _issue(
                    "<plan>",
                    "must be a str",
                    text,
                    "the plan text must be a string",
                )
            ]
        )
    if len(text) > MAX_PLAN_CHARS:
        raise PlanFormatError(
            [
                _issue(
                    "<file>",
                    f"must be at most {MAX_PLAN_CHARS} characters",
                    len(text),
                    f"the plan file is too large ({len(text)} characters)",
                )
            ]
        )
    try:
        document = json.loads(text)
    except ValueError as error:
        raise PlanFormatError(
            [
                _issue(
                    "<file>",
                    "must be valid JSON",
                    str(error),
                    f"the plan file is not valid JSON: {error}",
                )
            ]
        ) from error
    if not isinstance(document, Mapping):
        raise PlanFormatError(
            [
                _issue(
                    "<root>",
                    "must be a JSON object",
                    document,
                    "the plan file must contain one JSON object",
                )
            ]
        )

    issues: list[PlanIssue] = []
    _unknown_keys(document, "{root}", _TOP_LEVEL_KEYS, issues)
    schema_version = _schema_version(document, issues)
    project_name, mode = _project(document.get("project"), issues)
    plan_version = _optional_text(document, "plan_version", "", issues)
    steps = _steps(document.get("steps"), issues)
    if issues:
        raise PlanFormatError(_capped(issues))
    return Plan(
        schema_version=schema_version,
        project_name=project_name,
        mode=mode,
        plan_version=plan_version,
        plan_hash=_hash(text),
        steps=tuple(steps),
    )


def _hash(text: str) -> str:
    """The plan-file identity: sha256 of the text exactly as it was read."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _capped(issues: Sequence[PlanIssue]) -> tuple[PlanIssue, ...]:
    """At most :data:`MAX_ISSUES` problems, then an honest truncation marker."""
    if len(issues) <= MAX_ISSUES:
        return tuple(issues)
    kept = list(issues[:MAX_ISSUES])
    kept.append(
        _issue(
            "<report>",
            "truncated",
            len(issues),
            f"{len(issues) - MAX_ISSUES} further problem(s) were not listed",
        )
    )
    return tuple(kept)


def _unknown_keys(
    value: Mapping[str, Any],
    path: str,
    allowed: frozenset,
    issues: list[PlanIssue],
) -> None:
    """Refuse every key the schema does not define."""
    for key in sorted(value):
        if key not in allowed:
            issues.append(
                _issue(
                    f"{path}.{key}",
                    "must not be present",
                    value[key],
                    f"unknown field {key!r} is refused; a plan cannot set it",
                )
            )


def _enum_or_issue(
    value: Any, enum_cls: type, path: str, issues: list[PlanIssue]
) -> Any:
    """An enum member, or one issue naming every allowed value."""
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value)
        except ValueError:
            pass
    allowed = ", ".join(member.value for member in enum_cls)
    issues.append(
        _issue(
            path,
            f"must be one of ({allowed})",
            value,
            f"{path} must be one of ({allowed})",
        )
    )
    return None


def _schema_version(
    document: Mapping[str, Any], issues: list[PlanIssue]
) -> str:
    """The required ``schema_version``, which must name this one schema."""
    value = document.get("schema_version")
    if not isinstance(value, str) or value != PLAN_SCHEMA_VERSION:
        if value is None:
            issues.append(
                _issue(
                    "schema_version",
                    "is required",
                    None,
                    f"schema_version is required and must be "
                    f"{PLAN_SCHEMA_VERSION!r}",
                )
            )
        else:
            issues.append(
                _issue(
                    "schema_version",
                    f"must be {PLAN_SCHEMA_VERSION!r}",
                    value,
                    "unsupported plan schema version; this loader understands "
                    f"{PLAN_SCHEMA_VERSION!r} only",
                )
            )
        return ""
    return value


def _optional_text(
    document: Mapping[str, Any],
    key: str,
    default: str,
    issues: list[PlanIssue],
) -> str:
    """An optional non-empty string field."""
    value = document.get(key)
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        issues.append(
            _issue(
                key,
                "must be a non-empty string",
                value,
                f"{key} must be a non-empty string when present",
            )
        )
        return default
    return value.strip()


def _project(value: Any, issues: list[PlanIssue]) -> tuple[str, Optional[Mode]]:
    """The descriptive ``project`` block: a name (required) and a mode (optional)."""
    if value is None:
        issues.append(
            _issue(
                "project",
                "is required",
                None,
                "the plan must name the project it belongs to",
            )
        )
        return "", None
    if not isinstance(value, Mapping):
        issues.append(
            _issue(
                "project",
                "must be an object",
                value,
                "project must be a JSON object with a name",
            )
        )
        return "", None
    _unknown_keys(value, "project", _PROJECT_KEYS, issues)
    raw_name = value.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        issues.append(
            _issue(
                "project.name",
                "must be a non-empty string",
                raw_name,
                "project.name must be a non-empty string",
            )
        )
        name = ""
    else:
        name = raw_name.strip()
    raw_mode = value.get("mode")
    mode: Optional[Mode] = None
    if raw_mode is not None:
        mode = _enum_or_issue(raw_mode, Mode, "project.mode", issues)
    return name, mode


def _steps(value: Any, issues: list[PlanIssue]) -> list[PlanStepSpec]:
    """The required, non-empty ``steps`` list, validated entry by entry."""
    if value is None:
        issues.append(
            _issue(
                "steps",
                "is required",
                None,
                "the plan must contain a non-empty steps list",
            )
        )
        return []
    if not isinstance(value, list):
        issues.append(
            _issue("steps", "must be a list", value, "steps must be a JSON array")
        )
        return []
    if not value:
        issues.append(
            _issue(
                "steps",
                "must not be empty",
                [],
                "the plan must contain at least one step",
            )
        )
        return []
    if len(value) > MAX_PLAN_STEPS:
        issues.append(
            _issue(
                "steps",
                f"must hold at most {MAX_PLAN_STEPS} steps",
                len(value),
                f"the plan holds {len(value)} steps; at most "
                f"{MAX_PLAN_STEPS} are accepted",
            )
        )
        return []
    specs: list[PlanStepSpec] = []
    for index, raw in enumerate(value):
        spec = _step(raw, index, issues)
        if spec is not None:
            specs.append(spec)
    _check_step_numbers(specs, issues)
    return specs


def _step(
    raw: Any, index: int, issues: list[PlanIssue]
) -> Optional[PlanStepSpec]:
    """One ``steps[i]`` entry - or ``None`` when a required field is unusable."""
    path = f"steps[{index}]"
    if not isinstance(raw, Mapping):
        issues.append(
            _issue(path, "must be an object", raw, f"{path} must be a JSON object")
        )
        return None
    _unknown_keys(raw, path, _STEP_KEYS, issues)

    raw_step_no = raw.get("step_no")
    if (
        isinstance(raw_step_no, bool)
        or not isinstance(raw_step_no, int)
        or raw_step_no < 1
    ):
        issues.append(
            _issue(
                f"{path}.step_no",
                "must be an integer >= 1",
                raw_step_no,
                f"{path}.step_no must be an integer >= 1",
            )
        )
        step_no: Optional[int] = None
    else:
        step_no = raw_step_no

    raw_phase = raw.get("phase")
    if raw_phase is None:
        allowed = ", ".join(member.value for member in Phase)
        issues.append(
            _issue(
                f"{path}.phase",
                f"must be one of ({allowed})",
                None,
                f"{path}.phase is required",
            )
        )
        phase: Optional[Phase] = None
    else:
        phase = _enum_or_issue(raw_phase, Phase, f"{path}.phase", issues)

    raw_title = raw.get("title")
    if not isinstance(raw_title, str) or not raw_title.strip():
        issues.append(
            _issue(
                f"{path}.title",
                "must be a non-empty string",
                raw_title,
                f"{path}.title must be a non-empty string",
            )
        )
        title: Optional[str] = None
    else:
        title = raw_title.strip()

    risk = RiskLevel.LOW
    if raw.get("risk") is not None:
        risk = _enum_or_issue(raw["risk"], RiskLevel, f"{path}.risk", issues) or (
            RiskLevel.LOW
        )

    raw_max = raw.get("max_attempts", 3)
    if isinstance(raw_max, bool) or not isinstance(raw_max, int) or raw_max < 1:
        issues.append(
            _issue(
                f"{path}.max_attempts",
                "must be an integer >= 1",
                raw_max,
                f"{path}.max_attempts must be an integer >= 1",
            )
        )
        max_attempts = 3
    else:
        max_attempts = raw_max

    raw_human = raw.get("requires_human", False)
    if not isinstance(raw_human, bool):
        issues.append(
            _issue(
                f"{path}.requires_human",
                "must be a boolean",
                raw_human,
                f"{path}.requires_human must be true or false",
            )
        )
        requires_human = False
    else:
        requires_human = raw_human

    raw_description = raw.get("description", "")
    if not isinstance(raw_description, str):
        issues.append(
            _issue(
                f"{path}.description",
                "must be a string",
                raw_description,
                f"{path}.description must be a string",
            )
        )
        description = ""
    else:
        description = raw_description

    if step_no is None or phase is None or title is None:
        return None
    return PlanStepSpec(
        step_no=step_no,
        phase=phase,
        title=title,
        risk=risk,
        max_attempts=max_attempts,
        requires_human=requires_human,
        description=description,
    )


def _check_step_numbers(
    specs: Sequence[PlanStepSpec], issues: list[PlanIssue]
) -> None:
    """Step numbers must be unique and - by policy - contiguous ``1..N``.

    Contiguity is a *Plan Loader* policy, not a requirement of the assistant: the
    orchestrator derives the current step as the lowest-numbered step that is not
    ``VERIFIED`` and reports the next one as the following higher step number, so
    the core itself would cope with gaps. A gap is refused here because "next
    step" should mean the next number to a reader, and a plan that is not ``1..N``
    is far more often a numbering mistake than a deliberate omission.
    """
    counts: dict[int, int] = {}
    for spec in specs:
        counts[spec.step_no] = counts.get(spec.step_no, 0) + 1
    for number in sorted(number for number, count in counts.items() if count > 1):
        issues.append(
            _issue(
                f"steps[step_no={number}]",
                "must be unique",
                number,
                f"step_no {number} appears {counts[number]} times",
            )
        )
    numbers = sorted(counts)
    if not numbers:
        return
    missing = sorted(set(range(1, len(numbers) + 1)) - set(numbers))
    outside = sorted(number for number in numbers if number > len(numbers))
    if missing:
        issues.append(
            _issue(
                "steps",
                "must be numbered contiguously from 1",
                missing,
                f"step numbers are missing: {missing}",
            )
        )
    if outside:
        issues.append(
            _issue(
                "steps",
                "must be numbered contiguously from 1",
                outside,
                f"step numbers are outside 1..{len(numbers)}: {outside}",
            )
        )


def _phases(specs: Sequence[PlanStepSpec]) -> list[str]:
    """The plan's phases, in first-appearance order and without repetition."""
    ordered: dict[str, None] = {}
    for spec in specs:
        ordered.setdefault(spec.phase.value, None)
    return list(ordered)


# ---------------------------------------------------------------------------
# the use-case
# ---------------------------------------------------------------------------


def _preview(
    plan: Optional[Plan],
    issues: Sequence[PlanIssue],
    project: Project,
    *,
    existing: int,
    plan_hash: str,
) -> PlanPreview:
    """Assemble a preview from a parsed plan (or its issues) and the database.

    A refused import is *described* here rather than raised: the operator has to
    see why the Confirm action is unavailable before pressing it, and the reason
    is a property of the data, not an error of the session.
    """
    declared_version = "" if plan is None else plan.plan_version
    effective_version = declared_version or project.plan_version
    project_name = "" if plan is None else plan.project_name
    mode = None if plan is None else plan.mode
    project_matches = project_name == project.name
    version_matches = not declared_version or declared_version == project.plan_version
    mode_matches = mode is None or mode is project.mode
    specs: tuple[PlanStepSpec, ...] = () if plan is None else plan.steps
    numbers = sorted(spec.step_no for spec in specs)
    blocked = ""
    if issues:
        blocked = "the plan file is invalid"
    elif existing:
        blocked = (
            f"the database already holds {existing} step(s); v1.1.1 loads an "
            "initial plan only"
        )
    elif plan is not None and not project_matches:
        blocked = (
            f"the plan names project {project_name!r} but this database holds "
            f"{project.name!r}"
        )
    elif plan is not None and not version_matches:
        blocked = (
            f"the plan declares plan version {declared_version!r} but this "
            f"database holds {project.plan_version!r}"
        )
    return PlanPreview(
        valid=plan is not None,
        importable=plan is not None and not blocked,
        blocked_reason=blocked,
        db_project_name=project.name,
        db_plan_version=project.plan_version,
        db_mode=project.mode.value,
        db_step_count=existing,
        project_matches=project_matches,
        plan_version_matches=version_matches,
        mode_matches=mode_matches,
        project_name=project_name,
        mode="" if mode is None else mode.value,
        plan_version=effective_version,
        plan_version_declared=bool(declared_version),
        plan_hash=plan_hash,
        step_count=len(specs),
        first_step_no=numbers[0] if numbers else None,
        last_step_no=numbers[-1] if numbers else None,
        risk_counts=tuple(
            (level.value, sum(1 for spec in specs if spec.risk is level))
            for level in RiskLevel
        ),
        requires_human_count=sum(1 for spec in specs if spec.requires_human),
        phases=tuple(_phases(specs)),
        issues=tuple(issues),
    )


class PlanLoader:
    """The controlled initial-plan import.

    Holds exactly five collaborators - three repository ports, the shared
    transaction boundary and an injected clock - so it can read the project and
    the steps, write steps plus exactly one audit entry, and nothing else. It
    receives no monitor, no reporting, no worker and no architecture port, and it
    never opens a connection, imports an adapter or reads a file.
    """

    def __init__(
        self,
        projects: ProjectRepository,
        steps: StepRepository,
        audit: AuditRepository,
        transactions: TransactionPort,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        if not callable(clock):
            raise ValueError("clock must be a callable returning a datetime")
        self._projects = projects
        self._steps = steps
        self._audit = audit
        self._transactions = transactions
        self._clock = clock

    # -- read-only preview -------------------------------------------------
    def preview(self, text: Any) -> PlanPreview:
        """What an import of ``text`` would do - reads only, never writes.

        Reads the authoritative project and the current step count, then reports
        the outcome as data (``importable`` / ``blocked_reason`` / ``issues``).
        The single-project invariant is the one condition that is raised rather
        than described: if it is broken, no plan question can be answered at all.
        """
        project = self._single_project()
        plan_hash = _hash(text) if isinstance(text, str) else ""
        existing = len(self._steps.list())
        try:
            plan = parse_plan(text)
        except PlanFormatError as error:
            return _preview(
                None,
                error.issues,
                project,
                existing=existing,
                plan_hash=plan_hash,
            )
        return _preview(plan, (), project, existing=existing, plan_hash=plan_hash)

    # -- the import --------------------------------------------------------
    def import_plan(
        self,
        text: Any,
        *,
        actor: str,
        reason: str,
        source_file: str = "",
    ) -> PlanImportResult:
        """Load ``text`` as the project's initial plan - all or nothing.

        The text is re-parsed here and never trusted from an earlier preview, the
        actor and reason are required before anything is read, and the whole write
        - every step plus exactly one audit entry - shares one transaction, so a
        failure anywhere leaves the source of truth exactly as it was.

        ``source_file`` is recorded as the caller's own provenance: the core never
        reads a file, and the plan hash - not the path - is the strong identity.
        """
        _require_context(actor, reason)
        plan = parse_plan(text)
        now = self._clock()
        with self._transactions.transaction():
            project = self._single_project()
            if project.name != plan.project_name:
                raise PlanProjectMismatchError(
                    f"the plan names project {plan.project_name!r} but this "
                    f"database holds {project.name!r}"
                )
            if plan.plan_version and plan.plan_version != project.plan_version:
                raise PlanVersionMismatchError(
                    f"the plan declares plan version {plan.plan_version!r} but "
                    f"this database holds {project.plan_version!r}"
                )
            existing = self._steps.list()
            if existing:
                raise PlanConflictError(
                    "plan already loaded: this database holds "
                    f"{len(existing)} step(s)"
                )
            for spec in plan.steps:
                self._steps.upsert(_step_from_spec(spec, now))
            self._audit.append(
                AuditEntry(
                    entity_type=AuditEntityType.PLAN,
                    entity_id=plan.plan_hash,
                    action=AuditAction.IMPORT,
                    detail={
                        "operation": "load-plan",
                        "project_name": project.name,
                        "actor": actor,
                        "reason": reason,
                        "source_file": str(source_file),
                        "plan_version": plan.plan_version or project.plan_version,
                        "schema_version": plan.schema_version,
                        "plan_hash": plan.plan_hash,
                        "step_count": len(plan.steps),
                        "first_step_no": plan.steps[0].step_no,
                        "last_step_no": plan.steps[-1].step_no,
                        "phases": _phases(plan.steps),
                    },
                    created_at=now,
                )
            )
        return PlanImportResult(
            project_name=project.name,
            plan_version=plan.plan_version or project.plan_version,
            plan_hash=plan.plan_hash,
            step_count=len(plan.steps),
            first_step_no=plan.steps[0].step_no,
            last_step_no=plan.steps[-1].step_no,
            imported_at=now,
        )

    # -- source of truth ---------------------------------------------------
    def _single_project(self) -> Project:
        """The one authoritative project - or a fail-closed invariant error."""
        projects = self._projects.list()
        if not projects:
            raise PlanInvariantError("the source of truth contains no project")
        if len(projects) > 1:
            names = ", ".join(sorted(project.name for project in projects))
            raise PlanInvariantError(
                f"expected exactly one project, found {len(projects)}: {names}"
            )
        return projects[0]


def _require_context(actor: Any, reason: Any) -> None:
    """Fail closed unless the import names both an actor and a reason.

    Called before any read and any write, so an anonymous or unexplained import
    cannot even reach the source of truth. This mirrors the human-write
    convention of the override and approval use-cases.
    """
    if not isinstance(actor, str) or not actor.strip():
        raise PlanActorRequiredError("a plan import requires a non-empty actor")
    if not isinstance(reason, str) or not reason.strip():
        raise PlanReasonRequiredError("a plan import requires a non-empty reason")


def _step_from_spec(spec: PlanStepSpec, now: datetime) -> Step:
    """The one canonical domain Step of an imported plan step.

    The state comes from the authoritative FSM's initial state - never from the
    plan file and never from a literal - the attempt is 0, and the four lifecycle
    timestamps stay empty: an imported step has no workflow history to claim.
    """
    return Step(
        step_no=spec.step_no,
        phase=spec.phase,
        title=spec.title,
        description=spec.description,
        state=StepStateMachine.INITIAL_STATE,
        attempt=0,
        max_attempts=spec.max_attempts,
        risk=spec.risk,
        requires_human=spec.requires_human,
        created_at=now,
    )
