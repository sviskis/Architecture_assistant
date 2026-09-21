"""Step 22 - crash / restart recovery tests.

Principle: **the process may die, the state must survive.** SQLite plus the
persisted filesystem artifacts are the source of truth; nothing here may rely on
a Python object outliving a crash.

Three crash fidelities are used, and none of them substitutes for another:

``L1``
    failure points inside one process (an injected exploding port): the
    transaction must roll back and the loop must resume from persisted state.
``L2``
    a real **file-backed** SQLite database on ``tmp_path`` that is closed and
    reopened: the state must survive the process that wrote it.
``L3``
    a real **subprocess** killed with ``os._exit(1)`` - including one death
    **between the two writes of one transaction** (``tests/_recovery_child.py``).

``:memory:`` databases and "new repositories over the same live connection" are
deliberately absent from the decisive tests: they would prove nothing about a
process that dies. ``TestNoFakeSuccess`` pins that discipline.

Nothing in this module fixes a defect: AQ-1, AQ-2 and F-1 are *reproduced* and
carried to Step 23 (see ``TestDeferredArchitectureFindings``).
"""

from __future__ import annotations

import gc
import json
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest

from architecture_assistant.application import (
    PROJECT_NAME,
    HumanOverride,
    OverrideTransitionError,
)
from architecture_assistant.architecture import (
    ARCHITECTURE_CURRENT,
    ROOT_PACKAGE,
    ArchitectureValidator,
    scan_directory,
)
from architecture_assistant.composition import (
    Composition,
    CompositionConfig,
    compose,
)
from architecture_assistant.domain.enums import (
    Mode,
    Phase,
    ReportStatus,
    RiskLevel,
    StepEvent,
    StepState,
    TaskState,
)
from architecture_assistant.domain.fsm import STEP_TRANSITIONS
from architecture_assistant.domain.models import Step
from architecture_assistant.infrastructure import (
    ClineWorkerAdapter,
    ReportParseError,
)
from architecture_assistant.infrastructure.sqlite import applied_versions
from architecture_assistant.ports.capabilities import (
    CostIdentityConflictError,
    CostQuery,
    CostRecord,
)

#: A fixed stamp: no recovery test depends on the wall clock.
NOW = datetime(2026, 9, 21, 22, 0, tzinfo=timezone.utc)

#: The step the crash scenarios drive.
STEP_NO = 9

#: The two steps the mandatory end-to-end restart test drives.
FIRST, SECOND = 1, 2

#: The cost event identity reused by the cost scenarios. It **must** match
#: ``tests/_recovery_child.py``: the parent replays exactly the child's event.
COST_EVENT_ID = "openai:recovery-event"

#: The subprocess crash driver.
CHILD = Path(__file__).with_name("_recovery_child.py")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _src_root() -> Path:
    return _repo_root() / "src" / "architecture_assistant"


#: The tree the realization gate inspects: the assistant itself.
REAL_SOURCE_ROOT = _src_root()


class StepClock:
    """An injected clock a test can advance (for the timeout path)."""

    def __init__(self) -> None:
        self._now = NOW

    def __call__(self) -> datetime:
        return self._now

    def advance(self, **parts: float) -> None:
        self._now = self._now + timedelta(**parts)


def fixed_clock() -> datetime:
    return NOW


def make_config(
    tmp_path: Any,
    *,
    clock: Any = fixed_clock,
    mode: Mode = Mode.AUTO,
    timeout: Any = timedelta(hours=1),
) -> CompositionConfig:
    """A file-backed configuration: the database is a real file in ``tmp_path``."""
    return CompositionConfig(
        database_path=tmp_path / "assistant.db",
        exchange_dir=tmp_path / "cline",
        report_dir=tmp_path / "reports",
        mode=mode,
        timeout=timeout,
        source_root=REAL_SOURCE_ROOT,
        clock=clock,
    )


@pytest.fixture
def config(tmp_path) -> CompositionConfig:
    return make_config(tmp_path)


def make_step(
    step_no: int = STEP_NO,
    state: StepState = StepState.PENDING,
    attempt: int = 1,
    max_attempts: int = 3,
) -> Step:
    """A minimal step, seeded directly into the source of truth."""
    return Step(
        step_no=step_no,
        phase=Phase.LOOP,
        title=f"Recovery step {step_no}",
        state=state,
        attempt=attempt,
        max_attempts=max_attempts,
        risk=RiskLevel.LOW,
        requires_human=False,
        created_at=NOW,
        last_update_at=NOW,
    )


def cost_record(config: CompositionConfig, **overrides: Any) -> CostRecord:
    data: dict[str, Any] = dict(
        provider="openai",
        event_id=COST_EVENT_ID,
        input_tokens=10,
        output_tokens=2,
        cost_usd=0.5,
        pricing_known=True,
        project=config.project_name,
        step_no=STEP_NO,
        created_at=NOW,
    )
    data.update(overrides)
    return CostRecord(**data)


@contextmanager
def composed(config: CompositionConfig) -> Iterator[Composition]:
    """Compose and always close - the honest "the process ends" shape."""
    composition = compose(config)
    try:
        yield composition
    finally:
        composition.close()


def state_of(composition: Composition) -> dict[str, Any]:
    """The authoritative state, read through the composition's own ports."""
    return {
        "project": composition.storage.projects.get(PROJECT_NAME),
        "steps": composition.storage.steps.list(),
        "tasks": composition.storage.tasks.list(),
        "audit": composition.storage.audit.list(),
        "adrs": composition.storage.adrs.list(),
        "versions": composition.storage.architecture_versions.list(),
        "requests": composition.storage.change_requests.list(),
        "cost": composition.cost_plugin.query(CostQuery()),
        "health": composition.health(),
    }


def step_of(composition: Composition, step_no: int = STEP_NO) -> Step:
    step = composition.storage.steps.get(step_no)
    assert step is not None, step_no
    return step


def step_entries(composition: Composition, step_no: int = STEP_NO) -> list[Any]:
    """Every audit entry written for one step, in order."""
    return [
        entry
        for entry in composition.storage.audit.list()
        if entry.entity_id == str(step_no)
    ]


def report_path(
    config: CompositionConfig, step_no: int = STEP_NO, attempt: int = 1
) -> Path:
    return ClineWorkerAdapter(config.exchange_dir).report_path(step_no, attempt)


def archive_dir(config: CompositionConfig) -> Path:
    return ClineWorkerAdapter(config.exchange_dir).archive_dir()


def archived(
    config: CompositionConfig, step_no: int = STEP_NO, attempt: int = 1
) -> list[Path]:
    """Every archived report belonging to ``(step_no, attempt)``."""
    directory = archive_dir(config)
    if not directory.is_dir():
        return []
    prefix = f"step_{step_no:03d}_attempt_{attempt:03d}_report"
    return sorted(directory.glob(f"{prefix}*.json"))


def write_report(
    config: CompositionConfig,
    step_no: int = STEP_NO,
    attempt: int = 1,
    status: ReportStatus = ReportStatus.DONE,
    **overrides: Any,
) -> Path:
    """Publish a worker report exactly where the real file channel expects it."""
    path = report_path(config, step_no, attempt)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "step_no": step_no,
        "attempt": attempt,
        "status": status.value,
        "summary": f"worker report ({status.value})",
        "files_created": [],
        "files_changed": [],
        "files_deleted": [],
        "issues": [],
        "architecture_questions": [],
        "dependencies_added": [],
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def bare_rows(database_path: Any, query: str) -> list[tuple]:
    """Read the database directly - no composition and no repository involved."""
    connection = sqlite3.connect(str(database_path))
    try:
        return [tuple(row) for row in connection.execute(query).fetchall()]
    finally:
        connection.close()


def migration_versions(database_path: Any) -> tuple[int, ...]:
    """The applied migration versions, read through a fresh connection."""
    connection = sqlite3.connect(str(database_path))
    try:
        return applied_versions(connection)
    finally:
        connection.close()


class ExplodingAudit:
    """An audit port that raises instead of writing (an L1 failure point)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def append(self, entry: Any) -> None:
        raise RuntimeError("audit unavailable")

    def list(self) -> Any:
        return self._inner.list()

    def list_for_entity(self, *args: Any, **kwargs: Any) -> Any:
        return self._inner.list_for_entity(*args, **kwargs)


class TestSqliteIntegrity:
    """#10 A failure between the writes of one transaction leaves no trace."""

    def test_an_audit_failure_rolls_the_domain_write_back(self, config) -> None:
        """L1: the failure point is inside the process, the rollback is real."""
        with composed(config) as composition:
            composition.storage.steps.upsert(
                make_step(STEP_NO, StepState.PENDING, 1)
            )
            composition.storage._audit = ExplodingAudit(
                composition.storage._audit
            )
            before_audit = composition.storage.audit.list()

            with pytest.raises(RuntimeError, match="audit unavailable"):
                composition.orchestrator.run_once()

            assert step_of(composition).state is StepState.PENDING
            assert composition.storage.tasks.list() == ()
            assert composition.storage.audit.list() == before_audit

        assert bare_rows(config.database_path, "SELECT state FROM steps") == [
            ("PENDING",)
        ]

    def test_a_mid_transaction_process_death_leaves_no_partial_state(
        self, config
    ) -> None:
        """L3: a real process died between the two writes of one transaction."""
        stdout = crash_child("crash_inside_transition", config)
        assert "audit-append" in stdout

        assert bare_rows(
            config.database_path, "SELECT state, attempt FROM steps"
        ) == [("PENDING", 1)]
        assert bare_rows(config.database_path, "SELECT * FROM tasks") == []
        assert (
            bare_rows(
                config.database_path,
                "SELECT * FROM audit_entries WHERE entity_type = 'STEP'",
            )
            == []
        )

    def test_the_database_is_still_consistent_and_usable(self, config) -> None:
        crash_child("crash_inside_transition", config)

        assert bare_rows(config.database_path, "PRAGMA integrity_check") == [
            ("ok",)
        ]

        with composed(config) as composition:
            run = composition.run_until_idle()

            assert run.stopped_because == "worker-report-pending"
            assert step_of(composition).state is StepState.CLINE_WORKING

    def test_the_migrations_are_not_reapplied_after_a_crash(self, config) -> None:
        with composed(config):
            pass
        before = migration_versions(config.database_path)

        crash_child("crash_inside_transition", config)

        assert migration_versions(config.database_path) == before
        assert (
            len(bare_rows(config.database_path, "SELECT * FROM schema_migrations"))
            == len(before)
        )


class TestFileChannelIntegrity:
    """#11 temp-file + atomic replace: no partial artefact is committed input."""

    def test_a_temporary_report_sibling_is_never_read_as_a_report(
        self, config
    ) -> None:
        with composed(config) as composition:
            composition.storage.steps.upsert(make_step(STEP_NO, StepState.PENDING, 1))
            composition.run_until_idle()

            final = report_path(config, STEP_NO, 1)
            final.parent.mkdir(parents=True, exist_ok=True)
            temporary = final.parent / f".{final.name}.1234.tmp"
            temporary.write_text(
                json.dumps(
                    {"step_no": STEP_NO, "attempt": 1, "status": "DONE"}
                ),
                encoding="utf-8",
            )

            assert composition.worker.read_report(STEP_NO, 1) is None
            assert (
                composition.run_until_idle().stopped_because
                == "worker-report-pending"
            )
            assert step_of(composition).state is StepState.CLINE_WORKING
            assert not final.exists()

    def test_a_truncated_report_is_refused_and_never_processed(
        self, config
    ) -> None:
        with composed(config) as composition:
            composition.storage.steps.upsert(make_step(STEP_NO, StepState.PENDING, 1))
            composition.run_until_idle()
            before_audit = composition.storage.audit.list()

            final = report_path(config, STEP_NO, 1)
            final.parent.mkdir(parents=True, exist_ok=True)
            partial = '{"step_no": 9, "attempt": 1, "stat'
            final.write_text(partial, encoding="utf-8")

            with pytest.raises(ReportParseError):
                composition.orchestrator.run_once()

            # Nothing was processed, nothing was mutated, nothing was moved.
            assert step_of(composition).state is StepState.CLINE_WORKING
            assert composition.storage.audit.list() == before_audit
            assert final.read_text(encoding="utf-8") == partial

    def test_every_published_task_has_its_context(self, config) -> None:
        """The context is published before the task - and both atomically."""
        with composed(config) as composition:
            composition.storage.steps.upsert(make_step(STEP_NO, StepState.PENDING, 1))
            composition.run_until_idle()

        tasks = list((config.exchange_dir / "to_cline").glob("*_task.json"))
        assert len(tasks) == 1
        payload = json.loads(tasks[0].read_text(encoding="utf-8"))

        assert Path(payload["context_file"]).is_file()
        assert payload["attempt"] == 1
        assert list(config.exchange_dir.rglob("*.tmp")) == []

    def test_a_crash_leaves_no_partial_exchange_artefact(self, config) -> None:
        crash_child("crash_after_dispatch_commit", config)
        adapter = ClineWorkerAdapter(config.exchange_dir)

        assert list(config.exchange_dir.rglob("*.tmp")) == []
        assert json.loads(
            adapter.task_path(STEP_NO, 1).read_text(encoding="utf-8")
        )["step_no"] == STEP_NO
        assert json.loads(
            adapter.context_path(STEP_NO, 1).read_text(encoding="utf-8")
        ) != {}


class TestRecoveryIsPersistedStateOnly:
    """#12 No decision may depend on a Python object surviving the crash."""

    def test_every_loop_service_is_reconstructed_from_the_database(
        self, config
    ) -> None:
        with composed(config) as composition:
            composition.storage.steps.upsert(
                make_step(STEP_NO, StepState.BLOCKED, 1)
            )
            dead = {
                "orchestrator": composition.orchestrator,
                "scheduler": composition.scheduler,
                "worker": composition.worker,
                "report_builder": composition.report_builder,
                "monitor": composition.monitor,
            }

        with composed(config) as composition:
            for name, previous in dead.items():
                assert getattr(composition, name) is not previous, name
            assert step_of(composition).state is StepState.BLOCKED
            assert composition.orchestrator.current_step().step_no == STEP_NO

    def test_the_same_persisted_state_always_yields_the_same_decision(
        self, config
    ) -> None:
        """Two processes, one file: the decision follows the row, not the RAM."""
        with composed(config) as composition:
            composition.storage.steps.upsert(
                make_step(STEP_NO, StepState.READY, 1)
            )
            first = composition.orchestrator.run_once().to_state
            composition.storage.steps.upsert(
                make_step(STEP_NO, StepState.READY, 1)
            )

        with composed(config) as composition:
            second = composition.orchestrator.run_once().to_state

        assert first is StepState.DISPATCHED
        assert second is first

    def test_an_unpersisted_change_never_survives(self, config) -> None:
        """The negative control: RAM is not part of the recovery contract."""
        with composed(config) as composition:
            composition.storage.steps.upsert(
                make_step(STEP_NO, StepState.BLOCKED, 1)
            )
            step = step_of(composition)
            object.__setattr__(step, "state", StepState.VERIFIED)

        with composed(config) as composition:
            assert step_of(composition).state is StepState.BLOCKED
            assert step_of(composition) is not step


class TestFreshProcessRecovery:
    """#13 Recovery with a process that never saw the original journey."""

    def test_a_child_process_advance_is_readable_by_this_process(
        self, config
    ) -> None:
        completed = run_child("advance", config)

        assert completed.returncode == 0, completed.stderr

        with composed(config) as composition:
            assert step_of(composition).state is StepState.CLINE_WORKING
            assert step_of(composition).attempt == 1
            assert composition.storage.tasks.list() != ()
            assert composition.orchestrator.current_step().step_no == STEP_NO

    def test_this_process_finishes_what_the_child_started(self, config) -> None:
        run_child("advance", config)

        with composed(config) as composition:
            write_report(config, STEP_NO, 1, ReportStatus.DONE)

            run = composition.run_until_idle()

            assert run.stopped_because == "all-steps-verified"
            assert step_of(composition).state is StepState.VERIFIED
            assert len(archived(config, STEP_NO, 1)) == 1

    def test_a_killed_process_leaves_a_recoverable_workflow(self, config) -> None:
        crash_child("crash_after_report_written", config)

        with composed(config) as composition:
            run = composition.run_until_idle()

            assert run.stopped_because == "all-steps-verified"
            assert step_of(composition).state is StepState.VERIFIED

        assert len(archived(config, STEP_NO, 1)) == 1


class TestDeferredArchitectureFindings:
    """#14 AQ-1, AQ-2 and F-1 reproduced - none of them fixed by Step 22.

    These tests exist to *demonstrate* the findings, never to repair them: Step
    22 changed no production code, and a finding is only carried forward when it
    is reproducible.
    """

    def test_aq1_the_loop_waits_for_a_human_who_cannot_act(self, config) -> None:
        """``REVISE`` with a spent budget has no exit at all."""
        with composed(config) as composition:
            composition.storage.steps.upsert(
                make_step(STEP_NO, StepState.REVISE, attempt=3, max_attempts=3)
            )

        with composed(config) as composition:
            run = composition.run_until_idle()

            assert run.stopped_because == "revise-attempts-exhausted"
            assert step_of(composition).state is StepState.REVISE
            assert step_of(composition).attempt == 3

    def test_aq1_the_only_fsm_exit_is_retry(self) -> None:
        exits = [
            event
            for (state, event) in STEP_TRANSITIONS
            if state is StepState.REVISE
        ]

        assert exits == [StepEvent.RETRY]
        assert (StepState.REVISE, StepEvent.ABORT) not in STEP_TRANSITIONS

    def test_aq1_the_domain_forbids_retry_at_the_budget(self) -> None:
        step = make_step(STEP_NO, StepState.REVISE, attempt=3, max_attempts=3)

        assert step.can_retry is False

    def test_aq1_no_override_can_move_the_step(self, config) -> None:
        with composed(config) as composition:
            composition.storage.steps.upsert(
                make_step(STEP_NO, StepState.REVISE, attempt=3, max_attempts=3)
            )

        with composed(config) as composition:
            before = step_of(composition)

            with pytest.raises(OverrideTransitionError):
                composition.human_override.abort_step(
                    STEP_NO, actor="operator", reason="stuck"
                )
            with pytest.raises(OverrideTransitionError):
                composition.human_override.unblock_step(
                    STEP_NO, actor="operator", reason="stuck"
                )

            assert step_of(composition) == before

    def test_aq1_no_retry_api_was_added(self) -> None:
        api = {name for name in dir(HumanOverride) if not name.startswith("_")}

        assert "retry_step" not in api
        assert api == {
            "pause_project",
            "resume_project",
            "set_mode",
            "unblock_step",
            "resolve_step",
            "abort_step",
        }

    def test_aq1_the_monitor_does_not_call_it_blocking(self, config) -> None:
        """The loop waits for a human while the monitor shows a working state."""
        with composed(config) as composition:
            composition.storage.steps.upsert(
                make_step(STEP_NO, StepState.REVISE, attempt=3, max_attempts=3)
            )

        with composed(config) as composition:
            assert composition.health().current_state is StepState.REVISE
            assert composition.monitor.step_states() == ((STEP_NO, "REVISE"),)
            assert composition.monitor.blocking_steps() == ()

    def test_aq2_part_a_the_fsm_still_allows_approve_to_dispatch(self) -> None:
        assert (
            STEP_TRANSITIONS[(StepState.WAITING_APPROVAL, StepEvent.APPROVE)]
            is StepState.DISPATCHED
        )

    def test_aq2_part_a_no_approve_api_exists(self) -> None:
        api = {name for name in dir(HumanOverride) if not name.startswith("_")}

        assert "approve_step" not in api
        assert "reject_step" not in api

    def test_aq2_part_b_a_dispatch_without_a_task_hangs_then_fails(
        self, tmp_path
    ) -> None:
        """AQ-2 contract + downstream consequence reproduced.

        This does **not** reproduce an end-to-end APPROVE workflow: no approve
        API exists. The FSM transition is asserted structurally above, and the
        state an APPROVE *would* produce is seeded directly - ``DISPATCHED`` with
        no task row, no task file and no worker instructions. What follows is the
        documented consequence: the loop never publishes the missing task, no
        report can ever arrive and the attempt dies on the deadline.
        """
        clock = StepClock()
        config = make_config(tmp_path, clock=clock, timeout=timedelta(hours=1))
        adapter = ClineWorkerAdapter(config.exchange_dir)

        with composed(config) as composition:
            composition.storage.steps.upsert(
                make_step(STEP_NO, StepState.DISPATCHED, 1)
            )

        assert adapter.task_path(STEP_NO, 1).exists() is False

        with composed(config) as composition:
            assert composition.storage.tasks.list() == ()

            first = composition.run_until_idle()

            assert first.stopped_because == "worker-report-pending"
            assert step_of(composition).state is StepState.CLINE_WORKING
            # The missing publication is never repaired in place.
            assert composition.storage.tasks.list() == ()
            assert adapter.task_path(STEP_NO, 1).exists() is False

            clock.advance(hours=2)
            tick = composition.orchestrator.run_once()

            assert tick.event is StepEvent.FAIL
            assert tick.from_state is StepState.CLINE_WORKING
            assert tick.to_state is StepState.FAILED

            # Recovery costs a whole attempt: only a *fresh* dispatch publishes
            # a task, and that task belongs to attempt 2.
            composition.run_until_idle()

            assert step_of(composition).attempt == 2
            assert adapter.task_path(STEP_NO, 1).exists() is False
            assert adapter.task_path(STEP_NO, 2).exists() is True

    def test_f1_a_crash_before_the_ack_leaves_the_report_unarchived(
        self, config
    ) -> None:
        """F-1 evidence: reproduced by a real crash, not by a simulation."""
        crash_child("crash_after_verified_before_ack", config)

        with composed(config) as composition:
            assert step_of(composition).state is StepState.VERIFIED
            assert (
                composition.run_until_idle().stopped_because
                == "all-steps-verified"
            )

        assert report_path(config, STEP_NO, 1).is_file()
        assert archived(config, STEP_NO, 1) == []

    def test_f1_completion_never_acknowledges(self, config) -> None:
        """The root cause: a verified step is not current, so no tick acks it."""
        with composed(config) as composition:
            composition.storage.steps.upsert(
                make_step(STEP_NO, StepState.VERIFIED, 1)
            )
            write_report(config, STEP_NO, 1, ReportStatus.DONE)

        with composed(config) as composition:
            assert composition.orchestrator.current_step() is None
            assert (
                composition.run_until_idle().stopped_because
                == "all-steps-verified"
            )

        assert report_path(config, STEP_NO, 1).is_file()
        assert archived(config, STEP_NO, 1) == []


class TestSourceTreeUntouched:
    """Step 22 changed no production code - proven structurally, not promised."""

    def test_the_architecture_self_check_is_still_compliant(self) -> None:
        source = scan_directory(REAL_SOURCE_ROOT)
        result = ArchitectureValidator().validate(source)

        assert result.is_compliant is True
        assert result.violations == ()
        assert result.baseline_version == ARCHITECTURE_CURRENT.version

    def test_the_scanned_package_is_the_assistant_itself(self) -> None:
        modules = scan_directory(REAL_SOURCE_ROOT).modules()

        assert modules
        assert all(name.startswith(ROOT_PACKAGE) for name in modules)
        # Step 21 left 54 production modules. Step 23 adds exactly two - the
        # shared dispatch seam and the approval gate - and nothing else.
        assert len(modules) == 56

    def test_the_crash_driver_is_not_an_architecture_module(self) -> None:
        assert CHILD.parent.name == "tests"
        names = scan_directory(REAL_SOURCE_ROOT).modules()

        assert not [name for name in names if "recovery" in name]


# ---------------------------------------------------------------------------
# the subprocess crash driver
# ---------------------------------------------------------------------------

def run_child(
    scenario: str, config: CompositionConfig
) -> subprocess.CompletedProcess:
    """Run one child scenario in a brand-new Python process."""
    return subprocess.run(
        [
            sys.executable,
            str(CHILD),
            scenario,
            str(config.database_path),
            str(config.exchange_dir),
            str(config.report_dir),
            str(config.source_root),
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )


def crash_child(scenario: str, config: CompositionConfig) -> str:
    """Run a crash scenario and prove the child really died on purpose.

    A child that merely failed to start also exits ``1``, so the ``CRASH_AT``
    marker (printed immediately before ``os._exit(1)``) and the absence of a
    traceback are asserted as well: the death must be *deliberate*.
    """
    completed = run_child(scenario, config)

    assert completed.returncode == 1, completed.stderr
    assert "CRASH_AT " in completed.stdout, completed.stdout
    assert "Traceback" not in completed.stderr, completed.stderr
    return completed.stdout.strip()


# ---------------------------------------------------------------------------
# the mandatory journey
# ---------------------------------------------------------------------------

def advance_two_steps(
    config: CompositionConfig, composition: Composition
) -> None:
    """Reach a non-trivial persisted state.

    * step 1 is ``VERIFIED`` (its report was processed and archived);
    * step 2 is ``CLINE_WORKING`` and waits for the worker;
    * a task row exists, one cost record exists, the audit trail is written;
    * step 2's report is published **before** the restart that follows.
    """
    composition.storage.steps.upsert(make_step(FIRST))
    composition.storage.steps.upsert(make_step(SECOND))

    composition.run_until_idle()
    assert step_of(composition, FIRST).state is StepState.CLINE_WORKING
    write_report(config, FIRST, 1, ReportStatus.DONE)

    composition.run_until_idle()
    assert step_of(composition, FIRST).state is StepState.VERIFIED
    assert step_of(composition, SECOND).state is StepState.CLINE_WORKING

    composition.cost_plugin.record(cost_record(config, step_no=FIRST))
    write_report(config, SECOND, 1, ReportStatus.DONE)


class TestNoFakeSuccess:
    """The harness itself must be honest before any conclusion is drawn."""

    def test_the_recovery_database_is_a_file_not_memory(self, config) -> None:
        assert config.database_path.name == "assistant.db"
        assert str(config.database_path) != ":memory:"

        with composed(config):
            pass

        assert config.database_path.is_file()
        assert config.database_path.stat().st_size > 0

    def test_a_bare_connection_reads_the_authoritative_state(self, config) -> None:
        """No composition, no repository: the state really is on disk."""
        with composed(config) as composition:
            composition.storage.steps.upsert(
                make_step(STEP_NO, StepState.BLOCKED)
            )
            expected_audit = len(state_of(composition)["audit"])

        assert bare_rows(
            config.database_path, "SELECT step_no, state FROM steps"
        ) == [(STEP_NO, "BLOCKED")]
        assert (
            len(bare_rows(config.database_path, "SELECT * FROM audit_entries"))
            == expected_audit
        )

    def test_the_control_child_process_exits_cleanly(self, config) -> None:
        completed = run_child("advance", config)

        assert completed.returncode == 0, completed.stderr
        assert "ADVANCED" in completed.stdout
        assert "Traceback" not in completed.stderr

    def test_a_crash_child_dies_in_its_own_process(self, config) -> None:
        """Only a subprocess can die without taking the test down with it."""
        with composed(config) as composition:
            stdout = crash_child("crash_inside_transition", config)

            assert "audit-append" in stdout
            # The parent is alive, its own connection still works, and it can
            # read what the dead child committed before it died.
            assert step_of(composition).state is StepState.PENDING

        assert bare_rows(config.database_path, "SELECT state FROM steps") == [
            ("PENDING",)
        ]

    def test_the_decisive_journey_never_uses_a_memory_database(
        self, config
    ) -> None:
        with composed(config) as composition:
            advance_two_steps(config, composition)

        rows = bare_rows(
            config.database_path,
            "SELECT step_no, state FROM steps ORDER BY step_no",
        )
        assert rows == [(FIRST, "VERIFIED"), (SECOND, "CLINE_WORKING")]


@contextmanager
def restarted(
    tmp_path: Any,
) -> Iterator[tuple[CompositionConfig, dict, dict, Composition]]:
    """Journey -> full close -> brand-new composition over the same files.

    Yields ``(config, before, previous, second)`` where ``previous`` holds the
    objects of the dead composition (kept only to prove that the new one reuses
    none of them). The decisive acceptance test spells this sequence out in full
    instead of using the helper.
    """
    config = make_config(tmp_path)
    first = compose(config)
    advance_two_steps(config, first)
    before = state_of(first)

    previous = {
        "connection": first.connection,
        "storage": first.storage,
        "orchestrator": first.orchestrator,
        "scheduler": first.scheduler,
        "worker": first.worker,
        "context_builder": first.context_builder,
        "realization_control": first.realization_control,
        "report_builder": first.report_builder,
        "excel_reporting": first.excel_reporting,
        "markdown_reporting": first.markdown_reporting,
        "monitor": first.monitor,
        "human_override": first.human_override,
        "cost_plugin": first.cost_plugin,
        "judge_use_case": first.judge_use_case,
    }

    first.close()
    with pytest.raises(sqlite3.ProgrammingError):
        previous["connection"].execute("SELECT 1")
    del first
    gc.collect()

    second = compose(config)
    try:
        for name, dead in previous.items():
            if dead is None:  # optional capabilities may legitimately be absent
                continue
            assert getattr(second, name) is not dead, name
        yield config, before, previous, second
    finally:
        second.close()


class TestRealRestartRecovery:
    """The mandatory acceptance test: file-backed, full close, full recreate.

    The original ``sqlite3.Connection`` is closed, every Python object of the
    first composition is dropped, and a completely new ``Composition`` is built
    over the same database file and the same exchange directory. Since nothing
    is reused, the recovered state can only come from disk.
    """

    def test_a_file_backed_composition_survives_a_full_close_and_recreate(
        self, tmp_path
    ) -> None:
        # 1. A composition over a real file-backed database inside tmp_path.
        config = make_config(tmp_path)
        assert not config.database_path.exists()

        # 2..4 A non-trivial persisted state, driven through the real loop.
        first = compose(config)
        advance_two_steps(config, first)
        before = state_of(first)
        pending_report = report_path(config, SECOND, 1)
        assert pending_report.is_file()

        # 5. Every authoritative artefact is captured before the disposal.
        assert before["health"].current_step_no == SECOND
        assert before["health"].current_state is StepState.CLINE_WORKING
        assert [step.state for step in before["steps"]] == [
            StepState.VERIFIED,
            StepState.CLINE_WORKING,
        ]
        assert len(before["tasks"]) == 2
        assert before["cost"].record_count == 1
        assert before["versions"] and before["adrs"] and before["requests"]
        assert len(before["audit"]) > 0

        # 6..7 Fix what must not be reused, then dispose everything.
        previous = {
            "connection": first.connection,
            "storage": first.storage,
            "orchestrator": first.orchestrator,
            "scheduler": first.scheduler,
            "worker": first.worker,
            "context_builder": first.context_builder,
            "realization_control": first.realization_control,
            "report_builder": first.report_builder,
            "excel_reporting": first.excel_reporting,
            "markdown_reporting": first.markdown_reporting,
            "monitor": first.monitor,
            "human_override": first.human_override,
            "cost_plugin": first.cost_plugin,
            "judge_use_case": first.judge_use_case,
        }

        # 8..9 The connection is really closed - and provably unusable.
        first.close()
        with pytest.raises(sqlite3.ProgrammingError):
            previous["connection"].execute("SELECT 1")

        # 10. No old Python object survives the disposal.
        del first
        gc.collect()

        # 11..12 A brand-new composition and a brand-new connection.
        second = compose(config)
        try:
            assert second.connection is not previous["connection"]
            for name, dead in previous.items():
                if dead is None:
                    continue
                assert getattr(second, name) is not dead, name

            # 13. Nothing has ticked yet: the state must already be right.
            after = state_of(second)
            assert after == before
            assert after["health"].current_step_no == SECOND
            assert after["health"].current_state is StepState.CLINE_WORKING
            assert [step.attempt for step in after["steps"]] == [1, 1]
            assert len(after["tasks"]) == 2
            assert len(after["audit"]) == len(before["audit"])
            assert before["cost"].record_count == 1

            # 14..16 Continue from the persisted state and process the report
            #        that was published before the restart.
            run = second.run_until_idle()

            assert run.stopped_because == "all-steps-verified"
            assert not pending_report.exists()
            assert len(archived(config, SECOND, 1)) == 1
            assert step_of(second, SECOND).state is StepState.VERIFIED
            assert step_of(second, SECOND).attempt == 1
            assert second.health().complete is True

            # 17. A second run after completion adds nothing.
            finished = state_of(second)
            second.run_until_idle()
            assert state_of(second) == finished
        finally:
            second.close()

    def test_the_state_comes_from_disk_not_from_memory(self, tmp_path) -> None:
        """Proof A: a bare connection - no composition, no repository - sees it."""
        with restarted(tmp_path) as (config, _before, _previous, second):
            assert second.health().current_step_no == SECOND

        # Every composition object is closed and gone by now.
        assert bare_rows(
            config.database_path,
            "SELECT step_no, state FROM steps ORDER BY step_no",
        ) == [(FIRST, "VERIFIED"), (SECOND, "CLINE_WORKING")]
        assert len(bare_rows(config.database_path, "SELECT * FROM tasks")) == 2
        assert (
            len(bare_rows(config.database_path, "SELECT * FROM cost_records")) == 1
        )
        assert len(bare_rows(config.database_path, "SELECT * FROM audit_entries")) > 0

    def test_the_old_connection_is_unusable_after_the_close(self, tmp_path) -> None:
        """Proof B: the close really happened, for every part of the graph."""
        with restarted(tmp_path) as (_config, _before, previous, _second):
            with pytest.raises(sqlite3.ProgrammingError):
                previous["connection"].execute("SELECT 1")
            with pytest.raises(sqlite3.ProgrammingError):
                previous["connection"].execute("SELECT * FROM steps")

    def test_no_python_object_is_reused(self, tmp_path) -> None:
        """Proof C: the new composition shares no object with the dead one."""
        with restarted(tmp_path) as (_config, _before, previous, second):
            assert second.connection is not previous["connection"]
            assert second.storage is not previous["storage"]
            assert second.orchestrator is not previous["orchestrator"]
            assert second.scheduler is not previous["scheduler"]
            assert second.worker is not previous["worker"]
            assert second.report_builder is not previous["report_builder"]
            assert second.monitor is not previous["monitor"]
            assert second.human_override is not previous["human_override"]
            assert second.cost_plugin is not previous["cost_plugin"]

    def test_an_unpersisted_in_memory_change_is_invisible_after_the_restart(
        self, tmp_path
    ) -> None:
        """Proof D: recovery comes from disk, not from a live Python object."""
        config = make_config(tmp_path)
        first = compose(config)
        advance_two_steps(config, first)

        project = first.storage.projects.get(PROJECT_NAME)
        step = step_of(first, SECOND)
        object.__setattr__(project, "paused", True)
        object.__setattr__(step, "state", StepState.ABORTED)
        object.__setattr__(step, "attempt", 99)
        first.close()

        second = compose(config)
        try:
            reloaded_project = second.storage.projects.get(PROJECT_NAME)
            reloaded_step = step_of(second, SECOND)

            assert reloaded_project is not project
            assert reloaded_project.paused is False
            assert reloaded_step is not step
            assert reloaded_step.state is StepState.CLINE_WORKING
            assert reloaded_step.attempt == 1
        finally:
            second.close()

    def test_the_current_step_and_state_are_correct(self, tmp_path) -> None:
        with restarted(tmp_path) as (_config, _before, _previous, second):
            health = second.health()

            assert health.current_step_no == SECOND
            assert health.current_state is StepState.CLINE_WORKING
            assert health.step_count == 2
            assert health.next_step_no is None
            assert health.complete is False
            assert second.monitor.current_step().step_no == SECOND

    def test_the_verified_step_stays_verified(self, tmp_path) -> None:
        with restarted(tmp_path) as (_config, _before, _previous, second):
            first_step = step_of(second, FIRST)

            assert first_step.state is StepState.VERIFIED
            assert first_step.verified_at is not None
            assert first_step.attempt == 1

    def test_the_attempt_counter_is_unchanged_without_a_real_retry(
        self, tmp_path
    ) -> None:
        with restarted(tmp_path) as (_config, before, _previous, second):
            assert [step.attempt for step in before["steps"]] == [1, 1]

            second.run_until_idle()

            assert [
                step.attempt for step in second.storage.steps.list()
            ] == [1, 1]

    def test_no_duplicate_task_row_is_created(self, tmp_path) -> None:
        with restarted(tmp_path) as (_config, before, _previous, second):
            assert len(before["tasks"]) == 2

            second.run_until_idle()

            tasks = second.storage.tasks.list()
            assert len(tasks) == 2
            assert len({(task.step_no, task.attempt) for task in tasks}) == 2

    def test_no_duplicate_cost_record_is_created_on_replay(self, tmp_path) -> None:
        with restarted(tmp_path) as (config, before, _previous, second):
            assert before["cost"].record_count == 1

            # The very same event, replayed after the restart, adds nothing.
            second.cost_plugin.record(cost_record(config, step_no=FIRST))

            assert second.cost_plugin.query(CostQuery()).record_count == 1
            assert (
                len(bare_rows(config.database_path, "SELECT * FROM cost_records"))
                == 1
            )

    def test_no_duplicate_audit_entry_is_created(self, tmp_path) -> None:
        with restarted(tmp_path) as (_config, before, _previous, second):
            # Recreating the composition writes no audit entry at all.
            assert len(state_of(second)["audit"]) == len(before["audit"])

            second.run_until_idle()

            events = [
                entry.detail["event"] for entry in step_entries(second, SECOND)
            ]
            assert events.count("RECEIVE_REPORT") == 1
            assert events.count("START_REVIEW") == 1
            assert events.count("VERIFY") == 1

    def test_no_duplicate_architecture_side_effect_occurs(self, tmp_path) -> None:
        with restarted(tmp_path) as (_config, before, _previous, second):
            # compose() neither re-bootstraps nor re-applies the declared change.
            assert second.storage.architecture_versions.list() == before["versions"]
            assert second.storage.adrs.list() == before["adrs"]
            assert second.storage.change_requests.list() == before["requests"]

            second.run_until_idle()
            finished = state_of(second)

            assert finished["versions"] == before["versions"]
            assert finished["adrs"] == before["adrs"]
            assert finished["requests"] == before["requests"]
            assert [
                version.version
                for version in finished["versions"]
                if version.is_current
            ] == [ARCHITECTURE_CURRENT.version]

    def test_a_pending_report_written_before_the_restart_is_processed_safely(
        self, tmp_path
    ) -> None:
        with restarted(tmp_path) as (config, _before, _previous, second):
            pending = report_path(config, SECOND, 1)
            assert pending.is_file()  # published before the restart
            assert archived(config, SECOND, 1) == []

            second.run_until_idle()

            assert not pending.exists()  # acknowledged only after the outcome
            assert len(archived(config, SECOND, 1)) == 1
            assert step_of(second, SECOND).state is StepState.VERIFIED

    def test_the_final_state_reaches_the_expected_result(self, tmp_path) -> None:
        with restarted(tmp_path) as (_config, _before, _previous, second):
            run = second.run_until_idle()
            finished = state_of(second)

            assert run.stopped_because == "all-steps-verified"
            assert [step.state for step in finished["steps"]] == [
                StepState.VERIFIED,
                StepState.VERIFIED,
            ]
            assert finished["health"].complete is True
            assert finished["cost"].record_count == 1


class TestCrashAfterDispatch:
    """#1 Crash after DISPATCHED, before any worker report exists."""

    def test_the_dispatched_step_survives_the_crash(self, config) -> None:
        stdout = crash_child("crash_after_dispatch_commit", config)
        assert "after-dispatch-commit" in stdout

        with composed(config) as composition:
            step = step_of(composition)

            assert step.state is StepState.DISPATCHED
            assert step.attempt == 1
            assert step.max_attempts == 3

    def test_the_task_identity_survives_the_crash(self, config) -> None:
        crash_child("crash_after_dispatch_commit", config)

        with composed(config) as composition:
            tasks = composition.storage.tasks.list()

            assert [
                (task.step_no, task.attempt) for task in tasks
            ] == [(STEP_NO, 1)]
            assert tasks[0].state is TaskState.DISPATCHED

    def test_the_task_file_was_published_before_the_crash(self, config) -> None:
        crash_child("crash_after_dispatch_commit", config)

        task_file = ClineWorkerAdapter(config.exchange_dir).task_path(STEP_NO, 1)

        assert task_file.is_file()
        assert json.loads(task_file.read_text(encoding="utf-8"))["attempt"] == 1

    def test_no_duplicate_dispatch_happens_after_the_restart(self, config) -> None:
        crash_child("crash_after_dispatch_commit", config)
        adapter = ClineWorkerAdapter(config.exchange_dir)
        task_file = adapter.task_path(STEP_NO, 1)
        published = task_file.read_bytes()

        with composed(config) as composition:
            run = composition.run_until_idle()

            # The loop continues towards the worker; it never re-dispatches.
            assert run.stopped_because == "worker-report-pending"
            assert step_of(composition).state is StepState.CLINE_WORKING
            assert step_of(composition).attempt == 1
            assert task_file.read_bytes() == published
            assert adapter.task_path(STEP_NO, 2).exists() is False


class TestCrashAfterReportBeforePersistence:
    """#2 Crash after the worker report exists, before it was persisted."""

    def test_the_report_survives_the_crash(self, config) -> None:
        crash_child("crash_after_report_written", config)

        report = report_path(config, STEP_NO, 1)

        assert report.is_file()
        assert json.loads(report.read_text(encoding="utf-8"))["status"] == "DONE"
        assert archived(config, STEP_NO, 1) == []

    def test_the_loop_processes_the_surviving_report(self, config) -> None:
        crash_child("crash_after_report_written", config)

        with composed(config) as composition:
            assert step_of(composition).state is StepState.CLINE_WORKING

            run = composition.run_until_idle()

            assert run.stopped_because == "all-steps-verified"
            assert step_of(composition).state is StepState.VERIFIED
            assert len(archived(config, STEP_NO, 1)) == 1
            assert not report_path(config, STEP_NO, 1).exists()


class TestCrashAfterReportReceivedBeforeAck:
    """#3 Crash after ``REPORT_RECEIVED`` was persisted, before the ACK."""

    def test_the_persisted_state_is_report_received(self, config) -> None:
        crash_child("crash_after_report_received", config)

        with composed(config) as composition:
            assert step_of(composition).state is StepState.REPORT_RECEIVED
            assert step_of(composition).attempt == 1

    def test_the_report_is_readable_again_after_the_restart(self, config) -> None:
        crash_child("crash_after_report_received", config)

        with composed(config) as composition:
            result = composition.worker.read_report(STEP_NO, 1)

            assert result is not None
            assert result.status is ReportStatus.DONE
            assert report_path(config, STEP_NO, 1).is_file()

    def test_the_outcome_is_not_duplicated_and_the_ack_follows(
        self, config
    ) -> None:
        crash_child("crash_after_report_received", config)

        with composed(config) as composition:
            before_entries = len(step_entries(composition))

            composition.run_until_idle()

            events = [
                entry.detail["event"] for entry in step_entries(composition)
            ]
            assert events.count("RECEIVE_REPORT") == 1
            assert events.count("VERIFY") == 1
            assert len(step_entries(composition)) == before_entries + 2
            assert len(archived(config, STEP_NO, 1)) == 1
            assert not report_path(config, STEP_NO, 1).exists()


class TestCrashInReviewing:
    """#4 Crash while the step is ``REVIEWING``."""

    def test_reviewing_survives_the_crash(self, config) -> None:
        stdout = crash_child("crash_in_reviewing", config)
        assert "in-reviewing" in stdout

        with composed(config) as composition:
            assert step_of(composition).state is StepState.REVIEWING
            assert step_of(composition).attempt == 1
            assert report_path(config, STEP_NO, 1).is_file()

    def test_the_review_resumes_deterministically(self, config) -> None:
        crash_child("crash_in_reviewing", config)

        with composed(config) as composition:
            composition.run_until_idle()

            events = [
                entry.detail["event"] for entry in step_entries(composition)
            ]
            assert step_of(composition).state is StepState.VERIFIED
            assert events.count("VERIFY") == 1
            assert len(archived(config, STEP_NO, 1)) == 1

    def test_there_is_no_silent_state_reset(self, config) -> None:
        """The crash left ``REVIEWING`` on disk; nothing resets it to ``READY``."""
        crash_child("crash_in_reviewing", config)

        with composed(config) as composition:
            assert step_of(composition).state is StepState.REVIEWING
            assert composition.health().current_state is StepState.REVIEWING
            assert composition.monitor.step_states() == ((STEP_NO, "REVIEWING"),)
            assert composition.monitor.blocking_steps() == ()


class TestCrashAfterVerifiedBeforeAck:
    """#5 (F-1) Crash after the ``VERIFIED`` commit, before the report ACK."""

    def test_the_verified_outcome_survives_the_crash(self, config) -> None:
        stdout = crash_child("crash_after_verified_before_ack", config)
        assert "acknowledge_report" in stdout

        with composed(config) as composition:
            assert step_of(composition).state is StepState.VERIFIED
            assert step_of(composition).attempt == 1
            assert composition.health().complete is True

    def test_the_final_state_is_never_applied_twice(self, config) -> None:
        crash_child("crash_after_verified_before_ack", config)

        with composed(config) as composition:
            before = state_of(composition)

            run = composition.run_until_idle()

            assert run.stopped_because == "all-steps-verified"
            assert state_of(composition) == before
            assert (
                [
                    entry.detail["event"]
                    for entry in step_entries(composition)
                ].count("VERIFY")
                == 1
            )

    def test_f1_the_un_acked_report_is_never_archived(self, config) -> None:
        """F-1 evidence: the crash window leaves the report unacknowledged."""
        crash_child("crash_after_verified_before_ack", config)
        report = report_path(config, STEP_NO, 1)
        assert report.is_file()
        assert archived(config, STEP_NO, 1) == []

        with composed(config) as composition:
            for _ in range(3):
                composition.run_until_idle()

        assert report.is_file()  # still sitting on the channel
        assert archived(config, STEP_NO, 1) == []  # and never archived

    def test_f1_the_root_cause_is_current_step_selection(self, config) -> None:
        """A verified step is no longer current, so no tick ever acks it."""
        crash_child("crash_after_verified_before_ack", config)

        with composed(config) as composition:
            assert composition.orchestrator.current_step() is None
            assert (
                composition.run_until_idle().stopped_because
                == "all-steps-verified"
            )
            assert composition.storage.audit.list() != ()


class TestCrashDuringRetry:
    """#6 Crash inside the retry flow."""

    def test_the_attempt_is_not_incremented_by_the_crash(self, config) -> None:
        stdout = crash_child("crash_during_retry", config)
        assert "audit-append" in stdout

        with composed(config) as composition:
            step = step_of(composition)

            # The bump and the state change rolled back together.
            assert step.state is StepState.REVISE
            assert step.attempt == 1
            assert step.max_attempts == 3

    def test_the_retry_then_completes_exactly_once(self, config) -> None:
        crash_child("crash_during_retry", config)

        with composed(config) as composition:
            before_entries = len(step_entries(composition))

            composition.run_until_idle()

            entries = step_entries(composition)
            new_events = [
                entry.detail["event"] for entry in entries[before_entries:]
            ]

            assert new_events.count("RETRY") == 1
            assert new_events == ["RETRY", "DISPATCH", "CLINE_START"]
            assert step_of(composition).attempt == 2
            assert step_of(composition).state is StepState.CLINE_WORKING

    def test_the_attempt_budget_invariant_survives_a_restart(self, config) -> None:
        """``attempt <= max_attempts`` holds across a restart at the boundary."""
        with composed(config) as composition:
            composition.storage.steps.upsert(
                make_step(STEP_NO, StepState.REVISE, attempt=3, max_attempts=3)
            )

        with composed(config) as composition:
            assert step_of(composition).attempt == 3

            run = composition.run_until_idle()

            # The budget is spent: the loop waits, it never invents a fourth try.
            assert run.stopped_because == "revise-attempts-exhausted"
            assert step_of(composition).attempt == 3

        assert bare_rows(
            config.database_path, "SELECT attempt, max_attempts FROM steps"
        ) == [(3, 3)]


class TestCrashDuringOverride:
    """#7 Crash inside a Human Override transaction: both or neither."""

    def test_a_step_override_killed_mid_transaction_rolls_back(self, config) -> None:
        stdout = crash_child("crash_inside_override_step", config)
        assert "audit-append" in stdout

        with composed(config) as composition:
            assert step_of(composition).state is StepState.BLOCKED  # no mutation
            assert step_entries(composition) == []  # and no audit entry

    def test_the_step_override_succeeds_after_the_crash(self, config) -> None:
        crash_child("crash_inside_override_step", config)

        with composed(config) as composition:
            composition.human_override.unblock_step(
                STEP_NO, actor="operator", reason="after the crash"
            )

            assert step_of(composition).state is StepState.READY
            assert len(step_entries(composition)) == 1

    def test_a_project_override_killed_mid_transaction_rolls_back(
        self, config
    ) -> None:
        stdout = crash_child("crash_inside_override_project", config)
        assert "audit-append" in stdout

        with composed(config) as composition:
            assert composition.storage.projects.get(PROJECT_NAME).paused is False

        assert (
            bare_rows(
                config.database_path,
                "SELECT * FROM audit_entries WHERE entity_type = 'PROJECT'",
            )
            == []
        )

    def test_the_project_override_succeeds_after_the_crash(self, config) -> None:
        crash_child("crash_inside_override_project", config)

        with composed(config) as composition:
            composition.human_override.pause_project(
                actor="operator", reason="after the crash"
            )

            assert composition.storage.projects.get(PROJECT_NAME).paused is True

        assert (
            len(
                bare_rows(
                    config.database_path,
                    "SELECT * FROM audit_entries WHERE entity_type = 'PROJECT'",
                )
            )
            == 1
        )


class TestCrashDuringCost:
    """#8 Crash during Cost Plugin persistence."""

    def test_a_committed_cost_event_survives_the_crash(self, config) -> None:
        stdout = crash_child("crash_after_cost_commit", config)
        assert "after-cost-commit" in stdout

        with composed(config) as composition:
            summary = composition.cost_plugin.query(CostQuery())

            assert summary.record_count == 1
            assert summary.total_usd == 0.5

    def test_a_replay_after_the_crash_adds_no_second_row(self, config) -> None:
        crash_child("crash_after_cost_commit", config)

        with composed(config) as composition:
            composition.cost_plugin.record(cost_record(config))

            assert composition.cost_plugin.query(CostQuery()).record_count == 1
            assert (
                len(bare_rows(config.database_path, "SELECT * FROM cost_records"))
                == 1
            )

    def test_a_conflicting_replay_after_the_crash_fails_closed(
        self, config
    ) -> None:
        crash_child("crash_after_cost_commit", config)

        with composed(config) as composition:
            with pytest.raises(CostIdentityConflictError):
                composition.cost_plugin.record(cost_record(config, cost_usd=9.99))

            summary = composition.cost_plugin.query(CostQuery())
            assert summary.record_count == 1
            assert summary.total_usd == 0.5  # the stored event is untouched

    def test_a_cost_write_killed_inside_the_transaction_rolls_back(
        self, config
    ) -> None:
        stdout = crash_child("crash_inside_cost", config)
        assert "step-upsert" in stdout

        with composed(config) as composition:
            assert composition.cost_plugin.query(CostQuery()).record_count == 0

        assert bare_rows(config.database_path, "SELECT * FROM cost_records") == []


#: (state, report present before the restart, expected stop reason, expected attempt)
RESTART_MATRIX = (
    (StepState.BLOCKED, False, "human-unblock-required", 1),
    (StepState.WAITING_APPROVAL, False, "human-approval-required", 1),
    (StepState.DISPATCHED, False, "worker-report-pending", 1),
    (StepState.REPORT_RECEIVED, True, "all-steps-verified", 1),
    (StepState.REVIEWING, True, "all-steps-verified", 1),
    (StepState.REVISE, False, "worker-report-pending", 2),
    (StepState.FAILED, False, "worker-report-pending", 2),
    (StepState.CONFLICT, False, "human-conflict-resolution-required", 1),
    (StepState.ABORTED, False, "step-aborted", 1),
)


class TestRestartFromEveryPersistedState:
    """#9 Restart from each important persisted state.

    A persisted state is an *input* here, so it is seeded straight into the
    file-backed source of truth; what is asserted is the recovery behaviour that
    follows the restart. The crash classes reach several of these states through
    the real loop instead.
    """

    @pytest.mark.parametrize(
        "state, report_present, expected_reason, expected_attempt",
        RESTART_MATRIX,
        ids=[row[0].value for row in RESTART_MATRIX],
    )
    def test_the_state_survives_and_the_loop_continues(
        self, config, state, report_present, expected_reason, expected_attempt
    ) -> None:
        with composed(config) as composition:
            composition.storage.steps.upsert(make_step(STEP_NO, state, attempt=1))
            if report_present:
                write_report(config, STEP_NO, 1, ReportStatus.DONE)

        with composed(config) as composition:
            # Nothing has ticked yet: the state survived exactly, unre-set.
            assert step_of(composition).state is state
            assert step_of(composition).attempt == 1

            run = composition.run_until_idle()

            assert run.stopped_because == expected_reason
            assert step_of(composition).attempt == expected_attempt

    def test_a_restart_alone_writes_no_audit_entry(self, config) -> None:
        with composed(config) as composition:
            composition.storage.steps.upsert(make_step(STEP_NO, StepState.BLOCKED))
            before = composition.storage.audit.list()

        with composed(config) as composition:
            assert composition.storage.audit.list() == before
            assert composition.monitor.step_states() == ((STEP_NO, "BLOCKED"),)

    def test_an_aborted_step_stays_aborted(self, config) -> None:
        with composed(config) as composition:
            composition.storage.steps.upsert(
                make_step(STEP_NO, StepState.ABORTED, attempt=1)
            )

        with composed(config) as composition:
            assert step_of(composition).state is StepState.ABORTED
            assert composition.run_until_idle().stopped_because == "step-aborted"
            assert composition.health().complete is False
