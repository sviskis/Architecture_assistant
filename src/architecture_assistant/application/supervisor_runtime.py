"""The headless supervisor runtime: a cheap, tick-driven coordinator.

One tick is deliberately boring. It reconciles what a restart left half-done,
looks at the **current** report of the current step, and does nothing at all when
that report already has a decided supervision record. It never calls a supervisor
for an unchanged report, never rebuilds an architecture snapshot it does not need
and never writes an audit entry - a two-second tick must cost almost nothing, or
it will be the reason nobody dares enable supervision.

Why this object owns no thread
------------------------------
The assistant has exactly **one** thread allowed to touch the source of truth:
the core worker that owns the SQLite connection (``sqlite3`` connections are
created with the default ``check_same_thread=True``, and the GUI's background
runner is built around that rule). A second thread writing supervision rows would
break that invariant - so the runtime is a *tick-driven coordinator*, not a
daemon. ``start()``/``stop()`` mark it running, ``tick()`` does one tick's work on
whatever thread the host owns, and ``status()`` reports process-local counters.
A host that wants a two-second cadence calls ``tick()`` every :attr:`interval`
seconds; the GUI schedules exactly that with ``root.after``, and a test calls it
by hand. There is no global singleton, no service and no hidden thread.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Callable, Optional

from ..domain.enums import StepState
from ..domain.models import Step, utc_now
from ..ports.storage import StoragePort
from .eventlog import EVENT_LEVEL_INFO, build_event
from .supervision import (
    SUPERVISOR_COMPONENT,
    Supervision,
    SupervisionAnalysis,
    WaitingFor,
)

__all__ = [
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "MAX_TICK_RESULTS",
    "RuntimeState",
    "RuntimeTick",
    "SupervisorRuntime",
]

#: How often a host should tick. Cheap enough to be frequent, slow enough that a
#: worker report written between two ticks is never missed for long.
DEFAULT_POLL_INTERVAL_SECONDS = 2.0

#: How many per-record results one tick report carries.
MAX_TICK_RESULTS = 8


class RuntimeState(StrEnum):
    """Whether the runtime is ticking at all."""

    STOPPED = "STOPPED"
    RUNNING = "RUNNING"


@dataclass(frozen=True)
class RuntimeTick:
    """What one tick did - plain data, for the runtime's owner and for tests."""

    state: str
    outcome: str
    step_no: Optional[int] = None
    attempt: Optional[int] = None
    waiting_for: str = WaitingFor.NONE.value
    reason: str = ""
    results: tuple[SupervisionAnalysis, ...] = ()

    @property
    def provider_called(self) -> bool:
        """Whether this tick asked a supervisor anything."""
        return any(item.provider_called for item in self.results)

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "state": self.state,
            "outcome": self.outcome,
            "step_no": self.step_no,
            "attempt": self.attempt,
            "waiting_for": self.waiting_for,
            "reason": self.reason,
            "provider_called": self.provider_called,
            "results": [item.to_dict() for item in self.results],
        }


class SupervisorRuntime:
    """The cheap, tick-driven supervision coordinator.

    It is handed the source of truth and the supervision use-case and nothing
    else - no monitor, no orchestrator, no FSM. It cannot move a Step, cannot
    acknowledge a report and cannot reach the realization gate; the only thing it
    does that has an effect is call the use-case, which is itself bounded by the
    identity, the policy and the audit trail.
    """

    def __init__(
        self,
        storage: StoragePort,
        supervision: Supervision,
        *,
        interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if (
            isinstance(interval, bool)
            or not isinstance(interval, (int, float))
            or interval <= 0
        ):
            raise ValueError(
                f"interval must be a positive number; got {interval!r}"
            )
        if not callable(clock):
            raise ValueError("clock must be a callable returning a datetime")
        if not isinstance(supervision, Supervision):
            raise ValueError(
                "supervision must be a Supervision; got "
                f"{type(supervision).__name__}"
            )
        self._storage = storage
        self._supervision = supervision
        self._interval = float(interval)
        self._clock = clock
        self._state = RuntimeState.STOPPED
        self._started_at: Optional[datetime] = None
        self._last_tick_at: Optional[datetime] = None
        self._last_outcome = ""
        self._ticks = 0
        self._reconciled = 0
        self._provider_calls = 0
        #: Which report identities this process has already announced, so a
        #: two-second tick gives the panel one line per report instead of one every
        #: two seconds. Deliberately process-local: losing it can only cause one
        #: extra line, never a wrong decision.
        self._announced: dict[str, str] = {}

    # -- read-only ---------------------------------------------------------
    @property
    def interval(self) -> float:
        """How often a host should call :meth:`tick`."""
        return self._interval

    @property
    def state(self) -> RuntimeState:
        """Whether the runtime is running."""
        return self._state

    @property
    def running(self) -> bool:
        """Whether the runtime is running."""
        return self._state is RuntimeState.RUNNING

    @property
    def supervision(self) -> Supervision:
        """The supervision use-case this runtime drives."""
        return self._supervision

    def status(self) -> dict[str, Any]:
        """Process-local polling status - plain data, no database read."""
        return {
            "state": self._state.value,
            "running": self.running,
            "interval_seconds": self._interval,
            "tick_count": self._ticks,
            "reconcile_count": self._reconciled,
            "provider_calls": self._provider_calls,
            "last_outcome": self._last_outcome,
            "started_at": (
                None if self._started_at is None else self._started_at.isoformat()
            ),
            "last_tick_at": (
                None
                if self._last_tick_at is None
                else self._last_tick_at.isoformat()
            ),
        }

    # -- lifecycle ---------------------------------------------------------
    def start(
        self, *, on_event: Optional[Callable[[dict[str, Any]], None]] = None
    ) -> RuntimeTick:
        """Mark the runtime running and reconcile immediately.

        The immediate tick is what makes a restart honest: whatever a previous
        process left half-done is resolved before the first scheduled tick, so a
        ``SEND_PENDING`` record is never left waiting for the next poll to be
        noticed.

        With supervision disabled this does **nothing**: the runtime is not marked
        running, no event is emitted and no tick counter moves, because there is
        nothing for it to drive.
        """
        if not self._supervision.enabled:
            return self._disabled_tick()
        if not self.running:
            self._state = RuntimeState.RUNNING
            self._started_at = self._clock()
            self._emit(
                on_event,
                action="supervisor-started",
                message=(
                    "supervision runtime started; polling every "
                    f"{self._interval:g}s"
                ),
            )
        return self.tick(on_event=on_event)

    def stop(
        self, *, on_event: Optional[Callable[[dict[str, Any]], None]] = None
    ) -> None:
        """Mark the runtime stopped. Persisted state is untouched."""
        if not self.running:
            return
        self._state = RuntimeState.STOPPED
        self._emit(
            on_event,
            action="supervisor-stopped",
            message=(
                f"supervision runtime stopped after {self._ticks} tick(s); "
                "persisted supervision state is unchanged"
            ),
        )

    # -- the tick ----------------------------------------------------------
    def tick(
        self, *, on_event: Optional[Callable[[dict[str, Any]], None]] = None
    ) -> RuntimeTick:
        """One cheap tick: reconcile, then look at the current report.

        With supervision disabled the tick is inert: no reconcile, no analysis, no
        provider call, no counter, no event.
        """
        if not self._supervision.enabled:
            return self._disabled_tick()
        if not self.running:
            return RuntimeTick(
                state=RuntimeState.STOPPED.value,
                outcome="stopped",
                reason="the runtime is not running",
            )
        self._ticks += 1
        self._last_tick_at = self._clock()
        results = list(self._supervision.reconcile(on_event=on_event))
        self._reconciled += len(
            [
                item
                for item in results
                if item.outcome in ("send-recovered", "send-completed")
            ]
        )
        step = self._current_step()
        if step is None:
            return self._finish(
                RuntimeTick(
                    state=RuntimeState.RUNNING.value,
                    outcome="no-step",
                    reason="there is no current step to supervise",
                    results=tuple(results[:MAX_TICK_RESULTS]),
                )
            )
        analysis = self._supervision.advance(
            step.step_no, step.attempt, on_event=on_event
        )
        results.append(analysis)
        if analysis.provider_called:
            self._provider_calls += 1
        self._announce(analysis, on_event=on_event)
        return self._finish(
            RuntimeTick(
                state=RuntimeState.RUNNING.value,
                outcome=analysis.outcome,
                step_no=step.step_no,
                attempt=step.attempt,
                waiting_for=analysis.waiting_for,
                reason=analysis.reason,
                results=tuple(results[:MAX_TICK_RESULTS]),
            )
        )

    def _finish(self, tick: RuntimeTick) -> RuntimeTick:
        """Remember the last outcome and hand the report back."""
        self._last_outcome = tick.outcome
        return tick

    def _disabled_tick(self) -> RuntimeTick:
        """The answer of a disabled runtime: a report, not an action.

        Deliberately reuses :attr:`RuntimeState.STOPPED` instead of inventing a
        third runtime state, and deliberately touches nothing: no counter, no
        timestamp, no event, no last outcome. A host that keeps polling a disabled
        session sees the same answer every time and pays nothing for it.
        """
        return RuntimeTick(
            state=RuntimeState.STOPPED.value,
            outcome="disabled",
            waiting_for=WaitingFor.NONE.value,
            reason=(
                "supervision is disabled for this composition: there is nothing "
                "to tick"
            ),
        )

    def _current_step(self) -> Optional[Step]:
        """The authoritative current step: the lowest not ``VERIFIED``."""
        steps = self._storage.steps.list()
        for step in sorted(steps, key=lambda item: item.step_no):
            if step.state is not StepState.VERIFIED:
                return step
        return None

    def _announce(
        self,
        analysis: SupervisionAnalysis,
        *,
        on_event: Optional[Callable[[dict[str, Any]], None]],
    ) -> None:
        """Mention each report identity once, and never once per tick.

        One line when the report is analysed and one when a later tick finds it
        unchanged - never one every two seconds. The bookkeeping is deliberately
        process-local: losing it can only cause one extra line, never a decision.
        """
        identity = analysis.supervision_id
        if on_event is None or identity is None:
            return
        seen = self._announced.get(identity)
        if analysis.provider_called:
            if seen in (None, "seen"):
                self._announced[identity] = "analysed"
                self._emit(
                    on_event,
                    action="new-report-detected",
                    message=(
                        f"a new worker report was detected for step "
                        f"{analysis.step_no} attempt {analysis.attempt} and "
                        "analysed"
                    ),
                    step_no=analysis.step_no,
                )
            return
        if seen in (None, "analysed"):
            self._announced[identity] = "duplicate"
            self._emit(
                on_event,
                action="duplicate-report",
                message=(
                    f"the report of step {analysis.step_no} attempt "
                    f"{analysis.attempt} is already supervised; nothing to do"
                ),
                step_no=analysis.step_no,
            )

    def _emit(
        self,
        on_event: Optional[Callable[[dict[str, Any]], None]],
        *,
        action: str,
        message: str,
        step_no: Optional[int] = None,
    ) -> None:
        """One runtime lifecycle event, validated by the shared contract."""
        if on_event is None:
            return
        on_event(
            build_event(
                level=EVENT_LEVEL_INFO,
                component=SUPERVISOR_COMPONENT,
                action=action,
                message=message,
                step_no=step_no,
                timestamp=self._clock(),
            )
        )
