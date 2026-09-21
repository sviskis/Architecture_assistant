"""Repository ports - the persistence boundary of the assistant.

Ports are structural :class:`typing.Protocol` contracts, so the domain and
application layers depend only on these interfaces and never on SQLite. Concrete
adapters live in :mod:`architecture_assistant.infrastructure`.

Every port exposes a deterministic, minimal contract: ``upsert``, ``get``,
``list`` and ``delete``. Key types are deliberately per-aggregate (``str``,
``int`` or :class:`TaskKey`) so each adapter has an unambiguous type contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from ..domain.audit import AuditEntityType, AuditEntry
from ..domain.enums import (
    ACRStatus,
    ProposalStatus,
    StepState,
    SupervisorStatus,
)
from ..domain.models import (
    ADR,
    ArchitectureChangeRequest,
    ArchitectureProposal,
    ArchitectureVersion,
    Decision,
    Finding,
    Project,
    Risk,
    Step,
    SupervisionRecord,
    Task,
)

__all__ = [
    "TaskKey",
    "ProjectRepository",
    "StepRepository",
    "TaskRepository",
    "ArchitectureVersionRepository",
    "ArchitectureChangeRequestRepository",
    "ArchitectureProposalRepository",
    "SupervisionRepository",
    "ADRRepository",
    "RiskRepository",
    "FindingRepository",
    "DecisionRepository",
    "AuditRepository",
]


@dataclass(frozen=True)
class TaskKey:
    """Persistence key of a :class:`~architecture_assistant.domain.models.Task`.

    The domain ``Task`` intentionally has no ``id`` field: a Step is dispatched
    at most once per attempt, therefore ``(step_no, attempt)`` identifies a Task.
    """

    step_no: int
    attempt: int

    def __post_init__(self) -> None:
        if isinstance(self.step_no, bool) or not isinstance(self.step_no, int):
            raise ValueError(f"TaskKey.step_no must be an int; got {self.step_no!r}")
        if self.step_no < 1:
            raise ValueError(f"TaskKey.step_no must be >= 1; got {self.step_no!r}")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int):
            raise ValueError(f"TaskKey.attempt must be an int; got {self.attempt!r}")
        if self.attempt < 1:
            raise ValueError(f"TaskKey.attempt must be >= 1; got {self.attempt!r}")

    @classmethod
    def of(cls, task: Task) -> "TaskKey":
        """Build the key of an existing Task."""
        return cls(step_no=task.step_no, attempt=task.attempt)


@runtime_checkable
class ProjectRepository(Protocol):
    """Persistence port for the Project aggregate (key: project name)."""

    def upsert(self, project: Project) -> None: ...

    def get(self, name: str) -> Optional[Project]: ...

    def list(self) -> tuple[Project, ...]: ...

    def delete(self, name: str) -> bool: ...


@runtime_checkable
class StepRepository(Protocol):
    """Persistence port for Steps (key: step number)."""

    def upsert(self, step: Step) -> None: ...

    def get(self, step_no: int) -> Optional[Step]: ...

    def list(self) -> tuple[Step, ...]: ...

    def delete(self, step_no: int) -> bool: ...

    def list_by_state(self, state: StepState) -> tuple[Step, ...]: ...


@runtime_checkable
class TaskRepository(Protocol):
    """Persistence port for Cline dispatch tasks (key: ``(step_no, attempt)``)."""

    def upsert(self, task: Task) -> None: ...

    def get(self, key: TaskKey) -> Optional[Task]: ...

    def list(self) -> tuple[Task, ...]: ...

    def delete(self, key: TaskKey) -> bool: ...

    def list_for_step(self, step_no: int) -> tuple[Task, ...]: ...


@runtime_checkable
class ArchitectureVersionRepository(Protocol):
    """Persistence port for architecture baselines (key: version)."""

    def upsert(self, version: ArchitectureVersion) -> None: ...

    def get(self, version: str) -> Optional[ArchitectureVersion]: ...

    def list(self) -> tuple[ArchitectureVersion, ...]: ...

    def delete(self, version: str) -> bool: ...


@runtime_checkable
class ArchitectureChangeRequestRepository(Protocol):
    """Persistence port for Architecture Change Requests (key: ``request_id``).

    The history is append-and-advance: a request is created once and then only
    ever *advances* through its lifecycle. ``delete`` exists for contract
    consistency with the other repositories, but the architecture-evolution
    use-case never calls it - a proposed request must stay auditable.
    """

    def upsert(self, request: ArchitectureChangeRequest) -> None: ...

    def get(self, request_id: str) -> Optional[ArchitectureChangeRequest]: ...

    def list(self) -> tuple[ArchitectureChangeRequest, ...]: ...

    def list_by_status(
        self, status: ACRStatus
    ) -> tuple[ArchitectureChangeRequest, ...]: ...

    def delete(self, request_id: str) -> bool: ...


@runtime_checkable
class ArchitectureProposalRepository(Protocol):
    """Persistence port for **managed-project** architecture proposals.

    Key: ``proposal_id``. This is project-scoped state (a design for the project
    the assistant manages) and is deliberately separate from
    :class:`ArchitectureVersionRepository`, which holds the assistant's own
    baseline. Like a change request, a proposal is created once and then only
    ever *advances* through its lifecycle, so ``delete`` exists for contract
    consistency with the other repositories but the proposal use-cases never
    call it - a proposal must stay auditable.
    """

    def upsert(self, proposal: ArchitectureProposal) -> None: ...

    def get(self, proposal_id: str) -> Optional[ArchitectureProposal]: ...

    def list(self) -> tuple[ArchitectureProposal, ...]: ...

    def list_by_status(
        self, status: ProposalStatus
    ) -> tuple[ArchitectureProposal, ...]: ...

    def list_for_project(self, project: str) -> tuple[ArchitectureProposal, ...]: ...

    def delete(self, proposal_id: str) -> bool: ...


@runtime_checkable
class SupervisionRepository(Protocol):
    """Persistence port for **supervision records** (key: ``supervision_id``).

    One record per exact supervision identity - one row per
    ``(project, step_no, attempt, source_report_hash, architecture_version)``.
    The record is advisory bookkeeping, never authorization: it can describe a
    recommended directive and the human decision about it, but it holds no Step
    state, no attempt counter and no baseline.

    The three lookups exist because the caller genuinely needs three different
    questions answered:

    * ``find_current`` - "has **this exact report** already been supervised?" (the
      identity lookup the gate and the tick both use);
    * ``list_for_step`` - "what is the supervision history of this attempt?"
      (revisions of the same attempt, and the malformed first-seen search);
    * ``list_by_status`` / ``list_unresolved`` - "what still needs work?" (the
      restart reconciliation and the operator's board).
    """

    def upsert(self, record: SupervisionRecord) -> None: ...

    def get(self, supervision_id: str) -> Optional[SupervisionRecord]: ...

    def find_current(
        self,
        project: str,
        step_no: int,
        attempt: int,
        source_report_hash: str,
        architecture_version: str,
    ) -> Optional[SupervisionRecord]:
        """The record for one exact supervision identity, or ``None``."""
        ...

    def list(self) -> tuple[SupervisionRecord, ...]: ...

    def list_for_step(
        self, project: str, step_no: int, attempt: int
    ) -> tuple[SupervisionRecord, ...]:
        """Every supervision record of one step/attempt, oldest first."""
        ...

    def list_by_status(
        self, status: SupervisorStatus
    ) -> tuple[SupervisionRecord, ...]: ...

    def list_unresolved(self) -> tuple[SupervisionRecord, ...]:
        """Records a restart must reconcile, oldest first.

        Exactly the statuses in :data:`SUPERVISION_RECONCILE_STATUSES`: an
        analysis that was owed when the process died and a send intent whose
        publication may or may not have completed.
        """
        ...

    def delete(self, supervision_id: str) -> bool: ...


@runtime_checkable
class ADRRepository(Protocol):
    """Persistence port for Architecture Decision Records (key: ADR id)."""
    def upsert(self, adr: ADR) -> None: ...

    def get(self, adr_id: str) -> Optional[ADR]: ...

    def list(self) -> tuple[ADR, ...]: ...

    def delete(self, adr_id: str) -> bool: ...


@runtime_checkable
class RiskRepository(Protocol):
    """Persistence port for the risk register (key: risk id)."""

    def upsert(self, risk: Risk) -> None: ...

    def get(self, risk_id: str) -> Optional[Risk]: ...

    def list(self) -> tuple[Risk, ...]: ...

    def delete(self, risk_id: str) -> bool: ...

    def list_open(self) -> tuple[Risk, ...]: ...


@runtime_checkable
class FindingRepository(Protocol):
    """Persistence port for advisor findings (key: finding id)."""

    def upsert(self, finding: Finding) -> None: ...

    def get(self, finding_id: str) -> Optional[Finding]: ...

    def list(self) -> tuple[Finding, ...]: ...

    def delete(self, finding_id: str) -> bool: ...


@runtime_checkable
class DecisionRepository(Protocol):
    """Persistence port for decision-engine outcomes (key: decision id)."""

    def upsert(self, decision: Decision) -> None: ...

    def get(self, decision_id: str) -> Optional[Decision]: ...

    def list(self) -> tuple[Decision, ...]: ...

    def delete(self, decision_id: str) -> bool: ...



@runtime_checkable
class AuditRepository(Protocol):
    """Append-only persistence port for the audit trail.

    There is deliberately no ``update`` or ``delete``: the audit trail is
    immutable history.
    """

    def append(self, entry: AuditEntry) -> None: ...

    def list(self) -> tuple[AuditEntry, ...]: ...

    def list_for_entity(
        self, entity_type: AuditEntityType, entity_id: str
    ) -> tuple[AuditEntry, ...]: ...
