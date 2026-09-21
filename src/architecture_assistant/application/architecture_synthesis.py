"""Architecture synthesis - a **managed-project** proposal, and nothing else.

What this module is
-------------------
One explicit operator action turns one advisory architecture review into an
:class:`~architecture_assistant.domain.models.ArchitectureProposal`: a durable,
project-scoped design for the project the assistant manages (for example
``youtube_to_mp3``).

The boundary this module exists to keep
---------------------------------------
``ArchitectureVersion``, ``ArchitectureEvolution``, ``canonical_versions()``,
``DECLARED_CHANGES`` and ``architecture/rules.py`` describe the architecture of
the **assistant itself**: it is declared in code and enforced by the
deterministic validator over the assistant's own source tree. A proposal
describes the architecture of the project the assistant **manages**. The two are
deliberately unrelated, so this use-case is handed no ``ArchitectureEvolution``,
no ``ArchitectureVersioning``, no ``ADRManager``, no ``RiskManager`` and no
realization port. It can therefore never mutate an ``ArchitectureVersion``,
never create an ``ArchitectureChangeRequest``, never write an assistant ADR, an
assistant risk or an assistant rule, and is never read by the realization gate.
Conflating the two would force a source edit to the assistant every time a
managed project's design was approved.

Four things it may never do
---------------------------
* **Invent.** The deterministic organizer carries only what it was given: the
  operator's requirement verbatim, the bounded evidence digest of one review and
  the review's own unresolved questions. A section it cannot derive stays empty
  and is *named* as an unresolved question instead of being guessed.
* **Vote.** Advisors are never counted into a winner, weighted or out-voted;
  every status, claim, conflict and question is carried through unchanged.
* **Rewrite the requirement.** It is echoed verbatim - the stored proposal is
  the record of exactly what the operator asked for.
* **Write anything but one proposal row and one audit entry.**

Fail-closed rules
-----------------
* No requirement at all -> refuse
  (:class:`SynthesisInsufficientEvidenceError`) before anything is stored.
* No ``SynthesisPort`` configured -> the deterministic organizer runs. That is
  the default, and it needs no provider and costs nothing.
* A configured synthesizer that fails, or answers with content the proposal
  contract refuses, raises :class:`SynthesisProviderError` /
  :class:`SynthesisResponseError` and stores **nothing**. Substituting the
  deterministic skeleton for a configured provider's answer would make the
  proposal's provenance a lie, so it is never done.

Imports are restricted to ``domain``, ``ports`` and sibling ``application``
modules - never ``infrastructure``, never a concrete adapter, never ``sqlite3``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime
from typing import Any, Callable, Mapping, Optional, Sequence

from ..domain.audit import AuditAction, AuditEntityType, AuditEntry
from ..domain.enums import Phase, ProposalStatus
from ..domain.models import ArchitectureProposal, utc_now
from ..ports.capabilities import SynthesisPort, SynthesisQuery
from ..ports.repositories import ArchitectureProposalRepository, AuditRepository
from ..ports.transactions import TransactionPort
from .eventlog import (
    EVENT_LEVEL_ERROR,
    EVENT_LEVEL_INFO,
    build_event,
    exception_reason,
    sanitize_text,
)

__all__ = [
    "PROPOSAL_SCHEMA_VERSION",
    "MAX_PROPOSAL_ITEMS",
    "MAX_PROPOSAL_TEXT",
    "MAX_DIGEST_ITEMS",
    "DETERMINISTIC_SOURCE",
    "PROPOSAL_CONTENT_FIELDS",
    "SynthesisError",
    "SynthesisInsufficientEvidenceError",
    "SynthesisActorRequiredError",
    "SynthesisReasonRequiredError",
    "SynthesisProviderError",
    "SynthesisResponseError",
    "SynthesisNotFoundError",
    "proposal_fingerprint",
    "fingerprint_matches",
    "ArchitectureSynthesis",
]

#: Schema version of the stored proposal digest, so a later step can tell two
#: digest shapes apart without guessing.
PROPOSAL_SCHEMA_VERSION = "1.0"

#: How many records one proposal section may carry.
MAX_PROPOSAL_ITEMS = 64

#: How long one operator-facing proposal text may be.
MAX_PROPOSAL_TEXT = 4000

#: How many entries the bounded review digest keeps per list.
MAX_DIGEST_ITEMS = 32

#: The source recorded when the deterministic organizer produced the content.
DETERMINISTIC_SOURCE = "deterministic"

#: The exact, closed set of proposal content keys. Anything else is refused -
#: an unknown key means the content did not come from this contract.
PROPOSAL_CONTENT_FIELDS: tuple[str, ...] = (
    "summary",
    "modules",
    "data_flows",
    "external_dependencies",
    "architecture_rules",
    "proposed_rules",
    "risks",
    "adr_candidates",
    "implementation_phases",
    "unresolved_questions",
    "rationale",
)

#: The field names of one structured record per section. A record may not carry
#: an undeclared field, so a free-form blob can never slip into the proposal.
_RECORD_FIELDS: Mapping[str, tuple[str, ...]] = {
    "modules": ("name", "responsibility", "dependencies", "boundary_notes"),
    "data_flows": ("from", "to", "description"),
    "external_dependencies": ("name", "purpose", "impact"),
    "risks": (
        "severity",
        "probability",
        "impact",
        "description",
        "mitigation",
    ),
    "adr_candidates": ("title", "decision", "rationale", "recommended_status"),
    "implementation_phases": ("phase", "goal", "scope"),
}

#: The digest keys copied out of one review payload. The digest is bounded,
#: sanitized evidence - never a second copy of the review.
_DIGEST_KEYS: tuple[str, ...] = (
    "review_id",
    "reviewed_at",
    "question",
    "project",
    "source_root",
    "architecture_version",
    "step_no",
    "deterministic_gate",
    "providers",
    "evidence",
    "conflicts",
    "judge",
    "decision",
    "cost",
)

#: The keys of the review payload that must be present for synthesis to run.
_REQUIRED_REVIEW_KEYS: tuple[str, ...] = (
    "review_id",
    "question",
    "project",
    "providers",
    "evidence",
)


class SynthesisError(Exception):
    """Base class for architecture-synthesis errors."""


class SynthesisInsufficientEvidenceError(SynthesisError):
    """Raised when there is nothing to synthesize.

    Nothing is read, no provider is asked and nothing is stored: the operator
    never wrote a requirement and the review carries no question either.
    """


class SynthesisActorRequiredError(SynthesisError):
    """Raised when a proposal is created without an explicit actor."""


class SynthesisReasonRequiredError(SynthesisError):
    """Raised when a proposal is created without a non-empty reason."""


class SynthesisProviderError(SynthesisError):
    """Raised when a configured synthesizer fails. Nothing is stored."""


class SynthesisResponseError(SynthesisError):
    """Raised when a synthesizer's content is not a valid proposal answer.

    A malformed answer is a failure, never a proposal: storing it would hand a
    human an artifact the assistant cannot vouch for.
    """


class SynthesisNotFoundError(SynthesisError):
    """Raised when a proposal id does not exist."""


def _require_context(actor: Any, reason: Any) -> None:
    """Fail closed unless the operator and a reason are both named."""
    if not isinstance(actor, str) or not actor.strip():
        raise SynthesisActorRequiredError(
            f"actor must be a non-empty string; got {actor!r}"
        )
    if not isinstance(reason, str) or not reason.strip():
        raise SynthesisReasonRequiredError(
            f"reason must be a non-empty string; got {reason!r}"
        )


def _mapping(value: Any) -> Mapping[str, Any]:
    """A mapping or an empty one - never a surprise."""
    return value if isinstance(value, Mapping) else {}


def _text(value: Any, field_name: str, *, limit: int = MAX_PROPOSAL_TEXT) -> str:
    """A required, bounded, **unsanitized** text value.

    Used for operator-facing content (the requirement, a rationale): it is
    stored as written, and only its length is constrained.
    """
    if not isinstance(value, str) or not value.strip():
        raise SynthesisResponseError(f"{field_name} must be a non-empty string")
    if len(value) > limit:
        raise SynthesisResponseError(
            f"{field_name} must be at most {limit} characters; got {len(value)}"
        )
    return value


def _text_list(value: Any, field_name: str) -> tuple[str, ...]:
    """A bounded sequence of non-empty strings."""
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        raise SynthesisResponseError(
            f"{field_name} must be a sequence of strings, not a bare string"
        )
    items = tuple(value)
    if len(items) > MAX_PROPOSAL_ITEMS:
        raise SynthesisResponseError(
            f"{field_name} must carry at most {MAX_PROPOSAL_ITEMS} entries; "
            f"got {len(items)}"
        )
    return tuple(_text(item, f"{field_name} entry") for item in items)


def _phase(value: Any) -> str:
    """A declared :class:`Phase` value - the build-plan vocabulary is reused."""
    if isinstance(value, Phase):
        return value.value
    if isinstance(value, str):
        try:
            return Phase(value).value
        except ValueError as error:
            raise SynthesisResponseError(
                "implementation_phases.phase must be one of "
                f"{[member.value for member in Phase]}; got {value!r}"
            ) from error
    raise SynthesisResponseError(
        f"implementation_phases.phase must be a Phase or its value; got {value!r}"
    )


def _records(value: Any, section: str) -> tuple[Mapping[str, Any], ...]:
    """Validate one structured proposal section against its declared fields.

    A record may carry exactly the declared field names - every declared field
    present, nothing else - and every value must be a string or a number. That
    is what makes "the synthesizer answered with something else" a *detectable*
    failure instead of a surprise in the panel.
    """
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        raise SynthesisResponseError(
            f"{section} must be a sequence of records, not a bare string"
        )
    fields = _RECORD_FIELDS[section]
    records = tuple(value)
    if len(records) > MAX_PROPOSAL_ITEMS:
        raise SynthesisResponseError(
            f"{section} must carry at most {MAX_PROPOSAL_ITEMS} records; "
            f"got {len(records)}"
        )
    checked: list[Mapping[str, Any]] = []
    for record in records:
        if not isinstance(record, Mapping):
            raise SynthesisResponseError(
                f"{section} records must be mappings; got {record!r}"
            )
        unknown = sorted(set(record) - set(fields))
        if unknown:
            raise SynthesisResponseError(
                f"{section} record carries unknown field(s) {unknown}; the "
                f"contract declares {list(fields)}"
            )
        missing = [field for field in fields if field not in record]
        if missing:
            raise SynthesisResponseError(
                f"{section} record is missing {missing}; the contract declares "
                f"{list(fields)}"
            )
        row: dict[str, Any] = {}
        for field in fields:
            item = record[field]
            if field == "phase":
                row[field] = _phase(item)
            elif isinstance(item, (int, float)) and not isinstance(item, bool):
                row[field] = item
            else:
                row[field] = _text(item, f"{section}.{field}")
        checked.append(row)
    return tuple(checked)


def _content(value: Any, *, source: str) -> dict[str, Any]:
    """Validate a synthesizer's answer against the closed proposal contract."""
    if not isinstance(value, Mapping):
        raise SynthesisResponseError(
            f"{source} must answer with a mapping; got {value!r}"
        )
    unknown = sorted(set(value) - set(PROPOSAL_CONTENT_FIELDS))
    if unknown:
        raise SynthesisResponseError(
            f"{source} answered with unknown field(s) {unknown}; the contract "
            f"declares {list(PROPOSAL_CONTENT_FIELDS)}"
        )
    missing = [field for field in PROPOSAL_CONTENT_FIELDS if field not in value]
    if missing:
        raise SynthesisResponseError(
            f"{source} did not answer {missing}; every content field must be "
            "declared explicitly (an empty list is a valid answer)"
        )
    return {
        "summary": _text(value["summary"], "summary"),
        "modules": _records(value["modules"], "modules"),
        "data_flows": _records(value["data_flows"], "data_flows"),
        "external_dependencies": _records(
            value["external_dependencies"], "external_dependencies"
        ),
        "architecture_rules": _text_list(
            value["architecture_rules"], "architecture_rules"
        ),
        "proposed_rules": _text_list(
            value["proposed_rules"], "proposed_rules"
        ),
        "risks": _records(value["risks"], "risks"),
        "adr_candidates": _records(value["adr_candidates"], "adr_candidates"),
        "implementation_phases": _records(
            value["implementation_phases"], "implementation_phases"
        ),
        "unresolved_questions": _text_list(
            value["unresolved_questions"], "unresolved_questions"
        ),
        "rationale": _text(value["rationale"], "rationale"),
    }


def _bounded(value: Any, *, depth: int = 0) -> Any:
    """A JSON-safe, sanitized, bounded copy of review evidence.

    Every string is redacted and collapsed by the shared log contract, so a
    provider body or a credential can never reach the database through a
    proposal; lists are capped so one review cannot grow the row without limit.
    """
    if depth > 6:
        return sanitize_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, Mapping):
        return {
            sanitize_text(key, limit=64): _bounded(item, depth=depth + 1)
            for key, item in list(value.items())[:MAX_DIGEST_ITEMS]
        }
    if isinstance(value, (list, tuple)):
        return [
            _bounded(item, depth=depth + 1)
            for item in list(value)[:MAX_DIGEST_ITEMS]
        ]
    return sanitize_text(value)


def _payload_of(review: Any) -> Mapping[str, Any]:
    """The JSON-safe payload of one advisory review.

    Accepts the review result object (anything exposing ``to_dict``) or an
    already-built payload mapping, and refuses anything else: synthesis reads
    exactly one review, and it must be able to inspect its shape.
    """
    if isinstance(review, Mapping):
        payload: Any = dict(review)
    else:
        to_dict = getattr(review, "to_dict", None)
        if not callable(to_dict):
            raise SynthesisError(
                "review must be a review payload mapping or an object "
                f"exposing to_dict(); got {type(review).__name__}"
            )
        payload = to_dict()
    if not isinstance(payload, Mapping):
        raise SynthesisError("the review payload must be a mapping")
    missing = [key for key in _REQUIRED_REVIEW_KEYS if key not in payload]
    if missing:
        raise SynthesisError(
            f"the review payload is missing {missing}; synthesis needs exactly "
            "one complete review"
        )
    return payload


def _digest(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The bounded, sanitized evidence digest stored inside a proposal.

    Carries what the review said - advisor statuses and claims, the merged
    evidence, the conflicts, the judge, the advisory decision, the deterministic
    gate and the cost - so an approved proposal stays explicable after a restart
    even though the review itself is ephemeral.
    """
    digest: dict[str, Any] = {
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "origin": "architecture-review",
    }
    for key in _DIGEST_KEYS:
        if key in payload:
            digest[key] = _bounded(payload[key])
    providers = payload.get("providers")
    stages = tuple(providers) if isinstance(providers, (list, tuple)) else ()
    statuses = [
        str(_mapping(stage).get("status") or "")
        for stage in stages
        if isinstance(stage, Mapping)
    ]
    digest["provider_count"] = len(stages)
    digest["finding_count"] = statuses.count("FINDING")
    digest["abstain_count"] = statuses.count("ABSTAIN")
    digest["error_count"] = statuses.count("ERROR")
    digest["finding_sources"] = [
        str(_mapping(stage).get("source") or "")
        for stage in stages
        if isinstance(stage, Mapping)
        and str(_mapping(stage).get("status") or "") == "FINDING"
    ]
    return digest


def _review_questions(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """The review's own unresolved questions, verbatim and bounded."""
    evidence = _mapping(payload.get("evidence"))
    raw = evidence.get("unresolved_questions") or ()
    if isinstance(raw, (str, bytes)):
        return ()
    questions: list[str] = []
    for item in list(raw)[:MAX_DIGEST_ITEMS]:
        text = item if isinstance(item, str) else str(item)
        if text.strip():
            questions.append(text)
    return tuple(questions)


def _requirement(review_question: Any, requirement: Any) -> str:
    """The operator's requirement, echoed verbatim.

    The review question *is* the operator's own text, so an explicit addendum is
    optional and the fallback is the question exactly as it was asked. Nothing
    is sanitized, shortened or reworded - the proposal is the record of what the
    operator actually asked for - but the length is bounded.
    """
    explicit = requirement if isinstance(requirement, str) else ""
    text = explicit.strip() or str(review_question or "").strip()
    if not text:
        raise SynthesisInsufficientEvidenceError(
            "there is nothing to synthesize: neither the operator nor the "
            "review supplied a requirement"
        )
    if len(text) > MAX_PROPOSAL_TEXT:
        raise SynthesisError(
            f"the requirement must be at most {MAX_PROPOSAL_TEXT} characters; "
            f"got {len(text)}"
        )
    return explicit if explicit.strip() else text


def _skeleton(
    *,
    project: str,
    requirement: str,
    digest: Mapping[str, Any],
    questions: Sequence[str],
) -> dict[str, Any]:
    """The deterministic organizer's honest skeleton.

    It fills exactly what it can *derive* and leaves every section that would
    require invention empty - each one named as an unresolved question instead.
    No module, risk, rule or decision is ever guessed: the assistant may not
    describe the architecture of a managed project that nobody proposed.
    """
    sources = [str(source) for source in digest.get("finding_sources") or ()]
    providers = int(digest.get("provider_count") or 0)
    findings = int(digest.get("finding_count") or 0)
    first_line = requirement.strip().splitlines()[0].strip()
    summary = (
        f"Managed-project architecture proposal for {project}: {first_line}"
    )
    rationale = (
        "Assembled deterministically from the operator's own requirement and "
        f"the advisory review {digest.get('review_id')!r}. {findings} of "
        f"{providers} advisor(s) returned a finding"
        + (f" ({', '.join(sources)})" if sources else "")
        + ". No module boundary, risk, rule or decision was invented: every "
        "section this organizer could not derive from the review is empty and "
        "named as an unresolved question below. Sections may be filled by an "
        "explicitly configured synthesizer; until then they stay open."
    )
    unresolved = list(questions)
    unresolved.append(
        "Which modules (name, responsibility, dependencies, boundary notes) "
        f"should {project} consist of?"
    )
    unresolved.append(
        f"Which data flows and external dependencies does {project} have?"
    )
    unresolved.append(
        f"Which architecture rules should {project} be held to, and how would "
        "they be enforced?"
    )
    return {
        "summary": summary,
        "modules": [],
        "data_flows": [],
        "external_dependencies": [],
        "architecture_rules": [],
        "proposed_rules": [],
        "risks": [],
        "adr_candidates": [],
        "implementation_phases": [],
        "unresolved_questions": unresolved,
        "rationale": rationale,
    }


def _proposal_id(
    *,
    project: str,
    review_id: str,
    requirement: str,
    revision_no: int,
    revision_of: Optional[str],
    created_at: datetime,
) -> str:
    """A content-addressed proposal id - stable for identical inputs."""
    material = "|".join(
        (
            project,
            review_id,
            str(revision_no),
            revision_of or "-",
            requirement,
            created_at.isoformat(),
        )
    )
    return "proposal-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _fingerprint(
    *,
    project: str,
    review_id: str,
    architecture_version: Optional[str],
    requirement: str,
    revision_no: int,
    digest: Mapping[str, Any],
) -> str:
    """The identity of a proposal's inputs, so a stale one can never pass.

    Clock-free on purpose: the same review and the same requirement always
    fingerprint identically, and any change to the evidence changes it.
    """
    digest_hash = hashlib.sha256(
        json.dumps(dict(digest), sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    material = "|".join(
        (
            project,
            review_id,
            architecture_version or "-",
            requirement,
            str(revision_no),
            digest_hash,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def proposal_fingerprint(
    *,
    project: str,
    review_id: str,
    architecture_version: Optional[str],
    requirement: str,
    revision_no: int,
    digest: Mapping[str, Any],
) -> str:
    """The public name of :func:`_fingerprint` (the approval path re-verifies)."""
    return _fingerprint(
        project=project,
        review_id=review_id,
        architecture_version=architecture_version,
        requirement=requirement,
        revision_no=revision_no,
        digest=digest,
    )


def fingerprint_matches(proposal: ArchitectureProposal) -> bool:
    """Whether a persisted proposal is still exactly what it claims to be.

    Recomputed from the proposal's own stored content and compared with the
    stored fingerprint, so a tampered, truncated or hand-edited row is refused
    instead of being approved.
    """
    return proposal.fingerprint == _fingerprint(
        project=proposal.project,
        review_id=proposal.source_review_id,
        architecture_version=proposal.architecture_version,
        requirement=proposal.requirement,
        revision_no=proposal.revision_no,
        digest=proposal.review_digest,
    )


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
    on_event(
        build_event(
            level=level,
            component="Synthesis",
            action=action,
            message=message,
            timestamp=stamp(),
        )
    )


class ArchitectureSynthesis:
    """Turns one advisory review into one persisted managed-project proposal.

    It is handed three collaborators and nothing else: the proposal repository,
    the append-only audit trail and the shared transaction boundary. In
    particular it holds **no** architecture port - it cannot read or write the
    assistant's baseline, an ACR, an ADR, a risk or the realization gate - so the
    boundary between the assistant's own architecture and the managed project's
    design is enforced by what it was given, not by convention.

    ``synthesis`` is optional. Without it the deterministic organizer runs: no
    provider, no configuration, no cost - and the very same persisted proposal
    shape, so a proposal is always producible.
    """

    def __init__(
        self,
        proposals: ArchitectureProposalRepository,
        audit: AuditRepository,
        transactions: TransactionPort,
        *,
        clock: Callable[[], datetime] = utc_now,
        synthesis: Optional[SynthesisPort] = None,
    ) -> None:
        if not callable(getattr(proposals, "upsert", None)):
            raise ValueError(
                "proposals must implement ArchitectureProposalRepository "
                f"(upsert); got {type(proposals).__name__}"
            )
        if not callable(getattr(audit, "append", None)):
            raise ValueError(
                "audit must implement AuditRepository (append); got "
                f"{type(audit).__name__}"
            )
        if not callable(getattr(transactions, "transaction", None)):
            raise ValueError(
                "transactions must implement TransactionPort (transaction); "
                f"got {type(transactions).__name__}"
            )
        if not callable(clock):
            raise ValueError("clock must be a callable returning a datetime")
        if synthesis is not None and not isinstance(synthesis, SynthesisPort):
            raise ValueError(
                "synthesis must implement SynthesisPort (synthesize); got "
                f"{type(synthesis).__name__}"
            )
        self._proposals = proposals
        self._audit = audit
        self._transactions = transactions
        self._clock = clock
        self._synthesis = synthesis

    # -- read-only queries -------------------------------------------------

    @property
    def has_synthesizer(self) -> bool:
        """Whether an optional synthesizer is configured (the panel shows it)."""
        return self._synthesis is not None

    def proposals(self) -> tuple[ArchitectureProposal, ...]:
        """Every persisted proposal, oldest first - a read-only board query."""
        return tuple(self._proposals.list())

    def latest(self, project: str) -> Optional[ArchitectureProposal]:
        """The newest proposal of one managed project, or ``None``."""
        if not isinstance(project, str) or not project.strip():
            raise SynthesisError(
                f"project must be a non-empty string; got {project!r}"
            )
        found = tuple(self._proposals.list_for_project(project))
        if not found:
            return None
        return max(
            found,
            key=lambda item: (
                item.created_at,
                item.revision_no,
                item.proposal_id,
            ),
        )

    def require(self, proposal_id: str) -> ArchitectureProposal:
        """The proposal with this id, or fail closed."""
        if not isinstance(proposal_id, str) or not proposal_id.strip():
            raise SynthesisError(
                f"proposal_id must be a non-empty string; got {proposal_id!r}"
            )
        proposal = self._proposals.get(proposal_id)
        if proposal is None:
            raise SynthesisNotFoundError(
                f"proposal {proposal_id!r} does not exist"
            )
        return proposal

    # -- the one write path ------------------------------------------------

    def synthesize(
        self,
        review: Any,
        *,
        requirement: str = "",
        actor: str = "",
        reason: str = "",
        revision_of: Optional[str] = None,
        on_event: Optional[Callable[[Mapping[str, Any]], None]] = None,
    ) -> ArchitectureProposal:
        """Turn one review into one persisted ``DRAFT`` proposal.

        Nothing is written until the content is valid, and exactly one proposal
        row plus exactly one ``PROPOSAL``/``CREATE`` audit entry are written in
        one transaction. The returned proposal is authoritative only as the
        *managed project's* design record - it never touches the assistant's
        baseline, an ACR, an ADR, a risk or the realization gate.

        ``revision_of`` is the explicit operator request "this replaces that
        proposal": the predecessor is marked ``SUPERSEDED`` in the same
        transaction, and the new row carries ``revision_no + 1``. History is
        never rewritten - both rows remain.
        """
        _require_context(actor, reason)
        payload = _payload_of(review)
        requirement_text = _requirement(payload.get("question"), requirement)
        digest = _digest(payload)
        stamp = self._clock
        _emit(
            on_event,
            level=EVENT_LEVEL_INFO,
            action="synthesis-start",
            message="architecture proposal synthesis requested by the operator",
            stamp=stamp,
        )
        project = str(payload.get("project") or "").strip()
        if not project:
            raise SynthesisError(
                "the review payload carries no project identity"
            )
        skeleton = _skeleton(
            project=project,
            requirement=requirement_text,
            digest=digest,
            questions=_review_questions(payload),
        )
        _emit(
            on_event,
            level=EVENT_LEVEL_INFO,
            action="review-loaded",
            message=(
                f"review {payload.get('review_id')} loaded: "
                f"{digest.get('finding_count', 0)} of "
                f"{digest.get('provider_count', 0)} advisor(s) returned a "
                f"finding, "
                f"{len(payload.get('conflicts') or ())} conflict(s)"
            ),
            stamp=stamp,
        )

        source = DETERMINISTIC_SOURCE
        content = _content(skeleton, source=DETERMINISTIC_SOURCE)
        if self._synthesis is not None:
            _emit(
                on_event,
                level=EVENT_LEVEL_INFO,
                action="synthesis-provider-start",
                message="the configured synthesizer was asked for a proposal",
                stamp=stamp,
            )
            version = payload.get("architecture_version")
            query = SynthesisQuery(
                project=project,
                requirement=requirement_text,
                review_id=str(payload["review_id"]),
                architecture_version=(
                    None if version is None else str(version)
                ),
                skeleton=skeleton,
                digest=digest,
            )
            try:
                answer = self._synthesis.synthesize(query)
            except BaseException as error:  # reported, then fail closed
                _emit(
                    on_event,
                    level=EVENT_LEVEL_ERROR,
                    action="error",
                    message=(
                        "the configured synthesizer failed: "
                        f"{exception_reason(error)}"
                    ),
                    stamp=stamp,
                )
                raise SynthesisProviderError(
                    "the configured synthesizer failed: "
                    f"{exception_reason(error)}"
                ) from error
            source = str(getattr(answer, "source", "") or "synthesizer")
            try:
                content = _content(
                    getattr(answer, "content", None), source=source
                )
            except SynthesisResponseError:
                _emit(
                    on_event,
                    level=EVENT_LEVEL_ERROR,
                    action="error",
                    message=(
                        f"the synthesizer {source!r} answered with content the "
                        "proposal contract refuses; nothing was stored"
                    ),
                    stamp=stamp,
                )
                raise

        created_at = stamp()
        predecessor = self._revision_base(revision_of, project=project)
        revision_no = 1 if predecessor is None else predecessor.revision_no + 1
        proposal_id = _proposal_id(
            project=project,
            review_id=str(payload["review_id"]),
            requirement=requirement_text,
            revision_no=revision_no,
            revision_of=revision_of,
            created_at=created_at,
        )
        version = payload.get("architecture_version")
        proposal = ArchitectureProposal(
            proposal_id=proposal_id,
            project=project,
            created_at=created_at,
            requirement=requirement_text,
            summary=content["summary"],
            source_review_id=str(payload["review_id"]),
            fingerprint=_fingerprint(
                project=project,
                review_id=str(payload["review_id"]),
                architecture_version=(
                    None if version is None else str(version)
                ),
                requirement=requirement_text,
                revision_no=revision_no,
                digest=digest,
            ),
            modules=content["modules"],
            data_flows=content["data_flows"],
            external_dependencies=content["external_dependencies"],
            architecture_rules=content["architecture_rules"],
            proposed_rules=content["proposed_rules"],
            risks=content["risks"],
            adr_candidates=content["adr_candidates"],
            implementation_phases=content["implementation_phases"],
            unresolved_questions=content["unresolved_questions"],
            rationale=content["rationale"],
            review_digest=digest,
            architecture_version=(None if version is None else str(version)),
            revision_no=revision_no,
            revision_of=revision_of,
            status=ProposalStatus.DRAFT,
        )
        entry = self._entry(
            proposal,
            actor=actor,
            reason=reason,
            source=source,
            revision_of=revision_of,
            now=created_at,
        )
        with self._transactions.transaction():
            if predecessor is not None:
                self._proposals.upsert(
                    replace(
                        predecessor,
                        status=ProposalStatus.SUPERSEDED,
                        superseded_by=proposal_id,
                    )
                )
            self._proposals.upsert(proposal)
            self._audit.append(entry)
        _emit(
            on_event,
            level=EVENT_LEVEL_INFO,
            action="proposal-generated",
            message=(
                f"proposal {proposal_id} generated for {project} "
                f"(revision {revision_no}, status DRAFT, "
                f"{len(proposal.modules)} module(s), "
                f"{len(proposal.unresolved_questions)} unresolved "
                "question(s))"
            ),
            stamp=stamp,
        )
        return proposal

    # -- internals ---------------------------------------------------------

    def _revision_base(
        self, revision_of: Optional[str], *, project: str
    ) -> Optional[ArchitectureProposal]:
        """The predecessor a revision replaces, or fail closed.

        Only a ``DRAFT`` or ``REVISION_REQUESTED`` proposal of the *same*
        managed project may be superseded, and only explicitly - synthesis never
        guesses which proposal a revision belongs to.
        """
        if revision_of is None:
            return None
        predecessor = self.require(revision_of)
        if predecessor.project != project:
            raise SynthesisError(
                f"proposal {revision_of} belongs to project "
                f"{predecessor.project!r}, not {project!r}"
            )
        allowed = (ProposalStatus.DRAFT, ProposalStatus.REVISION_REQUESTED)
        if predecessor.status not in allowed:
            listed = ", ".join(member.value for member in allowed)
            raise SynthesisError(
                f"proposal {revision_of} is {predecessor.status.value}; only "
                f"{listed} may be superseded by a revision"
            )
        return predecessor

    def _entry(
        self,
        proposal: ArchitectureProposal,
        *,
        actor: str,
        reason: str,
        source: str,
        revision_of: Optional[str],
        now: datetime,
    ) -> AuditEntry:
        """One audit entry for one generated proposal - structured, not prose."""
        return AuditEntry(
            entity_type=AuditEntityType.PROPOSAL,
            entity_id=proposal.proposal_id,
            action=AuditAction.CREATE,
            detail={
                "operation": "generate-proposal",
                "actor": actor,
                "reason": reason,
                "project": proposal.project,
                "source_review_id": proposal.source_review_id,
                "architecture_version": proposal.architecture_version,
                "revision_no": proposal.revision_no,
                "revision_of": revision_of,
                "status": proposal.status.value,
                "fingerprint": proposal.fingerprint,
                "synthesis_source": source,
                "provider_count": int(
                    proposal.review_digest.get("provider_count") or 0
                ),
                "finding_count": int(
                    proposal.review_digest.get("finding_count") or 0
                ),
                "module_count": len(proposal.modules),
                "risk_count": len(proposal.risks),
                "adr_candidate_count": len(proposal.adr_candidates),
                "unresolved_question_count": len(
                    proposal.unresolved_questions
                ),
            },
            created_at=now,
        )
