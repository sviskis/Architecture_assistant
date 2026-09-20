"""Domain layer: enums, frozen domain models and deterministic FSMs.

This package is dependency-free (standard library only) and contains no
persistence, network, UI or plugin code.
"""

from __future__ import annotations

from .audit import (
    AuditAction,
    AuditEntityType,
    AuditEntry,
)
from .enums import (
    ACRStatus,
    ADRStatus,
    DecisionStatus,
    Mode,
    Phase,
    ReportStatus,
    RiskLevel,
    RiskStatus,
    Severity,
    StepEvent,
    StepState,
    TaskEvent,
    TaskState,
)
from .fsm import (
    STEP_TERMINAL_STATES,
    STEP_TRANSITIONS,
    TASK_TERMINAL_STATES,
    TASK_TRANSITIONS,
    FiniteStateMachine,
    FSMError,
    InvalidTransitionError,
    StepStateMachine,
    TaskStateMachine,
)
from .models import (
    ADR,
    ArchitectureChangeRequest,
    ArchitectureVersion,
    Decision,
    DomainModel,
    Finding,
    Project,
    Risk,
    Step,
    Task,
    utc_now,
)

__all__ = [
    # audit
    "AuditAction",
    "AuditEntityType",
    "AuditEntry",
    # enums
    "ACRStatus",
    "ADRStatus",
    "DecisionStatus",
    "Mode",
    "Phase",
    "ReportStatus",
    "RiskLevel",
    "RiskStatus",
    "Severity",
    "StepEvent",
    "StepState",
    "TaskEvent",
    "TaskState",
    # models
    "ADR",
    "ArchitectureChangeRequest",
    "ArchitectureVersion",
    "Decision",
    "DomainModel",
    "Finding",
    "Project",
    "Risk",
    "Step",
    "Task",
    "utc_now",
    # fsm
    "FSMError",
    "FiniteStateMachine",
    "InvalidTransitionError",
    "StepStateMachine",
    "TaskStateMachine",
    "STEP_TRANSITIONS",
    "TASK_TRANSITIONS",
    "STEP_TERMINAL_STATES",
    "TASK_TERMINAL_STATES",
]
