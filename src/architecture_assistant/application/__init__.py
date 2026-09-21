"""Application layer: use-cases orchestrating the domain through ports.

The application layer depends on ``domain`` and ``ports`` only. It never imports
``infrastructure`` or ``sqlite3``.
"""

from __future__ import annotations

from .adr_manager import (
    ADR_TRANSITIONS,
    ADRManager,
    AdrManagerError,
    AdrNotFoundError,
    InvalidAdrStatusTransitionError,
    InvalidSupersessionError,
)
from .architecture_bootstrap import (
    BOOTSTRAP_ADRS,
    BOOTSTRAP_RISKS,
    PROJECT_NAME,
    PROJECT_PLAN_VERSION,
    RETROSPECTIVE_NOTE,
    AdrSpec,
    ArchitectureBootstrap,
    BootstrapConflictError,
    BootstrapError,
    BootstrapOutcome,
    BootstrapSummary,
    RiskSpec,
    make_project,
)
from .context import (
    SCHEMA_VERSION,
    ContextBuilder,
    ContextError,
    ContextInvariantError,
    ContextSnapshot,
    ProjectNotFoundError,
    StepNotFoundError,
)
from .architecture_versioning import (
    COMPOSITION_ROOT_ADR,
    STEP_9_PROVENANCE,
    ArchitectureVersioning,
    ArchitectureVersioningConflictError,
    ArchitectureVersioningError,
    VersioningSummary,
)
from .architecture_evolution import (
    ArchitectureEvolution,
    ArchitectureEvolutionConflictError,
    ArchitectureEvolutionError,
    ArchitectureEvolutionNotApprovedError,
    ArchitectureEvolutionNotFoundError,
    EvolutionSummary,
    LinkedChange,
)
from .loop_policy import (
    ReviewOutcome,
    decide_review,
    requires_approval,
)
from .orchestrator import (
    DEFAULT_REPORT_SCHEMA,
    DEFAULT_STEP_TIMEOUT,
    DEFAULT_TASK_INSTRUCTIONS,
    HALTING_REASONS,
    LoopInvariantError,
    LoopStatus,
    Orchestrator,
    OrchestratorError,
    TickResult,
)
from .scheduler import (
    DEFAULT_MAX_ITERATIONS,
    LoopHealth,
    Scheduler,
    SchedulerRun,
)
from .realization_control import (
    RealizationControlError,
    RealizationControlUseCase,
)
from .risk_manager import (
    RISK_TRANSITIONS,
    InvalidRiskStatusTransitionError,
    RiskManager,
    RiskManagerError,
    RiskNotFoundError,
)
from .plugin_core import (
    PluginAlreadyRegisteredError,
    PluginBinding,
    PluginDefaultNotSetError,
    PluginError,
    PluginNotFoundError,
    PluginRegistry,
)
from .evidence_merger import (
    AdvisorObservation,
    EvidenceConflict,
    EvidenceIdentityCollisionError,
    EvidenceMergerError,
    EvidenceRelation,
    EvidenceView,
    ObservationOutcome,
    ObservationStatus,
    merge_evidence,
)
from .decision_engine import (
    DecisionEngineError,
    DecisionResult,
    decide_from_observations,
    synthesize_decision,
)
from .judge import (
    JudgeError,
    JudgeJudgment,
    JudgeRun,
    JudgeUseCase,
    build_judge_conflict,
)
from .reporting import (
    REPORT_SCHEMA_VERSION,
    ProjectMissingError,
    ReportBuilder,
    ReportingError,
    ReportingInvariantError,
    ReportSnapshot,
)
from .monitor import Monitor
from .human_override import (
    HumanOverride,
    HumanOverrideError,
    OverrideActorRequiredError,
    OverrideInvariantError,
    OverrideNoChangeError,
    OverrideProjectNotFoundError,
    OverrideReasonRequiredError,
    OverrideStepNotFoundError,
    OverrideTransitionError,
)
from .approval import (
    ApprovalActorRequiredError,
    ApprovalError,
    ApprovalGate,
    ApprovalReasonRequiredError,
    ApprovalStepNotFoundError,
    ApprovalTransitionError,
)

__all__ = [
    # context
    "SCHEMA_VERSION",
    "ContextBuilder",
    "ContextError",
    "ContextInvariantError",
    "ContextSnapshot",
    "ProjectNotFoundError",
    "StepNotFoundError",
    # ADR
    "ADRManager",
    "ADR_TRANSITIONS",
    "AdrManagerError",
    "AdrNotFoundError",
    "InvalidAdrStatusTransitionError",
    "InvalidSupersessionError",
    # risk
    "RiskManager",
    "RISK_TRANSITIONS",
    "RiskManagerError",
    "RiskNotFoundError",
    "InvalidRiskStatusTransitionError",
    # plugin core
    "PluginRegistry",
    "PluginBinding",
    "PluginError",
    "PluginNotFoundError",
    "PluginAlreadyRegisteredError",
    "PluginDefaultNotSetError",
    # architecture bootstrap
    "ArchitectureBootstrap",
    "BootstrapOutcome",
    "BootstrapSummary",
    "BootstrapError",
    "BootstrapConflictError",
    "AdrSpec",
    "RiskSpec",
    "BOOTSTRAP_ADRS",
    "BOOTSTRAP_RISKS",
    "RETROSPECTIVE_NOTE",
    "PROJECT_NAME",
    "PROJECT_PLAN_VERSION",
    "make_project",
    # architecture versioning (minimal versioned baseline update)
    "ArchitectureVersioning",
    "ArchitectureVersioningError",
    "ArchitectureVersioningConflictError",
    "VersioningSummary",
    "COMPOSITION_ROOT_ADR",
    "STEP_9_PROVENANCE",
    # loop policies
    "requires_approval",
    "ReviewOutcome",
    "decide_review",
    # orchestrator
    "Orchestrator",
    "OrchestratorError",
    "LoopInvariantError",
    "LoopStatus",
    "TickResult",
    "DEFAULT_STEP_TIMEOUT",
    "DEFAULT_TASK_INSTRUCTIONS",
    "DEFAULT_REPORT_SCHEMA",
    "HALTING_REASONS",
    # scheduler
    "Scheduler",
    "SchedulerRun",
    "LoopHealth",
    "DEFAULT_MAX_ITERATIONS",
    # realization control (fail-closed architecture gate)
    "RealizationControlUseCase",
    "RealizationControlError",
    # architecture evolution (persistent change requests)
    "ArchitectureEvolution",
    "ArchitectureEvolutionError",
    "ArchitectureEvolutionNotFoundError",
    "ArchitectureEvolutionConflictError",
    "ArchitectureEvolutionNotApprovedError",
    "EvolutionSummary",
    "LinkedChange",
    # evidence merger + decision engine (Step 15)
    "AdvisorObservation",
    "ObservationStatus",
    "EvidenceRelation",
    "ObservationOutcome",
    "EvidenceConflict",
    "EvidenceView",
    "EvidenceMergerError",
    "EvidenceIdentityCollisionError",
    "merge_evidence",
    "DecisionResult",
    "DecisionEngineError",
    "synthesize_decision",
    "decide_from_observations",
    # judge use-case (Step 16)
    "JudgeUseCase",
    "JudgeRun",
    "JudgeJudgment",
    "JudgeError",
    "build_judge_conflict",
    # reporting projection (Step 18)
    "ReportBuilder",
    "ReportSnapshot",
    "REPORT_SCHEMA_VERSION",
    "ReportingError",
    "ProjectMissingError",
    "ReportingInvariantError",
    # project monitor (Step 19)
    "Monitor",
    # human override (Step 20, controlled write)
    "HumanOverride",
    "HumanOverrideError",
    "OverrideActorRequiredError",
    "OverrideReasonRequiredError",
    "OverrideProjectNotFoundError",
    "OverrideStepNotFoundError",
    "OverrideInvariantError",
    "OverrideNoChangeError",
    "OverrideTransitionError",
    # human approval gate (Step 23, controlled write)
    "ApprovalGate",
    "ApprovalError",
    "ApprovalActorRequiredError",
    "ApprovalReasonRequiredError",
    "ApprovalStepNotFoundError",
    "ApprovalTransitionError",
]
