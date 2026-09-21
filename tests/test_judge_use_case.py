"""Tests for the Step 16 judge use-case.

The use-case exists to make the judge *narrow*: consulted only for an explicit
evidence conflict, exactly once per conflict, through an injected ``JudgePort``
that the application layer never learns the identity of. These tests pin the
conflict mapping, the call count, the provider-neutral boundary, and the fact
that the judge's decision is a separate layer that never replaces the
authoritative deterministic one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

from architecture_assistant.application import (
    AdvisorObservation,
    EvidenceRelation,
    EvidenceView,
    JudgeError,
    JudgeJudgment,
    JudgeUseCase,
    ObservationStatus,
    build_judge_conflict,
    decide_from_observations,
    merge_evidence,
    synthesize_decision,
)
from architecture_assistant.application.evidence_merger import EvidenceConflict
from architecture_assistant.architecture import (
    ARCHITECTURE_CURRENT,
    ROOT_PACKAGE,
    ArchitectureValidator,
    layer_of,
    scan_directory,
)
from architecture_assistant.domain.enums import DecisionStatus, Severity
from architecture_assistant.domain.models import Decision, Finding
from architecture_assistant.ports.capabilities import JudgeConflict, JudgePort

TARGET = "forbidden-layer-import"
OTHER_TARGET = "unknown-layer"
SUPPORTING_ID = "finding-openai-16-supporting"
CONTRADICTING_ID = "finding-claude-16-contradicting"
OTHER_SUPPORTING_ID = "finding-grok-16-supporting"
OTHER_CONTRADICTING_ID = "finding-openai-16-contradicting"
STEP = 16
FIXED = datetime(2026, 9, 21, 14, 0, tzinfo=timezone.utc)


def fixed_clock() -> datetime:
    return FIXED


def make_finding(
    *,
    source: str = "openai",
    claim: str = "a claim",
    finding_id: str = SUPPORTING_ID,
    step_no: int | None = STEP,
) -> Finding:
    return Finding(
        id=finding_id,
        source=source,
        claim=claim,
        evidence=("application/context.py:12",),
        confidence=0.5,
        severity=Severity.MEDIUM,
        step_no=step_no,
    )


def gate_decision(
    status: DecisionStatus = DecisionStatus.ACCEPTED,
    *,
    rules: tuple[str, ...] = (TARGET, OTHER_TARGET),
    refs: tuple[str, ...] = (),
) -> Decision:
    return Decision(
        id="realization-step-016-attempt-001",
        status=status,
        decision="compliant" if status is DecisionStatus.ACCEPTED else "violates",
        rationale="fresh deterministic validation of step 016 attempt 001",
        rules_applied=rules,
        evidence_refs=refs,
        perspectives=(),
        step_no=STEP,
    )


def _finding_observation(
    finding: Finding, relation: EvidenceRelation, target: str
) -> AdvisorObservation:
    return AdvisorObservation(
        source=finding.source,
        status=ObservationStatus.FINDING,
        finding=finding,
        relation=relation,
        relation_target=target,
    )


def conflicted_view(
    status: DecisionStatus = DecisionStatus.ACCEPTED,
    *,
    step_no: int = STEP,
) -> tuple[Decision, EvidenceView]:
    """A real merged view carrying exactly one validated conflict."""
    gate = gate_decision(status)
    supporting = make_finding(
        source="openai", claim="agrees", finding_id=SUPPORTING_ID, step_no=step_no
    )
    contradicting = make_finding(
        source="claude",
        claim="disagrees",
        finding_id=CONTRADICTING_ID,
        step_no=step_no,
    )
    view = merge_evidence(
        [
            _finding_observation(
                supporting, EvidenceRelation.SUPPORTING, TARGET
            ),
            _finding_observation(
                contradicting, EvidenceRelation.CONTRADICTING, TARGET
            ),
        ],
        gate=gate,
    )
    return gate, view


def two_conflict_view() -> tuple[Decision, EvidenceView]:
    """A merged view carrying two independent conflicts."""
    gate = gate_decision()
    pairs = (
        (SUPPORTING_ID, CONTRADICTING_ID, TARGET),
        (OTHER_SUPPORTING_ID, OTHER_CONTRADICTING_ID, OTHER_TARGET),
    )
    observations = []
    for index, (supporting_id, contradicting_id, target) in enumerate(pairs):
        observations.append(
            _finding_observation(
                make_finding(
                    source="openai",
                    claim=f"agrees {index}",
                    finding_id=supporting_id,
                ),
                EvidenceRelation.SUPPORTING,
                target,
            )
        )
        observations.append(
            _finding_observation(
                make_finding(
                    source="claude",
                    claim=f"disagrees {index}",
                    finding_id=contradicting_id,
                ),
                EvidenceRelation.CONTRADICTING,
                target,
            )
        )
    return gate, merge_evidence(observations, gate=gate)


def quiet_view() -> EvidenceView:
    """A view with findings but no conflict at all."""
    return merge_evidence(
        [
            _finding_observation(
                make_finding(claim="just a worry"),
                EvidenceRelation.UNRESOLVED,
                TARGET,
            )
        ]
    )


@dataclass
class SpyJudge:
    """A ``JudgePort`` stand-in that records every call it receives."""

    status: DecisionStatus = DecisionStatus.ACCEPTED
    calls: list = field(default_factory=list)

    def judge(self, conflict: JudgeConflict) -> Decision:
        self.calls.append(conflict)
        target = str(dict(conflict.context).get("target", ""))
        return Decision(
            id=f"spy-judge-{target}",
            status=self.status,
            decision="spy verdict",
            rationale="a stand-in judge resolved it",
            step_no=STEP,
        )


class TestBuildJudgeConflict:
    """The judge receives the conflict's findings and nothing else."""

    def test_it_carries_only_the_findings_the_conflict_names(self) -> None:
        _, view = conflicted_view()
        conflict = view.conflicts[0]

        judge_conflict = build_judge_conflict(conflict, view)

        assert isinstance(judge_conflict, JudgeConflict)
        assert [finding.id for finding in judge_conflict.findings] == [
            SUPPORTING_ID,
            CONTRADICTING_ID,
        ]

    def test_the_question_is_the_deterministic_conflict_question(self) -> None:
        _, view = conflicted_view()
        conflict = view.conflicts[0]

        judge_conflict = build_judge_conflict(conflict, view)

        assert judge_conflict.question == view.unresolved_questions[0]
        assert TARGET in judge_conflict.question

    def test_the_context_names_the_target_and_both_sides(self) -> None:
        _, view = conflicted_view()
        conflict = view.conflicts[0]

        context = dict(build_judge_conflict(conflict, view).context)

        assert context["target"] == TARGET
        assert context["supporting_ids"] == [SUPPORTING_ID]
        assert context["contradicting_ids"] == [CONTRADICTING_ID]
        assert context["gate_status"] == DecisionStatus.ACCEPTED.value

    def test_the_step_is_carried_through(self) -> None:
        _, view = conflicted_view()

        judge_conflict = build_judge_conflict(view.conflicts[0], view)

        assert {finding.step_no for finding in judge_conflict.findings} == {STEP}

    def test_a_conflict_outside_the_view_is_refused(self) -> None:
        _, view = conflicted_view()
        forged = EvidenceConflict(
            target="some-other-target",
            supporting_ids=(SUPPORTING_ID,),
            contradicting_ids=(CONTRADICTING_ID,),
        )

        with pytest.raises(JudgeError, match="not one of the merged conflicts"):
            build_judge_conflict(forged, view)

    def test_findings_from_different_steps_are_refused(self) -> None:
        mixed = EvidenceView(
            findings=(
                make_finding(finding_id=SUPPORTING_ID, step_no=16),
                make_finding(
                    source="claude", finding_id=CONTRADICTING_ID, step_no=17
                ),
            ),
            supporting_ids=(SUPPORTING_ID,),
            conflicting_ids=(CONTRADICTING_ID,),
            conflicts=(
                EvidenceConflict(
                    target=TARGET,
                    supporting_ids=(SUPPORTING_ID,),
                    contradicting_ids=(CONTRADICTING_ID,),
                ),
            ),
        )

        with pytest.raises(JudgeError, match="same step"):
            build_judge_conflict(mixed.conflicts[0], mixed)

    def test_the_types_are_validated(self) -> None:
        _, view = conflicted_view()

        with pytest.raises(JudgeError, match="EvidenceConflict"):
            build_judge_conflict("not a conflict", view)
        with pytest.raises(JudgeError, match="EvidenceView"):
            build_judge_conflict(view.conflicts[0], "not a view")

    def test_a_view_cannot_even_hold_a_conflict_with_unknown_ids(self) -> None:
        """Corruption is impossible by construction - the view refuses it."""
        with pytest.raises(ValueError, match="not in the view"):
            EvidenceView(
                findings=(make_finding(),),
                supporting_ids=("ghost-id",),
            )


class TestJudgeUseCase:
    """Consulted exactly once per conflict - and never otherwise."""

    def test_no_conflict_means_no_judge_call(self) -> None:
        judge = SpyJudge()

        run = JudgeUseCase(judge=judge).resolve(quiet_view())

        assert judge.calls == []
        assert run.count == 0
        assert run.judge_available is True
        assert run.judgments == ()

    def test_one_conflict_means_exactly_one_call(self) -> None:
        judge = SpyJudge()
        _, view = conflicted_view()

        run = JudgeUseCase(judge=judge).resolve(view)

        assert len(judge.calls) == 1
        assert run.count == 1
        assert judge.calls[0].question == view.unresolved_questions[0]

    def test_two_conflicts_mean_two_independent_calls(self) -> None:
        judge = SpyJudge()
        _, view = two_conflict_view()

        run = JudgeUseCase(judge=judge).resolve(view)

        assert len(view.conflicts) == 2
        assert len(judge.calls) == 2
        assert run.count == 2
        # never one combined mega-prompt: two separate conflicts, two findings each
        targets = [dict(call.context)["target"] for call in judge.calls]
        assert sorted(targets) == sorted([TARGET, OTHER_TARGET])
        assert all(len(call.findings) == 2 for call in judge.calls)

    def test_without_a_judge_it_is_explicitly_unavailable(self) -> None:
        use_case = JudgeUseCase()
        _, view = conflicted_view()

        run = use_case.resolve(view)

        assert use_case.is_available is False
        assert use_case.judge is None
        assert run.judge_available is False
        assert run.count == 0
        assert run.judgments == ()

    def test_a_judge_failure_is_kept_as_an_error_decision(self) -> None:
        judge = SpyJudge(status=DecisionStatus.ERROR)
        _, view = conflicted_view()

        run = JudgeUseCase(judge=judge).resolve(view)

        assert run.count == 1
        assert run.judgments[0].decision.status is DecisionStatus.ERROR

    def test_an_abstention_is_kept_as_an_abstention(self) -> None:
        judge = SpyJudge(status=DecisionStatus.ABSTAIN)
        _, view = conflicted_view()

        run = JudgeUseCase(judge=judge).resolve(view)

        assert run.judgments[0].decision.status is DecisionStatus.ABSTAIN

    def test_the_run_is_json_safe(self) -> None:
        _, view = conflicted_view()

        run = JudgeUseCase(judge=SpyJudge()).resolve(view)
        payload = run.to_dict()

        assert json.loads(json.dumps(payload)) == payload
        assert payload["count"] == 1
        assert payload["judgments"][0]["conflict"]["target"] == TARGET

    def test_a_non_view_is_rejected(self) -> None:
        with pytest.raises(JudgeError, match="EvidenceView"):
            JudgeUseCase(judge=SpyJudge()).resolve("not a view")

    def test_a_non_judge_is_rejected(self) -> None:
        with pytest.raises(JudgeError, match="JudgePort"):
            JudgeUseCase(judge="not a judge")

    def test_the_injected_port_is_the_one_used(self) -> None:
        judge = SpyJudge()
        use_case = JudgeUseCase(judge=judge)

        assert use_case.judge is judge
        assert isinstance(judge, JudgePort)

    def test_the_judgment_keeps_the_conflict_it_resolved(self) -> None:
        _, view = conflicted_view()

        run = JudgeUseCase(judge=SpyJudge()).resolve(view)

        judgment = run.judgments[0]
        assert isinstance(judgment, JudgeJudgment)
        assert judgment.conflict is view.conflicts[0]
        assert run.judgments == (
            JudgeJudgment(
                conflict=view.conflicts[0], decision=judgment.decision
            ),
        )


class TestSeparationFromTheDeterministicDecision:
    """The judge layer never replaces the authoritative gate decision."""

    @pytest.mark.parametrize(
        "gate_status", [DecisionStatus.ACCEPTED, DecisionStatus.REJECTED]
    )
    def test_a_judge_verdict_cannot_override_the_gate(
        self, gate_status
    ) -> None:
        judge_status = (
            DecisionStatus.REJECTED
            if gate_status is DecisionStatus.ACCEPTED
            else DecisionStatus.ACCEPTED
        )
        gate, view = conflicted_view(gate_status)

        result = synthesize_decision(view, gate=gate, step_no=STEP)
        run = JudgeUseCase(judge=SpyJudge(status=judge_status)).resolve(view)

        # the authoritative decision is exactly what the gate produced...
        assert result.decision.status is gate_status
        assert result.decision.id.startswith("advisor-decision-16-")
        # ...and the judge's opposite verdict is a separate layer
        judge_decision = run.judgments[0].decision
        assert judge_decision.status is judge_status
        assert judge_decision is not result.decision
        assert judge_decision.id.startswith("spy-judge-")
        assert result.decision.status is not judge_decision.status

    def test_the_run_carries_no_gate_decision(self) -> None:
        gate, view = conflicted_view()

        run = JudgeUseCase(judge=SpyJudge()).resolve(view)

        assert gate not in (judgment.decision for judgment in run.judgments)
        assert run.to_dict()["judgments"][0]["decision"]["rules_applied"] == []

    def test_the_deterministic_result_is_unchanged_by_a_judge_run(self) -> None:
        gate, view = conflicted_view()

        before = decide_from_observations(
            [], gate=gate, step_no=STEP, clock=fixed_clock
        )
        JudgeUseCase(judge=SpyJudge(status=DecisionStatus.REJECTED)).resolve(
            view
        )
        after = decide_from_observations(
            [], gate=gate, step_no=STEP, clock=fixed_clock
        )

        assert before.decision.status is DecisionStatus.ACCEPTED
        assert after == before


def _src_root() -> Path:
    return Path(__file__).resolve().parents[1] / "src" / "architecture_assistant"


class TestLayerBoundary:
    """The application layer stays provider-neutral."""

    def test_the_use_case_imports_the_port_only(self) -> None:
        source = scan_directory(_src_root())
        module = f"{ROOT_PACKAGE}.application.judge"
        layers = {
            layer_of(target)
            for target in source.imports_of(module)
            if target.startswith(ROOT_PACKAGE)
        }

        assert layers <= {"domain", "ports", "application"}

    def test_the_use_case_imports_the_port_contract_not_the_adapter(self) -> None:
        source = scan_directory(_src_root())
        module = f"{ROOT_PACKAGE}.application.judge"
        targets = source.imports_of(module)

        assert any("ports.capabilities" in target for target in targets)
        assert not any("openai_judge" in target for target in targets)
        assert not any("infrastructure" in target for target in targets)

    def test_the_use_case_never_names_a_provider_adapter(self) -> None:
        text = (_src_root() / "application" / "judge.py").read_text(
            encoding="utf-8"
        )

        for forbidden in (
            "OpenAIJudgeAdapter",
            "ClaudeJudgeAdapter",
            "OpenAIAdvisorAdapter",
            "OpenAIAdvisorError",
            "from ..infrastructure",
            "from ..architecture",
        ):
            assert forbidden not in text, forbidden

    def test_the_whole_tree_still_satisfies_the_current_baseline(self) -> None:
        source = scan_directory(_src_root())
        result = ArchitectureValidator().validate(source)

        assert result.is_compliant is True
        assert result.violations == ()
        assert result.baseline_version == ARCHITECTURE_CURRENT.version
