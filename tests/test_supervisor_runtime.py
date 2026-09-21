"""Tests for the supervision runtime, its send protocol and the human controls.

What this file proves, in order:

* a **cheap tick** - no report, no provider call; an unchanged report, no second
  call; a new report, exactly one call; and the same for the startup reconcile;
* **malformed bytes** never reach a supervisor, always block, and escalate on two
  *persisted* deadlines - one per hash, one absolute that no rewrite resets;
* the **send protocol** - a durable intent, an at-least-once publication and a
  durable completion, reconciled honestly after either half of a crash;
* the **human controls** - approve, reject, waive and escalate all need an exact
  supervision id plus an actor and a reason, all revalidate the identity first,
  and none of them can un-send a delivered directive;
* the **audit trail** - one entry per durable decision, and nothing at all for a
  polling tick.

Everything runs against a real SQLite source of truth and a real Cline file
channel with an offline scripted supervisor.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from architecture_assistant.application import (
    Supervision,
    SupervisionActorRequiredError,
    SupervisionGate,
    SupervisionNotSendableError,
    SupervisionReasonRequiredError,
    SupervisionStaleError,
    SupervisorRuntime,
    WaitingFor,
)
from architecture_assistant.domain.audit import AuditEntityType
from architecture_assistant.domain.enums import (
    Mode,
    Phase,
    RiskLevel,
    StepState,
    SupervisorAction,
    SupervisorRisk,
    SupervisorStatus,
)
from architecture_assistant.domain.models import Project, Step
from architecture_assistant.infrastructure import (
    ClineWorkerAdapter,
    ScriptedSupervisor,
    SqliteStorage,
    close_database,
    no_action,
    open_database,
    scripted_result,
)

NOW = datetime(2026, 9, 21, 22, 0, tzinfo=timezone.utc)
PROJECT = "pilot"
ARCH = "1.1"
ACTOR = "gints"
REASON = "supervise the report"


class FakeClock:
    """Deterministic, advanceable clock."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **parts: float) -> None:
        self.now = self.now + timedelta(**parts)


def report_text(
    step_no: int,
    *,
    attempt: int = 1,
    status: str = "DONE",
    summary: str = "the step is done",
    **extra,
) -> str:
    payload = {
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
    }
    payload.update(extra)
    return json.dumps(payload, indent=2)


class Rig:
    """A real storage + real Cline channel + the supervision trio.

    Deliberately without an orchestrator: this file is about the runtime, the
    send protocol and the human controls, none of which need the loop. The loop
    and its two gates are covered by ``test_supervision_gate.py``.
    """

    def __init__(
        self,
        tmp_path: Path,
        *,
        mode: Mode = Mode.MANUAL,
        responses: tuple = (),
        default=None,
        step_no: int = 3,
        attempt: int = 1,
        max_attempts: int = 3,
        state: StepState = StepState.REPORT_RECEIVED,
        paused: bool = False,
        supervision: bool = True,
        stability: timedelta = timedelta(seconds=30),
        timeout: timedelta = timedelta(seconds=120),
    ) -> None:
        self.connection = open_database(":memory:")
        self.storage = SqliteStorage(self.connection)
        self.clock = FakeClock()
        self.worker = ClineWorkerAdapter(
            tmp_path / "cline", project=PROJECT, clock=self.clock
        )
        self.storage.projects.upsert(
            Project(
                name=PROJECT,
                plan_version="1.0",
                mode=mode,
                paused=paused,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        self.step_no = step_no
        self.attempt = attempt
        self.storage.steps.upsert(
            Step(
                step_no=step_no,
                phase=Phase.LOOP,
                title="the supervised step",
                description="do the thing",
                state=state,
                attempt=attempt,
                max_attempts=max_attempts,
                risk=RiskLevel.LOW,
                created_at=NOW,
                last_update_at=NOW,
            )
        )
        self.mode = mode
        self.paused = paused
        self.supervision_enabled = supervision
        self.stability = stability
        self.timeout = timeout
        self.supervisor = ScriptedSupervisor(
            responses, default=no_action() if default is None else default
        )
        self.events: list = []
        self._build()

    def _build(self) -> None:
        self.gate = SupervisionGate(
            self.storage,
            self.worker,
            architecture_version=ARCH,
            enabled=self.supervision_enabled,
            clock=self.clock,
        )
        self.supervision = Supervision(
            self.storage,
            self.worker,
            self.supervisor,
            self.gate,
            malformed_stability=self.stability,
            malformed_timeout=self.timeout,
        )
        self.runtime = SupervisorRuntime(
            self.storage, self.supervision, clock=self.clock
        )

    def restart(self) -> None:
        """Fresh application objects over the same persisted state."""
        self._build()

    # -- helpers ----------------------------------------------------------
    def write(self, text: str, *, attempt: int | None = None) -> Path:
        path = self.worker.report_path(
            self.step_no, self.attempt if attempt is None else attempt
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def report(self, *, summary: str = "the step is done", **extra) -> Path:
        return self.write(
            report_text(self.step_no, attempt=self.attempt, summary=summary, **extra)
        )

    def step(self) -> Step:
        step = self.storage.steps.get(self.step_no)
        assert step is not None
        return step

    def set_step(self, **fields) -> None:
        from dataclasses import replace

        self.storage.steps.upsert(replace(self.step(), **fields))

    def set_project(self, **fields) -> None:
        from dataclasses import replace

        project = self.storage.projects.list()[0]
        self.storage.projects.upsert(replace(project, **fields))

    def current(self):
        return self.supervision.current(self.step_no, self.attempt)

    def record(self, supervision_id: str):
        found = self.storage.supervisions.get(supervision_id)
        assert found is not None
        return found

    def analyze(self):
        return self.supervision.analyze(
            self.step_no, self.attempt, on_event=self.events.append
        )

    def approve(self, supervision_id: str, **kwargs):
        """Approve a directive, with this rig's event sink attached."""
        return self.supervision.approve_and_send(
            supervision_id, on_event=self.events.append, **kwargs
        )

    def reject(self, supervision_id: str, **kwargs):
        return self.supervision.reject_directive(
            supervision_id, on_event=self.events.append, **kwargs
        )

    def waive(self, supervision_id: str, **kwargs):
        return self.supervision.waive_supervision(
            supervision_id, on_event=self.events.append, **kwargs
        )

    def escalate(self, supervision_id: str, **kwargs):
        return self.supervision.escalate(
            supervision_id, on_event=self.events.append, **kwargs
        )

    def tick(self):
        return self.runtime.tick(on_event=self.events.append)

    def start(self):
        return self.runtime.start(on_event=self.events.append)

    def directive_path(self) -> Path:
        return self.worker.directive_path(self.step_no, self.attempt)

    def outcomes(self, tick) -> list[str]:
        """The per-record outcomes of one tick report."""
        return [item.outcome for item in tick.results]

    def artifact(self) -> dict:
        return json.loads(self.directive_path().read_text(encoding="utf-8"))

    def event_actions(self) -> list[str]:
        return [event["action"] for event in self.events]

    def audit_entries(self):
        return self.storage.audit.list()

    def audit_operations(self) -> list[str]:
        return [str(entry.detail.get("operation")) for entry in self.audit_entries()]

    def close(self) -> None:
        close_database(self.connection)


@pytest.fixture
def rig(tmp_path):
    made: list[Rig] = []

    def factory(**kwargs) -> Rig:
        item = Rig(tmp_path, **kwargs)
        made.append(item)
        return item

    yield factory
    for item in made:
        item.close()


class TestRuntimeTick:
    """A two-second tick must be cheap, and exactly one analysis per report."""

    def test_a_stopped_runtime_does_nothing(self, rig) -> None:
        r = rig()
        r.report()

        tick = r.tick()

        assert tick.outcome == "stopped"
        assert r.supervisor.call_count == 0
        assert r.current() is None

    def test_start_reconciles_immediately(self, rig) -> None:
        r = rig()
        r.report()

        tick = r.start()

        assert r.runtime.running is True
        assert tick.state == "RUNNING"
        assert r.supervisor.call_count == 1
        assert "supervisor-started" in r.event_actions()
        assert "new-report-detected" in r.event_actions()

    def test_no_report_means_no_provider_call(self, rig) -> None:
        r = rig()
        r.start()

        tick = r.tick()

        assert tick.outcome == "no-report"
        assert tick.provider_called is False
        assert r.supervisor.call_count == 0
        assert r.current() is None

    def test_an_unchanged_report_is_never_analysed_again(self, rig) -> None:
        r = rig()
        r.report()
        r.start()
        first = r.supervisor.call_count

        for _ in range(3):
            tick = r.tick()

        assert first == 1
        assert r.supervisor.call_count == 1
        assert tick.outcome == "unchanged"
        assert tick.provider_called is False
        assert r.current().status is SupervisorStatus.NO_ACTION

    def test_an_unchanged_report_is_announced_once(self, rig) -> None:
        r = rig()
        r.report()
        r.start()
        for _ in range(4):
            r.tick()

        assert r.event_actions().count("duplicate-report") == 1

    def test_a_new_report_is_analysed_once(self, rig) -> None:
        r = rig(default=None)
        r.report(summary="R1")
        r.start()
        assert r.supervisor.call_count == 1

        r.report(summary="R2")
        r.tick()
        assert r.supervisor.call_count == 2

        r.tick()
        assert r.supervisor.call_count == 2
        assert len(r.supervision.history(r.step_no, r.attempt)) == 2

    def test_a_tick_writes_no_audit_entry(self, rig) -> None:
        r = rig()
        r.report()
        r.start()
        r.tick()
        r.tick()

        assert r.audit_entries() == ()

    def test_the_runtime_status_is_plain_and_process_local(self, rig) -> None:
        r = rig()
        before = r.runtime.status()
        assert before["state"] == "STOPPED"
        assert before["running"] is False

        r.report()
        r.start()
        r.tick()
        after = r.runtime.status()

        assert after["state"] == "RUNNING"
        assert after["tick_count"] == 2
        assert after["provider_calls"] == 1
        assert after["last_outcome"] == "unchanged"
        assert after["interval_seconds"] == 2.0
        assert json.loads(json.dumps(after)) == after

    def test_stop_and_start_are_idempotent(self, rig) -> None:
        r = rig()
        r.report()
        r.start()
        r.start()
        assert r.runtime.status()["tick_count"] == 2
        assert r.event_actions().count("supervisor-started") == 1

        r.runtime.stop(on_event=r.events.append)
        r.runtime.stop(on_event=r.events.append)
        assert r.event_actions().count("supervisor-stopped") == 1
        assert r.tick().outcome == "stopped"

    def test_the_runtime_interval_is_configurable(self, rig) -> None:
        r = rig()
        assert r.runtime.interval == 2.0
        from architecture_assistant.application import SupervisorRuntime

        with pytest.raises(ValueError):
            SupervisorRuntime(r.storage, r.supervision, interval=0)

    def test_the_runtime_owns_no_thread(self, rig) -> None:
        r = rig()
        r.report()
        r.start()
        # No new threads appear: the runtime is a tick-driven coordinator, and
        # the host schedules it. That is a design fact, so it is pinned.
        import threading

        assert threading.active_count() == 1


class TestMalformedReports:
    """Malformed bytes block, never reach a supervisor, and never loop."""

    def test_malformed_bytes_never_reach_a_supervisor(self, rig) -> None:
        r = rig()
        r.write("{not json")

        analysis = r.analyze()

        assert analysis.outcome == "malformed"
        assert analysis.status == SupervisorStatus.MALFORMED.value
        assert analysis.provider_called is False
        assert r.supervisor.call_count == 0

    def test_malformed_bytes_block_the_gate(self, rig) -> None:
        r = rig()
        r.write("{not json")
        r.analyze()

        assert r.gate.resolve(r.step_no, r.attempt).allowed is False
        assert (
            r.gate.waiting_for(r.step_no, r.attempt) is WaitingFor.BLOCKED
        )

    def test_a_malformed_record_carries_no_instruction(self, rig) -> None:
        r = rig()
        r.write("{not json")
        r.analyze()

        assert r.current().instruction_for_cline == ""
        assert r.current().malformed is True

    def test_a_wrong_status_value_is_malformed(self, rig) -> None:
        r = rig()
        r.write(report_text(r.step_no))
        text = r.worker.report_path(r.step_no, r.attempt).read_text(
            encoding="utf-8"
        )
        r.write(text.replace('"DONE"', '"PROBABLY"'))

        analysis = r.analyze()

        assert analysis.outcome == "malformed"
        assert r.supervisor.call_count == 0

    def test_a_mismatched_step_number_is_malformed(self, rig) -> None:
        r = rig()
        r.write(report_text(99, attempt=r.attempt))

        assert r.analyze().outcome == "malformed"

    def test_stable_malformed_bytes_escalate_at_the_threshold(self, rig) -> None:
        r = rig(stability=timedelta(seconds=30), timeout=timedelta(seconds=120))
        r.write("{not json")

        first = r.analyze()
        assert first.outcome == "malformed"
        assert r.current().requires_human is False
        assert "malformed-report" in r.event_actions()

        r.clock.advance(seconds=31)
        again = r.analyze()

        assert again.outcome == "malformed"
        assert r.current().requires_human is True
        assert "malformed-timeout" in r.event_actions()
        assert r.supervisor.call_count == 0

    def test_changing_malformed_bytes_reset_the_hash_timer_only(self, rig) -> None:
        r = rig(stability=timedelta(seconds=30), timeout=timedelta(seconds=120))
        r.write("{not json")
        r.analyze()
        first_seen = r.current().first_seen_at

        # New malformed bytes: a new identity with its own stability timer...
        r.clock.advance(seconds=20)
        r.write("still not json")
        r.analyze()
        second = r.current()
        assert second.first_seen_at == r.clock()
        assert second.first_seen_at > first_seen
        assert second.requires_human is False

        # ...but the absolute deadline kept running from the first sight.
        r.clock.advance(seconds=101)
        r.analyze()
        assert r.current().requires_human is True
        # The per-hash anchor is untouched: the escalation came from the
        # *absolute* deadline, which rewriting the bytes could not reset.
        assert r.current().first_seen_at == NOW + timedelta(seconds=20)

    def test_the_absolute_timer_survives_a_restart(self, rig) -> None:
        r = rig(stability=timedelta(seconds=30), timeout=timedelta(seconds=120))
        r.write("{not json")
        r.analyze()
        r.clock.advance(seconds=20)

        r.restart()
        r.write("still not json")
        r.analyze()
        assert r.current().requires_human is False
        r.clock.advance(seconds=101)
        r.analyze()

        assert r.current().requires_human is True

    def test_a_valid_report_leaves_the_malformed_path(self, rig) -> None:
        r = rig()
        r.write("{not json")
        r.analyze()
        assert r.current().status is SupervisorStatus.MALFORMED

        r.report()

        analysis = r.analyze()
        assert analysis.outcome == "analyzed"
        assert analysis.provider_called is True
        history = r.supervision.history(r.step_no, r.attempt)
        assert len(history) == 2
        assert {item.status for item in history} == {
            SupervisorStatus.MALFORMED,
            SupervisorStatus.NO_ACTION,
        }

    def test_malformed_bytes_do_not_write_an_audit_entry(self, rig) -> None:
        r = rig()
        r.write("{not json")
        r.analyze()

        assert r.audit_entries() == ()


def _boom(*_args, **_kwargs):
    """A publication that must never happen."""
    raise RuntimeError("publication attempted")


def crash_after_publish(real_publish):
    """Publish for real, then fail - a crash between TX1 and TX2."""

    def publish(directive, *, step_no, attempt):
        real_publish(directive, step_no=step_no, attempt=attempt)
        raise RuntimeError("simulated crash between publish and commit")

    return publish


class TestSendProtocol:
    """Intent, at-least-once publication, completion - and honest recovery."""

    def test_an_approved_directive_is_published_and_recorded(self, rig) -> None:
        r = rig(
            responses=(
                scripted_result(
                    SupervisorAction.REVISE,
                    instruction_for_cline="add the test counts",
                ),
            )
        )
        r.report()
        analysis = r.analyze()

        record = r.approve(analysis.supervision_id, actor=ACTOR, reason=REASON)

        assert record.status is SupervisorStatus.SENT
        assert record.sent_at is not None
        assert r.directive_path().exists()
        artifact = r.artifact()
        assert artifact["supervision_id"] == record.supervision_id
        assert artifact["source_report_hash"] == record.source_report_hash
        assert artifact["instruction"] == "add the test counts"
        assert artifact["action"] == "REVISE"
        assert artifact["schema_version"] == "1"
        assert r.audit_operations() == ["approve-send", "directive-sent"]

    def test_an_operator_may_replace_the_instruction(self, rig) -> None:
        r = rig(
            responses=(
                scripted_result(
                    SupervisorAction.REVISE,
                    instruction_for_cline="the supervisor's own wording",
                ),
            )
        )
        r.report()
        analysis = r.analyze()

        record = r.supervision.approve_and_send(
            analysis.supervision_id,
            actor=ACTOR,
            reason=REASON,
            instruction="the operator's wording",
        )

        assert record.instruction_for_cline == "the operator's wording"
        assert r.artifact()["instruction"] == "the operator's wording"
        assert (
            r.audit_entries()[0].detail["instruction"]
            == "the operator's wording"
        )

    def test_an_empty_instruction_is_refused(self, rig) -> None:
        r = rig()
        r.report()
        r.analyze()
        # NO_ACTION carries no directive, so approving it is refused outright.
        with pytest.raises(SupervisionNotSendableError):
            r.supervision.approve_and_send(
                r.current().supervision_id, actor=ACTOR, reason=REASON
            )

    def test_a_publish_failure_leaves_a_durable_intent(self, rig) -> None:
        r = rig(
            responses=(
                scripted_result(
                    SupervisorAction.REVISE,
                    instruction_for_cline="add the test counts",
                ),
            )
        )
        r.report()
        analysis = r.analyze()
        real = r.worker.publish_directive
        r.worker.publish_directive = _boom  # type: ignore[method-assign]

        record = r.approve(analysis.supervision_id, actor=ACTOR, reason=REASON)

        assert record.status is SupervisorStatus.SEND_PENDING
        assert r.directive_path().exists() is False
        assert "send-pending" in r.event_actions()
        r.worker.publish_directive = real  # type: ignore[method-assign]

        tick = r.start()

        assert "send-recovered" in r.outcomes(tick)
        assert r.current().status is SupervisorStatus.SENT
        assert r.artifact()["supervision_id"] == record.supervision_id

    def test_a_crash_after_the_publish_does_not_republish(self, rig) -> None:
        r = rig(
            responses=(
                scripted_result(
                    SupervisorAction.REVISE,
                    instruction_for_cline="add the test counts",
                ),
            )
        )
        r.report()
        analysis = r.analyze()
        real = r.worker.publish_directive
        r.worker.publish_directive = crash_after_publish(  # type: ignore[method-assign]
            real
        )
        r.approve(analysis.supervision_id, actor=ACTOR, reason=REASON)
        assert r.current().status is SupervisorStatus.SEND_PENDING
        assert r.directive_path().exists() is True
        before = r.directive_path().read_text(encoding="utf-8")

        # Any further publication raises: if reconciliation still completes the
        # send, it proved it read the artifact instead of guessing.
        r.worker.publish_directive = _boom  # type: ignore[method-assign]
        tick = r.start()

        assert "send-completed" in r.outcomes(tick)
        assert r.current().status is SupervisorStatus.SENT
        assert r.directive_path().read_text(encoding="utf-8") == before

    def test_reconciliation_is_idempotent(self, rig) -> None:
        r = rig(
            responses=(
                scripted_result(
                    SupervisorAction.REVISE,
                    instruction_for_cline="add the test counts",
                ),
            )
        )
        r.report()
        analysis = r.analyze()
        real = r.worker.publish_directive
        r.worker.publish_directive = _boom  # type: ignore[method-assign]
        r.approve(analysis.supervision_id, actor=ACTOR, reason=REASON)
        r.worker.publish_directive = real  # type: ignore[method-assign]

        first = r.start()
        second = r.tick()
        third = r.tick()

        assert "send-recovered" in r.outcomes(first)
        assert second.outcome == "unchanged"
        assert third.outcome == "unchanged"
        assert r.current().status is SupervisorStatus.SENT
        assert r.audit_operations().count("directive-sent") == 1

    def test_a_restart_reconciles_a_pending_send(self, rig) -> None:
        r = rig(
            responses=(
                scripted_result(
                    SupervisorAction.REVISE,
                    instruction_for_cline="add the test counts",
                ),
            )
        )
        r.report()
        analysis = r.analyze()
        real = r.worker.publish_directive
        r.worker.publish_directive = _boom  # type: ignore[method-assign]
        r.approve(analysis.supervision_id, actor=ACTOR, reason=REASON)
        # The blocker is the *crash*, not a broken channel: a restarted process
        # has a working channel and must finish the send itself.
        r.worker.publish_directive = real  # type: ignore[method-assign]

        r.restart()
        tick = r.start()

        assert "send-recovered" in r.outcomes(tick)
        assert r.current().status is SupervisorStatus.SENT
        assert r.artifact()["supervision_id"] == analysis.supervision_id

    def test_a_foreign_artifact_is_corrected(self, rig) -> None:
        r = rig(
            responses=(
                scripted_result(
                    SupervisorAction.REVISE,
                    instruction_for_cline="add the test counts",
                ),
            )
        )
        r.report()
        analysis = r.analyze()
        real = r.worker.publish_directive
        r.worker.publish_directive = _boom  # type: ignore[method-assign]
        r.approve(analysis.supervision_id, actor=ACTOR, reason=REASON)
        # A directive for a *different* supervision of the same dispatch: the
        # artifact must never be mistaken for this record's own publication.
        path = r.directive_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "supervision_id": "0" * 64,
                    "source_report_hash": "1" * 64,
                    "instruction": "someone else's directive",
                }
            ),
            encoding="utf-8",
        )
        tick = r.start()

        assert "send-pending" in r.outcomes(tick)
        assert r.current().status is SupervisorStatus.SEND_PENDING
        # It never claims success on a foreign artifact: the intent stays durable.
        r.worker.publish_directive = real  # type: ignore[method-assign]
        assert "send-recovered" in r.outcomes(r.tick())
        assert r.artifact()["supervision_id"] == analysis.supervision_id



    def test_an_oversized_report_is_malformed(self, rig) -> None:
        from architecture_assistant.application import MAX_REPORT_BYTES

        r = rig()
        r.write('{"padding": "' + "x" * (MAX_REPORT_BYTES + 1) + '"}')

        analysis = r.analyze()

        assert analysis.outcome == "malformed"
        assert r.supervisor.call_count == 0
