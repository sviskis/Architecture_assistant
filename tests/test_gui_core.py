"""Tests for the GUI's core-thread worker, against a real composition.

The worker is the only place the panel touches the assistant: it composes on one
thread, returns plain payloads and resolves the current step itself. These tests
run the real core on a file-backed database and assert the properties the plan
promised - plain data out, read-only audit access, exports through the existing
adapters, fail-closed human actions, and one owner thread per connection.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from architecture_assistant.composition import (
    CompositionConfig,
    compose,
)
from architecture_assistant.domain.enums import (
    Mode,
    Phase,
    RiskLevel,
    StepState,
)
from architecture_assistant.domain.models import Step
from architecture_assistant.infrastructure import ClineWorkerAdapter
from architecture_assistant_gui.core import (
    AUDIT_TAIL_LIMIT,
    BackgroundRunner,
    CoreError,
    CoreWorker,
    JobResult,
    describe_error,
    is_critical,
)

#: A fixed stamp: no GUI test depends on the wall clock.
NOW = datetime(2026, 9, 21, 22, 0, tzinfo=timezone.utc)

#: The assistant's package: the source tree the realization gate inspects.
CORE_ROOT = (
    Path(__file__).resolve().parents[1] / "src" / "architecture_assistant"
)


def _clock() -> datetime:
    return NOW


def make_config(
    tmp_path: Any, *, mode: Mode = Mode.AUTO, supervision: bool = False
) -> CompositionConfig:
    """A file-backed configuration the worker can compose."""
    return CompositionConfig(
        database_path=tmp_path / "assistant.db",
        exchange_dir=tmp_path / "cline",
        report_dir=tmp_path / "reports",
        source_root=CORE_ROOT,
        mode=mode,
        supervision=supervision,
        clock=_clock,
    )


def make_step(
    state: StepState = StepState.READY,
    attempt: int = 1,
    max_attempts: int = 3,
    *,
    requires_human: bool = False,
) -> Step:
    """One step the GUI can act on."""
    return Step(
        step_no=9,
        phase=Phase.LOOP,
        title="GUI step",
        state=state,
        attempt=attempt,
        max_attempts=max_attempts,
        risk=RiskLevel.LOW,
        requires_human=requires_human,
        created_at=NOW,
        last_update_at=NOW,
    )


def seed(config: CompositionConfig, *steps: Step) -> None:
    """Seed the plan through the storage port, then release the connection."""
    composition = compose(config)
    try:
        for step in steps:
            composition.storage.steps.upsert(step)
    finally:
        composition.close()


def audit_count(receiver: Any, limit: int = 1000) -> int:
    """The audit length seen through the worker's own read-only view."""
    return len(receiver.audit_tail(limit))


def report_text(
    *,
    step_no: int = 9,
    attempt: int = 1,
    status: str = "DONE",
    summary: str = "the step is done",
) -> str:
    """One valid worker report, exactly as the channel contract requires."""
    return json.dumps(
        {
            "step_no": step_no,
            "attempt": attempt,
            "status": status,
            "summary": summary,
            "files_created": [],
            "files_changed": [],
            "files_deleted": [],
            "tests": {"passed": 3, "failed": 0, "command": "pytest -q"},
            "dependencies_added": [],
            "architecture_questions": [],
            "issues": [],
        },
        indent=2,
    )


def write_report(
    config: CompositionConfig,
    text: str,
    *,
    step_no: int = 9,
    attempt: int = 1,
) -> Path:
    """Write one worker report into the real Cline exchange channel."""
    channel = ClineWorkerAdapter(
        config.exchange_dir, project="Project", clock=_clock
    )
    path = channel.report_path(step_no, attempt)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def supervised(tmp_path):
    """A supervisor-enabled worker factory over a real file-backed database.

    Seeding happens through the storage port in a separate, closed composition -
    exactly like the plan loader would - and the worker is then opened the way
    the panel opens it: one connection, one owner.
    """
    opened: list[CoreWorker] = []

    def factory(
        *,
        report: Optional[str] = None,
        step: Optional[Step] = None,
    ) -> CoreWorker:
        config = make_config(tmp_path, supervision=True)
        seed(
            config,
            step
            if step is not None
            else make_step(state=StepState.REPORT_RECEIVED),
        )
        if report is not None:
            write_report(config, report)
        instance = CoreWorker(config)
        instance.open()
        opened.append(instance)
        return instance

    yield factory
    for instance in opened:
        instance.close()


@pytest.fixture
def worker(tmp_path):
    """An open worker over a fresh file-backed database."""
    config = make_config(tmp_path)
    instance = CoreWorker(config)
    instance.open()
    try:
        yield instance
    finally:
        instance.close()


class TestPayload:
    """One fresh read of the canonical projection, as plain data."""

    def test_the_payload_carries_the_canonical_keys(self, worker) -> None:
        payload = worker.payload()

        assert set(payload) == {
            "canonical",
            "project",
            "health",
            "steps",
            "tasks",
            "cost",
            "architecture",
            "architecture_version",
            "current_step",
            "next_step",
            "blocking_steps",
            "open_risks",
            "change_requests",
            "paths",
            # read-only advisor configuration, so the review tab can label its
            # three panes before the operator has paid for a review
            "reviewers",
        }
        assert payload["reviewers"] == ["OpenAI", "Claude", "Grok"]
        assert set(payload["canonical"]) == {
            "schema_version",
            "generated_at",
            "project",
            "steps",
            "tasks",
            "architecture",
            "architecture_versions",
            "adrs",
            "risks",
            "findings",
            "decisions",
            "change_requests",
            "cost",
            "cost_by_step",
            "health",
        }

    def test_the_payload_is_plain_and_json_serializable(self, worker) -> None:
        """Rule 3: only dict/list/scalar data crosses to the UI thread."""
        payload = worker.payload()

        assert json.loads(json.dumps(payload))["health"]["step_count"] == 0

    def test_the_projection_matches_the_monitor(self, tmp_path) -> None:
        config = make_config(tmp_path)
        seed(config, make_step(StepState.BLOCKED))
        instance = CoreWorker(config)
        instance.open()
        try:
            payload = instance.payload()
            reference = compose(config)
            try:
                assert payload["project"] == reference.monitor.to_dict()[
                    "project"
                ]
                assert payload["steps"] == reference.monitor.to_dict()["steps"]
                assert payload["health"] == reference.monitor.to_dict()["health"]
                assert payload["architecture_version"] == (
                    reference.monitor.architecture_version()
                )
                assert payload["blocking_steps"] == [
                    {"step_no": step.step_no, "state": step.state.value}
                    for step in reference.monitor.blocking_steps()
                ]
                assert payload["open_risks"] == [
                    risk.to_dict() for risk in reference.monitor.open_risks()
                ]
            finally:
                reference.close()
        finally:
            instance.close()

    def test_an_empty_database_reports_no_steps(self, worker) -> None:
        """Startup on a fresh database: nothing to run, nothing to decide."""
        payload = worker.payload()

        assert payload["steps"] == []
        assert payload["health"]["step_count"] == 0
        assert payload["current_step"] is None
        assert payload["next_step"] is None
        assert payload["blocking_steps"] == []

    def test_the_current_and_blocking_steps_come_from_the_core(
        self, tmp_path
    ) -> None:
        config = make_config(tmp_path)
        seed(config, make_step(StepState.BLOCKED))
        instance = CoreWorker(config)
        instance.open()
        try:
            payload = instance.payload()

            assert payload["current_step"]["step_no"] == 9
            assert payload["current_step"]["state"] == "BLOCKED"
            assert payload["blocking_steps"] == [
                {"step_no": 9, "state": "BLOCKED"}
            ]
        finally:
            instance.close()


class TestAuditTail:
    """The audit panel reads the append-only trail and never writes it."""

    def test_only_the_most_recent_entries_are_returned_newest_first(
        self, worker
    ) -> None:
        rows = worker.audit_tail(5)

        assert len(rows) == 5
        assert all(
            set(row) == {
                "created_at",
                "entity_type",
                "entity_id",
                "action",
                "event",
                "actor",
                "step_no",
                "detail",
            }
            for row in rows
        )
        # Newest first: the timestamps never increase down the list.
        stamps = [row["created_at"] for row in rows]
        assert stamps == sorted(stamps, reverse=True)

    def test_reading_the_trail_changes_nothing(self, tmp_path) -> None:
        config = make_config(tmp_path)
        instance = CoreWorker(config)
        instance.open()
        try:
            before = audit_count(instance)
            rows = instance.audit_tail()
            assert isinstance(rows, list)
            assert len(rows) == min(before, AUDIT_TAIL_LIMIT)
            assert audit_count(instance) == before
        finally:
            instance.close()

    def test_the_limit_takes_the_newest_entries(self, worker) -> None:
        everything = worker.audit_tail(1000)

        assert worker.audit_tail(5) == everything[:5]

    def test_a_bad_limit_is_refused(self, worker) -> None:
        with pytest.raises(CoreError):
            worker.audit_tail(0)


class TestLoopCommand:
    """Run Until Idle is one explicit command - never an automatic poll."""

    def test_the_loop_reports_why_it_stopped(self, worker) -> None:
        result = worker.run_until_idle()

        assert result["stopped_because"] == "no-steps"
        assert json.dumps(result)

    def test_a_ready_step_is_dispatched_by_one_run(self, tmp_path) -> None:
        config = make_config(tmp_path)
        seed(config, make_step(StepState.READY))
        instance = CoreWorker(config)
        instance.open()
        try:
            run = instance.run_until_idle()
            payload = instance.payload()

            assert run["stopped_because"] == "worker-report-pending"
            assert payload["current_step"]["state"] == "CLINE_WORKING"
            assert payload["tasks"][0]["state"] == "DISPATCHED"
            assert payload["tasks"][0]["report_status"] is None
        finally:
            instance.close()


class TestHumanActions:
    """Every human action goes through the core paths and is audited there."""

    def test_pause_and_resume_toggle_the_project_flag(self, worker) -> None:
        before = audit_count(worker)

        paused = worker.pause_project(actor="operator", reason="maintenance")
        assert paused == {"action": "pause_project", "paused": True}
        assert worker.payload()["project"]["paused"] is True
        assert audit_count(worker) == before + 1

        resumed = worker.resume_project(actor="operator", reason="finished")
        assert resumed == {"action": "resume_project", "paused": False}
        assert worker.payload()["project"]["paused"] is False
        assert audit_count(worker) == before + 2

    def test_pausing_twice_fails_closed_in_the_core(self, worker) -> None:
        worker.pause_project(actor="operator", reason="first")

        with pytest.raises(Exception) as info:
            worker.pause_project(actor="operator", reason="again")

        assert type(info.value).__name__ == "OverrideNoChangeError"

    def test_the_reason_and_actor_are_recorded_verbatim(self, worker) -> None:
        worker.pause_project(actor="alice", reason="maintenance window")

        entry = json.loads(worker.audit_tail(1)[0]["detail"])

        assert entry["actor"] == "alice"
        assert entry["reason"] == "maintenance window"
        assert entry["operation"] == "pause_project"

    def test_approve_moves_a_requested_step_forward(self, tmp_path) -> None:
        config = make_config(tmp_path)
        seed(config, make_step(StepState.READY, requires_human=True))
        instance = CoreWorker(config)
        instance.open()
        try:
            stopped = instance.run_until_idle()["stopped_because"]
            assert stopped == "human-approval-required"
            before = audit_count(instance)

            approved = instance.approve(
                actor="operator", reason="plan reviewed"
            )

            assert approved == {
                "action": "approve",
                "step_no": 9,
                "state": "DISPATCHED",
                "attempt": 1,
                "max_attempts": 3,
            }
            assert audit_count(instance) == before + 1
            assert instance.payload()["current_step"]["state"] == "DISPATCHED"
        finally:
            instance.close()

    def test_reject_returns_the_step_to_ready(self, tmp_path) -> None:
        config = make_config(tmp_path)
        seed(config, make_step(StepState.READY, requires_human=True))
        instance = CoreWorker(config)
        instance.open()
        try:
            instance.run_until_idle()
            before = audit_count(instance)

            rejected = instance.reject(
                actor="operator", reason="not good enough"
            )

            assert rejected["state"] == "READY"
            assert rejected["step_no"] == 9
            assert audit_count(instance) == before + 1
            assert instance.payload()["tasks"] == []
        finally:
            instance.close()

    def test_unblock_returns_the_step_to_ready(self, tmp_path) -> None:
        config = make_config(tmp_path)
        seed(config, make_step(StepState.BLOCKED))
        instance = CoreWorker(config)
        instance.open()
        try:
            before = audit_count(instance)

            unblocked = instance.unblock(
                actor="operator", reason="dependency resolved"
            )

            assert unblocked["state"] == "READY"
            assert audit_count(instance) == before + 1
        finally:
            instance.close()

    def test_abort_is_legal_from_a_blocked_step(self, tmp_path) -> None:
        config = make_config(tmp_path)
        seed(config, make_step(StepState.BLOCKED))
        instance = CoreWorker(config)
        instance.open()
        try:
            before = audit_count(instance)

            aborted = instance.abort(actor="operator", reason="no longer needed")

            assert aborted["state"] == "ABORTED"
            assert audit_count(instance) == before + 1
            assert instance.payload()["health"]["complete"] is False
        finally:
            instance.close()

    def test_resolve_requires_a_conflicting_step(self, tmp_path) -> None:
        config = make_config(tmp_path)
        seed(config, make_step(StepState.READY))
        instance = CoreWorker(config)
        instance.open()
        try:
            with pytest.raises(Exception) as info:
                instance.resolve(actor="operator", reason="resolved")

            assert type(info.value).__name__ == "OverrideTransitionError"
        finally:
            instance.close()

    def test_a_human_action_without_a_current_step_is_refused(self, worker) -> None:
        with pytest.raises(CoreError):
            worker.unblock(actor="operator", reason="nothing to unblock")


class TestExports:
    """The panel renders through the existing reporting adapters."""

    def test_markdown_export_uses_the_core_adapter(
        self, tmp_path, monkeypatch
    ) -> None:
        from architecture_assistant.infrastructure import (
            MarkdownReportingAdapter,
        )

        calls: list[dict[str, Any]] = []
        original = MarkdownReportingAdapter.render

        def spy(self: Any, payload: Any) -> str:
            calls.append(dict(payload))
            return original(self, payload)

        monkeypatch.setattr(MarkdownReportingAdapter, "render", spy)

        config = make_config(tmp_path)
        instance = CoreWorker(config)
        instance.open()
        try:
            result = instance.export_markdown()
        finally:
            instance.close()

        assert len(calls) == 1
        assert "health" in calls[0] and "steps" in calls[0]
        artifact = Path(result["path"])
        assert artifact.is_file()
        assert artifact.suffix == ".md"
        assert artifact.parent == Path(config.report_dir).resolve()

    def test_excel_export_uses_the_core_adapter(
        self, tmp_path, monkeypatch
    ) -> None:
        from architecture_assistant.infrastructure import (
            ExcelReportingAdapter,
        )

        calls: list[dict[str, Any]] = []
        original = ExcelReportingAdapter.render

        def spy(self: Any, payload: Any) -> str:
            calls.append(dict(payload))
            return original(self, payload)

        monkeypatch.setattr(ExcelReportingAdapter, "render", spy)

        config = make_config(tmp_path)
        instance = CoreWorker(config)
        instance.open()
        try:
            result = instance.export_excel()
        finally:
            instance.close()

        assert len(calls) == 1
        artifact = Path(result["path"])
        assert artifact.is_file()
        assert artifact.suffix == ".xlsx"
        assert artifact.parent == Path(config.report_dir).resolve()

    def test_exporting_changes_no_domain_state(self, tmp_path) -> None:
        config = make_config(tmp_path)
        seed(config, make_step(StepState.BLOCKED))
        instance = CoreWorker(config)
        instance.open()
        try:
            before = instance.payload()
            count = audit_count(instance)

            instance.export_markdown()
            instance.export_excel()

            after = instance.payload()
            assert after["steps"] == before["steps"]
            assert after["architecture"] == before["architecture"]
            assert after["cost"] == before["cost"]
            assert audit_count(instance) == count
        finally:
            instance.close()


class TestOwnership:
    """One thread owns the connection; reconnect never repairs anything."""

    def test_reconnect_keeps_state_and_writes_nothing(self, tmp_path) -> None:
        config = make_config(tmp_path)
        seed(config, make_step(StepState.BLOCKED))
        instance = CoreWorker(config)
        instance.open()
        try:
            before = instance.payload()
            count = audit_count(instance)

            instance.reconnect()

            after = instance.payload()
            assert instance.is_open is True
            assert after["steps"] == before["steps"]
            assert after["project"] == before["project"]
            assert after["canonical"]["architecture_versions"] == (
                before["canonical"]["architecture_versions"]
            )
            assert audit_count(instance) == count
        finally:
            instance.close()

    def test_close_releases_the_composition(self, tmp_path) -> None:
        config = make_config(tmp_path)
        instance = CoreWorker(config)
        instance.open()

        assert instance.is_open is True
        instance.close()
        assert instance.is_open is False
        with pytest.raises(CoreError):
            instance.payload()

    def test_the_connection_may_only_be_used_by_its_own_thread(
        self, tmp_path
    ) -> None:
        """Why the runner exists: sqlite3 binds a connection to its thread."""
        config = make_config(tmp_path)
        instance = CoreWorker(config)
        opened = threading.Event()
        release = threading.Event()
        outcome: dict[str, str] = {}

        def core_thread() -> None:
            instance.open()
            opened.set()
            release.wait(5)
            instance.close()
            outcome["closed"] = "yes"

        thread = threading.Thread(target=core_thread, name="owner")
        thread.start()
        assert opened.wait(5) is True
        try:
            with pytest.raises(Exception) as info:
                instance.payload()
            outcome["cross"] = type(info.value).__name__
        finally:
            release.set()
            thread.join(5)

        assert outcome["cross"] == "ProgrammingError"
        assert outcome["closed"] == "yes"


def _drain(runner: Any, timeout: float = 15.0) -> list[Any]:
    """Poll a runner until it is idle - never used by the application itself."""
    import time

    deadline = time.monotonic() + timeout
    results: list[Any] = []
    while time.monotonic() < deadline:
        results.extend(runner.poll())
        if results and not runner.is_busy:
            return results
        time.sleep(0.01)
    raise AssertionError("the runner did not finish in time")


class TestBackgroundRunner:
    """One core thread, one job at a time, results as plain data."""

    def test_start_composes_on_the_core_thread(self, tmp_path) -> None:
        config = make_config(tmp_path)
        instance = CoreWorker(config)
        runner = BackgroundRunner(instance)
        try:
            runner.start()
            results = _drain(runner)

            assert [result.label for result in results] == ["open"]
            assert results[0].ok is True
            assert results[0].error is None
            assert instance.is_open is True
        finally:
            runner.stop()

    def test_a_job_runs_off_the_main_thread_and_reports_busy(
        self, tmp_path
    ) -> None:
        config = make_config(tmp_path)
        instance = CoreWorker(config)
        runner = BackgroundRunner(instance)
        seen: dict[str, Any] = {}

        def action(worker: Any) -> dict[str, Any]:
            seen["thread"] = threading.current_thread()
            seen["busy_during"] = runner.is_busy
            return {"ok": True}

        try:
            runner.start()
            _drain(runner)
            assert runner.submit("probe", action) is True
            results = _drain(runner)
        finally:
            runner.stop()

        assert [result.label for result in results] == ["probe"]
        assert results[0].payload == {"ok": True}
        assert seen["thread"] is not threading.main_thread()
        assert seen["busy_during"] is True
        assert runner.is_busy is False

    def test_only_one_job_runs_at_a_time(self, tmp_path) -> None:
        config = make_config(tmp_path)
        instance = CoreWorker(config)
        runner = BackgroundRunner(instance)
        release = threading.Event()

        try:
            runner.start()
            _drain(runner)

            assert runner.submit("slow", lambda worker: release.wait(5)) is True
            # A second action is refused while the first one is in flight.
            assert runner.submit("second", lambda worker: "never") is False

            release.set()
            results = _drain(runner)
            assert [result.label for result in results] == ["slow"]
        finally:
            release.set()
            runner.stop()

    def test_a_failing_job_is_reported_not_raised(self, tmp_path) -> None:
        config = make_config(tmp_path)
        instance = CoreWorker(config)
        runner = BackgroundRunner(instance)

        def boom(worker: Any) -> None:
            raise _named_error("RealizationControlError")

        try:
            runner.start()
            _drain(runner)
            assert runner.submit("boom", boom) is True
            results = _drain(runner)
        finally:
            runner.stop()

        assert len(results) == 1
        assert results[0].ok is False
        assert results[0].error.error_name == "RealizationControlError"
        assert results[0].error.critical is True
        assert json.dumps(results[0].error.to_dict())

    def test_stop_closes_the_composition_on_the_core_thread(
        self, tmp_path
    ) -> None:
        config = make_config(tmp_path)
        instance = CoreWorker(config)
        runner = BackgroundRunner(instance)

        runner.start()
        _drain(runner)
        assert instance.is_open is True

        runner.stop()

        assert runner.is_running is False
        assert instance.is_open is False

    def test_the_main_thread_cannot_reach_the_composition(self, tmp_path) -> None:
        """The runner owns the connection; the UI thread only sees payloads."""
        config = make_config(tmp_path)
        instance = CoreWorker(config)
        runner = BackgroundRunner(instance)
        try:
            runner.start()
            _drain(runner)

            with pytest.raises(Exception) as info:
                instance.payload()

            assert type(info.value).__name__ == "ProgrammingError"
        finally:
            runner.stop()

    def test_the_runner_refuses_a_bad_job(self, tmp_path) -> None:
        config = make_config(tmp_path)
        runner = BackgroundRunner(CoreWorker(config))

        with pytest.raises(ValueError):
            runner.submit("", lambda worker: None)
        with pytest.raises(ValueError):
            runner.submit("label", "not callable")


def _named_error(name: str) -> Exception:
    """An exception whose class *name* is ``name`` (classification is by name)."""
    return type(name, (Exception,), {})(f"{name} raised for the test")


class TestErrorClassification:
    """CRITICAL means "do not trust the source of truth this session"."""

    def test_invariant_and_database_errors_are_critical(self) -> None:
        for name in (
            "RealizationControlError",
            "LoopInvariantError",
            "BootstrapConflictError",
            "ProgrammingError",
            "IntegrityError",
            "OperationalError",
            "ReportingInvariantError",
        ):
            error = _named_error(name)

            assert is_critical(error) is True
            report = describe_error(error)
            assert report.critical is True
            assert report.error_name == name
            assert report.message

    def test_an_ordinary_action_error_is_not_critical(self) -> None:
        for name in (
            "ApprovalTransitionError",
            "OverrideNoChangeError",
            "OverrideTransitionError",
            "ReportParseError",
        ):
            error = _named_error(name)

            assert is_critical(error) is False
            assert describe_error(error).critical is False

    def test_the_report_is_plain_data(self) -> None:
        report = describe_error(_named_error("LoopInvariantError"))

        rendered = json.loads(json.dumps(report.to_dict()))

        assert rendered["critical"] is True
        assert rendered["error_name"] == "LoopInvariantError"


class TestSupervisionCore:
    """Advisory supervision through the core worker: the panel's whole API.

    Real file-backed SQLite, a real Cline exchange channel and the offline
    scripted supervisor the composition wires - no provider, no key, no network.
    """

    def test_supervision_is_off_by_default_and_says_so(self, worker) -> None:
        status = worker.supervisor_status()

        assert status["enabled"] is False
        assert status["supervision"] is None
        assert "no current step" in status["reason"]
        assert isinstance(json.dumps(status), str)

    def test_a_tick_without_a_report_waits_for_it_when_enabled(
        self, supervised
    ) -> None:
        worker = supervised()
        before = audit_count(worker)

        tick = worker.supervisor_tick()

        assert tick["result"]["state"] == "RUNNING"
        assert tick["result"]["outcome"] == "no-report"
        assert tick["result"]["waiting_for"] == "report"
        assert tick["result"]["provider_called"] is False
        assert audit_count(worker) == before
        assert isinstance(json.dumps(tick), str)

    def test_a_tick_without_a_step_reports_no_step_when_enabled(
        self, tmp_path
    ) -> None:
        worker = CoreWorker(make_config(tmp_path, supervision=True))
        worker.open()
        try:
            tick = worker.supervisor_tick()

            assert tick["result"]["state"] == "RUNNING"
            assert tick["result"]["outcome"] == "no-step"
            assert tick["result"]["provider_called"] is False
            assert worker.supervisor_status()["reason"].startswith(
                "there is no current step to supervise"
            )
        finally:
            worker.close()

    def test_a_tick_of_a_disabled_session_does_nothing_at_all(self, worker) -> None:
        """The default worker: supervision off, so the tick is inert."""
        before = audit_count(worker)

        tick = worker.supervisor_tick()

        assert tick["result"]["state"] == "STOPPED"
        assert tick["result"]["outcome"] == "disabled"
        assert tick["result"]["provider_called"] is False
        assert tick["supervisor"]["runtime"]["tick_count"] == 0
        assert audit_count(worker) == before
        assert isinstance(json.dumps(tick), str)

    def test_a_valid_report_is_analysed_once_and_allows_the_review(
        self, supervised
    ) -> None:
        worker = supervised(report=report_text())

        first = worker.supervisor_tick()
        analysis = worker.analyze_report()["analysis"]

        assert first["result"]["outcome"] == "analyzed"
        assert first["result"]["provider_called"] is True
        assert analysis["outcome"] == "already-supervised"
        assert analysis["status"] == "NO_ACTION"

        payload = worker.supervisor_status()["supervision"]
        assert payload["record"]["status"] == "NO_ACTION"
        assert payload["record"]["provider"] == "scripted"
        assert payload["waiting_for"] == "authoritative-review"
        assert payload["verdict"]["allowed"] is True
        assert payload["verdict"]["reason"] == "supervision-no_action"
        assert (
            payload["current_report_hash"]
            == payload["record"]["source_report_hash"]
        )
        assert isinstance(json.dumps(payload), str)

    def test_an_unchanged_report_costs_no_provider_call_and_no_audit_entry(
        self, supervised
    ) -> None:
        worker = supervised(report=report_text())

        worker.supervisor_tick()
        before = audit_count(worker)
        second = worker.supervisor_tick()

        assert second["result"]["outcome"] == "unchanged"
        assert second["result"]["provider_called"] is False
        assert second["supervisor"]["runtime"]["tick_count"] == 2
        assert second["supervisor"]["runtime"]["provider_calls"] == 1
        assert audit_count(worker) == before

    def test_a_malformed_report_blocks_the_gate_without_a_provider_call(
        self, supervised
    ) -> None:
        worker = supervised(report="{ this is not a worker report")

        analysis = worker.analyze_report()["analysis"]

        assert analysis["outcome"] == "malformed"
        assert analysis["status"] == "MALFORMED"
        assert analysis["provider_called"] is False
        assert analysis["gate_allowed"] is False

        payload = worker.supervisor_status()["supervision"]
        assert payload["record"]["status"] == "MALFORMED"
        assert payload["verdict"]["allowed"] is False
        assert payload["waiting_for"] == "blocked"

    def test_a_malformed_report_can_only_be_waived_by_a_human(
        self, supervised
    ) -> None:
        worker = supervised(report="{ this is not a worker report")
        worker.analyze_report()
        before = audit_count(worker)

        with pytest.raises(Exception) as refused:
            worker.approve_and_send(
                "fix the report", actor="gints", reason="repair"
            )
        assert type(refused.value).__name__ == "SupervisionNotSendableError"

        waived = worker.waive_supervision(
            actor="gints", reason="I read the bytes myself"
        )

        assert waived["record"]["status"] == "WAIVED"
        assert waived["record"]["decided_by"] == "gints"
        assert waived["record"]["decision_reason"] == "I read the bytes myself"
        payload = waived["supervisor"]["supervision"]
        assert payload["verdict"]["allowed"] is True
        assert payload["waiting_for"] == "authoritative-review"
        assert audit_count(worker) == before + 1

    def test_waiving_is_refused_when_nothing_is_waiting(self, supervised) -> None:
        worker = supervised(report=report_text())
        worker.analyze_report()

        with pytest.raises(Exception) as refused:
            worker.waive_supervision(actor="gints", reason="nothing to waive")

        assert type(refused.value).__name__ == "SupervisionNotSendableError"

    def test_an_unsupervised_report_needs_an_analysis_first(self, supervised) -> None:
        worker = supervised()

        analysis = worker.analyze_report()["analysis"]
        assert analysis["outcome"] == "no-report"
        assert analysis["waiting_for"] == "report"
        assert worker.supervisor_status()["supervision"]["record"] is None

        for call in (
            lambda: worker.approve_and_send(
                "fix it", actor="gints", reason="repair"
            ),
            lambda: worker.reject_directive(actor="gints", reason="no"),
            lambda: worker.waive_supervision(actor="gints", reason="waive it"),
            lambda: worker.escalate_supervision(actor="gints", reason="ask a human"),
        ):
            with pytest.raises(CoreError) as refused:
                call()
            assert "analyze it first" in str(refused.value)

    def test_a_supervision_action_without_a_step_fails_closed(self, worker) -> None:
        for call in (
            lambda: worker.analyze_report(),
            lambda: worker.approve_and_send(
                "fix it", actor="gints", reason="repair"
            ),
            lambda: worker.waive_supervision(actor="gints", reason="waive it"),
        ):
            with pytest.raises(CoreError) as refused:
                call()
            assert "no current step" in str(refused.value)

    def test_the_actor_and_the_reason_are_required_by_the_core(
        self, supervised
    ) -> None:
        worker = supervised(report=report_text())
        worker.analyze_report()

        with pytest.raises(Exception) as refused:
            worker.waive_supervision(actor="", reason="because")
        assert type(refused.value).__name__ == "SupervisionActorRequiredError"

        with pytest.raises(Exception) as refused:
            worker.waive_supervision(actor="gints", reason="   ")
        assert type(refused.value).__name__ == "SupervisionReasonRequiredError"

    def test_the_supervision_actions_return_plain_data(self, supervised) -> None:
        worker = supervised(report=report_text())
        worker.supervisor_tick()

        for payload in (
            worker.supervisor_status(),
            worker.analyze_report(),
            worker.supervisor_tick(),
        ):
            assert isinstance(json.loads(json.dumps(payload)), dict)


def open_worker(
    tmp_path: Any,
    *,
    report: Optional[str] = None,
    supervision: bool = True,
) -> CoreWorker:
    """Seed one step (and optionally a report), then open the core worker."""
    config = make_config(tmp_path, supervision=supervision)
    seed(config, make_step(state=StepState.REPORT_RECEIVED))
    if report is not None:
        write_report(config, report)
    instance = CoreWorker(config)
    instance.open()
    return instance


def supervision_rows(tmp_path: Any, *, supervision: bool = True) -> tuple[Any, ...]:
    """The persisted supervision rows, read through a separate composition.

    Called **after** the worker has closed, so the count can only have been
    produced by the calls the test made - never by opening the database.
    """
    reader = compose(make_config(tmp_path, supervision=supervision))
    try:
        return reader.storage.supervisions.list()
    finally:
        reader.close()


class TestDisabledSupervisionIsEnforcedInCore:
    """``supervision=False`` is enforced by the application, not by the panel.

    Every test here calls the **core worker** - the exact surface a host, a
    script or a future CLI would use - so a GUI that forgets to disable a button
    is not what keeps a disabled session inert.
    """

    def test_a_disabled_core_analyses_nothing_and_calls_no_provider(
        self, tmp_path
    ) -> None:
        worker = open_worker(tmp_path, report=report_text(), supervision=False)
        try:
            before = audit_count(worker)

            analysis = worker.analyze_report()["analysis"]

            assert analysis["outcome"] == "disabled"
            assert analysis["status"] == "DISABLED"
            assert analysis["provider_called"] is False
            assert analysis["waiting_for"] == "none"
            assert audit_count(worker) == before

            tick = worker.supervisor_tick()

            assert tick["result"]["outcome"] == "disabled"
            assert tick["result"]["state"] == "STOPPED"
            assert tick["result"]["provider_called"] is False
            runtime = tick["supervisor"]["runtime"]
            assert runtime["running"] is False
            assert runtime["tick_count"] == 0
            assert runtime["reconcile_count"] == 0
            assert runtime["provider_calls"] == 0
            assert audit_count(worker) == before

            status = worker.supervisor_status()
            payload = status["supervision"]

            assert status["enabled"] is False
            assert payload["enabled"] is False
            assert payload["record"] is None
            assert payload["history"] == []
            assert payload["current_report_hash"] is None
            assert payload["context"] is None
            assert payload["waiting_for"] == "none"
            assert payload["verdict"]["reason"] == "supervision-disabled"
            assert isinstance(json.dumps(status), str)
        finally:
            worker.close()

        assert supervision_rows(tmp_path, supervision=False) == ()

    def test_a_disabled_core_writes_no_row_no_audit_and_no_directive(
        self, tmp_path
    ) -> None:
        worker = open_worker(tmp_path, report=report_text(), supervision=False)
        try:
            worker.analyze_report()
            worker.supervisor_tick()

            entries = worker.audit_tail(1000)
            assert not [
                entry
                for entry in entries
                if entry.get("entity_type") == "SUPERVISION"
            ]
        finally:
            worker.close()

        assert supervision_rows(tmp_path, supervision=False) == ()
        assert not list((tmp_path / "cline").rglob("*directive*"))

    def test_a_disabled_core_refuses_every_human_decision(self, tmp_path) -> None:
        worker = open_worker(tmp_path, report=report_text(), supervision=False)
        try:
            for call in (
                lambda: worker.approve_and_send(
                    "add the pytest command", actor="gints", reason="repair"
                ),
                lambda: worker.reject_directive(actor="gints", reason="no"),
                lambda: worker.waive_supervision(actor="gints", reason="waive"),
                lambda: worker.escalate_supervision(actor="gints", reason="ask"),
            ):
                with pytest.raises(CoreError) as refused:
                    call()
                assert "disabled" in str(refused.value)
        finally:
            worker.close()

        assert supervision_rows(tmp_path, supervision=False) == ()

    def test_enabled_supervision_is_unchanged_by_the_guard(self, tmp_path) -> None:
        worker = open_worker(tmp_path, report=report_text(), supervision=True)
        try:
            analysis = worker.analyze_report()["analysis"]

            assert analysis["outcome"] == "analyzed"
            assert analysis["status"] == "NO_ACTION"
            assert analysis["provider_called"] is True
            payload = worker.supervisor_status()["supervision"]
            assert payload["enabled"] is True
            assert payload["record"]["status"] == "NO_ACTION"
            assert payload["waiting_for"] == "authoritative-review"
        finally:
            worker.close()

        assert len(supervision_rows(tmp_path, supervision=True)) == 1

    def test_a_disabled_core_makes_the_panel_offer_nothing(self, tmp_path) -> None:
        """The real payload of a disabled core, fed to the real controller."""
        from architecture_assistant_gui.controller import GuiController

        class _Runner:
            """The smallest stand-in for the background runner."""

            def __init__(self) -> None:
                self.jobs: list[tuple[str, Any]] = []
                self.busy = False

            def start(self) -> None:
                pass

            @property
            def is_busy(self) -> bool:
                return self.busy

            def submit(self, label: str, action: Any) -> bool:
                self.jobs.append((label, action))
                return True

        runner = _Runner()
        controller = GuiController(
            runner, actor="gints", reports_dir=str(tmp_path / "reports")
        )
        core = open_worker(tmp_path, report=report_text(), supervision=False)
        try:
            controller.apply_result(
                JobResult(
                    label="refresh",
                    payload={
                        "payload": core.payload(),
                        "audit": core.audit_tail(50),
                        "supervisor": core.supervisor_status(),
                    },
                )
            )

            view = controller.view_model()["supervisor"]

            assert view["enabled"] is False
            assert "disabled" in view["status"]
            for key in (
                "analyze_report",
                "approve_and_send",
                "reject_directive",
                "waive_supervision",
                "escalate_supervision",
            ):
                assert controller.supervisor_action_available(key) is False, key
                assert controller.enabled(controller.intent(key)) is False, key
                assert "disabled" in str(controller.supervisor_refusal(key)), key
                assert controller.submit(key, reason="a reason") is False, key
            assert runner.jobs == []
        finally:
            core.close()
