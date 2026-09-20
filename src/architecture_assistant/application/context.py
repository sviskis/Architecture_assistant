"""Context Manager - assembles the derived, read-only context for a step.

Design rules
------------
* **Authoritative** state lives in the domain models persisted behind the
  repository ports. The context builder only *reads* it.
* The produced :class:`ContextSnapshot` is **derived**: ephemeral, read-only and
  never persisted. It is not a source of truth and is never written back.
* Selection is deterministic and rule based. It never dumps chat history and
  never concatenates previous reports.
* Only targeted reads are used (``list_by_state`` for steps, ``list_open`` for
  risks). ``tasks``, ``findings`` and ``decisions`` are never queried.
* The clock is **injected**; this module never reads the system time itself.

Imports are restricted to ``domain`` and ``ports`` - never ``infrastructure``
or ``sqlite3``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional

from ..domain.enums import ADRStatus, StepState
from ..domain.models import ADR, ArchitectureVersion, Project, Risk, Step
from ..ports.repositories import (
    ADRRepository,
    ArchitectureVersionRepository,
    ProjectRepository,
    RiskRepository,
    StepRepository,
)

__all__ = [
    "SCHEMA_VERSION",
    "ContextError",
    "StepNotFoundError",
    "ProjectNotFoundError",
    "ContextInvariantError",
    "ContextSnapshot",
    "ContextBuilder",
]

#: Version of the snapshot payload produced by :class:`ContextBuilder`.
SCHEMA_VERSION = "1.0"


class ContextError(Exception):
    """Base class for context-manager errors."""


class StepNotFoundError(ContextError):
    """Raised when the requested step does not exist."""


class ProjectNotFoundError(ContextError):
    """Raised when the source of truth contains no project."""


class ContextInvariantError(ContextError):
    """Raised when a singleton invariant of the source of truth is violated."""


@dataclass(frozen=True)
class ContextSnapshot:
    """Derived, read-only context for a single step.

    Ephemeral by design: it is never persisted and never becomes a source of
    truth. All collections are ordered deterministically by the builder.
    """

    project: Project
    step: Step
    previous_steps: tuple[Step, ...]
    architecture: Optional[ArchitectureVersion]
    adrs: tuple[ADR, ...]
    risks: tuple[Risk, ...]
    generated_at: datetime
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic, JSON-safe representation of the snapshot."""
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at.isoformat(),
            "project": self.project.to_dict(),
            "step": self.step.to_dict(),
            "previous_steps": [step.to_dict() for step in self.previous_steps],
            "architecture": (
                None if self.architecture is None else self.architecture.to_dict()
            ),
            "adrs": [adr.to_dict() for adr in self.adrs],
            "risks": [risk.to_dict() for risk in self.risks],
        }


class ContextBuilder:
    """Builds a derived context snapshot for a step.

    Depends only on repository ports, so it stays decoupled from SQLite and is
    testable with fake ports. The clock is mandatory: this class never reads the
    system time itself.
    """

    def __init__(
        self,
        projects: ProjectRepository,
        steps: StepRepository,
        adrs: ADRRepository,
        risks: RiskRepository,
        architecture_versions: ArchitectureVersionRepository,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        if not callable(clock):
            raise ValueError("clock must be a callable returning a datetime")
        self._projects = projects
        self._steps = steps
        self._adrs = adrs
        self._risks = risks
        self._architecture_versions = architecture_versions
        self._clock = clock

    def build(self, step_no: int) -> ContextSnapshot:
        """Assemble the context snapshot for ``step_no``.

        Performs reads only - nothing is written back to the source of truth.
        """
        step = self._require_step(step_no)
        return ContextSnapshot(
            project=self._require_single_project(),
            step=step,
            previous_steps=self._previous_verified_steps(step),
            architecture=self._current_architecture(),
            adrs=self._accepted_adrs(),
            risks=self._open_risks(),
            generated_at=self._clock(),
            schema_version=SCHEMA_VERSION,
        )

    # -- selection rules -------------------------------------------------
    def _require_step(self, step_no: int) -> Step:
        step = self._steps.get(step_no)
        if step is None:
            raise StepNotFoundError(f"step {step_no} does not exist")
        return step

    def _require_single_project(self) -> Project:
        projects = self._projects.list()
        if not projects:
            raise ProjectNotFoundError("the source of truth contains no project")
        if len(projects) > 1:
            names = ", ".join(sorted(project.name for project in projects))
            raise ContextInvariantError(
                f"expected exactly one project, found {len(projects)}: {names}"
            )
        return projects[0]

    def _previous_verified_steps(self, step: Step) -> tuple[Step, ...]:
        return tuple(
            previous
            for previous in self._steps.list_by_state(StepState.VERIFIED)
            if previous.step_no < step.step_no
        )

    def _current_architecture(self) -> Optional[ArchitectureVersion]:
        current = tuple(
            version
            for version in self._architecture_versions.list()
            if version.is_current
        )
        if len(current) > 1:
            versions = ", ".join(sorted(version.version for version in current))
            raise ContextInvariantError(
                "expected at most one current architecture version, found "
                f"{len(current)}: {versions}"
            )
        if not current:
            # Temporary bootstrap state: no baseline exists until Step 4
            # ("Architecture Baseline + Rules") establishes one.
            return None
        return current[0]

    def _accepted_adrs(self) -> tuple[ADR, ...]:
        return tuple(
            adr for adr in self._adrs.list() if adr.status == ADRStatus.ACCEPTED
        )

    def _open_risks(self) -> tuple[Risk, ...]:
        return tuple(self._risks.list_open())

