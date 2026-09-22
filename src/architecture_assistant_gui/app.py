"""Tk application wiring: the window, dialogs and the single result pump.

The application owns three things and nothing else:

* the *display* - widgets built from the view model;
* the *dialogs* - the mandatory actor/reason question, confirmations, errors;
* the *pump* - ``root.after()`` drains finished core results and refreshes the
  view. It never calls the workflow by itself: only an operator command starts
  a loop run, and ``after()`` polls the result queue, never the assistant.

It also owns the panel's two *local preferences* - the remembered operator name
and the remembered window layout - which are read and written only here: no
widget opens a file, neither preference ever reaches the database, and a missing
or damaged preference file costs the operator a default and nothing else.

Tkinter is imported lazily so that :func:`main` can report a missing Tk
installation cleanly, and so that the package can be imported on a machine
without a display.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from architecture_assistant.composition import (
    DEFAULT_REVIEW_QUESTION,
    CompositionConfig,
)
from architecture_assistant.domain.enums import Mode

from .controller import PROPOSAL_INTENTS, GuiController
from .core import BackgroundRunner, CoreWorker
from .layout import (
    DEFAULT_LAYOUT_PATH,
    load_layout,
    parse_geometry,
    save_layout,
)

__all__ = [
    "LAYOUT_PATH",
    "POLL_INTERVAL_MS",
    "PLAN_FILE_TYPES",
    "SETTINGS_PATH",
    "SUPERVISOR_TICK_MS",
    "GuiApp",
    "build_config",
    "load_actor",
    "main",
    "parse_args",
    "save_actor",
]

#: How often the UI drains finished core results (queue polling only).
POLL_INTERVAL_MS = 100

#: How often the application asks the core for one supervision tick while
#: supervision is enabled. The runtime owns no thread and polls nothing: this is
#: the host's schedule, and one tick is submitted at a time (a tick that arrives
#: while another core action runs is simply dropped - the next one catches up).
SUPERVISOR_TICK_MS = 2000

#: The plan-file dialog filter: the plan schema is JSON only.
PLAN_FILE_TYPES: tuple[tuple[str, str], ...] = (
    ("Plan JSON", "*.json"),
    ("All files", "*.*"),
)

#: Where the remembered operator name lives. A GUI-local preference, never
#: domain state and never part of the assistant's database.
SETTINGS_PATH: Path = Path.home() / ".architecture_assistant_gui.json"

#: Where the remembered window layout lives. A local preference as well, but in
#: a file of its own: the assistant's runtime ``data/`` directory is the natural
#: home for a host's own runtime data (it is ignored by Git, like the default
#: database beside it), and a separate file means the layout and the remembered
#: operator name can never overwrite each other. Nothing about the layout is
#: ever written to the database - see :mod:`architecture_assistant_gui.layout`.
LAYOUT_PATH: Path = DEFAULT_LAYOUT_PATH


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse the operator-panel command line."""
    parser = argparse.ArgumentParser(
        prog="python -m architecture_assistant_gui",
        description=(
            "Operator control panel for the Architecture Assistant. Every "
            "action goes through the assistant's public API; the panel never "
            "writes the database directly."
        ),
    )
    parser.add_argument(
        "--database",
        default=None,
        help="SQLite source of truth (default: the assistant's default path).",
    )
    parser.add_argument(
        "--exchange-dir",
        default=None,
        help="Cline file channel directory (default: the assistant's default).",
    )
    parser.add_argument(
        "--report-dir",
        default=None,
        help="Directory for report artifacts (default: the assistant's default).",
    )
    parser.add_argument(
        "--source-root",
        default=None,
        help=(
            "Python source tree the realization gate inspects (default: the "
            "assistant's own package)."
        ),
    )
    parser.add_argument(
        "--actor",
        default=None,
        help="Operator name, prefilled in the panel and remembered locally.",
    )
    parser.add_argument(
        "--project-name",
        default=None,
        help=(
            "The project identity this database belongs to (default: the "
            "assistant's own). It must match the stored project - the bootstrap "
            "refuses a rename - and an imported plan must name it too."
        ),
    )
    parser.add_argument(
        "--plan-version",
        default=None,
        help=(
            "The plan version this database belongs to (default: the "
            "assistant's). A plan file that declares a different plan version is "
            "refused."
        ),
    )
    parser.add_argument(
        "--mode",
        default=None,
        choices=[member.name for member in Mode],
        help=(
            "Initialisation only: the operating mode used when the database is "
            "brand new. An existing project keeps its persisted mode - changing "
            "it is a human action (HumanOverride.set_mode with actor and "
            "reason), never startup magic."
        ),
    )
    parser.add_argument(
        "--supervision",
        action="store_true",
        help=(
            "Enable advisory supervision for this session: the assistant "
            "analyses each worker report before the authoritative review, and "
            "the panel polls the supervision runtime itself. Off by default - "
            "without this flag the loop behaves exactly as it did before "
            "supervision existed and the Supervisor tab only explains that it "
            "is disabled. The supervisor never decides anything: it is "
            "advisory, and every report it blocks still needs a human or a "
            "deterministic gate outcome."
        ),
    )
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> CompositionConfig:
    """The composition configuration: CLI values, else the core defaults."""
    defaults = CompositionConfig()
    return CompositionConfig(
        database_path=args.database or defaults.database_path,
        exchange_dir=Path(args.exchange_dir or defaults.exchange_dir),
        report_dir=Path(args.report_dir or defaults.report_dir),
        source_root=Path(args.source_root or defaults.source_root),
        project_name=args.project_name or defaults.project_name,
        plan_version=args.plan_version or defaults.plan_version,
        mode=(Mode[args.mode] if args.mode else defaults.mode),
        # Off by default and never persisted: supervision is a per-session
        # operator decision, exactly like the panel's other session-scoped
        # behaviour. Nothing about "is supervision enabled" is stored in the
        # database, so a restart cannot silently re-enable or disable it.
        supervision=bool(args.supervision),
    )


def load_actor() -> str:
    """The remembered operator name, or ``""`` - best effort, never fatal."""
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    actor = data.get("actor") if isinstance(data, Mapping) else None
    return actor if isinstance(actor, str) else ""


def save_actor(actor: str) -> None:
    """Remember the operator name locally (never in the database)."""
    try:
        SETTINGS_PATH.write_text(
            json.dumps({"actor": actor}, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


class GuiApp:
    """The Tk application: window, dialogs and the result pump."""

    def __init__(
        self,
        config: CompositionConfig,
        *,
        actor: str = "",
        interval_ms: int = POLL_INTERVAL_MS,
        layout_path: Optional[Path] = None,
    ) -> None:
        if not isinstance(config, CompositionConfig):
            raise ValueError(
                f"config must be a CompositionConfig; got {config!r}"
            )
        if (
            isinstance(interval_ms, bool)
            or not isinstance(interval_ms, int)
            or interval_ms < 1
        ):
            raise ValueError(
                f"interval_ms must be an int >= 1; got {interval_ms!r}"
            )
        if layout_path is not None and not isinstance(
            layout_path, (str, os.PathLike)
        ):
            raise ValueError(
                f"layout_path must be a path or None; got {layout_path!r}"
            )
        self._config = config
        self._worker = CoreWorker(config)
        self._runner = BackgroundRunner(self._worker)
        self._controller = GuiController(
            self._runner,
            actor=actor,
            reports_dir=str(config.report_dir),
            review_question=DEFAULT_REVIEW_QUESTION,
        )
        self._root: Any = None
        self._window: Any = None
        self._interval = interval_ms
        #: Where the window layout is remembered (never the database).
        self._layout_path = (
            Path(layout_path) if layout_path is not None else LAYOUT_PATH
        )
        #: The layout that was loaded on startup; empty means "use the defaults".
        self._layout: dict[str, Any] = {}
        #: When the next supervision tick is due (monotonic seconds). The runtime
        #: owns no thread, so the panel is its clock; nothing is submitted until
        #: supervision is configured.
        self._next_tick_at = 0.0

    @property
    def controller(self) -> GuiController:
        """The UI state machine (useful for diagnostics and tests)."""
        return self._controller

    @property
    def runner(self) -> BackgroundRunner:
        """The core-thread runner."""
        return self._runner

    # -- the event loop ----------------------------------------------------

    def run(self) -> int:
        """Create the window, start the core thread and enter the mainloop."""
        import tkinter as tk

        from .views import MainWindow

        # The remembered layout is read once, before the window exists: a file
        # that is missing or damaged simply means the default layout.
        self._load_layout()
        root = tk.Tk()
        self._root = root
        self._window = MainWindow(
            root,
            on_action=self._on_action,
            on_actor=self._on_actor,
            on_question=self._on_question,
            on_clear_logs=self._on_clear_logs,
            on_proposal_requirement=self._on_proposal_requirement,
            on_revision_feedback=self._on_revision_feedback,
            on_instruction=self._on_instruction,
            layout=self._layout,
        )
        # The toplevel belongs to the application, so its geometry is applied
        # here - before the window is mapped, so the splitters place their
        # remembered sashes at the size they were measured at.
        self._apply_saved_geometry()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._runner.start()
        self._render()
        root.after(self._interval, self._pump)
        root.mainloop()
        return 0

    # -- the remembered window layout --------------------------------------

    def _load_layout(self) -> None:
        """Read the remembered layout; never fatal, never partial state."""
        self._layout = load_layout(self._layout_path)

    def _apply_saved_geometry(self) -> None:
        """Give the window the remembered size and position, when it has one.

        Only the *numbers* are handed to the window, which decides what fits the
        screen: an off-screen or impossible geometry costs the operator the
        position (or the whole geometry) and nothing else.
        """
        if self._window is None:
            return
        parsed = parse_geometry(self._layout.get("geometry"))
        if parsed is None:
            return
        self._window.place_window(*parsed)

    def _remember_layout(self) -> None:
        """Write the operator's current layout - a local preference, not state.

        Called while the window still exists (its geometry is read from it) and
        guarded end to end: nothing about remembering a layout may keep the
        panel from closing.
        """
        if self._window is None:
            return
        try:
            snapshot = self._window.layout_snapshot()
        except BaseException:  # noqa: BLE001 - a preference never blocks a close
            return
        save_layout(self._layout_path, snapshot)

    def _pump(self) -> None:
        """Drain finished core results - the queue, never the workflow."""
        # Progress first, then the richer events (which set the stage label), so
        # the operator sees which stage the review is in while it is still running.
        self._controller.drain_progress(self._runner.progress())
        self._controller.drain_events(self._runner.events())
        results = self._runner.poll()
        opened = False
        previewed = False
        for result in results:
            self._controller.apply_result(result)
            if result.error is not None:
                self._report_error(result)
            elif result.label == "open":
                opened = True
            elif result.label == "load_plan":
                previewed = True
        if results:
            self._render()
        if opened:
            # One explicit follow-up read; the loop itself is never auto-run.
            self._controller.submit("refresh")
        if previewed:
            # The preview is offered, never acted on: Confirm is the operator's.
            self._show_plan_preview()
        self._maybe_tick_supervisor()
        if self._root is not None:
            self._root.after(self._interval, self._pump)

    def _maybe_tick_supervisor(self) -> None:
        """Submit one supervision tick when its interval has elapsed.

        Only when supervision is configured, and only through the runner: the
        tick runs on the core thread like every other action, and a tick that
        arrives while the operator is waiting for a result is dropped instead of
        queueing up. Nothing here touches the composition - the panel only asks
        the core to tick.
        """
        if not self._config.supervision:
            return
        now = time.monotonic()
        if now < self._next_tick_at:
            return
        self._next_tick_at = now + (SUPERVISOR_TICK_MS / 1000.0)
        self._runner.submit(
            "supervisor_tick", lambda worker: worker.supervisor_tick()
        )

    def _render(self) -> None:
        """Push the current view model into the widgets."""
        if self._window is not None:
            self._window.render(self._controller.view_model())

    def _on_close(self) -> None:
        """Remember the operator's layout, stop the core thread, then close.

        The layout is written while the window still exists - the geometry comes
        from the window itself - and before the core thread is stopped, so a
        slow shutdown can never lose it.
        """
        self._remember_layout()
        self._runner.stop()
        if self._root is not None:
            self._root.destroy()

    # -- operator actions --------------------------------------------------

    def _on_action(self, key: str) -> None:
        """Handle one button press; every dialog happens here, never in a view."""
        from tkinter import messagebox

        try:
            intent = self._controller.intent(key)
        except ValueError as error:
            self._controller.log(str(error))
            self._render()
            return

        if key == "view_snapshot":
            self._show_text(
                "Monitor snapshot", self._controller.snapshot_json()
            )
            return
        if key == "open_reports_folder":
            self._open_folder()
            self._render()
            return
        if key == "load_plan":
            self._load_plan_file()
            return
        if key == "run_review":
            # The button uses the question as the operator typed it: read the
            # field first, so a click without a focus-out cannot send a stale one.
            if self._window is not None:
                self._controller.set_review_question(
                    self._window.question_value()
                )
        if key in PROPOSAL_INTENTS:
            # The same rule for the proposal tab's two inputs: what the operator
            # typed is read from the fields before the action is submitted.
            if self._window is not None:
                self._controller.set_proposal_requirement(
                    self._window.proposal_requirement_value()
                )
                self._controller.set_revision_feedback(
                    self._window.revision_feedback_value()
                )

        reason = ""
        if intent.human:
            typed = self._ask_reason(intent)
            if typed is None:
                self._controller.log(
                    f"{intent.label} cancelled - no reason given."
                )
                self._render()
                return
            reason = typed

        if intent.confirmation and not messagebox.askyesno(
            "Confirm", intent.confirmation, parent=self._root
        ):
            self._controller.log(f"{intent.label} cancelled.")
            self._render()
            return

        self._controller.submit(key, reason=reason)
        self._render()

    def _ask_reason(self, intent: Any) -> Optional[str]:
        """Ask for the reason; ``None`` means "cancelled, send nothing"."""
        from tkinter import messagebox, simpledialog

        value = simpledialog.askstring(
            intent.label, intent.reason_prompt, parent=self._root
        )
        if value is None:
            return None
        if not value.strip():
            messagebox.showwarning(
                "Reason required",
                "A reason is required for this action; nothing was sent.",
                parent=self._root,
            )
            return None
        return value.strip()

    def _on_actor(self, value: str) -> None:
        """Remember the operator name after the field loses focus."""
        actor = str(value).strip()
        self._controller.actor = actor
        save_actor(actor)

    def _on_question(self, value: str) -> None:
        """Keep the review question the operator typed - verbatim, never rewritten."""
        self._controller.set_review_question(str(value))

    def _on_proposal_requirement(self, value: str) -> None:
        """Keep the optional requirement addendum - verbatim, never rewritten."""
        self._controller.set_proposal_requirement(str(value))

    def _on_revision_feedback(self, value: str) -> None:
        """Keep the revision feedback the operator typed - verbatim."""
        self._controller.set_revision_feedback(str(value))

    def _on_instruction(self, value: str) -> None:
        """Keep the supervisor instruction the operator edited - verbatim.

        The panel never rewrites, repairs or translates the text: it is published
        exactly as typed when the operator approves the directive, and the audit
        entry records what was sent.
        """
        self._controller.set_instruction(str(value))

    def _on_clear_logs(self) -> None:
        """Empty the log *view* only - no application data is touched."""
        self._controller.clear_log_view()
        self._render()
    def _report_error(self, result: Any) -> None:
        """Show a failed action; a CRITICAL failure also latches the banner."""
        from tkinter import messagebox

        error = result.error
        title = "CRITICAL" if error.critical else "Action failed"
        messagebox.showerror(
            title,
            f"{result.label}\n\n{error.error_name}\n{error.message}",
            parent=self._root,
        )

    def _show_text(self, title: str, text: str) -> None:
        """A read-only window showing text."""
        import tkinter as tk
        from tkinter import scrolledtext

        window = tk.Toplevel(self._root)
        window.title(title)
        window.geometry("900x620")
        body = scrolledtext.ScrolledText(window, wrap="none")
        body.pack(fill="both", expand=True)
        body.insert("1.0", text)
        body.configure(state="disabled")

    def _open_folder(self) -> None:
        """Open the reports folder, or report its path when that is impossible."""
        target = Path(self._controller.reports_dir)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            self._controller.log(f"Cannot create {target}: {error}")
            return
        opener = getattr(os, "startfile", None)
        if opener is None:
            self._controller.log(f"Reports folder: {target}")
            return
        try:
            opener(str(target))
        except OSError as error:
            self._controller.log(f"Cannot open {target}: {error}")

    # -- the plan loader ---------------------------------------------------

    def _load_plan_file(self) -> None:
        """Pick a plan file, read it as text and ask the core for a preview.

        The panel reads the file (the operator chose it here) and sends *text* to
        the core: the assistant never reads a path and the panel never parses,
        repairs or validates a plan itself.
        """
        from tkinter import filedialog

        path = filedialog.askopenfilename(
            title="Load plan",
            filetypes=PLAN_FILE_TYPES,
            parent=self._root,
        )
        if not path:
            self._controller.log("Load Plan cancelled.")
            self._render()
            return
        try:
            # utf-8-sig: a plan file saved by a Windows editor may carry a BOM.
            text = Path(path).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as error:
            self._controller.log(f"Cannot read {path}: {error}")
            self._render()
            return
        self._controller.set_plan(text, source_file=str(path))
        self._controller.submit("load_plan")
        self._render()

    def _show_plan_preview(self) -> None:
        """Offer the preview: Confirm Import imports, Cancel writes nothing."""
        import tkinter as tk
        from tkinter import scrolledtext, ttk

        window = tk.Toplevel(self._root)
        window.title("Plan preview")
        window.geometry("780x560")
        body = scrolledtext.ScrolledText(window, wrap="word", height=20)
        body.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        body.insert("1.0", "\n".join(self._controller.plan_preview_lines()))
        body.configure(state="disabled")

        buttons = ttk.Frame(window, padding=(8, 4))
        buttons.pack(fill="x")
        confirm = ttk.Button(
            buttons,
            text="Confirm Import",
            command=lambda: self._confirm_plan(window),
        )
        confirm.grid(row=0, column=0, padx=(0, 6))
        if not self._controller.plan_importable():
            confirm.state(["disabled"])
        ttk.Button(
            buttons, text="Cancel", command=lambda: self._cancel_plan(window)
        ).grid(row=0, column=1)

    def _confirm_plan(self, window: Any) -> None:
        """Import the loaded plan - the reason is asked by the action handler."""
        window.destroy()
        self._on_action("import_plan")

    def _cancel_plan(self, window: Any) -> None:
        """Forget the pending plan: nothing was sent to the core at all."""
        window.destroy()
        self._controller.clear_plan()
        self._controller.log("Plan import cancelled; nothing was written.")
        self._render()


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Console entry point: ``python -m architecture_assistant_gui``."""
    args = parse_args(argv)
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print(
            "Tkinter is not available in this Python installation, so the "
            "operator panel cannot start. Use a Python build with Tk support.",
            file=sys.stderr,
        )
        return 3
    config = build_config(args)
    actor = args.actor if args.actor is not None else load_actor()
    return GuiApp(config, actor=actor).run()
