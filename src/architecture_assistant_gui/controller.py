"""The UI state machine: what the operator sees and which button may run.

This module is deliberately thin: it imports no repository, no tkinter and no core
object, so it is fully testable without a display and cannot grow into a second
source of truth. It works only on the plain payloads the core worker produced and
on the label/error shape of a finished job.

The single exception is the shared log-event contract (``build_event`` and its
level names), which the controller reuses so that a panel-level entry has exactly
the same shape, vocabulary and sanitization as an event the core emits. That
contract is pure functions over plain data - no composition, no connection, no
adapter is constructed by importing it.

The enable/disable matrix is **advisory**: it keeps the operator out of actions
that the authoritative transition table would reject, but the core validates
every action again and fails closed, so a stale UI can never force a state.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

from architecture_assistant.composition import (
    EVENT_LEVEL_ERROR,
    EVENT_LEVEL_INFO,
    EVENT_LEVEL_WARN,
    build_event,
    component_for,
    exception_reason,
)

__all__ = [
    "ABORT_STATES",
    "APPROVAL_STATES",
    "CLEAR_LOGS_INTENT",
    "CONNECTION_INTENT_KEYS",
    "CONNECTION_SLOT_BY_INTENT",
    "DISPLAY_INTENTS",
    "INTENTS",
    "LOG_LIMIT",
    "MIN_PROVIDER_PANES",
    "POPUP_ADVISOR_KEYS",
    "POPUP_AUDIT",
    "POPUP_CONFLICTS",
    "POPUP_COST",
    "POPUP_INTENTS",
    "POPUP_INTENT_GROUP",
    "POPUP_LOGS",
    "POPUP_PROJECT",
    "POPUP_REPORTS",
    "POPUP_RISKS",
    "POPUP_ARCHITECTURE",
    "POPUP_JUDGE",
    "POPUP_SUPERVISOR",
    "PROVIDER_INTENT_GROUP",
    "PROVIDER_SETTINGS_INTENTS",
    "RESOLVE_STATES",
    "SAVE_SETTINGS_INTENT",
    "SUPERVISION_DECIDABLE_STATUSES",
    "SUPERVISION_SEND_STATUSES",
    "SUPERVISOR_DECISION_INTENTS",
    "SUPERVISOR_INTENTS",
    "UNBLOCK_STATES",
    "GuiController",
    "Intent",
]

#: How many log entries the GUI keeps for its read-only Logs view.
LOG_LIMIT = 200

#: How many advisor panes the review tab shows side by side. The assistant has
#: three advisors, and the point of the layout is comparison - so this is a
#: minimum, never a cap: a fourth configured advisor would get its own pane
#: instead of being hidden.
MIN_PROVIDER_PANES = 3

#: States in which each human action is legal - mirroring the authoritative
#: transition table. A boundary test pins them against
#: ``architecture_assistant.domain.fsm.STEP_TRANSITIONS``, so a core change
#: cannot silently leave the GUI advertising a transition that no longer exists.
APPROVAL_STATES = frozenset({"WAITING_APPROVAL"})
UNBLOCK_STATES = frozenset({"BLOCKED"})
RESOLVE_STATES = frozenset({"CONFLICT"})
ABORT_STATES = frozenset(
    {"WAITING_APPROVAL", "BLOCKED", "CONFLICT", "FAILED"}
)

#: The proposal actions that decide an existing proposal (as opposed to
#: generating one). They all require a ``DRAFT`` proposal and an actor+reason.
PROPOSAL_DECISION_INTENTS = frozenset(
    {"approve_proposal", "reject_proposal", "request_proposal_revision"}
)

#: Every proposal action, in the order the panel offers them.
PROPOSAL_INTENTS = frozenset({"synthesize_proposal"}) | PROPOSAL_DECISION_INTENTS

#: Supervision statuses in which a human may still act on a directive. ``SENT``
#: is deliberately absent: a delivered directive is resolved only by a **new**
#: worker report, so there is nothing left to approve, reject, waive or escalate.
#: The sets mirror the domain's own gate policy - a boundary test pins them
#: against ``SUPERVISION_ALLOWING_STATUSES`` so a core change cannot leave the
#: panel advertising an action the core would refuse.
SUPERVISION_SEND_STATUSES = frozenset({"WAITING_HUMAN", "READY_TO_SEND"})
SUPERVISION_DECIDABLE_STATUSES = frozenset(
    {
        "ANALYSIS_PENDING",
        "WAITING_HUMAN",
        "READY_TO_SEND",
        "SEND_PENDING",
        "ERROR",
        "ESCALATED",
        "STALE",
        "MALFORMED",
    }
)

#: The supervision decisions that act on an existing record.
SUPERVISOR_DECISION_INTENTS = frozenset(
    {"reject_directive", "waive_supervision", "escalate_supervision"}
)

#: Every supervision action the panel offers.
SUPERVISOR_INTENTS = (
    frozenset({"analyze_report", "approve_and_send"})
    | SUPERVISOR_DECISION_INTENTS
)

#: One Test Connection action per advisor pane, in pane order. A pane is the only
#: thing that knows *which* advisor slot it shows, so each pane gets its own key
#: and its button submits exactly that key.
CONNECTION_INTENT_KEYS: tuple[str, ...] = (
    "test_connection_1",
    "test_connection_2",
    "test_connection_3",
)

#: Pane action -> the advisor slot that action configures, in the same order.
CONNECTION_SLOT_BY_INTENT: dict[str, str] = {
    "test_connection_1": "advisor_1",
    "test_connection_2": "advisor_2",
    "test_connection_3": "advisor_3",
}

#: The one action that persists the whole provider configuration.
SAVE_SETTINGS_INTENT = "save_settings"

#: Every action the provider-settings header offers.
PROVIDER_SETTINGS_INTENTS: frozenset[str] = frozenset(
    {SAVE_SETTINGS_INTENT, *CONNECTION_SLOT_BY_INTENT}
)

#: The group of the pane-level configuration buttons. Deliberately **not** one of
#: the panel's ``GROUPS``: these two buttons live inside the advisor panes, so
#: they must not also be duplicated into the generic Actions panel.
PROVIDER_INTENT_GROUP = "Advisor pane"

#: The popups that carry the secondary views: one window per thing the operator
#: *consults* rather than works in. Each key is a display action - it queues no
#: core work, reads only the last payload, and is addressed through the
#: controller exactly like every other button, so no widget ever reaches the core.
POPUP_COST = "cost_details"
POPUP_LOGS = "open_logs"
POPUP_AUDIT = "open_audit"
POPUP_RISKS = "open_risks"
POPUP_REPORTS = "open_reports"
POPUP_PROJECT = "project_details"
POPUP_ARCHITECTURE = "architecture_details"
POPUP_SUPERVISOR = "supervisor_technical"
POPUP_JUDGE = "review_judge_details"
POPUP_CONFLICTS = "review_conflict_details"

#: One advisor-detail popup per pane, in pane order: a pane is the only thing
#: that knows which advisor slot it shows.
POPUP_ADVISOR_KEYS: tuple[str, ...] = (
    "advisor_details_1",
    "advisor_details_2",
    "advisor_details_3",
)

#: Every popup action the panel offers.
POPUP_INTENTS: frozenset[str] = frozenset(
    {
        POPUP_COST,
        POPUP_LOGS,
        POPUP_AUDIT,
        POPUP_RISKS,
        POPUP_REPORTS,
        POPUP_PROJECT,
        POPUP_ARCHITECTURE,
        POPUP_SUPERVISOR,
        POPUP_JUDGE,
        POPUP_CONFLICTS,
        *POPUP_ADVISOR_KEYS,
    }
)

#: The one action the logs area offers: it empties the *view* only.
CLEAR_LOGS_INTENT = "clear_logs"

#: The group of the popup windows' own buttons. Deliberately not one of the
#: panel's ``GROUPS``: a popup button lives in the popup or in the compact
#: utility bar, and is never duplicated into the generic Actions panel.
POPUP_INTENT_GROUP = "Windows"

#: Everything that only *displays* the last payload. These queue no core work, so
#: they stay available while an action runs and after a CRITICAL failure: an
#: operator must always be able to read the log, the audit trail or a detail view.
DISPLAY_INTENTS: frozenset[str] = frozenset(
    {"view_snapshot", CLEAR_LOGS_INTENT, *POPUP_INTENTS}
)

#: Popup table columns, in the same ``(key, heading, width)`` shape the main
#: window uses, so one renderer draws both and the payload stays plain data.
POPUP_FACT_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("field", "Field", 220),
    ("value", "Value", 640),
)
POPUP_COST_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("item", "Item", 150),
    ("cost", "Cost", 300),
    ("tokens", "Tokens", 160),
)
POPUP_AUDIT_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("created_at", "When", 150),
    ("entity", "Entity", 150),
    ("action", "Action", 100),
    ("event", "Event", 150),
    ("actor", "Actor", 110),
    ("step", "Step", 60),
    ("reason", "Reason", 320),
    ("detail", "Detail payload", 520),
)
POPUP_LOG_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("seq", "#", 50),
    ("time", "Time", 150),
    ("level", "Level", 60),
    ("component", "Component", 110),
    ("action", "Action", 140),
    ("message", "Message", 560),
    ("step", "Step", 50),
    ("review", "Review id", 240),
)
POPUP_RISK_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("id", "Risk id", 120),
    ("severity", "Severity", 90),
    ("probability", "Probability", 90),
    ("impact", "Impact", 90),
    ("owner", "Owner", 130),
    ("status", "Status", 90),
    ("description", "Description", 420),
    ("mitigation", "Mitigation", 420),
)
POPUP_CONFLICT_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("target", "Anchor", 160),
    ("supporting", "Supporting findings", 380),
    ("contradicting", "Contradicting findings", 380),
    ("judge", "Judge", 180),
)
POPUP_HISTORY_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("supervision", "Supervision", 160),
    ("status", "Status", 130),
    ("action", "Action", 100),
    ("risk", "Risk", 80),
    ("report", "Report hash", 160),
    ("updated", "Updated", 170),
)
POPUP_REPORT_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("kind", "Artifact", 140),
    ("path", "Path", 700),
)
POPUP_PROVIDER_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("source", "Advisor", 120),
    ("status", "Status", 130),
    ("relation", "Relation", 140),
    ("anchor", "Anchor", 180),
    ("cost", "Cost", 340),
)


@dataclass(frozen=True)
class Intent:
    """One operator action the UI can offer."""

    key: str
    label: str
    group: str
    #: The persisted step state required for this action, when it is step-bound.
    states: Optional[frozenset[str]] = None
    #: Whether a current step must exist for this action.
    needs_step: bool = False
    #: Whether the core requires an actor and a reason (a human write).
    human: bool = False
    #: Whether the action may change workflow state (disabled under CRITICAL).
    mutating: bool = True
    #: The question shown when the reason is collected.
    reason_prompt: str = ""
    #: A confirmation shown before a destructive action, when any.
    confirmation: str = ""


#: Every action the GUI offers - the single table the buttons are built from.
INTENTS: tuple[Intent, ...] = (
    Intent(
        key="load_plan",
        label="Load Plan",
        group="Plan",
    ),
    Intent(
        key="import_plan",
        label="Import Plan",
        group="Plan",
        human=True,
        reason_prompt="Why is this plan being imported?",
    ),
    Intent(
        key="run_review",
        label="Run Architecture Review",
        group="Review",
        mutating=False,
        confirmation=(
            "Running the review asks the three configured AI advisors and may "
            "incur provider cost. It changes no workflow state and writes "
            "nothing. Continue?"
        ),
    ),
    # The provider-settings header lives *inside* each advisor pane, so these
    # actions are grouped under a name the Actions panel does not render: the
    # button belongs next to the fields it saves, not in a generic list.
    Intent(
        key=SAVE_SETTINGS_INTENT,
        label="Save Settings",
        group=PROVIDER_INTENT_GROUP,
        mutating=False,
    ),
    Intent(
        key=CONNECTION_INTENT_KEYS[0],
        label="Test Connection",
        group=PROVIDER_INTENT_GROUP,
        mutating=False,
    ),
    Intent(
        key=CONNECTION_INTENT_KEYS[1],
        label="Test Connection",
        group=PROVIDER_INTENT_GROUP,
        mutating=False,
    ),
    Intent(
        key=CONNECTION_INTENT_KEYS[2],
        label="Test Connection",
        group=PROVIDER_INTENT_GROUP,
        mutating=False,
    ),
    # The popup windows. Each one displays the last payload in its own resizable
    # window, so the main tabs keep the workflow they exist for. They are grouped
    # under a name the Actions panel does not render: the button belongs where it
    # is used - in the utility bar, in the section it details, or in the pane.
    Intent(
        key=POPUP_COST,
        label="Cost Details",
        group=POPUP_INTENT_GROUP,
        mutating=False,
    ),
    Intent(key=POPUP_LOGS, label="Logs", group=POPUP_INTENT_GROUP, mutating=False),
    Intent(
        key=CLEAR_LOGS_INTENT,
        label="Clear Logs",
        group=POPUP_INTENT_GROUP,
        mutating=False,
    ),
    Intent(key=POPUP_AUDIT, label="Audit", group=POPUP_INTENT_GROUP, mutating=False),
    Intent(key=POPUP_RISKS, label="Risks", group=POPUP_INTENT_GROUP, mutating=False),
    Intent(
        key=POPUP_REPORTS, label="Reports", group=POPUP_INTENT_GROUP, mutating=False
    ),
    Intent(
        key=POPUP_PROJECT,
        label="Project Details",
        group=POPUP_INTENT_GROUP,
        mutating=False,
    ),
    Intent(
        key=POPUP_ARCHITECTURE,
        label="Architecture Details",
        group=POPUP_INTENT_GROUP,
        mutating=False,
    ),
    Intent(
        key=POPUP_SUPERVISOR,
        label="Technical Details",
        group=POPUP_INTENT_GROUP,
        mutating=False,
    ),
    Intent(
        key=POPUP_JUDGE,
        label="View Judge Details",
        group=POPUP_INTENT_GROUP,
        mutating=False,
    ),
    Intent(
        key=POPUP_CONFLICTS,
        label="View Conflicts",
        group=POPUP_INTENT_GROUP,
        mutating=False,
    ),
    Intent(
        key=POPUP_ADVISOR_KEYS[0],
        label="Details...",
        group=POPUP_INTENT_GROUP,
        mutating=False,
    ),
    Intent(
        key=POPUP_ADVISOR_KEYS[1],
        label="Details...",
        group=POPUP_INTENT_GROUP,
        mutating=False,
    ),
    Intent(
        key=POPUP_ADVISOR_KEYS[2],
        label="Details...",
        group=POPUP_INTENT_GROUP,
        mutating=False,
    ),
    Intent(
        key="synthesize_proposal",
        label="Generate Architecture Proposal",
        group="Proposal",
        human=True,
        reason_prompt="Why is this managed-project proposal being generated?",
        confirmation=(
            "Generating stores a new durable proposal for the managed project "
            "and writes one audit entry. It changes no workflow state and never "
            "touches the assistant's own architecture baseline. Continue?"
        ),
    ),
    Intent(
        key="approve_proposal",
        label="Approve",
        group="Proposal",
        human=True,
        reason_prompt="Why is this managed-project design approved?",
    ),
    Intent(
        key="request_proposal_revision",
        label="Request Revision",
        group="Proposal",
        human=True,
        reason_prompt="Why is a revision requested?",
    ),
    Intent(
        key="reject_proposal",
        label="Reject",
        group="Proposal",
        human=True,
        reason_prompt="Why is this managed-project proposal rejected?",
    ),
    Intent(
        key="analyze_report",
        label="Analyze Report",
        group="Supervisor",
        human=True,
        mutating=False,
        reason_prompt="Why is this report being supervised?",
    ),
    Intent(
        key="approve_and_send",
        label="Approve & Send",
        group="Supervisor",
        human=True,
        reason_prompt="Why is this directive approved for repair?",
        confirmation=(
            "Publishing the directive writes the exchange channel artifact Cline "
            "reads next. It changes no workflow state, cannot verify anything "
            "and cannot increment the attempt. Continue?"
        ),
    ),
    Intent(
        key="reject_directive",
        label="Reject Directive",
        group="Supervisor",
        human=True,
        reason_prompt="Why is this supervisor directive rejected?",
    ),
    Intent(
        key="waive_supervision",
        label="Waive",
        group="Supervisor",
        human=True,
        reason_prompt="Why is supervision waived for this exact report?",
    ),
    Intent(
        key="escalate_supervision",
        label="Escalate",
        group="Supervisor",
        human=True,
        reason_prompt="Why is this supervision escalated to a human decision?",
    ),
    Intent(
        key="run_until_idle",
        label="Run Until Idle",
        group="Loop",
    ),
    Intent(
        key="pause_project",
        label="Pause",
        group="Project",
        human=True,
        reason_prompt="Why is the project being paused?",
    ),
    Intent(
        key="resume_project",
        label="Resume",
        group="Project",
        human=True,
        reason_prompt="Why is the project being resumed?",
    ),
    Intent(
        key="approve",
        label="Approve",
        group="Approval",
        states=APPROVAL_STATES,
        needs_step=True,
        human=True,
        reason_prompt="Why is this approval granted?",
    ),
    Intent(
        key="reject",
        label="Reject",
        group="Approval",
        states=APPROVAL_STATES,
        needs_step=True,
        human=True,
        reason_prompt="Why is this approval rejected?",
    ),
    Intent(
        key="unblock",
        label="Unblock",
        group="Step control",
        states=UNBLOCK_STATES,
        needs_step=True,
        human=True,
        reason_prompt="Which blocker was resolved?",
    ),
    Intent(
        key="resolve",
        label="Resolve",
        group="Step control",
        states=RESOLVE_STATES,
        needs_step=True,
        human=True,
        reason_prompt="How was the conflict resolved?",
    ),
    Intent(
        key="abort",
        label="Abort",
        group="Step control",
        states=ABORT_STATES,
        needs_step=True,
        human=True,
        reason_prompt="Why is this step being aborted?",
        confirmation="Aborting is terminal for this step. Continue?",
    ),
    Intent(
        key="export_markdown",
        label="Export Markdown",
        group="Reports",
    ),
    Intent(
        key="export_excel",
        label="Export Excel",
        group="Reports",
    ),
    Intent(
        key="open_reports_folder",
        label="Open reports folder",
        group="Reports",
        mutating=False,
    ),
    Intent(
        key="refresh",
        label="Refresh",
        group="Monitoring",
        mutating=False,
    ),
    Intent(
        key="view_snapshot",
        label="View Monitor Snapshot",
        group="Monitoring",
        mutating=False,
    ),
    Intent(
        key="reconnect",
        label="Reconnect",
        group="Monitoring",
        mutating=False,
    ),
)


class GuiController:
    """Holds what the UI shows and decides which action may run.

    It never holds a core object: ``submit`` hands the runner a callable that
    receives the worker inside the core thread, and results arrive as plain
    payloads. Every mutation is followed by a fresh read of the projection and
    the audit tail, so the UI always shows what the source of truth says.
    """

    def __init__(
        self,
        runner: Any,
        *,
        actor: str = "",
        reports_dir: str = "",
        review_question: str = "",
        log_limit: int = LOG_LIMIT,
    ) -> None:
        if not callable(getattr(runner, "submit", None)):
            raise ValueError(
                f"runner must expose submit(label, action); got {runner!r}"
            )
        if (
            isinstance(log_limit, bool)
            or not isinstance(log_limit, int)
            or log_limit < 1
        ):
            raise ValueError(f"log_limit must be an int >= 1; got {log_limit!r}")
        if not isinstance(review_question, str):
            raise ValueError(
                f"review_question must be a str; got {review_question!r}"
            )
        self._runner = runner
        self.actor = actor
        self._reports_dir = str(reports_dir)
        self._payload: dict[str, Any] = {}
        self._audit: list[dict[str, Any]] = []
        #: The log view: validated plain dictionaries, oldest first, capped at
        #: ``log_limit``. Panel-level entries and drained core events land in the
        #: same buffer, in the order this thread learned about them - so the view
        #: has one deterministic order and one place to clear.
        self._logs: deque[dict[str, Any]] = deque(maxlen=log_limit)
        self._status = "Ready."
        self._last_action = ""
        self._last_run: Optional[dict[str, Any]] = None
        self._last_error: Optional[Any] = None
        self._critical = False
        self._busy = False
        #: The plan file the operator picked - plain text and a path, never a
        #: core object, and never a second source of truth.
        self._plan_text = ""
        self._plan_source = ""
        self._plan_preview: Optional[dict[str, Any]] = None
        #: The operator's review question (kept verbatim) and the last review.
        self._review_question = review_question
        self._review: Optional[dict[str, Any]] = None
        self._review_progress = ""
        #: What the operator has typed into the advisor panes' configuration
        #: header, per advisor slot. Plain data only, and the API key is only ever
        #: the *typed* value: ``None`` means "keep whatever is stored", so a saved
        #: secret never has to travel back into a widget.
        self._provider_fields: dict[str, dict[str, Any]] = {}
        #: The last Test Connection verdict per pane action key, exactly as the
        #: core reported it (one of the documented statuses).
        self._connection_results: dict[str, str] = {}
        #: One line about the last provider-settings save, shown with the fields.
        self._provider_note = ""
        #: The export artifacts produced in this session, newest first - plain
        #: data for the Reports popup. Only the path the core already reported is
        #: kept: nothing here is read from a repository and none of it is state.
        self._exports: list[dict[str, Any]] = []
        #: The managed-project proposal board (plain data from the core) plus the
        #: two operator inputs the tab collects: an optional requirement addendum
        #: and the revision feedback. Never a core object.
        self._proposal_board: dict[str, Any] = {}
        self._proposal: Optional[dict[str, Any]] = None
        self._proposal_requirement = ""
        self._revision_feedback = ""
        #: The Supervisor tab: the runtime/supervision plain payload and the one
        #: operator input the tab collects - an editable instruction that is sent
        #: with the approval. Never a core object, never a second source of truth.
        self._supervisor: dict[str, Any] = {}
        self._instruction = ""

    # -- status ------------------------------------------------------------

    @property
    def status(self) -> str:
        """The message shown on the status bar."""
        return self._status

    @property
    def last_action(self) -> str:
        """The label of the last action the operator requested."""
        return self._last_action

    @property
    def stopped_because(self) -> str:
        """Why the loop last stopped (``""`` until it has been run)."""
        if self._last_run is None:
            return ""
        return str(self._last_run.get("stopped_because", ""))

    @property
    def is_critical(self) -> bool:
        """Whether a failure disabled every mutating action."""
        return self._critical

    @property
    def is_busy(self) -> bool:
        """Whether a core action is queued or running."""
        return self._busy or bool(getattr(self._runner, "is_busy", False))

    @property
    def last_error(self) -> Optional[Any]:
        """The last error report, or ``None`` after a successful read."""
        return self._last_error

    @property
    def reports_dir(self) -> str:
        """The reports directory, as configured for this session."""
        paths = self._payload.get("paths")
        if isinstance(paths, Mapping) and paths.get("report_dir"):
            return str(paths["report_dir"])
        return self._reports_dir

    def log(
        self,
        message: str,
        *,
        level: str = EVENT_LEVEL_INFO,
        component: str = "GUI",
        action: str = "note",
        step_no: Optional[int] = None,
        review_id: Optional[str] = None,
    ) -> None:
        """Append one log entry and show it on the status bar.

        The entry is built by the shared contract, so it carries the same shape,
        the same vocabulary and the same sanitization as an event the core emits -
        the panel does not get a private log format. A malformed entry is
        downgraded to a safe one instead of crashing the UI thread; the strict
        producer on the core side (``CoreWorker.record_event``) raises instead.
        """
        text = str(message)
        try:
            entry = build_event(
                level=level,
                component=component,
                action=action,
                message=text,
                step_no=step_no,
                review_id=review_id,
            )
        except ValueError:
            entry = build_event(
                level=EVENT_LEVEL_INFO,
                component="GUI",
                action="note",
                message=text or "an entry could not be rendered",
            )
        self._logs.append(entry)
        self._status = text

    # -- the enable/disable matrix ----------------------------------------

    def intent(self, key: str) -> Intent:
        """The intent with this key, or ``ValueError``."""
        for intent in INTENTS:
            if intent.key == key:
                return intent
        raise ValueError(f"unknown action {key!r}")

    def enabled(self, intent: Intent) -> bool:
        """Whether the button for ``intent`` may be pressed right now."""
        if intent.key in DISPLAY_INTENTS:
            # pure display of the last payload: no core work, no mutation, so it
            # stays available while an action runs and even under CRITICAL
            return True
        if self.is_busy:
            return False
        if intent.key in (
            "refresh",
            "reconnect",
            "open_reports_folder",
            "run_review",
            # The provider-settings header: testing and saving a local
            # configuration is a diagnostic, never a mutation of workflow state,
            # so it stays available next to the review it configures.
            *PROVIDER_SETTINGS_INTENTS,
        ):
            return True
        if self._critical:
            return False
        if intent.key == "load_plan":
            # The initial plan only: a database that already holds steps is
            # never merged, replaced or renumbered by the panel.
            return self.step_count() == 0
        if intent.key == "import_plan":
            return self.plan_importable()
        if intent.key == "synthesize_proposal":
            # The review is the evidence: without one there is nothing to
            # organize, and the panel says so instead of generating a guess.
            return self.proposal_review_available()
        if intent.key in PROPOSAL_DECISION_INTENTS:
            return self.proposal_decidable()
        if intent.key in SUPERVISOR_INTENTS:
            return self.supervisor_action_available(intent.key)
        if intent.states is not None:
            state = self.current_state()
            if state is None or state not in intent.states:
                return False
        if intent.needs_step and self.current_step() is None:
            return False
        if intent.key == "pause_project":
            return not self.is_paused()
        if intent.key == "resume_project":
            return self.is_paused()
        if intent.key == "run_until_idle":
            return not self.is_paused() and self.step_count() > 0
        return True

    def refusal(self, intent: Intent, reason: str = "") -> Optional[str]:
        """Why this action may not run now - or ``None`` when it may."""
        if intent.key == "load_plan" and self.step_count() > 0:
            return (
                "Plan already loaded: this database already holds "
                f"{self.step_count()} step(s), and v1.1.1 imports an initial "
                "plan only."
            )
        if intent.key == "import_plan":
            if not self.plan_pending:
                return "Load a plan file first."
            if not self.plan_importable():
                return (
                    "The loaded plan cannot be imported: "
                    f"{self.plan_blocked_reason()}"
                )
        if intent.key == "run_review" and not self.review_question.strip():
            return (
                "A review question is required: write what the three advisors "
                "should answer (whitespace only is not a question)."
            )
        if intent.key == "synthesize_proposal" and not self.proposal_review_available():
            return (
                "An architecture proposal needs one review as its evidence: "
                "run the architecture review first."
            )
        if intent.key in PROPOSAL_DECISION_INTENTS and not self.proposal_decidable():
            return (
                "No DRAFT architecture proposal is available: generate one "
                "first (a decided or superseded proposal can never be decided "
                "again)."
            )
        if intent.key in SUPERVISOR_INTENTS:
            problem = self.supervisor_refusal(intent.key)
            if problem is not None:
                return problem
        if (
            intent.key == "request_proposal_revision"
            and not self._revision_feedback.strip()
        ):
            return (
                "Revision feedback is required: write what the managed "
                "project's design should change."
            )
        if not self.enabled(intent):
            return f"{intent.label} is not available for the current state."
        if intent.human:
            if not str(self.actor).strip():
                return "An actor is required: enter the operator name first."
            if not str(reason).strip():
                return "A reason is required for this action."
        return None

    # -- actions -----------------------------------------------------------

    def submit(self, key: str, *, reason: str = "") -> bool:
        """Request one action; ``False`` (with a status message) when refused."""
        intent = self.intent(key)
        problem = self.refusal(intent, reason)
        if problem is not None:
            self.log(
                problem,
                level=EVENT_LEVEL_WARN,
                action=intent.key,
                step_no=self.current_step_no(),
            )
            return False
        action = self._action_for(intent, str(reason).strip())
        self._last_action = intent.label
        if not self._runner.submit(intent.key, action):
            self.log(
                "Another core action is still running.",
                level=EVENT_LEVEL_WARN,
                action=intent.key,
            )
            return False
        self._busy = True
        self.log(
            f"{intent.label} requested.",
            action=intent.key,
            step_no=self.current_step_no(),
        )
        return True

    def _action_for(
        self, intent: Intent, reason: str
    ) -> Callable[[Any], dict[str, Any]]:
        """Build the callable the core thread will run for this intent."""
        if intent.key in POPUP_INTENTS:
            raise ValueError(
                f"{intent.key} opens a popup from the last payload; it queues no "
                "core work"
            )
        if intent.key == "refresh":
            return self._read_action()
        if intent.key == "reconnect":
            return self._reconnect_action()
        if intent.key in ("export_markdown", "export_excel"):
            return self._export_action(intent.key)
        if intent.key == "load_plan":
            return self._preview_action()
        if intent.key == "import_plan":
            return self._import_action(reason)
        if intent.key == "run_review":
            return self._review_action()
        if intent.key == SAVE_SETTINGS_INTENT:
            return self._save_settings_action()
        if intent.key in CONNECTION_SLOT_BY_INTENT:
            return self._test_connection_action(
                CONNECTION_SLOT_BY_INTENT[intent.key]
            )
        if intent.key in PROPOSAL_INTENTS:
            return self._proposal_action(intent.key, reason)
        if intent.key in SUPERVISOR_INTENTS:
            return self._supervisor_action(intent.key, reason)
        return self._mutation_action(intent.key, reason)

    def _read_action(self) -> Callable[[Any], dict[str, Any]]:
        def action(worker: Any) -> dict[str, Any]:
            return {
                "payload": worker.payload(),
                "audit": worker.audit_tail(),
                "board": worker.proposal_board(),
                "supervisor": worker.supervisor_status(),
            }

        return action

    def _reconnect_action(self) -> Callable[[Any], dict[str, Any]]:
        """Re-compose in the core thread - no repair, no state change."""

        def action(worker: Any) -> dict[str, Any]:
            worker.reconnect()
            return {
                "payload": worker.payload(),
                "audit": worker.audit_tail(),
                "board": worker.proposal_board(),
            }

        return action

    def _export_action(self, key: str) -> Callable[[Any], dict[str, Any]]:
        def action(worker: Any) -> dict[str, Any]:
            result = (
                worker.export_markdown()
                if key == "export_markdown"
                else worker.export_excel()
            )
            return {
                "result": result,
                "payload": worker.payload(),
                "audit": worker.audit_tail(),
            }

        return action

    def _mutation_action(
        self, key: str, reason: str
    ) -> Callable[[Any], dict[str, Any]]:
        """One mutation in the core thread, then a fresh read of everything."""
        actor = str(self.actor).strip()

        def action(worker: Any) -> dict[str, Any]:
            if key == "run_until_idle":
                primary = worker.run_until_idle()
            elif key == "pause_project":
                primary = worker.pause_project(actor=actor, reason=reason)
            elif key == "resume_project":
                primary = worker.resume_project(actor=actor, reason=reason)
            elif key == "approve":
                primary = worker.approve(actor=actor, reason=reason)
            elif key == "reject":
                primary = worker.reject(actor=actor, reason=reason)
            elif key == "unblock":
                primary = worker.unblock(actor=actor, reason=reason)
            elif key == "resolve":
                primary = worker.resolve(actor=actor, reason=reason)
            elif key == "abort":
                primary = worker.abort(actor=actor, reason=reason)
            else:
                raise ValueError(f"unknown action {key!r}")
            return {
                "result": primary,
                "payload": worker.payload(),
                "audit": worker.audit_tail(),
            }

        return action

    def _preview_action(self) -> Callable[[Any], dict[str, Any]]:
        """Validate the loaded plan in the core thread - a read, never a write."""

        text = self._plan_text
        source = self._plan_source

        def action(worker: Any) -> dict[str, Any]:
            return {"plan": worker.preview_plan(text, source_file=source)}

        return action

    def _import_action(self, reason: str) -> Callable[[Any], dict[str, Any]]:
        """Import the loaded plan, then read everything back fresh."""

        text = self._plan_text
        source = self._plan_source
        actor = str(self.actor).strip()

        def action(worker: Any) -> dict[str, Any]:
            primary = worker.import_plan(
                text, actor=actor, reason=reason, source_file=source
            )
            return {
                "result": primary,
                "payload": worker.payload(),
                "audit": worker.audit_tail(),
            }

        return action

    # -- the review --------------------------------------------------------

    def _review_action(self) -> Callable[[Any], dict[str, Any]]:
        """Run one review in the core thread with the operator's own question.

        The question is captured **verbatim** - it is never rewritten here, and
        the review is a single, one-shot consultation in the core thread.
        """

        question = self.review_question

        def action(worker: Any) -> dict[str, Any]:
            return {
                "review": worker.run_architecture_review(question),
                "payload": worker.payload(),
            }

        return action

    # -- the per-advisor provider configuration ----------------------------

    def set_provider_selection(self, selection: Mapping[str, Any]) -> None:
        """Remember what the panes' configuration header currently shows.

        Plain data per advisor slot: ``provider``, ``model`` and ``api_key``. The
        key is the **typed** value or ``None`` for "keep the stored one" - a
        stored key is never handed back here, so nothing downstream of a widget
        can hold a saved secret.
        """
        if not isinstance(selection, Mapping):
            raise ValueError("provider selection must be a mapping")
        fields: dict[str, dict[str, Any]] = {}
        for slot in CONNECTION_SLOT_BY_INTENT.values():
            entry = selection.get(slot)
            entry = entry if isinstance(entry, Mapping) else {}
            typed_key = entry.get("api_key", None)
            if typed_key is not None and not isinstance(typed_key, str):
                typed_key = str(typed_key)
            fields[slot] = {
                "provider": entry.get("provider"),
                "model": entry.get("model"),
                "api_key": typed_key,
            }
        self._provider_fields = fields

    def _stored_selection(self, slot: str) -> dict[str, Any]:
        """The configured selection of one slot, as the core last reported it."""
        settings = _mapping(self._payload.get("provider_settings"))
        advisors = _mapping(settings.get("advisors"))
        return dict(_mapping(advisors.get(slot)))

    def _provider_entry(self, slot: str) -> dict[str, Any]:
        """One slot's configuration: what was typed, else what is stored."""
        stored = self._stored_selection(slot)
        typed = self._provider_fields.get(slot) or {}
        provider = typed.get("provider") or stored.get("provider") or "disabled"
        model = typed.get("model")
        if model is None:
            model = stored.get("model", "")
        return {
            "provider": str(provider),
            "model": "" if model is None else str(model),
            "api_key": typed.get("api_key", None),
        }

    def provider_payload(self) -> dict[str, dict[str, Any]]:
        """The three slots exactly as the save action will send them."""
        return {
            slot: self._provider_entry(slot)
            for slot in CONNECTION_SLOT_BY_INTENT.values()
        }

    def _save_settings_action(self) -> Callable[[Any], dict[str, Any]]:
        """Persist the whole provider configuration, then read everything back."""

        payload = self.provider_payload()

        def action(worker: Any) -> dict[str, Any]:
            return {
                "settings": worker.save_provider_settings(payload),
                "payload": worker.payload(),
            }

        return action

    def _test_connection_action(
        self, slot: str
    ) -> Callable[[Any], dict[str, Any]]:
        """Probe one advisor's configuration - the smallest safe provider call."""

        entry = self._provider_entry(slot)

        def action(worker: Any) -> dict[str, Any]:
            return {
                "connection": worker.test_connection(
                    slot,
                    entry["provider"],
                    entry["model"],
                    entry.get("api_key"),
                )
            }

        return action

    def provider_settings_view(self) -> dict[str, Any]:
        """The provider-settings header as plain, key-free data.

        One row per advisor pane with the configured provider, the model, whether
        a key is stored (never the key) and the last Test Connection verdict,
        plus the catalog of choices and the load status - including the explicit
        "provider settings invalid" wording when the file was damaged.
        """
        settings = _mapping(self._payload.get("provider_settings"))
        advisors = _mapping(settings.get("advisors"))
        rows: list[dict[str, Any]] = []
        for index, intent_key in enumerate(CONNECTION_INTENT_KEYS):
            slot = CONNECTION_SLOT_BY_INTENT[intent_key]
            view = _mapping(advisors.get(slot))
            rows.append(
                {
                    "slot": slot,
                    "pane": index,
                    "action": intent_key,
                    "provider": str(view.get("provider") or ""),
                    "provider_label": str(view.get("provider_label") or ""),
                    "model": str(view.get("model") or ""),
                    "key_set": bool(view.get("key_set")),
                    "key_masked": str(view.get("key_masked") or ""),
                    "connection": str(self._connection_results.get(intent_key, "")),
                }
            )
        catalog = [
            dict(option)
            for option in (settings.get("catalog") or ())
            if isinstance(option, Mapping)
        ]
        return {
            "rows": rows,
            "catalog": catalog,
            "status": str(settings.get("status") or ""),
            "status_text": str(settings.get("status_text") or ""),
            "valid": bool(settings.get("valid", True)),
            "path": str(settings.get("path") or ""),
            "note": self._provider_note,
        }

    # -- advisory supervision ----------------------------------------------

    def _supervisor_action(
        self, key: str, reason: str
    ) -> Callable[[Any], dict[str, Any]]:
        """One supervision action in the core thread.

        Every input is captured here, on the UI thread, before the callable is
        handed to the runner: the core thread must never read controller state.
        The supervision id is *not* captured - the core resolves the current
        report identity itself, so a stale panel cannot approve a directive for a
        report that no longer exists.
        """
        actor = str(self.actor).strip()
        instruction = self._instruction

        def action(worker: Any) -> dict[str, Any]:
            if key == "analyze_report":
                return worker.analyze_report()
            if key == "approve_and_send":
                return worker.approve_and_send(
                    instruction, actor=actor, reason=reason
                )
            if key == "reject_directive":
                return worker.reject_directive(actor=actor, reason=reason)
            if key == "waive_supervision":
                return worker.waive_supervision(actor=actor, reason=reason)
            return worker.escalate_supervision(actor=actor, reason=reason)

        return action

    @property
    def instruction(self) -> str:
        """The directive instruction, exactly as the operator wrote it."""
        return self._instruction

    def set_instruction(self, text: str) -> None:
        """Replace the editable instruction - stored verbatim, never rewritten."""
        if not isinstance(text, str):
            raise ValueError(f"instruction must be a str; got {text!r}")
        self._instruction = text

    def supervision(self) -> Mapping[str, Any]:
        """The supervision payload the core last produced (``{}`` before that)."""
        return _mapping(self._supervisor.get("supervision"))

    def supervision_enabled(self) -> bool:
        """Whether supervision is configured at all."""
        return bool(self._supervisor.get("enabled"))

    def supervision_status(self) -> str:
        """The current supervision status of the current report (``""``/none)."""
        record = self.supervision().get("record")
        if not isinstance(record, Mapping):
            return ""
        return str(record.get("status", "") or "")

    def supervision_waiting_for(self) -> str:
        """The deterministic ``waiting_for`` value the core published."""
        return str(self.supervision().get("waiting_for", "") or "none")

    def supervision_directive(self) -> str:
        """The instruction the record currently carries (``""`` when none)."""
        record = self.supervision().get("record")
        if not isinstance(record, Mapping):
            return ""
        return str(record.get("instruction_for_cline", "") or "")

    def supervisor_action_available(self, key: str) -> bool:
        """Whether one supervision control may run right now.

        Advisory only - the core validates every action again and fails closed -
        but it keeps the operator out of actions the core would refuse, and the
        refusal text explains exactly why.
        """
        if not self.supervision_enabled():
            return False
        if key == "analyze_report":
            return bool(self.supervision().get("current_report_hash"))
        status = self.supervision_status()
        if key == "approve_and_send":
            if status not in SUPERVISION_SEND_STATUSES:
                return False
            return bool(self._instruction.strip() or self.supervision_directive().strip())
        if key == "reject_directive":
            return (
                status in SUPERVISION_DECIDABLE_STATUSES
                and status != "REJECTED"
            )
        if key == "waive_supervision":
            return status in SUPERVISION_DECIDABLE_STATUSES and status != "WAIVED"
        if key == "escalate_supervision":
            return (
                status in SUPERVISION_DECIDABLE_STATUSES
                and status != "ESCALATED"
            )
        return False

    def supervisor_refusal(self, key: str) -> Optional[str]:
        """Why one supervision control may not run now - or ``None``."""
        intent = self.intent(key)
        if not self.supervision_enabled():
            return (
                "Supervision is disabled for this session: the authoritative "
                "workflow runs exactly as it did before Step 28. Enable it in "
                "the composition configuration to supervise reports."
            )
        if not self.supervision():
            return (
                "No current step is supervised yet: there is no report to "
                "supervise."
            )
        if self.enabled(intent):
            return None
        status = self.supervision_status()
        if key == "analyze_report":
            return (
                "There is no current worker report to analyse: the report "
                "appears once Cline has written it into the exchange channel."
            )
        if not status:
            return (
                "The current report has no supervision record yet: analyze it "
                "first."
            )
        if status == "SENT":
            return (
                "This directive was already sent: it is resolved only by a NEW "
                "worker report, so it cannot be approved, rejected, waived or "
                "escalated again."
            )
        return (
            f"{intent.label} is not available while supervision is {status}."
        )

    def supervisor_view(self) -> dict[str, Any]:
        """The Supervisor tab, as plain data only.

        Three panes, one header and one deterministic ``waiting_for`` value - and
        nothing else. Every string here comes from the supervision payload the
        core published (persisted state plus the bounded fact sheet a supervisor
        would be handed); the panel computes no status of its own, so the tab can
        never disagree with the core about what is happening.
        """
        payload = self.supervision()
        runtime = _mapping(self._supervisor.get("runtime"))
        raw_record = payload.get("record")
        record = dict(raw_record) if isinstance(raw_record, Mapping) else None
        raw_context = payload.get("context")
        context = dict(raw_context) if isinstance(raw_context, Mapping) else None
        return {
            "enabled": self.supervision_enabled(),
            "available": bool(payload),
            "status": self._supervisor_summary(payload, record),
            "waiting_for": self.supervision_waiting_for(),
            "instruction": self._instruction,
            "header": self._supervisor_header(payload, record),
            "assistant_pane": self._supervisor_assistant_pane(payload),
            "cline_pane": self._supervisor_cline_pane(payload, context),
            "supervisor_pane": self._supervisor_supervisor_pane(record),
            "history_rows": self._supervision_history_rows(payload),
        }

    def _supervisor_summary(
        self, payload: Mapping[str, Any], record: Optional[Mapping[str, Any]]
    ) -> str:
        """One honest status line for the Supervisor tab."""
        if not self.supervision_enabled():
            return (
                "Supervision is disabled for this session: the authoritative "
                "workflow runs exactly as it did before Step 28."
            )
        if not payload:
            return "No current step to supervise."
        step_no = payload.get("step_no")
        attempt = payload.get("attempt")
        if not payload.get("current_report_hash"):
            return (
                f"No worker report yet for step {step_no} attempt {attempt}: "
                "nothing to supervise."
            )
        if record is None:
            return (
                f"The report of step {step_no} attempt {attempt} is not "
                "supervised yet - Analyze Report runs the analysis."
            )
        action = record.get("action") or "-"
        return (
            f"Supervision {record.get('status')}: {action}; waiting for "
            f"{self.supervision_waiting_for()}."
        )

    def _supervisor_header(
        self,
        payload: Mapping[str, Any],
        record: Optional[Mapping[str, Any]],
    ) -> list[list[str]]:
        """The compact summary row of the tab - the identifiers live in the popup."""
        return [
            ["Project", str(payload.get("project") or self.project_name())],
            ["Step", str(payload.get("step_no", "-"))],
            ["Attempt", str(payload.get("attempt", "-"))],
            ["Worker state", str(payload.get("step_state") or "-")],
            [
                "Supervisor state",
                str("" if record is None else record.get("status") or "-") or "-",
            ],
            ["Waiting For", self.supervision_waiting_for()],
        ]

    def project_name(self) -> str:
        """The managed project's name from the last projection (``"-"``)."""
        project = _mapping(self._payload.get("project"))
        return str(project.get("name", "-"))

    def _supervisor_assistant_pane(
        self, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """LEFT pane: the compact Assistant summary.

        The full fact sheet - task text, constraints, ADRs, risks, findings and
        change requests - is one click away in **Technical Details**; the pane
        itself keeps only what the operator needs while working.
        """
        return _pane(
            "Architecture Assistant",
            [
                (
                    "Step state",
                    f"{payload.get('step_state') or '-'} | attempt "
                    f"{payload.get('attempt', '-')} of "
                    f"{payload.get('max_attempts', '-')}",
                ),
                (
                    "Attempts remaining",
                    str(payload.get("attempts_remaining", "-")),
                ),
                ("Mode", str(payload.get("mode") or "-")),
                ("Project paused", _yes(payload.get("paused"))),
                (
                    "Architecture baseline",
                    str(payload.get("architecture_version") or "-"),
                ),
            ],
        )

    def _supervisor_cline_pane(
        self,
        payload: Mapping[str, Any],
        context: Optional[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """CENTER pane: the compact worker and report summary."""
        facts = context or {}
        return _pane(
            "Cline",
            [
                ("Worker state", str(payload.get("step_state") or "-")),
                ("Report status", str(facts.get("report_status") or "-")),
                ("Report summary", str(facts.get("report_summary") or "-")),
            ],
        )

    def _supervisor_supervisor_pane(
        self,
        record: Optional[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """RIGHT pane: the compact advisory analysis summary."""
        current = record or {}
        return _pane(
            "Supervisor",
            [
                (
                    "Analysis status",
                    str(current.get("status") or self.supervision_waiting_for()),
                ),
                ("Action", str(current.get("action") or "-")),
                ("Risk", str(current.get("risk") or "-")),
                ("Requires human", _yes(current.get("requires_human"))),
                (
                    "Instruction (short)",
                    _brief(current.get("instruction_for_cline"), 60) or "-",
                ),
            ],
        )

    def _supervision_history_rows(
        self, payload: Mapping[str, Any]
    ) -> list[list[str]]:
        """One row per supervision identity of this attempt, newest first."""
        history = payload.get("history")
        if not isinstance(history, Sequence) or isinstance(history, (str, bytes)):
            return []
        rows: list[list[str]] = []
        for item in history:
            if not isinstance(item, Mapping):
                continue
            rows.append(
                [
                    _short(item.get("supervision_id")),
                    str(item.get("status") or ""),
                    str(item.get("action") or "-"),
                    str(item.get("risk") or "-"),
                    _short(item.get("source_report_hash")),
                    str(item.get("updated_at") or ""),
                ]
            )
        return rows



    # -- the managed-project proposal --------------------------------------

    def _proposal_action(
        self, key: str, reason: str
    ) -> Callable[[Any], dict[str, Any]]:
        """One proposal action in the core thread, then a fresh read of everything.

        Every input the action needs is captured here, on the UI thread, before
        the callable is handed to the runner - the core thread must never read
        controller state.
        """
        actor = str(self.actor).strip()
        requirement = self._proposal_requirement
        feedback = self._revision_feedback
        proposal_id = self._decidable_proposal_id()
        revision_of = self._revision_target()

        def action(worker: Any) -> dict[str, Any]:
            if key == "synthesize_proposal":
                primary = worker.synthesize_proposal(
                    requirement,
                    actor=actor,
                    reason=reason,
                    revision_of=revision_of,
                )
            elif key == "approve_proposal":
                primary = worker.approve_proposal(
                    proposal_id, actor=actor, reason=reason
                )
            elif key == "reject_proposal":
                primary = worker.reject_proposal(
                    proposal_id, actor=actor, reason=reason
                )
            else:
                primary = worker.request_proposal_revision(
                    proposal_id,
                    actor=actor,
                    reason=reason,
                    feedback=feedback,
                )
            return {
                "result": primary,
                "payload": worker.payload(),
                "audit": worker.audit_tail(),
                "board": worker.proposal_board(),
            }

        return action

    @property
    def proposal_requirement(self) -> str:
        """The optional requirement addendum, exactly as the operator wrote it."""
        return self._proposal_requirement

    def set_proposal_requirement(self, text: str) -> None:
        """Replace the requirement addendum - stored verbatim, never rewritten."""
        if not isinstance(text, str):
            raise ValueError(f"requirement must be a str; got {text!r}")
        self._proposal_requirement = text

    @property
    def revision_feedback(self) -> str:
        """The revision feedback, exactly as the operator wrote it."""
        return self._revision_feedback

    def set_revision_feedback(self, text: str) -> None:
        """Replace the revision feedback - stored verbatim, never rewritten."""
        if not isinstance(text, str):
            raise ValueError(f"feedback must be a str; got {text!r}")
        self._revision_feedback = text

    def proposal_review_available(self) -> bool:
        """Whether the core holds a review a proposal could be built from."""
        return bool(self._proposal_board.get("review_available"))

    def proposal_decidable(self) -> bool:
        """Whether the latest proposal may still receive a human decision."""
        latest = self._proposal_board.get("latest")
        if not isinstance(latest, Mapping):
            return False
        return str(latest.get("status", "")) == "DRAFT"

    def _decidable_proposal_id(self) -> str:
        """The id a decision would target (``""`` when there is none)."""
        latest = self._proposal_board.get("latest")
        if not isinstance(latest, Mapping):
            return ""
        return str(latest.get("proposal_id", "") or "")

    def _revision_target(self) -> Optional[str]:
        """The proposal a new generation replaces, when a revision was requested."""
        latest = self._proposal_board.get("latest")
        if not isinstance(latest, Mapping):
            return None
        if str(latest.get("status", "")) != "REVISION_REQUESTED":
            return None
        return str(latest.get("proposal_id", "") or "") or None

    def proposal_view(self) -> dict[str, Any]:
        """The Architecture Proposal tab, as plain data only."""
        board = self._proposal_board
        raw = board.get("latest")
        latest = dict(raw) if isinstance(raw, Mapping) else None
        return {
            "available": latest is not None,
            "review_available": bool(board.get("review_available")),
            "synthesizer": bool(board.get("synthesizer")),
            "count": int(board.get("count") or 0),
            "requirement": self._proposal_requirement,
            "feedback": self._revision_feedback,
            "status": self._proposal_summary(latest),
            "header": self._proposal_header(latest, board),
            "sections": self._proposal_sections(latest),
            "digest_rows": self._proposal_digest_rows(latest),
            "history_rows": self._proposal_history_rows(board),
        }

    def _proposal_summary(self, latest: Optional[Mapping[str, Any]]) -> str:
        """One status line for the proposal tab - honest about what exists."""
        if latest is None:
            hint = (
                "a review is available - Generate can build one"
                if self.proposal_review_available()
                else "run the architecture review first"
            )
            return (
                "No architecture proposal for this managed project yet "
                f"({hint})."
            )
        modules = len(latest.get("modules") or ())
        questions = len(latest.get("unresolved_questions") or ())
        return (
            f"Proposal {latest.get('proposal_id')} for "
            f"{latest.get('project')}: {latest.get('status')}, revision "
            f"{latest.get('revision_no')}, {modules} module(s), {questions} "
            "unresolved question(s)."
        )

    def _proposal_header(
        self,
        latest: Optional[Mapping[str, Any]],
        board: Mapping[str, Any],
    ) -> list[tuple[str, str]]:
        """The proposal header - identity, provenance and the human decision."""
        project = _mapping(self._payload.get("project")).get("name", "-")
        review = (
            "yes"
            if board.get("review_available")
            else "no - run the architecture review first"
        )
        synthesizer = (
            "a provider is configured"
            if board.get("synthesizer")
            else "deterministic (offline, no provider, no cost)"
        )
        if latest is None:
            return [
                ("Proposal", "none yet"),
                ("Managed project", str(project)),
                ("Review available", review),
                ("Synthesizer", synthesizer),
                ("Proposals stored", str(board.get("count") or 0)),
            ]
        revision = str(latest.get("revision_no"))
        if latest.get("revision_of"):
            revision = f"{revision} (replaces {latest.get('revision_of')})"
        return [
            ("Proposal id", str(latest.get("proposal_id", "-"))),
            ("Managed project", str(latest.get("project", "-"))),
            ("Status", str(latest.get("status", "-"))),
            ("Revision", revision),
            ("Source review id", str(latest.get("source_review_id", "-"))),
            (
                "Baseline at review (traceability)",
                str(latest.get("architecture_version") or "-"),
            ),
            ("Created at", _stamp(latest.get("created_at"))),
            ("Decided by", str(latest.get("decided_by") or "-")),
            ("Decided at", _stamp(latest.get("decided_at"))),
            ("Decision reason", str(latest.get("decision_reason") or "-")),
            ("Revision feedback", str(latest.get("revision_feedback") or "-")),
            ("Fingerprint", str(latest.get("fingerprint", ""))[:16]),
            ("Synthesizer", synthesizer),
        ]

    def _proposal_sections(
        self, latest: Optional[Mapping[str, Any]]
    ) -> dict[str, Any]:
        """Every structured proposal section, as plain rows for the tables."""
        sections: dict[str, Any] = {
            "facts": [["Proposal", "none yet"]],
            "modules": [],
            "data_flows": [],
            "external_dependencies": [],
            "risks": [],
            "adr_candidates": [],
            "implementation_phases": [],
        }
        if latest is None:
            return sections
        rules = [str(item) for item in (latest.get("architecture_rules") or ())]
        proposed = [str(item) for item in (latest.get("proposed_rules") or ())]
        questions = [
            str(item) for item in (latest.get("unresolved_questions") or ())
        ]
        sections["facts"] = [
            ["Summary", str(latest.get("summary") or "-")],
            ["Rationale", str(latest.get("rationale") or "-")],
            ["Architecture rules (project)", " | ".join(rules) or "-"],
            [
                "Proposed rules (not enforced by the assistant)",
                " | ".join(proposed) or "-",
            ],
            ["Unresolved questions", " | ".join(questions) or "-"],
        ]
        sections["modules"] = [
            [
                str(_mapping(item).get("name", "")),
                str(_mapping(item).get("responsibility", "")),
                str(_mapping(item).get("dependencies", "")),
                str(_mapping(item).get("boundary_notes", "")),
            ]
            for item in latest.get("modules") or ()
        ]
        sections["data_flows"] = [
            [
                str(_mapping(item).get("from", "")),
                str(_mapping(item).get("to", "")),
                str(_mapping(item).get("description", "")),
            ]
            for item in latest.get("data_flows") or ()
        ]
        sections["external_dependencies"] = [
            [
                str(_mapping(item).get("name", "")),
                str(_mapping(item).get("purpose", "")),
                str(_mapping(item).get("impact", "")),
            ]
            for item in latest.get("external_dependencies") or ()
        ]
        sections["risks"] = [
            [
                str(_mapping(item).get("severity", "")),
                str(_mapping(item).get("probability", "")),
                str(_mapping(item).get("impact", "")),
                str(_mapping(item).get("description", "")),
                str(_mapping(item).get("mitigation", "")),
            ]
            for item in latest.get("risks") or ()
        ]
        sections["adr_candidates"] = [
            [
                str(_mapping(item).get("title", "")),
                str(_mapping(item).get("recommended_status", "")),
                str(_mapping(item).get("decision", "")),
                str(_mapping(item).get("rationale", "")),
            ]
            for item in latest.get("adr_candidates") or ()
        ]
        sections["implementation_phases"] = [
            [
                str(_mapping(item).get("phase", "")),
                str(_mapping(item).get("goal", "")),
                str(_mapping(item).get("scope", "")),
            ]
            for item in latest.get("implementation_phases") or ()
        ]
        return sections

    def _proposal_digest_rows(
        self, latest: Optional[Mapping[str, Any]]
    ) -> list[list[str]]:
        """The bounded evidence digest: what the proposal was built from."""
        digest = _mapping((latest or {}).get("review_digest"))
        if not digest:
            return [["Evidence", "no proposal yet"]]
        judge = _mapping(digest.get("judge"))
        decision = _mapping(digest.get("decision"))
        gate = _mapping(digest.get("deterministic_gate"))
        if not gate:
            gate_text = "-"
        elif not gate.get("available", False):
            gate_text = f"unavailable ({gate.get('reason') or 'no reason'})"
        else:
            gate_text = (
                f"{'compliant' if gate.get('compliant') else 'violation(s)'}"
                f" | {gate.get('violation_count', 0)} violation(s)"
                f" | baseline {gate.get('baseline_version') or '-'}"
            )
        return [
            ["Review id", str(digest.get("review_id") or "-")],
            [
                "Advisors",
                f"{digest.get('provider_count', 0)} "
                f"(findings {digest.get('finding_count', 0)}, "
                f"abstain {digest.get('abstain_count', 0)}, "
                f"errors {digest.get('error_count', 0)})",
            ],
            [
                "Finding sources",
                ", ".join(
                    str(source)
                    for source in (digest.get("finding_sources") or ())
                )
                or "-",
            ],
            ["Conflicts", str(len(digest.get("conflicts") or ()))],
            ["Judge", str(judge.get("status") or "-")],
            ["Advisory decision", str(decision.get("status") or "-")],
            ["Deterministic gate (assistant)", gate_text],
        ]

    def _proposal_history_rows(
        self, board: Mapping[str, Any]
    ) -> list[list[str]]:
        """Every stored proposal, newest first - the immutable revision history."""
        rows: list[list[str]] = []
        for item in board.get("proposals") or ():
            if not isinstance(item, Mapping):
                continue
            rows.append(
                [
                    str(item.get("proposal_id", "")),
                    str(item.get("revision_no", "")),
                    str(item.get("status", "")),
                    _stamp(item.get("created_at")),
                    str(item.get("decided_by") or "-"),
                    str(item.get("decision_reason") or "-"),
                ]
            )
        return rows





    @property
    def review_question(self) -> str:
        """The question the next review will ask, exactly as the operator wrote it."""
        return self._review_question

    def set_review_question(self, question: str) -> None:
        """Replace the review question - stored verbatim, never rewritten."""
        if not isinstance(question, str):
            raise ValueError(f"question must be a str; got {question!r}")
        self._review_question = question

    @property
    def review(self) -> Optional[dict[str, Any]]:
        """The last review payload, as plain data (a copy), or ``None``."""
        return None if self._review is None else dict(self._review)

    @property
    def review_progress(self) -> str:
        """The stage of the running review, or ``""`` when it is not running."""
        return self._review_progress

    def drain_progress(self, messages: Sequence[str]) -> None:
        """Show the newest core progress line - plain strings only."""
        for message in messages:
            self._review_progress = str(message)
            self._status = f"Architecture review: {self._review_progress}"

    def _review_summary(self) -> str:
        """One status line for a finished review - honest about what happened."""
        review = self._review or {}
        providers = tuple(review.get("providers") or ())
        findings = [
            stage
            for stage in providers
            if isinstance(stage, Mapping) and stage.get("status") == "FINDING"
        ]
        judge = _mapping(review.get("judge"))
        decision = _mapping(review.get("decision"))
        gate = _mapping(review.get("deterministic_gate"))
        if not gate.get("available"):
            gate_text = "gate unavailable"
        else:
            gate_text = (
                f"gate {'compliant' if gate.get('compliant') else 'violated'}"
            )
        return (
            f"Review {review.get('review_id', '?')}: "
            f"{len(findings)}/{len(providers)} finding(s), "
            f"{len(review.get('conflicts') or ())} conflict(s), judge "
            f"{judge.get('status', '-')}, decision "
            f"{decision.get('status', '-')}, {gate_text}."
        )

    # -- the pending plan --------------------------------------------------

    def set_plan(self, text: str, *, source_file: str = "") -> None:
        """Remember the file the operator picked - plain text, no core object.

        A newly picked file always invalidates the previous preview, so Confirm
        can never act on a stale verdict.
        """
        if not isinstance(text, str):
            raise ValueError(f"plan text must be a str; got {text!r}")
        self._plan_text = text
        self._plan_source = str(source_file)
        self._plan_preview = None

    def clear_plan(self) -> None:
        """Forget the pending plan - cancelling writes nothing at all."""
        self._plan_text = ""
        self._plan_source = ""
        self._plan_preview = None

    @property
    def plan_pending(self) -> bool:
        """Whether a plan file is loaded and awaiting a preview or a confirm."""
        return bool(self._plan_text)

    @property
    def plan_source(self) -> str:
        """The file the pending plan was read from (as reported by the panel)."""
        return self._plan_source

    @property
    def plan_preview(self) -> Optional[dict[str, Any]]:
        """The last preview of the pending plan, as plain data (a copy)."""
        return None if self._plan_preview is None else dict(self._plan_preview)

    def plan_importable(self) -> bool:
        """Whether the loaded plan may be imported right now, per the core."""
        return bool(self._plan_preview and self._plan_preview.get("importable"))

    def plan_blocked_reason(self) -> str:
        """Why the loaded plan cannot be imported, or ``""`` when it can."""
        if self._plan_preview is None:
            return (
                "the plan has not been validated yet"
                if self.plan_pending
                else "no plan is loaded"
            )
        return str(self._plan_preview.get("blocked_reason") or "")

    def plan_preview_lines(self) -> list[str]:
        """The preview dialog text - plain strings, built from core data only."""
        if not self.plan_pending:
            return ["No plan is loaded."]
        preview = self._plan_preview
        if preview is None:
            return [
                f"Source file: {self._plan_source or '-'}",
                "The plan has not been validated yet.",
            ]
        counts = _mapping(preview.get("risk_counts"))
        risks = ", ".join(
            f"{level} {counts.get(level, 0)}"
            for level in ("LOW", "MEDIUM", "HIGH")
        )
        lines = [
            f"Source file: {preview.get('source_file') or self._plan_source or '-'}",
            f"Plan hash:   {str(preview.get('plan_hash', ''))[:16]}",
            "",
            f"Project (plan):     {preview.get('project_name') or '-'}",
            f"Project (database): {preview.get('db_project_name') or '-'}"
            f"{'' if preview.get('project_matches') else '   <- MISMATCH'}",
            f"Plan version:       {preview.get('plan_version') or '-'}"
            f"{'' if preview.get('plan_version_matches') else '   <- MISMATCH'}"
            f"{'' if preview.get('plan_version_declared') else '  (from the database)'}",
            f"Mode (plan):        {preview.get('mode') or '-'}"
            f"{'' if preview.get('mode_matches') else '   (not applied)'}",
            f"Mode (database):    {preview.get('db_mode') or '-'}",
            "",
            f"Steps:             {preview.get('step_count', 0)}"
            f"  ({preview.get('first_step_no')} ... {preview.get('last_step_no')})",
            f"Risk:              {risks}",
            f"Requires a human:  {preview.get('requires_human_count', 0)}",
            f"Phases:            {', '.join(preview.get('phases') or ()) or '-'}",
            f"Steps in database: {preview.get('db_step_count', 0)}",
            "",
        ]
        if preview.get("importable"):
            lines.append(
                "Ready to import: the plan is valid and this database holds no "
                "steps."
            )
        else:
            lines.append(
                "Cannot import: "
                f"{preview.get('blocked_reason') or 'the plan is not importable'}"
            )
        issues = preview.get("issues") or []
        if issues:
            lines.append("")
            lines.append(f"{len(issues)} validation problem(s):")
            for issue in issues:
                if not isinstance(issue, Mapping):
                    continue
                lines.append(
                    f"  {issue.get('path')}: {issue.get('problem')}"
                    f"   (value: {issue.get('value')})"
                )
        return lines

    def _plan_line(self) -> str:
        """One line about the plan this database holds - or is about to hold."""
        if self.plan_pending:
            preview = self._plan_preview
            if preview is None:
                return "pending: not validated yet"
            if preview.get("importable"):
                return (
                    f"pending: {preview.get('step_count', 0)} step(s) ready to "
                    "import"
                )
            return f"pending: cannot import ({preview.get('blocked_reason')})"
        count = self.step_count()
        return "no plan loaded" if count == 0 else f"{count} step(s) loaded"

    # -- the review tab ----------------------------------------------------

    def review_highlights(self) -> dict[str, Any]:
        """The compact factlets the review tab shows instead of the detail.

        Cost, evidence conflicts and the judge are *summaries* here and full
        windows in their popups, so the main tab never reserves space for data it
        does not have: an unused judge and an empty conflict list cost one line
        each, and the cost area is a single line plus one button.
        """
        review = self._review or {}
        conflicts = [
            conflict
            for conflict in (review.get("conflicts") or ())
            if isinstance(conflict, Mapping)
        ]
        judge = _mapping(review.get("judge"))
        used = bool(judge.get("consulted"))
        total = _mapping(_mapping(review.get("cost")).get("total"))
        return {
            "cost_summary": self.total_cost_text(),
            "cost_available": bool(total.get("available")),
            "cost_unavailable": bool(total) and not total.get("available"),
            "conflicts_count": len(conflicts),
            "conflicts_available": bool(conflicts),
            "conflicts_summary": f"Conflicts: {len(conflicts)}",
            "judge_used": used,
            "judge_status": str(judge.get("status") or "-"),
            "judge_summary": (
                f"Judge used: {judge.get('status') or '-'}"
                if used
                else "Judge not used"
            ),
        }

    def review_view(self) -> dict[str, Any]:
        """The Architecture Review tab, as plain data.

        The tab is laid out in two levels, and so is this model: one read-only
        pane per advisor (side by side, for comparison) and, below them, the
        *shared* results - merged evidence, conflicts, the judge, the advisory
        decision and the cost. Everything comes from the core's review payload;
        before the first review the panes are labelled from configuration and the
        shared sections say so explicitly - never a fake result.
        """
        review = self._review
        if review is None:
            return {
                "available": False,
                "question": self._review_question,
                "progress": self._review_progress,
                "provider_settings": self.provider_settings_view(),
                **self.review_highlights(),
                "status": "No architecture review has been run in this session.",
                "header": [],
                "provider_panels": self.provider_panels(),
                "merged_rows": self.merged_evidence_rows(),
                "conflict_rows": self.review_conflict_rows(),
                "judge_lines": self.judge_lines(),
                "decision_lines": self.decision_lines(),
                "cost_rows": self.cost_rows(),
                "total_cost": self.total_cost_text(),
            }
        return {
            "available": True,
            "question": self._review_question,
            "progress": self._review_progress,
            "provider_settings": self.provider_settings_view(),
            **self.review_highlights(),
            "status": self._review_summary(),
            "header": self.review_header(),
            "provider_panels": self.provider_panels(),
            "merged_rows": self.merged_evidence_rows(),
            "conflict_rows": self.review_conflict_rows(),
            "judge_lines": self.judge_lines(),
            "decision_lines": self.decision_lines(),
            "cost_rows": self.cost_rows(),
            "total_cost": self.total_cost_text(),
        }

    def review_header(self) -> list[tuple[str, str]]:
        """The review header: project, source root, step, baseline and gate."""
        review = self._review or {}
        gate = _mapping(review.get("deterministic_gate"))
        if not gate.get("available"):
            gate_text = f"unavailable ({gate.get('reason') or 'no reason'})"
        else:
            gate_text = (
                f"{'compliant' if gate.get('compliant') else 'violation(s)'}"
                f" | {gate.get('violation_count', 0)} violation(s)"
                f" | baseline {gate.get('baseline_version') or '-'}"
                f" | decision {gate.get('decision_id') or '-'}"
            )
        root = str(review.get("source_root") or "-")
        if review.get("source_root_verified"):
            root_text = f"{root}  (confirmed by the realization check)"
        else:
            check_root = review.get("check_source_root")
            detail = (
                f"; the check reports {check_root}"
                if check_root
                else "; the check reports no root"
            )
            root_text = f"{root}  [NOT confirmed{detail}]"
        step_no = review.get("step_no")
        return [
            ("Project", str(review.get("project") or "-")),
            ("Source root", root_text),
            ("Current step", "-" if step_no is None else str(step_no)),
            (
                "Architecture version",
                str(review.get("architecture_version") or "-"),
            ),
            ("Deterministic gate", gate_text),
            ("Review id", str(review.get("review_id") or "-")),
            (
                "Reviewed at",
                str(review.get("reviewed_at") or "-").replace("T", " ")[:19],
            ),
            ("Question", str(review.get("question") or "-")),
            ("Persisted", "no - the review is advisory and read-only"),
        ]

    def provider_panels(self) -> list[dict[str, Any]]:
        """One read-only pane per advisor, in the declared order - plain data.

        The three advisors are shown **side by side** so their answers can be
        compared at a glance, and each pane carries everything one provider
        produced: its execution status, the full finding text, the structured
        facts, its evidence references, the sanitized ABSTAIN/ERROR reason and
        what it cost in tokens and money.

        Before the first review the panes are labelled from the *configuration*
        the core reports, so the operator sees which advisors are wired without
        running anything. A configured advisor is never hidden: there is one pane
        per configured advisor, and a missing one is padded with an explicit
        "not run" pane - never with an invented result.
        """
        review = self._review
        panels: list[dict[str, Any]] = []
        if review is None:
            names = self.configured_advisors()
            for index in range(max(len(names), MIN_PROVIDER_PANES)):
                panels.append(
                    _idle_panel(
                        names[index] if index < len(names) else "",
                        index,
                    )
                )
            return panels
        for stage in review.get("providers") or ():
            if isinstance(stage, Mapping):
                panels.append(_provider_panel(stage))
        while len(panels) < MIN_PROVIDER_PANES:
            panels.append(_idle_panel("", len(panels)))
        return panels

    def configured_advisors(self) -> list[str]:
        """The advisor names the core reports as configured, in declared order."""
        raw = self._payload.get("reviewers")
        if isinstance(raw, (list, tuple)):
            return [str(item) for item in raw]
        return []

    def merged_evidence_rows(self) -> list[list[str]]:
        """The shared merged-evidence facts, as label/value rows."""
        evidence = _mapping((self._review or {}).get("evidence"))
        if not evidence:
            return [["Merged evidence", "no architecture review yet"]]
        abstained = [
            outcome
            for outcome in (evidence.get("abstained") or ())
            if isinstance(outcome, Mapping)
        ]
        errors = [
            outcome
            for outcome in (evidence.get("errors") or ())
            if isinstance(outcome, Mapping)
        ]
        anchors = ", ".join(
            str(item) for item in (evidence.get("gate_anchors") or ())
        )
        return [
            ["Findings", str(len(evidence.get("findings") or ()))],
            ["Supporting", str(len(evidence.get("supporting_ids") or ()))],
            ["Conflicting", str(len(evidence.get("conflicting_ids") or ()))],
            ["Unresolved", str(len(evidence.get("unresolved_ids") or ()))],
            ["Abstained", str(len(abstained))],
            ["Errors", str(len(errors))],
            ["Validated conflicts", str(len(evidence.get("conflicts") or ()))],
            ["Gate status", str(evidence.get("gate_status") or "-")],
            ["Gate anchors", anchors or "-"],
            [
                "Abstained by",
                " | ".join(
                    f"{outcome.get('source')}: {outcome.get('reason')}"
                    for outcome in abstained
                )
                or "-",
            ],
            [
                "Failed",
                " | ".join(
                    f"{outcome.get('source')}: {outcome.get('reason')}"
                    for outcome in errors
                )
                or "-",
            ],
            [
                "Unresolved questions",
                " | ".join(
                    str(item)
                    for item in (evidence.get("unresolved_questions") or ())
                )
                or "-",
            ],
        ]

    def judge_lines(self) -> list[str]:
        """The judge result as text - explicitly a separate layer."""
        judge = _mapping((self._review or {}).get("judge"))
        if not judge:
            return ["No architecture review has been run in this session."]
        lines = [
            f"Available:  {bool(judge.get('available'))}",
            f"Consulted:  {bool(judge.get('consulted'))}",
            f"Status:     {judge.get('status') or '-'}",
            f"Conflicts answered: {judge.get('count', 0)}",
        ]
        if judge.get("reason"):
            lines.append(f"Reason: {judge.get('reason')}")
        for judgment in judge.get("judgments") or ():
            if not isinstance(judgment, Mapping):
                continue
            decision = _mapping(judgment.get("decision"))
            conflict = _mapping(judgment.get("conflict"))
            lines.extend(
                [
                    "",
                    f"--- conflict on {conflict.get('target')} ---",
                    "Supporting: "
                    + (
                        ", ".join(
                            str(item)
                            for item in (conflict.get("supporting_ids") or ())
                        )
                        or "-"
                    ),
                    "Contradicting: "
                    + (
                        ", ".join(
                            str(item)
                            for item in (
                                conflict.get("contradicting_ids") or ()
                            )
                        )
                        or "-"
                    ),
                    f"Judge decision: {decision.get('status')} "
                    f"({decision.get('id')})",
                    str(decision.get("decision") or "-"),
                ]
            )
            if decision.get("rationale"):
                lines.append(str(decision["rationale"]))
        return lines

    def review_conflict_rows(self) -> list[list[str]]:
        """One row per validated evidence conflict."""
        if self._review is None:
            return [["-", "no architecture review yet", "-"]]
        rows: list[list[str]] = []
        for conflict in (self._review or {}).get("conflicts") or ():
            if not isinstance(conflict, Mapping):
                continue
            rows.append(
                [
                    str(conflict.get("target", "")),
                    ", ".join(
                        str(item)
                        for item in (conflict.get("supporting_ids") or ())
                    )
                    or "-",
                    ", ".join(
                        str(item)
                        for item in (conflict.get("contradicting_ids") or ())
                    )
                    or "-",
                ]
            )
        return rows

    def decision_lines(self) -> list[str]:
        """The advisory decision as text - it explains the gate, never overrides it."""
        decision = _mapping((self._review or {}).get("decision"))
        if not decision:
            return ["No architecture review has been run in this session."]
        return [
            f"Status:      {decision.get('status') or '-'}",
            f"Decision id: {decision.get('id') or '-'}",
            "",
            str(decision.get("decision") or "-"),
            "",
            "Rationale:",
            str(decision.get("rationale") or "-"),
            "",
            "Perspectives: "
            + (
                ", ".join(
                    str(item) for item in (decision.get("perspectives") or ())
                )
                or "-"
            ),
            "",
            "Advisory only: the deterministic gate remains the only verdict.",
        ]

    def cost_rows(self) -> list[list[str]]:
        """Per-advisor, judge and total cost - the review's own bill."""
        cost = _mapping((self._review or {}).get("cost"))
        if not cost:
            return [["total", "no architecture review yet", "-"]]
        rows: list[list[str]] = []
        for entry in cost.get("providers") or ():
            if not isinstance(entry, Mapping):
                continue
            rows.append(
                [
                    str(entry.get("source", "?")),
                    _money_text(entry),
                    _tokens_text(entry),
                ]
            )
        judge = _mapping(cost.get("judge"))
        rows.append(["judge", _money_text(judge), _tokens_text(judge)])
        total = _mapping(cost.get("total"))
        rows.append(["total", _money_text(total), _tokens_text(total)])
        return rows

    def total_cost_text(self) -> str:
        """One bold line with the review's total bill - or why there is none."""
        total = _mapping(
            _mapping((self._review or {}).get("cost")).get("total")
        )
        if not total:
            return "no architecture review yet"
        if not total.get("available"):
            return f"unavailable ({total.get('reason') or 'no reason'})"
        return (
            f"{_number(total.get('total_usd')):.4f} USD | "
            f"{total.get('record_count', 0)} record(s) "
            f"({total.get('priced_record_count', 0)} priced, "
            f"{total.get('unpriced_record_count', 0)} unpriced) | "
            f"{total.get('input_tokens', 0)} in / "
            f"{total.get('output_tokens', 0)} out tokens"
        )

    # -- results -----------------------------------------------------------

    def apply_result(self, result: Any) -> None:
        """Apply one finished job: store payloads, classify failures, clear busy."""
        self._busy = False
        label = str(getattr(result, "label", ""))
        error = getattr(result, "error", None)
        if error is not None:
            self._last_error = error
            if bool(getattr(error, "critical", False)):
                self._critical = True
            error_name = str(getattr(error, "error_name", "Error"))
            if label == SAVE_SETTINGS_INTENT:
                # A refused save changes nothing at all - say so next to the fields.
                self._provider_note = (
                    f"provider settings rejected ({error_name})"
                )
            # The log names the exception *type*; the banner carries the detail.
            self.log(
                f"{label} failed: {error_name}",
                level=EVENT_LEVEL_ERROR,
                action=f"{label}-error",
                step_no=self.current_step_no(),
            )
            return
        if label == "open":
            self.log("Core ready.")
            return
        payload = getattr(result, "payload", None)
        if not isinstance(payload, Mapping):
            self.log(f"{label} finished.")
            return
        fresh = payload.get("payload")
        if isinstance(fresh, Mapping):
            self._payload = dict(fresh)
            # A successful read proves the source of truth is usable again.
            self._critical = False
            self._last_error = None
        audit = payload.get("audit")
        if isinstance(audit, list):
            self._audit = list(audit)
        plan = payload.get("plan")
        if isinstance(plan, Mapping):
            # A finished preview: plain data, shown by the application.
            self._plan_preview = dict(plan)
            self._log_action("load_plan", self._plan_summary())
        review = payload.get("review")
        if isinstance(review, Mapping):
            # A finished review: plain data, rendered by the review tab.
            self._review = dict(review)
            self._review_progress = ""
            self._log_action(
                "run_review",
                self._review_summary(),
                level=(
                    EVENT_LEVEL_INFO
                    if str(self._review.get("judge", {}).get("status", ""))
                    != "ERROR"
                    else EVENT_LEVEL_ERROR
                ),
            )
        board = payload.get("board")
        if isinstance(board, Mapping):
            # The proposal board: plain data, rendered by the proposal tab.
            self._proposal_board = dict(board)
        supervisor = payload.get("supervisor")
        if isinstance(supervisor, Mapping):
            # The supervision board: plain data, rendered by the supervisor tab.
            self._supervisor = dict(supervisor)
        proposal = payload.get("proposal")
        if isinstance(proposal, Mapping):
            self._proposal = dict(proposal)
        settings = payload.get("settings")
        if isinstance(settings, Mapping):
            # A finished provider-settings save: the note and a log line only.
            self._apply_settings_result(label, settings)
        connection = payload.get("connection")
        if isinstance(connection, Mapping):
            # A finished Test Connection probe: the verdict word only.
            self._apply_connection_result(label, connection)
        outcome = payload.get("result")
        if isinstance(outcome, Mapping):
            self._remember_export(label, outcome)
            self._describe(label, outcome)
        elif label == "refresh":
            self._log_action(label, "Refreshed from the monitor projection.")
        elif label == "reconnect":
            self._log_action(label, "Reconnected to the source of truth.")
        if label == "import_plan":
            # The plan is now the source of truth's plan: nothing is pending.
            self.clear_plan()

    def _apply_settings_result(
        self, label: str, outcome: Mapping[str, Any]
    ) -> None:
        """Record one finished provider-settings save - never a credential.

        The outcome carries a boolean, a path and the key-free configuration view,
        so this method cannot render a secret even by accident.
        """
        saved = bool(outcome.get("saved"))
        path = str(outcome.get("path") or "the provider settings file")
        self._provider_note = (
            "provider settings saved" if saved else "provider settings not saved"
        )
        self._log_action(
            label,
            (
                f"Provider settings saved to {path}."
                if saved
                else (
                    f"Provider settings were NOT saved: {path} could not be "
                    "written, so nothing was changed."
                )
            ),
            level=EVENT_LEVEL_INFO if saved else EVENT_LEVEL_WARN,
        )

    def _apply_connection_result(
        self, label: str, outcome: Mapping[str, Any]
    ) -> None:
        """Record one Test Connection verdict, keyed by the pane's action key.

        Only the verdict word travels: the probe answers ``CONNECTED``,
        ``AUTH ERROR``, ``PROVIDER ERROR``, ``NETWORK ERROR``, ``MODEL ERROR`` or
        ``DISABLED``, and nothing else is read from the payload.
        """
        status = str(outcome.get("status") or "")
        self._connection_results[label] = status
        provider = str(outcome.get("provider") or "?")
        self._log_action(
            label,
            f"Test Connection ({provider}): {status or 'no verdict'}.",
            level=EVENT_LEVEL_INFO if status == "CONNECTED" else EVENT_LEVEL_WARN,
        )

    def _plan_summary(self) -> str:
        """One status line for a finished preview."""
        preview = self._plan_preview or {}
        if preview.get("importable"):
            return (
                f"Plan preview: {preview.get('step_count', 0)} step(s) "
                f"({preview.get('first_step_no')}-{preview.get('last_step_no')}) "
                "ready to import."
            )
        return (
            "Plan preview: cannot be imported - "
            f"{preview.get('blocked_reason') or 'invalid plan'}."
        )

    def _remember_export(self, label: str, outcome: Mapping[str, Any]) -> None:
        """Remember one export artifact for the Reports popup.

        Only what the core already reported is kept - the artifact kind and the
        path it wrote - so the popup never has to ask for anything itself.
        """
        if label not in ("export_markdown", "export_excel"):
            return
        path = str(outcome.get("path") or "").strip()
        if not path:
            return
        entry = {"kind": label, "path": path}
        if entry not in self._exports:
            self._exports.insert(0, entry)

    def _describe(self, label: str, outcome: Mapping[str, Any]) -> None:
        """One human-readable status line for a finished action."""
        if label == "run_until_idle":
            self._last_run = dict(outcome)
            self._log_action(
                label,
                "Loop stopped: "
                f"{outcome.get('stopped_because', '?')} "
                f"({outcome.get('transition_count', 0)} transitions).",
            )
        elif label in ("pause_project", "resume_project"):
            state = "paused" if outcome.get("paused") else "running"
            self._log_action(label, f"Project is now {state}.")
        elif label == "import_plan":
            self._log_action(
                label,
                f"Plan imported: {outcome.get('step_count', 0)} step(s) "
                f"({outcome.get('first_step_no')}-{outcome.get('last_step_no')}), "
                f"plan {outcome.get('plan_version') or '-'}, hash "
                f"{str(outcome.get('plan_hash', ''))[:12]}. Current step is now "
                "visible in the work panel.",
            )
        elif label == "synthesize_proposal":
            self._log_action(
                label,
                f"Proposal {outcome.get('proposal_id')} generated for the "
                "managed project (status DRAFT).",
            )
        elif label in PROPOSAL_DECISION_INTENTS:
            self._log_action(
                label,
                f"Proposal {outcome.get('proposal_id')} is now "
                f"{outcome.get('status')}.",
            )
        elif "step_no" in outcome:
            self._log_action(
                label,
                f"{outcome.get('action')} applied to step "
                f"{outcome.get('step_no')}: now {outcome.get('state')}.",
            )
        elif "path" in outcome:
            self._log_action(label, f"Artifact written: {outcome.get('path')}")
        else:
            self._log_action(label, f"{label} finished.")

    def _log_action(
        self, action: str, message: str, *, level: str = EVENT_LEVEL_INFO
    ) -> None:
        """Log one panel line for a finished action, tagged with that action."""
        self.log(
            message, level=level, action=action, step_no=self.current_step_no()
        )

    # -- what the UI reads -------------------------------------------------

    def current_step(self) -> Optional[dict[str, Any]]:
        """The current step dictionary, or ``None`` when there is none."""
        step = self._payload.get("current_step")
        return dict(step) if isinstance(step, Mapping) else None

    def current_state(self) -> Optional[str]:
        """The current step's persisted state, or ``None``."""
        step = self.current_step()
        if step is None:
            return None
        state = step.get("state")
        return None if state is None else str(state)

    def is_paused(self) -> bool:
        """Whether the project is paused, per the projection."""
        project = self._payload.get("project")
        return bool(isinstance(project, Mapping) and project.get("paused"))

    def step_count(self) -> int:
        """How many steps the plan currently holds."""
        health = self._payload.get("health")
        if not isinstance(health, Mapping):
            return 0
        value = health.get("step_count", 0)
        if isinstance(value, bool) or not isinstance(value, int):
            return 0
        return value

    def log_lines(self) -> list[str]:
        """The newest-last log lines for the Reports / Log tab.

        A *view* over the one structured buffer, not a second store: the same
        entries the Logs tab renders as rows, as readable lines.
        """
        lines: list[str] = []
        for entry in self._logs:
            lines.append(
                f"{_stamp(entry.get('timestamp'))} | {entry.get('level')} | "
                f"{entry.get('component')} | {entry.get('action')} | "
                f"{entry.get('message')}"
            )
        return lines

    def log_rows(self) -> list[list[str]]:
        """The Logs tab rows, **newest first**, as plain strings.

        Newest first is the presentation order the operator needs: a new entry is
        visible immediately without scrolling. The buffer itself stays oldest
        first, so the data order is still the order events happened in. Rows are
        lists - not tuples - so the view model survives a JSON round trip
        unchanged, which is what "plain data" has to mean here.
        """
        rows: list[list[str]] = []
        for entry in reversed(self._logs):
            step_no = entry.get("step_no")
            rows.append(
                [
                    "-" if entry.get("seq") is None else str(entry["seq"]),
                    _stamp(entry.get("timestamp")),
                    str(entry.get("level", "")),
                    str(entry.get("component", "")),
                    str(entry.get("action", "")),
                    str(entry.get("message", "")),
                    "-" if step_no is None else str(step_no),
                    str(entry.get("review_id") or "-"),
                ]
            )
        return rows

    def log_entries(self) -> list[dict[str, Any]]:
        """A copy of the log entries, oldest first - plain data, for tests."""
        return [dict(entry) for entry in self._logs]

    @property
    def log_count(self) -> int:
        """How many entries the view currently holds."""
        return len(self._logs)

    @property
    def log_limit(self) -> int:
        """How many entries the view can hold before the oldest are dropped."""
        return self._logs.maxlen or 0

    def drain_events(self, events: Sequence[Mapping[str, Any]]) -> None:
        """Append drained core events in order and show the newest stage.

        This is the Tk thread's whole involvement with the core's log: it reads
        plain dictionaries off a queue and appends them. Nothing here reaches the
        worker, a provider or the database.
        """
        for raw in events:
            entry = _log_entry(raw)
            if entry is None:
                continue
            self._logs.append(entry)
            self._review_progress = (
                f"{entry.get('component')}: {entry.get('action')}"
            )

    def clear_log_view(self) -> None:
        """Empty the panel's log view - **nothing else is touched**.

        The logs are ephemeral by design. The persistent record of what happened
        is the append-only audit trail, which this method cannot reach: it holds
        no repository, no connection and no worker.
        """
        self._logs.clear()
        self._status = (
            "Log view cleared. Nothing was deleted: the persistent audit trail "
            "is on the Audit tab."
        )

    def current_step_no(self) -> Optional[int]:
        """The current step number as an int, or ``None``."""
        step = self.current_step()
        if step is None:
            return None
        value = step.get("step_no")
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    def snapshot_json(self) -> str:
        """The canonical monitor projection as JSON, for the snapshot dialog."""
        canonical = self._payload.get("canonical")
        if not isinstance(canonical, Mapping):
            return "{}"
        return json.dumps(dict(canonical), indent=2, sort_keys=True)

    # -- the popup windows (display only) ----------------------------------
    #
    # Every secondary view the main window no longer hosts is one of these: a
    # plain-data *spec* the host renders in a resizable, non-modal window. Nothing
    # here reads a repository, a provider or the database, and nothing here
    # decides anything - each builder only reshapes the payload the controller
    # already holds, so a popup can never disagree with the main window.

    def popup_view(self, key: str) -> dict[str, Any]:
        """The spec for one popup window, or ``{}`` when there is no such window."""
        if key in POPUP_ADVISOR_KEYS:
            return self.advisor_popup(POPUP_ADVISOR_KEYS.index(key))
        builders = {
            POPUP_COST: self.cost_popup,
            POPUP_LOGS: self.logs_popup,
            POPUP_AUDIT: self.audit_popup,
            POPUP_RISKS: self.risks_popup,
            POPUP_REPORTS: self.reports_popup,
            POPUP_PROJECT: self.project_popup,
            POPUP_ARCHITECTURE: self.architecture_popup,
            POPUP_SUPERVISOR: self.supervisor_popup,
            POPUP_JUDGE: self.judge_popup,
            POPUP_CONFLICTS: self.conflicts_popup,
        }
        builder = builders.get(str(key))
        return {} if builder is None else builder()

    def cost_popup(self) -> dict[str, Any]:
        """The full cost picture: total, per provider, the judge and the records.

        The unavailable state is stated in words and **no number is invented**:
        when the core reports that no cost telemetry exists, the popup says so and
        leaves the amount out rather than printing a zero-dollar cost.
        """
        review = self._review
        cost = _mapping((review or {}).get("cost"))
        total = _mapping(cost.get("total"))
        if review is None:
            note = (
                "Cost unavailable: no architecture review has been run in this "
                "session."
            )
        elif not total:
            note = "Cost unavailable: this review carried no cost section."
        elif not total.get("available"):
            note = (
                "Cost unavailable: "
                f"{total.get('reason') or 'no reason was reported'}."
            )
        else:
            note = (
                "Total cost of this review: "
                f"{_number(total.get('total_usd')):.4f} USD | "
                f"{total.get('record_count', 0)} record(s) | "
                f"{total.get('input_tokens', 0)} in / "
                f"{total.get('output_tokens', 0)} out tokens"
            )
        return {
            "title": "Cost Details",
            "note": note,
            "sections": [
                _popup_table(
                    "Total (this review)",
                    POPUP_COST_COLUMNS[:2],
                    self._cost_total_rows(),
                    empty="No cost data for this review.",
                ),
                _popup_table(
                    "Per advisor and judge",
                    POPUP_COST_COLUMNS,
                    self.cost_rows(),
                    empty="No per-advisor cost records for this review.",
                ),
                _popup_table(
                    "All recorded cost (project)",
                    POPUP_COST_COLUMNS,
                    self._project_cost_rows(),
                    empty="No cost records have been recorded yet.",
                ),
            ],
        }

    def _cost_total_rows(self) -> list[list[str]]:
        """The total row set - honest about availability, never a fake zero."""
        total = _mapping(_mapping((self._review or {}).get("cost")).get("total"))
        if not total:
            return []
        if not total.get("available"):
            return [
                [
                    "Availability",
                    f"unavailable ({total.get('reason') or 'no reason reported'})",
                ]
            ]
        records = total.get("record_count", 0)
        priced = total.get("priced_record_count", 0)
        unpriced = total.get("unpriced_record_count", 0)
        return [
            ["Availability", "available"],
            ["Total cost", f"{_number(total.get('total_usd')):.4f} USD"],
            [
                "Total tokens",
                f"{total.get('input_tokens', 0)} in / "
                f"{total.get('output_tokens', 0)} out",
            ],
            [
                "Cost records",
                f"{records} ({priced} priced, {unpriced} unpriced)",
            ],
        ]

    def _project_cost_rows(self) -> list[list[str]]:
        """The project-wide cost aggregate the projection already reports."""
        cost = _mapping(self._payload.get("cost"))
        if not cost:
            return []
        return [
            ["All records", f"{_number(cost.get('total_usd')):.4f} USD"],
            [
                "All tokens",
                f"{cost.get('input_tokens', 0)} in / "
                f"{cost.get('output_tokens', 0)} out",
            ],
            [
                "Records",
                f"{cost.get('record_count', 0)} "
                f"({cost.get('priced_record_count', 0)} priced, "
                f"{cost.get('unpriced_record_count', 0)} unpriced)",
            ],
        ]

    def logs_popup(self) -> dict[str, Any]:
        """The structured runtime events, newest first - the existing log view."""
        return {
            "title": "Logs",
            "note": (
                f"Read-only: {self.log_count} of {self.log_limit} entries kept, "
                "newest first. This is the runtime event view, not the audit "
                "trail: clearing it deletes nothing that was recorded."
            ),
            "sections": [
                _popup_table(
                    "Runtime events",
                    POPUP_LOG_COLUMNS,
                    self.log_rows(),
                    empty="No events",
                )
            ],
        }

    def audit_popup(self) -> dict[str, Any]:
        """The append-only trail, newest first - read-only here as everywhere."""
        rows = self.audit_detail_rows()
        return {
            "title": "Audit History",
            "note": (
                f"{len(rows)} audit entr{'y' if len(rows) == 1 else 'ies'} read "
                "from the append-only trail (newest first). Read-only: this window "
                "cannot change, retract or re-decide anything."
            ),
            "sections": [
                _popup_table(
                    "Audit entries",
                    POPUP_AUDIT_COLUMNS,
                    rows,
                    empty="No audit entries",
                )
            ],
        }

    def audit_detail_rows(self) -> list[list[str]]:
        """Audit rows with the recorded reason pulled out of the detail payload."""
        rows: list[list[str]] = []
        for entry in self._audit:
            detail = str(entry.get("detail", ""))
            step_no = entry.get("step_no")
            rows.append(
                [
                    str(entry.get("created_at", "")).replace("T", " ")[:19],
                    f"{entry.get('entity_type', '')} "
                    f"{entry.get('entity_id', '')}".strip(),
                    str(entry.get("action", "")),
                    str(entry.get("event", "")),
                    str(entry.get("actor", "")),
                    "-" if step_no is None else str(step_no),
                    _reason_of(detail),
                    detail,
                ]
            )
        return rows

    def risks_popup(self) -> dict[str, Any]:
        """The full risk register: owner, mitigation and impact included."""
        rows = self.risk_detail_rows()
        return {
            "title": "Risk Register",
            "note": (
                (
                    f"{len(rows)} open risk(s)."
                    if rows
                    else "No open risks."
                )
                + " Read-only: risks are raised, mitigated and closed by the "
                "core and by audited human actions, never here."
            ),
            "sections": [
                _popup_table(
                    "Open risks",
                    POPUP_RISK_COLUMNS,
                    rows,
                    empty="No open risks",
                )
            ],
        }

    def risk_detail_rows(self) -> list[list[str]]:
        """Every risk field the register holds, as table rows."""
        rows: list[list[str]] = []
        for risk in self._payload.get("open_risks") or ():
            if not isinstance(risk, Mapping):
                continue
            rows.append(
                [
                    str(risk.get("id", "")),
                    str(risk.get("severity", "")),
                    str(risk.get("probability", "")),
                    str(risk.get("impact", "")),
                    str(risk.get("owner") or "-"),
                    str(risk.get("status", "")),
                    str(risk.get("description", "")),
                    str(risk.get("mitigation") or "-"),
                ]
            )
        return rows

    def reports_popup(self) -> dict[str, Any]:
        """Report artifacts and where they live - this session's export history."""
        return {
            "title": "Reports",
            "note": (
                f"Report directory: {self.reports_dir} | "
                f"{len(self._exports)} artifact(s) produced in this session. The "
                "export actions here are the same core calls the Reports buttons "
                "use; nothing in this window writes anything itself."
            ),
            "sections": [
                _popup_table(
                    "Artifacts produced in this session",
                    POPUP_REPORT_COLUMNS,
                    [
                        [str(entry.get("kind", "")), str(entry.get("path", ""))]
                        for entry in self._exports
                    ],
                    empty="No report has been exported in this session.",
                ),
                _popup_table(
                    "Latest report",
                    POPUP_FACT_COLUMNS,
                    self._latest_report_rows(),
                    empty="No report projection is available yet.",
                ),
            ],
            "actions": [
                {"label": "Export Markdown", "key": "export_markdown"},
                {"label": "Export Excel", "key": "export_excel"},
                {"label": "Open reports folder", "key": "open_reports_folder"},
            ],
        }

    def _latest_report_rows(self) -> list[list[str]]:
        """What the last projection says about itself - no invented values."""
        canonical = _mapping(self._payload.get("canonical"))
        if not canonical:
            return []
        health = _mapping(self._payload.get("health"))
        return [
            ["Report directory", self.reports_dir],
            ["Schema version", str(canonical.get("schema_version") or "-")],
            ["Generated at", _stamp(canonical.get("generated_at"))],
            ["Project", str(_mapping(canonical.get("project")).get("name") or "-")],
            ["Steps", str(health.get("step_count", 0))],
            [
                "Architecture version",
                str(self._payload.get("architecture_version") or "-"),
            ],
        ]

    def project_popup(self) -> dict[str, Any]:
        """The project's own read-only facts, technical but never secret."""
        return {
            "title": "Project Details",
            "note": (
                "Read-only runtime configuration and state. No credential, no "
                "environment value and no provider key is shown here."
            ),
            "sections": [
                _popup_table(
                    "Project",
                    POPUP_FACT_COLUMNS,
                    self._project_rows(),
                    empty="No project has been read yet.",
                ),
                _popup_table(
                    "Paths and runtime",
                    POPUP_FACT_COLUMNS,
                    self._runtime_rows(),
                    empty="No runtime configuration has been read yet.",
                ),
            ],
        }

    def _project_rows(self) -> list[list[str]]:
        project = _mapping(self._payload.get("project"))
        if not project:
            return []
        current = self.current_step()
        return [
            ["Project", str(project.get("name", "-"))],
            ["Plan version", str(project.get("plan_version") or "-")],
            ["Plan hash", str(project.get("plan_hash") or "-")],
            ["Mode", str(project.get("mode", "-"))],
            ["Paused", _yes(project.get("paused"))],
            ["Current step", _step_no(current)],
            ["Current state", str(self.current_state() or "-")],
            [
                "Current step (snapshot field, non-authoritative)",
                str(project.get("current_step_no_snapshot") or "-"),
            ],
            ["Created at", _stamp(project.get("created_at"))],
            ["Updated at", _stamp(project.get("updated_at"))],
        ]

    def _runtime_rows(self) -> list[list[str]]:
        paths = _mapping(self._payload.get("paths"))
        if not paths and not self._payload:
            return []
        reviewers = self.configured_advisors()
        return [
            ["Database path", str(paths.get("database_path") or "-")],
            ["Worker channel", str(paths.get("exchange_dir") or "-")],
            ["Report directory", str(paths.get("report_dir") or self.reports_dir)],
            ["Source root", str(paths.get("source_root") or "-")],
            [
                "Architecture baseline",
                str(self._payload.get("architecture_version") or "-"),
            ],
            ["Advisors", ", ".join(reviewers) or "-"],
            ["Steps", str(self.step_count())],
        ]

    def architecture_popup(self) -> dict[str, Any]:
        """The deterministic side: baseline, rules, violations, gate and source root."""
        gate = _mapping((self._review or {}).get("deterministic_gate"))
        declared = _mapping(
            _mapping(self._payload.get("canonical")).get("architecture")
        )
        return {
            "title": "Architecture Details",
            "note": (
                "The deterministic view: the canonical baseline, the rules the "
                "review applied, the violations it found and the gate verdict. The "
                "gate remains the only verdict - an advisor can never override it."
            ),
            "sections": [
                _popup_table(
                    "Baseline",
                    POPUP_FACT_COLUMNS,
                    self._baseline_rows(declared),
                    empty="No baseline has been read from the projection.",
                ),
                _popup_table(
                    "Declared rules of the baseline",
                    POPUP_FACT_COLUMNS,
                    [
                        [f"Rule {index + 1}", str(rule)]
                        for index, rule in enumerate(declared.get("rules") or ())
                    ],
                    empty="No declared rule was read for this baseline.",
                ),
                _popup_table(
                    "Deterministic gate",
                    POPUP_FACT_COLUMNS,
                    self._gate_rows(gate),
                    empty="No deterministic gate check is available.",
                ),
                _popup_table(
                    "Rules applied by this review",
                    POPUP_FACT_COLUMNS,
                    [
                        [f"Rule {index + 1}", str(rule)]
                        for index, rule in enumerate(gate.get("rules") or ())
                    ],
                    empty="No rule identifiers were reported for this review.",
                ),
                _popup_table(
                    "Violations",
                    POPUP_FACT_COLUMNS,
                    [
                        [f"Finding {index + 1}", str(item)]
                        for index, item in enumerate(gate.get("finding_ids") or ())
                    ],
                    empty="No violating finding was reported.",
                ),
            ],
        }

    def _baseline_rows(self, declared: Mapping[str, Any]) -> list[list[str]]:
        """The canonical baseline as rows - and nothing it does not report."""
        version = str(self._payload.get("architecture_version") or "-")
        if not declared:
            return []
        rows = [
            ["Current baseline version", version],
            ["Declared baseline", str(declared.get("version") or "-")],
            ["Description", str(declared.get("baseline") or "-")],
            ["Declared here", _stamp(declared.get("created_at"))],
            ["Is current", _yes(declared.get("is_current"))],
            ["Superseded by", str(declared.get("superseded_by") or "-")],
            ["Declared rules", str(len(declared.get("rules") or ()))],
            # The deterministic check reports rules and violations; it does not
            # report how many modules it scanned, so the row says exactly that.
            [
                "Modules scanned",
                "not reported for this review (the check reports rules and "
                "violations)",
            ],
        ]
        return rows

    def _gate_rows(self, gate: Mapping[str, Any]) -> list[list[str]]:
        """The gate verdict as rows - or why there is none."""
        if not gate:
            return []
        rows = [
            ["Available", _yes(gate.get("available"))],
            ["Baseline version", str(gate.get("baseline_version") or "-")],
            [
                "Compliant",
                _yes(gate.get("compliant")) if gate.get("available") else "-",
            ],
            ["Violations", str(gate.get("violation_count", 0))],
            ["Decision id", str(gate.get("decision_id") or "-")],
            ["Decision status", str(gate.get("decision_status") or "-")],
            ["Source root", self._source_root_text()],
        ]
        if not gate.get("available"):
            rows.append(["Reason", str(gate.get("reason") or "not reported")])
        return rows

    def _source_root_text(self) -> str:
        """Which source tree the deterministic check actually inspected."""
        review = self._review or {}
        source_root = str(review.get("source_root") or "-")
        if review.get("source_root_verified"):
            return f"{source_root} (confirmed)"
        return (
            f"{source_root} [NOT confirmed; the check reports "
            f"{review.get('check_source_root') or 'no root'}]"
        )

    def supervisor_popup(self) -> dict[str, Any]:
        """Supervisor technical details - everything the compact pane leaves out."""
        payload = self.supervision()
        runtime = _mapping(self._supervisor.get("runtime"))
        raw_record = payload.get("record")
        record = dict(raw_record) if isinstance(raw_record, Mapping) else {}
        raw_context = payload.get("context")
        context = dict(raw_context) if isinstance(raw_context, Mapping) else {}
        verdict = _mapping(payload.get("verdict"))
        return {
            "title": "Supervisor - technical details",
            "note": (
                "The bounded fact sheet, the supervision identity, the report "
                "identity, the gate verdict, the persisted deadlines and the status "
                "history. Advisory only: nothing here is authority, and no "
                "credential or raw provider body is shown."
            ),
            "sections": [
                _popup_table(
                    "Supervision identity",
                    POPUP_FACT_COLUMNS,
                    self._supervision_identity_rows(payload, record),
                    empty="No supervision record exists for this attempt.",
                ),
                _popup_table(
                    "Report and attempt",
                    POPUP_FACT_COLUMNS,
                    self._supervision_report_rows(payload, context),
                    empty="No worker report has been read for this attempt.",
                ),
                _popup_table(
                    "Gate verdict and deadlines",
                    POPUP_FACT_COLUMNS,
                    self._supervision_gate_rows(payload, verdict),
                    empty="No supervision gate verdict is available.",
                ),
                _popup_table(
                    "Status history",
                    POPUP_HISTORY_COLUMNS,
                    self._supervision_history_rows(payload),
                    empty="No supervision history for this attempt.",
                ),
                _popup_text(
                    "Full evidence",
                    _lines_of(record.get("evidence")),
                    empty="No evidence was recorded with this supervision.",
                ),
                _popup_text(
                    "Full instruction",
                    [str(record.get("instruction_for_cline") or "")],
                    empty="No instruction was recorded with this supervision.",
                ),
                _popup_table(
                    "Runtime and polling",
                    POPUP_FACT_COLUMNS,
                    self._supervision_runtime_rows(payload, runtime),
                    empty="The supervision runtime is not reporting.",
                ),
            ],
        }

    def _supervision_identity_rows(
        self, payload: Mapping[str, Any], record: Mapping[str, Any]
    ) -> list[list[str]]:
        """The identity of one supervision: id, status, decision, timestamps."""
        if not record:
            return []
        return [
            ["Supervision id", str(record.get("supervision_id") or "-")],
            ["Status", str(record.get("status") or "-")],
            ["Action", str(record.get("action") or "-")],
            ["Risk", str(record.get("risk") or "-")],
            ["Requires human", _yes(record.get("requires_human"))],
            ["Decided by", str(record.get("decided_by") or "-")],
            ["Decision reason", str(record.get("decision_reason") or "-")],
            ["Reason", str(record.get("reason") or "-")],
            ["Created at", _stamp(record.get("created_at"))],
            ["Updated at", _stamp(record.get("updated_at"))],
            ["Waiting for", self.supervision_waiting_for()],
        ]

    def _supervision_report_rows(
        self, payload: Mapping[str, Any], context: Mapping[str, Any]
    ) -> list[list[str]]:
        """The report identity and the attempt it belongs to."""
        if not payload:
            return []
        return [
            ["Project", str(payload.get("project") or self.project_name())],
            ["Step", str(payload.get("step_no", "-"))],
            ["Attempt", str(payload.get("attempt", "-"))],
            [
                "Attempts",
                f"{payload.get('attempts_remaining', '-')} remaining of "
                f"{payload.get('max_attempts', '-')}",
            ],
            ["Worker state", str(payload.get("step_state") or "-")],
            ["Report hash (current)", str(payload.get("current_report_hash") or "-")],
            ["Report status", str(context.get("report_status") or "-")],
            ["Report summary", str(context.get("report_summary") or "-")],
            ["Architecture version", str(payload.get("architecture_version") or "-")],
            ["Provider", str(payload.get("provider") or "-")],
        ]

    def _supervision_gate_rows(
        self, payload: Mapping[str, Any], verdict: Mapping[str, Any]
    ) -> list[list[str]]:
        """The fail-closed gate verdict and the persisted malformed deadlines."""
        return [
            [
                "Gate",
                f"{'ALLOW' if verdict.get('allowed') else 'BLOCK'} | "
                f"{verdict.get('reason') or '-'}",
            ],
            [
                "Malformed deadlines",
                "stability "
                f"{payload.get('malformed_stability_seconds', '-')}s | "
                "absolute "
                f"{payload.get('malformed_timeout_seconds', '-')}s",
            ],
        ]

    def _supervision_runtime_rows(
        self, payload: Mapping[str, Any], runtime: Mapping[str, Any]
    ) -> list[list[str]]:
        """Runtime/poll details - the host's schedule, reported by the core."""
        polling = (
            "running"
            if runtime.get("running")
            else ("stopped" if runtime else "not reporting")
        )
        return [
            ["Supervision enabled", _yes(self.supervision_enabled())],
            ["Polling status", polling],
            ["Runtime state", str(runtime.get("state") or "-")],
            ["Runtime running", _yes(runtime.get("running"))],
            ["Recorded statuses", str(len(payload.get("history") or ()))],
        ]

    def judge_popup(self) -> dict[str, Any]:
        """The judge's full sanitized output - a separate layer, advisory only."""
        judge = _mapping((self._review or {}).get("judge"))
        used = bool(judge.get("consulted"))
        return {
            "title": "Judge Details",
            "note": (
                "The judge was consulted for an explicitly declared evidence "
                "conflict. Its decision explains that conflict and never replaces "
                "the deterministic verdict."
                if used
                else "The judge was not consulted for this review: no structurally "
                "valid evidence conflict was declared."
            ),
            "sections": [
                _popup_table(
                    "Judge",
                    POPUP_FACT_COLUMNS,
                    [
                        ["Available", _yes(judge.get("available"))],
                        ["Consulted", _yes(judge.get("consulted"))],
                        ["Status", str(judge.get("status") or "-")],
                        ["Conflicts answered", str(judge.get("count", 0))],
                        ["Reason", str(judge.get("reason") or "-")],
                    ],
                    empty="No judge result has been produced.",
                ),
                _popup_text(
                    "Full judge output",
                    self.judge_lines(),
                    empty="No judge output.",
                ),
            ],
        }

    def conflicts_popup(self) -> dict[str, Any]:
        """Every validated evidence conflict, with the judge's answer if any."""
        conflicts = [
            conflict
            for conflict in ((self._review or {}).get("conflicts") or ())
            if isinstance(conflict, Mapping)
        ]
        resolved = self._judge_status_by_target()
        rows = [
            [
                str(conflict.get("target", "")),
                ", ".join(
                    str(item) for item in (conflict.get("supporting_ids") or ())
                )
                or "-",
                ", ".join(
                    str(item) for item in (conflict.get("contradicting_ids") or ())
                )
                or "-",
                resolved.get(str(conflict.get("target", "")), "not judged"),
            ]
            for conflict in conflicts
        ]
        return {
            "title": "Evidence Conflicts",
            "note": (
                f"{len(rows)} validated conflict(s). A conflict is explicit "
                "metadata, never inferred from text, severity or overlap; the judge "
                "explains it and never decides."
            ),
            "sections": [
                _popup_table(
                    "Conflicts",
                    POPUP_CONFLICT_COLUMNS,
                    rows,
                    empty="Conflicts: 0",
                ),
                _popup_text(
                    "Judge output",
                    self.judge_lines(),
                    empty="No judge output.",
                ),
            ],
        }

    def _judge_status_by_target(self) -> dict[str, str]:
        """The judge's answer per conflict anchor, when it answered one."""
        judge = _mapping((self._review or {}).get("judge"))
        resolved: dict[str, str] = {}
        for judgment in judge.get("judgments") or ():
            if not isinstance(judgment, Mapping):
                continue
            conflict = _mapping(judgment.get("conflict"))
            decision = _mapping(judgment.get("decision"))
            target = str(conflict.get("target") or "")
            if target:
                resolved[target] = (
                    f"{decision.get('status') or '-'} "
                    f"({decision.get('id') or '-'})"
                )
        return resolved

    def advisor_popup(self, index: int) -> dict[str, Any]:
        """One advisor's complete read-only result - everything the pane omits.

        The credential is absent by construction: the panel data holds no key, no
        request header and no raw provider response body, so there is nothing here
        that could leak one.
        """
        panels = self.provider_panels()
        if not 0 <= index < len(panels):
            return {
                "title": "Advisor details",
                "note": "That advisor pane does not exist in this session.",
                "sections": [_popup_text("Details", (), empty="No advisor.")],
            }
        panel = panels[index]
        facts = [
            [str(row[0]), str(row[1])]
            for row in (panel.get("facts") or ())
            if len(tuple(row)) >= 2
        ]
        return {
            "title": f"{panel.get('name') or 'Advisor'} - details",
            "note": (
                "Read-only advisor result. No API key, no Authorization header and "
                "no raw provider response body is shown: the panel never receives "
                "any of them."
            ),
            "sections": [
                _popup_table(
                    "Advisor", POPUP_FACT_COLUMNS, facts, empty="Not run yet."
                ),
                _popup_text(
                    "Finding text",
                    [str(panel.get("response"))] if panel.get("response") else [],
                    empty="This advisor produced no finding text.",
                ),
                _popup_table(
                    "Evidence references",
                    POPUP_FACT_COLUMNS,
                    [
                        [f"Evidence {position + 1}", str(item)]
                        for position, item in enumerate(panel.get("evidence") or ())
                    ],
                    empty="No evidence reference was reported.",
                ),
                _popup_text(
                    "Result detail",
                    panel.get("body_lines") or (),
                    empty="No result detail.",
                ),
            ],
        }

    # -- the view model ----------------------------------------------------

    def banner(self) -> dict[str, Any]:
        """The health banner: OK, a failed action, or CRITICAL."""
        if self._critical:
            return {
                "critical": True,
                "text": (
                    "CRITICAL - the source of truth or an architecture "
                    "invariant failed. Mutations are disabled; use Refresh or "
                    "Reconnect. Last error: "
                    f"{_error_text(self._last_error)}"
                ),
            }
        if self._last_error is not None:
            return {
                "critical": False,
                "text": f"Last action failed: {_error_text(self._last_error)}",
            }
        return {"critical": False, "text": "Core reachable - no blocking error."}

    def monitor_rows(self) -> list[tuple[str, str]]:
        """Label/value rows for the Monitor tab - all from the projection."""
        payload = self._payload
        project = _mapping(payload.get("project"))
        health = _mapping(payload.get("health"))
        cost = _mapping(payload.get("cost"))
        following = payload.get("next_step")
        return [
            ("Project", str(project.get("name", "-"))),
            ("Mode", str(project.get("mode", "-"))),
            ("Project state", "paused" if self.is_paused() else "running"),
            (
                "Architecture version",
                str(payload.get("architecture_version") or "-"),
            ),
            ("Steps", str(health.get("step_count", 0))),
            ("Current step", _step_no(self.current_step())),
            ("Current state", str(self.current_state() or "-")),
            (
                "Next step",
                "-"
                if not isinstance(following, Mapping)
                else str(following.get("step_no", "-")),
            ),
            ("Complete", str(bool(health.get("complete")))),
            (
                "Blocking steps",
                _join_blocking(payload.get("blocking_steps")),
            ),
            ("Open risks", str(len(payload.get("open_risks") or ()))),
            (
                "Change requests",
                str(len(payload.get("change_requests") or ())),
            ),
            ("Cost (USD)", f"{_number(cost.get('total_usd')):.4f}"),
            ("Cost records", str(cost.get("record_count", 0))),
        ]

    def work_panel(self) -> dict[str, Any]:
        """The CURRENT WORK panel, resolved from the loop's own health."""
        current = self.current_step()
        following = self._payload.get("next_step")
        return {
            "no_steps": self.step_count() == 0,
            "plan": self._plan_line(),
            "step_no": _step_no(current),
            "title": "-" if current is None else str(current.get("title", "")),
            "phase": "-" if current is None else str(current.get("phase", "")),
            "state": "-" if current is None else str(current.get("state", "")),
            "attempt": (
                "-"
                if current is None
                else f"{current.get('attempt')}/{current.get('max_attempts')}"
            ),
            "next_step": (
                "-"
                if not isinstance(following, Mapping)
                else str(following.get("step_no", "-"))
            ),
            "stopped_because": self.stopped_because or "-",
            "channel": self._channel(current),
            "blocking": _join_blocking(self._payload.get("blocking_steps")),
        }

    def _channel(self, current: Optional[Mapping[str, Any]]) -> str:
        """The worker-channel marker of the current attempt, as one line."""
        if current is None:
            return "-"
        for task in self._payload.get("tasks") or ():
            if not isinstance(task, Mapping):
                continue
            if (
                task.get("step_no") == current.get("step_no")
                and task.get("attempt") == current.get("attempt")
            ):
                return (
                    f"{task.get('state', '?')} / report "
                    f"{task.get('report_status') or '-'}"
                )
        return "no task row for this attempt"

    def audit_rows(self) -> list[tuple[str, ...]]:
        """Newest-first audit rows for the read-only audit table."""
        rows: list[tuple[str, ...]] = []
        for entry in self._audit:
            detail = str(entry.get("detail", ""))
            if len(detail) > 120:
                detail = detail[:117] + "..."
            step_no = entry.get("step_no")
            rows.append(
                (
                    str(entry.get("created_at", "")).replace("T", " ")[:19],
                    f"{entry.get('entity_type', '')} "
                    f"{entry.get('entity_id', '')}".strip(),
                    str(entry.get("action", "")),
                    str(entry.get("event", "")),
                    str(entry.get("actor", "")),
                    "-" if step_no is None else str(step_no),
                    detail,
                )
            )
        return rows

    def risk_rows(self) -> list[tuple[str, str, str, str, str]]:
        """The open risks, as table rows."""
        rows: list[tuple[str, str, str, str, str]] = []
        for risk in self._payload.get("open_risks") or ():
            if not isinstance(risk, Mapping):
                continue
            rows.append(
                (
                    str(risk.get("id", "")),
                    str(risk.get("severity", "")),
                    str(risk.get("probability", "")),
                    str(risk.get("status", "")),
                    str(risk.get("description", "")),
                )
            )
        return rows

    def view_model(self) -> dict[str, Any]:
        """Everything the widgets need - plain data only."""
        project = _mapping(self._payload.get("project"))
        health = _mapping(self._payload.get("health"))
        buttons = {
            intent.key: {
                "label": intent.label,
                "group": intent.group,
                "enabled": self.enabled(intent),
                "human": intent.human,
                "reason_prompt": intent.reason_prompt,
                "confirmation": intent.confirmation,
            }
            for intent in INTENTS
        }
        return {
            "banner": self.banner(),
            "busy": self.is_busy,
            "critical": self._critical,
            "top": {
                "project": str(project.get("name", "-")),
                "mode": str(project.get("mode", "-")),
                "project_state": "paused" if self.is_paused() else "running",
                "architecture": str(
                    self._payload.get("architecture_version") or "-"
                ),
                "health": (
                    f"{health.get('step_count', 0)} steps | current "
                    f"{health.get('current_step_no') or '-'} | complete "
                    f"{bool(health.get('complete'))}"
                ),
            },
            "work": self.work_panel(),
            "buttons": buttons,
            "review": self.review_view(),
            "proposal": self.proposal_view(),
            "supervisor": self.supervisor_view(),
            "monitor": {"rows": self.monitor_rows()},
            "audit": {"rows": self.audit_rows()},
            "risks": {"rows": self.risk_rows()},
            "logs": {
                "rows": self.log_rows(),
                "count": self.log_count,
                "limit": self.log_limit,
            },
            "log": {"lines": self.log_lines()},
            "status": self._status,
            "last_action": self._last_action,
            "stopped_because": self.stopped_because,
            "actor": self.actor,
            "reports_dir": self.reports_dir,
        }


def _mapping(value: Any) -> Mapping[str, Any]:
    """A mapping or an empty one - never a surprise."""
    return value if isinstance(value, Mapping) else {}


def _pane(title: str, rows: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """One read-only pane: a title and label/value rows, all plain data."""
    return {
        "title": str(title),
        "rows": [[str(label), str(value or "-")] for label, value in rows],
    }


def _short(value: Any) -> str:
    """The first 16 characters of an identifier, or ``"-"`` when there is none."""
    text = "" if value is None else str(value)
    return text[:16] if text else "-"


def _brief(value: Any, limit: int = 180) -> str:
    """One compact, single-line excerpt for a pane's summary line."""
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 1)].rstrip() + "…"


def _yes(value: Any) -> str:
    """A tri-state boolean as the operator reads it."""
    if value is None:
        return "-"
    return "yes" if bool(value) else "no"


def _lines(value: Any) -> str:
    """A bounded list of strings as one multi-line cell."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return "-"
    items = [str(item) for item in value if str(item).strip()]
    return "\n".join(items) if items else "-"


def _records(value: Any, key: str, label: str) -> str:
    """A bounded list of records as ``key: label`` lines."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return "-"
    items: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        items.append(f"{item.get(key, '?')}: {item.get(label, '')}".strip())
    return "\n".join(items) if items else "-"


def _mapping_text(value: Any) -> str:
    """A mapping as ``key=value`` pairs - plain, never an object repr."""
    if not isinstance(value, Mapping) or not value:
        return "-"
    return " | ".join(f"{key}={value[key]}" for key in sorted(value))


def _status_text(status: str) -> str:
    """The operator-facing wording of one advisor execution status."""
    return {
        "FINDING": "finding returned",
        "ABSTAIN": "abstained (no finding)",
        "ERROR": "provider error",
        "": "not run yet",
    }.get(status, status or "not run yet")


def _status_level(status: str) -> str:
    """The colour bucket of one advisor status: a finding informs, the rest warn."""
    if status == "FINDING":
        return "INFO"
    if status == "ABSTAIN":
        return "WARN"
    if status == "ERROR":
        return "ERROR"
    return "INFO"


def _tokens_text(cost: Mapping[str, Any]) -> str:
    """The token usage of one cost record, or ``-`` when there is none."""
    if not cost or not cost.get("available"):
        return "-"
    return (
        f"{cost.get('input_tokens', 0)} in / {cost.get('output_tokens', 0)} out"
    )


def _provider_panel(stage: Mapping[str, Any]) -> dict[str, Any]:
    """One advisor pane, as plain data: status, full text, facts, evidence, reason.

    The pane exists so the operator can compare all three advisors side by side,
    so nothing about one provider is spread across the tab: its execution status,
    the finding text, the structured facts, the evidence references, the
    ABSTAIN/ERROR reason and its tokens and cost all live here.
    """
    source = str(stage.get("source", ""))
    status = str(stage.get("status", ""))
    finding = _mapping(stage.get("finding"))
    cost = _mapping(stage.get("cost"))
    reason = str(stage.get("reason") or "")
    evidence = [str(item) for item in (finding.get("evidence") or ())]
    response = str(finding.get("claim") or "") if finding else ""
    facts = [
        ["Execution status", _status_text(status)],
        ["Severity", str(finding.get("severity") or "-")],
        ["Confidence", str(finding.get("confidence") or "-")],
        ["Relation", str(stage.get("relation") or "-")],
        ["Anchor", str(stage.get("relation_target") or "-")],
        ["Step", str(finding.get("step_no") or "-")],
        ["Finding id", str(finding.get("id") or "-")],
        ["Evidence refs", str(len(evidence))],
        ["Tokens", _tokens_text(cost)],
        ["Cost", _money_text(cost)],
    ]
    if status == "FINDING":
        body = [response or "(the finding carries no text)", "", "Evidence refs:"]
        body.extend(f"  - {item}" for item in evidence)
        if not evidence:
            body.append("  - none reported")
    elif status == "ABSTAIN":
        body = [
            "The advisor abstained and produced no finding.",
            "",
            f"Reason: {reason or 'not reported'}",
        ]
    elif status == "ERROR":
        body = [
            "The advisor failed and produced no finding.",
            "",
            f"Reason: {reason or 'not reported'}",
        ]
    else:
        body = ["No result for this advisor in this session."]
    return {
        "configured": True,
        "source": source,
        "name": component_for(source) if source else "Advisor",
        "status": status,
        "status_text": _status_text(status),
        "level": _status_level(status),
        "severity": str(finding.get("severity") or "-"),
        "relation": str(stage.get("relation") or "-"),
        "anchor": str(stage.get("relation_target") or "-"),
        "summary": _brief(response),
        "reason": reason,
        "response": response,
        "evidence": evidence,
        "facts": facts,
        "body_lines": body,
    }


def _idle_panel(name: str, index: int) -> dict[str, Any]:
    """A pane for an advisor that is configured but has not answered (yet)."""
    title = name or f"Advisor {index + 1}"
    return {
        "configured": bool(name),
        "source": "",
        "name": title,
        "status": "",
        "status_text": "not run yet",
        "level": "INFO",
        "severity": "-",
        "relation": "-",
        "anchor": "-",
        "summary": "",
        "reason": "",
        "response": "",
        "evidence": [],
        "facts": [
            ["Execution status", "not run yet"],
            ["Severity", "-"],
            ["Confidence", "-"],
            ["Relation", "-"],
            ["Anchor", "-"],
            ["Step", "-"],
            ["Finding id", "-"],
            ["Evidence refs", "-"],
            ["Tokens", "-"],
            ["Cost", "-"],
        ],
        "body_lines": [
            (
                "This advisor is configured but has not answered:"
                if name
                else "No advisor is configured in this pane:"
            ),
            "",
            "run an architecture review to fill it - the review",
            "is advisory and read-only, and it never changes",
            "workflow state or the deterministic verdict.",
        ],
    }


def _log_entry(raw: Any) -> Optional[dict[str, Any]]:
    """One drained event, re-validated before it enters the view.

    A queue payload that is not a proper event is dropped rather than rendered:
    the panel would otherwise show a row it cannot trust. Returning ``None``
    keeps the caller honest - there is no silent placeholder entry.
    """
    if not isinstance(raw, Mapping):
        return None
    try:
        return build_event(**dict(raw))
    except (ValueError, TypeError):
        return None


def _stamp(value: Any) -> str:
    """A log timestamp as ``YYYY-MM-DD HH:MM:SS`` (or ``-``)."""
    text = str(value or "")
    if len(text) < 19:
        return "-" if not text else text
    return text.replace("T", " ")[:19]


def _step_no(step: Optional[Mapping[str, Any]]) -> str:
    """The step number of a step payload, or ``-``."""
    return "-" if step is None else str(step.get("step_no", "-"))


def _number(value: Any) -> float:
    """A float for display, defaulting to ``0.0``."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _money_text(cost: Mapping[str, Any]) -> str:
    """A cost cell: the amount, plus how much of it rests on a real price."""
    if not cost or not cost.get("available"):
        return "-"
    return (
        f"{_number(cost.get('total_usd')):.4f} USD | "
        f"{cost.get('priced_record_count', 0)} priced / "
        f"{cost.get('unpriced_record_count', 0)} unpriced | "
        f"{cost.get('input_tokens', 0)}+{cost.get('output_tokens', 0)} tokens"
    )


def _join_blocking(rows: Any) -> str:
    """The blocking steps as one compact line."""
    if not rows:
        return "-"
    parts = [
        f"{row.get('step_no')}:{row.get('state')}"
        for row in rows
        if isinstance(row, Mapping)
    ]
    return ", ".join(parts) or "-"


def _popup_section(
    title: str,
    *,
    columns: Sequence[Sequence[Any]] = (),
    rows: Sequence[Sequence[Any]] = (),
    lines: Sequence[Any] = (),
    empty: str = "",
) -> dict[str, Any]:
    """One popup section: a table when ``columns`` is set, else a text block.

    The shape is fixed and always plain data - a popup is a *spec*, never a
    widget - and every cell is a string, so the payload survives a JSON round trip
    and can be asserted without a display. A section whose lines are all blank
    counts as *no* lines, so a single empty string never replaces the section's
    documented empty state with a blank pane.
    """
    cleaned_lines = [str(line) for line in lines]
    if not any(line.strip() for line in cleaned_lines):
        cleaned_lines = []
    return {
        "title": str(title),
        "columns": [list(column) for column in columns],
        "rows": [[str(cell) for cell in row] for row in rows],
        "lines": cleaned_lines,
        "empty": str(empty),
    }


def _popup_table(
    title: str,
    columns: Sequence[Sequence[Any]],
    rows: Sequence[Sequence[Any]],
    *,
    empty: str = "No entries.",
) -> dict[str, Any]:
    """A popup table section, with a documented empty state."""
    return _popup_section(title, columns=columns, rows=rows, empty=empty)


def _popup_text(
    title: str,
    lines: Sequence[Any],
    *,
    empty: str = "Nothing to show.",
) -> dict[str, Any]:
    """A popup text section, with a documented empty state."""
    return _popup_section(title, lines=lines, empty=empty)


def _lines_of(value: Any) -> list[str]:
    """A list of non-empty strings from a list, or ``[]`` when there is none."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return []
    return [str(item) for item in value if str(item).strip()]


def _reason_of(detail: str) -> str:
    """The ``reason`` recorded inside an audit detail payload, or ``-``.

    The detail is the JSON string the audit trail itself stores, so this reads
    exactly what was recorded - it never guesses a reason that was not written.
    """
    try:
        payload = json.loads(detail)
    except (TypeError, ValueError):
        return "-"
    if not isinstance(payload, Mapping):
        return "-"
    reason = payload.get("reason")
    return str(reason) if isinstance(reason, str) and reason.strip() else "-"


def _error_text(report: Any) -> str:
    """A one-line description of an error report (or ``None``)."""
    if report is None:
        return "-"
    return (
        f"{getattr(report, 'error_name', 'Error')}: "
        f"{getattr(report, 'message', '')}"
    )
