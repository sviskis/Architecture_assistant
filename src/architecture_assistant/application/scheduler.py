"""The scheduler: drives the loop and stops the moment an external event is due.

The scheduler owns the *time* dimension of the loop and nothing else. It never
invents work: it calls :meth:`Orchestrator.run_once` repeatedly and stops as soon
as:

* a transition was made and the *next* tick would need an external event
  (worker report, human approval, unblock, conflict resolution, deadline) -
  :attr:`LoopStatus.WAITING`;
* the project is complete, paused or the current step was aborted;
* a defensive iteration limit is reached.

There is deliberately **no** polling loop, no ``sleep`` and no background thread:
waiting is expressed by *returning*, so the caller (the composition root, or a
later external process) decides when to call again. :meth:`Scheduler.health`
gives such a caller a read-only view of the loop without touching any state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..domain.enums import StepState
from .orchestrator import LoopStatus, Orchestrator, TickResult

__all__ = [
    "DEFAULT_MAX_ITERATIONS",
    "SchedulerRun",
    "LoopHealth",
    "Scheduler",
]

#: Defensive bound: the loop may only chain this many immediate transitions per
#: call. A correctly behaving loop is far below it.
DEFAULT_MAX_ITERATIONS = 64


@dataclass(frozen=True)
class SchedulerRun:
    """Deterministic record of one :meth:`Scheduler.run_until_idle` call."""

    ticks: tuple[TickResult, ...]
    limit_reached: bool = False

    @property
    def iterations(self) -> int:
        """Number of ticks performed."""
        return len(self.ticks)

    @property
    def transitions(self) -> tuple[TickResult, ...]:
        """Only the ticks that persisted a transition, in order."""
        return tuple(tick for tick in self.ticks if tick.transitioned)

    @property
    def transition_count(self) -> int:
        """Number of persisted transitions."""
        return len(self.transitions)

    @property
    def final_tick(self) -> Optional[TickResult]:
        """The tick that stopped the loop (``None`` when nothing was ticked)."""
        return self.ticks[-1] if self.ticks else None

    @property
    def stopped_because(self) -> str:
        """The reason the loop stopped."""
        if self.limit_reached:
            return "iteration-limit"
        last = self.final_tick
        return "never-started" if last is None else last.reason

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "iterations": self.iterations,
            "transition_count": self.transition_count,
            "limit_reached": self.limit_reached,
            "stopped_because": self.stopped_because,
            "ticks": [tick.to_dict() for tick in self.ticks],
        }


@dataclass(frozen=True)
class LoopHealth:
    """Read-only snapshot of the loop, for an external health check."""

    project_paused: bool
    step_count: int
    current_step_no: Optional[int]
    current_state: Optional[StepState]
    next_step_no: Optional[int]
    complete: bool

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "project_paused": self.project_paused,
            "step_count": self.step_count,
            "current_step_no": self.current_step_no,
            "current_state": (
                None if self.current_state is None else self.current_state.value
            ),
            "next_step_no": self.next_step_no,
            "complete": self.complete,
        }


class Scheduler:
    """Runs immediate loop transitions until an external event is required."""

    def __init__(
        self,
        orchestrator: Orchestrator,
        *,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
    ) -> None:
        if not isinstance(orchestrator, Orchestrator):
            raise ValueError(
                f"orchestrator must be an Orchestrator; "
                f"got {type(orchestrator).__name__}"
            )
        if (
            isinstance(max_iterations, bool)
            or not isinstance(max_iterations, int)
            or max_iterations < 1
        ):
            raise ValueError(
                f"max_iterations must be an int >= 1; got {max_iterations!r}"
            )
        self._orchestrator = orchestrator
        self._max_iterations = max_iterations

    @property
    def orchestrator(self) -> Orchestrator:
        """The orchestrator this scheduler drives."""
        return self._orchestrator

    @property
    def max_iterations(self) -> int:
        """The per-call iteration bound."""
        return self._max_iterations

    def run_until_idle(self) -> SchedulerRun:
        """Chain transitions until the loop must wait, or cannot progress."""
        ticks: list[TickResult] = []
        for _ in range(self._max_iterations):
            tick = self._orchestrator.run_once()
            ticks.append(tick)
            if tick.status is not LoopStatus.TRANSITIONED:
                return SchedulerRun(ticks=tuple(ticks), limit_reached=False)
        return SchedulerRun(ticks=tuple(ticks), limit_reached=True)

    def health(self) -> LoopHealth:
        """Read-only loop health - never advances anything."""
        current = self._orchestrator.current_step()
        return LoopHealth(
            project_paused=self._orchestrator.is_paused(),
            step_count=self._orchestrator.step_count(),
            current_step_no=None if current is None else current.step_no,
            current_state=None if current is None else current.state,
            next_step_no=self._orchestrator.next_step_no(),
            complete=self._orchestrator.is_complete(),
        )