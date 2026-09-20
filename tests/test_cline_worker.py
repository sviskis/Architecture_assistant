"""Tests for the Cline worker adapter.

Focus: the split read/acknowledge lifecycle, attempt-scoped file identity,
context-before-task dispatch ordering, fail-safe handling of corrupt or
mismatching reports, and the at-least-once delivery model.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

import pytest

import architecture_assistant.infrastructure.cline as cline_module
from architecture_assistant.domain.enums import Phase, ReportStatus, RiskLevel
from architecture_assistant.domain.models import Task
from architecture_assistant.infrastructure import (
    ClineWorkerAdapter,
    ClineWorkerError,
    ReportMismatchError,
    ReportNotAvailableError,
    ReportParseError,
    TaskDispatchError,
)
from architecture_assistant.ports import WorkerPort, WorkerRequest, WorkerResult

NOW = datetime(2026, 9, 20, 20, 0, tzinfo=timezone.utc)


def fixed_clock() -> datetime:
    return NOW


def make_task(step_no: int = 7, attempt: int = 1) -> Task:
    return Task(
        step_no=step_no,
        phase=Phase.CLINE,
        title="Cline Worker Adapter",
        description="Structured task/report contract",
        risk=RiskLevel.HIGH,
        attempt=attempt,
        max_attempts=3,
        instructions={"plan_first": True},
        report_schema={"status": "DONE|REVISE|BLOCKED|FAILED"},
        created_at=NOW,
    )


def make_request(
    step_no: int = 7,
    attempt: int = 1,
    context: Optional[Mapping[str, Any]] = None,
) -> WorkerRequest:
    return WorkerRequest(
        task=make_task(step_no, attempt),
        context={"step": step_no} if context is None else dict(context),
    )


def report_payload(step_no: int = 7, attempt: int = 1, **overrides: Any) -> dict:
    payload: dict[str, Any] = {
        "step_no": step_no,
        "attempt": attempt,
        "status": "DONE",
        "summary": "did the work",
        "files_created": ["src/x.py"],
        "files_changed": ["src/y.py"],
        "files_deleted": [],
        "tests": {"passed": 10, "failed": 0, "command": "pytest -q"},
        "dependencies_added": [],
        "architecture_questions": [],
        "issues": [],
    }
    payload.update(overrides)
    return payload


def write_report(
    adapter: ClineWorkerAdapter, step_no: int = 7, attempt: int = 1, **overrides
) -> Path:
    path = adapter.report_path(step_no, attempt)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report_payload(step_no, attempt, **overrides)),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def exchange(tmp_path: Path) -> Path:
    return tmp_path / "data" / "cline"


@pytest.fixture
def adapter(exchange: Path) -> ClineWorkerAdapter:
    return ClineWorkerAdapter(
        exchange,
        project="Architecture Lifecycle Assistant",
        plan_version="0.2",
        clock=fixed_clock,
    )


class TestFileIdentity:
    def test_file_names_include_step_and_attempt(self, adapter) -> None:
        assert (
            adapter.task_path(7, 1).name == "step_007_attempt_001_task.json"
        )
        assert (
            adapter.context_path(7, 1).name
            == "step_007_attempt_001_context.json"
        )
        assert (
            adapter.report_path(7, 1).name
            == "step_007_attempt_001_report.json"
        )

    def test_different_attempts_use_different_files(self, adapter) -> None:
        assert adapter.task_path(7, 1) != adapter.task_path(7, 2)
        assert adapter.report_path(7, 1) != adapter.report_path(7, 2)

    def test_exchange_root_is_injectable(self, exchange: Path) -> None:
        adapter = ClineWorkerAdapter(exchange)
        assert adapter.exchange_dir == exchange
        assert exchange in adapter.task_path(7, 1).parents

    def test_clock_must_be_callable(self, exchange: Path) -> None:
        with pytest.raises(ValueError, match="clock must be a callable"):
            ClineWorkerAdapter(exchange, clock="not-a-clock")  # type: ignore[arg-type]


class TestDispatch:
    def test_writes_context_and_task(self, adapter) -> None:
        path = adapter.dispatch(make_request())
        assert path == adapter.task_path(7, 1)
        assert path.is_file()
        assert adapter.context_path(7, 1).is_file()

    def test_context_is_written_before_the_task(self, adapter, monkeypatch) -> None:
        calls: list[str] = []
        real = cline_module._atomic_write_text

        def recording(target, text):
            calls.append(Path(target).name)
            return real(target, text)

        monkeypatch.setattr(cline_module, "_atomic_write_text", recording)
        adapter.dispatch(make_request())

        assert calls == [
            "step_007_attempt_001_context.json",
            "step_007_attempt_001_task.json",
        ]

    def test_context_write_failure_never_publishes_the_task(
        self, adapter, monkeypatch
    ) -> None:
        real = cline_module._atomic_write_text

        def failing(target, text):
            if "context" in Path(target).name:
                raise OSError("disk full")
            return real(target, text)

        monkeypatch.setattr(cline_module, "_atomic_write_text", failing)

        with pytest.raises(TaskDispatchError, match="context"):
            adapter.dispatch(make_request())

        assert not adapter.task_path(7, 1).exists()
        assert not adapter.context_path(7, 1).exists()

    def test_task_write_failure_leaves_no_task(self, adapter, monkeypatch) -> None:
        real = cline_module._atomic_write_text

        def failing(target, text):
            if "task" in Path(target).name:
                raise OSError("disk full")
            return real(target, text)

        monkeypatch.setattr(cline_module, "_atomic_write_text", failing)

        with pytest.raises(TaskDispatchError, match="task"):
            adapter.dispatch(make_request())

        assert not adapter.task_path(7, 1).exists()
        assert adapter.read_report(7, 1) is None

    def test_task_payload_carries_the_contract_header(self, adapter) -> None:
        adapter.dispatch(make_request())
        payload = json.loads(adapter.task_path(7, 1).read_text(encoding="utf-8"))

        assert payload["protocol"] == cline_module.DEFAULT_PROTOCOL
        assert payload["project"] == "Architecture Lifecycle Assistant"
        assert payload["plan_version"] == "0.2"
        assert payload["step_no"] == 7
        assert payload["attempt"] == 1
        assert payload["max_attempts"] == 3
        assert payload["phase"] == "CLINE"
        assert payload["risk"] == "HIGH"
        assert payload["title"] == "Cline Worker Adapter"
        assert payload["instructions"] == {"plan_first": True}
        assert payload["report_schema"] == {
            "status": "DONE|REVISE|BLOCKED|FAILED"
        }

    def test_context_file_points_at_the_written_context(self, adapter) -> None:
        adapter.dispatch(make_request())
        payload = json.loads(adapter.task_path(7, 1).read_text(encoding="utf-8"))
        referenced = Path(payload["context_file"])
        assert referenced == adapter.context_path(7, 1).resolve()
        assert referenced.is_file()

    def test_context_payload_is_the_serialized_context(self, adapter) -> None:
        adapter.dispatch(
            make_request(context={"step": 7, "project": {"name": "X"}})
        )
        payload = json.loads(
            adapter.context_path(7, 1).read_text(encoding="utf-8")
        )
        assert payload == {"step": 7, "project": {"name": "X"}}

    def test_dispatch_is_idempotent(self, adapter) -> None:
        request = make_request()
        first = adapter.dispatch(request)
        before = first.read_text(encoding="utf-8")
        second = adapter.dispatch(request)
        assert first == second
        assert second.read_text(encoding="utf-8") == before

    def test_dispatch_leaves_no_temporary_files(self, adapter, exchange) -> None:
        adapter.dispatch(make_request())
        assert list(exchange.rglob("*.tmp")) == []

    def test_dispatch_for_a_second_attempt_uses_new_files(self, adapter) -> None:
        adapter.dispatch(make_request(attempt=1))
        adapter.dispatch(make_request(attempt=2))
        assert adapter.task_path(7, 1).is_file()
        assert adapter.task_path(7, 2).is_file()
        assert adapter.context_path(7, 2).is_file()



class TestReadReport:
    def test_returns_none_when_no_report_exists(self, adapter) -> None:
        assert adapter.read_report(7, 1) is None

    def test_maps_a_valid_report(self, adapter) -> None:
        write_report(adapter)
        result = adapter.read_report(7, 1)

        assert isinstance(result, WorkerResult)
        assert result.status is ReportStatus.DONE
        assert result.summary == "did the work"
        assert result.artifacts == ("src/x.py",)
        assert result.issues == ()
        assert result.architecture_questions == ()
        assert result.raw["files_changed"] == ["src/y.py"]
        assert result.raw["tests"]["passed"] == 10

    def test_maps_issues_and_architecture_questions(self, adapter) -> None:
        write_report(
            adapter,
            status="BLOCKED",
            issues=["missing spec"],
            architecture_questions=["which store?"],
        )
        result = adapter.read_report(7, 1)
        assert result is not None
        assert result.status is ReportStatus.BLOCKED
        assert result.issues == ("missing spec",)
        assert result.architecture_questions == ("which store?",)

    def test_read_does_not_consume_the_report(self, adapter) -> None:
        path = write_report(adapter)
        adapter.read_report(7, 1)
        assert path.is_file()

    def test_two_reads_before_ack_return_the_same_result(self, adapter) -> None:
        write_report(adapter)
        first = adapter.read_report(7, 1)
        second = adapter.read_report(7, 1)
        assert first is not None
        assert first == second

    def test_read_ignores_temporary_siblings(self, adapter) -> None:
        path = adapter.report_path(7, 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        (path.parent / f".{path.name}.partial.tmp").write_text(
            json.dumps(report_payload()), encoding="utf-8"
        )
        assert not path.exists()
        assert adapter.read_report(7, 1) is None

    def test_other_attempts_are_independent(self, adapter) -> None:
        write_report(adapter, attempt=1)
        assert adapter.read_report(7, 1) is not None
        assert adapter.read_report(7, 2) is None



class TestReportValidation:
    def test_wrong_step_is_a_mismatch(self, adapter) -> None:
        path = write_report(adapter)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["step_no"] = 6
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(ReportMismatchError, match="step_no"):
            adapter.read_report(7, 1)

    def test_wrong_attempt_is_a_mismatch(self, adapter) -> None:
        path = write_report(adapter)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["attempt"] = 2
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(ReportMismatchError, match="attempt"):
            adapter.read_report(7, 1)

    def test_missing_attempt_is_a_parse_error(self, adapter) -> None:
        path = write_report(adapter)
        payload = json.loads(path.read_text(encoding="utf-8"))
        del payload["attempt"]
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(ReportParseError, match="attempt must be an int"):
            adapter.read_report(7, 1)

    def test_missing_step_no_is_a_parse_error(self, adapter) -> None:
        path = write_report(adapter)
        payload = json.loads(path.read_text(encoding="utf-8"))
        del payload["step_no"]
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(ReportParseError, match="step_no must be an int"):
            adapter.read_report(7, 1)

    def test_unknown_status_is_a_parse_error(self, adapter) -> None:
        write_report(adapter, status="MAYBE")
        with pytest.raises(ReportParseError, match="invalid status"):
            adapter.read_report(7, 1)

    def test_summary_must_be_a_string(self, adapter) -> None:
        write_report(adapter, summary=5)
        with pytest.raises(ReportParseError, match="summary must be a string"):
            adapter.read_report(7, 1)

    def test_string_list_fields_must_be_lists_of_strings(self, adapter) -> None:
        write_report(adapter, files_created=[1, 2])
        with pytest.raises(ReportParseError, match="files_created"):
            adapter.read_report(7, 1)

    def test_corrupt_json_is_a_parse_error(self, adapter) -> None:
        path = adapter.report_path(7, 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json at all", encoding="utf-8")
        with pytest.raises(ReportParseError, match="not valid JSON"):
            adapter.read_report(7, 1)

    def test_truncated_json_is_a_parse_error(self, adapter) -> None:
        path = adapter.report_path(7, 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(report_payload())
        path.write_text(text[: len(text) // 2], encoding="utf-8")
        with pytest.raises(ReportParseError):
            adapter.read_report(7, 1)

    def test_non_object_json_is_a_parse_error(self, adapter) -> None:
        path = adapter.report_path(7, 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        with pytest.raises(ReportParseError, match="must contain a JSON object"):
            adapter.read_report(7, 1)

    def test_invalid_report_is_left_untouched(self, adapter) -> None:
        path = write_report(adapter, status="NOPE")
        before = path.read_text(encoding="utf-8")

        with pytest.raises(ReportParseError):
            adapter.read_report(7, 1)

        assert path.is_file()
        assert path.read_text(encoding="utf-8") == before

    def test_stale_attempt_report_is_left_untouched(self, adapter) -> None:
        path = write_report(adapter)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["attempt"] = 9
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(ReportMismatchError):
            adapter.read_report(7, 1)

        assert path.is_file()



class TestAcknowledge:
    def test_moves_the_report_into_the_archive(self, adapter) -> None:
        path = write_report(adapter)
        target = adapter.acknowledge_report(7, 1)

        assert not path.exists()
        assert target.is_file()
        assert target.parent == adapter.archive_dir()
        assert (
            target.name
            == "step_007_attempt_001_report_20260920_200000.json"
        )

    def test_archive_keeps_an_exact_copy(self, adapter) -> None:
        path = write_report(adapter)
        before = path.read_text(encoding="utf-8")
        target = adapter.acknowledge_report(7, 1)
        assert target.read_text(encoding="utf-8") == before

    def test_acknowledging_twice_is_an_explicit_error(self, adapter) -> None:
        write_report(adapter)
        adapter.acknowledge_report(7, 1)
        with pytest.raises(ReportNotAvailableError):
            adapter.acknowledge_report(7, 1)

    def test_acknowledge_without_a_report_is_an_explicit_error(
        self, adapter
    ) -> None:
        with pytest.raises(ReportNotAvailableError, match="no report"):
            adapter.acknowledge_report(7, 1)

    def test_acknowledge_refuses_an_invalid_report(self, adapter) -> None:
        path = write_report(adapter, status="NOPE")
        with pytest.raises(ReportParseError):
            adapter.acknowledge_report(7, 1)
        assert path.is_file()

    def test_acknowledge_refuses_a_stale_attempt(self, adapter) -> None:
        path = write_report(adapter)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["attempt"] = 9
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(ReportMismatchError):
            adapter.acknowledge_report(7, 1)

        assert path.is_file()

    def test_acknowledge_only_affects_the_requested_attempt(self, adapter) -> None:
        write_report(adapter, attempt=1)
        write_report(adapter, attempt=2)
        adapter.acknowledge_report(7, 1)
        assert not adapter.report_path(7, 1).exists()
        assert adapter.report_path(7, 2).is_file()


class TestRestartSafety:
    def test_read_is_repeatable_across_adapter_instances(self, exchange) -> None:
        first = ClineWorkerAdapter(exchange, clock=fixed_clock)
        write_report(first)
        before = first.read_report(7, 1)

        second = ClineWorkerAdapter(exchange, clock=fixed_clock)
        after = second.read_report(7, 1)

        assert before is not None
        assert after == before

    def test_active_report_survives_until_acknowledged(self, exchange) -> None:
        writer = ClineWorkerAdapter(exchange, clock=fixed_clock)
        write_report(writer)
        assert writer.read_report(7, 1) is not None

        restarted = ClineWorkerAdapter(exchange, clock=fixed_clock)
        assert restarted.report_path(7, 1).is_file()
        assert restarted.read_report(7, 1) is not None

        restarted.acknowledge_report(7, 1)

        after_ack = ClineWorkerAdapter(exchange, clock=fixed_clock)
        assert not after_ack.report_path(7, 1).is_file()
        assert after_ack.read_report(7, 1) is None
        archived = list(
            after_ack.archive_dir().glob("step_007_attempt_001_report_*.json")
        )
        assert len(archived) == 1

    def test_dispatch_survives_a_restart(self, exchange) -> None:
        first = ClineWorkerAdapter(exchange, clock=fixed_clock)
        first.dispatch(make_request())

        restarted = ClineWorkerAdapter(exchange, clock=fixed_clock)
        assert restarted.task_path(7, 1).is_file()
        assert restarted.context_path(7, 1).is_file()



class TestRunConformance:
    def test_satisfies_worker_port(self, adapter) -> None:
        assert isinstance(adapter, WorkerPort)

    def test_run_dispatches_and_returns_the_result(self, adapter) -> None:
        write_report(adapter)
        result = adapter.run(make_request())

        assert result.status is ReportStatus.DONE
        assert adapter.task_path(7, 1).is_file()

    def test_run_raises_when_the_report_is_not_ready(self, adapter) -> None:
        with pytest.raises(ReportNotAvailableError, match="not available yet"):
            adapter.run(make_request())
        # the task was dispatched before the single read attempt
        assert adapter.task_path(7, 1).is_file()

    def test_run_does_not_acknowledge(self, adapter) -> None:
        path = write_report(adapter)
        adapter.run(make_request())
        assert path.is_file()

    def test_run_has_no_polling_loop_or_sleep(self) -> None:
        source = Path(cline_module.__file__ or "").read_text(encoding="utf-8")
        assert "import time" not in source
        assert "time.sleep" not in source
        assert "while True" not in source
        assert "asyncio" not in source


class TestBoundary:
    def test_worker_result_keeps_cline_fields_only_in_raw(self, adapter) -> None:
        write_report(adapter)
        result = adapter.read_report(7, 1)

        assert result is not None
        assert set(result.to_dict()) == {
            "status",
            "summary",
            "artifacts",
            "issues",
            "architecture_questions",
            "raw",
        }
        assert result.raw["step_no"] == 7
        assert result.raw["attempt"] == 1
        assert result.raw["tests"]["command"] == "pytest -q"
        assert result.raw["files_changed"] == ["src/y.py"]

    def test_worker_result_exposes_no_cline_header_fields(self, adapter) -> None:
        write_report(adapter)
        result = adapter.read_report(7, 1)
        assert result is not None
        for cline_field in ("protocol", "project", "plan_version", "context_file"):
            assert cline_field not in result.to_dict()

    def test_errors_share_an_adapter_base(self) -> None:
        for error in (
            TaskDispatchError,
            ReportNotAvailableError,
            ReportParseError,
            ReportMismatchError,
        ):
            assert issubclass(error, ClineWorkerError)

    def test_adapter_module_has_no_sqlite_knowledge(self) -> None:
        source = Path(cline_module.__file__ or "").read_text(encoding="utf-8")
        assert "sqlite3" not in source

    def test_adapter_does_not_import_the_application_layer(self) -> None:
        source = Path(cline_module.__file__ or "").read_text(encoding="utf-8")
        assert "from ..application" not in source
        assert "from ..architecture" not in source

    def test_acknowledge_uses_the_injected_clock(self, exchange) -> None:
        adapter = ClineWorkerAdapter(
            exchange,
            clock=lambda: datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        )
        write_report(adapter)
        target = adapter.acknowledge_report(7, 1)
        assert target.name == "step_007_attempt_001_report_20300102_030405.json"

    def test_registry_can_hold_the_adapter_as_a_worker(self, adapter) -> None:
        from architecture_assistant.application import PluginRegistry

        registry = PluginRegistry()
        registry.register("worker", "cline", adapter)
        assert registry.get_default("worker") is adapter

