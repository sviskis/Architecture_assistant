"""Tk widgets for the operator panel: layout and rendering only.

Every string this module shows comes from the controller's view model, and every
button calls back into the application with an intent key. No widget here ever
touches the assistant, a repository or a connection, and none of them decides
anything: a disabled button is advice, and the core still validates the action.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import scrolledtext, ttk
from typing import Any, Callable, Mapping

from .controller import INTENTS

__all__ = [
    "AUDIT_COLUMNS",
    "CONFLICT_COLUMNS",
    "COST_COLUMNS",
    "FACT_COLUMNS",
    "GROUPS",
    "LOG_COLUMNS",
    "RISK_COLUMNS",
    "SUPERVISION_HISTORY_COLUMNS",
    "MainWindow",
]

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

#: The Supervisor tab's one history table: one row per supervision identity of
#: the current attempt, newest first. Everything else in the tab is a text pane,
#: because a directive is prose and a fact sheet is prose.
SUPERVISION_HISTORY_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("supervision", "Supervision", 150),
    ("status", "Status", 130),
    ("action", "Action", 90),
    ("risk", "Risk", 70),
    ("report", "Report hash", 150),
    ("updated", "Updated", 170),
)

#: Read-only audit table columns: (key, heading, width).
AUDIT_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("created_at", "When", 150),
    ("entity", "Entity", 130),
    ("action", "Action", 90),
    ("event", "Event", 150),
    ("actor", "Actor", 100),
    ("step", "Step", 50),
    ("detail", "Detail", 420),
)

#: Open-risk table columns.
RISK_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("id", "ID", 90),
    ("severity", "Severity", 90),
    ("probability", "Probability", 90),
    ("status", "Status", 80),
    ("description", "Description", 620),
)

#: Conflict table columns of the Architecture Review tab.
CONFLICT_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("target", "Anchor", 160),
    ("supporting", "Supporting findings", 380),
    ("contradicting", "Contradicting findings", 380),
)

#: Log table columns of the Logs tab.
LOG_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("seq", "#", 50),
    ("time", "Time", 150),
    ("level", "Level", 60),
    ("component", "Component", 100),
    ("action", "Action", 130),
    ("message", "Message", 520),
    ("step", "Step", 50),
    ("review", "Review id", 240),
)

#: Cost table columns of the review tab's shared results.
COST_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("item", "Item", 120),
    ("cost", "Cost", 300),
    ("tokens", "Tokens", 120),
)

#: The label/value columns every facts table uses (one advisor pane, the merged
#: evidence, the review header).
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
        on_clear_logs: Callable[[], None],
        on_proposal_requirement: Callable[[str], None] = lambda _text: None,
        on_revision_feedback: Callable[[str], None] = lambda _text: None,
        on_instruction: Callable[[str], None] = lambda _text: None,
    ) -> None:
        self._root = root
        self._on_action = on_action
        self._on_actor = on_actor
        self._on_question = on_question
        self._on_clear_logs = on_clear_logs
        self._on_proposal_requirement = on_proposal_requirement
        self._on_revision_feedback = on_revision_feedback
        self._on_instruction = on_instruction
        self.buttons: dict[str, ttk.Button] = {}
        self._text: dict[str, tk.StringVar] = {}
        self._monitor: Any = None
        self._audit: Any = None
        self._risks: Any = None
        self._log: Any = None
        self._logs: Any = None
        self._banner: Any = None
        self._actor: Any = None
        self._question: Any = None
        self._review_header: Any = None
        self._panes_frame: Any = None
        #: One entry per advisor pane, built and rebuilt only when the number of
        #: advisors changes (in practice: once, at startup).
        self._panes: list[dict[str, Any]] = []
        self._merged: Any = None
        self._review_conflicts: Any = None
        self._judge: Any = None
        self._decision: Any = None
        self._cost: Any = None
        #: The Architecture Proposal tab: the managed project's design. All
        #: read-only tables plus the two operator inputs the tab collects.
        self._proposal_requirement_field: Any = None
        self._revision_feedback_field: Any = None
        self._proposal_header: Any = None
        self._proposal_facts: Any = None
        self._proposal_digest: Any = None
        self._proposal_history: Any = None
        self._proposal_sections: dict[str, Any] = {}
        #: The Supervisor tab: one header table, three read-only panes, one
        #: editable instruction and one history table. All plain data.
        self._supervisor_header: Any = None
        self._supervisor_panes: dict[str, Any] = {}
        self._supervisor_history: Any = None
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
        root.rowconfigure(3, weight=1)
        root.rowconfigure(4, weight=2)

        self._build_top()
        self._build_operator()
        self._build_middle()
        self._build_tabs()
        self._build_status()

    def _build_top(self) -> None:
        frame = ttk.Frame(self._root, padding=(8, 6, 8, 2))
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

        self._banner = ttk.Label(
            self._root,
            textvariable=self._var("banner"),
            padding=(10, 3),
            anchor="w",
        )
        self._banner.grid(row=1, column=0, sticky="ew")

    def _build_operator(self) -> None:
        frame = ttk.Frame(self._root, padding=(8, 2, 8, 4))
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
        frame = ttk.Frame(self._root, padding=(8, 4))
        frame.grid(row=3, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)

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
        notebook = ttk.Notebook(self._root)
        notebook.grid(row=4, column=0, sticky="nsew", padx=8, pady=(2, 4))

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
        self._build_proposal_tab(notebook)
        self._build_supervisor_tab(notebook)
        self._build_logs_tab(notebook)

        self._audit = self._table(notebook, "Audit", AUDIT_COLUMNS)
        self._risks = self._table(notebook, "Risks", RISK_COLUMNS)

        log_frame = ttk.Frame(notebook, padding=6)
        self._log = tk.Text(log_frame, height=10, wrap="word", state="disabled")
        self._log.pack(fill="both", expand=True)
        notebook.add(log_frame, text="Reports / Log")

    def _build_supervisor_tab(self, notebook: Any) -> None:
        """The Supervisor tab: the advisory analysis of the current report.

        Three panes and one header. The left pane is what the **Assistant** owns
        (its task, state, baseline and deterministic context), the center pane is
        the worker and the exact report it submitted, and the right pane is the
        **advisory** supervisor: what it proposes, why, and what the fail-closed
        gate answered. A proposal here is never authority - the authoritative
        workflow is on the Monitor/Audit tabs and is owned by the core.
        """
        frame = ttk.Frame(notebook, padding=6)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=2)
        frame.rowconfigure(3, weight=2)
        frame.rowconfigure(4, weight=2)

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
            height=5,
            row=1,
        )

        panes = ttk.Frame(frame)
        panes.grid(row=2, column=0, sticky="nsew", pady=(4, 0))
        for column in range(3):
            panes.columnconfigure(column, weight=1)
            panes.rowconfigure(0, weight=1)
        for column, key in enumerate(
            ("assistant_pane", "cline_pane", "supervisor_pane")
        ):
            box = ttk.LabelFrame(panes, text=key.replace("_pane", "").title())
            box.grid(
                row=0,
                column=column,
                sticky="nsew",
                padx=(0, 4) if column < 2 else 0,
            )
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

        self._supervisor_history = self._table_in(
            frame,
            tuple(key for key, _heading, _width in SUPERVISION_HISTORY_COLUMNS),
            columns=SUPERVISION_HISTORY_COLUMNS,
            height=5,
            row=4,
        )
        notebook.add(frame, text="Supervisor")

    def _build_logs_tab(self, notebook: Any) -> None:
        """The Logs tab: the read-only, newest-first view of GUI/core activity."""
        frame = ttk.Frame(notebook, padding=6)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        header = ttk.Frame(frame)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(
            header,
            textvariable=self._var("logs_note"),
            wraplength=700,
            justify="left",
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            header, text="Clear View", command=self._clear_logs
        ).grid(row=0, column=1, sticky="e", padx=(8, 0))

        self._logs = self._table_in(
            frame,
            tuple(key for key, _heading, _width in LOG_COLUMNS),
            columns=LOG_COLUMNS,
            height=16,
            row=1,
        )
        self._logs.tag_configure("WARN", foreground=_LEVEL_COLOURS["WARN"])
        self._logs.tag_configure("ERROR", foreground=_LEVEL_COLOURS["ERROR"])
        notebook.add(frame, text="Logs")

    def _clear_logs(self) -> None:
        """Ask the application to empty the log view (nothing else is touched)."""
        self._on_clear_logs()

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
        """
        frame = ttk.Frame(notebook, padding=6)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=3)
        frame.rowconfigure(3, weight=3)

        question_row = ttk.Frame(frame)
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

        header_frame = ttk.LabelFrame(frame, text="Review header", padding=(6, 4))
        header_frame.grid(row=1, column=0, sticky="ew", pady=(6, 4))
        header_frame.columnconfigure(0, weight=1)
        self._review_header = self._table_in(
            header_frame,
            tuple(key for key, _h, _w in FACT_COLUMNS),
            columns=FACT_COLUMNS,
            height=9,
        )

        # -- the three advisors, side by side ------------------------------
        panes_frame = ttk.LabelFrame(
            frame,
            text="Advisor responses (read-only, side by side)",
            padding=(6, 4),
        )
        panes_frame.grid(row=2, column=0, sticky="nsew", pady=(0, 4))
        panes_frame.columnconfigure(0, weight=1)
        panes_frame.rowconfigure(0, weight=1)
        self._panes_frame = ttk.Frame(panes_frame)
        self._panes_frame.grid(row=0, column=0, sticky="nsew")

        # -- the shared results, below them --------------------------------
        shared = ttk.Frame(frame)
        shared.grid(row=3, column=0, sticky="nsew")
        shared.columnconfigure(0, weight=1)
        shared.columnconfigure(1, weight=1)
        shared.rowconfigure(0, weight=1)
        shared.rowconfigure(1, weight=1)

        merged_frame = ttk.LabelFrame(
            shared, text="Merged evidence", padding=(6, 4)
        )
        merged_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 4), pady=(0, 4))
        merged_frame.columnconfigure(0, weight=1)
        merged_frame.rowconfigure(0, weight=1)
        self._merged = self._table_in(
            merged_frame,
            tuple(key for key, _h, _w in FACT_COLUMNS),
            columns=FACT_COLUMNS,
            height=8,
        )

        judge_frame = ttk.LabelFrame(
            shared, text="Judge result (a separate layer)", padding=(6, 4)
        )
        judge_frame.grid(row=0, column=1, sticky="nsew", pady=(0, 4))
        judge_frame.columnconfigure(0, weight=1)
        judge_frame.rowconfigure(0, weight=1)
        self._judge = scrolledtext.ScrolledText(
            judge_frame, height=8, wrap="word"
        )
        self._judge.grid(row=0, column=0, sticky="nsew")
        self._judge.configure(state="disabled")

        conflicts_frame = ttk.LabelFrame(
            shared, text="Evidence conflicts", padding=(6, 4)
        )
        conflicts_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 4))
        conflicts_frame.columnconfigure(0, weight=1)
        self._review_conflicts = self._table_in(
            conflicts_frame,
            tuple(key for key, _h, _w in CONFLICT_COLUMNS),
            columns=CONFLICT_COLUMNS,
            height=4,
        )

        decision_frame = ttk.LabelFrame(
            shared, text="Advisory decision (explains, never overrides)",
            padding=(6, 4),
        )
        decision_frame.grid(row=1, column=1, sticky="nsew")
        decision_frame.columnconfigure(0, weight=1)
        decision_frame.rowconfigure(0, weight=1)
        self._decision = scrolledtext.ScrolledText(
            decision_frame, height=7, wrap="word"
        )
        self._decision.grid(row=0, column=0, sticky="nsew")
        self._decision.configure(state="disabled")

        cost_frame = ttk.LabelFrame(
            shared, text="Cost of this review", padding=(6, 4)
        )
        cost_frame.grid(row=2, column=0, columnspan=2, sticky="ew")
        cost_frame.columnconfigure(0, weight=1)
        ttk.Label(
            cost_frame,
            textvariable=self._var("review_total_cost"),
            font=("", 9, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 4))
        self._cost = self._table_in(
            cost_frame,
            tuple(key for key, _h, _w in COST_COLUMNS),
            columns=COST_COLUMNS,
            height=5,
            row=1,
        )

        ttk.Label(
            frame, textvariable=self._var("review_progress"), foreground="#a05000"
        ).grid(row=4, column=0, sticky="w", pady=(4, 0))
        ttk.Label(
            frame, textvariable=self._var("review_status"), wraplength=900,
            justify="left",
        ).grid(row=5, column=0, sticky="ew")
        notebook.add(frame, text="Architecture Review")

    def question_value(self) -> str:
        """The question exactly as the operator typed it (never rewritten)."""
        return "" if self._question is None else self._question.get()

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
        frame.rowconfigure(3, weight=2)
        frame.rowconfigure(4, weight=3)

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

        header_frame = ttk.LabelFrame(
            frame, text="Proposal header", padding=(6, 4)
        )
        header_frame.grid(row=1, column=0, sticky="ew", pady=(6, 4))
        header_frame.columnconfigure(0, weight=1)
        self._proposal_header = self._table_in(
            header_frame,
            tuple(key for key, _h, _w in FACT_COLUMNS),
            columns=FACT_COLUMNS,
            height=13,
        )

        ttk.Label(
            frame,
            textvariable=self._var("proposal_status"),
            wraplength=900,
            justify="left",
        ).grid(row=2, column=0, sticky="ew", pady=(0, 4))

        top = ttk.Frame(frame)
        top.grid(row=3, column=0, sticky="nsew")
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
            frame, text="Proposed architecture (read-only)", padding=(6, 4)
        )
        sections_frame.grid(row=4, column=0, sticky="nsew", pady=(0, 4))
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
        feedback_frame.grid(row=5, column=0, sticky="ew", pady=(0, 4))
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
        ).grid(row=6, column=0, sticky="ew", pady=(0, 4))

        history_frame = ttk.LabelFrame(
            frame,
            text="Stored proposals (newest first - history is never rewritten)",
            padding=(6, 4),
        )
        history_frame.grid(row=7, column=0, sticky="nsew")
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
                pane["frame"].destroy()
            self._panes = []
            for index in range(len(panels)):
                self._panes.append(self._build_pane(index))
            for index in range(len(panels)):
                self._panes_frame.columnconfigure(index, weight=1)
                self._panes_frame.rowconfigure(0, weight=1)
        for pane, panel in zip(self._panes, panels):
            self._fill_pane(pane, panel)

    def _build_pane(self, index: int) -> dict[str, Any]:
        """One read-only advisor pane: a status line, its facts and its text."""
        pane = ttk.LabelFrame(self._panes_frame, text="Advisor", padding=(6, 4))
        pane.grid(
            row=0,
            column=index,
            sticky="nsew",
            padx=(0 if index == 0 else 4, 0),
        )
        pane.columnconfigure(0, weight=1)
        pane.rowconfigure(1, weight=2)
        pane.rowconfigure(2, weight=3)
        status_key = f"pane_status_{index}"
        status = ttk.Label(
            pane,
            textvariable=self._var(status_key),
            font=("", 9, "bold"),
        )
        status.grid(row=0, column=0, sticky="w")
        facts = self._table_in(
            pane,
            tuple(key for key, _h, _w in FACT_COLUMNS),
            columns=FACT_COLUMNS,
            height=10,
            row=1,
        )
        body = scrolledtext.ScrolledText(pane, height=8, wrap="word")
        body.grid(row=2, column=0, sticky="nsew", pady=(4, 0))
        body.configure(state="disabled")
        return {
            "frame": pane,
            "status": status,
            "status_key": status_key,
            "facts": facts,
            "body": body,
        }

    def _fill_pane(self, pane: dict[str, Any], panel: Mapping[str, Any]) -> None:
        """Fill one advisor pane from its model - text and colour only."""
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
            tuple(panel.get("facts") or ()),
            tuple(key for key, _h, _w in FACT_COLUMNS),
        )
        self._set_text(pane["body"], tuple(panel.get("body_lines") or ()))

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
        bar.grid(row=5, column=0, sticky="ew")
        bar.columnconfigure(0, weight=1)
        ttk.Label(bar, textvariable=self._var("status"), anchor="w").grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Label(
            bar, textvariable=self._var("status_extra"), anchor="e"
        ).grid(row=0, column=1, sticky="e")

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
        self._fill(
            self._audit,
            tuple(view.get("audit", {}).get("rows") or ()),
            tuple(key for key, _heading, _width in AUDIT_COLUMNS),
        )
        self._fill(
            self._risks,
            tuple(view.get("risks", {}).get("rows") or ()),
            tuple(key for key, _heading, _width in RISK_COLUMNS),
        )
        self._set_log(tuple(view.get("log", {}).get("lines") or ()))

        logs = view.get("logs") or {}
        self._var("logs_note").set(
            "Read-only. "
            f"{logs.get('count', 0)} of {logs.get('limit', 0)} entries kept, "
            "newest first. Clearing this view deletes nothing: the persistent "
            "audit trail is on the Audit tab."
        )
        self._fill_logs(tuple(logs.get("rows") or ()))

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
        self._fill(
            self._merged,
            tuple(review.get("merged_rows") or ()),
            tuple(key for key, _heading, _width in FACT_COLUMNS),
        )
        self._fill(
            self._review_conflicts,
            tuple(review.get("conflict_rows") or ()),
            tuple(key for key, _heading, _width in CONFLICT_COLUMNS),
        )
        self._set_text(self._judge, tuple(review.get("judge_lines") or ()))
        self._set_text(
            self._decision, tuple(review.get("decision_lines") or ())
        )
        self._var("review_total_cost").set(
            f"Total cost: {review.get('total_cost', '-')}"
        )
        self._fill(
            self._cost,
            tuple(review.get("cost_rows") or ()),
            tuple(key for key, _heading, _width in COST_COLUMNS),
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
        self._var("supervisor_status").set(str(supervisor.get("status", "")))
        self._fill(
            self._supervisor_header,
            tuple(supervisor.get("header") or ()),
            tuple(key for key, _heading, _width in FACT_COLUMNS),
        )
        for key, table in self._supervisor_panes.items():
            pane = supervisor.get(key) or {}
            table.delete(*table.get_children())
            for row in tuple(pane.get("rows") or ()):
                table.insert("", "end", values=tuple(row)[:2])
        instruction = str(supervisor.get("instruction", "") or "")
        if self._instruction_field is not None:
            if self._root.focus_get() is not self._instruction_field:
                self._instruction_field.delete(0, "end")
                self._instruction_field.insert(0, instruction)
        self._fill(
            self._supervisor_history,
            tuple(supervisor.get("history_rows") or ()),
            tuple(key for key, _heading, _width in SUPERVISION_HISTORY_COLUMNS),
        )

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

    def _set_log(self, lines: tuple[str, ...]) -> None:
        """Rewrite the read-only log text pane."""
        self._set_text(self._log, lines)

    def _fill_logs(self, rows: tuple[tuple[str, ...], ...]) -> None:
        """Rewrite the log table - newest first, warnings and errors emphasized.

        The rows arrive newest-first from the controller, so the table is written
        in that order: the newest entry sits at the top and is visible without
        scrolling.
        """
        table = self._logs
        for item in table.get_children():
            table.delete(item)
        for row in rows:
            values = tuple(str(value) for value in tuple(row))
            level = values[2] if len(values) > 2 else ""
            table.insert("", "end", values=values, tags=(level,))
