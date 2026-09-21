"""The core-thread worker: the only owner of the composition.

Two facts shape this module:

* a Tk mainloop must never be blocked, so core work runs off the UI thread;
* a SQLite connection may only be used by the thread that created it, so the
  :class:`~architecture_assistant.composition.Composition` is created **and**
  used by exactly one dedicated worker thread.

Therefore :class:`CoreWorker` is a synchronous facade over one composition -
whoever calls it must be that thread - and :class:`BackgroundRunner` owns the
thread, the input queue and the result queue. Nothing that crosses the queue to
the UI is a core object: every payload is plain ``dict`` / ``list`` / ``str`` /
``bool`` / ``int`` data, so the Tk thread can never reach the database, a
repository or the Monitor.

The runner polls nothing and starts no work by itself: the UI drains finished
results with ``root.after()`` and every action is an explicit operator command.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from architecture_assistant.composition import (
    EVENT_LEVEL_ERROR,
    EVENT_LEVEL_INFO,
    EVENT_LEVEL_WARN,
    Composition,
    CompositionConfig,
    build_event,
    component_for,
    compose,
    exception_reason,
)

__all__ = [
    "AUDIT_TAIL_LIMIT",
    "EVENT_LIMIT",
    "PROGRESS_LIMIT",
    "BackgroundRunner",
    "CoreError",
    "CoreWorker",
    "ErrorReport",
    "JobResult",
    "describe_error",
    "is_critical",
]

#: How many audit entries the audit panel reads by default.
AUDIT_TAIL_LIMIT = 200

#: How many progress lines one core action may buffer for the UI.
PROGRESS_LIMIT = 64

#: How many log events the worker may buffer before the UI drains them. The panel
#: keeps its own, larger, view; this bound only stops a stalled UI from growing
#: the queue without limit.
EVENT_LIMIT = 400

#: Exception class names that mean "the source of truth or an architecture
#: invariant cannot be trusted for this session". They are matched by *name*
#: on purpose: the GUI must not import the assistant's error hierarchies, and a
#: renamed class must not silently degrade the classification.
_CRITICAL_ERROR_NAMES: frozenset[str] = frozenset(
    {
        # architecture invariants
        "RealizationControlError",
        "LoopInvariantError",
        "BootstrapConflictError",
        "PlanInvariantError",
        "ArchitectureEvolutionError",
        "ArchitectureEvolutionConflictError",
        "ArchitectureEvolutionNotApprovedError",
        "ArchitectureEvolutionNotFoundError",
        # reporting invariants
        "ReportingInvariantError",
        "ReportingError",
        "ProjectMissingError",
        # the database itself
        "DatabaseError",
        "OperationalError",
        "IntegrityError",
        "ProgrammingError",
        "InterfaceError",
        "DataError",
        "InternalError",
        "NotSupportedError",
    }
)


class CoreError(RuntimeError):
    """Raised by the worker when it cannot serve a request at all."""


@dataclass(frozen=True)
class ErrorReport:
    """A failure that crossed from the core to the UI - as plain data."""

    error_name: str
    message: str
    critical: bool

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "error_name": self.error_name,
            "message": self.message,
            "critical": self.critical,
        }


def is_critical(error: BaseException) -> bool:
    """Whether a failure must disable every mutation for this session."""
    return type(error).__name__ in _CRITICAL_ERROR_NAMES


def describe_error(error: BaseException) -> ErrorReport:
    """Turn an exception into a plain, display-ready report."""
    return ErrorReport(
        error_name=type(error).__name__,
        message=str(error) or type(error).__name__,
        critical=is_critical(error),
    )


@dataclass(frozen=True)
class JobResult:
    """One finished background job, delivered to the UI thread."""

    label: str
    payload: Any = None
    error: Optional[ErrorReport] = None

    @property
    def ok(self) -> bool:
        """Whether the job finished without raising."""
        return self.error is None


class CoreWorker:
    """Synchronous facade over one composition, owned by one thread.

    Every public method returns plain data. No composition, repository, Monitor
    or SQLite handle is ever returned to a caller, and the worker resolves the
    *current* step itself before a human action, so a stale UI can never target
    another step.
    """

    def __init__(
        self,
        config: CompositionConfig,
        *,
        audit_tail: int = AUDIT_TAIL_LIMIT,
    ) -> None:
        if not isinstance(config, CompositionConfig):
            raise ValueError(
                f"config must be a CompositionConfig; got {config!r}"
            )
        if (
            isinstance(audit_tail, bool)
            or not isinstance(audit_tail, int)
            or audit_tail < 1
        ):
            raise ValueError(
                f"audit_tail must be an int >= 1; got {audit_tail!r}"
            )
        self._config = config
        self._audit_tail = audit_tail
        self._composition: Optional[Composition] = None
        #: Progress notes for the UI: a thread-safe queue only - never state, and
        #: never a reason to touch the composition from another thread.
        self._progress: "queue.Queue[str]" = queue.Queue()
        #: Log events for the Logs tab: plain, validated dictionaries in one
        #: thread-safe queue. The sequence number is assigned here, under the lock,
        #: so the drained order is the order in which events actually happened.
        self._events: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self._event_lock = threading.Lock()
        self._event_seq = 0
        #: The last advisory review, held as the *object* the core produced. A
        #: review is ephemeral (it is written nowhere), so this is the only place
        #: a proposal can get its evidence from - and it never crosses to the Tk
        #: thread, which only ever receives plain dictionaries.
        self._last_review: Optional[Any] = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def config(self) -> CompositionConfig:
        """The configuration this worker composes with."""
        return self._config

    @property
    def is_open(self) -> bool:
        """Whether a composition is currently held."""
        return self._composition is not None

    def open(self) -> None:
        """Compose the assistant (idempotent) in the calling thread."""
        if self._composition is None:
            self._composition = compose(self._config)

    def close(self) -> None:
        """Release the composition and its connection."""
        composition = self._composition
        self._composition = None
        if composition is not None:
            composition.close()

    def reconnect(self) -> None:
        """Close and compose again - never a repair, never a state change."""
        self.close()
        self.open()

    def _core(self) -> Composition:
        if self._composition is None:
            raise CoreError(
                "the core worker is not open; compose it before calling"
            )
        return self._composition

    # -- read-only payloads ------------------------------------------------

    def payload(self) -> dict[str, Any]:
        """One fresh read of the canonical projection plus its selectors.

        The projection comes from the Monitor (which owns the canonical
        ``ReportSnapshot``); the extra selectors are the Monitor's own methods,
        so no second query layer exists anywhere in the GUI.
        """
        composition = self._core()
        monitor = composition.monitor
        canonical = monitor.to_dict()
        current = monitor.current_step()
        following = monitor.next_step()
        return {
            "canonical": canonical,
            "project": canonical["project"],
            "health": canonical["health"],
            "steps": canonical["steps"],
            "tasks": canonical["tasks"],
            "cost": canonical["cost"],
            "architecture": canonical["architecture"],
            "architecture_version": monitor.architecture_version(),
            "current_step": None if current is None else current.to_dict(),
            "next_step": None if following is None else following.to_dict(),
            "blocking_steps": [
                {"step_no": step.step_no, "state": step.state.value}
                for step in monitor.blocking_steps()
            ],
            "open_risks": [risk.to_dict() for risk in monitor.open_risks()],
            "change_requests": canonical["change_requests"],
            #: Which advisors are wired, in their declared order - read-only
            #: configuration, so the review tab can label its panes before the
            #: operator has paid for a review.
            "reviewers": [
                component_for(reviewer.source)
                for reviewer in composition.architecture_review.reviewers
            ],
            "paths": {
                "database_path": str(composition.config.database_path),
                "exchange_dir": str(composition.config.exchange_dir),
                "report_dir": str(composition.config.report_dir),
                "source_root": str(composition.config.source_root),
            },
        }

    def audit_tail(self, limit: Optional[int] = None) -> list[dict[str, Any]]:
        """The most recent audit entries, newest first - strictly read-only.

        This is the one place the GUI reads a repository, and it only ever reads:
        the append-only audit trail has no update or delete in the adapter, and
        the worker exposes no write path to it. The returned rows are plain
        dictionaries, so no repository object reaches the UI.
        """
        composition = self._core()
        resolved = self._audit_tail if limit is None else limit
        if (
            isinstance(resolved, bool)
            or not isinstance(resolved, int)
            or resolved < 1
        ):
            raise CoreError(f"limit must be an int >= 1; got {resolved!r}")
        entries = composition.storage.audit.list()
        rows: list[dict[str, Any]] = []
        for entry in reversed(entries[-resolved:]):
            detail = entry.detail
            raw_step = detail.get("step_no")
            if raw_step is None and entry.entity_type.value == "STEP":
                raw_step = entry.entity_id
            try:
                step_no = None if raw_step is None else int(raw_step)
            except (TypeError, ValueError):
                step_no = None
            rows.append(
                {
                    "created_at": entry.created_at.isoformat(),
                    "entity_type": entry.entity_type.value,
                    "entity_id": entry.entity_id,
                    "action": entry.action.value,
                    "event": str(detail.get("event", "")),
                    "actor": str(detail.get("actor", "")),
                    "step_no": step_no,
                    "detail": entry.detail_json(),
                }
            )
        return rows

    # -- the loop and the human actions ------------------------------------

    def run_until_idle(self) -> dict[str, Any]:
        """Advance the loop until it must wait - one explicit operator command."""
        return self._core().run_until_idle().to_dict()

    def pause_project(self, *, actor: str, reason: str) -> dict[str, Any]:
        """Pause the project through the human override - audited by the core."""
        project = self._core().human_override.pause_project(
            actor=actor, reason=reason
        )
        return {"action": "pause_project", "paused": bool(project.paused)}

    def resume_project(self, *, actor: str, reason: str) -> dict[str, Any]:
        """Resume the project through the human override - audited by the core."""
        project = self._core().human_override.resume_project(
            actor=actor, reason=reason
        )
        return {"action": "resume_project", "paused": bool(project.paused)}

    def approve(self, *, actor: str, reason: str) -> dict[str, Any]:
        """Approve the current step's awaited approval."""
        step_no = self._current_step_number()
        step = self._core().approval.approve(
            step_no, actor=actor, reason=reason
        )
        return _step_result("approve", step)

    def reject(self, *, actor: str, reason: str) -> dict[str, Any]:
        """Reject the current step's awaited approval."""
        step_no = self._current_step_number()
        step = self._core().approval.reject(
            step_no, actor=actor, reason=reason
        )
        return _step_result("reject", step)

    def unblock(self, *, actor: str, reason: str) -> dict[str, Any]:
        """Unblock the current blocked step."""
        step_no = self._current_step_number()
        step = self._core().human_override.unblock_step(
            step_no, actor=actor, reason=reason
        )
        return _step_result("unblock", step)

    def resolve(self, *, actor: str, reason: str) -> dict[str, Any]:
        """Resolve the current conflicting step."""
        step_no = self._current_step_number()
        step = self._core().human_override.resolve_step(
            step_no, actor=actor, reason=reason
        )
        return _step_result("resolve", step)

    def abort(self, *, actor: str, reason: str) -> dict[str, Any]:
        """Abort the current step, when its persisted state allows it."""
        step_no = self._current_step_number()
        step = self._core().human_override.abort_step(
            step_no, actor=actor, reason=reason
        )
        return _step_result("abort", step)

    # -- plan loading ------------------------------------------------------

    def preview_plan(self, text: str, *, source_file: str = "") -> dict[str, Any]:
        """Validate a plan file and answer how it would be imported.

        Reads only, and never fails for a plan *problem*: the loader reports its
        issues as data, so the panel can show every one of them at once instead of
        one error at a time. A broken source of truth still raises.
        """
        preview = self._core().plan_loader.preview(text)
        payload = preview.to_dict()
        payload["source_file"] = str(source_file)
        return payload

    def import_plan(
        self,
        text: str,
        *,
        actor: str,
        reason: str,
        source_file: str = "",
    ) -> dict[str, Any]:
        """Import the plan through the core use-case, then answer with plain data."""
        result = self._core().plan_loader.import_plan(
            text, actor=actor, reason=reason, source_file=source_file
        )
        return result.to_dict()

    # -- architecture review ------------------------------------------------

    def run_architecture_review(self, question: str) -> dict[str, Any]:
        """Run one advisory architecture review and answer with plain data.

        The question is the operator's own and travels **verbatim** to every
        advisor - one question, one consultation, no conversation. The review is
        read-only: it persists nothing, changes no workflow state, cannot reach
        ``VERIFIED`` and never overrides the deterministic gate. Every provider
        call happens here, on the core thread.

        The review reports every stage as a log event, and this method guarantees
        the two boundaries of that log: the run is announced before anything is
        read, and a failure is recorded - with the exception **type name** only -
        before it is handed on. A review whose monitor read itself fails is
        therefore still visible in the panel.
        """
        composition = self._core()
        step_no: Optional[int] = None
        # announced before anything is read, so even a failure of the very first
        # read leaves a start marker next to its error
        self.record_event(
            EVENT_LEVEL_INFO,
            "CoreWorker",
            "review-start",
            "architecture review requested by the operator",
        )
        try:
            snapshot = composition.monitor.snapshot()
            step_no = snapshot.health.current_step_no
            result = composition.architecture_review.review(
                snapshot,
                question=question,
                on_event=self.forward_event,
            )
        except BaseException as error:  # logged, then reported, never swallowed
            self.record_event(
                EVENT_LEVEL_ERROR,
                "CoreWorker",
                "review-error",
                f"architecture review failed: {exception_reason(error)}",
                step_no=step_no,
            )
            raise
        self._last_review = result
        return result.to_dict()

    # -- the managed-project architecture proposal -------------------------

    def proposal_board(self) -> dict[str, Any]:
        """The proposal board as plain data - the read-only half of the tab.

        ``review_available`` is what decides whether a proposal can be generated
        at all: the review is the evidence, and a missing one is reported instead
        of being invented.
        """
        composition = self._core()
        proposals = composition.architecture_synthesis.proposals()
        latest = None
        if proposals:
            latest = max(
                proposals,
                key=lambda item: (
                    item.created_at,
                    item.revision_no,
                    item.proposal_id,
                ),
            )
        return {
            "review_available": self._last_review is not None,
            "synthesizer": composition.architecture_synthesis.has_synthesizer,
            "count": len(proposals),
            "proposals": [
                proposal.to_dict() for proposal in reversed(proposals)
            ],
            "latest": None if latest is None else latest.to_dict(),
        }

    def synthesize_proposal(
        self,
        requirement: str = "",
        *,
        actor: str,
        reason: str,
        revision_of: Optional[str] = None,
    ) -> dict[str, Any]:
        """Generate one ``DRAFT`` proposal from the last review - never automatic.

        Everything happens here, on the core thread: the review is consumed, the
        optional synthesizer runs, and exactly one proposal row plus exactly one
        audit entry are written in one transaction. The panel never touches the
        proposal repository, the transaction boundary or a synthesizer.
        """
        composition = self._core()
        review = self._last_review
        if review is None:
            raise CoreError(
                "no architecture review has been run in this session; a "
                "proposal needs one review as its evidence"
            )
        try:
            proposal = composition.architecture_synthesis.synthesize(
                review,
                requirement=requirement,
                actor=actor,
                reason=reason,
                revision_of=revision_of,
                on_event=self.forward_event,
            )
        except BaseException as error:  # logged, then reported, never swallowed
            self.record_event(
                EVENT_LEVEL_ERROR,
                "Synthesis",
                "error",
                "architecture proposal generation failed: "
                f"{exception_reason(error)}",
            )
            raise
        return {
            "action": "synthesize_proposal",
            "board": self.proposal_board(),
            "proposal": proposal.to_dict(),
        }

    def approve_proposal(
        self, proposal_id: str, *, actor: str, reason: str
    ) -> dict[str, Any]:
        """Approve one managed-project proposal - the human decision, audited."""
        composition = self._core()
        self.record_event(
            EVENT_LEVEL_INFO,
            "Synthesis",
            "proposal-approval-requested",
            f"approval requested for proposal {proposal_id}",
        )
        proposal = composition.proposal_approval.approve(
            proposal_id, actor=actor, reason=reason
        )
        self.record_event(
            EVENT_LEVEL_INFO,
            "Synthesis",
            "proposal-approved",
            f"proposal {proposal_id} approved for {proposal.project}",
        )
        return self._proposal_decision("approve_proposal", proposal)

    def reject_proposal(
        self, proposal_id: str, *, actor: str, reason: str
    ) -> dict[str, Any]:
        """Reject one managed-project proposal - the human decision, audited."""
        composition = self._core()
        proposal = composition.proposal_approval.reject(
            proposal_id, actor=actor, reason=reason
        )
        self.record_event(
            EVENT_LEVEL_INFO,
            "Synthesis",
            "proposal-rejected",
            f"proposal {proposal_id} rejected for {proposal.project}",
        )
        return self._proposal_decision("reject_proposal", proposal)

    def request_proposal_revision(
        self,
        proposal_id: str,
        *,
        actor: str,
        reason: str,
        feedback: str,
    ) -> dict[str, Any]:
        """Ask for a revision - explicit, and it never re-synthesizes by itself."""
        composition = self._core()
        proposal = composition.proposal_approval.request_revision(
            proposal_id, actor=actor, reason=reason, feedback=feedback
        )
        self.record_event(
            EVENT_LEVEL_INFO,
            "Synthesis",
            "revision-requested",
            f"revision requested for proposal {proposal_id}",
        )
        return self._proposal_decision("request_proposal_revision", proposal)

    def _proposal_decision(
        self, action: str, proposal: Any
    ) -> dict[str, Any]:
        """One decision result - plain data plus a fresh board read."""
        return {
            "action": action,
            "proposal_id": proposal.proposal_id,
            "project": proposal.project,
            "status": proposal.status.value,
            "decided_by": proposal.decided_by,
            "decided_at": (
                None
                if proposal.decided_at is None
                else proposal.decided_at.isoformat()
            ),
            "board": self.proposal_board(),
            "proposal": proposal.to_dict(),
        }

    # -- the log-event queue -----------------------------------------------

    def record_event(
        self,
        level: str,
        component: str,
        action: str,
        message: str,
        *,
        step_no: Optional[int] = None,
        review_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Build and queue one log event - the strict producer.

        Strict on purpose: an entry the panel could not render, or could not
        trust, is a defect and raises here instead of being queued. The message
        is sanitized by the shared contract, so no credential, header or provider
        body can reach the queue through this door either.
        """
        entry = build_event(
            level=level,
            component=component,
            action=action,
            message=message,
            step_no=step_no,
            review_id=review_id,
            timestamp=self._config.clock(),
        )
        return self._queue_event(entry)

    def forward_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """Queue an event the core built - re-validated, never trusted blindly.

        The review hands its events over through this method, so every field is
        re-checked (and re-sanitized) at the boundary while the core's own
        timestamp is kept: the moment the event happened does not move.
        """
        if not isinstance(event, Mapping):
            raise ValueError(f"event must be a mapping; got {event!r}")
        entry = build_event(**dict(event))
        return self._queue_event(entry)

    def _queue_event(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Stamp the sequence number and enqueue - the order is decided here.

        The stamp and the ``put`` happen inside one lock, so the queue order and
        the sequence order are the same thing: two producers (the core thread and
        the Tk thread) can therefore never interleave the two.
        """
        with self._event_lock:
            self._event_seq += 1
            stamped = {**entry, "seq": self._event_seq}
            if self._events.qsize() >= EVENT_LIMIT:
                try:
                    self._events.get_nowait()
                except queue.Empty:  # pragma: no cover - drained concurrently
                    pass
            self._events.put(stamped)
        # keep the simple progress line alive, derived from the same event
        self.note_progress(f"{stamped['component']}: {stamped['action']}")
        return stamped

    def events(self) -> list[dict[str, Any]]:
        """Drain the buffered log events - a queue read and nothing else."""
        drained: list[dict[str, Any]] = []
        while True:
            try:
                drained.append(self._events.get_nowait())
            except queue.Empty:
                return drained

    # -- progress (queue only, never the composition) -----------------------

    def note_progress(self, message: str) -> None:
        """Buffer one short progress line for the UI thread."""
        if self._progress.qsize() >= PROGRESS_LIMIT:
            try:
                self._progress.get_nowait()
            except queue.Empty:  # pragma: no cover - drained concurrently
                pass
        self._progress.put(str(message))

    def progress(self) -> list[str]:
        """Drain the buffered progress lines - a queue read and nothing else."""
        lines: list[str] = []
        while True:
            try:
                lines.append(self._progress.get_nowait())
            except queue.Empty:
                return lines

    # -- artifacts ---------------------------------------------------------

    def export_markdown(self) -> dict[str, Any]:
        """Render the markdown artifact through the existing reporting adapter."""
        return self._export("markdown")

    def export_excel(self) -> dict[str, Any]:
        """Render the workbook through the existing reporting adapter."""
        return self._export("excel")

    def _export(self, kind: str) -> dict[str, Any]:
        composition = self._core()
        payload = composition.report_builder.build().to_dict()
        renderer = (
            composition.markdown_reporting
            if kind == "markdown"
            else composition.excel_reporting
        )
        locator = renderer.render(payload)
        return {
            "action": f"export_{kind}",
            "path": str(locator),
            "report_dir": str(composition.config.report_dir),
        }

    def _current_step_number(self) -> int:
        """The loop's own current step, resolved inside the core thread."""
        step = self._core().orchestrator.current_step()
        if step is None:
            raise CoreError("there is no current step to act on")
        return step.step_no

    # -- advisory supervision (Step 28 Phase 1) ----------------------------

    def _supervision_target(self) -> tuple[int, int]:
        """The current step/attempt supervision would act on, or fail closed."""
        step = self._core().orchestrator.current_step()
        if step is None:
            raise CoreError("there is no current step to supervise")
        return step.step_no, step.attempt

    def supervisor_status(self) -> dict[str, Any]:
        """The Supervisor tab as plain data - one read, no provider call.

        The runtime's counters are process-local, and the supervision payload is
        persisted state plus the bounded fact sheet the *next* analysis would be
        handed. Neither is a core object: the Tk thread only ever receives
        dictionaries, lists, strings and numbers.
        """
        composition = self._core()
        status: dict[str, Any] = {
            "enabled": composition.config.supervision,
            "runtime": composition.supervisor_runtime.status(),
            "supervision": None,
            "reason": "",
        }
        step = composition.orchestrator.current_step()
        if step is None:
            status["reason"] = "there is no current step to supervise"
            return status
        status["supervision"] = composition.supervision.payload(
            step.step_no, step.attempt
        )
        return status

    def analyze_report(self) -> dict[str, Any]:
        """Analyse the current report once, on the core thread.

        Idempotent by identity: a report that already has a decided record is
        reported as ``already-supervised`` instead of being analysed again, so a
        double click cannot produce a second analysis.
        """
        composition = self._core()
        step_no, attempt = self._supervision_target()
        self.record_event(
            EVENT_LEVEL_INFO,
            "Supervisor",
            "analysis-requested",
            f"supervision analysis requested for step {step_no}",
            step_no=step_no,
        )
        analysis = composition.supervision.analyze(
            step_no, attempt, on_event=self.forward_event
        )
        return {
            "action": "analyze_report",
            **self._supervision_result(composition, step_no, attempt),
            "analysis": analysis.to_dict(),
        }

    def approve_and_send(
        self, instruction: str = "", *, actor: str, reason: str
    ) -> dict[str, Any]:
        """Approve the waiting directive and publish it - the human decision.

        The exact supervision id is resolved here, on the core thread, from the
        current report identity: a stale panel can therefore never approve a
        directive for a report the workflow has moved past.
        """
        composition = self._core()
        step_no, attempt = self._supervision_target()
        supervision_id = self._supervision_id_for(composition, step_no, attempt)
        record = composition.supervision.approve_and_send(
            supervision_id,
            actor=actor,
            reason=reason,
            instruction=instruction,
            on_event=self.forward_event,
        )
        return {
            "action": "approve_and_send",
            **self._supervision_result(composition, step_no, attempt),
            "record": record.to_dict(),
        }

    def reject_directive(self, *, actor: str, reason: str) -> dict[str, Any]:
        """Reject the directive for this exact report, allowing review."""
        return self._supervision_decision(
            "reject_directive",
            lambda supervision, supervision_id: supervision.reject_directive(
                supervision_id,
                actor=actor,
                reason=reason,
                on_event=self.forward_event,
            ),
        )

    def waive_supervision(self, *, actor: str, reason: str) -> dict[str, Any]:
        """Waive supervision for this exact report, allowing review."""
        return self._supervision_decision(
            "waive_supervision",
            lambda supervision, supervision_id: supervision.waive_supervision(
                supervision_id,
                actor=actor,
                reason=reason,
                on_event=self.forward_event,
            ),
        )

    def escalate_supervision(self, *, actor: str, reason: str) -> dict[str, Any]:
        """Escalate the record to a human decision."""
        return self._supervision_decision(
            "escalate_supervision",
            lambda supervision, supervision_id: supervision.escalate(
                supervision_id,
                actor=actor,
                reason=reason,
                on_event=self.forward_event,
            ),
        )

    def supervisor_tick(self) -> dict[str, Any]:
        """One supervision tick on the core thread (the runtime owns no thread).

        ``start`` is idempotent, so the first tick starts the runtime - which
        reconciles whatever a previous process left half-done - and every later
        call is a plain tick. Deliberately no monitor projection is rebuilt here:
        a two-second tick must not cost a full snapshot.
        """
        composition = self._core()
        tick = composition.supervisor_runtime.start(on_event=self.forward_event)
        return {
            "action": "supervisor_tick",
            "result": tick.to_dict(),
            "supervisor": self.supervisor_status(),
        }

    def _supervision_decision(self, action: str, decide: Any) -> dict[str, Any]:
        """One human supervision decision on the current report identity."""
        composition = self._core()
        step_no, attempt = self._supervision_target()
        supervision_id = self._supervision_id_for(composition, step_no, attempt)
        record = decide(composition.supervision, supervision_id)
        return {
            "action": action,
            **self._supervision_result(composition, step_no, attempt),
            "record": record.to_dict(),
        }

    def _supervision_result(
        self, composition: Composition, step_no: int, attempt: int
    ) -> dict[str, Any]:
        """The fresh read that follows every supervision action."""
        return {
            "payload": self.payload(),
            "audit": self.audit_tail(),
            "supervisor": self.supervisor_status(),
            "step_no": step_no,
            "attempt": attempt,
        }

    def _supervision_id_for(
        self, composition: Composition, step_no: int, attempt: int
    ) -> str:
        """The supervision id of the **current** report, or fail closed.

        A disabled session is refused here, with the reason the panel shows,
        instead of the misleading "analyze it first": there is nothing to analyse
        while supervision is off, and the use-case refuses the decision anyway.
        """
        if not composition.supervision.enabled:
            raise CoreError(
                "supervision is disabled for this session: there is nothing to "
                "approve, reject, waive or escalate"
            )
        record = composition.supervision.current(step_no, attempt)
        if record is None:
            raise CoreError(
                "the current report has no supervision record yet; analyze it "
                "first"
            )
        return record.supervision_id




def _step_result(action: str, step: Any) -> dict[str, Any]:
    """Describe a human step action as plain data."""
    return {
        "action": action,
        "step_no": step.step_no,
        "state": step.state.value,
        "attempt": step.attempt,
        "max_attempts": step.max_attempts,
    }


class BackgroundRunner:
    """Runs every core action on one dedicated thread.

    The Tk mainloop must stay responsive, and the SQLite connection must stay
    with the thread that created it, so this class owns the single core thread,
    the job queue and the result queue:

    * :meth:`submit` is the only way to reach the worker (one job at a time);
    * :meth:`poll` is the only way results travel back, and the UI drains it
      from ``root.after()`` - which polls the *queue*, never the workflow;
    * the composition is composed on that thread and closed there on
      :meth:`stop`.
    """

    #: Sentinel that ends the core thread's loop.
    _STOP: Optional[Any] = None

    def __init__(
        self,
        worker: CoreWorker,
        *,
        name: str = "architecture-assistant-core",
    ) -> None:
        if not hasattr(worker, "open"):
            raise ValueError(
                f"worker must expose the CoreWorker surface; got {worker!r}"
            )
        self._worker = worker
        self._name = name
        self._jobs: "queue.Queue[Any]" = queue.Queue()
        self._results: "queue.Queue[JobResult]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._pending = 0
        self._closed = False

    @property
    def worker(self) -> CoreWorker:
        """The worker whose thread this runner owns."""
        return self._worker

    @property
    def is_busy(self) -> bool:
        """Whether a core action is queued or running right now."""
        with self._lock:
            return self._pending > 0

    @property
    def is_running(self) -> bool:
        """Whether the core thread is alive."""
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        """Start the core thread and compose the assistant on it (idempotent)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name=self._name, daemon=True
        )
        self._thread.start()
        self.submit("open", lambda worker: worker.open())

    def submit(
        self, label: str, action: Any
    ) -> bool:
        """Queue one core action; ``False`` when busy or already stopped."""
        if not isinstance(label, str) or not label.strip():
            raise ValueError(f"label must be a non-empty string; got {label!r}")
        if not callable(action):
            raise ValueError(f"action must be callable; got {action!r}")
        with self._lock:
            if self._closed or self._pending > 0:
                return False
            self._pending = 1
        self._jobs.put((label, action))
        return True

    def poll(self) -> list[JobResult]:
        """Drain every finished job - non-blocking, UI thread only."""
        results: list[JobResult] = []
        while True:
            try:
                results.append(self._results.get_nowait())
            except queue.Empty:
                return results

    def progress(self) -> list[str]:
        """Drain the core's progress lines - a queue read, never the composition.

        The worker owns the queue and this method only forwards it, exactly like
        :meth:`poll` forwards the result queue: the Tk thread reads a thread-safe
        queue and nothing else.
        """
        return self._worker.progress()

    def events(self) -> list[dict[str, Any]]:
        """Drain the core's log events - plain, validated dictionaries.

        Same rule as :meth:`poll` and :meth:`progress`: the Tk thread only ever
        reads a thread-safe queue, so it can render the log without holding a
        core object, a repository or a provider.
        """
        return self._worker.events()

    def stop(self, timeout: float = 10.0) -> None:
        """End the core thread, closing the composition on that thread."""
        with self._lock:
            self._closed = True
            thread = self._thread
        if thread is None:
            return
        self._jobs.put(self._STOP)
        thread.join(timeout)

    # -- the core thread ---------------------------------------------------

    def _run(self) -> None:
        """Own the composition for the whole life of this thread."""
        while True:
            job = self._jobs.get()
            if job is self._STOP:
                break
            label, action = job
            try:
                payload = action(self._worker)
            except BaseException as error:  # noqa: BLE001 - reported, not raised
                self._results.put(
                    JobResult(label=label, error=describe_error(error))
                )
            else:
                self._results.put(JobResult(label=label, payload=payload))
            finally:
                with self._lock:
                    self._pending = 0
        try:
            self._worker.close()
        except BaseException as error:  # noqa: BLE001 - reported, not raised
            self._results.put(
                JobResult(label="close", error=describe_error(error))
            )
