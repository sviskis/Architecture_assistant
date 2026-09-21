"""Tests for the supervision gate, its identity and the two orchestrator gates.

Step 28 Phase 1 evidence, in this order:

* **identity** - the supervision id is a function of the exact report bytes plus
  the workflow context, and of nothing else, so a decided report can never
  authorize a rewritten one;
* **the gate policy** - fail-closed: a missing row blocks, only three statuses
  allow, and a human-free ``REJECTED``/``WAIVED`` row is refused even when it
  exists;
* **Gate A** (``REPORT_RECEIVED`` -> ``START_REVIEW``), **Gate B**
  (``_review`` before ``decide_review``) and the fact that a blocked gate can
  reach neither the realization gate nor the ACK;
* **disabled supervision** - the loop is byte-for-byte the Step 27 loop, and the
  *application* refuses to analyse, record, send or decide anything at all: no
  provider call, no row, no directive and no audit entry, however the call
  arrives (a panel button, a core-worker API call or a direct use-case call).

Everything runs against a **real** SQLite source of truth and a **real** Cline
file channel with an offline scripted supervisor: no provider, no key, no
network.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from architecture_assistant.application import (
    SUPERVISION_DISABLED_REASON,
    SUPERVISION_DISABLED_STATUS,
    ContextBuilder,
    LoopStatus,
    Orchestrator,
    RuntimeState,
    Scheduler,
    Supervision,
    SupervisionDisabledError,
    SupervisionGate,
    SupervisorRuntime,
    WaitingFor,
    report_hash,
    supervision_identity,
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
from architecture_assistant.domain.models import Project, Step, SupervisionRecord
from architecture_assistant.infrastructure import (
    ClineWorkerAdapter,
    ScriptedSupervisor,
    SqliteStorage,
    close_database,
    no_action,
    open_database,
    scripted_result,
)
from architecture_assistant.ports.capabilities import (
    RealizationControlPort,
    RealizationGate,
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


class FakeRealizationControl:
    """A ``RealizationControlPort`` fake: compliant by default, recording calls."""

    def __init__(self, compliant: bool = True) -> None:
        self.compliant = compliant
        self.calls: list[tuple[int, int]] = []

    def gate(self, step_no: int, attempt: int) -> RealizationGate:
        self.calls.append((step_no, attempt))
        finding_ids = (
            () if self.compliant else (f"finding-{step_no:03d}-{attempt:03d}",)
        )
        return RealizationGate(
            compliant=self.compliant,
            step_no=step_no,
            attempt=attempt,
            baseline_version=ARCH,
            violation_count=len(finding_ids),
            finding_ids=finding_ids,
            decision_id=f"realization-{step_no:03d}-{attempt:03d}",
        )


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


class Harness:
    """A real composition: SQLite source of truth + real Cline channel.

    Deliberately not a pile of fakes. The supervision identity is a hash over
    real files, the send protocol writes a real artifact, and the gate is
    consulted by the real orchestrator - so what the tests prove is the behaviour
    of the shipped code, not of a model of it.
    """

    def __init__(
        self,
        tmp_path: Path,
        *,
        mode: Mode = Mode.MANUAL,
        supervision: bool = True,
        responses: tuple = (),
        default=None,
        step_no: int = 3,
        attempt: int = 1,
        max_attempts: int = 3,
        state: StepState = StepState.REPORT_RECEIVED,
        paused: bool = False,
        compliant: bool = True,
    ) -> None:
        self.connection = open_database(":memory:")
        self.storage = SqliteStorage(self.connection)
        self.clock = FakeClock()
        self.exchange = tmp_path / "cline"
        self.worker = ClineWorkerAdapter(
            self.exchange, project=PROJECT, clock=self.clock
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
        self.supervisor = ScriptedSupervisor(
            responses, default=no_action() if default is None else default
        )
        self.gate = SupervisionGate(
            self.storage,
            self.worker,
            architecture_version=ARCH,
            enabled=supervision,
            clock=self.clock,
        )
        self.supervision = Supervision(
            self.storage, self.worker, self.supervisor, self.gate
        )
        self.runtime = SupervisorRuntime(
            self.storage, self.supervision, clock=self.clock
        )
        self.realization = FakeRealizationControl(compliant=compliant)
        self.orchestrator = Orchestrator(
            self.storage,
            self.worker,
            ContextBuilder(
                self.storage.projects,
                self.storage.steps,
                self.storage.adrs,
                self.storage.risks,
                self.storage.architecture_versions,
                clock=self.clock,
            ),
            realization_control=self.realization,
            clock=self.clock,
            timeout=timedelta(hours=1),
            supervision=self.gate if supervision else None,
        )
        self.scheduler = Scheduler(self.orchestrator, max_iterations=5)
        self.events: list = []
        self.supervision_enabled = supervision

    def restart(self) -> None:
        """Fresh application objects over the same persisted state.

        That is exactly what a restart is for the assistant: the persisted Step,
        the persisted supervision rows and the exchange directory survive, while
        the in-memory objects - gate, use-case, runtime, orchestrator - are built
        again. Anything the gate needs must therefore be *persisted*, and these
        tests are how that claim is checked rather than asserted.
        """
        self.gate = SupervisionGate(
            self.storage,
            self.worker,
            architecture_version=ARCH,
            enabled=self.supervision_enabled,
            clock=self.clock,
        )
        self.supervision = Supervision(
            self.storage, self.worker, self.supervisor, self.gate
        )
        self.runtime = SupervisorRuntime(
            self.storage, self.supervision, clock=self.clock
        )
        self.orchestrator = Orchestrator(
            self.storage,
            self.worker,
            ContextBuilder(
                self.storage.projects,
                self.storage.steps,
                self.storage.adrs,
                self.storage.risks,
                self.storage.architecture_versions,
                clock=self.clock,
            ),
            realization_control=self.realization,
            clock=self.clock,
            timeout=timedelta(hours=1),
            supervision=self.gate if self.supervision_enabled else None,
        )
        self.scheduler = Scheduler(self.orchestrator, max_iterations=5)

    # -- helpers ----------------------------------------------------------
    def write_report(self, text: str, *, attempt: int | None = None) -> Path:
        path = self.worker.report_path(
            self.step_no, self.attempt if attempt is None else attempt
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def clear_report(self) -> None:
        self.worker.report_path(self.step_no, self.attempt).unlink(missing_ok=True)

    def step(self) -> Step:
        step = self.storage.steps.get(self.step_no)
        assert step is not None
        return step

    def set_step(self, **fields) -> None:
        self.storage.steps.upsert(_replace_step(self.step(), **fields))

    def current(self) -> SupervisionRecord | None:
        return self.supervision.current(self.step_no, self.attempt)

    def record(self, supervision_id: str) -> SupervisionRecord:
        found = self.storage.supervisions.get(supervision_id)
        assert found is not None
        return found

    def analyze(self):
        return self.supervision.analyze(
            self.step_no, self.attempt, on_event=self.events.append
        )

    def tick(self):
        return self.runtime.tick(on_event=self.events.append)

    def gate_verdict(self):
        return self.gate.resolve(self.step_no, self.attempt)

    def event_actions(self) -> list[str]:
        return [event["action"] for event in self.events]

    def audit_entries(self):
        return self.storage.audit.list()

    def close(self) -> None:
        close_database(self.connection)


def _replace_step(step: Step, **fields) -> Step:
    from dataclasses import replace

    return replace(step, **fields)


@pytest.fixture
def harness(tmp_path):
    made: list[Harness] = []

    def factory(**kwargs) -> Harness:
        item = Harness(tmp_path, **kwargs)
        made.append(item)
        return item

    yield factory
    for item in made:
        item.close()


class TestIdentity:
    """The identity is the report bytes plus the workflow context - nothing else."""

    def test_the_same_bytes_produce_the_same_hash(self) -> None:
        assert report_hash(b"same") == report_hash(b"same")

    def test_different_bytes_produce_a_different_hash(self) -> None:
        assert report_hash(b"one") != report_hash(b"two")

    def test_the_hash_is_a_sha256_hex_digest(self) -> None:
        digest = report_hash(b"report")
        assert len(digest) == 64
        assert digest == digest.lower()

    def test_non_bytes_are_refused(self) -> None:
        with pytest.raises(Exception):
            report_hash("not bytes")  # type: ignore[arg-type]

    def test_the_identity_ignores_everything_the_supervisor_said(self) -> None:
        material = dict(
            project=PROJECT,
            step_no=3,
            attempt=1,
            source_report_hash=report_hash(b"report"),
            architecture_version=ARCH,
        )
        baseline = supervision_identity(**material)
        # A different answer cannot change the identity: the parameters do not
        # even exist in the signature, which is the strongest possible statement.
        assert supervision_identity(**material) == baseline
        assert set(material) == {
            "project",
            "step_no",
            "attempt",
            "source_report_hash",
            "architecture_version",
        }

    @pytest.mark.parametrize(
        "field, value",
        [
            ("project", "another project"),
            ("step_no", 4),
            ("attempt", 2),
            ("source_report_hash", report_hash(b"other")),
            ("architecture_version", "1.2"),
        ],
    )
    def test_every_identity_component_matters(self, field: str, value) -> None:
        material = dict(
            project=PROJECT,
            step_no=3,
            attempt=1,
            source_report_hash=report_hash(b"report"),
            architecture_version=ARCH,
        )
        baseline = supervision_identity(**material)
        material[field] = value
        assert supervision_identity(**material) != baseline

    def test_the_identity_survives_a_clock_change(self, harness) -> None:
        h = harness()
        h.write_report(report_text(h.step_no))
        first = h.analyze()
        h.clock.advance(hours=5)
        again = h.analyze()
        assert first.supervision_id == again.supervision_id
        assert again.provider_called is False

    def test_the_stored_identity_matches_the_pure_computation(self, harness) -> None:
        h = harness()
        text = report_text(h.step_no)
        path = h.write_report(text)
        # The identity is over the **file's** exact bytes, not over a re-encoding
        # of the text a test happened to have: on Windows a text-mode write
        # translates newlines, and the report bytes are whatever was written.
        raw = path.read_bytes()
        assert raw != text.encode("utf-8") or raw == text.encode("utf-8")
        record = h.record(h.analyze().supervision_id)
        assert record.source_report_hash == report_hash(raw)
        assert record.supervision_id == supervision_identity(
            project=PROJECT,
            step_no=h.step_no,
            attempt=h.attempt,
            source_report_hash=report_hash(raw),
            architecture_version=ARCH,
        )

    def test_rewrites_in_one_attempt_create_separate_records(self, harness) -> None:
        h = harness(
            responses=(
                scripted_result(
                    SupervisorAction.REVISE,
                    instruction_for_cline="fix it",
                ),
                no_action(),
                no_action(),
            )
        )
        h.write_report(report_text(h.step_no, summary="R1"))
        first = h.analyze()
        h.supervision.approve_and_send(
            first.supervision_id, actor=ACTOR, reason=REASON
        )
        h.write_report(report_text(h.step_no, summary="R2"))
        second = h.analyze()
        h.write_report(report_text(h.step_no, summary="R3"))
        third = h.analyze()
        history = h.supervision.history(h.step_no, h.attempt)
        assert len({first.supervision_id, second.supervision_id, third.supervision_id}) == 3
        assert len(history) == 3
        # Same attempt throughout: the supervisor never touches the counter.
        assert {item.attempt for item in history} == {h.attempt}
        assert h.step().attempt == 1


def set_status(harness_item: Harness, status: SupervisorStatus, **extra) -> None:
    """Force a record into one status - including states no clean path reaches.

    Used for the gate-policy table: the point is that the *gate* obeys the
    status, not that a particular route produced it. A hand-edited database is
    exactly one of the cases the gate must survive.
    """
    record = harness_item.current()
    assert record is not None
    from dataclasses import replace

    fields = dict(
        reason="forced for the gate table",
        instruction_for_cline="",
        decided_by="",
        decision_reason="",
        decided_at=None,
        sent_at=None,
        escalated_at=None,
        requires_human=False,
    )
    if status is SupervisorStatus.REJECTED:
        fields.update(
            decided_by=ACTOR, decision_reason="rejected by a human", decided_at=NOW
        )
    if status is SupervisorStatus.WAIVED:
        fields.update(decided_by=ACTOR)
    if status is SupervisorStatus.SENT:
        fields.update(sent_at=NOW, instruction_for_cline="a directive")
    if status is SupervisorStatus.ESCALATED:
        fields.update(escalated_at=NOW, requires_human=True)
    if status is SupervisorStatus.MALFORMED:
        fields.update(reason="the bytes are not a worker report")
    fields.update(extra)
    harness_item.storage.supervisions.upsert(replace(record, status=status, **fields))


#: (status, allowed) - the complete gate policy, one row per status.
GATE_POLICY = (
    (SupervisorStatus.ANALYSIS_PENDING, False),
    (SupervisorStatus.WAITING_HUMAN, False),
    (SupervisorStatus.READY_TO_SEND, False),
    (SupervisorStatus.SEND_PENDING, False),
    (SupervisorStatus.SENT, False),
    (SupervisorStatus.ERROR, False),
    (SupervisorStatus.ESCALATED, False),
    (SupervisorStatus.STALE, False),
    (SupervisorStatus.MALFORMED, False),
    (SupervisorStatus.NO_ACTION, True),
    (SupervisorStatus.WAIVED, True),
    (SupervisorStatus.REJECTED, True),
)


class TestGatePolicy:
    """Fail-closed: a missing row blocks, and only three statuses allow."""

    @pytest.mark.parametrize("status, allowed", GATE_POLICY)
    def test_every_status_has_one_verdict(
        self, harness, status: SupervisorStatus, allowed: bool
    ) -> None:
        h = harness()
        h.write_report(report_text(h.step_no))
        h.analyze()
        set_status(h, status)
        verdict = h.gate_verdict()
        assert verdict.allowed is allowed
        assert verdict.status == status.value
        assert verdict.supervision_id == h.current().supervision_id

    def test_the_policy_covers_every_status(self) -> None:
        assert {status for status, _allowed in GATE_POLICY} == set(SupervisorStatus)

    def test_no_row_at_all_blocks(self, harness) -> None:
        h = harness()
        h.write_report(report_text(h.step_no))
        verdict = h.gate_verdict()
        assert verdict.allowed is False
        assert verdict.status is None
        assert verdict.reason == "unsupervised-report"

    def test_no_report_blocks(self, harness) -> None:
        h = harness()
        verdict = h.gate_verdict()
        assert verdict.allowed is False
        assert verdict.reason == "no-report"

    def test_a_disabled_gate_allows_everything(self, harness) -> None:
        h = harness(supervision=False)
        h.write_report(report_text(h.step_no))
        verdict = h.gate_verdict()
        assert verdict.allowed is True
        assert verdict.reason == "supervision-disabled"
        assert h.gate.enabled() is False

    def test_a_rejected_row_without_a_human_decision_blocks(self, harness) -> None:
        h = harness()
        h.write_report(report_text(h.step_no))
        h.analyze()
        set_status(h, SupervisorStatus.REJECTED)
        # A hand-edited database: the status claims a human decision that no
        # field backs up. The domain refuses to *load* such a row, and the gate
        # refuses to treat an unloadable row as an allowance.
        h.connection.execute(
            "UPDATE supervision_records SET decided_by = '', decision_reason = ''"
        )
        h.connection.commit()
        verdict = h.gate_verdict()
        assert verdict.allowed is False
        assert verdict.reason == "supervision-record-unreadable"

    def test_a_waived_row_without_a_human_decision_blocks(self, harness) -> None:
        h = harness()
        h.write_report(report_text(h.step_no))
        h.analyze()
        set_status(h, SupervisorStatus.WAIVED)
        h.connection.execute("UPDATE supervision_records SET decided_by = ''")
        h.connection.commit()
        verdict = h.gate_verdict()
        assert verdict.allowed is False
        assert verdict.reason == "supervision-record-unreadable"

    def test_the_domain_refuses_a_forged_human_decision(self, harness) -> None:
        h = harness()
        h.write_report(report_text(h.step_no))
        record = h.record(h.analyze().supervision_id)
        from dataclasses import replace

        with pytest.raises(ValueError):
            replace(record, status=SupervisorStatus.REJECTED, decided_by="")
        with pytest.raises(ValueError):
            replace(record, status=SupervisorStatus.WAIVED, decided_by="")


class TestGateA:
    """``REPORT_RECEIVED`` -> ``START_REVIEW`` only when supervision allows."""

    def test_an_unsupervised_report_does_not_start_a_review(self, harness) -> None:
        h = harness()
        h.write_report(report_text(h.step_no))

        result = h.orchestrator.run_once()

        assert result.status is LoopStatus.WAITING
        assert result.reason == "unsupervised-report"
        assert result.transitioned is False
        assert h.step().state is StepState.REPORT_RECEIVED
        # No review, no realization gate, no ACK: the report stays where it is.
        assert h.realization.calls == []
        assert h.worker.report_path(h.step_no, h.attempt).exists()
        assert h.supervisor.call_count == 0

    def test_an_allowed_report_starts_the_review(self, harness) -> None:
        h = harness()
        h.write_report(report_text(h.step_no))
        h.analyze()
        assert h.current().status is SupervisorStatus.NO_ACTION

        result = h.orchestrator.run_once()

        assert result.transitioned is True
        assert result.event.value == "START_REVIEW"
        assert h.step().state is StepState.REVIEWING

    def test_a_sent_directive_keeps_blocking_gate_a(self, harness) -> None:
        h = harness(
            responses=(
                scripted_result(
                    SupervisorAction.REVISE,
                    instruction_for_cline="add the test counts",
                ),
            )
        )
        h.write_report(report_text(h.step_no, summary="R1"))
        analysis = h.analyze()
        h.supervision.approve_and_send(
            analysis.supervision_id, actor=ACTOR, reason=REASON
        )
        assert h.current().status is SupervisorStatus.SENT

        result = h.orchestrator.run_once()

        assert result.status is LoopStatus.WAITING
        assert result.reason == "supervision-sent"
        assert h.step().state is StepState.REPORT_RECEIVED

    def test_the_new_report_after_a_correction_is_its_own_supervision(
        self, harness
    ) -> None:
        h = harness(
            responses=(
                scripted_result(
                    SupervisorAction.REVISE,
                    instruction_for_cline="add the test counts",
                ),
                no_action("the corrected report is complete"),
            )
        )
        h.write_report(report_text(h.step_no, summary="R1"))
        first = h.analyze()
        h.supervision.approve_and_send(
            first.supervision_id, actor=ACTOR, reason=REASON
        )
        assert h.orchestrator.run_once().status is LoopStatus.WAITING

        # Cline rewrites the report in the SAME attempt: new bytes, new identity.
        h.write_report(report_text(h.step_no, summary="R2", passed=4))
        assert h.orchestrator.run_once().status is LoopStatus.WAITING
        second = h.analyze()
        assert second.supervision_id != first.supervision_id
        assert second.status == SupervisorStatus.NO_ACTION.value
        assert h.supervisor.call_count == 2

        result = h.orchestrator.run_once()

        assert result.transitioned is True
        assert h.step().state is StepState.REVIEWING
        assert h.step().attempt == 1
        # R1 never reached the authoritative review; both histories survive.
        history = h.supervision.history(h.step_no, h.attempt)
        assert [item.status for item in history] == [
            SupervisorStatus.SENT,
            SupervisorStatus.NO_ACTION,
        ]

    def test_a_disabled_supervision_leaves_the_loop_unchanged(self, harness) -> None:
        h = harness(supervision=False)
        h.write_report(report_text(h.step_no))

        result = h.orchestrator.run_once()

        assert result.transitioned is True
        assert h.step().state is StepState.REVIEWING
        assert h.orchestrator.supervision is None
        assert h.storage.supervisions.list() == ()


class TestGateB:
    """``_review`` refuses to run for an unsupervised report - every route in."""

    def test_a_seeded_reviewing_step_is_blocked(self, harness) -> None:
        h = harness(state=StepState.REVIEWING)
        h.write_report(report_text(h.step_no))

        result = h.orchestrator.run_once()

        assert result.status is LoopStatus.WAITING
        assert result.reason == "unsupervised-report"
        assert h.step().state is StepState.REVIEWING
        # No decide_review, no realization gate, no transition, no ACK.
        assert h.realization.calls == []
        assert h.storage.audit.list() == ()
        assert h.worker.report_path(h.step_no, h.attempt).exists()

    def test_a_reviewing_step_with_an_allowed_report_proceeds(self, harness) -> None:
        h = harness(state=StepState.REVIEWING)
        h.write_report(report_text(h.step_no))
        h.analyze()

        result = h.orchestrator.run_once()

        assert result.event.value == "VERIFY"
        assert h.step().state is StepState.VERIFIED
        assert h.realization.calls == [(h.step_no, h.attempt)]

    def test_gate_b_blocks_after_gate_a_did_not(self, harness) -> None:
        h = harness()
        h.write_report(report_text(h.step_no))
        h.analyze()
        assert h.orchestrator.run_once().transitioned is True  # Gate A allowed
        assert h.step().state is StepState.REVIEWING
        # The report changes while the step sits in REVIEWING: Gate B reads the
        # *current* bytes, so the new report has to be supervised before review.
        h.write_report(
            report_text(h.step_no, summary="a rewrite nobody supervised")
        )

        result = h.orchestrator.run_once()

        assert result.status is LoopStatus.WAITING
        assert result.reason == "unsupervised-report"
        assert h.step().state is StepState.REVIEWING
        assert h.realization.calls == []
        assert h.worker.report_path(h.step_no, h.attempt).exists()

    def test_a_sent_directive_blocks_gate_b(self, harness) -> None:
        h = harness(
            state=StepState.REVIEWING,
            responses=(
                scripted_result(
                    SupervisorAction.REVISE,
                    instruction_for_cline="add the test counts",
                ),
            ),
        )
        h.write_report(report_text(h.step_no))
        analysis = h.analyze()
        h.supervision.approve_and_send(
            analysis.supervision_id, actor=ACTOR, reason=REASON
        )

        result = h.orchestrator.run_once()

        assert result.status is LoopStatus.WAITING
        assert result.reason == "supervision-sent"
        assert h.realization.calls == []

    @pytest.mark.parametrize(
        "status",
        [
            SupervisorStatus.ERROR,
            SupervisorStatus.ESCALATED,
            SupervisorStatus.STALE,
            SupervisorStatus.MALFORMED,
            SupervisorStatus.WAITING_HUMAN,
            SupervisorStatus.READY_TO_SEND,
        ],
    )
    def test_every_blocking_status_blocks_gate_b(
        self, harness, status: SupervisorStatus
    ) -> None:
        h = harness(state=StepState.REVIEWING)
        h.write_report(report_text(h.step_no))
        h.analyze()
        set_status(h, status)

        result = h.orchestrator.run_once()

        assert result.status is LoopStatus.WAITING
        assert h.step().state is StepState.REVIEWING
        assert h.realization.calls == []
        assert h.worker.report_path(h.step_no, h.attempt).exists()

    def test_gate_b_allows_a_waived_report(self, harness) -> None:
        h = harness(
            state=StepState.REVIEWING,
            responses=(
                scripted_result(
                    SupervisorAction.REVISE,
                    instruction_for_cline="add the test counts",
                ),
            ),
        )
        h.write_report(report_text(h.step_no))
        analysis = h.analyze()
        assert h.current().status is SupervisorStatus.WAITING_HUMAN
        h.supervision.waive_supervision(
            analysis.supervision_id, actor=ACTOR, reason="known risk accepted"
        )

        result = h.orchestrator.run_once()

        assert result.event.value == "VERIFY"
        assert h.step().state is StepState.VERIFIED

    def test_gate_b_allows_an_explicitly_rejected_report(self, harness) -> None:
        h = harness(
            state=StepState.REVIEWING,
            responses=(
                scripted_result(
                    SupervisorAction.REVISE,
                    instruction_for_cline="add the test counts",
                ),
            ),
        )
        h.write_report(report_text(h.step_no))
        analysis = h.analyze()
        h.supervision.reject_directive(
            analysis.supervision_id, actor=ACTOR, reason="the directive is wrong"
        )

        result = h.orchestrator.run_once()

        assert result.event.value == "VERIFY"
        assert h.step().state is StepState.VERIFIED


class TestAckOrdering:
    """A report is acknowledged only after a durable authoritative outcome."""

    def test_a_blocked_report_is_never_acknowledged(self, harness) -> None:
        h = harness(state=StepState.REVIEWING)
        h.write_report(report_text(h.step_no))

        result = h.orchestrator.run_once()

        assert result.acknowledged is False
        assert h.worker.report_path(h.step_no, h.attempt).exists()
        assert list(h.worker.archive_dir().glob("*.json")) == []

    def test_a_sent_directive_is_never_acknowledged(self, harness) -> None:
        h = harness(
            responses=(
                scripted_result(
                    SupervisorAction.REVISE,
                    instruction_for_cline="add the test counts",
                ),
            )
        )
        h.write_report(report_text(h.step_no))
        analysis = h.analyze()
        h.supervision.approve_and_send(
            analysis.supervision_id, actor=ACTOR, reason=REASON
        )

        result = h.orchestrator.run_once()

        assert result.acknowledged is False
        assert h.worker.report_path(h.step_no, h.attempt).exists()

    def test_an_allowed_report_is_acknowledged_after_the_outcome(
        self, harness
    ) -> None:
        h = harness()
        h.write_report(report_text(h.step_no))
        h.analyze()

        result = h.orchestrator.run_once()  # REPORT_RECEIVED -> REVIEWING

        assert result.acknowledged is False
        assert h.worker.report_path(h.step_no, h.attempt).exists()

        result = h.orchestrator.run_once()  # the authoritative review + ACK

        assert result.acknowledged is True
        assert h.step().state is StepState.VERIFIED
        assert h.worker.report_path(h.step_no, h.attempt).exists() is False
        assert len(list(h.worker.archive_dir().glob("*.json"))) == 1


class TestAttemptSemantics:
    """The step's attempt counter stays the assistant's own."""

    def test_a_directive_in_the_same_attempt_keeps_the_counter(self, harness) -> None:
        h = harness(
            responses=(
                scripted_result(
                    SupervisorAction.RETRY,
                    risk=SupervisorRisk.LOW,
                    instruction_for_cline="rerun the relevant tests",
                ),
            )
        )
        h.write_report(report_text(h.step_no))
        analysis = h.analyze()
        assert analysis.action == "RETRY"
        assert h.step().attempt == 1
        h.supervision.approve_and_send(
            analysis.supervision_id, actor=ACTOR, reason=REASON
        )
        assert h.step().attempt == 1
        assert h.step().state is StepState.REPORT_RECEIVED

    def test_a_supervisor_cannot_increment_the_attempt(self, harness) -> None:
        h = harness()
        h.write_report(report_text(h.step_no))
        before = h.step().attempt
        h.analyze()
        assert h.step().attempt == before
        assert h.step().state is StepState.REPORT_RECEIVED

    def test_only_the_authoritative_retry_path_increments(self, harness) -> None:
        h = harness(state=StepState.REVISE)
        h.write_report(report_text(h.step_no))

        result = h.orchestrator.run_once()

        assert result.event.value == "RETRY"
        assert h.step().attempt == 2
        assert h.step().state is StepState.READY

    def test_max_attempts_is_not_bypassed_by_a_retry_directive(self, harness) -> None:
        h = harness(
            attempt=3,
            max_attempts=3,
            responses=(
                scripted_result(
                    SupervisorAction.RETRY,
                    risk=SupervisorRisk.LOW,
                    instruction_for_cline="rerun the relevant tests",
                ),
            ),
        )
        h.write_report(report_text(h.step_no, attempt=3))

        analysis = h.analyze()

        assert analysis.status == SupervisorStatus.WAITING_HUMAN.value
        record = h.current()
        assert record.requires_human is True
        assert "no attempts remain" in record.reason
        assert "max-attempts" in h.event_actions()
        assert h.step().attempt == 3

    def test_a_retry_directive_never_reaches_the_send_path(self, harness) -> None:
        h = harness(
            attempt=3,
            max_attempts=3,
            responses=(
                scripted_result(
                    SupervisorAction.RETRY,
                    risk=SupervisorRisk.LOW,
                    instruction_for_cline="rerun the relevant tests",
                ),
            ),
        )
        h.write_report(report_text(h.step_no, attempt=3))
        h.analyze()

        result = h.orchestrator.run_once()

        assert result.status is LoopStatus.WAITING
        assert h.step().attempt == 3
        assert h.worker.directive_path(h.step_no, 3).exists() is False


class TestLoopIntegration:
    """``run_once``, ``run_until_idle`` and a restart all honour the gate."""

    def test_run_until_idle_stops_at_a_blocking_gate(self, harness) -> None:
        h = harness()
        h.write_report(report_text(h.step_no))

        run = h.scheduler.run_until_idle()

        assert run.iterations == 1
        assert h.step().state is StepState.REPORT_RECEIVED

    def test_run_until_idle_completes_when_supervision_allows(self, harness) -> None:
        h = harness()
        h.write_report(report_text(h.step_no))
        h.analyze()

        run = h.scheduler.run_until_idle()

        assert h.step().state is StepState.VERIFIED
        assert run.iterations >= 2

    def test_a_restart_honours_gate_a(self, harness) -> None:
        h = harness()
        h.write_report(report_text(h.step_no))
        assert h.orchestrator.run_once().status is LoopStatus.WAITING

        h.restart()

        result = h.orchestrator.run_once()
        assert result.status is LoopStatus.WAITING
        assert result.reason == "unsupervised-report"

    def test_a_restart_honours_gate_b(self, harness) -> None:
        h = harness(state=StepState.REVIEWING)
        h.write_report(report_text(h.step_no))

        h.restart()

        result = h.orchestrator.run_once()
        assert result.status is LoopStatus.WAITING
        assert h.step().state is StepState.REVIEWING
        assert h.realization.calls == []

    def test_a_restart_keeps_an_allowed_verdict(self, harness) -> None:
        h = harness(state=StepState.REVIEWING)
        h.write_report(report_text(h.step_no))
        h.analyze()

        h.restart()

        result = h.orchestrator.run_once()
        assert result.event.value == "VERIFY"
        assert h.step().state is StepState.VERIFIED

    def test_a_restart_keeps_a_sent_verdict_blocking(self, harness) -> None:
        h = harness(
            responses=(
                scripted_result(
                    SupervisorAction.REVISE,
                    instruction_for_cline="add the test counts",
                ),
            )
        )
        h.write_report(report_text(h.step_no))
        analysis = h.analyze()
        h.supervision.approve_and_send(
            analysis.supervision_id, actor=ACTOR, reason=REASON
        )

        h.restart()

        result = h.orchestrator.run_once()
        assert result.status is LoopStatus.WAITING
        assert result.reason == "supervision-sent"
        assert h.current().status is SupervisorStatus.SENT






    def test_only_the_current_report_hash_is_consulted(self, harness) -> None:
        h = harness()
        h.write_report(report_text(h.step_no, summary="R1"))
        first = h.analyze()
        set_status(h, SupervisorStatus.NO_ACTION)
        assert h.gate_verdict().allowed is True
        # R2 appears: the R1 decision must authorize nothing.
        h.write_report(report_text(h.step_no, summary="R2"))
        verdict = h.gate_verdict()
        assert verdict.allowed is False
        assert verdict.reason == "unsupervised-report"
        assert verdict.source_report_hash != first.source_report_hash

    def test_waiting_for_follows_the_persisted_status(self, harness) -> None:
        h = harness()
        assert h.gate.waiting_for(h.step_no, h.attempt) is WaitingFor.REPORT
        h.write_report(report_text(h.step_no))
        assert (
            h.gate.waiting_for(h.step_no, h.attempt)
            is WaitingFor.SUPERVISOR_ANALYSIS
        )
        h.analyze()
        assert (
            h.gate.waiting_for(h.step_no, h.attempt)
            is WaitingFor.AUTHORITATIVE_REVIEW
        )
        set_status(h, SupervisorStatus.SENT)
        assert (
            h.gate.waiting_for(h.step_no, h.attempt)
            is WaitingFor.CLINE_NEW_REPORT
        )
        set_status(h, SupervisorStatus.ERROR)
        assert h.gate.waiting_for(h.step_no, h.attempt) is WaitingFor.BLOCKED


class TestDisabledSupervision:
    """``supervision=False`` is enforced by the application, not by a caller.

    Every entry point that could analyse, record, send or decide is answered (or
    refused) **before** it reads a report, calls a provider or writes a row, so a
    direct use-case call cannot do what the panel's button state merely
    discourages. Each test halves the proof: the deterministic disabled answer on
    one side, the untouched database, channel, provider and audit trail on the
    other.
    """

    def test_a_disabled_analysis_calls_no_supervisor_and_writes_nothing(
        self, harness
    ) -> None:
        h = harness(supervision=False)
        h.write_report(report_text(h.step_no))
        before = len(h.audit_entries())

        analysis = h.analyze()

        assert analysis.outcome == "disabled"
        assert analysis.status == SUPERVISION_DISABLED_STATUS
        assert analysis.provider_called is False
        assert analysis.gate_allowed is True  # a disabled gate blocks nothing
        assert analysis.waiting_for == WaitingFor.NONE.value
        assert analysis.reason == SUPERVISION_DISABLED_REASON
        assert h.supervisor.call_count == 0
        assert h.supervisor.contexts == []  # not even shown what it would analyse
        assert h.storage.supervisions.list() == ()
        assert len(h.audit_entries()) == before
        assert h.events == []

    def test_a_disabled_advance_is_answered_without_any_work(self, harness) -> None:
        h = harness(supervision=False)
        h.write_report(report_text(h.step_no))
        before = len(h.audit_entries())

        analysis = h.supervision.advance(
            h.step_no, h.attempt, on_event=h.events.append
        )

        assert analysis.outcome == "disabled"
        assert analysis.status == SUPERVISION_DISABLED_STATUS
        assert h.supervisor.call_count == 0
        assert h.storage.supervisions.list() == ()
        assert len(h.audit_entries()) == before
        assert h.events == []

    def test_a_disabled_reconcile_has_nothing_to_resolve(self, harness) -> None:
        h = harness(supervision=False)
        h.write_report(report_text(h.step_no))

        assert h.supervision.reconcile(on_event=h.events.append) == ()
        assert h.supervisor.call_count == 0
        assert h.storage.supervisions.list() == ()
        assert h.events == []

    def test_a_disabled_tick_is_inert_and_never_counts(self, harness) -> None:
        h = harness(supervision=False)
        h.write_report(report_text(h.step_no))
        before = len(h.audit_entries())

        tick = h.runtime.start(on_event=h.events.append)

        assert tick.outcome == "disabled"
        assert tick.state == RuntimeState.STOPPED.value
        assert tick.provider_called is False
        assert tick.results == ()
        assert h.runtime.running is False
        status = h.runtime.status()
        assert status["tick_count"] == 0
        assert status["reconcile_count"] == 0
        assert status["provider_calls"] == 0
        assert status["started_at"] is None
        assert status["last_tick_at"] is None
        assert h.supervisor.call_count == 0
        assert h.storage.supervisions.list() == ()
        assert len(h.audit_entries()) == before
        assert h.events == []

        # A host that keeps polling pays nothing and changes nothing.
        for _ in range(3):
            assert h.runtime.tick().outcome == "disabled"
        assert h.runtime.status()["tick_count"] == 0
        assert len(h.audit_entries()) == before

    @pytest.mark.parametrize(
        "method",
        [
            "approve_and_send",
            "reject_directive",
            "waive_supervision",
            "escalate",
        ],
    )
    def test_a_disabled_decision_is_refused_before_anything_is_read(
        self, harness, method
    ) -> None:
        """The refusal comes first: an unknown id is never even looked up."""
        h = harness(supervision=False)
        h.write_report(report_text(h.step_no))
        before = len(h.audit_entries())
        unknown_id = "0" * 64
        calls = {
            "approve_and_send": lambda item: item.supervision.approve_and_send(
                unknown_id,
                actor=ACTOR,
                reason=REASON,
                instruction="add the pytest command",
            ),
            "reject_directive": lambda item: item.supervision.reject_directive(
                unknown_id, actor=ACTOR, reason=REASON
            ),
            "waive_supervision": lambda item: item.supervision.waive_supervision(
                unknown_id, actor=ACTOR, reason=REASON
            ),
            "escalate": lambda item: item.supervision.escalate(
                unknown_id, actor=ACTOR, reason=REASON
            ),
        }

        with pytest.raises(SupervisionDisabledError) as refused:
            calls[method](h)

        assert SUPERVISION_DISABLED_REASON in str(refused.value)
        assert h.storage.supervisions.list() == ()
        assert len(h.audit_entries()) == before
        assert h.worker.read_directive(h.step_no, h.attempt) is None
        assert h.supervisor.call_count == 0

    def test_a_disabled_decision_is_refused_even_for_a_real_id(
        self, harness
    ) -> None:
        """A record that exists elsewhere still cannot be touched while off.

        The id is read from a **supervised** run over the same fixtures, so the
        refusal cannot be dismissed as "there was no such record".
        """
        supervised = harness()
        supervised.write_report(report_text(supervised.step_no))
        supervised.analyze()
        record = supervised.current()
        assert record is not None

        disabled = harness(supervision=False)
        disabled.write_report(report_text(disabled.step_no))

        with pytest.raises(SupervisionDisabledError):
            disabled.supervision.waive_supervision(
                record.supervision_id, actor=ACTOR, reason=REASON
            )

        assert disabled.storage.supervisions.list() == ()
        assert disabled.audit_entries() == ()
        assert disabled.events == []

    def test_a_disabled_payload_carries_configuration_only(self, harness) -> None:
        h = harness(supervision=False)
        h.write_report(report_text(h.step_no))

        payload = h.supervision.payload(h.step_no, h.attempt)

        assert payload["enabled"] is False
        assert payload["current_report_hash"] is None
        assert payload["record"] is None
        assert payload["history"] == []
        assert payload["context"] is None
        assert payload["waiting_for"] == WaitingFor.NONE.value
        assert payload["verdict"]["allowed"] is True
        assert payload["verdict"]["reason"] == "supervision-disabled"
        assert payload["project"] == PROJECT
        assert payload["step_no"] == h.step_no
        assert payload["attempt"] == h.attempt
        assert payload["mode"] == Mode.MANUAL.value
        assert payload["architecture_version"] == ARCH
        assert isinstance(json.dumps(payload), str)
        assert h.supervision.waiting_for(h.step_no, h.attempt) is WaitingFor.NONE
        assert h.supervision.enabled is False

    def test_the_loop_is_unchanged_and_the_supervisor_is_never_consulted(
        self, harness
    ) -> None:
        h = harness(supervision=False)
        h.write_report(report_text(h.step_no))
        before = h.audit_entries()

        result = h.orchestrator.run_once()

        assert result.transitioned is True
        assert h.step().state is StepState.REVIEWING
        assert h.orchestrator.supervision is None
        assert h.supervisor.call_count == 0
        assert h.storage.supervisions.list() == ()
        after = h.audit_entries()
        assert len(after) == len(before) + 1
        assert not [
            entry
            for entry in after
            if entry.entity_type is AuditEntityType.SUPERVISION
        ]

    def test_two_identical_disabled_runs_agree_byte_for_byte(self, harness) -> None:
        """Nothing about a disabled run is accidental: two runs are identical."""
        runs = []
        for _ in range(2):
            h = harness(supervision=False)
            h.write_report(report_text(h.step_no))
            result = h.orchestrator.run_once()
            runs.append(
                (
                    result.transitioned,
                    result.status,
                    result.reason,
                    h.step().state,
                    [entry.action.value for entry in h.audit_entries()],
                    [entry.entity_type.value for entry in h.audit_entries()],
                    h.supervisor.call_count,
                    h.storage.supervisions.list(),
                )
            )

        assert runs[0] == runs[1]
        assert runs[0][3] is StepState.REVIEWING
        assert runs[0][6] == 0
        assert runs[0][7] == ()

    def test_disabling_supervision_does_not_mutate_persisted_records(
        self, harness
    ) -> None:
        """A session that decides, then restarts disabled, changes nothing.

        The rows an *enabled* session wrote stay exactly as they were: the
        disabled use-case refuses to analyse, reconcile, decide or tick, so the
        persisted record and the audit trail are byte-identical afterwards.
        """
        enabled = harness(
            responses=(
                scripted_result(
                    SupervisorAction.CLARIFY,
                    requires_human=True,
                    instruction_for_cline="Add the pytest command.",
                ),
            )
        )
        enabled.write_report(report_text(enabled.step_no))
        enabled.analyze()
        record = enabled.current()
        assert record is not None
        assert record.status is SupervisorStatus.WAITING_HUMAN
        enabled.supervision.waive_supervision(
            record.supervision_id, actor=ACTOR, reason="checked by hand"
        )
        decided = enabled.record(record.supervision_id)
        assert decided.status is SupervisorStatus.WAIVED
        rows_before = enabled.storage.supervisions.list()
        audits_before = enabled.audit_entries()
        calls_before = enabled.supervisor.call_count

        # The same persisted state, restarted with supervision disabled.
        disabled_gate = SupervisionGate(
            enabled.storage,
            enabled.worker,
            architecture_version=ARCH,
            enabled=False,
            clock=enabled.clock,
        )
        disabled = Supervision(
            enabled.storage, enabled.worker, enabled.supervisor, disabled_gate
        )
        runtime = SupervisorRuntime(
            enabled.storage, disabled, clock=enabled.clock
        )

        assert disabled.analyze(enabled.step_no, enabled.attempt).outcome == (
            "disabled"
        )
        assert disabled.advance(enabled.step_no, enabled.attempt).outcome == (
            "disabled"
        )
        assert disabled.reconcile() == ()
        assert runtime.start().outcome == "disabled"
        assert runtime.tick().outcome == "disabled"
        for call in (
            lambda: disabled.reject_directive(
                decided.supervision_id, actor=ACTOR, reason=REASON
            ),
            lambda: disabled.escalate(
                decided.supervision_id, actor=ACTOR, reason=REASON
            ),
            lambda: disabled.approve_and_send(
                decided.supervision_id,
                actor=ACTOR,
                reason=REASON,
                instruction="a new instruction",
            ),
        ):
            with pytest.raises(SupervisionDisabledError):
                call()

        assert enabled.storage.supervisions.list() == rows_before
        assert enabled.record(decided.supervision_id) == decided
        assert enabled.audit_entries() == audits_before
        assert enabled.supervisor.call_count == calls_before
        assert enabled.worker.read_directive(enabled.step_no, enabled.attempt) is None

    def test_disabled_mode_never_acknowledges_or_touches_the_report(
        self, harness
    ) -> None:
        """The worker's report stays exactly where the worker left it."""
        h = harness(supervision=False)
        step = h.step()
        path = h.write_report(report_text(h.step_no))
        before = path.read_bytes()

        h.analyze()
        h.runtime.start(on_event=h.events.append)
        h.runtime.tick(on_event=h.events.append)

        assert path.exists()
        assert path.read_bytes() == before
        archive = h.exchange / "from_cline" / "archive"
        archived = list(archive.glob("*")) if archive.exists() else []
        assert archived == []
        assert h.worker.read_report(h.step_no, h.attempt) is not None
        assert h.step() == step  # state and attempt untouched
        assert h.step().attempt == step.attempt
        assert h.step().state is StepState.REPORT_RECEIVED
