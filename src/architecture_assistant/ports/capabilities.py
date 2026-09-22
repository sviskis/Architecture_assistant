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

from ..domain.enums import (
    ReportStatus,
    Severity,
    SupervisorAction,
    SupervisorRisk,
)
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
    "SupervisionChannelPort",
    "RealizationCheckPort",
    "RealizationCheckResult",
    "RealizationControlPort",
    "RealizationGate",
    "CanonicalBaseline",
    "AdvisorPort",
    "ConnectionStatus",
    "JudgePort",
    "CostPort",
    "ReportingPort",
    "NotifierPort",
    "SynthesisQuery",
    "SynthesisResult",
    "SynthesisPort",
    "MAX_DELIBERATION_ITEMS",
    "MAX_DELIBERATION_TEXT",
    "SLOT_AGENT_A",
    "SLOT_AGENT_B",
    "SLOT_LEAD",
    "AgentAnalysisQuery",
    "AgentReconsiderQuery",
    "AgentAnalysisResult",
    "AgentReconsiderResult",
    "DeliberationAgentPort",
    "LeadReviewQuery",
    "LeadReviewResult",
    "LeadSynthesisQuery",
    "FinalSynthesisResult",
    "DeliberationLeadPort",
    "MAX_SUPERVISOR_ITEMS",
    "MAX_SUPERVISOR_TEXT",
    "SupervisorContext",
    "SupervisorResult",
    "SupervisorPort",
    "SupervisionVerdict",
    "SupervisionGatePort",
]

#: How many records one supervisor-context section may carry, and how long one
#: supervisor-facing text may be. The context is a *bounded* fact sheet: a
#: supervisor is never handed an unbounded log, a chat history or a file dump.
MAX_SUPERVISOR_ITEMS = 32
MAX_SUPERVISOR_TEXT = 4000


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
class SupervisionChannelPort(Protocol):
    """The extra worker-channel seam **supervision** needs - and nothing else.

    Deliberately a *separate* protocol rather than more methods on
    :class:`WorkerChannelPort`: the loop only ever needs ``dispatch``,
    ``read_report`` and ``acknowledge_report``, and widening that contract would
    silently invalidate every existing channel implementation for a capability
    the loop must not have.

    What supervision adds is exactly two ideas:

    * the **exact bytes** of the current report, because the supervision identity
      is a SHA-256 over them - unparsed, unsanitized and not normalized;
    * one **distinct directive artifact** in the same channel, published
      at-least-once and identifiable by its own content, so a restart can tell
      whether its publication happened without guessing.

    An implementation must never return a temporary in-flight file as a report,
    and must never publish a directive through a second exchange root: a directive
    is another artifact of the one channel the worker already talks through.
    """

    def read_report_bytes(self, step_no: int, attempt: int) -> Optional[bytes]:
        """The exact current report bytes, or ``None`` when none exists yet."""
        ...

    def publish_directive(
        self, directive: Mapping[str, Any], *, step_no: int, attempt: int
    ) -> Any:
        """Publish one deterministic, identifiable directive artifact."""
        ...

    def read_directive(
        self, step_no: int, attempt: int
    ) -> Optional[Mapping[str, Any]]:
        """Read the published directive artifact for one dispatch, or ``None``."""
        ...


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


class ConnectionStatus(StrEnum):
    """The verdict of one *smallest safe* provider call (the Test Connection button).

    The vocabulary is deliberately tiny and provider-neutral: an operator needs
    to know whether a saved provider configuration *works*, and - when it does
    not - which layer refused. A verdict never carries a status code, a header,
    a response body or a credential: it is a single word.

    ``DISABLED`` exists because "this advisor runs no provider" is a legitimate,
    intentional configuration; it is what a disabled advisor answers, and it is
    produced **without any network call at all**.
    """

    CONNECTED = "CONNECTED"
    AUTH_ERROR = "AUTH ERROR"
    PROVIDER_ERROR = "PROVIDER ERROR"
    NETWORK_ERROR = "NETWORK ERROR"
    MODEL_ERROR = "MODEL ERROR"
    DISABLED = "DISABLED"


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


# ---------------------------------------------------------------------------
# the optional synthesis seam (Step 27)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SynthesisQuery:
    """One provider-neutral request for a **managed-project** design.

    The query carries only already-gathered, already-validated inputs: the
    operator's requirement verbatim, the bounded evidence digest of one review
    and the deterministic skeleton the application assembled. A synthesizer may
    refine the skeleton - it may never rewrite the requirement, and it is handed
    no storage port, repository, transaction or provider client.
    """

    project: str
    requirement: str
    review_id: str
    architecture_version: Optional[str] = None
    skeleton: Mapping[str, Any] = field(default_factory=dict)
    digest: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "project", _text(self.project, "project"))
        object.__setattr__(
            self, "requirement", _text(self.requirement, "requirement")
        )
        object.__setattr__(
            self, "review_id", _text(self.review_id, "review_id")
        )
        object.__setattr__(
            self,
            "architecture_version",
            _optional_text(self.architecture_version, "architecture_version"),
        )
        object.__setattr__(self, "skeleton", _mapping(self.skeleton, "skeleton"))
        object.__setattr__(self, "digest", _mapping(self.digest, "digest"))

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "project": self.project,
            "requirement": self.requirement,
            "review_id": self.review_id,
            "architecture_version": self.architecture_version,
            "skeleton": dict(self.skeleton),
            "digest": dict(self.digest),
        }


@dataclass(frozen=True)
class SynthesisResult:
    """What a synthesizer answers: proposal sections plus its own provenance.

    ``content`` is validated by the caller against the proposal contract before
    anything is stored - a malformed answer is a failure, never a proposal.
    """

    source: str
    content: Mapping[str, Any] = field(default_factory=dict)
    cost: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _text(self.source, "source"))
        object.__setattr__(self, "content", _mapping(self.content, "content"))
        object.__setattr__(self, "cost", _mapping(self.cost, "cost"))

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "source": self.source,
            "content": dict(self.content),
            "cost": dict(self.cost),
        }


@runtime_checkable
class SynthesisPort(Protocol):
    """Turns gathered evidence into managed-project design content.

    Provider-neutral and **optional**: the deterministic organizer is the default
    and is always available, so the assistant can produce and approve a proposal
    with no provider configured at all. An implementation must

    * echo ``query.requirement`` verbatim and never rewrite it;
    * return only JSON-safe content, because a malformed answer is refused;
    * never mutate the managed project's state - it receives no storage port.
    """

    def synthesize(self, query: SynthesisQuery) -> SynthesisResult: ...


# ---------------------------------------------------------------------------
# supervision (Step 28 Phase 1) - advisory analysis of one worker report
# ---------------------------------------------------------------------------

def _bounded_text_list(value: Any, field_name: str) -> tuple[str, ...]:
    """A bounded sequence of non-empty strings - the supervisor's one shape."""
    items = _text_tuple(value, field_name)
    if len(items) > MAX_SUPERVISOR_ITEMS:
        raise ValueError(
            f"{field_name} must carry at most {MAX_SUPERVISOR_ITEMS} entries; "
            f"got {len(items)}"
        )
    for item in items:
        if len(item) > MAX_SUPERVISOR_TEXT:
            raise ValueError(
                f"{field_name} entries must be at most {MAX_SUPERVISOR_TEXT} "
                f"characters; got {len(item)}"
            )
    return items


def _bounded_records(
    value: Any, field_name: str
) -> tuple[Mapping[str, Any], ...]:
    """A bounded sequence of JSON-safe structured records."""
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        raise ValueError(
            f"{field_name} must be a sequence of mappings, not a bare string"
        )
    records = tuple(value)
    if len(records) > MAX_SUPERVISOR_ITEMS:
        raise ValueError(
            f"{field_name} must carry at most {MAX_SUPERVISOR_ITEMS} records; "
            f"got {len(records)}"
        )
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError(
                f"{field_name} entries must be mappings; got {record!r}"
            )
    return records


@dataclass(frozen=True)
class SupervisorContext:
    """The **persisted facts** one supervision analysis may see - and nothing else.

    This is the fact half of the contract: everything here is already durable,
    already bounded and already sanitized by the assistant. The supervisor's
    answer is the *inference* half (:class:`SupervisorResult`), and it is never
    allowed to masquerade as fact.

    Deliberately **absent**: raw chat history of any kind, unbounded logs,
    secrets, API keys and Authorization headers. A supervisor sees the current
    report, the task it answered, the workflow state around it and the bounded
    architecture context - never a transcript, never a credential and never the
    whole repository.
    """

    project: str
    step_no: int
    attempt: int
    max_attempts: int
    step_state: str
    mode: str
    paused: bool
    architecture_version: str
    source_report_hash: str
    task_title: str = ""
    task_description: str = ""
    task_instructions: Mapping[str, Any] = field(default_factory=dict)
    report_status: str = ""
    report_summary: str = ""
    report: Mapping[str, Any] = field(default_factory=dict)
    report_files_created: tuple[str, ...] = ()
    report_files_changed: tuple[str, ...] = ()
    report_files_deleted: tuple[str, ...] = ()
    report_tests: Mapping[str, Any] = field(default_factory=dict)
    worker_issues: tuple[str, ...] = ()
    worker_architecture_questions: tuple[str, ...] = ()
    worker_dependencies_added: tuple[str, ...] = ()
    deterministic_findings: tuple[Mapping[str, Any], ...] = ()
    accepted_adrs: tuple[Mapping[str, Any], ...] = ()
    open_risks: tuple[Mapping[str, Any], ...] = ()
    open_change_requests: tuple[Mapping[str, Any], ...] = ()
    operator_constraints: tuple[str, ...] = ()
    attempts_remaining: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "project", _text(self.project, "project"))
        object.__setattr__(
            self, "step_no", _optional_positive_int(self.step_no, "step_no")
        )
        object.__setattr__(
            self, "attempt", _optional_positive_int(self.attempt, "attempt")
        )
        object.__setattr__(
            self,
            "max_attempts",
            _optional_positive_int(self.max_attempts, "max_attempts"),
        )
        object.__setattr__(
            self, "step_state", _text(self.step_state, "step_state")
        )
        object.__setattr__(self, "mode", _text(self.mode, "mode"))
        if not isinstance(self.paused, bool):
            raise ValueError(f"paused must be a bool; got {self.paused!r}")
        object.__setattr__(
            self,
            "architecture_version",
            _text(self.architecture_version, "architecture_version"),
        )
        object.__setattr__(
            self,
            "source_report_hash",
            _text(self.source_report_hash, "source_report_hash"),
        )
        object.__setattr__(self, "task_title", self.task_title or "")
        object.__setattr__(
            self, "task_description", self.task_description or ""
        )
        object.__setattr__(
            self,
            "task_instructions",
            _mapping(self.task_instructions, "task_instructions"),
        )
        object.__setattr__(self, "report_status", self.report_status or "")
        object.__setattr__(self, "report_summary", self.report_summary or "")
        object.__setattr__(self, "report", _mapping(self.report, "report"))
        object.__setattr__(
            self,
            "report_files_created",
            _bounded_text_list(
                self.report_files_created, "report_files_created"
            ),
        )
        object.__setattr__(
            self,
            "report_files_changed",
            _bounded_text_list(
                self.report_files_changed, "report_files_changed"
            ),
        )
        object.__setattr__(
            self,
            "report_files_deleted",
            _bounded_text_list(
                self.report_files_deleted, "report_files_deleted"
            ),
        )
        object.__setattr__(
            self, "report_tests", _mapping(self.report_tests, "report_tests")
        )
        object.__setattr__(
            self,
            "worker_issues",
            _bounded_text_list(self.worker_issues, "worker_issues"),
        )
        object.__setattr__(
            self,
            "worker_architecture_questions",
            _bounded_text_list(
                self.worker_architecture_questions,
                "worker_architecture_questions",
            ),
        )
        object.__setattr__(
            self,
            "worker_dependencies_added",
            _bounded_text_list(
                self.worker_dependencies_added, "worker_dependencies_added"
            ),
        )
        for name in (
            "deterministic_findings",
            "accepted_adrs",
            "open_risks",
            "open_change_requests",
        ):
            object.__setattr__(
                self, name, _bounded_records(getattr(self, name), name)
            )
        object.__setattr__(
            self,
            "operator_constraints",
            _bounded_text_list(
                self.operator_constraints, "operator_constraints"
            ),
        )
        object.__setattr__(
            self,
            "attempts_remaining",
            _count(self.attempts_remaining, "attempts_remaining"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "project": self.project,
            "step_no": self.step_no,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "attempts_remaining": self.attempts_remaining,
            "step_state": self.step_state,
            "mode": self.mode,
            "paused": self.paused,
            "architecture_version": self.architecture_version,
            "source_report_hash": self.source_report_hash,
            "task_title": self.task_title,
            "task_description": self.task_description,
            "task_instructions": dict(self.task_instructions),
            "report_status": self.report_status,
            "report_summary": self.report_summary,
            "report": dict(self.report),
            "report_files_created": list(self.report_files_created),
            "report_files_changed": list(self.report_files_changed),
            "report_files_deleted": list(self.report_files_deleted),
            "report_tests": dict(self.report_tests),
            "worker_issues": list(self.worker_issues),
            "worker_architecture_questions": list(
                self.worker_architecture_questions
            ),
            "worker_dependencies_added": list(self.worker_dependencies_added),
            "deterministic_findings": [
                dict(item) for item in self.deterministic_findings
            ],
            "accepted_adrs": [dict(item) for item in self.accepted_adrs],
            "open_risks": [dict(item) for item in self.open_risks],
            "open_change_requests": [
                dict(item) for item in self.open_change_requests
            ],
            "operator_constraints": list(self.operator_constraints),
        }


@dataclass(frozen=True)
class SupervisorResult:
    """The **inference** half: what a supervisor recommends, never what happens.

    ``action``, ``reason``, ``risk``, ``instruction_for_cline`` and
    ``requires_human`` are advisory. The application validates them, derives the
    send class itself (never trusting ``risk`` alone) and owns every mutation - a
    supervisor receives no storage port, no transaction boundary, no worker
    channel and no FSM, so it *cannot* set ``VERIFIED``, move a Step, increment an
    attempt or ACK a report even if it wanted to.

    ``evidence`` is bounded, sanitized provenance (file references, test ids),
    never a provider transcript.
    """

    action: SupervisorAction
    reason: str
    risk: SupervisorRisk = SupervisorRisk.HIGH
    instruction_for_cline: str = ""
    requires_human: bool = False
    evidence: tuple[str, ...] = ()
    provider: str = "supervisor"
    cost: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "action", _enum(SupervisorAction, self.action, "action")
        )
        object.__setattr__(self, "reason", _text(self.reason, "reason"))
        object.__setattr__(
            self, "risk", _enum(SupervisorRisk, self.risk, "risk")
        )
        object.__setattr__(
            self, "instruction_for_cline", self.instruction_for_cline or ""
        )
        if not isinstance(self.requires_human, bool):
            raise ValueError(
                f"requires_human must be a bool; got {self.requires_human!r}"
            )
        if len(self.reason) > MAX_SUPERVISOR_TEXT:
            raise ValueError(
                f"reason must be at most {MAX_SUPERVISOR_TEXT} characters; "
                f"got {len(self.reason)}"
            )
        if len(self.instruction_for_cline) > MAX_SUPERVISOR_TEXT:
            raise ValueError(
                "instruction_for_cline must be at most "
                f"{MAX_SUPERVISOR_TEXT} characters; "
                f"got {len(self.instruction_for_cline)}"
            )
        object.__setattr__(
            self, "evidence", _bounded_text_list(self.evidence, "evidence")
        )
        object.__setattr__(
            self, "provider", _text(self.provider, "provider")
        )
        object.__setattr__(self, "cost", _mapping(self.cost, "cost"))

    @property
    def cost_available(self) -> bool:
        """Whether the answer carried cost telemetry at all."""
        return bool(self.cost)

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "action": self.action.value,
            "reason": self.reason,
            "risk": self.risk.value,
            "instruction_for_cline": self.instruction_for_cline,
            "requires_human": self.requires_human,
            "evidence": list(self.evidence),
            "provider": self.provider,
            "cost": dict(self.cost),
        }


@runtime_checkable
class SupervisorPort(Protocol):
    """Analyses one worker report and recommends a directive.

    Provider-neutral and **advisory**: the assistant remains authoritative, so an
    implementation must

    * be handed only a :class:`SupervisorContext` - persisted, bounded facts, no
      chat history, no log dump, no secret;
    * answer with a :class:`SupervisorResult` and nothing else;
    * assume it may be asked again for the same context after a crash, so an
      answer must not depend on being called exactly once;
    * never mutate anything: it receives no storage, no transaction boundary, no
      FSM and no worker channel.
    """

    def supervise(self, context: SupervisorContext) -> SupervisorResult: ...


@dataclass(frozen=True)
class SupervisionVerdict:
    """Fail-closed answer to one question: may this report be reviewed?

    ``allowed`` is ``False`` for every status except the three allowing ones -
    **and for a missing record** - so the gate never has to guess. ``status`` and
    ``supervision_id`` are carried for traceability only: the orchestrator never
    branches on them, it only obeys ``allowed``.
    """

    allowed: bool
    status: Optional[str]
    supervision_id: Optional[str]
    reason: str
    source_report_hash: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.allowed, bool):
            raise ValueError(f"allowed must be a bool; got {self.allowed!r}")
        object.__setattr__(
            self, "status", _optional_text(self.status, "status")
        )
        object.__setattr__(
            self,
            "supervision_id",
            _optional_text(self.supervision_id, "supervision_id"),
        )
        object.__setattr__(self, "reason", _text(self.reason, "reason"))
        object.__setattr__(
            self,
            "source_report_hash",
            _optional_text(self.source_report_hash, "source_report_hash"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "allowed": self.allowed,
            "status": self.status,
            "supervision_id": self.supervision_id,
            "reason": self.reason,
            "source_report_hash": self.source_report_hash,
        }


@runtime_checkable
class SupervisionGatePort(Protocol):
    """The narrow, read-only seam the authoritative loop consults.

    Two methods, no writes: ``resolve`` answers whether the **current** report of
    one step/attempt may proceed to authoritative review, and ``enabled`` reports
    whether supervision is active at all. The orchestrator holds this instead of
    the supervision use-case, so the loop can never send a directive, approve one
    or read a supervisor's reasoning - it can only be blocked or allowed.
    """

    def enabled(self) -> bool: ...

    def resolve(self, step_no: int, attempt: int) -> SupervisionVerdict: ...


# ---------------------------------------------------------------------------
# the deliberation capability (Step 29)
#
# A controlled architecture review board: two *independent* architects answer the
# operator's requirement, a chair reviews them, each architect gets exactly one
# bounded reconsideration, and the chair produces a final synthesis. These
# contracts are deliberately **separate** from ``AdvisorPort`` (a Finding cannot
# represent a whole architecture proposal), from ``SynthesisPort`` (which returns
# managed-project proposal content and nothing about agreements or conflicts),
# from ``JudgePort`` (a judge resolves one evidence conflict, never a design) and
# from ``SupervisorPort`` (the implementation supervisor is a different system
# entirely - the Lead is never Codex).
#
# Two round contracts, not one, are the structural guarantee of independence: a
# Round-1 request has *no field* that could carry the peer's proposal, and only a
# Round-2 request can carry the Lead's critique and the peer's structured claims.
# ---------------------------------------------------------------------------

#: How many entries one deliberation section may carry, and how long one
#: deliberation-facing text may be. A deliberation is a *bounded* consultation: an
#: architect is never handed an unbounded transcript, a file dump or a chat log.
MAX_DELIBERATION_ITEMS = 64
MAX_DELIBERATION_TEXT = 4000

#: The three seats of a deliberation. Slots are *positions*, never providers.
SLOT_AGENT_A = "agent_a"
SLOT_AGENT_B = "agent_b"
SLOT_LEAD = "lead"


def _bounded_deliberation(value: Any, field_name: str) -> dict:
    """A JSON-safe mapping, bounded so no provider can flood a stage result."""
    payload = _mapping(value, field_name)
    if len(payload) > MAX_DELIBERATION_ITEMS:
        raise ValueError(
            f"{field_name} must carry at most {MAX_DELIBERATION_ITEMS} keys; "
            f"got {len(payload)}"
        )
    return payload


@dataclass(frozen=True)
class AgentAnalysisQuery:
    """Round 1: the **independent** analysis request.

    An agent receives the operator's requirement verbatim and the same bounded
    project context every other seat receives - and nothing else. There is no
    field here that could carry a peer's proposal, which is what makes Round-1
    independence structural rather than a convention.
    """

    project: str
    requirement: str
    deliberation_id: str
    slot: str
    context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("project", "requirement", "deliberation_id", "slot"):
            object.__setattr__(
                self, name, _text(getattr(self, name), name)
            )
        object.__setattr__(
            self, "context", _bounded_deliberation(self.context, "context")
        )

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "project": self.project,
            "requirement": self.requirement,
            "deliberation_id": self.deliberation_id,
            "slot": self.slot,
            "context": dict(self.context),
        }


@dataclass(frozen=True)
class AgentReconsiderQuery:
    """Round 2: one bounded reconsideration of an agent's **own** Round 1.

    It carries the agent's own Round-1 result, the Lead's structured critique for
    that seat and the peer's **structured claims** - never a transcript, never a
    hidden reasoning trace and never the peer's raw provider answer.
    """

    project: str
    requirement: str
    deliberation_id: str
    slot: str
    own_round1: Mapping[str, Any] = field(default_factory=dict)
    lead_critique: Mapping[str, Any] = field(default_factory=dict)
    peer_claims: Mapping[str, Any] = field(default_factory=dict)
    context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("project", "requirement", "deliberation_id", "slot"):
            object.__setattr__(
                self, name, _text(getattr(self, name), name)
            )
        for name in ("own_round1", "lead_critique", "peer_claims", "context"):
            object.__setattr__(
                self, name, _bounded_deliberation(getattr(self, name), name)
            )

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "project": self.project,
            "requirement": self.requirement,
            "deliberation_id": self.deliberation_id,
            "slot": self.slot,
            "own_round1": dict(self.own_round1),
            "lead_critique": dict(self.lead_critique),
            "peer_claims": dict(self.peer_claims),
            "context": dict(self.context),
        }


@dataclass(frozen=True)
class AgentAnalysisResult:
    """What an architect answers in Round 1: structured content plus provenance.

    ``content`` is validated by the caller against the round-result contract
    before anything is stored - a malformed answer is a failure, never a design.
    ``source`` is the provider id that answered, ``cost`` the adapter's own cost
    telemetry (empty when the provider reported none - never a fabricated zero).
    """

    source: str
    content: Mapping[str, Any] = field(default_factory=dict)
    cost: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _text(self.source, "source"))
        object.__setattr__(
            self, "content", _bounded_deliberation(self.content, "content")
        )
        object.__setattr__(self, "cost", _mapping(self.cost, "cost"))

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "source": self.source,
            "content": dict(self.content),
            "cost": dict(self.cost),
        }


@dataclass(frozen=True)
class AgentReconsiderResult:
    """What an architect answers in Round 2: KEEP / REVISE / WITHDRAW plus deltas."""

    source: str
    content: Mapping[str, Any] = field(default_factory=dict)
    cost: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _text(self.source, "source"))
        object.__setattr__(
            self, "content", _bounded_deliberation(self.content, "content")
        )
        object.__setattr__(self, "cost", _mapping(self.cost, "cost"))

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "source": self.source,
            "content": dict(self.content),
            "cost": dict(self.cost),
        }


@runtime_checkable
class DeliberationAgentPort(Protocol):
    """One independent architect of the review board.

    An implementation must

    * be handed only an :class:`AgentAnalysisQuery` (Round 1) or an
      :class:`AgentReconsiderQuery` (Round 2) - never a peer's raw answer;
    * answer with structured conclusions only, never hidden chain-of-thought;
    * never mutate anything: it receives no storage, no transaction boundary and
      no workflow authority.
    """

    def analyse(self, query: AgentAnalysisQuery) -> AgentAnalysisResult: ...

    def reconsider(self, query: AgentReconsiderQuery) -> AgentReconsiderResult: ...


@dataclass(frozen=True)
class LeadReviewQuery:
    """What the chair receives before it reviews: both Round-1 results, and nothing else.

    Deliberately absent: any hidden chain-of-thought, any provider system prompt,
    any credential, any chat history and any unrelated log. The chair reasons over
    two *structured conclusions*.
    """

    project: str
    requirement: str
    deliberation_id: str
    agent_a: Mapping[str, Any] = field(default_factory=dict)
    agent_b: Mapping[str, Any] = field(default_factory=dict)
    context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("project", "requirement", "deliberation_id"):
            object.__setattr__(
                self, name, _text(getattr(self, name), name)
            )
        for name in ("agent_a", "agent_b", "context"):
            object.__setattr__(
                self, name, _bounded_deliberation(getattr(self, name), name)
            )

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "project": self.project,
            "requirement": self.requirement,
            "deliberation_id": self.deliberation_id,
            "agent_a": dict(self.agent_a),
            "agent_b": dict(self.agent_b),
            "context": dict(self.context),
        }


@dataclass(frozen=True)
class LeadReviewResult:
    """The chair's **review packet source** - agreements, conflicts and questions.

    This is explicitly *not* a final architecture: it is the structured criticism
    one bounded reconsideration round is built from.
    """

    source: str
    content: Mapping[str, Any] = field(default_factory=dict)
    cost: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _text(self.source, "source"))
        object.__setattr__(
            self, "content", _bounded_deliberation(self.content, "content")
        )
        object.__setattr__(self, "cost", _mapping(self.cost, "cost"))

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "source": self.source,
            "content": dict(self.content),
            "cost": dict(self.cost),
        }


@dataclass(frozen=True)
class LeadSynthesisQuery:
    """Everything the chair receives for the **final** synthesis, and nothing else.

    Five exact upstream results travel with the content: both Round-1 results, the
    LeadReview and both Round-2 answers. The chair sees no storage port, no
    transaction boundary, no workflow authority and no credential.
    """

    project: str
    requirement: str
    deliberation_id: str
    agent_a_round1: Mapping[str, Any] = field(default_factory=dict)
    agent_b_round1: Mapping[str, Any] = field(default_factory=dict)
    lead_review: Mapping[str, Any] = field(default_factory=dict)
    agent_a_round2: Mapping[str, Any] = field(default_factory=dict)
    agent_b_round2: Mapping[str, Any] = field(default_factory=dict)
    context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("project", "requirement", "deliberation_id"):
            object.__setattr__(
                self, name, _text(getattr(self, name), name)
            )
        for name in (
            "agent_a_round1",
            "agent_b_round1",
            "lead_review",
            "agent_a_round2",
            "agent_b_round2",
            "context",
        ):
            object.__setattr__(
                self, name, _bounded_deliberation(getattr(self, name), name)
            )

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "project": self.project,
            "requirement": self.requirement,
            "deliberation_id": self.deliberation_id,
            "agent_a_round1": dict(self.agent_a_round1),
            "agent_b_round1": dict(self.agent_b_round1),
            "lead_review": dict(self.lead_review),
            "agent_a_round2": dict(self.agent_a_round2),
            "agent_b_round2": dict(self.agent_b_round2),
            "context": dict(self.context),
        }


@dataclass(frozen=True)
class FinalSynthesisResult:
    """The chair's final architecture synthesis - advisory content only.

    It can never approve anything: the application stores it as an **advisory**
    synthesis, and only a human decision through the unchanged proposal approval
    path can accept a managed project's design.
    """

    source: str
    content: Mapping[str, Any] = field(default_factory=dict)
    cost: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _text(self.source, "source"))
        object.__setattr__(
            self, "content", _bounded_deliberation(self.content, "content")
        )
        object.__setattr__(self, "cost", _mapping(self.cost, "cost"))

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "source": self.source,
            "content": dict(self.content),
            "cost": dict(self.cost),
        }


@runtime_checkable
class DeliberationLeadPort(Protocol):
    """The chair of the review board: reviews two proposals, then synthesizes.

    Two semantic operations, deliberately distinct - ``review`` produces the
    structured criticism the reconsideration round is built from, ``synthesize``
    produces the final advisory design. An implementation must

    * reason over the structured conclusions it is handed and **never vote**:
      there is no majority rule, no provider ranking and no weight here;
    * preserve a disagreement that remains instead of claiming a consensus that
      does not exist;
    * answer with structured, JSON-safe content only - a malformed answer is a
      failure, never a synthesis;
    * never mutate anything: it has no storage, no transaction boundary, no FSM
      and no approval authority, and it can never reach ``VERIFIED``.
    """

    def review(self, query: LeadReviewQuery) -> LeadReviewResult: ...

    def synthesize(self, query: LeadSynthesisQuery) -> FinalSynthesisResult: ...
