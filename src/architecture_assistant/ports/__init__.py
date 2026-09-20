"""Ports (interfaces) of the Architecture Lifecycle Assistant.

Persistence ports live in :mod:`architecture_assistant.ports.repositories`;
capability ports (worker, worker channel, advisor, judge, storage, cost,
reporting, notifier) live in :mod:`architecture_assistant.ports.capabilities`.
"""

from __future__ import annotations

from .capabilities import (
    AdvisorPort,
    AdvisorQuery,
    CanonicalBaseline,
    CostPort,
    CostQuery,
    CostRecord,
    CostSummary,
    JudgeConflict,
    JudgePort,
    Notification,
    NotifierPort,
    PluginCapability,
    RealizationCheckPort,
    RealizationCheckResult,
    RealizationControlPort,
    RealizationGate,
    ReportingPort,
    WorkerChannelPort,
    WorkerPort,
    WorkerRequest,
    WorkerResult,
)
from .repositories import (
    ADRRepository,
    ArchitectureChangeRequestRepository,
    ArchitectureVersionRepository,
    AuditRepository,
    DecisionRepository,
    FindingRepository,
    ProjectRepository,
    RiskRepository,
    StepRepository,
    TaskKey,
    TaskRepository,
)
from .storage import StoragePort
from .transactions import TransactionPort

__all__ = [
    # repositories
    "TaskKey",
    "ProjectRepository",
    "StepRepository",
    "TaskRepository",
    "ArchitectureVersionRepository",
    "ArchitectureChangeRequestRepository",
    "ADRRepository",
    "RiskRepository",
    "FindingRepository",
    "DecisionRepository",
    "AuditRepository",
    # transactions + storage
    "TransactionPort",
    "StoragePort",
    # capability ports
    "PluginCapability",
    "WorkerPort",
    "WorkerChannelPort",
    "RealizationCheckPort",
    "RealizationCheckResult",
    "RealizationControlPort",
    "RealizationGate",
    "CanonicalBaseline",
    "AdvisorPort",
    "JudgePort",
    "CostPort",
    "ReportingPort",
    "NotifierPort",
    # capability messages
    "WorkerRequest",
    "WorkerResult",
    "AdvisorQuery",
    "JudgeConflict",
    "CostRecord",
    "CostQuery",
    "CostSummary",
    "Notification",
]
