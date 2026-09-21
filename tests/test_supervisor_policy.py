"""Tests for the deterministic supervision send policy.

The policy answers **one** question - may this directive leave without a human
reading it first - and it must answer it from facts the assistant owns, never
from a supervisor's own label. These tests pin that: every mode, every action,
the risk label, the ``requires_human`` flag and the instruction denylist.
"""

from __future__ import annotations

import json

import pytest

from architecture_assistant.application import (
    AUTO_SEND_ACTIONS,
    MAX_INSTRUCTION_LENGTH,
    UNSAFE_INSTRUCTION_TERMS,
    SendDecision,
    SendVerdict,
    decide_send,
    unsafe_terms,
)
from architecture_assistant.domain.enums import (
    Mode,
    SupervisorAction,
    SupervisorRisk,
)

#: A bounded, obviously LOW-risk clarification that may pass every other rule.
SAFE_INSTRUCTION = "clarify which module owns the retry loop"


def verdict(
    *,
    mode: Mode = Mode.SUPERVISED,
    action: SupervisorAction = SupervisorAction.CLARIFY,
    risk: SupervisorRisk = SupervisorRisk.LOW,
    requires_human: bool = False,
    instruction: str = SAFE_INSTRUCTION,
) -> SendVerdict:
    return decide_send(
        mode=mode,
        action=action,
        risk=risk,
        requires_human=requires_human,
        instruction=instruction,
    )


class TestModeMatrix:
    """One row per operating mode - the declared semantics, not a guess."""

    def test_manual_never_sends_without_a_human(self) -> None:
        result = verdict(mode=Mode.MANUAL)
        assert result.decision is SendDecision.HUMAN
        assert "MANUAL" in result.reason

    def test_supervised_sends_the_low_risk_allowlist(self) -> None:
        assert verdict(mode=Mode.SUPERVISED).decision is SendDecision.AUTO

    def test_auto_uses_the_same_bounded_policy_as_supervised(self) -> None:
        auto = verdict(mode=Mode.AUTO)
        supervised = verdict(mode=Mode.SUPERVISED)
        assert auto.decision is supervised.decision is SendDecision.AUTO

    def test_auto_is_not_unrestricted(self) -> None:
        # The mode alone buys nothing: an action outside the allowlist, a higher
        # risk label or an unsafe instruction is refused in AUTO too.
        assert (
            verdict(mode=Mode.AUTO, action=SupervisorAction.REVISE).decision
            is SendDecision.HUMAN
        )
        assert (
            verdict(mode=Mode.AUTO, risk=SupervisorRisk.MEDIUM).decision
            is SendDecision.HUMAN
        )
        assert (
            verdict(mode=Mode.AUTO, instruction="add a new dependency").decision
            is SendDecision.HUMAN
        )


class TestActionAllowlist:
    """``REVISE`` is deliberately outside the automatic allowlist."""

    def test_the_allowlist_is_clarify_and_retry_only(self) -> None:
        assert set(AUTO_SEND_ACTIONS) == {
            SupervisorAction.CLARIFY,
            SupervisorAction.RETRY,
        }

    @pytest.mark.parametrize("action", list(AUTO_SEND_ACTIONS))
    def test_each_allowed_action_passes_when_every_other_rule_does(
        self, action: SupervisorAction
    ) -> None:
        assert verdict(action=action).decision is SendDecision.AUTO

    @pytest.mark.parametrize(
        "action",
        [
            SupervisorAction.REVISE,
            SupervisorAction.ESCALATE,
            SupervisorAction.ERROR,
            SupervisorAction.NO_ACTION,
        ],
    )
    def test_every_other_action_needs_a_human(
        self, action: SupervisorAction
    ) -> None:
        result = verdict(action=action)
        assert result.decision is SendDecision.HUMAN
        assert action.value in result.reason

    def test_no_instruction_is_not_a_send_of_anything(self) -> None:
        result = verdict(action=SupervisorAction.REVISE, instruction="   ")
        assert result.decision is SendDecision.NOTHING


class TestRiskAndHuman:
    """The supervisor's own labels are honoured, never trusted alone."""

    @pytest.mark.parametrize(
        "risk", [SupervisorRisk.MEDIUM, SupervisorRisk.HIGH]
    )
    def test_medium_and_high_always_need_a_human(
        self, risk: SupervisorRisk
    ) -> None:
        result = verdict(risk=risk)
        assert result.decision is SendDecision.HUMAN
        assert risk.value in result.reason

    def test_a_low_label_alone_is_not_enough(self) -> None:
        # LOW risk plus an allowlisted action is still refused when the
        # instruction asks for something that must never be sent automatically.
        result = verdict(instruction="remove the old module")
        assert result.decision is SendDecision.HUMAN
        assert result.unsafe

    def test_requires_human_is_never_overridden(self) -> None:
        result = verdict(requires_human=True)
        assert result.decision is SendDecision.HUMAN
        assert "human" in result.reason


class TestInstructionDenylist:
    """A deterministic keyphrase scan - broad on purpose."""

    def test_a_long_instruction_needs_a_human(self) -> None:
        result = verdict(instruction="x" * (MAX_INSTRUCTION_LENGTH + 1))
        assert result.decision is SendDecision.HUMAN
        assert str(MAX_INSTRUCTION_LENGTH) in result.reason

    @pytest.mark.parametrize(
        "instruction",
        [
            "change the architecture boundary",
            "add a dependency to requirements",
            "alter the database schema",
            "write a migration",
            "delete the legacy module",
            "rename the boundary module",
            "refactor the dispatcher",
            "tighten the security check",
            "rotate the api key",
            "expand the scope of the step",
            "raise max_attempts",
            "mark the step verified",
            "bypass the realization gate",
            "install a new package",
        ],
    )
    def test_unsafe_concepts_are_never_sent_automatically(
        self, instruction: str
    ) -> None:
        result = verdict(instruction=instruction)
        assert result.decision is SendDecision.HUMAN
        assert result.unsafe

    def test_the_scan_is_case_insensitive_and_ordered(self) -> None:
        assert unsafe_terms("DELETE the SCHEMA") == ("schema", "delete")

    def test_a_clean_instruction_names_nothing(self) -> None:
        assert unsafe_terms(SAFE_INSTRUCTION) == ()

    def test_the_denylist_covers_every_required_concept(self) -> None:
        # The plan's "must NOT auto-send" list, spelled out as terms.
        for concept in (
            "dependency",
            "architecture",
            "schema",
            "delete",
            "security",
            "scope",
            "attempt",
        ):
            assert any(
                concept.startswith(term) or term.startswith(concept)
                for term in UNSAFE_INSTRUCTION_TERMS
            ), concept


class TestVerdictShape:
    """The verdict is plain, deterministic data."""

    def test_the_same_inputs_always_produce_the_same_verdict(self) -> None:
        first = verdict().to_dict()
        second = decide_send(
            mode=Mode.SUPERVISED,
            action=SupervisorAction.CLARIFY,
            risk=SupervisorRisk.LOW,
            requires_human=False,
            instruction=SAFE_INSTRUCTION,
        ).to_dict()
        assert first == second

    def test_the_payload_is_json_safe(self) -> None:
        payload = verdict(instruction="delete the schema").to_dict()
        assert json.loads(json.dumps(payload)) == payload
        assert payload["decision"] == "HUMAN"
        assert isinstance(payload["unsafe"], list)

    def test_automatic_is_a_property_of_the_decision(self) -> None:
        assert verdict().automatic is True
        assert verdict(mode=Mode.MANUAL).automatic is False
