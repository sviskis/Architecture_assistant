"""Reporting projection - the single read-only view of the project state.

One place, one meaning
----------------------
Every read-only consumer - the Excel export, the project monitor and the
markdown export - renders **this** projection. None of them queries SQLite, a
repository or the source of truth on its own::

    SQLite
      -> repositories / ports
        -> ReportBuilder
          -> ReportSnapshot
            -> Excel adapter
            -> Monitor
            -> Markdown adapter

That is the whole point of the module: without it every consumer would
re-implement its own read access and the three views would drift apart.

What this is, and what it is not
--------------------------------
:class:`ReportSnapshot` is **derived application state**. It is ephemeral and
read-only: it is never persisted, never written back and never becomes a second
source of truth. Authoritative state stays in the domain models behind the
repository ports, and this module only *reads* it.

Read-only by construction
-------------------------
:class:`ReportBuilder` is handed the individual repository **ports** (never
``StoragePort``, which also carries ``transaction()`` and every write accessor)
plus two pure read seams: a ``cost_query`` callable and a ``health`` callable.
Consequently no write method is reachable from this module, and the projection
cannot mutate anything even by accident.

Imports are restricted to ``domain`` and ``ports`` - never ``infrastructure``
or ``sqlite3``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from ..domain.models import (
    ADR,
    ArchitectureChangeRequest,
    ArchitectureVersion,
    Decision,
    Finding,
    Project,
    Risk,
    Step,
    Task,
)
from ..ports.capabilities import CostQuery, CostSummary
from ..ports.repositories import (
    ADRRepository,
    ArchitectureChangeRequestRepository,
    ArchitectureVersionRepository,
    DecisionRepository,
    FindingRepository,
    ProjectRepository,
    RiskRepository,
    StepRepository,
    TaskRepository,
)
from .scheduler import LoopHealth

__all__ = [
    "REPORT_SCHEMA_VERSION",
    "ReportingError",
    "ProjectMissingError",
    "ReportingInvariantError",
    "ReportSnapshot",
    "ReportBuilder",
]

#: Version of the payload produced by :class:`ReportBuilder`.
REPORT_SCHEMA_VERSION = "1.0"


class ReportingError(Exception):
    """Base class for reporting-projection errors."""


class ProjectMissingError(ReportingError):
    """Raised when the source of truth contains no project."""


class ReportingInvariantError(ReportingError):
    """Raised when a singleton invariant of the source of truth is violated."""


@dataclass(frozen=True)
class ReportSnapshot:
    """Derived, read-only projection of the whole project.

    Ephemeral by design: it is never persisted and never becomes a source of
    truth. Every collection is ordered deterministically by the builder, so the
    same state always produces the same snapshot (and therefore the same
    rendering).
    """

    project: Project
    steps: tuple[Step, ...]
    tasks: tuple[Task, ...]
    architecture: ArchitectureVersion | None
    architecture_versions: tuple[ArchitectureVersion, ...]
    adrs: tuple[ADR, ...]
    risks: tuple[Risk, ...]
    findings: tuple[Finding, ...]
    decisions: tuple[Decision, ...]
    #: The persisted Architecture Change Requests - the evolution trail. Read
    #: here so every read-only consumer sees the ACR state through *this*
    #: projection instead of opening an access of its own.
    change_requests: tuple[ArchitectureChangeRequest, ...]
    cost: CostSummary
    cost_by_step: tuple[tuple[int, CostSummary], ...]
    health: LoopHealth
    generated_at: datetime
    schema_version: str = REPORT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation of the projection."""
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at.isoformat(),
            "project": self.project.to_dict(),
            "steps": [step.to_dict() for step in self.steps],
            "tasks": [task.to_dict() for task in self.tasks],
            "architecture": (
                None
                if self.architecture is None
                else self.architecture.to_dict()
            ),
            "architecture_versions": [
                version.to_dict() for version in self.architecture_versions
            ],
            "adrs": [adr.to_dict() for adr in self.adrs],
            "risks": [risk.to_dict() for risk in self.risks],
            "findings": [finding.to_dict() for finding in self.findings],
            "decisions": [decision.to_dict() for decision in self.decisions],
            "change_requests": [
                request.to_dict() for request in self.change_requests
            ],
            "cost": self.cost.to_dict(),
            "cost_by_step": [
                {"step_no": step_no, "cost": summary.to_dict()}
                for step_no, summary in self.cost_by_step
            ],
            "health": self.health.to_dict(),
        }


def _task_order(task: Task) -> tuple[int, int]:
    """Deterministic task ordering: by step, then by attempt."""
    return (task.step_no, task.attempt)


def _current_version(
    versions: tuple[ArchitectureVersion, ...],
) -> ArchitectureVersion | None:
    """The single ``is_current`` baseline, or ``None`` before one exists."""
    current = tuple(version for version in versions if version.is_current)
    if len(current) > 1:
        found = ", ".join(sorted(version.version for version in current))
        raise ReportingInvariantError(
            "expected at most one current architecture version, found "
            f"{len(current)}: {found}"
        )
    return current[0] if current else None


class ReportBuilder:
    """The single read-only projection of the project state.

    Depends only on the repository ports and two pure read seams, so it stays
    decoupled from SQLite and is testable with fakes. It is never handed
    ``StoragePort`` or ``TransactionPort``: no write path is reachable from here.
    The clock is mandatory - this class never reads the system time itself.
    """

    def __init__(
        self,
        projects: ProjectRepository,
        steps: StepRepository,
        tasks: TaskRepository,
        architecture_versions: ArchitectureVersionRepository,
        adrs: ADRRepository,
        risks: RiskRepository,
        findings: FindingRepository,
        decisions: DecisionRepository,
        change_requests: ArchitectureChangeRequestRepository,
        *,
        cost_query: Callable[[CostQuery], CostSummary],
        health: Callable[[], LoopHealth],
        clock: Callable[[], datetime],
    ) -> None:
        for name, seam in (
            ("cost_query", cost_query),
            ("health", health),
            ("clock", clock),
        ):
            if not callable(seam):
                raise ValueError(f"{name} must be a callable")
        self._projects = projects
        self._steps = steps
        self._tasks = tasks
        self._architecture_versions = architecture_versions
        self._adrs = adrs
        self._risks = risks
        self._findings = findings
        self._decisions = decisions
        self._change_requests = change_requests
        self._cost_query = cost_query
        self._health = health
        self._clock = clock

    def build(self) -> ReportSnapshot:
        """Assemble the projection for the whole project - reads only.

        Every collection is loaded through a read method and sorted
        deterministically, so identical state always yields an identical
        snapshot. Cost is aggregated through the injected ``cost_query`` seam:
        one unfiltered summary plus one summary per step.
        """
        project = self._require_single_project()
        steps = tuple(sorted(self._steps.list(), key=lambda step: step.step_no))
        versions = tuple(
            sorted(
                self._architecture_versions.list(),
                key=lambda version: version.version,
            )
        )
        return ReportSnapshot(
            project=project,
            steps=steps,
            tasks=tuple(sorted(self._tasks.list(), key=_task_order)),
            architecture=_current_version(versions),
            architecture_versions=versions,
            adrs=tuple(sorted(self._adrs.list(), key=lambda adr: adr.id)),
            risks=tuple(sorted(self._risks.list(), key=lambda risk: risk.id)),
            findings=tuple(
                sorted(self._findings.list(), key=lambda finding: finding.id)
            ),
            decisions=tuple(
                sorted(self._decisions.list(), key=lambda decision: decision.id)
            ),
            change_requests=tuple(
                sorted(
                    self._change_requests.list(),
                    key=lambda request: request.request_id,
                )
            ),
            cost=self._cost_query(CostQuery()),
            cost_by_step=tuple(
                (step.step_no, self._cost_query(CostQuery(step_no=step.step_no)))
                for step in steps
            ),
            health=self._health(),
            generated_at=self._clock(),
            schema_version=REPORT_SCHEMA_VERSION,
        )

    # -- selection rules (mirrors the context builder's invariants) -------
    def _require_single_project(self) -> Project:
        projects = self._projects.list()
        if not projects:
            raise ProjectMissingError("the source of truth contains no project")
        if len(projects) > 1:
            names = ", ".join(sorted(project.name for project in projects))
            raise ReportingInvariantError(
                f"expected exactly one project, found {len(projects)}: {names}"
            )
        return projects[0]
