"""Storage port - the typed composition boundary over the source of truth.

Role (deliberately narrow): expose the repository ports and the Step 5
transaction boundary as one injectable dependency. It contains **no business
logic** and does **not** define its own transaction semantics - ``transaction()``
is inherited from :class:`~architecture_assistant.ports.transactions.TransactionPort`,
so exactly one transaction abstraction exists in the codebase.
"""

from __future__ import annotations

from typing import Protocol

from .repositories import (
    ADRRepository,
    ArchitectureChangeRequestRepository,
    ArchitectureProposalRepository,
    ArchitectureVersionRepository,
    AuditRepository,
    DecisionRepository,
    FindingRepository,
    ProjectRepository,
    RiskRepository,
    StepRepository,
    SupervisionRepository,
    TaskRepository,
)
from .transactions import TransactionPort

__all__ = ["StoragePort"]


class StoragePort(TransactionPort, Protocol):
    """Typed facade over the source of truth.

    Pure aggregation: twelve typed accessors plus the reused transaction
    boundary. Because it inherits :class:`TransactionPort` there is exactly
    **one** transaction contract in the codebase - the facade never introduces
    competing transaction semantics. Conformance is checkable at runtime and
    requires precisely the twelve accessors plus ``transaction``.

    It must stay a thin composition boundary and never grow behaviour.
    """

    @property
    def projects(self) -> ProjectRepository: ...

    @property
    def steps(self) -> StepRepository: ...

    @property
    def tasks(self) -> TaskRepository: ...

    @property
    def architecture_versions(self) -> ArchitectureVersionRepository: ...

    @property
    def change_requests(self) -> ArchitectureChangeRequestRepository: ...

    @property
    def proposals(self) -> ArchitectureProposalRepository: ...

    @property
    def supervisions(self) -> SupervisionRepository: ...

    @property
    def adrs(self) -> ADRRepository: ...

    @property
    def risks(self) -> RiskRepository: ...

    @property
    def findings(self) -> FindingRepository: ...

    @property
    def decisions(self) -> DecisionRepository: ...

    @property
    def audit(self) -> AuditRepository: ...
