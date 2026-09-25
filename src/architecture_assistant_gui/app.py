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
import subprocess
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from architecture_assistant.composition import (
    DEFAULT_REVIEW_QUESTION,
    CompositionConfig,
    sanitize_text,
)
from architecture_assistant.domain.enums import Mode

from .controller import (
    CLEAR_LOGS_INTENT,
    POPUP_INTENTS,
    PROPOSAL_INTENTS,
    PROVIDER_SETTINGS_INTENTS,
    SAVE_SETTINGS_INTENT,
    GuiController,
)
from .core import BackgroundRunner, CoreWorker
from .layout import (
    DEFAULT_LAYOUT_PATH,
    load_layout,
    parse_geometry,
    save_layout,
)
from .windows import (
    POPUP_ACTION_DETAILS,
    POPUP_SIZE_SMALL,
    POPUP_SIZES,
    confirm_dialog,
    open_popup,
)
from .theme import COLORS, configure as configure_theme
from .project_setup import show_project_setup, save_preferences

__all__ = [
    "CONFIRM_LABELS",
    "LAYOUT_PATH",
    "PLAN_CANCEL_INTENT",
    "POLL_INTERVAL_MS",
    "PLAN_FILE_TYPES",
    "POPUP_ERROR_WINDOW",
    "POPUP_NOTICE_WINDOW",
    "POPUP_PLAN_WINDOW",
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

#: The stable window keys of the popups the application itself opens (the
#: spec-driven windows carry their own key in the spec). They are what the layout
#: preference stores, and what makes each of these windows a singleton.
POPUP_ERROR_WINDOW = "popup.error"
POPUP_NOTICE_WINDOW = "popup.notice"
POPUP_PLAN_WINDOW = "popup.plan_preview"

#: The action key of the plan preview's Cancel side. Unlike "import_plan" it is
#: deliberately *not* a core intent: cancelling writes nothing and reaches
#: nothing, so it is handled by the panel alone.
PLAN_CANCEL_INTENT = "cancel_plan"

#: The window titles of a failed action. One readable title, no exception name.
ERROR_TITLE = "Action Failed"
ERROR_TITLE_CRITICAL = "Critical Failure"

#: How much of a failure message the window shows before it belongs in Details.
ERROR_MESSAGE_LIMIT = 1200

#: The verb on a confirmation's confirm button, by action.
#:
#: A confirmation always reads ``[ Cancel ] [ <verb> ]`` with the verb naming
#: what will actually happen - never an anonymous "Yes", and never a different
#: button order in a different window. Anything not listed confirms with
#: "Confirm".
CONFIRM_LABELS: dict[str, str] = {
    "approve": "Approve",
    "approve_proposal": "Approve",
    "approve_and_send": "Approve and Send",
    "reject": "Reject",
    "reject_proposal": "Reject",
    "reject_directive": "Reject",
    "unblock": "Unblock",
    "resolve": "Resolve",
    "abort": "Abort",
    "run_review": "Run Review",
    "deliberation_round1": "Run Round 1",
    "deliberation_cancel": "Cancel Deliberation",
    "synthesize_proposal": "Generate Proposal",
    "deliberation_proposal": "Generate Proposal",
}


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
        "--workspace",
        default=None,
        help="Saved .architecture_assistant/workspace.json created by the project dialog.",
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
    workspace: Mapping[str, Any] = {}
    if args.workspace:
        try:
            raw = json.loads(Path(args.workspace).read_text(encoding="utf-8"))
            workspace = raw if isinstance(raw, Mapping) else {}
        except (OSError, ValueError):
            workspace = {}
    def choice(name: str, explicit: Any, fallback: Any) -> Any:
        return explicit if explicit is not None else workspace.get(name, fallback)
    return CompositionConfig(
        database_path=choice("database_path", args.database, defaults.database_path),
        exchange_dir=Path(choice("exchange_dir", args.exchange_dir, defaults.exchange_dir)),
        report_dir=Path(choice("report_dir", args.report_dir, defaults.report_dir)),
        source_root=Path(choice("source_root", args.source_root, defaults.source_root)),
        project_name=choice("project_name", args.project_name, defaults.project_name),
        plan_version=choice("plan_version", args.plan_version, defaults.plan_version),
        mode=Mode[choice("mode", args.mode, defaults.mode.name)],
        provider_settings_path=Path(choice("provider_settings_path", None, defaults.provider_settings_path)),
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


def _window_exists(window: Any) -> bool:
    """Whether a Tk window is still alive - ``False`` for ``None`` or a corpse.

    The popup registry holds windows the operator may have closed with the window
    manager, so every lookup has to ask the widget itself. Anything that cannot
    answer is treated as gone.
    """
    if window is None:
        return False
    try:
        return bool(window.winfo_exists())
    except BaseException:  # noqa: BLE001 - a destroyed window answers nothing
        return False


def _raise_window(window: Any) -> None:
    """Bring an already-open window to the front and focus it (never fatal).

    This is the whole duplicate rule: a second click on a button whose window is
    already open *shows* that window instead of opening a copy of it, so a utility
    window can never be opened twice by accident.
    """
    if not _window_exists(window):
        return
    try:
        window.deiconify()
        window.lift()
        window.focus_set()
    except BaseException:  # noqa: BLE001 - a closed window is not an error
        pass


def _text_section(
    title: str, lines: Sequence[str], *, empty: str = ""
) -> dict[str, Any]:
    """One text section in the popup contract's own shape.

    The contract a spec (and therefore every window) is built from: a title, no
    columns and no rows, and the lines to render - the same shape the controller
    builds for the windows it owns.
    """
    cleaned = [str(line) for line in lines]
    if not any(line.strip() for line in cleaned):
        cleaned = []
    return {
        "title": str(title),
        "columns": [],
        "rows": [],
        "lines": cleaned,
        "empty": str(empty or "Nothing to show."),
    }


def _save_succeeded(result: Any) -> bool:
    """Whether one finished job was a provider-settings save that landed.

    A failed save is not a success: the panel must keep the typed key and say so
    rather than pretending the configuration was remembered.
    """
    payload = getattr(result, "payload", None)
    if not isinstance(payload, Mapping):
        return False
    outcome = payload.get("settings")
    return isinstance(outcome, Mapping) and bool(outcome.get("saved"))


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
        #: The popup windows that are open right now, by *stable window key*. A
        #: second click on a button whose window is already open raises that
        #: window instead of opening a duplicate - and the key never changes when
        #: a title is reworded.
        self._popups: dict[str, Any] = {}
        #: The geometry of the popups that have been closed in this session. The
        #: live geometry of the open ones is read from the windows themselves when
        #: the layout is written, so both halves end up in the same preference.
        self._popup_geometry: dict[str, str] = {}
        #: The newest failed action, as the plain data its window renders. It is
        #: kept so ``Details...`` can reveal the same failure again without the
        #: panel asking the core for anything.
        self._failure: dict[str, Any] = {}
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
        import customtkinter as ctk

        # CTk paints the dark shell itself, avoiding the white default-Tk flash.
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        ctk.set_window_scaling(1.0)
        ctk.set_widget_scaling(1.0)
        root = ctk.CTk(fg_color=COLORS["shell"])
        configure_theme(root)
        self._root = root
        self._window = MainWindow(
            root,
            on_action=self._on_action,
            on_actor=self._on_actor,
            on_question=self._on_question,
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
            try:
                self._root.state("zoomed")
            except Exception:
                self._root.geometry("1400x900")
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
            snapshot = dict(self._window.layout_snapshot())
        except BaseException:  # noqa: BLE001 - a preference never blocks a close
            return
        # The popup windows are remembered by their *stable key*, next to the main
        # window's own geometry: a popup that is still open contributes where it is
        # now, a popup that was closed earlier contributes where it was left.
        # Neither is domain state - it is a preference, and `save_layout` validates
        # it.
        windows = {**self._popup_geometry, **self._open_popup_geometry()}
        if windows:
            snapshot["windows"] = windows
        save_layout(self._layout_path, snapshot)

    def _open_popup_geometry(self) -> dict[str, str]:
        """The live geometry of every popup that is still on screen."""
        geometry: dict[str, str] = {}
        for window_key, window in self._popups.items():
            if not _window_exists(window):
                continue
            try:
                geometry[window_key] = str(window.winfo_geometry())
            except BaseException:  # noqa: BLE001 - a preference, never a failure
                continue
        return geometry


    def _pump(self) -> None:
        """Drain finished core results - the queue, never the workflow."""
        # Progress first, then the richer events (which set the stage label), so
        # the operator sees which stage the review is in while it is still running.
        self._controller.drain_progress(self._runner.progress())
        self._controller.drain_events(self._runner.events())
        results = self._runner.poll()
        opened = False
        previewed = False
        saved = False
        for result in results:
            self._controller.apply_result(result)
            if result.error is not None:
                self._report_error(result)
            elif result.label == "open":
                opened = True
            elif result.label == "load_plan":
                previewed = True
            elif result.label == SAVE_SETTINGS_INTENT and _save_succeeded(result):
                saved = True
        if saved and self._window is not None:
            # The key is stored now: empty every entry so no secret stays typed
            # in a widget. The render below then shows "key: stored".
            self._window.clear_key_entries()
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
        if key.startswith("nav:"):
            if self._window is not None:
                self._window.select_tab(key.split(":", 1)[1])
            return
        if key == "save_brief":
            if self._window is not None:
                self._controller.set_proposal_requirement(self._window.brief_value())
            self._controller.log("Project brief saved for this session.")
            self._render()
            return
        if key == "validate_draft":
            self._validate_execution_draft()
            return
        if key == "save_plan_draft":
            self._save_execution_draft()
            return
        if key == "copy_cline_handoff":
            self._copy_cline_handoff()
            return
        if key == "open_exchange":
            self._open_exchange_folder()
            return
        if key == "new_project":
            self._show_project_setup()
            return
        if key == "open_guide":
            self._open_user_guide()
            return
        try:
            intent = self._controller.intent(key)
        except ValueError as error:
            self._controller.log(str(error))
            self._render()
            return

        if key == "view_snapshot":
            # The snapshot is a window like every other one: the controller builds
            # its spec and this is the only place that opens it.
            self._open_popup(key)
            return
        if key == CLEAR_LOGS_INTENT:
            # Empties the *view*: the runtime buffer holds nothing that was not
            # already written to the persistent audit trail.
            self._on_clear_logs()
            return
        if key in POPUP_INTENTS:
            self._open_popup(key)
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
        if key in PROVIDER_SETTINGS_INTENTS:
            # The pane headers are the operator's input: read them first, so a
            # click without a focus-out can never test or save a stale provider,
            # model or key. A blank key entry means "keep the stored one".
            if self._window is not None:
                self._controller.set_provider_selection(
                    self._window.provider_settings_values()
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

        if intent.confirmation and not self._confirm(intent):
            self._controller.log(f"{intent.label} cancelled.")
            self._render()
            return

        self._controller.submit(key, reason=reason)
        self._render()

    def _validate_execution_draft(self) -> None:
        if self._window is None:
            return
        text = self._window.execution_plan_value()
        self._controller.set_plan(text, source_file="generated-approved-architecture.json")
        self._controller.submit("load_plan")
        self._render()

    def _save_execution_draft(self) -> None:
        from tkinter import filedialog
        if self._window is None:
            return
        path = filedialog.asksaveasfilename(parent=self._root, title="Save execution plan",
                                            defaultextension=".json", filetypes=(("JSON plan", "*.json"),))
        if not path:
            return
        try:
            Path(path).write_text(self._window.execution_plan_value(), encoding="utf-8")
        except OSError as error:
            self._notice("Cannot save plan", str(error))

    def _copy_cline_handoff(self) -> None:
        if self._root is None:
            return
        text = str((self._controller.view_model().get("channel") or {}).get("handoff") or "")
        self._root.clipboard_clear()
        self._root.clipboard_append(text)
        self._controller.log("Cline handoff instruction copied to the clipboard.")
        self._render()

    def _open_exchange_folder(self) -> None:
        path = str((self._controller.view_model().get("channel") or {}).get("exchange_dir") or "")
        if path:
            Path(path).mkdir(parents=True, exist_ok=True)
            os.startfile(path)

    def _show_project_setup(self) -> None:
        if self._root is None:
            return
        def launch(spec: Mapping[str, Any], actor: str, brief: str) -> None:
            project_root = Path(spec["source_root"]).resolve()
            profile = project_root / ".architecture_assistant" / "workspace.json"
            save_preferences(profile, {**dict(spec), "actor": actor, "brief": brief})
            args = [sys.executable, "-m", "architecture_assistant_gui",
                    "--workspace", str(profile)]
            subprocess.Popen(args, cwd=str(Path(__file__).resolve().parents[2]))
        show_project_setup(self._root, launch, actor=self._controller.actor,
                           brief=self._controller.proposal_requirement)

    def _open_user_guide(self) -> None:
        path = Path(__file__).resolve().parents[2] / "USER_GUIDE_LV.md"
        if path.is_file():
            os.startfile(str(path))
        else:
            self._notice("User guide", "USER_GUIDE_LV.md is missing.")

    def _confirm(self, intent: Any) -> bool:
        """Ask the one modal question this action needs; ``False`` means "no".

        A confirmation is reserved for a decision with a consequence - a provider
        cost, a terminal state, an approval - and the button names that
        consequence instead of saying "Yes". Everything read-only happens without
        a dialog at all.
        """
        if self._root is None:
            return True
        return confirm_dialog(
            self._root,
            title=intent.label,
            message=str(intent.confirmation),
            confirm_label=CONFIRM_LABELS.get(str(intent.key), "Confirm"),
        )

    def _open_popup(self, key: str) -> None:
        """Open the window one display action stands for, from the last payload.

        The application - never a widget - owns this: the spec comes from the
        controller, the geometry from the same layout preference file as the main
        window, and the window itself from :mod:`architecture_assistant_gui.windows`.
        A button whose window is already open raises that window instead of
        opening a second copy of it, and Refresh re-reads the spec, so an open
        window can never show data the main window has moved past.
        """
        if self._root is None:
            return
        spec = self._controller.popup_view(key)
        if not spec:
            # An honest dead end rather than an empty window: this is a display
            # action, so it can never be the reason a panel does anything else.
            self._controller.log(f"{key}: nothing to show in a window yet.")
            self._render()
            return
        self._popup_window(
            self._window_key(spec, key),
            spec,
            size=self._spec_size(spec),
            on_refresh=lambda _window, name=key: self._refresh_popup(name),
        )

    def _window_key(self, spec: Mapping[str, Any], key: str) -> str:
        """The stable identity of a spec's window: its own key, else its action."""
        return str(spec.get("window") or f"popup.{key}")

    @staticmethod
    def _spec_size(spec: Mapping[str, Any]) -> str:
        """A spec's size category, or the default one when it names an unknown."""
        size = str(spec.get("size") or "")
        return size if size in POPUP_SIZES else "medium"

    def _popup_window(
        self,
        window_key: str,
        spec: Optional[Mapping[str, Any]],
        *,
        size: str = "medium",
        modal: bool = False,
        include_copy: bool = True,
        refresh_existing: bool = False,
        on_action: Any = None,
        on_refresh: Any = None,
    ) -> Any:
        """Open - or bring back - the one window this key stands for.

        One window per stable key: a second click focuses the window that is
        already open. Entity-specific details keep a key each (every advisor's
        result is its own window), so "one window per key" never hides something
        the operator asked for - it only stops the same window from piling up.
        The remembered geometry is handed to the window as plain numbers; the
        window clamps it, so a stale position cannot hide it.
        """
        if self._root is None:
            return None
        existing = self._popups.get(window_key)
        if _window_exists(existing):
            if refresh_existing and spec is not None:
                try:
                    existing.render(spec)
                except BaseException:  # noqa: BLE001 - a stale window is not fatal
                    pass
            _raise_window(existing)
            return existing
        placement = self._saved_popup_placement(window_key)
        window = open_popup(
            self._root,
            spec or {},
            size=size,
            width=placement[0] if placement else None,
            height=placement[1] if placement else None,
            x=placement[2] if placement else None,
            y=placement[3] if placement else None,
            modal=modal,
            include_copy=include_copy,
            on_close=lambda open_window, name=window_key: self._remember_popup(
                name, open_window
            ),
            on_action=on_action if on_action is not None else self._on_popup_action,
            on_refresh=on_refresh,
        )
        self._popups[window_key] = window
        return window

    def _on_popup_action(self, key: str) -> None:
        """Route one popup button: the panel's own actions first, then intents.

        Copy, Refresh and Close never arrive here - the window itself owns those -
        so this is only a spec's own extra buttons: a Details view, an export, the
        plan preview's two sides.
        """
        if key == POPUP_ACTION_DETAILS:
            self._show_error_window(reveal=True)
            return
        if key == PLAN_CANCEL_INTENT:
            self._cancel_plan()
            return
        self._on_action(key)

    def _refresh_popup(self, key: str) -> None:
        """Re-read one window's spec from the last payload - nothing is queued."""
        spec = self._controller.popup_view(key)
        if not spec:
            return
        window = self._popups.get(self._window_key(spec, key))
        if not _window_exists(window):
            return
        try:
            window.render(spec)
        except BaseException:  # noqa: BLE001 - a preference never breaks a window
            pass

    def _saved_popup_placement(
        self, window_key: str
    ) -> Optional[tuple[int, int, Optional[int], Optional[int]]]:
        """The remembered size and position of one window, or ``None``.

        The value is re-validated here - it came from a file the operator can
        edit, and it has the same ``WxH[+X+Y]`` shape the main window uses - so a
        damaged preference costs a window its position, never its usability. The
        window itself clamps a position that is off this screen, and centres
        itself when there is none at all.
        """
        windows = self._layout.get("windows")
        if not isinstance(windows, Mapping):
            return None
        return parse_geometry(windows.get(window_key))

    def _remember_popup(self, window_key: str, window: Any) -> None:
        """Keep one popup's geometry *before* it is destroyed (never fatal)."""
        try:
            geometry = str(window.winfo_geometry())
        except BaseException:  # noqa: BLE001 - a preference never blocks a close
            return
        if geometry:
            self._popup_geometry[window_key] = geometry
        self._popups.pop(window_key, None)

    def _ask_reason(self, intent: Any) -> Optional[str]:
        """Ask for the reason; ``None`` means "cancelled, send nothing".

        One modal prompt - the operator's answer *is* the input the core needs -
        and, when it comes back empty, one small non-modal notice instead of a
        second dialog stacked on top of the first.
        """
        from tkinter import simpledialog

        value = simpledialog.askstring(
            intent.label, intent.reason_prompt, parent=self._root
        )
        if value is None:
            return None
        if not value.strip():
            self._notice(
                "Reason Required",
                "A reason is required for this action; nothing was sent.",
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
        """Show the newest failed action in the panel's one error window.

        The window states what happened in one readable, sanitized line; the
        exception type and the rest of the message are behind ``Details...``. No
        stack trace, header, key or raw provider body can reach either view: the
        text goes through the same sanitizer the log contract uses, and nothing
        else is taken from the failure at all. A CRITICAL failure also latches the
        banner, exactly as before.
        """
        error = result.error
        label = sanitize_text(result.label, limit=120)
        error_name = sanitize_text(error.error_name, limit=120)
        message = sanitize_text(error.message, limit=ERROR_MESSAGE_LIMIT)
        critical = bool(error.critical)
        self._failure = {
            "title": ERROR_TITLE_CRITICAL if critical else ERROR_TITLE,
            "message": message or error_name or "The action failed.",
            "critical": critical,
            "details": [
                f"Action: {label}",
                f"Error type: {error_name or 'Error'}",
                f"Critical: {'yes' if critical else 'no'}",
                "",
                message or error_name or "The action failed.",
            ],
        }
        self._show_error_window()

    def _show_error_window(self, reveal: bool = False) -> None:
        """Draw the panel's one error window from the newest failure.

        One window, not one per failure: a repeated failure replaces the report
        that is on screen - the persistent record of every event is the log - so a
        bursting loop cannot flood the desktop with windows.
        """
        failure = self._failure
        if self._root is None or not failure:
            return
        actions: list[dict[str, Any]] = []
        sections: list[dict[str, Any]] = []
        if reveal:
            sections.append(
                _text_section(
                    "Details",
                    list(failure["details"]),
                    empty="No detail was reported.",
                )
            )
        else:
            actions.append(
                {
                    "key": POPUP_ACTION_DETAILS,
                    "label": "Details...",
                    "role": "secondary",
                }
            )
        spec: dict[str, Any] = {
            "title": str(failure["title"]),
            "note": str(failure["message"]),
            "sections": sections,
            "actions": actions,
        }
        self._popup_window(
            POPUP_ERROR_WINDOW,
            spec,
            size=POPUP_SIZE_SMALL,
            refresh_existing=True,
        )

    def _notice(self, title: str, message: str) -> None:
        """A small non-modal notice: what happened, with no decision to make.

        Anything that needs a *decision* is a confirmation and is modal; a notice
        never blocks the panel, so the operator can read it and keep working.
        """
        if self._root is None:
            self._controller.log(f"{title}: {message}")
            self._render()
            return
        spec: dict[str, Any] = {
            "title": str(title),
            "note": str(message),
            "sections": [],
        }
        self._popup_window(
            POPUP_NOTICE_WINDOW,
            spec,
            size=POPUP_SIZE_SMALL,
            include_copy=False,
            refresh_existing=True,
        )

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
        """Offer the preview: Confirm Import imports, Cancel writes nothing.

        A **decision** window, and so the one popup that is modal: the operator's
        answer decides whether the imported plan reaches the core, and nothing
        else in the panel should be touched before it is given. Escape, the X and
        ``[ Cancel ]`` all mean Cancel, so a closed window never imports anything.
        """
        importable = self._controller.plan_importable()
        blocked = self._controller.plan_blocked_reason()
        if importable:
            note = "The loaded plan can be imported into the project."
        elif blocked:
            note = f"This plan cannot be imported: {blocked}"
        else:
            note = "This plan cannot be imported."
        spec: dict[str, Any] = {
            "title": "Plan Preview",
            "note": note,
            "sections": [
                _text_section(
                    "Preview",
                    self._controller.plan_preview_lines(),
                    empty="The preview is empty.",
                )
            ],
            "actions": [
                {
                    "key": PLAN_CANCEL_INTENT,
                    "label": "Cancel",
                    "role": "cancel",
                    "closes": True,
                },
                {
                    "key": "import_plan",
                    "label": "Confirm Import",
                    "role": "primary",
                    "closes": True,
                    "enabled": importable,
                },
            ],
        }
        self._popup_window(
            POPUP_PLAN_WINDOW,
            spec,
            size="large",
            modal=True,
            refresh_existing=True,
        )

    def _cancel_plan(self) -> None:
        """Forget the pending plan: nothing was sent to the core at all."""
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
    workspace: Mapping[str, Any] = {}
    if args.workspace:
        try:
            loaded = json.loads(Path(args.workspace).read_text(encoding="utf-8"))
            workspace = loaded if isinstance(loaded, Mapping) else {}
        except (OSError, ValueError):
            pass
    actor = args.actor if args.actor is not None else str(workspace.get("actor") or load_actor())
    app = GuiApp(config, actor=actor)
    brief = str(workspace.get("brief") or "")
    if brief:
        app.controller.set_proposal_requirement(brief)
    return app.run()
