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
