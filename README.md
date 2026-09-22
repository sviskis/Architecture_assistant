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
| `ports` | `src/architecture_assistant/ports` | Protocols for storage, repositories, transactions, capabilities (worker, cost, realization, reporting, judge, supervisor) |
| `architecture` | `src/architecture_assistant/architecture` | Source scanner, deterministic rules and validator for the declared baseline |
| `application` | `src/architecture_assistant/application` | Use cases: orchestrator, scheduler, loop policy, context builder, realization control, bootstrap, versioning, evolution, evidence merger, decision engine, judge use case, reporting projection, monitor, human override, approval gate, plan loader, architecture review, synthesis, proposal approval, advisory supervision (send policy, gate, tick-driven runtime) |
| `infrastructure` | `src/architecture_assistant/infrastructure` | SQLite storage, repositories, migrations, Cline file channel, provider adapters, cost plugin, Excel and Markdown renderers, the offline scripted supervisor |
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
- **Architecture review** - one explicit, advisory consultation: the three
  existing advisors answer the operator's own question, the evidence merger and
  the decision engine explain the result, and the judge is consulted only for a
  structurally valid conflict. It is read-only: no workflow state, no gate
  verdict and no database row changes hands.
- **Architecture proposal (managed project)** - one explicit flow from one
  advisory review to one durable, human-approved design *for the project the
  assistant manages*: an `ArchitectureProposal` aggregate with its own lifecycle
  (`DRAFT -> APPROVED` / `REJECTED` / `REVISION_REQUESTED`, and `SUPERSEDED`
  when a later revision replaces it), its own `architecture_proposals` table and
  its own revision lineage (`revision_no` / `revision_of`). Synthesis is
  provider-neutral and **deterministic by default** (no provider, no key, no
  cost) and fails closed on malformed content; every generation and every human
  decision writes exactly one audit entry (`PROPOSAL`/`CREATE`, `UPDATE`,
  `REJECT`). An approved proposal records *the managed project's* architecture
  and **never** mutates the assistant's own baseline: no
  `ArchitectureVersion`, no ACR, no assistant ADR or risk, no rule change and no
  realization verdict.
- **Operator log** - one ephemeral, read-only event log per session: timestamp,
  level (`INFO`/`WARN`/`ERROR`), component, action, message and optional step /
  review id. A review logs every stage it reaches *and* the ones it does not
  (a gate that cannot be read, a judge that was not consulted). Messages are
  sanitized by contract, so no credential, authorization header or provider body
  can reach the panel, and clearing the view deletes nothing.
- **Side-by-side advisor comparison** - the review tab shows the three advisors in
  one window as three read-only panes, each with its own provider, model, key and
  connection controls and a **compact** summary (provider, model, execution
  status, severity, relation, short finding summary); the full result - finding
  id, confidence, anchor, step, evidence references, tokens, cost, the sanitized
  provider error and the whole finding text - is one `Details...` click away. The
  shared results are **one compact summary line** under them: the evidence count,
  the conflict count, the judge, the decision status and the cost, each with a
  button that opens the window holding the full information.
- **A popup for everything that is consulted, not worked in** - the main window
  keeps four tabs (**Monitor**, **Architecture Review**, **Architecture
  Proposal**, **Supervisor**) and one compact utility bar, and Logs, Audit, Risks,
  Reports, cost detail, the judge, the evidence conflicts, every advisor's
  technical result, the supervisor's technical details, the project details, the
  architecture details and the monitor snapshot open in their own window. Each
  window is a `Toplevel` that is **resizable, scrollable and non-modal** (only a
  confirmation dialog is modal), renders one plain-data spec the controller built
  from the last projection, and never calls a repository, a provider or the core.
  Nothing was removed by taking the tabs away: the same rows, from the same
  payload, are in the popups - and an unused judge or an empty conflict list now
  costs one line instead of a blank table.
- **One dialog standard** - every window, from a confirmation to the audit trail,
  is the same window: a **title**, an optional one-line **note**, a **content
  area** of sections and one **action bar** at the bottom
  (`[ Copy ] [ Refresh ] [ ... ] [ Close ]` for a view, `[ Cancel ] [ Confirm ]`
  for a decision), with the same padding, spacing and button order everywhere.
  Escape, the window's own X and `[ Close ]` all close it, Escape on a decision
  window means *Cancel*, and the content scrolls (a long table in both
  directions) instead of forcing a huge window. A window opens at one of three
  size categories - **small** for a confirmation or a notice, **medium** for a
  detail view, **large** for a long table (logs, audit, risks, a plan decision) -
  and a window that carries nothing but a note is only as tall as its note until
  it has content to show. It is centred on the main window when it has no
  remembered position, clamped back onto the screen when a remembered position
  would land off it, and it never steals the keyboard from a dialog that is
  already waiting for an answer.
- **Windows that remember where they were** - the size and position of every
  popup are remembered in the same local layout file as the main window and the
  sash positions, under a **stable key** per window (`popup.cost`, `popup.logs`,
  `popup.audit`, `popup.risks`, `popup.project_details`,
  `popup.architecture_details`, `popup.supervisor_details`,
  `popup.advisor_details_1`, `popup.snapshot`, ...). The key never changes when a
  title is reworded, so re-labelling a window cannot move it. Each of these
  windows is opened **once**: a second click raises and focuses the window that is
  already open instead of piling up a duplicate. `Refresh` re-reads the last
  payload, `Copy` puts the window's own content (tables tab-separated) on the
  clipboard, and a failure opens **one** error window - a readable message with
  the technical detail behind `Details...`, sanitized by the same contract the log
  uses, so no stack trace, header, key or provider body can reach it.

- **Advisory supervision (Step 28)** - an *optional*, fail-closed analysis of one
  worker report before the authoritative review. It is **off by default**; with
  it off the loop is exactly the pre-supervision loop, and the refusal is
  **enforced in the application, not in the panel**: with
  `supervision=False` no report is read or analysed, no supervisor is called, no
  supervision row is written, no directive is created and no audit entry is
  appended - a direct use-case or core-worker call gets the same deterministic
  disabled answer (`outcome: disabled`, `status: DISABLED`, `waiting_for: none`)
  as the panel's disabled tab, and every human supervision action fails closed
  with `SupervisionDisabledError`. When it is on:
  - the report is supervised under an **identity** that is a hash of the exact
    bytes plus `(project, step_no, attempt, baseline)`, so a rewritten report is a
    new record and an old decision can never authorize it;
  - a **fail-closed gate** lets the review start only for an explicitly allowing
    status of the *current* report: a missing row blocks, `SENT` blocks (a
    delivered directive waits for **new** evidence), and only `NO_ACTION`,
    `WAIVED` and `REJECTED` allow;
  - the send policy is **re-derived by the assistant** and never trusted from the
    supervisor's labels: only `CLARIFY`/`RETRY`, only `LOW` risk, never in
    `MANUAL`, never with `requires_human`, and a deterministic denylist refuses
    any instruction naming architecture, dependencies, schema, deletion,
    security, credentials or attempt/verification changes;
  - malformed report bytes never reach a supervisor: they are recorded
    `MALFORMED` and escalate on two **persisted** deadlines (per hash and
    absolute), so nothing loops forever;
  - a directive is delivered as a durable intent (`SEND_PENDING`), an
    at-least-once publication into the worker channel and a durable completion
    (`SENT`); a restart reconciles whichever half finished;
  - four human write paths - **Approve & Send**, **Reject Directive**, **Waive**
    and **Escalate** - each require an exact supervision id plus an actor and a
    reason, each revalidate the identity first, each write exactly one status
    change and exactly one audit entry in one transaction, and none of them can
    un-send a delivered directive.
  The supervisor is advisory in the strongest sense: it cannot set `VERIFIED`,
  move a step, increment an attempt, bypass `max_attempts`, acknowledge a report,
  touch a baseline, an ACR, an ADR or a risk, or reach the realization gate. The
  only table it writes is `supervision_records`.
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
- **Per-advisor provider settings** - each of the three advisor panes lets the
  operator choose *which* provider it uses (**OpenAI**, **Claude**, **Grok**,
  **DeepSeek** or **Disabled**), which **model**, and its **API key**, and to
  test the configuration with **Test Connection** before saving it. The choice is
  plain configuration, never architecture truth: it is resolved through one
  composition-level `AdvisorFactory` (the panel knows no adapter class), it can
  never touch the FSM, the gate, the baseline or the audit trail, and the
  **Disabled** option is a real AdvisorPort that abstains on every question
  **without any provider call**. Provider, model and key live in the local,
  Git-ignored `data/provider_settings.json`; nothing about them is ever written
  to SQLite, the audit trail, a report, a `Finding`, a `Decision`, an
  `ArchitectureReviewResult`, an exception message or a log line. The key is
  masked (`show="*"`) in the panel, is never read back into a widget, and
  **Test Connection** answers with exactly one of `CONNECTED`, `AUTH ERROR`,
  `PROVIDER ERROR`, `NETWORK ERROR`, `MODEL ERROR` (or `DISABLED`) - never a
  status code, a header or a body.

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
5. **Supervision (optional)** - when supervision is enabled, the report must
   first be analysed and reach an *allowing* status of the current report's
   supervision record before the review may start (Gate A at
   `REPORT_RECEIVED -> START_REVIEW`, Gate B again inside the review). A blocked
   report reaches neither the realization gate nor the acknowledgement, and the
   supervision record is written *before* the supervisor is asked, so a crash can
   never lose the fact that an analysis was owed. With supervision disabled the
   loop skips both checks entirely.
6. **Review** - the received report is mapped deterministically onto a review
   event. Architecture questions and unresolved issues are always a human
   decision; otherwise the report status decides.
7. **Realization gate** - before anything may reach `VERIFIED`, the source tree
   is scanned and validated against the authoritative baseline; a violation
   downgrades the outcome to `BLOCKED`.
8. **Outcomes** - verification on success, a bounded retry loop for revision and
   worker failure, blocking for human decisions, conflict resolution, or abort.
9. **Report consumption** - the outcome is persisted first; only then is the
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
- **Supervision restarts are honest.** A supervision identity is unique per
  `(project, step_no, attempt, report hash, baseline)` and the record is written
  **before** the supervisor is asked. A restart therefore finds either a decided
  record (nothing to do) or `ANALYSIS_PENDING` (the analysis was owed, so it is
  re-run - on the same row) or `SEND_PENDING` (the publication may or may not
  have happened, so reconciliation resolves it from persisted state **plus the
  artifact itself**). Neither SQLite nor the filesystem can be joined into one
  atomic step, so the design claims **at-least-once** delivery and a resolved
  record, never exactly-once magic.
- **A polling tick is nearly free.** The tick reconciles, looks at the current
  report and stops: an unchanged report is never analysed twice, no provider is
  called for it, no context is rebuilt and **no audit entry is written**. Only a
  durable decision (analysis, send, human action) writes one.
- **A disabled session cannot be half-on.** `supervision=False` is enforced at the
  application boundary, so no code path - a panel button, a core-worker call, a
  script - can analyse a report, call a supervisor, write a row, publish a
  directive or append a supervision audit entry while supervision is off. The
  disabled answer is deterministic (`outcome: disabled`, `status: DISABLED`) and
  the runtime tick is inert: no counter, no timestamp, no event, no
  reconciliation. A `SEND_PENDING` or `ANALYSIS_PENDING` record left by an earlier
  *enabled* session is deliberately not touched while supervision is off - it is
  resolved the moment supervision is enabled again, on the first tick.

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

**Runtime locations** (all ignored by `.gitignore`):

| Path | Purpose |
| --- | --- |
| `data/architecture_assistant.db` | SQLite source of truth |
| `data/cline/` | Cline file channel (`to_cline/`, `context/`, `from_cline/`, `from_cline/archive/`) |
| `data/gui_layout.json` | the operator panel's remembered window layout (a local preference) |
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

The plan itself is imported through the controlled plan loader - the only write
path a plan has:

```python
plan_text = Path("examples/plan_youtube_to_mp3.json").read_text(encoding="utf-8")

preview = composition.plan_loader.preview(plan_text)      # reads only, writes nothing
print(preview.importable, preview.step_count, preview.blocked_reason)

if preview.importable:
    result = composition.plan_loader.import_plan(
        plan_text, actor="operator", reason="the approved pilot plan",
        source_file="examples/plan_youtube_to_mp3.json",
    )
    print(result.step_count, composition.health().current_step_no)
```

An import validates the whole file first and writes **nothing** if anything is
wrong; it refuses a database that already holds steps, a plan that names another
project or plan version, and any unknown field (including a workflow state). All
steps plus exactly one `PLAN`/`IMPORT` audit entry are written in one
transaction, and the project row itself is never touched. Human commands are
issued through `composition.human_override` (state control) and
`composition.approval` (approve / reject), and `composition.monitor` reads state
without changing it.

**Operator panel (v1.1).** `python -m architecture_assistant_gui` opens the
presentation-only operator panel: it shows the monitor projection, the audit
trail, the open risks and the worker channel, and every button calls one of the
use-cases above through a single dedicated core thread. The panel never writes
the database; **Load Plan** performs the preview-and-confirm import shown above:

```console
python -m architecture_assistant_gui --database data/youtube_to_mp3.db \
    --project-name youtube_to_mp3 --plan-version 1.0 --mode MANUAL --actor gints
```

On Windows, **START_ARCHITECTURE_ASSISTANT.bat** in the project root does the
same thing for a double-click: it works from its own directory (`%~dp0`), puts
`src` on `PYTHONPATH`, finds a usable Python (preferring `py -3.13`, then `py -3`,
`python`, `python3`; only an interpreter that is 3.11+, has tkinter and can
import the panel is accepted), starts the panel **without a console window** and
closes itself, so nothing is left open. Anything it cannot start is reported in
its own window, which then waits for a key press, and the panel's own startup
output is kept in `%TEMP%\architecture_assistant_gui_launch.log`.
**START_ARCHITECTURE_ASSISTANT_CONSOLE.bat** is the same launcher with the console
kept open for logs and debugging. Arguments are forwarded, so
`START_ARCHITECTURE_ASSISTANT.bat --supervision` opens the panel with supervision.

Supervision is the panel's one **opt-in** switch; without it the panel behaves
exactly as it did before supervision existed:

```console
python -m architecture_assistant_gui --database data/youtube_to_mp3.db \
    --supervision --actor gints
```

**Run Architecture Review (v1.1).** The same panel carries one explicit,
advisory consultation of the three existing advisors. The operator writes the
**review question** himself (it is prefilled with a default and sent verbatim to
every advisor - one question, one run, no conversation).

The tab is laid out for comparison, in one window (never extra OS windows):

* the **three advisors side by side**, one read-only pane each, titled with the
  provider name and carrying that provider's execution status (coloured
  `INFO`/`WARN`/`ERROR`), the **full finding text**, its structured facts
  (severity, confidence, relation, anchor, step, finding id, evidence refs),
  its **evidence references**, the sanitized **ABSTAIN/ERROR reason** and its
  **tokens and cost** - so all three answers can be read against each other;
* **below them one compact summary line**, the whole lower half of the tab:
  `Evidence: n`, `Conflicts: n`, `Judge: used (status)` / `Judge: not used`,
  `Decision: status` and `Cost: amount` / `Cost: unavailable (reason)`, with one
  button under each value - **Evidence**, **Conflicts**, **Judge**, **Decision**,
  **Cost** - that opens the window with the full information (the merged-evidence
  table, the validated conflicts, the judge output, the advisory decision marked
  *explains, never overrides*, and the per-advisor cost). Nothing of that is
  rendered into the tab itself, so an unused judge, an empty conflict list or a
  missing review costs one line instead of five sections - and before the first
  review the panel is a single small empty state, never a set of empty tables.

Every region is a **draggable pane** rather than a fixed layout: the split
between the work area and the tab notebook, the split inside the review tab
between the question/header and the advisor area - the summary panel sits at the
bottom of that same pane, so there is **no second splitter under the advisors**
and they keep the height - the split between the three advisor panes themselves
(so one advisor can be widened without touching the other two), the one between
the proposal summary and the proposal detail, and the three panes of the
Supervisor tab. Each splitter starts at a sensible position - the three advisors
and the three supervisor panes each get a third, the notebook keeps most of the
window height - and each pane has a floor, so a drag can never collapse a region
to nothing.

Where the operator put those panes is **remembered**, so the panel comes back the
way it was left: on a normal close the window's size and position, the sash
position of every splitter and the tab that was open are written to one small
JSON file (`data/gui_layout.json`, ignored by Git, in a file of its own so it can
never overwrite the remembered operator name), and the next start applies the
geometry before the window is mapped and each remembered sash once that splitter
has a real size - kept there while the panels settle, and until the operator
drags that splitter himself, after which it belongs to the window again. The
saved layout is a **local preference and nothing else**: numbers plus one tab
title - no operator name, no path, no project or step, no secret - and nothing
about it is stored in the database. Every way the file can be wrong costs
the operator the broken part and no more: an unknown version, a wrong type or an
impossible value is dropped, a sash that would collapse a pane stops at that
pane's floor, a remembered window position that is not on this screen is dropped
(the size is kept), and a missing or corrupt file simply means the default
layout - the panel always starts.

Above the panes sit the review header (project, **configured `source_root`**,
current step, architecture version, fresh deterministic gate status) and the
question field. Before the first review the panes are already labelled from the
configured advisors and say *not run yet*; a configured advisor is never hidden,
and a missing one is padded with an explicit idle pane rather than an invented
result. The review writes **nothing** - no workflow state, no finding, no
decision, no ADR/ACR, no baseline - and the deterministic gate stays the only
verdict. The only database side effect is the cost telemetry each adapter records
for itself.

**Generate Architecture Proposal (v1.1).** After a review, the **Architecture
Proposal** tab offers four buttons - **Generate Architecture Proposal**,
**Approve**, **Request Revision** and **Reject** - plus two inputs: an optional
**requirement addendum** (when empty, the review question is used verbatim) and
the **revision feedback**. A typical run is: review, Generate, read the proposal,
Approve; or Generate, and if the design is not right, write the feedback and
Request Revision, fix the requirement addendum, and Generate again - which
creates the next revision and marks the previous one `SUPERSEDED`. Both revisions
stay in the stored-proposals table, so the history of the design is visible and
never rewritten.

The proposal is durable and project-scoped; the *review* it was built from is not
(a review is ephemeral and writes nothing). That is why the tab shows the
**bounded evidence digest** next to the proposal: the review id, the advisor
counts and finding sources, the conflicts, the judge status, the advisory
decision and - clearly labelled as the *assistant's* verdict, not the managed
project's - the deterministic gate that was current at review time. An approved
proposal therefore stays explicable after a restart even though the review that
produced it is gone.

**Supervisor (v1.1).** When the panel is started with `--supervision`, a
**Supervisor** tab shows the advisory supervision of the *current* report. It is
the same idea as the review tab: the operator sees three read-only panes in one
window, and the panel decides nothing.

- **LEFT - Architecture Assistant** (what the assistant owns): the task goal and
  description, the step state with `attempt of max_attempts`, attempts remaining,
  mode, whether the project is paused, the baseline, and the bounded facts a
  supervisor would be handed - the operator's own constraints, accepted ADRs,
  open risks, deterministic findings and open change requests.
- **CENTER - Cline** (what the worker submitted): report status and summary, the
  files created/changed/deleted, the test block, the issues and architecture
  questions the worker raised, the dependencies it added, when the report was
  first seen and its exact hash.
- **RIGHT - Supervisor** (the advisory analysis): the provider, the analysis
  status, the proposed action and risk, the reason, the evidence, the proposed
  instruction, whether a human is required, who decided and why, **the gate
  verdict** (`ALLOW`/`BLOCK` plus its reason) and the two persisted malformed
  deadlines.

Above the panes the header carries the identity and the polling state: project,
step, attempt, worker state, supervisor state, the current report hash, the
supervision id, whether the runtime is polling, and the deterministic
`waiting_for` value. Below them one history table lists every supervision
identity of that attempt, newest first, with its status, action, risk, report
hash and last update.

The controls are four human write paths plus one explicit read: **Analyze
Report** (idempotent by identity - a decided report is never analysed twice),
**Approve & Send**, **Reject Directive**, **Waive** and **Escalate**. Each is
enabled only while the *core* would accept it, each collects the actor and a
reason, and each shows the core's own refusal text when it would not. The panel
never sends a supervision id: the core resolves the current report identity
itself, so a stale panel cannot decide anything about a report the workflow has
moved past. The directive instruction is collected verbatim and captured on the
UI thread before the call is handed to the core. With supervision disabled the
tab is inert and says so, and the application submits no polling tick at all -
and the core refuses those same actions anyway, because disabled supervision is
enforced by the application: the button state is a convenience for the operator,
never the guard.

The runtime itself is **tick-driven, not a daemon**: the assistant owns exactly
one thread allowed to touch the source of truth, so the panel asks the core for
one tick (`--supervision` schedules that every two seconds) and a tick that
arrives while another action runs is dropped instead of queued. Nothing is
logged by a tick that changed nothing.

**Logs (v1.1).** A **Logs** tab shows the same activity as a newest-first,
read-only table: sequence, time, level, component, action, message, step and
review id, with `WARN` and `ERROR` entries coloured. An architecture review logs
its context build, the gate check, each advisor's start and outcome, the evidence
merge, the decision engine, the judge (`start`/`result`, or an explicit
`not-consulted`), the cost collection and its own completion - in the order it
happened. The button **Clear View** empties the panel's view and nothing else:
the persistent record of what happened is the append-only audit trail on the
Audit tab.

The two tabs divide the work deliberately: the **Architecture Review** tab is for
architecture *content* (what the advisors said, what was merged, what the gate
and the judge concluded), while **Logs** is for operational and debug *events*
(who started, what failed, how long and how much it cost). Neither replaces the
other, and neither writes anywhere.

**Architecture Proposal (v1.1).** A third tab turns one advisory review into a
durable, human-approved design for **the managed project**, and its first job is
to keep two architectures apart.

1. **The Architecture Assistant's own architecture** - its layers, its rules, its
   `ArchitectureVersion` baseline, its ACRs, its ADRs and risks, and the
   deterministic validator that enforces all of it. This is what the assistant is
   *made of*, it is declared in code, and nothing in this tab can touch it.
2. **The managed project's architecture** - the design for the project this
   database manages (`youtube_to_mp3` in the pilot). This is what the
   `ArchitectureProposal` aggregate describes, and it is the only thing this tab
   changes.

The tab is therefore honest about what an approval means: an `APPROVED`
proposal says *"this is the human-approved architecture for the managed
project"*. It is **not** an assistant release, it does **not** create a change
request, and it does **not** bump the assistant's baseline - a managed project
changing its mind is not a reason for Architecture Assistant v1.2.

The flow is deliberately narrow. **Generate Architecture Proposal** consumes
exactly one review (the review is the evidence, and the last one run in this
session is used; without a review the button is disabled rather than guessed),
echoes the operator's requirement verbatim, keeps every advisor finding, status,
conflict and unresolved question without ever voting on them, and writes one
`DRAFT` proposal plus one audit entry. With no synthesizer configured the
deterministic organizer runs: it fills only what it can *derive* from the review
and names everything else as an unresolved question, because inventing a managed
project's modules is not the assistant's call. A synthesizer that answers outside
the closed content contract is refused and **nothing is stored**.

Then a human decides: **Approve**, **Reject** or **Request Revision** (which
requires feedback). Each needs an explicit actor and a reason, each writes
exactly one status change and exactly one audit entry in one transaction, and
each is refused - before the transaction, so nothing is written - when the
proposal has already been decided, when a newer revision replaced it, when it
belongs to another project, or when its stored inputs no longer match its own
fingerprint. Repeating the identical decision is a no-op; repeating it with a
different reason fails closed rather than rewriting history. **Request Revision
never re-synthesizes on its own**: it records the feedback, and the new revision
exists only when the operator explicitly generates one, at which point the
predecessor is marked `SUPERSEDED` and both rows remain.

The tab shows the proposal's header (id, managed project, status, revision and
its predecessor, source review id, the baseline that was current at review time
as *traceability only*, created/decided timestamps, the deciding actor, the
reason, the feedback and the fingerprint), the proposed architecture as read-only
tables (summary and rationale, modules and boundaries, data flow, external
dependencies, rules, risks, ADR candidates, implementation phases, unresolved
questions) and the bounded evidence digest the proposal was built from. Nothing
in the tab is a rule the assistant enforces: the proposal's `proposed_rules` are
the managed project's rules, and no project-rule enforcement exists in v1.1.

The log is deliberately **not** a logging framework: the project issues no
`logging` call anywhere, and a process-wide logger would be shared state - two
assistant instances in one process would share it, which would make the per-run
order the panel promises non-deterministic. Instead the core emits a validated
event (`build_event`) through an injected hook, one thread-safe queue owned by the
GUI's core worker stamps the sequence number, and the Tk thread only drains plain
dictionaries and renders them. The contract itself lives in
`application/eventlog.py`: it fixes the shape and vocabulary, redacts credentials
and headers, collapses and bounds messages, and allows only an exception **type
name** - never its message - to be logged for a failure.

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

Current tree, after the operator panel (v1.1), the plan loader, the advisory
architecture review, the operator log, the side-by-side advisor layout, the
managed-project architecture proposal, the advisory supervision of a worker
report and the dialog/window cleanup:

- **3524 passed / 0 failed** (`python -m pytest -q`; one display-dependent test
  skips when Tk cannot give a window a real geometry)
- architecture self-check: baseline **1.1**, compliant **True**, **0**
  violations, **71** modules, **3** architecture rules

The plan loader, the architecture review, the log-event contract, the
managed-project proposal pair (`architecture_synthesis.py` and
`proposal_approval.py`) and the advisory supervision quartet
(`supervisor_policy.py`, `supervision.py`, `supervisor_runtime.py`,
`scripted_supervisor.py`) are each plain `application`/`infrastructure`
modules, and supervision adds one table (`supervision_records`, migration v6)
inside the existing persistence layer, so the baseline version and the rule set
are unchanged: `v1.0 -> v1.1` still describes the only architecture change, and
no version bump was needed for any of them.

The last two are the concrete answer to the one question this step exists to
settle: *an approved `ArchitectureProposal` for a managed project has no path to
`ArchitectureVersion`*. `tests/test_architecture_synthesis.py` and
`tests/test_proposal_approval.py` assert that from both directions - the modules'
code never names an assistant-scoped repository or use-case (checked on the AST,
not on prose), and after a full generate/reject/revision/approve cycle the
`architecture_versions`, `architecture_change_requests`, `adrs` and `risks`
tables are byte-identical.

The supervision evidence is deliberately behavioural rather than structural,
because its promise is about *what may happen*: `tests/test_supervision_gate.py`
proves the fail-closed gate and both orchestrator gates, and
`tests/test_supervisor_runtime.py` proves the cheap tick, the malformed
deadlines, the send/reconcile protocol and the four human decisions - all
against real file-backed SQLite and a real Cline channel with the offline
scripted supervisor, so no test in that pair needs a provider, a key or a
network. `tests/test_supervisor_policy.py` pins the send policy, and the panel's
own surface is covered by `tests/test_gui_controller.py` (the Supervisor tab and
its gating) and `tests/test_gui_core.py` (the core-thread API on a real
database). The **disabled** mode has its own evidence at both levels: the
`TestDisabledSupervision` class in `tests/test_supervision_gate.py` proves that a
direct use-case call cannot analyse, reconcile, tick, decide or write anything,
and `TestDisabledSupervisionIsEnforcedInCore` in `tests/test_gui_core.py` proves
the same through the real core worker - no provider call, no row, no directive
artifact, no supervision audit entry - while the enabled path stays unchanged.

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
- **Operator-supervised pilot usage.** A human imports the plan (validated, once,
  atomically), drives the loop, approves or rejects gated steps, unblocks or
  resolves blockers, may pause and resume the project, and never edits the
  database directly.
- **Supervision is opt-in and offline.** Advisory supervision is disabled unless
  an operator asks for it (`--supervision` or `CompositionConfig(supervision=True)`),
  and the wired supervisor needs no provider, no key and no network. While it is
  disabled the application refuses every supervision activity outright - no
  analysis, no provider call, no row, no directive, no audit entry - so the
  switch is a real switch and not a display preference. The panel's polling
  cadence (two seconds), the malformed deadlines and the operator constraints are
  composition configuration, not stored state.

---

## 11. Known V1 Limitations

- **The AI evidence-conflict capability is not wired into the automatic loop.**
  The evidence merger, the decision engine and the judge are implemented and
  tested, and a run reaches all three through the explicit, read-only
  architecture review - but no code path raises the `CONFLICT` state and nothing
  in the loop consults an advisor, so the deterministic architecture rules remain
  the only verdict and no advisor can influence a step. (The approval gap that
  earlier reviews tracked as AQ-2 is **not** an open limitation: v1.0 resolves it
  with `ApprovalGate`.)
- **No CLI, no daemon, and no real supervisor provider.** The loop is invoked
  through the composition API or the operator panel; there is no command-line
  assistant, no service and no background process. Advisory supervision exists
  and is wired end to end, but the only supervisor the composition constructs is
  the **offline scripted double** (`infrastructure/scripted_supervisor.py`): a
  real Codex (or any other) supervisor adapter is deliberately **not** part of
  this step and must arrive later behind the same `SupervisorPort`. The runtime
  is tick-driven - it owns no thread, no timer and no scheduler of its own; a
  host (the panel, via `root.after`) calls one tick. Enabling supervision is a
  per-session operator decision (`--supervision`); it is not persisted, so a
  restart never silently changes it. Supervision is also **advisory only**: it
  can delay a review, never authorize a `VERIFIED`, and with it disabled the loop
  is byte-for-byte the pre-supervision loop - and the disabled state is enforced
  by the *application* (no analysis, no provider call, no row, no directive, no
  audit entry) rather than by the panel's button state.
- **No plan replacement, merge or editing.** A plan enters the source of truth
  once, through `PlanLoader` (or the panel's **Load Plan**): a database that
  already holds steps refuses a second import, and nothing is merged, replaced,
  renumbered or deleted. Plan authoring and step reordering are out of scope.
- **No JSX analyzer yet.** The realization gate validates the Python source
  tree; language-specific analyzers are out of scope for v1.
- **A crashed acknowledgement can leave an orphan report (F-1).** If the process
  dies between the durable outcome and the report acknowledgement, the report
  remains in `from_cline/` while the authoritative outcome is already committed
  and can never be replayed into another outcome. The documented procedure is to
  inspect `from_cline/` after a restart or completion and, when the matching
  step is already `VERIFIED` or `ABORTED`, archive that file manually.
- **Provider API keys are stored in a plain-text local file (temporary).** The
  per-advisor provider configuration keeps its key in
  `data/provider_settings.json` - a **local-only**, Git-ignored file beside the
  operator's own database. That is a deliberate, temporary trade rather than a
  credential-store claim: the file is readable by whoever can read the operator's
  working copy, so the key is protected by *location and Git-ignore*, not by the
  operating system. The design is isolated behind the `ProviderSelection` /
  `ProviderSettings` value objects and the `save_provider_settings` /
  `load_provider_settings` pair, so a real credential-store backend (for example
  the Windows Credential Manager) can replace the key field without touching the
  panel, the factory or any adapter. Until then, the guarantees that *are* met
  are the ones the tests pin: the key is never logged, never audited, never
  exported, never rendered in a `repr`/view model and never embedded in an
  exception or an `ArchitectureReviewResult`.
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
│                                  (and data/gui_layout.json explicitly)
├── README.md                      this document
├── src/
│   ├── architecture_assistant/      the frozen core (the scanned tree)
│   │   ├── __init__.py            package metadata
│   │   ├── domain/                enums.py  models.py  fsm.py  audit.py
│   │   ├── ports/                 storage.py  repositories.py  transactions.py
│   │   │                          capabilities.py
│   │   ├── architecture/          model.py  scanner.py  rules.py  validator.py
│   │   ├── application/           orchestrator.py  scheduler.py  loop_policy.py
│   │   │                          context.py  realization_control.py
│   │   │                          architecture_bootstrap.py
│   │   │                          architecture_versioning.py
│   │   │                          architecture_evolution.py  adr_manager.py
│   │   │                          risk_manager.py  plugin_core.py
│   │   │                          evidence_merger.py  decision_engine.py  judge.py
│   │   │                          reporting.py  monitor.py  human_override.py
│   │   │                          approval.py  dispatch.py  plan_loader.py
│   │   │                          architecture_review.py  eventlog.py
│   │   │                          architecture_synthesis.py  proposal_approval.py
│   │   │                          supervision.py  supervisor_policy.py
│   │   │                          supervisor_runtime.py
│   │   ├── infrastructure/        sqlite.py  storage.py  migrations.py  repositories.py
│   │   │                          cline.py  scripted_supervisor.py  _http.py  cost.py
│   │   │                          openai.py  claude.py  grok.py  openai_judge.py
│   │   │                          _reporting.py  excel_reporting.py
│   │   │                          markdown_reporting.py
│   │   └── composition/           root.py  realization.py  evidence.py  evolution.py
│   └── architecture_assistant_gui/  operator panel (external host, v1.1)
│       ├── core.py                core-thread worker + background runner
│       ├── controller.py          UI state machine + button matrix
│       ├── views.py               Tk widgets (layout and rendering only)
│       ├── layout.py              the remembered window layout (one JSON file)
│       ├── app.py                 window, dialogs, plan preview, result pump
│       └── __main__.py            python -m architecture_assistant_gui
├── examples/
│   └── plan_youtube_to_mp3.json   the pilot plan (real Phase values)
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
    ├── test_plan_loader.py        plan validation, identity, atomicity
    ├── test_architecture_review.py  advisory three-advisor review
    ├── test_architecture_synthesis.py  managed-project proposal synthesis
    ├── test_proposal_approval.py    the proposal approval human write path
    ├── test_supervision_gate.py     the fail-closed gate and the loop's two gates
    ├── test_supervisor_runtime.py   the cheap tick, malformed deadlines, send protocol
    ├── test_supervisor_policy.py    the deterministic send allowlist and denylist
    ├── test_gui_logs.py        the log-event contract and the Logs tab
    ├── test_gui_layout.py      the draggable panes and the layout that is restored
    ├── test_gui_layout_store.py  the layout file: what it stores and what it refuses
    ├── test_gui_boundaries.py  test_gui_core.py  test_gui_controller.py
    ├── test_cline_worker.py  test_composition.py
    ├── test_recovery.py           crash/restart recovery suite
    └── _recovery_child.py         subprocess crash driver (not a pytest module)
```

`.mini_build/` is the build controller's own working area and is not part of the
product; `data/` and `reports/` are created at runtime and are ignored by Git -
including `data/gui_layout.json`, the panel's remembered window layout, which is
a local preference and never repository state.

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

Beyond that release, the v1.1 working tree described in section 9 adds the
operator panel, the plan loader, the advisory architecture review, the operator
log, the side-by-side advisor layout, the managed-project architecture proposal
and advisory supervision. None of them changes the architecture baseline, the
rule set, the step transition table, the workflow outcomes or the meaning of
`VERIFIED`; supervision adds exactly one table (`supervision_records`) and is
**off by default**, so an existing database keeps behaving as it did before it
existed. Nothing in this section is a release: no commit, tag or push has been
made for the v1.1 work.
