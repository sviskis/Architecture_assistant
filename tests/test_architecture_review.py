"""Tests for the three-advisor architecture review: advisory, read-only, exact.

The review is the only caller of the advisory capabilities, so these tests pin
what it promises: exactly the configured providers run in the declared order, one
provider failure never erases the others, the merger and the engine keep their
existing semantics, the judge is consulted only for a structurally valid
conflict (and its failure never discards the rest), the deterministic gate stays
authoritative, and nothing is written anywhere.

The observation seam used here is the **real** ``composition.evidence.observe``
with providers raising the **real** adapter errors, so ``ABSTAIN``/``ERROR`` are
tested as the core actually defines them.
"""

from __future__ import annotations

import ast
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

from architecture_assistant.application import (
    DEFAULT_REVIEW_QUESTION,
    JUDGE_STATUS_ERROR,
    JUDGE_STATUS_NOT_CONSULTED,
    JUDGE_STATUS_OK,
    JUDGE_STATUS_UNAVAILABLE,
    AdvisorReviewer,
    ArchitectureReview,
    EvidenceRelation,
    JudgeUseCase,
    LoopHealth,
    ReportSnapshot,
    ReviewError,
)
from architecture_assistant.composition import (
    CompositionConfig,
    compose,
    observe,
)
from architecture_assistant.domain.enums import (
    ADRStatus,
    DecisionStatus,
    Phase,
    RiskStatus,
    Severity,
    StepState,
)
from architecture_assistant.domain.models import (
    ADR,
    ArchitectureVersion,
    Decision,
    Finding,
    Project,
    Risk,
    Step,
)
from architecture_assistant.infrastructure import (
    ClaudeAdvisorAbstainError,
    ClaudeAdvisorAdapter,
    GrokAdvisorAdapter,
    HttpRequest,
    HttpResponse,
    OpenAIAdvisorAdapter,
    OpenAIAdvisorError,
)
from architecture_assistant.ports.capabilities import (
    AdvisorQuery,
    CostQuery,
    CostRecord,
    CostSummary,
    JudgeConflict,
    RealizationCheckResult,
)

NOW = datetime(2026, 9, 21, 23, 0, tzinfo=timezone.utc)

PROJECT = "youtube_to_mp3"
PLAN_VERSION = "1.0"
SOURCE_ROOT = "src/architecture_assistant"

#: The question the operator is expected to be able to write himself.
QUESTION = (
    "Review the youtube_to_mp3 architecture. Identify module boundaries, "
    "dependency risks, failure points and the simplest maintainable design."
)

RULE_ONE = "unknown-layer"
RULE_TWO = "forbidden-layer-import"
RULES = (RULE_ONE, RULE_TWO, "sqlite-outside-infrastructure")

#: The module under test - read by the boundary tests at the bottom.
MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "architecture_assistant"
    / "application"
    / "architecture_review.py"
)

#: A literal that is not, and never was, a real credential.
KEY = "sk-test-not-a-real-secret"


def fixed_clock() -> datetime:
    return NOW


def later_clock() -> datetime:
    return NOW.replace(hour=23, minute=59)


def make_project(**overrides: Any) -> Project:
    data: dict[str, Any] = dict(
        name=PROJECT,
        plan_version=PLAN_VERSION,
        mode="MANUAL",
        paused=False,
        created_at=NOW,
        updated_at=NOW,
    )
    data.update(overrides)
    return Project(**data)


def make_step(step_no: int = 1, **overrides: Any) -> Step:
    data: dict[str, Any] = dict(
        step_no=step_no,
        phase=Phase.CONTEXT,
        title=f"Step {step_no}",
        state=StepState.PENDING,
        attempt=0,
        max_attempts=3,
        created_at=NOW,
    )
    data.update(overrides)
    return Step(**data)


def make_version() -> ArchitectureVersion:
    return ArchitectureVersion(
        version="1.1",
        baseline="Layered",
        rules=RULES,
        created_at=NOW,
    )


def make_snapshot(
    *,
    steps: tuple[Step, ...] = (),
    current: Optional[int] = None,
    current_state: Optional[StepState] = None,
    architecture: bool = True,
    risks: tuple[Risk, ...] = (),
    adrs: tuple[ADR, ...] = (),
) -> ReportSnapshot:
    """The canonical read-only projection, built directly and deterministically."""
    steps = tuple(steps)
    version = make_version()
    return ReportSnapshot(
        project=make_project(),
        steps=steps,
        tasks=(),
        architecture=version if architecture else None,
        architecture_versions=(version,) if architecture else (),
        adrs=tuple(adrs),
        risks=tuple(risks),
        findings=(),
        decisions=(),
        change_requests=(),
        cost=CostSummary(),
        cost_by_step=(),
        health=LoopHealth(
            project_paused=False,
            step_count=len(steps),
            current_step_no=current,
            current_state=current_state,
            next_step_no=None,
            complete=False,
        ),
        generated_at=NOW,
    )


def finding(source: str, *, step_no: Optional[int] = 1, claim: str = "") -> Finding:
    return Finding(
        id=f"finding-{source}-{step_no or 0}-abc123abc123",
        source=source,
        claim=claim or f"{source} claim",
        evidence=("application/context.py:12",),
        confidence=0.6,
        severity=Severity.MEDIUM,
        step_no=step_no,
        created_at=NOW,
    )


# ---------------------------------------------------------------------------
# fakes: three advisors, one check, one judge, one cost query
# ---------------------------------------------------------------------------


class FakeAdvisor:
    """A scripted advisor: a finding, an abstention or a provider failure."""

    def __init__(self, source: str, *, behaviour: str = "finding") -> None:
        self.provider = source
        self.behaviour = behaviour
        self.calls = 0
        self.queries: list[AdvisorQuery] = []

    def advise(self, query: AdvisorQuery) -> Finding:
        self.calls += 1
        self.queries.append(query)
        if self.behaviour == "abstain":
            raise ClaudeAdvisorAbstainError("too little context")
        if self.behaviour == "error":
            raise OpenAIAdvisorError("the provider is unavailable")
        return finding(self.provider, step_no=query.step_no)


class FakeCheck:
    """A read-only realization check: a settled verdict and no persistence."""

    def __init__(
        self,
        *,
        compliant: bool = True,
        rules: tuple[str, ...] = RULES,
        findings: tuple[Finding, ...] = (),
        failure: Optional[BaseException] = None,
        source_root: Optional[str] = None,
    ) -> None:
        self.compliant = compliant
        self.rules = rules
        self.findings = findings
        self.failure = failure
        self.calls: list[tuple[int, int]] = []
        if source_root is not None:
            self.source_root = source_root

    def check(self, step_no: int, attempt: int) -> RealizationCheckResult:
        self.calls.append((step_no, attempt))
        if self.failure is not None:
            raise self.failure
        status = (
            DecisionStatus.ACCEPTED if self.compliant else DecisionStatus.REJECTED
        )
        findings = self.findings
        if not self.compliant and not findings:
            # the port requires a non-compliant result to carry its violations
            findings = (
                finding("architecture-validator", step_no=step_no),
            )
        decision = Decision(
            id=f"realization-{step_no}-{attempt}",
            status=status,
            decision="the deterministic gate verdict",
            rationale="deterministic rules decided this",
            rules_applied=self.rules,
            evidence_refs=tuple(item.id for item in findings),
            step_no=step_no,
            created_at=NOW,
        )
        return RealizationCheckResult(
            compliant=self.compliant,
            baseline_version="1.1",
            decision=decision,
            findings=findings,
        )


class FakeJudge:
    """A judge implementation that answers, or fails on purpose."""

    def __init__(
        self,
        *,
        status: DecisionStatus = DecisionStatus.ACCEPTED,
        failure: Optional[BaseException] = None,
    ) -> None:
        self.status = status
        self.failure = failure
        self.conflicts: list[JudgeConflict] = []

    def judge(self, conflict: JudgeConflict) -> Decision:
        self.conflicts.append(conflict)
        if self.failure is not None:
            raise self.failure
        return Decision(
            id=f"judge-{len(self.conflicts)}",
            status=self.status,
            decision=f"the judge answers {conflict.question[:20]}",
            rationale="the judge weighed the named findings",
            evidence_refs=tuple(item.id for item in conflict.findings),
            created_at=NOW,
        )


class SpyCostSink:
    """A cost port that records what the adapters report."""

    def __init__(self) -> None:
        self.records: list[CostRecord] = []

    def record(self, cost: CostRecord) -> None:
        self.records.append(cost)


class SpyCostQuery:
    """A cost query seam that answers per provider and records the filters."""

    def __init__(self) -> None:
        self.filters: list[CostQuery] = []

    def __call__(self, cost_filter: CostQuery) -> CostSummary:
        self.filters.append(cost_filter)
        priced = cost_filter.provider is not None
        return CostSummary(
            total_usd=0.25 if priced else 0.75,
            input_tokens=10,
            output_tokens=5,
            record_count=1,
            priced_record_count=1 if priced else 0,
            unpriced_record_count=0 if priced else 1,
        )


class ScriptedTransport:
    """A minimal offline transport: each call returns the next response."""

    def __init__(self, *responses: HttpResponse) -> None:
        self._responses = list(responses)
        self.calls = 0

    def __call__(self, request: HttpRequest) -> HttpResponse:
        index = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[index]


def envelope(payload: dict, *, shape: str = "openai") -> HttpResponse:
    """Wrap a provider contract in the provider's own envelope shape."""
    text = json.dumps(payload)
    if shape == "anthropic":
        body = {
            "id": "msg-test-1",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 5, "output_tokens": 7},
        }
    else:
        body = {
            "id": "cmpl-test-1",
            "choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7},
        }
    return HttpResponse(200, json.dumps(body).encode("utf-8"))


def contract(**overrides: Any) -> dict:
    payload: dict[str, Any] = {
        "status": "OK",
        "claim": "a structured claim",
        "evidence": ["application/context.py:12"],
        "confidence": 0.5,
        "severity": "MEDIUM",
        "reason": "",
    }
    payload.update(overrides)
    return payload


def make_reviewers(
    *, behaviours: tuple[str, ...] = ("finding", "finding", "finding")
) -> tuple[tuple[AdvisorReviewer, ...], tuple[FakeAdvisor, ...]]:
    """Scripted reviewers, in the declared provider order."""
    sources = ("openai", "claude", "grok")
    advisors = tuple(
        FakeAdvisor(source, behaviour=behaviour)
        for source, behaviour in zip(sources, behaviours)
    )
    return (
        tuple(
            AdvisorReviewer(advisor.provider, advisor) for advisor in advisors
        ),
        advisors,
    )


def build_review(
    *,
    reviewers: Optional[tuple[AdvisorReviewer, ...]] = None,
    behaviours: tuple[str, ...] = ("finding", "finding", "finding"),
    judge: Optional[JudgeUseCase] = None,
    check: Optional[FakeCheck] = None,
    cost_query: Any = None,
    observer: Any = observe,
    source_root: str = SOURCE_ROOT,
    clock: Any = fixed_clock,
) -> tuple[ArchitectureReview, tuple[FakeAdvisor, ...]]:
    """A review over scripted advisors, unless reviewers are supplied."""
    if reviewers is None:
        reviewers, advisors = make_reviewers(behaviours=behaviours)
    else:
        advisors = tuple(reviewer.advisor for reviewer in reviewers)
    review = ArchitectureReview(
        reviewers,
        observer=observer,
        source_root=source_root,
        judge=judge if judge is not None else JudgeUseCase(judge=FakeJudge()),
        check=check if check is not None else FakeCheck(),
        cost_query=cost_query,
        clock=clock,
    )
    return review, advisors  # type: ignore[return-value]


class TrackingAdvisor:
    """An advisor that records the order in which it was called."""

    def __init__(self, inner: FakeAdvisor, seen: list[str]) -> None:
        self.provider = inner.provider
        self._inner = inner
        self._seen = seen

    def advise(self, query: AdvisorQuery) -> Finding:
        self._seen.append(self.provider)
        return self._inner.advise(query)


class BrokenAdvisor:
    """An advisor with a programming defect (not a provider error)."""

    provider = "openai"

    def advise(self, query: AdvisorQuery) -> Finding:
        raise ZeroDivisionError("a bug, not a provider failure")


# ---------------------------------------------------------------------------
# 1, 2: exactly the configured advisors, in the declared order
# ---------------------------------------------------------------------------


class TestProviderExecution:
    """The three configured advisors run once each, in the declared order."""

    def test_exactly_the_configured_reviewers_are_invoked(self) -> None:
        review, advisors = build_review()

        result = review.review(make_snapshot(), question=QUESTION)

        assert [advisor.calls for advisor in advisors] == [1, 1, 1]
        assert [stage.source for stage in result.providers] == [
            "openai",
            "claude",
            "grok",
        ]
        assert len(result.providers) == 3

    def test_the_providers_are_reported_in_the_declared_order(self) -> None:
        called: list[str] = []
        reviewers, _advisors = make_reviewers()
        ordered = tuple(
            AdvisorReviewer(
                reviewer.source, TrackingAdvisor(reviewer.advisor, called)
            )
            for reviewer in reviewers
        )
        review, _ = build_review(reviewers=ordered)

        result = review.review(make_snapshot(), question=QUESTION).to_dict()

        assert called == ["openai", "claude", "grok"]
        assert [stage["source"] for stage in result["providers"]] == [
            "openai",
            "claude",
            "grok",
        ]

    def test_the_reviewers_keep_their_configured_order(self) -> None:
        reviewers, _advisors = make_reviewers()
        reversed_reviewers = tuple(reversed(reviewers))
        review, _ = build_review(reviewers=reversed_reviewers)

        assert [entry.source for entry in review.reviewers] == [
            entry.source for entry in reversed_reviewers
        ]
        assert [entry.source for entry in review.reviewers] == [
            "grok",
            "claude",
            "openai",
        ]


# ---------------------------------------------------------------------------
# 3, 4, 5: provider failure semantics - exactly the core's own
# ---------------------------------------------------------------------------


class TestProviderFailures:
    """One provider's fate never decides another's, and nothing is invented."""

    def test_one_provider_error_keeps_the_other_findings(self) -> None:
        review, _advisors = build_review(
            behaviours=("finding", "error", "finding")
        )

        result = review.review(make_snapshot(), question=QUESTION)

        assert [stage.status for stage in result.providers] == [
            "FINDING",
            "ERROR",
            "FINDING",
        ]
        assert [stage.source for stage in result.findings] == [
            "openai",
            "grok",
        ]
        errors = result.to_dict()["evidence"]["errors"]
        assert [entry["source"] for entry in errors] == ["claude"]
        assert errors[0]["reason"] == "advisor failed: OpenAIAdvisorError"

    def test_an_abstaining_provider_is_reported_as_abstain(self) -> None:
        review, _advisors = build_review(
            behaviours=("finding", "abstain", "finding")
        )

        result = review.review(make_snapshot(), question=QUESTION).to_dict()

        assert [stage["status"] for stage in result["providers"]] == [
            "FINDING",
            "ABSTAIN",
            "FINDING",
        ]
        abstained = result["evidence"]["abstained"]
        assert [entry["source"] for entry in abstained] == ["claude"]
        assert abstained[0]["reason"].startswith("advisor abstained:")
        # an abstention is not evidence and not a rejection
        assert len(result["evidence"]["findings"]) == 2

    def test_all_three_failing_is_an_error_not_a_fake_success(self) -> None:
        review, _advisors = build_review(behaviours=("error", "error", "error"))

        result = review.review(make_snapshot(), question=QUESTION).to_dict()

        assert result["decision"]["status"] == DecisionStatus.ERROR.value
        assert result["evidence"]["findings"] == []
        assert len(result["evidence"]["errors"]) == 3
        assert result["judge"]["consulted"] is False

    def test_a_malformed_provider_result_is_an_error(self) -> None:
        """A broken contract becomes ERROR - never a finding, never REJECT."""
        advisor = OpenAIAdvisorAdapter(
            api_key=KEY,
            transport=ScriptedTransport(HttpResponse(200, b'{"choices": []}')),
            sleep=lambda _seconds: None,
        )
        reviewers = (
            AdvisorReviewer("openai", advisor),
            AdvisorReviewer("claude", FakeAdvisor("claude")),
            AdvisorReviewer("grok", FakeAdvisor("grok")),
        )
        review, _ = build_review(reviewers=reviewers)

        result = review.review(make_snapshot(), question=QUESTION)

        assert result.providers[0].status == "ERROR"
        assert result.providers[0].reason == (
            "advisor failed: OpenAIAdvisorInvalidResponseError"
        )
        assert len(result.findings) == 2

    def test_a_non_provider_defect_is_not_swallowed(self) -> None:
        """The seam classifies provider errors only; a bug stays a bug."""
        review, _ = build_review(
            reviewers=(
                AdvisorReviewer("openai", BrokenAdvisor()),
                AdvisorReviewer("claude", FakeAdvisor("claude")),
                AdvisorReviewer("grok", FakeAdvisor("grok")),
            )
        )

        with pytest.raises(ZeroDivisionError):
            review.review(make_snapshot(), question=QUESTION)


def build_bare_review(
    reviewers: tuple[AdvisorReviewer, ...],
    *,
    judge: Optional[JudgeUseCase] = None,
) -> ArchitectureReview:
    """A review with **no** realization check - i.e. no deterministic gate."""
    return ArchitectureReview(
        reviewers,
        observer=observe,
        source_root=SOURCE_ROOT,
        judge=judge,
        check=None,
        cost_query=None,
        clock=fixed_clock,
    )


def declared(
    *pairs: tuple[str, EvidenceRelation, str],
    extra: tuple[str, ...] = (),
) -> tuple[AdvisorReviewer, ...]:
    """Reviewers whose relations are *explicit metadata*, never inferred."""
    reviewers = tuple(
        AdvisorReviewer(
            source, FakeAdvisor(source), relation=relation, relation_target=target
        )
        for source, relation, target in pairs
    )
    return reviewers + tuple(
        AdvisorReviewer(source, FakeAdvisor(source)) for source in extra
    )


# ---------------------------------------------------------------------------
# 6, 7, 12: the merger, the engine and JSON safety
# ---------------------------------------------------------------------------


class TestEvidenceAndDecision:
    """The merger and the engine keep their semantics; nothing is a vote."""

    def test_the_merger_receives_every_valid_observation(self) -> None:
        review, _advisors = build_review(
            behaviours=("finding", "abstain", "error")
        )

        evidence = review.review(
            make_snapshot(), question=QUESTION
        ).to_dict()["evidence"]

        assert [item["source"] for item in evidence["findings"]] == ["openai"]
        assert [item["source"] for item in evidence["abstained"]] == ["claude"]
        assert [item["source"] for item in evidence["errors"]] == ["grok"]

    def test_three_agreeing_findings_are_never_a_vote(self) -> None:
        reviewers = declared(
            ("openai", EvidenceRelation.SUPPORTING, RULE_ONE),
            ("claude", EvidenceRelation.SUPPORTING, RULE_ONE),
            ("grok", EvidenceRelation.SUPPORTING, RULE_ONE),
        )

        result = build_bare_review(reviewers).review(
            make_snapshot(), question=QUESTION
        ).to_dict()

        # without a deterministic verdict no relation can be validated, so
        # three identical supporting findings are still not an ACCEPTED
        assert result["evidence"]["supporting_ids"] == []
        assert result["evidence"]["gate_anchors"] == []
        assert result["decision"]["status"] == DecisionStatus.ABSTAIN.value
        assert result["deterministic_gate"]["available"] is False

    def test_the_gate_decides_and_the_findings_only_explain(self) -> None:
        snapshot = make_snapshot(steps=(make_step(),), current=1)
        for compliant, expected in ((True, "ACCEPTED"), (False, "REJECTED")):
            reviewers = declared(
                ("openai", EvidenceRelation.SUPPORTING, RULE_ONE),
                ("claude", EvidenceRelation.CONTRADICTING, RULE_ONE),
                extra=("grok",),
            )
            review, _ = build_review(
                reviewers=reviewers, check=FakeCheck(compliant=compliant)
            )

            result = review.review(snapshot, question=QUESTION)

            assert result.decision["status"] == expected
            assert result.deterministic_gate["compliant"] is compliant

    def test_the_review_is_json_safe(self) -> None:
        review, advisors = build_review(cost_query=SpyCostQuery())
        snapshot = make_snapshot(
            steps=(make_step(),),
            current=1,
            current_state=StepState.PENDING,
        )

        result = review.review(snapshot, question=QUESTION).to_dict()

        assert json.loads(json.dumps(result))["review_id"] == result["review_id"]
        assert result["persistence"] == {
            "written": False,
            "reason": (
                "the review is advisory and read-only: nothing is persisted"
            ),
        }
        # the context each provider received is plain data too - no default=str
        for advisor in advisors:
            context = dict(advisor.queries[0].context)
            assert json.loads(json.dumps(context))["project"]["name"] == PROJECT


# ---------------------------------------------------------------------------
# 8, 9, 10 + correction 3: the judge is gated, separate and non-fatal
# ---------------------------------------------------------------------------


class TestJudge:
    """The judge is consulted only for a validated conflict, and never wins."""

    def test_the_judge_is_not_called_without_a_conflict(self) -> None:
        judge = FakeJudge()
        review, _advisors = build_review(judge=JudgeUseCase(judge=judge))

        result = review.review(make_snapshot(), question=QUESTION)

        assert judge.conflicts == []
        assert result.judge.consulted is False
        assert result.judge.status == JUDGE_STATUS_NOT_CONSULTED
        assert result.judge.count == 0
        assert result.conflicts == ()

    def test_an_unvalidated_relation_is_not_a_conflict(self) -> None:
        reviewers = declared(
            ("openai", EvidenceRelation.SUPPORTING, "not-a-gate-anchor"),
            ("claude", EvidenceRelation.CONTRADICTING, "not-a-gate-anchor"),
            extra=("grok",),
        )
        judge = FakeJudge()
        review, _ = build_review(
            reviewers=reviewers, judge=JudgeUseCase(judge=judge)
        )

        result = review.review(make_snapshot(), question=QUESTION)

        assert result.conflicts == ()
        assert judge.conflicts == []
        assert [stage.relation for stage in result.providers] == [
            EvidenceRelation.UNRESOLVED.value,
            EvidenceRelation.UNRESOLVED.value,
            EvidenceRelation.UNRESOLVED.value,
        ]

    def test_the_judge_is_called_once_per_conflict(self) -> None:
        reviewers = declared(
            ("openai", EvidenceRelation.SUPPORTING, RULE_ONE),
            ("claude", EvidenceRelation.CONTRADICTING, RULE_ONE),
            ("grok", EvidenceRelation.SUPPORTING, RULE_TWO),
            ("fourth", EvidenceRelation.CONTRADICTING, RULE_TWO),
        )
        judge = FakeJudge()
        review, _ = build_review(
            reviewers=reviewers, judge=JudgeUseCase(judge=judge)
        )

        result = review.review(
            make_snapshot(steps=(make_step(),), current=1), question=QUESTION
        )

        assert sorted(
            conflict["target"] for conflict in result.conflicts
        ) == sorted([RULE_ONE, RULE_TWO])
        assert len(judge.conflicts) == 2
        assert result.judge.count == 2
        assert result.judge.status == JUDGE_STATUS_OK

    def test_a_rejected_gate_stays_rejected_with_a_judge(self) -> None:
        reviewers = declared(
            ("openai", EvidenceRelation.SUPPORTING, RULE_ONE),
            ("claude", EvidenceRelation.CONTRADICTING, RULE_ONE),
            extra=("grok",),
        )
        judge = FakeJudge(status=DecisionStatus.ACCEPTED)
        review, _ = build_review(
            reviewers=reviewers,
            judge=JudgeUseCase(judge=judge),
            check=FakeCheck(compliant=False),
        )

        result = review.review(
            make_snapshot(steps=(make_step(),), current=1), question=QUESTION
        )

        assert result.decision["status"] == DecisionStatus.REJECTED.value
        assert result.deterministic_gate["decision_status"] == "REJECTED"
        assert result.judge.count == 1
        (judgment,) = result.judge.judgments
        assert judgment["decision"]["status"] == DecisionStatus.ACCEPTED.value
        assert judgment["decision"]["id"] != result.decision["id"]
        assert judgment["decision"]["id"].startswith("judge-")

    def test_a_failing_judge_preserves_the_whole_review(self) -> None:
        reviewers = declared(
            ("openai", EvidenceRelation.SUPPORTING, RULE_ONE),
            ("claude", EvidenceRelation.CONTRADICTING, RULE_ONE),
            extra=("grok",),
        )
        judge = FakeJudge(failure=RuntimeError("the judge provider exploded"))
        review, _ = build_review(
            reviewers=reviewers, judge=JudgeUseCase(judge=judge),
            cost_query=SpyCostQuery(),
        )
        snapshot = make_snapshot(steps=(make_step(),), current=1)

        result = review.review(snapshot, question=QUESTION)
        payload = result.to_dict()

        # the judge layer reports a sanitized failure
        assert payload["judge"]["consulted"] is True
        assert payload["judge"]["available"] is True
        assert payload["judge"]["status"] == JUDGE_STATUS_ERROR
        assert payload["judge"]["reason"] == "RuntimeError"
        assert payload["judge"]["judgments"] == []
        assert payload["judge"]["count"] == 0
        # and everything already completed survives, unchanged
        assert [stage.status for stage in result.providers] == [
            "FINDING",
            "FINDING",
            "FINDING",
        ]
        assert len(payload["evidence"]["findings"]) == 3
        assert len(payload["conflicts"]) == 1
        assert payload["deterministic_gate"]["available"] is True
        assert payload["deterministic_gate"]["compliant"] is True
        assert payload["decision"]["status"] == DecisionStatus.ACCEPTED.value
        assert payload["cost"]["providers"][0]["available"] is True
        # the raw provider message never travels
        assert "the judge provider exploded" not in json.dumps(payload)

    def test_a_judge_use_case_without_an_implementation_is_unavailable(
        self,
    ) -> None:
        reviewers = declared(
            ("openai", EvidenceRelation.SUPPORTING, RULE_ONE),
            ("claude", EvidenceRelation.CONTRADICTING, RULE_ONE),
            extra=("grok",),
        )
        review, _ = build_review(reviewers=reviewers, judge=JudgeUseCase())

        result = review.review(
            make_snapshot(steps=(make_step(),), current=1), question=QUESTION
        )

        # a real conflict exists, but no judge implementation is configured
        assert len(result.conflicts) == 1
        assert result.judge.available is False
        assert result.judge.consulted is False
        assert result.judge.status == JUDGE_STATUS_UNAVAILABLE
        assert "no judge implementation" in result.judge.reason
        assert len(result.findings) == 3


# ---------------------------------------------------------------------------
# correction 2 + AQ-REVIEW-2: the read-only deterministic gate and its tree
# ---------------------------------------------------------------------------


class TestDeterministicGate:
    """The gate is read freshly through the check port, and persisted nowhere."""

    def test_the_check_runs_fresh_on_every_review(self) -> None:
        check = FakeCheck()
        review, _ = build_review(check=check)
        snapshot = make_snapshot(steps=(make_step(attempt=2),), current=1)

        review.review(snapshot, question=QUESTION)
        review.review(snapshot, question=QUESTION)

        assert check.calls == [(1, 2), (1, 2)]

    def test_the_first_attempt_follows_the_loops_own_convention(self) -> None:
        check = FakeCheck()
        review, _ = build_review(check=check)

        review.review(
            make_snapshot(steps=(make_step(),), current=1), question=QUESTION
        )

        # the orchestrator dispatches the first attempt as max(attempt, 1)
        assert check.calls == [(1, 1)]

    def test_no_current_step_means_no_gate(self) -> None:
        result = build_review()[0].review(make_snapshot(), question=QUESTION)

        assert result.step_no is None
        assert result.deterministic_gate["available"] is False
        assert result.deterministic_gate["reason"] == (
            "there is no current step to check"
        )

    def test_a_failing_check_is_reported_and_never_raised(self) -> None:
        check = FakeCheck(failure=RuntimeError("the control plane is broken"))
        review, _ = build_review(check=check)

        result = review.review(
            make_snapshot(steps=(make_step(),), current=1), question=QUESTION
        )
        payload = result.to_dict()

        assert payload["deterministic_gate"]["available"] is False
        assert payload["deterministic_gate"]["reason"] == "RuntimeError"
        assert len(payload["providers"]) == 3
        assert "the control plane is broken" not in json.dumps(payload)

    def test_the_reported_source_root_is_the_configured_one(self) -> None:
        review, _ = build_review(
            check=FakeCheck(source_root=SOURCE_ROOT), source_root=SOURCE_ROOT
        )

        payload = review.review(make_snapshot(), question=QUESTION).to_dict()

        assert payload["source_root"] == SOURCE_ROOT
        assert payload["check_source_root"] == SOURCE_ROOT
        assert payload["source_root_verified"] is True

    def test_a_check_on_another_tree_is_made_visible(self) -> None:
        review, _ = build_review(
            check=FakeCheck(source_root="src/another_project"),
            source_root=SOURCE_ROOT,
        )

        payload = review.review(make_snapshot(), question=QUESTION).to_dict()

        # the configured root is reported as configured - never silently swapped
        assert payload["source_root"] == SOURCE_ROOT
        assert payload["check_source_root"] == "src/another_project"
        assert payload["source_root_verified"] is False

    def test_the_context_carries_the_root_the_step_and_the_gate(self) -> None:
        review, advisors = build_review()

        review.review(
            make_snapshot(steps=(make_step(),), current=1), question=QUESTION
        )
        context = dict(advisors[0].queries[0].context)

        assert context["source_root"] == SOURCE_ROOT
        assert context["review_question"] == QUESTION
        assert context["architecture"]["version"] == "1.1"
        assert context["architecture"]["rules"] == list(RULES)
        assert context["step"]["step_no"] == 1
        assert context["health"]["step_count"] == 1
        assert context["deterministic_gate"]["available"] is True

    def test_the_review_id_is_deterministic_and_clock_free(self) -> None:
        reviewers, _advisors = make_reviewers()
        early, _ = build_review(reviewers=reviewers, clock=fixed_clock)
        late, _ = build_review(reviewers=reviewers, clock=later_clock)

        first = early.review(make_snapshot(), question=QUESTION)
        second = late.review(make_snapshot(), question=QUESTION)

        assert first.review_id == second.review_id
        assert first.reviewed_at != second.reviewed_at


# ---------------------------------------------------------------------------
# correction 1: the operator's own question, verbatim, once
# ---------------------------------------------------------------------------


class TestReviewQuestion:
    """The operator writes the question; the review never rewrites it."""

    def test_the_operator_question_is_sent_verbatim_to_every_advisor(self) -> None:
        review, advisors = build_review()
        asked = f"{QUESTION}  (as typed, with two trailing spaces)\n"

        result = review.review(make_snapshot(), question=asked)

        assert result.question == asked
        for advisor in advisors:
            assert advisor.queries[0].question == asked

    def test_the_default_question_is_used_when_none_is_given(self) -> None:
        review, advisors = build_review()

        result = review.review(make_snapshot())

        assert result.question == DEFAULT_REVIEW_QUESTION
        assert all(
            advisor.queries[0].question == DEFAULT_REVIEW_QUESTION
            for advisor in advisors
        )

    def test_a_blank_question_is_refused_before_any_provider_call(self) -> None:
        review, advisors = build_review()

        for blank in ("", "   ", "\n\t "):
            with pytest.raises(ReviewError, match="question"):
                review.review(make_snapshot(), question=blank)

        assert [advisor.calls for advisor in advisors] == [0, 0, 0]

    def test_a_non_string_question_is_refused(self) -> None:
        review, _advisors = build_review()

        with pytest.raises(ReviewError, match="question"):
            review.review(make_snapshot(), question=None)

    def test_two_reviews_are_two_independent_one_shots(self) -> None:
        review, advisors = build_review()
        snapshot = make_snapshot()

        review.review(snapshot, question=QUESTION)
        review.review(snapshot, question=QUESTION)

        for advisor in advisors:
            assert len(advisor.queries) == 2
            first, second = advisor.queries
            # identical input, identical context: no conversation, no memory
            assert first.question == second.question == QUESTION
            assert first.context == second.context


# ---------------------------------------------------------------------------
# 11: cost, through the existing CostPort only
# ---------------------------------------------------------------------------


class TestCost:
    """Cost is reported per provider, and never invented."""

    def test_the_review_reports_per_provider_cost(self) -> None:
        cost = SpyCostQuery()
        review, _advisors = build_review(cost_query=cost)

        payload = review.review(make_snapshot(), question=QUESTION).to_dict()

        providers = payload["cost"]["providers"]
        assert [entry["source"] for entry in providers] == [
            "openai",
            "claude",
            "grok",
        ]
        assert all(entry["available"] is True for entry in providers)
        assert all(entry["total_usd"] == 0.25 for entry in providers)
        assert payload["cost"]["total"]["total_usd"] == 0.75
        assert payload["cost"]["total"]["record_count"] == 3
        assert payload["cost"]["judge"]["available"] is False
        assert "not consulted" in payload["cost"]["judge"]["reason"]
        assert [entry.provider for entry in cost.filters] == [
            "openai",
            "claude",
            "grok",
        ]
        assert all(
            entry.from_time is not None and entry.to_time is not None
            for entry in cost.filters
        )

    def test_no_cost_capability_is_reported_honestly(self) -> None:
        payload = build_review()[0].review(
            make_snapshot(), question=QUESTION
        ).to_dict()

        assert payload["cost"]["providers"][0]["available"] is False
        assert "no cost capability" in payload["cost"]["providers"][0]["reason"]
        assert payload["cost"]["total"]["available"] is False

    def test_provider_usage_reaches_the_cost_port(self) -> None:
        sink = SpyCostSink()
        advisor = OpenAIAdvisorAdapter(
            api_key=KEY,
            transport=ScriptedTransport(envelope(contract())),
            cost_sink=sink,
            clock=fixed_clock,
        )
        reviewers = (
            AdvisorReviewer("openai", advisor),
            AdvisorReviewer("claude", FakeAdvisor("claude")),
            AdvisorReviewer("grok", FakeAdvisor("grok")),
        )
        review, _ = build_review(
            reviewers=reviewers, cost_query=SpyCostQuery()
        )

        result = review.review(
            make_snapshot(steps=(make_step(),), current=1), question=QUESTION
        )

        assert len(sink.records) == 1
        record = sink.records[0]
        assert record.provider == "openai"
        assert (record.input_tokens, record.output_tokens) == (5, 7)
        assert record.step_no == 1
        # no price was configured: an unknown is never a fake zero-dollar cost
        assert record.pricing_known is False
        assert result.providers[0].status == "FINDING"


def make_config(tmp_path: Path, **overrides: Any) -> CompositionConfig:
    """A real composition config over a temporary database."""
    data: dict[str, Any] = dict(
        database_path=tmp_path / "assistant.db",
        exchange_dir=tmp_path / "cline",
        report_dir=tmp_path / "reports",
        source_root=Path(SOURCE_ROOT),
        project_name=PROJECT,
        plan_version=PLAN_VERSION,
        clock=fixed_clock,
    )
    data.update(overrides)
    return CompositionConfig(**data)


# ---------------------------------------------------------------------------
# 17 + isolation: the review can only review
# ---------------------------------------------------------------------------


class TestReadOnlySurface:
    """The review holds the injected seams, and nothing that could mutate."""

    def test_the_review_holds_only_the_injected_seams(self) -> None:
        review, _advisors = build_review()

        assert set(vars(review)) == {
            "_reviewers",
            "_observer",
            "_source_root",
            "_judge",
            "_check",
            "_cost_query",
            "_clock",
        }

    def test_the_public_surface_is_the_review_and_nothing_else(self) -> None:
        public = {
            name
            for name in dir(ArchitectureReview)
            if not name.startswith("_")
        }

        assert public == {
            "review",
            "reviewers",
            "source_root",
            "has_judge",
            "has_check",
        }

    def test_the_module_imports_only_inner_layers(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        absolute: set[str] = set()
        relative: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                absolute.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    relative.add(node.module or "")
                else:
                    absolute.add(node.module or "")

        assert absolute == {
            "__future__",
            "dataclasses",
            "datetime",
            "hashlib",
            "pathlib",
            "typing",
        }
        assert relative == {
            "domain.enums",
            "domain.models",
            "ports.capabilities",
            "decision_engine",
            "eventlog",
            "evidence_merger",
            "judge",
            "reporting",
        }

    def test_the_review_writes_nothing_to_the_source_of_truth(
        self, tmp_path
    ) -> None:
        composition = compose(make_config(tmp_path))
        try:
            composition.storage.steps.upsert(make_step())
            storage = composition.storage

            def state() -> dict[str, Any]:
                return {
                    "project": storage.projects.list(),
                    "steps": storage.steps.list(),
                    "tasks": storage.tasks.list(),
                    "versions": storage.architecture_versions.list(),
                    "adrs": storage.adrs.list(),
                    "risks": storage.risks.list(),
                    "findings": storage.findings.list(),
                    "decisions": storage.decisions.list(),
                    "change_requests": storage.change_requests.list(),
                    "audit": storage.audit.list(),
                }

            before = state()
            payload = composition.architecture_review.review(
                composition.monitor.snapshot(), question=QUESTION
            ).to_dict()

            assert state() == before
            assert payload["persistence"]["written"] is False
            # no provider answered, so no cost telemetry exists either
            assert composition.cost_plugin.query(CostQuery()).record_count == 0
        finally:
            composition.close()


# ---------------------------------------------------------------------------
# the composed object graph (correction 2's required proof lives here)
# ---------------------------------------------------------------------------


class TestComposition:
    """The composed review is wired to the real capabilities, and to nothing else."""

    def test_the_composed_reviewers_are_the_three_real_adapters(
        self, tmp_path
    ) -> None:
        composition = compose(make_config(tmp_path))
        try:
            review = composition.architecture_review

            assert [entry.source for entry in review.reviewers] == [
                "openai",
                "claude",
                "grok",
            ]
            assert [entry.advisor for entry in review.reviewers] == [
                composition.openai_advisor,
                composition.claude_advisor,
                composition.grok_advisor,
            ]
            assert isinstance(
                review.reviewers[0].advisor, OpenAIAdvisorAdapter
            )
            assert isinstance(
                review.reviewers[1].advisor, ClaudeAdvisorAdapter
            )
            assert isinstance(review.reviewers[2].advisor, GrokAdvisorAdapter)
            assert review.has_judge is True
            assert review.has_check is True
            # no relation is declared by the composition: nothing is inferred
            assert [entry.relation for entry in review.reviewers] == [
                EvidenceRelation.UNRESOLVED
            ] * 3
        finally:
            composition.close()

    def test_the_review_uses_the_composition_configured_source_root(
        self, tmp_path
    ) -> None:
        """The displayed and checked tree is the configured one - never a guess."""
        config = make_config(tmp_path)
        composition = compose(config)
        try:
            review = composition.architecture_review

            assert review.source_root == str(config.source_root)
            assert str(composition.realization_adapter.source_root) == str(
                config.source_root
            )

            payload = review.review(
                composition.monitor.snapshot(), question=QUESTION
            ).to_dict()

            assert payload["source_root"] == str(config.source_root)
            assert payload["check_source_root"] == str(config.source_root)
            assert payload["source_root_verified"] is True
        finally:
            composition.close()

    def test_an_offline_review_is_honest_about_missing_keys(
        self, tmp_path
    ) -> None:
        composition = compose(make_config(tmp_path))
        try:
            payload = composition.architecture_review.review(
                composition.monitor.snapshot(), question=QUESTION
            ).to_dict()

            assert [entry["status"] for entry in payload["providers"]] == [
                "ERROR",
                "ERROR",
                "ERROR",
            ]
            assert payload["decision"]["status"] == DecisionStatus.ERROR.value
            assert payload["judge"]["status"] == JUDGE_STATUS_NOT_CONSULTED
            assert payload["deterministic_gate"]["available"] is False
            assert payload["persistence"]["written"] is False
        finally:
            composition.close()

    def test_the_review_is_not_part_of_the_decision_path(self, tmp_path) -> None:
        composition = compose(make_config(tmp_path))
        try:
            for collaborator in (
                composition.orchestrator,
                composition.scheduler,
                composition.realization_control,
                composition.realization_adapter,
                composition.monitor,
                composition.approval,
                composition.human_override,
                composition.plan_loader,
            ):
                assert "architecture_review" not in dir(collaborator)
                assert "advisor" not in dir(collaborator)
                assert "judge" not in dir(collaborator)

            review = composition.architecture_review
            for forbidden in (
                "gate",
                "orchestrator",
                "scheduler",
                "storage",
                "monitor",
                "worker",
                "plan_loader",
                "approval",
                "human_override",
            ):
                assert not hasattr(review, forbidden), forbidden
        finally:
            composition.close()

    def test_the_loop_never_calls_an_advisor(self, tmp_path) -> None:
        """The review is explicit-operator-only: a loop run calls no provider."""
        composition = compose(make_config(tmp_path))
        try:
            composition.storage.steps.upsert(make_step())

            composition.run_until_idle()

            assert composition.cost_plugin.query(CostQuery()).record_count == 0
        finally:
            composition.close()
