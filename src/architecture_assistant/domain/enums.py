"""Deterministic domain enumerations.

All enums subclass :class:`enum.StrEnum`, so members serialize to plain strings
(JSON / SQLite TEXT) while remaining type-safe. The module is pure standard
library - no third-party dependencies.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "Phase",
    "Mode",
    "RiskLevel",
    "Severity",
    "StepState",
    "StepEvent",
    "TaskState",
    "TaskEvent",
    "ReportStatus",
    "ACRStatus",
    "ADRStatus",
    "RiskStatus",
    "DecisionStatus",
    "ProposalStatus",
    "DELIBERATION_MAX_REVIEW_ROUNDS",
    "DeliberationStatus",
    "DeliberationStage",
    "DeliberationStageStatus",
    "Round2Decision",
    "SupervisorAction",
    "SupervisorRisk",
    "SupervisorStatus",
    "SUPERVISION_BLOCKING_STATUSES",
    "SUPERVISION_ALLOWING_STATUSES",
    "SUPERVISION_RECONCILE_STATUSES",
]


class Phase(StrEnum):
    """Build-plan phases (master plan v0.2)."""

    FOUNDATION = "FOUNDATION"
    CONTEXT = "CONTEXT"
    ARCHITECTURE = "ARCHITECTURE"
    PLUGINS = "PLUGINS"
    CLINE = "CLINE"
    LOOP = "LOOP"
    CONTROL = "CONTROL"
    EVOLUTION = "EVOLUTION"
    AI = "AI"
    MONITOR = "MONITOR"
    EXPORT = "EXPORT"
    TEST = "TEST"
    FINAL = "FINAL"


class Mode(StrEnum):
    """Assistant operating mode."""

    MANUAL = "MANUAL"
    SUPERVISED = "SUPERVISED"
    AUTO = "AUTO"


class RiskLevel(StrEnum):
    """Discretionary risk level of a Step or Task."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class Severity(StrEnum):
    """Severity of a registered Risk or a Finding."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class StepState(StrEnum):
    """Master workflow step states (Assistant <-> Cline loop).

    ``VERIFIED`` is the successful terminal state; ``ABORTED`` is the cancelled
    terminal state. No second "completed" state exists.
    """

    PENDING = "PENDING"
    READY = "READY"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    DISPATCHED = "DISPATCHED"
    CLINE_WORKING = "CLINE_WORKING"
    REPORT_RECEIVED = "REPORT_RECEIVED"
    REVIEWING = "REVIEWING"
    REVISE = "REVISE"
    BLOCKED = "BLOCKED"
    CONFLICT = "CONFLICT"
    FAILED = "FAILED"
    VERIFIED = "VERIFIED"
    ABORTED = "ABORTED"


class StepEvent(StrEnum):
    """Events driving the deterministic Step FSM."""

    PREPARE = "PREPARE"
    REQUEST_APPROVAL = "REQUEST_APPROVAL"
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    DISPATCH = "DISPATCH"
    CLINE_START = "CLINE_START"
    RECEIVE_REPORT = "RECEIVE_REPORT"
    FAIL = "FAIL"
    BLOCK = "BLOCK"
    ABORT = "ABORT"
    START_REVIEW = "START_REVIEW"
    VERIFY = "VERIFY"
    REQUEST_REVISE = "REQUEST_REVISE"
    RAISE_CONFLICT = "RAISE_CONFLICT"
    RETRY = "RETRY"
    UNBLOCK = "UNBLOCK"
    RESOLVE = "RESOLVE"


class TaskState(StrEnum):
    """Minimal Cline-dispatch state.

    Subordinate to the Step lifecycle: it only tracks dispatch/report handling
    and never drives Step transitions.
    """

    CREATED = "CREATED"
    DISPATCHED = "DISPATCHED"
    REPORTED = "REPORTED"
    FAILED = "FAILED"


class TaskEvent(StrEnum):
    """Events driving the minimal Task dispatch FSM."""

    DISPATCH = "DISPATCH"
    RECEIVE_REPORT = "RECEIVE_REPORT"
    FAIL = "FAIL"


class ReportStatus(StrEnum):
    """Cline -> Assistant report outcome (master contract)."""

    DONE = "DONE"
    REVISE = "REVISE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class ACRStatus(StrEnum):
    """Architecture Change Request lifecycle.

    ``PROPOSED`` is the entry state, ``APPROVED`` means a human accepted the
    change (the linked ADR is ``ACCEPTED``), ``REJECTED`` is terminal-negative
    and ``APPLIED`` is terminal-positive. A rejected request is never revived -
    the same idea needs a new request with a new ``request_id``.
    """

    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    APPLIED = "APPLIED"


class ADRStatus(StrEnum):
    """Architecture Decision Record lifecycle status."""

    PROPOSED = "PROPOSED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    DEPRECATED = "DEPRECATED"
    SUPERSEDED = "SUPERSEDED"


class RiskStatus(StrEnum):
    """Risk register entry status."""

    OPEN = "OPEN"
    MITIGATED = "MITIGATED"
    ACCEPTED = "ACCEPTED"
    CLOSED = "CLOSED"


class DecisionStatus(StrEnum):
    """Evidence-merger / decision-engine outcome."""

    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    ABSTAIN = "ABSTAIN"
    ERROR = "ERROR"


class ProposalStatus(StrEnum):
    """Lifecycle of a **managed-project** architecture proposal.

    Deliberately *not* a baseline lifecycle: an
    :class:`~architecture_assistant.domain.models.ArchitectureVersion` is the
    architecture of the assistant itself (declared in code and enforced by the
    deterministic validator), while a proposal describes a design for the
    project the assistant manages. ``DRAFT`` is the entry state, ``APPROVED``
    means a human accepted the design for the managed project, ``REJECTED`` is
    terminal-negative, ``REVISION_REQUESTED`` asks for another proposal and
    ``SUPERSEDED`` marks a proposal a newer revision replaced. History is never
    rewritten: only the status of a row moves, its content is frozen.
    """

    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    REVISION_REQUESTED = "REVISION_REQUESTED"
    SUPERSEDED = "SUPERSEDED"


#: How many review rounds a deliberation may run. Exactly one: the second round
#: is a **single** bounded reconsideration, never an autonomous debate loop
#: (``A -> B -> A -> B -> ...``). It is a constant of the lifecycle, not a knob.
DELIBERATION_MAX_REVIEW_ROUNDS = 1


class DeliberationStatus(StrEnum):
    """Lifecycle of one controlled architecture deliberation.

    This is deliberately **not** :class:`StepState`: the Step FSM drives the
    assistant's Cline loop (dispatch, report, verify, retry) and shares none of
    this lifecycle. A deliberation is an advisory review board over one operator
    requirement, and it moves strictly forward through its stages:

    ``DRAFT`` -> ``ROUND1_RUNNING`` -> ``ROUND1_COMPLETE`` ->
    ``LEAD_REVIEW_RUNNING`` -> ``LEAD_REVIEW_COMPLETE`` -> ``ROUND2_RUNNING`` ->
    ``ROUND2_COMPLETE`` -> ``SYNTHESIS_RUNNING`` -> ``READY_FOR_PROPOSAL``.

    ``ERROR`` records a stage failure (the run keeps every completed stage) and
    ``CANCELLED`` a deliberate human stop. Neither is a licence to loop: a failed
    stage is retried only by an explicit operator action.
    """

    DRAFT = "DRAFT"
    ROUND1_RUNNING = "ROUND1_RUNNING"
    ROUND1_COMPLETE = "ROUND1_COMPLETE"
    LEAD_REVIEW_RUNNING = "LEAD_REVIEW_RUNNING"
    LEAD_REVIEW_COMPLETE = "LEAD_REVIEW_COMPLETE"
    ROUND2_RUNNING = "ROUND2_RUNNING"
    ROUND2_COMPLETE = "ROUND2_COMPLETE"
    SYNTHESIS_RUNNING = "SYNTHESIS_RUNNING"
    READY_FOR_PROPOSAL = "READY_FOR_PROPOSAL"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"


class DeliberationStage(StrEnum):
    """The six addressable stages of a deliberation.

    Every stage result is stored as its own artifact row, so a stage is
    individually addressable (round history, traceability, cost attribution,
    restart safety) instead of being hidden inside one opaque document.
    """

    AGENT_A_ROUND1 = "AGENT_A_ROUND1"
    AGENT_B_ROUND1 = "AGENT_B_ROUND1"
    LEAD_REVIEW = "LEAD_REVIEW"
    AGENT_A_ROUND2 = "AGENT_A_ROUND2"
    AGENT_B_ROUND2 = "AGENT_B_ROUND2"
    FINAL_SYNTHESIS = "FINAL_SYNTHESIS"


class DeliberationStageStatus(StrEnum):
    """Outcome of one deliberation stage.

    ``COMPLETE`` - the stage produced a usable structured result;
    ``ABSTAINED`` - a provider the operator deliberately disabled answered
    nothing (no call was made); ``ERROR`` - the stage failed (a provider failure,
    a malformed answer or a missing collaborator) and the honest reason is
    recorded; ``INSUFFICIENT_PEER_REVIEW`` - the peer stage failed, so the Lead
    could not review two independent proposals and the run must never pretend it
    did; ``PENDING`` - the stage has not run yet.
    """

    PENDING = "PENDING"
    COMPLETE = "COMPLETE"
    ABSTAINED = "ABSTAINED"
    ERROR = "ERROR"
    INSUFFICIENT_PEER_REVIEW = "INSUFFICIENT_PEER_REVIEW"


class Round2Decision(StrEnum):
    """How one architect answered the Lead's structured review of its Round 1.

    ``KEEP`` - the Round-1 recommendation stands unchanged;
    ``REVISE`` - some decisions changed (``changed_decisions`` is non-empty);
    ``WITHDRAW`` - the recommendation is withdrawn entirely. None of the three is
    a vote and none of them is a majority: each architect answers about its own
    proposal, and the Lead reasons over the answers.
    """

    KEEP = "KEEP"
    REVISE = "REVISE"
    WITHDRAW = "WITHDRAW"


class SupervisorAction(StrEnum):
    """What a supervisor **recommends** for one worker report.

    This is the supervisor's *inference*, never a workflow event: the assistant
    owns the FSM, so no member of this enum can move a Step. ``RETRY`` asks the
    worker to redo the work inside the **same** attempt - the authoritative
    attempt counter is incremented only by the assistant's own RETRY path, never
    by a supervisor. ``ESCALATE`` hands the decision to a human and
    ``ERROR`` reports that the analysis itself failed.
    """

    NO_ACTION = "NO_ACTION"
    REVISE = "REVISE"
    CLARIFY = "CLARIFY"
    RETRY = "RETRY"
    ESCALATE = "ESCALATE"
    ERROR = "ERROR"


class SupervisorRisk(StrEnum):
    """How much a supervisor's *inference* could affect the project.

    Deliberately its own vocabulary rather than a reuse of
    :class:`RiskLevel`: this is an advisory classification of one proposed
    directive, not the registered risk of a Step. The application never trusts it
    alone - the automatic-send policy re-derives the send class itself.
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class SupervisorStatus(StrEnum):
    """Lifecycle of one supervision identity (one exact report hash).

    The identity is ``(project, step_no, attempt, source_report_hash,
    architecture_version)``, so a rewritten report is a *new* record and the
    decision taken for the previous bytes can never authorize the new ones.

    ``SENT`` is deliberately **not** completion: it means "a directive has been
    delivered to the worker and we are waiting for corrected evidence". Every
    state except :data:`SUPERVISION_ALLOWING_STATUSES` blocks the authoritative
    review, and a **missing row blocks as well** - the gate is fail-closed, so a
    report nobody supervised can never drift into deterministic review.
    """

    #: A row exists and its analysis is owed; the record is written *before* the
    #: supervisor is asked so a crash can never lose the fact that it was owed.
    ANALYSIS_PENDING = "ANALYSIS_PENDING"
    #: The analysis produced a directive that a human must approve before it is
    #: sent (any mode), or that the automatic policy refused to send.
    WAITING_HUMAN = "WAITING_HUMAN"
    #: A directive is allowed to be sent automatically (mode + LOW-risk
    #: allowlist) and has not been published yet.
    READY_TO_SEND = "READY_TO_SEND"
    #: The durable send intent is written; the artifact may or may not exist yet.
    #: This is the only state a restart must reconcile.
    SEND_PENDING = "SEND_PENDING"
    #: The directive was published; the assistant now waits for **new** evidence.
    #: Still blocking: the report this directive answered is not reviewable.
    SENT = "SENT"
    #: The report needs no directive - the gate allows the authoritative review.
    NO_ACTION = "NO_ACTION"
    #: The analysis itself failed (timeout, provider error, unusable answer).
    ERROR = "ERROR"
    #: A human must decide before anything else may happen.
    ESCALATED = "ESCALATED"
    #: The identity no longer matches the live project/step/attempt/baseline (or a
    #: newer report superseded it): it must never be sent, and it still blocks.
    STALE = "STALE"
    #: The report bytes do not satisfy the worker-report contract. Recorded on
    #: first sight - it blocks immediately and the supervisor is never called for
    #: bytes it can never describe. The persisted first-seen timestamps decide
    #: when the record escalates to the operator; nothing loops forever.
    MALFORMED = "MALFORMED"
    #: A human waived supervision for this **exact** report hash. Allows review.
    WAIVED = "WAIVED"
    #: A human rejected the directive for this **exact** report hash, explicitly.
    #: Allows review - but only with a recorded actor and reason (the gate
    #: re-checks that, so a REJECTED row can never appear without a human).
    REJECTED = "REJECTED"


#: Supervision statuses that **block** the authoritative review. The gate is a
#: whitelist, so this set is documentation of the policy as much as a check: a
#: status that is missing from :data:`SUPERVISION_ALLOWING_STATUSES` blocks even
#: if it is not listed here, and a missing row blocks too.
SUPERVISION_BLOCKING_STATUSES: frozenset = frozenset(
    {
        SupervisorStatus.ANALYSIS_PENDING,
        SupervisorStatus.WAITING_HUMAN,
        SupervisorStatus.READY_TO_SEND,
        SupervisorStatus.SEND_PENDING,
        SupervisorStatus.SENT,
        SupervisorStatus.ERROR,
        SupervisorStatus.ESCALATED,
        SupervisorStatus.STALE,
        SupervisorStatus.MALFORMED,
    }
)

#: Supervision statuses that **allow** the authoritative review to proceed.
SUPERVISION_ALLOWING_STATUSES: frozenset = frozenset(
    {
        SupervisorStatus.NO_ACTION,
        SupervisorStatus.WAIVED,
        SupervisorStatus.REJECTED,
    }
)

#: The two statuses a restart must reconcile: an analysis that was owed when the
#: process died, and a send intent whose publication may or may not have
#: succeeded. Both are resolved from persisted state plus the exchange channel.
SUPERVISION_RECONCILE_STATUSES: frozenset = frozenset(
    {
        SupervisorStatus.ANALYSIS_PENDING,
        SupervisorStatus.SEND_PENDING,
    }
)
