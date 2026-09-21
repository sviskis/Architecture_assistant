"""Tests for the Step 15 evidence merger.

The merger must be *conservative*: it never infers agreement or disagreement from
claim text, severity, confidence, provider identity or locator overlap, it never
drops a finding, and it never turns an unknown relation into a confident one.
These tests pin exactly that, plus the two integrity rules (a repeated delivery
collapses, a reused id with different content fails closed).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from architecture_assistant.application import (
    AdvisorObservation,
    EvidenceConflict,
    EvidenceIdentityCollisionError,
    EvidenceMergerError,
    EvidenceRelation,
    EvidenceView,
    ObservationOutcome,
    ObservationStatus,
    merge_evidence,
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

RULES = (
    "unknown-layer",
    "forbidden-layer-import",
    "sqlite-outside-infrastructure",
)

#: A deterministic anchor the tests can point a relation at.
ANCHOR = "forbidden-layer-import"
VIOLATION_ID = "finding-015-001-abc123abc123"


def make_finding(
    *,
    source: str = "openai",
    claim: str = "a risk",
    evidence: tuple[str, ...] = ("application/context.py:12",),
    finding_id: str | None = None,
    step_no: int | None = 15,
    severity: Severity = Severity.MEDIUM,
    confidence: float = 0.5,
) -> Finding:
    """A finding whose id follows the production scheme (content-addressed)."""
    digest = hashlib.sha1(
        "|".join((source, claim, *evidence, str(step_no))).encode("utf-8")
    ).hexdigest()[:12]
    return Finding(
        id=finding_id or f"finding-{source}-{step_no}-{digest}",
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


def abstained(
    source: str = "claude", reason: str = "advisor abstained: ClaudeAdvisorAbstainError"
) -> AdvisorObservation:
    return AdvisorObservation(
        source=source, status=ObservationStatus.ABSTAIN, reason=reason
    )


def failed(
    source: str = "grok", reason: str = "advisor failed: GrokAdvisorTimeoutError"
) -> AdvisorObservation:
    return AdvisorObservation(
        source=source, status=ObservationStatus.ERROR, reason=reason
    )


def gate(
    status: DecisionStatus = DecisionStatus.ACCEPTED,
    *,
    rules: tuple[str, ...] = RULES,
    refs: tuple[str, ...] = (),
    gate_id: str = "realization-step-015-attempt-001",
) -> Decision:
    """A settled realization-gate verdict; only ACCEPTED/REJECTED is legitimate."""
    return Decision(
        id=gate_id,
        status=status,
        decision="compliant" if status is DecisionStatus.ACCEPTED else "violates",
        rationale="fresh deterministic validation of step 015 attempt 001",
        rules_applied=rules,
        evidence_refs=refs,
        perspectives=(),
        step_no=15,
    )


class TestConservativeClassification:
    """Nothing is inferred - not from severity, confidence, text or provider."""

    def test_a_high_severity_finding_is_not_automatically_supporting(
        self,
    ) -> None:
        finding = make_finding(severity=Severity.CRITICAL)

        view = merge_evidence([observation(finding)], gate=gate())

        assert view.supporting_ids == ()
        assert view.unresolved_ids == (finding.id,)

    def test_a_low_severity_finding_is_not_automatically_contradicting(
        self,
    ) -> None:
        finding = make_finding(severity=Severity.LOW)

        view = merge_evidence(
            [observation(finding)], gate=gate(DecisionStatus.REJECTED)
        )

        assert view.conflicting_ids == ()
        assert view.unresolved_ids == (finding.id,)

    def test_a_shared_locator_does_not_imply_agreement(self) -> None:
        shared = ("application/context.py:12",)
        first = make_finding(source="openai", claim="one", evidence=shared)
        second = make_finding(source="claude", claim="two", evidence=shared)

        view = merge_evidence(
            [observation(first), observation(second)], gate=gate()
        )

        assert view.supporting_ids == ()
        assert view.unresolved_ids == (first.id, second.id)
        assert view.conflicts == ()

    def test_a_shared_locator_does_not_imply_contradiction(self) -> None:
        shared = ("application/context.py:12",)
        first = make_finding(source="openai", claim="yes", evidence=shared)
        second = make_finding(source="claude", claim="no", evidence=shared)

        view = merge_evidence(
            [observation(first), observation(second)],
            gate=gate(DecisionStatus.REJECTED),
        )

        assert view.conflicts == ()
        assert view.conflicting_ids == ()
        assert view.unresolved_ids == (first.id, second.id)

    def test_a_missing_relation_stays_unresolved(self) -> None:
        finding = make_finding()

        view = merge_evidence([observation(finding)], gate=gate())

        assert view.unresolved_ids == (finding.id,)
        assert view.findings == (finding,)

    def test_provider_identity_does_not_decide_a_relation(self) -> None:
        findings = [
            make_finding(source=name, claim=f"claim of {name}")
            for name in ("openai", "claude", "grok")
        ]

        view = merge_evidence(
            [observation(finding) for finding in findings], gate=gate()
        )

        assert view.supporting_ids == ()
        assert view.conflicting_ids == ()
        assert view.unresolved_ids == tuple(finding.id for finding in findings)

    def test_confidence_does_not_decide_a_relation(self) -> None:
        certain = make_finding(claim="certain", confidence=1.0)
        unsure = make_finding(claim="unsure", confidence=0.0)

        view = merge_evidence(
            [observation(certain), observation(unsure)], gate=gate()
        )

        assert view.supporting_ids == ()
        assert view.unresolved_ids == (certain.id, unsure.id)

    def test_an_unprovable_relation_degrades_to_unresolved(self) -> None:
        finding = make_finding()

        view = merge_evidence(
            [
                observation(
                    finding,
                    relation=EvidenceRelation.SUPPORTING,
                    target="not-an-anchor-at-all",
                )
            ],
            gate=gate(),
        )

        assert view.supporting_ids == ()
        assert view.unresolved_ids == (finding.id,)

    def test_no_gate_means_no_relation_is_honoured(self) -> None:
        finding = make_finding()

        view = merge_evidence(
            [
                observation(
                    finding,
                    relation=EvidenceRelation.SUPPORTING,
                    target=ANCHOR,
                )
            ]
        )

        assert view.supporting_ids == ()
        assert view.unresolved_ids == (finding.id,)
        assert view.gate_status is None
        assert view.gate_anchors == ()

    def test_a_validated_relation_is_honoured(self) -> None:
        finding = make_finding()

        view = merge_evidence(
            [
                observation(
                    finding,
                    relation=EvidenceRelation.SUPPORTING,
                    target=ANCHOR,
                )
            ],
            gate=gate(),
        )

        assert view.supporting_ids == (finding.id,)
        assert view.unresolved_ids == ()

    def test_a_violation_id_is_also_a_valid_anchor(self) -> None:
        finding = make_finding()

        view = merge_evidence(
            [
                observation(
                    finding,
                    relation=EvidenceRelation.CONTRADICTING,
                    target=VIOLATION_ID,
                )
            ],
            gate=gate(DecisionStatus.REJECTED, refs=(VIOLATION_ID,)),
        )

        assert view.conflicting_ids == (finding.id,)
        assert view.unresolved_ids == ()


class TestDeduplication:
    """An id identifies one finding - and a collision is corruption, not a merge."""

    def test_a_repeated_delivery_of_the_same_finding_collapses(self) -> None:
        finding = make_finding()

        view = merge_evidence(
            [observation(finding), observation(finding)], gate=gate()
        )

        assert view.findings == (finding,)
        assert view.unresolved_ids == (finding.id,)

    def test_a_repeated_delivery_with_another_certainty_still_collapses(
        self,
    ) -> None:
        """Confidence is not part of the id, so this is the same finding."""
        first = make_finding(finding_id="finding-openai-15-fixed", confidence=0.2)
        second = make_finding(
            finding_id="finding-openai-15-fixed", confidence=0.9
        )

        view = merge_evidence(
            [observation(first), observation(second)], gate=gate()
        )

        assert view.findings == (first,)

    def test_the_same_id_with_different_content_fails_closed(self) -> None:
        first = make_finding(finding_id="finding-openai-15-fixed", claim="one")
        second = make_finding(finding_id="finding-openai-15-fixed", claim="two")

        with pytest.raises(EvidenceIdentityCollisionError):
            merge_evidence([observation(first), observation(second)], gate=gate())

    def test_the_same_id_with_other_evidence_fails_closed(self) -> None:
        first = make_finding(
            finding_id="finding-openai-15-fixed", evidence=("a:1",)
        )
        second = make_finding(
            finding_id="finding-openai-15-fixed", evidence=("b:2",)
        )

        with pytest.raises(EvidenceIdentityCollisionError):
            merge_evidence([observation(first), observation(second)], gate=gate())

    def test_a_collision_error_is_also_a_merger_error(self) -> None:
        assert issubclass(EvidenceIdentityCollisionError, EvidenceMergerError)

    def test_similar_findings_from_two_providers_stay_separate(self) -> None:
        openai = make_finding(source="openai", claim="the same worry")
        claude = make_finding(source="claude", claim="the same worry")

        view = merge_evidence(
            [observation(openai), observation(claude)], gate=gate()
        )

        assert openai.id != claude.id
        assert view.findings == (openai, claude)
        assert view.sources == ("claude", "openai")

    def test_identical_claims_from_two_providers_are_not_merged(self) -> None:
        """The id contains the provider, so two providers never collapse."""
        openai = make_finding(source="openai", claim="identical")
        grok = make_finding(source="grok", claim="identical")

        view = merge_evidence([observation(openai), observation(grok)], gate=gate())

        assert len(view.findings) == 2
        assert view.unresolved_ids == (openai.id, grok.id)


class TestConflicts:
    """A conflict needs explicit, validated, incompatible relations."""

    def test_two_unresolved_findings_never_conflict(self) -> None:
        first = make_finding(source="openai", claim="alpha")
        second = make_finding(source="claude", claim="omega")

        view = merge_evidence(
            [observation(first), observation(second)],
            gate=gate(DecisionStatus.REJECTED),
        )

        assert view.conflicts == ()
        assert view.unresolved_questions == ()

    def test_opposing_validated_relations_create_a_conflict(self) -> None:
        supporting = make_finding(source="openai", claim="agrees")
        contradicting = make_finding(source="claude", claim="disagrees")

        view = merge_evidence(
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
        )

        assert view.has_conflicts is True
        assert view.conflicts == (
            EvidenceConflict(
                target=ANCHOR,
                supporting_ids=(supporting.id,),
                contradicting_ids=(contradicting.id,),
            ),
        )
        assert view.supporting_ids == (supporting.id,)
        assert view.conflicting_ids == (contradicting.id,)

    def test_a_conflict_needs_one_relation_on_each_side(self) -> None:
        supporters = [make_finding(claim=f"yes {index}") for index in range(2)]
        contradictor = make_finding(claim="no")

        view = merge_evidence(
            [
                observation(
                    finding,
                    relation=EvidenceRelation.SUPPORTING,
                    target=ANCHOR,
                )
                for finding in supporters
            ]
            + [
                observation(
                    contradictor,
                    relation=EvidenceRelation.CONTRADICTING,
                    target=ANCHOR,
                )
            ],
            gate=gate(),
        )

        assert len(view.conflicts) == 1
        assert view.conflicts[0].supporting_ids == tuple(
            finding.id for finding in supporters
        )
        assert view.conflicts[0].contradicting_ids == (contradictor.id,)

    def test_linguistic_difference_alone_is_not_a_conflict(self) -> None:
        first = make_finding(source="openai", claim="it is safe")
        second = make_finding(source="grok", claim="it is not safe")

        view = merge_evidence(
            [observation(first), observation(second)], gate=gate()
        )

        assert view.conflicts == ()
        assert view.unresolved_ids == (first.id, second.id)

    def test_a_conflict_needs_the_same_target(self) -> None:
        supporting = make_finding(claim="agrees")
        contradicting = make_finding(claim="disagrees", source="claude")

        view = merge_evidence(
            [
                observation(
                    supporting,
                    relation=EvidenceRelation.SUPPORTING,
                    target="unknown-layer",
                ),
                observation(
                    contradicting,
                    relation=EvidenceRelation.CONTRADICTING,
                    target="forbidden-layer-import",
                ),
            ],
            gate=gate(),
        )

        assert view.conflicts == ()
        assert view.supporting_ids == (supporting.id,)
        assert view.conflicting_ids == (contradicting.id,)

    def test_a_conflict_produces_one_deterministic_question(self) -> None:
        supporting = make_finding(claim="agrees")
        contradicting = make_finding(claim="disagrees", source="claude")

        view = merge_evidence(
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
        )

        assert view.unresolved_questions == (
            f"Unresolved evidence conflict on {ANCHOR}: supporting "
            f"{supporting.id} vs contradicting {contradicting.id}",
        )

    def test_an_unvalidated_opposing_relation_creates_no_conflict(self) -> None:
        supporting = make_finding(claim="agrees")
        contradicting = make_finding(claim="disagrees", source="claude")

        view = merge_evidence(
            [
                observation(
                    supporting,
                    relation=EvidenceRelation.SUPPORTING,
                    target=ANCHOR,
                ),
                observation(
                    contradicting,
                    relation=EvidenceRelation.CONTRADICTING,
                    target="not-an-anchor",
                ),
            ],
            gate=gate(),
        )

        assert view.conflicts == ()
        assert view.supporting_ids == (supporting.id,)
        assert view.unresolved_ids == (contradicting.id,)


class TestLosslessness:
    """Nothing is dropped, reworded or hidden."""

    def test_every_finding_is_preserved_with_its_source(self) -> None:
        findings = [
            make_finding(source=name, claim=f"claim of {name}")
            for name in ("openai", "claude", "grok")
        ]

        view = merge_evidence(
            [observation(finding) for finding in findings], gate=gate()
        )

        assert view.findings == tuple(findings)
        assert view.sources == ("claude", "grok", "openai")
        for original, kept in zip(findings, view.findings):
            assert kept is original
            assert kept.source == original.source

    def test_abstentions_and_failures_are_kept_apart(self) -> None:
        finding = make_finding()
        view = merge_evidence(
            [observation(finding), abstained(), failed()], gate=gate()
        )

        assert view.abstained == (
            ObservationOutcome(
                source="claude",
                reason="advisor abstained: ClaudeAdvisorAbstainError",
            ),
        )
        assert view.errors == (
            ObservationOutcome(
                source="grok", reason="advisor failed: GrokAdvisorTimeoutError"
            ),
        )
        assert view.findings == (finding,)

    def test_the_id_groups_partition_the_findings(self) -> None:
        supporting = make_finding(claim="agrees")
        contradicting = make_finding(claim="disagrees", source="claude")
        unresolved = make_finding(claim="who knows", source="grok")

        view = merge_evidence(
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
                observation(unresolved),
            ],
            gate=gate(),
        )

        assert set(view.finding_ids) == {
            supporting.id,
            contradicting.id,
            unresolved.id,
        }
        assert (
            view.supporting_ids + view.conflicting_ids + view.unresolved_ids
        ) == view.finding_ids
        assert view.unresolved_ids == (unresolved.id,)

    def test_unresolved_findings_are_never_hidden(self) -> None:
        finding = make_finding(claim="an unresolved worry")

        view = merge_evidence([observation(finding)], gate=gate())

        assert finding in view.findings
        assert finding.id in view.finding_ids
        assert finding.id in view.unresolved_ids

    def test_gate_status_and_anchors_are_recorded(self) -> None:
        view = merge_evidence([], gate=gate(DecisionStatus.REJECTED, refs=("f-1",)))

        assert view.gate_status is DecisionStatus.REJECTED
        assert view.gate_anchors == tuple(sorted((*RULES, "f-1")))

    def test_the_view_is_json_safe_and_deterministic(self) -> None:
        finding = make_finding()
        view = merge_evidence([observation(finding), abstained()], gate=gate())

        payload = view.to_dict()

        assert json.loads(json.dumps(payload)) == payload
        assert payload["gate_status"] == DecisionStatus.ACCEPTED.value
        assert payload["findings"][0]["id"] == finding.id
        assert payload["unresolved_ids"] == [finding.id]
        assert payload["abstained"][0]["source"] == "claude"

    def test_the_merge_is_repeatable(self) -> None:
        finding = make_finding()
        observations = [observation(finding), abstained()]

        first = merge_evidence(observations, gate=gate())
        second = merge_evidence(observations, gate=gate())

        assert first == second

    def test_an_empty_merge_is_valid(self) -> None:
        view = merge_evidence([])

        assert view.findings == ()
        assert view.finding_ids == ()
        assert view.gate_status is None
        assert view.to_dict()["findings"] == []


class TestInputValidation:
    """Bad or inconsistent input is refused - never reinterpreted."""

    def test_a_non_observation_is_rejected(self) -> None:
        with pytest.raises(EvidenceMergerError, match="AdvisorObservation"):
            merge_evidence([make_finding()])

    def test_a_bare_string_is_not_a_sequence_of_observations(self) -> None:
        with pytest.raises(EvidenceMergerError, match="sequence"):
            merge_evidence("openai")

    def test_a_finding_observation_needs_a_finding(self) -> None:
        with pytest.raises(EvidenceMergerError, match="must carry a Finding"):
            AdvisorObservation(
                source="openai", status=ObservationStatus.FINDING, reason="nope"
            )

    def test_a_finding_observation_needs_a_matching_source(self) -> None:
        finding = make_finding(source="openai")

        with pytest.raises(EvidenceMergerError, match="must match the finding"):
            AdvisorObservation(
                source="claude",
                status=ObservationStatus.FINDING,
                finding=finding,
            )

    def test_a_finding_observation_must_not_carry_a_reason(self) -> None:
        with pytest.raises(EvidenceMergerError, match="must not carry a reason"):
            AdvisorObservation(
                source="openai",
                status=ObservationStatus.FINDING,
                finding=make_finding(source="openai"),
                reason="because",
            )

    def test_a_declared_relation_needs_a_target(self) -> None:
        with pytest.raises(EvidenceMergerError, match="requires an explicit"):
            AdvisorObservation(
                source="openai",
                status=ObservationStatus.FINDING,
                finding=make_finding(source="openai"),
                relation=EvidenceRelation.SUPPORTING,
            )

    def test_an_abstention_needs_a_reason(self) -> None:
        with pytest.raises(EvidenceMergerError, match="reason"):
            AdvisorObservation(
                source="claude", status=ObservationStatus.ABSTAIN, reason=None
            )

    def test_a_failure_must_not_carry_a_finding(self) -> None:
        with pytest.raises(EvidenceMergerError, match="must not carry a Finding"):
            AdvisorObservation(
                source="grok",
                status=ObservationStatus.ERROR,
                finding=make_finding(source="grok"),
                reason="failed",
            )

    def test_an_abstention_cannot_declare_a_relation(self) -> None:
        with pytest.raises(EvidenceMergerError, match="cannot relate"):
            AdvisorObservation(
                source="claude",
                status=ObservationStatus.ABSTAIN,
                reason="abstained",
                relation=EvidenceRelation.SUPPORTING,
                relation_target=ANCHOR,
            )

    def test_an_unknown_status_is_rejected(self) -> None:
        with pytest.raises(EvidenceMergerError, match="status must be one of"):
            AdvisorObservation(source="openai", status="MAYBE")

    def test_an_unknown_relation_is_rejected(self) -> None:
        with pytest.raises(EvidenceMergerError, match="relation must be one of"):
            AdvisorObservation(
                source="openai",
                status=ObservationStatus.FINDING,
                finding=make_finding(source="openai"),
                relation="SUPPORTS",
            )

    def test_a_gate_that_is_not_settled_is_refused(self) -> None:
        unsettled = Decision(id="D-1", status=DecisionStatus.PENDING)

        with pytest.raises(EvidenceMergerError, match="ACCEPTED or REJECTED"):
            merge_evidence([], gate=unsettled)

    def test_a_non_decision_gate_is_refused(self) -> None:
        with pytest.raises(EvidenceMergerError, match="must be a Decision"):
            merge_evidence([], gate="compliant")

    def test_a_view_rejects_an_id_it_does_not_hold(self) -> None:
        with pytest.raises(EvidenceMergerError, match="not in the view"):
            EvidenceView(unresolved_ids=("ghost",))

    def test_a_view_rejects_overlapping_groups(self) -> None:
        finding = make_finding()

        with pytest.raises(EvidenceMergerError, match="one relation state"):
            EvidenceView(
                findings=(finding,),
                supporting_ids=(finding.id,),
                unresolved_ids=(finding.id,),
            )

    def test_a_view_rejects_an_unclassified_finding(self) -> None:
        finding = make_finding()

        with pytest.raises(EvidenceMergerError, match="unclassified"):
            EvidenceView(findings=(finding,))

    def test_a_conflict_needs_both_sides(self) -> None:
        with pytest.raises(EvidenceMergerError, match="at least one"):
            EvidenceConflict(target=ANCHOR, supporting_ids=("a",))


def _src_root() -> Path:
    return Path(__file__).resolve().parents[1] / "src" / "architecture_assistant"


def _application_dir() -> Path:
    return _src_root() / "application"


class TestLayerBoundary:
    """The merger is application-layer and pure - it owns no provider knowledge."""

    def test_the_merger_never_reaches_outwards(self) -> None:
        source = scan_directory(_src_root())
        module = f"{ROOT_PACKAGE}.application.evidence_merger"
        layers = {
            layer_of(target)
            for target in source.imports_of(module)
            if target.startswith(ROOT_PACKAGE)
        }

        assert layers == {"domain"}

    def test_the_merger_imports_no_infrastructure_or_architecture(self) -> None:
        source = scan_directory(_src_root())
        targets = source.imports_of(
            f"{ROOT_PACKAGE}.application.evidence_merger"
        )

        for target in targets:
            assert "infrastructure" not in target
            assert "architecture." not in target
            assert "composition" not in target

    def test_the_module_never_names_a_provider_error(self) -> None:
        text = (_application_dir() / "evidence_merger.py").read_text(
            encoding="utf-8"
        )

        for forbidden in (
            "OpenAIAdvisorError",
            "ClaudeAdvisorError",
            "GrokAdvisorError",
            "AdvisorAbstainError",
        ):
            assert forbidden not in text, forbidden

    def test_the_merger_is_pure(self) -> None:
        text = (_application_dir() / "evidence_merger.py").read_text(
            encoding="utf-8"
        )

        for forbidden in (
            "sqlite3",
            "urllib",
            "socket",
            "requests",
            "open(",
            "Path(",
        ):
            assert forbidden not in text, forbidden

    def test_no_judge_and_no_voting_helper_exists(self) -> None:
        from architecture_assistant.application import (
            decision_engine,
            evidence_merger,
        )

        for module in (decision_engine, evidence_merger):
            names = {name.lower() for name in dir(module)}
            assert not [name for name in names if "judge" in name]
            for name in ("majority", "ranking", "vote", "ballot"):
                assert name not in names, (module.__name__, name)

    def test_the_whole_tree_still_satisfies_the_current_baseline(self) -> None:
        source = scan_directory(_src_root())
        result = ArchitectureValidator().validate(source)

        assert result.is_compliant is True
        assert result.violations == ()
        assert result.baseline_version == ARCHITECTURE_CURRENT.version
