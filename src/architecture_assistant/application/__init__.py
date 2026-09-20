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
]
