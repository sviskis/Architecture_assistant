"""Capability ports of the plugin core.

The core defines *provider-neutral* contracts for the seven capabilities of the
assistant. Concrete adapters (Cline, OpenAI/Claude/Grok, judge, cost, reporting,
notifier) are plugged in later - nothing here knows about any of them.

Message shapes crossing these boundaries are minimal, immutable and
deterministic DTOs, so every adapter sees the same contract regardless of
provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, StrEnum
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

from ..domain.enums import ReportStatus, Severity
from ..domain.models import Decision, Finding, Task, utc_now

__all__ = [
    "PluginCapability",
    "WorkerRequest",
    "WorkerResult",
    "AdvisorQuery",
    "JudgeConflict",
    "CostRecord",
    "CostQuery",
    "CostSummary",
    "CostIdentityConflictError",
    "CostIdentityUnavailableError",
    "Notification",
    "WorkerPort",
    "WorkerChannelPort",
    "RealizationCheckPort",
    "RealizationCheckResult",
    "RealizationControlPort",
    "RealizationGate",
    "CanonicalBaseline",
    "AdvisorPort",
    "JudgePort",
    "CostPort",
    "ReportingPort",
    "NotifierPort",
]


class PluginCapability(StrEnum):
    """Stable identity of a plugin capability.

    The registry stores any number of named adapters per capability; which one is
    *active* is a composition/configuration decision, not a registry rule.
    """

    WORKER = "worker"
    ADVISOR = "advisor"
    JUDGE = "judge"
    STORAGE = "storage"
    COST = "cost"
    REPORTING = "reporting"
    NOTIFIER = "notifier"


# ---------------------------------------------------------------------------
# validation helpers (local, dependency-free)
# ---------------------------------------------------------------------------

def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string; got {value!r}")
    return value


def _optional_text(value: Any, field_name: str) -> Optional[str]:
    if value is None:
        return None
    return _text(value, field_name)


def _count(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an int; got {value!r}")
    if value < 0:
        raise ValueError(f"{field_name} must be >= 0; got {value!r}")
    return value


def _money(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number; got {value!r}")
    amount = float(value)
    if amount < 0:
        raise ValueError(f"{field_name} must be >= 0; got {value!r}")
    return amount


def _optional_positive_int(value: Any, field_name: str) -> Optional[int]:
    if value is None:
        return None
    number = _count(value, field_name)
    if number < 1:
        raise ValueError(f"{field_name} must be >= 1; got {value!r}")
    return number


def _text_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        raise ValueError(
            f"{field_name} must be a sequence of strings, not a bare string"
        )
    return tuple(value)


def _mapping(value: Any, field_name: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping; got {value!r}")
    return dict(value)


def _optional_mapping(value: Any, field_name: str) -> Optional[dict]:
    if value is None:
        return None
    return _mapping(value, field_name)


def _enum(enum_cls: type, value: Any, field_name: str) -> Enum:
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value)
        except ValueError:
            pass
    valid = ", ".join(member.value for member in enum_cls)
    raise ValueError(f"{field_name} must be one of ({valid}); got {value!r}")


def _datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime; got {value!r}")
    return value


def _optional_datetime(value: Any, field_name: str) -> Optional[datetime]:
    if value is None:
        return None
    return _datetime(value, field_name)


# ---------------------------------------------------------------------------
# provider-neutral message objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WorkerRequest:
    """Everything a worker adapter needs - provider-neutral.

    ``task`` is the domain task (it carries instructions and the report schema)
    and ``context`` is the serialized context snapshot.
    """

    task: Task
    context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.task, Task):
            raise ValueError(f"task must be a Task; got {self.task!r}")
        object.__setattr__(self, "context", _mapping(self.context, "context"))

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {"task": self.task.to_dict(), "context": dict(self.context)}


@dataclass(frozen=True)
class WorkerResult:
    """Provider-neutral worker outcome.

    A worker adapter (Cline in Step 7, or anything else) normalises its own
    report format into this shape; the core never sees a provider format.
    """

    status: ReportStatus
    summary: str = ""
    artifacts: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()
    architecture_questions: tuple[str, ...] = ()
    raw: Optional[Mapping[str, Any]] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "status", _enum(ReportStatus, self.status, "status")
        )
        object.__setattr__(self, "summary", self.summary or "")
        object.__setattr__(
            self, "artifacts", _text_tuple(self.artifacts, "artifacts")
        )
        object.__setattr__(self, "issues", _text_tuple(self.issues, "issues"))
        object.__setattr__(
            self,
            "architecture_questions",
            _text_tuple(self.architecture_questions, "architecture_questions"),
        )
        object.__setattr__(self, "raw", _optional_mapping(self.raw, "raw"))

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "status": self.status.value,
            "summary": self.summary,
            "artifacts": list(self.artifacts),
            "issues": list(self.issues),
            "architecture_questions": list(self.architecture_questions),
            "raw": None if self.raw is None else dict(self.raw),
        }


@dataclass(frozen=True)
class AdvisorQuery:
    """A question put to an advisor adapter."""

    question: str
    context: Mapping[str, Any] = field(default_factory=dict)
    step_no: Optional[int] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "question", _text(self.question, "question"))
        object.__setattr__(self, "context", _mapping(self.context, "context"))
        object.__setattr__(
            self, "step_no", _optional_positive_int(self.step_no, "step_no")
        )


@dataclass(frozen=True)
class JudgeConflict:
    """Unresolved evidence conflict handed to a judge adapter."""

    question: str
    findings: tuple[Finding, ...]
    context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "question", _text(self.question, "question"))
        findings = tuple(self.findings or ())
        if not findings:
            raise ValueError("findings must contain at least one Finding")
        for finding in findings:
            if not isinstance(finding, Finding):
                raise ValueError(
                    f"findings must contain Finding objects; got {finding!r}"
                )
        object.__setattr__(self, "findings", findings)
        object.__setattr__(self, "context", _mapping(self.context, "context"))



@dataclass(frozen=True)
class CostRecord:
    """One recorded cost event.

    ``event_id`` is the **stable identity of one logical billable provider
    event**, and it is what makes cost recording idempotent. Adapters build it
    from the provider-native response id (``f"{provider}:{response_id}"``), so
    replaying the same logical provider result is recognised as the same event
    instead of being counted twice, while two genuine calls remain two events.
    It is deliberately never derived from the clock and never from token counts
    or cost, because two legitimate calls may share all of those.

    ``pricing_known`` separates "this figure rests on a configured price" from
    "no price was available". A record with ``pricing_known=False`` carries the
    documented ``cost_usd = 0.0`` of an *unknown*, so a summary can never
    silently present it as a real zero-dollar cost.
    """

    provider: str
    event_id: str
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    pricing_known: bool = False
    project: Optional[str] = None
    step_no: Optional[int] = None
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _text(self.provider, "provider"))
        object.__setattr__(self, "event_id", _text(self.event_id, "event_id"))
        object.__setattr__(self, "model", self.model or "")
        object.__setattr__(
            self, "input_tokens", _count(self.input_tokens, "input_tokens")
        )
        object.__setattr__(
            self, "output_tokens", _count(self.output_tokens, "output_tokens")
        )
        object.__setattr__(
            self, "cost_usd", _money(self.cost_usd, "cost_usd")
        )
        if not isinstance(self.pricing_known, bool):
            raise ValueError(
                f"pricing_known must be a bool; got {self.pricing_known!r}"
            )
        object.__setattr__(
            self, "project", _optional_text(self.project, "project")
        )
        object.__setattr__(
            self, "step_no", _optional_positive_int(self.step_no, "step_no")
        )
        object.__setattr__(
            self, "created_at", _datetime(self.created_at, "created_at")
        )

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "provider": self.provider,
            "event_id": self.event_id,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "pricing_known": self.pricing_known,
            "project": self.project,
            "step_no": self.step_no,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True)
class CostQuery:
    """Filter for a cost aggregation.

    Every field is optional and ``None`` means "no filter", so a single contract
    covers every reporting need: total (``CostQuery()``), today
    (``CostQuery(from_time=start_of_day, to_time=end_of_day)``), by project, by
    step and by provider all use the same shape.
    """

    project: Optional[str] = None
    step_no: Optional[int] = None
    provider: Optional[str] = None
    from_time: Optional[datetime] = None
    to_time: Optional[datetime] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "project", _optional_text(self.project, "project")
        )
        object.__setattr__(
            self, "step_no", _optional_positive_int(self.step_no, "step_no")
        )
        object.__setattr__(
            self, "provider", _optional_text(self.provider, "provider")
        )
        from_time = _optional_datetime(self.from_time, "from_time")
        to_time = _optional_datetime(self.to_time, "to_time")
        if (
            from_time is not None
            and to_time is not None
            and from_time > to_time
        ):
            raise ValueError(
                f"from_time ({from_time!r}) must be <= to_time ({to_time!r})"
            )
        object.__setattr__(self, "from_time", from_time)
        object.__setattr__(self, "to_time", to_time)

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "project": self.project,
            "step_no": self.step_no,
            "provider": self.provider,
            "from_time": (
                None if self.from_time is None else self.from_time.isoformat()
            ),
            "to_time": (
                None if self.to_time is None else self.to_time.isoformat()
            ),
        }

    def matches(self, record: CostRecord) -> bool:
        """Whether ``record`` satisfies every non-``None`` filter.

        Fully deterministic and side-effect free: adapters can reuse this to
        implement their own aggregation consistently.
        """
        if self.project is not None and record.project != self.project:
            return False
        if self.step_no is not None and record.step_no != self.step_no:
            return False
        if self.provider is not None and record.provider != self.provider:
            return False
        if self.from_time is not None and record.created_at < self.from_time:
            return False
        if self.to_time is not None and record.created_at > self.to_time:
            return False
        return True


@dataclass(frozen=True)
class CostSummary:
    """Aggregated result of a :class:`CostQuery`.

    ``total_usd`` is the sum of every matched record's ``cost_usd`` - a record
    with ``pricing_known=False`` contributes its documented ``0.0`` - while
    ``priced_record_count`` and ``unpriced_record_count`` partition
    ``record_count``. A reader therefore always sees how much of the figure
    rests on a configured price and how much of it is simply unknown; an
    unpriced record is never silently presented as a known zero-dollar cost.
    """

    total_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    record_count: int = 0
    priced_record_count: int = 0
    unpriced_record_count: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "total_usd", _money(self.total_usd, "total_usd")
        )
        object.__setattr__(
            self, "input_tokens", _count(self.input_tokens, "input_tokens")
        )
        object.__setattr__(
            self, "output_tokens", _count(self.output_tokens, "output_tokens")
        )
        object.__setattr__(
            self, "record_count", _count(self.record_count, "record_count")
        )
        object.__setattr__(
            self,
            "priced_record_count",
            _count(self.priced_record_count, "priced_record_count"),
        )
        object.__setattr__(
            self,
            "unpriced_record_count",
            _count(self.unpriced_record_count, "unpriced_record_count"),
        )
        if (
            self.priced_record_count + self.unpriced_record_count
            != self.record_count
        ):
            raise ValueError(
                "priced_record_count + unpriced_record_count must equal "
                f"record_count; got {self.priced_record_count} + "
                f"{self.unpriced_record_count} != {self.record_count}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "total_usd": self.total_usd,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "record_count": self.record_count,
            "priced_record_count": self.priced_record_count,
            "unpriced_record_count": self.unpriced_record_count,
        }


class CostIdentityConflictError(Exception):
    """One ``event_id`` was re-used for *different* accounting content.

    Raised by a cost adapter when :meth:`CostPort.record` receives an
    ``event_id`` that is already stored but whose accounting content differs.
    The ledger fails closed: overwriting would silently lose one version and
    storing both would charge one logical event twice.
    """


class CostIdentityUnavailableError(Exception):
    """A billable response carried no stable provider event identity.

    Raised *inside* an adapter's telemetry path when a successful 2xx response
    carries usable usage but no provider-native response id. Nothing is
    persisted, because a fabricated or request-derived identity would either
    collapse two genuine calls into one event or invent a billable event that
    never happened.
    """


@dataclass(frozen=True)
class Notification:
    """A message handed to a notifier adapter."""

    level: Severity
    title: str
    body: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "level", _enum(Severity, self.level, "level"))
        object.__setattr__(self, "title", _text(self.title, "title"))
        object.__setattr__(self, "body", self.body or "")

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "level": self.level.value,
            "title": self.title,
            "body": self.body,
        }



# ---------------------------------------------------------------------------
# capability ports (core contracts - no provider knowledge)
# ---------------------------------------------------------------------------

@runtime_checkable
class WorkerPort(Protocol):
    """Executes a build task and returns a provider-neutral result."""

    def run(self, request: WorkerRequest) -> WorkerResult: ...


@runtime_checkable
class WorkerChannelPort(Protocol):
    """Provider-neutral *asynchronous* worker channel.

    For workers that complete out of process (a file channel, a queue, an
    external process) the synchronous :class:`WorkerPort` contract is not
    enough: the assistant must be able to dispatch, then read the report later,
    and only consume it once the result has been durably accepted. The three
    methods below are that lifecycle - deliberately free of any provider term
    (no directories, no file names, no process ids):

    * ``dispatch`` - hand the request to the worker;
    * ``read_report`` - read the result if it is available, otherwise ``None``.
      Strictly read-only and repeatable: calling it again yields the same result
      until the report is acknowledged (at-least-once delivery);
    * ``acknowledge_report`` - consume the report. Called **only** after the
      caller has durably persisted the outcome, so a crash in between can never
      lose a result that was never committed.

    :class:`WorkerPort` remains the synchronous convenience contract; an adapter
    may implement either or both.
    """

    def dispatch(self, request: WorkerRequest) -> Any: ...

    def read_report(
        self, step_no: int, attempt: int
    ) -> Optional[WorkerResult]: ...

    def acknowledge_report(self, step_no: int, attempt: int) -> Any: ...


@runtime_checkable
class RealizationCheckPort(Protocol):
    """Runs the *deterministic* architecture check over the realized source.

    The check is provider-neutral on purpose: it does not know whether it is
    inspecting this assistant or a later target project, and it never reads the
    source of truth. Implementations scan the injected source root, evaluate the
    authoritative baseline rules and translate the outcome into domain evidence
    (:class:`~architecture_assistant.domain.models.Finding` /
    :class:`~architecture_assistant.domain.models.Decision`).
    """

    def check(self, step_no: int, attempt: int) -> "RealizationCheckResult": ...


@dataclass(frozen=True)
class RealizationCheckResult:
    """Provider-neutral outcome of one deterministic realization check.

    ``compliant`` is derived from the deterministic rules only - never from an
    advisory opinion - so it can not be out-voted later.
    """

    compliant: bool
    baseline_version: str
    decision: Decision
    findings: tuple[Finding, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.compliant, bool):
            raise ValueError(f"compliant must be a bool; got {self.compliant!r}")
        object.__setattr__(
            self,
            "baseline_version",
            _text(self.baseline_version, "baseline_version"),
        )
        if not isinstance(self.decision, Decision):
            raise ValueError(
                f"decision must be a Decision; got {self.decision!r}"
            )
        findings = tuple(self.findings or ())
        for finding in findings:
            if not isinstance(finding, Finding):
                raise ValueError(
                    f"findings must contain Finding objects; got {finding!r}"
                )
        object.__setattr__(self, "findings", findings)
        if self.compliant and findings:
            raise ValueError("a compliant result can not carry findings")
        if not self.compliant and not findings:
            raise ValueError(
                "a non-compliant result must carry at least one finding"
            )

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "compliant": self.compliant,
            "baseline_version": self.baseline_version,
            "decision": self.decision.to_dict(),
            "findings": [finding.to_dict() for finding in self.findings],
        }


@dataclass(frozen=True)
class CanonicalBaseline:
    """The code-side canonical baseline expectation.

    Composition builds it from the ``architecture`` layer and injects it, so the
    application layer can compare the *persisted* authoritative baseline against
    the canonical one without ever importing that layer.
    """

    version: str
    rule_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "version", _text(self.version, "version"))
        rule_ids = _text_tuple(self.rule_ids, "rule_ids")
        if not rule_ids:
            raise ValueError("rule_ids must declare at least one rule id")
        for rule_id in rule_ids:
            if not isinstance(rule_id, str) or not rule_id.strip():
                raise ValueError(
                    f"rule_ids must be non-empty strings; got {rule_id!r}"
                )
        object.__setattr__(self, "rule_ids", rule_ids)

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {"version": self.version, "rule_ids": list(self.rule_ids)}


@dataclass(frozen=True)
class RealizationGate:
    """Fail-closed verdict of the loop's realization gate.

    ``step_no`` **and** ``attempt`` are part of the verdict identity: one step
    may violate the baseline on attempt 1 and satisfy it on attempt 2, and both
    verdicts must stay distinguishable (and preserved) in the evidence trail.
    """

    compliant: bool
    step_no: int
    attempt: int
    baseline_version: str
    violation_count: int = 0
    finding_ids: tuple[str, ...] = ()
    decision_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.compliant, bool):
            raise ValueError(f"compliant must be a bool; got {self.compliant!r}")
        object.__setattr__(
            self, "step_no", _optional_positive_int(self.step_no, "step_no")
        )
        object.__setattr__(
            self, "attempt", _optional_positive_int(self.attempt, "attempt")
        )
        object.__setattr__(
            self,
            "baseline_version",
            _text(self.baseline_version, "baseline_version"),
        )
        object.__setattr__(
            self, "decision_id", _text(self.decision_id, "decision_id")
        )
        finding_ids = _text_tuple(self.finding_ids, "finding_ids")
        object.__setattr__(self, "finding_ids", finding_ids)
        object.__setattr__(
            self,
            "violation_count",
            _count(self.violation_count, "violation_count"),
        )
        if self.violation_count != len(finding_ids):
            raise ValueError(
                "violation_count must equal the number of finding ids; "
                f"got {self.violation_count} and {len(finding_ids)}"
            )
        if self.compliant and self.violation_count:
            raise ValueError("a compliant gate can not report violations")

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "compliant": self.compliant,
            "step_no": self.step_no,
            "attempt": self.attempt,
            "baseline_version": self.baseline_version,
            "violation_count": self.violation_count,
            "finding_ids": list(self.finding_ids),
            "decision_id": self.decision_id,
        }


@runtime_checkable
class RealizationControlPort(Protocol):
    """The loop's realization gate - fresh, deterministic and *fail-closed*.

    The orchestrator depends on this contract, never on a concrete validator, so
    it behaves identically against any target project. Implementations must

    * re-validate **freshly** on every call - a previously stored ``ACCEPTED``
      decision may never stand in for a new run;
    * raise instead of returning a verdict when no verdict can be reached
      (a broken control plane must never let ``VERIFY`` through).
    """

    def gate(self, step_no: int, attempt: int) -> RealizationGate: ...


@runtime_checkable
class AdvisorPort(Protocol):
    """Produces an evidence-based perspective on a question."""

    def advise(self, query: AdvisorQuery) -> Finding: ...


@runtime_checkable
class JudgePort(Protocol):
    """Resolves a genuine, unresolved evidence conflict."""

    def judge(self, conflict: JudgeConflict) -> Decision: ...


@runtime_checkable
class CostPort(Protocol):
    """Records cost entries and aggregates them through one query contract."""

    def record(self, cost: CostRecord) -> None: ...

    def query(self, cost_filter: CostQuery) -> CostSummary: ...


@runtime_checkable
class ReportingPort(Protocol):
    """Renders a report payload (markdown, export, ...)."""

    def render(self, payload: Mapping[str, Any]) -> str: ...


@runtime_checkable
class NotifierPort(Protocol):
    """Delivers a notification to the operator."""

    def notify(self, message: Notification) -> None: ...

