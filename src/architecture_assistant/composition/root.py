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
    ApprovalGate,
    ArchitectureBootstrap,
    ArchitectureEvolution,
    ArchitectureVersioning,
    BootstrapSummary,
    ContextBuilder,
    HumanOverride,
    JudgeUseCase,
    LoopHealth,
    Monitor,
    Orchestrator,
    PROJECT_NAME,
    PROJECT_PLAN_VERSION,
    RealizationControlUseCase,
    ReportBuilder,
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
    ClaudeAdvisorAdapter,
    ClineWorkerAdapter,
    ExcelReportingAdapter,
    GrokAdvisorAdapter,
    MarkdownReportingAdapter,
    OpenAIAdvisorAdapter,
    OpenAIJudgeAdapter,
    SqliteCostPlugin,
    SqliteStorage,
    close_database,
    open_database,
)
from ..ports.capabilities import (
    CanonicalBaseline,
    CostPort,
    JudgePort,
    ReportingPort,
)
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
    "DEFAULT_REPORT_DIR",
    "baseline_v1",
    "baseline_v1_1",
    "canonical_baseline",
    "compose",
]

#: The source tree the assistant inspects by default: itself. It is only a
#: default - the realization gate takes any injected ``source_root``, so the
#: assistant can control other Python/JSX projects later.
DEFAULT_SOURCE_ROOT: Path = Path("src") / "architecture_assistant"

#: Where read-only reporting artifacts (Excel now, markdown later) are written.
#: Deliberately separate from ``exchange_dir``, which belongs to the worker
#: runtime exchange and must never receive reporting products.
DEFAULT_REPORT_DIR: Path = Path("reports")


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
    #: Read-only reporting artifacts. Never ``exchange_dir``: that directory is
    #: the worker runtime channel, not a reporting destination.
    report_dir: Path = DEFAULT_REPORT_DIR
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
        object.__setattr__(self, "report_dir", Path(self.report_dir))
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
            "report_dir": str(self.report_dir),
        }


@dataclass
class Composition:
    """The wired object graph, owned by whoever called :func:`compose`."""

    config: CompositionConfig
    #: The open SQLite connection. Deliberately ``Any``: ``sqlite3`` stays
    #: confined to the infrastructure layer, so this layer must not name it.
    connection: Any
    storage: SqliteStorage
    #: The cost capability: the one idempotent accounting store every provider
    #: adapter reports to through the provider-neutral ``CostPort``. It shares
    #: the storage connection and its transaction ownership rule, and it is
    #: telemetry only - it never decides anything and never changes a
    #: Finding/Decision.
    cost_plugin: CostPort
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
    #: Optional capabilities: built always (construction touches nothing, opens
    #: no connection and reads no key), but *never* part of a decision. Without
    #: the matching API key an advisor simply is not usable, and nothing else in
    #: the graph notices - the deterministic gate, the loop and the review policy
    #: are unaffected. Three independent, consultative perspectives: OpenAI is the
    #: implementation analyst, Claude the critical reviewer / risk analyst, Grok
    #: the challenger / alternative framing. They never vote.
    openai_advisor: OpenAIAdvisorAdapter
    claude_advisor: ClaudeAdvisorAdapter
    grok_advisor: GrokAdvisorAdapter

    #: The judge capability, kept as its own layer. It is consulted **only** for
    #: an explicit, otherwise unresolvable evidence conflict, and the Decision it
    #: returns never replaces the authoritative deterministic one. Both are
    #: optional: a deployment may inject no judge at all, and the deterministic
    #: workflow is unaffected either way.
    judge: Optional[JudgePort]
    judge_use_case: JudgeUseCase

    #: Read-only reporting: the canonical projection plus the two renderers that
    #: turn it into artifacts. All three are read-only - the projection only reads
    #: repository ports (never ``StoragePort`` or ``TransactionPort``), and each
    #: renderer only reads the payload it is handed - so none of them can mutate a
    #: Project, Step, Task, ADR, Risk, ArchitectureVersion, ACR or cost record.
    #: Both renderers write into the same ``report_dir``: one workbook and one
    #: markdown document per snapshot, one snapshot for both.
    report_builder: ReportBuilder
    excel_reporting: ReportingPort
    markdown_reporting: ReportingPort

    #: Read-only project monitor: a pull-based selector layer over the
    #: projection above. It receives **only** ``report_builder`` - no storage, no
    #: transaction, no cost sink, no repository - so it cannot write, pause,
    #: resume or override anything. Rendering is the caller's job. Nothing in the
    #: loop or in a decision consults it.
    monitor: Monitor

    #: The **approval** human write path: the two FSM events a step in
    #: ``WAITING_APPROVAL`` needs - the human go-ahead (which publishes the same
    #: deterministic artifacts the loop publishes) and the authoritative
    #: rejection. It holds only three repository ports, the transaction boundary,
    #: the context builder and the worker channel: no monitor, no reporting, no
    #: realization gate, no architecture port. It can never reach ``VERIFIED``
    #: and it never performs an override operation (that is ``human_override``).
    approval: ApprovalGate

    #: The controlled **state-control** human write path: pause/resume, mode
    #: control and the human Step events the authoritative FSM already defines
    #: (unblock, resolve, abort). Every call writes exactly one domain mutation
    #: and exactly one audit entry inside one transaction, and requires an
    #: explicit actor and a non-empty reason. It cannot reach ``VERIFIED``,
    #: cannot invent a transition and is never handed to the monitor, the loop or
    #: a decision. Approval is deliberately **not** here - it is its own gate.
    human_override: HumanOverride

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

    # 0. The one cost capability, shared by every provider adapter built below.
    #    It owns the same connection as the storage facade, so its writes obey
    #    exactly the same transaction ownership rule - a cost row written inside
    #    `storage.transaction()` is committed or rolled back with that
    #    transaction, never early. It is accounting telemetry, not a
    #    decision-maker: no advisor or judge result depends on it, and a failing
    #    sink can only surface through each adapter's telemetry error.
    cost_plugin = SqliteCostPlugin(connection)

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

    # 4. Optional, consultative capabilities. Constructing them opens no
    #    connection and reads no key (each key is resolved lazily in advise()),
    #    so a project with no key at all runs exactly as before: the application
    #    starts, the deterministic gate still works, and an advisor is simply
    #    unavailable. They are deliberately NOT handed to the orchestrator, the
    #    review policy or the realization gate - deterministic rules stay the
    #    decision maker and an advisor only explains. There is no majority vote
    #    and no provider ranking here: the three advisors stay independent and
    #    never vote.
    openai_advisor = OpenAIAdvisorAdapter(
        project=resolved.project_name,
        cost_sink=cost_plugin,
        clock=resolved.clock,
    )
    claude_advisor = ClaudeAdvisorAdapter(
        project=resolved.project_name,
        cost_sink=cost_plugin,
        clock=resolved.clock,
    )
    grok_advisor = GrokAdvisorAdapter(
        project=resolved.project_name,
        cost_sink=cost_plugin,
        clock=resolved.clock,
    )

    # 5. The judge - a separate layer, not a fourth advisor. It exists for one
    #    situation only: an explicit evidence conflict the deterministic layer
    #    could not resolve. Nothing in the loop calls it, and its Decision never
    #    replaces the authoritative gate verdict; a caller resolves conflicts
    #    explicitly and inspects the gate decision, the evidence view and the
    #    judge run side by side. Constructing it opens no connection and reads no
    #    key, so a project without an OpenAI key still runs unchanged.
    judge = OpenAIJudgeAdapter(
        project=resolved.project_name,
        cost_sink=cost_plugin,
        clock=resolved.clock,
    )
    judge_use_case = JudgeUseCase(judge=judge)

    # 6. Read-only reporting. The projection receives individual repository ports
    #    and two read seams - never StoragePort or TransactionPort - so no write
    #    path exists here. Both renderers only write an artifact into the shared
    #    `report_dir` - a workbook and a markdown document. Neither is part of
    #    any decision.
    report_builder = ReportBuilder(
        storage.projects,
        storage.steps,
        storage.tasks,
        storage.architecture_versions,
        storage.adrs,
        storage.risks,
        storage.findings,
        storage.decisions,
        storage.change_requests,
        cost_query=cost_plugin.query,
        health=scheduler.health,
        clock=resolved.clock,
    )
    excel_reporting = ExcelReportingAdapter(
        resolved.report_dir, clock=resolved.clock
    )

    # The markdown document is the second renderer of the very same payload and
    # writes into the very same `report_dir`: one snapshot, two artifacts.
    markdown_reporting = MarkdownReportingAdapter(
        resolved.report_dir, clock=resolved.clock
    )

    # 7. The read-only monitor. It is handed the projection and nothing else: no
    #    StoragePort, no TransactionPort, no CostPort and no repository, so no
    #    write path is reachable from it. It is pull-based - a caller asks for
    #    `snapshot()` when it wants one - and it is part of no decision and of no
    #    loop step. The operator's ability to *change* the workflow (pause,
    #    resume, override) is a write use-case of its own, not a monitor concern.
    monitor = Monitor(report_builder)

    # 8. The one controlled human write path. It is handed three repository
    #    ports and the shared transaction boundary - never the monitor, never an
    #    adapter. READ (monitor) and CONTROLLED WRITE (human_override) are wired
    #    separately and are never handed to each other, so no read path can
    #    mutate and no write path can present itself as a monitor. Nothing in
    #    the loop or in a decision consults it.
    human_override = HumanOverride(
        storage.projects,
        storage.steps,
        storage.audit,
        storage,
        clock=resolved.clock,
    )

    # 9. The approval gate - the human path a step in ``WAITING_APPROVAL`` needs.
    #    It owns exactly the two FSM events that state declares for a human (the
    #    go-ahead and the rejection) plus the workflow mechanics the go-ahead
    #    requires: the same deterministic artifact publication the loop performs
    #    (through the shared `dispatch` seam), one Task row and exactly one audit
    #    entry per successful call, in one transaction. It is handed no monitor,
    #    no reporting, no realization gate and no architecture port, so it cannot
    #    render a verdict, touch the baseline, or perform an override.
    approval = ApprovalGate(
        storage.steps,
        storage.tasks,
        storage.audit,
        storage,
        context_builder,
        worker,
        clock=resolved.clock,
    )

    return Composition(
        config=resolved,
        connection=connection,
        storage=storage,
        cost_plugin=cost_plugin,
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
        claude_advisor=claude_advisor,
        grok_advisor=grok_advisor,
        judge=judge,
        judge_use_case=judge_use_case,
        report_builder=report_builder,
        excel_reporting=excel_reporting,
        markdown_reporting=markdown_reporting,
        monitor=monitor,
        approval=approval,
        human_override=human_override,
    )
