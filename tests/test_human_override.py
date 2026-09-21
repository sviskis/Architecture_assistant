"""Tests for the Step 20 human-override use-case - the controlled write path.

The use-case exists so that a human can change a specific project / workflow
state *deliberately*. These tests pin exactly that:

* every operation requires an explicit actor and a non-empty reason;
* exactly one domain mutation, exactly one audit entry, one transaction;
* the authoritative FSM decides - an illegal request fails closed and writes
  nothing at all;
* ``VERIFIED`` is unreachable and the terminal states are untouchable;
* a failing repository or audit write rolls everything back;
* nothing here "repairs" the two known FSM gaps - they stay unresolved.
"""

from __future__ import annotations

import inspect
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

from architecture_assistant.application import (
    HumanOverride,
    HumanOverrideError,
    OverrideActorRequiredError,
    OverrideInvariantError,
    OverrideNoChangeError,
    OverrideProjectNotFoundError,
    OverrideReasonRequiredError,
    OverrideStepNotFoundError,
    OverrideTransitionError,
)
from architecture_assistant.architecture import (
    ARCHITECTURE_CURRENT,
    ROOT_PACKAGE,
    ArchitectureValidator,
    layer_of,
    scan_directory,
)
from architecture_assistant.domain.audit import (
    AuditAction,
    AuditEntityType,
    AuditEntry,
)
from architecture_assistant.domain.enums import (
    Mode,
    Phase,
    RiskLevel,
    StepEvent,
    StepState,
)
from architecture_assistant.domain.fsm import (
    STEP_TERMINAL_STATES,
    STEP_TRANSITIONS,
)
from architecture_assistant.domain.models import Project, Step
from architecture_assistant.infrastructure import (
    SqliteStorage,
    close_database,
    open_database,
)

NOW = datetime(2026, 9, 21, 21, 0, tzinfo=timezone.utc)
LATER = NOW.replace(hour=22)

ACTOR = "operator"
REASON = "because the workflow says so"

#: The approved public surface of the use-case - six named operations, no more.
PUBLIC_API = {
    "pause_project",
    "resume_project",
    "set_mode",
    "unblock_step",
    "resolve_step",
    "abort_step",
}

#: Capabilities Step 20 deliberately does NOT provide (asserted by name).
FORBIDDEN_API = {
    "approve_step",
    "reject_step",
    "retry_step",
    "verify_step",
    "force_ready",
    "force_dispatched",
    "set_state",
    "set_step_state",
    "override_state",
    "force_state",
}

#: The state each operation is legal from - so a context failure in the tests
#: below can never be mistaken for a transition failure.
LEGAL_FROM: dict[str, Optional[StepState]] = {
    "pause_project": None,
    "resume_project": None,
    "set_mode": None,
    "unblock_step": StepState.BLOCKED,
    "resolve_step": StepState.CONFLICT,
    "abort_step": StepState.BLOCKED,
}


def fixed_clock() -> datetime:
    return NOW


def _src_root() -> Path:
    return Path(__file__).resolve().parents[1] / "src" / "architecture_assistant"


# ---------------------------------------------------------------------------
# domain fixtures
# ---------------------------------------------------------------------------

def make_project(**overrides: Any) -> Project:
    data: dict[str, Any] = dict(
        name="Architecture Lifecycle Assistant",
        plan_version="0.3",
        mode=Mode.MANUAL,
        paused=False,
        created_at=NOW,
        updated_at=NOW,
    )
    data.update(overrides)
    return Project(**data)


def make_step(
    step_no: int = 9,
    state: StepState = StepState.READY,
    **overrides: Any,
) -> Step:
    data: dict[str, Any] = dict(
        step_no=step_no,
        phase=Phase.LOOP,
        title=f"Step {step_no}",
        state=state,
        attempt=1,
        max_attempts=3,
        risk=RiskLevel.LOW,
        requires_human=False,
        created_at=NOW,
        last_update_at=NOW,
    )
    data.update(overrides)
    return Step(**data)


def _step_events_referenced() -> set[str]:
    """The ``StepEvent`` member names the public methods actually reference.

    Derived from the compiled code objects (``co_names``), not from the prose:
    ``StepEvent.X`` compiles to a global + attribute lookup, so the set below is
    exactly what the use-case is able to send.
    """
    names = {
        name
        for method in PUBLIC_API
        for name in getattr(HumanOverride, method).__code__.co_names
    }
    return names & {member.name for member in StepEvent}


def _invoke(override: HumanOverride, method: str, **kwargs: Any) -> Any:
    """Call one override method by name, with its own positional arguments."""
    if method == "set_mode":
        return override.set_mode(Mode.AUTO, **kwargs)
    if method in ("unblock_step", "resolve_step", "abort_step"):
        return getattr(override, method)(9, **kwargs)
    return getattr(override, method)(**kwargs)


# ---------------------------------------------------------------------------
# fakes: three repository ports, one transaction port
# ---------------------------------------------------------------------------

class RecordingTransaction:
    """A transaction port that records the commit/rollback boundary."""

    def __init__(self) -> None:
        self.opened = 0
        self.committed = 0
        self.rolled_back = 0

    @contextmanager
    def transaction(self):
        self.opened += 1
        try:
            yield
        except BaseException:
            self.rolled_back += 1
            raise
        self.committed += 1


class FakeProjectRepository:
    """In-memory project port with an optional write failure."""

    def __init__(
        self, projects: tuple = (), *, fail_on_upsert: bool = False
    ) -> None:
        self.items = {project.name: project for project in projects}
        self.upserts = 0
        self._fail = fail_on_upsert

    def upsert(self, project: Project) -> None:
        self.upserts += 1
        if self._fail:
            raise RuntimeError("project write failed")
        self.items[project.name] = project

    def get(self, name: str) -> Optional[Project]:
        return self.items.get(name)

    def list(self) -> tuple:
        return tuple(self.items.values())

    def delete(self, name: str) -> bool:
        return self.items.pop(name, None) is not None


class FakeStepRepository:
    """In-memory step port with an optional write failure."""

    def __init__(
        self, steps: tuple = (), *, fail_on_upsert: bool = False
    ) -> None:
        self.items = {step.step_no: step for step in steps}
        self.upserts = 0
        self._fail = fail_on_upsert

    def upsert(self, step: Step) -> None:
        self.upserts += 1
        if self._fail:
            raise RuntimeError("step write failed")
        self.items[step.step_no] = step

    def get(self, step_no: int) -> Optional[Step]:
        return self.items.get(step_no)

    def list(self) -> tuple:
        return tuple(self.items.values())

    def list_by_state(self, state: StepState) -> tuple:
        return tuple(step for step in self.items.values() if step.state is state)

    def delete(self, step_no: int) -> bool:
        return self.items.pop(step_no, None) is not None


class FakeAuditRepository:
    """Append-only audit port with an optional append failure."""

    def __init__(self, entries: tuple = (), *, fail_on_append: bool = False):
        self.entries = list(entries)
        self.appends = 0
        self._fail = fail_on_append

    def append(self, entry: AuditEntry) -> None:
        self.appends += 1
        if self._fail:
            raise RuntimeError("audit write failed")
        self.entries.append(entry)

    def list(self) -> tuple:
        return tuple(self.entries)

    def list_for_entity(self, entity_type, entity_id: str) -> tuple:
        return tuple(
            entry
            for entry in self.entries
            if entry.entity_type == entity_type and entry.entity_id == entity_id
        )


def build_override(
    *,
    projects: Any = None,
    steps: Any = None,
    audit: Any = None,
    transactions: Any = None,
    clock: Any = fixed_clock,
) -> HumanOverride:
    return HumanOverride(
        projects
        if projects is not None
        else FakeProjectRepository((make_project(),)),
        steps if steps is not None else FakeStepRepository((make_step(),)),
        audit if audit is not None else FakeAuditRepository(),
        transactions
        if transactions is not None
        else RecordingTransaction(),
        clock=clock,
    )


class TestRequiredContext:
    """No override happens without both an explicit actor and a reason."""

    @pytest.mark.parametrize("method", sorted(LEGAL_FROM))
    @pytest.mark.parametrize("missing", ["actor", "reason"])
    @pytest.mark.parametrize("value", [None, "   "])
    def test_a_missing_field_is_refused(self, method, missing, value) -> None:
        projects = FakeProjectRepository(
            (make_project(paused=method == "resume_project"),)
        )
        steps = FakeStepRepository(
            (make_step(9, LEGAL_FROM[method] or StepState.READY),)
        )
        audit = FakeAuditRepository()
        transactions = RecordingTransaction()
        override = build_override(
            projects=projects,
            steps=steps,
            audit=audit,
            transactions=transactions,
        )
        kwargs: dict[str, Any] = {"actor": ACTOR, "reason": REASON}
        kwargs[missing] = value
        expected = (
            OverrideActorRequiredError
            if missing == "actor"
            else OverrideReasonRequiredError
        )

        with pytest.raises(expected):
            _invoke(override, method, **kwargs)

        # Fail-closed: nothing was read far enough to be written, and no
        # transaction was even opened.
        assert audit.appends == 0
        assert transactions.opened == 0
        assert projects.upserts == 0
        assert steps.upserts == 0

    def test_a_non_string_field_is_refused(self) -> None:
        override = build_override()

        with pytest.raises(OverrideActorRequiredError):
            override.pause_project(actor=7, reason=REASON)
        with pytest.raises(OverrideReasonRequiredError):
            override.pause_project(actor=ACTOR, reason=7)

    def test_actor_and_reason_are_keyword_only(self) -> None:
        with pytest.raises(TypeError):
            HumanOverride.pause_project(
                build_override(), ACTOR, REASON  # type: ignore[misc]
            )


class TestPublicAPI:
    """Six named operations - and nothing resembling a state assignment."""

    def test_the_public_api_is_exactly_the_six_operations(self) -> None:
        api = {name for name in dir(HumanOverride) if not name.startswith("_")}

        assert api == PUBLIC_API

    def test_every_operation_is_documented(self) -> None:
        for name in PUBLIC_API:
            assert (getattr(HumanOverride, name).__doc__ or "").strip(), name

    def test_every_operation_requires_both_fields(self) -> None:
        for name in PUBLIC_API:
            parameters = inspect.signature(
                getattr(HumanOverride, name)
            ).parameters

            for field in ("actor", "reason"):
                assert parameters[field].kind is inspect.Parameter.KEYWORD_ONLY
                assert parameters[field].default is inspect.Parameter.empty

    def test_no_state_assignment_api_exists(self) -> None:
        """Deliberately structural: no force/set-state capability, by name."""
        api = {name for name in dir(HumanOverride) if not name.startswith("_")}

        assert not (FORBIDDEN_API & api)

    def test_the_constructor_takes_only_write_capabilities(self) -> None:
        parameters = set(
            inspect.signature(HumanOverride.__init__).parameters
        )

        assert parameters == {
            "self",
            "projects",
            "steps",
            "audit",
            "transactions",
            "clock",
        }
        for forbidden in ("storage", "monitor", "report", "worker", "cost"):
            assert forbidden not in parameters


class TestNoDirectWrite:
    """The use-case is application-only and never touches SQLite directly."""

    def test_the_module_imports_no_infrastructure_and_no_storage_port(self):
        targets = scan_directory(_src_root()).imports_of(
            f"{ROOT_PACKAGE}.application.human_override"
        )

        assert not any("infrastructure" in target for target in targets)
        assert not any(target == "sqlite3" for target in targets)
        assert not any("ports.storage" in target for target in targets)
        assert not any("monitor" in target for target in targets)

    def test_the_module_imports_only_domain_ports_and_application(self) -> None:
        source = scan_directory(_src_root())
        layers = {
            layer_of(target)
            for target in source.imports_of(
                f"{ROOT_PACKAGE}.application.human_override"
            )
            if target.startswith(ROOT_PACKAGE)
        }

        assert layers <= {"domain", "ports", "application"}

    def test_the_use_case_lives_in_the_application_layer(self) -> None:
        assert (
            layer_of(f"{ROOT_PACKAGE}.application.human_override")
            == "application"
        )


class TestProjectControl:
    """pause/resume/set_mode: one project write + one audit entry each."""

    @pytest.mark.parametrize(
        "method, action, paused_before, paused_after",
        [
            ("pause_project", AuditAction.PAUSE, False, True),
            ("resume_project", AuditAction.RESUME, True, False),
        ],
    )
    def test_pause_and_resume_are_audited(
        self, method, action, paused_before, paused_after
    ) -> None:
        projects = FakeProjectRepository(
            (make_project(mode=Mode.AUTO, paused=paused_before),)
        )
        audit = FakeAuditRepository()
        transactions = RecordingTransaction()
        override = build_override(
            projects=projects, audit=audit, transactions=transactions
        )

        updated = _invoke(override, method, actor=ACTOR, reason=REASON)

        assert updated.paused is paused_after
        assert updated.updated_at == NOW
        assert updated.created_at == NOW
        assert projects.get(updated.name).paused is paused_after
        assert projects.upserts == 1
        assert audit.appends == 1
        assert transactions.opened == 1
        assert transactions.committed == 1
        assert transactions.rolled_back == 0

        (entry,) = audit.list()
        assert entry.entity_type is AuditEntityType.PROJECT
        assert entry.entity_id == updated.name
        assert entry.action is action
        assert entry.created_at == NOW
        assert entry.detail == {
            "operation": method,
            "actor": ACTOR,
            "reason": REASON,
            "paused_before": paused_before,
            "paused_after": paused_after,
            "mode": "AUTO",
        }

    def test_set_mode_is_audited(self) -> None:
        projects = FakeProjectRepository((make_project(mode=Mode.MANUAL),))
        audit = FakeAuditRepository()
        transactions = RecordingTransaction()
        override = build_override(
            projects=projects, audit=audit, transactions=transactions
        )

        updated = override.set_mode(
            Mode.SUPERVISED, actor=ACTOR, reason=REASON
        )

        assert updated.mode is Mode.SUPERVISED
        assert updated.paused is False
        assert updated.updated_at == NOW
        assert projects.upserts == 1
        assert audit.appends == 1
        assert transactions.committed == 1

        (entry,) = audit.list()
        assert entry.action is AuditAction.SET_MODE
        assert entry.detail == {
            "operation": "set_mode",
            "actor": ACTOR,
            "reason": REASON,
            "mode_before": "MANUAL",
            "mode_after": "SUPERVISED",
            "paused": False,
        }

    def test_a_bad_mode_is_refused_without_writing(self) -> None:
        projects = FakeProjectRepository((make_project(),))
        audit = FakeAuditRepository()
        transactions = RecordingTransaction()
        override = build_override(
            projects=projects, audit=audit, transactions=transactions
        )

        with pytest.raises(HumanOverrideError):
            override.set_mode("AUTO", actor=ACTOR, reason=REASON)  # type: ignore[arg-type]

        assert audit.appends == 0
        assert transactions.opened == 0
        assert projects.upserts == 0

    def test_the_clock_is_injected(self) -> None:
        override = build_override(clock=lambda: LATER)

        updated = override.pause_project(actor=ACTOR, reason=REASON)

        assert updated.updated_at == LATER
        assert updated.created_at == NOW

    def test_a_missing_project_is_refused(self) -> None:
        override = build_override(projects=FakeProjectRepository(()))

        with pytest.raises(OverrideProjectNotFoundError):
            override.pause_project(actor=ACTOR, reason=REASON)

    def test_two_projects_are_refused(self) -> None:
        override = build_override(
            projects=FakeProjectRepository(
                (make_project(), make_project(name="Second"))
            )
        )

        with pytest.raises(OverrideInvariantError, match="exactly one project"):
            override.pause_project(actor=ACTOR, reason=REASON)


class TestNoChange:
    """A no-op is an error - never a silent success and never an audit lie."""

    @pytest.mark.parametrize(
        "method, mode, paused",
        [
            ("pause_project", Mode.AUTO, True),
            ("resume_project", Mode.AUTO, False),
            ("set_mode", Mode.AUTO, False),
        ],
    )
    def test_a_no_op_override_is_refused(self, method, mode, paused) -> None:
        projects = FakeProjectRepository(
            (make_project(mode=mode, paused=paused),)
        )
        audit = FakeAuditRepository()
        transactions = RecordingTransaction()
        override = build_override(
            projects=projects, audit=audit, transactions=transactions
        )

        with pytest.raises(OverrideNoChangeError):
            _invoke(override, method, actor=ACTOR, reason=REASON)

        assert audit.appends == 0
        assert transactions.opened == 0
        assert projects.upserts == 0
        assert projects.get(make_project().name) == make_project(
            mode=mode, paused=paused
        )


class TestAuditCounts:
    """Exactly one entry per successful operation, and none for a failure."""

    def test_every_operation_succeeds_from_its_legal_state(self) -> None:
        for method in sorted(LEGAL_FROM):
            projects = FakeProjectRepository(
                (make_project(paused=method == "resume_project"),)
            )
            steps = FakeStepRepository(
                (make_step(9, LEGAL_FROM[method] or StepState.READY),)
            )
            audit = FakeAuditRepository()
            transactions = RecordingTransaction()
            override = build_override(
                projects=projects,
                steps=steps,
                audit=audit,
                transactions=transactions,
            )

            _invoke(override, method, actor=ACTOR, reason=REASON)

            assert audit.appends == 1, method
            assert len(audit.list()) == 1, method
            assert transactions.opened == 1, method
            assert transactions.committed == 1, method
            assert transactions.rolled_back == 0, method

    def test_one_entry_per_call_across_operations(self) -> None:
        projects = FakeProjectRepository((make_project(),))
        steps = FakeStepRepository((make_step(9, StepState.BLOCKED),))
        audit = FakeAuditRepository()
        transactions = RecordingTransaction()
        override = build_override(
            projects=projects,
            steps=steps,
            audit=audit,
            transactions=transactions,
        )

        override.pause_project(actor=ACTOR, reason=REASON)
        override.resume_project(actor=ACTOR, reason=REASON)
        override.set_mode(Mode.AUTO, actor=ACTOR, reason=REASON)
        override.unblock_step(9, actor=ACTOR, reason=REASON)

        assert audit.appends == 4
        assert len(audit.list()) == 4
        assert transactions.opened == transactions.committed == 4
        assert [entry.action for entry in audit.list()] == [
            AuditAction.PAUSE,
            AuditAction.RESUME,
            AuditAction.SET_MODE,
            AuditAction.UPDATE,
        ]
        assert [entry.entity_type for entry in audit.list()] == [
            AuditEntityType.PROJECT,
            AuditEntityType.PROJECT,
            AuditEntityType.PROJECT,
            AuditEntityType.STEP,
        ]

    def test_the_audit_trail_records_the_human_provenance(self) -> None:
        projects = FakeProjectRepository((make_project(),))
        audit = FakeAuditRepository()
        override = build_override(projects=projects, audit=audit)

        override.pause_project(actor=ACTOR, reason=REASON)
        override.resume_project(actor=ACTOR, reason=REASON)

        history = audit.list_for_entity(
            AuditEntityType.PROJECT, make_project().name
        )

        assert [entry.action for entry in history] == [
            AuditAction.PAUSE,
            AuditAction.RESUME,
        ]
        assert [entry.detail["operation"] for entry in history] == [
            "pause_project",
            "resume_project",
        ]
        assert [entry.detail["actor"] for entry in history] == [ACTOR, ACTOR]
        assert [entry.detail["reason"] for entry in history] == [REASON, REASON]
        assert [entry.created_at for entry in history] == [NOW, NOW]


class TestStepOverrides:
    """unblock/resolve/abort apply one authoritative FSM event each."""

    @pytest.mark.parametrize(
        "method, event, from_state, to_state",
        [
            (
                "unblock_step",
                StepEvent.UNBLOCK,
                StepState.BLOCKED,
                StepState.READY,
            ),
            (
                "resolve_step",
                StepEvent.RESOLVE,
                StepState.CONFLICT,
                StepState.READY,
            ),
            (
                "abort_step",
                StepEvent.ABORT,
                StepState.BLOCKED,
                StepState.ABORTED,
            ),
        ],
    )
    def test_a_step_override_is_audited(
        self, method, event, from_state, to_state
    ) -> None:
        steps = FakeStepRepository((make_step(9, from_state, attempt=2),))
        audit = FakeAuditRepository()
        transactions = RecordingTransaction()
        override = build_override(
            steps=steps, audit=audit, transactions=transactions
        )

        updated = _invoke(override, method, actor=ACTOR, reason=REASON)

        assert updated.state is to_state
        assert updated.last_update_at == NOW
        assert updated.attempt == 2  # an override never resets the budget
        assert updated.max_attempts == 3
        assert updated.created_at == NOW
        assert steps.get(9).state is to_state
        assert steps.upserts == 1
        assert audit.appends == 1
        assert transactions.opened == transactions.committed == 1
        assert transactions.rolled_back == 0

        (entry,) = audit.list()
        assert entry.entity_type is AuditEntityType.STEP
        assert entry.entity_id == "9"
        assert entry.action is AuditAction.UPDATE
        assert entry.created_at == NOW
        assert entry.detail == {
            "operation": method,
            "actor": ACTOR,
            "reason": REASON,
            "step_no": 9,
            "event": event.value,
            "from": from_state.value,
            "to": to_state.value,
            "attempt": 2,
        }

    @pytest.mark.parametrize(
        "from_state",
        [
            StepState.WAITING_APPROVAL,
            StepState.BLOCKED,
            StepState.CONFLICT,
            StepState.FAILED,
        ],
    )
    def test_abort_is_legal_from_every_human_halted_state(
        self, from_state
    ) -> None:
        steps = FakeStepRepository((make_step(9, from_state),))
        override = build_override(steps=steps)

        updated = override.abort_step(9, actor=ACTOR, reason=REASON)

        assert updated.state is StepState.ABORTED
        assert updated.finished_at == NOW
        assert updated.last_update_at == NOW

    @pytest.mark.parametrize("method", ["unblock_step", "resolve_step"])
    def test_a_recovery_override_does_not_stamp_the_finish_time(
        self, method
    ) -> None:
        steps = FakeStepRepository((make_step(9, LEGAL_FROM[method]),))
        override = build_override(steps=steps)

        updated = _invoke(override, method, actor=ACTOR, reason=REASON)

        assert updated.state is StepState.READY
        assert updated.finished_at is None
        assert updated.verified_at is None

    def test_a_missing_step_is_refused(self) -> None:
        override = build_override(steps=FakeStepRepository(()))

        with pytest.raises(OverrideStepNotFoundError):
            override.unblock_step(9, actor=ACTOR, reason=REASON)

    def test_a_non_integer_step_number_is_refused(self) -> None:
        override = build_override()

        with pytest.raises(HumanOverrideError):
            override.unblock_step(
                "9", actor=ACTOR, reason=REASON  # type: ignore[arg-type]
            )


class TestFSMIsAuthoritative:
    """An illegal request fails closed - a step is never forced."""

    ILLEGAL = [
        ("unblock_step", StepState.READY),
        ("unblock_step", StepState.PENDING),
        ("unblock_step", StepState.VERIFIED),
        ("resolve_step", StepState.READY),
        ("resolve_step", StepState.BLOCKED),
        ("abort_step", StepState.READY),
        ("abort_step", StepState.PENDING),
        ("abort_step", StepState.VERIFIED),
        ("abort_step", StepState.ABORTED),
        ("abort_step", StepState.REVISE),
    ]

    @pytest.mark.parametrize("method, state", ILLEGAL)
    def test_an_illegal_transition_writes_nothing(self, method, state) -> None:
        steps = FakeStepRepository((make_step(9, state),))
        audit = FakeAuditRepository()
        transactions = RecordingTransaction()
        override = build_override(
            steps=steps, audit=audit, transactions=transactions
        )

        with pytest.raises(OverrideTransitionError):
            _invoke(override, method, actor=ACTOR, reason=REASON)

        assert steps.upserts == 0
        assert audit.appends == 0
        assert transactions.opened == 0
        assert steps.get(9).state is state

    def test_the_error_names_the_operation_and_the_state(self) -> None:
        override = build_override(
            steps=FakeStepRepository((make_step(9, StepState.VERIFIED),))
        )

        with pytest.raises(OverrideTransitionError) as raised:
            override.unblock_step(9, actor=ACTOR, reason=REASON)

        message = str(raised.value)
        assert "unblock_step" in message
        assert "VERIFIED" in message


class TestVerifiedIsUntouchable:
    """No override can reach, change or destroy a verified outcome."""

    def test_the_only_fsm_events_the_use_case_can_send(self) -> None:
        """Structural: the referenced events come from the compiled code.

        Deriving the set from ``co_names`` (not from the docstrings) proves that
        no other event - ``VERIFY`` above all - can be sent from here.
        """
        assert _step_events_referenced() == {"UNBLOCK", "RESOLVE", "ABORT"}

    def test_a_verified_step_survives_every_operation(self) -> None:
        for method in sorted(LEGAL_FROM):
            steps = FakeStepRepository(
                (
                    make_step(7, StepState.VERIFIED),
                    make_step(9, LEGAL_FROM[method] or StepState.READY),
                )
            )
            audit = FakeAuditRepository()
            override = build_override(
                projects=FakeProjectRepository(
                    (make_project(paused=method == "resume_project"),)
                ),
                steps=steps,
                audit=audit,
            )

            _invoke(override, method, actor=ACTOR, reason=REASON)

            assert steps.get(7).state is StepState.VERIFIED, method
            assert steps.get(7).verified_at is None, method
            assert audit.appends == 1, method

    def test_verified_and_aborted_are_terminal_in_the_fsm(self) -> None:
        assert STEP_TERMINAL_STATES == {StepState.VERIFIED, StepState.ABORTED}
        for state in STEP_TERMINAL_STATES:
            outgoing = [
                event
                for (source, event) in STEP_TRANSITIONS
                if source is state
            ]
            assert outgoing == [], state


class BrokenRepository:
    """A real repository whose ``upsert`` is deliberately broken."""

    def __init__(self, inner: Any, message: str = "write failed") -> None:
        self._inner = inner
        self._message = message

    def upsert(self, item: Any) -> None:
        raise RuntimeError(self._message)

    def get(self, key: Any) -> Any:
        return self._inner.get(key)

    def list(self) -> Any:
        return self._inner.list()

    def delete(self, key: Any) -> Any:
        return self._inner.delete(key)


class BrokenAudit:
    """A real audit repository whose ``append`` is deliberately broken."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def append(self, entry: AuditEntry) -> None:
        raise RuntimeError("audit write failed")

    def list(self) -> Any:
        return self._inner.list()

    def list_for_entity(self, entity_type: Any, entity_id: str) -> Any:
        return self._inner.list_for_entity(entity_type, entity_id)


@pytest.fixture
def sqlite_storage():
    connection = open_database(":memory:")
    yield SqliteStorage(connection)
    close_database(connection)


class TestAtomicity:
    """One transaction per operation, and a failure rolls everything back."""

    def test_a_failing_audit_write_rolls_back(self) -> None:
        projects = FakeProjectRepository((make_project(),))
        steps = FakeStepRepository((make_step(9, StepState.BLOCKED),))
        transactions = RecordingTransaction()
        override = build_override(
            projects=projects,
            steps=steps,
            audit=FakeAuditRepository(fail_on_append=True),
            transactions=transactions,
        )

        with pytest.raises(RuntimeError, match="audit write failed"):
            override.pause_project(actor=ACTOR, reason=REASON)

        with pytest.raises(RuntimeError, match="audit write failed"):
            override.unblock_step(9, actor=ACTOR, reason=REASON)

        assert transactions.opened == 2
        assert transactions.rolled_back == 2
        assert transactions.committed == 0

    def test_a_failing_repository_write_leaves_no_audit_residue(self) -> None:
        audit = FakeAuditRepository()
        transactions = RecordingTransaction()
        override = build_override(
            projects=FakeProjectRepository(
                (make_project(),), fail_on_upsert=True
            ),
            steps=FakeStepRepository(
                (make_step(9, StepState.BLOCKED),), fail_on_upsert=True
            ),
            audit=audit,
            transactions=transactions,
        )

        with pytest.raises(RuntimeError, match="write failed"):
            override.pause_project(actor=ACTOR, reason=REASON)

        assert audit.appends == 0
        assert audit.list() == ()
        assert transactions.rolled_back == 1

    def test_a_successful_override_commits_both_writes(
        self, sqlite_storage
    ) -> None:
        storage = sqlite_storage
        storage.projects.upsert(make_project())
        override = HumanOverride(
            storage.projects,
            storage.steps,
            storage.audit,
            storage,
            clock=fixed_clock,
        )

        updated = override.pause_project(actor=ACTOR, reason=REASON)

        persisted = storage.projects.get(updated.name)
        assert persisted.paused is True
        assert persisted.updated_at == NOW
        (entry,) = storage.audit.list()
        assert entry.entity_type is AuditEntityType.PROJECT
        assert entry.action is AuditAction.PAUSE
        assert entry.detail["actor"] == ACTOR

    def test_a_step_override_commits_both_writes(self, sqlite_storage) -> None:
        storage = sqlite_storage
        storage.projects.upsert(make_project())
        storage.steps.upsert(make_step(9, StepState.BLOCKED))
        override = HumanOverride(
            storage.projects,
            storage.steps,
            storage.audit,
            storage,
            clock=fixed_clock,
        )

        override.unblock_step(9, actor=ACTOR, reason=REASON)

        assert storage.steps.get(9).state is StepState.READY
        (entry,) = storage.audit.list()
        assert entry.entity_type is AuditEntityType.STEP
        assert entry.action is AuditAction.UPDATE
        assert entry.detail["event"] == "UNBLOCK"
        assert entry.detail["operation"] == "unblock_step"

    def test_a_failing_audit_write_rolls_back_in_sqlite(
        self, sqlite_storage
    ) -> None:
        """The real transaction port: a broken audit leaves no change behind."""
        storage = sqlite_storage
        storage.projects.upsert(make_project())
        before = storage.projects.get(make_project().name)
        override = HumanOverride(
            storage.projects,
            storage.steps,
            BrokenAudit(storage.audit),
            storage,
            clock=fixed_clock,
        )

        with pytest.raises(RuntimeError, match="audit write failed"):
            override.pause_project(actor=ACTOR, reason=REASON)

        assert storage.projects.get(make_project().name) == before
        assert storage.projects.get(make_project().name).paused is False
        assert storage.audit.list() == ()

    def test_a_failing_step_write_leaves_no_audit_residue_in_sqlite(
        self, sqlite_storage
    ) -> None:
        storage = sqlite_storage
        storage.projects.upsert(make_project())
        storage.steps.upsert(make_step(9, StepState.BLOCKED))
        override = HumanOverride(
            storage.projects,
            BrokenRepository(storage.steps, message="step write failed"),
            storage.audit,
            storage,
            clock=fixed_clock,
        )

        with pytest.raises(RuntimeError, match="step write failed"):
            override.unblock_step(9, actor=ACTOR, reason=REASON)

        assert storage.steps.get(9).state is StepState.BLOCKED
        assert storage.audit.list() == ()

    def test_a_failing_project_write_leaves_no_audit_residue_in_sqlite(
        self, sqlite_storage
    ) -> None:
        storage = sqlite_storage
        storage.projects.upsert(make_project())
        override = HumanOverride(
            BrokenRepository(storage.projects, message="project write failed"),
            storage.steps,
            storage.audit,
            storage,
            clock=fixed_clock,
        )

        with pytest.raises(RuntimeError, match="project write failed"):
            override.pause_project(actor=ACTOR, reason=REASON)

        assert storage.audit.list() == ()
        assert storage.projects.get(make_project().name).paused is False


class TestUnresolvedQuestions:
    """The two known FSM gaps are recorded, never repaired, by Step 20."""

    def test_revise_exhausted_has_no_human_exit(self) -> None:
        """AQ-1: ``REVISE`` offers only ``RETRY`` - and this step sends none."""
        exits = [
            event
            for (state, event) in STEP_TRANSITIONS
            if state is StepState.REVISE
        ]

        assert exits == [StepEvent.RETRY]
        with pytest.raises(OverrideTransitionError):
            build_override(
                steps=FakeStepRepository((make_step(9, StepState.REVISE),))
            ).abort_step(9, actor=ACTOR, reason=REASON)

    def test_approve_semantics_are_untouched(self) -> None:
        """AQ-2: ``APPROVE`` still goes straight to ``DISPATCHED``."""
        assert (
            STEP_TRANSITIONS[(StepState.WAITING_APPROVAL, StepEvent.APPROVE)]
            is StepState.DISPATCHED
        )

    @pytest.mark.parametrize(
        "event",
        [
            StepEvent.PREPARE,
            StepEvent.REQUEST_APPROVAL,
            StepEvent.APPROVE,
            StepEvent.REJECT,
            StepEvent.DISPATCH,
            StepEvent.CLINE_START,
            StepEvent.RECEIVE_REPORT,
            StepEvent.START_REVIEW,
            StepEvent.VERIFY,
            StepEvent.REQUEST_REVISE,
            StepEvent.RAISE_CONFLICT,
            StepEvent.BLOCK,
            StepEvent.FAIL,
            StepEvent.RETRY,
        ],
    )
    def test_no_loop_event_is_reachable_from_the_use_case(self, event) -> None:
        assert event.name not in _step_events_referenced()


class TestLayerBoundary:
    """Step 20 added one application module and no new permission."""

    def test_the_whole_tree_still_satisfies_the_current_baseline(self) -> None:
        result = ArchitectureValidator().validate(scan_directory(_src_root()))

        assert result.is_compliant is True
        assert result.violations == ()
        assert result.baseline_version == ARCHITECTURE_CURRENT.version
