# Architecture Assistant - Performance Profile

| | |
| --- | --- |
| Document | `Arhitect_goal/PERFORMANCE.md` |
| Basis | Read-only runtime measurements taken on the code in this repository at `v1.1-gui` / `ebe36b9` (+ working-tree changes) |
| Method | Every number below was produced by running small throw-away measurement scripts from `%TEMP%` against the project's own modules. Databases, exchange directories and report directories were created in `%TEMP%` and deleted afterwards. **No repository file was written, no test was executed, nothing was committed.** Where a number is derived from code constants instead of measured, or could not be measured at all, it says so explicitly. |
| Environment | Windows (win32), CPython **3.14.0** (`C:\Python314\python.exe`), `customtkinter` installed, `pytest` installed, `psutil` **not** installed (memory read via Win32 `GetProcessMemoryInfo`, `tracemalloc` and PowerShell `Get-Process`). The repository lives on a **OneDrive-synced** path, which can add lock/anti-virus overhead to file timings and is itself unmeasured. |

---

## 1. Current performance profile

### 1.1 Startup time (measured)

| Stage | Measured | Notes |
| --- | --- | --- |
| Python interpreter start (`python -c "import sys"`) | **53.8 ms** | single `Measure-Command` run |
| `import architecture_assistant.composition` | **501.8 / 536.3 / 591.1 / 851.5 ms** (4 runs) | **the dominant single cost**; pulls in every layer and every adapter |
| `import architecture_assistant.architecture` (already resident) | ~0 ms | the layer itself is cheap; the cost is the package graph |
| `import tkinter` | **10.1 - 11.4 ms** | |
| `import customtkinter` | **90.3 - 100.3 ms** | |
| GUI stack import (`.controller`, `.core`, `.theme`, `.views`) | **12.2 - 20.7 ms** | on top of the composition import |
| CTk root bootstrap (appearance + theme + `CTk()` + `withdraw()`) | **52.6 - 113.7 ms** | |
| `MainWindow` construction (the whole widget tree) | **377.7 - 455.4 ms** | builds **642 descendant widgets** |
| First render (`render(view_model)`) | **91.7 - 104.7 ms** | |
| First layout settle (`update_idletasks`) | **16.5 - 20.7 ms** | |
| Cold `compose()` on a fresh database (on the core thread) | **41.6 - 78.4 ms** | includes schema creation + baseline reconciliation |

**Measured cold start to first paint: ~1.26 - 1.61 s** (interpreter + imports + window + first
render), of which the `composition` import alone is 0.50 - 0.85 s and it happens **on the Tk
(main) thread** before the window is created (`app.py` imports `.core`, which imports
`architecture_assistant.composition` at module import time).

### 1.2 Memory usage (measured)

| Scenario | Working set | Peak |
| --- | --- | --- |
| Full stack imported (core + all GUI modules), then idling, no Tk mainloop | **48.09 MB** steady over a 10 s sample | **49.14 MB** |
| `import architecture_assistant.composition` only (`tracemalloc` peak) | - | **11.41 MB** allocated |
| Scanning + validating the scanned tree (71 files, 1.36 MB of source) | - | **27.65 MB** allocated |
| Composition + 12-step import + projection + renders + audit reads (one process) | - | **11.01 MB** allocated (`tracemalloc`) |
| On-disk state | `data/architecture_assistant.db` **200,704 B**, `data/youtube_to_mp3.db` **135,168 B** | 4096-byte pages, `journal_mode = delete` |

The panel's steady-state footprint is dominated by the interpreter + CustomTkinter + the widget
tree, not by domain data: a 12-step project and a 500-step project use the same working set
within measurement noise.

### 1.3 CPU at idle (measured)

| Scenario | CPU | Detail |
| --- | --- | --- |
| Full stack imported, process sleeping | **0.000 s CPU over 10.000 s wall (0.00 % of one core)**; working set unchanged | PowerShell `TotalProcessorTime` delta |
| Tk event loop with `after(100 ms)` pump, window withdrawn, nothing pending | **26 pump ticks in 3.008 s wall, 0.0 s CPU** (< 0.1 ms per tick, below `time.process_time` resolution) | the 100 ms interval is honoured; an idle pump is effectively free |

The panel polls **queues**, never the workflow: `_pump()` drains `runner.progress()`,
`runner.events()` and `runner.poll()` and only re-renders when something arrived. With
supervision off (the default) no tick is ever submitted, so an idle panel does no work at all.

### 1.4 CPU under load (measured)

| Workload | CPU | Wall |
| --- | --- | --- |
| compose + 12-step plan import + `run_until_idle` + 10 projections + Excel/Markdown render + 20 idle ticks | **0.672 s** | ~1.5 s |
| 3 × `ReportBuilder.build()` at 500 steps (under `cProfile`) | 0.309 s (profiled) | - |
| 50 × `Orchestrator.run_once()` at 500 steps (under `cProfile`) | 0.498 s (profiled, ~10 ms per tick) | - |
| One `scan_directory` of the scanned tree, `cProfile`: **1,817,898 function calls in 0.538 s** | hottest entries: `builtins.compile` 0.131 s, `ast.iter_child_nodes` 0.104 s (0.208 s cumulative), `ast.iter_fields` 0.053 s, `ast.walk` 0.051 s (0.311 s cumulative) | the cost is **AST parsing and walking**, not file reading |
| One `audit.list()` at 22,234 rows: 837,337 calls in 0.381 s | hottest: `AuditEntry.__post_init__` 0.196 s cumulative, `json.dumps` 0.090 s, `_row_to_audit_entry` 0.349 s cumulative | the cost is **object construction + JSON decoding per row** |

Profiler numbers are inflated by instrumentation; they are used here only to identify *where*
the time goes, never as absolute latency.

---

## 2. Bottlenecks identified (measured, not guessed)

Ordered by measured impact. Each bottleneck names the code, the measurement, and what would have
to change.

### B1 - The architecture realization scan re-reads and re-parses the whole tree on every check

- **Code**: `architecture_assistant/architecture/scanner.py::scan_directory` (no cache, by
  design) is called from `composition/realization.py::ArchitectureRealizationAdapter.check`,
  which the loop calls **inside the `VERIFY` transaction**
  (`orchestrator._review` -> `self._realization.gate(...)`).
- **Measured**: **708 - 942 ms per scan** (5 consecutive scans of the scanned tree: 716.5, 713.6,
  708.2, 730.0, 724.7 ms in one session; 758.7 - 941.6 ms in another). Input: **71 files /
  1,358,305 bytes**. Profiled hot spots: `compile` (0.131 s), `ast.iter_child_nodes` /
  `ast.iter_fields` / `ast.walk` (~0.31 s cumulative).
- **Impact**: every tick that can verify a step pays ~0.7-0.9 s of CPU **while holding the write
  transaction**, on the core thread. The panel stays responsive, but every other operator action
  is refused for that period, and the cost scales linearly with the inspected project's size (a
  5x larger target repository implies roughly 3.5 - 5 s per verification).
- **Smallest honest fix**: memoise the scan keyed by a content hash of the inspected tree (the
  adapter already computes a content hash for its finding ids), or reuse the verdict for an
  unchanged `(step_no, attempt, tree hash)`.

### B2 - The audit tail reads the entire audit table on every refresh

- **Code**: `gui/core.py::CoreWorker.audit_tail(limit=200)` calls
  `composition.storage.audit.list()`, which is `SELECT * FROM audit_entries ORDER BY id ASC`
  (no `LIMIT`), and only then slices `entries[-resolved:]` in Python.
- **Measured** (throw-away database, 5 runs each):

  | `audit_entries` rows | `audit.list()` avg | max |
  | --- | --- | --- |
  | 234 | **1.86 ms** | 2.25 ms |
  | 2,234 | **19.75 ms** | 22.19 ms |
  | 22,234 | **187.36 ms** | 227.49 ms |
  | 42,234 | **342.86 ms** | 365.51 ms |

  That is ~**8.1 us per row**, linear, with no plateau - and the panel triggers a refresh after
  almost every action (`payload()` + `audit_tail()`).
- **Impact**: the first bottleneck an operator will actually notice. At 42k audit rows (a few
  hundred workflow steps with retries, reviews, proposals and supervision decisions) every button
  press costs a third of a second of core-thread time just to show the newest 200 lines.
- **Smallest honest fix**: `SELECT ... ORDER BY id DESC LIMIT ?` in the adapter (the append order
  column is the autoincrement `id`), optionally with an index on `id`; the panel contract
  (newest-first, bounded 200) does not change.

### B3 - The projection is O(steps) in queries and re-reads the step table several times per build

- **Code**: `application/reporting.py::ReportBuilder.build()` - one `cost_query` per step
  (`cost_by_step`) plus `self._health()`, which calls `orchestrator.is_paused()`, `step_count()`,
  `current_step()`, `next_step_no()` and `is_complete()` - each of which calls
  `self._storage.steps.list()`.
- **Measured**: profiling exactly **3 builds at 500 steps produced 1,545 `sqlite3.execute` calls
  => ~515 statements per build**, and **7,500 `_row_to_step` / `Step.__post_init__` calls
  => ~2,500 per build** (about 5 full reads of a 500-row table). Wall clock:

  | Steps | `ReportBuilder.build()` | `Monitor.to_dict()` | panel refresh (build only) |
  | --- | --- | --- | --- |
  | 12 | **2.57 ms** | 2.60 ms | 2.27 ms |
  | 50 | **5.90 ms** | 6.32 ms | 5.79 ms |
  | 200 | **20.68 ms** | 23.92 ms | 19.99 ms |
  | 500 | **53.05 ms** | 81.48 ms | 56.42 ms |

- **Impact**: the projection is read by the monitor, the panel payload, the reporting exporters
  and the GUI's monitor rows; at 500 steps the monitor payload alone takes 81 ms and the panel
  builds one on every action.
- **Smallest honest fix**: read the step list once per build and pass it to the health selectors;
  collapse the per-step cost roll-up into one grouped query (`GROUP BY step_no`).

### B4 - Every loop tick materialises every step row

- **Code**: `application/orchestrator.py::run_once()` -> `self._storage.steps.list()` (all rows)
  plus `_require_project()`.
- **Measured**: idle tick **0.475 - 0.520 ms** at 12 steps (20 runs, mean ~0.49 ms) and
  **6.11 ms** at 500 steps; profiling 50 idle ticks at 500 steps shows **25,000 `_row_to_step`
  calls = 500 rows per tick**. Tick scaling by step count: 1.78 ms (12) -> 1.65 ms (50) ->
  3.28 ms (200) -> 6.11 ms (500).
- **Impact**: cheap at pilot scale (sub-millisecond), but it makes both the loop and the
  supervision tick linear in step count. With supervision enabled the panel ticks every 2 s, so at
  500 steps a tick costs ~6 ms of core-thread time every two seconds (0.3 % duty cycle) -
  acceptable, but it is the same O(N) read pattern as B3.
- **Smallest honest fix**: not urgent; a targeted "current step + state counts" read would remove
  it if step counts grow.

### B5 - Single-writer SQLite with no busy handling

- **Code**: `infrastructure/sqlite.py::open_database` sets `PRAGMA foreign_keys = ON` and nothing
  else - no `journal_mode`, no `synchronous`, no `busy_timeout`. Measured `journal_mode = delete`
  on both databases; one connection, default `check_same_thread = True`, owned by the core thread.
- **Measured/derived**: this is a *limit*, not a latency: any second writer (a script, the
  reporting tool, a second panel instance) hits `database is locked` **without waiting**, because
  the SQLite default busy timeout is 0.
- **Smallest honest fix**: `PRAGMA busy_timeout = <ms>` plus a documented "one process per
  database" rule; WAL only if the crash-recovery proofs are re-run against it.

### B6 - Provider calls block the single core thread (derived from constants, not measured)

- **Code**: `infrastructure/_http.py` - `DEFAULT_TIMEOUT_SECONDS = 30.0`,
  `DEFAULT_MAX_RETRIES = 2`, `DEFAULT_BACKOFF_SCHEDULE = (0.0, 1.0, 2.0)`; the adapters call
  `time.sleep` between attempts and the transport is a blocking
  `urllib.request.urlopen(..., timeout=30)`.
- **Derived worst case**: 3 attempts x 30 s + 0 + 1 + 2 s of backoff = **~93 s** during which the
  core thread cannot serve anything else, and `BackgroundRunner.submit()` refuses every new job
  (including `Refresh`, `Pause` and `Abort`). A deliberation run multiplies this by its stage
  count (two seats + lead + two seats + lead = at least five provider round trips).
- **Not measured**: no provider call was made, so real provider latency is unknown by the design
  of this analysis.
- **Smallest honest fix**: (a) a cancellation token consulted between retries, and (b) a UI state
  that says "waiting for a provider" instead of merely looking busy.

### B7 - Write-path cost is small at every measured size

For completeness, the write side is **not** a bottleneck at pilot scale - measured on throw-away
databases:

| Operation | 12 steps | 50 steps | 200 steps | 500 steps |
| --- | --- | --- | --- | --- |
| `plan_loader.preview()` | 0.45 ms | 0.48 ms | 1.24 ms | 2.72 ms |
| `plan_loader.import_plan()` (one transaction, all steps + 1 audit entry) | **2.95 ms** | 3.28 ms | 5.18 ms | **10.80 ms** |
| resulting database size | 200,704 B | 212,992 B | 249,856 B | 319,488 B |

One Excel render measured **20.0 ms** (it writes an `.xlsx` from `zipfile` + `xml`); one
Markdown render measured **1.6 ms**. Both are operator-triggered, never in a loop.

---

## 3. UI responsiveness - event-loop blocking points

### 3.1 What the Tk thread actually does per pump cycle (measured)

| Step | Measured | Notes |
| --- | --- | --- |
| `controller.drain_progress()` / `drain_events()` / `runner.poll()` | queue reads, microseconds | three thread-safe `queue.Queue` drains |
| `controller.apply_result()` | **0.12 - 0.14 ms** | stores plain payloads, classifies failures |
| `controller.view_model()` | **0.16 - 0.52 ms** (flat with step count) | pure dictionary shaping |
| `MainWindow.render(view_model)` | **36.0 - 56.2 ms** (10 runs at 12 steps: avg 38.3 / 42.5 / 43.4 ms across three sessions; first render 91.7 - 104.7 ms) | the whole widget tree is refreshed; 642 widgets |
| `root.update_idletasks()` (layout settle) | **11.4 - 20.7 ms** | |
| full repaint (render + settle) | **49.4 ms** at 12 steps; **51.2 ms avg / 62.4 ms max** at 500 steps | 20 repeated repaints |
| idle pump (`after(100 ms)` firing, nothing to do) | 26 ticks in 3.008 s, **0.0 s CPU** | |

**Key measured finding: the render cost is essentially independent of the step count** (steady
state 37.8 ms at 12 steps, 37.8 ms at 200, 40.2 ms at 500), because it is dominated by the fixed
widget tree rather than by the number of rows. What *does* grow with step count is the
**core-thread** projection (B3), not the widget work.

### 3.2 Is the event loop blocked?

At the measured numbers: **not by the pump, and not by the workflow**. A repaint costs ~40-50 ms
against a 100 ms poll interval, so the loop keeps up; and everything expensive happens on the core
thread, whose result arrives through a queue.

The real blocking points, verified in the code, are these (in order of operator impact):

1. **Startup order**: `app.py` imports `.core` (-> `architecture_assistant.composition`) on the
   main thread, measured at **0.50 - 0.85 s**, *before* the window exists. The operator sees
   nothing during that time. Fix: lazy import inside `GuiApp.run()`.
2. **Plan-file read on the UI thread**: `GuiApp._on_load_plan` runs
   `filedialog.askopenfilename` and then `Path(path).read_text(encoding="utf-8-sig")` directly on
   the Tk thread. For the 12-step pilot plan this is sub-millisecond; a large hand-written plan
   (the importer accepts up to 500 steps) is read and decoded synchronously while the window
   cannot repaint.
3. **Modal dialogs**: `filedialog`, `messagebox` and the plan-preview popup run their own nested
   event loops. That is correct usage, but nothing else in the panel proceeds while one is open.
4. **Preference write and thread join on close**: `_on_close` -> `_remember_layout()` (write the
   layout JSON) -> `runner.stop(timeout=10.0)`. If the core thread is inside a provider call, the
   process waits up to **10 s** for the join (the timeout is a code constant; the resulting
   freeze was not measured because no provider call was made).
5. **`_render()` inside handlers**: many handlers call `self._render()` synchronously right after a
   state change, so an action costs one extra ~40 ms widget pass on the UI thread. That is
   deliberate and cheap, but it is what makes a click feel slower at 500 steps.
6. **Long actions are invisible rather than blocking**: because `submit()` refuses work while
   busy, a second click is silently dropped (`False`). Responsiveness is preserved at the cost of
   the click doing nothing; the UI shows a busy state, and the operator cannot distinguish a 5 ms
   write from a 90 s provider wait.

### 3.3 Summary

The panel's *idle* behaviour is excellent (0 % CPU, 100 ms polls over cheap queue reads) and the
*steady-state* repaint is acceptable (40-50 ms). The three things that make the panel feel
unresponsive are the pre-window composition import, the refusal-based busy model with no
cancellation, and the up-to-10 s close join - none of which is a widget-rendering problem.

---

## 4. File I/O patterns - synchronous vs asynchronous

**Everything in this project is synchronous.** There is no `asyncio`, no `aiofiles`, no I/O thread
pool, no file watcher and no `logging` handler writing to disk. Measured counts across `src/`:
**10 `read_text` calls, 10 `write_text` calls, 1 `read_bytes`, 0 `write_bytes`**, `zipfile` 5
uses (the XLSX writer), `subprocess` 3 uses, `time.sleep` 6 uses (provider retry backoff), `socket`
2 uses (timeout classification in `_http`).

| I/O pattern | Where | Durability / atomicity | Measured cost |
| --- | --- | --- | --- |
| **Atomic durable publish** | `infrastructure/cline.py::_write_atomic` - `NamedTemporaryFile(suffix=".tmp")` -> `flush()` -> `os.fsync()` -> `os.replace()` | durable; readers ignore `.tmp` siblings | one task + one context file per dispatch (not individually benchmarked) |
| **Atomic rename, no fsync** | `gui/layout.py::save_layout`, `composition/provider_settings.py::save_provider_settings`, `gui/project_setup.py::save_preferences` | atomic but not fsynced; a power loss can lose the newest preference | layout write on close; provider settings on save |
| **Atomic archive move** | `infrastructure/cline.py::acknowledge_report` - `os.replace()` into `from_cline/archive/` | same-volume rename; the report is never copied | once per outcome |
| **Report/plan reads** | `cline.read_report` (JSON parse + structural validation), `app._on_load_plan` (`read_text` on the **UI thread**), `composition.load_provider_settings`, `gui.layout.load_layout` (once, before the window is mapped) | read-only | plan preview 0.45 - 2.72 ms |
| **Existence polling** | `cline.read_report` and the channel status use `Path.is_file()`; the panel never lists the exchange directory | cheap `stat` | sub-millisecond |
| **Bulk write** | `plan_loader.import_plan` - all steps + one audit entry in **one** SQLite transaction | atomic | 2.95 - 10.80 ms for 12 - 500 steps |
| **Artifact writes** | `excel_reporting` (XLSX built with `zipfile` + `xml`), `markdown_reporting` | plain writes into `report_dir` | Excel 20.0 ms, Markdown 1.6 ms |
| **Scan reads** | `architecture/scanner.py::scan_directory` - one `read_text` per `*.py` plus `ast.parse` | read-only, no cache | 708 - 942 ms for 71 files / 1.36 MB (AST-bound, not read-bound) |

Implications:

- Blocking I/O on the **core** thread is by design and harmless: that thread exists to be blocked.
- The three I/O operations on the **Tk thread** are the plan-file read, the layout write on close,
  and the actor/preferences write - all small, all synchronous.
- The `fsync` in the worker channel is the only durability guarantee in the codebase, and it is
  paid once per published artifact, never per row.

---

## 5. Threading model

### 5.1 Threads in the process

| Thread | Created by | Owns | Lifetime |
| --- | --- | --- | --- |
| **Main / Tk thread** | the interpreter -> `ctk.CTk()` -> `root.mainloop()` | the widget tree, the `after(100 ms)` pump, every dialog, the layout/actor preference files, the plan-file read | process lifetime |
| **One core thread** (`architecture-assistant-core`, daemon) | `BackgroundRunner.start()` -> `threading.Thread(target=self._run, daemon=True)` | the `Composition`, the **SQLite connection** (created on this thread; `check_same_thread` stays at its default `True`), the file channel, the provider transports | from `start()` until `stop()` joins it |
| process-spawned children | `subprocess` (open the reports folder; test crash drivers) | nothing shared | momentary |

No thread pool, no `asyncio`, no per-request threads. A measured inventory of the tree finds
`threading` in 5 places and `queue.Queue` in 4 - essentially all of them in `gui/core.py`
(`_jobs`, `_results`, `_progress`, `_events`, one `threading.Lock`).

### 5.2 What runs where

| Work | Thread | Why it is safe |
| --- | --- | --- |
| `compose()`, every workflow action, every human write, every provider call, every realization scan, every report build, `audit_tail()` | **core** | the single owner of the connection; SQLite's `check_same_thread` default would raise if anyone else touched it |
| rendering, dialogs, the pump, preference files, the plan-file read | **main** | no core object ever crosses the queues - payloads are plain `dict` / `list` / `str` / `int` / `bool` |
| passing work and results | both | `jobs` (main -> core) and `results` (core -> main) are `queue.Queue`s; `progress` and `events` are core-owned queues the main thread only drains |

### 5.3 Concurrency policy and its measured consequences

- **One job at a time**: `submit()` sets a `_pending` flag under a lock and returns `False` while a
  job is running; a click during work is *dropped*, never queued. The UI stays responsive and the
  operator cannot flood a queue.
- **No cancellation**: nothing interrupts a running job, so the observed action latency is
  "however long the longest provider call takes" (derived worst case ~93 s, section 2 B6).
- **Shutdown**: `stop(timeout=10.0)` enqueues a sentinel and joins; if the core thread is mid-call
  the process waits up to 10 s, because the sentinel is only read between jobs.
- **Supervision ticks** are submitted by the UI thread every 2 s when supervision is enabled, and
  are dropped whenever a job is running - a tick never queues up behind slow work. Disabled (the
  default), no tick is submitted at all; a `supervisor_runtime.tick()` call on a disabled
  composition measured **0.036 ms**.
- **Timers**: only `root.after()`; the core owns no timer, no sleep loop and no daemon scheduler
  (`Scheduler` has no `sleep` - waiting is expressed by returning).

### 5.4 Is the threading model the right one?

For this product, yes: one writer, one connection, one thread makes the crash-recovery story
provable and guarantees the GUI can never race the workflow. The costs are exactly the three
identified above - no parallelism while a provider call runs, no cancellation, and an unbounded
in-flight wait on close. Any future parallelism (for example parallel advisor calls) would have to
preserve the "one thread touches the source of truth" rule and therefore parallelise *adapter*
calls, never core writes.

---

## 6. Scalability limits - what breaks first

Ranked by "what breaks first as the workload grows", each with its measured evidence:

| # | Limit | Measured evidence | Breaks at (estimate) |
| --- | --- | --- | --- |
| 1 | **Audit-table growth** - every refresh reads all audit rows | 1.86 ms @ 234 rows, 19.75 ms @ 2,234, 187 ms @ 22,234, 343 ms @ 42,234 (~8.1 us/row, linear) | already visible around 20k rows; every button press costs >0.2 s of core time |
| 2 | **Step count** - projection, monitor payload and idle tick are O(steps) | 2.6 -> 53 ms projection, 2.6 -> 81 ms monitor payload, 0.5 -> 6.1 ms idle tick, 515 SQL statements per build at 500 steps | above ~500 steps (the draft generator's own cap); at 500 steps one refresh costs ~80-110 ms of core time |
| 3 | **Inspected source-tree size** - the realization gate re-reads and re-parses everything | 708 - 942 ms for 71 files / 1.36 MB, uncached, **inside the write transaction** | a 5x larger target repo (~350 files / ~7 MB) implies several seconds per verification; a monorepo makes `VERIFY` impractical |
| 4 | **Provider latency** - one blocking call owns the core thread | derived: up to ~93 s per call (30 s x 3 attempts + 3 s backoff); a deliberation run needs 5+ round trips | any provider incident or slow model leaves the panel unable to act for minutes, with no cancel |
| 5 | **A second writer / second process** | no `busy_timeout`, `journal_mode = delete`, one connection owned by one thread | immediately: `database is locked` on the first concurrent write |
| 6 | **One job at a time** | `submit()` refuses while `_pending > 0` | as soon as one slow action exists (see #4): the operator cannot even refresh |
| 7 | **File-channel growth** | one task/context file per dispatch plus one archived report per attempt; nothing prunes `from_cline/archive/` | thousands of steps/attempts produce thousands of small files; no measured threshold |
| 8 | **Cost-table growth** | one idempotent row per provider call, read per step by the projection | compounds #1 and #2: more provider use means more rows in both tables |
| 9 | **Widget-tree cost** | 642 widgets, 56 buttons, ~40-50 ms per full repaint, independent of step count | does not scale with data, but it sets the floor: ~20 repaints/s maximum, plus one extra pass per action |
| 10 | **Deliberation latency** | at least five sequential provider round trips per run (2 seats + lead + 2 seats + lead) | minutes per deliberation run, with no partial-result UI |

What does **not** break under the measured loads: memory (48 MB steady, flat with project size),
idle CPU (0 %), the write path (a 500-step import is 10.8 ms) and the UI's render cost with respect
to data volume.

---

## 7. Benchmark baseline

### 7.1 The canonical numbers (pin these to a commit hash)

Environment: Windows, CPython 3.14.0 (`C:\Python314\python.exe`), repository on a OneDrive-synced
path, `customtkinter` installed, no provider calls made.

| # | Metric | Baseline value |
| --- | --- | --- |
| 1 | Interpreter start | 53.8 ms |
| 2 | `import architecture_assistant.composition` | **0.50 - 0.85 s** |
| 3 | `import customtkinter` / `tkinter` / GUI stack | 90.3 - 100.3 ms / 10.1 - 11.4 ms / 12.2 - 20.7 ms |
| 4 | Cold `compose()` (fresh database, no plan) | **41.6 - 78.4 ms** |
| 5 | Cold start to first paint (measured sum) | **~1.26 - 1.61 s** |
| 6 | `MainWindow` construction | 377.7 - 455.4 ms (642 widgets) |
| 7 | First render / steady render / full repaint | 91.7 - 104.7 ms / **36 - 43 ms** / **49.4 ms** (12 steps) and **51.2 ms avg** (500 steps) |
| 8 | `controller.view_model()` | 0.16 - 0.52 ms |
| 9 | Idle process (full stack imported, sleeping) | **0.000 s CPU over 10 s**, 48.09 MB working set (peak 49.14 MB) |
| 10 | Idle Tk loop (withdrawn window, 100 ms pump) | 26 ticks in 3.008 s, **0.0 s CPU** |
| 11 | Loop workload CPU (compose + 12-step import + run + 10 projections + renders) | 0.672 s |
| 12 | One idle loop tick | **0.475 - 0.520 ms** (12 steps) -> 6.11 ms (500 steps) |
| 13 | `run_until_idle()` over 12 steps (3 ticks, 2 transitions, stops at `human-approval-required`) | **8.5 ms** |
| 14 | `plan_loader.import_plan()` | 2.95 ms (12 steps) -> **10.80 ms** (500 steps) |
| 15 | `ReportBuilder.build()` / `Monitor.to_dict()` | 2.57 / 2.60 ms (12 steps) -> 53.05 / **81.48 ms** (500 steps) |
| 16 | SQL statements per `build()` at 500 steps | **515** |
| 17 | Realization scan of the scanned tree | **708 - 942 ms** (71 files / 1,358,305 bytes) |
| 18 | `audit.list()` | 1.86 ms (234 rows) -> **342.86 ms** (42,234 rows) |
| 19 | Excel / Markdown render | 20.0 ms / 1.6 ms |
| 20 | Disabled supervision tick | 0.036 ms |
| 21 | Working-set footprint | **48.09 MB** steady |

### 7.2 How to reproduce (the harness shape)

The numbers were produced by five short scripts that used the project's own modules and throw-away
`%TEMP%` data directories. To re-run them, recreate the same shape:

1. **Import + scan + memory**: `sys.path.insert(0, "src")`; time
   `import architecture_assistant.composition`; then time
   `scan_directory(Path("src/architecture_assistant"))` and validate it with
   `ArchitectureValidator(ARCHITECTURE_CURRENT)`; wrap both in `tracemalloc`.
2. **Runtime**: `CompositionConfig(database_path=<temp>/bench.db, exchange_dir=<temp>/cline,
   report_dir=<temp>/reports, source_root=Path("src/architecture_assistant"),
   project_name="youtube_to_mp3", plan_version="1.0", mode=Mode.MANUAL)`; `compose()`; import a
   synthetic plan with N steps; time `report_builder.build()`, `monitor.to_dict()`,
   `orchestrator.run_once()`, `supervisor_runtime.tick()`; then `close()`.
3. **UI**: build a `ctk.CTk()` root and `withdraw()` it; construct `MainWindow(...)` with no-op
   callbacks; build a `GuiController(_Runner(), ...)` whose runner only needs `submit()`. Time
   `apply_result(JobResult(label="refresh", payload=monitor_payload))`, `view_model()`,
   `window.render(view_model)` and `root.update_idletasks()`; count widgets recursively.
4. **Audit scaling**: insert N synthetic rows into `audit_entries` and time
   `storage.audit.list()` (5 runs per size).
5. **Whole-process CPU/memory**: launch the runtime script with `Start-Process -PassThru` and
   sample `Get-Process -Id` -> `TotalProcessorTime`, `WorkingSet64`, `PeakWorkingSet64`.

### 7.3 What was **not** measured (the honest gaps)

- **The test suite**: no `pytest` run was performed (explicitly out of scope), so suite runtime and
  the README's "3524 passed / 0 failed" figure are **unverified**.
- **A visible window**: the UI measurements used a withdrawn window, so real compositor/repaint
  cost, DPI scaling and monitor refresh behaviour are unmeasured.
- **Provider latency, throughput and cost**: no provider call was made; section 2 B6 is derived
  from constants only.
- **Long-run soak**: audit growth was measured synthetically (rows inserted directly), not by
  running thousands of real workflow steps; no multi-hour memory or handle-leak measurement exists.
- **Larger target repositories**: the realization scan was measured on this repository only
  (71 files); no 500-file or 5,000-file project was measured.
- **Concurrency**: no two-process contention test was run, so the `database is locked` behaviour is
  derived from the absence of `busy_timeout`, not observed.
- **OneDrive interference**: the repository lives on a synced path; the sync client's effect on the
  file timings was not isolated.
- **Per-module memory attribution**: only process working set and `tracemalloc` peaks were captured.
- **Deliberation / review end-to-end latency**: measured only through adapter constants, never as a
  real run.
