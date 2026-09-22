"""Architecture deliberation - a controlled two-round architecture review board.

The lifecycle this module owns
------------------------------

    USER REQUIREMENT
          |
    ROUND 1:  Agent A (independent)   Agent B (independent)
                \\                    /
                 LEAD REVIEW (agreements, conflicts, questions)
                        |
                 REVIEW PACKETS
                    /        \\
    ROUND 2:  Agent A        Agent B        (exactly ONE reconsideration)
                    \\        /
              FINAL LEAD SYNTHESIS
                        |
              ARCHITECTURE PROPOSAL (DRAFT)
                        |
              HUMAN approval (the unchanged ``ProposalApproval``)

Five rules define it
--------------------

* **Independence is structural.** Round 1 is dispatched through
  :class:`~architecture_assistant.ports.capabilities.AgentAnalysisQuery`, which
  has *no field* that could carry a peer's proposal; only a Round-2
  :class:`~architecture_assistant.ports.capabilities.AgentReconsiderQuery` can
  carry the Lead's critique and the peer's structured claims. Agent A can
  therefore never see Agent B in Round 1 - not because a prompt says so, but
  because the request cannot express it.
* **Exactly one review round.** ``max_review_rounds`` is one. There is no debate
  loop, no ``A -> B -> A -> B`` chain, no timer and no autonomous re-run: a failed
  stage is retried only by an explicit operator action.
* **No voting, no majority, no ranking.** No provider is counted, weighted,
  ordered or preferred. The Lead reasons over evidence and trade-offs, and a
  disagreement that remains is preserved explicitly rather than averaged away.
* **Advisory only.** This module holds no workflow authority. It cannot approve a
  proposal, cannot reach ``VERIFIED``, cannot move a Step, and is handed no
  ``ArchitectureVersion``, no ``ArchitectureEvolution``, no ``ADRManager``, no
  ``RiskManager`` and no realization port - so a deliberation can never mutate the
  assistant's own baseline, create an ACR, write an assistant ADR/risk/rule or
  reach the deterministic gate.
* **Honest about failure.** A failed stage is recorded with a non-sensitive reason
  and every completed stage is preserved. One failed architect produces an
  explicit ``INSUFFICIENT_PEER_REVIEW`` peer-review status instead of pretending a
  two-agent deliberation happened. A provider the operator disabled abstains with
  **no call at all**. A malformed answer is a failure, never a design.

The Lead is not the Judge and not the Supervisor
------------------------------------------------
``JudgePort`` resolves one genuine evidence conflict and stays in the Quick Review
path. ``SupervisorPort`` supervises one Cline report and is a different system
entirely. The chair of this board is ``DeliberationLeadPort`` and nothing else.

Provider neutrality
-------------------
Imports are restricted to ``domain``, ``ports`` and sibling ``application``
modules - never ``infrastructure``, never a concrete adapter, never ``sqlite3``.
The seats (:class:`DeliberationSeat`) and the chair (:class:`DeliberationChair`)
are **injected**, so which provider answers is a composition decision and every
stage is testable offline with a scripted double.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from ..domain.audit import AuditAction, AuditEntityType, AuditEntry
from ..domain.enums import (
    DELIBERATION_MAX_REVIEW_ROUNDS,
    DeliberationStage,
    DeliberationStageStatus,
    DeliberationStatus,
    Round2Decision,
)
from ..domain.models import (
    ArchitectureProposal,
    DeliberationArtifact,
    DeliberationRun,
    utc_now,
)
from ..ports.capabilities import (
    SLOT_AGENT_A,
    SLOT_AGENT_B,
    SLOT_LEAD,
    AgentAnalysisQuery,
    AgentReconsiderQuery,
    DeliberationAgentPort,
    DeliberationLeadPort,
    LeadReviewQuery,
    LeadSynthesisQuery,
)
from ..ports.repositories import (
    ArchitectureProposalRepository,
    AuditRepository,
    DeliberationRepository,
    ProjectRepository,
)
from ..ports.transactions import TransactionPort
from .architecture_synthesis import proposal_fingerprint
from .eventlog import (
    EVENT_LEVEL_ERROR,
    EVENT_LEVEL_INFO,
    EVENT_LEVEL_WARN,
    build_event,
    exception_reason,
)

__all__ = [
    "DELIBERATION_SCHEMA_VERSION",
    "MAX_DELIBERATION_SECTION",
    "ROUND1_SECTIONS",
    "LEAD_REVIEW_SECTIONS",
    "SYNTHESIS_SECTIONS",
    "DeliberationError",
    "DeliberationNotFoundError",
    "DeliberationRequirementRequiredError",
    "DeliberationActorRequiredError",
    "DeliberationReasonRequiredError",
    "DeliberationStageOrderError",
    "DeliberationStageError",
    "DeliberationProviderError",
    "DeliberationResponseError",
    "DeliberationStaleError",
    "DeliberationLeadUnavailableError",
    "DeliberationSeat",
    "DeliberationChair",
    "deliberation_fingerprint",
    "peer_claims",
    "ACTION_RUN_ROUND1",
    "ACTION_GENERATE_LEAD_REVIEW",
    "ACTION_RUN_ROUND2",
    "ACTION_GENERATE_FINAL_SYNTHESIS",
    "ACTION_GENERATE_PROPOSAL",
    "ACTION_CANCEL",
    "DELIBERATION_ACTIONS",
    "RunSnapshot",
    "ArchitectureDeliberation",
]

#: Schema version of the synthesized/derived deliberation payloads.
DELIBERATION_SCHEMA_VERSION = "1.0"

#: How many entries one deliberation section may carry. A stage result is a
#: bounded fact sheet, never a document dump.
MAX_DELIBERATION_SECTION = 64

#: The structured sections of one Round-1 answer. Each is a list of records or a
#: list of texts; a section the provider omits is stored as an explicit empty
#: list rather than being invented.
ROUND1_SECTIONS: tuple[tuple[str, str], ...] = (
    ("modules", "records"),
    ("dependencies", "records"),
    ("data_flows", "records"),
    ("external_dependencies", "records"),
    ("architecture_choices", "records"),
    ("risks", "records"),
    ("assumptions", "texts"),
    ("open_questions", "texts"),
    ("alternatives", "records"),
    ("recommended_decisions", "records"),
    ("evidence", "records"),
)

#: The structured sections of the Lead's review. This is the review packet
#: source - explicitly *not* a final architecture.
LEAD_REVIEW_SECTIONS: tuple[str, ...] = (
    "agreements",
    "conflicts",
    "weak_assumptions",
    "missing_information",
    "risk_deltas",
    "questions_for_agent_a",
    "questions_for_agent_b",
    "common_questions",
    "recommendations_to_reconsider",
    "unresolved_questions",
)

#: The structured sections of the final synthesis.
SYNTHESIS_SECTIONS: tuple[tuple[str, str], ...] = (
    ("modules", "records"),
    ("responsibilities", "texts"),
    ("dependencies", "records"),
    ("boundaries", "texts"),
    ("data_flows", "records"),
    ("external_dependencies", "records"),
    ("architecture_decisions", "records"),
    ("accepted_points", "texts"),
    ("rejected_alternatives", "records"),
    ("remaining_disagreements", "texts"),
    ("risks", "records"),
    ("adr_candidates", "records"),
    ("implementation_phases", "records"),
    ("unresolved_questions", "texts"),
)

#: The Round-2 sections an architect answers with, plus the three decisions.
ROUND2_SECTIONS: tuple[str, ...] = (
    "changed_decisions",
    "unchanged_decisions",
    "remaining_disagreements",
    "new_risks",
    "remaining_questions",
)

#: Rounds a Round-1 section list may hold, and how long one entry may be.
MAX_SECTION_ENTRY_TEXT = 2000

#: The stage prefix used in an artifact id, so a row is readable in the database.
_ARTIFACT_PREFIX: dict[DeliberationStage, str] = {
    DeliberationStage.AGENT_A_ROUND1: "agent-a-round1",
    DeliberationStage.AGENT_B_ROUND1: "agent-b-round1",
    DeliberationStage.LEAD_REVIEW: "lead-review",
    DeliberationStage.AGENT_A_ROUND2: "agent-a-round2",
    DeliberationStage.AGENT_B_ROUND2: "agent-b-round2",
    DeliberationStage.FINAL_SYNTHESIS: "final-synthesis",
}

#: The stages a final synthesis depends on, in the order they must exist. The
#: synthesis identity is derived from exactly these, so it can never be produced
#: from a different set of upstream answers.
_UPSTREAM_STAGES: tuple[tuple[DeliberationStage, str], ...] = (
    (DeliberationStage.AGENT_A_ROUND1, SLOT_AGENT_A),
    (DeliberationStage.AGENT_B_ROUND1, SLOT_AGENT_B),
    (DeliberationStage.LEAD_REVIEW, SLOT_LEAD),
    (DeliberationStage.AGENT_A_ROUND2, SLOT_AGENT_A),
    (DeliberationStage.AGENT_B_ROUND2, SLOT_AGENT_B),
)


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------

class DeliberationError(Exception):
    """Base class for architecture-deliberation errors."""


class DeliberationNotFoundError(DeliberationError):
    """Raised when a deliberation id does not exist."""


class DeliberationRequirementRequiredError(DeliberationError):
    """Raised when a deliberation is started without a requirement."""


class DeliberationActorRequiredError(DeliberationError):
    """Raised when a lifecycle act is performed without an explicit actor."""


class DeliberationReasonRequiredError(DeliberationError):
    """Raised when a lifecycle act is performed without a non-empty reason."""


class DeliberationStageOrderError(DeliberationError):
    """Raised when a stage is requested out of order or twice."""


class DeliberationStageError(DeliberationError):
    """Raised when a stage cannot produce a usable result."""


class DeliberationProviderError(DeliberationStageError):
    """Raised when a seat's provider call fails."""


class DeliberationResponseError(DeliberationStageError):
    """Raised when a seat answered with content the contract refuses."""


class DeliberationStaleError(DeliberationError):
    """Raised when a downstream artifact no longer matches its recorded inputs."""


class DeliberationLeadUnavailableError(DeliberationStageError):
    """Raised when the chair's provider is not configured - with no call made."""


# ---------------------------------------------------------------------------
# the configured seats (injected; the application never learns a provider)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DeliberationSeat:
    """One configured architect seat.

    ``agent is None`` is the **disabled** seat: the operator chose to run this
    position without a provider, so the stage abstains and **no call is made at
    all**. ``provider``/``model`` are carried for identity, persistence and the
    panel - they are never a policy.
    """

    slot: str
    provider: str
    model: str
    agent: Optional[DeliberationAgentPort] = None

    def __post_init__(self) -> None:
        if self.slot not in (SLOT_AGENT_A, SLOT_AGENT_B):
            raise DeliberationError(
                f"slot must be {SLOT_AGENT_A!r} or {SLOT_AGENT_B!r}; "
                f"got {self.slot!r}"
            )
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise DeliberationError("provider must be a non-empty string")
        object.__setattr__(self, "model", self.model or "")
        if self.agent is not None and not callable(
            getattr(self.agent, "analyse", None)
        ):
            raise DeliberationError(
                "agent must implement DeliberationAgentPort (analyse/reconsider)"
            )

    @property
    def enabled(self) -> bool:
        """Whether this seat has a provider and will therefore be called."""
        return self.agent is not None


@dataclass(frozen=True)
class DeliberationChair:
    """The configured chair (the Lead) of the board.

    ``lead is None`` means no chair provider is configured: the review and
    synthesis stages then fail closed with
    :class:`DeliberationLeadUnavailableError` **without any call**, and every
    advisor result is preserved.
    """

    provider: str
    model: str
    lead: Optional[DeliberationLeadPort] = None

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise DeliberationError("provider must be a non-empty string")
        object.__setattr__(self, "model", self.model or "")
        if self.lead is not None and not callable(
            getattr(self.lead, "review", None)
        ):
            raise DeliberationError(
                "lead must implement DeliberationLeadPort (review/synthesize)"
            )

    @property
    def enabled(self) -> bool:
        """Whether a chair provider is configured."""
        return self.lead is not None


# ---------------------------------------------------------------------------
# content contracts (validated strictly; a malformed answer fails closed)
# ---------------------------------------------------------------------------

def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeliberationResponseError(
            f"{field_name} must be a non-empty string; got {value!r}"
        )
    return value


def _optional_text(value: Any, field_name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise DeliberationResponseError(
            f"{field_name} must be a string; got {value!r}"
        )
    if len(value) > MAX_SECTION_ENTRY_TEXT:
        raise DeliberationResponseError(
            f"{field_name} must be at most {MAX_SECTION_ENTRY_TEXT} characters"
        )
    return value


def _record_section(value: Any, field_name: str) -> tuple[Mapping[str, Any], ...]:
    """A bounded list of JSON-safe structured records."""
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise DeliberationResponseError(
            f"{field_name} must be a list of objects; got {value!r}"
        )
    if len(value) > MAX_DELIBERATION_SECTION:
        raise DeliberationResponseError(
            f"{field_name} must carry at most {MAX_DELIBERATION_SECTION} "
            f"entries; got {len(value)}"
        )
    records: list[Mapping[str, Any]] = []
    for entry in value:
        if isinstance(entry, str):
            records.append({"text": entry})
            continue
        if not isinstance(entry, Mapping):
            raise DeliberationResponseError(
                f"{field_name} entries must be objects or strings; got {entry!r}"
            )
        records.append(dict(entry))
    return tuple(records)


def _text_section(value: Any, field_name: str) -> tuple[str, ...]:
    """A bounded list of non-empty texts."""
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise DeliberationResponseError(
            f"{field_name} must be a list of strings; got {value!r}"
        )
    if len(value) > MAX_DELIBERATION_SECTION:
        raise DeliberationResponseError(
            f"{field_name} must carry at most {MAX_DELIBERATION_SECTION} "
            f"entries; got {len(value)}"
        )
    texts: list[str] = []
    for entry in value:
        if isinstance(entry, str):
            texts.append(entry)
            continue
        if isinstance(entry, Mapping) and isinstance(entry.get("text"), str):
            texts.append(entry["text"])
            continue
        raise DeliberationResponseError(
            f"{field_name} entries must be strings; got {entry!r}"
        )
    return tuple(texts)


def _any_section(value: Any, field_name: str) -> tuple[Any, ...]:
    """A bounded list of either texts or structured records."""
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise DeliberationResponseError(
            f"{field_name} must be a list; got {value!r}"
        )
    if len(value) > MAX_DELIBERATION_SECTION:
        raise DeliberationResponseError(
            f"{field_name} must carry at most {MAX_DELIBERATION_SECTION} "
            f"entries; got {len(value)}"
        )
    entries: list[Any] = []
    for entry in value:
        if isinstance(entry, str):
            entries.append(entry)
            continue
        if not isinstance(entry, Mapping):
            raise DeliberationResponseError(
                f"{field_name} entries must be strings or objects; got {entry!r}"
            )
        entries.append(dict(entry))
    return tuple(entries)


def _confidence(value: Any) -> Optional[float]:
    """A confidence score when the provider reported one, else ``None``."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DeliberationResponseError(
            f"confidence must be a number between 0 and 1; got {value!r}"
        )
    score = float(value)
    if not 0.0 <= score <= 1.0:
        raise DeliberationResponseError(
            f"confidence must be between 0 and 1; got {value!r}"
        )
    return round(score, 4)


def _round1_content(
    *,
    requirement: str,
    deliberation_id: str,
    source: str,
    provider: str,
    model: str,
    slot: str,
    raw: Any,
) -> dict[str, Any]:
    """Validate and normalize one Round-1 answer into the stored shape.

    The operator's requirement is echoed **verbatim**: an architect may reason
    about it, never rewrite it. Every section the provider omitted is stored as an
    explicit empty list, so a downstream stage can distinguish "nothing claimed"
    from "not reported".
    """
    if not isinstance(raw, Mapping):
        raise DeliberationResponseError(
            f"a Round-1 answer must be an object; got {raw!r}"
        )
    content: dict[str, Any] = {
        "schema_version": DELIBERATION_SCHEMA_VERSION,
        "review_id": deliberation_id,
        "round_no": 1,
        "advisor_id": f"{deliberation_id}:{slot}:1",
        "slot": slot,
        "provider": provider,
        "model": model,
        "source": source,
        "requirement": requirement,
        "proposal_summary": _require_text(
            raw.get("proposal_summary"), "proposal_summary"
        ),
        "confidence": _confidence(raw.get("confidence")),
    }
    for name, kind in ROUND1_SECTIONS:
        if kind == "records":
            content[name] = [
                dict(entry) for entry in _record_section(raw.get(name), name)
            ]
        else:
            content[name] = list(_text_section(raw.get(name), name))
    return content


def _round2_content(
    *,
    requirement: str,
    deliberation_id: str,
    source: str,
    provider: str,
    model: str,
    slot: str,
    links_to_round1_id: str,
    raw: Any,
) -> dict[str, Any]:
    """Validate and normalize one Round-2 answer (KEEP / REVISE / WITHDRAW)."""
    if not isinstance(raw, Mapping):
        raise DeliberationResponseError(
            f"a Round-2 answer must be an object; got {raw!r}"
        )
    try:
        decision = Round2Decision(str(raw.get("decision", "")).strip().upper())
    except ValueError as error:
        raise DeliberationResponseError(
            "decision must be one of KEEP, REVISE, WITHDRAW; "
            f"got {raw.get('decision')!r}"
        ) from error
    content: dict[str, Any] = {
        "schema_version": DELIBERATION_SCHEMA_VERSION,
        "review_id": deliberation_id,
        "round_no": 2,
        "advisor_id": f"{deliberation_id}:{slot}:2",
        "slot": slot,
        "provider": provider,
        "model": model,
        "source": source,
        "requirement": requirement,
        "decision": decision.value,
        "revised_summary": _optional_text(
            raw.get("revised_summary"), "revised_summary"
        ),
        "links_to_round1_id": links_to_round1_id,
    }
    for name in ROUND2_SECTIONS:
        content[name] = list(_any_section(raw.get(name), name))
    if decision is Round2Decision.KEEP and content["changed_decisions"]:
        raise DeliberationResponseError(
            "decision KEEP must not carry changed_decisions: an answer that "
            "changed nothing cannot also have changed decisions"
        )
    return content


def _lead_review_content(
    *,
    requirement: str,
    deliberation_id: str,
    source: str,
    provider: str,
    model: str,
    raw: Any,
) -> dict[str, Any]:
    """Validate and normalize the Lead's review (the review packet source)."""
    if not isinstance(raw, Mapping):
        raise DeliberationResponseError(
            f"a Lead review must be an object; got {raw!r}"
        )
    content: dict[str, Any] = {
        "schema_version": DELIBERATION_SCHEMA_VERSION,
        "review_id": deliberation_id,
        "lead_review_id": f"{deliberation_id}:lead-review",
        "stage": DeliberationStage.LEAD_REVIEW.value,
        "provider": provider,
        "model": model,
        "source": source,
        "requirement": requirement,
    }
    for name in LEAD_REVIEW_SECTIONS:
        content[name] = list(_any_section(raw.get(name), name))
    return content


def _synthesis_content(
    *,
    requirement: str,
    deliberation_id: str,
    source: str,
    provider: str,
    model: str,
    upstream_fingerprints: Sequence[str],
    raw: Any,
) -> dict[str, Any]:
    """Validate and normalize the Lead's final synthesis.

    A nested ``recommended_architecture`` object is accepted and flattened into
    the top-level sections, so a provider that answers either shape is understood
    - while an explicit top-level section always wins.
    """
    if not isinstance(raw, Mapping):
        raise DeliberationResponseError(
            f"a final synthesis must be an object; got {raw!r}"
        )
    nested = raw.get("recommended_architecture")
    flat: dict[str, Any] = dict(raw)
    if isinstance(nested, Mapping):
        for key in ("modules", "responsibilities", "dependencies", "boundaries"):
            if key not in flat and key in nested:
                flat[key] = nested[key]
    content: dict[str, Any] = {
        "schema_version": DELIBERATION_SCHEMA_VERSION,
        "review_id": deliberation_id,
        "synthesis_id": _final_synthesis_id(
            deliberation_id, tuple(upstream_fingerprints)
        ),
        "stage": DeliberationStage.FINAL_SYNTHESIS.value,
        "provider": provider,
        "model": model,
        "source": source,
        "requirement": requirement,
        "upstream_fingerprints": list(upstream_fingerprints),
        "summary": _require_text(flat.get("summary"), "summary"),
        "rationale": _optional_text(flat.get("rationale"), "rationale"),
    }
    for name, kind in SYNTHESIS_SECTIONS:
        if kind == "records":
            content[name] = [
                dict(entry) for entry in _record_section(flat.get(name), name)
            ]
        else:
            content[name] = list(_text_section(flat.get(name), name))
    return content


# ---------------------------------------------------------------------------
# identities and fingerprints (deterministic, clock-free, content-based)
# ---------------------------------------------------------------------------

def _digest(*parts: str) -> str:
    """The first 16 hex characters of a stable hash over ``parts``."""
    material = "|".join(parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _recorded_model(model: str) -> str:
    """The model name as it is recorded on a run.

    A **disabled** seat or chair has no model, so a stable placeholder is stored
    instead of an empty string: the run's identity requires a non-empty pair, and
    the placeholder keeps the recorded identity comparable with the live seat.
    """
    return model or "(none)"


def deliberation_fingerprint(
    *,
    project: str,
    requirement: str,
    agent_a_provider: str,
    agent_a_model: str,
    agent_b_provider: str,
    agent_b_model: str,
    lead_provider: str,
    lead_model: str,
    context_fingerprint: str,
    revision_no: int = 1,
) -> str:
    """The identity of a deliberation's **material inputs**.

    Clock-free on purpose: the same requirement, the same three configured seats
    and the same bounded context always fingerprint identically, and *any* change
    to them produces a different identity - which is what makes a stale synthesis
    detectable instead of silently reusable.
    """
    return hashlib.sha256(
        "|".join(
            (
                project,
                requirement,
                agent_a_provider,
                agent_a_model,
                agent_b_provider,
                agent_b_model,
                lead_provider,
                lead_model,
                context_fingerprint,
                str(revision_no),
            )
        ).encode("utf-8")
    ).hexdigest()


def _deliberation_id(material: str) -> str:
    """The deterministic id of one deliberation."""
    return f"deliberation-{material[:16]}"


def _artifact_id(
    deliberation_id: str, stage: DeliberationStage, slot: str
) -> str:
    """A readable, deterministic artifact id for one stage of one run."""
    prefix = _ARTIFACT_PREFIX[stage]
    return f"{prefix}-{_digest(deliberation_id, stage.value, slot)}"


def _final_synthesis_id(
    deliberation_id: str, upstream_fingerprints: Sequence[str]
) -> str:
    """The synthesis identity, derived from the **exact** upstream artifacts.

    A synthesis therefore cannot be attached to a different set of Round-1,
    LeadReview or Round-2 answers: change one upstream artifact and the identity
    changes too.
    """
    return "final-synthesis-" + _digest(
        deliberation_id, *upstream_fingerprints
    )


def _content_fingerprint(
    deliberation_id: str, stage: DeliberationStage, slot: str, content: Mapping[str, Any]
) -> str:
    """The content hash of one stage result.

    Recomputed from the artifact's own content whenever a downstream stage
    depends on it, so a hand-edited or truncated row is detected instead of being
    trusted.
    """
    payload = json.dumps(dict(content), sort_keys=True, default=str)
    return hashlib.sha256(
        "|".join((deliberation_id, stage.value, slot, payload)).encode("utf-8")
    ).hexdigest()


def _context_fingerprint(context: Mapping[str, Any]) -> str:
    """A stable hash of the bounded project context both architects receive."""
    payload = json.dumps(dict(context), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


#: The peer-facing projection of a Round-1 answer. Deliberately *structured
#: conclusions only*: a Round-2 packet carries this instead of the peer's whole
#: answer, so no transcript and no hidden reasoning can cross the boundary.
_PEER_CLAIM_KEYS: tuple[str, ...] = (
    "proposal_summary",
    "modules",
    "dependencies",
    "data_flows",
    "external_dependencies",
    "architecture_choices",
    "risks",
    "assumptions",
    "open_questions",
    "recommended_decisions",
)


def peer_claims(content: Mapping[str, Any]) -> dict[str, Any]:
    """The structured claims of one proposal, safe to hand to the other seat."""
    return {
        key: content.get(key)
        for key in _PEER_CLAIM_KEYS
        if content.get(key) is not None
    }


def _emit(
    on_event: Optional[Callable[[Mapping[str, Any]], None]],
    *,
    level: str,
    action: str,
    message: str,
    stamp: Callable[[], datetime],
) -> None:
    """Hand one sanitized event to the injected sink; never raise out of it."""
    if on_event is None:
        return
    try:
        on_event(
            build_event(
                level=level,
                component="CoreWorker",
                action=action,
                message=message,
                timestamp=stamp(),
            )
        )
    except Exception:  # noqa: BLE001 - an event sink never breaks a stage
        return


# ---------------------------------------------------------------------------
# the read-only snapshot (plain data for a GUI or a report)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RunSnapshot:
    """Everything one deliberation currently is, as plain JSON-safe data.

    A snapshot **decides nothing**: it reports the run, its seat configuration,
    the status of each of the six stages, the compact counts the panel shows, the
    per-stage cost, the addresses of every artifact and - importantly for a GUI -
    exactly which actions are valid at this stage, so an invalid button is never
    offered.
    """

    deliberation_id: str
    project: str
    requirement: str
    status: str
    fingerprint: str
    context_fingerprint: str
    max_review_rounds: int
    revision_no: int
    revision_of_deliberation_id: Optional[str]
    error_reason: str
    stale_reason: str
    seats: Mapping[str, Any]
    stages: Mapping[str, Any]
    counts: Mapping[str, int]
    cost: Mapping[str, Any]
    artifacts: tuple[Mapping[str, Any], ...]
    available_actions: tuple[str, ...]
    ready_for_proposal: bool
    final_synthesis_id: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "deliberation_id": self.deliberation_id,
            "project": self.project,
            "requirement": self.requirement,
            "status": self.status,
            "fingerprint": self.fingerprint,
            "context_fingerprint": self.context_fingerprint,
            "max_review_rounds": self.max_review_rounds,
            "revision_no": self.revision_no,
            "revision_of_deliberation_id": self.revision_of_deliberation_id,
            "error_reason": self.error_reason,
            "stale_reason": self.stale_reason,
            "seats": dict(self.seats),
            "stages": dict(self.stages),
            "counts": dict(self.counts),
            "cost": dict(self.cost),
            "artifacts": [dict(entry) for entry in self.artifacts],
            "available_actions": list(self.available_actions),
            "ready_for_proposal": self.ready_for_proposal,
            "final_synthesis_id": self.final_synthesis_id,
        }


#: The actions a snapshot may offer, in the order a panel shows them. A stage
#: action is offered only when the run is at the stage it belongs to, so the GUI
#: never has to invent an enabled/disabled rule of its own.
ACTION_RUN_ROUND1 = "run-round1"
ACTION_GENERATE_LEAD_REVIEW = "generate-lead-review"
ACTION_RUN_ROUND2 = "run-round2"
ACTION_GENERATE_FINAL_SYNTHESIS = "generate-final-synthesis"
ACTION_GENERATE_PROPOSAL = "generate-proposal"
ACTION_CANCEL = "cancel"
DELIBERATION_ACTIONS: tuple[str, ...] = (
    ACTION_RUN_ROUND1,
    ACTION_GENERATE_LEAD_REVIEW,
    ACTION_RUN_ROUND2,
    ACTION_GENERATE_FINAL_SYNTHESIS,
    ACTION_GENERATE_PROPOSAL,
    ACTION_CANCEL,
)


# ---------------------------------------------------------------------------
# the use-case
# ---------------------------------------------------------------------------

class ArchitectureDeliberation:
    """Runs the controlled two-round board over one operator requirement.

    It is handed exactly four collaborators - the deliberation repository, the
    project repository (project identity only), the audit trail and the shared
    transaction boundary - plus the injected seats and the injected clock. That
    is the *whole* dependency surface, so this object cannot reach the assistant's
    own architecture lifecycle, the FSM, the worker, the monitor or the
    realization gate even by accident.

    Every stage is a separate explicit call. Nothing here runs autonomously, on a
    timer or in a loop, and nothing here approves anything: the generated
    ``ArchitectureProposal`` is a ``DRAFT`` until a human decides through the
    unchanged ``ProposalApproval`` path.
    """

    def __init__(
        self,
        deliberations: DeliberationRepository,
        proposals: ArchitectureProposalRepository,
        projects: ProjectRepository,
        audit: AuditRepository,
        transactions: TransactionPort,
        *,
        agent_a: DeliberationSeat,
        agent_b: DeliberationSeat,
        chair: DeliberationChair,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        for name, port, method in (
            ("deliberations", deliberations, "upsert_run"),
            ("proposals", proposals, "upsert"),
            ("projects", projects, "list"),
            ("audit", audit, "append"),
            ("transactions", transactions, "transaction"),
        ):
            if not callable(getattr(port, method, None)):
                raise ValueError(
                    f"{name} must implement {method}; got {type(port).__name__}"
                )
        if not isinstance(agent_a, DeliberationSeat):
            raise ValueError("agent_a must be a DeliberationSeat")
        if not isinstance(agent_b, DeliberationSeat):
            raise ValueError("agent_b must be a DeliberationSeat")
        if agent_a.slot != SLOT_AGENT_A or agent_b.slot != SLOT_AGENT_B:
            raise ValueError(
                "agent_a must sit in the agent_a slot and agent_b in agent_b"
            )
        if not isinstance(chair, DeliberationChair):
            raise ValueError("chair must be a DeliberationChair")
        if not callable(clock):
            raise ValueError("clock must be a callable returning a datetime")
        self._deliberations = deliberations
        self._proposals = proposals
        self._projects = projects
        self._audit = audit
        self._transactions = transactions
        self._agent_a = agent_a
        self._agent_b = agent_b
        self._chair = chair
        self._clock = clock

    # -- configuration -----------------------------------------------------

    @property
    def agent_a(self) -> DeliberationSeat:
        """The seat that answers as Agent A."""
        return self._agent_a

    @property
    def agent_b(self) -> DeliberationSeat:
        """The seat that answers as Agent B."""
        return self._agent_b

    @property
    def chair(self) -> DeliberationChair:
        """The chair (the Lead) of the board."""
        return self._chair

    def configure(
        self,
        *,
        agent_a: Optional[DeliberationSeat] = None,
        agent_b: Optional[DeliberationSeat] = None,
        chair: Optional[DeliberationChair] = None,
    ) -> None:
        """Re-place the seats. A new run picks up the new configuration.

        An existing run is **never** silently re-pointed: its own fingerprint
        already records which provider/model answered, so a change here only
        affects the *next* deliberation - which is exactly what keeps a completed
        run traceable.
        """
        if agent_a is not None:
            if not isinstance(agent_a, DeliberationSeat):
                raise ValueError("agent_a must be a DeliberationSeat")
            if agent_a.slot != SLOT_AGENT_A:
                raise ValueError("agent_a must sit in the agent_a slot")
            self._agent_a = agent_a
        if agent_b is not None:
            if not isinstance(agent_b, DeliberationSeat):
                raise ValueError("agent_b must be a DeliberationSeat")
            if agent_b.slot != SLOT_AGENT_B:
                raise ValueError("agent_b must sit in the agent_b slot")
            self._agent_b = agent_b
        if chair is not None:
            if not isinstance(chair, DeliberationChair):
                raise ValueError("chair must be a DeliberationChair")
            self._chair = chair

    # -- reads -------------------------------------------------------------

    def require(self, deliberation_id: str) -> DeliberationRun:
        """One run, or fail closed."""
        if not isinstance(deliberation_id, str) or not deliberation_id.strip():
            raise DeliberationError(
                "deliberation_id must be a non-empty string; "
                f"got {deliberation_id!r}"
            )
        run = self._deliberations.get_run(deliberation_id)
        if run is None:
            raise DeliberationNotFoundError(
                f"no deliberation {deliberation_id!r}"
            )
        return run

    def runs(self) -> tuple[DeliberationRun, ...]:
        """Every deliberation, oldest first - the panel's history."""
        return tuple(self._deliberations.list_runs())

    def artifacts(self, deliberation_id: str) -> tuple[DeliberationArtifact, ...]:
        """Every stage artifact of one run, in lifecycle stage order."""
        return tuple(self._deliberations.list_artifacts(deliberation_id))

    def latest_artifact(
        self, deliberation_id: str, stage: DeliberationStage, slot: str = ""
    ) -> Optional[DeliberationArtifact]:
        """The newest artifact of one stage (optionally one seat), or ``None``."""
        found = self._deliberations.list_artifacts_for_stage(
            deliberation_id, stage, slot
        )
        return found[-1] if found else None

    # -- internals ---------------------------------------------------------

    def _seat(self, slot: str) -> DeliberationSeat:
        return self._agent_a if slot == SLOT_AGENT_A else self._agent_b

    def _store(
        self,
        run: DeliberationRun,
        artifacts: Sequence[DeliberationArtifact],
        entry: AuditEntry,
    ) -> None:
        """One stage write: the run, its artifacts and one audit entry."""
        with self._transactions.transaction():
            self._deliberations.upsert_run(run)
            for artifact in artifacts:
                self._deliberations.upsert_artifact(artifact)
            self._audit.append(entry)

    def _persist_run(self, run: DeliberationRun) -> None:
        """Persist a status transition **without** an audit entry.

        Used for the ``*_RUNNING`` markers: they exist so a crash mid-stage is
        visible after a restart, but a stage *start* is not itself a durable
        decision - the stage's **completion** is audited, once, with its outcome.
        """
        with self._transactions.transaction():
            self._deliberations.upsert_run(run)

    def _moved(
        self,
        run: DeliberationRun,
        status: DeliberationStatus,
        *,
        now: datetime,
        error_reason: str = "",
    ) -> DeliberationRun:
        """The run with a new lifecycle status (content is never rewritten)."""
        return replace(
            run,
            status=status,
            error_reason=error_reason,
            updated_at=now,
        )

    def _entry(
        self,
        run: DeliberationRun,
        *,
        action: AuditAction,
        operation: str,
        actor: str,
        reason: str,
        now: datetime,
        detail: Optional[Mapping[str, Any]] = None,
    ) -> AuditEntry:
        """One audit entry for one lifecycle act - structured, never prose."""
        payload: dict[str, Any] = {
            "operation": operation,
            "actor": actor,
            "reason": reason,
            "project": run.project,
            "status": run.status.value,
            "fingerprint": run.fingerprint,
            "revision_no": run.revision_no,
            "revision_of_deliberation_id": run.revision_of_deliberation_id,
            "agent_a": f"{run.agent_a_provider}:{run.agent_a_model}",
            "agent_b": f"{run.agent_b_provider}:{run.agent_b_model}",
            "lead": f"{run.lead_provider}:{run.lead_model}",
        }
        if detail:
            payload.update(dict(detail))
        return AuditEntry(
            entity_type=AuditEntityType.DELIBERATION,
            entity_id=run.deliberation_id,
            action=action,
            detail=payload,
            created_at=now,
        )

    # -- stage 0: start ----------------------------------------------------

    def start(
        self,
        requirement: str,
        *,
        actor: str,
        reason: str,
        context: Optional[Mapping[str, Any]] = None,
        revision_of: Optional[str] = None,
        project: Optional[str] = None,
        on_event: Optional[Callable[[Mapping[str, Any]], None]] = None,
    ) -> DeliberationRun:
        """Create the run for one requirement - identity, seats, status DRAFT.

        Idempotent by identity: starting the **same** requirement with the same
        three configured seats and the same bounded context returns the existing
        run instead of creating a second one. A changed requirement, a changed
        provider/model or a changed context is a *different* deliberation and
        therefore a different id - which is what makes staleness detectable.

        ``revision_of`` starts an explicit **new** run that points at a previous
        one (``revision_of_deliberation_id``); the previous run is never touched.
        """
        if not isinstance(requirement, str) or not requirement.strip():
            raise DeliberationRequirementRequiredError(
                "a deliberation needs the operator's requirement verbatim"
            )
        if not isinstance(actor, str) or not actor.strip():
            raise DeliberationActorRequiredError(
                "starting a deliberation requires an explicit actor"
            )
        if not isinstance(reason, str) or not reason.strip():
            raise DeliberationReasonRequiredError(
                "starting a deliberation requires a non-empty reason"
            )
        bounded = dict(context or {})
        if len(bounded) > MAX_DELIBERATION_SECTION:
            raise DeliberationError(
                f"context must carry at most {MAX_DELIBERATION_SECTION} keys; "
                f"got {len(bounded)}"
            )
        resolved_project = self._project_name(project)
        now = self._clock()

        revision_no = 1
        lineage: Optional[str] = None
        if revision_of is not None:
            predecessor = self.require(revision_of)
            if predecessor.project != resolved_project:
                raise DeliberationError(
                    f"deliberation {revision_of} belongs to project "
                    f"{predecessor.project!r}, not {resolved_project!r}"
                )
            if not predecessor.is_settled:
                raise DeliberationError(
                    f"deliberation {revision_of} is "
                    f"{predecessor.status.value}; only a settled deliberation "
                    "may be superseded by a new one"
                )
            revision_no = predecessor.revision_no + 1
            lineage = predecessor.deliberation_id

        context_fingerprint = _context_fingerprint(bounded)
        fingerprint = deliberation_fingerprint(
            project=resolved_project,
            requirement=requirement,
            agent_a_provider=self._agent_a.provider,
            agent_a_model=self._agent_a.model,
            agent_b_provider=self._agent_b.provider,
            agent_b_model=self._agent_b.model,
            lead_provider=self._chair.provider,
            lead_model=self._chair.model,
            context_fingerprint=context_fingerprint,
            revision_no=revision_no,
        )
        deliberation_id = _deliberation_id(_digest(fingerprint))
        return self._create(
            deliberation_id,
            requirement=requirement,
            project=resolved_project,
            fingerprint=fingerprint,
            context_fingerprint=context_fingerprint,
            revision_no=revision_no,
            lineage=lineage,
            actor=actor,
            reason=reason,
            now=now,
            on_event=on_event,
        )

    def _project_name(self, project: Optional[str]) -> str:
        """The managed project's name - explicit, or the single managed one."""
        if project is not None:
            if not isinstance(project, str) or not project.strip():
                raise DeliberationError(
                    f"project must be a non-empty string; got {project!r}"
                )
            return project
        names = tuple(
            sorted(getattr(item, "name", "") for item in self._projects.list())
        )
        if len(names) != 1:
            raise DeliberationError(
                "expected exactly one managed project, found "
                f"{len(names)}: {', '.join(names) or 'none'}"
            )
        return names[0]

    def _create(
        self,
        deliberation_id: str,
        *,
        requirement: str,
        project: str,
        fingerprint: str,
        context_fingerprint: str,
        revision_no: int,
        lineage: Optional[str],
        actor: str,
        reason: str,
        now: datetime,
        on_event: Optional[Callable[[Mapping[str, Any]], None]],
    ) -> DeliberationRun:
        """Create the run if its identity is new; return what exists either way."""
        existing = self._deliberations.get_run(deliberation_id)
        if existing is not None:
            return existing
        run = DeliberationRun(
            deliberation_id=deliberation_id,
            project=project,
            requirement=requirement,
            fingerprint=fingerprint,
            context_fingerprint=context_fingerprint,
            agent_a_provider=self._agent_a.provider,
            agent_a_model=_recorded_model(self._agent_a.model),
            agent_b_provider=self._agent_b.provider,
            agent_b_model=_recorded_model(self._agent_b.model),
            lead_provider=self._chair.provider,
            lead_model=_recorded_model(self._chair.model),
            created_at=now,
            updated_at=now,
            status=DeliberationStatus.DRAFT,
            max_review_rounds=DELIBERATION_MAX_REVIEW_ROUNDS,
            revision_no=revision_no,
            revision_of_deliberation_id=lineage,
        )
        self._store(
            run,
            (),
            self._entry(
                run,
                action=AuditAction.CREATE,
                operation="create-deliberation",
                actor=actor,
                reason=reason,
                now=now,
                detail={
                    "max_review_rounds": run.max_review_rounds,
                    "agent_a_enabled": self._agent_a.enabled,
                    "agent_b_enabled": self._agent_b.enabled,
                    "lead_enabled": self._chair.enabled,
                },
            ),
        )
        _emit(
            on_event,
            level=EVENT_LEVEL_INFO,
            action="deliberation-created",
            message=(
                f"deliberation {deliberation_id} created for {project} "
                f"(revision {revision_no}, agent A {self._agent_a.provider}, "
                f"agent B {self._agent_b.provider}, "
                f"lead {self._chair.provider})"
            ),
            stamp=self._clock,
        )
        return run

    # -- shared stage guards ----------------------------------------------

    def _require_stage(
        self,
        deliberation_id: str,
        allowed: Sequence[DeliberationStatus],
        stage: str,
    ) -> DeliberationRun:
        """Load a run and refuse a stage the run is not at (fail closed)."""
        run = self.require(deliberation_id)
        if run.status not in allowed:
            listed = ", ".join(status.value for status in allowed)
            raise DeliberationStageOrderError(
                f"{stage} requires status {listed}; deliberation "
                f"{run.deliberation_id} is {run.status.value}"
            )
        return run

    def _context_for(
        self, run: DeliberationRun, context: Optional[Mapping[str, Any]]
    ) -> dict[str, Any]:
        """The bounded context, verified against the run's recorded fingerprint.

        A context that has since changed is refused instead of being fed to a
        provider: the run was fingerprinted over *its* context, so silently
        substituting another one would make the recorded identity a lie.
        """
        bounded = dict(context or {})
        if _context_fingerprint(bounded) != run.context_fingerprint:
            raise DeliberationStaleError(
                f"deliberation {run.deliberation_id} was fingerprinted over a "
                "different project context; the deliberation is stale"
            )
        return bounded

    # -- stage 1: round 1 (independent analysis) ---------------------------

    def run_round1(
        self,
        deliberation_id: str,
        *,
        actor: str,
        reason: str,
        context: Optional[Mapping[str, Any]] = None,
        on_event: Optional[Callable[[Mapping[str, Any]], None]] = None,
    ) -> DeliberationRun:
        """Round 1: both architects analyse the requirement **independently**.

        Agent A is dispatched and answered *before* Agent B is even asked, and the
        request each receives is an :class:`AgentAnalysisQuery` - a shape that
        cannot carry the other's proposal. A Round-1 run therefore cannot leak a
        peer's answer, whatever a prompt might say.
        """
        run = self._require_stage(
            deliberation_id,
            (
                DeliberationStatus.DRAFT,
                DeliberationStatus.ERROR,
                DeliberationStatus.CANCELLED,
                DeliberationStatus.ROUND1_RUNNING,
            ),
            "round 1",
        )
        self._require_actor(actor)
        self._require_reason(reason)
        bounded = self._context_for(run, context)
        now = self._clock()
        running = self._moved(run, DeliberationStatus.ROUND1_RUNNING, now=now)
        self._persist_run(running)
        _emit(
            on_event,
            level=EVENT_LEVEL_INFO,
            action="round1-started",
            message=f"deliberation {run.deliberation_id}: round 1 started",
            stamp=self._clock,
        )

        produced: list[DeliberationArtifact] = []
        for seat in (self._agent_a, self._agent_b):
            self._assert_seat_matches(run, seat)
            stage = (
                DeliberationStage.AGENT_A_ROUND1
                if seat.slot == SLOT_AGENT_A
                else DeliberationStage.AGENT_B_ROUND1
            )
            artifact = self._analyse(run, seat, stage, bounded, on_event=on_event)
            produced.append(artifact)

        usable = [artifact for artifact in produced if artifact.is_usable]
        status = (
            DeliberationStatus.ROUND1_COMPLETE
            if usable
            else DeliberationStatus.ERROR
        )
        finished = self._moved(
            running,
            status,
            now=self._clock(),
            error_reason=(
                ""
                if usable
                else "round 1 produced no usable analysis from either architect"
            ),
        )
        self._store(
            finished,
            tuple(produced),
            self._entry(
                finished,
                action=AuditAction.UPDATE,
                operation="run-round1",
                actor=actor,
                reason=reason,
                now=self._clock(),
                detail={
                    "stage": "round1",
                    "usable": len(usable),
                    "stages": [artifact.stage.value for artifact in produced],
                    "statuses": [
                        artifact.status.value for artifact in produced
                    ],
                },
            ),
        )
        _emit(
            on_event,
            level=(
                EVENT_LEVEL_INFO if usable else EVENT_LEVEL_ERROR
            ),
            action="round1-completed",
            message=(
                f"deliberation {run.deliberation_id}: round 1 complete "
                f"({len(usable)}/{len(produced)} usable)"
            ),
            stamp=self._clock,
        )
        return finished

    def _require_actor(self, actor: str) -> None:
        if not isinstance(actor, str) or not actor.strip():
            raise DeliberationActorRequiredError(
                "this act requires an explicit actor"
            )

    def _require_reason(self, reason: str) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise DeliberationReasonRequiredError(
                "this act requires a non-empty reason"
            )

    # -- the artifact builder and the two provider dispatches --------------

    def _artifact(
        self,
        run: DeliberationRun,
        stage: DeliberationStage,
        slot: str,
        content: Mapping[str, Any],
        status: DeliberationStageStatus,
    ) -> DeliberationArtifact:
        """One addressable stage result, with its content fingerprint."""
        payload = dict(content)
        return DeliberationArtifact(
            artifact_id=_artifact_id(run.deliberation_id, stage, slot),
            deliberation_id=run.deliberation_id,
            stage=stage,
            status=status,
            content=payload,
            fingerprint=_content_fingerprint(
                run.deliberation_id, stage, slot, payload
            ),
            slot=slot,
            created_at=self._clock(),
        )

    def _stage_failure_content(
        self,
        seat: DeliberationSeat,
        run: DeliberationRun,
        *,
        round_no: int,
        reason: str,
    ) -> dict[str, Any]:
        """A non-sensitive description of a stage that produced no result.

        It carries no provider message, no request, no header and no credential -
        only a fixed phrase and (where relevant) an exception **type name**.
        """
        return {
            "schema_version": DELIBERATION_SCHEMA_VERSION,
            "review_id": run.deliberation_id,
            "round_no": round_no,
            "slot": seat.slot,
            "provider": seat.provider,
            "model": seat.model,
            "requirement": run.requirement,
            "reason": reason,
        }

    def _analyse(
        self,
        run: DeliberationRun,
        seat: DeliberationSeat,
        stage: DeliberationStage,
        context: Mapping[str, Any],
        *,
        on_event: Optional[Callable[[Mapping[str, Any]], None]],
    ) -> DeliberationArtifact:
        """Dispatch **one independent** Round-1 analysis and record the outcome.

        A disabled seat abstains without a call. A provider failure and a malformed
        answer are recorded as an ``ERROR`` stage with a non-sensitive reason, so
        one seat never hides the other's work and nothing is silently invented.
        """
        slot = seat.slot
        event_slot = slot.replace("_", "-")
        _emit(
            on_event,
            level=EVENT_LEVEL_INFO,
            action=f"{event_slot}-round1-started",
            message=(
                f"deliberation {run.deliberation_id}: {slot} round 1 started "
                f"({seat.provider})"
            ),
            stamp=self._clock,
        )
        if not seat.enabled:
            artifact = self._artifact(
                run,
                stage,
                slot,
                self._stage_failure_content(
                    seat,
                    run,
                    round_no=1,
                    reason="provider disabled: no call was made",
                ),
                DeliberationStageStatus.ABSTAINED,
            )
        else:
            try:
                result = seat.agent.analyse(
                    AgentAnalysisQuery(
                        project=run.project,
                        requirement=run.requirement,
                        deliberation_id=run.deliberation_id,
                        slot=slot,
                        context=context,
                    )
                )
            except Exception as error:  # noqa: BLE001 - one seat never blocks another
                artifact = self._artifact(
                    run,
                    stage,
                    slot,
                    self._stage_failure_content(
                        seat,
                        run,
                        round_no=1,
                        reason=f"provider failed: {exception_reason(error)}",
                    ),
                    DeliberationStageStatus.ERROR,
                )
            else:
                content, failure = self._round_content(
                    seat=seat,
                    run=run,
                    raw=result.content,
                    round_no=1,
                    slot=slot,
                    source=result.source,
                    links_to_round1_id="",
                )
                if failure is not None:
                    artifact = self._artifact(
                        run,
                        stage,
                        slot,
                        self._stage_failure_content(
                            seat, run, round_no=1, reason=failure
                        ),
                        DeliberationStageStatus.ERROR,
                    )
                else:
                    content["cost"] = dict(result.cost)
                    artifact = self._artifact(
                        run,
                        stage,
                        slot,
                        content,
                        DeliberationStageStatus.COMPLETE,
                    )
        _emit(
            on_event,
            level=EVENT_LEVEL_INFO if artifact.is_usable else EVENT_LEVEL_WARN,
            action=f"{event_slot}-round1-completed",
            message=(
                f"deliberation {run.deliberation_id}: {slot} round 1 "
                f"{artifact.status.value}"
            ),
            stamp=self._clock,
        )
        return artifact

    def _round_content(
        self,
        *,
        seat: DeliberationSeat,
        run: DeliberationRun,
        raw: Any,
        round_no: int,
        slot: str,
        source: str,
        links_to_round1_id: str,
    ) -> tuple[dict[str, Any], Optional[str]]:
        """Validate one answer; return ``(content, failure_reason)``.

        A failure reason is a short non-sensitive phrase (the exception *type
        name*), never a provider message.
        """
        try:
            if round_no == 1:
                content = _round1_content(
                    requirement=run.requirement,
                    deliberation_id=run.deliberation_id,
                    source=source,
                    provider=seat.provider,
                    model=seat.model,
                    slot=slot,
                    raw=raw,
                )
            else:
                content = _round2_content(
                    requirement=run.requirement,
                    deliberation_id=run.deliberation_id,
                    source=source,
                    provider=seat.provider,
                    model=seat.model,
                    slot=slot,
                    links_to_round1_id=links_to_round1_id,
                    raw=raw,
                )
        except DeliberationResponseError as error:
            return {}, "malformed answer: " + type(error).__name__
        return content, None

    def _reconsider(
        self,
        run: DeliberationRun,
        seat: DeliberationSeat,
        stage: DeliberationStage,
        context: Mapping[str, Any],
        *,
        lead_review: Mapping[str, Any],
        own_round1: DeliberationArtifact,
        peer_round1: DeliberationArtifact,
        on_event: Optional[Callable[[Mapping[str, Any]], None]],
    ) -> DeliberationArtifact:
        """Dispatch **one** Round-2 reconsideration for one seat.

        The request carries the seat's own Round-1 result, the Lead's critique
        **for that seat** and the peer's *structured claims* - the projection
        produced by :func:`peer_claims`. The peer's answer object, its raw provider
        text and any hidden reasoning are deliberately never passed.
        """
        slot = seat.slot
        event_slot = slot.replace("_", "-")
        critique = {
            "conflicts": lead_review.get("conflicts", []),
            "weak_assumptions": lead_review.get("weak_assumptions", []),
            "missing_information": lead_review.get("missing_information", []),
            "risk_deltas": lead_review.get("risk_deltas", []),
            "recommendations_to_reconsider": lead_review.get(
                "recommendations_to_reconsider", []
            ),
            "questions": lead_review.get(
                "questions_for_agent_a"
                if slot == SLOT_AGENT_A
                else "questions_for_agent_b",
                [],
            ),
            "common_questions": lead_review.get("common_questions", []),
        }
        _emit(
            on_event,
            level=EVENT_LEVEL_INFO,
            action=f"{event_slot}-round2-started",
            message=(
                f"deliberation {run.deliberation_id}: {slot} round 2 started "
                f"({seat.provider})"
            ),
            stamp=self._clock,
        )
        if not seat.enabled:
            artifact = self._artifact(
                run,
                stage,
                slot,
                self._stage_failure_content(
                    seat,
                    run,
                    round_no=2,
                    reason="provider disabled: no call was made",
                ),
                DeliberationStageStatus.ABSTAINED,
            )
        else:
            try:
                result = seat.agent.reconsider(
                    AgentReconsiderQuery(
                        project=run.project,
                        requirement=run.requirement,
                        deliberation_id=run.deliberation_id,
                        slot=slot,
                        own_round1=dict(own_round1.content),
                        lead_critique=critique,
                        peer_claims=peer_claims(peer_round1.content),
                        context=context,
                    )
                )
            except Exception as error:  # noqa: BLE001 - one seat never blocks another
                artifact = self._artifact(
                    run,
                    stage,
                    slot,
                    self._stage_failure_content(
                        seat,
                        run,
                        round_no=2,
                        reason=f"provider failed: {exception_reason(error)}",
                    ),
                    DeliberationStageStatus.ERROR,
                )
            else:
                content, failure = self._round_content(
                    seat=seat,
                    run=run,
                    raw=result.content,
                    round_no=2,
                    slot=slot,
                    source=result.source,
                    links_to_round1_id=own_round1.artifact_id,
                )
                if failure is not None:
                    artifact = self._artifact(
                        run,
                        stage,
                        slot,
                        self._stage_failure_content(
                            seat, run, round_no=2, reason=failure
                        ),
                        DeliberationStageStatus.ERROR,
                    )
                else:
                    content["cost"] = dict(result.cost)
                    artifact = self._artifact(
                        run,
                        stage,
                        slot,
                        content,
                        DeliberationStageStatus.COMPLETE,
                    )
        _emit(
            on_event,
            level=EVENT_LEVEL_INFO if artifact.is_usable else EVENT_LEVEL_WARN,
            action=f"{event_slot}-round2-completed",
            message=(
                f"deliberation {run.deliberation_id}: {slot} round 2 "
                f"{artifact.status.value}"
            ),
            stamp=self._clock,
        )
        return artifact

    # -- stage 2: the lead review -----------------------------------------

    def generate_lead_review(
        self,
        deliberation_id: str,
        *,
        actor: str,
        reason: str,
        context: Optional[Mapping[str, Any]] = None,
        on_event: Optional[Callable[[Mapping[str, Any]], None]] = None,
    ) -> DeliberationRun:
        """The chair reviews both Round-1 proposals and produces the packet source.

        The chair receives both **structured** Round-1 results and the bounded
        context - no hidden reasoning, no provider system prompt and no credential.
        With only one usable Round-1 result the review is recorded as
        ``INSUFFICIENT_PEER_REVIEW`` and **no call is made**: a one-architect board
        must never be presented as a two-agent deliberation.
        """
        run = self._require_stage(
            deliberation_id,
            (
                DeliberationStatus.ROUND1_COMPLETE,
                DeliberationStatus.LEAD_REVIEW_RUNNING,
                # an explicit operator retry after a failed review: the artifact
                # checks below still enforce that both round-1 answers exist
                DeliberationStatus.ERROR,
            ),
            "the lead review",
        )
        self._require_actor(actor)
        self._require_reason(reason)
        bounded = self._context_for(run, context)
        now = self._clock()
        stage = DeliberationStage.LEAD_REVIEW
        self._assert_chair_matches(run)
        self._assert_seat_matches(run, self._agent_a)
        self._assert_seat_matches(run, self._agent_b)

        agent_a = self.latest_artifact(
            deliberation_id, DeliberationStage.AGENT_A_ROUND1, SLOT_AGENT_A
        )
        agent_b = self.latest_artifact(
            deliberation_id, DeliberationStage.AGENT_B_ROUND1, SLOT_AGENT_B
        )
        missing = [
            slot
            for slot, artifact in (
                (SLOT_AGENT_A, agent_a),
                (SLOT_AGENT_B, agent_b),
            )
            if artifact is None or not artifact.is_usable
        ]
        if missing:
            shortage = (
                "insufficient peer review: no usable round 1 answer from "
                + ", ".join(missing)
            )
            content = self._stage_failure_content(
                self._agent_a, run, round_no=1, reason=shortage
            )
            artifact = self._artifact(
                run,
                stage,
                SLOT_LEAD,
                content,
                DeliberationStageStatus.INSUFFICIENT_PEER_REVIEW,
            )
            finished = self._moved(
                run,
                DeliberationStatus.ERROR,
                now=self._clock(),
                error_reason=shortage,
            )
            self._store(
                finished,
                (artifact,),
                self._entry(
                    finished,
                    action=AuditAction.UPDATE,
                    operation="generate-lead-review",
                    actor=actor,
                    reason=reason,
                    now=self._clock(),
                    detail={
                        "stage": stage.value,
                        "status": artifact.status.value,
                        "missing": missing,
                    },
                ),
            )
            _emit(
                on_event,
                level=EVENT_LEVEL_ERROR,
                action="lead-review-completed",
                message=(
                    f"deliberation {run.deliberation_id}: lead review "
                    "INSUFFICIENT_PEER_REVIEW (missing "
                    + ", ".join(missing)
                    + ")"
                ),
                stamp=self._clock,
            )
            return finished

        running = self._moved(run, DeliberationStatus.LEAD_REVIEW_RUNNING, now=now)
        self._persist_run(running)
        _emit(
            on_event,
            level=EVENT_LEVEL_INFO,
            action="lead-review-started",
            message=(
                f"deliberation {run.deliberation_id}: lead review started "
                f"({self._chair.provider})"
            ),
            stamp=self._clock,
        )

        artifact, content = self._lead_call(run, stage, bounded, agent_a, agent_b)
        status = (
            DeliberationStatus.LEAD_REVIEW_COMPLETE
            if artifact.is_usable
            else DeliberationStatus.ERROR
        )
        finished = self._moved(
            running,
            status,
            now=self._clock(),
            error_reason=(
                "" if artifact.is_usable else str(content.get("reason", ""))
            ),
        )
        self._store(
            finished,
            (artifact,),
            self._entry(
                finished,
                action=AuditAction.UPDATE,
                operation="generate-lead-review",
                actor=actor,
                reason=reason,
                now=self._clock(),
                detail={
                    "stage": stage.value,
                    "status": artifact.status.value,
                    "lead_review_id": content.get("lead_review_id", ""),
                    "conflicts": len(content.get("conflicts") or ()),
                    "unresolved_questions": len(
                        content.get("unresolved_questions") or ()
                    ),
                },
            ),
        )
        _emit(
            on_event,
            level=(EVENT_LEVEL_INFO if artifact.is_usable else EVENT_LEVEL_WARN),
            action="lead-review-completed",
            message=(
                f"deliberation {run.deliberation_id}: lead review "
                f"{artifact.status.value}"
            ),
            stamp=self._clock,
        )
        return finished

    def _lead_call(
        self,
        run: DeliberationRun,
        stage: DeliberationStage,
        context: Mapping[str, Any],
        agent_a: Optional[DeliberationArtifact],
        agent_b: Optional[DeliberationArtifact],
    ) -> tuple[DeliberationArtifact, dict[str, Any]]:
        """Call the chair once, or record why it could not be called.

        A chair whose provider is not configured is refused **without any call**;
        every advisor result stays exactly as it was.
        """
        if not self._chair.enabled:
            content = {
                "schema_version": DELIBERATION_SCHEMA_VERSION,
                "review_id": run.deliberation_id,
                "stage": stage.value,
                "provider": self._chair.provider,
                "model": self._chair.model,
                "requirement": run.requirement,
                "reason": "lead provider not configured: no call was made",
            }
            return (
                self._artifact(
                    run, stage, SLOT_LEAD, content, DeliberationStageStatus.ERROR
                ),
                content,
            )
        try:
            result = self._chair.lead.review(
                LeadReviewQuery(
                    project=run.project,
                    requirement=run.requirement,
                    deliberation_id=run.deliberation_id,
                    agent_a=dict(agent_a.content),
                    agent_b=dict(agent_b.content),
                    context=context,
                )
            )
        except Exception as error:  # noqa: BLE001 - a chair failure is recorded
            content = {
                "schema_version": DELIBERATION_SCHEMA_VERSION,
                "review_id": run.deliberation_id,
                "stage": stage.value,
                "provider": self._chair.provider,
                "model": self._chair.model,
                "requirement": run.requirement,
                "reason": f"lead provider failed: {exception_reason(error)}",
            }
            return (
                self._artifact(
                    run, stage, SLOT_LEAD, content, DeliberationStageStatus.ERROR
                ),
                content,
            )
        try:
            content = _lead_review_content(
                requirement=run.requirement,
                deliberation_id=run.deliberation_id,
                source=result.source,
                provider=self._chair.provider,
                model=self._chair.model,
                raw=result.content,
            )
        except DeliberationResponseError as error:
            content = {
                "schema_version": DELIBERATION_SCHEMA_VERSION,
                "review_id": run.deliberation_id,
                "stage": stage.value,
                "provider": self._chair.provider,
                "model": self._chair.model,
                "requirement": run.requirement,
                "reason": "malformed answer: " + type(error).__name__,
            }
            return (
                self._artifact(
                    run, stage, SLOT_LEAD, content, DeliberationStageStatus.ERROR
                ),
                content,
            )
        content["cost"] = dict(result.cost)
        return (
            self._artifact(
                run, stage, SLOT_LEAD, content, DeliberationStageStatus.COMPLETE
            ),
            content,
        )

    # -- stage 3: round 2 (one bounded reconsideration) --------------------

    def run_round2(
        self,
        deliberation_id: str,
        *,
        actor: str,
        reason: str,
        context: Optional[Mapping[str, Any]] = None,
        on_event: Optional[Callable[[Mapping[str, Any]], None]] = None,
    ) -> DeliberationRun:
        """Round 2: each architect reconsiders **its own** answer - exactly once.

        After this call the run is ``ROUND2_COMPLETE`` and the stage can never run
        again: the run has no transition back to ``ROUND2_RUNNING``, so there is no
        ``A -> B -> A -> B`` debate and ``max_review_rounds`` stays one.
        """
        run = self._require_stage(
            deliberation_id,
            (
                DeliberationStatus.LEAD_REVIEW_COMPLETE,
                DeliberationStatus.ROUND2_RUNNING,
                DeliberationStatus.ERROR,
            ),
            "round 2",
        )
        self._require_actor(actor)
        self._require_reason(reason)
        bounded = self._context_for(run, context)
        stage = DeliberationStage.AGENT_A_ROUND2

        lead_review = self.latest_artifact(
            deliberation_id, DeliberationStage.LEAD_REVIEW, SLOT_LEAD
        )
        a1 = self.latest_artifact(
            deliberation_id, DeliberationStage.AGENT_A_ROUND1, SLOT_AGENT_A
        )
        b1 = self.latest_artifact(
            deliberation_id, DeliberationStage.AGENT_B_ROUND1, SLOT_AGENT_B
        )
        if (
            lead_review is None
            or not lead_review.is_usable
            or a1 is None
            or not a1.is_usable
            or b1 is None
            or not b1.is_usable
        ):
            raise DeliberationStageOrderError(
                "round 2 needs a complete lead review and both round 1 answers"
            )

        now = self._clock()
        running = self._moved(run, DeliberationStatus.ROUND2_RUNNING, now=now)
        self._persist_run(running)
        _emit(
            on_event,
            level=EVENT_LEVEL_INFO,
            action="round2-started",
            message=f"deliberation {run.deliberation_id}: round 2 started",
            stamp=self._clock,
        )

        produced: list[DeliberationArtifact] = []
        for seat, own, peer in (
            (self._agent_a, a1, b1),
            (self._agent_b, b1, a1),
        ):
            self._assert_seat_matches(run, seat)
            round2_stage = (
                DeliberationStage.AGENT_A_ROUND2
                if seat.slot == SLOT_AGENT_A
                else DeliberationStage.AGENT_B_ROUND2
            )
            produced.append(
                self._reconsider(
                    run,
                    seat,
                    round2_stage,
                    bounded,
                    lead_review=lead_review.content,
                    own_round1=own,
                    peer_round1=peer,
                    on_event=on_event,
                )
            )

        usable = [artifact for artifact in produced if artifact.is_usable]
        finished = self._moved(
            running,
            (
                DeliberationStatus.ROUND2_COMPLETE
                if usable
                else DeliberationStatus.ERROR
            ),
            now=self._clock(),
            error_reason=(
                ""
                if usable
                else "round 2 produced no usable reconsideration"
            ),
        )
        self._store(
            finished,
            tuple(produced),
            self._entry(
                finished,
                action=AuditAction.UPDATE,
                operation="run-round2",
                actor=actor,
                reason=reason,
                now=self._clock(),
                detail={
                    "stage": "round2",
                    "usable": len(usable),
                    "decisions": [
                        artifact.content.get("decision", "")
                        for artifact in produced
                    ],
                    "statuses": [
                        artifact.status.value for artifact in produced
                    ],
                },
            ),
        )
        _emit(
            on_event,
            level=(EVENT_LEVEL_INFO if usable else EVENT_LEVEL_ERROR),
            action="round2-completed",
            message=(
                f"deliberation {run.deliberation_id}: round 2 complete "
                f"({len(usable)}/{len(produced)} usable)"
            ),
            stamp=self._clock,
        )
        return finished


    # -- stage 4: the final synthesis --------------------------------------

    def generate_final_synthesis(
        self,
        deliberation_id: str,
        *,
        actor: str,
        reason: str,
        context: Optional[Mapping[str, Any]] = None,
        on_event: Optional[Callable[[Mapping[str, Any]], None]] = None,
    ) -> DeliberationRun:
        """The chair synthesizes the **exact** five upstream results into a design.

        Two things are deliberately *not* left to the provider. First, the
        synthesis identity is derived from the upstream artifact fingerprints, so a
        synthesis can never be attached to a different set of answers. Second, the
        remaining disagreements and the advisor-change summary are computed **here**
        from the Round-2 answers and written over whatever the chair claimed - so a
        chair cannot report a consensus that does not exist.
        """
        run = self._require_stage(
            deliberation_id,
            (
                DeliberationStatus.ROUND2_COMPLETE,
                DeliberationStatus.SYNTHESIS_RUNNING,
                DeliberationStatus.ERROR,
            ),
            "the final synthesis",
        )
        self._require_actor(actor)
        self._require_reason(reason)
        bounded = self._context_for(run, context)
        stage = DeliberationStage.FINAL_SYNTHESIS
        self._assert_chair_matches(run)
        self._assert_seat_matches(run, self._agent_a)
        self._assert_seat_matches(run, self._agent_b)
        upstream: list[DeliberationArtifact] = []
        for upstream_stage, slot in _UPSTREAM_STAGES:
            artifact = self.latest_artifact(deliberation_id, upstream_stage, slot)
            if artifact is None or not artifact.is_usable:
                raise DeliberationStageOrderError(
                    "the final synthesis needs a complete "
                    f"{upstream_stage.value} result"
                )
            upstream.append(artifact)

        now = self._clock()
        running = self._moved(run, DeliberationStatus.SYNTHESIS_RUNNING, now=now)
        self._persist_run(running)
        _emit(
            on_event,
            level=EVENT_LEVEL_INFO,
            action="final-synthesis-started",
            message=(
                f"deliberation {run.deliberation_id}: final synthesis started "
                f"({self._chair.provider})"
            ),
            stamp=self._clock,
        )

        artifact, content = self._synthesis_call(run, stage, bounded, upstream)
        status = (
            DeliberationStatus.READY_FOR_PROPOSAL
            if artifact.is_usable
            else DeliberationStatus.ERROR
        )
        finished = self._moved(
            running,
            status,
            now=self._clock(),
            error_reason=(
                "" if artifact.is_usable else str(content.get("reason", ""))
            ),
        )
        self._store(
            finished,
            (artifact,),
            self._entry(
                finished,
                action=AuditAction.UPDATE,
                operation="generate-final-synthesis",
                actor=actor,
                reason=reason,
                now=self._clock(),
                detail={
                    "stage": stage.value,
                    "status": artifact.status.value,
                    "synthesis_id": content.get("synthesis_id", ""),
                    "upstream": [entry.artifact_id for entry in upstream],
                    "upstream_fingerprints": [
                        entry.fingerprint for entry in upstream
                    ],
                    "remaining_disagreements": len(
                        content.get("remaining_disagreements") or ()
                    ),
                    "consensus": bool(content.get("consensus", False)),
                },
            ),
        )
        _emit(
            on_event,
            level=(EVENT_LEVEL_INFO if artifact.is_usable else EVENT_LEVEL_WARN),
            action="final-synthesis-completed",
            message=(
                f"deliberation {run.deliberation_id}: final synthesis "
                f"{artifact.status.value} ({content.get('synthesis_id', '?')})"
            ),
            stamp=self._clock,
        )
        return finished

    def _disagreement_evidence(
        self, upstream: Sequence[DeliberationArtifact]
    ) -> dict[str, Any]:
        """Compute - never ask - what actually remained unresolved.

        Every architect's own ``remaining_disagreements`` and
        ``remaining_questions`` are carried forward, and each seat's Round-2
        ``decision`` is recorded. This is the deterministic half of the "no phantom
        consensus" rule: the chair's answer is merged *into* this, not the other way
        round, so a chair cannot report agreement that the answers do not support.
        """
        disagreements: list[str] = []
        questions: list[str] = []
        decisions: dict[str, str] = {}
        changed: list[Any] = []
        for artifact in upstream:
            content = artifact.content
            decision = content.get("decision")
            if decision:
                decisions[artifact.slot] = str(decision)
                if str(decision) == Round2Decision.WITHDRAW.value:
                    withdrawn = f"{artifact.slot} withdrew its proposal"
                    if withdrawn not in disagreements:
                        disagreements.append(withdrawn)
                changed.extend(content.get("changed_decisions") or ())
            for text in content.get("remaining_disagreements") or ():
                if str(text) not in disagreements:
                    disagreements.append(str(text))
            for text in content.get("remaining_questions") or ():
                if str(text) not in questions:
                    questions.append(str(text))
        return {
            "remaining_disagreements": disagreements,
            "remaining_questions": questions,
            "decisions": decisions,
            "changed_decisions": changed,
        }

    def _synthesis_call(
        self,
        run: DeliberationRun,
        stage: DeliberationStage,
        context: Mapping[str, Any],
        upstream: Sequence[DeliberationArtifact],
    ) -> tuple[DeliberationArtifact, dict[str, Any]]:
        """Call the chair for the synthesis, then apply the deterministic half."""
        fingerprints = tuple(artifact.fingerprint for artifact in upstream)
        by_stage = {artifact.stage: artifact for artifact in upstream}
        evidence = self._disagreement_evidence(upstream)
        if not self._chair.enabled:
            content = {
                "schema_version": DELIBERATION_SCHEMA_VERSION,
                "review_id": run.deliberation_id,
                "stage": stage.value,
                "provider": self._chair.provider,
                "model": self._chair.model,
                "requirement": run.requirement,
                "reason": "lead provider not configured: no call was made",
            }
            return (
                self._artifact(
                    run, stage, SLOT_LEAD, content, DeliberationStageStatus.ERROR
                ),
                content,
            )
        try:
            result = self._chair.lead.synthesize(
                LeadSynthesisQuery(
                    project=run.project,
                    requirement=run.requirement,
                    deliberation_id=run.deliberation_id,
                    agent_a_round1=dict(
                        by_stage[DeliberationStage.AGENT_A_ROUND1].content
                    ),
                    agent_b_round1=dict(
                        by_stage[DeliberationStage.AGENT_B_ROUND1].content
                    ),
                    lead_review=dict(
                        by_stage[DeliberationStage.LEAD_REVIEW].content
                    ),
                    agent_a_round2=dict(
                        by_stage[DeliberationStage.AGENT_A_ROUND2].content
                    ),
                    agent_b_round2=dict(
                        by_stage[DeliberationStage.AGENT_B_ROUND2].content
                    ),
                    context=context,
                )
            )
        except Exception as error:  # noqa: BLE001 - a chair failure is recorded
            content = {
                "schema_version": DELIBERATION_SCHEMA_VERSION,
                "review_id": run.deliberation_id,
                "stage": stage.value,
                "provider": self._chair.provider,
                "model": self._chair.model,
                "requirement": run.requirement,
                "reason": f"lead provider failed: {exception_reason(error)}",
            }
            return (
                self._artifact(
                    run, stage, SLOT_LEAD, content, DeliberationStageStatus.ERROR
                ),
                content,
            )
        try:
            content = _synthesis_content(
                requirement=run.requirement,
                deliberation_id=run.deliberation_id,
                source=result.source,
                provider=self._chair.provider,
                model=self._chair.model,
                upstream_fingerprints=fingerprints,
                raw=result.content,
            )
        except DeliberationResponseError as error:
            content = {
                "schema_version": DELIBERATION_SCHEMA_VERSION,
                "review_id": run.deliberation_id,
                "stage": stage.value,
                "provider": self._chair.provider,
                "model": self._chair.model,
                "requirement": run.requirement,
                "reason": "malformed answer: " + type(error).__name__,
            }
            return (
                self._artifact(
                    run, stage, SLOT_LEAD, content, DeliberationStageStatus.ERROR
                ),
                content,
            )
        # The deterministic half wins: a remaining disagreement the answers prove
        # is carried through whether or not the chair mentioned it, and the
        # advisor-change summary is the seats' own recorded decisions.
        content["remaining_disagreements"] = _merge_texts(
            content.get("remaining_disagreements") or (),
            evidence["remaining_disagreements"],
        )
        content["unresolved_questions"] = _merge_texts(
            content.get("unresolved_questions") or (),
            evidence["remaining_questions"],
        )
        content["advisor_change_summary"] = {
            "decisions": dict(evidence["decisions"]),
            "changed_decisions": list(evidence["changed_decisions"]),
        }
        content["consensus"] = not content["remaining_disagreements"]
        content["cost"] = dict(result.cost)
        return (
            self._artifact(
                run, stage, SLOT_LEAD, content, DeliberationStageStatus.COMPLETE
            ),
            content,
        )

    # -- integrity: seats, staleness --------------------------------------

    def _assert_seat_matches(
        self, run: DeliberationRun, seat: DeliberationSeat
    ) -> None:
        """Refuse to answer a seat with a *different* provider than recorded.

        A run's identity includes the three configured provider/model pairs, so
        answering its round 1 with another provider would make the stored identity
        a lie. Changing the configuration is legal - it simply affects the **next**
        deliberation.
        """
        recorded = (
            (run.agent_a_provider, run.agent_a_model)
            if seat.slot == SLOT_AGENT_A
            else (run.agent_b_provider, run.agent_b_model)
        )
        if (seat.provider, _recorded_model(seat.model)) != recorded:
            raise DeliberationStaleError(
                f"deliberation {run.deliberation_id} was fingerprinted with "
                f"{recorded[0]}:{recorded[1]} in the {seat.slot} seat; "
                f"the configured seat is {seat.provider}:{seat.model}"
            )

    def _assert_chair_matches(self, run: DeliberationRun) -> None:
        """Refuse a chair whose provider/model is not the recorded one."""
        if (self._chair.provider, _recorded_model(self._chair.model)) != (
            run.lead_provider,
            run.lead_model,
        ):
            raise DeliberationStaleError(
                f"deliberation {run.deliberation_id} was fingerprinted with "
                f"{run.lead_provider}:{run.lead_model} as the lead; the "
                f"configured chair is {self._chair.provider}:{self._chair.model}"
            )

    def _stale_reason(self, run: DeliberationRun) -> str:
        """Why this run's downstream artifacts can no longer be trusted, if so.

        Three independent guards, all recomputed from stored content:

        * the run's own material inputs must still fingerprint to its recorded
          identity (a hand-edited requirement or provider pair is refused);
        * every upstream artifact and the synthesis must still be present and its
          content must still hash to its recorded fingerprint (a rewritten row is
          refused);
        * the synthesis identity must still be exactly what the current upstream
          fingerprints derive (a synthesis attached to a different set of answers
          is refused).
        """
        recomputed = deliberation_fingerprint(
            project=run.project,
            requirement=run.requirement,
            agent_a_provider=run.agent_a_provider,
            agent_a_model=run.agent_a_model,
            agent_b_provider=run.agent_b_provider,
            agent_b_model=run.agent_b_model,
            lead_provider=run.lead_provider,
            lead_model=run.lead_model,
            context_fingerprint=run.context_fingerprint,
            revision_no=run.revision_no,
        )
        if recomputed != run.fingerprint:
            return "the deliberation's material inputs no longer match its identity"
        stages = (*_UPSTREAM_STAGES, (DeliberationStage.FINAL_SYNTHESIS, SLOT_LEAD))
        for stage, slot in stages:
            artifact = self.latest_artifact(run.deliberation_id, stage, slot)
            if artifact is None:
                return f"{stage.value} is missing"
            if artifact.fingerprint != _content_fingerprint(
                run.deliberation_id, stage, slot, artifact.content
            ):
                return f"{stage.value} was rewritten after it was recorded"
            if not artifact.is_usable:
                return f"{stage.value} is not usable"
        upstream_fingerprints = tuple(
            self.latest_artifact(run.deliberation_id, stage, slot).fingerprint
            for stage, slot in _UPSTREAM_STAGES
        )
        synthesis = self.latest_artifact(
            run.deliberation_id, DeliberationStage.FINAL_SYNTHESIS, SLOT_LEAD
        )
        expected = _final_synthesis_id(run.deliberation_id, upstream_fingerprints)
        if synthesis.content.get("synthesis_id") != expected:
            return (
                "the final synthesis no longer matches its upstream answers"
            )
        return ""

    # -- stage 5: the architecture proposal (never an approval) ------------

    def generate_proposal(
        self,
        deliberation_id: str,
        *,
        actor: str,
        reason: str,
        architecture_version: Optional[str] = None,
        on_event: Optional[Callable[[Mapping[str, Any]], None]] = None,
    ) -> ArchitectureProposal:
        """Turn the **exact** final synthesis into a ``DRAFT`` proposal.

        The proposal remains unapproved: only a human decision through the
        unchanged ``ProposalApproval`` path can accept it. ``source_review_id``
        carries the final synthesis id, so the existing fingerprint and staleness
        machinery protects the proposal unchanged.
        """
        run = self._require_stage(
            deliberation_id,
            (DeliberationStatus.READY_FOR_PROPOSAL,),
            "generating an architecture proposal",
        )
        self._require_actor(actor)
        self._require_reason(reason)
        stale = self._stale_reason(run)
        if stale:
            raise DeliberationStaleError(
                f"deliberation {run.deliberation_id} is stale: {stale}"
            )
        if self._project_name(None) != run.project:
            raise DeliberationStaleError(
                f"deliberation {run.deliberation_id} belongs to project "
                f"{run.project!r}, which this database no longer manages"
            )
        synthesis = self.latest_artifact(
            deliberation_id, DeliberationStage.FINAL_SYNTHESIS, SLOT_LEAD
        )
        content = dict(synthesis.content)
        synthesis_id = str(content.get("synthesis_id", ""))
        version = None if architecture_version is None else str(architecture_version)
        digest = self._proposal_digest(run, synthesis, architecture_version=version)
        now = self._clock()
        questions = tuple(
            str(question)
            for question in (content.get("unresolved_questions") or ())
            if str(question).strip()
        )
        proposal = ArchitectureProposal(
            proposal_id="proposal-"
            + _digest(run.project, synthesis_id, run.requirement),
            project=run.project,
            created_at=now,
            requirement=run.requirement,
            summary=str(content.get("summary") or ""),
            source_review_id=synthesis_id,
            fingerprint=proposal_fingerprint(
                project=run.project,
                review_id=synthesis_id,
                architecture_version=version,
                requirement=run.requirement,
                revision_no=1,
                digest=digest,
            ),
            modules=tuple(content.get("modules") or ()),
            data_flows=tuple(content.get("data_flows") or ()),
            external_dependencies=tuple(
                content.get("external_dependencies") or ()
            ),
            risks=tuple(content.get("risks") or ()),
            adr_candidates=tuple(content.get("adr_candidates") or ()),
            implementation_phases=tuple(
                content.get("implementation_phases") or ()
            ),
            unresolved_questions=questions,
            rationale=str(content.get("rationale") or ""),
            review_digest=digest,
            architecture_version=version,
        )
        return self._store_proposal(
            run, proposal, synthesis_id, content, actor, reason, now, on_event
        )

    def _store_proposal(
        self,
        run: DeliberationRun,
        proposal: ArchitectureProposal,
        synthesis_id: str,
        content: Mapping[str, Any],
        actor: str,
        reason: str,
        now: datetime,
        on_event: Optional[Callable[[Mapping[str, Any]], None]],
    ) -> ArchitectureProposal:
        """Write the proposal and its two audit entries in one transaction."""
        with self._transactions.transaction():
            self._proposals.upsert(proposal)
            self._audit.append(
                AuditEntry(
                    entity_type=AuditEntityType.PROPOSAL,
                    entity_id=proposal.proposal_id,
                    action=AuditAction.CREATE,
                    detail={
                        "operation": "generate-proposal",
                        "actor": actor,
                        "reason": reason,
                        "project": proposal.project,
                        "source_review_id": proposal.source_review_id,
                        "deliberation_id": run.deliberation_id,
                        "architecture_version": proposal.architecture_version,
                        "revision_no": proposal.revision_no,
                        "status": proposal.status.value,
                        "fingerprint": proposal.fingerprint,
                        "synthesis_source": "architecture-deliberation",
                        "provider_count": 3,
                        "module_count": len(proposal.modules),
                        "risk_count": len(proposal.risks),
                        "adr_candidate_count": len(proposal.adr_candidates),
                        "unresolved_question_count": len(
                            proposal.unresolved_questions
                        ),
                    },
                    created_at=now,
                )
            )
            self._audit.append(
                self._entry(
                    run,
                    action=AuditAction.UPDATE,
                    operation="generate-proposal",
                    actor=actor,
                    reason=reason,
                    now=now,
                    detail={
                        "proposal_id": proposal.proposal_id,
                        "synthesis_id": synthesis_id,
                        "fingerprint": proposal.fingerprint,
                        "module_count": len(proposal.modules),
                        "remaining_disagreements": len(
                            content.get("remaining_disagreements") or ()
                        ),
                    },
                )
            )
        _emit(
            on_event,
            level=EVENT_LEVEL_INFO,
            action="proposal-generated",
            message=(
                f"deliberation {run.deliberation_id}: proposal "
                f"{proposal.proposal_id} generated for {proposal.project} "
                f"(status {proposal.status.value}, "
                f"{len(proposal.modules)} module(s))"
            ),
            stamp=self._clock,
        )
        return proposal

    def _proposal_digest(
        self,
        run: DeliberationRun,
        synthesis: DeliberationArtifact,
        *,
        architecture_version: Optional[str],
    ) -> dict[str, Any]:
        """The bounded, JSON-safe digest of exactly what the proposal rests on."""

        def stage_of(stage: DeliberationStage, slot: str) -> dict[str, Any]:
            artifact = self.latest_artifact(run.deliberation_id, stage, slot)
            if artifact is None:
                return {}
            return {
                "artifact_id": artifact.artifact_id,
                "status": artifact.status.value,
                "fingerprint": artifact.fingerprint,
                "provider": str(artifact.content.get("provider", "")),
                "model": str(artifact.content.get("model", "")),
            }

        content = synthesis.content
        return {
            "schema_version": DELIBERATION_SCHEMA_VERSION,
            "source": "architecture-deliberation",
            "deliberation_id": run.deliberation_id,
            "deliberation_fingerprint": run.fingerprint,
            "context_fingerprint": run.context_fingerprint,
            "synthesis_id": str(content.get("synthesis_id", "")),
            "project": run.project,
            "requirement": run.requirement,
            "architecture_version": architecture_version,
            "max_review_rounds": run.max_review_rounds,
            "revision_no": run.revision_no,
            "agent_a": stage_of(DeliberationStage.AGENT_A_ROUND1, SLOT_AGENT_A),
            "agent_b": stage_of(DeliberationStage.AGENT_B_ROUND1, SLOT_AGENT_B),
            "lead_review": stage_of(DeliberationStage.LEAD_REVIEW, SLOT_LEAD),
            "agent_a_round2": stage_of(
                DeliberationStage.AGENT_A_ROUND2, SLOT_AGENT_A
            ),
            "agent_b_round2": stage_of(
                DeliberationStage.AGENT_B_ROUND2, SLOT_AGENT_B
            ),
            "final_synthesis": stage_of(
                DeliberationStage.FINAL_SYNTHESIS, SLOT_LEAD
            ),
            "advisor_change_summary": dict(
                content.get("advisor_change_summary") or {}
            ),
            "remaining_disagreements": [
                str(item)
                for item in (content.get("remaining_disagreements") or ())
            ],
            "unresolved_questions": [
                str(item) for item in (content.get("unresolved_questions") or ())
            ],
            "consensus": bool(content.get("consensus", False)),
            "cost": self._cost(self.artifacts(run.deliberation_id)),
            "provider_count": 3,
            "module_count": len(content.get("modules") or ()),
            "risk_count": len(content.get("risks") or ()),
            "adr_candidate_count": len(content.get("adr_candidates") or ()),
        }

    # -- the read-only snapshot --------------------------------------------

    def snapshot(self, deliberation_id: str) -> RunSnapshot:
        """Everything the panel (or a report) needs, as plain JSON-safe data.

        It decides nothing and writes nothing: it reports the run, the six stage
        statuses, the compact counts, the per-stage cost, the artifact addresses
        and exactly which actions are valid now.
        """
        run = self.require(deliberation_id)
        artifacts = self.artifacts(deliberation_id)
        return RunSnapshot(
            deliberation_id=run.deliberation_id,
            project=run.project,
            requirement=run.requirement,
            status=run.status.value,
            fingerprint=run.fingerprint,
            context_fingerprint=run.context_fingerprint,
            max_review_rounds=run.max_review_rounds,
            revision_no=run.revision_no,
            revision_of_deliberation_id=run.revision_of_deliberation_id,
            error_reason=run.error_reason,
            stale_reason=(
                self._stale_reason(run)
                if run.status is DeliberationStatus.READY_FOR_PROPOSAL
                else ""
            ),
            seats={
                SLOT_AGENT_A: {
                    "provider": run.agent_a_provider,
                    "model": run.agent_a_model,
                    "enabled": self._agent_a.enabled,
                },
                SLOT_AGENT_B: {
                    "provider": run.agent_b_provider,
                    "model": run.agent_b_model,
                    "enabled": self._agent_b.enabled,
                },
                SLOT_LEAD: {
                    "provider": run.lead_provider,
                    "model": run.lead_model,
                    "enabled": self._chair.enabled,
                },
            },
            stages=self._stages(artifacts),
            counts=self._counts(artifacts),
            cost=self._cost(artifacts),
            artifacts=tuple(artifact.to_dict() for artifact in artifacts),
            available_actions=self._actions(run, artifacts),
            ready_for_proposal=(
                run.status is DeliberationStatus.READY_FOR_PROPOSAL
            ),
            final_synthesis_id=self._synthesis_id(run, artifacts),
        )

    def _synthesis_id(
        self, run: DeliberationRun, artifacts: Sequence[DeliberationArtifact]
    ) -> Optional[str]:
        """The final synthesis id when one exists (the exact upstream identity)."""
        synthesis = self.latest_artifact(
            run.deliberation_id, DeliberationStage.FINAL_SYNTHESIS, SLOT_LEAD
        )
        if synthesis is None:
            return None
        return str(synthesis.content.get("synthesis_id", "")) or None

    # -- compact views and the one human stop ------------------------------

    def _stages(
        self, artifacts: Sequence[DeliberationArtifact]
    ) -> dict[str, Any]:
        """One compact record per stage, for the panel's progress checklist."""
        stages: dict[str, Any] = {
            stage.value: {"status": DeliberationStageStatus.PENDING.value}
            for stage in DeliberationStage
        }
        for artifact in artifacts:
            content = artifact.content
            stages[artifact.stage.value] = {
                "status": artifact.status.value,
                "artifact_id": artifact.artifact_id,
                "slot": artifact.slot,
                "fingerprint": artifact.fingerprint,
                "provider": str(content.get("provider", "")),
                "model": str(content.get("model", "")),
                "reason": str(content.get("reason", "")),
                "decision": str(content.get("decision", "")),
                "synthesis_id": str(content.get("synthesis_id", "")),
            }
        return stages

    def _counts(
        self, artifacts: Sequence[DeliberationArtifact]
    ) -> dict[str, int]:
        """The compact counts the panel shows - computed, never guessed."""
        lead = next(
            (
                artifact
                for artifact in artifacts
                if artifact.stage is DeliberationStage.LEAD_REVIEW
                and artifact.is_usable
            ),
            None,
        )
        synthesis = next(
            (
                artifact
                for artifact in artifacts
                if artifact.stage is DeliberationStage.FINAL_SYNTHESIS
                and artifact.is_usable
            ),
            None,
        )
        round1 = [
            artifact
            for artifact in artifacts
            if artifact.stage
            in (
                DeliberationStage.AGENT_A_ROUND1,
                DeliberationStage.AGENT_B_ROUND1,
            )
            and artifact.is_usable
        ]
        if lead is not None:
            agreements = len(lead.content.get("agreements") or ())
            conflicts = len(lead.content.get("conflicts") or ())
            open_questions = len(lead.content.get("unresolved_questions") or ())
        else:
            agreements = 0
            conflicts = 0
            open_questions = len(
                {
                    str(question)
                    for artifact in round1
                    for question in (artifact.content.get("open_questions") or ())
                }
            )
        if synthesis is not None:
            risks = len(synthesis.content.get("risks") or ())
            disagreements = len(
                synthesis.content.get("remaining_disagreements") or ()
            )
        else:
            risks = len(
                {
                    json.dumps(dict(entry), sort_keys=True, default=str)
                    for artifact in round1
                    for entry in (artifact.content.get("risks") or ())
                    if isinstance(entry, Mapping)
                }
            )
            disagreements = 0
        return {
            "agreements": agreements,
            "conflicts": conflicts,
            "open_questions": open_questions,
            "risks": risks,
            "remaining_disagreements": disagreements,
        }

    def _cost(self, artifacts: Sequence[DeliberationArtifact]) -> dict[str, Any]:
        """Per-stage cost, with an honest total.

        A stage that reported no telemetry stays ``available: False``, and when no
        stage reported any the total is ``None`` with a reason - a missing cost is
        never presented as a real zero-dollar cost.
        """
        stages: list[dict[str, Any]] = []
        priced = 0
        unpriced = 0
        total = 0.0
        for artifact in artifacts:
            cost = artifact.content.get("cost")
            available = isinstance(cost, Mapping) and bool(cost)
            amount: Optional[float] = None
            if isinstance(cost, Mapping):
                value = cost.get("total_usd")
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    amount = float(value)
            if amount is None:
                unpriced += 1
            else:
                total += amount
                priced += 1
            stages.append(
                {
                    "stage": artifact.stage.value,
                    "slot": artifact.slot,
                    "status": artifact.status.value,
                    "available": available,
                    "cost": dict(cost) if isinstance(cost, Mapping) else {},
                    "total_usd": None if amount is None else round(amount, 6),
                }
            )
        return {
            "stages": stages,
            "available": priced > 0,
            "priced_stages": priced,
            "unpriced_stages": unpriced,
            "total_usd": round(total, 6) if priced else None,
            "reason": "" if priced else "no stage reported cost telemetry",
        }

    def _actions(
        self,
        run: DeliberationRun,
        artifacts: Sequence[DeliberationArtifact],
    ) -> tuple[str, ...]:
        """Exactly the actions that are valid at this stage - and no others."""

        def artifact_of(
            stage: DeliberationStage, slot: str = ""
        ) -> Optional[DeliberationArtifact]:
            return self.latest_artifact(run.deliberation_id, stage, slot)

        status = run.status
        actions: list[str] = []
        stage_action = {
            DeliberationStatus.DRAFT: ACTION_RUN_ROUND1,
            DeliberationStatus.ROUND1_RUNNING: ACTION_RUN_ROUND1,
            DeliberationStatus.ROUND1_COMPLETE: ACTION_GENERATE_LEAD_REVIEW,
            DeliberationStatus.LEAD_REVIEW_RUNNING: ACTION_GENERATE_LEAD_REVIEW,
            DeliberationStatus.LEAD_REVIEW_COMPLETE: ACTION_RUN_ROUND2,
            DeliberationStatus.ROUND2_RUNNING: ACTION_RUN_ROUND2,
            DeliberationStatus.ROUND2_COMPLETE: ACTION_GENERATE_FINAL_SYNTHESIS,
            DeliberationStatus.SYNTHESIS_RUNNING: (
                ACTION_GENERATE_FINAL_SYNTHESIS
            ),
            DeliberationStatus.READY_FOR_PROPOSAL: ACTION_GENERATE_PROPOSAL,
        }
        if status in stage_action:
            actions.append(stage_action[status])
        elif status in (DeliberationStatus.ERROR, DeliberationStatus.CANCELLED):
            lead = artifact_of(DeliberationStage.LEAD_REVIEW, SLOT_LEAD)
            synthesis = artifact_of(
                DeliberationStage.FINAL_SYNTHESIS, SLOT_LEAD
            )
            round2_gap = (
                artifact_of(
                    DeliberationStage.AGENT_A_ROUND2, SLOT_AGENT_A
                )
                is None
                or artifact_of(
                    DeliberationStage.AGENT_B_ROUND2, SLOT_AGENT_B
                )
                is None
            )
            if lead is None:
                actions.append(ACTION_RUN_ROUND1)
            elif not lead.is_usable:
                actions.append(ACTION_GENERATE_LEAD_REVIEW)
            elif round2_gap:
                actions.append(ACTION_RUN_ROUND2)
            elif synthesis is None or not synthesis.is_usable:
                actions.append(ACTION_GENERATE_FINAL_SYNTHESIS)
            else:
                actions.append(ACTION_RUN_ROUND1)
        if status is not DeliberationStatus.CANCELLED:
            actions.append(ACTION_CANCEL)
        return tuple(actions)

    def cancel(
        self,
        deliberation_id: str,
        *,
        actor: str,
        reason: str,
        on_event: Optional[Callable[[Mapping[str, Any]], None]] = None,
    ) -> DeliberationRun:
        """Stop the run deliberately. Nothing is deleted; every stage is kept."""
        run = self.require(deliberation_id)
        self._require_actor(actor)
        self._require_reason(reason)
        if run.status is DeliberationStatus.CANCELLED:
            return run
        if run.status is DeliberationStatus.READY_FOR_PROPOSAL:
            raise DeliberationStageOrderError(
                f"deliberation {run.deliberation_id} already reached "
                "READY_FOR_PROPOSAL and cannot be cancelled"
            )
        now = self._clock()
        cancelled = self._moved(run, DeliberationStatus.CANCELLED, now=now)
        self._store(
            cancelled,
            (),
            self._entry(
                cancelled,
                action=AuditAction.UPDATE,
                operation="cancel-deliberation",
                actor=actor,
                reason=reason,
                now=now,
                detail={"from": run.status.value},
            ),
        )
        _emit(
            on_event,
            level=EVENT_LEVEL_WARN,
            action="deliberation-cancelled",
            message=(
                f"deliberation {run.deliberation_id} cancelled by {actor} "
                f"(was {run.status.value})"
            ),
            stamp=self._clock,
        )
        return cancelled


def _merge_texts(primary: Iterable[Any], extra: Iterable[Any]) -> list[str]:
    """Two sequences merged in order, deduplicated, as non-empty strings."""
    merged: list[str] = []
    for item in (*tuple(primary), *tuple(extra)):
        text = str(item)
        if text.strip() and text not in merged:
            merged.append(text)
    return merged

