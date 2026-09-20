"""Tests for the pure loop policies (approval mode and review decision).

The approval matrix is the declared operating-mode semantics, tested row by row;
the review decision is tested against every report status plus the precedence
rules (architecture questions and unresolved issues always win).
"""

from __future__ import annotations

import pytest

from architecture_assistant.application import (
    ReviewOutcome,
    decide_review,
    requires_approval,
)
from architecture_assistant.domain.enums import (
    Mode,
    ReportStatus,
    RiskLevel,
    StepEvent,
    StepState,
)
from architecture_assistant.domain.fsm import StepStateMachine
from architecture_assistant.ports.capabilities import WorkerResult


def report(
    status: ReportStatus,
    *,
    issues: tuple[str, ...] = (),
    questions: tuple[str, ...] = (),
) -> WorkerResult:
    return WorkerResult(
        status=status,
        summary="a summary",
        issues=issues,
        architecture_questions=questions,
    )


#: (mode, risk, requires_human, expected) - the complete approval semantics.
APPROVAL_MATRIX = (
    # MANUAL: every step waits for a human decision.
    (Mode.MANUAL, RiskLevel.LOW, False, True),
    (Mode.MANUAL, RiskLevel.LOW, True, True),
    (Mode.MANUAL, RiskLevel.MEDIUM, False, True),
    (Mode.MANUAL, RiskLevel.MEDIUM, True, True),
    (Mode.MANUAL, RiskLevel.HIGH, False, True),
    (Mode.MANUAL, RiskLevel.HIGH, True, True),
    # SUPERVISED: only LOW without an explicit human requirement is automatic.
    (Mode.SUPERVISED, RiskLevel.LOW, False, False),
    (Mode.SUPERVISED, RiskLevel.LOW, True, True),
    (Mode.SUPERVISED, RiskLevel.MEDIUM, False, True),
    (Mode.SUPERVISED, RiskLevel.MEDIUM, True, True),
    (Mode.SUPERVISED, RiskLevel.HIGH, False, True),
    (Mode.SUPERVISED, RiskLevel.HIGH, True, True),
    # AUTO: LOW and MEDIUM without a human requirement are automatic.
    (Mode.AUTO, RiskLevel.LOW, False, False),
    (Mode.AUTO, RiskLevel.LOW, True, True),
    (Mode.AUTO, RiskLevel.MEDIUM, False, False),
    (Mode.AUTO, RiskLevel.MEDIUM, True, True),
    (Mode.AUTO, RiskLevel.HIGH, False, True),
    (Mode.AUTO, RiskLevel.HIGH, True, True),
)


class TestRequiresApproval:
    @pytest.mark.parametrize(
        "mode,risk,requires_human,expected", APPROVAL_MATRIX
    )
    def test_approval_matrix(
        self,
        mode: Mode,
        risk: RiskLevel,
        requires_human: bool,
        expected: bool,
    ) -> None:
        assert requires_approval(mode, risk, requires_human) is expected

    def test_manual_never_dispatches_automatically(self) -> None:
        assert all(
            requires_approval(Mode.MANUAL, risk, flag)
            for risk in RiskLevel
            for flag in (False, True)
        )

    def test_supervised_is_not_the_same_as_auto(self) -> None:
        """SUPERVISED must not be flattened into "everything is automatic"."""
        assert requires_approval(Mode.SUPERVISED, RiskLevel.MEDIUM, False)
        assert not requires_approval(Mode.AUTO, RiskLevel.MEDIUM, False)

    def test_explicit_human_requirement_always_wins(self) -> None:
        for mode in Mode:
            for risk in RiskLevel:
                assert requires_approval(mode, risk, True) is True

    def test_mode_must_be_a_mode(self) -> None:
        with pytest.raises(ValueError, match="mode must be a Mode"):
            requires_approval("AUTO", RiskLevel.LOW, False)  # type: ignore[arg-type]

    def test_risk_must_be_a_risk_level(self) -> None:
        with pytest.raises(ValueError, match="risk must be a RiskLevel"):
            requires_approval(Mode.AUTO, "LOW", False)  # type: ignore[arg-type]

    def test_policy_is_pure(self) -> None:
        assert requires_approval(Mode.AUTO, RiskLevel.LOW, False) is False
        assert requires_approval(Mode.AUTO, RiskLevel.LOW, False) is False


class TestDecideReview:
    def test_done_verifies(self) -> None:
        outcome = decide_review(
            report(ReportStatus.DONE), attempt=1, max_attempts=3
        )
        assert outcome == ReviewOutcome(StepEvent.VERIFY, "report-done")

    def test_revise_requests_a_revision_while_attempts_remain(self) -> None:
        outcome = decide_review(
            report(ReportStatus.REVISE), attempt=2, max_attempts=3
        )
        assert outcome.event is StepEvent.REQUEST_REVISE
        assert outcome.reason == "report-revise"

    def test_revise_without_attempts_left_escalates_to_a_human(self) -> None:
        """REVISE has no terminating exit, so an exhausted revise blocks."""
        outcome = decide_review(
            report(ReportStatus.REVISE), attempt=3, max_attempts=3
        )
        assert outcome == ReviewOutcome(
            StepEvent.BLOCK, "revise-attempts-exhausted"
        )

    def test_blocked_blocks(self) -> None:
        outcome = decide_review(
            report(ReportStatus.BLOCKED), attempt=1, max_attempts=3
        )
        assert outcome == ReviewOutcome(StepEvent.BLOCK, "report-blocked")

    def test_failed_is_retried_while_attempts_remain(self) -> None:
        outcome = decide_review(
            report(ReportStatus.FAILED), attempt=1, max_attempts=3
        )
        assert outcome.event is StepEvent.REQUEST_REVISE
        assert outcome.reason == "report-failed-retry"

    def test_failed_becomes_a_human_decision_once_exhausted(self) -> None:
        outcome = decide_review(
            report(ReportStatus.FAILED), attempt=3, max_attempts=3
        )
        assert outcome == ReviewOutcome(
            StepEvent.BLOCK, "failed-attempts-exhausted"
        )

    def test_every_outcome_is_legal_from_reviewing(self) -> None:
        """The policy may never invent a transition the FSM does not have."""
        allowed = StepStateMachine(initial_state=StepState.REVIEWING).allowed_events()
        for status in ReportStatus:
            for issues in ((), ("a problem",)):
                for questions in ((), ("a question?",)):
                    for attempt in (1, 3):
                        outcome = decide_review(
                            report(
                                status, issues=issues, questions=questions
                            ),
                            attempt=attempt,
                            max_attempts=3,
                        )
                        assert outcome.event in allowed, (status, attempt)

    def test_architecture_questions_always_block(self) -> None:
        for status in ReportStatus:
            outcome = decide_review(
                report(status, questions=("do we need a new layer?",)),
                attempt=1,
                max_attempts=3,
            )
            assert outcome == ReviewOutcome(
                StepEvent.BLOCK, "architecture-questions"
            )

    def test_unresolved_issues_always_block(self) -> None:
        for status in ReportStatus:
            outcome = decide_review(
                report(status, issues=("the build is red",)),
                attempt=1,
                max_attempts=3,
            )
            assert outcome == ReviewOutcome(
                StepEvent.BLOCK, "unresolved-issues"
            )

    def test_architecture_questions_take_precedence_over_issues(self) -> None:
        outcome = decide_review(
            report(
                ReportStatus.DONE,
                issues=("minor",),
                questions=("architecture?",),
            ),
            attempt=1,
            max_attempts=3,
        )
        assert outcome.reason == "architecture-questions"

    def test_decision_is_pure_and_repeatable(self) -> None:
        value = report(ReportStatus.REVISE)
        first = decide_review(value, attempt=1, max_attempts=3)
        second = decide_review(value, attempt=1, max_attempts=3)
        assert first == second

    def test_outcome_is_json_safe(self) -> None:
        outcome = decide_review(
            report(ReportStatus.DONE), attempt=1, max_attempts=3
        )
        assert outcome.to_dict() == {
            "event": "VERIFY",
            "reason": "report-done",
        }

    def test_report_must_be_a_worker_result(self) -> None:
        with pytest.raises(ValueError, match="report must be a WorkerResult"):
            decide_review("DONE", attempt=1, max_attempts=3)  # type: ignore[arg-type]

    def test_attempt_must_be_non_negative(self) -> None:
        with pytest.raises(ValueError, match="attempt must be an int"):
            decide_review(
                report(ReportStatus.DONE), attempt=-1, max_attempts=3
            )

    def test_max_attempts_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="max_attempts must be an int"):
            decide_review(
                report(ReportStatus.DONE), attempt=1, max_attempts=0
            )

    def test_attempt_boundary_is_exclusive(self) -> None:
        """``attempt == max_attempts`` is already the last attempt."""
        assert (
            decide_review(
                report(ReportStatus.REVISE), attempt=2, max_attempts=3
            ).event
            is StepEvent.REQUEST_REVISE
        )
        assert (
            decide_review(
                report(ReportStatus.REVISE), attempt=3, max_attempts=3
            ).event
            is StepEvent.BLOCK
        )
