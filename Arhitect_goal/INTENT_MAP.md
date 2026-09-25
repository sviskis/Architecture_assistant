# Intent Map - every operator action in the panel's button matrix

| | |
| --- | --- |
| Document | `Arhitect_goal/INTENT_MAP.md` |
| Source of truth | `src/architecture_assistant_gui/controller.py` (`INTENTS`, `enabled()`, `refusal()`, `submit()`, `_action_for()` and every action builder), read in full, plus the `CoreWorker` methods in `src/architecture_assistant_gui/core.py` that each intent calls |
| Revision | branch `v1.1-gui` / HEAD `ebe36b9` + working-tree changes |
| Method | Complete read of `controller.py` (4,610 lines) and the relevant `core.py` methods. No code was changed, no test was run, nothing was committed. |
| Count | **56 intents**, exactly as the `INTENTS` tuple enumerates them |

---

## 0. How the button matrix works (read this first)

`INTENTS` is the single table the buttons are built from. Every button carries one `Intent`
(`key`, `label`, `group`, optional `states`, `needs_step`, `human`, `mutating`, `reason_prompt`,
`confirmation`), and **the controller owns the whole decision**: a view only reports which key was
pressed (`GuiApp._on_action` -> `controller.submit(key, reason=...)`).

`submit()` runs three gates in order:

1. `refusal(intent, reason)` - the per-intent preconditions plus the human-write requirement
   (a non-empty `actor` and a non-empty `reason`). A refusal is logged as `WARN` and returns
   `False`; **nothing reaches the core**.
2. `_action_for(intent, reason)` - builds the callable the core thread will run.
3. `runner.submit(intent.key, action)` - queues it. This returns `False` while another action is
   running ("Another core action is still running."), so a click during work is **dropped, not
   queued**.

`enabled(intent)` answers "may this button be pressed", in this exact order:

| # | Condition | Effect |
| --- | --- | --- |
| 1 | key is in `DISPLAY_INTENTS` (`view_snapshot`, `clear_logs` + all 16 popups) | **always enabled** - even while busy and even after a CRITICAL failure |
| 2 | `is_busy` | disabled |
| 3 | key is `refresh`, `reconnect`, `open_reports_folder`, `run_review` or one of the 7 `PROVIDER_SETTINGS_INTENTS` (`save_settings`, `test_connection_1..3`, `test_agent_a`, `test_agent_b`, `test_lead`) | **enabled, even under CRITICAL** |
| 4 | `_critical` (a critical core failure has been seen and not yet cleared by a successful read) | disabled |
| 5 | per-intent rules (`prepare_execution_plan`, deliberation, `load_plan`, `import_plan`, `synthesize_proposal`, proposal decisions, supervisor actions, `states`, `needs_step`, pause/resume/run) | see each entry below |
| 6 | otherwise | enabled |

Two facts to keep in mind while reading the entries:

- **The matrix is advisory.** The core re-validates every action and fails closed, so a stale
  panel can never force a state; `button_hint()` explains a disabled button in one line.
- **No intent blocks the Tk event loop.** Every action is a queued job on the single core thread;
  `Blocks UI` below therefore means "how long the panel refuses other actions", i.e. how long the
  core thread is occupied.

---

## 1. Intent index (in `INTENTS` order)

| # | key | Label | Group | human | mutating |
| --- | --- | --- | --- | --- | --- |
| 1 | `prepare_execution_plan` | Create Execution Plan | Plan | no | no |
| 2 | `test_agent_a` | Test Architect A | Advisor pane | no | no |
| 3 | `test_agent_b` | Test Architect B | Advisor pane | no | no |
| 4 | `test_lead` | Test Lead | Advisor pane | no | no |
| 5 | `load_plan` | Load Plan | Plan | no | yes (default) |
| 6 | `import_plan` | Import Plan | Plan | yes | yes |
| 7 | `run_review` | Run Architecture Review | Review | no | no |
| 8 | `deliberation_round1` | Run Round 1 | Deliberation workbench | yes | no |
| 9 | `deliberation_lead_review` | Generate Lead Review | Deliberation workbench | yes | no |
| 10 | `deliberation_round2` | Run Round 2 | Deliberation workbench | yes | no |
| 11 | `deliberation_synthesis` | Generate Final Synthesis | Deliberation workbench | yes | no |
| 12 | `deliberation_proposal` | Generate Architecture Proposal | Deliberation workbench | yes | yes |
| 13 | `deliberation_cancel` | Cancel Deliberation | Deliberation workbench | yes | yes |
| 14 | `save_settings` | Save Settings | Advisor pane | no | no |
| 15 | `test_connection_1` | Test Connection | Advisor pane | no | no |
| 16 | `test_connection_2` | Test Connection | Advisor pane | no | no |
| 17 | `test_connection_3` | Test Connection | Advisor pane | no | no |
| 18 | `cost_details` | Cost Details | Windows | no | no |
| 19 | `open_logs` | Logs | Windows | no | no |
| 20 | `clear_logs` | Clear Logs | Windows | no | no |
| 21 | `open_audit` | Audit | Windows | no | no |
| 22 | `open_risks` | Risks | Windows | no | no |
| 23 | `open_reports` | Reports | Windows | no | no |
| 24 | `project_details` | Project Details | Windows | no | no |
| 25 | `architecture_details` | Architecture Details | Windows | no | no |
| 26 | `supervisor_technical` | Technical Details | Windows | no | no |
| 27 | `review_judge_details` | View Judge Details | Windows | no | no |
| 28 | `review_conflict_details` | View Conflicts | Windows | no | no |
| 29 | `review_evidence_details` | Evidence | Windows | no | no |
| 30 | `review_decision_details` | Decision | Windows | no | no |
| 31 | `advisor_details_1` | Details... | Windows | no | no |
| 32 | `advisor_details_2` | Details... | Windows | no | no |
| 33 | `advisor_details_3` | Details... | Windows | no | no |
| 34 | `synthesize_proposal` | Generate Architecture Proposal | Proposal | yes | yes |
| 35 | `approve_proposal` | Approve | Proposal | yes | yes |
| 36 | `request_proposal_revision` | Request Revision | Proposal | yes | yes |
| 37 | `reject_proposal` | Reject | Proposal | yes | yes |
| 38 | `analyze_report` | Analyze Report | Supervisor | yes | no |
| 39 | `approve_and_send` | Approve & Send | Supervisor | yes | yes |
| 40 | `reject_directive` | Reject Directive | Supervisor | yes | yes |
| 41 | `waive_supervision` | Waive | Supervisor | yes | yes |
| 42 | `escalate_supervision` | Escalate | Supervisor | yes | yes |
| 43 | `run_until_idle` | Run Until Idle | Loop | no | yes (default) |
| 44 | `pause_project` | Pause | Project | yes | yes |
| 45 | `resume_project` | Resume | Project | yes | yes |
| 46 | `approve` | Approve | Approval | yes | yes |
| 47 | `reject` | Reject | Approval | yes | yes |
| 48 | `unblock` | Unblock | Step control | yes | yes |
| 49 | `resolve` | Resolve | Step control | yes | yes |
| 50 | `abort` | Abort | Step control | yes | yes |
| 51 | `export_markdown` | Export Markdown | Reports | no | yes (default) |
| 52 | `export_excel` | Export Excel | Reports | no | yes (default) |
| 53 | `open_reports_folder` | Open reports folder | Reports | no | no |
| 54 | `refresh` | Refresh | Monitoring | no | no |
| 55 | `view_snapshot` | View Monitor Snapshot | Monitoring | no | no |
| 56 | `reconnect` | Reconnect | Monitoring | no | no |

"Group" is presentation only. Three groups - `Advisor pane`, `Windows`, `Deliberation workbench` -
are deliberately **not** rendered by the generic Actions panel: those buttons live next to the
thing they act on.

---

## 2. Intent reference

### prepare_execution_plan

- **Button label:** `Create Execution Plan` (group `Plan`, `mutating=False`)
- **Enabled when:** `step_count() == 0` **and** the latest proposal on the board has `status == "APPROVED"` (plus not busy and not CRITICAL). This is why the button appears only after `approve_proposal`.
- **Action:** `_action_for` returns `lambda worker: {"execution_draft": worker.prepare_execution_plan()}`.
- **Core method:** `CoreWorker.prepare_execution_plan()` -> `composition.monitor.to_dict()`, `self.proposal_board()`, `gui.workspace.execution_plan(latest_proposal, project)`, `composition.plan_loader.preview(text)`.
- **Writes to DB:** **no.** It reads the monitor projection, the proposal board and a plan preview; nothing is persisted.
- **Provider call:** no.
- **Blocks UI:** no - queued to the core thread; measured cost of the reads is ~3 ms at 12 steps (monitor build 2.6 ms) and ~85 ms at 500 steps, plus 0.45-2.72 ms for the preview.
- **Notes:** the generated draft is **not** imported. It is handed to the UI as `execution_draft`, and `apply_result` feeds it into `set_plan(text, source_file="proposal:<id>")`, so the operator can inspect/edit it and then press `Import Plan`. The core raises `CoreError("This project already has an execution plan; it will not be replaced.")` if the database already holds steps, and the workspace generator refuses a dependency cycle or a draft above the importer's 500-step cap. Keying off the *latest* proposal means a superseded approval cannot be turned into a plan.

### test_agent_a

- **Button label:** `Test Architect A` (group `Advisor pane`, invisible to the generic Actions panel)
- **Enabled when:** always enabled unless `is_busy` - it is one of the seven `PROVIDER_SETTINGS_INTENTS`, so it stays available **even under CRITICAL**.
- **Action:** `_test_connection_action("agent_a")` -> `{"connection": worker.test_connection(slot, provider, model, api_key)}`.
- **Core method:** `CoreWorker.test_connection(advisor="agent_a", provider, model, api_key)` -> `probe_deliberation(provider, model, credential)` -> `AdvisorFactory`/deliberation adapter probe.
- **Writes to DB:** **no** (no workflow state, no audit entry, no cost row for a probe).
- **Provider call:** **yes** - one minimal authenticated request to the chosen provider. `Disabled` is a real provider choice and makes **no call at all**.
- **Blocks UI:** no - queued; the probe occupies the core thread for the request duration (typically well under a second on a healthy connection, bounded by the adapter's 30 s timeout and 2 retries with 0/1/2 s backoff, i.e. up to ~93 s if the provider hangs).
- **Notes:** the typed key is used verbatim, or the stored one when the field is blank, so testing a saved configuration needs no retyping. The verdict is exactly one of `CONNECTED`, `AUTH ERROR`, `PROVIDER ERROR`, `NETWORK ERROR`, `MODEL ERROR` (or `DISABLED`) and is stored in `_connection_results["test_agent_a"]` for the pane to render; never a status code, header or body. `agent_a` is a **deliberation** seat, not one of the three review panes.

### test_agent_b

- **Button label:** `Test Architect B` (group `Advisor pane`)
- **Enabled when:** always unless `is_busy`; also available under CRITICAL.
- **Action:** `_test_connection_action("agent_b")`.
- **Core method:** `CoreWorker.test_connection(advisor="agent_b", ...)` -> `probe_deliberation(...)`.
- **Writes to DB:** no.
- **Provider call:** yes (one minimal probe; none when the slot is `Disabled`).
- **Blocks UI:** no - see `test_agent_a` for the timing bound.
- **Notes:** identical semantics to `test_agent_a`, different advisor slot (`agent_b`); the two seats are deliberately independent, so one can be verified while the other is broken.

### test_lead

- **Button label:** `Test Lead` (group `Advisor pane`)
- **Enabled when:** always unless `is_busy`; also available under CRITICAL.
- **Action:** `_test_connection_action("lead")`.
- **Core method:** `CoreWorker.test_connection(advisor="lead", ...)` -> `probe_deliberation(...)`.
- **Writes to DB:** no.
- **Provider call:** yes (one minimal probe; none when the slot is `Disabled`).
- **Blocks UI:** no.
- **Notes:** same as the other two seats; `lead` is the chair of the deliberation workbench and the only slot used by `deliberation_lead_review` and `deliberation_synthesis`.

### load_plan

- **Button label:** `Load Plan` (group `Plan`; `human=False`, `mutating` left at its default `True`, so it is disabled under CRITICAL)
- **Enabled when:** `step_count() == 0`, i.e. only while the database holds **no** steps.
- **Action:** intercepted by the **application**, not by the core: `GuiApp._on_action` -> `_load_plan_file()` -> `filedialog.askopenfilename(...)`, `Path(path).read_text(encoding="utf-8-sig")`, `controller.set_plan(text, source_file=path)`, then `controller.submit("load_plan")` -> `_preview_action()`.
- **Core method:** `CoreWorker.preview_plan(text, source_file=...)` -> `composition.plan_loader.preview(text)` (`PlanLoader.preview`).
- **Writes to DB:** **no** - the preview reads the authoritative project and the current step count and reports its findings as data.
- **Provider call:** no.
- **Blocks UI:** **the file dialog and the file read are the only genuinely Tk-thread-blocking steps in the whole matrix** (a modal dialog plus a synchronous `read_text`). The preview itself is queued and measured at 0.45 ms (12 steps) to 2.72 ms (500 steps).
- **Notes:** the chosen file **always invalidates the previous preview**, so `Import Plan` can never act on a stale verdict. Cancelling the dialog logs "Load Plan cancelled." and writes nothing. Once a plan is imported the button stays disabled for the life of that database, and the refusal text says why: "Plan already loaded: this database already holds N step(s), and v1.1.1 imports an initial plan only." A preview that cannot be imported still renders (with every issue found), it just never enables the import.

### import_plan

- **Button label:** `Import Plan` (group `Plan`, `human=True` -> "Why is this plan being imported?")
- **Enabled when:** `plan_importable()` - a preview for the **currently loaded** file exists and its `importable` flag is true. The refusal additionally requires a pending plan ("Load a plan file first.") and a non-empty actor and reason.
- **Action:** `_import_action(reason)` -> `worker.import_plan(text, actor, reason, source_file)`, then `worker.payload()` and `worker.audit_tail()`.
- **Core method:** `CoreWorker.import_plan(...)` -> `composition.plan_loader.import_plan(...)` (`PlanLoader.import_plan`), returning `PlanImportResult.to_dict()`.
- **Writes to DB:** **yes** - all steps plus **exactly one** `PLAN`/`IMPORT` audit entry in **one transaction**. The project row itself is never touched.
- **Provider call:** no.
- **Blocks UI:** no; measured 2.95 ms (12 steps), 10.80 ms (500 steps).
- **Notes:** the whole file is validated before anything is written, and a single problem means **nothing** is written; the core refuses a database that already holds steps, a plan naming another project or another plan version, and any unknown field (including a workflow state). `apply_result` logs "Plan imported: N step(s) (a-b), plan X, hash Y. Current step is now visible in the work panel.", after which `run_until_idle` becomes enabled.

### run_review

- **Button label:** `Run Architecture Review` (group `Review`, `mutating=False`, with a confirmation: "Running the review asks the three configured AI advisors and may incur provider cost. It changes no workflow state and writes nothing. Continue?")
- **Enabled when:** always enabled unless `is_busy` - it is in the always-allowed set, so it also works under CRITICAL. The refusal, however, requires a non-empty review question (whitespace is not a question).
- **Action:** `_review_action()` -> `{"review": worker.run_architecture_review(question), "payload": worker.payload()}`.
- **Core method:** `CoreWorker.run_architecture_review(question)` -> `composition.monitor.snapshot()` then `composition.architecture_review.review(snapshot, question=..., on_event=self.forward_event)`.
- **Writes to DB:** **only cost telemetry** - each provider adapter records its own idempotent cost row through `CostPort`. No workflow state, no step/finding/decision rows, no audit entry, no baseline change.
- **Provider call:** **yes** - one call per configured advisor (three by default, plus DeepSeek if configured) and, only for a structurally valid evidence conflict, one additional judge call. `Disabled` advisors make no call and abstain.
- **Blocks UI:** no - queued, but this is the longest normal action in the panel: the core thread is occupied for the sum of the advisor calls; seconds on a healthy connection, bounded per call by the adapter's 30 s timeout, 3 attempts and 0/1/2 s backoff (up to ~93 s per advisor if the provider hangs, i.e. minutes across three panes).
- **Notes:** the question travels **verbatim** to every advisor and the review is a single one-shot consultation (no conversation). The result is kept **in memory only** (`CoreWorker._last_review`) and is the sole evidence `synthesize_proposal` accepts - so a restart (or `reconnect`) reports "no review available" again unless the review is re-run. The run announces itself *before* the first read and records a failure with the exception **type name** only, so a review that dies during its first read is still visible in the Logs tab.

### deliberation_round1

- **Button label:** `Run Round 1` (group `Deliberation workbench`, `human=True`, `mutating=False`, confirmation: "Round 1 asks both configured architects the same requirement independently and may incur provider cost. It writes the run and one audit entry, and approves nothing. Continue?")
- **Enabled when:** with **no** deliberation in view, a non-empty requirement (the project brief) must be present; with a deliberation in view, the core's `available_actions` must contain `RUN_ROUND1` **and** a `deliberation_id` must be set (`_deliberation_buttons()`).
- **Action:** `_deliberation_action("deliberation_round1", reason)`: if `worker.deliberation_board().get("current")` is empty it first calls `worker.start_deliberation(requirement, actor, reason)`, then `worker.run_deliberation_stage(ACTION_RUN_ROUND1, actor, reason)` and returns the new snapshot plus a fresh audit tail.
- **Core method:** `CoreWorker.start_deliberation(...)` -> `ArchitectureDeliberation.start(...)`, then `CoreWorker.run_deliberation_stage(...)` -> `ArchitectureDeliberation.run_round1(...)`.
- **Writes to DB:** **yes** - the deliberation run, its per-stage artifacts and the audit entries are durable.
- **Provider call:** **yes** - both architect seats are asked independently (one call each).
- **Blocks UI:** no - queued; two provider calls, so seconds normally and up to minutes if a provider hangs (the same adapter bounds as the review).
- **Notes:** starting is idempotent by requirement identity, so a double click cannot create two boards; the requirement is stored **verbatim**. Round 1 is the only deliberation verb that can start a run from scratch; every later stage acts on the run in view.

### deliberation_lead_review

- **Button label:** `Generate Lead Review` (group `Deliberation workbench`, `human=True`, `mutating=False`, reason prompt "Why are you advancing this architecture discussion?")
- **Enabled when:** the core's `available_actions` for the run in view contain `GENERATE_LEAD_REVIEW` (i.e. Round 1 produced usable answers).
- **Action:** `_deliberation_action("deliberation_lead_review", reason)` -> `worker.run_deliberation_stage(ACTION_GENERATE_LEAD_REVIEW, ...)`.
- **Core method:** `CoreWorker.run_deliberation_stage(...)` -> `ArchitectureDeliberation.generate_lead_review(...)`.
- **Writes to DB:** **yes** (the stage artifact and its audit entry).
- **Provider call:** **yes** - one call to the lead/chair seat.
- **Blocks UI:** no - queued; one provider call.
- **Notes:** the lead compares the two independent answers and names consensus, conflicts and open questions. If the core finds "insufficient peer review" (no usable Round 1 answer from one seat) it records that failure explicitly instead of inventing a comparison.

### deliberation_round2

- **Button label:** `Run Round 2` (group `Deliberation workbench`, `human=True`, `mutating=False`)
- **Enabled when:** `available_actions` contain `RUN_ROUND2`.
- **Action:** `_deliberation_action("deliberation_round2", reason)` -> `worker.run_deliberation_stage(ACTION_RUN_ROUND2, ...)`.
- **Core method:** `CoreWorker.run_deliberation_stage(...)` -> `ArchitectureDeliberation.run_round2(...)`.
- **Writes to DB:** **yes**.
- **Provider call:** **yes** - both architect seats, each seeing the lead's critique and the other seat's structured arguments.
- **Blocks UI:** no - queued; two provider calls.
- **Notes:** the protocol is deliberately staged - there is no "run everything" verb, so the operator decides when the board advances.

### deliberation_synthesis

- **Button label:** `Generate Final Synthesis` (group `Deliberation workbench`, `human=True`, `mutating=False`)
- **Enabled when:** `available_actions` contain `GENERATE_FINAL_SYNTHESIS`.
- **Action:** `_deliberation_action("deliberation_synthesis", reason)` -> `worker.run_deliberation_stage(ACTION_GENERATE_FINAL_SYNTHESIS, ...)`.
- **Core method:** `CoreWorker.run_deliberation_stage(...)` -> `ArchitectureDeliberation.generate_final_synthesis(...)`.
- **Writes to DB:** **yes**.
- **Provider call:** **yes** - one lead call.
- **Blocks UI:** no - queued; one provider call.
- **Notes:** the synthesis is what `deliberation_proposal` consumes; a stale synthesis (produced for an earlier revision) is visible in the snapshot and is refused as a proposal source rather than silently reused.

### deliberation_proposal

- **Button label:** `Generate Architecture Proposal` (group `Deliberation workbench`, `human=True`, `mutating=True`, reason prompt "Why is this deliberation's synthesis becoming a proposal?")
- **Enabled when:** `available_actions` contain `GENERATE_PROPOSAL` (a final synthesis exists for the run in view).
- **Action:** `_deliberation_action("deliberation_proposal", reason)` -> `worker.generate_deliberation_proposal(actor, reason)`.
- **Core method:** `CoreWorker.generate_deliberation_proposal(...)` -> `ArchitectureDeliberation.generate_proposal(...)`; the payload carries the proposal, the run snapshot and a fresh proposal board.
- **Writes to DB:** **yes** - one durable `DRAFT` proposal bound to the deliberation's exact synthesis identity, plus its audit entries.
- **Provider call:** no (it consumes the stored synthesis; no synthesizer runs here).
- **Blocks UI:** no; one transaction (milliseconds, plus a proposal-board read).
- **Notes:** the proposal stays `DRAFT` - only the human approval path can decide it - and it describes the **managed** project; it never mutates the assistant's own baseline. Note that this label collides with `synthesize_proposal`'s label ("Generate Architecture Proposal") on purpose: one sits in the workbench, the other in the Proposal tab.

### deliberation_cancel

- **Button label:** `Cancel Deliberation` (group `Deliberation workbench`, `human=True`, `mutating=True`, reason prompt "Why is this deliberation being cancelled?")
- **Enabled when:** `available_actions` contain `CANCEL` for the run in view (a fresh, not-yet-started run offers no cancel).
- **Action:** `_deliberation_cancel_action(reason)` -> `worker.run_deliberation_stage(ACTION_CANCEL, actor, reason)`.
- **Core method:** `CoreWorker.run_deliberation_stage(...)` -> `ArchitectureDeliberation.cancel(...)`.
- **Writes to DB:** **yes** - the run's status and the audit entry.
- **Provider call:** no.
- **Blocks UI:** no; one transaction.
- **Notes:** cancelling is terminal for that run but **keeps its history**: the run and every artifact it already produced stay stored, and a new Round 1 starts a new run. It touches no workflow state, no baseline and no proposal already generated from that run.

### save_settings

- **Button label:** `Save Settings` (group `Advisor pane`, `mutating=False`)
- **Enabled when:** always unless `is_busy` - one of the seven `PROVIDER_SETTINGS_INTENTS`, so also available under CRITICAL.
- **Action:** `_save_settings_action()` captures `provider_payload()` (per slot: `provider`, `model`, `api_key`, where `None` means "keep the stored key") and returns `{"settings": worker.save_provider_settings(payload), "payload": worker.payload()}`.
- **Core method:** `CoreWorker.save_provider_settings(payload)` -> `composition.save_provider_settings`-equivalent path: the settings file is written atomically and, **only if the write succeeded**, `apply_provider_settings(composition, settings)` re-wires the running advisors.
- **Writes to DB:** **no** - the provider configuration is a local preference file (`data/provider_settings.json`, or `<project>/.architecture_assistant/providers.json`), never a table.
- **Provider call:** no (nothing is tested here; use `Test Connection` for that).
- **Blocks UI:** no; an atomic file write plus a re-wire, i.e. milliseconds.
- **Notes:** the API key is only ever the **typed** value; a stored key is never read back into a widget, and on a successful save the application empties the key entries (`window.clear_key_entries()`). A refused save changes nothing at all and sets the pane note `provider settings rejected (<ErrorName>)`; `apply_result` logs the failure with the exception **type name** only. Changing a slot silently changes which provider/model the **next** review or deliberation stage uses, which is the main risk of this intent.

### test_connection_1

- **Button label:** `Test Connection` (group `Advisor pane`) - the header control of **review pane 1**
- **Enabled when:** always unless `is_busy`; also available under CRITICAL.
- **Action:** `_test_connection_action("advisor_1")` (the pane key `test_connection_1` maps to the advisor slot `advisor_1`).
- **Core method:** `CoreWorker.test_connection(advisor="advisor_1", provider, model, api_key)` -> `probe_connection(provider, model, credential)` -> `AdvisorFactory` probe.
- **Writes to DB:** **no**.
- **Provider call:** **yes** - one minimal authenticated probe; no call at all when the pane is set to `Disabled`.
- **Blocks UI:** no - queued; the probe owns the core thread for its duration (bounded by the 30 s timeout, 3 attempts and 0/1/2 s backoff, i.e. up to ~93 s worst case).
- **Notes:** the verdict lands in `_connection_results["test_connection_1"]` and is rendered in the pane header; a non-`CONNECTED` verdict is logged at `WARN`. Keys are masked in the UI and never returned by the probe.

### test_connection_2

- **Button label:** `Test Connection` (group `Advisor pane`) - the header control of **review pane 2**
- **Enabled when:** always unless `is_busy`; also available under CRITICAL.
- **Action:** `_test_connection_action("advisor_2")` (slot `advisor_2`).
- **Core method:** `CoreWorker.test_connection(advisor="advisor_2", ...)` -> `probe_connection(...)`.
- **Writes to DB:** no.
- **Provider call:** yes (one minimal probe; none when `Disabled`).
- **Blocks UI:** no.
- **Notes:** identical to `test_connection_1`, different slot; each pane knows only its own slot, so one broken pane never blocks the others.

### test_connection_3

- **Button label:** `Test Connection` (group `Advisor pane`) - the header control of **review pane 3**
- **Enabled when:** always unless `is_busy`; also available under CRITICAL.
- **Action:** `_test_connection_action("advisor_3")` (slot `advisor_3`).
- **Core method:** `CoreWorker.test_connection(advisor="advisor_3", ...)` -> `probe_connection(...)`.
- **Writes to DB:** no.
- **Provider call:** yes (one minimal probe; none when `Disabled`).
- **Blocks UI:** no.
- **Notes:** the three pane slots (`advisor_1..3`) and the three deliberation slots (`agent_a`, `agent_b`, `lead`) are separate configuration entries, so changing a review pane never changes a deliberation seat.

### cost_details

- **Button label:** `Cost Details` (group `Windows`; **always enabled** - it is in `DISPLAY_INTENTS`)
- **Enabled when:** always, even while another action runs and even under CRITICAL.
- **Action:** **no core work.** `app._on_action` sees `key in POPUP_INTENTS` and opens/raises the window built by `controller.popup_view("cost_details")` -> `cost_popup()`. (`_action_for` raises `ValueError` for a popup key, so a popup can never be queued to the core.)
- **Core method:** none - it reshapes the payload the controller already holds (`cost`, `cost_by_step`).
- **Writes to DB:** no (the popup displays the last read; it never queries).
- **Provider call:** no.
- **Blocks UI:** no; window construction on the Tk thread only.
- **Notes:** window identity `popup.cost`, size category `large`; a second click **raises the existing window** instead of opening a duplicate, and its size/position is remembered in `data/gui_layout.json`. Because it renders the *last* payload, the numbers can be stale until `refresh` (or any other action) has read the projection again.

### open_logs

- **Button label:** `Logs` (group `Windows`; **always enabled**)
- **Enabled when:** always (display intent).
- **Action:** no core work - `popup_view("open_logs")` -> `logs_popup()`, fed by the controller's own in-memory log buffer (`deque(maxlen=200)`).
- **Core method:** none.
- **Writes to DB:** no.
- **Provider call:** no.
- **Blocks UI:** no.
- **Notes:** window identity `popup.logs`, size `large`. The buffer holds validated, sanitized events (panel-level entries plus drained core events); `Clear Logs` empties exactly this view.

### clear_logs

- **Button label:** `Clear Logs` (group `Windows`; **always enabled**)
- **Enabled when:** always (display intent).
- **Action:** handled by the application: `app._on_action` -> `_on_clear_logs()` -> `controller.clear_log_view()`.
- **Core method:** none.
- **Writes to DB:** **no** - deliberately: the persistent record is the append-only audit trail, which this path cannot reach (the controller holds no repository, connection or worker).
- **Provider call:** no.
- **Blocks UI:** no; it empties a deque (instant).
- **Notes:** the status line says "Log view cleared. Nothing was deleted: the persistent audit trail is on the Audit tab." Nothing else is touched - no audit row, no file, no workflow state.

### open_audit

- **Button label:** `Audit` (group `Windows`; **always enabled**)
- **Enabled when:** always (display intent).
- **Action:** no core work - `popup_view("open_audit")` -> `audit_popup()`, rendering the audit rows the controller already holds (from the last `audit_tail()`, at most 200, newest first).
- **Core method:** none (the data arrived with the last read; `CoreWorker.audit_tail()` is what produced it).
- **Writes to DB:** no.
- **Provider call:** no.
- **Blocks UI:** no.
- **Notes:** window identity `popup.audit`, size `large`; the table shows when / entity / action / event / actor / step / reason plus the raw detail payload. To see entries newer than the last read, press `Refresh` first.

### open_risks

- **Button label:** `Risks` (group `Windows`; **always enabled**)
- **Enabled when:** always (display intent).
- **Action:** no core work - `risks_popup()` over `payload["open_risks"]`.
- **Core method:** none.
- **Writes to DB:** no.
- **Provider call:** no.
- **Blocks UI:** no.
- **Notes:** window identity `popup.risks`, size `large`; it lists **open** risks only, as the monitor reported them (id, severity, probability, impact, owner, status, description, mitigation).

### open_reports

- **Button label:** `Reports` (group `Windows`; **always enabled**)
- **Enabled when:** always (display intent).
- **Action:** no core work - `reports_popup()`, built from the payload's `paths`, the artifacts this session exported (`_exports`) and the report directory.
- **Core method:** none.
- **Writes to DB:** no.
- **Provider call:** no.
- **Blocks UI:** no.
- **Notes:** window identity `popup.reports`, default size `medium`. It lists artifact paths only - it never reads a file or lists a directory by itself; `Open reports folder` is the intent that touches the filesystem.

### project_details

- **Button label:** `Project Details` (group `Windows`; **always enabled**)
- **Enabled when:** always (display intent).
- **Action:** no core work - `project_popup()` over the payload's `project`, `health`, `paths` and the runtime rows.
- **Core method:** none.
- **Writes to DB:** no.
- **Provider call:** no.
- **Blocks UI:** no.
- **Notes:** window identity `popup.project_details`, default size `medium`; it restates the persisted project identity (name, plan version, mode, paused flag) and the loop health the last read reported.

### architecture_details

- **Button label:** `Architecture Details` (group `Windows`; **always enabled**)
- **Enabled when:** always (display intent).
- **Action:** no core work - `architecture_popup()` over the payload's `architecture` (the persisted baseline version and its rules) plus the deterministic gate rows.
- **Core method:** none.
- **Writes to DB:** no.
- **Provider call:** no.
- **Blocks UI:** no.
- **Notes:** window identity `popup.architecture_details`, default size `medium`. This is the **assistant's own** baseline as persisted state - not the managed project's proposal, which lives in the Proposal tab.

### supervisor_technical

- **Button label:** `Technical Details` (group `Windows`; **always enabled**)
- **Enabled when:** always (display intent) - even when supervision is disabled, in which case the window explains that.
- **Action:** no core work - `supervisor_popup()` over the payload's `supervisor` board, with the identity, report, gate and runtime rows.
- **Core method:** none.
- **Writes to DB:** no.
- **Provider call:** no.
- **Blocks UI:** no.
- **Notes:** window identity `popup.supervisor_details`, size `large`; it shows the persisted supervision identity, the two malformed deadlines and the deterministic gate verdict (`ALLOW`/`BLOCK` plus its reason). A no-op supervision tick writes nothing, so reading this window never changes what it shows.

### review_judge_details

- **Button label:** `View Judge Details` (group `Windows`; **always enabled**)
- **Enabled when:** always (display intent).
- **Action:** no core work - `judge_popup()` over the last review payload's judge block.
- **Core method:** none (the judge was consulted, or not, during `run_review`).
- **Writes to DB:** no.
- **Provider call:** no.
- **Blocks UI:** no.
- **Notes:** window identity `popup.judge_details`, default size `medium`. It states explicitly when the judge was **not** consulted ("not used" / `not-consulted`), so an empty window is never mistaken for a favourable verdict; the judge's decision is labelled as explaining, never overriding, the deterministic gate.

### review_conflict_details

- **Button label:** `View Conflicts` (group `Windows`; **always enabled**)
- **Enabled when:** always (display intent).
- **Action:** no core work - `conflicts_popup()` over the last review's validated conflicts, with the judge status per anchor.
- **Core method:** none.
- **Writes to DB:** no.
- **Provider call:** no.
- **Blocks UI:** no.
- **Notes:** window identity `popup.conflicts`, default size `medium`; the table pairs each conflict anchor with its supporting and contradicting finding ids and the judge's verdict (or "-").

### review_evidence_details

- **Button label:** `Evidence` (group `Windows`; **always enabled**)
- **Enabled when:** always (display intent).
- **Action:** no core work - `evidence_popup()`, the merged-evidence table (advisor, status, severity, relation, anchor, evidence references).
- **Core method:** none.
- **Writes to DB:** no.
- **Provider call:** no.
- **Blocks UI:** no.
- **Notes:** window identity `popup.evidence_details`, size `large`. Abstaining and failing advisors are listed explicitly (with their reason), so an unanswered advisor is visible instead of silently missing.

### review_decision_details

- **Button label:** `Decision` (group `Windows`; **always enabled**)
- **Enabled when:** always (display intent).
- **Action:** no core work - `decision_popup()`, the advisory decision plus the deterministic gate anchors.
- **Core method:** none.
- **Writes to DB:** no.
- **Provider call:** no.
- **Blocks UI:** no.
- **Notes:** window identity `popup.decision_details`, default size `medium`; the panel marks the advisory decision as *explains, never overrides*, so the deterministic gate remains the only verdict.

### advisor_details_1

- **Button label:** `Details...` (group `Windows`; **always enabled**) - the detail button of **review pane 1**
- **Enabled when:** always (display intent).
- **Action:** no core work - `popup_view("advisor_details_1")` -> `advisor_popup(0)`, rendering that pane's provider, model, status, relation, anchor, cost and the full answer/error for the last review.
- **Core method:** none.
- **Writes to DB:** no.
- **Provider call:** no.
- **Blocks UI:** no.
- **Notes:** window identity `popup.advisor_details_1`, default size `medium`. Before the first review the pane is an explicit idle panel ("not run yet") rather than an invented result.

### advisor_details_2

- **Button label:** `Details...` (group `Windows`; **always enabled**) - the detail button of **review pane 2**
- **Enabled when:** always (display intent).
- **Action:** no core work - `advisor_popup(1)`.
- **Core method:** none.
- **Writes to DB:** no.
- **Provider call:** no.
- **Blocks UI:** no.
- **Notes:** window identity `popup.advisor_details_2`; same content shape as pane 1 for its own slot.

### advisor_details_3

- **Button label:** `Details...` (group `Windows`; **always enabled**) - the detail button of **review pane 3**
- **Enabled when:** always (display intent).
- **Action:** no core work - `advisor_popup(2)`.
- **Core method:** none.
- **Writes to DB:** no.
- **Provider call:** no.
- **Blocks UI:** no.
- **Notes:** window identity `popup.advisor_details_3`. A configured advisor is never hidden: if fewer than three are configured the tab pads the row with explicit idle panes, and a fourth configured advisor would get its own pane instead of being dropped (`MIN_PROVIDER_PANES = 3` is a floor, not a cap).

### synthesize_proposal

- **Button label:** `Generate Architecture Proposal` (group `Proposal`, `human=True`, reason prompt "Why is this managed-project proposal being generated?", confirmation: "Generating stores a new durable proposal for the managed project and writes one audit entry. It changes no workflow state and never touches the assistant's own architecture baseline. Continue?")
- **Enabled when:** `proposal_review_available()` - `CoreWorker._last_review is not None`, i.e. a review was run **in this process**. The refusal explains it: "An architecture proposal needs one review as its evidence: run the architecture review first."
- **Action:** `_proposal_action("synthesize_proposal", reason)` - it captures the requirement addendum, the actor, the reason and `revision_of` (set only when the latest proposal is `REVISION_REQUESTED`) on the UI thread, then calls the worker.
- **Core method:** `CoreWorker.synthesize_proposal(requirement, actor, reason, revision_of)` -> `composition.architecture_synthesis.synthesize(review, ...)`.
- **Writes to DB:** **yes** - one proposal row plus exactly one audit entry in one transaction; when a revision is generated the predecessor is marked `SUPERSEDED` (both rows remain).
- **Provider call:** **conditional** - none with the shipped deterministic organizer; one synthesizer call per generation if a synthesizer is configured (`has_synthesizer`), and a synthesizer answering outside the closed content contract is refused with **nothing stored**.
- **Blocks UI:** no; milliseconds with the deterministic organizer, a provider call otherwise.
- **Notes:** the review is the evidence and it is **ephemeral**: `proposal_review_available()` is memory-only, so after a restart (or a `reconnect` that re-composes) this button is disabled again until a new review is run, even though proposals from earlier sessions are still visible on the board.

### approve_proposal

- **Button label:** `Approve` (group `Proposal`, `human=True`, reason prompt "Why is this managed-project design approved?")
- **Enabled when:** `proposal_decidable()` - the latest proposal's `status == "DRAFT"` (plus actor and reason). The refusal names the rule: "No DRAFT architecture proposal is available: generate one first (a decided or superseded proposal can never be decided again)."
- **Action:** `_proposal_action("approve_proposal", reason)` - the target id is resolved from the board on the UI thread, then `worker.approve_proposal(proposal_id, actor, reason)`.
- **Core method:** `CoreWorker.approve_proposal(...)` -> `composition.proposal_approval.approve(...)` (`ProposalApproval.approve`).
- **Writes to DB:** **yes** - exactly one status change plus exactly one audit entry in one transaction.
- **Provider call:** no.
- **Blocks UI:** no; one transaction (milliseconds) plus a fresh board and audit read.
- **Notes:** the approval is validated against the proposal's **fingerprint**, so a proposal whose stored inputs no longer match is refused *before* the transaction and nothing is written. Repeating the identical decision is a no-op; repeating it with a different reason fails closed instead of rewriting history. This is the intent that unlocks `prepare_execution_plan`, and it approves the **managed project's** design only - it creates no change request and never bumps the assistant's own baseline.

### request_proposal_revision

- **Button label:** `Request Revision` (group `Proposal`, `human=True`, reason prompt "Why is a revision requested?")
- **Enabled when:** `proposal_decidable()` (the latest proposal is `DRAFT`) **and** a non-empty revision feedback; the extra refusal is explicit: "Revision feedback is required: write what the managed project's design should change."
- **Action:** `_proposal_action("request_proposal_revision", reason)` -> `worker.request_proposal_revision(proposal_id, actor, reason, feedback)`.
- **Core method:** `CoreWorker.request_proposal_revision(...)` -> `composition.proposal_approval.request_revision(...)`.
- **Writes to DB:** **yes** - one status change (`REVISION_REQUESTED`) plus one audit entry.
- **Provider call:** no.
- **Blocks UI:** no; one transaction.
- **Notes:** it **never re-synthesizes by itself**: the feedback is recorded, and the next revision exists only when the operator explicitly presses `Generate Architecture Proposal` again - at which point the predecessor becomes `SUPERSEDED` and both rows remain.

### reject_proposal

- **Button label:** `Reject` (group `Proposal`, `human=True`, reason prompt "Why is this managed-project proposal rejected?")
- **Enabled when:** `proposal_decidable()` (the latest proposal is `DRAFT`).
- **Action:** `_proposal_action("reject_proposal", reason)` -> `worker.reject_proposal(proposal_id, actor, reason)`.
- **Core method:** `CoreWorker.reject_proposal(...)` -> `composition.proposal_approval.reject(...)`.
- **Writes to DB:** **yes** - one status change plus one audit entry.
- **Provider call:** no.
- **Blocks UI:** no; one transaction.
- **Notes:** a rejected proposal can never be decided again; the row and its audit history stay (nothing is deleted), and the operator can still generate a fresh proposal from a new review.

### analyze_report

- **Button label:** `Analyze Report` (group `Supervisor`, `human=True`, `mutating=False`, reason prompt "Why is this report being supervised?")
- **Enabled when:** `supervisor_action_available("analyze_report")` = supervision enabled **and** the supervision payload carries a `current_report_hash`. The refusal text distinguishes the cases ("Supervision is disabled for this session..." / "There is no current worker report to analyse...").
- **Action:** `_supervisor_action("analyze_report", reason)` -> `worker.analyze_report()`.
- **Core method:** `CoreWorker.analyze_report()` -> `composition.supervision.analyze(step_no, attempt, on_event=...)`, resolving step and attempt inside the core thread.
- **Writes to DB:** **yes** - the supervision record is written **before** the supervisor is asked (so a crash can never lose the fact that an analysis was owed), plus its audit entry.
- **Provider call:** **yes in the code path** (`SupervisorPort`), but with the shipped composition the adapter is the offline `ScriptedSupervisor`, which makes **no network call**; a real supervisor adapter would.
- **Blocks UI:** no; milliseconds with the scripted double, a provider call with a real one.
- **Notes:** idempotent by identity - a report that already has a decided record answers `already-supervised` instead of being analysed twice, so a double click cannot produce a second analysis. The panel never sends a supervision id: the core resolves the current report identity itself, so a stale panel cannot supervise a report the workflow has moved past.

### approve_and_send

- **Button label:** `Approve & Send` (group `Supervisor`, `human=True`, reason prompt "Why is this directive approved for repair?", confirmation: "Publishing the directive writes the exchange channel artifact Cline reads next. It changes no workflow state, cannot verify anything and cannot increment the attempt. Continue?")
- **Enabled when:** the supervision status is in `SUPERVISION_SEND_STATUSES` (`WAITING_HUMAN`, `READY_TO_SEND`) **and** either the typed instruction or the supervisor's proposed instruction is non-empty.
- **Action:** `_supervisor_action("approve_and_send", reason)` - the instruction text is captured **on the UI thread** before the callable is queued, then `worker.approve_and_send(instruction, actor, reason)`.
- **Core method:** `CoreWorker.approve_and_send(...)` -> `composition.supervision.approve_and_send(resolved supervision_id, ...)` -> `supervisor_policy` decides, then the directive is published.
- **Writes to DB:** **yes** - the record's status/decision plus its audit entry.
- **Provider call:** no (the analysis already happened; sending is deterministic).
- **Blocks UI:** no; one transaction plus one atomic file publication (milliseconds).
- **Notes:** publishing writes the directive artifact into the exchange channel (`to_cline/<step>_attempt_<n>_directive.json`) - the **only** intent with an effect outside the database and the panel's own preference files. It cannot verify anything, cannot increment the attempt, and the deterministic send policy can still refuse it. `Reject Directive`, `Waive` and `Escalate` are the alternatives. A record already in `SENT` offers no further decision: it is resolved only by a **new** worker report.

### reject_directive

- **Button label:** `Reject Directive` (group `Supervisor`, `human=True`, reason prompt "Why is this supervisor directive rejected?")
- **Enabled when:** the supervision status is in `SUPERVISION_DECIDABLE_STATUSES` and is not already `REJECTED`.
- **Action:** `_supervisor_action("reject_directive", reason)` -> `worker.reject_directive(actor, reason)`.
- **Core method:** `CoreWorker.reject_directive(...)` -> `composition.supervision.reject_directive(resolved_id, ...)`.
- **Writes to DB:** **yes** - the record plus one audit entry.
- **Provider call:** no.
- **Blocks UI:** no; one transaction.
- **Notes:** rejecting the directive allows the authoritative review to proceed (the report is not approved for repair). It publishes no file, so nothing reaches Cline.

### waive_supervision

- **Button label:** `Waive` (group `Supervisor`, `human=True`, reason prompt "Why is supervision waived for this exact report?")
- **Enabled when:** the supervision status is in `SUPERVISION_DECIDABLE_STATUSES` and is not already `WAIVED`.
- **Action:** `_supervisor_action("waive_supervision", reason)` -> `worker.waive_supervision(actor, reason)`.
- **Core method:** `CoreWorker.waive_supervision(...)` -> `composition.supervision.waive_supervision(resolved_id, ...)`.
- **Writes to DB:** **yes** - the record plus one audit entry.
- **Provider call:** no.
- **Blocks UI:** no; one transaction.
- **Notes:** waiving is scoped to **that exact report identity** (step, attempt and report hash): it lets the gate allow this one report through and cannot be reused for a later revision of the same attempt. It is the highest-trust decision in the Supervisor tab, because it silently unblocks the workflow for that report.

### escalate_supervision

- **Button label:** `Escalate` (group `Supervisor`, `human=True`, reason prompt "Why is this supervision escalated to a human decision?")
- **Enabled when:** the supervision status is in `SUPERVISION_DECIDABLE_STATUSES` and is not already `ESCALATED`.
- **Action:** `_supervisor_action("escalate_supervision", reason)` -> `worker.escalate_supervision(actor, reason)`.
- **Core method:** `CoreWorker.escalate_supervision(...)` -> `composition.supervision.escalate(resolved_id, ...)`.
- **Writes to DB:** **yes** - the record plus one audit entry.
- **Provider call:** no.
- **Blocks UI:** no; one transaction.
- **Notes:** escalating deliberately hands the decision to a human instead of answering it here, so the loop keeps waiting (the report reaches neither the realization gate nor the acknowledgement) until a human acts.

### run_until_idle

- **Button label:** `Run Until Idle` (group `Loop`; `human=False`, `mutating` left at its default `True`)
- **Enabled when:** the project is not paused **and** `step_count() > 0`. The hint for a disabled button is "Import an execution plan and resume the project first."
- **Action:** `_mutation_action("run_until_idle")` -> `worker.run_until_idle()`, then `worker.payload()` and `worker.audit_tail()`.
- **Core method:** `CoreWorker.run_until_idle()` -> `Composition.run_until_idle()` -> `Scheduler.run_until_idle()` (bounded to `max_iterations`, 64 by default) -> repeated `Orchestrator.run_once()`.
- **Writes to DB:** **yes** - every transition it makes is persisted with exactly one audit entry in one transaction, plus task rows on dispatch, plus the realization gate's findings/decisions, plus cost rows from any provider the reviewed report path touches.
- **Provider call:** no - the loop itself never calls a provider. (With supervision enabled, an analysis is triggered by the panel's 2-second tick, not by this press.)
- **Blocks UI:** no - queued; measured **8.5 ms** for a 12-step project that stops at `human-approval-required` (each idle tick 0.475-0.520 ms), plus **708-942 ms** when a tick reaches `VERIFY` and runs the architecture scan. In short: milliseconds normally, up to about a second when a step is verified.
- **Notes:** one press can chain several transitions (up to 64) - it is both the routine "advance" button and the most consequential, because a single press can verify steps. It stops for human approval, a blocked or conflicting step, a paused project, an aborted step, completion, an exhausted retry budget or the iteration limit; the log line is "Loop stopped: `<reason>` (N transitions)." It never retries without budget and never reaches `VERIFIED` without the deterministic gate.

### pause_project

- **Button label:** `Pause` (group `Project`, `human=True`, reason prompt "Why is the project being paused?")
- **Enabled when:** the project is **not** currently paused (from the projection's `project.paused`).
- **Action:** `_mutation_action("pause_project", reason)` -> `worker.pause_project(actor, reason)`.
- **Core method:** `CoreWorker.pause_project(...)` -> `composition.human_override.pause_project(actor=..., reason=...)`.
- **Writes to DB:** **yes** - the project row's paused flag plus exactly one audit entry in one transaction.
- **Provider call:** no.
- **Blocks UI:** no; one transaction (milliseconds) plus a fresh payload.
- **Notes:** pausing is the loop's own stop condition: `run_until_idle` is disabled while paused and a tick reports `project-paused`. It stops *new* progress only - a running action is never interrupted (there is no cancellation), and the flag survives a restart because it is persisted.

### resume_project

- **Button label:** `Resume` (group `Project`, `human=True`, reason prompt "Why is the project being resumed?")
- **Enabled when:** the project **is** paused.
- **Action:** `_mutation_action("resume_project", reason)` -> `worker.resume_project(actor, reason)`.
- **Core method:** `CoreWorker.resume_project(...)` -> `composition.human_override.resume_project(actor=..., reason=...)`.
- **Writes to DB:** **yes** - the paused flag plus one audit entry.
- **Provider call:** no.
- **Blocks UI:** no; one transaction.
- **Notes:** exactly the inverse of `Pause`; the two are mutually exclusive by their enable rules, so the panel never offers "pause an already paused project". A supervision record left `SEND_PENDING`/`ANALYSIS_PENDING` by an earlier enabled session is reconciled on the next tick once supervision is enabled again.

### approve

- **Button label:** `Approve` (group `Approval`, `states={WAITING_APPROVAL}`, `needs_step=True`, `human=True`, reason prompt "Why is this approval granted?")
- **Enabled when:** the current step's persisted state is exactly `WAITING_APPROVAL` **and** a current step exists (plus actor and reason).
- **Action:** `_mutation_action("approve", reason)` -> `worker.approve(actor, reason)`.
- **Core method:** `CoreWorker.approve(...)` -> `self._current_step_number()` (resolved inside the core thread) -> `composition.approval.approve(step_no, actor=..., reason=...)` (`ApprovalGate`).
- **Writes to DB:** **yes** - the `WAITING_APPROVAL -> DISPATCHED` transition, **a task row** and exactly one audit entry in one transaction.
- **Provider call:** no.
- **Blocks UI:** no; one transaction plus the deterministic task/context publication (a few milliseconds - the measured 12-row plan import costs 2.95 ms, so a single-row dispatch is well below that).
- **Notes:** it publishes the **same** deterministic task and context artifacts the loop publishes, so a persisted dispatch never exists without its published task. The step number is resolved inside the core thread, so a stale panel cannot approve the wrong step; an illegal or repeated approval fails closed and writes (and publishes) nothing. Approval verifies nothing - the worker still has to deliver a report the gate accepts.

### reject

- **Button label:** `Reject` (group `Approval`, `states={WAITING_APPROVAL}`, `needs_step=True`, `human=True`, reason prompt "Why is this approval rejected?")
- **Enabled when:** identical to `approve` (`WAITING_APPROVAL` + a current step + actor + reason).
- **Action:** `_mutation_action("reject", reason)` -> `worker.reject(actor, reason)`.
- **Core method:** `CoreWorker.reject(...)` -> `composition.approval.reject(step_no, actor=..., reason=...)`.
- **Writes to DB:** **yes** - the `WAITING_APPROVAL -> READY` transition plus exactly one audit entry.
- **Provider call:** no.
- **Blocks UI:** no; one transaction.
- **Notes:** it publishes **nothing** and touches no task - rejecting an approval is not a rejection of the work. The step returns to `READY` and the loop evaluates the approval policy again on its next tick, so `run_until_idle` may ask for approval a second time (the same request can legitimately come back). Use `Abort` to end the step instead.

### unblock

- **Button label:** `Unblock` (group `Step control`, `states={BLOCKED}`, `needs_step=True`, `human=True`, reason prompt "Which blocker was resolved?")
- **Enabled when:** the current step's state is `BLOCKED` and a current step exists.
- **Action:** `_mutation_action("unblock", reason)` -> `worker.unblock(actor, reason)`.
- **Core method:** `CoreWorker.unblock(...)` -> `composition.human_override.unblock_step(step_no, actor=..., reason=...)`.
- **Writes to DB:** **yes** - the step transition plus exactly one audit entry.
- **Provider call:** no.
- **Blocks UI:** no; one transaction.
- **Notes:** this is the human assertion "the blocker is really gone"; the loop re-evaluates the step on the next tick. A step blocked at an exhausted attempt budget can be unblocked and re-dispatched, and that re-dispatch is **operator-controlled** (the attempt counter counts retries, not physical dispatches) - so unblocking repeatedly at an exhausted budget repeats work that the loop itself would have refused.

### resolve

- **Button label:** `Resolve` (group `Step control`, `states={CONFLICT}`, `needs_step=True`, `human=True`, reason prompt "How was the conflict resolved?")
- **Enabled when:** the current step's state is `CONFLICT` and a current step exists.
- **Action:** `_mutation_action("resolve", reason)` -> `worker.resolve(actor, reason)`.
- **Core method:** `CoreWorker.resolve(...)` -> `composition.human_override.resolve_step(step_no, actor=..., reason=...)`.
- **Writes to DB:** **yes** - the step transition plus exactly one audit entry.
- **Provider call:** no.
- **Blocks UI:** no; one transaction.
- **Notes:** `CONFLICT` is the loop's halt for an evidence conflict or an unresolvable question; resolving it is a **human judgement** recorded with actor and reason, and the advisory evidence (and any judge verdict) stays exactly as it was - the human's decision is the one that moves the workflow.

### abort

- **Button label:** `Abort` (group `Step control`, `states={WAITING_APPROVAL, BLOCKED, CONFLICT, FAILED}`, `needs_step=True`, `human=True`, reason prompt "Why is this step being aborted?", confirmation "Aborting is terminal for this step. Continue?")
- **Enabled when:** the current step's state is one of `WAITING_APPROVAL`, `BLOCKED`, `CONFLICT`, `FAILED` and a current step exists. Note it is **not** offered in `REVISE`, `DISPATCHED`, `CLINE_WORKING` or `REPORT_RECEIVED`.
- **Action:** `_mutation_action("abort", reason)` -> `worker.abort(actor, reason)`.
- **Core method:** `CoreWorker.abort(...)` -> `composition.human_override.abort_step(step_no, actor=..., reason=...)`.
- **Writes to DB:** **yes** - the step transition to `ABORTED` plus exactly one audit entry.
- **Provider call:** no.
- **Blocks UI:** no; one transaction.
- **Notes:** `ABORTED` is a **terminal** step state with no exit in the transition table, so this is the one step-bound action that cannot be undone by any later press. It also stops the project from ever being "complete" (completeness requires every step `VERIFIED`), which is why the panel asks for confirmation first.

### export_markdown

- **Button label:** `Export Markdown` (group `Reports`; `mutating` left at its default `True`)
- **Enabled when:** no extra condition beyond "not busy and not CRITICAL" - `enabled()` falls through to `True`.
- **Action:** `_export_action("export_markdown")` -> `worker.export_markdown()`, then `worker.payload()` and `worker.audit_tail()`.
- **Core method:** `CoreWorker.export_markdown()` -> `_export("markdown")` -> `composition.report_builder.build()` then `composition.markdown_reporting.render(payload)` (`ReportingPort.render`).
- **Writes to DB:** **no** - the projection is read-only and the renderer only reads the payload it is handed.
- **Provider call:** no.
- **Blocks UI:** no; measured **1.6 ms** for the render plus a projection build (2.57 ms at 12 steps, up to 53 ms at 500).
- **Notes:** this **writes a file** into the report directory (`reports/` by default), which is deliberately separate from the worker exchange directory. The returned path is remembered in `_exports` (so the Reports popup can list it) and logged as "Artifact written: <path>". Exporting is a projection of the current state - it does not freeze it, and it changes no workflow state.

### export_excel

- **Button label:** `Export Excel` (group `Reports`; `mutating` default `True`)
- **Enabled when:** no extra condition beyond "not busy and not CRITICAL".
- **Action:** `_export_action("export_excel")` -> `worker.export_excel()`, then a fresh payload and audit tail.
- **Core method:** `CoreWorker.export_excel()` -> `_export("excel")` -> `composition.report_builder.build()` then `composition.excel_reporting.render(payload)`.
- **Writes to DB:** no.
- **Provider call:** no.
- **Blocks UI:** no; measured **20.0 ms** for the XLSX render (it builds the workbook with `zipfile` + `xml`, no third-party spreadsheet library) plus the projection build.
- **Notes:** one snapshot, two artifacts: Excel and Markdown render the *same* `ReportSnapshot`, so the two files can never disagree about the state they describe.

### open_reports_folder

- **Button label:** `Open reports folder` (group `Reports`, `mutating=False`)
- **Enabled when:** always unless `is_busy` (it is in the always-allowed set), so it works under CRITICAL too.
- **Action:** handled by the application: `app._on_action` -> `self._open_folder()` -> `subprocess.Popen` of the OS file manager rooted at the report directory (falling back to the project root). No core action is queued.
- **Core method:** none.
- **Writes to DB:** no.
- **Provider call:** no.
- **Blocks UI:** effectively no; it spawns a separate process rather than blocking the Tk thread (the launched file manager is a different process, so a slow shell cannot freeze the panel).
- **Notes:** it is the only intent that hands control to the operating system. It reads no file and lists no directory itself - the listing is the file manager's job.

### refresh

- **Button label:** `Refresh` (group `Monitoring`, `mutating=False`)
- **Enabled when:** always unless `is_busy` (always-allowed set; available under CRITICAL).
- **Action:** `_read_action()` -> `worker.payload()`, `worker.audit_tail()`, `worker.proposal_board()`, `worker.supervisor_status()` and (when the worker exposes it) `worker.deliberation_board()`.
- **Core method:** the five `CoreWorker` read methods above; `payload()` itself calls `monitor.to_dict()` plus the monitor selectors (`current_step`, `next_step`, `blocking_steps`, `open_risks`, `architecture_version`) and `channel_status()`.
- **Writes to DB:** **no** - but it **reads the whole audit table** (the audit adapter's `list()` has no `LIMIT`; the 200-row tail is sliced in Python) plus the full projection.
- **Provider call:** no.
- **Blocks UI:** no; the core-thread cost grows with data: ~5 ms at 12 steps / 234 audit rows, and roughly 0.4 s at 500 steps / 42k audit rows (measured: monitor payload 81 ms at 500 steps, audit list 187 ms at 22k rows / 343 ms at 42k).
- **Notes:** the application submits `refresh` **by itself** once after the core reports `open`, so the panel's first payload needs no operator press. It is also the action that clears a CRITICAL state: a successful payload read sets `_critical = False` and drops the last error, which re-enables the mutating buttons.

### view_snapshot

- **Button label:** `View Monitor Snapshot` (group `Monitoring`; **always enabled** - display intent)
- **Enabled when:** always, including while busy and after a CRITICAL failure.
- **Action:** no core work - `popup_view("view_snapshot")` -> `snapshot_popup()`, which pretty-prints `snapshot_json()` (the last `canonical` payload, `indent=2, sort_keys=True`) into a scrollable window.
- **Core method:** none.
- **Writes to DB:** no.
- **Provider call:** no.
- **Blocks UI:** no; JSON serialisation of the last payload (25,572 characters at 12 steps) on the Tk thread - sub-millisecond to a few milliseconds.
- **Notes:** window identity `popup.snapshot`, size `large`. Nothing is reformatted, summarised or interpreted: it is the projection the core already produced, so it is the most literal "what does the core currently say" view in the panel.

### reconnect

- **Button label:** `Reconnect` (group `Monitoring`, `mutating=False`)
- **Enabled when:** always unless `is_busy` (always-allowed set; available under CRITICAL - which is exactly when it is needed).
- **Action:** `_reconnect_action()` -> `worker.reconnect()`, then `worker.payload()`, `worker.audit_tail()`, `worker.proposal_board()`.
- **Core method:** `CoreWorker.reconnect()` -> `CoreWorker.close()` + `CoreWorker.open()` -> `compose(config)` again (open the database, apply pending migrations, reconcile the baseline, rebuild every adapter, re-read the provider settings).
- **Writes to DB:** **no domain write** - but re-composing **applies pending migrations**, so a database behind the current schema (for example the pilot file at migration v4 while the code has 7) writes its schema bookkeeping rows on the way in. No workflow state, audit row or baseline is rewritten.
- **Provider call:** no.
- **Blocks UI:** no; measured cold `compose()` is 41.6-78.4 ms, plus the fresh reads that follow (about 5 ms at 12 steps, up to ~0.4 s with a large audit table).
- **Notes:** it is a repair for a broken *connection*, never for broken *state*: it re-creates the object graph and cannot change a step, bypass a transition or "fix" a conflict. Two consequences worth knowing: the panel is then reading through brand-new objects (so a stale payload is refreshed), and the in-memory last review **survives** (`_last_review` lives on the worker, not on the composition), so `synthesize_proposal` stays enabled after a reconnect and would consume a review taken before it.

---

## 3. Dependency graph

Which intents must run before another can be enabled. Read the arrows as "produces the condition
that enables"; the bracketed text is the exact check in `enabled()`.

```text
   NO DEPENDENCY - available from the first payload onwards
   popups x16  o--  clear_logs  o--  save_settings  o--  test_connection_1..3
                o--  test_agent_a / test_agent_b / test_lead
                o--  refresh  o--  view_snapshot  o--  reconnect
                o--  export_markdown  o--  export_excel  o--  open_reports_folder

   ONBOARDING
   load_plan [step_count() == 0]  -->  import_plan [preview.importable]
        |
        v
   run_until_idle [step_count() > 0 and not paused]

   DESIGN (two independent routes into the same proposal aggregate)
   run_review  -->  synthesize_proposal [_last_review set in THIS process]
                         |
                         +--> approve_proposal [latest DRAFT] --> prepare_execution_plan
                         |        [step_count() == 0 and latest status APPROVED]
                         +--> request_proposal_revision [latest DRAFT + non-empty feedback]
                         |        --> synthesize_proposal again (predecessor SUPERSEDED)
                         +--> reject_proposal [latest DRAFT]

   deliberation_round1 [non-empty brief]  -->  deliberation_lead_review
        -->  deliberation_round2  -->  deliberation_synthesis  -->  deliberation_proposal
        -->  (the same approve / reject / request-revision decisions as above)
   deliberation_cancel is offered at every stage once the run exists

   EXECUTION LOOP
   run_until_idle  -->  WAITING_APPROVAL  -->  approve / reject
                    -->  DISPATCHED / CLINE_WORKING  -->  run_until_idle (collect report)
                    -->  BLOCKED   -->  unblock  -->  run_until_idle again
                    -->  CONFLICT  -->  resolve  -->  run_until_idle again
                    -->  FAILED / REVISE  -->  run_until_idle (retry while budget remains)
                    -->  abort (terminal) from WAITING_APPROVAL, BLOCKED, CONFLICT or FAILED

   SUPERVISION (requires --supervision; off by default)
   run_until_idle dispatches and collects a report  -->  analyze_report [current_report_hash]
        -->  approve_and_send [WAITING_HUMAN | READY_TO_SEND + an instruction]
        |    reject_directive / waive_supervision / escalate_supervision
        |    [a DECIDABLE status that is not already that decision]
        v
     a NEW worker report (the only way a SENT directive is resolved)
```

**The gating facts behind the graph**

| Gate | Why it exists |
| --- | --- |
| `load_plan` needs `step_count() == 0` | v1.1.1 imports an **initial** plan only - there is no merge, replace or renumber. Once steps exist the button is disabled for the life of that database. |
| `import_plan` needs a finished preview | the preview is the only thing that ever sets `_plan_preview`, and picking a new file clears it, so a stale verdict can never be imported. |
| `prepare_execution_plan` needs `step_count() == 0` **and** an `APPROVED` proposal | the plan is derived from the approved managed-project design, and a project that already has steps is never given a second plan. |
| `synthesize_proposal` needs `_last_review` | the review **is** the evidence. It is memory-only, so it does not survive a restart. |
| proposal decisions need `latest.status == "DRAFT"` | a decided proposal is final; the next revision is a new row and marks its predecessor `SUPERSEDED`. |
| deliberation stages need the core's `available_actions` | the two-round protocol is staged; the core - not the panel - decides which verb is legal at this point of the run. |
| step-bound intents need `states` **and** `needs_step` | `approve`/`reject` only in `WAITING_APPROVAL`; `unblock` only in `BLOCKED`; `resolve` only in `CONFLICT`; `abort` only in four halt states - all read from the **persisted** step the core last reported. |
| `pause_project` / `resume_project` are mutually exclusive | each requires the opposite of the current paused flag, so exactly one of the two is ever enabled. |
| every non-display intent needs `not is_busy` | one job at a time: while the core thread is occupied, a click is dropped rather than queued. |
| every non-display, non-always-allowed intent needs `not _critical` | after a critical core failure the panel refuses to start more work; a successful read that returns a fresh payload clears the flag. |
| supervisor intents additionally need supervision **and** a current report | with supervision off the whole group is disabled and explains itself; with it on, `analyze_report` needs a report hash and the decisions need a decidable status. |

---

## 4. Risk map

Ordered by how much an operator can lose. Class legend:

- **[TERMINAL]** - cannot be undone by any later press.
- **[DURABLE]** - writes a record that will never be deleted again.
- **[EXTERNAL]** - has an effect outside the database (files that Cline or the OS will read).
- **[COST]** - spends provider money.
- **[MOMENTUM]** - can advance the workflow several steps in one press.
- **[BYPASS]** - deliberately lets something proceed that the normal path would stop.
- **[PREF]** - overwrites a local preference.

| Intent | Class | What can go wrong | Mitigation in the code | What remains irreversible |
| --- | --- | --- | --- | --- |
| `abort` | **[TERMINAL]** | the step can never be verified again; the project can never be "complete" | confirmation dialog, `states={WAITING_APPROVAL, BLOCKED, CONFLICT, FAILED}`, actor + reason, one audited transaction | the `ABORTED` state itself - the FSM has no exit from it |
| `import_plan` | **[DURABLE]** | the initial plan is written forever; steps cannot be removed, merged or renumbered afterwards | full-file validation first; refuses a non-empty database, a foreign project/plan version and unknown fields; nothing is written on any problem; one transaction | the imported steps and the one audit entry |
| `approve_proposal` | **[DURABLE]** + unlock | records a human decision on a design and unlocks `prepare_execution_plan` | fingerprint validation before the transaction; a different reason after a decision fails closed; an identical repeat is a no-op | the `APPROVED` status |
| `reject_proposal` | **[DURABLE]** | the proposal can never be decided again | requires `DRAFT`; the refusal text says so | the `REJECTED` status |
| `request_proposal_revision` | **[DURABLE]** | records feedback and marks the proposal `REVISION_REQUESTED` | requires non-empty feedback; never re-synthesizes by itself | the recorded feedback |
| `synthesize_proposal` | **[DURABLE]** / **[COST]** | stores a new durable proposal (and supersedes the previous one when revising); costs money if a synthesizer is configured | needs a review from this process; a synthesizer answering outside the closed contract is refused with nothing stored | the new proposal row |
| `deliberation_proposal` | **[DURABLE]** | stores a durable proposal bound to one synthesis | the core decides `GENERATE_PROPOSAL` availability | the proposal row and its link to the synthesis |
| `deliberation_cancel` | **[TERMINAL]** (for the run) | ends the deliberation | the core decides `CANCEL` availability; history is kept | the run's terminal status |
| `approve_and_send` | **[EXTERNAL]** / **[DURABLE]** | publishes a directive file that Cline reads **next** | confirmation dialog, deterministic send policy, instruction captured before the call, cannot verify or increment the attempt | the delivered artifact - the operator can send a follow-up directive but cannot unsend this one |
| `waive_supervision` | **[BYPASS]** / **[DURABLE]** | lets an unsupervised report reach the authoritative review | scoped to the exact report identity (step + attempt + report hash); requires actor + reason | the waiver, which a new report does not inherit |
| `analyze_report` | **[DURABLE]** / **[COST]** with a real supervisor | writes a supervision record before the supervisor is asked | idempotent by identity; the record is written first so a crash cannot lose the obligation | the persisted record |
| `escalate_supervision` | **[DURABLE]** | hands the decision to a human and keeps the loop waiting | requires a decidable status and not already `ESCALATED` | the escalated record |
| `reject_directive` | **[DURABLE]** | allows the review to proceed | requires a decidable status and not already `REJECTED` | the recorded decision |
| `run_until_idle` | **[MOMENTUM]** / **[COST]** indirectly | one press can chain up to 64 transitions, dispatch work, verify steps and even abort a step whose retry budget is exhausted; a verification tick runs the realization scan (708-942 ms) inside its transaction | bounded by `max_iterations`; every transition is one audited transaction; the gate can only block, never be bypassed; the loop stops at every human decision point | the transitions it made - each is persisted and audited |
| `approve` (approval) | **[EXTERNAL]** / **[DURABLE]** | publishes the task + context artifacts and dispatches a step to Cline | resolves the step number inside the core thread; an illegal or repeated approval fails closed and publishes nothing | the published artifacts and the `DISPATCHED` state |
| `reject` (approval) | **[DURABLE]** | returns the step to `READY`, so the policy can ask for approval again | writes one transition + one audit entry and publishes nothing | the audit entry (the state change itself is recoverable by design) |
| `unblock` | **[DURABLE]** | re-dispatches a step the loop considered blocked - at an exhausted budget this repeats work the loop itself refused | `states={BLOCKED}` only; actor + reason required | the audit entry; repeated unblocks keep re-dispatching |
| `resolve` | **[DURABLE]** | a human judgement replaces the loop's halt for a conflict | `states={CONFLICT}` only; actor + reason required | the recorded judgement |
| `pause_project` / `resume_project` | **[DURABLE]** (a reversible pair) | stops or restarts progress | each requires the opposite paused flag; actor + reason required | nothing a second press cannot reverse |
| `run_review` | **[COST]** | spends provider money (the confirmation says so) | every advisor is optional; a missing key means "unavailable", not a crash; `Disabled` makes no call; the result is advisory and writes no workflow state | the money spent, and the cost rows it recorded |
| `test_connection_1..3`, `test_agent_a/b`, `test_lead` | **[COST]** (minimal) | sends the key and a tiny request to the chosen provider | one minimal probe per press; `Disabled` makes no call | the request itself |
| `save_settings` | **[PREF]** | silently changes which provider/model/key an advisor uses from the next call onwards | atomic write, applied only when the write succeeded; masked keys; a refused save leaves the configuration untouched | the previous configuration (recoverable only by re-entering it) |
| `deliberation_round1` / `lead_review` / `round2` / `synthesis` | **[COST]** / **[DURABLE]** | each stage spends provider money and writes artifacts | every stage is its own explicit press; the core refuses an out-of-order stage; Round 1 carries the confirmation | the artifacts and the money |
| `export_markdown` / `export_excel` | **[EXTERNAL]** | writes an artifact into the report directory, overwriting a same-named file | read-only projection; the same snapshot feeds both renderers | the overwritten file |
| `open_reports_folder` | **[EXTERNAL]** (OS) | launches the OS file manager | a separate process; the panel never blocks on it | nothing |
| `clear_logs` | **none** | only the in-memory view is emptied | the audit trail cannot be reached from this path, and the status line says so | nothing - this is the safest button in the matrix |
| all 16 popups | **none** | display only, from the last payload | no core work, no DB, no provider; a duplicate click raises the existing window | nothing |
| `refresh` / `reconnect` | **none** (reads) | `reconnect` re-applies pending migrations; a large audit table makes a refresh slow | the reads are read-only and idempotent; `reconnect` cannot repair state | nothing - but both can be *slow* on a large database (see PERFORMANCE.md) |
| `view_snapshot` | **none** | shows the last projection verbatim | no core work | nothing |

### The five most dangerous single presses, in order

1. **`abort`** - the only irreversible step-bound action; it removes the step (and the project's
   completeness) permanently.
2. **`import_plan`** - the only plan write path; a database that holds steps can never receive a
   different plan, and there is no delete or renumber.
3. **`approve_and_send`** - the only intent that puts a file in front of Cline; a wrong directive
   instructs real work, and the attempt counter is untouched.
4. **`waive_supervision`** - the only bypass: it lets one specific report skip the advisory gate
   that would otherwise hold it.
5. **`run_until_idle`** - not destructive, but it is the only press that can verify (or abort)
   several steps before the operator looks again, and a verification can spend a second of CPU
   inside a write transaction on the architecture scan.

### What protects the operator

- Every write path requires an explicit actor and a non-empty reason; a refusal never reaches the
  core, and the core fails closed even if the panel is stale.
- The panel's own consequential actions (`abort`, `import_plan`, `run_review`,
  `deliberation_round1`, `synthesize_proposal`, `approve_and_send`) carry a confirmation, and the
  irreversible step action carries the strongest wording.
- Display intents (16 popups + `clear_logs` + `view_snapshot`) stay available while busy and after
  a CRITICAL failure, so an operator can always read the log, the audit trail and the last
  projection instead of being locked out of the panel.
- `refresh`, `reconnect`, `open_reports_folder`, `run_review` and the provider-settings controls
  also stay available under CRITICAL, so a broken state can still be inspected and the
  configuration re-tested.
- The four "leaves" of the matrix - `clear_logs`, the popups, `view_snapshot` and
  `open_reports_folder` - cannot write anything at all, which is why they are the ones the panel
  never disables.

---

## Document control

- Source: `src/architecture_assistant_gui/controller.py`, read in full - the `INTENTS` tuple with
  all 56 entries, `enabled()`, `refusal()`, `submit()`, `_action_for()` and every action builder
  (`_deliberation_action`, `_deliberation_cancel_action`, `_read_action`, `_reconnect_action`,
  `_export_action`, `_mutation_action`, `_preview_action`, `_import_action`, `_review_action`,
  `_save_settings_action`, `_test_connection_action`, `_supervisor_action`, `_proposal_action`),
  plus `apply_result()`, `_describe()`, `_remember_export()`, `_deliberation_buttons()`,
  `supervisor_action_available()`, `supervisor_refusal()`, the proposal and plan helpers,
  `clear_log_view()`, `popup_view()` and the view-model accessors.
- Also read for the side-effect claims: the `CoreWorker` methods each intent calls in
  `src/architecture_assistant_gui/core.py`, and the `GuiApp._on_action` branches that intercept
  `load_plan`, `clear_logs`, `open_reports_folder` and the popup intents.
- Timing figures are quoted from the read-only measurements in `Arhitect_goal/PERFORMANCE.md`;
  anything not measurable (for example real provider latency) is stated as a bound derived from
  the adapter constants (30 s timeout, 3 attempts, 0/1/2 s backoff).
- No code was changed, no test was run, nothing was committed.
