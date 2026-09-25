# Architecture Assistant - Architecture Overview

| | |
| --- | --- |
| Document | `Arhitect_goal/ARCHITECTURE.md` |
| Repository | `Architecture_assistant` |
| Revision analysed | branch `v1.1-gui`, HEAD `ebe36b9` plus uncommitted working-tree changes |
| Baseline analysed | architecture baseline **v1.1** (71 scanned modules, 3 deterministic rules) |
| Language / runtime | Python `>=3.11` (measured on CPython `3.14.0`, Windows) |
| Method | Static reading of every file in the repository plus read-only runtime measurements in throw-away databases under `%TEMP%`. No repository file was modified, no test was executed, nothing was committed. |

---

## 1. Project overview

Architecture Assistant is a Python "architecture lifecycle" application that drives **one**
software project through a persisted, step-by-step workflow and keeps every decision
traceable. A file-backed SQLite database is the single runtime source of truth (project,
steps, tasks, architecture versions, ADRs, risks, findings, decisions, change requests,
proposals, deliberations, supervisions, cost records, audit entries), and a deterministic
finite state machine - a pure lookup in an authoritative transition table - owns every
workflow transition, so the process may die at any moment and a restart resumes exactly where
it stopped. Around that core sit provider-neutral *capability* ports (worker, advisors, judge,
cost, reporting, supervisor, deliberation seats) whose concrete adapters live only in the
infrastructure layer, while AI providers stay strictly **advisory**: the deterministic
architecture validator and the realization gate can never be out-voted. The only write paths
are explicit, audited human actions (approve/reject, override, plan import, proposal decision,
supervision decision), and each performs exactly one mutation plus exactly one audit entry
inside one transaction. A separate, presentation-only operator panel
(`architecture_assistant_gui`, a CustomTkinter desktop application) is an *external host*: it
never touches SQLite, owns no core object, and reaches the core exclusively through one
dedicated worker thread.

---

## 2. Folder structure

```text
Architecture_assistant/
├── pyproject.toml                    packaging + pytest config (src layout, testpaths=tests)
├── .gitignore                        caches, .mini_build/, data/, reports/, *.db, .env
├── README.md                         the main (v1.0/v1.1) document, 965 lines
├── USER_GUIDE_LV.md                  Latvian operator quick guide, 96 lines (untracked)
├── START_ARCHITECTURE_ASSISTANT.bat          windowless launcher (pythonw, own PYTHONPATH)
├── START_ARCHITECTURE_ASSISTANT_CONSOLE.bat  same launcher, console kept for logs
├── src/
│   ├── architecture_assistant/       the frozen core - this is the scanned tree
│   │   ├── __init__.py               package metadata (__version__ = "0.1.0")
│   │   ├── domain/
│   │   │   ├── __init__.py           re-exports enums, models, fsm, audit
│   │   │   ├── enums.py              Phase, Mode, RiskLevel, Severity, StepState, StepEvent,
│   │   │   │                         TaskState, TaskEvent, ReportStatus, ACRStatus, ADRStatus,
│   │   │   │                         RiskStatus, DecisionStatus, ProposalStatus,
│   │   │   │                         Deliberation*, Round2Decision, Supervisor*
│   │   │   ├── models.py             Project, Step, Task, ArchitectureVersion,
│   │   │   │                         ArchitectureChangeRequest, ArchitectureProposal,
│   │   │   │                         DeliberationRun, DeliberationArtifact, ADR, Risk,
│   │   │   │                         Finding, Decision, SupervisionRecord, utc_now
│   │   │   ├── fsm.py                FiniteStateMachine, StepStateMachine, TaskStateMachine,
│   │   │   │                         STEP_TRANSITIONS, TASK_TRANSITIONS, terminal sets
│   │   │   └── audit.py              AuditEntityType, AuditAction, AuditEntry
│   │   ├── ports/
│   │   │   ├── __init__.py           re-exports the protocol surface
│   │   │   ├── capabilities.py       WorkerPort, WorkerChannelPort, SupervisionChannelPort,
│   │   │   │                         RealizationCheckPort, RealizationControlPort,
│   │   │   │                         CanonicalBaseline, AdvisorPort, JudgePort, CostPort,
│   │   │   │                         ReportingPort, NotifierPort, SynthesisPort,
│   │   │   │                         SupervisorPort, SupervisionGatePort,
│   │   │   │                         DeliberationAgentPort, DeliberationLeadPort + DTOs
│   │   │   ├── repositories.py       Project/Step/Task/ArchitectureVersion/ACR/Proposal/
│   │   │   │                         Supervision/Deliberation/ADR/Risk/Finding/Decision/
│   │   │   │                         Audit repository protocols + TaskKey
│   │   │   ├── storage.py            StoragePort (13 accessors + transaction)
│   │   │   └── transactions.py       TransactionPort
│   │   ├── architecture/
│   │   │   ├── __init__.py           re-exports model, rules, scanner, validator
│   │   │   ├── model.py              SourceModel, RuleViolation, ValidationResult,
│   │   │   │                         ArchitectureRule, ArchitectureBaseline
│   │   │   ├── rules.py              ARCHITECTURE_V1 / ARCHITECTURE_V1_1 / *_CURRENT,
│   │   │   │                         KNOWN_LAYERS, ALLOWED_IMPORTS, layer_of, 3 rules
│   │   │   ├── scanner.py            scan_directory, discover_python_files,
│   │   │   │                         extract_imports, build_source_model, module_name_for
│   │   │   └── validator.py          ArchitectureValidator.validate()
│   │   ├── application/              use cases - 28 modules (section 3.5)
│   │   ├── infrastructure/           concrete adapters (section 3.6)
│   │   └── composition/              the wiring point (section 3.7): root.py, realization.py,
│   │                                 evidence.py, evolution.py, advisor_factory.py,
│   │                                 provider_settings.py
│   └── architecture_assistant_gui/   operator panel - external host, NOT scanned
│       ├── __init__.py               __main__.py (python -m ...)  app.py  controller.py
│       ├── core.py                   one core thread + queues
│       ├── views.py  workspace_views.py  workspace.py  windows.py  theme.py
│       └── layout.py  project_setup.py
├── examples/
│   └── plan_youtube_to_mp3.json      the pilot plan: 12 steps, real Phase values
├── tests/                            63 files, 50,127 lines, no conftest.py
│   ├── __init__.py  _recovery_child.py (subprocess crash driver, not a pytest module)
│   ├── test_fsm.py  test_models.py  test_capabilities.py  test_storage_port.py
│   ├── test_repositories.py  test_migrations.py  test_audit_trail.py  test_context.py
│   ├── test_architecture*.py  test_adr_manager.py  test_risk_manager.py
│   ├── test_orchestrator.py  test_scheduler.py  test_loop_policy.py
│   ├── test_realization_control.py  test_realization_gate.py
│   ├── test_openai_advisor.py  test_claude_advisor.py  test_grok_advisor.py
│   ├── test_deepseek_advisor.py  test_openai_judge.py  test_judge_use_case.py
│   ├── test_evidence_merger.py  test_evidence_composition.py  test_decision_engine.py
│   ├── test_cost_plugin.py  test_cost_composition.py  test_advisor_factory.py
│   ├── test_reporting.py  test_excel_reporting.py  test_markdown_reporting.py
│   ├── test_monitor.py  test_human_override.py  test_approval_gate.py
│   ├── test_plan_loader.py  test_architecture_review.py  test_architecture_synthesis.py
│   ├── test_architecture_deliberation.py  test_deliberation_persistence.py
│   ├── test_proposal_approval.py  test_supervision_gate.py  test_supervisor_runtime.py
│   ├── test_supervisor_policy.py  test_provider_settings.py  test_provider_secret_safety.py
│   ├── test_gui_logs.py  test_gui_layout.py  test_gui_layout_store.py
│   ├── test_gui_boundaries.py  test_gui_core.py  test_gui_controller.py
│   ├── test_gui_popups.py  test_gui_provider_settings.py  test_gui_deliberation.py
│   └── test_cline_worker.py  test_composition.py  test_recovery.py
├── data/                             Git-ignored runtime area (present locally)
│   ├── architecture_assistant.db     200,704 bytes, migrations v7, journal_mode=delete
│   ├── youtube_to_mp3.db             135,168 bytes, migrations v4, 12 steps
│   ├── gui_layout.json               374 bytes - remembered window layout (a preference)
│   └── dark_gui_preview.png  gui_workspace_preview.png
├── .mini_build/                      build controller's own area (Git-ignored, not product)
│   └── build_state.db  to_cline/  from_cline/  archive/ (step reports)  backups/
└── .pytest_cache/                    pytest cache (stale - see STRATEGY.md section 2)
```

Measured size of the tree: **83 Python files / 46,563 lines** under `src/` (5.84 MB), of which
**71 modules / 1,358,305 bytes** are the scanner's own tree (`src/architecture_assistant`), and
`src/architecture_assistant_gui` adds **12 modules / 467,258 bytes**. The test suite is
**63 files / 50,127 lines** (12.42 MB).

---

## 3. Core modules

### 3.1 The layer contract

Baseline **v1.1** admits six layers plus one external host. Imports are enforced by three
deterministic rules (`unknown-layer`, `forbidden-layer-import`,
`sqlite-outside-infrastructure`) that the validator applies to the source model; a measured
run over the scanned tree reports **compliant = True, 0 violations, 71 modules, 3 rules**.

| Layer | Package | May import | Responsibility |
| --- | --- | --- | --- |
| `domain` | `architecture_assistant.domain` | stdlib only | dependency-free state: enums, frozen dataclasses, transition tables, audit model |
| `ports` | `architecture_assistant.ports` | domain | `Protocol` contracts: repositories, storage, transactions, capabilities |
| `architecture` | `architecture_assistant.architecture` | domain | baseline declaration, source scanner, deterministic rules, validator |
| `application` | `architecture_assistant.application` | domain, ports | use cases; no SQLite, no network, no UI |
| `infrastructure` | `architecture_assistant.infrastructure` | domain, ports | concrete adapters; the **only** layer allowed to import `sqlite3` |
| `composition` | `architecture_assistant.composition` | every layer | wiring and configuration only - no policy |
| host | `architecture_assistant_gui` | the assistant's public API | presentation only; outside the scanned tree, so it cannot change the baseline |

### 3.2 `domain` - dependency-free state

| Module | Purpose | Depends on | Key names |
| --- | --- | --- | --- |
| `enums.py` | deterministic enumerations; every enum is a `StrEnum`, so members serialize to plain strings while staying real enums | stdlib | `Phase`, `Mode`, `RiskLevel`, `Severity`, `StepState`, `StepEvent`, `TaskState`, `TaskEvent`, `ReportStatus`, `ACRStatus`, `ADRStatus`, `RiskStatus`, `DecisionStatus`, `ProposalStatus`, `DeliberationStatus`, `DeliberationStage`, `DeliberationStageStatus`, `Round2Decision`, `SupervisorAction`, `SupervisorRisk`, `SupervisorStatus` |
| `fsm.py` | the two deterministic machines: `StepStateMachine` (the authoritative Assistant <-> Cline workflow, incl. `WAITING_APPROVAL`, `REVISE`, `FAILED` retries and the terminal `VERIFIED`/`ABORTED`) and `TaskStateMachine` (a subordinate dispatch marker that never drives a Step transition) | stdlib | `STEP_TRANSITIONS`, `TASK_TRANSITIONS`, `STEP_TERMINAL_STATES`, `TASK_TERMINAL_STATES`, `FiniteStateMachine`, `StepStateMachine`, `TaskStateMachine`, `InvalidTransitionError` |
| `models.py` | the frozen domain models with explicit `__post_init__` validation and deterministic `to_dict`/`from_dict` | stdlib | `DomainModel`, `Project`, `Step` (incl. `can_retry`), `Task`, `ArchitectureVersion`, `ArchitectureChangeRequest`, `ArchitectureProposal`, `DeliberationRun`, `DeliberationArtifact`, `ADR`, `Risk`, `Finding`, `Decision`, `SupervisionRecord`, `utc_now` |
| `audit.py` | the append-only audit model; one entry carries entity type, entity id, action, a JSON detail mapping and a timestamp | stdlib | `AuditEntityType`, `AuditAction`, `AuditEntry` (`detail_json`), `_as_enum` |

### 3.3 `ports` - the interfaces

| Module | Purpose | Depends on | Key names |
| --- | --- | --- | --- |
| `transactions.py` | the single transaction-boundary contract: application services group several adapter writes into one atomic unit without importing SQLite | stdlib | `TransactionPort.transaction()` |
| `storage.py` | the typed composition boundary: 13 repository accessors plus the inherited transaction boundary; adds no behaviour and never defines competing transaction semantics | `repositories`, `transactions` | `StoragePort` |
| `repositories.py` | the persistence boundary as structural protocols, one per aggregate | domain | `TaskKey`, `ProjectRepository`, `StepRepository`, `TaskRepository`, `ArchitectureVersionRepository`, `ArchitectureChangeRequestRepository`, `ArchitectureProposalRepository`, `SupervisionRepository`, `DeliberationRepository`, `ADRRepository`, `RiskRepository`, `FindingRepository`, `DecisionRepository`, `AuditRepository` |
| `capabilities.py` | provider-neutral capability contracts plus their DTOs (the largest port module, 1,707 lines) | domain | `WorkerPort`, `WorkerChannelPort`, `SupervisionChannelPort`, `RealizationCheckPort`, `RealizationControlPort`, `CanonicalBaseline`, `AdvisorPort`/`AdvisorQuery`, `JudgePort`/`JudgeConflict`, `CostPort`/`CostRecord`/`CostQuery`/`CostSummary`, `ReportingPort`, `NotifierPort`, `SynthesisPort`, `SupervisorPort`, `SupervisionGatePort`, `DeliberationAgentPort`, `DeliberationLeadPort`, `ConnectionStatus` |

### 3.4 `architecture` - what the architecture is and how it is enforced

| Module | Purpose | Depends on | Key names |
| --- | --- | --- | --- |
| `model.py` | immutable data types for the baseline and its validation results | stdlib | `SourceModel` (module -> imports), `RuleViolation` (`ordering_key`), `ValidationResult` (`compliant`), `ArchitectureRule`, `ArchitectureBaseline` (`rule_ids()`) |
| `rules.py` | the versioned baselines and the pure rule functions: v1.0 (frozen history) and v1.1 (adds the `composition` layer), plus `layer_of` | domain | `ARCHITECTURE_V1`, `ARCHITECTURE_V1_1`, `ARCHITECTURE_BASELINES`, `ARCHITECTURE_CURRENT`, `KNOWN_LAYERS`, `ALLOWED_IMPORTS`, `V1_1_RULES`, `UNKNOWN_LAYER`, `FORBIDDEN_LAYER_IMPORT`, `SQLITE_OUTSIDE_INFRASTRUCTURE`, `ROOT_PACKAGE` |
| `scanner.py` | reads every `*.py` below a root and builds a `SourceModel` with the stdlib `ast` module; relative imports are resolved to absolute module names | stdlib | `scan_directory`, `discover_python_files`, `extract_imports`, `build_source_model`, `module_name_for` |
| `validator.py` | runs every rule of a baseline over a source model and returns the deterministically ordered violations | `model`, `rules` | `ArchitectureValidator(baseline).validate(source)` |

Note: `scan_directory` is the only filesystem-facing function of the layer and it is **purely
functional per call** - no cache, no reuse. The composition-layer
`ArchitectureRealizationAdapter` re-scans on every `check` (measured cost in PERFORMANCE.md).

### 3.5 `application` - the use cases (28 modules)

The application layer owns policy: the loop, the approval policy, the review policy, the
evidence pipeline, the gates and every human write path. It imports `domain` and `ports` only.

| Module | Purpose | Key classes / functions |
| --- | --- | --- |
| `orchestrator.py` (672 lines) | the event-driven Assistant <-> Cline loop: `run_once()` performs **at most one** persisted transition of the current step, routes the persisted state to exactly one handler, and returns a `TickResult` that says exactly why the loop did or did not progress | `LoopStatus`, `TickResult`, `Orchestrator`, `LoopInvariantError` |
| `scheduler.py` | owns the *time* dimension: chains `run_once()` until the loop must wait, then returns - no polling loop, no `sleep`, no thread | `Scheduler.run_until_idle()`, `Scheduler.health()`, `SchedulerRun`, `LoopHealth`, `DEFAULT_MAX_ITERATIONS = 64` |
| `loop_policy.py` | the two pure policy functions of the loop: `requires_approval(mode, risk, requires_human)` and `decide_review(report, attempt, max_attempts)` | `ReviewOutcome`, `requires_approval`, `decide_review` |
| `context.py` | builds the derived, read-only context snapshot for one step (targeted reads only: verified previous steps, accepted ADRs, open risks, the single current architecture version) | `ContextBuilder.build(step_no)`, `ContextSnapshot`, `ContextInvariantError` |
| `dispatch.py` | the single shared task publication path: build the task, publish the worker artifacts, then persist the task row and its audit entry | `build_task`, `publish_task` |
| `realization_control.py` | the fail-closed gate before `VERIFY`: asks one question, records the deterministic evidence, and can only block | `RealizationControlUseCase.gate(step_no, attempt)`, `RealizationControlError` |
| `reporting.py` | the single read-only projection of the project state: one immutable `ReportSnapshot` built through repository ports | `ReportBuilder.build()`, `ReportSnapshot`, `ProjectMissingError` |
| `monitor.py` | a pull-based, read-only selector layer over the projection; no thread, no timer, no alerting | `Monitor.to_dict()`, `current_step()`, `next_step()`, `blocking_steps()`, `open_risks()`, `architecture_version()` |
| `human_override.py` | the controlled *state-control* human write path: pause/resume, mode change, unblock/resolve/abort - one mutation + one audit entry per call, actor and reason mandatory | `HumanOverride`, `OverrideActorRequiredError`, `OverrideNoChangeError`, `OverrideTransitionError` |
| `approval.py` | the `APPROVE`/`REJECT` human decision for a step in `WAITING_APPROVAL`; separate module, separate ports, no reference to `HumanOverride` | `ApprovalGate.approve()/reject()`, `ApprovalTransitionError` |
| `plan_loader.py` (1,026 lines) | the controlled initial plan import: validates the whole file, refuses an already-loaded database, a foreign project/plan version or any unknown field, and writes all steps plus exactly one `PLAN`/`IMPORT` audit entry in one transaction | `PlanLoader.preview()/import_plan()`, `Plan`, `PlanStepSpec`, `PlanPreview`, `PlanImportResult`, `PlanFormatError`, `PlanConflictError` |
| `architecture_bootstrap.py` (799 lines) | formalises the accepted architecture as authoritative state (project, version, ADRs, risks): idempotent reconciliation that raises on any contradiction instead of repairing it | `ArchitectureBootstrap.seed()`, `AdrSpec`, `RiskSpec`, `BootstrapSummary`, `BootstrapConflictError` |
| `architecture_versioning.py` | the minimal, deterministic versioned update of the baseline (one current version, superseded versions kept as history) | `ArchitectureVersioning`, `VersioningSummary`, `ArchitectureVersioningConflictError` |
| `architecture_evolution.py` (757 lines) | the change-request lifecycle of the baseline: `propose -> approve -> reject -> apply`; applying supersedes the baseline and can register the risks it introduces | `ArchitectureEvolution`, `EvolutionSummary`, `LinkedChange`, `ArchitectureEvolutionNotApprovedError` |
| `adr_manager.py` | deterministic, versioned ADRs; every mutation is one ADR write plus exactly one audit entry in one transaction | `ADRManager.accept()/reject()/deprecate()/supersede()`, `InvalidSupersessionError` |
| `risk_manager.py` | the deterministic risk register with the same one-write-one-audit contract | `RiskManager`, `InvalidRiskStatusTransitionError` |
| `plugin_core.py` | a deterministic registry of named capability adapters (registry is not runtime policy: the default selection is explicit and deterministic) | `PluginRegistry`, `PluginBinding`, `PluginDefaultNotSetError` |
| `evidence_merger.py` (569 lines) | a lossless, conservative view over advisor observations with explicit relation semantics (`SUPPORTS`/`CONTRADICTS`/...); advisors never vote | `AdvisorObservation`, `ObservationOutcome`, `EvidenceConflict`, `EvidenceView`, `EvidenceIdentityCollisionError` |
| `decision_engine.py` | the single place where merged evidence and the deterministic architecture gate become one explainable `Decision`; the gate always wins | `DecisionResult`, `DecisionEngineError` |
| `judge.py` | the last resort of the evidence pipeline: resolves only genuine, explicit conflicts and never replaces the deterministic decision | `JudgeUseCase`, `JudgeJudgment`, `JudgeRun`, `JudgeError` |
| `architecture_review.py` (1,283 lines) | one explicit, advisory consultation: the three advisors answer the operator's question, the evidence merger and decision engine explain the result, the judge is consulted only for a structurally valid conflict; writes no workflow state | `ArchitectureReview`, `ArchitectureReviewResult`, `AdvisorReviewer`, `ReviewStage`, `JudgeOutcome` |
| `architecture_synthesis.py` (1,025 lines) | turns one review into a durable **managed-project** proposal: deterministic by default, provider-neutral when a synthesizer is configured, fails closed on content outside the closed contract | `ArchitectureSynthesis`, `PROPOSAL_CONTENT_FIELDS`, `SynthesisProviderError`, `SynthesisResponseError` |
| `proposal_approval.py` | the human decision on a managed-project proposal: `APPROVED` / `REJECTED` / `REVISION_REQUESTED`, each with actor and reason, each one status change plus one audit entry, each refused when the stored inputs no longer match the proposal fingerprint | `ProposalApproval`, `ProposalNotApprovableError`, `ProposalDecisionConflictError` |
| `architecture_deliberation.py` (3,053 lines) | the controlled two-round architecture review board: two architect seats and one lead chair, per-stage persistence of artifacts, stale-run refusal, cancel, and a proposal at the end | `ArchitectureDeliberation`, `DeliberationSeat`, `DeliberationChair`, `RunSnapshot`, `DeliberationStageOrderError`, `DeliberationStaleError` |
| `supervision.py` (2,513 lines) | advisory analysis of one worker report: writes the supervision record **before** the supervisor is asked (so a crash never loses the fact that an analysis was owed), resolves `ANALYSIS_PENDING` / `SEND_PENDING` on the next tick, and gates the authoritative review through `SupervisionGate` (read-only, two methods) | `Supervision`, `SupervisionGate`, `SupervisionAnalysis`, `WaitingFor`, `SupervisionDisabledError`, `SupervisionStaleError` |
| `supervisor_policy.py` | the deterministic send policy: allowlist/denylist that decides whether a directive may be published at all; the supervisor's own labels never authorize a send | `SendDecision`, `SendVerdict` |
| `supervisor_runtime.py` | the headless, tick-driven coordinator: one tick reconciles, looks at the current report and stops; an unchanged report is never analysed twice and nothing is written by a no-op tick | `SupervisorRuntime.tick()`, `RuntimeTick`, `RuntimeState` |
| `eventlog.py` | the log-event contract: one validated, sanitized, JSON-safe shape, with credential/header redaction, bounded messages and exception **type names** only | `build_event`, `sanitize_text`, `component_for`, `exception_reason`, `EVENT_LEVELS`, `LOG_COMPONENTS` |

### 3.6 `infrastructure` - the concrete adapters

| Module | Purpose | Key names |
| --- | --- | --- |
| `sqlite.py` | **the only place that opens a SQLite connection**: resolves the path (creating the parent directory), sets `row_factory = sqlite3.Row`, enforces `PRAGMA foreign_keys = ON`, runs the migration runner, and provides the explicit transaction boundary | `open_database`, `close_database`, `apply_migrations`, `applied_versions`, `SqliteTransactionPort` (`BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`, nestable), `DEFAULT_DATABASE_PATH = data/architecture_assistant.db` |
| `migrations.py` (616 lines) | the versioned, deterministic schema: 7 migrations applied in one transaction each, with the `schema_migrations` bookkeeping row written inside the same transaction | `MIGRATIONS`, `Migration`, `BOOTSTRAP_SCHEMA_SQL`, table-name constants (`AUDIT_TABLE_NAME`, `CHANGE_REQUEST_TABLE_NAME`, `COST_TABLE_NAME`, `PROPOSAL_TABLE_NAME`, `SUPERVISION_TABLE_NAME`, `DELIBERATION_RUN_TABLE_NAME`, `DELIBERATION_ARTIFACT_TABLE_NAME`) |
| `repositories.py` (1,515 lines) | one adapter class per aggregate, all sharing one connection and the private `_SqliteRepository` helpers; writes use `INSERT ... ON CONFLICT(...) DO UPDATE` (never `INSERT OR REPLACE`), so row identity is stable | `SqliteProjectRepository`, `SqliteStepRepository`, `SqliteTaskRepository`, `SqliteArchitectureVersionRepository`, `SqliteArchitectureChangeRequestRepository`, `SqliteArchitectureProposalRepository`, `SqliteADRRepository`, `SqliteRiskRepository`, `SqliteFindingRepository`, `SqliteDecisionRepository`, `SqliteAuditRepository`, `SqliteSupervisionRepository`, `SqliteDeliberationRepository` |
| `storage.py` | the `StoragePort` facade: 13 accessors plus the reused transaction boundary; zero business logic | `SqliteStorage` |
| `cline.py` (556 lines) | the Cline file-channel worker adapter: task/context/report/directive files under the exchange directory, published atomically (temp file + `flush` + `os.fsync` + `os.replace`) and acknowledged by moving the report into `from_cline/archive/` | `ClineWorkerAdapter.dispatch()/read_report()/read_report_bytes()/acknowledge_report()/publish_directive()/read_directive()`, `TaskDispatchError`, `ReportMismatchError`, `ReportParseError` |
| `_http.py` | the private, provider-neutral transport and cost primitives shared by every provider adapter: DTOs, the `urllib` transport, retry *classification*, body redaction, primitive validators, the deterministic finding-id helper and the price model | `DEFAULT_TIMEOUT_SECONDS = 30.0`, `DEFAULT_MAX_RETRIES = 2`, `DEFAULT_BACKOFF_SCHEDULE = (0.0, 1.0, 2.0)`, `is_retryable_status`, `urllib_transport`, `HttpRequest`, `HttpResponse`, `HttpTransportError` |
| `cost.py` | the idempotent SQLite cost plugin: every provider adapter reports its own telemetry through `CostPort`; event identity is derived from the provider's stable response id | `SqliteCostPlugin` |
| `openai.py` / `claude.py` / `grok.py` / `deepseek.py` (815/871/872/868 lines) | the four advisor adapters (implementation analyst, critical reviewer, challenger, added independent perspective), each behind `AdvisorPort` with its own retry policy, redaction and ABSTAIN semantics; a missing key means "unavailable", never a crash | `*AdvisorAdapter`, `*AdvisorError`, `*AdvisorAbstainError`, `*MissingApiKeyError` |
| `openai_judge.py` (812 lines) | the evidence-judge adapter (`JudgePort`): consulted only for an explicit conflict, returns ABSTAIN/verdict and never overrides the deterministic decision | `OpenAIJudgeAdapter` |
| `deliberation_lead.py` (810 lines) | the deliberation seats over the same transport: one architect agent adapter and one chair/lead adapter | `DeliberationAgentAdapter`, `DeliberationLeadAdapter`, `DeliberationHttpError` |
| `disabled.py` | the real "no provider" `AdvisorPort`: abstains on every question **without any provider call** | `DisabledAdvisorAdapter` |
| `scripted_supervisor.py` | the offline, deterministic `SupervisorPort` double used by the pilot: constructing it opens no connection and reads no key, so the whole supervision lifecycle runs with no provider at all | `ScriptedSupervisor` |
| `_reporting.py` | the shared primitives of the two reporting adapters | helper functions only |
| `excel_reporting.py` (16 KB) | renders the canonical snapshot into a read-only `.xlsx` artifact using `zipfile` + `xml` (no third-party spreadsheet library) | `ExcelReportingAdapter.render()` |
| `markdown_reporting.py` (17 KB) | renders the same snapshot into a `.md` artifact | `MarkdownReportingAdapter.render()` |

Schema facts (measured on `data/architecture_assistant.db`): `journal_mode = delete`,
`page_size = 4096`, 10 explicit indexes, 15 tables, no foreign key other than
`tasks.step_no -> steps.step_no`, and `UNIQUE (step_no, attempt)` on `tasks`. The audit trail
has an index on `(entity_type, entity_id)` only - there is no index on the append order column
`id`, and no `LIMIT`-based tail query (see PERFORMANCE.md).

### 3.7 `composition` - the single wiring point

| Module | Purpose | Key names |
| --- | --- | --- |
| `root.py` (995 lines) | the only place that knows every layer: opens the database, reconciles the baseline and the declared changes, instantiates every adapter and hands the wired graph to the caller. Strict rule: wiring and configuration only - no `if` about the workflow. It never imports `sqlite3` (the connection is typed `Any`). | `CompositionConfig` (19 fields incl. `database_path`, `exchange_dir`, `report_dir`, `source_root`, `mode`, `timeout`, `max_iterations`, `supervision`, `supervisor_poll_interval`, `malformed_stability`, `malformed_timeout`, `provider_settings_path`), `Composition` (~28 attributes), `compose(config)`, `canonical_baseline()`, `baseline_v1()/baseline_v1_1()`, `DEFAULT_SOURCE_ROOT = src/architecture_assistant`, `DEFAULT_REPORT_DIR = reports/`, the log-event helpers (`build_event`, `sanitize_text`, `component_for`, `exception_reason`), the deliberation action verbs (`ACTION_RUN_ROUND1`, `ACTION_GENERATE_LEAD_REVIEW`, `ACTION_RUN_ROUND2`, `ACTION_GENERATE_FINAL_SYNTHESIS`, `ACTION_GENERATE_PROPOSAL`, `ACTION_CANCEL`, `DELIBERATION_ACTIONS`), `apply_provider_settings` |
| `realization.py` | the seam where the validator meets the filesystem: *expected* baseline vs *actual* source tree; scans fresh on every `check` (no cache), turns each violation into one `Finding` with `confidence=1.0` and one `Decision` with no `perspectives`, and idempotently encodes `step_no`+`attempt`+content hash in the ids | `ArchitectureRealizationAdapter`, `VALIDATOR_SOURCE = "architecture-validator"` |
| `evidence.py` | the wiring seam that converts provider adapter outcomes into provider-neutral observations (timeouts, rate limits and abstains included) | `observe`, `ABSTAIN_ERRORS`, `ADVISOR_ERRORS`, `ABSTAIN_REASON_PREFIX`, `ERROR_REASON_PREFIX` |
| `evolution.py` | the code-side change catalogue declared in the repository (v1.0 -> v1.1 and the ADR/risk it introduced) plus the idempotent reconciliation of that catalogue into persisted state | `DeclaredChange`, `ReconciliationSummary`, `DECLARED_CHANGES`, `V1_1_ACR_ID`, `reconcile_declared_changes`, `change_v1_0_to_v1_1`, `build_evolution`, `canonical_versions` |
| `advisor_factory.py` | the one factory that turns plain configuration into advisor instances; the panel knows no adapter class | `AdvisorFactory`, `assemble_reviewers`, `assemble_deliberation`, `create_deliberation_agent`, `create_deliberation_lead`, `probe_connection`, `probe_deliberation`, `provider_catalog`, `PROVIDER_DEFAULT_MODELS` |
| `provider_settings.py` | the shape of the operator's local preference file (`data/provider_settings.json`): per-advisor provider/model/key, atomic save, masked keys, and a documented "invalid" status instead of a crash | `ProviderSettings`, `ProviderSelection`, `ProviderSettingsLoad`, `load_provider_settings`, `save_provider_settings`, `settings_from_mapping`, `MASKED_KEY`, `SUPPORTED_PROVIDERS`, `ADVISOR_KEYS`, `DEFAULT_PROVIDER_BY_ADVISOR` |

`compose()` steps, in order: (1) open and migrate SQLite, (2) reconcile the architecture
bootstrap and the declared changes, (3) build storage, the Cline worker adapter, the context
builder, the realization adapter and the mandatory realization-control gate, (4) build the
supervision trio (`SupervisionGate`, `ScriptedSupervisor`, `Supervision`) and the
`SupervisorRuntime`, (5) wire the `Orchestrator` and `Scheduler`, (6) build the optional
capabilities (advisors, judge, reporting, monitor, approval, override, plan loader, review,
synthesis, proposal approval, deliberation), (7) read the provider settings file exactly once.
Composing a fresh database measured **0.042 - 0.078 s**; importing the `composition` package
measured **0.50 - 0.85 s** (see PERFORMANCE.md).

### 3.8 `architecture_assistant_gui` - the operator panel (external host)

The panel is deliberately **outside** the scanned tree, so it cannot change the architecture
baseline. It depends on the assistant in exactly three modules: `core.py`, `controller.py`
and `app.py`. `app.py` additionally imports `architecture_assistant.domain.enums` (for the
`--mode` CLI choices) - the only direct inner-layer import of the host.

| Module | Purpose | Key names |
| --- | --- | --- |
| `__main__.py` | the module entry point: `python -m architecture_assistant_gui` -> `app.main(sys.argv[1:])` | - |
| `app.py` (1,202 lines) | Tk application wiring: the window, dialogs and the single result pump; also owns the two local preferences (remembered actor, remembered layout). Tk is imported lazily so a missing Tk installation is reported cleanly. | `GuiApp` (`run()`, `_pump`, `_render`, `_on_action`, `_remember_layout`, `_maybe_tick_supervisor`), `main`, `parse_args`, `build_config`, `load_actor`, `save_actor`, `POLL_INTERVAL_MS = 100`, `SUPERVISOR_TICK_MS = 2000`, `SETTINGS_PATH = ~/.architecture_assistant_gui.json`, `LAYOUT_PATH = data/gui_layout.json` |
| `core.py` (1,357 lines) | the core-thread worker: `CoreWorker` is a synchronous facade over one composition (callable only from its own thread) and every public method returns plain data; `BackgroundRunner` owns the thread, the job queue and the result queue | `CoreWorker` (`open/close/reconnect`, `payload()`, `audit_tail(limit=200)`, provider settings, review, deliberation, proposal, supervision, plan, exports), `BackgroundRunner` (`start`, `submit`, `poll`, `progress`, `events`, `stop`), `JobResult`, `ErrorReport`, `AUDIT_TAIL_LIMIT = 200`, `PROGRESS_LIMIT = 64`, `EVENT_LIMIT = 400`, `_CRITICAL_ERROR_NAMES` |
| `controller.py` (4,610 lines) | the UI state machine: what the operator sees and which button may run; holds no core object and no Tk, imports no repository | `GuiController` (`submit`, `intent`, `enabled`, `apply_result`, `drain_progress`, `drain_events`, `view_model()`), `Intent`, `INTENTS` (56 entries), `POPUP_INTENTS`, `PROPOSAL_INTENTS`, `PROVIDER_SETTINGS_INTENTS`, `CLEAR_LOGS_INTENT`, `SAVE_SETTINGS_INTENT`, `LOG_LIMIT = 200` |
| `views.py` (2,200 lines) | Tk widgets: layout and rendering only; every string comes from the view model | `MainWindow` (extends `WorkspaceViews`; `render(view_model)`, `select_tab`, `clear_key_entries`, splitter and sash handling) |
| `workspace_views.py` | the guided workspace widgets (project brief, agent settings, discussion, architecture, plan, coding, progress) | `WorkspaceViews` |
| `workspace.py` | plain presentation helpers and editable drafts; **never** workflow mutations. Contains `execution_plan()`, which turns an approved proposal into an importable plan JSON draft in dependency order (and refuses a cycle or a >500-step draft) | `execution_plan`, `proposal_document`, `cline_handoff`, `readable`, `SEATS`, `BRIEF_TEMPLATE` |
| `windows.py` (939 lines) | the one window every popup uses: standard, resizable, non-modal, built from a plain-data spec | `PopupWindow`, `PopupSize`, `open_popup`, `confirm_dialog`, `POPUP_SIZES`, `POPUP_ACTION_DETAILS` |
| `theme.py` | one dark visual language shared by CustomTkinter and the remaining ttk widgets | `COLORS`, `FONT`, `configure`, `button`, `DarkButton` |
| `layout.py` | the remembered window layout in one small JSON file: presentation only, fail-safe, written atomically | `load_layout`, `save_layout`, `normalise_layout`, `parse_geometry`, `format_geometry`, `DEFAULT_LAYOUT_PATH`, `LAYOUT_VERSION = 1`, `MAX_POPUPS`, `MAX_SASHES` |
| `project_setup.py` | the "start / open a project" dialog and the per-project workspace spec: it derives a `.architecture_assistant/` state folder (`project.db`, `cline/`, `reports/`, `providers.json`, `workspace.json`) inside the chosen project folder and saves preferences atomically | `workspace_spec`, `save_preferences`, `show_project_setup` |

Measured UI facts: the widget tree has **642 descendant widgets**, the button matrix has
**56 intents**, and the canonical monitor payload is **25,572 characters** of JSON for 12
steps.

---

## 4. Data flow

### 4.1 Startup: from process start to a composed graph

```text
python -m architecture_assistant_gui
  -> __main__.py            raise SystemExit(app.main(sys.argv[1:]))
  -> app.main()             parse_args -> build_config -> (optional) --workspace JSON
                            -> GuiApp(config, actor=...)
  -> GuiApp.run()           read the remembered layout (data/gui_layout.json)
                            -> ctk root + theme -> MainWindow (642 widgets)
                            -> apply remembered geometry/sashes -> root.mainloop()
                            -> BackgroundRunner.start():
                                 spawn ONE daemon thread "architecture-assistant-core"
                                 submit("open", worker.open)
  -> core thread            CoreWorker.open() -> compose(CompositionConfig)
                                 open SQLite + apply_migrations()
                                 ArchitectureBootstrap.seed()        (idempotent)
                                 reconcile_declared_changes()        (idempotent)
                                 build storage/worker/context/realization/supervision
                                 build Orchestrator + Scheduler + optional capabilities
                                 load_provider_settings() exactly once
  -> UI thread              root.after(100, _pump): drain progress, drain events,
                            poll results, re-render when anything arrived,
                            submit one supervision tick every 2000 ms (only when enabled)
```

There is no hidden work at startup: `compose()` opens the database, migrates it and reconciles
declared state; nothing in the workflow advances until an operator press (or a tick) is
submitted.

### 4.2 One loop tick (`Orchestrator.run_once`)

A tick performs **at most one** persisted transition. It reads the project and the step list,
derives the current step as *the lowest-numbered step that is not `VERIFIED`*, and routes the
**persisted** state (never an in-memory guess) to exactly one handler:

| Persisted step state | Tick action | Result |
| --- | --- | --- |
| `PENDING` | apply `PREPARE` with `attempt = max(attempt, 1)` | `TRANSITIONED` -> `READY` |
| `READY` | `requires_approval(mode, risk, requires_human)`; if true apply `REQUEST_APPROVAL`, else dispatch | `WAITING_APPROVAL` or `DISPATCHED` |
| `WAITING_APPROVAL`, `BLOCKED`, `CONFLICT` | wait (halting reasons) | `WAITING`, nothing written |
| `DISPATCHED` / `CLINE_WORKING` | check the deadline, then look for the report; **the report check runs before the deadline check** | `WAITING` or `REPORT_RECEIVED` |
| `REPORT_RECEIVED` | Gate A: the supervision gate must allow *this exact report*; then apply `START_REVIEW` | `REVIEWING` or `WAITING` |
| `REVIEWING` | re-read the report, `decide_review(...)` maps it to one FSM event; on `VERIFY` the mandatory realization gate runs **inside the same transaction** as the transition | `VERIFIED`, `BLOCKED`, `REVISE`, `FAILED`, `CONFLICT` |
| `REVISE` / `FAILED` | retry with the next attempt while budget remains, else block or abort | `READY` / `WAITING` / `ABORTED` |
| `ABORTED` | report the stop condition | `ABORTED` |

`Scheduler.run_until_idle()` chains these ticks until the loop returns something other than
`TRANSITIONED` (max 64 iterations per call). After an outcome is durable, the report is
acknowledged (moved to the archive) - never before, which is what makes a crash between the
two steps replay-safe.

### 4.3 Worker round trip (the file channel)

```text
assistant                       exchange dir (data/cline by default)
  build ContextSnapshot  --->   context/step_007_attempt_001_context.json   (temp + fsync + replace)
  dispatch task          --->   to_cline/step_007_attempt_001_task.json     (temp + fsync + replace, LAST)
  persist Task row + 1 audit entry in one transaction
  ... operator hands the task to Cline ...
                                 from_cline/step_007_attempt_001_report.json (written by Cline)
  tick: read_report(step, attempt)  read-only, validated structurally, identity checked
  review -> outcome persisted -> acknowledge_report(): os.replace into from_cline/archive/
  supervisor directive  --->     to_cline/step_007_attempt_001_directive.json
```

Ordering is the guarantee: the context is published *before* the task, so a worker can never
observe a task without its context; the report is archived only *after* the outcome is durable,
so a crash cannot lose a report.

### 4.4 Advisory review -> managed-project proposal

```text
operator question
  -> ArchitectureReview.run()
       -> observe() per advisor (OpenAI, Claude, Grok, optionally DeepSeek/Disabled)
       -> EvidenceMerger            (advisors never vote; relations are explicit)
       -> DecisionEngine            (the deterministic gate stays authoritative)
       -> JudgeUseCase              only for a structurally valid conflict
       -> cost telemetry per advisor (CostPort)
     => ArchitectureReviewResult (ephemeral: writes no workflow state)
  -> ArchitectureSynthesis.generate()   deterministic by default
     => ArchitectureProposal (DRAFT) + exactly one PROPOSAL/CREATE audit entry
  -> ProposalApproval.approve() / reject() / request_revision()
     => one status change + one audit entry in one transaction
```

An approved proposal describes the **managed project**. It never mutates the assistant's own
baseline: no `ArchitectureVersion`, no ACR, no assistant ADR or risk, no rule change, no
realization verdict.

### 4.5 Deliberation (two-round review board)

```text
Run Round 1            -> both architect seats answer independently  -> artifacts persisted per stage
Generate Lead Review   -> the lead compares both answers             -> consensus / conflicts / questions
Run Round 2            -> each seat reconsiders, seeing the critique  -> artifacts persisted
Generate Final Synthesis -> the lead produces one architecture       -> treated as a review result
Generate Architecture Proposal -> DRAFT proposal + 1 audit entry
Cancel Deliberation    -> terminal status, history kept
```

Each stage is persisted as a `DeliberationArtifact` bound to its `DeliberationRun`; a stage
that is out of order, a stale run or a disabled seat fails closed
(`DeliberationStageOrderError`, `DeliberationStaleError`, `DeliberationLeadUnavailableError`).

### 4.6 Supervision (optional, off by default)

```text
tick (every 2 s, only when --supervision)
  -> SupervisorRuntime.tick()
       resolve a half-finished record left by a crash (ANALYSIS_PENDING / SEND_PENDING)
       look at the CURRENT report bytes of the current step/attempt
       unchanged report -> stop (no provider call, no context rebuild, no audit entry)
       new report -> write the supervision record FIRST, then ask SupervisorPort
  -> Gate A (REPORT_RECEIVED -> START_REVIEW) and Gate B (inside REVIEWING)
       an not-allowing record makes the loop WAIT and touches nothing at all
  -> human writes: Analyze Report / Approve & Send / Reject Directive / Waive / Escalate
       the core resolves the current report identity itself, so a stale panel decides nothing
  -> Approve & Send -> supervisor_policy decides (allowlist/denylist) -> publish a directive
```

With `supervision=False` the gate is not injected at all, so the loop is byte-for-byte the
pre-supervision loop, and the runtime tick is inert (`outcome: disabled`, `status: DISABLED`).

### 4.7 GUI <-> core (one thread, two queues)

```text
Tk thread                                   core thread (daemon, owns the composition)
  press -> controller.intent(key)             BackgroundRunner._run:
  runner.submit(label, lambda w: w.x())  -->    jobs.get() -> action(worker) -> results.put(JobResult)
     (refused while busy: one job at a time)   exceptions -> describe_error(...) -> JobResult(error=...)
  after(100ms) _pump() <-- runner.poll()      progress lines and log events travel on their own queues
     controller.apply_result(...)              every payload is plain dict/list/str/int/bool
     controller.view_model() -> MainWindow.render()
```

No core object ever crosses the queues: the Tk thread never sees a composition, a repository,
a Monitor or a SQLite handle. `submit` returns `False` while a job is running, so a click
during a long action is *dropped*, not queued.

### 4.8 Reporting and export

`ReportBuilder.build()` reads the project, steps, tasks, architecture versions, ADRs, risks,
findings, decisions, change requests, cost roll-up and loop health through repository ports
and returns one immutable `ReportSnapshot`. `ExcelReportingAdapter.render()` and
`MarkdownReportingAdapter.render()` turn the *same* snapshot into one `.xlsx` and one `.md`
artifact in `report_dir` (default `reports/`, deliberately separate from the worker exchange
directory). Reporting is a projection: never authoritative, never mutating.

---

## 5. External dependencies

### 5.1 `requirements.txt`

**There is no `requirements.txt` in this repository** (verified with a recursive search for
`requirements*.txt`; the file does not exist at any level). Dependency declarations live in
`pyproject.toml` only:

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "architecture-assistant"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["customtkinter>=5.2,<7"]

[project.optional-dependencies]
dev = ["pytest>=7.0"]

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
```

### 5.2 Imports actually present in the source (AST-extracted, 83 files)

| Bucket | Modules |
| --- | --- |
| third-party (**1**) | `customtkinter` (imported only by `architecture_assistant_gui`: `app.py`, `views.py`, `views`-companions, `windows.py`, `theme.py`, `project_setup.py`) |
| stdlib GUI | `tkinter` (incl. `ttk`, `filedialog`, `messagebox`) |
| stdlib (25) | `argparse`, `ast`, `collections`, `contextlib`, `dataclasses`, `datetime`, `enum`, `hashlib`, `json`, `os`, `pathlib`, `queue`, `re`, `socket`, `sqlite3`, `subprocess`, `sys`, `tempfile`, `threading`, `time`, `typing`, `urllib`, `xml`, `zipfile` |
| local | `architecture_assistant` |

Three consequences worth recording:

1. The core (`architecture_assistant`) is genuinely third-party-free: the only non-stdlib
   import in the whole tree is `customtkinter`, and it lives in the host package.
2. `README.md` section 8 states that there are "**no** third-party runtime dependencies" and
   that `pytest` is "the only declared optional dependency". That statement was true before
   the operator panel; the panel now requires `customtkinter`, and the `.bat` launchers
   additionally require `tkinter` (Tcl/Tk) in the chosen interpreter. The README sentence is
   therefore **stale relative to `pyproject.toml`**.
3. Windows launcher contract: `START_ARCHITECTURE_ASSISTANT*.bat` accept an interpreter only if
   it is Python `>=3.11`, can `import tkinter`, and can `import architecture_assistant_gui`
   with `PYTHONPATH=<repo>/src` - i.e. `customtkinter` must be importable too.

No lock file, no `pip-compile` output, no vendored wheels and no CI configuration were found.

---

## 6. Entry points

| Entry point | How it is started | What it does |
| --- | --- | --- |
| Operator panel (module) | `python -m architecture_assistant_gui [flags]` -> `architecture_assistant_gui/__main__.py` -> `app.main(argv)` | creates the CTk window, starts the single core thread, enters the Tk mainloop; returns an exit code (`0` normal, `3` when Tk is unavailable) |
| Operator panel (Windows) | double-click `START_ARCHITECTURE_ASSISTANT.bat` | detects a usable interpreter, sets `PYTHONPATH=%~dp0src`, starts `pythonw -m architecture_assistant_gui` with no console and forwards all arguments |
| Operator panel (debug) | `START_ARCHITECTURE_ASSISTANT_CONSOLE.bat` | identical detection, keeps the console and prints interpreter, launcher, `PYTHONPATH` and working directory, then waits for a key press |
| Core library | `from architecture_assistant.composition import CompositionConfig, compose` | the documented programmatic path: `compose(config)` -> `composition.run_until_idle()` / `composition.health()` / `composition.close()` |
| Plan import | `composition.plan_loader.preview(text)` then `.import_plan(text, actor=..., reason=..., source_file=...)` | the only write path a plan has |
| Human writes | `composition.human_override`, `composition.approval`, `composition.proposal_approval`, `composition.supervision` | each requires an explicit actor and a non-empty reason |
| Read-only inspection | `composition.monitor` | pull-based selectors over the canonical projection |
| Test suite | `python -m pytest -q` (config already in `pyproject.toml`) | `testpaths = ["tests"]`, `pythonpath = ["src"]` |

Panel CLI flags (`app.parse_args`): `--database`, `--exchange-dir`, `--report-dir`,
`--source-root`, `--project-name`, `--plan-version`, `--mode {MANUAL,SUPERVISED,AUTO}`,
`--supervision`, `--actor`, `--workspace <json>`. Values not given on the command line fall
back to the optional `--workspace` JSON and then to `CompositionConfig` defaults. **There is no
CLI entry point for the core loop** - the assistant is driven programmatically or through the
panel, by design (README section 11).
