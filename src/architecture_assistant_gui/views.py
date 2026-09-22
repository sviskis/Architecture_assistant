"""Tk widgets for the operator panel: layout and rendering only.

Every string this module shows comes from the controller's view model, and every
button calls back into the application with an intent key. No widget here ever
touches the assistant, a repository or a connection, and none of them decides
anything: a disabled button is advice, and the core still validates the action.

The *layout* is the operator's: every major region sits in a splitter, and this
module both applies a remembered layout (sash positions and the open tab) and
reports the current one back. Reading and writing the layout file belongs to the
layout store and the application - no widget here opens a file.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import scrolledtext, ttk
from typing import Any, Callable, Mapping, Optional, Sequence

from .controller import (
    CLEAR_LOGS_INTENT,
    CONNECTION_INTENT_KEYS,
    INTENTS,
    POPUP_ADVISOR_KEYS,
    POPUP_ARCHITECTURE,
    POPUP_AUDIT,
    POPUP_CONFLICTS,
    POPUP_COST,
    POPUP_JUDGE,
    POPUP_LOGS,
    POPUP_PROJECT,
    POPUP_REPORTS,
    POPUP_RISKS,
    POPUP_SUPERVISOR,
    SAVE_SETTINGS_INTENT,
)

__all__ = [
    "ADVISOR_SPLIT_MIN_SIZE",
    "COMPACT_FACT_COLUMNS",
    "FACT_COLUMNS",
    "GROUPS",
    "MAIN_SPLIT_MIN_SIZE",
    "MAIN_SPLIT_TOP_START_SHARE",
    "MIN_ON_SCREEN_EDGE",
    "PROPOSAL_SPLIT_MIN_SIZE",
    "REVIEW_RESULT_SPLIT_MIN_SIZE",
    "REVIEW_SECTION_SPLIT_MIN_SIZE",
    "REVIEW_SPLIT_MIN_SIZE",
    "SPLIT_KEYS",
    "SUPERVISOR_SPLIT_MIN_SIZE",
    "UTILITY_BUTTONS",
    "MainWindow",
]

#: The compact utility bar: one button per secondary window, in display order.
#: Every one of them is a *display* action - it opens a window built from the
#: last payload - so they share one row in the header instead of each owning a
#: permanent tab. The log view's own "clear" action sits here too: clearing it
#: deletes nothing, because the runtime view is not storage.
UTILITY_BUTTONS: tuple[tuple[str, str], ...] = (
    (POPUP_COST, "Costs"),
    (POPUP_LOGS, "Logs"),
    (CLEAR_LOGS_INTENT, "Clear Logs"),
    (POPUP_AUDIT, "Audit"),
    (POPUP_RISKS, "Risks"),
    (POPUP_REPORTS, "Reports"),
    (POPUP_PROJECT, "Project Details"),
    (POPUP_ARCHITECTURE, "Architecture Details"),
)

#: The groups buttons are arranged in, in display order.
GROUPS: tuple[str, ...] = (
    "Plan",
    "Review",
    "Proposal",
    "Supervisor",
    "Loop",
    "Project",
    "Approval",
    "Step control",
    "Reports",
    "Monitoring",
)

#: The label/value columns every facts table uses (one advisor pane, the merged
#: evidence, the review header, the supervisor header and every popup's fact
#: section). The tables that used to live here for logs, audit, risks, cost and
#: the conflict list are popup tables now, and their columns are declared next to
#: the payload that fills them, in the controller.
FACT_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("field", "Field", 150),
    ("value", "Value", 250),
)

#: The Architecture Proposal tab's tables. Every one is read-only: the tab shows
#: the persisted proposal (a design for the *managed project*) and never writes.
PROPOSAL_MODULE_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("name", "Module", 150),
    ("responsibility", "Responsibility", 320),
    ("dependencies", "Dependencies", 220),
    ("boundary_notes", "Boundary notes", 260),
)

PROPOSAL_FLOW_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("from", "From", 160),
    ("to", "To", 160),
    ("description", "Description", 630),
)

PROPOSAL_DEPENDENCY_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("name", "Dependency", 180),
    ("purpose", "Purpose", 340),
    ("impact", "Impact", 430),
)

PROPOSAL_RISK_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("severity", "Severity", 90),
    ("probability", "Probability", 90),
    ("impact", "Impact", 90),
    ("description", "Description", 300),
    ("mitigation", "Mitigation", 380),
)

PROPOSAL_ADR_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("title", "Candidate", 200),
    ("recommended_status", "Recommended", 110),
    ("decision", "Decision", 320),
    ("rationale", "Rationale", 320),
)

PROPOSAL_PHASE_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("phase", "Phase", 110),
    ("goal", "Goal", 340),
    ("scope", "Scope", 500),
)

PROPOSAL_HISTORY_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("proposal_id", "Proposal id", 220),
    ("revision_no", "Rev", 45),
    ("status", "Status", 130),
    ("created_at", "Created", 150),
    ("decided_by", "Decided by", 100),
    ("decision_reason", "Reason", 300),
)


#: Minimum size of **every pane** of one draggable splitter, in pixels. These
#: exist so a drag can never collapse a region to nothing: whatever the operator
#: does, each pane keeps at least this much room and can always be dragged back.
#: A minimum is a floor, never a layout - no widget position is hard-coded.
#:
#: Tk 8.6's ``ttk::panedwindow`` has no ``-minsize`` pane option (it arrived in
#: later Tk versions), so the floor is enforced by ``_clamp_split`` on every
#: finished drag instead of by an option this Tk would reject.
MAIN_SPLIT_MIN_SIZE = 150
REVIEW_SPLIT_MIN_SIZE = 90
REVIEW_RESULT_SPLIT_MIN_SIZE = 70
REVIEW_SECTION_SPLIT_MIN_SIZE = 60
ADVISOR_SPLIT_MIN_SIZE = 180
SUPERVISOR_SPLIT_MIN_SIZE = 160
PROPOSAL_SPLIT_MIN_SIZE = 130

#: The deliberation workbench (Step 29). The chair's pane is the centre and the
#: widest of the three, because the review packet, the progress checklist, the
#: compact counts and every stage control live there; the two architects flank it.
DELIBERATION_SPLIT_MIN_SIZE = 180
DELIBERATION_PANE_MIN_SIZE = 150

#: Where the deliberation splitter starts: 27% | 46% | 27%, so the chair owns the
#: middle and the two architects get equal, narrower panes. It is a start, not a
#: rule - the operator drags it and the position is remembered like every other.
DELIBERATION_PANE_FRACTIONS: tuple[float, float] = (0.27, 0.73)

#: Where the main splitter starts: the top area is given its natural height, but
#: never more than this share of the window, so the notebook (the tab content)
#: always owns most of it and a window resize grows the notebook more.
MAIN_SPLIT_TOP_START_SHARE = 0.45

#: The stable name of every splitter whose sash positions are remembered, in the
#: order they are built. A remembered layout stores one sash list per name, so a
#: saved position is matched to a splitter by *name* - never by a widget path,
#: which differs on every run. A name that no longer exists (or a splitter whose
#: panes were rebuilt with a different count) is simply not restored.
SPLIT_KEYS: tuple[str, ...] = (
    "main",
    "review",
    "review_advisors",
    "review_results",
    "review_bottom",
    "review_merged",
    "review_judge",
    "supervisor",
    "proposal",
    "deliberation",
)

#: The four compact rows one advisor pane keeps. Everything else about the
#: advisor - finding id, confidence, anchor, step, evidence references, tokens,
#: cost and the whole finding text - is one click away in its own window.
COMPACT_FACT_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("field", "Field", 90),
    ("value", "Value", 180),
)

#: How much of a remembered window position must still be on screen, in pixels,
#: before it is used. A position that would hide the panel (a monitor that is
#: gone, a resolution that shrank) is dropped and only the size is kept, because
#: a stale preference must never be able to hide the window it belongs to.
MIN_ON_SCREEN_EDGE = 60


def _even_sashes(count: int) -> tuple[float, ...]:
    """Sash fractions that split ``count`` panes evenly (thirds for three)."""
    return tuple(index / count for index in range(1, count))


def _fraction_sashes(*fractions: float) -> Callable[[int], tuple[int, ...]]:
    """A split start that places each sash at a fraction of the split's size."""
    return lambda total: tuple(int(total * fraction) for fraction in fractions)


def _add_pane(
    split: Any, child: Any, minimum: int, *, weight: int = 1
) -> None:
    """Add one draggable pane to ``split`` and keep its floor alive.

    ``weight`` decides how extra space is shared when the window (or the split)
    grows; ``minimum`` is the size the pane can never be dragged below. The floor
    is applied on ``<ButtonRelease-1>`` through ``after_idle``, i.e. after Tk's
    own drag handling, so a drag is never fought while the mouse is down.
    """
    split.add(child, weight=weight)
    split.bind(
        "<ButtonRelease-1>",
        lambda _event, s=split, m=minimum: s.after_idle(
            lambda: _clamp_split(s, m)
        ),
        add="+",
    )


def _clamp_split(split: Any, minimum: int) -> None:
    """Push the sashes of one split back to where every pane keeps its floor.

    Tk 8.6 offers no pane minimum at all, so a pane could otherwise be dragged
    to zero width and become impossible to grab again. The sashes are recomputed
    left to right, which keeps the pane order and the total size.
    """
    if split is None or minimum <= 0:
        return
    panes = split.panes()
    if len(panes) < 2:
        return
    horizontal = str(split.cget("orient")) == "horizontal"
    total = split.winfo_width() if horizontal else split.winfo_height()
    if total <= 1:
        return
    try:
        sash = int(split.cget("sashwidth"))
    except tk.TclError:  # pragma: no cover - every ttk theme has a sash
        sash = 0
    room = minimum + sash
    if total < room * len(panes):
        # Smaller than the floors allow: share what there is, never overflow.
        room = max(1, total // len(panes))
    start = 0
    for index in range(len(panes) - 1):
        current = split.sashpos(index)
        lowest = start + room
        highest = total - room * (len(panes) - index - 1)
        position = max(lowest, min(current, highest))
        if position != current:
            split.sashpos(index, position)
        start = position

#: The colour of one entry or pane per level. Kept in one place so the Logs tab
#: and the advisor panes can never disagree about what "warning" looks like.
_LEVEL_COLOURS: dict[str, str] = {
    "INFO": "#1a1a1a",
    "WARN": "#a05000",
    "ERROR": "#b00020",
}


class MainWindow:
    """Builds the operator panel and renders one view model at a time."""

    def __init__(
        self,
        root: tk.Misc,
        *,
        on_action: Callable[[str], None],
        on_actor: Callable[[str], None],
        on_question: Callable[[str], None],
        on_proposal_requirement: Callable[[str], None] = lambda _text: None,
        on_revision_feedback: Callable[[str], None] = lambda _text: None,
        on_instruction: Callable[[str], None] = lambda _text: None,
        layout: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self._root = root
        #: The layout the host remembered (window geometry, the open tab, one
        #: sash list per splitter, one geometry per popup title). Only ever read
        #: through ``_saved_sashes``, ``_restore_saved_tab`` and the host's own
        #: popup geometry lookup, which validate what they touch: a damaged
        #: layout costs the operator a default, never a working panel.
        self._layout: Mapping[str, Any] = (
            layout if isinstance(layout, Mapping) else {}
        )
        self._on_action = on_action
        self._on_actor = on_actor
        self._on_question = on_question
        self._on_proposal_requirement = on_proposal_requirement
        self._on_revision_feedback = on_revision_feedback
        self._on_instruction = on_instruction
        self.buttons: dict[str, ttk.Button] = {}
        self._text: dict[str, tk.StringVar] = {}
        #: The draggable splits. Every major region lives in one of these, so the
        #: operator resizes the window with the mouse instead of accepting a
        #: fixed pixel layout. ``None`` only until the corresponding tab is built.
        self._main_split: Any = None
        self._top_area: Any = None
        self._notebook: Any = None
        self._review_split: Any = None
        self._review_bottom_split: Any = None
        self._review_results_split: Any = None
        #: The two vertical splitters inside the shared results: merged evidence
        #: over conflicts, and the judge over the advisory decision.
        self._review_section_splits: list[Any] = []
        self._proposal_split: Any = None
        self._supervisor_split: Any = None
        self._supervisor_frames: dict[str, Any] = {}
        #: The deliberation workbench (Step 29): its own horizontal splitter, the
        #: three panes (Agent A | Lead | Agent B) and the stage buttons. Empty
        #: until the tab is built and filled by every render.
        self._deliberation_split: Any = None
        self._deliberation_panes: dict[str, Any] = {}
        self._deliberation_buttons: dict[str, Any] = {}
        #: The splitters whose starting sash positions have already been placed.
        self._placed_splits: set[str] = set()
        #: The splitters the operator has dragged by hand in this session. A
        #: remembered position keeps winning over Tk's own re-arrange until that
        #: happens; after it, the splitters are Tk's for the rest of the session.
        self._operator_splits: set[str] = set()
        #: Every splitter under its stable name (see ``SPLIT_KEYS``), filled by
        #: ``_register_splits`` once every tab has been built: the single place
        #: that knows the layout's vocabulary.
        self._splits: dict[str, Any] = {}
        self._monitor: Any = None
        self._banner: Any = None
        self._actor: Any = None
        self._question: Any = None
        self._review_header: Any = None
        self._panes_frame: Any = None
        #: One entry per advisor pane, built and rebuilt only when the number of
        #: advisors changes (in practice: once, at startup).
        self._panes: list[dict[str, Any]] = []
        self._merged: Any = None
        #: The compact sections the tab keeps instead of a table: the conflict
        #: count, the judge line and the review's total cost, each with the button
        #: that opens its detail window.
        self._review_conflicts: Any = None
        self._conflicts_button: Any = None
        self._judge: Any = None
        self._judge_button: Any = None
        self._decision: Any = None
        self._cost: Any = None
        self._cost_button: Any = None
        #: The Architecture Proposal tab: the managed project's design. All
        #: read-only tables plus the two operator inputs the tab collects.
        self._proposal_requirement_field: Any = None
        self._revision_feedback_field: Any = None
        self._proposal_header: Any = None
        self._proposal_facts: Any = None
        self._proposal_digest: Any = None
        self._proposal_history: Any = None
        self._proposal_sections: dict[str, Any] = {}
        #: The Supervisor tab: one compact header table, three read-only summary
        #: panes, one editable instruction and the button to the detail window.
        #: All plain data.
        self._supervisor_header: Any = None
        self._supervisor_panes: dict[str, Any] = {}
        self._supervisor_details: Any = None
        self._instruction_field: Any = None
        self._build()

    # -- construction ------------------------------------------------------

    def _var(self, key: str) -> tk.StringVar:
        value = self._text.get(key)
        if value is None:
            value = tk.StringVar(value="-")
            self._text[key] = value
        return value

    def _build(self) -> None:
        root = self._root
        root.title("Architecture Assistant - operator panel")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)

        # One horizontal splitter right below the project header: everything the
        # operator *does* (current work and the action buttons) sits in the top
        # pane, the tab notebook in the bottom one. The notebook owns the larger
        # weight, so a window resize gives it most of the new vertical space.
        self._main_split = ttk.PanedWindow(root, orient="vertical")
        self._main_split.grid(row=0, column=0, sticky="nsew")

        top_area = ttk.Frame(self._main_split)
        top_area.columnconfigure(0, weight=1)
        top_area.rowconfigure(3, weight=1)
        _add_pane(self._main_split, top_area, MAIN_SPLIT_MIN_SIZE, weight=1)
        self._top_area = top_area

        self._build_top()
        self._build_operator()
        self._build_middle()
        self._build_tabs()
        self._build_status()

        # The window's own geometry is the application's to apply (it owns the
        # toplevel); everything inside the window is named and restored here.
        self._register_splits()
        self._restore_saved_tab()

        # Every splitter is given a sensible start the first time it is laid out;
        # after that the sashes belong to the operator. A remembered layout wins
        # over that start (see ``_start_split``). The advisor split is a special
        # case: its panes (one per configured advisor) only exist after the first
        # render, so its start is count-aware (see ``_advisor_sashes``) and it is
        # re-placed whenever that number changes.
        self._place_split_start(
            "main",
            self._main_split_start,
            MAIN_SPLIT_MIN_SIZE,
        )
        self._place_split_start(
            "review",
            _fraction_sashes(0.12, 0.55),
            REVIEW_SPLIT_MIN_SIZE,
        )
        self._place_split_start(
            "review_results",
            _fraction_sashes(0.5),
            REVIEW_RESULT_SPLIT_MIN_SIZE,
        )
        self._place_split_start(
            "review_bottom",
            _fraction_sashes(0.72),
            REVIEW_RESULT_SPLIT_MIN_SIZE,
        )
        for key in ("review_merged", "review_judge"):
            self._place_split_start(
                key,
                _fraction_sashes(0.5),
                REVIEW_SECTION_SPLIT_MIN_SIZE,
            )
        self._place_split_start(
            "supervisor",
            _fraction_sashes(*_even_sashes(3)),
            SUPERVISOR_SPLIT_MIN_SIZE,
        )
        self._place_split_start(
            "proposal",
            _fraction_sashes(0.38),
            PROPOSAL_SPLIT_MIN_SIZE,
        )
        self._place_split_start(
            "review_advisors",
            self._advisor_sashes,
            ADVISOR_SPLIT_MIN_SIZE,
        )

    def _build_top(self) -> None:
        frame = ttk.Frame(self._top_area, padding=(8, 6, 8, 2))
        frame.grid(row=0, column=0, sticky="ew")
        for column in range(5):
            frame.columnconfigure(column, weight=1)

        fields = (
            ("project", "Project"),
            ("mode", "Mode"),
            ("project_state", "Project state"),
            ("architecture", "Architecture"),
            ("health", "Health"),
        )
        for column, (key, label) in enumerate(fields):
            ttk.Label(frame, text=f"{label}:").grid(
                row=0, column=column, sticky="w"
            )
            ttk.Label(
                frame, textvariable=self._var(key), font=("", 9, "bold")
            ).grid(row=1, column=column, sticky="w")

        self._build_utility(frame)

        self._banner = ttk.Label(
            self._top_area,
            textvariable=self._var("banner"),
            padding=(10, 3),
            anchor="w",
        )
        self._banner.grid(row=1, column=0, sticky="ew")

    def _build_utility(self, parent: Any) -> None:
        """The compact utility bar: one small button per secondary window.

        It is a single row inside the header, so it costs almost no vertical
        space while keeping every secondary view one click away. The buttons are
        ordinary intents: the *application* opens the window, and this module
        still only ever calls ``self._on_action``.
        """
        bar = ttk.Frame(parent, padding=(0, 4, 0, 0))
        bar.grid(row=2, column=0, columnspan=5, sticky="ew")
        for index, (key, label) in enumerate(UTILITY_BUTTONS):
            button = ttk.Button(
                bar, text=label, command=lambda k=key: self._on_action(k)
            )
            button.grid(row=0, column=index, sticky="w", padx=(0, 4))
            self.buttons.setdefault(key, button)

    def _build_operator(self) -> None:
        frame = ttk.Frame(self._top_area, padding=(8, 2, 8, 4))
        frame.grid(row=2, column=0, sticky="ew")
        ttk.Label(frame, text="Operator:").grid(row=0, column=0, sticky="w")
        self._actor = ttk.Entry(frame, width=28)
        self._actor.grid(row=0, column=1, sticky="w", padx=(4, 8))
        self._actor.bind(
            "<FocusOut>", lambda _event: self._on_actor(self._actor.get())
        )
        ttk.Label(
            frame,
            text=(
                "Required for every human action; a reason is asked per action."
            ),
        ).grid(row=0, column=2, sticky="w")

    def _build_middle(self) -> None:
        frame = ttk.Frame(self._top_area, padding=(8, 4))
        frame.grid(row=3, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(0, weight=1)

        work = ttk.LabelFrame(frame, text="Current work", padding=(8, 6))
        work.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        work.columnconfigure(1, weight=1)
        work_rows = (
            ("work_plan", "Plan"),
            ("work_step_no", "Step"),
            ("work_title", "Title"),
            ("work_phase", "Phase"),
            ("work_state", "State"),
            ("work_attempt", "Attempt"),
            ("work_next_step", "Next step"),
            ("work_stopped", "Stopped because"),
            ("work_channel", "Worker channel"),
            ("work_blocking", "Blocking steps"),
        )
        for row, (key, label) in enumerate(work_rows):
            ttk.Label(work, text=f"{label}:").grid(
                row=row, column=0, sticky="w"
            )
            ttk.Label(
                work, textvariable=self._var(key), anchor="w"
            ).grid(row=row, column=1, sticky="ew")
        ttk.Label(
            work,
            textvariable=self._var("work_note"),
            foreground="#a05000",
            wraplength=380,
            justify="left",
        ).grid(
            row=len(work_rows),
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(6, 0),
        )

        actions = ttk.LabelFrame(frame, text="Actions", padding=(8, 6))
        actions.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        for index, group in enumerate(GROUPS):
            group_frame = ttk.LabelFrame(actions, text=group, padding=(6, 4))
            group_frame.grid(
                row=index // 2,
                column=index % 2,
                sticky="nsew",
                padx=2,
                pady=2,
            )
            group_frame.columnconfigure(0, weight=1)
            row = 0
            for intent in INTENTS:
                if intent.group != group:
                    continue
                button = ttk.Button(
                    group_frame,
                    text=intent.label,
                    command=lambda key=intent.key: self._on_action(key),
                )
                button.grid(row=row, column=0, sticky="ew", pady=1)
                self.buttons[intent.key] = button
                row += 1

    def _build_tabs(self) -> None:
        """The tab notebook: the bottom pane of the main splitter."""
        pane = ttk.Frame(self._main_split, padding=(8, 2, 8, 4))
        pane.columnconfigure(0, weight=1)
        pane.rowconfigure(0, weight=1)
        _add_pane(self._main_split, pane, MAIN_SPLIT_MIN_SIZE, weight=3)

        notebook = ttk.Notebook(pane)
        notebook.grid(row=0, column=0, sticky="nsew")
        self._notebook = notebook

        monitor = ttk.Frame(notebook, padding=6)
        self._monitor = ttk.Treeview(
            monitor, columns=("field", "value"), show="headings", height=10
        )
        self._monitor.heading("field", text="Field")
        self._monitor.heading("value", text="Value")
        self._monitor.column("field", width=200, anchor="w")
        self._monitor.column("value", width=560, anchor="w")
        self._monitor.pack(fill="both", expand=True)
        notebook.add(monitor, text="Monitor")

        self._build_review_tab(notebook)
        self._build_deliberation_tab(notebook)
        self._build_proposal_tab(notebook)
        self._build_supervisor_tab(notebook)
        # Logs, Audit, Risks and Reports are **popups** now: they are things the
        # operator consults rather than works in, so they open from the utility
        # bar (and from the section they belong to) instead of each owning a tab.
        # Nothing behind them changed - the controller still builds every row, so
        # no functionality was removed by taking the tabs away.

    def _build_supervisor_tab(self, notebook: Any) -> None:
        """The Supervisor tab: the advisory analysis of the current report.

        Three panes and one header. The left pane is what the **Assistant** owns
        (its task, state, baseline and deterministic context), the center pane is
        the worker and the exact report it submitted, and the right pane is the
        **advisory** supervisor: what it proposes, why, and what the fail-closed
        gate answered. A proposal here is never authority - the authoritative
        workflow is on the Monitor/Audit tabs and is owned by the core.

        The three panes are the panes of one horizontal splitter, so the operator
        decides how much width each of them gets.
        """
        frame = ttk.Frame(notebook, padding=6)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        note = ttk.Label(
            frame,
            textvariable=self._var("supervisor_status"),
            wraplength=980,
            justify="left",
        )
        note.grid(row=0, column=0, sticky="w")

        self._supervisor_header = self._table_in(
            frame,
            tuple(key for key, _heading, _width in FACT_COLUMNS),
            columns=FACT_COLUMNS,
            height=6,
            row=1,
        )

        panes = ttk.PanedWindow(frame, orient="horizontal")
        panes.grid(row=2, column=0, sticky="nsew", pady=(4, 0))
        self._supervisor_split = panes
        for key in ("assistant_pane", "cline_pane", "supervisor_pane"):
            box = ttk.LabelFrame(
                panes, text=key.replace("_pane", "").title()
            )
            _add_pane(panes, box, SUPERVISOR_SPLIT_MIN_SIZE, weight=1)
            box.columnconfigure(0, weight=1)
            box.rowconfigure(0, weight=1)
            table = ttk.Treeview(
                box, columns=("field", "value"), show="headings", height=14
            )
            table.heading("field", text="Field")
            table.heading("value", text="Value")
            table.column("field", width=150, anchor="w")
            table.column("value", width=260, anchor="w")
            table.grid(row=0, column=0, sticky="nsew")
            self._supervisor_panes[key] = table
            self._supervisor_frames[key] = box

        instruction_row = ttk.Frame(frame)
        instruction_row.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        instruction_row.columnconfigure(1, weight=1)
        ttk.Label(instruction_row, text="Edit Instruction:").grid(
            row=0, column=0, sticky="w"
        )
        self._instruction_field = ttk.Entry(instruction_row)
        self._instruction_field.grid(row=0, column=1, sticky="ew", padx=(6, 6))
        self._instruction_field.bind(
            "<FocusOut>",
            lambda _event: self._on_instruction(self._instruction_field.get()),
        )
        self._instruction_field.bind(
            "<Return>",
            lambda _event: self._on_instruction(self._instruction_field.get()),
        )
        ttk.Label(
            instruction_row,
            text=(
                "Sent with Approve & Send; empty means the supervisor's own "
                "instruction is published verbatim."
            ),
        ).grid(row=0, column=2, sticky="w")

        # The supervision *identity* - ids, hashes, the status history, the
        # deadlines and the runtime schedule - is one window away instead of a
        # permanent table: the tab keeps the summary, the popup keeps the rest.
        details_row = ttk.Frame(frame)
        details_row.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        details_row.columnconfigure(1, weight=1)
        ttk.Label(
            details_row,
            text=(
                "Supervision identity, report hash, status history, deadlines "
                "and polling details:"
            ),
        ).grid(row=0, column=0, sticky="w")
        details = ttk.Button(
            details_row,
            text="Technical Details",
            command=lambda: self._on_action(POPUP_SUPERVISOR),
        )
        details.grid(row=0, column=1, sticky="w", padx=(6, 0))
        self._supervisor_details = details
        self.buttons.setdefault(POPUP_SUPERVISOR, details)
        notebook.add(frame, text="Supervisor")

    def _table_in(
        self,
        parent: Any,
        keys: tuple[str, ...],
        *,
        columns: tuple[tuple[str, str, int], ...] = (),
        height: int = 6,
        row: int = 0,
    ) -> Any:
        """A read-only table inside an existing frame."""
        table = ttk.Treeview(parent, columns=keys, show="headings", height=height)
        for index, key in enumerate(keys):
            if columns:
                _key, heading, width = columns[index]
            else:
                heading, width = key, 160
            table.heading(key, text=heading)
            table.column(key, width=width, anchor="w")
        table.grid(row=row, column=0, sticky="nsew")
        return table

    def _build_review_tab(self, notebook: Any) -> None:
        """The Architecture Review tab: question, header, three advisor panes, shared results.

        The layout is the point of this tab: the three advisors sit **side by
        side in one window** so their answers can be read against each other, and
        everything that is *shared* - merged evidence, conflicts, the judge, the
        advisory decision and the cost - sits below them.

        Every one of those regions sits in a draggable splitter: question and
        header on top, the advisor panes in the middle, the shared results and
        the cost at the bottom. The three advisor panes are the panes of their
        own horizontal splitter, so one advisor can be widened without touching
        the other two.
        """
        frame = ttk.Frame(notebook, padding=6)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        # -- the vertical splitter of the whole tab --------------------------
        split = ttk.PanedWindow(frame, orient="vertical")
        split.grid(row=0, column=0, sticky="nsew")
        self._review_split = split

        summary = ttk.Frame(split)
        summary.columnconfigure(0, weight=1)
        _add_pane(split, summary, REVIEW_SPLIT_MIN_SIZE, weight=0)

        question_row = ttk.Frame(summary)
        question_row.grid(row=0, column=0, sticky="ew")
        question_row.columnconfigure(1, weight=1)
        ttk.Label(question_row, text="Review question:").grid(
            row=0, column=0, sticky="w"
        )
        self._question = ttk.Entry(question_row)
        self._question.grid(row=0, column=1, sticky="ew", padx=(6, 6))
        self._question.bind(
            "<FocusOut>",
            lambda _event: self._on_question(self._question.get()),
        )
        self._question.bind(
            "<Return>", lambda _event: self._on_question(self._question.get())
        )
        ttk.Label(
            question_row,
            text="Required; sent verbatim to all three advisors.",
        ).grid(row=0, column=2, sticky="w")

        header_frame = ttk.LabelFrame(
            summary, text="Review header", padding=(6, 4)
        )
        header_frame.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        header_frame.columnconfigure(0, weight=1)
        header_frame.rowconfigure(0, weight=1)
        self._review_header = self._table_in(
            header_frame,
            tuple(key for key, _h, _w in FACT_COLUMNS),
            columns=FACT_COLUMNS,
            height=9,
        )

        # -- the three advisors, side by side ------------------------------
        panes_frame = ttk.LabelFrame(
            split,
            text="Advisor responses (read-only, side by side)",
            padding=(6, 4),
        )
        panes_frame.columnconfigure(0, weight=1)
        panes_frame.rowconfigure(0, weight=1)
        _add_pane(split, panes_frame, REVIEW_SPLIT_MIN_SIZE, weight=2)
        # One horizontal splitter for the advisors: each pane is dragged on its
        # own, so OpenAI can be widened without touching Claude or Grok.
        self._panes_frame = ttk.PanedWindow(panes_frame, orient="horizontal")
        self._panes_frame.grid(row=0, column=0, sticky="nsew")

        # -- the shared results, below them --------------------------------
        # The bottom region is a splitter of its own: the merged results above,
        # the cost of the review below - each can be given the height it needs.
        bottom = ttk.PanedWindow(split, orient="vertical")
        _add_pane(split, bottom, REVIEW_SPLIT_MIN_SIZE, weight=2)
        self._review_bottom_split = bottom

        results = ttk.PanedWindow(bottom, orient="horizontal")
        _add_pane(bottom, results, REVIEW_RESULT_SPLIT_MIN_SIZE, weight=3)
        self._review_results_split = results

        left = ttk.PanedWindow(results, orient="vertical")
        _add_pane(
            results, left, REVIEW_RESULT_SPLIT_MIN_SIZE, weight=1
        )
        right = ttk.PanedWindow(results, orient="vertical")
        _add_pane(
            results, right, REVIEW_RESULT_SPLIT_MIN_SIZE, weight=1
        )
        self._review_section_splits = [left, right]

        merged_frame = ttk.LabelFrame(
            left, text="Merged evidence", padding=(6, 4)
        )
        merged_frame.columnconfigure(0, weight=1)
        merged_frame.rowconfigure(0, weight=1)
        _add_pane(
            left, merged_frame, REVIEW_SECTION_SPLIT_MIN_SIZE, weight=1
        )
        self._merged = self._table_in(
            merged_frame,
            tuple(key for key, _h, _w in FACT_COLUMNS),
            columns=FACT_COLUMNS,
            height=8,
        )

        conflicts_frame = ttk.LabelFrame(
            left, text="Evidence conflicts", padding=(6, 4)
        )
        conflicts_frame.columnconfigure(0, weight=1)
        _add_pane(
            left, conflicts_frame, REVIEW_SECTION_SPLIT_MIN_SIZE, weight=1
        )
        # One compact line and one button: an empty conflict list must not hold a
        # large table, so the count is the whole visible cost of having none.
        conflicts_row = ttk.Frame(conflicts_frame)
        conflicts_row.grid(row=0, column=0, sticky="new")
        self._review_conflicts = conflicts_row
        ttk.Label(
            conflicts_row,
            textvariable=self._var("review_conflicts_summary"),
            font=("", 9, "bold"),
        ).grid(row=0, column=0, sticky="w")
        self._conflicts_button = ttk.Button(
            conflicts_row,
            text="View Conflicts",
            command=lambda: self._on_action(POPUP_CONFLICTS),
        )
        self._conflicts_button.grid(row=0, column=1, sticky="w", padx=(6, 0))
        self.buttons.setdefault(POPUP_CONFLICTS, self._conflicts_button)
        ttk.Label(
            conflicts_frame,
            text=(
                "A conflict is explicit metadata two advisors declared over one "
                "anchor - never inferred from wording, severity or overlap."
            ),
            wraplength=420,
            justify="left",
        ).grid(row=1, column=0, sticky="nw")

        judge_frame = ttk.LabelFrame(
            right, text="Judge result (a separate layer)", padding=(6, 4)
        )
        judge_frame.columnconfigure(0, weight=1)
        _add_pane(
            right, judge_frame, REVIEW_SECTION_SPLIT_MIN_SIZE, weight=1
        )
        # The judge is advisory and usually absent, so its panel is a single
        # honest line: it never reserves a text area for a result that does not
        # exist, and the full output is one button away.
        judge_row = ttk.Frame(judge_frame)
        judge_row.grid(row=0, column=0, sticky="new")
        self._judge = judge_row
        ttk.Label(
            judge_row,
            textvariable=self._var("review_judge_summary"),
            font=("", 9, "bold"),
        ).grid(row=0, column=0, sticky="w")
        self._judge_button = ttk.Button(
            judge_row,
            text="View Judge Details",
            command=lambda: self._on_action(POPUP_JUDGE),
        )
        self._judge_button.grid(row=0, column=1, sticky="w", padx=(6, 0))
        self.buttons.setdefault(POPUP_JUDGE, self._judge_button)
        ttk.Label(
            judge_frame,
            text=(
                "The judge only explains an explicitly declared evidence "
                "conflict; it never replaces the deterministic verdict."
            ),
            wraplength=420,
            justify="left",
        ).grid(row=1, column=0, sticky="nw")

        decision_frame = ttk.LabelFrame(
            right,
            text="Advisory decision (explains, never overrides)",
            padding=(6, 4),
        )
        decision_frame.columnconfigure(0, weight=1)
        decision_frame.rowconfigure(0, weight=1)
        _add_pane(
            right, decision_frame, REVIEW_SECTION_SPLIT_MIN_SIZE, weight=1
        )
        self._decision = scrolledtext.ScrolledText(
            decision_frame, height=7, wrap="word"
        )
        self._decision.grid(row=0, column=0, sticky="nsew")
        self._decision.configure(state="disabled")

        cost_frame = ttk.LabelFrame(
            bottom, text="Cost of this review", padding=(6, 4)
        )
        _add_pane(bottom, cost_frame, REVIEW_RESULT_SPLIT_MIN_SIZE, weight=1)
        cost_frame.columnconfigure(0, weight=1)
        # A single total line plus one button. The detail - per provider, the
        # judge, the records and the unavailable state - lives in the popup, so
        # the tab never spends a table on it.
        cost_row = ttk.Frame(cost_frame)
        cost_row.grid(row=0, column=0, sticky="ew")
        self._cost = cost_row
        ttk.Label(
            cost_row,
            textvariable=self._var("review_total_cost"),
            font=("", 9, "bold"),
        ).grid(row=0, column=0, sticky="w")
        self._cost_button = ttk.Button(
            cost_row,
            text="Cost Details",
            command=lambda: self._on_action(POPUP_COST),
        )
        self._cost_button.grid(row=0, column=1, sticky="w", padx=(6, 0))
        self.buttons.setdefault(POPUP_COST, self._cost_button)
        ttk.Label(
            cost_frame,
            text=(
                "Cost telemetry is reported by the core; when it is unavailable "
                "this line says so and no amount is invented."
            ),
            wraplength=700,
            justify="left",
        ).grid(row=1, column=0, sticky="w")

        ttk.Label(
            frame, textvariable=self._var("review_progress"), foreground="#a05000"
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Label(
            frame, textvariable=self._var("review_status"), wraplength=900,
            justify="left",
        ).grid(row=2, column=0, sticky="ew")
        # The provider configuration's own status line: where the settings came
        # from, whether they were saved, and the explicit "provider settings
        # invalid" when the file could not be used. It never shows a key.
        ttk.Label(
            frame,
            textvariable=self._var("review_provider_status"),
            wraplength=900,
            justify="left",
        ).grid(row=3, column=0, sticky="ew")
        notebook.add(frame, text="Architecture Review")

    def question_value(self) -> str:
        """The question exactly as the operator typed it (never rewritten)."""
        return "" if self._question is None else self._question.get()

    def _build_deliberation_tab(self, notebook: Any) -> None:
        """The Deliberation workbench: Agent A | Lead Agent | Agent B.

        The centre pane is the **chair**: the deliberation id, the stage, the
        six-stage progress checklist, the compact counts (agreements, conflicts,
        open questions, risks) and the stage controls. The two side panes are the
        two independent architects. Every stage button is offered only when the
        run is actually at that stage - the controller computes the valid set, so
        this tab never has to invent an enabled/disabled rule.

        Nothing here reads a repository, a provider or the database: the tab
        renders one plain payload and reports which action the operator pressed.
        """
        frame = ttk.Frame(notebook, padding=6)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        ttk.Label(frame, textvariable=self._var("deliberation_status"), wraplength=980,
                  justify="left").grid(row=0, column=0, sticky="ew")
        ttk.Label(frame, textvariable=self._var("deliberation_requirement"),
                  wraplength=980, justify="left").grid(row=1, column=0, sticky="ew")

        panes = ttk.PanedWindow(frame, orient="horizontal")
        panes.grid(row=2, column=0, sticky="nsew", pady=(4, 0))
        self._deliberation_split = panes

        self._deliberation_panes: dict[str, Any] = {}
        for slot, title in (
            ("agent_a", "Agent A"),
            ("lead", "Lead Agent"),
            ("agent_b", "Agent B"),
        ):
            pane = ttk.LabelFrame(panes, text=title, padding=(6, 4))
            pane.columnconfigure(0, weight=1)
            pane.rowconfigure(1, weight=1)
            ttk.Label(pane, textvariable=self._var(f"deliberation_{slot}_summary"),
                      wraplength=320, justify="left").grid(
                row=0, column=0, sticky="ew"
            )
            table = ttk.Treeview(
                pane, columns=("field", "value"), show="headings", height=10
            )
            table.heading("field", text="Field")
            table.heading("value", text="Value")
            table.column("field", width=110, anchor="w")
            table.column("value", width=190, anchor="w")
            scroll = ttk.Scrollbar(pane, orient="vertical", command=table.yview)
            table.configure(yscrollcommand=scroll.set)
            table.grid(row=1, column=0, sticky="nsew")
            scroll.grid(row=1, column=1, sticky="ns")
            _add_pane(panes, pane, DELIBERATION_PANE_MIN_SIZE, weight=1)
            self._deliberation_panes[slot] = {"frame": pane, "rows": table}

        # The controls live under the centre pane, so they are always beside the
        # review they act on.
        controls = ttk.Frame(self._deliberation_panes["lead"]["frame"])
        controls.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        self._deliberation_buttons: dict[str, Any] = {}
        for index, key in enumerate(
            (
                "deliberation_round1",
                "deliberation_lead_review",
                "deliberation_round2",
                "deliberation_synthesis",
                "deliberation_proposal",
                "deliberation_cancel",
            )
        ):
            button = ttk.Button(
                controls,
                text=key.replace("deliberation_", "").replace("_", " ").title(),
                command=lambda name=key: self._on_action(name),
            )
            button.grid(row=0, column=index % 3, sticky="ew", padx=(0, 4), pady=2)
            self._deliberation_buttons[key] = button

        ttk.Label(
            frame,
            textvariable=self._var("deliberation_cost"),
            wraplength=980,
            justify="left",
        ).grid(row=3, column=0, sticky="ew", pady=(4, 0))
        notebook.add(frame, text="Deliberation")

    def _render_deliberation(self, view: Mapping[str, Any]) -> None:
        """Render one deliberation payload: plain data in, widgets out."""
        if not self._deliberation_panes:
            return
        self._var("deliberation_status").set(str(view.get("status", "")))
        self._var("deliberation_requirement").set(
            str(view.get("requirement", ""))
        )
        self._var("deliberation_cost").set(str(view.get("cost", "")))
        for slot in ("agent_a", "lead", "agent_b"):
            pane = self._deliberation_panes.get(slot)
            if pane is None:
                continue
            rows = pane["rows"]
            for item in rows.get_children():
                rows.delete(item)
            for row in tuple(view.get(f"{slot}_rows") or ()):
                values = tuple(row)
                rows.insert("", "end", values=values[:2])
            self._var(f"deliberation_{slot}_summary").set(
                str(view.get(f"{slot}_summary", ""))
            )
        enabled = {
            key: bool(value)
            for key, value in (view.get("buttons") or {}).items()
        }
        for key, button in self._deliberation_buttons.items():
            allowed = enabled.get(key, False)
            button.state(["!disabled"] if allowed else ["disabled"])

    def _build_proposal_tab(self, notebook: Any) -> None:
        """The Architecture Proposal tab: the *managed project's* design.

        This tab is the visible half of one boundary. It shows a managed
        project's architecture proposal and the three human decisions it can
        receive; it never shows, and can never change, the Architecture
        Assistant's own architecture baseline - that lives in
        ``architecture_versions``, is declared in code and is enforced by the
        deterministic validator, not by anything here.
        """
        frame = ttk.Frame(notebook, padding=6)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        requirement_row = ttk.Frame(frame)
        requirement_row.grid(row=0, column=0, sticky="ew")
        requirement_row.columnconfigure(1, weight=1)
        ttk.Label(requirement_row, text="Requirement addendum:").grid(
            row=0, column=0, sticky="w"
        )
        self._proposal_requirement_field = ttk.Entry(requirement_row)
        self._proposal_requirement_field.grid(
            row=0, column=1, sticky="ew", padx=(6, 6)
        )
        self._proposal_requirement_field.bind(
            "<FocusOut>",
            lambda _event: self._on_proposal_requirement(
                self._proposal_requirement_field.get()
            ),
        )
        self._proposal_requirement_field.bind(
            "<Return>",
            lambda _event: self._on_proposal_requirement(
                self._proposal_requirement_field.get()
            ),
        )
        ttk.Label(
            requirement_row,
            text=(
                "Optional; when empty the review question is used verbatim."
            ),
        ).grid(row=0, column=2, sticky="w")

        # -- the splitter between the summary and the detail ----------------
        # The proposal *summary* (header, status, the facts and the evidence
        # digest) and the proposal *detail* (the read-only architecture sections
        # and the stored proposals) are two panes: either one can be given the
        # room it needs, and the detail starts with the larger share.
        proposal_split = ttk.PanedWindow(frame, orient="vertical")
        proposal_split.grid(row=1, column=0, sticky="nsew", pady=(6, 4))
        self._proposal_split = proposal_split

        summary = ttk.Frame(proposal_split)
        summary.columnconfigure(0, weight=1)
        summary.rowconfigure(2, weight=1)
        _add_pane(
            proposal_split, summary, PROPOSAL_SPLIT_MIN_SIZE, weight=1
        )

        detail = ttk.Frame(proposal_split)
        detail.columnconfigure(0, weight=1)
        detail.rowconfigure(0, weight=1)
        _add_pane(proposal_split, detail, PROPOSAL_SPLIT_MIN_SIZE, weight=2)

        header_frame = ttk.LabelFrame(
            summary, text="Proposal header", padding=(6, 4)
        )
        header_frame.grid(row=0, column=0, sticky="nsew")
        header_frame.columnconfigure(0, weight=1)
        header_frame.rowconfigure(0, weight=1)
        self._proposal_header = self._table_in(
            header_frame,
            tuple(key for key, _h, _w in FACT_COLUMNS),
            columns=FACT_COLUMNS,
            height=10,
        )

        ttk.Label(
            summary,
            textvariable=self._var("proposal_status"),
            wraplength=900,
            justify="left",
        ).grid(row=1, column=0, sticky="ew", pady=(0, 4))

        top = ttk.Frame(summary)
        top.grid(row=2, column=0, sticky="nsew")
        top.columnconfigure(0, weight=1)
        top.columnconfigure(1, weight=1)
        top.rowconfigure(0, weight=1)

        facts_frame = ttk.LabelFrame(
            top, text="Summary, rules and questions", padding=(6, 4)
        )
        facts_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        facts_frame.columnconfigure(0, weight=1)
        facts_frame.rowconfigure(0, weight=1)
        self._proposal_facts = self._table_in(
            facts_frame,
            tuple(key for key, _h, _w in FACT_COLUMNS),
            columns=FACT_COLUMNS,
            height=6,
        )

        digest_frame = ttk.LabelFrame(
            top,
            text="Evidence the proposal was built from (traceability only)",
            padding=(6, 4),
        )
        digest_frame.grid(row=0, column=1, sticky="nsew")
        digest_frame.columnconfigure(0, weight=1)
        digest_frame.rowconfigure(0, weight=1)
        self._proposal_digest = self._table_in(
            digest_frame,
            tuple(key for key, _h, _w in FACT_COLUMNS),
            columns=FACT_COLUMNS,
            height=6,
        )

        # -- the proposed architecture, read-only --------------------------
        sections_frame = ttk.LabelFrame(
            detail, text="Proposed architecture (read-only)", padding=(6, 4)
        )
        sections_frame.grid(row=0, column=0, sticky="nsew")
        sections_frame.columnconfigure(0, weight=1)
        sections_frame.rowconfigure(0, weight=1)
        sections = ttk.Notebook(sections_frame)
        sections.grid(row=0, column=0, sticky="nsew")
        self._proposal_sections = {
            "modules": self._table(
                sections, "Modules / boundaries", PROPOSAL_MODULE_COLUMNS
            ),
            "data_flows": self._table(
                sections, "Data flow", PROPOSAL_FLOW_COLUMNS
            ),
            "external_dependencies": self._table(
                sections,
                "External dependencies",
                PROPOSAL_DEPENDENCY_COLUMNS,
            ),
            "risks": self._table(sections, "Risks", PROPOSAL_RISK_COLUMNS),
            "adr_candidates": self._table(
                sections, "ADR candidates", PROPOSAL_ADR_COLUMNS
            ),
            "implementation_phases": self._table(
                sections, "Implementation phases", PROPOSAL_PHASE_COLUMNS
            ),
        }

        # -- the revision feedback the operator writes ----------------------
        feedback_frame = ttk.LabelFrame(
            frame,
            text="Revision feedback (required to request a revision)",
            padding=(6, 4),
        )
        feedback_frame.grid(row=2, column=0, sticky="ew", pady=(0, 4))
        feedback_frame.columnconfigure(0, weight=1)
        self._revision_feedback_field = tk.Text(
            feedback_frame, height=3, wrap="word"
        )
        self._revision_feedback_field.grid(row=0, column=0, sticky="ew")
        self._revision_feedback_field.bind(
            "<FocusOut>",
            lambda _event: self._on_revision_feedback(
                self._revision_feedback_field.get("1.0", "end-1c")
            ),
        )

        ttk.Label(
            frame,
            textvariable=self._var("proposal_note"),
            wraplength=900,
            foreground="#a05000",
            justify="left",
        ).grid(row=3, column=0, sticky="ew", pady=(0, 4))

        history_frame = ttk.LabelFrame(
            detail,
            text="Stored proposals (newest first - history is never rewritten)",
            padding=(6, 4),
        )
        history_frame.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        history_frame.columnconfigure(0, weight=1)
        history_frame.rowconfigure(0, weight=1)
        self._proposal_history = self._table_in(
            history_frame,
            tuple(key for key, _h, _w in PROPOSAL_HISTORY_COLUMNS),
            columns=PROPOSAL_HISTORY_COLUMNS,
            height=5,
        )
        notebook.add(frame, text="Architecture Proposal")

    def proposal_requirement_value(self) -> str:
        """The requirement addendum as typed (never rewritten)."""
        if self._proposal_requirement_field is None:
            return ""
        return self._proposal_requirement_field.get()

    def revision_feedback_value(self) -> str:
        """The revision feedback as typed (never rewritten)."""
        if self._revision_feedback_field is None:
            return ""
        return self._revision_feedback_field.get("1.0", "end-1c")


    # -- the three advisor panes -------------------------------------------

    def _sync_panes(self, panels: tuple[Mapping[str, Any], ...]) -> None:
        """Keep exactly one pane per advisor panel - created, never duplicated.

        The number of panes follows the model (one per configured advisor), so a
        fourth advisor would get its own pane instead of being hidden. Panes are
        rebuilt only when that number changes, which in practice means once.
        """
        if len(self._panes) != len(panels):
            for pane in self._panes:
                if pane["frame"] in self._panes_frame.panes():
                    self._panes_frame.forget(pane["frame"])
                pane["frame"].destroy()
            self._panes = [
                self._build_pane(index) for index in range(len(panels))
            ]
            if self._placed_splits:
                # The window already started the splitters; a rebuilt one gets
                # the same even start (thirds for three advisors).
                self._place_advisor_start()
        for pane, panel in zip(self._panes, panels):
            self._fill_pane(pane, panel)

    def _refresh_pane_summaries(
        self, panels: tuple[Mapping[str, Any], ...]
    ) -> None:
        """Re-fill the panes once the settings are in, so the summary matches.

        The pane's own provider and model controls are what the compact rows read,
        so the summary is drawn *after* the settings have been applied: a pane can
        never show a model it is not about to use.
        """
        for pane, panel in zip(self._panes, panels):
            self._fill_pane(pane, panel)

    def _build_pane(self, index: int) -> dict[str, Any]:
        """One advisor pane: a compact settings header, then its result.

        The pane is added to the horizontal advisor splitter, one weight each, so
        all advisors start equally wide and every sash between them can be
        dragged on its own.

        The header is the operator's half of the pane - which provider and model
        this advisor uses, its key and the two configuration buttons. It is kept
        to three compact rows on purpose: the *result* is what the pane exists to
        compare, so the header must not push it off the screen. The key entry is
        masked and is never filled from a stored value; an empty entry means
        "keep whatever is stored", so a secret never has to be retyped to save
        the rest of the form and never has to be shown again.
        """
        pane = ttk.LabelFrame(self._panes_frame, text="Advisor", padding=(6, 4))
        _add_pane(self._panes_frame, pane, ADVISOR_SPLIT_MIN_SIZE, weight=1)
        pane.columnconfigure(0, weight=1)
        pane.rowconfigure(2, weight=2)
        pane.rowconfigure(3, weight=3)

        settings = ttk.LabelFrame(
            pane, text="Provider settings", padding=(4, 2)
        )
        settings.grid(row=0, column=0, sticky="ew")
        settings.columnconfigure(1, weight=1)

        ttk.Label(settings, text="Provider:").grid(row=0, column=0, sticky="w")
        provider = ttk.Combobox(settings, state="readonly", width=12)
        provider.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(4, 0))
        provider.bind(
            "<<ComboboxSelected>>",
            lambda _event, i=index: self._on_provider_changed(i),
        )

        ttk.Label(settings, text="Model:").grid(
            row=1, column=0, sticky="w", pady=(2, 0)
        )
        model = ttk.Combobox(settings, width=18)
        model.grid(
            row=1, column=1, columnspan=2, sticky="ew", padx=(4, 0), pady=(2, 0)
        )

        ttk.Label(settings, text="API Key:").grid(
            row=2, column=0, sticky="w", pady=(2, 0)
        )
        key = ttk.Entry(settings, show="*", width=18)
        key.grid(row=2, column=1, sticky="ew", padx=(4, 2), pady=(2, 0))
        reveal = tk.BooleanVar(master=self._root, value=False)
        show = ttk.Checkbutton(
            settings,
            text="Show",
            variable=reveal,
            command=lambda i=index: self._toggle_key_reveal(i),
        )
        show.grid(row=2, column=2, sticky="w", pady=(2, 0))

        buttons = ttk.Frame(settings)
        buttons.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(3, 0))
        test = ttk.Button(
            buttons,
            text="Test Connection",
            command=lambda i=index: self._on_test_connection(i),
        )
        test.grid(row=0, column=0, sticky="w")
        save = ttk.Button(
            buttons,
            text="Save",
            command=lambda: self._on_action(SAVE_SETTINGS_INTENT),
        )
        save.grid(row=0, column=1, sticky="w", padx=(6, 0))
        # The full result - finding id, confidence, anchor, step, evidence refs,
        # tokens, cost, the sanitized error and the whole finding text - opens in
        # the advisor's own window. The main pane keeps the summary.
        details_key = (
            POPUP_ADVISOR_KEYS[index] if index < len(POPUP_ADVISOR_KEYS) else ""
        )
        details = ttk.Button(buttons, text="Details...")
        if details_key:
            details.configure(
                command=lambda k=details_key: self._on_action(k)
            )
        else:
            details.state(["disabled"])
        details.grid(row=0, column=2, sticky="w", padx=(6, 0))
        ttk.Label(settings, textvariable=self._var(f"pane_settings_{index}")).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(2, 0)
        )

        status_key = f"pane_status_{index}"
        status = ttk.Label(
            pane,
            textvariable=self._var(status_key),
            font=("", 9, "bold"),
        )
        status.grid(row=1, column=0, sticky="w", pady=(4, 0))
        facts = self._table_in(
            pane,
            tuple(key for key, _h, _w in COMPACT_FACT_COLUMNS),
            columns=COMPACT_FACT_COLUMNS,
            height=4,
            row=2,
        )
        summary_key = f"pane_summary_{index}"
        summary = ttk.Label(
            pane,
            textvariable=self._var(summary_key),
            wraplength=300,
            justify="left",
        )
        summary.grid(row=3, column=0, sticky="new", pady=(4, 0))
        return {
            "frame": pane,
            "status": status,
            "status_key": status_key,
            "summary": summary,
            "summary_key": summary_key,
            "facts": facts,
            "details": details,
            "details_key": details_key,
            "provider": provider,
            "model": model,
            "key": key,
            "reveal": reveal,
            "save": save,
            "test": test,
        }

    def _fill_pane(self, pane: dict[str, Any], panel: Mapping[str, Any]) -> None:
        """Fill one advisor pane from its model - text, colour and summary only."""
        pane["frame"].configure(text=str(panel.get("name") or "Advisor"))
        # the label owns a textvariable, so the text must be set through it -
        # setting ``text=`` next to a ``textvariable`` is silently ignored by Tk
        self._var(str(pane["status_key"])).set(
            str(panel.get("status_text") or "-")
        )
        pane["status"].configure(
            foreground=_LEVEL_COLOURS.get(
                str(panel.get("level") or "INFO"), _LEVEL_COLOURS["INFO"]
            )
        )
        self._fill(
            pane["facts"],
            self._compact_rows(pane, panel),
            tuple(key for key, _h, _w in COMPACT_FACT_COLUMNS),
        )
        self._var(str(pane["summary_key"])).set(str(panel.get("summary") or ""))

    def _compact_rows(
        self, pane: dict[str, Any], panel: Mapping[str, Any]
    ) -> tuple[tuple[str, str], ...]:
        """The four compact rows the main pane keeps for one advisor.

        Provider and model come from the pane's own controls, so the summary can
        never disagree with the settings right above it; the execution status is
        the bold line above the table, and severity and relation are here because
        they are what the operator compares between the three panes.
        """
        return (
            ("Provider", str(pane["provider"].get() or "-")),
            ("Model", str(pane["model"].get() or "-")),
            ("Severity", str(panel.get("severity") or "-")),
            ("Relation", str(panel.get("relation") or "-")),
        )

    # -- the per-pane provider settings header -----------------------------

    def _options_of(self, pane: dict[str, Any]) -> list[dict[str, Any]]:
        """The provider choices of one pane, as the last render reported them."""
        return list(pane.get("options") or [])

    def _provider_token(self, pane: dict[str, Any], label: str) -> str:
        """The provider token behind the label shown in the combobox."""
        for option in self._options_of(pane):
            if str(option.get("label")) == label:
                return str(option.get("provider"))
        return label.strip().lower()

    def _provider_default(self, pane: dict[str, Any], token: str) -> str:
        """That provider's documented default model, or ``""`` when unknown."""
        for option in self._options_of(pane):
            if str(option.get("provider")) == token:
                return str(option.get("default_model") or "")
        return ""

    def _on_provider_changed(self, index: int) -> None:
        """A new provider: offer *its* documented default model (changeable).

        The model list depends on the provider, and it comes from the core's
        configuration - never from a list of model names baked into the panel -
        so a stale name can never be offered as if it were the default.
        """
        if index >= len(self._panes):
            return
        pane = self._panes[index]
        token = self._provider_token(pane, str(pane["provider"].get() or ""))
        default = self._provider_default(pane, token)
        pane["model"].configure(values=(default,) if default else ())
        if default and pane["model"].get().strip() != default:
            pane["model"].delete(0, "end")
            pane["model"].insert(0, default)

    def _toggle_key_reveal(self, index: int) -> None:
        """Show or hide the key *this pane's entry currently holds*.

        Only the entry is toggled and nothing else: no stored key is read, shown
        or copied - an empty entry simply stays empty either way.
        """
        if index >= len(self._panes):
            return
        pane = self._panes[index]
        pane["key"].configure(show="" if pane["reveal"].get() else "*")

    def _on_test_connection(self, index: int) -> None:
        """Submit the pane's ``Test Connection`` action (one per pane)."""
        if index >= len(CONNECTION_INTENT_KEYS):
            return
        self._on_action(CONNECTION_INTENT_KEYS[index])

    def provider_settings_values(self) -> dict[str, dict[str, Any]]:
        """What the configuration headers currently show - plain data.

        One entry per advisor pane with the provider token, the model and the
        typed key - or ``None`` for ``api_key`` when the (masked, always empty
        until typed) entry is blank, which is how "keep the stored key" is
        expressed. No stored key is ever read back into a widget, so this method
        cannot return one.
        """
        values: dict[str, dict[str, Any]] = {}
        for index, pane in enumerate(self._panes):
            if index >= len(CONNECTION_INTENT_KEYS):
                break
            typed = pane["key"].get()
            values[f"advisor_{index + 1}"] = {
                "provider": self._provider_token(
                    pane, str(pane["provider"].get() or "")
                ),
                "model": str(pane["model"].get() or ""),
                "api_key": typed if typed else None,
            }
        return values

    def clear_key_entries(self) -> None:
        """Empty every key entry after a successful save (mask the fields again).

        A saved key lives in the settings file and nowhere else; leaving it typed
        in a widget would be the one place it could linger on screen.
        """
        for pane in self._panes:
            pane["key"].delete(0, "end")
            pane["reveal"].set(False)
            pane["key"].configure(show="*")

    def _fill_pane_settings(self, settings: Mapping[str, Any]) -> None:
        """Fill every pane's settings header from the key-free view model.

        Two rules apply here and nowhere more strictly: a widget the operator is
        *using* is never overwritten, and a key is never written into a widget -
        an empty entry plus a "key stored" line is the whole story.
        """
        options = [
            dict(option)
            for option in (settings.get("catalog") or ())
            if isinstance(option, Mapping)
        ]
        labels = tuple(
            str(option.get("label") or option.get("provider"))
            for option in options
        )
        rows = list(settings.get("rows") or ())
        for index, pane in enumerate(self._panes):
            pane["options"] = options
            pane["provider"].configure(values=labels)
            row = rows[index] if index < len(rows) else {}
            row = row if isinstance(row, Mapping) else {}
            token = str(row.get("provider") or "")
            label = str(row.get("provider_label") or token)
            stored_model = str(row.get("model") or "")
            default = self._provider_default(pane, token)
            pane["model"].configure(values=(default,) if default else ())
            self._refresh_field(pane, "provider", label)
            self._refresh_field(pane, "model", stored_model)
            self._var(f"pane_settings_{index}").set(self._settings_line(row, settings))
        status = str(settings.get("status_text") or "")
        note = str(settings.get("note") or "")
        self._var("review_provider_status").set(
            " | ".join(part for part in (status, note) if part)
        )

    def _refresh_field(
        self, pane: dict[str, Any], field: str, value: str
    ) -> None:
        """Refresh a header field only while the operator has not changed it.

        The panel re-renders on a timer, so a render must never fight the
        operator's typing. It does not ask Tk who has focus - ``focus_get()`` is
        ``None`` whenever the *window* is not the active one - it uses a
        deterministic rule instead: a field whose content is still empty, or
        still exactly what this view last rendered, has not been touched and may
        be updated; anything else belongs to the operator until it is saved.
        """
        widget = pane[field]
        current = str(widget.get() or "")
        rendered = str(pane.get(f"{field}_rendered", ""))
        if current in ("", rendered):
            if field == "provider":
                widget.set(value)
            else:
                widget.delete(0, "end")
                widget.insert(0, value)
        pane[f"{field}_rendered"] = value

    @staticmethod
    def _settings_line(row: Mapping[str, Any], settings: Mapping[str, Any]) -> str:
        """One compact line: the stored key and the last connection verdict."""
        parts = ["key: stored" if row.get("key_set") else "key: not set"]
        connection = str(row.get("connection") or "")
        if connection:
            parts.append(f"connection: {connection}")
        if not settings.get("valid", True):
            parts.append("provider settings invalid")
        return " | ".join(parts)

    def _table(
        self,
        parent: Any,
        title: str,
        columns: tuple[tuple[str, str, int], ...],
    ) -> Any:
        """One read-only table tab."""
        frame = ttk.Frame(parent, padding=6)
        keys = [key for key, _heading, _width in columns]
        table = ttk.Treeview(frame, columns=keys, show="headings", height=10)
        for key, heading, width in columns:
            table.heading(key, text=heading)
            table.column(key, width=width, anchor="w")
        table.pack(fill="both", expand=True)
        parent.add(frame, text=title)
        return table

    def _build_status(self) -> None:
        bar = ttk.Frame(self._root, padding=(8, 4))
        bar.grid(row=1, column=0, sticky="ew")
        bar.columnconfigure(0, weight=1)
        ttk.Label(bar, textvariable=self._var("status"), anchor="w").grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Label(
            bar, textvariable=self._var("status_extra"), anchor="e"
        ).grid(row=0, column=1, sticky="e")

    # -- the remembered layout ---------------------------------------------

    def _register_splits(self) -> None:
        """Name every splitter once, after every tab has been built.

        The names (see ``SPLIT_KEYS``) are the vocabulary of the remembered
        layout: the host stores one sash list per name and reads it back by name
        on the next start, so a saved position never depends on a widget path
        (which differs on every run) or on a build order.
        """
        self._splits = {
            "main": self._main_split,
            "review": self._review_split,
            "review_advisors": self._panes_frame,
            "review_results": self._review_results_split,
            "review_bottom": self._review_bottom_split,
            "supervisor": self._supervisor_split,
            "proposal": self._proposal_split,
            "deliberation": self._deliberation_split,
        }
        for key, split in zip(
            ("review_merged", "review_judge"), self._review_section_splits
        ):
            self._splits[key] = split

    def _restore_saved_tab(self) -> None:
        """Open the tab that was open when the layout was saved, if it exists."""
        title = self._layout.get("tab")
        if isinstance(title, str) and title:
            self.select_tab(title)

    def _saved_sashes(self, key: str, split: Any) -> tuple[int, ...]:
        """The remembered sash positions for one splitter, when they still fit.

        A remembered layout may come from a run in which this splitter had a
        different number of panes (the advisor panes follow the configured
        advisors, for instance). Positions are therefore used only when their
        count matches this splitter exactly; one that no longer fits keeps the
        default start instead of a shape Tk would misread. Anything that is not
        a positive integer is refused too - Tk is never handed a value that came
        from the file.
        """
        positions = self._layout.get("sashes")
        if not isinstance(positions, Mapping):
            return ()
        values = positions.get(key)
        if isinstance(values, (str, bytes)) or not isinstance(
            values, Sequence
        ):
            return ()
        count = len(split.panes()) - 1
        if count < 1 or len(values) != count:
            return ()
        clean: list[int] = []
        for value in values:
            if isinstance(value, bool) or not isinstance(value, int):
                return ()
            if value <= 0:
                return ()
            clean.append(int(value))
        return tuple(clean)

    def layout_snapshot(self) -> dict[str, Any]:
        """The layout as it is right now: window geometry, open tab, sashes.

        This is what the host writes to the layout file - a *snapshot*, never
        state: the caller may throw it away, and nothing here reads it back.
        Only geometry Tk has actually delivered is captured, so a splitter whose
        tab was never opened contributes its remembered positions (or nothing at
        all) instead of a meaningless zero. Every read is guarded, so a snapshot
        can never raise into the close path.
        """
        sashes: dict[str, list[int]] = {}
        for key, split in self._splits.items():
            positions = self._sash_positions(split) or self._saved_sashes(
                key, split
            )
            if positions:
                sashes[key] = list(positions)
        return {
            "geometry": self._current_geometry(),
            "tab": self._current_tab(),
            "sashes": sashes,
        }

    def _current_geometry(self) -> str:
        """The window's own geometry string, or ``""`` when Tk cannot answer."""
        try:
            return str(self._root.winfo_geometry())
        except tk.TclError:
            return ""

    def _current_tab(self) -> str:
        """The title of the open tab, or ``""`` when there is none."""
        if self._notebook is None:
            return ""
        try:
            return str(self._notebook.tab(self._notebook.select(), "text"))
        except tk.TclError:
            return ""

    def _sash_positions(self, split: Any) -> tuple[int, ...]:
        """One splitter's live sash positions, or ``()`` when it has none yet.

        A splitter that has never been laid out reports no size and no positive
        sash, and saving that would restore a collapsed region, so an unmeasured
        splitter is left out of the snapshot. Whether the widget is *mapped* is
        deliberately not part of the test: a splitter inside a tab that is not
        the open one keeps the size and the sashes it was last given, which is
        exactly what the operator expects to find again.
        """
        if split is None:
            return ()
        try:
            count = len(split.panes()) - 1
            if split.winfo_width() <= 1 or split.winfo_height() <= 1:
                return ()
            positions = tuple(
                int(split.sashpos(index)) for index in range(count)
            )
        except (tk.TclError, TypeError, ValueError):
            return ()
        if count < 1 or any(position <= 0 for position in positions):
            return ()
        return positions

    def select_tab(self, title: str) -> bool:
        """Open the tab with this title; ``False`` when there is no such tab."""
        if self._notebook is None:
            return False
        try:
            for tab in self._notebook.tabs():
                if str(self._notebook.tab(tab, "text")) == title:
                    self._notebook.select(tab)
                    return True
        except tk.TclError:
            return False
        return False

    def place_window(
        self,
        width: int,
        height: int,
        x: Optional[int] = None,
        y: Optional[int] = None,
    ) -> None:
        """Give the window a remembered size and position that fit this screen.

        Called while the widgets exist but before the window is mapped, so Tk
        creates it there and the remembered sash positions are later placed at
        the size they were measured at. A position that would leave almost none
        of the panel on screen is dropped and the size alone is kept: a stale
        preference must never be able to hide the window it belongs to.
        """
        try:
            screen_width = int(self._root.winfo_screenwidth())
            screen_height = int(self._root.winfo_screenheight())
        except tk.TclError:  # pragma: no cover - Tk always knows its screen
            return
        width = min(int(width), screen_width)
        height = min(int(height), screen_height)
        if x is None or y is None:
            x = y = None
        elif not (
            -width + MIN_ON_SCREEN_EDGE
            <= x
            <= screen_width - MIN_ON_SCREEN_EDGE
            and 0 <= y <= screen_height - MIN_ON_SCREEN_EDGE
        ):
            x = y = None
        spec = f"{width}x{height}"
        if x is not None and y is not None:
            spec = f"{spec}{x:+d}{y:+d}"
        try:
            self._root.geometry(spec)
        except tk.TclError:  # pragma: no cover - Tk accepts its own format
            return

    # -- the starting sash positions ---------------------------------------

    def _place_split_start(
        self,
        key: str,
        start: Callable[[int], tuple[int, ...]],
        minimum: int,
    ) -> None:
        """Give one named splitter its starting sash positions, once laid out.

        Tk's ``ttk::panedwindow`` computes its first layout from the panes'
        *requested* sizes, so a region whose content asks for more room than the
        window has would start squeezed to a few pixels and would have to be
        dragged open on every launch. The start - or the remembered positions,
        which win over it - is therefore placed on the split's first
        ``<Configure>``, after Tk's own arrange and through ``after_idle``. From
        then on the sashes belong to the operator: the only thing ever placed
        again is a *remembered* position, and only until the operator drags that
        splitter himself (see :meth:`_start_split`).
        """
        split = self._splits.get(key)
        if split is None:
            return
        split.bind(
            "<Configure>",
            lambda _event, s=split, k=key: s.after_idle(
                lambda: self._start_split(k, s, start, minimum)
            ),
            add="+",
        )
        # One drag of a sash is the operator taking that splitter over by hand.
        split.bind(
            "<ButtonRelease-1>",
            lambda _event, k=key: self._on_split_drag(k),
            add="+",
        )

    def _on_split_drag(self, key: str) -> None:
        """Note that the operator has resized this splitter by hand.

        Only a *drag* counts, and it is not undone: once the operator has moved
        a sash, that splitter belongs to Tk for the rest of the session and the
        remembered position is never placed over it again.
        """
        self._operator_splits.add(key)

    def _start_split(
        self,
        key: str,
        split: Any,
        start: Callable[[int], tuple[int, ...]],
        minimum: int,
    ) -> None:
        """Place one split's start once, then keep it honest on every resize.

        Every call places the *remembered* positions when there are any, and the
        default start otherwise, which matters because Tk lays a region out more
        than once before it settles: a tab that has not been shown before is laid
        out at a smaller size first, and a window resize re-arranges the panes
        proportionally. A position placed only once would come back clamped or
        drifted - and a splitter whose content shrinks (a compact section instead
        of a full table) would otherwise keep a collapsed arrangement for the rest
        of the session.

        The operator's own positions always win: a single drag of a sash takes
        that splitter over for the session, and from then on Tk owns it exactly as
        in a session that started without a saved layout.
        """
        if key in self._operator_splits:
            return  # the operator has taken this splitter over by hand
        total = split.winfo_height()
        if str(split.cget("orient")) == "horizontal":
            total = split.winfo_width()
        if total <= 1:
            return  # not laid out yet: the next <Configure> tries again
        saved = self._saved_sashes(key, split)
        self._placed_splits.add(str(split))
        self._place_sashes(split, saved or start(total), minimum)

    def _place_sashes(
        self, split: Any, positions: Any, minimum: int
    ) -> None:
        """Apply sash positions and then enforce the pane floors.

        Tk decides what a sash position may be, so a value it refuses ends the
        placement instead of escaping as an error: an impossible layout costs
        the operator the rest of that splitter's positions, never the panel.
        """
        for index, position in enumerate(positions):
            if index >= len(split.panes()) - 1:
                break
            try:
                split.sashpos(index, int(position))
            except (tk.TclError, TypeError, ValueError):
                break
        _clamp_split(split, minimum)

    def _advisor_sashes(self, total: int) -> tuple[int, ...]:
        """Whatever the number of advisors, they start equally wide."""
        count = max(1, len(self._panes))
        return tuple(int(total * fraction) for fraction in _even_sashes(count))

    def _main_split_start(self, total: int) -> tuple[int, ...]:
        """The work area keeps its natural height, capped so the tabs stay larger.

        Without the cap a small window would be mostly buttons; with it the
        notebook always owns most of the height, at any window size.
        """
        return (
            min(
                self._top_area.winfo_reqheight(),
                int(total * MAIN_SPLIT_TOP_START_SHARE),
            ),
        )

    def _place_advisor_start(self) -> None:
        """(Re)start the advisor splitter evenly after its panes were rebuilt."""
        self._placed_splits.discard(str(self._panes_frame))
        self._start_split(
            "review_advisors",
            self._panes_frame,
            self._advisor_sashes,
            ADVISOR_SPLIT_MIN_SIZE,
        )

    # -- rendering ---------------------------------------------------------

    def render(self, view: Mapping[str, Any]) -> None:
        """Apply one view model - the only way this window changes."""
        top = view.get("top") or {}
        for key in ("project", "mode", "project_state", "architecture", "health"):
            self._var(key).set(str(top.get(key, "-")))

        banner = view.get("banner") or {}
        critical = bool(banner.get("critical"))
        self._var("banner").set(str(banner.get("text", "")))
        self._banner.configure(foreground="#b00020" if critical else "#205020")

        work = view.get("work") or {}
        for key in (
            "plan",
            "step_no",
            "title",
            "phase",
            "state",
            "attempt",
            "next_step",
            "blocking",
        ):
            self._var(f"work_{key}").set(str(work.get(key, "-")))
        self._var("work_stopped").set(str(work.get("stopped_because", "-")))
        self._var("work_channel").set(str(work.get("channel", "-")))
        self._var("work_note").set(
            "No steps loaded. Seed a plan through the storage port; the GUI "
            "never generates an architecture plan."
            if work.get("no_steps")
            else ""
        )

        for key, spec in (view.get("buttons") or {}).items():
            button = self.buttons.get(key)
            if button is None:
                continue
            button.state(["!disabled"] if spec.get("enabled") else ["disabled"])

        self._fill(
            self._monitor,
            tuple(view.get("monitor", {}).get("rows") or ()),
            ("field", "value"),
        )
        # The audit trail, the risk register and the runtime log are popups now:
        # they are read from the same payload in the application, so nothing is
        # rendered from them here and nothing about them is lost.

        review = view.get("review") or {}
        self._var("review_status").set(str(review.get("status", "")))
        progress = str(review.get("progress", ""))
        self._var("review_progress").set(
            f"running: {progress}" if progress else ""
        )
        self._fill(
            self._review_header,
            tuple(review.get("header") or ()),
            tuple(key for key, _heading, _width in FACT_COLUMNS),
        )

        # the three advisors, side by side, then the shared results below them
        panels = tuple(review.get("provider_panels") or ())
        self._sync_panes(panels)
        self._fill_pane_settings(review.get("provider_settings") or {})
        self._refresh_pane_summaries(panels)
        self._fill(
            self._merged,
            tuple(review.get("merged_rows") or ()),
            tuple(key for key, _heading, _width in FACT_COLUMNS),
        )
        # The conflicts, the judge and the cost are summaries here: one line each,
        # with their detail one button away, so a tab with nothing to report shows
        # nothing but the honest count.
        self._var("review_conflicts_summary").set(
            str(review.get("conflicts_summary") or "Conflicts: 0")
        )
        self._var("review_judge_summary").set(
            str(review.get("judge_summary") or "Judge not used")
        )
        self._show_if(
            self._conflicts_button, bool(review.get("conflicts_available"))
        )
        self._show_if(self._judge_button, bool(review.get("judge_used")))
        self._set_text(
            self._decision, tuple(review.get("decision_lines") or ())
        )
        self._var("review_total_cost").set(
            f"Total cost: {review.get('total_cost', '-')}"
        )

        question = str(review.get("question", ""))
        # Never fight the operator: only prefill when the field is not focused.
        if (
            question
            and self._question is not None
            and self._question.get() != question
        ):
            if self._root.focus_get() is not self._question:
                self._question.delete(0, "end")
                self._question.insert(0, question)

        supervisor = view.get("supervisor") or {}
        self._render_deliberation(view.get("deliberation") or {})
        self._var("supervisor_status").set(str(supervisor.get("status", "")))
        self._fill(
            self._supervisor_header,
            tuple(supervisor.get("header") or ()),
            tuple(key for key, _heading, _width in FACT_COLUMNS),
        )
        for key, table in self._supervisor_panes.items():
            pane = supervisor.get(key) or {}
            frame = self._supervisor_frames.get(key)
            title = str(pane.get("title") or "")
            if frame is not None and title and frame.cget("text") != title:
                # the pane title is the controller's name for that pane
                frame.configure(text=title)
            table.delete(*table.get_children())
            for row in tuple(pane.get("rows") or ()):
                table.insert("", "end", values=tuple(row)[:2])
        instruction = str(supervisor.get("instruction", "") or "")
        if self._instruction_field is not None:
            if self._root.focus_get() is not self._instruction_field:
                self._instruction_field.delete(0, "end")
                self._instruction_field.insert(0, instruction)

        proposal = view.get("proposal") or {}
        self._var("proposal_status").set(str(proposal.get("status", "")))
        self._var("proposal_note").set(
            ""
            if proposal.get("review_available")
            else (
                "No architecture review is available in this session: run the "
                "review first. A proposal is built from one review and is never "
                "invented."
            )
        )
        self._fill(
            self._proposal_header,
            tuple(proposal.get("header") or ()),
            tuple(key for key, _heading, _width in FACT_COLUMNS),
        )
        sections = proposal.get("sections") or {}
        self._fill(
            self._proposal_facts,
            tuple(sections.get("facts") or ()),
            tuple(key for key, _heading, _width in FACT_COLUMNS),
        )
        self._fill(
            self._proposal_digest,
            tuple(proposal.get("digest_rows") or ()),
            tuple(key for key, _heading, _width in FACT_COLUMNS),
        )
        for name, proposal_columns in (
            ("modules", PROPOSAL_MODULE_COLUMNS),
            ("data_flows", PROPOSAL_FLOW_COLUMNS),
            ("external_dependencies", PROPOSAL_DEPENDENCY_COLUMNS),
            ("risks", PROPOSAL_RISK_COLUMNS),
            ("adr_candidates", PROPOSAL_ADR_COLUMNS),
            ("implementation_phases", PROPOSAL_PHASE_COLUMNS),
        ):
            table = self._proposal_sections.get(name)
            if table is None:
                continue
            self._fill(
                table,
                tuple(sections.get(name) or ()),
                tuple(key for key, _heading, _width in proposal_columns),
            )
        self._fill(
            self._proposal_history,
            tuple(proposal.get("history_rows") or ()),
            tuple(key for key, _heading, _width in PROPOSAL_HISTORY_COLUMNS),
        )
        requirement = str(proposal.get("requirement", ""))
        if (
            requirement
            and self._proposal_requirement_field is not None
            and self._proposal_requirement_field.get() != requirement
        ):
            if self._root.focus_get() is not self._proposal_requirement_field:
                self._proposal_requirement_field.delete(0, "end")
                self._proposal_requirement_field.insert(0, requirement)
        feedback = str(proposal.get("feedback", ""))
        if (
            feedback
            and self._revision_feedback_field is not None
            and self._revision_feedback_field.get("1.0", "end-1c") != feedback
        ):
            if self._root.focus_get() is not self._revision_feedback_field:
                self._revision_feedback_field.delete("1.0", "end")
                self._revision_feedback_field.insert("1.0", feedback)

        self._var("status").set(str(view.get("status", "")))
        self._var("status_extra").set(
            f"last action: {view.get('last_action') or '-'} | "
            f"stopped: {view.get('stopped_because') or '-'}"
        )

        actor = str(view.get("actor", ""))
        # Never fight the operator: only prefill the field when it is not focused.
        if actor and self._actor.get() != actor:
            if self._root.focus_get() is not self._actor:
                self._actor.delete(0, "end")
                self._actor.insert(0, actor)

    def _fill(self, table: Any, rows: Any, keys: tuple[str, ...]) -> None:
        """Replace a table's contents with these rows."""
        table.delete(*table.get_children())
        for row in rows:
            values = tuple(row)[: len(keys)] if row else ()
            table.insert("", "end", values=values)

    def _set_text(self, widget: Any, lines: tuple[str, ...]) -> None:
        """Rewrite one read-only text pane."""
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        if lines:
            widget.insert("1.0", "\n".join(lines))
            widget.see("1.0")
        widget.configure(state="disabled")

    def _show_if(self, widget: Any, visible: bool) -> None:
        """Show or hide a widget without ever forgetting where it belongs.

        ``grid_remove`` keeps the place the widget was given, so a button that
        comes back - a judge consulted on a later review - returns exactly where
        the operator last saw it. A hidden button is the honest empty state: an
        absent judge and an empty conflict list take no room at all.
        """
        if widget is None:
            return
        if visible:
            widget.grid()
        else:
            widget.grid_remove()
