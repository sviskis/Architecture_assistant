"""Composition root of the Architecture Lifecycle Assistant.

This module is the **only** place that knows every layer: it opens the SQLite
source of truth, reconciles the versioned architecture baseline, instantiates the
adapters and hands the wired object graph to the caller. It exists because of
:data:`ADR-008 <architecture_assistant.application.COMPOSITION_ROOT_ADR>`:
architecture v1.1 admits ``composition`` as an outer layer precisely so that the
object graph can be built in production code.

Strict rule: **wiring and configuration only**. No business logic, no policy and
no loop decision lives here - the approval policy is in the orchestrator, the
loop is in the scheduler, and the canonical baseline is in the ``architecture``
layer. If this module grows an ``if`` about the workflow, it is a defect.

It also never imports ``sqlite3``: the connection stays typed as ``Any`` because
architecture v1.1 keeps ``sqlite3`` confined to the infrastructure layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional, Union

from ..application import (
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_STEP_TIMEOUT,
    ArchitectureBootstrap,
    ArchitectureEvolution,
    ArchitectureVersioning,
    BootstrapSummary,
    ContextBuilder,
    LoopHealth,
    Orchestrator,
    PROJECT_NAME,
    PROJECT_PLAN_VERSION,
    RealizationControlUseCase,
    Scheduler,
    SchedulerRun,
    VersioningSummary,
)
from ..architecture import ARCHITECTURE_CURRENT, ARCHITECTURE_V1, ARCHITECTURE_V1_1
from ..domain.enums import Mode
from ..domain.models import ArchitectureVersion, Project, utc_now
from ..infrastructure import (
    DEFAULT_DATABASE_PATH,
    DEFAULT_EXCHANGE_DIR,
    DEFAULT_PROTOCOL,
    ClineWorkerAdapter,
    OpenAIAdvisorAdapter,
    SqliteStorage,
    close_database,
    open_database,
)
from ..ports.capabilities import CanonicalBaseline
from .evolution import (
    ReconciliationSummary,
    build_evolution,
    canonical_versions,
    reconcile_declared_changes,
)
from .realization import ArchitectureRealizationAdapter

__all__ = [
    "CompositionConfig",
    "Composition",
    "DEFAULT_SOURCE_ROOT",
    "baseline_v1",
    "baseline_v1_1",
    "canonical_baseline",
    "compose",
]

#: The source tree the assistant inspects by default: itself. It is only a
#: default - the realization gate takes any injected ``source_root``, so the
#: assistant can control other Python/JSX projects later.
DEFAULT_SOURCE_ROOT: Path = Path("src") / "architecture_assistant"


def baseline_v1(now: datetime) -> ArchitectureVersion:
    """Build the canonical v1.0 baseline record from the code baseline.

    This is the seam that Steps 1-8 had to keep in the test suite: the
    canonical rule identifiers live in exactly one place (the ``architecture``
    layer) and are never duplicated by an application service.
    """
    return canonical_versions(now)[ARCHITECTURE_V1.version]


def baseline_v1_1(now: datetime) -> ArchitectureVersion:
    """Build the canonical v1.1 baseline record (composition layer added)."""
    return canonical_versions(now)[ARCHITECTURE_V1_1.version]


def canonical_baseline() -> CanonicalBaseline:
    """The canonical baseline *expectation* the realization gate enforces.

    Derived from the code baseline, so the persisted authoritative version and
    the validator can never drift apart unnoticed.
    """
    return CanonicalBaseline(
        version=ARCHITECTURE_CURRENT.version,
        rule_ids=ARCHITECTURE_CURRENT.rule_ids(),
    )


@dataclass(frozen=True)
class CompositionConfig:
    """Everything the composition root needs - supplied, never discovered."""

    database_path: Union[str, Path] = DEFAULT_DATABASE_PATH
    exchange_dir: Path = DEFAULT_EXCHANGE_DIR
    project_name: str = PROJECT_NAME
    plan_version: str = PROJECT_PLAN_VERSION
    protocol: str = DEFAULT_PROTOCOL
    mode: Mode = Mode.MANUAL
    timeout: Optional[timedelta] = DEFAULT_STEP_TIMEOUT
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    #: The source tree the realization gate inspects (injected, never assumed).
    source_root: Union[str, Path] = DEFAULT_SOURCE_ROOT
    clock: Callable[[], datetime] = utc_now

    def __post_init__(self) -> None:
        if not callable(self.clock):
            raise ValueError("clock must be a callable returning a datetime")
        if not isinstance(self.mode, Mode):
            raise ValueError(f"mode must be a Mode; got {self.mode!r}")
        object.__setattr__(self, "source_root", Path(self.source_root))
        if (
            isinstance(self.max_iterations, bool)
            or not isinstance(self.max_iterations, int)
            or self.max_iterations < 1
        ):
            raise ValueError(
                "max_iterations must be an int >= 1; "
                f"got {self.max_iterations!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation (the clock is omitted)."""
        return {
            "database_path": str(self.database_path),
            "exchange_dir": str(self.exchange_dir),
            "project_name": self.project_name,
            "plan_version": self.plan_version,
            "protocol": self.protocol,
            "mode": self.mode.value,
            "timeout_seconds": (
                None if self.timeout is None else self.timeout.total_seconds()
            ),
            "max_iterations": self.max_iterations,
            "source_root": str(self.source_root),
        }


@dataclass
class Composition:
    """The wired object graph, owned by whoever called :func:`compose`."""

    config: CompositionConfig
    #: The open SQLite connection. Deliberately ``Any``: ``sqlite3`` stays
    #: confined to the infrastructure layer, so this layer must not name it.
    connection: Any
    storage: SqliteStorage
    bootstrap: ArchitectureBootstrap
    versioning: ArchitectureVersioning
    evolution: ArchitectureEvolution
    worker: ClineWorkerAdapter
    context_builder: ContextBuilder
    realization_adapter: ArchitectureRealizationAdapter
    realization_control: RealizationControlUseCase
    orchestrator: Orchestrator
    scheduler: Scheduler
    bootstrap_summary: BootstrapSummary
    versioning_summary: VersioningSummary
    evolution_summary: ReconciliationSummary
    #: Optional capability: built always (construction touches nothing), but
    #: *never* part of a decision. Without an API key it simply is not usable,
    #: and nothing else in the graph notices - the deterministic gate, the loop
    #: and the review policy are unaffected.
    openai_advisor: OpenAIAdvisorAdapter

    def run_until_idle(self) -> SchedulerRun:
        """Chain every immediate transition, then stop and report why."""
        return self.scheduler.run_until_idle()

    def health(self) -> LoopHealth:
        """Read-only loop health, without advancing anything."""
        return self.scheduler.health()

    def close(self) -> None:
        """Release the SQLite connection opened by :func:`compose`."""
        close_database(self.connection)


def compose(config: Optional[CompositionConfig] = None) -> Composition:
    """Open, migrate, reconcile and wire the assistant - wiring only."""
    resolved = config or CompositionConfig()
    now = resolved.clock()

    connection = open_database(resolved.database_path)
    storage = SqliteStorage(connection)

    # 1. Reconcile the historical v1.0 baseline and the canonical records.
    bootstrap = ArchitectureBootstrap(
        storage,
        lambda: baseline_v1(now),
        clock=resolved.clock,
        project_factory=lambda: Project(
            name=resolved.project_name,
            plan_version=resolved.plan_version,
            mode=resolved.mode,
        ),
    )
    bootstrap_summary = bootstrap.seed()

    # 2. Reconcile the declared architecture changes through the persisted
    #    change-request lifecycle: propose -> approve -> apply. The engine drives
    #    the Step 9 supersede primitive, so history is never rewritten.
    versioning = ArchitectureVersioning(storage, clock=resolved.clock)
    evolution = build_evolution(
        storage, clock=resolved.clock, now=now, versioning=versioning
    )
    reconciliation = reconcile_declared_changes(evolution)
    versioning_summary = reconciliation.version_summary

    worker = ClineWorkerAdapter(
        resolved.exchange_dir,
        protocol=resolved.protocol,
        project=resolved.project_name,
        plan_version=resolved.plan_version,
        clock=resolved.clock,
    )
    context_builder = ContextBuilder(
        storage.projects,
        storage.steps,
        storage.adrs,
        storage.risks,
        storage.architecture_versions,
        clock=resolved.clock,
    )

    # 3. Wire the mandatory realization gate: the deterministic architecture
    #    check over the injected source tree, plus the fail-closed gate around
    #    the authoritative persisted baseline.
    realization_adapter = ArchitectureRealizationAdapter(
        resolved.source_root,
        baseline=ARCHITECTURE_CURRENT,
        clock=resolved.clock,
    )
    realization_control = RealizationControlUseCase(
        storage, realization_adapter, canonical=canonical_baseline()
    )

    orchestrator = Orchestrator(
        storage,
        worker,
        context_builder,
        realization_control=realization_control,
        clock=resolved.clock,
        timeout=resolved.timeout,
    )
    scheduler = Scheduler(
        orchestrator, max_iterations=resolved.max_iterations
    )

    # 4. Optional, consultative capability. Constructing it opens no connection
    #    and reads no key (the key is resolved lazily in advise()), so a project
    #    without OPENAI_API_KEY runs exactly as before. It is deliberately NOT
    #    handed to the orchestrator, the review policy or the realization gate:
    #    deterministic rules stay the decision maker, the advisor only explains.
    openai_advisor = OpenAIAdvisorAdapter(
        project=resolved.project_name, clock=resolved.clock
    )
    return Composition(
        config=resolved,
        connection=connection,
        storage=storage,
        bootstrap=bootstrap,
        versioning=versioning,
        evolution=evolution,
        worker=worker,
        context_builder=context_builder,
        realization_adapter=realization_adapter,
        realization_control=realization_control,
        orchestrator=orchestrator,
        scheduler=scheduler,
        bootstrap_summary=bootstrap_summary,
        versioning_summary=versioning_summary,
        evolution_summary=reconciliation,
        openai_advisor=openai_advisor,
    )
