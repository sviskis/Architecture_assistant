"""Tests for the Step 15 decision engine.

The engine exists to *not* let advisors decide anything. These tests pin the
authority order (deterministic gate > everything), the no-gate outcomes
(``ABSTAIN`` / ``ERROR`` and never a fabricated ACCEPTED or REJECTED), the
meaning of ACCEPTED, the explainability contract, and the absence of any form of
voting.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from architecture_assistant.application import (
    AdvisorObservation,
    DecisionEngineError,
    DecisionResult,
    EvidenceRelation,
    EvidenceView,
    ObservationStatus,
    decide_from_observations,
    merge_evidence,
    synthesize_decision,
)
from architecture_assistant.architecture import (
    ARCHITECTURE_CURRENT,
    ROOT_PACKAGE,
    ArchitectureValidator,
    layer_of,
    scan_directory,
)
from architecture_assistant.domain.enums import DecisionStatus, Severity
from architecture_assistant.domain.models import Decision, Finding

FIXED = datetime(2026, 9, 21, 11, 0, tzinfo=timezone.utc)

RULES = (
    "unknown-layer",
    "forbidden-layer-import",
    "sqlite-outside-infrastructure",
)
ANCHOR = "forbidden-layer-import"
VIOLATION_ID = "finding-015-001-abc123abc123"


def fixed_clock() -> datetime:
    return FIXED


def make_finding(
    *,
    source: str = "openai",
    claim: str = "a risk",
    evidence: tuple[str, ...] = ("application/context.py:12",),
    step_no: int | None = 15,
    severity: Severity = Severity.MEDIUM,
    confidence: float = 0.5,
) -> Finding:
    digest = hashlib.sha1(
        "|".join((source, claim, *evidence, str(step_no))).encode("utf-8")
    ).hexdigest()[:12]
    return Finding(
        id=f"finding-{source}-{step_no}-{digest}",
        source=source,
        claim=claim,
        evidence=evidence,
        confidence=confidence,
        severity=severity,
        step_no=step_no,
    )


def observation(
    finding: Finding,
    *,
    relation: EvidenceRelation = EvidenceRelation.UNRESOLVED,
    target: str | None = None,
) -> AdvisorObservation:
    return AdvisorObservation(
        source=finding.source,
        status=ObservationStatus.FINDING,
        finding=finding,
        relation=relation,
        relation_target=target,
    )


def abstained(source: str = "claude") -> AdvisorObservation:
    return AdvisorObservation(
        source=source,
        status=ObservationStatus.ABSTAIN,
        reason="advisor abstained: ClaudeAdvisorAbstainError",
    )


def failed(source: str = "grok") -> AdvisorObservation:
    return AdvisorObservation(
        source=source,
        status=ObservationStatus.ERROR,
        reason="advisor failed: GrokAdvisorTimeoutError",
    )


def gate(
    status: DecisionStatus = DecisionStatus.ACCEPTED,
    *,
    rules: tuple[str, ...] = RULES,
    refs: tuple[str, ...] = (),
) -> Decision:
    return Decision(
        id="realization-step-015-attempt-001",
        status=status,
        decision="compliant" if status is DecisionStatus.ACCEPTED else "violates",
        rationale="fresh deterministic validation of step 015 attempt 001",
        rules_applied=rules,
        evidence_refs=refs,
        perspectives=(),
        step_no=15,
    )


class TestDeterministicAuthority:
    """The deterministic gate decides; the advisors only explain."""

    def test_a_rejected_gate_cannot_be_overridden_by_advisors(self) -> None:
        findings = [
            make_finding(source=name, claim=f"supporting {name}")
            for name in ("openai", "claude", "grok")
        ]

        result = decide_from_observations(
            [
                observation(
                    finding,
                    relation=EvidenceRelation.SUPPORTING,
                    target=ANCHOR,
                )
                for finding in findings
            ],
            gate=gate(DecisionStatus.REJECTED, refs=(VIOLATION_ID,)),
            step_no=15,
            clock=fixed_clock,
        )

        assert result.decision.status is DecisionStatus.REJECTED
        assert result.evidence.supporting_ids == tuple(
            finding.id for finding in findings
        )
        assert "could not change this outcome" in result.decision.rationale

    def test_a_rejected_gate_stays_rejected_without_any_finding(self) -> None:
        result = decide_from_observations(
            [],
            gate=gate(DecisionStatus.REJECTED, refs=(VIOLATION_ID,)),
            clock=fixed_clock,
        )

        assert result.decision.status is DecisionStatus.REJECTED
        assert result.decision.evidence_refs == (VIOLATION_ID,)

    def test_an_accepted_gate_stays_accepted_with_unresolved_concerns(
        self,
    ) -> None:
        risky = make_finding(
            claim="a serious unresolved worry", severity=Severity.CRITICAL
        )

        result = decide_from_observations(
            [observation(risky)], gate=gate(), step_no=15, clock=fixed_clock
        )

        assert result.decision.status is DecisionStatus.ACCEPTED
        assert result.evidence.unresolved_ids == (risky.id,)
        assert risky in result.evidence.findings

    def test_an_accepted_gate_stays_accepted_with_three_supporting_findings(
        self,
    ) -> None:
        findings = [
            make_finding(source=name, claim=f"looks fine {name}")
            for name in ("openai", "claude", "grok")
        ]

        result = decide_from_observations(
            [
                observation(
                    finding,
                    relation=EvidenceRelation.SUPPORTING,
                    target=ANCHOR,
                )
                for finding in findings
            ],
            gate=gate(),
            step_no=15,
            clock=fixed_clock,
        )

        assert result.decision.status is DecisionStatus.ACCEPTED
        assert result.evidence.supporting_ids == tuple(
            finding.id for finding in findings
        )

    def test_a_contradicting_finding_cannot_flip_an_accepted_gate(self) -> None:
        dissenter = make_finding(source="grok", claim="I disagree")

        result = decide_from_observations(
            [
                observation(
                    dissenter,
                    relation=EvidenceRelation.CONTRADICTING,
                    target=ANCHOR,
                )
            ],
            gate=gate(),
            step_no=15,
            clock=fixed_clock,
        )

        assert result.decision.status is DecisionStatus.ACCEPTED
        assert result.evidence.conflicting_ids == (dissenter.id,)

    def test_the_gate_rules_are_carried_onto_the_decision(self) -> None:
        result = decide_from_observations([], gate=gate(), clock=fixed_clock)

        assert result.decision.rules_applied == RULES

    def test_the_decision_carries_the_step_and_the_injected_clock(self) -> None:
        result = decide_from_observations(
            [], gate=gate(), step_no=15, clock=fixed_clock
        )

        assert result.decision.step_no == 15
        assert result.decision.created_at == FIXED


class TestNoGate:
    """With no deterministic verdict the engine invents nothing."""

    def test_no_gate_with_partial_findings_is_an_abstention(self) -> None:
        finding = make_finding()

        result = decide_from_observations(
            [observation(finding), failed()], step_no=15, clock=fixed_clock
        )

        assert result.decision.status is DecisionStatus.ABSTAIN
        assert result.evidence.findings == (finding,)
        assert len(result.evidence.errors) == 1

    def test_no_gate_and_every_provider_failed_is_an_error(self) -> None:
        result = decide_from_observations(
            [failed("grok"), failed("openai")], clock=fixed_clock
        )

        assert result.decision.status is DecisionStatus.ERROR
        assert result.evidence.findings == ()
        assert len(result.evidence.errors) == 2

    def test_no_gate_with_no_observations_is_an_abstention(self) -> None:
        result = decide_from_observations([], clock=fixed_clock)

        assert result.decision.status is DecisionStatus.ABSTAIN

    def test_no_gate_with_only_abstentions_is_an_abstention(self) -> None:
        result = decide_from_observations(
            [abstained("claude"), abstained("grok")], clock=fixed_clock
        )

        assert result.decision.status is DecisionStatus.ABSTAIN

    def test_one_failed_provider_never_blocks_the_others(self) -> None:
        finding = make_finding(source="openai")

        result = decide_from_observations(
            [observation(finding), failed("grok"), abstained("claude")],
            clock=fixed_clock,
        )

        assert result.decision.status is DecisionStatus.ABSTAIN
        assert result.evidence.findings == (finding,)
        assert result.evidence.sources == ("openai",)
        assert result.decision.perspectives == ("openai",)

    def test_three_supporting_findings_without_a_gate_are_not_accepted(
        self,
    ) -> None:
        findings = [
            make_finding(source=name, claim=f"agreeing {name}")
            for name in ("openai", "claude", "grok")
        ]

        result = decide_from_observations(
            [observation(finding) for finding in findings],
            step_no=15,
            clock=fixed_clock,
        )

        assert result.decision.status is DecisionStatus.ABSTAIN
        assert result.evidence.supporting_ids == ()
        assert result.evidence.unresolved_ids == tuple(
            finding.id for finding in findings
        )

    def test_an_abstention_is_not_a_rejection(self) -> None:
        result = decide_from_observations([abstained()], clock=fixed_clock)

        assert result.decision.status is not DecisionStatus.REJECTED
        assert "not a rejection" in result.decision.rationale

    def test_an_error_is_not_a_rejection(self) -> None:
        result = decide_from_observations([failed()], clock=fixed_clock)

        assert result.decision.status is not DecisionStatus.REJECTED
        assert "is not a rejection" in result.decision.rationale


class TestExplainability:
    """The decision and the whole evidence view travel together."""

    def test_accepted_is_scoped_to_the_deterministic_gate(self) -> None:
        result = decide_from_observations(
            [observation(make_finding())], gate=gate(), clock=fixed_clock
        )

        rationale = result.decision.rationale
        assert "deterministic architecture gate accepted" in rationale
        assert "does not mean the design is correct" in rationale
        assert "AI advisors approve" in rationale
        assert "carries no risks" in rationale
        assert "never a vote" in rationale

    def test_the_outcome_phrase_names_the_deterministic_checks(self) -> None:
        result = decide_from_observations([], gate=gate(), clock=fixed_clock)

        assert "deterministic architecture checks" in result.decision.decision
        assert result.decision.decision.startswith("accepted:")

    def test_the_rejected_outcome_and_rationale_name_the_rules(self) -> None:
        result = decide_from_observations(
            [],
            gate=gate(DecisionStatus.REJECTED, refs=(VIOLATION_ID,)),
            clock=fixed_clock,
        )

        assert result.decision.decision.startswith("rejected:")
        for rule in RULES:
            assert rule in result.decision.rationale

    def test_the_result_exposes_the_whole_evidence_view(self) -> None:
        finding = make_finding()

        result = decide_from_observations(
            [observation(finding), abstained(), failed()],
            gate=gate(),
            clock=fixed_clock,
        )

        assert isinstance(result, DecisionResult)
        assert isinstance(result.evidence, EvidenceView)
        assert result.evidence.findings == (finding,)
        assert len(result.evidence.abstained) == 1
        assert len(result.evidence.errors) == 1

    def test_evidence_refs_hold_authoritative_and_supporting_refs(self) -> None:
        supporting = make_finding(claim="agrees")
        unresolved = make_finding(claim="unclear", source="claude")

        result = decide_from_observations(
            [
                observation(
                    supporting,
                    relation=EvidenceRelation.SUPPORTING,
                    target=ANCHOR,
                ),
                observation(unresolved),
            ],
            gate=gate(DecisionStatus.REJECTED, refs=(VIOLATION_ID,)),
            clock=fixed_clock,
        )

        assert result.decision.evidence_refs == (VIOLATION_ID, supporting.id)
        assert unresolved.id not in result.decision.evidence_refs
        # an unresolved concern is never hidden, it is just not claimed as backing
        assert unresolved in result.evidence.findings
        assert result.evidence.unresolved_ids == (unresolved.id,)

    def test_perspectives_are_the_providers_that_gave_a_finding(self) -> None:
        findings = [
            make_finding(source=name, claim=f"a claim from {name}")
            for name in ("openai", "grok")
        ]

        result = decide_from_observations(
            [observation(finding) for finding in findings] + [abstained("claude")],
            gate=gate(),
            clock=fixed_clock,
        )

        assert result.decision.perspectives == ("grok", "openai")

    def test_a_validated_conflict_is_surfaced_with_its_question(self) -> None:
        supporting = make_finding(claim="agrees")
        contradicting = make_finding(claim="disagrees", source="claude")

        result = decide_from_observations(
            [
                observation(
                    supporting,
                    relation=EvidenceRelation.SUPPORTING,
                    target=ANCHOR,
                ),
                observation(
                    contradicting,
                    relation=EvidenceRelation.CONTRADICTING,
                    target=ANCHOR,
                ),
            ],
            gate=gate(),
            clock=fixed_clock,
        )

        assert len(result.evidence.conflicts) == 1
        assert len(result.evidence.unresolved_questions) == 1
        assert ANCHOR in result.evidence.unresolved_questions[0]
        # and the deterministic outcome is still untouched by the conflict
        assert result.decision.status is DecisionStatus.ACCEPTED

    def test_the_result_is_json_safe(self) -> None:
        result = decide_from_observations(
            [observation(make_finding())], gate=gate(), clock=fixed_clock
        )

        payload = result.to_dict()

        assert json.loads(json.dumps(payload)) == payload
        assert payload["decision"]["status"] == DecisionStatus.ACCEPTED.value
        assert payload["evidence"]["unresolved_ids"]
        assert payload["evidence"]["findings"][0]["source"] == "openai"


class TestNoVoting:
    """Counts and confidences are described, never counted into the outcome."""

    def test_the_number_of_findings_never_changes_the_status(self) -> None:
        verdict = gate()

        for count in (1, 2, 3, 7):
            findings = [
                make_finding(claim=f"claim number {index}")
                for index in range(count)
            ]
            result = decide_from_observations(
                [observation(finding) for finding in findings],
                gate=verdict,
                clock=fixed_clock,
            )

            assert result.decision.status is DecisionStatus.ACCEPTED

    def test_a_unanimous_advisor_block_cannot_create_a_verdict(self) -> None:
        findings = [
            make_finding(claim=f"unanimous claim {index}") for index in range(3)
        ]

        result = decide_from_observations(
            [observation(finding) for finding in findings], clock=fixed_clock
        )

        assert result.decision.status is DecisionStatus.ABSTAIN

    def test_high_confidence_findings_do_not_outweigh_a_rejection(self) -> None:
        certain = make_finding(claim="certain it is fine", confidence=1.0)

        result = decide_from_observations(
            [
                observation(
                    certain,
                    relation=EvidenceRelation.CONTRADICTING,
                    target=ANCHOR,
                )
            ],
            gate=gate(DecisionStatus.REJECTED, refs=(VIOLATION_ID,)),
            clock=fixed_clock,
        )

        assert result.decision.status is DecisionStatus.REJECTED

    def test_the_rationale_reports_the_tally_without_letting_it_decide(
        self,
    ) -> None:
        findings = [
            make_finding(claim=f"counted claim {index}") for index in range(3)
        ]

        result = decide_from_observations(
            [observation(finding) for finding in findings], clock=fixed_clock
        )

        assert "3 unresolved" in result.decision.rationale
        assert result.decision.status is DecisionStatus.ABSTAIN

    def test_provider_identity_is_never_a_ranking(self) -> None:
        openai_only = decide_from_observations(
            [observation(make_finding(source="openai"))], clock=fixed_clock
        )
        grok_only = decide_from_observations(
            [observation(make_finding(source="grok"))], clock=fixed_clock
        )

        assert openai_only.decision.status is grok_only.decision.status


class TestInputValidation:
    """Inconsistent input is refused rather than quietly reinterpreted."""

    def test_a_non_view_is_rejected(self) -> None:
        with pytest.raises(DecisionEngineError, match="EvidenceView"):
            synthesize_decision("merged evidence")

    def test_a_non_decision_gate_is_rejected(self) -> None:
        with pytest.raises(DecisionEngineError, match="must be a Decision"):
            synthesize_decision(merge_evidence([]), gate="ACCEPTED")

    def test_an_unsettled_gate_is_rejected(self) -> None:
        unsettled = Decision(id="D-1", status=DecisionStatus.PENDING)

        with pytest.raises(DecisionEngineError, match="ACCEPTED or REJECTED"):
            synthesize_decision(merge_evidence([]), gate=unsettled)

    def test_a_gate_missing_from_the_engine_call_is_rejected(self) -> None:
        evidence = merge_evidence([], gate=gate())

        with pytest.raises(DecisionEngineError, match="must be passed"):
            synthesize_decision(evidence)

    def test_a_gate_the_evidence_never_saw_is_rejected(self) -> None:
        with pytest.raises(DecisionEngineError, match="merged without a gate"):
            synthesize_decision(merge_evidence([]), gate=gate())

    def test_a_gate_mismatch_is_rejected(self) -> None:
        evidence = merge_evidence([], gate=gate(DecisionStatus.ACCEPTED))

        with pytest.raises(DecisionEngineError, match="does not match"):
            synthesize_decision(
                evidence,
                gate=gate(DecisionStatus.REJECTED, refs=(VIOLATION_ID,)),
            )

    def test_a_bad_step_number_is_rejected(self) -> None:
        evidence = merge_evidence([])

        for bad in (0, -1, "15", True):
            with pytest.raises(DecisionEngineError, match="step_no"):
                synthesize_decision(evidence, step_no=bad)

    def test_a_blank_decision_id_is_rejected(self) -> None:
        with pytest.raises(DecisionEngineError, match="decision_id"):
            synthesize_decision(merge_evidence([]), decision_id="   ")

    def test_a_non_callable_clock_is_rejected(self) -> None:
        with pytest.raises(DecisionEngineError, match="clock"):
            synthesize_decision(merge_evidence([]), clock="now")

    def test_the_result_validates_its_parts(self) -> None:
        evidence = merge_evidence([])

        with pytest.raises(DecisionEngineError, match="decision must be"):
            DecisionResult(decision="go", evidence=evidence)

        with pytest.raises(DecisionEngineError, match="evidence must be"):
            DecisionResult(decision=Decision(id="D-1"), evidence="merged")

    def test_the_decision_id_is_deterministic_and_clock_independent(
        self,
    ) -> None:
        finding = make_finding()

        def later() -> datetime:
            return datetime(2030, 1, 1, tzinfo=timezone.utc)

        first = decide_from_observations(
            [observation(finding)], gate=gate(), step_no=15, clock=fixed_clock
        )
        second = decide_from_observations(
            [observation(finding)], gate=gate(), step_no=15, clock=later
        )

        assert first.decision.id == second.decision.id
        assert first.decision.id.startswith("advisor-decision-15-")

    def test_an_explicit_decision_id_is_used(self) -> None:
        result = decide_from_observations(
            [], gate=gate(), decision_id="advisory-015", clock=fixed_clock
        )

        assert result.decision.id == "advisory-015"


def _src_root() -> Path:
    return Path(__file__).resolve().parents[1] / "src" / "architecture_assistant"


class TestLayerBoundary:
    """The engine is application-layer and pure - infrastructure is out of reach."""

    def test_the_engine_never_reaches_outwards(self) -> None:
        source = scan_directory(_src_root())
        module = f"{ROOT_PACKAGE}.application.decision_engine"
        layers = {
            layer_of(target)
            for target in source.imports_of(module)
            if target.startswith(ROOT_PACKAGE)
        }

        assert layers <= {"domain", "application"}

    def test_the_engine_never_names_a_provider_error(self) -> None:
        text = (_src_root() / "application" / "decision_engine.py").read_text(
            encoding="utf-8"
        )

        for forbidden in (
            "OpenAIAdvisorError",
            "ClaudeAdvisorError",
            "GrokAdvisorError",
            "AdvisorAbstainError",
        ):
            assert forbidden not in text, forbidden

    def test_the_engine_imports_no_infrastructure_module(self) -> None:
        source = scan_directory(_src_root())
        targets = source.imports_of(
            f"{ROOT_PACKAGE}.application.decision_engine"
        )

        for target in targets:
            assert "infrastructure" not in target
            assert "composition" not in target

    def test_the_whole_tree_still_satisfies_the_current_baseline(self) -> None:
        source = scan_directory(_src_root())
        result = ArchitectureValidator().validate(source)

        assert result.is_compliant is True
        assert result.violations == ()
        assert result.baseline_version == ARCHITECTURE_CURRENT.version
