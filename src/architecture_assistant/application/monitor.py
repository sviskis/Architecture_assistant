"""Read-only project monitor - a pull-based view of the canonical projection.

The monitor answers exactly one question: *what does the project look like right
now?* It is a thin, deterministic **selector** layer over
:class:`~architecture_assistant.application.reporting.ReportSnapshot`: the
projection stays the single read access to the source of truth, and this module
adds nothing but named accessors over data the projection already loaded::

    SQLite
      -> repository ports
        -> ReportBuilder
          -> ReportSnapshot      (the canonical read-only projection)
            -> Monitor           (this module: pure selectors, pull-based)
              -> GUI / CLI renderer

Read-only by construction
-------------------------
* :class:`Monitor` is handed **one** collaborator - the
  :class:`~architecture_assistant.application.reporting.ReportBuilder` - and
  nothing else. It never receives ``StoragePort``, ``TransactionPort``,
  ``CostPort`` or any repository, so no write method is reachable from it and an
  accidental mutation is impossible.
* It opens no connection, imports no ``infrastructure`` module and writes no
  audit entry.
* It keeps **no** loop state of its own. :meth:`Monitor.current_step` and
  :meth:`Monitor.next_step` re-expose the authoritative numbers the loop itself
  already computed (``LoopHealth``), and :meth:`Monitor.blocking_steps` reuses
  the loop's own halt set, so the monitor can never disagree with the
  orchestrator about where the loop is or which step is waiting.

Pull-based, never an event stream
---------------------------------
There is no thread, no timer, no polling loop, no background refresh and no
alerting here: a caller asks for the state when it wants it and receives a
plain, JSON-safe payload. Refreshing, rendering and alerting belong to whoever
calls this module, and the operator's ability to *change* the workflow (pause,
resume, override) is a write use-case of its own - deliberately **not** here.
"""

from __future__ import annotations

from typing import Any, Optional

from ..domain.enums import RiskStatus
from ..domain.models import ArchitectureChangeRequest, Risk, Step
from ..ports.capabilities import CostSummary
from .orchestrator import HALTING_REASONS
from .reporting import ReportBuilder, ReportSnapshot
from .scheduler import LoopHealth

__all__ = ["Monitor"]


class Monitor:
    """A pull-based, read-only view of the canonical project projection.

    Every public method performs one fresh read through the injected
    :class:`ReportBuilder` and returns a plain immutable value: there is no
    cache to go stale and no state to advance. The monitor is never part of a
    loop decision.
    """

    def __init__(self, report_builder: ReportBuilder) -> None:
        if not isinstance(report_builder, ReportBuilder):
            raise ValueError(
                "report_builder must be a ReportBuilder "
                f"(a read-only projection); got {report_builder!r}"
            )
        self._report_builder = report_builder

    def _snapshot(self) -> ReportSnapshot:
        """One fresh read of the canonical projection."""
        return self._report_builder.build()

    # -- the canonical projection ------------------------------------------

    def snapshot(self) -> ReportSnapshot:
        """The whole project projection, freshly read."""
        return self._snapshot()

    def to_dict(self) -> dict[str, Any]:
        """The projection as a deterministic, JSON-safe payload.

        This is the provider-neutral value a GUI, a CLI or a log line renders -
        the monitor itself never formats anything.
        """
        return self._snapshot().to_dict()

    # -- loop state ---------------------------------------------------------

    def health(self) -> LoopHealth:
        """The loop health the scheduler computed for the current state.

        Read-only by definition: asking for it advances nothing.
        """
        return self._snapshot().health

    def current_step(self) -> Optional[Step]:
        """The authoritative current step, or ``None`` when there is none.

        The step *number* is the loop's own answer
        (``LoopHealth.current_step_no``, i.e. the lowest-numbered step that is
        not ``VERIFIED``); the monitor only resolves it to the persisted
        :class:`~architecture_assistant.domain.models.Step` so the caller has a
        record it can render. No second ordering rule exists here.
        """
        snapshot = self._snapshot()
        return _find_step(snapshot, snapshot.health.current_step_no)

    def next_step(self) -> Optional[Step]:
        """The step after the current one - exactly as the loop reports it.

        The number comes from ``LoopHealth.next_step_no``; the monitor only
        resolves it to the persisted record.
        """
        snapshot = self._snapshot()
        return _find_step(snapshot, snapshot.health.next_step_no)

    def step_states(self) -> tuple[tuple[int, str], ...]:
        """Every step with its declared state, ordered by step number.

        A flat ``(step_no, state)`` map for a UI list: the state is the
        authoritative domain value, never a derived label.
        """
        return tuple(
            (step.step_no, step.state.value) for step in self._snapshot().steps
        )

    def blocking_steps(self) -> tuple[Step, ...]:
        """The steps that are waiting for a **human**, in step order.

        "Blocking" is not re-decided here. The set is the loop's own
        ``HALTING_REASONS`` (from
        :mod:`architecture_assistant.application.orchestrator`), i.e. exactly
        the states in which the orchestrator halts and waits for an external
        event instead of advancing:

        * ``WAITING_APPROVAL`` - waiting for a human approval;
        * ``BLOCKED`` - waiting for a human unblock;
        * ``CONFLICT`` - waiting for a human conflict resolution.

        ``REVISE`` and ``FAILED`` are deliberately **not** blocking: the loop
        owns those two and resolves them itself (a retry while attempts remain,
        an escalation to ``BLOCKED`` afterwards), so they are never reported as
        human blockers. The working states (``PENDING``, ``READY``,
        ``DISPATCHED``, ``CLINE_WORKING``, ``REPORT_RECEIVED``, ``REVIEWING``)
        and the terminal states (``VERIFIED``, ``ABORTED``) are not blocking
        either.
        """
        return tuple(
            step
            for step in self._snapshot().steps
            if step.state in HALTING_REASONS
        )

    # -- architecture and risk ---------------------------------------------

    def architecture_version(self) -> Optional[str]:
        """The current baseline version, or ``None`` before one exists."""
        current = self._snapshot().architecture
        return None if current is None else current.version

    def open_risks(self) -> tuple[Risk, ...]:
        """The risk-register entries still ``OPEN``."""
        return tuple(
            risk
            for risk in self._snapshot().risks
            if risk.status is RiskStatus.OPEN
        )

    def change_requests(self) -> tuple[ArchitectureChangeRequest, ...]:
        """Every persisted Architecture Change Request, in projection order.

        Read-only: proposing, approving, rejecting and applying a change stay
        with the architecture-evolution use-case.
        """
        return self._snapshot().change_requests

    # -- cost ---------------------------------------------------------------

    def cost_summary(self) -> CostSummary:
        """The provider-neutral cost roll-up of the whole project."""
        return self._snapshot().cost


def _find_step(
    snapshot: ReportSnapshot, step_no: Optional[int]
) -> Optional[Step]:
    """Resolve a loop-reported step number to the persisted step, if any.

    ``None`` in, ``None`` out: a finished loop (no current step) reports no
    current step rather than raising.
    """
    if step_no is None:
        return None
    for step in snapshot.steps:
        if step.step_no == step_no:
            return step
    return None
