"""The deterministic send policy: who may deliver a directive, and when.

The supervisor is advisory, so its own labels are **not** trusted to authorize a
send. This module re-derives the send class from facts the assistant owns:

* the **mode** (``MANUAL`` never auto-sends, ``SUPERVISED`` and ``AUTO`` auto-send
  only the narrow LOW-risk allowlist);
* the **action**, restricted to an allowlist that cannot change architecture,
  scope, dependencies, schema or security;
* the supervisor's ``requires_human`` flag (honoured, never overridden);
* a **denylist of unsafe instruction terms** - a deterministic keyphrase scan of
  the proposed instruction, so an answer that merely *claims* to be LOW risk but
  asks for a dependency, a migration, a deletion or an attempt override is still
  refused automatic delivery.

The scan is deliberately broad: a false positive costs one human click, while a
false negative would auto-send a directive nobody reviewed. Nothing here decides
whether a step may be ``VERIFIED``, increments an attempt, or touches the FSM -
this module only answers one question: *may this directive leave without a human
reading it first?*
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..domain.enums import Mode, SupervisorAction, SupervisorRisk

__all__ = [
    "AUTO_SEND_ACTIONS",
    "UNSAFE_INSTRUCTION_TERMS",
    "MAX_INSTRUCTION_LENGTH",
    "SendDecision",
    "SendVerdict",
    "unsafe_terms",
    "decide_send",
]

#: The only actions that may ever be delivered without a human. ``REVISE`` is
#: deliberately **absent**: it is the action that changes what the worker builds,
#: so it always waits for a human - even in ``AUTO`` mode, and even if the
#: supervisor labelled it LOW.
AUTO_SEND_ACTIONS: tuple[SupervisorAction, ...] = (
    SupervisorAction.CLARIFY,
    SupervisorAction.RETRY,
)

#: Instruction terms that disqualify automatic delivery. A deterministic
#: substring scan, case-insensitive: an instruction naming any of these concepts
#: is treated as a scope/architecture/destructive change no matter how it is
#: labelled. The list is broad on purpose (see the module docstring).
UNSAFE_INSTRUCTION_TERMS: tuple[str, ...] = (
    "architecture",
    "baseline",
    "dependenc",
    "schema",
    "migration",
    "migrate",
    "delete",
    "remove",
    "drop ",
    "rm ",
    "rename",
    "refactor",
    "redesign",
    "security",
    "credential",
    "secret",
    "api key",
    "token",
    "scope",
    "max_attempt",
    "attempt",
    "version",
    "verified",
    "verify",
    "approve",
    "override",
    "bypass",
    "skip the",
    "disable",
    "install",
    "upgrade",
    "downgrade",
    "subprocess",
    "eval(",
    "exec(",
)

#: How long an instruction may be before it is refused automatic delivery. A
#: long instruction is not necessarily unsafe, but it is no longer a bounded
#: clarification, so a human reads it.
MAX_INSTRUCTION_LENGTH = 1000


class SendDecision(StrEnum):
    """What the policy decided about one directive."""

    #: The directive may be published without a human.
    AUTO = "AUTO"
    #: A human must approve the directive before it is published.
    HUMAN = "HUMAN"
    #: There is nothing to send (no directive): the status follows the action.
    NOTHING = "NOTHING"


@dataclass(frozen=True)
class SendVerdict:
    """One deterministic answer plus the reason a human can read."""

    decision: SendDecision
    reason: str
    unsafe: tuple[str, ...] = ()

    @property
    def automatic(self) -> bool:
        """Whether the directive may be published without a human."""
        return self.decision is SendDecision.AUTO

    def to_dict(self) -> dict[str, object]:
        """Deterministic, JSON-safe representation."""
        return {
            "decision": self.decision.value,
            "reason": self.reason,
            "unsafe": list(self.unsafe),
        }


def unsafe_terms(instruction: str) -> tuple[str, ...]:
    """The denylisted terms an instruction names, in the denylist's order."""
    text = (instruction or "").casefold()
    return tuple(term for term in UNSAFE_INSTRUCTION_TERMS if term in text)


def decide_send(
    *,
    mode: Mode,
    action: SupervisorAction,
    risk: SupervisorRisk,
    requires_human: bool,
    instruction: str,
) -> SendVerdict:
    """Decide whether one directive may be delivered without a human.

    Pure and total: the same inputs always produce the same verdict, and every
    refusal carries the reason the operator sees. Note what is *not* trusted:
    ``risk`` alone never authorizes a send (a LOW label from the supervisor is
    necessary but never sufficient), and ``requires_human`` can only move a
    directive towards a human, never away from one.
    """
    text = (instruction or "").strip()
    if not text:
        return SendVerdict(
            SendDecision.NOTHING,
            "the supervisor proposed no instruction, so there is nothing to send",
        )
    if requires_human:
        return SendVerdict(
            SendDecision.HUMAN,
            "the supervisor asked for a human decision",
        )
    if mode is Mode.MANUAL:
        return SendVerdict(
            SendDecision.HUMAN,
            "mode MANUAL never sends a directive without a human",
        )
    if action not in AUTO_SEND_ACTIONS:
        allowed = ", ".join(member.value for member in AUTO_SEND_ACTIONS)
        return SendVerdict(
            SendDecision.HUMAN,
            f"action {action.value} is not in the automatic allowlist "
            f"({allowed})",
        )
    if risk is not SupervisorRisk.LOW:
        return SendVerdict(
            SendDecision.HUMAN,
            f"risk {risk.value} is not LOW, so the directive needs a human",
        )
    if len(text) > MAX_INSTRUCTION_LENGTH:
        return SendVerdict(
            SendDecision.HUMAN,
            "the instruction is longer than "
            f"{MAX_INSTRUCTION_LENGTH} characters, so a human reads it",
        )
    unsafe = unsafe_terms(text)
    if unsafe:
        return SendVerdict(
            SendDecision.HUMAN,
            "the instruction names terms that must never be sent "
            "automatically: " + ", ".join(unsafe),
            unsafe,
        )
    return SendVerdict(
        SendDecision.AUTO,
        f"{mode.value} mode may send a LOW-risk {action.value} automatically",
    )
