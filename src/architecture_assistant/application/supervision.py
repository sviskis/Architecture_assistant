"""Supervision: advisory analysis of one worker report, and nothing more.

The assistant stays authoritative. A supervisor may *recommend* that the worker
correct, clarify or retry its work; it may never decide anything. This module is
where that line is drawn in code:

* **identity** - every analysis belongs to one exact report
  (``(project, step_no, attempt, source_report_hash, architecture_version)``), so a
  rewritten report is a new record and an old decision can never authorize it;
* **fail-closed gate** - :class:`SupervisionGate` allows an authoritative review
  only for an *explicitly allowing* status of the **current** report's record; a
  missing row blocks, and so does every other status. ``SENT`` blocks too: it
  means "a directive was delivered, we are waiting for new evidence", never
  "supervision finished";
* **no workflow authority** - this module cannot set ``VERIFIED``, move a Step,
  increment an attempt, bypass ``max_attempts``, ACK a report, touch a baseline,
  an ACR, an ADR or a Risk, or reach the realization gate. It holds no FSM and no
  monitor, and the only table it writes is ``supervision_records``;
* **explicit human control** - approving, rejecting, waiving and escalating all
  require an exact ``supervision_id`` plus an actor and a reason, and every one of
  them revalidates the identity before it mutates anything (a project that was
  paused, an attempt that moved on, a report that changed or a baseline that was
  bumped makes the record ``STALE`` instead of sendable);
* **honest transport** - the send is a durable intent (``SEND_PENDING``) followed
  by an at-least-once publication of a deterministic artifact and then a durable
  completion (``SENT``). SQLite and the filesystem are not atomic, so exactly-once
  is never claimed: reconciliation on the next tick resolves the gap from
  persisted state plus the artifact itself.

Malformed report bytes never reach a supervisor: they are recorded on first sight
(``MALFORMED``, blocking, no provider call) and the persisted first-seen stamps
decide when the record escalates to the operator, so nothing can loop forever.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Callable, Mapping, Optional, Sequence

from ..domain.audit import AuditAction, AuditEntityType, AuditEntry
from ..domain.enums import (
    ACRStatus,
    ADRStatus,
    SUPERVISION_ALLOWING_STATUSES,
    Mode,
    StepState,
    SupervisorAction,
    SupervisorRisk,
    SupervisorStatus,
)
from ..domain.models import (
    Project,
    Step,
    SupervisionRecord,
    Task,
    utc_now,
)
from ..ports.capabilities import (
    MAX_SUPERVISOR_TEXT,
    SupervisorContext,
    SupervisorPort,
    SupervisorResult,
    SupervisionChannelPort,
    SupervisionVerdict,
    WorkerChannelPort,
    WorkerResult,
)
from ..ports.repositories import TaskKey
from ..ports.storage import StoragePort
from .eventlog import (
    EVENT_LEVEL_ERROR,
    EVENT_LEVEL_INFO,
    EVENT_LEVEL_WARN,
    build_event,
    exception_reason,
    sanitize_text,
)
from .supervisor_policy import SendDecision, SendVerdict, decide_send

__all__ = [
    "SUPERVISOR_COMPONENT",
    "SUPERVISION_DISABLED_STATUS",
    "SUPERVISION_DISABLED_REASON",
    "SUPERVISION_DISABLED_VERDICT_REASON",
    "DEFAULT_MALFORMED_STABILITY",
    "DEFAULT_MALFORMED_TIMEOUT",
    "DIRECTIVE_SCHEMA_VERSION",
    "MAX_REPORT_BYTES",
    "MAX_SUPERVISOR_SECTION",
    "LIVE_STEP_STATES",
    "WaitingFor",
    "SupervisionError",
    "SupervisionNotFoundError",
    "SupervisionActorRequiredError",
    "SupervisionReasonRequiredError",
    "SupervisionNotSendableError",
    "SupervisionDisabledError",
    "SupervisionStaleError",
    "report_hash",
    "supervision_identity",
    "SupervisionAnalysis",
    "SupervisionGate",
    "Supervision",
]

#: The log component every supervision event is built under.
SUPERVISOR_COMPONENT = "Supervisor"

#: The plain-data status a **disabled** supervision reports. Deliberately *not* a
#: :class:`SupervisorStatus`: no record is ever written with it, so it must stay
#: outside the persisted status vocabulary (and therefore outside the gate's
#: blocking and allowing sets). It says one thing only: supervision is off, so
#: nothing was analysed, recorded, sent or audited.
SUPERVISION_DISABLED_STATUS = "DISABLED"

#: The one deterministic reason every disabled answer carries.
SUPERVISION_DISABLED_REASON = "supervision is disabled for this composition"

#: The reason the disabled *gate* reports. Shared with the use-case so the reason
#: the panel shows and the reason the gate returns can never drift apart.
SUPERVISION_DISABLED_VERDICT_REASON = "supervision-disabled"

#: How long the *same* malformed bytes must persist before the record escalates
#: to the operator, and how long malformed bytes may keep changing before the
#: absolute deadline escalates anyway. Both are measured from **persisted**
#: first-seen stamps, so both survive a restart.
DEFAULT_MALFORMED_STABILITY: timedelta = timedelta(seconds=30)
DEFAULT_MALFORMED_TIMEOUT: timedelta = timedelta(seconds=120)

#: Schema version stamped into a directive artifact (Phase 15).
DIRECTIVE_SCHEMA_VERSION = "1"

#: A report larger than this is refused as malformed *before* it is hashed: the
#: identity of one worker report is not a place to stream a database dump.
MAX_REPORT_BYTES = 1_000_000

#: How many records of one architecture section a supervisor context carries.
MAX_SUPERVISOR_SECTION = 16

#: The step states in which a directive is still meaningful. Any other state
#: (``VERIFIED``, ``REVISE``, ``BLOCKED``, ``CONFLICT``, ``FAILED``, ``ABORTED``,
#: ``READY``, ``PENDING``, ``WAITING_APPROVAL``) makes an existing record
#: ``STALE``: the workflow has moved past the report the directive answers.
LIVE_STEP_STATES: frozenset = frozenset(
    {
        StepState.DISPATCHED,
        StepState.CLINE_WORKING,
        StepState.REPORT_RECEIVED,
        StepState.REVIEWING,
    }
)


class WaitingFor(StrEnum):
    """What the *persisted* state says the assistant is waiting for.

    Derived from application state only - never from a button's enabled flag, and
    never from anything the GUI remembers - so the panel and a headless run always
    agree.
    """

    REPORT = "report"
    SUPERVISOR_ANALYSIS = "supervisor-analysis"
    HUMAN_APPROVAL = "human-approval"
    DIRECTIVE_SEND = "directive-send"
    CLINE_NEW_REPORT = "cline-new-report"
    AUTHORITATIVE_REVIEW = "authoritative-review"
    BLOCKED = "blocked"
    NONE = "none"


class SupervisionError(Exception):
    """Base class for supervision errors."""


class SupervisionNotFoundError(SupervisionError):
    """Raised when a supervision id does not exist."""


class SupervisionActorRequiredError(SupervisionError):
    """Raised when a human supervision action names no actor."""


class SupervisionReasonRequiredError(SupervisionError):
    """Raised when a human supervision action names no reason."""


class SupervisionNotSendableError(SupervisionError):
    """Raised when the record's own status forbids the requested action."""


class SupervisionDisabledError(SupervisionError):
    """Raised when a human supervision action is asked for while disabled.

    Supervision disabled is not a status of a record - it is the absence of
    supervision, enforced in the application layer rather than left to a caller
    (or a panel) to remember. The action fails closed and writes nothing.
    """


class SupervisionStaleError(SupervisionError):
    """Raised when an identity no longer matches the live workflow.

    The record is marked :attr:`SupervisorStatus.STALE` before this is raised, and
    nothing is sent: a directive must never answer a report the workflow has
    already moved past.
    """


def report_hash(raw: bytes) -> str:
    """The SHA-256 of the **exact** report bytes written by the worker."""
    if not isinstance(raw, (bytes, bytearray)):
        raise SupervisionError(
            f"report bytes must be bytes; got {type(raw).__name__}"
        )
    return hashlib.sha256(bytes(raw)).hexdigest()


def supervision_identity(
    *,
    project: str,
    step_no: int,
    attempt: int,
    source_report_hash: str,
    architecture_version: str,
) -> str:
    """The stable identity of one supervision: report bytes plus context.

    Deliberately independent of the supervisor's answer: not the action, not the
    reason, not the risk, not the instruction, not the provider and not a
    timestamp. The identity is therefore computable **before** a supervisor is
    asked, which is what lets the assistant persist "this analysis is owed" first.
    """
    material = "|".join(
        (
            str(project),
            str(step_no),
            str(attempt),
            str(source_report_hash),
            str(architecture_version),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _mapping(value: Any) -> Mapping[str, Any]:
    """A mapping or an empty one - never a surprise."""
    return value if isinstance(value, Mapping) else {}


def _project_name(storage: StoragePort) -> str:
    """The managed project's name, or a fail-closed error.

    Supervision is scoped to *the* project this database manages: with three
    projects it would not be clear which one a directive belongs to, so the
    assistant refuses instead of guessing - exactly like every other use-case that
    reads the single project.
    """
    projects = storage.projects.list()
    if not projects:
        raise SupervisionError("the source of truth contains no project")
    if len(projects) > 1:
        names = ", ".join(sorted(project.name for project in projects))
        raise SupervisionError(
            f"expected exactly one project, found {len(projects)}: {names}"
        )
    return projects[0].name


@dataclass(frozen=True)
class SupervisionAnalysis:
    """What one ``analyze``/``reconcile`` call did - plain data, no domain object.

    Deliberately a report, not a handle: the runtime, the panel and the tests all
    read the same fields, and nothing in it can be used to mutate anything.
    """

    outcome: str
    step_no: Optional[int] = None
    attempt: Optional[int] = None
    status: Optional[str] = None
    supervision_id: Optional[str] = None
    source_report_hash: Optional[str] = None
    action: Optional[str] = None
    provider_called: bool = False
    gate_allowed: bool = False
    waiting_for: str = WaitingFor.NONE.value
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "outcome": self.outcome,
            "step_no": self.step_no,
            "attempt": self.attempt,
            "status": self.status,
            "supervision_id": self.supervision_id,
            "source_report_hash": self.source_report_hash,
            "action": self.action,
            "provider_called": self.provider_called,
            "gate_allowed": self.gate_allowed,
            "waiting_for": self.waiting_for,
            "reason": self.reason,
        }


class SupervisionGate:
    """The fail-closed, read-only answer the authoritative loop consults.

    Two gates and one answer. The orchestrator asks this object - never the
    supervision use-case - whether the **current** report of one step/attempt may
    proceed, and it is deliberately incapable of anything else: it holds no
    supervisor, no transaction boundary, no audit trail and no FSM, and it writes
    nothing. Its single collaborator that can touch anything is the worker channel,
    and only to read the report bytes.

    The whitelist is the whole policy: a missing row blocks, and only
    ``NO_ACTION``, ``WAIVED`` and an explicitly human ``REJECTED`` allow. A
    ``REJECTED`` or ``WAIVED`` row is re-checked here for its human fields, so a
    hand-edited database cannot smuggle a report past the gate.
    """

    def __init__(
        self,
        storage: StoragePort,
        worker: WorkerChannelPort,
        *,
        architecture_version: str,
        enabled: bool = True,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if (
            not isinstance(architecture_version, str)
            or not architecture_version.strip()
        ):
            raise ValueError(
                "architecture_version must be a non-empty string; got "
                f"{architecture_version!r}"
            )
        if not isinstance(enabled, bool):
            raise ValueError(f"enabled must be a bool; got {enabled!r}")
        if not callable(clock):
            raise ValueError("clock must be a callable returning a datetime")
        if not isinstance(worker, SupervisionChannelPort):
            raise ValueError(
                "worker must implement SupervisionChannelPort "
                "(read_report_bytes/publish_directive/read_directive); got "
                f"{type(worker).__name__}"
            )
        self._storage = storage
        self._worker = worker
        self._architecture_version = architecture_version
        self._enabled = enabled
        self._clock = clock

    # -- read-only introspection -------------------------------------------
    @property
    def architecture_version(self) -> str:
        """The baseline version that is part of every supervision identity."""
        return self._architecture_version

    @property
    def clock(self) -> Callable[[], datetime]:
        """The clock this gate stamps with (shared with the use-case)."""
        return self._clock

    def enabled(self) -> bool:
        """Whether supervision is active at all."""
        return self._enabled

    @property
    def project(self) -> Optional[str]:
        """The managed project's name, or ``None`` when it cannot be read."""
        try:
            return _project_name(self._storage)
        except SupervisionError:
            return None

    def current_report_hash(self, step_no: int, attempt: int) -> Optional[str]:
        """The hash of the **current** report bytes, or ``None`` when absent."""
        raw = self._worker.read_report_bytes(step_no, attempt)
        return None if raw is None else report_hash(raw)

    def _lookup(
        self, project: str, step_no: int, attempt: int, digest: str
    ) -> tuple[Optional[SupervisionRecord], str]:
        """The record for one identity - or ``(None, problem)``.

        A row the domain refuses to load (a hand-edited database, a row written
        by an older schema) is reported as a problem instead of raising: the
        caller then fails closed rather than crashing the loop or - far worse -
        treating an unreadable row as an allowance.
        """
        try:
            return (
                self._storage.supervisions.find_current(
                    project,
                    step_no,
                    attempt,
                    digest,
                    self._architecture_version,
                ),
                "",
            )
        except BaseException:  # noqa: BLE001 - unreadable is never an allowance
            return None, "supervision-record-unreadable"

    def current_record(
        self, step_no: int, attempt: int
    ) -> Optional[SupervisionRecord]:
        """The record of the current report identity, or ``None``."""
        project = self.project
        if project is None:
            return None
        digest = self.current_report_hash(step_no, attempt)
        if digest is None:
            return None
        record, _problem = self._lookup(project, step_no, attempt, digest)
        return record

    def history(
        self, step_no: int, attempt: int
    ) -> tuple[SupervisionRecord, ...]:
        """Every supervision record of one step/attempt, oldest first."""
        project = self.project
        if project is None:
            return ()
        return self._storage.supervisions.list_for_step(
            project, step_no, attempt
        )

    # -- the one question that matters -------------------------------------
    def resolve(self, step_no: int, attempt: int) -> SupervisionVerdict:
        """May the current report of this step/attempt be reviewed?

        Deliberately reads the **current bytes** every time: the answer is about
        the report that exists right now, never about the latest directive, the
        latest row, the attempt number alone or anything cached in memory. That is
        what makes "R1 was answered, so R2 must be supervised too" a structural
        property rather than a careful caller's habit.
        """
        digest = self.current_report_hash(step_no, attempt)
        if digest is None:
            return SupervisionVerdict(
                allowed=False,
                status=None,
                supervision_id=None,
                reason="no-report",
                source_report_hash=None,
            )
        return self.resolve_hash(step_no, attempt, digest)

    def resolve_hash(
        self, step_no: int, attempt: int, digest: str
    ) -> SupervisionVerdict:
        """The same verdict, for an already-computed report hash."""
        if not self._enabled:
            return SupervisionVerdict(
                allowed=True,
                status=None,
                supervision_id=None,
                reason=SUPERVISION_DISABLED_VERDICT_REASON,
                source_report_hash=digest,
            )
        project = self.project
        if project is None:
            return SupervisionVerdict(
                allowed=False,
                status=None,
                supervision_id=None,
                reason="no-project",
                source_report_hash=digest,
            )
        try:
            record = self._storage.supervisions.find_current(
                project, step_no, attempt, digest, self._architecture_version
            )
        except BaseException:  # noqa: BLE001 - an unreadable row may never allow
            # A row the domain refuses to load - a hand-edited database, a row
            # written by an older schema - is not an allowance. The gate fails
            # closed instead of propagating: a report nobody can supervise must be
            # blocked, not crash the loop and not slip through.
            return SupervisionVerdict(
                allowed=False,
                status=None,
                supervision_id=None,
                reason="supervision-record-unreadable",
                source_report_hash=digest,
            )
        if record is None:
            return SupervisionVerdict(
                allowed=False,
                status=None,
                supervision_id=None,
                reason="unsupervised-report",
                source_report_hash=digest,
            )
        return self._verdict(record)

    def verdict_for(self, record: SupervisionRecord) -> SupervisionVerdict:
        """The verdict one record would produce for the **current** report."""
        return self._verdict(record)

    def _verdict(self, record: SupervisionRecord) -> SupervisionVerdict:
        """Turn one record into a verdict, fail-closed on every doubt."""
        status = record.status
        if status not in SUPERVISION_ALLOWING_STATUSES:
            return SupervisionVerdict(
                allowed=False,
                status=status.value,
                supervision_id=record.supervision_id,
                reason=f"supervision-{status.value.lower()}",
                source_report_hash=record.source_report_hash,
            )
        # Defence in depth: the model already refuses these shapes, so a row that
        # reaches here without its human decision was not written by this
        # assistant - and an unexplained allowance is exactly what the gate must
        # never grant.
        if status is SupervisorStatus.REJECTED and (
            not record.decided_by.strip() or not record.decision_reason.strip()
        ):
            return SupervisionVerdict(
                allowed=False,
                status=status.value,
                supervision_id=record.supervision_id,
                reason="supervision-rejected-without-human-decision",
                source_report_hash=record.source_report_hash,
            )
        if status is SupervisorStatus.WAIVED and not record.decided_by.strip():
            return SupervisionVerdict(
                allowed=False,
                status=status.value,
                supervision_id=record.supervision_id,
                reason="supervision-waived-without-human-decision",
                source_report_hash=record.source_report_hash,
            )
        return SupervisionVerdict(
            allowed=True,
            status=status.value,
            supervision_id=record.supervision_id,
            reason=f"supervision-{status.value.lower()}",
            source_report_hash=record.source_report_hash,
        )

    # -- what the assistant is waiting for ---------------------------------
    def waiting_for(self, step_no: int, attempt: int) -> WaitingFor:
        """The deterministic ``waiting_for`` of the current report."""
        digest = self.current_report_hash(step_no, attempt)
        if digest is None:
            return WaitingFor.REPORT
        project = self.project
        if project is None:
            return WaitingFor.BLOCKED
        record, problem = self._lookup(project, step_no, attempt, digest)
        if problem:
            return WaitingFor.BLOCKED
        if record is None:
            return WaitingFor.SUPERVISOR_ANALYSIS
        return self.waiting_for_record(record)

    def waiting_for_record(self, record: SupervisionRecord) -> WaitingFor:
        """The ``waiting_for`` of one record, derived from its status alone."""
        status = record.status
        if status is SupervisorStatus.ANALYSIS_PENDING:
            return WaitingFor.SUPERVISOR_ANALYSIS
        if status in (
            SupervisorStatus.WAITING_HUMAN,
            SupervisorStatus.READY_TO_SEND,
        ):
            return WaitingFor.HUMAN_APPROVAL
        if status is SupervisorStatus.SEND_PENDING:
            return WaitingFor.DIRECTIVE_SEND
        if status is SupervisorStatus.SENT:
            return WaitingFor.CLINE_NEW_REPORT
        if status in SUPERVISION_ALLOWING_STATUSES:
            return WaitingFor.AUTHORITATIVE_REVIEW
        return WaitingFor.BLOCKED


class Supervision:
    """The supervision use-case: analyse a report, record it, send - or stop.

    It is handed the source of truth, the worker channel, the gate and one
    ``SupervisorPort`` - and nothing else. No monitor, no FSM, no realization
    control, no architecture port: the *only* table it writes is
    ``supervision_records``, and the only other thing it touches is the dedicated
    directive artifact of the exchange channel it was handed.
    """

    def __init__(
        self,
        storage: StoragePort,
        worker: WorkerChannelPort,
        supervisor: SupervisorPort,
        gate: SupervisionGate,
        *,
        operator_constraints: Sequence[str] = (),
        malformed_stability: timedelta = DEFAULT_MALFORMED_STABILITY,
        malformed_timeout: timedelta = DEFAULT_MALFORMED_TIMEOUT,
    ) -> None:
        for label, value in (
            ("malformed_stability", malformed_stability),
            ("malformed_timeout", malformed_timeout),
        ):
            if not isinstance(value, timedelta) or value <= timedelta(0):
                raise ValueError(f"{label} must be a positive timedelta")
        if malformed_stability > malformed_timeout:
            raise ValueError(
                "malformed_stability must not exceed malformed_timeout; got "
                f"{malformed_stability} and {malformed_timeout}"
            )
        if not isinstance(gate, SupervisionGate):
            raise ValueError(
                f"gate must be a SupervisionGate; got {type(gate).__name__}"
            )
        if not isinstance(supervisor, SupervisorPort):
            raise ValueError(
                "supervisor must implement SupervisorPort (supervise); got "
                f"{type(supervisor).__name__}"
            )
        if not isinstance(worker, WorkerChannelPort):
            raise ValueError(
                "worker must implement WorkerChannelPort "
                "(dispatch/read_report/acknowledge_report/read_report_bytes); got "
                f"{type(worker).__name__}"
            )
        if not isinstance(worker, SupervisionChannelPort):
            raise ValueError(
                "worker must also implement SupervisionChannelPort "
                "(read_report_bytes/publish_directive/read_directive); got "
                f"{type(worker).__name__}"
            )
        self._storage = storage
        self._worker = worker
        self._supervisor = supervisor
        self._gate = gate
        self._operator_constraints = tuple(
            str(item) for item in operator_constraints
        )[:MAX_SUPERVISOR_SECTION]
        self._malformed_stability = malformed_stability
        self._malformed_timeout = malformed_timeout
        # The gate owns the clock, and this use-case reads it instead of holding
        # a second one: two clocks could disagree, and a supervision whose
        # identity and whose staleness check were stamped by different clocks
        # would be a bug nobody would ever find.
        self._clock = gate.clock

    # -- read-only surface -------------------------------------------------
    @property
    def enabled(self) -> bool:
        """Whether supervision is active at all.

        The one switch **every** entry point of this use-case consults before it
        does anything. With supervision disabled the use-case performs no work:
        no report is read or analysed, no provider is called, no row and no
        directive is written, no audit entry is appended and no event is emitted.
        That is enforced here, in the application layer - a direct API call (or a
        future host with no panel at all) cannot do what the panel's button state
        merely discourages.
        """
        return self._gate.enabled()

    @property
    def gate(self) -> SupervisionGate:
        """The shared gate - the same object the orchestrator consults."""
        return self._gate

    @property
    def supervisor(self) -> SupervisorPort:
        """The configured supervisor (advisory; it decides nothing)."""
        return self._supervisor

    @property
    def architecture_version(self) -> str:
        """The baseline version every identity is stamped with."""
        return self._gate.architecture_version

    def _disabled_analysis(
        self, step_no: Optional[int] = None, attempt: Optional[int] = None
    ) -> SupervisionAnalysis:
        """The deterministic, side-effect-free answer of a disabled use-case.

        It is a report like any other :class:`SupervisionAnalysis` - same fields,
        plain data - so a caller (the runtime, the panel, a test) needs no special
        case to read it. ``gate_allowed`` is ``True`` because a disabled gate
        blocks nothing: with supervision off, nothing about supervision can hold
        the authoritative review back.
        """
        return SupervisionAnalysis(
            outcome="disabled",
            step_no=step_no,
            attempt=attempt,
            status=SUPERVISION_DISABLED_STATUS,
            provider_called=False,
            gate_allowed=True,
            waiting_for=WaitingFor.NONE.value,
            reason=SUPERVISION_DISABLED_REASON,
        )

    def _require_enabled(self) -> None:
        """Refuse a supervision **mutation** while supervision is disabled.

        Analysis, sending and every human decision are supervision state; with
        supervision disabled there is no state to change, so the call fails closed
        before it reads or writes anything.
        """
        if not self.enabled:
            raise SupervisionDisabledError(
                SUPERVISION_DISABLED_REASON
                + ": no report can be analysed, supervised or decided"
            )

    def _disabled_payload(self, step_no: int, attempt: int) -> dict[str, Any]:
        """The read-only payload of a disabled composition - configuration only.

        Same keys as the live payload, so every reader stays uniform, but no
        report-derived data at all: no report hash, no record, no history, no
        supervisor context, and ``waiting_for`` is ``none`` because nothing is
        being supervised.
        """
        project = self._project()
        step = self._storage.steps.get(step_no)
        return {
            "enabled": False,
            "project": project.name,
            "step_no": step_no,
            "attempt": attempt,
            "step_state": None if step is None else step.state.value,
            "max_attempts": None if step is None else step.max_attempts,
            "attempts_remaining": (
                None if step is None else max(step.max_attempts - step.attempt, 0)
            ),
            "mode": project.mode.value,
            "paused": project.paused,
            "architecture_version": self.architecture_version,
            "current_report_hash": None,
            "waiting_for": WaitingFor.NONE.value,
            "verdict": SupervisionVerdict(
                allowed=True,
                status=None,
                supervision_id=None,
                reason=SUPERVISION_DISABLED_VERDICT_REASON,
                source_report_hash=None,
            ).to_dict(),
            "record": None,
            "history": [],
            "context": None,
            "provider": (getattr(self._supervisor, "provider", "") or "supervisor"),
            "malformed_stability_seconds": (
                self._malformed_stability.total_seconds()
            ),
            "malformed_timeout_seconds": (
                self._malformed_timeout.total_seconds()
            ),
        }

    def record(self, supervision_id: str) -> SupervisionRecord:
        """One record by id, or fail closed."""
        if not isinstance(supervision_id, str) or not supervision_id.strip():
            raise SupervisionError(
                f"supervision_id must be a non-empty string; got {supervision_id!r}"
            )
        found = self._storage.supervisions.get(supervision_id)
        if found is None:
            raise SupervisionNotFoundError(
                f"supervision {supervision_id!r} does not exist"
            )
        return found

    def current(
        self, step_no: int, attempt: int
    ) -> Optional[SupervisionRecord]:
        """The record of the current report identity, or ``None``."""
        return self._gate.current_record(step_no, attempt)

    def history(
        self, step_no: int, attempt: int
    ) -> tuple[SupervisionRecord, ...]:
        """Every record of one step/attempt, oldest first."""
        return self._gate.history(step_no, attempt)

    def waiting_for(self, step_no: int, attempt: int) -> WaitingFor:
        """What the persisted state is waiting for.

        With supervision disabled the honest answer is :attr:`WaitingFor.NONE`:
        no analysis, no approval and no delivery is pending, because none of them
        can happen.
        """
        if not self.enabled:
            return WaitingFor.NONE
        return self._gate.waiting_for(step_no, attempt)

    def verdict(self, step_no: int, attempt: int) -> SupervisionVerdict:
        """The gate's verdict, for callers that want to show it."""
        return self._gate.resolve(step_no, attempt)

    def is_latest(
        self, record: SupervisionRecord, history: Sequence[SupervisionRecord]
    ) -> bool:
        """Whether ``record`` is the latest identity of its step/attempt.

        The order is the repository's - ``(created_at, rowid)``, i.e. when the
        identity appeared and then the order the rows were written - so "latest"
        is deterministic, survives a restart, and is still answerable when two
        reports arrive inside one clock reading.
        """
        if not history:
            return False
        return history[-1].supervision_id == record.supervision_id

    def resolve(self, step_no: int, attempt: int) -> SupervisionVerdict:
        """Alias of :meth:`verdict` (the gate's language)."""
        return self._gate.resolve(step_no, attempt)

    def payload(self, step_no: int, attempt: int) -> dict[str, Any]:
        """A plain, JSON-safe supervision view - what the panel renders.

        Everything here is either persisted state or derived from it: the gate
        verdict, the ``waiting_for`` value, the current record, the history of the
        attempt and - when the current report is valid - the same bounded
        :class:`SupervisorContext` a supervisor would be handed. No provider is
        called, nothing is written, and no domain object crosses this boundary.

        With supervision **disabled** the answer is configuration only
        (:meth:`_disabled_payload`): no report hash, no record, no context. A
        disabled session shows what it is, not a stale picture of what some
        earlier enabled session analysed.
        """
        if not self.enabled:
            return self._disabled_payload(step_no, attempt)
        project = self._project()
        step = self._storage.steps.get(step_no)
        digest = self._gate.current_report_hash(step_no, attempt)
        record = self._gate.current_record(step_no, attempt)
        context: Optional[SupervisorContext] = None
        if step is not None and digest is not None:
            parsed, _problem = self._read_validated(step_no, attempt)
            if parsed is not None:
                context = self._build_context(project, step, parsed, digest)
        return {
            "enabled": self._gate.enabled(),
            "project": project.name,
            "step_no": step_no,
            "attempt": attempt,
            "step_state": None if step is None else step.state.value,
            "max_attempts": None if step is None else step.max_attempts,
            "attempts_remaining": (
                None
                if step is None
                else max(step.max_attempts - step.attempt, 0)
            ),
            "mode": project.mode.value,
            "paused": project.paused,
            "architecture_version": self.architecture_version,
            "current_report_hash": digest,
            "waiting_for": self._gate.waiting_for(step_no, attempt).value,
            "verdict": self._gate.resolve(step_no, attempt).to_dict(),
            "record": None if record is None else record.to_dict(),
            "history": [
                item.to_dict()
                for item in reversed(self._gate.history(step_no, attempt))
            ],
            "context": None if context is None else context.to_dict(),
            "provider": (
                getattr(self._supervisor, "provider", "") or "supervisor"
            ),
            "malformed_stability_seconds": (
                self._malformed_stability.total_seconds()
            ),
            "malformed_timeout_seconds": (
                self._malformed_timeout.total_seconds()
            ),
        }


    # -- the analysis ------------------------------------------------------
    def analyze(
        self,
        step_no: int,
        attempt: int,
        *,
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> SupervisionAnalysis:
        """Supervise the current report of one step/attempt, at most once.

        The order is the contract:

        1. read the **current bytes** (no bytes, no supervision);
        2. compute the report hash and the supervision identity - *before* any
           provider is involved;
        3. return immediately when this identity already has a record that is not
           ``ANALYSIS_PENDING`` (a decided report is never analysed twice);
        4. refuse malformed bytes without asking anybody;
        5. persist ``ANALYSIS_PENDING`` for a **new** identity *before* the
           provider call, so a crash can never lose the fact that the analysis was
           owed;
        6. call the supervisor **exactly once**;
        7. persist the validated answer on the same row.

        A re-run after a crash finds ``ANALYSIS_PENDING`` and analyses again -
        that is the defined restart behaviour, and it is safe precisely because the
        provider contract requires an answer to depend only on its context, and
        because the row (and therefore the history) is the same row.

        With supervision disabled this returns the disabled analysis immediately:
        the report is not even read, no identity is computed, no row is written and
        no supervisor is called.
        """
        if not self.enabled:
            return self._disabled_analysis(step_no, attempt)
        project = self._project()
        now = self._clock()
        raw = self._worker.read_report_bytes(step_no, attempt)
        if raw is None:
            return SupervisionAnalysis(
                outcome="no-report",
                step_no=step_no,
                attempt=attempt,
                waiting_for=WaitingFor.REPORT.value,
                reason="no report bytes exist for this dispatch",
            )
        if len(raw) > MAX_REPORT_BYTES:
            return self._malformed(
                project,
                step_no,
                attempt,
                raw,
                reason=(
                    f"the report is larger than {MAX_REPORT_BYTES} bytes, which "
                    "is not a worker report"
                ),
                now=now,
                on_event=on_event,
            )
        digest = report_hash(raw)
        identity = supervision_identity(
            project=project.name,
            step_no=step_no,
            attempt=attempt,
            source_report_hash=digest,
            architecture_version=self.architecture_version,
        )
        existing = self._storage.supervisions.find_current(
            project.name,
            step_no,
            attempt,
            digest,
            self.architecture_version,
        )
        if existing is not None and existing.status not in (
            SupervisorStatus.ANALYSIS_PENDING,
            SupervisorStatus.MALFORMED,
        ):
            # A decided record is never analysed twice. ``ANALYSIS_PENDING`` means
            # the analysis was owed when the process died (so it is re-run, on the
            # same row), and ``MALFORMED`` must stay re-evaluated on every tick -
            # otherwise the persisted malformed deadlines would never be reached.
            return SupervisionAnalysis(
                outcome="already-supervised",
                step_no=step_no,
                attempt=attempt,
                status=existing.status.value,
                supervision_id=existing.supervision_id,
                source_report_hash=digest,
                action=None if existing.action is None else existing.action.value,
                gate_allowed=self._gate.verdict_for(existing).allowed,
                waiting_for=self._gate.waiting_for_record(existing).value,
                reason="this exact report already has a supervision record",
            )
        parsed, problem = self._read_validated(step_no, attempt)
        if parsed is None:
            return self._malformed(
                project,
                step_no,
                attempt,
                raw,
                reason=problem,
                now=now,
                on_event=on_event,
            )
        step = self._step(step_no)
        pending = SupervisionRecord(
            supervision_id=identity,
            project=project.name,
            step_no=step_no,
            attempt=attempt,
            source_report_hash=digest,
            architecture_version=self.architecture_version,
            status=SupervisorStatus.ANALYSIS_PENDING,
            first_seen_at=now,
            created_at=now,
            updated_at=now,
        )
        if existing is not None:
            pending = replace(existing, status=SupervisorStatus.ANALYSIS_PENDING)
        with self._storage.transaction():
            self._storage.supervisions.upsert(pending)
        self._emit(
            on_event,
            level=EVENT_LEVEL_INFO if existing is None else EVENT_LEVEL_WARN,
            action="analysis-started" if existing is None else "analysis-restart",
            message=(
                f"supervision {identity[:16]} analysing the report of step "
                f"{step_no} attempt {attempt} after a restart"
                if existing is not None
                else f"supervision {identity[:16]} analysing the report of step "
                f"{step_no} attempt {attempt}"
            ),
            step_no=step_no,
            now=now,
        )
        return self._ask(pending, project, step, parsed, digest, now, on_event)

    # -- internals: reading and malformed handling -------------------------
    def _read_validated(
        self, step_no: int, attempt: int
    ) -> tuple[Optional[WorkerResult], str]:
        """The validated worker report, or ``(None, reason)``.

        The channel's own validation is reused rather than re-implemented: a
        report is well-formed exactly when the worker channel can parse it. The
        catch is deliberately broad - *anything* the channel refuses to hand back
        is, by definition, a report the assistant cannot review - and only the
        exception's **type name** is kept, never its message.
        """
        try:
            parsed = self._worker.read_report(step_no, attempt)
        except BaseException as error:  # noqa: BLE001 - any refusal is malformed
            return None, (
                "the worker channel refused the report bytes: "
                f"{exception_reason(error)}"
            )
        if parsed is None:
            return None, "the report disappeared between the read and the check"
        if not isinstance(parsed, WorkerResult):
            return None, (
                "the worker channel returned "
                f"{type(parsed).__name__} instead of a WorkerResult"
            )
        return parsed, ""

    def _malformed(
        self,
        project: Project,
        step_no: int,
        attempt: int,
        raw: bytes,
        *,
        reason: str,
        now: datetime,
        on_event: Optional[Callable[[dict[str, Any]], None]],
    ) -> SupervisionAnalysis:
        """Record malformed bytes: block immediately, never ask a supervisor.

        Two clocks, both persisted, both honest about what they measure:

        * the **per-hash** timer is this record's own ``first_seen_at`` - a
          malformed record is keyed by the malformed bytes' hash, so the timer
          cannot leak from one set of bytes to another;
        * the **absolute** timer is the minimum ``first_seen_at`` across the
          malformed records of this step/attempt/baseline - so changing the bytes
          can never reset it, and a restart finds it exactly as it was.

        The status is ``MALFORMED`` from the first sight, because that is a *fact*
        about the bytes rather than a timeout outcome: it blocks, it calls nobody,
        and it cannot spin. What the two clocks decide is when the record
        **escalates to the operator** (``requires_human``), which is a derived
        stamp - recomputing it later yields the same answer, so it is not an audit
        event.
        """
        digest = report_hash(raw)
        identity = supervision_identity(
            project=project.name,
            step_no=step_no,
            attempt=attempt,
            source_report_hash=digest,
            architecture_version=self.architecture_version,
        )
        existing = self._storage.supervisions.find_current(
            project.name,
            step_no,
            attempt,
            digest,
            self.architecture_version,
        )
        history = self._storage.supervisions.list_for_step(
            project.name, step_no, attempt
        )
        anchors = [
            item.first_seen_at
            for item in history
            if item.status is SupervisorStatus.MALFORMED
            and item.architecture_version == self.architecture_version
        ]
        first_seen = existing.first_seen_at if existing is not None else now
        absolute_first = min([*anchors, first_seen])
        stable = (now - first_seen) >= self._malformed_stability
        absolute = (now - absolute_first) >= self._malformed_timeout
        escalating = stable or absolute
        already_escalated = existing is not None and existing.requires_human
        record = SupervisionRecord(
            supervision_id=identity,
            project=project.name,
            step_no=step_no,
            attempt=attempt,
            source_report_hash=digest,
            architecture_version=self.architecture_version,
            status=SupervisorStatus.MALFORMED,
            reason=reason,
            requires_human=bool(escalating or already_escalated),
            first_seen_at=first_seen,
            created_at=existing.created_at if existing is not None else now,
            updated_at=now,
        )
        with self._storage.transaction():
            self._storage.supervisions.upsert(record)
        if existing is None:
            self._emit(
                on_event,
                level=EVENT_LEVEL_WARN,
                action="malformed-report",
                message=(
                    f"the report of step {step_no} attempt {attempt} is not a "
                    f"valid worker report ({reason}); no supervisor was asked"
                ),
                step_no=step_no,
                now=now,
            )
        if escalating and not already_escalated:
            self._emit(
                on_event,
                level=EVENT_LEVEL_ERROR,
                action="malformed-timeout",
                message=(
                    f"the malformed report of step {step_no} attempt {attempt} "
                    f"reached the {'stability' if stable else 'absolute'} deadline; "
                    "an operator must look"
                ),
                step_no=step_no,
                now=now,
            )
        return SupervisionAnalysis(
            outcome="malformed",
            step_no=step_no,
            attempt=attempt,
            status=SupervisorStatus.MALFORMED.value,
            supervision_id=identity,
            source_report_hash=digest,
            provider_called=False,
            gate_allowed=False,
            waiting_for=WaitingFor.BLOCKED.value,
            reason=reason,
        )

    # -- internals: the provider call --------------------------------------
    def _ask(
        self,
        pending: SupervisionRecord,
        project: Project,
        step: Step,
        report: WorkerResult,
        digest: str,
        now: datetime,
        on_event: Optional[Callable[[dict[str, Any]], None]],
    ) -> SupervisionAnalysis:
        """Ask the supervisor once, then persist the validated answer."""
        context = self._build_context(project, step, report, digest)
        try:
            answer = self._supervisor.supervise(context)
        except BaseException as error:  # noqa: BLE001 - reported, never swallowed
            failed = replace(
                pending,
                status=SupervisorStatus.ERROR,
                reason=f"the supervisor failed: {exception_reason(error)}",
                requires_human=True,
                provider="",
                cost_available=False,
                updated_at=self._clock(),
            )
            with self._storage.transaction():
                self._storage.supervisions.upsert(failed)
            self._emit(
                on_event,
                level=EVENT_LEVEL_ERROR,
                action="analysis-error",
                message=(
                    f"supervision {pending.supervision_id[:16]} failed: "
                    f"{exception_reason(error)}"
                ),
                step_no=step.step_no,
                now=now,
            )
            return self._analysis(
                "analysis-error", failed, reason=failed.reason
            )
        record = self._apply_answer(
            pending, answer, project=project, step=step, now=now, on_event=on_event
        )
        return self._analysis(
            "analyzed", record, provider_called=True, reason=record.reason
        )

    def _analysis(
        self,
        outcome: str,
        record: SupervisionRecord,
        *,
        provider_called: bool = False,
        reason: str = "",
    ) -> SupervisionAnalysis:
        """One plain description of a finished analysis step."""
        return SupervisionAnalysis(
            outcome=outcome,
            step_no=record.step_no,
            attempt=record.attempt,
            status=record.status.value,
            supervision_id=record.supervision_id,
            source_report_hash=record.source_report_hash,
            action=None if record.action is None else record.action.value,
            provider_called=provider_called,
            gate_allowed=self._gate.verdict_for(record).allowed,
            waiting_for=self._gate.waiting_for_record(record).value,
            reason=reason or record.reason,
        )

    def _build_context(
        self,
        project: Project,
        step: Step,
        report: WorkerResult,
        digest: str,
    ) -> SupervisorContext:
        """The bounded, sanitized **facts** one supervisor may see.

        Every section is capped and every string is clipped, and nothing here is a
        transcript: the context carries the report, the task it answered, the
        workflow state around it and the bounded architecture context. No chat
        history, no log dump, no secret, no credential - and the report itself is
        already capped at :data:`MAX_REPORT_BYTES` by the channel contract.
        """
        task: Optional[Task] = self._storage.tasks.get(
            TaskKey(step.step_no, step.attempt)
        )
        raw = dict(report.raw or {})
        findings = [
            self._finding_row(item) for item in self._storage.findings.list()
        ]
        adrs = [
            self._adr_row(item)
            for item in self._storage.adrs.list()
            if item.status is ADRStatus.ACCEPTED
        ]
        risks = [
            self._risk_row(item) for item in self._storage.risks.list_open()
        ]
        changes = [
            self._change_row(item)
            for item in self._storage.change_requests.list()
            if item.status in (ACRStatus.PROPOSED, ACRStatus.APPROVED)
        ]
        return SupervisorContext(
            project=project.name,
            step_no=step.step_no,
            attempt=step.attempt,
            max_attempts=step.max_attempts,
            attempts_remaining=max(step.max_attempts - step.attempt, 0),
            step_state=step.state.value,
            mode=project.mode.value,
            paused=project.paused,
            architecture_version=self.architecture_version,
            source_report_hash=digest,
            task_title=task.title if task is not None else step.title,
            task_description=(
                task.description if task is not None else step.description
            ),
            task_instructions=(
                dict(task.instructions) if task is not None else {}
            ),
            report_status=report.status.value,
            report_summary=_clip(report.summary, MAX_SUPERVISOR_TEXT),
            report=raw,
            report_files_created=_text_sequence(raw.get("files_created")),
            report_files_changed=_text_sequence(raw.get("files_changed")),
            report_files_deleted=_text_sequence(raw.get("files_deleted")),
            report_tests=_mapping(raw.get("tests")),
            worker_issues=tuple(report.issues),
            worker_architecture_questions=tuple(report.architecture_questions),
            worker_dependencies_added=_text_sequence(
                raw.get("dependencies_added")
            ),
            deterministic_findings=tuple(findings[-MAX_SUPERVISOR_SECTION:]),
            accepted_adrs=tuple(adrs[-MAX_SUPERVISOR_SECTION:]),
            open_risks=tuple(risks[-MAX_SUPERVISOR_SECTION:]),
            open_change_requests=tuple(changes[-MAX_SUPERVISOR_SECTION:]),
            operator_constraints=self._operator_constraints,
        )

    @staticmethod
    def _finding_row(finding: Any) -> dict[str, Any]:
        """One deterministic finding fact (never the whole finding object)."""
        return {
            "id": str(finding.id),
            "claim": _clip(str(finding.claim), 400),
            "severity": finding.severity.value,
            "source": str(finding.source),
            "step_no": finding.step_no,
        }

    @staticmethod
    def _adr_row(adr: Any) -> dict[str, Any]:
        """One accepted decision record, compactly."""
        return {
            "id": str(adr.id),
            "title": _clip(str(adr.title), 200),
            "status": adr.status.value,
            "decision": _clip(str(adr.decision), 400),
        }

    @staticmethod
    def _risk_row(risk: Any) -> dict[str, Any]:
        """One open risk, compactly - facts, not the whole register entry."""
        return {
            "id": str(risk.id),
            "severity": risk.severity.value,
            "status": risk.status.value,
            "description": _clip(str(risk.description), 400),
            "mitigation": _clip(str(risk.mitigation), 300),
        }

    @staticmethod
    def _change_row(change: Any) -> dict[str, Any]:
        """One open architecture change request, compactly."""
        return {
            "request_id": str(change.request_id),
            "title": _clip(str(change.title), 200),
            "status": change.status.value,
            "source_version": str(change.source_version),
            "target_version": str(change.target_version),
        }

    # -- internals: turning an answer into a record ------------------------
    def _write(
        self,
        pending: SupervisionRecord,
        *,
        status: SupervisorStatus,
        **fields: Any,
    ) -> SupervisionRecord:
        """Persist one status change on the **same** row, in one transaction."""
        record = replace(pending, status=status, **fields)
        with self._storage.transaction():
            self._storage.supervisions.upsert(record)
        return record

    def _fail(
        self,
        pending: SupervisionRecord,
        reason: str,
        *,
        step_no: int,
        now: datetime,
        on_event: Optional[Callable[[dict[str, Any]], None]],
    ) -> SupervisionRecord:
        """Record a failed analysis as ``ERROR`` - never as a rejection.

        A supervisor that could not do its job must not be able to look as if it
        had *decided* something: ``REJECTED`` allows the review and is reachable
        only through an explicit human decision, so an analysis failure lands in a
        blocking ``ERROR`` instead.
        """
        record = self._write(
            pending,
            status=SupervisorStatus.ERROR,
            requires_human=True,
            action=None,
            reason=reason,
            updated_at=now,
        )
        self._emit(
            on_event,
            level=EVENT_LEVEL_ERROR,
            action="analysis-error",
            message=f"supervision {pending.supervision_id[:16]}: {reason}",
            step_no=step_no,
            now=now,
        )
        return record

    def _apply_answer(
        self,
        pending: SupervisionRecord,
        answer: Any,
        *,
        project: Project,
        step: Step,
        now: datetime,
        on_event: Optional[Callable[[dict[str, Any]], None]],
    ) -> SupervisionRecord:
        """Validate a supervisor's answer and record the state it implies.

        The answer is **inference**: it decides nothing by itself. What happens
        next is derived here - and the send class comes from
        :func:`~architecture_assistant.application.supervisor_policy.decide_send`,
        never from the answer's own ``risk`` label.
        """
        if not isinstance(answer, SupervisorResult):
            return self._fail(
                pending,
                "the supervisor answered with "
                f"{type(answer).__name__}, not a SupervisorResult",
                step_no=step.step_no,
                now=now,
                on_event=on_event,
            )
        common: dict[str, Any] = {
            "action": answer.action,
            "reason": answer.reason,
            "risk": answer.risk,
            "instruction_for_cline": answer.instruction_for_cline,
            "provider": answer.provider,
            "cost_available": answer.cost_available,
            "evidence": tuple(answer.evidence),
            "updated_at": now,
        }
        action = answer.action
        if action is SupervisorAction.ERROR:
            self._emit(
                on_event,
                level=EVENT_LEVEL_ERROR,
                action="analysis-error",
                message=(
                    f"supervision {pending.supervision_id[:16]} reported it could "
                    f"not analyse the report: {_clip(answer.reason, 200)}"
                ),
                step_no=step.step_no,
                now=now,
            )
            return self._write(
                pending,
                status=SupervisorStatus.ERROR,
                requires_human=True,
                **common,
            )
        if action is SupervisorAction.ESCALATE:
            self._emit(
                on_event,
                level=EVENT_LEVEL_WARN,
                action="escalation",
                message=(
                    f"supervision {pending.supervision_id[:16]} escalated to a "
                    f"human: {_clip(answer.reason, 200)}"
                ),
                step_no=step.step_no,
                now=now,
            )
            return self._write(
                pending,
                status=SupervisorStatus.ESCALATED,
                requires_human=True,
                escalated_at=now,
                **common,
            )
        if action is SupervisorAction.NO_ACTION:
            if answer.requires_human:
                fields = dict(common)
                fields["reason"] = (
                    f"{answer.reason} | the supervisor asked a human to look"
                )
                self._emit(
                    on_event,
                    level=EVENT_LEVEL_INFO,
                    action="waiting-human",
                    message=(
                        f"supervision {pending.supervision_id[:16]} proposed no "
                        "directive but asked for a human"
                    ),
                    step_no=step.step_no,
                    now=now,
                )
                return self._write(
                    pending,
                    status=SupervisorStatus.WAITING_HUMAN,
                    requires_human=True,
                    **fields,
                )
            self._emit(
                on_event,
                level=EVENT_LEVEL_INFO,
                action="analysis-completed",
                message=(
                    f"supervision {pending.supervision_id[:16]} needs no "
                    f"directive: {_clip(answer.reason, 200)}"
                ),
                step_no=step.step_no,
                now=now,
            )
            return self._write(
                pending,
                status=SupervisorStatus.NO_ACTION,
                requires_human=False,
                **common,
            )
        verdict = decide_send(
            mode=project.mode,
            action=action,
            risk=answer.risk,
            requires_human=answer.requires_human,
            instruction=answer.instruction_for_cline,
        )
        return self._record_directive(
            pending,
            answer=answer,
            verdict=verdict,
            step=step,
            now=now,
            on_event=on_event,
            common=common,
        )

    def _record_directive(
        self,
        pending: SupervisionRecord,
        *,
        answer: SupervisorResult,
        verdict: SendVerdict,
        step: Step,
        now: datetime,
        on_event: Optional[Callable[[dict[str, Any]], None]],
        common: dict[str, Any],
    ) -> SupervisionRecord:
        """Record the directive state: ready to send, or waiting for a human.

        Two facts can only ever move a directive *away* from an automatic send,
        and both are checked here rather than trusted to the supervisor: the
        authoritative attempt budget (a supervisor may not bypass ``max_attempts``)
        and the deterministic send policy.
        """
        if answer.action is SupervisorAction.RETRY and not step.can_retry:
            fields = dict(common)
            fields["reason"] = (
                f"{answer.reason} | no attempts remain "
                f"(attempt {step.attempt} of {step.max_attempts}): only the "
                "authoritative workflow may decide"
            )
            self._emit(
                on_event,
                level=EVENT_LEVEL_WARN,
                action="max-attempts",
                message=(
                    f"supervision {pending.supervision_id[:16]} suggested a retry "
                    f"while step {step.step_no} has no attempts left; a human "
                    "decides"
                ),
                step_no=step.step_no,
                now=now,
            )
            return self._write(
                pending,
                status=SupervisorStatus.WAITING_HUMAN,
                requires_human=True,
                **fields,
            )
        fields = dict(common)
        fields["reason"] = f"{answer.reason} | {verdict.reason}"
        if verdict.automatic:
            self._emit(
                on_event,
                level=EVENT_LEVEL_INFO,
                action="directive-created",
                message=(
                    f"supervision {pending.supervision_id[:16]} proposed a "
                    f"{answer.action.value} directive that may be sent "
                    f"automatically: {verdict.reason}"
                ),
                step_no=step.step_no,
                now=now,
            )
            return self._write(
                pending,
                status=SupervisorStatus.READY_TO_SEND,
                requires_human=False,
                **fields,
            )
        self._emit(
            on_event,
            level=EVENT_LEVEL_INFO,
            action="waiting-human",
            message=(
                f"supervision {pending.supervision_id[:16]} proposed a "
                f"{answer.action.value} directive that needs a human: "
                f"{verdict.reason}"
            ),
            step_no=step.step_no,
            now=now,
        )
        return self._write(
            pending,
            status=SupervisorStatus.WAITING_HUMAN,
            requires_human=True,
            **fields,
        )

    # -- internals: the send protocol --------------------------------------
    def _directive_payload(
        self, record: SupervisionRecord, *, text: str, now: datetime
    ) -> dict[str, Any]:
        """The deterministic directive artifact (Phase 15).

        It carries its own identity - ``supervision_id`` and
        ``source_report_hash`` - so a restart can tell *whose* directive is on
        disk, and so the worker side can tell which report a correction answers.
        """
        return {
            "kind": "supervisor-directive",
            "schema_version": DIRECTIVE_SCHEMA_VERSION,
            "project": record.project,
            "step_no": record.step_no,
            "attempt": record.attempt,
            "supervision_id": record.supervision_id,
            "source_report_hash": record.source_report_hash,
            "architecture_version": record.architecture_version,
            "action": None if record.action is None else record.action.value,
            "risk": None if record.risk is None else record.risk.value,
            "requires_human": record.requires_human,
            "instruction": text,
            "reason": record.reason,
            "created_at": now.isoformat(),
        }

    def _begin_send(
        self,
        record: SupervisionRecord,
        *,
        actor: str,
        reason: str,
        instruction: str,
        operation: str,
        auto: bool,
        now: datetime,
    ) -> tuple[SupervisionRecord, str]:
        """TX1: the durable send intent, plus exactly one audit entry.

        The intent is committed **before** anything is published, so a crash
        between the two leaves a record that says "a send was started" - which is
        exactly what reconciliation needs in order to finish the job honestly.
        """
        text = (instruction or record.instruction_for_cline).strip()
        if not text:
            raise SupervisionError(
                "a directive needs a non-empty instruction; refusing to publish "
                "an empty artifact"
            )
        pending = replace(
            record,
            status=SupervisorStatus.SEND_PENDING,
            instruction_for_cline=text,
            decided_by=actor,
            decided_at=now,
            decision_reason=reason,
            updated_at=now,
        )
        entry = self._entry(
            record,
            pending,
            action=AuditAction.UPDATE,
            operation=operation,
            actor=actor,
            reason=reason,
            now=now,
            extra={"auto": auto, "instruction": text, "sent": False},
        )
        with self._storage.transaction():
            self._storage.supervisions.upsert(pending)
            self._storage.audit.append(entry)
        return pending, text

    def _finish_send(
        self,
        pending: SupervisionRecord,
        *,
        now: datetime,
        actor: str,
        reason: str,
        on_event: Optional[Callable[[dict[str, Any]], None]],
    ) -> SupervisionRecord:
        """TX2: the durable send completion, plus exactly one audit entry.

        ``SENT`` still blocks the gate: it means a directive was delivered and the
        assistant is waiting for **new evidence**, not that supervision is over.
        """
        sent = replace(
            pending, status=SupervisorStatus.SENT, sent_at=now, updated_at=now
        )
        artifact = (
            f"step_{pending.step_no:03d}_attempt_{pending.attempt:03d}"
            "_directive.json"
        )
        entry = self._entry(
            pending,
            sent,
            action=AuditAction.UPDATE,
            operation="directive-sent",
            actor=actor,
            reason=reason,
            now=now,
            extra={"artifact": artifact},
        )
        with self._storage.transaction():
            self._storage.supervisions.upsert(sent)
            self._storage.audit.append(entry)
        self._emit(
            on_event,
            level=EVENT_LEVEL_INFO,
            action="directive-sent",
            message=(
                f"directive for supervision {sent.supervision_id[:16]} published; "
                "waiting for a new worker report"
            ),
            step_no=sent.step_no,
            now=now,
        )
        return sent

    def _send(
        self,
        record: SupervisionRecord,
        *,
        actor: str,
        reason: str,
        instruction: str,
        operation: str,
        auto: bool,
        on_event: Optional[Callable[[dict[str, Any]], None]],
    ) -> tuple[SupervisionRecord, bool]:
        """Intent, publication, completion - in that order, always.

        Nothing here claims exactly-once delivery: the intent is durable before
        the file is written, the publication is at-least-once and the artifact is
        deterministic, so a failure at any point leaves a record that the next
        reconciliation can finish without inventing anything.
        """
        now = self._clock()
        pending, text = self._begin_send(
            record,
            actor=actor,
            reason=reason,
            instruction=instruction,
            operation=operation,
            auto=auto,
            now=now,
        )
        payload = self._directive_payload(pending, text=text, now=now)
        try:
            self._worker.publish_directive(
                payload, step_no=pending.step_no, attempt=pending.attempt
            )
        except BaseException as error:  # noqa: BLE001 - reported, never hidden
            self._emit(
                on_event,
                level=EVENT_LEVEL_ERROR,
                action="send-pending",
                message=(
                    f"the directive for supervision {pending.supervision_id[:16]} "
                    f"could not be published ({exception_reason(error)}); the send "
                    "intent is durable and will be reconciled"
                ),
                step_no=pending.step_no,
                now=now,
            )
            return pending, False
        sent = self._finish_send(
            pending,
            now=self._clock(),
            actor=actor,
            reason=reason,
            on_event=on_event,
        )
        return sent, True

    # -- internals: staleness ----------------------------------------------
    def _staleness(self, record: SupervisionRecord) -> Optional[str]:
        """Why this identity is no longer live - or ``None`` when it still is.

        Everything checked here is an assistant fact: the managed project, the
        architecture version this process runs, the step's own attempt and state,
        whether the project is paused and whether the report bytes are still the
        bytes the record was made for. Nothing consults the supervisor, so a
        provider cannot keep a stale directive alive.
        """
        project = self._project()
        if record.project != project.name:
            return (
                f"it belongs to project {record.project!r} while this database "
                f"manages {project.name!r}"
            )
        if record.architecture_version != self.architecture_version:
            return (
                f"it was analysed under architecture "
                f"{record.architecture_version}, and this process runs "
                f"{self.architecture_version}"
            )
        step = self._storage.steps.get(record.step_no)
        if step is None:
            return f"step {record.step_no} no longer exists"
        if step.attempt != record.attempt:
            return (
                f"the step moved to attempt {step.attempt}; this record answers "
                f"attempt {record.attempt}"
            )
        if step.state not in LIVE_STEP_STATES:
            return (
                f"the step is {step.state.value}, which is past the report this "
                "directive answers"
            )
        if project.paused:
            return "the project is paused"
        digest = self._gate.current_report_hash(record.step_no, record.attempt)
        if digest is None:
            return "the report this directive answers is gone"
        if digest != record.source_report_hash:
            return "the worker report changed after this directive was analysed"
        return None

    def _mark_stale(
        self,
        record: SupervisionRecord,
        *,
        reason: str,
        now: datetime,
        on_event: Optional[Callable[[dict[str, Any]], None]],
    ) -> SupervisionRecord:
        """Mark an identity stale - it can never be sent, and it still blocks."""
        stale = replace(
            record,
            status=SupervisorStatus.STALE,
            requires_human=True,
            reason=f"{record.reason} | {reason}".strip(" |"),
            updated_at=now,
        )
        with self._storage.transaction():
            self._storage.supervisions.upsert(stale)
        self._emit(
            on_event,
            level=EVENT_LEVEL_WARN,
            action="stale-directive",
            message=(
                f"supervision {record.supervision_id[:16]} is stale and will not "
                f"be sent: {reason}"
            ),
            step_no=record.step_no,
            now=now,
        )
        return stale

    def _revalidate(
        self,
        record: SupervisionRecord,
        *,
        now: datetime,
        on_event: Optional[Callable[[dict[str, Any]], None]],
    ) -> None:
        """Refuse to act on a stale identity, after recording ``STALE``."""
        problem = self._staleness(record)
        if problem is None:
            return
        self._mark_stale(record, reason=problem, now=now, on_event=on_event)
        raise SupervisionStaleError(
            f"supervision {record.supervision_id} is stale: {problem}"
        )

    # -- reconciliation and the tick ---------------------------------------
    def reconcile(
        self, *, on_event: Optional[Callable[[dict[str, Any]], None]] = None
    ) -> tuple[SupervisionAnalysis, ...]:
        """Resolve everything a restart may have left half-done.

        Two statuses are unresolved by construction: ``SEND_PENDING`` (the intent
        is durable, the publication may or may not have happened) and
        ``ANALYSIS_PENDING`` (the analysis was owed when the process died). Both
        are resolved from persisted state plus the exchange channel - never from a
        guess - and both are idempotent, so running this twice is harmless.

        With supervision disabled there is nothing to reconcile **by design**: no
        row can have been created or left half-done while it was off, and anything
        an *earlier* enabled session left behind is resolved the moment supervision
        is enabled again - on the first tick, before anything else happens.
        """
        if not self.enabled:
            return ()
        results: list[SupervisionAnalysis] = []
        for record in self._storage.supervisions.list_unresolved():
            if record.status is SupervisorStatus.SEND_PENDING:
                results.append(self._reconcile_send(record, on_event=on_event))
            elif record.status is SupervisorStatus.ANALYSIS_PENDING:
                results.append(
                    self._reconcile_analysis(record, on_event=on_event)
                )
        return tuple(results)

    def _reconcile_send(
        self,
        record: SupervisionRecord,
        *,
        on_event: Optional[Callable[[dict[str, Any]], None]],
    ) -> SupervisionAnalysis:
        """Finish, correct or abandon one interrupted send.

        The artifact itself is the arbiter: it carries its own ``supervision_id``
        and ``source_report_hash``, so "did my publication happen?" is answered by
        reading it rather than by assuming. Four outcomes, all honest:

        * nothing on disk (or it is unreadable) - publish and finish;
        * the artifact is **ours** - the publication succeeded before the crash,
          so only the completion transaction is owed;
        * the artifact belongs to a **later** identity - this record was
          superseded, so it becomes ``STALE`` and is never sent;
        * the artifact belongs to an *earlier* identity while this record is the
          latest - the artifact is corrected by republishing ours (deterministic,
          at-least-once) and only then finished.
        """
        now = self._clock()
        published: Optional[Mapping[str, Any]] = None
        try:
            published = self._worker.read_directive(
                record.step_no, record.attempt
            )
        except BaseException as error:  # noqa: BLE001 - unreadable is not success
            self._emit(
                on_event,
                level=EVENT_LEVEL_WARN,
                action="send-pending",
                message=(
                    "the directive artifact of step "
                    f"{record.step_no} attempt {record.attempt} is unreadable "
                    f"({exception_reason(error)}); it will be republished"
                ),
                step_no=record.step_no,
                now=now,
            )
            published = None
        if published is not None:
            owner = str(published.get("supervision_id") or "")
            owner_hash = str(published.get("source_report_hash") or "")
            mine = (
                owner == record.supervision_id
                and owner_hash == record.source_report_hash
            )
            if not mine:
                history = self._storage.supervisions.list_for_step(
                    record.project, record.step_no, record.attempt
                )
                if not self.is_latest(record, history):
                    stale = self._mark_stale(
                        record,
                        reason=(
                            "a later supervision owns the directive artifact of "
                            "this dispatch"
                        ),
                        now=now,
                        on_event=on_event,
                    )
                    return self._analysis("stale", stale, reason=stale.reason)
                self._emit(
                    on_event,
                    level=EVENT_LEVEL_WARN,
                    action="send-pending",
                    message=(
                        f"the directive artifact of step {record.step_no} "
                        f"attempt {record.attempt} belongs to another "
                        "supervision; republishing this one"
                    ),
                    step_no=record.step_no,
                    now=now,
                )
                published = None
        return self._publish_reconciled(
            record, published=published, now=now, on_event=on_event
        )

    def _publish_reconciled(
        self,
        record: SupervisionRecord,
        *,
        published: Optional[Mapping[str, Any]],
        now: datetime,
        on_event: Optional[Callable[[dict[str, Any]], None]],
    ) -> SupervisionAnalysis:
        """Publish when nothing usable is on disk, then finish either way."""
        if published is None:
            payload = self._directive_payload(
                record, text=record.instruction_for_cline, now=now
            )
            try:
                self._worker.publish_directive(
                    payload, step_no=record.step_no, attempt=record.attempt
                )
            except BaseException as error:  # noqa: BLE001 - reported, never hidden
                self._emit(
                    on_event,
                    level=EVENT_LEVEL_ERROR,
                    action="send-pending",
                    message=(
                        f"the directive for supervision "
                        f"{record.supervision_id[:16]} still cannot be published "
                        f"({exception_reason(error)}); the intent stays durable"
                    ),
                    step_no=record.step_no,
                    now=now,
                )
                return self._analysis(
                    "send-pending",
                    record,
                    reason=f"publish failed: {exception_reason(error)}",
                )
            sent = self._finish_send(
                record,
                now=self._clock(),
                actor=record.decided_by,
                reason=record.decision_reason,
                on_event=on_event,
            )
            return self._analysis(
                "send-recovered",
                sent,
                reason="the directive was published during reconciliation",
            )
        sent = self._finish_send(
            record,
            now=self._clock(),
            actor=record.decided_by,
            reason=record.decision_reason,
            on_event=on_event,
        )
        return self._analysis(
            "send-completed",
            sent,
            reason="the directive artifact was already published",
        )

    def _reconcile_analysis(
        self,
        record: SupervisionRecord,
        *,
        on_event: Optional[Callable[[dict[str, Any]], None]],
    ) -> SupervisionAnalysis:
        """Re-run an analysis that was owed when the process died.

        The report must still be the same report: if the step moved on or the
        bytes changed, the owed analysis is ``STALE`` instead - the new bytes get
        their own identity and their own analysis, so nothing is ever attributed
        to the wrong report.
        """
        now = self._clock()
        step = self._storage.steps.get(record.step_no)
        if step is None or step.attempt != record.attempt:
            stale = self._mark_stale(
                record,
                reason="the step moved on while the analysis was owed",
                now=now,
                on_event=on_event,
            )
            return self._analysis("stale", stale, reason=stale.reason)
        digest = self._gate.current_report_hash(record.step_no, record.attempt)
        if digest != record.source_report_hash:
            stale = self._mark_stale(
                record,
                reason="the report changed while the analysis was owed",
                now=now,
                on_event=on_event,
            )
            return self._analysis("stale", stale, reason=stale.reason)
        return self.analyze(record.step_no, record.attempt, on_event=on_event)

    def advance(
        self,
        step_no: int,
        attempt: int,
        *,
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> SupervisionAnalysis:
        """Bring the current report one supervision step forward, cheaply.

        This is what a tick calls after :meth:`reconcile`: it does nothing at all
        for a report that already has a decided record, which is what keeps a
        two-second tick from calling a provider, rebuilding a context or writing
        an audit entry.

        With supervision disabled it does nothing at all, full stop.
        """
        if not self.enabled:
            return self._disabled_analysis(step_no, attempt)
        record = self.current(step_no, attempt)
        if record is None or record.status is SupervisorStatus.ANALYSIS_PENDING:
            return self.analyze(step_no, attempt, on_event=on_event)
        if record.status is SupervisorStatus.READY_TO_SEND:
            return self._auto_send(record, on_event=on_event)
        return self._analysis(
            "unchanged", record, reason="this report is already supervised"
        )

    def _auto_send(
        self,
        record: SupervisionRecord,
        *,
        on_event: Optional[Callable[[dict[str, Any]], None]],
    ) -> SupervisionAnalysis:
        """Deliver a directive the deterministic policy allows - or hand it back.

        The policy is re-derived here, at the moment of sending, from the *live*
        project: a mode that changed to ``MANUAL`` since the analysis, a paused
        project or a step that moved on all stop the automatic send instead of
        silently going ahead with an older decision.
        """
        now = self._clock()
        self._revalidate(record, now=now, on_event=on_event)
        project = self._project()
        if record.action is None or record.risk is None:
            return self._analysis(
                "unchanged", record, reason="this record carries no directive"
            )
        verdict = decide_send(
            mode=project.mode,
            action=record.action,
            risk=record.risk,
            requires_human=record.requires_human,
            instruction=record.instruction_for_cline,
        )
        if not verdict.automatic:
            waiting = self._write(
                record,
                status=SupervisorStatus.WAITING_HUMAN,
                requires_human=True,
                reason=f"{record.reason} | {verdict.reason}",
                updated_at=now,
            )
            self._emit(
                on_event,
                level=EVENT_LEVEL_INFO,
                action="waiting-human",
                message=(
                    f"supervision {record.supervision_id[:16]} can no longer be "
                    f"sent automatically: {verdict.reason}"
                ),
                step_no=record.step_no,
                now=now,
            )
            return self._analysis(
                "waiting-human", waiting, reason=verdict.reason
            )
        sent, published = self._send(
            record,
            actor=f"auto:{project.mode.value}",
            reason=f"{project.mode.value} mode: {verdict.reason}",
            instruction=record.instruction_for_cline,
            operation="auto-send",
            auto=True,
            on_event=on_event,
        )
        return self._analysis(
            "auto-sent" if published else "send-pending",
            sent,
            reason=verdict.reason,
        )

    # -- human supervision control -----------------------------------------
    def approve_and_send(
        self,
        supervision_id: str,
        *,
        actor: str,
        reason: str,
        instruction: str = "",
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> SupervisionRecord:
        """A human approves a directive and it is published.

        The identity is re-validated immediately before the mutation, so a
        project that was paused, an attempt that moved on or a report that changed
        since the analysis makes the record ``STALE`` instead of sendable. An
        operator may replace the instruction - what is published is exactly what
        the audit entry records.

        Refused outright while supervision is disabled: there is no supervision to
        approve, and nothing is read, written or published.
        """
        self._require_enabled()
        record = self._require(supervision_id, actor=actor, reason=reason)
        if record.status is SupervisorStatus.SENT:
            return record
        now = self._clock()
        if record.status is SupervisorStatus.SEND_PENDING:
            return self._finish_send(
                record,
                now=now,
                actor=actor,
                reason=reason,
                on_event=on_event,
            )
        self._assert_sendable(record)
        self._revalidate(record, now=now, on_event=on_event)
        sent, _published = self._send(
            record,
            actor=actor,
            reason=reason,
            instruction=instruction or record.instruction_for_cline,
            operation="approve-send",
            auto=False,
            on_event=on_event,
        )
        return sent

    def reject_directive(
        self,
        supervision_id: str,
        *,
        actor: str,
        reason: str,
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> SupervisionRecord:
        """A human rejects the directive; the exact report may then be reviewed.

        Deliberately refused for a ``SENT`` record: a directive that was already
        delivered cannot be un-sent, and the whole point of ``SENT`` is that the
        report it answered is never reviewed afterwards. New evidence resolves it.
        """
        self._require_enabled()
        record = self._require(supervision_id, actor=actor, reason=reason)
        if record.status is SupervisorStatus.REJECTED:
            return record
        if record.status is SupervisorStatus.SENT:
            raise SupervisionNotSendableError(
                "a sent directive cannot be rejected: SENT is resolved only by a "
                "new worker report"
            )
        if record.status in SUPERVISION_ALLOWING_STATUSES:
            raise SupervisionNotSendableError(
                f"a {record.status.value} record is already decided; there is "
                "nothing to reject"
            )
        return self._decide(
            record,
            status=SupervisorStatus.REJECTED,
            operation="reject-directive",
            action=AuditAction.REJECT,
            event="directive-rejected",
            level=EVENT_LEVEL_WARN,
            actor=actor,
            reason=reason,
            on_event=on_event,
        )

    def waive_supervision(
        self,
        supervision_id: str,
        *,
        actor: str,
        reason: str,
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> SupervisionRecord:
        """A human waives supervision for this exact report, allowing review.

        The record keeps its ``decided_by``/``decision_reason``, so the waiver is
        as auditable as any other decision - and the gate re-checks those fields,
        which is what makes "waived" impossible to forge.
        """
        self._require_enabled()
        record = self._require(supervision_id, actor=actor, reason=reason)
        if record.status is SupervisorStatus.WAIVED:
            return record
        if record.status is SupervisorStatus.SENT:
            raise SupervisionNotSendableError(
                "a sent directive cannot be waived: SENT is resolved only by a new "
                "worker report"
            )
        if record.status in SUPERVISION_ALLOWING_STATUSES:
            raise SupervisionNotSendableError(
                f"a {record.status.value} record already allows the review; there "
                "is nothing to waive"
            )
        return self._decide(
            record,
            status=SupervisorStatus.WAIVED,
            operation="waive-supervision",
            action=AuditAction.UPDATE,
            event="waiver",
            level=EVENT_LEVEL_WARN,
            actor=actor,
            reason=reason,
            on_event=on_event,
        )

    def escalate(
        self,
        supervision_id: str,
        *,
        actor: str,
        reason: str,
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> SupervisionRecord:
        """A human escalates the record: it keeps blocking until it is resolved."""
        self._require_enabled()
        record = self._require(supervision_id, actor=actor, reason=reason)
        if record.status is SupervisorStatus.ESCALATED:
            return record
        if record.status is SupervisorStatus.SENT:
            raise SupervisionNotSendableError(
                "a sent directive cannot be escalated: SENT is resolved only by a "
                "new worker report"
            )
        if record.status in SUPERVISION_ALLOWING_STATUSES:
            raise SupervisionNotSendableError(
                f"a {record.status.value} record needs no escalation: it already "
                "allows the authoritative review"
            )
        return self._decide(
            record,
            status=SupervisorStatus.ESCALATED,
            operation="escalate-supervision",
            action=AuditAction.UPDATE,
            event="escalation",
            level=EVENT_LEVEL_WARN,
            actor=actor,
            reason=reason,
            on_event=on_event,
        )

    def _decide(
        self,
        record: SupervisionRecord,
        *,
        status: SupervisorStatus,
        operation: str,
        action: AuditAction,
        event: str,
        level: str,
        actor: str,
        reason: str,
        on_event: Optional[Callable[[dict[str, Any]], None]],
    ) -> SupervisionRecord:
        """One human decision: one status change and exactly one audit entry."""
        now = self._clock()
        fields: dict[str, Any] = {
            "decided_by": actor,
            "decided_at": now,
            "decision_reason": reason,
            "requires_human": status is SupervisorStatus.ESCALATED,
            "updated_at": now,
        }
        if status is SupervisorStatus.ESCALATED:
            fields["escalated_at"] = now
        decided = replace(record, status=status, **fields)
        entry = self._entry(
            record,
            decided,
            action=action,
            operation=operation,
            actor=actor,
            reason=reason,
            now=now,
            extra={},
        )
        with self._storage.transaction():
            self._storage.supervisions.upsert(decided)
            self._storage.audit.append(entry)
        self._emit(
            on_event,
            level=level,
            action=event,
            message=(
                f"supervision {record.supervision_id[:16]} is now "
                f"{status.value} by {actor}: {_clip(reason, 200)}"
            ),
            step_no=record.step_no,
            now=now,
        )
        return decided

    # -- internals: guards, events, audit ----------------------------------
    def _require(
        self, supervision_id: str, *, actor: str, reason: str
    ) -> SupervisionRecord:
        """Load a record for a human action, or fail closed."""
        if not isinstance(actor, str) or not actor.strip():
            raise SupervisionActorRequiredError(
                f"actor must be a non-empty string; got {actor!r}"
            )
        if not isinstance(reason, str) or not reason.strip():
            raise SupervisionReasonRequiredError(
                f"reason must be a non-empty string; got {reason!r}"
            )
        return self.record(supervision_id)

    def _assert_sendable(self, record: SupervisionRecord) -> None:
        """Only a directive that is waiting may be approved and sent."""
        if record.status not in (
            SupervisorStatus.WAITING_HUMAN,
            SupervisorStatus.READY_TO_SEND,
        ):
            raise SupervisionNotSendableError(
                f"a {record.status.value} record cannot be sent; only a "
                "WAITING_HUMAN or READY_TO_SEND directive may be approved"
            )
        if not record.has_directive:
            raise SupervisionNotSendableError(
                "this record carries no instruction, so there is nothing to send"
            )

    def _project(self) -> Project:
        """The single managed project, or fail closed."""
        projects = self._storage.projects.list()
        if not projects:
            raise SupervisionError("the source of truth contains no project")
        if len(projects) > 1:
            names = ", ".join(sorted(project.name for project in projects))
            raise SupervisionError(
                f"expected exactly one project, found {len(projects)}: {names}"
            )
        return projects[0]

    def _step(self, step_no: int) -> Step:
        """One step, or fail closed."""
        step = self._storage.steps.get(step_no)
        if step is None:
            raise SupervisionError(f"step {step_no} does not exist")
        return step

    def _entry(
        self,
        before: SupervisionRecord,
        after: SupervisionRecord,
        *,
        action: AuditAction,
        operation: str,
        actor: str,
        reason: str,
        now: datetime,
        extra: Mapping[str, Any],
    ) -> AuditEntry:
        """One durable supervision decision - structured, never prose."""
        detail: dict[str, Any] = {
            "operation": operation,
            "actor": actor,
            "reason": reason,
            "project": after.project,
            "step_no": after.step_no,
            "attempt": after.attempt,
            "supervision_id": after.supervision_id,
            "source_report_hash": after.source_report_hash,
            "status_from": before.status.value,
            "status_to": after.status.value,
            "action": None if after.action is None else after.action.value,
            "risk": None if after.risk is None else after.risk.value,
        }
        detail.update(dict(extra))
        return AuditEntry(
            entity_type=AuditEntityType.SUPERVISION,
            entity_id=after.supervision_id,
            action=action,
            detail=detail,
            created_at=now,
        )

    def _emit(
        self,
        on_event: Optional[Callable[[dict[str, Any]], None]],
        *,
        level: str,
        action: str,
        message: str,
        step_no: Optional[int],
        now: datetime,
    ) -> None:
        """Hand one validated, sanitized event to the injected hook.

        Nothing is logged unless the caller asked to be told: the runtime and the
        GUI pass a hook, a test may pass ``None`` - and no audit entry is ever
        written here, because an event is not a decision.
        """
        if on_event is None:
            return
        on_event(
            build_event(
                level=level,
                component=SUPERVISOR_COMPONENT,
                action=action,
                message=message,
                step_no=step_no,
                timestamp=now,
            )
        )


def _clip(text: str, limit: int) -> str:
    """A bounded rendering of one string (never sanitized - this is fact)."""
    value = str(text)
    return value if len(value) <= limit else value[: limit - 1] + "\u2026"


def _text_sequence(value: Any) -> tuple[str, ...]:
    """The string entries of a report field, defensively - never a surprise."""
    if not isinstance(value, (list, tuple)):
        return ()
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        items.append(_clip(item, 400))
        if len(items) >= MAX_SUPERVISOR_SECTION:
            break
    return tuple(items)
