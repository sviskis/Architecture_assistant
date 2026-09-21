"""Proposal approval - the human decision on a **managed-project** proposal.

What this module decides
------------------------
Exactly three human acts on one persisted
:class:`~architecture_assistant.domain.models.ArchitectureProposal`:

* ``approve`` - ``DRAFT -> APPROVED``: *this is the human-approved architecture
  of the managed project*;
* ``reject`` - ``DRAFT -> REJECTED``: the design is refused;
* ``request_revision`` - ``DRAFT -> REVISION_REQUESTED``: the design is sent back
  with the operator's own feedback.

Each act writes exactly one status change and exactly one audit entry inside one
transaction, and each requires an explicit ``actor`` and a non-empty ``reason``
(a revision request additionally requires ``feedback``).

What it deliberately is not
---------------------------
The proposal is **project-scoped** state. This use-case is therefore handed no
``ArchitectureEvolution``, no ``ArchitectureVersioning``, no ``ADRManager``, no
``RiskManager`` and no realization port: approving a managed project's design can
never mutate the assistant's own baseline, create an ACR, write an assistant ADR,
a risk or a rule, or reach the deterministic realization gate. There is
deliberately **no** path from here that moves ``ArchitectureVersion``.

It also never re-synthesizes. ``request_revision`` records the request and the
feedback; a new revision exists only when the operator explicitly generates one
again (``ArchitectureSynthesis.synthesize(..., revision_of=...)``), which marks
the predecessor ``SUPERSEDED``. No autonomous loop, no timer, no retry.

Fail-closed rules
-----------------
* A missing actor, a blank reason or a blank revision feedback raises before any
  read.
* A proposal that is ``APPROVED``, ``REJECTED``, ``REVISION_REQUESTED`` or
  ``SUPERSEDED`` can never be approved.
* A proposal that a newer revision replaced is refused
  (:class:`ProposalNotApprovableError`), so a stale design cannot be approved.
* A proposal whose own content no longer matches its recorded fingerprint is
  refused.
* A proposal that does not belong to the database's single managed project is
  refused.
* Repeating the *identical* decision is idempotent (no second audit entry);
  repeating it with a different reason - or as the other decision - fails closed.

Imports are restricted to ``domain``, ``ports`` and sibling ``application``
modules - never ``infrastructure``, never ``sqlite3``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any, Callable, Mapping, Optional

from ..domain.audit import AuditAction, AuditEntityType, AuditEntry
from ..domain.enums import ProposalStatus
from ..domain.models import ArchitectureProposal, Project, utc_now
from ..ports.repositories import (
    ArchitectureProposalRepository,
    AuditRepository,
    ProjectRepository,
)
from ..ports.transactions import TransactionPort
from .architecture_synthesis import fingerprint_matches

__all__ = [
    "ProposalApprovalError",
    "ProposalNotFoundError",
    "ProposalActorRequiredError",
    "ProposalReasonRequiredError",
    "ProposalFeedbackRequiredError",
    "ProposalNotApprovableError",
    "ProposalDecisionConflictError",
    "ProposalApproval",
]


class ProposalApprovalError(Exception):
    """Base class for proposal-approval errors."""


class ProposalNotFoundError(ProposalApprovalError):
    """Raised when a proposal id does not exist."""


class ProposalActorRequiredError(ProposalApprovalError):
    """Raised when a decision is taken without an explicit actor."""


class ProposalReasonRequiredError(ProposalApprovalError):
    """Raised when a decision is taken without a non-empty reason."""


class ProposalFeedbackRequiredError(ProposalApprovalError):
    """Raised when a revision is requested without the operator's feedback."""


class ProposalNotApprovableError(ProposalApprovalError):
    """Raised when the proposal's own state forbids the requested decision.

    Stale, superseded, already-decided or foreign-project proposals are refused
    here - and because the refusal happens before the transaction, nothing is
    written and nothing is audited.
    """


class ProposalDecisionConflictError(ProposalApprovalError):
    """Raised when a repeated decision disagrees with the one already recorded."""


def _require_context(actor: Any, reason: Any) -> None:
    """Fail closed unless the decision names both an actor and a reason."""
    if not isinstance(actor, str) or not actor.strip():
        raise ProposalActorRequiredError(
            f"actor must be a non-empty string; got {actor!r}"
        )
    if not isinstance(reason, str) or not reason.strip():
        raise ProposalReasonRequiredError(
            f"reason must be a non-empty string; got {reason!r}"
        )


class ProposalApproval:
    """Takes the three human decisions on one managed-project proposal.

    It is handed three repository ports and the shared transaction boundary -
    never a monitor, a worker, an architecture port or an adapter - so the only
    state it can move is the proposal's own lifecycle.
    """

    def __init__(
        self,
        proposals: ArchitectureProposalRepository,
        projects: ProjectRepository,
        audit: AuditRepository,
        transactions: TransactionPort,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not callable(getattr(proposals, "upsert", None)):
            raise ValueError(
                "proposals must implement ArchitectureProposalRepository "
                f"(upsert); got {type(proposals).__name__}"
            )
        if not callable(getattr(projects, "list", None)):
            raise ValueError(
                "projects must implement ProjectRepository (list); got "
                f"{type(projects).__name__}"
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
        self._proposals = proposals
        self._projects = projects
        self._audit = audit
        self._transactions = transactions
        self._clock = clock

    # -- read-only queries -------------------------------------------------

    def require(self, proposal_id: str) -> ArchitectureProposal:
        """The proposal with this id, or fail closed."""
        if not isinstance(proposal_id, str) or not proposal_id.strip():
            raise ProposalApprovalError(
                f"proposal_id must be a non-empty string; got {proposal_id!r}"
            )
        proposal = self._proposals.get(proposal_id)
        if proposal is None:
            raise ProposalNotFoundError(
                f"proposal {proposal_id!r} does not exist"
            )
        return proposal

    # -- the three human acts ----------------------------------------------

    def approve(
        self, proposal_id: str, *, actor: str, reason: str
    ) -> ArchitectureProposal:
        """``DRAFT -> APPROVED``: the managed project's approved design."""
        return self._decide(
            proposal_id,
            actor=actor,
            reason=reason,
            target=ProposalStatus.APPROVED,
            operation="approve-proposal",
            action=AuditAction.UPDATE,
        )

    def reject(
        self, proposal_id: str, *, actor: str, reason: str
    ) -> ArchitectureProposal:
        """``DRAFT -> REJECTED``: the managed project's design is refused."""
        return self._decide(
            proposal_id,
            actor=actor,
            reason=reason,
            target=ProposalStatus.REJECTED,
            operation="reject-proposal",
            action=AuditAction.REJECT,
        )

    def request_revision(
        self,
        proposal_id: str,
        *,
        actor: str,
        reason: str,
        feedback: str,
    ) -> ArchitectureProposal:
        """``DRAFT -> REVISION_REQUESTED``: record the operator's feedback.

        This never re-synthesizes: a new revision exists only when the operator
        explicitly generates one, and this call is what tells them what to
        change.
        """
        if not isinstance(feedback, str) or not feedback.strip():
            raise ProposalFeedbackRequiredError(
                "feedback must be a non-empty string: a revision request must "
                "say what should change"
            )
        return self._decide(
            proposal_id,
            actor=actor,
            reason=reason,
            target=ProposalStatus.REVISION_REQUESTED,
            operation="request-proposal-revision",
            action=AuditAction.UPDATE,
            feedback=feedback,
        )

    # -- internals ---------------------------------------------------------

    def _decide(
        self,
        proposal_id: str,
        *,
        actor: str,
        reason: str,
        target: ProposalStatus,
        operation: str,
        action: AuditAction,
        feedback: str = "",
    ) -> ArchitectureProposal:
        """One decision: validate, then move the status and audit it once.

        Validation happens *before* the transaction, so every refusal leaves the
        row, the audit trail and the transaction log untouched.
        """
        _require_context(actor, reason)
        proposal = self.require(proposal_id)
        self._assert_intact(proposal)
        if proposal.status is target:
            # the identical decision is already recorded: idempotent, and no
            # second audit entry - but a *different* decision on the same row is
            # refused rather than silently overwritten
            if self._same_decision(proposal, actor, reason, target, feedback):
                return proposal
            raise ProposalDecisionConflictError(
                f"proposal {proposal_id} is already {target.value} with a "
                "different decision; refusing to overwrite history"
            )
        if proposal.status is not ProposalStatus.DRAFT:
            raise ProposalNotApprovableError(
                f"proposal {proposal_id} is {proposal.status.value}; only a "
                "DRAFT proposal can be decided"
            )
        self._assert_current(proposal)
        self._assert_project(proposal)

        now = self._clock()
        decided = replace(
            proposal,
            status=target,
            decided_by=actor,
            decided_at=now,
            decision_reason=reason,
            revision_feedback=feedback,
        )
        entry = self._entry(
            proposal,
            decided,
            action=action,
            operation=operation,
            actor=actor,
            reason=reason,
            now=now,
            feedback=feedback,
        )
        with self._transactions.transaction():
            self._proposals.upsert(decided)
            self._audit.append(entry)
        return decided

    def _same_decision(
        self,
        proposal: ArchitectureProposal,
        actor: str,
        reason: str,
        target: ProposalStatus,
        feedback: str,
    ) -> bool:
        """Whether the recorded decision is exactly the requested one."""
        if proposal.decided_by != actor or proposal.decision_reason != reason:
            return False
        if target is ProposalStatus.REVISION_REQUESTED:
            return proposal.revision_feedback == feedback
        return True

    def _assert_intact(self, proposal: ArchitectureProposal) -> None:
        """Refuse a proposal whose content no longer matches its fingerprint."""
        if not fingerprint_matches(proposal):
            raise ProposalNotApprovableError(
                f"proposal {proposal.proposal_id} does not match its recorded "
                "fingerprint; refusing to decide on it"
            )

    def _assert_current(self, proposal: ArchitectureProposal) -> None:
        """Refuse a proposal a newer revision already replaced (stale)."""
        if proposal.superseded_by is not None:
            raise ProposalNotApprovableError(
                f"proposal {proposal.proposal_id} was superseded by "
                f"{proposal.superseded_by}; decide on the newer revision"
            )
        for other in self._proposals.list():
            if other.revision_of == proposal.proposal_id:
                raise ProposalNotApprovableError(
                    f"proposal {proposal.proposal_id} was replaced by revision "
                    f"{other.proposal_id}; decide on the newer revision"
                )

    def _assert_project(self, proposal: ArchitectureProposal) -> None:
        """Refuse a proposal that belongs to a different managed project."""
        projects = tuple(self._projects.list())
        if not projects:
            raise ProposalApprovalError(
                "the source of truth contains no project"
            )
        if len(projects) > 1:
            names = ", ".join(sorted(project.name for project in projects))
            raise ProposalApprovalError(
                f"expected exactly one project, found {len(projects)}: {names}"
            )
        current: Project = projects[0]
        if proposal.project != current.name:
            raise ProposalNotApprovableError(
                f"proposal {proposal.proposal_id} belongs to project "
                f"{proposal.project!r}, but this database manages "
                f"{current.name!r}"
            )

    def _entry(
        self,
        before: ArchitectureProposal,
        after: ArchitectureProposal,
        *,
        action: AuditAction,
        operation: str,
        actor: str,
        reason: str,
        now: datetime,
        feedback: str,
    ) -> AuditEntry:
        """One audit entry for one human decision - structured, never prose."""
        detail: dict[str, Any] = {
            "operation": operation,
            "actor": actor,
            "reason": reason,
            "project": after.project,
            "status_from": before.status.value,
            "status_to": after.status.value,
            "revision_no": after.revision_no,
            "revision_of": after.revision_of,
            "source_review_id": after.source_review_id,
            "architecture_version": after.architecture_version,
            "fingerprint": after.fingerprint,
            "decided_at": now.isoformat(),
        }
        if feedback:
            detail["revision_feedback"] = feedback
        return AuditEntry(
            entity_type=AuditEntityType.PROPOSAL,
            entity_id=after.proposal_id,
            action=action,
            detail=detail,
            created_at=now,
        )
