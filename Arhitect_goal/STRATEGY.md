# Architecture Assistant - Development Strategy

| | |
| --- | --- |
| Document | `Arhitect_goal/STRATEGY.md` |
| Basis | Full read of the repository at branch `v1.1-gui` (HEAD `ebe36b9` + uncommitted working-tree changes), plus the read-only measurements recorded in `Arhitect_goal/PERFORMANCE.md` |
| Constraint | No code was changed, no test was run, nothing was committed. Every statement below is either read from the code or measured; anything that could not be verified is labelled as such. |

---

## 1. Current state assessment

### 1.1 What exists and works (verified by reading + measurement)

- **The core is complete and internally consistent.** Six layers (`domain`, `ports`,
  `architecture`, `application`, `infrastructure`, `composition`) with a mechanical import
  gate: a measured validation of the scanned tree returns baseline **1.1**, **compliant =
  True**, **0 violations**, **71 modules**, **3 rules**.
- **The persistence and recovery story is real, not aspirational.** One shared SQLite
  connection, explicit `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`, one audit entry per mutation,
  at-least-once report handling (outcome persisted before the report is archived), idempotent
  bootstrap and version reconciliation, and a dedicated crash-recovery suite
  (`test_recovery.py`, 65,854 bytes) that kills real child processes.
- **The engine is measured and fast where it matters.** Cold composition of a fresh database
  measured **0.042 - 0.078 s**; a bounded loop run over 12 steps measured **8.5 ms** in total;
  an idle tick measured **0.48 - 0.52 ms** at 12 steps.
- **The write surface is small and uniform.** Every human write path (`ApprovalGate`,
  `HumanOverride`, `PlanLoader`, `ProposalApproval`, `Supervision` decisions) requires an
  explicit actor and a non-empty reason, performs exactly one mutation plus exactly one audit
  entry in one transaction, and fails closed on anything it cannot vouch for.
- **The AI surface is strictly advisory and provider-neutral.** Four advisor adapters, one
  judge, one supervisor, two deliberation roles - all behind `Protocol` ports, all optional,
  none able to out-vote the deterministic gate or write workflow state.
- **Dependency hygiene is excellent.** AST-extracted imports across 83 source files show
  exactly **one** third-party module (`customtkinter`, host-only) and 25 stdlib modules. The
  core needs no third-party package at all.
- **The host is genuinely decoupled from the core.** Only three GUI modules import the
  assistant (`core.py`, `controller.py`, `app.py`); the Tk thread never receives a core object,
  and there is exactly one core worker thread.

### 1.2 Where the tree actually is (state facts)

| Fact | Evidence |
| --- | --- |
| Branch `v1.1-gui`, HEAD `ebe36b9`, also pushed to `origin/v1.1-gui` | `git log` / `git status` |
| **Uncommitted work exists**: modified `pyproject.toml`, `composition/__init__.py`, `gui/app.py`, `gui/controller.py`, `gui/core.py`, `gui/views.py`, `tests/test_gui_layout.py`; untracked `USER_GUIDE_LV.md`, `gui/project_setup.py`, `gui/theme.py`, `gui/workspace.py`, `gui/workspace_views.py` | `git status --short` |
| `src/` is **83 files / 46,563 lines (5.84 MB)**; the scanned core is **71 modules / 1,358,305 bytes**; the host is **12 modules / 467,258 bytes** | measured |
| The test suite is **63 files / 50,127 lines (12.42 MB)** - larger than the source it tests; there is **no `conftest.py`** | measured |
| Four modules dominate the tree: `gui/controller.py` 4,610 lines, `application/architecture_deliberation.py` 3,053, `application/supervision.py` 2,513, `gui/views.py` 2,200 | measured |
| Migrations in code: **7**; `data/architecture_assistant.db` is at **v7**, `data/youtube_to_mp3.db` is at **v4** (it will be migrated on its next open) | read-only SQLite inspection |
| `.mini_build/` holds 94 files / 2,230,127 bytes of build-controller artefacts inside the working tree (Git-ignored, not product) | measured |
| `.pytest_cache/v/cache/lastfailed` lists 41 previously failing node ids - including `tests/test_zz_diag.py::test_diag`, **a test file that no longer exists** | read |
| **The new host modules have no tests**: no test file imports `project_setup`, `workspace`, `workspace_views` or `theme` | grep over `tests/` |
| `README.md` claims "3524 passed / 0 failed" and "no third-party runtime dependencies" | read - the first was not re-verified (tests were not run by instruction); the second is contradicted by `pyproject.toml` |
| Version metadata: `pyproject.toml` and `__version__` say **0.1.0**, the release identity is the Git tag **v1.0.0**, the baseline is **v1.1** | read |

### 1.3 Summary judgement

The **core** is in good shape: layering is enforced mechanically, the invariants are explicit,
recovery is genuinely tested, and the hot paths are short. The **risk has moved to the edges**:
an uncommitted, untested onboarding/workspace layer; a 4,610-line GUI controller; a 965-line
README that no longer describes the working tree; and a handful of read paths whose cost grows
with data volume (measured in PERFORMANCE.md). Nothing found is architecturally wrong; the
problems are *undone work* rather than *broken design*.

---

## 2. Identified risks and weak points

Ordered by "probability x cost". Each item names the evidence and the smallest honest fix.

### R1 - Uncommitted, untested host surface (high probability, high cost)

The onboarding/workspace feature set (`project_setup.py`, `workspace.py`,
`workspace_views.py`, `theme.py`) is **untracked** and has **no test coverage**, while it owns a
new, user-visible workflow: it derives a per-project state folder
(`<project>/.architecture_assistant/{project.db, cline/, reports/, providers.json, workspace.json}`)
and generates plan JSON drafts. A regression here is invisible to the suite.

*Fix:* commit or revert, then add focused tests for `workspace_spec()` (path derivation,
invalid folder/name/mode), `execution_plan()` (dependency order, cycle refusal, step cap) and
`save_preferences()` (atomic write, unreadable file).

### R2 - Documentation no longer describes the product (high probability, medium-high cost)

The project's value proposition *is* its documented behaviour, yet:

- `README.md` (965 lines) documents the panel as a set of tabs and does not mention the guided
  workspace, the project-setup dialog, the deliberation workbench UI or the per-project state
  folder;
- `README.md` section 8 claims there are no third-party runtime dependencies;
- `USER_GUIDE_LV.md` (the only end-user guide) exists **only in Latvian** and is itself
  untracked;
- `README.md` section 9's "current tree" list predates deliberation and the workspace;
- no `requirements.txt` exists, so its packaging section cannot be followed literally.

*Fix:* make `Arhitect_goal/*.md` the architectural source of truth, then rewrite README
sections 3, 8, 11, 12 to match the tree, and decide the documentation language (see O3).

### R3 - God-modules in the host and in two application modules (medium probability, medium cost)

`gui/controller.py` (4,610 lines / 184 KB) is a single class holding the view model, the button
matrix (56 intents), the payload state (~30 fields) and every per-tab projection;
`gui/views.py` (2,200 lines) and `gui/workspace_views.py` both build widgets;
`application/supervision.py` (2,513 lines) and `application/architecture_deliberation.py`
(3,053 lines) each mix policy, persistence orchestration and panel-facing payload shaping.
Merge conflicts, review cost and the chance of an accidental invariant change all grow with
these files.

*Fix:* split by tab/feature (controller: one module per tab; views: one module per region) and
by concern in application (policy vs payload shaping), keeping the public class names.

### R4 - Read paths whose cost grows with data (measured; see PERFORMANCE.md)

- `SqliteAuditRepository.list()` reads the **entire** `audit_entries` table and the worker then
  slices the last 200 rows in Python. Measured: **1.86 ms** at 234 rows -> **19.75 ms** at
  2,234 -> **187 ms** at 22,234 -> **343 ms** at 42,234 rows (~8.1 us/row). Every panel refresh
  pays this, on the core thread.
- `ArchitectureRealizationAdapter.check()` re-reads and re-parses the whole scanned tree on
  every call: measured **710 - 930 ms** for 71 files / 1.36 MB, inside the `VERIFY`
  transaction.
- `ReportBuilder.build()` issues **515 SQL statements at 500 steps** (one per step for the cost
  roll-up) and re-reads the step list about five times per build; measured **2.6 ms** at 12
  steps -> **53 ms** at 500, with `monitor.to_dict()` at **81 ms**.
- `Orchestrator.run_once()` materialises every step row on every tick: measured **6.1 ms** per
  idle tick at 500 steps (0.5 ms at 12).

*Fix:* SQL `ORDER BY id DESC LIMIT ?` for the audit tail (the `id` column is the append order
and has no index yet); a content-hash-keyed memo for the realization scan; hoist the repeated
`steps.list()` out of the projection; keep the per-step cost query but make it one grouped
query.

### R5 - One job at a time, no cancellation (medium probability, medium cost)

`BackgroundRunner.submit()` refuses a second job while one runs, and every action - including
provider calls - runs on that single core thread. A provider call is bounded only by the
adapter constants (30 s timeout, 2 retries, 0/1/2 s backoff), so a slow provider can make the
panel refuse `Refresh`, `Pause` and `Abort` for tens of seconds, with no way to abandon the
request. Nothing in the code exposes a cancellation token.

*Fix:* decide the product answer first (see O6): either a cancel affordance (needs a
cooperative cancellation flag inside the adapters) or an honest UI state that distinguishes
"core is busy with a paid provider call" from "core is busy with a fast write".

### R6 - SQLite concurrency and durability configuration is untuned

`open_database` sets exactly one pragma (`foreign_keys = ON`); there is **no**
`journal_mode` / `synchronous` / `busy_timeout` configuration anywhere, and the measured
databases are in the default rollback-journal mode (`journal_mode = delete`). One connection is
created with the default `check_same_thread = True` and is owned by the core thread by design.
Two consequences: a second process (a script, a report tool, the `.bat` launcher's probe) can
hit `database is locked` with no wait at all, and the pilot's runtime data sits inside a
**OneDrive-synced** folder, where the sync client can hold locks and inflate the file times
measured here.

*Fix:* decide WAL + `busy_timeout` (see O7), document the "one process per database" rule in
the README, and keep runtime data out of synced folders in the pilot instructions.

### R7 - Provider adapter duplication (medium probability, medium cost)

`openai.py` (815 lines), `claude.py` (871), `grok.py` (872), `deepseek.py` (868),
`openai_judge.py` (812) and `deliberation_lead.py` (810) repeat the same
transport/retry/probe/parse skeleton over `_http.py`. A change to redaction, retry
classification, timeout handling or cost identity must be repeated five or six times, and only
one provider (OpenAI) also has a judge variant.

*Fix:* extract one shared "provider call pipeline" (request builder + retry loop + response
validation hooks + redaction) and keep only provider-specific payload shapes in each adapter.

### R8 - Secret handling is honest but weak (low probability, high cost if it happens)

Provider API keys are stored in plaintext JSON (`data/provider_settings.json`, or
`<project>/.architecture_assistant/providers.json`) and read once at composition time. The code
does everything it can about *leakage* (masking, `key_set` booleans, redaction, no key in logs,
audit, reports, exceptions or `repr`s), but a local file disclosure exposes the keys. README
section 11 already admits this is deferred.

*Fix:* see O4 (OS keyring or an env-var-only mode).

### R9 - Reproducibility and release hygiene (medium probability, medium cost)

There is no `requirements.txt`, no lock file, no CI configuration and no `conftest.py`; the
packaging metadata (`0.1.0`) matches neither the release tag (`v1.0.0`) nor the baseline
(`v1.1`). `.mini_build/` (94 files, 2.1 MB of step reports) and `.pytest_cache/` (with a stale
`lastfailed` referring to a deleted `test_zz_diag.py`) live in the working tree.

*Fix:* decide the version story (O8), add a minimal CI job (`pip install -e .[dev]`,
`python -m pytest -q`, plus the architecture self-check), and clean the stale caches.

### R10 - The single-project invariant is load-bearing but implicit (low probability, medium cost)

Bootstrap, projection, monitor, review and proposal all assume **exactly one** `Project` row and
raise on zero or many. The new onboarding flow quietly creates *one database per project*, so a
user who points two workspaces at the same database hits an invariant error phrased as
"exactly one project", not as "you opened the same database twice".

*Fix:* make the rule explicit in the onboarding UI and the README, or take decision O1.

---

## 3. Module dependency problems

The layering of the **core** is enforced and clean (measured: 0 violations). The problems below
are all *boundary erosion inside the host* and *interface width*, not layer violations.

### 3.1 The host reaches one layer too deep

`architecture_assistant_gui/__init__.py` states that the host "may call the assistant's public
`architecture_assistant.composition` API". Exactly three host modules import the assistant -
`core.py`, `controller.py`, `app.py` - but **`app.py` also imports
`architecture_assistant.domain.enums`** (for `--mode` choices). It is a one-line dependency and
harmless today; it is still the first crack in the boundary: the next GUI feature can reach
`domain.models` or `ports` just as easily, and nothing mechanical stops it (the GUI is *outside*
the scanned tree, so the architecture gate never sees it).

*Options:* re-export the mode names from `composition` (for example a `MODES` tuple) and use only
that; or accept the import and document it as an explicit, narrow exception.

### 3.2 The core-thread worker imports presentation helpers

`gui/core.py` imports `from .workspace import execution_plan, cline_handoff`. The consequence is
that the module which owns the SQLite connection and runs every workflow action also
**generates plan JSON** for the UI (`CoreWorker.prepare_execution_plan`). Plan-schema knowledge
therefore exists in two places:

- `application/plan_loader.py` owns the schema and the limit - `MAX_PLAN_STEPS = 500`;
- `gui/workspace.py` hardcodes the same limit - `if len(steps) > 500`.

If the importer's schema or cap ever changes, the draft generator silently produces plans the
importer rejects (or refuses drafts the importer would accept). Nothing tests the pairing.

*Options:* move the draft generator into `application` (it is pure: proposal + project -> text)
and have the worker call it; or at minimum import the constant instead of repeating the literal.

### 3.3 Four independent atomic-write implementations, only one durable

`os.replace`-based publication is implemented four times:

| Module | Sink | `fsync` |
| --- | --- | --- |
| `infrastructure/cline.py` | worker task/context/report/directive files | **yes** (`flush()` + `os.fsync`) |
| `composition/provider_settings.py` | the operator's provider preference file | no |
| `architecture_assistant_gui/layout.py` | the remembered window layout | no |
| `architecture_assistant_gui/project_setup.py` | local preferences / workspace profile | no |

The asymmetry is defensible (a worker artifact claims at-least-once delivery, a preference does
not) but it is *implicit*: nothing in the code states which sink needs durability. One helper
with an explicit `durable: bool` flag would make the guarantee visible and testable in one place.

### 3.4 Interface width: `Composition`, `application/__init__`, `composition/__init__`

- `Composition` (dataclass, ~28 attributes) is the single object the host holds. Every capability
  added since v1.0 (`cost`, `review`, `synthesis`, `proposal approval`, `deliberation`,
  `supervision`) widened it.
- `application/__init__.py` (15 KB) re-exports the whole application surface, and
  `composition/root.py` imports about 45 names from it. The application package is one flat
  namespace: an import error anywhere inside it makes `compose()` unimportable.
- `composition/__init__.py` re-exports about 90 names (the wiring, the evolution catalogue, the
  log contract, the advisor factory, the provider settings, the deliberation verbs). It is the
  documented host API *and* the deepest aggregation point in the project, so
  `import architecture_assistant.composition` pulls in **every** adapter module - measured at
  **0.50 - 0.85 s**, the single largest startup cost (see PERFORMANCE.md).

*Options:* keep `Composition` but group the optional capabilities behind three or four sub-facades
(for example `composition.advisory`, `composition.proposals`, `composition.supervision`); split
the `application/__init__.py` re-exports so that `compose()` does not need all of them.

### 3.5 Two places shape the same data

Policy modules in `application` (`supervision.py`, `architecture_deliberation.py`,
`architecture_review.py`) build panel-facing dictionaries (`to_dict()` payloads whose keys are
exactly what a tab renders), while `gui/controller.py` (4,610 lines) builds its own view model
over the same payloads. Two layers therefore own "the shape shown to the operator", so a field
rename must be coordinated across both and neither can be changed independently.

*Options:* freeze a small set of payload contracts - `application/eventlog.py` is the good
example of how to do that - and make the controller's projections explicitly derived from them.

### 3.6 The host speaks two widget languages

`views.py` builds `ttk` widgets with its own styling, while `theme.py`,
`workspace_views.py` and `project_setup.py` build CustomTkinter widgets; `theme.button` and
`DarkButton` coexist as two button factories. `theme.py`'s own docstring admits the mix ("for
CustomTkinter and the remaining ttk widgets"). Every visual change has to be applied twice, and
the two families do not share scaling or theming behaviour.

*Options:* pick one toolkit per region and document it, or finish the migration to CustomTkinter
and delete the `ttk` styling path.

### 3.7 Startup-order coupling inside the host

`gui/app.py` imports `.core` at module import time (line 47), and `core.py` imports
`architecture_assistant.composition` at module import time. Because `main()` imports Tk first (to
report a missing Tk installation cleanly), the **0.5 - 0.85 s composition import happens on the
Tk (main) thread before the window is built**. It is correct - it all happens before the mainloop
starts - but it delays first paint and couples "show a window" to "the entire core package
imports cleanly".

*Options:* import `.core` lazily inside `GuiApp.run()` (after the shell window is created and
mapped), so the operator sees the panel immediately.

---

## 4. Recommended development sequence

The order is chosen by *dependency and risk*, not by feature appeal: you cannot measure, refactor
or release reliably from an uncommitted, undocumented tree.

### Phase 0 - Stabilise the working tree (do this first)

1. Decide, per uncommitted file, commit or revert (`pyproject.toml`,
   `composition/__init__.py`, the four GUI modules, `tests/test_gui_layout.py`, and the four
   untracked modules + `USER_GUIDE_LV.md`).
2. Run the full suite and the architecture self-check once, and record the numbers together with
   the commit hash. (`python -m pytest -q`; the self-check is reproducible: measured baseline
   1.1, compliant True, 0 violations, 71 modules, 3 rules.)
3. Delete/refresh `.pytest_cache/` (it currently advertises a deleted `tests/test_zz_diag.py`)
   and decide whether `.mini_build/` (94 files, 2.1 MB) should keep living inside the working
   tree.

*Exit criterion:* one green, documented baseline on a single commit.

### Phase 1 - Make the tree self-describing

1. Add tests for the new host surface: `workspace_spec()`, `execution_plan()`,
   `save_preferences()`, and a smoke test that `project_setup`/`theme` import without a display.
2. Reconcile the documentation: the dependency statement (README section 8), the panel/workflow
   description (README section 3 and the "Repository Structure" section 12), the features added
   since v1.0 (deliberation workbench, guided workspace, project state folder), and add a
   `requirements.txt`-or-explicit-policy statement.
3. Take decisions O3 (language) and O8 (version identity), because both change what you write.

*Exit criterion:* a reader can reproduce the described product from the README alone; every file
in `src/architecture_assistant_gui` is imported by at least one test.

### Phase 2 - Performance fixes with the best ratio

Highest value first, all small and independently testable:

1. **Audit tail**: add `ORDER BY id DESC LIMIT ?` to the audit adapter (plus an index on `id` if
   the query plan needs it) and keep the existing 200-row contract. Measured today: 1.86 ms at 234
   rows -> 343 ms at 42,234 rows, on every refresh.
2. **Realization scan**: memoise the `scan_directory` result per process keyed by a content hash
   of the tree (or reuse the verdict for identical `(step_no, attempt, tree hash)`), so the
   710-930 ms scan is not paid on every `VERIFY` tick.
3. **Projection query count**: build the step list once per `ReportBuilder.build()` (measured 515
   statements at 500 steps, with the step table read about five times per build) and collapse the
   per-step cost roll-up into one grouped query.

*Exit criterion:* measured audit tail under ~5 ms at 40k rows; a repeated `VERIFY` on an
unchanged tree does not re-parse the tree; `build()` issues a bounded number of statements
independent of step count.

### Phase 3 - Operations and robustness

1. Decide O7 and implement it (`PRAGMA busy_timeout` at minimum; WAL only if the crash-recovery
   story still holds for the existing proofs).
2. Unify the four atomic-write implementations behind one helper with an explicit durability
   flag, and keep `cline.py`'s `fsync` behaviour as the durable case.
3. Make the "one process per database" rule explicit in the onboarding dialog and the docs, with
   a clear error message when the database is locked or held by another process.

*Exit criterion:* a second process opening the same database fails with a documented, tested
message instead of a raw `sqlite3.OperationalError`.

### Phase 4 - Maintainability refactors (with the suite green)

1. Split `gui/controller.py` (4,610 lines) by tab and `gui/views.py` (2,200) by region; keep
   `GuiController` and `MainWindow` as the public names.
2. Split `application/supervision.py` (2,513) and `application/architecture_deliberation.py`
   (3,053) into policy vs payload shaping, and freeze the payload keys as explicit contracts
   (the `eventlog.py` pattern).
3. Collapse the provider adapters (`openai`, `claude`, `grok`, `deepseek`, `openai_judge`,
   `deliberation_lead`) onto one shared call pipeline.
4. Move plan-draft generation out of `gui/workspace.py` into `application`, importing
   `MAX_PLAN_STEPS` instead of duplicating `500`.
5. Pick one widget toolkit per region (3.6) and remove the duplicate styling path.
6. Import `.core` lazily in `GuiApp.run()` so the window appears before the core package loads.

*Exit criterion:* no source file above ~1,200 lines; one atomic-write helper; one plan cap
constant; suite still green.

### Phase 5 - Close the product gaps

In the order the pilot would feel them: (a) cancellation or honest busy-state for provider calls
(O6), (b) OS-keyring or env-var key storage (O4), (c) a real `SupervisorPort` adapter if the
scripted double is not acceptable for the pilot (O5), (d) a small CLI for the core loop
(`run`, `status`, `import-plan`) so the assistant is usable without the GUI, (e) the
multi-project answer (O1).

### Phase 6 - Scale, packaging and release

1. Contract the step-count ceiling (the draft generator already caps at 500) and publish the
   supported range; the measured worst case at 500 steps is a 51 ms panel repaint and an 81 ms
   projection build.
2. Decide the audit/cost retention policy (the trail is append-only and never pruned; the
   measured cost curve makes growth the first thing that will hurt).
3. Add the smallest useful CI (install, `pytest -q`, architecture self-check) and a lock file or
   pinned constraints for reproducible installs.

---

## 5. Open questions that need decisions

Each item states the question, the options, why it blocks, and the recommended default if nobody
decides. Nothing here is settled by the code today - these are genuinely open.

### O1 - Is "one project per database" permanent?

- (a) Yes: rewrite the invariant error in operator language, guard the onboarding dialog against
  reusing a database, and document the rule.
- (b) No: support N projects per database (affects the bootstrap singleton, the projection, the
  monitor, the review/proposal scoping and every repository query).

*Blocks:* the onboarding UX, error messages, and any investment in a project switcher.
*Default recommendation:* (a) for the pilot; revisit after Phase 4.

### O2 - Which UI is the product: the guided workspace or the tabbed panel?

The tree now contains two overlapping operator experiences: the documented tab panel
(`views.py`, 2,200 lines) and the newer guided workspace (`workspace_views.py`,
`project_setup.py`, `workspace.py`) with a different navigation model and a partly Latvian
surface.

- (a) The guided workspace becomes primary; README and screenshots follow it; the tabs become an
  advanced view.
- (b) The tabbed panel stays primary; the workspace is reduced to onboarding only.
- (c) Maintain both deliberately (the most expensive option: every change lands twice).

*Blocks:* all documentation work and the split of `controller.py`/`views.py` in Phase 4.
*Default recommendation:* (a) - the workspace is what a new operator sees first.

### O3 - Documentation language

- (a) English-only documentation (translate or retire `USER_GUIDE_LV.md`).
- (b) Bilingual: English README plus a maintained Latvian operator guide.
- (c) Latvian-first for operator docs, English for architecture docs.

*Blocks:* the Phase 1 documentation rewrite and any future guide.
*Default recommendation:* (b) if translation capacity exists, otherwise (a).

### O4 - Where do provider API keys live?

- (a) Status quo: plaintext JSON in `data/` (or the project's `.architecture_assistant/`).
- (b) OS keyring (`keyring` becomes a new third-party dependency; `AdvisorFactory` already
  centralises resolution, so the change is contained).
- (c) Environment variables only (nothing persisted; friction for non-developers).

*Blocks:* the honest security statement in the documentation and the pilot's threat model.
*Default recommendation:* (b) for stored keys, with (c) as an override - key resolution already
has exactly one seam.

### O5 - Does the pilot ship an offline supervisor or a live one?

`ScriptedSupervisor` is the only implementation today; the port (`SupervisorPort`) already
exists.

- (a) Keep the scripted double and label it clearly (partially done already).
- (b) Build a real supervisor adapter (which provider, which budget, which actions may it
  propose?).

*Blocks:* what "supervision" demonstrates in the pilot, and the supervisor tab's honesty claim.
*Default recommendation:* (a) unless a live supervisor is an explicit demo requirement.

### O6 - May an in-flight provider call be cancelled?

- (a) No: show an explicit "waiting for a provider" state and keep refusing new actions.
- (b) Yes: add a cooperative cancellation token consulted between retry attempts (a bounded change
  - the retry loops already exist in six adapters).

*Blocks:* how honest the panel can be about being unavailable for up to ~90 s worst case.
*Default recommendation:* (b): the retry loop is the natural cancellation point, and the
alternative is a UI that claims to be available while it is not.

### O7 - SQLite durability and concurrency mode

- (a) Status quo: rollback journal (`journal_mode = delete`), no `busy_timeout`, one writer
  process assumed.
- (b) WAL + `busy_timeout`: better read concurrency, but new `-wal`/`-shm` files and a changed
  crash-recovery surface that the existing proofs (`test_recovery.py`) would have to be
  re-validated against.

*Blocks:* Phase 3 and the "second process" story.
*Default recommendation:* add `busy_timeout` now (safe, no durability change); consider WAL only
with a re-run of the recovery proofs.

### O8 - What identifies a release?

Today: tag `v1.0.0`, `pyproject.toml` `0.1.0`, package `__version__` `0.1.0`, architecture
baseline `1.1`.

- (a) Bump the packaging metadata to match the release tag and keep the baseline version
  separate.
- (b) Keep tag-only identity and document it (status quo).
- (c) Version the baseline independently and document the mapping.

*Blocks:* packaging, any "which version am I running?" support question, and how the
architecture self-check output is read.
*Default recommendation:* (a) plus (c): a product version and an architecture-baseline version are
two different things and both should be visible.

### O9 - Is the 500-step plan cap a product limit?

`MAX_PLAN_STEPS = 500` in the importer, duplicated as a literal in the draft generator. Measured
at 500 steps: 81 ms projection, 51 ms repaint, 6.1 ms idle tick - workable but visibly slower
than at 12 steps. A database that already holds steps refuses a new plan, and there is no append
or re-import path.

- (a) Keep 500 and publish the measured cost.
- (b) Raise the cap after fixing the read paths (Phase 2).
- (c) Add incremental plan extension (a real feature with audit implications).

*Blocks:* the supported-workload statement and the ceiling test the suite should pin.
*Default recommendation:* (b), then (a) with numbers.

### O10 - Retention and ownership of accumulated data

The audit trail is append-only with no delete path (by design) and cost records accumulate per
provider call; the panel shows the newest 200 audit rows and buffers 400 log events.

- (a) Unbounded, with explicit backup/archive guidance (the database is a file).
- (b) An explicit retention/archival command (export + vacuum), never an automatic delete.
- (c) Prune inside the assistant (would contradict the append-only promise).

*Blocks:* long-run pilot feasibility and the Phase 6 work.
*Default recommendation:* (b) - keep "never delete automatically" and give the operator a
reviewed, audited archival action. Also decide whether the `.mini_build/` artefacts (94 files,
2.1 MB) stay inside the project folder.

---

## Document control

- This strategy is derived exclusively from the repository as it exists at
  `v1.1-gui` / `ebe36b9` plus the working-tree changes, and from the measurements recorded in
  `Arhitect_goal/PERFORMANCE.md`.
- No test was executed while writing it, so the README's "3524 passed / 0 failed" figure is
  quoted as an **unverified claim**, never as a measured fact.
- Re-run the architecture self-check and the performance harness after Phase 0 so that every
  number quoted here is pinned to a commit hash.
