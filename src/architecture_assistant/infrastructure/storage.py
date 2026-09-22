"""SQLite storage facade - the concrete :class:`StoragePort` implementation.

Zero business logic: it composes the already-tested Step 2/5 adapters over one
connection and reuses the Step 5 transaction boundary verbatim. No second
transaction semantics are introduced.
"""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager

from ..ports.repositories import (
    ADRRepository,
    ArchitectureChangeRequestRepository,
    ArchitectureProposalRepository,
    ArchitectureVersionRepository,
    AuditRepository,
    DecisionRepository,
    DeliberationRepository,
    FindingRepository,
    ProjectRepository,
    RiskRepository,
    StepRepository,
    SupervisionRepository,
    TaskRepository,
)
from .repositories import (
    SqliteADRRepository,
    SqliteArchitectureChangeRequestRepository,
    SqliteArchitectureProposalRepository,
    SqliteArchitectureVersionRepository,
    SqliteAuditRepository,
    SqliteDecisionRepository,
    SqliteDeliberationRepository,
    SqliteFindingRepository,
    SqliteProjectRepository,
    SqliteRiskRepository,
    SqliteStepRepository,
    SqliteSupervisionRepository,
    SqliteTaskRepository,
)
from .sqlite import SqliteTransactionPort

__all__ = ["SqliteStorage"]


class SqliteStorage:
    """Typed facade over the SQLite adapters.

    Holds one adapter instance per aggregate plus one transaction boundary; it
    adds no behaviour of its own.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._projects = SqliteProjectRepository(connection)
        self._steps = SqliteStepRepository(connection)
        self._tasks = SqliteTaskRepository(connection)
        self._architecture_versions = SqliteArchitectureVersionRepository(
            connection
        )
        self._change_requests = SqliteArchitectureChangeRequestRepository(
            connection
        )
        self._proposals = SqliteArchitectureProposalRepository(connection)
        self._deliberations = SqliteDeliberationRepository(connection)
        self._supervisions = SqliteSupervisionRepository(connection)
        self._adrs = SqliteADRRepository(connection)
        self._risks = SqliteRiskRepository(connection)
        self._findings = SqliteFindingRepository(connection)
        self._decisions = SqliteDecisionRepository(connection)
        self._audit = SqliteAuditRepository(connection)
        self._transactions = SqliteTransactionPort(connection)

    # -- infrastructure conveniences -------------------------------------
    @property
    def connection(self) -> sqlite3.Connection:
        """The shared SQLite connection."""
        return self._connection

    @property
    def transaction_port(self) -> SqliteTransactionPort:
        """The reused Step 5 transaction boundary."""
        return self._transactions

    # -- StoragePort accessors -------------------------------------------
    @property
    def projects(self) -> ProjectRepository:
        return self._projects

    @property
    def steps(self) -> StepRepository:
        return self._steps

    @property
    def tasks(self) -> TaskRepository:
        return self._tasks

    @property
    def architecture_versions(self) -> ArchitectureVersionRepository:
        return self._architecture_versions

    @property
    def change_requests(self) -> ArchitectureChangeRequestRepository:
        return self._change_requests

    @property
    def proposals(self) -> ArchitectureProposalRepository:
        return self._proposals

    @property
    def deliberations(self) -> DeliberationRepository:
        return self._deliberations

    @property
    def supervisions(self) -> SupervisionRepository:
        return self._supervisions

    @property
    def adrs(self) -> ADRRepository:
        return self._adrs

    @property
    def risks(self) -> RiskRepository:
        return self._risks

    @property
    def findings(self) -> FindingRepository:
        return self._findings

    @property
    def decisions(self) -> DecisionRepository:
        return self._decisions

    @property
    def audit(self) -> AuditRepository:
        return self._audit

    def transaction(self) -> AbstractContextManager[None]:
        """Delegate to the Step 5 transaction boundary - never reimplemented."""
        return self._transactions.transaction()
