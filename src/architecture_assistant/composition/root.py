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
    DEFAULT_MALFORMED_STABILITY,
    DEFAULT_MALFORMED_TIMEOUT,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_REVIEW_QUESTION,
    DEFAULT_STEP_TIMEOUT,
    EVENT_LEVEL_ERROR,
    EVENT_LEVEL_INFO,
    EVENT_LEVEL_WARN,
    EVENT_LEVELS,
    LOG_COMPONENTS,
    ACTION_CANCEL,
    ACTION_GENERATE_FINAL_SYNTHESIS,
    ACTION_GENERATE_LEAD_REVIEW,
    ACTION_GENERATE_PROPOSAL,
    ACTION_RUN_ROUND1,
    ACTION_RUN_ROUND2,
    DELIBERATION_ACTIONS,
    ApprovalGate,
    ArchitectureBootstrap,
    ArchitectureDeliberation,
    ArchitectureEvolution,
    ArchitectureReview,
    ArchitectureSynthesis,
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
    PlanLoader,
    ProposalApproval,
    RealizationControlUseCase,
    ReportBuilder,
    Scheduler,
    SchedulerRun,
    Supervision,
    SupervisionGate,
    SupervisorRuntime,
    VersioningSummary,
    build_event,
    component_for,
    exception_reason,
    sanitize_text,
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
    ScriptedSupervisor,
    SqliteCostPlugin,
    SqliteStorage,
    close_database,
    open_database,
)
from ..ports.capabilities import (
    AdvisorPort,
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
from .evidence import observe
from .realization import ArchitectureRealizationAdapter
from .advisor_factory import (
    AdvisorFactory,
    assemble_deliberation,
    assemble_reviewers,
)
from .provider_settings import (
    DEFAULT_PROVIDER_SETTINGS_PATH,
    SETTINGS_STATUS_LOADED,
    ProviderSettings,
    ProviderSettingsLoad,
    load_provider_settings,
)

__all__ = [
    "CompositionConfig",
    "Composition",
    "DEFAULT_SOURCE_ROOT",
    "DEFAULT_REPORT_DIR",
    "DEFAULT_REVIEW_QUESTION",
    "DELIBERATION_ACTIONS",
    "ACTION_RUN_ROUND1",
    "ACTION_GENERATE_LEAD_REVIEW",
    "ACTION_RUN_ROUND2",
    "ACTION_GENERATE_FINAL_SYNTHESIS",
    "ACTION_GENERATE_PROPOSAL",
    "ACTION_CANCEL",
    "DEFAULT_PROVIDER_SETTINGS_PATH",
    "apply_provider_settings",
    "EVENT_LEVELS",
    "EVENT_LEVEL_INFO",
    "EVENT_LEVEL_WARN",
    "EVENT_LEVEL_ERROR",
    "LOG_COMPONENTS",
    "build_event",
    "component_for",
    "exception_reason",
    "sanitize_text",
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
    #: Whether advisory supervision gates the authoritative review at all. Off by
    #: default: with supervision disabled the loop is byte-for-byte the Step 27
    #: loop, and an operator turns it on deliberately.
    supervision: bool = False
    #: How often a host should tick the supervision runtime (Phase 18: ~2s).
    supervisor_poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS
    #: Malformed-report thresholds: how long the *same* malformed bytes must
    #: persist, and how long malformed bytes may keep changing, before the record
    #: escalates to an operator. Both are persisted, so both survive a restart.
    malformed_stability: timedelta = DEFAULT_MALFORMED_STABILITY
    malformed_timeout: timedelta = DEFAULT_MALFORMED_TIMEOUT
    #: Bounded operator constraints handed to a supervisor as **facts** (never a
    #: chat history, never a secret). Empty by default.
    operator_constraints: tuple[str, ...] = ()
    #: Where each advisor's provider/model/key is remembered. A local preference
    #: (Git-ignored ``data/``), never domain state, never in the database. A
    #: missing file means the documented defaults (OpenAI, Claude, Grok) and a
    #: malformed one means the same defaults plus an explicit "invalid" report.
    provider_settings_path: Union[str, Path] = DEFAULT_PROVIDER_SETTINGS_PATH

    def __post_init__(self) -> None:
        if not callable(self.clock):
            raise ValueError("clock must be a callable returning a datetime")
        if not isinstance(self.mode, Mode):
            raise ValueError(f"mode must be a Mode; got {self.mode!r}")
        if not isinstance(self.supervision, bool):
            raise ValueError(
                f"supervision must be a bool; got {self.supervision!r}"
            )
        if (
            isinstance(self.supervisor_poll_interval, bool)
            or not isinstance(self.supervisor_poll_interval, (int, float))
            or self.supervisor_poll_interval <= 0
        ):
            raise ValueError(
                "supervisor_poll_interval must be a positive number; got "
                f"{self.supervisor_poll_interval!r}"
            )
        for label, value in (
            ("malformed_stability", self.malformed_stability),
            ("malformed_timeout", self.malformed_timeout),
        ):
            if not isinstance(value, timedelta) or value <= timedelta(0):
                raise ValueError(f"{label} must be a positive timedelta")
        if self.malformed_stability > self.malformed_timeout:
            raise ValueError(
                "malformed_stability must not exceed malformed_timeout; got "
                f"{self.malformed_stability} and {self.malformed_timeout}"
            )
        object.__setattr__(
            self,
            "operator_constraints",
            tuple(str(item) for item in (self.operator_constraints or ())),
        )
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
            "supervision": self.supervision,
            "supervisor_poll_interval": self.supervisor_poll_interval,
            "malformed_stability_seconds": (
                self.malformed_stability.total_seconds()
            ),
            "malformed_timeout_seconds": (
                self.malformed_timeout.total_seconds()
            ),
            "operator_constraints": list(self.operator_constraints),
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

    #: The controlled **initial plan import**: the only way a plan enters the
    #: source of truth. It validates a plan file completely, refuses everything
    #: it cannot vouch for (an already loaded plan, a different project identity,
    #: a different plan version, any unknown field) and writes all steps plus
    #: exactly one ``PLAN``/``IMPORT`` audit entry in one transaction. It cannot
    #: create or rename the project, cannot repair invalid input and cannot set a
    #: workflow state. It is handed no monitor, no reporting and no worker.
    plan_loader: PlanLoader

    #: The explicit, advisory **three-advisor architecture review**. It is handed
    #: the three consultative advisors in a fixed order, the provider-neutral
    #: observation seam, the judge use-case, the *read-only* realization check,
    #: the cost query and the clock - and no storage, transaction, repository,
    #: monitor, orchestrator or scheduler. One call runs one review: the advisors,
    #: the evidence merger, the decision engine, the judge only for a structurally
    #: valid conflict, and a JSON-safe result. It writes nothing, changes no
    #: workflow state and can never reach ``VERIFIED``.
    architecture_review: ArchitectureReview

    #: The controlled **architecture deliberation** (Step 29): the two-round,
    #: advisory review board over one operator requirement - two independent
    #: architects, a chair's review, one bounded reconsideration each and a final
    #: synthesis that can become a managed-project proposal. It is wired with the
    #: deliberation repository, the proposal repository, the project repository,
    #: the audit trail and the shared transaction boundary - and **no**
    #: ``ArchitectureEvolution``, ``ArchitectureVersioning``, ``ADRManager``,
    #: ``RiskManager``, monitor, orchestrator, worker or realization port. It
    #: approves nothing and reaches ``VERIFIED`` never.
    architecture_deliberation: ArchitectureDeliberation

    #: The provider configuration this graph was wired with: which provider,
    #: which model and which (local) key each advisor slot uses. It is held so
    #: the panel can show the live configuration and so a saved change can
    #: re-wire the review without recomposing the whole assistant. It is
    #: configuration, never state: nothing here is persisted, audited, reported
    #: or copied into a Finding, a Decision or an ArchitectureReviewResult.
    provider_settings: ProviderSettings
    #: How that configuration was obtained: loaded, missing (defaults) or
    #: invalid (defaults + "provider settings invalid"). Displayed, never acted on.
    provider_settings_status: str

    #: The **managed-project** architecture proposal generator: the one path
    #: from one advisory review to one durable, project-scoped design. It is
    #: handed the proposal repository, the audit trail and the shared
    #: transaction boundary - and, optionally, a provider-neutral
    #: :class:`~architecture_assistant.ports.capabilities.SynthesisPort`. It is
    #: deliberately handed **no** ``ArchitectureEvolution``,
    #: ``ArchitectureVersioning``, ``ADRManager``, ``RiskManager`` or
    #: realization port, so a proposal can never mutate the assistant's own
    #: baseline, create an ACR, write an assistant ADR/risk/rule or reach the
    #: deterministic gate.
    architecture_synthesis: ArchitectureSynthesis

    #: The **proposal approval** human write path - the three decisions a
    #: managed-project proposal can receive (approve, reject, request a
    #: revision). It holds three repository ports and the transaction boundary
    #: and nothing else, so approving a managed project's design stays entirely
    #: separate from the assistant's own architecture lifecycle.
    proposal_approval: ProposalApproval

    #: The **offline** supervisor: the scripted double that answers from a fixed
    #: script and never touches a network, a key or a provider. Phase 1 wires this
    #: one on purpose - the real Codex adapter arrives in Phase 2 behind the very
    #: same ``SupervisorPort``, and until then supervision can be exercised
    #: end-to-end with no provider at all.
    supervisor: ScriptedSupervisor

    #: The advisory supervision use-case. The only table it writes is
    #: ``supervision_records`` and the only artifact it publishes is the directive
    #: file of the one Cline channel: no Step, no attempt, no baseline, no ACR, no
    #: ADR and no Risk is reachable from it.
    supervision: Supervision

    #: The fail-closed, read-only gate the orchestrator consults. It is handed to
    #: the loop and to nobody else, so the loop can be blocked or allowed but never
    #: redirected - and passing ``None`` to the loop is how "supervision disabled
    #: means exactly the previous behaviour" is expressed in code.
    supervision_gate: SupervisionGate

    #: The headless supervision runtime. Built here (composition owns its
    #: dependencies) and deliberately **not started**: whoever hosts the loop - the
    #: GUI app or a test - decides when ticking begins. It owns no thread.
    supervisor_runtime: SupervisorRuntime

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

    # 3b. Advisory supervision (Step 28 Phase 1). Two decisions are made here and
    #     nowhere else.
    #
    #     First: **off by default**. With `supervision=False` the loop receives no
    #     gate at all, so the authoritative workflow is exactly the Step 27
    #     workflow - the same transitions, the same audits, the same ACKs.
    #
    #     Second: the supervisor is the **offline scripted double**. Constructing
    #     it opens no connection and reads no key, so a deployment with no provider
    #     still runs the whole lifecycle; the real Codex adapter will replace it
    #     behind the same port without touching anything else. Whatever the answer,
    #     the assistant stays authoritative: the supervisor holds no storage, no
    #     FSM and no gate.
    supervision_gate = SupervisionGate(
        storage,
        worker,
        architecture_version=ARCHITECTURE_CURRENT.version,
        enabled=resolved.supervision,
        clock=resolved.clock,
    )
    supervisor = ScriptedSupervisor()
    supervision = Supervision(
        storage,
        worker,
        supervisor,
        supervision_gate,
        operator_constraints=resolved.operator_constraints,
        malformed_stability=resolved.malformed_stability,
        malformed_timeout=resolved.malformed_timeout,
    )
    supervisor_runtime = SupervisorRuntime(
        storage,
        supervision,
        interval=resolved.supervisor_poll_interval,
        clock=resolved.clock,
    )

    orchestrator = Orchestrator(
        storage,
        worker,
        context_builder,
        realization_control=realization_control,
        clock=resolved.clock,
        timeout=resolved.timeout,
        supervision=supervision_gate if resolved.supervision else None,
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
    #    Which provider, model and key each advisor slot uses is the operator's
    #    *local* configuration (``data/provider_settings.json``): read exactly
    #    here, and nowhere else. The read cannot fail - a missing file means the
    #    documented defaults (OpenAI, Claude, Grok, i.e. the wiring this graph had
    #    before the file existed) and a malformed one means the same defaults plus
    #    an explicit "invalid" status the panel reports.
    provider_settings_load: ProviderSettingsLoad = load_provider_settings(
        resolved.provider_settings_path
    )
    provider_settings = provider_settings_load.settings
    #    These three are the composition's canonical capability handles. A default
    #    advisor slot reuses exactly these instances (see ``assemble_reviewers``),
    #    so one provider is one adapter object - never a second, parallel one.
    openai_advisor = AdvisorFactory.create(
        "openai",
        project=resolved.project_name,
        cost_sink=cost_plugin,
        clock=resolved.clock,
    )
    claude_advisor = AdvisorFactory.create(
        "claude",
        project=resolved.project_name,
        cost_sink=cost_plugin,
        clock=resolved.clock,
    )
    grok_advisor = AdvisorFactory.create(
        "grok",
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

    # 10. The controlled plan loader - the one way an initial plan enters the
    #     source of truth. It is handed three repository ports and the shared
    #     transaction boundary (the same least-power wiring as the human write
    #     paths above) and no monitor, reporting, worker or architecture port: it
    #     validates a plan file, imports it atomically or refuses, and can never
    #     create, rename or otherwise touch the project identity.
    plan_loader = PlanLoader(
        storage.projects,
        storage.steps,
        storage.audit,
        storage,
        clock=resolved.clock,
    )

    # 11. The explicit, advisory architecture review. It is the **only** caller of
    #     the advisory capabilities, and it is reachable only through an explicit
    #     operator action - never from the loop, the gate or a decision. The
    #     reviewers keep their configured order, and each relation is *explicit
    #     metadata*: nothing is declared here, so nothing is inferred and no
    #     evidence conflict can exist by default. The check is the read-only
    #     realization adapter: it validates freshly and persists nothing, so the
    #     review can report the deterministic verdict without ever writing it.
    architecture_review = _build_architecture_review(
        provider_settings,
        canonical=_canonical_adapters(
            openai_advisor, claude_advisor, grok_advisor
        ),
        project=resolved.project_name,
        cost_sink=cost_plugin,
        source_root=resolved.source_root,
        judge=judge_use_case,
        check=realization_adapter,
        cost_query=cost_plugin.query,
        clock=resolved.clock,
    )

    # 12. The **managed-project** architecture proposal. Two facts decide its
    #     shape. First, it is project-scoped: it produces a durable design for
    #     the project this database manages and is *not* the assistant's own
    #     baseline, so it is wired with the proposal repository, the audit trail
    #     and the shared transaction boundary only. Second, it is deterministic
    #     by default: no `SynthesisPort` is configured here, so the offline
    #     organizer runs and a proposal can always be produced with no provider,
    #     no key and no cost. A deployment that later adds a provider-neutral
    #     synthesizer passes it as `synthesis=` - and the offline path stays the
    #     fallback that never needs one.
    #
    #     Neither object below is handed `evolution`, `versioning` or the
    #     realization control: an approved proposal can therefore never mutate
    #     `ArchitectureVersion`, create an ACR, write an assistant ADR/risk/rule
    #     or reach the deterministic gate. That separation is a wiring decision,
    #     not a convention.
    architecture_synthesis = ArchitectureSynthesis(
        storage.proposals,
        storage.audit,
        storage,
        clock=resolved.clock,
    )

    # 13. The proposal approval human write path. It owns exactly three
    #     decisions on one persisted proposal (approve, reject, request a
    #     revision) plus the staleness, fingerprint and project-identity checks
    #     that make each of them safe: one status change and exactly one audit
    #     entry per act, in one transaction, with an explicit actor and reason.
    #     It is handed no monitor, no worker and no architecture port.
    proposal_approval = ProposalApproval(
        storage.proposals,
        storage.projects,
        storage.audit,
        storage,
        clock=resolved.clock,
    )

    # 14. The controlled **architecture deliberation**. Its seats come from the
    #     same provider configuration the panel edits, and its collaborators are
    #     exactly the deliberation repository, the proposal repository, the
    #     project repository, the audit trail and the shared transaction boundary.
    #     A proposal it generates is a DRAFT, and only the human path above can
    #     ever decide it.
    architecture_deliberation = _build_architecture_deliberation(
        storage,
        provider_settings,
        project=resolved.project_name,
        cost_sink=cost_plugin,
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
        plan_loader=plan_loader,
        architecture_review=architecture_review,
        architecture_deliberation=architecture_deliberation,
        provider_settings=provider_settings,
        provider_settings_status=provider_settings_load.status,
        architecture_synthesis=architecture_synthesis,
        proposal_approval=proposal_approval,
        supervisor=supervisor,
        supervision=supervision,
        supervision_gate=supervision_gate,
        supervisor_runtime=supervisor_runtime,
    )


# ---------------------------------------------------------------------------
# the advisory review wiring, keyed by the operator's provider configuration
# ---------------------------------------------------------------------------


def _canonical_adapters(
    openai_advisor: AdvisorPort,
    claude_advisor: AdvisorPort,
    grok_advisor: AdvisorPort,
) -> dict[str, AdvisorPort]:
    """The composition's own provider handles, by provider id.

    ``assemble_reviewers`` reuses one of these for a *default* slot, so the
    composed review and the composition's advisor attributes are the same
    objects - the capability exists once, not twice.
    """
    return {
        "openai": openai_advisor,
        "claude": claude_advisor,
        "grok": grok_advisor,
    }


def _build_architecture_review(
    settings: ProviderSettings,
    *,
    canonical: Mapping[str, AdvisorPort],
    project: str,
    cost_sink: CostPort,
    source_root: Union[str, Path],
    judge: JudgeUseCase,
    check: Any,
    cost_query: Callable[..., Any],
    clock: Callable[[], datetime],
) -> ArchitectureReview:
    """The advisory review, wired from one provider configuration.

    Wiring only: it builds one ``AdvisorReviewer`` per advisor slot from the
    configuration and hands the review the *same* seams it has always had - the
    provider-neutral observation seam, the judge, the read-only realization
    check, the cost query, the source root and the clock. It contains no policy
    and decides nothing.
    """
    return ArchitectureReview(
        assemble_reviewers(
            settings,
            project=project,
            cost_sink=cost_sink,
            clock=clock,
            shared=canonical,
        ),
        observer=observe,
        source_root=source_root,
        judge=judge,
        check=check,
        cost_query=cost_query,
        clock=clock,
    )


def _build_architecture_deliberation(
    storage: Any,
    settings: ProviderSettings,
    *,
    project: str,
    cost_sink: CostPort,
    clock: Callable[[], datetime],
) -> ArchitectureDeliberation:
    """The deliberation board, wired from one provider configuration.

    Wiring only: it turns the operator's ``agent_a``/``agent_b``/``lead`` slots
    into two seats and a chair and hands the use-case its five collaborators. It
    contains no policy, decides nothing and approves nothing.
    """
    agent_a, agent_b, chair = assemble_deliberation(
        settings,
        project=project,
        cost_sink=cost_sink,
        clock=clock,
    )
    return ArchitectureDeliberation(
        storage.deliberations,
        storage.proposals,
        storage.projects,
        storage.audit,
        storage,
        agent_a=agent_a,
        agent_b=agent_b,
        chair=chair,
        clock=clock,
    )


def apply_provider_settings(
    composition: "Composition", settings: ProviderSettings
) -> None:
    """Re-wire the advisory review for a new provider configuration.

    Called when the operator saves provider settings. Only the *reviewers* can
    change: every other seam of the review is reused unchanged, and nothing on
    the deterministic side of the graph - the FSM, the realization gate, the
    validator, the rules, the baseline and the audit trail - is touched, because
    none of them is even reachable from here.

    Re-wiring (rather than a global) is the point: the panel's configuration is
    an explicit, local preference, and applying it must not be able to change
    anything but which advisor answers which question.
    """
    if not isinstance(composition, Composition):
        raise ValueError("composition must be a Composition")
    if not isinstance(settings, ProviderSettings):
        raise ValueError("settings must be a ProviderSettings")
    composition.provider_settings = settings
    # A saved configuration is, by definition, a loaded one - the panel must stop
    # saying "invalid" the moment the operator has replaced the damaged file.
    composition.provider_settings_status = SETTINGS_STATUS_LOADED
    composition.architecture_review = _build_architecture_review(
        settings,
        canonical=_canonical_adapters(
            composition.openai_advisor,
            composition.claude_advisor,
            composition.grok_advisor,
        ),
        project=composition.config.project_name,
        cost_sink=composition.cost_plugin,
        source_root=composition.config.source_root,
        judge=composition.judge_use_case,
        check=composition.realization_adapter,
        cost_query=composition.cost_plugin.query,
        clock=composition.config.clock,
    )
    # The deliberation seats are re-placed too: a saved configuration must apply
    # to the review board exactly as it applies to the quick review. A run that is
    # already underway is never re-pointed - its fingerprint records who answered.
    agent_a, agent_b, chair = assemble_deliberation(
        settings,
        project=composition.config.project_name,
        cost_sink=composition.cost_plugin,
        clock=composition.config.clock,
    )
    composition.architecture_deliberation.configure(
        agent_a=agent_a, agent_b=agent_b, chair=chair
    )
