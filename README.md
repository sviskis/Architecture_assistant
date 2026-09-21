# Architecture Assistant

A Python architecture lifecycle assistant that manages software-development
workflow using deterministic architecture rules, SQLite-persisted state,
auditable workflow transitions, worker/advisor adapters, reporting, and
crash/restart recovery.

---

## 1. Overview

Architecture Assistant drives one project through a persisted, step-by-step
workflow and keeps every decision traceable.

- **SQLite is the runtime source of truth.** Project, steps, tasks,
  architecture versions, ADRs, risks, findings, decisions, change requests,
  audit entries and cost records live in a file-backed SQLite database. Nothing
  about "where the loop is" is kept in memory, so a restart resumes exactly
  where the process stopped.
- **A finite state machine controls the workflow.** Every transition is a pure
  lookup in an authoritative transition table. One loop tick performs at most
  one transition, and a transition is persisted together with exactly one audit
  entry in a single transaction.
- **Architecture rules and realization control are authoritative.** A
  deterministic validator checks the real source tree against the persisted
  architecture baseline. `VERIFIED` is reachable only when the realization gate
  reports compliance, and a deterministic violation can never be out-voted.
- **Cline acts as a worker, not a decision-maker.** The worker receives a task
  and a context file, executes the step and returns a structured report. It does
  not decide step state, architecture compliance or outcomes.
- **AI advisors are advisory, not authoritative.** OpenAI, Claude and Grok
  advisors produce findings for a caller; the evidence merger and decision
  engine keep the deterministic gate authoritative and never let advisors vote.
  The judge is consulted only for an explicit evidence conflict.
- **Human actions are audited.** Every human command requires an explicit actor
  and a non-empty reason, and performs exactly one mutation plus exactly one
  audit entry inside one transaction.

---

## 2. Core Architecture

Six layers exist; imports are enforced by deterministic architecture rules.

| Layer | Package | Responsibility |
| --- | --- | --- |
| `domain` | `src/architecture_assistant/domain` | Dependency-free state: enums, frozen dataclasses, the transition tables, audit model |
| `ports` | `src/architecture_assistant/ports` | Protocols for storage, repositories, transactions, capabilities (worker, cost, realization, reporting, judge) |
| `architecture` | `src/architecture_assistant/architecture` | Source scanner, deterministic rules and validator for the declared baseline |
| `application` | `src/architecture_assistant/application` | Use cases: orchestrator, scheduler, loop policy, context builder, realization control, bootstrap, versioning, evolution, evidence merger, decision engine, judge use case, reporting projection, monitor, human override, approval gate |
| `infrastructure` | `src/architecture_assistant/infrastructure` | SQLite storage, repositories, migrations, Cline file channel, provider adapters, cost plugin, Excel and Markdown renderers |
| `composition` | `src/architecture_assistant/composition` | The single wiring point: opens the database, reconciles the baseline and declared changes, builds the object graph |

Architecture baseline **v1.1** declares three deterministic rules that every
validation run applies to the source tree:

- `unknown-layer` - every module must live in a known layer;
- `forbidden-layer-import` - imports must follow the allowed direction;
- `sqlite-outside-infrastructure` - database access stays in `infrastructure`.

The composition layer exists because baseline v1.1 admitted it; the wiring
itself stays wiring and configuration only.

---

## 3. Main Capabilities

All capabilities below are implemented in `src/architecture_assistant` and
covered by tests.

- **Persisted architecture versions** - the baseline is state: one current
  version, with the superseded versions kept as history.
- **ADR and risk handling** - formal decisions and the risk register are
  reconciled seed data and can be amended or superseded through their managers.
- **Architecture evolution** - change requests move through a persisted
  `propose -> approve -> reject -> apply` lifecycle; applying a change
  supersedes the baseline and can register the risks it introduces.
- **Cline worker file channel** - task dispatch, report collection and
  acknowledgement over files, with atomic publication and at-least-once
  delivery.
- **OpenAI, Claude and Grok advisors** - provider-specific adapters that return
  findings behind provider-neutral ports; response and error text is redacted.
- **Evidence Merger / Decision Engine** - provider-neutral merging of advisor
  observations with explicit relation semantics; the deterministic gate stays
  authoritative and advisors never vote.
- **Judge adapter** - a separate capability consulted only for an explicit,
  otherwise unresolvable evidence conflict; its decision never replaces the
  deterministic one.
- **Cost tracking** - an idempotent accounting store keyed by the stable
  identity of a billable provider event; a conflicting replay fails closed.
- **Excel reporting** - a workbook rendered from the reporting projection.
- **Markdown reporting** - a Markdown document rendered from the same payload.
- **Read-only Monitor** - a pull-based view over the canonical projection, with
  no write path reachable from it.
- **Human Override** - the state-control human write path:
  `pause_project` / `resume_project` / `set_mode` plus the human step events
  `unblock_step` / `resolve_step` / `abort_step`.
- **ApprovalGate** - the approval human write path: `approve` and `reject` for a
  step that waits for a human decision.
- **Crash/restart recovery** - recovery from persisted state only, proven with
  real subprocess crashes, mid-transaction crashes and a full close/recreate of
  a file-backed database.

---

## 4. Workflow Model

A project is a numbered plan of steps. The current step is always the
lowest-numbered step that is not `VERIFIED`; a later step never becomes current
before an earlier one is verified.

1. **Preparation** - a pending step becomes ready, carrying a 1-based attempt
   number.
2. **Approval policy** - a pure function of operating mode, step risk and an
   explicit human requirement decides whether the step is dispatched
   automatically or must wait for a human decision. `MANUAL` always waits;
   `SUPERVISED` waits above `LOW`; `AUTO` waits above `MEDIUM`; an explicit
   human requirement always waits.
3. **Dispatch** - the context is built and the task artifacts are published to
   the file channel first; only then is the dispatch persisted with the task
   row and its audit entry. A persisted dispatch therefore never exists without
   its published task.
4. **Report collection** - the loop checks for the worker report on every tick,
   before the deadline check, so a report that arrived in time is never
   discarded by a timeout.
5. **Review** - the received report is mapped deterministically onto a review
   event. Architecture questions and unresolved issues are always a human
   decision; otherwise the report status decides.
6. **Realization gate** - before anything may reach `VERIFIED`, the source tree
   is scanned and validated against the authoritative baseline; a violation
   downgrades the outcome to `BLOCKED`.
7. **Outcomes** - verification on success, a bounded retry loop for revision and
   worker failure, blocking for human decisions, conflict resolution, or abort.
8. **Report consumption** - the outcome is persisted first; only then is the
   report acknowledged (archived). A crash before that point keeps the report on
   disk and the persisted state authoritative.

`REVISE` and `FAILED` are owned by the loop: they retry while attempts remain.
At an exhausted attempt budget no retry is attempted - the review policy
escalates the outcome to blocking, and a step left in `FAILED` without budget
aborts. `WAITING_APPROVAL`, `BLOCKED` and `CONFLICT` halt the loop and wait for
a human.

---

## 5. Crash / Restart Guarantees

The design principle is: **the process may die, the state must survive.** The
guarantees are stated precisely - they are *crash-safe*, *replay-safe* and
*fail-closed*, and they are deliberately **not** described as one atomic
transaction, because filesystem publication and a database commit cannot form
one.

- **File-backed SQLite.** The runtime database is a real file
  (`data/architecture_assistant.db` by default). In-memory databases are used by
  unit tests only, never by the recovery proofs.
- **Transactional state changes.** One shared connection with an explicit
  `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK` boundary; a step transition, its
  task row and its audit entry are one commit or one rollback.
- **At-least-once report handling.** Reading a report is read-only and
  repeatable. The outcome is persisted first and the report is acknowledged
  afterwards, so a crash before the acknowledgement can never lose or
  re-process a result.
- **Restart from persisted state.** Composing the assistant opens and migrates
  the database and reconciles declared state idempotently; the loop resumes from
  the persisted step, not from anything held in memory.
- **Subprocess crash tests.** Real child processes are killed with
  `os._exit(1)`, asserting the crash return code and a deliberate crash marker;
  both the workflow scenarios and the approval scenarios are covered, including
  a full close/recreate of the composition over the same database and files.
- **Mid-transaction crash tests.** A process is killed between the two writes of
  one transaction (after the domain write, before the audit write); afterwards
  the database shows only the previously committed state.
- **Replay and idempotence.** Task rows are unique by `(step_no, attempt)`,
  cost records are unique by `event_id`, and replaying the same report after a
  restart adds no duplicate task, cost, audit or architecture side effect.

---

## 6. Approval Workflow

v1.0 includes an explicit approval path, `ApprovalGate`
(`application/approval.py`), for the two human decisions the authoritative
transition table defines for a step that waits for approval:

- `approve` applies `APPROVE` (`WAITING_APPROVAL -> DISPATCHED`). It validates
  the transition first, publishes the same deterministic task artifacts the loop
  publishes, then writes one task row and exactly one audit entry in one
  transaction.
- `reject` applies `REJECT` (`WAITING_APPROVAL -> READY`). It writes the step and
  exactly one audit entry, publishes nothing and touches no task; the loop then
  evaluates the approval policy again on its next tick.

`ApprovalGate` is **separate from `HumanOverride`**: different module, different
ports, no reference to each other. `HumanOverride` is the state-control path
(pause/resume, mode, unblock/resolve/abort); `ApprovalGate` is the approval
decision. Both require an explicit actor and a non-empty reason, and both audit
their act.

The gate cannot reach `VERIFIED`, cannot render or override a realization
verdict, cannot bypass the transition table, cannot force an arbitrary step
state, cannot write an architecture version, ADR or change request, cannot
perform an override operation, and owns no monitor or reporting behaviour. An
illegal or repeated approval fails closed and writes (and publishes) nothing.

Crash windows around an approval are covered by tests: death before publication,
death after publication but before the commit, a clean commit, a repeated
approval after success, and a full restart on a file-backed database.

---

## 7. Reporting

- **`ReportSnapshot` is the canonical read-only projection.** `ReportBuilder`
  reads the project, steps, tasks, architecture versions, ADRs, risks, findings,
  decisions, change requests, cost roll-up and loop health through repository
  ports and returns one immutable snapshot. Reporting is a projection: it is
  never authoritative and never mutates state.
- **Excel and Markdown exporters** render the same snapshot into artifacts in
  the configured report directory (`reports/` by default), which is deliberately
  separate from the worker exchange directory. One snapshot, two artifacts.
- **The Monitor is read-only.** It is a pull-based selector layer over the
  projection and is handed only the report builder - no storage, no
  transaction, no cost port, no repository - so no write path is reachable from
  it. It exposes the current step, step states, blocking steps, architecture
  version, open risks, change requests and the cost summary. There is no thread,
  timer, polling loop or alerting in it; a caller asks for state when it wants
  it.

---

## 8. Installation / Development

**Requirements.** Python `>=3.11` (declared in `pyproject.toml`). There are **no
third-party runtime dependencies**: the package uses the standard library only
(`sqlite3`, `urllib`, `zipfile`, `ast`, `json`, ...). `pytest` is the only
declared optional dependency.

**Environment and install** (run from the repository root):

```console
python -m venv .venv
# Windows
.venv\Scripts\activate
# POSIX
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -e .            # runtime package (setuptools, src layout)
python -m pip install -e ".[dev]"     # adds pytest
```

**Run the tests:**

```console
python -m pytest -q
```

`pyproject.toml` already configures `testpaths = ["tests"]` and
`pythonpath = ["src"]`, so the suite runs without installing the package first.

**Runtime locations** (both are ignored by `.gitignore`):

| Path | Purpose |
| --- | --- |
| `data/architecture_assistant.db` | SQLite source of truth |
| `data/cline/` | Cline file channel (`to_cline/`, `context/`, `from_cline/`, `from_cline/archive/`) |
| `reports/` | Excel and Markdown artifacts |

**Driving the loop.** v1.0 has no CLI by design (see limitations). The operator
composes the object graph and asks the scheduler to advance:

```python
from architecture_assistant.composition import CompositionConfig, compose

composition = compose(CompositionConfig())
try:
    run = composition.run_until_idle()
    print(run.stopped_because, composition.health())
finally:
    composition.close()
```

The plan itself is seeded through the storage port
(`composition.storage.steps.upsert(...)`); there is no plan-ingestion UI in v1.
Human commands are issued through `composition.human_override` (state control)
and `composition.approval` (approve / reject), and `composition.monitor` reads
state without changing it.

---

## 9. Test Status

Architecture Assistant v1.0.0 final validation:

- 2428 passed / 0 failed (`python -m pytest -q`)
- architecture self-check: baseline **1.1**, compliant **True**, **0**
  violations, **56** modules, **3** architecture rules

These are the **v1.0.0 freeze results** recorded for the release commit. They are
a snapshot of that validation run, not a live guarantee: any later change to the
source, tests, dependencies or environment can change them, and the architecture
self-check has to be re-run to confirm compliance on the current tree.

---

## 10. V1.0 Scope

- **One controlled project per database.** The storage facade requires exactly
  one project and fails closed on zero or several.
- **Python-first architecture analysis.** The realization gate scans and
  validates a Python source tree; the default source root is the assistant's own
  package, and the root is injected configuration, so another Python tree can be
  validated without code changes.
- **File-backed SQLite** as the only runtime source of truth, together with the
  worker file channel and the report directory.
- **Operator-supervised pilot usage.** A human seeds the plan, drives the loop,
  approves or rejects gated steps, unblocks or resolves blockers, may pause and
  resume the project, and never edits the database directly.

---

## 11. Known V1 Limitations

- **The AI evidence-conflict capability is not wired into the automatic loop.**
  The evidence merger, the decision engine and the judge are implemented and
  tested, but no code path raises the `CONFLICT` state, so the deterministic
  architecture rules remain the only verdict and no advisor can influence a
  step in v1. (The approval gap that earlier reviews tracked as AQ-2 is **not**
  an open limitation: v1.0 resolves it with `ApprovalGate`.)
- **No CLI, daemon or supervisor implementation.** The loop is invoked through
  the composition API; process restart and supervision are outside the product.
- **No plan-ingestion UI or importer.** The step plan is seeded through the
  storage port.
- **No JSX analyzer yet.** The realization gate validates the Python source
  tree; language-specific analyzers are out of scope for v1.
- **A crashed acknowledgement can leave an orphan report (F-1).** If the process
  dies between the durable outcome and the report acknowledgement, the report
  remains in `from_cline/` while the authoritative outcome is already committed
  and can never be replayed into another outcome. The documented procedure is to
  inspect `from_cline/` after a restart or completion and, when the matching
  step is already `VERIFIED` or `ABORTED`, archive that file manually.
- **The retry attempt counter is not a physical dispatch-count metric.** It
  counts retries: after a human unblock at an exhausted attempt budget the same
  attempt is dispatched again. That re-dispatch is operator-controlled and never
  automatic; at an exhausted budget the recommended actions are pause or abort
  rather than repeating the unblock.

---

## 12. Repository Structure

```text
Architecture_assistant/
├── pyproject.toml                 packaging, pytest configuration
├── .gitignore                     ignores caches, .mini_build/, data/, reports/, *.db
├── README.md                      this document
├── src/
│   └── architecture_assistant/
│       ├── __init__.py            package metadata
│       ├── domain/                enums.py  models.py  fsm.py  audit.py
│       ├── ports/                 storage.py  repositories.py  transactions.py
│       │                          capabilities.py
│       ├── architecture/          model.py  scanner.py  rules.py  validator.py
│       ├── application/           orchestrator.py  scheduler.py  loop_policy.py
│       │                          context.py  realization_control.py
│       │                          architecture_bootstrap.py
│       │                          architecture_versioning.py
│       │                          architecture_evolution.py  adr_manager.py
│       │                          risk_manager.py  plugin_core.py
│       │                          evidence_merger.py  decision_engine.py  judge.py
│       │                          reporting.py  monitor.py  human_override.py
│       │                          approval.py  dispatch.py
│       └── composition/           root.py  realization.py  evidence.py  evolution.py
└── tests/
    ├── test_fsm.py  test_models.py  test_capabilities.py  test_storage_port.py
    ├── test_repositories.py  test_migrations.py  test_audit_trail.py  test_context.py
    ├── test_architecture.py  test_architecture_bootstrap.py  test_plugin_core.py
    ├── test_architecture_versioning.py  test_architecture_evolution.py
    ├── test_adr_manager.py  test_risk_manager.py  test_loop_policy.py
    ├── test_orchestrator.py  test_scheduler.py  test_realization_control.py
    ├── test_realization_gate.py
    ├── test_openai_advisor.py  test_claude_advisor.py  test_grok_advisor.py
    ├── test_openai_judge.py  test_judge_use_case.py
    ├── test_evidence_merger.py  test_evidence_composition.py  test_decision_engine.py
    ├── test_cost_plugin.py  test_cost_composition.py
    ├── test_reporting.py  test_excel_reporting.py  test_markdown_reporting.py
    ├── test_monitor.py  test_human_override.py  test_approval_gate.py
    ├── test_cline_worker.py  test_composition.py
    ├── test_recovery.py           crash/restart recovery suite
    └── _recovery_child.py         subprocess crash driver (not a pytest module)
```

`.mini_build/` is the build controller's own working area and is not part of the
product; `data/` and `reports/` are created at runtime and are ignored by Git.

---

## 13. Version

Architecture Assistant **v1.0.0**

Git tag: `v1.0.0`

The release is identified by the Git tag `v1.0.0`. Note that the packaging
metadata still carries the original build version: `pyproject.toml` declares
`version = "0.1.0"` and the package `__version__` is `"0.1.0"`. That metadata was
not bumped for the v1.0.0 tag, so the tag - not the metadata string - is the
release identity.

---

## 14. Status

**v1.0.0 is frozen and intended for a controlled Python pilot.** The
architecture baseline stays at **v1.1** with its three deterministic rules, the
workflow contracts and the persistence schema are frozen, the validation results
in section 9 describe the freeze commit, and the limitations listed in section 11
are accepted for this release.
